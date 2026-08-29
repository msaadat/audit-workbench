"""Reporting capability group of the audit workflow.

Owns the post-roll-up outcomes of the authoritative audit graph:
``findings.drafted``, ``working_papers.generated``, ``report.working_draft``,
and ``audit.verified``.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry keys for its
declared context. The dependency edges come from the authoritative graph in
:mod:`agent.workflows.audit`; this module never restates them.
"""

from __future__ import annotations

import json

from ... import findings, rcm_execution, report
from ...text import counted, verb
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
from ._shared import rows as _rows
from ._shared import single_unit as _single

CAPABILITY_IDS: tuple[str, ...] = (
    "findings.drafted",
    "working_papers.generated",
    "report.working_draft",
    "audit.verified",
)


# --------------------------------------------------------------------------- #
# findings.drafted (P7H)
# --------------------------------------------------------------------------- #
def _findings_ready(workspace: Workspace, scope: dict) -> Readiness:
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
            (f"{counted(len(invalid), 'existing finding draft')} {verb(len(invalid), 'fails', 'fail')} support validation",),
            details={"eligible": len(eligible), "invalid": len(invalid)},
        )
    covered = set(linked)
    missing = [item["id"] for item in eligible if item["id"] not in covered]
    if missing:
        return Readiness(
            "missing",
            (f"{counted(len(missing), 'eligible observation')} {verb(len(missing))} finding drafts",),
            details={"eligible": len(eligible)},
        )
    return Readiness("satisfied", details={"eligible": len(eligible), "drafted": len(eligible)})


def _finding_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    existing = {str(item.get("source_observation_id") or "") for item in workspace.findings}
    forced = str(scope.get("generation_mode") or "") == "force"
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
        if forced or item["id"] not in existing
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
            (f"{counted(len(missing), 'RCM working paper')} {verb(len(missing), 'is', 'are')} missing",),
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
    )
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
    completion = rcm_execution.completion(workspace)
    quality = report.quality_checks(workspace)
    errors = [item for item in quality.get("issues") or [] if item.get("severity") == "error"]
    status = str(completion.get("status") or "completed_with_open_items")
    if status == "completed" and not errors:
        return Readiness(
            "satisfied",
            details={"completion_status": "completed", "report_quality_ok": True},
        )
    reasons = [f"audit completion status is {status}"]
    if errors:
        reasons.append(f"report quality has {counted(len(errors), 'error')}")
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
    "report.working_draft": _report_working_draft,
    "audit.verified": _audit_verified,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
