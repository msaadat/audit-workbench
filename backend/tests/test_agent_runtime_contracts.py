import inspect
import json
import re
from typing import Any

from app import documents, llm
from app.agent import base, runner, store
from app.agent.runtime import ModelGateway, RunRuntime


class _ActiveModelGateway:
    """Expose the still-active BaseRunner behavior through the target contract."""

    def __init__(self, active_runner: base.BaseRunner):
        self._runner = active_runner

    def complete(
        self,
        system: str,
        user: str,
        activity: dict[str, Any] | None = None,
        *,
        attempt: int = 1,
    ) -> str:
        return self._runner._llm_content(
            system,
            user,
            activity,
            attempt=attempt,
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

    def fake_chat(messages, **_kwargs):
        calls.append(messages)
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
    gateway = _ActiveModelGateway(active)
    system = "[agent:contract_test]\nSENSITIVE_SYSTEM_PROMPT"
    user = "SENSITIVE_USER_PAYLOAD"

    assert isinstance(gateway, ModelGateway)
    assert gateway.complete(system, user) == "SENSITIVE_PROVIDER_RESPONSE"
    assert calls == [[
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]]

    usage = store.load_run(workspace_with_data, active.run["id"])["usage"]
    assert usage["llm_turns"] == 1
    assert usage["prompt_tokens"] == 17
    assert usage["completion_tokens"] == 5
    assert usage["model_calls_by_worker"] == {"agent:contract_test": 1}

    activity = documents.activities(workspace_with_data)["items"][-1]
    serialized = json.dumps(activity)
    assert activity["prompt_version"]
    assert activity["response_hash"]
    assert "SENSITIVE_SYSTEM_PROMPT" not in serialized
    assert "SENSITIVE_USER_PAYLOAD" not in serialized
    assert "SENSITIVE_PROVIDER_RESPONSE" not in serialized


def test_runtime_contract_modules_are_behavior_free_and_domain_neutral():
    assert getattr(ModelGateway, "_is_protocol", False)
    assert getattr(RunRuntime, "_is_protocol", False)

    for contract in (ModelGateway, RunRuntime):
        source = inspect.getsource(inspect.getmodule(contract)).casefold()
        assert "from .." not in source
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
