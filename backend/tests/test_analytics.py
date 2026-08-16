import random

import polars as pl
import pytest

from app.analytics import (
    SIGNAL_DESCRIPTIVE,
    SIGNAL_EXCEPTION,
    SIGNAL_SCREENING,
    SIGNALS,
    FrameSource,
    registry_payload,
    run_test,
    ids_with_signal,
    signal_for,
    value_mask,
)
from app.explore import QueryError


def test_registry_is_json_safe():
    registry = registry_payload()
    ids = {t["id"] for t in registry}
    assert {"duplicates", "sampling", "period_compare", "date_lag"} <= ids
    assert all("func" not in t for t in registry)


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


def test_analytics_column_params_are_canonicalized(transactions_df):
    result = run_test(
        transactions_df, "duplicates", {"columns": ["INVOICE_NO"]}
    )
    assert result.verdict == "fail"
    assert result.detail.height == 2


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


def test_rare_values(transactions_df):
    result = run_test(transactions_df, "rare_values", {"column": "cust_id", "max_count": 1})
    # C3 appears once; C1 twice, C2 three times.
    assert result.verdict == "warn"
    assert result.summary["cust_id"].to_list() == ["C3"]
    assert result.detail.height == 1


def test_unknown_test_rejected(transactions_df):
    with pytest.raises(QueryError, match="Unknown analytics test"):
        run_test(transactions_df, "nope", {})


# --------------------------------------------------------------- signal
def test_every_registered_test_declares_what_its_output_means():
    """Three places used to guess this; they now read one field, so it must exist."""
    for entry in registry_payload():
        assert entry.get("signal") in SIGNALS, entry["id"]


def test_signal_reads_the_registry():
    assert signal_for("referential") == SIGNAL_EXCEPTION
    assert signal_for("weekend_activity") == SIGNAL_SCREENING
    assert signal_for("stratify") == SIGNAL_DESCRIPTIVE
    # An unregistered id claims no control evidence rather than defaulting to it.
    assert signal_for("nope") == SIGNAL_DESCRIPTIVE
    assert signal_for(None) == SIGNAL_DESCRIPTIVE


def test_the_screening_family_is_the_one_data_tests_used_to_hardcode():
    """``data_tests`` and ``dashboard`` each kept their own literal copy of it."""
    assert {"outliers", "weekend_activity", "rare_values"} <= ids_with_signal(
        SIGNAL_SCREENING
    )


# ---------------------------------------------------------- referential
def _masters() -> FrameSource:
    frames = {
        "po_master": pl.DataFrame({"po_number": ["PO1", "PO2", "PO3"]}),
        "req_master": pl.DataFrame({"req_id": ["RQ9", "RQ8"]}),
    }
    return FrameSource(resolve=frames.__getitem__, tables=tuple(frames))


def test_referential_flags_rows_whose_key_does_not_exist():
    invoices = pl.DataFrame(
        {"invoice": ["I1", "I2", "I3"], "po_link": ["PO1", "PO2", "NOPE"]}
    )
    result = run_test(
        invoices,
        "referential",
        {"column": "po_link", "lookup_table": "po_master", "lookup_column": "po_number"},
        source=_masters(),
    )
    assert result.verdict == "fail"
    assert result.tested == 3
    assert result.detail.height == 1
    assert result.summary["unmatched_value"].to_list() == ["NOPE"]


def test_referential_names_where_the_unmatched_keys_do_resolve():
    """The diagnosis, not just the count: a key that reconciles to nothing here
    usually reconciles to the wrong table, and saying which turns a data-quality
    note into a finding."""
    invoices = pl.DataFrame(
        {"invoice": ["I1", "I2", "I3"], "po_link": ["PO1", "RQ9", "RQ8"]}
    )
    result = run_test(
        invoices,
        "referential",
        {"column": "po_link", "lookup_table": "po_master", "lookup_column": "po_number"},
        source=_masters(),
    )
    assert "req_master.req_id" in result.verdict_text
    assert set(result.summary["resolves_in"].to_list()) == {"req_master.req_id (2)"}


def test_referential_separates_a_null_key_from_an_unmatched_one():
    """A row referencing nothing is a completeness question, not a broken
    reference. Merging the two would report one number for two findings."""
    invoices = pl.DataFrame({"invoice": ["I1", "I2"], "po_link": ["PO1", None]})
    result = run_test(
        invoices,
        "referential",
        {"column": "po_link", "lookup_table": "po_master", "lookup_column": "po_number"},
        source=_masters(),
    )
    assert result.verdict == "ok"
    assert result.tested == 1
    assert any(stat["label"] == "Rows with no key" for stat in result.stats)


def test_referential_says_so_when_nothing_matches_at_all():
    """A zero match rate with the values found nowhere else is a scope
    limitation — the master was never imported — and reads as neither a clean
    result nor a population of exceptions without being told."""
    orders = pl.DataFrame({"po": ["X1", "X2"], "buyer": ["B001", "B002"]})
    result = run_test(
        orders,
        "referential",
        {"column": "buyer", "lookup_table": "po_master", "lookup_column": "po_number"},
        source=_masters(),
    )
    assert result.verdict == "fail"
    assert "may not have been imported" in result.verdict_text


def test_referential_matches_an_integer_code_to_its_text_twin():
    frames = {"staff": pl.DataFrame({"staff_id": [1001, 1002]})}
    source = FrameSource(resolve=frames.__getitem__, tables=("staff",))
    rows = pl.DataFrame({"who": ["1001", "1002"]})
    result = run_test(
        rows,
        "referential",
        {"column": "who", "lookup_table": "staff", "lookup_column": "staff_id"},
        source=source,
    )
    assert result.verdict == "ok"


def test_referential_without_a_source_refuses_rather_than_clearing():
    invoices = pl.DataFrame({"po_link": ["PO1"]})
    with pytest.raises(QueryError, match="not available"):
        run_test(
            invoices,
            "referential",
            {
                "column": "po_link",
                "lookup_table": "po_master",
                "lookup_column": "po_number",
            },
        )


# ------------------------------------------------------- compare columns
def test_compare_columns_flags_the_rows_that_breach_the_expectation():
    frame = pl.DataFrame({"billed": [100.0, 250.0, 90.0], "ordered": [100.0, 200.0, 100.0]})
    result = run_test(
        frame, "compare_columns", {"column": "billed", "op": "le", "other": "ordered"}
    )
    assert result.verdict == "fail"
    assert result.tested == 3
    assert result.detail["billed"].to_list() == [250.0]


def test_compare_columns_reads_dates_as_dates():
    frame = pl.DataFrame(
        {"invoice_date": ["2026-01-10", "2026-03-01"], "po_date": ["2026-01-20", "2026-02-01"]}
    )
    result = run_test(
        frame,
        "compare_columns",
        {"column": "invoice_date", "op": "ge", "other": "po_date"},
    )
    assert result.verdict == "fail"
    assert result.detail.height == 1


def test_compare_columns_excludes_rows_it_could_not_compare():
    """A row missing either side is a row no conclusion covers, so it belongs in
    neither the numerator nor the denominator — and is reported, not dropped."""
    frame = pl.DataFrame({"billed": [100.0, None, 300.0], "ordered": [50.0, 10.0, None]})
    result = run_test(
        frame, "compare_columns", {"column": "billed", "op": "le", "other": "ordered"}
    )
    assert result.tested == 1
    assert result.detail.height == 1
    assert {"label": "Not comparable", "value": "2"} in result.stats


def test_compare_columns_rejects_a_column_against_itself():
    frame = pl.DataFrame({"amount": [1.0, 2.0]})
    with pytest.raises(QueryError, match="two different columns"):
        run_test(
            frame, "compare_columns", {"column": "amount", "op": "le", "other": "amount"}
        )


# -------------------------------------------------------- format anomaly
def test_format_anomaly_finds_the_batch_built_differently():
    """No pattern is supplied: the test learns the shape the column actually
    takes and reports what does not fit it."""
    frame = pl.DataFrame(
        {"ref": [f"VINV{index:03d}-202404" for index in range(20)] + ["VINSUSP001", "VINSUSP002"]}
    )
    result = run_test(frame, "format_anomaly", {"column": "ref"})
    assert result.verdict == "warn"
    assert result.detail["ref"].to_list() == ["VINSUSP001", "VINSUSP002"]
    assert result.summary["pattern"][0] == "A{4}9{3}-9{6}"


def test_format_anomaly_concludes_nothing_where_no_shape_governs():
    """Per-row identifiers and free text both produce a census where every
    pattern is rare. Flagging all of them would report the population."""
    frame = pl.DataFrame({"note": ["a", "bb", "ccc", "dddd", "eeeee", "ffffff"]})
    result = run_test(frame, "format_anomaly", {"column": "note"})
    assert result.verdict == "ok"
    assert result.detail is None
    assert "No format governs" in result.verdict_text


def test_format_anomaly_is_quiet_on_a_uniform_column():
    frame = pl.DataFrame({"ref": [f"INV{index:04d}" for index in range(12)]})
    result = run_test(frame, "format_anomaly", {"column": "ref"})
    assert result.verdict == "ok"
    assert result.detail is None


def test_value_mask_keeps_separators_and_counts_runs():
    assert value_mask("VINV011-202404") == "A{4}9{3}-9{6}"
    assert value_mask("PO2024004") == "A{2}9{7}"
