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
    adopt_pending_conclusions,
    result_ref,
    roll_up_results,
    untested_populations,
)
from app.agent.capabilities.fieldwork import _rollup_ready


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


# --------------------------------------------------------------------------- #
# Conclusions the roll-up adopts on tests that already ran
# --------------------------------------------------------------------------- #
def test_a_test_that_ran_without_concluding_is_concluded_by_the_roll_up(
    workspace_with_data,
):
    """The dead end this closes.

    ``data_tests.run`` computes a verdict and stops; adopting it is a separate
    act only the agent's execution path performed. A test run from the register
    therefore held a verdict nobody had read, and because nothing re-visits a
    test that already has a durable result, the row above it could never reach a
    conclusion however many times the roll-up ran.
    """
    ws = workspace_with_data
    row = _executed_rcm_row(ws)
    item = data_tests._record(ws, ws.data_tests[0]["id"])
    # Ran, holds a verdict, and nobody has adopted it.
    assert item["evaluation"]["state"] != "not_run"
    assert item["evaluation"]["suggested_control_conclusion"] == "ineffective"
    assert item["control_conclusion"] == "no_conclusion"
    assert item["control_conclusion_source"] == "none"

    roll_up_results(ws)

    concluded = data_tests._record(ws, item["id"])
    assert concluded["control_conclusion"] == "ineffective"
    # Stamped as the assistant's, so the disclosure of unread conclusions counts
    # it rather than the file reading as though an auditor had signed it.
    assert concluded["control_conclusion_source"] == "agent"
    # And the row above it now concludes, which is the whole point.
    rolled = next(item for item in ws.rcm if item["id"] == row["id"])
    assert rolled["execution_rollup"]["control_conclusion"] == "ineffective"


def test_a_conclusion_the_auditor_reached_is_never_overwritten(workspace_with_data):
    """Their judgment stands; the roll-up reports it rather than revising it."""
    ws = workspace_with_data
    _executed_rcm_row(ws)
    test_id = ws.data_tests[0]["id"]
    data_tests.update(ws, test_id, {"control_conclusion": "not_applicable"})
    assert data_tests._record(ws, test_id)["control_conclusion_source"] == "auditor"

    assert adopt_pending_conclusions(ws) == []

    kept = data_tests._record(ws, test_id)
    assert kept["control_conclusion"] == "not_applicable"
    assert kept["control_conclusion_source"] == "auditor"


def test_adoption_is_idempotent(workspace_with_data):
    """A second roll-up finds nothing left to adopt."""
    ws = workspace_with_data
    _executed_rcm_row(ws)

    assert len(adopt_pending_conclusions(ws)) == 1
    assert adopt_pending_conclusions(ws) == []


def test_a_test_that_never_ran_is_left_alone(workspace_with_data):
    """There is nothing to conclude from, so nothing is concluded."""
    ws = workspace_with_data
    row = ws.add_rcm({"process": "AP", "risk": "R", "control": "C"})
    data_tests.create(
        ws,
        {
            "title": "Never run",
            "objective": "Unexecuted.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )

    assert adopt_pending_conclusions(ws) == []
    assert data_tests._record(ws, ws.data_tests[0]["id"])["control_conclusion"] == "no_conclusion"


# --------------------------------------------------------------------------- #
# Roll-up readiness and the engagement record ask the same question
# --------------------------------------------------------------------------- #
def test_readiness_is_unsatisfied_while_a_row_has_reached_no_conclusion(
    workspace_with_data,
):
    """The disagreement that produced a Run button no press could satisfy.

    Readiness asked only whether a row carried an ``execution_rollup`` dict,
    which every rolled row does. The record asked whether the row had reached a
    conclusion. So the run reused the capability and reported that nothing
    needed doing, while the record redrew the button that had just done nothing.
    """
    from app import engagement_record, rcm_execution

    ws = workspace_with_data
    _executed_rcm_row(ws)
    # Roll up without adopting, which is the state a register-run test leaves.
    rcm_execution.rollup(ws)
    assert all(row.get("execution_rollup") for row in ws.rcm)

    readiness = _rollup_ready(ws, {})

    assert readiness.state == "missing"
    assert not readiness.satisfied
    # The same answer the record gives, which is the property being fixed.
    assert engagement_record._conclusions_set(ws) is False


def test_readiness_is_satisfied_once_every_row_has_concluded(workspace_with_data):
    from app import engagement_record

    ws = workspace_with_data
    _executed_rcm_row(ws)
    roll_up_results(ws)

    assert _rollup_ready(ws, {}).satisfied
    assert engagement_record._conclusions_set(ws) is True


def test_a_readiness_check_does_not_cost_the_roll_up_that_follows_it_its_write(
    workspace_with_data,
):
    """Readiness must read the workspace, not roll it up in place.

    ``rollup`` decides whether to write by hashing ``rcm`` and ``observations``
    before its pass and again after. Readiness runs on the same object the
    executor then rolls up, so a probe that applied the roll-up and left it
    there had the executor compare a mutated state with itself: no change, no
    save, and the row on disk keeping a conclusion the evidence had moved past.

    Staged with an auditor's own conclusion because that is the case with
    nothing for the run to adopt — the adoption would otherwise change the
    projection and mask the missing write.
    """
    from app import rcm_execution, workspaces

    ws = workspace_with_data
    row = _executed_rcm_row(ws)
    test_id = ws.data_tests[0]["id"]
    rcm_execution.rollup(ws)

    # The auditor concludes by hand, so adoption has nothing to do.
    data_tests.update(ws, test_id, {"control_conclusion": "partially_effective"})
    assert rcm_execution.unconcluded_data_tests(ws) == []

    _rollup_ready(ws, {})
    roll_up_results(ws)

    reread = workspaces.Workspace(ws.root)
    stored = next(item for item in reread.rcm if item["id"] == row["id"])
    assert stored["execution_rollup"]["control_conclusion"] == "partially_effective"


def test_the_readiness_probe_leaves_the_workspace_as_it_found_it(workspace_with_data):
    """The property the test above depends on, asserted directly."""
    from app import rcm_execution
    from app.workspace_transactions import canonical_sha1, material_projection

    ws = workspace_with_data
    _executed_rcm_row(ws)
    rcm_execution.rollup(ws)
    data_tests.update(ws, ws.data_tests[0]["id"], {"control_conclusion": "effective"})

    def projection():
        return canonical_sha1(
            material_projection({"rcm": ws.rcm, "observations": ws.observations})
        )

    before = projection()
    _rollup_ready(ws, {})

    assert projection() == before
