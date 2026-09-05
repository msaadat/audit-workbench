"""Per-column test coverage, as the auditor reading one table sees it.

``untested_columns`` already answers the disclosure question — what did the
audit never evaluate — and reaches the report as counts only, deliberately.
This is the same measurement turned round for the table page, where the useful
answer names the test rather than counting the gap.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import column_coverage, data_tests
from app.main import create_app


def _polars_test(ws, *, title: str, code: str) -> dict:
    return data_tests.create(
        ws,
        {
            "title": title,
            "objective": "Evaluate the population.",
            "criteria": "Every amount is supported.",
            "engine": "polars",
            "table_refs": ["transactions"],
            "steps": [
                {
                    "label": "Filter",
                    "instruction": "Filter the population.",
                    "table_refs": ["transactions"],
                    "code": code,
                }
            ],
            "spec": {
                "schema_version": 2,
                "steps": [
                    {
                        "label": "Filter",
                        "instruction": "Filter the population.",
                        "table_refs": ["transactions"],
                        "code": code,
                    }
                ],
            },
        },
    )


def test_coverage_names_the_tests_that_evaluate_each_column(workspace_with_data):
    ws = workspace_with_data
    first = _polars_test(ws, title="Amounts", code="result = transactions.filter(pl.col('amount') > 0)")
    second = _polars_test(
        ws, title="Amounts by customer",
        code="result = transactions.group_by('cust_id').agg(pl.col('amount').sum())",
    )

    coverage = column_coverage.table_coverage(ws, "transactions")
    by_column = {item["column"]: item["tests"] for item in coverage["columns"]}

    assert coverage["table"] == "transactions"
    # Both tests name `amount`; only the second names `cust_id`.
    assert by_column["amount"] == [first["id"], second["id"]]
    assert by_column["cust_id"] == [second["id"]]
    # The columns nothing evaluates are the scope statement, and are present
    # rather than omitted — an absent row would read as "no data".
    assert by_column["invoice_no"] == []
    assert by_column["tx_date"] == []


def test_coverage_reports_every_column_when_no_test_exists(workspace_with_data):
    coverage = column_coverage.table_coverage(workspace_with_data, "customers")

    assert [item["column"] for item in coverage["columns"]] == ["id", "customer"]
    assert all(item["tests"] == [] for item in coverage["columns"])


def test_coverage_declines_a_table_it_cannot_read(workspace_with_data):
    assert column_coverage.table_coverage(workspace_with_data, "not_a_table") is None


def test_the_coverage_route_answers_for_one_table(workspace_with_data):
    ws = workspace_with_data
    _polars_test(ws, title="Amounts", code="result = transactions.filter(pl.col('amount') > 0)")
    client = TestClient(create_app())

    response = client.get(f"/api/workspaces/{ws.id}/tables/transactions/coverage")

    assert response.status_code == 200
    payload = response.json()
    assert {item["column"] for item in payload["columns"]} == {
        "invoice_no", "cust_id", "amount", "tx_date",
    }
    assert next(item for item in payload["columns"] if item["column"] == "amount")["tests"]


def test_the_coverage_route_refuses_a_table_that_is_not_there(workspace_with_data):
    client = TestClient(create_app())

    response = client.get(f"/api/workspaces/{workspace_with_data.id}/tables/nope/coverage")

    assert response.status_code == 400
    assert "nope" in response.json()["detail"]
