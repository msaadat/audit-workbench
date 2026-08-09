"""Comparison primitives retained for auditor-authored simple `vouching`.

Transaction-cycle linking, role binding, and cycle coverage moved to the
registry-backed `cycle_vouching` pipeline when the broad-document-type
`build_cycle_vouching` builder was removed (plan section 4.3). What remains here
is the literal expected-value comparison used by simple `vouching`: nothing
calls a model, and every comparison is deterministic and reproducible.
"""

from __future__ import annotations

import pytest

from app import doc_tests, workspaces


# --------------------------------------------------------------------------- #
# Literal simple-vouching comparisons
# --------------------------------------------------------------------------- #
def test_dotted_cycle_paths_are_rejected_from_simple_vouching():
    with pytest.raises(workspaces.WorkspaceError, match="Dotted-path checks"):
        doc_tests._normalize_check(
            {
                "field": "amount",
                "method": "normalized",
                "left": "row.amount",
                "right": "invoice.amount.total",
            }
        )


def test_literal_comparison_methods_remain_deterministic():
    assert doc_tests.compare_values("Invoice 001", " invoice-001 ", "normalized")[
        "result"
    ] == "match"
    assert doc_tests.compare_values(100, 100.4, "numeric_tolerance", 0.5)[
        "result"
    ] == "match"
    assert doc_tests.compare_values("Approved", "Rejected", "exact")["result"] == "mismatch"
    assert doc_tests.compare_values("Expected", None, "exact")["result"] == "missing"


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
    assert not hasattr(doc_tests, "resolve_check_path")
    assert not hasattr(doc_tests, "voucher_field_index")
    assert not hasattr(doc_tests, "_run_cycle_item")
