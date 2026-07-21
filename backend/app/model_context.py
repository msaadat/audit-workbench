"""Compact, JSON-safe frame projections for model context.

Models may inspect real workspace values.  The projection remains bounded so
large audit populations do not overwhelm a model's context window.
"""

from __future__ import annotations

import polars as pl


def project_frame(df: pl.DataFrame, *, row_limit: int = 40) -> dict:
    """Return schema, aggregate statistics, and an unmasked row preview."""
    limit = max(0, int(row_limit))
    summary: dict[str, dict] = {}
    for name, dtype in df.schema.items():
        if dtype.is_numeric():
            col = df[name]
            summary[name] = {
                "min": _round(col.min()),
                "max": _round(col.max()),
                "mean": _round(col.mean()),
                "nulls": int(col.null_count()),
            }
    preview = df.head(limit)
    return {
        "shape": [df.height, df.width],
        "columns": df.columns,
        "dtypes": [str(value) for value in df.dtypes],
        "numeric_summary": summary,
        "rows": [[_json(value) for value in row] for row in preview.iter_rows()],
        "truncated": df.height > preview.height,
    }


def project_column_profile(
    profile: dict,
    *,
    category_limit: int = 30,
    include_category_values: bool = True,
) -> dict:
    """Return compact profiler metadata with optional category literals."""
    meta = {
        "name": profile["name"],
        "dtype": profile["dtype"],
        "type": profile["inferred_type"],
        "nulls_pct": profile["blank_pct"],
        "distinct": profile["distinct_count"],
    }
    if profile["inferred_type"] in ("numeric", "date"):
        meta.update(min=profile.get("min"), max=profile.get("max"))
        if profile.get("mean") is not None:
            meta["mean"] = profile["mean"]
    if (
        include_category_values
        and profile["distinct_count"] <= category_limit
        and profile.get("top_values")
    ):
        meta["values"] = [item.get("value") for item in profile["top_values"]]
    return meta


def _round(value):
    return round(value, 4) if isinstance(value, float) else value


def _json(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
