"""Excel-style pivot (cross-tab) engine.

Executes a declarative pivot spec against a Polars frame::

    {
      "filters": [{"column": "year", "op": "eq", "value": "2025"}, ...],
      "rows":    ["region", "city"],
      "columns": "quarter",
      "values":  [{"column": "amount", "func": "sum"}, {"func": "count"}],
      "totals":  true
    }

Filters reuse the Explore filter specs. ``rows`` (one or more fields) go down,
``columns`` (at most one field) goes across, ``values`` are Explore-style
aggregations (default: row count). Value columns in the result are named
``<value>::<column label>`` — column labels are the stringified column-field
values (``null`` for blanks), which is how Polars names pivoted columns.

Totals are re-aggregated from the filtered rows, never summed across cells,
so a ``mean`` total is the true mean of the underlying rows rather than a
mean of means. Row totals land in ``<value>::Total`` columns; the grand-total
row is returned separately (aligned to ``columns``) so the frontend can
render it as a footer.

Builds on :mod:`.explore` (filters, aggregations, serialization) without
touching it.
"""

from __future__ import annotations

import polars as pl

from .explore import QueryError, _agg_expr, _filter_expr, _serialize, frame_payload

# Pivots explode horizontally; past this the result is unreadable anyway.
MAX_COLUMN_VALUES = 50

TOTAL_LABEL = "Total"


def _value_name(spec: dict) -> str:
    """The alias :func:`explore._agg_expr` gives this aggregation."""
    if spec.get("func") == "count":
        return "row_count"
    return f"{spec.get('column')}_{spec.get('func')}"


def _column_labels(long: pl.DataFrame, column_field: str) -> list[str]:
    """Distinct column-field values, sorted by native dtype, stringified the
    way ``DataFrame.pivot`` names its output columns (nulls become "null")."""
    return (
        long.select(
            pl.col(column_field)
            .unique()
            .sort(nulls_last=True)
            .cast(pl.String)
            .fill_null("null")
        )
        .to_series()
        .to_list()
    )


def _build(df: pl.DataFrame, spec: dict) -> tuple[pl.DataFrame, dict | None, dict]:
    """Compute the cross-tab. Returns (wide frame, raw grand-total by column
    name or None, payload metadata)."""
    schema = dict(df.schema)

    filters = [f for f in (spec.get("filters") or []) if f.get("column") or f.get("op")]
    if filters:
        df = df.filter(pl.all_horizontal([_filter_expr(f, schema) for f in filters]))
    filtered_rows = df.height

    row_fields = [c for c in (spec.get("rows") or []) if c]
    if not row_fields:
        raise QueryError("Pivot needs at least one row field.")

    raw_columns = spec.get("columns") or []
    if isinstance(raw_columns, str):
        raw_columns = [raw_columns]
    column_fields = [c for c in raw_columns if c]
    if len(column_fields) > 1:
        raise QueryError("Pivot supports at most one column field.")
    column_field = column_fields[0] if column_fields else None

    for field in row_fields + column_fields:
        if field not in schema:
            raise QueryError(f"Unknown column '{field}'.")
    if column_field is not None and column_field in row_fields:
        raise QueryError(f"'{column_field}' cannot be both a row and a column field.")

    value_specs = [v for v in (spec.get("values") or []) if v.get("func")]
    if not value_specs:
        value_specs = [{"func": "count"}]
    agg_exprs = [_agg_expr(v, schema) for v in value_specs]
    value_names = [_value_name(v) for v in value_specs]
    if len(set(value_names)) != len(value_names):
        raise QueryError("Duplicate value fields — each column/function pair once.")

    # Alignment invariant: every frame below enumerates the same distinct
    # row-field combinations in the same order (sorted, nulls last), so they
    # combine by hstack. Joins would silently drop null row-field keys.
    if column_field is None:
        labels: list[str] = []
        wide = df.group_by(row_fields).agg(agg_exprs).sort(row_fields, nulls_last=True)
    else:
        if df.select(pl.col(column_field).n_unique()).item() > MAX_COLUMN_VALUES:
            raise QueryError(
                f"'{column_field}' has more than {MAX_COLUMN_VALUES} distinct values — "
                "filter first, or use it as a row field."
            )
        long = (
            df.group_by(row_fields + [column_field])
            .agg(agg_exprs)
            .sort(row_fields + [column_field], nulls_last=True)
        )
        labels = _column_labels(long, column_field)
        wide = None
        for name in value_names:
            piece = long.pivot(
                on=column_field, index=row_fields, values=name, aggregate_function="first"
            ).rename({label: f"{name}::{label}" for label in labels})
            named = [f"{name}::{label}" for label in labels]
            if wide is None:
                wide = piece.select(row_fields + named)
            else:
                wide = wide.hstack(piece.select(named))

    grand_raw: dict | None = None
    if spec.get("totals", True):
        if column_field is not None:
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
        if column_field is None:
            for name in value_names:
                grand_raw[name] = overall[name]
        else:
            per_column = (
                df.group_by(column_field)
                .agg(agg_exprs)
                .with_columns(
                    pl.col(column_field).cast(pl.String).fill_null("null").alias("__label__")
                )
            )
            for entry in per_column.iter_rows(named=True):
                for name in value_names:
                    grand_raw[f"{name}::{entry['__label__']}"] = entry[name]
            for name in value_names:
                grand_raw[f"{name}::{TOTAL_LABEL}"] = overall[name]

    meta = {
        "row_fields": row_fields,
        "column_field": column_field,
        "value_names": value_names,
        "column_keys": labels,
        "filtered_rows": filtered_rows,
    }
    return wide, grand_raw, meta


def run_pivot(df: pl.DataFrame, spec: dict) -> dict:
    """The JSON payload for the pivot UI: metadata + wide frame + grand total."""
    wide, grand_raw, meta = _build(df, spec)
    return {
        **meta,
        "grand_total": (
            [_serialize(grand_raw[column]) for column in wide.columns]
            if grand_raw is not None
            else None
        ),
        **frame_payload(wide),
    }


def run_pivot_full(df: pl.DataFrame, spec: dict) -> tuple[pl.DataFrame, int]:
    """The cross-tab as one frame for Excel export — grand total appended as a
    final row with blank row fields. Returns (frame, filtered_row_count)."""
    wide, grand_raw, meta = _build(df, spec)
    if grand_raw is not None and wide.height:
        grand = pl.DataFrame(
            {column: [grand_raw[column]] for column in wide.columns}, schema=wide.schema
        )
        wide = pl.concat([wide, grand])
    return wide, meta["filtered_rows"]
