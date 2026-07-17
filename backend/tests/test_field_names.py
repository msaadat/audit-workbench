import pytest

from app.field_names import resolve_column


def test_resolve_column_accepts_only_unique_case_difference():
    assert resolve_column("po_number", ["PO_NUMBER", "PO_NUMBER_LINK"]) == "PO_NUMBER"


def test_resolve_column_does_not_guess_a_semantic_alias():
    with pytest.raises(ValueError, match="PO_NUMBER_LINK"):
        resolve_column("po_number", ["PO_NUMBER_LINK"])


def test_resolve_column_rejects_case_ambiguous_source_headers():
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_column("FIELD", ["Field", "field"])
