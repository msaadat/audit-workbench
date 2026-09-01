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


def test_non_latin_letter_ratio_ignores_everything_that_is_not_a_letter():
    # Markdown syntax, digits and punctuation must stay out of the denominator,
    # or a heading-heavy note would score differently from the same prose.
    assert text.non_latin_letter_ratio("## Deal capture (2023) — 15 minutes!") == 0.0
    assert text.non_latin_letter_ratio("") == 0.0
    assert text.non_latin_letter_ratio(None) == 0.0
    assert text.non_latin_letter_ratio("1234 — ### ...") == 0.0


def test_non_latin_letter_ratio_reads_accented_european_text_as_latin():
    # Latin Extended-B and below is still the Latin script; a policy naming a
    # French or Turkish counterparty has not drifted out of English.
    assert text.non_latin_letter_ratio("Société Générale confirmed the deal") == 0.0
    assert text.non_latin_letter_ratio("Işbank ağı") == 0.0


def test_non_latin_letter_ratio_separates_borrowed_names_from_drifted_prose():
    borrowed = text.non_latin_letter_ratio(
        "The deal confirms a trade with 中国银行 for USD 5m on 3 February."
    )
    drifted = text.non_latin_letter_ratio(
        "本摘录为财务和投资政策的一部分，文档参考TP/2023/04,版本4.1。"
    )
    assert borrowed < 0.2 < drifted
    assert drifted > 0.85
