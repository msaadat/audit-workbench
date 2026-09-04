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
import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from ... import cycle_vouching, document_analysis
from .. import workflow
from ..prompts import JSON_RULES, LANGUAGE_RULES
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
    submission_response,
)

CHUNK_WORKER_ID = "documents.analysis_chunk"
VISUAL_WORKER_ID = "documents.analysis_visual_page"
REDUCTION_WORKER_ID = "documents.analysis_reduction"


DOCUMENT_METADATA_SOURCE_ID = "document_metadata"
DOCUMENT_IDENTITY_SOURCE_ID = "document_identity"
DOCUMENT_CHUNK_SOURCE_ID = "document_chunk"
DOCUMENT_VISUAL_SOURCE_ID = "document_page_images"
CHUNK_ANALYSES_SOURCE_ID = "chunk_analyses"

# Some OpenAI-compatible tool-call implementations cap an individual string
# argument at 1,024 characters and flatten embedded newlines. Voucher audit notes
# therefore travel as small structured fragments alongside their structured
# records. Voucher summaries are not model output: the application renders them
# deterministically from the validated, reduced records. Standard document
# narratives do not use a tool call at all.
NARRATIVE_FRAGMENT_MAX_CHARACTERS = 768
NARRATIVE_HEADING_MAX_CHARACTERS = 160
NARRATIVE_MAX_SECTIONS = 16
NARRATIVE_MAX_ITEMS_PER_SECTION = 12
NARRATIVE_MAX_AUDIT_NOTES = 16

# The longest excerpt that still points at something. A citation is an anchor,
# and an excerpt spanning the whole chunk anchors nothing while satisfying every
# containment check trivially. Two lines is enough for a value the source wrapped
# mid-phrase.
CITATION_EXCERPT_CHARACTERS = 240
CITATION_EXCERPT_LINES = 2

STRUCTURED_NARRATIVE_RULES = f"""
Narrative output uses structured fragments so formatting never depends on a
provider preserving newlines inside one long string:
- summary_sections is a non-empty array of sections. Each section has a short
  plain-text heading, paragraphs, and bullets. Put each paragraph or bullet in
  its own array item. Do not include heading markers or newline characters;
  local code renders headings and lists as Markdown.
- audit_notes is an array of concrete observations. Each item has a short
  plain-text title plus observation, why_it_matters, and follow_up. Return an
  empty array only when there is genuinely no specific observation; local code
  then renders the standard no-observations statement.
- Each paragraph, bullet, observation, rationale, and follow-up is limited to
  {NARRATIVE_FRAGMENT_MAX_CHARACTERS} characters. Use another section, paragraph,
  bullet, or note instead of truncating a thought.
"""


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

Return exactly summary_markdown, audit_notes_markdown, and citations as one JSON
object. summary_markdown and audit_notes_markdown are freeform Markdown strings;
use real newline characters to separate headings, paragraphs, and lists.
The summary must be a neutral, concise representation of the document.
Audit notes must identify supported review observations such as missing or
unclear governance metadata, unresolved template placeholders, ambiguous
thresholds/criteria/timeframes/responsibilities, incomplete exception or
escalation rules, referenced documents to obtain, or operating evidence to
verify. Each useful note should state the observation, why it matters, and a
follow-up. Describe omissions as not specified in the supplied document or
extract, not as proof that the underlying process lacks them. Do not fill notes
with a generic restatement of every documented control.

Both Markdown fields are required and cannot be blank. If there is genuinely no
specific review observation, use: `No specific drafting or control-design
observations were identified from the supplied text. Operating effectiveness
was not assessed.`

Every substantive point must use citation markers. A marker is a citation's own
`id` in square brackets, written exactly as you declared it in citations: the
citation with id `c1` is cited as [c1], never [C1] or [1]. Citations is
an array of objects with id, page, and an ``excerpt`` copied verbatim from this
chunk. Metadata and generated orientation are context only and cannot support
citations. Distinguish documented requirements from evidence that a control
operated, and omit unsupported claims. Keep each excerpt focused on the source
text that supports the point: at most {CITATION_EXCERPT_CHARACTERS} characters
and {CITATION_EXCERPT_LINES} lines. Never join separate source lines with spaces.
An excerpt that is not character-for-character from this chunk is dropped, and
every marker naming it is removed with it: copy the source rather than retype it.
{JSON_RULES} {LANGUAGE_RULES}"""


REDUCTION_SYSTEM = f"""[agent:document_analysis_reduce]
Consolidate generated chunk analyses into one document-centric summary and one
set of useful audit notes. You receive no raw source. Preserve citation markers,
document type, governance metadata, process order, responsibilities, approvals,
key requirements, and explicit `not stated` qualifications. Remove duplication
and do not introduce new document facts. Do not convert the result into audit
objective/scope, engagement background, an audit plan, or a control-operation
claim. Audit notes must retain concrete observations, why they matter, and
follow-up evidence; they cannot be blank. Every marker you keep must be an id
one of the supplied chunk analyses declares: never renumber a marker or write a
new one, and drop the marker rather than cite an id no chunk carries. Return
exactly derived_text_markdown, summary_markdown, and audit_notes_markdown.
{JSON_RULES} {LANGUAGE_RULES}"""


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
Cite at least one material visual fact. {JSON_RULES} {LANGUAGE_RULES}"""


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
    """Parse the shared fenced-or-bare JSON envelope, saying where it broke.

    This used to carry its own copy of the decoder and report only "the response
    is not a valid JSON object" — true, and unactionable: it locates nothing, so
    a response with one unescaped quote in two thousand characters is re-emitted
    with the same quote in the same place until the repair allowance runs out.
    A live map response spent its single repair turn on exactly that, over a
    stray quote in `only the "Agreed" decision`.
    """

    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    return payload


def _narrative_submission_properties() -> dict[str, Any]:
    """Return the provider-safe narrative shape the text workers share.

    No individual generated string may reach the provider's observed 1,024
    character boundary.  Arrays remove that boundary from the complete
    narrative, and the closed objects keep Markdown layout deterministic.
    """

    fragment = {
        "type": "string",
        "minLength": 1,
        "maxLength": NARRATIVE_FRAGMENT_MAX_CHARACTERS,
    }
    return {
        "summary_sections": {
            "type": "array",
            "minItems": 1,
            "maxItems": NARRATIVE_MAX_SECTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "heading": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": NARRATIVE_HEADING_MAX_CHARACTERS,
                    },
                    "paragraphs": {
                        "type": "array",
                        "maxItems": NARRATIVE_MAX_ITEMS_PER_SECTION,
                        "items": dict(fragment),
                    },
                    "bullets": {
                        "type": "array",
                        "maxItems": NARRATIVE_MAX_ITEMS_PER_SECTION,
                        "items": dict(fragment),
                    },
                },
                "required": ["heading", "paragraphs", "bullets"],
                "additionalProperties": False,
            },
        },
        "audit_notes": {
            "type": "array",
            "maxItems": NARRATIVE_MAX_AUDIT_NOTES,
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": NARRATIVE_HEADING_MAX_CHARACTERS,
                    },
                    "observation": dict(fragment),
                    "why_it_matters": dict(fragment),
                    "follow_up": dict(fragment),
                },
                "required": [
                    "title",
                    "observation",
                    "why_it_matters",
                    "follow_up",
                ],
                "additionalProperties": False,
            },
        },
    }


def _narrative_fragment(value: object, label: str, *, heading: bool = False) -> str:
    if not isinstance(value, str):
        raise WorkerResponseValidationError(f"`{label}` must be a string")
    text = value.strip()
    if not text:
        raise WorkerResponseValidationError(f"`{label}` cannot be blank")
    if "\n" in text or "\r" in text:
        raise WorkerResponseValidationError(
            f"`{label}` must be one fragment without newline characters"
        )
    limit = (
        NARRATIVE_HEADING_MAX_CHARACTERS
        if heading
        else NARRATIVE_FRAGMENT_MAX_CHARACTERS
    )
    if len(text) > limit:
        raise WorkerResponseValidationError(
            f"`{label}` exceeds {limit} characters; split it into more fragments"
        )
    if heading and text.startswith("#"):
        raise WorkerResponseValidationError(
            f"`{label}` must be plain text without Markdown heading markers"
        )
    return text


def _narrative_array(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise WorkerResponseValidationError(f"`{label}` must be an array")
    if len(value) > NARRATIVE_MAX_ITEMS_PER_SECTION:
        raise WorkerResponseValidationError(
            f"`{label}` has more than {NARRATIVE_MAX_ITEMS_PER_SECTION} items"
        )
    return [
        _narrative_fragment(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]


#: A citation marker: a bracketed id that is not one half of a Markdown link.
#: The narrative is Markdown, so `[Portal](https://…)` and `[label][ref]` are
#: prose. Reading either as a marker would reject a legitimate link — and, now
#: that an unbound marker is stripped rather than merely reported, mangle it.
_CITATION_MARKER_RE = re.compile(
    r"(?<!\])\[([A-Za-z][A-Za-z0-9_-]{0,63})\](?![(\[])"
)

#: How many supplied markers a repair message names before it summarizes. Long
#: enough to list a typical chunk's citations in full, short enough that the
#: guidance does not crowd out the other errors sharing the turn.
_MARKER_GUIDANCE_LIMIT = 24


def _citation_marker_ids(text: str) -> set[str]:
    return set(_CITATION_MARKER_RE.findall(text))


def _replace_citation_markers(text: str, aliases: Mapping[str, str]) -> str:
    return _CITATION_MARKER_RE.sub(
        lambda match: f"[{aliases.get(match.group(1), match.group(1))}]",
        text,
    )


def _supplied_citation_ids(payload: Mapping[str, Any]) -> list[str]:
    """The citation ids a response declared, in the order it declared them.

    Order is the response's own, not sorted: ``c2`` before ``c10`` reads as the
    numbering the model chose, where a lexical sort would read as a jumble.
    """

    ordered: list[str] = []
    seen: set[str] = set()
    for item in payload.get("citations") or []:
        if not isinstance(item, Mapping):
            continue
        value = str(item.get("id") or "")
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _citation_case_map(supplied_ids: Iterable[str]) -> dict[str, str]:
    """Fold every case variant of a supplied id onto the id itself.

    A marker's case carries no information: ``[C8]`` and ``[c8]`` name the same
    citation, and the recognizer above accepts either. Rejecting the difference
    spends a repair turn on a correction with exactly one possible outcome —
    and a run whose repair allowance is spent that way fails holding an answer
    that was already right. Normalize the drift instead; reserve rejection for
    markers that name no supplied citation at all.
    """

    canonical: dict[str, str] = {}
    for value in supplied_ids:
        if value:
            canonical.setdefault(value.casefold(), value)
    return canonical


def _normalize_citation_markers(text: str, canonical: Mapping[str, str]) -> str:
    return _CITATION_MARKER_RE.sub(
        lambda match: f"[{canonical.get(match.group(1).casefold(), match.group(1))}]",
        text,
    )


#: One marker with the single space either side of it, so removing a marker
#: leaves the words it sat between correctly spaced rather than a gap.
_MARKER_SPAN_RE = re.compile(
    r"(?P<before>[ \t]?)(?<!\])\[(?P<id>[A-Za-z][A-Za-z0-9_-]{0,63})\](?![(\[])"
    r"(?P<after>[ \t]?)"
)
#: Whitespace a removed marker stranded in front of the punctuation it sat
#: before, or of the marker that followed it.
_STRANDED_SPACE_RE = re.compile(r"[ \t]+([.,;:!?)])")


def _strip_unbound_markers(text: str, bound: Collection[str]) -> str:
    """Remove every marker naming no citation in ``bound``, and only those."""

    def replace(match: "re.Match[str]") -> str:
        if match.group("id") in bound:
            return match.group(0)
        # Spaced on both sides, the marker sat between two words that still
        # need separating. Otherwise it opened or closed its phrase and takes
        # its own spacing with it.
        return match.group("after") if match.group("before") else ""

    stripped = _MARKER_SPAN_RE.sub(replace, text)
    return "\n".join(
        _STRANDED_SPACE_RE.sub(r"\1", line) for line in stripped.split("\n")
    )


def _marker_guidance(supplied_ids: Sequence[str]) -> str:
    """Name the markers a repair turn may actually use.

    "must cite a supplied citation marker" restates the rule the response just
    broke without naming a single value that would satisfy it, leaving the model
    to guess at the spelling of ids it cannot see quoted anywhere. Listing them
    turns the guess into a lookup.
    """

    if not supplied_ids:
        return (
            "no citations were supplied, so add the citation itself before "
            "citing it"
        )
    shown = list(supplied_ids[:_MARKER_GUIDANCE_LIMIT])
    listed = ", ".join(f"[{value}]" for value in shown)
    if len(supplied_ids) > len(shown):
        listed += f", … ({len(supplied_ids)} citations supplied in total)"
    return f"the markers this response supplies are {listed}"


def _structured_narrative(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    """Assemble provider-safe fragments into stable application Markdown.

    Freeform markdown fields remain readable for the standard document worker
    and for proposal sidecars written before the structured shape existed.
    """

    structured = "summary_sections" in payload or "audit_notes" in payload
    if not structured:
        return (
            str(payload.get("summary_markdown") or ""),
            str(payload.get("audit_notes_markdown") or ""),
            "legacy_markdown",
        )

    sections = payload.get("summary_sections")
    notes = payload.get("audit_notes")
    if not isinstance(sections, list) or not sections:
        raise WorkerResponseValidationError(
            "`summary_sections` must be a non-empty array"
        )
    if len(sections) > NARRATIVE_MAX_SECTIONS:
        raise WorkerResponseValidationError(
            f"`summary_sections` has more than {NARRATIVE_MAX_SECTIONS} items"
        )
    if not isinstance(notes, list):
        raise WorkerResponseValidationError("`audit_notes` must be an array")
    if len(notes) > NARRATIVE_MAX_AUDIT_NOTES:
        raise WorkerResponseValidationError(
            f"`audit_notes` has more than {NARRATIVE_MAX_AUDIT_NOTES} items"
        )

    summary_parts: list[str] = []
    cited_text: list[str] = []
    for index, raw in enumerate(sections):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(
                f"`summary_sections[{index}]` must be an object"
            )
        heading = _narrative_fragment(
            raw.get("heading"), f"summary_sections[{index}].heading", heading=True
        )
        paragraphs = _narrative_array(
            raw.get("paragraphs"), f"summary_sections[{index}].paragraphs"
        )
        bullets = _narrative_array(
            raw.get("bullets"), f"summary_sections[{index}].bullets"
        )
        if not paragraphs and not bullets:
            raise WorkerResponseValidationError(
                f"`summary_sections[{index}]` needs a paragraph or bullet"
            )
        blocks = [f"## {heading}"]
        if paragraphs:
            blocks.append("\n\n".join(paragraphs))
            cited_text.extend(paragraphs)
        if bullets:
            blocks.append("\n".join(f"- {item}" for item in bullets))
            cited_text.extend(bullets)
        summary_parts.append("\n\n".join(blocks))

    note_parts = ["## Audit notes"]
    for index, raw in enumerate(notes):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(
                f"`audit_notes[{index}]` must be an object"
            )
        title = _narrative_fragment(
            raw.get("title"), f"audit_notes[{index}].title", heading=True
        )
        observation = _narrative_fragment(
            raw.get("observation"), f"audit_notes[{index}].observation"
        )
        why = _narrative_fragment(
            raw.get("why_it_matters"), f"audit_notes[{index}].why_it_matters"
        )
        follow_up = _narrative_fragment(
            raw.get("follow_up"), f"audit_notes[{index}].follow_up"
        )
        cited_text.extend((observation, why, follow_up))
        note_parts.append(
            f"### {index + 1}. {title}\n\n{observation}\n\n"
            f"**Why it matters:** {why}\n\n**Follow-up:** {follow_up}"
        )
    if not notes:
        note_parts.append(
            "No specific drafting or control-design observations were identified "
            "from the supplied text. Operating effectiveness was not assessed."
        )

    supplied_ids = _supplied_citation_ids(payload)
    canonical = _citation_case_map(supplied_ids)
    guidance = _marker_guidance(supplied_ids)
    # Case is folded on the assembled Markdown as well as on the cited
    # fragments, so what is rendered, what is validated here, and what the
    # surviving-citation check reads later all carry the same canonical ids.
    summary = _normalize_citation_markers("\n\n".join(summary_parts), canonical)
    audit_notes = _normalize_citation_markers("\n\n".join(note_parts), canonical)
    marker_ids = _citation_marker_ids(
        _normalize_citation_markers("\n".join(cited_text), canonical)
    )
    errors: list[str] = []
    if not _citation_marker_ids(summary):
        errors.append(
            "`summary_sections` must cite at least one supplied citation "
            f"marker — {guidance}"
        )
    unknown = sorted(marker_ids - set(supplied_ids))
    if unknown:
        errors.append(
            "narrative citation marker(s) have no supplied citation: "
            + ", ".join(f"[{value}]" for value in unknown)
            + f" — {guidance}"
        )
    if errors:
        raise WorkerResponseValidationError(errors)
    return summary, audit_notes, "structured_blocks_v1"


def _narrative_bound_to_citations(
    result: Mapping[str, Any],
    *,
    declared_ids: Sequence[str] = (),
) -> Mapping[str, Any]:
    """Return ``result`` with every narrative marker naming a citation it carries.

    A marker is a promise that the sentence rests on an anchor a reader — and
    ``executors.planning._resolved_criteria_refs`` — can open. Two things break
    that promise: an id the response never declared, and one whose excerpt
    ``document_analysis.validate_citations`` dropped for not appearing verbatim
    in the supplied source. Either leaves prose citing a record no consumer can
    find, and the RCM consumer skips an id it cannot resolve without a word, so
    an unbound marker stays invisible until criteria go missing at commit.

    This ran only for the structured narrative shape, which the standard
    document workers never emit — their prompt asks for freeform Markdown and
    calls no tool — so in practice nothing checked the analyses that carry
    these markers. It now runs on every narrative, whatever shape it arrived
    in.

    Rejecting spends the repair turn where it can help: the model re-copies the
    excerpt and the citation survives. When the allowance is spent, the
    stripped narrative is the partial — the statement stays and only the anchor
    it could not honour goes, which is what the analysis already was, minus the
    promise it could not keep.
    """

    bound = _supplied_citation_ids({"citations": result.get("citations") or []})
    canonical = _citation_case_map(bound)
    summary = _normalize_citation_markers(
        str(result.get("summary_markdown") or ""), canonical
    )
    audit_notes = _normalize_citation_markers(
        str(result.get("audit_notes_markdown") or ""), canonical
    )
    normalized = {
        **result,
        "summary_markdown": summary,
        "audit_notes_markdown": audit_notes,
    }
    unbound = sorted(
        (_citation_marker_ids(summary) | _citation_marker_ids(audit_notes))
        - set(bound)
    )
    if not unbound:
        return normalized

    declared = set(declared_ids)
    guidance = _marker_guidance(bound)
    errors: list[str] = []
    dropped = [value for value in unbound if value in declared]
    if dropped:
        errors.append(
            "narrative citation marker(s) did not survive exact source "
            "validation: "
            + ", ".join(f"[{value}]" for value in dropped)
            + f" — {guidance}"
        )
    invented = [value for value in unbound if value not in declared]
    if invented:
        errors.append(
            "narrative citation marker(s) have no supplied citation: "
            + ", ".join(f"[{value}]" for value in invented)
            + f" — {guidance}"
        )
    raise WorkerResponseValidationError(
        errors,
        partial={
            **normalized,
            "summary_markdown": _strip_unbound_markers(summary, set(bound)),
            "audit_notes_markdown": _strip_unbound_markers(audit_notes, set(bound)),
        },
    )


def _citation_submission_tool(
    name: str,
    *,
    description: str,
) -> dict[str, Any]:
    """Return the provider-enforced shape shared by text document workers."""

    properties: dict[str, Any] = {
        **_narrative_submission_properties(),
        "citations": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    # Constrained to the shape the narrative markers use, so the
                    # provider settles the spelling rather than the validator
                    # rejecting a response over it. An id is cited by writing it
                    # in square brackets exactly as declared: id "c1" is [c1].
                    "id": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": "^c[0-9]{1,3}$",
                        "description": (
                            "Lower-case 'c' followed by the citation's number, "
                            "e.g. c1. Cite it in the narrative as [c1]."
                        ),
                    },
                    "page": {"type": "integer", "minimum": 1},
                    "excerpt": {"type": "string", "minLength": 1},
                },
                "required": ["id", "page", "excerpt"],
                "additionalProperties": False,
            },
        },
    }
    required = ["summary_sections", "audit_notes", "citations"]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _repair_note(attempt: WorkerAttempt, instruction: str) -> str:
    """The prose half of a repair: what was wrong, then what to do about it."""

    return (
        "Your previous response could not be used:\n- "
        + "\n- ".join(attempt.validation_errors)
        + "\n\n"
        + instruction
    )


def _repair_conversation(
    user: str,
    *,
    tool: str,
    attempt: WorkerAttempt,
    instruction: str,
) -> list[dict[str, Any]] | None:
    """Replay a rejected submission as the tool result it actually was.

    A repair used to be a fresh single-turn request with the rejected arguments
    pasted into the user message as quoted text. Every provider call runs at
    temperature 0 on a first attempt, so that request re-derived the tokens it
    was meant to correct: two repairs in the procurement run returned
    arguments byte-identical to the response being repaired, and the document
    failed having spent three calls on one answer.

    Handing the model back its own tool call, and the validator's verdict as a
    ``tool`` message, puts the rejection where a tool-trained model already
    looks for it. The trailing user turn restates the same faults in prose
    rather than relying on the tool message alone — the duplication costs a few
    tokens and buys the same guidance on providers that weight the last user
    turn far more heavily than a tool result.

    Returns ``None`` when there is no prior submission to replay — a worker
    attempt may legitimately carry guidance without one — and the caller falls
    back to the single-turn text form.
    """

    if not attempt.is_repair or not attempt.previous_response:
        return None
    try:
        arguments = json.loads(attempt.previous_response)
    except json.JSONDecodeError:
        return None
    # The sentinel means the model never called the tool. There is no submission
    # to hand back, and inventing one would teach it the wrong shape.
    if not isinstance(arguments, dict) or arguments.get("_submission_error"):
        return None
    call_id = f"repair_{attempt.number - 1}_{tool}"
    return [
        {"role": "user", "content": user},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool,
                        "arguments": attempt.previous_response,
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool,
            "content": json.dumps(
                {"accepted": False, "errors": list(attempt.validation_errors)},
                ensure_ascii=False,
            ),
        },
        {"role": "user", "content": _repair_note(attempt, instruction)},
    ]


# --------------------------------------------------------------------------- #
# documents.analysis_chunk (P9.4)
# --------------------------------------------------------------------------- #
def _chunk_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    if payload.get("_submission_error"):
        raise WorkerResponseValidationError(str(payload["_submission_error"]))
    citations = payload.get("citations")
    if citations is None:
        citations = []
    if not isinstance(citations, list) or any(
        not isinstance(item, dict) for item in citations
    ):
        raise WorkerResponseValidationError("`citations` must be an array of objects")
    summary, audit_notes, narrative_contract = _structured_narrative(payload)
    return {
        "summary_markdown": summary,
        "audit_notes_markdown": audit_notes,
        "citations": citations,
        "_narrative_contract": narrative_contract,
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
    metadata. That check drops an inexact excerpt silently, so the narrative is
    bound to what survived it before the proposal leaves here.
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
    return _narrative_bound_to_citations(
        {
            "chunk_id": str(chunk.get("id") or ""),
            "document_id": str(document.get("document_id") or ""),
            "pages": [int(page) for page in chunk.get("pages") or []],
            "modality": "text",
            "analysis_profile": "standard",
            "derived_text_markdown": "",
            "summary_markdown": validated["summary_markdown"],
            "audit_notes_markdown": validated["audit_notes_markdown"],
            "citations": validated["citations"],
        },
        # What the response declared, so a marker naming a citation the exact
        # check dropped is repaired as the bad excerpt it is, rather than as an
        # id invented out of nothing.
        declared_ids=_supplied_citation_ids(proposal),
    )


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
        instruction = (
            "Return one complete corrected JSON object. Preserve every valid "
            "field and preserve freeform Markdown with real newline characters. "
            "Citation text must use the field name `excerpt` and remain "
            "character-for-character in the supplied source chunk."
        )
        user += "\n\n" + _repair_note(attempt, instruction)
        if attempt.previous_response:
            user += "\n\nYOUR PREVIOUS RESPONSE:\n" + attempt.previous_response
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
    return gateway.complete(
        CHUNK_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


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
    never saw — including a marker in its own prose naming an id no chunk
    carries.
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
    # The reduction is asked to preserve the markers it was given, and a
    # consolidation that renumbers or carries over one the chunks never bound
    # writes exactly the same broken promise the map worker is held to. Every
    # id it may use is in the citations above, so nothing is declared here.
    return _narrative_bound_to_citations(
        {
            "derived_text_markdown": derived,
            "summary_markdown": validated["summary_markdown"],
            "audit_notes_markdown": validated["audit_notes_markdown"],
            "citations": citations,
            "chunk_ids": [
                str(analysis.get("chunk_id") or "") for analysis in analyses
            ],
        }
    )


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
        "document-chunk-response:v3:freeform-markdown-and-citations"
    ),
    validator=_chunk_response_schema,
)
CHUNK_WORKER = WorkerDefinition(
    worker_id=CHUNK_WORKER_ID,
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
    semantic_validator=validate_visual_proposal,
)


REDUCTION_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.analysis_reduction.response",
    schema_hash=_sha256_text("document-reduction-response:json-object-with-summary-notes"),
    validator=_reduction_response_schema,
)
REDUCTION_WORKER = WorkerDefinition(
    worker_id=REDUCTION_WORKER_ID,
    prompt_hash=_sha256_text(REDUCTION_SYSTEM),
    response_schema=REDUCTION_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the document reduction against the supplied chunk analyses."
        ),
    ),
    implementation=run_reduction_worker,
    semantic_validator=validate_reduction_proposal,
)

WORKERS.register(CHUNK_WORKER)
WORKERS.register(VISUAL_WORKER)
WORKERS.register(REDUCTION_WORKER)


# --------------------------------------------------------------------------- #
# document category
# --------------------------------------------------------------------------- #
CATEGORY_WORKER_ID = "documents.category"
DOCUMENT_CATEGORY_SOURCE_ID = "document_category"

#: The four values, in the order the prompt presents them: planning material
#: first, evidence last, so the partition reads as a partition.
CATEGORY_VALUES = ("policy", "minutes", "background", "evidence")

CATEGORY_SYSTEM = f"""[agent:document_category]
Say what the supplied document is *to an audit* — one of four values.

  policy      how the entity says it should operate: a policy, a procedure, a
              manual, a regulation, an authority or approval matrix, a
              delegation of authority, a signature schedule
  minutes     minuted decisions of a governing body: board or committee
              minutes, a resolution
  background  any other planning material: a contract or agreement, a prior
              audit report, correspondence, an org chart, a briefing
  evidence    a record of one transaction or one step in it: an invoice, a
              purchase order, a goods receipt, a payslip, a timesheet, a
              payment instruction, a dealing ticket, a counterparty
              confirmation, a bank statement, a bill of lading, a journal
              voucher, a tax payment receipt

The split that matters is the last one against the first three. Policy, minutes
and background describe how the entity operates, and are read as prose that an
auditor plans against. Evidence is a record of something that happened, and is
read under fields as material for a test.

Two traps, both of which have been fallen into:

  - Classify by what the document *is*, not what it is about. An approval matrix
    setting payment limits is policy, not evidence, however many transactions it
    governs. A memo discussing an invoice is background; the invoice is evidence.
  - A document naming one transaction is evidence even where its form looks
    administrative — a letter confirming one deal is evidence, a letter setting
    out how deals are confirmed is policy.

You are shown the opening page only. That is where a document states what it is.

{{JSON_RULES_PLACEHOLDER}}
Keys:
  category    one of "policy", "minutes", "background", "evidence"
  confidence  "high" | "medium" | "low"
  rationale   one sentence naming what in the text decided it"""
CATEGORY_SYSTEM = CATEGORY_SYSTEM.replace(
    "{JSON_RULES_PLACEHOLDER}", f"{JSON_RULES} {LANGUAGE_RULES}"
)


def _category_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError("The category response must be an object.")
    value = str(payload.get("category") or "").strip().lower()
    if value not in CATEGORY_VALUES:
        raise WorkerResponseValidationError(
            "category must be one of " + ", ".join(CATEGORY_VALUES) + "."
        )
    confidence = str(payload.get("confidence") or "").strip()
    if confidence not in {"high", "medium", "low"}:
        raise WorkerResponseValidationError(
            "confidence must be one of high, medium, low."
        )
    return {
        "category": value,
        "confidence": confidence,
        "rationale": str(payload.get("rationale") or "").strip(),
    }


def run_category_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Send one document's opening page and nothing else.

    Deliberately not shown the type catalog. The bucket decides which prompt runs
    next and nothing more, and a worker holding the catalog would be invited to
    answer the type question early — under a partition that has not been settled
    yet, which is the order this stage exists to establish.
    """

    payload = {
        "document_id": str(request.unit_input.get("document_id") or ""),
        "title": str(request.unit_input.get("title") or ""),
        "text": str(request.unit_input.get("text") or ""),
    }
    user = json.dumps(payload, indent=1, default=str)
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
            "worker_kind": "document_category",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(CATEGORY_SYSTEM, user, activity, attempt=attempt.number)


CATEGORY_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.category.response",
    schema_hash=_sha256_text("documents-category-response:category-confidence-rationale"),
    validator=_category_response_schema,
)
CATEGORY_WORKER = WorkerDefinition(
    worker_id=CATEGORY_WORKER_ID,
    # The whole prompt is module-level: unlike the type catalog, the four values
    # are the same in every workspace, so nothing about this question varies per
    # engagement and the prompt hash covers all of it.
    prompt_hash=_sha256_text(CATEGORY_SYSTEM),
    response_schema=CATEGORY_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text("Repair the document category against the four values."),
    ),
    implementation=run_category_worker,
)

WORKERS.register(CATEGORY_WORKER)


# --------------------------------------------------------------------------- #
# document type classification
# --------------------------------------------------------------------------- #
CLASSIFY_WORKER_ID = "documents.classification"
DOCUMENT_CLASSIFICATION_SOURCE_ID = "document_classification"


def _classification_system(catalog: str) -> str:
    return f"""[agent:document_classification]
Name what the supplied document *is*, choosing one id from the catalog below.

Classify by form, not by subject. A purchase order attached to an email is a
purchase_order; the email is correspondence. A document about a payroll dispute
is not a payslip.

Where direction is ambiguous, resolve it from the entity being audited: a demand
for payment addressed *to* the entity is a vendor_invoice; one issued *by* the
entity is a sales_invoice. If the text alone cannot settle it, answer other and
say why rather than guessing.

A document carrying several records — a scanned bundle, a voucher pack — takes
the id of its principal record.

You are shown the opening page only. That is where a document states what it is;
do not infer a type from what a document of some type usually goes on to contain.

{JSON_RULES} {LANGUAGE_RULES}
Keys:
  document_type   one id from the catalog, exactly as written
  document_type_other  short name for the document, required only when the id is
                  other, omitted otherwise
  confidence      "high" | "medium" | "low"
  rationale       one sentence naming what in the text decided it

Catalog:
{catalog}"""


def _classification_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError("The classification response must be an object.")
    document_type = str(payload.get("document_type") or "").strip()
    if not document_type:
        raise WorkerResponseValidationError("The response must name a document_type.")
    confidence = str(payload.get("confidence") or "").strip()
    if confidence not in {"high", "medium", "low"}:
        raise WorkerResponseValidationError(
            "confidence must be one of high, medium, low."
        )
    other = str(payload.get("document_type_other") or "").strip()
    return {
        "document_type": document_type,
        "document_type_other": other,
        "confidence": confidence,
        "rationale": str(payload.get("rationale") or "").strip(),
    }


def validate_classification_proposal(
    proposal: Mapping[str, Any], request: WorkerRequest
) -> Mapping[str, Any]:
    """Check the label against the catalog this unit was actually offered.

    The selectable ids travel on the unit input rather than being read from the
    global catalog here, because a workspace's coined types are part of what the
    prompt offered. Validating against a different list than the model was shown
    would reject a correct answer.
    """

    selectable = {
        str(value) for value in (request.unit_input.get("selectable_types") or [])
    }
    document_type = str(proposal.get("document_type") or "")
    if selectable and document_type not in selectable:
        raise WorkerResponseValidationError(
            f"'{document_type}' is not one of the offered document types."
        )
    if document_type == "other" and not str(proposal.get("document_type_other") or ""):
        raise WorkerResponseValidationError(
            "An 'other' classification must name what the document is."
        )
    if document_type != "other" and proposal.get("document_type_other"):
        # A named id plus free text is two answers. Dropping the text keeps the
        # stored assignment single-valued rather than leaving a second opinion
        # attached to it.
        proposal = {**proposal, "document_type_other": ""}
    return proposal


def run_classification_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Send one document's opening page and the offered catalog."""

    payload = {
        "document_id": str(request.unit_input.get("document_id") or ""),
        "title": str(request.unit_input.get("title") or ""),
        "text": str(request.unit_input.get("text") or ""),
    }
    user = json.dumps(payload, indent=1, default=str)
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
            "worker_kind": "document_classification",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    catalog = str(request.unit_input.get("catalog") or "")
    return gateway.complete(
        _classification_system(catalog), user, activity, attempt=attempt.number
    )


CLASSIFY_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.classification.response",
    schema_hash=_sha256_text(
        "documents-classification-response:document_type-confidence-rationale"
    ),
    validator=_classification_response_schema,
)
CLASSIFY_WORKER = WorkerDefinition(
    worker_id=CLASSIFY_WORKER_ID,
    # The catalog is per-workspace, so it cannot be part of a module-level prompt
    # hash. What is hashed is the instruction text around it; the offered ids
    # travel on the unit input and are covered by the unit's own input hash,
    # which is what re-expands a unit when a coined type changes the catalog.
    prompt_hash=_sha256_text(_classification_system("<catalog>")),
    response_schema=CLASSIFY_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the document classification against the offered catalog ids."
        ),
    ),
    implementation=run_classification_worker,
    semantic_validator=validate_classification_proposal,
)

WORKERS.register(CLASSIFY_WORKER)


# --------------------------------------------------------------------------- #
# shared field vocabularies
# --------------------------------------------------------------------------- #
# The schema store's vocabularies, restated here as ordered tuples because a
# provider enum needs an order and a frozenset has none. ``_schema_field`` is
# what a declared descriptor is validated against at the worker boundary, and
# ``document_schemas.validate_fields`` re-validates the accumulated master at
# commit — same roles, same value types, same errors, one definition each.
_FIELD_ROLES = ("identifier", "party", "attribute", "control")
_VALUE_TYPES = ("identifier", "date", "number", "text", "boolean")


def _schema_field(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError(f"{label} must be an object.")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise WorkerResponseValidationError(f"{label} needs a name.")
    role = str(raw.get("role") or "").strip()
    if role not in _FIELD_ROLES:
        raise WorkerResponseValidationError(f"{label} has an unsupported role '{role}'.")
    value_type = str(raw.get("value_type") or "").strip()
    if value_type not in _VALUE_TYPES:
        raise WorkerResponseValidationError(
            f"{label} has an unsupported value_type '{value_type}'."
        )
    cardinality = str(raw.get("cardinality") or "one").strip()
    if cardinality not in {"one", "many"}:
        raise WorkerResponseValidationError(
            f"{label} has an unsupported cardinality '{cardinality}'."
        )
    confidence = str(raw.get("confidence") or "medium").strip()
    if confidence not in {"high", "medium", "low"}:
        raise WorkerResponseValidationError(
            f"{label} has an unsupported confidence '{confidence}'."
        )
    verbatim = raw.get("verbatim", True)
    if not isinstance(verbatim, bool):
        raise WorkerResponseValidationError(f"{label} needs a boolean verbatim.")
    return {
        "name": name,
        "role": role,
        "value_type": value_type,
        "cardinality": cardinality,
        "verbatim": verbatim,
        "confidence": confidence,
        "label": str(raw.get("label") or "").strip(),
    }


# --------------------------------------------------------------------------- #
# schema-guided extraction
# --------------------------------------------------------------------------- #
STRUCTURED_WORKER_ID = "documents.analysis_structured"
DOCUMENT_STRUCTURED_SOURCE_ID = "document_structured_chunk"


def schema_descriptor(document_type: str, fields: Iterable[Mapping[str, Any]]) -> str:
    """Render one type's frozen schema as prompt text.

    The schema goes into the prompt verbatim, which is what keeps the staleness
    interlock exact: a re-derived schema moves this text, the prompt hash, and
    with it the execution identity, so no proposal built against the old fields
    can be reused under the new ones. It is the same property the pack
    descriptor had, with the vocabulary now coming from the engagement rather
    than from code.
    """

    lines = [f"DOCUMENT TYPE {document_type}", "Fields this type carries:"]
    for field in fields:
        parts = [
            f"  {field.get('name')} — {field.get('value_type')}",
            f"role {field.get('role')}",
        ]
        if str(field.get("cardinality") or "one") == "many":
            parts.append("may appear more than once")
        if not bool(field.get("verbatim", True)):
            parts.append("interpretive; needs no excerpt")
        lines.append("; ".join(parts))
    return "\n".join(lines)


def _structured_system(descriptor: str) -> str:
    return f"""[agent:document_analysis_structured]
Extract what this chunk states, using the fields this document type carries.

Report only what the chunk says. Do not infer a value from a filename, from
metadata, or from what a document of this type usually contains. A field the
chunk does not state is simply absent — never guess one to fill the schema.

{descriptor}

records is an array. Each entry is one record the chunk carries, with:
  fields — the schema fields this record states, as
    {{"name": a field name above, "entry": 1-based ordinal when the field may
      appear more than once, "value": the value exactly as printed,
      "citation": the id of a citation showing it}}
  additional_fields — facts the record states that no field above can hold, as
    {{"name": lower_snake_case, "value_type": one of identifier, date, number,
      text, boolean, "value", "citation"}}

Use additional_fields rather than forcing a fact into a field that does not mean
it. A value put under the wrong name is worse than one recorded outside the
schema: the schema can be widened later, a mislabelled value cannot be found.

Return an empty records array when this chunk carries no record at all. Never
invent a record to fill it.

citations is an array of objects with id, page, and a short exact `excerpt`
copied verbatim from this chunk. Every excerpt must appear character for
character — do not join separate lines, tidy spacing, or paraphrase. Quote the
line carrying the fact and no more: at most {CITATION_EXCERPT_LINES} lines and
{CITATION_EXCERPT_CHARACTERS} characters. Quoting the whole chunk once and
citing it everywhere anchors nothing and is rejected.

Every value you report must carry a citation, except a field marked
interpretive above — those are your reading of the record and routinely use a
word it never prints.

audit_notes records observations visible on the face of the document — a missing
signature or date, an unreferenced attachment, an internal inconsistency, an
alteration, an incomplete field. State the observation and why it matters. Do
not conclude that a control operated or failed. Return an empty array when there
is no such observation.

{JSON_RULES} {LANGUAGE_RULES}"""


def _structured_value(raw: object, label: str, *, extra: bool) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError(f"{label} must be an object.")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise WorkerResponseValidationError(f"{label} needs a name.")
    value = raw.get("value")
    if value is None or str(value).strip() == "":
        raise WorkerResponseValidationError(f"{label} needs a value.")
    entry = raw.get("entry", 1)
    if isinstance(entry, bool) or not isinstance(entry, int) or entry < 1:
        raise WorkerResponseValidationError(f"{label} needs a positive integer entry.")
    record = {
        "name": name,
        "entry": entry,
        # The value as printed. Normalization is applied server-side: asking a
        # model to also return a derived form adds no evidence and one failure
        # mode.
        "value": str(value),
        "citation": str(raw.get("citation") or "").strip(),
    }
    if extra:
        value_type = str(raw.get("value_type") or "text").strip()
        if value_type not in _VALUE_TYPES:
            raise WorkerResponseValidationError(
                f"{label} has an unsupported value_type '{value_type}'."
            )
        record["value_type"] = value_type
    return record


def _structured_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError("The extraction response must be an object.")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise WorkerResponseValidationError("The response must carry a records array.")
    records = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(f"records[{index}] must be an object.")
        fields = [
            _structured_value(item, f"records[{index}].fields[{position}]", extra=False)
            for position, item in enumerate(raw.get("fields") or [])
        ]
        additional = [
            _structured_value(
                item, f"records[{index}].additional_fields[{position}]", extra=True
            )
            for position, item in enumerate(raw.get("additional_fields") or [])
        ]
        if not fields and not additional:
            raise WorkerResponseValidationError(
                f"records[{index}] states nothing; omit it rather than returning it empty."
            )
        records.append({"fields": fields, "additional_fields": additional})
    return {
        "analysis_profile": "structured",
        "records": records,
        "audit_notes": list(payload.get("audit_notes") or []),
        "citations": list(payload.get("citations") or []),
    }


STRUCTURED_SUBMISSION_TOOL = "submit_structured_extraction"


def _structured_submission_tool(field_names: Sequence[str]) -> dict[str, Any]:
    """The provider-enforced shape for one schema-guided extraction.

    The analysis workers have had this since they were written; the document
    workers were given the pieces — ``_citation_submission_tool``,
    ``_submission_response`` — and never wired to them, which is why they are
    the family that returns unparseable JSON. Asking in prose for an object and
    validating it afterwards leaves a bare token where a value belongs, or a
    stray colon between two keys, entirely possible; both were observed here,
    and each cost a document its whole repair allowance.

    ``name`` is an enum of this type's own fields, so "names a field this type
    does not carry" stops being a thing the model can do rather than a thing
    the validator catches. ``additional_fields`` keeps a free name on purpose —
    it exists precisely for facts the schema has no room for.
    """

    stated = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": list(field_names)},
            "entry": {"type": "integer", "minimum": 1},
            "value": {"type": "string", "minLength": 1},
            # Required, and empty where the field is interpretive: demanding a
            # quote for a value the document never prints is unsatisfiable, and
            # that judgement belongs to the schema rather than to this shape.
            "citation": {"type": "string"},
        },
        "required": ["name", "entry", "value", "citation"],
        "additionalProperties": False,
    }
    escaped = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "value_type": {"type": "string", "enum": list(_VALUE_TYPES)},
            "entry": {"type": "integer", "minimum": 1},
            "value": {"type": "string", "minLength": 1},
            "citation": {"type": "string", "minLength": 1},
        },
        "required": ["name", "value_type", "entry", "value", "citation"],
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": STRUCTURED_SUBMISSION_TOOL,
            "description": (
                "Submit every record this chunk states, under the fields this "
                "document type carries. Submit an empty records array only "
                "when the chunk states no record at all."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "records": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "fields": {"type": "array", "items": stated},
                                "additional_fields": {
                                    "type": "array",
                                    "items": escaped,
                                },
                            },
                            "required": ["fields", "additional_fields"],
                            "additionalProperties": False,
                        },
                    },
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "minLength": 1},
                                "page": {"type": "integer", "minimum": 1},
                                "excerpt": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": CITATION_EXCERPT_CHARACTERS,
                                },
                            },
                            "required": ["id", "page", "excerpt"],
                            "additionalProperties": False,
                        },
                    },
                    "audit_notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["records", "citations", "audit_notes"],
                "additionalProperties": False,
            },
        },
    }


def _supplied_structured_chunk(request: WorkerRequest) -> Mapping[str, Any]:
    """The one chunk this extraction was scoped to, with its text.

    ``document_structured_chunk_scope`` supplies exactly this, and the preset
    budgets it above the chunk size so a citation can never bind to text the
    worker only saw truncated. Raising when it is absent keeps that a contract
    rather than a silently empty extraction.
    """

    chunk = _resolved_item(request, DOCUMENT_STRUCTURED_SOURCE_ID)
    if not str(chunk.get("text") or ""):
        raise WorkerContractError(
            "The supplied structured chunk carries no text."
        )
    return chunk


def validate_structured_proposal(
    proposal: Mapping[str, Any], request: WorkerRequest
) -> Mapping[str, Any]:
    """Hold the schema and the citation rule the store also enforces.

    A named field must be one the type actually carries — a value under a field
    the schema does not state cannot be read back by any rule written against
    that schema, so it is a silent loss rather than an extra.
    """

    known = {
        str(field.get("name")): field
        for field in request.unit_input.get("schema_fields") or []
    }
    records = list(proposal.get("records") or [])
    if (
        not records
        and request.unit_input.get("schema_sampled_this_document")
        and request.unit_input.get("sole_chunk")
    ):
        # An empty records array is normally a complete answer: a page of prose
        # inside a transaction document states no record. Not here. This
        # document is one the type's schema was induced *from*, so a sample
        # read its fields off this very text, and this chunk is the whole
        # document — there is no other page the records could be on. Accepting
        # it stored three vouchers as analysed, structured, schema-stamped and
        # empty, which is the silent degradation this profile exists to remove.
        raise WorkerResponseValidationError(
            "records is empty, but this document's own fields are what the "
            f"'{request.unit_input.get('document_type')}' schema was induced "
            "from, and this chunk is the whole document. Extract the record it "
            "states; report only fields the chunk actually prints."
        )
    citations = {
        str(item.get("id"))
        for item in proposal.get("citations") or []
        if isinstance(item, Mapping)
    }
    for index, record in enumerate(records):
        for field in record.get("fields") or []:
            name = str(field.get("name"))
            definition = known.get(name)
            if definition is None:
                raise WorkerResponseValidationError(
                    f"records[{index}] names field '{name}', which this document "
                    "type does not carry. Report it under additional_fields."
                )
            if str(definition.get("cardinality") or "one") == "one" and field["entry"] != 1:
                raise WorkerResponseValidationError(
                    f"Field '{name}' appears once on this type, so entry must be 1."
                )
            if bool(definition.get("verbatim", True)) and not field.get("citation"):
                raise WorkerResponseValidationError(
                    f"Field '{name}' is stated on the record and needs a citation."
                )
            if field.get("citation") and field["citation"] not in citations:
                raise WorkerResponseValidationError(
                    f"Field '{name}' cites '{field['citation']}', which is not a "
                    "citation you declared."
                )
        for field in record.get("additional_fields") or []:
            if known.get(str(field.get("name"))) is not None:
                raise WorkerResponseValidationError(
                    f"'{field.get('name')}' is a field of this type; report it "
                    "under fields rather than additional_fields."
                )
            if not field.get("citation"):
                raise WorkerResponseValidationError(
                    f"Additional field '{field.get('name')}' needs a citation."
                )
    return proposal


def run_structured_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Extract one chunk against its document type's frozen schema."""

    # The chunk itself, not just its identifiers. This worker is asked to
    # extract what the chunk states and to quote it character for character,
    # and it was being sent 68 bytes of ids: document, chunk, page. An empty
    # records array was then the only honest answer available to it, which is
    # exactly what five vouchers returned — and, before the contradiction check
    # above existed, what got stored as their completed structured analysis.
    chunk = _supplied_structured_chunk(request)
    payload = {
        "document_id": str(request.unit_input.get("document_id") or ""),
        "chunk_id": str(request.unit_input.get("chunk_id") or ""),
        "page": request.unit_input.get("page"),
    }
    user = (
        f"{json.dumps(payload, indent=1, default=str)}\n\n"
        f"RAW SOURCE CHUNK:\n{chunk['text']}"
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return a complete corrected JSON object."
        )
        if attempt.previous_response:
            user += "\n\nYOUR PREVIOUS RESPONSE:\n" + attempt.previous_response
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "document_analysis_structured",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    descriptor = str(request.unit_input.get("schema_descriptor") or "")
    field_names = [
        str(field.get("name"))
        for field in request.unit_input.get("schema_fields") or []
        if str(field.get("name") or "")
    ]
    tool = _structured_submission_tool(field_names)
    message = gateway.complete(
        _structured_system(descriptor),
        user,
        activity,
        attempt=attempt.number,
        tools=[tool],
        tool_choice={
            "type": "function",
            "function": {"name": STRUCTURED_SUBMISSION_TOOL},
        },
        return_message=True,
    )
    return submission_response(message, STRUCTURED_SUBMISSION_TOOL)


STRUCTURED_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.analysis_structured.response",
    schema_hash=_sha256_text(
        "documents-structured-response:records-fields-additional-citations"
    ),
    validator=_structured_response_schema,
)
STRUCTURED_WORKER = WorkerDefinition(
    worker_id=STRUCTURED_WORKER_ID,
    # The schema is per-workspace, so it cannot be part of a module-level prompt
    # hash. What is hashed is the instruction text around it; the descriptor
    # travels on the unit input and is covered by the unit's own input hash,
    # which is what re-expands a unit when a re-derived schema changes it.
    prompt_hash=_sha256_text(_structured_system("<schema>")),
    response_schema=STRUCTURED_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the structured extraction against the document type's schema."
        ),
    ),
    implementation=run_structured_worker,
    semantic_validator=validate_structured_proposal,
)

WORKERS.register(STRUCTURED_WORKER)

# --------------------------------------------------------------------------- #
# whole-document evidence read
# --------------------------------------------------------------------------- #
READ_WORKER_ID = "documents.evidence_read"
DOCUMENT_READ_TEXT_SOURCE_ID = "document_pages"
DOCUMENT_READ_IMAGE_SOURCE_ID = "document_page_images"

READ_SUBMISSION_TOOL = "submit_document_reading"


def master_descriptor(
    document_type: str,
    fields: Iterable[Mapping[str, Any]],
    *,
    documents_read: int = 0,
) -> str:
    """Render a type's accumulating master as prompt text.

    The prior art a document is shown: what the documents of its type before it
    settled on, so it reuses those names rather than coining a synonym.
    ``approved_by_id`` and ``approved_by_employee_id`` cannot both enter a master
    that already holds one of them, and this text is where that is decided.

    Fill counts travel with it, and they are not a refinement. A single
    internally-produced confirmation genuinely contributes ``printed_by_name`` to
    its type — harmless as a hint, misleading as a selector — and an authoring
    turn shown names without frequencies read a 0-of-11 field as a comparison it
    could write. One of eighteen would have read exactly the same to it.
    """

    if not list(fields):
        return (
            f"DOCUMENT TYPE {document_type}\n"
            "No document of this type has been read yet, so it carries no fields "
            "and the records[].fields array must stay empty. This document is the "
            "first: every fact it states is a new field. Report all of them under "
            "new_fields, each with a full descriptor — name, role, value_type, "
            "cardinality, verbatim, confidence, label, reason — and a values "
            "array naming every record that states it. Return one entry in "
            "records per record the document carries, each with an empty fields "
            "array."
        )
    lines = [
        f"DOCUMENT TYPE {document_type}",
        f"Read so far: {documents_read} document{'s' if documents_read != 1 else ''}.",
        "Fields documents of this type have stated:",
    ]
    for field in fields:
        parts = [
            f"  {field.get('name')} — {field.get('value_type')}",
            f"role {field.get('role')}",
        ]
        fill = field.get("fill_count")
        if fill is not None:
            parts.append(f"stated by {fill} of {documents_read}")
        if str(field.get("cardinality") or "one") == "many":
            parts.append("may appear more than once")
        if not bool(field.get("verbatim", True)):
            parts.append("interpretive; needs no excerpt")
        lines.append("; ".join(parts))
    return "\n".join(lines)


def _read_system(descriptor: str) -> str:
    return f"""[agent:document_evidence_read]
Read this whole document and report every record it states.

You are given the document's text and, where a page carries no usable text, that
page as an image. A stamp, a signature block, or a countersigned approval is
routinely on a scanned page, and it is evidence like any other — read it.

Report only what the document says. Do not infer a value from a filename, from
metadata, or from what a document of this type usually contains. A field the
document does not state is simply absent — never guess one to fill the
vocabulary.

{descriptor}

The fields listed above are what documents of this type have stated before.
They are prior art, not a record of this document: **report a value for every
one of them this document states**, under records[].fields. A field listed above
that this document does not print is simply absent — but silence about a field
the document does print is a false absence, and absence is what an audit reads
as the finding.

records is an array. Each entry is one record the document carries, with:
  fields — the fields *listed above* that this record states, as
    {{"name": one of the names above, "entry": 1-based ordinal when the field may
      appear more than once, "value": the value exactly as printed,
      "citation": the id of a citation showing it}}

new_fields is for a fact this document states that no field above can hold. Each
entry both *declares* the field for the type and *fills* it wherever this
document states it:
  {{"name": lower_snake_case, "role", "value_type", "cardinality", "verbatim",
    "confidence", "label", "reason": why the type needs this field,
    "values": [{{"record": the 1-based index of the record in records this value
      belongs to, "entry", "value", "citation"}}]}}

Declare each field once, however many records state it, and list one entry in
its values array per record. A statement with twenty transaction lines declares
its columns once and fills each of them twenty times — declaring the same name
twice is refused.

Add a field freely — a document stating something the vocabulary has no place
for is the common case, and it cannot invalidate an earlier reading. But do not
add a field that means what an existing one means under a different name. A
synonym is the one thing this vocabulary cannot absorb: two names for one fact
split every rule written against either of them.

Field names are lower_snake_case and describe the fact, not this instance:
invoice_number, not inv_1042.

role says what a new field is *for*, and is the only part with consequences:
  identifier — a reference that could tie this document to another one. A
               document number, an order number, a contract number. Its
               value_type must be identifier. Choosing this wrongly either
               invents a link or loses one, so state it deliberately.
  party      — a named person, company, or department.
  control    — evidence that someone performed a step: an approval, a signature,
               a checked box, an attachment reference.
  attribute  — anything else the document states.

verbatim is false only when the value does not appear in the document as text —
the *role* a named party plays, the *decision* a signature block represents.
Anything printed on the page is verbatim.

renames is for the rare case where a name above is *wrong* about what the field
holds, as {{"from": the name above, "to": lower_snake_case, "reason": one
sentence}}. Preferring your own phrasing is never grounds: every document has
some reason its wording reads better, and eighteen renames in a row is a
vocabulary that never settles. A rename re-opens every document of this type
already read, because they were read under the old name.

A field being renamed still reports its value under the name listed above — the
rename is applied afterwards.

Never remove a field, and never report a field above under a different meaning.

citations is an array of objects with id, page, and a short exact `excerpt`
copied verbatim from the document. Every excerpt must appear character for
character — do not join separate lines, tidy spacing, or paraphrase. Quote the
line carrying the fact and no more: at most {CITATION_EXCERPT_LINES} lines and
{CITATION_EXCERPT_CHARACTERS} characters. A value you read from a page image
cites that page, with the excerpt as you transcribed it.

Every value you report must carry a citation, except a field marked interpretive
above.

Return an empty records array when this document carries no record at all. A
transaction document stating no record is a truthful reading and must not be
made to invent one.

audit_notes records observations visible on the face of the document — a missing
signature or date, an unreferenced attachment, an internal inconsistency, an
alteration, an incomplete field. State the observation and why it matters. Do
not conclude that a control operated or failed. Return an empty array when there
is no such observation.

{JSON_RULES} {LANGUAGE_RULES}"""


def _read_descriptor_field(raw: object, label: str) -> dict[str, Any]:
    """Validate one declared descriptor, plus the value that introduces it.

    ``_schema_field`` is the validator the sample worker already used and is
    reused unchanged, because the vocabularies — ``FIELD_ROLES``,
    ``VALUE_TYPES``, ``CARDINALITIES`` — are the same ones the schema store
    enforces and a second definition of them would drift. What is added here is
    the half that makes a declaration cost something: a field enters the master
    only by being filled, so the descriptor arrives with the record it appears
    on, its value, and its citation.
    """

    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError(f"{label} must be an object.")
    descriptor = _schema_field(raw, label)
    supplied = raw.get("values")
    if not isinstance(supplied, (list, tuple)) or not supplied:
        raise WorkerResponseValidationError(
            f"{label} declares a field but states no value. A field enters the "
            "vocabulary only by being filled in the document that introduces it."
        )
    values = []
    for position, item in enumerate(supplied):
        where = f"{label}.values[{position}]"
        if not isinstance(item, Mapping):
            raise WorkerResponseValidationError(f"{where} must be an object.")
        record = item.get("record", 1)
        if isinstance(record, bool) or not isinstance(record, int) or record < 1:
            raise WorkerResponseValidationError(
                f"{where} needs the 1-based index of the record it appears on."
            )
        entry = item.get("entry", 1)
        if isinstance(entry, bool) or not isinstance(entry, int) or entry < 1:
            raise WorkerResponseValidationError(
                f"{where} needs a positive integer entry."
            )
        value = item.get("value")
        if value is None or str(value).strip() == "":
            raise WorkerResponseValidationError(f"{where} needs a value.")
        values.append(
            {
                "record": record,
                "entry": entry,
                "value": str(value),
                "citation": str(item.get("citation") or "").strip(),
            }
        )
    return {
        **descriptor,
        "reason": str(raw.get("reason") or "").strip(),
        "values": values,
    }


def _read_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError("The reading response must be an object.")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise WorkerResponseValidationError("The response must carry a records array.")
    records = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(f"records[{index}] must be an object.")
        records.append(
            {
                "fields": [
                    _structured_value(
                        item, f"records[{index}].fields[{position}]", extra=False
                    )
                    for position, item in enumerate(raw.get("fields") or [])
                ]
            }
        )
    new_fields = [
        _read_descriptor_field(item, f"new_fields[{position}]")
        for position, item in enumerate(payload.get("new_fields") or [])
    ]
    renames = []
    for position, raw in enumerate(payload.get("renames") or []):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(
                f"renames[{position}] must be an object."
            )
        source = str(raw.get("from") or "").strip()
        target = str(raw.get("to") or "").strip()
        if not source or not target:
            raise WorkerResponseValidationError(
                f"renames[{position}] needs both 'from' and 'to'."
            )
        renames.append(
            {
                "from": source,
                "to": target,
                "reason": str(raw.get("reason") or "").strip(),
            }
        )
    return {
        "analysis_profile": "structured",
        "records": records,
        "new_fields": new_fields,
        "renames": renames,
        "audit_notes": list(payload.get("audit_notes") or []),
        "citations": list(payload.get("citations") or []),
    }


def _read_submission_tool(master_names: Sequence[str]) -> dict[str, Any]:
    """The provider-enforced shape for one whole-document reading.

    A merge of two shapes that already exist rather than new machinery:
    ``_structured_submission_tool`` constrains ``name`` to an enum of the type's
    fields, so naming a field the type does not carry stops being something the
    model can do; ``_schema_field`` validates a full field descriptor. Reading a
    document and learning what its type carries are now the same act, so the
    call needs both.

    There is no ``additional_fields``. It existed to hold a fact a frozen
    vocabulary had no room for, and under a master there is no such fact — the
    master takes the field. Keeping both channels open would be worse than
    having no master at all: one asks for a full descriptor and a reason, the
    other for a name and a value, and a model offered both reaches for the cheap
    one every time.

    The enum is fixed when the call is made, which is what makes it enforceable.
    A field being renamed in this same response therefore reports its value under
    the name the enum offers, and the commit applies the rename afterwards.
    """

    # A type nothing has been read into yet has no names to offer, and this is
    # the case that has to be closed *structurally* rather than by the validator.
    # Measured on the treasury corpus: with a free-string ``name`` here, seven of
    # eight first-of-type reads put their fields in ``records`` anyway and failed
    # validation — and because a type's first read is what fills its master, the
    # sibling behind it faced the same empty master and failed the same way. The
    # enum is what makes "name a field this type does not carry" impossible, so
    # where there is nothing to enumerate the array itself is closed: with
    # ``maxItems: 0`` the only place a value can go is ``new_fields``, which is
    # where a first document's values belong anyway.
    #
    # The alternative — letting an unknown name through and treating it as an
    # implicit new field — is ``additional_fields`` by another name: a name and a
    # value with no descriptor, no role, and no reason. The descriptor is the
    # point.
    name_property: dict[str, Any] = (
        {"type": "string", "enum": list(master_names)}
        if master_names
        else {"type": "string", "minLength": 1}
    )
    stated = {
        "type": "object",
        "properties": {
            "name": dict(name_property),
            "entry": {"type": "integer", "minimum": 1},
            "value": {"type": "string", "minLength": 1},
            # Required, and empty where the field is interpretive: demanding a
            # quote for a value the document never prints is unsatisfiable.
            "citation": {"type": "string"},
        },
        "required": ["name", "entry", "value", "citation"],
        "additionalProperties": False,
    }
    stated_array: dict[str, Any] = (
        {"type": "array", "items": stated}
        if master_names
        else {"type": "array", "items": stated, "maxItems": 0}
    )
    filled = {
        "type": "object",
        "properties": {
            "record": {"type": "integer", "minimum": 1},
            "entry": {"type": "integer", "minimum": 1},
            "value": {"type": "string", "minLength": 1},
            "citation": {"type": "string", "minLength": 1},
        },
        "required": ["record", "entry", "value", "citation"],
        "additionalProperties": False,
    }
    declared = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "role": {"type": "string", "enum": list(_FIELD_ROLES)},
            "value_type": {"type": "string", "enum": list(_VALUE_TYPES)},
            "cardinality": {"type": "string", "enum": ["one", "many"]},
            "verbatim": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "label": {"type": "string"},
            "reason": {"type": "string"},
            # Declared once, filled on every record that states it. A nostro
            # statement carries one record per transaction line, and a field the
            # type does not carry yet cannot be named in the enum — so without
            # this a multi-record first document could fill only its first
            # record, and the model's only way out was to declare the same field
            # once per record, which the contract refuses.
            "values": {"type": "array", "items": filled, "minItems": 1},
        },
        "required": [
            "name",
            "role",
            "value_type",
            "cardinality",
            "verbatim",
            "confidence",
            "label",
            "reason",
            "values",
        ],
        "additionalProperties": False,
    }
    renamed = {
        "type": "object",
        "properties": {
            "from": dict(name_property),
            "to": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["from", "to", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": READ_SUBMISSION_TOOL,
            "description": (
                "Submit every record this document states, under the fields its "
                "type carries, declaring any field the type does not yet have. "
                "Submit an empty records array only when the document states no "
                "record at all."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "records": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"fields": dict(stated_array)},
                            "required": ["fields"],
                            "additionalProperties": False,
                        },
                    },
                    "new_fields": {"type": "array", "items": declared},
                    "renames": {"type": "array", "items": renamed},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "minLength": 1},
                                "page": {"type": "integer", "minimum": 1},
                                "excerpt": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": CITATION_EXCERPT_CHARACTERS,
                                },
                            },
                            "required": ["id", "page", "excerpt"],
                            "additionalProperties": False,
                        },
                    },
                    "audit_notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "records",
                    "new_fields",
                    "renames",
                    "citations",
                    "audit_notes",
                ],
                "additionalProperties": False,
            },
        },
    }


def _supplied_document_text(request: WorkerRequest) -> Mapping[str, Any]:
    """The whole document's text, which the reading and its citations bind to.

    Raising when it is absent keeps the read's one bound a contract: a document
    that exceeded the window is reported as over-window by the unit generator
    before it gets here, and a read reaching the model with no text would
    produce an empty reading that looks exactly like a document stating nothing.
    """

    supplied = _resolved_item(request, DOCUMENT_READ_TEXT_SOURCE_ID)
    if not str(supplied.get("text") or ""):
        raise WorkerContractError("The supplied document carries no text to read.")
    return supplied


def validate_read_proposal(
    proposal: Mapping[str, Any], request: WorkerRequest
) -> Mapping[str, Any]:
    """Hold the master, the citation rule, and the modification asymmetry.

    The asymmetry is enforced by *cost*, not by judging the reason: whether a
    rename is a genuine correction or a preferred synonym is not a question code
    can settle, and a validator that tried would either wave everything through
    or refuse corrections that were right. So a rename is applied and recorded,
    and it re-opens every prior reading of the type on the same terms a
    late-added field does. What is refused here is only what is structurally
    incoherent.
    """

    # Cardinality is deliberately *not* enforced here, and the schema-guided
    # extraction it replaces did enforce it. Under a frozen schema an ``entry``
    # above one on a ``cardinality: one`` field was a model error, because the
    # schema was settled before the document was read. Under an accumulating
    # master it is evidence: the field's cardinality is a guess made from
    # whichever document introduced it, and a later document stating it twice
    # proves the type carries two. Refusing it charged document one's guess to
    # document two — measured, the second dealing ticket failed outright and
    # blocked its type's stamp. The master widens instead.
    known = {
        str(field.get("name")): field
        for field in request.unit_input.get("master_fields") or []
    }
    records = list(proposal.get("records") or [])
    new_fields = list(proposal.get("new_fields") or [])
    renames = list(proposal.get("renames") or [])
    citations = {
        str(item.get("id"))
        for item in proposal.get("citations") or []
        if isinstance(item, Mapping)
    }

    stated_values = sum(len(record.get("fields") or []) for record in records) + sum(
        len(field.get("values") or []) for field in new_fields
    )
    if not stated_values and citations:
        # Self-contradictory, and the shape a validator can name with certainty.
        # Measured: an FX confirmation returned fifteen citations and not one
        # field value — it plainly read the document and then filled nothing.
        # "This document states no record" cannot be true of a reading that
        # quoted the document fifteen times, so this is refused whatever the
        # master holds, and the genuinely empty reading — no records, no
        # citations — stays the complete answer the plan protects.
        raise WorkerResponseValidationError(
            f"You returned {len(citations)} citation(s) and not one field value. "
            "Every fact you quoted belongs under a field: report the values under "
            "records[].fields where this type already carries the name, and under "
            "new_fields where it does not."
        )
    if not stated_values and not known:
        # An empty ``records`` array is normally a complete answer: an evidence
        # document that carries no transaction record is a truthful reading and
        # must not be made to invent one. Not here. This document is the first of
        # its type, so what it states *is* the type's entire vocabulary — an
        # empty reading leaves every sibling behind it reading against nothing,
        # and the type reaches its stamp with no fields at all. The read saw the
        # whole document, so there is no other page the records could be on.
        #
        # Measured: a counterparty confirmation came back with zero records, zero
        # fields and zero citations, and took its type and its sibling with it.
        # This is the same contradiction the sampled profile refused, asked of
        # the document that now carries the vocabulary rather than the one the
        # sample was drawn from.
        raise WorkerResponseValidationError(
            "This reading states nothing at all, and it is the first document of "
            f"'{request.unit_input.get('document_type')}' — what it states is "
            "the whole vocabulary its type will be read under. Report the fields "
            "this document prints under new_fields, each with a descriptor and "
            "the records that fill it."
        )

    for position, rename in enumerate(renames):
        source = str(rename.get("from"))
        if source not in known:
            raise WorkerResponseValidationError(
                f"renames[{position}] renames '{source}', which this document "
                "type does not carry. Only a field listed above can be renamed."
            )
        target = str(rename.get("to"))
        if target in known and target != source:
            raise WorkerResponseValidationError(
                f"renames[{position}] renames '{source}' to '{target}', which this "
                "type already carries. Two names for one fact is what the "
                "vocabulary exists to prevent."
            )

    for index, record in enumerate(records):
        for field in record.get("fields") or []:
            name = str(field.get("name"))
            definition = known.get(name)
            if definition is None:
                raise WorkerResponseValidationError(
                    f"records[{index}] names field '{name}', which this document "
                    "type does not carry yet. Declare it under new_fields with a "
                    "full descriptor, or use one of the names listed."
                )
            if bool(definition.get("verbatim", True)) and not field.get("citation"):
                raise WorkerResponseValidationError(
                    f"Field '{name}' is stated on the record and needs a citation."
                )
            if field.get("citation") and field["citation"] not in citations:
                raise WorkerResponseValidationError(
                    f"Field '{name}' cites '{field['citation']}', which is not a "
                    "citation you declared."
                )

    renamed_to = {str(item.get("to")) for item in renames}
    for position, field in enumerate(new_fields):
        name = str(field.get("name"))
        if name in known:
            raise WorkerResponseValidationError(
                f"new_fields[{position}] declares '{name}', which this type "
                "already carries. Report its value under fields instead."
            )
        if name in renamed_to:
            raise WorkerResponseValidationError(
                f"new_fields[{position}] declares '{name}', which a rename in this "
                "same response also produces. A split is a rename plus a "
                "different new name."
            )
        for index, value in enumerate(field.get("values") or []):
            where = f"new_fields[{position}].values[{index}]"
            if int(value.get("record") or 1) > len(records):
                raise WorkerResponseValidationError(
                    f"{where} fills record {value.get('record')}, but you "
                    f"returned {len(records)} record(s). A field enters the "
                    "vocabulary only by being filled on a record you reported."
                )
            if bool(field.get("verbatim", True)) and not value.get("citation"):
                raise WorkerResponseValidationError(
                    f"{where} states '{name}' and needs a citation."
                )
            if value.get("citation") and value["citation"] not in citations:
                raise WorkerResponseValidationError(
                    f"{where} cites '{value['citation']}', which is not a "
                    "citation you declared."
                )

    declared_names = [str(field.get("name")) for field in new_fields]
    if len(declared_names) != len(set(declared_names)):
        raise WorkerResponseValidationError(
            "new_fields declares one name twice. Declare a field once and list "
            "every record that states it under its own values array."
        )
    return proposal


def run_read_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Read one whole evidence document against its type's accumulating master."""

    supplied = _supplied_document_text(request)
    # Optional by design. Most evidence is digital, and a document with no
    # visually-routed page supplies none — which is why this is not
    # ``_supplied_media``, whose contract is that prepared media must be there.
    handles = [
        dict(item)
        for item in (
            _plain_json(raw)
            for raw in _source_items(request, DOCUMENT_READ_IMAGE_SOURCE_ID)
        )
        if isinstance(item, Mapping)
    ]
    payload = {
        "document_id": str(request.unit_input.get("document_id") or ""),
        "document_type": str(request.unit_input.get("document_type") or ""),
        "pages": supplied.get("pages"),
        "scanned_pages": sorted({int(item.get("page") or 0) for item in handles}),
    }
    user = (
        f"{json.dumps(payload, indent=1, default=str)}\n\n"
        f"DOCUMENT TEXT:\n{supplied['text']}"
    )
    if handles:
        user += (
            "\n\nThe pages listed under scanned_pages carry no usable text and "
            "are supplied as images. Read them too — a control signature is "
            "routinely the only thing on such a page."
        )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return a complete corrected JSON object."
        )
        if attempt.previous_response:
            user += "\n\nYOUR PREVIOUS RESPONSE:\n" + attempt.previous_response
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "document_evidence_read",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    descriptor = str(request.unit_input.get("master_descriptor") or "")
    master_names = [
        str(field.get("name"))
        for field in request.unit_input.get("master_fields") or []
        if str(field.get("name") or "")
    ]
    message = gateway.complete(
        _read_system(descriptor),
        user,
        activity,
        attempt=attempt.number,
        tools=[_read_submission_tool(master_names)],
        tool_choice={
            "type": "function",
            "function": {"name": READ_SUBMISSION_TOOL},
        },
        return_message=True,
    )
    return submission_response(message, READ_SUBMISSION_TOOL)


READ_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="documents.evidence_read.response",
    schema_hash=_sha256_text(
        "documents-evidence-read-response:records-new-fields-renames-citations"
    ),
    validator=_read_response_schema,
)
READ_WORKER = WorkerDefinition(
    worker_id=READ_WORKER_ID,
    # The master is per-workspace and moves as the type is read, so it cannot be
    # part of a module-level prompt hash. What is hashed is the instruction text
    # around it; the descriptor travels on the unit input and is covered by the
    # unit's own input hash, which is what re-expands a read when the vocabulary
    # its siblings settled has moved.
    prompt_hash=_sha256_text(_read_system("<master>")),
    response_schema=READ_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=workflow.READ_REPAIR_ATTEMPTS,
        guidance_hash=_sha256_text(
            "Repair the document reading against the type's accumulating master."
        ),
    ),
    implementation=run_read_worker,
    semantic_validator=validate_read_proposal,
)

WORKERS.register(READ_WORKER)


__all__ = [
    "CHUNK_ANALYSES_SOURCE_ID",
    "CHUNK_RESPONSE_SCHEMA",
    "CHUNK_SYSTEM",
    "CHUNK_WORKER",
    "CHUNK_WORKER_ID",
    "DOCUMENT_CHUNK_SOURCE_ID",
    "DOCUMENT_METADATA_SOURCE_ID",
    "DOCUMENT_READ_IMAGE_SOURCE_ID",
    "DOCUMENT_READ_TEXT_SOURCE_ID",
    "DOCUMENT_VISUAL_SOURCE_ID",
    "READ_RESPONSE_SCHEMA",
    "READ_SUBMISSION_TOOL",
    "READ_WORKER",
    "READ_WORKER_ID",
    "REDUCTION_RESPONSE_SCHEMA",
    "REDUCTION_SYSTEM",
    "REDUCTION_WORKER",
    "REDUCTION_WORKER_ID",
    "VISUAL_RESPONSE_SCHEMA",
    "VISUAL_SYSTEM",
    "VISUAL_WORKER",
    "VISUAL_WORKER_ID",
    "document_metadata",
    "master_descriptor",
    "run_chunk_worker",
    "run_read_worker",
    "run_category_worker",
    "run_classification_worker",
    "run_structured_worker",
    "run_reduction_worker",
    "run_visual_worker",
    "validate_chunk_proposal",
    "validate_read_proposal",
    "validate_classification_proposal",
    "validate_structured_proposal",
    "validate_reduction_proposal",
    "validate_visual_proposal",
]
