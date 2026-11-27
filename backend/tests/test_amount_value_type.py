"""The ``amount`` value type: a sum of money that keeps the currency it is in.

Two failures motivated it, both seen on the procurement engagement. A voucher's
paid amount was declared ``text`` because "PKR 2,900,000.00" is not a bare
number, so it never paired with the invoice it settled. And ``number`` reduced
"PKR 2,900,000.00" and "USD 2,900,000" to the same 2900000, so two sums in
different currencies read as one.
"""

from __future__ import annotations

import pytest

from app import cycle_field_pairs, cycle_linking, cycle_vouching, document_schemas
from app.agent import runner  # noqa: F401  (import order: workers need the runtime)
from app.agent.workers import documents as document_workers
from app.agent.workers.fieldwork import (
    CYCLE_VOUCH_ITEM_SOURCE_ID,
    CYCLE_VOUCH_SYSTEM,
    validate_cycle_vouch_proposal,
)
from app.agent.workers.model import WorkerResponseValidationError
from app.workspaces import WorkspaceError


# ------------------------------------------------------------ normalization
def _amount(raw, context=None):
    return cycle_vouching.normalize_evidence_value(
        raw, semantic_type="amount", citation="c1", context=context
    )


def test_an_amount_keeps_the_currency_it_is_printed_with():
    envelope = _amount("PKR 2,900,000.00")
    assert envelope["value"] == 2900000
    assert envelope["currency"] == "PKR"


def test_a_bare_amount_takes_its_currency_from_the_line_it_was_read_from():
    envelope = _amount("2,900,000", context="Invoice amount (PKR)   2,900,000")
    assert (envelope["value"], envelope["currency"]) == (2900000, "PKR")


def test_an_unstated_currency_is_left_unstated_rather_than_guessed():
    assert _amount("2,900,000", context="Total   2,900,000")["currency"] is None
    # Two currencies on one line do not say which the figure is in.
    assert _amount("2,900,000", context="USD 10,000 at 290 = PKR")["currency"] is None


def test_lower_case_words_are_not_read_as_currency_codes():
    assert cycle_vouching.stated_currency("all items, top up") is None
    assert cycle_vouching.stated_currency("ALL 500") == "ALL"


def test_a_symbol_naming_several_currencies_is_kept_as_printed():
    assert _amount("$1,200")["currency"] == "$"
    assert _amount("€1.200,00")["currency"] == "EUR"
    assert _amount("Rs. 500")["currency"] == "Rs"


def test_a_number_carries_no_currency_and_normalizes_as_before():
    envelope = cycle_vouching.normalize_evidence_value(
        "PKR 2,900,000.00", semantic_type="number", citation="c1"
    )
    assert envelope["value"] == 2900000
    assert "currency" not in envelope


def test_an_amount_that_is_a_date_is_invalid_evidence():
    envelope = _amount("19 Apr 2024")
    assert envelope["normalization_status"] == "invalid"
    assert envelope["currency"] is None


# --------------------------------------------------------------- resolution
class _Prepared:
    def __init__(self, records):
        self.by_id = {record["id"]: record for record in records}

    def field_definition(self, role, field):
        return {"name": field, "value_type": "amount"}


def _resolve(*values):
    records = [
        {"id": f"r{index}", "fields": [{"name": "total", "value": value, "citation": "c"}]}
        for index, value in enumerate(values)
    ]
    item = {
        "role_bindings": [
            {"role": "invoice", "record_id": record["id"], "document_id": "d"}
            for record in records
        ]
    }
    return cycle_linking.resolve_operand(
        _Prepared(records), item, {"role": "invoice", "field": "total"}, {}
    )


def test_one_record_resolves_to_its_figure_and_currency():
    resolved = _resolve("PKR 2,900,000.00")
    assert (resolved["state"], resolved["value"], resolved["currency"]) == (
        "resolved", 2900000, "PKR",
    )


def test_records_stating_one_figure_in_two_currencies_are_ambiguous():
    assert _resolve("PKR 2,900,000", "USD 2,900,000")["state"] == "ambiguous"


def test_a_record_leaving_the_currency_unstated_does_not_contradict_one_stating_it():
    resolved = _resolve("PKR 2,900,000", "2,900,000")
    assert (resolved["state"], resolved["currency"]) == ("resolved", "PKR")


# ------------------------------------------------------------------ judging
def _request(left_currency, right_currency):
    class _Item:
        source_id = CYCLE_VOUCH_ITEM_SOURCE_ID
        content = {
            "item_id": "itm_1",
            "checks": [{
                "check_id": "as_paid",
                "operands": [
                    {"operand": "payment.payment_amount", "value": "2,900,000",
                     "currency": left_currency},
                    {"operand": "invoice.invoice_amount", "value": "2,900,000",
                     "currency": right_currency},
                ],
            }],
        }

    class _Request:
        context = type("Bundle", (), {"items": (_Item(),)})()

    return _Request()


def _agrees():
    return {"cells": [{
        "check_id": "as_paid",
        "verdict": "agrees",
        "compared": [
            {"operand": "payment.payment_amount", "value": "2,900,000"},
            {"operand": "invoice.invoice_amount", "value": "2,900,000"},
        ],
        "reason": "Both state 2,900,000.",
    }]}


def test_amounts_in_two_stated_currencies_never_agree():
    cell = validate_cycle_vouch_proposal(_agrees(), _request("PKR", "USD"))["cells"][0]
    assert cell["verdict"] == "disagrees"
    assert "PKR, USD" in cell["reason"]


def test_a_currency_stated_on_one_side_only_is_left_to_the_reader():
    cell = validate_cycle_vouch_proposal(_agrees(), _request("PKR", None))["cells"][0]
    assert cell["verdict"] == "agrees"


# ------------------------------------------------------------- schema store
def _field(**overrides):
    field = {
        "name": "payment_amount", "role": "attribute", "value_type": "amount",
        "cardinality": "one", "verbatim": True, "confidence": "high", "label": "",
    }
    field.update(overrides)
    return field


def test_the_schema_store_accepts_an_amount_and_still_types_identifiers_strictly():
    assert document_schemas.validate_fields([_field()])[0]["value_type"] == "amount"
    with pytest.raises(WorkspaceError):
        document_schemas.validate_fields([_field(role="identifier")])


def test_the_reader_offers_exactly_the_store_vocabulary():
    assert set(document_workers._VALUE_TYPES) == document_schemas.VALUE_TYPES
    assert "amount     — a sum of money" in document_workers._read_system("")


# ------------------------------------------------------ the declaration check
@pytest.mark.parametrize(
    "value",
    ["PKR 2,900,000.00", "2,900,000", "20 Laptops", "$1,200", "12.5%", "(1,200.00)"],
)
def test_a_printed_figure_is_recognized(value):
    assert document_workers._printed_figure(value)


@pytest.mark.parametrize(
    "value",
    ["44000", "Office 4, 2nd Floor", "V1034", "19 Apr 2024", "Laptop refresh",
     "2024-04-06", "No. 5"],
)
def test_codes_addresses_and_dates_are_not_figures(value):
    assert not document_workers._printed_figure(value)


def _reading(value_type, value):
    return {
        "records": [{"fields": []}],
        "document_fields": [],
        "renames": [],
        "audit_notes": [],
        "citations": [{"id": "1", "page": 1, "excerpt": f"Amount paid {value}"}],
        "new_fields": [{
            **_field(value_type=value_type),
            "scope": "record",
            "reason": "The voucher states the amount paid.",
            "values": [{"record": 1, "entry": 1, "value": value, "citation": "1"}],
        }],
    }


def test_a_figure_declared_as_text_is_sent_back():
    proposal = _reading("text", "PKR 2,900,000.00")
    with pytest.raises(WorkerResponseValidationError, match="never compared as one"):
        document_workers._validate_new_field_types(proposal["new_fields"])


def test_text_that_is_words_is_kept_as_text():
    document_workers._validate_new_field_types(_reading("text", "Paid in full")["new_fields"])
    document_workers._validate_new_field_types(_reading("amount", "PKR 2,900,000.00")["new_fields"])


# -------------------------------------------------------------- field pairs
_ROLES = [
    {"name": "invoice", "document_type": "vendor_invoice"},
    {"name": "payment", "document_type": "payment_voucher"},
]


def _schemas(paid_type):
    return [
        {"document_type": "vendor_invoice", "fields": [
            {"name": "invoice_amount", "role": "attribute", "value_type": "amount"},
            {"name": "quantity", "role": "attribute", "value_type": "number"},
        ]},
        {"document_type": "payment_voucher", "fields": [
            {"name": "payment_amount", "role": "attribute", "value_type": paid_type},
        ]},
    ]


def _pairs(paid_type):
    found = cycle_field_pairs.candidates(_schemas(paid_type), _ROLES)["pairs"]
    return {(pair["left"]["field"], pair["right"]["field"]) for pair in found}


def test_the_amount_paid_pairs_with_the_amount_invoiced():
    assert ("invoice_amount", "payment_amount") in _pairs("amount")


def test_an_amount_typed_number_on_one_side_still_pairs_by_name():
    assert ("invoice_amount", "payment_amount") in _pairs("number")


def test_an_unpaired_amount_is_listed_for_the_review():
    schemas = _schemas("amount")
    schemas[1]["fields"][0]["name"] = "settled_sum"
    schemas[0]["fields"][0]["name"] = "billed"
    listed = cycle_field_pairs.unpaired_fields(schemas, _ROLES, [])
    assert {"role": "payment", "field": "settled_sum", "no_pair_with": ["invoice"]} in [
        {key: item[key] for key in ("role", "field", "no_pair_with")} for item in listed
    ]
