"""Focused tests for the deterministic audit-verification executor."""

from __future__ import annotations

from app import workspaces
from app.agent.executors.reporting import (
    VERIFICATION_REF,
    output_issues,
    verify_audit,
)


_OUTPUT_CAPABILITIES = {
    "working_papers.generated",
    "dashboard.curated",
    "report.working_draft",
}


def test_verify_audit_is_read_only_and_deterministic():
    ws = workspaces.create_workspace("Verify read-only")
    before = ws.revision

    outcome = verify_audit(ws)

    # Read-only: verification must not mutate the workspace.
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.revision == before
    # Deterministic: the same workspace yields an identical outcome.
    assert verify_audit(reloaded) == outcome


def test_verify_audit_reports_incomplete_when_outputs_missing():
    ws = workspaces.create_workspace("Verify incomplete")

    outcome = verify_audit(ws)

    # A fresh workspace has no dashboard curation or report draft, so at least
    # those output capabilities are unsatisfied and the audit is not complete.
    assert outcome["audit_complete"] is False
    assert set(outcome["output_readiness"]) == _OUTPUT_CAPABILITIES
    issues = set(output_issues(outcome))
    assert issues, "an empty workspace must have unsatisfied output capabilities"
    assert issues <= _OUTPUT_CAPABILITIES
    assert {"dashboard.curated", "report.working_draft"} <= issues
    assert outcome["open_gate_count"] >= len(issues)


def test_verification_ref_is_the_stable_artifact_reference():
    assert VERIFICATION_REF == "audit:verification"
