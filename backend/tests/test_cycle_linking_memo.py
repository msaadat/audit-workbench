"""One index and one traversal per row, however many tests run the same rules.

Seven evidence-linked tests over one ledger used to index the corpus seven
times and link every row seven times. The index depends on the rules and the
corpus; a traversal depends on the index, its limits, and the values it starts
from. Inside a request cache scope neither moves, so both are made once.
"""

from __future__ import annotations

from app import cycle_linking, cycle_vouching

from tests.test_cycle_linking import approved, extract, ws  # noqa: F401 - fixtures


def _corpus(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po.txt", "purchase_order", order_number="PO-1", total_amount="100")


def test_one_index_per_ruleset_inside_a_scope(ws):
    _corpus(ws)
    ruleset = approved(ws)

    with cycle_vouching.request_cache_scope():
        first = cycle_linking.prepare(ws, ruleset)
        second = cycle_linking.prepare(ws, ruleset)

    assert first is second


def test_the_index_is_rebuilt_outside_a_scope(ws):
    """No scope, no promise that the corpus stands still."""
    _corpus(ws)
    ruleset = approved(ws)

    assert cycle_linking.prepare(ws, ruleset) is not cycle_linking.prepare(ws, ruleset)


def test_a_traversal_is_made_once_per_anchor_value(ws, monkeypatch):
    _corpus(ws)
    prepared = cycle_linking.prepare(ws, approved(ws))
    calls = {"n": 0}
    real = cycle_linking.bind_roles

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cycle_linking, "bind_roles", counted)

    first = cycle_linking.link(prepared, anchor_values=["INV-1"])
    second = cycle_linking.link(prepared, anchor_values=[" inv-1 "])
    other = cycle_linking.link(prepared, anchor_values=["INV-2"])

    assert first is second
    assert first["state"] == "linked"
    assert calls["n"] == 2
    assert other["records"] == []


def test_a_traversal_under_other_limits_is_its_own(ws, monkeypatch):
    _corpus(ws)
    prepared = cycle_linking.prepare(ws, approved(ws))

    wide = cycle_linking.link(prepared, anchor_values=["INV-1"])
    narrow = cycle_linking.link(prepared, anchor_values=["INV-1"], max_records=1)

    assert wide is not narrow
    assert narrow["state"] == "needs_review"
