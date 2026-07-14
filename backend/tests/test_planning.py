import time

from fastapi.testclient import TestClient

from app.agent import runner, store
from app.main import create_app
from app import documents, llm, methodology, templates_store, workspaces

from conftest import FakeAgentLLM, wait_run


PLANNING_RESPONSES = {
    "agent:document_context": {
        "context": {
            "scope": "Procurement policy governance",
            "background_notes": "Procurement approvals are governed by the disclosed policy.",
        }
    },
    "agent:apm": {"apm_markdown": "# APM\n\nScope covers accounts payable."},
    "agent:rcm": {
        "rows": [
            {
                "process": "Accounts payable",
                "risk": "Duplicate payments are processed",
                "risk_rating": "high",
                "assertion": "Occurrence",
                "control": "Duplicate invoice validation",
                "control_type": "Automated preventive",
                "test_procedure": "Test invoice number and amount duplicates.",
                "test_refs": [],
            }
        ]
    },
    "agent:work_program": {
        "procedures": [
            {
                "stable_slug": "duplicate-payments",
                "rcm_refs": [
                    "rcm:accounts-payable:duplicate-payments-are-processed"
                ],
                "objective": "Determine whether duplicate payments occurred",
                "criteria": "Each valid invoice is paid once.",
                "steps": ["Identify repeated invoice numbers and amounts.", "Inspect exceptions."],
                "method": "Data analysis and inspection",
                "expected_evidence": "Exception listing and inspected invoices",
                "test_refs": [],
            }
        ]
    },
}


def configure_planning_llm(monkeypatch, interview=None):
    responses = dict(PLANNING_RESPONSES)
    responses["agent:interview"] = interview or {
        "action": "conclude",
        "question": "",
        "captured": {},
    }
    fake = FakeAgentLLM(responses)
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    return fake


def test_templates_default_override_and_reset():
    ws = workspaces.create_workspace("Planning templates")
    default = templates_store.get_template(ws, "apm")
    assert default["source"] == "default"
    assert "{{entity}}" in default["markdown"]

    override = templates_store.put_template(ws, "apm", "# Firm APM\n{{scope}}")
    assert override == {"name": "apm", "markdown": "# Firm APM\n{{scope}}", "source": "workspace"}
    reset = templates_store.put_template(ws, "apm", None, reset=True)
    assert reset["source"] == "default"


def test_planning_crud_and_user_touch():
    ws = workspaces.create_workspace("Planning CRUD")
    planning = ws.update_planning({"context": {"entity": "Example Co"}, "apm_markdown": "# Draft"})
    assert planning["context"]["entity"] == "Example Co"

    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue is incomplete", "agent_run_id": "run-1"})
    assert row["created_by"] == "agent"
    row = ws.update_rcm(row["id"], {"control": "Monthly reconciliation"})
    assert row["created_by"] == "user"
    procedure = ws.add_procedure({"objective": "Test revenue completeness", "rcm_refs": [row["id"]], "agent_run_id": "run-1"})
    assert ws.find_semantic("procedures", procedure["semantic_id"]) == procedure
    ws.remove_rcm(row["id"])
    assert ws.work_program[0]["rcm_refs"] == []


def test_planning_run_without_tables_and_user_safe_rerun(monkeypatch):
    fake = configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("No-table planning")
    first = runner.start_run(ws, "auto", {"skip_interview": True}, kind="planning")
    completed = wait_run(ws, first["id"])
    ws = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert ws.planning["apm_markdown"].startswith("# APM")
    assert len(ws.rcm) == 1
    assert len(ws.work_program) == 1
    assert ws.work_program[0]["rcm_refs"] == [ws.rcm[0]["id"]]
    assert {call["tag"] for call in fake.calls} >= {"agent:apm", "agent:rcm", "agent:work_program"}

    row_id = ws.rcm[0]["id"]
    ws.update_rcm(row_id, {"control": "Auditor-confirmed manual review"})
    second = runner.start_run(ws, "auto", {"skip_interview": True}, kind="planning")
    wait_run(ws, second["id"])
    ws = workspaces.load_workspace(ws.id)
    assert len(ws.rcm) == 1
    assert ws.rcm[0]["control"] == "Auditor-confirmed manual review"


def test_planning_interview_waits_for_inbox(monkeypatch):
    def interview(user):
        if "The engagement covers FY2026" in user:
            return {"action": "conclude", "question": "", "captured": {"period": "FY2026"}}
        return {"action": "ask", "question": "What period is under review?", "captured": {}}

    configure_planning_llm(monkeypatch, interview)
    ws = workspaces.create_workspace("Interview planning")
    started = runner.start_run(ws, "auto", {}, kind="planning")
    waiting = wait_run(ws, started["id"], statuses=("awaiting_input",))
    assert waiting["interview"]["pending_question"] == "What period is under review?"

    response = runner.steer(ws, started["id"], "The engagement covers FY2026")
    assert response["handled"] == "steering"
    completed = wait_run(ws, started["id"])
    ws = workspaces.load_workspace(ws.id)
    assert completed["status"] == "completed"
    assert completed["interview"]["turns"] == 1
    assert ws.planning["context"]["period"] == "FY2026"


def test_permission_planning_uses_three_editable_approval_gates(monkeypatch):
    configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Permission planning")
    started = runner.start_run(ws, "permission", {"skip_interview": True}, kind="planning")
    seen = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = store.load_run(ws, started["id"])
        if run["status"] == "awaiting_approval":
            pending = next(item for item in run["approvals"] if item["status"] == "pending")
            if pending["id"] not in seen:
                seen.append(pending["id"])
                runner.resolve_approval(
                    ws,
                    run["id"],
                    pending["id"],
                    [{"item_id": item["id"], "action": "approve"} for item in pending["items"]],
                )
        elif run["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    completed = wait_run(ws, started["id"])
    assert completed["status"] == "completed"
    assert [approval["kind"] for approval in completed["approvals"]] == ["apm", "rcm", "work_program"]


def test_planning_routes():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "Planning API"}).json()
    base = f"/api/workspaces/{created['id']}"

    response = client.patch(f"{base}/planning", json={"context": {"scope": "Head office"}})
    assert response.status_code == 200
    assert response.json()["context"]["scope"] == "Head office"
    row = client.post(f"{base}/rcm", json={"process": "Cash", "risk": "Cash is misstated"}).json()
    assert client.patch(f"{base}/rcm/{row['id']}", json={"risk_rating": "high"}).json()["risk_rating"] == "high"
    procedure = client.post(f"{base}/procedures", json={"objective": "Test cash", "rcm_refs": [row["id"]]}).json()
    payload = client.get(f"{base}/planning").json()
    assert payload["rcm"][0]["id"] == row["id"]
    assert payload["procedures"][0]["id"] == procedure["id"]
    template = client.put(f"{base}/templates/apm", json={"markdown": "# Custom"})
    assert template.json()["source"] == "workspace"


def test_planning_cites_opted_in_methodology_pack(monkeypatch):
    configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Methodology planning")
    methodology.save_pack(
        ws,
        "Firm AP Guide",
        "# Duplicate payments\nAudit procedures should address duplicate-payment risk and preventive controls.",
    )
    ws.settings["doc_llm_optin"] = True
    ws.save()
    started = runner.start_run(
        ws,
        "auto",
        {"skip_interview": True},
        kind="planning",
    )
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)
    assert completed["status"] == "completed"
    refs = reloaded.work_program[0]["methodology_refs"]
    assert refs[0]["pack_name"] == "Firm AP Guide"
    assert refs[0]["section"] == "Duplicate payments"
    logged = documents.disclosures(reloaded)["items"]
    assert logged[0]["purpose"] == "planning_methodology"
    activity = documents.activities(reloaded)["items"]
    assert any(item.get("artifact_ref") == "planning:apm" for item in activity)
    apm_call = next(item for item in activity if item.get("stage") == "agent:apm")
    assert apm_call["template_versions"][0]["name"] == "apm"
    assert apm_call["knowledge_packs"][0]["sha1"] == refs[0]["sha1"]


def test_planning_update_discloses_selected_imported_documents(monkeypatch):
    fake = configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Policy planning update", doc_llm_optin=True)
    policy_text = b"Procurement Policy: purchases require documented approval before commitment."
    policy = documents.add_document(ws, "Procurement Policy.txt", policy_text, category="policy")

    started = runner.start_run(
        ws,
        "auto",
        {"skip_interview": True, "document_ids": [policy["id"]]},
        kind="planning",
    )
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["context"]["scope"] == "Procurement policy governance"
    context_call = next(call for call in fake.calls if call["tag"] == "agent:document_context")
    assert policy_text.decode() in context_call["messages"][-1]["content"]
    disclosures = documents.disclosures(reloaded)["items"]
    assert disclosures[0]["document_id"] == policy["id"]
    assert disclosures[0]["purpose"] == "planning_update"
    assert completed["planning_basis"]["document_content_disclosed"] is True
