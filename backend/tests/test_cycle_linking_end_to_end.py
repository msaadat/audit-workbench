"""A cycle test built on approved rules, materialized, and evaluated.

This is the phase 7 gate. The registry is not involved anywhere below: the
vocabulary is a document-type catalogue, the fields are induced schemas, and the
rules are a ruleset an auditor approved after reading its measured fan-out.
"""

from __future__ import annotations

import polars as pl
import pytest

from app import (
    cycle_linking,
    cycle_rulesets,
    cycle_vouching,
    doc_tests,
    workspaces,
)

from tests.test_cycle_linking import (  # noqa: F401 - fixture and helpers reused
    INVOICE_FIELDS,
    ORDER_FIELDS,
    approved,
    extract,
    ruleset_payload,
)
from app import document_schemas


@pytest.fixture
def engagement():
    """Three invoices in the ledger, two of them fully evidenced."""

    ws = workspaces.create_workspace("Procure to pay")
    document_schemas.save_schema(ws, "vendor_invoice", INVOICE_FIELDS)
    document_schemas.save_schema(ws, "purchase_order", ORDER_FIELDS)
    ws.add_table(
        "invoices.csv",
        pl.DataFrame({
            "INVOICE_NO": ["INV-1", "INV-2", "INV-3"],
            "AMOUNT": [100.0, 200.0, 300.0],
        }).write_csv().encode(),
    )
    row = ws.add_rcm({
        "process": "Procure to pay",
        "risk": "Payments may be made for goods never ordered.",
        "risk_rating": "high",
        "control": "Every invoice is matched to an approved purchase order.",
    })

    extract(ws, "inv1.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100", invoice_date="2024-04-19")
    extract(ws, "po1.txt", "purchase_order", order_number="PO-1",
            total_amount="100", order_date="2024-04-01")
    # A mismatch an auditor should see, not a missing document.
    extract(ws, "inv2.txt", "vendor_invoice", invoice_number="INV-2",
            order_number="PO-2", total_amount="200", invoice_date="2024-05-02")
    extract(ws, "po2.txt", "purchase_order", order_number="PO-2",
            total_amount="180", order_date="2024-04-20")
    # INV-3 has no order at all.
    extract(ws, "inv3.txt", "vendor_invoice", invoice_number="INV-3",
            order_number="PO-3", total_amount="300", invoice_date="2024-05-09")
    return ws, row


def build(ws, row, **selection) -> dict:
    return cycle_vouching.build_cycle_vouch_test(ws, {
        "title": "Vouch invoices to purchase orders",
        "objective": "Vouch each sampled invoice to its approved order.",
        "rcm_id": str(row["id"]),
        "procedure_key": "match_invoice_to_order",
        "requirement_refs": [f"{row['id']}:matched"],
        "definition": {
            "population": {"selection": selection or {"mode": "evidence_linked"}},
        },
    })


# --------------------------------------------------------------------- build
def test_a_cycle_test_builds_against_the_approved_ruleset(engagement):
    ws, row = engagement
    approved(ws)

    test = build(ws, row)

    assert test["definition"]["ruleset_id"]
    assert test["definition"]["population"]["table"] == "invoices"
    assert test["definition"]["population"]["column"] == "INVOICE_NO"
    assert "registry" not in test


def test_building_without_approved_rules_says_so(engagement):
    ws, row = engagement
    record = cycle_rulesets.save(ws, ruleset_payload(), proposed_by="agent")

    with pytest.raises(cycle_vouching.CycleSchemaError, match="proposed, not approved"):
        cycle_vouching.build_cycle_vouch_test(ws, {
            "title": "t", "objective": "o", "rcm_id": str(row["id"]),
            "procedure_key": "p", "requirement_refs": [f"{row['id']}:matched"],
            "definition": {
                "ruleset_id": record["ruleset_id"],
                "population": {"selection": {"mode": "evidence_linked"}},
            },
        })


def test_coverage_reports_what_the_rules_actually_reach(engagement):
    ws, row = engagement
    approved(ws)

    coverage = build(ws, row)["coverage"]

    assert coverage["population_rows"] == 3
    assert coverage["rows_with_evidence"] == 3
    # INV-3 links only its own invoice; its order role never binds.
    assert coverage["complete_cycles"] == 2
    assert coverage["missing_role_counts"] == {"order": 1}


# -------------------------------------------------------------- materialize
def test_every_linked_row_becomes_an_item_with_its_bindings(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)

    items = cycle_vouching.materialize_cycle_items(ws, test)

    assert sorted(item["label"] for item in items) == ["INV-1", "INV-2", "INV-3"]
    by_label = {item["label"]: item for item in items}
    assert sorted(b["role"] for b in by_label["INV-1"]["role_bindings"]) == [
        "invoice", "order"
    ]
    assert by_label["INV-3"]["missing_roles"] == ["order"]


def test_an_item_id_survives_a_reordered_population(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    before = {item["label"]: item["id"] for item in
              cycle_vouching.materialize_cycle_items(ws, test)}

    ws.replace_table(
        "invoices",
        "invoices.csv",
        pl.DataFrame({
            "INVOICE_NO": ["INV-3", "INV-1", "INV-2"],
            "AMOUNT": [300.0, 100.0, 200.0],
        }).write_csv().encode(),
    )
    after = {item["label"]: item["id"] for item in
             cycle_vouching.materialize_cycle_items(ws, test)}

    assert before == after


# ----------------------------------------------------------------- evaluate
def test_the_cycle_evaluates_end_to_end(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    verdicts = {
        item["label"]: item["result_by_assertion"]["as_total"]["verdict"]
        for item in evaluated["items"]
    }

    assert verdicts["INV-1"] == "match"
    assert verdicts["INV-2"] == "mismatch"
    # No order bound, so there is nothing to compare against — which is not the
    # same as agreeing, and must not read as a pass.
    assert verdicts["INV-3"] == "missing_evidence"
    assert evaluated["status"] == "review_required"


def test_a_verdict_carries_the_rules_it_was_produced_under(engagement):
    ws, row = engagement
    ruleset = approved(ws)
    test = build(ws, row)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    result = evaluated["items"][0]["result_by_assertion"]["as_total"]

    assert result["ruleset_hash"] == ruleset["ruleset_hash"]
    assert result["result_sha1"]


def test_a_comparison_cites_the_pages_it_read(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    matched = next(item for item in evaluated["items"] if item["label"] == "INV-1")

    assert matched["evidence_refs"]
    assert all(anchor["source_kind"] == "document" for anchor in matched["evidence_refs"])


def test_a_date_that_will_not_type_is_invalid_evidence_not_a_mismatch(engagement):
    """A value the engine cannot read is a gap in the evidence, and saying
    'mismatch' would report a finding the documents do not support."""

    ws, row = engagement
    extract(ws, "inv9.txt", "vendor_invoice", invoice_number="INV-9",
            order_number="PO-9", total_amount="90", invoice_date="04-01-2024")
    extract(ws, "po9.txt", "purchase_order", order_number="PO-9",
            total_amount="90", order_date="2024-04-01")
    ruleset = approved(ws, assertions=[{
        "id": "as_sequence", "label": "Order precedes invoice",
        "left": {"role": "order", "field": "order_date"},
        "right": {"role": "invoice", "field": "invoice_date"},
        "operator": "date_on_or_before",
        "rationale": "Goods cannot be billed before they were ordered.",
    }])
    ws.replace_table(
        "invoices",
        "invoices.csv",
        pl.DataFrame({"INVOICE_NO": ["INV-9"], "AMOUNT": [90.0]}).write_csv().encode(),
    )
    test = build(ws, row)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    verdict = evaluated["items"][0]["result_by_assertion"]["as_sequence"]["verdict"]

    assert ruleset["status"] == "approved"
    assert verdict == "invalid_extraction"


def test_an_ambiguous_role_never_produces_a_verdict(engagement):
    ws, row = engagement
    extract(ws, "po1b.txt", "purchase_order", order_number="PO-1", total_amount="100")
    approved(ws)
    test = build(ws, row)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    first = next(item for item in evaluated["items"] if item["label"] == "INV-1")

    assert first["linkage_state"] == "needs_review"
    assert first["result_by_assertion"]["as_total"]["verdict"] == "ambiguous"
    assert first["evaluation"]["state"] == "needs_review"


# ------------------------------------------------------------- persistence
def test_the_test_round_trips_through_storage(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    doc_tests.save_test(ws, evaluated)
    reloaded = doc_tests.load_test(ws, test["id"])

    assert reloaded["definition"]["ruleset_hash"] == test["definition"]["ruleset_hash"]
    assert len(reloaded["items"]) == 3


def test_a_result_from_other_rules_is_refused_on_read(engagement):
    """A stored verdict names the rules that produced it. If those are not the
    rules the test now runs, it is not a result of this test."""

    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    evaluated["items"][0]["result_by_assertion"]["as_total"]["ruleset_hash"] = "sha256:other"
    doc_tests.save_test(ws, evaluated)

    with pytest.raises(Exception, match="produced under different rules"):
        doc_tests.load_test(ws, test["id"])


def test_assertions_are_edited_on_the_ruleset_not_the_test(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)

    with pytest.raises(cycle_vouching.CycleSchemaError, match="cycle rules review"):
        cycle_vouching.mutate_cycle_assertions(ws, test, [{
            "key": "as_new", "operator": "present",
            "left": {"source": "role", "role": "invoice"},
        }])


def test_the_rollup_counts_assertion_cells_without_the_registry(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    rollup = cycle_vouching.result_rollup(evaluated)

    assert rollup["assertion_columns"] == 1
    assert rollup["assertion_counts"]["match"] == 1
    assert rollup["assertion_counts"]["mismatch"] == 1
    assert rollup["assertion_counts"]["missing_evidence"] == 1


# ------------------------------------------------------------- rule changes
def test_superseding_the_rules_reopens_the_test(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    cycle_vouching.evaluate_cycle_test(ws, test)

    approved(ws, assertions=[{
        "id": "as_total", "label": "Totals agree",
        "left": {"role": "invoice", "field": "total_amount"},
        "right": {"role": "order", "field": "total_amount"},
        "operator": "numeric_within", "tolerance": {"absolute": 25},
        "rationale": "This vendor rounds freight to the nearest 25.",
    }])
    reevaluated = cycle_vouching.evaluate_cycle_test(ws, test)

    assert reevaluated["ruleset_superseded"] is True
    assert reevaluated["status"] == "review_required"


def test_a_ruleset_edited_under_a_test_fails_closed(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    test["definition"]["ruleset_hash"] = "sha256:moved"

    with pytest.raises(cycle_vouching.CycleSchemaError, match="regenerate the test"):
        cycle_vouching.materialize_cycle_items(ws, test)


def test_a_reextraction_that_changes_a_value_stales_the_stored_verdict(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)
    assert evaluated["items"][0]["result_by_assertion"]["as_total"]["verdict"] != "not_run"

    extract(ws, "po1-revised.txt", "purchase_order",
            order_number="PO-1", total_amount="100")
    refreshed = cycle_vouching.materialize_cycle_items(ws, evaluated)
    first = next(item for item in refreshed if item["label"] == "INV-1")

    # A second order now answers to PO-1, so the role is ambiguous and the
    # verdict that assumed one order can no longer stand.
    assert first["linkage_state"] == "needs_review"
    assert first["result_by_assertion"]["as_total"]["verdict"] == "not_run"


def test_the_grid_projects_the_ruleset_columns(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)

    grid = cycle_vouching.grid_projection(evaluated, workspace=ws)

    assert [column["key"] for column in grid["columns"]] == ["as_total"]
    assert grid["columns"][0]["label"] == "Totals agree"
    assert grid["columns"][0]["applicable_roles"] == ["invoice", "order"]
    assert grid["columns"][0]["counts"]["match"] == 1
    assert grid["columns"][0]["stale_cells"] == 0


def test_a_grid_cell_is_attributable_to_the_definition_it_was_evaluated_under(engagement):
    ws, row = engagement
    approved(ws)
    test = build(ws, row)
    evaluated = cycle_vouching.evaluate_cycle_test(ws, test)

    grid = cycle_vouching.grid_projection(evaluated, workspace=ws)
    row_one = next(item for item in grid["rows"] if item["label"] == "INV-1")

    assert row_one["definition_stale"] is False
    assert row_one["cells"]["as_total"]["attribution_stale"] is False
    assert sorted(row_one["roles_present"]) == ["invoice", "order"]


def test_the_working_paper_names_the_rules_and_who_approved_them(engagement):
    """A cycle result is only as authorised as the rules that produced it, so
    the file has to say which rules those were and who stood behind them."""

    from app import working_papers

    ws, row = engagement
    ruleset = approved(ws)
    test = build(ws, row)
    doc_tests.save_test(ws, cycle_vouching.evaluate_cycle_test(ws, test))

    markdown = working_papers.render_rcm_markdown(ws, str(row["id"]))

    assert ruleset["ruleset_id"] in markdown
    assert "approved by auditor@example.com" in markdown
    assert "Population: invoices keyed by INVOICE_NO" in markdown
    # Every binding says which approved rule reached it.
    assert "jk_order=po-1" in markdown
