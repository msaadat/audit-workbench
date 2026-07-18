import time

from fastapi.testclient import TestClient

from app.agent import runner, store
from app.main import create_app
from app import document_analysis, documents, llm, methodology, templates_store, workspaces

from conftest import FakeAgentLLM, wait_run


PLANNING_RESPONSES = {
    "agent:document_context": {
        "context": {
            "scope": "Procurement policy governance",
            "background_notes": "Procurement approvals are governed by the disclosed policy.",
        }
    },
    "agent:apm": {
        "apm_markdown": (
            "# Audit Planning Memorandum\n\n"
            "## Engagement\nScope covers accounts payable.\n\n"
            "## Introduction and background\nPlanning background.\n\n"
            "## Process flow and understanding\nAccounts payable processing.\n\n"
            "## Prior audit findings\nNo information available.\n\n"
            "## Key risks and planned response\nTest duplicate payments."
        )
    },
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


def configure_planning_llm(monkeypatch, overrides=None):
    responses = dict(PLANNING_RESPONSES)
    responses.update(overrides or {})
    fake = FakeAgentLLM(responses)
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    return fake


def start_planning(ws, mode="auto", context=None):
    return runner.start_command_run(
        ws,
        mode,
        {
            "source": "goal_template",
            "text": "Update planning using current engagement evidence.",
            "goal_template": "planning",
        },
        context=context or {},
    )


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


def test_unchanged_apm_save_preserves_agent_ownership():
    ws = workspaces.create_workspace("Planning provenance")
    ws.update_planning(
        {
            "apm_markdown": "# Agent draft",
            "created_by": "agent",
            "agent_run_id": "run-1",
        },
        agent=True,
    )

    unchanged = ws.update_planning(
        {
            "context": {"entity": "Example Co"},
            "apm_markdown": "# Agent draft",
        }
    )
    assert unchanged["created_by"] == "agent"
    assert unchanged["agent_run_id"] == "run-1"

    edited = ws.update_planning({"apm_markdown": "# Auditor revision"})
    assert edited["created_by"] == "user"


def test_planning_run_without_tables_and_user_safe_rerun(monkeypatch):
    fake = configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("No-table planning")
    first = start_planning(ws)
    completed = wait_run(ws, first["id"])
    ws = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert completed["schema_version"] == 2
    assert completed["kind"] == "audit"
    assert completed["actions"] == []
    assert ws.planning["apm_markdown"].startswith("# Audit Planning Memorandum")
    assert len(ws.rcm) == 1
    assert len(ws.work_program) == 1
    assert ws.work_program[0]["rcm_refs"] == [ws.rcm[0]["id"]]
    assert {call["tag"] for call in fake.calls} >= {"agent:apm", "agent:rcm", "agent:work_program"}

    row_id = ws.rcm[0]["id"]
    ws.update_rcm(row_id, {"control": "Auditor-confirmed manual review"})
    second = start_planning(ws)
    wait_run(ws, second["id"])
    ws = workspaces.load_workspace(ws.id)
    assert len(ws.rcm) == 1
    assert ws.rcm[0]["control"] == "Auditor-confirmed manual review"


def test_planning_llm_failure_is_not_reported_as_completed(monkeypatch):
    ws = workspaces.create_workspace("Planning provider failure")

    def disconnect(_user):
        raise llm.LLMError("LLM request failed: Remote end closed connection without response")

    configure_planning_llm(monkeypatch, {"agent:apm": disconnect})
    completed = wait_run(ws, start_planning(ws)["id"])

    tasks = {
        task["id"]: task
        for stage in completed["plan"]["stages"]
        for task in stage["tasks"]
    }
    assert completed["status"] == "failed"
    assert completed["command"]["status"] == "failed"
    assert completed["error"] == "LLM request failed: Remote end closed connection without response"
    assert not completed["summary_markdown"]
    assert tasks["planning:context"]["status"] == "completed"
    assert tasks["planning:apm"]["status"] == "failed"
    assert "planning:rcm" not in tasks


def test_auto_planning_selects_relevant_documents(monkeypatch):
    ws = workspaces.create_workspace("Auto document selection")
    policy_text = b"Procurement Policy: purchases require documented approval before commitment."
    policy = documents.add_document(ws, "Procurement Policy.txt", policy_text, category="policy")

    def select(user):
        assert policy["id"] in user
        return {"selected": [{"id": policy["id"], "reason": "Governs procurement approvals."}]}

    fake = configure_planning_llm(monkeypatch, {"agent:document_selection": select})
    # No document_ids in the run context: the agent must select them itself.
    started = start_planning(ws)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["context"]["scope"] == "Procurement policy governance"
    context_call = next(call for call in fake.calls if call["tag"] == "agent:document_context")
    assert policy_text.decode() in context_call["messages"][-1]["content"]
    activity = documents.activities(reloaded, limit=250)["items"]
    assert any(policy["id"] in item.get("document_ids", []) for item in activity)
    assert completed["planning_basis"]["document_content_included"] is True
    context_details = [
        event["data"]["task"]["detail"]
        for event in store.read_events(ws, started["id"])
        if event["type"] == "task_update"
        and event["data"]["task"]["id"] == "planning:context"
        and event["data"]["task"].get("detail")
    ]
    assert "Selecting relevant documents for audit planning…" in context_details
    assert "Analyzing document 1 of 1: Procurement Policy.txt" in context_details
    assert "Synthesizing planning context from 1 document…" in context_details


def test_planning_recovers_labelled_context_when_synthesis_returns_empty(monkeypatch):
    ws = workspaces.create_workspace("Planning context fallback")
    policy = documents.add_document(
        ws, "Procurement Policy.txt", b"Procurement approvals require documented authorization.",
        category="policy",
    )
    extracted = documents.extract_document(ws, policy["id"])
    document_analysis.persist_analysis(
        ws,
        policy,
        extracted,
        {
            "summary_markdown": (
                "- **Objective:** Review procurement approvals\n"
                "- **Scope:** Procurement authorization controls"
            ),
            "audit_notes_markdown": "",
            "citations": [],
        },
        provider="fake",
        model="fake",
    )
    fake = configure_planning_llm(monkeypatch, {"agent:document_context": {}})

    completed = wait_run(
        ws,
        start_planning(ws, context={"document_ids": [policy["id"]]})["id"],
    )
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["context"]["objective"] == "Review procurement approvals"
    assert reloaded.planning["context"]["scope"] == "Procurement authorization controls"
    assert completed["planning_basis"]["planning"]["context"]["objective"] == "Review procurement approvals"
    assert [call["tag"] for call in fake.calls].count("agent:document_context") == 2
    assert any("recovered labelled facts" in warning for warning in completed["warnings"])


def test_permission_planning_uses_three_editable_approval_gates(monkeypatch):
    configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Permission planning")
    started = start_planning(ws, "permission")
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


def test_apm_unfilled_placeholders_become_not_available_notes(monkeypatch):
    configure_planning_llm(monkeypatch, {
        "agent:apm": {
            "apm_markdown": (
                "# Audit Planning Memorandum\n\n"
                "## Engagement\n- Entity: {{entity}}\n- Period: {{period}}\n\n"
                "## Introduction and background\n{{introduction}}\n\n"
                "## Process flow and understanding\n{{process_flow}}\n\n"
                "## Prior audit findings\n{{prior_audit_findings}}\n\n"
                "## Key risks and planned response\n{{key_risks}}"
            )
        },
    })
    ws = workspaces.create_workspace("Placeholder planning")
    started = start_planning(ws)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    apm = reloaded.planning["apm_markdown"]
    assert "{{" not in apm and "}}" not in apm
    assert "_[entity - context not available]_" in apm
    assert "_[prior audit findings - context not available]_" in apm


def test_permission_planning_confirms_agent_selected_documents(monkeypatch):
    ws = workspaces.create_workspace("Permission document selection")
    policy = documents.add_document(
        ws, "Procurement Policy.txt",
        b"Procurement Policy: purchases require documented approval.", category="policy",
    )
    configure_planning_llm(monkeypatch, {
        "agent:document_selection": {
            "selected": [{"id": policy["id"], "reason": "Governs procurement approvals."}]
        },
    })
    started = start_planning(ws, "permission")
    kinds, seen = [], []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = store.load_run(ws, started["id"])
        if run["status"] == "awaiting_approval":
            pending = next(item for item in run["approvals"] if item["status"] == "pending")
            if pending["id"] not in seen:
                seen.append(pending["id"])
                kinds.append(pending["kind"])
                runner.resolve_approval(
                    ws, run["id"], pending["id"],
                    [{"item_id": item["id"], "action": "approve"} for item in pending["items"]],
                )
        elif run["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)
    assert completed["status"] == "completed"
    # The auditor confirms the agent's document choice before any content is used.
    assert kinds[0] == "documents"
    assert any(policy["id"] in item.get("document_ids", []) for item in documents.activities(reloaded, limit=250)["items"])


def test_planning_routes():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "Planning API"}).json()
    base = f"/api/workspaces/{created['id']}"

    response = client.patch(f"{base}/planning", json={"context": {"scope": "Head office"}})
    assert response.status_code == 200
    assert response.json()["context"]["scope"] == "Head office"
    assert "status" not in response.json()
    assert client.patch(f"{base}/planning", json={"status": "final"}).status_code == 400
    row = client.post(f"{base}/rcm", json={"process": "Cash", "risk": "Cash is misstated"}).json()
    assert client.patch(f"{base}/rcm/{row['id']}", json={"risk_rating": "high"}).json()["risk_rating"] == "high"
    procedure = client.post(f"{base}/procedures", json={"objective": "Test cash", "rcm_refs": [row["id"]]}).json()
    payload = client.get(f"{base}/planning").json()
    assert payload["rcm"][0]["id"] == row["id"]
    assert payload["procedures"][0]["id"] == procedure["id"]
    template = client.put(f"{base}/templates/apm", json={"markdown": "# Custom"})
    assert template.json()["source"] == "workspace"


def test_planning_cites_methodology_pack(monkeypatch):
    configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Methodology planning")
    methodology.save_pack(
        ws,
        "Firm AP Guide",
        "# Duplicate payments\nAudit procedures should address duplicate-payment risk and preventive controls.",
    )
    started = start_planning(ws)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)
    assert completed["status"] == "completed"
    refs = reloaded.work_program[0]["methodology_refs"]
    assert refs[0]["pack_name"] == "Firm AP Guide"
    assert refs[0]["section"] == "Duplicate payments"
    activity = documents.activities(reloaded)["items"]
    assert any(item.get("artifact_ref") == "planning:apm" for item in activity)
    apm_call = next(item for item in activity if item.get("stage") == "agent:apm")
    assert apm_call["template_versions"][0]["name"] == "apm"
    assert apm_call["knowledge_packs"][0]["sha1"] == refs[0]["sha1"]


def test_planning_update_includes_selected_imported_documents(monkeypatch):
    fake = configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Policy planning update")
    policy_text = b"Procurement Policy: purchases require documented approval before commitment."
    policy = documents.add_document(ws, "Procurement Policy.txt", policy_text, category="policy")

    started = start_planning(ws, context={"document_ids": [policy["id"]]})
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["context"]["scope"] == "Procurement policy governance"
    context_call = next(call for call in fake.calls if call["tag"] == "agent:document_context")
    assert policy_text.decode() in context_call["messages"][-1]["content"]
    activity = documents.activities(reloaded, limit=250)["items"]
    assert any(policy["id"] in item.get("document_ids", []) for item in activity)
    assert completed["planning_basis"]["document_content_included"] is True
