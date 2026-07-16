import json

import pytest
from fastapi.testclient import TestClient

from app import doc_tests, findings, llm, report, workspaces
from app.agent import store
from app.main import create_app


def linked_workspace(workspace_with_data):
    ws = workspace_with_data
    rcm = ws.add_rcm({"process": "Payables", "risk": "Duplicate payments", "risk_rating": "high"})
    procedure = ws.add_procedure(
        {
            "objective": "Determine whether duplicate payments occurred",
            "criteria": "Invoices are paid once.",
            "rcm_refs": [rcm["id"]],
            "scope_limitations": "Only the supplied period was tested.",
        }
    )
    analysis = ws.add_analysis(
        {
            "kind": "analytics", "table": "transactions", "title": "Duplicate invoices",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
        }
    )
    anchor = findings.anchor_from_ref(ws, f"analysis:{analysis['id']}")
    return ws, rcm, procedure, analysis, anchor


def complete_finding_payload(rcm, procedure, anchor):
    return {
        "title": "Duplicate invoices were processed",
        "severity": "high",
        "condition": "Invoice 1006 appears twice.",
        "criteria": "Each invoice should be paid once.",
        "cause": "The duplicate check did not identify the repeated invoice.",
        "effect": "Duplicate payment risk is elevated.",
        "recommendation": "Configure and monitor a duplicate-payment control.",
        "management_response": "Management will update the control.",
        "rcm_refs": [rcm["id"]],
        "procedure_refs": [procedure["id"]],
        "evidence_refs": [anchor],
    }


def test_finding_crud_validates_typed_sources_and_rolls_up(workspace_with_data):
    ws, rcm, procedure, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, anchor))
    assert item["source"] == "manual"
    assert item["evidence_refs"][0]["source_sha1"]
    assert findings.rollups(ws)["by_rcm"][rcm["id"]][0]["id"] == item["id"]

    updated = findings.update(ws, item["id"], {"severity": "critical"})
    assert updated["severity"] == "critical"
    assert "status" not in updated
    assert workspaces.load_workspace(ws.id).find_semantic("findings", item["semantic_id"])["id"] == item["id"]

    broken = {**anchor, "source_id": "missing"}
    with pytest.raises(workspaces.WorkspaceError, match="does not exist"):
        findings.update(ws, item["id"], {"evidence_refs": [broken]})

    findings.remove(ws, item["id"])
    assert ws.findings == []


def test_agent_finding_promotion_is_explicit_typed_and_idempotent(workspace_with_data):
    ws, _rcm, _procedure, analysis, _anchor = linked_workspace(workspace_with_data)
    run = store.new_run(ws, "auto")
    run["findings"] = [
        {
            "id": "finding-1", "severity": "medium", "statement": "A duplicate invoice was observed.",
            "basis": "observed", "evidence_refs": [f"analysis:{analysis['id']}"],
        }
    ]
    store.save_run(ws, run)

    promoted = findings.promote(ws, run["id"], "finding-1")
    again = findings.promote(ws, run["id"], "finding-1")
    assert promoted["id"] == again["id"]
    assert promoted["source"] == "promoted"
    assert "status" not in promoted
    assert promoted["condition"] == "A duplicate invoice was observed."
    assert promoted["evidence_refs"][0]["source_kind"] == "analysis"


def test_report_context_excludes_rows_and_document_excerpts(workspace_with_data):
    ws, rcm, procedure, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, anchor))
    context = report.build_context(ws)
    serialized = json.dumps(context)
    assert context["statistics"]["findings"] == 1
    assert "invoice_no" not in serialized
    assert "excerpt" not in serialized
    assert "Duplicate invoices were processed" in serialized


def test_deterministic_report_edit_aware_regeneration_and_reconcile(monkeypatch, workspace_with_data):
    ws, rcm, procedure, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, anchor))
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})

    first = report.generate(ws)
    assert first["requires_reconcile"] is False
    assert f"finding={item['id']}" in first["markdown"]
    report.update(ws, {"markdown": first["markdown"] + "\nAuditor edit.\n"})
    second = report.generate(ws)
    assert second["requires_reconcile"] is True
    assert second["markdown"].endswith("Auditor edit.\n")
    assert second["candidate_markdown"] != second["markdown"]

    kept = report.reconcile(ws, "keep")
    assert kept["edited"] is True
    replaced = report.reconcile(ws, "replace")
    assert replaced["edited"] is False
    assert replaced["markdown"] == replaced["generated_markdown"]


def test_model_report_and_section_chunking_record_generation(monkeypatch, workspace_with_data):
    ws, rcm, procedure, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, anchor))
    calls = []

    def fake_chat(messages, tools=None, temperature=0.0, profile="assistant"):
        calls.append(messages[0]["content"].splitlines()[0])
        return {"content": "## Generated section\n\nEvidence-linked model draft."}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "fake", "model": "fake"})
    monkeypatch.setattr(report, "MODEL_CONTEXT_LIMIT", 1)
    result = report.generate(ws)
    assert result["used_model"] is True
    assert result["chunked"] is True
    assert len(calls) >= 5
    assert "Evidence-linked model draft" in result["markdown"]


def test_quality_checks_are_advisory_and_detect_traceability_arithmetic_and_exceptions(workspace_with_data):
    ws, rcm, procedure, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, {"title": "Incomplete finding", "rcm_refs": [rcm["id"]]})
    test = doc_tests.create_test(
        ws,
        {
            "kind": "review", "title": "Minutes review",
            "items": [{"id": "ITEM-1", "label": "Minutes", "state": "exception", "auditor_disposition": "exception"}],
        },
    )
    checked = report.quality_checks(
        ws,
        f"# Report\n\nThere are 9 findings and 3 exceptions. [Broken](?tab=findings&finding=F-MISSING)",
    )
    codes = {issue["code"] for issue in checked["issues"]}
    assert {"finding_incomplete", "finding_no_procedure", "unsupported_finding", "unresolved_exception", "report_arithmetic", "broken_report_citation", "missing_limitations"} <= codes
    assert checked["ok"] is False
    assert ws.findings[0]["id"] == item["id"]
    assert doc_tests.exists(ws, test["id"])


def test_report_html_escapes_content_and_only_allows_safe_links():
    value = report.markdown_to_html(
        "# <script>alert(1)</script>\n\n[Bad](javascript:alert(1)) [Finding](?tab=findings&finding=F-1)"
    )
    assert "<script>" not in value
    assert "&lt;script&gt;" in value
    assert "javascript:" not in value
    assert 'href="?tab=findings&amp;finding=F-1"' in value


def test_finding_and_report_routes(monkeypatch, workspace_with_data):
    ws, rcm, procedure, _analysis, anchor = linked_workspace(workspace_with_data)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})
    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}"
    created = client.post(f"{base}/findings", json=complete_finding_payload(rcm, procedure, anchor))
    assert created.status_code == 200
    finding_id = created.json()["id"]
    assert client.get(f"{base}/findings").json()["items"][0]["id"] == finding_id
    generated = client.post(f"{base}/report/generate", json={"use_model": False})
    assert generated.status_code == 200
    assert client.post(f"{base}/report/quality", json={}).status_code == 200
    assert client.post(f"{base}/findings", json={"title": "Old payload", "status": "draft"}).status_code == 400
    assert client.patch(f"{base}/report", json={"status": "final"}).status_code == 400
    assert client.patch(f"{base}/findings/{finding_id}", json={"status": "final"}).status_code == 400
    assert client.delete(f"{base}/findings/{finding_id}").json() == {"ok": True}


def test_removed_artifact_statuses_are_discarded_when_loading(workspace_with_data):
    ws = workspace_with_data
    ws.planning["status"] = "final"
    ws.findings.append({"id": "F-OLD", "title": "Legacy finding", "status": "draft"})
    ws.report = {"status": "final", "markdown": "# Existing report"}
    ws.save()

    loaded = workspaces.load_workspace(ws.id)
    assert "status" not in loaded.planning
    assert "status" not in loaded.findings[0]
    assert "status" not in loaded.report
    loaded.save()

    persisted = json.loads(loaded.definition_path.read_text(encoding="utf-8"))
    assert "status" not in persisted["planning"]
    assert "status" not in persisted["findings"][0]
    assert "status" not in persisted["report"]
