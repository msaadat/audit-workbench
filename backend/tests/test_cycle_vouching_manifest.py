"""The deterministic transaction-evidence manifest, against real frames.

Everything here runs the *real* mapping inference, candidate scoring, and
manifest assembly over imported tables. The Phase 2 service tests supply a
pre-built manifest to isolate the semantic gate; these cover the layer beneath
it, where a column has to be recognised as an identifier before any of that can
happen.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import polars as pl
import pytest

from app import cycle_vouching, document_analysis, workspaces


FIXTURE = Path(__file__).parent / "fixtures" / "procurement_cycle_phase0.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _records(contract: dict) -> list[dict]:
    """The five reduced records, with the live workspace's identifier collision.

    In the real procurement pack the payment voucher's own number *is* the
    invoice's internal id, so a column holding invoice ids matches two
    identifier kinds equally often. That collision is the reason mapping needs
    a tie-break at all, so the fixture is adjusted to reproduce it.
    """
    records = copy.deepcopy(contract["reduction"]["records"])
    payment = next(
        record
        for record in records
        if record["record_kind"] == "procure_to_pay.payment_voucher"
    )
    internal = next(
        fact["value"]["value"]
        for fact in payment["identifiers"]
        if fact["kind"] == "procure_to_pay.internal_invoice_id"
    )
    voucher_number = next(
        fact
        for fact in payment["identifiers"]
        if fact["kind"] == "procure_to_pay.payment_voucher_number"
    )
    voucher_number["value"] = {**voucher_number["value"], "raw_value": internal, "value": internal}
    return records


def _table(workspace, name: str, frame: pl.DataFrame):
    path = workspace.root / "Data" / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_csv(path)
    workspace.tables.append({"name": name, "file": f"{name}.csv"})


@pytest.fixture
def populated(contract):
    """A workspace shaped like the procurement pack: two linked populations."""

    workspace = workspaces.create_workspace("Manifest inference")
    # One evidenced transaction inside a wider population, which is what an
    # evidence-linked reach actually looks like.
    _table(
        workspace,
        "invoice_data",
        pl.DataFrame(
            {
                "INVOICE_ID": ["INV2024004", "INV2024005", "INV2024006"],
                # Repeated by the vendor, as in the real population: the
                # internal id is the table's only non-null unique key.
                "VENDOR_INVOICE_NUMBER": ["V-INV-778", "V-INV-779", "V-INV-779"],
                "PO_NUMBER_LINK": ["PO-2024-004", "PO-2024-005", None],
                "GRN_ID_LINK": ["GRN-2024-011", None, None],
                "INVOICE_AMOUNT": [2000000.0, 15000.0, 900.0],
            }
        ),
    )
    _table(
        workspace,
        "po_data",
        pl.DataFrame(
            {
                "PO_NUMBER": ["PO-2024-004", "PO-2024-005"],
                "REQUISITION_ID": ["PR-2024-004", "PR-2024-005"],
                "GRN_ID": ["GRN-2024-011", "GRN-2024-012"],
            }
        ),
    )
    workspace.save()
    return workspaces.load_workspace(workspace.id)


def _mappings(populated, contract):
    return cycle_vouching.infer_cycle_mappings(
        populated,
        registry_ref=contract["registry"],
        records=_records(contract),
    )


def test_a_colliding_identifier_value_does_not_discard_the_only_row_key(
    populated, contract
):
    """Row-count ties are broken by evidence reach, not by dropping the column.

    ``INVOICE_ID`` matches ``internal_invoice_id`` and ``payment_voucher_number``
    on exactly the same single row. Omitting it as ambiguous would leave
    ``invoice_data`` with no non-null unique key at all, and the invoice-grain
    population — the one every invoice control needs — would not exist.
    """
    mappings = _mappings(populated, contract)
    invoice = [item for item in mappings if item["table"] == "invoice_data"]

    assert [item["row_key"]["column"] for item in invoice] == ["INVOICE_ID"]
    # internal_invoice_id is carried by the invoice *and* the payment voucher;
    # the voucher number reaches the voucher alone.
    assert invoice[0]["row_key"]["identifier_kind"] == "procure_to_pay.internal_invoice_id"
    assert {key["column"] for key in invoice[0]["cycle_keys"]} == {
        "VENDOR_INVOICE_NUMBER",
        "PO_NUMBER_LINK",
        "GRN_ID_LINK",
    }


def _single_reach_records(contract: dict) -> list[dict]:
    """The live shape: each colliding kind is carried by exactly one record."""

    records = _records(contract)
    invoice = next(
        record
        for record in records
        if record["record_kind"] == "procure_to_pay.vendor_invoice"
    )
    payment = next(
        record
        for record in records
        if record["record_kind"] == "procure_to_pay.payment_voucher"
    )
    payment["identifiers"] = [
        fact
        for fact in payment["identifiers"]
        if fact["kind"] != "procure_to_pay.internal_invoice_id"
    ]
    assert any(
        fact["kind"] == "procure_to_pay.internal_invoice_id"
        for fact in invoice["identifiers"]
    )
    return records


def test_equal_reach_falls_back_to_the_registry_s_own_naming(populated, contract):
    """When reach ties too, the pack's words for the kind decide.

    ``INVOICE_ID`` shares two tokens with ``internal_invoice_id`` and none with
    ``payment_voucher_number``. This reads the registered id and label, so it
    holds for payroll or any future pack without a domain switch.
    """
    mappings = cycle_vouching.infer_cycle_mappings(
        populated,
        registry_ref=contract["registry"],
        records=_single_reach_records(contract),
    )
    invoice = [item for item in mappings if item["table"] == "invoice_data"]

    assert invoice[0]["row_key"] == {
        "column": "INVOICE_ID",
        "identifier_kind": "procure_to_pay.internal_invoice_id",
    }


def test_a_column_tied_on_every_signal_is_omitted_not_guessed(populated, contract):
    """The safety property survives: a real tie is still never resolved."""

    records = _single_reach_records(contract)
    # Rename the column so it favours neither kind, leaving rows, reach and
    # naming all tied.
    frame = populated.get_frame("invoice_data").rename({"INVOICE_ID": "REF"})
    frame.write_csv(populated.root / "Data" / "invoice_data.csv")
    reloaded = workspaces.load_workspace(populated.id)

    mappings = cycle_vouching.infer_cycle_mappings(
        reloaded, registry_ref=contract["registry"], records=records
    )

    assert not [
        item for item in mappings if item["row_key"]["column"] == "REF"
    ]


def test_a_table_ranks_the_key_of_its_own_grain_first(populated, contract):
    """A PO population is keyed on its PO number, not on the GRN it also carries.

    All three of ``po_data``'s identifier columns are unique and non-null, so
    without a positional term the lexically first column wins and a purchase
    order population is labelled by goods-receipt number.
    """
    manifest = cycle_vouching.generate_cycle_candidates(
        populated,
        registry_ref=contract["registry"],
        mappings=_mappings(populated, contract),
        records=_records(contract),
        required_roles=cycle_vouching.default_roles(
            contract["control_attributes"][0]["required_record_kinds"]
        ),
    )
    po_candidates = [
        item for item in manifest["candidates"] if item["table"] == "po_data"
    ]

    assert po_candidates[0]["row_key"]["column"] == "PO_NUMBER"


def test_derived_joins_are_never_inferred(populated, contract):
    """§4.1 prefers an authoritative population, so a join is not a candidate."""

    populated.joins.append(
        {
            "name": "invoice_data_po_data_joined",
            "left": "invoice_data",
            "right": "po_data",
            "left_on": "PO_NUMBER_LINK",
            "right_on": "PO_NUMBER",
            "how": "left",
        }
    )
    populated.save()
    reloaded = workspaces.load_workspace(populated.id)

    mappings = _mappings(reloaded, contract)

    assert not [item for item in mappings if "joined" in item["table"]]


def test_role_coverage_counts_only_the_rows_a_test_would_select(
    populated, contract
):
    """An unlinked row is one uncovered row, not a miss against every role.

    Counting it per role would report a 3-row population with one evidenced
    cycle as missing every role twice over, which reads as a coverage failure
    of the evidence rather than of the population reach.
    """
    manifest = cycle_vouching.generate_cycle_candidates(
        populated,
        registry_ref=contract["registry"],
        mappings=_mappings(populated, contract),
        records=_records(contract),
        required_roles=cycle_vouching.default_roles(
            contract["control_attributes"][0]["required_record_kinds"]
        ),
    )
    invoice = next(
        item for item in manifest["candidates"] if item["table"] == "invoice_data"
    )

    assert invoice["population_rows"] == 3
    assert invoice["linked_rows"] < invoice["population_rows"]
    assert max(invoice["missing_role_counts"].values()) <= invoice["linked_rows"]


def test_the_manifest_is_hash_identified_and_content_free(
    populated, contract, monkeypatch
):
    """The whole assembly runs, and no row value leaves the machine."""

    monkeypatch.setattr(
        document_analysis,
        "registry_evidence_records",
        lambda *_args, **_kwargs: _records(contract),
    )

    manifest = cycle_vouching.transaction_evidence_manifest(
        populated, contract["control_attributes"]
    )
    repeated = cycle_vouching.transaction_evidence_manifest(
        populated, contract["control_attributes"]
    )

    assert manifest["manifest_sha256"] == repeated["manifest_sha256"]
    group = manifest["groups"][0]
    assert {record["record_kind"] for record in group["records"]} == {
        "procure_to_pay.purchase_requisition",
        "procure_to_pay.purchase_order",
        "procure_to_pay.goods_receipt",
        "procure_to_pay.vendor_invoice",
        "procure_to_pay.payment_voucher",
    }
    assert any(
        item["table"] == "invoice_data"
        and item["row_key"]["column"] == "INVOICE_ID"
        for item in group["candidates"]
    )
    # Counts, names and column types only: no identifier or amount value.
    serialized = json.dumps(manifest)
    assert "2000000" not in serialized
    assert "INV2024005" not in serialized


def test_an_unrefreshed_voucher_analysis_is_excluded_and_named(populated, contract):
    """One stale analysis must not close the manifest for the whole engagement.

    Refusing the call made a single document that predates the current pack fail
    every caller in the workspace — including the authoring UX an auditor would
    use to repair it. It is excluded instead, and named, because evidence that
    exists and is not counted reads very differently from a document that was
    never voucher-analyzed.
    """

    from app import documents

    document = documents.add_document(
        populated, "legacy-voucher.txt", b"Payment voucher PV-9.", category="voucher"
    )
    documents.extract_document(populated, document["id"])
    document_analysis.persist_analysis(
        populated,
        document,
        {"pages": [{"page": 1, "text": "Payment voucher PV-9."}]},
        {
            "summary_markdown": "A voucher analyzed before the current pack.",
            "audit_notes_markdown": "None.",
            "citations": [],
            "analysis_profile": "voucher",
            # No registry reference: exactly what a pre-registry analysis holds.
        },
        provider="local",
        model="test",
    )
    populated = workspaces.load_workspace(populated.id)

    excluded: list[dict] = []
    records = document_analysis.registry_evidence_records(
        populated,
        cycle_vouching.DEFAULT_REGISTRY.reference("procure_to_pay").to_dict(),
        excluded=excluded,
    )

    assert records == []
    assert excluded == [
        {"document_id": document["id"], "reason": "not_registry_backed"}
    ]

    manifest = cycle_vouching.transaction_evidence_manifest(
        populated, contract["control_attributes"]
    )
    assert manifest["groups"][0]["excluded_documents"] == excluded
