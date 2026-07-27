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

from ... import doc_tests, sandbox
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


def _repair_suffix(attempt: WorkerAttempt) -> str:
    if not attempt.is_repair:
        return ""
    return (
        "\n\nYour previous response could not be used: "
        + "; ".join(attempt.validation_errors)
        + ". Return a complete corrected JSON object."
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
GENERATE_SYSTEM = f"""[agent:test_generate]
Generate the complete, executable tests that cover exactly one supplied RCM
row. Return an object with `tests`, a discriminated array. Each test has
exactly these fields, all required:
  source      "data" if the test is answered by analysing an imported table,
              "document" if it is answered by reading documents
  title       short name for the test
  objective   what the test establishes about the control
  steps       array of step objects, the complete executable procedure

One durable test has one source. If a row needs both data analysis and
document inspection, return two tests — one "data" and one "document". A
Document Test has one execution mode; if a row needs both a question and a
comparison, return two Document Tests.

A "data" test's steps are Polars only. Each step is an object:
  label         short name for the step
  instruction   what the step determines
  table_refs    array of exact table names the step's code reads
  code          Polars code assigning the exception rows to `result`
Each supplied table is available as a DataFrame variable under its exact
name, and `pl` is the Polars module. `result` must be a DataFrame holding the
rows that fail the step — an empty result means no exception. Use exact
column and table names from the supplied schemas. No imports, no file or
network access, and no printing.

A "document" test's steps use only "question" or "vouch" mode, the same mode
on every step in one test. Each step is an object:
  label             short name for the step
  instruction       what the step determines
  mode              "question" or "vouch"
  document_ids      array of exact document ids the step reads
  question          non-empty only in "question" mode
  checks            array of {{field, expected}}, non-empty only in "vouch"
                    mode
  missing_evidence  non-empty only when document_ids is empty, naming the
                     specific evidence required
Use only document ids from the supplied context. If the required evidence is
missing, still return a concrete step with an empty document_ids array and a
specific missing_evidence string — never invent a document id.

Choose each test's source from what is actually available in the supplied
tables and documents. Add no other fields. {JSON_RULES}"""

GENERATE_ROW_SOURCE_ID = "rcm_row"
GENERATE_METHODOLOGY_SOURCE_ID = "methodology"
GENERATE_TABLE_SOURCE_ID = "table_metadata"
GENERATE_DOCUMENT_SOURCE_ID = "documents"
_GENERATE_SOURCES = {"data", "document"}
_GENERATE_COMMON_FIELDS = ("source", "title", "objective", "steps")
_GENERATE_DATA_STEP_FIELDS = ("label", "instruction", "table_refs", "code")
_GENERATE_DOCUMENT_STEP_FIELDS = (
    "label", "instruction", "mode", "document_ids", "question", "checks",
    "missing_evidence",
)
_GENERATE_MODES = {"question", "vouch"}
_COLUMN_REF_RE = re.compile(r"pl\.col\(\s*['\"]([^'\"]+)['\"]\s*\)")


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


def _generate_supplied_tables(request: WorkerRequest) -> dict[str, set[str]]:
    """Return the supplied table names and their exact column spellings."""
    tables: dict[str, set[str]] = {}
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
            str(column.get("name") or "")
            for column in content.get("columns") or []
            if isinstance(column, Mapping)
        }
    return tables


def _generate_supplied_document_ids(request: WorkerRequest) -> set[str]:
    ids = set()
    for item in request.context.items:
        if item.source_id != GENERATE_DOCUMENT_SOURCE_ID:
            continue
        prefix, separator, document_id = str(item.source_ref).partition(":")
        if prefix == "document" and separator and document_id:
            ids.add(document_id)
    return ids


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
    path: str, raw_step: object, known_tables: dict[str, set[str]], errors: list[str]
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
    refs = step.get("table_refs")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)) or not refs:
        errors.append(f"{path}.table_refs must be a non-empty array of table names")
        refs = []
    refs = [str(ref) for ref in refs]
    for ref in refs:
        if ref not in known_tables:
            errors.append(f"{path}.table_refs references unknown table '{ref}'")
    code = step.get("code")
    known_columns: set[str] = set()
    for ref in refs:
        known_columns |= known_tables.get(ref, set())
    if not isinstance(code, str) or not code.strip():
        errors.append(f"{path}.code must be non-empty Polars code")
    else:
        try:
            sandbox.validate(code)
        except ValueError as error:
            errors.append(f"{path}.code is not allowed in the sandbox: {error}")
        if "result" not in code:
            errors.append(f"{path}.code must assign the exception rows to `result`")
        if known_columns:
            unknown_columns = sorted(set(_COLUMN_REF_RE.findall(code)) - known_columns)
            if unknown_columns:
                errors.append(f"{path}.code references unknown column '{unknown_columns[0]}'")
    return {
        "label": str(step.get("label") or "").strip(),
        "instruction": str(step.get("instruction") or "").strip(),
        "table_refs": refs,
        "code": str(code).strip() if isinstance(code, str) else "",
    }


def _validate_generate_document_step(
    path: str, raw_step: object, known_document_ids: set[str], errors: list[str]
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
    if mode not in _GENERATE_MODES:
        errors.append(f"{path}.mode must be 'question' or 'vouch'")
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
    checks = step.get("checks")
    missing_evidence = str(step.get("missing_evidence") or "").strip()
    if mode == "question":
        if not question:
            errors.append(f"{path} needs a question")
        if checks:
            errors.append(f"{path} has checks but mode is 'question'")
        checks = []
    elif mode == "vouch":
        if question:
            errors.append(f"{path} has a question but mode is 'vouch'")
        if not isinstance(checks, (list, tuple)) or not checks:
            errors.append(f"{path} needs comparison checks")
            checks = []
        else:
            normalized_checks = []
            for check_index, check in enumerate(checks, 1):
                if not isinstance(check, Mapping) or not str(check.get("field") or "").strip():
                    errors.append(f"{path}.checks[{check_index - 1}] needs a field")
                    continue
                normalized_checks.append(
                    {"field": str(check["field"]).strip(), "expected": str(check.get("expected") or "")}
                )
            checks = normalized_checks
        question = ""
    else:
        checks = []
    if not document_ids and not missing_evidence:
        errors.append(f"{path} has no documents; missing_evidence must name what is required")
    if document_ids and missing_evidence:
        errors.append(f"{path} has documents but also claims missing_evidence")
    normalized = {
        "label": str(step.get("label") or "").strip(),
        "instruction": str(step.get("instruction") or "").strip(),
        "mode": mode or "",
        "document_ids": document_ids,
        "missing_evidence": missing_evidence,
    }
    if mode == "question":
        normalized["question"] = question
    elif mode == "vouch":
        normalized["checks"] = checks
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
                    step_path, raw_step, known_document_ids, errors
                )
                if normalized_step is not None:
                    steps.append(normalized_step)
                if mode:
                    modes.add(mode)
            if len(modes) > 1:
                errors.append(f"{path} mixes document modes {sorted(modes)}; return separate tests")
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
        {
            "TARGET RCM ROW": _resolved_item(request, GENERATE_ROW_SOURCE_ID),
            "RESOLVED CONTEXT": request.context.to_dict(),
            "INSTRUCTIONS": (
                "Generate the complete executable tests for the target RCM row "
                "only. Do not duplicate a test already covering another RCM row "
                "in the supplied context."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    return gateway.complete(
        GENERATE_SYSTEM,
        user + _repair_suffix(attempt),
        _context_metrics(request, "test_generation"),
        attempt=attempt.number,
    )


GENERATE_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="tests.generate.response",
    schema_hash=_sha256_text("test-generate-response:json-object-with-tests-array"),
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
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_generate_proposal)),
    semantic_validator=validate_generate_proposal,
)

WORKERS.register(GENERATE_WORKER)


__all__ = [
    "DEFAULT_COMPARISON_METHOD",
    "GENERATE_RESPONSE_SCHEMA",
    "GENERATE_SYSTEM",
    "GENERATE_WORKER",
    "GENERATE_WORKER_ID",
    "run_generate_worker",
    "validate_generate_proposal",
]
