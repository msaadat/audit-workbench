"""Reporting capability group of the audit workflow.

Owns the post-roll-up outcomes of the authoritative audit graph:
``findings.drafted``, ``working_papers.generated``, ``dashboard.curated``,
``report.working_draft``, and ``audit.verified``.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, the registry keys for its declared
context, and — for ``findings.drafted`` — the auditor-judgment checkpoint that
gates it. The dependency edges come from the authoritative graph in
:mod:`agent.workflows.audit`; this module never restates them.
"""

from __future__ import annotations

import json

from ... import findings, rcm_execution, report
from ...workspaces import Workspace
from ..workflow import (
    Capability,
    Readiness,
    UnitSpec,
    semantic_unit_id,
)
from ..workflows import audit as audit_workflow
from ._shared import all_tests as _all_tests
from ._shared import eligible_observations as _eligible_observations
from ._shared import scoped_observations as _scoped_observations
from ._shared import rows as _rows
from ._shared import single_unit as _single

CAPABILITY_IDS: tuple[str, ...] = (
    "findings.drafted",
    "working_papers.generated",
    "dashboard.curated",
    "report.working_draft",
    "audit.verified",
)


# Declared auditor-judgment checkpoint. Observation disposition is authoritative
# auditor judgment that gates finding creation, so it is declared here at the
# rollup -> findings boundary rather than being embedded inside the (now
# deterministic) roll-up executor. ``STAGE_CHECKPOINTS`` maps the capability whose
# units the checkpoint gates to the checkpoint name; the audit execution
# composition resolves that name to the concrete blocking handler and only runs it
# in permission mode.
OBSERVATION_DISPOSITION_CHECKPOINT = "observation_disposition"
STAGE_CHECKPOINTS: dict[str, str] = {
    "findings.drafted": OBSERVATION_DISPOSITION_CHECKPOINT,
}


# --------------------------------------------------------------------------- #
# findings.drafted (P7H)
# --------------------------------------------------------------------------- #
def _findings_ready(workspace: Workspace, scope: dict) -> Readiness:
    open_observations = [
        item for item in _scoped_observations(workspace, scope) if item.get("status") != "disposed"
    ]
    if open_observations and scope.get("permission_mode"):
        return Readiness(
            "review_required",
            (f"{len(open_observations)} observation(s) require auditor disposition",),
            details={"open_observations": len(open_observations)},
        )
    eligible = _eligible_observations(workspace, scope)
    linked = {
        str(item.get("source_observation_id") or ""): item
        for item in workspace.findings
        if item.get("source_observation_id")
    }
    invalid = [
        item["id"]
        for observation in eligible
        if (item := linked.get(observation["id"])) is not None
        and findings.support_issues(workspace, item)
    ]
    if invalid:
        return Readiness(
            "review_required",
            (f"{len(invalid)} existing finding draft(s) fail support validation",),
            details={"eligible": len(eligible), "invalid": len(invalid)},
        )
    covered = set(linked)
    missing = [item["id"] for item in eligible if item["id"] not in covered]
    if missing:
        return Readiness(
            "missing",
            (f"{len(missing)} eligible observation(s) need finding drafts",),
            details={"eligible": len(eligible)},
        )
    return Readiness("satisfied", details={"eligible": len(eligible), "drafted": len(eligible)})


def _finding_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    existing = {str(item.get("source_observation_id") or "") for item in workspace.findings}
    return [
        UnitSpec(
            semantic_unit_id("finding", item["id"]),
            "finding_draft",
            f"Draft finding — {item.get('summary') or item['id']}",
            (
                f"observation:{item['id']}",
                f"rcm:{item['rcm_id']}",
                str(item.get("execution_ref") or "").rsplit(":", 1)[0]
                if str(item.get("execution_ref") or "").count(":") > 1
                else str(item.get("execution_ref") or ""),
            ),
            item,
        )
        for item in _eligible_observations(workspace, scope)
        if item["id"] not in existing
    ]


def _findings_drafted() -> Capability:
    return Capability(
        "findings.drafted",
        "findings",
        "Eligible finding drafts",
        "finding_draft",
        audit_workflow.dependencies("findings.drafted"),
        _findings_ready,
        _finding_units,
        context="reporting.finding_draft",
        invalidate_on=("observation",),
    )


# --------------------------------------------------------------------------- #
# working_papers.generated (P7I)
# --------------------------------------------------------------------------- #
def _working_papers_ready(workspace: Workspace, scope: dict) -> Readiness:
    rcm_rows = _rows(workspace, scope)
    missing = []
    for row in rcm_rows:
        path = workspace.root / "WorkingPapers" / f"{row['id']}.json"
        if not path.is_file():
            missing.append(row["id"])
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append(row["id"])
    # Existence and structural readability only; whether a paper predates changed
    # source results is currency and is not assessed by the framework.
    return (
        Readiness(
            "missing",
            (f"{len(missing)} RCM working paper(s) are missing",),
            details={"missing": len(missing)},
        )
        if missing
        else Readiness("satisfied", details={"artifact_count": len(rcm_rows)})
    )


def _paper_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    return [
        UnitSpec(
            semantic_unit_id("working_paper", row["id"]),
            "working_paper",
            f"Generate working paper — {row.get('risk') or row['id']}",
            (f"rcm:{row['id']}",),
            row,
        )
        for row in _rows(workspace, scope)
    ]


def _working_papers_generated() -> Capability:
    return Capability(
        "working_papers.generated",
        "working_papers",
        "RCM working papers",
        "working_paper",
        audit_workflow.dependencies("working_papers.generated"),
        _working_papers_ready,
        _paper_units,
        invalidate_on=("rollup",),
    )


# --------------------------------------------------------------------------- #
# dashboard.curated (P7J)
# --------------------------------------------------------------------------- #
def _dashboard_ready(workspace: Workspace, _scope: dict) -> Readiness:
    curation = workspace.planning.get("dashboard_curation") or {}
    if not curation.get("completed_at"):
        return Readiness("missing", ("the RCM dashboard has not been curated",))
    # A completed curation exists; whether it predates current roll-ups is
    # currency and is not assessed by the framework.
    return Readiness(
        "satisfied", details={"tile_count": int(curation.get("created_count") or 0)}
    )


def _dashboard_curated() -> Capability:
    return Capability(
        "dashboard.curated",
        "dashboard",
        "Dashboard curation",
        "dashboard",
        audit_workflow.dependencies("dashboard.curated"),
        _dashboard_ready,
        _single("dashboard", "Curate RCM dashboard"),
        invalidate_on=("rollup",),
    )


# --------------------------------------------------------------------------- #
# report.working_draft (P7K)
# --------------------------------------------------------------------------- #
def _report_ready(workspace: Workspace, _scope: dict) -> Readiness:
    current = report.hydrate(workspace)
    if not str(current.get("generated_markdown") or current.get("markdown") or "").strip():
        return Readiness("missing", ("the report working draft is empty",))
    if current.get("edited") and current.get("generated_markdown") != current.get("markdown"):
        return Readiness(
            "review_required",
            ("an auditor-edited report has a generated candidate awaiting reconciliation",),
        )
    # A non-empty, reconciled report working draft exists; whether it predates
    # current planning/results/findings is currency and is not assessed here.
    preliminary = any(
        str(item.get("status") or "")
        in {"draft", "ready", "in_progress", "blocked", "review_required"}
        for item in _all_tests(workspace)
    ) or any(item.get("status") != "disposed" for item in workspace.observations)
    return Readiness("satisfied", details={"preliminary": preliminary})


def _report_working_draft() -> Capability:
    return Capability(
        "report.working_draft",
        "report",
        "Report working draft",
        "report",
        audit_workflow.dependencies("report.working_draft"),
        _report_ready,
        _single("report", "Assemble report working draft"),
        invalidate_on=("planning:apm", "rollup", "findings"),
    )


# --------------------------------------------------------------------------- #
# audit.verified (P7L)
# --------------------------------------------------------------------------- #
def _verified(workspace: Workspace, _scope: dict) -> Readiness:
    coverage = rcm_execution.coverage(workspace)
    quality = report.quality_checks(workspace)
    errors = [item for item in quality.get("issues") or [] if item.get("severity") == "error"]
    open_items = bool(
        coverage.get("issue_count")
        or any(item.get("status") != "disposed" for item in workspace.observations)
        or any(
            str(item.get("status") or "")
            not in {"completed_no_exception", "completed_with_exception", "not_applicable"}
            for item in _all_tests(workspace)
        )
    )
    status = "completed" if not open_items else "completed_with_open_items"
    if status == "completed" and not errors:
        return Readiness(
            "satisfied",
            details={"completion_status": "completed", "report_quality_ok": True},
        )
    reasons = [f"audit completion status is {status}"]
    if errors:
        reasons.append(f"report quality has {len(errors)} error(s)")
    return Readiness(
        "review_required",
        tuple(reasons),
        details={"completion_status": status, "report_quality_ok": not errors},
    )


def _audit_verified() -> Capability:
    return Capability(
        "audit.verified",
        "verify",
        "Audit verification",
        "verify",
        audit_workflow.dependencies("audit.verified"),
        _verified,
        _single("verify", "Verify completion and report quality"),
        invalidate_on=("outputs",),
    )


_BUILDERS = {
    "findings.drafted": _findings_drafted,
    "working_papers.generated": _working_papers_generated,
    "dashboard.curated": _dashboard_curated,
    "report.working_draft": _report_working_draft,
    "audit.verified": _audit_verified,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
