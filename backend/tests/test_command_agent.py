import json
import time

import polars as pl
import pytest

from app import assistant, findings, llm, privacy, workspaces
from app.agent import actions, artifact_index, ledger, runner, store
from conftest import FakeAgentLLM, wait_run


def configured(monkeypatch, response):
    fake = FakeAgentLLM({"agent:command_interpreter": response})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"})
    return fake


def test_schema_v2_round_trip_and_legacy_projection(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "run duplicate testing"})
    ledger.append_actions(run, [{"id": "a1", "type": "run_analytics", "args": {"table": "transactions", "test": "duplicates", "params": {"columns": ["invoice_no"]}}}])
    store.save_run(workspace_with_data, run)
    loaded = store.load_run(workspace_with_data, run["id"])
    assert loaded["schema_version"] == 2
    assert loaded["actions"][0]["definition_version"] == 1
    assert loaded["plan"]["stages"][0]["tasks"][0]["id"] == "a1"


def test_registry_and_graph_reject_invalid_contracts(workspace_with_data):
    assert {item.type for item in actions.REGISTRY.all()} >= {"edit_finding", "delete_finding", "generate_report"}
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test"})
    with pytest.raises(workspaces.WorkspaceError, match="Unknown agent action"):
        ledger.append_actions(run, [{"type": "write_json", "args": {}}])
    with pytest.raises(workspaces.WorkspaceError, match="unknown action"):
        ledger.append_actions(run, [{"id": "a", "type": "run_report_quality", "args": {}, "depends_on": ["missing"]}])


def test_artifact_resolution_exact_ambiguous_and_no_match(workspace_with_data):
    one = workspace_with_data.add_rcm({"risk": "Duplicate invoices may be paid"})
    workspace_with_data.add_rcm({"risk": "Duplicate suppliers may be created"})
    index = artifact_index.build(workspace_with_data)
    exact = artifact_index.resolve(index, "rcm", None, one["id"])
    assert exact["resolved_id"] == one["id"] and exact["confidence"] == 1.0
    ambiguous = artifact_index.resolve(index, "rcm", "duplicate")
    assert ambiguous["resolved_id"] is None and len(ambiguous["candidates"]) == 2
    assert artifact_index.resolve(index, "rcm", "unrelated treasury hedge")["candidates"] == []

    findings.add(workspace_with_data, {"title": "Same title"})
    findings.add(workspace_with_data, {"title": "Same title"})
    duplicate_titles = artifact_index.resolve(artifact_index.build(workspace_with_data), "finding", "same title")
    assert duplicate_titles["resolved_id"] is None and len(duplicate_titles["candidates"]) == 2


def test_privacy_projector_masks_identifier_aliases_and_embedded_patterns():
    frame = pl.DataFrame({"acct_no": [123456789, 987654321], "branch": ["North", "South"], "amount": [10.0, 20.0]})
    projected = privacy.project_frame(frame, allow_rows=True)
    assert "acct_no" not in projected["numeric_summary"]
    assert projected["rows"][0][0] == "[sensitive identifier withheld]"
    assert projected["rows"][0][1:] == ["North", 10.0]
    assert "person@example.com" not in privacy.scrub_text("Contact person@example.com")


def test_narrow_command_executes_only_requested_action(monkeypatch, workspace_with_data):
    response = {
        "objective": "Add one finding",
        "constraints": [], "completion_criteria": ["Finding exists"],
        "actions": [{"id": "create-one", "type": "create_finding", "args": {"title": "Duplicate invoices need review", "severity": "medium"}}],
        "needs_planning_wave": False,
    }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": "add a finding for duplicate invoices"})
    completed = wait_run(workspace_with_data, started["id"])
    reloaded = workspaces.load_workspace(workspace_with_data.id)
    assert completed["status"] == "completed"
    assert [item["type"] for item in completed["actions"]] == ["create_finding"]
    assert reloaded.findings[0]["title"] == "Duplicate invoices need review"


def test_destructive_command_requires_dedicated_confirmation(monkeypatch, workspace_with_data):
    finding = __import__("app.findings", fromlist=["add"]).add(workspace_with_data, {"title": "Duplicate invoices"})
    response = {
        "objective": "Remove finding", "constraints": [], "completion_criteria": [],
        "actions": [{"id": "delete-one", "type": "delete_finding", "target": {"kind": "finding", "resolved_id": finding["id"]}, "args": {}}],
    }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": f"remove finding {finding['id']}"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = store.load_run(workspace_with_data, started["id"])
        pending = next((item for item in state.get("interactions") or [] if item["status"] == "pending"), None)
        if pending:
            break
        time.sleep(0.02)
    assert pending["type"] == "confirmation"
    assert any(item["id"] == finding["id"] for item in workspace_with_data.findings)
    runner.resolve_interaction(workspace_with_data, started["id"], pending["id"], {"decision": "approve"})
    completed = wait_run(workspace_with_data, started["id"])
    assert completed["status"] == "completed"
    assert not workspaces.load_workspace(workspace_with_data.id).findings


def test_general_chat_queues_follow_up_without_altering_graph(monkeypatch, workspace_with_data):
    def response(user):
        command = json.loads(user)["command"]["text"]
        return {
            "objective": command, "constraints": [], "completion_criteria": [],
            "actions": [] if "review the APM" in command else [
                {"id": "rewrite", "type": "generate_report", "args": {"use_model": False}}
            ],
        }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": "prepare report"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = store.load_run(workspace_with_data, started["id"])
        if any(item["status"] == "pending" for item in state.get("interactions") or []):
            break
        time.sleep(0.02)
    result = runner.steer(workspace_with_data, started["id"], "then review the APM")
    assert result["handled"] == "queued_command"
    time.sleep(0.05)
    state = store.load_run(workspace_with_data, started["id"])
    assert [item["type"] for item in state["actions"]] == ["generate_report"]
    assert state["pending_commands"][0]["text"] == "then review the APM"
    runner.cancel_run(workspace_with_data, started["id"])
    wait_run(workspace_with_data, started["id"])
    deadline = time.monotonic() + 5
    follow_up = None
    while time.monotonic() < deadline:
        follow_up = next((item for item in store.list_runs(workspace_with_data) if item["parent_run_id"] == started["id"]), None)
        if follow_up:
            break
        time.sleep(0.02)
    assert follow_up is not None
    assert wait_run(workspace_with_data, follow_up["id"])["status"] == "completed"


def test_reversible_edit_can_be_undone_while_postcondition_is_current(monkeypatch, workspace_with_data):
    finding = findings.add(workspace_with_data, {"title": "Duplicate invoice risk", "severity": "medium"})
    response = {
        "objective": "Change then undo", "constraints": [], "completion_criteria": [],
        "actions": [
            {"id": "edit", "type": "edit_finding", "target": {"kind": "finding", "resolved_id": finding["id"]}, "args": {"changes": {"severity": "high"}}},
            {"id": "undo", "type": "undo_action", "args": {"action_id": "edit"}, "depends_on": ["edit"]},
        ],
    }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": f"temporarily change and undo {finding['id']}"})
    completed = wait_run(workspace_with_data, started["id"])
    assert completed["status"] == "completed"
    assert workspaces.load_workspace(workspace_with_data.id).findings[0]["severity"] == "medium"


def test_post_success_planner_failure_keeps_committed_work(monkeypatch, workspace_with_data):
    """Regression: an invalid planner proposal after an action succeeded must
    become a warning, not an illegal succeeded → failed transition that fails
    the run and strands the remaining ready actions."""
    interpreter = {
        "objective": "Create two findings", "constraints": [], "completion_criteria": [],
        "actions": [
            {"id": "first", "type": "create_finding", "args": {"title": "First finding"}, "planning_significant": True},
            {"id": "second", "type": "create_finding", "args": {"title": "Second finding"}, "depends_on": ["first"]},
        ],
    }
    planner = {"actions": [{"id": "bad", "type": "run_analytics", "args": {}}]}
    fake = FakeAgentLLM({"agent:command_interpreter": interpreter, "agent:command_planner": planner})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"})
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": "create two findings"})
    completed = wait_run(workspace_with_data, started["id"])
    by_id = {item["id"]: item for item in completed["actions"]}
    assert completed["status"] == "completed"
    assert by_id["first"]["status"] == "succeeded" and by_id["first"]["error"] is None
    assert by_id["second"]["status"] == "succeeded"
    assert any("invalid planning proposal" in warning for warning in completed["warnings"])
    assert len(workspaces.load_workspace(workspace_with_data.id).findings) == 2


def test_append_actions_rolls_back_a_rejected_batch(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test"})
    ledger.append_actions(run, [{"id": "a1", "type": "run_report_quality", "args": {}}])
    revision = run["graph_revision"]
    with pytest.raises(workspaces.WorkspaceError, match="unknown action"):
        ledger.append_actions(run, [
            {"id": "b1", "type": "create_finding", "args": {"title": "Valid"}},
            {"id": "b2", "type": "create_finding", "args": {"title": "Broken"}, "depends_on": ["missing"]},
        ])
    assert [item["id"] for item in run["actions"]] == ["a1"]
    assert run["graph_revision"] == revision
    assert [task["id"] for task in run["plan"]["stages"][0]["tasks"]] == ["a1"]


def test_create_reconciler_detects_after_apply_before_receipt(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "create finding"})
    action = ledger.append_actions(run, [{"id": "create", "type": "create_finding", "args": {"title": "Crash-safe finding"}}])[0]
    action["postcondition"] = actions.expected_postcondition(action)
    action["status"] = "running"
    definition = actions.validate_action(action)
    definition.executor(workspace_with_data, action, run)  # crash boundary: domain write happened, receipt did not
    assert definition.reconciler(workspace_with_data, action) == "already_applied"
    assert len(workspaces.load_workspace(workspace_with_data.id).findings) == 1
