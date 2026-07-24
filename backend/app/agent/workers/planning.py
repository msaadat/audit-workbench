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
            f"APM context source '{source_id}' must supply exactly one item."
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
risk_rating (low|medium|high|critical), assertion, control, control_type, and
test_procedure. All ids and narrative fields are strings. New rows also include
new_risk_reason as a string. Do not invent control
operation as fact when evidence is absent. {JSON_RULES}"""

RCM_CURRENT_ROWS_SOURCE_ID = "current_rcm"
_RCM_REQUIRED_FIELDS = (
    "process",
    "risk",
    "risk_rating",
    "assertion",
    "control",
    "control_type",
    "test_procedure",
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


__all__ = [
    "APM_RESPONSE_SCHEMA",
    "APM_SYSTEM",
    "APM_WORKER",
    "APM_WORKER_ID",
    "RCM_RESPONSE_SCHEMA",
    "RCM_SYSTEM",
    "RCM_WORKER",
    "RCM_WORKER_ID",
    "run_apm_worker",
    "run_rcm_worker",
    "validate_apm_proposal",
    "validate_rcm_proposal",
]
