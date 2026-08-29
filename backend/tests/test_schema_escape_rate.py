"""The escape-rate metric: what catches an unrepresentative induction sample."""

from __future__ import annotations

import pytest

from app import document_schemas, workspaces


def _extraction(*escaped: str) -> dict:
    return {
        "records": [
            {
                "fields": [{"name": "invoice_number", "value": "INV-1"}],
                "additional_fields": [{"name": name, "value": "x"} for name in escaped],
            }
        ]
    }


@pytest.fixture
def ws() -> workspaces.Workspace:
    workspace = workspaces.create_workspace("Escape rate")
    document_schemas.save_schema(
        workspace,
        "vendor_invoice",
        [{"name": "invoice_number", "role": "identifier", "value_type": "identifier",
          "cardinality": "one", "verbatim": True, "confidence": "high"}],
    )
    return workspace


def test_a_schema_that_holds_every_document_escapes_nothing(ws):
    measured = document_schemas.escape_rate(
        ws, "vendor_invoice", [_extraction(), _extraction(), _extraction()]
    )
    assert measured["rate"] == 0.0
    assert measured["fields"] == []
    assert measured["unrepresentative"] is False


def test_one_unusual_document_does_not_condemn_the_schema(ws):
    """An occasional one-off is a document being unusual, not a schema failing."""

    measured = document_schemas.escape_rate(
        ws, "vendor_invoice",
        [_extraction("incoterm")] + [_extraction() for _ in range(9)],
    )
    assert measured["rate"] == pytest.approx(0.1)
    assert measured["unrepresentative"] is False


def test_a_field_escaping_widely_marks_the_schema_unrepresentative(ws):
    """The safety net for small-n induction: two samples agreeing tells you
    little when the corpus is heterogeneous."""

    measured = document_schemas.escape_rate(
        ws, "vendor_invoice",
        [_extraction("vat_amount") for _ in range(4)] + [_extraction()],
    )
    assert measured["rate"] == pytest.approx(0.8)
    assert measured["unrepresentative"] is True
    assert measured["fields"][0]["name"] == "vat_amount"
    assert measured["fields"][0]["documents"] == 4


def test_fields_are_ranked_by_how_widely_they_escape(ws):
    """Breadth is what says the *type* carries a field, not one document."""

    measured = document_schemas.escape_rate(
        ws, "vendor_invoice",
        [_extraction("vat_amount", "incoterm"), _extraction("vat_amount"), _extraction()],
    )
    assert [field["name"] for field in measured["fields"]] == ["vat_amount", "incoterm"]
    assert measured["fields"][0]["rate"] == pytest.approx(2 / 3)


def test_a_field_escaping_twice_in_one_document_counts_once(ws):
    """The measure is how many documents need the field, not how many times it
    appears — a repeated party on one invoice is one document's evidence."""

    extraction = {
        "records": [
            {"additional_fields": [{"name": "vat_amount"}]},
            {"additional_fields": [{"name": "vat_amount"}]},
        ]
    }
    measured = document_schemas.escape_rate(ws, "vendor_invoice", [extraction])
    assert measured["fields"][0]["documents"] == 1
    assert measured["documents_with_escapes"] == 1


def test_the_metric_carries_the_schema_version_it_measured(ws):
    measured = document_schemas.escape_rate(ws, "vendor_invoice", [_extraction()])
    assert measured["schema_version"] == 1


def test_no_extractions_is_not_a_verdict(ws):
    """Nothing measured is not evidence the schema is wrong."""

    measured = document_schemas.escape_rate(ws, "vendor_invoice", [])
    assert measured["documents"] == 0
    assert measured["rate"] == 0.0
    assert measured["unrepresentative"] is False
