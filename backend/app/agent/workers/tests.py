"""Registered model workers for the test capability group.

Three workers, two passes. ``tests.draft`` turns one RCM row into the audit plan
for each test that covers it and chooses each test's single source.
``tests.data_spec`` and ``tests.document_spec`` then write the executable part
onto one already-drafted record.

Every contract here is deliberately small. The plan is six flat fields with no
nested objects; a generated Data Test is Polars code against named tables; a
generated Document Test is one of two item shapes. Larger contracts were tried
and produced worse proposals: a discriminated union over four document-test item
kinds and three data-test engines made the model pick a shape and then fill it
badly, and two registry payloads worth ~14,000 characters crowded the actual
engagement material out of the prompt.

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


# --------------------------------------------------------------------------- #
# tests.draft worker
# --------------------------------------------------------------------------- #
DRAFT_WORKER_ID = "tests.draft"
DRAFT_SYSTEM = f"""[agent:test_plan]
Plan the tests that cover exactly one supplied RCM row. Return an object with
`tests`. Each test has exactly these fields, all required:
  title              short name for the test
  objective          what the test establishes about the control
  criteria           what result would mean the control operated
  steps              array of non-empty strings, the procedure to perform
  source             "data" if the test is answered by analysing an imported
                     table, "document" if it is answered by reading documents
  expected_evidence  what the test should produce as evidence
One test has one source. If a control needs both data analysis and document
inspection, return two tests. Choose the source from what is actually available
in the supplied tables and documents. Add no other fields. {JSON_RULES}"""

DRAFT_ROW_SOURCE_ID = "rcm_row"
DRAFT_METHODOLOGY_SOURCE_ID = "methodology"
DRAFT_TABLE_SOURCE_ID = "table_metadata"
DRAFT_DOCUMENT_SOURCE_ID = "documents"
# The citation fields a committed test persists in ``methodology_refs``.
_METHODOLOGY_REF_FIELDS = (
    "pack_id",
    "pack_name",
    "version",
    "sha1",
    "section",
    "citation",
)
_SOURCES = {"data", "document"}
_DRAFT_FIELDS = ("title", "objective", "criteria", "source", "expected_evidence")


def _draft_rcm_id(request: WorkerRequest) -> str:
    """Return the durable RCM id from the one supplied target-row item."""
    refs = [
        item.source_ref
        for item in request.context.items
        if item.source_id == DRAFT_ROW_SOURCE_ID
    ]
    if len(refs) != 1:
        raise WorkerContractError(
            "The test-draft context must supply exactly one target RCM row."
        )
    prefix, separator, rcm_id = str(refs[0]).partition(":")
    if prefix != "rcm" or not separator or not rcm_id:
        raise WorkerContractError(
            "The test-draft target row must be an 'rcm:<id>' reference."
        )
    return rcm_id


def _methodology_refs(request: WorkerRequest) -> list[dict]:
    """Project the supplied methodology excerpts into durable citations."""
    refs = []
    for item in request.context.items:
        if item.source_id != DRAFT_METHODOLOGY_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            raise WorkerContractError(
                "Test-draft methodology context must supply citation objects."
            )
        refs.append(
            {key: content[key] for key in _METHODOLOGY_REF_FIELDS if key in content}
        )
    return refs


def _draft_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_payload(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    values = payload.get("tests")
    if not isinstance(values, list):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a `tests` array"
        )
    return {"tests": values}


def validate_draft_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the test-plan quality gate for one RCM row.

    Every violation across every proposed test is collected so one bounded repair
    turn can correct them together.
    """
    values = proposal.get("tests")
    if not isinstance(values, (list, tuple)) or not values:
        raise WorkerResponseValidationError("tests must be a non-empty array")
    rcm_id = _draft_rcm_id(request)
    methodology_refs = _methodology_refs(request)
    # A test can only be specified against material the workspace actually has,
    # so a source with nothing behind it is a contract violation the model can
    # fix now rather than a unit that fails two stages later.
    available = {
        "data": any(
            item.source_id == DRAFT_TABLE_SOURCE_ID for item in request.context.items
        ),
        "document": any(
            item.source_id == DRAFT_DOCUMENT_SOURCE_ID for item in request.context.items
        ),
    }
    normalized: list[dict] = []
    errors: list[str] = []
    for index, raw in enumerate(values, 1):
        path = f"tests[{index - 1}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{path} must be an object")
            continue
        value = _plain_json(raw)
        errors.extend(
            f"{path}.{key} must be a non-empty string"
            for key in _DRAFT_FIELDS
            if not isinstance(value.get(key), str) or not value[key].strip()
        )
        source = value.get("source")
        if source not in _SOURCES:
            errors.append(f"{path}.source must be 'data' or 'document'")
        elif not available[source]:
            errors.append(
                f"{path}.source is '{source}' but no "
                f"{'table' if source == 'data' else 'document'} is available; "
                "use the other source or return no test for this row"
            )
        steps = value.get("steps")
        if not isinstance(steps, list):
            errors.append(f"{path}.steps expected an array")
        else:
            cleaned = [str(item).strip() for item in steps]
            if not cleaned or any(not item for item in cleaned):
                errors.append(f"{path}.steps array must contain non-empty strings")
            else:
                value["steps"] = cleaned
        value["rcm_id"] = rcm_id
        value.setdefault("methodology_refs", methodology_refs)
        normalized.append(value)
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"tests": normalized}


def run_draft_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {
            "TARGET RCM ROW": _resolved_item(request, DRAFT_ROW_SOURCE_ID),
            "RESOLVED CONTEXT": request.context.to_dict(),
            "INSTRUCTIONS": (
                "Plan the tests for the target RCM row only. Do not duplicate a "
                "test already covering another RCM row in the supplied context."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    return gateway.complete(
        DRAFT_SYSTEM,
        user + _repair_suffix(attempt),
        _context_metrics(request, "test_draft"),
        attempt=attempt.number,
    )


DRAFT_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="tests.draft.response",
    schema_hash=_sha256_text("test-draft-response:json-object-with-tests-array"),
    validator=_draft_response_schema,
)
DRAFT_WORKER = WorkerDefinition(
    worker_id=DRAFT_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_draft_worker)),
    prompt_hash=_sha256_text(DRAFT_SYSTEM),
    response_schema=DRAFT_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair test-plan contract violations against the supplied RCM row."
        ),
    ),
    implementation=run_draft_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_draft_proposal)),
    semantic_validator=validate_draft_proposal,
)

WORKERS.register(DRAFT_WORKER)


# --------------------------------------------------------------------------- #
# tests.data_spec worker
# --------------------------------------------------------------------------- #
DATA_SPEC_WORKER_ID = "tests.data_spec"
DATA_SPEC_SYSTEM = f"""[agent:data_test_spec]
Write the Polars code that performs one already-planned Data Test. Return an
object with exactly these fields:
  table_refs  array of exact table names the code reads
  code        Polars code assigning the exception rows to `result`
Each supplied table is available as a DataFrame variable under its exact name,
and `pl` is the Polars module. `result` must be a DataFrame holding the rows
that fail the test — an empty result means the control operated. Use exact
column names from the supplied schemas. No imports, no file or network access,
and no printing. {JSON_RULES}"""

SPEC_ROW_SOURCE_ID = "rcm_row"
SPEC_TEST_SOURCE_ID = "test"
SPEC_TABLE_SOURCE_ID = "table_metadata"


def _supplied_tables(request: WorkerRequest) -> dict[str, set[str]]:
    """Return the supplied table names and their exact column spellings."""
    tables: dict[str, set[str]] = {}
    for item in request.context.items:
        if item.source_id != SPEC_TABLE_SOURCE_ID:
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


def _data_spec_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_payload(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    # A model that wraps the two fields in the object name it was shown is
    # returning the right content in a harmless shape; unwrap rather than repair.
    inner = payload.get("data_test")
    if isinstance(inner, Mapping):
        payload = dict(inner)
    return {
        "table_refs": payload.get("table_refs"),
        "code": payload.get("code"),
    }


def validate_data_spec_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Check what the supplied context and the sandbox can decide.

    Frame-dependent execution stays with the registered executor, which owns the
    workspace frames; this gate covers the shape, the exact table spellings, and
    the sandbox's static safety analysis — the three things a repair turn can
    actually act on.
    """
    errors: list[str] = []
    refs = proposal.get("table_refs")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)) or not refs:
        errors.append("table_refs must be a non-empty array of table names")
        refs = []
    tables = _supplied_tables(request)
    for ref in refs:
        if str(ref) not in tables:
            errors.append(f"table_refs references unknown table '{ref}'")
    code = proposal.get("code")
    if not isinstance(code, str) or not code.strip():
        errors.append("code must be non-empty Polars code")
    else:
        try:
            sandbox.validate(code)
        except ValueError as error:
            errors.append(f"code is not allowed in the sandbox: {error}")
        if "result" not in code:
            errors.append("code must assign the exception rows to `result`")
    if errors:
        raise WorkerResponseValidationError(errors)
    return {
        "engine": "polars",
        "table_refs": [str(ref) for ref in refs],
        "spec": {"code": str(code).strip()},
    }


def run_data_spec_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {
            "RCM ROW": _resolved_item(request, SPEC_ROW_SOURCE_ID),
            "TEST TO IMPLEMENT": _resolved_item(request, SPEC_TEST_SOURCE_ID),
            "RESOLVED CONTEXT": request.context.to_dict(),
        },
        indent=1,
        ensure_ascii=False,
    )
    return gateway.complete(
        DATA_SPEC_SYSTEM,
        user + _repair_suffix(attempt),
        _context_metrics(request, "data_test_spec"),
        attempt=attempt.number,
    )


DATA_SPEC_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="tests.data_spec.response",
    schema_hash=_sha256_text("data-spec-response:json-object-with-tables-and-code"),
    validator=_data_spec_response_schema,
)
DATA_SPEC_WORKER = WorkerDefinition(
    worker_id=DATA_SPEC_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_data_spec_worker)),
    prompt_hash=_sha256_text(DATA_SPEC_SYSTEM),
    response_schema=DATA_SPEC_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair generated Polars against the supplied schemas and the sandbox."
        ),
    ),
    implementation=run_data_spec_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_data_spec_proposal)
    ),
    semantic_validator=validate_data_spec_proposal,
)

WORKERS.register(DATA_SPEC_WORKER)


# --------------------------------------------------------------------------- #
# tests.document_spec worker
# --------------------------------------------------------------------------- #
DOCUMENT_SPEC_WORKER_ID = "tests.document_spec"
DOCUMENT_SPEC_SYSTEM = f"""[agent:document_test_spec]
Write the items that perform one already-planned Document Test. Return an object
with exactly these fields:
  mode             "question" to answer questions from the attached documents,
                   "vouch" to compare expected values against them
  items            array of objects, each with a label and its exact
                   document_ids. A "question" item also has a question string.
                   A "vouch" item also has checks: objects with field and
                   expected.
  missing_evidence optional string; set it only when the required documents are
                   not in the supplied set, and name what is needed
Use only document ids from the supplied context. If the evidence is missing,
still return concrete items with an empty document_ids array so the request for
that evidence is specific. {JSON_RULES}"""

SPEC_DOCUMENT_SOURCE_ID = "documents"
_MODES = {"question": "qa", "vouch": "vouching"}


def _supplied_document_ids(request: WorkerRequest) -> set[str]:
    ids = set()
    for item in request.context.items:
        if item.source_id != SPEC_DOCUMENT_SOURCE_ID:
            continue
        prefix, separator, document_id = str(item.source_ref).partition(":")
        if prefix == "document" and separator and document_id:
            ids.add(document_id)
    return ids


def _document_spec_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_payload(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    inner = payload.get("document_test")
    if isinstance(inner, Mapping):
        payload = dict(inner)
    return {
        "mode": payload.get("mode"),
        "items": payload.get("items"),
        "missing_evidence": payload.get("missing_evidence"),
    }


def validate_document_spec_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Check the item contract the supplied context can decide.

    Document identity is checked against the documents actually supplied in the
    bundle rather than the workspace, so the worker stays bundle-only while the
    executor keeps the authoritative durable-write validation.
    """
    errors: list[str] = []
    mode = str(proposal.get("mode") or "")
    if mode not in _MODES:
        errors.append("mode must be 'question' or 'vouch'")
    items = proposal.get("items")
    if not isinstance(items, (list, tuple)) or not items:
        errors.append("items must be a non-empty array")
        items = []
    known = _supplied_document_ids(request)
    normalized: list[dict] = []
    for index, raw in enumerate(items, 1):
        path = f"items[{index - 1}]"
        if not isinstance(raw, Mapping) or not str(raw.get("label") or "").strip():
            errors.append(f"{path} needs a label")
            continue
        item = _plain_json(raw)
        unknown = [
            str(document_id)
            for document_id in item.get("document_ids") or []
            if str(document_id) not in known
        ]
        if unknown:
            errors.append(f"{path} references unknown document '{unknown[0]}'")
        if mode == "question" and not str(item.get("question") or "").strip():
            errors.append(f"{path} needs a question")
        elif mode == "vouch":
            checks = item.get("checks")
            if not isinstance(checks, (list, tuple)) or not checks:
                errors.append(f"{path} needs comparison checks")
            else:
                for check_index, check in enumerate(checks, 1):
                    if not isinstance(check, Mapping):
                        errors.append(f"{path}.checks[{check_index - 1}] must be an object")
                    elif not str(check.get("field") or "").strip():
                        errors.append(f"{path}.checks[{check_index - 1}] needs a field")
        normalized.append(item)
    if errors:
        raise WorkerResponseValidationError(errors)
    return {
        "kind": _MODES[mode],
        "items": normalized,
        "missing_evidence": str(proposal.get("missing_evidence") or ""),
    }


def run_document_spec_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {
            "RCM ROW": _resolved_item(request, SPEC_ROW_SOURCE_ID),
            "TEST TO IMPLEMENT": _resolved_item(request, SPEC_TEST_SOURCE_ID),
            "RESOLVED CONTEXT": request.context.to_dict(),
            "COMPARISON DEFAULT": (
                "checks are compared with normalized text matching unless the "
                "auditor changes the method later"
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    return gateway.complete(
        DOCUMENT_SPEC_SYSTEM,
        user + _repair_suffix(attempt),
        _context_metrics(request, "document_test_spec"),
        attempt=attempt.number,
    )


DOCUMENT_SPEC_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="tests.document_spec.response",
    schema_hash=_sha256_text("document-spec-response:json-object-with-mode-and-items"),
    validator=_document_spec_response_schema,
)
DOCUMENT_SPEC_WORKER = WorkerDefinition(
    worker_id=DOCUMENT_SPEC_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_document_spec_worker)),
    prompt_hash=_sha256_text(DOCUMENT_SPEC_SYSTEM),
    response_schema=DOCUMENT_SPEC_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair Document Test items against the supplied documents."
        ),
    ),
    implementation=run_document_spec_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_document_spec_proposal)
    ),
    semantic_validator=validate_document_spec_proposal,
)

WORKERS.register(DOCUMENT_SPEC_WORKER)

# Comparison methods stay the auditor's to change; generation always uses the
# established default rather than asking the model to pick one.
DEFAULT_COMPARISON_METHOD = "normalized"
assert DEFAULT_COMPARISON_METHOD in doc_tests.METHODS


__all__ = [
    "DATA_SPEC_RESPONSE_SCHEMA",
    "DATA_SPEC_SYSTEM",
    "DATA_SPEC_WORKER",
    "DATA_SPEC_WORKER_ID",
    "DEFAULT_COMPARISON_METHOD",
    "DOCUMENT_SPEC_RESPONSE_SCHEMA",
    "DOCUMENT_SPEC_SYSTEM",
    "DOCUMENT_SPEC_WORKER",
    "DOCUMENT_SPEC_WORKER_ID",
    "DRAFT_RESPONSE_SCHEMA",
    "DRAFT_SYSTEM",
    "DRAFT_WORKER",
    "DRAFT_WORKER_ID",
    "run_data_spec_worker",
    "run_document_spec_worker",
    "run_draft_worker",
    "validate_data_spec_proposal",
    "validate_document_spec_proposal",
    "validate_draft_proposal",
]
