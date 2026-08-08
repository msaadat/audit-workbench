"""The gate that decides which diagnosed relationships are worth joining.

The worker is proposal-only: it reads schemas and aggregate join diagnostics,
never rows, and its answer decides what ``data.joins_ready`` is allowed to
materialize. These tests cover the contract at the seam that actually broke —
between the registered response schema, which freezes what it returns, and the
semantic validator, which reads it.
"""

from __future__ import annotations

import json

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import WORKERS, WorkerRequest, WorkerRunError
from app.agent.workers import analysis as analysis_worker


class _Gateway:
    """A provider that answers with one scripted tool call per attempt."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, **kwargs):
        self.calls.append(
            {"system": system, "user": user, "activity": activity, **kwargs}
        )
        payload = self.responses.pop(0)
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": analysis_worker.JOIN_UTILITY_SUBMISSION_TOOL,
                        "arguments": json.dumps(payload),
                    },
                }
            ],
        }


_ORDERS_TO_STAFF = {
    "ref": "relationship:orders:staff:approved_by:staff_id",
    "left": "orders",
    "right": "staff",
    "left_on": ["approved_by"],
    "right_on": ["staff_id"],
    "role_key": True,
    "strength": "strong",
    "diagnostics": {"match_rate": 1.0, "row_multiplication": 1.0},
}
_ORDERS_TO_STAFF_REQUESTER = {
    **_ORDERS_TO_STAFF,
    "ref": "relationship:orders:staff:requested_by:staff_id",
    "left_on": ["requested_by"],
}


def _catalog(candidates=None):
    return {
        "candidates": list(
            candidates if candidates is not None else [_ORDERS_TO_STAFF]
        ),
        "table_columns": {
            "orders": ["order_id", "approved_by", "requested_by", "amount"],
            "staff": ["staff_id", "job_title"],
        },
    }


def _request(catalog=None):
    content = catalog or _catalog()
    item = ContextBundleItem(
        source_id=analysis_worker.JOIN_UTILITY_CANDIDATES_SOURCE_ID,
        source_ref="analysis:join_candidates",
        representation=ContextRepresentation("table_aggregate"),
        content=content,
        supplied_size=supplied_size(content),
    )
    return WorkerRequest(
        worker_id=analysis_worker.JOIN_UTILITY_WORKER_ID,
        capability_id="data.join_utility_ready",
        unit_id="join_utility:orders:staff",
        context=ContextBundle(
            capability_id="data.join_utility_ready",
            unit_id="join_utility:orders:staff",
            items=(item,),
            supplied_size=total_supplied_size([item.supplied_size]),
        ),
        unit_input={"parent_refs": ["table:orders", "table:staff"]},
        activity={"artifact_refs": ["table:orders", "table:staff"]},
    )


def _retain(ref=_ORDERS_TO_STAFF["ref"], columns=None, requires=None):
    return {
        "ref": ref,
        "decision": "retain",
        "rationale": "Approval authority is only testable across both tables.",
        "hypothesis": "Every approver holds a job title authorized for the amount.",
        "columns": columns or ["orders.approved_by", "staff.staff_id"],
        "requires": requires or ["orders", "staff"],
    }


def test_a_valid_decision_survives_the_freeze_between_schema_and_validator():
    """The registered schema returns a frozen proposal, so its arrays arrive as
    tuples. A semantic validator that demanded a ``list`` would reject every
    response the model could possibly send, and the failure would look like a
    provider fault rather than a contract one."""

    definition = WORKERS.get(analysis_worker.JOIN_UTILITY_WORKER_ID)
    frozen = definition.response_schema.validate(
        json.dumps({"decisions": [_retain()]})
    )

    assert not isinstance(frozen["decisions"], list), "the freeze is what is under test"

    accepted = analysis_worker.validate_join_utility_proposal(frozen, _request())

    assert [item["decision"] for item in accepted["decisions"]] == ["retain"]
    assert accepted["decisions"][0]["hypothesis"]


def test_the_registered_worker_returns_the_tool_call_it_asked_for():
    gateway = _Gateway([{"decisions": [_retain()]}])

    result = WORKERS.execute(_request(), gateway)

    assert [item["decision"] for item in result.proposal["decisions"]] == ["retain"]
    assert result.attempts == 1
    call = gateway.calls[0]
    assert call["system"] == analysis_worker.JOIN_UTILITY_SYSTEM
    assert call["tool_choice"]["function"]["name"] == (
        analysis_worker.JOIN_UTILITY_SUBMISSION_TOOL
    )
    # The catalog is named once. Sending the resolved bundle beside it would
    # bill the same content twice on every run.
    assert call["user"].count(_ORDERS_TO_STAFF["ref"]) == 1


def test_two_routes_to_one_pair_are_named_together_for_the_single_repair_turn():
    """One pair materializes one join, so retaining both roles is not an answer.
    The gate has a single repair turn, so it has to be told which refs compete
    rather than discovering them one rejection at a time."""

    catalog = _catalog([_ORDERS_TO_STAFF, _ORDERS_TO_STAFF_REQUESTER])

    with pytest.raises(analysis_worker.WorkerResponseValidationError) as caught:
        analysis_worker.validate_join_utility_proposal(
            {
                "decisions": (
                    _retain(),
                    _retain(
                        _ORDERS_TO_STAFF_REQUESTER["ref"],
                        ["orders.requested_by", "staff.staff_id"],
                    ),
                )
            },
            _request(catalog),
        )

    message = "; ".join(caught.value.errors)
    assert "orders and staff" in message
    assert _ORDERS_TO_STAFF["ref"] in message
    assert _ORDERS_TO_STAFF_REQUESTER["ref"] in message


def test_a_retained_test_declares_every_table_it_reads():
    """``requires`` is how a pairwise gate states a test that spans three
    tables. It decides which frame the test is later prepared on, so a name
    outside the catalog, or one of the two sides left out, is not usable."""

    accepted = analysis_worker.validate_join_utility_proposal(
        {"decisions": (_retain(requires=["orders", "staff", "staff"]),)}, _request()
    )
    assert accepted["decisions"][0]["requires"] == ["orders", "staff"]

    with pytest.raises(analysis_worker.WorkerResponseValidationError) as missing:
        analysis_worker.validate_join_utility_proposal(
            {"decisions": (_retain(requires=["orders", "orders"]),)}, _request()
        )
    assert "staff" in "; ".join(missing.value.errors)

    with pytest.raises(analysis_worker.WorkerResponseValidationError) as unknown:
        analysis_worker.validate_join_utility_proposal(
            {"decisions": (_retain(requires=["orders", "staff", "ledger"]),)},
            _request(),
        )
    assert "ledger" in "; ".join(unknown.value.errors)


def test_a_test_may_require_a_table_outside_the_pair_it_was_judged_on():
    """The approval-limit shape: the relationship is approver-to-staff, but the
    test needs the transaction whose amount is checked. Nothing rejects that —
    it is the reason the field exists."""

    catalog = _catalog()
    catalog["table_columns"]["ledger"] = ["ledger_id", "amount"]
    accepted = analysis_worker.validate_join_utility_proposal(
        {"decisions": (_retain(requires=["orders", "staff", "ledger"]),)},
        _request(catalog),
    )

    assert accepted["decisions"][0]["requires"] == ["ledger", "orders", "staff"]


def test_a_retained_relationship_must_name_columns_from_both_sides():
    with pytest.raises(analysis_worker.WorkerResponseValidationError) as caught:
        analysis_worker.validate_join_utility_proposal(
            {"decisions": ({**_retain(), "columns": ["orders.approved_by", "orders.amount"]},)},
            _request(),
        )

    assert "staff" in "; ".join(caught.value.errors)


def test_every_candidate_needs_a_decision():
    catalog = _catalog([_ORDERS_TO_STAFF, _ORDERS_TO_STAFF_REQUESTER])

    with pytest.raises(analysis_worker.WorkerResponseValidationError) as caught:
        analysis_worker.validate_join_utility_proposal(
            {"decisions": (_retain(),)}, _request(catalog)
        )

    assert _ORDERS_TO_STAFF_REQUESTER["ref"] in "; ".join(caught.value.errors)


def test_a_rejection_needs_no_hypothesis_but_still_needs_its_reason():
    accepted = analysis_worker.validate_join_utility_proposal(
        {
            "decisions": (
                {
                    "ref": _ORDERS_TO_STAFF["ref"],
                    "decision": "reject",
                    "rationale": "Matching keys alone establish no control to test.",
                },
            )
        },
        _request(),
    )

    assert accepted["decisions"][0]["rationale"]
    assert accepted["decisions"][0]["hypothesis"] == ""

    with pytest.raises(analysis_worker.WorkerResponseValidationError):
        analysis_worker.validate_join_utility_proposal(
            {"decisions": ({"ref": _ORDERS_TO_STAFF["ref"], "decision": "reject"},)},
            _request(),
        )


def test_the_worker_spends_its_repair_turn_and_then_fails_cleanly():
    unknown = {**_retain(), "ref": "relationship:orders:staff:nonexistent:staff_id"}
    gateway = _Gateway([{"decisions": [unknown]}, {"decisions": [unknown]}])

    with pytest.raises(WorkerRunError) as caught:
        WORKERS.execute(_request(), gateway)

    assert caught.value.attempts == 2
    assert len(gateway.calls) == 2
    assert "Repair the prior response" in gateway.calls[1]["user"]
