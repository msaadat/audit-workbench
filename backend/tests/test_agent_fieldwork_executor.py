"""Focused tests for the deterministic audit fieldwork executors (P7G.2)."""

from __future__ import annotations

from app import data_tests
from app.agent.executors.fieldwork import result_ref, roll_up_results


def _executed_rcm_row(ws):
    """Build one RCM row with an executed data test that raises an exception."""

    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "method": "data_analytics",
            "steps": ["Identify repeated invoice identifiers."],
        },
    )
    data_test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    data_tests.run(ws, data_test["id"])
    return row


def test_roll_up_results_commits_and_returns_stable_row_refs(workspace_with_data):
    ws = workspace_with_data
    row = _executed_rcm_row(ws)
    before = ws.revision

    refs = roll_up_results(ws)

    # One stable ``rcm:<id>`` result reference per RCM row.
    assert refs == [result_ref(row["id"])]
    # Self-committing: the derived roll-up is persisted on the row.
    assert row["execution_rollup"]["planned_tests"] == 1
    assert ws.revision > before
    # The roll-up created an observation for the exception.
    assert ws.observations


def test_roll_up_results_reuses_stable_observation_identities(workspace_with_data):
    ws = workspace_with_data
    _executed_rcm_row(ws)

    roll_up_results(ws)
    first = [
        (item["id"], item.get("execution_ref"), item.get("status"))
        for item in ws.observations
    ]
    assert first

    # A repeated roll-up over unchanged execution artifacts reuses the same
    # observation rows (keyed on ``execution_ref``) rather than duplicating them.
    roll_up_results(ws)
    second = [
        (item["id"], item.get("execution_ref"), item.get("status"))
        for item in ws.observations
    ]
    assert second == first


def test_roll_up_results_is_read_stable_on_a_workspace_without_executions(
    workspace_with_data,
):
    ws = workspace_with_data
    ws.add_rcm({"process": "AP", "risk": "Duplicate payments", "control": "Check"})

    refs = roll_up_results(ws)

    assert refs == [result_ref(ws.rcm[0]["id"])]
    # No execution artifacts means no observations were raised.
    assert ws.observations == []
