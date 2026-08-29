"""The reader that judges one cycle, and the contract around what it returns."""

from __future__ import annotations

import json
import re

import pytest

from app.agent.workers.fieldwork import (
    CYCLE_VOUCH_ITEM_SOURCE_ID,
    CYCLE_VOUCH_SYSTEM,
    _cycle_vouch_response_schema,
    validate_cycle_vouch_proposal,
)
from app.agent.workers.model import WorkerResponseValidationError


def _request(checks=None):
    class _Item:
        source_id = CYCLE_VOUCH_ITEM_SOURCE_ID
        content = {
            "item_id": "itm_1",
            "documents": ["doc_1", "doc_2"],
            "checks": checks
            if checks is not None
            else [{
                "check_id": "as_total",
                "requirement": "The invoice is settled for the amount ordered.",
                "operands": [
                    {
                        "operand": "invoice.total_amount",
                        "value": "PKR 2,000,000.00",
                        "excerpt": "Total PKR 2,000,000.00",
                    },
                    {
                        "operand": "order.total_amount",
                        "value": "2,000,000.00",
                        "excerpt": "Order total 2,000,000.00",
                    },
                ],
            }],
        }

    class _Request:
        context = type("Bundle", (), {"items": (_Item(),)})()

    return _Request()


def _cell(**overrides):
    cell = {
        "check_id": "as_total",
        "verdict": "agrees",
        "compared": [
            {"operand": "invoice.total_amount", "value": "PKR 2,000,000.00"},
            {"operand": "order.total_amount", "value": "2,000,000.00"},
        ],
        "reason": "Both state 2,000,000.00; one prints the currency.",
    }
    cell.update(overrides)
    return cell


# ------------------------------------------------------------- response shape
def test_a_verdict_outside_the_vocabulary_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="verdict"):
        _cycle_vouch_response_schema(json.dumps({"cells": [_cell(verdict="probably")]}))


def test_a_verdict_without_a_reason_is_refused():
    """A verdict that cannot say what it compared is not evidence of anything."""

    with pytest.raises(WorkerResponseValidationError, match="reason is required"):
        _cycle_vouch_response_schema(json.dumps({"cells": [_cell(reason="  ")]}))


def test_the_three_verdicts_all_parse():
    for verdict in ("agrees", "disagrees", "cannot_determine"):
        parsed = _cycle_vouch_response_schema(
            json.dumps({"cells": [_cell(verdict=verdict)]})
        )
        assert parsed["cells"][0]["verdict"] == verdict


# ------------------------------------------------------------- the provenance
def test_a_value_that_was_never_supplied_demotes_the_verdict():
    """A verdict whose stated evidence is not in what the worker was given.

    The prototype for this worker answered a check by reading a fact out of a
    neighbouring field's source excerpt and reporting it under the named
    operand. Nothing was invented and the fact was sound — but the value it
    quoted appeared in no supplied field, and a verdict nobody can trace is not
    one an audit can rest on. It is demoted rather than dropped, so the check
    still reads as unsettled instead of vanishing.
    """

    proposal = {"cells": [_cell(compared=[
        {"operand": "invoice.total_amount", "value": "PKR 9,999,999.00"},
    ])]}

    validated = validate_cycle_vouch_proposal(proposal, _request())

    assert validated["cells"][0]["verdict"] == "cannot_determine"
    assert "not the ones supplied" in validated["cells"][0]["reason"]


def test_quoting_the_source_line_rather_than_the_field_is_still_traceable():
    """Both readings are honest: the field, or the line it was read from."""

    proposal = {"cells": [_cell(compared=[
        {"operand": "invoice.total_amount", "value": "Total PKR 2,000,000.00"},
    ])]}

    validated = validate_cycle_vouch_proposal(proposal, _request())

    assert validated["cells"][0]["verdict"] == "agrees"


def test_a_check_the_reader_passed_over_is_not_thereby_satisfied():
    validated = validate_cycle_vouch_proposal({"cells": []}, _request())

    assert [cell["verdict"] for cell in validated["cells"]] == ["cannot_determine"]
    assert "no verdict" in validated["cells"][0]["reason"]


def test_a_cell_for_a_check_nobody_asked_about_is_dropped():
    proposal = {"cells": [_cell(), _cell(check_id="as_invented")]}

    validated = validate_cycle_vouch_proposal(proposal, _request())

    assert [cell["check_id"] for cell in validated["cells"]] == ["as_total"]


def test_a_second_cell_for_one_check_cannot_be_told_from_the_first():
    proposal = {"cells": [_cell(), _cell(verdict="disagrees", reason="On reflection.")]}

    validated = validate_cycle_vouch_proposal(proposal, _request())

    assert len(validated["cells"]) == 1
    assert validated["cells"][0]["verdict"] == "agrees"


# -------------------------------------------------------------------- prompt
def test_the_prompt_legitimises_the_answer_it_most_needs_to_hear():
    """A reader that cannot say "I could not tell" will guess instead."""

    assert "cannot_determine" in CYCLE_VOUCH_SYSTEM
    assert "It is a real answer, not a failure" in CYCLE_VOUCH_SYSTEM
    assert "Never guess to avoid cannot_determine" in CYCLE_VOUCH_SYSTEM


def test_the_prompt_separates_presentation_from_substance():
    assert "'PKR 2,000,000.00' and '2,000,000.00'" in CYCLE_VOUCH_SYSTEM
    assert "P02024004" in CYCLE_VOUCH_SYSTEM
    # "Material" must not be read as an accounting threshold the reader applies.
    assert "Do not apply a materiality threshold of your own" in CYCLE_VOUCH_SYSTEM


def test_the_prompt_names_no_comparison_operator():
    for operator in ("equal_exact", "equal_normalized", "numeric_within"):
        assert operator not in CYCLE_VOUCH_SYSTEM


# --------------------------------------------------------- the executor side
def test_every_reconciliation_this_executor_returns_is_a_declared_disposition():
    """A disposition outside the vocabulary fails the unit, not the commit.

    The first live run of this executor died on ``retry`` — a word that reads
    like an instruction and is not one of the three the contract accepts. The
    suite had nothing exercising the reconciler, so it validated cleanly and
    then failed at the pipeline, which is the same shape the worker contract
    tests exist to prevent on the other side of the call.
    """
    import inspect

    from app.agent.executors import fieldwork as fieldwork_executors
    from app.agent.executors.model import RECONCILIATION_DISPOSITIONS

    source = inspect.getsource(fieldwork_executors.reconcile_cycle_vouch)
    returned = {
        literal.strip("\"'")
        for literal in re.findall(r'ExecutorReconciliation\(\s*["\']([a-z_]+)["\']', source)
    }

    assert returned, "the reconciler returns no classified disposition"
    assert returned <= RECONCILIATION_DISPOSITIONS, (
        f"undeclared disposition(s): {sorted(returned - RECONCILIATION_DISPOSITIONS)}"
    )


def test_the_judged_verdicts_map_onto_what_the_executor_stores():
    """The worker answers in one vocabulary and the record keeps another."""
    from app import cycle_vouching

    from app.agent.workers.fieldwork import CYCLE_VOUCH_VERDICTS

    assert set(cycle_vouching.JUDGED_VERDICTS) == set(CYCLE_VOUCH_VERDICTS)
    assert set(cycle_vouching.JUDGED_VERDICTS.values()) <= cycle_vouching.ASSERTION_VERDICTS
