"""Gates that separate a runnable cycle assertion from an informative one.

Every rule here exists because the live procurement engagement produced a
definition that passed structural validation, executed cleanly, and answered
nothing: a reversed date ordering reported as an exception, a population column
checked for non-nullness, an existence test standing on whichever field the form
happened to print, and a scalar comparison against a selector the evidence
already held twice.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app import cycle_vouching
from app.cycle_registry import DEFAULT_REGISTRY

from test_cycle_vouching_phase2 import _manifest, _row_payload, _test_payload


FIXTURE = Path(__file__).parent / "fixtures" / "procurement_cycle_phase0.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _validate(contract: dict, test: dict, *, manifest: dict | None = None) -> dict:
    return cycle_vouching.validate_cycle_test_semantics(
        test,
        rcm_row=_row_payload(contract),
        manifest=manifest or _manifest(contract),
    )


def _assertion(test: dict, key: str) -> dict:
    return next(
        item for item in test["definition"]["assertions"] if item["key"] == key
    )


# --- Tier 1: the executor and the extraction it reads -----------------------


def test_present_reports_a_resolved_population_value_as_a_match():
    """The row entry has a column and a value, never a normalization envelope.

    Deriving the verdict by scanning entries for `normalization_status` reported
    a resolved value as missing evidence, and stored a result whose comparison
    state said `resolved` beside a verdict that said the evidence was absent.
    """

    item = {
        "frozen_row": {"PO_NUMBER": "PO2024004"},
        "role_bindings": [],
        "role_conflicts": [],
    }
    resolved = cycle_vouching._scalar_operand(
        item, {"source": "row", "column": "PO_NUMBER"}, {}, {}
    )

    assert resolved["state"] == "resolved"
    assert resolved["value"] == "PO2024004"


def test_equal_exact_compares_typed_numbers_numerically_and_text_textually():
    """Two extractions of one quantity may normalize to 25 and 25.0."""

    assert cycle_vouching._comparison("equal_exact", 25, 25.0, None) == "match"
    assert cycle_vouching._comparison("equal_exact", 25, 26, None) == "mismatch"
    # An identifier stays exact: the live PO/P0 typo must remain a mismatch.
    assert (
        cycle_vouching._comparison("equal_exact", "PO2024004", "P02024004", None)
        == "mismatch"
    )
    assert cycle_vouching._comparison("equal_exact", True, 1, None) == "mismatch"


def test_reduction_numbers_repeated_occurrences_of_one_attribute():
    """Three party names reported on one ordinal are three occurrences."""

    facts = [
        {
            "group": "parties",
            "kind": "name",
            "attribute": "name",
            "entry": 0,
            "value": {"value": value},
        }
        for value in ("GLOBAL BANK", "OfficeSupply Co.", "Vendor representative")
    ]

    repaired = cycle_vouching._separate_entry_ordinals(copy.deepcopy(facts))

    assert sorted(fact["entry"] for fact in repaired) == [0, 1, 2]


def test_reduction_leaves_an_unrecoverable_pairing_alone():
    """Two overloaded attributes cannot be re-paired without inventing one.

    Splitting an approver and a date independently would state that a named
    approver signed on a date the record never printed beside their name, so the
    facts stay as they are and evaluation continues to report ambiguity.
    """

    facts = [
        {
            "group": "approvals",
            "kind": "approval",
            "attribute": attribute,
            "entry": 0,
            "value": {"value": value},
        }
        for attribute in ("approver", "date")
        for value in ("first", "second")
    ]

    repaired = cycle_vouching._separate_entry_ordinals(copy.deepcopy(facts))

    assert {fact["entry"] for fact in repaired} == {0}


def test_record_manifest_reports_observed_multiplicity_and_registry_vocabulary(
    contract,
):
    """`entry_count` is what extraction claimed; the counts are what it holds."""

    record = cycle_vouching.validate_evidence_reduction(contract["reduction"])[
        "records"
    ][0]
    record["fields"].append(
        {
            "group": "descriptions",
            "kind": "description",
            "attribute": "value",
            "entry": 0,
            "value": {
                "raw_value": "Onboarding kits",
                "value": "Onboarding kits",
                "normalization_status": "normalized",
                "normalization_error": None,
                "citation": "PR-C1",
            },
        }
    )
    record["fields"].append(
        {
            "group": "descriptions",
            "kind": "description",
            "attribute": "value",
            "entry": 0,
            "value": {
                "raw_value": "Procurement of onboarding kits",
                "value": "Procurement of onboarding kits",
                "normalization_status": "normalized",
                "normalization_error": None,
                "citation": "PR-C2",
            },
        }
    )

    manifest = cycle_vouching._record_manifest(record, DEFAULT_REGISTRY)
    descriptions = next(
        field for field in manifest["available_fields"] if field["kind"] == "description"
    )
    approvals = next(
        field for field in manifest["available_fields"] if field["kind"] == "approval"
    )

    assert descriptions["entry_count"] == 1
    assert descriptions["distinct_value_counts"]["value"] == 2
    assert descriptions["label"] == "Description"
    assert descriptions["attribute_types"]["value"] == "text"
    assert descriptions["control_evidence_attributes"] == []
    assert approvals["control_evidence_attributes"] == ["approver"]


# --- Tier 2: assertions that run cleanly and prove nothing ------------------


def test_gate_rejects_a_presence_check_on_a_population_column(contract):
    test = _test_payload(contract)
    _assertion(test, "requisition_approved")["left"] = {
        "source": "row",
        "column": "INVOICE_AMOUNT",
    }

    with pytest.raises(cycle_vouching.CycleSchemaError, match="data test over the table"):
        _validate(contract, test)


def test_gate_rejects_presence_standing_in_for_a_required_role_existing(contract):
    """A bound role exists before any assertion runs.

    The live engagement wrote this as "Purchase Order exists and is reachable"
    over `statuses.status`. It reported `match` because the form prints a
    status, and would have reported an exception on a record that did not —
    an extraction gap dressed as a control failure. Any non-control-evidence
    attribute is the same shape; this uses one the fixture supplies.
    """

    test = _test_payload(contract)
    _assertion(test, "requisition_approved")["left"]["field"] = {
        "group": "amounts",
        "kind": "total",
        "attribute": "value",
    }
    _assertion(test, "requisition_approved")["left"]["role"] = "vendor_invoice"

    with pytest.raises(
        cycle_vouching.CycleSchemaError, match="already bound before any assertion runs"
    ):
        _validate(contract, test)


def test_gate_accepts_presence_of_a_control_evidence_attribute(contract):
    """An approver is on the record because someone performed the control."""

    validated = _validate(contract, _test_payload(contract))

    assert _assertion(validated, "requisition_approved")["operator"] == "present"


def test_gate_rejects_a_scalar_operand_the_evidence_already_holds_twice(contract):
    test = _test_payload(contract)
    manifest = _manifest(contract)
    invoice = next(
        record
        for group in manifest["groups"]
        for record in group["records"]
        if record["record_kind"] == "procure_to_pay.vendor_invoice"
    )
    total = next(
        field for field in invoice["available_fields"] if field["kind"] == "total"
    )
    total["distinct_value_counts"]["value"] = 2

    with pytest.raises(cycle_vouching.CycleSchemaError, match="but the evidence holds 2"):
        _validate(contract, test, manifest=manifest)


def test_gate_rejects_a_date_ordering_stated_against_the_cycle(contract):
    """The live GRN test asserted a receipt falls on or before its own order."""

    test = _test_payload(contract)
    receipt = _assertion(test, "receipt_before_payment")
    receipt["left"], receipt["right"] = receipt["right"], receipt["left"]

    with pytest.raises(
        cycle_vouching.CycleSchemaError, match="registered cycle order puts it later"
    ):
        _validate(contract, test)


def test_gate_accepts_a_date_ordering_that_runs_with_the_cycle(contract):
    validated = _validate(contract, _test_payload(contract))

    assert _assertion(validated, "receipt_before_payment")["operator"] == (
        "date_on_or_before"
    )


def test_gate_ignores_direction_for_a_date_it_has_not_staged(contract):
    """A vendor invoice's receipt date may mean the goods or the invoice.

    An unstaged field is left unordered rather than guessed at, so a comparison
    that names one is accepted and the auditor reviews the result.
    """

    assert (
        DEFAULT_REGISTRY.date_lifecycle_order(
            "procure_to_pay", "procure_to_pay.vendor_invoice", "dates", "receipt_date"
        )
        is None
    )


def test_gate_rejects_a_role_no_assertion_reads(contract):
    test = _test_payload(contract)
    test["definition"]["assertions"] = [
        item
        for item in test["definition"]["assertions"]
        if item["key"] != "invoice_amount_to_purchase_order"
    ]

    with pytest.raises(
        cycle_vouching.CycleSchemaError, match="Role 'purchase_order' is declared"
    ):
        _validate(contract, test)


def test_payroll_stages_its_own_chronology_through_the_same_rule():
    """The rule is registry-owned, so a second pack needs no new code."""

    order = DEFAULT_REGISTRY.date_lifecycle_order
    assert order("payroll", "payroll.time_record", "dates", "document_date") < order(
        "payroll", "payroll.bank_payment", "dates", "document_date"
    )
    assert order("payroll", "payroll.payslip", "dates", "pay_period_end") == order(
        "payroll", "payroll.payroll_register", "dates", "pay_period_end"
    )
    with pytest.raises(Exception):
        DEFAULT_REGISTRY.date_lifecycle_order(
            "payroll", "procure_to_pay.purchase_order", "dates", "document_date"
        )


# --- Tier 3: making the population choice visible --------------------------


def test_candidates_carry_a_rank_in_their_deterministic_order(contract):
    manifest = _manifest(contract)

    assert manifest["groups"][0]["candidates"][0]["rank"] == 1


def test_gate_rejects_a_lower_ranked_candidate_over_the_same_table(contract):
    """The same rows keyed differently are not a lifecycle choice."""

    test = _test_payload(contract)
    manifest = _manifest(contract)
    candidates = manifest["groups"][0]["candidates"]
    best = copy.deepcopy(candidates[0])
    best["candidate_id"] = "CYCLE-CAND-INVOICE-OWN-KEY"
    best["rank"] = 1
    candidates[0]["rank"] = 2
    candidates.insert(0, best)

    with pytest.raises(
        cycle_vouching.CycleSchemaError, match="CYCLE-CAND-INVOICE-OWN-KEY"
    ):
        _validate(contract, test, manifest=manifest)


def test_gate_leaves_a_different_table_to_the_authoring_turn(contract):
    """Another table is another grain, and that is a real decision."""

    test = _test_payload(contract)
    manifest = _manifest(contract)
    candidates = manifest["groups"][0]["candidates"]
    other = copy.deepcopy(candidates[0])
    other["candidate_id"] = "CYCLE-CAND-PO"
    other["table"] = "po_data"
    other["rank"] = 1
    candidates[0]["rank"] = 2
    candidates.insert(0, other)

    validated = _validate(contract, test, manifest=manifest)

    assert validated["definition"]["population"]["table"] == "invoice_data"
