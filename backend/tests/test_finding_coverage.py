"""Findings consolidation, phases 0 and 1: stable refs and intra-row coverage.

Phase 0: a finding keeps the RCM ref it was drafted against when the row is
later removed (a regenerated matrix), warns about it, and still resolves a
process through ``rcm_semantic_refs``.

Phase 1: three tests on one row that flag the same records are one
measurement made three times. The roll-up keeps every observation but marks
the non-lead ones ``covered_by`` their lead, and finding expansion skips them.
"""

from __future__ import annotations

import pytest

from app import data_test_redundancy, data_tests, findings, rcm_execution, report, workspaces
from app.agent import capabilities as audit_capabilities
from app.agent.capabilities import _shared
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.reporting import FindingExecutorTarget
from app.workspace_transactions import parent_hashes


NARRATIVE = """## Condition

Invoices 1001 and 1003 were paid twice.

## Criteria

Invoice identifiers are required to be unique.

## Root Cause

No duplicate check at entry.

## Risk

Financial loss.

## Recommendation

Enforce uniqueness.
"""


def _row(ws, *, risk="Transactions may bypass controls"):
    return ws.add_rcm(
        {
            "process": "Procurement",
            "risk": risk,
            "risk_rating": "high",
            "control": "Automated validation",
        }
    )


def _polars_test(ws, row, *, title, code):
    step = {
        "label": title,
        "instruction": title,
        "table_refs": ["transactions"],
        "code": code,
    }
    return data_tests.create(
        ws,
        {
            "title": title,
            "objective": title,
            "rcm_id": row["id"],
            "engine": "polars",
            "table_refs": ["transactions"],
            "steps": [step],
            "spec": {"schema_version": 2, "steps": [step]},
        },
    )


def _selecting(*invoices: int) -> str:
    return (
        'result = tables["transactions"].filter('
        f"pl.col('invoice_no').is_in({list(invoices)}))"
    )


_BY_CUSTOMER = 'result = tables["transactions"].filter(pl.col("cust_id") == "C1")'


def _draft(**overrides):
    value = {
        "title": "Duplicate invoice processing",
        "severity": "medium",
        "narrative": NARRATIVE,
        "cause_pending": False,
    }
    value.update(overrides)
    return value


def _commit_finding(ws, observation, run_id="run-1"):
    request = ExecutorRequest(
        executor_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id=f"finding:{observation['id']}",
        proposal={"finding": _draft()},
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"observation:{observation['id']}"]),
        activity={"artifact_refs": [f"observation:{observation['id']}"]},
    )
    target = FindingExecutorTarget(ws, run_id, observation["id"])
    EXECUTORS.execute(request, target)
    return target.workspace


# --------------------------------------------------------------------------- #
# Phase 0 — stable refs
# --------------------------------------------------------------------------- #
def test_an_observation_carries_the_rows_semantic_id(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    data_tests.run_all(ws)

    observation = ws.observations[0]

    assert observation["rcm_id"] == row["id"]
    assert observation["rcm_semantic_id"] == row["semantic_id"]
    assert observation["rcm_semantic_id"].startswith("rcm:procurement:")


def test_a_finding_keeps_its_row_ref_and_process_after_the_row_is_regenerated(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _row(ws)
    _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    data_tests.run_all(ws)
    ws = _commit_finding(ws, ws.observations[0])
    finding = ws.findings[0]
    assert finding["rcm_refs"] == [row["id"]]
    assert finding["rcm_semantic_refs"] == [row["semantic_id"]]

    # The matrix is regenerated: the row goes and comes back under a new id
    # with the same semantic identity.
    ws.remove_rcm(row["id"])
    replacement = _row(ws)
    ws = workspaces.load_workspace(ws.id)
    finding = ws.findings[0]

    assert replacement["id"] != row["id"]
    assert replacement["semantic_id"] == row["semantic_id"]
    # Kept, not cleared — and said so.
    assert finding["rcm_refs"] == [row["id"]]
    assert any("no longer exists" in item for item in findings.evidence_warnings(ws, finding))
    # The report still places the finding under its process.
    context = {"rcm": [{"id": replacement["id"], "semantic_id": replacement["semantic_id"], "process": "Procurement"}]}
    assert report._finding_processes(context, finding) == "Procurement"


def test_a_finding_with_a_dangling_row_ref_can_still_be_saved(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = findings.add(ws, {"title": "Manual finding", "rcm_refs": [row["id"]]})
    ws.remove_rcm(row["id"])
    ws = workspaces.load_workspace(ws.id)

    # The UI writes every ref on each save; a ref already held is tolerated.
    saved = findings.update(ws, item["id"], {"title": "Renamed", "rcm_refs": [row["id"]]})
    assert saved["rcm_refs"] == [row["id"]]
    assert saved["rcm_semantic_refs"] == [row["semantic_id"]]

    # A *new* reference to an unknown row is still refused.
    with pytest.raises(workspaces.WorkspaceError, match="does not exist"):
        findings.update(ws, item["id"], {"rcm_refs": [row["id"], "RCM-NOPE"]})


# --------------------------------------------------------------------------- #
# Phase 1 — row duplicate groups
# --------------------------------------------------------------------------- #
def test_identical_tests_on_one_row_form_one_group_lead_first(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    first = _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    second = _polars_test(ws, row, title="Invoices 1001 and 1003", code=_selecting(1001, 1003))
    data_tests.run_all(ws)

    groups = data_test_redundancy.row_duplicate_groups(ws, row["id"])

    # Identical: the earliest-created test leads.
    assert groups == [[first["id"], second["id"]]]


def test_a_subsumed_chain_resolves_to_the_outermost_test(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    narrow = _polars_test(ws, row, title="Two", code=_selecting(1001, 1003))
    middle = _polars_test(ws, row, title="Three", code=_selecting(1001, 1003, 1005))
    wide = _polars_test(ws, row, title="Four", code=_selecting(1001, 1002, 1003, 1005))
    data_tests.run_all(ws)

    groups = data_test_redundancy.row_duplicate_groups(ws, row["id"])

    assert len(groups) == 1
    assert groups[0][0] == wide["id"]
    assert set(groups[0]) == {narrow["id"], middle["id"], wide["id"]}


def test_overlapping_tests_do_not_form_a_group(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    _polars_test(ws, row, title="Left", code=_selecting(1001, 1002, 1003))
    _polars_test(ws, row, title="Right", code=_selecting(1002, 1003, 1005))
    data_tests.run_all(ws)

    # Half their records in common is worth an auditor's eye and is marked as
    # such, but two tests that each say something the other does not are not
    # one measurement.
    assert all(
        item["redundancy"]["state"] == data_test_redundancy.OVERLAPPING
        for item in ws.data_tests
    )
    assert data_test_redundancy.row_duplicate_groups(ws, row["id"]) == []


def test_identical_tests_on_different_rows_do_not_form_a_row_group(workspace_with_data):
    ws = workspace_with_data
    left_row = _row(ws, risk="Risk A")
    right_row = _row(ws, risk="Risk B")
    _polars_test(ws, left_row, title="A", code=_BY_CUSTOMER)
    _polars_test(ws, right_row, title="B", code=_selecting(1001, 1003))
    data_tests.run_all(ws)

    assert data_test_redundancy.row_duplicate_groups(ws, left_row["id"]) == []
    assert data_test_redundancy.row_duplicate_groups(ws, right_row["id"]) == []


# --------------------------------------------------------------------------- #
# Phase 1 — covered observations at roll-up
# --------------------------------------------------------------------------- #
def test_three_duplicate_tests_yield_one_uncovered_observation(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    lead = _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    _polars_test(ws, row, title="Two invoices", code=_selecting(1001, 1003))
    _polars_test(ws, row, title="Same two", code=_selecting(1003, 1001))
    data_tests.run_all(ws)

    ws = workspaces.load_workspace(ws.id)
    observations = [item for item in ws.observations if item["rcm_id"] == row["id"]]
    assert len(observations) == 3
    uncovered = [item for item in observations if not item.get("covered_by")]
    covered = [item for item in observations if item.get("covered_by")]
    assert [item["test_id"] for item in uncovered] == [lead["id"]]
    assert {item["covered_by"] for item in covered} == {uncovered[0]["id"]}
    # Every observation keeps its own outcome: coverage is not a disposition.
    assert all(item["outcome"] == "exception" for item in observations)
    assert row["execution_rollup"]["covered_observations"] == 2 or (
        ws.rcm[0]["execution_rollup"]["covered_observations"] == 2
    )

    # Expansion drafts the lead only, and readiness reports the rest.
    assert [item["test_id"] for item in _shared.eligible_observations(ws, {})] == [lead["id"]]
    capability = audit_capabilities.REGISTRY.get("findings.drafted")
    readiness = capability.readiness(ws, {})
    assert readiness.state == "missing"
    assert readiness.details["eligible"] == 1
    assert readiness.details["covered"] == 2
    units = capability.expand_units(ws, {})
    assert [unit.parent_refs[0] for unit in units] == [f"observation:{uncovered[0]['id']}"]


def test_retiring_a_duplicate_clears_the_coverage_on_the_next_rollup(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    twin = _polars_test(ws, row, title="Two invoices", code=_selecting(1001, 1003))
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    assert any(item.get("covered_by") for item in ws.observations)

    # The duplicate is rewritten to flag something else and re-run: the mark
    # it earned last run no longer holds.
    data_tests.update(ws, twin["id"], {
        "spec": {"schema_version": 2, "steps": [{
            "label": "Other", "instruction": "Other", "table_refs": ["transactions"],
            "code": 'result = tables["transactions"].filter(pl.col("cust_id") == "C3")',
        }]},
    })
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)

    assert not any(item.get("covered_by") for item in ws.observations)


def test_a_drafted_finding_whose_observation_becomes_covered_reports_a_support_issue(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _row(ws)
    lead = _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    ws = _commit_finding(ws, ws.observations[0])
    finding = ws.findings[0]
    assert findings.support_issues(ws, finding) == []

    # A second, identical test appears on the row *and* is made the lead by
    # subsuming the first, so the drafted finding's observation is now covered.
    _polars_test(ws, row, title="Wider", code=_selecting(1001, 1002, 1003))
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    source = next(item for item in ws.observations if item["test_id"] == lead["id"])
    assert source.get("covered_by")

    finding = ws.findings[0]
    issues = findings.support_issues(ws, finding)
    assert any("covered by a duplicate" in issue for issue in issues)
    assert any("covered by a duplicate" in issue for issue in findings.observation_support_issues(ws, source))
    readiness = audit_capabilities.REGISTRY.get("findings.drafted").readiness(ws, {})
    assert readiness.details["covered"] == 1
