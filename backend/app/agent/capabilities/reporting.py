"""Reporting capability group of the audit workflow.

Owns the post-roll-up outcomes of the authoritative audit graph:
``findings.drafted``, ``findings.consolidated``, ``working_papers.generated``,
``report.working_draft``, and ``audit.verified``.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry keys for its
declared context. The dependency edges come from the authoritative graph in
:mod:`agent.workflows.audit`; this module never restates them.
"""

from __future__ import annotations

import json

from ... import finding_consolidation, findings, rcm_execution, report
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
from ._shared import covered_observations as _covered_observations
from ._shared import eligible_observations as _eligible_observations
from ._shared import named_observation_ids as _named_observation_ids
from ._shared import rows as _rows
from ._shared import single_unit as _single

CAPABILITY_IDS: tuple[str, ...] = (
    "findings.drafted",
    "findings.consolidated",
    "working_papers.generated",
    "report.working_draft",
    "audit.verified",
)


# --------------------------------------------------------------------------- #
# findings.drafted (P7H)
# --------------------------------------------------------------------------- #
def _unsupported_observations(
    workspace: Workspace, eligible: list[dict]
) -> dict[str, list[str]]:
    """Eligible observations a finding executor would refuse, and why.

    The support check is deterministic and needs no draft, so asking it here
    costs nothing and saves a whole model turn: ``executors.reporting`` runs the
    same check after the worker has written a draft and raises out of the unit,
    which bills the turn and fails the stage for a reason no prompt could have
    avoided.
    """
    issues = {
        str(item["id"]): findings.observation_support_issues(workspace, item)
        for item in eligible
    }
    return {key: value for key, value in issues.items() if value}


def _pending_lead_observations(workspace: Workspace, eligible: list[dict]) -> set[str]:
    """Observations whose finding is a consolidated lead still to be redrafted.

    Accepting a consolidation merges the members into the lead and marks the
    lead's narrative pending: the text still reads as one member's draft until
    the finding worker rewrites it from every member observation. That is
    outstanding generation work, so it is reported here and expanded below —
    readiness and the unit builder have to name the same work, or the
    scheduler reuses a capability whose units it never asks for.
    """
    leads = {
        str(item.get("source_observation_id") or "")
        for item in workspace.findings
        if finding_consolidation.is_lead(item)
        and (item.get("consolidation") or {}).get("narrative_pending")
    }
    return {item["id"] for item in eligible if item["id"] in leads}


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
    unsupported = _unsupported_observations(workspace, eligible)
    details: dict = {"eligible": len(eligible)}
    covered = _covered_observations(workspace, scope)
    if covered:
        details["covered"] = len(covered)
    if unsupported:
        details["unsupported"] = sorted(unsupported)
        details["unsupported_issues"] = {
            key: unsupported[key] for key in sorted(unsupported)
        }
    unsupported_reason = (
        f"{counted(len(unsupported), 'eligible observation')} "
        f"{verb(len(unsupported), 'rests', 'rest')} on a test that is not complete"
        if unsupported
        else ""
    )
    if invalid:
        return Readiness(
            "review_required",
            tuple(
                reason
                for reason in (
                    f"{counted(len(invalid), 'existing finding draft')} {verb(len(invalid), 'fails', 'fail')} support validation",
                    unsupported_reason,
                )
                if reason
            ),
            details={**details, "invalid": len(invalid)},
        )
    covered = set(linked)
    # An unsupported observation is not missing a draft; it is not draftable.
    # Counting it as missing would report work the expansion will not do, and
    # the run would finish "completed" having quietly skipped it.
    missing = [
        item["id"]
        for item in eligible
        if item["id"] not in covered and item["id"] not in unsupported
    ]
    pending = sorted(_pending_lead_observations(workspace, eligible) - set(unsupported))
    if pending:
        details["pending_redraft"] = len(pending)
    if missing or pending:
        return Readiness(
            "missing",
            tuple(
                reason
                for reason in (
                    f"{counted(len(missing), 'eligible observation')} {verb(len(missing))} finding drafts"
                    if missing
                    else "",
                    f"{counted(len(pending), 'consolidated finding')} "
                    f"{verb(len(pending), 'awaits', 'await')} redrafting from "
                    f"{verb(len(pending), 'its', 'their')} members"
                    if pending
                    else "",
                    unsupported_reason,
                )
                if reason
            ),
            details=details,
        )
    if unsupported:
        return Readiness("blocked", (unsupported_reason,), details=details)
    return Readiness("satisfied", details={**details, "drafted": len(eligible)})


def _finding_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    existing = {str(item.get("source_observation_id") or "") for item in workspace.findings}
    forced = str(scope.get("generation_mode") or "") == "force"
    # Naming a finding, or the observation under it, is the instruction to
    # redraft: an auditor who points at a draft and says it is wrong has already
    # said they want it replaced, so force need not be asked for a second time.
    named = set(_named_observation_ids(workspace, scope))
    eligible = _eligible_observations(workspace, scope)
    unsupported = _unsupported_observations(workspace, eligible)
    # A lead whose narrative is still pending is outstanding work whether or
    # not the request named it, so it expands the way an undrafted
    # observation does. See `_pending_lead_observations`.
    pending = _pending_lead_observations(workspace, eligible)
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
        for item in eligible
        if (
            forced
            or item["id"] in named
            or item["id"] in pending
            or item["id"] not in existing
        )
        and item["id"] not in unsupported
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
    
        produces=("finding",),
        accepts_refs=("observation", "finding", "rcm"),
        redoes_named=("finding", "observation"),
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
    
        produces=("procedure",),
        accepts_refs=(),
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
    
        produces=("report",),
        accepts_refs=(),
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
    
        produces=(),
        accepts_refs=(),
    )


# --------------------------------------------------------------------------- #
# findings.consolidated
# --------------------------------------------------------------------------- #
def _consolidation_ready(workspace: Workspace, _scope: dict) -> Readiness:
    """Whether the current draft set has been reviewed for consolidation.

    Satisfied when every group in the current-basis suggestion has a
    decision, or when there is nothing to ask (fewer than two drafts).
    ``review_required`` while undecided groups exist: the report proceeds on
    the partial edge and carries every undecided draft, so an unreviewed
    suggestion withholds nothing. ``missing`` when no suggestion exists for
    the current basis, which is what a changed finding set looks like.
    """
    drafts = finding_consolidation.draft_findings(workspace)
    if len(drafts) < 2:
        return Readiness("satisfied", details={"drafts": len(drafts)})
    basis = finding_consolidation.basis_sha1(workspace)
    suggestion = finding_consolidation.load(workspace, basis)
    if suggestion is None:
        return Readiness(
            "missing",
            (f"{counted(len(drafts), 'draft finding')} have not been reviewed for consolidation",),
            details={"drafts": len(drafts), "basis_sha1": basis},
        )
    groups = list(suggestion.get("groups") or [])
    undecided = finding_consolidation.undecided_groups(suggestion)
    if undecided:
        return Readiness(
            "review_required",
            (
                f"{counted(len(undecided), 'suggested consolidation')} "
                f"{verb(len(undecided), 'awaits', 'await')} a decision",
            ),
            details={
                "drafts": len(drafts),
                "basis_sha1": basis,
                "groups": len(groups),
                "undecided": len(undecided),
            },
        )
    return Readiness(
        "satisfied",
        details={"drafts": len(drafts), "basis_sha1": basis, "groups": len(groups)},
    )


def _consolidation_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per engagement, and only when the basis has no suggestion."""
    drafts = finding_consolidation.draft_findings(workspace)
    if len(drafts) < 2:
        return []
    basis = finding_consolidation.basis_sha1(workspace)
    forced = str(scope.get("generation_mode") or "") == "force"
    if finding_consolidation.load(workspace, basis) is not None and not forced:
        return []
    finding_ids = [str(item.get("id") or "") for item in drafts]
    return [
        UnitSpec(
            "finding_consolidation",
            "finding_consolidation",
            "Review findings for consolidation",
            tuple(f"finding:{value}" for value in finding_ids),
            {"basis_sha1": basis, "finding_ids": finding_ids},
        )
    ]


def _findings_consolidated() -> Capability:
    return Capability(
        "findings.consolidated",
        "finding_consolidation",
        "Finding consolidation",
        "finding_consolidation",
        audit_workflow.dependencies("findings.consolidated"),
        _consolidation_ready,
        _consolidation_units,
        context="reporting.finding_consolidation",
        invalidate_on=("findings",),
        # Proposal-only: nothing is produced, and the suggestion set the unit
        # persists is decided on the Findings page rather than committed.
        produces=(),
        accepts_refs=("finding",),
    )


_BUILDERS = {
    "findings.drafted": _findings_drafted,
    "findings.consolidated": _findings_consolidated,
    "working_papers.generated": _working_papers_generated,
    "report.working_draft": _report_working_draft,
    "audit.verified": _audit_verified,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
