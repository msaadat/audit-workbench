"""Carrying an exploratory procedure that found something into a Data Test.

The loss this closes is specific and was measured: findings are drafted only
from RCM test executions, so an analysis that computed a real exception had no
edge into the audit graph and expired where it ran. These tests pin the
guarantee that replaces it — every procedure holding exceptions is answered,
promoted or declined, and the answer is durable.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from app import (
    analysis_promotion,
    analysis_results,
    data_tests,
    llm,
    report,
    workspaces,
)
from app.agent import runner, store
from app.agent.context import PRESETS, ContextResolver, promotion_scope
from app.agent.workers import analysis as analysis_workers
from app.agent.workers.model import WorkerResponseValidationError

from conftest import stamp_planning_cycle, FakeAgentLLM, wait_run


PYTHON_CODE = (
    'result = transactions.filter(pl.col("amount") > 100)'
)


def _workspace() -> workspaces.Workspace:
    ws = workspaces.create_workspace("Promotion")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame(
            {"invoice": [1, 2, 3], "amount": [50.0, 500.0, 900.0]}
        ).write_csv().encode(),
    )
    ws.update_planning(
        {
            "context": {"objective": "Assess payments", "scope": "Payables"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nPayables.",
        }
    )
    row = ws.add_rcm(
        {
            "process": "Payments",
            "risk": "Payments exceed the approved commitment",
            "control": "No control identified",
            "risk_rating": "high",
        }
    )
    # An engagement whose matrix is already settled has a cycle shape saying so.
    # Without one the cycle stage materializes and the matrix, whose dependency
    # it is, stops being reusable — so these runs would re-derive the whole
    # matrix instead of exercising promotion.
    stamp_planning_cycle(ws)
    # The row already carries an executable test, so ``tests.specified`` is
    # satisfied and these runs exercise promotion rather than re-deriving the
    # generation stage that precedes it.
    data_tests.create(
        ws,
        {
            "title": "Payment records are present",
            "objective": "Confirm every payment carries an amount.",
            "engine": "polars",
            "rcm_id": row["id"],
            "steps": [
                {
                    "label": "Missing amounts",
                    "instruction": "Exception rows have no amount.",
                    "population": "transactions",
                    "code": 'result = transactions.filter(pl.col("amount").is_null())',
                }
            ],
            "spec": {
                "schema_version": 2,
                "steps": [
                    {
                        "label": "Missing amounts",
                        "instruction": "Exception rows have no amount.",
                        "population": "transactions",
                        "code": 'result = transactions.filter(pl.col("amount").is_null())',
                    }
                ],
            },
        },
    )
    return ws


def _flagging_analysis(ws: workspaces.Workspace) -> dict:
    saved = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions",
            "title": "Amount above the approved commitment",
            "note": "A payment over the approved amount indicates the match did not run.",
            "spec": {"code": PYTHON_CODE},
            "outcome_policy": {"mode": "exception_rows"},
        }
    )
    analysis_results.execute_and_record(ws, saved["id"])
    return next(item for item in ws.analyses if item["id"] == saved["id"])


# --------------------------------------------------------------------------- #
# Candidate selection
# --------------------------------------------------------------------------- #
def test_a_procedure_that_found_something_is_a_candidate_until_it_is_answered():
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    assert analysis_promotion.exception_count(analysis) > 0

    pending = analysis_promotion.candidates(ws)
    assert [item["id"] for item in pending] == [analysis["id"]]

    analysis[analysis_promotion.PROMOTION_FIELD] = analysis_promotion.declined_record(
        result_sha1=analysis_promotion.result_sha1(analysis),
        reason="A distribution fact, not a control exception.",
        agent_run_id="run",
        decided_at="2026-01-01T00:00:00+00:00",
    )
    ws.save()

    assert analysis_promotion.candidates(ws) == []
    assert analysis_promotion.undispositioned_warning(ws) == ""
    assert analysis_promotion.declined(ws) == [
        {
            "analysis_id": analysis["id"],
            "title": "Amount above the approved commitment",
            "exception_count": 2,
            "reason": "A distribution fact, not a control exception.",
        }
    ]


def test_a_procedure_that_found_nothing_is_never_a_candidate():
    ws = _workspace()
    saved = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions",
            "title": "Amounts are non-negative",
            "spec": {"code": 'result = transactions.filter(pl.col("amount") < 0)'},
            "outcome_policy": {"mode": "exception_rows"},
        }
    )
    analysis_results.execute_and_record(ws, saved["id"])

    # Nothing to carry: a clean result is coverage, not an exception. This is
    # also the shape of the residual gap the module documents — a procedure
    # whose key was wrong reports a clean pass and graduates nothing.
    assert analysis_promotion.candidates(ws) == []


def test_an_answer_does_not_survive_the_conclusion_it_answered():
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    analysis[analysis_promotion.PROMOTION_FIELD] = analysis_promotion.declined_record(
        result_sha1="a-conclusion-that-no-longer-exists",
        reason="Set aside.",
        agent_run_id="run",
        decided_at="2026-01-01T00:00:00+00:00",
    )
    ws.save()

    # The procedure was rewritten and now concludes something else, so the
    # recorded answer belongs to a result that is gone.
    assert analysis_promotion.disposition(analysis) is None
    assert [item["id"] for item in analysis_promotion.candidates(ws)] == [
        analysis["id"]
    ]


# --------------------------------------------------------------------------- #
# The fitting turn's contract
# --------------------------------------------------------------------------- #
def _worker_request(ws: workspaces.Workspace, analysis_id: str):
    capability = type(
        "Capability", (), {"id": "tests.promoted_from_analysis", "context": "analysis.promotion"}
    )()
    _manifest, bundle = ContextResolver().resolve(
        ws, capability, {"id": "unit"}, promotion_scope(ws, analysis_id)
    )
    return type("Request", (), {"context": bundle, "activity": {}})()


def test_a_carried_procedure_may_not_be_rewritten():
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    request = _worker_request(ws, analysis["id"])
    row = ws.rcm[0]

    rewritten = {
        "promote": True,
        "rcm_id": row["id"],
        "title": "Payments above the approved commitment",
        "objective": "Determine whether payments exceeded the approved amount.",
        "step": {
            "label": "Compare",
            "instruction": "Compare each payment to its approved amount.",
            "population": "transactions",
            # The same intent, a different procedure. Its exception count would
            # be inherited from a computation that never ran.
            "code": 'result = transactions.filter(pl.col("amount") > 200)',
        },
    }
    with pytest.raises(WorkerResponseValidationError) as error:
        analysis_workers.validate_promotion_proposal(rewritten, request)
    assert any("unchanged" in message for message in error.value.errors)

    carried = {**rewritten, "step": {**rewritten["step"], "code": PYTHON_CODE}}
    assert analysis_workers.validate_promotion_proposal(carried, request) == carried


def test_the_chosen_row_must_be_one_of_the_supplied_rows():
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    request = _worker_request(ws, analysis["id"])

    with pytest.raises(WorkerResponseValidationError) as error:
        analysis_workers.validate_promotion_proposal(
            {
                "promote": True,
                "rcm_id": "RCM-INVENTED",
                "title": "Placed nowhere",
                "objective": "Determine something.",
                "step": {
                    "label": "Check",
                    "instruction": "Check.",
                    "population": "transactions",
                    "code": PYTHON_CODE,
                },
            },
            request,
        )
    assert any("RCM-INVENTED" in message for message in error.value.errors)


def test_a_decline_needs_a_reason_and_a_promotion_needs_a_step():
    with pytest.raises(WorkerResponseValidationError):
        analysis_workers._promotion_response_schema(json.dumps({"promote": False}))
    with pytest.raises(WorkerResponseValidationError):
        analysis_workers._promotion_response_schema(
            json.dumps({"promote": True, "rcm_id": "RCM-1", "title": "t", "objective": "o"})
        )
    declined = analysis_workers._promotion_response_schema(
        json.dumps({"promote": False, "reason": "A calendar fact."})
    )
    assert declined == {"promote": False, "reason": "A calendar fact."}


def test_the_fitting_turn_is_never_shown_a_flagged_row():
    """The permission that would admit them is not on this path at all."""
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    privacy = PRESETS.get("analysis.promotion").spec.privacy
    assert privacy.allow_analysis_exception_rows is False
    assert privacy.allow_table_rows is False
    assert privacy.allow_small_table_rows is False

    request = _worker_request(ws, analysis["id"])
    serialized = request.context.to_json()
    # 500.0 and 900.0 are the amounts the procedure flagged.
    assert "500.0" not in serialized
    assert "900.0" not in serialized


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def _promotion_run(ws, monkeypatch, response, outcomes=None):
    fake = FakeAgentLLM({"agent:analysis_promotion": response})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {
            "configured": True,
            "backend": "fake",
            "provider": "fake",
            "model": "fake",
        },
    )
    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Place saved analyses",
            "requested_outcomes": outcomes or ["tests.promoted_from_analysis"],
        },
    )
    return wait_run(ws, started["id"])


def test_a_promoted_procedure_becomes_a_data_test_on_the_chosen_row(monkeypatch):
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    row = ws.rcm[0]
    _promotion_run(
        ws,
        monkeypatch,
        {
            "promote": True,
            "rcm_id": row["id"],
            "title": "Payments above the approved commitment",
            "objective": "Determine whether payments exceeded the approved amount.",
            "step": {
                "label": "Compare each payment to its approved amount",
                "instruction": "Exception rows are payments above the approved amount.",
                "population": "transactions",
                "code": PYTHON_CODE,
            },
        },
    )

    current = workspaces.load_workspace(ws.id)
    promoted = [
        item
        for item in current.data_tests
        if item.get("source_analysis_id") == analysis["id"]
    ]
    assert len(promoted) == 1
    assert promoted[0]["rcm_id"] == row["id"]
    assert promoted[0]["steps"][0]["code"] == PYTHON_CODE

    recorded = analysis_promotion.disposition(
        next(item for item in current.analyses if item["id"] == analysis["id"])
    )
    assert recorded["state"] == analysis_promotion.PROMOTED
    assert recorded["test_id"] == promoted[0]["id"]
    # The assertion the whole capability exists to make.
    assert analysis_promotion.undispositioned_warning(current) == ""
    assert analysis_promotion.candidates(current) == []


def test_a_declined_procedure_writes_its_reason_and_no_test(monkeypatch):
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    _promotion_run(
        ws,
        monkeypatch,
        {
            "promote": False,
            "reason": "Values unusual only relative to their own population.",
        },
    )

    current = workspaces.load_workspace(ws.id)
    assert [
        item for item in current.data_tests if item.get("source_analysis_id")
    ] == []
    recorded = analysis_promotion.disposition(
        next(item for item in current.analyses if item["id"] == analysis["id"])
    )
    assert recorded["state"] == analysis_promotion.DECLINED
    assert "own population" in recorded["reason"]
    # A decline is an answer, so nothing is left pending — but it stays
    # readable, because a set-aside procedure is a judgement about the
    # engagement and not an absence.
    assert analysis_promotion.undispositioned_warning(current) == ""
    assert len(analysis_promotion.declined(current)) == 1


def test_an_engagement_with_no_saved_analysis_is_unaffected(monkeypatch):
    ws = _workspace()
    completed = _promotion_run(ws, monkeypatch, {"promote": False, "reason": "n/a"})

    # The capability adds no work of its own: an audit that ran no exploratory
    # analysis must not become an incomplete one because this exists.
    assert completed["status"] in {"completed", "completed_with_open_items"}
    assert [
        item
        for item in workspaces.load_workspace(ws.id).data_tests
        if item.get("source_analysis_id")
    ] == []


def test_an_unanswered_procedure_is_disclosed_as_a_coverage_gap(monkeypatch):
    """The loss, restated as something the report says out loud.

    Before this capability existed a saved procedure holding exceptions simply
    was not mentioned anywhere. It is now a coverage warning until it is
    answered, and silent only once it has been.
    """
    ws = _workspace()
    _flagging_analysis(ws)

    warnings = report._coverage_warnings(report._incomplete_coverage(ws))
    assert any(
        "neither tested nor dispositioned" in warning for warning in warnings
    )

    _promotion_run(
        ws,
        monkeypatch,
        {"promote": False, "reason": "A property of the population."},
    )

    current = workspaces.load_workspace(ws.id)
    warnings = report._coverage_warnings(report._incomplete_coverage(current))
    assert not any(
        "neither tested nor dispositioned" in warning for warning in warnings
    )
    # Answered, not erased: the judgement stays readable.
    assert any(
        "judged not to evidence a control failure" in warning
        for warning in warnings
    )


def test_a_placement_that_cannot_be_made_does_not_withhold_fieldwork(monkeypatch):
    """Promotion is additive, so its failure may not cost the audit its tests.

    A fit the model cannot make is one test the engagement does not gain. Before
    ``tests.promoted_from_analysis`` joined fieldwork's partial dependencies, one
    unsatisfiable placement blocked every data and document test in the run.
    """
    ws = _workspace()
    _flagging_analysis(ws)

    # A response that can never satisfy the contract: promote with no row and
    # no step, through every repair attempt.
    completed = _promotion_run(
        ws, monkeypatch, {"promote": True}, outcomes=["fieldwork.executed"]
    )

    stages = {
        stage["capability"]: stage["status"]
        for stage in completed["workflow"]["stages"]
    }
    assert stages["tests.promoted_from_analysis"] == "failed"
    assert stages["fieldwork.executed"] == "succeeded"

    # And the procedure stays pending rather than being recorded as answered,
    # so the coverage warning still reports it.
    current = workspaces.load_workspace(ws.id)
    assert len(analysis_promotion.candidates(current)) == 1


@pytest.mark.parametrize(
    "population, expected",
    [
        # Both observed on the procurement workspace, and both failed at commit
        # rather than at validation, so the repair turn never saw them.
        ("Invoice population (118 invoices)", "not a supplied frame"),
        ("transactions_customers_joined", "joined frame"),
    ],
)
def test_a_population_must_be_a_frame_that_is_its_own_grain(population, expected):
    ws = _workspace()
    # A joined frame carrying the transactions grain, so the second case has a
    # real frame to name rather than an invented one.
    ws.add_table(
        "customers.csv",
        pl.DataFrame({"invoice": [1, 2, 3], "customer": ["a", "b", "c"]})
        .write_csv()
        .encode(),
    )
    ws.add_join(
        {
            "name": "transactions_customers_joined",
            "left": "transactions",
            "right": "customers",
            "left_on": ["invoice"],
            "right_on": ["invoice"],
            "how": "left",
        }
    )
    # Run the procedure against the joined frame, so the frame is supplied to
    # the turn and the grain rule is what rejects it — rather than it simply
    # being absent, which is a different error with a different fix.
    saved = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions_customers_joined",
            "title": "Large amounts by customer",
            "spec": {
                "code": 'result = tables["transactions_customers_joined"]'
                '.filter(pl.col("amount") > 100)'
            },
            "outcome_policy": {"mode": "exception_rows"},
        }
    )
    analysis_results.execute_and_record(ws, saved["id"])
    request = _worker_request(ws, saved["id"])
    carried = next(x for x in ws.analyses if x["id"] == saved["id"])["spec"]["code"]

    with pytest.raises(WorkerResponseValidationError) as error:
        analysis_workers.validate_promotion_proposal(
            {
                "promote": True,
                "rcm_id": ws.rcm[0]["id"],
                "title": "Placed on the wrong grain",
                "objective": "Determine something.",
                "step": {
                    "label": "Check",
                    "instruction": "Check.",
                    "population": population,
                    "code": carried,
                },
            },
            request,
        )
    assert any(expected in message for message in error.value.errors)


def test_the_frame_a_procedure_runs_against_is_always_supplied():
    """A turn that cannot see the procedure's own frame declines it falsely.

    Observed on the procurement workspace: the schema projection is large, so
    twenty frames overran the source budget and the resolver dropped eight by
    reference order — including two base tables and the joined frame the
    carried procedure reads. The turn declined it, reporting that no supplied
    table held the columns. That was true of what it saw and false of the
    engagement, and it was recorded as a judgement about the audit.
    """
    ws = _workspace()
    ws.add_table(
        "customers.csv",
        pl.DataFrame({"invoice": [1, 2, 3], "customer": ["a", "b", "c"]})
        .write_csv()
        .encode(),
    )
    ws.add_join(
        {
            "name": "transactions_customers_joined",
            "left": "transactions",
            "right": "customers",
            "left_on": ["invoice"],
            "right_on": ["invoice"],
            "how": "left",
        }
    )
    saved = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions_customers_joined",
            "title": "Large amounts by customer",
            "spec": {
                "code": 'result = tables["transactions_customers_joined"]'
                '.filter(pl.col("amount") > 100)'
            },
            "outcome_policy": {"mode": "exception_rows"},
        }
    )
    analysis_results.execute_and_record(ws, saved["id"])

    request = _worker_request(ws, saved["id"])
    supplied = {
        item.content.get("table")
        for item in request.context.items
        if item.source_id == "table_metadata"
    }
    # The frame it runs against, and every base population it could declare.
    assert "transactions_customers_joined" in supplied
    assert {"transactions", "customers"} <= supplied


def test_a_step_that_cannot_execute_is_rejected_before_it_is_committed():
    """Safe is not runnable, and only the second is worth anything here.

    Four steps written for analytics-kind procedures on the procurement
    workspace passed the static sandbox check, referred to a bare ``frame``
    variable the sandbox never defines, and failed at execution. By then the
    analysis was recorded as answered and the test sat on its RCM row reporting
    no exceptions over a procedure that had found four — a false clear on a
    real issue, which is worse than never having promoted it.
    """
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    request = _worker_request(ws, analysis["id"])

    with pytest.raises(WorkerResponseValidationError) as error:
        analysis_workers.validate_promotion_proposal(
            {
                "promote": True,
                "rcm_id": ws.rcm[0]["id"],
                "title": "Amounts above the approved commitment",
                "objective": "Determine whether payments exceeded the amount.",
                "step": {
                    "label": "Compare",
                    "instruction": "Compare each payment to its approved amount.",
                    "population": "transactions",
                    # Exactly what the run shipped: syntactically fine, safe
                    # under the static check, and unrunnable.
                    "code": 'result = frame.filter(pl.col("amount") > 100)',
                },
            },
            request,
        )
    assert any("cannot run against" in message for message in error.value.errors)
    assert any("frame" in message for message in error.value.errors)


def test_an_unknown_column_is_rejected_before_it_is_committed():
    """The same check, on the other way a written step goes wrong."""
    ws = _workspace()
    analysis = _flagging_analysis(ws)
    request = _worker_request(ws, analysis["id"])

    with pytest.raises(WorkerResponseValidationError) as error:
        analysis_workers.validate_promotion_proposal(
            {
                "promote": True,
                "rcm_id": ws.rcm[0]["id"],
                "title": "Amounts above the approved commitment",
                "objective": "Determine whether payments exceeded the amount.",
                "step": {
                    "label": "Compare",
                    "instruction": "Compare each payment to its approved amount.",
                    "population": "transactions",
                    "code": 'result = transactions.filter(pl.col("APPROVED_AMOUNT") > 100)',
                },
            },
            request,
        )
    assert any("cannot run against" in message for message in error.value.errors)
