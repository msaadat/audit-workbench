"""Registered model workers for the test capability group.

One worker, one pass: ``tests.generate`` turns one RCM row into the complete,
executable tests that cover it, choosing each test's source and writing its
executable steps in a single turn (docs/test-capability-merge-plan.md).

The contract is deliberately small. The common fields are a discriminator plus
title/objective/steps; a generated Data Test's steps are Polars code against
named tables; a generated Document Test's steps are one of two execution
modes. Larger contracts were tried and produced worse proposals: a
discriminated union over four document-test item kinds and three data-test
engines made the model pick a shape and then fill it badly, and two registry
payloads worth ~14,000 characters crowded the actual engagement material out
of the prompt.

Analytics and validation engines remain fully supported for auditor-authored
Data Tests, and the ``attribute``/``review`` document builders for
auditor-authored Document Tests. They are simply not what generation emits.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import cycle_linking, cycle_vouching, doc_tests, sandbox
from ...text import counted, relevance_tokens
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
)


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


_PARTIAL_CYCLE_COVERAGE = re.compile(
    r"\b(?:not covered|not available|outside (?:the )?scope|"
    r"cannot (?:test|verify|establish)|unable to (?:test|verify|establish)|"
    r"as a prerequisite|prerequisite for)\b",
    re.IGNORECASE,
)


def _plain_json(value: object) -> object:
    """Deep-copy frozen proposal values back to plain JSON containers."""
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


def _source_items(request: WorkerRequest, source_id: str) -> list[object]:
    """Return the supplied contents for one optional source in stable order."""
    return [item.content for item in request.context.items if item.source_id == source_id]


def _model_transaction_manifest(value: object) -> object:
    """Project only decision-relevant facts about the approved rules.

    The turn chooses which requirements one procedure answers and which rows it
    runs over. Rule ids, hashes and per-role counts do not help it do either,
    and the rules themselves are read from the ruleset rather than restated.
    """

    if not isinstance(value, Mapping):
        return value
    if not value.get("ruleset_id"):
        return {"available": False, "reason": str(value.get("reason") or "")}
    return {
        "available": True,
        "cycle_label": value.get("cycle_label"),
        "roles": [
            {
                "name": role.get("name"),
                "document_type": role.get("document_type"),
                "required": role.get("required", True),
            }
            for role in value.get("roles") or []
            if isinstance(role, Mapping)
        ],
        "population": {
            "table": (value.get("anchor") or {}).get("table"),
            "column": (value.get("anchor") or {}).get("column"),
        },
        "assertions": value.get("assertions") or [],
        "reach": value.get("reach") or {},
    }


#: How many table schemas one generation prompt carries. Eight rather than six
#: because the join family of a six-table workspace runs to twenty frames, and
#: a cut at six was spending every slot on frames over one population.
_SCHEMA_LIMIT = 8
#: How many of those are held back for undecorated base tables. Three, because
#: the small shared dimensions — an approval matrix, a staff master — rank
#: highly on almost every row, and a requirement about a *third* population
#: still has to see it.
_BASE_TABLE_RESERVE = 3


def _relevant_table_schemas(
    raw_tables: list[object], rcm_row: object, transaction_manifest: object
) -> list[object]:
    """Keep a bounded, deterministic set of schemas relevant to one RCM row."""
    if not isinstance(rcm_row, Mapping):
        return [_plain_json(value) for value in raw_tables[:_SCHEMA_LIMIT]]
    attributes = [
        item
        for item in rcm_row.get("control_attributes") or []
        if isinstance(item, Mapping)
    ]
    evidence_kinds = {str(item.get("evidence_kind") or "") for item in attributes}
    if attributes and "tabular_population" not in evidence_kinds:
        return []
    query_parts = [
        rcm_row.get(key) for key in ("process", "risk", "control", "criteria")
    ]
    for attribute in attributes:
        query_parts.extend((attribute.get("key"), attribute.get("requirement")))
    query_tokens = set().union(*(relevance_tokens(item) for item in query_parts))
    candidate_tables = {
        str(candidate.get("table") or "")
        for group in (
            transaction_manifest.get("groups") or []
            if isinstance(transaction_manifest, Mapping)
            else []
        )
        if isinstance(group, Mapping)
        for candidate in group.get("candidates") or []
        if isinstance(candidate, Mapping)
    }
    ranked = []
    for index, raw in enumerate(raw_tables):
        if not isinstance(raw, Mapping):
            ranked.append((0, 0, -index, raw, False))
            continue
        table = str(raw.get("table") or "")
        table_tokens = relevance_tokens(table)
        column_tokens = set().union(
            *(
                relevance_tokens(column.get("name"))
                for column in raw.get("columns") or []
                if isinstance(column, Mapping)
            ),
            set(),
        )
        score = 4 * len(query_tokens & table_tokens) + len(query_tokens & column_tokens)
        ranked.append(
            (
                1 if table in candidate_tables else 0,
                score,
                -index,
                raw,
                not raw.get("derived"),
            )
        )
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    # A derived frame's name contains every word of the tables it was built
    # from, so on name score it can never rank below them: the six best-scoring
    # frames for a vendor-master risk were six *joins over* the vendor master,
    # and the population itself did not reach the prompt. Reserving the tail
    # for the best-ranked base tables keeps the frame a test would be written
    # against and the population it is about both available, without giving up
    # the ranking for the rest of the list.
    selected = ranked[:_SCHEMA_LIMIT - _BASE_TABLE_RESERVE]
    chosen = {id(item[3]) for item in selected}
    for item in ranked:
        if len(selected) >= _SCHEMA_LIMIT:
            break
        if item[4] and id(item[3]) not in chosen:
            selected.append(item)
            chosen.add(id(item[3]))
    for item in ranked:
        if len(selected) >= _SCHEMA_LIMIT:
            break
        if id(item[3]) not in chosen:
            selected.append(item)
            chosen.add(id(item[3]))
    selected.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return [_plain_json(item[3]) for item in selected]


def _relevant_documents(raw_documents: list[object], rcm_row: object) -> list[object]:
    """Keep the six documents with the strongest lexical fit to one RCM row."""
    if not isinstance(rcm_row, Mapping):
        return [_plain_json(value) for value in raw_documents[:6]]
    query_parts = [
        rcm_row.get(key) for key in ("process", "risk", "control", "criteria")
    ]
    for attribute in rcm_row.get("control_attributes") or []:
        if isinstance(attribute, Mapping):
            query_parts.extend((attribute.get("key"), attribute.get("requirement")))
    query_tokens = set().union(*(relevance_tokens(item) for item in query_parts))
    ranked = []
    for index, raw in enumerate(raw_documents):
        if not isinstance(raw, Mapping):
            ranked.append((0, -index, raw))
            continue
        document_tokens = set().union(
            *(
                relevance_tokens(raw.get(key))
                for key in ("title", "source", "category", "summary")
            )
        )
        ranked.append((len(query_tokens & document_tokens), -index, raw))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    return [_plain_json(item[2]) for item in ranked[:6]]


def _generation_prompt_payload(request: WorkerRequest) -> dict[str, object]:
    """Project the durable bundle into the small model-facing generation input.

    The context manifest and bundle retain source identity, representations, and
    supplied-size metrics for provenance.  The model only needs the underlying
    audit material, so do not spend prompt tokens serializing that transport
    envelope on every generation or repair attempt.
    """
    planning = _resolved_item(request, "planning_context")
    if isinstance(planning, Mapping):
        planning = planning.get("context") or {}
    rcm_row = _resolved_item(request, GENERATE_ROW_SOURCE_ID)
    transaction_evidence = _resolved_item(
        request, GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID
    )
    raw_tables = _source_items(request, GENERATE_TABLE_SOURCE_ID)
    table_schemas = _relevant_table_schemas(
        raw_tables, rcm_row, transaction_evidence
    )
    raw_documents = _source_items(request, GENERATE_DOCUMENT_SOURCE_ID)
    attributes = (
        [
            item
            for item in rcm_row.get("control_attributes") or []
            if isinstance(item, Mapping)
        ]
        if isinstance(rcm_row, Mapping)
        else []
    )
    evidence_kinds = {str(item.get("evidence_kind") or "") for item in attributes}
    cycle_available = bool(
        isinstance(transaction_evidence, Mapping)
        and transaction_evidence.get("ruleset_id")
    )
    needs_documents = not attributes or bool(
        evidence_kinds & {"document_content", "manual_inspection", "inquiry", "mixed"}
    ) or not cycle_available
    documents = (
        _relevant_documents(raw_documents, rcm_row)
        if needs_documents
        else []
    )
    allowed_variants = []
    if table_schemas:
        allowed_variants.append("data")
    if documents:
        allowed_variants.append("document_question")
    if cycle_available:
        allowed_variants.append("cycle_vouch")
    if "cycle_vouch" in allowed_variants:
        variant_instruction = (
            "Cycle Vouch runs on the approved cycle rules in "
            "transaction_evidence. Choose the requirements it answers and the "
            "rows it runs over; the rules themselves are already settled."
        )
    else:
        variant_instruction = (
            "Cycle Vouch is forbidden because transaction_evidence supplies no "
            "prevalidated candidates. For a document_question omit kind and use "
            "steps shaped as {label, instruction, mode:'question', document_ids, "
            "question}; question may repeat instruction. Never return an empty "
            "tests array."
        )
    return {
        "target_rcm_row": rcm_row,
        "planning_context": planning,
        "table_schemas": table_schemas,
        "documents": documents,
        "transaction_evidence": _model_transaction_manifest(transaction_evidence),
        "methodology": _source_items(request, GENERATE_METHODOLOGY_SOURCE_ID),
        "allowed_test_variants": allowed_variants,
        "instructions": (
            "Generate complete executable tests for target_rcm_row only. Use "
            f"only allowed_test_variants. {variant_instruction}"
        ),
    }


def _json_payload(response: str) -> object:
    return decode_json_response(response)


def _context_metrics(request: WorkerRequest, worker_kind: str) -> dict:
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


def _repair_instruction(attempt: WorkerAttempt) -> str:
    """Tell the model to correct the exact prior candidate in place."""

    if not attempt.is_repair:
        raise WorkerContractError("Repair guidance requires a repair attempt.")
    return (
        "The preceding JSON response could not be used: "
        + "; ".join(attempt.validation_errors)
        + ". Correct those violations while preserving every unaffected test and "
        "field. Return the complete corrected JSON object."
    )


# Comparison methods stay the auditor's to change; generation always uses the
# established default rather than asking the model to pick one.
DEFAULT_COMPARISON_METHOD = "normalized"
assert DEFAULT_COMPARISON_METHOD in doc_tests.METHODS

# The citation fields a committed test persists in ``methodology_refs``.
_METHODOLOGY_REF_FIELDS = (
    "pack_id",
    "pack_name",
    "version",
    "sha1",
    "section",
    "citation",
)


# --------------------------------------------------------------------------- #
# tests.generate worker
#
# Replaces the retired two-pass ``tests.draft`` / ``tests.data_spec`` /
# ``tests.document_spec`` flow with one worker that decides source and writes
# the complete executable definition in a single turn, per the merge plan
# (docs/test-capability-merge-plan.md).
# --------------------------------------------------------------------------- #
GENERATE_WORKER_ID = "tests.generate"

# Phase 2 clean contract. The former broad-document-type ``vouch`` step is not
# an accepted response variant; transaction-cycle work is one canonical
# registry-backed test definition and ordinary document questions retain their
# existing shape.
GENERATE_RESPONSE_CONTRACT = {
    "envelope": {"required": ["tests"], "additional_fields": False},
    "variants": {
        "data": {
            "required": ["source", "title", "objective", "steps"],
            "source": "data",
        },
        "document_question": {
            "required": ["source", "title", "objective", "steps"],
            "source": "document",
            "step_mode": "question",
        },
        "cycle_vouch": {
            "required": [
                "source",
                "kind",
                "title",
                "objective",
                "requirement_refs",
                "procedure_key",
                "selection",
            ],
            "source": "document",
            "kind": "cycle_vouch",
            "forbidden": [
                "registry",
                "definition",
                "roles",
                "steps",
                "checks",
                "document_types",
                "assertions",
            ],
        },
    },
}
GENERATE_SYSTEM = f"""[agent:test_generate]
Generate the complete executable tests for exactly one supplied RCM row.
Return JSON with a non-empty `tests` array. A test is one of:

1. Data Test: source `data`, title, objective, and non-empty `steps`; each step
   has label, instruction, `population`, and self-contained Polars code
   assigning exception rows to `result`. Every step runs separately: it cannot
   use a variable made by another step. When more than one table is supplied,
   never use `df`; name the exact in-memory table variable or use
   `tables["exact_table_name"]`. Never import anything: `pl`, the table
   variables, and `tables['name']` are already available. Do not read files.
   For duration days use `.dt.total_days()`, not `.dt.days()` or `.dt.day()`.

   `population` names the table the step makes a statement *about* — one of the
   supplied schemas whose `derived` is false. Each supplied schema carries a
   `grain`: the population it holds one row of. Every join is a left join, so
   a frame's grain is its left-most base table and the step reaches only those
   rows of every other table that frame joins in. A step asserting about
   population A must therefore read a frame whose `grain` is A. Reading a
   requisition's approver from an invoice-grained frame tests the requisitions
   that happen to have an invoice and silently passes the rest, which is how a
   99M approval outside its limit went unreported: it belonged to a requisition
   that never became a purchase order, so no invoice-grained frame contained
   it. Join outward from the population you are asserting about; never anchor
   on one population to make a claim about another.
2. Document question: source `document`, title, objective, and non-empty
   question-mode steps using only supplied document ids. A missing-evidence step
   has an empty document_ids array and a specific missing_evidence string.
3. Cycle Vouch: source `document`, kind `cycle_vouch`, title, objective,
   requirement_refs, procedure_key, and selection. It has no assertions,
   definition, roles, or steps: the roles, the join keys and the assertions were
   approved in the cycle rules review, and local code reads them from there.

For Cycle Vouch you decide two things: which requirements belong in one
procedure, and which rows it runs over. Nothing else.

`transaction_evidence` carries the approved rules — the cycle label, the roles,
the anchor table and column the population is seeded from, and what the rules
reach across it. Do not repeat any of it: local code reads it from the ruleset
the test names.

requirement_refs use `<RCM id>:<control attribute key>` and must name control
attributes of this row whose evidence_kind is `transaction_cycle`. Every such
attribute must be referenced by some returned cycle test. Group requirements
that share one population into one test: a three-way match over an
invoice-grain population is one test, not four.

If the approved rules hold no assertion answering a requirement, the test is
refused locally and the gap is reported. Do not substitute a nearby comparison
and do not reference that requirement: return your other tests and leave it
uncovered, so the gap is reported rather than papered over.

Evidence-linked selection is targeted evidence only. Sampling uses random,
interval, or stratified with size 1..{cycle_vouching.MAX_ITEMS} and an integer
seed. assurance_scope is derived locally and may be omitted.

The exact Cycle Vouch response shape is:
{{"source":"document","kind":"cycle_vouch","title":"Three-way match of paid invoices",
"objective":"...","requirement_refs":["RCM-ID:attribute_key"],
"procedure_key":"invoice-three-way-match",
"selection":{{"mode":"evidence_linked"}}}}

Keep non-cycle attributes independent of cycle vocabulary. A tabular attribute
normally produces a Data Test; document-content, inspection, inquiry, and mixed
attributes use the evidence that is actually supplied. One durable test has one
source. Use only supplied table/column/document ids. {JSON_RULES} {LANGUAGE_RULES}"""

GENERATE_ROW_SOURCE_ID = "rcm_row"
GENERATE_METHODOLOGY_SOURCE_ID = "methodology"
GENERATE_TABLE_SOURCE_ID = "table_metadata"
GENERATE_DOCUMENT_SOURCE_ID = "documents"
GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID = "transaction_evidence"
_GENERATE_SOURCES = {"data", "document"}
_GENERATE_COMMON_FIELDS = ("source", "title", "objective", "steps")
_GENERATE_DATA_STEP_FIELDS = ("label", "instruction", "population", "code")
_GENERATE_DOCUMENT_STEP_FIELDS = (
    "label", "instruction", "mode", "document_ids", "question", "checks",
    "missing_evidence", "scope_limitation", "anchor_table", "anchor_key",
    "document_roles",
)
_GENERATE_MODES = {"question"}
_UNKNOWN_COLUMN_ERROR_RE = re.compile(
    r"ColumnNotFoundError: unable to find column [\"']([^\"']+)[\"']"
)
_UNDEFINED_NAME_ERROR_RE = re.compile(
    r"NameError: name [\"']([^\"']+)[\"'] is not defined"
)


def _generate_rcm_id(request: WorkerRequest) -> str:
    """Return the durable RCM id from the one supplied target-row item."""
    refs = [
        item.source_ref
        for item in request.context.items
        if item.source_id == GENERATE_ROW_SOURCE_ID
    ]
    if len(refs) != 1:
        raise WorkerContractError(
            "The test-generation context must supply exactly one target RCM row."
        )
    prefix, separator, rcm_id = str(refs[0]).partition(":")
    if prefix != "rcm" or not separator or not rcm_id:
        raise WorkerContractError(
            "The test-generation target row must be an 'rcm:<id>' reference."
        )
    return rcm_id


def _generate_methodology_refs(request: WorkerRequest) -> list[dict]:
    """Project the supplied methodology excerpts into durable citations."""
    refs = []
    for item in request.context.items:
        if item.source_id != GENERATE_METHODOLOGY_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            raise WorkerContractError(
                "Test-generation methodology context must supply citation objects."
            )
        refs.append(
            {key: content[key] for key in _METHODOLOGY_REF_FIELDS if key in content}
        )
    return refs


def _generate_supplied_tables(request: WorkerRequest) -> dict[str, dict[str, str]]:
    """Return supplied table schemas without loading workspace rows.

    The generation worker only receives declared context, never a workspace.  The
    dtype strings are enough to construct empty frames for a schema-only Polars
    validation pass below.
    """
    tables: dict[str, dict[str, str]] = {}
    for item in request.context.items:
        if item.source_id != GENERATE_TABLE_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            raise WorkerContractError("Table metadata context must supply objects.")
        name = str(content.get("table") or "").strip()
        if not name:
            continue
        tables[name] = {
            str(column.get("name") or ""): str(column.get("dtype") or "")
            for column in content.get("columns") or []
            if isinstance(column, Mapping) and str(column.get("name") or "")
        }
    return tables


def _generate_supplied_grains(request: WorkerRequest) -> dict[str, str]:
    """Each supplied frame's population, defaulting to the frame itself.

    A frame with no declared grain is treated as its own population, so a
    caller that supplies plain schemas — an older bundle, a fixture — keeps
    working and simply cannot fail the anchor rule.
    """
    grains: dict[str, str] = {}
    for item in request.context.items:
        if item.source_id != GENERATE_TABLE_SOURCE_ID:
            continue
        content = item.content
        if not isinstance(content, Mapping):
            continue
        name = str(content.get("table") or "").strip()
        if not name:
            continue
        grains[name] = str(content.get("grain") or name).strip() or name
    return grains


def _empty_schema_frames(tables: Mapping[str, Mapping[str, str]]) -> dict:
    """Build zero-row frames that preserve the supplied table schemas only."""
    return sandbox.empty_schema_frames(tables)


def _generate_supplied_document_ids(request: WorkerRequest) -> set[str]:
    ids = set()
    for item in request.context.items:
        if item.source_id != GENERATE_DOCUMENT_SOURCE_ID:
            continue
        prefix, separator, document_id = str(item.source_ref).partition(":")
        if prefix == "document" and separator and document_id:
            ids.add(document_id)
    return ids


def _generate_cycle_candidate(request: WorkerRequest) -> dict:
    """What a cycle test can be built on, or an empty mapping where nothing is.

    One candidate rather than a list: the anchor is part of what the auditor
    approved, so the only decision left to this turn is the selection.
    """

    value = _resolved_item(request, GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID)
    if not isinstance(value, Mapping):
        return {}
    resolved = _plain_json(value)
    return resolved if isinstance(resolved, dict) else {}


def _generate_rcm_row(request: WorkerRequest) -> dict:
    value = _resolved_item(request, GENERATE_ROW_SOURCE_ID)
    if not isinstance(value, Mapping):
        raise WorkerContractError("RCM-row context must supply one object.")
    return _plain_json(value)


def _validate_generate_cycle_test(
    path: str,
    value: Mapping[str, object],
    *,
    request: WorkerRequest,
    rcm_id: str,
    errors: list[str],
) -> dict | None:
    """Validate one proposed cycle test against this engagement's rules.

    The turn chooses almost nothing here. The roles, the join keys, the
    assertions and the anchor were approved in the cycle rules review, so what
    is left is which requirements the test answers and which rows it runs over
    — and both of those are checked, not taken on trust.
    """

    candidate = _generate_cycle_candidate(request)
    if not candidate or not candidate.get("ruleset_id"):
        errors.append(
            f"{path} cycle_vouch is unavailable because this engagement has no "
            "approved cycle ruleset; return an ordinary document question test "
            "with source 'document', no kind, and steps containing label, "
            "instruction, mode 'question', document_ids, and question. Never "
            "return an empty tests array"
        )
        return None
    for field, text in (
        ("objective", str(value.get("objective") or "")),
        ("selection_reason", str(value.get("selection_reason") or "")),
    ):
        if _PARTIAL_CYCLE_COVERAGE.search(text):
            errors.append(
                f"{path}.{field} admits that the proposed cycle procedure does "
                "not cover its referenced requirement; return no substitute. "
                "The RCM evidence strategy must be corrected or the missing "
                "evidence supplied."
            )
            return None
    selection = value.get("selection")
    if not isinstance(selection, Mapping):
        errors.append(
            f"{path}.selection must be an object such as "
            "{'mode':'evidence_linked'}"
        )
        return None
    requirement_refs = _plain_json(value.get("requirement_refs"))
    if not isinstance(requirement_refs, list) or not requirement_refs:
        errors.append(
            f"{path}.requirement_refs must name the '<RCM id>:<attribute key>' "
            "requirements this procedure answers"
        )
        return None
    rcm_row = _generate_rcm_row(request)
    comparisons = cycle_linking.required_comparisons_for(rcm_row, requirement_refs)
    if not comparisons:
        errors.append(
            f"{path}.requirement_refs cite no transaction_cycle control "
            "attribute of this row that states a comparison; reference the "
            "exact '<RCM id>:<attribute key>' the procedure answers"
        )
        return None
    return {
        "source": "document",
        "kind": "cycle_vouch",
        "title": str(value.get("title") or "").strip(),
        "objective": str(value.get("objective") or "").strip(),
        "rcm_id": rcm_id,
        "requirement_refs": requirement_refs,
        "procedure_key": str(value.get("procedure_key") or "").strip(),
        "definition": {
            "ruleset_id": str(candidate.get("ruleset_id") or ""),
            "population": {"selection": _plain_json(selection)},
        },
        "steps": [],
    }


def _generate_response_schema(response: str) -> Mapping[str, Any]:
    payload = _json_payload(response)
    if not isinstance(payload, dict):
        raise WorkerResponseValidationError("the response must be a JSON object")
    values = payload.get("tests")
    if not isinstance(values, list):
        raise WorkerResponseValidationError(
            "the response must be a JSON object with a `tests` array"
        )
    return {"tests": values}


def _import_bindings(node: ast.Import | ast.ImportFrom) -> set[str]:
    """The names one import statement introduces into the snippet namespace."""
    return {
        alias.asname or alias.name.split(".")[0] for alias in node.names
    }


def _redundant_imports(code: str, provided: set[str]) -> str:
    """Drop imports that only re-bind a name the sandbox already supplies.

    ``import polars as pl`` asks for something the snippet is handed anyway, so
    rejecting it spent a provider turn — the single most common cause of repair
    in observed runs — on a correction with exactly one possible outcome, and
    left that turn unavailable for the semantic errors that genuinely need the
    model. An import that reaches for anything else is a real attempt to widen
    the sandbox and is deliberately left in place for it to refuse. Only
    top-level statements are considered, and only when what remains still
    parses.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return code
    drop = {
        node.lineno
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and _import_bindings(node) <= provided
    }
    if not drop:
        return code
    kept = [
        line
        for number, line in enumerate(code.splitlines(), 1)
        if number not in drop
    ]
    stripped = "\n".join(kept).strip()
    try:
        ast.parse(stripped, mode="exec")
    except SyntaxError:
        return code
    return stripped


def _step_text(step: Mapping[str, object], *fallbacks: str) -> str:
    """The first non-empty candidate for a step's label or instruction.

    Generated steps sometimes omit this boilerplate while carrying the same
    sentence in ``question`` or ``instruction``. The value is descriptive, not
    executable, so deriving it locally is exact where a repair turn is merely
    likely — and it keeps that turn for errors only the model can fix.
    """
    for key in fallbacks:
        text = str(step.get(key) or "").strip()
        if text:
            return text
    return ""


def _code_loaded_names(code: str) -> set[str]:
    """Return names loaded by a syntactically valid generated snippet."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return set()
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _code_polars_columns(code: str) -> set[str]:
    """Return literal column names used by ``pl.col`` for repair guidance."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return set()
    columns: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "col"
            and isinstance(function.value, ast.Name)
            and function.value.id == "pl"
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            columns.add(first.value)
    return columns


def _tables_covering_columns(
    columns: set[str], known_tables: Mapping[str, Mapping[str, str]]
) -> list[str]:
    """Rank named frames that contain the referenced literal columns."""
    if not columns:
        return []
    return sorted(
        (
            table
            for table, schema in known_tables.items()
            if columns <= set(schema)
        ),
        key=lambda table: (len(known_tables[table]), table),
    )


def _code_frames(code: str, known_tables: Mapping[str, Mapping[str, str]]) -> set[str]:
    """Supplied frames a step reads, by bare name or ``tables['name']``."""
    frames = {name for name in _code_loaded_names(code) if name in known_tables}
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return frames
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "tables"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value in known_tables
        ):
            frames.add(node.slice.value)
    return frames


def _validate_step_population(
    path: str,
    step: Mapping[str, object],
    code: str,
    grains: Mapping[str, str],
    known_tables: Mapping[str, Mapping[str, str]],
    errors: list[str],
) -> None:
    """A step may only assert about the population its frames actually hold.

    The declaration is the model's; the check is arithmetic. A frame's grain is
    its left-most base table because every materialized join is a left join, so
    a step reading an invoice-grained frame sees only the requisitions that
    carry an invoice — 93 of 112 on the engagement this rule comes from, and
    the largest approval breach in the population was in the other 19.
    """

    populations = sorted({name for name, grain in grains.items() if grain == name})
    declared = str(step.get("population") or "").strip()
    if not declared:
        errors.append(
            f"{path}.population must name the table this step makes a statement "
            f"about; supplied populations: {', '.join(populations) or 'none'}"
        )
        return
    if declared not in known_tables:
        errors.append(
            f"{path}.population '{declared}' is not a supplied table; "
            f"supplied populations: {', '.join(populations) or 'none'}"
        )
        return
    if grains.get(declared, declared) != declared:
        errors.append(
            f"{path}.population '{declared}' is a derived frame over "
            f"'{grains[declared]}'; name the population itself"
        )
        return
    read = _code_frames(code, known_tables)
    if not read:
        return
    matching = sorted(frame for frame in read if grains.get(frame, frame) == declared)
    if matching:
        return
    available = sorted(
        frame for frame, grain in grains.items() if grain == declared
    )
    reached = ", ".join(
        f"'{frame}' (grain '{grains.get(frame, frame)}')" for frame in sorted(read)
    )
    errors.append(
        f"{path} declares population '{declared}' but reads {reached}, so it "
        f"asserts about '{declared}' from another population's rows and reaches "
        f"only those of '{declared}' that the join matched. Anchor the step on "
        f"one of: {', '.join(available)}"
    )


def _validate_generate_data_step(
    path: str,
    raw_step: object,
    known_tables: Mapping[str, Mapping[str, str]],
    grains: Mapping[str, str],
    errors: list[str],
) -> dict | None:
    if not isinstance(raw_step, Mapping):
        errors.append(f"{path} must be an object")
        return None
    step = _plain_json(raw_step)
    foreign = [key for key in _GENERATE_DOCUMENT_STEP_FIELDS if key in step and key not in _GENERATE_DATA_STEP_FIELDS]
    if foreign:
        errors.append(f"{path} has document-only field '{foreign[0]}' on a data step")
    label = _step_text(step, "label", "instruction")
    instruction = _step_text(step, "instruction", "label")
    if not label:
        errors.append(f"{path}.label must be a non-empty string")
    if not instruction:
        errors.append(f"{path}.instruction must be a non-empty string")
    code = step.get("code")
    if isinstance(code, str) and code.strip():
        code = _redundant_imports(code, {"pl", "tables", "df", *known_tables})
    if not isinstance(code, str) or not code.strip():
        errors.append(f"{path}.code must be non-empty Polars code")
    else:
        safe_code = True
        try:
            sandbox.validate(code)
        except ValueError as error:
            if "without assigning `result`" in str(error):
                errors.append(
                    f"{path}.code must assign the exception rows to `result`; "
                    "every step runs independently"
                )
            else:
                errors.append(f"{path}.code is not allowed in the sandbox: {error}")
            safe_code = False
        loaded_names = _code_loaded_names(code)
        if safe_code and len(known_tables) > 1 and "df" in loaded_names:
            columns = _code_polars_columns(code)
            suggested_tables = _tables_covering_columns(columns, known_tables)
            suggestion = (
                f" Likely named table(s): {', '.join(suggested_tables[:5])}."
                if suggested_tables
                else ""
            )
            errors.append(
                f"{path}.code uses ambiguous `df`, which means only the first "
                "supplied table. Use an exact named table variable or "
                f"tables['exact_name']; every step runs independently.{suggestion}"
            )
            safe_code = False
        if safe_code and known_tables:
            try:
                # Use Polars itself to resolve schemas created by the snippet.
                # A flat source-column check cannot see aliases introduced by
                # joins (for example, ``ITEM_DESCRIPTION_right``), which are
                # valid only after the preceding join expression.  Frames have
                # no rows, so this validates schema and expression semantics
                # without reading or exposing table data.
                sandbox.run(code, _empty_schema_frames(known_tables))
            except sandbox.SandboxError as error:
                unknown_column = _UNKNOWN_COLUMN_ERROR_RE.search(str(error))
                if unknown_column:
                    column = unknown_column.group(1)
                    containing = sorted(
                        table for table, schema in known_tables.items() if column in schema
                    )
                    location = (
                        f"; it exists in named table(s): {', '.join(containing)}"
                        if containing
                        else ""
                    )
                    errors.append(
                        f"{path}.code references unknown column '{column}'{location}"
                    )
                else:
                    undefined_name = _UNDEFINED_NAME_ERROR_RE.search(str(error))
                    if undefined_name:
                        errors.append(
                            f"{path}.code depends on undefined name "
                            f"'{undefined_name.group(1)}'; every step runs "
                            "independently and must build its full calculation "
                            "before assigning exception rows to `result`"
                        )
                    elif ".days" in code and "ExprDateTimeNameSpace" in str(error):
                        errors.append(
                            f"{path}.code uses unsupported `.dt.days()`; use "
                            "`.dt.total_days()` for a Polars duration"
                        )
                    else:
                        errors.append(
                            f"{path}.code cannot run against the supplied table "
                            f"schemas: {error}"
                        )
        _validate_step_population(path, step, code, grains, known_tables, errors)
    return {
        "label": label,
        "instruction": instruction,
        "population": str(step.get("population") or "").strip(),
        "code": str(code).strip() if isinstance(code, str) else "",
    }


def _validate_generate_document_step(
    path: str,
    raw_step: object,
    known_document_ids: set[str],
    errors: list[str],
) -> tuple[dict | None, str | None]:
    if not isinstance(raw_step, Mapping):
        errors.append(f"{path} must be an object")
        return None, None
    step = _plain_json(raw_step)
    foreign = [key for key in _GENERATE_DATA_STEP_FIELDS if key in step and key not in _GENERATE_DOCUMENT_STEP_FIELDS]
    if foreign:
        errors.append(f"{path} has data-only field '{foreign[0]}' on a document step")
    label = _step_text(step, "label", "instruction", "question")
    instruction = _step_text(step, "instruction", "question", "label")
    if not label:
        errors.append(f"{path}.label must be a non-empty string")
    if not instruction:
        errors.append(f"{path}.instruction must be a non-empty string")
    mode = step.get("mode")
    if mode == "vouch":
        errors.append(
            f"{path} uses the removed vouch-step schema; return a canonical "
            "cycle_vouch test definition"
        )
        return {"label": label, "instruction": instruction, "mode": ""}, None
    if mode in (None, ""):
        # Ordinary generated Document Tests have exactly one accepted mode.
        # Treat omitted discriminator boilerplate as that closed default rather
        # than spending a repair turn asking the model to repeat it.
        mode = "question"
    elif mode not in _GENERATE_MODES:
        errors.append(f"{path}.mode must be 'question'")
        mode = None
    document_ids = step.get("document_ids")
    if document_ids is None:
        document_ids = []
    if isinstance(document_ids, str):
        document_ids = [document_ids]
    if not isinstance(document_ids, (list, tuple)):
        errors.append(f"{path}.document_ids must be an array")
        document_ids = []
    document_ids = [str(value) for value in document_ids]
    unknown = [value for value in document_ids if value not in known_document_ids]
    if unknown:
        errors.append(f"{path} references unknown document '{unknown[0]}'")
    question = str(step.get("question") or step.get("instruction") or "").strip()
    missing_evidence = str(step.get("missing_evidence") or "").strip()
    scope_limitation = str(step.get("scope_limitation") or "").strip()
    normalized = {"label": label, "instruction": instruction, "mode": mode or ""}
    if mode == "question":
        if not question:
            errors.append(f"{path} needs a question")
        if step.get("checks"):
            errors.append(f"{path} has checks but mode is 'question'")
        for key in ("anchor_table", "anchor_key", "document_roles"):
            if step.get(key):
                errors.append(f"{path} has vouch-only field '{key}' on a question step")
    # Older prompts caused the model to use `missing_evidence` for a useful but
    # different statement: documents were reviewed, while some additional
    # evidence remained unavailable. Preserve that meaning as the explicit
    # sourced-question scope limitation instead of spending a repair turn on a
    # harmless field-name mismatch.
    if document_ids and missing_evidence:
        if not scope_limitation:
            scope_limitation = missing_evidence
        missing_evidence = ""
    if not document_ids and not missing_evidence:
        errors.append(f"{path} has no documents; missing_evidence must name what is required")
    normalized.update(
        document_ids=document_ids,
        missing_evidence=missing_evidence,
    )
    if scope_limitation:
        normalized["scope_limitation"] = scope_limitation
    if mode == "question":
        normalized["question"] = question
    return normalized, mode


def validate_generate_proposal(
    proposal: Mapping[str, Any],
    request: WorkerRequest,
) -> Mapping[str, Any]:
    """Apply the merged generation quality gate for one RCM row.

    Every violation across every proposed test and step is collected so one
    bounded repair turn can correct the complete response, per the merge
    plan's worker contract (section 4).
    """
    values = proposal.get("tests")
    if not isinstance(values, (list, tuple)) or not values:
        raise WorkerResponseValidationError("tests must be a non-empty array")
    rcm_id = _generate_rcm_id(request)
    methodology_refs = _generate_methodology_refs(request)
    known_tables = _generate_supplied_tables(request)
    table_grains = _generate_supplied_grains(request)
    known_document_ids = _generate_supplied_document_ids(request)
    available = {
        "data": bool(known_tables),
        "document": any(
            item.source_id == GENERATE_DOCUMENT_SOURCE_ID for item in request.context.items
        ),
    }
    errors: list[str] = []
    normalized: list[dict] = []
    # Which entries of ``normalized`` were built without any error of their own.
    # Tests on one row are independent records with independent semantic ids, so
    # a sibling's defect is not a reason to discard them.
    clean: list[dict] = []
    cycle_identities: set[tuple[str, str, str, str]] = set()
    attempted_refs: set[str] = set()
    for index, raw in enumerate(values, 1):
        path = f"tests[{index - 1}]"
        errors_before = len(errors)
        if not isinstance(raw, Mapping):
            errors.append(f"{path} must be an object")
            continue
        value = _plain_json(raw)
        source = value.get("source")
        if source not in _GENERATE_SOURCES:
            errors.append(f"{path}.source must be 'data' or 'document'")
            continue
        if not available[source]:
            errors.append(
                f"{path}.source is '{source}' but no "
                f"{'table' if source == 'data' else 'document'} is available; "
                "use the other source or return no test for this row"
            )
        for key in ("title", "objective"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                errors.append(f"{path}.{key} must be a non-empty string")
        if source == "document" and value.get("kind") == "cycle_vouch":
            forbidden = [
                key
                for key in (
                    "registry",
                    "definition",
                    "roles",
                    "steps",
                    "checks",
                    "document_types",
                )
                if value.get(key) not in (None, [], {})
            ]
            if forbidden:
                errors.append(
                    f"{path} cycle_vouch must not carry model-authored "
                    f"'{forbidden[0]}'; the roles, join keys and assertions come "
                    "from the approved cycle rules"
                )
            # What the response tried to cover, recorded before validation can
            # reject it. A cycle test refused for its own reason must not also
            # be reported as an absent one: the repair message then carries a
            # defect and its own consequence, telling the model to add a test
            # it has already written, and no rewrite can satisfy both.
            attempted_refs.update(
                str(reference).split(":", 1)[-1]
                for reference in value.get("requirement_refs") or []
            )
            cycle_test = _validate_generate_cycle_test(
                path, value, request=request, rcm_id=rcm_id, errors=errors
            )
            if cycle_test is not None:
                # One procedure over one population is one test. The population
                # comes from the approved anchor, so two cycle tests of this row
                # differ only by procedure key — and two with the same key would
                # be the same test written twice.
                identity = (
                    cycle_test["procedure_key"],
                    cycle_test["definition"]["ruleset_id"],
                )
                if identity in cycle_identities:
                    errors.append(
                        f"{path} duplicates a cycle procedure/population; group "
                        "its compatible assertions into one test"
                    )
                cycle_identities.add(identity)
                entry = {
                    **cycle_test,
                    "rcm_id": rcm_id,
                    "methodology_refs": methodology_refs,
                }
                normalized.append(entry)
                if len(errors) == errors_before:
                    clean.append(entry)
            continue
        if value.get("kind") is not None:
            errors.append(f"{path}.kind is valid only for cycle_vouch")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
            errors.append(f"{path}.steps must be a non-empty array")
            continue
        steps: list[dict] = []
        if source == "data":
            for step_index, raw_step in enumerate(raw_steps):
                step_path = f"{path}.steps[{step_index}]"
                normalized_step = _validate_generate_data_step(
                    step_path, raw_step, known_tables, table_grains, errors
                )
                if normalized_step is not None:
                    steps.append(normalized_step)
        else:
            modes: set[str] = set()
            for step_index, raw_step in enumerate(raw_steps):
                step_path = f"{path}.steps[{step_index}]"
                normalized_step, mode = _validate_generate_document_step(
                    step_path,
                    raw_step,
                    known_document_ids,
                    errors,
                )
                if normalized_step is not None:
                    steps.append(normalized_step)
                if mode:
                    modes.add(mode)
            if len(modes) > 1:
                errors.append(f"{path} mixes document modes {sorted(modes)}; return separate tests")
            # One vouch step is one cycle definition over one population. Two of
            # them in a single test would be two populations sharing one record's
            # items, conclusion, and coverage figures.
            if modes == {"vouch"} and len(raw_steps) > 1:
                errors.append(
                    f"{path} has {len(raw_steps)} vouch steps; a vouch test is one "
                    "cycle plan, so return separate tests"
                )
        entry = {
            "source": source,
            "title": str(value.get("title") or "").strip(),
            "objective": str(value.get("objective") or "").strip(),
            "steps": steps,
            "rcm_id": rcm_id,
            "methodology_refs": methodology_refs,
        }
        normalized.append(entry)
        if len(errors) == errors_before:
            clean.append(entry)
    coverage = _missing_cycle_coverage(
        request, rcm_id, normalized, attempted=attempted_refs
    )
    # Both row-level gaps: a transaction-cycle requirement with no cycle test,
    # and a population a requirement names with no step about it.
    row_level = coverage + _untested_named_populations(
        request, normalized, known_tables
    )
    errors.extend(row_level)
    if errors:
        # A row-level coverage gap is the one failure a partial commit must not
        # absorb: readiness is satisfied by any executable test, so committing
        # the siblings would mark the row done and retire the very unit that
        # still owes the missing one. Every other defect is scoped to its own
        # record, and its siblings are durable work worth keeping.
        raise WorkerResponseValidationError(
            errors,
            partial=({"tests": clean} if clean and not row_level else None),
        )
    return {"tests": normalized}


def _columns_corroborate_name(
    population: str,
    columns: Mapping[str, str],
    requirement_tokens: set[str],
) -> bool:
    """Whether a requirement reaches past a population's name into its columns.

    A table name shares words with a requirement for two very different
    reasons, and the shared words themselves cannot tell them apart.

    ``vendor_master_file`` against "duplicate vendor identities and bank
    details in the vendor master file" is a requirement naming the population
    it asserts about, and ``BANK_ACCOUNT_NUMBER`` says so a second time,
    independently of the name.

    ``financial_approval_matrix`` against "the purchase order date is on or
    after the financial approval date" is two ordinary words colliding. The
    requirement means a date column on the requisition; the table it matched
    is a four-row delegation matrix of job titles and approval limits, holding
    no date at all. Demanding a step at that grain asks for a test that cannot
    be written, and the row failed six generation attempts on it.

    So corroboration is scored only on the words the columns add — ``approval``
    in ``MAX_APPROVAL_AMOUNT`` is the collision restated, not a second
    witness — and a population whose schema offers nothing the requirement
    mentions is left alone. That trades away the rows a name alone would have
    caught, which is the right way round: a missed prompt costs one untested
    population, and a false one costs the whole row, permanently.
    """

    name_tokens = relevance_tokens(population)
    return any(
        (relevance_tokens(column) - name_tokens) & requirement_tokens
        for column in columns
    )


def _untested_named_populations(
    request: WorkerRequest,
    tests: list[dict],
    known_tables: Mapping[str, Mapping[str, str]],
) -> list[str]:
    """Populations a tabular requirement names by word that no step asserts about.

    The narrower half of the anchor rule. A step declares which population it
    is about and is checked against its frames; this checks the other
    direction — that a requirement naming a population by name produced a step
    about *that* population, rather than one over a frame that merely carries
    its columns.

    Deliberately scored on the attribute requirements alone, not the row's risk
    narrative. The risk sentence names every population the process touches,
    and demanding a step for each would reject rows whose tabular attributes
    are legitimately about one of them. A requirement that says "the maintained
    vendor records" is naming the population it is asserting about.

    A name match alone is not enough to prompt for the step: the population's
    own columns have to corroborate it, per `_columns_corroborate_name`.
    """

    try:
        row = _generate_rcm_row(request)
    except WorkerContractError:
        return []
    grains = _generate_supplied_grains(request)
    populations = {name for name, grain in grains.items() if grain == name}
    if not populations:
        return []
    requirement_tokens = set().union(
        *(
            relevance_tokens(attribute.get("requirement"))
            for attribute in row.get("control_attributes") or []
            if isinstance(attribute, Mapping)
            and attribute.get("evidence_kind") == "tabular_population"
        ),
        set(),
    )
    named = {
        population
        for population in populations
        if relevance_tokens(population) & requirement_tokens
        and _columns_corroborate_name(
            population, known_tables.get(population) or {}, requirement_tokens
        )
    }
    asserted = {
        str(step.get("population") or "")
        for test in tests
        if test.get("source") == "data"
        for step in test.get("steps") or []
    }
    return [
        f"a control attribute names population '{population}' and no data step "
        f"declares it; test '{population}' at its own grain rather than through "
        "a frame that only carries its columns"
        for population in sorted(named - asserted)
    ]


def _missing_cycle_coverage(
    request: WorkerRequest,
    rcm_id: str,
    tests: list[dict],
    *,
    attempted: set[str] | None = None,
) -> list[str]:
    """Name every transaction-cycle attribute the response left untested.

    A row states which of its requirements are answered by linking records
    rather than by querying a table, and the cycle test is the only executable
    form of that answer. Without this the flagship three-way-match attribute
    could be satisfied by a Polars join of the ledgers against themselves — a
    complete response by every structural rule, and no voucher examined.
    """

    try:
        row = _generate_rcm_row(request)
    except WorkerContractError:
        return []
    candidate = _generate_cycle_candidate(request)
    if not candidate or not candidate.get("ruleset_id"):
        return []
    covered = {
        str(reference).split(":", 1)[-1]
        for test in tests
        if test.get("kind") == "cycle_vouch"
        for reference in test.get("requirement_refs") or []
    } | (attempted or set())
    missing = [
        str(attribute.get("key") or "")
        for attribute in row.get("control_attributes") or []
        if isinstance(attribute, Mapping)
        and attribute.get("evidence_kind") == "transaction_cycle"
        and str(attribute.get("key") or "") not in covered
    ]
    return [
        f"{rcm_id} control attribute '{key}' declares transaction_cycle evidence "
        "and no returned cycle_vouch test references it; add a cycle test whose "
        f"requirement_refs contain '{rcm_id}:{key}', or group it into an existing "
        "one over the same population"
        for key in missing
    ]


def run_generate_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    payload = _generation_prompt_payload(request)
    user = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    allowed_variants = [str(value) for value in payload["allowed_test_variants"]]
    variant_gate = (
        "\n\n[agent:test_generate_variant_gate]\n"
        f"This unit allows only these variants: {', '.join(allowed_variants)}. "
        "Do not return any other variant. "
    )
    if "cycle_vouch" not in allowed_variants:
        variant_gate += (
            "The Cycle Vouch section above does not apply to this unit. Do not "
            "emit kind, selection, assertions, or definition. Return an "
            "ordinary data or document-question test as allowed, and never "
            "return an empty tests array."
        )
    elif allowed_variants == ["cycle_vouch"]:
        variant_gate += (
            "This is a Cycle Vouch-only unit. Use the exact narrowed Cycle "
            "Vouch response shape."
        )
    system = GENERATE_SYSTEM + variant_gate
    conversation = None
    if attempt.is_repair:
        if attempt.previous_response is None:
            raise WorkerContractError(
                "A test-generation repair requires the previous response."
            )
        conversation = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": attempt.previous_response},
            {"role": "user", "content": _repair_instruction(attempt)},
        ]
    return gateway.complete(
        system,
        user,
        _context_metrics(request, "test_generation"),
        attempt=attempt.number,
        conversation=conversation,
    )


GENERATE_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="tests.generate.response",
    schema_hash=_sha256_text(
        json.dumps(
            GENERATE_RESPONSE_CONTRACT,
            sort_keys=True,
            separators=(",", ":"),
        )
    ),
    validator=_generate_response_schema,
)
GENERATE_WORKER = WorkerDefinition(
    worker_id=GENERATE_WORKER_ID,
    prompt_hash=_sha256_text(GENERATE_SYSTEM),
    response_schema=GENERATE_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair generated test contract violations against the supplied RCM row."
        ),
    ),
    implementation=run_generate_worker,
    semantic_validator=validate_generate_proposal,
)

WORKERS.register(GENERATE_WORKER)


# --------------------------------------------------------------------------- #
# cycle linkage proposal
# --------------------------------------------------------------------------- #
LINKAGE_WORKER_ID = "tests.cycle_linkage"
CYCLE_SCHEMA_SOURCE_ID = "cycle_schemas"
CYCLE_REQUIREMENT_SOURCE_ID = "cycle_requirements"

LINKAGE_SYSTEM = f"""[agent:cycle_linkage]
Describe how this engagement's documents relate, as rules an auditor can review.

You are given the document types the engagement holds and the fields each one
carries. Propose the cycle they form.

roles name the positions in the cycle. A role is *not* a document type: it is a
place in the flow that a type fills, which is what lets one cycle hold two of the
same type — an original and a revised invoice, two counterparty confirmations.
Give each role a short lower_snake_case name.

anchor names where a cycle starts from a population row: the role and identifier
field a row in the accounting records would match.

join_keys say which document reaches which. Each names a field on one role that
should equal a field on another. **Only ever an identifier field** — a reference
that could tie two documents together. Never join on an amount or a date: two
records sharing a value there is a coincidence, not a link, and joining on one
would fuse unrelated transactions.

assertions say what must then agree once the documents are linked. These are the
audit tests: an amount that must match, a date that must not follow another, an
approval that must be present.

Propose only what the fields support. A rule naming a field a type does not carry
cannot run, and a plausible rule that never runs is worse than an absent one —
it reads as a passing test.

Where required comparisons are supplied, they are what the matrix has already
decided this cycle must demonstrate, and your assertions must answer every one
of them: an assertion reading exactly the two fields a comparison names, or
exactly the one field it names where it asks only that a field be stated. Answer
them in the matrix's own operands rather than equivalent ones — a receipt naming
its order and an order naming its receipt are different facts, and only one of
them is what was asked for.

A comparison whose pair a join key already binds needs no assertion: the join
established it, and repeating it files a check that cannot fail.

Give every rule a short lower_snake_case id and a one-sentence rationale saying
why it holds. The rationale is what an auditor reads when deciding whether to
approve it, so state the reason, not the mechanics.

{JSON_RULES} {LANGUAGE_RULES}
Keys:
  cycle_label   short name for the cycle
  roles         array of {{"name", "document_type", "cardinality": "one" | "many",
                "required": true | false}}
  anchor        {{"table", "column", "role", "field"}}
  join_keys     array of {{"id", "left": {{"role", "field"}},
                "right": {{"role", "field"}}, "rationale"}}
  assertions    array of {{"id", "label", "left": {{"role", "field"}},
                "right": {{"role", "field"}} or null, "requirement",
                "rationale"}}

requirement says what these fields must show for the control to hold, in the
terms the control is written in — "the invoice is settled for the amount the
order committed". Do not say how to compare them. Whether two values agree is
settled later against the values themselves, by a reader that can see one
document prints an amount with its currency and another without, and that one
scanned date carries a stray space. You are proposing rules for approval and
have read neither document.

Give an assertion a right operand when the requirement is that two fields
agree, and omit it — null — when the requirement is that a field be stated at
all."""


def _linkage_operand(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError(f"{label} must be an object.")
    role = str(raw.get("role") or "").strip()
    field = str(raw.get("field") or "").strip()
    if not role or not field:
        raise WorkerResponseValidationError(f"{label} needs a role and a field.")
    return {"role": role, "field": field}


def _linkage_response_schema(response: str) -> Mapping[str, Any]:
    payload = decode_json_response(response)
    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError("The linkage response must be an object.")
    roles = []
    for index, raw in enumerate(payload.get("roles") or []):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(f"roles[{index}] must be an object.")
        roles.append({
            "name": str(raw.get("name") or "").strip(),
            "document_type": str(raw.get("document_type") or "").strip(),
            "cardinality": str(raw.get("cardinality") or "one"),
            "required": bool(raw.get("required", True)),
        })
    if not roles:
        raise WorkerResponseValidationError("The response must name at least one role.")

    join_keys = []
    for index, raw in enumerate(payload.get("join_keys") or []):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(f"join_keys[{index}] must be an object.")
        join_keys.append({
            "id": str(raw.get("id") or "").strip(),
            "left": _linkage_operand(raw.get("left"), f"join_keys[{index}].left"),
            "right": _linkage_operand(raw.get("right"), f"join_keys[{index}].right"),
            "match": "normalized_equal",
            "rationale": str(raw.get("rationale") or "").strip(),
        })

    assertions = []
    for index, raw in enumerate(payload.get("assertions") or []):
        if not isinstance(raw, Mapping):
            raise WorkerResponseValidationError(f"assertions[{index}] must be an object.")
        right = raw.get("right")
        assertions.append({
            "id": str(raw.get("id") or "").strip(),
            "label": str(raw.get("label") or "").strip(),
            "left": _linkage_operand(raw.get("left"), f"assertions[{index}].left"),
            "right": (
                _linkage_operand(right, f"assertions[{index}].right")
                if right is not None
                else None
            ),
            "requirement": str(
                raw.get("requirement") or raw.get("rationale") or ""
            ).strip(),
            "rationale": str(raw.get("rationale") or "").strip(),
        })
    if not assertions:
        raise WorkerResponseValidationError(
            "The response must propose at least one assertion; a cycle that tests "
            "nothing is not a cycle."
        )

    anchor = payload.get("anchor")
    if not isinstance(anchor, Mapping):
        raise WorkerResponseValidationError("The response must name an anchor.")
    return {
        "cycle_label": str(payload.get("cycle_label") or "").strip(),
        "roles": roles,
        "anchor": {
            "table": str(anchor.get("table") or "").strip(),
            "column": str(anchor.get("column") or "").strip(),
            "role": str(anchor.get("role") or "").strip(),
            "field": str(anchor.get("field") or "").strip(),
        },
        "join_keys": join_keys,
        "assertions": assertions,
    }


def _supplied_schemas(request: WorkerRequest) -> list[object]:
    """The engagement's vocabulary, as one atomic supplied item.

    Read through one accessor by both the prompt and the validator, so what a
    proposal is judged against is exactly what it was shown.
    """

    item = _resolved_item(request, CYCLE_SCHEMA_SOURCE_ID)
    if isinstance(item, Mapping):
        return list(item.get("schemas") or [])
    return list(item or [])


def _supplied_requirements(request: WorkerRequest) -> list[dict]:
    """What the matrix asks this cycle to demonstrate, if it asks anything.

    Optional by declaration: an engagement may proposes rules before its matrix
    asks anything of them. Where it does ask, these are what the assertions have
    to answer, and they are read through one accessor by both the prompt and the
    validator so a proposal is judged against exactly what it was shown.
    """

    try:
        item = _resolved_item(request, CYCLE_REQUIREMENT_SOURCE_ID)
    except WorkerContractError:
        return []
    if isinstance(item, Mapping):
        return [
            dict(value)
            for value in item.get("required_comparisons") or []
            if isinstance(value, Mapping)
        ]
    return []


def validate_linkage_proposal(
    proposal: Mapping[str, Any], request: WorkerRequest
) -> Mapping[str, Any]:
    """Check the proposal against the schemas it was shown.

    The identifier rule is enforced here rather than left to the store because
    it is the one mistake worth spending a repair turn on: a join key on an
    amount reads perfectly and fuses every transaction sharing that amount.
    """

    schemas = {
        str(item.get("document_type")): {
            str(field.get("name")): field for field in item.get("fields") or []
        }
        for item in _supplied_schemas(request)
    }
    roles = {
        str(role.get("name")): str(role.get("document_type"))
        for role in proposal.get("roles") or []
    }
    for name, document_type in roles.items():
        if document_type not in schemas:
            raise WorkerResponseValidationError(
                f"Role '{name}' names '{document_type}', which this engagement "
                "has no schema for."
            )

    def field_of(operand: Mapping[str, Any], label: str) -> Mapping[str, Any]:
        role = str(operand.get("role"))
        if role not in roles:
            raise WorkerResponseValidationError(f"{label} names unknown role '{role}'.")
        field = schemas[roles[role]].get(str(operand.get("field")))
        if field is None:
            raise WorkerResponseValidationError(
                f"{label} names field '{operand.get('field')}', which "
                f"'{roles[role]}' does not carry."
            )
        return field

    for item in proposal.get("join_keys") or []:
        for side in ("left", "right"):
            field = field_of(item[side], f"Join key '{item['id']}' {side}")
            if str(field.get("role")) != "identifier":
                raise WorkerResponseValidationError(
                    f"Join key '{item['id']}' joins on '{field.get('name')}', which "
                    f"is a {field.get('role')} field. Only identifier fields can "
                    "join — two records sharing an amount is a coincidence, not a link."
                )
    for item in proposal.get("assertions") or []:
        field_of(item["left"], f"Assertion '{item['id']}' left")
        if item.get("right") is not None:
            field_of(item["right"], f"Assertion '{item['id']}' right")
        # The tolerance a numeric comparison used to carry is gone with the
        # operator that took one. A rule states what the fields must show; how
        # close two amounts have to be is part of that sentence, and is read
        # with the values rather than parsed out of the rule.
        if not str(item.get("requirement") or item.get("rationale") or "").strip():
            raise WorkerResponseValidationError(
                f"Assertion '{item['id']}' states no requirement. Say what these "
                "fields must show for the control to hold."
            )
    field_of(proposal["anchor"], "The anchor")
    _refuse_uncovered_requirements(proposal, request)
    return proposal


def _refuse_uncovered_requirements(
    proposal: Mapping[str, Any], request: WorkerRequest
) -> None:
    """Refuse a proposal that leaves a matrix requirement unanswered.

    The same check ``tests.specified`` runs, run here instead. It used to fire
    three stages downstream, by which time the ruleset had been approved and an
    approved ruleset is immutable — so the repair was a successor proposal and a
    second signature, for a defect the worker's own bounded loop could have
    fixed for one attempt. Nothing about the check changed; only when it is
    asked.

    Scoped to what the worker was shown. A requirement naming a document type
    this cycle does not carry is not this proposal's to answer.
    """

    required = _supplied_requirements(request)
    if not required:
        return
    document_types = {
        str(role.get("document_type") or "") for role in proposal.get("roles") or []
    }

    def in_scope(comparison: Mapping[str, Any]) -> bool:
        sides = [comparison.get("left"), comparison.get("right")]
        return all(
            str((side or {}).get("document_type") or "") in document_types
            for side in sides
            if isinstance(side, Mapping)
        )

    comparisons = [item for item in required if in_scope(item)]
    if not comparisons:
        return
    uncovered = cycle_linking.uncovered_comparisons(proposal, comparisons)
    if not uncovered:
        return
    named = "; ".join(
        f"{item.get('control_attribute')}.{item.get('comparison')} "
        f"({(item.get('left') or {}).get('document_type')}."
        f"{(item.get('left') or {}).get('field')}"
        + (
            f" with {(item.get('right') or {}).get('document_type')}."
            f"{(item.get('right') or {}).get('field')})"
            if item.get("right")
            else " stated at all)"
        )
        for item in uncovered
    )
    raise WorkerResponseValidationError(
        f"The proposal answers {counted(len(required) - len(uncovered), 'required comparison')} "
        f"of {len(required)}. Add an assertion for each of these, reading exactly "
        f"the fields named: {named}. An assertion that merely repeats a join key "
        "is not one of them — the join already binds that pair."
    )


def run_linkage_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Send the engagement's schemas and ask how its documents relate."""

    # Unwrapped, not just listed: ``WorkerRequest`` returns a recursively
    # immutable input, so each entry arrives as ``MappingProxyType`` and
    # ``json.dumps`` refuses it. The same shape stopped the RCM's schema
    # catalog reaching its authoring turn, and stayed invisible there because
    # the only catalog ever serialized was the empty one.
    payload = {
        "schemas": _plain_json(_supplied_schemas(request)),
        "tables": _plain_json(list(request.unit_input.get("tables") or [])),
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
            "worker_kind": "cycle_linkage",
            "total_characters": request.context.supplied_size.characters,
            "estimated_tokens": request.context.supplied_size.estimated_tokens,
            "selected_items": request.context.supplied_size.items,
        },
    )
    return gateway.complete(LINKAGE_SYSTEM, user, activity, attempt=attempt.number)


LINKAGE_RESPONSE_SCHEMA = WorkerResponseSchema(
    schema_id="tests.cycle_linkage.response",
    schema_hash=_sha256_text("tests-cycle-linkage-response:roles-joins-assertions"),
    validator=_linkage_response_schema,
)
LINKAGE_WORKER = WorkerDefinition(
    worker_id=LINKAGE_WORKER_ID,
    prompt_hash=_sha256_text(LINKAGE_SYSTEM),
    response_schema=LINKAGE_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=1,
        guidance_hash=_sha256_text(
            "Repair the cycle linkage proposal against the supplied schemas."
        ),
    ),
    implementation=run_linkage_worker,
    semantic_validator=validate_linkage_proposal,
)

WORKERS.register(LINKAGE_WORKER)

__all__ = [
    "DEFAULT_COMPARISON_METHOD",
    "GENERATE_RESPONSE_SCHEMA",
    "GENERATE_RESPONSE_CONTRACT",
    "GENERATE_SYSTEM",
    "GENERATE_WORKER",
    "GENERATE_WORKER_ID",
    "run_generate_worker",
    "validate_generate_proposal",
]
