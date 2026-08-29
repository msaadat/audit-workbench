"""The global document-type catalog and per-workspace induced schemas."""

from __future__ import annotations

import pytest

from app import document_schemas, document_types, workspaces
from app.workspaces import WorkspaceError


@pytest.fixture
def ws() -> workspaces.Workspace:
    return workspaces.create_workspace("Schema Engagement")


def _fields(**overrides) -> list[dict]:
    base = [
        {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
        {"name": "total_amount", "role": "attribute", "value_type": "number",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ]
    base[0].update(overrides)
    return base


# --------------------------------------------------------------- the catalog
def test_catalog_is_internally_consistent():
    assert document_types.OTHER in document_types.BY_ID
    assert len(document_types.BY_ID) == len(document_types.DEFINITIONS)
    areas = {area for area, _ in document_types.AREAS}
    for definition in document_types.DEFINITIONS:
        assert definition.area in areas
        assert definition.discriminator.strip()


def test_treasury_is_covered_without_new_mechanism():
    """The area that fails silently today must be expressible in the list alone."""

    treasury = {
        definition.id
        for definition in document_types.DEFINITIONS
        if definition.area == "treasury_banking"
    }
    assert {"bank_confirmation", "bank_statement", "payment_instruction"} <= treasury


def test_validate_rejects_unknown_and_accepts_listed():
    assert document_types.validate("vendor_invoice") == "vendor_invoice"
    with pytest.raises(document_types.DocumentTypeError):
        document_types.validate("not_a_type")


def test_local_type_must_be_defined_in_the_workspace():
    """A `local.` prefix is not itself authority to use an id."""

    with pytest.raises(document_types.DocumentTypeError):
        document_types.validate("local.letter_of_indemnity")
    assert document_types.validate(
        "local.letter_of_indemnity", local_types=["local.letter_of_indemnity"]
    ) == "local.letter_of_indemnity"


def test_local_id_refuses_to_shadow_a_global_type():
    assert document_types.local_id("Letter of Indemnity") == "local.letter_of_indemnity"
    with pytest.raises(document_types.DocumentTypeError):
        document_types.local_id("cheque")
    with pytest.raises(document_types.DocumentTypeError):
        document_types.local_id("   ")


def test_prompt_catalog_carries_discriminators_and_aliases():
    text = document_types.prompt_catalog()
    assert "goods_receipt — Internal record that goods were received" in text
    assert "GRN" in text
    assert f"{document_types.OTHER} —" in text


def test_prompt_catalog_lists_coined_types_separately():
    text = document_types.prompt_catalog(
        local_types=[{"id": "local.letter_of_indemnity", "discriminator": "Shipper's indemnity"}]
    )
    assert "Defined for this engagement:" in text
    assert "local.letter_of_indemnity — Shipper's indemnity" in text


# --------------------------------------------------------------- coined types
def test_coining_is_idempotent_and_extends_the_effective_list(ws):
    first = document_schemas.coin_local_type(ws, "Letter of Indemnity")
    second = document_schemas.coin_local_type(ws, "Letter of Indemnity")
    assert first["id"] == second["id"] == "local.letter_of_indemnity"
    assert len(document_schemas.local_types(ws)) == 1
    assert "local.letter_of_indemnity" in document_schemas.effective_type_ids(ws)
    assert "vendor_invoice" in document_schemas.effective_type_ids(ws)


# --------------------------------------------------------------- schema round trip
def test_save_and_load_round_trip(ws):
    saved = document_schemas.save_schema(
        ws, "vendor_invoice", _fields(), derived_from=["doc-1", "doc-2"]
    )
    assert saved["schema_version"] == 1
    assert saved["schema_hash"].startswith("sha256:")
    loaded = document_schemas.get_schema(ws, "vendor_invoice")
    assert loaded == saved
    assert [field["name"] for field in loaded["fields"]] == ["invoice_number", "total_amount"]


def test_reinducing_identical_fields_does_not_bump_the_version(ws):
    """The version is what extractions are stamped with; an identical result
    must not invalidate a corpus."""

    first = document_schemas.save_schema(ws, "vendor_invoice", _fields(), derived_from=["a"])
    second = document_schemas.save_schema(ws, "vendor_invoice", _fields(), derived_from=["b", "c"])
    assert second["schema_version"] == first["schema_version"] == 1
    assert second["schema_hash"] == first["schema_hash"]


def test_changed_meaning_bumps_the_version_and_hash(ws):
    first = document_schemas.save_schema(ws, "vendor_invoice", _fields())
    changed = [
        *_fields(),
        {"name": "currency", "role": "attribute", "value_type": "text",
         "cardinality": "one", "verbatim": True, "confidence": "medium"},
    ]
    second = document_schemas.save_schema(ws, "vendor_invoice", changed)
    assert second["schema_version"] == 2
    assert second["schema_hash"] != first["schema_hash"]
    assert [entry["schema_version"] for entry in second["versions"]] == [1, 2]


def test_hash_ignores_bookkeeping(ws):
    """derived_from and timestamps must not move the hash."""

    first = document_schemas.save_schema(ws, "purchase_order", _fields(), derived_from=["x"])
    document_schemas.remove_schema(ws, "purchase_order")
    second = document_schemas.save_schema(
        ws, "purchase_order", _fields(), derived_from=["totally", "different"]
    )
    assert second["schema_hash"] == first["schema_hash"]


def test_field_order_does_not_affect_the_hash(ws):
    forward = document_schemas.save_schema(ws, "vendor_invoice", _fields())
    document_schemas.remove_schema(ws, "vendor_invoice")
    reversed_fields = list(reversed(_fields()))
    backward = document_schemas.save_schema(ws, "vendor_invoice", reversed_fields)
    assert backward["schema_hash"] == forward["schema_hash"]


# --------------------------------------------------------------- validation
def test_identifier_field_must_normalize_as_an_identifier(ws):
    """A numeric join key would compare 0042 against 42 and split a cycle."""

    with pytest.raises(WorkspaceError, match="value_type 'identifier'"):
        document_schemas.save_schema(ws, "vendor_invoice", _fields(value_type="number"))


@pytest.mark.parametrize(
    "override, message",
    [
        ({"name": "Invoice Number"}, "invalid field name"),
        ({"role": "join_key"}, "unsupported role"),
        ({"cardinality": "several"}, "unsupported cardinality"),
        ({"confidence": "certain"}, "unsupported confidence"),
    ],
)
def test_field_validation_rejects_bad_values(ws, override, message):
    with pytest.raises(WorkspaceError, match=message):
        document_schemas.save_schema(ws, "vendor_invoice", _fields(**override))


def test_duplicate_field_names_are_rejected(ws):
    duplicated = [*_fields(), dict(_fields()[0])]
    with pytest.raises(WorkspaceError, match="repeat a field name"):
        document_schemas.save_schema(ws, "vendor_invoice", duplicated)


def test_empty_schema_is_rejected(ws):
    with pytest.raises(WorkspaceError, match="at least one field"):
        document_schemas.save_schema(ws, "vendor_invoice", [])


def test_other_cannot_carry_a_schema(ws):
    with pytest.raises(WorkspaceError, match="retype first"):
        document_schemas.save_schema(ws, document_types.OTHER, _fields())


def test_unknown_type_is_rejected(ws):
    with pytest.raises(document_types.DocumentTypeError):
        document_schemas.save_schema(ws, "not_a_type", _fields())


def test_coined_type_can_carry_a_schema(ws):
    document_schemas.coin_local_type(ws, "Letter of Indemnity")
    saved = document_schemas.save_schema(ws, "local.letter_of_indemnity", _fields())
    assert saved["document_type"] == "local.letter_of_indemnity"
    assert document_schemas.get_schema(ws, "local.letter_of_indemnity") == saved


def test_uncoined_local_type_cannot_carry_a_schema(ws):
    with pytest.raises(document_types.DocumentTypeError):
        document_schemas.save_schema(ws, "local.never_coined", _fields())


def test_path_traversal_is_refused(ws):
    with pytest.raises((WorkspaceError, document_types.DocumentTypeError)):
        document_schemas.save_schema(ws, "../escape", _fields())


# --------------------------------------------------------------- staleness
def test_schema_ref_is_current_until_the_meaning_moves(ws):
    document_schemas.save_schema(ws, "vendor_invoice", _fields())
    ref = document_schemas.schema_ref(ws, "vendor_invoice")
    assert document_schemas.is_current(ws, ref)
    document_schemas.save_schema(
        ws,
        "vendor_invoice",
        [*_fields(), {"name": "currency", "role": "attribute", "value_type": "text",
                      "cardinality": "one", "verbatim": True, "confidence": "low"}],
    )
    assert not document_schemas.is_current(ws, ref)


def test_is_current_fails_closed_on_junk(ws):
    document_schemas.save_schema(ws, "vendor_invoice", _fields())
    assert not document_schemas.is_current(ws, None)
    assert not document_schemas.is_current(ws, {})
    assert not document_schemas.is_current(ws, {"document_type": "vendor_invoice"})
    assert not document_schemas.is_current(ws, {"document_type": "never_induced",
                                                 "schema_hash": "sha256:x",
                                                 "schema_version": 1})


# --------------------------------------------------------------- listing
def test_listing_and_index_track_saved_schemas(ws):
    document_schemas.save_schema(ws, "vendor_invoice", _fields())
    document_schemas.save_schema(ws, "purchase_order", _fields(), low_confidence=True)
    listed = [item["document_type"] for item in document_schemas.list_schemas(ws)]
    assert listed == ["purchase_order", "vendor_invoice"]
    index = {item["document_type"]: item for item in document_schemas.index(ws)["schemas"]}
    assert index["purchase_order"]["low_confidence"] is True
    assert index["vendor_invoice"]["schema_hash"].startswith("sha256:")


def test_missing_schema_reads_as_absent_not_an_error(ws):
    assert document_schemas.load_schema(ws, "vendor_invoice") is None
    assert document_schemas.load_schema(ws, "not_a_type") is None
    with pytest.raises(WorkspaceError, match="No schema has been induced"):
        document_schemas.get_schema(ws, "vendor_invoice")


def test_joinable_reports_whether_a_join_key_is_possible(ws):
    with_identifier = document_schemas.save_schema(ws, "vendor_invoice", _fields())
    assert document_schemas.joinable(with_identifier)
    no_identifier = document_schemas.save_schema(
        ws,
        "board_minutes",
        [{"name": "meeting_date", "role": "attribute", "value_type": "date",
          "cardinality": "one", "verbatim": True, "confidence": "high"}],
    )
    assert not document_schemas.joinable(no_identifier)
