"""Safe resolution of user/model supplied field names to source columns.

Source headers are preserved exactly for audit traceability.  Declarative API
specifications may nevertheless arrive with different casing, so callers use
these helpers to accept a unique case-insensitive match without fuzzy guessing
between semantically different fields.
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Type


# Word endings that mark a column as an identifier rather than as the thing it
# identifies. Stripped before comparing two columns by subject, so ``VENDOR_ID``
# and ``VENDOR_INVOICE_NUMBER`` are compared as "vendor" against "vendor
# invoice".
IDENTIFIER_SEGMENTS = frozenset(
    {"id", "ids", "code", "key", "no", "num", "number", "ref", "reference"}
)
_SEGMENT_SPLIT = re.compile(r"[A-Za-z0-9]+")


def subject_tokens(column: str) -> frozenset[str]:
    """What a column names, with the fact that it is an identifier removed.

    Two callers depend on the same reading of a column name and must not drift
    apart. The duplicate-key rule uses it to recognise that one key column only
    qualifies another (``VENDOR_ID`` inside ``VENDOR_INVOICE_NUMBER``), and the
    probe sweep uses it to tell two *roles* of one entity
    (``VERIFIED_BY_ID`` / ``SUPERVISOR_APPROVAL_ID``, disjoint subjects) from two
    statements of one *attribute* (``REQUESTER_DEPARTMENT`` / ``DEPARTMENT``, one
    subject contained in the other). Those two shapes want opposite comparisons,
    so a shared reading of the names is what keeps the two rules consistent.
    """
    segments = [segment.lower() for segment in _SEGMENT_SPLIT.findall(str(column))]
    while segments and segments[-1] in IDENTIFIER_SEGMENTS:
        segments = segments[:-1]
    return frozenset(segments)


def matching_column(requested: object, columns: list[str]) -> str | None:
    """Return an exact or unique case-insensitive match, otherwise ``None``."""
    name = str(requested or "").strip()
    if not name:
        return None
    if name in columns:
        return name
    matches = [column for column in columns if column.casefold() == name.casefold()]
    return matches[0] if len(matches) == 1 else None


def resolve_column(
    requested: object,
    columns: list[str],
    *,
    table: str | None = None,
    error_type: Type[ValueError] = ValueError,
) -> str:
    """Resolve a field name without unsafe semantic/fuzzy substitution."""
    name = str(requested or "").strip()
    match = matching_column(name, columns)
    if match is not None:
        return match

    folded = name.casefold()
    case_matches = [column for column in columns if column.casefold() == folded]
    scope = f" in '{table}'" if table else " in this table"
    if len(case_matches) > 1:
        raise error_type(
            f"Column '{name}' is ambiguous{scope}; use the exact source spelling."
        )

    folded_to_columns: dict[str, list[str]] = {}
    for column in columns:
        folded_to_columns.setdefault(column.casefold(), []).append(column)
    suggestions = []
    for candidate in get_close_matches(folded, list(folded_to_columns), n=3, cutoff=0.55):
        suggestions.extend(folded_to_columns[candidate])
    hint = f" Similar columns: {', '.join(suggestions)}." if suggestions else ""
    raise error_type(f"Column '{name}' not found{scope}.{hint}")


def resolve_columns(
    requested: object,
    columns: list[str],
    *,
    table: str | None = None,
    error_type: Type[ValueError] = ValueError,
) -> list[str]:
    """Normalize a scalar/list field input and resolve every non-empty name."""
    values = [requested] if isinstance(requested, str) else list(requested or [])
    return [
        resolve_column(value, columns, table=table, error_type=error_type)
        for value in values
        if str(value or "").strip()
    ]
