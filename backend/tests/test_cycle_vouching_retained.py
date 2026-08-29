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


# ------------------------------------------------------------------ verdicts
def test_cannot_determine_is_a_verdict_and_not_a_pass():
    """The reader had both values and still could not settle the requirement.

    Distinct from ``missing_evidence`` and ``invalid_extraction``, which are
    resolution failures decided before anything is judged — but it lands where
    they land, because none of the three is a tested pass.
    """

    assert "cannot_determine" in cycle_vouching.ASSERTION_VERDICTS
    item = {"result_by_assertion": {"a": {"verdict": "cannot_determine"}}}

    assert cycle_vouching._aggregate_evaluation(item) == "incomplete"


def test_a_disagreement_fails_the_item_even_beside_one_it_could_not_settle():
    item = {
        "result_by_assertion": {
            "a": {"verdict": "cannot_determine"},
            "b": {"verdict": "mismatch"},
        }
    }

    assert cycle_vouching._aggregate_evaluation(item) == "failed"


def test_an_unjudged_assertion_leaves_the_item_pending():
    """Nothing infers agreement from the absence of a verdict."""

    item = {"result_by_assertion": {"a": {"verdict": "not_run"}}}

    assert cycle_vouching._aggregate_evaluation(item) == "not_run"
    assert cycle_vouching.execution_pending(item, cycle=True)


def test_the_judged_vocabulary_maps_onto_the_durable_one():
    """What a reader may answer, and what each answer means in the record."""

    assert set(cycle_vouching.JUDGED_VERDICTS) == {
        "agrees",
        "disagrees",
        "cannot_determine",
    }
    assert set(cycle_vouching.JUDGED_VERDICTS.values()) <= cycle_vouching.ASSERTION_VERDICTS
    assert cycle_vouching.JUDGED_VERDICTS["agrees"] == "match"
    assert cycle_vouching.JUDGED_VERDICTS["disagrees"] == "mismatch"


def test_no_comparison_operator_survives_on_the_module():
    """Agreement is judged against the values, so nothing here computes it."""

    assert not hasattr(cycle_vouching, "OPERATORS")
    assert not hasattr(cycle_vouching, "_comparison")


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


def test_the_published_vocabulary_is_what_a_reader_may_answer():
    """The UI binds its pickers to this, so a stale key here is a broken screen.

    Caught late: nothing exercised ``metadata()`` while the operator table was
    being removed, so it referenced a name that no longer existed and every
    caller would have raised. It is asserted now rather than left to a caller.
    """

    published = cycle_vouching.metadata()

    assert published["verdicts"] == sorted(cycle_vouching.JUDGED_VERDICTS)
    assert "operators" not in published
