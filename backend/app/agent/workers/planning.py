"""Registered model workers for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import cycle_rulesets, cycle_vouching, planning_cycle
from ...text import counted, relevance_tokens
from .. import prompts
from ..prompts import JSON_RULES, LANGUAGE_RULES
from ..runtime.model_gateway import ModelGateway
from .model import (
    AUDITOR_INSTRUCTION_RULE,
    AUDITOR_INSTRUCTION_SOURCE_ID,
    auditor_instruction,
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
fence. """ + LANGUAGE_RULES + f"\n\n{AUDITOR_INSTRUCTION_RULE}"

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


def _context_from_sources(
    request: WorkerRequest,
    *source_ids: str,
) -> dict[str, Any]:
    """Serialize only the named sources of the resolved bundle.

    The bundle is resolved once, for the unit, and each call is handed the part
    of it that bears on its own job — so the risks call is not shown the
    documents it would otherwise write its risk set out of, and the controls
    call is not shown a memorandum it might re-rate a risk from. What is
    withheld is withheld from the message, not from the manifest: the unit's
    context identity is unchanged and every item is still recorded as supplied
    to the unit.
    """
    context = request.context.to_dict()
    wanted = set(source_ids)
    context["items"] = [
        item for item in context["items"] if item.get("source_id") in wanted
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
    template_bodies = _section_bodies(template)
    for heading in template_bodies:
        if not re.search(r"[A-Za-z0-9]", bodies.get(heading, "")):
            raise WorkerResponseValidationError(
                f"template section '{heading}' is present but has no content"
            )
    # Read only the section the template declares the field in, never the whole
    # memo. The proximity window bounds distance but says nothing about *place*,
    # and a memo is tens of thousands of characters: any prose pairing the word
    # with "not available" trips it from anywhere. That is not hypothetical, and
    # it is the second time — ``period`` was dropped from this gate after
    # "in the audit period; if not available" read as the memo disowning its own
    # period. The same shape then rejected a complete treasury memo for saying
    # its policy extract held "sections 1-3 (scope, definitions, governance) and
    # 9-11 ... are not available" — an accurate remark about a *source document's*
    # sections, seventy-nine characters from the word and nowhere near where the
    # engagement states its scope.
    #
    # Following the template rather than naming a heading keeps the guard honest
    # if the field moves: it checks wherever the template asks for the field, and
    # checks nothing else.
    for field_name in ("objective", "scope"):
        if not structured.get(field_name):
            continue
        # Leaf sections only. ``_section_bodies`` is depth-aware, so a document's
        # ``#`` title carries every ``##`` beneath it as its body — matching that
        # would scope the scan back to the whole memo and change nothing.
        declared_in = [
            heading
            for heading, body in template_bodies.items()
            if not _HEADING.search(body)
            and re.search(rf"\b{field_name}\b", f"{heading}\n{body}", re.IGNORECASE)
        ]
        # A template that never names the field leaves nowhere to narrow to, so
        # the whole memo stands in. That is the old behaviour, kept only for the
        # case that cannot be scoped rather than as the default.
        searched = (
            [bodies.get(heading, "") for heading in declared_in]
            if declared_in
            else [markdown]
        )
        for text in searched:
            section = re.sub(r"\s+", " ", text.casefold())
            if re.search(rf"\b{field_name}\b.{{0,80}}{_UNAVAILABLE}", section):
                raise WorkerResponseValidationError(
                    f"the memorandum says {field_name} is unavailable "
                    "despite structured context"
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
    instruction = auditor_instruction(request)
    user = json.dumps(
        {
            "ACTIVE APM TEMPLATE (verbatim)": template,
            "CURRENT APM TO REVISE": current_apm,
            # Omitted rather than sent empty: a key present and blank invites a
            # model to invent what should have been in it.
            **({"auditor_instruction": instruction} if instruction else {}),
            "RESOLVED CONTEXT": _context_without_sources(
                request,
                "apm_template",
                "current_apm",
                AUDITOR_INSTRUCTION_SOURCE_ID,
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
RCM_RISKS_SYSTEM = f"""[agent:rcm]
Revise the risk set of the current risk and control matrix, using durable RCM
ids. Return an object with `rows`, each row containing operation
(update|create), rcm_id for updates, process, risk,
risk_rating (low|medium|high|critical), and business_cycle. Nothing else: the
control, its type, its owner, its criteria and the control attributes are each
decided by a later pass, and a field you write here that they own is discarded.

All ids and narrative fields are strings. business_cycle names the cycle this
row belongs to; BUSINESS CYCLE gives it, and every row carries that value.

You are shown the memorandum and the methodology, and no engagement documents,
tables or profiles. That is deliberate. A risk is what could go wrong in this
cycle whether or not this engagement's data happens to show it, and a risk set
assembled from the supplied evidence is a description of the evidence.

Follow the ACTIVE RISK TEMPLATE for methodology. Its non-negotiable rules:
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
- process groups the rows; it does not label them. Choose each row's process
  from PROCESS NAMES, spelled exactly as given: those are the steps of this
  cycle, already read from the memorandum, and the matrix is grouped by them.
  A cycle has a handful of steps and each carries several risks, so a matrix
  whose every row names a different process has not grouped anything. Using
  fewer process names never means covering fewer of them: every step keeps its
  risks, under the name the cycle gives it.
- Rate against the band the template describes, not against the row beside it.
  A set of risks with no medium in it has not been rated, and the rating is the
  first thing a reviewer uses to direct effort.
- One risk per row, and two rows describing the same underlying failure are one
  row. Say it once, in the wording that covers both.
{JSON_RULES} {LANGUAGE_RULES}""" + f"\n\n{AUDITOR_INSTRUCTION_RULE}"


#: The control pass: what management asserts it does about each settled risk.
#:
#: The one call that reads the engagement's own material. Risk enumeration is
#: recall and needs none of it; classification is a lookup and is given names
#: only; describing a control is grounded reading, and this is the turn that
#: does it.
RCM_CONTROLS_SYSTEM = f"""[agent:rcm_controls]
Describe the control management asserts is in place against risks that are
already written.

Return an object with `controls`, one entry per supplied row, each with
row_index copied exactly from the request, plus control, control_type, and —
where the supplied basis states them — control_owner, criteria and
criteria_hint.

The risks are settled. Do not revise, restate or re-rate one, and do not return
any field of a row other than its row_index and the control fields above.

Follow the ACTIVE CONTROL TEMPLATE. Its non-negotiable rules:
- The control field records a control management asserts is in place. Where none
  exists, write "No control identified" rather than describing the control that
  ought to exist. Read the basis for one before concluding it is absent: a
  control the basis plainly describes and this matrix reports as missing
  understates the entity's control environment and sends fieldwork the wrong
  way.
- Never assert that a system enforces, prevents, blocks, or validates something
  unless the planning basis states it: a field existing in a table shows a value
  is recorded, never that it is controlled. Where the basis names a control but
  not its mechanism, describe what it is asserted to do and say the mechanism is
  not confirmed in the planning basis.
- The risk wording rules apply to the control field too. No percentages, null
  rates, counts, or column names, and no appended deficiency clause.
- control_type is exactly preventive or detective — preventive where the control
  stops the error before it occurs, detective where it identifies the error
  afterwards. A row whose control is "No control identified" leaves it empty:
  there is no control to classify, and naming a kind for one that does not exist
  asserts mechanics the basis never described.
- control_owner names a role only where that role appears verbatim in the
  supplied basis. Copy it as the basis spells it. Where the basis names no owner
  for this control, leave the field empty: an empty owner is a question to put
  to the client, an invented one is a false attribution that survives into the
  working paper.
- criteria is the clause the control is measured against, quoted verbatim from
  the supplied basis, at most about 300 characters. Copy the sentence; do not
  paraphrase it and do not name the document it came from — the quote is matched
  back to its source locally. Where a `[C...]` marker sits beside the sentence
  you quoted, you may give it as criteria_hint; it is a hint and nothing more.
  Where no supplied clause states a criterion, leave criteria empty.
- Supplied table profiles are value-free shape statistics, not evidence. A null
  percentage is not an exception rate; a maximum is not a policy limit.
{JSON_RULES} {LANGUAGE_RULES}""" + f"\n\n{AUDITOR_INSTRUCTION_RULE}"


#: The attribute pass: the closed-vocabulary half of a matrix row.
#:
#: Split from the rows prompt because it is a different job at a different
#: altitude. Enumerating a process's risks is domain recall; choosing an
#: assertion from a list of eight and an evidence strategy from a list of six is
#: classification against a supplied catalogue. Fused, a defect in either cost
#: the whole matrix — and the whole matrix is the largest completion in the
#: system, so it cost the most to re-ask.
#:
#: It is shown the rows and no engagement prose. The strategy is a question
#: about *where an answer lives*, which the names of the tables and record kinds
#: this engagement holds already settle; the documents that would let it revise
#: a risk are deliberately withheld, because revising one is not its job.
RCM_ATTRIBUTES_SYSTEM = f"""[agent:rcm_attributes]
State the control attributes of matrix rows that are already written.

Return an object with `attributes`, one entry per supplied row, each with
row_index copied exactly from the request and control_attributes.

The rows are settled. Do not revise, restate or reorder them, and do not return
any field of a row other than its row_index: the risk, the control and the
rating are judgments already made, and a row you were not asked about is not
yours to change.

Follow the ACTIVE ATTRIBUTE TEMPLATE. Its non-negotiable rules:
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
  comparison_recipes: what must agree is decided later, against this
  engagement's own documents.

{JSON_RULES} {LANGUAGE_RULES}""" + f"\n\n{AUDITOR_INSTRUCTION_RULE}"


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
)
_RCM_RISK_RATINGS = {"low", "medium", "high", "critical"}
#: What a control does about the error: stops it, or finds it afterwards.
#: There is no third kind, and the field was free text until a procurement run
#: put the literal string "None" on seven rows — accepted, because the only
#: check was that it was not empty, and useless to everything that reads it.
_RCM_CONTROL_TYPES = {"preventive", "detective"}
#: The one control statement that classifies nothing. A row saying management
#: asserts no control has no control to be preventive or detective *about*, and
#: the honest field is empty. Requiring a value there is how the earlier runs
#: got a classification of a control that does not exist.
_NO_CONTROL = "no control identified"


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


#: Sentence ends, and line breaks. A document summary is markdown — a heading,
#: then a bullet per clause — and most of its lines carry no full stop at all,
#: so splitting on terminators alone swallowed the file name and the heading
#: into the first clause and no quote of that clause matched it.
_SENTENCE_END = re.compile(r"\n+|(?<=[.!?])[ \t]+")
#: A marker at the very start of a piece belongs to the piece before it: an
#: anchor follows its sentence's full stop ("… the requisition. [C4]"), and the
#: split above cuts exactly there. Left uncorrected, every sentence lands in one
#: piece and every marker in the next, and no sentence carries the anchor it was
#: written for.
_LEADING_MARKER = re.compile(r"^\s*(\[[Cc]\d+\])")
#: A quote must overlap a sentence this much to be accepted as that sentence
#: when nothing matched outright. Below it the two are about different things,
#: and attaching the wrong citation is worse than attaching none.
_CRITERIA_FUZZY_FLOOR = 0.6
_CRITERIA_QUOTE_LIMIT = 400


def _normalized_quote(value: object) -> str:
    """Casefold, collapse whitespace, strip surrounding punctuation and quotes.

    Citation markers are removed: an anchor is not part of the clause, and a
    quote copied without it would otherwise score as a different sentence.
    """

    text = _CITATION_MARKER.sub(" ", str(value or ""))
    text = " ".join(text.split()).casefold()
    return text.strip(" \t\"'“”‘’`.,;:—-*#")


def _quote_tokens(normalized: str) -> set[str]:
    """The words of a quote, each stripped of the punctuation attached to it.

    ``requisition.`` and ``requisition`` are the same word, and a quote that
    stops one clause early otherwise loses the overlap on its own last word.
    """

    words = (word.strip("\"'“”‘’`.,;:!?()[]—-*") for word in normalized.split())
    return {word for word in words if word}


def rcm_sentence_index(request: WorkerRequest) -> list[dict[str, Any]]:
    """Every citable sentence of every supplied document, with its marker.

    The register the criteria resolver matches a quote against. A worker quotes
    the clause; local code finds which document it came from and which anchor
    sits beside it. That is the whole of the change: an identifier the model
    used to have to copy correctly is now one that local code looks up, and a
    quote it gets slightly wrong costs a citation rather than failing a row.
    """

    index: list[dict[str, Any]] = []
    for entry in _citable_documents(request):
        for sentence in _sentences_with_their_markers(entry["text"]):
            markers = _CITATION_MARKER.findall(sentence)
            if not markers:
                continue
            normalized = _normalized_quote(sentence)
            if not normalized:
                continue
            index.append({
                "ref": entry["ref"],
                "document_id": entry["document_id"],
                "document": entry["document"],
                "citation": markers[-1],
                "sentence": normalized,
                "tokens": _quote_tokens(normalized),
            })
    return index


def _sentences_with_their_markers(text: str) -> list[str]:
    """Split into sentences, each keeping the citation anchor written for it."""

    pieces = _SENTENCE_END.split(text)
    sentences: list[str] = []
    for piece in pieces:
        leading = _LEADING_MARKER.match(piece)
        if leading and sentences:
            sentences[-1] += " " + leading.group(1)
            piece = piece[leading.end():]
        sentences.append(piece)
    return sentences


def _citable_documents(request: WorkerRequest) -> list[dict[str, Any]]:
    """Supplied engagement documents, numbered in bundle order."""

    found: list[dict[str, Any]] = []
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
        document_id = item.source_ref.split(":", 1)[1]
        found.append({
            "ref": number,
            "document_id": document_id,
            "document": prompts.summary_document_name(text) or document_id,
            "text": text,
        })
    return found


def resolve_criteria(
    quote: object, hint: object, index: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str]:
    """Find the sentence a quoted criterion came from. Never an error.

    Five outcomes, in order of how much they are trusted:

    1. the quote is a supplied sentence, or sits inside one — that ref;
    2. several sentences match and the hint names one of them — that one;
       several and no usable hint — the first in bundle order, flagged
       ``criteria_ambiguous``;
    3. nothing matched outright but one sentence overlaps it enough — that one;
    4. nothing matched and the hint is a marker the documents carry — that
       sentence's ref, flagged ``criteria_unverified``;
    5. nothing at all — no refs, flagged ``criteria_unresolved``. The quote is
       kept either way: a criterion an auditor can read is worth having without
       a pointer, and a pointer is never invented to make one look sourced.
    """

    wanted = _normalized_quote(quote)
    if not wanted or not index:
        return [], ("" if not wanted else "criteria_unresolved")
    folded_hint = str(hint or "").strip().casefold()

    exact = [
        entry for entry in index
        if wanted in entry["sentence"] or entry["sentence"] in wanted
    ]
    if exact:
        return _resolved_refs(_preferred(exact, folded_hint)), (
            "" if len(exact) == 1 or _hinted(exact, folded_hint) else "criteria_ambiguous"
        )

    tokens = _quote_tokens(wanted)
    scored = [
        (len(tokens & entry["tokens"]) / max(1, len(tokens | entry["tokens"])), entry)
        for entry in index
    ]
    best = max(scored, key=lambda pair: pair[0], default=(0.0, None))
    if best[1] is not None and best[0] >= _CRITERIA_FUZZY_FLOOR:
        close = [entry for score, entry in scored if score >= _CRITERIA_FUZZY_FLOOR]
        return _resolved_refs(_preferred(close, folded_hint)), ""

    hinted = _hinted(index, folded_hint)
    if hinted is not None:
        return _resolved_refs(hinted), "criteria_unverified"
    return [], "criteria_unresolved"


def _hinted(entries: list[dict[str, Any]], folded_hint: str):
    """The entry whose marker the hint names, folding case as the sheet does."""

    if not folded_hint:
        return None
    for entry in entries:
        if str(entry["citation"]).casefold() == folded_hint:
            return entry
    return None


def _preferred(entries: list[dict[str, Any]], folded_hint: str) -> dict[str, Any]:
    return _hinted(entries, folded_hint) or entries[0]


def _resolved_refs(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The anchor shape the executor freezes into a typed evidence reference.

    ``document_id`` and ``citation_id``, not a bundle-order ``ref``: the number
    is an artefact of how this turn was assembled and means nothing outside it,
    which is exactly why the model no longer writes one. Local code holds the
    number and hands on the identifier.
    """

    return [{
        "document_id": entry["document_id"],
        "document": entry["document"],
        "citation_id": entry["citation"],
    }]


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
    """Split proposed rows into normalized-valid, failed-with-reasons, and flags.

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
    sentences = rcm_sentence_index(request)
    supplied_text = _supplied_basis_text(request)
    process_names = _rcm_process_names(request)
    business_cycle = str(request.unit_input.get("business_cycle") or "")
    normalized: list[dict] = []
    failures: list[dict] = []
    flags: list[dict] = []
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
                _normalized_rcm_row(
                    row,
                    index,
                    existing_ids,
                    answers,
                    sentences,
                    supplied_text,
                    flags,
                    process_names,
                    business_cycle,
                )
            )
        except WorkerResponseValidationError as error:
            errors = list(error.errors)
            failures.append(
                {
                    "index": index,
                    "row": row,
                    "errors": errors,
                    "stage": _failure_stage(index, errors),
                }
            )
    flags.extend(_near_duplicate_flags(rows))
    return normalized, failures, flags


#: A row error the attributes call is answerable for. Two shapes, because the
#: row gate reports a wholly absent attribute list as a missing required field
#: and a malformed one through the attribute validator's own paths.
#: Which call owns each error, by the wording the gate reports it in. Read in
#: order, and a row goes back to the *earliest* call any of its errors belongs
#: to: correcting a risk changes the control written against it, so a row that
#: failed on both starts again at the risk.
_STAGE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "risks",
        (
            "is missing process",
            "is missing risk",
            "is missing risk_rating",
            "field process",
            "field risk",
            "field risk_rating",
            "unsupported risk rating",
            "unsupported operation",
            "invalid rcm_id",
            "does not identify an existing RCM row",
            "risk quotes a percentage",
            "risk names the column",
            "is not an object",
        ),
    ),
    (
        "controls",
        (
            "is missing control",
            "field control",
            "unsupported control_type",
            "missing control_type",
            "control_owner",
            "control says what ought to happen",
            "control quotes a percentage",
            "control names the column",
        ),
    ),
    (
        "attributes",
        ("control_attributes",),
    ),
)


def _failure_stage(index: int, errors: list[str]) -> str:
    """Which call is answerable for this row's errors.

    The earliest owner of any of them. A row that failed on its rating *and* on
    its attributes goes back to the risks call, because the control and the
    attributes are written against a risk that is about to change — repairing
    the attributes of a risk that no longer says the same thing corrects
    nothing.

    An error no marker claims routes to ``risks``, which is where the sequence
    starts: an unrecognised failure re-asks everything rather than being
    quietly handed to a call that cannot fix it.
    """

    for stage, markers in _STAGE_MARKERS:
        if any(marker in error for error in errors for marker in markers):
            return stage
    return "risks"


#: The wording rules the template states, as checks rather than as prose. Each
#: was a live defect: a risk quoting "18.64%", a control naming ``GRN_ID_LINK``,
#: a control describing what management ought to do rather than what it does.
_PERCENT = re.compile(r"\d+(\.\d+)?\s*%")
_COLUMN_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
#: Recommendations wearing the grammar of a control. Refused on the control
#: field, because a control that does not exist cannot be tested and the design
#: gap is a finding rather than a row.
_ASPIRATIONAL = re.compile(
    r"\b(should|shall|must be (?:established|implemented|introduced|put in place)"
    r"|needs? to be|ought to)\b",
    re.I,
)
#: A system mechanism the basis may not state. Flagged, never refused: the
#: phrasing is sometimes exactly right, and refusing it cost correct rows.
_SYSTEM_ENFORCEMENT = re.compile(
    r"\b(?:system|portal|erp|workflow|application)\b[^.]{0,60}"
    r"\b(?:prevents?|blocks?|enforces?|validates?|restricts?|prohibits?)\b"
    r"|\bonly\b[^.]{0,40}\bselectable\b",
    re.I,
)
_CRITERIA_FLAG_MESSAGES = {
    "criteria_ambiguous": (
        "the quoted criterion appears in more than one supplied document and no "
        "marker distinguished them; the first was cited (\u201c{quote}\u201d)"
    ),
    "criteria_unverified": (
        "the quoted criterion matched no supplied sentence and was cited from "
        "its marker alone (\u201c{quote}\u201d)"
    ),
    "criteria_unresolved": (
        "the quoted criterion matched no supplied sentence, so it carries no "
        "citation (\u201c{quote}\u201d)"
    ),
}
#: Two risks whose normalized words overlap this much are one risk twice. Set
#: high on purpose: near-duplicates are worth merging, and distinct risks in one
#: cycle share a great deal of vocabulary.
_DUPLICATE_OVERLAP = 0.8


def _refuse_forbidden_wording(row: Mapping[str, Any], index: int) -> None:
    """The template's wording rules, checked rather than asked for.

    Prompt-only until now, and recommendation 7 of the quality doc had no
    deterministic check behind it. A percentage in a risk statement
    pre-concludes what fieldwork establishes; a column name ties an
    engagement-independent risk to one corpus's schema.
    """

    problems: list[str] = []
    for field in ("risk", "control"):
        value = str(row.get(field) or "")
        if not value:
            continue
        if _PERCENT.search(value):
            problems.append(
                f"RCM row {index} {field} quotes a percentage; a statistic read "
                "from a profile is not a fact about the population, and a "
                "quantified condition belongs to a test or a finding"
            )
        found = _COLUMN_TOKEN.search(value)
        if found:
            problems.append(
                f"RCM row {index} {field} names the column '{found.group(0)}'; "
                "state it in auditor wording that does not depend on this "
                "corpus's schema"
            )
    control = str(row.get("control") or "")
    if control and _NO_CONTROL not in control.casefold():
        opener = _ASPIRATIONAL.search(control)
        if opener:
            problems.append(
                f"RCM row {index} control says what ought to happen "
                f"('{opener.group(0)}'), not what management asserts is in "
                "place. Describe the control as it operates, or write \"No "
                "control identified\" — a control that does not exist cannot be "
                "tested, and the design gap is a finding rather than a row"
            )
    if problems:
        raise WorkerResponseValidationError(problems)


def _validated_control_owner(
    row: Mapping[str, Any], index: int, supplied_text: str
) -> str:
    """The owner must occur in the basis the turn was shown.

    Exact and cheap, and it is the D4 defect class: a role inferred from the
    nature of the control — an IT owner because the control sounded automated —
    is a false attribution that survives into the working paper. An empty owner
    is a question to put to the client; an invented one is an answer nobody gave.
    """

    stated = str(row.get("control_owner") or "").strip()
    if not stated:
        return ""
    if not supplied_text:
        # Nothing to check against. The response validator runs where no bundle
        # is in hand, and refusing every owner there would reject correct rows.
        return stated
    if " ".join(stated.split()).casefold() not in supplied_text:
        raise WorkerResponseValidationError(
            f"RCM row {index} names control_owner '{stated}', which does not "
            "appear in the planning basis. Leave the field empty rather than "
            "naming a role the basis does not."
        )
    return stated


def _rcm_process_names(request: WorkerRequest) -> list[str]:
    """The step names a row's ``process`` may take, in the cycle's own order."""

    return [
        str(name)
        for name in request.unit_input.get("process_names") or []
        if str(name or "").strip()
    ]


def _process_flags(
    row: Mapping[str, Any],
    index: int,
    process_names: list[str],
) -> list[dict]:
    """A process the cycle does not name. Reported, not refused — yet.

    The shape is what the vocabulary *should* be, and a row outside it is
    either a step the cycle missed or a name the matrix invented. Which of the
    two it is cannot be told from the string, and refusing on the first run of
    this would spend the risks call's one repair re-deriving a whole matrix
    over a disagreement an auditor settles by reading two lists. It becomes a
    row error once a treasuryfull and a procurement regeneration have been read
    (step 5d of docs/rcm-generation-redesign.md).
    """

    if not process_names:
        return []
    process = str(row.get("process") or "").strip()
    if any(process.casefold() == name.casefold() for name in process_names):
        return []
    return [{
        "row_index": index,
        "kind": "process_outside_cycle",
        "message": (
            f"names process \u201c{process}\u201d, which the cycle does not have; "
            f"its steps are: {', '.join(process_names)}"
        ),
    }]


def _control_flags(row: Mapping[str, Any], index: int) -> list[dict]:
    """Reported to the auditor, never refused.

    System-enforcement phrasing is right about as often as it is wrong — the
    basis sometimes does say the portal blocks it — and enforcing the rule
    rejected correct rows. The auditor is told and decides.
    """

    control = str(row.get("control") or "")
    match = _SYSTEM_ENFORCEMENT.search(control)
    if not match:
        return []
    return [{
        "row_index": index,
        "kind": "asserted_system_enforcement",
        "message": (
            f"states that a system enforces something (\u201c{match.group(0)}\u201d); "
            "confirm the planning basis says so, rather than that a field exists"
        ),
    }]


def _near_duplicate_flags(rows: object) -> list[dict]:
    """Two rows stating one risk. Reported with both indices so either can go."""

    tokens: list[tuple[int, set[str]]] = []
    for index, row in enumerate(rows or (), start=1):
        if not isinstance(row, Mapping):
            continue
        words = set(_normalized_quote(row.get("risk")).split())
        if words:
            tokens.append((index, words))
    flags: list[dict] = []
    for position, (left_index, left) in enumerate(tokens):
        for right_index, right in tokens[position + 1:]:
            overlap = len(left & right) / max(1, len(left | right))
            if overlap >= _DUPLICATE_OVERLAP:
                flags.append({
                    "row_index": left_index,
                    "kind": "near_duplicate_risk",
                    "message": (
                        f"states nearly the same risk as row {right_index} "
                        f"({overlap:.0%} of the same words); two rows describing "
                        "one underlying failure are one row"
                    ),
                })
    return flags


def _supplied_basis_text(request: WorkerRequest) -> str:
    """Everything the turn was shown, normalized once for substring checks."""

    parts: list[str] = []
    for item in request.context.items:
        content = item.content
        if isinstance(content, str):
            parts.append(content)
    return " ".join(" ".join(" ".join(parts).split()).casefold().split())


def _validated_control_type(row: Mapping[str, Any], index: int) -> str:
    """What the asserted control does about the error, or nothing where none is.

    Free text until a procurement regeneration wrote the literal "None" on the
    seven rows that had identified no control. It passed — the only check was
    that the field was not empty — and reached the matrix as a control type
    nothing can read. The earlier behaviour was no better: those rows were
    classified ``preventive`` or ``detective``, which states the mechanics of a
    control the row says does not exist.
    """

    stated = str(row.get("control_type") or "").strip()
    asserts_control = _NO_CONTROL not in str(row.get("control") or "").casefold()
    if not asserts_control:
        # A placeholder for the absence is the absence. "None", "N/A" and an
        # empty field are the same answer, and the field is cleared rather than
        # the row refused: the model got the substance right.
        return "" if stated.casefold() in {"", "none", "n/a", "not applicable"} else (
            _control_type_or_refuse(stated, index)
        )
    if not stated:
        raise WorkerResponseValidationError(
            f"RCM row {index} is missing control_type; a row asserting a control "
            f"states whether it is {' or '.join(sorted(_RCM_CONTROL_TYPES))}"
        )
    return _control_type_or_refuse(stated, index)


def _control_type_or_refuse(stated: str, index: int) -> str:
    value = stated.casefold()
    if value not in _RCM_CONTROL_TYPES:
        raise WorkerResponseValidationError(
            f"RCM row {index} has an unsupported control_type '{stated}'; it must "
            f"be exactly one of {', '.join(sorted(_RCM_CONTROL_TYPES))}. Where the "
            "row identifies no control, leave it empty rather than naming a kind "
            "of control that does not exist."
        )
    return value


def _normalized_rcm_row(
    row: object,
    index: int,
    existing_ids: set[str],
    tabular_answers: list[tuple[str, set[str]]] | None = None,
    sentences: list[dict[str, Any]] | None = None,
    supplied_text: str = "",
    flags: list[dict] | None = None,
    process_names: list[str] | None = None,
    business_cycle: str = "",
) -> dict:
    """Validate and normalize exactly one proposed RCM row."""

    if not isinstance(row, Mapping):
        raise WorkerResponseValidationError(f"RCM row {index} is not an object")
    _refuse_forbidden_wording(row, index)
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
    control_type = _validated_control_type(row, index)
    control_owner = _validated_control_owner(row, index, supplied_text)
    if flags is not None:
        flags.extend(_control_flags(row, index))
        flags.extend(_process_flags(row, index, list(process_names or [])))
    criteria_refs, criteria_flag = resolve_criteria(
        row.get("criteria"), row.get("criteria_hint"), list(sentences or [])
    )
    if criteria_flag and flags is not None:
        flags.append({
            "row_index": index,
            "kind": criteria_flag,
            "message": _CRITERIA_FLAG_MESSAGES[criteria_flag].format(
                quote=str(row.get("criteria") or "")[:120]
            ),
        })
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
    # The cycle names itself, and every row of the matrix belongs to it. The
    # row's own answer is kept only where no cycle has been designed, which is
    # the state an engagement is in before the shape exists.
    expected_cycle = (
        str(business_cycle).strip() or str(row.get("business_cycle") or "").strip()
    )
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
        "control_type": control_type,
        "control_owner": control_owner,
        "control_attributes": attributes,
        # Resolved locally from the quote, so the criterion carries a pointer to
        # the sentence it rests on and the model never writes an identifier.
        "criteria_refs": criteria_refs,
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
    return _distinct_themes(dict.fromkeys(theme for theme in themes if theme))


def _distinct_themes(themes) -> list[str]:
    """Drop a theme that only restates a fuller one in the same memorandum.

    A memo names its lens in full under the first process it applies to and
    abbreviates it under the rest — "Segregation of incompatible duties." then
    "Segregation." three times. Exact-string dedup keeps both, and the pair does
    not behave alike: the fuller one is owned by the row that answers it while
    the bare one, carrying a single abstract noun no row writes, is owned by
    nothing. That failed matrices for their vocabulary rather than their
    coverage. The fuller wording is kept because it is the memo's own statement
    of the theme; the abbreviation is shorthand for it.
    """
    tokens = {theme: _comparable(theme) for theme in themes}
    return [
        theme
        for theme in themes
        if tokens[theme]
        and not any(
            other != theme and tokens[theme] < tokens[other] for other in themes
        )
    ]


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


# A bullet's bold lead names the theme; the sentence after it is what the memo
# actually committed to. ``_BOLD_LED_ITEM`` captures only the name, which is
# what the enumeration should report — but scoring coverage against a name
# alone asks a matrix to repeat a label rather than answer a commitment, and
# fraud-frame labels ("Opportunity.") are words no control matrix will ever
# write. Both are captured here so ownership can read the commitment.
#
# The name is held to one line, exactly as ``_BOLD_LED_ITEM`` holds it, because
# the two have to enumerate the same themes. Read under DOTALL it did not: an
# unclosed ``**`` hunted across lines for its partner and took the *next*
# bullet's opening marker instead. A memo wrote "- **Collusion / rate
# manipulation.</b>." and the match ran on to swallow the bullet after it, so
# "Suspension avoidance." kept its place in the enumeration but lost its body
# here. Scored against the bare label it shared no word with the row that
# answered it in full, and a 26-row matrix was rejected twice for a coverage
# gap that did not exist. A malformed bullet may cost its own body; it may not
# cost its neighbour's.
_BOLD_LED_BODY = re.compile(
    r"^[^\S\n]*[-*][^\S\n]+\*\*[^\S\n]*([^\n]+?)[^\S\n]*:?[^\S\n]*\*\*"
    r"(.*?)(?=^[^\S\n]*[-*][^\S\n]+\*\*|\Z)",
    re.MULTILINE | re.DOTALL,
)


def risk_theme_texts(apm_markdown: str) -> dict[str, str]:
    """Each theme with the prose stating what the memo planned for it.

    Keyed by the same name :func:`planned_risk_themes` reports, so a caller can
    score against the commitment and still name the theme the way the memo did.
    A theme carrying no body — a sub-heading, or a bullet that is only a label —
    maps to itself, which is the most that can be read of it.
    """
    texts: dict[str, str] = {}
    for heading, body in _section_bodies(apm_markdown).items():
        if "risk" not in heading:
            continue
        if [match for match in _HEADING.finditer(body) if len(match.group(1)) == 3]:
            continue
        for match in _BOLD_LED_BODY.finditer(body):
            name = match.group(1).strip()
            if name:
                texts.setdefault(name, f"{name} {match.group(2).strip()}".strip())
    return texts


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


# Words a theme and a row share by writing English, not by covering the same
# risk. Scoring counted them, so a padded theme was owned by any row at all:
# "Compliance with the entity's own stated policy." passed on ``the``, while
# "Compliance." — the same lens, stated bare — failed. A match has to mean a
# shared audit word to mean anything.
_FUNCTION_WORDS = frozenset({
    "also", "all", "and", "any", "are", "been", "being", "but", "can", "could",
    "each", "for", "from", "had", "has", "have", "how", "into", "its", "may",
    "might", "must", "nor", "not", "onto", "own", "shall", "should", "some",
    "such", "than", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "those", "upon", "was", "were", "what", "when", "where",
    "which", "who", "whom", "whose", "why", "will", "with", "would",
})


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
        if token not in _FUNCTION_WORDS
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


def _theme_ownership(
    themes: list[str],
    rows: list[dict],
    texts: Mapping[str, str] | None = None,
) -> list[tuple[str, int]]:
    """Each theme with the best overlap any one row achieves against it.

    Read across the control as well as the process and risk, because that is
    what the failure this raises tells the model to fix — "add a row whose risk
    *and control* concern each" — and a row that answered the theme in its
    control was being failed for it. ``texts`` supplies what the memo said about
    each theme where the caller has it; a theme falls back to its own name.
    """
    owned = [
        _comparable(row.get("process"))
        | _comparable(row.get("risk"))
        | _comparable(row.get("control"))
        for row in rows
    ]
    scored = []
    for theme in themes:
        tokens = _comparable((texts or {}).get(theme) or theme)
        if not tokens:
            continue
        best = max((_shares(tokens, row_tokens) for row_tokens in owned), default=0)
        scored.append((theme, best))
    return scored


def _unowned_themes(
    themes: list[str], rows: list[dict], texts: Mapping[str, str] | None = None
) -> list[str]:
    """Risk themes the APM plans for that no proposed row so much as mentions.

    The matrix is the APM's risk assessment made testable. A theme the memo
    committed to and the matrix never converts into a control is how a planned
    response becomes no procedure at all — which is what happened to goods
    receipt: raised in planning, never a row, so the invoices with no receipt
    evidence had no control to fail.

    Reserved for a theme with no lexical connection to any row at all. See
    :data:`MIN_THEME_MATCH`: a stricter bar measures phrasing rather than
    coverage, and a matrix may not be judged on phrasing.
    """
    return [
        theme
        for theme, best in _theme_ownership(themes, rows, texts)
        if best < MIN_THEME_MATCH
    ]


def unowned_themes(apm_markdown: str, rows: list[dict]) -> list[str]:
    """Planned risk themes no row in the matrix so much as mentions.

    Reported to the auditor rather than enforced, which is a deliberate trade of
    coverage assurance for generation robustness. As a gate this measured what
    it could parse rather than what the matrix covered, and twice threw away a
    matrix that answered the theme: once on "Suspension avoidance." — a
    malformed bullet elsewhere in the memo cost this theme its body, leaving a
    two-word label to match against — and once on "Fraud risks considered.",
    a sub-heading that never had a body to lose because sections written as
    sub-headings are not read here at all. Each rejection cost the run two
    model calls and every row it had drafted, and neither named a real gap.

    A label the matrix does not repeat is not evidence the matrix skipped the
    theme, so it cannot decide whether a matrix is acceptable. It is still the
    sharpest coverage signal available — a theme nothing discusses is exactly
    how a planned response becomes no procedure, which is what happened to goods
    receipt: raised in planning, never a row, so the invoices with no receipt
    evidence had no control to fail. The auditor decides, on a matrix that
    exists rather than on one that was discarded.
    """
    return _unowned_themes(
        planned_risk_themes(apm_markdown), rows, risk_theme_texts(apm_markdown)
    )


def weakly_owned_themes(apm_markdown: str, rows: list[dict]) -> list[str]:
    """Themes whose ownership rests on a single shared word.

    Reported to the auditor rather than enforced. On a matrix that genuinely
    covered every theme, three of ten sat here — so this cannot decide whether
    a matrix is acceptable, and saying so is the whole of its usefulness.

    Scored against the theme's name alone, unlike the gate in
    :func:`_unowned_themes`. The gate asks whether the matrix converted the
    memo's commitment at all, which is a question about the commitment; this
    asks whether the connection is thin, and against a whole paragraph of body
    prose two shared words are free, which would retire the report without
    anything saying so. Naming the lens is the thin-coverage signal worth
    keeping: a matrix that answers "Segregation of incompatible duties." while
    never writing the word is exactly what an auditor should be told.
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
    # Theme coverage was judged here once. It is reported to the auditor now
    # instead — see :func:`unowned_themes` for what it cost as a gate.
    #
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
    normalized, failures, flags = _partition_rcm_rows(rows, request)
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
    if flags:
        # Reported, never enforced. Each is a judgement an auditor makes better
        # than a regex: whether the basis really does say the portal blocks it,
        # whether two similar risks are one. ``_validated_rcm`` in the executor
        # reads ``rows`` alone, so nothing downstream changes shape.
        accepted["flags"] = flags
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


def _rcm_risks_user(request: WorkerRequest) -> str:
    """The risks call's message: the memorandum, the methodology, and no more.

    Documents, table profiles, small-table rows and table metadata are in the
    bundle and are withheld from here. A risk is what could go wrong in this
    cycle whether or not this engagement's data happens to show it, and a turn
    shown the evidence writes the evidence back as risks.

    ``EXISTING RISKS`` is the risk half of the current rows and nothing else:
    the call revises risks, so the controls and attributes already on those rows
    would only invite it to restate them.
    """

    template = str(_resolved_item(request, "rcm_template") or "")
    current_apm = str(_resolved_item(request, "current_apm") or "")
    instruction = auditor_instruction(request)
    return json.dumps(
        {
            "ACTIVE RISK TEMPLATE (verbatim)": template,
            "REVISED APM": current_apm,
            **({"auditor_instruction": instruction} if instruction else {}),
            # The closed vocabulary. Supplied even when empty, so the rule in
            # the system prompt is never left pointing at a key that is not
            # there — an engagement with no cycle designed gets an empty list
            # and the flag below rather than silence.
            "PROCESS NAMES": _rcm_process_names(request),
            "BUSINESS CYCLE": str(request.unit_input.get("business_cycle") or ""),
            "EXISTING RISKS": [
                {
                    "rcm_id": str(row.get("id") or ""),
                    "process": str(row.get("process") or ""),
                    "risk": str(row.get("risk") or ""),
                    "risk_rating": str(row.get("risk_rating") or ""),
                }
                for row in _current_rcm_rows(request)
                if isinstance(row, Mapping)
            ],
            "RESOLVED CONTEXT": _context_from_sources(
                request, "planning_context", "methodology"
            ),
            "INSTRUCTIONS": (
                "Return the full set of proposed risks. Work in two passes: "
                "first enumerate the standard risks of every in-scope process from "
                "your own knowledge of the cycle, then tailor wording, rating and "
                "process to this engagement using the memorandum. Do not stop at "
                "the risks the supplied material happens to comment on. For an "
                "existing risk, include operation='update' and its exact rcm_id. Use "
                "operation='create' only for a genuinely uncovered risk. Omission "
                "never deletes an existing row."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )


def _controls_user(request: WorkerRequest, rows: list[dict]) -> str:
    """The controls call's message: the settled risks, and the basis to read.

    The one call given the engagement's own material, because describing what
    management asserts is grounded reading. No citation register: a criterion
    is quoted and matched back to its sentence locally, so nothing here is an
    identifier the model has to copy.

    The memorandum is deliberately not here, and it was tried. Adding it moved
    every criterion onto it: nine of nine on one treasury regeneration were
    quoted verbatim from the memo, and the memo is generated planning prose
    carrying no ``[C...]`` anchors, so that engagement went from ten of ten
    criteria carrying a citation to none. A criterion should rest on the policy
    the entity issued, not on this memorandum's paraphrase of it.

    Nothing else about that change survived inspection. It appeared to improve
    control identification and attribute enumeration, but ``risk_rating`` — a
    field this call does not write, produced by a turn identical across both
    variants — moved by up to 34 points between the same two runs. One run per
    variant cannot separate an effect that size from the noise, and only the
    citation displacement was verified directly, by reading where the quotes
    came from.
    """

    instruction = auditor_instruction(request)
    return json.dumps(
        {
            "ACTIVE CONTROL TEMPLATE (verbatim)": str(
                _resolved_item(request, "rcm_controls_template") or ""
            ),
            **({"auditor_instruction": instruction} if instruction else {}),
            "RISKS": [
                {
                    "row_index": index,
                    "process": str(row.get("process") or ""),
                    "risk": str(row.get("risk") or ""),
                    "risk_rating": str(row.get("risk_rating") or ""),
                }
                for index, row in enumerate(rows, start=1)
            ],
            "RESOLVED CONTEXT": _context_from_sources(
                request,
                RCM_DOCUMENT_SOURCE_ID,
                "small_table_rows",
                "table_profiles",
                "table_metadata",
            ),
            "INSTRUCTIONS": (
                "Return one controls entry per supplied row, each carrying its "
                "exact row_index. Describe the control management asserts is in "
                "place against that row's risk, or write \"No control "
                "identified\". Every row gets an entry: a row you omit reaches "
                "the gate with no control and fails there."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )


def _with_controls(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    rows: list[dict],
) -> list[dict]:
    """Ask for the controls of these risks and splice them on."""

    if not rows:
        return rows
    response = gateway.complete(
        RCM_CONTROLS_SYSTEM,
        _controls_user(request, rows),
        _rcm_activity(request, "rcm_controls"),
        attempt=attempt.number,
    )
    try:
        return _merged_controls(rows, response)
    except WorkerResponseValidationError:
        # Nothing to splice. The rows reach the gate without a control and fail
        # there as such, which is bounded and repairable.
        return rows


#: What the controls call owns. A field outside this set arriving in its
#: response is discarded rather than spliced: the risks are settled, and a
#: control turn quietly re-rating one is the failure this split exists to stop.
_RCM_CONTROL_FIELDS = frozenset(
    {"control", "control_type", "control_owner", "criteria", "criteria_hint"}
)


def _merged_controls(rows: list[dict], response: str) -> list[dict]:
    """Write returned controls onto the risks that asked for them, by index."""

    entries = _first_json_object(response).get("controls")
    if not isinstance(entries, list):
        raise WorkerResponseValidationError(
            "the controls response must be a JSON object with a `controls` array"
        )
    by_index: dict[int, dict] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        try:
            index = int(entry.get("row_index"))
        except (TypeError, ValueError):
            continue
        by_index[index] = {
            key: _plain_json(value)
            for key, value in entry.items()
            if key in _RCM_CONTROL_FIELDS
        }
    return [
        {**row, **by_index[index]} if index in by_index else row
        for index, row in enumerate(rows, start=1)
    ]


def _parsed_rows(response: str) -> list[dict]:
    """Parse a `rows` document, raising the schema error the registry expects."""

    parsed = _rcm_response_schema(response)
    return [_plain_json(row) for row in parsed["rows"]]


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

    Three calls at three altitudes, because they are three different jobs.

    Risks is domain recall: what could go wrong in this cycle, from knowledge of
    the cycle, and it is shown the memorandum and no engagement material at all
    — a risk set assembled from the supplied evidence is a description of the
    evidence. Controls is grounded reading: what management asserts it does,
    which needs the documents and the profiles and is the only call given them.
    Attributes is classification from supplied lists.

    Fused, a defect in any one cost the whole output, and the whole output was
    the largest completion in the system. Split, a repair is scoped to the rows
    that failed and routed to the call whose job it was.

    What a transaction-cycle attribute must *show* is settled further
    downstream still, by the stage that has this engagement's document schemas
    in front of it.
    """

    if attempt.is_repair:
        if attempt.previous_response is None:
            raise WorkerContractError("An RCM repair requires the previous response.")
        return _repaired_rcm(request, gateway, attempt, _rcm_risks_user(request))
    response = gateway.complete(
        RCM_RISKS_SYSTEM,
        _rcm_risks_user(request),
        _rcm_activity(request, "rcm_risks"),
        attempt=attempt.number,
    )
    try:
        rows = _parsed_rows(response)
    except WorkerResponseValidationError:
        # A worker returns response text; the registry owns rejection and the
        # bounded repair that follows it. Raising from here would escape that
        # loop entirely, so an unusable draft is handed back verbatim to be
        # rejected — and repaired — through the normal path. Nothing is spent on
        # the later calls over rows that do not parse.
        return response
    rows = _with_controls(request, gateway, attempt, rows)
    return _rcm_document(
        request, attempt, _with_attributes(request, gateway, attempt, rows)
    )


def _attributes_user(request: WorkerRequest, rows: list[dict]) -> str:
    """The attributes call's message: the rows, and where an answer could live.

    No engagement prose. This call classifies, and the documents and profiles
    that would let it second-guess a risk are exactly what it must not do.
    Tables by name and column name, record kinds by name and count: enough to
    answer "can the population answer this, or does it need the documents",
    which is the whole of what ``evidence_kind`` asks.
    """

    instruction = auditor_instruction(request)
    return json.dumps(
        {
            **({"auditor_instruction": instruction} if instruction else {}),
            "ROWS": [
                {
                    "row_index": index,
                    "process": str(row.get("process") or ""),
                    "risk": str(row.get("risk") or ""),
                    "control": str(row.get("control") or ""),
                    "control_type": str(row.get("control_type") or ""),
                }
                for index, row in enumerate(rows, start=1)
            ],
            "TABLES": _table_column_names(request),
            "DOCUMENT TYPES HELD": _plain_json(
                request.unit_input.get("document_types") or []
            ),
            "ACTIVE ATTRIBUTE TEMPLATE (verbatim)": str(
                _resolved_item(request, "rcm_attributes_template") or ""
            ),
            "INSTRUCTIONS": (
                "Return one attributes entry per supplied row, each carrying "
                "its exact row_index and its control_attributes. Every row "
                "gets an entry: a row you omit reaches the gate with no "
                "attributes and fails there."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )


def _table_column_names(request: WorkerRequest) -> list[dict]:
    """Each supplied population as its name and its column names.

    Not the profile. A null percentage or a maximum says nothing about where an
    answer lives, and reading one as an exception rate is the mistake the
    template spends a section warning against — so this call is never shown a
    statistic it could misread.
    """

    tables: list[dict] = []
    for item in _supplied_items(request, "table_metadata"):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("table") or item.get("name") or "")
        columns = [
            str(column.get("name") or "")
            for column in item.get("columns") or []
            if isinstance(column, Mapping) and column.get("name")
        ]
        if name:
            tables.append({"table": name, "columns": columns})
    return tables


def _with_attributes(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    rows: list[dict],
) -> list[dict]:
    """Ask for the attributes of these rows and splice them on."""

    if not rows:
        return rows
    response = gateway.complete(
        RCM_ATTRIBUTES_SYSTEM,
        _attributes_user(request, rows),
        _rcm_activity(request, "rcm_attributes"),
        attempt=attempt.number,
    )
    try:
        return _merged_attributes(rows, response)
    except WorkerResponseValidationError:
        # Nothing to splice. The rows reach the gate without attributes and
        # fail there as such, which is bounded and repairable — where raising
        # from inside the worker would escape the loop that exists to fix it.
        return rows


def _merged_attributes(rows: list[dict], response: str) -> list[dict]:
    """Write returned attributes onto the rows that asked for them, by index.

    An entry naming no row, or naming one twice, is not written. That row then
    fails the gate as a row with no attributes — the honest outcome, and one
    the scoped attributes repair can act on. Inventing an attribute here would
    answer for a control this call never classified.
    """

    entries = _first_json_object(response).get("attributes")
    if not isinstance(entries, list):
        raise WorkerResponseValidationError(
            "the attributes response must be a JSON object with an "
            "`attributes` array"
        )
    by_index: dict[int, object] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        try:
            index = int(entry.get("row_index"))
        except (TypeError, ValueError):
            continue
        if "control_attributes" in entry:
            by_index[index] = _plain_json(entry["control_attributes"])
    return [
        row if index not in by_index else {**row, "control_attributes": by_index[index]}
        for index, row in enumerate(rows, start=1)
    ]


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
    accepted, still_failing, _flags = _partition_rcm_rows(rows, request)
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


def _repaired_rcm(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    user: str,
) -> str:
    """Correct only the rows that failed, at the call whose job each was.

    A row flows forward from wherever it re-enters: repaired at the risks call
    it then goes through controls and attributes again, because both were
    written against a risk that has changed. Repaired at controls it goes
    through attributes. Repaired at attributes it stops there.
    """

    try:
        rows = _parsed_rows(str(attempt.previous_response))
    except WorkerResponseValidationError:
        # The prior draft was rejected by the schema rather than the quality
        # gate — a linked retry from a parent run can start here — so there are
        # no rows to scope to and the whole risk set is re-asked. It is a risks
        # response like any other and flows through the later calls.
        return _redrafted_matrix(
            request,
            gateway,
            attempt,
            user
            + "\n\nThe previous response could not be parsed: "
            + "; ".join(attempt.validation_errors)
            + ". Return the complete JSON object.",
        )
    _, failures, _flags = _partition_rcm_rows(rows, request)
    document_errors = document_level_errors(
        request, [row for row in rows if isinstance(row, Mapping)]
    )
    if not failures and not document_errors:
        # Nothing reproduced. The gate rejected the draft and this pass cannot
        # see why, so the whole matrix is asked again rather than a repair
        # scoped to nothing.
        return _redrafted_matrix(
            request,
            gateway,
            attempt,
            user
            + "\n\nThe previous matrix failed the engagement quality gate: "
            + "; ".join(attempt.validation_errors)
            + ". Return the complete matrix again, correcting every listed "
            "error and preserving every other row unchanged.",
        )
    by_stage = {
        stage: [item for item in failures if item["stage"] == stage]
        for stage, _markers in _STAGE_MARKERS
    }
    repaired = rows

    if by_stage["risks"]:
        response = gateway.complete(
            RCM_RISKS_SYSTEM,
            _scoped_repair_user(
                "RISKS TO CORRECT",
                by_stage["risks"],
                "Each supplied row failed the engagement quality gate for the "
                "listed reasons. Return an object with `rows` containing one "
                "corrected row per supplied row, each carrying its exact "
                "row_index. Correct every listed error and change nothing "
                "else. Rows not supplied here are already accepted and must "
                "not be returned: they are preserved unchanged.",
                keep=_RCM_RISK_FIELDS,
            ),
            _rcm_activity(request, "rcm_risks_repair"),
            attempt=attempt.number,
        )
        try:
            repaired = _repair_scoped_rows(rows, by_stage["risks"], response)
        except WorkerResponseValidationError:
            return response

    # A row whose risk was rewritten needs its control written against the new
    # wording, and a row that failed at the control needs it corrected. One
    # call, scoped to exactly those rows.
    control_scope = _merged_scope(by_stage["risks"], by_stage["controls"])
    if control_scope:
        repaired = _repaired_stage(
            request,
            gateway,
            attempt,
            repaired,
            control_scope,
            system=RCM_CONTROLS_SYSTEM,
            envelope="CONTROLS TO CORRECT",
            worker_kind="rcm_controls_repair",
            merge=_merged_controls,
            instructions=(
                "Return an object with `controls` containing one entry per "
                "supplied row, each carrying its exact row_index and its "
                "corrected control fields. Correct every listed error. Rows "
                "not supplied here are already accepted and must not be "
                "returned."
            ),
            extra=lambda index, row: {
                "risk": str(row.get("risk") or ""),
                "current_control": str(row.get("control") or ""),
            },
        )

    attribute_scope = _merged_scope(
        by_stage["risks"], by_stage["controls"], by_stage["attributes"]
    )
    if document_errors:
        # The matrix is wrong as a whole: no control requirement asserts that
        # recorded values agree. That gate reads attribute requirement text, so
        # the correction is the attributes call's — and it needs every row in
        # view, because the fix is to *add* a requirement and the scoped
        # envelope forbids returning a row it was not given. The earlier calls
        # are never re-asked for a document-level error; where a row also failed
        # on its own terms it was re-asked above, for that.
        for index in range(1, len(repaired) + 1):
            attribute_scope.setdefault(index, []).extend(document_errors)
    pending = [
        {
            "row_index": index,
            "row": _row_without_attributes(repaired[index - 1]),
            "current_attributes": _plain_json(
                repaired[index - 1].get("control_attributes")
            ),
            "errors": errors,
        }
        for index, errors in sorted(attribute_scope.items())
        if index <= len(repaired)
    ]
    return _rcm_document(
        request,
        attempt,
        _repaired_attributes(request, gateway, attempt, repaired, pending),
    )


#: What the risks call owns, and all a scoped risk repair is shown of a row.
_RCM_RISK_FIELDS = ("operation", "rcm_id", "process", "risk", "risk_rating",
                    "business_cycle")


def _merged_scope(*failure_groups: list[dict]) -> dict[int, list[str]]:
    """Row indices to re-ask, with every reason gathered under each."""

    scope: dict[int, list[str]] = {}
    for group in failure_groups:
        for failure in group:
            scope.setdefault(failure["index"], []).extend(failure["errors"])
    return scope


def _scoped_repair_user(
    envelope: str,
    failures: list[dict],
    instructions: str,
    *,
    keep: tuple[str, ...] | None = None,
) -> str:
    """The scoped repair message: the failing rows, projected, and their errors."""

    return json.dumps(
        {
            envelope: [
                {
                    "row_index": failure["index"],
                    "row": _projected_row(failure["row"], keep),
                    "errors": failure["errors"],
                }
                for failure in failures
            ],
            "INSTRUCTIONS": instructions,
        },
        indent=1,
        ensure_ascii=False,
    )


def _projected_row(row: object, keep: tuple[str, ...] | None) -> dict:
    if not isinstance(row, Mapping):
        return {}
    if keep is None:
        return _row_without_attributes(row)
    return {key: _plain_json(row[key]) for key in keep if key in row}


def _repaired_stage(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    rows: list[dict],
    scope: dict[int, list[str]],
    *,
    system: str,
    envelope: str,
    worker_kind: str,
    merge,
    instructions: str,
    extra,
) -> list[dict]:
    """Re-ask one call for exactly the rows that need it, and splice the answer."""

    pending = [
        {
            "row_index": index,
            **extra(index, rows[index - 1]),
            "errors": errors,
        }
        for index, errors in sorted(scope.items())
        if index <= len(rows)
    ]
    if not pending:
        return rows
    response = gateway.complete(
        system,
        json.dumps(
            {envelope: pending, "INSTRUCTIONS": instructions},
            indent=1,
            ensure_ascii=False,
        ),
        _rcm_activity(request, worker_kind),
        attempt=attempt.number,
    )
    try:
        merged = merge(rows, response)
    except WorkerResponseValidationError:
        # The rows keep what they had; the gate reports them again and the
        # quarantine sets aside what will not repair.
        return rows
    asked = set(scope)
    return [
        merged[index - 1] if index in asked else row
        for index, row in enumerate(rows, start=1)
    ]


def _redrafted_matrix(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    user: str,
) -> str:
    """Re-ask the whole risk set, then flow it through the later two calls."""

    response = gateway.complete(
        RCM_RISKS_SYSTEM,
        user,
        _rcm_activity(request, "rcm_risks_repair"),
        attempt=attempt.number,
    )
    try:
        rows = _parsed_rows(response)
    except WorkerResponseValidationError:
        return response
    rows = _with_controls(request, gateway, attempt, rows)
    return _rcm_document(
        request, attempt, _with_attributes(request, gateway, attempt, rows)
    )


def _row_without_attributes(row: object) -> dict:
    """A row as the attributes call is shown it: everything but its attributes."""

    if not isinstance(row, Mapping):
        return {}
    return {
        key: _plain_json(value)
        for key, value in row.items()
        if key != "control_attributes"
    }


def _repaired_attributes(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
    rows: list[dict],
    pending: list[dict],
) -> list[dict]:
    """Re-ask the attributes call for exactly the rows that need it."""

    if not pending:
        return rows
    response = gateway.complete(
        RCM_ATTRIBUTES_SYSTEM,
        json.dumps(
            {
                "ATTRIBUTES TO CORRECT": pending,
                "TABLES": _table_column_names(request),
                "DOCUMENT TYPES HELD": _plain_json(
                    request.unit_input.get("document_types") or []
                ),
                "ACTIVE ATTRIBUTE TEMPLATE (verbatim)": str(
                    _resolved_item(request, "rcm_attributes_template") or ""
                ),
                "INSTRUCTIONS": (
                    "Return an object with `attributes` containing one entry "
                    "per supplied row, each carrying its exact row_index and "
                    "its corrected control_attributes. Correct every listed "
                    "error. Rows not supplied here are already accepted and "
                    "must not be returned."
                ),
            },
            indent=1,
            ensure_ascii=False,
        ),
        _rcm_activity(request, "rcm_attributes_repair"),
        attempt=attempt.number,
    )
    try:
        merged = _merged_attributes(rows, response)
    except WorkerResponseValidationError:
        # The rows keep whatever attributes they had; the gate reports them
        # again, and the quarantine sets aside what will not repair.
        return rows
    scoped = {int(item["row_index"]) for item in pending}
    # Only the rows this call was asked about. A response naming one it was not
    # would otherwise replace attributes the gate already accepted.
    return [
        merged[index - 1] if index in scoped else row
        for index, row in enumerate(rows, start=1)
    ]


RCM_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.rcm.response",
    schema_hash=_sha256_text(
        "rcm-response:v7:first-json-object-with-rows-array-spliced-controls-"
        "resolved-criteria-uncontracted-attributes-quarantine-and-flags"
    ),
    validator=_rcm_response_schema,
)
RCM_WORKER = WorkerDefinition(
    worker_id=RCM_WORKER_ID,
    # Both prompts: the sequence is what decides what reaches the model, so
    # both halves belong in the identity a persisted proposal is reused against.
    prompt_hash=_sha256_text(
        RCM_RISKS_SYSTEM + RCM_CONTROLS_SYSTEM + RCM_ATTRIBUTES_SYSTEM
    ),
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
operation. {JSON_RULES} {LANGUAGE_RULES}"""

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


# --------------------------------------------------------------------------- #
# planning.cycle worker (step 5 of docs/rcm-generation-redesign.md)
# --------------------------------------------------------------------------- #
CYCLE_WORKER_ID = "planning.cycle"
CYCLE_SYSTEM = f"""[agent:planning_cycle]
Name the steps of the process this engagement audits, as the audit planning
memorandum names them, in the order it gives them. The memorandum's process
flow is the source; do not invent a step it does not describe, and do not
merge two it separates.

Return {{"name": ..., "steps": [...], "cross_cutting": {{...}}}}.

- `name` is the cycle as a whole, in the entity's own words where the
  memorandum gives them ("Procure-to-pay", "Treasury dealing and settlement").
- Each step has `name`, `roles`, `populations` and `themes`.
- `roles` are the document types that record the step, chosen from DOCUMENT
  TYPES HELD and spelled exactly as listed. A role's `name` is a short
  lowercase identifier for the position it fills in the cycle (`order`,
  `receipt`, `invoice`), unique across the whole cycle, and is not the type id
  repeated. A step no held document type records has no roles; say so by
  leaving the list empty rather than by naming a type the engagement lacks.
- `populations` are the tables whose rows *are* that step, chosen from TABLES
  and spelled exactly. Where a step's records live on another step's table,
  give that table with the `columns` that hold them. A step with no table has
  an empty list.
- Flag `anchor: true` on the one population a test of this cycle would start
  from — the table whose rows are the transactions being vouched. Exactly one
  population in the whole cycle carries it.
- `themes` assigns each entry of PLANNED RISK THEMES to the one step that
  answers it, copied verbatim. A theme that belongs to no single step goes to
  `cross_cutting`. Every theme is placed exactly once.
- `cross_cutting` is one bucket for what runs across the cycle rather than
  within a step — override, monitoring, segregation. Give it a `name` and its
  `themes`.

Where EXISTING STEP NAMES or EXISTING PROCESS NAMES describe a step you are
naming, reuse that wording exactly: the matrix's rows are grouped by it, and
renaming a step it already uses orphans them.

You are describing the process, not assessing it. No risks, no controls, no
judgement about whether a step is adequately covered. {JSON_RULES} """ + LANGUAGE_RULES

CYCLE_PLANNING_SOURCE_ID = "planning_context"
CYCLE_CURRENT_APM_SOURCE_ID = "current_apm"


def _cycle_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError("the cycle response is not an object")
    steps = payload.get("steps")
    if not isinstance(steps, (list, tuple)):
        raise WorkerResponseValidationError("the cycle response carries no steps")
    return {
        "name": str(payload.get("name") or ""),
        "steps": [_plain_json(step) for step in steps],
        "cross_cutting": _plain_json(payload.get("cross_cutting")),
    }


def _cycle_allowed_values(request: WorkerRequest) -> tuple[list[str], list[str]]:
    """The document types and tables this call may name, in the order supplied."""

    types = [
        str(entry.get("document_type") or "")
        for entry in request.unit_input.get("document_types") or []
        if isinstance(entry, Mapping) and entry.get("document_type")
    ]
    tables = [entry["table"] for entry in _table_column_names(request)]
    return types, tables


def _cycle_placed_themes(cycle: Mapping[str, Any]) -> list[str]:
    placed = [
        str(theme)
        for step in cycle.get("steps") or []
        if isinstance(step, Mapping)
        for theme in step.get("themes") or []
    ]
    cross = cycle.get("cross_cutting")
    if isinstance(cross, Mapping):
        placed.extend(str(theme) for theme in cross.get("themes") or [])
    return placed


def _cycle_with_unplaced_themes(
    cycle: dict[str, Any],
    planned: list[str],
) -> dict[str, Any]:
    """Assign a theme the response left out to the cross-cutting bucket.

    Locally rather than by refusing the response: a theme nobody claimed is a
    theme no step owns, which is what the cross-cutting bucket is for, and
    spending the one repair on re-deriving a whole shape to move one string
    would be paying the largest available price for the smallest defect. A
    theme placed *twice* is refused instead — that is a contradiction the
    model has to resolve, not an omission local code can settle.
    """
    placed = {theme.casefold() for theme in _cycle_placed_themes(cycle)}
    missing = [theme for theme in planned if theme.casefold() not in placed]
    if not missing:
        return cycle
    cross = dict(cycle.get("cross_cutting") or {})
    cross.setdefault("name", "Cross-cutting")
    cross["themes"] = [*(cross.get("themes") or []), *missing]
    return {**cycle, "cross_cutting": cross}


def validate_cycle_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Gate one proposed cycle shape against the vocabularies it was given.

    The structural rules are :func:`planning_cycle.validate_cycle_shape`, which
    is also what the commit and every auditor edit run through — checked here
    against the *supplied* lists rather than against a workspace this worker
    must not reach, so a shape that passes here is a shape the workspace will
    store. What is added is the part only this turn knows: the themes it was
    asked to place.
    """
    planned = [
        str(theme) for theme in request.unit_input.get("risk_themes") or [] if theme
    ]
    cycle = _cycle_with_unplaced_themes(dict(proposal), planned)
    allowed_types, allowed_tables = _cycle_allowed_values(request)
    try:
        return planning_cycle.validate_cycle_shape(
            cycle,
            allowed_types=set(allowed_types),
            base_tables=set(allowed_tables),
        )
    except planning_cycle.CycleShapeError as error:
        raise WorkerResponseValidationError(str(error)) from error


def _cycle_user(request: WorkerRequest) -> str:
    return json.dumps(
        {
            "REVISED APM": str(_resolved_item(request, CYCLE_CURRENT_APM_SOURCE_ID) or ""),
            "PLANNED RISK THEMES": [
                str(theme) for theme in request.unit_input.get("risk_themes") or []
            ],
            "DOCUMENT TYPES HELD": _plain_json(
                request.unit_input.get("document_types") or []
            ),
            "TABLES": _table_column_names(request),
            "TABLE JOINS ALREADY INFERRED": _plain_json(
                request.unit_input.get("joins") or []
            ),
            "EXISTING STEP NAMES": [
                str(name) for name in request.unit_input.get("existing_steps") or []
            ],
            "EXISTING PROCESS NAMES": [
                str(name) for name in request.unit_input.get("existing_processes") or []
            ],
            "RESOLVED CONTEXT": _context_from_sources(
                request, CYCLE_PLANNING_SOURCE_ID
            ),
        },
        indent=1,
        ensure_ascii=False,
    )


def run_cycle_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform the supplied bundle into one small structured cycle request."""

    user = _cycle_user(request)
    if attempt.is_repair:
        if attempt.previous_response:
            user += "\n\nPREVIOUS CYCLE DRAFT:\n" + attempt.previous_response
        user += (
            "\n\nThe previous cycle draft failed the engagement quality gate: "
            + "; ".join(attempt.validation_errors)
            + ". Return the complete corrected cycle, keeping the steps that "
            "did not fail."
        )
    return gateway.complete(
        CYCLE_SYSTEM,
        user,
        _rcm_activity(request, "planning_cycle"),
        attempt=attempt.number,
    )


CYCLE_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="planning.cycle.response",
    schema_hash=_sha256_text("cycle-response:v1:name-steps-roles-populations-themes"),
    validator=_cycle_response_schema,
)
CYCLE_WORKER = WorkerDefinition(
    worker_id=CYCLE_WORKER_ID,
    prompt_hash=_sha256_text(CYCLE_SYSTEM),
    response_schema=CYCLE_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair a cycle shape against the supplied types, tables and themes."
        ),
    ),
    implementation=run_cycle_worker,
    semantic_validator=validate_cycle_proposal,
)

WORKERS.register(CYCLE_WORKER)



__all__ = [
    "APM_RESPONSE_SCHEMA",
    "APM_SYSTEM",
    "APM_WORKER",
    "APM_WORKER_ID",
    "CYCLE_RESPONSE_SCHEMA",
    "CYCLE_SYSTEM",
    "CYCLE_WORKER",
    "CYCLE_WORKER_ID",
    "PLANNING_CONTEXT_FIELDS",
    "PLANNING_CONTEXT_RESPONSE_SCHEMA",
    "PLANNING_CONTEXT_SYSTEM",
    "PLANNING_CONTEXT_WORKER",
    "PLANNING_CONTEXT_WORKER_ID",
    "RCM_RESPONSE_SCHEMA",
    "RCM_ATTRIBUTES_SYSTEM",
    "RCM_CONTROLS_SYSTEM",
    "RCM_RISKS_SYSTEM",
    "RCM_WORKER",
    "RCM_WORKER_ID",
    "run_apm_worker",
    "run_cycle_worker",
    "run_planning_context_worker",
    "run_rcm_worker",
    "validate_apm_proposal",
    "validate_cycle_proposal",
    "validate_planning_context_proposal",
    "validate_rcm_proposal",
]
