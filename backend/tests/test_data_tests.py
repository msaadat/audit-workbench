import json

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import dashboard, data_tests, findings, rcm_execution, report, workspaces
from app.main import create_app


def _rcm_row(ws):
    return ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Transactions may bypass controls",
            "risk_rating": "high",
            "control": "Automated validation",
        }
    )


def _analytics_payload(row):
    return {
        "title": "Duplicate invoice numbers",
        "objective": "Identify repeated invoice numbers.",
        "criteria": "Invoice identifiers must be unique.",
        "steps": [{"label": "Analyze the full population.", "instruction": "Analyze the full population."}],
        "rcm_id": row["id"],
        "engine": "analytics",
        "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    }


def _polars_spec(*, label="Filter", instruction="Filter the population.", table_refs, code):
    return {
        "schema_version": 2,
        "steps": [
            {
                "label": label,
                "instruction": instruction,
                "table_refs": table_refs,
                "code": code,
            }
        ],
    }


def test_create_validates_but_does_not_count_as_execution(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)

    item = data_tests.create(ws, _analytics_payload(row))

    assert item["status"] == "ready"
    assert item["last_run"] is None
    assert "runs" not in item
    assert f"datatest:{item['id']}" in row["test_refs"]
    assert not (ws.root / "DataTestResults").exists()


def test_analytics_run_replaces_the_current_durable_result(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))

    first = data_tests.run(ws, item["id"])

    assert first["status"] == "completed_with_exception"
    assert first["exception_count"] == 2
    assert first["dataset_fingerprints"]["transactions"]
    assert first["result_sha1"]
    assert data_tests.load_result(ws, item["id"], first["id"]) == first
    assert item["last_run"]["id"] == first["id"]
    assert item["last_run"]["id"] == first["id"]

    second = data_tests.run(ws, item["id"])
    assert second["id"] == first["id"] == data_tests.CURRENT_RESULT_ID
    assert data_tests.load_result(ws, item["id"], first["id"])["result_sha1"] == second["result_sha1"]
    assert [path.name for path in (ws.root / "DataTestResults" / item["id"]).glob("*.json")] == [
        "DTR-CURRENT.json"
    ]


def test_rerun_discards_unreferenced_legacy_result_files(workspace_with_data):
    ws = workspace_with_data
    item = data_tests.create(ws, _analytics_payload(_rcm_row(ws)))
    first = data_tests.run(ws, item["id"])
    legacy_path = ws.root / "DataTestResults" / item["id"] / "DTR-OLD.json"
    legacy_path.write_text(json.dumps({**first, "id": "DTR-OLD"}), encoding="utf-8")

    data_tests.run(ws, item["id"])

    assert not legacy_path.exists()


def test_validation_and_polars_engines_persist_bounded_exception_results(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    validation_test = data_tests.create(
        ws,
        {
            "title": "Positive amounts",
            "objective": "Identify non-positive transaction amounts.",
            "rcm_id": row["id"],
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
            "engine": "polars",
            "spec": _polars_spec(
                label="Large transactions",
                instruction="Filter transactions greater than 500.",
                table_refs=["transactions"],
                code="result = transactions.filter(pl.col('amount') > 500)",
            ),
        },
    )
    polars_run = data_tests.run(ws, polars_test["id"])
    assert polars_run["exception_count"] == 2
    assert polars_run["exception_frame"]["columns"] == [*ws.get_frame("transactions").columns, "_step_id", "_step_label"]
    assert polars_run["step_results"][0]["exception_count"] == 2


def test_polars_steps_expose_every_workspace_table_without_table_refs(workspace_with_data):
    ws = workspace_with_data
    ws.add_table("limits.csv", pl.DataFrame({"threshold": [500]}).write_csv().encode())

    item = data_tests.create(
        ws,
        {
            "title": "Amounts over the workspace limit",
            "objective": "Identify transactions above the configured limit.",
            "engine": "polars",
            "spec": {
                "schema_version": 2,
                "steps": [{
                    "label": "Compare to limit",
                    "instruction": "Return transactions above the configured limit.",
                    "code": "result = transactions.filter(pl.col('amount') > limits['threshold'][0])",
                }],
            },
        },
    )

    assert item["table_refs"] == []
    assert "table_refs" not in item["spec"]["steps"][0]
    result = data_tests.run(ws, item["id"])
    assert result["exception_count"] == 2
    assert set(result["dataset_fingerprints"]) == set(ws.table_names())


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
    row = _rcm_row(ws)
    item = data_tests.create(
        ws,
        {
            "title": "Finance approval authority",
            "objective": "Test whether requesters had sufficient approval authority.",
            "rcm_id": row["id"],
            "engine": "polars",
            "spec": _polars_spec(
                label="Count joined rows",
                instruction="Count rows across the invalid join.",
                table_refs=["invalid_finance_authority"],
                code="result = invalid_finance_authority.select(pl.len())",
            ),
        },
    )

    result = data_tests.run(ws, item["id"])

    assert result["semantic_valid"] is False
    assert result["status"] == "review_required"
    assert any("0% key match coverage" in issue for issue in result["semantic_issues"])


def test_null_only_result_is_not_accepted_as_success(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(
        ws,
        {
            "title": "Null output",
            "objective": "Exercise the semantic output gate.",
            "rcm_id": row["id"],
            "engine": "polars",
            "spec": _polars_spec(
                label="Null output",
                instruction="Produce an all-null result.",
                table_refs=["transactions"],
                code="result = pl.DataFrame({'outcome': [None, None]})",
            ),
        },
    )

    result = data_tests.run(ws, item["id"])

    assert result["semantic_valid"] is False
    assert result["status"] == "review_required"
    assert any("entirely null" in issue for issue in result["semantic_issues"])


def test_null_exception_field_with_populated_identifier_is_valid(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(
        ws,
        {
            "title": "Missing approval identifiers",
            "objective": "Identify transactions without an approval identifier.",
            "rcm_id": row["id"],
            "engine": "polars",
            "spec": _polars_spec(
                label="Select transactions with a missing approval",
                instruction="Return identifiers and the missing approval value.",
                table_refs=["transactions"],
                code=(
                    "result = transactions.select([pl.col('invoice_no'), "
                    "pl.lit(None).cast(pl.String).alias('missing_approval')])"
                ),
            ),
        },
    )

    result = data_tests.run(ws, item["id"])

    assert result["semantic_valid"] is True
    assert result["status"] == "completed_with_exception"
    assert result["exception_count"] > 0
    assert not any("entirely null" in issue for issue in result["semantic_issues"])


def test_result_can_be_used_as_immutable_finding_evidence(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))
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
            "test_refs": [item["id"]],
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
            "spec": _polars_spec(
                label="Large transactions",
                instruction="Filter transactions greater than 500.",
                table_refs=["transactions"],
                code="result = transactions.filter(pl.col('amount') > 500)",
            ),
        },
    )

    result = data_tests.run(ws, item["id"])

    assert item["rcm_id"] is None
    assert result["rcm_id"] is None
    assert result["exception_count"] == 2
    assert rcm_execution.coverage(ws)["invalid_test_parents"] == []
    assert dashboard.curate_rcm_tiles(ws)["curation"]["created_count"] == 0
    assert item["id"] not in {test["id"] for test in report.build_context(ws)["data_tests"]}


def test_data_test_rejects_a_link_to_a_row_that_does_not_exist(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(workspaces.WorkspaceError, match="RCM row 'RCM-MISSING' not found"):
        data_tests.create(
            ws,
            {
                "title": "Badly linked",
                "objective": "Invalid audit link.",
                "rcm_id": "RCM-MISSING",
                "engine": "polars",
                "spec": _polars_spec(
                    instruction="Take the first row.",
                    table_refs=["transactions"],
                    code="result = transactions.head(1)",
                ),
            },
        )


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
    row = _rcm_row(ws)

    data_tests.update(ws, item["id"], {"rcm_id": row["id"]})
    assert f"datatest:{item['id']}" in row["test_refs"]

    data_tests.update(ws, item["id"], {"rcm_id": None})
    assert item["rcm_id"] is None
    assert f"datatest:{item['id']}" not in row["test_refs"]


def test_result_integrity_check_detects_tampering(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))
    result = data_tests.run(ws, item["id"])
    path = ws.root / "DataTestResults" / item["id"] / f"{result['id']}.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["exception_count"] = 999
    path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(workspaces.WorkspaceError, match="integrity"):
        data_tests.load_result(ws, item["id"], result["id"])


def test_table_rename_updates_data_test_references_and_invalidates_latest_status(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))
    data_tests.run(ws, item["id"])

    renamed = ws.rename_table("transactions", "ledger entries")

    assert renamed["updated"]["data_tests"] == 1
    assert item["table_refs"] == ["ledger_entries"]
    assert item["status"] == "ready"


def test_data_test_api_create_run_reopen_and_pin(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}"

    created_response = client.post(f"{base}/data-tests", json=_analytics_payload(row))
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
    assert pinned.json()["rcm_id"] == row["id"]

    exploratory = client.post(
        f"{base}/data-tests",
        json={
            "title": "Explore transaction values",
            "objective": "Inspect the population without asserting audit coverage.",
            "engine": "polars",
            "spec": _polars_spec(
                instruction="Select the amount column.",
                table_refs=["transactions"],
                code="result = transactions.select('amount')",
            ),
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


def test_data_test_api_runs_all_rcm_linked_tests_and_skips_exploratory(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    linked = data_tests.create(ws, _analytics_payload(row))
    exploratory = data_tests.create(
        ws,
        {
            "title": "Exploratory duplicates",
            "objective": "Inspect duplicates without RCM coverage.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    client = TestClient(create_app())

    response = client.post(f"/api/workspaces/{ws.id}/data-tests/run-all-rcm")

    assert response.status_code == 200
    batch = response.json()
    assert batch["total"] == 1
    assert batch["failed"] == []
    assert batch["completed"][0]["data_test_id"] == linked["id"]
    persisted = workspaces.load_workspace(ws.id)
    persisted_linked = next(item for item in persisted.data_tests if item["id"] == linked["id"])
    persisted_exploratory = next(
        item for item in persisted.data_tests if item["id"] == exploratory["id"]
    )
    assert persisted_linked["last_run"] is not None
    assert persisted_exploratory["last_run"] is None
