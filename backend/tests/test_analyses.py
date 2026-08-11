import json

import pytest
from fastapi.testclient import TestClient

from app import analysis_results, workspaces
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


def test_add_analysis_derives_the_tests_natural_chart(workspace_with_data):
    """An analytics test that suggests a chart gets it as its saved viz —
    not the "table" default — the moment it is created."""
    ws = workspace_with_data
    weekend = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Weekend postings",
            "spec": {"test": "weekend_activity", "params": {"date_column": "tx_date"}},
        }
    )
    assert weekend["viz"] == {"type": "bar", "x": "weekday", "y": ["count"]}

    completeness = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Completeness",
            "spec": {"test": "completeness", "params": {"columns": ["amount"]}},
        }
    )
    assert completeness["viz"] == {"type": "bar", "x": "column", "y": ["missing"]}


def test_add_analysis_keeps_the_table_default_when_the_test_has_no_chart(
    workspace_with_data,
):
    """duplicates has no natural chart — it stays the plain table default,
    and a broken spec degrades the same way rather than blocking the save."""
    ws = workspace_with_data
    duplicates = _library_analysis(ws)  # test 'duplicates', no viz in the registry
    assert duplicates["viz"] == {"type": "table"}

    broken = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Broken spec",
            "spec": {"test": "weekend_activity", "params": {"date_column": "no_such_column"}},
        }
    )
    assert broken["viz"] == {"type": "table"}


def test_regenerating_a_definition_refreshes_its_chart(workspace_with_data):
    """A definition an executor regenerates with a different test must not
    keep the previous spec's chart preference."""
    ws = workspace_with_data
    analysis = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Duplicate invoices",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
        }
    )
    assert analysis["viz"] == {"type": "table"}

    # Mirrors what execute_analysis_definitions does on an update: derive a
    # fresh viz for the new spec and only overwrite when one exists.
    new_spec = {"test": "weekend_activity", "params": {"date_column": "tx_date"}}
    refreshed = ws._analytics_default_viz("transactions", new_spec)
    assert refreshed == {"type": "bar", "x": "weekday", "y": ["count"]}


def test_editing_a_spec_refreshes_its_chart_unless_viz_is_explicit(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)  # 'duplicates' — no chart
    assert analysis["viz"] == {"type": "table"}

    # Changing the test to one with a natural chart, without an explicit viz:
    # the server derives it, the same as a fresh save would.
    ws.update_analysis(
        analysis["id"],
        {"spec": {"test": "weekend_activity", "params": {"date_column": "tx_date"}}},
    )
    assert ws.analyses[0]["viz"] == {"type": "bar", "x": "weekday", "y": ["count"]}

    # An explicit viz in the same request is honored over the derived one.
    ws.update_analysis(
        analysis["id"],
        {"spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}}, "viz": {"type": "table"}},
    )
    assert ws.analyses[0]["viz"] == {"type": "table"}

    # Re-saving the unchanged spec (a title edit) leaves viz untouched.
    ws.update_analysis(analysis["id"], {"viz": {"type": "bar", "x": "band", "y": ["count"]}})
    ws.update_analysis(analysis["id"], {"title": "Renamed"})
    assert ws.analyses[0]["viz"] == {"type": "bar", "x": "band", "y": ["count"]}


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
        "exception": 1,
        "unusual": 0,
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
    executed = run_analysis(ws, analysis, run_id="run-summary")
    result = executed.result
    assert result["verdict"] == "warn"
    assert result["verdict_text"] == "2 potential exception rows returned."
    assert result["input_sha1"] == analysis_input_sha1(ws, analysis)
    # An exception_rows policy makes the returned rows the flagged rows, so the
    # execution carries evidence a reviewer can read back.
    assert result["exception_count"] == 2
    assert executed.evidence is not None
    assert len(executed.evidence["frame"]["rows"]) == 2


def test_broken_analysis_degrades_to_error(workspace_with_data):
    ws = workspace_with_data
    lib = _library_analysis(ws)
    ws.remove_table("transactions")
    broken = analysis_payload(ws, lib)
    assert broken["error"] is not None
    assert "transactions" in broken["error"]


def test_listing_describes_analyses_without_executing_them(workspace_with_data):
    """The rail costs no compute: definitions and recorded outcomes only."""
    ws = workspace_with_data
    lib = _library_analysis(ws)
    py = _python_analysis(ws)
    ws.remove_table("transactions")

    listed = analyses_payload(ws)["analyses"]

    # A spec that can no longer run does not raise, and does not report a live
    # error either — nothing was executed to discover one.
    assert [item["id"] for item in listed] == [lib["id"], py["id"]]
    for item in listed:
        assert "frame" not in item
        assert "code" not in item
        assert "verdict" not in item
        assert item["state"] == "not_run"
        assert item["classification"] == "not_run"
    # The definition still travels, so the editor can open without a second call.
    assert listed[0]["spec"]["test"] == "duplicates"


def test_listing_reports_the_recorded_outcome(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    executed = analysis_results.execute_analysis(
        ws, analysis, run_id=analysis_results.manual_run_id()
    )
    analysis_results.record_analysis_result(
        ws, analysis["id"], executed.result, evidence=executed.evidence
    )

    listed = analyses_payload(workspaces.load_workspace(ws.id))["analyses"][0]

    assert listed["state"] == "current"
    assert listed["classification"] in {"clear", "unusual", "exception", "informational"}
    assert listed["last_result"]["status"] == "ok"


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


def test_execute_endpoint_records_the_same_contract_as_a_workflow_run(
    workspace_with_data,
):
    """An auditor's Run is as durable, and as current, as the agent's."""
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    client = TestClient(create_app())

    response = client.post(
        f"/api/workspaces/{ws.id}/analyses/{analysis['id']}/execute"
    )

    assert response.status_code == 200
    body = response.json()
    # The response carries the rows for display *and* the record that persisted.
    assert body["frame"] is not None
    assert body["state"] == "current"
    assert body["last_result"]["status"] == "ok"
    assert body["last_result"]["run_id"].startswith(analysis_results.MANUAL_RUN_PREFIX)

    fresh = workspaces.load_workspace(ws.id)
    stored = fresh.analyses[0]["last_result"]
    assert stored["result_sha1"] == body["last_result"]["result_sha1"]
    assert stored["input_sha1"] == analysis_input_sha1(fresh, fresh.analyses[0])
    # The durable record is the bounded contract and nothing else: result rows
    # live in the evidence sidecar, never on the definition.
    assert set(stored) == set(
        run_analysis(fresh, fresh.analyses[0], run_id="run-1").result
    )
    assert "frame" not in stored and "exceptions" not in stored
    assert analyses_summary_payload(fresh)["counts"]["not_run"] == 0


def test_manual_execution_does_not_take_ownership_of_an_agent_analysis(
    workspace_with_data,
):
    ws = workspace_with_data
    analysis = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Agent duplicates",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
            "agent_run_id": "run-agent",
        }
    )
    assert analysis["created_by"] == "agent"

    analysis_results.execute_and_record(ws, analysis["id"])

    fresh = workspaces.load_workspace(ws.id)
    assert fresh.analyses[0]["created_by"] == "agent"


def test_execute_all_brings_stale_and_unrun_procedures_current(workspace_with_data):
    ws = workspace_with_data
    first = _library_analysis(ws)
    second = _python_analysis(ws)
    client = TestClient(create_app())

    response = client.post(f"/api/workspaces/{ws.id}/analyses/execute", json={})

    assert response.status_code == 200
    body = response.json()
    assert {item["analysis_id"] for item in body["executed"]} == {
        first["id"], second["id"],
    }
    assert all(item["ok"] for item in body["executed"])
    assert body["summary"]["counts"]["not_run"] == 0

    # Nothing is stale now, so a second sweep has nothing to do.
    again = client.post(f"/api/workspaces/{ws.id}/analyses/execute", json={})
    assert again.json()["executed"] == []


def test_execute_all_reports_one_broken_spec_without_blocking_the_others(
    workspace_with_data,
):
    ws = workspace_with_data
    good = _library_analysis(ws)
    broken = ws.add_analysis(
        {"kind": "python", "title": "Broken", "spec": {"code": "result = nope"}}
    )
    client = TestClient(create_app())

    body = client.post(f"/api/workspaces/{ws.id}/analyses/execute", json={}).json()

    outcomes = {item["analysis_id"]: item for item in body["executed"]}
    assert outcomes[good["id"]]["ok"] is True
    # A spec that raises is a recorded execution error, not a failed request:
    # the auditor needs to see it on the procedure.
    assert outcomes[broken["id"]]["ok"] is True
    assert outcomes[broken["id"]]["result"]["status"] == "error"
    assert body["summary"]["counts"]["errors"] == 1


def test_detail_endpoint_computes_one_analysis(workspace_with_data):
    ws = workspace_with_data
    analysis = _python_analysis(ws)
    client = TestClient(create_app())

    body = client.get(f"/api/workspaces/{ws.id}/analyses/{analysis['id']}").json()

    assert body["code"] == "result = df.select(pl.len())"
    assert body["total_rows"] == 1
    assert body["state"] == "not_run"
    # Opening a procedure shows what it returns; it does not record a result.
    assert "last_result" not in body
    assert workspaces.load_workspace(ws.id).analyses[0].get("last_result") is None


def test_outcome_policy_is_validated_and_invalidates_its_result(workspace_with_data):
    ws = workspace_with_data
    analysis = ws.add_analysis(
        {
            "kind": "python",
            "title": "Weekend postings",
            "spec": {"code": "result = df.head(2)"},
            "outcome_policy": {"mode": "exception_rows"},
        }
    )
    analysis_results.execute_and_record(ws, analysis["id"])
    assert ws.analyses[0]["last_result"]["verdict"] == "warn"

    with pytest.raises(WorkspaceError, match="outcome policy"):
        ws.add_analysis(
            {
                "kind": "python",
                "title": "x",
                "spec": {"code": "result = df"},
                "outcome_policy": {"mode": "whatever"},
            }
        )

    # Re-declaring the same policy is not a change and keeps the conclusion.
    ws.update_analysis(analysis["id"], {"outcome_policy": {"mode": "exception_rows"}})
    assert ws.analyses[0].get("last_result") is not None
    # Changing it changes what the rows mean, so the old conclusion is dropped.
    ws.update_analysis(analysis["id"], {"outcome_policy": {"mode": "informational"}})
    assert ws.analyses[0].get("last_result") is None


def test_resaving_an_unchanged_definition_keeps_its_result(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis_results.execute_and_record(ws, analysis["id"])
    spec = dict(ws.analyses[0]["spec"])

    ws.update_analysis(analysis["id"], {"title": "Renamed", "spec": spec})
    assert ws.analyses[0].get("last_result") is not None

    ws.update_analysis(
        analysis["id"], {"spec": {"test": "duplicates", "params": {"columns": ["vendor"]}}}
    )
    assert ws.analyses[0].get("last_result") is None


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


# --------------------------------------------------------- exception evidence
# A procedure that concludes "2 rows are duplicated" is not reviewable until an
# auditor can see *which* rows. Every exception-producing analytics test already
# computes that frame; these cover it reaching durable state, staying bounded,
# staying attached to the exact result it supports, and being discarded with it.
def _evidence_file(ws, analysis_id):
    return workspaces.analysis_evidence_path(ws.root, analysis_id)


def test_execution_records_the_rows_it_flagged(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)

    executed = analysis_results.execute_and_record(ws, analysis["id"])

    # invoice_no 1006 appears twice in the fixture.
    assert executed.result["exception_count"] == 2
    assert executed.result["exception_rows_retained"] == 2
    stored = json.loads(_evidence_file(ws, analysis["id"]).read_text(encoding="utf-8"))
    assert stored["result_sha1"] == executed.result["result_sha1"]
    flagged = stored["frame"]
    invoices = [
        row[flagged["columns"].index("invoice_no")] for row in flagged["rows"]
    ]
    assert invoices == [1006, 1006]

    # The definition itself never carries rows.
    fresh = workspaces.load_workspace(ws.id)
    definition = fresh.analyses[0]
    assert "frame" not in json.dumps(definition["last_result"])
    assert "1006" not in json.dumps(definition["last_result"])


def test_a_procedure_that_flags_nothing_stores_no_evidence(workspace_with_data):
    ws = workspace_with_data
    clean = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Amounts are populated",
            "spec": {"test": "completeness", "params": {"columns": ["amount"]}},
        }
    )

    executed = analysis_results.execute_and_record(ws, clean["id"])

    assert executed.result["exception_count"] == 0
    assert not _evidence_file(ws, clean["id"]).exists()
    assert analysis_results.read_exception_evidence(ws, ws.analyses[0]) is None


def test_a_rerun_that_clears_the_exception_discards_its_evidence(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis_results.execute_and_record(ws, analysis["id"])
    assert _evidence_file(ws, analysis["id"]).exists()

    # The same definition, re-recorded with a result that flags nothing.
    clean = dict(analysis["last_result"])
    clean["exception_count"] = 0
    analysis_results.record_analysis_result(ws, analysis["id"], clean, evidence=None)

    assert not _evidence_file(ws, analysis["id"]).exists()


def test_evidence_from_a_superseded_result_is_not_read_back(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis_results.execute_and_record(ws, analysis["id"])
    path = _evidence_file(ws, analysis["id"])

    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["result_sha1"] = "a-conclusion-that-is-no-longer-on-the-definition"
    path.write_text(json.dumps(stored), encoding="utf-8")

    fresh = workspaces.load_workspace(ws.id)
    assert analysis_results.read_exception_evidence(fresh, fresh.analyses[0]) is None


def test_editing_the_definition_discards_the_rows_it_flagged(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis_results.execute_and_record(ws, analysis["id"])
    assert _evidence_file(ws, analysis["id"]).exists()

    ws.update_analysis(
        analysis["id"],
        {"spec": {"test": "duplicates", "params": {"columns": ["cust_id"]}}},
    )

    # The conclusion went; so did the evidence that supported it.
    assert ws.analyses[0].get("last_result") is None
    assert not _evidence_file(ws, analysis["id"]).exists()


def test_removing_an_analysis_discards_the_rows_it_flagged(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis_results.execute_and_record(ws, analysis["id"])
    path = _evidence_file(ws, analysis["id"])
    assert path.exists()

    ws.remove_analysis(analysis["id"])

    assert not path.exists()


def test_retained_rows_are_capped_and_the_cap_is_reported(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    monkeypatch.setattr(analysis_results, "EXCEPTION_ROWS", 1)

    executed = analysis_results.execute_and_record(ws, analysis["id"])

    # The count is the population; the retained slice is what was kept. A
    # truncated sidecar must never read as the whole set.
    assert executed.result["exception_count"] == 2
    assert executed.result["exception_rows_retained"] == 1
    stored = json.loads(_evidence_file(ws, analysis["id"]).read_text(encoding="utf-8"))
    assert len(stored["frame"]["rows"]) == 1


def test_exceptions_endpoint_reads_the_record_without_recomputing(workspace_with_data):
    ws = workspace_with_data
    analysis = _library_analysis(ws)
    analysis_results.execute_and_record(ws, analysis["id"])
    client = TestClient(create_app())

    # Remove the frame the spec needs. A recompute is now impossible, which is
    # exactly what proves this endpoint is a read of what was recorded.
    workspaces.load_workspace(ws.id).remove_table("transactions")

    response = client.get(
        f"/api/workspaces/{ws.id}/analyses/{analysis['id']}/exceptions"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exception_count"] == 2
    assert body["retained"] == 2
    assert body["run_id"].startswith(analysis_results.MANUAL_RUN_PREFIX)
    assert len(body["frame"]["rows"]) == 2

    # The detail endpoint, by contrast, has to recompute and now cannot.
    assert client.get(
        f"/api/workspaces/{ws.id}/analyses/{analysis['id']}"
    ).json()["error"] is not None
