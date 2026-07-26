"""Registered model workers for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

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
)


APM_WORKER_ID = "planning.apm"
APM_SYSTEM = """[agent:apm]
Draft an audit planning memorandum grounded only in the supplied planning
basis. Document content and methodology excerpts may be present. Methodology
must be cited by pack/version/section. Preserve the
selected Markdown template's structure. Where a fact is unavailable, do not
leave the raw {{placeholder}} token — replace it with a short italic note
such as _[entity — context not available]_ so the reader knows the information
was missing; clearly label assumptions. Return the memorandum as Markdown
only, without a JSON wrapper or Markdown code fence."""

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _resolved_item(request: WorkerRequest, source_id: str) -> object:
    matches = [
        item.content for item in request.context.items if item.source_id == source_id
    ]
    if len(matches) != 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply exactly one item."
        )
    return matches[0]


def _fill_unavailable_placeholders(markdown: str) -> str:
    def replace(match: re.Match[str]) -> str:
        label = match.group(1).replace("_", " ").strip()
        return f"_[{label} - context not available]_"

    return _PLACEHOLDER.sub(replace, markdown)


def _response_schema(response: str) -> Mapping[str, Any]:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:markdown|md)?\s*\n?(.*?)\n?```",
        value,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        value = fenced.group(1).strip()
    if value.startswith("{"):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "apm_markdown" in payload:
            return {"apm_markdown": str(payload.get("apm_markdown") or "")}
        marker = re.search(r'["\']apm_markdown["\']\s*:\s*["\']', value)
        if marker:
            body = value[marker.end() :].strip()
            body = re.sub(r'["\']\s*}\s*$', "", body, count=1).strip()
            try:
                value = json.loads(f'"{body}"').strip()
            except json.JSONDecodeError:
                value = body.replace(r"\n", "\n").replace(r'\"', '"').strip()
    heading = re.search(r"(?m)^#{1,6}\s+", value)
    if heading:
        value = value[heading.start() :].strip()
    return {"apm_markdown": value}


def validate_apm_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Validate template coverage and structured planning contradictions."""
    markdown = _fill_unavailable_placeholders(
        str(proposal.get("apm_markdown") or "").strip()
    )
    if not markdown:
        raise WorkerResponseValidationError("the memorandum is empty")
    template = str(_resolved_item(request, "apm_template") or "")
    headings = {
        match.group(1).strip().casefold()
        for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", markdown, re.MULTILINE)
    }
    required = [
        match.group(1).strip().casefold()
        for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", template, re.MULTILINE)
    ]
    missing = [heading for heading in required if heading not in headings]
    if missing:
        raise WorkerResponseValidationError(
            f"missing template section '{missing[0]}'"
        )
    planning = _resolved_item(request, "planning_context")
    if not isinstance(planning, dict):
        raise WorkerContractError("APM planning context must be an object.")
    structured = (
        planning.get("context")
        if isinstance(planning.get("context"), dict)
        else planning
    )
    normalized = re.sub(r"\s+", " ", markdown.casefold())
    for field_name in ("objective", "scope"):
        if structured.get(field_name) and re.search(
            rf"\b{field_name}\b.{{0,80}}\b(?:not available|not defined|undefined)\b",
            normalized,
        ):
            raise WorkerResponseValidationError(
                f"the memorandum says {field_name} is unavailable despite structured context"
            )
    return {"apm_markdown": markdown}


def run_apm_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    template = str(_resolved_item(request, "apm_template") or "")
    current_apm = str(_resolved_item(request, "current_apm") or "")
    _resolved_item(request, "planning_context")
    user = json.dumps(
        {
            "ACTIVE APM TEMPLATE (verbatim)": template,
            "CURRENT APM TO REVISE": current_apm,
            "RESOLVED CONTEXT": request.context.to_dict(),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        user += (
            "\n\nThe previous APM draft failed the engagement quality gate: "
            + "; ".join(attempt.validation_errors)
            + ". Return a complete corrected memorandum."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "apm",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        APM_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


APM_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.apm.response",
    schema_hash=_sha256_text("apm-response:non-empty-template-complete-markdown"),
    validator=_response_schema,
)
APM_WORKER = WorkerDefinition(
    worker_id=APM_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_apm_worker)),
    prompt_hash=_sha256_text(APM_SYSTEM),
    response_schema=APM_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair missing APM template sections and structured-context contradictions."
        ),
    ),
    implementation=run_apm_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_apm_proposal)),
    semantic_validator=validate_apm_proposal,
)

WORKERS.register(APM_WORKER)


# --------------------------------------------------------------------------- #
# planning.rcm worker (P7C)
# --------------------------------------------------------------------------- #
RCM_WORKER_ID = "planning.rcm"
RCM_SYSTEM = f"""[agent:rcm]
Revise the current risk and control matrix using durable RCM ids. Return an object with `rows`, each
row containing operation (update|create), rcm_id for updates, process, risk,
risk_rating (low|medium|high|critical), assertion, control, and control_type.
All ids and narrative fields are strings. New rows also include new_risk_reason
as a string. Describe the risk and the control only — how the control is tested
is decided later, one test at a time. Do not invent control operation as fact
when evidence is absent. {JSON_RULES}"""

RCM_CURRENT_ROWS_SOURCE_ID = "current_rcm"
_RCM_REQUIRED_FIELDS = (
    "process",
    "risk",
    "risk_rating",
    "assertion",
    "control",
    "control_type",
)
_RCM_RISK_RATINGS = {"low", "medium", "high", "critical"}


def _current_rcm_rows(request: WorkerRequest) -> list[object]:
    return [
        item.content
        for item in request.context.items
        if item.source_id == RCM_CURRENT_ROWS_SOURCE_ID
    ]


def _plain_json(value: object) -> object:
    """Deep-copy frozen proposal values back to plain JSON containers."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _canonical_rcm_id(value: object) -> str:
    """Return the durable id from a bare id or a typed ``rcm:<id>`` reference."""
    text = str(value or "").strip()
    if not text:
        return ""
    prefix, separator, item_id = text.partition(":")
    if not separator:
        return text
    if prefix != "rcm" or not item_id:
        raise ValueError(f"'{text}' is not an RCM reference")
    return item_id


def _rcm_response_schema(response: str) -> Mapping[str, Any]:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        value,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        value = fenced.group(1).strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise WorkerResponseValidationError("the response is not a valid JSON object")
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a `rows` array"
        )
    return {"rows": payload["rows"]}


def validate_rcm_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the RCM engagement quality gate against current durable row ids."""
    rows = proposal.get("rows")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise WorkerResponseValidationError("no RCM rows were proposed")
    existing_ids = {
        str(row.get("id"))
        for row in _current_rcm_rows(request)
        if isinstance(row, Mapping) and row.get("id")
    }
    normalized: list[dict] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise WorkerResponseValidationError(f"RCM row {index} is not an object")
        missing = [
            key for key in _RCM_REQUIRED_FIELDS if not str(row.get(key) or "").strip()
        ]
        if missing:
            raise WorkerResponseValidationError(
                f"RCM row {index} is missing {missing[0]}"
            )
        non_string = [
            key for key in _RCM_REQUIRED_FIELDS if not isinstance(row.get(key), str)
        ]
        if non_string:
            raise WorkerResponseValidationError(
                f"RCM row {index} field {non_string[0]} must be a string"
            )
        if str(row.get("risk_rating")).casefold() not in _RCM_RISK_RATINGS:
            raise WorkerResponseValidationError(
                f"RCM row {index} has an unsupported risk rating"
            )
        operation = str(row.get("operation") or "").strip().lower()
        if operation not in {"update", "create"}:
            raise WorkerResponseValidationError(
                f"RCM row {index} has an unsupported operation"
            )
        if operation == "update":
            try:
                row_id = _canonical_rcm_id(row.get("rcm_id"))
            except ValueError:
                raise WorkerResponseValidationError(
                    f"RCM row {index} has an invalid rcm_id"
                )
            if not row_id or row_id not in existing_ids:
                raise WorkerResponseValidationError(
                    f"RCM row {index} does not identify an existing RCM row"
                )
        if operation == "create" and not isinstance(row.get("new_risk_reason"), str):
            raise WorkerResponseValidationError(
                f"RCM row {index} new_risk_reason must be a string"
            )
        if operation == "create" and not str(row.get("new_risk_reason") or "").strip():
            raise WorkerResponseValidationError(
                f"RCM row {index} does not explain why the risk is new"
            )
        normalized.append(_plain_json(row))
    return {"rows": normalized}


def run_rcm_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    template = str(_resolved_item(request, "rcm_template") or "")
    current_apm = str(_resolved_item(request, "current_apm") or "")
    user = json.dumps(
        {
            "ACTIVE RCM TEMPLATE (verbatim)": template,
            "REVISED APM": current_apm,
            "CURRENT RCM TO REVISE": _current_rcm_rows(request),
            "RESOLVED CONTEXT": request.context.to_dict(),
            "INSTRUCTIONS": (
                "Return the full set of proposed revisions. For an existing risk, "
                "include operation='update' and its exact rcm_id. Use "
                "operation='create' only for a genuinely uncovered risk and include "
                "new_risk_reason. Omission never deletes an existing row."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        user += (
            "\n\nThe previous RCM draft failed the engagement quality gate: "
            + "; ".join(attempt.validation_errors)
            + ". Return a complete corrected JSON object."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "rcm",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        RCM_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


RCM_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.rcm.response",
    schema_hash=_sha256_text("rcm-response:json-object-with-rows-array"),
    validator=_rcm_response_schema,
)
RCM_WORKER = WorkerDefinition(
    worker_id=RCM_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_rcm_worker)),
    prompt_hash=_sha256_text(RCM_SYSTEM),
    response_schema=RCM_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair RCM row contract violations against the current durable row ids."
        ),
    ),
    implementation=run_rcm_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_rcm_proposal)),
    semantic_validator=validate_rcm_proposal,
)

WORKERS.register(RCM_WORKER)


# --------------------------------------------------------------------------- #
# planning.context worker (P7A.2)
# --------------------------------------------------------------------------- #
PLANNING_CONTEXT_WORKER_ID = "planning.context"
PLANNING_CONTEXT_SYSTEM = f"""[agent:document_context]
Extract planning facts only from the included engagement documents.
Return an object with `context`, containing only supported fields that are
grounded in the documents: objective, entity, period, scope, materiality,
key_contacts, and background_notes. Every supplied context value must be a
string; format multiple key contacts as one newline-separated string. Omit fields that the documents do not
support; do not turn policy requirements into claims about actual control
operation. {JSON_RULES}"""

PLANNING_CONTEXT_CURRENT_SOURCE_ID = "current_planning_context"
PLANNING_CONTEXT_DOCUMENT_SOURCE_ID = "planning_documents"
PLANNING_CONTEXT_FIELDS = (
    "objective",
    "entity",
    "period",
    "scope",
    "materiality",
    "key_contacts",
    "background_notes",
)
# The labelled facts the document-analysis contract emits, mapped to the
# planning fields they populate. Recovery is deliberately narrow: only these
# exact labels are accepted, never inferred from prose.
_PLANNING_CONTEXT_LABELS = {
    "objective": "objective",
    "entity": "entity",
    "period": "period",
    "scope": "scope",
    "materiality": "materiality",
    "key contacts": "key_contacts",
    "background notes": "background_notes",
}
_LABELLED_FACT = re.compile(r"^\s*[-*]?\s*\*\*([^*]+?):\*\*\s*(.+?)\s*$")


def _supplied_items(request: WorkerRequest, source_id: str) -> tuple[object, ...]:
    return tuple(
        item.content for item in request.context.items if item.source_id == source_id
    )


def _planning_context_response_schema(response: str) -> Mapping[str, Any]:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        value,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        value = fenced.group(1).strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise WorkerResponseValidationError("the response is not a valid JSON object")
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    context = payload.get("context")
    if context is None:
        # Some providers flatten the requested object wrapper while still
        # returning correctly grounded fields. Normalize that harmless shape
        # drift instead of discarding useful planning context.
        context = {
            key: value
            for key, value in payload.items()
            if key in PLANNING_CONTEXT_FIELDS and isinstance(value, str)
        }
    if not isinstance(context, dict):
        raise WorkerResponseValidationError("`context` must be an object")
    for key, value in context.items():
        if key in PLANNING_CONTEXT_FIELDS and not isinstance(value, str):
            raise WorkerResponseValidationError(f"context.{key} must be a string")
    return {"context": context}


def _recovered_labelled_facts(request: WorkerRequest) -> dict[str, str]:
    """Recover labelled planning facts from the supplied document material.

    Deliberately narrow: only the labelled fields the document-analysis contract
    emits are accepted, and prose is never interpreted. This protects the
    planning chain when synthesis returns valid JSON with an empty context.
    """
    recovered: dict[str, str] = {}
    for content in _supplied_items(request, PLANNING_CONTEXT_DOCUMENT_SOURCE_ID):
        for line in str(content or "").splitlines():
            match = _LABELLED_FACT.match(line)
            if not match:
                continue
            label = re.sub(r"\s+", " ", match.group(1).strip().casefold())
            key = _PLANNING_CONTEXT_LABELS.get(label)
            value = match.group(2).strip()
            if key and value and key not in recovered:
                recovered[key] = value
    return recovered


def validate_planning_context_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Keep only grounded, non-empty declared fields; recover labelled facts.

    A syntactically valid response with no usable field is not repaired by asking
    again: the labelled facts already present in the supplied summaries are the
    better answer and cost nothing. Only when neither the model nor those labels
    yield a field is this a contract violation the model can be told to fix.
    """
    raw = proposal.get("context")
    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError("context must be an object")
    context = {
        key: str(value).strip()
        for key, value in raw.items()
        if key in PLANNING_CONTEXT_FIELDS and str(value or "").strip()
    }
    if context:
        return {"context": context}
    recovered = _recovered_labelled_facts(request)
    if recovered:
        return {"context": recovered, "recovered_from_labelled_facts": True}
    raise WorkerResponseValidationError(
        "context must contain at least one field grounded in the supplied "
        f"documents; supported fields are {', '.join(PLANNING_CONTEXT_FIELDS)}"
    )


def run_planning_context_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    current = _supplied_items(request, PLANNING_CONTEXT_CURRENT_SOURCE_ID)
    documents = _supplied_items(request, PLANNING_CONTEXT_DOCUMENT_SOURCE_ID)
    if not documents:
        raise WorkerContractError(
            "Planning-context synthesis requires at least one supplied document."
        )
    user = (
        "CURRENT PLANNING CONTEXT:\n"
        f"{json.dumps(current[0] if current else {}, default=str)}\n\n"
        "INCLUDED DOCUMENT CONTENT:\n"
        f"{json.dumps(list(documents), default=str)}"
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return a corrected object with a non-empty `context` grounded "
            "only in the supplied documents."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "planning_context",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        PLANNING_CONTEXT_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


PLANNING_CONTEXT_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.context.response",
    schema_hash=_sha256_text("planning-context-response:json-object-with-context"),
    validator=_planning_context_response_schema,
)
PLANNING_CONTEXT_WORKER = WorkerDefinition(
    worker_id=PLANNING_CONTEXT_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_planning_context_worker)),
    prompt_hash=_sha256_text(PLANNING_CONTEXT_SYSTEM),
    response_schema=PLANNING_CONTEXT_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair planning-context synthesis against the supplied documents."
        ),
    ),
    implementation=run_planning_context_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_planning_context_proposal)
    ),
    semantic_validator=validate_planning_context_proposal,
)

WORKERS.register(PLANNING_CONTEXT_WORKER)


__all__ = [
    "APM_RESPONSE_SCHEMA",
    "APM_SYSTEM",
    "APM_WORKER",
    "APM_WORKER_ID",
    "PLANNING_CONTEXT_FIELDS",
    "PLANNING_CONTEXT_RESPONSE_SCHEMA",
    "PLANNING_CONTEXT_SYSTEM",
    "PLANNING_CONTEXT_WORKER",
    "PLANNING_CONTEXT_WORKER_ID",
    "RCM_RESPONSE_SCHEMA",
    "RCM_SYSTEM",
    "RCM_WORKER",
    "RCM_WORKER_ID",
    "run_apm_worker",
    "run_planning_context_worker",
    "run_rcm_worker",
    "validate_apm_proposal",
    "validate_planning_context_proposal",
    "validate_rcm_proposal",
]
