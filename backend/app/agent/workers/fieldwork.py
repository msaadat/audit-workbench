"""Registered model workers for audit fieldwork capabilities.

The two definition workers translate one planned test into an executable durable
test; the document Q&A worker answers one attached document's question from the
pages it was supplied. All three own their prompt, their bounded
bundle-to-message transformation, and the part of their contract that can be
decided from the supplied context: response shape, enum membership, and
reference existence against the supplied table schemas, documents, pages, and
the application's own test catalogs.

The authoritative, data-dependent validation (analytics parameter
canonicalization, validation-rule resolution, Polars sandbox checks) stays with
the registered executor, which owns the workspace frames. Importing the
``analytics``/``validation`` registry payloads here is deliberate: they are
application catalogs rather than engagement content, so they belong to the
response contract the worker enforces, not to the declared engagement context.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import analytics, doc_tests, validation
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


DATA_TEST_SPEC_WORKER_ID = "fieldwork.data_test_spec"
DATA_TEST_SPEC_SYSTEM = f"""[agent:data_test_spec]
Translate one planned test into exactly one executable durable Data Test.
Return an object with data_test containing title, objective, engine
(analytics|validation|polars), table_refs, and spec. Use exact table and column
names and only supplied registry IDs. For analytics, spec contains test_id and
params; test_id is an analytics_registry id, never the planned-test id. For
validation, spec contains non-empty rules shaped as objects with check (a
validation_registry id), optional column, and params; SQL is not supported.
For polars, spec contains safe Polars code assigning a DataFrame to result;
imports and filesystem or network access are forbidden. Do not return document
work. {JSON_RULES}"""

DATA_TEST_ROW_SOURCE_ID = "rcm_row"
DATA_TEST_PLANNED_SOURCE_ID = "planned_test"
DATA_TEST_TABLE_SOURCE_ID = "table_metadata"
_DATA_TEST_ENGINES = {"analytics", "validation", "polars"}


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


def _supplied_tables(request: WorkerRequest) -> dict[str, set[str]]:
    """Return the supplied table names and their exact column spellings."""
    tables: dict[str, set[str]] = {}
    for item in request.context.items:
        if item.source_id != DATA_TEST_TABLE_SOURCE_ID:
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


def _json_object(response: str, key: str) -> Mapping[str, Any]:
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
    if not isinstance(payload, dict) or not isinstance(payload.get(key), dict):
        raise WorkerResponseValidationError(
            f"the response must be a JSON object with a `{key}` object"
        )
    return {key: payload[key]}


def _data_test_response_schema(response: str) -> Mapping[str, Any]:
    return _json_object(response, "data_test")


def validate_data_test_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Check the definition contract that the supplied context can decide.

    Frame-dependent canonicalization (analytics parameter defaults, validation
    rule resolution, Polars sandbox analysis) is the executor's authoritative
    step; this gate covers the shape, the enums, the registry IDs, the exact
    table/column spellings, and the planned test's required execution engine.
    """
    value = proposal.get("data_test")
    if not isinstance(value, Mapping):
        raise WorkerResponseValidationError("data_test must be an object")
    definition = _plain_json(value)
    errors: list[str] = []
    for key in ("title", "objective"):
        if not str(definition.get(key) or "").strip():
            errors.append(f"data_test.{key} must be a non-empty string")
    engine = str(definition.get("engine") or "").strip().lower()
    if engine not in _DATA_TEST_ENGINES:
        errors.append("data_test.engine is unsupported")
    definition["engine"] = engine
    tables = _supplied_tables(request)
    refs = definition.get("table_refs")
    if not isinstance(refs, list) or not refs:
        errors.append("data_test.table_refs must be non-empty")
        refs = []
    for ref in refs:
        if str(ref) not in tables:
            errors.append(f"data_test.table_refs references unknown table '{ref}'")
    spec = definition.get("spec")
    if not isinstance(spec, Mapping):
        errors.append("data_test.spec must be an object")
        spec = {}
    columns = {column for ref in refs for column in tables.get(str(ref), set())}
    if engine == "analytics":
        test_id = analytics.canonical_test_id(spec.get("test_id"))
        if test_id not in analytics.ANALYTICS:
            errors.append(f"data_test.spec.test_id '{test_id}' is not a registry ID")
        params = spec.get("params")
        if params is not None and not isinstance(params, Mapping):
            errors.append("data_test.spec.params must be an object")
        elif params:
            errors.extend(
                f"data_test.spec.params references unknown column '{name}'"
                for name in _referenced_columns(params)
                if columns and name not in columns
            )
    elif engine == "validation":
        rules = spec.get("rules")
        if not isinstance(rules, list) or not rules:
            errors.append("data_test.spec.rules must be a non-empty array")
        else:
            for index, rule in enumerate(rules):
                path = f"data_test.spec.rules[{index}]"
                if not isinstance(rule, Mapping):
                    errors.append(f"{path} must be an object")
                    continue
                if str(rule.get("check") or "") not in validation.CHECKS:
                    errors.append(f"{path}.check is not a validation registry ID")
                column = rule.get("column")
                if column is not None and columns and str(column) not in columns:
                    errors.append(f"{path}.column '{column}' is not a supplied column")
    elif engine == "polars" and not str(spec.get("code") or "").strip():
        errors.append("data_test.spec.code must be non-empty Polars code")
    required = str(_planned_method(request))
    if required == "validation" and engine != "validation":
        errors.append(
            "a validation planned test requires a validation-engine Data Test"
        )
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"data_test": definition}


def _referenced_columns(params: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for key, value in params.items():
        if not str(key).startswith("column"):
            continue
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, (list, tuple)):
            names.extend(str(item) for item in value)
    return tuple(name for name in names if name)


def _planned_method(request: WorkerRequest) -> str:
    planned = _resolved_item(request, DATA_TEST_PLANNED_SOURCE_ID)
    if not isinstance(planned, Mapping):
        raise WorkerContractError("The planned-test context must supply an object.")
    return str(planned.get("method") or "")


def run_data_test_spec_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {
            "RCM ROW": _resolved_item(request, DATA_TEST_ROW_SOURCE_ID),
            "PLANNED TEST": _resolved_item(request, DATA_TEST_PLANNED_SOURCE_ID),
            "RESOLVED CONTEXT": request.context.to_dict(),
            "ANALYTICS REGISTRY": analytics.registry_payload(),
            "VALIDATION REGISTRY": validation.registry_payload(),
            "DATA TEST CONTRACT": {
                "required": ["title", "objective", "engine", "table_refs", "spec"],
                "engines": sorted(_DATA_TEST_ENGINES),
                "analytics_spec": {
                    "test_id": "exact id from ANALYTICS REGISTRY",
                    "params": "object matching that registry entry",
                },
                "validation_spec": {
                    "rules": [
                        {
                            "check": "exact id from VALIDATION REGISTRY",
                            "column": "exact column name when the check is column-scoped",
                            "params": "object matching that registry entry",
                        }
                    ],
                    "sql_supported": False,
                },
                "polars_spec": {"code": "safe Polars assigning a DataFrame to result"},
            },
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
            "worker_kind": "data_test_spec",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        DATA_TEST_SPEC_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


DATA_TEST_SPEC_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="fieldwork.data_test_spec.response",
    schema_hash=_sha256_text("data-test-spec-response:json-object-with-data-test"),
    validator=_data_test_response_schema,
)
DATA_TEST_SPEC_WORKER = WorkerDefinition(
    worker_id=DATA_TEST_SPEC_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_data_test_spec_worker)),
    prompt_hash=_sha256_text(DATA_TEST_SPEC_SYSTEM),
    response_schema=DATA_TEST_SPEC_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair Data Test definition contract violations against the supplied "
            "schemas and registries."
        ),
    ),
    implementation=run_data_test_spec_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_data_test_proposal)
    ),
    semantic_validator=validate_data_test_proposal,
)

WORKERS.register(DATA_TEST_SPEC_WORKER)


# --------------------------------------------------------------------------- #
# fieldwork.document_test_spec worker (P7E.3)
# --------------------------------------------------------------------------- #
DOCUMENT_TEST_SPEC_WORKER_ID = "fieldwork.document_test_spec"
DOCUMENT_TEST_SPEC_SYSTEM = f"""[agent:document_test_spec]
Translate one planned test into exactly one substantive durable Document Test.
Return an object with document_test containing title, kind
(vouching|attribute|review|qa), spec, and non-empty items. Every item must have
a label and its exact attached document_ids when evidence is available.
Vouching items need checks with field, expected, method, and optional tolerance;
method must be exact, normalized, fuzzy, numeric_tolerance, or date_tolerance.
attribute items need attributes; review items need page, excerpt, or summary;
Q&A items need a concrete question. If evidence is unavailable, still return a
concrete question/attribute/check plus missing_evidence containing required
document types/identifiers and rationale. Never return a description-only
manual-review item. {JSON_RULES}"""

DOCUMENT_TEST_DOCUMENT_SOURCE_ID = "documents"
_DOCUMENT_TEST_KINDS = {"vouching", "attribute", "review", "qa"}


def _supplied_document_ids(request: WorkerRequest) -> set[str]:
    ids = set()
    for item in request.context.items:
        if item.source_id != DOCUMENT_TEST_DOCUMENT_SOURCE_ID:
            continue
        prefix, separator, document_id = str(item.source_ref).partition(":")
        if prefix == "document" and separator and document_id:
            ids.add(document_id)
    return ids


def _document_test_response_schema(response: str) -> Mapping[str, Any]:
    return _json_object(response, "document_test")


def validate_document_test_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Check the Document Test contract the supplied context can decide.

    Document identity is checked against the documents actually supplied in the
    bundle rather than the workspace, so the worker stays bundle-only while the
    executor keeps the authoritative durable-write validation.
    """
    value = proposal.get("document_test")
    if not isinstance(value, Mapping):
        raise WorkerResponseValidationError("document_test must be an object")
    definition = _plain_json(value)
    errors: list[str] = []
    kind = str(definition.get("kind") or "")
    if kind not in _DOCUMENT_TEST_KINDS:
        errors.append("document_test.kind is unsupported")
    if not str(definition.get("title") or "").strip():
        errors.append("document_test.title is required")
    if not isinstance(definition.get("spec"), Mapping):
        errors.append("document_test.spec must be an object")
    items = definition.get("items")
    if not isinstance(items, list) or not items:
        errors.append("document_test.items must be non-empty")
        items = []
    known = _supplied_document_ids(request)
    for index, item in enumerate(items, 1):
        if not isinstance(item, Mapping) or not str(item.get("label") or "").strip():
            errors.append(f"document_test item {index} needs a label")
            continue
        unknown = [
            str(document_id)
            for document_id in item.get("document_ids") or []
            if str(document_id) not in known
        ]
        if unknown:
            errors.append(
                f"document_test item {index} references unknown document "
                f"'{unknown[0]}'"
            )
        if kind == "vouching":
            checks = item.get("checks")
            if not checks:
                errors.append(f"document_test item {index} needs comparison checks")
            else:
                for check_index, check in enumerate(checks, 1):
                    if not isinstance(check, Mapping):
                        errors.append(
                            f"document_test item {index} check {check_index} must be "
                            "an object"
                        )
                        continue
                    method = str(check.get("method") or "normalized")
                    if method not in doc_tests.METHODS:
                        errors.append(f"Unknown comparison method '{method}'.")
        elif kind == "attribute" and not item.get("attributes"):
            errors.append(f"document_test item {index} needs attributes")
        elif kind == "review" and not any(
            item.get(key) not in (None, "") for key in ("page", "excerpt", "summary")
        ):
            errors.append(
                f"document_test item {index} needs a review location or summary"
            )
        elif kind == "qa" and not str(item.get("question") or "").strip():
            errors.append(f"document_test item {index} needs a question")
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"document_test": definition}


def run_document_test_spec_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {
            "RCM ROW": _resolved_item(request, DATA_TEST_ROW_SOURCE_ID),
            "PLANNED TEST": _resolved_item(request, DATA_TEST_PLANNED_SOURCE_ID),
            "RESOLVED CONTEXT": request.context.to_dict(),
            "SUPPORTED BUILDERS": sorted(_DOCUMENT_TEST_KINDS),
            "COMPARISON METHODS": sorted(doc_tests.METHODS),
            "REQUIRED ITEM CONTRACTS": {
                "vouching": {
                    "required": ["label", "document_ids", "checks"],
                    "check": ["field", "expected", "method"],
                },
                "attribute": {"required": ["label", "document_ids", "attributes"]},
                "review": "items need a document page/excerpt/summary",
                "qa": "items need document_ids and a question",
            },
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
            "worker_kind": "document_test_spec",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        DOCUMENT_TEST_SPEC_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


DOCUMENT_TEST_SPEC_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="fieldwork.document_test_spec.response",
    schema_hash=_sha256_text(
        "document-test-spec-response:json-object-with-document-test"
    ),
    validator=_document_test_response_schema,
)
DOCUMENT_TEST_SPEC_WORKER = WorkerDefinition(
    worker_id=DOCUMENT_TEST_SPEC_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_document_test_spec_worker)),
    prompt_hash=_sha256_text(DOCUMENT_TEST_SPEC_SYSTEM),
    response_schema=DOCUMENT_TEST_SPEC_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair Document Test definition contract violations against the "
            "supplied documents."
        ),
    ),
    implementation=run_document_test_spec_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_document_test_proposal)
    ),
    semantic_validator=validate_document_test_proposal,
)

WORKERS.register(DOCUMENT_TEST_SPEC_WORKER)


# --------------------------------------------------------------------------- #
# fieldwork.document_qa worker (P7F.3)
# --------------------------------------------------------------------------- #
DOCUMENT_QA_WORKER_ID = "fieldwork.document_qa"
DOCUMENT_QA_SYSTEM = """[agent:document_qa]
Answer only from the included pages. Return one JSON object only, with `answer`
as a string and `citations` as an array of objects. Each citation object has
`page` as an integer and `excerpt` as a short verbatim string. If the answer is
absent, say so. Do not invent facts. Do not return prose outside the JSON object
or a Markdown fence."""

DOCUMENT_QA_QUESTION_SOURCE_ID = "qa_item"
DOCUMENT_QA_PAGE_SOURCE_ID = "document_pages"
# A citation excerpt that is not verbatim in its page is replaced by the page's
# opening text rather than rejected, matching the established anchor contract.
_DOCUMENT_QA_FALLBACK_EXCERPT_CHARACTERS = 240


def _supplied_pages(request: WorkerRequest) -> dict[int, str]:
    """Return the exact page text supplied to this worker, keyed by page."""
    pages: dict[int, str] = {}
    for item in request.context.items:
        if item.source_id != DOCUMENT_QA_PAGE_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            raise WorkerContractError("Document page context must supply objects.")
        try:
            page = int(content.get("page"))
        except (TypeError, ValueError):
            raise WorkerContractError("Document page context requires a page number.")
        pages[page] = str(content.get("text") or "")
    if not pages:
        raise WorkerContractError(
            "The document Q&A worker requires at least one supplied page."
        )
    return pages


def _document_qa_response_schema(response: str) -> Mapping[str, Any]:
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
    if not isinstance(payload.get("answer"), str):
        raise WorkerResponseValidationError("`answer` must be a string")
    citations = payload.get("citations")
    if citations is None:
        citations = []
    if not isinstance(citations, list) or any(
        not isinstance(item, dict) for item in citations
    ):
        raise WorkerResponseValidationError("`citations` must be an array of objects")
    return {"answer": payload["answer"], "citations": citations}


def validate_document_qa_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Bind every citation to a page that was actually supplied.

    Citations off the supplied pages are dropped rather than repaired: the answer
    may legitimately cite only some pages, and an unsupplied page is not evidence
    this worker saw. The excerpt is normalized against the exact supplied text so
    the executor can turn it into an evidence anchor without re-reading the
    document.
    """
    pages = _supplied_pages(request)
    citations: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in proposal.get("citations") or []:
        try:
            page = int(raw.get("page"))
        except (TypeError, ValueError):
            continue
        if page not in pages or page in seen:
            continue
        excerpt = str(raw.get("excerpt") or "").strip()
        if not excerpt or excerpt not in pages[page]:
            excerpt = pages[page][:_DOCUMENT_QA_FALLBACK_EXCERPT_CHARACTERS].strip()
        if not excerpt:
            continue
        seen.add(page)
        citations.append({"page": page, "excerpt": excerpt})
    return {
        "answer": str(proposal.get("answer") or ""),
        "citations": sorted(citations, key=lambda item: item["page"]),
    }


def run_document_qa_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied question and pages into one model request."""
    item = _resolved_item(request, DOCUMENT_QA_QUESTION_SOURCE_ID)
    if not isinstance(item, Mapping):
        raise WorkerContractError("The document Q&A item context must be an object.")
    question = str(item.get("question") or "").strip()
    if not question:
        raise WorkerContractError("The document Q&A item context requires a question.")
    pages = _supplied_pages(request)
    page_text = "\n\n".join(
        f"--- Page {page} ---\n{pages[page]}" for page in sorted(pages)
    )
    user = f"Question: {question}\n\nIncluded document pages:\n{page_text}"
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return exactly one JSON object with a string `answer` and an "
            "array of citation objects containing integer `page` and string "
            "`excerpt`."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "document_qa_execution",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        DOCUMENT_QA_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


DOCUMENT_QA_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="fieldwork.document_qa.response",
    schema_hash=_sha256_text("document-qa-response:json-object-with-answer-citations"),
    validator=_document_qa_response_schema,
)
DOCUMENT_QA_WORKER = WorkerDefinition(
    worker_id=DOCUMENT_QA_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_document_qa_worker)),
    prompt_hash=_sha256_text(DOCUMENT_QA_SYSTEM),
    response_schema=DOCUMENT_QA_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the document Q&A response contract against the supplied pages."
        ),
    ),
    implementation=run_document_qa_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_document_qa_proposal)
    ),
    semantic_validator=validate_document_qa_proposal,
)

WORKERS.register(DOCUMENT_QA_WORKER)


__all__ = [
    "DATA_TEST_SPEC_RESPONSE_SCHEMA",
    "DATA_TEST_SPEC_SYSTEM",
    "DATA_TEST_SPEC_WORKER",
    "DATA_TEST_SPEC_WORKER_ID",
    "DOCUMENT_QA_PAGE_SOURCE_ID",
    "DOCUMENT_QA_QUESTION_SOURCE_ID",
    "DOCUMENT_QA_RESPONSE_SCHEMA",
    "DOCUMENT_QA_SYSTEM",
    "DOCUMENT_QA_WORKER",
    "DOCUMENT_QA_WORKER_ID",
    "DOCUMENT_TEST_SPEC_RESPONSE_SCHEMA",
    "DOCUMENT_TEST_SPEC_SYSTEM",
    "DOCUMENT_TEST_SPEC_WORKER",
    "DOCUMENT_TEST_SPEC_WORKER_ID",
    "run_data_test_spec_worker",
    "run_document_qa_worker",
    "run_document_test_spec_worker",
    "validate_data_test_proposal",
    "validate_document_qa_proposal",
    "validate_document_test_proposal",
]
