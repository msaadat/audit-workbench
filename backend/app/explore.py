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

A query may also carry ``split_by`` (one column) to produce an Excel-style
cross-tab: ``group_by`` fields go down, the ``split_by`` field's values go
across, and ``aggs`` become the cells (default: row count). Value columns are
named ``<value>::<column label>``; row totals land in ``<value>::Total`` and a
separate grand-total row is returned. This is the one cross-tab implementation
— the Query tab's ``split_by`` and the dashboard's legacy ``pivot`` tiles both
go through :func:`build_crosstab`.
"""

from __future__ import annotations

from datetime import date, datetime, time

import polars as pl

PAGE_SIZE_MAX = 500

# A cross-tab explodes horizontally; past this the result is unreadable anyway.
MAX_SPLIT_VALUES = 50
TOTAL_LABEL = "Total"

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


def _value_name(spec: dict) -> str:
    """The alias :func:`_agg_expr` gives an aggregation, as a cross-tab value."""
    if spec.get("func") == "count":
        return "row_count"
    return f"{spec.get('column')}_{spec.get('func')}"


def _split_labels(long: pl.DataFrame, split_field: str) -> list[str]:
    """Distinct split-field values, sorted by native dtype, stringified the way
    ``DataFrame.pivot`` names its output columns (nulls become "null")."""
    return (
        long.select(
            pl.col(split_field)
            .unique()
            .sort(nulls_last=True)
            .cast(pl.String)
            .fill_null("null")
        )
        .to_series()
        .to_list()
    )


def build_crosstab(
    df: pl.DataFrame,
    *,
    filters: list | None,
    row_fields: list[str],
    split_field: str | None,
    value_specs: list | None,
    totals: bool = True,
) -> tuple[pl.DataFrame, dict | None, dict]:
    """Excel-style cross-tab. Returns (wide frame, raw grand-total by column name
    or None, metadata). ``row_fields`` go down, ``split_field`` (one column, its
    values) go across, ``value_specs`` are the cells.

    Totals are re-aggregated from the filtered rows, never summed across cells,
    so a ``mean`` total is the true mean of the underlying rows. Row totals land
    in ``<value>::Total`` columns; the grand-total row is returned separately.
    """
    schema = dict(df.schema)

    filters = [f for f in (filters or []) if f.get("column") or f.get("op")]
    if filters:
        df = df.filter(pl.all_horizontal([_filter_expr(f, schema) for f in filters]))
    filtered_rows = df.height

    row_fields = [c for c in (row_fields or []) if c]
    if not row_fields:
        raise QueryError("A cross-tab needs at least one Group by field.")

    for field in row_fields + ([split_field] if split_field else []):
        if field not in schema:
            raise QueryError(f"Unknown column '{field}'.")
    if split_field is not None and split_field in row_fields:
        raise QueryError(f"'{split_field}' cannot be both a Group by and a Split by field.")

    value_specs = [v for v in (value_specs or []) if v.get("func")] or [{"func": "count"}]
    agg_exprs = [_agg_expr(v, schema) for v in value_specs]
    value_names = [_value_name(v) for v in value_specs]
    if len(set(value_names)) != len(value_names):
        raise QueryError("Duplicate value fields — use each column/function pair once.")

    # Alignment invariant: every frame below enumerates the same distinct
    # row-field combinations in the same order (sorted, nulls last), so they
    # combine by hstack. Joins would silently drop null row-field keys.
    if split_field is None:
        labels: list[str] = []
        wide = df.group_by(row_fields).agg(agg_exprs).sort(row_fields, nulls_last=True)
    else:
        if df.select(pl.col(split_field).n_unique()).item() > MAX_SPLIT_VALUES:
            raise QueryError(
                f"'{split_field}' has more than {MAX_SPLIT_VALUES} distinct values — "
                "filter first, or use it as a Group by field."
            )
        long = (
            df.group_by(row_fields + [split_field])
            .agg(agg_exprs)
            .sort(row_fields + [split_field], nulls_last=True)
        )
        labels = _split_labels(long, split_field)
        wide = None
        for name in value_names:
            piece = long.pivot(
                on=split_field, index=row_fields, values=name, aggregate_function="first"
            ).rename({label: f"{name}::{label}" for label in labels})
            named = [f"{name}::{label}" for label in labels]
            if wide is None:
                wide = piece.select(row_fields + named)
            else:
                wide = wide.hstack(piece.select(named))

    grand_raw: dict | None = None
    if totals:
        if split_field is not None:
            row_totals = (
                df.group_by(row_fields)
                .agg(agg_exprs)
                .sort(row_fields, nulls_last=True)
                .rename({name: f"{name}::{TOTAL_LABEL}" for name in value_names})
            )
            wide = wide.hstack(
                row_totals.select([f"{name}::{TOTAL_LABEL}" for name in value_names])
            )

        grand_raw = {column: None for column in wide.columns}
        overall = df.select(agg_exprs).row(0, named=True)
        if split_field is None:
            for name in value_names:
                grand_raw[name] = overall[name]
        else:
            per_split = (
                df.group_by(split_field)
                .agg(agg_exprs)
                .with_columns(
                    pl.col(split_field).cast(pl.String).fill_null("null").alias("__label__")
                )
            )
            for entry in per_split.iter_rows(named=True):
                for name in value_names:
                    grand_raw[f"{name}::{entry['__label__']}"] = entry[name]
            for name in value_names:
                grand_raw[f"{name}::{TOTAL_LABEL}"] = overall[name]

    meta = {
        "row_fields": row_fields,
        "split_field": split_field,
        "value_names": value_names,
        "column_keys": labels,
        "filtered_rows": filtered_rows,
    }
    return wide, grand_raw, meta


def run_query_full(df: pl.DataFrame, spec: dict) -> tuple[pl.DataFrame, int]:
    """Filters → grouping/aggregation → sort. Returns (frame, filtered_row_count).

    When the spec carries ``split_by``, the result is a cross-tab (with the
    grand total appended as a final row for export). Otherwise it is the flat
    grouped/filtered frame. No pagination — this is the full result, used
    directly for Excel export and sliced by :func:`run_query` for the UI.
    """
    if spec.get("split_by"):
        wide, grand_raw, meta = build_crosstab(
            df,
            filters=spec.get("filters"),
            row_fields=spec.get("group_by") or [],
            split_field=spec.get("split_by"),
            value_specs=spec.get("aggs"),
            totals=spec.get("totals", True),
        )
        if grand_raw is not None and wide.height:
            grand = pl.DataFrame(
                {column: [grand_raw[column]] for column in wide.columns}, schema=wide.schema
            )
            wide = pl.concat([wide, grand])
        return wide, meta["filtered_rows"]

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

    projection = [c for c in (spec.get("columns") or []) if c]
    if projection and not group_by and not aggs:
        unknown = [c for c in projection if c not in schema]
        if unknown:
            raise QueryError(f"Unknown column '{unknown[0]}'.")
        seen = set()
        projection = [c for c in projection if not (c in seen or seen.add(c))]
        df = df.select(projection)
    return df, filtered_rows


def run_query(df: pl.DataFrame, spec: dict) -> dict:
    """Paginated query payload for the UI. With ``split_by`` the payload is a
    cross-tab: the full wide frame (cross-tabs stay small — one row per group
    combination) plus ``row_fields``/``split_field``/``value_names``/
    ``column_keys``/``grand_total`` so the SPA can render a grouped grid."""
    if spec.get("split_by"):
        wide, grand_raw, meta = build_crosstab(
            df,
            filters=spec.get("filters"),
            row_fields=spec.get("group_by") or [],
            split_field=spec.get("split_by"),
            value_specs=spec.get("aggs"),
            totals=spec.get("totals", True),
        )
        return {
            "total_rows": wide.height,
            "filtered_rows": meta["filtered_rows"],
            "page": 1,
            "page_size": max(wide.height, 1),
            "row_fields": meta["row_fields"],
            "split_field": meta["split_field"],
            "value_names": meta["value_names"],
            "column_keys": meta["column_keys"],
            "grand_total": (
                [_serialize(grand_raw[column]) for column in wide.columns]
                if grand_raw is not None
                else None
            ),
            **frame_payload(wide),
        }

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
