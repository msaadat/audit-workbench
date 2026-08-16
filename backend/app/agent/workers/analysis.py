"""Registered model worker for the exploratory data-analysis workflow.

The analysis-definition worker turns one target frame's declared metadata
context — schema, bounded statistical profile, value-free aggregates, related
frame schemas, and the deterministic relationship evidence — into rerunnable
saved-analysis definitions. It owns its prompt, its bundle-to-message
transformation, and the part of the contract the supplied context can decide:
response shape, kind enum, registry-ID membership, exact column spelling against
the supplied schema, the static Polars sandbox contract, and de-duplication
against the analyses it was shown.

The authoritative execution stays with the registered executor, which owns the
workspace frames.  This worker nevertheless validates the complete library-test
contract it supplied to the model: parameter names, required fields, select
options, number values, and schema-visible column types. Importing the
``analytics`` registry payload and the static ``sandbox`` validator here is
deliberate and follows the fieldwork precedent: both are application catalogs
and static contracts rather than engagement content, so they belong to the
response contract the worker enforces, not to the declared engagement context.

The worker never sees a table row: the declared context denies
``allow_table_rows`` and the resolver rejects the representation structurally.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from itertools import permutations
from typing import Any

from ... import analytics, sandbox
# The memo's embed grammar is shared with the context adapter that hands the
# memo to the APM. It lives outside ``agent/`` because a worker may not import
# the context package and vice versa; the module has no dependencies of its own.
from ...analysis_memo import EMBED_FENCE, EMBED_KINDS, parse_embeds
from ...field_names import subject_tokens
from ...text import counted, verb
from ..prompts import JSON_RULES
from ..runtime.model_gateway import ModelGateway
from .model import (
    MAX_REPAIR_ERRORS,
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


ANALYSIS_DEFINITION_WORKER_ID = "analysis.definitions"
JOIN_UTILITY_WORKER_ID = "analysis.join_utility"
JOIN_UTILITY_CANDIDATES_SOURCE_ID = "join_candidates"
JOIN_UTILITY_SUBMISSION_TOOL = "submit_join_utility_decisions"
JOIN_UTILITY_SYSTEM = """[agent:join_utility]
You are selecting which technically safe table relationships are worth
materializing for audit analysis. You receive only schemas and aggregate join
diagnostics, never table rows. For every supplied candidate, return exactly one
decision: retain only when you can state a concrete, falsifiable audit test that
needs columns from both sides; otherwise reject. Do not retain a relationship
merely because keys match, a date can be compared, or a join is technically
safe. Do not invent controls, values, or facts not present in the supplied
context. For every retained decision, name schema-visible columns from both
sides of the relationship.

Exactly one candidate may be retained for any pair of tables, in either
direction. Several candidates often connect the same two tables — alternate
keys that express one relationship by different routes, and role keys that
reach the same dimension as requester, verifier, or approver. Only one of them
can be materialized, and for role keys that choice decides what every later
test measures, so retain the single route whose audit test matters most.

Do not manage cross-references between decisions. The application records a
same-pair alternate deterministically after you choose the one retained route.
Retaining fewer relationships is not itself a goal: retain every relationship
whose own pair supports a test, and reject only what that pair itself does not
support or when you retained a better alternate for that same pair. A
relationship between a different pair never makes this pair redundant.

For every retained candidate also list, in ``requires``, every table your
stated test reads. A pair of tables is what the relationship connects, not
necessarily what the test needs: comparing a transaction against the approval
limit for its approver's job title reads the transaction table, the staff
table, and the approval matrix, so all three belong in ``requires``. Name the
tables the test actually reads and no others — a table listed here that the
test does not use narrows where the test can run for no reason, and one left
out means the test is prepared against data that cannot answer it. Submit the
required function tool exactly once; do not return Markdown or plain text.
"""
ANALYSIS_DEFINITION_SYSTEM = f"""[agent:analysis_definitions]
Propose rerunnable data analyses for exactly one supplied target frame. Submit
them through the required function tool; its JSON Schema is the authoritative
output contract, including how many analyses this frame takes and which tests
its size supports. For analytics, use only a test ID and parameters permitted
by that schema, with exact supplied column names.
For python, spec contains code: safe Polars that reads the supplied frames
through the `tables` mapping or their bare table variables and assigns the
result DataFrame to `result`; imports and any file or network access are
forbidden. Use `series.len()` (not `series.height`), use
`.dt.total_days()` for duration values (not `.dt.days()`), and specify a join
suffix or select columns before joining frames with overlapping column names.
Every generated Python analysis is run locally against its supplied workspace
tables before it can be saved. Set outcome_policy to ``exception_rows`` only
when every returned row is a potential exception; otherwise use
``informational``. Every analysis must be relevant to the supplied schema, profile,
aggregates, and relationship evidence, and must not repeat an analysis already
supplied in current_analyses. Those cover the target frame's whole join family,
so a check on columns that all come from one table is already covered wherever
it is saved: on a joined frame, propose analyses that use columns from more than
one of the joined tables, since that is what the join makes possible. You are
never shown table rows and must not invent values, counts, or relationships.

Where the request carries MEASURED ON THIS FRAME, those are procedures that have
already been run against this frame's own data. Each is a complete, valid spec
and the counts beside it are what it produced — a reconciliation that left rows
unmatched, an ordering the population breaks, a key that repeats, an identifier
built unlike the rest. They are not suggestions to evaluate: the arithmetic is
done. Your judgment is what they *mean*, not whether they are worth measuring.

A nomination that flagged rows is a finding this frame already has, and the
array is sized to hold them, so **take it** — copy its `test` and `params`
exactly and say in `note` what the flagged condition would mean, in terms of the
business events the columns record. Changing a spec measures something else and
the counts stop applying to it. Leave one only where you can say why the
condition it flags could not indicate a control failing, and spend whatever
slots remain on what the measurements do not reach. A nomination with no flagged
rows is a relationship the population holds to; it is worth a line in a memo and
is not a procedure worth a slot.

VALUE DOMAINS lists the complete set of values each low-cardinality column
holds. Use those exact spellings when a procedure has to name one — a status, a
designation — and never invent a value that is not listed for that column.

Where the request carries TESTS THIS FRAME WAS MATERIALIZED TO SUPPORT, those
are the reasons this frame exists. Each names a hypothesis that was found worth
testing, and the columns that state it, before the frame was built. Write those
tests first, as analyses; they are the frame's purpose, and a response that
omits them has answered a question nobody asked. Propose further analyses only
where the frame supports something the listed hypotheses do not already cover.

A hypothesis is stated in terms of the two tables it was diagnosed from, before
either was joined — "invoice X has a matching GRN in po_data" describes the
join condition that already built this frame. The frame's rows already reflect
it: for every row here, the invoice's columns and the matching PO's columns
sit side by side, aligned by that same key, with no missing counterpart. Do not
re-establish the match — no join, self-join, or key lookup against the same
table again — to write the test; simply read and compare the already-aligned
columns directly on this frame. The open question a hypothesis leaves for you
is never "does X match Y" (that is why this frame exists); it is what the
hypothesis says should also hold, row by row, once they do.

The array's maximum is a ceiling, not a quota. Propose only the analyses this
frame genuinely supports, and return an empty array where it supports none. A
frame that joins two tables with nothing meaningful to compare across them is a
real answer; padding it with whatever column pairing the schema permits is not.

Two columns being of the same type does not make them comparable. Use
`column_origins` to see which table each column came from, and propose a
comparison only where the two columns describe the same thing about the same
entity or two steps in one chain of events. A purchase order's date and a
vendor's supplier-approval date are both dates on the joined frame, and
ordering one against the other measures nothing — the vendor was approved long
before any order was raised, and always will have been. An amount and a limit
are both numbers, and testing them for equality measures nothing, because a
cost is never expected to equal the ceiling it must stay under; test the
threshold instead. Before proposing, ask what result would mean the data is
sound: if that is "every row is flagged" or "no row can be flagged", the
comparison is wrong and the analysis should not be proposed.

`note` is required and is where you say that: one sentence giving the
relationship you expect to hold and why, in terms of the business events the
columns record — "a GRN records receipt, so it cannot predate the order it
receives" rather than "checks the dates". If you cannot write that sentence for
a proposal, you have not established that the two columns belong together, and
the analysis should not be proposed.
{JSON_RULES}"""

TARGET_SCHEMA_SOURCE_ID = "target_schema"
JOIN_HYPOTHESIS_SOURCE_ID = "join_hypotheses"
ANALYTICS_REGISTRY_SOURCE_ID = "analytics_registry"
LOOKUP_CANDIDATES_SOURCE_ID = "lookup_candidates"
PROBE_FINDINGS_SOURCE_ID = "probe_findings"
VALUE_DOMAINS_SOURCE_ID = "value_domains"
CURRENT_ANALYSES_SOURCE_ID = "current_analyses"
ANALYSIS_SUBMISSION_TOOL = "submit_analysis_definitions"

MAX_PROPOSED_ANALYSES = 4
ANALYSIS_KINDS = ("analytics", "python")

# Tests whose answer is a property of a population rather than of a row. An
# IQR fence over four values, a "rare" category among four, or a monthly trend
# across two months is arithmetic without a finding in it — and it still spends
# one of the frame's proposal slots and one execution. Below the threshold
# these are dropped from the tool schema, so they cannot be proposed at all
# rather than being rejected after the model has already spent a turn on them.
# Integrity checks — completeness, duplicates, sequence gaps, date lags, sign
# scans — are per-row and stay available at any size.
POPULATION_TEST_IDS = frozenset(
    {
        "outliers",
        "stratify",
        "period_compare",
        "threshold_check",
        "rare_values",
        "weekend_activity",
        "sampling",
        # A dominant format is a property of the population: across four values
        # the majority shape is whatever two of them happen to share, and every
        # remaining value is an anomaly by construction.
        "format_anomaly",
    }
)
MIN_ROWS_FOR_POPULATION_TESTS = 30

# How many analyses one frame may be asked for. A frame supports a certain
# amount of distinct, useful work and no more: a four-row lookup table has a
# completeness check and a duplicate check in it, and asking for four means the
# last two are padding that later has to be deduplicated, executed, and read.
SMALL_FRAME_ROWS = 30
MODEST_FRAME_ROWS = 100
SMALL_FRAME_ANALYSES = 2
MODEST_FRAME_ANALYSES = 3

# The ceiling once a frame's findings have been measured rather than guessed.
# The size budget above exists to stop padding, and that reasoning inverts when
# the sweep arrives with runnable specs and the counts they produced: a
# procedure that already flagged rows is not padding, it is a finding, and a
# frame carrying ten of them cannot state them in four slots. Observed directly
# — a frame with ten measured findings saved four, and the invoices dated before
# their own purchase order were among the six that did not fit.
#
# Still bounded, because one turn has to write every analysis it returns and a
# frame that measures fifteen things is better served by the sweep's own ranking
# than by a completion long enough to hold all of them.
MAX_MEASURED_ANALYSES = 8


def proposal_budget(rows: int | None, measured: int = 0) -> int:
    """How many analyses to ask of a frame.

    From the frame's size, or from the number of measured findings supplied for
    it where that is larger — a slot spent restating something the sweep already
    computed is not speculative work, and refusing it drops a finding that has
    already been established.
    """
    return max(_size_budget(rows), min(int(measured or 0), MAX_MEASURED_ANALYSES))


def _size_budget(rows: int | None) -> int:
    if rows is None:
        return MAX_PROPOSED_ANALYSES
    if rows < SMALL_FRAME_ROWS:
        return SMALL_FRAME_ANALYSES
    if rows < MODEST_FRAME_ROWS:
        return MODEST_FRAME_ANALYSES
    return MAX_PROPOSED_ANALYSES


# Carried in the validation error when a frame has nothing of its own left to
# contribute — every proposal either repeats a computation its join family
# already holds, or reads only one of the tables the frame was built from. That
# is a settled outcome, not a contract violation, so the binder matches on this
# to settle the unit as skipped once the repair turn has had its chance.
NOTHING_NEW_TO_ANALYSE = "nothing_new_to_analyse"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _model_facing_context(
    request: WorkerRequest,
    *exclude_source_ids: str,
) -> dict[str, Any]:
    """Serialize the resolved bundle as the audit material, without its transport.

    Every resolved item carries a ``supplied_size`` block — item, character and
    token counts, media tallies — and a ``representation`` envelope. Both exist
    so the durable manifest can account for what was supplied and prove what the
    worker saw. Neither tells the model anything about the engagement: the
    counts describe the message being sent, and ``source_id`` already says what
    kind of material each item is.

    Across the validation engagement's analysis stages that envelope was around
    an eighth of every prompt, and ``supplied_size`` alone was 9% of the summary
    call. It is dropped here rather than in the bundle because the manifest must
    keep it — this is the same projection ``workers.tests`` performs by
    promoting item content, expressed for a bundle with too many source types to
    promote one at a time.
    """
    context = request.context.to_dict()
    excluded = set(exclude_source_ids)
    items = []
    for item in context["items"]:
        if item.get("source_id") in excluded:
            continue
        projected = {
            key: value
            for key, value in item.items()
            if key not in {"supplied_size", "representation"}
        }
        # The representation's ``kind`` names what the content is where a source
        # supplies more than one shape of it; its ``options`` are resolver input
        # the model never acts on.
        kind = str((item.get("representation") or {}).get("kind") or "")
        if kind:
            projected["representation"] = kind
        items.append(projected)
    context["items"] = items
    return context


def _json_object(response: str) -> dict[str, Any]:
    try:
        value = json.loads(response)
    except json.JSONDecodeError as error:
        raise WorkerResponseValidationError("Response must be a JSON object.") from error
    if not isinstance(value, dict):
        raise WorkerResponseValidationError("Response must be a JSON object.")
    return value


def _join_utility_catalog(request: WorkerRequest) -> dict[str, Any]:
    catalog = _plain_json(_resolved_item(request, JOIN_UTILITY_CANDIDATES_SOURCE_ID))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("candidates"), list):
        raise WorkerContractError("Join-utility context must contain a candidate catalog.")
    return catalog


def _join_utility_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_object(response)
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise WorkerResponseValidationError("decisions must be an array.")
    return {"decisions": decisions}


def _join_utility_submission_tool(request: WorkerRequest) -> dict[str, Any]:
    catalog = _join_utility_catalog(request)
    refs = sorted(str(item["ref"]) for item in catalog["candidates"])
    columns = sorted(
        f"{table}.{column}"
        for table, values in (catalog.get("table_columns") or {}).items()
        if isinstance(values, list)
        for column in values
    )
    tables = sorted(str(name) for name in (catalog.get("table_columns") or {}))
    def branch(decision: str) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "ref": {"type": "string", "enum": refs},
            "decision": {"type": "string", "enum": [decision]},
            "rationale": {"type": "string", "minLength": 1},
        }
        required = ["ref", "decision", "rationale"]
        if decision == "retain":
            properties.update({
                "hypothesis": {"type": "string", "minLength": 1},
                "columns": {"type": "array", "items": {"type": "string", "enum": columns}, "minItems": 2},
                "requires": {
                    "type": "array",
                    "items": {"type": "string", "enum": tables},
                    "minItems": 2,
                    "description": (
                        "Every table the stated test reads, including both sides "
                        "of this relationship and any further table the test "
                        "compares against."
                    ),
                },
            })
            required.extend(("hypothesis", "columns", "requires"))
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    return {"type": "function", "function": {
        "name": JOIN_UTILITY_SUBMISSION_TOOL,
        "description": (
            "Submit a retain or reject decision for every join candidate. "
            "At most one candidate may be retained for any pair of tables, in "
            "either direction: several candidates commonly connect the same "
            "two tables by alternate keys or by different roles, and only one "
            "of them can be materialized."
        ),
        "parameters": {"type": "object", "properties": {
            "decisions": {"type": "array", "items": {"oneOf": [branch("retain"), branch("reject")]}, "minItems": len(refs), "maxItems": len(refs)},
        }, "required": ["decisions"], "additionalProperties": False},
    }}


def _join_utility_submission_response(message: object) -> str:
    if not isinstance(message, Mapping):
        return str(message or "")
    matches = [
        item for item in message.get("tool_calls") or []
        if isinstance(item, Mapping) and isinstance(item.get("function"), Mapping)
        and item["function"].get("name") == JOIN_UTILITY_SUBMISSION_TOOL
    ]
    if len(matches) == 1:
        arguments = matches[0]["function"].get("arguments")
        return arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return str(message.get("content") or "")


def validate_join_utility_proposal(
    proposal: Mapping[str, Any], request: WorkerRequest
) -> Mapping[str, Any]:
    catalog = _join_utility_catalog(request)
    candidates = {
        str(item.get("ref")): item
        for item in catalog["candidates"]
        if isinstance(item, Mapping) and str(item.get("ref") or "").strip()
    }
    table_columns = {
        str(table): {str(column) for column in columns}
        for table, columns in (catalog.get("table_columns") or {}).items()
        if isinstance(columns, list)
    }
    # The registered response schema freezes its proposal before this runs, so
    # every array arrives as a tuple. Normalize rather than type-check: an
    # `isinstance(..., list)` here can never hold, and would fail every
    # response the model could possibly send.
    raw = proposal.get("decisions")
    if not isinstance(raw, (list, tuple)):
        raise WorkerResponseValidationError("decisions must be an array.")
    raw = list(raw)
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            errors.append(f"decisions[{index}] must be an object")
            continue
        ref = str(item.get("ref") or "").strip()
        decision = str(item.get("decision") or "").strip().lower()
        rationale = str(item.get("rationale") or "").strip()
        hypothesis = str(item.get("hypothesis") or "").strip()
        columns = [str(value) for value in item.get("columns") or []]
        if ref not in candidates:
            errors.append(f"decisions[{index}] names an unknown candidate '{ref}'")
            continue
        if ref in seen:
            errors.append(f"decisions[{index}] repeats '{ref}'")
            continue
        seen.add(ref)
        if decision not in {"retain", "reject"}:
            errors.append(f"decisions[{index}].decision must be retain or reject")
            continue
        if not rationale:
            errors.append(f"decisions[{index}].rationale is required")
        if decision == "retain" and not hypothesis:
            errors.append(f"decisions[{index}].hypothesis is required for retain")
        requires = [str(value) for value in item.get("requires") or []]
        if decision == "retain":
            candidate = candidates[ref]
            expected = {str(candidate.get("left") or ""), str(candidate.get("right") or "")}
            used = {
                table for table, separator, column in (value.partition(".") for value in columns)
                if separator and column in table_columns.get(table, set())
            }
            if not expected <= used:
                errors.append(
                    f"decisions[{index}].columns must name visible columns from both "
                    f"{candidate.get('left')} and {candidate.get('right')}"
                )
            # A test that does not read both sides of the relationship it was
            # asked about is a test for some other relationship.
            unknown = sorted(set(requires) - set(table_columns))
            if unknown:
                errors.append(
                    f"decisions[{index}].requires names unknown table(s): "
                    + ", ".join(unknown)
                )
            elif not expected <= set(requires):
                errors.append(
                    f"decisions[{index}].requires must include both "
                    f"{candidate.get('left')} and {candidate.get('right')}"
                )
        accepted.append({
            "ref": ref,
            "decision": decision,
            "rationale": rationale,
            "hypothesis": hypothesis,
            "columns": columns,
            "requires": sorted(set(requires)) if decision == "retain" else [],
            # This is computed below from the selected routes. It is deliberately
            # not model-authored: cross-referencing a decision set is mechanical
            # work that caused repeated generation failures with stricter models.
            "superseded_by": "",
        })
    # One pair of tables materializes at most one join, so a response retaining
    # two routes between them has not made the choice it was asked for. Every
    # competing ref is named together: the one repair turn has to be able to
    # settle each contested pair, not discover them one at a time.
    retained_pairs: dict[frozenset[str], list[str]] = {}
    for item in accepted:
        if item["decision"] != "retain":
            continue
        candidate = candidates[item["ref"]]
        pair = frozenset((str(candidate.get("left") or ""), str(candidate.get("right") or "")))
        retained_pairs.setdefault(pair, []).append(item["ref"])
    for pair, refs in retained_pairs.items():
        if len(refs) > 1:
            errors.append(
                f"Only one candidate may be retained for {' and '.join(sorted(pair))}; "
                f"keep the single most useful of {', '.join(sorted(refs))} and "
                "reject the others"
            )
    # A floor, and deliberately a systemic one. Two tables that share a key and
    # nothing worth testing across it are ordinary, so rejecting a pair outright
    # — even a strongly-evidenced one — stays a decision the gate is allowed to
    # make. What is not ordinary is rejecting *every* pair in an engagement while
    # several of them match on every row without multiplying any: that says this
    # data supports no cross-table test at all, and the run that said it
    # proceeded over six frames instead of twenty without anything stopping to
    # ask. The claim stays available; it may not be arrived at by accident.
    if accepted and not any(item["decision"] == "retain" for item in accepted):
        strong = {
            ref: frozenset(
                (str(candidate.get("left") or ""), str(candidate.get("right") or ""))
            )
            for ref, candidate in candidates.items()
            if str(candidate.get("strength") or "") == "strong"
        }
        if len(set(strong.values())) > 1:
            named = sorted(strong)
            errors.append(
                f"Every candidate was rejected, including {counted(len(named), 'candidate')} "
                f"across {counted(len(set(strong.values())), 'table pair')} whose keys "
                "match on every row without multiplying any: "
                + ", ".join(named[:4])
                + (f" and {len(named) - 4} more" if len(named) > 4 else "")
                + ". Retain the relationships that support an audit test and "
                "reject only the rest, or resubmit rejecting them all again if "
                "this engagement genuinely supports no cross-table test"
            )
    missing = sorted(set(candidates) - seen)
    if missing:
        errors.append("A decision is required for every candidate: " + ", ".join(missing))
    if errors:
        raise WorkerResponseValidationError(errors)
    retained_by_pair = {
        pair: refs[0]
        for pair, refs in retained_pairs.items()
        if len(refs) == 1
    }
    for item in accepted:
        if item["decision"] != "reject":
            continue
        candidate = candidates[item["ref"]]
        pair = frozenset(
            (str(candidate.get("left") or ""), str(candidate.get("right") or ""))
        )
        item["superseded_by"] = retained_by_pair.get(pair, "")
    return {"decisions": accepted}


def run_join_utility_worker(
    request: WorkerRequest, gateway: ModelGateway, attempt: WorkerAttempt
) -> str:
    catalog = _join_utility_catalog(request)
    tool = _join_utility_submission_tool(request)
    # The catalog is one of the resolved bundle items, so serializing the whole
    # bundle alongside it would send the same 10 KB twice. The catalog is named
    # once, in the shape the submission tool's enums were built from, and the
    # remaining items — the table schemas — follow it.
    schemas = [
        item.content
        for item in request.context.items
        if item.source_id != JOIN_UTILITY_CANDIDATES_SOURCE_ID
    ]
    prompt = json.dumps({
        "JOIN CANDIDATES": catalog,
        "TABLE SCHEMAS": schemas,
        "REQUIRED OUTPUT": f"Call {JOIN_UTILITY_SUBMISSION_TOOL} exactly once.",
    }, indent=1, ensure_ascii=False)
    conversation = None
    if attempt.is_repair:
        # The prior decisions are replayed, not summarized into a list of
        # complaints. Every violation this gate raises is of the form "keep the
        # single most useful of A, B and reject the others", which cannot be
        # acted on by a turn that has been shown neither A's rationale nor B's:
        # the cheapest response satisfying five such instructions at once is to
        # reject everything, and a run did exactly that — sixteen retained on the
        # first attempt, sixteen rejected on the second, no join materialized and
        # the workspace reduced from twenty frames to six.
        if attempt.previous_response is None:
            raise WorkerContractError(
                "A join-utility repair requires the previous response."
            )
        conversation = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": attempt.previous_response},
            {
                "role": "user",
                "content": (
                    "Correct only the listed violations, and keep every other "
                    "decision and rationale exactly as you already made them: "
                    + "; ".join(attempt.validation_errors)
                    + ". Where a pair retained more than one candidate, keep the "
                    "one whose stated test matters most and reject only its "
                    "alternates — a pair that supported a test before still "
                    "supports it. Resubmit the complete decision set."
                ),
            },
        ]
    activity = dict(request.activity)
    activity.setdefault("context_metrics", {
        "worker_kind": "join_utility",
        "total_characters": request.context.supplied_size.characters,
        "estimated_tokens": request.context.supplied_size.estimated_tokens,
        "selected_items": request.context.supplied_size.items,
    })
    message = gateway.complete(
        JOIN_UTILITY_SYSTEM, prompt, activity, attempt=attempt.number,
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": JOIN_UTILITY_SUBMISSION_TOOL}},
        conversation=conversation,
        return_message=True,
    )
    return _join_utility_submission_response(message)


JOIN_UTILITY_WORKER = WorkerDefinition(
    worker_id=JOIN_UTILITY_WORKER_ID,
    prompt_hash=_sha256_text(JOIN_UTILITY_SYSTEM),
    response_schema=WorkerResponseSchema(
        schema_id="analysis.join_utility.response",
        schema_hash=_sha256_text("join-utility-response"),
        validator=_join_utility_response_schema,
    ),
    repair_policy=WorkerRepairPolicy(max_repair_attempts=1, guidance_hash=_sha256_text("Repair join utility decisions.")),
    implementation=run_join_utility_worker,
    semantic_validator=validate_join_utility_proposal,
)
WORKERS.register(JOIN_UTILITY_WORKER)


def _source_items(request: WorkerRequest, source_id: str) -> list[object]:
    return [item.content for item in request.context.items if item.source_id == source_id]


def _resolved_item(request: WorkerRequest, source_id: str) -> object:
    matches = _source_items(request, source_id)
    if len(matches) != 1:
        raise WorkerContractError(
            f"Context source '{source_id}' must supply exactly one item."
        )
    return matches[0]


def _target_schema(request: WorkerRequest) -> dict[str, Any]:
    schema = _plain_json(_resolved_item(request, TARGET_SCHEMA_SOURCE_ID))
    if not isinstance(schema, dict) or not str(schema.get("table") or "").strip():
        raise WorkerContractError("The supplied target schema names no table.")
    return schema


def _resolve_provenance(
    value: object, origins: Mapping[str, str]
) -> tuple[object, set[str]]:
    """Rewrite every column name in a spec to ``origin_table.column``.

    Walks the spec structurally rather than consulting the parameter contract:
    a column is recognised by being a name the frame actually has, which holds
    for every test in the library without this needing to know any of them.
    """
    if isinstance(value, Mapping):
        resolved: dict[str, object] = {}
        scope: set[str] = set()
        for key, item in value.items():
            resolved[str(key)], found = _resolve_provenance(item, origins)
            scope |= found
        return resolved, scope
    if isinstance(value, (list, tuple)):
        items: list[object] = []
        scope = set()
        for item in value:
            resolved_item, found = _resolve_provenance(item, origins)
            items.append(resolved_item)
            scope |= found
        return items, scope
    if isinstance(value, str) and value in origins:
        return f"{origins[value]}.{value}", {origins[value]}
    return value, set()


def spec_scope(spec: Mapping[str, Any], origins: Mapping[str, str]) -> set[str]:
    """The base tables a spec's columns actually come from."""
    _, tables = _resolve_provenance(_plain_json(spec), origins)
    return tables


def python_spec_scope(spec: Mapping[str, Any], origins: Mapping[str, str]) -> set[str]:
    """Return the origins of schema columns referenced by safe Polars code.

    Python definitions are deliberately constrained to a small static sandbox.
    We do not need to execute a procedure (and therefore touch rows) merely to
    establish whether a materialized join contributes to it: any quoted value
    that is also an exact schema column name is a column reference.  This is
    conservative by design; an ambiguous literal only makes the procedure use
    *more* origins, never permits a single-sided joined-frame procedure.

    Code that quotes no column name at all returns an empty set, which says
    nothing either way — a frame-wide preview and a procedure that builds its
    column names by concatenation look identical here.  Callers read an empty
    result as unknown rather than as one-sided.
    """
    code = str(spec.get("code") or "")
    literals = re.findall(r"['\"]([^'\"]+)['\"]", code)
    return {origins[value] for value in literals if value in origins}


def analysis_semantic_id(
    kind: str,
    table: str,
    spec: Mapping[str, Any],
    origins: Mapping[str, str] | None = None,
) -> str:
    """Stable identity for one analysis definition.

    Derived from the canonical spec rather than the title, so a reworded
    proposal for the same computation deduplicates against the analysis already
    saved instead of creating a second one.

    ``origins`` maps the frame's columns to the base tables they come from. With
    it, identity is the computation itself — which columns, from which tables —
    rather than the frame it happened to be written against. That is what makes
    an invoice date lag proposed on ``invoice_data`` and the identical lag
    proposed on ``invoice_data_po_data_joined`` one analysis instead of two:
    the join adds columns, but it does not make an invoice-only test a
    different test. A spec spanning both sides of a join still resolves to its
    own identity, because its scope names both tables.

    Without ``origins`` the frame name carries identity, as before — a Python
    analysis reaches frames the spec never names, so its code is only
    meaningfully identified against the frame it was written for.
    """
    resolved_spec: object = _plain_json(spec)
    scope = str(table)
    if origins and str(kind) != "python":
        resolved_spec, tables = _resolve_provenance(resolved_spec, origins)
        if tables:
            scope = "+".join(sorted(tables))
    canonical = json.dumps(
        {"kind": str(kind), "table": scope, "spec": resolved_spec},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "analysis:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _analytics_contract(request: WorkerRequest) -> dict[str, dict[str, Any]]:
    """Return the complete test contract that was supplied to the worker."""
    registry = _plain_json(_resolved_item(request, ANALYTICS_REGISTRY_SOURCE_ID))
    supplied = {
        str(item.get("id")): dict(item)
        for item in (registry if isinstance(registry, list) else [])
        if isinstance(item, Mapping) and item.get("id")
    }
    return supplied or {
        str(item["id"]): item for item in analytics.registry_payload()
    }


def _column_names(schema: Mapping[str, Any]) -> set[str]:
    return {
        str(column.get("name"))
        for column in schema.get("columns") or []
        if str(column.get("name") or "").strip()
    }


def _column_types(schema: Mapping[str, Any]) -> dict[str, str]:
    """Map exact source columns to the type visible in the model's schema."""
    return {
        str(column.get("name")): str(column.get("type") or "")
        for column in schema.get("columns") or []
        if str(column.get("name") or "").strip()
    }


def _measured_findings(request: WorkerRequest) -> int:
    """How many supplied nominations actually flagged rows.

    A confirmed invariant buys no slot: it is worth one line in a memo and is
    not a procedure competing with the ones that found something.
    """
    total = 0
    for raw in _source_items(request, PROBE_FINDINGS_SOURCE_ID):
        item = _plain_json(raw)
        if isinstance(item, Mapping):
            try:
                total += 1 if int(item.get("flagged") or 0) else 0
            except (TypeError, ValueError):
                continue
    return total


def _target_rows(schema: Mapping[str, Any]) -> int | None:
    """The target frame's row count, when the supplied schema declares one."""
    raw = schema.get("rows")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _column_origins(schema: Mapping[str, Any]) -> dict[str, str]:
    """The base table each supplied column originates in, when declared."""
    raw = schema.get("column_origins")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(column): str(origin)
        for column, origin in raw.items()
        if str(column or "").strip() and str(origin or "").strip()
    }


def _existing_semantic_ids(request: WorkerRequest, origins: Mapping[str, str]) -> set[str]:
    """Semantic ids of every analysis the request was shown.

    These come from the target frame's whole join family, so an id here may
    belong to a sibling frame. That is the point: identity is provenance-based,
    so a sibling's saved computation and this frame's proposal of it are the
    same id, and the repeat is caught before it is written.
    """
    ids: set[str] = set()
    for raw in _source_items(request, CURRENT_ANALYSES_SOURCE_ID):
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            continue
        semantic = str(item.get("semantic_id") or "").strip()
        if semantic:
            ids.add(semantic)
            continue
        ids.add(
            analysis_semantic_id(
                str(item.get("kind") or ""),
                str(item.get("table") or ""),
                item.get("spec") or {},
                origins,
            )
        )
    return ids


def _parameter_json_schema(
    parameter: Mapping[str, Any],
    columns: set[str],
    column_types: Mapping[str, str],
) -> dict[str, Any] | None:
    """Translate one library parameter contract into provider JSON Schema."""
    kind = str(parameter.get("kind") or "")
    if kind in {"column", "columns"}:
        expected = str(parameter.get("column_kind") or "")
        eligible = sorted(
            column
            for column in columns
            if not expected or column_types.get(column) == expected
        )
        if not eligible:
            return None
        if kind == "column":
            return {"type": "string", "enum": eligible}
        return {
            "type": "array",
            "items": {"type": "string", "enum": eligible},
            "minItems": 1,
        }
    if kind == "select":
        values = [
            option.get("value")
            for option in parameter.get("options") or []
            if isinstance(option, Mapping) and "value" in option
        ]
        return {"enum": values}
    if kind == "number":
        return {"type": "number"}
    # Cross-frame kinds are not built here: ``lookup_column`` is only meaningful
    # once ``lookup_table`` is fixed, so the two are emitted together as one
    # branch per candidate table by ``_lookup_branches``. Reaching this point for
    # any other kind means the registry grew a parameter shape this contract
    # cannot constrain, and a permissive schema there would let the model write
    # free text into a field the executor will act on.
    return None


def _lookup_catalog(request: WorkerRequest) -> list[tuple[str, list[str]]]:
    """(table, key columns) the supplied context admits as reconciliation targets."""
    catalog: list[tuple[str, list[str]]] = []
    for raw in _source_items(request, LOOKUP_CANDIDATES_SOURCE_ID):
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            continue
        table = str(item.get("table") or "").strip()
        keys = [
            str(column).strip()
            for column in item.get("key_columns") or []
            if str(column or "").strip()
        ]
        if table and keys:
            catalog.append((table, keys))
    catalog.sort()
    return catalog


def _domain_catalog(request: WorkerRequest) -> list[tuple[str, list[str]]]:
    """(column, its complete value vocabulary) for the supplied domains."""
    catalog: list[tuple[str, list[str]]] = []
    for raw in _source_items(request, VALUE_DOMAINS_SOURCE_ID):
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            continue
        column = str(item.get("column") or "").strip()
        values = [
            str(value) for value in item.get("values") or [] if str(value).strip()
        ]
        if column and values:
            catalog.append((column, values))
    catalog.sort()
    return catalog


def _value_branches(
    request: WorkerRequest, base: Mapping[str, Any], required: list[str]
) -> list[dict[str, Any]]:
    """One params branch per column that has a vocabulary.

    ``values`` only means anything relative to a chosen column, and the values a
    column actually holds are the only ones worth checking against: a filter on a
    status that does not occur flags nothing and reads as a clean pass. Emitting a
    branch per column, each carrying that column's own domain as an enum, makes
    an invented value unrepresentable rather than merely wrong — which matters
    here more than anywhere, because a value is the one part of a spec that
    cannot be checked against a schema.
    """
    branches = []
    for column, values in _domain_catalog(request):
        properties = {
            **{key: value for key, value in base.items() if key != "column"},
            "column": {"type": "string", "enum": [column]},
            "values": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(values)},
                "minItems": 1,
            },
        }
        branches.append(
            {
                "type": "object",
                "properties": properties,
                "required": [*(key for key in required if key != "column"), "column", "values"],
                "additionalProperties": False,
            }
        )
    return branches


def _lookup_branches(
    request: WorkerRequest, base: Mapping[str, Any], required: list[str]
) -> list[dict[str, Any]]:
    """One params branch per candidate lookup table.

    ``lookup_column`` only means anything relative to a chosen ``lookup_table``,
    and a single pair of independent enums would admit every cross product of
    them — most of which name a column the chosen table does not have. Emitting a
    branch per table, each with its own single-valued table enum and that table's
    own key columns, makes the invalid combinations unrepresentable instead of
    leaving them to be caught after a turn has been spent. This is the same shape
    the ``test`` discriminator already uses.
    """
    branches = []
    for table, keys in _lookup_catalog(request):
        properties = {
            **base,
            "lookup_table": {"type": "string", "enum": [table]},
            "lookup_column": {"type": "string", "enum": sorted(keys)},
        }
        branches.append(
            {
                "type": "object",
                "properties": properties,
                "required": [*required, "lookup_table", "lookup_column"],
                "additionalProperties": False,
            }
        )
    return branches


def _analytics_spec_schemas(
    request: WorkerRequest,
) -> list[dict[str, Any]]:
    """Build exact per-test schemas from the contract and target columns."""
    schema = _target_schema(request)
    columns = _column_names(schema)
    column_types = _column_types(schema)
    rows = _target_rows(schema)
    populous = rows is None or rows >= MIN_ROWS_FOR_POPULATION_TESTS
    branches: list[dict[str, Any]] = []
    for test_id, metadata in sorted(_analytics_contract(request).items()):
        if not populous and test_id in POPULATION_TEST_IDS:
            continue
        properties: dict[str, Any] = {}
        required: list[str] = []
        usable = True
        needs_lookup = bool(metadata.get("needs_lookup"))
        needs_values = bool(metadata.get("needs_values"))
        for raw in metadata.get("params") or []:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            if needs_lookup and name in {"lookup_table", "lookup_column"}:
                # Supplied by the per-table branch below, where the two are
                # constrained together rather than independently.
                continue
            if needs_values and name == "values":
                # Supplied by the per-column branch below, from that column's
                # own vocabulary.
                continue
            parameter_schema = _parameter_json_schema(raw, columns, column_types)
            is_required = not raw.get("optional") and "default" not in raw
            if parameter_schema is None:
                if is_required:
                    usable = False
                    break
                continue
            if "default" in raw:
                parameter_schema["default"] = _plain_json(raw["default"])
            properties[name] = parameter_schema
            if is_required:
                required.append(name)
        if not usable:
            continue
        if needs_lookup or needs_values:
            # With no candidate the test cannot be written at all — a workspace
            # of one table has nothing to reconcile against, and a frame whose
            # every column is unique per row has no vocabulary to check — so it
            # is dropped rather than offered with an empty enum.
            params_branches = (
                _lookup_branches(request, properties, required)
                if needs_lookup
                else _value_branches(request, properties, required)
            )
            if not params_branches:
                continue
            params_schema: dict[str, Any] = (
                params_branches[0]
                if len(params_branches) == 1
                else {"oneOf": params_branches}
            )
        else:
            params_schema = {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
            if required:
                params_schema["required"] = required
        branches.append(
            {
                "type": "object",
                "properties": {
                    "test": {"type": "string", "enum": [test_id]},
                    "params": params_schema,
                },
                "required": ["test", "params"],
                "additionalProperties": False,
            }
        )
    return branches


def _analysis_submission_tool(request: WorkerRequest) -> dict[str, Any]:
    """One forced function tool whose schema constrains every generated spec."""
    analytics_specs = _analytics_spec_schemas(request)
    item_branches: list[dict[str, Any]] = []
    if analytics_specs:
        item_branches.append(
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": ["analytics"]},
                    "spec": {"oneOf": analytics_specs},
                    "note": {"type": "string", "minLength": 1},
                },
                "required": ["title", "kind", "spec", "note"],
                "additionalProperties": False,
            }
        )
    item_branches.append(
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["python"]},
                "spec": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "minLength": 1},
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
                "note": {"type": "string", "minLength": 1},
                "outcome_policy": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["exception_rows", "informational"],
                        }
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            },
            "required": ["title", "kind", "spec", "note"],
            "additionalProperties": False,
        }
    )
    return {
        "type": "function",
        "function": {
            "name": ANALYSIS_SUBMISSION_TOOL,
            "description": (
                "Submit the rerunnable analysis definitions this frame supports, "
                "up to the array's maximum — fewer where it supports fewer, and "
                "an empty array where it supports none. The schema contains "
                "every permitted analytics ID, parameter, and column."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "analyses": {
                        "type": "array",
                        "items": {"oneOf": item_branches},
                        # No minimum. A frame that relates two tables with
                        # nothing meaningful to compare across them should say
                        # so by proposing nothing, rather than filling its slots
                        # with whatever column pairing the schema permits.
                        "minItems": 0,
                        "maxItems": proposal_budget(
                            _target_rows(_target_schema(request)),
                            _measured_findings(request),
                        ),
                    }
                },
                "required": ["analyses"],
                "additionalProperties": False,
            },
        },
    }


def _submission_response(message: object) -> str:
    """Extract forced-tool arguments, with a text fallback for weak providers."""
    if not isinstance(message, Mapping):
        return str(message or "")
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        matches = [
            item
            for item in tool_calls
            if isinstance(item, Mapping)
            and isinstance(item.get("function"), Mapping)
            and item["function"].get("name") == ANALYSIS_SUBMISSION_TOOL
        ]
        if len(matches) == 1:
            arguments = matches[0]["function"].get("arguments")
            if isinstance(arguments, str):
                return arguments
            if isinstance(arguments, Mapping):
                return json.dumps(arguments, ensure_ascii=False)
    return str(message.get("content") or "")


def _analysis_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    proposed = payload.get("analyses")
    if proposed is None and isinstance(payload.get("analysis"), dict):
        # A single-object answer is a common shape slip; normalize rather than
        # spending a repair turn on it.
        proposed = [payload["analysis"]]
    # An empty array is a permitted answer, not a malformed response: the prompt
    # asks for one where the frame supports nothing, the submission schema sets
    # `minItems: 0` to allow it, and `validate_analysis_proposal` turns it into
    # NOTHING_NEW_TO_ANALYSE so the binder settles the unit as skipped. Rejecting
    # it here short-circuited that path — a model that complied with the prompt
    # spent both attempts being told its compliant answer was invalid, and the
    # run failed over an answer the workflow already knows how to accept.
    if not isinstance(proposed, list):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with an `analyses` array"
        )
    if any(not isinstance(item, dict) for item in proposed):
        raise WorkerResponseValidationError("every analyses entry must be an object")
    return {"analyses": proposed}


def _qualifying_key_columns(columns: list[str]) -> list[tuple[str, str]]:
    """(qualifier, qualified) pairs where one key column only narrows another.

    A duplicate-detection key is a claim about what "the same thing twice" means.
    Adding a column whose subject is already contained in another column's
    subject does not widen that claim, it narrows it — and narrows it along
    exactly the dimension the test was supposed to look across.

    The case this exists for: duplicates keyed on ``(VENDOR_ID,
    VENDOR_INVOICE_NUMBER)``. A vendor invoice number is the reference *the
    vendor assigned*, so grouping it by vendor asks whether one supplier billed
    the same number twice and can never see the same number arriving from two
    suppliers. That result was reported as "No duplicate keys found" over a
    population holding two such pairs — an affirmative clear of the risk the test
    was named for, which is worse than not having run it.

    Deliberately a name-shape rule and nothing more. ``(VENDOR_ID,
    INVOICE_DATE, INVOICE_AMOUNT)`` is a legitimate same-payment key and does not
    trip it: no subject there contains another.
    """
    subjects = {column: subject_tokens(column) for column in columns}
    pairs = []
    for qualifier, qualified in permutations(columns, 2):
        narrow, wide = subjects[qualifier], subjects[qualified]
        if narrow and narrow < wide:
            pairs.append((qualifier, qualified))
    return pairs


def _validate_analytics_spec(
    spec: Mapping[str, Any],
    registry: Mapping[str, Mapping[str, Any]],
    columns: set[str],
    column_types: Mapping[str, str],
    label: str,
    lookups: Mapping[str, set[str]] | None = None,
    domains: Mapping[str, set[str]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    test_id = analytics.canonical_test_id(spec.get("test") or spec.get("test_id"))
    if test_id not in registry:
        errors.append(f"{label} names unknown analytics test '{test_id or ''}'")
        return {"test": test_id, "params": {}}, errors
    raw_params = spec.get("params")
    if raw_params is not None and not isinstance(raw_params, Mapping):
        errors.append(f"{label} params must be an object")
    params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
    meta = registry[test_id]
    definitions = list(meta.get("params") or [])
    allowed = {
        str(parameter.get("name"))
        for parameter in definitions
        if isinstance(parameter, Mapping) and str(parameter.get("name") or "").strip()
    }
    unexpected = sorted(str(name) for name in params if str(name) not in allowed)
    if unexpected:
        errors.append(f"{label} has unsupported parameter '{unexpected[0]}' for '{test_id}'")
    for parameter in definitions:
        if not isinstance(parameter, Mapping):
            continue
        name = str(parameter.get("name"))
        kind = parameter.get("kind")
        has_value = name in params and params[name] not in (None, "", [])
        value = params.get(name)
        if not has_value:
            if not parameter.get("optional") and "default" not in parameter:
                errors.append(f"{label} is missing the required '{name}' parameter")
            continue
        if kind == "column":
            if str(value) not in columns:
                errors.append(
                    f"{label} names column '{value}' which is not in the supplied schema"
                )
            elif parameter.get("column_kind") and column_types.get(str(value)) != parameter["column_kind"]:
                errors.append(
                    f"{label} requires a {parameter['column_kind']} column for '{name}', "
                    f"but '{value}' is {column_types.get(str(value)) or 'unknown'}"
                )
        elif kind == "columns":
            values = value if isinstance(value, list) else []
            if not values:
                errors.append(f"{label} is missing the '{name}' column list")
            else:
                normalized_values = [str(item) for item in values]
                if len(normalized_values) != len(set(normalized_values)):
                    errors.append(f"{label} repeats a column in the '{name}' column list")
                unknown = [item for item in normalized_values if item not in columns]
                if unknown:
                    errors.append(
                        f"{label} names column '{unknown[0]}' which is not in the "
                        "supplied schema"
                    )
        elif kind == "select":
            allowed_values = [
                option.get("value")
                for option in parameter.get("options") or []
                if isinstance(option, Mapping) and "value" in option
            ]
            if not any(
                type(value) is type(allowed_value) and value == allowed_value
                for allowed_value in allowed_values
            ):
                errors.append(
                    f"{label} parameter '{name}' must be one of {allowed_values!r}"
                )
        elif kind == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            errors.append(f"{label} parameter '{name}' must be a number")
    # The lookup pair is checked together, because either half is meaningless
    # alone: a real table with a column it does not have, and a real column name
    # against a table nobody offered, are both a reconciliation against nothing.
    # The submission schema already makes both unrepresentable; this holds when a
    # provider returns something the schema did not describe.
    if test_id == "duplicates":
        key = [str(value) for value in params.get("columns") or []]
        for qualifier, qualified in _qualifying_key_columns(key):
            remaining = [column for column in key if column != qualifier]
            errors.append(
                f"{label} keys duplicates on '{qualifier}' together with "
                f"'{qualified}', and '{qualifier}' only narrows what "
                f"'{qualified}' already identifies — the test can then never see "
                f"the same {qualified} arriving under two different {qualifier} "
                f"values, which is the duplication worth finding. Key on "
                f"{', '.join(remaining)} instead"
            )
    if registry[test_id].get("needs_lookup"):
        catalog = dict(lookups or {})
        table = str(params.get("lookup_table") or "").strip()
        key = str(params.get("lookup_column") or "").strip()
        if table not in catalog:
            errors.append(
                f"{label} names lookup table '{table}' which was not supplied; "
                f"use one of {', '.join(sorted(catalog)) or 'the supplied tables'}"
            )
        elif key not in catalog[table]:
            errors.append(
                f"{label} names lookup column '{key}', which is not a key of "
                f"'{table}'; use one of {', '.join(sorted(catalog[table]))}"
            )
    if registry[test_id].get("needs_values"):
        vocabulary = dict(domains or {})
        column = str(params.get("column") or "").strip()
        raw = params.get("values")
        named = [
            str(value).strip()
            for value in (raw if isinstance(raw, (list, tuple)) else [raw])
            if str(value or "").strip()
        ]
        if not named:
            errors.append(f"{label} names no value to check against")
        elif column not in vocabulary:
            errors.append(
                f"{label} checks values in '{column}', whose vocabulary was not "
                f"supplied; use one of {', '.join(sorted(vocabulary)) or 'the supplied columns'}"
            )
        else:
            # A value is the one part of a spec no schema can vouch for. A
            # filter on a status the column never holds flags nothing and
            # records a clean pass over a question that was never asked.
            unknown = sorted(set(named) - vocabulary[column])
            if unknown:
                errors.append(
                    f"{label} names {', '.join(unknown)}, which "
                    f"{verb(len(unknown), 'does', 'do')} not occur in '{column}'; "
                    f"its values are {', '.join(sorted(vocabulary[column]))}"
                )
    return {"test": test_id, "params": params}, errors


def _validate_python_spec(
    spec: Mapping[str, Any],
    frames: set[str],
    label: str,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    code = str(spec.get("code") or "").strip()
    if not code:
        errors.append(f"{label} has no python code")
        return {"code": code}, errors
    try:
        # Static contract only: no execution, no workspace, no frames.
        sandbox.validate(code)
    except sandbox.SandboxError as error:
        errors.append(f"{label} is not safe Polars: {error}")
    referenced = set(re.findall(r"tables\[['\"]([^'\"]+)['\"]\]", code))
    unknown = sorted(referenced - frames)
    if unknown:
        errors.append(
            f"{label} reads table '{unknown[0]}' which was not supplied as context"
        )
    return {"code": code}, errors


def validate_analysis_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply everything the supplied context can decide about the proposal.

    Every violation across every proposed analysis is collected so one bounded
    repair turn can correct them together. The target frame is taken from the
    supplied schema rather than from the model, so a proposal cannot retarget
    itself at a frame it was never shown.
    """
    schema = _target_schema(request)
    target = str(schema["table"])
    columns = _column_names(schema)
    column_types = _column_types(schema)
    origins = _column_origins(schema)
    registry = _analytics_contract(request)
    lookups = {table: set(keys) for table, keys in _lookup_catalog(request)}
    domains = {column: set(values) for column, values in _domain_catalog(request)}
    existing = _existing_semantic_ids(request, origins)
    related = {
        str((_plain_json(item) or {}).get("table"))
        for item in _source_items(request, "related_frames")
        if isinstance(_plain_json(item), Mapping)
    }
    frames = {target, *(name for name in related if name)}

    proposed = list(proposal.get("analyses") or [])
    errors: list[str] = []
    duplicates: list[str] = []
    single_sided: list[str] = []
    # A frame built from more than one table exists to relate them. An analytics
    # spec there that reads only one of those tables computes what that table
    # alone computes, so it belongs on the table, not here — and it is exactly
    # the shape that filled a joined frame's slots with its sides' work.
    joined_frame = len(set(origins.values())) > 1
    budget = proposal_budget(_target_rows(schema), _measured_findings(request))
    if len(proposed) > budget:
        # Reported, but every proposal is still validated: an over-budget
        # response raises anyway, and trimming first would hide the violations
        # the one repair turn exists to correct together.
        errors.append(
            f"return at most {budget} analyses for a frame of this size; "
            f"{len(proposed)} were proposed"
        )

    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(proposed):
        label = f"analyses[{index}]"
        item = _plain_json(raw)
        if not isinstance(item, Mapping):
            errors.append(f"{label} must be an object")
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            errors.append(f"{label} is missing a title")
        # The rationale is required because writing it is the check. A
        # proposal whose author cannot say which business relationship the two
        # columns stand in has not established that they stand in one, and that
        # is precisely the proposal that compares a purchase order's date to a
        # vendor's approval date and flags the whole frame.
        note = str(item.get("note") or "").strip()
        if not note:
            errors.append(
                f"{label} is missing a note; state the relationship you expect "
                "to hold between these columns and why, in terms of the "
                "business events they record"
            )
        kind = str(item.get("kind") or "").strip()
        if kind not in ANALYSIS_KINDS:
            errors.append(
                f"{label} kind must be one of: {', '.join(ANALYSIS_KINDS)}"
            )
            continue
        raw_spec = item.get("spec")
        raw_spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        if kind == "analytics":
            spec, spec_errors = _validate_analytics_spec(
                raw_spec, registry, columns, column_types, label, lookups, domains
            )
        else:
            spec, spec_errors = _validate_python_spec(raw_spec, frames, label)
        outcome_policy = item.get("outcome_policy")
        if kind == "python":
            outcome_policy = dict(outcome_policy) if isinstance(outcome_policy, Mapping) else {"mode": "informational"}
            if outcome_policy.get("mode") not in {"exception_rows", "informational"}:
                spec_errors.append(
                    f"{label} outcome_policy.mode must be exception_rows or informational"
                )
        else:
            outcome_policy = None
        errors.extend(spec_errors)
        if spec_errors or not title or not note:
            continue
        if joined_frame:
            if kind == "analytics":
                # An analytics spec names its columns in declared parameters,
                # so an empty scope is a real answer: it reads nothing.
                scope = spec_scope(spec, origins)
                # A reconciliation reads a second frame by name rather than by
                # column, so counting only its columns makes every lookup test
                # look one-sided and drops it from exactly the frames it is
                # worth most on. Reconciling the approver's job title against
                # the approval matrix reads one column of the join — and asks it
                # once per invoice, which is a different population from asking
                # it once per member of staff, and the difference is the finding.
                lookup = str((spec.get("params") or {}).get("lookup_table") or "")
                if lookup:
                    scope = scope | {lookup}
                single = len(scope) < 2
            else:
                # Code is read for the column names it quotes, which no
                # procedure is obliged to quote — a frame-wide preview names
                # none and is exactly the work only the joined frame can do.
                # Naming none is therefore absence of evidence, not evidence of
                # one-sidedness; only code that does name columns, all from one
                # side, has shown itself to belong on that side.
                scope = python_spec_scope(spec, origins)
                single = bool(scope) and len(scope) < 2
            if single:
                # Dropped, not rejected, for the same reason a repeat is: the
                # rest of the response is still good work.
                single_sided.append(label)
                continue
        semantic = analysis_semantic_id(kind, target, spec, origins)
        if semantic in existing:
            # Dropped rather than rejected: the rest of the response is still
            # good work, and spending the repair turn to re-ask for four when
            # three were fine would cost a model call to gain nothing.
            duplicates.append(label)
            continue
        if semantic in seen:
            errors.append(f"{label} duplicates an earlier proposal in this response")
            continue
        seen.add(semantic)
        accepted.append(
            {
                "title": title,
                "kind": kind,
                # The frame comes from the supplied context, never the model.
                "table": target,
                "spec": spec,
                "note": str(item.get("note") or "").strip(),
                **({"outcome_policy": outcome_policy} if outcome_policy else {}),
                "semantic_id": semantic,
            }
        )

    if errors:
        raise WorkerResponseValidationError(errors)
    if not accepted:
        if duplicates or single_sided:
            # Worth one repair turn — a joined frame usually has something its
            # sides cannot compute. If the retry says the same thing, the frame
            # genuinely adds nothing, and the binder settles the unit as
            # skipped rather than failing the run over it.
            reasons = []
            if duplicates:
                reasons.append(
                    "repeats a computation already saved for these columns, on "
                    "this frame or another built from the same tables"
                )
            if single_sided:
                reasons.append(
                    "reads columns from only one of the tables this frame joins"
                )
            raise WorkerResponseValidationError(
                f"{NOTHING_NEW_TO_ANALYSE}: every proposed analysis "
                + ", or ".join(reasons)
                + ". Propose an analysis that uses columns from more than one of "
                "the joined tables."
            )
        # Nothing was proposed at all. That is now a permitted answer — the
        # budget is a ceiling, and a frame whose tables have nothing meaningful
        # to compare across them should say so rather than pad. It still costs
        # the one repair turn, because an empty array and a model that failed
        # to produce one are indistinguishable from here; if the retry agrees,
        # the binder settles the unit as skipped.
        raise WorkerResponseValidationError(
            f"{NOTHING_NEW_TO_ANALYSE}: no analysis was proposed for this frame. "
            "Propose one only if this frame genuinely supports it."
        )
    return {"analyses": accepted}


def run_analysis_definition_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    schema = _target_schema(request)
    tool = _analysis_submission_tool(request)
    item_branches = tool["function"]["parameters"]["properties"]["analyses"]["items"][
        "oneOf"
    ]
    analytics_branch = next(
        (
            item
            for item in item_branches
            if item["properties"]["kind"]["enum"] == ["analytics"]
        ),
        None,
    )
    eligible_ids = (
        [
            item["properties"]["test"]["enum"][0]
            for item in analytics_branch["properties"]["spec"]["oneOf"]
        ]
        if analytics_branch is not None
        else []
    )
    # The target schema is named once, above, in the shape the submission
    # tool's enums were built from. Repeating it inside the serialized bundle
    # bills the same 4 KB twice on every frame.
    #
    # The analytics catalog leaves the bundle for a different reason. One
    # definition turn is taken per frame, and the catalog is byte-identical on
    # every one of them — the same ~1,500 tokens re-sent per frame. A provider
    # can only reuse a prompt prefix it has already seen, and the prefix ends at
    # the first byte that differs, so a stable block sitting behind the frame
    # description can never be part of one. Promoted here it sits ahead of
    # everything frame-specific, extending the identical head from the system
    # prompt alone to the system prompt plus the catalog. Nothing is dropped:
    # the same content is sent, earlier.
    catalog = _plain_json(_resolved_item(request, ANALYTICS_REGISTRY_SOURCE_ID))
    # Named once, ahead of the bundle, for the same reason the target schema is:
    # the nominations are the part of this turn the model acts on directly, and
    # burying them among the aggregates they were derived from bills the same
    # content twice and reads as one more description of the frame.
    context = _model_facing_context(
        request,
        TARGET_SCHEMA_SOURCE_ID,
        ANALYTICS_REGISTRY_SOURCE_ID,
        PROBE_FINDINGS_SOURCE_ID,
        VALUE_DOMAINS_SOURCE_ID,
    )
    measured = [
        _plain_json(item) for item in _source_items(request, PROBE_FINDINGS_SOURCE_ID)
    ]
    domains = [
        _plain_json(item) for item in _source_items(request, VALUE_DOMAINS_SOURCE_ID)
    ]
    hypotheses = [
        _plain_json(item) for item in _source_items(request, JOIN_HYPOTHESIS_SOURCE_ID)
    ]
    user = json.dumps(
        {
            **({"ANALYTICS CATALOG": catalog} if catalog else {}),
            "TARGET FRAME": schema,
            **({"VALUE DOMAINS": domains} if domains else {}),
            **({"MEASURED ON THIS FRAME": measured} if measured else {}),
            # What this frame was admitted for. Present only on a frame the
            # utility gate retained a relationship for, and authoritative about
            # its purpose in a way no amount of schema reading recovers.
            **(
                {"TESTS THIS FRAME WAS MATERIALIZED TO SUPPORT": hypotheses}
                if hypotheses
                else {}
            ),
            "RESOLVED CONTEXT": context,
            "ALLOWED ANALYTICS IDS FOR THIS FRAME": eligible_ids,
            "REQUIRED OUTPUT": (
                f"Call {ANALYSIS_SUBMISSION_TOOL} exactly once. Its JSON Schema "
                "is authoritative; do not invent test IDs or parameters."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + ". Allowed analytics IDs are: "
            + ", ".join(eligible_ids)
            + f". Call {ANALYSIS_SUBMISSION_TOOL} once with a complete correction."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "analysis_definition",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    message = gateway.complete(
        ANALYSIS_DEFINITION_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
        tools=[tool],
        tool_choice={
            "type": "function",
            "function": {"name": ANALYSIS_SUBMISSION_TOOL},
        },
        return_message=True,
    )
    return _submission_response(message)


ANALYSIS_DEFINITION_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.definitions.response",
    schema_hash=_sha256_text(
        "analysis-definitions-response:v2:json-object-with-analyses-empty-permitted"
    ),
    validator=_analysis_response_schema,
)
ANALYSIS_DEFINITION_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_DEFINITION_WORKER_ID,
    prompt_hash=_sha256_text(ANALYSIS_DEFINITION_SYSTEM),
    response_schema=ANALYSIS_DEFINITION_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair analysis-definition contract violations against the supplied "
            "schema, aggregates, and analytics registry."
        ),
    ),
    implementation=run_analysis_definition_worker,
    semantic_validator=validate_analysis_proposal,
)

WORKERS.register(ANALYSIS_DEFINITION_WORKER)


# --------------------------------------------------------------------------- #
# analysis.summary
# --------------------------------------------------------------------------- #
ANALYSIS_SUMMARY_WORKER_ID = "analysis.summary"

# The memo's structure is fixed in code rather than in an editable template: it
# is a derived artifact regenerated from results, so its shape belongs to the
# contract the validator enforces, not to a per-engagement preference.
#
# The sections are deliberately finding-shaped rather than procedure-shaped. An
# earlier skeleton ran "Procedures performed", "Exceptions noted", and "Data
# quality observations" as three sections, which sorted one set of facts three
# ways — by procedure, then by severity, then by theme — and every failing
# procedure is an exception, so the second pass restated the first almost
# entirely. A heading is a stronger instruction than any sentence of guidance
# below it: asked for a section of procedures performed, a writer supplies a
# list of procedures performed, whatever the prose asks for. There is also no
# "relationships and joins" section, because a register of joins established is
# not a finding; what a join's match rate and unmatched count *mean* belongs
# with the population they qualify and the results that rest on them.
SUMMARY_SECTIONS: tuple[str, ...] = (
    "Data received and its limitations",
    "What the analysis found",
    "How far these results can be relied on",
    "Further work required",
)

_SECTION_LIST = "\n".join(f"## {section}" for section in SUMMARY_SECTIONS)

ANALYSIS_SUMMARY_SYSTEM = f"""[agent:analysis_summary]
Write the exploratory data analysis summary for one audit engagement, as the
auditor who performed the work would write it for the file. Ground every
statement in the supplied procedures, their recorded verdicts and statistics,
the flagged rows supplied for them, the table profiles, and the supplied
coverage gaps. Never invent a count, a value, an identifier, or a procedure.

Open with a short paragraph, before any heading, saying what the analysis
concluded — the two or three things a reader who stops there should leave with.
Not what was performed: what it showed.

Then use exactly these level-2 sections, in this order, and no others:
{_SECTION_LIST}

This is an analysis, not a register of procedures. Nobody reads it to learn
which tests were run; they read it to learn what the data says about the
engagement. So organise "What the analysis found" by *issue* — one thread per
thing that is actually true of this population — and cite the procedures that
establish each one as support inside that thread. Never organise by procedure,
by test type, or by verdict label. If two procedures evidence one issue, they
belong in one paragraph; if one procedure evidences two, it is cited twice.
Order the issues by how much they matter — exposure, then how far the evidence
carries — and say why the first one is first. A procedure that established
nothing does not earn a mention here at all.

Say each thing once. A fact that belongs to a finding goes in "What the
analysis found"; a fact about how much the results can be trusted goes in "How
far these results can be relied on". Restating a finding under a second heading
because it is also a data-quality point is the single fastest way to make a
memo unreadable.

Write the way an auditor writes for the file. Use whichever form carries the
content best, and mix them freely within a section:

- Prose for judgment, cause, and anything qualified — what the data shows, what
  it does not settle, and why it matters. This should be most of the memo.
- A Markdown table where the content is genuinely tabular. The data received is
  one: a row per source table with its record count, period covered, and a short
  commentary. Coverage of procedures by cycle stage or by table is another, and
  it belongs in "How far these results can be relied on" — one table there is
  the record of work performed, and it replaces any enumeration of procedures.
- A list where the content is genuinely a set of items — outstanding work, or
  the corroboration a finding needs.

Structure has to earn its place: never emit a bullet per procedure, and do not
break a line of reasoning into fragments. A section that is one argument should
be one or two paragraphs. Use standard Markdown tables with a `---` separator
row of at least three dashes per column.

State the population before the exceptions. Distinguish what the data
establishes from what it merely suggests, and say plainly where a conclusion
cannot yet be drawn.

An exception nobody can locate is not an exception anybody can follow up, so
name instances — but name the two or three that carry the finding, chosen for
size or egregiousness, with their amounts and dates, not the first few you were
handed. For everything beyond that, embed the result: the reader gets the whole
flagged set as a table they can open, which is more than a list of identifiers
in a sentence gives them and costs the paragraph nothing. A run of bare
identifiers is not evidence, it is a truncated copy of a table that already
exists. Remember also that the rows you were shown are a capped sample —
`rows_supplied` of `exception_count` — so never write or imply that they are
the set.

A procedure's title is what somebody called it; its `test` and `parameters` —
or, for a python procedure, its `code` and `outcome_policy` — are what it did.
Read those, and report what ran. Where the two disagree the title is wrong, and
saying so is the finding: a duplicate-key test over two join columns found
repeated keys, which is not the same as the two systems disagreeing; a date-lag
test flags whichever order its parameters name, so one direction of a date pair
is the exception and the reverse direction is the population behaving normally.
Under `outcome_policy: exception_rows` every row a python procedure returns is
counted as a potential exception, so code that returns its frame without
narrowing it has counted the population — say that, rather than reporting the
count as exceptions.

Every count is a count of something. Give exceptions against `tested`, the rows
the procedure could evaluate, and where `not_tested` is non-trivial say so and
say why the rows fell out — a null key or an unparseable date is a row no
conclusion covers, which is itself a limitation of the work. `row_count` is the
size of a result frame and is never a denominator. Where a procedure records no
denominator, do not supply one.

Use joins only from the supplied join definitions, and never infer a join key
from a column name or a frame name. There is no section for cataloguing them:
what a join *shows* is what belongs in the memo. Its unmatched count is a
population fact — records on one side with no counterpart on the other are
records no procedure over that frame covers — and belongs with the data
received. Where a join multiplied rows, every per-row count taken over that
frame counts duplicated rows, so say so wherever you report one.

You are shown the flagged rows of many procedures at once, so you will notice
things no single procedure tested — the same vendor across two exception sets,
a value that recurs, a pattern spanning frames. Say so, but never as an
established fact: no procedure computed it, and a handful of supplied rows is
not the population. Write it as what it is — an observation to confirm — and
say what would confirm it. Never state a count, a proportion, or "all" or
"none" about a pattern no supplied result actually measured.

"How far these results can be relied on" is about the instrument, not about the
findings. It carries the coverage table — what was tested across the cycle and
what was not — and then the things that bound how far the rest of the memo can
be pushed: rows no procedure could evaluate, populations too small or too
skewed for the method applied, and results that measured nothing.

A procedure carrying `informative: false` established nothing, and
`uninformative_reason` says why. It is not a finding and must not appear as one
— no count from it, no verdict, no "systemic" reading of it. Mention it here if
it is worth mentioning at all, as what it is: a procedure that did not
distinguish any of its rows from the rest. Apply the same judgment where no
flag is set but the arithmetic says the same thing — a weekend share close to
two days in seven is what any ordinary spread of dates produces, not a finding
about after-hours work.

"Further work required" must cover every outstanding, stale, and errored
procedure named in the supplied coverage gaps, every frame carrying no
procedure, and the judgment items the results themselves raise — corroboration
a data test cannot supply, populations needing a different cut, controls the
data cannot see. Do not invent work the supplied material does not support.

Where the outstanding work is a procedure this workspace could run, specify it
so the reader can write it without re-deriving your reasoning: name the frame
or the tables to join, the columns, and the comparison. "Join requisitions to
financial_approval_matrix through the approver's job title and test
ESTIMATED_TOTAL_COST against MAX_APPROVAL_AMOUNT" is a usable instruction;
"perform further approval testing" is not. Where the work is corroboration
outside the data — a document, a person, a system behaviour — say what evidence
is wanted and from whom.

To place a result in the memo, emit a fenced block on its own lines, carrying
exactly the three fields `analysis`, `as` and `caption`. Filled in, one reads:
```{EMBED_FENCE}
analysis: A-0DAB063C
as: exception_table
caption: the four vendor rows sharing one bank account
```
Both values there are illustration. Every block you write must name a procedure
from the supplied results by its exact analysis_id — an id that was not
supplied is rejected, and so is a block left holding the shape of a value
instead of a value. Where you cannot fill a block in from what you were
supplied, delete it: a block naming nothing is worse than no block, because it
reads as evidence and opens on nothing.

`as` is one of {" | ".join(EMBED_KINDS)}, and that set is closed. An embed is
not a footnote — it renders the result itself, as a table the reader can read
and open, so it is where the detail behind a finding belongs. In "What the
analysis found", every cited procedure whose supplied `exception_count` is
greater than zero must be embedded as `exception_table`. Use `chart` for a
distribution or a trend that has no flagged-row set, and `stats` where the
numbers are the point. Do not embed a procedure you only mention in passing,
and do not embed one twice.

Return the finished memo and nothing else: Markdown only, no JSON wrapper, no
outer code fence, and none of the drafting — no note to yourself, no working
comment, no abandoned first pass left above the real one. Emit each section
once.
"""

SUMMARY_RESULTS_SOURCE_ID = "analysis_results"
SUMMARY_EXCEPTIONS_SOURCE_ID = "analysis_exceptions"


def _resolved_items(request: WorkerRequest, source_id: str) -> list[object]:
    return [item.content for item in request.context.items if item.source_id == source_id]


FINDINGS_SECTION = "What the analysis found"
RELIANCE_SECTION = "How far these results can be relied on"

# A conversational hand-off, not a lead paragraph: one short line ending in a
# colon, before anything else. Bounded in length so a genuine opening sentence
# that happens to introduce a list is not mistaken for chat.
_PREAMBLE = re.compile(r"\A[^\n]{0,120}:[ \t]*\n+")


def _citation_pattern(supplied: set[str]) -> re.Pattern[str] | None:
    """Match exactly the analysis ids this memo was shown, and nothing else.

    Deliberately not a shape. A workflow-authored procedure is ``A-1F2E3D4C``
    and one the auditor saved by hand is a bare hex string, so any regex
    guessing the format would silently skip half the register — and read the
    bare form as an instance identifier, since it too mixes letters and digits.
    """
    if not supplied:
        return None
    alternatives = "|".join(
        re.escape(item) for item in sorted(supplied, key=len, reverse=True) if item
    )
    if not alternatives:
        return None
    return re.compile(rf"(?<![A-Za-z0-9_-])({alternatives})(?![A-Za-z0-9_-])")


def _prose_lines(markdown: str) -> Iterator[tuple[int, str]]:
    """Every line of the memo a reader reads as prose, with its position.

    Table rows and embed blocks are dropped: both legitimately carry many
    identifiers, and neither is prose a reader has to wade through. One
    implementation because two rules that disagree about what counts as prose
    would disagree silently — one reading a citation the other cannot see.
    """
    fenced = False
    for index, line in enumerate(markdown.splitlines()):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith("|"):
            continue
        yield index, line


# A token that could be meant as a citation: an identifier run, kept together
# with the marks a truncated one ends in, so ``A-DF4??`` is read as one broken
# reference rather than as a good prefix followed by punctuation.
_REFERENCE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9][A-Za-z0-9_?…-]*)")

# How much of a procedure's id has to survive in a token before the token is
# read as a mangled copy of it. Ids are high-entropy hex, so a run this long
# reaching an unrelated identifier is not a thing that happens; a run much
# shorter starts matching dates and amounts.
_MIN_CITATION_RUN = 7


def _citation_conventions(
    supplied: set[str],
) -> tuple[frozenset[str], dict[str, str]]:
    """The prefixes and the id bodies this workspace's procedures actually use.

    Derived from the supplied register rather than assumed, for the same reason
    ``_citation_pattern`` matches ids exactly: a workflow-authored procedure is
    ``A-1F2E3D4C`` and a hand-saved one is a bare hex string, so a guessed shape
    would either miss half the register or read instance identifiers as
    procedures. Here it also keeps the check away from the business identifiers
    the memo is full of — ``REQ-2024081`` and ``PO-20251017`` carry prefixes no
    procedure uses, so nothing about them looks like a citation.
    """
    prefixes: set[str] = set()
    stems: dict[str, str] = {}
    for item in supplied:
        prefix, separator, stem = item.partition("-")
        if separator and prefix and stem:
            prefixes.add(prefix.casefold())
            stems[stem.upper()] = item
        elif not separator and item:
            # A hand-saved procedure is a bare hex string with no prefix to
            # lose. There is no prefix rule to derive from it, but it can still
            # be truncated or mistyped, and the body is the whole id.
            stems[item.upper()] = item
    return frozenset(prefixes), stems


def _looks_like_an_id(value: str) -> bool:
    """Whether what follows a procedure prefix was meant to be an id.

    Bounds the prefix rule to references, so an ordinary hyphenated word that
    happens to start with a procedure's prefix letter is not read as a citation.
    """
    body = value.rstrip("?…")
    if len(body) < 3 or not body.isalnum():
        return False
    return any(character.isdigit() for character in body) or body.isupper()


def _unresolvable_citations(markdown: str, supplied: set[str]) -> list[str]:
    """References written where a citation belongs that open on nothing.

    The gate above requires an exception table under any *resolvable* citation,
    which quietly made a correctly written id the expensive one: a memo that
    cited ``A-C9CCB8C4`` was held to the rule, and the same memo citing
    ``C9CCB8C4`` or ``A-DF4??`` for the same procedure was not held to anything,
    because neither matches an id and the scan saw no citation there at all. So
    the rule fell hardest on the references that were right, and the reader was
    left with the ones that were wrong.

    Two forms are recognised, and both are anchored to the supplied register
    rather than to a guessed shape: a reference carrying a prefix this
    workspace's procedures use that names no procedure, and a reference
    carrying enough of one procedure's id to be a copy of it that has lost the
    prefix or some characters. What is deliberately not recognised is a
    reference with nothing of a real id left in it — there is nothing to
    anchor on, and guessing would start flagging invoice numbers.
    """
    prefixes, stems = _citation_conventions(supplied)
    if not prefixes and not stems:
        return []
    # Runs long enough to identify one procedure, letter-bearing so a run of
    # digits shared with a date or an amount cannot stand in for one.
    needles: dict[str, str] = {}
    for stem, analysis_id in stems.items():
        for start in range(len(stem) - _MIN_CITATION_RUN + 1):
            run = stem[start : start + _MIN_CITATION_RUN]
            if any(character.isalpha() for character in run):
                needles.setdefault(run, analysis_id)

    reported: dict[str, str] = {}
    for _index, line in _prose_lines(markdown):
        for match in _REFERENCE_TOKEN.finditer(line):
            token = match.group(1)
            if token in supplied or token in reported:
                continue
            prefix, separator, body = token.partition("-")
            if separator and prefix.casefold() in prefixes and _looks_like_an_id(body):
                reported[token] = (
                    f"'{token}' is written as a procedure reference but names no "
                    "supplied procedure; cite one by its exact id or drop the "
                    "reference"
                )
                continue
            upper = token.upper()
            for run, analysis_id in needles.items():
                if run in upper:
                    reported[token] = (
                        f"'{token}' reads as a mangled copy of '{analysis_id}'; "
                        f"cite '{analysis_id}' exactly or drop the reference"
                    )
                    break
    return list(reported.values())


def _memo_section_lines(markdown: str) -> list[tuple[str, list[tuple[int, str]]]]:
    """The level-2 sections in document order, each with its numbered prose.

    Line numbers travel with the prose because a validator that can only repair
    what it can locate needs to point back into the document it was handed, not
    into a filtered copy of it.
    """
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    heading: str | None = None
    body: list[tuple[int, str]] = []
    for index, line in _prose_lines(markdown):
        match = re.fullmatch(r"##\s+(.+?)\s*", line)
        if match:
            if heading is not None:
                sections.append((heading, body))
            heading, body = match.group(1).strip(), []
        elif heading is not None:
            body.append((index, line))
    if heading is not None:
        sections.append((heading, body))
    return sections


def _memo_sections(markdown: str) -> list[tuple[str, list[str]]]:
    """The level-2 sections in document order, each with its prose lines."""
    return [
        (heading, [line for _, line in body])
        for heading, body in _memo_section_lines(markdown)
    ]


def _lead_paragraph(markdown: str) -> str:
    """Whatever the memo actually *says* before its first section heading.

    A title and a horizontal rule are not an opening: both are punctuation, and
    a memo whose first words are a rule has still gone straight to its headings.
    """
    head = re.split(r"(?m)^##\s+", markdown, maxsplit=1)[0]
    return "\n".join(
        line
        for line in head.splitlines()
        if not line.lstrip().startswith("#")
        and not re.fullmatch(r"\s*([-_*])(\s*\1){2,}\s*", line)
    ).strip()


def _paragraph_end(lines: list[str], start: int) -> int:
    """Where the paragraph containing ``start`` stops.

    Fences are stepped over rather than stopped at: a paragraph that already
    carries an embed still ends at the blank line after it, and an insertion
    point inside a fence would be swallowed as fence content.
    """
    fenced = False
    index = start
    while index < len(lines):
        stripped = lines[index].lstrip()
        if stripped.startswith("```"):
            fenced = not fenced
            index += 1
            continue
        if not fenced and (not stripped or stripped.startswith("## ")):
            return index
        index += 1
    return len(lines)


def _exception_table_embed(analysis_id: str, result: Mapping[str, Any]) -> list[str]:
    """The embed directive the memo should have carried for one procedure.

    The caption is the recorded verdict rather than authored prose. It is the
    one line already written about what the reader should see in the result,
    and inventing a better one here would be the validator writing memo copy.
    """
    caption = str(result.get("verdict_text") or result.get("title") or "").strip()
    block = [
        f"```{EMBED_FENCE}",
        f"analysis: {analysis_id}",
        "as: exception_table",
    ]
    if caption:
        block.append(f"caption: {caption}")
    block.append("```")
    return block


def _embed_exception_tables(
    markdown: str,
    placements: Mapping[str, int],
    results_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    """Place the exception tables the findings cite but did not embed.

    Which result belongs under which finding is not a judgment call — the
    citation names it, the kind is fixed, and the caption is the verdict the
    procedure already recorded. Asking the model for it spends a repair turn on
    bookkeeping the validator can do outright, and spending the turn is how a
    memo that is right about the engagement gets discarded for punctuation.
    The model is still told to place these itself; this is what happens when it
    does not, not a replacement for it doing so.
    """
    lines = markdown.splitlines()
    # Bottom-up, so an insertion never moves the line another insertion is
    # still pointing at.
    for analysis_id, line_number in sorted(
        placements.items(), key=lambda item: item[1], reverse=True
    ):
        block = _exception_table_embed(analysis_id, results_by_id.get(analysis_id) or {})
        stop = _paragraph_end(lines, line_number)
        trailing = [] if stop < len(lines) and not lines[stop].strip() else [""]
        lines[stop:stop] = ["", *block, *trailing]
    # Stripped for the same reason the incoming markdown was: the validator's
    # output is the memo, and an insertion at the end of the document must not
    # be the reason it gains trailing whitespace.
    return "\n".join(lines).strip()


def validate_analysis_summary(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Enforce the memo skeleton, its citations, and its shape.

    The skeleton checks are the older half. The rest exist because a memo can
    satisfy every structural rule and still be a catalogue: the failure mode
    this guards is a document that lists procedures, restates each one under a
    second heading, and pastes flagged-row identifiers into prose instead of
    embedding the result the reader could have opened.
    """
    markdown = str(proposal.get("markdown") or "").strip()
    if not markdown:
        raise WorkerResponseValidationError("the summary is empty")

    errors: list[str] = []
    # The half of the rules that decide whether a memo may be committed at all
    # when the repair budget runs out. An unresolvable citation and a procedure
    # that established nothing written up as a finding both make the memo say
    # something untrue about the engagement, and a memo in the audit file is
    # read as the work. Everything else here is shape — a section out of order,
    # a restatement, a duplicated embed — and a misshapen memo still beats the
    # nothing that gets committed when the whole document is discarded.
    untrue: list[str] = []

    def reject(message: str, *, integrity: bool = False) -> None:
        if message not in errors:
            errors.append(message)
            if integrity:
                untrue.append(message)

    sections = _memo_section_lines(markdown)
    present = [name for name, _ in sections]
    folded = {name.casefold() for name in present}
    missing = [
        section for section in SUMMARY_SECTIONS if section.casefold() not in folded
    ]
    if missing:
        for section in missing:
            reject(f"missing section '{section}'")
    # A memo that abandons a draft partway and starts again emits the whole
    # skeleton twice. That reads to the ordering rule below as a jumble, and
    # "sections are out of order" then sends the repair turn to rearrange a
    # document whose actual defect is that it is two documents.
    counts = Counter(name.casefold() for name in present)
    repeated = [
        section for section in SUMMARY_SECTIONS if counts[section.casefold()] > 1
    ]
    if repeated:
        # One message, because it is one defect. Four sections duplicated is a
        # memo written twice, not four separate things to go and fix, and
        # spending four of the repair turn's errors saying so crowds out the
        # ones that name something else.
        reject(
            "the memo is written more than once: "
            + ", ".join(
                f"'{section}' appears {counts[section.casefold()]} times"
                for section in repeated
            )
            + ". Return one document — keep a single copy of each section, and "
            "delete the abandoned pass along with any note you wrote to yourself "
            "while drafting"
        )
    # Order carries meaning here: the populations frame the findings, and the
    # findings are what the reliance limits qualify. Read out of order they
    # argue for nothing. Only worth saying once the skeleton is single: against
    # a duplicated one the comparison always fails, and says the wrong thing.
    ordered = [name for name in present if name.casefold() in {
        section.casefold() for section in SUMMARY_SECTIONS
    }]
    expected = [
        section
        for section in SUMMARY_SECTIONS
        if section.casefold() in {name.casefold() for name in ordered}
    ]
    if not repeated and [name.casefold() for name in ordered] != [
        name.casefold() for name in expected
    ]:
        reject("sections are out of order: expected " + ", ".join(expected))

    if not _lead_paragraph(markdown):
        reject(
            "the summary opens with no paragraph saying what the analysis concluded"
        )

    results = [
        item
        for item in _resolved_items(request, SUMMARY_RESULTS_SOURCE_ID)
        if isinstance(item, Mapping)
    ]
    supplied = {str((item or {}).get("analysis_id") or "") for item in results}
    if not supplied:
        raise WorkerContractError("The analysis summary context supplied no procedures.")
    results_by_id = {
        str(item.get("analysis_id") or ""): item
        for item in results
        if str(item.get("analysis_id") or "")
    }
    # Procedures the workspace already determined establish nothing. Whether a
    # result separated any of its rows from the rest is arithmetic, so it is
    # decided before the memo is written rather than left to be spotted in a
    # denominator — the failure this prevents is a saturated result written up
    # as a systemic finding, which is what it looks like from every angle
    # except that one.
    uninformative = {
        str(item.get("analysis_id") or ""): str(item.get("uninformative_reason") or "")
        for item in results
        if item.get("informative") is False
    }

    embeds = parse_embeds(markdown)
    embedded: list[str] = []
    embedded_kinds: dict[str, str] = {}
    for embed in embeds:
        analysis_id = embed.get("analysis")
        if not analysis_id:
            reject("an embed names no analysis; delete the block")
            continue
        # A citation the reader cannot open is worse than no citation at all: it
        # looks like evidence and resolves to nothing.
        #
        # Every one of these says deleting the block is a way to comply, because
        # a repair turn told only that a value is wrong will reach for a
        # replacement, and a worker with no valid replacement to hand invents
        # one. That is not hypothetical: told an embed named no supplied
        # procedure, one repair substituted an id that did not exist, under a
        # kind that did not exist, three lines above the correct embed for the
        # same result.
        if analysis_id not in supplied:
            reject(
                f"embed cites '{analysis_id}', which is not a supplied procedure. "
                "Name a procedure from the supplied results, or delete the whole "
                "block — never substitute an id you cannot find in them",
                integrity=True,
            )
        kind = embed.get("as") or ""
        if kind not in EMBED_KINDS:
            reject(
                f"embed for '{analysis_id}' uses unknown kind '{kind}'. Use one of "
                f"{', '.join(EMBED_KINDS)}, or delete the block — the kinds are a "
                "closed set and a new one cannot be coined"
            )
        if analysis_id in embedded:
            reject(
                f"'{analysis_id}' is embedded more than once; keep the placement "
                "that carries the finding and delete the other block"
            )
        else:
            embedded.append(analysis_id)
            if analysis_id in supplied and kind in EMBED_KINDS:
                embedded_kinds[analysis_id] = kind

    # Before the citation scan, because the scan can only see references that
    # resolve, and the ones that do not are exactly what this reads.
    #
    # Shape rather than integrity, unlike the same defect in an embed. An embed
    # is rendered evidence, and one naming nothing puts a block in the memo
    # claiming to show a result that does not exist. A broken reference in prose
    # is a pointer the reader cannot follow next to a finding that is still
    # true, and discarding an otherwise-sound memo over a mistyped id is the
    # failure this whole gate was rewritten to stop causing.
    for message in _unresolvable_citations(markdown, supplied):
        reject(message)

    citation = _citation_pattern(supplied)
    cited: dict[str, set[str]] = {}
    # Where each unembedded result is argued, so the table can be placed under
    # the paragraph that argues it rather than appended at the end of a section
    # it has nothing to do with. The last citation wins: a thread that returns
    # to a result closes on the reading the table is evidence for.
    unembedded: dict[str, int] = {}
    for name, body in sections:
        for line_number, line in body:
            on_line = citation.findall(line) if citation else []
            for analysis_id in on_line:
                cited.setdefault(analysis_id, set()).add(name)
            if name != FINDINGS_SECTION:
                continue
            for analysis_id in on_line:
                if analysis_id in uninformative:
                    reject(
                        f"'{analysis_id}' cannot be a finding: it "
                        f"{uninformative[analysis_id]}. Report it under "
                        f"'{RELIANCE_SECTION}' as a limit on the work, if at all",
                        integrity=True,
                    )
                result = results_by_id.get(analysis_id) or {}
                try:
                    has_exceptions = int(result.get("exception_count") or 0) > 0
                except (TypeError, ValueError):
                    has_exceptions = False
                kind = embedded_kinds.get(analysis_id)
                if has_exceptions and kind != "exception_table":
                    if kind is None:
                        unembedded[analysis_id] = line_number
                    else:
                        # Already placed, under the wrong kind. Which view of a
                        # result carries a finding is the writer's call and
                        # replacing it here would overrule prose the validator
                        # cannot read, so this one goes back to the model.
                        reject(
                            f"'{analysis_id}' supports a finding with flagged rows "
                            f"but is embedded as {kind}, not exception_table"
                        )

    # The two sections the memo must keep apart. Findings belong above,
    # reliance limits below, and a procedure argued in both is the restatement
    # the older skeleton produced by construction.
    for analysis_id, names in cited.items():
        if {FINDINGS_SECTION, RELIANCE_SECTION} <= names:
            reject(
                f"'{analysis_id}' is argued in both '{FINDINGS_SECTION}' and "
                f"'{RELIANCE_SECTION}'; say it once, in whichever it belongs to"
            )

    # Done after the rules that read the document, so every error is raised
    # against what the model actually wrote, and before the result is assembled,
    # so what the memo carries and what it reports carrying are the same list.
    if unembedded:
        markdown = _embed_exception_tables(markdown, unembedded, results_by_id)
        embedded = [
            *embedded,
            *sorted(unembedded, key=lambda item: unembedded[item]),
        ]

    if errors:
        # A memo the budget ran out on is committed only where every surviving
        # error is about shape. Where one of them makes the memo untrue, there
        # is nothing to salvage: the strict path discards it, and a gap in the
        # workspace is the honest outcome.
        raise WorkerResponseValidationError(
            errors,
            partial=(
                None
                if untrue
                else {"markdown": markdown, "cited_analysis_ids": embedded}
            ),
        )

    return {
        "markdown": markdown,
        "cited_analysis_ids": embedded,
    }


def _summary_response_schema(response: str) -> Mapping[str, Any]:
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:markdown|md)?\s*\n?(.*?)\n?```", value, re.DOTALL | re.IGNORECASE
    )
    # Only unwrap an outer fence that wraps the whole document. A memo whose
    # body carries embed fences must not be mistaken for a fenced document and
    # gutted down to its first block.
    if fenced:
        inner = fenced.group(1).strip()
        if "```" not in inner:
            value = inner
    # Everything before the first heading used to be discarded as model
    # preamble. The memo now opens with a paragraph saying what the analysis
    # concluded, so that rule would delete the one part a reader who stops
    # early actually reads. What is left is the narrow case it was written for:
    # a single short line handing over to the document, which announces itself
    # by ending in a colon.
    value = _PREAMBLE.sub("", value, count=1).strip()
    return {"markdown": value}


def run_analysis_summary_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        {"RESOLVED CONTEXT": _model_facing_context(request)},
        indent=1,
        ensure_ascii=False,
        default=str,
    )
    conversation = None
    if attempt.is_repair:
        if attempt.previous_response is None:
            raise WorkerContractError(
                "An analysis-summary repair requires the previous response."
            )
        conversation = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": attempt.previous_response},
            {
                "role": "user",
                "content": (
                    "Correct every listed quality-gate error in the prior summary "
                    "while preserving all otherwise-valid wording, evidence links, "
                    "and embeds: "
                    + "; ".join(attempt.validation_errors)
                    + ". Return the complete corrected summary as Markdown only."
                ),
            },
        ]
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "analysis_summary",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        ANALYSIS_SUMMARY_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
        conversation=conversation,
    )


ANALYSIS_SUMMARY_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.summary.response",
    schema_hash=_sha256_text("analysis-summary-response:markdown-with-embed-fences"),
    validator=_summary_response_schema,
)
ANALYSIS_SUMMARY_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_SUMMARY_WORKER_ID,
    prompt_hash=_sha256_text(ANALYSIS_SUMMARY_SYSTEM),
    response_schema=ANALYSIS_SUMMARY_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair missing or misordered summary sections, a missing lead "
            "paragraph, citations to unsupplied procedures, exception-bearing "
            "findings embedded under the wrong kind, and findings restated "
            "under the reliance section."
        ),
        # One memo argues every procedure in the workspace, so its errors scale
        # with the register rather than with the length of the response. The
        # default of eight is a whole class of finding short of that, and an
        # error that does not fit is an error the one repair turn cannot fix.
        max_validation_errors=MAX_REPAIR_ERRORS,
    ),
    implementation=run_analysis_summary_worker,
    semantic_validator=validate_analysis_summary,
)

WORKERS.register(ANALYSIS_SUMMARY_WORKER)


__all__ = [
    "ANALYSIS_DEFINITION_RESPONSE_SCHEMA",
    "ANALYSIS_DEFINITION_SYSTEM",
    "ANALYSIS_DEFINITION_WORKER",
    "ANALYSIS_DEFINITION_WORKER_ID",
    "ANALYSIS_KINDS",
    "JOIN_UTILITY_WORKER",
    "JOIN_UTILITY_WORKER_ID",
    "ANALYSIS_SUMMARY_RESPONSE_SCHEMA",
    "ANALYSIS_SUMMARY_SYSTEM",
    "ANALYSIS_SUMMARY_WORKER",
    "ANALYSIS_SUMMARY_WORKER_ID",
    "EMBED_KINDS",
    "MAX_PROPOSED_ANALYSES",
    "SUMMARY_SECTIONS",
    "analysis_semantic_id",
    "parse_embeds",
    "run_analysis_definition_worker",
    "run_join_utility_worker",
    "run_analysis_summary_worker",
    "validate_analysis_proposal",
    "validate_join_utility_proposal",
    "validate_analysis_summary",
]


# --------------------------------------------------------------------------- #
# analysis.promotion
# --------------------------------------------------------------------------- #
ANALYSIS_PROMOTION_WORKER_ID = "analysis.promotion"
PROMOTION_SUBJECT_SOURCE_ID = "promotion_subject"
PROMOTION_RCM_SOURCE_ID = "rcm_rows"
PROMOTION_TABLE_SOURCE_ID = "table_metadata"

ANALYSIS_PROMOTION_SYSTEM = f"""[agent:analysis_promotion]
You are placing one exploratory data-analysis procedure into the audit's risk
and control matrix, or setting it aside.

The procedure has already run and already flagged rows. Nothing asks you to
decide whether its arithmetic is right or to reproduce what it found. You are
answering one question: **is this procedure evidence about a control, and if so
which one?**

Promote when the condition the procedure flags would, if real, indicate that a
control did not operate — an amount that should have agreed and did not, an
authorisation that is missing or exceeded, a sequence that ran out of order, a
record that should be unique and is not, a payment to a party that was not
approved.

Decline when the condition is a property of the data rather than a failure of a
control: a calendar fact (activity on a weekend or a holiday), a distribution
fact (a value unusual only relative to its own population), a descriptive
count, or a data-quality observation with no control that would have prevented
it. Declining is a real answer and the reason is recorded — say plainly what
the procedure establishes and why that is not a control exception.

Choosing the row. Pick the RCM row whose control the flagged condition would
be a failure *of*. Where several could hold it, pick the one whose control
requirement names the same thing the procedure measures. Do not create a row
and do not pick a row merely because it mentions the same population.

Writing the step.
- Where `code` is supplied, it is the procedure that produced these exceptions.
  Return it **unchanged** as the step's code. Do not rewrite, re-filter, tidy
  or extend it.
- Where `code` is empty, the procedure is a catalog test named by
  `catalog_test` with `parameters`. Write the Polars step that performs exactly
  that test, using only the supplied schemas for column spelling.
  The step runs in a sandbox whose only names are `pl`, `tables`, and each
  supplied frame under its own table name. Read the frame the procedure ran
  against — the one `source_frame_name` names — as `tables["<that name>"]` or
  by that bare name, and assign the flagged rows to `result`. No other
  variable exists; a name you have not created yourself will not resolve.
- `population` is a table name copied exactly from TABLE SCHEMAS — never a
  description, a row count, or a phrase. Choose the frame the step asserts
  *about*, which is one whose `grain` equals its own name: for a test over
  invoices joined to purchase orders that is `invoice_data`, not
  `invoice_data_po_data_joined`. The code may read whichever frame carries the
  columns; the declared population is what the conclusion is about.

`title` names the test as an auditor would list it. `objective` states what the
test determines, in one sentence, without asserting its outcome.

{JSON_RULES}

Return exactly one of:
{{"promote": true, "rcm_id": "...", "title": "...", "objective": "...",
  "step": {{"label": "...", "instruction": "...", "population": "...",
           "code": "..."}}}}
{{"promote": false, "reason": "..."}}"""


def _promotion_subject(request: WorkerRequest) -> Mapping[str, Any]:
    subject = _resolved_item(request, PROMOTION_SUBJECT_SOURCE_ID)
    if not isinstance(subject, Mapping):
        raise WorkerContractError(
            "Analysis promotion requires exactly one supplied procedure."
        )
    return subject


def _promotion_rows(request: WorkerRequest) -> list[object]:
    return _source_items(request, PROMOTION_RCM_SOURCE_ID)


def _promotion_response_schema(response: str) -> Mapping[str, Any]:
    """Structural contract for one fitting decision."""
    payload = decode_json_response(response)
    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError(
            "the response must be a JSON object"
        )
    if "promote" not in payload or not isinstance(payload.get("promote"), bool):
        raise WorkerResponseValidationError(
            "the response must carry a boolean `promote`"
        )
    if not payload.get("promote"):
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise WorkerResponseValidationError(
                "a declined procedure must carry a `reason` saying what it "
                "establishes and why that is not a control exception"
            )
        return {"promote": False, "reason": reason}
    errors = []
    fields = {
        key: str(payload.get(key) or "").strip()
        for key in ("rcm_id", "title", "objective")
    }
    for key, value in fields.items():
        if not value:
            errors.append(f"a promoted procedure requires `{key}`")
    step = payload.get("step")
    if not isinstance(step, Mapping):
        errors.append("a promoted procedure requires a `step` object")
        step = {}
    step_fields = {
        key: str(step.get(key) or "").strip()
        for key in ("label", "instruction", "population", "code")
    }
    for key, value in step_fields.items():
        if not value:
            errors.append(f"a promoted procedure requires `step.{key}`")
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"promote": True, **fields, "step": step_fields}


def validate_promotion_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Check the fit against the material the turn was actually given.

    Three things the supplied bundle can decide and the model cannot be trusted
    to: that the chosen row exists, that a carried procedure was carried rather
    than rewritten, and that the code is a valid sandbox program. The last is
    checked here rather than left to the commit because a promotion that fails
    at execution has already consumed its disposition — the analysis would read
    as answered while nothing tests it.
    """
    if not proposal.get("promote"):
        return proposal
    errors: list[str] = []
    rows = {
        str(row.get("id") or "")
        for row in _promotion_rows(request)
        if isinstance(row, Mapping)
    }
    rcm_id = str(proposal.get("rcm_id") or "")
    if rcm_id not in rows:
        errors.append(
            f"rcm_id '{rcm_id}' is not one of the supplied RCM rows; choose one "
            f"of {', '.join(sorted(rows)) or 'the supplied rows'}"
        )
    step = proposal.get("step") or {}
    # The anchor rule, checked here rather than at commit. ``population`` names
    # what the step asserts *about*, so it has to be a supplied frame whose
    # grain is itself: a joined frame carries its left side's grain, and a
    # conclusion drawn over it is stated about a population that does not
    # exist. Both failures this catches were observed — a prose description
    # ("Invoice population (118 invoices)") and a joined frame — and both
    # surfaced at commit, after the repair budget was already spent.
    grains: dict[str, str] = {}
    for table in _source_items(request, PROMOTION_TABLE_SOURCE_ID):
        if not isinstance(table, Mapping):
            continue
        name = str(table.get("table") or "").strip()
        if name:
            grains[name] = str(table.get("grain") or name).strip() or name
    population = str(step.get("population") or "").strip()
    if grains and population not in grains:
        errors.append(
            f"step.population '{population}' is not a supplied frame; use one "
            f"of {', '.join(sorted(name for name, grain in grains.items() if grain == name))} "
            "exactly as spelled in TABLE SCHEMAS"
        )
    elif grains and grains[population] != population:
        errors.append(
            f"step.population '{population}' is a joined frame carrying the "
            f"grain of '{grains[population]}'; a step asserts about a "
            f"population, so declare '{grains[population]}' and read the "
            "joined frame in the code"
        )
    code = str(step.get("code") or "")
    carried = str(_promotion_subject(request).get("code") or "").strip()
    # A procedure that already ran is evidence. A rewritten one is a different
    # procedure with the first one's exception count attached to it, which is
    # the single most misleading artifact this capability could produce.
    if carried and code.strip() != carried:
        errors.append(
            "the supplied procedure carries its own code and must be promoted "
            "unchanged; return step.code exactly as supplied"
        )
    if "result" not in code:
        errors.append("step.code must assign the flagged rows to `result`")
    try:
        sandbox.validate(code)
    except Exception as error:  # noqa: BLE001 - surfaced as a repairable error
        errors.append(f"step.code is not a valid sandbox program: {error}")
    else:
        # Safe is not the same as runnable, and only the second is worth
        # anything here. Four promoted steps passed the static check, referred
        # to a bare ``frame`` variable that the sandbox never defines, and
        # failed at execution — by which point the analysis was already
        # recorded as answered and the test sat in the workspace reporting no
        # exceptions over a procedure that had found four. Running against
        # zero-row frames resolves every name and column without reading a row.
        schemas = {
            str(table.get("table") or ""): {
                str(column.get("name") or ""): str(column.get("dtype") or "")
                for column in table.get("columns") or []
                if isinstance(column, Mapping)
            }
            for table in _source_items(request, PROMOTION_TABLE_SOURCE_ID)
            if isinstance(table, Mapping) and table.get("table")
        }
        if schemas:
            try:
                sandbox.run(code, sandbox.empty_schema_frames(schemas))
            except sandbox.SandboxError as error:
                errors.append(
                    f"step.code cannot run against the supplied table schemas: "
                    f"{error}. Read a frame as tables['exact_name'] or by its "
                    "bare table name; no other frame variable exists."
                )
    if errors:
        raise WorkerResponseValidationError(errors)
    return proposal


def run_analysis_promotion_worker(
    request: WorkerRequest, gateway: ModelGateway, attempt: WorkerAttempt
) -> str:
    """Transform one candidate procedure and the matrix into one fitting turn."""
    subject = _promotion_subject(request)
    prompt = json.dumps(
        {
            "PROCEDURE": subject,
            "RCM ROWS": _promotion_rows(request),
            "TABLE SCHEMAS": _source_items(request, PROMOTION_TABLE_SOURCE_ID),
            "REQUIRED OUTPUT": (
                "Return one JSON object: a promotion naming an exact rcm_id, or "
                "a decline carrying a reason."
            ),
        },
        indent=1,
        ensure_ascii=False,
    )
    if attempt.is_repair:
        prompt += "\nRepair the prior response: " + "; ".join(
            attempt.validation_errors
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "analysis_promotion",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(
        ANALYSIS_PROMOTION_SYSTEM, prompt, activity, attempt=attempt.number
    )


ANALYSIS_PROMOTION_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_PROMOTION_WORKER_ID,
    prompt_hash=_sha256_text(ANALYSIS_PROMOTION_SYSTEM),
    response_schema=WorkerResponseSchema(
        schema_id="analysis.promotion.response",
        schema_hash=_sha256_text(
            "analysis-promotion-response:v1:promote-or-decline"
        ),
        validator=_promotion_response_schema,
    ),
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the fit against the supplied RCM rows, schemas, and the "
            "procedure's own code."
        ),
    ),
    implementation=run_analysis_promotion_worker,
    semantic_validator=validate_promotion_proposal,
)
WORKERS.register(ANALYSIS_PROMOTION_WORKER)
