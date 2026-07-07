import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.dashboard import dashboard_payload, tile_payload
from app.main import create_app
from app.workspaces import WorkspaceError


def _query_tile(ws) -> dict:
    return ws.add_tile(
        {
            "kind": "query",
            "table": "transactions",
            "title": "Amount by customer",
            "spec": {
                "group_by": ["cust_id"],
                "aggs": [{"column": "amount", "func": "sum"}],
                "sort": [{"column": "amount_sum", "desc": True}],
            },
            "viz": {"type": "bar", "x": "cust_id", "y": ["amount_sum"]},
            "note": "Concentration check",
        }
    )


def test_add_update_move_remove_tile(workspace_with_data):
    ws = workspace_with_data
    tile = _query_tile(ws)
    second = ws.add_tile(
        {"kind": "analytics", "table": "transactions", "title": "Dupes",
         "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}}}
    )
    assert [t["title"] for t in ws.tiles] == ["Amount by customer", "Dupes"]

    ws.update_tile(tile["id"], {"title": "Renamed", "note": "n"})
    ws.update_tile(second["id"], {"move": -1})
    reloaded = workspaces.load_workspace(ws.id)
    assert [t["title"] for t in reloaded.tiles] == ["Dupes", "Renamed"]
    assert reloaded.tiles[1]["note"] == "n"

    ws.remove_tile(tile["id"])
    assert len(ws.tiles) == 1
    with pytest.raises(WorkspaceError, match="not found"):
        ws.remove_tile("nope")


def test_add_tile_validation(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(WorkspaceError, match="kind"):
        ws.add_tile({"kind": "chart", "table": "transactions", "title": "x"})
    with pytest.raises(WorkspaceError, match="Unknown table"):
        ws.add_tile({"kind": "query", "table": "ghost", "title": "x"})
    with pytest.raises(WorkspaceError, match="title"):
        ws.add_tile({"kind": "query", "table": "transactions", "title": "  "})


def test_query_tile_computes(workspace_with_data):
    ws = workspace_with_data
    tile = _query_tile(ws)
    payload = tile_payload(ws, tile)
    assert payload["error"] is None
    assert payload["total_rows"] == 3
    assert payload["frame"]["columns"] == ["cust_id", "amount_sum"]
    assert payload["viz"]["type"] == "bar"


def test_pivot_tile_computes(workspace_with_data):
    ws = workspace_with_data
    tile = ws.add_tile(
        {
            "kind": "pivot",
            "table": "transactions",
            "title": "Amount by customer",
            "spec": {
                "rows": ["cust_id"],
                "columns": [],
                "values": [{"column": "amount", "func": "sum"}],
                "totals": True,
            },
            "viz": {"type": "bar", "x": "cust_id", "y": ["amount_sum"]},
        }
    )
    payload = tile_payload(ws, tile)
    assert payload["error"] is None
    # Wide cross-tab, no grand-total row appended (chartable): 3 customers.
    assert payload["total_rows"] == 3
    assert payload["frame"]["columns"] == ["cust_id", "amount_sum"]
    assert payload["viz"]["type"] == "bar"


def test_analytics_tile_uses_suggested_viz(workspace_with_data):
    ws = workspace_with_data
    tile = ws.add_tile(
        {"kind": "analytics", "table": "transactions", "title": "Monthly",
         "spec": {"test": "period_compare",
                  "params": {"date_column": "tx_date", "value_column": "amount", "period": "month"}}}
    )
    payload = tile_payload(ws, tile)
    assert payload["error"] is None
    assert payload["viz"] == {"type": "line", "x": "period", "y": ["amount_sum"]}
    assert payload["verdict"] in ("info", "warn")
    assert payload["frame"]["columns"][0] == "period"


def test_broken_tile_degrades_to_error(workspace_with_data):
    ws = workspace_with_data
    tile = ws.add_tile(
        {"kind": "query", "table": "customers", "title": "Will break", "spec": {}}
    )
    ws.remove_table("customers")
    payload = dashboard_payload(ws)
    broken = next(t for t in payload["tiles"] if t["id"] == tile["id"])
    assert broken["error"] is not None
    assert "customers" in broken["error"]


def test_tiles_api_roundtrip(workspace_with_data):
    ws = workspace_with_data
    client = TestClient(create_app())

    created = client.post(
        f"/api/workspaces/{ws.id}/tiles",
        json={
            "kind": "query", "table": "transactions", "title": "By customer",
            "spec": {"group_by": ["cust_id"], "aggs": [{"func": "count"}]},
            "viz": {"type": "pie", "x": "cust_id", "y": ["row_count"]},
        },
    )
    assert created.status_code == 200
    tile_id = created.json()["id"]
    assert created.json()["frame"]["columns"] == ["cust_id", "row_count"]

    board = client.get(f"/api/workspaces/{ws.id}/dashboard").json()
    assert len(board["tiles"]) == 1

    renamed = client.patch(
        f"/api/workspaces/{ws.id}/tiles/{tile_id}", json={"title": "Customers"}
    )
    assert renamed.json()["title"] == "Customers"

    assert client.delete(f"/api/workspaces/{ws.id}/tiles/{tile_id}").json() == {"ok": True}
    assert client.get(f"/api/workspaces/{ws.id}/dashboard").json()["tiles"] == []
