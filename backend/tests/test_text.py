"""The shared plural helper.

It replaced 170 hand-written `f"{n} row(s)"` sites, so the cases it gets wrong
are the cases that will read wrongly across the whole product.
"""

from __future__ import annotations

import pytest

from app import text


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0 rows"), (1, "1 row"), (2, "2 rows"), (1000, "1,000 rows")],
)
def test_counted_agrees_with_its_number(count, expected):
    assert text.counted(count, "row") == expected


def test_counted_takes_an_irregular_plural():
    assert text.counted(1, "analysis", "analyses") == "1 analysis"
    assert text.counted(3, "analysis", "analyses") == "3 analyses"


def test_verb_agrees_so_a_built_sentence_reads_at_one_and_at_many():
    assert f"{text.counted(1, 'item')} {text.verb(1)} you" == "1 item needs you"
    assert f"{text.counted(2, 'item')} {text.verb(2)} you" == "2 items need you"
    assert text.verb(1, "has", "have") == "has"
    assert text.verb(0, "has", "have") == "have"


def test_zero_takes_the_plural_the_way_english_does():
    # "0 rows have no test", never "0 row has no test".
    assert text.counted(0, "row") == "0 rows"
    assert text.plural_word(0, "row") == "rows"


def test_plural_word_omits_the_number_for_callers_that_render_it_themselves():
    assert text.plural_word(1, "finding") == "finding"
    assert text.plural_word(4, "finding") == "findings"
