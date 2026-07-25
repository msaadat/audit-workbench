"""Run-process behavior: launch, explicit-engine dispatch, recovery, control.

This module tests :mod:`app.agent.runner` as the process layer it now is —
record creation, the one-live-run rule, engine-only dispatch, terminal-state
guarantees, orphan recovery, and resume through the public control surface.
Scheduling behavior belongs to the engines and is tested with them.

Phase 12 retired the fixed-stage v1 analysis pipeline, so the vehicles here are
the surviving engines: a command run for the action scheduler, and the retained
folder-intake protocol run. The provider-accounting and provider-concurrency
characterizations moved onto `ActionRunner` unchanged in substance; they always
described the shared gateway, not the deleted runner.
"""

import hashlib
import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import app.agent as agent_package
from app import documents, intake, llm, workspaces
from app.agent import (
    action_runner,
    base,
    intake_runner,
    ledger,
    runner,
    store,
    workflow_dispatch,
)
from conftest import wait_run


POLICY_TEXT = b"Procurement policy requires approval before commitment.\n"


def _command_run(ws, text="run a bounded action", mode="auto"):
    """A durable command record without launching a worker thread."""
    return store.new_command_run(ws, mode, {"source": "chat", "text": text})


def _scheduler(ws, run):
    return action_runner.ActionRunner(ws, run, runner.RunHandle(ws.id, run["id"]))


def _intake_batch(ws, mode="auto", relative_path="Audit/guidance.txt"):
    source = intake.create_source(ws, "Audit folder", "Client Audit")
    staged = intake.compare_manifest(
        ws,
        source["id"],
        [{"relative_path": relative_path, "size": len(POLICY_TEXT), "last_modified": 1}],
        mode,
    )["batch"]
    item = intake.requested_item(staged, relative_path)
    target = intake.staging_path(ws, staged, item)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(POLICY_TEXT)
    intake.mark_uploaded(
        ws, staged, item, hashlib.sha1(POLICY_TEXT).hexdigest(), len(POLICY_TEXT), target
    )
    return source, intake.complete_upload(ws, staged["id"])


def _start_intake(ws, source, batch, mode="auto"):
    return runner.start_run(
        ws, mode, {"batch_id": batch["id"], "source_id": source["id"]}, kind="intake"
    )


def _pending_approval(ws, run_id, timeout=15.0):
    """Wait until the run parks on a pending approval or reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = store.load_run(ws, run_id)
        pending = [a for a in current.get("approvals") or [] if a["status"] == "pending"]
        if pending:
            return current, pending[0]
        if current["status"] in store.TERMINAL_STATUSES:
            return current, None
        time.sleep(0.02)
    raise AssertionError(f"Run stuck in {current['status']} without a pending approval")


# ------------------------------------------------------------ run creation
def test_start_run_creates_only_the_retained_protocol_run(workspace_with_data):
    with pytest.raises(workspaces.WorkspaceError, match="run kind"):
        runner.start_run(workspace_with_data, "auto", {}, kind="analysis")


def test_command_run_requires_a_configured_model(workspace_with_data, monkeypatch):
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})
    with pytest.raises(llm.LLMError, match="not configured"):
        runner.start_command_run(
            workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
        )


# --------------------------------------------------------- provider accounting
def test_model_gateway_charges_budgets_and_persists_hash_only_provenance(
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
        lambda: {"configured": True, "provider": "accounting-test", "model": "model"},
    )
    run = _command_run(workspace_with_data)
    run["limits"] = {
        "max_model_turns": 1,
        "max_estimated_prompt_tokens": 1_000,
        "max_completion_tokens": 100,
        "max_llm_concurrency": 1,
    }
    store.save_run(workspace_with_data, run)
    scheduler = _scheduler(workspace_with_data, run)
    system = "[agent:accounting_test]\nSENSITIVE_SYSTEM_PROMPT"
    user = "SENSITIVE_USER_PAYLOAD"

    assert scheduler._llm_content(system, user) == "SENSITIVE_PROVIDER_RESPONSE"

    usage = store.load_run(workspace_with_data, run["id"])["usage"]
    assert usage["llm_turns"] == 1
    assert usage["request_characters"] == len(system) + len(user)
    assert usage["prompt_tokens"] == 17
    assert usage["completion_tokens"] == 5
    assert usage["model_calls_by_worker"] == {"agent:accounting_test": 1}
    assert usage["model_call_metrics"][0]["estimated_input_tokens"] == max(
        1, (len(system) + len(user)) // 4
    )
    activity = documents.activities(workspace_with_data)["items"][-1]
    serialized = json.dumps(activity)
    assert activity["prompt_version"]
    assert activity["response_hash"]
    assert "SENSITIVE_SYSTEM_PROMPT" not in serialized
    assert "SENSITIVE_USER_PAYLOAD" not in serialized
    assert "SENSITIVE_PROVIDER_RESPONSE" not in serialized

    with pytest.raises(base.LimitExceeded, match="model turn limit"):
        scheduler._llm_content(system, user)
    assert len(calls) == 1


def test_provider_concurrency_is_shared_and_bounded_across_runs(
    workspace_with_data, monkeypatch
):
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_chat(_messages, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return {"content": "ok", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        finally:
            with lock:
                active -= 1

    monkeypatch.setenv("AGENT_PROVIDER_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {
            "configured": True,
            "provider": "shared-concurrency-characterization",
            "model": "one-at-a-time",
        },
    )
    schedulers = []
    for index in range(2):
        run = _command_run(workspace_with_data, text=f"concurrency {index}")
        run["limits"] = {
            "max_model_turns": 2,
            "max_estimated_prompt_tokens": 100,
            "max_completion_tokens": 10,
            "max_llm_concurrency": 2,
        }
        store.save_run(workspace_with_data, run)
        schedulers.append(_scheduler(workspace_with_data, run))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda scheduler: scheduler._llm_content(
                    "[agent:concurrency_test]", "bounded request"
                ),
                schedulers,
            )
        )

    assert results == ["ok", "ok"]
    assert peak == 1
# ------------------------------------------------ explicit engine dispatch
def test_action_runner_has_no_legacy_module_or_class_alias():
    assert importlib.util.find_spec("app.agent.command_runner") is None
    assert not hasattr(action_runner, "CommandRunner")
    assert not hasattr(agent_package, "CommandRunner")


def test_old_workflow_scheduler_module_was_deleted_without_an_adapter():
    assert importlib.util.find_spec("app.agent.workflow_runner") is None


def test_phase_one_has_no_v2_reader_alias_or_compatibility_module(
    workspace_with_data,
):
    package_dir = Path(action_runner.__file__).parent
    forbidden_paths = {
        path
        for path in package_dir.iterdir()
        if path.name == "command_runner.py" or "compat" in path.name.casefold()
    }
    assert not forbidden_paths

    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "run a bounded action"},
    )
    run.pop("engine")
    store.save_run(workspace_with_data, run)

    loaded = store.load_run(workspace_with_data, run["id"])
    assert "engine" not in loaded
    assert store.run_summary(loaded)["engine"] is None


@pytest.mark.parametrize(
    ("engine", "module", "class_name", "expected"),
    [
        (
            store.WORKFLOW_ENGINE,
            workflow_dispatch,
            "build_workflow_runner",
            "workflow",
        ),
        (store.ACTION_ENGINE, action_runner, "ActionRunner", "action"),
        (store.INTAKE_ENGINE, intake_runner, "IntakeRunner", "intake"),
    ],
)
def test_execute_dispatches_by_explicit_engine_only(
    workspace_with_data, monkeypatch, engine, module, class_name, expected
):
    called = []

    class StubRunner:
        def __init__(self, _workspace, _run, _handle):
            pass

        def execute(self):
            called.append(expected)

    run = store.new_run(workspace_with_data, "auto")
    run["engine"] = engine
    run["kind"] = "deliberately-not-a-dispatch-key"
    store.save_run(workspace_with_data, run)
    monkeypatch.setattr(module, class_name, StubRunner)

    runner._execute(
        workspace_with_data.id,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    assert called == [expected]


@pytest.mark.parametrize(
    ("engine", "label"),
    [
        (None, "missing"),
        ("v2", "unsupported"),
        # The retired v1 engine value is not a dispatch key any more; a record
        # carrying it is a pre-cutover shape and fails closed like any other.
        ("analysis", "unsupported"),
    ],
)
def test_execute_fails_closed_without_a_supported_explicit_engine(
    workspace_with_data, engine, label
):
    run = store.new_run(workspace_with_data, "auto", kind="intake")
    if engine is None:
        run.pop("engine")
    else:
        run["engine"] = engine
    store.save_run(workspace_with_data, run)

    runner._execute(
        workspace_with_data.id,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    failed = store.load_run(workspace_with_data, run["id"])
    assert failed["status"] == "failed"
    assert "engine" in failed["error"]
    assert label in failed["error"]


def test_terminal_crash_still_launches_the_next_queued_follow_up(
    workspace_with_data,
    monkeypatch,
):
    queued = {
        "id": "follow-up-1",
        "source": "chat",
        "text": "then review the report",
        "run_context": {"document_ids": ["DOC-1"]},
    }
    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "run the first action"},
    )
    run["engine"] = store.ACTION_ENGINE
    run["status"] = "executing"
    run["pending_commands"] = [queued]
    store.save_run(workspace_with_data, run)
    launched = []

    class CrashingActionRunner:
        def __init__(self, _workspace, _run, _handle):
            pass

        def execute(self):
            raise RuntimeError("phase-three crash")

    def capture_start(workspace, mode, command, parent_run_id=None, context=None):
        launched.append(
            {
                "workspace_id": workspace.id,
                "mode": mode,
                "command": command,
                "parent_run_id": parent_run_id,
                "context": context,
            }
        )
        return {"id": "captured-follow-up"}

    monkeypatch.setattr(action_runner, "ActionRunner", CrashingActionRunner)
    monkeypatch.setattr(runner, "start_command_run", capture_start)

    runner._execute(
        workspace_with_data.id,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    failed = store.load_run(workspace_with_data, run["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "phase-three crash"
    assert failed["finished"]
    assert failed["pending_commands"] == []
    assert store.read_events(workspace_with_data, run["id"])[-1]["data"] == {
        "status": "failed",
        "error": "phase-three crash",
    }
    assert len(launched) == 1
    assert launched[0]["workspace_id"] == workspace_with_data.id
    assert launched[0]["mode"] == "auto"
    assert launched[0]["command"]["id"] == queued["id"]
    assert launched[0]["command"]["text"] == queued["text"]
    assert launched[0]["parent_run_id"] == run["id"]
    assert launched[0]["context"] == {"document_ids": ["DOC-1"]}


# -------------------------------------------------------- interruption
def test_same_schema_orphan_recovery_preserves_durable_checkpoints(workspace_with_data):
    cases = {}

    queued = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "queued command"}
    )
    cases[queued["id"]] = {"status": "queued", "checkpoint": queued["command"]}

    partial = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "partially committed command"}
    )
    committed, in_flight = ledger.append_actions(
        partial,
        [
            {"id": "committed", "type": "run_report_quality", "args": {}},
            {
                "id": "in-flight",
                "type": "run_analytics",
                "args": {
                    "table": "transactions",
                    "test": "duplicates",
                    "params": {"columns": ["invoice_no"]},
                },
                "depends_on": ["committed"],
            },
        ],
    )
    ledger.transition(committed, "ready")
    ledger.transition(committed, "running")
    ledger.transition(committed, "succeeded")
    ledger.transition(in_flight, "ready")
    ledger.transition(in_flight, "running")
    partial["status"] = "executing"
    store.save_run(workspace_with_data, partial)
    cases[partial["id"]] = {
        "status": "executing",
        "checkpoint": [action["status"] for action in partial["actions"]],
    }

    approval = store.new_command_run(
        workspace_with_data, "permission", {"source": "chat", "text": "approval command"}
    )
    approval["status"] = "awaiting_approval"
    approval["approvals"] = [
        {"id": "approval-1", "kind": "mutations", "status": "pending", "items": []}
    ]
    store.save_run(workspace_with_data, approval)
    cases[approval["id"]] = {
        "status": "awaiting_approval",
        "checkpoint": approval["approvals"],
    }

    interaction = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "interaction command"}
    )
    interaction["status"] = "awaiting_input"
    interaction["interactions"] = [
        {
            "id": "interaction-1",
            "action_id": "missing-target",
            "type": "clarification",
            "status": "pending",
            "prompt": "Which artifact?",
        }
    ]
    store.save_run(workspace_with_data, interaction)
    cases[interaction["id"]] = {
        "status": "awaiting_input",
        "checkpoint": interaction["interactions"],
    }

    provider = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "provider command"}
    )
    provider["status"] = "interpreting"
    provider["usage"].update(llm_turns=1, estimated_prompt_tokens=321)
    store.save_run(workspace_with_data, provider)
    cases[provider["id"]] = {
        "status": "interpreting",
        "checkpoint": provider["usage"],
    }

    completed = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "completed command"}
    )
    completed.update(status="completed", finished=store.utcnow(), summary_markdown="Done")
    store.save_run(workspace_with_data, completed)

    recovered = runner.recover_workspace(workspace_with_data)

    assert set(recovered) == set(cases)
    for run_id, expected in cases.items():
        durable = store.load_run(workspace_with_data, run_id)
        assert durable["status"] == "interrupted"
        if expected["status"] == "queued":
            checkpoint = durable["command"]
        elif expected["status"] == "executing":
            checkpoint = [action["status"] for action in durable["actions"]]
        elif expected["status"] == "awaiting_approval":
            checkpoint = durable["approvals"]
        elif expected["status"] == "awaiting_input":
            checkpoint = durable["interactions"]
        else:
            checkpoint = durable["usage"]
        assert checkpoint == expected["checkpoint"]
        assert "prepared_planning" not in durable
        assert store.read_events(workspace_with_data, run_id)[-1] == {
            "seq": 1,
            "at": store.read_events(workspace_with_data, run_id)[-1]["at"],
            "type": "run_status",
            "data": {"status": "interrupted"},
        }

    durable_completed = store.load_run(workspace_with_data, completed["id"])
    assert durable_completed["status"] == "completed"
    assert durable_completed["summary_markdown"] == "Done"
    assert store.read_events(workspace_with_data, completed["id"]) == []


def test_interrupted_run_can_resume_to_completion(fake_agent_llm):
    ws = workspaces.create_workspace("Resume engagement")
    source, batch = _intake_batch(ws, mode="permission")
    fake_agent_llm.overrides["agent:file_classification"] = {
        "items": [
            {
                "id": batch["items"][0]["id"],
                "route": "document",
                "document_category": "policy",
                "confidence": "high",
                "rationale": "The filename indicates audit guidance.",
                "proposed_action": "import",
            }
        ]
    }
    run = _start_intake(ws, source, batch, mode="permission")
    _pending_approval(ws, run["id"])
    # Simulate a process crash: kill the worker, then rewrite the active
    # status a real crash would have left behind.
    handle = runner.get_handle(run["id"])
    handle.cancel.set()
    handle.thread.join(timeout=5)
    state = store.load_run(ws, run["id"])
    state["status"] = "awaiting_approval"
    state["finished"] = None
    store.save_run(ws, state)
    assert runner.recover_workspace(ws) == [run["id"]]
    assert store.load_run(ws, run["id"])["status"] == "interrupted"

    runner.resume_run(ws, run["id"])
    resumed, approval = _pending_approval(ws, run["id"])
    assert approval is not None
    # The pending approval survived and is re-served, not re-proposed.
    assert len([a for a in resumed["approvals"] if a["status"] == "pending"]) == 1
    runner.resolve_approval(
        ws,
        run["id"],
        approval["id"],
        [{"item_id": item["id"], "action": "approve"} for item in approval["items"]],
    )
    done = wait_run(ws, run["id"])
    assert done["status"] == "completed"


# ------------------------------------------------------- terminal follow-ups
def test_a_finished_intake_run_follows_up_as_a_command_run(monkeypatch):
    """Text sent to a finished protocol run starts a command run, not a replay."""
    ws = workspaces.create_workspace("Follow-up engagement")
    run = store.new_run(ws, "auto", {"batch_id": "batch-1"}, kind="intake")
    run["status"] = "completed"
    run["finished"] = store.utcnow()
    store.save_run(ws, run)
    launched = {}

    def capture(workspace, mode, command, parent_run_id=None, context=None):
        launched.update(command=command, parent_run_id=parent_run_id)
        return {"id": "captured-follow-up"}

    monkeypatch.setattr(runner, "start_command_run", capture)

    result = runner.steer(ws, run["id"], "Now analyze the imported tables")

    assert result["handled"] == "follow_up_run"
    assert launched["parent_run_id"] == run["id"]
    assert launched["command"]["text"] == "Now analyze the imported tables"


def test_a_finished_record_without_a_supported_engine_is_not_replayed():
    """A pre-cutover terminal record fails closed instead of being resumed."""
    ws = workspaces.create_workspace("Pre-cutover engagement")
    run = store.new_run(ws, "auto", {"objective": "revenue audit"}, kind="intake")
    run["engine"] = "analysis"  # the retired v1 engine value
    run["status"] = "completed"
    run["finished"] = store.utcnow()
    store.save_run(ws, run)

    with pytest.raises(workspaces.WorkspaceError, match="supported agent run schema"):
        runner.steer(ws, run["id"], "Also check round numbers")
