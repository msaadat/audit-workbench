"""Server-side query engine for the Explore tab.

Executes a declarative query spec against a Polars frame::

    {
      "filters":  [{"column": "amount", "op": "gt", "value": "1000"}, ...],
      "group_by": ["branch"],
      "aggs":     [{"column": "amount", "func": "sum"}, ...],
      "sort":     [{"column": "amount_sum", "desc": true}],
      "page": 1, "page_size": 50
    }

Filters AND together. Filter values arrive as strings from the UI and are
cast to the column's dtype here — a numeric column compares numerically, a
date column by parsed date. Aggregated columns are named ``<column>_<func>``
(plus ``row_count`` for ``count``), which is what sort specs reference after
grouping.
"""

from __future__ import annotations

from datetime import date, datetime, time

import polars as pl

PAGE_SIZE_MAX = 500

FILTER_OPS = {
    "eq": "equals",
    "neq": "not equals",
    "contains": "contains",
    "not_contains": "does not contain",
    "starts_with": "starts with",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "between": "between",
    "blank": "is blank",
    "not_blank": "is not blank",
    "in": "in list",
}

AGG_FUNCS = ("count", "sum", "mean", "min", "max", "n_unique")


class QueryError(ValueError):
    """A user-facing query problem (unknown column, bad value, bad op)."""


def _cast_value(raw: str, dtype: pl.DataType):
    text = str(raw).strip()
    if dtype.is_numeric():
        try:
            return float(text)
        except ValueError as error:
            raise QueryError(f"'{text}' is not a number.") from error
    if dtype.is_temporal():
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if dtype == pl.Date:
                return parsed.date()
            return parsed
        raise QueryError(f"'{text}' is not a date (try YYYY-MM-DD).")
    return text


def _filter_expr(spec: dict, schema: dict[str, pl.DataType]) -> pl.Expr:
    column = spec.get("column")
    op = spec.get("op")
    if column not in schema:
        raise QueryError(f"Unknown column '{column}'.")
    if op not in FILTER_OPS:
        raise QueryError(f"Unknown filter operator '{op}'.")

    dtype = schema[column]
    col = pl.col(column)

    if op == "blank":
        if dtype == pl.String:
            return col.is_null() | (col.str.strip_chars() == "")
        return col.is_null()
    if op == "not_blank":
        if dtype == pl.String:
            return col.is_not_null() & (col.str.strip_chars() != "")
        return col.is_not_null()

    raw = spec.get("value", "")
    if op in ("contains", "not_contains", "starts_with"):
        text = str(raw)
        target = col if dtype == pl.String else col.cast(pl.String)
        if op == "starts_with":
            return target.str.starts_with(text)
        matched = target.str.contains(text, literal=True)
        return matched.fill_null(False) if op == "contains" else ~matched.fill_null(False)

    if op == "in":
        items = [v.strip() for v in str(raw).split(",") if v.strip()]
        if not items:
            raise QueryError("'in list' needs comma-separated values.")
        values = [_cast_value(v, dtype) for v in items]
        return col.is_in(values)

    if op == "between":
        low, high = spec.get("value", ""), spec.get("value2", "")
        return (col >= _cast_value(low, dtype)) & (col <= _cast_value(high, dtype))

    value = _cast_value(raw, dtype)
    if op == "eq":
        # Case-insensitive string equality: auditors type "karachi", data says "Karachi".
        if dtype == pl.String:
            return col.str.to_lowercase() == str(value).lower()
        return col == value
    if op == "neq":
        if dtype == pl.String:
            return col.str.to_lowercase() != str(value).lower()
        return col != value
    return {"gt": col > value, "gte": col >= value, "lt": col < value, "lte": col <= value}[op]


def _agg_expr(spec: dict, schema: dict[str, pl.DataType]) -> pl.Expr:
    func = spec.get("func")
    if func not in AGG_FUNCS:
        raise QueryError(f"Unknown aggregation '{func}'.")
    if func == "count":
        return pl.len().alias("row_count")

    column = spec.get("column")
    if column not in schema:
        raise QueryError(f"Unknown column '{column}'.")
    col = pl.col(column)
    alias = f"{column}_{func}"
    if func in ("sum", "mean") and not schema[column].is_numeric():
        raise QueryError(f"'{column}' is not numeric — cannot {func} it.")
    return getattr(col, func)().alias(alias)


def _serialize(value):
    if value is None or isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return str(value)


def frame_payload(df: pl.DataFrame, limit: int | None = None) -> dict:
    """A JSON-safe {columns, dtypes, rows} slice of a frame."""
    sliced = df.head(limit) if limit is not None else df
    return {
        "columns": sliced.columns,
        "dtypes": [str(t) for t in sliced.dtypes],
        "rows": [[_serialize(v) for v in row] for row in sliced.rows()],
    }


def run_query_full(df: pl.DataFrame, spec: dict) -> tuple[pl.DataFrame, int]:
    """Filters → grouping/aggregation → sort. Returns (frame, filtered_row_count).

    No pagination — this is the full result, used directly for Excel export
    and sliced by :func:`run_query` for the UI.
    """
    schema = dict(df.schema)

    filters = [f for f in (spec.get("filters") or []) if f.get("column") or f.get("op")]
    if filters:
        df = df.filter(pl.all_horizontal([_filter_expr(f, schema) for f in filters]))
    filtered_rows = df.height

    group_by = [c for c in (spec.get("group_by") or []) if c]
    aggs = [a for a in (spec.get("aggs") or []) if a.get("func")]
    for column in group_by:
        if column not in schema:
            raise QueryError(f"Unknown column '{column}'.")

    if group_by:
        agg_exprs = [_agg_expr(a, schema) for a in aggs] or [pl.len().alias("row_count")]
        df = df.group_by(group_by, maintain_order=True).agg(agg_exprs)
    elif aggs:
        df = df.select([_agg_expr(a, schema) for a in aggs])

    for sort in reversed(spec.get("sort") or []):
        column = sort.get("column")
        if column in df.columns:
            df = df.sort(column, descending=bool(sort.get("desc")), nulls_last=True)
    return df, filtered_rows


def run_query(df: pl.DataFrame, spec: dict) -> dict:
    result, filtered_rows = run_query_full(df, spec)

    page_size = min(int(spec.get("page_size") or 50), PAGE_SIZE_MAX)
    page = max(int(spec.get("page") or 1), 1)
    offset = (page - 1) * page_size

    return {
        "total_rows": result.height,
        "filtered_rows": filtered_rows,
        "page": page,
        "page_size": page_size,
        **frame_payload(result.slice(offset, page_size)),
    }
