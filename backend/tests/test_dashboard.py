import json

import pytest
from fastapi.testclient import TestClient

from app import data_tests, doc_tests, findings, llm, rcm_execution, workspaces
from app.dashboard import (
    curate_rcm_tiles,
    dashboard_payload,
    engagement_status_payload,
    generate_advice,
    tile_payload,
)
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


def test_empty_workspace_dashboard_drives_onboarding():
    ws = workspaces.create_workspace("Empty engagement")
    board = dashboard_payload(ws)

    assert board["overview"]["tables"] == 0
    assert [phase["state"] for phase in board["phases"]] == [
        "not_started", "not_started", "not_started",
    ]
    assert board["actions"][0]["id"] == "import-sources"
    assert board["actions"][0]["target"]["tab"] == "data"
    assert board["ai_advice"] is None


def test_engagement_status_endpoint_is_lightweight(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    client = TestClient(create_app())

    def fail_compute(*_args, **_kwargs):
        raise AssertionError("status endpoint must not compute dashboard tiles")

    monkeypatch.setattr("app.dashboard.compute_payload", fail_compute)
    response = client.get(f"/api/workspaces/{ws.id}/dashboard/status")

    assert response.status_code == 200
    assert [phase["id"] for phase in response.json()["phases"]] == [
        "planning", "fieldwork", "report",
    ]


def test_derived_phases_can_reach_complete(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({
        "apm_markdown": "# Planning",
        "context": {"objective": "Test revenue controls.", "scope": "Recorded transactions."},
    })
    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    test = data_tests.create(ws, {
        "title": "Required transaction identifiers", "objective": "Identify missing IDs.",
        "steps": [{"label": "Scan transaction amounts.", "instruction": "Scan transaction amounts."}],
        "engine": "analytics", "table_refs": ["transactions"],
        "rcm_id": row["id"],
        "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
    })
    data_tests.run(ws, test["id"])
    rcm_execution.rollup(ws)
    data_tests.update(ws, test["id"], {
        "conclusion": "No missing transaction identifiers were identified.",
        "control_conclusion": "effective",
    })
    ws.report = {"markdown": "# Audit report\n\nThere are 0 findings."}
    ws.save()

    board = dashboard_payload(ws)
    phases = {phase["id"]: phase for phase in board["phases"]}
    assert all(phase["complete"] for phase in phases.values())
    assert phases["fieldwork"]["state"] == "complete"
    assert phases["report"]["counts"]["quality_errors"] == 0

    status = engagement_status_payload(ws)
    assert all(phase["complete"] for phase in status["phases"])


def test_rcm_dashboard_curation_scores_and_pins_four_to_six_results(workspace_with_data):
    ws = workspace_with_data
    for index in range(6):
        row = ws.add_rcm({
            "process": "Procurement", "risk": f"Vendor or approval risk {index}",
            "risk_rating": "high",
        })
        item = data_tests.create(ws, {
            "title": f"Vendor integrity result {index}",
            "objective": "Identify management-relevant vendor integrity signals.",
            "steps": [{"label": "Scan the amount population.", "instruction": "Scan the amount population."}],
            "engine": "analytics", "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
        })
        data_tests.run(ws, item["id"])

    curated = curate_rcm_tiles(ws, run_id="RUN-1")

    assert 4 <= len(curated["tiles"]) <= 6
    assert len(ws.tiles) == len(curated["tiles"])
    assert all(tile.get("rcm_id") for tile in ws.tiles)
    assert all(str(tile.get("result_ref")).startswith("datatest:") for tile in ws.tiles)
    repeated = curate_rcm_tiles(ws, run_id="RUN-2")
    assert repeated["tiles"] == []
    assert len(ws.tiles) == 6
    assert ws.planning["dashboard_curation"]["completed_at"]


def test_engagement_status_surfaces_attention_and_report_quality(workspace_with_data):
    ws = workspace_with_data
    test = doc_tests.create_test(ws, {
        "kind": "attribute", "title": "Approval review",
        "items": [{"label": "Sample 1", "state": "manual_review"}],
    })
    findings.add(ws, {"title": "Unsupported transaction"})
    ws.report = {"markdown": "# Audit report\n\n99 findings."}
    ws.save()

    phases = {
        phase["id"]: phase for phase in engagement_status_payload(ws)["phases"]
    }

    assert phases["fieldwork"]["state"] == "attention"
    assert any("manual review" in issue for issue in phases["fieldwork"]["issues"])
    assert phases["report"]["state"] == "attention"
    assert phases["report"]["counts"]["quality_errors"] > 0
    assert "draft_findings" not in phases["report"]["counts"]


def test_terminal_document_test_result_statuses_are_not_marked_incomplete(
    workspace_with_data,
):
    ws = workspace_with_data
    for status in ("completed_no_exception", "completed_with_exception"):
        doc_tests.create_test(ws, {
            "kind": "qa",
            "title": f"{status} test",
            "status": status,
            "items": [{"label": "Settled item", "state": "confirmed"}],
        })

    phases = {phase["id"]: phase for phase in engagement_status_payload(ws)["phases"]}

    assert not any(
        "document test(s) are incomplete" in issue
        for issue in phases["fieldwork"]["issues"]
    )


def test_dashboard_advice_is_metadata_only_cached_and_marked_stale(
    workspace_with_data, monkeypatch,
):
    ws = workspace_with_data
    ws.report = {"markdown": "PRIVATE REPORT BODY 1001 C1"}
    ws.save()
    captured = {}

    def fake_chat(messages, tools=None, temperature=0.0, profile="assistant"):
        captured["messages"] = messages
        captured["profile"] = profile
        return {"content": json.dumps({"suggestions": [
            {"title": "Define the audit scope", "reason": "Planning has not started.",
             "priority": "high", "tab": "planning"},
            {"title": "Bad destination", "reason": "Must be discarded.",
             "priority": "high", "tab": "external"},
        ]})}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {
        "configured": True, "provider": "fake", "model": "audit-model",
    })
    advice = generate_advice(ws)

    outbound = json.dumps(captured["messages"])
    assert captured["profile"] == "agent"
    assert "PRIVATE REPORT BODY" not in outbound
    assert '"C1"' not in outbound
    assert len(advice["items"]) == 1
    assert advice["items"][0]["target"]["tab"] == "planning"
    assert workspaces.load_workspace(ws.id).dashboard_advice["model"] == "audit-model"
    assert dashboard_payload(ws)["ai_advice"]["stale"] is False

    ws.update_planning({"context": {"objective": "Changed after advice"}})
    assert dashboard_payload(ws)["ai_advice"]["stale"] is True


def test_dashboard_advice_api(workspace_with_data, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: {
        "content": '{"suggestions": []}',
    })
    monkeypatch.setattr(llm, "agent_status", lambda: {
        "configured": True, "provider": "fake", "model": "fake",
    })
    client = TestClient(create_app())
    response = client.post(f"/api/workspaces/{workspace_with_data.id}/dashboard/advice")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_dashboard_advice_does_not_overwrite_edits_made_during_model_call(
    workspace_with_data, monkeypatch,
):
    ws = workspace_with_data

    def fake_chat(*args, **kwargs):
        concurrent = workspaces.load_workspace(ws.id)
        concurrent.update_planning({"context": {"objective": "Saved during advice"}})
        return {"content": '{"suggestions": []}'}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {
        "configured": True, "provider": "fake", "model": "fake",
    })
    advice = generate_advice(ws)
    reloaded = workspaces.load_workspace(ws.id)

    assert reloaded.planning["context"]["objective"] == "Saved during advice"
    assert advice["stale"] is True
    assert reloaded.dashboard_advice["items"] == []
