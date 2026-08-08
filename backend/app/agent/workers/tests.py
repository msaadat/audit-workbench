"""Registered model workers for the test capability group.

One worker, one pass: ``tests.generate`` turns one RCM row into the complete,
executable tests that cover it, choosing each test's source and writing its
executable steps in a single turn (docs/test-capability-merge-plan.md).

The contract is deliberately small. The common fields are a discriminator plus
title/objective/steps; a generated Data Test's steps are Polars code against
named tables; a generated Document Test's steps are one of two execution
modes. Larger contracts were tried and produced worse proposals: a
discriminated union over four document-test item kinds and three data-test
engines made the model pick a shape and then fill it badly, and two registry
payloads worth ~14,000 characters crowded the actual engagement material out
of the prompt.

Analytics and validation engines remain fully supported for auditor-authored
Data Tests, and the ``attribute``/``review`` document builders for
auditor-authored Document Tests. They are simply not what generation emits.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import cycle_vouching, doc_tests, sandbox
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


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    """Deep-copy frozen proposal values back to plain JSON containers."""
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


def _source_items(request: WorkerRequest, source_id: str) -> list[object]:
    """Return the supplied contents for one optional source in stable order."""
    return [item.content for item in request.context.items if item.source_id == source_id]


def _generation_prompt_payload(request: WorkerRequest) -> dict[str, object]:
    """Project the durable bundle into the small model-facing generation input.

    The context manifest and bundle retain source identity, representations, and
    supplied-size metrics for provenance.  The model only needs the underlying
    audit material, so do not spend prompt tokens serializing that transport
    envelope on every generation or repair attempt.
    """
    planning = _resolved_item(request, "planning_context")
    if isinstance(planning, Mapping):
        planning = planning.get("context") or {}
    raw_tables = _source_items(request, GENERATE_TABLE_SOURCE_ID)
    table_schemas = []
    for raw in raw_tables:
        if not isinstance(raw, Mapping):
            table_schemas.append(raw)
            continue
        table_schemas.append(_plain_json(raw))
    raw_documents = _source_items(request, GENERATE_DOCUMENT_SOURCE_ID)
    documents = []
    for raw in raw_documents:
        if not isinstance(raw, Mapping):
            documents.append(raw)
            continue
        documents.append(_plain_json(raw))
    transaction_evidence = _resolved_item(
        request, GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID
    )
    return {
        "target_rcm_row": _resolved_item(request, GENERATE_ROW_SOURCE_ID),
        "planning_context": planning,
        "table_schemas": table_schemas,
        "documents": documents,
        "transaction_evidence": transaction_evidence,
        "methodology": _source_items(request, GENERATE_METHODOLOGY_SOURCE_ID),
        "instructions": "Generate complete executable tests for target_rcm_row only.",
    }


def _json_payload(response: str) -> object:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        value,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        value = fenced.group(1).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise WorkerResponseValidationError("the response is not a valid JSON object")


def _context_metrics(request: WorkerRequest, worker_kind: str) -> dict:
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": worker_kind,
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return activity


def _repair_instruction(attempt: WorkerAttempt) -> str:
    """Tell the model to correct the exact prior candidate in place."""

    if not attempt.is_repair:
        raise WorkerContractError("Repair guidance requires a repair attempt.")
    return (
        "The preceding JSON response could not be used: "
        + "; ".join(attempt.validation_errors)
        + ". Correct those violations while preserving every unaffected test and "
        "field. Return the complete corrected JSON object."
    )


# Comparison methods stay the auditor's to change; generation always uses the
# established default rather than asking the model to pick one.
DEFAULT_COMPARISON_METHOD = "normalized"
assert DEFAULT_COMPARISON_METHOD in doc_tests.METHODS

# The citation fields a committed test persists in ``methodology_refs``.
_METHODOLOGY_REF_FIELDS = (
    "pack_id",
    "pack_name",
    "version",
    "sha1",
    "section",
    "citation",
)


# --------------------------------------------------------------------------- #
# tests.generate worker
#
# Replaces the retired two-pass ``tests.draft`` / ``tests.data_spec`` /
# ``tests.document_spec`` flow with one worker that decides source and writes
# the complete executable definition in a single turn, per the merge plan
# (docs/test-capability-merge-plan.md).
# --------------------------------------------------------------------------- #
GENERATE_WORKER_ID = "tests.generate"

# Phase 2 clean contract. The former broad-document-type ``vouch`` step is not
# an accepted response variant; transaction-cycle work is one canonical
# registry-backed test definition and ordinary document questions retain their
# existing shape.
GENERATE_RESPONSE_CONTRACT = {
    "envelope": {"required": ["tests"], "additional_fields": False},
    "variants": {
        "data": {
            "required": ["source", "title", "objective", "steps"],
            "source": "data",
        },
        "document_question": {
            "required": ["source", "title", "objective", "steps"],
            "source": "document",
            "step_mode": "question",
        },
        "cycle_vouch": {
            "required": [
                "source",
                "kind",
                "title",
                "objective",
                "registry",
                "requirement_refs",
                "procedure_key",
                "definition",
            ],
            "source": "document",
            "kind": "cycle_vouch",
            "forbidden": ["steps", "checks", "document_types"],
        },
    },
}
GENERATE_SYSTEM = f"""[agent:test_generate]
Generate the complete executable tests for exactly one supplied RCM row.
Return JSON with a non-empty `tests` array. A test is one of:

1. Data Test: source `data`, title, objective, and non-empty `steps`; each step
   has label, instruction, and Polars code assigning exception rows to `result`.
2. Document question: source `document`, title, objective, and non-empty
   question-mode steps using only supplied document ids. A missing-evidence step
   has an empty document_ids array and a specific missing_evidence string.
3. Cycle Vouch: source `document`, kind `cycle_vouch`, title, objective,
   registry, requirement_refs, procedure_key, and definition. It has no steps.

For Cycle Vouch, the supplied `transaction_evidence` manifest is authoritative.
Choose an exact registry group and candidate_id, copy that candidate's table,
row_key, and cycle_keys exactly, and explain the selection. requirement_refs use
`<RCM id>:<control attribute key>` and cover the transaction-cycle attributes
the procedure tests. Group compatible assertions sharing one population and
lifecycle scope into one test. Roles name exact reachable record kinds and state
required, cardinality (one|many), and reuse_across_items (exclusive|allowed)
independently. Assertions use explicit row/role/roles operands and one of
{", ".join(sorted(cycle_vouching.OPERATORS))}; field selectors always name
group, kind, and attribute. Use numeric tolerance objects and integer day
tolerances. Do not invent identifiers, fields, mappings, roles, or literal row
values. Do not emit dotted paths, checks, document_types, or a vouch mode step.
Evidence-linked selection is targeted evidence only. Sampling uses random,
interval, or stratified with size 1..{cycle_vouching.MAX_ITEMS} and an integer
seed. assurance_scope is derived locally and may be omitted.

Keep non-cycle attributes independent of cycle vocabulary. A tabular attribute
normally produces a Data Test; document-content, inspection, inquiry, and mixed
attributes use the evidence that is actually supplied. One durable test has one
source. Use only supplied table/column/document ids. {JSON_RULES}"""

GENERATE_ROW_SOURCE_ID = "rcm_row"
GENERATE_METHODOLOGY_SOURCE_ID = "methodology"
GENERATE_TABLE_SOURCE_ID = "table_metadata"
GENERATE_DOCUMENT_SOURCE_ID = "documents"
GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID = "transaction_evidence"
_GENERATE_SOURCES = {"data", "document"}
_GENERATE_COMMON_FIELDS = ("source", "title", "objective", "steps")
_GENERATE_DATA_STEP_FIELDS = ("label", "instruction", "code")
_GENERATE_DOCUMENT_STEP_FIELDS = (
    "label", "instruction", "mode", "document_ids", "question", "checks",
    "missing_evidence", "scope_limitation", "anchor_table", "anchor_key",
    "document_roles",
)
_GENERATE_MODES = {"question"}
_UNKNOWN_COLUMN_ERROR_RE = re.compile(
    r"ColumnNotFoundError: unable to find column [\"']([^\"']+)[\"']"
)


def _generate_rcm_id(request: WorkerRequest) -> str:
    """Return the durable RCM id from the one supplied target-row item."""
    refs = [
        item.source_ref
        for item in request.context.items
        if item.source_id == GENERATE_ROW_SOURCE_ID
    ]
    if len(refs) != 1:
        raise WorkerContractError(
            "The test-generation context must supply exactly one target RCM row."
        )
    prefix, separator, rcm_id = str(refs[0]).partition(":")
    if prefix != "rcm" or not separator or not rcm_id:
        raise WorkerContractError(
            "The test-generation target row must be an 'rcm:<id>' reference."
        )
    return rcm_id


def _generate_methodology_refs(request: WorkerRequest) -> list[dict]:
    """Project the supplied methodology excerpts into durable citations."""
    refs = []
    for item in request.context.items:
        if item.source_id != GENERATE_METHODOLOGY_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            raise WorkerContractError(
                "Test-generation methodology context must supply citation objects."
            )
        refs.append(
            {key: content[key] for key in _METHODOLOGY_REF_FIELDS if key in content}
        )
    return refs


def _generate_supplied_tables(request: WorkerRequest) -> dict[str, dict[str, str]]:
    """Return supplied table schemas without loading workspace rows.

    The generation worker only receives declared context, never a workspace.  The
    dtype strings are enough to construct empty frames for a schema-only Polars
    validation pass below.
    """
    tables: dict[str, dict[str, str]] = {}
    for item in request.context.items:
        if item.source_id != GENERATE_TABLE_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            raise WorkerContractError("Table metadata context must supply objects.")
        name = str(content.get("table") or "").strip()
        if not name:
            continue
        tables[name] = {
            str(column.get("name") or ""): str(column.get("dtype") or "")
            for column in content.get("columns") or []
            if isinstance(column, Mapping) and str(column.get("name") or "")
        }
    return tables


def _empty_frame_dtype(dtype: str):
    """Map the compact context dtype into a safe, useful empty-frame dtype."""
    normalized = dtype.casefold().replace(" ", "")
    if normalized.startswith("uint"):
        return sandbox.pl.UInt64
    if normalized.startswith("int"):
        return sandbox.pl.Int64
    if normalized.startswith(("float", "decimal")):
        return sandbox.pl.Float64
    if normalized in {"bool", "boolean"}:
        return sandbox.pl.Boolean
    if normalized == "date":
        return sandbox.pl.Date
    if normalized.startswith("datetime"):
        return sandbox.pl.Datetime
    if normalized == "time":
        return sandbox.pl.Time
    return sandbox.pl.String


def _empty_schema_frames(tables: Mapping[str, Mapping[str, str]]) -> dict:
    """Build zero-row frames that preserve the supplied table schemas only."""
    return {
        table: sandbox.pl.DataFrame(
            schema={column: _empty_frame_dtype(dtype) for column, dtype in columns.items()}
        )
        for table, columns in tables.items()
    }


def _generate_supplied_document_ids(request: WorkerRequest) -> set[str]:
    ids = set()
    for item in request.context.items:
        if item.source_id != GENERATE_DOCUMENT_SOURCE_ID:
            continue
        prefix, separator, document_id = str(item.source_ref).partition(":")
        if prefix == "document" and separator and document_id:
            ids.add(document_id)
    return ids


def _generate_transaction_manifest(request: WorkerRequest) -> dict:
    value = _resolved_item(request, GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID)
    if not isinstance(value, Mapping):
        raise WorkerContractError(
            "Transaction-evidence context must supply one manifest object."
        )
    return _plain_json(value)


def _generate_rcm_row(request: WorkerRequest) -> dict:
    value = _resolved_item(request, GENERATE_ROW_SOURCE_ID)
    if not isinstance(value, Mapping):
        raise WorkerContractError("RCM-row context must supply one object.")
    return _plain_json(value)


def _validate_generate_cycle_test(
    path: str,
    value: Mapping[str, object],
    *,
    request: WorkerRequest,
    rcm_id: str,
    errors: list[str],
) -> dict | None:
    candidate = {
        **_plain_json(value),
        "source": "document",
        "kind": "cycle_vouch",
        "schema_version": cycle_vouching.SCHEMA_VERSION,
        "rcm_id": rcm_id,
        "steps": [],
    }
    rcm_row = _generate_rcm_row(request)
    manifest = _generate_transaction_manifest(request)
    try:
        validated = cycle_vouching.validate_cycle_test_semantics(
            candidate, rcm_row=rcm_row, manifest=manifest
        )
        group = cycle_vouching.manifest_group_for_test(validated, manifest)
        population = validated["definition"]["population"]
        selected = next(
            item
            for item in group["candidates"]
            if item["candidate_id"] == population["candidate_id"]
        )
        confirmation = (
            cycle_vouching.selection_confirmation(selected)
            if population["selection"].get("mode") == "evidence_linked"
            else None
        )
        if confirmation is not None:
            # The eligible reach exceeds the item cap, so the proposal carries
            # the deterministic sample the auditor confirms or adjusts at
            # approval. The confirmation travels with the test so the durable
            # record stays distinguishable from a freely chosen sample.
            population["selection"] = dict(confirmation["suggested_selection"])
            validated = cycle_vouching.validate_cycle_test_semantics(
                validated, rcm_row=rcm_row, manifest=manifest
            )
            validated["selection_confirmation"] = confirmation
    except (cycle_vouching.CycleSchemaError, StopIteration) as error:
        errors.append(f"{path} {error}")
        return None
    if not validated["definition"]["assertions"]:
        errors.append(f"{path}.definition.assertions must not be empty")
    return {
        "source": "document",
        "kind": "cycle_vouch",
        "title": str(value.get("title") or "").strip(),
        "objective": str(value.get("objective") or "").strip(),
        "registry": validated["registry"],
        "requirement_refs": validated["requirement_refs"],
        "procedure_key": validated["procedure_key"],
        "definition": validated["definition"],
        "context_manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "selection_confirmation": validated.get("selection_confirmation"),
    }


def _generate_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_payload(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    values = payload.get("tests")
    if not isinstance(values, list):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a `tests` array"
        )
    return {"tests": values}


def _validate_generate_data_step(
    path: str, raw_step: object, known_tables: Mapping[str, Mapping[str, str]], errors: list[str]
) -> dict | None:
    if not isinstance(raw_step, Mapping):
        errors.append(f"{path} must be an object")
        return None
    step = _plain_json(raw_step)
    foreign = [key for key in _GENERATE_DOCUMENT_STEP_FIELDS if key in step and key not in _GENERATE_DATA_STEP_FIELDS]
    if foreign:
        errors.append(f"{path} has document-only field '{foreign[0]}' on a data step")
    for key in ("label", "instruction"):
        if not isinstance(step.get(key), str) or not step[key].strip():
            errors.append(f"{path}.{key} must be a non-empty string")
    code = step.get("code")
    if not isinstance(code, str) or not code.strip():
        errors.append(f"{path}.code must be non-empty Polars code")
    else:
        safe_code = True
        try:
            sandbox.validate(code)
        except ValueError as error:
            errors.append(f"{path}.code is not allowed in the sandbox: {error}")
            safe_code = False
        if "result" not in code:
            errors.append(f"{path}.code must assign the exception rows to `result`")
        if safe_code and known_tables:
            try:
                # Use Polars itself to resolve schemas created by the snippet.
                # A flat source-column check cannot see aliases introduced by
                # joins (for example, ``ITEM_DESCRIPTION_right``), which are
                # valid only after the preceding join expression.  Frames have
                # no rows, so this validates schema and expression semantics
                # without reading or exposing table data.
                sandbox.run(code, _empty_schema_frames(known_tables))
            except sandbox.SandboxError as error:
                unknown_column = _UNKNOWN_COLUMN_ERROR_RE.search(str(error))
                if unknown_column:
                    errors.append(
                        f"{path}.code references unknown column '{unknown_column.group(1)}'"
                    )
                else:
                    errors.append(
                        f"{path}.code cannot run against the supplied table schemas: {error}"
                    )
    return {
        "label": str(step.get("label") or "").strip(),
        "instruction": str(step.get("instruction") or "").strip(),
        "code": str(code).strip() if isinstance(code, str) else "",
    }


def _validate_generate_document_step(
    path: str,
    raw_step: object,
    known_document_ids: set[str],
    errors: list[str],
) -> tuple[dict | None, str | None]:
    if not isinstance(raw_step, Mapping):
        errors.append(f"{path} must be an object")
        return None, None
    step = _plain_json(raw_step)
    foreign = [key for key in _GENERATE_DATA_STEP_FIELDS if key in step and key not in _GENERATE_DOCUMENT_STEP_FIELDS]
    if foreign:
        errors.append(f"{path} has data-only field '{foreign[0]}' on a document step")
    for key in ("label", "instruction"):
        if not isinstance(step.get(key), str) or not step[key].strip():
            errors.append(f"{path}.{key} must be a non-empty string")
    mode = step.get("mode")
    if mode == "vouch":
        errors.append(
            f"{path} uses the removed vouch-step schema; return a canonical "
            "cycle_vouch test definition"
        )
        return {
            "label": str(step.get("label") or "").strip(),
            "instruction": str(step.get("instruction") or "").strip(),
            "mode": "",
        }, None
    if mode not in _GENERATE_MODES:
        errors.append(f"{path}.mode must be 'question'")
        mode = None
    document_ids = step.get("document_ids")
    if document_ids is None:
        document_ids = []
    if isinstance(document_ids, str):
        document_ids = [document_ids]
    if not isinstance(document_ids, (list, tuple)):
        errors.append(f"{path}.document_ids must be an array")
        document_ids = []
    document_ids = [str(value) for value in document_ids]
    unknown = [value for value in document_ids if value not in known_document_ids]
    if unknown:
        errors.append(f"{path} references unknown document '{unknown[0]}'")
    question = str(step.get("question") or "").strip()
    missing_evidence = str(step.get("missing_evidence") or "").strip()
    scope_limitation = str(step.get("scope_limitation") or "").strip()
    normalized = {
        "label": str(step.get("label") or "").strip(),
        "instruction": str(step.get("instruction") or "").strip(),
        "mode": mode or "",
    }
    if mode == "question":
        if not question:
            errors.append(f"{path} needs a question")
        if step.get("checks"):
            errors.append(f"{path} has checks but mode is 'question'")
        for key in ("anchor_table", "anchor_key", "document_roles"):
            if step.get(key):
                errors.append(f"{path} has vouch-only field '{key}' on a question step")
    # Older prompts caused the model to use `missing_evidence` for a useful but
    # different statement: documents were reviewed, while some additional
    # evidence remained unavailable. Preserve that meaning as the explicit
    # sourced-question scope limitation instead of spending a repair turn on a
    # harmless field-name mismatch.
    if document_ids and missing_evidence:
        if not scope_limitation:
            scope_limitation = missing_evidence
        missing_evidence = ""
    if not document_ids and not missing_evidence:
        errors.append(f"{path} has no documents; missing_evidence must name what is required")
    normalized.update(
        document_ids=document_ids,
        missing_evidence=missing_evidence,
    )
    if scope_limitation:
        normalized["scope_limitation"] = scope_limitation
    if mode == "question":
        normalized["question"] = question
    return normalized, mode


def validate_generate_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the merged generation quality gate for one RCM row.

    Every violation across every proposed test and step is collected so one
    bounded repair turn can correct the complete response, per the merge
    plan's worker contract (section 4).
    """
    values = proposal.get("tests")
    if not isinstance(values, (list, tuple)) or not values:
        raise WorkerResponseValidationError("tests must be a non-empty array")
    rcm_id = _generate_rcm_id(request)
    methodology_refs = _generate_methodology_refs(request)
    known_tables = _generate_supplied_tables(request)
    known_document_ids = _generate_supplied_document_ids(request)
    available = {
        "data": bool(known_tables),
        "document": any(
            item.source_id == GENERATE_DOCUMENT_SOURCE_ID for item in request.context.items
        ),
    }
    errors: list[str] = []
    normalized: list[dict] = []
    cycle_identities: set[tuple[str, str, str, str]] = set()
    for index, raw in enumerate(values, 1):
        path = f"tests[{index - 1}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{path} must be an object")
            continue
        value = _plain_json(raw)
        source = value.get("source")
        if source not in _GENERATE_SOURCES:
            errors.append(f"{path}.source must be 'data' or 'document'")
            continue
        if not available[source]:
            errors.append(
                f"{path}.source is '{source}' but no "
                f"{'table' if source == 'data' else 'document'} is available; "
                "use the other source or return no test for this row"
            )
        for key in ("title", "objective"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                errors.append(f"{path}.{key} must be a non-empty string")
        if source == "document" and value.get("kind") == "cycle_vouch":
            if value.get("steps") not in (None, []):
                errors.append(f"{path} cycle_vouch must not carry steps")
            cycle_test = _validate_generate_cycle_test(
                path, value, request=request, rcm_id=rcm_id, errors=errors
            )
            if cycle_test is not None:
                population = cycle_test["definition"]["population"]
                identity = (
                    cycle_test["procedure_key"],
                    population["table"],
                    population["row_key"]["column"],
                    population["row_key"]["identifier_kind"],
                )
                if identity in cycle_identities:
                    errors.append(
                        f"{path} duplicates a cycle procedure/population; group "
                        "its compatible assertions into one test"
                    )
                cycle_identities.add(identity)
                normalized.append(
                    {
                        **cycle_test,
                        "rcm_id": rcm_id,
                        "methodology_refs": methodology_refs,
                    }
                )
            continue
        if value.get("kind") is not None:
            errors.append(f"{path}.kind is valid only for cycle_vouch")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
            errors.append(f"{path}.steps must be a non-empty array")
            continue
        steps: list[dict] = []
        if source == "data":
            for step_index, raw_step in enumerate(raw_steps):
                step_path = f"{path}.steps[{step_index}]"
                normalized_step = _validate_generate_data_step(step_path, raw_step, known_tables, errors)
                if normalized_step is not None:
                    steps.append(normalized_step)
        else:
            modes: set[str] = set()
            for step_index, raw_step in enumerate(raw_steps):
                step_path = f"{path}.steps[{step_index}]"
                normalized_step, mode = _validate_generate_document_step(
                    step_path,
                    raw_step,
                    known_document_ids,
                    errors,
                )
                if normalized_step is not None:
                    steps.append(normalized_step)
                if mode:
                    modes.add(mode)
            if len(modes) > 1:
                errors.append(f"{path} mixes document modes {sorted(modes)}; return separate tests")
            # One vouch step is one cycle definition over one population. Two of
            # them in a single test would be two populations sharing one record's
            # items, conclusion, and coverage figures.
            if modes == {"vouch"} and len(raw_steps) > 1:
                errors.append(
                    f"{path} has {len(raw_steps)} vouch steps; a vouch test is one "
                    "cycle plan, so return separate tests"
                )
        normalized.append(
            {
                "source": source,
                "title": str(value.get("title") or "").strip(),
                "objective": str(value.get("objective") or "").strip(),
                "steps": steps,
                "rcm_id": rcm_id,
                "methodology_refs": methodology_refs,
            }
        )
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"tests": normalized}


def run_generate_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        _generation_prompt_payload(request),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    conversation = None
    if attempt.is_repair:
        if attempt.previous_response is None:
            raise WorkerContractError(
                "A test-generation repair requires the previous response."
            )
        conversation = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": attempt.previous_response},
            {"role": "user", "content": _repair_instruction(attempt)},
        ]
    return gateway.complete(
        GENERATE_SYSTEM,
        user,
        _context_metrics(request, "test_generation"),
        attempt=attempt.number,
        conversation=conversation,
    )


GENERATE_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="tests.generate.response",
    schema_hash=_sha256_text(
        json.dumps(
            GENERATE_RESPONSE_CONTRACT,
            sort_keys=True,
            separators=(",", ":"),
        )
    ),
    validator=_generate_response_schema,
)
GENERATE_WORKER = WorkerDefinition(
    worker_id=GENERATE_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_generate_worker)),
    prompt_hash=_sha256_text(GENERATE_SYSTEM),
    response_schema=GENERATE_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair generated test contract violations against the supplied RCM row."
        ),
    ),
    implementation=run_generate_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_generate_proposal)
        + inspect.getsource(_validate_generate_cycle_test)
        + inspect.getsource(cycle_vouching.validate_cycle_test_semantics)
    ),
    semantic_validator=validate_generate_proposal,
)

WORKERS.register(GENERATE_WORKER)


__all__ = [
    "DEFAULT_COMPARISON_METHOD",
    "GENERATE_RESPONSE_SCHEMA",
    "GENERATE_RESPONSE_CONTRACT",
    "GENERATE_SYSTEM",
    "GENERATE_WORKER",
    "GENERATE_WORKER_ID",
    "run_generate_worker",
    "validate_generate_proposal",
]
