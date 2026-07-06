import math
import random

import polars as pl
import pytest

from app.analytics import registry_payload, run_test
from app.explore import QueryError


def _benford_like(n: int = 2000, seed: int = 7) -> pl.DataFrame:
    rng = random.Random(seed)
    # 10**uniform gives an exactly Benford-distributed mantissa.
    return pl.DataFrame({"amount": [10 ** rng.uniform(0, 4) for _ in range(n)]})


def test_registry_is_json_safe():
    registry = registry_payload()
    ids = {t["id"] for t in registry}
    assert {"benford", "duplicates", "gaps", "sampling", "period_compare", "round_numbers"} <= ids
    assert all("func" not in t for t in registry)


def test_benford_conforming_data_passes():
    result = run_test(_benford_like(), "benford", {"column": "amount", "digits": 1})
    assert result.verdict == "ok"
    assert result.summary.height == 9
    total_expected = result.summary["expected_pct"].sum()
    assert math.isclose(total_expected, 100.0, abs_tol=0.1)


def test_benford_uniform_data_fails():
    df = pl.DataFrame({"amount": [float(v) for v in range(100000, 101000)]})
    result = run_test(df, "benford", {"column": "amount", "digits": 1})
    assert result.verdict == "fail"


def test_benford_requires_enough_values():
    with pytest.raises(QueryError, match="at least 100"):
        run_test(pl.DataFrame({"amount": [1.0] * 50}), "benford", {"column": "amount"})


def test_duplicates(transactions_df):
    result = run_test(transactions_df, "duplicates", {"columns": ["invoice_no"]})
    assert result.verdict == "fail"
    assert result.summary.height == 1  # invoice 1006 twice
    assert result.detail.height == 2
    assert "occurrences" in result.detail.columns

    clean = run_test(transactions_df, "duplicates", {"columns": ["invoice_no", "amount", "tx_date", "cust_id"]})
    assert clean.verdict == "fail"  # exact duplicate row exists
    unique = run_test(
        transactions_df.unique(maintain_order=True), "duplicates", {"columns": ["invoice_no"]}
    )
    assert unique.verdict == "ok"


def test_gaps(transactions_df):
    result = run_test(transactions_df, "gaps", {"column": "invoice_no"})
    assert result.verdict == "warn"
    assert result.summary.rows() == [(1004, 1004, 1)]
    reused = next(s for s in result.stats if s["label"] == "Reused numbers")
    assert reused["value"] == "1"


def test_gaps_strips_prefixes():
    df = pl.DataFrame({"doc": ["INV-001", "INV-002", "INV-005"]})
    result = run_test(df, "gaps", {"column": "doc"})
    assert result.summary.rows() == [(3, 4, 2)]


def test_sampling_random_reproducible(transactions_df):
    a = run_test(transactions_df, "sampling", {"method": "random", "size": 3, "seed": 1})
    b = run_test(transactions_df, "sampling", {"method": "random", "size": 3, "seed": 1})
    assert a.detail.rows() == b.detail.rows()
    assert a.detail.height == 3
    assert "source_row" in a.detail.columns


def test_sampling_stratified_covers_all_strata(transactions_df):
    result = run_test(
        transactions_df,
        "sampling",
        {"method": "stratified", "size": 3, "stratify_by": "cust_id"},
    )
    assert set(result.detail["cust_id"]) == {"C1", "C2", "C3"}
    assert result.summary is not None


def test_period_compare_monthly(transactions_df):
    result = run_test(
        transactions_df,
        "period_compare",
        {"date_column": "tx_date", "value_column": "amount", "period": "month"},
    )
    assert result.summary.height == 3
    assert result.summary.columns == ["period", "transactions", "amount_sum", "change_pct"]
    jan = result.summary.row(0, named=True)
    assert jan["amount_sum"] == pytest.approx(2150.0)


def test_round_numbers(transactions_df):
    result = run_test(transactions_df, "round_numbers", {"column": "amount"})
    thousand_row = result.summary.filter(pl.col("multiple_of") == 1000)
    assert thousand_row["count"][0] == 2  # 2000 and 1000
    assert result.detail.height == 2


def test_unknown_test_rejected(transactions_df):
    with pytest.raises(QueryError, match="Unknown analytics test"):
        run_test(transactions_df, "nope", {})
