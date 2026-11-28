"""A cycle test is decided by the assertions its own requirement asks for.

One approved ruleset contracts the whole cycle, and every test written over it
evaluates every assertion so the grid can show the cycle around the control.
Before scoping, one planted mismatch failed the same transaction in every test
over the cycle — the vendor test, the description test, the payment test — when
it was an exception to exactly one control.
"""

from __future__ import annotations

from app import cycle_vouching, doc_tests

from tests.test_cycle_linking import approved
from tests.test_cycle_linking_end_to_end import (  # noqa: F401 - fixture reused
    engagement,
    evaluate,
)

TOTALS = {
    "key": "totals",
    "assertion": "Accuracy",
    "requirement": "The amount billed is the amount ordered.",
    "evidence_kind": "transaction_cycle",
    "required_comparisons": [{
        "key": "as_total",
        "left": {"document_type": "vendor_invoice", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "rationale": "The amount billed must be the amount ordered.",
    }],
}
DATED = {
    "key": "dated",
    "assertion": "Existence",
    "requirement": "The invoice states its date.",
    "evidence_kind": "transaction_cycle",
    "required_comparisons": [{
        "key": "as_dated",
        "left": {"document_type": "vendor_invoice", "field": "invoice_date"},
        "right": None,
        "rationale": "An undated invoice cannot be placed in the cycle.",
    }],
}
ORDERED = {
    "key": "ordered",
    "assertion": "Existence",
    "requirement": "The invoice cites the order it bills against.",
    "evidence_kind": "transaction_cycle",
    # The join key binds exactly these fields, so the linkage answers it.
    "required_comparisons": [{
        "key": "as_ordered",
        "left": {"document_type": "vendor_invoice", "field": "order_number"},
        "right": {"document_type": "purchase_order", "field": "order_number"},
        "rationale": "The invoice names the order it bills against.",
    }],
}


def _cycle(ws):
    approved(ws, assertions=[
        {
            "id": "as_total", "requirement": "The records must agree.",
            "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "rationale": "The amount billed must be the amount ordered.",
        },
        {
            "id": "as_dated", "requirement": "The invoice is dated.",
            "label": "Invoice dated",
            "left": {"role": "invoice", "field": "invoice_date"},
            "right": None,
            "rationale": "An undated invoice cannot be placed in the cycle.",
        },
    ])
    return ws.add_rcm({
        "process": "Procure to pay",
        "risk": "Invoices may be paid that do not agree to an order.",
        "risk_rating": "high",
        "control": "Accounts payable vouches each invoice to its order.",
        "control_attributes": [TOTALS, DATED, ORDERED],
    })


def _build(ws, row, attribute: str) -> dict:
    return cycle_vouching.build_cycle_vouch_test(ws, {
        "title": f"Vouch invoices: {attribute}",
        "objective": "Vouch each invoice to its order.",
        "rcm_id": str(row["id"]),
        "procedure_key": f"vouch_{attribute}",
        "requirement_refs": [f"{row['id']}:{attribute}"],
        "definition": {"population": {"selection": {"mode": "evidence_linked"}}},
    })


def _states(test: dict) -> dict[str, str]:
    return {item["label"]: item["evaluation"]["state"] for item in test["items"]}


def test_the_scope_is_the_assertions_the_requirement_asks_for(engagement):
    ws, _ = engagement
    row = _cycle(ws)

    assert _build(ws, row, "totals")["assertion_scope"] == ["as_total"]
    assert _build(ws, row, "dated")["assertion_scope"] == ["as_dated"]


def test_a_mismatch_fails_only_the_control_it_is_an_exception_to(engagement):
    ws, _ = engagement
    row = _cycle(ws)

    totals = evaluate(ws, _build(ws, row, "totals"), {"INV-2": "mismatch"})
    dated = evaluate(ws, _build(ws, row, "dated"), {"INV-2": "mismatch"})

    assert _states(totals)["INV-2"] == "failed"
    # The same cell disagrees on the same item — and is still shown — but the
    # dating control was never asked about totals.
    (inv2,) = [item for item in dated["items"] if item["label"] == "INV-2"]
    assert inv2["result_by_assertion"]["as_total"]["verdict"] == "mismatch"
    assert _states(dated)["INV-2"] == "passed"
    # INV-3 has no order: missing evidence for totals, irrelevant to dating.
    assert _states(totals)["INV-3"] == "incomplete"
    assert _states(dated)["INV-3"] == "passed"


def test_the_rollup_counts_only_the_scoped_cells(engagement):
    ws, _ = engagement
    row = _cycle(ws)

    dated = evaluate(ws, _build(ws, row, "dated"), {"INV-2": "mismatch"})
    rollup = cycle_vouching.result_rollup(dated)

    assert rollup["assertion_columns"] == 1
    assert rollup["assertion_mismatches"] == 0
    assert rollup["failed_items"] == 0
    assert rollup["assertion_counts"]["total"] == len(dated["items"])


def test_the_grid_marks_context_columns_and_names_the_scope(engagement):
    ws, _ = engagement
    row = _cycle(ws)
    test = _build(ws, row, "dated")
    doc_tests.save_test(ws, evaluate(ws, test, {"INV-2": "mismatch"}))

    grid = cycle_vouching.grid_projection(
        doc_tests.load_test(ws, test["id"]), workspace=ws
    )

    assert grid["assertion_scope"] == ["as_dated"]
    assert {column["key"]: column["in_scope"] for column in grid["columns"]} == {
        "as_total": False,
        "as_dated": True,
    }
    assert grid["assertion_counts"]["mismatch"] == 0



def test_the_grid_counts_open_items_over_the_test_not_the_page(engagement):
    ws, _ = engagement
    row = _cycle(ws)
    test = _build(ws, row, "dated")
    doc_tests.save_test(ws, evaluate(ws, test, {"INV-2": "mismatch"}))
    loaded = doc_tests.load_test(ws, test["id"])

    grid = cycle_vouching.grid_projection(loaded, workspace=ws, limit=1)

    assert len(grid["rows"]) == 1
    assert grid["page"]["total"] == len(loaded["items"]) > 1
    assert grid["pending_dispositions"] == len(loaded["items"])

def test_a_requirement_answered_by_the_join_is_decided_by_the_linkage(engagement):
    ws, _ = engagement
    row = _cycle(ws)

    ordered = evaluate(ws, _build(ws, row, "ordered"), {"INV-2": "mismatch"})

    assert ordered["assertion_scope"] == []
    assert _states(ordered)["INV-1"] == "passed"
    assert _states(ordered)["INV-2"] == "passed"
    # Nothing bound the order, so the citation was never established.
    assert _states(ordered)["INV-3"] == "incomplete"


def test_a_test_without_a_live_requirement_stays_unscoped(engagement):
    """The fixture's row states no comparisons; every assertion decides it."""

    ws, row = engagement
    approved(ws)
    test = cycle_vouching.build_cycle_vouch_test(ws, {
        "title": "Vouch invoices to purchase orders",
        "objective": "Vouch each sampled invoice to its approved order.",
        "rcm_id": str(row["id"]),
        "procedure_key": "match_invoice_to_order",
        "requirement_refs": [f"{row['id']}:matched"],
        "definition": {"population": {"selection": {"mode": "evidence_linked"}}},
    })

    evaluated = evaluate(ws, test, {"INV-2": "mismatch"})

    assert evaluated["assertion_scope"] is None
    assert _states(evaluated)["INV-2"] == "failed"


def test_a_finding_is_drafted_from_the_scoped_assertions_with_their_values(engagement):
    """The draft's required execution source carries what this control found.

    It used to carry every assertion of the cycle with every anchor, which on a
    real ruleset overflowed the source's budget; an oversized candidate is
    dropped whole, so the required source came back empty and no finding could
    be drafted at all.
    """
    from app.agent.context import adapters

    ws, _ = engagement
    row = _cycle(ws)
    test = evaluate(ws, _build(ws, row, "totals"), {"INV-2": "mismatch"})
    doc_tests.save_test(ws, test)
    (inv2,) = [item for item in test["items"] if item["label"] == "INV-2"]

    projection = adapters._finding_execution_projection(
        ws, f"doctest:{test['id']}", cycle_item_id=inv2["id"]
    )

    (result,) = projection["item"]["assertion_results"]
    assert result["key"] == "as_total"
    assert result["verdict"] == "mismatch"
    assert result["values"] == "200 vs 180"
    assert result["evidence"]
    assert projection["item"]["assertion_scope"] == ["as_total"]
