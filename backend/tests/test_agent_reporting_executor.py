"""Focused tests for the deterministic audit reporting executors."""

from __future__ import annotations

import json

import pytest

from app import data_tests, report, workspaces
from app.agent.executors.reporting import (
    REPORT_DRAFT_REF,
    VERIFICATION_REF,
    curate_dashboard,
    dashboard_tile_ref,
    generate_report_draft,
    generate_working_paper,
    output_issues,
    verify_audit,
    working_paper_ref,
)
from app.workspaces import WorkspaceConflict


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


def test_curate_dashboard_commits_tiles_and_returns_stable_refs(workspace_with_data):
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Vendor approval risk",
            "control": "Vendor master control",
            "risk_rating": "high",
        }
    )
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Vendor integrity test",
            "objective": "Assess vendor and approval integrity.",
            "method": "data_analytics",
            "steps": ["Scan the amount population."],
        },
    )
    data_test = data_tests.create(
        ws,
        {
            "title": "Vendor integrity result",
            "objective": "Identify management-relevant vendor integrity signals.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
        },
    )
    data_tests.run(ws, data_test["id"])

    refs = curate_dashboard(ws, run_id="RUN-exec")

    # The executor owns the stable ``tile:<id>`` reference for each pinned tile.
    assert refs == [dashboard_tile_ref(tile["id"]) for tile in ws.tiles]
    assert refs == [dashboard_tile_ref(f"rcm-{data_test['id'].casefold()}")]
    assert ws.planning["dashboard_curation"]["created_count"] == 1
    assert ws.planning["dashboard_curation"]["run_id"] == "RUN-exec"


def test_curate_dashboard_conflicts_when_rcm_parent_changed():
    ws = workspaces.create_workspace("Dashboard curation conflict")
    ws.add_rcm(
        {"process": "AP", "risk": "Duplicate payments", "control": "Duplicate check"}
    )

    # A concurrent writer changes the RCM basis on disk after this handle read it.
    concurrent = workspaces.load_workspace(ws.id)
    concurrent.update_rcm(concurrent.rcm[0]["id"], {"control": "Changed control"})

    # ``ws`` still reflects the pre-change RCM, so the curation commit must
    # conflict rather than pin tiles selected against a stale matrix.
    with pytest.raises(WorkspaceConflict):
        curate_dashboard(ws, run_id="RUN-conflict")


# --------------------------------------------------------------------------- #
# report.working_draft deterministic executor (P7K.2)
# --------------------------------------------------------------------------- #
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
