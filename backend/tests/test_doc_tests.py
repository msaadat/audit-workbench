import json

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import doc_tests, documents, llm, working_papers, workspaces
from app.agent import runner
from app.main import create_app
from conftest import wait_run


def _procedure(ws):
    rcm = ws.add_rcm({"process": "Purchasing", "risk": "Unsupported purchases"})
    procedure = ws.add_procedure({
        "objective": "Verify sampled purchases are supported",
        "criteria": "Invoices agree to the ledger and are approved.",
        "steps": ["Select a seeded sample.", "Compare invoices to ledger fields."],
        "method": "Document vouching",
        "rcm_refs": [rcm["id"]],
    })
    return rcm, procedure


def test_explainable_comparison_methods():
    assert doc_tests.compare_values("ABC-123", "ABC-123", "exact")["result"] == "match"
    normalized = doc_tests.compare_values("Acme, Inc.", " ACME INC ", "normalized")
    assert normalized["result"] == "match"
    assert normalized["normalization"] == {"expected": "acme inc", "found": "acme inc"}
    fuzzy = doc_tests.compare_values("purchase order approved", "purchase order aproved", "fuzzy", 80)
    assert fuzzy["result"] == "match"
    assert 0 < fuzzy["similarity"] <= 1
    numeric = doc_tests.compare_values(1000, "1,004", "numeric_tolerance", {"absolute": 5})
    assert numeric["result"] == "match"
    assert numeric["tolerance"]["allowed"] == 5
    date = doc_tests.compare_values("2026-01-10", "12/01/2026", "date_tolerance", {"days": 2})
    assert date["result"] == "match"
    assert doc_tests.compare_values("x", None, "exact")["result"] == "missing"


def test_vouching_builder_freezes_seeded_sample_outside_workspace(workspace_with_data):
    ws = workspace_with_data
    test = doc_tests.build_vouching(ws, {
        "title": "Invoice support", "table": "transactions", "size": 2,
        "seed": 17, "frozen_fields": ["invoice_no", "amount", "tx_date"],
    })
    assert test["spec"]["sampling"]["seed"] == 17
    assert len(test["items"]) == 2
    assert set(test["items"][0]["frozen"]) == {"invoice_no", "amount", "tx_date"}
    assert (ws.root / "DocTests" / f"{test['id']}.json").is_file()
    workspace_json = json.loads((ws.root / "workspace.json").read_text(encoding="utf-8"))
    assert "DocTests" not in workspace_json
    assert "frozen" not in workspace_json


def test_local_multi_document_matching_records_anchors_and_conflicts(workspace_with_data):
    ws = workspace_with_data
    test = doc_tests.create_test(ws, {
        "kind": "vouching", "title": "Three-way match",
        "spec": {"direction": "vouching", "require_all_documents": True},
        "items": [{
            "label": "Invoice 1001", "frozen": {"invoice_no": 1001, "amount": 150.0},
            "checks": [
                {"field": "invoice_no", "expected": 1001, "method": "normalized"},
                {"field": "amount", "expected": 150.0, "method": "numeric_tolerance"},
            ],
        }],
    })
    invoice = documents.add_document(ws, "invoice-1001.txt", b"Invoice number: 1001\nTotal amount: 150.00\n")
    receipt = documents.add_document(ws, "receipt-1001.txt", b"Receipt for invoice 1001\nPaid 150.00\n")
    for doc in (invoice, receipt):
        doc_tests.attach_document(ws, test["id"], test["items"][0]["id"], doc["id"])
    item = doc_tests.run_item(ws, test["id"], test["items"][0]["id"], run_id="RUN-1")
    assert item["state"] == "confirmed"
    assert all(check["verdict"] == "match" for check in item["checks"])
    assert all(len(check["comparisons"]) == 2 for check in item["checks"])
    anchor = item["checks"][0]["evidence_refs"][0]
    assert anchor["source_sha1"] == invoice["sha1"]
    assert anchor["page"] == 1 and anchor["excerpt_hash"]

    duplicate = documents.add_document(ws, "copy-of-invoice.txt", b"Invoice number: 1001\nTotal amount: 150.00\n")
    doc_tests.attach_document(ws, test["id"], item["id"], duplicate["id"])
    conflicted = doc_tests.run_item(ws, test["id"], item["id"])
    assert conflicted["state"] == "manual_review"
    assert conflicted["document_conflicts"]["duplicate_documents"]


def test_replaced_document_does_not_create_an_attachment_conflict(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "voucher.txt", b"Voucher 1001 amount 150")
    replaced = documents.add_document(
        ws, "voucher.txt", b"Voucher 1001 amount 151", replace=True,
    )
    test = doc_tests.create_test(ws, {
        "kind": "vouching", "title": "Replacement check",
        "items": [{"label": "1001", "checks": [{"field": "id", "expected": 1001}], "document_ids": [document["id"]]}],
    })
    item = doc_tests.run_item(ws, test["id"], test["items"][0]["id"])
    assert replaced["id"] == document["id"]
    assert item["state"] == "confirmed"
    assert item["document_conflicts"] == {"duplicate_documents": []}


def test_all_four_builders_expose_final_result_states(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "policy.txt", b"Approval is required.\nEvidence must be retained.")
    attribute = doc_tests.build_attribute(ws, {"title": "Attributes", "attributes": [{"name": "Approval"}]})
    review = doc_tests.build_review(ws, {"title": "Policy review", "document_id": document["id"]})
    qa = doc_tests.build_qa(ws, {"title": "Policy Q&A", "document_ids": [document["id"]], "questions": ["Is approval required?"]})
    assert {attribute["kind"], review["kind"], qa["kind"]} == {"attribute", "review", "qa"}
    assert review["items"][0]["evidence_refs"][0]["source_sha1"] == document["sha1"]
    checked = doc_tests.run_item(ws, review["id"], review["items"][0]["id"])
    assert checked["state"] == "manual_review"


def test_auditor_can_sign_off_an_item_stuck_in_manual_review(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "policy.txt", b"Approval is required.\nEvidence must be retained.")
    review = doc_tests.build_review(ws, {"title": "Policy review", "document_id": document["id"]})
    doc_tests.run_item(ws, review["id"], review["items"][0]["id"])
    item_id = review["items"][0]["id"]

    confirmed = doc_tests.update_item(ws, review["id"], item_id, {"state": "confirmed"})
    assert confirmed["items"][0]["state"] == "confirmed"
    assert confirmed["status"] == "completed"

    excepted = doc_tests.update_item(ws, review["id"], item_id, {"state": "exception"})
    assert excepted["items"][0]["state"] == "exception"
    # Signing off never rewrites what the runner found.
    assert excepted["items"][0]["evaluation"]["state"] == "inconclusive"

    # Clearing a sign-off drops the auditor's call and falls back to the
    # runner's verdict; it does not pretend the run never happened.
    reset = doc_tests.update_item(ws, review["id"], item_id, {"state": "pending"})
    assert reset["items"][0]["disposition"]["state"] == "pending"
    assert reset["items"][0]["state"] == "manual_review"
    assert reset["items"][0]["evaluation"]["state"] == "inconclusive"
    assert reset["status"] == "review_required"

    # Parking an item for a second pair of eyes is an auditor's call to make.
    parked = doc_tests.update_item(
        ws, review["id"], item_id,
        {"state": "needs_review", "disposition_note": "Ask the engagement manager."},
    )
    assert parked["items"][0]["disposition"]["state"] == "needs_review"
    assert parked["items"][0]["disposition"]["note"] == "Ask the engagement manager."
    assert parked["status"] == "review_required"

    # Runner-owned outcomes stay runner-owned.
    for state in ("manual_review", "agent_checked"):
        with pytest.raises(workspaces.WorkspaceError):
            doc_tests.update_item(ws, review["id"], item_id, {"state": state})


def test_summary_reports_where_each_test_conclusion_stands(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "policy.txt", b"Approval is required.")
    review = doc_tests.build_review(ws, {"title": "Policy review", "document_id": document["id"]})
    item_id = review["items"][0]["id"]
    doc_tests.run_item(ws, review["id"], item_id)
    unattended = doc_tests.build_qa(ws, {
        "title": "Policy Q&A", "document_ids": [document["id"]],
        "questions": ["Is approval required?"],
    })

    def states():
        return {
            entry["test_id"]: entry["conclusion_state"]
            for entry in doc_tests.summary_payload(ws)["entries"]
        }

    assert states() == {review["id"]: "none", unattended["id"]: "none"}

    # Written reasoning is not a conclusion: a run that narrates its result and
    # still reaches no_conclusion has left the sign-off outstanding.
    drafted = doc_tests.load_test(ws, unattended["id"])
    drafted["conclusion"] = "The policy requires approval."
    drafted["conclusion_source"] = "agent"
    doc_tests.save_test(ws, drafted)
    assert states()[unattended["id"]] == "none"

    # A bounded run's own control conclusion is recorded and reported as its
    # own, so an auditor can filter for the ones nobody has reviewed.
    drafted["control_conclusion"] = "effective"
    drafted["control_conclusion_source"] = "agent"
    doc_tests.save_test(ws, drafted)
    assert states()[unattended["id"]] == "agent"

    doc_tests.update_item(ws, review["id"], item_id, {"state": "confirmed"})
    doc_tests.update_test(ws, review["id"], {"control_conclusion": "effective"})
    assert states()[review["id"]] == "auditor"

    # Withdrawing the sign-off the conclusion rested on does not delete it. The
    # worklist reports it as no longer standing on current evidence.
    doc_tests.update_item(ws, review["id"], item_id, {"state": "pending"})
    assert states()[review["id"]] == "stale"


_MATCHING_CHECK = {"field": "invoice", "expected": 1001, "method": "normalized"}


def _vouching_with_result(ws, checks=None):
    document = documents.add_document(
        ws,
        f"invoice-{len(ws.documents) + 1}.txt",
        b"Invoice 1001\nAmount 150.00\nApproved by the controller.",
    )
    test = doc_tests.create_test(ws, {
        "kind": "vouching", "title": "Invoice support",
        "items": [{
            "label": "Invoice 1001",
            "document_ids": [document["id"]],
            "checks": list(checks or [_MATCHING_CHECK]),
        }],
    })
    item_id = test["items"][0]["id"]
    doc_tests.run_item(ws, test["id"], item_id)
    return document, test["id"], item_id


def test_an_auditor_may_overturn_a_runner_verdict_and_both_stay_on_the_record(
    workspace_with_data,
):
    ws = workspace_with_data
    # One check matches, so the evidence is usable; the other does not, which is
    # a deterministic failure rather than an item the runner could not read.
    _document, test_id, item_id = _vouching_with_result(ws, [
        _MATCHING_CHECK,
        {"field": "amount", "expected": 999, "method": "numeric_tolerance"},
    ])
    ran = doc_tests.load_test(ws, test_id)["items"][0]
    assert ran["evaluation"]["state"] == "failed"
    assert ran["state"] == "exception"

    overturned = doc_tests.update_item(
        ws, test_id, item_id,
        {
            "state": "confirmed",
            "disposition_note": "Vendor reissued the invoice under a new number.",
        },
    )
    item = overturned["items"][0]
    # The auditor's call wins the joint reading without erasing the disagreement.
    assert item["state"] == "confirmed"
    assert item["evaluation"]["state"] == "failed"
    assert item["disposition"]["state"] == "confirmed"
    assert item["disposition"]["note"].startswith("Vendor reissued")
    assert item["disposition"]["actor"] == "auditor"
    assert item["disposition"]["at"]
    assert overturned["status"] == "completed"


def test_new_evidence_makes_a_sign_off_stale_instead_of_discarding_it(
    workspace_with_data,
):
    ws = workspace_with_data
    _document, test_id, item_id = _vouching_with_result(ws)
    doc_tests.update_item(
        ws, test_id, item_id, {"state": "confirmed", "disposition_note": "Agreed."}
    )

    second = documents.add_document(ws, "credit-note.txt", b"Credit note 55 against 1001.")
    doc_tests.attach_document(ws, test_id, item_id, second["id"])

    item = doc_tests.load_test(ws, test_id)["items"][0]
    # The sign-off is still on the record — it just no longer counts as current.
    assert item["disposition"]["state"] == "confirmed"
    assert item["disposition"]["note"] == "Agreed."
    assert item["disposition"]["stale"] is True
    assert item["evaluation"]["state"] == "not_run"
    assert item["state"] == "pending"
    assert doc_tests.load_test(ws, test_id)["status"] == "in_progress"

    # Re-running against the new evidence and re-signing clears the staleness.
    doc_tests.run_item(ws, test_id, item_id)
    resigned = doc_tests.update_item(ws, test_id, item_id, {"state": "confirmed"})
    assert resigned["items"][0]["disposition"]["stale"] is False
    assert resigned["status"] == "completed"


def test_editing_matching_rules_retires_the_run_behind_a_sign_off(workspace_with_data):
    ws = workspace_with_data
    _document, test_id, item_id = _vouching_with_result(ws)
    doc_tests.update_item(ws, test_id, item_id, {"state": "confirmed"})

    doc_tests.update_comparisons(
        ws, test_id, item_id,
        [{"field": "amount", "expected": 150, "method": "numeric_tolerance"}],
    )

    item = doc_tests.load_test(ws, test_id)["items"][0]
    assert item["evaluation"]["state"] == "not_run"
    assert item["disposition"]["stale"] is True


def test_a_parked_item_keeps_its_test_out_of_completed(workspace_with_data):
    ws = workspace_with_data
    _document, test_id, item_id = _vouching_with_result(ws)
    assert doc_tests.load_test(ws, test_id)["status"] == "completed"

    parked = doc_tests.update_item(
        ws, test_id, item_id,
        {"state": "needs_review", "disposition_note": "Second reviewer to confirm."},
    )
    assert parked["status"] == "review_required"
    assert parked["items"][0]["state"] == "manual_review"
    # A parked item is not a settled one, so it cannot carry a conclusion.
    assert doc_tests.result_rollup(parked)["conclusion_eligible"] is False


def test_one_call_dispositions_a_selection_spanning_several_tests(workspace_with_data):
    ws = workspace_with_data
    _first_doc, first_test, first_item = _vouching_with_result(ws)
    _second_doc, second_test, second_item = _vouching_with_result(ws)

    result = doc_tests.update_dispositions(ws, [
        {"test_id": first_test, "item_id": first_item, "state": "confirmed"},
        {
            "test_id": second_test, "item_id": second_item,
            "state": "exception", "disposition_note": "Amount disagrees.",
        },
    ])

    assert result["items"] == 2
    first = doc_tests.load_test(ws, first_test)["items"][0]
    second = doc_tests.load_test(ws, second_test)["items"][0]
    assert first["disposition"]["state"] == "confirmed"
    assert second["disposition"]["state"] == "exception"
    assert second["disposition"]["note"] == "Amount disagrees."

    with pytest.raises(workspaces.WorkspaceError):
        doc_tests.update_dispositions(
            ws, [{"test_id": first_test, "item_id": first_item, "state": "agent_checked"}]
        )


def test_signing_off_keeps_a_refined_completed_status(workspace_with_data):
    ws = workspace_with_data
    _document, test_id, item_id = _vouching_with_result(ws, [
        _MATCHING_CHECK,
        {"field": "amount", "expected": 999, "method": "numeric_tolerance"},
    ])
    # RCM execution refines a completed test; a later sign-off must not flatten it.
    doc_tests.update_test(ws, test_id, {"status": "completed_with_exception"})

    agreed = doc_tests.update_item(ws, test_id, item_id, {"state": "exception"})
    assert agreed["status"] == "completed_with_exception"

    # Once the items stop bearing that reading out, it is dropped rather than kept.
    overturned = doc_tests.update_item(
        ws, test_id, item_id,
        {"state": "confirmed", "disposition_note": "Reference mismatch only."},
    )
    assert overturned["status"] == "completed"


def test_a_disposition_note_requires_an_accompanying_call(workspace_with_data):
    ws = workspace_with_data
    _document, test_id, item_id = _vouching_with_result(ws)
    with pytest.raises(workspaces.WorkspaceError):
        doc_tests.update_item(ws, test_id, item_id, {"disposition_note": "orphan"})


def _two_item_qa(ws):
    """A test the runner leaves half-settled: one exception, one it cannot read."""

    first = documents.add_document(ws, "valuation.txt", b"Valuation support attached.")
    test = doc_tests.create_test(ws, {
        "kind": "qa", "title": "Valuation support",
        "items": [
            {"label": "Inspect valuation support", "document_ids": [first["id"]],
             "question": "Is the valuation supported?"},
            {"label": "Assess documented requirements", "document_ids": [],
             "question": "Are the requirements documented?"},
        ],
    })
    for item in test["items"]:
        doc_tests.run_item(ws, test["id"], item["id"])
    return test["id"], [item["id"] for item in test["items"]]


def test_an_auditor_may_conclude_over_unresolved_items_and_it_is_disclosed(
    workspace_with_data,
):
    ws = workspace_with_data
    test_id, item_ids = _two_item_qa(ws)
    doc_tests.update_item(ws, test_id, item_ids[0], {"state": "confirmed"})
    before = doc_tests.load_test(ws, test_id)
    assert doc_tests.result_rollup(before)["conclusion_eligible"] is False

    updated = doc_tests.update_test(ws, test_id, {
        "control_conclusion": "effective",
        "conclusion": "The control operated for the population tested.",
    })

    # The judgment is the auditor's to make; the file records what was open.
    assert updated["control_conclusion"] == "effective"
    assert updated["control_conclusion_source"] == "auditor"
    limitations = updated["scope_limitations"]
    assert "Concluded with 1 of 2 items unresolved" in limitations
    assert "Assess documented requirements" in limitations
    assert updated["conclusion_override"]["unresolved_items"][0]["id"] == item_ids[1]

    # And it survives the rollup, or overriding would achieve nothing downstream.
    rollup = doc_tests.result_rollup(updated)
    assert rollup["control_conclusion"] == "effective"
    assert rollup["conclusion_disclosed"] is True
    assert rollup["conclusion_eligible"] is False


def test_resolving_the_open_items_clears_the_disclosure(workspace_with_data):
    ws = workspace_with_data
    test_id, item_ids = _two_item_qa(ws)
    doc_tests.update_item(ws, test_id, item_ids[0], {"state": "confirmed"})
    doc_tests.update_test(ws, test_id, {"control_conclusion": "effective"})

    doc_tests.update_item(ws, test_id, item_ids[1], {"state": "exception"})
    resolved = doc_tests.update_test(ws, test_id, {"control_conclusion": "effective"})

    assert resolved["scope_limitations"] == ""
    assert "conclusion_override" not in resolved
    assert doc_tests.result_rollup(resolved)["conclusion_eligible"] is True


def test_the_disclosure_never_overwrites_the_auditors_own_scope_text(
    workspace_with_data,
):
    ws = workspace_with_data
    test_id, item_ids = _two_item_qa(ws)
    doc_tests.update_item(ws, test_id, item_ids[0], {"state": "confirmed"})

    updated = doc_tests.update_test(ws, test_id, {
        "scope_limitations": "Vendor master was out of scope this period.",
        "control_conclusion": "effective",
    })
    assert updated["scope_limitations"].startswith("Vendor master was out of scope")
    assert "Concluded with 1 of 2 items unresolved" in updated["scope_limitations"]

    # Saving again rewrites the generated block rather than stacking a copy.
    again = doc_tests.update_test(ws, test_id, {"control_conclusion": "ineffective"})
    assert again["scope_limitations"].count("Concluded with") == 1
    assert again["scope_limitations"].startswith("Vendor master was out of scope")


def test_a_conclusion_still_needs_a_run_and_a_population_it_can_speak_for(
    workspace_with_data,
):
    ws = workspace_with_data
    test_id, _item_ids = _two_item_qa(ws)
    unrun = doc_tests.create_test(ws, {
        "kind": "qa", "title": "Never run",
        "items": [{"label": "Ask", "document_ids": [], "question": "Well?"}],
    })
    with pytest.raises(workspaces.WorkspaceError, match="Run every item"):
        doc_tests.update_test(ws, unrun["id"], {"control_conclusion": "effective"})

    # Clearing a conclusion is always allowed, whatever the item state.
    assert doc_tests.update_test(
        ws, test_id, {"control_conclusion": "no_conclusion"}
    )["control_conclusion"] == "no_conclusion"


def test_auditor_can_record_a_conclusion_on_a_document_test(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "policy.txt", b"Approval is required.\nEvidence must be retained.")
    review = doc_tests.build_review(ws, {"title": "Policy review", "document_id": document["id"]})
    doc_tests.run_item(ws, review["id"], review["items"][0]["id"])
    # A review item the runner could not settle has to be resolved before the
    # test can carry a control conclusion over it.
    doc_tests.update_item(
        ws, review["id"], review["items"][0]["id"], {"state": "confirmed"}
    )
    loaded = doc_tests.load_test(ws, review["id"])
    before_status = loaded["status"]
    assert loaded["conclusion"] == ""
    assert loaded["control_conclusion"] == "no_conclusion"

    updated = doc_tests.update_test(
        ws, review["id"],
        {"conclusion": "Reviewed the policy document; approval requirement is documented.",
         "control_conclusion": "effective"},
    )
    assert updated["control_conclusion"] == "effective"
    assert updated["conclusion"].startswith("Reviewed the policy")
    # The runner's own read of the item is untouched by the auditor's conclusion.
    assert updated["status"] == before_status

    with pytest.raises(workspaces.WorkspaceError):
        doc_tests.update_test(ws, review["id"], {"control_conclusion": "bogus"})


def test_llm_conclusion_is_rolled_up_without_overwriting_an_auditor(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "policy.txt", b"Approval is required.")
    test = doc_tests.build_qa(ws, {
        "title": "Policy Q&A", "document_ids": [document["id"]],
        "questions": ["Is approval required?"],
    })
    item_id = test["items"][0]["id"]

    doc_tests.commit_llm_assessment(
        ws, test["id"], item_id, document["id"],
        {
            "answer": "Yes, approval is required.",
            "conclusion": "The cited policy supports the approval requirement.",
            "control_conclusion": "effective",
            "outcome": "accepted",
            "citations": [],
        },
    )
    saved = doc_tests.load_test(ws, test["id"])
    assert saved["conclusion"] == "The cited policy supports the approval requirement."
    assert saved["conclusion_source"] == "agent"
    assert saved["control_conclusion"] == "effective"
    assert saved["control_conclusion_source"] == "agent"

    doc_tests.update_test(
        ws,
        test["id"],
        {
            "conclusion": "Auditor conclusion.",
            "control_conclusion": "partially_effective",
        },
    )
    doc_tests.commit_llm_assessment(
        ws, test["id"], item_id, document["id"],
        {
            "answer": "Yes, approval is required.",
            "conclusion": "A rerun would otherwise replace this text.",
            "control_conclusion": "effective",
            "outcome": "accepted",
            "citations": [],
        },
    )
    saved = doc_tests.load_test(ws, test["id"])
    assert saved["conclusion"] == "Auditor conclusion."
    assert saved["conclusion_source"] == "auditor"
    assert saved["control_conclusion"] == "partially_effective"
    assert saved["control_conclusion_source"] == "auditor"


def test_doc_test_workflow_persists_each_item_and_completes(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "evidence.txt", b"Invoice: 1001\nAmount: 150.00")
    test = doc_tests.create_test(ws, {
        "kind": "vouching", "title": "Runner test",
        "items": [{"label": "Invoice 1001", "document_ids": [document["id"]], "checks": [
            {"field": "invoice", "expected": 1001, "method": "normalized"},
            {"field": "amount", "expected": 150, "method": "numeric_tolerance"},
        ]}],
    })
    run = runner.start_command_run(
        ws,
        "auto",
        {
            "text": "Run document test.",
            "requested_outcomes": ["doc_tests.executed"],
            "target_refs": [f"doctest:{test['id']}"],
        },
        context={"test_id": test["id"]},
    )
    finished = wait_run(ws, run["id"])
    assert finished["status"] == "completed", finished.get("error")
    assert finished["engine"] == "workflow"
    assert finished["workflow"]["definition"] == "doc_tests_workflow_v2"
    assert finished["doc_tests"]["rollup"]["matched"] == 2
    saved = doc_tests.load_test(ws, test["id"])
    assert saved["items"][0]["state"] == "confirmed"
    assert saved["status"] == "completed"


def test_qa_runner_uses_document_context_without_workspace_setting(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    document = documents.add_document(ws, "policy.txt", b"Purchases require manager approval.")
    test = doc_tests.build_qa(ws, {
        "title": "Policy question", "document_ids": [document["id"]],
        "questions": ["Is manager approval required?"],
    })
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: {
        "content": json.dumps({
            "answer": "Yes, manager approval is required.",
            "citations": [{"page": 1, "excerpt": "Purchases require manager approval."}],
        })
    })
    checked = doc_tests.run_item(ws, test["id"], test["items"][0]["id"], run_id="RUN-QA")
    assert checked["state"] == "confirmed"
    assert checked["citations"][0]["source_sha1"] == document["sha1"]
    activity = documents.activities(ws)["items"][0]
    assert activity["purpose"] == "document_qa"
    assert activity["run_id"] == "RUN-QA"


def test_working_paper_draft_traceability_and_safe_html(workspace_with_data):
    ws = workspace_with_data
    rcm, procedure = _procedure(ws)
    test = doc_tests.create_test(ws, {
        "kind": "vouching", "title": "Invoice support", "rcm_refs": [rcm["id"]],
        "procedure_refs": [procedure["id"]],
        "items": [{"label": "Item one", "state": "exception", "checks": [
            {"field": "amount", "expected": 100, "found": 90, "verdict": "mismatch"}
        ]}],
    })
    assert f"doctest:{test['id']}" in ws.work_program[0]["test_refs"]
    drafted = working_papers.draft_results(ws, procedure["id"])
    assert "1 linked document test" in drafted["result_summary"]
    assert "exceptions or unmatched" in drafted["conclusion"]
    paper = working_papers.render(ws, procedure["id"])
    assert test["id"] in paper["markdown"]
    assert rcm["id"] in paper["markdown"]
    assert "<article" in paper["html"]

    ws.update_procedure(procedure["id"], {"objective": "Check <script>alert(1)</script>"})
    safe = working_papers.render(ws, procedure["id"])["html"]
    assert "<script>" not in safe
    assert "&lt;script&gt;" in safe


def test_doctest_reference_validation_and_api(workspace_with_data):
    ws = workspace_with_data
    rcm, procedure = _procedure(ws)
    with pytest.raises(workspaces.WorkspaceError, match="does not exist"):
        ws.update_procedure(procedure["id"], {"test_refs": ["doctest:missing"]})

    client = TestClient(create_app())
    created = client.post(f"/api/workspaces/{ws.id}/doc-tests", json={
        "kind": "qa", "title": "API Q&A", "procedure_refs": [procedure["id"]],
        "items": [{"label": "Question", "question": "What happened?"}],
    })
    assert created.status_code == 200
    test_id = created.json()["id"]
    listed = client.get(f"/api/workspaces/{ws.id}/doc-tests").json()["items"]
    assert any(item["id"] == test_id for item in listed)
    compare = client.post(f"/api/workspaces/{ws.id}/matching/compare", json={
        "expected": 100, "found": 101, "method": "numeric_tolerance", "tolerance": 1,
    })
    assert compare.status_code == 200 and compare.json()["result"] == "match"
    paper = client.get(f"/api/workspaces/{ws.id}/procedures/{procedure['id']}/working-paper")
    assert paper.status_code == 200 and test_id in paper.json()["markdown"]
