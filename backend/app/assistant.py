"""Natural-language analysis assistant.

An auditor asks a question in plain English; an LLM answers it by *calling
tools* that run locally against the workspace's data. The tools are the same
primitives the app already exposes — structured queries, the audit-analytics
library, and an escape hatch that runs visible, editable Polars — so simple
requests ("top 10 vendors by spend", "run Benford on amount") are one tool
call away and anything bespoke drops down to Python the auditor can inspect.

Model tool results include compact, unmasked previews. Full computations stay
local and previews are bounded only to protect the model's context window.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import polars as pl

from . import analytics, debug_store, document_context as document_context_module, document_search, documents, explore, llm, model_context, profiler, sandbox
from .workspaces import Workspace, WorkspaceError

MAX_STEPS = 8
# Model previews are capped hard so large populations fit in context.
MODEL_PREVIEW_ROWS = 40
# Full results streamed to the browser (local) are capped only to stay light.
ARTIFACT_ROWS = 200
# Low-cardinality string columns get their category labels surfaced as metadata.
CATEGORY_SAMPLE_MAX = 30

# Conversation history is text-only and deliberately small.
HISTORY_MAX_MESSAGES = 8
HISTORY_MAX_CHARACTERS = 12_000
HISTORY_MAX_MESSAGE_CHARACTERS = 2_000

# ============================================================ metadata context
def _column_meta(profile: dict) -> dict:
    """Compact profiler metadata for one column."""
    return model_context.project_column_profile(profile, category_limit=CATEGORY_SAMPLE_MAX)


def table_metadata(workspace: Workspace, table: str) -> dict:
    profile = workspace.get_profile(table)
    return {
        "table": table,
        "rows": profile["rows"],
        "columns": [_column_meta(c) for c in profile["column_profiles"]],
    }


def schema_brief(workspace: Workspace) -> list[dict]:
    """A light table/column listing for the opening system prompt."""
    brief = []
    for name in workspace.table_names():
        try:
            frame = workspace.get_frame(name)
        except Exception as error:
            brief.append({"table": name, "error": str(error)})
            continue
        brief.append(
            {
                "table": name,
                "rows": frame.height,
                "columns": [
                    {"name": c["name"], "dtype": c["dtype"], "type": c["kind"]}
                    for c in profiler.schema_payload(frame)
                ],
            }
        )
    return brief


# ================================================================ model views
def _frame_for_model(df: pl.DataFrame) -> dict:
    """Return a bounded, unmasked model preview of a result frame."""
    return model_context.project_frame(df, row_limit=MODEL_PREVIEW_ROWS)


def _artifact_frame(df: pl.DataFrame) -> dict:
    return explore.frame_payload(df, ARTIFACT_ROWS)


# ====================================================================== tools
TOOLS = [
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


class _Session:
    """Holds per-request state: the workspace, a frame cache, and artifacts."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
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
    def list_tables(self, _args: dict):
        return {"tables": schema_brief(self.workspace)}, None

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
        params = analytics.canonicalize_params(self.frame(table), test, params)
        result = analytics.run_test(self.frame(table), test, params)
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

    def dispatch(self, name: str, args: dict):
        handler = {
            "search_documents": self.search_documents,
            "list_tables": self.list_tables,
            "describe_table": self.describe_table,
            "query_table": self.query_table,
            "run_analytics": self.run_analytics,
            "run_python": self.run_python,
        }.get(name)
        if handler is None:
            return {"error": f"Unknown tool '{name}'."}, None
        return handler(args)


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
SYSTEM_PROMPT = """You are an audit data-analysis assistant embedded in a local \
workbench. You help an auditor interrogate their own datasets by calling tools \
that run on their machine.

Rules:
- Use the tools to get answers; never fabricate numbers. Discover schema with \
list_tables / describe_table before querying unfamiliar columns.
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
Mention the concrete figures you were shown. Note truncation or data-quality \
caveats briefly.

Workspace tables and columns:
%s
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
) -> dict:
    """Run the tool-calling loop for one question. Returns answer + trace +
    artifacts. Raises :class:`llm.LLMError` if the backend isn't configured."""
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

    session = _Session(workspace)
    schema_text = json.dumps(schema_brief(workspace), indent=1)
    system_prompt = SYSTEM_PROMPT % schema_text
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
            message = llm.chat(messages, tools=TOOLS)
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
