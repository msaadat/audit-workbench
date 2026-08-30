"""Measuring a ruleset against the corpus — above all, fan-out."""

from __future__ import annotations

import pytest

from app import cycle_measurement, document_analysis, document_classification as dc
from app import document_schemas, documents, workspaces

INVOICE_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "vendor_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]

ORDER_FIELDS = [
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "vendor_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


@pytest.fixture
def ws() -> workspaces.Workspace:
    workspace = workspaces.create_workspace("Measurement")
    document_schemas.save_schema(workspace, "vendor_invoice", INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", ORDER_FIELDS)
    return workspace


def _extract(ws, name: str, document_type: str, **values) -> str:
    """Store a structured extraction the way the reduction executor would."""

    document = documents.add_document(ws, name, b"source text", category="voucher")
    dc.assign(ws, str(document["id"]), document_type, assigned_by="model")
    schema = document_schemas.get_schema(ws, document_type)
    document_analysis.persist_analysis(
        ws, document, {"pages": [{"page": 1, "text": "source text"}]},
        {
            "analysis_profile": "structured",
            "summary_markdown": "s",
            "audit_notes_markdown": "n",
            "schema_ref": {
                "document_type": schema["document_type"],
                "schema_version": schema["schema_version"],
                "schema_hash": schema["schema_hash"],
            },
            "records": [{
                "fields": [
                    {"name": key, "entry": 1, "value": value, "citation": "c1"}
                    for key, value in values.items()
                ],
                "additional_fields": [],
            }],
        },
        provider="local", model="test",
    )
    return str(document["id"])


def _ruleset(join_field: str = "order_number") -> dict:
    return {
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice"},
            {"name": "order", "document_type": "purchase_order"},
        ],
        "join_keys": [{
            "id": "jk",
            "left": {"role": "invoice", "field": join_field},
            "right": {"role": "order", "field": join_field},
        }],
        "assertions": [{
            "id": "as_total", "requirement": "The records must agree.",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
        }],
    }


# ----------------------------------------------------------------- fan-out
def test_a_transaction_key_reaches_about_one_record_each(ws):
    for index in range(3):
        _extract(ws, f"inv{index}.txt", "vendor_invoice",
                 invoice_number=f"INV-{index}", order_number=f"PO-{index}",
                 vendor_id="V-1", total_amount="100")
        _extract(ws, f"po{index}.txt", "purchase_order",
                 order_number=f"PO-{index}", vendor_id="V-1", total_amount="100")

    measured = cycle_measurement.measure(ws, _ruleset())["join_keys"]["jk"]
    assert measured["fan_out_p95"] == 1
    assert measured["matched_pairs"] == 3
    assert measured["left_unmatched"] == 0
    assert cycle_measurement.concerns({"join_keys": {"jk": measured}}) == []


def test_an_entity_key_is_visible_as_runaway_fan_out(ws):
    """The most damaging mistake available here, and invisible in the rule text:
    joining on a vendor id would fuse every unrelated transaction."""

    for index in range(6):
        _extract(ws, f"inv{index}.txt", "vendor_invoice",
                 invoice_number=f"INV-{index}", order_number=f"PO-{index}",
                 vendor_id="V-1", total_amount="100")
        _extract(ws, f"po{index}.txt", "purchase_order",
                 order_number=f"PO-{index}", vendor_id="V-1", total_amount="100")

    measured = cycle_measurement.measure(ws, _ruleset("vendor_id"))
    # Every invoice reaches every order: the entity pattern exactly.
    assert measured["join_keys"]["jk"]["fan_out_p95"] == 6
    raised = cycle_measurement.concerns(measured)
    assert [item["concern"] for item in raised] == ["entity_fan_out"]
    assert "fuse unrelated transactions" in raised[0]["detail"]


def test_poor_coverage_points_back_at_the_field(ws):
    for index in range(4):
        _extract(ws, f"inv{index}.txt", "vendor_invoice",
                 invoice_number=f"INV-{index}", order_number=f"PO-{index}",
                 total_amount="100")
    _extract(ws, "po0.txt", "purchase_order", order_number="PO-0", total_amount="100")

    measured = cycle_measurement.measure(ws, _ruleset())
    assert measured["join_keys"]["jk"]["left_unmatched"] == 3
    concerns = [item["concern"] for item in cycle_measurement.concerns(measured)]
    assert "poor_coverage" in concerns


def test_a_record_not_stating_the_key_is_not_counted_against_it(ws):
    """A document the rule has nothing to say about is not evidence it is bad."""

    _extract(ws, "inv0.txt", "vendor_invoice", invoice_number="INV-0",
             order_number="PO-0", total_amount="100")
    _extract(ws, "inv1.txt", "vendor_invoice", invoice_number="INV-1",
             total_amount="100")
    _extract(ws, "po0.txt", "purchase_order", order_number="PO-0", total_amount="100")

    measured = cycle_measurement.measure(ws, _ruleset())["join_keys"]["jk"]
    assert measured["left_documents"] == 2
    assert measured["left_stating_key"] == 1
    assert measured["left_unmatched"] == 0


def test_matching_ignores_presentation_but_not_punctuation(ws):
    """`PO-1` and `po 1` are one reference written twice; `PO-1-A` is another."""

    _extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-0",
             order_number="  PO-1  ", total_amount="100")
    _extract(ws, "po.txt", "purchase_order", order_number="po-1", total_amount="100")
    _extract(ws, "po2.txt", "purchase_order", order_number="PO-1-A", total_amount="100")

    measured = cycle_measurement.measure(ws, _ruleset())["join_keys"]["jk"]
    assert measured["matched_pairs"] == 1


# -------------------------------------------------------------- assertions
def test_an_assertion_nothing_can_test_is_reported_as_silent(ws):
    """A rule that never runs looks the same as one that always passes."""

    _extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-0",
             order_number="PO-0")
    _extract(ws, "po.txt", "purchase_order", order_number="PO-0")

    measured = cycle_measurement.measure(ws, _ruleset())
    assert measured["assertions"]["as_total"]["silent"] is True
    assert [item["concern"] for item in cycle_measurement.concerns(measured)] == ["silent"]


def test_an_evaluable_assertion_counts_the_smaller_side(ws):
    _extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-0",
             order_number="PO-0", total_amount="100")
    for index in range(3):
        _extract(ws, f"po{index}.txt", "purchase_order",
                 order_number=f"PO-{index}", total_amount="100")

    measured = cycle_measurement.measure(ws, _ruleset())["assertions"]["as_total"]
    assert measured["left_stating"] == 1
    assert measured["right_stating"] == 3
    assert measured["evaluable_records"] == 1
    assert measured["silent"] is False


# ------------------------------------------------------------- staleness
def test_an_extraction_made_against_a_stale_schema_is_excluded(ws):
    """Reading it under today's schema would attribute values to a vocabulary
    that never produced them."""

    _extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-0",
             order_number="PO-0", total_amount="100")
    assert len(cycle_measurement.structured_records(ws)) == 1

    document_schemas.save_schema(
        ws, "vendor_invoice",
        [*INVOICE_FIELDS, {"name": "currency", "role": "attribute",
                           "value_type": "text", "cardinality": "one",
                           "verbatim": True, "confidence": "low"}],
    )
    assert cycle_measurement.structured_records(ws) == []


def test_measuring_an_empty_corpus_reports_zero_rather_than_failing(ws):
    measured = cycle_measurement.measure(ws, _ruleset())
    assert measured["records_measured"] == 0
    assert measured["join_keys"]["jk"]["fan_out_p95"] == 0
    assert measured["assertions"]["as_total"]["silent"] is True


# ----------------------------------------------------------------- migration
def test_a_legacy_pack_analysis_is_named_rather_than_silently_skipped(ws):
    """The failure this whole design exists to remove: a document that
    contributes nothing and says nothing about why."""

    document = documents.add_document(ws, "old.txt", b"source", category="voucher")
    document_analysis.persist_analysis(
        ws, document, {"pages": [{"page": 1, "text": "source"}]},
        {
            "analysis_profile": "voucher",
            "summary_markdown": "Read under a pack that no longer exists.",
            "audit_notes_markdown": "n",
            "records": [{"record_kind": "procure_to_pay.vendor_invoice"}],
        },
        provider="local", model="test",
    )

    excluded: list[dict] = []
    rows = cycle_measurement.structured_records(ws, excluded=excluded)

    assert rows == []
    assert [item["reason"] for item in excluded] == ["legacy_pack_analysis"]
    # And it is still readable: re-extraction is an offered action, not a
    # precondition for opening the workspace.
    stored = document_analysis.load_analysis(ws, str(document["id"]))["effective"]
    assert stored["summary_markdown"].startswith("Read under a pack")


def test_a_document_retyped_since_extraction_is_named_rather_than_misfiled(ws):
    """The stamp is current, so the staleness check passes it. It is still wrong.

    Nothing about ``vendor_invoice``'s schema moved, so ``is_current`` says yes
    and the row would be filed under a type the auditor has said this document
    is not. Excluded and named, because a document that silently contributes
    under the wrong type is worse than one that contributes nothing.
    """

    document_id = _extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1")
    dc.retype(ws, document_id, coin="Internal deal confirmation")
    reloaded = workspaces.load_workspace(ws.id)

    excluded: list[dict] = []

    assert cycle_measurement.structured_records(reloaded, excluded=excluded) == []
    assert excluded == [
        {"document_id": document_id, "reason": "retyped_since_extraction"}
    ]


def test_an_extraction_against_a_schema_that_moved_is_named_too(ws):
    document_id = _extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1")
    document_schemas.save_schema(ws, "vendor_invoice", [
        *INVOICE_FIELDS,
        {"name": "currency", "role": "attribute", "value_type": "text",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ])

    excluded: list[dict] = []

    assert cycle_measurement.structured_records(ws, excluded=excluded) == []
    assert excluded == [
        {"document_id": document_id, "reason": "stale_schema_reference"}
    ]
