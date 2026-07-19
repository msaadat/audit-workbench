import json

import pytest

from app import assistant_chats, llm
from app.agent import store


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

    def fake_ask(workspace, question, document_ids, *, prior_turns):
        calls.append((question, prior_turns))
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
    restored = assistant_chats.get_chat(ws, chat["id"])
    assert [item["state"] for item in restored["messages"]] == ["complete"] * 4
    assert restored["title"] == "What tables are available?"


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
