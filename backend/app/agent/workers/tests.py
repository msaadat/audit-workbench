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

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any

from ... import doc_tests, document_analysis, sandbox
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
    raw_tables = _source_items(request, GENERATE_TABLE_SOURCE_ID)
    table_schemas = []
    anchor_candidates = []
    for raw in raw_tables:
        if not isinstance(raw, Mapping):
            table_schemas.append(raw)
            continue
        anchor_candidates.extend(
            dict(item)
            for item in raw.get("vouch_anchor_candidates") or []
            if isinstance(item, Mapping)
        )
        table_schemas.append(
            {
                str(key): _plain_json(value)
                for key, value in raw.items()
                if key != "vouch_anchor_candidates"
            }
        )
    raw_documents = _source_items(request, GENERATE_DOCUMENT_SOURCE_ID)
    documents = []
    vouch_documents = []
    for raw in raw_documents:
        if not isinstance(raw, Mapping):
            documents.append(raw)
            continue
        profile = raw.get("vouch_profile")
        if isinstance(profile, Mapping) and profile.get("document_type"):
            vouch_documents.append(_plain_json(profile))
        documents.append(
            {
                str(key): _plain_json(value)
                for key, value in raw.items()
                if key != "vouch_profile"
            }
        )
    # Which supplied documents are transaction evidence is the single fact that
    # decides whether a cycle test is possible, and it is otherwise buried in a
    # per-document category field. Surfacing it is context the model lacks, not a
    # thumb on the scale: the model still chooses the mode.
    evidence = [
        str(item.get("id") or "")
        for item in raw_documents
        if isinstance(item, Mapping)
        and (
            str(item.get("category") or "") == _TRANSACTION_EVIDENCE_CATEGORY
            or bool(item.get("vouch_profile"))
        )
    ]
    available_document_types = sorted(
        {
            str(item.get("document_type") or "")
            for item in vouch_documents
            if str(item.get("document_type") or "")
        }
    )
    return {
        "target_rcm_row": _resolved_item(request, GENERATE_ROW_SOURCE_ID),
        "planning_context": planning,
        "table_schemas": table_schemas,
        "documents": documents,
        "transaction_evidence": {
            "document_ids": evidence,
            "documents": vouch_documents,
            "available_document_types": available_document_types,
            "anchor_candidates": anchor_candidates,
            "note": (
                "These documents carry an extracted structured record — "
                "identifiers, parties, dates, amounts, line items, approvals, and "
                "attachments — that a vouch check resolves its paths against."
            )
            if evidence
            else "No transaction evidence is available; a vouch test is not possible.",
        },
        "methodology": _source_items(request, GENERATE_METHODOLOGY_SOURCE_ID),
        "instructions": "Generate complete executable tests for target_rcm_row only.",
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

# Machine-readable authored contract. The prose below explains audit method;
# this descriptor owns the response variants and is also the response-schema
# identity. Keeping discriminator shapes in data makes the unary/binary and
# question/evidence distinctions inspectable and testable without growing a
# second hand-written pseudo-schema.
GENERATE_RESPONSE_CONTRACT = {
    "envelope": {"required": ["tests"], "additional_fields": False},
    "test": {
        "discriminator": ["source", "steps[].mode"],
        "required": ["source", "title", "objective", "steps"],
        "additional_fields": False,
        "variants": {
            "data": {
                "when": {"source": "data"},
                "step": {
                    "required": ["label", "instruction", "code"],
                    "additional_fields": False,
                }
            },
            "document_question": {
                "when": {"source": "document", "mode": "question"},
                "step": {
                    "required": [
                        "label",
                        "instruction",
                        "mode",
                        "document_ids",
                        "question",
                    ],
                    "mode": "question",
                    "variants": {
                        "with_sources": {
                            "document_ids": "non_empty",
                            "optional": ["scope_limitation"],
                            "forbidden": ["missing_evidence"],
                        },
                        "missing_evidence": {
                            "document_ids": "empty",
                            "required": ["missing_evidence"],
                            "forbidden": ["scope_limitation"],
                        },
                    },
                    "additional_fields": False,
                }
            },
            "document_vouch": {
                "when": {"source": "document", "mode": "vouch"},
                "step": {
                    "required": [
                        "label",
                        "instruction",
                        "mode",
                        "anchor_table",
                        "anchor_key",
                        "document_roles",
                        "checks",
                    ],
                    "mode": "vouch",
                    "count": 1,
                    "check_variants": {
                        "present": {
                            "required": ["field", "left", "method"],
                            "method": "present",
                            "forbidden": ["right", "tolerance"],
                        },
                        "binary": {
                            "required": ["field", "left", "right", "method"],
                            "methods": sorted(
                                set(doc_tests.METHODS) - set(doc_tests.UNARY_METHODS)
                            ),
                            "optional": ["tolerance"],
                        },
                    },
                    "document_types": list(
                        document_analysis.VOUCHER_DOCUMENT_TYPES
                    ),
                    "additional_fields": False,
                }
            },
        },
    },
}

GENERATE_SYSTEM = f"""[agent:test_generate]
Generate the complete, executable tests that cover exactly one supplied RCM
row. Return an object with `tests`, a discriminated union. Each test has
exactly these fields, all required:
  source      "data" if the test is answered by analysing an imported table,
              "document" if it is answered by reading documents
  title       short name for the test
  objective   what the test establishes about the control
  steps       array of step objects, the complete executable procedure

One durable test has one source. If a row needs both data analysis and
document inspection, return two tests — one "data" and one "document". A
Document Test has one execution mode; if a row needs both a question and a
comparison, return two Document Tests.

A test obtains evidence about whether the control in the row operated. It is
not a restatement of the risk, and not a re-confirmation of something the
supplied material already concludes: where the planning basis already reports
that a policy is silent on a point, establishing that again yields no evidence
about a control. Where the row records that no control was identified, do not
invent one to test — test whether the exposure the risk describes is present in
the records themselves.

Say what the test runs over. The objective names the population — which
records, in what state, over what period — and the steps operate on all of it.
A data test runs across the whole population rather than a sample, so narrow it
only where the control genuinely applies to a subset, and say which subset in
the step's instruction when it does.

An exception is a record that contradicts the control having operated. It is
not merely a record that is large, unusual, or incomplete. Before treating an
absent value as an exception, decide whether it is a legitimate state of the
process rather than a failure of it: a record that is still in progress, or was
cancelled or rejected, is routinely missing a downstream field by design, and
excluding those states is part of defining the population.

Where the supplied material cannot answer the row's question, say so in the
objective instead of substituting a proxy. A test resting on a stand-in that
the data does not actually carry returns a confident and wrong conclusion,
where naming the missing evidence leaves the auditor something true. Treat
a threshold, limit, or approval rule read from a document the same way: it is
only as complete as that document, so do not encode one in a step as though it
were the entire rule unless the document states that it is.

A "data" test's steps are Polars only. Each step is an object:
  label         short name for the step
  instruction   what the step determines
  code          Polars code assigning the exception rows to `result`
All workspace tables and joins are available as DataFrame variables under
their exact names, and in the `tables` mapping; `pl` is the Polars module.
`result` must be a DataFrame holding the rows that fail the step — an empty
result means no exception. Use exact column and table names from the supplied
schemas. No imports, no file or network access, and no printing.

A step is a predicate, so the values a column holds decide whether it can match
anything. Where a column carries `values`, that is the complete set of values
present in that column — filter only on those, and never on a value outside it.
Where a column carries `distinct` without `values`, its contents are unknown to
you: do not guess a code, status, or category literal for it. Write the step so
it derives what it needs from the column itself instead of from an assumed
value, and prefer a comparison between two columns, a null check, or an
aggregate over a guessed string. Guessing wrong does not fail — it silently
matches every row or no row, and both are reported as a control conclusion.
Check the two sides of a comparison hold the same kind of identifier: comparing
columns from different id schemes returns no rows and looks like a clean result.

A "document" test's steps use only "question" or "vouch" mode, the same mode
on every step in one test. Choose the mode from what the row's risk is about:
  "question"  the answer is a statement a document makes about the process —
              a policy requirement, a described control, a governance fact
  "vouch"     the answer is whether recorded transactions are actually
              supported by their evidence — amounts, dates, approvals, and
              attachments agreeing between the records and the documents
A row about whether a control *operated* — whether claims were supported,
approved within limits, or paid after approval — is a "vouch" test whenever
transaction evidence is available, because reading a policy cannot establish
that. Prefer "vouch" over a question that merely asks whether evidence exists.

A "question" step has exactly one of two shapes:
  with sources      document_ids is non-empty; missing_evidence is omitted;
                    scope_limitation may name evidence the supplied documents
                    do not themselves provide
  missing evidence  document_ids is empty; missing_evidence is a non-empty
                    description of the evidence required

Both question shapes also carry:
  label             short name for the step
  instruction       what the step determines
  mode              "question"
  document_ids      array of exact document ids the step reads
  question          the question to answer from those documents
Use only document ids from the supplied context. If the required evidence is
missing, still return a concrete step with an empty document_ids array and a
specific missing_evidence string — never invent a document id.

A "vouch" step is one transaction-cycle plan, and a vouch test has exactly one
step. It names no document ids and no expected values: each population row is
linked to the documents that carry its identifier, and the row supplies what the
document is compared against. Fields:
  label           short name for the step
  instruction     what the comparison establishes
  mode            "vouch"
  anchor_table    the imported table whose rows are the population
  anchor_key      the column holding the identifier the documents carry
  document_roles  array of {{role, required, document_types}}; role is the name
                  a check refers to, document_types lists the extracted
                  document types that fill it. document_types must come from
                  this closed vocabulary, which is what the extraction records —
                  not the document's import category:
                  {", ".join(document_analysis.VOUCHER_DOCUMENT_TYPES)}
  checks          array of discriminated check objects

The supplied `transaction_evidence` manifest is authoritative. Choose
`anchor_table` and `anchor_key` only from `anchor_candidates`, document types
only from `available_document_types`, and document field paths only from each
type's `available_path_suffixes`. Do not infer a path merely because it appears
in an example or in the global field vocabulary below.

A check has exactly one of these two shapes:
  unary presence  {{field, left, method: "present"}}
  binary          {{field, left, right, method, optional tolerance}}, where
                  method is not "present"

A check names both of its sides by path, never by value:
  row.<column>                   a value from the population row
  <role>.<group>.<key>           a value extracted from the document attached
  <role>.<group>.<key>.<attr>    in that role
<key> is the kind or role that names the entry, such as `claim_id`,
`payment_date`, `total`, or `receipt`; `*` matches every entry in the group.
<group> and its permitted <attr> are exactly:
{chr(10).join(
    f"  {group:<12} {', '.join(sorted(doc_tests.FIELD_GROUP_ATTRIBUTES[group]))}"
    for group in sorted(doc_tests.FIELD_GROUPS)
)}
Omitting <attr> reads the group's default: {", ".join(
    f"{group}->{doc_tests.FIELD_GROUPS[group][2]}" for group in sorted(doc_tests.FIELD_GROUPS)
)}.

<method> is one of: {", ".join(sorted(doc_tests.METHODS))}. Use `date_order` when
the left date must not fall after the right one, such as approval before
payment. Use `present` when a single addressed value must be affirmatively true,
such as a receipt being attached; it has no `right` field. If two values must
both be present, return two unary checks.

Both sides of a check may be documents, which is how one row tests a whole
cycle: compare `purchase_order.amount.total` to `invoice.amount.total`, and
`goods_receipt.date.delivery_date` to `invoice.date.invoice_date`. Propose a
vouch test only when the supplied documents include transaction evidence.

A vouch test reaches only those population rows whose identifier appears in the
supplied evidence, which is usually a small part of the population and may be a
single transaction. Mark a role `required` only where the test is meaningless
without it — a required role no document fills leaves every check on that role
unresolved. Say in the objective which cycle the test covers and that its reach
is limited to the transactions holding evidence, so a clean result is not read
as assurance over the whole population.

Choose each test's source from what is actually available in the supplied
tables and documents. Add no other fields.

Authoritative response contract:
{json.dumps(GENERATE_RESPONSE_CONTRACT, sort_keys=True, separators=(",", ":"))}

{JSON_RULES}"""

GENERATE_ROW_SOURCE_ID = "rcm_row"
GENERATE_METHODOLOGY_SOURCE_ID = "methodology"
GENERATE_TABLE_SOURCE_ID = "table_metadata"
GENERATE_DOCUMENT_SOURCE_ID = "documents"
_GENERATE_SOURCES = {"data", "document"}
_GENERATE_COMMON_FIELDS = ("source", "title", "objective", "steps")
_GENERATE_DATA_STEP_FIELDS = ("label", "instruction", "code")
_GENERATE_DOCUMENT_STEP_FIELDS = (
    "label", "instruction", "mode", "document_ids", "question", "checks",
    "missing_evidence", "scope_limitation", "anchor_table", "anchor_key",
    "document_roles",
)
_GENERATE_MODES = {"question", "vouch"}
# The document category that carries a structured field record a vouch check can
# resolve. A cycle plan against documents that were never analyzed under the
# voucher profile would resolve every path to nothing.
_TRANSACTION_EVIDENCE_CATEGORY = "voucher"
_UNKNOWN_COLUMN_ERROR_RE = re.compile(
    r"ColumnNotFoundError: unable to find column [\"']([^\"']+)[\"']"
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


def _generate_has_transaction_evidence(request: WorkerRequest) -> bool:
    """Whether any supplied document is transaction evidence.

    A cycle plan resolves its paths against the structured fields the voucher
    profile extracts, so proposing one where no such document was supplied would
    produce a test whose every comparison is missing.
    """
    for item in request.context.items:
        if item.source_id != GENERATE_DOCUMENT_SOURCE_ID:
            continue
        content = item.content
        if (
            isinstance(content, Mapping)
            and (
                str(content.get("category") or "")
                == _TRANSACTION_EVIDENCE_CATEGORY
                or bool(content.get("vouch_profile"))
            )
        ):
            return True
    return False


def _generate_vouch_grounding(
    request: WorkerRequest,
) -> tuple[bool, set[tuple[str, str]], dict[str, set[str]]]:
    """Return verified anchor candidates and field paths by extracted type."""

    declared = False
    anchors: set[tuple[str, str]] = set()
    paths_by_document_type: dict[str, set[str]] = {}
    for item in request.context.items:
        content = item.content
        if not isinstance(content, Mapping):
            continue
        if item.source_id == GENERATE_TABLE_SOURCE_ID:
            if "vouch_anchor_candidates" in content:
                declared = True
            for candidate in content.get("vouch_anchor_candidates") or []:
                if not isinstance(candidate, Mapping):
                    continue
                table = str(candidate.get("table") or "").strip()
                key = str(candidate.get("anchor_key") or "").strip()
                if table and key:
                    anchors.add((table, key))
        elif item.source_id == GENERATE_DOCUMENT_SOURCE_ID:
            profile = content.get("vouch_profile")
            if isinstance(profile, Mapping):
                document_type = str(profile.get("document_type") or "").strip()
                if document_type:
                    paths_by_document_type.setdefault(document_type, set()).update(
                        str(value).strip()
                        for value in profile.get("available_path_suffixes") or []
                        if str(value).strip()
                    )
    return declared, anchors, paths_by_document_type


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
    for key in ("label", "instruction"):
        if not isinstance(step.get(key), str) or not step[key].strip():
            errors.append(f"{path}.{key} must be a non-empty string")
    code = step.get("code")
    if not isinstance(code, str) or not code.strip():
        errors.append(f"{path}.code must be non-empty Polars code")
    else:
        safe_code = True
        try:
            sandbox.validate(code)
        except ValueError as error:
            errors.append(f"{path}.code is not allowed in the sandbox: {error}")
            safe_code = False
        if "result" not in code:
            errors.append(f"{path}.code must assign the exception rows to `result`")
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
                    errors.append(
                        f"{path}.code references unknown column '{unknown_column.group(1)}'"
                    )
                else:
                    errors.append(
                        f"{path}.code cannot run against the supplied table schemas: {error}"
                    )
    return {
        "label": str(step.get("label") or "").strip(),
        "instruction": str(step.get("instruction") or "").strip(),
        "code": str(code).strip() if isinstance(code, str) else "",
    }


def _validate_generate_cycle_step(
    path: str,
    step: dict,
    known_tables: Mapping[str, Mapping[str, str]],
    has_evidence: bool,
    declared_anchors: bool,
    grounded_anchors: set[tuple[str, str]],
    paths_by_document_type: Mapping[str, set[str]],
    errors: list[str],
) -> dict:
    """Validate one transaction-cycle plan against the supplied schemas.

    The plan is checked for resolvability, not for judgement: the anchor must be
    a real column of a real table, every declared role must be referenced by a
    path that parses, and every ``row.<column>`` must name a column the anchor
    table actually has. A literal ``expected`` is rejected outright — the whole
    point of the shape is that the population supplies the expected value, and a
    model has no row data to write one from.
    """
    anchor_table = str(step.get("anchor_table") or "").strip()
    anchor_key = str(step.get("anchor_key") or "").strip()
    columns = known_tables.get(anchor_table) or {}
    if not anchor_table:
        errors.append(f"{path} needs an anchor_table")
    elif anchor_table not in known_tables:
        errors.append(f"{path} references unknown table '{anchor_table}'")
    if not anchor_key:
        errors.append(f"{path} needs an anchor_key")
    elif columns and anchor_key not in columns:
        errors.append(
            f"{path}.anchor_key '{anchor_key}' is not a column of '{anchor_table}'"
        )
    elif (
        declared_anchors
        and anchor_table
        and anchor_key
        and (anchor_table, anchor_key) not in grounded_anchors
    ):
        choices = ", ".join(
            f"{table}.{key}" for table, key in sorted(grounded_anchors)
        ) or "none"
        errors.append(
            f"{path} anchor '{anchor_table}.{anchor_key}' has no locally verified "
            f"identifier overlap; supplied anchor candidates: {choices}"
        )
    if not has_evidence:
        errors.append(
            f"{path} is a vouch step but no transaction-evidence document is "
            "available; use a question step or return no test for this row"
        )

    roles = []
    for index, raw_role in enumerate(step.get("document_roles") or []):
        role_path = f"{path}.document_roles[{index}]"
        if not isinstance(raw_role, Mapping):
            errors.append(f"{role_path} must be an object")
            continue
        name = str(raw_role.get("role") or "").strip()
        if not name:
            errors.append(f"{role_path} needs a role name")
            continue
        types = [
            str(value).strip()
            for value in (raw_role.get("document_types") or [name])
            if str(value).strip()
        ]
        # A role whose declared types are not what the extraction records can
        # never be filled, so every item would report a missing role and the
        # whole cycle would land in manual review. Caught here rather than
        # discovered as an empty result.
        unknown_types = [
            value
            for value in types
            if value not in document_analysis.VOUCHER_DOCUMENT_TYPES
        ]
        if unknown_types:
            errors.append(
                f"{role_path}.document_types has '{unknown_types[0]}', which is not "
                "an extracted document type; expected one of: "
                + ", ".join(document_analysis.VOUCHER_DOCUMENT_TYPES)
            )
        available_document_types = set(paths_by_document_type)
        unavailable_types = [
            value
            for value in types
            if value in document_analysis.VOUCHER_DOCUMENT_TYPES
            and available_document_types
            and value not in available_document_types
        ]
        if unavailable_types:
            errors.append(
                f"{role_path}.document_types has '{unavailable_types[0]}', but "
                "the supplied evidence contains only: "
                + ", ".join(sorted(available_document_types))
            )
        roles.append(
            {
                "role": name,
                "required": bool(raw_role.get("required", True)),
                "document_types": types or [name],
            }
        )
    if not roles:
        errors.append(f"{path} needs at least one document role")
    declared_roles = {entry["role"] for entry in roles}
    paths_by_role = {
        entry["role"]: set().union(
            *(
                paths_by_document_type.get(document_type, set())
                for document_type in entry["document_types"]
            )
        )
        for entry in roles
    }

    def validate_side(label: str, value: str) -> None:
        """Check one side of one comparison for resolvability."""
        try:
            doc_tests.validate_path(value)
        except doc_tests.WorkspaceError as error:
            errors.append(f"{label} {error}")
            return
        head = value.split(".", 1)[0]
        if head == doc_tests.ROW_PREFIX:
            column = value.split(".", 1)[1]
            if columns and column not in columns:
                errors.append(
                    f"{label} references unknown column '{column}' of "
                    f"'{anchor_table}'"
                )
        elif declared_roles and head not in declared_roles:
            errors.append(f"{label} references undeclared role '{head}'")
        elif head in paths_by_role and paths_by_document_type:
            suffix = value.split(".", 1)[1]
            if suffix not in paths_by_role[head]:
                choices = ", ".join(sorted(paths_by_role[head])) or "none"
                errors.append(
                    f"{label} references unavailable extracted path '{suffix}' "
                    f"for role '{head}'; supplied paths: {choices}"
                )

    checks = []
    raw_checks = step.get("checks")
    if not isinstance(raw_checks, (list, tuple)) or not raw_checks:
        errors.append(f"{path} needs comparison checks")
        raw_checks = []
    for index, raw_check in enumerate(raw_checks):
        check_path_label = f"{path}.checks[{index}]"
        if not isinstance(raw_check, Mapping):
            errors.append(f"{check_path_label} must be an object")
            continue
        if raw_check.get("expected") not in (None, ""):
            errors.append(
                f"{check_path_label} carries a literal expected value; a vouch "
                "check names both sides by path"
            )
        field = str(raw_check.get("field") or "").strip()
        if not field:
            errors.append(f"{check_path_label} needs a field")
        method = str(raw_check.get("method") or "normalized").strip()
        if method not in doc_tests.METHODS:
            errors.append(
                f"{check_path_label}.method must be one of: "
                + ", ".join(sorted(doc_tests.METHODS))
            )
            method = "normalized"
        left = str(raw_check.get("left") or "").strip()
        right = str(raw_check.get("right") or "").strip()
        if not left:
            errors.append(f"{check_path_label} needs a left path")
        else:
            validate_side(f"{check_path_label}.left", left)
        if method in doc_tests.UNARY_METHODS:
            if right:
                errors.append(
                    f"{check_path_label} has a right path but method '{method}' "
                    "reads only the left side"
                )
                right = ""
        elif not right:
            errors.append(f"{check_path_label} needs a right path")
        elif right:
            validate_side(f"{check_path_label}.right", right)
        checks.append(
            {
                "field": field,
                "left": left,
                "right": right,
                "method": method,
                "tolerance": raw_check.get("tolerance"),
            }
        )
    return {
        "anchor_table": anchor_table,
        "anchor_key": anchor_key,
        "document_roles": roles,
        "checks": checks,
    }


def _validate_generate_document_step(
    path: str,
    raw_step: object,
    known_document_ids: set[str],
    errors: list[str],
    *,
    known_tables: Mapping[str, Mapping[str, str]] | None = None,
    has_evidence: bool = False,
    declared_anchors: bool = False,
    grounded_anchors: set[tuple[str, str]] | None = None,
    paths_by_document_type: Mapping[str, set[str]] | None = None,
) -> tuple[dict | None, str | None]:
    if not isinstance(raw_step, Mapping):
        errors.append(f"{path} must be an object")
        return None, None
    step = _plain_json(raw_step)
    foreign = [key for key in _GENERATE_DATA_STEP_FIELDS if key in step and key not in _GENERATE_DOCUMENT_STEP_FIELDS]
    if foreign:
        errors.append(f"{path} has data-only field '{foreign[0]}' on a document step")
    for key in ("label", "instruction"):
        if not isinstance(step.get(key), str) or not step[key].strip():
            errors.append(f"{path}.{key} must be a non-empty string")
    mode = step.get("mode")
    if mode not in _GENERATE_MODES:
        errors.append(f"{path}.mode must be 'question' or 'vouch'")
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
    question = str(step.get("question") or "").strip()
    missing_evidence = str(step.get("missing_evidence") or "").strip()
    scope_limitation = str(step.get("scope_limitation") or "").strip()
    normalized = {
        "label": str(step.get("label") or "").strip(),
        "instruction": str(step.get("instruction") or "").strip(),
        "mode": mode or "",
    }
    if mode == "vouch":
        # A cycle plan names no documents: linking is by the identifiers already
        # extracted from them, so a model-chosen document id would either
        # duplicate that work or contradict it.
        if question:
            errors.append(f"{path} has a question but mode is 'vouch'")
        if document_ids:
            errors.append(
                f"{path} names document ids, but a vouch step links documents to "
                "population rows by their extracted identifiers"
            )
        if missing_evidence:
            errors.append(
                f"{path} claims missing_evidence; a vouch step reports uncovered "
                "population rows from its own coverage instead"
            )
        if scope_limitation:
            errors.append(
                f"{path} has scope_limitation but mode is 'vouch'; state limited "
                "transaction reach in the test objective"
            )
        normalized.update(
            _validate_generate_cycle_step(
                path,
                step,
                known_tables or {},
                has_evidence,
                declared_anchors,
                grounded_anchors or set(),
                paths_by_document_type or {},
                errors,
            )
        )
        return normalized, mode

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
    has_transaction_evidence = _generate_has_transaction_evidence(request)
    (
        declared_anchors,
        grounded_anchors,
        paths_by_document_type,
    ) = _generate_vouch_grounding(request)
    available = {
        "data": bool(known_tables),
        "document": any(
            item.source_id == GENERATE_DOCUMENT_SOURCE_ID for item in request.context.items
        ),
    }
    errors: list[str] = []
    normalized: list[dict] = []
    for index, raw in enumerate(values, 1):
        path = f"tests[{index - 1}]"
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
                    known_tables=known_tables,
                    has_evidence=has_transaction_evidence,
                    declared_anchors=declared_anchors,
                    grounded_anchors=grounded_anchors,
                    paths_by_document_type=paths_by_document_type,
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
        normalized.append(
            {
                "source": source,
                "title": str(value.get("title") or "").strip(),
                "objective": str(value.get("objective") or "").strip(),
                "steps": steps,
                "rcm_id": rcm_id,
                "methodology_refs": methodology_refs,
            }
        )
    if errors:
        raise WorkerResponseValidationError(errors)
    return {"tests": normalized}


def run_generate_worker(
    request: WorkerRequest,
    gateway: ModelGateway,
    attempt: WorkerAttempt,
) -> str:
    """Transform only the supplied bundle into one budgeted model request."""
    user = json.dumps(
        _generation_prompt_payload(request),
        separators=(",", ":"),
        ensure_ascii=False,
    )
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
        GENERATE_SYSTEM,
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
    implementation_hash=_sha256_text(inspect.getsource(run_generate_worker)),
    prompt_hash=_sha256_text(GENERATE_SYSTEM),
    response_schema=GENERATE_RESPONSE_SCHEMA,
    repair_policy=WorkerRepairPolicy(
        max_repair_attempts=2,
        guidance_hash=_sha256_text(
            "Repair generated test contract violations against the supplied RCM row."
        ),
    ),
    implementation=run_generate_worker,
    semantic_validation_hash=_sha256_text(inspect.getsource(validate_generate_proposal)),
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
