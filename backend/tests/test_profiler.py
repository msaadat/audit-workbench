"""Column typing in the profiler."""

from __future__ import annotations

import datetime as dt

import polars as pl

from app import profiler


def test_a_time_of_day_is_not_typed_as_a_date():
    """A ``Time`` column places nothing in a period.

    Typed as a date, its "09:18:34" won every string ``min()`` against real
    dates, and the analysis memo said the deal book covered "09:18:34 to
    2025-05-12".
    """

    df = pl.DataFrame({
        "DEAL_DATE": [dt.date(2025, 1, 31), dt.date(2025, 2, 10)],
        "DEAL_TIME": [dt.time(9, 18, 34), dt.time(16, 49, 37)],
    })

    assert profiler.profile_column(df, "DEAL_DATE")["inferred_type"] == "date"
    time_profile = profiler.profile_column(df, "DEAL_TIME")
    assert time_profile["inferred_type"] == "time"
    assert time_profile["min"] and time_profile["max"]
    kinds = {item["name"]: item["kind"] for item in profiler.schema_payload(df)}
    assert kinds == {"DEAL_DATE": "date", "DEAL_TIME": "time"}
