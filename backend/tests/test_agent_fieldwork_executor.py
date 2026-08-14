"""Focused tests for the deterministic fieldwork roll-up executor.

Writing a test's executable specification is not fieldwork — those executors
live in :mod:`app.agent.executors.tests` and are covered by
``test_agent_tests_executor.py``.
"""

from __future__ import annotations

import pytest

from app import data_tests, doc_tests, documents, workspaces
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.fieldwork import (
    result_ref,
    roll_up_results,
    untested_populations,
)


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
    data_test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    data_tests.run(ws, data_test["id"])
    return row


def test_a_population_no_executed_step_asserts_about_is_reported(workspace_with_data):
    """The gap no individual test can be blamed for, and so nothing reports.

    Every data test on the engagement this comes from was anchored on the
    invoice population. Each concluded soundly on what it tested, the roll-up
    concluded soundly on those, and the requisitions population — nineteen of
    whose rows no frame in use could even reach — was never mentioned.
    """
    ws = workspace_with_data
    _executed_rcm_row(ws)

    untested = untested_populations(ws)

    # ``transactions`` carries the executed test; ``customers`` carries none.
    assert untested == ["customers"]


def test_a_population_a_step_declares_is_not_reported_as_untested(
    workspace_with_data,
):
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Customers",
            "risk": "Customer records may be duplicated",
            "control": "Customer master review",
        }
    )
    item = data_tests.create(
        ws,
        {
            "title": "Duplicate customers",
            "objective": "Identify repeated customer identifiers.",
            "engine": "polars",
            "rcm_id": row["id"],
            "spec": {
                "schema_version": 2,
                "steps": [
                    {
                        "label": "Duplicate ids",
                        "instruction": "Repeated customer id.",
                        "table_refs": ["customers"],
                        "population": "customers",
                        "code": "result = customers.filter(pl.col('id').is_duplicated())",
                    }
                ],
            },
        },
    )
    data_tests.run(ws, item["id"])

    assert "customers" not in untested_populations(ws)


def test_roll_up_results_commits_and_returns_stable_row_refs(workspace_with_data):
    ws = workspace_with_data
    row = _executed_rcm_row(ws)
    before = ws.revision

    refs = roll_up_results(ws)

    # One stable ``rcm:<id>`` result reference per RCM row.
    assert refs == [result_ref(row["id"])]
    # Self-committing: the derived roll-up is persisted on the row.
    assert row["execution_rollup"]["tests"] == 1
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


# --------------------------------------------------------------------------- #
# fieldwork.data_test / fieldwork.document_test executors (P7E.2/P7E.3)
# --------------------------------------------------------------------------- #
