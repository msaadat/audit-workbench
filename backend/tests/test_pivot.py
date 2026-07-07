import polars as pl
import pytest

from app.explore import QueryError
from app.pivot import run_pivot, run_pivot_full


@pytest.fixture
def monthly_df(transactions_df) -> pl.DataFrame:
    return transactions_df.with_columns(pl.col("tx_date").str.slice(0, 7).alias("month"))


def test_basic_cross_tab(monthly_df):
    result = run_pivot(
        monthly_df,
        {
            "rows": ["cust_id"],
            "columns": "month",
            "values": [{"column": "amount", "func": "sum"}],
        },
    )
    assert result["columns"] == [
        "cust_id",
        "amount_sum::2026-01",
        "amount_sum::2026-02",
        "amount_sum::2026-03",
        "amount_sum::Total",
    ]
    assert result["column_keys"] == ["2026-01", "2026-02", "2026-03"]
    assert result["rows"] == [
        ["C1", 150.0, 99.5, None, 249.5],
        ["C2", 2000.0, None, 300.0, 2300.0],
        ["C3", None, 1000.0, None, 1000.0],
    ]
    assert result["grand_total"] == [None, 2150.0, 1099.5, 300.0, 3549.5]


def test_no_column_field_is_grouped_table(monthly_df):
    result = run_pivot(
        monthly_df,
        {"rows": ["cust_id"], "values": [{"column": "amount", "func": "sum"}]},
    )
    assert result["columns"] == ["cust_id", "amount_sum"]
    assert result["column_keys"] == []
    assert result["grand_total"] == [None, 3549.5]


def test_filters_and_default_count(monthly_df):
    result = run_pivot(
        monthly_df,
        {
            "filters": [{"column": "amount", "op": "gte", "value": "150"}],
            "rows": ["cust_id"],
        },
    )
    assert result["filtered_rows"] == 5
    assert result["columns"] == ["cust_id", "row_count"]
    assert result["rows"] == [["C1", 1], ["C2", 3], ["C3", 1]]


def test_multiple_values(monthly_df):
    result = run_pivot(
        monthly_df,
        {
            "rows": ["cust_id"],
            "columns": "month",
            "values": [{"column": "amount", "func": "sum"}, {"func": "count"}],
            "totals": False,
        },
    )
    assert result["value_names"] == ["amount_sum", "row_count"]
    assert "amount_sum::2026-01" in result["columns"]
    assert "row_count::2026-01" in result["columns"]
    assert "amount_sum::Total" not in result["columns"]
    assert result["grand_total"] is None


def test_mean_total_is_weighted_not_mean_of_means(monthly_df):
    # C2: Jan mean 2000, Mar mean 150 — true mean is 2300/3, not (2000+150)/2.
    result = run_pivot(
        monthly_df,
        {
            "rows": ["cust_id"],
            "columns": "month",
            "values": [{"column": "amount", "func": "mean"}],
        },
    )
    c2 = next(row for row in result["rows"] if row[0] == "C2")
    assert c2[result["columns"].index("amount_mean::Total")] == pytest.approx(2300.0 / 3)


def test_null_row_and_column_values_survive(monthly_df):
    df = monthly_df.with_columns(
        pl.when(pl.col("cust_id") == "C3").then(None).otherwise(pl.col("cust_id")).alias("cust_id"),
        pl.when(pl.col("month") == "2026-02").then(None).otherwise(pl.col("month")).alias("month"),
    )
    result = run_pivot(
        df,
        {
            "rows": ["cust_id"],
            "columns": "month",
            "values": [{"column": "amount", "func": "sum"}],
        },
    )
    assert result["column_keys"] == ["2026-01", "2026-03", "null"]
    null_row = result["rows"][-1]  # nulls sort last
    assert null_row[0] is None
    assert null_row[result["columns"].index("amount_sum::null")] == 1000.0
    assert null_row[result["columns"].index("amount_sum::Total")] == 1000.0


def test_empty_after_filters(monthly_df):
    result = run_pivot(
        monthly_df,
        {
            "filters": [{"column": "amount", "op": "gt", "value": "99999"}],
            "rows": ["cust_id"],
            "columns": "month",
        },
    )
    assert result["filtered_rows"] == 0
    assert result["rows"] == []
    assert result["column_keys"] == []


def test_export_appends_grand_total_row(monthly_df):
    frame, filtered = run_pivot_full(
        monthly_df,
        {
            "rows": ["cust_id"],
            "columns": "month",
            "values": [{"column": "amount", "func": "sum"}],
        },
    )
    assert filtered == 6
    assert frame.height == 4  # 3 customers + grand total
    last = frame.row(-1, named=True)
    assert last["cust_id"] is None
    assert last["amount_sum::Total"] == pytest.approx(3549.5)


def test_bad_specs_raise(monthly_df):
    with pytest.raises(QueryError):
        run_pivot(monthly_df, {"rows": []})
    with pytest.raises(QueryError):
        run_pivot(monthly_df, {"rows": ["nope"]})
    with pytest.raises(QueryError):
        run_pivot(monthly_df, {"rows": ["cust_id"], "columns": ["month", "cust_id"]})
    with pytest.raises(QueryError):
        run_pivot(monthly_df, {"rows": ["month"], "columns": "month"})
    with pytest.raises(QueryError):
        run_pivot(
            monthly_df,
            {
                "rows": ["cust_id"],
                "values": [{"column": "amount", "func": "sum"}, {"column": "amount", "func": "sum"}],
            },
        )


def test_too_many_column_values_rejected():
    df = pl.DataFrame({"g": ["a"] * 60, "wide": list(range(60)), "x": [1.0] * 60})
    with pytest.raises(QueryError, match="distinct values"):
        run_pivot(df, {"rows": ["g"], "columns": "wide"})
