"""Phase 1 gates for registry-backed extraction reduction and exact linkage."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app import cycle_vouching, workspaces
from app.cycle_registry import CycleRegistry, DEFAULT_REGISTRY


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fragment(
    reference: dict,
    *,
    chunk: str,
    record_kind: str,
    identifiers: list[tuple[str, str]],
    citation: str | None = None,
) -> dict:
    citation = citation or f"{chunk}-C1"
    return {
        "registry": reference,
        "chunk_id": chunk,
        "page_span": [1, 1],
        "record_kind": record_kind,
        "classification_evidence": [citation],
        "identifiers": [
            {
                "kind": kind,
                "value": {
                    "raw_value": value,
                    "value": value,
                    "normalization_status": "normalized",
                    "normalization_error": None,
                    "citation": citation,
                },
            }
            for kind, value in identifiers
        ],
        "fields": [],
    }


def test_multi_chunk_exact_primary_reduces_once_and_preserves_citations():
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    fragments = [
        _fragment(
            reference,
            chunk="CHUNK-1",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "V-INV-1"),
                ("procure_to_pay.purchase_order_number", "PO-1"),
            ],
        ),
        _fragment(
            reference,
            chunk="CHUNK-2",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "V-INV-1"),
                ("procure_to_pay.goods_receipt_number", "GRN-1"),
            ],
        ),
    ]

    reduction = cycle_vouching.reduce_record_fragments("DOC-INV", fragments)

    assert len(reduction["records"]) == 1
    record = reduction["records"][0]
    assert set(record["fragment_hashes"]) == {
        cycle_vouching.fragment_identity(
            cycle_vouching.normalize_record_fragment(fragment)
        )
        for fragment in fragments
    }
    invoice = next(
        value
        for value in record["identifiers"]
        if value["kind"] == "procure_to_pay.vendor_invoice_number"
    )
    assert invoice["value"]["citation"] == ["CHUNK-1-C1", "CHUNK-2-C1"]


@pytest.mark.parametrize(
    "identifier_kind",
    [
        "procure_to_pay.payment_voucher_number",
        "procure_to_pay.vendor_invoice_number",
        "procure_to_pay.internal_invoice_id",
    ],
)
def test_payment_voucher_accepts_declared_alternate_primary_identity(identifier_kind):
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    fragment = _fragment(
        reference,
        chunk="PAYMENT",
        record_kind="procure_to_pay.payment_voucher",
        identifiers=[(identifier_kind, "PAYMENT-IDENTITY")],
    )

    reduction = cycle_vouching.reduce_record_fragments("DOC-PAY", [fragment])

    assert not reduction["unresolved_fragments"]
    assert not reduction["conflicts"]
    assert len(reduction["records"]) == 1
    assert reduction["records"][0]["primary_identifier"] == {
        "kind": identifier_kind,
        "normalized_value": "payment-identity",
    }


def test_conflicting_primary_ids_never_merge_through_a_shared_secondary():
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    fragments = [
        _fragment(
            reference,
            chunk="A",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-A"),
                ("procure_to_pay.purchase_order_number", "PO-SHARED"),
            ],
        ),
        _fragment(
            reference,
            chunk="B",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-B"),
                ("procure_to_pay.purchase_order_number", "PO-SHARED"),
            ],
        ),
    ]

    reduction = cycle_vouching.reduce_record_fragments("DOC-COMBINED", fragments)

    assert len(reduction["records"]) == 2
    assert {record["primary_identifier"]["normalized_value"] for record in reduction["records"]} == {
        "inv-a",
        "inv-b",
    }


def test_primaryless_fragment_joins_only_one_exact_compatible_component():
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    fragments = [
        _fragment(
            reference,
            chunk="A",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-A"),
                ("procure_to_pay.purchase_order_number", "PO-A"),
            ],
        ),
        _fragment(
            reference,
            chunk="B",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-B"),
                ("procure_to_pay.purchase_order_number", "PO-B"),
            ],
        ),
        _fragment(
            reference,
            chunk="PARTIAL",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[("procure_to_pay.purchase_order_number", "PO-A")],
        ),
    ]

    reduction = cycle_vouching.reduce_record_fragments("DOC-COMBINED", fragments)

    assert not reduction["unresolved_fragments"]
    joined = next(
        record
        for record in reduction["records"]
        if record["primary_identifier"]["normalized_value"] == "inv-a"
    )
    assert len(joined["fragment_hashes"]) == 2


@pytest.mark.parametrize(
    ("secondary_values", "reason"),
    [(["PO-NONE"], "missing_identity"), (["PO-SHARED"], "ambiguous_identity")],
)
def test_zero_and_multiple_primaryless_matches_remain_unresolved(
    secondary_values, reason
):
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    shared = secondary_values[0] == "PO-SHARED"
    fragments = [
        _fragment(
            reference,
            chunk="A",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-A"),
                ("procure_to_pay.purchase_order_number", "PO-SHARED" if shared else "PO-A"),
            ],
        ),
        _fragment(
            reference,
            chunk="B",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-B"),
                ("procure_to_pay.purchase_order_number", "PO-SHARED" if shared else "PO-B"),
            ],
        ),
        _fragment(
            reference,
            chunk="PARTIAL",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[("procure_to_pay.purchase_order_number", secondary_values[0])],
        ),
    ]

    reduction = cycle_vouching.reduce_record_fragments("DOC-COMBINED", fragments)

    assert [value["reason"] for value in reduction["unresolved_fragments"]] == [reason]


def test_reviewed_fragment_assignment_is_exact_and_stales_on_reanalysis():
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    fragments = [
        _fragment(
            reference,
            chunk="A",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-A"),
                ("procure_to_pay.purchase_order_number", "PO-SHARED"),
            ],
        ),
        _fragment(
            reference,
            chunk="B",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[
                ("procure_to_pay.vendor_invoice_number", "INV-B"),
                ("procure_to_pay.purchase_order_number", "PO-SHARED"),
            ],
        ),
        _fragment(
            reference,
            chunk="PARTIAL",
            record_kind="procure_to_pay.vendor_invoice",
            identifiers=[("procure_to_pay.purchase_order_number", "PO-SHARED")],
        ),
    ]
    normalized = [cycle_vouching.normalize_record_fragment(value) for value in fragments]
    override = {
        "fragment_hash": cycle_vouching.fragment_identity(normalized[2]),
        "assign_to_fragment_hash": cycle_vouching.fragment_identity(normalized[0]),
    }

    reduction = cycle_vouching.reduce_record_fragments(
        "DOC-COMBINED", fragments, overrides=[override]
    )

    assert not reduction["unresolved_fragments"]
    assert max(len(record["fragment_hashes"]) for record in reduction["records"]) == 2
    changed = copy.deepcopy(fragments)
    changed[2]["identifiers"][0]["value"]["raw_value"] = "PO-CHANGED"
    with pytest.raises(cycle_vouching.CycleSchemaError, match="stale"):
        cycle_vouching.reduce_record_fragments(
            "DOC-COMBINED", changed, overrides=[override]
        )


def test_readable_dates_normalize_and_unreadable_dates_remain_explicitly_invalid():
    readable = cycle_vouching.normalize_evidence_value(
        "29-Apr -2024", semantic_type="date", citation="C1"
    )
    invalid = cycle_vouching.normalize_evidence_value(
        "April-ish", semantic_type="date", citation="C2"
    )

    assert readable["value"] == "2024-04-29"
    assert readable["normalization_status"] == "normalized"
    assert invalid == {
        "raw_value": "April-ish",
        "value": None,
        "normalization_status": "invalid",
        "normalization_error": "unrecognized date format",
        "citation": "C2",
    }


def test_a_numeric_date_with_two_readings_is_invalid_rather_than_resolved():
    """``04-01-2024`` is 4 January or 1 April; the record does not say which.

    Resolving it by the order the accepted formats happen to be listed decides a
    cut-off comparison silently, so the ambiguity is reported instead.
    """

    ambiguous = cycle_vouching.normalize_evidence_value(
        "04-01-2024", semantic_type="date", citation="C1"
    )
    assert ambiguous["value"] is None
    assert ambiguous["normalization_status"] == "invalid"
    assert ambiguous["normalization_error"] == "ambiguous day and month order"

    # A day past 12 has only one reading, and month names are never ambiguous.
    for raw, expected in (("19-04-2024", "2024-04-19"), ("01-Apr-2024", "2024-04-01")):
        resolved = cycle_vouching.normalize_evidence_value(
            raw, semantic_type="date", citation="C1"
        )
        assert resolved["value"] == expected
        assert resolved["normalization_status"] == "normalized"


def test_a_date_supplied_for_a_number_does_not_normalize_to_its_first_digits():
    """``19 Apr 2024`` scanned as a number yields 19, which reads as evidence.

    The map validator's type check can only send a misplaced value back for
    repair when local normalization actually fails, so this has to fail.
    """

    misplaced = cycle_vouching.normalize_evidence_value(
        "19 Apr 2024", semantic_type="number", citation="C1"
    )
    assert misplaced["value"] is None
    assert misplaced["normalization_status"] == "invalid"
    assert misplaced["normalization_error"] == "value is a date, not a number"


def test_two_numbers_in_one_raw_value_do_not_concatenate():
    """OCR that emits a label column and a value column separately produces
    ``25 25``; whitespace inside the digit scan made that 2525."""

    assert (
        cycle_vouching.normalize_evidence_value(
            "25 25", semantic_type="number", citation="C1"
        )["value"]
        == 25
    )
    assert (
        cycle_vouching.normalize_evidence_value(
            "Quantity received 25\n25", semantic_type="number", citation="C1"
        )["value"]
        == 25
    )
    # Ordinary presentation of one figure is unaffected.
    for raw, expected in (
        ("PKR 2,000,000.00", 2000000),
        ("25 Kits", 25),
        ("(1,200.50)", -1200.5),
    ):
        assert (
            cycle_vouching.normalize_evidence_value(
                raw, semantic_type="number", citation="C1"
            )["value"]
            == expected
        )


def test_record_manifest_reports_registered_attributes_not_envelope_keys():
    """Both the assertion validator and the authoring dialog read this list.

    Every envelope carries ``raw_value`` and ``value`` whatever the field kind
    declares, so deriving the list from the envelope advertised an approval as
    answering ``value`` — not one of its attributes — while hiding ``approver``,
    ``decision``, ``role``, and ``date``, which are. An approval could then be
    neither asserted nor offered.
    """

    record = {
        "registry": DEFAULT_REGISTRY.reference("procure_to_pay").to_dict(),
        "record_id": "REC-1",
        "document_id": "DOC-1",
        "record_kind": "procure_to_pay.purchase_requisition",
        "classification_evidence": ["C1"],
        "identifiers": [
            {
                "kind": "procure_to_pay.requisition_number",
                "value": {
                    "raw_value": "REQ-1",
                    "value": "req-1",
                    "normalization_status": "normalized",
                    "normalization_error": None,
                    "citation": "C1",
                },
            }
        ],
        "fields": [
            {
                "group": "approvals",
                "kind": "approval",
                "attribute": attribute,
                "entry": entry,
                "value": {
                    "raw_value": raw,
                    "value": raw,
                    "normalization_status": "normalized",
                    "normalization_error": None,
                    "citation": "C1",
                },
            }
            for attribute, raw, entry in (
                ("approver", "A. Khan", 0),
                ("role", "Finance", 0),
                ("approver", "B. Iqbal", 1),
            )
        ],
    }

    manifest = cycle_vouching._record_manifest(record, DEFAULT_REGISTRY)
    approvals = next(
        item
        for item in manifest["available_fields"]
        if (item["group"], item["kind"]) == ("approvals", "approval")
    )
    assert approvals["attributes"] == ["approver", "role"]
    assert "value" not in approvals["attributes"]
    assert approvals["entry_count"] == 2


def test_record_kind_conflict_is_non_bindable():
    base = DEFAULT_REGISTRY
    alternate_kind = replace(
        base.record_kind("procure_to_pay", "procure_to_pay.purchase_order"),
        id="procure_to_pay.order_confirmation",
    )
    pack = replace(
        base.pack("procure_to_pay"),
        record_kind_ids=(
            *base.pack("procure_to_pay").record_kind_ids,
            alternate_kind.id,
        ),
    )
    registry = CycleRegistry(
        normalizers=base.normalizers.values(),
        identifier_kinds=base.identifier_kinds.values(),
        field_kinds=base.field_kinds.values(),
        record_kinds=(*base.record_kinds.values(), alternate_kind),
        evidence_kinds=base.evidence_kinds.values(),
        packs=(pack, base.pack("payroll")),
    )
    reference = registry.reference("procure_to_pay").to_dict()
    fragments = [
        _fragment(
            reference,
            chunk="A",
            record_kind="procure_to_pay.purchase_order",
            identifiers=[("procure_to_pay.purchase_order_number", "PO-1")],
        ),
        _fragment(
            reference,
            chunk="B",
            record_kind="procure_to_pay.order_confirmation",
            identifiers=[("procure_to_pay.purchase_order_number", "PO-1")],
        ),
    ]

    reduction = cycle_vouching.reduce_record_fragments(
        "DOC-CONFLICT", fragments, registry=registry
    )

    assert reduction["records"] == []
    assert reduction["conflicts"][0]["kind"] == "record_kind_conflict"
    assert reduction["conflicts"][0]["bindable"] is False


def test_record_identity_includes_registry_definition_hash():
    base = DEFAULT_REGISTRY
    changed_pack = replace(base.pack("payroll"), label="Payroll definition changed")
    changed = CycleRegistry(
        normalizers=base.normalizers.values(),
        identifier_kinds=base.identifier_kinds.values(),
        field_kinds=base.field_kinds.values(),
        record_kinds=base.record_kinds.values(),
        evidence_kinds=base.evidence_kinds.values(),
        packs=(base.pack("procure_to_pay"), changed_pack),
    )
    original_ref = base.reference("payroll")
    changed_ref = changed.reference("payroll")

    assert cycle_vouching.stable_record_id(
        "DOC-1", original_ref, "payroll.payslip", "payroll.payslip_number", "ps-1"
    ) != cycle_vouching.stable_record_id(
        "DOC-1", changed_ref, "payroll.payslip", "payroll.payslip_number", "ps-1"
    )


def test_transaction_index_excludes_all_common_entity_identifiers():
    for kind in (
        "common.vendor_id",
        "common.buyer_id",
        "common.employee_id",
        "common.department_id",
        "common.customer_id",
        "common.account_number",
    ):
        assert DEFAULT_REGISTRY.identifier_kinds[kind].edge_policy == "non_linking"

    procurement = _fixture("procurement_cycle_phase0.json")
    index = cycle_vouching.build_identifier_index(procurement["reduction"]["records"])
    assert all(not key[1].startswith("common.") for key in index)


def test_exact_links_form_transitive_procurement_and_payroll_cycles():
    for fixture_name, expected_records in (
        ("procurement_cycle_phase0.json", 5),
        # Employment/time evidence shares only the deliberately non-linking
        # employee entity ID in this compact fixture, so it must not be pulled
        # into the payroll transaction closure.
        ("payroll_cycle_phase0.json", 2),
    ):
        contract = _fixture(fixture_name)
        test = contract["cycle_test"]
        population = test["definition"]["population"]
        row = test["items"][0]["frozen_row"]
        linkage = cycle_vouching.link_cycle_records(
            registry_ref=contract["registry"],
            seeds=[
                {"kind": key["identifier_kind"], "value": row[key["column"]]}
                for key in population["cycle_keys"]
            ],
            records=contract["reduction"]["records"],
            roles=test["definition"]["roles"],
        )
        assert linkage["state"] == "linked"
        assert len(linkage["records"]) == expected_records
        assert len(linkage["role_bindings"]) == expected_records
        assert all(binding["matched_by"] for binding in linkage["role_bindings"])


def test_identifier_typo_is_not_fuzzy_linked():
    contract = _fixture("procurement_cycle_phase0.json")
    records = copy.deepcopy(contract["reduction"]["records"])
    requisition = next(
        value for value in records if value["record_kind"].endswith("purchase_requisition")
    )
    for identifier in requisition["identifiers"]:
        if identifier["kind"].endswith("purchase_order_number"):
            identifier["value"].update(raw_value="P0-2024-004", value="P0-2024-004")
    linkage = cycle_vouching.link_cycle_records(
        registry_ref=contract["registry"],
        seeds=[
            {"kind": "procure_to_pay.internal_invoice_id", "value": "INV2024004"}
        ],
        records=records,
    )
    assert len(linkage["records"]) == 5
    linked_requisition = next(
        record for record in linkage["records"] if record["record_id"] == requisition["record_id"]
    )
    # The exact requisition-number path keeps the cycle connected; the typo is
    # retained on the record but never appears as a corrected PO edge.
    assert linked_requisition["matched_by"][-1]["identifier_kind"] == (
        "procure_to_pay.requisition_number"
    )
    assert any(
        identifier["value"]["raw_value"] == "P0-2024-004"
        for identifier in linked_requisition["identifiers"]
    )


def test_shared_reuse_and_cardinality_are_explicit():
    items = [
        {"id": "ITEM-1", "role_bindings": [{"role": "po", "record_id": "REC-PO", "matched_by": [{"identifier_kind": "x", "normalized_value": "1"}]}]},
        {"id": "ITEM-2", "role_bindings": [{"role": "po", "record_id": "REC-PO", "matched_by": [{"identifier_kind": "x", "normalized_value": "1"}]}]},
    ]
    allowed = cycle_vouching.apply_cross_item_reuse(
        items, [{"role": "po", "reuse_across_items": "allowed"}]
    )
    exclusive = cycle_vouching.apply_cross_item_reuse(
        items, [{"role": "po", "reuse_across_items": "exclusive"}]
    )
    assert all(value.get("shared_record_facts") for value in allowed)
    assert not any(value.get("collisions") for value in allowed)
    assert all(value["linkage_state"] == "needs_review" for value in exclusive)


def test_cardinality_one_reports_conflict_instead_of_selecting_first():
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    fragments = [
        _fragment(
            reference,
            chunk="PO-1",
            record_kind="procure_to_pay.purchase_order",
            identifiers=[
                ("procure_to_pay.purchase_order_number", "PO-1"),
                ("procure_to_pay.internal_invoice_id", "INV-1"),
            ],
        ),
        _fragment(
            reference,
            chunk="PO-2",
            record_kind="procure_to_pay.purchase_order",
            identifiers=[
                ("procure_to_pay.purchase_order_number", "PO-2"),
                ("procure_to_pay.internal_invoice_id", "INV-1"),
            ],
        ),
    ]
    records = cycle_vouching.reduce_record_fragments("DOC-PO", fragments)["records"]
    linkage = cycle_vouching.link_cycle_records(
        registry_ref=reference,
        seeds=[{"kind": "procure_to_pay.internal_invoice_id", "value": "INV-1"}],
        records=records,
        roles=[
            {
                "role": "po",
                "record_kind": "procure_to_pay.purchase_order",
                "cardinality": "one",
                "reuse_across_items": "allowed",
            }
        ],
    )
    assert linkage["state"] == "needs_review"
    assert linkage["role_bindings"] == []
    assert linkage["role_conflicts"][0]["record_ids"] == sorted(
        record["record_id"] for record in records
    )


def test_graph_limits_fail_visibly_without_truncated_success():
    contract = _fixture("procurement_cycle_phase0.json")
    result = cycle_vouching.link_cycle_records(
        registry_ref=contract["registry"],
        seeds=[{"kind": "procure_to_pay.internal_invoice_id", "value": "INV2024004"}],
        records=contract["reduction"]["records"],
        max_records=1,
    )
    assert result["state"] == "needs_review"
    assert result["review_reason"] == "graph_records_limit_exceeded"
    assert result["records"] == []
    assert result["triggering_identifier"]["identifier_kind"].startswith("procure_to_pay.")


def test_authoritative_candidate_outranks_equivalent_join_deterministically():
    contract = _fixture("procurement_cycle_phase0.json")
    ws = workspaces.create_workspace("Phase 1 candidate ranking")
    ws.add_table(
        "invoice.csv",
        b"INVOICE_ID,VENDOR_INVOICE_NUMBER,PO_NUMBER_LINK,GRN_ID_LINK\nINV2024004,V-INV-778,PO-2024-004,GRN-2024-011\n",
    )
    ws = workspaces.load_workspace(ws.id)
    ws.add_table("labels.csv", b"INVOICE_ID,LABEL\nINV2024004,one\n")
    ws = workspaces.load_workspace(ws.id)
    ws.add_join(
        {
            "name": "invoice_join",
            "left": "invoice",
            "right": "labels",
            "how": "left",
            "left_on": ["INVOICE_ID"],
            "right_on": ["INVOICE_ID"],
        }
    )
    ws = workspaces.load_workspace(ws.id)
    common = {
        "row_key": {
            "column": "INVOICE_ID",
            "identifier_kind": "procure_to_pay.internal_invoice_id",
        },
        "cycle_keys": [
            {"column": "VENDOR_INVOICE_NUMBER", "identifier_kind": "procure_to_pay.vendor_invoice_number"},
            {"column": "PO_NUMBER_LINK", "identifier_kind": "procure_to_pay.purchase_order_number"},
            {"column": "GRN_ID_LINK", "identifier_kind": "procure_to_pay.goods_receipt_number"},
        ],
    }
    mappings = [
        {"table": "invoice_join", "join_justification": "Adds a display label only.", **common},
        {"table": "invoice", **common},
    ]
    kwargs = {
        "registry_ref": contract["registry"],
        "mappings": mappings,
        "records": contract["reduction"]["records"],
        "required_roles": contract["cycle_test"]["definition"]["roles"],
    }

    first = cycle_vouching.generate_cycle_candidates(ws, **kwargs)
    second = cycle_vouching.generate_cycle_candidates(ws, **kwargs)

    assert first == second
    assert [value["table"] for value in first["candidates"]] == [
        "invoice",
        "invoice_join",
    ]
    assert first["candidates"][0]["candidate_id"] != first["candidates"][1]["candidate_id"]
    assert first["candidates"][0]["registry"] == contract["registry"]
    selected = cycle_vouching.select_prevalidated_candidate(
        first, first["candidates"][0]["candidate_id"], "Authoritative source population."
    )
    assert selected["table"] == "invoice"
    with pytest.raises(cycle_vouching.CycleSchemaError, match="not prevalidated"):
        cycle_vouching.select_prevalidated_candidate(first, "CYCLE-CAND-INVENTED", "No")


@pytest.mark.parametrize(
    "rows",
    [
        b"INVOICE_ID,PO_NUMBER\n,PO-1\nINV-2,PO-2\n",
        b"INVOICE_ID,PO_NUMBER\nINV-1,PO-1\nINV-1,PO-2\n",
    ],
)
def test_candidate_rejects_null_or_non_unique_transaction_row_keys(rows):
    reference = DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()
    ws = workspaces.create_workspace("Unsafe candidate")
    ws.add_table("invoice.csv", rows)
    ws = workspaces.load_workspace(ws.id)

    manifest = cycle_vouching.generate_cycle_candidates(
        ws,
        registry_ref=reference,
        mappings=[
            {
                "table": "invoice",
                "row_key": {
                    "column": "INVOICE_ID",
                    "identifier_kind": "procure_to_pay.internal_invoice_id",
                },
                "cycle_keys": [
                    {
                        "column": "PO_NUMBER",
                        "identifier_kind": "procure_to_pay.purchase_order_number",
                    }
                ],
            }
        ],
        records=[],
        required_roles=[],
    )

    assert manifest["candidates"] == []
    assert manifest["rejected_candidates"][0]["reason"] == (
        "row_key_must_be_non_null_and_unique"
    )
