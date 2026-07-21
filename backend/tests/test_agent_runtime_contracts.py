import inspect
import json
import re

from app import documents, llm
from app.agent import base, runner, store
from app.agent.runtime import DefaultModelGateway, ModelGateway, RunRuntime


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
        "set_status",
        "set_activity",
        "warn",
        "checkpoint",
        "wait_for_input",
        "request_approval",
    }


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

    for runtime_type in (DefaultModelGateway, ModelGateway, RunRuntime):
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

    run_runtime_source = inspect.getsource(inspect.getmodule(RunRuntime)).casefold()
    assert "from .." not in run_runtime_source


def test_base_runner_no_longer_owns_provider_call_behavior():
    source = inspect.getsource(base.BaseRunner)

    assert "_provider_semaphore" not in source
    assert "llm.chat(" not in source
    assert "debug_store.trace_context" not in source
    assert 'usage["prompt_tokens"]' not in source
    assert "hashlib.sha1" not in source
