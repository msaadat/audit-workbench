import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.analysis_results import analyses_summary_payload, analysis_input_sha1
from app.agent.executors.analysis import run_analysis
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


def test_analysis_payload_includes_persisted_workflow_result(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis["last_result"] = {
        "run_id": "run-123",
        "executed_at": "2026-07-28T16:22:41+00:00",
        "status": "ok",
        "verdict": "warn",
        "verdict_text": "2 duplicate values",
        "row_count": 4,
        "column_count": 2,
        "stat_count": 1,
        "stats": [{"label": "Rows", "value": "4"}],
    }

    payload = analysis_payload(ws, analysis)

    assert payload["last_result"] == analysis["last_result"]
    assert analyses_payload(ws)["analyses"][0]["last_result"] == analysis["last_result"]


def test_analysis_summary_projects_only_bounded_outcomes(workspace_with_data):
    ws = workspace_with_data
    flagged = _library_analysis(ws)
    clear = _python_analysis(ws)
    flagged["last_result"] = {
        "run_id": "run-123",
        "executed_at": "2026-07-28T16:22:41+00:00",
        "status": "ok",
        "verdict": "fail",
        "verdict_text": "2 duplicate values",
        "row_count": 2,
        "stats": [{"label": "Duplicates", "value": "2"}],
        "input_sha1": analysis_input_sha1(ws, flagged),
        "result_sha1": "result-1",
    }
    clear["last_result"] = {
        "run_id": "run-124",
        "executed_at": "2026-07-28T16:22:42+00:00",
        "status": "ok",
        "verdict": "ok",
        "verdict_text": "No potential exception rows returned.",
        "row_count": 0,
        "stats": [],
        "input_sha1": analysis_input_sha1(ws, clear),
        "result_sha1": "result-2",
    }

    payload = analyses_summary_payload(ws)

    assert payload["counts"] == {
        "needs_review": 1,
        "errors": 0,
        "clear": 1,
        "informational": 0,
        "stale": 0,
        "not_run": 0,
    }
    item = next(item for item in payload["items"] if item["analysis_id"] == flagged["id"])
    assert item["classification"] == "exception"
    assert item["stats"] == [{"label": "Duplicates", "value": "2"}]
    assert "spec" not in item and "code" not in item and "frame" not in item


def test_analysis_summary_marks_changed_inputs_stale_and_spec_edits_clear_result(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis["last_result"] = {
        "run_id": "run-123",
        "executed_at": "2026-07-28T16:22:41+00:00",
        "status": "ok",
        "verdict": "warn",
        "verdict_text": "Possible duplicates",
        "row_count": 2,
        "stats": [],
        "input_sha1": "outdated",
        "result_sha1": "result-1",
    }
    assert analyses_summary_payload(ws)["counts"]["stale"] == 1

    ws.update_analysis(analysis["id"], {"spec": {"test": "duplicates", "params": {"columns": ["amount"]}}})
    assert "last_result" not in ws._analysis(analysis["id"])


def test_python_exception_policy_turns_nonempty_result_into_a_review_signal(workspace_with_data):
    ws = workspace_with_data
    analysis = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions",
            "title": "Potential exception rows",
            "spec": {"code": "result = df.head(2)"},
            "outcome_policy": {"mode": "exception_rows"},
        }
    )
    result = run_analysis(ws, analysis, run_id="run-summary")
    assert result["verdict"] == "warn"
    assert result["verdict_text"] == "2 potential exception row(s) returned."
    assert result["input_sha1"] == analysis_input_sha1(ws, analysis)


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
    summary = client.get(f"/api/workspaces/{ws.id}/analyses/summary")
    assert summary.status_code == 200
    assert summary.json()["counts"]["not_run"] == 1

    renamed = client.patch(
        f"/api/workspaces/{ws.id}/analyses/{analysis_id}", json={"title": "Duplicates"}
    )
    assert renamed.json()["title"] == "Duplicates"

    assert client.delete(
        f"/api/workspaces/{ws.id}/analyses/{analysis_id}"
    ).json() == {"ok": True}
    assert client.get(f"/api/workspaces/{ws.id}/analyses").json()["analyses"] == []


def test_pin_analysis_from_summary_creates_dashboard_tile(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis["last_result"] = {"run_id": "run-pin"}
    ws.save()
    client = TestClient(create_app())

    response = client.post(f"/api/workspaces/{ws.id}/analyses/{analysis['id']}/pin", json={})

    assert response.status_code == 200
    fresh = workspaces.load_workspace(ws.id)
    assert len(fresh.tiles) == 1
    assert fresh.tiles[0]["analysis_id"] == analysis["id"]
    assert fresh.tiles[0]["result_ref"] == f"analysis:{analysis['id']}:run-pin"
    assert response.json()["error"] is None
