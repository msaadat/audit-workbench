"""The brief screen states only what it can substantiate.

Its whole purpose is informed consent before an agent starts spending time and
tokens, so the two things that must not slip are the cost line and the "where
does my data go" line. A fabricated number on this screen is worse than no
number, and a wrong locality claim is worse than either.
"""

from __future__ import annotations

import pytest

from app import engagement
from app.agent import store
from app.agent.workflows import audit as audit_workflow
from app.workspaces import WorkspaceError


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #
def test_the_preview_lists_the_real_dependency_closure():
    outcomes = engagement.plan_outcomes()
    listed = [item["capability"] for item in outcomes]
    # Not a hand-written list: it is the closure of the template's outcomes.
    registry_order = audit_workflow.TEMPLATE_OUTCOMES[engagement.DEFAULT_TEMPLATE]
    for capability_id in registry_order:
        assert capability_id in listed
    assert "audit.verified" in listed
    assert len(listed) == len(set(listed))


def test_every_listed_outcome_has_an_auditor_facing_title():
    for item in engagement.plan_outcomes():
        assert item["title"] and item["title"] != item["capability"]
        assert item["stage_id"]


def test_an_unknown_template_is_refused_rather_than_guessed():
    with pytest.raises(WorkspaceError, match="Unknown goal template"):
        engagement.plan_outcomes("not_a_template")


def test_phases_partition_every_outcome_and_keep_run_order():
    phases = engagement.plan_phases()
    outcomes = engagement.plan_outcomes()

    # A phase that swallowed or duplicated a step would put a step count on the
    # screen that does not describe the run.
    grouped = [step["capability"] for phase in phases for step in phase["steps"]]
    assert grouped == [item["capability"] for item in outcomes]

    order = [phase["id"] for phase in engagement.PLAN_PHASES]
    assert [phase["id"] for phase in phases] == [
        item for item in order if item in {phase["id"] for phase in phases}
    ]
    for phase in phases:
        assert phase["title"] and phase["summary"] and phase["steps"]


def test_every_capability_domain_is_placed_in_a_named_phase():
    """A new domain must not be silently folded into an unrelated phase."""
    unplaced = [
        phase for phase in engagement.plan_phases()
        if phase["id"] == engagement._UNGROUPED_PHASE
    ]
    assert not unplaced, (
        "unmapped capability domains: "
        f"{[step['capability'] for phase in unplaced for step in phase['steps']]}"
    )


def test_a_dependency_never_appears_after_the_outcome_that_needs_it():
    order = [item["capability"] for item in engagement.plan_outcomes()]
    position = {capability: index for index, capability in enumerate(order)}
    for capability, dependencies in audit_workflow.DEPENDENCIES.items():
        if capability not in position:
            continue
        for dependency in dependencies:
            assert position[dependency] < position[capability], (
                f"{dependency} is listed after {capability}, which depends on it"
            )


# --------------------------------------------------------------------------- #
# The cost line
# --------------------------------------------------------------------------- #
def test_with_no_history_the_estimate_refuses_to_produce_a_number(monkeypatch):
    monkeypatch.setattr(engagement, "_comparable_runs", lambda actor=None: [])
    estimate = engagement.cost_estimate()

    assert estimate["state"] == "insufficient_history"
    assert estimate["runs_observed"] == 0
    assert estimate["reason"]
    # The screen must have nothing to render as a duration.
    assert "median_minutes" not in estimate
    assert "median_model_calls" not in estimate


def test_one_run_is_not_enough_to_estimate_from(monkeypatch):
    monkeypatch.setattr(engagement, "_comparable_runs", lambda actor=None: [
        {"usage": {"llm_turns": 100}, "duration_ms": 600_000},
    ])
    assert engagement.cost_estimate()["state"] == "insufficient_history"


def test_an_estimate_reports_the_median_and_the_worst_case(monkeypatch):
    monkeypatch.setattr(engagement, "_comparable_runs", lambda actor=None: [
        {"usage": {"llm_turns": 100}, "duration_ms": 600_000},    # 10 min
        {"usage": {"llm_turns": 200}, "duration_ms": 1_200_000},  # 20 min
        {"usage": {"llm_turns": 150}, "duration_ms": 1_800_000},  # 30 min
    ])
    estimate = engagement.cost_estimate()

    assert estimate["state"] == "measured"
    assert estimate["runs_observed"] == 3
    assert estimate["median_model_calls"] == 150
    assert estimate["median_minutes"] == 20.0
    assert estimate["slowest_minutes"] == 30.0
    # A median of past runs is not a promise about this engagement.
    assert estimate["caveat"]


def test_runs_that_did_not_finish_or_did_no_work_are_excluded(workspace_with_data):
    """Duration on a halted run is wall-clock, including time spent waiting.

    An interrupted run in this repo reads as 24 hours because it sat waiting
    for a person overnight. Counting it would measure the auditor, not the
    agent — and a run that reused everything and called no model measures
    nothing at all.
    """
    assert "interrupted" not in engagement._COMPLETED
    assert "paused" not in engagement._COMPLETED
    assert "failed" not in engagement._COMPLETED
    assert "cancelled" not in engagement._COMPLETED
    assert set(engagement._COMPLETED) <= set(store.TERMINAL_STATUSES)

    # No workflow run exists in a bare workspace, so nothing qualifies.
    assert engagement._comparable_runs() == []


# --------------------------------------------------------------------------- #
# The destination line
# --------------------------------------------------------------------------- #
def test_a_cloud_provider_is_never_described_as_staying_on_this_machine(monkeypatch):
    monkeypatch.setattr(engagement.llm, "agent_status", lambda: {
        "configured": True, "provider": "openrouter",
        "model": "some/model", "base_url": "https://openrouter.ai/api/v1",
    })
    destination = engagement.destination()

    assert destination["local"] is False
    assert "openrouter" in destination["summary"]
    assert "nothing leaves" not in destination["summary"].casefold()


def test_a_local_model_is_reported_as_local(monkeypatch):
    monkeypatch.setattr(engagement.llm, "agent_status", lambda: {
        "configured": True, "provider": "lmstudio",
        "model": "local/model", "base_url": "http://localhost:1234/v1",
    })
    destination = engagement.destination()
    assert destination["local"] is True
    assert "machine" in destination["summary"]


def test_an_unconfigured_model_says_the_agent_cannot_run(monkeypatch):
    monkeypatch.setattr(engagement.llm, "agent_status", lambda: {
        "configured": False, "provider": "", "model": "", "base_url": "",
    })
    destination = engagement.destination()
    assert destination["configured"] is False
    assert "cannot run" in destination["summary"]


# --------------------------------------------------------------------------- #
# The brief itself
# --------------------------------------------------------------------------- #
def test_a_brief_does_not_require_an_objective_or_scope(workspace_with_data):
    from app.agent.capabilities import REGISTRY_BY_WORKFLOW

    capability = REGISTRY_BY_WORKFLOW[audit_workflow.WORKFLOW_ID].get("planning.context_ready")
    assert capability.readiness(workspace_with_data, {}).state != "satisfied"

    engagement.apply_brief(workspace_with_data, {
        "entity": "Global Bank",
    })

    assert capability.readiness(workspace_with_data, {}).state == "satisfied"
    context = workspace_with_data.planning["context"]
    assert context["entity"] == "Global Bank"


def test_an_empty_brief_leaves_planning_untouched(workspace_with_data):
    before = dict(workspace_with_data.planning["context"])
    engagement.apply_brief(workspace_with_data, {"entity": "   ", "period": ""})
    assert workspace_with_data.planning["context"] == before


def test_a_brief_cannot_write_fields_outside_the_declared_set(workspace_with_data):
    engagement.apply_brief(workspace_with_data, {
        "entity": "Real entity.",
        "objective": "Ignored legacy field.",
        "scope": "Ignored legacy field.",
        "created_by": "agent",
        "apm_markdown": "# Injected",
    })
    context = workspace_with_data.planning["context"]
    assert context["entity"] == "Real entity."
    assert not context["objective"]
    assert not context["scope"]
    assert "apm_markdown" not in context
    # Provenance stays workbench-managed; a brief is an auditor edit.
    assert workspace_with_data.planning.get("created_by") != "agent"


def test_gates_describe_the_launch_mode_that_was_asked_for():
    assert engagement.gates("permission")["mode"] == "permission"
    assert "approval" in engagement.gates("permission")["summary"]
    assert engagement.gates("auto")["mode"] == "auto"
    assert engagement.gates("anything-else")["mode"] == "auto"
