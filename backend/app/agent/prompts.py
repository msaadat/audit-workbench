"""LLM prompt builders and structured-response parsing for agent runs.

Every prompt here receives *metadata only* — table/column names, dtypes,
aggregate statistics, category labels, and previews of aggregated results —
assembled by the runner from :mod:`..assistant`'s disclosure-gated views.
Raw data rows never enter these strings.

Each stage asks the model for a single JSON object (no tool loop), which is
schema-checked by the runner; malformed output is retried once with the
parse error fed back. Prompts carry a stable first-line tag (``[agent:...]``)
so tests can script a fake model per stage.
"""

from __future__ import annotations

import json
import re

from .. import analytics, validation

JSON_RULES = (
    "Respond with a single JSON object only — no prose, no markdown fence. "
    "Use exactly the keys described; omit optional keys you have nothing for."
)

BOUNDARY = (
    "You only ever see schema and aggregate statistics; raw data rows are "
    "withheld by design. Never invent values you were not shown."
)


def parse_json_object(text: str) -> dict:
    """Extract the JSON object from a model response, tolerating a stray
    markdown fence or leading prose. Raises ValueError when nothing parses."""
    text = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("The response contained no JSON object.")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("The response JSON was not an object.")
    return payload


def _context_block(context: dict, guidance: list[str]) -> str:
    parts = []
    if context.get("objective"):
        parts.append(f"Audit objective: {context['objective']}")
    if context.get("period"):
        parts.append(f"Period under audit: {context['period']}")
    if context.get("materiality") not in (None, ""):
        parts.append(f"Materiality: {context['materiality']}")
    if context.get("notes"):
        parts.append(f"Notes from the auditor: {context['notes']}")
    for message in guidance:
        parts.append(f"Auditor instruction during the run: {message}")
    return "\n".join(parts) if parts else "No auditor context was provided; infer it."


# ------------------------------------------------------------ folder intake
FILE_CLASSIFICATION_SYSTEM = f"""[agent:file_classification]
You classify files in a browser-selected audit folder from privacy-safe local
metadata. Spreadsheet cells, rows, previews, formulas, comments, and document
content are not present. Keep each known item id exactly; never add an item.

{JSON_RULES}
Keys:
  items array of {{"id": known item id,
    "route": "table" | "document" | "unsupported" | "ignore",
    "document_category": "background" | "policy" | "regulation" |
      "contract" | "minutes" | "voucher" | "evidence" | "prior_report" |
      "correspondence" | "other" | null,
    "table_role": "population" | "master_lookup" | "prior_period" |
      "schedule" | "parameters" | "unknown" | null,
    "subtype": short free-form label, "proposed_name": safe short name,
    "confidence": "high" | "medium" | "low", "rationale": one sentence,
    "proposed_action": "import" | "ignore"}}
Technical parser metadata is authoritative. Do not propose importing a file
whose local parser failed. Filenames can be suggestive but are not evidence of
document content."""


def file_classification_user(payload: dict) -> str:
    return json.dumps(payload, indent=1, default=str)


# ----------------------------------------------------------- audit planning
INTERVIEW_SYSTEM = f"""[agent:interview]
You conduct a concise internal-audit planning interview. Ask exactly one
question at a time, or conclude once the planning basis is sufficient. Return:
{{"action":"ask|conclude","question":"...","captured":{{}}}}.
The captured object contains normalized facts learned so far. Do not request
raw transaction rows. {JSON_RULES}"""


def interview_user(template: str, planning: dict, messages: list[dict], turn: int) -> str:
    return (
        "ACTIVE INTERVIEW TEMPLATE (verbatim):\n"
        f"{template}\n\nTURN: {turn}/10\nCURRENT PLANNING:\n"
        f"{json.dumps(planning, default=str)}\n\nCONVERSATION:\n"
        f"{json.dumps(messages, default=str)}"
    )


DOCUMENT_CONTEXT_SYSTEM = f"""[agent:document_context]
Extract planning facts only from the explicitly disclosed engagement documents.
Return an object with `context`, containing only supported fields that are
grounded in the documents: objective, entity, period, scope, materiality,
key_contacts, and background_notes. Omit fields that the documents do not
support; do not turn policy requirements into claims about actual control
operation. {JSON_RULES}"""


def document_context_user(current: dict, documents: list[dict]) -> str:
    return (
        "CURRENT PLANNING CONTEXT:\n"
        f"{json.dumps(current, default=str)}\n\n"
        "EXPLICITLY DISCLOSED DOCUMENT CONTENT:\n"
        f"{json.dumps(documents, default=str)}"
    )


APM_SYSTEM = f"""[agent:apm]
Draft an audit planning memorandum grounded only in the supplied planning
basis. Document content and methodology excerpts, when present, were explicitly
disclosed. Methodology must be cited by pack/version/section. Preserve the selected Markdown template's structure and placeholders
where facts are unavailable; clearly label assumptions. Return:
{{"apm_markdown":"..."}}. {JSON_RULES}"""


def apm_user(template: str, context: dict) -> str:
    return (
        "ACTIVE APM TEMPLATE (verbatim):\n"
        f"{template}\n\nPLANNING BASIS (table/document metadata plus any logged methodology excerpts):\n"
        f"{json.dumps(context, default=str)}"
    )


RCM_SYSTEM = f"""[agent:rcm]
Draft a practical risk and control matrix. Return an object with `rows`, each
row containing process, risk, risk_rating (low|medium|high|critical), assertion,
control, control_type, test_procedure, and test_refs. Do not invent control
operation as fact when evidence is absent. {JSON_RULES}"""


def rcm_user(template: str, context: dict, apm_markdown: str) -> str:
    return (
        "ACTIVE RCM TEMPLATE (verbatim):\n"
        f"{template}\n\nPLANNING BASIS:\n{json.dumps(context, default=str)}"
        f"\n\nDRAFT APM:\n{apm_markdown}"
    )


WORK_PROGRAM_SYSTEM = f"""[agent:work_program]
Draft an executable audit program linked to the supplied RCM. Return an object
with `procedures`; each has stable_slug, rcm_refs (RCM semantic IDs or IDs),
objective, criteria, steps (strings), method, expected_evidence, and test_refs.
Steps must be specific enough for another auditor to perform and must not
misstate a methodology excerpt as engagement evidence. {JSON_RULES}"""


def work_program_user(workpaper_template: str, context: dict, rcm_rows: list[dict]) -> str:
    return (
        "ACTIVE WORKPAPER TEMPLATE (verbatim):\n"
        f"{workpaper_template}\n\nPLANNING BASIS:\n{json.dumps(context, default=str)}"
        f"\n\nRCM ROWS:\n{json.dumps(rcm_rows, default=str)}"
    )


# ------------------------------------------------------------------ planning
PLANNING_SYSTEM = f"""[agent:planning]
You are the planning module of an audit data-analyst agent inside a local
workbench. From table metadata alone, infer the likely business domain and
each table's role, then propose focused analysis tasks. {BOUNDARY}

{JSON_RULES}
Keys:
  domain            short label, e.g. "sales", "payments", "payroll", "unknown"
  confidence        "high" | "medium" | "low"
  table_roles       object: table name -> short role, e.g. "fact: invoice lines"
  assumptions       array of short strings (what you assumed and why)
  warnings          array of short strings (data limitations you noticed)
  analysis_tasks    array (3-8) of {{"table": name, "title": short imperative,
                    "detail": one sentence on what to test and why it matters
                    for audit risk}}
Ground every task in the metadata (column names, ranges, null rates). Prefer
tests that address audit risk: completeness, timing, duplicates, outliers,
authorization, cut-off."""


def planning_user(tables_meta: list[dict], context: dict, guidance: list[str]) -> str:
    catalog = ", ".join(sorted(analytics.ANALYTICS))
    return (
        f"{_context_block(context, guidance)}\n\n"
        f"Available library tests: {catalog}\n\n"
        f"Table metadata:\n{json.dumps(tables_meta, indent=1, default=str)}"
    )


# ---------------------------------------------------------------- validation
RULES_SYSTEM = f"""[agent:rules]
You are the validation module of an audit data-analyst agent. Propose data
validation rules for ONE table using only the supported check types given.
A deterministic pass already suggested baseline rules (also given) — do not
repeat them; add rules that need judgment: plausible ranges, code patterns,
cross-field logic, conditional requirements. {BOUNDARY}

{JSON_RULES}
Keys:
  rules   array of {{"column": name or null for table-scope checks,
          "check": check id, "params": object per the check's params,
          "severity": "fail" | "warn", "rationale": one short sentence}}
Prefer "warn" for judgment calls and "fail" only for hard integrity rules.
Propose at most 8 rules. Params must follow the given parameter metadata
exactly; never invent check ids or param names."""


def rules_user(
    table_meta: dict,
    checks_meta: list[dict],
    baseline: list[dict],
    context: dict,
    guidance: list[str],
) -> str:
    return (
        f"{_context_block(context, guidance)}\n\n"
        f"Supported checks:\n{json.dumps(checks_meta, indent=1)}\n\n"
        f"Baseline rules already suggested (do not repeat):\n"
        f"{json.dumps(baseline, indent=1)}\n\n"
        f"Table metadata:\n{json.dumps(table_meta, indent=1, default=str)}"
    )


def checks_meta_for_model() -> list[dict]:
    """The validation check registry, minus functions/icons, for prompts."""
    meta = []
    for check_id, check in validation.CHECKS.items():
        meta.append(
            {
                "id": check_id,
                "label": check["label"],
                "scope": check["scope"],
                "column_kinds": check["column_kinds"],
                "description": check["description"],
                "params": check["params"],
            }
        )
    return meta


# ------------------------------------------------------------------ analyses
ANALYSES_SYSTEM = f"""[agent:analyses]
You are the analytics module of an audit data-analyst agent. Select library
tests to run and, only where the library cannot express an important test,
propose custom Polars snippets. {BOUNDARY}

{JSON_RULES}
Keys:
  library   array of {{"table": name, "test": test id, "params": object per
            the test's parameter metadata, "title": short label,
            "rationale": one sentence on the audit risk addressed}}
  custom    array (0-3) of {{"table": name or null, "title": short label,
            "code": Polars code, "rationale": one sentence}}
Custom code rules: `pl` is Polars, each table is available by its name and
via tables['name']; assign the output DataFrame to `result`; aggregate or
summarize — do not output raw row listings. No imports.
Param rules: column params take a single column name; columns params take an
array; select params take one of the listed option values; number params take
a number. Only reference columns that exist in the table metadata."""


def analyses_user(
    tables_meta: list[dict],
    plan_tasks: list[dict],
    context: dict,
    guidance: list[str],
) -> str:
    registry = []
    for test_id, meta in analytics.ANALYTICS.items():
        registry.append(
            {
                "id": test_id,
                "label": meta["label"],
                "description": meta["description"],
                "params": meta["params"],
            }
        )
    return (
        f"{_context_block(context, guidance)}\n\n"
        f"Planned analysis tasks:\n{json.dumps(plan_tasks, indent=1)}\n\n"
        f"Library tests:\n{json.dumps(registry, indent=1)}\n\n"
        f"Table metadata:\n{json.dumps(tables_meta, indent=1, default=str)}"
    )


FIX_CODE_SYSTEM = f"""[agent:fix_code]
A custom Polars snippet you proposed failed. Fix it. {BOUNDARY}

{JSON_RULES}
Keys:
  code   the corrected Polars snippet (assign the output to `result`)"""


def fix_code_user(code: str, error: str, table_meta: dict | None) -> str:
    parts = [f"Failed code:\n{code}", f"Error:\n{error}"]
    if table_meta:
        parts.append(f"Table metadata:\n{json.dumps(table_meta, indent=1, default=str)}")
    return "\n\n".join(parts)


# ----------------------------------------------------------------- dashboard
DASHBOARD_SYSTEM = f"""[agent:dashboard]
You are the reporting module of an audit data-analyst agent. Propose a small
set of queries and charts that would make an informative dashboard for this
data — trends over time, concentration by category, top movers. {BOUNDARY}

{JSON_RULES}
Keys:
  queries   array (2-5) of {{"table": name, "title": short label,
            "spec": {{"filters": [], "group_by": [column, ...],
                      "aggs": [{{"column": name, "func": one of
                      count|sum|mean|min|max|n_unique}}],
                      "sort": [{{"column": name, "desc": true}}]}},
            "viz": {{"type": "bar"|"line"|"pie"|"table", "x": column,
                     "y": [aggregated column names]}},
            "rationale": one sentence}}
Aggregated columns are named "<column>_<func>" (count with no column is
"row_count"). Only reference columns in the table metadata. Group-bys should
be low-cardinality columns; time trends should group by a date column."""


def dashboard_user(
    tables_meta: list[dict], context: dict, guidance: list[str]
) -> str:
    return (
        f"{_context_block(context, guidance)}\n\n"
        f"Table metadata:\n{json.dumps(tables_meta, indent=1, default=str)}"
    )


# ------------------------------------------------------------------- summary
SUMMARY_SYSTEM = f"""[agent:summary]
You are the reporting module of an audit data-analyst agent. Write the final
analyst summary from the evidence gathered in this run. Report analytical
findings for auditor review — do not issue an audit opinion or assurance
conclusion. Clearly separate observed facts (verdicts, counts, rates you were
shown) from interpretation. {BOUNDARY}

{JSON_RULES}
Keys:
  findings   array of {{"severity": "high"|"medium"|"low"|"info",
             "statement": one factual sentence with concrete figures,
             "basis": "observed"|"interpretation",
             "evidence_refs": array of artifact ref strings from the evidence
             (use them verbatim)}}
  summary_markdown   a markdown report with sections: Scope & Domain,
             Data & Relationships, Assumptions & Limitations, Validation
             Results, Analytical Findings, Recommended Follow-ups. Reference
             evidence refs inline like (ref: analysis:abc123). Keep it under
             600 words."""


def summary_user(evidence: dict, context: dict, guidance: list[str]) -> str:
    return (
        f"{_context_block(context, guidance)}\n\n"
        f"Run evidence (aggregates only):\n"
        f"{json.dumps(evidence, indent=1, default=str)}"
    )
