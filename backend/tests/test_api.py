import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import main, workspaces
from app.main import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    """A client for an app built with a production frontend build present,
    which is the only case where the SPA history fallback is mounted."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>shell</title>", "utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIST", dist)
    return TestClient(create_app())


@pytest.fixture
def ws_id(client, transactions_df) -> str:
    response = client.post("/api/workspaces", json={"name": "API Test"})
    assert response.status_code == 200
    workspace_id = response.json()["id"]
    upload = client.post(
        f"/api/workspaces/{workspace_id}/tables",
        files={"files": ("transactions.csv", transactions_df.write_csv().encode(), "text/csv")},
    )
    assert upload.status_code == 200
    return workspace_id


def test_workspace_lifecycle(client, ws_id):
    listed = client.get("/api/workspaces").json()
    assert [w["id"] for w in listed] == [ws_id]

    detail = client.get(f"/api/workspaces/{ws_id}").json()
    assert detail["tables"][0]["name"] == "transactions"
    assert detail["tables"][0]["rows"] == 6

    assert client.delete(f"/api/workspaces/{ws_id}").json() == {"ok": True}
    assert client.get(f"/api/workspaces/{ws_id}").status_code == 400


def test_schema_preview_profile(client, ws_id):
    schema = client.get(f"/api/workspaces/{ws_id}/tables/transactions/schema").json()
    kinds = {c["name"]: c["kind"] for c in schema["columns"]}
    assert kinds["amount"] == "numeric"

    preview = client.get(f"/api/workspaces/{ws_id}/tables/transactions/preview").json()
    assert preview["total_rows"] == 6
    assert len(preview["rows"]) == 6

    profile = client.get(f"/api/workspaces/{ws_id}/tables/transactions/profile").json()
    assert profile["rows"] == 6
    amount = next(p for p in profile["column_profiles"] if p["name"] == "amount")
    assert amount["inferred_type"] == "numeric"


def test_rename_table_api_rebinds_workspace_items(client, ws_id):
    ws = workspaces.load_workspace(ws_id)
    ws.add_analysis({"kind": "analytics", "table": "transactions", "title": "TX",
                     "spec": {"test": "sign_scan", "params": {"column": "amount"}}})

    response = client.patch(
        f"/api/workspaces/{ws_id}/tables/transactions",
        json={"name": "ledger"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["renamed"]["name"] == "ledger"
    assert body["renamed"]["updated"]["analyses"] == 1
    assert [t["name"] for t in body["workspace"]["tables"]] == ["ledger"]
    assert workspaces.load_workspace(ws_id).analyses[0]["table"] == "ledger"


def test_query_and_export(client, ws_id):
    spec = {
        "group_by": ["cust_id"],
        "aggs": [{"column": "amount", "func": "sum"}],
        "sort": [{"column": "amount_sum", "desc": True}],
    }
    result = client.post(
        f"/api/workspaces/{ws_id}/tables/transactions/query", json=spec
    ).json()
    assert result["total_rows"] == 3

    export = client.post(
        f"/api/workspaces/{ws_id}/tables/transactions/query/export", json=spec
    )
    assert export.status_code == 200
    assert export.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert len(export.content) > 500


def test_analytics_run_and_errors(client, ws_id):
    registry = client.get("/api/analytics").json()
    assert any(t["id"] == "duplicates" for t in registry)

    result = client.post(
        f"/api/workspaces/{ws_id}/tables/transactions/analytics/duplicates",
        json={"columns": ["invoice_no"]},
    ).json()
    assert result["verdict"] == "fail"
    assert result["detail_rows"] == 2

    bad = client.post(
        f"/api/workspaces/{ws_id}/tables/transactions/analytics/threshold_check",
        json={"column": "amount"},
    )
    assert bad.status_code == 400
    assert "threshold value is required" in bad.json()["detail"]


def test_join_via_api(client, ws_id):
    customers = pl.DataFrame({"id": ["C1", "C2", "C3"], "customer": ["A", "B", "G"]})
    client.post(
        f"/api/workspaces/{ws_id}/tables",
        files={"files": ("customers.csv", customers.write_csv().encode(), "text/csv")},
    )
    joined = client.post(
        f"/api/workspaces/{ws_id}/joins",
        json={
            "name": "enriched",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        },
    )
    assert joined.status_code == 200
    names = [t["name"] for t in joined.json()["tables"]]
    assert "enriched" in names

    bad = client.post(
        f"/api/workspaces/{ws_id}/joins",
        json={"name": "x", "left": "transactions", "right": "ghost", "how": "left"},
    )
    assert bad.status_code == 400


def test_unknown_api_route_404s_instead_of_serving_the_spa(spa_client, ws_id):
    """The history fallback must not answer /api with the SPA shell — a 200
    text/html body gets cached and the frontend then parses markup as JSON."""
    missing = spa_client.get(f"/api/workspaces/{ws_id}/does-not-exist")

    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")
    assert "detail" in missing.json()


def test_spa_fallback_still_serves_the_shell_for_app_routes(spa_client):
    shell = spa_client.get("/workspaces/anything/deep/link")

    assert shell.status_code == 200
    assert shell.headers["content-type"].startswith("text/html")
