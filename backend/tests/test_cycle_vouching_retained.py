"""The vocabulary-agnostic half of cycle evidence testing.

Value normalization, the six comparison operators, and the state words an
auditor's dispositions are recorded in. None of these ever depended on where
the vocabulary came from, so none of them changed when the packs went; these
assertions are carried over from the pack suites unaltered for that reason.
"""

from __future__ import annotations

import pytest

from app import cycle_vouching


# ------------------------------------------------------------- normalization
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
    assert "ambiguous" in ambiguous["normalization_error"]


def test_a_date_supplied_for_a_number_does_not_normalize_to_its_first_digits():
    """``19 Apr 2024`` yields 19 from a bare numeric scan, and reporting it as
    an amount of 19 would commit a wrong number that normalized cleanly."""

    supplied = cycle_vouching.normalize_evidence_value(
        "19 Apr 2024", semantic_type="number", citation="C1"
    )

    assert supplied["value"] is None
    assert supplied["normalization_error"] == "value is a date, not a number"


def test_two_numbers_in_one_raw_value_do_not_concatenate():
    parsed = cycle_vouching.normalize_evidence_value(
        "1,250.00 (25 units)", semantic_type="number", citation="C1"
    )

    assert parsed["value"] == 1250.0


def test_a_bracketed_number_is_negative():
    parsed = cycle_vouching.normalize_evidence_value(
        "(1,250.00)", semantic_type="number", citation="C1"
    )

    assert parsed["value"] == -1250.0


def test_an_identifier_folds_presentation_and_keeps_punctuation():
    """The conservative normalizer the join keys use, so a value that linked
    two records compares equal to itself.

    Case and surrounding or repeated whitespace are presentation. Punctuation
    is not: ``PO-2025/17`` and ``PO-2025-17`` are different references, and a
    normalizer that stripped separators would silently merge them.
    """

    folded = cycle_vouching.normalize_evidence_value(
        "  PO-2025/17 ", semantic_type="identifier", citation="C1"
    )
    same = cycle_vouching.normalize_evidence_value(
        "po-2025/17", semantic_type="identifier", citation="C2"
    )
    different = cycle_vouching.normalize_evidence_value(
        "PO-2025-17", semantic_type="identifier", citation="C3"
    )

    assert folded["value"] == same["value"]
    assert folded["value"] != different["value"]


def test_an_empty_raw_value_is_refused_rather_than_normalized():
    with pytest.raises(cycle_vouching.CycleSchemaError):
        cycle_vouching.normalize_evidence_value(
            "   ", semantic_type="text", citation="C1"
        )


# ----------------------------------------------------------------- operators
def test_equal_exact_compares_typed_numbers_numerically_and_text_textually():
    """Two extractions of one quantity may normalize to 25 and 25.0."""

    assert cycle_vouching._comparison("equal_exact", 25, 25.0, None) == "match"
    assert cycle_vouching._comparison("equal_exact", 25, 26, None) == "mismatch"
    # An identifier stays exact: the live PO/P0 typo must remain a mismatch.
    assert (
        cycle_vouching._comparison("equal_exact", "PO2024004", "P02024004", None)
        == "mismatch"
    )
    assert cycle_vouching._comparison("equal_exact", True, 1, None) == "mismatch"


def test_equal_normalized_folds_case_and_spacing():
    assert (
        cycle_vouching._comparison(
            "equal_normalized", "OfficeSupply  Co.", "officesupply co.", None
        )
        == "match"
    )


def test_numeric_within_honours_the_larger_of_absolute_and_percent():
    assert (
        cycle_vouching._comparison(
            "numeric_within", "1000", "1005", {"absolute": 10, "percent": 0}
        )
        == "match"
    )
    assert (
        cycle_vouching._comparison(
            "numeric_within", "1000", "1005", {"absolute": 0, "percent": 1}
        )
        == "match"
    )
    assert (
        cycle_vouching._comparison(
            "numeric_within", "1000", "1005", {"absolute": 1, "percent": 0}
        )
        == "mismatch"
    )


def test_a_value_that_will_not_type_is_invalid_evidence_not_a_mismatch():
    """Saying 'mismatch' would report a finding the documents do not support."""

    assert (
        cycle_vouching._comparison("numeric_within", "not a number", "5", None)
        == "invalid_extraction"
    )
    assert (
        cycle_vouching._comparison("date_on_or_before", "whenever", "2024-04-01", None)
        == "invalid_extraction"
    )


def test_present_reads_one_operand():
    assert cycle_vouching._comparison("present", "PO-1", None, None) == "match"
    assert cycle_vouching._comparison("present", "", None, None) == "missing_evidence"


def test_the_operator_vocabulary_matches_what_a_rule_may_name():
    from app import cycle_rulesets

    assert cycle_vouching.OPERATORS == cycle_rulesets.OPERATORS


# ------------------------------------------------------------ state accessors
@pytest.mark.parametrize(
    "state,pending,current",
    [
        ("not_run", True, False),
        ("stale", True, False),
        ("passed", False, True),
        ("failed", False, True),
        # Reached a conclusion the auditor has to look at, which is a result
        # rather than outstanding work.
        ("needs_review", False, True),
    ],
)
def test_execution_state_accessors_read_the_split_fields(state, pending, current):
    item = {"evaluation": {"state": state}}

    assert cycle_vouching.execution_pending(item, cycle=True) is pending
    assert cycle_vouching.execution_current(item, cycle=True) is current


def test_a_stale_disposition_is_not_current():
    """And an evaluated item carrying one is back to being outstanding work."""

    evaluated = {"evaluation": {"state": "passed"}}
    confirmed = {**evaluated, "disposition": {"state": "confirmed", "stale": False}}
    stale = {**evaluated, "disposition": {"state": "confirmed", "stale": True}}

    assert cycle_vouching.disposition_current(confirmed, cycle=True) is True
    assert cycle_vouching.disposition_current(stale, cycle=True) is False
    assert cycle_vouching.disposition_pending(stale, cycle=True) is True
    # An item nothing has evaluated is not a pending disposition; it is pending
    # execution, which is a different queue.
    assert cycle_vouching.disposition_pending({}, cycle=True) is False


def test_assurance_scope_is_structurally_derived():
    assert cycle_vouching.assurance_scope_for({"mode": "evidence_linked"}) == (
        "targeted_evidence_only"
    )
    assert cycle_vouching.assurance_scope_for({"mode": "sample"}) == (
        "sampled_population"
    )
    with pytest.raises(cycle_vouching.CycleSchemaError):
        cycle_vouching.assurance_scope_for({"mode": "whatever"})
