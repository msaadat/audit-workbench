"""Registered model worker for folder-intake file classification.

`P10.2` moved this out of `IntakeRunner`. The runner is retained as a protocol
runner (see `docs/agent-protocol-runner-decisions.md`), but its one model call is
not exempt from the target worker contract: the prompt, the response schema, and
the semantic checks live here, hash-identified, and the registry owns the bounded
repair loop through the shared `ModelGateway`.

The worker receives only the resolved local bundle — one `file_metadata` item per
uploaded staged file, from the declared `intake.classification` preset. It has no
workspace, batch, or filesystem access, so it cannot see a cell value, a row
preview, a formula, a comment, or extracted document text, and it cannot invent a
staged file that the batch does not contain.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ...intake import DOCUMENT_CATEGORIES, ROUTES
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

CLASSIFICATION_WORKER_ID = "intake.classification"
STAGED_FILE_SOURCE_ID = "staged_files"

CONFIDENCE_VALUES = ("high", "medium", "low")
PROPOSED_ACTIONS = ("import", "ignore")
TABLE_ROLES = (
    "population",
    "master_lookup",
    "prior_period",
    "schedule",
    "parameters",
    "unknown",
)


CLASSIFICATION_SYSTEM = f"""[agent:file_classification]
You classify files in a browser-selected audit folder from local technical
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
Categories. "voucher" is transaction-level source evidence for any business
cycle, not only a document titled "voucher": a purchase order, a goods receipt,
a payslip, a timesheet, a dealing ticket, a counterparty confirmation, a
payment instruction, a bill of lading, a journal voucher, a tax payment receipt
all belong to it. "evidence" is other support obtained for a test. "policy",
"regulation", "contract", "minutes", "prior_report", "correspondence" and
"background" are planning material describing how the entity should operate,
not a record of one transaction. Reach for "other" only when the filename
supports none of the rest.
Technical parser metadata is authoritative. Do not propose importing a file
whose local parser failed. Filenames can be suggestive but are not evidence of
document content."""


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def supplied_files(request: WorkerRequest) -> list[dict[str, Any]]:
    """The staged files this worker was actually supplied, in item-id order."""

    files: list[dict[str, Any]] = []
    for item in request.context.items:
        if item.source_id != STAGED_FILE_SOURCE_ID:
            continue
        value = _plain_json(item.content)
        if not isinstance(value, Mapping):
            raise WorkerContractError(
                "The staged-file context source must supply objects."
            )
        files.append(dict(value))
    if not files:
        raise WorkerContractError(
            "The intake classification worker requires at least one staged file."
        )
    return sorted(files, key=lambda value: str(value.get("id") or ""))


def _json_object(response: str) -> dict[str, Any]:
    """Parse the shared fenced-or-bare JSON envelope, saying where it broke."""

    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    return payload


def _classification_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    items = payload.get("items")
    if not isinstance(items, list) or any(
        not isinstance(item, dict) for item in items
    ):
        raise WorkerResponseValidationError("`items` must be an array of objects")
    return {"items": items}


def validate_classification_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Keep only proposals about files this worker was supplied.

    Every violation is collected so the single repair turn corrects them
    together. An unknown item id is rejected rather than dropped, because
    silently discarding it would let a model that renamed or invented an
    identifier look like a model that simply had nothing to say about a file.
    """
    known = {str(item.get("id") or "") for item in supplied_files(request)}
    errors: list[str] = []
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(proposal.get("items") or [], start=1):
        item = dict(_plain_json(raw) or {})
        item_id = str(item.get("id") or item.get("item_id") or "").strip()
        if item_id not in known:
            errors.append(f"item {index} names unknown file id '{item_id}'")
            continue
        if item_id in seen:
            errors.append(f"file id '{item_id}' is classified more than once")
            continue
        seen.add(item_id)
        route = item.get("route")
        if route is not None and route not in ROUTES:
            errors.append(f"file id '{item_id}' has unsupported route '{route}'")
            continue
        category = item.get("document_category")
        if category is not None and category not in DOCUMENT_CATEGORIES:
            errors.append(
                f"file id '{item_id}' has unsupported document_category '{category}'"
            )
            continue
        role = item.get("table_role")
        if role is not None and role not in TABLE_ROLES:
            errors.append(f"file id '{item_id}' has unsupported table_role '{role}'")
            continue
        confidence = item.get("confidence")
        if confidence is not None and confidence not in CONFIDENCE_VALUES:
            errors.append(
                f"file id '{item_id}' has unsupported confidence '{confidence}'"
            )
            continue
        action = item.get("proposed_action")
        if action is not None and action not in PROPOSED_ACTIONS:
            errors.append(
                f"file id '{item_id}' has unsupported proposed_action '{action}'"
            )
            continue
        items.append({**item, "id": item_id})
    if errors:
        raise WorkerResponseValidationError("; ".join(errors))
    return {"items": sorted(items, key=lambda value: str(value.get("id") or ""))}


def run_classification_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied staged-file metadata into one model request."""

    payload = {
        "batch_id": str(request.unit_input.get("batch_id") or ""),
        "items": supplied_files(request),
    }
    user = json.dumps(payload, indent=1, default=str)
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
            "worker_kind": "intake_classification",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(CLASSIFICATION_SYSTEM, user, activity, attempt=attempt.number)


CLASSIFICATION_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="intake.classification.response",
    schema_hash=_sha256_text("intake-classification-response:json-object-with-items"),
    validator=_classification_response_schema,
)
CLASSIFICATION_WORKER = WorkerDefinition(
    worker_id=CLASSIFICATION_WORKER_ID,
    prompt_hash=_sha256_text(CLASSIFICATION_SYSTEM),
    response_schema=CLASSIFICATION_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the folder-intake classification against the supplied staged "
            "file identifiers and the declared enumerations."
        ),
    ),
    implementation=run_classification_worker,
    semantic_validator=validate_classification_proposal,
)

WORKERS.register(CLASSIFICATION_WORKER)


__all__ = [
    "CLASSIFICATION_RESPONSE_SCHEMA",
    "CLASSIFICATION_SYSTEM",
    "CLASSIFICATION_WORKER",
    "CLASSIFICATION_WORKER_ID",
    "CONFIDENCE_VALUES",
    "PROPOSED_ACTIONS",
    "STAGED_FILE_SOURCE_ID",
    "TABLE_ROLES",
    "run_classification_worker",
    "supplied_files",
    "validate_classification_proposal",
]
