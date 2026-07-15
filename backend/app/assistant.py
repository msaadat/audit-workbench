"""Natural-language analysis assistant.

An auditor asks a question in plain English; an LLM answers it by *calling
tools* that run locally against the workspace's data. The tools are the same
primitives the app already exposes — structured queries, the audit-analytics
library, and an escape hatch that runs visible, editable Polars — so simple
requests ("top 10 vendors by spend", "run Benford on amount") are one tool
call away and anything bespoke drops down to Python the auditor can inspect.

**Metadata-only guarantee.** The data never leaves the machine. The only
workspace-derived text that enters the LLM conversation is:

  * schema — table names, column names, dtypes;
  * aggregate statistics — row counts, null counts, distinct counts, numeric
    min/max/mean, and low-cardinality category labels; and
  * previews of *aggregated* results (a group-by summary, an analytics
    verdict) — never row-level detail from the raw dataset.

:func:`_frame_for_model` is the choke point: raw (non-aggregated) results are
reduced to their shape and numeric summary before going back to the model,
while the full result is returned to the browser (same machine) for display.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import polars as pl

from . import analytics, documents, explore, llm, privacy, profiler, sandbox
from .workspaces import Workspace, WorkspaceError

MAX_STEPS = 8
# Aggregated previews handed to the model are capped hard — a summary, not data.
MODEL_PREVIEW_ROWS = 40
# Full results streamed to the browser (local) are capped only to stay light.
ARTIFACT_ROWS = 200
# A run_python result this small is treated as a derived summary the model may
# see in full; anything larger is summarized (shape + stats) only.
SMALL_RESULT_ROWS = 25
# Low-cardinality string columns get their category labels surfaced as metadata.
CATEGORY_SAMPLE_MAX = 30

DISCLOSURE = (
    "Only schema (table/column names and types) and aggregate statistics "
    "(counts, distinct counts, numeric ranges, category labels, and previews "
    "of aggregated results) are sent to the language model. Raw data rows "
    "never leave this machine."
)


# ============================================================ metadata context
def _column_meta(profile: dict) -> dict:
    """Compact, aggregate-only metadata for one column (from the profiler)."""
    return privacy.project_column_profile(profile)


def table_metadata(workspace: Workspace, table: str) -> dict:
    profile = workspace.get_profile(table)
    return {
        "table": table,
        "rows": profile["rows"],
        "columns": [_column_meta(c) for c in profile["column_profiles"]],
    }


def _schema_brief(workspace: Workspace) -> list[dict]:
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
def _numeric_summary(df: pl.DataFrame) -> dict:
    summary = {}
    for name, dtype in df.schema.items():
        if dtype.is_numeric():
            col = df[name]
            summary[name] = {
                "min": _round(col.min()),
                "max": _round(col.max()),
                "mean": _round(col.mean()),
                "nulls": int(col.null_count()),
            }
    return summary


def _round(value):
    return round(value, 4) if isinstance(value, float) else value


def _frame_for_model(df: pl.DataFrame, allow_rows: bool) -> dict:
    """Reduce a result frame to what the model is allowed to see.

    Always: shape, columns, dtypes, numeric aggregate summary. Row values are
    included only when ``allow_rows`` (i.e. the result is an aggregate/summary,
    not raw rows) and the frame is small.
    """
    return privacy.project_frame(df, allow_rows=allow_rows, row_limit=MODEL_PREVIEW_ROWS)


def _artifact_frame(df: pl.DataFrame) -> dict:
    return explore.frame_payload(df, ARTIFACT_ROWS)


# ====================================================================== tools
TOOLS = [
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
                "You see only the shape and aggregate stats of large/raw "
                "results; the auditor sees the full table."
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
        return {"tables": _schema_brief(self.workspace)}, None

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
        content = {"result": _frame_for_model(result, allow_rows=aggregated)}
        return content, artifact

    def run_analytics(self, args: dict):
        table = args.get("table")
        test = args.get("test")
        params = args.get("params") or {}
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
            # summary frames are aggregates → the model may see them
            "summary": _frame_for_model(summary, allow_rows=True) if summary is not None else None,
        }
        return content, artifact

    def run_python(self, args: dict):
        code = str(args.get("code") or "")
        result, stdout = sandbox.run(code, self.all_frames())
        allow_rows = result.height <= SMALL_RESULT_ROWS
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
        content = {"result": _frame_for_model(result, allow_rows=allow_rows)}
        if stdout:
            content["stdout"] = stdout
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
- For structured tables, you only ever see schema, aggregate statistics and \
previews of aggregated results — raw rows are withheld from you by design. Do \
not ask for raw rows; compute aggregates instead. Explicitly attached document \
text is the only exception and is governed by a separate disclosure gate.
- When done, give a short, plain-English answer grounded in the tool results. \
Mention the concrete figures you were shown. Note any caveats (withheld rows, \
data quality) briefly.

Workspace tables and columns:
%s
"""


DOCUMENT_CONTEXT_RULES = """
The auditor explicitly attached the documents below. Treat their disclosed
text as evidence and use it alongside the local data tools when relevant.
When you finish, respond with one JSON object only:
{"answer": "plain-language answer", "citations": [
  {"document_id": "attached id", "page": 1, "excerpt": "exact short excerpt"}
]}
Include citations only for claims grounded in attached documents. Excerpts
must be exact text from the disclosed page. Do not cite omitted content.

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
    return json.dumps(payload, ensure_ascii=False, default=str)


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
    mask_pii: bool | None = None,
) -> dict:
    """Run the tool-calling loop for one question. Returns answer + trace +
    artifacts. Raises :class:`llm.LLMError` if the backend isn't configured."""
    question = str(question or "").strip()
    if not question:
        raise WorkspaceError("Ask a question first.")

    if document_ids is not None and not isinstance(document_ids, list):
        raise WorkspaceError("document_ids must be an array.")
    attached_ids = [str(value) for value in (document_ids or [])]
    effective_masking = (
        bool(workspace.settings.get("doc_pii_masking"))
        if mask_pii is None else bool(mask_pii)
    )
    document_context = (
        documents.assistant_document_context(
            workspace, attached_ids, mask_pii=effective_masking,
        )
        if attached_ids else None
    )

    session = _Session(workspace)
    schema_text = json.dumps(_schema_brief(workspace), indent=1)
    system_prompt = SYSTEM_PROMPT % schema_text
    if document_context:
        system_prompt += DOCUMENT_CONTEXT_RULES % _document_prompt(document_context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    answer = ""
    for _ in range(MAX_STEPS):
        message = llm.chat(messages, tools=TOOLS)
        tool_calls = message.get("tool_calls") or []
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
            name = call.get("function", {}).get("name", "")
            raw_args = call.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
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
            workspace, raw_citations, document_context, mask_pii=effective_masking,
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
            source_hashes=[event.get("source_sha1") for event in document_context["disclosures"]],
            response_at=documents.utcnow(),
            response_hash=hashlib.sha1(raw_answer.encode()).hexdigest() if raw_answer else None,
            artifact_ref="assistant_chat", disposition="generated",
        )
    return {
        "answer": answer,
        "steps": session.steps,
        "artifacts": session.artifacts,
        "disclosure": (
            DISCLOSURE + " Explicitly attached document text was also disclosed under the engagement's Document AI setting."
            if document_context else DISCLOSURE
        ),
        "citations": citations,
        "document_context": ({
            "manifest": document_context["manifest"],
            "trimmed": document_context["trimmed"],
            "character_budget": document_context["character_budget"],
        } if document_context else None),
    }


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
