"""Registered model workers for the document-analysis workflow.

There are exactly two, and they are the only document map/reduce implementation
in the repository:

``documents.analysis_chunk`` analyzes one bounded source chunk and must ground
every substantive point in an exact excerpt from the chunk it was supplied.
``documents.analysis_reduction`` consolidates the chunk proposals for one
document into a single summary and one set of audit notes, and never sees raw
source.

Both own their prompt, their bundle-to-message transformation, and the part of
the contract the supplied context can decide: response shape, required
human-facing fields, and — for the map worker — citation excerpts that appear
verbatim in the supplied chunk. Neither worker schedules units, reads the
workspace, or persists anything; the map worker's validated proposal is the
reduction's only input.

Citation and text validation reuse ``document_analysis.validate_analysis_map``
and ``validate_analysis_text``: those are the application's own durable analysis
contract rather than engagement content, so they belong to the response contract
these workers enforce.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from ... import document_analysis
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

CHUNK_WORKER_ID = "documents.analysis_chunk"
REDUCTION_WORKER_ID = "documents.analysis_reduction"

DOCUMENT_METADATA_SOURCE_ID = "document_metadata"
DOCUMENT_CHUNK_SOURCE_ID = "document_chunk"
CHUNK_ANALYSES_SOURCE_ID = "chunk_analyses"


CHUNK_SYSTEM = f"""[agent:document_analysis_map]
Analyze the supplied source chunk as part of the document itself. Do not turn
its content into an audit objective, audit scope, audit plan, engagement
background, risk assessment, or claim that a control operated.

Infer the document type primarily from source text; metadata is a fallible
classification hint. Use a document-appropriate summary:
- policy/procedure/regulation: identity and governance status; purpose and
  applicability; process sequence; roles and approvals; requirements,
  thresholds, records, exceptions, escalation, monitoring, and review;
- contract: parties, status and dates, term, scope, deliverables, commercial
  terms, obligations, service levels, changes, renewal, and termination;
- minutes/correspondence: date, participants or sender/recipient, matters
  discussed, decisions, actions, owners, deadlines, and open items;
- voucher/evidence: record type, date, parties, amounts or references,
  approvals, and recorded status;
- prior report: subject and period, conclusion, findings, recommendations,
  management actions, owners, deadlines, and status;
- other/background: identity, purpose, principal facts, responsibilities,
  decisions, obligations, dates, and dependencies that the text supports.

For policy-like documents, use `Purpose` and `Applicability`; do not recast
them as audit `Objective` and `Scope`. In an opening chunk, report important
governance metadata such as issuer, version, owner, approval/effective/review
dates, and draft/final status. If an important field is absent, say `Not stated
in the supplied document` or `Not stated in the supplied extract`; never infer
status or currency from a filename or category. Continuation chunks should
omit claims that front-matter metadata is missing.

Return exactly summary_markdown, audit_notes_markdown, and citations.
The summary must be a neutral, concise representation of the document.
Audit notes must identify supported review observations such as missing or
unclear governance metadata, unresolved template placeholders, ambiguous
thresholds/criteria/timeframes/responsibilities, incomplete exception or
escalation rules, referenced documents to obtain, or operating evidence to
verify. Each useful note should state the observation, why it matters, and a
follow-up. Describe omissions as not specified in the supplied document or
extract, not as proof that the underlying process lacks them. Do not fill notes
with a generic restatement of every documented control.

summary_markdown and audit_notes_markdown are required and cannot be blank. If
there is genuinely no specific review observation, use: `No specific drafting
or control-design observations were identified from the supplied text.
Operating effectiveness was not assessed.`

Every substantive point must use citation markers such as [C1]. Citations is
an array of objects with id, page, and a short exact excerpt copied verbatim
from this chunk. Metadata and generated orientation are context only and cannot
support citations. Distinguish documented requirements from evidence that a
control operated, and omit unsupported claims. {JSON_RULES}"""


REDUCTION_SYSTEM = f"""[agent:document_analysis_reduce]
Consolidate generated chunk analyses into one document-centric summary and one
set of useful audit notes. You receive no raw source. Preserve citation markers,
document type, governance metadata, process order, responsibilities, approvals,
key requirements, and explicit `not stated` qualifications. Remove duplication
and do not introduce new document facts. Do not convert the result into audit
objective/scope, engagement background, an audit plan, or a control-operation
claim. Audit notes must retain concrete observations, why they matter, and
follow-up evidence; they cannot be blank. Return exactly summary_markdown and
audit_notes_markdown. {JSON_RULES}"""


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _source_items(request: WorkerRequest, source_id: str) -> list[object]:
    return [
        item.content for item in request.context.items if item.source_id == source_id
    ]


def _resolved_item(request: WorkerRequest, source_id: str) -> Mapping[str, Any]:
    matches = _source_items(request, source_id)
    if len(matches) != 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply exactly one item."
        )
    value = _plain_json(matches[0])
    if not isinstance(value, Mapping):
        raise WorkerContractError(f"Context source '{source_id}' must supply an object.")
    return value


def document_metadata(document: Mapping[str, Any]) -> dict:
    """Return useful classification context without internal storage paths."""

    relative_path = str(document.get("relative_path") or "").replace("\\", "/")
    folder = str(PurePosixPath(relative_path).parent) if relative_path else ""
    if folder == ".":
        folder = ""
    values = {
        "document_id": document.get("id"),
        "title": document.get("title"),
        "original_filename": document.get("source"),
        "category": document.get("category"),
        "folder_context": folder,
        "user_note": document.get("note"),
    }
    return {key: value for key, value in values.items() if value not in (None, "")}


def _json_object(response: str) -> dict[str, Any]:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```", value, re.DOTALL | re.IGNORECASE
    )
    if fenced:
        value = fenced.group(1).strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise WorkerResponseValidationError("the response is not a valid JSON object")
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    return payload


# --------------------------------------------------------------------------- #
# documents.analysis_chunk (P9.4)
# --------------------------------------------------------------------------- #
def _chunk_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    citations = payload.get("citations")
    if citations is None:
        citations = []
    if not isinstance(citations, list) or any(
        not isinstance(item, dict) for item in citations
    ):
        raise WorkerResponseValidationError("`citations` must be an array of objects")
    return {
        "summary_markdown": payload.get("summary_markdown"),
        "audit_notes_markdown": payload.get("audit_notes_markdown"),
        "citations": citations,
    }


def _supplied_chunk(request: WorkerRequest) -> Mapping[str, Any]:
    chunk = _resolved_item(request, DOCUMENT_CHUNK_SOURCE_ID)
    if not str(chunk.get("text") or ""):
        raise WorkerContractError("The supplied document chunk carries no text.")
    return chunk


def validate_chunk_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Require complete text and at least one exact excerpt from this chunk.

    The excerpt check is the same ``validate_analysis_map`` contract the former
    runner enforced, applied against the one chunk this worker was supplied — so
    a citation cannot be carried over from another chunk's text or invented from
    metadata.
    """
    chunk = _supplied_chunk(request)
    document = _resolved_item(request, DOCUMENT_METADATA_SOURCE_ID)
    source_sha1 = str(document.get("source_sha1") or "")
    if not source_sha1:
        raise WorkerContractError("The supplied document metadata has no source hash.")
    try:
        validated = document_analysis.validate_analysis_map(
            dict(proposal), [dict(chunk)], source_sha1
        )
    except ValueError as error:
        raise WorkerResponseValidationError(str(error)) from error
    return {
        "chunk_id": str(chunk.get("id") or ""),
        "document_id": str(document.get("document_id") or ""),
        "pages": [int(page) for page in chunk.get("pages") or []],
        "summary_markdown": validated["summary_markdown"],
        "audit_notes_markdown": validated["audit_notes_markdown"],
        "citations": validated["citations"],
    }


def run_chunk_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied chunk and metadata into one model request."""

    chunk = _supplied_chunk(request)
    document = _resolved_item(request, DOCUMENT_METADATA_SOURCE_ID)
    # No running orientation from earlier chunks. Chunk units are independent
    # semantic units that the scheduler may run concurrently and resume in any
    # order, and a proposal is reusable only when its exact context identity
    # still holds — both of which an accumulating generated preamble would break.
    # The chunk position below is what tells the model whether front matter is
    # expected here.
    user = (
        "DOCUMENT METADATA (context only; not citation evidence):\n"
        f"{json.dumps(document_metadata(document), indent=1, default=str)}\n"
        f"SOURCE SHA: {document.get('source_sha1')}\nPAGE: {chunk['page']}\n"
        f"CHARACTER RANGE: {chunk['start_character']}..{chunk['end_character']}\n"
        "DOCUMENT OPENING CHUNK: "
        f"{'yes' if int(chunk['start_character']) == 0 else 'no'}\n\n"
        f"RAW SOURCE CHUNK:\n{chunk['text']}"
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
            "worker_kind": "document_chunk_analysis",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(CHUNK_SYSTEM, user, activity, attempt=attempt.number)


# --------------------------------------------------------------------------- #
# documents.analysis_reduction (P9.6)
# --------------------------------------------------------------------------- #
def _reduction_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    return {
        "summary_markdown": payload.get("summary_markdown"),
        "audit_notes_markdown": payload.get("audit_notes_markdown"),
    }


def _supplied_chunk_analyses(request: WorkerRequest) -> list[dict[str, Any]]:
    """The chunk proposals supplied to this reduction, in stable chunk order."""

    analyses: list[dict[str, Any]] = []
    for raw in _source_items(request, CHUNK_ANALYSES_SOURCE_ID):
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            raise WorkerContractError("Chunk analysis context must supply objects.")
        analyses.append(dict(item))
    if not analyses:
        raise WorkerContractError(
            "The document reduction worker requires at least one chunk analysis."
        )
    return sorted(analyses, key=lambda item: str(item.get("chunk_id") or ""))


def validate_reduction_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Require both human-facing fields and carry the supplied citations through.

    Citations are not re-derived here: the reduction sees no raw source, so the
    only citations it may carry are exactly the ones the map worker already bound
    to a supplied chunk. Anything else would be a claim about text this worker
    never saw.
    """
    analyses = _supplied_chunk_analyses(request)
    try:
        validated = document_analysis.validate_analysis_text(dict(proposal))
    except ValueError as error:
        raise WorkerResponseValidationError(str(error)) from error
    citations = [
        dict(citation)
        for analysis in analyses
        for citation in analysis.get("citations") or []
    ]
    return {
        "summary_markdown": validated["summary_markdown"],
        "audit_notes_markdown": validated["audit_notes_markdown"],
        "citations": citations,
        "chunk_ids": [str(analysis.get("chunk_id") or "") for analysis in analyses],
    }


def run_reduction_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied chunk analyses into one model request."""

    document = _resolved_item(request, DOCUMENT_METADATA_SOURCE_ID)
    analyses = _supplied_chunk_analyses(request)
    user = (
        "DOCUMENT METADATA (context only; not citation evidence):\n"
        f"{json.dumps(document_metadata(document), indent=1, default=str)}\n"
        f"GENERATED CHUNK ANALYSES:\n{json.dumps(analyses, default=str)}"
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
            "worker_kind": "document_analysis_reduction",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(REDUCTION_SYSTEM, user, activity, attempt=attempt.number)


CHUNK_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.analysis_chunk.response",
    schema_hash=_sha256_text(
        "document-chunk-response:json-object-with-summary-notes-citations"
    ),
    validator=_chunk_response_schema,
)
CHUNK_WORKER = WorkerDefinition(
    worker_id=CHUNK_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_chunk_worker)),
    prompt_hash=_sha256_text(CHUNK_SYSTEM),
    response_schema=CHUNK_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the document chunk analysis against the supplied chunk text "
            "and its exact excerpts."
        ),
    ),
    implementation=run_chunk_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_chunk_proposal)),
    semantic_validator=validate_chunk_proposal,
)

REDUCTION_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.analysis_reduction.response",
    schema_hash=_sha256_text("document-reduction-response:json-object-with-summary-notes"),
    validator=_reduction_response_schema,
)
REDUCTION_WORKER = WorkerDefinition(
    worker_id=REDUCTION_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_reduction_worker)),
    prompt_hash=_sha256_text(REDUCTION_SYSTEM),
    response_schema=REDUCTION_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the document reduction against the supplied chunk analyses."
        ),
    ),
    implementation=run_reduction_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_reduction_proposal)
    ),
    semantic_validator=validate_reduction_proposal,
)

WORKERS.register(CHUNK_WORKER)
WORKERS.register(REDUCTION_WORKER)


__all__ = [
    "CHUNK_ANALYSES_SOURCE_ID",
    "CHUNK_RESPONSE_SCHEMA",
    "CHUNK_SYSTEM",
    "CHUNK_WORKER",
    "CHUNK_WORKER_ID",
    "DOCUMENT_CHUNK_SOURCE_ID",
    "DOCUMENT_METADATA_SOURCE_ID",
    "REDUCTION_RESPONSE_SCHEMA",
    "REDUCTION_SYSTEM",
    "REDUCTION_WORKER",
    "REDUCTION_WORKER_ID",
    "document_metadata",
    "run_chunk_worker",
    "run_reduction_worker",
    "validate_chunk_proposal",
    "validate_reduction_proposal",
]
