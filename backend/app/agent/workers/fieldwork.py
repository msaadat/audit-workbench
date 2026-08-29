"""Registered model worker for audit fieldwork execution.

The document-assessment worker answers one attached document's bounded request
from the pages it was supplied. It owns its prompt, its bounded bundle-to-message
transformation, and the part of its contract that can be decided from the
supplied context: response shape and citation binding against the exact pages it
received.

Writing a test's executable specification is not fieldwork — that is the tests
capability group, in :mod:`agent.workers.tests`.
"""

from __future__ import annotations

import hashlib
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
    decode_json_response,
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
DOCUMENT_QA_OUTCOMES = frozenset(
    {"accepted", "exception", "needs_manual_check"}
)
DOCUMENT_QA_CONTROL_CONCLUSIONS = frozenset(
    {"effective", "partially_effective", "ineffective", "no_conclusion", "not_applicable"}
)
DOCUMENT_QA_SYSTEM = """[agent:document_qa]
Answer or assess only from the included pages. Return one JSON object only, with
`answer` as a string, `outcome` as one of `accepted`, `exception`, or
`needs_manual_check`, `conclusion` as a concise statement of what this result
means for the test item, `control_conclusion` as one of `effective`,
`partially_effective`, `ineffective`, `no_conclusion`, or `not_applicable`, and
`citations` as an array of objects. Each citation object has `page` as an
integer and `excerpt` as a short verbatim string.

Choose `accepted` when the evidence affirmatively satisfies the question or
expected condition, `exception` when it affirmatively does not, and
`needs_manual_check` when the evidence is absent, ambiguous, or inconclusive.
Use `no_conclusion` when evidence is inconclusive; do not choose a conclusion
outside the fixed list. Do not invent facts. Do not return prose outside the JSON
object or a Markdown fence."""

DOCUMENT_QA_QUESTION_SOURCE_ID = "qa_item"
DOCUMENT_QA_PAGE_SOURCE_ID = "document_pages"
# A citation excerpt that is not verbatim in its page is replaced by the page's
# opening text rather than rejected, matching the established anchor contract.
_DOCUMENT_QA_FALLBACK_EXCERPT_CHARACTERS = 240


def _fallback_control_conclusion(outcome: str) -> str:
    """Keep pre-change proposal sidecars compatible with the new enum."""
    return {
        "accepted": "effective",
        "exception": "ineffective",
    }.get(outcome, "no_conclusion")


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
        if page in pages:
            raise WorkerContractError(
                "Document page context must not supply the same page more than once."
            )
        pages[page] = str(content.get("text") or "")
    if not pages:
        raise WorkerContractError(
            "The document Q&A worker requires at least one supplied page."
        )
    return pages


def _document_qa_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    if not isinstance(payload.get("answer"), str):
        raise WorkerResponseValidationError("`answer` must be a string")
    if "conclusion" in payload and not isinstance(payload["conclusion"], str):
        raise WorkerResponseValidationError("`conclusion` must be a string")
    outcome = str(payload.get("outcome") or "").strip()
    if outcome not in DOCUMENT_QA_OUTCOMES:
        choices = ", ".join(sorted(DOCUMENT_QA_OUTCOMES))
        raise WorkerResponseValidationError(f"`outcome` must be one of: {choices}")
    control_conclusion = str(
        payload.get("control_conclusion") or _fallback_control_conclusion(outcome)
    )
    if control_conclusion not in DOCUMENT_QA_CONTROL_CONCLUSIONS:
        choices = ", ".join(sorted(DOCUMENT_QA_CONTROL_CONCLUSIONS))
        raise WorkerResponseValidationError(
            f"`control_conclusion` must be one of: {choices}"
        )
    citations = payload.get("citations")
    if citations is None:
        citations = []
    if not isinstance(citations, list) or any(
        not isinstance(item, dict) for item in citations
    ):
        raise WorkerResponseValidationError("`citations` must be an array of objects")
    return {
        "answer": payload["answer"],
        # Keep old proposal sidecars and interrupted runs readable. New model
        # responses are instructed to supply this explicitly.
        "conclusion": str(payload.get("conclusion") or payload["answer"]),
        "control_conclusion": control_conclusion,
        "outcome": outcome,
        "citations": citations,
    }


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
    outcome = str(proposal.get("outcome") or "needs_manual_check")
    if outcome not in DOCUMENT_QA_OUTCOMES:
        outcome = "needs_manual_check"
    if outcome in {"accepted", "exception"} and not citations:
        outcome = "needs_manual_check"
    control_conclusion = str(
        proposal.get("control_conclusion") or _fallback_control_conclusion(outcome)
    )
    if control_conclusion not in DOCUMENT_QA_CONTROL_CONCLUSIONS:
        control_conclusion = _fallback_control_conclusion(outcome)
    return {
        "answer": str(proposal.get("answer") or ""),
        "conclusion": str(proposal.get("conclusion") or proposal.get("answer") or ""),
        "control_conclusion": control_conclusion,
        "outcome": outcome,
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
            + ". Return exactly one JSON object with a string `answer`, an "
            "string `conclusion`, "
            "`outcome` of `accepted`, `exception`, or `needs_manual_check`, "
            "a `control_conclusion` from the fixed enum, "
            "and an array of citation objects containing integer `page` and "
            "string `excerpt`."
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
    schema_hash=_sha256_text(
        "document-qa-response:json-object-with-answer-conclusion-control-conclusion-outcome-citations"
    ),
    validator=_document_qa_response_schema,
)
DOCUMENT_QA_WORKER = WorkerDefinition(
    worker_id=DOCUMENT_QA_WORKER_ID,
    prompt_hash=_sha256_text(DOCUMENT_QA_SYSTEM),
    response_schema=DOCUMENT_QA_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the document Q&A response contract against the supplied pages."
        ),
    ),
    implementation=run_document_qa_worker,
    semantic_validator=validate_document_qa_proposal,
)

WORKERS.register(DOCUMENT_QA_WORKER)


__all__ = [
    "DOCUMENT_QA_OUTCOMES",
    "DOCUMENT_QA_CONTROL_CONCLUSIONS",
    "DOCUMENT_QA_PAGE_SOURCE_ID",
    "DOCUMENT_QA_QUESTION_SOURCE_ID",
    "DOCUMENT_QA_RESPONSE_SCHEMA",
    "DOCUMENT_QA_SYSTEM",
    "DOCUMENT_QA_WORKER",
    "DOCUMENT_QA_WORKER_ID",
    "run_document_qa_worker",
    "validate_document_qa_proposal",
]


# --------------------------------------------------------------------------- #
# fieldwork.cycle_vouch worker
# --------------------------------------------------------------------------- #
#: One pass judges one linked cycle against the whole grid of checks approved
#: for it. Per-cell rather than per-call: the reader needs the other documents
#: in front of it to tell a presentation difference from a real one, and asking
#: cell by cell would spend a call to answer each half of a comparison.
CYCLE_VOUCH_WORKER_ID = "fieldwork.cycle_vouch"
CYCLE_VOUCH_ITEM_SOURCE_ID = "cycle_item"
CYCLE_VOUCH_VERDICTS = frozenset({"agrees", "disagrees", "cannot_determine"})

CYCLE_VOUCH_SYSTEM = """[agent:cycle_vouch]
You are vouching one transaction cycle against a fixed grid of checks.

The documents were already linked into one cycle, and the fields each check
reads were extracted verbatim - including any defects left by scanning. For
each check, read the values it names and decide whether the documents state the
same thing.

WHAT YOU ARE DECIDING
Agreement of substance, not of rendering. Two documents recording one fact in
different house styles agree. Two recording different facts do not.

LOOK PAST THESE - they agree
- currency words, codes and symbols: 'PKR 2,000,000.00' and '2,000,000.00'
- thousands separators and trailing zeros: '2,000,000', '2000000.00', '25', '25.0'
- date formats and separators: '06-Apr-2024', '6 April 2024', '2024-04-06'
- case, accents, and whitespace, including stray spaces left by scanning:
  '29-Apr -2024' is 29 April 2024
- a code appended to a name: 'OfficeSupply Co. (V1022)' and 'OfficeSupply Co.'
- legal-form and abbreviation variants of one name: 'Ltd' and 'Limited'

REPORT THESE AS AN EXCEPTION - they disagree
- a different amount, quantity, date, party or reference, once rendering is set aside
- a description denoting different goods, not merely different wording

Do not apply a materiality threshold of your own. 'Material' here means the
difference lies in the fact recorded rather than in how it was printed. A
2,000,000 order settled by a 2,000,040 invoice disagrees. Whether a difference
is large enough to matter is the audit's decision, not yours, and a difference
you excuse is one nobody is told about.

WHEN YOU CANNOT TELL
Answer cannot_determine. It is a real answer, not a failure:
- a value is absent, empty or illegible
- the text is ambiguous in a way typical of scanning - 0/O, 1/l/I, 5/S, 8/B -
  so two references may be the same or may genuinely differ. 'P02024004'
  against 'PO2024004' is exactly this. Say so rather than deciding.
- what was extracted does not settle the requirement
- a value contradicts the source excerpt quoted beside it

Never guess to avoid cannot_determine. An audit recording an untested check as
passed is worse than one recording it as untested.

Return one JSON object with `cells`, one entry per supplied check, each with
`check_id` copied exactly, `verdict` of `agrees`, `disagrees`, or
`cannot_determine`, `compared` as an array of {"operand", "value"} quoting each
value exactly as supplied, and `reason` as one sentence."""


def _supplied_checks(request: WorkerRequest) -> dict[str, Mapping[str, Any]]:
    """The checks this worker was asked to judge, keyed by id."""
    item = _resolved_item(request, CYCLE_VOUCH_ITEM_SOURCE_ID)
    if not isinstance(item, Mapping):
        raise WorkerContractError("The cycle item context must be an object.")
    checks: dict[str, Mapping[str, Any]] = {}
    for raw in item.get("checks") or []:
        if not isinstance(raw, Mapping):
            raise WorkerContractError("Every supplied cycle check must be an object.")
        check_id = str(raw.get("check_id") or "").strip()
        if not check_id:
            raise WorkerContractError("Every supplied cycle check needs a check_id.")
        if check_id in checks:
            raise WorkerContractError(
                f"Cycle check '{check_id}' was supplied more than once."
            )
        checks[check_id] = raw
    if not checks:
        raise WorkerContractError(
            "The cycle vouch worker requires at least one check to judge."
        )
    return checks


def _cycle_vouch_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    cells = payload.get("cells")
    if not isinstance(cells, list) or not cells:
        raise WorkerResponseValidationError(
            "the response must carry a non-empty `cells` array"
        )
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(cells):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(f"cells[{index}] must be an object")
        verdict = str(raw.get("verdict") or "")
        if verdict not in CYCLE_VOUCH_VERDICTS:
            raise WorkerResponseValidationError(
                f"cells[{index}].verdict '{verdict}' must be one of "
                f"{', '.join(sorted(CYCLE_VOUCH_VERDICTS))}"
            )
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise WorkerResponseValidationError(
                f"cells[{index}].reason is required: say what you compared"
            )
        compared = []
        for entry in raw.get("compared") or []:
            if not isinstance(entry, Mapping):
                raise WorkerResponseValidationError(
                    f"cells[{index}].compared entries must be objects"
                )
            compared.append({
                "operand": str(entry.get("operand") or ""),
                "value": str(entry.get("value") or ""),
            })
        normalized.append({
            "check_id": str(raw.get("check_id") or ""),
            "verdict": verdict,
            "compared": compared,
            "reason": reason,
        })
    return {"cells": normalized}


def _value_was_supplied(value: str, supplied: set[str]) -> bool:
    """Whether a reported value is one the worker was actually given.

    Containment either way, because both readings are honest: a reader may
    quote the field itself ('PO2024004') or the source line it was read from
    ('Purchase order PO2024004'). What it may not do is report a value that
    appears in neither, which is what this exists to catch.
    """
    return any(
        value == item or value in item or item in value for item in supplied
    )


def validate_cycle_vouch_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Bind every cell to a check that was asked, and every value to one supplied.

    The prototype for this worker answered a check whose named field had been
    removed by reading the same fact out of a sibling field's source excerpt.
    The fact was sound and nothing was invented — but it reported the value
    under the operand's name, and the string it reported appeared in no field at
    all. A verdict whose stated evidence cannot be found in what the worker was
    given is not evidence of anything, so it is demoted here rather than
    trusted: the values are checked against the supplied ones, and a cell that
    cannot be reconciled becomes ``cannot_determine`` with the mismatch named.

    Checked here rather than in the prompt because a prompt asks and a contract
    decides, and this is the one property no reading of the response can be
    allowed to assume.
    """
    checks = _supplied_checks(request)
    supplied_values: dict[str, set[str]] = {}
    for check_id, check in checks.items():
        values = set()
        for operand in check.get("operands") or []:
            if isinstance(operand, Mapping):
                values.add(str(operand.get("value") or "").strip())
                excerpt = str(operand.get("excerpt") or "").strip()
                if excerpt:
                    values.add(excerpt)
        supplied_values[check_id] = {value for value in values if value}
    cells: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in proposal.get("cells") or []:
        check_id = str(raw.get("check_id") or "")
        if check_id not in checks or check_id in seen:
            # A cell for a check nobody asked about answers nothing, and a
            # second cell for one already answered cannot be told from it.
            continue
        seen.add(check_id)
        verdict = str(raw.get("verdict") or "")
        reason = str(raw.get("reason") or "").strip()
        compared = [dict(entry) for entry in raw.get("compared") or []]
        supplied = supplied_values.get(check_id, set())
        unfounded = [
            value
            for value in (str(entry.get("value") or "").strip() for entry in compared)
            if value and not _value_was_supplied(value, supplied)
        ]
        if unfounded and verdict != "cannot_determine":
            verdict = "cannot_determine"
            reason = (
                "Demoted: the values reported for this check were not the ones "
                f"supplied ({'; '.join(repr(value) for value in unfounded[:2])}). "
                + reason
            )
        cells.append({
            "check_id": check_id,
            "verdict": verdict,
            "compared": compared,
            "reason": reason,
        })
    # A check the reader passed over is not thereby satisfied.
    for check_id in checks:
        if check_id not in seen:
            cells.append({
                "check_id": check_id,
                "verdict": "cannot_determine",
                "compared": [],
                "reason": "The reader returned no verdict for this check.",
            })
    return {"cells": cells}


def run_cycle_vouch_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied cycle and its grid into one model request."""
    item = _resolved_item(request, CYCLE_VOUCH_ITEM_SOURCE_ID)
    checks = _supplied_checks(request)
    user = json.dumps(
        {
            "cycle": {
                "item_id": item.get("item_id"),
                "anchor": item.get("anchor"),
                "documents": item.get("documents") or [],
            },
            "grid": list(checks.values()),
        },
        indent=1,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return exactly one JSON object with a `cells` array carrying "
            "one entry per supplied check, each with check_id, verdict, "
            "compared, and reason."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "cycle_vouch_execution",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        CYCLE_VOUCH_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


CYCLE_VOUCH_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="fieldwork.cycle_vouch.response",
    schema_hash=_sha256_text(
        "cycle-vouch-response:json-object-with-cells-check-id-verdict-compared-reason"
    ),
    validator=_cycle_vouch_response_schema,
)
CYCLE_VOUCH_WORKER = WorkerDefinition(
    worker_id=CYCLE_VOUCH_WORKER_ID,
    prompt_hash=_sha256_text(CYCLE_VOUCH_SYSTEM),
    response_schema=CYCLE_VOUCH_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the cycle vouch grid against the supplied checks."
        ),
    ),
    implementation=run_cycle_vouch_worker,
    semantic_validator=validate_cycle_vouch_proposal,
)

WORKERS.register(CYCLE_VOUCH_WORKER)

__all__ += [
    "CYCLE_VOUCH_ITEM_SOURCE_ID",
    "CYCLE_VOUCH_RESPONSE_SCHEMA",
    "CYCLE_VOUCH_SYSTEM",
    "CYCLE_VOUCH_VERDICTS",
    "CYCLE_VOUCH_WORKER",
    "CYCLE_VOUCH_WORKER_ID",
    "run_cycle_vouch_worker",
    "validate_cycle_vouch_proposal",
]
