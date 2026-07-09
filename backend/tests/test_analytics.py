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


def test_registry_includes_new_tests():
    ids = {t["id"] for t in registry_payload()}
    assert {
        "outliers",
        "threshold_check",
        "weekend_activity",
        "date_lag",
        "stratify",
        "completeness",
        "sign_scan",
        "last_two_digits",
        "rare_values",
    } <= ids


def test_outliers_zscore_flags_extreme():
    df = pl.DataFrame({"amount": [10.0] * 50 + [1_000_000.0]})
    result = run_test(df, "outliers", {"column": "amount", "method": "zscore", "threshold": 3})
    assert result.verdict == "warn"
    assert result.detail.height == 1
    assert "z_score" in result.detail.columns
    assert result.summary["outliers"][0] == 1


def test_outliers_iqr_and_clean():
    spread = pl.DataFrame({"amount": [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 1000]})
    flagged = run_test(spread, "outliers", {"column": "amount", "method": "iqr"})
    assert flagged.verdict == "warn"
    assert "iqr_distance" in flagged.detail.columns
    uniform = run_test(pl.DataFrame({"amount": [5.0] * 10 + [6.0] * 10}), "outliers", {"column": "amount", "method": "iqr"})
    assert uniform.verdict == "ok"


def test_threshold_clustering():
    # Ten values bunched just under 1000, one above.
    df = pl.DataFrame({"amount": [float(v) for v in [960, 970, 980, 985, 990, 995, 999, 998, 997, 996, 1050]]})
    result = run_test(df, "threshold_check", {"column": "amount", "threshold": 1000, "tolerance_pct": 5})
    assert result.verdict == "warn"
    assert result.detail.height == 10
    assert result.summary["count"].to_list() == [10, 0]


def test_threshold_requires_value(transactions_df):
    with pytest.raises(QueryError, match="threshold value is required"):
        run_test(transactions_df, "threshold_check", {"column": "amount"})


def test_weekend_activity(transactions_df):
    result = run_test(transactions_df, "weekend_activity", {"date_column": "tx_date"})
    # 2026-02-28 is a Saturday.
    assert result.verdict == "warn"
    assert result.detail.height == 1
    assert "weekday" in result.detail.columns
    weekend_stat = next(s for s in result.stats if s["label"] == "Weekend rows")
    assert weekend_stat["value"] == "1"


def test_date_lag_backdating():
    df = pl.DataFrame(
        {
            "order": ["2026-01-10", "2026-02-01", "2026-03-01"],
            "ship": ["2026-01-12", "2026-01-20", "2026-03-30"],  # row 2 ships before order
        }
    )
    result = run_test(df, "date_lag", {"from_date": "order", "to_date": "ship", "max_days": 20})
    assert result.verdict == "fail"  # backdated row present
    # backdated (row 2) + excessive (row 3, 29 days > 20) both flagged
    assert result.detail.height == 2


def test_stratify_bands(transactions_df):
    result = run_test(transactions_df, "stratify", {"column": "amount", "method": "equal", "bands": 4})
    assert result.verdict == "info"
    assert result.summary.columns == ["band", "count", "sum", "pct"]
    assert result.summary["count"].sum() == transactions_df.height
    assert result.summary["pct"].sum() == pytest.approx(100.0, abs=0.1)


def test_completeness():
    df = pl.DataFrame({"id": ["A", "B", None], "name": ["x", "  ", "z"]})
    result = run_test(df, "completeness", {"columns": ["id", "name"]})
    assert result.verdict == "warn"
    assert result.detail.height == 2  # null id row + blank name row
    id_row = result.summary.filter(pl.col("column") == "id")
    assert id_row["missing"][0] == 1


def test_sign_scan():
    df = pl.DataFrame({"amount": [10.0, -5.0, 0.0, 20.0, -1.0]})
    result = run_test(df, "sign_scan", {"column": "amount"})
    assert result.verdict == "warn"
    assert result.detail.height == 3  # two negatives + one zero
    neg_row = result.summary.filter(pl.col("sign") == "negative")
    assert neg_row["count"][0] == 2


def test_last_two_digits_uniform_and_spiked():
    uniform = pl.DataFrame({"amount": [float(v) for v in range(100, 1100)]})
    clean = run_test(uniform, "last_two_digits", {"column": "amount"})
    assert clean.verdict == "ok"
    spiked = pl.DataFrame({"amount": [100.0] * 500})  # every value ends in 00
    bad = run_test(spiked, "last_two_digits", {"column": "amount"})
    assert bad.verdict == "fail"


def test_last_two_digits_needs_enough_values():
    with pytest.raises(QueryError, match="at least 100"):
        run_test(pl.DataFrame({"amount": [123.0] * 20}), "last_two_digits", {"column": "amount"})


def test_rare_values(transactions_df):
    result = run_test(transactions_df, "rare_values", {"column": "cust_id", "max_count": 1})
    # C3 appears once; C1 twice, C2 three times.
    assert result.verdict == "warn"
    assert result.summary["cust_id"].to_list() == ["C3"]
    assert result.detail.height == 1


def test_unknown_test_rejected(transactions_df):
    with pytest.raises(QueryError, match="Unknown analytics test"):
        run_test(transactions_df, "nope", {})
