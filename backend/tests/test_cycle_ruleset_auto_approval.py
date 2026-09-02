"""Auto mode approves the cycle rules it wrote; permission mode still cannot.

The gate exists because approving linkage rules is a judgement, and the design
put it beyond an agent's reach outright. That turned out to cost more than it
bought on a run the auditor had already marked ``auto``: the agent proposed the
rules, every stage reported success, and the engagement silently fell back to
prose tests because ``cycle_rulesets.effective`` returned nothing. Selecting
``mode: auto`` is the auditor delegating that run's approvals, and this suite
pins both halves of the bargain -- the delegation, and its disclosure.
"""

from __future__ import annotations

import polars as pl
import pytest

from app import (
    cycle_linking,
    cycle_rulesets,
    document_schemas,
    workspaces,
)
from app.agent import runner, store
from app.agent.audit_execution import build_audit_workflow_runner
from app.agent.capabilities import tests as test_capabilities
from app.agent.routing import resolve_route


DEAL_FIELDS = [
    {"name": "deal_reference", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "principal", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]

CONFIRMATION_FIELDS = [
    {"name": "counterparty_reference", "role": "identifier",
     "value_type": "identifier", "cardinality": "one", "verbatim": True,
     "confidence": "high"},
    {"name": "transaction_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


def _rules(**overrides) -> dict:
    payload = {
        "cycle_label": "Treasury settlement",
        "roles": [
            {"name": "deal", "document_type": "treasury_deal_ticket",
             "cardinality": "one"},
            {"name": "confirmation", "document_type": "fx_contract",
             "cardinality": "one"},
        ],
        "anchor": {"table": "deals", "column": "DEAL_ID",
                   "role": "deal", "field": "deal_reference"},
        "join_keys": [{
            "id": "jk_deal",
            "left": {"role": "deal", "field": "deal_reference"},
            "right": {"role": "confirmation", "field": "counterparty_reference"},
            "match": "normalized_equal",
            "rationale": "A confirmation cites the deal it confirms.",
        }],
        "assertions": [{
            "id": "as_amount",
            "label": "Principal agrees to the confirmed amount",
            "requirement": "The confirmed amount is the principal dealt.",
            "left": {"role": "deal", "field": "principal"},
            "right": {"role": "confirmation", "field": "transaction_amount"},
        }],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def engagement():
    """A workspace whose matrix asks for a cycle and whose rules await approval."""

    ws = workspaces.create_workspace("Treasury auto approval")
    document_schemas.save_schema(ws, "treasury_deal_ticket", DEAL_FIELDS)
    document_schemas.save_schema(ws, "fx_contract", CONFIRMATION_FIELDS)
    ws.add_table(
        "deals.csv",
        pl.DataFrame({"DEAL_ID": ["TD-0165", "TD-0166"]}).write_csv().encode(),
    )
    ws.add_rcm({
        "process": "Treasury",
        "risk": "A deal settles on terms the counterparty never confirmed.",
        "control_attributes": [{
            "key": "match_all_fields",
            "assertion": "Accuracy",
            "requirement": "The confirmation agrees to the deal ticket.",
            "evidence_kind": "transaction_cycle",
            "required_comparisons": [{
                "key": "amount",
                "left": {"document_type": "treasury_deal_ticket",
                         "field": "principal"},
                "right": {"document_type": "fx_contract",
                          "field": "transaction_amount"},
                "rationale": "The confirmed amount is the principal dealt.",
            }],
        }],
    })
    ws = workspaces.load_workspace(ws.id)
    record = cycle_rulesets.save(ws, _rules(), proposed_by="agent")
    return ws, record


def _approval_stage(ws, mode: str):
    """Schedule the approval capability and hand back its stage and runner."""

    run = store.new_command_run(
        ws,
        mode,
        {
            "source": "follow_up",
            "text": "Make the cycle rules effective",
            "requested_outcomes": ["tests.cycle_ruleset_approved"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(ws, run) == "workflow"
    run = store.load_run(ws, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "tests.cycle_ruleset_approved"
    )
    command = build_audit_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )
    command._refresh()
    return command, stage


def test_an_auto_run_makes_the_rules_effective_without_a_model_call(engagement):
    ws, record = engagement
    command, stage = _approval_stage(ws, "auto")

    command._run_stage(stage)

    assert stage["status"] == "succeeded"
    (unit,) = stage["units"]
    assert unit["status"] == "succeeded"
    assert unit["result_refs"] == [f"cycle_ruleset:{record['ruleset_id']}"]
    # Approving reads stored rules and a corpus measured in code. There is no
    # question here a model would answer.
    assert command.run["usage"]["llm_turns"] == 0

    reloaded = workspaces.load_workspace(ws.id)
    effective = cycle_rulesets.effective(reloaded)
    assert effective["ruleset_id"] == record["ruleset_id"]
    assert effective["approved_by"] == test_capabilities.AGENT_APPROVER
    assert cycle_rulesets.approver_kind(effective) == "agent"


def test_the_approval_opens_the_gate_that_forbade_a_cycle_test(engagement):
    """The consequence the change exists for.

    ``cycle_available`` in the generation worker is exactly whether
    ``candidate(...)`` yields a ``ruleset_id``. While it did not, the turn was
    told "Cycle Vouch is forbidden" and wrote a document-question test instead
    -- which is how a five-field confirmation match became two isolated Q&A
    calls that could each see only one of the two documents.
    """

    ws, record = engagement
    assert cycle_linking.candidate(ws)["reason"] == "no_approved_ruleset"

    command, stage = _approval_stage(ws, "auto")
    command._run_stage(stage)

    candidate = cycle_linking.candidate(workspaces.load_workspace(ws.id))
    assert candidate.get("reason") is None
    assert candidate["ruleset_id"] == record["ruleset_id"]


def test_a_permission_run_leaves_the_rules_for_the_auditor(engagement):
    """The gate survives where it was never delegated."""

    ws, record = engagement
    command, stage = _approval_stage(ws, "permission")

    command._run_stage(stage)

    assert stage["units"] == []
    assert stage["status"] == "blocked"
    reloaded = workspaces.load_workspace(ws.id)
    assert cycle_rulesets.effective(reloaded) is None
    assert cycle_rulesets.get(reloaded, record["ruleset_id"])["status"] == "proposed"


def test_generation_is_not_withheld_when_the_approval_gate_stays_shut():
    """Permission mode must not lose every other test in the engagement.

    The edge into ``tests.specified`` is partial for this reason: a row whose
    cycle rules are unapproved has always been able to get a document-question
    test instead, and blocking generation to wait on an approval that stage is
    not allowed to make would break the mode the gate exists to protect.
    """

    from app.agent import audit_execution

    assert (
        "tests.cycle_ruleset_approved"
        in audit_execution._PARTIAL_DEPENDENCIES["tests.specified"]
    )


def test_a_concern_the_auditor_never_read_is_reported_on_the_run(engagement):
    """What auto mode gives up, said out loud rather than left to be noticed.

    A reviewer approves a join key by reading its fan-out and its unmatched
    count, and approves an assertion by reading how many records could evaluate
    it. Nobody read these, so the run carries what they would have seen.
    """

    ws, record = engagement
    silent = _rules()
    silent["assertions"][0]["right"] = {
        "role": "confirmation", "field": "counterparty_reference"
    }
    cycle_rulesets.save(ws, silent, ruleset_id=record["ruleset_id"])

    command, stage = _approval_stage(ws, "auto")
    command._run_stage(stage)

    assert stage["status"] == "succeeded"
    warnings = command.run.get("warnings") or []
    assert any("approved automatically in auto mode" in text for text in warnings)
