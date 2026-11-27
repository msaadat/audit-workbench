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

import ast
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from itertools import permutations
from typing import Any

from ... import analytics, sandbox
# The memo's embed grammar is shared with the context adapter that hands the
# memo to the APM. It lives outside ``agent/`` because a worker may not import
# the context package and vice versa; the module has no dependencies of its own.
from ...analysis_memo import EMBED_FENCE, EMBED_KINDS, parse_embeds
from ...field_names import subject_tokens
from ...text import counted, verb
from ..analysis_identity import (
    _plain_json,
    analysis_semantic_id,
    python_spec_scope,
    spec_scope,
)
from ..prompts import JSON_RULES, LANGUAGE_RULES
from ..runtime.model_gateway import ModelGateway
from .model import (
    AUDITOR_INSTRUCTION_RULE,
    AUDITOR_INSTRUCTION_SOURCE_ID,
    auditor_instruction,
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
needs columns from both sides; otherwise reject. The decisions object is keyed
by the candidate's own reference, so answer each reference exactly once, under
its own key. Do not retain a relationship
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
""" + LANGUAGE_RULES
ANALYSIS_DEFINITION_SYSTEM = f"""[agent:analysis_definitions]
Write, as Polars code, every analysis the register asked for — all of them, in
one response. WORK ITEMS lists them: each has a `ref`, a `kind`, the `frame` it
runs on, the `columns` it reads, and what it should establish.

- kind `assert` is something that should hold. Its code returns, as `result`,
  the rows where it does not — every returned row a potential exception — with
  `outcome_policy` `exception_rows`. Select the identifying columns and the
  columns that show why the row fails, not every column.
- kind `explore` is a question about the population. Its code returns a summary
  that answers it — grouped and aggregated, a few dozen rows at most, never the
  population's rows — with `outcome_policy` `informational`, and a `chart`
  naming how to draw it (`bar` or `line` with the `x` column and the `y`
  columns, or `table`).

FRAMES describes each frame: its columns and types, the table each column comes
from (`column_origins`), and the table whose rows the frame's rows are
(`frame_root`). On a joined frame the joining is done: each row already carries
its counterpart's columns, and an unmatched row carries nulls for them. Never
re-join a table the frame already holds. Distinguish rows the check cannot
evaluate (a null on either side) from rows it evaluated.

Code reads a frame as `tables["name"]` or by its bare name, assigns a DataFrame
to `result`, and nothing else: no imports, no files, no network. Use
`series.len()`, not `series.height`; `.dt.total_days()` for a duration; and
select or suffix columns before a join that would collide. Inside `.agg(...)`
name columns as `pl.col("x")`, never `df["x"]`: `df["x"]` is the whole column,
so `df["x"].sum()` is one number for every group rather than one per group. VALUE DOMAINS gives
the exact spelling of every value a low-cardinality column holds; never name a
value it does not list. You are never shown a row and must not invent values,
counts or relationships.

`note` is one sentence on what the result means in terms of the business events
the columns record — "a GRN records receipt, so it cannot predate the order it
receives", not "checks the dates". Do not repeat an analysis CURRENT ANALYSES
already holds for the same frame and question.

Answer every work item exactly once: with an analysis, or in `declined` with the
reason it cannot be written against these columns. Every analysis is run against
the real frames before it is saved, and one that fails to run is dropped.
{JSON_RULES} {LANGUAGE_RULES}""" + f"\n\n{AUDITOR_INSTRUCTION_RULE}"

AUTHORING_ITEMS_SOURCE_ID = "work_items"
AUTHORING_FRAMES_SOURCE_ID = "frame_schemas"
AUTHORING_SUBMISSION_TOOL = "submit_authored_analyses"

# Carried in the validation error when a frame has nothing of its own left to
# contribute — every proposal either repeats a computation its join family
# already holds, or reads only one of the tables the frame was built from. That
# is a settled outcome, not a contract violation, so the binder matches on this
# to settle the unit as skipped once the repair turn has had its chance.
NOTHING_NEW_TO_ANALYSE = "nothing_new_to_analyse"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


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
    """Normalize the decision set, which is keyed by candidate reference.

    An array of ``{ref, decision, …}`` objects can express the same reference
    twice, and a model enumerating sixteen candidates does exactly that: one run
    returned sixteen decisions covering six distinct references, four of them
    repeated four times, and the whole gate failed. Keying the object by
    reference makes that unrepresentable rather than rejected — the same move
    ``value_domains`` makes for a value a column does not hold.

    The array shape is still accepted. A provider that ignores the schema and
    sends one is answering the question, and the duplicate rule below is what it
    is for; refusing to parse it would spend the repair turn on transport.
    """
    payload = _json_object(response)
    decisions = payload.get("decisions")
    if isinstance(decisions, Mapping):
        return {
            "decisions": [
                {**value, "ref": str(ref)}
                for ref, value in decisions.items()
                if isinstance(value, Mapping)
            ]
        }
    if not isinstance(decisions, list):
        raise WorkerResponseValidationError(
            "decisions must be an object keyed by candidate reference."
        )
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
        # No ``ref`` field: the candidate's reference is the key this branch is
        # filed under, so a decision cannot name a different candidate than the
        # one it answers for, and cannot answer for one twice.
        properties: dict[str, Any] = {
            "decision": {"type": "string", "enum": [decision]},
            "rationale": {"type": "string", "minLength": 1},
        }
        required = ["decision", "rationale"]
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
            # Keyed by candidate reference, and every reference is required.
            # Three failure modes stop being possible rather than being caught:
            # a decision for a candidate that does not exist, a candidate with
            # no decision, and — the one that actually broke a run — the same
            # candidate decided several times.
            "decisions": {
                "type": "object",
                "properties": {
                    ref: {"oneOf": [branch("retain"), branch("reject")]}
                    for ref in refs
                },
                "required": list(refs),
                "additionalProperties": False,
            },
        }, "required": ["decisions"], "additionalProperties": False},
    }}


def _forced_tool_arguments(message: object, tool_name: str) -> str:
    """The arguments of a forced tool call, with a text fallback.

    **Several matching calls are not an error.** Every one of these workers
    forces a single tool and asks for it once; a model that submits twice has
    repeated itself, not contradicted itself, and the first complete submission
    is the answer. This used to require *exactly* one match and fall through to
    ``content`` otherwise — which on a tool-call response is empty or
    whitespace, so a duplicate submission surfaced as "the response is not a
    valid JSON object: Expecting value at character 0", an error naming nothing
    that could be acted on.

    Measured: one run's memo repair returned two tool calls carrying 11,253 and
    4,708 characters, both parsing cleanly with every required key. The run
    discarded both, read three tab characters instead, and failed the stage. It
    lost its memo while holding two good ones.
    """
    if not isinstance(message, Mapping):
        return str(message or "")
    matches = [
        item
        for item in message.get("tool_calls") or []
        if isinstance(item, Mapping)
        and isinstance(item.get("function"), Mapping)
        and item["function"].get("name") == tool_name
    ]
    for match in matches:
        arguments = match["function"].get("arguments")
        if isinstance(arguments, str) and arguments.strip():
            return arguments
        if isinstance(arguments, Mapping):
            return json.dumps(arguments, ensure_ascii=False)
    return str(message.get("content") or "")


def _join_utility_submission_response(message: object) -> str:
    return _forced_tool_arguments(message, JOIN_UTILITY_SUBMISSION_TOOL)


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
    }, separators=(",", ":"), ensure_ascii=False)
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
                    "supports it. Resubmit the complete decision set, one entry "
                    "per candidate reference."
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
        schema_hash=_sha256_text("join-utility-response:v2:decisions-keyed-by-ref"),
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


def _column_names(schema: Mapping[str, Any]) -> set[str]:
    return {
        str(column.get("name"))
        for column in schema.get("columns") or []
        if str(column.get("name") or "").strip()
    }


def _whole_column_aggregations(code: str) -> list[str]:
    """Columns named as a frame's Series inside ``.agg(...)``.

    ``df["x"]`` is the whole column, so inside a group-by's aggregation its
    reduction is one number for every group — and ``.alias`` on that number
    raises. Measured on the expenses pack: four of eleven authored analyses
    were written this way and every one was dropped at execution, where no
    repair could reach it. Said here it costs a repair turn instead.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "agg"):
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            for inner in ast.walk(argument):
                if (
                    isinstance(inner, ast.Subscript)
                    and isinstance(inner.slice, ast.Constant)
                    and isinstance(inner.slice.value, str)
                ):
                    column = inner.slice.value
                    message = (
                        f'names column "{column}" as a frame\'s Series inside .agg(); '
                        f'use pl.col("{column}") so it is aggregated per group'
                    )
                    if message not in found:
                        found.append(message)
    return found


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
    errors.extend(f"{label} {message}" for message in _whole_column_aggregations(code))
    referenced = set(re.findall(r"tables\[['\"]([^'\"]+)['\"]\]", code))
    unknown = sorted(referenced - frames)
    if unknown:
        errors.append(
            f"{label} reads table '{unknown[0]}' which was not supplied as context"
        )
    return {"code": code}, errors


# --------------------------------------------------------------------------- #
# analysis.definitions — the authoring turn
# --------------------------------------------------------------------------- #
#: How a chart is drawn from an explore answer. ``table`` is always available.
CHART_TYPES = ("bar", "line", "table")
OUTCOME_POLICIES = ("exception_rows", "informational")


def _authoring_items(request: WorkerRequest) -> dict[str, Mapping[str, Any]]:
    return {
        str(entry.get("ref")): entry
        for entry in (_plain_json(item) for item in _source_items(request, AUTHORING_ITEMS_SOURCE_ID))
        if isinstance(entry, Mapping) and entry.get("ref")
    }


def _authoring_frames(request: WorkerRequest) -> dict[str, Mapping[str, Any]]:
    return {
        str(entry.get("table")): entry
        for entry in (_plain_json(item) for item in _source_items(request, AUTHORING_FRAMES_SOURCE_ID))
        if isinstance(entry, Mapping) and entry.get("table")
    }


def _authoring_submission_tool(request: WorkerRequest) -> dict[str, Any]:
    refs = sorted(_authoring_items(request))
    frames = sorted(_authoring_frames(request))
    if not refs:
        raise WorkerContractError("The authoring context supplied no work items.")
    return {
        "type": "function",
        "function": {
            "name": AUTHORING_SUBMISSION_TOOL,
            "description": "Submit one Polars analysis per work item, or decline it with a reason.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["analyses", "declined"],
                "properties": {
                    "analyses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["ref", "frame", "title", "note", "code", "outcome_policy"],
                            "properties": {
                                "ref": {"type": "string", "enum": refs},
                                "frame": {"type": "string", "enum": frames},
                                "title": {"type": "string", "minLength": 3},
                                "note": {"type": "string", "minLength": 10},
                                "code": {"type": "string", "minLength": 10},
                                "outcome_policy": {"type": "string", "enum": list(OUTCOME_POLICIES)},
                                "chart": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["type"],
                                    "properties": {
                                        "type": {"type": "string", "enum": list(CHART_TYPES)},
                                        "x": {"type": "string"},
                                        "y": {"type": "array", "items": {"type": "string"}},
                                    },
                                },
                            },
                        },
                    },
                    "declined": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["ref", "reason"],
                            "properties": {
                                "ref": {"type": "string", "enum": refs},
                                "reason": {"type": "string", "minLength": 15},
                            },
                        },
                    },
                },
            },
        },
    }


def _authoring_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    out: dict[str, Any] = {}
    for section in ("analyses", "declined"):
        value = payload.get(section)
        if value is None:
            value = []
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise WorkerResponseValidationError(f"`{section}` must be an array of objects")
        out[section] = value
    return out


def validate_authoring_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Hold the authoring contract the supplied work items and frames can decide.

    Every work item is answered exactly once: with code, or with a reason it
    cannot be written against these columns. The code is checked statically
    here — it is run against the real frames by the executor, which can reach
    them — and a check on a joined frame that reads only the columns of the
    table the frame is built on is set aside as belonging on that table.
    """
    items = _authoring_items(request)
    frames = _authoring_frames(request)
    errors: list[str] = []
    answered: dict[str, str] = {}
    accepted: list[dict[str, Any]] = []
    declined: list[str] = []

    for index, entry in enumerate(proposal.get("declined") or ()):
        ref = str(entry.get("ref") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        if ref not in items:
            errors.append(f"declined[{index}] names no supplied work item: '{ref}'")
            continue
        if not reason:
            errors.append(f"declined[{index}] declines {ref} with no reason")
            continue
        if ref in answered:
            errors.append(f"{ref} is answered twice")
            continue
        answered[ref] = "declined"
        declined.append(f"{ref}: {reason}")

    for index, entry in enumerate(proposal.get("analyses") or ()):
        label = f"analyses[{index}]"
        ref = str(entry.get("ref") or "").strip()
        frame = str(entry.get("frame") or "").strip()
        if ref not in items:
            errors.append(f"{label} names no supplied work item: '{ref}'")
            continue
        if ref in answered:
            errors.append(f"{ref} is answered twice")
            continue
        if frame not in frames:
            errors.append(f"{label} names no supplied frame: '{frame}'")
            continue
        title = str(entry.get("title") or "").strip()
        note = str(entry.get("note") or "").strip()
        if not title or not note:
            errors.append(f"{label} needs a title and a note saying what its result means")
            continue
        spec, spec_errors = _validate_python_spec({"code": entry.get("code")}, set(frames), label)
        if spec_errors:
            errors.extend(spec_errors)
            continue
        kind = str(items[ref].get("kind") or "assert")
        policy = str(entry.get("outcome_policy") or "")
        if policy not in OUTCOME_POLICIES:
            policy = "exception_rows" if kind == "assert" else "informational"
        if kind == "explore" and policy == "exception_rows":
            errors.append(
                f"{label} answers an explore question; its result describes the "
                "population and is `informational`, not rows of exceptions"
            )
            continue
        schema = frames[frame]
        origins = {str(k): str(v) for k, v in (schema.get("column_origins") or {}).items()}
        root = str(schema.get("frame_root") or frame)
        if kind == "assert" and len(set(origins.values())) > 1:
            # The rule the per-frame turn enforced, judged by population: a
            # check reading only the root's own columns belongs on the root.
            scope = python_spec_scope(spec, origins)
            if scope and not (scope - {root}):
                answered[ref] = "declined"
                declined.append(
                    f"{ref}: reads only the columns of {root}, which this frame is "
                    "built on, and belongs on that table"
                )
                continue
        definition: dict[str, Any] = {
            "ref": ref,
            "frame": frame,
            "title": title,
            "kind": "python",
            "spec": spec,
            "note": note,
            "outcome_policy": {"mode": policy},
        }
        chart = entry.get("chart")
        if isinstance(chart, Mapping) and str(chart.get("type") or "") in CHART_TYPES:
            definition["viz"] = {
                "type": str(chart["type"]),
                **({"x": str(chart["x"])} if chart.get("x") else {}),
                **({"y": [str(c) for c in chart.get("y") or []]} if chart.get("y") else {}),
            }
        answered[ref] = "analysis"
        accepted.append(definition)

    missing = sorted(set(items) - set(answered))
    if missing:
        errors.append(
            f"every work item is answered once, with code or a declined reason; "
            f"{', '.join(missing)} {verb(len(missing), 'has', 'have')} no answer"
        )
    if errors:
        # The items that passed stand on their own: should the repair never
        # arrive, they are committed and the rest are reported as not written.
        raise WorkerResponseValidationError(
            errors,
            partial=(
                {"analyses": accepted, "declined": [*declined, *(f"not written: {e}" for e in errors)]}
                if accepted
                else None
            ),
        )
    if not accepted:
        raise WorkerResponseValidationError(
            f"{NOTHING_NEW_TO_ANALYSE}: every work item was declined — "
            + "; ".join(declined)
        )
    return {"analyses": accepted, **({"declined": declined} if declined else {})}


def _authoring_submission_response(message: object) -> str:
    return _forced_tool_arguments(message, AUTHORING_SUBMISSION_TOOL)


def run_analysis_authoring_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """One turn writing every work item the register placed, as code."""
    tool = _authoring_submission_tool(request)
    user = json.dumps(
        {
            **(
                {"AUDITOR INSTRUCTION": auditor_instruction(request)}
                if auditor_instruction(request)
                else {}
            ),
            "WORK ITEMS": list(_authoring_items(request).values()),
            "FRAMES": list(_authoring_frames(request).values()),
            "RESOLVED CONTEXT": _model_facing_context(
                request,
                AUTHORING_ITEMS_SOURCE_ID,
                AUTHORING_FRAMES_SOURCE_ID,
                AUDITOR_INSTRUCTION_SOURCE_ID,
            ),
        },
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + f". Call {AUTHORING_SUBMISSION_TOOL} once, answering every work item."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "analysis_authoring",
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
        tool_choice={"type": "function", "function": {"name": AUTHORING_SUBMISSION_TOOL}},
        return_message=True,
    )
    return _authoring_submission_response(message)


ANALYSIS_DEFINITION_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.definitions.response",
    schema_hash=_sha256_text("analysis-authoring-response:v1:analyses-declined-code-only"),
    validator=_authoring_response_schema,
)
ANALYSIS_DEFINITION_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_DEFINITION_WORKER_ID,
    prompt_hash=_sha256_text(ANALYSIS_DEFINITION_SYSTEM),
    response_schema=ANALYSIS_DEFINITION_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text("Answer every work item once, with safe Polars or a reason."),
    ),
    implementation=run_analysis_authoring_worker,
    semantic_validator=validate_authoring_proposal,
)

WORKERS.register(ANALYSIS_DEFINITION_WORKER)

ANALYSIS_READING_WORKER_ID = "analysis.reading"
READING_FRAMES_SOURCE_ID = "frame_map"
READING_NOMINATIONS_SOURCE_ID = "nominations"
READING_HYPOTHESIS_SOURCE_ID = "join_hypotheses"
READING_DOMAIN_SOURCE_ID = "value_domains"
READING_REGISTRY_SOURCE_ID = "analytics_registry"
READING_PROFILE_SOURCE_ID = "population_profile"
READING_REFERENCE_SOURCE_ID = "reference_tables"
READING_SUBMISSION_TOOL = "submit_assertion_register"
#: Questions about the population the profile did not already answer. Each is a
#: piece of code the authoring turn writes, so this bounds that turn's work.
MAX_EXPLORE_QUESTIONS = 6
#: Facts about the data that change how it should be tested. A handful is the
#: whole of what a memo can carry.
MAX_CHARACTERIZED = 8

# How many assertions one reading turn may add. The kept nominations are not
# bounded here — they are already measured and already deduplicated — but an
# authored assertion costs a definition turn to write a spec for, so this is the
# real fan-out control for the stage below. Twelve is more than the four runs on
# record ever produced worth keeping.
MAX_AUTHORED_ASSERTIONS = 12

ANALYSIS_READING_SYSTEM = f"""[agent:analysis_reading]
You are reading one engagement's entire data map at once and settling what it
should be tested for. You receive every frame's columns, every relationship that
was diagnosed — including the ones no join was built from — the complete value
vocabulary of every low-cardinality column, and NOMINATIONS: procedures that
have already been run against this data, each with the counts it produced.

You are never shown a table row and must not invent values, counts,
relationships, or controls that are not in the supplied context.

**The nominations are settled.** Each is a valid spec and the arithmetic is
done. They are already saved unless you say otherwise, so you do not need to
list them to keep them, and listing one buys nothing but a better name for it.
Spend the turn on the five things that are genuinely yours:

1. **add** — what should be true that no nomination measures. This is the whole
   reason a turn reads every frame together. A comparison whose two columns sit
   on different frames; an authority limit that has to be reached through two
   joins; a control the columns plainly imply and no single measurement states.
   Name the frame it should be tested on and the columns that state it, both
   from the supplied map. Write the assertion as something that should hold —
   "an invoice is never approved by a person whose title carries a lower limit
   than its amount" — not as an instruction to run a test.

2. **decline** — a nomination that should not be a procedure at all, with the
   reason. Use it where the condition flagged could not indicate a control
   failing: two columns that were never in the relationship the spec asks for,
   or a minority state that is ordinary operations rather than an exception.
   A decline needs an argument, not a preference, and it is recorded against
   your name. Declining nothing is a perfectly good answer.

   One exception you must not mistake for a mis-aimed test. A nomination
   carrying ``lookup_is_arbitrary`` has been reconciled against *every*
   imported master and matched none of them. The table named in its params is
   one of those masters, picked arbitrarily, because nothing in the data
   ranks them. A column whose values exist in no master anywhere is one of the
   strongest reconciliation failures there is; declining it because the named
   master is the wrong domain rejects the finding on the strength of a detail
   the measurement explicitly disclaims.

3. **characterize** — what kind of data this is, read off POPULATION PROFILE:
   facts that change which tests can prove anything, each with what it means
   for testing. A figure equal to the step before it on every row makes a test
   comparing them unable to fail. Prices that never vary between suppliers
   leave only a price that breaks the list to find. Dates spread over weekends
   as evenly as weekdays were keyed, not stamped, so day counts measure what
   was typed. Identifiers carried from one record to the next show which
   records were created apart. State only what a supplied figure shows, and
   give the figure.

4. **explore** — what the profile does not answer and an auditor would ask of
   this population: a breakdown the profile did not cut, a duration it did not
   measure, a figure derived within a group. A question, not an assertion —
   its answer describes the population rather than flagging rows. Name the
   frame and the columns it reads. Leave this empty when the profile already
   answers what matters; a question it already answers costs a turn to restate.

5. **unanswerable** — what an auditor would want to test and this data cannot
   answer, because no field anywhere records it. You are the only stage that can
   see this: it is a statement about the whole column inventory, and no
   frame-scoped turn holds enough of the engagement to make it. A control the
   file gives no column for is a finding, and it will never appear as a result.

Read the nominations against POPULATION PROFILE. A decline argues that a flagged
condition is ordinary operations, and the profile is where ordinary is shown: a
requisition with no purchase order is mid-cycle only if others like it were
ordered later, and is stale if every other one was ordered within days. A
decline the profile contradicts is not a decline.

REFERENCE TABLES, when present, are the engagement's small lookups shown whole:
a policy list, a limit matrix. A threshold stated there — a daily cap, a
deadline in days, a limit per role — is something a population can be tested
against, so **add** the assertion with the number in it and name the reference
row it comes from: "no domestic meal line exceeds the 4,000 per employee per
day of POL-02". Read the unit each row states (per line, per claim, per
employee per day) and write the assertion at that grain. A row stating no
number is not a test.

You may also **keep** a nomination with a better title and a note saying what
its flagged condition would mean in terms of the business events the columns
record. Do this for the ones that matter most; the rest keep the name the
measurement derives.

FRAME MAP describes base schemas once. A join descriptor carries all left
columns plus right columns except its coalesced right_on keys; apply renamed
to colliding right columns. These are temporary local alignments unless already
saved by the auditor. Left joins retain unmatched rows with null counterpart
attributes. Place each assertion on the exact population and role route it needs.
Descriptors marked selectable=false supply dependency columns only, not targets.

Do not restate a nomination as an added assertion. It is already in the
register, and adding it again asks a model to rewrite a spec that was measured
exactly.

**When AUDITOR INSTRUCTION is present**, the auditor has named what they want
tested, and this is the only turn that decides what gets tested. Account for
every test it names in `instruction_response`, one entry each, quoting what was
asked in `asked`:
- `added`: you add an assertion for it in `add` (repeat the assertion in
  `reference`);
- `nomination`: a supplied nomination already measures it (its ref in
  `reference`);
- `unanswerable`: no field in this data records it (the reason in `reference`;
  it is recorded with the other unanswerable questions).
A test the auditor named is never silently left out: the memo says what was
asked and what was tested, from this answer.
{JSON_RULES} {LANGUAGE_RULES}""" + f"\n\n{AUDITOR_INSTRUCTION_RULE}"


def _reading_frames(request: WorkerRequest) -> dict[str, set[str]]:
    """Supplied frame names mapped to the columns each actually has."""
    entries = {str(item.get("table") or ""): _plain_json(item)
               for item in _source_items(request, READING_FRAMES_SOURCE_ID)
               if isinstance(item, Mapping) and item.get("table")}
    frames = {}
    def columns(name, seen=frozenset()):
        if name in frames:
            return frames[name]
        if name in seen or name not in entries:
            return None
        schema = entries[name]
        join = schema.get("join")
        if not isinstance(join, Mapping):
            frames[name] = _column_names(schema)
            return frames[name]
        left = columns(join.get("left"), seen | {name})
        right = columns(join.get("right"), seen | {name})
        if left is None or right is None:
            return None  # A truncated source cannot authorize an assertion.
        removed = set(join.get("right_on") or ()) if join.get("how") != "cross" else set()
        renames = schema.get("renamed") or {}
        frames[name] = left | {renames.get(c, c) for c in right - removed}
        return frames[name]
    for name in entries:
        columns(name)
    return {name: cols for name, cols in frames.items() if entries[name].get("selectable", True)}


def _reading_nominations(request: WorkerRequest) -> dict[str, Mapping[str, Any]]:
    """Supplied nominations by the reference a decision names them with."""
    found: dict[str, Mapping[str, Any]] = {}
    for item in _source_items(request, READING_NOMINATIONS_SOURCE_ID):
        entry = _plain_json(item)
        if not isinstance(entry, Mapping):
            continue
        ref = str(entry.get("ref") or "").strip()
        if ref:
            found[ref] = entry
    return found


def _reading_submission_tool(request: WorkerRequest) -> dict[str, Any]:
    """Build the register schema from the map this turn was actually supplied.

    Every reference and every frame name is an enum drawn from the bundle, so a
    decision about a nomination that does not exist, or an assertion placed on a
    frame that was never built, is unrepresentable rather than merely wrong.
    """
    refs = sorted(_reading_nominations(request))
    frames = sorted(_reading_frames(request))
    ref_schema: dict[str, Any] = {"type": "string"}
    if refs:
        ref_schema = {"type": "string", "enum": refs}
    frame_schema: dict[str, Any] = {"type": "string"}
    if frames:
        frame_schema = {"type": "string", "enum": frames}
    tool = {
        "type": "function",
        "function": {
            "name": READING_SUBMISSION_TOOL,
            "description": (
                "Settle the assertion register: keep nominations under better "
                "names, add what no nomination measures, decline with a reason, "
                "and record what this data cannot answer."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "keep": {
                        "type": "array",
                        "minItems": 0,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["ref", "title", "note"],
                            "properties": {
                                "ref": ref_schema,
                                "title": {"type": "string", "minLength": 3},
                                "note": {"type": "string", "minLength": 10},
                            },
                        },
                    },
                    "add": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": MAX_AUTHORED_ASSERTIONS,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["frame", "columns", "assertion", "why"],
                            "properties": {
                                "frame": frame_schema,
                                "columns": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                                "assertion": {"type": "string", "minLength": 10},
                                "why": {"type": "string", "minLength": 10},
                            },
                        },
                    },
                    "decline": {
                        "type": "array",
                        "minItems": 0,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["ref", "reason"],
                            "properties": {
                                "ref": ref_schema,
                                "reason": {"type": "string", "minLength": 20},
                            },
                        },
                    },
                    "unanswerable": {
                        "type": "array",
                        "minItems": 0,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["question", "why"],
                            "properties": {
                                "question": {"type": "string", "minLength": 10},
                                "why": {"type": "string", "minLength": 10},
                            },
                        },
                    },
                    "characterize": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": MAX_CHARACTERIZED,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["fact", "implication"],
                            "properties": {
                                "fact": {"type": "string", "minLength": 10},
                                "implication": {"type": "string", "minLength": 10},
                            },
                        },
                    },
                    "explore": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": MAX_EXPLORE_QUESTIONS,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["frame", "columns", "question", "why"],
                            "properties": {
                                "frame": frame_schema,
                                "columns": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                                "question": {"type": "string", "minLength": 10},
                                "why": {"type": "string", "minLength": 10},
                            },
                        },
                    },
                },
                "required": ["keep", "add", "decline", "unanswerable", "characterize", "explore"],
            },
        },
    }
    if auditor_instruction(request):
        # The auditor named what they want tested: the answer to that is part
        # of the register, required, so a named test cannot go unmentioned.
        parameters = tool["function"]["parameters"]
        parameters["properties"]["instruction_response"] = {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["asked", "answer", "reference"],
                "properties": {
                    "asked": {"type": "string", "minLength": 3},
                    "answer": {"type": "string", "enum": list(INSTRUCTION_ANSWERS)},
                    "reference": {"type": "string", "minLength": 3},
                },
            },
        }
        parameters["required"] = [*parameters["required"], "instruction_response"]
    return tool


#: How the register answers each test the auditor's instruction names.
INSTRUCTION_ANSWERS = ("added", "nomination", "unanswerable")


def _reading_submission_response(message: object) -> str:
    return _forced_tool_arguments(message, READING_SUBMISSION_TOOL)


_READING_SECTIONS = ("keep", "add", "decline", "unanswerable", "characterize", "explore")


def _reading_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    settled: dict[str, Any] = {}
    for section in _READING_SECTIONS:
        value = payload.get(section)
        if value is None:
            # Every section is optional in substance: a turn that adds nothing
            # and declines nothing has still read the map and kept the floor,
            # which is a complete answer and must not cost a repair turn.
            value = []
        if not isinstance(value, list):
            raise WorkerResponseValidationError(f"`{section}` must be an array")
        if any(not isinstance(item, dict) for item in value):
            raise WorkerResponseValidationError(
                f"every `{section}` entry must be an object"
            )
        settled[section] = value
    answers = payload.get("instruction_response")
    if answers is not None:
        if not isinstance(answers, list) or any(not isinstance(item, dict) for item in answers):
            raise WorkerResponseValidationError(
                "`instruction_response` must be an array of objects"
            )
        settled["instruction_response"] = answers
    return settled


def validate_reading_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Hold the register contract the supplied map can decide.

    The asymmetry between keeping and declining is the point and it is enforced
    here. A malformed *keep* costs the entry its better name and nothing else,
    because the nomination survives on the floor regardless. A malformed
    *decline* is rejected outright: subtracting from a measured set is the only
    thing this turn does that cannot be undone by a later stage, so it is the
    only thing held to a repair turn.
    """
    frames = _reading_frames(request)
    nominations = _reading_nominations(request)
    errors: list[str] = []

    decided: set[str] = set()
    declines: list[dict[str, Any]] = []
    for index, item in enumerate(proposal.get("decline") or ()):
        label = f"decline[{index}]"
        ref = str(item.get("ref") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if ref not in nominations:
            errors.append(f"{label} names no supplied nomination: '{ref}'")
            continue
        if not reason:
            errors.append(
                f"{label} declines {ref} with no reason; a nomination is only "
                "set aside with an argument for why its flagged condition "
                "could not indicate a control failing"
            )
            continue
        if ref in decided:
            errors.append(f"{label} decides {ref} a second time")
            continue
        decided.add(ref)
        declines.append({"ref": ref, "reason": reason})

    keeps: list[dict[str, Any]] = []
    for item in proposal.get("keep") or ():
        ref = str(item.get("ref") or "").strip()
        title = str(item.get("title") or "").strip()
        note = str(item.get("note") or "").strip()
        # Silently ignored rather than rejected: the floor already keeps this
        # entry under its derived name, so a bad keep loses a nicety and the
        # measurement is never at risk.
        if ref not in nominations or ref in decided or not title or not note:
            continue
        decided.add(ref)
        keeps.append({"ref": ref, "title": title, "note": note})

    def placed(section: str, index: int, item: Mapping[str, Any], statement: str) -> dict | None:
        label = f"{section}[{index}]"
        frame = str(item.get("frame") or "").strip()
        if frame not in frames:
            errors.append(f"{label} names no supplied frame: '{frame}'")
            return None
        columns = [str(value).strip() for value in item.get("columns") or ()]
        columns = [value for value in columns if value]
        unknown = [value for value in columns if value not in frames[frame]]
        if unknown:
            errors.append(
                f"{label} names {', '.join(unknown)}, which "
                f"{verb(len(unknown), 'is', 'are')} not a column of '{frame}'"
            )
            return None
        if not columns:
            errors.append(f"{label} names no column of '{frame}'")
            return None
        text = str(item.get(statement) or "").strip()
        why = str(item.get("why") or "").strip()
        if not text or not why:
            errors.append(f"{label} is missing the {statement} or the reason it matters")
            return None
        return {"frame": frame, "columns": columns, statement: text, "why": why}

    explored = [
        entry
        for index, item in enumerate(proposal.get("explore") or ())
        if (entry := placed("explore", index, item, "question")) is not None
    ]
    if len(explored) > MAX_EXPLORE_QUESTIONS:
        errors.append(f"explore at most {MAX_EXPLORE_QUESTIONS} questions; {len(explored)} were written")
    characterized = [
        {"fact": str(item.get("fact") or "").strip(), "implication": str(item.get("implication") or "").strip()}
        for item in proposal.get("characterize") or ()
        if str(item.get("fact") or "").strip()
    ][:MAX_CHARACTERIZED]

    authored: list[dict[str, Any]] = []
    for index, item in enumerate(proposal.get("add") or ()):
        label = f"add[{index}]"
        frame = str(item.get("frame") or "").strip()
        if frame not in frames:
            errors.append(f"{label} names no supplied frame: '{frame}'")
            continue
        columns = [str(value).strip() for value in item.get("columns") or ()]
        columns = [value for value in columns if value]
        unknown = [value for value in columns if value not in frames[frame]]
        if unknown:
            errors.append(
                f"{label} names {', '.join(unknown)}, which "
                f"{verb(len(unknown), 'is', 'are')} not a column of '{frame}'"
            )
            continue
        if not columns:
            errors.append(f"{label} names no column of '{frame}'")
            continue
        assertion = str(item.get("assertion") or "").strip()
        why = str(item.get("why") or "").strip()
        if not assertion or not why:
            errors.append(
                f"{label} is missing the assertion or the reason it matters"
            )
            continue
        authored.append(
            {
                "frame": frame,
                "columns": columns,
                "assertion": assertion,
                "why": why,
            }
        )
    if len(authored) > MAX_AUTHORED_ASSERTIONS:
        errors.append(
            f"add at most {MAX_AUTHORED_ASSERTIONS} assertions; "
            f"{len(authored)} were written"
        )

    unanswerable = [
        {
            "question": str(item.get("question") or "").strip(),
            "why": str(item.get("why") or "").strip(),
        }
        for item in proposal.get("unanswerable") or ()
        if str(item.get("question") or "").strip()
    ]

    answered: list[dict[str, str]] = []
    if auditor_instruction(request):
        # The one check this turn owes the auditor: every test they named is
        # added, pointed at, or said to be unanswerable. A reading silent on
        # the instruction is the stage-1 failure (B6), and costs one repair.
        responses = list(proposal.get("instruction_response") or ())
        if not responses:
            errors.append(
                "AUDITOR INSTRUCTION names tests to run; account for each in "
                "instruction_response as added, nomination or unanswerable"
            )
        for index, item in enumerate(responses):
            label = f"instruction_response[{index}]"
            asked = str(item.get("asked") or "").strip()
            answer = str(item.get("answer") or "").strip()
            reference = str(item.get("reference") or "").strip()
            if not asked or answer not in INSTRUCTION_ANSWERS or not reference:
                errors.append(
                    f"{label} needs what was asked, an answer of "
                    f"{', '.join(INSTRUCTION_ANSWERS)}, and its reference"
                )
                continue
            if answer == "nomination" and reference not in nominations:
                errors.append(
                    f"{label} points at '{reference}', which is not a supplied "
                    "nomination"
                )
                continue
            if answer == "added" and not authored:
                errors.append(
                    f"{label} says an assertion was added for '{asked}', but "
                    "`add` is empty"
                )
                continue
            if answer == "unanswerable" and not any(
                entry["question"] == asked for entry in unanswerable
            ):
                unanswerable.append({"question": asked, "why": reference})
            answered.append({"asked": asked, "answer": answer, "reference": reference})

    if errors:
        raise WorkerResponseValidationError(errors)
    return {
        "keep": keeps,
        "add": authored,
        "decline": declines,
        "unanswerable": unanswerable,
        "characterize": characterized,
        "explore": explored,
        **({"instruction_response": answered} if answered else {}),
    }


def run_analysis_reading_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform the whole supplied map into one budgeted model request."""
    tool = _reading_submission_tool(request)
    catalog = _plain_json(_resolved_item(request, READING_REGISTRY_SOURCE_ID))
    nominations = [
        _plain_json(item)
        for item in _source_items(request, READING_NOMINATIONS_SOURCE_ID)
    ]
    frames = [
        _plain_json(item) for item in _source_items(request, READING_FRAMES_SOURCE_ID)
    ]
    domains = [
        _plain_json(item) for item in _source_items(request, READING_DOMAIN_SOURCE_ID)
    ]
    hypotheses = [
        _plain_json(item)
        for item in _source_items(request, READING_HYPOTHESIS_SOURCE_ID)
    ]
    profile = [
        _plain_json(item) for item in _source_items(request, READING_PROFILE_SOURCE_ID)
    ]
    reference = [
        _plain_json(item) for item in _source_items(request, READING_REFERENCE_SOURCE_ID)
    ]
    # The frame map, the nominations and the catalog are all promoted ahead of
    # the serialized bundle for the same reason the definition turn promotes its
    # target schema: they are what this turn acts on, and describing them twice
    # bills the same content twice.
    context = _model_facing_context(
        request,
        READING_FRAMES_SOURCE_ID,
        READING_NOMINATIONS_SOURCE_ID,
        READING_REGISTRY_SOURCE_ID,
        READING_DOMAIN_SOURCE_ID,
        READING_HYPOTHESIS_SOURCE_ID,
        READING_PROFILE_SOURCE_ID,
        READING_REFERENCE_SOURCE_ID,
        AUDITOR_INSTRUCTION_SOURCE_ID,
    )
    instruction = auditor_instruction(request)
    user = json.dumps(
        {
            **({"AUDITOR INSTRUCTION": instruction} if instruction else {}),
            **({"ANALYTICS CATALOG": catalog} if catalog else {}),
            "FRAME MAP": frames,
            **({"POPULATION PROFILE": profile} if profile else {}),
            **({"REFERENCE TABLES": reference} if reference else {}),
            **({"VALUE DOMAINS": domains} if domains else {}),
            "NOMINATIONS": nominations,
            **(
                {"TESTS THE JOIN GATE ADMITTED": hypotheses} if hypotheses else {}
            ),
            "RESOLVED CONTEXT": context,
            "REQUIRED OUTPUT": (
                f"Call {READING_SUBMISSION_TOOL} exactly once. Every nomination "
                "you do not decline is kept, so return only the decisions you "
                "actually want to make."
            ),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if attempt.is_repair:
        user += (
            "\n\nYour previous response could not be used: "
            + "; ".join(attempt.validation_errors)
            + f". Call {READING_SUBMISSION_TOOL} once with a complete correction."
        )
    activity = dict(request.activity)
    activity.setdefault(
        "context_metrics",
        {
            "worker_kind": "analysis_reading",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    message = gateway.complete(
        ANALYSIS_READING_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
        tools=[tool],
        tool_choice={
            "type": "function",
            "function": {"name": READING_SUBMISSION_TOOL},
        },
        return_message=True,
    )
    return _reading_submission_response(message)


ANALYSIS_READING_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.reading.response",
    schema_hash=_sha256_text(
        "analysis-reading-response:v2:keep-add-decline-unanswerable-instruction"
    ),
    validator=_reading_response_schema,
)
ANALYSIS_READING_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_READING_WORKER_ID,
    prompt_hash=_sha256_text(ANALYSIS_READING_SYSTEM),
    response_schema=ANALYSIS_READING_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair assertion-register contract violations against the supplied "
            "frame map and nominations."
        ),
    ),
    implementation=run_analysis_reading_worker,
    semantic_validator=validate_reading_proposal,
)

WORKERS.register(ANALYSIS_READING_WORKER)


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

# Sections a memo carries only when the population profile supports them, in
# the order they appear between the data received and the findings. A two-table
# engagement with no roles has no "People and authority", and a heading over
# nothing is worse than no heading — so these are offered, never demanded.
PROFILE_SECTIONS: tuple[str, ...] = (
    "Where the value is",
    "How the process runs",
    "People and authority",
)
# What planning should do about it, after the reliance section. A list of
# procedures run is no help to an auditor planning the work; "test the 18
# requisitions over 10M individually" is.
IMPLICATIONS_SECTION = "Implications for the plan"
# What no column in the data received can answer, as the reading named it. The
# assembler writes this one from the reading itself: it is a judgement already
# made, and a writer asked to restate it rewords a gap into further work.
UNANSWERABLE_SECTION = "What the data cannot answer"
# Results whose summary is drawn as an exhibit — bars, shares, a heat table, a
# range, a strip, a bridge — rather than placed as its table. Every profile test
# but the identifier series declares one; an authored analysis says so itself
# (``exhibit`` in its projection) by carrying a chart.
_CHART_TESTS = frozenset({
    "breakdown", "value_bands", "calendar_profile", "period_compare", "skipped_steps",
    "crosstab", "step_durations", "role_matrix", "role_overlap", "chain_reconciliation",
    "ratio_consistency", "authority_grid",
})
# How many exhibits the memo places, measured on procurement: one table of
# flagged rows under every procedure a finding listed, and every summary a
# profile section cited, came to 37 exhibits — 24 of them under ten findings.
# The benchmark carries twelve. A finding shows the rows of the first procedure
# it lists that flagged any; a profile section, its first three summaries. The
# rest stay cited in the prose and open from there.
MAX_FINDING_EXHIBITS = 1
MAX_PROFILE_EXHIBITS = 3

_SECTION_LIST = "\n".join(
    f"## {section}"
    for section in (
        SUMMARY_SECTIONS[0],
        *(f"{name}  (optional, from `profile`)" for name in PROFILE_SECTIONS),
        *SUMMARY_SECTIONS[1:3],
        IMPLICATIONS_SECTION,
        f"{UNANSWERABLE_SECTION}  (assembled for you, from WHAT THE READING ESTABLISHED)",
        SUMMARY_SECTIONS[3],
    )
)

SUMMARY_SUBMISSION_TOOL = "submit_analysis_summary"

ANALYSIS_SUMMARY_SYSTEM = f"""[agent:analysis_summary]
Write the exploratory data analysis summary for one audit engagement, as the
auditor who performed the work would write it for the file. Ground every
statement in the supplied procedures, their recorded verdicts and statistics,
the flagged rows supplied for them, the table profiles, and the supplied
coverage gaps. Never invent a count, a value, an identifier, or a procedure.

You do not write the document. Call {SUMMARY_SUBMISSION_TOOL} once, with the
memo in parts, and the parts are assembled into it: the headings, their order,
the result tables, the table of populations received and the table of coverage
by frame are all added for you. Write prose into each part and nothing else —
no heading, no fenced block, and no draft you then replace.

`lead` is a short paragraph saying what the analysis concluded — the two or
three things a reader who stops there should leave with. Not what was performed:
what it showed.

Understanding the population comes before its exceptions. The items of
source `analysis_aggregates` carry the summary each procedure computed — a breakdown, a duration profile,
a role matrix, a value carried from step to step — bounded, with any group too
small to report withheld and counted in `groups_withheld`. Most of them come
from the population profile (procedures marked `profile` in PROCEDURE
REFERENCES), written to describe the data rather than to test it. Read them as
an analyst would, for what they say about how this business runs:

- `data_received` says what kind of data this is, not only how much. A figure
  that equals the step before it on every row, a price that never varies
  between suppliers, dates spread across weekends as evenly as across weekdays,
  numbers carried from one record to the next — each changes which tests can
  prove anything, and saying so is the most useful sentence in the memo. A
  test that cannot fail on this data is worth naming here.
- `profile` holds the parts that describe the population, one per section it
  supports: "Where the value is" (concentration, size bands, categories, time),
  "How the process runs" (how long each step takes, what runs out of order or
  is skipped, what is paid early or late), "People and authority" (who acts in
  which role, how authority is used, who holds two roles that are usually kept
  apart). Give each the procedures it rests on, the one that shows it best
  first: the first three are drawn under your prose as exhibits. Leave out a
  section the supplied results do not support.
- An exception reads better against the profile: three requisitions never
  ordered mean something when every other one was ordered within thirteen days.

`implications` is a list of what the plan should do about it, ordered by
importance — where to point the testing, which records to examine individually,
which evidence to obtain, which planned test the data makes pointless. One
entry per decision, at most 40 words each.

WHAT THE READING ESTABLISHED, when present, is the judgement of the turn that
read every frame before anything was tested. Its `characterized` facts say what
kind of data this is and what that does to testing: carry each into
`data_received`, and its implication into `implications`, in your own words and
against the results that bear it out. Its `unanswerable` questions are placed
for you under "{UNANSWERABLE_SECTION}"; do not repeat them in `further_work`
unless there is evidence somebody could obtain to answer one.

When AUDITOR INSTRUCTION is present, the auditor asked for particular tests.
`lead` says, in one sentence, which of them the data could test and which it
could not, and a result described as clean is clean only on the dimensions
named: never "clean on every tested dimension" while a test the auditor asked
for was not run. The register's answer to the instruction is in the supplied
context. The other parts become these sections, in this order:
{_SECTION_LIST}

PROCEDURE REFERENCES numbers every procedure you were supplied. Refer to one by
its number and never by its id: list the numbers a part rests on in its
`procedures`, and attribute a sentence inside the prose with the marker [#7],
which is replaced by the procedure's id when the memo is assembled. Punctuate
the marker as you would the citation it becomes — "no ceiling is recorded for
110 of 112 invoices ([#7])". A number you did not list in `procedures` cannot be
marked in that part's prose.

This is an analysis, not a register of procedures. Nobody reads it to learn
which tests were run; they read it to learn what the data says about the
engagement. So write `findings` by *issue* — one entry per thing that is
actually true of this population — and cite the procedures that establish each
one as support inside that entry. Never organise by procedure, by test type, or
by verdict label. If two procedures evidence one issue, they belong in one
entry; if one procedure evidences two, it is cited in both.
Order the issues by how much they matter — exposure, then how far the evidence
carries — and say why the first one is first. They are numbered and headed with
their titles in that order, so the ranking is visible without your describing
it. A procedure that established nothing does not earn a mention here at all.

Say each thing once. A fact that belongs to a finding goes in "What the
analysis found"; a fact about how much the results can be trusted goes in "How
far these results can be relied on". Restating a finding under a second heading
because it is also a data-quality point is the single fastest way to make a
memo unreadable.

Be terse. An auditor reads this memo to decide what to do next, and every
sentence that does not change that decision is in the way. Work to these
budgets — they are ceilings, not targets, and a part that says its piece in
half of one is a better part:

- `lead`, at most 120 words.
- `data_received`, at most 150 words. The figures are in the table below it, so
  do not narrate them: write what they mean.
- each finding's `prose`, at most 120 words. Its `title` is a phrase naming the
  issue — around eight words, no procedure reference, no final full stop.
- `reliance.prose`, at most 180 words. The coverage table is below it.
- each `profile` part's prose, at most 150 words.
- each `implications` and `further_work` item, at most 40 words.

Cut in this order: adjectives, then any sentence restating a figure the reader
can see, then any clause explaining why a number is what a number is. Never cut
a qualification — "for the rows the data allowed to compare" is the sentence
doing the work.

Use whichever form carries the content best:

- Prose for judgment, cause, and anything qualified — what the data shows, what
  it does not settle, and why it matters. This is most of the memo.
- A Markdown table where the content is genuinely tabular and is not one of the
  two written for you. Those two are the populations received — a row per source
  table with its record count, its columns and the period it covers — and the
  coverage of frames by procedure, a row per frame with the procedures it
  carries, the rows they reached and how many flagged. Each is placed at the end
  of its section, so prose above it may call it the table below. Write neither.
  Write what they mean instead: which population is too small to carry a rate,
  which frame nobody tested, which period is short of a full year.
- A bulleted list inside a part where the content is genuinely a set — the
  corroboration one finding needs, or the conditions under which a result would
  change. Three items or more, each a full clause; two items are a sentence.

The findings and the outstanding work are numbered for you, from the order you
put them in, so never number them yourself and never write "finally" or "as
noted above" — the numbers carry that.

Structure still has to earn its place. Never emit a bullet per procedure: a
list of what ran is the register, not the analysis, and the coverage table
already is that list. Never break one line of reasoning into fragments — an
argument that has to be read in order is a paragraph, and chopping it into
bullets loses the order. Use standard Markdown tables with a `---` separator
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
findings. The coverage table is placed under it for you, so this prose is the
things that bound how far the rest of the memo can be pushed: rows no procedure
could evaluate, populations too small or too skewed for the method applied, and
results that measured nothing. Never enumerate the procedures — the table below
your prose is the record of what ran.

A procedure carrying `informative: false` established nothing, and
`uninformative_reason` says why. It is not a finding and must not appear as one
— no count from it, no verdict, no "systemic" reading of it. Mention it here if
it is worth mentioning at all, as what it is: a procedure that did not
distinguish any of its rows from the rest. Apply the same judgment where no
flag is set but the arithmetic says the same thing — a weekend share close to
two days in seven is what any ordinary spread of dates produces, not a finding
about after-hours work.

`further_work` is a list, one entry per piece of work, ordered by what should
happen first. One entry is one thing somebody can be asked to do — never a
paragraph carrying three, and never a heading followed by its own sub-list.
Between them the entries must cover every outstanding, stale, and errored
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

A finding resting on flagged rows gets those rows placed under it as a table
the reader can open — you do not write that table or ask for it. The rows of the
first procedure the finding lists that flagged any are placed, so list the one
whose rows prove the finding first, then every other procedure the entry rests
on, and none you only mention in passing.

Call {SUMMARY_SUBMISSION_TOOL} exactly once, with every part filled in.

{LANGUAGE_RULES}
""" + f"\n\n{AUDITOR_INSTRUCTION_RULE}"

SUMMARY_RESULTS_SOURCE_ID = "analysis_results"
SUMMARY_EXCEPTIONS_SOURCE_ID = "analysis_exceptions"
SUMMARY_GAPS_SOURCE_ID = "coverage_gaps"
SUMMARY_READING_SOURCE_ID = "analysis_reading"
SUMMARY_TABLE_METADATA_SOURCE_ID = "table_metadata"
SUMMARY_TABLE_PROFILE_SOURCE_ID = "table_profiles"

# How a memo attributes a sentence to the procedure that establishes it. One or
# two digits, because that is a copy a writer makes correctly: asked instead for
# the eight-hex-character id, one draft got 15 of 20 wrong, spelled one with a
# character that is not hexadecimal, and another with a Greek mu. Assembly
# rewrites each marker to the real id, so the memo a reader opens is unchanged.
_REFERENCE_MARKER = re.compile(r"\[#(\d+)\]")


def _resolved_items(request: WorkerRequest, source_id: str) -> list[object]:
    return [item.content for item in request.context.items if item.source_id == source_id]


def _summary_procedures(request: WorkerRequest) -> list[Mapping[str, Any]]:
    """The supplied procedures in the order the memo will be shown them."""
    return [
        item
        for item in _resolved_items(request, SUMMARY_RESULTS_SOURCE_ID)
        if isinstance(item, Mapping) and str(item.get("analysis_id") or "")
    ]


def _summary_reference_index(request: WorkerRequest) -> dict[int, str]:
    """``ref`` to analysis id, derived from the request and nothing else.

    Both the prompt that offers the references and the validator that resolves
    them read this, so the numbering cannot drift between them: it is a pure
    function of the resolved bundle, computed twice rather than carried.
    """
    return {
        number: str(item["analysis_id"])
        for number, item in enumerate(_summary_procedures(request), start=1)
    }


def _summary_reference_sheet(request: WorkerRequest) -> list[dict[str, Any]]:
    """The numbered register the memo cites from.

    Enough of each procedure to choose it and to say what it showed, and no id:
    an id in front of a writer is an id that ends up copied, and copying them is
    the thing this worker stopped asking for.
    """
    sheet = []
    for number, item in enumerate(_summary_procedures(request), start=1):
        entry: dict[str, Any] = {
            "ref": number,
            "title": item.get("title"),
            "table": item.get("table"),
            "verdict": item.get("verdict_text") or item.get("verdict"),
        }
        if item.get("profile"):
            entry["profile"] = True
        if item.get("informative") is False:
            entry["established_nothing"] = item.get("uninformative_reason")
        try:
            entry["flagged_rows"] = int(item.get("exception_count") or 0)
        except (TypeError, ValueError):
            entry["flagged_rows"] = 0
        sheet.append(entry)
    return sheet


def _summary_submission_response(message: object) -> str:
    return _forced_tool_arguments(message, SUMMARY_SUBMISSION_TOOL)


FINDINGS_SECTION = "What the analysis found"
RELIANCE_SECTION = "How far these results can be relied on"

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


def _handwritten_procedure_ids(prose: str, supplied: set[str]) -> list[str]:
    """Procedure ids typed into prose where a ``[#n]`` marker belongs.

    A memo no longer has any reason to contain an analysis id: assembly writes
    them. So this is not the citation checker it replaces — it does not care
    whether the id is right. Both forms are the same protocol slip, and the id
    being *correct* is the less likely half: asked to write them, one draft got
    15 of 20 wrong, spelling one with a character that is not hexadecimal.

    Anchored to the supplied register rather than to a guessed shape, so the
    business identifiers a memo is full of — ``REQ-2024081``, ``PO20251017`` —
    carry prefixes no procedure uses and read as nothing.
    """
    prefixes, stems = _citation_conventions(supplied)
    if not prefixes and not stems:
        return []
    # Runs long enough to identify one procedure, letter-bearing so a run of
    # digits shared with a date or an amount cannot stand in for one.
    needles = {
        stem[start : start + _MIN_CITATION_RUN]
        for stem in stems
        for start in range(len(stem) - _MIN_CITATION_RUN + 1)
        if any(character.isalpha() for character in stem[start : start + _MIN_CITATION_RUN])
    }
    seen: list[str] = []
    for match in _REFERENCE_TOKEN.finditer(prose):
        token = match.group(1)
        if token in seen:
            continue
        prefix, separator, body = token.partition("-")
        looks_like_one = (
            separator and prefix.casefold() in prefixes and _looks_like_an_id(body)
        ) or any(needle in token.upper() for needle in needles)
        if looks_like_one:
            seen.append(token)
    return [
        f"'{token}' is a procedure id written into the prose. Attribute a "
        "sentence with the marker [#n] of the procedure instead, and list that "
        "n in this entry's `procedures`"
        for token in seen
    ]






def _cited_numbers(part: Mapping[str, Any]) -> list[int]:
    """Every procedure a part rests on: its `procedures`, plus its own markers.

    A ``[#7]`` in the prose *is* a declaration. Requiring the same number in
    ``procedures`` as well made the model state one fact twice, consistently,
    across a long document — and that is exactly the coupling a long emission
    breaks. One memo attributed nine sentences to procedures it had cited in
    prose and not relisted, and the whole stage failed on a rule that had
    nothing to say about whether the memo was right.

    Folding the markers in is also the more faithful reading. A finding whose
    prose cites a procedure is resting on it, whatever the array says; and the
    numbers gained this way now pass the same range and informativeness checks
    the declared ones do, which they previously escaped.
    """
    numbers = [int(number) for number in (part.get("procedures") or [])]
    numbers += [
        int(match.group(1))
        for match in _REFERENCE_MARKER.finditer(str(part.get("prose") or ""))
    ]
    return list(dict.fromkeys(numbers))


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



def _summary_embed(analysis_id: str, result: Mapping[str, Any]) -> list[str]:
    """The embed a profile part places under its prose: a chart or the summary."""
    charted = str(result.get("test") or "") in _CHART_TESTS or result.get("exhibit") == "chart"
    kind = "chart" if charted else "summary_table"
    caption = str(result.get("verdict_text") or result.get("title") or "").strip()
    block = [f"```{EMBED_FENCE}", f"analysis: {analysis_id}", f"as: {kind}"]
    if caption:
        block.append(f"caption: {caption}")
    block.append("```")
    return block


def _profile_parts(value: object) -> list[tuple[str, Mapping[str, Any]]]:
    """The profile parts in section order, one per section, unknown ones dropped."""
    by_section: dict[str, Mapping[str, Any]] = {}
    for raw in value if isinstance(value, (list, tuple)) else []:
        part = _memo_part(raw)
        if part is None:
            continue
        section = str(part.get("section") or "").strip()
        if section in PROFILE_SECTIONS and section not in by_section:
            by_section[section] = part
    return [(section, by_section[section]) for section in PROFILE_SECTIONS if section in by_section]


def _summary_submission_tool(request: WorkerRequest, *, repair: bool = False) -> dict[str, Any]:
    """One forced tool whose schema binds every procedure reference in the memo.

    The references are an enum of the numbers actually supplied, which is the
    whole point of this worker's shape: a memo cannot name a procedure that does
    not exist, because naming one is not a value the schema admits. The rules
    that used to catch a bad id — unsupplied, unknown kind, embedded twice,
    named nothing — describe states unreachable from here.
    """
    references = sorted(_summary_reference_index(request))
    if not references:
        raise WorkerContractError("The analysis summary context supplied no procedures.")
    # No lower bound. The prompt asks for the pattern spanning several results
    # that no single procedure measured — written as an observation to confirm
    # rather than a finding — and that entry rests on nothing in the register by
    # construction. Requiring a reference would make the honest form of it
    # unsayable.
    procedure_list = {
        "type": "array",
        "items": {"type": "integer", "enum": references},
    }
    return {
        "type": "function",
        "function": {
            "name": SUMMARY_SUBMISSION_TOOL,
            "description": (
                "Submit the exploratory data-analysis summary. Refer to a "
                "procedure only by its supplied reference number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lead": {"type": "string", "minLength": 1},
                    "data_received": {"type": "string", "minLength": 1},
                    "findings": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                # The issue named, so the assembler can number
                                # it and head it. Eight words of writing buys a
                                # memo a reader can scan, and buys the prose
                                # below it the opening sentence it no longer
                                # has to spend restating its own subject.
                                "title": {"type": "string", "minLength": 1},
                                "prose": {"type": "string", "minLength": 1},
                                "procedures": procedure_list,
                            },
                            "required": ["title", "prose", "procedures"],
                            "additionalProperties": False,
                        },
                    },
                    "reliance": {
                        "type": "object",
                        "properties": {
                            "prose": {"type": "string", "minLength": 1},
                            "procedures": {
                                "type": "array",
                                "items": {"type": "integer", "enum": references},
                            },
                        },
                        "required": ["prose"],
                        "additionalProperties": False,
                    },
                    "profile": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "section": {"type": "string", "enum": list(PROFILE_SECTIONS)},
                                "prose": {"type": "string", "minLength": 1},
                                "procedures": procedure_list,
                            },
                            "required": ["section", "prose", "procedures"],
                            "additionalProperties": False,
                        },
                    },
                    "implications": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    # A set of items, and now shaped as one. Asked for a string,
                    # the writer returned 460 words of paragraphs enumerating
                    # work the reader then had to separate out again; an array
                    # is numbered by the assembler and cannot run together.
                    "further_work": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                # A repair returns only the parts it changes; the rest is
                # carried from the draft it repairs (``_carry_unrepaired_parts``).
                "required": [] if repair else [
                    "lead",
                    "data_received",
                    "findings",
                    "reliance",
                    "implications",
                    "further_work",
                ],
                "additionalProperties": False,
            },
        },
    }


# The two tables a memo carries that are not judgment: what was received, and
# what was tested over it. Both used to be typed into a prose field — pipes and
# newlines inside a JSON string, which is the region a long emission corrupts
# first. One attempt broke its JSON mid-table; the next flattened the object a
# table sat in. Neither table ever wanted a writer: every figure in them is
# already in the supplied bundle, so they are assembled here for exactly the
# reason the headings and the embed blocks are, and the model is left the half
# of each section that needs an auditor — what the figures mean.
def _as_count(value: object) -> int:
    """A supplied count, or zero where the procedure recorded none."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _markdown_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]]
) -> list[str]:
    """A Markdown table, or nothing at all where there are no rows to show."""
    if not rows:
        return []
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def _period_covered(profile: Mapping[str, Any]) -> str:
    """The span of every date column in one table, as ISO bounds.

    Every date the table carries, not one nominated column: a procure-to-pay
    invoice is raised, received, verified, approved and paid, and the period the
    file covers is the outer edge of all five.
    """
    lows: list[str] = []
    highs: list[str] = []
    for column in profile.get("columns") or []:
        if not isinstance(column, Mapping) or column.get("type") != "date":
            continue
        low = str(column.get("min") or "").strip()
        high = str(column.get("max") or "").strip()
        if low and high:
            lows.append(low)
            highs.append(high)
    if not lows:
        return "—"
    return f"{min(lows)} to {max(highs)}"


def _described_tables(
    request: WorkerRequest, source_id: str
) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("table") or ""): item
        for item in _resolved_items(request, source_id)
        if isinstance(item, Mapping)
    }


def _population_table(request: WorkerRequest) -> list[str]:
    """A row per source table: what was received, before anything was done to it."""
    profiles = _described_tables(request, SUMMARY_TABLE_PROFILE_SOURCE_ID)
    metadata = _described_tables(request, SUMMARY_TABLE_METADATA_SOURCE_ID)
    rows: list[tuple[str, ...]] = []
    for table in sorted(set(profiles) | set(metadata)):
        if not table:
            continue
        profile = profiles.get(table) or {}
        source = profile or metadata[table]
        rows.append(
            (
                table,
                f"{_as_count(source.get('rows')):,}",
                str(len(source.get("columns") or [])),
                _period_covered(profile),
            )
        )
    return _markdown_table(
        ("Source table", "Records", "Columns", "Period covered"), rows
    )


def _rows_reached(procedures: Sequence[Mapping[str, Any]]) -> str:
    """The rows a frame's procedures could evaluate, as one figure or a range.

    A procedure that recorded no denominator contributes none: ``row_count`` is
    the size of a result frame and was never a denominator, and inventing one
    here would be the assembler making the claim the prompt forbids the writer.
    """
    tested = sorted(
        {
            count
            for item in procedures
            for count in (_as_count(item.get("tested")),)
            if count > 0
        }
    )
    if not tested:
        return "—"
    if len(tested) == 1:
        return f"{tested[0]:,}"
    return f"{tested[0]:,} to {tested[-1]:,}"


def _coverage_table(request: WorkerRequest) -> list[str]:
    """A row per frame: the procedures it carries, and the rows they reached.

    The record of work performed, which is what the reliance section needs and
    what an enumeration of procedures in prose was a worse copy of. Frames the
    register never reached are rows here too — a frame nobody tested is the most
    load-bearing thing this table says, and it is invisible in a list of what
    ran.
    """
    frames: dict[str, list[Mapping[str, Any]]] = {}
    for item in _summary_procedures(request):
        frames.setdefault(str(item.get("table") or ""), []).append(item)
    frames.pop("", None)

    # An untested frame has no procedure to read a population off, but a base
    # table still has a profile. Saying how many records nobody tested is the
    # whole point of listing it.
    described = {
        **_described_tables(request, SUMMARY_TABLE_METADATA_SOURCE_ID),
        **_described_tables(request, SUMMARY_TABLE_PROFILE_SOURCE_ID),
    }

    def records(frame: str, procedures: Sequence[Mapping[str, Any]]) -> str:
        population = max(
            (_as_count(item.get("population")) for item in procedures), default=0
        ) or _as_count((described.get(frame) or {}).get("rows"))
        return f"{population:,}" if population else "—"

    rows: list[tuple[str, ...]] = []
    for frame in sorted(frames):
        procedures = frames[frame]
        flagged = sum(
            1 for item in procedures if _as_count(item.get("exception_count")) > 0
        )
        rows.append(
            (
                frame,
                records(frame, procedures),
                str(len(procedures)),
                _rows_reached(procedures),
                str(flagged),
            )
        )
    for frame in _uncovered_frames(request):
        if frame not in frames:
            rows.append((frame, records(frame, ()), "0", "—", "0"))
    return _markdown_table(
        ("Frame", "Records", "Procedures", "Rows tested", "Procedures flagging"),
        rows,
    )


def _uncovered_frames(request: WorkerRequest) -> list[str]:
    """Frames the register never reached, from the supplied coverage gaps."""
    for item in _resolved_items(request, SUMMARY_GAPS_SOURCE_ID):
        if isinstance(item, Mapping):
            return [
                str(frame)
                for frame in item.get("frames_without_any_procedure") or []
                if str(frame or "").strip()
            ]
    return []


def _rendered_prose(prose: str, index: Mapping[int, str]) -> str:
    """Prose with every ``[#n]`` marker resolved to the procedure's real id.

    The memo a reader opens is unchanged by any of this: it still attributes a
    sentence to ``A-1856942A``. What changed is who typed that.
    """
    return _REFERENCE_MARKER.sub(
        lambda match: index.get(int(match.group(1)), match.group(0)), prose.strip()
    )


def _render_summary_markdown(
    payload: Mapping[str, Any],
    index: Mapping[int, str],
    results_by_id: Mapping[str, Mapping[str, Any]],
    request: WorkerRequest,
) -> tuple[str, list[str]]:
    """Assemble the memo, and report the procedures it ended up embedding.

    Deterministic on purpose. The skeleton, the ordering, the embed blocks and
    the two figure tables are the parts a long emission got wrong — two of three
    early drafts wrote the whole document twice, one pasted the prompt's own
    template into its output, and a later one broke its JSON inside a table it
    was typing — so none of them is asked for any more.

    Each table is placed at the end of its section, after the prose that reads
    it. One rule, both sections: prose written above a table can refer to it as
    the table below and be right, whatever else the section carries.
    """
    embedded: list[str] = []
    out: list[str] = [_rendered_prose(str(payload.get("lead") or ""), index), ""]

    out += [f"## {SUMMARY_SECTIONS[0]}", ""]
    out += [_rendered_prose(str(payload.get("data_received") or ""), index), ""]
    population = _population_table(request)
    if population:
        out += [*population, ""]

    # The population profile, in the fixed section order, each part followed by
    # the summaries it rests on. A section the writer left out is not drawn.
    placed: list[str] = []
    for section, part in _profile_parts(payload.get("profile")):
        out += [f"## {section}", ""]
        out += [_rendered_prose(str(part.get("prose") or ""), index), ""]
        shown = 0
        for number in _cited_numbers(part):
            analysis_id = index.get(int(number))
            if analysis_id is None or analysis_id in placed or shown >= MAX_PROFILE_EXHIBITS:
                continue
            shown += 1
            result = results_by_id.get(analysis_id) or {}
            placed.append(analysis_id)
            out += [*_summary_embed(analysis_id, result), ""]

    out += [f"## {FINDINGS_SECTION}", ""]
    # Numbered by the assembler, from the order the writer chose. The order is
    # already the argument — the prompt asks for the issues ranked by how much
    # they matter — and numbering it is what lets the rest of the file, and the
    # reader, refer to "finding 3" and mean something stable.
    for position, finding in enumerate(payload.get("findings") or [], start=1):
        title = _rendered_prose(str(finding.get("title") or ""), index)
        out += [f"### {position}. {title}".rstrip(" ."), ""]
        out += [_rendered_prose(str(finding.get("prose") or ""), index), ""]
        shown = 0
        for number in _cited_numbers(finding):
            analysis_id = index.get(int(number))
            if analysis_id is None or analysis_id in embedded or shown >= MAX_FINDING_EXHIBITS:
                continue
            result = results_by_id.get(analysis_id) or {}
            try:
                flagged = int(result.get("exception_count") or 0) > 0
            except (TypeError, ValueError):
                flagged = False
            # The rule the old validator spent a repair turn asking for, now
            # simply true: a finding resting on flagged rows shows them.
            if not flagged:
                continue
            shown += 1
            embedded.append(analysis_id)
            out += [*_exception_table_embed(analysis_id, result), ""]

    reliance = payload.get("reliance") or {}
    out += [f"## {RELIANCE_SECTION}", ""]
    out += [_rendered_prose(str(reliance.get("prose") or ""), index), ""]
    coverage = _coverage_table(request)
    if coverage:
        out += [*coverage, ""]

    implications = _memo_items(payload.get("implications"))
    if implications:
        out += [f"## {IMPLICATIONS_SECTION}", ""]
        for position, item in enumerate(implications, start=1):
            text = re.sub(r"\s+", " ", _rendered_prose(item, index))
            out += [f"{position}. {text}"]
        out += [""]

    unanswerable = _summary_reading(request).get("unanswerable") or []
    if unanswerable:
        out += [f"## {UNANSWERABLE_SECTION}", ""]
        for entry in unanswerable:
            question = re.sub(r"\s+", " ", str(entry.get("question") or "")).strip()
            why = re.sub(r"\s+", " ", str(entry.get("why") or "")).strip()
            if question:
                out += [f"- **{question}**" + (f" {why}" if why else "")]
        out += [""]

    out += [f"## {SUMMARY_SECTIONS[3]}", ""]
    for position, item in enumerate(_memo_items(payload.get("further_work")), start=1):
        # One line per item: a newline inside a numbered entry ends the list as
        # far as Markdown is concerned, and the item after it stops being one.
        text = re.sub(r"\s+", " ", _rendered_prose(item, index))
        out += [f"{position}. {text}"]

    return "\n".join(out).strip(), embedded


# A Markdown table row, which is the shape a hand-written table announces
# itself by wherever it starts. Only the two sections that get a table
# assembled for them are checked against it: a table anywhere else is a form
# the memo is free to use.
_TABLE_ROW = re.compile(r"(?m)^\s*\|.*\|\s*$")


def _prose_fields(payload: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    """(where, text, assembled_table) for every field the model wrote.

    The flag marks the two sections whose table this worker writes, so prose
    that types one anyway is caught before the reader gets the same figures
    twice under one heading.
    """
    fields = [
        ("the lead paragraph", str(payload.get("lead") or ""), False),
        (f"'{SUMMARY_SECTIONS[0]}'", str(payload.get("data_received") or ""), True),
        (
            f"'{RELIANCE_SECTION}'",
            str((payload.get("reliance") or {}).get("prose") or ""),
            True,
        ),
        (
            f"'{SUMMARY_SECTIONS[3]}'",
            "\n\n".join(_memo_items(payload.get("further_work"))),
            False,
        ),
        (
            f"'{IMPLICATIONS_SECTION}'",
            "\n\n".join(_memo_items(payload.get("implications"))),
            False,
        ),
    ]
    for section, part in _profile_parts(payload.get("profile")):
        fields.append((f"'{section}'", str(part.get("prose") or ""), False))
    for position, finding in enumerate(payload.get("findings") or [], start=1):
        fields.append(
            (f"finding {position}'s title", str(finding.get("title") or ""), False)
        )
        fields.append((f"finding {position}", str(finding.get("prose") or ""), False))
    return fields


def validate_analysis_summary(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Check the memo's argument, and assemble its Markdown.

    What is left to check is what the schema cannot say. A reference is in
    range by construction, so the questions are whether the procedure behind it
    established anything, whether it is being argued in two places at once, and
    whether the prose stayed prose instead of reaching for the document the
    assembler owns.
    """
    index = _summary_reference_index(request)
    if not index:
        raise WorkerContractError("The analysis summary context supplied no procedures.")
    results_by_id = {
        str(item["analysis_id"]): item for item in _summary_procedures(request)
    }

    errors: list[str] = []
    # The half of the rules that decide whether a memo may be committed at all
    # when the repair budget runs out. A procedure that established nothing,
    # written up as a finding, makes the memo say something untrue about the
    # engagement, and a memo in the audit file is read as the work. The rest is
    # shape, and a misshapen memo still beats the nothing that gets committed
    # when the whole document is discarded.
    untrue: list[str] = []

    def reject(message: str, *, integrity: bool = False) -> None:
        if message not in errors:
            errors.append(message)
            if integrity:
                untrue.append(message)

    findings = payload_findings = list(proposal.get("findings") or [])
    if not findings:
        raise WorkerResponseValidationError("the summary records no findings")

    # Procedures the workspace already determined establish nothing. Whether a
    # result separated any of its rows from the rest is arithmetic, so it is
    # decided before the memo is written rather than left to be spotted in a
    # denominator — the failure this prevents is a saturated result written up
    # as a systemic finding, which is what it looks like from every angle
    # except that one.
    uninformative = {
        str(item["analysis_id"]): str(item.get("uninformative_reason") or "")
        for item in _summary_procedures(request)
        if item.get("informative") is False
    }

    in_findings: set[int] = set()
    for position, finding in enumerate(payload_findings, start=1):
        declared = _cited_numbers(finding)
        # Out of range is unreachable through the tool schema, which enumerates
        # the references. It is reachable through the text fallback a weak
        # provider takes, so it is answered rather than raised.
        for number in declared:
            if number not in index:
                reject(
                    f"finding {position} references [#{number}], which is not one of "
                    f"the supplied procedures (1 to {len(index)})",
                    integrity=True,
                )
        declared = [number for number in declared if number in index]
        in_findings.update(declared)
        for number in declared:
            analysis_id = index[number]
            if analysis_id in uninformative:
                reject(
                    f"finding {position} rests on [#{number}] ({analysis_id}), which "
                    f"{uninformative[analysis_id]}. It cannot carry a finding; report "
                    f"it under '{RELIANCE_SECTION}' as a limit on the work, if at all",
                    integrity=True,
                )
    reliance = proposal.get("reliance") or {}
    reliance_numbers = _cited_numbers(reliance)
    for number in reliance_numbers:
        if number not in index:
            reject(
                f"'{RELIANCE_SECTION}' references [#{number}], which is not one of "
                f"the supplied procedures (1 to {len(index)})",
                integrity=True,
            )
    in_reliance = {number for number in reliance_numbers if number in index}

    sections_seen: list[str] = []
    for raw in proposal.get("profile") or []:
        part = _memo_part(raw) or {}
        section = str(part.get("section") or "").strip()
        if section not in PROFILE_SECTIONS:
            reject(f"profile section '{section}' is not one of: {', '.join(PROFILE_SECTIONS)}")
            continue
        if section in sections_seen:
            reject(f"profile section '{section}' is written twice; write each once")
        sections_seen.append(section)
        for number in _cited_numbers(part):
            if number not in index:
                reject(
                    f"'{section}' references [#{number}], which is not one of the "
                    f"supplied procedures (1 to {len(index)})",
                    integrity=True,
                )
            elif index[number] in uninformative:
                reject(
                    f"'{section}' rests on [#{number}] ({index[number]}), which "
                    f"{uninformative[index[number]]}; it describes nothing",
                    integrity=True,
                )

    # The two sections the memo must keep apart. Findings belong above,
    # reliance limits below, and a procedure argued in both is the restatement
    # the older skeleton produced by construction.
    for number in sorted(in_findings & in_reliance):
        reject(
            f"[#{number}] ({index[number]}) is argued in both '{FINDINGS_SECTION}' "
            f"and '{RELIANCE_SECTION}'; say it once, in whichever it belongs to"
        )

    supplied = set(index.values())
    for where, prose, assembled_table in _prose_fields(proposal):
        # The assembler owns the skeleton, the embeds and the figure tables.
        # Prose that reaches for any of them is a draft trying to emit the
        # document, which is the failure this worker's shape exists to make
        # impossible rather than to catch.
        if re.search(r"(?m)^\s*#{1,6}\s", prose):
            reject(
                f"{where} contains a Markdown heading. Write prose only — the "
                "section headings are added when the memo is assembled"
            )
        if "```" in prose:
            reject(
                f"{where} contains a fenced block. Write prose only — the result "
                "tables are added when the memo is assembled"
            )
        if assembled_table and _TABLE_ROW.search(prose):
            reject(
                f"{where} contains a Markdown table. This section's table is "
                "assembled from the supplied figures and placed under your "
                "prose — write what it means, not the table itself"
            )
        for message in _handwritten_procedure_ids(prose, supplied):
            reject(f"{where}: {message}")

    markdown, _embedded = _render_summary_markdown(
        proposal, index, results_by_id, request
    )
    if not markdown:
        raise WorkerResponseValidationError("the summary is empty")

    # What the memo rests on, which is what it declared — not the subset that
    # happened to earn a table. A procedure with nothing flagged is cited in
    # prose and embeds nothing, and a memo resting on it still rests on it.
    cited: list[str] = []
    for number in [
        *(
            number
            for finding in payload_findings
            for number in _cited_numbers(finding)
        ),
        *_cited_numbers(reliance),
        *(
            number
            for _section, part in _profile_parts(proposal.get("profile"))
            for number in _cited_numbers(part)
        ),
    ]:
        analysis_id = index.get(int(number))
        if analysis_id is not None and analysis_id not in cited:
            cited.append(analysis_id)

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
                else {"markdown": markdown, "cited_analysis_ids": cited, "unrepaired": list(errors)}
            ),
        )

    return {
        "markdown": markdown,
        "cited_analysis_ids": cited,
    }


def _memo_part(value: object) -> Mapping[str, Any] | None:
    """One ``{prose, procedures}`` part, from the shapes a long emission returns.

    Nesting is the first thing a long emission loses. One run flattened
    `reliance` to the prose string it would have held; the attempt before it
    flattened `findings` the same way. Neither flattening lost anything the memo
    needed — assembly reads a part only through `prose`, and ``_cited_numbers``
    already recovers the references from the ``[#n]`` markers in that prose,
    because a marker *is* a declaration — so a whole memo was discarded over a
    pair of braces.

    Reading the flattened form is not leniency about what the memo may say.
    Every rule about the argument runs immediately after this, on the same text.
    The asymmetry it removes is the one that ran the wrong way: a memo whose
    argument is faulty is salvaged by ``validate_analysis_summary``'s partial
    path, and a memo that was merely misshapen was not salvaged at all.
    """
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        return {"prose": value}
    return None


def _memo_findings(value: object) -> list[Mapping[str, Any]] | None:
    """The findings array, from those same shapes. ``None`` where it is corrupt.

    A flattened array is worth one parse: it is JSON often enough to recover.
    Where the parse fails the emission is corrupt rather than merely flattened —
    the run this reads for returned a duplicated clause, an invented key and a
    stray closing tag inside that string — and no coercion should paper over it.

    An entry that will not coerce fails the whole array rather than being
    dropped from it. A memo silently missing one of its issues is worse than a
    memo rejected for missing one.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, list) or not value:
        return None
    parts = [_memo_part(item) for item in value]
    if any(part is None for part in parts):
        return None
    return [part for part in parts if part is not None]


def _memo_items(value: object) -> list[str]:
    """A list part, from the shapes a long emission returns.

    A run of items flattened into one string is the same slip as a flattened
    object, and recovers the same way: the bullets or the blank lines the writer
    reached for anyway are where the items divide. Where it flattened to a
    single paragraph there is one item, which is what a paragraph is.
    """
    if isinstance(value, str):
        blocks = [
            re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", block).strip()
            for block in re.split(r"\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)])\s)", value)
        ]
        return [block for block in blocks if block]
    # ``tuple`` because the assembler runs from the semantic validator, and the
    # registered response schema freezes its proposal first — so the array shape
    # this is *mostly* called with never reaches here from a real memo. Matching
    # only ``list`` emptied every ``further_work`` section a model ever wrote,
    # and emptied it silently: the heading was still rendered above it.
    if isinstance(value, (list, tuple)):
        return [
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip()
        ]
    return []


def _summary_response_schema(response: str) -> Mapping[str, Any]:
    """The submitted memo parts, before anything is asked about the argument."""
    payload = decode_json_response(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    findings = _memo_findings(payload.get("findings"))
    if findings is None:
        raise WorkerResponseValidationError(
            "the response must carry a non-empty `findings` array, and every "
            "entry must carry prose"
        )
    reliance = _memo_part(payload.get("reliance"))
    if reliance is None:
        raise WorkerResponseValidationError("`reliance` must be an object")
    further_work = _memo_items(payload.get("further_work"))
    if not further_work:
        raise WorkerResponseValidationError(
            "`further_work` must carry at least one item"
        )
    for field in ("lead", "data_received"):
        if not str(payload.get(field) or "").strip():
            raise WorkerResponseValidationError(f"`{field}` must carry prose")
    profile = payload.get("profile")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except ValueError:
            profile = None
    return {
        "lead": payload.get("lead"),
        "data_received": payload.get("data_received"),
        "findings": findings,
        "reliance": reliance,
        # Both optional to the parser: the profile because a population may
        # support none of its sections, the implications because a memo that
        # omits them is still a memo — the tool asks for them, and the
        # assembler draws the heading only over what was written.
        "profile": [part for part in (_memo_part(p) for p in (profile or [])) if part is not None]
        if isinstance(profile, (list, tuple)) else [],
        "implications": _memo_items(payload.get("implications")),
        "further_work": further_work,
    }


def _summary_reading(request: WorkerRequest) -> Mapping[str, Any]:
    """The reading's kept judgement, or an empty mapping when none was supplied."""
    for item in _source_items(request, SUMMARY_READING_SOURCE_ID):
        reading = _plain_json(item)
        if isinstance(reading, Mapping):
            return reading
    return {}


def run_analysis_summary_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request.

    A repair is not the first turn asked again. It corrects a draft against
    errors that name what is wrong, so it is sent the reference sheet, the draft
    and the errors — not the 60k-token bundle the draft was written from — and
    it returns only the parts it changes. Measured on procurement, the full
    resend asked for a whole memo again and took six minutes to fail.
    """
    tool = _summary_submission_tool(request, repair=attempt.is_repair)
    user = json.dumps(
        {
            # Numbered ahead of the bundle, because the number is how the memo
            # refers to a procedure and the id is not something the writer ever
            # needs to type.
            "PROCEDURE REFERENCES": _summary_reference_sheet(request),
            **(
                {"AUDITOR INSTRUCTION": auditor_instruction(request)}
                if auditor_instruction(request)
                else {}
            ),
            **(
                {"WHAT THE READING ESTABLISHED": dict(_summary_reading(request))}
                if _summary_reading(request)
                else {}
            ),
            "RESOLVED CONTEXT": _model_facing_context(
                request, AUDITOR_INSTRUCTION_SOURCE_ID, SUMMARY_READING_SOURCE_ID
            ),
        },
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    conversation = None
    if attempt.is_repair:
        if attempt.previous_response is None:
            raise WorkerContractError(
                "An analysis-summary repair requires the previous response."
            )
        user = json.dumps(
            {
                "PROCEDURE REFERENCES": _summary_reference_sheet(request),
                **(
                    {"AUDITOR INSTRUCTION": auditor_instruction(request)}
                    if auditor_instruction(request)
                    else {}
                ),
                "NOTE": (
                    "This turn corrects the draft below. The material it was "
                    "written from is not resent: every figure and identifier the "
                    "draft states stands, and a correction moves or removes "
                    "wording rather than adding facts."
                ),
            },
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        conversation = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": attempt.previous_response},
            {
                "role": "user",
                "content": (
                    "Correct every listed quality-gate error in the prior summary "
                    "while preserving all otherwise-valid wording and evidence: "
                    + "; ".join(attempt.validation_errors)
                    + f". Call {SUMMARY_SUBMISSION_TOOL} once with only the parts "
                    "you change. A part you return replaces that part whole — "
                    "`findings` is then the complete list of findings — and every "
                    "part you leave out is kept from the draft unchanged."
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
    message = gateway.complete(
        ANALYSIS_SUMMARY_SYSTEM,
        user,
        activity,
        attempt=attempt.number,
        tools=[tool],
        tool_choice={
            "type": "function",
            "function": {"name": SUMMARY_SUBMISSION_TOOL},
        },
        conversation=conversation,
        return_message=True,
    )
    response = _summary_submission_response(message)
    if attempt.is_repair and attempt.previous_response:
        response = _carry_unrepaired_parts(response, attempt.previous_response)
    return response


#: Every part of the memo. A repair returns only what it changes; anything it
#: leaves out is the draft's. First added for the two optional parts after a
#: rewrite asked to move one procedure out of the reliance section returned
#: without the three profile sections the draft had written.
_CARRIED_PARTS = (
    "lead", "data_received", "findings", "reliance", "profile", "implications", "further_work",
)


def _carry_unrepaired_parts(response: str, previous: str) -> str:
    """Restore the optional parts a repair dropped, from the draft it repaired."""
    try:
        repaired = json.loads(response)
        earlier = json.loads(previous)
    except (TypeError, ValueError):
        return response
    if not isinstance(repaired, dict) or not isinstance(earlier, dict):
        return response
    carried = {
        part: earlier[part]
        for part in _CARRIED_PARTS
        if not repaired.get(part) and earlier.get(part)
    }
    if not carried:
        return response
    return json.dumps({**repaired, **carried}, ensure_ascii=False)


ANALYSIS_SUMMARY_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.summary.response",
    schema_hash=_sha256_text(
        "analysis-summary-response:v5:structured-references:flattened-parts-read"
        ":titled-findings:listed-further-work:profile-sections:implications"
    ),
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
    "UNANSWERABLE_SECTION",
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
    "SUMMARY_SECTIONS",
    "analysis_semantic_id",
    "parse_embeds",
    "run_analysis_authoring_worker",
    "run_join_utility_worker",
    "run_analysis_summary_worker",
    "validate_authoring_proposal",
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

{JSON_RULES} {LANGUAGE_RULES}

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
        separators=(",", ":"),
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
