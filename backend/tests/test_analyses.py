import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.dashboard import analyses_payload, analysis_payload
from app.main import create_app
from app.workspaces import WorkspaceError


def _library_analysis(ws) -> dict:
    return ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Duplicate invoices",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
        }
    )


def _python_analysis(ws) -> dict:
    return ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions",
            "title": "Weekend postings",
            "spec": {"code": "result = df.select(pl.len())"},
        }
    )


def test_add_update_remove_analysis(workspace_with_data):
    ws = workspace_with_data
    lib = _library_analysis(ws)
    py = _python_analysis(ws)
    assert [a["title"] for a in ws.analyses] == ["Duplicate invoices", "Weekend postings"]
    # source is inferred from kind.
    assert lib["source"] == "library"
    assert py["source"] == "ai"

    # Editing persists title, note, and — unlike a tile — the spec.
    ws.update_analysis(lib["id"], {"title": "Dupes", "note": "n"})
    ws.update_analysis(py["id"], {"spec": {"code": "result = df.head(2)"}})
    reloaded = workspaces.load_workspace(ws.id)
    assert [a["title"] for a in reloaded.analyses] == ["Dupes", "Weekend postings"]
    assert reloaded.analyses[0]["note"] == "n"
    assert reloaded.analyses[1]["spec"]["code"] == "result = df.head(2)"

    ws.remove_analysis(lib["id"])
    assert len(ws.analyses) == 1
    with pytest.raises(WorkspaceError, match="not found"):
        ws.remove_analysis("nope")


def test_add_analysis_validation(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(WorkspaceError, match="kind"):
        ws.add_analysis({"kind": "query", "table": "transactions", "title": "x"})
    with pytest.raises(WorkspaceError, match="Unknown table"):
        ws.add_analysis({"kind": "analytics", "table": "ghost", "title": "x"})
    with pytest.raises(WorkspaceError, match="title"):
        ws.add_analysis({"kind": "analytics", "table": "transactions", "title": "  "})
    with pytest.raises(WorkspaceError, match="code"):
        ws.add_analysis({"kind": "python", "title": "x", "spec": {}})
    # Clearing a python analysis's code on update is rejected too.
    py = _python_analysis(ws)
    with pytest.raises(WorkspaceError, match="code"):
        ws.update_analysis(py["id"], {"spec": {"code": "  "}})


def test_python_analysis_table_is_optional_label(workspace_with_data):
    ws = workspace_with_data
    # An unknown table on a python analysis degrades to None (label only).
    py = ws.add_analysis(
        {"kind": "python", "table": "ghost", "title": "x", "spec": {"code": "result = df"}}
    )
    assert py["table"] is None


def test_library_analysis_computes(workspace_with_data):
    ws = workspace_with_data
    payload = analysis_payload(ws, _library_analysis(ws))
    assert payload["error"] is None
    assert payload["kind"] == "analytics"
    assert payload["verdict"] in ("ok", "warn", "fail", "info")
    assert payload["frame"] is not None


def test_python_analysis_computes(workspace_with_data):
    ws = workspace_with_data
    payload = analysis_payload(ws, _python_analysis(ws))
    assert payload["error"] is None
    assert payload["kind"] == "python"
    assert payload["code"] == "result = df.select(pl.len())"
    assert payload["total_rows"] == 1


def test_broken_analysis_degrades_to_error(workspace_with_data):
    ws = workspace_with_data
    lib = _library_analysis(ws)
    ws.remove_table("transactions")
    payload = analyses_payload(ws)
    broken = next(a for a in payload["analyses"] if a["id"] == lib["id"])
    assert broken["error"] is not None
    assert "transactions" in broken["error"]


def test_analyses_api_roundtrip(workspace_with_data):
    ws = workspace_with_data
    client = TestClient(create_app())

    created = client.post(
        f"/api/workspaces/{ws.id}/analyses",
        json={
            "kind": "analytics",
            "table": "transactions",
            "title": "Dupes",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    assert created.status_code == 200
    analysis_id = created.json()["id"]
    assert created.json()["source"] == "library"

    listed = client.get(f"/api/workspaces/{ws.id}/analyses").json()
    assert len(listed["analyses"]) == 1

    renamed = client.patch(
        f"/api/workspaces/{ws.id}/analyses/{analysis_id}", json={"title": "Duplicates"}
    )
    assert renamed.json()["title"] == "Duplicates"

    assert client.delete(
        f"/api/workspaces/{ws.id}/analyses/{analysis_id}"
    ).json() == {"ok": True}
    assert client.get(f"/api/workspaces/{ws.id}/analyses").json()["analyses"] == []
