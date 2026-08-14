"""Registered model workers for audit reporting capabilities.

The finding worker turns one exception observation and its immutable
execution result into an unconfirmed finding draft. It owns the prompt, the
bundle-to-message transformation, and the response contract; evidence linking,
support validation, and the durable write belong to the registered executor.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import templates_store
from ..prompts import JSON_RULES
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
    decode_json_response,
)


FINDING_WORKER_ID = "reporting.finding"
FINDING_SYSTEM = f"""[agent:finding]
Draft one unconfirmed audit finding from the supplied exception observation and
immutable execution reference. Return finding with title, severity
(critical|high|medium|low|info), narrative, and cause_pending.

narrative is Markdown. Its sections are the `##` headings of the supplied
finding template, in that order, with no heading added, renamed, or dropped.
Follow the guidance comments in that template: they are instructions to you and
must not be copied into the narrative. Every section must carry text, except
that the root-cause section may be left empty when you set cause_pending true
because the supplied evidence does not establish why the exception occurred.

The narrative is copied into the audit report unchanged, so write final report
prose: no first person, no test ids, run ids, or run mechanics, and no
commentary about drafting. Use British spelling throughout — analyse,
summarise, recognise, organisation — so the deliverable matches the rest of the
audit file. Any number you state must be a number the supplied execution result
holds.

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
not claim auditor confirmation. {JSON_RULES}"""

FINDING_OBSERVATION_SOURCE_ID = "observation"
FINDING_EXECUTION_SOURCE_ID = "execution_result"
FINDING_TEMPLATE_SOURCE_ID = "finding_template"
FINDING_EXCEPTION_ROWS_SOURCE_ID = "exception_rows"
_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_FINDING_REQUIRED = ("title", "severity", "narrative")
# The root-cause section is the one a draft may leave open, and only by saying
# so through ``cause_pending``.
_CAUSE_SECTION_KEYS = frozenset({"cause", "root cause"})


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


def _finding_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("finding"), dict):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a `finding` object"
        )
    return {"finding": payload["finding"]}


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
    missing = [
        key for key in _FINDING_REQUIRED if not str(finding.get(key) or "").strip()
    ]
    if missing:
        errors.append(f"finding is missing {missing[0]}")
    if finding.get("severity") not in _FINDING_SEVERITIES:
        errors.append("finding severity is unsupported")
    bodies = templates_store.section_bodies(str(finding.get("narrative") or ""))
    for heading in templates_store.sections(template):
        key = templates_store.section_key(heading)
        if bodies.get(key):
            continue
        if key in _CAUSE_SECTION_KEYS and finding.get("cause_pending"):
            continue
        errors.append(
            f"narrative section '{heading}' is empty; every template section "
            "needs text"
            + (
                " unless cause_pending is true"
                if key in _CAUSE_SECTION_KEYS
                else ""
            )
        )
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"finding": finding}


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
            "REQUIRED OUTPUT": ["title", "severity", "narrative", "cause_pending"],
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
            + ". Return a complete corrected JSON object."
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
    return gateway.complete(
        FINDING_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


FINDING_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="reporting.finding.response",
    schema_hash=_sha256_text("finding-response:json-object-with-finding"),
    validator=_finding_response_schema,
)
FINDING_WORKER = WorkerDefinition(
    worker_id=FINDING_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_finding_worker)),
    prompt_hash=_sha256_text(FINDING_SYSTEM),
    response_schema=FINDING_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair finding contract violations against the supplied observation."
        ),
    ),
    implementation=run_finding_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_finding_proposal)),
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
