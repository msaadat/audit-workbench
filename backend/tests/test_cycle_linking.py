"""Linking, role binding and evaluation against an approved ruleset.

The registry engine's tests proved the same properties against packs. These
prove them against rules an auditor approved, which is the only thing that
changed: the graph is still bounded, an ambiguous role is still surfaced rather
than resolved, and a comparison still refuses to run on evidence it cannot type.
"""

from __future__ import annotations

import pytest

from app import (
    cycle_linking,
    cycle_measurement,
    cycle_rulesets,
    document_analysis,
    document_classification as dc,
    document_schemas,
    documents,
    workspaces,
)

INVOICE_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "vendor_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "invoice_date", "role": "attribute", "value_type": "date",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]

ORDER_FIELDS = [
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "vendor_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "order_date", "role": "attribute", "value_type": "date",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


@pytest.fixture
def ws() -> workspaces.Workspace:
    workspace = workspaces.create_workspace("Linking")
    document_schemas.save_schema(workspace, "vendor_invoice", INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", ORDER_FIELDS)
    return workspace


def extract(ws, name: str, document_type: str, **values) -> str:
    """Store one structured extraction the way the reduction executor would."""

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
            "citations": [{
                "id": "c1", "page": 1, "excerpt": "source text",
                "excerpt_hash": "sha1:" + "0" * 40,
            }],
        },
        provider="local", model="test",
    )
    return str(document["id"])


def ruleset_payload(**overrides) -> dict:
    payload = {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice"},
            {"name": "order", "document_type": "purchase_order"},
        ],
        "anchor": {
            "table": "invoices", "column": "INVOICE_NO",
            "role": "invoice", "field": "invoice_number",
        },
        "join_keys": [{
            "id": "jk_order",
            "left": {"role": "invoice", "field": "order_number"},
            "right": {"role": "order", "field": "order_number"},
            "match": "normalized_equal",
            "rationale": "An invoice cites the order it bills against.",
        }],
        "assertions": [{
            "id": "as_total", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "operator": "numeric_within", "tolerance": {"absolute": 1},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }
    payload.update(overrides)
    return payload


def approved(ws, **overrides) -> dict:
    record = cycle_rulesets.save(ws, ruleset_payload(**overrides), proposed_by="agent")
    return cycle_rulesets.approve(
        ws, record["ruleset_id"], approved_by="auditor@example.com"
    )


# ----------------------------------------------------------------- evidence
def test_a_record_carries_its_type_and_a_content_addressed_id(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")

    records, hashes = cycle_linking.structured_evidence(ws)

    assert [record["document_type"] for record in records] == ["vendor_invoice"]
    assert records[0]["record_id"].startswith("REC-")
    assert hashes[records[0]["document_id"]]


def test_a_changed_extraction_is_a_different_record(ws):
    """Not a staleness check that has to be remembered — a different identity."""

    first = cycle_linking.record_id("doc-1", 0, {"fields": [{"name": "a", "value": "1"}]})
    second = cycle_linking.record_id("doc-1", 0, {"fields": [{"name": "a", "value": "2"}]})

    assert first != second


def test_an_analysis_against_a_superseded_schema_is_not_evidence(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1", total_amount="100")
    document_schemas.save_schema(
        ws, "vendor_invoice", [*INVOICE_FIELDS,
                               {"name": "currency", "role": "attribute",
                                "value_type": "text", "cardinality": "one",
                                "verbatim": True, "confidence": "high"}]
    )

    records, _ = cycle_linking.structured_evidence(ws)

    assert records == []


# ------------------------------------------------------------------ linking
def test_an_approved_join_key_links_the_two_documents(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po.txt", "purchase_order", order_number="PO-1", total_amount="100")

    prepared = cycle_linking.prepare(ws, approved(ws))
    linkage = cycle_linking.link(prepared, anchor_values=["INV-1"])

    assert linkage["state"] == "linked"
    assert sorted(binding["role"] for binding in linkage["role_bindings"]) == [
        "invoice", "order"
    ]
    assert linkage["counts"]["records"] == 2


def test_nothing_links_without_an_approved_join_key(ws):
    """The edges are the rules. Removing the rule removes the edge.

    Assembled directly rather than stored: the store refuses a ruleset with an
    unreachable role, which is the same fact stated at authoring time.
    """

    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po.txt", "purchase_order", order_number="PO-1", total_amount="100")

    prepared = cycle_linking.prepare(
        ws, {**ruleset_payload(join_keys=[]), "ruleset_hash": "sha256:none"}
    )
    linkage = cycle_linking.link(prepared, anchor_values=["INV-1"])

    assert [binding["role"] for binding in linkage["role_bindings"]] == ["invoice"]
    assert linkage["counts"]["records"] == 1


def test_an_unrelated_transaction_is_not_reached(ws):
    extract(ws, "inv1.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po1.txt", "purchase_order", order_number="PO-1", total_amount="100")
    extract(ws, "po2.txt", "purchase_order", order_number="PO-2", total_amount="999")

    prepared = cycle_linking.prepare(ws, approved(ws))
    linkage = cycle_linking.link(prepared, anchor_values=["INV-1"])

    reached = {record["document_type"] for record in linkage["records"]}
    assert reached == {"vendor_invoice", "purchase_order"}
    assert linkage["counts"]["records"] == 2


def test_the_anchor_matches_a_differently_written_reference(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="inv 1",
            order_number="PO-1", total_amount="100")

    prepared = cycle_linking.prepare(ws, approved(ws))

    assert cycle_linking.link(prepared, anchor_values=["INV 1"])["counts"]["records"] == 1


def test_exact_match_mode_does_not_fold_case(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="po-1", total_amount="100")
    extract(ws, "po.txt", "purchase_order", order_number="PO-1", total_amount="100")

    ruleset = approved(ws, join_keys=[{
        "id": "jk_order",
        "left": {"role": "invoice", "field": "order_number"},
        "right": {"role": "order", "field": "order_number"},
        "match": "exact_equal",
        "rationale": "This vendor's references are case-significant.",
    }])
    linkage = cycle_linking.link(
        cycle_linking.prepare(ws, ruleset), anchor_values=["INV-1"]
    )

    assert [binding["role"] for binding in linkage["role_bindings"]] == ["invoice"]


def test_two_candidates_for_a_single_role_are_surfaced_not_picked(ws):
    """Which order this invoice bills is a question the evidence did not answer."""

    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po1.txt", "purchase_order", order_number="PO-1", total_amount="100")
    extract(ws, "po2.txt", "purchase_order", order_number="PO-1", total_amount="140")

    linkage = cycle_linking.link(
        cycle_linking.prepare(ws, approved(ws)), anchor_values=["INV-1"]
    )

    assert linkage["state"] == "needs_review"
    assert [conflict["role"] for conflict in linkage["role_conflicts"]] == ["order"]
    assert [binding["role"] for binding in linkage["role_bindings"]] == ["invoice"]


def test_a_many_role_binds_every_match(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po1.txt", "purchase_order", order_number="PO-1", total_amount="100")
    extract(ws, "po2.txt", "purchase_order", order_number="PO-1", total_amount="100")

    ruleset = approved(ws, roles=[
        {"name": "invoice", "document_type": "vendor_invoice"},
        {"name": "order", "document_type": "purchase_order", "cardinality": "many"},
    ])
    linkage = cycle_linking.link(
        cycle_linking.prepare(ws, ruleset), anchor_values=["INV-1"]
    )

    assert linkage["state"] == "linked"
    assert sum(1 for b in linkage["role_bindings"] if b["role"] == "order") == 2


def test_the_traversal_is_bounded_and_says_which_limit_it_hit(ws):
    for index in range(6):
        extract(ws, f"po{index}.txt", "purchase_order",
                order_number="PO-1", total_amount="100")
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")

    ruleset = approved(ws, roles=[
        {"name": "invoice", "document_type": "vendor_invoice"},
        {"name": "order", "document_type": "purchase_order", "cardinality": "many"},
    ])
    linkage = cycle_linking.link(
        cycle_linking.prepare(ws, ruleset), anchor_values=["INV-1"], max_records=3
    )

    assert linkage["state"] == "needs_review"
    assert linkage["review_reason"] == "graph_records_limit_exceeded"
    assert linkage["limit"] == "records"


def test_the_edge_records_which_rule_and_value_produced_it(ws):
    extract(ws, "inv.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po.txt", "purchase_order", order_number="PO-1", total_amount="100")

    linkage = cycle_linking.link(
        cycle_linking.prepare(ws, approved(ws)), anchor_values=["INV-1"]
    )
    order = next(b for b in linkage["role_bindings"] if b["role"] == "order")

    assert order["matched_by"][0]["join_key"] == "jk_order"
    assert order["matched_by"][0]["normalized_value"] == "po-1"


# ------------------------------------------------------- fan-out parity
def test_the_linker_reaches_exactly_what_measurement_reported(ws):
    """An auditor approves a join key on its measured fan-out. If the engine
    reaches a different number, they approved a rule it does not honour."""

    for index in range(4):
        extract(ws, f"inv{index}.txt", "vendor_invoice",
                invoice_number=f"INV-{index}", order_number=f"PO-{index}",
                total_amount="100")
        extract(ws, f"po{index}.txt", "purchase_order",
                order_number=f"PO-{index}", total_amount="100")

    ruleset = approved(ws)
    measured = cycle_measurement.measure(ws, ruleset)["join_keys"]["jk_order"]
    prepared = cycle_linking.prepare(ws, ruleset)

    reached = []
    for index in range(4):
        linkage = cycle_linking.link(prepared, anchor_values=[f"INV-{index}"])
        reached.append(
            sum(1 for b in linkage["role_bindings"] if b["role"] == "order")
        )

    assert max(reached) == measured["fan_out_max"]
    assert sum(reached) == measured["matched_pairs"]


def test_an_entity_key_fuses_the_corpus_exactly_as_measured(ws):
    for index in range(6):
        extract(ws, f"inv{index}.txt", "vendor_invoice",
                invoice_number=f"INV-{index}", vendor_id="V-1", total_amount="100")
        extract(ws, f"po{index}.txt", "purchase_order",
                order_number=f"PO-{index}", vendor_id="V-1", total_amount="100")

    ruleset = approved(ws, join_keys=[{
        "id": "jk_vendor",
        "left": {"role": "invoice", "field": "vendor_id"},
        "right": {"role": "order", "field": "vendor_id"},
        "match": "normalized_equal",
        "rationale": "Both name the vendor.",
    }], roles=[
        {"name": "invoice", "document_type": "vendor_invoice"},
        {"name": "order", "document_type": "purchase_order", "cardinality": "many"},
    ])
    measured = cycle_measurement.measure(ws, ruleset)["join_keys"]["jk_vendor"]
    linkage = cycle_linking.link(
        cycle_linking.prepare(ws, ruleset), anchor_values=["INV-0"]
    )

    assert measured["fan_out_p95"] == 6
    assert [item["concern"] for item in cycle_measurement.concerns(
        {"join_keys": {"jk_vendor": measured}}
    )] == ["entity_fan_out"]
    assert sum(1 for b in linkage["role_bindings"] if b["role"] == "order") == 6
