"""Step 6c: a stage checkpoint the auditor can answer before work is spent.

Permission mode already approves each proposal. This approves each *stage*,
before its first model turn — the cheapest place to stop work nobody wanted.
It is off unless the run is in permission mode *and* its context asked for it,
so every existing run is unaffected.
"""

from __future__ import annotations

import pytest

from app import workspaces
from app.agent import audit_execution, runner, store, workflow
from app.agent.audit_execution import build_audit_workflow_runner
from app.agent.runtime import Cancelled


def _run(workspace, *, mode="permission", context=None) -> dict:
    from app.agent.routing import resolve_route

    run = store.new_command_run(
        workspace,
        mode,
        {"source": "chat", "text": "Draft the APM", "requested_outcomes": ["planning.apm_ready"]},
        context=context or {},
    )
    assert resolve_route(workspace, run) == "workflow"
    return store.load_run(workspace, run["id"])


def _stage(status="queued", units=2) -> dict:
    return {
        "id": "stage-apm",
        "capability": "planning.apm_ready",
        "title": "Audit planning memorandum",
        "status": status,
        "units": [
            {"id": f"apm:{index}", "status": "queued", "parent_refs": []}
            for index in range(units)
        ],
    }


def _adapter(workspace, run, monkeypatch, response):
    scheduler = build_audit_workflow_runner(
        workspace, run, runner.RunHandle(workspace.id, run["id"])
    )
    adapter = scheduler.execution_adapter
    asked = []

    def answer(interaction, **_kwargs):
        asked.append(interaction)
        return response

    monkeypatch.setattr(adapter.runtime, "wait_for_interaction", answer)
    monkeypatch.setattr(adapter.runtime, "resolve_interaction", lambda *_a, **_k: None)
    return adapter, asked


@pytest.fixture
def workspace():
    return workspaces.create_workspace("Stage review")


def test_continue_runs_the_stage_and_says_what_is_next(workspace, monkeypatch):
    run = _run(workspace, context={"review_each_stage": True})
    adapter, asked = _adapter(workspace, run, monkeypatch, {"text": "continue"})
    capability = adapter_capability()
    stage = _stage()

    decision = audit_execution.stage_review(adapter, capability, stage)

    assert decision == "continue"
    assert asked[0]["type"] == "stage_review"
    assert asked[0]["options"] == ["continue", "skip", "stop"]
    assert "2 items" in asked[0]["prompt"]
    assert asked[0]["payload"]["capability"] == "planning.apm_ready"
    assert [unit["status"] for unit in stage["units"]] == ["queued", "queued"]


def test_skip_settles_the_stages_units_without_running_them(workspace, monkeypatch):
    run = _run(workspace, context={"review_each_stage": True})
    adapter, _asked = _adapter(workspace, run, monkeypatch, {"choice": "skip"})
    stage = _stage()

    decision = audit_execution.stage_review(adapter, adapter_capability(), stage)

    assert decision == "skip"
    assert [unit["status"] for unit in stage["units"]] == ["skipped", "skipped"]
    assert stage["review_decision"] == "skip"


def test_stop_cancels_the_run(workspace, monkeypatch):
    run = _run(workspace, context={"review_each_stage": True})
    adapter, _asked = _adapter(workspace, run, monkeypatch, {"text": "stop"})

    with pytest.raises(Cancelled):
        audit_execution.stage_review(adapter, adapter_capability(), _stage())


def test_an_auto_run_never_waits_and_neither_does_one_that_did_not_ask(
    workspace, monkeypatch
):
    auto = _run(workspace, mode="auto", context={"review_each_stage": True})
    adapter, asked = _adapter(workspace, auto, monkeypatch, {"text": "stop"})
    assert audit_execution.stage_review(adapter, adapter_capability(), _stage()) == "continue"

    unasked = _run(workspace, mode="permission")
    adapter, asked_two = _adapter(workspace, unasked, monkeypatch, {"text": "stop"})
    assert audit_execution.stage_review(adapter, adapter_capability(), _stage()) == "continue"

    assert asked == [] and asked_two == []


def test_a_stage_with_no_units_is_not_worth_asking_about(workspace, monkeypatch):
    run = _run(workspace, context={"review_each_stage": True})
    adapter, asked = _adapter(workspace, run, monkeypatch, {"text": "stop"})

    assert audit_execution.stage_review(
        adapter, adapter_capability(), _stage(units=0)
    ) == "continue"
    assert asked == []


@pytest.mark.parametrize(
    ("response", "decision"),
    [
        ({"text": "Continue please"}, "continue"),
        ({"option": "SKIP"}, "skip"),
        ({"options": ["stop"]}, "stop"),
        ({"decision": "skip it"}, "skip"),
        # Anything unrecognized continues: permission mode still approves each
        # proposal, so continuing asks again rather than acting unasked, while
        # skipping or stopping would discard work nobody declined.
        ({"text": "what is this?"}, "continue"),
        ({}, "continue"),
    ],
)
def test_the_answer_is_read_out_of_whatever_shape_came_back(response, decision):
    assert audit_execution._stage_review_decision(response) == decision


def adapter_capability():
    from app.agent import capabilities as audit_capabilities

    return audit_capabilities.REGISTRY.get("planning.apm_ready")
