"""Registered model workers for the document-analysis workflow.

There are exactly three, and they are the only document map/reduce implementation
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
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

from ... import cycle_vouching, document_analysis
from ...cycle_registry import DEFAULT_REGISTRY, RegistryReference
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
VISUAL_WORKER_ID = "documents.analysis_visual_page"
VOUCHER_WORKER_ID = "documents.analysis_voucher"
REDUCTION_WORKER_ID = "documents.analysis_reduction"

DOCUMENT_METADATA_SOURCE_ID = "document_metadata"
DOCUMENT_IDENTITY_SOURCE_ID = "document_identity"
DOCUMENT_CHUNK_SOURCE_ID = "document_chunk"
DOCUMENT_VISUAL_SOURCE_ID = "document_page_images"
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
follow-up evidence; they cannot be blank. Return exactly derived_text_markdown,
summary_markdown, and audit_notes_markdown.
{JSON_RULES}"""


VISUAL_SYSTEM = f"""[agent:document_analysis_visual_map]
Analyze the supplied normalized views as one page or standalone image. The
views may contain one overview followed by overlapping detail tiles; treat them
as parts of the same source and do not duplicate content visible in more than
one part.

Return exactly transcription_markdown, summary_markdown,
audit_notes_markdown, and citations. Transcribe meaningful labels, hierarchy,
relationships, table cells, annotations, and other visually encoded content in
reading order. The summary must remain document-centric and neutral. Audit notes
must distinguish a generated visual interpretation from evidence that a control
operated.

Each citation must use kind `visual`, the supplied page, an optional normalized
region with x, y, width, and height between 0 and 1, a short description, and
the tile_order that shows it. A visual description is not a verbatim quote.
Cite at least one material visual fact. {JSON_RULES}"""


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
        "modality": "text",
        "analysis_profile": "standard",
        "derived_text_markdown": "",
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
# documents.analysis_voucher
#
# The transaction-evidence profile. It reads the same bounded chunk the standard
# map worker reads and returns the same narrative pair, plus a structured
# registry-backed `record_fragments`. Every extracted fact carries a citation id;
# durable record identities are deliberately deferred to deterministic reduction.
# --------------------------------------------------------------------------- #
# The longest excerpt that still points at something. A citation is an anchor,
# and an excerpt spanning the whole chunk anchors nothing while satisfying every
# containment check trivially — which is what a bare "must appear in its excerpt"
# rule rewards. Two lines is enough for a value the source wrapped mid-phrase.
CITATION_EXCERPT_CHARACTERS = 240
CITATION_EXCERPT_LINES = 2


def _field_selectors(field_ids: Iterable[str]) -> list[str]:
    """Render registered field kinds as the exact selectors a response must use.

    ``group.kind.attribute|attribute`` is the form the response actually needs.
    The raw pack JSON expressed the same information as two lists the model had
    to join by hand — namespaced ids under each record kind, group/short-kind
    under each field kind — and every first-attempt failure observed in the
    procurement run was a field the record genuinely states, named through a
    selector the record kind does not offer. This rendering is shared by the
    prompt and by the repair message so both name the vocabulary identically.
    """

    return sorted(
        f"{definition.group}.{definition.kind}."
        + "|".join(attribute.id for attribute in definition.attributes)
        for definition in (
            DEFAULT_REGISTRY.field_kinds[field_id] for field_id in field_ids
        )
    )


def _interpretive_selectors(field_ids: Iterable[str]) -> list[str]:
    """Interpretive attributes, listed apart from the selectors themselves.

    Marking them inline — ``role~`` — put a syntax character inside the very
    string a response has to copy, and responses copied it: ``role~`` arrived as
    the attribute in two of five documents on one run, surviving a repair. A
    selector list has to be copyable verbatim, so the distinction goes on its own
    line.
    """

    return sorted(
        f"{definition.group}.{definition.kind}.{attribute.id}"
        for definition in (
            DEFAULT_REGISTRY.field_kinds[field_id] for field_id in field_ids
        )
        for attribute in definition.attributes
        if not attribute.verbatim
    )


def _pack_descriptor(pack_id: str) -> str:
    registry = DEFAULT_REGISTRY
    pack = registry.pack(pack_id)
    reference = json.dumps(
        registry.reference(pack_id).to_dict(), sort_keys=True, separators=(",", ":")
    )
    bindable = [
        registry.record_kinds[record_id]
        for record_id in pack.record_kind_ids
        if registry.record_kinds[record_id].bindable
    ]
    shared = (
        set.intersection(*(set(record.available_field_kinds) for record in bindable))
        if bindable
        else set()
    )
    lines = [f"PACK {pack.id} (v{pack.version}) registry={reference}"]
    for policy, label in (("transaction", "linking"), ("non_linking", "entity")):
        kinds = [
            identifier_id
            for identifier_id in pack.identifier_kind_ids
            if registry.identifier_kinds[identifier_id].edge_policy == policy
        ]
        if kinds:
            lines.append(f"  {label} identifier kinds: {', '.join(kinds)}")
    if shared:
        lines.append(
            "  fields available on every record kind below: "
            + ", ".join(_field_selectors(shared))
        )
    interpretive = _interpretive_selectors(pack.field_kind_ids)
    if interpretive:
        lines.append(
            "  interpretive attributes, which may use your own wording rather "
            "than a quote: " + ", ".join(interpretive)
        )
    for record_id in pack.record_kind_ids:
        record = registry.record_kinds[record_id]
        if not record.bindable:
            lines.append(
                f"  {record.id} — not bindable; use only when the record cannot be "
                "classified, and then supply candidate_record_kinds and "
                "review_reason; no fields"
            )
            continue
        extra = _field_selectors(set(record.available_field_kinds) - shared)
        lines.append(
            f"  {record.id} — primary identifier kinds: "
            + ", ".join(record.primary_identifier_kinds)
            + ("; also " + ", ".join(extra) if extra else "")
        )
    return "\n".join(lines)


_VOUCHER_REGISTRY_DESCRIPTORS = "\n".join(
    _pack_descriptor(str(pack["id"])) for pack in DEFAULT_REGISTRY.metadata()["packs"]
)
VOUCHER_SYSTEM = f"""[agent:document_analysis_voucher]
Analyze the supplied chunk as transaction evidence — a voucher, invoice,
purchase order, goods-received note, receipt, approval record, or similar.
Report only what this chunk states. Do not infer a value from a filename, from
metadata, or from what a document of this type usually contains.

Return exactly summary_markdown, audit_notes_markdown, citations, registry, and
record_fragments.

summary_markdown is a short neutral description of what this record is and what
it evidences. audit_notes_markdown records observations visible on the face of
the document — a missing signature or date, an unreferenced attachment, an
internal inconsistency, an alteration, an incomplete field. State the
observation and why it matters. Do not conclude that a control operated or
failed; that determination is made elsewhere by comparing this record against
the accounting population. If there is no such observation, use: `No
observations were identified on the face of this record.`

Support every substantive statement in summary_markdown and audit_notes_markdown
with a citation marker such as [c1]. A fact the registered fields below cannot
carry still belongs in the narrative, and there it needs the same anchor as
anything else.

citations is an array of objects with id, page, and a short exact excerpt copied
verbatim from this chunk. Every excerpt must appear character for character in
the chunk text — do not join separate lines into one excerpt, tidy spacing, or
paraphrase. An excerpt that is not found is a rejected response, not a dropped
citation.

An excerpt points at the part of the record a fact came from, so quote the line
that carries it and no more: at most {CITATION_EXCERPT_LINES} lines and
{CITATION_EXCERPT_CHARACTERS} characters. Quoting the whole chunk once and
citing it everywhere anchors nothing and is rejected. Emit as many citations as
you have distinct facts.

Select exactly one registered pack from the descriptors below and copy its
`registry` object exactly, including the keys `pack_id`, `pack_version`, and
`definition_hash` at the response root. You may use only record, identifier,
field, group, kind, and attribute IDs declared by that selected pack. Never
invent or abbreviate an ID.

record_fragments is an array. Each fragment describes one candidate record in
this chunk and contains:
- record_kind, classification_evidence (one or more citation IDs), optional
  candidate_record_kinds and review_reason for common.other;
- identifiers: {{kind, value: {{raw_value, normalization_status, citation}}}}; and
- fields: {{group, kind, attribute, entry,
  value: {{raw_value, normalization_status, citation}}}}.

Return an empty record_fragments array when this chunk carries no transaction
record at all. Never invent a record to fill the array.

Usually one physical source record produces one fragment. Identifiers that the
record references belong on that physical record's fragment; a reference label
or number alone does not justify a separate fragment for the referenced record.
Emit multiple fragments only when the chunk actually contains multiple
standalone records or distinct values for the physical record's same primary
identifier kind. Classify the physical record from its overall purpose and
operative fields or status, not from reference labels alone.

An identifier value is the code or reference number the record prints, such as
`V1022` or `PO2024004`. A display name is never an identifier value.

Report **every** code the record prints under an identifier kind, including the
codes of records this one only refers to — a purchase order number on an
invoice, a goods receipt number on a voucher. Identifiers are what link records
to each other, so a reference left in a description, a note, or an attachment
reference is lost. `attachments.attachment.reference` is for an item the record
encloses, never for a cycle reference.

A name and its code are two facts, not a choice. `Requested by Ethan Smith
(1041)` is a `parties.name` field with `role` `Requested by` *and* a
`common.employee_id` identifier; `Proposed vendor OfficeSupply Co. (V1022)` is a
`parties.name` field *and* a `common.vendor_id` identifier.

Every value is one envelope: `raw_value` copied with the source spelling,
`citation`, and `normalization_status`. Local code computes the normalized value
itself, so send neither `value` nor `normalization_error`, and do not correct
transaction typos.

`raw_value` and its cited excerpt must be the same text: quote the line the value
sits on, not the heading above it or the line beside it. Where the record wraps
one value across two lines, either quote both lines or quote the line you took
the value from. The descriptors below list the interpretive attributes this rule
does not apply to — a party's `role`, an approval's `role` and `decision`, an
attachment's `kind` and `present` are your reading of what the record shows, so
they may use a word the record does not print; cite the excerpt behind the
reading.

`normalization_status` is exactly `normalized` or `invalid` — no other word.
Use `normalized` for a value the record states well enough for that field's type
to read: a real date in a date attribute, a figure in a number attribute. Use
`invalid` only when the record itself prints something malformed, which keeps the
defect visible as evidence. Do not report `invalid` for a value that is fine but
belongs in a different field.

For a field, copy `group`, the short `kind`, and one `attribute` from the
selectors listed under the record kind you chose — the descriptors below give
them in `group.kind.attribute|attribute` form. Do not put a namespaced field id
in `kind`, and do not select `raw_value` as the attribute: it lives inside the
envelope. One fact carries one attribute, so a record's total in a stated
currency is two facts on the same field kind: `amounts.total.value` and
`amounts.total.currency`.

`entry` numbers the occurrence, from 0, when a record carries a field kind more
than once — three approvals, or a vendor and a buyer. Every attribute of one
occurrence must share its `entry`, which is what keeps an approver with the date
and role printed beside it. Omit `entry` when a field kind occurs once.

Local code attaches the exact selected registry reference, supplied chunk_id,
and inclusive two-integer page_span to every fragment. Do not derive those
context-envelope fields. Every classification, identifier, and field must cite
this chunk. If two distinct values occur for the selected record kind's same
primary identifier kind, emit separate fragments; never blend them. A
continuation without a primary identifier may still emit a fragment so local
reduction can attach it only when the exact evidence is unambiguous.

If the source still contains a fact for which the selected record kind has no
declared field, keep that fact in the cited narrative and omit it from fields.
Never relabel an unsupported fact as a different registered field merely to
satisfy the schema: a description is not a status, and a date is not an amount.

REGISTERED PACK DESCRIPTORS
Fields read `group.kind.attribute|attribute`. Copy a `group`, `kind`, and one
`attribute` exactly as written below.
{_VOUCHER_REGISTRY_DESCRIPTORS}

{JSON_RULES}"""


def _voucher_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    citations = payload.get("citations")
    if citations is None:
        citations = []
    if not isinstance(citations, list) or any(
        not isinstance(item, dict) for item in citations
    ):
        raise WorkerResponseValidationError("`citations` must be an array of objects")
    fragments = payload.get("record_fragments")
    if fragments is None:
        fragments = []
    # An empty array is a valid answer: a document routed to this profile that
    # carries no transaction record should say so. Demanding at least one
    # fragment demanded a fabricated one.
    if not isinstance(fragments, list) or any(
        not isinstance(item, dict) for item in fragments
    ):
        raise WorkerResponseValidationError(
            "`record_fragments` must be an array of objects"
        )
    return {
        "summary_markdown": payload.get("summary_markdown"),
        "audit_notes_markdown": payload.get("audit_notes_markdown"),
        "citations": citations,
        "registry": payload.get("registry"),
        "record_fragments": fragments,
    }


def _canonicalize_voucher_fragment(
    raw: object,
    *,
    index: int,
    reference: RegistryReference,
    chunk: Mapping[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    """Own deterministic envelope and registered field-selector mechanics locally.

    Selector problems are appended to ``errors`` rather than raised, so one
    repair turn is told about every field it has to change. Reporting only the
    first offending field left a response with two bad selectors unrepairable
    within the single permitted repair attempt.
    """

    fragment = dict(_plain_json(raw))
    pack_id = str(reference.pack_id)
    record_kind = str(fragment.get("record_kind") or "")
    record = DEFAULT_REGISTRY.record_kind(pack_id, record_kind)
    pack = DEFAULT_REGISTRY.pack(pack_id)
    allowed_fields = set(record.available_field_kinds)
    allowed_description = (
        ", ".join(_field_selectors(record.available_field_kinds)) or "none"
    )
    fields = []
    for field_index, raw_fact in enumerate(fragment.get("fields") or []):
        fact = dict(_plain_json(raw_fact))
        label = f"record_fragments[{index}].fields[{field_index}]"
        supplied_group = str(fact.get("group") or "")
        supplied_kind = str(fact.get("kind") or "")
        definition = DEFAULT_REGISTRY.field_kinds.get(supplied_kind)
        if definition is not None and definition.id in pack.field_kind_ids:
            fact["group"] = definition.group
            fact["kind"] = definition.kind
        else:
            # Resolve a canonical group/short-kind selector. Unknown or cross-pack
            # selectors deliberately continue to the strict fragment validator.
            try:
                definition = DEFAULT_REGISTRY.field_kind(
                    pack_id,
                    supplied_group,
                    supplied_kind,
                )
            except ValueError:
                definition = None
        if definition is None:
            errors.append(
                f"{label} uses unregistered selector "
                f"'{supplied_group}.{supplied_kind}' for pack '{pack_id}'; "
                f"record kind '{record_kind}' allows: {allowed_description}"
            )
            continue
        if definition.id not in allowed_fields:
            errors.append(
                f"{label} selects '{definition.group}.{definition.kind}', which is "
                f"not available on record kind '{record_kind}'; allowed fields: "
                f"{allowed_description}"
            )
            continue
        attributes = {attribute.id for attribute in definition.attributes}
        if fact.get("attribute") == "raw_value" and "value" in attributes:
            fact["attribute"] = "value"
        if str(fact.get("attribute") or "") not in attributes:
            errors.append(
                f"{label} selects attribute '{fact.get('attribute')}', which "
                f"{definition.group}.{definition.kind} does not declare; it "
                f"declares: {'|'.join(sorted(attributes))}"
            )
            continue
        fields.append(fact)
    pages = [int(page) for page in chunk.get("pages") or []]
    fragment["registry"] = reference.to_dict()
    fragment["chunk_id"] = str(chunk.get("id") or "")
    fragment["page_span"] = [min(pages), max(pages)]
    fragment["fields"] = fields
    return fragment


def _collapsed(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


# A printed reference code. Dates are removed from the text first rather than
# filtered per token, because a token scan splits ``29-Apr-2024`` into a fragment
# that no longer reads as a date. A bare four-digit year is excluded for the same
# reason: prose legitimately contains one, and it identifies nothing.
_DATE_LIKE_RE = re.compile(
    r"\d{1,2}\s*[-/ ]\s*[A-Za-z]{3,9}\s*[-/ ]\s*\d{2,4}"
    r"|\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}"
)
_REFERENCE_TOKEN_RE = re.compile(r"[\w][\w\-/]{3,}")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_PARENTHESISED_RE = re.compile(r"\(([^)]{2,})\)")
# Prose fields have no business holding a transaction reference, and a party's
# name is routinely printed beside its code.
_REFERENCE_BEARING_FIELDS = {
    ("descriptions", "description"),
    ("notes", "note"),
    ("attachments", "attachment"),
}


def _reference_tokens(text: str) -> list[str]:
    """Code-like tokens in ``text``, excluding anything that reads as a date."""

    without_dates = _DATE_LIKE_RE.sub(" ", str(text or ""))
    tokens = []
    for token in _REFERENCE_TOKEN_RE.findall(without_dates):
        if sum(character.isdigit() for character in token) < 2:
            continue
        if _YEAR_RE.fullmatch(token):
            continue
        tokens.append(token)
    return tokens


def _citation_shape_errors(citations: list[Mapping[str, Any]]) -> list[str]:
    problems = []
    for citation in citations:
        excerpt = str(citation.get("excerpt") or "")
        lines = len(excerpt.splitlines())
        if len(excerpt) > CITATION_EXCERPT_CHARACTERS or lines > CITATION_EXCERPT_LINES:
            problems.append(
                f"citation '{citation.get('id')}' quotes {len(excerpt)} characters "
                f"over {lines} line(s); an excerpt must be at most "
                f"{CITATION_EXCERPT_CHARACTERS} characters and "
                f"{CITATION_EXCERPT_LINES} line(s), so that it points at the part "
                "of the record the fact came from"
            )
    return problems


def _attribute_is_verbatim(
    pack_id: str, group: str, kind: str, attribute: str
) -> bool:
    try:
        definition = DEFAULT_REGISTRY.field_kind(pack_id, group, kind)
    except ValueError:
        return True
    return next(
        (item.verbatim for item in definition.attributes if item.id == attribute),
        True,
    )


def _apply_citation_aliases(
    fragment: dict[str, Any], aliases: Mapping[str, str]
) -> None:
    """Point evidence at the surviving id when its citation was a duplicate."""

    if not aliases:
        return
    fragment["classification_evidence"] = [
        aliases.get(str(evidence or ""), evidence)
        for evidence in fragment.get("classification_evidence") or []
    ]
    for collection in ("identifiers", "fields"):
        for item in fragment.get(collection) or []:
            envelope = item.get("value")
            if isinstance(envelope, dict):
                citation = str(envelope.get("citation") or "")
                if citation in aliases:
                    envelope["citation"] = aliases[citation]


def _ungrounded_evidence(
    fragment: Mapping[str, Any],
    index: int,
    excerpts: Mapping[str, str],
    *,
    pack_id: str,
    chunk_text: str,
) -> list[str]:
    """Require each value to sit inside the excerpt it names, not merely cite one.

    Checking only that the citation id exists let a field anchor itself to any
    surviving excerpt, so a value read from one line could be attributed to
    another. Whitespace is collapsed because the excerpt is already proven
    verbatim against the chunk and OCR line wrapping is not a grounding defect.

    Interpretive attributes are exempt: a party's ``role`` or an approval's
    ``decision`` is what the worker concludes the record shows, and a goods
    receipt naming ``GLOBAL BANK`` as the buyer never prints the word "buyer".
    """

    problems: list[str] = []
    for evidence in fragment.get("classification_evidence") or []:
        if str(evidence or "") not in excerpts:
            problems.append(
                f"record_fragments[{index}].classification_evidence names citation "
                f"'{evidence}', which this response does not supply"
            )
    for collection, selector in (("identifiers", "kind"), ("fields", "attribute")):
        for item_index, item in enumerate(fragment.get(collection) or []):
            envelope = item.get("value") or {}
            citation = str(envelope.get("citation") or "")
            label = f"record_fragments[{index}].{collection}[{item_index}]"
            if citation not in excerpts:
                problems.append(
                    f"{label} cites '{citation}', which this response does not supply"
                )
                continue
            if collection == "fields" and not _attribute_is_verbatim(
                pack_id,
                str(item.get("group") or ""),
                str(item.get("kind") or ""),
                str(item.get("attribute") or ""),
            ):
                continue
            problem = _value_grounding_error(
                envelope.get("raw_value"),
                excerpts[citation],
                chunk_text,
                label=label,
                selector=str(item.get(selector) or ""),
                citation=citation,
            )
            if problem:
                problems.append(problem)
    return problems


def _spans(needle: str, haystack: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in re.finditer(re.escape(needle), haystack)
    ]


def _value_grounding_error(
    raw_value: object,
    excerpt: str,
    chunk_text: str,
    *,
    label: str,
    selector: str,
    citation: str,
) -> str | None:
    """Require the citation to point at where the value sits in the chunk.

    Containment in either direction is not enough. A value the source wrapped
    mid-phrase overlaps the line quoted beside it without either enclosing the
    other — ``Business requirement Procurement of ... to support`` against
    ``Procurement of ... approved operational requirements.`` — and rejecting
    that threw away a correct extraction over the choice of granularity. Locating
    both in the chunk and requiring their spans to overlap says exactly what is
    meant, and still rejects a heading quoted from the line above the value.
    """

    raw = _collapsed(raw_value)
    if not raw:
        return None
    text = _collapsed(chunk_text)
    raw_spans = _spans(raw, text)
    if not raw_spans:
        return (
            f"{label} reports raw_value {json.dumps(str(raw_value))} for {selector}, "
            "which does not appear in the supplied chunk; copy the value exactly as "
            "the record prints it"
        )
    excerpt_spans = _spans(_collapsed(excerpt), text)
    if any(
        left[0] < right[1] and right[0] < left[1]
        for left in raw_spans
        for right in excerpt_spans
    ):
        return None
    return (
        f"{label} reports raw_value {json.dumps(str(raw_value))} for {selector} but "
        f"its citation '{citation}' excerpt is {json.dumps(excerpt)}, which is a "
        "different part of the record; cite the line the value sits on"
    )


def _misplaced_reference_errors(
    fragment: Mapping[str, Any],
    index: int,
    excerpts: Mapping[str, str],
    *,
    pack_id: str,
) -> list[str]:
    """Keep printed reference codes in ``identifiers``, where linking reads them.

    Two losses observed on the same run, both invisible downstream: an invoice's
    purchase-order and goods-receipt references filed as attachment references,
    and a requisition's party codes reported only as part of the excerpt behind a
    party name. Neither is indexed, so the cycle graph split into disconnected
    components while every affected document still looked complete.
    """

    reported = {
        _collapsed((item.get("value") or {}).get("raw_value"))
        for item in fragment.get("identifiers") or []
    }
    kinds = ", ".join(DEFAULT_REGISTRY.pack(pack_id).identifier_kind_ids)
    problems: list[str] = []
    seen: set[str] = set()
    for item_index, fact in enumerate(fragment.get("fields") or []):
        selector = (str(fact.get("group") or ""), str(fact.get("kind") or ""))
        envelope = fact.get("value") or {}
        if selector in _REFERENCE_BEARING_FIELDS:
            found = _reference_tokens(envelope.get("raw_value"))
            where = "its value"
        elif selector == ("parties", "name"):
            # ``Ethan Smith (1041)`` — the code beside the name is the linkable
            # half, and reporting only the name discards it.
            found = [
                token
                for group in _PARENTHESISED_RE.findall(
                    excerpts.get(str(envelope.get("citation") or "")) or ""
                )
                for token in _reference_tokens(group)
            ]
            where = "the excerpt it cites"
        else:
            continue
        for token in found:
            if _collapsed(token) in reported or token in seen:
                continue
            seen.add(token)
            problems.append(
                f"record_fragments[{index}].fields[{item_index}] leaves the reference "
                f"{json.dumps(token)} in {where} without reporting it under any "
                f"identifier kind; a code the record prints belongs in identifiers, "
                f"which is what links records to each other. Available kinds: {kinds}"
            )
    return problems


def _normalization_mismatches(
    supplied_fragment: Mapping[str, Any],
    normalized_fragment: Mapping[str, Any],
    index: int,
) -> list[str]:
    """Reject a value claimed well-formed that this field's type cannot read.

    A value the record genuinely prints malformed stays as ``invalid`` evidence —
    that is the point of the envelope. What is rejected is the combination of a
    ``normalized`` claim with a local failure, because that is how a description
    ends up in a status and a date in an amount.
    """

    problems: list[str] = []
    for collection in ("identifiers", "fields"):
        pairs = zip(
            supplied_fragment.get(collection) or [],
            normalized_fragment.get(collection) or [],
        )
        for item_index, (supplied, normalized) in enumerate(pairs):
            claimed = _collapsed((supplied.get("value") or {}).get("normalization_status"))
            envelope = normalized.get("value") or {}
            label = f"record_fragments[{index}].{collection}[{item_index}]"
            if claimed not in cycle_vouching.NORMALIZATION_STATUSES:
                problems.append(
                    f"{label}.value.normalization_status is "
                    f"{json.dumps(str((supplied.get('value') or {}).get('normalization_status') or ''))}; "
                    "it must be exactly 'normalized' or 'invalid'"
                )
                continue
            if claimed == "normalized" and envelope.get("normalization_status") == "invalid":
                selector = (
                    f"{normalized.get('group')}.{normalized.get('kind')}."
                    f"{normalized.get('attribute')}"
                    if collection == "fields"
                    else str(normalized.get("kind") or "")
                )
                problems.append(
                    f"{label} claims a normalized {selector} value, but "
                    f"{json.dumps(str(envelope.get('raw_value') or ''))} cannot be "
                    f"normalized for it ({envelope.get('normalization_error')}); use "
                    "the correct registered field, or report the source value as "
                    "invalid when the record itself is malformed"
                )
    return problems


def validate_voucher_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the standard map contract, then ground every structured field.

    The narrative half goes through exactly the same ``validate_analysis_map``
    gate the standard profile uses. The structured half is then normalized
    against the citations that survived it, so a field anchored to an excerpt the
    chunk does not contain is rejected rather than committed.
    """
    chunk = _supplied_chunk(request)
    document = _resolved_item(request, DOCUMENT_IDENTITY_SOURCE_ID)
    source_sha1 = str(document.get("source_sha1") or "")
    if not source_sha1:
        raise WorkerContractError("The supplied document identity has no source hash.")
    try:
        validated = document_analysis.validate_analysis_map(
            dict(proposal), [dict(chunk)], source_sha1
        )
    except ValueError as error:
        raise WorkerResponseValidationError(str(error)) from error
    excerpts = {
        str(value.get("id") or ""): str(value.get("excerpt") or "")
        for value in validated["citations"]
    }
    surviving_by_content = {
        (int(value.get("page") or 0), str(value.get("excerpt") or "")): str(
            value.get("id") or ""
        )
        for value in validated["citations"]
    }
    errors: list[str] = []
    # ``validate_analysis_map`` drops a citation whose excerpt is not in the chunk.
    # For the narrative that is tolerable; here it silently removes the anchor a
    # structured fact depends on, and the whole fragment is then rejected for
    # ungrounded evidence without ever saying which excerpt was wrong.
    #
    # It also drops a citation that merely repeats an earlier one's excerpt. That
    # is a duplicate, not a bad quote, so the second id is remapped onto the
    # surviving one instead of being reported as text the page does not contain —
    # guidance no response could act on, because the excerpt was already correct.
    aliases: dict[str, str] = {}
    for supplied in proposal.get("citations") or []:
        if not isinstance(supplied, Mapping):
            continue
        supplied_id = str(supplied.get("id") or "")
        if supplied_id in excerpts:
            continue
        try:
            page = int(supplied.get("page"))
        except (TypeError, ValueError):
            page = 0
        duplicate = surviving_by_content.get(
            (page, str(supplied.get("excerpt") or "").strip())
        )
        if duplicate:
            aliases[supplied_id] = duplicate
            continue
        errors.append(
            f"citation '{supplied_id}' excerpt "
            f"{json.dumps(str(supplied.get('excerpt') or ''))} does not appear "
            "verbatim on the supplied page; copy it character for character "
            "from the chunk text"
        )
    errors.extend(_citation_shape_errors(validated["citations"]))
    try:
        reference = DEFAULT_REGISTRY.validate_reference(
            _plain_json(proposal.get("registry"))
        )
    except ValueError as error:
        raise WorkerResponseValidationError(str(error)) from error
    candidate_packs = [
        str(value) for value in document.get("cycle_pack_ids") or [] if str(value)
    ]
    if candidate_packs and reference.pack_id not in candidate_packs:
        errors.append(
            f"pack '{reference.pack_id}' is not one this engagement uses; select one "
            f"of: {', '.join(sorted(candidate_packs))}"
        )
    fragments = []
    for index, raw in enumerate(proposal.get("record_fragments") or []):
        try:
            fragment = _canonicalize_voucher_fragment(
                raw,
                index=index,
                reference=reference,
                chunk=chunk,
                errors=errors,
            )
        except ValueError as error:
            errors.append(f"record_fragments[{index}]: {error}")
            continue
        _apply_citation_aliases(fragment, aliases)
        errors.extend(
            _ungrounded_evidence(
                fragment,
                index,
                excerpts,
                pack_id=reference.pack_id,
                chunk_text=str(chunk.get("text") or ""),
            )
        )
        errors.extend(
            _misplaced_reference_errors(
                fragment, index, excerpts, pack_id=reference.pack_id
            )
        )
        try:
            normalized_fragment = cycle_vouching.normalize_record_fragment(fragment)
        except (cycle_vouching.CycleSchemaError, ValueError) as error:
            errors.append(f"record_fragments[{index}]: {error}")
            continue
        errors.extend(
            _normalization_mismatches(fragment, normalized_fragment, index)
        )
        fragments.append(normalized_fragment)
    if errors:
        raise WorkerResponseValidationError("; ".join(errors))
    return {
        "chunk_id": str(chunk.get("id") or ""),
        "document_id": str(document.get("document_id") or ""),
        "pages": [int(page) for page in chunk.get("pages") or []],
        "modality": "text",
        "analysis_profile": "voucher",
        "derived_text_markdown": "",
        "summary_markdown": validated["summary_markdown"],
        "audit_notes_markdown": validated["audit_notes_markdown"],
        "citations": validated["citations"],
        "registry": reference.to_dict(),
        "record_fragments": fragments,
    }


def run_voucher_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied chunk and metadata into one model request."""

    chunk = _supplied_chunk(request)
    document = _resolved_item(request, DOCUMENT_IDENTITY_SOURCE_ID)
    # No filename, title, or note: every value in the structured result must come
    # from the record's own text, and a voucher pack's filename routinely carries
    # the transaction identifiers the worker is asked to extract.
    candidate_packs = [
        str(value) for value in document.get("cycle_pack_ids") or [] if str(value)
    ]
    # Which pack a record belongs to is a property of the engagement, not a
    # judgement about one chunk. When the workspace has already committed to one,
    # say so rather than offering the model a choice it cannot inform.
    constraint = (
        "SELECT PACK: " + ", ".join(sorted(candidate_packs)) + "\n"
        if candidate_packs
        else ""
    )
    user = (
        f"SOURCE SHA: {document.get('source_sha1')}\nCHUNK ID: {chunk['id']}\n"
        f"PAGE: {chunk['page']}\n"
        f"CHARACTER RANGE: {chunk['start_character']}..{chunk['end_character']}\n"
        "DOCUMENT OPENING CHUNK: "
        f"{'yes' if int(chunk['start_character']) == 0 else 'no'}\n"
        f"{constraint}\n"
        f"RAW SOURCE CHUNK:\n{chunk['text']}"
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used:\n- "
            + "\n- ".join(attempt.validation_errors)
            + "\n\nReturn a complete corrected JSON object that fixes every point "
            "above. Keep every identifier, field, and citation from your previous "
            "response that no point above names — a repair that also drops correct "
            "evidence is a worse answer, not a safer one. Where a fact has no "
            "allowed field on the record kind you selected, move it into the cited "
            "narrative rather than substituting an unrelated field."
        )
        if attempt.previous_response:
            user += f"\n\nYOUR PREVIOUS RESPONSE:\n{attempt.previous_response}"
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "document_voucher_analysis",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(VOUCHER_SYSTEM, user, activity, attempt=attempt.number)


# --------------------------------------------------------------------------- #
# documents.analysis_visual_page
# --------------------------------------------------------------------------- #
def _visual_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    citations = payload.get("citations")
    if not isinstance(citations, list) or any(
        not isinstance(item, dict) for item in citations
    ):
        raise WorkerResponseValidationError("`citations` must be an array of objects")
    return {
        "transcription_markdown": payload.get("transcription_markdown"),
        "summary_markdown": payload.get("summary_markdown"),
        "audit_notes_markdown": payload.get("audit_notes_markdown"),
        "citations": citations,
    }


def _supplied_media(request: WorkerRequest) -> list[dict[str, Any]]:
    handles: list[dict[str, Any]] = []
    for raw in _source_items(request, DOCUMENT_VISUAL_SOURCE_ID):
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            raise WorkerContractError(
                "Visual document context must supply prepared-media handles."
            )
        handles.append(dict(item))
    if not handles:
        raise WorkerContractError(
            "The visual document worker requires prepared media."
        )
    return sorted(handles, key=lambda item: int(item.get("tile_order") or 0))


def _normalized_region(value: object) -> dict[str, float] | None:
    if value in (None, {}):
        return None
    if not isinstance(value, Mapping):
        raise WorkerResponseValidationError(
            "visual citation region must be an object"
        )
    if set(value) != {"x", "y", "width", "height"}:
        raise WorkerResponseValidationError(
            "visual citation region must contain x, y, width, and height"
        )
    try:
        region = {
            key: float(value[key])
            for key in ("x", "y", "width", "height")
        }
    except (TypeError, ValueError) as error:
        raise WorkerResponseValidationError(
            "visual citation region values must be numeric"
        ) from error
    if (
        region["x"] < 0
        or region["y"] < 0
        or region["width"] <= 0
        or region["height"] <= 0
        or region["x"] + region["width"] > 1.000001
        or region["y"] + region["height"] > 1.000001
    ):
        raise WorkerResponseValidationError(
            "visual citation region must be normalized within the supplied image"
        )
    return region


def validate_visual_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    handles = _supplied_media(request)
    document = _resolved_item(request, DOCUMENT_METADATA_SOURCE_ID)
    transcription = str(proposal.get("transcription_markdown") or "").strip()
    try:
        text = document_analysis.validate_analysis_text(dict(proposal))
    except ValueError as error:
        raise WorkerResponseValidationError(str(error)) from error
    if not transcription:
        raise WorkerResponseValidationError(
            "Required analysis field(s) were blank: transcription_markdown"
        )
    by_order = {int(item["tile_order"]): item for item in handles}
    citations: list[dict[str, Any]] = []
    for raw in proposal.get("citations") or []:
        if str(raw.get("kind") or raw.get("evidence_kind") or "") != "visual":
            raise WorkerResponseValidationError(
                "visual map citations must use kind `visual`"
            )
        try:
            page = int(raw.get("page"))
            tile_order = int(raw.get("tile_order"))
        except (TypeError, ValueError) as error:
            raise WorkerResponseValidationError(
                "visual citations require supplied page and tile_order values"
            ) from error
        handle = by_order.get(tile_order)
        if handle is None or int(handle.get("page") or 0) != page:
            raise WorkerResponseValidationError(
                "visual citation refers to an image part that was not supplied"
            )
        description = str(raw.get("description") or "").strip()
        if not description:
            raise WorkerResponseValidationError(
                "visual citation description cannot be blank"
            )
        citations.append(
            {
                "id": str(raw.get("id") or f"V{len(citations) + 1}"),
                "page": page,
                "evidence_kind": "visual",
                "region": _normalized_region(raw.get("region")),
                "description": description,
                "tile_order": tile_order,
                "variant": str(handle.get("variant") or ""),
                "source_sha1": str(document.get("source_sha1") or ""),
                "prepared_sha256": str(handle.get("prepared_sha256") or ""),
                "generated_description": True,
            }
        )
    if not citations:
        raise WorkerResponseValidationError(
            "citations contained no validated visual anchor"
        )
    page = int(handles[0]["page"])
    return {
        "chunk_id": str(
            request.unit_input.get("chunk_id") or f"VISUAL-{page:04d}"
        ),
        "document_id": str(document.get("document_id") or ""),
        "pages": [page],
        "modality": "image",
        "transcription_markdown": transcription,
        "derived_text_markdown": transcription,
        "summary_markdown": text["summary_markdown"],
        "audit_notes_markdown": text["audit_notes_markdown"],
        "citations": citations,
        "prepared_media_set_hash": str(
            handles[0].get("prepared_set_hash") or ""
        ),
    }


def run_visual_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    handles = _supplied_media(request)
    document = _resolved_item(request, DOCUMENT_METADATA_SOURCE_ID)
    user = (
        "DOCUMENT METADATA (context only):\n"
        f"{json.dumps(document_metadata(document), indent=1, default=str)}\n"
        f"PAGE/FRAME: {handles[0]['page']}\n"
        "PREPARED PARTS (overview first, then row-major details):\n"
        + json.dumps(
            [
                {
                    key: item.get(key)
                    for key in (
                        "page",
                        "variant",
                        "tile_order",
                        "width",
                        "height",
                        "prepared_sha256",
                    )
                }
                for item in handles
            ],
            default=str,
        )
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
            "worker_kind": "document_visual_page_analysis",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "image_count": request.context.supplied_size.media_items,
            "prepared_bytes": request.context.supplied_size.media_bytes,
            "prepared_pixels": request.context.supplied_size.media_pixels,
            "estimated_image_tokens": (
                request.context.supplied_size.estimated_image_tokens
            ),
        },
    )
    return gateway.complete(
        VISUAL_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
        media=tuple(handles),
        required_capabilities=("vision",),
    )


# --------------------------------------------------------------------------- #
# documents.analysis_reduction (P9.6)
# --------------------------------------------------------------------------- #
def _reduction_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    return {
        "derived_text_markdown": payload.get("derived_text_markdown"),
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
    derived = str(proposal.get("derived_text_markdown") or "").strip()
    if not derived:
        derived = "\n\n".join(
            str(analysis.get("derived_text_markdown") or "").strip()
            for analysis in analyses
            if str(analysis.get("derived_text_markdown") or "").strip()
        )
    return {
        "derived_text_markdown": derived,
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

VISUAL_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.analysis_visual_page.response",
    schema_hash=_sha256_text(
        "document-visual-page-response:v1:transcription-summary-notes-visual-citations"
    ),
    validator=_visual_response_schema,
)
VISUAL_WORKER = WorkerDefinition(
    worker_id=VISUAL_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_visual_worker)),
    prompt_hash=_sha256_text(VISUAL_SYSTEM),
    response_schema=VISUAL_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the visual analysis against only the supplied prepared image parts."
        ),
    ),
    implementation=run_visual_worker,
    required_model_capabilities=("vision",),
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_visual_proposal)
    ),
    semantic_validator=validate_visual_proposal,
)

VOUCHER_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.analysis_voucher.response",
    schema_hash=_sha256_text(
        "document-voucher-response:v2:registry-record-fragments"
    ),
    validator=_voucher_response_schema,
)
VOUCHER_WORKER = WorkerDefinition(
    worker_id=VOUCHER_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_voucher_worker)),
    prompt_hash=_sha256_text(VOUCHER_SYSTEM),
    response_schema=VOUCHER_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        # Two, not one. This profile checks several independent things — selector,
        # attribute, citation shape, citation exactness, value grounding, typed
        # normalization, reference placement — and a response that gets one wrong
        # commonly gets a second wrong somewhere else, or introduces one while
        # fixing another. A single repair turned recoverable responses into failed
        # documents; the cost of the extra turn is one call on a document that
        # would otherwise have produced nothing.
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair the voucher analysis against the supplied chunk text, its "
            "exact excerpts, and the selected registered cycle pack."
        ),
    ),
    implementation=run_voucher_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_voucher_proposal)
        + inspect.getsource(_canonicalize_voucher_fragment)
        + inspect.getsource(cycle_vouching.normalize_record_fragment)
        + _VOUCHER_REGISTRY_DESCRIPTORS
    ),
    semantic_validator=validate_voucher_proposal,
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
        + inspect.getsource(cycle_vouching.reduce_record_fragments)
    ),
    semantic_validator=validate_reduction_proposal,
)

WORKERS.register(CHUNK_WORKER)
WORKERS.register(VISUAL_WORKER)
WORKERS.register(VOUCHER_WORKER)
WORKERS.register(REDUCTION_WORKER)


__all__ = [
    "CHUNK_ANALYSES_SOURCE_ID",
    "CHUNK_RESPONSE_SCHEMA",
    "CHUNK_SYSTEM",
    "CHUNK_WORKER",
    "CHUNK_WORKER_ID",
    "DOCUMENT_CHUNK_SOURCE_ID",
    "DOCUMENT_METADATA_SOURCE_ID",
    "DOCUMENT_VISUAL_SOURCE_ID",
    "REDUCTION_RESPONSE_SCHEMA",
    "REDUCTION_SYSTEM",
    "REDUCTION_WORKER",
    "REDUCTION_WORKER_ID",
    "VISUAL_RESPONSE_SCHEMA",
    "VISUAL_SYSTEM",
    "VISUAL_WORKER",
    "VISUAL_WORKER_ID",
    "VOUCHER_RESPONSE_SCHEMA",
    "VOUCHER_SYSTEM",
    "VOUCHER_WORKER",
    "VOUCHER_WORKER_ID",
    "document_metadata",
    "run_chunk_worker",
    "run_reduction_worker",
    "run_visual_worker",
    "run_voucher_worker",
    "validate_chunk_proposal",
    "validate_reduction_proposal",
    "validate_visual_proposal",
    "validate_voucher_proposal",
]
