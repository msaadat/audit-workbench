"""Registered model workers for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import cycle_vouching
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
)


APM_WORKER_ID = "planning.apm"
APM_SYSTEM = """[agent:apm]
Draft an audit planning memorandum grounded only in the supplied planning
basis. Document content and methodology excerpts may be present. Methodology
must be cited by pack/version/section. Preserve the
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

A population summary may be supplied: the row counts of the imported tables and
the observed range of their date and numeric columns. Where the planning context
states no audit period and this summary carries an `observed_period`, state that
range as the proposed period, attributed to the populations received and marked
for confirmation with the auditee. It is an observation about the data, not an
asserted scope — say which. Do not report the period as unavailable when a range
was observed, and do not infer one where no range was supplied.

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


def _observed_period(request: WorkerRequest) -> Mapping[str, Any] | None:
    """The date range derived from the imported populations, where one exists."""
    for item in _supplied_items(request, "population_summary"):
        if isinstance(item, Mapping):
            observed = item.get("observed_period")
            if isinstance(observed, Mapping) and observed.get("start"):
                return observed
    return None


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
    for field_name in ("objective", "scope", "period"):
        if structured.get(field_name) and re.search(
            rf"\b{field_name}\b.{{0,80}}{_UNAVAILABLE}",
            normalized,
        ):
            raise WorkerResponseValidationError(
                f"the memorandum says {field_name} is unavailable despite structured context"
            )
    # The period is the one planning fact the populations can supply when the
    # context does not. Declaring it unavailable with a range observed in the
    # data is the specific defect this catches.
    period_derivable = not structured.get("period") and _observed_period(request)
    if period_derivable and re.search(
        rf"\bperiod\b.{{0,80}}{_UNAVAILABLE}",
        normalized,
    ):
        raise WorkerResponseValidationError(
            "the memorandum reports the audit period as unavailable although the "
            "supplied population summary carries an observed date range; state it "
            "as the proposed period, attributed to the populations and subject to "
            "confirmation"
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
        user += (
            "\n\nThe previous APM draft failed the engagement quality gate: "
            + "; ".join(attempt.validation_errors)
            + ". Return a complete corrected memorandum."
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
    implementation_hash=_sha256_text(inspect.getsource(run_apm_worker)),
    prompt_hash=_sha256_text(APM_SYSTEM),
    response_schema=APM_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair missing APM template sections and structured-context contradictions."
        ),
    ),
    implementation=run_apm_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_apm_proposal)),
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
- A transaction_cycle attribute states evidence_kind and stops there. Do not
  write registry, required_record_kinds, required_comparisons, or
  comparison_recipes: the evidence contract for those attributes is authored in a
  separate step that is given the installed pack catalog, and the row's
  business_cycle is derived from it locally.
- criteria and control_owner are optional: cite or name only what the planning
  basis supplies, and leave the field empty otherwise rather than guessing.
- Supplied table profiles are value-free shape statistics, not evidence. A null
  percentage is not an exception rate; a maximum is not a policy limit.
- One risk and one control per row. {JSON_RULES}"""

# An RCM row selects a pack and its record kinds; it never names an identifier,
# field, or normalizer. Supplying only what the row can legitimately reference
# keeps the prompt small and stops the cycle vocabulary reading as the preferred
# evidence strategy simply because it is described at greater length.
_RCM_CANONICAL_PACK_REFERENCES = [
    {
        "registry": {
            "pack_id": pack["id"],
            "pack_version": pack["version"],
            "definition_hash": pack["definition_hash"],
        },
        "record_kinds": [
            {
                "id": record_kind["id"],
                "available_field_kinds": record_kind["available_field_kinds"],
            }
            for record_kind in pack["record_kinds"]
            if record_kind["bindable"]
        ],
        "field_kinds": [
            {
                "id": field["id"],
                "group": field["group"],
                "kind": field["kind"],
                "attributes": [
                    {
                        "id": attribute["id"],
                        "semantic_type": attribute["semantic_type"],
                        "control_evidence": bool(
                            attribute.get("control_evidence", False)
                        ),
                    }
                    for attribute in field["attributes"]
                ],
            }
            for field in pack["field_kinds"]
        ],
    }
    for pack in cycle_vouching.metadata()["registry"]["packs"]
]
_RCM_PACK_IDS = tuple(
    str(pack["id"]) for pack in cycle_vouching.metadata()["registry"]["packs"]
)

# --------------------------------------------------------------------------- #
# The second pass: the evidence contract for transaction-cycle attributes only.
#
# This vocabulary used to live in the judgment prompt above, where it was 11kB
# of pack catalog carried by every RCM turn — including the great majority whose
# attributes never touch it — while the operator vocabulary it depends on was
# stated nowhere at all. Splitting it out puts the catalog only in the call that
# needs it, and lets that call state the DSL properly.
# --------------------------------------------------------------------------- #
RCM_EVIDENCE_SYSTEM = f"""[agent:rcm_evidence]
Name the audit shapes that would answer control attributes already judged to
need linked source records. The risk, control, and requirement are settled: do
not revise them, and do not add or remove attributes.

You are choosing what would prove the requirement, not how it will be tested
here. Which record kinds fill a shape's placeholders, and whether this
engagement's evidence can fill them at all, is decided later against the
extracted records — you are not shown those, and must not guess at them.

Return an object with `contracts`, one entry per supplied attribute, each with
row_index and attribute_key copied exactly from the request, plus:
- registry: an object with exactly pack_id, pack_version, and definition_hash,
  copied verbatim from one installed pack below. All attributes of one row must
  use the same pack. The pack is the business cycle the control belongs to.
- comparison_recipes: a non-empty array of {{"recipe_id": "<id>"}} objects and
  nothing else. No bindings, no record kinds, no field selectors, no operators.
  Cite each shape once; a shape used against two different record pairs is bound
  twice downstream, not cited twice here.

{prompts.comparison_recipe_catalog(_RCM_PACK_IDS)}

Choose only shapes that directly answer the requirement; never cite a related
prerequisite because it is close. If no recipe answers the requirement, say so
by returning `unsupported: true` with a one-line reason instead of the contract
fields, and the attribute's evidence strategy will be reconsidered rather than
answered by a shape that proves something else. {JSON_RULES}"""

RCM_EVIDENCE_SYSTEM += (
    "\n\nInstalled transaction-evidence packs. Copy one `registry` object "
    "exactly:\n"
) + json.dumps(
    [
        {"registry": pack["registry"]}
        for pack in _RCM_CANONICAL_PACK_REFERENCES
    ],
    sort_keys=True,
    separators=(",", ":"),
)

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
        "control_owner",
    }
)
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
    normalized: list[dict] = []
    failures: list[dict] = []
    for index, row in enumerate(rows or (), start=1):
        try:
            normalized.append(_normalized_rcm_row(row, index, existing_ids))
        except WorkerResponseValidationError as error:
            failures.append(
                {
                    "index": index,
                    "row": _plain_json(row) if isinstance(row, Mapping) else row,
                    "errors": list(error.errors),
                }
            )
    return normalized, failures


def _normalized_rcm_row(
    row: object,
    index: int,
    existing_ids: set[str],
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
    packs = {
        str(attribute["registry"]["pack_id"])
        for attribute in attributes
        if attribute.get("evidence_kind") == "transaction_cycle"
    }
    if len(packs) > 1:
        raise WorkerResponseValidationError(
            f"RCM row {index} mixes transaction-cycle packs"
        )
    expected_cycle = next(iter(packs), "")
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
    }


def validate_rcm_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the RCM engagement quality gate against current durable row ids."""
    rows = proposal.get("rows")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise WorkerResponseValidationError("no RCM rows were proposed")
    normalized, failures = _partition_rcm_rows(rows, request)
    if failures:
        raise WorkerResponseValidationError(
            [message for failure in failures for message in failure["errors"]]
        )
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
            if attribute.get("comparison_recipes"):
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


_CONTRACT_FIELDS = (
    "registry",
    "comparison_recipes",
)


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
            updated.append(
                {
                    **attribute,
                    **{
                        field: _plain_json(entry[field])
                        for field in _CONTRACT_FIELDS
                        if entry.get(field) is not None
                    },
                }
            )
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
        RCM_EVIDENCE_SYSTEM,
        json.dumps(
            {
                "ATTRIBUTES NEEDING AN EVIDENCE CONTRACT": pending,
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
        return _merge_evidence_contracts(rows, response)
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
    if not failures:
        # The prior draft validates as parsed: the errors were about the document
        # as a whole (an empty rows array), so there is nothing to scope to.
        return json.dumps({"rows": rows}, ensure_ascii=False)
    response = gateway.complete(
        RCM_SYSTEM + "\n\n" + RCM_EVIDENCE_SYSTEM,
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
    implementation_hash=_sha256_text(
        "".join(
            inspect.getsource(function)
            for function in (
                run_rcm_worker,
                _contracted_document,
                _rcm_document,
                _with_evidence_contracts,
                _repaired_rcm,
                _rcm_judgment_user,
                _cycle_attribute_requests,
                _merge_evidence_contracts,
                _repair_scoped_rows,
                _first_json_object,
            )
        )
    ),
    prompt_hash=_sha256_text(RCM_SYSTEM + RCM_EVIDENCE_SYSTEM),
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
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_rcm_proposal)
        + inspect.getsource(_partition_rcm_rows)
        + inspect.getsource(_normalized_rcm_row)
        + inspect.getsource(cycle_vouching.validate_control_attributes)
        + inspect.getsource(cycle_vouching._validate_control_attribute)
        + inspect.getsource(cycle_vouching._validate_required_comparisons)
    ),
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
    implementation_hash=_sha256_text(inspect.getsource(run_planning_context_worker)),
    prompt_hash=_sha256_text(PLANNING_CONTEXT_SYSTEM),
    response_schema=PLANNING_CONTEXT_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair planning-context synthesis against the supplied documents."
        ),
    ),
    implementation=run_planning_context_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_planning_context_proposal)
    ),
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
    "RCM_EVIDENCE_SYSTEM",
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
