import json

import pytest

from app import assistant_chats, llm, workspaces
from app.agent import routing, store


def configured(monkeypatch):
    monkeypatch.setattr(llm, "status", lambda: {"configured": True, "provider": "fake", "model": "fake"})
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "fake", "model": "fake"})


def test_chat_crud_title_and_safe_delete(workspace_with_data):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    assert chat["title"] == "New chat"
    assert assistant_chats.list_chats(ws)["chats"][0]["id"] == chat["id"]

    renamed = assistant_chats.update_chat(ws, chat["id"], {"title": "Duplicate payment review"})
    assert renamed["title"] == "Duplicate payment review"
    assert renamed["title_source"] == "user"
    with pytest.raises(Exception, match="Invalid chat id"):
        assistant_chats.get_chat(ws, "../workspace.json")

    assistant_chats.delete_chat(ws, chat["id"])
    assert assistant_chats.list_chats(ws)["chats"] == []
    assert ws.definition_path.exists()


def test_q_and_a_is_durable_idempotent_and_uses_bounded_history(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    calls = []

    def fake_ask(workspace, question, document_ids, *, prior_turns, chat_id=None, commander=None):
        calls.append((question, prior_turns, chat_id))
        return {
            "answer": f"Answer to {question}", "steps": [{"tool": "list_tables", "args": {}, "ok": True}],
            "artifacts": [], "citations": [], "document_context": None,
        }

    monkeypatch.setattr(assistant_chats.assistant, "ask", fake_ask)
    chat = assistant_chats.create_chat(ws)
    first = assistant_chats.send_message(ws, chat["id"], {
        "content": "What tables are available?", "intent": "ask", "mode": "auto",
        "request_id": "request-first", "source": "composer",
    })
    duplicate = assistant_chats.send_message(ws, chat["id"], {
        "content": "This must not be used", "intent": "ask", "mode": "auto",
        "request_id": "request-first", "source": "composer",
    })
    assert duplicate["outcome"] == first["outcome"]
    assert len(calls) == 1

    assistant_chats.send_message(ws, chat["id"], {
        "content": "How many of those are data tables?", "intent": "ask", "mode": "auto",
        "request_id": "request-second", "source": "composer",
    })
    assert calls[1][1] == [
        {"role": "user", "content": "What tables are available?"},
        {"role": "assistant", "content": "Answer to What tables are available?"},
    ]
    assert calls[1][2] == chat["id"]
    restored = assistant_chats.get_chat(ws, chat["id"])
    assert [item["state"] for item in restored["messages"]] == ["complete"] * 4
    assert restored["title"] == "What tables are available?"


def test_auto_question_reaches_the_coordinator_with_chat_context_and_command_tools(
    workspace_with_data, monkeypatch,
):
    ws = workspace_with_data
    configured(monkeypatch)
    captured = {}

    def fake_ask(
        workspace, question, document_ids, *, prior_turns, chat_id=None, commander=None,
    ):
        captured.update(
            question=question,
            document_ids=document_ids,
            prior_turns=prior_turns,
            chat_id=chat_id,
            # An auto message is coordinated: the loop is lent the capability
            # to start work, and answering is the model's own choice.
            commander_lent=commander is not None,
        )
        return {
            "answer": "The latest run is complete; inspect audit progress next.",
            "steps": [{"tool": "get_audit_progress", "args": {}, "ok": True}],
            "artifacts": [],
            "citations": [],
            "document_context": None,
        }

    monkeypatch.setattr(assistant_chats.assistant, "ask", fake_ask)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "What's the next task to be done?",
        "intent": "auto",
        "mode": "auto",
        "request_id": "request-next-task",
        "source": "composer",
    })

    user = result["chat"]["messages"][0]
    assert user["resolved_intent"] == "ask"
    assert result["outcome"]["kind"] == "answer"
    assert captured == {
        "question": "What's the next task to be done?",
        "document_ids": [],
        "prior_turns": [],
        "chat_id": chat["id"],
        "commander_lent": True,
    }
    assert store.list_runs(ws) == []


def test_artifact_sidecar_edit_rerun_and_revision_conflict(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    monkeypatch.setattr(assistant_chats.assistant, "ask", lambda *args, **kwargs: {
        "answer": "Computed locally", "steps": [], "citations": [],
        "document_context": None, "artifacts": [{
            "id": "temporary", "tool": "run_python", "title": "Totals", "table": None,
            "kind": "python", "spec": {"code": "result = transactions.head(1)"},
            "viz": {"type": "table"}, "frame": {"columns": [], "dtypes": [], "rows": []},
            "total_rows": 0, "error": None, "code": "result = transactions.head(1)", "stdout": None,
        }],
    })
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Calculate a local total?", "intent": "ask", "mode": "auto",
        "request_id": "request-artifact", "source": "composer",
    })
    restored = result["chat"]
    artifact = next(iter(restored["artifacts"].values()))
    updated = assistant_chats.update_artifact(
        ws, chat["id"], artifact["id"], {"code": "result = transactions.head(2)"}, artifact["revision"],
    )
    with pytest.raises(assistant_chats.RevisionConflict):
        assistant_chats.update_artifact(ws, chat["id"], artifact["id"], {"title": "Stale"}, artifact["revision"])

    rerun = assistant_chats.rerun_artifact(ws, chat["id"], artifact["id"], updated["revision"])
    assert rerun["total_rows"] == 2
    assert rerun["revision"] == updated["revision"] + 1


def test_explicit_action_starts_linked_schema_v2_run(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Start data analysis work for this engagement.", "intent": "act", "mode": "permission",
        "request_id": "request-action", "source": "shortcut", "goal_template": "data_analysis",
    })
    assert result["outcome"]["kind"] == "run_started"
    assert launched["chat_id"] == chat["id"]
    assert launched["source_message_id"].startswith("msg_")
    linked = store.load_run(ws, result["outcome"]["run_id"])
    assert linked["chat_id"] == chat["id"]
    assert any(item["type"] == "run" for item in result["chat"]["transcript"])


def test_next_step_action_forwards_declared_outcomes(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    assistant_chats.send_message(ws, chat["id"], {
        "content": "Generate each executable test the RCM rows need.",
        "intent": "act", "mode": "auto", "request_id": "request-next-step",
        "source": "shortcut", "requested_outcomes": ["tests.specified"],
    })

    assert launched["requested_outcomes"] == ["tests.specified"]


def test_planning_action_passes_structured_run_context(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command=command, context=context)
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Update planning from selected policies", "intent": "act", "mode": "auto",
        "request_id": "request-planning-context", "source": "tab_button",
        "goal_template": "planning", "run_context": {"document_ids": ["doc-1", "doc-2"]},
    })

    assert result["outcome"]["kind"] == "run_started"
    assert launched["command"]["goal_template"] == "planning"
    assert launched["context"] == {"document_ids": ["doc-1", "doc-2"]}


def test_finding_draft_tab_button_keeps_selected_observation_scope(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command=command, context=context)
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    assistant_chats.send_message(ws, chat["id"], {
        "content": "Draft a finding from observation OBS-1.",
        "intent": "act", "mode": "auto", "request_id": "request-finding-scope",
        "source": "tab_button", "goal_template": "finding_draft",
        "run_context": {"observation_id": "OBS-1"},
    })

    assert launched["command"]["goal_template"] == "finding_draft"
    assert launched["command"]["target_refs"] == ["observation:OBS-1"]
    assert launched["context"] == {"observation_id": "OBS-1"}


def test_finding_draft_tab_button_scopes_a_redraft_to_the_named_finding(
    workspace_with_data, monkeypatch
):
    """Accepting a consolidation redrafts the lead by naming it.

    ``finding_id`` is a declared scope key for the template, so the Findings
    page's redraft reaches the capability as a ``finding:`` ref rather than
    being refused as an undeclared key and leaving the lead's narrative
    reading as one member's draft.
    """
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command=command, context=context)
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Redraft finding F-1 from its consolidated observations.",
        "intent": "act", "mode": "auto", "request_id": "request-finding-redraft",
        "source": "tab_button", "goal_template": "finding_draft",
        "run_context": {"finding_id": "F-1"},
    })

    assert result["outcome"]["kind"] != "error"
    assert launched["command"]["target_refs"] == ["finding:F-1"]


def test_finding_draft_tab_button_scopes_a_batch_to_named_rcm_rows(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command=command, context=context)
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    assistant_chats.send_message(ws, chat["id"], {
        "content": "Draft findings for 2 RCM rows with undrafted exceptions.",
        "intent": "act", "mode": "auto", "request_id": "request-finding-batch",
        "source": "tab_button", "goal_template": "finding_draft",
        "run_context": {"rcm_ids": ["RCM-1", "RCM-2"]},
    })

    assert launched["command"]["target_refs"] == ["rcm:RCM-1", "rcm:RCM-2"]


def _capture_launch(monkeypatch) -> dict:
    launched: dict = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command=command, context=context)
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    return launched


@pytest.mark.parametrize(
    ("action", "mode"),
    [("analyze", "reuse_existing"), ("refresh", "force"), ("revise_vocabulary", "force")],
)
def test_document_analysis_tab_button_carries_its_action_and_documents(
    workspace_with_data, monkeypatch, action, mode
):
    """The Documents tab's buttons once started runs the chat never saw."""

    ws = workspace_with_data
    configured(monkeypatch)
    launched = _capture_launch(monkeypatch)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Analyse invoice.pdf.",
        "intent": "act", "mode": "auto", "request_id": f"request-doc-{action}",
        "source": "tab_button", "command": "analyze_documents",
        "run_context": {
            "document_ids": ["DOC-1"], "action": action,
            "full_visual_document_ids": ["DOC-1"],
        },
    })

    assert result["outcome"]["kind"] != "error"
    assert launched["command"]["target_refs"] == ["document:DOC-1"]
    assert launched["command"]["generation_mode"] == mode
    assert launched["context"]["full_visual_document_ids"] == ["DOC-1"]


def test_document_analysis_tab_button_rejects_an_unknown_action(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    _capture_launch(monkeypatch)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Analyse invoice.pdf.",
        "intent": "act", "mode": "auto", "request_id": "request-doc-bad-action",
        "source": "tab_button", "command": "analyze_documents",
        "run_context": {"document_ids": ["DOC-1"], "action": "rebuild_everything"},
    })

    assert result["outcome"]["kind"] == "error"


def test_report_regenerate_requests_only_the_report(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = _capture_launch(monkeypatch)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Redraft the audit report.",
        "intent": "act", "mode": "auto", "request_id": "request-report-redraft",
        "source": "tab_button", "requested_outcomes": ["report.working_draft"],
        "run_context": {"generation_mode": "force", "requested_only": True},
    })

    assert result["outcome"]["kind"] != "error"
    assert launched["command"]["requested_outcomes"] == ["report.working_draft"]
    assert launched["command"]["generation_mode"] == "force"
    assert launched["context"]["requested_only"] is True


def test_finding_draft_batch_rejects_a_malformed_row_list(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    chat = assistant_chats.create_chat(ws)

    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Draft findings for the rows with undrafted exceptions.",
        "intent": "act", "mode": "auto", "request_id": "request-finding-batch-bad",
        "source": "tab_button", "goal_template": "finding_draft",
        "run_context": {"rcm_ids": "RCM-1"},
    })

    assert result["outcome"]["kind"] == "error"
    assert "rcm_ids" in result["outcome"]["message"]


def test_run_projection_uses_durable_activity_instead_of_coarse_status(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto",
        {"source": "chat", "text": "prepare planning"},
    )
    run["status"] = "executing"
    run["started"] = store.utcnow()
    run["activity_revision"] = 4
    run["activity"] = {
        "phase": "planning.documents", "label": "Analyzing documents",
        "detail": "Procurement policy.docx", "current": 2, "total": 7,
        "attempt": 1, "task_id": "planning:context", "action_id": None,
        "started_at": store.utcnow(), "updated_at": store.utcnow(),
        "waiting_on": "model", "model_calls_active": 1,
        "model_started_at": store.utcnow(),
    }

    projection = assistant_chats._run_projection(run)

    assert projection["current_activity"] == (
        "Analyzing documents (2/7) — Procurement policy.docx"
    )
    assert projection["activity_revision"] == 4
    assert projection["activity"]["waiting_on"] == "model"
    assert projection["duration_ms"] is not None


def test_slash_command_never_reaches_the_model(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    def fail(*args, **kwargs):
        raise AssertionError("a slash command must not spend a model turn")

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    monkeypatch.setattr(assistant_chats.assistant, "ask", fail)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "/generate apm", "intent": "auto", "mode": "auto",
        "request_id": "request-slash-apm", "source": "composer",
    })

    assert result["outcome"]["kind"] == "run_started"
    assert launched["goal_template"] == "apm_only"
    assert result["chat"]["messages"][0]["resolved_intent"] == "act"


def test_run_analyses_command_requests_execution_only(workspace_with_data, monkeypatch):
    """"Run the saved analyses" executes; it does not invite new definitions."""
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    monkeypatch.setattr(
        assistant_chats.assistant, "ask",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model turn")),
    )
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "/run analyses", "intent": "auto", "mode": "auto",
        "request_id": "request-slash-run-analyses", "source": "composer",
    })

    assert result["outcome"]["kind"] == "run_started"
    assert launched["goal_template"] == "analysis_execution"
    assert routing.template_outcomes("analysis_execution") == ["analysis.executed"]


def test_analysis_tab_can_scope_a_run_to_the_tables_on_screen(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None, **kwargs):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Analyse the data in this workspace.", "intent": "act", "mode": "auto",
        "request_id": "request-scoped-analysis", "source": "tab_button",
        "goal_template": "data_analysis",
        "run_context": {"tables": ["transactions"]},
    })

    assert result["outcome"]["kind"] == "run_started"
    # Scope travels as target refs, which is what the analysis capabilities read.
    assert launched["target_refs"] == ["table:transactions"]


def test_generate_rcm_slash_command_starts_rcm_run(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "/generate rcm", "intent": "auto", "mode": "auto",
        "request_id": "request-slash-rcm", "source": "composer",
    })

    assert result["outcome"]["kind"] == "run_started"
    assert launched["goal_template"] == "rcm_only"


def test_status_slash_command_is_deterministic_and_does_not_start_a_run(
    workspace_with_data, monkeypatch,
):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)

    def fail(*args, **kwargs):
        raise AssertionError("/status must not call a model or start a run")

    monkeypatch.setattr(assistant_chats.llm, "chat", fail)
    monkeypatch.setattr(assistant_chats.assistant, "ask", fail)
    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fail)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "/status", "intent": "auto", "mode": "auto",
        "request_id": "request-slash-status", "source": "composer",
    })

    assert result["outcome"]["kind"] == "status"
    assert store.list_runs(ws) == []
    reply = result["chat"]["messages"][-1]
    assert reply["kind"] == "text"
    assert reply["resolved_intent"] == "ask"
    assert "## Audit status" in reply["content"]
    assert "Planning" in reply["content"]


def test_unknown_slash_command_asks_for_clarification(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "/nonexistent-command", "intent": "auto", "mode": "auto",
        "request_id": "request-slash-unknown", "source": "composer",
    })

    assert result["outcome"]["kind"] == "clarification_requested"
    reply = result["chat"]["messages"][-1]
    assert reply["kind"] == "clarification"
    assert "Unknown command" in reply["content"]


def test_explicit_command_field_resolves_goal_template(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Prepare document tests for this engagement.", "intent": "auto", "mode": "auto",
        "request_id": "request-command-field", "source": "tab_button",
        "command": "prepare_document_tests",
    })

    assert result["outcome"]["kind"] == "run_started"
    assert launched["goal_template"] == "document_test_preparation"
    assert result["chat"]["messages"][0]["resolved_intent"] == "act"


def test_command_field_rejects_conflicting_goal_template(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    chat = assistant_chats.create_chat(ws)
    with pytest.raises(Exception, match="Use either command or goal_template"):
        assistant_chats.send_message(ws, chat["id"], {
            "content": "Prepare document tests.", "intent": "auto", "mode": "auto",
            "request_id": "request-command-conflict", "source": "tab_button",
            "command": "prepare_document_tests", "goal_template": "document_test_preparation",
        })


def test_unknown_command_field_is_rejected(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    chat = assistant_chats.create_chat(ws)
    with pytest.raises(Exception, match="Unknown command"):
        assistant_chats.send_message(ws, chat["id"], {
            "content": "Do something.", "intent": "auto", "mode": "auto",
            "request_id": "request-command-unknown", "source": "tab_button",
            "command": "not_a_real_command",
        })


def test_exact_command_phrase_starts_a_run_without_a_model_turn(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None):
        launched.update(command)
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    def fail(*args, **kwargs):
        raise AssertionError("an exact command phrase must not spend a model turn")

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    monkeypatch.setattr(assistant_chats.assistant, "ask", fail)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Generate the audit report.", "intent": "auto", "mode": "auto",
        "request_id": "request-phrase-report", "source": "composer",
    })

    assert result["outcome"]["kind"] == "run_started"
    assert launched["goal_template"] == "report"
    assert launched["source"] == "goal_template"


def test_text_merely_mentioning_a_command_is_coordinated_not_launched(workspace_with_data, monkeypatch):
    """The phrase table is whole-message, so a question about an artifact is
    still a question."""

    ws = workspace_with_data
    configured(monkeypatch)
    seen = {}

    def fake_ask(workspace, question, document_ids, *, prior_turns, chat_id=None, commander=None):
        seen["question"] = question
        return {
            "answer": "The report has not been drafted yet.", "steps": [],
            "artifacts": [], "citations": [], "document_context": None,
            "started_run": None,
        }

    def fail(*args, **kwargs):
        raise AssertionError("a question must not start a run on its own")

    monkeypatch.setattr(assistant_chats.assistant, "ask", fake_ask)
    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fail)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Tell me about the audit report.", "intent": "auto", "mode": "auto",
        "request_id": "request-phrase-mention", "source": "composer",
    })

    assert seen["question"] == "Tell me about the audit report."
    assert result["outcome"]["kind"] == "answer"
    assert result["chat"]["messages"][0]["resolved_intent"] == "ask"
    assert store.list_runs(ws) == []


def test_coordinator_starting_a_run_is_recorded_as_an_action(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command)
        launched["run_context"] = dict(context or {})
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id)
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    def fake_ask(workspace, question, document_ids, *, prior_turns, chat_id=None, commander=None):
        # Stand in for the model choosing the mutating tool.
        started = commander.launch_command("generate_report")
        return {
            "answer": "Starting the report now.", "steps": [], "artifacts": [],
            "citations": [], "document_context": None, "started_run": started,
        }

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    monkeypatch.setattr(assistant_chats.assistant, "ask", fake_ask)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "I think it is time we pulled the report together.",
        "intent": "auto", "mode": "auto",
        "request_id": "request-coordinated-act", "source": "composer",
    })

    assert result["outcome"]["kind"] == "run_started"
    assert launched["goal_template"] == "report"
    # The auditor's own words travel with the run they started (B6).
    assert launched["run_context"]["instruction"] == (
        "I think it is time we pulled the report together."
    )
    messages = result["chat"]["messages"]
    assert messages[0]["resolved_intent"] == "act"
    assert messages[0]["outcome"]["run_id"] == result["outcome"]["run_id"]
    # The model's closing line is kept, and carries no run id of its own: the
    # run card hangs off the auditor's message only.
    assert messages[1]["content"] == "Starting the report now."
    assert messages[1]["outcome"]["kind"] == "answer"
    run_cards = [item for item in result["chat"]["transcript"] if item.get("type") == "run"]
    assert len(run_cards) == 1


def test_explicit_ask_is_lent_no_mutating_capability(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    seen = {}

    def fake_ask(workspace, question, document_ids, *, prior_turns, chat_id=None, commander=None):
        seen["commander"] = commander
        return {
            "answer": "Six tables.", "steps": [], "artifacts": [], "citations": [],
            "document_context": None, "started_run": None,
        }

    monkeypatch.setattr(assistant_chats.assistant, "ask", fake_ask)
    chat = assistant_chats.create_chat(ws)
    assistant_chats.send_message(ws, chat["id"], {
        "content": "How many tables are there?", "intent": "ask", "mode": "auto",
        "request_id": "request-explicit-ask", "source": "composer",
    })

    assert seen["commander"] is None


def test_pending_turn_is_recovered_as_failed(workspace_with_data):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    path = assistant_chats._chat_path(ws, chat["id"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["messages"].append({"id": "msg_deadbeefcafe", "state": "pending", "content": "Interrupted"})
    path.write_text(json.dumps(record), encoding="utf-8")
    restored = assistant_chats.get_chat(ws, chat["id"])
    assert restored["messages"][0]["state"] == "failed"
    assert "Interrupted before completion" in restored["messages"][0]["error"]


# --------------------------------------------------------------------------- #
# A Redraft button naming one test (step 2)
# --------------------------------------------------------------------------- #
def test_a_redraft_button_carries_the_test_ref_and_the_force(
    workspace_with_data, monkeypatch
):
    """Force travels with the command, not inferred from the sentence on it."""
    ws = workspace_with_data
    configured(monkeypatch)
    launched = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command=command, context=context)
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    assistant_chats.send_message(ws, chat["id"], {
        "content": "Data test DAT-1 does not look right; redraft it.",
        "intent": "act", "mode": "auto", "request_id": "request-redraft",
        "source": "tab_button",
        "requested_outcomes": ["tests.specified"],
        "run_context": {
            "target_refs": ["datatest:DAT-1"],
            "generation_mode": "force",
        },
    })

    assert launched["command"]["target_refs"] == ["datatest:DAT-1"]
    assert launched["command"]["generation_mode"] == "force"


def test_a_requested_outcomes_message_rejects_an_unknown_generation_mode(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    configured(monkeypatch)
    chat = assistant_chats.create_chat(ws)

    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Redraft it.",
        "intent": "act", "mode": "auto", "request_id": "request-bad-mode",
        "source": "tab_button",
        "requested_outcomes": ["tests.specified"],
        "run_context": {"target_refs": ["datatest:DAT-1"], "generation_mode": "wipe"},
    })

    assert result["outcome"]["kind"] == "error"
    assert "generation_mode" in result["outcome"]["message"]


def test_a_requested_outcomes_message_still_refuses_an_undeclared_scope_key(
    workspace_with_data, monkeypatch
):
    """Scope, never a routing override: the allowed set is exactly two keys."""
    ws = workspace_with_data
    configured(monkeypatch)
    chat = assistant_chats.create_chat(ws)

    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Redraft it.",
        "intent": "act", "mode": "auto", "request_id": "request-bad-key",
        "source": "tab_button",
        "requested_outcomes": ["tests.specified"],
        "run_context": {"engine": "action"},
    })

    assert result["outcome"]["kind"] == "error"


# --------------------------------------------------------------------------- #
# The auditor's instruction: what a person typed travels with the run it starts,
# so every stage that declares an instruction source reads it.
# --------------------------------------------------------------------------- #
TYPED_REQUEST = "Analyse the data and test capture timeliness, dealing hours and segregation."


def _from_composer(content=TYPED_REQUEST, source="composer"):
    return {"id": "msg_1", "content": content, "source": source}


@pytest.fixture
def started_runs(monkeypatch):
    calls: list[dict] = []
    configured(monkeypatch)
    monkeypatch.setattr(assistant_chats, "_active_run", lambda workspace, summaries=None: None)

    def start(workspace, mode, command, parent_run_id=None, context=None):
        calls.append({"command": command, "context": dict(context or {})})
        return {"id": "20261107-000000-aaaaaa", "command": {"id": "cmd_1"}}

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", start)
    return calls


@pytest.mark.parametrize("content, source, expected", [
    (TYPED_REQUEST, "composer", TYPED_REQUEST),
    ("/analysis", "composer", ""),
    # A registered command phrase is the command itself, like a button.
    ("Generate the audit report.", "composer", ""),
    ("Run the saved analyses", "tab_button", ""),
    ("Run the saved analyses", "shortcut", ""),
    ("   ", "composer", ""),
])
def test_only_words_a_person_typed_are_an_instruction(content, source, expected):
    assert assistant_chats.auditor_instruction_of(_from_composer(content, source)) == expected


def test_a_typed_request_routed_to_a_template_carries_the_auditors_words(
    workspace_with_data, started_runs,
):
    assistant_chats._launch_command(
        workspace_with_data, "chat_1", _from_composer(), {"messages": []}, "auto",
        goal_template="data_analysis",
    )

    assert started_runs[0]["context"]["instruction"] == TYPED_REQUEST


def test_a_button_label_is_not_an_instruction(workspace_with_data, started_runs):
    assistant_chats._launch_command(
        workspace_with_data, "chat_1", _from_composer("Run the saved analyses", "tab_button"),
        {"messages": []}, "auto", goal_template="analysis_execution",
    )

    assert "instruction" not in started_runs[0]["context"]


def test_an_instruction_the_caller_supplied_is_kept(workspace_with_data, started_runs):
    assistant_chats._launch_command(
        workspace_with_data, "chat_1", _from_composer(), {"messages": []}, "auto",
        goal_template="data_analysis", run_context={"instruction": "Focus on segregation."},
    )

    assert started_runs[0]["context"]["instruction"] == "Focus on segregation."


def test_a_loop_hand_off_keeps_the_verbatim_request_beside_its_brief(
    workspace_with_data, started_runs,
):
    commander = assistant_chats._commander(
        workspace_with_data, "chat_1", _from_composer(), {"messages": []}, "auto",
    )

    commander.launch_loop("Run the data analysis focused on the policy clauses.")

    # The brief is the coordinator's paraphrase; the instruction is the request.
    assert started_runs[0]["command"]["text"].startswith("Run the data analysis")
    assert started_runs[0]["context"]["instruction"] == TYPED_REQUEST


def test_a_continuing_message_carries_the_requirements_it_refers_to(
    workspace_with_data, started_runs,
):
    """"Carry on with the analysis" names no test; the ones it means were in an
    earlier message. The coordinator quotes them, and the instruction is the
    verbatim message followed by that quote."""

    carry_on = "Please carry on with the analysis and tell me what exceptions you find."
    earlier = "test capture timeliness, dealing hours and segregation between dealers and confirmers"
    command_id = next(
        key for key, item in assistant_chats.commands.COMMANDS.items()
        if item.goal_template == "data_analysis"
    )
    commander = assistant_chats._commander(
        workspace_with_data, "chat_1", _from_composer(carry_on), {"messages": []}, "auto",
    )

    commander.launch_command(command_id, earlier)

    instruction = started_runs[0]["context"]["instruction"]
    assert instruction.startswith(carry_on)
    assert "segregation between dealers and confirmers" in instruction


def test_every_goal_template_accepts_an_instruction_as_run_context(workspace_with_data, monkeypatch):
    seen: dict = {}

    def process(workspace, chat_id, user, record, mode, goal_template, run_kind,
                run_context, requested_outcomes, local_handler):
        seen["run_context"] = run_context
        return {"kind": "run_started"}

    monkeypatch.setattr(assistant_chats, "_process_message", process)
    chat = assistant_chats.create_chat(workspace_with_data)

    assistant_chats.send_message(workspace_with_data, chat["id"], {
        "content": "Draft the APM", "intent": "act", "request_id": "req_instruction_1",
        "goal_template": "apm_only", "run_context": {"instruction": "Focus on settlement."},
    })

    assert seen["run_context"] == {"instruction": "Focus on settlement."}


def test_apm_regenerate_redrafts_a_current_memorandum_and_nothing_upstream(workspace_with_data, monkeypatch):
    """The APM page's Regenerate sent a command that a command run creates as
    reuse_existing, so it found the current memorandum, scheduled nothing and
    reported success. As a forced requested outcome it redrafts that one stage."""
    from app.agent.routing import resolve_route

    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Assess procurement"}})
    ws.update_planning({"apm_markdown": "# Audit Planning Memorandum\n\n## Engagement\n\nAcme."})
    configured(monkeypatch)
    launched: dict = {}

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        run = store.new_command_run(workspace, mode, command, parent_run_id=parent_run_id, context=context)
        launched["run"] = run
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    result = assistant_chats.send_message(ws, chat["id"], {
        "content": "Regenerate the audit planning memorandum.",
        "intent": "act", "mode": "auto", "request_id": "request-apm-regenerate",
        "source": "tab_button", "requested_outcomes": ["planning.apm_ready"],
        "run_context": {"generation_mode": "force", "requested_only": True},
    })
    assert result["outcome"]["kind"] != "error"

    run = launched["run"]
    assert resolve_route(workspaces.load_workspace(ws.id), run) == "workflow"
    stages = store.load_run(ws, run["id"])["workflow"]["stages"]
    assert [stage["capability"] for stage in stages] == ["planning.apm_ready"]
    assert stages[0]["scheduled_because"] == "forced"


def test_revise_slash_commands_accept_trailing_instruction(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    configured(monkeypatch)
    launched: list[dict] = []

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.append({"command": dict(command), "context": dict(context or {})})
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    monkeypatch.setattr(
        assistant_chats.assistant,
        "ask",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("slash command must not call model")),
    )
    chat = assistant_chats.create_chat(ws)

    rcm_res = assistant_chats.send_message(ws, chat["id"], {
        "content": "/revise rcm merge duplicate vendor risks",
        "intent": "auto", "mode": "auto", "request_id": "req-slash-revise-rcm", "source": "composer",
    })
    assert rcm_res["outcome"]["kind"] == "run_started"
    assert launched[0]["command"]["goal_template"] == "rcm_revision"
    assert launched[0]["context"]["instruction"] == "merge duplicate vendor risks"

    apm_res = assistant_chats.send_message(ws, chat["id"], {
        "content": "/revise apm tighten the materiality scope",
        "intent": "auto", "mode": "auto", "request_id": "req-slash-revise-apm", "source": "composer",
    })
    assert apm_res["outcome"]["kind"] == "run_started"
    assert launched[1]["command"]["goal_template"] == "apm_revision"
    assert launched[1]["context"]["instruction"] == "tighten the materiality scope"

    # Commands that do not accept arguments still reject trailing text.
    bad_res = assistant_chats.send_message(ws, chat["id"], {
        "content": "/generate apm extra words",
        "intent": "auto", "mode": "auto", "request_id": "req-slash-bad-args", "source": "composer",
    })
    assert bad_res["outcome"]["kind"] == "clarification_requested"


def test_composer_document_context_forwards_to_document_analysis_and_loop(
    workspace_with_data, monkeypatch
):
    from app import documents as document_service

    ws = workspace_with_data
    doc = document_service.add_document(
        ws, "policy.txt", b"Approval limits require director sign-off above 5000."
    )
    configured(monkeypatch)
    launched: list[dict] = []

    def fake_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.append({"command": dict(command), "context": dict(context or {})})
        run = store.new_command_run(
            workspace, mode, command, parent_run_id=parent_run_id, context=context
        )
        run["status"] = "completed"
        store.save_run(workspace, run)
        return run

    monkeypatch.setattr(assistant_chats.runner, "start_command_run", fake_start)
    chat = assistant_chats.create_chat(ws)
    assistant_chats.update_chat(ws, chat["id"], {"document_ids": [doc["id"]]})

    res = assistant_chats.send_message(ws, chat["id"], {
        "content": "/analyze documents",
        "intent": "auto", "mode": "auto", "request_id": "req-doc-ctx-slash", "source": "composer",
    })
    assert res["outcome"]["kind"] == "run_started"
    assert launched[0]["context"]["document_ids"] == [doc["id"]]
    assert launched[0]["command"]["target_refs"] == [f"document:{doc['id']}"]

    # And when the coordinator hands off to the steering loop via take_action:
    def fake_ask(_ws, _q, _docs, *, prior_turns=None, chat_id=None, commander=None):
        started = commander.launch_loop("Assess whether policy.txt changes the RCM.")
        return {"answer": "On it.", "steps": [], "artifacts": [], "citations": [], "started_run": started}

    monkeypatch.setattr(assistant_chats.assistant, "ask", fake_ask)
    res_loop = assistant_chats.send_message(ws, chat["id"], {
        "content": "Does this policy change our RCM?",
        "intent": "auto", "mode": "auto", "request_id": "req-doc-ctx-loop", "source": "composer",
    })
    assert res_loop["outcome"]["kind"] == "run_started"
    assert launched[1]["context"]["document_ids"] == [doc["id"]]
