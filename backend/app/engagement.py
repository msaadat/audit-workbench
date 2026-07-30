"""What the agent would do with a new engagement, before it does any of it.

Creating a workspace used to ask for a name and drop the auditor on an empty
dashboard. The one thing the agent genuinely cannot infer is *what are we
auditing and why* — everything else it can read off the folder — so that is
what the brief collects, and this module answers the two questions an auditor
asks before letting it start: what will you do, and what will it cost?

The cost answer is measured, never guessed. It comes from runs already recorded
on this machine, and when there are none it says so rather than inventing a
number, because a fabricated estimate on a screen whose whole purpose is
informed consent is worse than no estimate.
"""

from __future__ import annotations

import statistics
from typing import Any

from . import llm, workspaces
from .agent import store
from .agent.capabilities import REGISTRY_BY_WORKFLOW
from .agent.workflows import audit as audit_workflow
from .workspaces import Workspace, WorkspaceError

# The goal template a brief starts. Named here so the preview and the launch
# cannot drift apart.
DEFAULT_TEMPLATE = "full_audit_working_draft"

# A run is comparable when it asked for the whole audit. Anything narrower
# would understate what a fresh engagement costs.
_FULL_AUDIT_MARKER = "audit.verified"

# Only runs that finished. An interrupted or paused run's `duration_ms` is
# wall-clock including however long it sat waiting for a person — one of them
# in this repo reads as 24 hours — which measures the auditor's lunch break,
# not the agent's work. Failed and cancelled runs stopped early and would pull
# the estimate the other way.
_COMPLETED = ("completed", "completed_with_open_items", "completed_with_issues")

# Enough runs to have a middle, few enough that one bad run cannot dominate.
_MIN_RUNS_FOR_ESTIMATE = 2
_MAX_RUNS_SCANNED = 40

# The context fields a brief fills. `objective` and `scope` are exactly what
# `planning.context_ready` requires, so a completed brief means the agent can
# skip the interview and start from planning.
BRIEF_FIELDS = ("objective", "entity", "period", "scope", "materiality", "background_notes")


def plan_outcomes(template: str = DEFAULT_TEMPLATE) -> list[dict[str, str]]:
    """The capabilities a template resolves to, in the order they would run."""
    requested = audit_workflow.outcomes_for_template(template)
    if requested is None:
        raise WorkspaceError(f"Unknown goal template '{template}'.")
    registry = REGISTRY_BY_WORKFLOW[audit_workflow.WORKFLOW_ID]
    outcomes = []
    for capability_id in registry.closure(requested):
        try:
            capability = registry.get(capability_id)
        except (KeyError, AttributeError):
            continue
        outcomes.append({
            "capability": capability_id,
            "title": capability.title,
            "stage_id": capability.stage_id,
        })
    return outcomes


def _comparable_runs() -> list[dict]:
    """Completed full-audit runs that actually did work.

    Every filter here removes a run that would mislead rather than inform. A
    narrower run measures a fraction of the graph; a run that reused everything
    and called no model measures nothing at all. Extrapolating a whole audit
    from either is how a number that looks measured ends up being invented.
    """
    comparable: list[dict] = []
    scanned = 0
    for entry in workspaces.list_workspaces():
        try:
            workspace = workspaces.load_workspace(str(entry.get("id")))
        except WorkspaceError:
            continue
        for summary in store.list_runs(workspace):
            scanned += 1
            if summary.get("engine") != "workflow":
                continue
            if summary.get("status") not in _COMPLETED:
                continue
            if not summary.get("duration_ms"):
                continue
            if _FULL_AUDIT_MARKER not in (summary.get("requested_outcomes") or []):
                continue
            if not int((summary.get("usage") or {}).get("llm_turns") or 0):
                continue
            comparable.append(summary)
        if scanned >= _MAX_RUNS_SCANNED:
            break
    return comparable


def cost_estimate() -> dict[str, Any]:
    """How long past full audits took, measured — or a refusal to guess."""
    runs = _comparable_runs()
    if len(runs) < _MIN_RUNS_FOR_ESTIMATE:
        return {
            "state": "insufficient_history",
            "runs_observed": len(runs),
            "reason": (
                "No completed full audit has run on this machine yet, so there is "
                "nothing to measure from. This engagement will be the first."
            ),
        }
    calls = [int((run.get("usage") or {}).get("llm_turns") or 0) for run in runs]
    minutes = [float(run.get("duration_ms") or 0) / 60_000 for run in runs]
    return {
        "state": "measured",
        "runs_observed": len(runs),
        "basis": "completed full-audit runs on this machine",
        "median_model_calls": int(statistics.median(calls)),
        "median_minutes": round(statistics.median(minutes), 1),
        "slowest_minutes": round(max(minutes), 1),
        # Past runs measure this machine and this model, not this engagement's
        # size. Saying so keeps the number from reading as a promise.
        "caveat": (
            "Measured from past runs, not from this engagement — a larger folder "
            "or a slower model will take longer."
        ),
    }


def destination() -> dict[str, Any]:
    """Where this engagement's model requests would actually go."""
    status = llm.agent_status()
    provider = str(status.get("provider") or status.get("backend") or "")
    base_url = str(status.get("base_url") or "")
    local = bool(provider == "lmstudio" or "localhost" in base_url or "127.0.0.1" in base_url)
    return {
        "configured": bool(status.get("configured")),
        "provider": provider,
        "model": str(status.get("model") or ""),
        "local": local,
        # The mockup's "nothing leaves this machine" is only true of a local
        # model. Stating the real destination is the entire point of the line.
        "summary": (
            "Nothing leaves this machine."
            if local
            else f"Requests and bounded result previews go to {provider}."
            if provider
            else "No model is configured, so the agent cannot run yet."
        ),
    }


def gates(mode: str) -> dict[str, Any]:
    """Where the agent stops for the auditor under a launch mode."""
    normalized = "permission" if str(mode) == "permission" else "auto"
    return {
        "mode": normalized,
        "summary": (
            "I'll stop for your approval before committing any change to the engagement file."
            if normalized == "permission"
            else "I'll apply changes as I go, and still stop when something genuinely needs you."
        ),
    }


def plan_preview(template: str = DEFAULT_TEMPLATE, mode: str = "auto") -> dict[str, Any]:
    """Everything the brief screen states before the auditor approves."""
    return {
        "template": template,
        "outcomes": plan_outcomes(template),
        "estimate": cost_estimate(),
        "destination": destination(),
        "gates": gates(mode),
    }


def apply_brief(workspace: Workspace, brief: dict[str, Any]) -> dict[str, Any]:
    """Record the brief as planning context.

    A brief is not a new concept: `objective`, `scope`, `period` and the rest
    are the planning-context fields the assistant interview would otherwise
    collect. Writing them here means `planning.context_ready` is satisfied from
    the start and the agent begins at the memorandum instead of an interview.
    """
    context = {
        field: str(brief.get(field) or "").strip()
        for field in BRIEF_FIELDS
        if str(brief.get(field) or "").strip()
    }
    if not context:
        return workspace.planning
    return workspace.update_planning({"context": context})
