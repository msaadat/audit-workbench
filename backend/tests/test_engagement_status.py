from fastapi.testclient import TestClient

from app import analysis_payloads, data_tests, doc_tests, documents, findings, rcm_execution, workspaces
from app.engagement_progress import engagement_status_payload
from app.main import create_app


def test_planned_rcm_without_tests_is_not_complete_fieldwork(workspace_with_data):
    ws = workspace_with_data
    ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})

    fieldwork = next(
        phase for phase in engagement_status_payload(ws)["phases"]
        if phase["id"] == "fieldwork"
    )

    assert fieldwork["complete"] is False
    assert fieldwork["summary"] == "No tests have been planned yet."
    assert fieldwork["state"] != "not_started"


def test_the_document_tests_section_answers_only_for_document_tests(
    workspace_with_data,
):
    ws = workspace_with_data
    # An RCM row with no test at all puts the broad fieldwork phase into
    # attention. That is real work, but it is not document-test work, and the
    # rail badges this section against the Document tests tab.
    ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    document = documents.add_document(ws, "policy.txt", b"Approval is required.")
    test = doc_tests.build_review(
        ws, {"title": "Policy review", "document_id": document["id"]}
    )
    doc_tests.run_item(ws, test["id"], test["items"][0]["id"])

    payload = engagement_status_payload(ws)
    fieldwork = next(p for p in payload["phases"] if p["id"] == "fieldwork")
    section = payload["sections"]["doc-tests"]

    assert fieldwork["state"] == "attention"
    # The runner could not settle the review item, so this section is in
    # attention on its own merits — and says which item, not which RCM row.
    assert section["state"] == "attention"
    assert any("unresolved" in issue for issue in section["issues"])

    # Resolving the document-test work clears this badge even though the
    # untested RCM row keeps the fieldwork phase in attention.
    doc_tests.update_item(
        ws, test["id"], test["items"][0]["id"], {"state": "confirmed"}
    )
    doc_tests.update_test(ws, test["id"], {"control_conclusion": "effective"})

    payload = engagement_status_payload(ws)
    assert next(
        p for p in payload["phases"] if p["id"] == "fieldwork"
    )["state"] == "attention"
    assert payload["sections"]["doc-tests"]["state"] == "complete"
    assert payload["sections"]["doc-tests"]["issues"] == []


def test_the_document_tests_section_is_not_started_without_any(workspace_with_data):
    assert engagement_status_payload(workspace_with_data)["sections"]["doc-tests"][
        "state"
    ] == "not_started"


def test_the_data_tests_section_answers_only_for_data_tests(workspace_with_data):
    ws = workspace_with_data
    # An untested RCM row keeps the broad fieldwork phase in attention without
    # saying anything about data-test work, which is what this badge reports.
    ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    row = ws.add_rcm({"process": "Revenue", "risk": "Duplicates may be recorded"})
    test = data_tests.create(ws, {
        "title": "Duplicate invoice check", "objective": "Identify duplicates.",
        "engine": "analytics", "table_refs": ["transactions"],
        "rcm_id": row["id"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    })

    # Defined but never run.
    assert engagement_status_payload(
        workspaces.load_workspace(ws.id)
    )["sections"]["data-tests"]["state"] == "in_progress"

    ws = workspaces.load_workspace(ws.id)
    data_tests.run(ws, test["id"])
    rcm_execution.rollup(ws)

    section = engagement_status_payload(ws)["sections"]["data-tests"]
    assert section["state"] == "attention"
    assert any("no control conclusion" in issue for issue in section["issues"])

    data_tests.update(ws, test["id"], {"control_conclusion": "ineffective"})

    payload = engagement_status_payload(ws)
    assert next(
        p for p in payload["phases"] if p["id"] == "fieldwork"
    )["state"] == "attention"
    assert payload["sections"]["data-tests"]["state"] == "complete"
    assert payload["sections"]["data-tests"]["issues"] == []


def test_the_data_tests_section_is_not_started_without_any(workspace_with_data):
    assert engagement_status_payload(workspace_with_data)["sections"]["data-tests"][
        "state"
    ] == "not_started"


def test_apm_status_rechecks_current_content(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({
        "context": {"objective": "Test controls.", "scope": "Transactions."},
        "apm_markdown": "# Audit planning memorandum\n\nInitial draft.",
    })
    ws.update_planning({"apm_markdown": ""})

    planning = next(
        phase for phase in engagement_status_payload(ws)["phases"]
        if phase["id"] == "planning"
    )
    apm = next(sub for sub in planning["sub"] if sub["id"] == "apm")

    assert apm["complete"] is False
    assert apm["state"] == "in_progress"
    assert "APM content is empty" in planning["issues"]


def test_engagement_status_endpoint_is_lightweight(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    client = TestClient(create_app())

    def fail_compute(*_args, **_kwargs):
        raise AssertionError("the status endpoint must not recompute any analysis")

    monkeypatch.setattr("app.analysis_payloads.compute_payload", fail_compute)
    response = client.get(f"/api/workspaces/{ws.id}/engagement/status")

    assert response.status_code == 200
    assert [phase["id"] for phase in response.json()["phases"]] == [
        "planning", "fieldwork", "report",
    ]


def test_derived_phases_can_reach_complete(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({
        "apm_markdown": "# Planning",
        "context": {"objective": "Test revenue controls.", "scope": "Recorded transactions."},
    })
    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    test = data_tests.create(ws, {
        "title": "Required transaction identifiers", "objective": "Identify missing IDs.",
        "steps": [{"label": "Scan transaction amounts.", "instruction": "Scan transaction amounts."}],
        "engine": "analytics", "table_refs": ["transactions"],
        "rcm_id": row["id"],
        "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
    })
    data_tests.run(ws, test["id"])
    rcm_execution.rollup(ws)
    data_tests.update(ws, test["id"], {
        "conclusion": "No missing transaction identifiers were identified.",
        "control_conclusion": "effective",
    })
    ws.report = {"markdown": "# Audit report\n\nThere are 0 findings."}
    ws.save()

    phases = {phase["id"]: phase for phase in engagement_status_payload(ws)["phases"]}
    assert all(phase["complete"] for phase in phases.values())
    assert phases["fieldwork"]["state"] == "complete"
    assert phases["report"]["counts"]["quality_errors"] == 0


def test_status_counts_carry_the_denominators_the_console_rail_reads(workspace_with_data):
    ws = workspace_with_data
    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    concluded = data_tests.create(ws, {
        "title": "Required transaction identifiers", "objective": "Identify missing IDs.",
        "engine": "analytics", "table_refs": ["transactions"], "rcm_id": row["id"],
        "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
    })
    unrun = data_tests.create(ws, {
        "title": "Duplicate invoices", "objective": "Identify duplicates.",
        "engine": "analytics", "table_refs": ["transactions"], "rcm_id": row["id"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    })
    data_tests.run(ws, concluded["id"])
    rcm_execution.rollup(ws)
    data_tests.update(ws, concluded["id"], {"control_conclusion": "effective"})

    payload = engagement_status_payload(ws)
    fieldwork = next(phase for phase in payload["phases"] if phase["id"] == "fieldwork")
    section = payload["sections"]["data-tests"]

    # One of two linked tests has both run and concluded, which is the fraction
    # the rail leads with and the same one the RCM bar computes.
    assert fieldwork["counts"]["tests_linked"] == 2
    assert fieldwork["counts"]["tests_concluded"] == 1
    assert section["counts"] == {"total": 2, "concluded": 1}
    # The rail runs the outstanding test itself, so it is handed the id.
    assert section["unrun_test_ids"] == [unrun["id"]]
    assert section["stale_test_ids"] == []


def test_status_counts_findings_whose_follow_up_no_gate_covers(workspace_with_data):
    ws = workspace_with_data
    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    findings.add(ws, {
        "title": "Duplicate payments were made",
        "narrative": "Two invoices were paid twice.",
        "severity": "high", "rcm_refs": [row["id"]],
        "cause_pending": True, "management_response": "",
    })
    settled = findings.add(ws, {
        "title": "Approval limits were exceeded",
        "narrative": "Three payments exceeded the approver's limit.",
        "severity": "medium", "rcm_refs": [row["id"]],
    })
    findings.update(ws, settled["id"], {
        "cause_pending": False,
        "management_response": "Management accepts the point and will retrain approvers.",
    })

    report_phase = next(
        phase for phase in engagement_status_payload(ws)["phases"] if phase["id"] == "report"
    )
    # Neither an open cause nor a missing response is a quality error, so the
    # count exists precisely to be said beside a tick the gates still allow.
    assert report_phase["counts"]["findings"] == 2
    assert report_phase["counts"]["findings_awaiting_followup"] == 1


def test_engagement_status_surfaces_attention_and_report_quality(workspace_with_data):
    ws = workspace_with_data
    test = doc_tests.create_test(ws, {
        "kind": "attribute", "title": "Approval review",
        "items": [{"label": "Sample 1", "state": "manual_review"}],
    })
    findings.add(ws, {"title": "Unsupported transaction"})
    ws.report = {"markdown": "# Audit report\n\n99 findings."}
    ws.save()

    phases = {
        phase["id"]: phase for phase in engagement_status_payload(ws)["phases"]
    }

    assert phases["fieldwork"]["state"] == "attention"
    assert any("manual review" in issue for issue in phases["fieldwork"]["issues"])
    assert phases["report"]["state"] == "attention"
    assert phases["report"]["counts"]["quality_errors"] > 0
    assert "draft_findings" not in phases["report"]["counts"]


def test_terminal_document_test_result_statuses_are_not_marked_incomplete(
    workspace_with_data,
):
    ws = workspace_with_data
    for status in ("completed_no_exception", "completed_with_exception"):
        doc_tests.create_test(ws, {
            "kind": "qa",
            "title": f"{status} test",
            "status": status,
            "items": [{"label": "Settled item", "state": "confirmed"}],
        })

    phases = {phase["id"]: phase for phase in engagement_status_payload(ws)["phases"]}

    assert not any(
        "document test(s) are incomplete" in issue
        for issue in phases["fieldwork"]["issues"]
    )


def test_fieldwork_gate_uses_control_conclusion_not_free_text(
    workspace_with_data,
):
    ws = workspace_with_data
    ws.update_planning({
        "context": {"objective": "Test controls.", "scope": "Transactions."},
    })
    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    test = data_tests.create(ws, {
        "title": "Required transaction identifiers", "objective": "Identify missing IDs.",
        "engine": "analytics", "table_refs": ["transactions"],
        "rcm_id": row["id"],
        "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
    })
    data_tests.run(ws, test["id"])
    rcm_execution.rollup(ws)
    data_tests.update(ws, test["id"], {"control_conclusion": "effective"})

    phase = next(
        item for item in engagement_status_payload(ws)["phases"]
        if item["id"] == "fieldwork"
    )
    assert not any("open execution or outcomes" in issue for issue in phase["issues"])


