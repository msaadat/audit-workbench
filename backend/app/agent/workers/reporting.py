"""Registered model workers for audit reporting capabilities.

The finding worker turns one exception observation and its immutable
execution result into an unconfirmed finding draft. It owns the prompt, the
bundle-to-message transformation, and the response contract; evidence linking,
support validation, and the durable write belong to the registered executor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import templates_store
from ..prompts import JSON_RULES, LANGUAGE_RULES
from ..runtime.model_gateway import ModelGateway
from .model import (
    AUDITOR_INSTRUCTION_RULE,
    AUDITOR_INSTRUCTION_SOURCE_ID,
    auditor_instruction,
    decode_json_response,
    WORKERS,
    WorkerAttempt,
    WorkerContractError,
    WorkerDefinition,
    WorkerRepairPolicy,
    WorkerRequest,
    WorkerResponseSchema,
    WorkerResponseValidationError,
)


FINDING_WORKER_ID = "reporting.finding"
FINDING_SYSTEM = f"""[agent:finding]
Draft one unconfirmed audit finding from the supplied exception observation and
immutable execution reference.

Return the finding as Markdown only, without a JSON wrapper or Markdown code
fence, in the shape of the supplied finding template:

- a `#` line carrying the finding's title, naming the audit point rather than
  the test that found it;
- a `**Severity:**` line carrying exactly one of critical, high, medium, low,
  info;
- the template's `##` sections, in that order, with no heading added, renamed,
  or dropped.

Follow the guidance comments in that template: they are instructions to you and
must not be copied into the finding. Every section must carry text. Where the
supplied evidence does not establish why the exception occurred, write
`_Root cause pending auditor follow-up._` as the whole of the root-cause section
rather than leaving it blank or asserting a cause the evidence does not support.

Write ordinary Markdown: each heading on its own line, paragraphs separated by a
blank line, and tables written a row per line. The sections are copied into the
audit report unchanged, so write final report prose: no first person, no test
ids, run ids, or run mechanics, and no commentary about drafting. Use British
spelling throughout — analyse, summarise, recognise, organisation — so the
deliverable matches the rest of the audit file. Any number you state must be a
number the supplied execution result holds.

Be specific. A finding that counts exceptions without identifying them is not
actionable:

- When the supplied item names documents, name them in the condition rather
  than writing "the supplied documentation".
- When EXCEPTION ROWS is supplied, identify the records that failed. Where the
  rows are few, set them out as a Markdown table inside the condition section,
  choosing only the columns that evidence the exception — the identifier and
  the fields the test compared — and giving each a readable heading rather than
  the raw column name. Where they are many, describe the pattern and quantify
  it, and name a small number of examples by identifier.
- EXCEPTION ROWS states rows_supplied, rows_withheld, and truncated. When rows
  were withheld, say the table shows the first rows_supplied of
  exception_count; never present a truncated table as the full population.
- When semantic_valid is false the rows do not establish the exception. Report
  what the result does and does not support, and recommend validating and
  rerunning the check.

When SIBLING OBSERVATIONS is supplied, the finding is a consolidated one: the
observation and its siblings are instances of one issue, and CONSOLIDATION
BRIEF states the relation the auditor accepted, the title they chose, and the
root-cause hypothesis they were shown. Then:

- The condition section must set out every instance, each led by the title of
  the test that found it (the control stage), stating what that test found and
  naming its records as above. Never fold two instances into one sentence that
  loses which control each record failed.
- The root-cause section must state the one shared cause. Treat the brief's
  hypothesis as a hypothesis: keep it where the evidence supports it, and
  otherwise defer the cause as above.
- Use the brief's title unless the evidence contradicts it.

Do not create or alter RCM, planned-test, execution, or evidence references. Do
not claim auditor confirmation. {LANGUAGE_RULES}""" + f"\n\n{AUDITOR_INSTRUCTION_RULE}"

FINDING_OBSERVATION_SOURCE_ID = "observation"
FINDING_EXECUTION_SOURCE_ID = "execution_result"
FINDING_TEMPLATE_SOURCE_ID = "finding_template"
FINDING_TEST_SOURCE_ID = "test"
FINDING_EXCEPTION_ROWS_SOURCE_ID = "exception_rows"
FINDING_SIBLING_OBSERVATIONS_SOURCE_ID = "sibling_observations"
FINDING_SIBLING_EXECUTION_SOURCE_ID = "sibling_execution_results"
FINDING_SIBLING_EXCEPTION_ROWS_SOURCE_ID = "sibling_exception_rows"
FINDING_CONSOLIDATION_BRIEF_SOURCE_ID = "consolidation_brief"
_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
# The root-cause section is the one a draft may leave open, and only by saying
# so with the deferral note below, which is what sets ``cause_pending``.
_CAUSE_SECTION_KEYS = frozenset({"cause", "root cause"})

_FENCED_MARKDOWN = re.compile(
    r"```(?:markdown|md)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE
)
#: The finding's title: the one ``#`` line, distinguished from the ``##``
#: section headings that follow it.
_TITLE_LINE = re.compile(r"^#(?!#)\s*(.+?)\s*$", re.MULTILINE)
#: The severity line, however the model emphasises it — ``**Severity:** high``,
#: ``**Severity**: high`` and ``Severity: high`` are the same statement.
_SEVERITY_LINE = re.compile(
    r"^\s*[*_]*\s*severity\s*[*_]*\s*[::]\s*(.*)$", re.IGNORECASE | re.MULTILINE
)
_NARRATIVE_START = re.compile(r"^##\s+", re.MULTILINE)
#: The one accepted stand-in for a cause the evidence does not establish.
#: Matched against the whole section body so a cause that merely mentions
#: follow-up is not read as a deferral.
_CAUSE_DEFERRAL = re.compile(
    r"root cause (?:is )?pending auditor follow-?up\.?", re.IGNORECASE
)


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
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


def _context_without_sources(
    request: WorkerRequest,
    *source_ids: str,
) -> dict[str, Any]:
    """Serialize context without items already promoted in the model prompt."""
    context = request.context.to_dict()
    excluded = set(source_ids)
    context["items"] = [
        item for item in context["items"] if item.get("source_id") not in excluded
    ]
    return context


def _optional_item(request: WorkerRequest, source_id: str) -> object | None:
    """One item from a declared-but-optional source, or None when it is absent.

    A Document Test has no tabular exception population, so the exception-row
    source resolves to nothing for those units. That is a normal shape, not a
    contract violation.
    """
    matches = [
        item.content for item in request.context.items if item.source_id == source_id
    ]
    if len(matches) > 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply at most one item."
        )
    return matches[0] if matches else None


def _plain_note(body: str) -> str:
    """One section body reduced to its words, emphasis and wrapping removed."""
    return " ".join(str(body or "").replace("*", "").replace("_", "").split())


def _cause_is_deferred(narrative: str) -> bool:
    """Whether the root-cause section carries the deferral note and nothing else."""
    bodies = templates_store.section_bodies(narrative)
    return any(
        _CAUSE_DEFERRAL.fullmatch(_plain_note(bodies.get(key) or ""))
        for key in _CAUSE_SECTION_KEYS
    )


def _finding_from_json(value: str) -> Mapping[str, Any] | None:
    """The finding from a response that arrived as JSON despite the instruction.

    Kept as tolerance, not as the contract: a model that falls back on its JSON
    habit is repaired against the same rules rather than discarded, and
    ``strict=False`` accepts the unescaped newline such a response carries
    inside the narrative.
    """
    if not value.startswith("{"):
        return None
    try:
        payload = json.loads(value, strict=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    finding = payload.get("finding")
    if not isinstance(finding, Mapping) or "narrative" not in finding:
        return None
    narrative = templates_store.strip_guidance(
        str(finding.get("narrative") or "")
    ).strip()
    return {
        "title": str(finding.get("title") or "").strip(),
        "severity": str(finding.get("severity") or "").strip().casefold(),
        "narrative": narrative,
        "cause_pending": bool(finding.get("cause_pending"))
        or _cause_is_deferred(narrative),
    }


def _finding_response_schema(response: str) -> Mapping[str, Any]:
    """Read one finding from the Markdown the worker asked for.

    The narrative is multi-line Markdown — a heading per line, a table row per
    line — so it is carried as the response body rather than as a string field
    inside JSON. A model that will not emit a newline inside a JSON string
    delivers every section flattened onto one line, which parses as a single
    heading with an empty body and fails every section check at once; that is
    what cost a whole run of eight drafts, including complete ones. Markdown has
    no such failure mode, and it is how the planning memorandum has always been
    returned by the same models.

    The title and severity lines are read off the draft and become fields; the
    narrative is what follows the first ``##`` heading, so neither reaches the
    prose that is copied into the report.
    """
    value = str(response or "").strip()
    fenced = _FENCED_MARKDOWN.fullmatch(value)
    if fenced:
        value = fenced.group(1).strip()
    wrapped = _finding_from_json(value)
    if wrapped is not None:
        return {"finding": dict(wrapped)}
    title = _TITLE_LINE.search(value)
    severity = _SEVERITY_LINE.search(value)
    start = _NARRATIVE_START.search(value)
    narrative = (
        templates_store.strip_guidance(value[start.start():]).strip()
        if start
        else ""
    )
    return {
        "finding": {
            "title": title.group(1).strip() if title else "",
            "severity": _plain_note(severity.group(1) if severity else "")
            .rstrip(".")
            .strip()
            .casefold(),
            "narrative": narrative,
            "cause_pending": _cause_is_deferred(narrative),
        }
    }


def validate_finding_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the finding contract; evidence linkage stays with the executor.

    The narrative's shape is the supplied template's, not a list held here, so a
    firm that renames a section moves the repair loop with it. The deterministic
    gate in ``findings.support_issues`` applies the same rule at commit time;
    checking it here is what lets the worker repair a thin draft before one is
    written.
    """
    value = proposal.get("finding")
    if not isinstance(value, Mapping):
        raise WorkerResponseValidationError("finding must be an object")
    # Reading the observation proves the draft was grounded in a supplied one.
    _resolved_item(request, FINDING_OBSERVATION_SOURCE_ID)
    template = str(_resolved_item(request, FINDING_TEMPLATE_SOURCE_ID) or "")
    finding = _plain_json(value)
    errors: list[str] = []
    title = str(finding.get("title") or "").strip()
    if not title or "{{" in title:
        errors.append(
            "the finding needs a title on its own `#` line, naming the audit "
            "point rather than the test"
        )
    if finding.get("severity") not in _FINDING_SEVERITIES:
        errors.append(
            "the finding needs a `**Severity:**` line carrying exactly one of "
            + ", ".join(sorted(_FINDING_SEVERITIES))
        )
    narrative = str(finding.get("narrative") or "")
    bodies = templates_store.section_bodies(narrative)
    for heading in templates_store.sections(template):
        key = templates_store.section_key(heading)
        if bodies.get(key):
            continue
        # A heading that never arrived and one that arrived empty are different
        # mistakes, and saying "is empty" for both is what kept a model
        # re-emitting a narrative it had in fact written — flattened onto one
        # line, so every heading was absent rather than blank.
        if key not in bodies:
            errors.append(
                f"the narrative is missing the `## {heading}` heading; every "
                "template section must appear, each on its own line"
            )
        elif key in _CAUSE_SECTION_KEYS:
            errors.append(
                f"narrative section '{heading}' is empty; state the cause, or "
                "write `_Root cause pending auditor follow-up._` as the whole "
                "of it where the evidence does not establish one"
            )
        else:
            errors.append(
                f"narrative section '{heading}' is empty; every template "
                "section needs text"
            )
    # A consolidated draft must set out every instance. Each sibling's test is
    # the control stage its instance is led by, so the narrative has to name
    # every one of them — the lead's own test included — or an instance has
    # been folded away and the report loses which control the record failed.
    siblings = [
        item.content
        for item in request.context.items
        if item.source_id == FINDING_SIBLING_OBSERVATIONS_SOURCE_ID
    ]
    if siblings:
        lowered = narrative.casefold()
        lead_test = _optional_item(request, FINDING_TEST_SOURCE_ID)
        stages = [
            str((entry.get("test") or {}).get("title") or "")
            for entry in siblings
            if isinstance(entry, Mapping)
        ]
        if isinstance(lead_test, Mapping):
            stages.append(str(lead_test.get("title") or ""))
        for stage in stages:
            if stage and stage.casefold() not in lowered:
                errors.append(
                    "the consolidated finding must set out every instance in the "
                    f"condition section, each led by its test title; '{stage}' is not named"
                )
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"finding": {**finding, "title": title, "narrative": narrative}}


def _sibling_material(request: WorkerRequest) -> dict[str, Any]:
    """The consolidated lead's siblings, keyed the way the prompt names them."""
    observations = [
        item.content
        for item in request.context.items
        if item.source_id == FINDING_SIBLING_OBSERVATIONS_SOURCE_ID
    ]
    if not observations:
        return {}
    return {
        "SIBLING OBSERVATIONS": observations,
        "SIBLING EXECUTION RESULTS": [
            item.content
            for item in request.context.items
            if item.source_id == FINDING_SIBLING_EXECUTION_SOURCE_ID
        ],
        "SIBLING EXCEPTION ROWS": [
            item.content
            for item in request.context.items
            if item.source_id == FINDING_SIBLING_EXCEPTION_ROWS_SOURCE_ID
        ],
        "CONSOLIDATION BRIEF": _optional_item(
            request, FINDING_CONSOLIDATION_BRIEF_SOURCE_ID
        ),
    }


def run_finding_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    instruction = auditor_instruction(request)
    user = json.dumps(
        {
            # Omitted rather than sent empty: a key present and blank invites a
            # model to invent what should have been in it.
            **({"auditor_instruction": instruction} if instruction else {}),
            "OBSERVATION": _resolved_item(request, FINDING_OBSERVATION_SOURCE_ID),
            "IMMUTABLE EXECUTION RESULT": _resolved_item(
                request, FINDING_EXECUTION_SOURCE_ID
            ),
            "FINDING TEMPLATE": _resolved_item(request, FINDING_TEMPLATE_SOURCE_ID),
            "EXCEPTION ROWS": _optional_item(
                request, FINDING_EXCEPTION_ROWS_SOURCE_ID
            ),
            # Present only for a consolidated lead's redraft; omitted rather
            # than sent empty on the ordinary single-observation draft.
            **_sibling_material(request),
            "RESOLVED CONTEXT": _context_without_sources(
                request,
                AUDITOR_INSTRUCTION_SOURCE_ID,
                FINDING_SIBLING_OBSERVATIONS_SOURCE_ID,
                FINDING_SIBLING_EXECUTION_SOURCE_ID,
                FINDING_SIBLING_EXCEPTION_ROWS_SOURCE_ID,
                FINDING_CONSOLIDATION_BRIEF_SOURCE_ID,
            ),
            "REQUIRED OUTPUT": (
                "Markdown only: a `#` title line, a `**Severity:**` line, then "
                "the narrative sections below as `##` headings, each on its own "
                "line."
            ),
            "REQUIRED NARRATIVE SECTIONS": templates_store.sections(
                str(_resolved_item(request, FINDING_TEMPLATE_SOURCE_ID) or "")
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return the whole finding again as Markdown, corrected."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "finding_draft",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return str(
        gateway.complete(FINDING_SYSTEM, user, activity, attempt=attempt.number)
        or ""
    )


FINDING_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="reporting.finding.response",
    schema_hash=_sha256_text("finding-response:template-shaped-markdown"),
    validator=_finding_response_schema,
)
FINDING_WORKER = WorkerDefinition(
    worker_id=FINDING_WORKER_ID,
    prompt_hash=_sha256_text(FINDING_SYSTEM),
    response_schema=FINDING_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair finding contract violations against the supplied observation."
        ),
    ),
    implementation=run_finding_worker,
    # The finding's contract is Markdown, so the provider must not be asked to
    # constrain the response to a JSON object. Leaving this on told the model to
    # emit Markdown and forbade it in the same breath: it answered with the
    # finding filed under a key of its own choosing — `{"agent:finding": {...}}`,
    # the stage tag — and every template section was then missing. That is the
    # same failure the APM worker carries this flag for.
    json_response=False,
    semantic_validator=validate_finding_proposal,
)

WORKERS.register(FINDING_WORKER)


# --------------------------------------------------------------------------- #
# reporting.finding_consolidation worker
# --------------------------------------------------------------------------- #
CONSOLIDATION_WORKER_ID = "reporting.finding_consolidation"
CONSOLIDATION_DRAFTS_SOURCE_ID = "draft_findings"
CONSOLIDATION_KEYS_SOURCE_ID = "finding_exception_keys"
CONSOLIDATION_OVERLAPS_SOURCE_ID = "finding_overlaps"
CONSOLIDATION_RELATIONS = ("same_condition", "shared_cause")
CONSOLIDATION_BASES = ("entity", "process")
#: The Jaccard an entity-backed pair must reach before two drafts may be
#: called the same condition. Below it they share records but each still
#: says something the other does not.
CONSOLIDATION_SAME_CONDITION_JACCARD = 0.5

CONSOLIDATION_SYSTEM = f"""[agent:finding_consolidation]
Decide which draft audit findings report one issue.

You are shown every draft finding in the engagement — its title, severity,
the process and control it sits under, and the test that produced it — the
identifiers of the records each one flagged, and an OVERLAP TABLE computed
locally: every pair of findings that flagged the same records (with the count,
the Jaccard, and the shared identifiers), and every pair that sits in the same
process without sharing a record.

Return an object with:
- groups: a list of {{finding_ids, lead_finding_id, relation, basis,
  proposed_title, root_cause_hypothesis, rationale}}.
  - relation is "same_condition" when the members are the same exception
    observed more than once, or "shared_cause" when they are different
    control failures with one root cause.
  - basis is "entity" when every pair in the group appears in the overlap
    table with shared records, or "process" when the group rests only on
    the members sharing a process.
  - lead_finding_id is the member whose draft best states the issue.
  - proposed_title names the audit point for the combined finding.
  - root_cause_hypothesis states the one cause in a sentence; write it as a
    hypothesis, since the auditor will confirm or edit it.
  - rationale says, in a sentence, why these are one finding — name the
    shared records or the shared stage.
- singletons: the ids of every finding that stands alone.

Rules:
- Every finding appears exactly once: in one group or in singletons.
- A group needs at least two members and its lead must be a member.
- A "same_condition" group must be basis "entity" and every pair in it must
  appear in the overlap table with a Jaccard of at least
  {CONSOLIDATION_SAME_CONDITION_JACCARD}.
- A "shared_cause" group may rest on a process alone, but say so with basis
  "process"; do not claim shared records the table does not show.
- Leaving every finding a singleton is a real answer. Do not group findings
  to look thorough: two findings that merely sound alike are not one finding.
- Do not rewrite any finding here. The merge and the redraft are separate,
  reviewable steps the auditor triggers.
- {AUDITOR_INSTRUCTION_RULE}
{JSON_RULES} {LANGUAGE_RULES}"""


def _consolidation_drafts(request: WorkerRequest) -> dict[str, Mapping[str, Any]]:
    drafts: dict[str, Mapping[str, Any]] = {}
    for item in request.context.items:
        if item.source_id != CONSOLIDATION_DRAFTS_SOURCE_ID:
            continue
        content = item.content
        if isinstance(content, Mapping) and content.get("id"):
            drafts[str(content["id"])] = content
    return drafts


def _consolidation_pairs(request: WorkerRequest) -> dict[frozenset, Mapping[str, Any]]:
    pairs: dict[frozenset, Mapping[str, Any]] = {}
    for item in request.context.items:
        if item.source_id != CONSOLIDATION_OVERLAPS_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            continue
        for pair in content.get("pairs") or []:
            if isinstance(pair, Mapping):
                ids = frozenset(str(value) for value in pair.get("finding_ids") or [])
                if len(ids) == 2:
                    pairs[ids] = pair
    return pairs


def validate_consolidation_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Hold the proposal to the drafts it was shown and the overlap table.

    A group the table does not support is refused: a ``same_condition`` claim
    needs every pair of its members to share records at the Jaccard bar, and
    a ``shared_cause`` claim needs every pair either in the table or in one
    process. Every supplied draft must be placed exactly once, so a finding
    the model forgot cannot vanish from the review.
    """
    drafts = _consolidation_drafts(request)
    if not drafts:
        raise WorkerContractError(
            f"Context source '{CONSOLIDATION_DRAFTS_SOURCE_ID}' supplied no drafts."
        )
    pairs = _consolidation_pairs(request)
    groups = list(proposal.get("groups") or [])
    singletons = [str(value) for value in proposal.get("singletons") or []]
    placed: dict[str, str] = {}
    normalized_groups: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            raise WorkerResponseValidationError(f"groups[{index}] must be an object")
        finding_ids = [str(value) for value in group.get("finding_ids") or []]
        if len(finding_ids) < 2:
            raise WorkerResponseValidationError(
                f"groups[{index}] needs at least two finding_ids"
            )
        unknown = [value for value in finding_ids if value not in drafts]
        if unknown:
            raise WorkerResponseValidationError(
                f"groups[{index}] names '{unknown[0]}', which is not a supplied draft"
            )
        for value in finding_ids:
            if value in placed:
                raise WorkerResponseValidationError(
                    f"finding '{value}' appears more than once (groups[{index}] and {placed[value]})"
                )
            placed[value] = f"groups[{index}]"
        lead = str(group.get("lead_finding_id") or "")
        if lead not in finding_ids:
            raise WorkerResponseValidationError(
                f"groups[{index}] lead_finding_id must be one of its finding_ids"
            )
        relation = str(group.get("relation") or "").strip().casefold()
        if relation not in CONSOLIDATION_RELATIONS:
            raise WorkerResponseValidationError(
                f"groups[{index}] relation must be one of {', '.join(CONSOLIDATION_RELATIONS)}"
            )
        basis = str(group.get("basis") or "entity").strip().casefold()
        if basis not in CONSOLIDATION_BASES:
            raise WorkerResponseValidationError(
                f"groups[{index}] basis must be one of {', '.join(CONSOLIDATION_BASES)}"
            )
        if not str(group.get("rationale") or "").strip():
            raise WorkerResponseValidationError(f"groups[{index}] rationale is empty")
        shared: dict[str, set[str]] = {}
        for position, left in enumerate(finding_ids):
            for right in finding_ids[position + 1:]:
                pair = pairs.get(frozenset((left, right)))
                if relation == "same_condition":
                    if basis != "entity":
                        raise WorkerResponseValidationError(
                            f"groups[{index}] is same_condition and must be basis entity"
                        )
                    if (
                        pair is None
                        or pair.get("basis") != "entity"
                        or float(pair.get("jaccard") or 0) < CONSOLIDATION_SAME_CONDITION_JACCARD
                    ):
                        raise WorkerResponseValidationError(
                            f"groups[{index}] claims {left} and {right} report the same "
                            "condition, but the overlap table does not show them sharing "
                            f"records at a Jaccard of {CONSOLIDATION_SAME_CONDITION_JACCARD}"
                        )
                elif pair is None:
                    raise WorkerResponseValidationError(
                        f"groups[{index}] claims {left} and {right} share a cause, but they "
                        "neither share records nor sit in one process"
                    )
                elif basis == "entity" and pair.get("basis") != "entity":
                    raise WorkerResponseValidationError(
                        f"groups[{index}] is basis entity but {left} and {right} share no records"
                    )
                if pair is not None and pair.get("basis") == "entity":
                    shared.setdefault(str(pair.get("key") or ""), set()).update(
                        str(value) for value in pair.get("shared_ids") or []
                    )
        normalized_groups.append(
            {
                "finding_ids": finding_ids,
                "lead_finding_id": lead,
                "relation": relation,
                "basis": basis,
                "proposed_title": str(group.get("proposed_title") or "").strip(),
                "root_cause_hypothesis": str(group.get("root_cause_hypothesis") or "").strip(),
                "rationale": str(group.get("rationale") or "").strip(),
                "shared_entities": {key: sorted(values) for key, values in shared.items() if key},
            }
        )
    for value in singletons:
        if value not in drafts:
            raise WorkerResponseValidationError(
                f"singletons names '{value}', which is not a supplied draft"
            )
        if value in placed:
            raise WorkerResponseValidationError(
                f"finding '{value}' is both grouped and a singleton"
            )
        placed[value] = "singletons"
    missing = sorted(set(drafts) - set(placed))
    if missing:
        raise WorkerResponseValidationError(
            "every supplied draft must be placed in a group or in singletons; "
            f"missing {', '.join(missing)}"
        )
    return {"groups": normalized_groups, "singletons": list(dict.fromkeys(singletons))}


def run_consolidation_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    instruction = auditor_instruction(request)
    keys = [
        item.content
        for item in request.context.items
        if item.source_id == CONSOLIDATION_KEYS_SOURCE_ID
    ]
    overlaps = _optional_item(request, CONSOLIDATION_OVERLAPS_SOURCE_ID)
    user = json.dumps(
        {
            **({"auditor_instruction": instruction} if instruction else {}),
            "DRAFT FINDINGS": list(_consolidation_drafts(request).values()),
            "FLAGGED IDENTIFIERS": keys,
            "OVERLAP TABLE": overlaps,
            "REQUIRED OUTPUT": {
                "groups": [
                    {
                        "finding_ids": ["F-…", "F-…"],
                        "lead_finding_id": "F-…",
                        "relation": "same_condition | shared_cause",
                        "basis": "entity | process",
                        "proposed_title": "…",
                        "root_cause_hypothesis": "…",
                        "rationale": "…",
                    }
                ],
                "singletons": ["F-…"],
            },
        },
        indent=1,
        ensure_ascii=False,
        default=_plain_json,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Return the whole object again, corrected."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "finding_consolidation",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return str(
        gateway.complete(CONSOLIDATION_SYSTEM, user, activity, attempt=attempt.number)
        or ""
    )


CONSOLIDATION_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="reporting.finding_consolidation.response",
    schema_hash=_sha256_text("finding-consolidation:groups-singletons"),
    validator=decode_json_response,
)
CONSOLIDATION_WORKER = WorkerDefinition(
    worker_id=CONSOLIDATION_WORKER_ID,
    prompt_hash=_sha256_text(CONSOLIDATION_SYSTEM),
    response_schema=CONSOLIDATION_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair groups the overlap table does not support and unplaced drafts."
        ),
    ),
    implementation=run_consolidation_worker,
    semantic_validator=validate_consolidation_proposal,
)

WORKERS.register(CONSOLIDATION_WORKER)


__all__ = [
    "CONSOLIDATION_RESPONSE_SCHEMA",
    "CONSOLIDATION_SAME_CONDITION_JACCARD",
    "CONSOLIDATION_SYSTEM",
    "CONSOLIDATION_WORKER",
    "CONSOLIDATION_WORKER_ID",
    "run_consolidation_worker",
    "validate_consolidation_proposal",
    "FINDING_EXCEPTION_ROWS_SOURCE_ID",
    "FINDING_RESPONSE_SCHEMA",
    "FINDING_SYSTEM",
    "FINDING_TEMPLATE_SOURCE_ID",
    "FINDING_WORKER",
    "FINDING_WORKER_ID",
    "run_finding_worker",
    "validate_finding_proposal",
]
