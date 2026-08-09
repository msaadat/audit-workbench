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
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import cycle_vouching, doc_tests, sandbox
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
    """Project only decision-relevant manifest facts into the model prompt.

    The full manifest remains in the bounded worker context and is used by the
    deterministic semantic gate.  Record IDs, document IDs, registry copies,
    hashes, and diagnostic ranking internals do not help the model choose a
    candidate or author valid field assertions.
    """
    if not isinstance(value, Mapping):
        return value
    groups = []
    candidate_fields = (
        "candidate_id",
        "rank",
        "table",
        "source_kind",
        "join_justification",
        "row_key",
        "cycle_keys",
        "column_types",
        "population_rows",
        "linked_rows",
        "local_coverage",
        "collision_count",
        "complete_cycle_count",
        "reachable_record_kinds",
        "reachable_roles",
        "required_role_coverage",
        "required_role_count",
        "missing_role_counts",
        "relationship_facts",
    )
    for raw_group in value.get("groups") or []:
        if not isinstance(raw_group, Mapping):
            continue
        candidates = [
            {
                key: _plain_json(candidate[key])
                for key in candidate_fields
                if key in candidate
            }
            for candidate in raw_group.get("candidates") or []
            if isinstance(candidate, Mapping)
        ]
        records = [
            {
                "record_kind": str(record.get("record_kind") or ""),
                "available_fields": _plain_json(record.get("available_fields") or []),
            }
            for record in raw_group.get("records") or []
            if isinstance(record, Mapping)
        ]
        groups.append(
            {
                key: _plain_json(raw_group[key])
                for key in (
                    "registry",
                    "requirement_refs",
                    "required_record_kinds",
                    "roles",
                )
                if key in raw_group
            }
            | {"candidates": candidates, "records": records}
        )
    return {
        "groups": groups,
        "manifest_sha256": str(value.get("manifest_sha256") or ""),
    }


def _schema_relevance_tokens(value: object) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if len(token) > 2
    }


def _relevant_table_schemas(
    raw_tables: list[object], rcm_row: object, transaction_manifest: object
) -> list[object]:
    """Keep a bounded, deterministic set of schemas relevant to one RCM row."""
    if not isinstance(rcm_row, Mapping):
        return [_plain_json(value) for value in raw_tables[:6]]
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
    query_tokens = set().union(*(_schema_relevance_tokens(item) for item in query_parts))
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
            ranked.append((0, 0, -index, raw))
            continue
        table = str(raw.get("table") or "")
        table_tokens = _schema_relevance_tokens(table)
        column_tokens = set().union(
            *(
                _schema_relevance_tokens(column.get("name"))
                for column in raw.get("columns") or []
                if isinstance(column, Mapping)
            ),
            set(),
        )
        score = 4 * len(query_tokens & table_tokens) + len(query_tokens & column_tokens)
        ranked.append((1 if table in candidate_tables else 0, score, -index, raw))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return [_plain_json(item[3]) for item in ranked[:6]]


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
    query_tokens = set().union(*(_schema_relevance_tokens(item) for item in query_parts))
    ranked = []
    for index, raw in enumerate(raw_documents):
        if not isinstance(raw, Mapping):
            ranked.append((0, -index, raw))
            continue
        document_tokens = set().union(
            *(
                _schema_relevance_tokens(raw.get(key))
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
    candidate_ids = _manifest_candidate_ids(transaction_evidence)
    needs_documents = not attributes or bool(
        evidence_kinds & {"document_content", "manual_inspection", "inquiry", "mixed"}
    ) or not candidate_ids
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
    if candidate_ids:
        allowed_variants.append("cycle_vouch")
    if "cycle_vouch" in allowed_variants:
        variant_instruction = (
            "Cycle Vouch may be used only with an exact candidate_id from "
            "transaction_evidence."
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
    value = str(response or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        value,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        value = fenced.group(1).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise WorkerResponseValidationError("the response is not a valid JSON object")


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
                "candidate_id",
                "selection_reason",
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
   has label, instruction, and self-contained Polars code assigning exception
   rows to `result`. Every step runs separately: it cannot use a variable made
   by another step. When more than one table is supplied, never use `df`; name
   the exact in-memory table variable or use `tables["exact_table_name"]`. Never
   import anything: `pl`, the table variables, and `tables['name']` are already
   available. Do not read files. For duration days use `.dt.total_days()`, not
   `.dt.days()` or `.dt.day()`.
2. Document question: source `document`, title, objective, and non-empty
   question-mode steps using only supplied document ids. A missing-evidence step
   has an empty document_ids array and a specific missing_evidence string.
3. Cycle Vouch: source `document`, kind `cycle_vouch`, title, objective,
   requirement_refs, procedure_key, candidate_id, selection_reason, and
   selection. It has no assertions, registry, definition, roles, or steps. Every
   comparison the procedure performs is derived locally from the
   required_comparisons the referenced control attributes already carry, and the
   registry-backed fields are hydrated from the chosen manifest candidate.

For Cycle Vouch you decide two things: which population to vouch, and which
requirements belong in one procedure. Nothing else.

The supplied `transaction_evidence` manifest is authoritative. Choose an exact
candidate_id copied from one supplied registry group and explain the choice in
selection_reason. Candidates carry `rank`; among candidates over the same `table`
only the best-ranked one is accepted, because they are the same rows keyed
differently and a table keyed on another record's identifier is a grain error.
Choosing a different `table` is a real decision — make it on which grain the
requirement is about — and say so in selection_reason. Do not repeat the
candidate's table, row_key, cycle_keys, registry, or roles; local code copies
them exactly.

requirement_refs use `<RCM id>:<control attribute key>` and must name control
attributes of this row whose evidence_kind is `transaction_cycle`. Every such
attribute must be referenced by some returned cycle test. Group requirements
that share one population and lifecycle scope into one test: a three-way match
over an invoice-grain population is one test, not four. Do not invent
identifiers, fields, mappings, or roles, and do not emit assertions,
dotted paths, checks, document_types, or a vouch mode step.

Evidence-linked selection is targeted evidence only. Sampling uses random,
interval, or stratified with size 1..{cycle_vouching.MAX_ITEMS} and an integer
seed. assurance_scope is derived locally and may be omitted.

The exact Cycle Vouch response shape is:
{{"source":"document","kind":"cycle_vouch","title":"Three-way match of paid invoices",
"objective":"...","requirement_refs":["RCM-ID:attribute_key"],
"procedure_key":"invoice-three-way-match",
"candidate_id":"CYCLE-CAND-EXACTLY-AS-SUPPLIED","selection_reason":"...",
"selection":{{"mode":"evidence_linked"}}}}

Keep non-cycle attributes independent of cycle vocabulary. A tabular attribute
normally produces a Data Test; document-content, inspection, inquiry, and mixed
attributes use the evidence that is actually supplied. One durable test has one
source. Use only supplied table/column/document ids. {JSON_RULES}"""

GENERATE_ROW_SOURCE_ID = "rcm_row"
GENERATE_METHODOLOGY_SOURCE_ID = "methodology"
GENERATE_TABLE_SOURCE_ID = "table_metadata"
GENERATE_DOCUMENT_SOURCE_ID = "documents"
GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID = "transaction_evidence"
_GENERATE_SOURCES = {"data", "document"}
_GENERATE_COMMON_FIELDS = ("source", "title", "objective", "steps")
_GENERATE_DATA_STEP_FIELDS = ("label", "instruction", "code")
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


def _empty_frame_dtype(dtype: str):
    """Map the compact context dtype into a safe, useful empty-frame dtype."""
    normalized = dtype.casefold().replace(" ", "")
    if normalized.startswith("uint"):
        return sandbox.pl.UInt64
    if normalized.startswith("int"):
        return sandbox.pl.Int64
    if normalized.startswith(("float", "decimal")):
        return sandbox.pl.Float64
    if normalized in {"bool", "boolean"}:
        return sandbox.pl.Boolean
    if normalized == "date":
        return sandbox.pl.Date
    if normalized.startswith("datetime"):
        return sandbox.pl.Datetime
    if normalized == "time":
        return sandbox.pl.Time
    return sandbox.pl.String


def _empty_schema_frames(tables: Mapping[str, Mapping[str, str]]) -> dict:
    """Build zero-row frames that preserve the supplied table schemas only."""
    return {
        table: sandbox.pl.DataFrame(
            schema={column: _empty_frame_dtype(dtype) for column, dtype in columns.items()}
        )
        for table, columns in tables.items()
    }


def _generate_supplied_document_ids(request: WorkerRequest) -> set[str]:
    ids = set()
    for item in request.context.items:
        if item.source_id != GENERATE_DOCUMENT_SOURCE_ID:
            continue
        prefix, separator, document_id = str(item.source_ref).partition(":")
        if prefix == "document" and separator and document_id:
            ids.add(document_id)
    return ids


def _generate_transaction_manifest(request: WorkerRequest) -> dict:
    value = _resolved_item(request, GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID)
    if not isinstance(value, Mapping):
        raise WorkerContractError(
            "Transaction-evidence context must supply one manifest object."
        )
    return _plain_json(value)


def _manifest_candidate_matches(
    manifest: Mapping[str, object], candidate_id: object
) -> list[tuple[dict, dict]]:
    """Return exact group/candidate matches from the supplied local manifest."""
    matches: list[tuple[dict, dict]] = []
    for raw_group in manifest.get("groups") or []:
        if not isinstance(raw_group, Mapping):
            continue
        group = _plain_json(raw_group)
        assert isinstance(group, dict)
        for raw_candidate in group.get("candidates") or []:
            if not isinstance(raw_candidate, Mapping):
                continue
            candidate = _plain_json(raw_candidate)
            assert isinstance(candidate, dict)
            if candidate.get("candidate_id") == candidate_id:
                matches.append((group, candidate))
    return matches


def _manifest_candidate_ids(manifest: Mapping[str, object]) -> list[str]:
    """List exact eligible IDs in stable order for actionable repair guidance."""
    return sorted(
        {
            str(candidate.get("candidate_id") or "")
            for group in manifest.get("groups") or []
            if isinstance(group, Mapping)
            for candidate in group.get("candidates") or []
            if isinstance(candidate, Mapping) and candidate.get("candidate_id")
        }
    )


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
    manifest = _generate_transaction_manifest(request)
    eligible_ids = _manifest_candidate_ids(manifest)
    eligible_text = ", ".join(eligible_ids) if eligible_ids else "none"
    if not eligible_ids:
        errors.append(
            f"{path} cycle_vouch is unavailable because transaction_evidence "
            "supplies no prevalidated candidates; return an ordinary document "
            "question test with source 'document', no kind, and steps containing "
            "label, instruction, mode 'question', document_ids, and question. "
            "Never return an empty tests array"
        )
        return None
    candidate_id = value.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        errors.append(
            f"{path}.candidate_id must copy exactly one supplied candidate ID; "
            f"eligible IDs: {eligible_text}"
        )
        return None
    matches = _manifest_candidate_matches(manifest, candidate_id)
    if len(matches) != 1:
        errors.append(
            f"{path}.candidate_id '{candidate_id}' is not an exact prevalidated "
            f"candidate; eligible IDs: {eligible_text}"
        )
        return None
    group, selected = matches[0]
    selection_reason = value.get("selection_reason")
    if not isinstance(selection_reason, str) or not selection_reason.strip():
        errors.append(f"{path}.selection_reason must be a non-empty string")
        return None
    selection = value.get("selection")
    if not isinstance(selection, Mapping):
        errors.append(
            f"{path}.selection must be an object such as "
            "{'mode':'evidence_linked'}"
        )
        return None
    for field, text in (
        ("objective", str(value.get("objective") or "")),
        ("selection_reason", selection_reason),
    ):
        if _PARTIAL_CYCLE_COVERAGE.search(text):
            errors.append(
                f"{path}.{field} admits that the proposed cycle procedure does "
                "not cover its referenced requirement; return no substitute. "
                "The RCM evidence strategy must be corrected or the missing "
                "evidence supplied."
            )
            return None
    requirement_refs = _plain_json(value.get("requirement_refs"))
    rcm_row = _generate_rcm_row(request)
    try:
        assertions = cycle_vouching.compile_required_assertions(
            rcm_row=rcm_row,
            requirement_refs=(
                requirement_refs if isinstance(requirement_refs, list) else []
            ),
            group=group,
        )
    except cycle_vouching.CycleSchemaError as error:
        errors.append(f"{path} {error}")
        return None
    if not assertions:
        errors.append(
            f"{path}.requirement_refs cite no transaction_cycle control "
            "attribute of this row that carries required comparisons; reference "
            "the exact '<RCM id>:<attribute key>' the procedure answers"
        )
        return None
    candidate = {
        "source": "document",
        "kind": "cycle_vouch",
        "schema_version": cycle_vouching.SCHEMA_VERSION,
        "rcm_id": rcm_id,
        "registry": group.get("registry"),
        "requirement_refs": requirement_refs,
        "procedure_key": value.get("procedure_key"),
        "definition": {
            "population": {
                "candidate_id": candidate_id,
                "selection_reason": selection_reason.strip(),
                "table": selected.get("table"),
                "row_key": _plain_json(selected.get("row_key")),
                "cycle_keys": _plain_json(selected.get("cycle_keys")),
                "selection": _plain_json(selection),
            },
            "roles": _plain_json(group.get("roles")),
            "assertions": assertions,
        },
        "steps": [],
    }
    try:
        validated = cycle_vouching.validate_cycle_test_semantics(
            candidate, rcm_row=rcm_row, manifest=manifest
        )
        group = cycle_vouching.manifest_group_for_test(validated, manifest)
        population = validated["definition"]["population"]
        hydrated_candidate = next(
            item
            for item in group["candidates"]
            if item["candidate_id"] == population["candidate_id"]
        )
        confirmation = (
            cycle_vouching.selection_confirmation(hydrated_candidate)
            if population["selection"].get("mode") == "evidence_linked"
            else None
        )
        if confirmation is not None:
            # The eligible reach exceeds the item cap, so the proposal carries
            # the deterministic sample the auditor confirms or adjusts at
            # approval. The confirmation travels with the test so the durable
            # record stays distinguishable from a freely chosen sample.
            population["selection"] = dict(confirmation["suggested_selection"])
            validated = cycle_vouching.validate_cycle_test_semantics(
                validated, rcm_row=rcm_row, manifest=manifest
            )
            validated["selection_confirmation"] = confirmation
    except (cycle_vouching.CycleSchemaError, StopIteration) as error:
        errors.append(f"{path} {error}")
        return None
    if not validated["definition"]["assertions"]:
        errors.append(f"{path}.definition.assertions must not be empty")
    return {
        "source": "document",
        "kind": "cycle_vouch",
        "title": str(value.get("title") or "").strip(),
        "objective": str(value.get("objective") or "").strip(),
        "registry": validated["registry"],
        "requirement_refs": validated["requirement_refs"],
        "procedure_key": validated["procedure_key"],
        "definition": validated["definition"],
        "context_manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "selection_confirmation": validated.get("selection_confirmation"),
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


def _validate_generate_data_step(
    path: str, raw_step: object, known_tables: Mapping[str, Mapping[str, str]], errors: list[str]
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
    return {
        "label": label,
        "instruction": instruction,
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
                    f"'{forbidden[0]}'; local code hydrates registry, population, "
                    "and roles from candidate_id"
                )
            cycle_test = _validate_generate_cycle_test(
                path, value, request=request, rcm_id=rcm_id, errors=errors
            )
            if cycle_test is not None:
                population = cycle_test["definition"]["population"]
                identity = (
                    cycle_test["procedure_key"],
                    population["table"],
                    population["row_key"]["column"],
                    population["row_key"]["identifier_kind"],
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
                normalized_step = _validate_generate_data_step(step_path, raw_step, known_tables, errors)
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
    coverage = _missing_cycle_coverage(request, rcm_id, normalized)
    errors.extend(coverage)
    if errors:
        # A row-level coverage gap is the one failure a partial commit must not
        # absorb: readiness is satisfied by any executable test, so committing
        # the siblings would mark the row done and retire the very unit that
        # still owes a cycle test. Every other defect is scoped to its own
        # record, and its siblings are durable work worth keeping.
        raise WorkerResponseValidationError(
            errors,
            partial=({"tests": clean} if clean and not coverage else None),
        )
    return {"tests": normalized}


def _missing_cycle_coverage(
    request: WorkerRequest, rcm_id: str, tests: list[dict]
) -> list[str]:
    """Name every transaction-cycle attribute the response left untested.

    A row states which of its requirements are answered by linking records
    rather than by querying a table, and the cycle test is the only executable
    form of that answer. Without this the flagship three-way-match attribute
    could be satisfied by a Polars join of the ledgers against themselves — a
    complete response by every structural rule, and no voucher examined.
    """

    try:
        manifest = _generate_transaction_manifest(request)
        row = _generate_rcm_row(request)
    except WorkerContractError:
        return []
    if not _manifest_candidate_ids(manifest):
        return []
    covered = {
        str(reference).split(":", 1)[-1]
        for test in tests
        if test.get("kind") == "cycle_vouch"
        for reference in test.get("requirement_refs") or []
    }
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
            "emit kind, candidate_id, selection, assertions, registry, or "
            "definition. Return an ordinary data or document-question test as "
            "allowed, and never return an empty tests array."
        )
    elif allowed_variants == ["cycle_vouch"]:
        variant_gate += (
            "This is a Cycle Vouch-only unit. Use the exact narrowed Cycle Vouch "
            "response shape and an exact supplied candidate_id."
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
    implementation_hash=_sha256_text(
        inspect.getsource(run_generate_worker)
        + inspect.getsource(_generation_prompt_payload)
        + inspect.getsource(_model_transaction_manifest)
        + inspect.getsource(_relevant_table_schemas)
        + inspect.getsource(_relevant_documents)
        + inspect.getsource(_schema_relevance_tokens)
        + inspect.getsource(_manifest_candidate_ids)
    ),
    prompt_hash=_sha256_text(GENERATE_SYSTEM),
    response_schema=GENERATE_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair generated test contract violations against the supplied RCM row."
        ),
    ),
    implementation=run_generate_worker,
    semantic_validation_hash=_sha256_text(
        inspect.getsource(validate_generate_proposal)
        + inspect.getsource(_validate_generate_cycle_test)
        + inspect.getsource(_validate_generate_data_step)
        + inspect.getsource(_validate_generate_document_step)
        + inspect.getsource(_manifest_candidate_matches)
        + inspect.getsource(_manifest_candidate_ids)
        + inspect.getsource(_code_loaded_names)
        + inspect.getsource(_code_polars_columns)
        + inspect.getsource(_redundant_imports)
        + inspect.getsource(_step_text)
        + inspect.getsource(_tables_covering_columns)
        + inspect.getsource(cycle_vouching.compile_required_assertions)
        + inspect.getsource(cycle_vouching.validate_cycle_test_semantics)
    ),
    semantic_validator=validate_generate_proposal,
)

WORKERS.register(GENERATE_WORKER)


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
