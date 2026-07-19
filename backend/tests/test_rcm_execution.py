from app import data_tests, doc_tests, rcm_execution, working_papers


def _setup(ws, *, method="hybrid"):
    row = ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Unsupported purchases",
            "risk_rating": "high",
            "control": "Purchases require approval and supporting documents.",
            "criteria": "Procurement policy section 4.",
        }
    )
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Approval and support test",
            "objective": "Determine whether purchases were approved and supported.",
            "criteria": "Purchases require approval and invoice support.",
            "method": method,
            "steps": ["Analyze approval fields.", "Inspect supporting documents."],
        },
    )
    return row, planned


def _data_test(ws, row, planned):
    return data_tests.create(
        ws,
        {
            "title": "Large transaction screening",
            "objective": "Identify purchases above 500 for follow-up.",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "engine": "polars",
            "table_refs": ["transactions"],
            "spec": {"code": "result = transactions.filter(pl.col('amount') > 500)"},
        },
    )


def _document_test(ws, row, planned, *, exception=False):
    state = "exception" if exception else "confirmed"
    disposition = "exception" if exception else "accepted"
    test = doc_tests.create_test(
        ws,
        {
            "kind": "attribute",
            "title": "Approval evidence",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "items": [
                {
                    "label": "Invoice 1001",
                    "state": state,
                    "auditor_disposition": disposition,
                    "attributes": [{"name": "Approval", "result": "fail" if exception else "pass"}],
                }
            ],
        },
    )
    return doc_tests.update_test(ws, test["id"], {"status": "completed"})


def test_coverage_reports_missing_plans_execution_and_invalid_parents(workspace_with_data):
    ws = workspace_with_data
    orphan_row = ws.add_rcm(
        {"process": "Treasury", "risk": "Cash misuse", "risk_rating": "high"}
    )
    row, planned = _setup(ws, method="hybrid")

    result = rcm_execution.coverage(ws)

    assert orphan_row["id"] in result["rows_without_planned_tests"]
    missing = next(
        item for item in result["planned_tests_without_execution"]
        if item["planned_test_id"] == planned["id"]
    )
    assert missing["missing"] == ["datatest", "doctest"]
    assert result["ok"] is False


def test_execution_manifest_preserves_method_and_required_kinds(workspace_with_data):
    row, planned = _setup(workspace_with_data, method="hybrid")

    manifest = rcm_execution.execution_manifest(workspace_with_data)

    item = next(value for value in manifest if value["planned_test_id"] == planned["id"])
    assert item["rcm_id"] == row["id"]
    assert item["method"] == "hybrid"
    assert item["required_execution"] == ["datatest", "doctest"]
    assert item["steps"] == ["Analyze approval fields.", "Inspect supporting documents."]
    assert item["missing_execution"] == ["datatest", "doctest"]


def test_description_only_linked_document_test_does_not_satisfy_execution_coverage(
    workspace_with_data,
):
    row, planned = _setup(workspace_with_data, method="document_inspection")
    shell = doc_tests.create_test(workspace_with_data, {
        "kind": "review", "title": "Approval review shell",
        "rcm_id": row["id"], "planned_test_id": planned["id"],
        "items": [{"label": "Review approval evidence"}],
    })

    manifest = rcm_execution.execution_manifest(workspace_with_data)
    item = next(value for value in manifest if value["planned_test_id"] == planned["id"])
    covered = rcm_execution.coverage(workspace_with_data)

    assert item["existing_execution"][0]["id"] == shell["id"]
    assert item["existing_execution"][0]["executable"] is False
    assert item["missing_execution"] == ["doctest"]
    assert covered["planned_tests_without_execution"] == [{
        "rcm_id": row["id"], "planned_test_id": planned["id"], "missing": ["doctest"],
    }]


def test_rollup_combines_both_execution_kinds_and_creates_observations(workspace_with_data):
    ws = workspace_with_data
    row, planned = _setup(ws)
    data_test = _data_test(ws, row, planned)
    data_result = data_tests.run(ws, data_test["id"])
    document_test = _document_test(ws, row, planned, exception=True)

    rolled = rcm_execution.rollup(ws)
    planned_rollup = rolled["rows"][0]["planned_test_rollups"][0]

    assert planned_rollup["status"] == "completed_with_exception"
    assert planned_rollup["executed_count"] == 2
    assert planned_rollup["exception_count"] == data_result["exception_count"] + 1
    assert {item["kind"] for item in planned_rollup["linked_execution"]} == {
        "datatest",
        "doctest",
    }
    assert len(ws.observations) == 2
    assert planned["open_exception_count"] == data_result["exception_count"] + 1
    assert row["execution_rollup"]["exceptions"] == planned_rollup["exception_count"]
    assert document_test["id"] in planned["execution_refs"][1]


def test_disposition_closes_observation_and_reduces_open_rollup(workspace_with_data):
    ws = workspace_with_data
    row, planned = _setup(ws, method="data_analytics")
    item = _data_test(ws, row, planned)
    data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    observation = ws.observations[0]

    disposed = rcm_execution.disposition(
        ws, observation["id"], "expected_or_benign", "Known approved high-value items."
    )
    rcm_execution.rollup(ws)

    assert disposed["status"] == "disposed"
    assert planned["open_exception_count"] == 0


def test_completed_planned_test_without_durable_execution_is_rejected(workspace_with_data):
    ws = workspace_with_data
    row, planned = _setup(ws, method="data_analytics")
    _data_test(ws, row, planned)
    planned["status"] = "completed_no_exception"

    result = rcm_execution.coverage(ws)

    assert result["completed_without_durable_result"] == [
        {"rcm_id": row["id"], "planned_test_id": planned["id"]}
    ]


def test_effective_conclusion_with_open_exception_is_inconsistent(workspace_with_data):
    ws = workspace_with_data
    row, planned = _setup(ws, method="data_analytics")
    item = _data_test(ws, row, planned)
    data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    planned["control_conclusion"] = "effective"

    result = rcm_execution.coverage(ws)

    assert result["inconsistent_conclusions"][0]["planned_test_id"] == planned["id"]


def test_rcm_working_paper_contains_data_and_document_result_hashes(workspace_with_data):
    ws = workspace_with_data
    row, planned = _setup(ws)
    item = _data_test(ws, row, planned)
    data_result = data_tests.run(ws, item["id"])
    document_test = _document_test(ws, row, planned)

    paper = working_papers.generate_rcm(ws, row["id"])

    assert f"Data Test {item['id']}" in paper["markdown"]
    assert f"Document Test {document_test['id']}" in paper["markdown"]
    assert data_result["result_sha1"] in paper["markdown"]
    assert document_test["sha1"] in paper["markdown"]
    assert (ws.root / "WorkingPapers" / f"{row['id']}.json").is_file()
    assert "<script" not in paper["html"]


def test_completion_uses_execution_and_outcome_gates(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Test procurement controls.", "scope": "Invoice population."}})
    row, planned = _setup(ws, method="data_analytics")
    item = _data_test(ws, row, planned)
    data_tests.run(ws, item["id"])

    open_result = rcm_execution.completion(ws)
    assert open_result["status"] == "completed_with_open_items"
    assert open_result["open_observations"]

    for observation in list(ws.observations):
        rcm_execution.disposition(ws, observation["id"], "expected_or_benign")
    planned["conclusion"] = "The control operated effectively for the tested population."
    planned["control_conclusion"] = "effective"
    completed = rcm_execution.completion(ws)
    assert completed["status"] == "completed"
