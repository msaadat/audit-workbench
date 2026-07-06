import polars as pl
import pytest

from app.explore import QueryError, run_query, run_query_full


def test_filter_numeric_and_string(transactions_df):
    result = run_query(
        transactions_df,
        {"filters": [{"column": "amount", "op": "gte", "value": "150"}]},
    )
    assert result["filtered_rows"] == 5

    result = run_query(
        transactions_df,
        {"filters": [{"column": "cust_id", "op": "eq", "value": "c1"}]},
    )
    assert result["filtered_rows"] == 2  # case-insensitive


def test_filter_between_in_blank(transactions_df):
    df = transactions_df.with_columns(
        pl.when(pl.col("cust_id") == "C3").then(None).otherwise(pl.col("cust_id")).alias("cust_id")
    )
    assert run_query(df, {"filters": [{"column": "cust_id", "op": "blank"}]})["filtered_rows"] == 1
    assert (
        run_query(
            df,
            {"filters": [{"column": "amount", "op": "between", "value": "100", "value2": "200"}]},
        )["filtered_rows"]
        == 3
    )
    assert (
        run_query(df, {"filters": [{"column": "cust_id", "op": "in", "value": "C1, C2"}]})[
            "filtered_rows"
        ]
        == 5
    )


def test_group_by_aggregation_and_sort(transactions_df):
    result = run_query(
        transactions_df,
        {
            "group_by": ["cust_id"],
            "aggs": [{"column": "amount", "func": "sum"}, {"func": "count"}],
            "sort": [{"column": "amount_sum", "desc": True}],
        },
    )
    assert result["columns"] == ["cust_id", "amount_sum", "row_count"]
    assert result["rows"][0][0] == "C2"
    assert result["rows"][0][1] == pytest.approx(2300.0)


def test_global_aggregation_without_grouping(transactions_df):
    result = run_query(
        transactions_df, {"aggs": [{"column": "amount", "func": "mean"}]}
    )
    assert result["total_rows"] == 1


def test_pagination(transactions_df):
    result = run_query(transactions_df, {"page": 2, "page_size": 4})
    assert result["total_rows"] == 6
    assert len(result["rows"]) == 2


def test_bad_specs_raise(transactions_df):
    with pytest.raises(QueryError):
        run_query(transactions_df, {"filters": [{"column": "nope", "op": "eq", "value": "1"}]})
    with pytest.raises(QueryError):
        run_query(transactions_df, {"filters": [{"column": "amount", "op": "eq", "value": "abc"}]})
    with pytest.raises(QueryError):
        run_query(transactions_df, {"aggs": [{"column": "cust_id", "func": "sum"}]})
    with pytest.raises(QueryError):
        run_query(transactions_df, {"filters": [{"column": "amount", "op": "explode", "value": "1"}]})


def test_full_query_matches_paged_totals(transactions_df):
    spec = {"group_by": ["cust_id"], "aggs": [{"column": "amount", "func": "sum"}]}
    full, filtered = run_query_full(transactions_df, spec)
    paged = run_query(transactions_df, spec)
    assert full.height == paged["total_rows"] == 3
    assert filtered == 6


def test_dates_serialize_as_iso(transactions_df):
    df = transactions_df.with_columns(pl.col("tx_date").str.to_date())
    result = run_query(df, {"page_size": 1})
    date_index = result["columns"].index("tx_date")
    assert result["rows"][0][date_index] == "2026-01-15"
