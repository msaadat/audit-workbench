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
from .field_names import resolve_column, resolve_columns

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
            _stat("MAD", f"{mad:.4f}"),
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
            f"{flagged.height:,} value(s) outside {band}"
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
            f"{below_count:,} value(s) cluster just below {threshold:,.0f} "
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
        viz={"type": "bar", "x": "weekday", "y": ["count"]},
    )


# ------------------------------------------------------------- date lag
def date_lag(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    from_col = params.get("from_date")
    to_col = params.get("to_date")
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
        verdict, text = "fail", f"{backdated.height:,} row(s) have {to_col} before {from_col}"
    elif excessive_n:
        verdict, text = "warn", f"{excessive_n:,} row(s) exceed {max_days} days"
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
        verdict_text=f"{total:,} values across {summary.height} band(s)",
        stats=[
            _stat("Values tested", total),
            _stat("Range", f"{lo:,.2f} … {hi:,.2f}"),
            _stat("Bands", summary.height),
        ],
        summary=summary,
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
            f"{detail.height:,} row(s) have a blank in a checked column"
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
            f"{neg:,} negative and {zero:,} zero value(s)"
            if neg
            else f"No negatives ({zero:,} zero value(s))"
        ),
        stats=[
            _stat("Values tested", n),
            _stat("Negative", neg),
            _stat("Zero", zero),
        ],
        summary=summary,
        detail=detail if detail.height else None,
        viz={"type": "bar", "x": "sign", "y": ["count"]},
    )


# ------------------------------------------------------- last two digits
def last_two_digits(df: pl.DataFrame, params: dict) -> AnalyticsResult:
    column = params.get("column")
    values = (
        _numeric_series(df, column)
        .select(pl.col("value").abs())
        .filter(pl.col("value") > 0)
    )
    n = values.height
    if n < 100:
        raise QueryError(
            f"Only {n} usable values in '{column}' — this test needs at least 100."
        )

    observed = (
        values.select((pl.col("value").round(0).cast(pl.Int64) % 100).alias("last_two"))
        .group_by("last_two")
        .agg(pl.len().alias("count"))
    )
    table = (
        pl.DataFrame({"last_two": list(range(100))})
        .join(observed, on="last_two", how="left")
        .with_columns(pl.col("count").fill_null(0))
        .with_columns(
            (100.0 * pl.col("count") / n).round(2).alias("observed_pct"),
            pl.lit(1.0).alias("expected_pct"),
        )
        .sort("last_two")
    )

    expected = n / 100.0
    chi_square = sum((c - expected) ** 2 / expected for c in table["count"].to_list())
    # 99 degrees of freedom: χ² critical ≈ 123.2 (α=0.05), 134.6 (α=0.01).
    if chi_square > 134.6:
        verdict, text = "fail", "Strong non-uniformity in the last two digits"
    elif chi_square > 123.2:
        verdict, text = "warn", "Last-two-digit distribution deviates from uniform"
    else:
        verdict, text = "ok", "Last two digits look uniform"

    top = table.sort("count", descending=True).row(0, named=True)
    return AnalyticsResult(
        title=f"Last-two-digit uniformity of {column}",
        verdict=verdict,
        verdict_text=text,
        stats=[
            _stat("Values tested", n),
            _stat("Chi-square", round(chi_square, 1)),
            _stat("Most common ending", f"{top['last_two']:02d} ({top['observed_pct']}%)"),
        ],
        summary=table,
        viz={"type": "bar", "x": "last_two", "y": ["observed_pct", "expected_pct"]},
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
        title=f"Rare values in {column} (≤{max_count} occurrence(s))",
        verdict="warn" if rare.height else "ok",
        verdict_text=(
            f"{rare.height:,} value(s) occur ≤{max_count} time(s), covering {affected:,} row(s)"
            if rare.height
            else f"No values occur ≤{max_count} time(s)"
        ),
        stats=[
            _stat("Distinct values", distinct),
            _stat("Rare values", rare.height),
            _stat("Rows affected", affected),
        ],
        summary=rare,
        detail=detail,
    )


# ---------------------------------------------------------------- registry
ANALYTICS: dict[str, dict] = {
    "benford": {
        "group": "Digit & number patterns",
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
        "group": "Duplicates & sequences",
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
        "group": "Duplicates & sequences",
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
        "group": "Sampling",
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
        "group": "Timing",
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
        "group": "Digit & number patterns",
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
    "outliers": {
        "group": "Amounts & outliers",
        "label": "Outlier Detection",
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
    "last_two_digits": {
        "group": "Digit & number patterns",
        "label": "Last-Two-Digit Test",
        "icon": "pi pi-percentage",
        "description": (
            "A Benford-family test: the last two digits of genuine amounts are "
            "close to uniform. Spikes point to rounding, estimates, or "
            "fabricated figures the first-digit test can miss."
        ),
        "params": [
            {"name": "column", "kind": "column", "label": "Amount column", "column_kind": "numeric"},
        ],
        "func": last_two_digits,
    },
    "rare_values": {
        "group": "Data quality",
        "label": "Rare Values",
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
}


def registry_payload() -> list[dict]:
    return [
        {key: value for key, value in {**meta, "id": test_id}.items() if key != "func"}
        for test_id, meta in ANALYTICS.items()
    ]


def canonicalize_params(df: pl.DataFrame, test_id: str, params: dict | None) -> dict:
    """Resolve analytics column parameters to their exact source spelling."""
    meta = ANALYTICS.get(test_id)
    if meta is None:
        raise QueryError(f"Unknown analytics test '{test_id}'.")
    normalized = dict(params or {})
    for parameter in meta.get("params") or []:
        name = parameter.get("name")
        value = normalized.get(name)
        if value in (None, "", []):
            continue
        if parameter.get("kind") == "column":
            normalized[name] = resolve_column(
                value, df.columns, error_type=QueryError
            )
        elif parameter.get("kind") == "columns":
            normalized[name] = resolve_columns(
                value, df.columns, error_type=QueryError
            )
    return normalized


def run_test(df: pl.DataFrame, test_id: str, params: dict) -> AnalyticsResult:
    meta = ANALYTICS.get(test_id)
    if meta is None:
        raise QueryError(f"Unknown analytics test '{test_id}'.")
    return meta["func"](df, canonicalize_params(df, test_id, params))
