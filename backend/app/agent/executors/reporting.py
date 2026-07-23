"""Deterministic executors for audit reporting/verification capabilities.

These executors perform no model calls. ``verify_audit`` is a pure, read-only
computation of the audit-verification outcome; the audit lifecycle's terminal
``audit.verified`` capability binds it through the scheduler's deterministic
execution path.
"""

from __future__ import annotations

from .. import audit_capabilities
from ... import doc_tests, findings, rcm_execution, report
from ...workspaces import Workspace

VERIFICATION_REF = "audit:verification"

# The output capabilities whose structural readiness gates audit completion.
_OUTPUT_CAPABILITIES = (
    "working_papers.generated",
    "dashboard.curated",
    "report.working_draft",
)


def output_issues(outcome: dict) -> list[str]:
    """Output capabilities that are not structurally satisfied."""

    return [
        capability
        for capability, readiness in (outcome.get("output_readiness") or {}).items()
        if readiness.get("state") != "satisfied"
    ]


def verify_audit(workspace: Workspace) -> dict:
    """Compute the deterministic audit-verification outcome (read-only).

    The outcome combines RCM completion, report quality, and the structural
    readiness of the audit's output capabilities. It mutates nothing; the caller
    records it on the run and derives the terminal unit status from it.
    """

    completion = rcm_execution.completion(workspace)
    quality = report.quality_checks(workspace)
    errors = [
        item for item in quality.get("issues") or [] if item.get("severity") == "error"
    ]
    output_states = {
        capability: audit_capabilities.REGISTRY.get(capability)
        .readiness(workspace, {})
        .payload()
        for capability in _OUTPUT_CAPABILITIES
    }
    issues = [
        capability
        for capability, readiness in output_states.items()
        if readiness.get("state") != "satisfied"
    ]
    return {
        "audit_complete": completion["status"] == "completed"
        and not errors
        and not issues,
        "completion_status": completion["status"],
        "planned_tests_total": sum(
            len(row.get("planned_tests") or []) for row in workspace.rcm
        ),
        "planned_tests_completed": sum(
            str(item.get("status") or "").startswith("completed")
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "planned_tests_review_required": sum(
            item.get("status") == "review_required"
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "planned_tests_blocked": sum(
            item.get("status") == "blocked"
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "data_tests_required": sum(
            "datatest"
            in rcm_execution.required_execution_kinds(item.get("method") or "")
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "data_tests_executed": sum(
            bool(item.get("last_run"))
            for item in workspace.data_tests
            if item.get("planned_test_id")
        ),
        "document_tests_required": sum(
            "doctest"
            in rcm_execution.required_execution_kinds(item.get("method") or "")
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "document_tests_executed": sum(
            item.get("status") == "completed"
            for item in doc_tests.list_tests(workspace)
        ),
        "open_observations": len(completion.get("open_observations") or []),
        "supported_findings": sum(
            item.get("auditor_confirmed")
            and not findings.support_issues(workspace, item)
            for item in workspace.findings
        ),
        "draft_findings": sum(
            not item.get("auditor_confirmed") for item in workspace.findings
        ),
        "report_quality_ok": not errors,
        "report_quality_errors": len(errors),
        "output_readiness": output_states,
        "open_gate_count": len(completion.get("open_observations") or [])
        + int((completion.get("coverage") or {}).get("issue_count") or 0)
        + len(errors)
        + len(issues),
    }


__all__ = ["VERIFICATION_REF", "output_issues", "verify_audit"]
