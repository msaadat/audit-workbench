"""Column and dataset profiling for the workspace Profile tab.

Adapted from the validation platform's EDA module, but for *typed* frames:
the loader already inferred dtypes, so numeric/date detection starts from the
schema instead of parse attempts. String columns still get the categorical /
id / free-text heuristics.

All output is plain dicts, JSON-ready for the SPA.
"""

from __future__ import annotations

import re

import polars as pl

# Profiling runs on a capped sample — enough to characterize, cheap to compute.
MAX_ROWS = 200_000
TOP_VALUES = 8

CATEGORICAL_MAX_DISTINCT = 25
CATEGORICAL_MAX_DISTINCT_PCT = 60.0
ID_UNIQUENESS_PCT = 95.0

_ID_NAME_RE = re.compile(r"(?i)\b(id|ref|no|code|number|cnic|account|acct)\b|_(id|no)$")


def _fmt(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _blank_expr(column: str, dtype: pl.DataType) -> pl.Expr:
    if dtype == pl.String:
        return pl.col(column).is_null() | (pl.col(column).str.strip_chars() == "")
    return pl.col(column).is_null()


def profile_column(df: pl.DataFrame, column: str) -> dict:
    dtype = df.schema[column]
    total = df.height
    blank_count = int(df.select(_blank_expr(column, dtype).sum()).item()) if total else 0
    values = df[column].drop_nulls()
    if dtype == pl.String:
        stripped = values.str.strip_chars()
        values = values.filter(stripped != "")

    non_blank = len(values)
    distinct = values.n_unique() if non_blank else 0

    profile = {
        "name": column,
        "dtype": str(dtype),
        "total": total,
        "blank_count": blank_count,
        "blank_pct": round(100.0 * blank_count / total, 2) if total else 0.0,
        "distinct_count": distinct,
        "distinct_pct": round(100.0 * distinct / non_blank, 2) if non_blank else 0.0,
        "inferred_type": "text",
        "min": None,
        "max": None,
        "mean": None,
        "top_values": [],
    }

    if non_blank == 0:
        profile["inferred_type"] = "empty"
        return profile

    if dtype.is_numeric():
        profile["inferred_type"] = "numeric"
        profile["min"] = _fmt(values.min())
        profile["max"] = _fmt(values.max())
        profile["mean"] = _fmt(float(values.mean()))
    elif dtype.is_temporal():
        profile["inferred_type"] = "date"
        profile["min"] = _fmt(values.min())
        profile["max"] = _fmt(values.max())
    elif dtype == pl.Boolean:
        profile["inferred_type"] = "boolean"
    else:
        profile["min"] = _fmt(values.min())
        profile["max"] = _fmt(values.max())
        if (
            distinct <= CATEGORICAL_MAX_DISTINCT
            and profile["distinct_pct"] <= CATEGORICAL_MAX_DISTINCT_PCT
        ):
            profile["inferred_type"] = "categorical"
        elif profile["distinct_pct"] >= ID_UNIQUENESS_PCT or _ID_NAME_RE.search(column):
            profile["inferred_type"] = "id" if profile["distinct_pct"] >= 50.0 else "text"

    # Integer columns named like ids are ids (account numbers parse as ints).
    if (
        profile["inferred_type"] == "numeric"
        and dtype.is_integer()
        and _ID_NAME_RE.search(column)
        and profile["distinct_pct"] >= 50.0
    ):
        profile["inferred_type"] = "id"

    counts = (
        values.value_counts(sort=True)
        .head(TOP_VALUES)
        .rows()
    )
    profile["top_values"] = [
        {
            "value": _fmt(value),
            "count": int(count),
            "pct": round(100.0 * count / non_blank, 2),
        }
        for value, count in counts
    ]
    return profile


def profile_table(df: pl.DataFrame) -> dict:
    sampled = df.height > MAX_ROWS
    sample = df.head(MAX_ROWS)

    duplicate_rows = int(sample.is_duplicated().sum())
    return {
        "rows": df.height,
        "columns": df.width,
        "sampled": sampled,
        "sample_rows": sample.height,
        "duplicate_rows": duplicate_rows,
        "estimated_size_mb": round(df.estimated_size("mb"), 2),
        "column_profiles": [profile_column(sample, c) for c in sample.columns],
    }


def schema_payload(df: pl.DataFrame) -> list[dict]:
    """Lightweight column metadata for pickers (Explore/Analytics forms)."""
    payload = []
    for column, dtype in df.schema.items():
        kind = "text"
        if dtype.is_numeric():
            kind = "numeric"
        elif dtype.is_temporal():
            kind = "date"
        elif dtype == pl.Boolean:
            kind = "boolean"
        payload.append({"name": column, "dtype": str(dtype), "kind": kind})
    return payload
