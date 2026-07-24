"""Registered model workers for audit reporting capabilities.

The finding worker turns one auditor-dispositioned observation and its immutable
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


FINDING_WORKER_ID = "reporting.finding"
FINDING_SYSTEM = f"""[agent:finding]
Draft one unconfirmed audit finding from the supplied auditor-dispositioned
observation and immutable execution reference. Return finding with title,
severity (critical|high|medium|low|info), condition, criteria, cause or
cause_pending, effect, recommendation, and severity_rationale. Do not create or
alter RCM, planned-test, execution, or evidence references. Do not claim auditor
confirmation. {JSON_RULES}"""

FINDING_OBSERVATION_SOURCE_ID = "observation"
FINDING_EXECUTION_SOURCE_ID = "execution_result"
_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_FINDING_REQUIRED = (
    "title",
    "severity",
    "condition",
    "criteria",
    "effect",
    "recommendation",
    "severity_rationale",
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


def _finding_response_schema(response: str) -> Mapping[str, Any]:
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
    if not isinstance(payload, dict) or not isinstance(payload.get("finding"), dict):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a `finding` object"
        )
    return {"finding": payload["finding"]}


def validate_finding_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the finding contract; evidence linkage stays with the executor."""
    value = proposal.get("finding")
    if not isinstance(value, Mapping):
        raise WorkerResponseValidationError("finding must be an object")
    # Reading the observation proves the draft was grounded in a supplied one.
    _resolved_item(request, FINDING_OBSERVATION_SOURCE_ID)
    finding = _plain_json(value)
    errors: list[str] = []
    missing = [
        key for key in _FINDING_REQUIRED if not str(finding.get(key) or "").strip()
    ]
    if missing:
        errors.append(f"finding is missing {missing[0]}")
    if finding.get("severity") not in _FINDING_SEVERITIES:
        errors.append("finding severity is unsupported")
    if not str(finding.get("cause") or "").strip() and not finding.get("cause_pending"):
        errors.append("finding needs cause or cause_pending")
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
            "RESOLVED CONTEXT": request.context.to_dict(),
            "REQUIRED OUTPUT": [
                "title",
                "severity",
                "condition",
                "criteria",
                "cause",
                "cause_pending",
                "effect",
                "recommendation",
                "severity_rationale",
            ],
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
    "FINDING_RESPONSE_SCHEMA",
    "FINDING_SYSTEM",
    "FINDING_WORKER",
    "FINDING_WORKER_ID",
    "run_finding_worker",
    "validate_finding_proposal",
]
