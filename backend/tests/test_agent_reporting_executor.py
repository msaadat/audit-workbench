"""Focused tests for the deterministic audit reporting executors."""

from __future__ import annotations

import json

from app import workspaces
from app.agent.executors.reporting import (
    VERIFICATION_REF,
    generate_working_paper,
    output_issues,
    verify_audit,
    working_paper_ref,
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


def test_generate_working_paper_commits_paper_and_returns_stable_ref():
    ws = workspaces.create_workspace("Working paper generation")
    row = ws.add_rcm(
        {"process": "AP", "risk": "Duplicate payments", "control": "Duplicate check"}
    )

    ref = generate_working_paper(ws, row["id"])

    assert ref == working_paper_ref(row["id"]) == f"working_paper:{row['id']}"
    paper_path = ws.root / "WorkingPapers" / f"{row['id']}.json"
    assert paper_path.is_file()
    paper = json.loads(paper_path.read_text(encoding="utf-8"))
    assert paper["rcm_id"] == row["id"]
    assert paper["markdown"].startswith(f"# RCM Working Paper — {row['id']}")


def test_generate_working_paper_is_deterministic_over_unchanged_state():
    ws = workspaces.create_workspace("Working paper determinism")
    row = ws.add_rcm({"process": "AP", "risk": "Duplicate payments"})
    paper_path = ws.root / "WorkingPapers" / f"{row['id']}.json"

    generate_working_paper(ws, row["id"])
    first = json.loads(paper_path.read_text(encoding="utf-8"))
    generate_working_paper(ws, row["id"])
    second = json.loads(paper_path.read_text(encoding="utf-8"))

    # The rendered content (and its content hash) is a pure projection of the
    # unchanged RCM/execution state; only the wall-clock stamp differs.
    assert second["markdown"] == first["markdown"]
    assert second["source_sha1"] == first["source_sha1"]
