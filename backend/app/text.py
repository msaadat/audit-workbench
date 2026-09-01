"""Sentence-level formatting for auditor-facing copy.

Domain-neutral by construction: it knows about counting and English, nothing
about audits, so any layer may import it.

It exists because `f"{n} row(s)"` had been written 170 times across this
package. Individually that construction is trivial; collectively it was the
clearest signal in the product that a program rather than a person wrote the
words on screen.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-z0-9]+")


def relevance_tokens(value: object) -> set[str]:
    """The comparable words of a phrase, a column name, or a requirement.

    Deliberately crude: lowercase alphanumeric runs of more than two
    characters, so ``PO_TOTAL_AMOUNT`` and "the amount of the purchase order"
    share ``amount``. It lives here rather than beside either caller because
    two stages must score relevance *identically* — a matrix row may not claim
    that no table can answer its requirement when the test worker, using this
    same function, would have ranked a table into the prompt for it.
    """
    return {token for token in _TOKEN.findall(str(value or "").casefold()) if len(token) > 2}


#: Latin Extended-B ends here. Everything above is a different script: CJK,
#: kana, hangul, Cyrillic, Greek, and the abjads and abugidas beyond them.
#: Accented European text stays below it and so reads as Latin.
_LATIN_CEILING = 0x24F


def non_latin_letter_ratio(value: object) -> float:
    """Share of a string's letters that are written outside the Latin script.

    Letters rather than characters, so Markdown syntax, digits, punctuation and
    whitespace stay out of the denominator and a one-line note is measured on
    the same scale as a page of prose. The measure is a ratio rather than a
    flag because the interesting cases are not symmetrical: prose that has
    drifted wholesale into another script scores near 1.0, while English prose
    naming a Chinese counterparty scores a few percent, and only a threshold
    can tell those apart.
    """
    letters = [character for character in str(value or "") if character.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for character in letters if ord(character) > _LATIN_CEILING) / len(letters)


def plural_word(count: int, singular: str, plural: str | None = None) -> str:
    """Just the noun, for callers that render the number themselves."""
    return singular if abs(count) == 1 else (plural or f"{singular}s")


def counted(count: int, singular: str, plural: str | None = None) -> str:
    """``3 findings`` / ``1 finding``, with a thousands separator.

    Irregular nouns pass their own plural: ``counted(2, "analysis", "analyses")``.
    """
    return f"{count:,} {plural_word(count, singular, plural)}"


def verb(count: int, singular: str = "needs", plural: str = "need") -> str:
    """Subject-verb agreement for a sentence built around ``counted``.

    ``f"{counted(n, 'item')} {verb(n)} you"`` reads correctly at n = 1 and n = 2,
    which the "(s)" construction never managed.
    """
    return singular if abs(count) == 1 else plural
