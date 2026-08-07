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
them through the required function tool; its JSON Schema is the authoritative
output contract, including how many analyses this frame takes and which tests
its size supports. For analytics, use only a test ID and parameters permitted
by that schema, with exact supplied column names.
For python, spec contains code: safe Polars that reads the supplied frames
through the `tables` mapping or their bare table variables and assigns the
result DataFrame to `result`; imports and any file or network access are
forbidden. Use `series.len()` (not `series.height`), use
`.dt.total_days()` for duration values (not `.dt.days()`), and specify a join
suffix or select columns before joining frames with overlapping column names.
Every generated Python analysis is run locally against its supplied workspace
tables before it can be saved. Set outcome_policy to ``exception_rows`` only
when every returned row is a potential exception; otherwise use
``informational``. Every analysis must be relevant to the supplied schema, profile,
aggregates, and relationship evidence, and must not repeat an analysis already
supplied in current_analyses. Those cover the target frame's whole join family,
so a check on columns that all come from one table is already covered wherever
it is saved: on a joined frame, prefer analyses that use columns from more than
one of the joined tables, since that is what the join makes possible. You are
never shown table rows and must not invent values, counts, or relationships.
{JSON_RULES}"""

TARGET_SCHEMA_SOURCE_ID = "target_schema"
ANALYTICS_REGISTRY_SOURCE_ID = "analytics_registry"
CURRENT_ANALYSES_SOURCE_ID = "current_analyses"
ANALYSIS_SUBMISSION_TOOL = "submit_analysis_definitions"

MAX_PROPOSED_ANALYSES = 4
ANALYSIS_KINDS = ("analytics", "python")

# Tests whose answer is a property of a population rather than of a row. An
# IQR fence over four values, a "rare" category among four, or a monthly trend
# across two months is arithmetic without a finding in it — and it still spends
# one of the frame's proposal slots and one execution. Below the threshold
# these are dropped from the tool schema, so they cannot be proposed at all
# rather than being rejected after the model has already spent a turn on them.
# Integrity checks — completeness, duplicates, sequence gaps, date lags, sign
# scans — are per-row and stay available at any size.
POPULATION_TEST_IDS = frozenset(
    {
        "outliers",
        "stratify",
        "period_compare",
        "threshold_check",
        "rare_values",
        "weekend_activity",
        "sampling",
    }
)
MIN_ROWS_FOR_POPULATION_TESTS = 30

# How many analyses one frame may be asked for. A frame supports a certain
# amount of distinct, useful work and no more: a four-row lookup table has a
# completeness check and a duplicate check in it, and asking for four means the
# last two are padding that later has to be deduplicated, executed, and read.
SMALL_FRAME_ROWS = 30
MODEST_FRAME_ROWS = 100
SMALL_FRAME_ANALYSES = 2
MODEST_FRAME_ANALYSES = 3


def proposal_budget(rows: int | None) -> int:
    """How many analyses to ask of a frame, from the size of the frame."""
    if rows is None:
        return MAX_PROPOSED_ANALYSES
    if rows < SMALL_FRAME_ROWS:
        return SMALL_FRAME_ANALYSES
    if rows < MODEST_FRAME_ROWS:
        return MODEST_FRAME_ANALYSES
    return MAX_PROPOSED_ANALYSES


# Carried in the validation error when a frame has nothing of its own left to
# contribute — every proposal either repeats a computation its join family
# already holds, or reads only one of the tables the frame was built from. That
# is a settled outcome, not a contract violation, so the binder matches on this
# to settle the unit as skipped once the repair turn has had its chance.
NOTHING_NEW_TO_ANALYSE = "nothing_new_to_analyse"


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


def _resolve_provenance(
    value: object, origins: Mapping[str, str]
) -> tuple[object, set[str]]:
    """Rewrite every column name in a spec to ``origin_table.column``.

    Walks the spec structurally rather than consulting the parameter contract:
    a column is recognised by being a name the frame actually has, which holds
    for every test in the library without this needing to know any of them.
    """
    if isinstance(value, Mapping):
        resolved: dict[str, object] = {}
        scope: set[str] = set()
        for key, item in value.items():
            resolved[str(key)], found = _resolve_provenance(item, origins)
            scope |= found
        return resolved, scope
    if isinstance(value, (list, tuple)):
        items: list[object] = []
        scope = set()
        for item in value:
            resolved_item, found = _resolve_provenance(item, origins)
            items.append(resolved_item)
            scope |= found
        return items, scope
    if isinstance(value, str) and value in origins:
        return f"{origins[value]}.{value}", {origins[value]}
    return value, set()


def spec_scope(spec: Mapping[str, Any], origins: Mapping[str, str]) -> set[str]:
    """The base tables a spec's columns actually come from."""
    _, tables = _resolve_provenance(_plain_json(spec), origins)
    return tables


def analysis_semantic_id(
    kind: str,
    table: str,
    spec: Mapping[str, Any],
    origins: Mapping[str, str] | None = None,
) -> str:
    """Stable identity for one analysis definition.

    Derived from the canonical spec rather than the title, so a reworded
    proposal for the same computation deduplicates against the analysis already
    saved instead of creating a second one.

    ``origins`` maps the frame's columns to the base tables they come from. With
    it, identity is the computation itself — which columns, from which tables —
    rather than the frame it happened to be written against. That is what makes
    an invoice date lag proposed on ``invoice_data`` and the identical lag
    proposed on ``invoice_data_po_data_joined`` one analysis instead of two:
    the join adds columns, but it does not make an invoice-only test a
    different test. A spec spanning both sides of a join still resolves to its
    own identity, because its scope names both tables.

    Without ``origins`` the frame name carries identity, as before — a Python
    analysis reaches frames the spec never names, so its code is only
    meaningfully identified against the frame it was written for.
    """
    resolved_spec: object = _plain_json(spec)
    scope = str(table)
    if origins and str(kind) != "python":
        resolved_spec, tables = _resolve_provenance(resolved_spec, origins)
        if tables:
            scope = "+".join(sorted(tables))
    canonical = json.dumps(
        {"kind": str(kind), "table": scope, "spec": resolved_spec},
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


def _target_rows(schema: Mapping[str, Any]) -> int | None:
    """The target frame's row count, when the supplied schema declares one."""
    raw = schema.get("rows")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _column_origins(schema: Mapping[str, Any]) -> dict[str, str]:
    """The base table each supplied column originates in, when declared."""
    raw = schema.get("column_origins")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(column): str(origin)
        for column, origin in raw.items()
        if str(column or "").strip() and str(origin or "").strip()
    }


def _existing_semantic_ids(request: WorkerRequest, origins: Mapping[str, str]) -> set[str]:
    """Semantic ids of every analysis the request was shown.

    These come from the target frame's whole join family, so an id here may
    belong to a sibling frame. That is the point: identity is provenance-based,
    so a sibling's saved computation and this frame's proposal of it are the
    same id, and the repeat is caught before it is written.
    """
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
                origins,
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
    rows = _target_rows(schema)
    populous = rows is None or rows >= MIN_ROWS_FOR_POPULATION_TESTS
    branches: list[dict[str, Any]] = []
    for test_id, metadata in sorted(_analytics_contract(request).items()):
        if not populous and test_id in POPULATION_TEST_IDS:
            continue
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
                "outcome_policy": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["exception_rows", "informational"],
                        }
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
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
                        "maxItems": proposal_budget(
                            _target_rows(_target_schema(request))
                        ),
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
    origins = _column_origins(schema)
    registry = _analytics_contract(request)
    existing = _existing_semantic_ids(request, origins)
    related = {
        str((_plain_json(item) or {}).get("table"))
        for item in _source_items(request, "related_frames")
        if isinstance(_plain_json(item), Mapping)
    }
    frames = {target, *(name for name in related if name)}

    proposed = list(proposal.get("analyses") or [])
    errors: list[str] = []
    duplicates: list[str] = []
    single_sided: list[str] = []
    # A frame built from more than one table exists to relate them. An analytics
    # spec there that reads only one of those tables computes what that table
    # alone computes, so it belongs on the table, not here — and it is exactly
    # the shape that filled a joined frame's slots with its sides' work.
    joined_frame = len(set(origins.values())) > 1
    budget = proposal_budget(_target_rows(schema))
    if len(proposed) > budget:
        # Reported, but every proposal is still validated: an over-budget
        # response raises anyway, and trimming first would hide the violations
        # the one repair turn exists to correct together.
        errors.append(
            f"return at most {budget} analyses for a frame of this size; "
            f"{len(proposed)} were proposed"
        )

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
        outcome_policy = item.get("outcome_policy")
        if kind == "python":
            outcome_policy = dict(outcome_policy) if isinstance(outcome_policy, Mapping) else {"mode": "informational"}
            if outcome_policy.get("mode") not in {"exception_rows", "informational"}:
                spec_errors.append(
                    f"{label} outcome_policy.mode must be exception_rows or informational"
                )
        else:
            outcome_policy = None
        errors.extend(spec_errors)
        if spec_errors or not title:
            continue
        if joined_frame and kind == "analytics":
            scope = spec_scope(spec, origins)
            if len(scope) < 2:
                # Dropped, not rejected, for the same reason a repeat is: the
                # rest of the response is still good work.
                single_sided.append(label)
                continue
        semantic = analysis_semantic_id(kind, target, spec, origins)
        if semantic in existing:
            # Dropped rather than rejected: the rest of the response is still
            # good work, and spending the repair turn to re-ask for four when
            # three were fine would cost a model call to gain nothing.
            duplicates.append(label)
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
                **({"outcome_policy": outcome_policy} if outcome_policy else {}),
                "semantic_id": semantic,
            }
        )

    if errors:
        raise WorkerResponseValidationError(errors)
    if not accepted:
        if duplicates or single_sided:
            # Worth one repair turn — a joined frame usually has something its
            # sides cannot compute. If the retry says the same thing, the frame
            # genuinely adds nothing, and the binder settles the unit as
            # skipped rather than failing the run over it.
            reasons = []
            if duplicates:
                reasons.append(
                    "repeats a computation already saved for these columns, on "
                    "this frame or another built from the same tables"
                )
            if single_sided:
                reasons.append(
                    "reads columns from only one of the tables this frame joins"
                )
            raise WorkerResponseValidationError(
                f"{NOTHING_NEW_TO_ANALYSE}: every proposed analysis "
                + ", or ".join(reasons)
                + ". Propose an analysis that uses columns from more than one of "
                "the joined tables."
            )
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


# --------------------------------------------------------------------------- #
# analysis.summary
# --------------------------------------------------------------------------- #
ANALYSIS_SUMMARY_WORKER_ID = "analysis.summary"

# The memo's structure is fixed in code rather than in an editable template: it
# is a derived artifact regenerated from results, so its shape belongs to the
# contract the validator enforces, not to a per-engagement preference.
SUMMARY_SECTIONS: tuple[str, ...] = (
    "Data received and population characteristics",
    "Relationships and joins established",
    "Procedures performed",
    "Exceptions noted",
    "Data quality observations",
    "Further work required",
)

# An embedded result. A fenced block rather than a JSON block list because the
# memo has to survive being read as plain text — in the APM, in the report, in
# an export — and a fence degrades to something legible rather than to broken
# markup.
EMBED_FENCE = "embed"
EMBED_KINDS: tuple[str, ...] = ("chart", "summary_table", "exception_table", "stats")
_EMBED_BLOCK = re.compile(
    r"^```embed[ \t]*\n(.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_EMBED_FIELD = re.compile(r"^([a-z_]+)\s*:\s*(.*)$")

_SECTION_LIST = "\n".join(f"## {section}" for section in SUMMARY_SECTIONS)

ANALYSIS_SUMMARY_SYSTEM = f"""[agent:analysis_summary]
Write the exploratory data analysis summary for one audit engagement, as the
auditor who performed the work would write it for the file. Ground every
statement in the supplied procedures, their recorded verdicts and statistics,
the flagged rows supplied for them, the table profiles, and the supplied
coverage gaps. Never invent a count, a value, an identifier, or a procedure.

Use exactly these level-2 sections, in this order, and no others:
{_SECTION_LIST}

Write continuous prose an auditor would recognise, not a bullet inventory of
every test. Group procedures by what they were testing and what they showed.
Where a procedure flagged rows, say what those rows actually are: name the
document, vendor, or staff identifiers from the supplied flagged rows and give
the amounts and dates, because an exception nobody can locate is not an
exception anybody can follow up. State the population before the exceptions.
Distinguish what the data establishes from what it merely suggests, and say
plainly where a conclusion cannot yet be drawn.

"Further work required" must cover every outstanding, stale, and errored
procedure named in the supplied coverage gaps, every frame carrying no
procedure, and the judgment items the results themselves raise — corroboration
a data test cannot supply, populations needing a different cut, controls the
data cannot see. Do not invent work the supplied material does not support.

To place a result in the memo, emit a fenced block on its own lines:
```{EMBED_FENCE}
analysis: <analysis_id>
as: <{" | ".join(EMBED_KINDS)}>
caption: <one short line saying what the reader should see in it>
```
Use the exact analysis_id of a supplied procedure; an id that was not supplied
is rejected. Embed only where the result carries the paragraph — a chart for a
distribution or trend, exception_table where you have named the exceptions.
Six to twelve embeds across the memo is right; do not embed every procedure.
Return Markdown only, with no JSON wrapper and no outer code fence.
"""

SUMMARY_RESULTS_SOURCE_ID = "analysis_results"
SUMMARY_EXCEPTIONS_SOURCE_ID = "analysis_exceptions"


def _resolved_items(request: WorkerRequest, source_id: str) -> list[object]:
    return [item.content for item in request.context.items if item.source_id == source_id]


def parse_embeds(markdown: str) -> list[dict[str, str]]:
    """Extract the embed directives from a memo, in document order."""
    embeds: list[dict[str, str]] = []
    for match in _EMBED_BLOCK.finditer(markdown or ""):
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            field = _EMBED_FIELD.match(line.strip())
            if field:
                fields[field.group(1)] = field.group(2).strip()
        embeds.append(fields)
    return embeds


def validate_analysis_summary(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Enforce the memo skeleton and reject any citation that was not supplied."""
    markdown = str(proposal.get("markdown") or "").strip()
    if not markdown:
        raise WorkerResponseValidationError("the summary is empty")

    headings = {
        match.group(1).strip().casefold()
        for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", markdown, re.MULTILINE)
    }
    missing = [
        section for section in SUMMARY_SECTIONS if section.casefold() not in headings
    ]
    if missing:
        raise WorkerResponseValidationError(f"missing section '{missing[0]}'")

    supplied = {
        str((item or {}).get("analysis_id") or "")
        for item in _resolved_items(request, SUMMARY_RESULTS_SOURCE_ID)
        if isinstance(item, Mapping)
    }
    if not supplied:
        raise WorkerContractError("The analysis summary context supplied no procedures.")

    embeds = parse_embeds(markdown)
    for embed in embeds:
        analysis_id = embed.get("analysis")
        if not analysis_id:
            raise WorkerResponseValidationError("an embed names no analysis")
        # A citation the reader cannot open is worse than no citation at all: it
        # looks like evidence and resolves to nothing.
        if analysis_id not in supplied:
            raise WorkerResponseValidationError(
                f"embed cites '{analysis_id}', which is not a supplied procedure"
            )
        kind = embed.get("as") or ""
        if kind not in EMBED_KINDS:
            raise WorkerResponseValidationError(
                f"embed for '{analysis_id}' uses unknown kind '{kind}'"
            )
    return {
        "markdown": markdown,
        "cited_analysis_ids": list(dict.fromkeys(embed["analysis"] for embed in embeds)),
    }


def _summary_response_schema(response: str) -> Mapping[str, Any]:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:markdown|md)?\s*\n?(.*?)\n?```", value, re.DOTALL | re.IGNORECASE
    )
    # Only unwrap an outer fence that wraps the whole document. A memo whose
    # body carries embed fences must not be mistaken for a fenced document and
    # gutted down to its first block.
    if fenced:
        inner = fenced.group(1).strip()
        if "```" not in inner:
            value = inner
    heading = re.search(r"(?m)^#{1,6}\s+", value)
    if heading:
        value = value[heading.start() :].strip()
    return {"markdown": value}


def run_analysis_summary_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {"RESOLVED CONTEXT": request.context.to_dict()},
        indent=1,
        ensure_ascii=False,
        default=str,
    )
    if attempt.is_repair:
        user += (
            "\n\nThe previous summary failed validation: "
            + "; ".join(attempt.validation_errors)
            + ". Return the complete corrected summary."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "analysis_summary",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        ANALYSIS_SUMMARY_SYSTEM, user, activity, attempt=attempt.number
    )


ANALYSIS_SUMMARY_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.summary.response",
    schema_hash=_sha256_text("analysis-summary-response:markdown-with-embed-fences"),
    validator=_summary_response_schema,
)
ANALYSIS_SUMMARY_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_SUMMARY_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_analysis_summary_worker)),
    prompt_hash=_sha256_text(ANALYSIS_SUMMARY_SYSTEM),
    response_schema=ANALYSIS_SUMMARY_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair missing summary sections and citations to unsupplied procedures."
        ),
    ),
    implementation=run_analysis_summary_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_analysis_summary)),
    semantic_validator=validate_analysis_summary,
)

WORKERS.register(ANALYSIS_SUMMARY_WORKER)


__all__ = [
    "ANALYSIS_DEFINITION_RESPONSE_SCHEMA",
    "ANALYSIS_DEFINITION_SYSTEM",
    "ANALYSIS_DEFINITION_WORKER",
    "ANALYSIS_DEFINITION_WORKER_ID",
    "ANALYSIS_KINDS",
    "ANALYSIS_SUMMARY_RESPONSE_SCHEMA",
    "ANALYSIS_SUMMARY_SYSTEM",
    "ANALYSIS_SUMMARY_WORKER",
    "ANALYSIS_SUMMARY_WORKER_ID",
    "EMBED_KINDS",
    "MAX_PROPOSED_ANALYSES",
    "SUMMARY_SECTIONS",
    "analysis_semantic_id",
    "parse_embeds",
    "run_analysis_definition_worker",
    "run_analysis_summary_worker",
    "validate_analysis_proposal",
    "validate_analysis_summary",
]
