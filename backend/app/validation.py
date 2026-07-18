"""Field-wise data validation rules, Polars-only.

A rule set is a saved collection of per-column (and table-level) checks bound
to a table by name — the spec-not-data sibling of analyses/tiles, so re-running
against replaced or newly uploaded data needs no extra machinery.

Each check lives in ``CHECKS`` with parameter metadata that drives the SPA's
rule-builder forms (same pattern as :data:`analytics.ANALYTICS`). Column-scope
checks build a boolean *violation* expression; table-scope checks compute
their own counts. Null values only violate ``required`` — every other check
passes nulls, so blanks and content rules compose instead of double-counting.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Callable

import polars as pl

from .explore import QueryError, frame_payload
from .field_names import matching_column, resolve_column, resolve_columns

DETAIL_PREVIEW_ROWS = 50
SEVERITIES = ("fail", "warn")

# A callable resolving a table name to its current frame — checks that look
# across tables (referential) receive it; everything else ignores it.
Resolve = Callable[[str], pl.DataFrame]


def _column(df: pl.DataFrame, column: str | None) -> str:
    if not column:
        raise QueryError("This check needs a column.")
    return resolve_column(column, df.columns, error_type=QueryError)


def _numeric(df: pl.DataFrame, column: str) -> pl.Expr:
    return pl.col(_column(df, column)).cast(pl.Float64, strict=False)


def _text(df: pl.DataFrame, column: str) -> pl.Expr:
    return pl.col(_column(df, column)).cast(pl.String)


def _date(df: pl.DataFrame, column: str) -> pl.Expr:
    name = _column(df, column)
    dtype = df.schema[name]
    if dtype == pl.Date:
        return pl.col(name)
    if dtype.is_temporal():
        return pl.col(name).cast(pl.Date)
    return pl.col(name).cast(pl.String).str.to_date(strict=False)


def _parse_date(value, label: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as error:
        raise QueryError(f"{label} must be a date like 2025-12-31.") from error


def _number(params: dict, name: str) -> float | None:
    value = params.get(name)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise QueryError(f"'{name}' must be a number.") from error


# ------------------------------------------------------- column-scope checks
def check_required(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    name = _column(df, column)
    expr = pl.col(name).is_null()
    if df.schema[name] == pl.String:
        expr = expr | (pl.col(name).str.strip_chars() == "")
    return expr


def check_numeric_sign(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    mode = params.get("mode") or "positive"
    value = _numeric(df, column)
    if mode == "positive":
        return value <= 0
    if mode == "non_negative":
        return value < 0
    if mode == "nonzero":
        return value == 0
    if mode == "negative":
        return value >= 0
    raise QueryError(f"Unknown sign mode '{mode}'.")


def check_range(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    low, high = _number(params, "min"), _number(params, "max")
    if low is None and high is None:
        raise QueryError("Range needs a min and/or a max.")
    value = _numeric(df, column)
    expr = pl.lit(False)
    if low is not None:
        expr = expr | (value < low)
    if high is not None:
        expr = expr | (value > high)
    return expr


def check_allowed_values(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    values = [str(v) for v in (params.get("values") or []) if str(v).strip() != ""]
    if not values:
        raise QueryError("Allowed-values needs at least one value.")
    value = _text(df, column).str.strip_chars()
    if params.get("ignore_case"):
        return ~value.str.to_lowercase().is_in([v.strip().lower() for v in values])
    return ~value.is_in([v.strip() for v in values])


def check_pattern(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    regex = str(params.get("regex") or "").strip()
    if not regex:
        raise QueryError("Pattern needs a regular expression.")
    try:
        re.compile(regex)
    except re.error as error:
        raise QueryError(f"Invalid regular expression: {error}") from error
    if (params.get("mode") or "full") == "full":
        regex = f"^(?:{regex})$"
    return ~_text(df, column).str.contains(regex)


def check_length(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    low, high = _number(params, "min"), _number(params, "max")
    if low is None and high is None:
        raise QueryError("Length needs a min and/or a max.")
    length = _text(df, column).str.len_chars()
    expr = pl.lit(False)
    if low is not None:
        expr = expr | (length < int(low))
    if high is not None:
        expr = expr | (length > int(high))
    return expr


def check_date_range(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    low = _parse_date(params.get("min"), "Earliest date")
    high = _parse_date(params.get("max"), "Latest date")
    not_future = bool(params.get("not_in_future"))
    if low is None and high is None and not not_future:
        raise QueryError("Date range needs a min, a max, or 'not in future'.")
    value = _date(df, column)
    expr = pl.lit(False)
    if low is not None:
        expr = expr | (value < low)
    if high is not None:
        expr = expr | (value > high)
    if not_future:
        expr = expr | (value > date.today())
    return expr


def check_unique(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    name = _column(df, column)
    return pl.col(name).is_duplicated() & pl.col(name).is_not_null()


def check_referential(
    df: pl.DataFrame, column: str, params: dict, resolve: Resolve | None
) -> pl.Expr:
    lookup_table = str(params.get("lookup_table") or "").strip()
    lookup_column = str(params.get("lookup_column") or "").strip()
    if not lookup_table or not lookup_column:
        raise QueryError("Referential needs a lookup table and column.")
    if resolve is None:
        raise QueryError("Lookup tables are not available in this context.")
    try:
        lookup = resolve(lookup_table)
    except Exception as error:
        raise QueryError(f"Could not load lookup table '{lookup_table}': {error}") from error
    lookup_column = resolve_column(
        lookup_column, lookup.columns, table=lookup_table, error_type=QueryError
    )
    # Values compare as trimmed strings so an int code matches its text twin.
    allowed = (
        lookup.select(
            pl.col(lookup_column).cast(pl.String).str.strip_chars().drop_nulls().unique()
        )
        .to_series()
        .to_list()
    )
    return ~_text(df, column).str.strip_chars().is_in(allowed)


_COMPARE_OPS = ("ge", "gt", "eq", "ne", "le", "lt")


def check_compare_fields(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    left_name = _column(df, column)
    right_name = _column(df, str(params.get("other") or ""))
    op = params.get("op") or "ge"
    if op not in _COMPARE_OPS:
        raise QueryError(f"Unknown comparison '{op}'.")
    mode = params.get("compare_as") or "auto"
    if mode == "auto":
        left_dt, right_dt = df.schema[left_name], df.schema[right_name]
        if left_dt.is_temporal() or right_dt.is_temporal():
            mode = "date"
        elif left_dt.is_numeric() and right_dt.is_numeric():
            mode = "number"
        else:
            mode = "text"
    if mode == "date":
        left, right = _date(df, left_name), _date(df, right_name)
    elif mode == "number":
        left, right = _numeric(df, left_name), _numeric(df, right_name)
    else:
        left = _text(df, left_name).str.strip_chars()
        right = _text(df, right_name).str.strip_chars()
    held = {
        "ge": left >= right,
        "gt": left > right,
        "eq": left == right,
        "ne": left != right,
        "le": left <= right,
        "lt": left < right,
    }[op]
    # A null on either side yields a null comparison → passes (nulls are
    # 'required's business), so ~held stays null and fill_null(False) applies.
    return ~held


def _conditional_trigger(df: pl.DataFrame, params: dict) -> pl.Expr:
    when_column = _column(df, str(params.get("when_column") or ""))
    raw_value = params.get("when_value")
    if raw_value in (None, ""):
        raise QueryError("Conditional required needs a trigger value.")
    op = str(params.get("when_op") or "eq")
    if op not in _COMPARE_OPS:
        raise QueryError(f"Unknown conditional comparison '{op}'.")
    mode = str(params.get("compare_as") or "auto")
    dtype = df.schema[when_column]
    if mode == "auto":
        mode = "number" if dtype.is_numeric() else "date" if dtype.is_temporal() else "text"
    if mode == "number":
        try:
            expected = float(raw_value)
        except (TypeError, ValueError) as error:
            raise QueryError("Conditional trigger value must be a number.") from error
        left = _numeric(df, when_column)
    elif mode == "date":
        expected = _parse_date(raw_value, "Conditional trigger value")
        if expected is None:
            raise QueryError("Conditional required needs a trigger date.")
        left = _date(df, when_column)
    elif mode == "text":
        expected = str(raw_value).strip()
        left = _text(df, when_column).str.strip_chars()
    else:
        raise QueryError(f"Unknown conditional comparison mode '{mode}'.")
    return {
        "ge": left >= expected, "gt": left > expected, "eq": left == expected,
        "ne": left != expected, "le": left <= expected, "lt": left < expected,
    }[op].fill_null(False)


def check_conditional_required(df: pl.DataFrame, column: str, params: dict) -> pl.Expr:
    target = _column(df, column)
    triggered = _conditional_trigger(df, params)
    blank = pl.col(target).is_null()
    if df.schema[target] == pl.String:
        blank = blank | (pl.col(target).str.strip_chars() == "")
    return triggered & blank


def check_expression(df: pl.DataFrame, column: str | None, params: dict) -> pl.Expr:
    # The escape hatch, matching the Analysis tab's custom-code trust model
    # (this is a local-first tool that already runs user Python via the
    # sandbox). The expression must be a Polars expression that is True for
    # VIOLATING rows, e.g.: (pl.col("qty") * pl.col("price") - pl.col("total")).abs() > 0.01
    code = str(params.get("code") or "").strip()
    if not code:
        raise QueryError("Expression needs Polars code.")
    try:
        expr = eval(code, {"__builtins__": {}}, {"pl": pl, "col": pl.col})  # noqa: S307
    except Exception as error:
        raise QueryError(f"Expression error: {error}") from error
    if not isinstance(expr, pl.Expr):
        raise QueryError("The code must evaluate to a Polars expression (pl.Expr).")
    return expr


# -------------------------------------------------------- table-scope checks
def check_unique_key(df: pl.DataFrame, column: str | None, params: dict) -> pl.Expr:
    columns = resolve_columns(
        params.get("columns"), df.columns, error_type=QueryError
    )
    if not columns:
        raise QueryError("Unique key needs at least one column.")
    return pl.struct(columns).is_duplicated()


def check_row_count(df: pl.DataFrame, column: str | None, params: dict) -> pl.Expr:
    low, high = _number(params, "min"), _number(params, "max")
    if low is None and high is None:
        raise QueryError("Row count needs a min and/or a max.")
    violated = (low is not None and df.height < low) or (
        high is not None and df.height > high
    )
    # A whole-table property: every row "fails" or none do, so the verdict
    # reads as a single condition rather than a per-row count.
    return pl.lit(bool(violated))


# ---------------------------------------------------------------- registry
CHECKS: dict[str, dict] = {
    "required": {
        "label": "Required",
        "icon": "pi pi-asterisk",
        "scope": "column",
        "column_kinds": ["numeric", "date", "boolean", "text"],
        "description": "Value must be present — nulls and blank/whitespace-only text fail.",
        "params": [],
        "func": check_required,
    },
    "numeric_sign": {
        "label": "Sign",
        "icon": "pi pi-sort-numeric-up",
        "scope": "column",
        "column_kinds": ["numeric"],
        "description": "Value must have the expected sign (positive, non-negative, nonzero, or negative).",
        "params": [
            {
                "name": "mode",
                "kind": "select",
                "label": "Values must be",
                "options": [
                    {"label": "Positive (> 0)", "value": "positive"},
                    {"label": "Non-negative (≥ 0)", "value": "non_negative"},
                    {"label": "Nonzero (≠ 0)", "value": "nonzero"},
                    {"label": "Negative (< 0)", "value": "negative"},
                ],
                "default": "positive",
            },
        ],
        "func": check_numeric_sign,
    },
    "range": {
        "label": "Range",
        "icon": "pi pi-arrows-h",
        "scope": "column",
        "column_kinds": ["numeric"],
        "description": "Value must fall inside a min/max range (either bound optional).",
        "params": [
            {"name": "min", "kind": "number", "label": "Min", "optional": True},
            {"name": "max", "kind": "number", "label": "Max", "optional": True},
        ],
        "func": check_range,
    },
    "allowed_values": {
        "label": "Allowed values",
        "icon": "pi pi-list-check",
        "scope": "column",
        "column_kinds": ["numeric", "boolean", "text"],
        "description": "Value must be one of an allowed list — pick from the column's current values or type your own.",
        "params": [
            {"name": "values", "kind": "values", "label": "Allowed values"},
            {
                "name": "ignore_case",
                "kind": "toggle",
                "label": "Ignore case",
                "default": False,
            },
        ],
        "func": check_allowed_values,
    },
    "pattern": {
        "label": "Pattern",
        "icon": "pi pi-code",
        "scope": "column",
        "column_kinds": ["text"],
        "description": "Text must match a regular expression (whole value, or anywhere within it).",
        "params": [
            {"name": "regex", "kind": "text", "label": "Regular expression"},
            {
                "name": "mode",
                "kind": "select",
                "label": "Match",
                "options": [
                    {"label": "Whole value", "value": "full"},
                    {"label": "Anywhere in value", "value": "contains"},
                ],
                "default": "full",
            },
        ],
        "func": check_pattern,
    },
    "length": {
        "label": "Length",
        "icon": "pi pi-arrows-v",
        "scope": "column",
        "column_kinds": ["text"],
        "description": "Text length (characters) must fall inside a min/max range.",
        "params": [
            {"name": "min", "kind": "number", "label": "Min length", "optional": True},
            {"name": "max", "kind": "number", "label": "Max length", "optional": True},
        ],
        "func": check_length,
    },
    "date_range": {
        "label": "Date range",
        "icon": "pi pi-calendar",
        "scope": "column",
        "column_kinds": ["date", "text"],
        "description": "Date must fall inside a range and/or not lie in the future. Text columns are parsed as dates.",
        "params": [
            {"name": "min", "kind": "date", "label": "Earliest", "optional": True},
            {"name": "max", "kind": "date", "label": "Latest", "optional": True},
            {
                "name": "not_in_future",
                "kind": "toggle",
                "label": "Not in the future",
                "default": False,
            },
        ],
        "func": check_date_range,
    },
    "unique": {
        "label": "Unique",
        "icon": "pi pi-fingerprint",
        "scope": "column",
        "column_kinds": ["numeric", "date", "boolean", "text"],
        "description": "No value may occur more than once (nulls are ignored).",
        "params": [],
        "func": check_unique,
    },
    "referential": {
        "label": "Exists in table",
        "icon": "pi pi-link",
        "scope": "column",
        "column_kinds": ["numeric", "text"],
        "description": "Value must exist in another table's column — e.g. every cost center appears in the cost-center master.",
        "params": [
            {"name": "lookup_table", "kind": "table", "label": "Lookup table"},
            {"name": "lookup_column", "kind": "lookup_column", "label": "Lookup column"},
        ],
        "needs_lookup": True,
        "func": check_referential,
    },
    "compare_fields": {
        "label": "Compare fields",
        "icon": "pi pi-arrow-right-arrow-left",
        "scope": "column",
        "column_kinds": ["numeric", "date", "text"],
        "description": "This field must relate to another field in the same row — e.g. end_date ≥ start_date.",
        "params": [
            {
                "name": "op",
                "kind": "select",
                "label": "Must be",
                "options": [
                    {"label": "≥ (at least)", "value": "ge"},
                    {"label": "> (greater than)", "value": "gt"},
                    {"label": "= (equal to)", "value": "eq"},
                    {"label": "≠ (different from)", "value": "ne"},
                    {"label": "≤ (at most)", "value": "le"},
                    {"label": "< (less than)", "value": "lt"},
                ],
                "default": "ge",
            },
            {"name": "other", "kind": "column", "label": "Other field"},
            {
                "name": "compare_as",
                "kind": "select",
                "label": "Compare as",
                "options": [
                    {"label": "Auto (from column types)", "value": "auto"},
                    {"label": "Numbers", "value": "number"},
                    {"label": "Dates", "value": "date"},
                    {"label": "Text", "value": "text"},
                ],
                "default": "auto",
            },
        ],
        "func": check_compare_fields,
    },
    "conditional_required": {
        "label": "Required when",
        "icon": "pi pi-question-circle",
        "scope": "column",
        "column_kinds": ["numeric", "date", "boolean", "text"],
        "description": "Value must be present whenever another field meets a typed comparison — e.g. approval_ref required when amount > 50000.",
        "params": [
            {"name": "when_column", "kind": "column", "label": "When this field"},
            {
                "name": "when_op", "kind": "select", "label": "Comparison",
                "options": [
                    {"label": "= (equal to)", "value": "eq"},
                    {"label": "≠ (different from)", "value": "ne"},
                    {"label": "> (greater than)", "value": "gt"},
                    {"label": "≥ (at least)", "value": "ge"},
                    {"label": "< (less than)", "value": "lt"},
                    {"label": "≤ (at most)", "value": "le"},
                ],
                "default": "eq",
            },
            {"name": "when_value", "kind": "text", "label": "Trigger value"},
            {
                "name": "compare_as", "kind": "select", "label": "Compare as",
                "options": [
                    {"label": "Auto (from field type)", "value": "auto"},
                    {"label": "Numbers", "value": "number"},
                    {"label": "Dates", "value": "date"},
                    {"label": "Text", "value": "text"},
                ],
                "default": "auto",
            },
        ],
        "func": check_conditional_required,
    },
    "expression": {
        "label": "Custom expression",
        "icon": "pi pi-code",
        "scope": "table",
        "column_kinds": [],
        "description": "A Polars expression that is True for violating rows — the escape hatch for anything the other checks can't say.",
        "params": [
            {"name": "name", "kind": "text", "label": "Rule name", "optional": True},
            {"name": "code", "kind": "code", "label": "Violation expression"},
        ],
        "func": check_expression,
    },
    "unique_key": {
        "label": "Unique key",
        "icon": "pi pi-key",
        "scope": "table",
        "column_kinds": [],
        "description": "No two rows may share the same combination of key columns.",
        "params": [{"name": "columns", "kind": "columns", "label": "Key columns"}],
        "func": check_unique_key,
    },
    "row_count": {
        "label": "Row count",
        "icon": "pi pi-bars",
        "scope": "table",
        "column_kinds": [],
        "description": "The table must have between min and max rows — a sanity check on refreshed data.",
        "params": [
            {"name": "min", "kind": "number", "label": "Min rows", "optional": True},
            {"name": "max", "kind": "number", "label": "Max rows", "optional": True},
        ],
        "func": check_row_count,
    },
}


CHECK_ALIASES = {
    # Models commonly borrow these spellings from dataframe APIs. In the
    # validation registry the equivalent presence check is named `required`.
    "not_null": "required",
    "is_not_null": "required",
}


def canonical_check_id(value: object) -> str:
    """Return the registered id for a validation check or a known alias."""
    check = str(value or "").strip()
    return CHECK_ALIASES.get(check, check)


def canonicalize_rules(
    df: pl.DataFrame,
    rules: list[dict],
    *,
    resolve: Resolve | None = None,
    strict: bool = True,
) -> list[dict]:
    """Copy rules while resolving declarative field references safely."""
    normalized = []
    for source in rules or []:
        rule = {**source, "params": dict(source.get("params") or {})}
        rule["check"] = canonical_check_id(rule.get("check"))
        meta = CHECKS.get(rule["check"])
        if meta is None:
            if strict:
                raise QueryError(f"Unknown check '{rule.get('check')}'.")
            normalized.append(rule)
            continue

        if meta.get("scope") == "column" and rule.get("column"):
            match = matching_column(rule["column"], df.columns)
            if match is not None:
                rule["column"] = match
            elif strict:
                rule["column"] = resolve_column(
                    rule["column"], df.columns, error_type=QueryError
                )

        for parameter in meta.get("params") or []:
            name = parameter.get("name")
            value = rule["params"].get(name)
            if value in (None, "", []):
                continue
            kind = parameter.get("kind")
            if kind == "column":
                match = matching_column(value, df.columns)
                if match is not None:
                    rule["params"][name] = match
                elif strict:
                    rule["params"][name] = resolve_column(
                        value, df.columns, error_type=QueryError
                    )
            elif kind == "columns":
                values = [value] if isinstance(value, str) else list(value or [])
                matches = [matching_column(item, df.columns) for item in values]
                if all(match is not None for match in matches):
                    rule["params"][name] = matches
                elif strict:
                    rule["params"][name] = resolve_columns(
                        values, df.columns, error_type=QueryError
                    )
            elif kind == "lookup_column" and resolve is not None:
                table = str(rule["params"].get("lookup_table") or "").strip()
                if not table:
                    continue
                try:
                    lookup = resolve(table)
                except Exception:
                    if strict:
                        raise
                    continue
                match = matching_column(value, lookup.columns)
                if match is not None:
                    rule["params"][name] = match
                elif strict:
                    rule["params"][name] = resolve_column(
                        value, lookup.columns, table=table, error_type=QueryError
                    )
        normalized.append(rule)
    return normalized


def registry_payload() -> list[dict]:
    return [
        {key: value for key, value in {**meta, "id": check_id}.items() if key != "func"}
        for check_id, meta in CHECKS.items()
    ]


# ---------------------------------------------------------------- labelling
def _fmt(value: float) -> str:
    return f"{value:,.4g}"


def rule_label(rule: dict) -> str:
    """Short human summary of a rule, used in results and Excel evidence."""
    check = rule.get("check")
    params = rule.get("params") or {}
    meta = CHECKS.get(check)
    if meta is None:
        return str(check)
    label = meta["label"]
    if check == "numeric_sign":
        return f"{label}: {str(params.get('mode') or 'positive').replace('_', '-')}"
    if check in ("range", "length", "row_count"):
        low, high = params.get("min"), params.get("max")
        parts = []
        if low not in (None, ""):
            parts.append(f"≥ {_fmt(float(low))}")
        if high not in (None, ""):
            parts.append(f"≤ {_fmt(float(high))}")
        return f"{label} {' and '.join(parts)}" if parts else label
    if check == "allowed_values":
        values = params.get("values") or []
        return f"In list ({len(values)} value{'s' if len(values) != 1 else ''})"
    if check == "pattern":
        return f"{label} /{params.get('regex') or ''}/"
    if check == "date_range":
        parts = []
        if params.get("min"):
            parts.append(f"≥ {params['min']}")
        if params.get("max"):
            parts.append(f"≤ {params['max']}")
        if params.get("not_in_future"):
            parts.append("not in future")
        return f"{label}: {', '.join(parts)}" if parts else label
    if check == "unique_key":
        return f"{label}: {' + '.join(params.get('columns') or [])}"
    if check == "referential":
        return f"In {params.get('lookup_table') or '?'}.{params.get('lookup_column') or '?'}"
    if check == "compare_fields":
        symbol = {"ge": "≥", "gt": ">", "eq": "=", "ne": "≠", "le": "≤", "lt": "<"}.get(
            params.get("op") or "ge", "≥"
        )
        return f"{symbol} {params.get('other') or '?'}"
    if check == "conditional_required":
        symbol = {"ge": "≥", "gt": ">", "eq": "=", "ne": "≠", "le": "≤", "lt": "<"}.get(
            params.get("when_op") or "eq", "="
        )
        return f"Required when {params.get('when_column') or '?'} {symbol} {params.get('when_value') or '?'}"
    if check == "expression":
        return str(params.get("name") or "").strip() or label
    return label


# ------------------------------------------------------------------ running
def _violations(df: pl.DataFrame, rule: dict, resolve: Resolve | None = None) -> pl.Expr:
    meta = CHECKS.get(rule.get("check"))
    if meta is None:
        raise QueryError(f"Unknown check '{rule.get('check')}'.")
    if meta.get("needs_lookup"):
        expr = meta["func"](df, rule.get("column"), rule.get("params") or {}, resolve)
    else:
        expr = meta["func"](df, rule.get("column"), rule.get("params") or {})
    # Comparisons against null yield null; a null verdict is "no violation".
    return expr.fill_null(False)


def rule_failures(
    df: pl.DataFrame, rule: dict, resolve: Resolve | None = None
) -> pl.DataFrame:
    """All rows violating one rule (full frame, for drill-in and export)."""
    return df.filter(_violations(df, rule, resolve))


def generated_rule_issues(
    df: pl.DataFrame, rules: list[dict], resolve: Resolve | None = None
) -> list[str]:
    """Semantic preflight for model-generated rules before auto-persistence."""
    issues = []
    for index, rule in enumerate(rules, start=1):
        try:
            df.select(_violations(df, rule, resolve).sum())
            if rule.get("check") == "conditional_required":
                trigger_count = int(
                    df.select(_conditional_trigger(df, rule.get("params") or {}).sum()).item() or 0
                )
                if df.height and trigger_count == 0:
                    issues.append(f"rule {index} conditional trigger matches zero rows")
            elif rule.get("check") == "allowed_values":
                column = _column(df, rule.get("column"))
                params = rule.get("params") or {}
                ignore_case = bool(params.get("ignore_case"))
                allowed = {
                    str(value).strip().casefold() if ignore_case else str(value).strip()
                    for value in params.get("values") or []
                    if str(value).strip()
                }
                observed = {
                    str(value).strip().casefold() if ignore_case else str(value).strip()
                    for value in df[column].drop_nulls().unique().head(1000).to_list()
                    if str(value).strip()
                }
                if observed and allowed and observed.isdisjoint(allowed):
                    issues.append(f"rule {index} allowed values have no overlap with observed values")
        except Exception as error:
            issues.append(f"rule {index} is invalid: {error}")
    return issues


def run_rules(
    df: pl.DataFrame, rules: list[dict], table: str, resolve: Resolve | None = None
) -> dict:
    results = []
    counts = {"passed": 0, "warned": 0, "failed": 0, "errored": 0, "skipped": 0}

    for rule in rules:
        severity = rule.get("severity") if rule.get("severity") in SEVERITIES else "fail"
        result = {
            "rule_id": rule.get("id"),
            "column": rule.get("column"),
            "check": rule.get("check"),
            "label": rule_label(rule),
            "severity": severity,
            "verdict": "skipped",
            "fail_count": 0,
            "checked_rows": df.height,
            "pass_pct": None,
            "error": None,
        }
        if rule.get("enabled") is False:
            counts["skipped"] += 1
            results.append(result)
            continue
        try:
            fail_count = int(df.select(_violations(df, rule, resolve).sum()).item() or 0)
            result["fail_count"] = fail_count
            # Row count is a whole-table condition — a pass percentage or a
            # "failing rows" figure would be noise next to its verdict.
            if df.height and rule.get("check") != "row_count":
                result["pass_pct"] = round((df.height - fail_count) / df.height * 100, 2)
            if fail_count == 0:
                result["verdict"] = "ok"
                counts["passed"] += 1
            else:
                result["verdict"] = severity
                counts["warned" if severity == "warn" else "failed"] += 1
        except Exception as error:  # degrade to an error row, never kill the run
            result["verdict"] = "error"
            result["error"] = str(error)
            counts["errored"] += 1
        results.append(result)

    if counts["failed"] or counts["errored"]:
        verdict = "fail"
    elif counts["warned"]:
        verdict = "warn"
    elif counts["passed"]:
        verdict = "ok"
    else:
        verdict = "info"

    return {
        "table": table,
        "rows": df.height,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "counts": counts,
        "results": results,
    }


def detail_payload(df: pl.DataFrame, rule: dict, resolve: Resolve | None = None) -> dict:
    failures = rule_failures(df, rule, resolve)
    return {
        "label": rule_label(rule),
        "detail": frame_payload(failures, DETAIL_PREVIEW_ROWS),
        "detail_rows": failures.height,
    }


def summary_frame(run: dict) -> pl.DataFrame:
    """One row per rule — the core of the evidence sheet and the tile table."""
    return pl.DataFrame(
        {
            "field": [r["column"] or "(table)" for r in run["results"]],
            "rule": [r["label"] for r in run["results"]],
            "severity": [r["severity"] for r in run["results"]],
            "verdict": [r["verdict"] for r in run["results"]],
            "rows_checked": [r["checked_rows"] for r in run["results"]],
            "failing_rows": [r["fail_count"] for r in run["results"]],
            "pass_pct": [r["pass_pct"] for r in run["results"]],
            "error": [r["error"] for r in run["results"]],
        }
    )


def report_frame(run: dict) -> pl.DataFrame:
    """The summary frame with the run context prepended, for Excel evidence."""
    core = summary_frame(run)
    return core.with_columns(
        pl.Series("table", [run["table"]] * core.height),
        pl.Series("run_at", [run["run_at"]] * core.height),
    ).select(["table", "run_at", *core.columns])
