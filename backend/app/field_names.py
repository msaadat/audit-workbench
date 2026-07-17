"""Safe resolution of user/model supplied field names to source columns.

Source headers are preserved exactly for audit traceability.  Declarative API
specifications may nevertheless arrive with different casing, so callers use
these helpers to accept a unique case-insensitive match without fuzzy guessing
between semantically different fields.
"""

from __future__ import annotations

from difflib import get_close_matches
from typing import Type


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
