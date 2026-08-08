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
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import analytics, sandbox
# The memo's embed grammar is shared with the context adapter that hands the
# memo to the APM. It lives outside ``agent/`` because a worker may not import
# the context package and vice versa; the module has no dependencies of its own.
from ...analysis_memo import EMBED_FENCE, EMBED_KINDS, parse_embeds
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


ANALYSIS_DEFINITION_WORKER_ID = "analysis.definitions"
JOIN_UTILITY_WORKER_ID = "analysis.join_utility"
JOIN_UTILITY_CANDIDATES_SOURCE_ID = "join_candidates"
JOIN_UTILITY_SUBMISSION_TOOL = "submit_join_utility_decisions"
# A rejection may argue redundancy only through the structural superseded_by
# field, which the cross-pair check reads; this catches the same claim made in
# free-text rationale instead, where that check cannot see it.
JOIN_UTILITY_REDUNDANCY_LANGUAGE = (
    "supersede", "superseded", "redundant", "already retained", "already covered",
)
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

When you reject one of those alternates, set ``superseded_by`` to the ref of
the retained candidate for the *same pair of tables* and say so in the
rationale. Never set ``superseded_by`` to a candidate that connects a
*different* pair: two tables already being joined through some other pair does
not make a relationship between them redundant, because it is a different
relationship. "financial_approval_matrix and staff_details are joined" does
not supersede "requisitions and staff_details" — they connect different
tables, and the second may be exactly what a later test needs to reach a
specific transaction's approver at all. Retaining fewer relationships is not
itself a goal; retain every relationship whose own pair supports a test, and
reject only what that pair itself does not support or what a same-pair
alternate has already covered. Leave ``superseded_by`` empty for every other
kind of rejection — the keys match but no control spans the two tables, or the
join is merely technically safe.

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


def proposal_budget(rows: int | None) -> int:
    """How many analyses to ask of a frame, from the size of the frame."""
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
        else:
            properties["superseded_by"] = {
                "type": "string",
                "enum": [""] + refs,
                "description": (
                    "The ref of the retained candidate for the SAME pair of "
                    "tables that makes this one redundant. Set to the empty "
                    "string unless this rejection is only because a better "
                    "route to that same pair was retained instead."
                ),
            }
            # Required, not merely offered: an optional field a model can skip
            # while still writing "superseded by X" in free-text rationale
            # would let the exact cross-pair claim this exists to catch slip
            # past uncaught. Forcing an explicit "" or a ref every time is what
            # makes the mechanical check below able to see it at all.
            required.append("superseded_by")
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
            "superseded_by": (
                str(item.get("superseded_by") or "").strip()
                if decision == "reject"
                else ""
            ),
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
    # A rejection may only cite a same-pair alternate as its reason: two
    # different tables already being reachable through some other pair does
    # not make a relationship between them redundant, and citing one anyway is
    # exactly the mistake that silently disconnects a table from every join —
    # the rejected relationship may be the only route to it.
    retained_refs = {item["ref"] for item in accepted if item["decision"] == "retain"}
    for item in accepted:
        cited = item["superseded_by"]
        if item["decision"] != "reject" or not cited:
            continue
        if cited == item["ref"]:
            errors.append(f"'{item['ref']}' cannot cite itself as superseded_by")
            continue
        if cited not in retained_refs:
            errors.append(
                f"'{item['ref']}' claims to be superseded by '{cited}', which "
                "was not retained; superseded_by must name a retained candidate"
            )
            continue
        own_pair = frozenset(
            (str(candidates[item["ref"]].get("left") or ""),
             str(candidates[item["ref"]].get("right") or ""))
        )
        other_pair = frozenset(
            (str(candidates[cited].get("left") or ""),
             str(candidates[cited].get("right") or ""))
        )
        if own_pair != other_pair:
            errors.append(
                f"'{item['ref']}' cites '{cited}' as superseding it, but they "
                f"connect different table pairs ({sorted(own_pair)} vs "
                f"{sorted(other_pair)}); only a retained alternate for the "
                "SAME pair may supersede a rejection"
            )
    # The structural field is required precisely so this check has something
    # to read, but a rationale can still argue redundancy in prose while
    # leaving the field empty — the two are supposed to agree, and only the
    # structural one is checked above.
    for item in accepted:
        if item["decision"] != "reject" or item["superseded_by"]:
            continue
        lowered = item["rationale"].lower()
        if any(phrase in lowered for phrase in JOIN_UTILITY_REDUNDANCY_LANGUAGE):
            errors.append(
                f"'{item['ref']}' rationale claims another relationship makes "
                "it redundant but superseded_by is empty; name the retained, "
                "same-pair candidate in superseded_by or rephrase the "
                "rejection reason"
            )
    missing = sorted(set(candidates) - seen)
    if missing:
        errors.append("A decision is required for every candidate: " + ", ".join(missing))
    if errors:
        raise WorkerResponseValidationError(errors)
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
    if attempt.is_repair:
        prompt += "\nRepair the prior response: " + "; ".join(attempt.validation_errors)
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
        return_message=True,
    )
    return _join_utility_submission_response(message)


JOIN_UTILITY_WORKER = WorkerDefinition(
    worker_id=JOIN_UTILITY_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_join_utility_worker)),
    prompt_hash=_sha256_text(JOIN_UTILITY_SYSTEM),
    response_schema=WorkerResponseSchema(
        schema_id="analysis.join_utility.response",
        schema_hash=_sha256_text("join-utility-response"),
        validator=_join_utility_response_schema,
    ),
    repair_policy=WorkerRepairPolicy(max_repair_attempts=1, guidance_hash=_sha256_text("Repair join utility decisions.")),
    implementation=run_join_utility_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_join_utility_proposal)),
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
    return {}


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
        for raw in metadata.get("params") or []:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
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
        params_schema: dict[str, Any] = {
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
                            _target_rows(_target_schema(request))
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
    proposed = payload.get("analyses")
    if proposed is None and isinstance(payload.get("analysis"), dict):
        # A single-object answer is a common shape slip; normalize rather than
        # spending a repair turn on it.
        proposed = [payload["analysis"]]
    if not isinstance(proposed, list) or not proposed:
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a non-empty `analyses` array"
        )
    if any(not isinstance(item, dict) for item in proposed):
        raise WorkerResponseValidationError("every analyses entry must be an object")
    return {"analyses": proposed}


def _validate_analytics_spec(
    spec: Mapping[str, Any],
    registry: Mapping[str, Mapping[str, Any]],
    columns: set[str],
    column_types: Mapping[str, str],
    label: str,
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
    budget = proposal_budget(_target_rows(schema))
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
                raw_spec, registry, columns, column_types, label
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
                single = len(spec_scope(spec, origins)) < 2
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
    context = request.context.to_dict()
    context["items"] = [
        item
        for item in context["items"]
        if item.get("source_id") != TARGET_SCHEMA_SOURCE_ID
    ]
    hypotheses = [
        _plain_json(item) for item in _source_items(request, JOIN_HYPOTHESIS_SOURCE_ID)
    ]
    user = json.dumps(
        {
            "TARGET FRAME": schema,
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
    schema_hash=_sha256_text("analysis-definitions-response:json-object-with-analyses"),
    validator=_analysis_response_schema,
)
ANALYSIS_DEFINITION_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_DEFINITION_WORKER_ID,
    implementation_hash=_sha256_text(
        inspect.getsource(run_analysis_definition_worker)
    ),
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
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_analysis_proposal)
    ),
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

To place a result in the memo, emit a fenced block on its own lines:
```{EMBED_FENCE}
analysis: <analysis_id>
as: <{" | ".join(EMBED_KINDS)}>
caption: <one short line saying what the reader should see in it>
```
Use the exact analysis_id of a supplied procedure; an id that was not supplied
is rejected. An embed is not a footnote — it renders the result itself, as a
table the reader can read and open, so it is where the detail behind a finding
belongs. Embed every result whose exceptions you state a count for, as
`exception_table`; use `chart` for a distribution or a trend you are drawing a
conclusion from, and `stats` where the numbers are the point. Do not embed a
procedure you only mention in passing, and do not embed one twice.
Return Markdown only, with no JSON wrapper and no outer code fence.
"""

SUMMARY_RESULTS_SOURCE_ID = "analysis_results"
SUMMARY_EXCEPTIONS_SOURCE_ID = "analysis_exceptions"


def _resolved_items(request: WorkerRequest, source_id: str) -> list[object]:
    return [item.content for item in request.context.items if item.source_id == source_id]


FINDINGS_SECTION = "What the analysis found"
RELIANCE_SECTION = "How far these results can be relied on"

# How many instance identifiers one line may name. A finding names the two or
# three instances that carry it; past that the writer has stopped choosing and
# started copying the flagged-row table into a sentence, which hands the reader
# a truncated sample where an embed would have given them the whole result as
# something they can open.
MAX_NAMED_INSTANCES_PER_LINE = 4

_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{2,23}\b")
# A conversational hand-off, not a lead paragraph: one short line ending in a
# colon, before anything else. Bounded in length so a genuine opening sentence
# that happens to introduce a list is not mistaken for chat.
_PREAMBLE = re.compile(r"\A[^\n]{0,120}:[ \t]*\n+")


def _is_instance_identifier(token: str) -> bool:
    """Whether a token looks like an identifier for one record.

    Letters and digits together is what separates ``INV2024017``, ``V1008``, or
    ``REQ2024063`` from a column name, a table name, or an ordinary word. A
    bare year or an amount carries no letters and never reaches here.
    """
    return any(c.isalpha() for c in token) and any(c.isdigit() for c in token)


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


def _memo_sections(markdown: str) -> list[tuple[str, list[str]]]:
    """The level-2 sections in document order, each with its prose lines.

    Table rows and embed blocks are dropped: both legitimately carry many
    identifiers, and neither is prose a reader has to wade through.
    """
    sections: list[tuple[str, list[str]]] = []
    heading: str | None = None
    body: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith("|"):
            continue
        match = re.fullmatch(r"##\s+(.+?)\s*", line)
        if match:
            if heading is not None:
                sections.append((heading, body))
            heading, body = match.group(1).strip(), []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        sections.append((heading, body))
    return sections


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

    sections = _memo_sections(markdown)
    present = [name for name, _ in sections]
    folded = {name.casefold() for name in present}
    missing = [
        section for section in SUMMARY_SECTIONS if section.casefold() not in folded
    ]
    if missing:
        raise WorkerResponseValidationError(f"missing section '{missing[0]}'")
    # Order carries meaning here: the populations frame the findings, and the
    # findings are what the reliance limits qualify. Read out of order they
    # argue for nothing.
    ordered = [name for name in present if name.casefold() in {
        section.casefold() for section in SUMMARY_SECTIONS
    }]
    expected = [
        section
        for section in SUMMARY_SECTIONS
        if section.casefold() in {name.casefold() for name in ordered}
    ]
    if [name.casefold() for name in ordered] != [name.casefold() for name in expected]:
        raise WorkerResponseValidationError(
            "sections are out of order: expected " + ", ".join(expected)
        )

    if not _lead_paragraph(markdown):
        raise WorkerResponseValidationError(
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
    for embed in embeds:
        analysis_id = embed.get("analysis")
        if not analysis_id:
            raise WorkerResponseValidationError("an embed names no analysis")
        # A citation the reader cannot open is worse than no citation at all: it
        # looks like evidence and resolves to nothing.
        if analysis_id not in supplied:
            raise WorkerResponseValidationError(
                f"embed cites '{analysis_id}', which is not a supplied procedure"
            )
        kind = embed.get("as") or ""
        if kind not in EMBED_KINDS:
            raise WorkerResponseValidationError(
                f"embed for '{analysis_id}' uses unknown kind '{kind}'"
            )
        if analysis_id in embedded:
            raise WorkerResponseValidationError(
                f"'{analysis_id}' is embedded more than once"
            )
        embedded.append(analysis_id)

    citation = _citation_pattern(supplied)
    cited: dict[str, set[str]] = {}
    for name, body in sections:
        for line in body:
            # A citation is not an instance. Strip the ids the memo was shown
            # before counting what is left, or a paragraph citing three
            # auditor-saved procedures would read as three named invoices.
            stripped = citation.sub(" ", line) if citation else line
            named = {
                token
                for token in _TOKEN.findall(stripped)
                if _is_instance_identifier(token)
            }
            if len(named) > MAX_NAMED_INSTANCES_PER_LINE:
                raise WorkerResponseValidationError(
                    f"'{name}' names {len(named)} instance identifiers in one line "
                    f"({', '.join(sorted(named)[:4])}, …); name the two or three "
                    "that carry the finding and embed the result for the rest"
                )
            on_line = citation.findall(line) if citation else []
            for analysis_id in on_line:
                cited.setdefault(analysis_id, set()).add(name)
            if name != FINDINGS_SECTION:
                continue
            for analysis_id in on_line:
                if analysis_id in uninformative:
                    raise WorkerResponseValidationError(
                        f"'{analysis_id}' cannot be a finding: it "
                        f"{uninformative[analysis_id]}. Report it under "
                        f"'{RELIANCE_SECTION}' as a limit on the work, if at all"
                    )
            # A count stated for a procedure is a claim about rows the reader
            # should be able to see, and the embed is how they see them.
            if any(c.isdigit() for c in stripped):
                for analysis_id in on_line:
                    if analysis_id not in embedded:
                        raise WorkerResponseValidationError(
                            f"'{analysis_id}' is discussed with a count but its "
                            "result is not embedded; embed it as exception_table"
                        )

    # The two sections the memo must keep apart. Findings belong above,
    # reliance limits below, and a procedure argued in both is the restatement
    # the older skeleton produced by construction.
    for analysis_id, names in cited.items():
        if {FINDINGS_SECTION, RELIANCE_SECTION} <= names:
            raise WorkerResponseValidationError(
                f"'{analysis_id}' is argued in both '{FINDINGS_SECTION}' and "
                f"'{RELIANCE_SECTION}'; say it once, in whichever it belongs to"
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
        {"RESOLVED CONTEXT": request.context.to_dict()},
        indent=1,
        ensure_ascii=False,
        default=str,
    )
    if attempt.is_repair:
        user += (
            "\n\nThe previous summary failed validation: "
            + "; ".join(attempt.validation_errors)
            + ". Return the complete corrected summary."
        )
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
        ANALYSIS_SUMMARY_SYSTEM, user, activity, attempt=attempt.number
    )


ANALYSIS_SUMMARY_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="analysis.summary.response",
    schema_hash=_sha256_text("analysis-summary-response:markdown-with-embed-fences"),
    validator=_summary_response_schema,
)
ANALYSIS_SUMMARY_WORKER = WorkerDefinition(
    worker_id=ANALYSIS_SUMMARY_WORKER_ID,
    implementation_hash=_sha256_text(inspect.getsource(run_analysis_summary_worker)),
    prompt_hash=_sha256_text(ANALYSIS_SUMMARY_SYSTEM),
    response_schema=ANALYSIS_SUMMARY_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair missing or misordered summary sections, a missing lead "
            "paragraph, citations to unsupplied procedures, runs of named "
            "instance identifiers, counts stated without an embedded result, "
            "and findings restated under the reliance section."
        ),
    ),
    implementation=run_analysis_summary_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_analysis_summary)),
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
