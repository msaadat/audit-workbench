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


def test_workspace_without_rulesets_key_loads(workspace_with_data):
    # Pre-existing workspace.json files predate the rulesets collection.
    ws = workspace_with_data
    definition = ws.definition_path.read_text(encoding="utf-8")
    assert '"rulesets"' in definition
    import json

    data = json.loads(definition)
    del data["rulesets"]
    ws.definition_path.write_text(json.dumps(data), encoding="utf-8")
    assert workspaces.load_workspace(ws.id).rulesets == []
