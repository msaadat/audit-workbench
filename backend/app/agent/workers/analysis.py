"""Registered model worker for the exploratory data-analysis workflow.

The analysis-definition worker turns one target frame's declared metadata
context — schema, bounded statistical profile, value-free aggregates, related
frame schemas, and the deterministic relationship evidence — into rerunnable
saved-analysis definitions. It owns its prompt, its bundle-to-message
transformation, and the part of the contract the supplied context can decide:
response shape, kind enum, registry-ID membership, exact column spelling against
the supplied schema, the static Polars sandbox contract, and de-duplication
against the analyses it was shown.

The authoritative execution stays with the registered executor, which owns the
workspace frames.  This worker nevertheless validates the complete library-test
contract it supplied to the model: parameter names, required fields, select
options, number values, and schema-visible column types. Importing the
``analytics`` registry payload and the static ``sandbox`` validator here is
deliberate and follows the fieldwork precedent: both are application catalogs
and static contracts rather than engagement content, so they belong to the
response contract the worker enforces, not to the declared engagement context.

The worker never sees a table row: the declared context denies
``allow_table_rows`` and the resolver rejects the representation structurally.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import analytics, sandbox
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


ANALYSIS_DEFINITION_WORKER_ID = "analysis.definitions"
ANALYSIS_DEFINITION_SYSTEM = f"""[agent:analysis_definitions]
Propose rerunnable data analyses for exactly one supplied target frame. Submit
1 to 4 objects through the required function tool; its JSON Schema is the
authoritative output contract. For analytics, use only a test ID and parameters
permitted by that schema, with exact supplied column names.
For python, spec contains code: safe Polars that reads the supplied frames
through the `tables` mapping or their bare table variables and assigns the
result DataFrame to `result`; imports and any file or network access are
forbidden. Use `series.len()` (not `series.height`), use
`.dt.total_days()` for duration values (not `.dt.days()`), and specify a join
suffix or select columns before joining frames with overlapping column names.
Every generated Python analysis is run locally against its supplied workspace
tables before it can be saved. Every analysis must be relevant to the supplied schema, profile,
aggregates, and relationship evidence, and must not repeat an analysis already
supplied in current_analyses. You are never shown table rows and must not invent
values, counts, or relationships. {JSON_RULES}"""

TARGET_SCHEMA_SOURCE_ID = "target_schema"
ANALYTICS_REGISTRY_SOURCE_ID = "analytics_registry"
CURRENT_ANALYSES_SOURCE_ID = "current_analyses"
ANALYSIS_SUBMISSION_TOOL = "submit_analysis_definitions"

MAX_PROPOSED_ANALYSES = 4
ANALYSIS_KINDS = ("analytics", "python")


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _source_items(request: WorkerRequest, source_id: str) -> list[object]:
    return [item.content for item in request.context.items if item.source_id == source_id]


def _resolved_item(request: WorkerRequest, source_id: str) -> object:
    matches = _source_items(request, source_id)
    if len(matches) != 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply exactly one item."
        )
    return matches[0]


def _target_schema(request: WorkerRequest) -> dict[str, Any]:
    schema = _plain_json(_resolved_item(request, TARGET_SCHEMA_SOURCE_ID))
    if not isinstance(schema, dict) or not str(schema.get("table") or "").strip():
        raise WorkerContractError("The supplied target schema names no table.")
    return schema


def analysis_semantic_id(kind: str, table: str, spec: Mapping[str, Any]) -> str:
    """Stable identity for one analysis definition.

    Derived from the frame and the canonical spec rather than the title, so a
    reworded proposal for the same computation deduplicates against the analysis
    already saved instead of creating a second one.
    """
    canonical = json.dumps(
        {"kind": str(kind), "table": str(table), "spec": _plain_json(spec)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "analysis:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _analytics_contract(request: WorkerRequest) -> dict[str, dict[str, Any]]:
    """Return the complete test contract that was supplied to the worker."""
    registry = _plain_json(_resolved_item(request, ANALYTICS_REGISTRY_SOURCE_ID))
    supplied = {
        str(item.get("id")): dict(item)
        for item in (registry if isinstance(registry, list) else [])
        if isinstance(item, Mapping) and item.get("id")
    }
    return supplied or {
        str(item["id"]): item for item in analytics.registry_payload()
    }


def _column_names(schema: Mapping[str, Any]) -> set[str]:
    return {
        str(column.get("name"))
        for column in schema.get("columns") or []
        if str(column.get("name") or "").strip()
    }


def _column_types(schema: Mapping[str, Any]) -> dict[str, str]:
    """Map exact source columns to the type visible in the model's schema."""
    return {
        str(column.get("name")): str(column.get("type") or "")
        for column in schema.get("columns") or []
        if str(column.get("name") or "").strip()
    }


def _existing_semantic_ids(request: WorkerRequest) -> set[str]:
    ids: set[str] = set()
    for raw in _source_items(request, CURRENT_ANALYSES_SOURCE_ID):
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            continue
        semantic = str(item.get("semantic_id") or "").strip()
        if semantic:
            ids.add(semantic)
            continue
        ids.add(
            analysis_semantic_id(
                str(item.get("kind") or ""),
                str(item.get("table") or ""),
                item.get("spec") or {},
            )
        )
    return ids


def _parameter_json_schema(
    parameter: Mapping[str, Any],
    columns: set[str],
    column_types: Mapping[str, str],
) -> dict[str, Any] | None:
    """Translate one library parameter contract into provider JSON Schema."""
    kind = str(parameter.get("kind") or "")
    if kind in {"column", "columns"}:
        expected = str(parameter.get("column_kind") or "")
        eligible = sorted(
            column
            for column in columns
            if not expected or column_types.get(column) == expected
        )
        if not eligible:
            return None
        if kind == "column":
            return {"type": "string", "enum": eligible}
        return {
            "type": "array",
            "items": {"type": "string", "enum": eligible},
            "minItems": 1,
        }
    if kind == "select":
        values = [
            option.get("value")
            for option in parameter.get("options") or []
            if isinstance(option, Mapping) and "value" in option
        ]
        return {"enum": values}
    if kind == "number":
        return {"type": "number"}
    return {}


def _analytics_spec_schemas(
    request: WorkerRequest,
) -> list[dict[str, Any]]:
    """Build exact per-test schemas from the contract and target columns."""
    schema = _target_schema(request)
    columns = _column_names(schema)
    column_types = _column_types(schema)
    branches: list[dict[str, Any]] = []
    for test_id, metadata in sorted(_analytics_contract(request).items()):
        properties: dict[str, Any] = {}
        required: list[str] = []
        usable = True
        for raw in metadata.get("params") or []:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            parameter_schema = _parameter_json_schema(raw, columns, column_types)
            is_required = not raw.get("optional") and "default" not in raw
            if parameter_schema is None:
                if is_required:
                    usable = False
                    break
                continue
            if "default" in raw:
                parameter_schema["default"] = _plain_json(raw["default"])
            properties[name] = parameter_schema
            if is_required:
                required.append(name)
        if not usable:
            continue
        params_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            params_schema["required"] = required
        branches.append(
            {
                "type": "object",
                "properties": {
                    "test": {"type": "string", "enum": [test_id]},
                    "params": params_schema,
                },
                "required": ["test", "params"],
                "additionalProperties": False,
            }
        )
    return branches


def _analysis_submission_tool(request: WorkerRequest) -> dict[str, Any]:
    """One forced function tool whose schema constrains every generated spec."""
    analytics_specs = _analytics_spec_schemas(request)
    item_branches: list[dict[str, Any]] = []
    if analytics_specs:
        item_branches.append(
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": ["analytics"]},
                    "spec": {"oneOf": analytics_specs},
                    "note": {"type": "string"},
                },
                "required": ["title", "kind", "spec"],
                "additionalProperties": False,
            }
        )
    item_branches.append(
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["python"]},
                "spec": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "minLength": 1},
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
                "note": {"type": "string"},
            },
            "required": ["title", "kind", "spec"],
            "additionalProperties": False,
        }
    )
    return {
        "type": "function",
        "function": {
            "name": ANALYSIS_SUBMISSION_TOOL,
            "description": (
                "Submit one to four rerunnable analysis definitions. The schema "
                "contains every permitted analytics ID, parameter, and column."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "analyses": {
                        "type": "array",
                        "items": {"oneOf": item_branches},
                        "minItems": 1,
                        "maxItems": MAX_PROPOSED_ANALYSES,
                    }
                },
                "required": ["analyses"],
                "additionalProperties": False,
            },
        },
    }


def _submission_response(message: object) -> str:
    """Extract forced-tool arguments, with a text fallback for weak providers."""
    if not isinstance(message, Mapping):
        return str(message or "")
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        matches = [
            item
            for item in tool_calls
            if isinstance(item, Mapping)
            and isinstance(item.get("function"), Mapping)
            and item["function"].get("name") == ANALYSIS_SUBMISSION_TOOL
        ]
        if len(matches) == 1:
            arguments = matches[0]["function"].get("arguments")
            if isinstance(arguments, str):
                return arguments
            if isinstance(arguments, Mapping):
                return json.dumps(arguments, ensure_ascii=False)
    return str(message.get("content") or "")


def _analysis_response_schema(response: str) -> Mapping[str, Any]:
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
    proposed = payload.get("analyses")
    if proposed is None and isinstance(payload.get("analysis"), dict):
        # A single-object answer is a common shape slip; normalize rather than
        # spending a repair turn on it.
        proposed = [payload["analysis"]]
    if not isinstance(proposed, list) or not proposed:
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a non-empty `analyses` array"
        )
    if any(not isinstance(item, dict) for item in proposed):
        raise WorkerResponseValidationError("every analyses entry must be an object")
    return {"analyses": proposed}


def _validate_analytics_spec(
    spec: Mapping[str, Any],
    registry: Mapping[str, Mapping[str, Any]],
    columns: set[str],
    column_types: Mapping[str, str],
    label: str,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    test_id = analytics.canonical_test_id(spec.get("test") or spec.get("test_id"))
    if test_id not in registry:
        errors.append(f"{label} names unknown analytics test '{test_id or ''}'")
        return {"test": test_id, "params": {}}, errors
    raw_params = spec.get("params")
    if raw_params is not None and not isinstance(raw_params, Mapping):
        errors.append(f"{label} params must be an object")
    params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
    meta = registry[test_id]
    definitions = list(meta.get("params") or [])
    allowed = {
        str(parameter.get("name"))
        for parameter in definitions
        if isinstance(parameter, Mapping) and str(parameter.get("name") or "").strip()
    }
    unexpected = sorted(str(name) for name in params if str(name) not in allowed)
    if unexpected:
        errors.append(f"{label} has unsupported parameter '{unexpected[0]}' for '{test_id}'")
    for parameter in definitions:
        if not isinstance(parameter, Mapping):
            continue
        name = str(parameter.get("name"))
        kind = parameter.get("kind")
        has_value = name in params and params[name] not in (None, "", [])
        value = params.get(name)
        if not has_value:
            if not parameter.get("optional") and "default" not in parameter:
                errors.append(f"{label} is missing the required '{name}' parameter")
            continue
        if kind == "column":
            if str(value) not in columns:
                errors.append(
                    f"{label} names column '{value}' which is not in the supplied schema"
                )
            elif parameter.get("column_kind") and column_types.get(str(value)) != parameter["column_kind"]:
                errors.append(
                    f"{label} requires a {parameter['column_kind']} column for '{name}', "
                    f"but '{value}' is {column_types.get(str(value)) or 'unknown'}"
                )
        elif kind == "columns":
            values = value if isinstance(value, list) else []
            if not values:
                errors.append(f"{label} is missing the '{name}' column list")
            else:
                normalized_values = [str(item) for item in values]
                if len(normalized_values) != len(set(normalized_values)):
                    errors.append(f"{label} repeats a column in the '{name}' column list")
                unknown = [item for item in normalized_values if item not in columns]
                if unknown:
                    errors.append(
                        f"{label} names column '{unknown[0]}' which is not in the "
                        "supplied schema"
                    )
        elif kind == "select":
            allowed_values = [
                option.get("value")
                for option in parameter.get("options") or []
                if isinstance(option, Mapping) and "value" in option
            ]
            if not any(
                type(value) is type(allowed_value) and value == allowed_value
                for allowed_value in allowed_values
            ):
                errors.append(
                    f"{label} parameter '{name}' must be one of {allowed_values!r}"
                )
        elif kind == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            errors.append(f"{label} parameter '{name}' must be a number")
    return {"test": test_id, "params": params}, errors


def _validate_python_spec(
    spec: Mapping[str, Any],
    frames: set[str],
    label: str,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    code = str(spec.get("code") or "").strip()
    if not code:
        errors.append(f"{label} has no python code")
        return {"code": code}, errors
    try:
        # Static contract only: no execution, no workspace, no frames.
        sandbox.validate(code)
    except sandbox.SandboxError as error:
        errors.append(f"{label} is not safe Polars: {error}")
    referenced = set(re.findall(r"tables\[['\"]([^'\"]+)['\"]\]", code))
    unknown = sorted(referenced - frames)
    if unknown:
        errors.append(
            f"{label} reads table '{unknown[0]}' which was not supplied as context"
        )
    return {"code": code}, errors


def validate_analysis_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply everything the supplied context can decide about the proposal.

    Every violation across every proposed analysis is collected so one bounded
    repair turn can correct them together. The target frame is taken from the
    supplied schema rather than from the model, so a proposal cannot retarget
    itself at a frame it was never shown.
    """
    schema = _target_schema(request)
    target = str(schema["table"])
    columns = _column_names(schema)
    column_types = _column_types(schema)
    registry = _analytics_contract(request)
    existing = _existing_semantic_ids(request)
    related = {
        str((_plain_json(item) or {}).get("table"))
        for item in _source_items(request, "related_frames")
        if isinstance(_plain_json(item), Mapping)
    }
    frames = {target, *(name for name in related if name)}

    proposed = list(proposal.get("analyses") or [])
    errors: list[str] = []
    if len(proposed) > MAX_PROPOSED_ANALYSES:
        errors.append(
            f"return at most {MAX_PROPOSED_ANALYSES} analyses; {len(proposed)} were proposed"
        )
        proposed = proposed[:MAX_PROPOSED_ANALYSES]

    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(proposed):
        label = f"analyses[{index}]"
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            errors.append(f"{label} must be an object")
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            errors.append(f"{label} is missing a title")
        kind = str(item.get("kind") or "").strip()
        if kind not in ANALYSIS_KINDS:
            errors.append(
                f"{label} kind must be one of: {', '.join(ANALYSIS_KINDS)}"
            )
            continue
        raw_spec = item.get("spec")
        raw_spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        if kind == "analytics":
            spec, spec_errors = _validate_analytics_spec(
                raw_spec, registry, columns, column_types, label
            )
        else:
            spec, spec_errors = _validate_python_spec(raw_spec, frames, label)
        errors.extend(spec_errors)
        if spec_errors or not title:
            continue
        semantic = analysis_semantic_id(kind, target, spec)
        if semantic in existing:
            errors.append(
                f"{label} repeats an analysis already saved for '{target}'; "
                "propose a different computation"
            )
            continue
        if semantic in seen:
            errors.append(f"{label} duplicates an earlier proposal in this response")
            continue
        seen.add(semantic)
        accepted.append(
            {
                "title": title,
                "kind": kind,
                # The frame comes from the supplied context, never the model.
                "table": target,
                "spec": spec,
                "note": str(item.get("note") or "").strip(),
                "semantic_id": semantic,
            }
        )

    if errors:
        raise WorkerResponseValidationError(errors)
    if not accepted:
        raise WorkerResponseValidationError(
            "propose at least one analysis that is not already saved"
        )
    return {"analyses": accepted}


def run_analysis_definition_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    schema = _target_schema(request)
    tool = _analysis_submission_tool(request)
    item_branches = tool["function"]["parameters"]["properties"]["analyses"]["items"][
        "oneOf"
    ]
    analytics_branch = next(
        (
            item
            for item in item_branches
            if item["properties"]["kind"]["enum"] == ["analytics"]
        ),
        None,
    )
    eligible_ids = (
        [
            item["properties"]["test"]["enum"][0]
            for item in analytics_branch["properties"]["spec"]["oneOf"]
        ]
        if analytics_branch is not None
        else []
    )
    user = json.dumps(
        {
            "TARGET FRAME": schema,
            "RESOLVED CONTEXT": request.context.to_dict(),
            "ALLOWED ANALYTICS IDS FOR THIS FRAME": eligible_ids,
            "REQUIRED OUTPUT": (
                f"Call {ANALYSIS_SUBMISSION_TOOL} exactly once. Its JSON Schema "
                "is authoritative; do not invent test IDs or parameters."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Allowed analytics IDs are: "
            + ", ".join(eligible_ids)
            + f". Call {ANALYSIS_SUBMISSION_TOOL} once with a complete correction."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "analysis_definition",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    message = gateway.complete(
        ANALYSIS_DEFINITION_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
        tools=[tool],
        tool_choice={
            "type": "function",
            "function": {"name": ANALYSIS_SUBMISSION_TOOL},
        },
        return_message=True,
    )
    return _submission_response(message)


ANALYSIS_DEFINITION_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.definitions.response",
    schema_hash=_sha256_text("analysis-definitions-response:json-object-with-analyses"),
    validator=_analysis_response_schema,
)
ANALYSIS_DEFINITION_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_DEFINITION_WORKER_ID,
    implementation_hash=_sha256_text(
        inspect.getsource(run_analysis_definition_worker)
    ),
    prompt_hash=_sha256_text(ANALYSIS_DEFINITION_SYSTEM),
    response_schema=ANALYSIS_DEFINITION_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair analysis-definition contract violations against the supplied "
            "schema, aggregates, and analytics registry."
        ),
    ),
    implementation=run_analysis_definition_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_analysis_proposal)
    ),
    semantic_validator=validate_analysis_proposal,
)

WORKERS.register(ANALYSIS_DEFINITION_WORKER)


__all__ = [
    "ANALYSIS_DEFINITION_RESPONSE_SCHEMA",
    "ANALYSIS_DEFINITION_SYSTEM",
    "ANALYSIS_DEFINITION_WORKER",
    "ANALYSIS_DEFINITION_WORKER_ID",
    "ANALYSIS_KINDS",
    "MAX_PROPOSED_ANALYSES",
    "analysis_semantic_id",
    "run_analysis_definition_worker",
    "validate_analysis_proposal",
]
