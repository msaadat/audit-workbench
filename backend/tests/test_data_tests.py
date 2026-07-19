import json

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import dashboard, data_tests, findings, rcm_execution, report, workspaces
from app.main import create_app


def _planned(ws, *, method="data_analytics"):
    row = ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Transactions may bypass controls",
            "risk_rating": "high",
            "control": "Automated validation",
        }
    )
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Population control test",
            "objective": "Identify transactions that bypass the control.",
            "method": method,
            "steps": ["Analyze the full population."],
        },
    )
    return row, planned


def _analytics_payload(row, planned):
    return {
        "title": "Duplicate invoice numbers",
        "objective": "Identify repeated invoice numbers.",
        "rcm_id": row["id"],
        "planned_test_id": planned["id"],
        "engine": "analytics",
        "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    }


def test_create_validates_but_does_not_count_as_execution(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws)

    item = data_tests.create(ws, _analytics_payload(row, planned))

    assert item["status"] == "ready"
    assert item["last_run"] is None
    assert item["runs"] == []
    assert f"datatest:{item['id']}" in planned["execution_refs"]
    assert not (ws.root / "DataTestResults").exists()


def test_analytics_run_is_durable_reopenable_and_history_preserving(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws)
    item = data_tests.create(ws, _analytics_payload(row, planned))

    first = data_tests.run(ws, item["id"])

    assert first["status"] == "completed_with_exception"
    assert first["exception_count"] == 2
    assert first["dataset_fingerprints"]["transactions"]
    assert first["result_sha1"]
    assert data_tests.load_result(ws, item["id"], first["id"]) == first
    assert item["last_run"]["id"] == first["id"]
    assert len(item["runs"]) == 1

    second = data_tests.run(ws, item["id"])
    assert second["id"] != first["id"]
    assert len(item["runs"]) == 2
    assert data_tests.load_result(ws, item["id"], first["id"])["result_sha1"] == first["result_sha1"]


def test_validation_and_polars_engines_persist_bounded_exception_results(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws, method="hybrid")
    validation_test = data_tests.create(
        ws,
        {
            "title": "Positive amounts",
            "objective": "Identify non-positive transaction amounts.",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "engine": "validation",
            "table_refs": ["transactions"],
            "spec": {
                "rules": [
                    {
                        "id": "RULE-1",
                        "column": "amount",
                        "check": "range",
                        "params": {"max": 1000},
                        "severity": "warn",
                    }
                ]
            },
        },
    )
    validation_run = data_tests.run(ws, validation_test["id"])
    assert validation_run["verdict"] == "warn"
    assert validation_run["exception_count"] == 1
    assert validation_run["semantic_valid"] is True
    assert not any("entirely null" in issue for issue in validation_run["semantic_issues"])
    assert validation_run["exception_frame"]["rows"][0][-1] == "RULE-1"

    polars_test = data_tests.create(
        ws,
        {
            "title": "Large transactions",
            "objective": "Identify transactions greater than 500.",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "engine": "polars",
            "table_refs": ["transactions"],
            "spec": {"code": "result = transactions.filter(pl.col('amount') > 500)"},
        },
    )
    polars_run = data_tests.run(ws, polars_test["id"])
    assert polars_run["exception_count"] == 2
    assert polars_run["exception_frame"]["columns"] == ws.get_frame("transactions").columns


def test_zero_match_join_is_rejected_as_semantically_invalid(workspace_with_data):
    ws = workspace_with_data
    roles = pl.DataFrame({"designation": ["Finance", "Manager"], "limit": [1000, 5000]})
    ws.add_table("authority.csv", roles.write_csv().encode())
    ws.add_join(
        {
            "name": "invalid_finance_authority",
            "left": "transactions",
            "right": "authority",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["designation"],
        }
    )
    row, planned = _planned(ws)
    item = data_tests.create(
        ws,
        {
            "title": "Finance approval authority",
            "objective": "Test whether requesters had sufficient approval authority.",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "engine": "polars",
            "table_refs": ["invalid_finance_authority"],
            "spec": {"code": "result = invalid_finance_authority.select(pl.len())", "result_mode": "summary"},
        },
    )

    result = data_tests.run(ws, item["id"])

    assert result["semantic_valid"] is False
    assert result["status"] == "review_required"
    assert any("0% key match coverage" in issue for issue in result["semantic_issues"])


def test_null_only_result_is_not_accepted_as_success(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws)
    item = data_tests.create(
        ws,
        {
            "title": "Null output",
            "objective": "Exercise the semantic output gate.",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "engine": "polars",
            "table_refs": ["transactions"],
            "spec": {"code": "result = pl.DataFrame({'outcome': [None, None]})"},
        },
    )

    result = data_tests.run(ws, item["id"])

    assert result["semantic_valid"] is False
    assert result["status"] == "review_required"
    assert any("entirely null" in issue for issue in result["semantic_issues"])


def test_result_can_be_used_as_immutable_finding_evidence(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws)
    item = data_tests.create(ws, _analytics_payload(row, planned))
    result = data_tests.run(ws, item["id"])
    source_id = f"{item['id']}:{result['id']}"
    anchor = {
        "source_kind": "datatest",
        "source_id": source_id,
        "source_sha1": result["result_sha1"],
    }

    created = findings.add(
        ws,
        {
            "title": "Duplicate invoice identifiers",
            "severity": "medium",
            "condition": "One invoice identifier was repeated.",
            "criteria": "Invoice identifiers must be unique.",
            "cause_pending": True,
            "effect": "Duplicate payment risk.",
            "recommendation": "Review and block duplicate identifiers.",
            "severity_rationale": "A repeated identifier creates a material duplicate-payment risk.",
            "rcm_refs": [row["id"]],
            "planned_test_refs": [planned["id"]],
            "execution_refs": [f"datatest:{source_id}"],
            "evidence_refs": [anchor],
            "auditor_confirmed": True,
        },
    )

    assert created["evidence_refs"][0]["source_sha1"] == result["result_sha1"]


def test_exploratory_data_test_runs_without_counting_as_rcm_execution(workspace_with_data):
    ws = workspace_with_data
    item = data_tests.create(
        ws,
        {
            "title": "Explore large transactions",
            "objective": "Understand the population before planning fieldwork.",
            "engine": "polars",
            "table_refs": ["transactions"],
            "spec": {"code": "result = transactions.filter(pl.col('amount') > 500)"},
        },
    )

    result = data_tests.run(ws, item["id"])

    assert item["rcm_id"] is None
    assert item["planned_test_id"] is None
    assert result["rcm_id"] is None
    assert result["planned_test_id"] is None
    assert result["exception_count"] == 2
    assert rcm_execution.coverage(ws)["invalid_execution_parents"] == []
    assert dashboard.curate_rcm_tiles(ws)["curation"]["created_count"] == 0
    assert item["id"] not in {test["id"] for test in report.build_context(ws)["data_tests"]}


def test_data_test_parent_is_all_or_nothing_and_method_compatible(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(workspaces.WorkspaceError, match="both an RCM row and planned test"):
        data_tests.create(
            ws,
            {
                "title": "Partially linked",
                "objective": "Invalid partial audit link.",
                "rcm_id": "RCM-MISSING-PLANNED",
                "engine": "polars",
                "table_refs": ["transactions"],
                "spec": {"code": "result = transactions.head(1)"},
            },
        )
    row, planned = _planned(ws, method="document_inspection")
    payload = _analytics_payload(row, planned)
    with pytest.raises(workspaces.WorkspaceError, match="does not permit"):
        data_tests.create(ws, payload)


def test_exploratory_data_test_can_be_linked_and_unlinked(workspace_with_data):
    ws = workspace_with_data
    item = data_tests.create(
        ws,
        {
            "title": "Explore duplicate invoices",
            "objective": "Determine whether duplicate testing belongs in the audit plan.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    row, planned = _planned(ws)

    data_tests.update(
        ws, item["id"], {"rcm_id": row["id"], "planned_test_id": planned["id"]}
    )
    assert f"datatest:{item['id']}" in planned["execution_refs"]

    data_tests.update(ws, item["id"], {"rcm_id": None, "planned_test_id": None})
    assert item["rcm_id"] is None
    assert item["planned_test_id"] is None
    assert f"datatest:{item['id']}" not in planned["execution_refs"]


def test_result_integrity_check_detects_tampering(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws)
    item = data_tests.create(ws, _analytics_payload(row, planned))
    result = data_tests.run(ws, item["id"])
    path = ws.root / "DataTestResults" / item["id"] / f"{result['id']}.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["exception_count"] = 999
    path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(workspaces.WorkspaceError, match="integrity"):
        data_tests.load_result(ws, item["id"], result["id"])


def test_table_rename_updates_data_test_references_and_invalidates_latest_status(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws)
    item = data_tests.create(ws, _analytics_payload(row, planned))
    data_tests.run(ws, item["id"])

    renamed = ws.rename_table("transactions", "ledger entries")

    assert renamed["updated"]["data_tests"] == 1
    assert item["table_refs"] == ["ledger_entries"]
    assert item["status"] == "ready"


def test_data_test_api_create_run_reopen_and_pin(workspace_with_data):
    ws = workspace_with_data
    row, planned = _planned(ws)
    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}"

    created_response = client.post(f"{base}/data-tests", json=_analytics_payload(row, planned))
    assert created_response.status_code == 200
    created = created_response.json()
    assert created["last_run"] is None
    assert client.get(f"{base}/data-tests").json()["items"][0]["id"] == created["id"]

    executed = client.post(f"{base}/data-tests/{created['id']}/run")
    assert executed.status_code == 200
    result = executed.json()
    reopened = client.get(f"{base}/data-tests/{created['id']}/runs/{result['id']}")
    assert reopened.status_code == 200
    assert reopened.json()["result_sha1"] == result["result_sha1"]

    pinned = client.post(
        f"{base}/data-tests/{created['id']}/pin",
        json={"title": "RCM duplicate invoices"},
    )
    assert pinned.status_code == 200
    assert pinned.json()["result_ref"] == f"datatest:{created['id']}:{result['id']}"
    assert pinned.json()["planned_test_id"] == planned["id"]

    exploratory = client.post(
        f"{base}/data-tests",
        json={
            "title": "Explore transaction values",
            "objective": "Inspect the population without asserting audit coverage.",
            "engine": "polars",
            "table_refs": ["transactions"],
            "spec": {"code": "result = transactions.select('amount')", "result_mode": "summary"},
        },
    )
    assert exploratory.status_code == 200
    assert exploratory.json()["rcm_id"] is None
    exploratory_run = client.post(
        f"{base}/data-tests/{exploratory.json()['id']}/run"
    )
    assert exploratory_run.status_code == 200
    exploratory_pin = client.post(
        f"{base}/data-tests/{exploratory.json()['id']}/pin", json={}
    )
    assert exploratory_pin.status_code == 200
    assert exploratory_pin.json().get("rcm_id") is None
    assert exploratory_pin.json().get("planned_test_id") is None
