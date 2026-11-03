"""Focused tests for the deterministic audit reporting executors."""

from __future__ import annotations

import json

import pytest

from app import data_tests, report, working_papers, workspaces
from app.agent.executors.reporting import (
    REPORT_DRAFT_REF,
    VERIFICATION_REF,
    generate_report_draft,
    generate_working_paper,
    output_issues,
    verify_audit,
    working_paper_ref,
)
from app.workspaces import WorkspaceConflict


_OUTPUT_CAPABILITIES = {
    "working_papers.generated",
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

    # A fresh workspace has no report draft, so the audit is not complete.
    # Working papers are vacuously satisfied on an empty matrix — no rows means
    # no papers missing — so the report is the one output that must be owed.
    assert outcome["audit_complete"] is False
    assert set(outcome["output_readiness"]) == _OUTPUT_CAPABILITIES
    issues = set(output_issues(outcome))
    assert issues, "an empty workspace must have unsatisfied output capabilities"
    assert issues <= _OUTPUT_CAPABILITIES
    assert {"report.working_draft"} <= issues
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


def test_the_paper_index_names_only_rows_with_a_readable_paper():
    """What the matrix's paper column reads: one rule with the readiness."""
    ws = workspaces.create_workspace("Working paper index")
    written = ws.add_rcm({"process": "AP", "risk": "Duplicate payments"})
    unwritten = ws.add_rcm({"process": "AP", "risk": "Unapproved vendors"})
    broken = ws.add_rcm({"process": "AP", "risk": "Late invoices"})

    generate_working_paper(ws, written["id"])
    (ws.root / "WorkingPapers" / f"{broken['id']}.json").write_text("{not json", encoding="utf-8")

    index = working_papers.rcm_paper_index(ws)

    assert set(index) == {written["id"]}
    assert index[written["id"]]
    assert working_papers.stored_rcm_paper(ws, unwritten["id"]) is None
    assert working_papers.stored_rcm_paper(ws, broken["id"]) is None


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


def test_generate_report_draft_commits_and_reports_no_reconciliation(
    workspace_with_data,
):
    ws = workspace_with_data
    ws.update_planning(
        {
            "context": {"objective": "Assess payments", "scope": "Accounts payable"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nPayments.",
        }
    )

    ref, requires_reconcile = generate_report_draft(ws, run_id="run-report")

    assert ref == REPORT_DRAFT_REF
    assert requires_reconcile is False
    committed = workspaces.load_workspace(ws.id).report
    assert committed["generated_markdown"]
    assert committed["markdown"] == committed["generated_markdown"]
    assert committed["generated_by_run"] == "run-report"
    assert committed["edited"] is False


def test_generate_report_draft_preserves_an_auditor_edit_for_reconciliation(
    workspace_with_data,
):
    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Assess payments"}})
    generate_report_draft(ws, run_id="run-first")
    report.update(ws, {"markdown": "# Auditor rewrote the report"})

    ref, requires_reconcile = generate_report_draft(ws, run_id="run-second")

    assert ref == REPORT_DRAFT_REF
    # The auditor's draft stays authoritative; the candidate waits for review.
    assert requires_reconcile is True
    committed = workspaces.load_workspace(ws.id).report
    assert committed["markdown"] == "# Auditor rewrote the report"
    assert committed["generated_markdown"] != committed["markdown"]


def test_generate_report_draft_drafts_its_prose_through_the_supplied_model(
    workspace_with_data,
):
    """A run hands its gateway in, so the report's turns are the run's."""

    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Assess payments"}})
    asked: list[str] = []

    def model(system: str, user: str) -> str:
        asked.append(system.split("]", 1)[0].lstrip("["))
        if system.startswith("[agent:report_overview]"):
            return (
                "## Introduction\nDrafted introduction.\n\n"
                "## Objective and Scope\nDrafted scope."
            )
        return json.dumps({"rating": "", "conclusion": "Drafted conclusion.", "rows": []})

    generate_report_draft(ws, run_id="run-model", model=model)

    assert asked[:2] == ["agent:report_overview", "agent:report_conclusion"]
    assert len(asked) <= report.DRAFTING_TURNS
    assert "Drafted introduction." in workspaces.load_workspace(ws.id).report["generated_markdown"]
