"""Natural-language audit-workbench assistant coordinator.

An auditor writes in plain English; an LLM responds by *calling registered
tools* that run locally against the workspace. The coordinator is deliberately
domain-neutral: audit progress, prior runs, planning artifacts, documents, and
tabular analysis are peer capabilities, and a question is never equated with
data analysis.

The loop is read-only by default. A caller that lends a :class:`Commander` also
gets ``start_command`` and ``start_action``, letting the same turn decide by
ordinary tool-calling whether to answer or to hand work to the agent runner —
which is why no separate ask/act classification step exists ahead of it.

Model tool results include compact, unmasked previews. Full computations stay
local and previews are bounded only to protect the model's context window.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from . import analysis_results, analytics, debug_store, document_context as document_context_module, document_search, documents, explore, llm, model_context, sandbox, tooling
from .workspaces import Workspace, WorkspaceError

MAX_STEPS = 8
# Model previews are capped hard so large populations fit in context.
MODEL_PREVIEW_ROWS = 40
# Full results streamed to the browser (local) are capped only to stay light.
ARTIFACT_ROWS = 200

# Conversation history is text-only and deliberately small.
HISTORY_MAX_MESSAGES = 8
HISTORY_MAX_CHARACTERS = 12_000
HISTORY_MAX_MESSAGE_CHARACTERS = 2_000

def table_metadata(
    workspace: Workspace,
    table: str,
    *,
    include_category_values: bool = True,
) -> dict:
    """Compatibility wrapper for the shared bounded profile handler."""
    return tooling.table_profile(
        workspace, table, include_category_values=include_category_values,
    )


def schema_brief(workspace: Workspace) -> list[dict]:
    """Compatibility wrapper for the shared schema handler."""
    return tooling.table_schemas(workspace)


def workspace_manifest(workspace: Workspace) -> dict:
    """Content-light opening context for domain selection.

    Detailed table schemas, audit state, runs, and artifact content are all
    discoverable through registered tools. Keeping the opening prompt to names
    and counts avoids biasing every question toward whichever domain happens to
    have the largest payload.
    """

    from . import doc_tests

    test_count = len(workspace.data_tests) + len(doc_tests.list_tests(workspace))
    return {
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "revision": workspace.revision,
        },
        "available_domains": [
            "audit_progress",
            "agent_runs",
            "planning_and_rcm",
            "fieldwork_and_findings",
            "reporting",
            "documents",
            "data_analysis",
        ],
        "artifact_counts": {
            "tables": len(workspace.tables),
            "joins": len(workspace.joins),
            "saved_analyses": len(workspace.analyses),
            "documents": len(workspace.documents),
            "rcm_rows": len(workspace.rcm),
            "tests": test_count,
            "work_program_items": len(workspace.work_program),
            "findings": len(workspace.findings),
        },
        "table_names": workspace.table_names(),
        "planning": {
            "context_available": bool(
                any(
                    str(value or "").strip()
                    for key, value in (workspace.planning.get("context") or {}).items()
                    if key != "interview_answers"
                )
            ),
            "apm_available": bool(
                str(workspace.planning.get("apm_markdown") or "").strip()
            ),
        },
        "report_available": bool(
            str((workspace.report or {}).get("markdown") or "").strip()
        ),
        # Counts only, so a question about what the analyses found is answered
        # by reading their recorded outcomes rather than re-running procedures.
        "analysis_outcomes": analysis_results.analyses_summary_payload(workspace)["counts"],
    }


# ================================================================ model views
def _frame_for_model(df: pl.DataFrame) -> dict:
    """Return a bounded, unmasked model preview of a result frame."""
    return model_context.project_frame(df, row_limit=MODEL_PREVIEW_ROWS)


def _artifact_frame(df: pl.DataFrame) -> dict:
    return explore.frame_payload(df, ARTIFACT_ROWS)


# ====================================================================== tools
_TOOL_SCHEMAS = [
    tooling.TABLE_SCHEMAS_TOOL,
    tooling.TABLE_PROFILE_TOOL,
    {
        "type": "function",
        "function": {
            "name": "get_audit_progress",
            "description": (
                "Get deterministic audit-lifecycle readiness, blockers, current "
                "artifact counts, recommended next tasks, and a compact latest-run "
                "summary. Use for questions about what is done, what remains, what "
                "is blocked, audit status, or what should happen next."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_run",
            "description": (
                "Get a bounded read-only account of the latest agent run: command, "
                "status, completed/skipped/blocked stages, artifacts, closing "
                "message, and open items. Defaults to the latest run linked to this "
                "chat; request workspace scope to inspect the latest run anywhere "
                "in the engagement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["chat", "workspace"],
                        "description": "Which run history to inspect.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_audit_artifacts",
            "description": (
                "Inspect bounded, read-only audit artifacts outside table data. "
                "Use for questions about planning context or the APM, RCM rows, "
                "fieldwork, findings, the report, the outcomes of saved analysis "
                "procedures, or a general workspace overview. Prefer area "
                "'analysis' over re-running a test when the question is what the "
                "analyses already found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {
                        "type": "string",
                        "enum": [
                            "overview",
                            "planning",
                            "rcm",
                            "fieldwork",
                            "findings",
                            "report",
                            "analysis",
                        ],
                    }
                },
                "required": ["area"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search locally indexed engagement documents and return only bounded page-linked source excerpts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "document_ids": {"type": "array", "items": {"type": "string"}},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 6},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "List the workspace's tables and joins with their row counts and column schema.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": (
                "Aggregate profile of one table: per-column null %, distinct "
                "count, numeric min/max/mean, and category labels for "
                "low-cardinality columns. Use it to learn a column's values "
                "before filtering."
            ),
            "parameters": {
                "type": "object",
                "properties": {"table": {"type": "string"}},
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_table",
            "description": (
                "Run a structured query: filter, group-by aggregate, sort. "
                "Prefer this for standard slice/aggregate questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "filters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "op": {
                                    "type": "string",
                                    "enum": list(explore.FILTER_OPS.keys()),
                                },
                                "value": {"type": "string"},
                                "value2": {"type": "string"},
                            },
                            "required": ["column", "op"],
                        },
                    },
                    "group_by": {"type": "array", "items": {"type": "string"}},
                    "aggregates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "func": {
                                    "type": "string",
                                    "enum": list(explore.AGG_FUNCS),
                                },
                            },
                            "required": ["func"],
                        },
                    },
                    "sort": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "desc": {"type": "boolean"},
                            },
                            "required": ["column"],
                        },
                    },
                    "limit": {"type": "integer"},
                },
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_analytics",
            "description": (
                "Run a canned audit-analytics test: "
                + ", ".join(analytics.ANALYTICS.keys())
                + ". Returns a verdict, stat chips and a summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "test": {"type": "string", "enum": list(analytics.ANALYTICS.keys())},
                    "params": {"type": "object"},
                },
                "required": ["table", "test"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Run Polars code for anything the other tools can't express. "
                "`pl` is Polars; every table is available by name and via "
                "tables['name']. Assign the output DataFrame to `result`. "
                "You receive an unmasked preview capped to the model row "
                "limit; the auditor sees the larger local artifact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "title": {"type": "string", "description": "Short label for the result."},
                },
                "required": ["code"],
            },
        },
    },
]


@dataclass(frozen=True)
class ReadTool:
    """One registered read-only capability exposed to the Ask coordinator."""

    name: str
    handler: str
    schema: dict


_TOOL_HANDLERS = {
    "get_table_schemas": "get_table_schemas",
    "get_table_profile": "get_table_profile",
    "get_audit_progress": "get_audit_progress",
    "get_latest_run": "get_latest_run",
    "inspect_audit_artifacts": "inspect_audit_artifacts",
    "search_documents": "search_documents",
    "list_tables": "list_tables",
    "describe_table": "describe_table",
    "query_table": "query_table",
    "run_analytics": "run_analytics",
    "run_python": "run_python",
}

READ_TOOLS = tuple(
    ReadTool(
        name=str(schema["function"]["name"]),
        handler=_TOOL_HANDLERS[str(schema["function"]["name"])],
        schema=schema,
    )
    for schema in _TOOL_SCHEMAS
)
READ_TOOL_REGISTRY = {tool.name: tool for tool in READ_TOOLS}
if len(READ_TOOL_REGISTRY) != len(READ_TOOLS):
    raise RuntimeError("Read-only assistant tool names must be unique.")
if set(READ_TOOL_REGISTRY) != set(_TOOL_HANDLERS):
    raise RuntimeError("Every read-only assistant tool needs one registered handler.")

# OpenAI-compatible wire schemas. Kept as a public compatibility alias for
# callers and tests that already import ``TOOLS``.
TOOLS = [tool.schema for tool in READ_TOOLS]


@dataclass(frozen=True)
class Commander:
    """The mutating capability a caller may lend to the coordinator.

    This module never imports the agent runner: the chat layer owns run policy
    (approval mode, the single-live-run rule, queueing) and passes in already
    bound launchers. A caller that passes no commander gets the historical
    strictly read-only loop, schemas included.
    """

    catalog: tuple[dict, ...]
    launch_command: Callable[[str], dict]
    launch_action: Callable[[str], dict]


_COMMAND_TOOL_HANDLERS = {
    "start_command": "start_command",
    "start_action": "start_action",
}


def _command_schemas(commander: Commander | None) -> list[dict]:
    """Wire schemas for the mutating tools, or none when no commander is lent."""

    if commander is None:
        return []
    catalog = "\n".join(
        f"- {item['id']}: {item['label']} — {item['description']}"
        for item in commander.catalog
    )
    return [
        {
            "type": "function",
            "function": {
                "name": "start_command",
                "description": (
                    "Start a registered audit workflow as a durable background "
                    "run. Use only when the auditor is asking for the work to be "
                    "carried out, never to answer a question about it. Returns "
                    "as soon as the run is accepted; the work continues in the "
                    "background.\nAvailable commands:\n" + catalog
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command_id": {
                            "type": "string",
                            "enum": [item["id"] for item in commander.catalog],
                            "description": "Which registered workflow to start.",
                        },
                    },
                    "required": ["command_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "start_action",
                "description": (
                    "Start a durable run for an isolated artifact operation that "
                    "no registered workflow owns — renaming, pinning, deleting, "
                    "attaching, or rerunning one existing artifact. Prefer "
                    "start_command whenever a registered workflow covers the "
                    "request."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request": {
                            "type": "string",
                            "description": (
                                "The operation to perform, in one imperative "
                                "sentence naming the artifact it applies to."
                            ),
                        },
                    },
                    "required": ["request"],
                },
            },
        },
    ]


class _Session:
    """Holds per-request state: the workspace, a frame cache, and artifacts."""

    def __init__(
        self, workspace: Workspace, *, chat_id: str | None = None,
        commander: Commander | None = None,
    ):
        self.workspace = workspace
        self.chat_id = str(chat_id or "").strip() or None
        self.commander = commander
        # The outcome of the one run this message was allowed to start, if it
        # started one. Read by :func:`ask` to report what the turn actually did.
        self.started_run: dict | None = None
        self._frames: dict[str, pl.DataFrame] = {}
        self.artifacts: list[dict] = []
        self.steps: list[dict] = []

    def frame(self, table: str) -> pl.DataFrame:
        if table not in self._frames:
            if table not in self.workspace.table_names():
                raise WorkspaceError(f"Unknown table '{table}'.")
            self._frames[table] = self.workspace.get_frame(table)
        return self._frames[table]

    def all_frames(self) -> dict[str, pl.DataFrame]:
        frames = {}
        for name in self.workspace.table_names():
            try:
                frames[name] = self.frame(name)
            except Exception:
                continue  # skip broken joins; code can still use the rest
        return frames

    # -- tool implementations; each returns (content_for_model, artifact|None)
    def _select_run(self, scope: str = "chat") -> dict | None:
        from .agent import store

        normalized = str(scope or "chat").strip().casefold()
        if normalized not in {"chat", "workspace"}:
            raise WorkspaceError("Run scope must be chat or workspace.")
        summaries = store.list_runs(self.workspace)
        if normalized == "chat" and self.chat_id:
            summaries = [
                item for item in summaries
                if str(item.get("chat_id") or "") == self.chat_id
            ]
        if not summaries:
            return None
        return store.load_run(self.workspace, str(summaries[0]["id"]))

    @staticmethod
    def _run_payload(run: dict | None) -> dict | None:
        if run is None:
            return None
        from .agent import narration, store

        workflow = run.get("workflow") or {}
        stages = []
        for stage in workflow.get("stages") or []:
            units = list(stage.get("units") or [])
            stages.append(
                {
                    "capability": stage.get("capability"),
                    "title": stage.get("title"),
                    "status": stage.get("status"),
                    "unit_counts": {
                        status: sum(
                            1 for unit in units if str(unit.get("status") or "") == status
                        )
                        for status in (
                            "succeeded",
                            "skipped",
                            "blocked",
                            "awaiting_input",
                            "awaiting_confirmation",
                            "failed",
                            "conflict",
                        )
                        if any(
                            str(unit.get("status") or "") == status for unit in units
                        )
                    },
                    "exceptions": [
                        {
                            "title": unit.get("title"),
                            "status": unit.get("status"),
                            "reason": unit.get("error"),
                        }
                        for unit in units
                        if unit.get("status")
                        in {
                            "skipped",
                            "blocked",
                            "awaiting_input",
                            "awaiting_confirmation",
                            "failed",
                            "conflict",
                        }
                    ][:20],
                }
            )
        closing = next(
            (
                str(message.get("content") or "").strip()
                for message in reversed(run.get("messages") or [])
                if message.get("role") == "agent"
                and str(message.get("content") or "").strip()
            ),
            "",
        )
        summary = store.run_summary(run)
        return {
            "id": run.get("id"),
            "status": run.get("status"),
            "engine": run.get("engine"),
            "command": str((run.get("command") or {}).get("text") or ""),
            "created": run.get("created"),
            "finished": run.get("finished"),
            "task_counts": summary.get("task_counts"),
            "requested_outcomes": list(workflow.get("requested_outcomes") or []),
            "next_outcomes": list(workflow.get("next_outcomes") or []),
            "workflow_explanation": workflow.get("workflow_explanation"),
            "stages": stages,
            "artifacts": [
                {
                    key: artifact.get(key)
                    for key in ("kind", "id", "semantic_id", "action")
                    if artifact.get(key) is not None
                }
                for artifact in (run.get("artifacts") or [])[:40]
            ],
            "open_items": narration.blockers(run),
            "closing_message": closing[:4_000],
            "error": run.get("error"),
            "warnings": [str(value)[:1_000] for value in (run.get("warnings") or [])[-10:]],
        }

    def get_audit_progress(self, _args: dict):
        from .agent import capabilities as audit_capabilities
        from .agent import narration

        state = audit_capabilities.workflow_state(self.workspace)
        lifecycle = [
            {
                "capability": capability_id,
                "label": narration.humanize(capability_id),
                **dict(readiness),
            }
            for capability_id, readiness in state.items()
        ]
        return {
            "workspace": workspace_manifest(self.workspace),
            "lifecycle": lifecycle,
            "recommended_next_tasks": narration.next_steps(
                self.workspace, state, limit=5
            ),
            "latest_run": self._run_payload(self._select_run("chat")),
        }, None

    def get_latest_run(self, args: dict):
        scope = str(args.get("scope") or ("chat" if self.chat_id else "workspace"))
        run = self._select_run(scope)
        return {
            "scope": scope,
            "run": self._run_payload(run),
            "message": None if run else f"No {scope}-scoped agent run was found.",
        }, None

    @staticmethod
    def _text(value: object, limit: int = 4_000) -> str:
        text = str(value or "")
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    def inspect_audit_artifacts(self, args: dict):
        area = str(args.get("area") or "").strip().casefold()
        if area == "overview":
            return workspace_manifest(self.workspace), None
        if area == "planning":
            context = self.workspace.planning.get("context") or {}
            return {
                "context": {
                    key: self._text(value, 2_000)
                    for key, value in context.items()
                    if key != "interview_answers"
                },
                "interview_answers": {
                    str(key): self._text(value, 1_000)
                    for key, value in (context.get("interview_answers") or {}).items()
                },
                "apm": {
                    "available": bool(
                        str(self.workspace.planning.get("apm_markdown") or "").strip()
                    ),
                    "updated": self.workspace.planning.get("updated"),
                    "created_by": self.workspace.planning.get("created_by"),
                    "excerpt": self._text(
                        self.workspace.planning.get("apm_markdown"), 8_000
                    ),
                },
            }, None
        if area == "rcm":
            return {
                "total": len(self.workspace.rcm),
                "rows": [
                    {
                        key: row.get(key)
                        for key in (
                            "id",
                            "process",
                            "risk",
                            "risk_rating",
                            "control",
                            "control_owner",
                            "control_frequency",
                            "status",
                        )
                        if row.get(key) not in (None, "")
                    }
                    | {"test_count": len(row.get("test_refs") or [])}
                    for row in self.workspace.rcm[:50]
                ],
                "truncated": len(self.workspace.rcm) > 50,
            }, None
        if area == "fieldwork":
            return {
                "total": len(self.workspace.work_program),
                "items": [
                    {
                        key: self._text(item.get(key), 1_500)
                        for key in (
                            "id",
                            "title",
                            "objective",
                            "procedure",
                            "status",
                            "conclusion",
                            "review_status",
                        )
                        if item.get(key) not in (None, "")
                    }
                    for item in self.workspace.work_program[:50]
                ],
                "truncated": len(self.workspace.work_program) > 50,
            }, None
        if area == "findings":
            return {
                "total": len(self.workspace.findings),
                "findings": [
                    {
                        # The narrative now carries what five fields used to,
                        # so it gets the budget those five shared.
                        key: self._text(
                            finding.get(key), 6_000 if key == "narrative" else 1_500
                        )
                        for key in (
                            "id",
                            "title",
                            "status",
                            "severity",
                            "narrative",
                        )
                        if finding.get(key) not in (None, "")
                    }
                    for finding in self.workspace.findings[:50]
                ],
                "truncated": len(self.workspace.findings) > 50,
            }, None
        if area == "report":
            report = self.workspace.report or {}
            return {
                "available": bool(str(report.get("markdown") or "").strip()),
                "updated": report.get("updated"),
                "edited": report.get("edited"),
                "excerpt": self._text(report.get("markdown"), 10_000),
                "quality": report.get("quality"),
            }, None
        if area == "analysis":
            # What the saved procedures concluded, read from their durable
            # results. No procedure is re-executed and no result row is
            # returned: this is the same bounded record the Analysis tab shows.
            summary = analysis_results.analyses_summary_payload(self.workspace)
            return {
                "counts": summary["counts"],
                "total": len(summary["items"]),
                "analyses": [
                    {
                        key: item[key]
                        for key in (
                            "analysis_id", "title", "table", "kind", "source",
                            "classification", "state", "executed_at", "status",
                            "verdict", "verdict_text", "error", "row_count", "stats",
                        )
                        if item.get(key) not in (None, "", [])
                    }
                    for item in summary["items"][:50]
                ],
                "truncated": len(summary["items"]) > 50,
                "note": (
                    "'stale' means the definition or its input data changed after "
                    "the recorded result; 'not_run' means the procedure has never "
                    "been executed. Neither is a finding."
                ),
            }, None
        raise WorkspaceError(
            "Audit artifact area must be overview, planning, rcm, fieldwork, "
            "findings, report, or analysis."
        )

    def list_tables(self, _args: dict):
        return {"tables": schema_brief(self.workspace)}, None

    def get_table_schemas(self, args: dict):
        return {
            "tables": tooling.table_schemas(
                self.workspace, args.get("tables"),
            )
        }, None

    def search_documents(self, args: dict):
        query = str(args.get("query") or "")
        result = document_search.search(
            self.workspace, query,
            document_ids=args.get("document_ids"), top_k=min(6, max(1, int(args.get("top_k") or 6))),
        )
        documents.append_activity(
            self.workspace, run_id=None, stage="assistant_chat", task=None,
            purpose="assistant_document_search", provider=None, model=None, vision_used=False,
            prompt_version=None, template_versions=[], knowledge_packs=[],
            document_ids=sorted({item["document_id"] for item in result["results"]}),
            page_ranges=sorted({item["page"] for item in result["results"]}),
            source_hashes=sorted({item["citation"]["source_sha1"] for item in result["results"]}),
            response_at=documents.utcnow(), response_hash=None, artifact_ref=None,
            disposition="retrieved", representation="excerpt",
            search_query_hash=hashlib.sha1(query.encode()).hexdigest(),
            characters_supplied=result["characters"], cache_hit=True,
            retrieval_duration_ms=result["duration_ms"], model_duration_ms=None,
            context_outcome="trimmed" if result["trimmed"] else ("supplied" if result["results"] else "unavailable"),
        )
        return {"results": [
            {key: item[key] for key in ("document_id", "title", "page", "excerpt", "citation_id")}
            for item in result["results"]
        ], "trimmed": result["trimmed"]}, None

    def describe_table(self, args: dict):
        table = args.get("table")
        return table_metadata(self.workspace, table), None

    def get_table_profile(self, args: dict):
        return tooling.table_profile(self.workspace, str(args.get("table") or "")), None

    def query_table(self, args: dict):
        table = args.get("table")
        spec = {
            "filters": args.get("filters") or [],
            "group_by": args.get("group_by") or [],
            "aggs": args.get("aggregates") or [],
            "sort": args.get("sort") or [],
            "page": 1,
            "page_size": min(int(args.get("limit") or ARTIFACT_ROWS), ARTIFACT_ROWS),
        }
        spec = explore.canonicalize_query_spec(self.frame(table), spec)
        result, _ = explore.run_query_full(self.frame(table), spec)
        aggregated = bool(spec["group_by"] or spec["aggs"])
        artifact = self._artifact(
            tool="query_table",
            title=args.get("title") or f"Query on {table}",
            table=table,
            kind="query",
            spec=spec,
            viz=_default_viz(result, aggregated),
            frame=result,
        )
        content = {"result": _frame_for_model(result)}
        return content, artifact

    def run_analytics(self, args: dict):
        table = args.get("table")
        test = args.get("test")
        params = args.get("params") or {}
        source = self.workspace.frame_source()
        params = analytics.canonicalize_params(
            self.frame(table), test, params, source=source
        )
        result = analytics.run_test(self.frame(table), test, params, source=source)
        summary = result.summary
        artifact = self._artifact(
            tool="run_analytics",
            title=result.title,
            table=table,
            kind="analytics",
            spec={"test": test, "params": params},
            viz=result.viz or {"type": "table"},
            frame=summary,
            extra={
                "verdict": result.verdict,
                "verdict_text": result.verdict_text,
                "stats": result.stats,
            },
        )
        content = {
            "verdict": result.verdict,
            "verdict_text": result.verdict_text,
            "stats": result.stats,
            "summary": _frame_for_model(summary) if summary is not None else None,
        }
        return content, artifact

    def run_python(self, args: dict):
        code = str(args.get("code") or "")
        result, stdout = sandbox.run(code, self.all_frames())
        artifact = self._artifact(
            tool="run_python",
            title=args.get("title") or "Python result",
            table=None,
            kind="python",
            spec={"code": code},
            viz={"type": "table"},
            frame=result,
            extra={"code": code, "stdout": stdout or None},
        )
        output = stdout or ""
        content = {
            "result": _frame_for_model(result),
            "stdout": output[:4_000],
            "stdout_truncated": len(output) > 4_000,
        }
        return content, artifact

    def _artifact(self, *, tool, title, table, kind, spec, viz, frame, extra=None):
        artifact = {
            "id": uuid.uuid4().hex[:8],
            "tool": tool,
            "title": title,
            "table": table,
            "kind": kind,
            "spec": spec,
            "viz": viz,
            "frame": _artifact_frame(frame) if frame is not None else None,
            "total_rows": frame.height if frame is not None else 0,
            "error": None,
        }
        if extra:
            artifact.update(extra)
        self.artifacts.append(artifact)
        return artifact

    def _guard_single_run(self) -> None:
        """One message may start at most one run.

        A model that has already handed work to the runner has nothing left to
        decide this turn, and a second run would either be refused by the
        single-live-run rule or silently queue behind the first.
        """

        if self.commander is None:
            raise WorkspaceError("This assistant turn cannot change the workspace.")
        if self.started_run is not None:
            raise WorkspaceError(
                "A run was already started for this message. Report it and stop."
            )

    def start_command(self, args: dict):
        self._guard_single_run()
        command_id = str(args.get("command_id") or "").strip()
        known = {str(item["id"]) for item in self.commander.catalog}
        if command_id not in known:
            raise WorkspaceError(
                f"Unknown command '{command_id}'. Available: {', '.join(sorted(known))}."
            )
        self.started_run = self.commander.launch_command(command_id)
        return dict(self.started_run), None

    def start_action(self, args: dict):
        self._guard_single_run()
        request = str(args.get("request") or "").strip()
        if not request:
            raise WorkspaceError("An action needs a request describing what to do.")
        self.started_run = self.commander.launch_action(request)
        return dict(self.started_run), None

    def dispatch(self, name: str, args: dict):
        definition = READ_TOOL_REGISTRY.get(name)
        if definition is not None:
            return getattr(self, definition.handler)(args)
        if name in _COMMAND_TOOL_HANDLERS:
            return getattr(self, _COMMAND_TOOL_HANDLERS[name])(args)
        return {"error": f"Unknown tool '{name}'."}, None


def _default_viz(df: pl.DataFrame, aggregated: bool) -> dict:
    """Suggest a chart for a query result: a bar of the first category vs the
    first numeric measure when it looks groupable, else a table."""
    if not aggregated:
        return {"type": "table"}
    label = next((n for n, t in df.schema.items() if not t.is_numeric()), None)
    measures = [n for n, t in df.schema.items() if t.is_numeric()]
    if label and measures and df.height <= 50:
        return {"type": "bar", "x": label, "y": measures[:2]}
    return {"type": "table"}


# ================================================================= the agent
SYSTEM_PROMPT = """You are the read-only audit assistant embedded in a local \
audit workbench. Answer questions about the engagement, audit workflow and prior \
runs, planning and fieldwork artifacts, findings and reports, documents, or \
datasets by selecting the relevant registered tools.

Rules:
- Use tools for claims about current workspace state; never guess from the \
wording of the question.
- Use get_audit_progress for audit status, remaining work, blockers, and next \
tasks. Use get_latest_run for what a prior run did or why it stopped. Use \
inspect_audit_artifacts for planning, RCM, fieldwork, findings, and report \
questions.
- Do not assume verbs such as show, summarize, compare, inspect, count, or \
calculate imply table analysis. Select tools from the subject of the question \
and its conversation context.
- For dataset questions, discover schema with list_tables / describe_table \
before querying unfamiliar columns.
- Prefer query_table for filters and group-by aggregations, run_analytics for \
the canned audit tests, and run_python only when the structured tools can't \
express the task. Keep run_python to Polars, assign the answer to `result`.
- Use search_documents for a concrete source question. It runs locally and \
returns only bounded cited excerpts; never imply that an oversized unscoped \
attachment was fully considered.
- Structured tools return bounded previews of real rows and computed results. \
Use filters and aggregates for large populations rather than asking for an \
entire table at once. Attached document text is also available as context.
- When done, give a short, plain-English answer grounded in the tool results. \
Mention concrete figures when the tools provide them. Note truncation, blockers, \
or data-quality caveats briefly. Never claim to have changed the workspace \
unless a tool you called reports that it did.

Workspace manifest:
%s
"""


# Appended only when the caller lends a commander. Without it the loop has no
# mutating tool at all, so these rules would describe capabilities that are
# absent.
COMMAND_RULES = """
You can also carry work out, not only describe it. Two tools start durable
background runs: start_command for a registered audit workflow, and
start_action for an isolated operation on one existing artifact.

Rules for starting work:
- Start a run only when the auditor is asking for the work to be carried out. \
A question about what something is, what it would involve, what state it is \
in, or whether it is worth doing is answered with the read tools — never by \
starting a run.
- Starting a run changes the workspace and is not silently undoable. When a \
request could be either a question or an instruction, answer it and ask which \
they meant rather than guessing.
- Prefer start_command whenever a registered command covers the request. Use \
start_action only for an operation no registered workflow owns.
- Start at most one run per message. Once a run has started, stop calling \
tools and reply with one short sentence naming what is now running.
- The run continues in the background after you reply. Do not poll it, and \
never describe its results as though they already exist.
"""


DOCUMENT_CONTEXT_RULES = """
The auditor attached the documents below. Treat their text as evidence and use
it alongside the local data tools when relevant.
When you finish, respond with one JSON object only:
{"answer": "plain-language answer", "citations": [
  {"document_id": "attached id", "page": 1, "excerpt": "exact short excerpt"}
]}
Include citations only for claims grounded in attached documents. Excerpts
must be exact text from the included page. Do not cite omitted content.

ATTACHED DOCUMENTS:
%s
"""


def _document_prompt(context: dict) -> str:
    payload = []
    for doc in context.get("documents") or []:
        payload.append({
            "document_id": doc["document_id"], "title": doc["title"], "source": doc["source"],
            "pages": [{"page": page["page"], "text": page.get("text") or ""} for page in doc["pages"]],
        })
    unavailable = [
        {"document_id": item["document_id"], "title": item["title"],
         "context_outcome": item.get("context_outcome"),
         "instruction": "Use search_documents with a concrete query or ask the auditor for pages."}
        for item in context.get("manifest") or [] if not item.get("included_pages")
    ]
    return json.dumps({"supplied": payload, "scope_required": unavailable}, ensure_ascii=False, default=str)


def _parse_document_answer(value: str) -> tuple[str, list[dict]]:
    raw = str(value or "").strip()
    candidate = raw
    if candidate.startswith("```"):
        candidate = candidate.removeprefix("```json").removeprefix("```")
        candidate = candidate.removesuffix("```").strip()
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return raw, []
    if not isinstance(parsed, dict):
        return raw, []
    citations = parsed.get("citations") if isinstance(parsed.get("citations"), list) else []
    return str(parsed.get("answer") or raw), [item for item in citations if isinstance(item, dict)]


def ask(
    workspace: Workspace,
    question: str,
    document_ids: list[str] | None = None,
    *,
    prior_turns: list[dict] | None = None,
    chat_id: str | None = None,
    commander: Commander | None = None,
) -> dict:
    """Run the tool-calling loop for one message.

    Returns answer + trace + artifacts, plus ``started_run`` describing the run
    the model chose to start, if any. Without a ``commander`` the loop is
    strictly read-only and ``started_run`` is always ``None``. Raises
    :class:`llm.LLMError` if the backend isn't configured."""
    question = str(question or "").strip()
    if not question:
        raise WorkspaceError("Ask a question first.")

    if document_ids is not None and not isinstance(document_ids, list):
        raise WorkspaceError("document_ids must be an array.")
    attached_ids = [str(value) for value in (document_ids or [])]
    document_context = (
        document_context_module.assistant_attachments(workspace, attached_ids)
        if attached_ids else None
    )

    session = _Session(workspace, chat_id=chat_id, commander=commander)
    command_schemas = _command_schemas(commander)
    tools = TOOLS + command_schemas
    manifest_text = json.dumps(workspace_manifest(workspace), indent=1)
    system_prompt = SYSTEM_PROMPT % manifest_text
    if command_schemas:
        system_prompt += COMMAND_RULES
    if document_context:
        system_prompt += DOCUMENT_CONTEXT_RULES % _document_prompt(document_context)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(_bounded_history(prior_turns or []))
    # The current question is appended after applying history budgets and is
    # therefore never truncated by the conversation-context policy.
    messages.append({"role": "user", "content": question})

    answer = ""
    for step in range(MAX_STEPS):
        with debug_store.trace_context(
            workspace_id=workspace.id, workspace_root=str(workspace.root), chat_id=chat_id, stage="assistant.tool_loop",
            purpose="assistant_answer", document_ids=attached_ids,
            tool_step=step + 1,
        ):
            message = llm.chat(messages, tools=tools)
        raw_tool_calls = message.get("tool_calls") or []
        tool_calls = (
            [call for call in raw_tool_calls if isinstance(call, dict)]
            if isinstance(raw_tool_calls, list)
            else []
        )
        # Persist the assistant turn (tool_calls must round-trip verbatim).
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                **({"tool_calls": tool_calls} if tool_calls else {}),
            }
        )
        if not tool_calls:
            answer = message.get("content") or ""
            break

        for call in tool_calls:
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = function.get("name", "")
            raw_args = function.get("arguments") or "{}"
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(parsed_args, dict):
                    raise ValueError("Tool arguments must be an object.")
                args = dict(parsed_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
            try:
                content, _artifact = session.dispatch(name, args)
                ok = True
            except Exception as error:  # tool failures are fed back to the model
                content, ok = {"error": str(error)}, False
            session.steps.append({"tool": name, "args": args, "ok": ok})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(content, default=str),
                }
            )
    else:
        answer = answer or (
            "Stopped after reaching the tool-call limit. Here is what was "
            "gathered so far."
        )

    raw_answer = answer
    raw_citations: list[dict] = []
    if document_context:
        answer, raw_citations = _parse_document_answer(answer)
    citations = (
        documents.assistant_document_citations(
            workspace, raw_citations, document_context,
        )
        if document_context else []
    )
    if document_context:
        profile = llm.status()
        documents.append_activity(
            workspace, run_id=None, stage="assistant_chat", task=None,
            purpose="assistant_chat", provider=profile.get("provider") or profile.get("backend"),
            model=profile.get("model"), vision_used=False,
            prompt_version=hashlib.sha1(system_prompt.encode()).hexdigest(),
            template_versions=[], knowledge_packs=[], document_ids=attached_ids,
            page_ranges=sorted({
                page for item in document_context["manifest"] for page in item["included_pages"]
            }),
            source_hashes=[item.get("source_sha1") for item in document_context["manifest"] if item.get("source_sha1")],
            response_at=documents.utcnow(),
            response_hash=hashlib.sha1(raw_answer.encode()).hexdigest() if raw_answer else None,
            artifact_ref="assistant_chat", disposition="generated",
            representation="raw_pages",
            characters_supplied=sum(item["characters_included"] for item in document_context["manifest"]),
            cache_hit=True, retrieval_duration_ms=None, model_duration_ms=None,
            context_outcome="scope_required" if document_context.get("scope_required") else ("trimmed" if document_context["trimmed"] else "supplied"),
        )
    return {
        "answer": answer,
        "steps": session.steps,
        "artifacts": session.artifacts,
        "citations": citations,
        "started_run": session.started_run,
        "document_context": ({
            "manifest": document_context["manifest"],
            "trimmed": document_context["trimmed"],
            "character_budget": document_context["character_budget"],
        } if document_context else None),
    }


def _bounded_history(prior_turns: list[dict]) -> list[dict]:
    """Return the newest contiguous, text-only history within all budgets.

    Eligibility is decided by the durable chat service. This final choke point accepts only completed text
    roles and never tool messages, frames, traces, citations, or artifacts.
    """
    if not isinstance(prior_turns, list):
        raise WorkspaceError("prior_turns must be an array.")
    eligible: list[dict] = []
    for item in prior_turns:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "")[:HISTORY_MAX_MESSAGE_CHARACTERS]
        if not content:
            continue
        eligible.append({"role": item["role"], "content": content})

    selected: list[dict] = []
    characters = 0
    for item in reversed(eligible[-HISTORY_MAX_MESSAGES:]):
        length = len(item["content"])
        if characters + length > HISTORY_MAX_CHARACTERS:
            break
        selected.append(item)
        characters += length
    return list(reversed(selected))


def run_python_snippet(workspace: Workspace, code: str) -> dict:
    """Execute an (edited) snippet directly — powers the 'Re-run' button and
    the live recomputation of pinned Python tiles."""
    frames = {}
    for name in workspace.table_names():
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            continue
    result, stdout = sandbox.run(code, frames)
    return {
        "frame": _artifact_frame(result),
        "total_rows": result.height,
        "stdout": stdout or None,
    }
