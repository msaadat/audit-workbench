"""A stored cycle population vouches for itself while its inputs stand still.

Materialization is idempotent: drawn again from the same definition, rules,
population and evidence, it produces the items already on the file. A reader
used to pay for that drawing on every load to learn, most of the time, that
nothing had moved. Now every write that materializes stores the fingerprint of
what it read, and a reader draws again only when the fingerprint no longer
matches the inputs as they stand.
"""

from __future__ import annotations

import json

import pytest

from app import cycle_linking, cycle_vouching, doc_tests

from tests.test_cycle_linking import approved, extract  # noqa: F401 - helpers
from tests.test_cycle_linking_end_to_end import (  # noqa: F401 - fixture and helpers
    build,
    engagement,
    evaluate,
)


@pytest.fixture
def drawings(monkeypatch):
    """Count the materializations the reader actually performs."""
    calls = {"n": 0}
    real = cycle_linking.materialize_cycle_population

    def counted(workspace, test):
        calls["n"] += 1
        return real(workspace, test)

    monkeypatch.setattr(cycle_linking, "materialize_cycle_population", counted)
    return calls


def _stored(ws, test_id: str) -> dict:
    return json.loads(doc_tests._test_path(ws, test_id).read_text(encoding="utf-8"))


def test_a_write_that_materializes_stores_the_fingerprint(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)

    doc_tests.save_test(ws, evaluate(ws, test))

    stored = _stored(ws, test["id"])
    assert stored[cycle_vouching.ITEMS_INPUTS_KEY]
    assert stored[cycle_vouching.ITEMS_INPUTS_KEY] == (
        cycle_vouching.materialization_inputs_sha1(ws, stored)
    )


def test_a_current_population_is_read_without_being_drawn_again(engagement, drawings):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = evaluate(ws, test)
    doc_tests.save_test(ws, evaluated)
    drawings["n"] = 0

    reloaded = doc_tests.load_test(ws, test["id"])

    assert drawings["n"] == 0
    assert reloaded["items"] == evaluated["items"]


def test_new_evidence_draws_the_population_again(engagement, drawings):
    """INV-3 had no order. One arrives, and the next read binds it."""
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    doc_tests.save_test(ws, evaluate(ws, test))
    before = doc_tests.load_test(ws, test["id"])
    unbound = next(item for item in before["items"] if item["label"] == "INV-3")
    assert "order" in unbound["missing_roles"]
    drawings["n"] = 0

    extract(ws, "po3.txt", "purchase_order", order_number="PO-3",
            total_amount="300", order_date="2024-05-01")
    after = doc_tests.load_test(ws, test["id"])

    assert drawings["n"] == 1
    bound = next(item for item in after["items"] if item["label"] == "INV-3")
    assert "order" not in bound["missing_roles"]


def test_a_changed_population_draws_again(engagement, drawings):
    import polars as pl

    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    doc_tests.save_test(ws, evaluate(ws, test))
    before = doc_tests.load_test(ws, test["id"])["items"][0]["population_ref"]["source_sha1"]
    drawings["n"] = 0

    # The ledger is re-imported under the same name with a row it did not
    # have: the file the population reads is replaced in place. INV-4 has no
    # evidence, so an evidence-linked selection still draws three items — but
    # it draws them again, from the population as it now stands.
    entry = next(item for item in ws.tables if item["name"] == "invoices")
    (ws.data_dir / entry["file"]).write_bytes(
        pl.DataFrame({
            "INVOICE_NO": ["INV-1", "INV-2", "INV-3", "INV-4"],
            "AMOUNT": [100.0, 200.0, 300.0, 400.0],
        }).write_csv().encode()
    )

    reloaded = doc_tests.load_test(ws, test["id"])

    assert drawings["n"] == 1
    assert reloaded["items"][0]["population_ref"]["source_sha1"] != before


def test_a_disposition_keeps_the_population_current(engagement, drawings):
    """Sign-off re-materializes before recording, and files what it read."""
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = evaluate(ws, test)
    doc_tests.save_test(ws, evaluated)
    item = evaluated["items"][0]

    doc_tests.update_item(ws, test["id"], item["id"], {"state": "confirmed"})
    drawings["n"] = 0

    reloaded = doc_tests.load_test(ws, test["id"])

    assert drawings["n"] == 0
    assert reloaded["items"][0]["disposition"]["state"] == "confirmed"


def test_a_population_stored_without_a_fingerprint_is_drawn_as_before(engagement, drawings):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = evaluate(ws, test)
    doc_tests.save_test(ws, evaluated)
    path = doc_tests._test_path(ws, test["id"])
    stored = json.loads(path.read_text(encoding="utf-8"))
    del stored[cycle_vouching.ITEMS_INPUTS_KEY]
    path.write_text(json.dumps(stored), encoding="utf-8")
    drawings["n"] = 0

    reloaded = doc_tests.load_test(ws, test["id"])

    assert drawings["n"] == 1
    assert reloaded["items"] == evaluated["items"]


def test_the_fingerprint_is_no_part_of_the_tests_identity(engagement):
    """A finding anchored to the test's hash must not read 'the evidence
    changed' because a re-materialization confirmed that it had not."""
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = evaluate(ws, test)

    without = {
        key: value for key, value in evaluated.items()
        if key != cycle_vouching.ITEMS_INPUTS_KEY
    }
    assert doc_tests.test_sha1(evaluated) == doc_tests.test_sha1(without)


# --------------------------------------------------------------------------- #
# What a stored verdict is a statement about
# --------------------------------------------------------------------------- #
def test_an_unrelated_population_row_does_not_invalidate_a_verdict(engagement):
    """A verdict about INV-1 says nothing about the rest of the ledger.

    The population's fingerprint sat in every assertion's inputs beside the
    item's own, so one row arriving anywhere in the table reset every verdict
    of every item in every cycle test to `not_run` and flagged it stale. Five
    passing tests on a real engagement went stale that way with the source file
    untouched since import, and no auditor action could clear it: re-running is
    the only route back and the verdicts it needs are not deterministic.
    """
    import polars as pl

    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    doc_tests.save_test(ws, evaluate(ws, test))
    before = doc_tests.load_test(ws, test["id"])
    settled = next(item for item in before["items"] if item["label"] == "INV-1")
    assert settled["evaluation"]["state"] == "passed"

    entry = next(item for item in ws.tables if item["name"] == "invoices")
    (ws.data_dir / entry["file"]).write_bytes(
        pl.DataFrame({
            "INVOICE_NO": ["INV-1", "INV-2", "INV-3", "INV-4"],
            "AMOUNT": [100.0, 200.0, 300.0, 400.0],
        }).write_csv().encode()
    )

    after = doc_tests.load_test(ws, test["id"])
    redrawn = next(item for item in after["items"] if item["label"] == "INV-1")

    # The population is read again — that part is right, and the item says so.
    assert redrawn["population_ref"]["source_sha1"] != (
        settled["population_ref"]["source_sha1"]
    )
    # What INV-1's evidence established still stands.
    assert redrawn["evaluation"]["state"] == "passed"
    assert [
        result["verdict"] for result in redrawn["result_by_assertion"].values()
    ] == [
        result["verdict"] for result in settled["result_by_assertion"].values()
    ]
    assert not any(
        result["stale"] for result in redrawn["result_by_assertion"].values()
    )


def test_the_items_own_evidence_still_invalidates_its_verdict(engagement):
    """The guarantee that matters is kept: this item's inputs still bind it."""
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    doc_tests.save_test(ws, evaluate(ws, test))
    stored = _stored(ws, test["id"])
    item = next(item for item in stored["items"] if item["label"] == "INV-1")
    assert item["evaluation"]["state"] == "passed"

    for result in item["result_by_assertion"].values():
        result["input_hashes"]["frozen_row_sha1"] = "sha1:something-else"

    redrawn = cycle_linking.materialize_cycle_items(ws, stored)
    changed = next(one for one in redrawn if one["label"] == "INV-1")

    assert changed["evaluation"]["state"] == "stale"


def test_a_verdict_stored_before_the_population_key_was_dropped_still_stands(
    engagement,
):
    """The migration the change would otherwise have caused itself.

    Dropping the population fingerprint changes the shape of every stored
    `input_hashes` and the digest over it, so a verdict written under the old
    shape would have compared unequal to a freshly computed one — re-staling
    every verdict on file at the moment the fix landed.
    """
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    doc_tests.save_test(ws, evaluate(ws, test))
    stored = _stored(ws, test["id"])
    item = next(item for item in stored["items"] if item["label"] == "INV-1")

    # Put the old shape back: the population key, and a digest that counted it.
    for result in item["result_by_assertion"].values():
        legacy = dict(result["input_hashes"])
        legacy.pop("input_sha1", None)
        legacy["population_source_sha1"] = item["population_ref"]["source_sha1"]
        legacy["input_sha1"] = cycle_linking.sha1_hash(legacy)
        result["input_hashes"] = legacy

    redrawn = cycle_linking.materialize_cycle_items(ws, stored)
    carried = next(one for one in redrawn if one["label"] == "INV-1")

    assert carried["evaluation"]["state"] == "passed"
