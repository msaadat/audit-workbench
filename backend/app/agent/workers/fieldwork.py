"""Registered model worker for audit fieldwork execution.

The document Q&A worker answers one attached document's question from the pages
it was supplied. It owns its prompt, its bounded bundle-to-message
transformation, and the part of its contract that can be decided from the
supplied context: response shape and citation binding against the exact pages it
received.

Writing a test's executable specification is not fieldwork — that is the tests
capability group, in :mod:`agent.workers.tests`.
"""

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


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _resolved_item(request: WorkerRequest, source_id: str) -> object:
    matches = [
        item.content for item in request.context.items if item.source_id == source_id
    ]
    if len(matches) != 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply exactly one item."
        )
    return matches[0]


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
    "DOCUMENT_QA_PAGE_SOURCE_ID",
    "DOCUMENT_QA_QUESTION_SOURCE_ID",
    "DOCUMENT_QA_RESPONSE_SCHEMA",
    "DOCUMENT_QA_SYSTEM",
    "DOCUMENT_QA_WORKER",
    "DOCUMENT_QA_WORKER_ID",
    "run_document_qa_worker",
    "validate_document_qa_proposal",
]
