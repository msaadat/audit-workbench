"""Comparison primitives retained for auditor-authored simple `vouching`.

Transaction-cycle linking, role binding, and cycle coverage moved to the
registry-backed `cycle_vouching` pipeline when the broad-document-type
`build_cycle_vouching` builder was removed (plan section 4.3); their gates live
in ``test_cycle_registry``, ``test_cycle_vouching_phase1`` and
``test_cycle_vouching_phase2``. What remains here is the path/method/resolution
layer that the simple `vouching` kind still uses: nothing calls a model, and
every comparison is deterministic and reproducible.
"""

from __future__ import annotations

import pytest

from app import doc_tests, workspaces


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
def test_paths_are_validated_when_the_check_is_written():
    """A path that could never resolve is rejected at definition time."""

    for path in ("", "row", "row.a.b", "invoice", "invoice.total", "invoice.nope.x"):
        with pytest.raises(workspaces.WorkspaceError):
            doc_tests._normalize_check(
                {"field": "f", "method": "normalized", "left": path, "right": "row.a"}
            )
    # Both shapes stay valid.
    doc_tests._normalize_check(
        {"field": "f", "method": "normalized", "left": "row.a", "right": "invoice.amount.total"}
    )
    doc_tests._normalize_check({"field": "f", "method": "normalized", "expected": "x"})


def test_unknown_attributes_are_rejected_at_authoring_time():
    """An attribute the schema has no field for is a mistake, not absent evidence.

    Resolving it leniently would return no matches, and the check would report
    ``missing`` — indistinguishable from a document that genuinely lacks the
    fact. Rejecting the path when it is written keeps that distinction honest.
    """
    with pytest.raises(workspaces.WorkspaceError, match="unknown attribute 'receipt_id'"):
        doc_tests.validate_path("voucher.attachment.receipt.receipt_id")
    with pytest.raises(workspaces.WorkspaceError, match="unknown attribute 'total'"):
        doc_tests.validate_path("invoice.amount.total.total")

    # Documented attributes, the group default, and the verbatim `raw_` form
    # normalization preserves are all addressable.
    doc_tests.validate_path("invoice.amount.total")
    doc_tests.validate_path("invoice.amount.total.currency")
    doc_tests.validate_path("invoice.amount.total.raw_value")
    doc_tests.validate_path("voucher.attachment.receipt.reference")
    doc_tests.validate_path("voucher.approval.*.approver")


def test_unary_and_binary_methods_declare_the_sides_they_need():
    """``present`` reads one side; every other method needs both."""

    doc_tests._normalize_check(
        {"field": "f", "method": "present", "left": "invoice.attachment.receipt.present"}
    )
    with pytest.raises(workspaces.WorkspaceError):
        doc_tests._normalize_check(
            {"field": "f", "method": "numeric_tolerance", "left": "row.amount"}
        )
    with pytest.raises(workspaces.WorkspaceError):
        doc_tests._normalize_check({"field": "f", "method": "present", "expected": "x"})


def test_conflicting_matches_are_an_ambiguity_not_a_silent_choice():
    """Two differing values for one path must not be resolved by list order."""

    role_fields = {
        "invoice": [
            ("d1", {"amounts": [{"kind": "total", "value": 100.0, "citation": "C1"}]}, {}),
            ("d2", {"amounts": [{"kind": "total", "value": 250.0, "citation": "C1"}]}, {}),
        ]
    }
    matches = doc_tests.resolve_check_path(
        "invoice.amount.total", frozen={}, role_fields=role_fields
    )
    assert len(matches) == 2
    assert doc_tests._single_value(matches) == (None, "ambiguous")

    # The same value stated twice is one fact, not an ambiguity.
    same = [dict(matches[0]), dict(matches[0])]
    value, state = doc_tests._single_value(same)
    assert (value, state) == (100.0, "resolved")


def test_sequencing_and_presence_methods():
    """The two comparison shapes an equality method cannot express."""

    assert doc_tests.compare_values("2025-05-01", "2025-05-02", "date_order")["result"] == "match"
    assert doc_tests.compare_values("2025-05-02", "2025-05-02", "date_order")["result"] == "match"
    # Payment before approval: the left date falls after the right one.
    assert doc_tests.compare_values("2025-05-26", "2025-05-25", "date_order")["result"] == "mismatch"
    assert doc_tests.compare_values("not a date", "2025-05-25", "date_order")["result"] == "invalid"

    # An affirmative negative is a mismatch; an absent value is missing.
    assert doc_tests.compare_values(True, False, "present")["result"] == "mismatch"
    assert doc_tests.compare_values(True, True, "present")["result"] == "match"
    assert doc_tests.compare_values(True, None, "present")["result"] == "missing"


def test_legacy_literal_checks_are_untouched():
    """The original expected-value shape still normalizes and runs."""

    check = doc_tests._normalize_check(
        {"field": "amount", "expected": "50,000", "method": "normalized"}
    )
    assert check["expected"] == "50,000"


def test_the_removed_cycle_builder_is_gone():
    """No compatibility path survives for the broad-document-type builder.

    Plan section 4.3 replaces it with the canonical
    ``cycle_vouching.build_cycle_vouch_test`` service; a reintroduced shim would
    be a second source of executable cycle checks.
    """
    assert not hasattr(doc_tests, "build_cycle_vouching")
    assert not hasattr(doc_tests, "voucher_anchor_candidates")
    assert not hasattr(doc_tests, "voucher_document_profile")
