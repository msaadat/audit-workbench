"""Schema induction: sampling, union, conflict, and freeze."""

from __future__ import annotations

import pytest

from app import document_classification as dc
from app import document_schemas, documents, workspaces
from app.document_schemas import SchemaConflict


def _field(name: str, **overrides) -> dict:
    field = {
        "name": name,
        "role": "attribute",
        "value_type": "text",
        "cardinality": "one",
        "verbatim": True,
        "confidence": "high",
    }
    field.update(overrides)
    return field


@pytest.fixture
def ws() -> workspaces.Workspace:
    return workspaces.create_workspace("Induction Engagement")


# --------------------------------------------------------------------- union
def test_union_keeps_a_field_only_one_sample_saw():
    """Intersecting would permanently discard a field the corpus contains."""

    fields, conflicts = document_schemas.union_fields([
        [_field("invoice_number"), _field("total_amount")],
        [_field("invoice_number"), _field("currency")],
    ])
    assert [field["name"] for field in fields] == [
        "currency", "invoice_number", "total_amount",
    ]
    assert conflicts == []


def test_union_is_order_independent():
    forward, _ = document_schemas.union_fields([
        [_field("a")], [_field("b")], [_field("c")],
    ])
    backward, _ = document_schemas.union_fields([
        [_field("c")], [_field("b")], [_field("a")],
    ])
    assert forward == backward


def test_many_wins_over_one():
    """One sample seeing two of something proves the type can carry two."""

    fields, _ = document_schemas.union_fields([
        [_field("party_name", cardinality="one")],
        [_field("party_name", cardinality="many")],
    ])
    assert fields[0]["cardinality"] == "many"


def test_interpretive_wins_over_verbatim():
    """Demanding a quote for a value the document never prints is unsatisfiable."""

    fields, _ = document_schemas.union_fields([
        [_field("approval", verbatim=True)],
        [_field("approval", verbatim=False)],
    ])
    assert fields[0]["verbatim"] is False


def test_the_strongest_stated_confidence_survives():
    fields, _ = document_schemas.union_fields([
        [_field("total_amount", confidence="low")],
        [_field("total_amount", confidence="high")],
        [_field("total_amount", confidence="medium")],
    ])
    assert fields[0]["confidence"] == "high"


@pytest.mark.parametrize("attribute, other", [
    ("value_type", "number"),
    ("role", "identifier"),
])
def test_a_field_meaning_two_things_is_a_conflict(attribute, other):
    """These change what the field *is*, so they cannot be merged arithmetically."""

    first = _field("reference")
    second = _field("reference")
    second[attribute] = other
    assert first[attribute] != other
    _, conflicts = document_schemas.union_fields([[first], [second]])
    assert [item["attribute"] for item in conflicts] == [attribute]
    assert conflicts[0]["name"] == "reference"


def test_differing_cardinality_is_not_a_conflict():
    _, conflicts = document_schemas.union_fields([
        [_field("x", cardinality="one")], [_field("x", cardinality="many")],
    ])
    assert conflicts == []


# -------------------------------------------------------------------- freeze
def test_induce_freezes_the_union(ws):
    schema = document_schemas.induce(
        ws, "vendor_invoice",
        [
            [_field("invoice_number", role="identifier", value_type="identifier")],
            [_field("invoice_number", role="identifier", value_type="identifier"),
             _field("total_amount", value_type="number")],
        ],
        derived_from=["doc-1", "doc-2"],
    )
    assert [field["name"] for field in schema["fields"]] == [
        "invoice_number", "total_amount",
    ]
    assert schema["low_confidence"] is False
    assert schema["schema_version"] == 1


def test_a_single_sample_freezes_but_is_marked_low_confidence(ws):
    """The agreement check cannot run on one document, so nothing is corroborated
    — but a one-off with a real schema still beats an unclassified one."""

    schema = document_schemas.induce(
        ws, "vendor_invoice",
        [[_field("invoice_number", role="identifier", value_type="identifier")]],
        derived_from=["doc-1"],
    )
    assert schema["low_confidence"] is True


def test_induce_refuses_an_unsettled_conflict(ws):
    with pytest.raises(SchemaConflict) as raised:
        document_schemas.induce(ws, "vendor_invoice", [
            [_field("reference", value_type="text")],
            [_field("reference", value_type="number")],
        ])
    assert raised.value.conflicts[0]["name"] == "reference"
    assert "disagree on what a field is" in str(raised.value)
    assert document_schemas.load_schema(ws, "vendor_invoice") is None


def test_a_reconciled_induction_records_that_it_was(ws):
    schema = document_schemas.induce(
        ws, "vendor_invoice",
        [[_field("reference", value_type="text")]],
        reconciled=True,
    )
    assert schema["reconciled"] is True


# ------------------------------------------------------------------ sampling
def _add(ws, name: str, *, path: str | None = None, body: bytes = b"Invoice text here") -> str:
    document = documents.add_document(ws, name, body, category="evidence")
    if path is not None:
        document["relative_path"] = path
        ws.save()
    return str(document["id"])


def test_sampling_spreads_across_folders_rather_than_taking_the_first_two(ws):
    """Two hundred invoices from a dozen vendors: the first two in id order can
    easily share a layout the rest of the corpus does not."""

    ids = []
    for index in range(4):
        ids.append(_add(ws, f"a{index}.txt", path="vendorA/a.txt"))
    ids.append(_add(ws, "b0.txt", path="vendorB/b.txt"))
    for document_id in ids:
        dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")

    picked = dc.sample_for_induction(ws, "vendor_invoice", limit=2)
    folders = {
        str(next(d for d in ws.documents if str(d["id"]) == pick).get("relative_path") or "")
        .rsplit("/", 1)[0]
        for pick in picked
    }
    assert folders == {"vendorA", "vendorB"}


def test_sampling_is_deterministic(ws):
    for index in range(5):
        document_id = _add(ws, f"d{index}.txt")
        dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    assert dc.sample_for_induction(ws, "vendor_invoice") == dc.sample_for_induction(
        ws, "vendor_invoice"
    )


def test_a_high_volume_type_buys_a_third_sample(ws):
    for index in range(dc.HIGH_VOLUME_DOCUMENTS):
        document_id = _add(ws, f"d{index}.txt")
        dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    assert len(dc.sample_for_induction(ws, "vendor_invoice")) == 3


def test_sampling_never_asks_for_more_than_exists(ws):
    document_id = _add(ws, "only.txt")
    dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    assert dc.sample_for_induction(ws, "vendor_invoice") == [document_id]


def test_sampling_skips_documents_with_no_text(ws):
    with_text = _add(ws, "has.txt")
    without = _add(ws, "empty.txt", body=b"")
    for document_id in (with_text, without):
        dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    assert dc.sample_for_induction(ws, "vendor_invoice") == [with_text]


def test_an_unassigned_type_samples_nothing(ws):
    assert dc.sample_for_induction(ws, "vendor_invoice") == []


# ------------------------------------------------------------------ selection
def test_types_awaiting_schema_shrinks_as_schemas_are_frozen(ws):
    first = _add(ws, "inv.txt")
    second = _add(ws, "po.txt")
    dc.assign(ws, first, "vendor_invoice", assigned_by="model")
    dc.assign(ws, second, "purchase_order", assigned_by="model")
    assert dc.types_awaiting_schema(ws) == ["purchase_order", "vendor_invoice"]

    document_schemas.induce(ws, "vendor_invoice", [[_field("invoice_number")]])
    assert dc.types_awaiting_schema(ws) == ["purchase_order"]


def test_other_never_awaits_a_schema(ws):
    document_id = _add(ws, "odd.txt")
    dc.assign(ws, document_id, "other", assigned_by="model", other_label="Unclear")
    assert dc.types_awaiting_schema(ws) == []


def test_induction_reads_more_of_the_document_than_classification(ws):
    """Naming a document needs its first page; listing its fields needs the body."""

    body = ("Field line\n" * 4000).encode()
    document_id = _add(ws, "long.txt", body=body)
    naming = dc.classification_text(ws, document_id)
    listing = dc.induction_text(ws, document_id)
    assert len(listing) > len(naming)
    assert len(listing) <= dc.INDUCTION_CHARACTERS
