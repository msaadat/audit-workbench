import json

import pytest
from fastapi.testclient import TestClient

from app import doc_tests, findings, llm, report, workspaces
from app.agent import store
from app.main import create_app


def linked_workspace(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Test duplicate-payment controls.", "scope": "Supplied transactions."}})
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
    execution = doc_tests.create_test(ws, {
        "kind": "review", "title": "Duplicate-payment result review",
        "objective": "Determine whether duplicate payments occurred",
        "rcm_id": rcm["id"],
        "items": [{"label": "Review result", "state": "confirmed", "auditor_disposition": "accepted"}],
    })
    execution = doc_tests.update_test(ws, execution["id"], {
        "status": "completed",
        "scope_limitations": "Only the supplied period was tested.",
    })
    return ws, rcm, procedure, execution, analysis, anchor


def complete_finding_payload(rcm, procedure, execution, anchor):
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
        "test_refs": [execution["id"]],
        "execution_refs": [f"doctest:{execution['id']}"],
        "evidence_refs": [anchor],
        "severity_rationale": "A duplicate payment could cause a material financial loss.",
        "auditor_confirmed": True,
    }


def test_finding_crud_validates_typed_sources_and_rolls_up(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
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
    ws, _rcm, _procedure, _execution, analysis, _anchor = linked_workspace(workspace_with_data)
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


def test_finding_derives_typed_evidence_from_execution_reference(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, _anchor = linked_workspace(
        workspace_with_data
    )
    payload = complete_finding_payload(
        rcm, procedure, execution,
        {"source_kind": "doctest", "source_id": execution["id"], "source_sha1": execution["sha1"]},
    )
    payload.pop("evidence_refs")

    item = findings.add(ws, payload)

    assert item["evidence_refs"][0]["source_kind"] == "doctest"
    assert item["evidence_refs"][0]["source_id"] == execution["id"]
    assert item["evidence_refs"][0]["source_sha1"] == findings.artifact(
        ws, "doctest", execution["id"]
    )["sha1"]
    assert findings.support_issues(ws, item) == []


def test_report_context_excludes_rows_and_document_excerpts(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    context = report.build_context(ws)
    serialized = json.dumps(context)
    assert context["statistics"]["findings"] == 1
    assert "invoice_no" not in serialized
    assert "excerpt" not in serialized
    assert "Duplicate invoices were processed" in serialized


def test_report_context_falls_back_to_labelled_apm_fields(workspace_with_data):
    workspace_with_data.update_planning({
        "apm_markdown": (
            "# APM\n\n## Engagement\n\n"
            "- Entity: Global Bank\n"
            "- Period: January–December 2025\n"
            "- Objective & Scope: Review procurement approvals.\n"
        )
    })

    planning = report.build_context(workspace_with_data)["planning"]

    assert planning == {
        "objective": "Review procurement approvals.",
        "entity": "Global Bank",
        "period": "January–December 2025",
        "scope": "Review procurement approvals.",
        "materiality": None,
    }


def test_deterministic_report_edit_aware_regeneration_and_reconcile(monkeypatch, workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
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


def test_deterministic_preliminary_report_discloses_incomplete_workflow_coverage():
    ws = workspaces.create_workspace("Incomplete report coverage")
    ws.update_planning({
        "context": {"objective": "Assess procurement", "scope": "Procure to pay"},
        "apm_markdown": "# Audit Planning Memorandum\n\nProcurement scope.",
    })
    ws.add_rcm({
        "process": "Purchasing", "risk": "Purchases bypass approval",
        "control": "Approval workflow",
    })
    row = ws.add_rcm({
        "process": "Payments", "risk": "Duplicate invoices are paid",
        "control": "Duplicate invoice check",
    })
    doc_tests.create_draft(ws, {
        "title": "Test duplicate invoices",
        "objective": "Identify duplicate invoices",
        "criteria": "Each invoice is paid once.",
        "steps": [{"label": "Inspect duplicate invoice identifiers.", "instruction": "Inspect duplicate invoice identifiers."}],
        "rcm_id": row["id"],
    })
    workflow_state = {
        "stages": [
            {"capability": "planning.rcm_ready", "units": [{"status": "failed"}]},
            {"capability": "tests.specified", "units": [{"status": "failed"}]},
        ]
    }

    generated = report.generate(ws, use_model=False, workflow=workflow_state)

    assert generated["generation_warnings"] == [
        "Incomplete planning coverage: 1 planning workflow unit(s) failed and "
        "1 required planning item(s) are missing.",
        "Incomplete execution-definition coverage: 1 execution-definition workflow "
        "unit(s) failed and 1 required execution definition(s) are missing.",
    ]
    assert "# Preliminary Internal Audit Working Draft" in generated["markdown"]
    assert "## Scope limitations" in generated["markdown"]
    assert "Incomplete planning coverage: 1 planning workflow unit(s) failed" in generated["markdown"]
    assert "Incomplete execution-definition coverage: 1 execution-definition workflow unit(s) failed" in generated["markdown"]


def test_model_report_and_section_chunking_record_generation(monkeypatch, workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
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
    ws, rcm, procedure, _execution, _analysis, anchor = linked_workspace(workspace_with_data)
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
    assert {"finding_draft", "unsupported_finding", "unresolved_exception", "report_arithmetic", "broken_report_citation", "missing_limitations", "preliminary_label_missing"} <= codes
    assert checked["ok"] is False
    assert ws.findings[0]["id"] == item["id"]
    assert doc_tests.exists(ws, test["id"])


def test_quality_checks_detect_rcm_risk_distribution_drift(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Audit procurement", "scope": "Procurement"}})
    ws.add_rcm({"risk": "Approval bypass", "risk_rating": "high"})
    ws.add_rcm({"risk": "Duplicate payment", "risk_rating": "medium"})
    ws.add_rcm({"risk": "Vendor concentration", "risk_rating": "medium"})

    checked = report.quality_checks(
        ws,
        "# Preliminary report\n\nRisk distribution: high 2, medium 1, low 0.",
    )

    risk_issues = [
        issue for issue in checked["issues"]
        if issue["code"] == "report_risk_arithmetic"
    ]
    assert len(risk_issues) == 2
    assert any("high-risk count" in issue["message"] for issue in risk_issues)
    assert any("medium-risk count" in issue["message"] for issue in risk_issues)


def test_bare_markdown_finding_reference_is_a_citation_and_model_output_is_normalized(
    monkeypatch, workspace_with_data
):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    checked = report.quality_checks(ws, f"# Report\n\n### Finding [{item['id']}]: Duplicate invoices")
    assert "finding_missing_from_report" not in {issue["code"] for issue in checked["issues"]}

    monkeypatch.setattr(
        llm, "chat",
        lambda *args, **kwargs: {"content": f"# Report\n\n### Finding [{item['id']}]: Duplicate invoices"},
    )
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "fake", "model": "fake"})
    generated = report.generate(ws)
    assert f"[Finding {item['id']}](?tab=findings&finding={item['id']})" in generated["markdown"]


@pytest.mark.parametrize(
    "reference",
    [
        "[F-{id}](#f-{id_lower})",
        "[F-{id}](finding:F-{id})",
        "[Finding F-{id}](?tab=findings\\&finding=F-{id})",
    ],
)
def test_linked_markdown_finding_references_satisfy_report_quality(
    workspace_with_data, reference
):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    markdown = reference.format(id=item["id"], id_lower=item["id"].lower())

    checked = report.quality_checks(ws, f"# Report\n\n{markdown}")

    assert "finding_missing_from_report" not in {issue["code"] for issue in checked["issues"]}


def test_report_html_escapes_content_and_only_allows_safe_links():
    value = report.markdown_to_html(
        "# <script>alert(1)</script>\n\n[Bad](javascript:alert(1)) [Finding](?tab=findings&finding=F-1)"
    )
    assert "<script>" not in value
    assert "&lt;script&gt;" in value
    assert "javascript:" not in value
    assert 'href="?tab=findings&amp;finding=F-1"' in value


def test_editorial_review_degrades_safely_on_wrong_issue_shape(
    monkeypatch, workspace_with_data
):
    ws = workspace_with_data
    ws.report = {"markdown": "# Preliminary internal audit report"}
    ws.save()
    monkeypatch.setattr(
        llm, "chat", lambda *args, **kwargs: {
            "content": json.dumps({"issues": ["unclear wording"]})
        },
    )
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "fake", "model": "fake"},
    )

    reviewed = report.editorial_review(ws)

    assert reviewed["editorial"][0]["code"] == "editorial_unavailable"


def test_finding_and_report_routes(monkeypatch, workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})
    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}"
    created = client.post(f"{base}/findings", json=complete_finding_payload(rcm, procedure, execution, anchor))
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
