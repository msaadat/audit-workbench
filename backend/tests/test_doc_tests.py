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
                {"field": "amount", "expected": 150.0, "method": "numeric_tolerance", "tolerance": {"absolute": 0}},
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

    reset = doc_tests.update_item(ws, review["id"], item_id, {"state": "pending"})
    assert reset["items"][0]["state"] == "pending"
    assert reset["status"] == "in_progress"

    with pytest.raises(workspaces.WorkspaceError):
        doc_tests.update_item(ws, review["id"], item_id, {"state": "manual_review"})


def test_auditor_can_record_a_conclusion_on_a_document_test(workspace_with_data):
    ws = workspace_with_data
    document = documents.add_document(ws, "policy.txt", b"Approval is required.\nEvidence must be retained.")
    review = doc_tests.build_review(ws, {"title": "Policy review", "document_id": document["id"]})
    doc_tests.run_item(ws, review["id"], review["items"][0]["id"])
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
    assert finished["workflow"]["definition"] == "doc_tests_workflow_v1"
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
