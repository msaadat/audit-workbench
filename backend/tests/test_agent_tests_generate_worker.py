"""Focused tests for the registered ``tests.generate`` worker.

The worker owns only the merged generation prompt, bundle-to-message
transformation, response schema, and the full-contract quality gate from
docs/test-capability-merge-plan.md section 4. It is exercised with
constructed bundles and a gateway stub and must not touch a workspace, store,
resolver, or scheduler.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import WORKERS, WorkerContractError, WorkerRequest, WorkerRunError
from app.agent.workers import tests as tests_workers


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(
        self,
        system,
        user,
        activity=None,
        *,
        attempt=1,
        conversation=None,
    ):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "activity": activity,
                "attempt": attempt,
                "conversation": conversation,
            }
        )
        return self.responses.pop(0)


_METHODOLOGY_SECTION = {
    "pack_id": "PK-1",
    "pack_name": "Firm AP Guide",
    "version": 2,
    "sha1": "a" * 40,
    "section": "Duplicate payments",
    "citation": "Firm AP Guide v2, Duplicate payments",
    "text": "Audit procedures should address duplicate-payment risk.",
}


def _bundle(
    *,
    rcm_rows=("RCM-1",),
    methodology=(_METHODOLOGY_SECTION,),
    tables=("transactions",),
    table_columns=("invoice", "amount"),
    table_grains=None,
    documents=("DOC-1",),
    document_categories=None,
    document_vouch_profiles=None,
    table_anchor_candidates=None,
    transaction_manifest=None,
    rcm_payload=None,
):
    values = [
        (
            "planning_context",
            "planning:context",
            ContextRepresentation("planning_context"),
            {"context": {"objective": "Assess procurement approvals"}},
        ),
        (
            "transaction_evidence",
            "workspace:transaction-evidence",
            ContextRepresentation("table_metadata"),
            transaction_manifest or {
                "kind": "ruleset",
                "ruleset": None,
                "reason": "no_approved_ruleset",
            },
        ),
    ]
    for rcm_id in rcm_rows:
        values.append(
            (
                "rcm_row",
                f"rcm:{rcm_id}",
                ContextRepresentation("current_artifact"),
                rcm_payload or {
                    "id": rcm_id,
                    "risk": "Duplicate payments are processed",
                    "control": "Duplicate invoice validation",
                    "existing_tests": [],
                },
            )
        )
    for table in tables:
        columns = (
            table_columns.get(table, ())
            if isinstance(table_columns, dict)
            else table_columns
        )
        grain = (table_grains or {}).get(table, table)
        table_content = {
            "table": table,
            "rows": 3,
            "columns": [{"name": column} for column in columns],
            "grain": grain,
            "derived": grain != table,
        }
        if table_anchor_candidates is not None:
            table_content["vouch_anchor_candidates"] = list(
                table_anchor_candidates.get(table, ())
            )
        values.append(
            (
                "table_metadata",
                f"table:{table}",
                ContextRepresentation("table_metadata"),
                table_content,
            )
        )
    categories = document_categories or {}
    profiles = document_vouch_profiles or {}
    for document_id in documents:
        document_content = {
            "id": document_id,
            "title": document_id,
            "summary": "Policy.",
            # A vouch step resolves its paths against fields only the
            # voucher profile extracts, so the category is what tells the
            # worker whether a cycle plan is proposable at all.
            "category": categories.get(document_id, "policy"),
        }
        if document_id in profiles:
            document_content["vouch_profile"] = profiles[document_id]
        values.append(
            (
                "documents",
                f"document:{document_id}",
                ContextRepresentation("summary"),
                document_content,
            )
        )
    for index, section in enumerate(methodology, start=1):
        values.append(
            (
                "methodology",
                f"methodology:firm:{section['pack_id']}:{index}",
                ContextRepresentation("excerpt"),
                section,
            )
        )
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=representation,
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(
        capability_id="tests.specified",
        unit_id="test_generation:RCM-1",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="tests.generate",
        capability_id="tests.specified",
        unit_id="test_generation:RCM-1",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "test-generate-input"},
        activity={"artifact_refs": ["rcm:RCM-1"]},
    )


def _data_step(**overrides):
    value = {
        "label": "Find duplicate invoice keys",
        "instruction": "Compare invoice numbers for duplicates.",
        "population": "transactions",
        "code": "result = transactions.filter(pl.col('invoice').is_duplicated())",
    }
    value.update(overrides)
    return value


def _question_step(**overrides):
    value = {
        "label": "Inspect approval evidence",
        "instruction": "Determine whether the payment was approved.",
        "mode": "question",
        "document_ids": ["DOC-1"],
        "question": "Was this payment approved before release?",
        "missing_evidence": "",
    }
    value.update(overrides)
    return value


def _vouch_step(**overrides):
    """One transaction-cycle plan: paths on both sides, no literal values."""

    value = {
        "label": "Vouch payments to their invoices",
        "instruction": "Agree each recorded payment to its supporting invoice.",
        "mode": "vouch",
        "anchor_table": "transactions",
        "anchor_key": "invoice",
        "document_roles": [{"role": "invoice", "required": True}],
        "checks": [
            {
                "field": "amount agrees",
                "method": "numeric_tolerance",
                "tolerance": 0,
                "left": "row.amount",
                "right": "invoice.amount.total",
            }
        ],
    }
    value.update(overrides)
    return value


def _voucher_bundle(**overrides):
    """A bundle whose supplied document is transaction evidence."""

    return _bundle(document_categories={"DOC-1": "voucher"}, **overrides)


def _grounded_voucher_bundle(**overrides):
    """A bundle carrying the safe manifest produced by the live adapter."""

    return _bundle(
        document_categories={"DOC-1": "voucher"},
        document_vouch_profiles={
            "DOC-1": {
                "document_id": "DOC-1",
                "document_type": "invoice",
                "available_path_suffixes": [
                    "identifier.invoice_number",
                    "amount.total",
                ],
            }
        },
        table_anchor_candidates={
            "transactions": [
                {
                    "table": "transactions",
                    "anchor_key": "invoice",
                    "matched_rows": 1,
                    "matched_document_count": 1,
                    "document_types": ["invoice"],
                }
            ]
        },
        **overrides,
    )


def _data_test(**overrides):
    value = {
        "source": "data",
        "title": "Duplicate payment detection",
        "objective": "Determine whether duplicate payments were prevented.",
        "steps": [_data_step()],
    }
    value.update(overrides)
    return value


def _document_test(**overrides):
    value = {
        "source": "document",
        "title": "Payment approval review",
        "objective": "Determine whether selected payments were approved.",
        "steps": [_question_step()],
    }
    value.update(overrides)
    return value


def test_generate_worker_produces_a_ready_data_test():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    result = WORKERS.execute(_request(), gateway)

    proposed = result.proposal["tests"]
    assert [item["title"] for item in proposed] == ["Duplicate payment detection"]
    assert proposed[0]["source"] == "data"
    assert proposed[0]["rcm_id"] == "RCM-1"
    assert "table_refs" not in proposed[0]["steps"][0]
    assert "result" in proposed[0]["steps"][0]["code"]
    assert gateway.calls[0]["system"].startswith(tests_workers.GENERATE_SYSTEM)
    assert "[agent:test_generate_variant_gate]" in gateway.calls[0]["system"]
    assert (
        gateway.calls[0]["activity"]["context_metrics"]["worker_kind"]
        == "test_generation"
    )


def test_generate_worker_produces_a_ready_document_question_test():
    gateway = _Gateway([json.dumps({"tests": [_document_test()]})])

    result = WORKERS.execute(_request(), gateway)

    proposed = result.proposal["tests"][0]
    assert proposed["source"] == "document"
    step = proposed["steps"][0]
    assert step["mode"] == "question"
    assert step["question"]
    assert "checks" not in step


def test_generate_worker_defaults_document_mode_and_question_from_instruction():
    response = _document_test(
        steps=[
            {
                "label": "Inspect approval workflow",
                "instruction": "Was the requisition approved by authorized staff?",
                "document_ids": ["DOC-1"],
            }
        ]
    )

    result = WORKERS.execute(
        _request(), _Gateway([json.dumps({"tests": [response]})])
    )

    step = result.proposal["tests"][0]["steps"][0]
    assert step["mode"] == "question"
    assert step["question"] == response["steps"][0]["instruction"]


def test_generate_worker_repairs_unavailable_cycle_to_document_question():
    bundle = _bundle(
        rcm_payload={
            "id": "RCM-1",
            "risk": "Unauthorized requisitions may be initiated.",
            "control": "Authorized staff initiate requisitions.",
            "control_attributes": [
                {
                    "key": "authorization",
                    "requirement": "Inspect authorization evidence.",
                    "evidence_kind": "manual_inspection",
                }
            ],
        }
    )
    invalid_cycle = {
        "source": "document",
        "kind": "cycle_vouch",
        "title": "Authorization",
        "objective": "Inspect authorization.",
        "requirement_refs": ["RCM-1:authorization"],
        "procedure_key": "authorization",
        "candidate_id": "DOC-1",
        "selection_reason": "The document is relevant.",
        "selection": {"mode": "evidence_linked"},
        "assertions": [],
    }
    repaired_question = _document_test(
        steps=[
            {
                "label": "Inspect authorization",
                "instruction": "Was the requisition initiated by authorized staff?",
                "document_ids": ["DOC-1"],
            }
        ]
    )
    gateway = _Gateway(
        [
            json.dumps({"tests": [invalid_cycle]}),
            json.dumps({"tests": [repaired_question]}),
        ]
    )

    result = WORKERS.execute(_request(bundle), gateway)

    assert result.repaired is True
    assert "allows only these variants: document_question" in gateway.calls[0]["system"]
    assert "Cycle Vouch section above does not apply" in gateway.calls[0]["system"]
    guidance = gateway.calls[1]["conversation"][-1]["content"]
    assert "cycle_vouch is unavailable" in guidance
    assert "Never return an empty tests array" in guidance
    assert result.proposal["tests"][0]["steps"][0]["mode"] == "question"


def test_generate_worker_produces_a_ready_document_vouch_test():
    """The retired narrative cycle branch fails closed."""

    invalid = json.dumps({"tests": [_document_test(steps=[_vouch_step()])]})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_voucher_bundle()), gateway)


def test_generate_worker_keeps_document_fallback_when_cycle_has_no_candidates():
    rcm_payload = {
        "id": "RCM-1",
        "risk": "A transaction cycle may be incomplete.",
        "control": "Supporting cycle records are retained.",
        "control_attributes": [
            {
                "key": "complete_cycle",
                "requirement": "The transaction cycle is complete.",
                "evidence_kind": "transaction_cycle",
            }
        ],
    }
    bundle = _bundle(rcm_payload=rcm_payload)
    gateway = _Gateway([json.dumps({"tests": [_document_test()]})])

    WORKERS.execute(_request(bundle), gateway)

    payload = json.loads(gateway.calls[0]["user"])
    assert payload["documents"]
    assert payload["table_schemas"] == []
    assert payload["allowed_test_variants"] == ["document_question"]


def test_generate_worker_bounds_tabular_schema_projection():
    tables = tuple(f"table_{index}" for index in range(12))
    bundle = _bundle(
        tables=tables,
        table_columns={table: ("invoice", "amount") for table in tables},
        rcm_payload={
            "id": "RCM-1",
            "risk": "Invoice amounts may be duplicated.",
            "control": "Invoice tables are checked.",
            "control_attributes": [
                {
                    "key": "duplicate_invoice",
                    "requirement": "Detect duplicated invoice amounts.",
                    "evidence_kind": "tabular_population",
                }
            ],
        },
    )
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "tests": [
                        _data_test(
                            steps=[
                                _data_step(
                                    population="table_0",
                                    code=(
                                        "result = table_0.filter("
                                        "pl.col('invoice').is_duplicated())"
                                    )
                                )
                            ]
                        )
                    ]
                }
            )
        ]
    )

    WORKERS.execute(_request(bundle), gateway)

    payload = json.loads(gateway.calls[0]["user"])
    assert len(payload["table_schemas"]) == tests_workers._SCHEMA_LIMIT


def test_the_population_a_requirement_names_survives_the_schema_projection():
    """Ranking alone lets views over a population crowd the population out.

    A join frame's name contains every word of the tables it was built from, so
    it matches every query they match and sorts above all of them. Six joins
    *over* the vendor master once filled a vendor-master row's schema list
    while the vendor master itself never reached the prompt, and the generated
    test read staff bank accounts instead.
    """
    tables = tuple(f"vendor_master_file_join_{index}" for index in range(12))
    bundle = _bundle(
        tables=(*tables, "vendor_master_file"),
        table_columns={
            table: ("vendor", "bank", "account") for table in (*tables, "vendor_master_file")
        },
        table_grains={table: "invoice_data" for table in tables},
        rcm_payload={
            "id": "RCM-1",
            "risk": "Two vendor master records may share one bank account.",
            "control": "Vendor master bank account review.",
            "control_attributes": [
                {
                    "key": "vendor_bank_account",
                    "requirement": "Vendor bank account details are unique.",
                    "evidence_kind": "tabular_population",
                }
            ],
        },
    )
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "tests": [
                        _data_test(
                            steps=[
                                _data_step(
                                    population="vendor_master_file",
                                    code=(
                                        "result = vendor_master_file.filter("
                                        "pl.col('account').is_duplicated())"
                                    )
                                )
                            ]
                        )
                    ]
                }
            )
        ]
    )

    WORKERS.execute(_request(bundle), gateway)

    payload = json.loads(gateway.calls[0]["user"])
    supplied = {schema["table"] for schema in payload["table_schemas"]}
    assert "vendor_master_file" in supplied


def test_generate_worker_bounds_document_projection():
    documents = tuple(f"DOC-{index}" for index in range(8))
    bundle = _bundle(
        documents=documents,
        rcm_payload={
            "id": "RCM-1",
            "risk": "Authorization evidence may be missing.",
            "control": "Authorization is inspected.",
            "control_attributes": [
                {
                    "key": "authorization",
                    "requirement": "Inspect authorization evidence.",
                    "evidence_kind": "manual_inspection",
                }
            ],
        },
    )
    response = _document_test(
        steps=[_question_step(document_ids=["DOC-0"])]
    )
    gateway = _Gateway([json.dumps({"tests": [response]})])

    WORKERS.execute(_request(bundle), gateway)

    payload = json.loads(gateway.calls[0]["user"])
    assert len(payload["documents"]) == 6
    assert payload["allowed_test_variants"] == ["document_question"]


def test_generate_worker_rejects_a_literal_expected_value_in_a_vouch_check():
    """The population supplies the expected value; a model has no row data."""

    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[
                        _vouch_step(
                            checks=[
                                {
                                    "field": "amount agrees",
                                    "method": "normalized",
                                    "left": "row.amount",
                                    "expected": "12500.00",
                                }
                            ]
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_voucher_bundle()), gateway)


def test_generate_worker_rejects_a_vouch_plan_the_schemas_cannot_resolve():
    """Anchor and every row path must name real columns of a real table."""

    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[
                        _vouch_step(
                            anchor_table="nope",
                            anchor_key="missing_key",
                            checks=[
                                {
                                    "field": "amount agrees",
                                    "method": "normalized",
                                    "left": "row.not_a_column",
                                    "right": "invoice.amount.total",
                                }
                            ],
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_voucher_bundle()), gateway)


def test_generate_worker_rejects_an_ungrounded_anchor_when_candidates_exist():
    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[_vouch_step(anchor_key="amount")]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_grounded_voucher_bundle()), gateway)


def test_generate_worker_rejects_a_check_naming_an_undeclared_role():
    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[
                        _vouch_step(
                            checks=[
                                {
                                    "field": "amount agrees",
                                    "method": "normalized",
                                    "left": "row.amount",
                                    "right": "goods_receipt.amount.total",
                                }
                            ]
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_voucher_bundle()), gateway)


def test_generate_worker_refuses_a_vouch_test_without_transaction_evidence():
    """A cycle plan over documents that carry no extracted fields tests nothing."""

    invalid = json.dumps({"tests": [_document_test(steps=[_vouch_step()])]})
    gateway = _Gateway([invalid, invalid, invalid])

    # The default bundle supplies a policy document only.
    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_allows_document_to_document_cycle_checks():
    """Even a well-shaped old dotted-path cycle is rejected."""

    invalid = json.dumps(
                {
                    "tests": [
                        _document_test(
                            steps=[
                                _vouch_step(
                                    document_roles=[
                                        {"role": "purchase_order", "required": True},
                                        {"role": "invoice", "required": True},
                                        {
                                            "role": "goods_receipt",
                                            "required": False,
                                            # A role may accept more than one
                                            # extracted type; both must be real.
                                            "document_types": ["goods_receipt", "receipt"],
                                        },
                                    ],
                                    checks=[
                                        {
                                            "field": "invoice agrees to order",
                                            "method": "numeric_tolerance",
                                            "tolerance": 0,
                                            "left": "purchase_order.amount.total",
                                            "right": "invoice.amount.total",
                                        },
                                        {
                                            "field": "goods received before invoice",
                                            "method": "date_order",
                                            "left": "goods_receipt.date.delivery_date",
                                            "right": "invoice.date.invoice_date",
                                        },
                                        {
                                            "field": "receipt attached",
                                            "method": "present",
                                            "left": "invoice.attachment.receipt.present",
                                        },
                                    ],
                                )
                            ]
                        )
                    ]
                }
            )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_voucher_bundle()), gateway)


def test_generate_worker_rejects_an_import_category_as_a_document_type():
    """`voucher` is how a document is imported, not how it is classified.

    A role declared against the import category can never be filled — the
    extraction records `payment_voucher` — so every item would report a missing
    role and the whole cycle would land in manual review with no result.
    """
    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[
                        _vouch_step(
                            document_roles=[
                                {
                                    "role": "voucher",
                                    "required": True,
                                    "document_types": ["voucher"],
                                }
                            ],
                            checks=[
                                {
                                    "field": "amount agrees",
                                    "method": "normalized",
                                    "left": "row.amount",
                                    "right": "voucher.amount.total",
                                }
                            ],
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_voucher_bundle()), gateway)


def test_generate_worker_rejects_a_type_absent_from_the_supplied_evidence():
    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[
                        _vouch_step(
                            document_roles=[
                                {
                                    "role": "purchase_order",
                                    "required": True,
                                    "document_types": ["purchase_order"],
                                }
                            ],
                            checks=[
                                {
                                    "field": "amount agrees",
                                    "method": "normalized",
                                    "left": "row.amount",
                                    "right": "purchase_order.amount.total",
                                }
                            ],
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_grounded_voucher_bundle()), gateway)


def test_generate_worker_rejects_a_path_absent_from_the_supplied_evidence():
    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[
                        _vouch_step(
                            checks=[
                                {
                                    "field": "invoice date agrees",
                                    "method": "normalized",
                                    "left": "row.invoice",
                                    "right": "invoice.date.invoice_date",
                                }
                            ]
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_grounded_voucher_bundle()), gateway)


def test_generate_worker_rejects_two_vouch_steps_in_one_test():
    """One vouch test is one cycle plan over one population."""

    invalid = json.dumps(
        {"tests": [_document_test(steps=[_vouch_step(), _vouch_step()])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(_voucher_bundle()), gateway)


def test_generate_worker_carries_supplied_methodology_citations():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    result = WORKERS.execute(_request(), gateway)

    refs = result.proposal["tests"][0]["methodology_refs"]
    assert [ref["pack_name"] for ref in refs] == ["Firm AP Guide"]
    assert "text" not in refs[0]


def test_generate_worker_accepts_mixed_source_tests_in_one_response():
    gateway = _Gateway(
        [json.dumps({"tests": [_data_test(), _document_test()]})]
    )

    result = WORKERS.execute(_request(), gateway)

    sources = {item["source"] for item in result.proposal["tests"]}
    assert sources == {"data", "document"}


def test_generate_worker_accepts_a_fenced_response():
    gateway = _Gateway(["```json\n" + json.dumps({"tests": [_data_test()]}) + "\n```"])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["tests"][0]["title"] == "Duplicate payment detection"


def test_generate_worker_rejects_document_only_field_on_a_data_step():
    invalid = json.dumps(
        {"tests": [_data_test(steps=[_data_step(mode="question")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="document-only field 'mode'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_data_only_field_on_a_document_step():
    invalid = json.dumps(
        {"tests": [_document_test(steps=[_question_step(code="result = df")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="data-only field 'code'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_mixed_document_modes_within_one_test():
    invalid = json.dumps(
        {"tests": [_document_test(steps=[_question_step(), _vouch_step()])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="removed vouch-step schema"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_discards_legacy_step_table_refs():
    proposed = _data_test(steps=[_data_step(table_refs=["ghost_table"])])

    result = WORKERS.execute(_request(), _Gateway([json.dumps({"tests": [proposed]})]))

    assert "table_refs" not in result.proposal["tests"][0]["steps"][0]


def test_generate_worker_rejects_an_unknown_column():
    invalid = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            code="result = transactions.filter(pl.col('ghost_column') > 0)"
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown column 'ghost_column'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_ambiguous_df_with_named_table_guidance():
    bundle = _bundle(
        tables=("financial_approval_matrix", "invoice_data"),
        table_columns={
            "financial_approval_matrix": ("JOB_TITLE", "MAX_APPROVAL_AMOUNT"),
            "invoice_data": ("INVOICE_ID", "VENDOR_INVOICE_NUMBER"),
        },
    )
    invalid = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            code=(
                                "result = df.filter("
                                "pl.col('VENDOR_INVOICE_NUMBER').is_duplicated())"
                            )
                        )
                    ]
                )
            ]
        }
    )

    with pytest.raises(WorkerRunError) as caught:
        WORKERS.execute(_request(bundle), _Gateway([invalid, invalid, invalid]))

    message = str(caught.value)
    assert "uses ambiguous `df`" in message
    assert "invoice_data" in message


def test_generate_worker_rejects_cross_step_state_with_standalone_guidance():
    invalid = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            code="result = joined_matrix.filter(pl.col('invoice') == '')"
                        )
                    ]
                )
            ]
        }
    )

    with pytest.raises(WorkerRunError, match="every step runs independently"):
        WORKERS.execute(_request(), _Gateway([invalid, invalid, invalid]))


def test_generate_worker_gives_polars_duration_repair_guidance():
    invalid = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            code=(
                                "result = transactions.filter("
                                "(pl.datetime(2025, 1, 2) - "
                                "pl.datetime(2025, 1, 1)).dt.days() > 0)"
                            )
                        )
                    ]
                )
            ]
        }
    )

    with pytest.raises(WorkerRunError, match=r"dt\.total_days"):
        WORKERS.execute(_request(), _Gateway([invalid, invalid, invalid]))


def test_generate_worker_accepts_columns_introduced_by_a_join():
    bundle = _bundle(
        tables=("requisitions", "po_data"),
        table_columns={
            "requisitions": ("REQUISITION_ID", "ITEM_DESCRIPTION"),
            "po_data": ("REQUISITION_ID", "ITEM_DESCRIPTION"),
        },
    )
    proposed = _data_test(
        steps=[
            _data_step(
                population="requisitions",
                code=(
                    'joined = requisitions.join(po_data, on="REQUISITION_ID", how="inner")\n'
                    'result = joined.filter(pl.col("ITEM_DESCRIPTION") '
                    '!= pl.col("ITEM_DESCRIPTION_right"))'
                )
            )
        ]
    )

    result = WORKERS.execute(
        _request(bundle), _Gateway([json.dumps({"tests": [proposed]})])
    )

    assert result.proposal["tests"][0]["steps"][0]["code"].endswith(
        'pl.col("ITEM_DESCRIPTION_right"))'
    )


def test_a_step_may_not_assert_about_a_population_it_is_not_anchored_on():
    """The grain error that hid the largest approval breach in the population.

    Every materialized join is a left join, so an invoice-grained frame holds
    only the requisitions that reached an invoice — 93 of 112 on the engagement
    this rule comes from. The one requisition approved 99M outside a 10M limit
    never became a purchase order, so the approval-limit test written against
    that frame returned 22 rows, every one of them a null-join artefact, and
    the breach itself was unreachable by construction.
    """
    bundle = _bundle(
        tables=("requisitions", "invoice_data_requisitions_joined"),
        table_columns={
            "requisitions": ("REQUISITION_ID", "ESTIMATED_TOTAL_COST", "FIN_APPROVED_BY_ID"),
            "invoice_data_requisitions_joined": (
                "INVOICE_ID",
                "REQUISITION_ID",
                "ESTIMATED_TOTAL_COST",
                "FIN_APPROVED_BY_ID",
            ),
        },
        table_grains={"invoice_data_requisitions_joined": "invoice_data"},
    )
    misanchored = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            population="requisitions",
                            code=(
                                "result = invoice_data_requisitions_joined.filter("
                                'pl.col("FIN_APPROVED_BY_ID").is_null())'
                            ),
                        )
                    ]
                )
            ]
        }
    )

    with pytest.raises(WorkerRunError) as caught:
        WORKERS.execute(_request(bundle), _Gateway([misanchored] * 3))

    message = str(caught.value)
    assert "declares population 'requisitions'" in message
    assert "grain 'invoice_data'" in message
    assert "Anchor the step on one of: requisitions" in message


def test_a_step_anchored_on_the_population_it_asserts_about_is_accepted():
    """Joining outward from the population is the shape the rule asks for."""
    bundle = _bundle(
        tables=("requisitions", "invoice_data_requisitions_joined"),
        table_columns={
            "requisitions": ("REQUISITION_ID", "ESTIMATED_TOTAL_COST", "FIN_APPROVED_BY_ID"),
            "invoice_data_requisitions_joined": ("INVOICE_ID", "REQUISITION_ID"),
        },
        table_grains={"invoice_data_requisitions_joined": "invoice_data"},
    )
    proposed = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            population="requisitions",
                            code=(
                                "result = requisitions.filter("
                                'pl.col("FIN_APPROVED_BY_ID").is_null())'
                            ),
                        )
                    ]
                )
            ]
        }
    )

    result = WORKERS.execute(_request(bundle), _Gateway([proposed]))

    assert result.proposal["tests"][0]["steps"][0]["population"] == "requisitions"


def test_a_requirement_naming_a_population_must_produce_a_step_about_it():
    """The wrong-table failure the anchor rule alone does not catch.

    A vendor-master requirement was answered by a step grouping *staff* names
    and bank accounts off an invoice-grained frame. It ran, returned 113 rows
    of a 118-row population, named VENDOR_ID in its output, and tested nothing
    the requirement asked about — while the vendor master sat unread.
    """
    bundle = _bundle(
        tables=(
            "vendor_master_file",
            "invoice_data",
            "invoice_data_staff_details_joined",
        ),
        table_columns={
            "vendor_master_file": ("VENDOR_ID", "BANK_ACCOUNT_NUMBER"),
            "invoice_data": ("INVOICE_ID", "VENDOR_ID"),
            "invoice_data_staff_details_joined": (
                "INVOICE_ID",
                "VENDOR_ID",
                "NAME",
                "BANK_ACCOUNT_NUMBER",
            ),
        },
        table_grains={"invoice_data_staff_details_joined": "invoice_data"},
        rcm_payload={
            "id": "RCM-1",
            "risk": "Duplicate vendor master records may share bank details.",
            "control": "Vendor master review.",
            "control_attributes": [
                {
                    "key": "vendor_duplicate_review",
                    "requirement": (
                        "Potential duplicate vendor identities and bank details "
                        "in the vendor master file are identified."
                    ),
                    "evidence_kind": "tabular_population",
                }
            ],
        },
    )
    wrong_table = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            population="invoice_data",
                            code=(
                                "result = invoice_data_staff_details_joined.filter("
                                'pl.col("BANK_ACCOUNT_NUMBER").is_duplicated())'
                            ),
                        )
                    ]
                )
            ]
        }
    )

    with pytest.raises(WorkerRunError) as caught:
        WORKERS.execute(_request(bundle), _Gateway([wrong_table] * 3))

    message = str(caught.value)
    assert "names population 'vendor_master_file'" in message
    assert "at its own grain" in message


def test_a_name_collision_alone_does_not_demand_a_step_about_a_population():
    """RCM-C37D96, which failed six generation attempts on an impossible ask.

    Its requirement — "the purchase order date is on or after the financial
    approval date" — shares `financial` and `approval` with the four-row
    `financial_approval_matrix`, a delegation table of job titles and approval
    limits carrying no date at all. The dates it means live on `requisitions`,
    whose name shares nothing with the sentence. So the rule demanded a step at
    a grain that cannot answer the requirement, and because a row-level gap
    suppresses the partial commit, it discarded the correct test sitting beside
    it and left the row untestable across two runs.
    """

    bundle = _bundle(
        tables=(
            "requisitions",
            "financial_approval_matrix",
            "po_data_requisitions_joined",
        ),
        table_columns={
            "requisitions": ("REQUISITION_ID", "FIN_APPROVAL_DATE", "PO_NUMBER"),
            "financial_approval_matrix": ("JOB_TITLE", "MAX_APPROVAL_AMOUNT"),
            "po_data_requisitions_joined": (
                "REQUISITION_ID",
                "PO_DATE",
                "FIN_APPROVAL_DATE",
            ),
        },
        table_grains={"po_data_requisitions_joined": "po_data"},
        rcm_payload={
            "id": "RCM-1",
            "risk": "Purchase orders may be dated before the financial approval date.",
            "control": "The Purchase Order is dated on or after the date of financial approval.",
            "control_attributes": [
                {
                    "key": "po_date_after_approval",
                    "requirement": (
                        "The purchase order date is on or after the financial "
                        "approval date."
                    ),
                    "evidence_kind": "tabular_population",
                }
            ],
        },
    )
    anchored_on_requisitions = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            population="requisitions",
                            code=(
                                "result = requisitions.join("
                                'po_data_requisitions_joined.select('
                                '["REQUISITION_ID", "PO_DATE"]), '
                                'on="REQUISITION_ID", how="left").filter('
                                'pl.col("PO_DATE") < pl.col("FIN_APPROVAL_DATE"))'
                            ),
                        )
                    ]
                )
            ]
        }
    )

    gateway = _Gateway([anchored_on_requisitions])
    result = WORKERS.execute(_request(bundle), gateway)

    assert len(gateway.calls) == 1, "the response was sent back for repair"
    assert result.repaired is False
    assert result.proposal["tests"][0]["steps"][0]["population"] == "requisitions"


def test_a_population_its_columns_corroborate_is_still_demanded():
    """The vendor-master catch survives the narrowing.

    Same shape as the collision above — a requirement sharing words with a
    table name — but here `BANK_ACCOUNT_NUMBER` names `bank` a second time,
    independently of `vendor_master_file`. That is the requirement reaching
    into the population rather than two words colliding, so the step is still
    demanded.
    """

    bundle = _bundle(
        tables=("vendor_master_file", "invoice_data_staff_details_joined"),
        table_columns={
            "vendor_master_file": ("VENDOR_ID", "BANK_ACCOUNT_NUMBER"),
            "invoice_data_staff_details_joined": ("INVOICE_ID", "BANK_ACCOUNT_NUMBER"),
        },
        table_grains={"invoice_data_staff_details_joined": "invoice_data"},
        rcm_payload={
            "id": "RCM-1",
            "risk": "Duplicate vendor master records may share bank details.",
            "control": "Vendor master review.",
            "control_attributes": [
                {
                    "key": "vendor_duplicate_review",
                    "requirement": (
                        "Duplicate vendor identities and bank details in the "
                        "vendor master file are identified."
                    ),
                    "evidence_kind": "tabular_population",
                }
            ],
        },
    )
    wrong_table = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            population="invoice_data",
                            code=(
                                "result = invoice_data_staff_details_joined.filter("
                                'pl.col("BANK_ACCOUNT_NUMBER").is_duplicated())'
                            ),
                        )
                    ]
                )
            ]
        }
    )

    with pytest.raises(WorkerRunError) as caught:
        WORKERS.execute(_request(bundle), _Gateway([wrong_table] * 3))

    assert "names population 'vendor_master_file'" in str(caught.value)


def test_a_malformed_response_is_repaired_against_the_position_that_broke():
    """"Not a valid JSON object" locates nothing, so nothing gets corrected.

    A live splitting-risk row emitted one stray ``}`` after its first step, in
    two thousand characters of escaped Polars code. Told only that the response
    was invalid, the model re-emitted the same brace in the same place three
    times and the row was lost. The decoder already knows the offending
    position; the repair message has to carry it.
    """
    broken = (
        '{"tests":[{"source":"data","title":"T","objective":"O","steps":['
        '{"label":"One","instruction":"I","population":"transactions",'
        '"code":"result = transactions.head(1)"}},'
        '{"label":"Two","instruction":"I","population":"transactions",'
        '"code":"result = transactions.head(2)"}]}]}'
    )
    gateway = _Gateway([broken] * 3)

    with pytest.raises(WorkerRunError) as caught:
        WORKERS.execute(_request(), gateway)

    message = str(caught.value)
    assert "not a valid JSON object" in message
    # The decoder's own position, and the text there, so the model can see the
    # brace rather than re-derive the whole response.
    assert "at character" in message
    assert 'transactions.head(1)"}}' in message
    # The repair turn is given the same guidance, not just the run error.
    guidance = gateway.calls[1]["conversation"][-1]["content"]
    assert "at character" in guidance


def test_generate_worker_rejects_an_unknown_document_id():
    invalid = json.dumps(
        {"tests": [_document_test(steps=[_question_step(document_ids=["DOC-GHOST"])])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown document 'DOC-GHOST'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_accepts_missing_evidence_as_a_concrete_blocked_step():
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "tests": [
                        _document_test(
                            steps=[
                                _question_step(
                                    document_ids=[], missing_evidence="Signed approval memo"
                                )
                            ]
                        )
                    ]
                }
            )
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    step = result.proposal["tests"][0]["steps"][0]
    assert list(step["document_ids"]) == []
    assert step["missing_evidence"] == "Signed approval memo"


def test_generate_worker_preserves_missing_evidence_as_a_sourced_scope_limitation():
    response = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[_question_step(missing_evidence="Signed approval memo")]
                )
            ]
        }
    )
    gateway = _Gateway([response])

    result = WORKERS.execute(_request(), gateway)

    step = result.proposal["tests"][0]["steps"][0]
    assert step["missing_evidence"] == ""
    assert step["scope_limitation"] == "Signed approval memo"


def test_generate_worker_rejects_sandbox_invalid_code():
    invalid = json.dumps(
        {"tests": [_data_test(steps=[_data_step(code="import os\nresult = df")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="not allowed in the sandbox"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_code_without_result():
    invalid = json.dumps(
        {"tests": [_data_test(steps=[_data_step(code="output = transactions.head(1)")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="assign the exception rows to `result`"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_reports_every_contract_error_in_one_repair():
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "tests": [
                        _data_test(
                            objective="",
                            steps=[_data_step(code="result = transactions.filter(pl.col('ghost') > 0)")],
                        ),
                        _document_test(
                            steps=[_question_step(question=""), _vouch_step()]
                        ),
                    ]
                }
            ),
            json.dumps({"tests": [_data_test(), _document_test()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    conversation = gateway.calls[1]["conversation"]
    assert [message["role"] for message in conversation] == [
        "user",
        "assistant",
        "user",
    ]
    assert '"objective": ""' in conversation[1]["content"]
    assert "ghost" in conversation[1]["content"]
    guidance = conversation[2]["content"]
    assert "tests[0].objective" in guidance
    assert "unknown column 'ghost'" in guidance
    assert "removed vouch-step schema" in guidance
    assert "preserving every unaffected test and field" in guidance


def test_generate_worker_rejects_a_source_the_workspace_cannot_supply():
    invalid = json.dumps({"tests": [_data_test()]})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="no table is available"):
        WORKERS.execute(_request(_bundle(tables=())), gateway)


def test_generate_worker_rejects_an_empty_test_array():
    invalid = json.dumps({"tests": []})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="tests must be a non-empty array"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_requires_exactly_one_target_row():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    with pytest.raises(WorkerContractError, match="'rcm_row' must supply exactly one item"):
        WORKERS.execute(_request(_bundle(rcm_rows=("RCM-1", "RCM-2"))), gateway)


# --------------------------------------------------------------------------- #
# Locally absorbed deviations: corrections with exactly one possible outcome
# are applied here rather than spent as a repair turn, which observed runs
# showed was consuming the allowance the semantic errors needed.
# --------------------------------------------------------------------------- #
def test_generate_worker_strips_a_redundant_polars_import():
    step = _data_step(
        code="import polars as pl\nresult = transactions.filter(pl.col('invoice').is_duplicated())"
    )
    gateway = _Gateway([json.dumps({"tests": [_data_test(steps=[step])]})])

    result = WORKERS.execute(_request(), gateway)

    code = result.proposal["tests"][0]["steps"][0]["code"]
    assert "import" not in code
    assert code.startswith("result =")
    # One call: the import cost no repair turn.
    assert len(gateway.calls) == 1


def test_generate_worker_still_refuses_an_import_the_sandbox_does_not_supply():
    """Only names the snippet is handed anyway are absorbed."""
    invalid = json.dumps(
        {"tests": [_data_test(steps=[_data_step(code="import os\nresult = df")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="not allowed in the sandbox"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_derives_a_missing_step_label_from_its_question():
    step = _question_step()
    step.pop("label")
    step.pop("instruction")
    gateway = _Gateway([json.dumps({"tests": [_document_test(steps=[step])]})])

    result = WORKERS.execute(_request(), gateway)

    committed = result.proposal["tests"][0]["steps"][0]
    assert committed["label"] == "Was this payment approved before release?"
    assert committed["instruction"] == "Was this payment approved before release?"
    assert len(gateway.calls) == 1


# --------------------------------------------------------------------------- #
# Partial acceptance on exhaustion
# --------------------------------------------------------------------------- #
def test_generate_worker_commits_valid_siblings_when_repair_is_exhausted():
    """A defective test must not take its valid siblings down with it."""
    broken = _data_test(
        title="Broken", steps=[_data_step(code="result = df.select(pl.col('ghost'))")]
    )
    response = json.dumps({"tests": [_data_test(), broken]})
    gateway = _Gateway([response, response, response])

    result = WORKERS.execute(_request(), gateway)

    assert result.partial is True
    titles = [test["title"] for test in result.proposal["tests"]]
    assert titles == ["Duplicate payment detection"]
    # The allowance was still spent trying to fix it first.
    assert len(gateway.calls) == 3


def test_generate_worker_keeps_a_clean_response_whole_and_not_partial():
    gateway = _Gateway([json.dumps({"tests": [_data_test(), _document_test()]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.partial is False
    assert len(result.proposal["tests"]) == 2




# --------------------------------------------------------------- cycle tests
def _approved_rules(**overrides):
    """The candidate the adapter supplies for a workspace with approved rules."""

    candidate = {
        "kind": "ruleset",
        "ruleset_id": "lnk-1",
        "ruleset_hash": "sha256:rules",
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice", "required": True},
            {"name": "order", "document_type": "purchase_order", "required": True},
        ],
        "anchor": {
            "table": "transactions", "column": "invoice",
            "role": "invoice", "field": "invoice_number",
        },
        "assertions": [{
            "id": "as_total", "requirement": "The records must agree.", "label": "Totals agree",
            "rationale": "The amount billed must be the amount ordered.",
        }],
        "reach": {
            "population_rows": 40, "linked_rows": 32,
            "complete_cycles": 30, "missing_role_counts": {},
        },
        "selection_confirmation": None,
    }
    candidate.update(overrides)
    return candidate


def _cycle_row(**overrides):
    row = {
        "id": "RCM-1",
        "risk": "Payments may be made for goods never ordered",
        "control": "Every invoice is matched to an approved purchase order",
        "existing_tests": [],
        "control_attributes": [{
            "key": "invoice_match",
            "assertion": "Accuracy",
            "requirement": "The invoice agrees to the order it bills against.",
            "evidence_kind": "transaction_cycle",
            "required_comparisons": [{
                "key": "totals_agree",
                "left": {"document_type": "vendor_invoice", "field": "total_amount"},
                "right": {"document_type": "purchase_order", "field": "total_amount"},
            }],
        }],
    }
    row.update(overrides)
    return row


def _cycle_bundle(**overrides):
    return _bundle(
        rcm_payload=_cycle_row(),
        transaction_manifest=_approved_rules(),
        **overrides,
    )


def _cycle_test(**overrides):
    value = {
        "source": "document",
        "kind": "cycle_vouch",
        "title": "Vouch invoices to purchase orders",
        "objective": "Vouch each selected invoice to its approved order.",
        "requirement_refs": ["RCM-1:invoice_match"],
        "procedure_key": "match_invoice_to_order",
        "selection": {"mode": "evidence_linked"},
    }
    value.update(overrides)
    return value


def test_a_cycle_test_names_its_rules_and_its_rows_and_nothing_else():
    gateway = _Gateway([json.dumps({"tests": [_cycle_test()]})])

    result = WORKERS.execute(_request(_cycle_bundle()), gateway)

    proposed = result.proposal["tests"][0]
    assert proposed["definition"]["ruleset_id"] == "lnk-1"
    assert proposed["definition"]["population"]["selection"] == {
        "mode": "evidence_linked"
    }
    # The roles, join keys and assertions are read from the approved ruleset.
    assert "roles" not in proposed["definition"]
    assert "assertions" not in proposed["definition"]


def test_the_turn_is_shown_the_rules_rather_than_asked_to_restate_them():
    gateway = _Gateway([json.dumps({"tests": [_cycle_test()]})])

    WORKERS.execute(_request(_cycle_bundle()), gateway)

    supplied = json.loads(gateway.calls[0]["user"])["transaction_evidence"]
    assert supplied["available"] is True
    assert supplied["population"] == {"table": "transactions", "column": "invoice"}
    assert [role["document_type"] for role in supplied["roles"]] == [
        "vendor_invoice", "purchase_order"
    ]
    # Identity and hashes help it choose nothing.
    assert "ruleset_hash" not in supplied


def test_a_cycle_test_is_refused_where_no_rules_are_approved():
    """The turn is told what to return instead, rather than left to guess."""

    stubborn = json.dumps({"tests": [_cycle_test()]})
    gateway = _Gateway([stubborn, stubborn, stubborn])

    with pytest.raises(WorkerRunError) as raised:
        WORKERS.execute(_request(_bundle(rcm_payload=_cycle_row())), gateway)

    assert "no approved cycle ruleset" in str(raised.value)


def test_a_requirement_the_row_does_not_state_is_refused():
    stubborn = json.dumps(
        {"tests": [_cycle_test(requirement_refs=["RCM-1:invented"])]}
    )
    gateway = _Gateway([stubborn, stubborn, stubborn])

    with pytest.raises(WorkerRunError) as raised:
        WORKERS.execute(_request(_cycle_bundle()), gateway)

    assert "cite no transaction_cycle control attribute" in str(raised.value)


def test_a_cycle_attribute_left_untested_is_reported():
    """A three-way match could otherwise be answered by joining the ledgers to
    themselves — complete by every structural rule, and no voucher examined."""

    ordinary = {
        "source": "document", "title": "Read the invoices",
        "objective": "Read them.",
        "steps": [{
            "label": "Read", "instruction": "Read it.", "mode": "question",
            "document_ids": ["DOC-1"], "question": "What does it say?",
        }],
    }
    stubborn = json.dumps({"tests": [ordinary]})
    gateway = _Gateway([stubborn, stubborn, stubborn])

    with pytest.raises(WorkerRunError) as raised:
        WORKERS.execute(_request(_cycle_bundle()), gateway)

    assert "invoice_match" in str(raised.value)
    assert "no returned cycle_vouch test references it" in str(raised.value)


def test_a_procedure_admitting_it_does_not_cover_its_requirement_is_refused():
    """No substitute is accepted: the strategy is corrected, or the evidence is
    supplied. Papering over it would report the requirement as tested."""

    admitted = _cycle_test(
        objective="Vouch the orders; the receipts are not available to test.",
    )
    stubborn = json.dumps({"tests": [admitted]})
    gateway = _Gateway([stubborn, stubborn, stubborn])

    with pytest.raises(WorkerRunError) as raised:
        WORKERS.execute(_request(_cycle_bundle()), gateway)

    assert "admits that the proposed cycle procedure does not cover" in str(
        raised.value
    )


def _row_with_tests(*ids):
    return {
        "id": "RCM-1",
        "risk": "Duplicate payments are processed",
        "control": "Duplicate invoice validation",
        "existing_tests": [
            {"id": test_id, "title": "Existing", "created_by": "agent"}
            for test_id in ids
        ],
    }


def test_generate_worker_carries_the_test_a_proposal_says_it_revises():
    bundle = _bundle(rcm_payload=_row_with_tests("DAT-EXISTING1"))
    gateway = _Gateway(
        [json.dumps({"tests": [_data_test(revises="DAT-EXISTING1")]})]
    )

    result = WORKERS.execute(_request(bundle), gateway)

    assert result.proposal["tests"][0]["revises"] == "DAT-EXISTING1"


def test_generate_worker_defaults_revises_to_empty_for_a_new_test():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["tests"][0]["revises"] == ""


def test_generate_worker_rejects_a_revises_that_is_not_on_this_row():
    """Rejected rather than ignored.

    Ignoring it would fall back to the title-derived match, which is the
    behaviour that stored a reworded test beside the original instead of
    replacing it. One repair turn is cheaper than another duplicate.
    """
    bundle = _bundle(rcm_payload=_row_with_tests("DAT-EXISTING1"))
    gateway = _Gateway(
        [
            json.dumps({"tests": [_data_test(revises="DAT-NOTHERE")]}),
            json.dumps({"tests": [_data_test(revises="DAT-EXISTING1")]}),
        ]
    )

    result = WORKERS.execute(_request(bundle), gateway)

    guidance = gateway.calls[1]["conversation"][-1]["content"]
    assert "DAT-NOTHERE" in guidance
    assert "DAT-EXISTING1" in guidance
    assert result.proposal["tests"][0]["revises"] == "DAT-EXISTING1"
