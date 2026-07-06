"""Canned audit analytics tests, Polars-only.

Each test is a function ``(df, params) -> AnalyticsResult`` registered in
``ANALYTICS`` with parameter metadata that drives the SPA's dynamic forms
(same idea as the validation platform's RULE_TYPES).

Results carry a headline verdict, stat chips, an aggregated summary frame
(rendered as a table) and optionally a row-level detail frame (previewed in
the UI, exported to Excel in full).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import polars as pl

from .explore import QueryError, frame_payload

SUMMARY_MAX_ROWS = 500
DETAIL_PREVIEW_ROWS = 50

PERIODS = {
    "day": "1d",
    "week": "1w",
    "month": "1mo",
    "quarter": "1q",
    "year": "1y",
}


@dataclass
class AnalyticsResult:
    title: str
    verdict: str = "info"  # ok | warn | fail | info
    verdict_text: str = ""
    stats: list = field(default_factory=list)  # [{"label", "value"}]
    summary: pl.DataFrame | None = None
    detail: pl.DataFrame | None = None
    # Suggested visualization of the summary frame for dashboard tiles,
    # e.g. {"type": "bar", "x": "digit", "y": ["observed_pct", "expected_pct"]}.
    viz: dict | None = None

    def payload(self) -> dict:
        return {
            "title": self.title,
            "verdict": self.verdict,
            "verdict_text": self.verdict_text,
            "stats": self.stats,
            "viz": self.viz,
            "summary": frame_payload(self.summary, SUMMARY_MAX_ROWS)
            if self.summary is not None
            else None,
            "summary_rows": self.summary.height if self.summary is not None else 0,
            "detail": frame_payload(self.detail, DETAIL_PREVIEW_ROWS)
            if self.detail is not None
            else None,
            "detail_rows": self.detail.height if self.detail is not None else 0,
        }

    def export_frame(self) -> pl.DataFrame | None:
        return self.detail if self.detail is not None else self.summary


def _stat(label: str, value) -> dict:
    if isinstance(value, float):
        value = f"{value:,.2f}".rstrip("0").rstrip(".")
    elif isinstance(value, int):
        value = f"{value:,}"
    return {"label": label, "value": str(value)}


def _numeric_series(df: pl.DataFrame, column: str, alias: str = "value") -> pl.DataFrame:
    if column not in df.columns:
        raise QueryError(f"Unknown column '{column}'.")
    frame = df.select(
        pl.col(column).cast(pl.Float64, strict=False).alias(alias)
    ).drop_nulls()
    if frame.is_empty():
        raise QueryError(f"'{column}' has no numeric values.")
    return frame


def _date_expr(df: pl.DataFrame, column: str) -> pl.Expr:
    if column not in df.columns:
        raise QueryError(f"Unknown column '{column}'.")
    dtype = df.schema[column]
    if dtype == pl.Date:
        return pl.col(column)
    if dtype.is_temporal():
        return pl.col(column).cast(pl.Date)
    return pl.col(column).cast(pl.String).str.to_date(strict=False)


# ---------------------------------------------------------------- Benford
# Nigrini's Mean Absolute Deviation conformity bands.
_MAD_BANDS = {
    1: (0.006, 0.012, 0.015),
    2: (0.0012, 0.0018, 0.0022),
}


def benford(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    digits = int(params.get("digits") or 1)
    if digits not in (1, 2):
        raise QueryError("Digits must be 1 or 2.")

    values = _numeric_series(df, column).select(pl.col("value").abs()).filter(
        pl.col("value") > 0
    )
    n = values.height
    if n < 100:
        raise QueryError(
            f"Only {n} usable values in '{column}' — Benford needs at least 100."
        )

    # First significant digits: floor(v / 10^(floor(log10 v) - digits + 1)).
    lead = values.select(
        (
            pl.col("value")
            / pl.lit(10.0).pow(pl.col("value").log10().floor() - (digits - 1))
        )
        .floor()
        .cast(pl.Int64)
        .alias("lead")
    )

    start, end = (1, 9) if digits == 1 else (10, 99)
    expected = pl.DataFrame(
        {
            "lead": list(range(start, end + 1)),
            "expected_pct": [
                100.0 * math.log10(1 + 1 / d) for d in range(start, end + 1)
            ],
        }
    )

    observed = lead.group_by("lead").agg(pl.len().alias("count"))
    table = (
        expected.join(observed, on="lead", how="left")
        .with_columns(pl.col("count").fill_null(0))
        .with_columns((100.0 * pl.col("count") / n).alias("observed_pct"))
        .with_columns(
            (pl.col("observed_pct") - pl.col("expected_pct")).alias("deviation_pct")
        )
        .sort("lead")
        .select(
            pl.col("lead").alias("digit" if digits == 1 else "digits"),
            "count",
            pl.col("observed_pct").round(2),
            pl.col("expected_pct").round(2),
            pl.col("deviation_pct").round(2),
        )
    )

    obs_p = (table["observed_pct"] / 100.0).to_list()
    exp_p = (table["expected_pct"] / 100.0).to_list()
    chi_square = n * sum((o - e) ** 2 / e for o, e in zip(obs_p, exp_p))
    mad = sum(abs(o - e) for o, e in zip(obs_p, exp_p)) / len(exp_p)

    close, acceptable, marginal = _MAD_BANDS[digits]
    if mad <= acceptable:
        verdict, text = "ok", "Close conformity" if mad <= close else "Acceptable conformity"
    elif mad <= marginal:
        verdict, text = "warn", "Marginally acceptable conformity"
    else:
        verdict, text = "fail", "Nonconformity — distribution deviates from Benford's law"

    worst = table.sort("deviation_pct", descending=True).row(0, named=True)
    return AnalyticsResult(
        title=f"Benford's law — first {'digit' if digits == 1 else 'two digits'} of {column}",
        verdict=verdict,
        verdict_text=text,
        stats=[
            _stat("Values tested", n),
            _stat("MAD", round(mad, 5)),
            _stat("Chi-square", round(chi_square, 1)),
            _stat(
                "Most over-represented",
                f"{worst['digit' if digits == 1 else 'digits']} (+{worst['deviation_pct']}%)",
            ),
        ],
        summary=table,
        viz={
            "type": "bar",
            "x": "digit" if digits == 1 else "digits",
            "y": ["observed_pct", "expected_pct"],
        },
    )


# ------------------------------------------------------------- duplicates
def duplicates(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    columns = [c for c in (params.get("columns") or []) if c]
    if not columns:
        raise QueryError("Pick at least one key column.")
    for column in columns:
        if column not in df.columns:
            raise QueryError(f"Unknown column '{column}'.")

    counts = (
        df.group_by(columns)
        .agg(pl.len().alias("occurrences"))
        .filter(pl.col("occurrences") > 1)
        .sort("occurrences", descending=True)
    )
    dup_rows = int(counts["occurrences"].sum() or 0)
    detail = (
        df.join(counts, on=columns, how="semi")
        .join(counts.select(columns + ["occurrences"]), on=columns, how="left")
        .sort(columns)
        if counts.height
        else None
    )

    verdict = "fail" if counts.height else "ok"
    return AnalyticsResult(
        title=f"Duplicates on {', '.join(columns)}",
        verdict=verdict,
        verdict_text=(
            f"{counts.height:,} duplicated key(s) covering {dup_rows:,} rows"
            if counts.height
            else "No duplicate keys found"
        ),
        stats=[
            _stat("Rows tested", df.height),
            _stat("Duplicated keys", counts.height),
            _stat("Rows in duplicate groups", dup_rows),
        ],
        summary=counts,
        detail=detail,
    )


# ------------------------------------------------------------------- gaps
def gaps(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    if column not in df.columns:
        raise QueryError(f"Unknown column '{column}'.")

    numbers = df.select(
        pl.col(column).cast(pl.String).str.replace_all(r"[^\d]", "").cast(
            pl.Int64, strict=False
        ).alias("value")
    ).drop_nulls()
    if numbers.is_empty():
        raise QueryError(f"'{column}' contains no sequence numbers.")

    total = numbers.height
    distinct = numbers["value"].n_unique()
    ordered = numbers.unique().sort("value")
    gap_rows = (
        ordered.with_columns(pl.col("value").shift(1).alias("previous"))
        .drop_nulls()
        .filter(pl.col("value") - pl.col("previous") > 1)
        .select(
            (pl.col("previous") + 1).alias("gap_from"),
            (pl.col("value") - 1).alias("gap_to"),
            (pl.col("value") - pl.col("previous") - 1).alias("missing_count"),
        )
        .sort("gap_from")
    )
    missing_total = int(gap_rows["missing_count"].sum() or 0)
    low, high = int(ordered["value"].min()), int(ordered["value"].max())

    verdict = "ok" if gap_rows.is_empty() else "warn"
    return AnalyticsResult(
        title=f"Sequence gaps in {column}",
        verdict=verdict,
        verdict_text=(
            "Sequence is complete"
            if gap_rows.is_empty()
            else f"{gap_rows.height:,} gap(s), {missing_total:,} missing number(s)"
        ),
        stats=[
            _stat("Range", f"{low:,} – {high:,}"),
            _stat("Distinct numbers", distinct),
            _stat("Missing numbers", missing_total),
            _stat("Reused numbers", total - distinct),
        ],
        summary=gap_rows,
    )


# --------------------------------------------------------------- sampling
def sampling(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    method = params.get("method") or "random"
    size = int(params.get("size") or 25)
    seed = int(params.get("seed") or 42)
    if size < 1:
        raise QueryError("Sample size must be at least 1.")
    size = min(size, df.height)

    indexed = df.with_row_index("source_row", offset=2)  # matches Excel row numbers

    if method == "random":
        sample = indexed.sample(size, seed=seed)
        summary = None
    elif method == "interval":
        step = max(df.height // size, 1)
        start = seed % step
        sample = indexed.gather_every(step, offset=start).head(size)
        summary = None
    elif method == "stratified":
        stratify_by = params.get("stratify_by")
        if not stratify_by or stratify_by not in df.columns:
            raise QueryError("Stratified sampling needs a valid stratify column.")
        strata = indexed.group_by(stratify_by).agg(pl.len().alias("population"))
        # Proportional allocation, at least 1 per stratum.
        strata = strata.with_columns(
            pl.max_horizontal(
                (pl.col("population") * size / df.height).round(0).cast(pl.Int64),
                pl.lit(1),
            ).alias("allocated")
        )
        parts = []
        for row in strata.rows(named=True):
            group = indexed.filter(pl.col(stratify_by) == row[stratify_by])
            parts.append(group.sample(min(row["allocated"], group.height), seed=seed))
        sample = pl.concat(parts)
        summary = strata.sort("population", descending=True)
    else:
        raise QueryError(f"Unknown sampling method '{method}'.")

    sample = sample.sort("source_row")
    return AnalyticsResult(
        title=f"{method.title()} sample ({sample.height} of {df.height:,} rows)",
        verdict="info",
        verdict_text=f"{sample.height:,} rows selected (seed {seed})",
        stats=[
            _stat("Population", df.height),
            _stat("Sample size", sample.height),
            _stat("Coverage", f"{100.0 * sample.height / df.height:.2f}%"),
        ],
        summary=summary,
        detail=sample,
    )


# --------------------------------------------------------- period compare
def period_compare(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    date_column = params.get("date_column")
    value_column = params.get("value_column") or None
    period = params.get("period") or "month"
    if period not in PERIODS:
        raise QueryError(f"Unknown period '{period}'.")

    frame = df.select(_date_expr(df, date_column).alias("date")).drop_nulls()
    if frame.is_empty():
        raise QueryError(f"No parseable dates in '{date_column}'.")
    if value_column:
        if value_column not in df.columns:
            raise QueryError(f"Unknown column '{value_column}'.")
        frame = df.select(
            _date_expr(df, date_column).alias("date"),
            pl.col(value_column).cast(pl.Float64, strict=False).alias("value"),
        ).drop_nulls(subset=["date"])

    aggregations = [pl.len().alias("transactions")]
    if value_column:
        aggregations.append(pl.col("value").sum().round(2).alias(f"{value_column}_sum"))
    measure = f"{value_column}_sum" if value_column else "transactions"

    table = (
        frame.with_columns(pl.col("date").dt.truncate(PERIODS[period]).alias("period"))
        .group_by("period")
        .agg(aggregations)
        .sort("period")
        .with_columns(
            (
                100.0
                * (pl.col(measure) - pl.col(measure).shift(1))
                / pl.col(measure).shift(1).abs()
            )
            .round(2)
            .alias("change_pct")
        )
    )

    swings = table.drop_nulls(subset=["change_pct"])
    stats = [
        _stat("Periods", table.height),
        _stat(
            "Date range",
            f"{frame['date'].min().isoformat()} – {frame['date'].max().isoformat()}",
        ),
    ]
    verdict, text = "info", f"{table.height} {period}(s) compared on {measure}"
    if swings.height:
        biggest = swings.sort(pl.col("change_pct").abs(), descending=True).row(0, named=True)
        stats.append(
            _stat(
                "Largest swing",
                f"{biggest['period']} ({biggest['change_pct']:+.1f}%)",
            )
        )
        if abs(biggest["change_pct"]) >= 50:
            verdict, text = "warn", (
                f"Largest {period}-on-{period} swing is {biggest['change_pct']:+.1f}% "
                f"({biggest['period']})"
            )

    return AnalyticsResult(
        title=f"{period.title()}ly comparison of {measure}",
        verdict=verdict,
        verdict_text=text,
        stats=stats,
        summary=table,
        viz={"type": "line", "x": "period", "y": [measure]},
    )


# ------------------------------------------------------------ round numbers
def round_numbers(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    values = _numeric_series(df, column)
    n = values.height

    tiers = []
    for base in (10, 100, 1000, 10000):
        count = values.filter(
            (pl.col("value") != 0) & (pl.col("value") % base == 0)
        ).height
        tiers.append(
            {
                "multiple_of": base,
                "count": count,
                "pct": round(100.0 * count / n, 2),
                # Under a uniform last-digits assumption ~1/base of values hit.
                "expected_pct": round(100.0 / base, 2),
            }
        )
    summary = pl.DataFrame(tiers)

    thousand = tiers[2]
    detail = df.filter(
        (pl.col(column).cast(pl.Float64, strict=False) != 0)
        & (pl.col(column).cast(pl.Float64, strict=False) % 1000 == 0)
    )
    excessive = thousand["pct"] > 10 * thousand["expected_pct"] and thousand["count"] >= 10
    return AnalyticsResult(
        title=f"Round-number analysis of {column}",
        verdict="warn" if excessive else "ok",
        verdict_text=(
            f"{thousand['pct']}% of values are multiples of 1,000 "
            f"(expected ≈{thousand['expected_pct']}%)"
            if excessive
            else "Round-number frequency looks unremarkable"
        ),
        stats=[
            _stat("Values tested", n),
            _stat("Multiples of 100", f"{tiers[1]['count']:,} ({tiers[1]['pct']}%)"),
            _stat("Multiples of 1,000", f"{thousand['count']:,} ({thousand['pct']}%)"),
        ],
        summary=summary,
        detail=detail if detail.height else None,
        viz={"type": "bar", "x": "multiple_of", "y": ["pct", "expected_pct"]},
    )


# ---------------------------------------------------------------- registry
ANALYTICS: dict[str, dict] = {
    "benford": {
        "label": "Benford's Law",
        "icon": "pi pi-chart-bar",
        "description": (
            "Compares leading-digit frequencies of an amount column against "
            "Benford's expected distribution. Deviations can indicate fabricated "
            "or manipulated figures."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Amount column", "column_kind": "numeric"},
            {
                "name": "digits",
                "kind": "select",
                "label": "Digits",
                "options": [
                    {"label": "First digit", "value": 1},
                    {"label": "First two digits", "value": 2},
                ],
                "default": 1,
            },
        ],
        "func": benford,
    },
    "duplicates": {
        "label": "Duplicate Detection",
        "icon": "pi pi-clone",
        "description": (
            "Finds rows sharing the same key values — duplicate invoices, "
            "double payments, reused references."
        ),
        "params": [
            {"name": "columns", "kind": "columns", "label": "Key columns"},
        ],
        "func": duplicates,
    },
    "gaps": {
        "label": "Sequence Gaps",
        "icon": "pi pi-sort-numeric-up",
        "description": (
            "Checks a document/invoice number sequence for missing and reused "
            "numbers. Non-digit characters are ignored."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Sequence column"},
        ],
        "func": gaps,
    },
    "sampling": {
        "label": "Sampling",
        "icon": "pi pi-filter",
        "description": (
            "Draws a documented, reproducible sample: random, fixed-interval, "
            "or stratified by a category column."
        ),
        "params": [
            {
                "name": "method",
                "kind": "select",
                "label": "Method",
                "options": [
                    {"label": "Random", "value": "random"},
                    {"label": "Interval (every k-th)", "value": "interval"},
                    {"label": "Stratified", "value": "stratified"},
                ],
                "default": "random",
            },
            {"name": "size", "kind": "number", "label": "Sample size", "default": 25},
            {
                "name": "stratify_by",
                "kind": "column",
                "label": "Stratify by (stratified only)",
                "optional": True,
            },
            {"name": "seed", "kind": "number", "label": "Seed", "default": 42},
        ],
        "func": sampling,
    },
    "period_compare": {
        "label": "Period Comparison",
        "icon": "pi pi-calendar",
        "description": (
            "Aggregates activity per day/week/month/quarter/year and shows "
            "period-over-period change — spikes and dips stand out."
        ),
        "params": [
            {"name": "date_column", "kind": "column", "label": "Date column", "column_kind": "date"},
            {
                "name": "value_column",
                "kind": "column",
                "label": "Value column (blank = row counts)",
                "column_kind": "numeric",
                "optional": True,
            },
            {
                "name": "period",
                "kind": "select",
                "label": "Period",
                "options": [
                    {"label": "Day", "value": "day"},
                    {"label": "Week", "value": "week"},
                    {"label": "Month", "value": "month"},
                    {"label": "Quarter", "value": "quarter"},
                    {"label": "Year", "value": "year"},
                ],
                "default": "month",
            },
        ],
        "func": period_compare,
    },
    "round_numbers": {
        "label": "Round Numbers",
        "icon": "pi pi-circle",
        "description": (
            "Measures how often amounts are exact multiples of 10/100/1,000. "
            "Excessive round amounts suggest estimates or fabricated entries."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Amount column", "column_kind": "numeric"},
        ],
        "func": round_numbers,
    },
}


def registry_payload() -> list[dict]:
    return [
        {key: value for key, value in {**meta, "id": test_id}.items() if key != "func"}
        for test_id, meta in ANALYTICS.items()
    ]


def run_test(df: pl.DataFrame, test_id: str, params: dict) -> AnalyticsResult:
    meta = ANALYTICS.get(test_id)
    if meta is None:
        raise QueryError(f"Unknown analytics test '{test_id}'.")
    return meta["func"](df, params or {})
