"""Phase 3 gates for cycle item materialization and deterministic evaluation."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app import cycle_vouching, doc_tests, rcm_execution, workspaces
from app.agent.capabilities import doc_tests as doc_test_capabilities
from app.agent.executors import fieldwork
from app.agent.executors.reporting import verify_audit


FIXTURE = Path(__file__).parent / "fixtures" / "procurement_cycle_phase0.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _workspace(contract: dict, monkeypatch, *, selection: dict | None = None):
    workspace = workspaces.create_workspace("Phase 3 cycle")
    workspace.add_table(
        "invoice_data.csv",
        (
            b"INVOICE_ID,VENDOR_INVOICE_NUMBER,PO_NUMBER_LINK,GRN_ID_LINK,INVOICE_AMOUNT\n"
            b"INV2024004,V-INV-778,PO-2024-004,GRN-2024-011,2000000\n"
        ),
    )
    workspace = workspaces.load_workspace(workspace.id)
    workspace.add_rcm(
        {
            "id": contract["cycle_test"]["rcm_id"],
            "process": "Procure to pay",
            "risk": "Payments may lack a complete procurement cycle.",
            "control": "Payments are supported by the procurement record pack.",
        }
    )
    workspace = workspaces.load_workspace(workspace.id)
    test = copy.deepcopy(contract["cycle_test"])
    test.update(
        title="Five-document procurement cycle",
        objective="Vouch the complete procurement cycle.",
        semantic_id=cycle_vouching.stable_test_semantic_id(test),
        items=[],
    )
    if selection is not None:
        test["definition"]["population"]["selection"] = selection
    records = copy.deepcopy(contract["reduction"]["records"])
    extraction_hashes = {
        record["document_id"]: f"sha256:extract-{record['record_id']}"
        for record in records
    }
    current = {"records": records, "extraction_hashes": extraction_hashes}
    monkeypatch.setattr(
        cycle_vouching,
        "_current_records",
        lambda *_args, **_kwargs: (
            copy.deepcopy(current["records"]),
            copy.deepcopy(current["extraction_hashes"]),
        ),
    )
    monkeypatch.setattr(cycle_vouching, "_evidence_catalog", lambda *_args: {})
    created = doc_tests.create_test(workspace, test)
    return workspace, created, current


def test_materializer_builds_stable_complete_role_bindings(contract, monkeypatch):
    workspace, test, _current = _workspace(contract, monkeypatch)

    first = cycle_vouching.materialize_cycle_items(workspace, test)
    second = cycle_vouching.materialize_cycle_items(workspace, test)

    assert first == second
    assert len(first) == 1
    item = first[0]
    assert item["id"].startswith("ITEM-")
    assert item["population_ref"]["table"] == "invoice_data"
    assert item["frozen_row"]["INVOICE_ID"] == "INV2024004"
    assert item["missing_roles"] == []
    assert {binding["role"] for binding in item["role_bindings"]} == {
        role["role"] for role in test["definition"]["roles"]
    }
    assert all(binding["matched_by"] for binding in item["role_bindings"])
    assert all(binding["record_content_hash"] for binding in item["role_bindings"])
    assert all(binding["extraction_hash"] for binding in item["role_bindings"])
    assert "state" not in item

    # The Phase 4 summary projects Cycle vouching once at test grain rather
    # than flattening every transaction into the ordinary item worklist. The
    # read still materializes locally and leaves the durable test untouched.
    summary = doc_tests.summary_payload(workspace)
    assert [(value["entry_type"], value["test_id"]) for value in summary["entries"]] == [
        ("cycle_test", test["id"])
    ]
    assert summary["tested_item_counts"]["not_run"] == 1
    stored = json.loads(
        (workspace.root / "DocTests" / f"{test['id']}.json").read_text("utf-8")
    )
    assert stored["items"] == []


def test_scalar_results_distinguish_match_invalid_missing_and_ambiguity(
    contract, monkeypatch
):
    workspace, test, current = _workspace(contract, monkeypatch)
    # Raw evidence is present but normalization failed: this must not collapse
    # into the same result as an absent role/field.
    receipt = next(
        record
        for record in current["records"]
        if record["record_kind"] == "procure_to_pay.goods_receipt"
    )
    receipt_date = receipt["fields"][0]["value"]
    receipt_date.update(
        raw_value="29-Apr -2024",
        value=None,
        normalization_status="invalid",
        normalization_error="unrecognized date format",
    )
    receipt["content_hash"] = cycle_vouching._canonical_hash(
        {key: value for key, value in receipt.items() if key != "content_hash"}
    )
    item = cycle_vouching.materialize_cycle_items(workspace, test)[0]
    evaluated = cycle_vouching.evaluate_cycle_item(
        workspace, test, item, records=current["records"]
    )

    assert evaluated["result_by_assertion"]["invoice_amount_to_payment"]["verdict"] == "match"
    invalid = evaluated["result_by_assertion"]["receipt_before_payment"]
    assert invalid["verdict"] == "invalid_extraction"
    assert invalid["comparisons"][0]["entries"][0]["raw_value"] == "29-Apr -2024"
    assert evaluated["result_by_assertion"]["requisition_approved"]["verdict"] == "match"
    assert evaluated["evaluation"]["state"] == "incomplete"
    assert evaluated["disposition"]["state"] == "pending"

    payment = next(
        record
        for record in current["records"]
        if record["record_kind"] == "procure_to_pay.payment_voucher"
    )
    payment_total = next(
        field
        for field in payment["fields"]
        if field["group"] == "amounts" and field["attribute"] == "value"
    )
    payment_total["value"].update(raw_value="2100000", value=2100000)
    payment["content_hash"] = cycle_vouching._canonical_hash(
        {key: value for key, value in payment.items() if key != "content_hash"}
    )
    mismatch_item = cycle_vouching.materialize_cycle_items(workspace, test)[0]
    mismatch = cycle_vouching.evaluate_cycle_item(
        workspace, test, mismatch_item, records=current["records"]
    )
    assert mismatch["result_by_assertion"]["invoice_amount_to_payment"]["verdict"] == "mismatch"
    assert mismatch["evaluation"]["state"] == "failed"
    payment_total["value"].update(raw_value="2000000", value=2000000)
    payment["content_hash"] = cycle_vouching._canonical_hash(
        {key: value for key, value in payment.items() if key != "content_hash"}
    )

    ambiguous_test = copy.deepcopy(test)
    duplicate = copy.deepcopy(
        next(
            record
            for record in current["records"]
            if record["record_kind"] == "procure_to_pay.vendor_invoice"
        )["fields"][0]
    )
    duplicate["entry"] = 1
    duplicate["value"].update(raw_value="2100000", value=2100000)
    invoice = next(
        record
        for record in current["records"]
        if record["record_kind"] == "procure_to_pay.vendor_invoice"
    )
    invoice["fields"].append(duplicate)
    invoice["content_hash"] = cycle_vouching._canonical_hash(
        {key: value for key, value in invoice.items() if key != "content_hash"}
    )
    ambiguous_item = cycle_vouching.materialize_cycle_items(workspace, ambiguous_test)[0]
    ambiguous = cycle_vouching.evaluate_cycle_item(
        workspace, ambiguous_test, ambiguous_item, records=current["records"]
    )
    assert ambiguous["result_by_assertion"]["invoice_amount_to_payment"]["verdict"] == "ambiguous"
    assert ambiguous["evaluation"]["state"] == "needs_review"

    sampled = copy.deepcopy(test)
    sampled["definition"]["population"]["selection"] = {
        "mode": "sample",
        "method": "random",
        "size": 1,
        "seed": 42,
        "assurance_scope": "sampled_population",
    }
    current["records"] = []
    missing_item = cycle_vouching.materialize_cycle_items(workspace, sampled)[0]
    missing = cycle_vouching.evaluate_cycle_item(workspace, sampled, missing_item, records=[])
    assert {result["verdict"] for result in missing["result_by_assertion"].values()} == {
        "missing_evidence"
    }
    assert missing["evaluation"]["state"] == "incomplete"


def test_explicit_role_set_keeps_every_document_sub_result(contract, monkeypatch):
    workspace, test, current = _workspace(contract, monkeypatch)
    catalog = {}
    for record in current["records"]:
        for field in record.get("fields") or []:
            citations = (field.get("value") or {}).get("citation")
            for citation_id in citations if isinstance(citations, list) else [citations]:
                if citation_id:
                    catalog[(record["document_id"], str(citation_id))] = {
                        "id": f"EV-{record['record_id']}-{citation_id}",
                        "source_kind": "document",
                        "source_id": record["document_id"],
                        "source_sha1": "sha1:document",
                        "page": 1,
                        "excerpt": str(citation_id),
                    }
    monkeypatch.setattr(cycle_vouching, "_evidence_catalog", lambda *_args: catalog)
    assertion = {
        "key": "recorded_total_agrees",
        "label": "Recorded total agrees across applicable documents",
        "left": {"source": "row", "column": "INVOICE_AMOUNT"},
        "right": {
            "source": "roles",
            "roles": ["vendor_invoice", "payment_voucher"],
            "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            "entry_quantifier": "one",
        },
        "operator": "numeric_within",
        "tolerance": {"absolute": 0.01, "percent": 0},
        "role_quantifier": "all",
    }
    test["definition"]["assertions"] = [assertion]
    item = cycle_vouching.materialize_cycle_items(workspace, test)[0]
    evaluated = cycle_vouching.evaluate_cycle_item(
        workspace, test, item, records=current["records"]
    )
    result = evaluated["result_by_assertion"]["recorded_total_agrees"]

    assert result["verdict"] == "match"
    assert [(entry["role"], entry["verdict"]) for entry in result["comparisons"]] == [
        ("vendor_invoice", "match"),
        ("payment_voucher", "match"),
    ]
    assert all(entry["record_ids"] for entry in result["comparisons"])
    assert all(entry["entries"][0]["evidence_refs"] for entry in result["comparisons"])
    assert len(result["evidence_refs"]) == 2


def test_cardinality_conflict_evaluates_as_ambiguity_without_first_match(
    contract, monkeypatch
):
    workspace, test, current = _workspace(contract, monkeypatch)
    purchase_order = next(
        record
        for record in current["records"]
        if record["record_kind"] == "procure_to_pay.purchase_order"
    )
    competing = copy.deepcopy(purchase_order)
    competing.update(record_id="REC-PO-COMPETING", document_id="DOC-PO-COMPETING")
    competing["content_hash"] = cycle_vouching._canonical_hash(
        {key: value for key, value in competing.items() if key != "content_hash"}
    )
    current["records"].append(competing)
    current["extraction_hashes"][competing["document_id"]] = "sha256:competing"
    test["definition"]["assertions"] = [
        {
            "key": "po_date_present",
            "label": "Purchase order date is present",
            "left": {
                "source": "role",
                "role": "purchase_order",
                "field": {"group": "dates", "kind": "document_date", "attribute": "value"},
            },
            "operator": "present",
        }
    ]
    item = cycle_vouching.materialize_cycle_items(workspace, test)[0]
    assert item["role_conflicts"][0]["role"] == "purchase_order"
    assert not any(binding["role"] == "purchase_order" for binding in item["role_bindings"])

    evaluated = cycle_vouching.evaluate_cycle_item(
        workspace, test, item, records=current["records"]
    )
    result = evaluated["result_by_assertion"]["po_date_present"]
    assert result["verdict"] == "ambiguous"
    assert sorted(result["comparisons"][0]["record_ids"]) == [
        "REC-PO",
        "REC-PO-COMPETING",
    ]


@pytest.mark.parametrize(
    "selection",
    [
        {"mode": "sample", "method": "random", "size": 3, "seed": 42, "assurance_scope": "sampled_population"},
        {"mode": "sample", "method": "interval", "size": 3, "seed": 42, "assurance_scope": "sampled_population"},
        {"mode": "sample", "method": "stratified", "size": 3, "seed": 42, "stratify_by": "GROUP", "assurance_scope": "sampled_population"},
    ],
)
def test_all_sample_methods_materialize_missing_evidence_rows_deterministically(
    contract, monkeypatch, selection
):
    workspace = workspaces.create_workspace("Phase 3 deterministic sample")
    workspace.add_table(
        "population.csv",
        b"INVOICE_ID,PO_NUMBER_LINK,GROUP\n"
        b"INV-1,PO-1,A\nINV-2,PO-2,A\nINV-3,PO-3,A\n"
        b"INV-4,PO-4,B\nINV-5,PO-5,B\nINV-6,PO-6,B\n",
    )
    workspace = workspaces.load_workspace(workspace.id)
    test = copy.deepcopy(contract["cycle_test"])
    test.update(items=[])
    population = test["definition"]["population"]
    population.update(
        table="population",
        row_key={"column": "INVOICE_ID", "identifier_kind": "procure_to_pay.internal_invoice_id"},
        cycle_keys=[{"column": "PO_NUMBER_LINK", "identifier_kind": "procure_to_pay.purchase_order_number"}],
        selection=selection,
    )
    test["semantic_id"] = cycle_vouching.stable_test_semantic_id(test)
    monkeypatch.setattr(cycle_vouching, "_current_records", lambda *_args, **_kwargs: ([], {}))

    first = cycle_vouching.materialize_cycle_items(workspace, test)
    second = cycle_vouching.materialize_cycle_items(workspace, test)

    assert first == second
    assert len(first) == 3
    assert all(item["missing_roles"] for item in first)
    if selection["method"] == "stratified":
        assert {item["frozen_row"]["GROUP"] for item in first} == {"A", "B"}


def test_hash_staleness_is_selective_and_current_results_do_not_rerun(
    contract, monkeypatch
):
    workspace, test, current = _workspace(contract, monkeypatch)
    evaluated_test = cycle_vouching.evaluate_cycle_test(workspace, test)
    item = evaluated_test["items"][0]
    original_results = copy.deepcopy(item["result_by_assertion"])
    item["disposition"] = {
        "state": "confirmed",
        "evaluated_definition_sha1": item["evaluation"]["definition_sha1"],
        "stale": False,
    }

    original_comparison = cycle_vouching._comparison
    monkeypatch.setattr(
        cycle_vouching,
        "_comparison",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a current result was recomputed")
        ),
    )
    unchanged = cycle_vouching.evaluate_cycle_test(
        workspace, {**evaluated_test, "items": [item]}
    )
    assert unchanged["items"][0]["result_by_assertion"] == original_results

    # Restore the evaluator and change only the receipt record.  Assertions
    # that do not consume that role retain their exact result hashes.
    monkeypatch.setattr(cycle_vouching, "_comparison", original_comparison)
    prior = evaluated_test["items"][0]
    prior["disposition"] = {
        "state": "confirmed",
        "evaluated_definition_sha1": prior["evaluation"]["definition_sha1"],
        "stale": False,
    }
    receipt = next(
        record
        for record in current["records"]
        if record["record_kind"] == "procure_to_pay.goods_receipt"
    )
    receipt["fields"][0]["value"].update(raw_value="bad date", value=None, normalization_status="invalid", normalization_error="bad date")
    receipt["content_hash"] = cycle_vouching._canonical_hash(
        {key: value for key, value in receipt.items() if key != "content_hash"}
    )
    projected_test = {**evaluated_test, "items": [prior]}
    projected = cycle_vouching.materialize_cycle_items(workspace, projected_test)[0]
    assert projected["result_by_assertion"]["receipt_before_payment"]["verdict"] == "not_run"
    assert projected["result_by_assertion"]["receipt_before_payment"]["stale"] is True
    assert projected["result_by_assertion"]["invoice_amount_to_payment"] == prior["result_by_assertion"]["invoice_amount_to_payment"]
    assert projected["disposition"]["state"] == "confirmed"
    assert projected["disposition"]["stale"] is True
    assert doc_tests.item_execution_pending(projected_test, projected)
    assert not doc_tests.item_disposition_current(projected_test, projected)


def test_executor_materializes_once_then_waits_for_auditor_disposition(
    contract, monkeypatch
):
    workspace, test, _current = _workspace(contract, monkeypatch)

    first = fieldwork.run_document_test(
        workspace, test["id"], unit_id="UNIT-CYCLE", run_id="RUN-CYCLE"
    )
    stored = doc_tests.load_test(workspace, test["id"])

    assert first.executed is True
    assert stored["status"] == "review_required"
    assert len(stored["items"]) == 1
    assert doc_test_capabilities.unexecuted_items(stored, workspace) == 0
    assert doc_test_capabilities.undispositioned_items(stored) == 1
    assert stored["items"][0]["disposition"]["state"] == "pending"

    second = fieldwork.run_document_test(
        workspace, test["id"], unit_id="UNIT-CYCLE-2", run_id="RUN-CYCLE-2"
    )
    assert second.executed is False

    signed = doc_tests.update_item(
        workspace,
        test["id"],
        stored["items"][0]["id"],
        {"state": "confirmed"},
    )
    assert signed["status"] == "completed"
    assert doc_tests.item_disposition_current(signed, signed["items"][0])


def test_audit_completion_never_accepts_pending_or_stale_cycle_disposition(
    contract, monkeypatch
):
    workspace, test, _current = _workspace(contract, monkeypatch)
    fieldwork.run_document_test(
        workspace, test["id"], unit_id="UNIT-CYCLE", run_id="RUN-CYCLE"
    )
    pending = rcm_execution.completion(workspace)
    assert pending["pending_cycle_dispositions"] == [
        {
            "rcm_id": contract["cycle_test"]["rcm_id"],
            "test_id": test["id"],
            "item_id": doc_tests.load_test(workspace, test["id"])["items"][0]["id"],
        }
    ]
    assert verify_audit(workspace)["completion_status"] == "completed_with_open_items"

    stored = doc_tests.load_test(workspace, test["id"])
    stored["items"][0]["disposition"] = {
        "state": "confirmed",
        "evaluated_definition_sha1": stored["items"][0]["evaluation"]["definition_sha1"],
        "stale": True,
    }
    stored["status"] = "completed"
    doc_tests.save_test(workspace, stored)
    stale = rcm_execution.completion(workspace)
    assert stale["pending_cycle_dispositions"][0]["test_id"] == test["id"]
    assert verify_audit(workspace)["audit_complete"] is False
