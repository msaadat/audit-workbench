"""Registered model workers for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

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


__all__ = [
    "APM_RESPONSE_SCHEMA",
    "APM_SYSTEM",
    "APM_WORKER",
    "APM_WORKER_ID",
    "run_apm_worker",
    "validate_apm_proposal",
]
