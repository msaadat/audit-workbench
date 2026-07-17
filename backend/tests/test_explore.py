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


def test_query_field_names_are_canonicalized_case_insensitively(transactions_df):
    result = run_query(
        transactions_df,
        {
            "filters": [{"column": "AMOUNT", "op": "gte", "value": "100"}],
            "group_by": ["CUST_ID"],
            "aggs": [{"column": "AMOUNT", "func": "sum"}],
            "sort": [{"column": "AMOUNT_SUM", "desc": True}],
        },
    )

    assert result["columns"] == ["cust_id", "amount_sum"]
    assert result["rows"][0][0] == "C2"


def test_global_aggregation_without_grouping(transactions_df):
    result = run_query(
        transactions_df, {"aggs": [{"column": "amount", "func": "mean"}]}
    )
    assert result["total_rows"] == 1


def test_pagination(transactions_df):
    result = run_query(transactions_df, {"page": 2, "page_size": 4})
    assert result["total_rows"] == 6
    assert len(result["rows"]) == 2


def test_row_level_query_can_project_visible_columns(transactions_df):
    result = run_query(
        transactions_df,
        {
            "columns": ["invoice_no", "amount"],
            "sort": [{"column": "cust_id", "desc": True}],
        },
    )
    assert result["columns"] == ["invoice_no", "amount"]
    assert result["total_rows"] == 6


def test_projection_does_not_affect_grouped_results(transactions_df):
    result = run_query(
        transactions_df,
        {
            "columns": ["invoice_no"],
            "group_by": ["cust_id"],
            "aggs": [{"column": "amount", "func": "sum"}],
        },
    )
    assert result["columns"] == ["cust_id", "amount_sum"]


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


def test_split_by_builds_cross_tab(transactions_df):
    df = transactions_df.with_columns(pl.col("tx_date").str.slice(0, 7).alias("month"))
    result = run_query(
        df,
        {
            "group_by": ["CUST_ID"],
            "split_by": "MONTH",
            "aggs": [{"column": "AMOUNT", "func": "sum"}],
        },
    )
    assert result["split_field"] == "month"
    assert result["row_fields"] == ["cust_id"]
    assert result["column_keys"] == ["2026-01", "2026-02", "2026-03"]
    assert result["columns"] == [
        "cust_id",
        "amount_sum::2026-01",
        "amount_sum::2026-02",
        "amount_sum::2026-03",
        "amount_sum::Total",
    ]
    assert result["grand_total"] == [None, 2150.0, 1099.5, 300.0, 3549.5]


def test_split_by_without_group_by_raises(transactions_df):
    df = transactions_df.with_columns(pl.col("tx_date").str.slice(0, 7).alias("month"))
    with pytest.raises(QueryError):
        run_query(df, {"split_by": "month", "aggs": [{"column": "amount", "func": "sum"}]})


def test_split_by_export_appends_grand_total(transactions_df):
    df = transactions_df.with_columns(pl.col("tx_date").str.slice(0, 7).alias("month"))
    frame, filtered = run_query_full(
        df,
        {"group_by": ["cust_id"], "split_by": "month", "aggs": [{"column": "amount", "func": "sum"}]},
    )
    assert filtered == 6
    assert frame.height == 4  # 3 customers + grand total
    assert frame.row(-1, named=True)["cust_id"] is None


def test_dates_serialize_as_iso(transactions_df):
    df = transactions_df.with_columns(pl.col("tx_date").str.to_date())
    result = run_query(df, {"page_size": 1})
    date_index = result["columns"].index("tx_date")
    assert result["rows"][0][date_index] == "2026-01-15"
