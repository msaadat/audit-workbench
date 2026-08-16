"""Canned audit analytics tests, Polars-only.

Each test is a function ``(df, params) -> AnalyticsResult`` registered in
``ANALYTICS`` with parameter metadata that drives the SPA's dynamic forms
(same idea as the validation platform's RULE_TYPES).

Results carry a headline verdict, stat chips, an aggregated summary frame
(rendered as a table) and optionally a row-level detail frame (previewed in
the UI, exported to Excel in full).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import polars as pl

from .explore import QueryError, frame_payload
from .text import counted, verb
from .field_names import resolve_column, resolve_columns

SUMMARY_MAX_ROWS = 500
DETAIL_PREVIEW_ROWS = 50

# Reading another frame is the validation engine's contract, reused verbatim so
# the two engines cannot disagree about what a lookup is.
Resolve = Callable[[str], pl.DataFrame]


@dataclass(frozen=True)
class FrameSource:
    """The workspace's other frames, for tests that reconcile across them.

    Most analytics are a property of one frame and never see this. A referential
    test is not: its whole question is whether this frame's values exist in
    another one, so it needs both a way to read a named frame and the catalog of
    what it may read. The catalog travels because a resolver alone cannot be
    enumerated, and diagnosing *where* an unmatched key does resolve means
    sweeping the keys the workspace actually holds.
    """

    resolve: Resolve
    tables: tuple[str, ...] = ()
    # The frame being tested, when the caller knows it. Only the orphan
    # diagnosis uses it, and only to exclude the tested column itself: the
    # unmatched values of a column are trivially present in that column, so a
    # frame that does not name itself can be told its keys "resolve in" the very
    # column they came from. The rest of the frame stays in scope, because a
    # reference pointing inside its own table is ordinary in a master file.
    origin: str = ""

    def frame(self, name: str) -> pl.DataFrame:
        try:
            return self.resolve(name)
        except QueryError:
            raise
        except Exception as error:
            raise QueryError(f"Could not load lookup table '{name}': {error}") from error


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
    # How many source rows the test actually evaluated. Every test already
    # computes this to build its own statistics, but only as a formatted
    # label/value pair ("Rows compared", "Values tested", "Dated rows"), which
    # is presentation rather than contract: a reader downstream had to guess
    # which label carried the denominator, and a count of flagged rows without
    # its denominator is not an audit conclusion. Declared here so the
    # execution record can carry it typed.
    tested: int | None = None
    # Suggested visualization of the summary frame for dashboard tiles,
    # e.g. {"type": "bar", "x": "digit", "y": ["observed_pct", "expected_pct"]}.
    viz: dict | None = None

    def payload(self) -> dict:
        return {
            "title": self.title,
            "verdict": self.verdict,
            "verdict_text": self.verdict_text,
            "stats": self.stats,
            "tested": self.tested,
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
        # Two decimals reads well for ordinary magnitudes, but would squash
        # small metrics like a Benford MAD of 0.004 to "0" — fall back to
        # significant digits for those.
        if value != 0 and abs(value) < 0.005:
            value = f"{value:.3g}"
        else:
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
            f"{counted(counts.height, 'duplicated key')} covering {dup_rows:,} rows"
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
        tested=df.height,
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
        tested=df.height,
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
    verdict, text = "info", f"{counted(table.height, period)} compared on {measure}"
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
        tested=frame.height,
        viz={"type": "line", "x": "period", "y": [measure]},
    )


# --------------------------------------------------------------- outliers
def outliers(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    method = (params.get("method") or "zscore").lower()
    values = _numeric_series(df, column)
    n = values.height
    col = pl.col(column).cast(pl.Float64, strict=False)

    if method == "zscore":
        threshold = float(params.get("threshold") or 3.0)
        mean = values["value"].mean()
        std = values["value"].std()
        if not std:
            raise QueryError(f"'{column}' has zero variance — no outliers to detect.")
        lower, upper = mean - threshold * std, mean + threshold * std
        score = ((col - mean) / std).abs()
        flagged = (
            df.with_columns(score.round(2).alias("z_score"))
            .filter(col.is_not_null() & (score > threshold))
            .sort("z_score", descending=True)
        )
        band = f"mean ± {threshold:g}σ"
    elif method == "iqr":
        threshold = float(params.get("threshold") or 1.5)
        q1, q3 = values["value"].quantile(0.25), values["value"].quantile(0.75)
        iqr = q3 - q1
        lower, upper = q1 - threshold * iqr, q3 + threshold * iqr
        distance = pl.max_horizontal(pl.lit(lower) - col, col - pl.lit(upper))
        flagged = (
            df.with_columns(distance.round(2).alias("iqr_distance"))
            .filter(col.is_not_null() & ((col < lower) | (col > upper)))
            .sort("iqr_distance", descending=True)
        )
        band = f"{threshold:g}×IQR fence"
    else:
        raise QueryError(f"Unknown outlier method '{method}'.")

    summary = pl.DataFrame(
        {
            "method": [method],
            "lower_bound": [round(lower, 2)],
            "upper_bound": [round(upper, 2)],
            "outliers": [flagged.height],
            "pct": [round(100.0 * flagged.height / n, 2)],
        }
    )
    return AnalyticsResult(
        title=f"Outliers in {column} ({method.upper()})",
        verdict="warn" if flagged.height else "ok",
        verdict_text=(
            f"{counted(flagged.height, 'value')} outside {band}"
            if flagged.height
            else f"No values outside {band}"
        ),
        stats=[
            _stat("Values tested", n),
            _stat("Bounds", f"{lower:,.2f} … {upper:,.2f}"),
            _stat("Outliers", flagged.height),
        ],
        summary=summary,
        detail=flagged if flagged.height else None,
        tested=n,
    )


# ------------------------------------------------------------- threshold
def threshold_check(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    raw = params.get("threshold")
    if raw is None or raw == "":
        raise QueryError("A threshold value is required.")
    threshold = float(raw)
    tolerance_pct = float(params.get("tolerance_pct") or 5.0)
    if tolerance_pct <= 0:
        raise QueryError("Tolerance must be greater than 0%.")

    values = _numeric_series(df, column)
    n = values.height
    lower = threshold * (1 - tolerance_pct / 100.0)
    upper = threshold * (1 + tolerance_pct / 100.0)
    col = pl.col(column).cast(pl.Float64, strict=False)

    just_below = df.filter(
        col.is_not_null() & (col >= lower) & (col < threshold)
    ).sort(column, descending=True)
    below_count = just_below.height
    above_count = values.filter(
        (pl.col("value") >= threshold) & (pl.col("value") < upper)
    ).height

    summary = pl.DataFrame(
        {
            "band": [
                f"just below ({lower:,.0f}–{threshold:,.0f})",
                f"just above ({threshold:,.0f}–{upper:,.0f})",
            ],
            "count": [below_count, above_count],
        }
    )
    clustered = below_count >= 3 and below_count >= 2 * max(above_count, 1)
    return AnalyticsResult(
        title=f"Just-below-threshold clustering on {column} (limit {threshold:,.0f})",
        verdict="warn" if clustered else "info",
        verdict_text=(
            f"{counted(below_count, 'value')} cluster just below {threshold:,.0f} "
            f"vs {above_count:,} just above — possible limit avoidance"
            if clustered
            else f"{below_count:,} just below vs {above_count:,} just above — no unusual clustering"
        ),
        stats=[
            _stat("Values tested", n),
            _stat(f"Just below (−{tolerance_pct:g}%)", below_count),
            _stat(f"Just above (+{tolerance_pct:g}%)", above_count),
        ],
        summary=summary,
        detail=just_below if below_count else None,
        tested=n,
        viz={"type": "bar", "x": "band", "y": ["count"]},
    )


# ------------------------------------------------------- weekend activity
def weekend_activity(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    date_column = params.get("date_column")
    tagged = df.with_columns(_date_expr(df, date_column).alias("_d")).drop_nulls(
        subset=["_d"]
    )
    if tagged.is_empty():
        raise QueryError(f"No parseable dates in '{date_column}'.")
    tagged = tagged.with_columns(
        pl.col("_d").dt.weekday().alias("_wd"),
        pl.col("_d").dt.to_string("%A").alias("weekday"),
    )
    n = tagged.height
    weekend = tagged.filter(pl.col("_wd") >= 6)

    by_day = (
        tagged.group_by(["_wd", "weekday"])
        .agg(pl.len().alias("count"))
        .sort("_wd")
        .select("weekday", "count")
    )
    detail = weekend.drop(["_d", "_wd"]) if weekend.height else None
    return AnalyticsResult(
        title=f"Weekend postings in {date_column}",
        verdict="warn" if weekend.height else "ok",
        verdict_text=(
            f"{weekend.height:,} of {n:,} dated rows fall on a weekend"
            if weekend.height
            else "No weekend-dated rows"
        ),
        stats=[
            _stat("Dated rows", n),
            _stat("Weekend rows", weekend.height),
            _stat("Weekend share", f"{100.0 * weekend.height / n:.2f}%"),
        ],
        summary=by_day,
        detail=detail,
        tested=n,
        viz={"type": "bar", "x": "weekday", "y": ["count"]},
    )


# ------------------------------------------------------------- date lag
def date_lag(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    from_col = params.get("from_date")
    to_col = params.get("to_date")
    if from_col == to_col:
        # A column against itself has a lag of zero on every row and records a
        # clean pass over a question nobody asked. ``compare_columns`` has
        # refused this since it was written; the date test had not, and a run
        # saved ``APPROVED_DATE`` against ``APPROVED_DATE`` as a chronology
        # check that found nothing.
        raise QueryError("Pick two different date columns to compare.")
    raw_max = params.get("max_days")
    max_days = int(raw_max) if raw_max not in (None, "") else None

    tagged = df.with_columns(
        (_date_expr(df, to_col) - _date_expr(df, from_col))
        .dt.total_days()
        .alias("lag_days")
    ).drop_nulls(subset=["lag_days"])
    n = tagged.height
    if n == 0:
        raise QueryError(f"No rows with both '{from_col}' and '{to_col}' parseable.")

    backdated = tagged.filter(pl.col("lag_days") < 0)
    flag_expr = pl.col("lag_days") < 0
    excessive_n = 0
    if max_days is not None:
        excessive_n = tagged.filter(pl.col("lag_days") > max_days).height
        flag_expr = flag_expr | (pl.col("lag_days") > max_days)
    flagged = tagged.filter(flag_expr).sort("lag_days")
    avg_lag = tagged["lag_days"].mean()

    summary = pl.DataFrame(
        {
            "metric": [
                "backdated (<0 days)",
                f"over {max_days} days" if max_days is not None else "excessive (no limit set)",
                "average lag (days)",
            ],
            "value": [float(backdated.height), float(excessive_n), round(avg_lag, 1)],
        }
    )
    if backdated.height:
        verdict, text = "fail", f"{counted(backdated.height, 'row')} {verb(backdated.height, 'has', 'have')} {to_col} before {from_col}"
    elif excessive_n:
        verdict, text = "warn", f"{counted(excessive_n, 'row')} {verb(excessive_n, 'exceeds', 'exceed')} {max_days} days"
    else:
        verdict, text = "ok", "No backdated or excessively lagged rows"
    return AnalyticsResult(
        title=f"Date lag: {from_col} → {to_col}",
        verdict=verdict,
        verdict_text=text,
        stats=[
            _stat("Rows compared", n),
            _stat("Backdated", backdated.height),
            _stat("Average lag (days)", round(avg_lag, 1)),
        ],
        tested=n,
        summary=summary,
        detail=flagged if flagged.height else None,
    )


# ----------------------------------------------------------- stratify
def stratify(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    method = (params.get("method") or "equal").lower()
    n_bands = int(params.get("bands") or 10)
    if n_bands < 2:
        raise QueryError("Need at least 2 bands.")

    values = _numeric_series(df, column)
    lo, hi = values["value"].min(), values["value"].max()
    if lo == hi:
        raise QueryError(f"'{column}' has a single value — nothing to stratify.")

    if method == "quantile":
        edges = [values["value"].quantile(i / n_bands) for i in range(1, n_bands)]
    elif method == "equal":
        width = (hi - lo) / n_bands
        edges = [lo + width * i for i in range(1, n_bands)]
    else:
        raise QueryError(f"Unknown stratification method '{method}'.")
    edges = sorted({round(e, 6) for e in edges})

    binned = (
        df.select(pl.col(column).cast(pl.Float64, strict=False).alias("_v"))
        .drop_nulls()
        .with_columns(pl.col("_v").cut(breaks=edges, include_breaks=True).alias("_b"))
        .unnest("_b")
    )
    total = binned.height
    summary = (
        binned.group_by("category")
        .agg(
            pl.len().alias("count"),
            pl.col("_v").sum().round(2).alias("sum"),
            pl.col("breakpoint").first().alias("_edge"),
        )
        .sort("_edge")
        .with_columns((100.0 * pl.col("count") / total).round(2).alias("pct"))
        .rename({"category": "band"})
        .select("band", "count", "sum", "pct")
    )
    return AnalyticsResult(
        title=f"Stratification of {column} ({method}, {n_bands} bands)",
        verdict="info",
        verdict_text=f"{total:,} values across {counted(summary.height, 'band')}",
        stats=[
            _stat("Values tested", total),
            _stat("Range", f"{lo:,.2f} … {hi:,.2f}"),
            _stat("Bands", summary.height),
        ],
        summary=summary,
        tested=total,
        viz={"type": "bar", "x": "band", "y": ["count"]},
    )


# --------------------------------------------------------- completeness
def completeness(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    columns = [c for c in (params.get("columns") or []) if c]
    if not columns:
        raise QueryError("Pick at least one column to check.")
    for column in columns:
        if column not in df.columns:
            raise QueryError(f"Unknown column '{column}'.")

    n = df.height
    rows, cond = [], None
    for column in columns:
        missing_expr = pl.col(column).is_null()
        if df.schema[column] == pl.String:
            missing_expr = missing_expr | (
                pl.col(column).cast(pl.String).str.strip_chars() == ""
            )
        missing = int(df.select(missing_expr.sum()).item() or 0)
        rows.append(
            {"column": column, "missing": missing, "pct": round(100.0 * missing / n, 2)}
        )
        cond = missing_expr if cond is None else cond | missing_expr

    summary = pl.DataFrame(rows).sort("missing", descending=True)
    total_missing = sum(r["missing"] for r in rows)
    detail = df.filter(cond) if total_missing else None
    return AnalyticsResult(
        title=f"Completeness of {', '.join(columns)}",
        verdict="warn" if total_missing else "ok",
        verdict_text=(
            f"{counted(detail.height, 'row')} {verb(detail.height, 'has', 'have')} a blank in a checked column"
            if total_missing
            else "No blank values in the checked columns"
        ),
        stats=[
            _stat("Rows", n),
            _stat("Columns checked", len(columns)),
            _stat("Rows with gaps", detail.height if detail is not None else 0),
        ],
        summary=summary,
        detail=detail,
        tested=n,
        viz={"type": "bar", "x": "column", "y": ["missing"]},
    )


# ----------------------------------------------------------- sign scan
def sign_scan(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    values = _numeric_series(df, column)
    n = values.height
    v = pl.col("value")
    neg = values.filter(v < 0).height
    zero = values.filter(v == 0).height
    pos = n - neg - zero

    summary = pl.DataFrame(
        {
            "sign": ["negative", "zero", "positive"],
            "count": [neg, zero, pos],
            "pct": [
                round(100.0 * neg / n, 2),
                round(100.0 * zero / n, 2),
                round(100.0 * pos / n, 2),
            ],
        }
    )
    col = pl.col(column).cast(pl.Float64, strict=False)
    detail = df.filter(col.is_not_null() & (col <= 0))
    return AnalyticsResult(
        title=f"Negative / zero values in {column}",
        verdict="warn" if neg else "ok",
        verdict_text=(
            f"{neg:,} negative and {counted(zero, 'zero value')}"
            if neg
            else f"No negatives ({counted(zero, 'zero value')})"
        ),
        stats=[
            _stat("Values tested", n),
            _stat("Negative", neg),
            _stat("Zero", zero),
        ],
        summary=summary,
        detail=detail if detail.height else None,
        tested=n,
        viz={"type": "bar", "x": "sign", "y": ["count"]},
    )


# ---------------------------------------------------------- rare values
def rare_values(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    if column not in df.columns:
        raise QueryError(f"Unknown column '{column}'.")
    max_count = int(params.get("max_count") or 1)
    if max_count < 1:
        raise QueryError("Max occurrences must be at least 1.")

    counts = df.group_by(column).agg(pl.len().alias("count"))
    distinct = counts.height
    rare = counts.filter(pl.col("count") <= max_count).sort(["count", column])
    affected = int(rare["count"].sum() or 0)
    detail = (
        df.join(rare.select(column), on=column, how="semi").sort(column)
        if rare.height
        else None
    )
    return AnalyticsResult(
        title=f"Rare values in {column} (≤{counted(max_count, 'occurrence')})",
        verdict="warn" if rare.height else "ok",
        verdict_text=(
            f"{counted(rare.height, 'value')} occur ≤{counted(max_count, 'time')}, covering {counted(affected, 'row')}"
            if rare.height
            else f"No values occur ≤{counted(max_count, 'time')}"
        ),
        stats=[
            _stat("Distinct values", distinct),
            _stat("Rare values", rare.height),
            _stat("Rows affected", affected),
        ],
        summary=rare,
        detail=detail,
        tested=df.height,
    )


# --------------------------------------------------------------- referential
def key_strings(column: str) -> pl.Expr:
    """A key column as trimmed text, so an integer code matches its text twin.

    The same normalization :mod:`agent.joins` applies before measuring a match
    rate. A referential test that disagreed with the join diagnostics about
    whether a key matches would be reporting on a different relationship than
    the one an auditor was shown.

    Public for the same reason :func:`comparable_expr` is: the probe sweep
    decides which values are worth a test by counting them, and a sweep that
    counted values differently from the test it then nominates would report a
    share the test does not reproduce.
    """
    return pl.col(column).cast(pl.String).str.strip_chars()


MAX_RESOLVES_IN = 3


def _resolves_in(
    orphans: set[str],
    source: FrameSource,
    exclude: set[str],
    exclude_column: tuple[str, str] | None = None,
) -> list[tuple[str, int]]:
    """Where the unmatched values *do* resolve, across the workspace's keys.

    This is the part that turns a count into a diagnosis. "19 invoices name a
    purchase order that does not exist" is a data-quality note; "19 invoices
    carry a requisition id in the purchase-order field" is the finding, and the
    difference between them is one pass over the other imported key columns.

    Reported as candidates ordered by how much of the orphan set each explains.
    Nothing is asserted about *why* the values landed there — that is the
    auditor's call — only that they are another table's keys.

    ``exclude_column`` is the column under test. It is excluded by name rather
    than by frame, which matters for a key that points inside its own table: a
    supervisor id resolves in the staff master it sits on, and suppressing the
    whole frame to avoid the trivial self-match would report a self-reference as
    resolving nowhere — turning the commonest correct shape in a master file
    into a reconciliation failure.
    """
    if not orphans:
        return []
    found: list[tuple[str, int]] = []
    for table in source.tables:
        if table in exclude:
            continue
        try:
            frame = source.resolve(table)
        except Exception:
            # A frame that will not load explains nothing, which is the same
            # answer as a frame that holds none of the values. Diagnosis is a
            # courtesy on top of the count; it may never fail the test.
            continue
        for column in frame.columns:
            if exclude_column == (table, column):
                continue
            series = frame.select(key_strings(column)).to_series().drop_nulls()
            # Only a key column can explain an orphan set: a description column
            # that happens to contain one of the values explains nothing.
            if not len(series) or series.n_unique() != len(series):
                continue
            hits = len(orphans & set(series.to_list()))
            if hits:
                found.append((f"{table}.{column}", hits))
    found.sort(key=lambda item: (-item[1], item[0]))
    return found[:MAX_RESOLVES_IN]


def referential(
    df: pl.DataFrame, params: dict, source: FrameSource | None = None
) -> AnalyticsResult:
    lookup_table = str(params.get("lookup_table") or "").strip()
    lookup_column = str(params.get("lookup_column") or "").strip()
    column = params.get("column")
    if column not in df.columns:
        raise QueryError(f"Unknown column '{column}'.")
    if not lookup_table or not lookup_column:
        raise QueryError("Exists-in needs a lookup table and column.")
    if source is None:
        raise QueryError("Lookup tables are not available in this context.")
    lookup = source.frame(lookup_table)
    lookup_column = resolve_column(
        lookup_column, lookup.columns, table=lookup_table, error_type=QueryError
    )

    allowed = set(
        lookup.select(key_strings(lookup_column)).to_series().drop_nulls().to_list()
    )
    keyed = df.with_columns(key_strings(column).alias("_key"))
    # A null key is not an unmatched key: the row references nothing, which is a
    # completeness question and belongs to a completeness test. Counting it here
    # would merge two different findings into one number. It is still reported,
    # because a denominator that silently drops rows is the defect this whole
    # contract exists to prevent.
    present = keyed.filter(pl.col("_key").is_not_null() & (pl.col("_key") != ""))
    null_keys = df.height - present.height
    n = present.height
    if not n:
        raise QueryError(f"'{column}' has no values to reconcile.")

    unmatched = present.filter(~pl.col("_key").is_in(list(allowed)))
    matched = n - unmatched.height
    census = (
        unmatched.group_by("_key")
        .agg(pl.len().alias("occurrences"))
        .sort(["occurrences", "_key"], descending=[True, False])
        .rename({"_key": "unmatched_value"})
    )
    elsewhere = _resolves_in(
        set(census["unmatched_value"].to_list()),
        source,
        {lookup_table},
        (source.origin, str(column)) if source.origin else None,
    )
    if elsewhere:
        census = census.with_columns(
            pl.lit(", ".join(f"{name} ({hits})" for name, hits in elsewhere)).alias(
                "resolves_in"
            )
        )

    rate = matched / n
    stats = [
        _stat("Values reconciled", n),
        _stat("Unmatched rows", unmatched.height),
        _stat("Distinct unmatched keys", census.height),
        _stat("Match rate", f"{100.0 * rate:.2f}%"),
    ]
    if null_keys:
        stats.append(_stat("Rows with no key", null_keys))
    if elsewhere:
        stats.append(_stat("Unmatched keys resolve in", elsewhere[0][0]))
    text = (
        f"{counted(unmatched.height, 'row')} {verb(unmatched.height, 'names', 'name')} "
        f"a {lookup_table}.{lookup_column} value that does not exist"
        if unmatched.height
        else f"Every {column} value exists in {lookup_table}.{lookup_column}"
    )
    if elsewhere:
        name, hits = elsewhere[0]
        text += f"; {hits} of {census.height} unmatched keys are {name} values"
    elif unmatched.height == n:
        # Nothing matched and the values are nobody else's key either. Worth
        # saying outright: the column references a master that was never
        # imported, which is a scope limitation rather than a population of
        # exceptions, and reads as neither from a bare count.
        text += (
            f"; no {column} value matches, so the master it references may not "
            "have been imported"
        )
    return AnalyticsResult(
        title=f"{column} exists in {lookup_table}.{lookup_column}",
        verdict="fail" if unmatched.height else "ok",
        verdict_text=text,
        stats=stats,
        summary=census if census.height else None,
        detail=unmatched.drop("_key") if unmatched.height else None,
        tested=n,
    )


# ----------------------------------------------------------- compare columns
_COMPARE_OPS: dict[str, str] = {
    "ge": "≥",
    "gt": ">",
    "eq": "=",
    "ne": "≠",
    "le": "≤",
    "lt": "<",
}


def comparable_expr(df: pl.DataFrame, column: str, mode: str) -> pl.Expr:
    """How ``compare_columns`` reads one column before comparing it.

    Public because a caller deciding *whether* to propose a comparison has to
    measure it the same way the test will. Re-deriving the coercion — a string
    date parsed one way here and another way there — would let a nomination
    report a hold rate the test it nominates does not reproduce.
    """
    if mode == "number":
        return pl.col(column).cast(pl.Float64, strict=False)
    if mode == "date":
        return _date_expr(df, column)
    return pl.col(column).cast(pl.String).str.strip_chars()


_comparable = comparable_expr


def compare_columns(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    left = params.get("column")
    right = params.get("other")
    op = str(params.get("op") or "ge")
    mode = str(params.get("compare_as") or "auto")
    for name in (left, right):
        if name not in df.columns:
            raise QueryError(f"Unknown column '{name}'.")
    if left == right:
        raise QueryError("Pick two different columns to compare.")
    if op not in _COMPARE_OPS:
        raise QueryError(f"Unknown comparison '{op}'.")
    if mode == "auto":
        left_dtype, right_dtype = df.schema[left], df.schema[right]
        if left_dtype.is_temporal() or right_dtype.is_temporal():
            mode = "date"
        elif left_dtype.is_numeric() and right_dtype.is_numeric():
            mode = "number"
        else:
            mode = "text"

    left_expr = _comparable(df, left, mode).alias("_l")
    right_expr = _comparable(df, right, mode).alias("_r")
    tagged = df.with_columns(left_expr, right_expr).drop_nulls(subset=["_l", "_r"])
    n = tagged.height
    if not n:
        raise QueryError(
            f"No rows have both '{left}' and '{right}' readable as {mode}s."
        )
    # ``holds`` is the expectation; the exception is its negation. Stated this
    # way round because the parameter names the relationship an auditor expects
    # to be true, and a test whose flagged rows were the *conforming* ones would
    # invert every downstream count.
    holds = {
        "ge": pl.col("_l") >= pl.col("_r"),
        "gt": pl.col("_l") > pl.col("_r"),
        "eq": pl.col("_l") == pl.col("_r"),
        "ne": pl.col("_l") != pl.col("_r"),
        "le": pl.col("_l") <= pl.col("_r"),
        "lt": pl.col("_l") < pl.col("_r"),
    }[op]
    breached = tagged.filter(~holds)
    symbol = _COMPARE_OPS[op]

    summary = pl.DataFrame(
        {
            "outcome": [f"{left} {symbol} {right}", f"{left} not {symbol} {right}"],
            "rows": [n - breached.height, breached.height],
            "pct": [
                round(100.0 * (n - breached.height) / n, 2),
                round(100.0 * breached.height / n, 2),
            ],
        }
    )
    return AnalyticsResult(
        title=f"{left} {symbol} {right}",
        verdict="fail" if breached.height else "ok",
        verdict_text=(
            f"{counted(breached.height, 'row')} of {n:,} {verb(breached.height, 'breaches', 'breach')} "
            f"{left} {symbol} {right}"
            if breached.height
            else f"{left} {symbol} {right} holds for all {n:,} comparable rows"
        ),
        stats=[
            _stat("Rows compared", n),
            _stat("Breaches", breached.height),
            _stat("Not comparable", df.height - n),
        ],
        summary=summary,
        detail=breached.drop(["_l", "_r"]) if breached.height else None,
        tested=n,
        viz={"type": "bar", "x": "outcome", "y": ["rows"]},
    )


# ------------------------------------------------------------- value filter
def value_filter(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    """Rows whose value in one column is, or is not, among named values.

    The shape every other test in this library talks around. A requisition that
    was *rejected* and still drew an invoice, a vendor that is *Under Review* and
    still received a purchase order — the condition is a value, and until now the
    nearest thing available was a comparison between two columns. A run reached
    for it: asked to find invoices for inactive vendors it submitted
    ``PAYMENT_STATUS = VENDOR_STATUS``, which compares "Paid" against "Active",
    flags every row of the population, and reads as a total control failure.

    Two directions, because auditors ask this both ways round. ``flag`` names the
    values that are themselves the exception; ``allow`` names the values that are
    permitted and flags everything else, which is the classic valid-value test
    over a column whose vocabulary is supposed to be closed.

    Null and blank rows are reported and never flagged. A row that names no value
    breaches neither reading of the rule, and folding it in would merge a
    completeness finding into a valid-value one.
    """
    column = params.get("column")
    if column not in df.columns:
        raise QueryError(f"Unknown column '{column}'.")
    mode = str(params.get("mode") or "flag").strip()
    if mode not in {"flag", "allow"}:
        raise QueryError("Value check mode must be flag or allow.")
    raw = params.get("values")
    values = [
        str(value).strip()
        for value in (raw if isinstance(raw, (list, tuple)) else [raw])
        if str(value or "").strip()
    ]
    if not values:
        raise QueryError("Name at least one value to check against.")

    keyed = df.with_columns(key_strings(column).alias("_v"))
    present = keyed.filter(pl.col("_v").is_not_null() & (pl.col("_v") != ""))
    blanks = df.height - present.height
    n = present.height
    if not n:
        raise QueryError(f"'{column}' has no values to check.")

    named = pl.col("_v").is_in(values)
    flagged = present.filter(named if mode == "flag" else ~named)
    census = (
        present.group_by("_v")
        .agg(pl.len().alias("rows"))
        .rename({"_v": "value"})
        .with_columns(
            pl.col("value").is_in(values).alias("named"),
            (100.0 * pl.col("rows") / n).round(2).alias("share_pct"),
        )
        .sort(["rows", "value"], descending=[True, False])
    )
    listed = ", ".join(values[:4]) + (f" and {len(values) - 4} more" if len(values) > 4 else "")
    stats = [
        _stat("Rows checked", n),
        _stat("Rows flagged", flagged.height),
        _stat("Distinct values present", census.height),
        _stat("Values named", len(values)),
    ]
    if blanks:
        stats.append(_stat("Rows with no value", blanks))
    return AnalyticsResult(
        title=(
            f"{column} is {listed}" if mode == "flag" else f"{column} outside {listed}"
        ),
        verdict="fail" if flagged.height else "ok",
        verdict_text=(
            (
                f"{counted(flagged.height, 'row')} of {n:,} {verb(flagged.height, 'holds', 'hold')} {listed}"
                if mode == "flag"
                else f"{counted(flagged.height, 'row')} of {n:,} {verb(flagged.height, 'holds', 'hold')} a value outside {listed}"
            )
            if flagged.height
            else (
                f"No row holds {listed}"
                if mode == "flag"
                else f"Every one of the {n:,} rows checked holds one of {listed}"
            )
        ),
        stats=stats,
        summary=census,
        detail=flagged.drop("_v") if flagged.height else None,
        tested=n,
        viz={"type": "bar", "x": "value", "y": ["rows"]},
    )


# ---------------------------------------------------------- format anomaly
_MASK_DIGIT = re.compile(r"[0-9]")
_MASK_ALPHA = re.compile(r"[A-Za-z]")
_MASK_RUN = re.compile(r"(.)\1+")


def value_mask(value: object) -> str:
    """Collapse one value to its character-class shape.

    ``VINV011-202404`` becomes ``A{4}9{3}-9{6}``. Letters and digits lose their
    identity, everything else — separators, prefixes' punctuation — is kept
    verbatim, and runs are counted rather than repeated so a shape stays legible
    at a glance and two values of the same construction land on one pattern.
    """
    text = str(value)
    masked = _MASK_DIGIT.sub("9", _MASK_ALPHA.sub("A", text))
    return _MASK_RUN.sub(lambda run: f"{run.group(1)}{{{len(run.group(0))}}}", masked)


def format_anomaly(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    if column not in df.columns:
        raise QueryError(f"Unknown column '{column}'.")
    raw_share = params.get("max_share_pct")
    max_share = float(raw_share) if raw_share not in (None, "") else 10.0
    if not 0 < max_share < 100:
        raise QueryError("Minority share must be between 0 and 100 percent.")

    tagged = df.with_columns(
        pl.col(column)
        .cast(pl.String)
        .str.strip_chars()
        .map_elements(value_mask, return_dtype=pl.String)
        .alias("_pattern")
    ).filter(pl.col(column).is_not_null() & (pl.col("_pattern") != ""))
    n = tagged.height
    if not n:
        raise QueryError(f"'{column}' has no values to profile.")

    census = (
        tagged.group_by("_pattern")
        .agg(pl.len().alias("count"))
        .with_columns((100.0 * pl.col("count") / n).round(2).alias("share_pct"))
        .sort(["count", "_pattern"], descending=[True, False])
        .rename({"_pattern": "pattern"})
    )
    # A column with no dominant shape has no minority: free-text descriptions
    # and per-row identifiers both produce a census where every pattern is rare,
    # and flagging all of them would report the population. The test only
    # concludes where one shape actually governs the column.
    dominant = float(census["share_pct"][0])
    minority = census.filter(pl.col("share_pct") <= max_share)
    governed = dominant > 50.0
    odd = (
        tagged.filter(pl.col("_pattern").is_in(minority["pattern"].to_list()))
        if governed and minority.height
        else None
    )
    flagged = odd.height if odd is not None else 0
    return AnalyticsResult(
        title=f"Format anomalies in {column}",
        verdict="warn" if flagged else "ok",
        verdict_text=(
            f"{counted(flagged, 'value')} in {counted(minority.height, 'minority format')} "
            f"against a dominant {census['pattern'][0]} ({dominant:.1f}%)"
            if flagged
            else (
                f"No format governs {column}; {census.height:,} shapes across {n:,} values"
                if not governed
                else f"All {n:,} values conform to {counted(census.height, 'format')}"
            )
        ),
        stats=[
            _stat("Values profiled", n),
            _stat("Distinct formats", census.height),
            _stat("Dominant format", f"{census['pattern'][0]} ({dominant:.1f}%)"),
            _stat("Off-pattern values", flagged),
        ],
        summary=census,
        detail=odd.drop("_pattern") if odd is not None else None,
        tested=n,
        viz={"type": "bar", "x": "pattern", "y": ["count"]},
    )


# ---------------------------------------------------------------- registry
# What a test's flagged output *means*, which is not something a count can say
# and not something a title reliably says either.
#
#   exception   - the flagged items are control exceptions on their face. An
#                 invoice exceeding its purchase order, a key that reconciles to
#                 nothing, a duplicate where a key must be unique, a gap in a
#                 document sequence. These may evidence a control failure
#                 directly.
#   screening   - the flagged items are candidates for review, not conclusions.
#                 A weekend posting, a value outside an IQR fence, a Benford
#                 deviation: each is unusual relative to its own population and
#                 nothing more, and whether it matters is a judgment the data
#                 cannot make. Note this is about what the output means, not
#                 whether there are rows — a digit test flags no rows and still
#                 reaches a verdict a reader can mistake for a finding.
#   descriptive - the test characterises a population and has no exception
#                 concept at all: a stratification, a period trend, a drawn
#                 sample. Nothing here can be promoted or reported as an
#                 exception because nothing here is one.
#
# Typed here rather than inferred downstream because three separate places were
# guessing at it: the workflow's hand-maintained exclusion list, the memo prompt
# (three paragraphs of prose about not writing up an outlier as a finding), and
# ``data_tests``, which kept its own literal list of three screening test ids to
# decide whether a result needed corroboration. They now read one field.
SIGNAL_EXCEPTION = "exception"
SIGNAL_SCREENING = "screening"
SIGNAL_DESCRIPTIVE = "descriptive"
SIGNALS = (SIGNAL_EXCEPTION, SIGNAL_SCREENING, SIGNAL_DESCRIPTIVE)

ANALYTICS: dict[str, dict] = {
    "duplicates": {
        "group": "Duplicates & sequences",
        "label": "Duplicate Detection",
        "signal": SIGNAL_EXCEPTION,
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
    "sampling": {
        "group": "Sampling",
        "label": "Sampling",
        "signal": SIGNAL_DESCRIPTIVE,
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
        "group": "Timing",
        "label": "Period Comparison",
        "signal": SIGNAL_DESCRIPTIVE,
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
    "outliers": {
        "group": "Amounts & outliers",
        "label": "Outlier Detection",
        "signal": SIGNAL_SCREENING,
        "icon": "pi pi-chart-scatter",
        "description": (
            "Flags numeric values far from the centre of the distribution using "
            "a Z-score or Tukey IQR fence — anomalously large or small amounts."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Amount column", "column_kind": "numeric"},
            {
                "name": "method",
                "kind": "select",
                "label": "Method",
                "options": [
                    {"label": "Z-score (mean ± kσ)", "value": "zscore"},
                    {"label": "IQR (Tukey fence)", "value": "iqr"},
                ],
                "default": "zscore",
            },
            {
                "name": "threshold",
                "kind": "number",
                "label": "Threshold (σ for Z-score, ×IQR for IQR)",
                "default": 3,
                "optional": True,
            },
        ],
        "func": outliers,
    },
    "threshold_check": {
        "group": "Amounts & outliers",
        "label": "Threshold Clustering",
        "signal": SIGNAL_SCREENING,
        "icon": "pi pi-arrows-h",
        "description": (
            "Counts values sitting just below an approval/authorization limit "
            "versus just above it. Clustering underneath suggests split "
            "transactions or limit avoidance."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Amount column", "column_kind": "numeric"},
            {"name": "threshold", "kind": "number", "label": "Limit / threshold"},
            {"name": "tolerance_pct", "kind": "number", "label": "Window (± %)", "default": 5},
        ],
        "func": threshold_check,
    },
    "weekend_activity": {
        "group": "Timing",
        "label": "Weekend Postings",
        "signal": SIGNAL_SCREENING,
        "icon": "pi pi-calendar-times",
        "description": (
            "Breaks activity down by weekday and flags entries dated on a "
            "Saturday or Sunday — unusual timing worth a second look."
        ),
        "params": [
            {"name": "date_column", "kind": "column", "label": "Date column", "column_kind": "date"},
        ],
        "func": weekend_activity,
    },
    "date_lag": {
        "group": "Timing",
        "label": "Date Lag / Backdating",
        "signal": SIGNAL_EXCEPTION,
        "icon": "pi pi-history",
        "description": (
            "Measures the gap between two date columns. Flags negative gaps "
            "(the later date precedes the earlier one — backdating) and, "
            "optionally, gaps beyond a maximum number of days."
        ),
        "params": [
            {"name": "from_date", "kind": "column", "label": "From date", "column_kind": "date"},
            {"name": "to_date", "kind": "column", "label": "To date", "column_kind": "date"},
            {"name": "max_days", "kind": "number", "label": "Max allowed days", "optional": True},
        ],
        "func": date_lag,
    },
    "stratify": {
        "group": "Amounts & outliers",
        "label": "Stratification",
        "signal": SIGNAL_DESCRIPTIVE,
        "icon": "pi pi-align-left",
        "description": (
            "Buckets a numeric column into bands (equal-width or by quantile) "
            "and shows the count, sum, and share of each band — the classic "
            "audit stratification view."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Amount column", "column_kind": "numeric"},
            {
                "name": "method",
                "kind": "select",
                "label": "Banding",
                "options": [
                    {"label": "Equal width", "value": "equal"},
                    {"label": "Quantile", "value": "quantile"},
                ],
                "default": "equal",
            },
            {"name": "bands", "kind": "number", "label": "Number of bands", "default": 10},
        ],
        "func": stratify,
    },
    "completeness": {
        "group": "Data quality",
        "label": "Completeness",
        "signal": SIGNAL_EXCEPTION,
        "icon": "pi pi-check-square",
        "description": (
            "Counts blank and null values in the chosen mandatory columns and "
            "lists the offending rows — missing keys, dates, or amounts."
        ),
        "params": [
            {"name": "columns", "kind": "columns", "label": "Required columns"},
        ],
        "func": completeness,
    },
    "sign_scan": {
        "group": "Amounts & outliers",
        "label": "Negative / Zero Scan",
        "signal": SIGNAL_SCREENING,
        "icon": "pi pi-minus-circle",
        "description": (
            "Splits an amount column into negative, zero, and positive values. "
            "Unexpected negatives can signal reversals, credits, or errors."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Amount column", "column_kind": "numeric"},
        ],
        "func": sign_scan,
    },
    "rare_values": {
        "group": "Data quality",
        "label": "Rare Values",
        "signal": SIGNAL_SCREENING,
        "icon": "pi pi-search-minus",
        "description": (
            "Finds category values that occur only a handful of times — "
            "one-off vendors, typos, or misclassified codes."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Category column"},
            {"name": "max_count", "kind": "number", "label": "Max occurrences", "default": 1},
        ],
        "func": rare_values,
    },
    "referential": {
        "group": "Cross-table reconciliation",
        "label": "Exists in table",
        "signal": SIGNAL_EXCEPTION,
        "icon": "pi pi-link",
        "description": (
            "Reconciles a reference column against another table's key — every "
            "PO number on an invoice exists in the PO master, every approver id "
            "exists in the staff master. Reports the unmatched values, and where "
            "those values do resolve elsewhere in the workspace: a key that "
            "reconciles to nothing usually reconciles to the wrong table."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Reference column"},
            {"name": "lookup_table", "kind": "table", "label": "Lookup table"},
            {"name": "lookup_column", "kind": "lookup_column", "label": "Lookup key"},
        ],
        "needs_lookup": True,
        "func": referential,
    },
    "compare_columns": {
        "group": "Cross-table reconciliation",
        "label": "Compare Two Columns",
        "signal": SIGNAL_EXCEPTION,
        "icon": "pi pi-arrow-right-arrow-left",
        "description": (
            "Tests a relationship that must hold between two columns of the same "
            "row — an invoice amount at most its purchase-order total, a receipt "
            "date on or after the order date, a stated department equal to the "
            "master's. Flags the rows that breach it. On a joined frame this is "
            "the test the join was built for."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "This column"},
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
                "default": "le",
            },
            {"name": "other", "kind": "column", "label": "That column"},
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
        "func": compare_columns,
    },
    "value_filter": {
        "group": "Data quality",
        "label": "Value Check",
        "signal": SIGNAL_EXCEPTION,
        "icon": "pi pi-flag",
        "description": (
            "Flags rows by the value in one column — the requisitions that were "
            "rejected, the vendors under review, the payments in a status they "
            "should never reach. Either name the values that are themselves the "
            "exception, or name the values that are permitted and flag "
            "everything outside them."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Column"},
            {
                "name": "mode",
                "kind": "select",
                "label": "Flag rows that",
                "options": [
                    {"label": "hold one of these values", "value": "flag"},
                    {"label": "hold anything but these values", "value": "allow"},
                ],
                "default": "flag",
            },
            {"name": "values", "kind": "values", "label": "Values"},
        ],
        "needs_values": True,
        "func": value_filter,
    },
    "format_anomaly": {
        "group": "Data quality",
        "label": "Format Anomalies",
        "signal": SIGNAL_SCREENING,
        "icon": "pi pi-question",
        "description": (
            "Learns the shape identifiers in a column actually take — letters, "
            "digits and separators, not the values — and flags the ones built "
            "differently. Finds the batch of references that does not look like "
            "the others without anyone specifying what to look for. Only "
            "concludes where one shape governs the column."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Identifier column"},
            {
                "name": "max_share_pct",
                "kind": "number",
                "label": "Minority share (%)",
                "default": 10,
                "optional": True,
            },
        ],
        "func": format_anomaly,
    },
}


ANALYTICS_ALIASES = {
    # The model sometimes copies a library test's display label instead of
    # its stable registry id.
    "duplicate_detection": "duplicates",
}


def canonical_test_id(value: object) -> str:
    """Return the registered id for an analytics test or a known alias."""
    test_id = str(value or "").strip()
    return ANALYTICS_ALIASES.get(test_id, test_id)


def registry_payload() -> list[dict]:
    return [
        {key: value for key, value in {**meta, "id": test_id}.items() if key != "func"}
        for test_id, meta in ANALYTICS.items()
    ]


def signal_for(test_id: object) -> str:
    """What a flagged row from this test is: exception, screening, descriptive.

    Unknown tests read as ``descriptive`` — the conservative answer, since it is
    the one classification that claims no control evidence.
    """
    meta = ANALYTICS.get(canonical_test_id(test_id))
    return str((meta or {}).get("signal") or SIGNAL_DESCRIPTIVE)


def ids_with_signal(*signals: str) -> frozenset[str]:
    """Every registered test whose flagged rows carry one of these meanings."""
    wanted = set(signals)
    return frozenset(
        test_id
        for test_id, meta in ANALYTICS.items()
        if str(meta.get("signal") or SIGNAL_DESCRIPTIVE) in wanted
    )


def canonicalize_params(
    df: pl.DataFrame,
    test_id: str,
    params: dict | None,
    *,
    source: FrameSource | None = None,
) -> dict:
    """Resolve analytics column parameters to their exact source spelling.

    A ``lookup_column`` resolves against the lookup frame rather than the target,
    which is the whole point of the kind: the two frames have different columns
    and resolving a lookup key against the target would reject every valid one.
    """
    test_id = canonical_test_id(test_id)
    meta = ANALYTICS.get(test_id)
    if meta is None:
        raise QueryError(f"Unknown analytics test '{test_id}'.")
    normalized = dict(params or {})
    for parameter in meta.get("params") or []:
        name = parameter.get("name")
        value = normalized.get(name)
        if value in (None, "", []):
            continue
        kind = parameter.get("kind")
        if kind == "column":
            normalized[name] = resolve_column(
                value, df.columns, error_type=QueryError
            )
        elif kind == "columns":
            normalized[name] = resolve_columns(
                value, df.columns, error_type=QueryError
            )
        elif kind == "lookup_column" and source is not None:
            table = str(normalized.get("lookup_table") or "").strip()
            if table:
                normalized[name] = resolve_column(
                    value,
                    source.frame(table).columns,
                    table=table,
                    error_type=QueryError,
                )
    return normalized


def run_test(
    df: pl.DataFrame,
    test_id: str,
    params: dict,
    *,
    source: FrameSource | None = None,
) -> AnalyticsResult:
    """Run one registered test against a frame.

    ``source`` is optional and only reaches the tests that declare
    ``needs_lookup``: every other test is a property of ``df`` alone and its
    signature does not change. A lookup test called without a source raises
    rather than silently reconciling against nothing.
    """
    test_id = canonical_test_id(test_id)
    meta = ANALYTICS.get(test_id)
    if meta is None:
        raise QueryError(f"Unknown analytics test '{test_id}'.")
    resolved = canonicalize_params(df, test_id, params, source=source)
    if meta.get("needs_lookup"):
        return meta["func"](df, resolved, source)
    return meta["func"](df, resolved)


def suggested_viz(df: pl.DataFrame, test_id: str, params: dict) -> dict | None:
    """The chart a test suggests for its own summary frame, or ``None``.

    Most tests report a chart shape that is structural — fixed column names,
    or a column taken straight from ``params`` — rather than data-dependent, so
    computing it once at definition time (here) is what a saved analysis's
    static ``viz`` should hold, instead of always defaulting to a table. A
    broken spec (bad params, missing column) degrades to ``None`` rather than
    raising: creating or regenerating a definition must not fail just because
    its chart preference could not be computed.
    """
    try:
        return run_test(df, test_id, params).viz
    except Exception:
        return None
