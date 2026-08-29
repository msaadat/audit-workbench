"""Registered model workers for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import cycle_rulesets, cycle_vouching
from ...text import counted, relevance_tokens
from .. import prompts
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
    decode_json_response,
)


APM_WORKER_ID = "planning.apm"
APM_SYSTEM = """[agent:apm]
Draft an audit planning memorandum grounded only in the supplied planning
basis. Document content and methodology excerpts may be present. Methodology
must be cited by pack/version/section; where no methodology excerpt is
supplied, record that none was available to this engagement and attribute the
absence to the methodology material, not to any other source. Preserve the
selected Markdown template's structure. Where a fact is unavailable, do not
leave the raw {{placeholder}} token — replace it with a short italic note
such as _[entity — context not available]_ so the reader knows the information
was missing; clearly label assumptions.

An analysis summary may be supplied: the memorandum written from this
engagement's own exploratory data analysis. Where it is present, it is evidence
about this population rather than background, so let it carry the analytics
section and inform the risk assessment — a risk the data has already evidenced
or contradicted should be described as such, not restated as an open question.
Do not repeat the summary wholesale; take from it what bears on planning. Where
no summary is supplied, say plainly that no data analysis has been performed
rather than implying coverage that does not exist.

A population summary may be supplied: for each imported table, its row count,
the observed range of each date column and the total of each valued column. Use
it to state the size of what the engagement covers — records received and
amounts at stake — rather than describing the populations without their scale.

Where the planning context states no audit period, propose one from the observed
ranges: take it from the columns that carry the entity's transactions, not from
master-data columns such as hire or record-creation dates, name the columns it
came from, and mark it for confirmation with the auditee. It is an observation
about the data received, not an asserted scope — say which. Do not report the
period as unavailable when dated populations were supplied, and do not infer one
where no range was.

Every section the template declares must be answered. A consideration that does
not apply to this engagement is recorded as considered and not applicable, with
the reason; an empty section is not the same as a dismissed one.

Return the memorandum as Markdown only, without a JSON wrapper or Markdown code
fence."""

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_UNAVAILABLE = r"\b(?:not available|not defined|undefined)\b"


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


def _supplied_items(request: WorkerRequest, source_id: str) -> tuple[object, ...]:
    return tuple(
        item.content for item in request.context.items if item.source_id == source_id
    )


def _section_bodies(markdown: str) -> dict[str, str]:
    """Each heading's body, taken to the next heading of the same or higher level.

    Level-tolerant on both sides, because the template-coverage check is: a
    template's ``##`` answered by a memo's ``#`` is the same section. Depth-aware
    so that a section written entirely as subsections is answered rather than
    empty — a ``##`` body runs through its ``###`` children and stops at the
    next ``##``.
    """
    text = _HTML_COMMENT.sub("", str(markdown or ""))
    matches = list(_HEADING.finditer(text))
    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = len(text)
        for following in matches[index + 1 :]:
            if len(following.group(1)) <= level:
                end = following.start()
                break
        bodies.setdefault(
            match.group(2).strip().casefold(), text[match.end() : end].strip()
        )
    return bodies


def _context_without_sources(
    request: WorkerRequest,
    *source_ids: str,
) -> dict[str, Any]:
    """Serialize context without items already promoted in the model prompt."""
    context = request.context.to_dict()
    excluded = set(source_ids)
    context["items"] = [
        item
        for item in context["items"]
        if item.get("source_id") not in excluded
    ]
    return context


def _fill_unavailable_placeholders(markdown: str) -> str:
    def replace(match: re.Match[str]) -> str:
        label = match.group(1).replace("_", " ").strip()
        return f"_[{label} - context not available]_"

    return _PLACEHOLDER.sub(replace, markdown)


def _response_schema(response: str) -> Mapping[str, Any]:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:markdown|md)?\s*\n?(.*?)\n?```",
        value,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        value = fenced.group(1).strip()
    if value.startswith("{"):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "apm_markdown" in payload:
            return {"apm_markdown": str(payload.get("apm_markdown") or "")}
        marker = re.search(r'["\']apm_markdown["\']\s*:\s*["\']', value)
        if marker:
            body = value[marker.end() :].strip()
            body = re.sub(r'["\']\s*}\s*$', "", body, count=1).strip()
            try:
                value = json.loads(f'"{body}"').strip()
            except json.JSONDecodeError:
                value = body.replace(r"\n", "\n").replace(r'\"', '"').strip()
    heading = re.search(r"(?m)^#{1,6}\s+", value)
    if heading:
        value = value[heading.start() :].strip()
    return {"apm_markdown": value}


def validate_apm_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Validate template coverage and structured planning contradictions."""
    markdown = _fill_unavailable_placeholders(
        str(proposal.get("apm_markdown") or "").strip()
    )
    if not markdown:
        raise WorkerResponseValidationError("the memorandum is empty")
    template = str(_resolved_item(request, "apm_template") or "")
    headings = {
        match.group(1).strip().casefold()
        for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", markdown, re.MULTILINE)
    }
    required = [
        match.group(1).strip().casefold()
        for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", template, re.MULTILINE)
    ]
    missing = [heading for heading in required if heading not in headings]
    if missing:
        raise WorkerResponseValidationError(
            f"missing template section '{missing[0]}'"
        )
    planning = _resolved_item(request, "planning_context")
    if not isinstance(planning, dict):
        raise WorkerContractError("APM planning context must be an object.")
    structured = (
        planning.get("context")
        if isinstance(planning.get("context"), dict)
        else planning
    )
    # A declared section may be answered with an unavailable note — that
    # mechanism is deliberate and honest. What it may not be is a heading with
    # nothing under it, which reads as covered and is not.
    bodies = _section_bodies(markdown)
    for heading in _section_bodies(template):
        if not re.search(r"[A-Za-z0-9]", bodies.get(heading, "")):
            raise WorkerResponseValidationError(
                f"template section '{heading}' is present but has no content"
            )
    normalized = re.sub(r"\s+", " ", markdown.casefold())
    # Proximity over the whole memo, so this holds only for fields whose absence
    # the structured context contradicts outright. The period was such a gate and
    # is no longer: it is proposed from observed ranges rather than asserted, a
    # wrong one is corrected in place by the auditor, and the scan discarded a
    # complete valid memo when "in the audit period; if not available" — prose
    # about whether evidence exists, twenty thousand characters from the
    # Engagement section — read as the memo disowning its own period. Proposing
    # a period is steered by APM_SYSTEM instead of gated here.
    for field_name in ("objective", "scope"):
        if structured.get(field_name) and re.search(
            rf"\b{field_name}\b.{{0,80}}{_UNAVAILABLE}",
            normalized,
        ):
            raise WorkerResponseValidationError(
                f"the memorandum says {field_name} is unavailable despite structured context"
            )
    return {"apm_markdown": markdown}


def run_apm_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    template = str(_resolved_item(request, "apm_template") or "")
    current_apm = str(_resolved_item(request, "current_apm") or "")
    _resolved_item(request, "planning_context")
    user = json.dumps(
        {
            "ACTIVE APM TEMPLATE (verbatim)": template,
            "CURRENT APM TO REVISE": current_apm,
            "RESOLVED CONTEXT": _context_without_sources(
                request,
                "apm_template",
                "current_apm",
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        # The rejected draft goes back with the errors. Without it the repair is
        # a fresh generation that happens to know one thing the last one got
        # wrong: a memo that failed a single gate is rewritten end to end, and
        # every section that had passed is re-rolled. ``CURRENT APM TO REVISE``
        # cannot stand in for it — that slot carries the committed artifact,
        # which is empty on a first draft, exactly when a repair is likeliest.
        if attempt.previous_response:
            user += "\n\nPREVIOUS APM DRAFT:\n" + attempt.previous_response
        user += (
            "\n\nThe previous APM draft failed the engagement quality gate: "
            + "; ".join(attempt.validation_errors)
            + ". Correct that draft and return the complete corrected "
            "memorandum, keeping the sections that did not fail."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "apm",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        APM_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


APM_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.apm.response",
    schema_hash=_sha256_text("apm-response:non-empty-template-complete-markdown"),
    validator=_response_schema,
)
APM_WORKER = WorkerDefinition(
    worker_id=APM_WORKER_ID,
    prompt_hash=_sha256_text(APM_SYSTEM),
    response_schema=APM_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair missing APM template sections and structured-context contradictions."
        ),
    ),
    implementation=run_apm_worker,
    # The one worker whose response is not JSON. Asking the provider to
    # constrain it produced a complete memorandum under a key of the model's
    # own choosing, and the template check failed on an empty document.
    json_response=False,
    semantic_validator=validate_apm_proposal,
)

WORKERS.register(APM_WORKER)


# --------------------------------------------------------------------------- #
# planning.rcm worker (P7C)
# --------------------------------------------------------------------------- #
RCM_WORKER_ID = "planning.rcm"
RCM_SYSTEM = f"""[agent:rcm]
Revise the current risk and control matrix using durable RCM ids. Return an object with `rows`, each
row containing operation (update|create), rcm_id for updates, process, risk,
risk_rating (low|medium|high|critical), control, control_type, and
control_attributes,
plus criteria and control_owner where the planning basis supports them.
All ids and narrative fields are strings. business_cycle is derived locally
from validated transaction-cycle attributes; do not infer or return it.
Describe the risk and the control. Test populations are decided later.
Do not invent control operation as fact when evidence is absent.

Follow the ACTIVE RCM TEMPLATE for methodology. Its non-negotiable rules:
- Cover the standard risks a competent auditor would consider for every in-scope
  process, drawn from your own knowledge of the cycle. Supplied observations
  refine the risk set; they never define it. Never emit a row whose only content
  is a restatement of a supplied observation or audit note.
- Every risk theme the planning memorandum organises its assessment under must
  be owned by a row. Where the memorandum assesses fraud risk and management
  override, the override patterns it names are risks in their own right —
  circumventing an approval threshold by dividing a commitment, paying the same
  obligation twice, and directing payment to a party the entity has not
  approved are matrix rows, not narrative. A theme the memo plans a response
  for and the matrix omits is a planned procedure that will never run.
- Write risks in generic, condition-independent auditor wording. Never quote
  percentages, counts, null rates, column names, or file names in a risk, never
  embed the cause, and never pre-conclude that a deficiency exists.
- The control field records a control management asserts is in place. Where none
  exists, write "No control identified" rather than describing the control that
  ought to exist. Never assert that a system enforces, prevents, blocks, or
  validates something unless the planning basis states it: a field existing in a
  table shows a value is recorded, never that it is controlled.
- The risk wording rules apply to the control field too. No percentages, null
  rates, counts, or column names, and no appended deficiency clause.
- control_attributes is a non-empty array. Each entry has exactly key,
  assertion, requirement, and evidence_kind — and nothing else. Each describes
  one distinct requirement of this same asserted control. Assertions use exactly
  Existence, Completeness, Accuracy, Authorization, Valuation, Cut-off,
  Compliance, or Operational. Attribute keys are unique within the row. Do not
  split one risk/control into extra rows merely because it has several
  attributes.
- Enumerate the control's requirements; do not collapse a control to one
  attribute out of habit. A control described as matching an invoice to its PO
  and receipt before payment carries separate requirements for the match, for
  receipt preceding payment, and for the amount agreeing. Where the control
  genuinely asserts one thing, one attribute is correct.
- evidence_kind names where the evidence for that requirement actually lives.
  Choose it from the supplied material, not from the requirement's wording:
    tabular_population  the imported tables can answer it across the whole
                        population — uniqueness, null or missing values,
                        thresholds, date ordering, status combinations,
                        counts, or a value compared against another column.
                        This is the default whenever a table carries the fields
                        the requirement names.
    transaction_cycle   the answer needs several *source records* of different
                        registered record kinds linked by transaction
                        identifiers. A denormalized table repeating values from
                        two source records does not replace vouching those source
                        records when the requirement is their agreement.
    document_content    one document states the requirement or the fact.
    manual_inspection, inquiry, mixed  no imported evidence answers it.
  Prefer tabular_population for population-level completeness, uniqueness,
  threshold, and status tests. For agreement between distinct source records,
  retain a transaction_cycle attribute when source-record vouching is required;
  add a separate tabular_population attribute if the table can also provide
  broader population assurance. Do not use inquiry for something supplied
  tables can measure.
- A row whose control field says "No control identified" still chooses
  evidence_kind from the supplied material. Where the imported tables carry the
  fields the requirement names, that is tabular_population regardless of
  whether a control is asserted: testing the population is how the absence of
  the control is evidenced, and it is the only evidence there is. Reserve
  inquiry for a requirement no supplied table and no supplied document can
  answer.
- A transaction_cycle attribute states evidence_kind and stops there. Do not
  write registry, required_record_kinds, required_comparisons, or
  comparison_recipes: the evidence contract for those attributes is authored in a
  separate step that is given the installed pack catalog, and the row's
  business_cycle is derived from it locally.
- criteria and control_owner are optional: cite or name only what the planning
  basis supplies, and leave the field empty otherwise rather than guessing.
- Where criteria rests on a supplied document, also set criteria_refs, choosing
  from CITABLE DOCUMENTS: one entry per document, `document` its `ref` number
  and `citations` the `[C...]` ids you are relying on. Cite only ids listed for
  that ref. Never write a document id. Omit criteria_refs where the criterion
  does not rest on a supplied document.
- Supplied table profiles are value-free shape statistics, not evidence. A null
  percentage is not an exception rate; a maximum is not a policy limit.
- One risk and one control per row. {JSON_RULES}"""


#: The evidence pass: what must agree, for attributes already judged to need
#: linked source records.
#:
#: The vocabulary is not in this prompt. It is per-workspace and travels on the
#: unit input, so the prompt hash stays stable while the catalog varies, and a
#: re-derived schema moves the unit's input hash instead.
RCM_SCHEMA_EVIDENCE_SYSTEM = f"""[agent:rcm_schema_evidence]
Say what must agree, for control attributes already judged to need linked source
records. The risk, control, and requirement are settled: do not revise them, and
do not add or remove attributes.

You are shown this engagement's document types and the fields each one states.
Those fields are the whole vocabulary. A comparison naming anything else cannot
be evaluated and will be refused.

Return an object with `contracts`, one entry per supplied attribute, each with
row_index and attribute_key copied exactly from the request, plus:
- required_comparisons: a non-empty array of objects, each with
  - key: a short snake_case name for this comparison, unique within the attribute
  - left: {{{{"document_type": "<type>", "field": "<field>"}}}}
  - right: the same shape, omitted only for `present`
  - operator: one of {', '.join(sorted(cycle_rulesets.OPERATORS))}
  - tolerance: omitted, or {{{{"absolute": n}}}} / {{{{"percent": n}}}} for
    numeric_within, or {{{{"days": n}}}} for date_within
  - rationale: one sentence on why the requirement needs this to hold

State only what the requirement itself asks. A comparison that is merely nearby
— the vendor names agreeing when the requirement is about amounts — proves
something else, and a control covered by it reads as tested when it is not.

If the requirement cannot be expressed over the fields shown, say so by
returning `unsupported: true` with a one-line reason instead of the contract
fields. The attribute's evidence strategy is then reconsidered, which is the
honest outcome; inventing a comparison over a field that does not exist is not.
{JSON_RULES}"""


RCM_CURRENT_ROWS_SOURCE_ID = "current_rcm"
# Keys a proposed row may carry into normalization. Anything else — a rationale,
# a suggested procedure, a confidence note — is dropped rather than rejected: the
# workspace already discards unknown row keys, and failing a whole row over a
# harmless extra narrative field would trade a real defect class for a
# manufactured one. Placement *inside* the evidence contract is different and is
# rejected, because there a misplaced key silently changes what the row asserts.
_RCM_ROW_KEYS = frozenset(
    {
        "operation",
        "rcm_id",
        "process",
        "risk",
        "risk_rating",
        "control",
        "control_type",
        "control_attributes",
        "criteria",
        "criteria_refs",
        "control_owner",
    }
)

# The source id the RCM scope supplies engagement documents under.
RCM_DOCUMENT_SOURCE_ID = "documents"
# Citation anchors are authored by document analysis as `[c7]` markers inside
# the summary a worker reads, so the ids a row may cite are exactly the ids
# present in the text it was shown.
#
# Either case, because a marker's case carries no information and the worker
# that writes them says so: ``documents._citation_case_map`` folds every variant
# onto the supplied id rather than rejecting the difference. Recognizing one
# spelling here did not reject anything — it silently recognized *nothing*. Every
# marker in one live engagement was lower case, so the sheet found no citations
# in any document, skipped all three, and handed the model an empty register;
# it noticed ("CITABLE DOCUMENTS says [] yet documents summaries have [c1]") and
# correctly wrote a matrix citing no criteria at all.
_CITATION_MARKER = re.compile(r"\[([Cc]\d+)\]")
# One initial call plus this many correction turns, mirrored into the registered
# repair policy below. The worker needs it to know which attempt is its last, and
# therefore when an unrepairable row should be quarantined rather than sink the
# whole document.
_RCM_MAX_REPAIR_ATTEMPTS = 1
_RCM_REQUIRED_FIELDS = (
    "process",
    "risk",
    "risk_rating",
    "control_attributes",
    "control",
    "control_type",
)
_RCM_RISK_RATINGS = {"low", "medium", "high", "critical"}


def rcm_citation_sheet(request: WorkerRequest) -> list[dict[str, Any]]:
    """The numbered register an RCM row cites its criteria from.

    One entry per supplied document, in bundle order, carrying the file's name
    and the citation ids that actually appear in the text the worker was shown.
    A worker chooses a ``ref`` and a citation id; it never writes a document id,
    which is the ten-hex-character token that
    ``docs/memo-structured-references.md`` established models corrupt.
    """
    sheet: list[dict[str, Any]] = []
    for number, item in enumerate(
        (
            entry
            for entry in request.context.items
            if entry.source_id == RCM_DOCUMENT_SOURCE_ID
            and str(entry.source_ref or "").startswith("document:")
        ),
        start=1,
    ):
        text = str(item.content or "")
        citations = sorted(
            set(_CITATION_MARKER.findall(text)),
            key=lambda value: int(value[1:]),
        )
        if not citations:
            continue
        sheet.append({
            "ref": number,
            "document": prompts.summary_document_name(text)
            or item.source_ref.split(":", 1)[1],
            "document_id": item.source_ref.split(":", 1)[1],
            "citations": citations,
        })
    return sheet


def _validated_criteria_refs(
    row: Mapping[str, Any], index: int, sheet: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve a row's cited refs, rejecting anything the sheet cannot support.

    An out-of-range ref is not a mistake to tolerate: a criterion that points at
    the wrong document is worse than one that points nowhere, so it is raised
    into the worker's own repair loop rather than dropped.
    """
    value = row.get("criteria_refs")
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise WorkerResponseValidationError(
            f"RCM row {index} criteria_refs must be an array"
        )
    by_ref = {entry["ref"]: entry for entry in sheet}
    resolved: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise WorkerResponseValidationError(
                f"RCM row {index} criteria_refs entries must be objects"
            )
        try:
            ref = int(entry.get("document"))
        except (TypeError, ValueError):
            raise WorkerResponseValidationError(
                f"RCM row {index} criteria_refs entry needs a numeric document ref"
            ) from None
        supplied = by_ref.get(ref)
        if supplied is None:
            raise WorkerResponseValidationError(
                f"RCM row {index} cites document ref {ref}, which was not supplied; "
                f"available refs are {sorted(by_ref) or 'none'}"
            )
        citations = entry.get("citations")
        if not isinstance(citations, list) or not citations:
            raise WorkerResponseValidationError(
                f"RCM row {index} criteria_refs entry for ref {ref} needs citations"
            )
        # Folded onto the register's own spelling, for the same reason the
        # recognizer above accepts both: a row that answers `[C4]` to a sheet
        # listing `c4` has cited the right sentence, and spending the repair
        # allowance on the difference corrects nothing an auditor would read.
        allowed = {
            str(value).casefold(): str(value) for value in supplied["citations"]
        }
        for citation in citations:
            supplied_id = str(citation or "").strip()
            identifier = allowed.get(supplied_id.casefold())
            if identifier is None:
                raise WorkerResponseValidationError(
                    f"RCM row {index} cites {supplied_id} in "
                    f"'{supplied['document']}', which does not carry it"
                )
            resolved.append({
                "document_id": supplied["document_id"],
                "document": supplied["document"],
                "citation_id": identifier,
            })
    # One row citing the same anchor twice is a duplicate, not two sources.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in resolved:
        key = (item["document_id"], item["citation_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _current_rcm_rows(request: WorkerRequest) -> list[object]:
    return [
        item.content
        for item in request.context.items
        if item.source_id == RCM_CURRENT_ROWS_SOURCE_ID
    ]


def _plain_json(value: object) -> object:
    """Deep-copy frozen proposal values back to plain JSON containers."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _canonical_rcm_id(value: object) -> str:
    """Return the durable id from a bare id or a typed ``rcm:<id>`` reference."""
    text = str(value or "").strip()
    if not text:
        return ""
    prefix, separator, item_id = text.partition(":")
    if not separator:
        return text
    if prefix != "rcm" or not item_id:
        raise ValueError(f"'{text}' is not an RCM reference")
    return item_id


def _rcm_response_schema(response: str) -> Mapping[str, Any]:
    payload = _first_json_object(response)
    if not isinstance(payload.get("rows"), list):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a `rows` array"
        )
    parsed: dict[str, Any] = {"rows": payload["rows"]}
    # Rows the worker could not repair within its bounded allowance, carried so
    # the executor can record them for the auditor instead of the run failing
    # and discarding every row that was correct.
    if isinstance(payload.get("quarantined"), list):
        parsed["quarantined"] = payload["quarantined"]
    return parsed


# The test worker ranks a table for a row as 4×(table-name hits) + (column-name
# hits), and this gate must agree with it: a row may not claim no table can
# answer its requirement when the same scorer would have ranked one into the
# generation prompt. The bar is one table-name hit — a table *named* for the
# subject — or the four column hits worth as much. One stray column word is
# not enough; "date" and "amount" occur in every ledger ever imported.
TABLE_NAME_WEIGHT = 4
MIN_TABULAR_RELEVANCE = TABLE_NAME_WEIGHT
# How many words two phrases must share before one is held to be about the
# other, where no table name is involved.
MIN_TOKEN_MATCH = 2
# What it takes for a matrix row to answer a memo's risk theme. Deliberately
# one shared stem, because a memo names the *technique* and a matrix names the
# *risk condition*: "Circumvention through transaction splitting" is answered
# by "a commitment may be divided across related requisitions to circumvent
# approval", and those two phrases share exactly one word. Requiring two
# rejected a 27-row matrix that covered every theme it was accused of missing,
# and failed the run outright. A theme sharing nothing at all with any row is
# still a real signal; anything above that is not one this can measure.
MIN_THEME_MATCH = 1


# A table smaller than this is a reference list, not a population: the audit
# reads *through* it rather than testing it. The same floor the analysis stage
# uses to decide a frame is a lookup. Without it the four-row approval matrix
# answered every row whose risk mentioned approval — eight of twenty-nine in
# one run — because its name alone carries the word.
MIN_TESTABLE_POPULATION_ROWS = 5


def _tabular_answers(request: WorkerRequest) -> list[tuple[str, set[str], set[str]]]:
    """Each testable population as the words in its name and its column names."""
    return tabular_answers(_supplied_items(request, "table_profiles"))


def tabular_answers(profiles) -> list[tuple[str, set[str], set[str]]]:
    """The same view of the populations, from profiles a caller already holds."""
    answers = []
    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        rows = profile.get("rows")
        if not isinstance(rows, int) or rows < MIN_TESTABLE_POPULATION_ROWS:
            continue
        table = str(profile.get("table") or "")
        columns: set[str] = set()
        for column in profile.get("columns") or []:
            if isinstance(column, Mapping):
                columns |= relevance_tokens(column.get("name"))
        if table and columns:
            answers.append((table, relevance_tokens(table), columns))
    return answers


def _answering_table(
    row: Mapping[str, Any],
    answers: list[tuple[str, set[str], set[str]]],
):
    """The supplied table most plainly about this row, if any is."""
    query: set[str] = set()
    for key in ("process", "risk", "control", "criteria"):
        query |= relevance_tokens(row.get(key))
    for attribute in row.get("control_attributes") or []:
        if isinstance(attribute, Mapping):
            query |= relevance_tokens(attribute.get("key"))
            query |= relevance_tokens(attribute.get("requirement"))
    for table, name_tokens, column_tokens in answers:
        matched = (query & name_tokens) | (query & column_tokens)
        score = TABLE_NAME_WEIGHT * len(query & name_tokens) + len(
            query & column_tokens
        )
        if score >= MIN_TABULAR_RELEVANCE:
            return table, sorted(matched)
    return None


def _untested_population(
    row: Mapping[str, Any],
    answers: list[tuple[str, set[str], set[str]]],
):
    """The table that may answer a row asserting that none can.

    A row with no ``tabular_population`` attribute tells the test worker to
    withhold every table schema, and without schemas ``data`` never enters the
    allowed variants — so the row is not under-tested, it is untestable by
    construction. Absence of a control is precisely when the population matters
    most: it is the only evidence left.

    Reported, never enforced. This began as a rejection and failed three
    consecutive regenerations on rows that were right: competitive bidding,
    vendor due diligence and ERP configuration are documentary because no
    column in the engagement holds a quotation, a due-diligence file or a role
    assignment — yet each names a business noun that some table is named for.
    Measured across 71 attributes of one matrix, column overlap ran 0-4 for
    ``tabular_population`` and 0-5 for ``manual_inspection``: the classes do
    not separate, so no threshold over this signal can carry a veto. The
    classification itself is now the prompt's job, and it does it — that run
    produced 32 population attributes and no inquiry at all.
    """
    attributes = [
        item
        for item in row.get("control_attributes") or []
        if isinstance(item, Mapping)
    ]
    if not attributes:
        return None
    if any(
        item.get("evidence_kind") == "tabular_population"
        or (item.get("evidence_kind") == "transaction_cycle" and item.get("registry"))
        for item in attributes
    ):
        return None
    return _answering_table(row, answers)


def untested_population_rows(
    profiles,
    rows: list[Mapping[str, Any]],
) -> list[str]:
    """Rows an auditor should check are not testable from the populations."""
    answers = tabular_answers(profiles)
    flagged = []
    for row in rows:
        answered = _untested_population(row, answers)
        if answered:
            flagged.append(f"{row.get('risk') or row.get('process')} ({answered[0]})")
    return flagged


def _partition_rcm_rows(
    rows: object,
    request: WorkerRequest,
) -> tuple[list[dict], list[dict]]:
    """Split proposed rows into normalized-valid and failed-with-reasons.

    Rows are independent artifacts: the executor matches and commits them one at
    a time. Validating them as a single document meant one unsupported operator
    in one comparison of one row discarded twelve correct rows, so the partition
    is what both the repair prompt and the quarantine decision are built on.
    """

    existing_ids = {
        str(row.get("id"))
        for row in _current_rcm_rows(request)
        if isinstance(row, Mapping) and row.get("id")
    }
    answers = _tabular_answers(request)
    sheet = rcm_citation_sheet(request)
    normalized: list[dict] = []
    failures: list[dict] = []
    for index, row in enumerate(rows or (), start=1):
        # Both callers reach here with different containers: the registered
        # response schema freezes its proposal before the semantic validator
        # runs, so every array in it arrives as a tuple, while the worker's own
        # pre-serialization pass calls this with the plain output of
        # ``json.loads``. Normalizing once, here, is what makes the two agree.
        # They disagreed on a live matrix: nine rows whose ``criteria_refs``
        # was a tuple failed an ``isinstance(..., list)`` the worker's own pass
        # could never see, so the quarantine never engaged and all twenty-seven
        # rows were discarded over citations that were correct.
        row = _plain_json(row)
        try:
            normalized.append(
                _normalized_rcm_row(row, index, existing_ids, answers, sheet)
            )
        except WorkerResponseValidationError as error:
            failures.append(
                {
                    "index": index,
                    "row": row,
                    "errors": list(error.errors),
                }
            )
    return normalized, failures


def _normalized_rcm_row(
    row: object,
    index: int,
    existing_ids: set[str],
    tabular_answers: list[tuple[str, set[str]]] | None = None,
    citation_sheet: list[dict[str, Any]] | None = None,
) -> dict:
    """Validate and normalize exactly one proposed RCM row."""

    if not isinstance(row, Mapping):
        raise WorkerResponseValidationError(f"RCM row {index} is not an object")
    missing = [key for key in _RCM_REQUIRED_FIELDS if not row.get(key)]
    if missing:
        raise WorkerResponseValidationError(
            f"RCM row {index} is missing {missing[0]}"
        )
    non_string = [
        key for key in _RCM_REQUIRED_FIELDS
        if key != "control_attributes" and not isinstance(row.get(key), str)
    ]
    if non_string:
        raise WorkerResponseValidationError(
            f"RCM row {index} field {non_string[0]} must be a string"
        )
    if str(row.get("risk_rating")).casefold() not in _RCM_RISK_RATINGS:
        raise WorkerResponseValidationError(
            f"RCM row {index} has an unsupported risk rating; it must be exactly "
            f"one of {', '.join(sorted(_RCM_RISK_RATINGS))}"
        )
    try:
        attributes = cycle_vouching.validate_control_attributes(
            _plain_json(row.get("control_attributes"))
        )
    except cycle_vouching.CycleSchemaError as error:
        # Every independent attribute and comparison violation, each carrying its
        # own path, rather than the first one found.
        raise WorkerResponseValidationError(
            [f"RCM row {index}: {message}" for message in error.errors]
        ) from error
    # A row's business cycle is a label the matrix chose, not a projection of
    # anything the engine owns.
    expected_cycle = str(row.get("business_cycle") or "").strip()
    operation = str(row.get("operation") or "").strip().lower()
    if operation not in {"update", "create"}:
        raise WorkerResponseValidationError(
            f"RCM row {index} has an unsupported operation; it must be exactly "
            "'update' or 'create'"
        )
    if operation == "update":
        try:
            row_id = _canonical_rcm_id(row.get("rcm_id"))
        except ValueError:
            raise WorkerResponseValidationError(
                f"RCM row {index} has an invalid rcm_id"
            )
        if not row_id or row_id not in existing_ids:
            raise WorkerResponseValidationError(
                f"RCM row {index} does not identify an existing RCM row"
            )
    return {
        **{
            key: value
            for key, value in _plain_json(row).items()
            if key in _RCM_ROW_KEYS
        },
        "operation": operation,
        "business_cycle": expected_cycle,
        "control_attributes": attributes,
        # Resolved from refs the row chose out of the supplied register, so the
        # criterion carries a pointer to the sentence it rests on rather than
        # only prose naming a document.
        "criteria_refs": _validated_criteria_refs(
            row, index, list(citation_sheet or [])
        ),
    }


# A memo names a risk theme in one of two shapes: a sub-heading, or a list
# item that leads with the theme in bold. Both are enumerations; which one a
# given memo uses is a formatting choice the reconciliation must not depend on.
# It did depend on it once — a regenerated APM moved its fraud risks from
# sub-headings to bold bullets, and the coverage check went from enforcing six
# themes to enforcing none without saying so.
_BOLD_LED_ITEM = re.compile(r"^\s*[-*]\s+\*\*\s*(.+?)\s*:?\s*\*\*", re.MULTILINE)


def planned_risk_themes(apm_markdown: str) -> list[str]:
    """The themes the APM organises its risk assessment under.

    Scoped by the word "risk" in the parent heading rather than by the shipped
    template's exact wording, so a firm that renames the section keeps the
    check. A section that enumerates nothing contributes nothing — but see
    :func:`unstructured_risk_sections`, because that is worth saying out loud
    rather than passing as coverage.
    """
    themes: list[str] = []
    for heading, body in _section_bodies(apm_markdown).items():
        if "risk" not in heading:
            continue
        # A section enumerates at one level. Where it has sub-headings those are
        # its themes, and the bullets beneath them are detail within a theme —
        # reading both turns a per-theme checklist into nine more themes, each
        # demanding its own row. Bullets are the enumeration only when nothing
        # else is.
        headed = [
            match.group(2).strip()
            for match in _HEADING.finditer(body)
            if len(match.group(1)) == 3
        ]
        themes.extend(
            headed
            or [match.group(1).strip() for match in _BOLD_LED_ITEM.finditer(body)]
        )
    return list(dict.fromkeys(theme for theme in themes if theme))


# What the memorandum could not settle lives under a section of its own. Which
# words head it is a template choice — a firm may call it assumptions,
# limitations, or matters for the auditee — so this is scoped by what the
# heading means rather than by the shipped template's exact wording.
_MATTERS_HEADING = re.compile(r"assumption|matters|limitation|outstanding", re.I)
_LIST_MARKER = re.compile(r"^[-*]\s+(.*)$")


def _list_items(body: str) -> list[str]:
    """Top-level list items of a Markdown block, each carried whole.

    Walked line by line rather than matched by one regex, because a matter is a
    sentence and a sentence wraps: a bullet's continuation lines belong to it,
    and a bullet indented under it qualifies it rather than adding one. A
    per-line regex truncated both — the first at its line break, the second into
    a matter of its own.
    """
    items: list[list[str]] = []
    base: int | None = None
    for raw in body.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        marker = _LIST_MARKER.match(stripped)
        if marker and (base is None or indent <= base):
            base = indent if base is None else min(base, indent)
            items.append([marker.group(1).strip()])
        elif items:
            # A continuation, whether it wraps the sentence or nests under it.
            items[-1].append(marker.group(1).strip() if marker else stripped)
    return [" ".join(part for part in item if part).strip() for item in items]


def planning_matters(apm_markdown: str) -> list[str] | None:
    """The matters the memorandum recorded as unresolved, or None if it has no
    section for them.

    The distinction is the point. ``[]`` is the memorandum answering that
    nothing is outstanding — the template asks for a sentence and no bullets in
    that case — while ``None`` is a memorandum that was never asked, which is
    every APM drafted before the section existed. Reporting both as "no matters"
    would state a clean plan on a memorandum that simply has nothing to say.
    """
    found: list[str] = []
    seen = False
    for heading, body in _section_bodies(apm_markdown).items():
        if not _MATTERS_HEADING.search(heading):
            continue
        seen = True
        headed = [
            match.group(2).strip()
            for match in _HEADING.finditer(body)
            if len(match.group(1)) == 3
        ]
        # Unlike a risk section — where bullets are detail beneath an
        # enumerated theme and only the theme's name is wanted — here the
        # bullets *are* the enumeration, and the sentence after the lead is the
        # reason the matter has to be resolved.
        found.extend(headed or _list_items(body))
    if not seen:
        return None
    return list(dict.fromkeys(item for item in found if item))


def unstructured_risk_sections(apm_markdown: str) -> list[str]:
    """Risk sections carrying substantive prose that enumerate no theme.

    Not an error: a memo may argue its risk assessment in continuous prose, and
    a matrix built from that is not thereby incomplete. It is a degradation of
    what the reconciliation can check, and degradation that says nothing is how
    this check silently stopped covering fraud.
    """
    quiet = []
    for heading, body in _section_bodies(apm_markdown).items():
        if "risk" not in heading or len(body.split()) < 40:
            continue
        if not _HEADING.search(body) and not _BOLD_LED_ITEM.search(body):
            quiet.append(heading)
    return quiet


# Shortest token that may be matched as a prefix of a longer one. Below this,
# prefixes stop being word stems: "pay" would tie "payment" to "payable".
_MIN_STEM = 5


def _comparable(value: object) -> set[str]:
    """Tokens normalized enough to survive ordinary variation in wording.

    A memo writes "circumvention" where a matrix row writes "circumvent", and
    "unauthorised" where another writes "unauthorized". Neither difference is a
    coverage gap, and both defeated exact token equality — the first draft of
    this check reported three owned themes as unowned for no better reason.
    """
    return {
        token.replace("isa", "iza").replace("ise", "ize").replace("yse", "yze")
        for token in relevance_tokens(value)
    }


def _shares(left: set[str], right: set[str]) -> int:
    """How many of ``left``'s tokens some token of ``right`` matches."""
    matched = 0
    for token in left:
        if token in right or any(
            (other.startswith(token) and len(token) >= _MIN_STEM)
            or (token.startswith(other) and len(other) >= _MIN_STEM)
            for other in right
        ):
            matched += 1
    return matched


def _theme_ownership(themes: list[str], rows: list[dict]) -> list[tuple[str, int]]:
    """Each theme with the best overlap any one row achieves against it."""
    owned = [
        _comparable(row.get("process")) | _comparable(row.get("risk"))
        for row in rows
    ]
    scored = []
    for theme in themes:
        tokens = _comparable(theme)
        if not tokens:
            continue
        best = max((_shares(tokens, row_tokens) for row_tokens in owned), default=0)
        scored.append((theme, best))
    return scored


def _unowned_themes(themes: list[str], rows: list[dict]) -> list[str]:
    """Risk themes the APM plans for that no proposed row so much as mentions.

    The matrix is the APM's risk assessment made testable. A theme the memo
    committed to and the matrix never converts into a control is how a planned
    response becomes no procedure at all — which is what happened to goods
    receipt: raised in planning, never a row, so the invoices with no receipt
    evidence had no control to fail.

    Rejection is reserved for a theme with no lexical connection to any row at
    all. See :data:`MIN_THEME_MATCH`: a stricter bar measures phrasing rather
    than coverage, and a matrix may not be discarded over phrasing.
    """
    return [
        theme
        for theme, best in _theme_ownership(themes, rows)
        if best < MIN_THEME_MATCH
    ]


def weakly_owned_themes(apm_markdown: str, rows: list[dict]) -> list[str]:
    """Themes whose ownership rests on a single shared word.

    Reported to the auditor rather than enforced. On a matrix that genuinely
    covered every theme, three of ten sat here — so this cannot decide whether
    a matrix is acceptable, and saying so is the whole of its usefulness.
    """
    return [
        theme
        for theme, best in _theme_ownership(planned_risk_themes(apm_markdown), rows)
        if best == MIN_THEME_MATCH
    ]


# The language in which one record is asserted to agree with another. Ordinary
# audit vocabulary, not a domain's: the same words state that a payroll payment
# agrees to an approved rate and that an invoice agrees to its purchase order.
_AGREEMENT = re.compile(
    r"\b(agree\w*|match\w*|reconcil\w*|equal\w*|exceed\w*|consistent|"
    r"tally|tallies|corroborat\w*|within)\b",
    re.IGNORECASE,
)
_VALUED = re.compile(
    r"\b(amount\w*|total\w*|value\w*|price\w*|quantit\w*|cost\w*|sum|rate\w*)\b",
    re.IGNORECASE,
)
# Populations carrying a summable column. Two or more of them means the
# engagement records what a transaction is worth in more than one place, and
# whether those places agree is an assertion the matrix has to carry.
MIN_VALUED_POPULATIONS = 2


def _valued_populations(request: WorkerRequest) -> list[str]:
    """Imported tables carrying at least one column whose total means something."""
    tables = []
    for profile in _supplied_items(request, "table_profiles"):
        if not isinstance(profile, Mapping):
            continue
        if any(
            isinstance(column, Mapping) and column.get("sum") is not None
            for column in profile.get("columns") or []
        ):
            tables.append(str(profile.get("table") or ""))
    return [table for table in tables if table]


def _asserts_agreement(rows: list[Mapping[str, Any]]) -> bool:
    """Whether any requirement states that recorded values agree with each other."""
    for row in rows:
        for attribute in row.get("control_attributes") or []:
            if not isinstance(attribute, Mapping):
                continue
            requirement = str(attribute.get("requirement") or "")
            if _AGREEMENT.search(requirement) and _VALUED.search(requirement):
                return True
    return False


def document_level_errors(
    request: WorkerRequest,
    rows: list[Mapping[str, Any]],
) -> list[str]:
    """Quality failures of the matrix as a whole rather than of any one row.

    Judged against every row the model wrote, including rows that failed their
    own validation: a row needing a small correction still states its risk, and
    holding its subject against it would report coverage gaps that vanish the
    moment the row is repaired.

    Collected rather than raised one at a time. Each gate that fires in its own
    turn costs a repair attempt, and with one attempt available a matrix that
    tripped a row rule first could never reach these at all — which is exactly
    how a regeneration failed with the row errors fixed and this unreported.
    """
    errors: list[str] = []
    existing = [
        row for row in _current_rcm_rows(request) if isinstance(row, Mapping)
    ]
    everything = [*existing, *rows]
    themes = planned_risk_themes(str(_resolved_item(request, "current_apm") or ""))
    unowned = _unowned_themes(themes, everything)
    if unowned:
        errors.append(
            "the planning memorandum plans a response for "
            f"{counted(len(unowned), 'risk theme')} that no row owns: "
            f"{'; '.join(unowned)}. Add a row whose risk and control concern "
            "each, or state in the risk why the theme needs no control."
        )
    # The three-way match's third leg. Sequence and authorization are what a
    # matrix reaches for unprompted; agreement of the amounts is what it omits,
    # and it is where the largest single exception in this engagement lived —
    # an invoice of 80,000,000 billed against a purchase order of 8,000,000,
    # with no requirement anywhere it could fail.
    valued = _valued_populations(request)
    if len(valued) >= MIN_VALUED_POPULATIONS and not _asserts_agreement(everything):
        errors.append(
            f"{counted(len(valued), 'imported population')} record what a "
            f"transaction is worth ({', '.join(sorted(valued))}), and no control "
            "requirement asserts that those recorded values agree with each "
            "other. Add the requirement to the row that owns the matching or "
            "approval control."
        )
    return errors


def validate_rcm_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the RCM engagement quality gate against current durable row ids."""
    rows = proposal.get("rows")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise WorkerResponseValidationError("no RCM rows were proposed")
    normalized, failures = _partition_rcm_rows(rows, request)
    proposed = [row for row in rows if isinstance(row, Mapping)]
    problems = [
        message for failure in failures for message in failure["errors"]
    ] + document_level_errors(request, proposed)
    if problems:
        raise WorkerResponseValidationError(problems)
    accepted: dict[str, Any] = {"rows": normalized}
    quarantined = proposal.get("quarantined")
    if isinstance(quarantined, (list, tuple)) and quarantined:
        accepted["quarantined"] = [_plain_json(item) for item in quarantined]
    return accepted


def _rcm_activity(request: WorkerRequest, worker_kind: str) -> dict:
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


def _rcm_judgment_user(request: WorkerRequest) -> str:
    """Build the judgment pass's message from the declared bundle alone."""

    template = str(_resolved_item(request, "rcm_template") or "")
    current_apm = str(_resolved_item(request, "current_apm") or "")
    return json.dumps(
        {
            "ACTIVE RCM TEMPLATE (verbatim)": template,
            "REVISED APM": current_apm,
            "CURRENT RCM TO REVISE": _current_rcm_rows(request),
            # The register a criterion cites from. Promoted out of the bundle
            # so the row never has to name a document from its own contents,
            # and never has to copy an id to point at one.
            "CITABLE DOCUMENTS": [
                {key: entry[key] for key in ("ref", "document", "citations")}
                for entry in rcm_citation_sheet(request)
            ],
            "RESOLVED CONTEXT": _context_without_sources(
                request,
                "rcm_template",
                "current_apm",
                RCM_CURRENT_ROWS_SOURCE_ID,
            ),
            "INSTRUCTIONS": (
                "Return the full set of proposed revisions. Work in two passes: "
                "first enumerate the standard risks of every in-scope process from "
                "your own knowledge of the cycle, then tailor wording, rating, and "
                "control to this engagement using the supplied basis. Do not stop at "
                "the risks the supplied material happens to comment on. For an "
                "existing risk, include operation='update' and its exact rcm_id. Use "
                "operation='create' only for a genuinely uncovered risk. Omission "
                "never deletes an existing row."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )


def _parsed_rows(response: str) -> list[dict]:
    """Parse a `rows` document, raising the schema error the registry expects."""

    parsed = _rcm_response_schema(response)
    return [_plain_json(row) for row in parsed["rows"]]


def _cycle_attribute_requests(rows: list[dict]) -> list[dict]:
    """Collect the attributes that still need an evidence contract."""

    pending: list[dict] = []
    for index, row in enumerate(rows, start=1):
        attributes = row.get("control_attributes")
        if not isinstance(attributes, list):
            continue
        for attribute in attributes:
            if not isinstance(attribute, Mapping):
                continue
            if attribute.get("evidence_kind") != "transaction_cycle":
                continue
            if attribute.get("comparison_recipes") or attribute.get(
                "required_comparisons"
            ):
                continue
            pending.append(
                {
                    "row_index": index,
                    "attribute_key": str(attribute.get("key") or ""),
                    "process": str(row.get("process") or ""),
                    "risk": str(row.get("risk") or ""),
                    "control": str(row.get("control") or ""),
                    "assertion": str(attribute.get("assertion") or ""),
                    "requirement": str(attribute.get("requirement") or ""),
                }
            )
    return pending


def _merge_evidence_contracts(rows: list[dict], response: str) -> list[dict]:
    """Write returned contracts onto the attributes that asked for them.

    A contract that names no attribute, names one twice, or reports the pack
    cannot express the requirement is simply not written. The attribute then
    fails the gate as an evidence strategy with no contract, which is the honest
    outcome and one the bounded repair turn can act on — quietly inventing a
    comparison here would answer a different question than the requirement asked.
    """

    contracts = _first_json_object(response).get("contracts")
    if not isinstance(contracts, list):
        raise WorkerResponseValidationError(
            "the evidence-contract response must be a JSON object with a "
            "`contracts` array"
        )
    by_target: dict[tuple[int, str], Mapping[str, Any]] = {}
    for entry in contracts:
        if not isinstance(entry, Mapping) or entry.get("unsupported"):
            continue
        try:
            row_index = int(entry.get("row_index"))
        except (TypeError, ValueError):
            continue
        by_target[(row_index, str(entry.get("attribute_key") or ""))] = entry
    merged: list[dict] = []
    for index, row in enumerate(rows, start=1):
        attributes = row.get("control_attributes")
        if not isinstance(attributes, list):
            merged.append(row)
            continue
        updated = []
        for attribute in attributes:
            entry = (
                by_target.get((index, str(attribute.get("key") or "")))
                if isinstance(attribute, Mapping)
                else None
            )
            if entry is None:
                updated.append(attribute)
                continue
            if entry.get("required_comparisons") is None:
                # No contract, so the attribute reaches the gate as a cycle
                # strategy with nothing behind it — the honest failure, and one
                # the bounded repair turn can act on.
                updated.append(attribute)
                continue
            # The fields are named outright, so there is nothing to derive.
            # Validation against the current schemas happens at the commit,
            # which is the turn that holds the engagement.
            contract = {
                "required_comparisons": _plain_json(entry["required_comparisons"])
            }
            updated.append({**attribute, **contract})
        merged.append({**row, "control_attributes": updated})
    return merged


def _stripped_fence(response: str) -> str:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        value,
        re.DOTALL | re.IGNORECASE,
    )
    return fenced.group(1).strip() if fenced else value


def _first_json_object(response: str) -> dict:
    """Return the first complete JSON object in the response.

    ``json.loads`` requires the *entire* string to be one value, so a complete
    object followed by a couple of stray closing brackets is discarded whole. A
    live 24-row draft was lost exactly that way: valid JSON, then a trailing
    ``]}`` from the model closing brackets it had already closed.

    Only *surplus* is tolerated, never shortfall. A truncated object still fails
    to decode and is still rejected, so this cannot turn a partial draft into a
    document that looks complete.
    """

    value = _stripped_fence(response)
    start = value.find("{")
    if start < 0:
        raise WorkerResponseValidationError(
            "the response is not a valid JSON object"
        )
    try:
        payload, _ = json.JSONDecoder().raw_decode(value[start:])
    except json.JSONDecodeError:
        raise WorkerResponseValidationError(
            "the response is not a valid JSON object"
        )
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    return payload


def _repair_scoped_rows(
    rows: list[dict],
    failures: list[dict],
    response: str,
) -> list[dict]:
    """Splice corrected rows back over exactly the rows that failed.

    Rows that validated are carried through as the identical objects that were
    parsed, never re-serialized from a second model turn, so a repair cannot
    quietly reword a row it was not asked about.

    A corrected row may arrive flat, carrying ``row_index`` beside its own
    fields, or nested under ``row`` the way the request presented it. Both are
    accepted: the model echoed the request envelope on a real engagement, every
    corrected row spliced in as ``{"row": {…}}`` with no process and no risk of
    its own, and nineteen good rows were replaced by empty ones without a word.
    """

    corrected = _first_json_object(response)
    if not isinstance(corrected.get("rows"), list):
        raise WorkerResponseValidationError(
            "the repair response must be a JSON object with a `rows` array"
        )
    by_index: dict[int, dict] = {}
    scoped = {failure["index"] for failure in failures}
    for entry in corrected["rows"]:
        if not isinstance(entry, Mapping):
            continue
        try:
            index = int(entry.get("row_index"))
        except (TypeError, ValueError):
            continue
        if isinstance(entry.get("row"), Mapping):
            entry = {"row_index": index, **entry["row"]}
        if index in scoped:
            by_index[index] = {
                key: _plain_json(value)
                for key, value in entry.items()
                if key != "row_index"
            }
    return [by_index.get(index, row) for index, row in enumerate(rows, start=1)]


def _quarantined(failures: list[dict]) -> list[dict]:
    """Project unrepairable rows into the durable record of what was dropped."""

    quarantined: list[dict] = []
    for failure in failures:
        row = failure["row"] if isinstance(failure["row"], Mapping) else {}
        quarantined.append(
            {
                "process": str(row.get("process") or ""),
                "risk": str(row.get("risk") or ""),
                "errors": list(failure["errors"]),
            }
        )
    return quarantined


def run_rcm_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Author one RCM revision from the supplied bundle.

    Two passes, because the work is at two altitudes. The judgment pass decides
    the risks, the controls, and each attribute's evidence strategy; it never
    sees the cycle pack catalog. The evidence pass authors the comparison
    contract for the attributes that asked for one, and it is the only call that
    carries the DSL and the catalog. A repair is scoped to the rows that actually
    failed and merged locally over the rows that did not.
    """

    user = _rcm_judgment_user(request)
    if attempt.is_repair:
        if attempt.previous_response is None:
            raise WorkerContractError("An RCM repair requires the previous response.")
        return _repaired_rcm(request, gateway, attempt, user)
    response = gateway.complete(
        RCM_SYSTEM,
        user,
        _rcm_activity(request, "rcm"),
        attempt=attempt.number,
    )
    return _contracted_document(request, gateway, attempt, response)


def _contracted_document(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    response: str,
) -> str:
    """Turn one judgment-pass response into the finished document.

    Every path that produces a whole document goes through here — the initial
    attempt and the whole-document re-ask alike. Returning a judgment response
    directly, as the re-ask used to, skips the evidence pass and leaves every
    transaction-cycle attribute without the contract it was told not to write.
    """

    try:
        rows = _parsed_rows(response)
    except WorkerResponseValidationError:
        # A worker returns response text; the registry owns rejection and the
        # bounded repair that follows it. Raising from here would escape that
        # loop entirely, so an unusable draft is handed back verbatim to be
        # rejected — and repaired — through the normal path.
        return response
    return _rcm_document(
        request,
        attempt,
        _with_evidence_contracts(request, gateway, attempt, rows),
    )


def _rcm_document(
    request: WorkerRequest,
    attempt: WorkerAttempt,
    rows: list[dict],
) -> str:
    """Serialize the finished rows, quarantining what will not repair.

    Only on the last attempt. Rows that still fail are set aside with their
    reasons rather than failing the run: the alternative discards every correct
    row in the document, which is how a single unsupported operator used to cost
    thirteen good rows. An empty survivor set is still a failure — there is
    nothing to commit — and the gate reports it.
    """

    if attempt.number < 1 + _RCM_MAX_REPAIR_ATTEMPTS:
        return json.dumps({"rows": rows}, ensure_ascii=False)
    accepted, still_failing = _partition_rcm_rows(rows, request)
    if not accepted or not still_failing:
        return json.dumps({"rows": rows}, ensure_ascii=False)
    dropped = {failure["index"] for failure in still_failing}
    return json.dumps(
        {
            "rows": [
                row
                for index, row in enumerate(rows, start=1)
                if index not in dropped
            ],
            "quarantined": _quarantined(still_failing),
        },
        ensure_ascii=False,
    )


def _downgraded_uncontracted(
    request: WorkerRequest,
    rows: list[dict],
) -> list[dict]:
    """Re-route attributes the evidence pass could not contract for.

    An attribute this engagement's documents cannot express is a limit of the
    *documentary* path, not of the requirement. "The invoice amount agrees to
    the purchase order total" is answerable from the imported populations
    whether or not the extracted schemas carry those fields, and leaving the
    attribute classified ``transaction_cycle`` with no contract does not
    preserve rigour — it fails the row, discards its risk and its control along
    with it, and tests nothing at all.

    So the attribute keeps its requirement and takes the strongest path still
    open to it: the population where the supplied tables bear on the row, the
    documents otherwise. This became load-bearing the moment the matrix started
    classifying attributes as ``transaction_cycle`` at all — before that, this
    path never ran, and eight rows carrying real risks died on it in one run.
    """
    answers = _tabular_answers(request)
    downgraded: list[dict] = []
    for row in rows:
        attributes = row.get("control_attributes")
        if not isinstance(attributes, list):
            downgraded.append(row)
            continue
        fallback = (
            "tabular_population"
            if _answering_table(row, answers)
            else "document_content"
        )
        updated = []
        for attribute in attributes:
            if (
                isinstance(attribute, Mapping)
                and attribute.get("evidence_kind") == "transaction_cycle"
                and not attribute.get("required_comparisons")
            ):
                # Comparisons go with the strategy that owned them: they name
                # the fields a cycle links, and mean nothing once the attribute
                # is answered another way. Leaving one behind trades one
                # rejection for another on a row this path exists to save.
                attribute = {
                    key: value
                    for key, value in attribute.items()
                    if key != "required_comparisons"
                }
                attribute["evidence_kind"] = fallback
            updated.append(attribute)
        downgraded.append({**row, "control_attributes": updated})
    return downgraded


def _with_evidence_contracts(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    rows: list[dict],
) -> list[dict]:
    """Run the evidence pass, but only when an attribute actually needs it."""

    pending = _cycle_attribute_requests(rows)
    if not pending:
        return rows
    response = gateway.complete(
        RCM_SCHEMA_EVIDENCE_SYSTEM,
        json.dumps(
            {
                "ATTRIBUTES NEEDING AN EVIDENCE CONTRACT": pending,
                # Unwrapped: ``WorkerRequest`` hands back a recursively
                # immutable input, so the catalog's entries arrive as
                # ``MappingProxyType`` and ``json.dumps`` refuses them. It went
                # unnoticed because the catalog was being sent with a different
                # unit, and ``or []`` made an absent one a plain empty list —
                # so the only shape ever serialized here was the empty one.
                "DOCUMENT TYPES AND THE FIELDS THEY STATE": _plain_json(
                    request.unit_input.get("schema_catalog") or []
                ),
                "INSTRUCTIONS": (
                    "Return one contracts entry per supplied attribute, with "
                    "row_index and attribute_key copied exactly."
                ),
            },
            indent=1,
            ensure_ascii=False,
        ),
        _rcm_activity(request, "rcm_evidence"),
        attempt=attempt.number,
    )
    try:
        return _downgraded_uncontracted(request, _merge_evidence_contracts(rows, response))
    except WorkerResponseValidationError:
        # Nothing to merge. The attributes stay without a contract and fail the
        # gate as such, which is a bounded, repairable outcome — and a truthful
        # one — where raising from inside the worker would not be.
        return rows


def _repaired_rcm(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    user: str,
) -> str:
    """Correct only the rows that failed, and quarantine what will not repair."""

    try:
        rows = _parsed_rows(str(attempt.previous_response))
    except WorkerResponseValidationError:
        # The prior draft was rejected by the schema rather than the quality
        # gate — a linked retry from a parent run can start here — so there are
        # no rows to scope to and the whole document is re-asked. It is a
        # judgment-pass response like any other, so it goes through the evidence
        # pass on the way out.
        return _contracted_document(
            request,
            gateway,
            attempt,
            gateway.complete(
                RCM_SYSTEM,
                user
                + "\n\nThe previous response could not be parsed: "
                + "; ".join(attempt.validation_errors)
                + ". Return the complete JSON object.",
                _rcm_activity(request, "rcm_repair"),
                attempt=attempt.number,
            ),
        )
    _, failures = _partition_rcm_rows(rows, request)
    document_errors = document_level_errors(
        request, [row for row in rows if isinstance(row, Mapping)]
    )
    if document_errors or not failures:
        # The matrix is wrong as a whole — a risk theme no row owns, no
        # requirement that recorded values agree — and a scoped repair cannot
        # express that: the correction is to *add* something, and the scoped
        # prompt forbids returning rows it did not list. Whole-document even
        # when individual rows also failed, because one repair attempt has to
        # answer everything that is wrong or the next gate is never reached.
        return _contracted_document(
            request,
            gateway,
            attempt,
            gateway.complete(
                RCM_SYSTEM,
                user
                + "\n\nThe previous matrix failed the engagement quality gate: "
                + "; ".join(attempt.validation_errors)
                + ". Return the complete matrix again, correcting every listed "
                "error and preserving every other row unchanged.",
                _rcm_activity(request, "rcm_repair"),
                attempt=attempt.number,
            ),
        )
    response = gateway.complete(
        RCM_SYSTEM + "\n\n" + RCM_SCHEMA_EVIDENCE_SYSTEM,
        json.dumps(
            {
                "ROWS TO CORRECT": [
                    {
                        "row_index": failure["index"],
                        "row": failure["row"],
                        "errors": failure["errors"],
                    }
                    for failure in failures
                ],
                "INSTRUCTIONS": (
                    "Each supplied row failed the engagement quality gate for "
                    "the listed reasons. Return an object with `rows` containing "
                    "one corrected row per supplied row, each carrying its exact "
                    "row_index. Correct every listed error and change nothing "
                    "else. Rows not supplied here are already accepted and must "
                    "not be returned: they are preserved unchanged."
                ),
            },
            indent=1,
            ensure_ascii=False,
        ),
        _rcm_activity(request, "rcm_repair"),
        attempt=attempt.number,
    )
    try:
        repaired = _repair_scoped_rows(rows, failures, response)
    except WorkerResponseValidationError:
        return response
    return _rcm_document(
        request,
        attempt,
        _with_evidence_contracts(request, gateway, attempt, repaired),
    )


RCM_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.rcm.response",
    schema_hash=_sha256_text(
        "rcm-response:v4:first-json-object-with-rows-array-control-attributes-"
        "recipe-expanded-contracts-and-quarantine"
    ),
    validator=_rcm_response_schema,
)
RCM_WORKER = WorkerDefinition(
    worker_id=RCM_WORKER_ID,
    # The implementation is now the two-pass sequence plus the local merges, so
    # every part of it that decides what reaches the model is in the identity a
    # persisted proposal is reused against.
    prompt_hash=_sha256_text(RCM_SYSTEM + RCM_SCHEMA_EVIDENCE_SYSTEM),
    response_schema=RCM_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=_RCM_MAX_REPAIR_ATTEMPTS,
        guidance_hash=_sha256_text(
            "Repair only the RCM rows that failed, against all of their bounded "
            "errors and current ids; quarantine what will not repair."
        ),
        # Row-scoped repair means the guidance is grouped per row rather than
        # flattened across the document, so a document with several bad rows needs
        # more room than the default before errors start being dropped — and an
        # error the model never sees is one it cannot fix.
        max_validation_errors=20,
        max_guidance_characters=4_000,
    ),
    implementation=run_rcm_worker,
    semantic_validator=validate_rcm_proposal,
)

WORKERS.register(RCM_WORKER)


# --------------------------------------------------------------------------- #
# planning.context worker (P7A.2)
# --------------------------------------------------------------------------- #
PLANNING_CONTEXT_WORKER_ID = "planning.context"
PLANNING_CONTEXT_SYSTEM = f"""[agent:document_context]
Extract planning facts only from the included engagement documents.
Return an object with `context`, containing only supported fields that are
grounded in the documents: objective, entity, period, scope, materiality,
key_contacts, and background_notes. Every supplied context value must be a
string; format multiple key contacts as one newline-separated string. Omit fields that the documents do not
support; do not turn policy requirements into claims about actual control
operation. {JSON_RULES}"""

PLANNING_CONTEXT_CURRENT_SOURCE_ID = "current_planning_context"
PLANNING_CONTEXT_DOCUMENT_SOURCE_ID = "planning_documents"
PLANNING_CONTEXT_FIELDS = (
    "objective",
    "entity",
    "period",
    "scope",
    "materiality",
    "key_contacts",
    "background_notes",
)
# The labelled facts the document-analysis contract emits, mapped to the
# planning fields they populate. Recovery is deliberately narrow: only these
# exact labels are accepted, never inferred from prose.
_PLANNING_CONTEXT_LABELS = {
    "objective": "objective",
    "entity": "entity",
    "period": "period",
    "scope": "scope",
    "materiality": "materiality",
    "key contacts": "key_contacts",
    "background notes": "background_notes",
}
_LABELLED_FACT = re.compile(r"^\s*[-*]?\s*\*\*([^*]+?):\*\*\s*(.+?)\s*$")


def _planning_context_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    context = payload.get("context")
    if context is None:
        # Some providers flatten the requested object wrapper while still
        # returning correctly grounded fields. Normalize that harmless shape
        # drift instead of discarding useful planning context.
        context = {
            key: value
            for key, value in payload.items()
            if key in PLANNING_CONTEXT_FIELDS and isinstance(value, str)
        }
    if not isinstance(context, dict):
        raise WorkerResponseValidationError("`context` must be an object")
    for key, value in context.items():
        if key in PLANNING_CONTEXT_FIELDS and not isinstance(value, str):
            raise WorkerResponseValidationError(f"context.{key} must be a string")
    return {"context": context}


def _recovered_labelled_facts(request: WorkerRequest) -> dict[str, str]:
    """Recover labelled planning facts from the supplied document material.

    Deliberately narrow: only the labelled fields the document-analysis contract
    emits are accepted, and prose is never interpreted. This protects the
    planning chain when synthesis returns valid JSON with an empty context.
    """
    recovered: dict[str, str] = {}
    for content in _supplied_items(request, PLANNING_CONTEXT_DOCUMENT_SOURCE_ID):
        for line in str(content or "").splitlines():
            match = _LABELLED_FACT.match(line)
            if not match:
                continue
            label = re.sub(r"\s+", " ", match.group(1).strip().casefold())
            key = _PLANNING_CONTEXT_LABELS.get(label)
            value = match.group(2).strip()
            if key and value and key not in recovered:
                recovered[key] = value
    return recovered


def validate_planning_context_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Keep only grounded, non-empty declared fields; recover labelled facts.

    A syntactically valid response with no usable field is not repaired by asking
    again: the labelled facts already present in the supplied summaries are the
    better answer and cost nothing. Only when neither the model nor those labels
    yield a field is this a contract violation the model can be told to fix.
    """
    raw = proposal.get("context")
    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError("context must be an object")
    context = {
        key: str(value).strip()
        for key, value in raw.items()
        if key in PLANNING_CONTEXT_FIELDS and str(value or "").strip()
    }
    if context:
        return {"context": context}
    recovered = _recovered_labelled_facts(request)
    if recovered:
        return {"context": recovered, "recovered_from_labelled_facts": True}
    raise WorkerResponseValidationError(
        "context must contain at least one field grounded in the supplied "
        f"documents; supported fields are {', '.join(PLANNING_CONTEXT_FIELDS)}"
    )


def run_planning_context_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    current = _supplied_items(request, PLANNING_CONTEXT_CURRENT_SOURCE_ID)
    documents = _supplied_items(request, PLANNING_CONTEXT_DOCUMENT_SOURCE_ID)
    if not documents:
        raise WorkerContractError(
            "Planning-context synthesis requires at least one supplied document."
        )
    user = (
        "CURRENT PLANNING CONTEXT:\n"
        f"{json.dumps(current[0] if current else {}, default=str)}\n\n"
        "INCLUDED DOCUMENT CONTENT:\n"
        f"{json.dumps(list(documents), default=str)}"
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return a corrected object with a non-empty `context` grounded "
            "only in the supplied documents."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "planning_context",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        PLANNING_CONTEXT_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
    )


PLANNING_CONTEXT_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.context.response",
    schema_hash=_sha256_text("planning-context-response:json-object-with-context"),
    validator=_planning_context_response_schema,
)
PLANNING_CONTEXT_WORKER = WorkerDefinition(
    worker_id=PLANNING_CONTEXT_WORKER_ID,
    prompt_hash=_sha256_text(PLANNING_CONTEXT_SYSTEM),
    response_schema=PLANNING_CONTEXT_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair planning-context synthesis against the supplied documents."
        ),
    ),
    implementation=run_planning_context_worker,
    semantic_validator=validate_planning_context_proposal,
)

WORKERS.register(PLANNING_CONTEXT_WORKER)


__all__ = [
    "APM_RESPONSE_SCHEMA",
    "APM_SYSTEM",
    "APM_WORKER",
    "APM_WORKER_ID",
    "PLANNING_CONTEXT_FIELDS",
    "PLANNING_CONTEXT_RESPONSE_SCHEMA",
    "PLANNING_CONTEXT_SYSTEM",
    "PLANNING_CONTEXT_WORKER",
    "PLANNING_CONTEXT_WORKER_ID",
    "RCM_SCHEMA_EVIDENCE_SYSTEM",
    "RCM_RESPONSE_SCHEMA",
    "RCM_SYSTEM",
    "RCM_WORKER",
    "RCM_WORKER_ID",
    "run_apm_worker",
    "run_planning_context_worker",
    "run_rcm_worker",
    "validate_apm_proposal",
    "validate_planning_context_proposal",
    "validate_rcm_proposal",
]
