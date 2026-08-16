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
# The all-reject floor fires only on strong evidence, so a test whose subject is
# something else uses moderately-evidenced candidates and stays clear of it.
_ORDERS_TO_STAFF_MODERATE = {**_ORDERS_TO_STAFF, "strength": "moderate"}
_ORDERS_TO_VENDOR = {
    "ref": "relationship:orders:vendor:vendor_id:vendor_id",
    "left": "orders",
    "right": "vendor",
    "left_on": ["vendor_id"],
    "right_on": ["vendor_id"],
    "role_key": False,
    "strength": "strong",
    "diagnostics": {"match_rate": 1.0, "row_multiplication": 1.0},
}


def _catalog(candidates=None):
    return {
        "candidates": list(
            candidates if candidates is not None else [_ORDERS_TO_STAFF]
        ),
        "table_columns": {
            "orders": ["order_id", "approved_by", "requested_by", "amount", "vendor_id"],
            "staff": ["staff_id", "job_title"],
            "vendor": ["vendor_id", "status"],
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


def _reject(ref, rationale="No concrete audit test spans these tables.", superseded_by=None):
    payload = {"ref": ref, "decision": "reject", "rationale": rationale}
    if superseded_by is not None:
        payload["superseded_by"] = superseded_by
    return payload


def test_a_same_pair_rejection_is_linked_to_the_retained_route_deterministically():
    """The model chooses the route; the validator owns the mechanical link."""

    catalog = _catalog([_ORDERS_TO_STAFF, _ORDERS_TO_STAFF_REQUESTER])
    accepted = analysis_worker.validate_join_utility_proposal(
        {
            "decisions": (
                _retain(),
                _reject(_ORDERS_TO_STAFF_REQUESTER["ref"]),
            )
        },
        _request(catalog),
    )

    rejected = next(
        item
        for item in accepted["decisions"]
        if item["ref"] == _ORDERS_TO_STAFF_REQUESTER["ref"]
    )
    assert rejected["superseded_by"] == _ORDERS_TO_STAFF["ref"]


def test_a_model_cross_pair_reference_is_ignored():
    """A model cannot create an invalid graph through a cross-reference."""

    catalog = _catalog([_ORDERS_TO_STAFF, _ORDERS_TO_VENDOR])

    accepted = analysis_worker.validate_join_utility_proposal(
        {
            "decisions": (
                _retain(),
                _reject(
                    _ORDERS_TO_VENDOR["ref"],
                    superseded_by=_ORDERS_TO_STAFF["ref"],
                ),
            )
        },
        _request(catalog),
    )

    rejected = next(item for item in accepted["decisions"] if item["decision"] == "reject")
    assert rejected["superseded_by"] == ""


def test_a_model_reference_to_a_rejected_route_is_ignored():
    """The deterministic result has no retained route to link to."""

    catalog = _catalog(
        [
            _ORDERS_TO_STAFF_MODERATE,
            {**_ORDERS_TO_VENDOR, "strength": "moderate"},
        ]
    )

    accepted = analysis_worker.validate_join_utility_proposal(
        {
            "decisions": (
                _reject(_ORDERS_TO_STAFF["ref"]),
                _reject(
                    _ORDERS_TO_VENDOR["ref"],
                    superseded_by=_ORDERS_TO_STAFF["ref"],
                ),
            )
        },
        _request(catalog),
    )

    assert all(item["superseded_by"] == "" for item in accepted["decisions"])


def test_a_model_self_reference_is_ignored():
    catalog = _catalog([_ORDERS_TO_STAFF, _ORDERS_TO_VENDOR])

    accepted = analysis_worker.validate_join_utility_proposal(
        {
            "decisions": (
                _retain(),
                _reject(_ORDERS_TO_VENDOR["ref"], superseded_by=_ORDERS_TO_VENDOR["ref"]),
            )
        },
        _request(catalog),
    )

    assert accepted["decisions"][1]["superseded_by"] == ""


def test_a_rejection_with_no_superseded_by_needs_no_alternate():
    """Most rejections are not about redundancy at all — the pair itself
    supports no control. Nothing here should demand a citation for those."""

    catalog = _catalog([_ORDERS_TO_STAFF, _ORDERS_TO_VENDOR])
    accepted = analysis_worker.validate_join_utility_proposal(
        {"decisions": (_retain(), _reject(_ORDERS_TO_VENDOR["ref"]))},
        _request(catalog),
    )

    rejected = next(
        item for item in accepted["decisions"] if item["ref"] == _ORDERS_TO_VENDOR["ref"]
    )
    assert rejected["superseded_by"] == ""


def test_free_text_redundancy_language_cannot_corrupt_the_linkage():
    """The model's prose no longer participates in graph construction."""

    catalog = _catalog([_ORDERS_TO_STAFF, _ORDERS_TO_VENDOR])

    accepted = analysis_worker.validate_join_utility_proposal(
        {
            "decisions": (
                _retain(),
                _reject(
                    _ORDERS_TO_VENDOR["ref"],
                    rationale=(
                        "Superseded by the retained relationship between "
                        "orders and staff for approval authority."
                    ),
                ),
            )
        },
        _request(catalog),
    )

    assert accepted["decisions"][1]["superseded_by"] == ""


def test_superseded_by_is_not_model_authored():
    """The tool asks only for the decision; linkage is deterministic."""

    tool = analysis_worker._join_utility_submission_tool(
        _request(_catalog([_ORDERS_TO_STAFF, _ORDERS_TO_VENDOR]))
    )
    reject_branch = next(
        branch
        for branch in tool["function"]["parameters"]["properties"]["decisions"]["items"]["oneOf"]
        if branch["properties"]["decision"]["enum"] == ["reject"]
    )
    assert "superseded_by" not in reject_branch["properties"]
    assert "superseded_by" not in reject_branch["required"]


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
    # The repair replays the prior decisions rather than reducing them to a list
    # of complaints. A turn told "keep the single most useful of A, B" without
    # being shown what it said about A or B cannot act on that, and one run
    # answered five such instructions at once by rejecting all sixteen
    # candidates.
    conversation = gateway.calls[1]["conversation"]
    assert [message["role"] for message in conversation] == [
        "user",
        "assistant",
        "user",
    ]
    assert "nonexistent" in conversation[1]["content"]
    assert "keep every other decision" in conversation[2]["content"]


def test_one_pair_may_be_rejected_outright_but_a_whole_engagement_may_not():
    """The floor is systemic, not local.

    Two tables sharing a key and nothing worth testing across it are ordinary,
    so a lone pair stays rejectable. Rejecting every pair while several match on
    every row without multiplying any is a different claim — that this data
    supports no cross-table test at all — and a run arrived at it by accident:
    sixteen retained on the first attempt, sixteen rejected on the repair, six
    frames instead of twenty, and the run reported ``completed``.
    """
    lone = analysis_worker.validate_join_utility_proposal(
        {"decisions": (_reject(_ORDERS_TO_STAFF["ref"]),)},
        _request(_catalog([_ORDERS_TO_STAFF])),
    )
    assert lone["decisions"][0]["decision"] == "reject"

    with pytest.raises(analysis_worker.WorkerResponseValidationError) as caught:
        analysis_worker.validate_join_utility_proposal(
            {
                "decisions": (
                    _reject(_ORDERS_TO_STAFF["ref"]),
                    _reject(_ORDERS_TO_VENDOR["ref"]),
                )
            },
            _request(_catalog([_ORDERS_TO_STAFF, _ORDERS_TO_VENDOR])),
        )
    assert "2 table pairs" in str(caught.value)

    # And the claim stays available: alternates of one pair are not two pairs.
    same_pair = analysis_worker.validate_join_utility_proposal(
        {
            "decisions": (
                _reject(_ORDERS_TO_STAFF["ref"]),
                _reject(_ORDERS_TO_STAFF_REQUESTER["ref"]),
            )
        },
        _request(_catalog([_ORDERS_TO_STAFF, _ORDERS_TO_STAFF_REQUESTER])),
    )
    assert all(item["decision"] == "reject" for item in same_pair["decisions"])
