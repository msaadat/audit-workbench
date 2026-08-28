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

# Optional engagement details captured at workspace creation. Planning derives
# its objective and scope from the available engagement material instead of
# requiring either as an upfront field.
BRIEF_FIELDS = ("entity", "period", "materiality", "background_notes")


# The capabilities a full audit resolves to are the pipeline's own vocabulary —
# "Join utility selection", "Materialized joins", "Document chunk analysis". As a
# flat numbered list on the brief screen they read as a build log, so the preview
# also groups them into the phases an auditor recognises. The capability domain
# already partitions the graph in run order, so this is a rename rather than a
# second ordering that could drift out of step with the first.
_PHASE_OF_DOMAIN = {
    "data": "sources",
    "analysis": "sources",
    "documents": "documents",
    "planning": "planning",
    "tests": "planning",
    "fieldwork": "fieldwork",
    "results": "fieldwork",
    "findings": "fieldwork",
    "working_papers": "writeup",
    "dashboard": "writeup",
    "report": "writeup",
    "audit": "writeup",
}

# Ordered. A domain this table does not name lands in the trailing catch-all
# rather than being folded into whichever phase happens to be last — a new
# capability should look unplaced here, not quietly mislabelled.
_UNGROUPED_PHASE = "other"
PLAN_PHASES: tuple[dict[str, str], ...] = (
    {
        "id": "sources",
        "title": "Understand the data",
        "summary": "Profile the tables, work out how they join, and summarise what the population shows.",
    },
    {
        "id": "documents",
        "title": "Read the documents",
        "summary": "Extract the text, analyse each source, and keep every citation anchored to its page.",
    },
    {
        "id": "planning",
        "title": "Plan the engagement",
        "summary": "Draft the planning memorandum, build the risk and control matrix, and specify the tests.",
    },
    {
        "id": "fieldwork",
        "title": "Do the fieldwork",
        "summary": "Run the tests, record results and observations, and draft findings where the evidence supports one.",
    },
    {
        "id": "writeup",
        "title": "Write it up",
        "summary": "Produce the working papers, curate the dashboard, and draft the report with its quality checks.",
    },
    {
        "id": _UNGROUPED_PHASE,
        "title": "Further steps",
        "summary": "Other capabilities this template resolves to.",
    },
)


def plan_phases(template: str = DEFAULT_TEMPLATE) -> list[dict[str, Any]]:
    """`plan_outcomes` grouped into the phases an auditor recognises.

    Every outcome appears in exactly one phase and phases with no outcomes are
    dropped, so the step counts shown beside each phase still sum to the whole
    template.
    """
    grouped: dict[str, list[dict[str, str]]] = {}
    for outcome in plan_outcomes(template):
        domain = str(outcome["capability"]).split(".", 1)[0]
        phase = _PHASE_OF_DOMAIN.get(domain, _UNGROUPED_PHASE)
        grouped.setdefault(phase, []).append(outcome)
    return [
        {**phase, "steps": grouped[phase["id"]]}
        for phase in PLAN_PHASES
        if grouped.get(phase["id"])
    ]


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


def _comparable_runs(actor=None) -> list[dict]:
    """Completed full-audit runs that actually did work.

    Every filter here removes a run that would mislead rather than inform. A
    narrower run measures a fraction of the graph; a run that reused everything
    and called no model measures nothing at all. Extrapolating a whole audit
    from either is how a number that looks measured ends up being invented.

    Scoped to one auditor's own workspaces. This used to walk every workspace on
    the machine, which on a shared server would have measured — and reported —
    other people's engagements back to whoever asked. The estimate is thinner as
    a result, which is the correct answer rather than a regression.
    """
    comparable: list[dict] = []
    scanned = 0
    for entry in workspaces.list_workspaces(actor):
        try:
            workspace = workspaces.open_workspace(
                actor or workspaces._actor(), str(entry.get("id"))
            )
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


def cost_estimate(actor=None) -> dict[str, Any]:
    """How long this auditor's past full audits took, or a refusal to guess."""
    runs = _comparable_runs(actor)
    if len(runs) < _MIN_RUNS_FOR_ESTIMATE:
        return {
            "state": "insufficient_history",
            "runs_observed": len(runs),
            "reason": (
                "You have no completed full audit to measure from yet, so there "
                "is nothing to base an estimate on. This engagement will be the "
                "first."
            ),
        }
    calls = [int((run.get("usage") or {}).get("llm_turns") or 0) for run in runs]
    minutes = [float(run.get("duration_ms") or 0) / 60_000 for run in runs]
    return {
        "state": "measured",
        "runs_observed": len(runs),
        "basis": "your completed full-audit runs",
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


def plan_preview(template: str = DEFAULT_TEMPLATE, mode: str = "auto",
                 actor=None) -> dict[str, Any]:
    """Everything the brief screen states before the auditor approves."""
    return {
        "template": template,
        # The flat list stays: it is what the brief screen falls back to, and it
        # is the shape every existing consumer reads.
        "outcomes": plan_outcomes(template),
        "phases": plan_phases(template),
        "estimate": cost_estimate(actor),
        "destination": destination(),
        "gates": gates(mode),
    }


def apply_brief(workspace: Workspace, brief: dict[str, Any]) -> dict[str, Any]:
    """Record the brief as planning context.

    A brief is not a new concept: its optional details are planning-context
    fields the assistant can use alongside the imported engagement material.
    Objective and scope are not collected at workspace creation.
    """
    context = {
        field: str(brief.get(field) or "").strip()
        for field in BRIEF_FIELDS
        if str(brief.get(field) or "").strip()
    }
    if not context:
        return workspace.planning
    return workspace.update_planning({"context": context})
