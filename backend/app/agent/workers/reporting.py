"""Registered model workers for audit reporting capabilities.

The finding worker turns one exception observation and its immutable
execution result into an unconfirmed finding draft. It owns the prompt, the
bundle-to-message transformation, and the response contract; evidence linking,
support validation, and the durable write belong to the registered executor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import templates_store
from ..prompts import LANGUAGE_RULES
from ..runtime.model_gateway import ModelGateway
from .model import (
    WORKERS,
    WorkerAttempt,
    WorkerContractError,
    WorkerDefinition,
    WorkerRepairPolicy,
    WorkerRequest,
    WorkerResponseSchema,
    WorkerResponseValidationError,
)


FINDING_WORKER_ID = "reporting.finding"
FINDING_SYSTEM = f"""[agent:finding]
Draft one unconfirmed audit finding from the supplied exception observation and
immutable execution reference.

Return the finding as Markdown only, without a JSON wrapper or Markdown code
fence, in the shape of the supplied finding template:

- a `#` line carrying the finding's title, naming the audit point rather than
  the test that found it;
- a `**Severity:**` line carrying exactly one of critical, high, medium, low,
  info;
- the template's `##` sections, in that order, with no heading added, renamed,
  or dropped.

Follow the guidance comments in that template: they are instructions to you and
must not be copied into the finding. Every section must carry text. Where the
supplied evidence does not establish why the exception occurred, write
`_Root cause pending auditor follow-up._` as the whole of the root-cause section
rather than leaving it blank or asserting a cause the evidence does not support.

Write ordinary Markdown: each heading on its own line, paragraphs separated by a
blank line, and tables written a row per line. The sections are copied into the
audit report unchanged, so write final report prose: no first person, no test
ids, run ids, or run mechanics, and no commentary about drafting. Use British
spelling throughout — analyse, summarise, recognise, organisation — so the
deliverable matches the rest of the audit file. Any number you state must be a
number the supplied execution result holds.

Be specific. A finding that counts exceptions without identifying them is not
actionable:

- When the supplied item names documents, name them in the condition rather
  than writing "the supplied documentation".
- When EXCEPTION ROWS is supplied, identify the records that failed. Where the
  rows are few, set them out as a Markdown table inside the condition section,
  choosing only the columns that evidence the exception — the identifier and
  the fields the test compared — and giving each a readable heading rather than
  the raw column name. Where they are many, describe the pattern and quantify
  it, and name a small number of examples by identifier.
- EXCEPTION ROWS states rows_supplied, rows_withheld, and truncated. When rows
  were withheld, say the table shows the first rows_supplied of
  exception_count; never present a truncated table as the full population.
- When semantic_valid is false the rows do not establish the exception. Report
  what the result does and does not support, and recommend validating and
  rerunning the check.

Do not create or alter RCM, planned-test, execution, or evidence references. Do
not claim auditor confirmation. {LANGUAGE_RULES}"""

FINDING_OBSERVATION_SOURCE_ID = "observation"
FINDING_EXECUTION_SOURCE_ID = "execution_result"
FINDING_TEMPLATE_SOURCE_ID = "finding_template"
FINDING_EXCEPTION_ROWS_SOURCE_ID = "exception_rows"
_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
# The root-cause section is the one a draft may leave open, and only by saying
# so with the deferral note below, which is what sets ``cause_pending``.
_CAUSE_SECTION_KEYS = frozenset({"cause", "root cause"})

_FENCED_MARKDOWN = re.compile(
    r"```(?:markdown|md)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE
)
#: The finding's title: the one ``#`` line, distinguished from the ``##``
#: section headings that follow it.
_TITLE_LINE = re.compile(r"^#(?!#)\s*(.+?)\s*$", re.MULTILINE)
#: The severity line, however the model emphasises it — ``**Severity:** high``,
#: ``**Severity**: high`` and ``Severity: high`` are the same statement.
_SEVERITY_LINE = re.compile(
    r"^\s*[*_]*\s*severity\s*[*_]*\s*[::]\s*(.*)$", re.IGNORECASE | re.MULTILINE
)
_NARRATIVE_START = re.compile(r"^##\s+", re.MULTILINE)
#: The one accepted stand-in for a cause the evidence does not establish.
#: Matched against the whole section body so a cause that merely mentions
#: follow-up is not read as a deferral.
_CAUSE_DEFERRAL = re.compile(
    r"root cause (?:is )?pending auditor follow-?up\.?", re.IGNORECASE
)


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _resolved_item(request: WorkerRequest, source_id: str) -> object:
    matches = [
        item.content for item in request.context.items if item.source_id == source_id
    ]
    if len(matches) != 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply exactly one item."
        )
    return matches[0]


def _optional_item(request: WorkerRequest, source_id: str) -> object | None:
    """One item from a declared-but-optional source, or None when it is absent.

    A Document Test has no tabular exception population, so the exception-row
    source resolves to nothing for those units. That is a normal shape, not a
    contract violation.
    """
    matches = [
        item.content for item in request.context.items if item.source_id == source_id
    ]
    if len(matches) > 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply at most one item."
        )
    return matches[0] if matches else None


def _plain_note(body: str) -> str:
    """One section body reduced to its words, emphasis and wrapping removed."""
    return " ".join(str(body or "").replace("*", "").replace("_", "").split())


def _cause_is_deferred(narrative: str) -> bool:
    """Whether the root-cause section carries the deferral note and nothing else."""
    bodies = templates_store.section_bodies(narrative)
    return any(
        _CAUSE_DEFERRAL.fullmatch(_plain_note(bodies.get(key) or ""))
        for key in _CAUSE_SECTION_KEYS
    )


def _finding_from_json(value: str) -> Mapping[str, Any] | None:
    """The finding from a response that arrived as JSON despite the instruction.

    Kept as tolerance, not as the contract: a model that falls back on its JSON
    habit is repaired against the same rules rather than discarded, and
    ``strict=False`` accepts the unescaped newline such a response carries
    inside the narrative.
    """
    if not value.startswith("{"):
        return None
    try:
        payload = json.loads(value, strict=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    finding = payload.get("finding")
    if not isinstance(finding, Mapping) or "narrative" not in finding:
        return None
    narrative = templates_store.strip_guidance(
        str(finding.get("narrative") or "")
    ).strip()
    return {
        "title": str(finding.get("title") or "").strip(),
        "severity": str(finding.get("severity") or "").strip().casefold(),
        "narrative": narrative,
        "cause_pending": bool(finding.get("cause_pending"))
        or _cause_is_deferred(narrative),
    }


def _finding_response_schema(response: str) -> Mapping[str, Any]:
    """Read one finding from the Markdown the worker asked for.

    The narrative is multi-line Markdown — a heading per line, a table row per
    line — so it is carried as the response body rather than as a string field
    inside JSON. A model that will not emit a newline inside a JSON string
    delivers every section flattened onto one line, which parses as a single
    heading with an empty body and fails every section check at once; that is
    what cost a whole run of eight drafts, including complete ones. Markdown has
    no such failure mode, and it is how the planning memorandum has always been
    returned by the same models.

    The title and severity lines are read off the draft and become fields; the
    narrative is what follows the first ``##`` heading, so neither reaches the
    prose that is copied into the report.
    """
    value = str(response or "").strip()
    fenced = _FENCED_MARKDOWN.fullmatch(value)
    if fenced:
        value = fenced.group(1).strip()
    wrapped = _finding_from_json(value)
    if wrapped is not None:
        return {"finding": dict(wrapped)}
    title = _TITLE_LINE.search(value)
    severity = _SEVERITY_LINE.search(value)
    start = _NARRATIVE_START.search(value)
    narrative = (
        templates_store.strip_guidance(value[start.start():]).strip()
        if start
        else ""
    )
    return {
        "finding": {
            "title": title.group(1).strip() if title else "",
            "severity": _plain_note(severity.group(1) if severity else "")
            .rstrip(".")
            .strip()
            .casefold(),
            "narrative": narrative,
            "cause_pending": _cause_is_deferred(narrative),
        }
    }


def validate_finding_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the finding contract; evidence linkage stays with the executor.

    The narrative's shape is the supplied template's, not a list held here, so a
    firm that renames a section moves the repair loop with it. The deterministic
    gate in ``findings.support_issues`` applies the same rule at commit time;
    checking it here is what lets the worker repair a thin draft before one is
    written.
    """
    value = proposal.get("finding")
    if not isinstance(value, Mapping):
        raise WorkerResponseValidationError("finding must be an object")
    # Reading the observation proves the draft was grounded in a supplied one.
    _resolved_item(request, FINDING_OBSERVATION_SOURCE_ID)
    template = str(_resolved_item(request, FINDING_TEMPLATE_SOURCE_ID) or "")
    finding = _plain_json(value)
    errors: list[str] = []
    title = str(finding.get("title") or "").strip()
    if not title or "{{" in title:
        errors.append(
            "the finding needs a title on its own `#` line, naming the audit "
            "point rather than the test"
        )
    if finding.get("severity") not in _FINDING_SEVERITIES:
        errors.append(
            "the finding needs a `**Severity:**` line carrying exactly one of "
            + ", ".join(sorted(_FINDING_SEVERITIES))
        )
    narrative = str(finding.get("narrative") or "")
    bodies = templates_store.section_bodies(narrative)
    for heading in templates_store.sections(template):
        key = templates_store.section_key(heading)
        if bodies.get(key):
            continue
        # A heading that never arrived and one that arrived empty are different
        # mistakes, and saying "is empty" for both is what kept a model
        # re-emitting a narrative it had in fact written — flattened onto one
        # line, so every heading was absent rather than blank.
        if key not in bodies:
            errors.append(
                f"the narrative is missing the `## {heading}` heading; every "
                "template section must appear, each on its own line"
            )
        elif key in _CAUSE_SECTION_KEYS:
            errors.append(
                f"narrative section '{heading}' is empty; state the cause, or "
                "write `_Root cause pending auditor follow-up._` as the whole "
                "of it where the evidence does not establish one"
            )
        else:
            errors.append(
                f"narrative section '{heading}' is empty; every template "
                "section needs text"
            )
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"finding": {**finding, "title": title, "narrative": narrative}}


def run_finding_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {
            "OBSERVATION": _resolved_item(request, FINDING_OBSERVATION_SOURCE_ID),
            "IMMUTABLE EXECUTION RESULT": _resolved_item(
                request, FINDING_EXECUTION_SOURCE_ID
            ),
            "FINDING TEMPLATE": _resolved_item(request, FINDING_TEMPLATE_SOURCE_ID),
            "EXCEPTION ROWS": _optional_item(
                request, FINDING_EXCEPTION_ROWS_SOURCE_ID
            ),
            "RESOLVED CONTEXT": request.context.to_dict(),
            "REQUIRED OUTPUT": (
                "Markdown only: a `#` title line, a `**Severity:**` line, then "
                "the narrative sections below as `##` headings, each on its own "
                "line."
            ),
            "REQUIRED NARRATIVE SECTIONS": templates_store.sections(
                str(_resolved_item(request, FINDING_TEMPLATE_SOURCE_ID) or "")
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return the whole finding again as Markdown, corrected."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "finding_draft",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return str(
        gateway.complete(FINDING_SYSTEM, user, activity, attempt=attempt.number)
        or ""
    )


FINDING_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="reporting.finding.response",
    schema_hash=_sha256_text("finding-response:template-shaped-markdown"),
    validator=_finding_response_schema,
)
FINDING_WORKER = WorkerDefinition(
    worker_id=FINDING_WORKER_ID,
    prompt_hash=_sha256_text(FINDING_SYSTEM),
    response_schema=FINDING_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair finding contract violations against the supplied observation."
        ),
    ),
    implementation=run_finding_worker,
    semantic_validator=validate_finding_proposal,
)

WORKERS.register(FINDING_WORKER)


__all__ = [
    "FINDING_EXCEPTION_ROWS_SOURCE_ID",
    "FINDING_RESPONSE_SCHEMA",
    "FINDING_SYSTEM",
    "FINDING_TEMPLATE_SOURCE_ID",
    "FINDING_WORKER",
    "FINDING_WORKER_ID",
    "run_finding_worker",
    "validate_finding_proposal",
]
