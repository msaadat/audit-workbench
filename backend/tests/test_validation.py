import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import validation, workspaces
from app.explore import QueryError
from app.main import create_app
from app.workspaces import WorkspaceError


@pytest.fixture
def dirty_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "invoice_no": ["A1", "A2", "A2", "A4", None],
            "amount": [100.0, -5.0, 0.0, None, 20.0],
            "category": ["Ops", "HR", "ops", "Weird", "HR"],
            "posting_date": ["2025-01-05", "bad", "2099-12-31", "2024-12-31", None],
            "desc": ["ok", "  ", "x" * 300, "fine", "fine"],
        }
    )


def _run_one(df, check, column=None, params=None, severity="fail"):
    rules = [
        {
            "id": "r1",
            "column": column,
            "check": check,
            "params": params or {},
            "severity": severity,
            "enabled": True,
        }
    ]
    return validation.run_rules(df, rules, "t")["results"][0]


# ------------------------------------------------------------------ checks
def test_required_counts_nulls_and_blank_strings(dirty_df):
    assert _run_one(dirty_df, "required", "invoice_no")["fail_count"] == 1
    assert _run_one(dirty_df, "required", "desc")["fail_count"] == 1  # whitespace-only
    assert _run_one(dirty_df, "required", "amount")["fail_count"] == 1


def test_validation_field_names_are_canonicalized(dirty_df):
    assert _run_one(dirty_df, "required", "INVOICE_NO")["fail_count"] == 1
    key = _run_one(
        dirty_df, "unique_key", params={"columns": ["INVOICE_NO", "CATEGORY"]}
    )
    assert key["error"] is None


def test_numeric_sign_modes(dirty_df):
    assert _run_one(dirty_df, "numeric_sign", "amount", {"mode": "positive"})["fail_count"] == 2
    assert _run_one(dirty_df, "numeric_sign", "amount", {"mode": "non_negative"})["fail_count"] == 1
    assert _run_one(dirty_df, "numeric_sign", "amount", {"mode": "nonzero"})["fail_count"] == 1
    # Nulls never violate content checks — 'required' owns blanks.
    assert _run_one(dirty_df, "numeric_sign", "amount", {"mode": "negative"})["fail_count"] == 3


def test_range_and_length(dirty_df):
    assert _run_one(dirty_df, "range", "amount", {"min": 0, "max": 50})["fail_count"] == 2
    assert _run_one(dirty_df, "range", "amount", {"min": -100})["fail_count"] == 0
    assert _run_one(dirty_df, "length", "desc", {"max": 100})["fail_count"] == 1


def test_allowed_values_case_handling(dirty_df):
    exact = _run_one(dirty_df, "allowed_values", "category", {"values": ["Ops", "HR"]})
    assert exact["fail_count"] == 2  # 'ops' and 'Weird'
    relaxed = _run_one(
        dirty_df, "allowed_values", "category", {"values": ["Ops", "HR"], "ignore_case": True}
    )
    assert relaxed["fail_count"] == 1  # only 'Weird'


def test_pattern_full_vs_contains(dirty_df):
    # Full match: only the whitespace-only value violates [a-z]+.
    assert _run_one(dirty_df, "pattern", "desc", {"regex": "[a-z]+"})["fail_count"] == 1
    # Contains: everything not containing 'fine' violates.
    assert _run_one(dirty_df, "pattern", "desc", {"regex": "fine", "mode": "contains"})["fail_count"] == 3


def test_date_range_parses_text_dates(dirty_df):
    result = _run_one(
        dirty_df, "date_range", "posting_date", {"min": "2025-01-01", "max": "2025-12-31"}
    )
    # 2099 too late, 2024 too early; unparseable 'bad' and null pass.
    assert result["fail_count"] == 2
    future = _run_one(dirty_df, "date_range", "posting_date", {"not_in_future": True})
    assert future["fail_count"] == 1


def test_unique_and_unique_key(dirty_df):
    assert _run_one(dirty_df, "unique", "invoice_no")["fail_count"] == 2  # A2 twice, null ignored
    key = _run_one(dirty_df, "unique_key", params={"columns": ["invoice_no", "category"]})
    assert key["fail_count"] == 0  # (A2, HR) vs (A2, ops) differ


def test_row_count_has_no_pass_pct(dirty_df):
    ok = _run_one(dirty_df, "row_count", params={"min": 3})
    assert ok["verdict"] == "ok" and ok["pass_pct"] is None
    bad = _run_one(dirty_df, "row_count", params={"min": 100})
    assert bad["verdict"] == "fail"


def test_row_count_fails_on_empty_table(dirty_df):
    empty = dirty_df.head(0)
    assert _run_one(empty, "row_count", params={"min": 1})["verdict"] == "fail"


def test_referential_matches_across_types(dirty_df):
    master = pl.DataFrame({"code": ["Ops", "HR", "FIN"]})
    result = validation.run_rules(
        dirty_df,
        [
            {
                "id": "r1",
                "column": "category",
                "check": "referential",
                "params": {"lookup_table": "master", "lookup_column": "code"},
                "severity": "fail",
                "enabled": True,
            }
        ],
        "t",
        resolve=lambda name: master,
    )["results"][0]
    assert result["fail_count"] == 2  # 'ops' (case differs) and 'Weird'

    # Without a resolver (or with a bad lookup) the rule degrades to error.
    no_resolver = validation.run_rules(
        dirty_df,
        [
            {
                "id": "r1",
                "column": "category",
                "check": "referential",
                "params": {"lookup_table": "master", "lookup_column": "nope"},
                "severity": "fail",
                "enabled": True,
            }
        ],
        "t",
        resolve=lambda name: master,
    )["results"][0]
    assert no_resolver["verdict"] == "error"
    assert "nope" in no_resolver["error"]


def test_compare_fields_dates_and_numbers():
    df = pl.DataFrame(
        {
            "start": ["2025-01-01", "2025-06-01", None],
            "end": ["2025-03-01", "2025-01-01", "2025-02-01"],
            "gross": [100.0, 50.0, 10.0],
            "net": [90.0, 60.0, 10.0],
        }
    )
    end_ge_start = _run_one(df, "compare_fields", "end", {"op": "ge", "other": "start"})
    assert end_ge_start["fail_count"] == 1  # row 2; null start passes
    gross_ge_net = _run_one(df, "compare_fields", "gross", {"op": "ge", "other": "net"})
    assert gross_ge_net["fail_count"] == 1  # 50 < 60


def test_conditional_required():
    df = pl.DataFrame(
        {
            "band": ["HIGH", "HIGH", "LOW", None],
            "approval": ["A-1", "  ", None, None],
        }
    )
    result = _run_one(
        df, "conditional_required", "approval", {"when_column": "band", "when_value": "HIGH"}
    )
    assert result["fail_count"] == 1  # blank approval on the second HIGH row


def test_conditional_required_supports_numeric_thresholds():
    df = pl.DataFrame({"amount": [49_999, 50_001, 75_000], "approval": [None, None, "CFO"]})
    result = _run_one(
        df,
        "conditional_required",
        "approval",
        {"when_column": "amount", "when_op": "gt", "when_value": 50_000},
    )
    assert result["fail_count"] == 1
    assert result["label"] == "Required when amount > 50000"


def test_generated_rule_preflight_rejects_zero_trigger_and_disjoint_domain():
    df = pl.DataFrame({
        "amount": [80_000, 90_000],
        "approval": ["A", "B"],
        "currency": ["PKR", "PKR"],
    })
    issues = validation.generated_rule_issues(df, [
        {
            "check": "conditional_required", "column": "approval",
            "params": {"when_column": "amount", "when_value": 50_000},
        },
        {
            "check": "allowed_values", "column": "currency",
            "params": {"values": ["USD", "EUR"]},
        },
    ])
    assert any("matches zero rows" in issue for issue in issues)
    assert any("no overlap" in issue for issue in issues)


def test_expression_check():
    df = pl.DataFrame({"qty": [2, 3], "price": [5.0, 5.0], "total": [10.0, 14.0]})
    result = _run_one(
        df,
        "expression",
        params={"code": 'pl.col("qty") * pl.col("price") != pl.col("total")'},
    )
    assert result["fail_count"] == 1
    bad = _run_one(df, "expression", params={"code": "1 + 1"})
    assert bad["verdict"] == "error"
    assert "Polars expression" in bad["error"]
    broken = _run_one(df, "expression", params={"code": "pl.col('"})
    assert broken["verdict"] == "error"


def test_bad_params_raise_query_error(dirty_df):
    with pytest.raises(QueryError, match="min and/or a max"):
        validation.CHECKS["range"]["func"](dirty_df, "amount", {})
    with pytest.raises(QueryError, match="regular expression"):
        validation.CHECKS["pattern"]["func"](dirty_df, "desc", {"regex": "("})
    with pytest.raises(QueryError, match="date like"):
        validation.CHECKS["date_range"]["func"](dirty_df, "posting_date", {"min": "nope"})


# --------------------------------------------------------------- run_rules
def test_run_degrades_missing_column_to_error(dirty_df):
    run = validation.run_rules(
        dirty_df,
        [
            {"id": "a", "column": "ghost", "check": "required", "severity": "fail", "enabled": True},
            {"id": "b", "column": "amount", "check": "required", "severity": "fail", "enabled": True},
        ],
        "t",
    )
    by_id = {r["rule_id"]: r for r in run["results"]}
    assert by_id["a"]["verdict"] == "error"
    assert "ghost" in by_id["a"]["error"]
    assert by_id["b"]["verdict"] == "fail"  # the rest of the run still completed
    assert run["verdict"] == "fail"
    assert run["counts"]["errored"] == 1


def test_run_verdict_ladder(dirty_df):
    ok_rule = {"id": "a", "column": "category", "check": "required", "severity": "fail", "enabled": True}
    warn_rule = {"id": "b", "column": "amount", "check": "range", "params": {"max": 50}, "severity": "warn", "enabled": True}
    assert validation.run_rules(dirty_df, [ok_rule], "t")["verdict"] == "ok"
    assert validation.run_rules(dirty_df, [ok_rule, warn_rule], "t")["verdict"] == "warn"
    assert validation.run_rules(dirty_df, [], "t")["verdict"] == "info"
    disabled = {**warn_rule, "enabled": False}
    run = validation.run_rules(dirty_df, [disabled], "t")
    assert run["results"][0]["verdict"] == "skipped"
    assert run["verdict"] == "info"


def test_detail_and_report_frames(dirty_df):
    rule = {"id": "a", "column": "invoice_no", "check": "unique", "severity": "fail", "enabled": True}
    detail = validation.detail_payload(dirty_df, rule)
    assert detail["detail_rows"] == 2
    assert detail["detail"]["columns"] == dirty_df.columns
    run = validation.run_rules(dirty_df, [rule], "t")
    report = validation.report_frame(run)
    assert report.height == 1
    assert report["verdict"][0] == "fail"


# ----------------------------------------------------------- workspace CRUD
def test_ruleset_crud_persists(workspace_with_data):
    ws = workspace_with_data
    created = ws.add_ruleset(
        {
            "title": "TX checks",
            "table": "transactions",
            "rules": [
                {"column": "amount", "check": "required"},
                {"column": None, "check": "row_count", "params": {"min": 1}},
            ],
        }
    )
    assert created["rules"][0]["severity"] == "fail"  # defaulted
    assert created["rules"][0]["enabled"] is True
    assert created["rules"][0]["id"]

    reloaded = workspaces.load_workspace(ws.id)
    assert [r["title"] for r in reloaded.rulesets] == ["TX checks"]

    ws.update_ruleset(created["id"], {"title": "TX", "table": "customers"})
    assert workspaces.load_workspace(ws.id).rulesets[0]["table"] == "customers"

    ws.remove_ruleset(created["id"])
    assert ws.rulesets == []
    with pytest.raises(WorkspaceError, match="not found"):
        ws.remove_ruleset("nope")


def test_ruleset_validation_errors(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(WorkspaceError, match="Unknown table"):
        ws.add_ruleset({"title": "x", "table": "ghost"})
    with pytest.raises(WorkspaceError, match="title"):
        ws.add_ruleset({"title": " ", "table": "transactions"})
    with pytest.raises(WorkspaceError, match="Unknown check"):
        ws.add_ruleset(
            {"title": "x", "table": "transactions", "rules": [{"check": "nope", "column": "amount"}]}
        )
    with pytest.raises(WorkspaceError, match="needs a column"):
        ws.add_ruleset(
            {"title": "x", "table": "transactions", "rules": [{"check": "required"}]}
        )


def test_not_null_aliases_are_saved_as_required(workspace_with_data):
    ws = workspace_with_data
    first = ws.add_ruleset({
        "title": "Not-null alias",
        "table": "transactions",
        "rules": [{"check": "not_null", "column": "invoice_no"}],
    })
    second = ws.add_ruleset({
        "title": "Polars alias",
        "table": "transactions",
        "rules": [{"check": "is_not_null", "column": "invoice_no"}],
    })

    assert first["rules"][0]["check"] == "required"
    assert second["rules"][0]["check"] == "required"


# --------------------------------------------------------------------- API
def test_validation_api_round_trip(workspace_with_data):
    ws = workspace_with_data
    client = TestClient(create_app())

    checks = client.get("/api/validation/checks").json()
    assert {c["id"] for c in checks} >= {"required", "allowed_values", "unique_key"}
    assert all("func" not in c for c in checks)

    created = client.post(
        f"/api/workspaces/{ws.id}/rulesets",
        json={
            "title": "TX checks",
            "table": "transactions",
            "rules": [
                {"column": "amount", "check": "numeric_sign", "params": {"mode": "positive"}},
                {"column": None, "check": "unique_key", "params": {"columns": ["invoice_no"]}},
            ],
        },
    ).json()

    run = client.post(f"/api/workspaces/{ws.id}/rulesets/{created['id']}/run", json={}).json()
    assert run["table"] == "transactions"
    assert run["verdict"] == "fail"  # invoice 1006 is duplicated
    assert run["counts"]["failed"] == 1

    # Stateless draft run + table override behave the same way.
    draft = client.post(
        f"/api/workspaces/{ws.id}/tables/transactions/validate",
        json={"rules": created["rules"]},
    ).json()
    assert draft["counts"] == run["counts"]

    detail = client.post(
        f"/api/workspaces/{ws.id}/tables/transactions/validate/detail",
        json={"rule": created["rules"][1]},
    ).json()
    assert detail["detail_rows"] == 2  # both 1006 rows

    values = client.get(
        f"/api/workspaces/{ws.id}/tables/transactions/columns/cust_id/values"
    ).json()
    assert values["values"] == ["C1", "C2", "C3"]
    assert values["truncated"] is False

    export = client.post(
        f"/api/workspaces/{ws.id}/tables/transactions/validate/export",
        json={"rule": created["rules"][0]},
    )
    assert export.status_code == 200
    assert "spreadsheetml" in export.headers["content-type"]

    report = client.post(
        f"/api/workspaces/{ws.id}/tables/transactions/validate/report",
        json={"rules": created["rules"]},
    )
    assert report.status_code == 200


def test_run_against_other_table_degrades_missing_columns(workspace_with_data):
    ws = workspace_with_data
    created = ws.add_ruleset(
        {
            "title": "TX checks",
            "table": "transactions",
            "rules": [{"column": "amount", "check": "required"}],
        }
    )
    client = TestClient(create_app())
    run = client.post(
        f"/api/workspaces/{ws.id}/rulesets/{created['id']}/run",
        json={"table": "customers"},
    ).json()
    assert run["table"] == "customers"
    assert run["results"][0]["verdict"] == "error"
    assert "amount" in run["results"][0]["error"]


def test_saved_runs_record_history(workspace_with_data):
    ws = workspace_with_data
    created = ws.add_ruleset(
        {
            "title": "TX",
            "table": "transactions",
            "rules": [{"column": "amount", "check": "numeric_sign", "params": {"mode": "positive"}}],
        }
    )
    client = TestClient(create_app())
    first = client.post(f"/api/workspaces/{ws.id}/rulesets/{created['id']}/run", json={}).json()
    assert len(first["history"]) == 1
    second = client.post(
        f"/api/workspaces/{ws.id}/rulesets/{created['id']}/run", json={"table": "customers"}
    ).json()
    assert len(second["history"]) == 2
    assert second["history"][1]["table"] == "customers"
    assert "results" not in second["history"][0]  # summary only, never row data

    reloaded = workspaces.load_workspace(ws.id)
    assert len(reloaded.rulesets[0]["runs"]) == 2

    # Draft runs are not evidence — no history entry.
    client.post(
        f"/api/workspaces/{ws.id}/tables/transactions/validate",
        json={"rules": created["rules"]},
    )
    assert len(workspaces.load_workspace(ws.id).rulesets[0]["runs"]) == 2


def test_history_is_capped(workspace_with_data):
    ws = workspace_with_data
    created = ws.add_ruleset(
        {"title": "TX", "table": "transactions", "rules": [{"column": "amount", "check": "required"}]}
    )
    run = {"run_at": "t", "table": "transactions", "rows": 6, "verdict": "ok",
           "counts": {"passed": 1, "warned": 0, "failed": 0, "errored": 0, "skipped": 0}}
    for _ in range(25):
        ws.record_run(created["id"], run)
    assert len(ws.rulesets[0]["runs"]) == ws.RUN_HISTORY_MAX


def test_report_is_multi_sheet(workspace_with_data):
    import io
    import zipfile

    ws = workspace_with_data
    rules = [
        {"id": "a", "column": "amount", "check": "range", "params": {"max": 500},
         "severity": "fail", "enabled": True},
        {"id": "b", "column": "cust_id", "check": "required", "params": {},
         "severity": "fail", "enabled": True},
    ]
    client = TestClient(create_app())
    response = client.post(
        f"/api/workspaces/{ws.id}/tables/transactions/validate/report", json={"rules": rules}
    )
    assert response.status_code == 200
    workbook_xml = zipfile.ZipFile(io.BytesIO(response.content)).read("xl/workbook.xml").decode()
    assert "Summary" in workbook_xml
    # The range rule fails (2000 and 1000 exceed 500) → its failing-rows sheet
    # exists; the passing required rule gets none.
    assert "1 amount" in workbook_xml
    assert "cust_id" not in workbook_xml


def test_workspace_without_rulesets_key_loads(workspace_with_data):
    # Rulesets are sidecars in v4, so an empty manifest has none to hydrate.
    ws = workspace_with_data
    definition = ws.definition_path.read_text(encoding="utf-8")
    assert '"rulesets"' not in definition
    assert workspaces.load_workspace(ws.id).rulesets == []
