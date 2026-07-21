import inspect
import json
import re
import threading

import pytest

from app import documents, llm
from app.agent import base, runner, store
from app.agent.runtime import (
    Cancelled,
    DefaultModelGateway,
    DefaultRunRuntime,
    LimitExceeded,
    ModelGateway,
    RunRuntime,
)


def _base_runner(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    return base.BaseRunner(workspace_with_data, run, handle)


def test_run_runtime_contract_matches_active_base_runner(workspace_with_data):
    active = _base_runner(workspace_with_data)

    assert isinstance(active, RunRuntime)
    assert set(RunRuntime.__dict__) >= {
        "save",
        "emit",
        "utcnow",
        "mark_started",
        "mark_finished",
        "set_status",
        "set_activity",
        "set_model_wait",
        "warn",
        "deadline",
        "update_limits",
        "reserve_model_turn",
        "record_model_usage",
        "checkpoint",
        "drain_inbox",
        "wait_for_input",
        "request_approval",
    }


def test_base_runner_delegates_durable_run_operations(workspace_with_data, monkeypatch):
    active = _base_runner(workspace_with_data)
    calls = []

    monkeypatch.setattr(active.runtime, "save", lambda: calls.append(("save",)))
    monkeypatch.setattr(
        active.runtime,
        "emit",
        lambda type_, data: calls.append(("emit", type_, data)),
    )
    monkeypatch.setattr(
        active.runtime,
        "set_status",
        lambda status: calls.append(("status", status)),
    )
    monkeypatch.setattr(
        active.runtime,
        "set_activity",
        lambda phase, label, **fields: calls.append(
            ("activity", phase, label, fields)
        ),
    )
    monkeypatch.setattr(
        active.runtime,
        "warn",
        lambda warning: calls.append(("warning", warning)),
    )

    active.save()
    active.emit("test_event", {"ok": True})
    active.set_status("executing")
    active.set_activity("test.phase", "Testing", current=1, total=2)
    active.warn("Be careful")

    assert calls == [
        ("save",),
        ("emit", "test_event", {"ok": True}),
        ("status", "executing"),
        (
            "activity",
            "test.phase",
            "Testing",
            {
                "detail": None,
                "current": 1,
                "total": 2,
                "attempt": None,
                "task_id": None,
                "action_id": None,
            },
        ),
        ("warning", "Be careful"),
    ]


def test_default_run_runtime_owns_durable_timing_and_activity(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    timestamps = iter(
        [
            "2026-07-21T10:00:00.000+00:00",
            "2026-07-21T10:00:01.000+00:00",
            "2026-07-21T10:00:02.000+00:00",
            "2026-07-21T10:00:03.000+00:00",
        ]
    )
    runtime = DefaultRunRuntime(
        workspace=workspace_with_data,
        run=run,
        state_lock=threading.RLock(),
        clock=lambda: next(timestamps),
    )

    assert runtime.mark_started() == "2026-07-21T10:00:00.000+00:00"
    assert runtime.mark_started() == "2026-07-21T10:00:00.000+00:00"
    runtime.set_activity("test.phase", "First")
    runtime.set_activity("test.phase", "Second")
    assert runtime.mark_finished() == "2026-07-21T10:00:03.000+00:00"

    durable = store.load_run(workspace_with_data, run["id"])
    assert durable["started"] == "2026-07-21T10:00:00.000+00:00"
    assert durable["finished"] == "2026-07-21T10:00:03.000+00:00"
    assert durable["activity"]["started_at"] == "2026-07-21T10:00:01.000+00:00"
    assert durable["activity"]["updated_at"] == "2026-07-21T10:00:02.000+00:00"
    assert durable["activity_revision"] == 2


def test_default_run_runtime_owns_limits_and_model_budgets(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    runtime = DefaultRunRuntime(
        workspace=workspace_with_data,
        run=run,
        state_lock=threading.RLock(),
    )

    runtime.update_limits(
        {
            "max_model_turns": 2,
            "max_estimated_prompt_tokens": 50,
            "max_completion_tokens": 10,
        }
    )
    runtime.update_limits(
        {
            "max_model_turns": 1,
            "max_estimated_prompt_tokens": 100,
        },
        grow_only=True,
    )
    budget = runtime.reserve_model_turn(
        request_characters=80,
        estimated_input_tokens=20,
        attempt=2,
    )
    assert budget == {
        "max_prompt_tokens": 100,
        "max_completion_tokens": 10,
    }
    assert runtime.record_model_usage(
        worker="agent:contract_test",
        request_characters=80,
        estimated_input_tokens=20,
        prompt_tokens=25,
        completion_tokens=11,
        latency_ms=12.5,
        attempt=2,
        context_metrics={"selected": 1},
        budget=budget,
    ) is True

    durable = store.load_run(workspace_with_data, run["id"])
    assert durable["limits"]["max_model_turns"] == 2
    assert durable["limits"]["max_estimated_prompt_tokens"] == 100
    assert durable["usage"]["llm_turns"] == 1
    assert durable["usage"]["estimated_prompt_tokens"] == 20
    assert durable["usage"]["prompt_tokens"] == 25
    assert durable["usage"]["completion_tokens"] == 11
    assert durable["usage"]["retries"] == 1
    assert durable["usage"]["model_calls_by_worker"] == {
        "agent:contract_test": 1
    }
    assert durable["usage"]["model_call_metrics"][0]["context_metrics"] == {
        "selected": 1
    }


def test_default_run_runtime_owns_checkpoint_controls_deadline_and_inbox(
    workspace_with_data,
):
    run = store.new_run(workspace_with_data, "auto")
    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    handle.pause_requested.set()
    handle.resume.set()
    handle.inbox.extend(["first", "second"])
    monotonic_values = iter([100.0, 110.0, 120.0, 121.0])
    timestamps = iter(
        [
            "2026-07-21T11:00:00.000+00:00",
            "2026-07-21T11:00:01.000+00:00",
        ]
    )
    runtime = DefaultRunRuntime(
        workspace=workspace_with_data,
        run=run,
        state_lock=threading.RLock(),
        handle=handle,
        monotonic=lambda: next(monotonic_values),
        max_runtime_seconds=30,
        clock=lambda: next(timestamps),
    )

    runtime.checkpoint()

    durable = store.load_run(workspace_with_data, run["id"])
    assert durable["status"] == "executing"
    assert [message["content"] for message in durable["messages"]] == [
        "first",
        "second",
    ]
    assert handle.inbox == []
    assert runtime.deadline == 140.0
    statuses = [
        event["data"]["status"]
        for event in store.read_events(workspace_with_data, run["id"])
    ]
    assert statuses == ["paused", "executing"]

    handle.command_queue.append({"id": "command-1", "text": "follow up"})
    handle.command_queued.set()
    runtime.drain_inbox(queue_commands=True)
    durable = store.load_run(workspace_with_data, run["id"])
    assert durable["pending_commands"] == [
        {"id": "command-1", "text": "follow up"}
    ]
    assert handle.command_queue == []
    assert not handle.command_queued.is_set()

    handle.cancel.set()
    with pytest.raises(Cancelled):
        runtime.checkpoint()

    expired_run = store.new_run(workspace_with_data, "auto")
    expired_handle = runner.RunHandle(workspace_with_data.id, expired_run["id"])
    expired_times = iter([200.0, 202.0])
    expired_runtime = DefaultRunRuntime(
        workspace=workspace_with_data,
        run=expired_run,
        state_lock=threading.RLock(),
        handle=expired_handle,
        monotonic=lambda: next(expired_times),
        max_runtime_seconds=1,
    )
    with pytest.raises(LimitExceeded, match="run time limit reached"):
        expired_runtime.checkpoint()


def test_run_runtime_active_status_activity_and_warning_contract(workspace_with_data):
    active = _base_runner(workspace_with_data)

    active.set_status("executing")
    active.set_status("executing")
    active.set_activity(
        "contract.test",
        "Checking runtime contract",
        detail="Safe progress only",
        current=1,
        total=2,
        task_id="task-1",
    )
    active.warn("Contract warning")

    durable = store.load_run(workspace_with_data, active.run["id"])
    assert durable["status"] == "executing"
    assert durable["activity"] == {
        "phase": "contract.test",
        "label": "Checking runtime contract",
        "detail": "Safe progress only",
        "current": 1,
        "total": 2,
        "attempt": None,
        "task_id": "task-1",
        "action_id": None,
        "started_at": durable["activity"]["started_at"],
        "updated_at": durable["activity"]["updated_at"],
        "waiting_on": None,
        "model_calls_active": 0,
        "model_started_at": None,
    }
    assert durable["activity_revision"] == 1
    assert durable["warnings"] == ["Contract warning"]

    events = store.read_events(workspace_with_data, active.run["id"])
    assert [event["type"] for event in events] == [
        "run_status",
        "activity_update",
        "warning",
    ]
    assert events[1]["data"]["revision"] == 1
    assert events[2]["data"] == {"text": "Contract warning"}


def test_model_gateway_contract_wraps_active_budget_and_provenance_behavior(
    workspace_with_data, monkeypatch
):
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return {
            "content": "SENSITIVE_PROVIDER_RESPONSE",
            "usage": {"prompt_tokens": 17, "completion_tokens": 5},
        }

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "provider": "contract-test", "model": "model"},
    )
    active = _base_runner(workspace_with_data)
    active.run["limits"] = {
        "max_model_turns": 1,
        "max_estimated_prompt_tokens": 1_000,
        "max_completion_tokens": 100,
        "max_llm_concurrency": 1,
    }
    active.save()
    gateway = active.model_gateway
    system = "[agent:contract_test]\nSENSITIVE_SYSTEM_PROMPT"
    user = "SENSITIVE_USER_PAYLOAD"

    assert isinstance(gateway, ModelGateway)
    assert gateway.complete(system, user, attempt=2) == "SENSITIVE_PROVIDER_RESPONSE"
    assert calls == [(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        {"profile": "agent"},
    )]

    usage = store.load_run(workspace_with_data, active.run["id"])["usage"]
    assert usage["llm_turns"] == 1
    assert usage["prompt_tokens"] == 17
    assert usage["completion_tokens"] == 5
    assert usage["retries"] == 1
    assert usage["model_calls_by_worker"] == {"agent:contract_test": 1}
    assert usage["model_usage_by_worker"]["agent:contract_test"]["retries"] == 1
    assert usage["model_call_metrics"][0]["retry_number"] == 2

    activity = documents.activities(workspace_with_data)["items"][-1]
    serialized = json.dumps(activity)
    assert activity["prompt_version"]
    assert activity["response_hash"]
    assert "SENSITIVE_SYSTEM_PROMPT" not in serialized
    assert "SENSITIVE_USER_PAYLOAD" not in serialized
    assert "SENSITIVE_PROVIDER_RESPONSE" not in serialized
    assert activity["stage"] == "agent:contract_test"
    assert activity["retry_number"] == 2


def test_base_runner_delegates_model_calls_to_gateway(workspace_with_data, monkeypatch):
    active = _base_runner(workspace_with_data)
    calls = []

    def fake_complete(system, user, activity=None, *, attempt=1):
        calls.append((system, user, activity, attempt))
        return "delegated"

    monkeypatch.setattr(active.model_gateway, "complete", fake_complete)

    assert (
        active._llm_content(
            "system",
            "user",
            {"task_id": "task-1"},
            attempt=2,
        )
        == "delegated"
    )
    assert calls == [("system", "user", {"task_id": "task-1"}, 2)]


def test_runtime_contracts_and_gateway_are_domain_neutral():
    assert getattr(ModelGateway, "_is_protocol", False)
    assert getattr(RunRuntime, "_is_protocol", False)
    assert isinstance(DefaultModelGateway, type)
    assert isinstance(DefaultRunRuntime, type)

    for runtime_type in (
        DefaultModelGateway,
        DefaultRunRuntime,
        ModelGateway,
        RunRuntime,
    ):
        source = inspect.getsource(inspect.getmodule(runtime_type)).casefold()
        forbidden = {
            "apm",
            "rcm",
            "fieldwork",
            "finding",
            "working_paper",
            "dashboard",
            "report",
            "audit",
        }
        assert not {
            term for term in forbidden if re.search(rf"\b{term}\b", source)
        }


def test_base_runner_no_longer_owns_provider_call_behavior():
    source = inspect.getsource(base.BaseRunner)

    assert "_provider_semaphore" not in source
    assert "llm.chat(" not in source
    assert "debug_store.trace_context" not in source
    assert 'usage["prompt_tokens"]' not in source
    assert "hashlib.sha1" not in source


def test_base_runner_no_longer_owns_durable_run_projection_behavior():
    source = inspect.getsource(base.BaseRunner)

    assert "store.save_run" not in source
    assert "store.append_event" not in source
    assert 'self.run["activity"]' not in source
    assert 'self.run["activity_revision"]' not in source
    assert 'self.run["status"] = status' not in source


def test_model_gateway_delegates_activity_projection_to_runtime():
    source = inspect.getsource(DefaultModelGateway)

    assert 'self.run["activity"]' not in source
    assert 'self.run["activity_revision"]' not in source
    assert 'self._emit("activity_update"' not in source


def test_base_runner_no_longer_owns_budgets_or_checkpoint_controls():
    source = inspect.getsource(base.BaseRunner)
    checkpoint_source = inspect.getsource(base.BaseRunner.checkpoint)
    init_source = inspect.getsource(base.BaseRunner.__init__)
    inbox_source = inspect.getsource(base.BaseRunner._drain_inbox)

    assert "MAX_RUNTIME_SECONDS" not in source
    assert 'self.run.setdefault("usage"' not in source
    assert "time.monotonic()" not in init_source
    assert "self.handle.cancel.is_set()" not in checkpoint_source
    assert "self.handle.pause_requested.is_set()" not in checkpoint_source
    assert "self.handle.inbox[:]" not in inbox_source


def test_model_gateway_delegates_budget_ledger_to_runtime():
    source = inspect.getsource(DefaultModelGateway)

    assert 'self.run.setdefault("usage"' not in source
    assert 'self.run.get("limits")' not in source
    assert "self._reserve_model_turn(" in source
    assert "self._record_model_usage(" in source
