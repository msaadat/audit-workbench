from app import data_tests, doc_tests, rcm_execution, working_papers


def _row(ws):
    return ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Unsupported purchases",
            "risk_rating": "high",
            "control": "Purchases require approval and supporting documents.",
            "criteria": "Procurement policy section 4.",
        }
    )


def _data_test(ws, row):
    return data_tests.create(
        ws,
        {
            "title": "Large transaction screening",
            "objective": "Identify purchases above 500 for follow-up.",
            "criteria": "No purchase above 500 lacks approval.",
            "steps": [{"label": "Analyze approval fields.", "instruction": "Analyze approval fields."}],
            "rcm_id": row["id"],
            "engine": "polars",
            "spec": {
                "schema_version": 2,
                "steps": [
                    {
                        "label": "Large transactions",
                        "instruction": "Filter transactions greater than 500.",
                        "table_refs": ["transactions"],
                        "code": "result = transactions.filter(pl.col('amount') > 500)",
                    }
                ],
            },
        },
    )


def _document_test(ws, row, *, exception=False):
    state = "exception" if exception else "confirmed"
    disposition = "exception" if exception else "accepted"
    test = doc_tests.create_test(
        ws,
        {
            "kind": "attribute",
            "title": "Approval evidence",
            "objective": "Determine whether purchases were approved.",
            "steps": [{"label": "Inspect supporting documents.", "instruction": "Inspect supporting documents."}],
            "rcm_id": row["id"],
            "items": [
                {
                    "label": "Invoice 1001",
                    "state": state,
                    "auditor_disposition": disposition,
                    "attributes": [
                        {"name": "Approval", "result": "fail" if exception else "pass"}
                    ],
                }
            ],
        },
    )
    return doc_tests.update_test(ws, test["id"], {"status": "completed"})


def test_coverage_reports_rows_without_tests_and_unspecified_drafts(
    workspace_with_data,
):
    ws = workspace_with_data
    orphan_row = ws.add_rcm(
        {"process": "Treasury", "risk": "Cash misuse", "risk_rating": "high"}
    )
    row = _row(ws)
    draft = data_tests.create_draft(
        ws,
        {
            "title": "Approval analytics",
            "objective": "Analyze approval fields.",
            "rcm_id": row["id"],
        },
    )

    result = rcm_execution.coverage(ws)

    assert orphan_row["id"] in result["rows_without_tests"]
    assert {"rcm_id": row["id"], "test_id": draft["id"]} in result["unspecified_tests"]
    assert result["ok"] is False


def test_an_unlinked_test_is_exploration_not_a_coverage_defect(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    _data_test(ws, row)
    data_tests.create(
        ws,
        {
            "title": "Exploratory scan",
            "objective": "Look at the population.",
            "engine": "polars",
            "spec": {
                "schema_version": 2,
                "steps": [
                    {
                        "label": "Scan",
                        "instruction": "Look at the first row.",
                        "table_refs": ["transactions"],
                        "code": "result = transactions.head(1)",
                    }
                ],
            },
        },
    )

    result = rcm_execution.coverage(ws)

    assert result["rows_without_tests"] == []
    assert result["invalid_test_parents"] == []


def test_test_manifest_carries_the_plan_and_the_specification_state(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    draft = doc_tests.create_draft(
        ws,
        {
            "title": "Approval inspection",
            "objective": "Inspect approvals.",
            "rcm_id": row["id"],
        },
    )

    manifest = rcm_execution.test_manifest(ws)

    specified = next(value for value in manifest if value["test_id"] == item["id"])
    assert specified["rcm_id"] == row["id"]
    assert specified["kind"] == "datatest"
    assert specified["specified"] is True
    assert specified["steps"] == [{"label": "Analyze approval fields.", "instruction": "Analyze approval fields."}]
    unspecified = next(value for value in manifest if value["test_id"] == draft["id"])
    assert unspecified["kind"] == "doctest"
    assert unspecified["specified"] is False


def test_a_description_only_document_test_is_not_executable(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    shell = doc_tests.create_test(
        ws,
        {
            "kind": "review",
            "title": "Approval review shell",
            "objective": "Review approval evidence.",
            "rcm_id": row["id"],
            "items": [{"label": "Review approval evidence"}],
        },
    )

    manifest = rcm_execution.test_manifest(ws)
    item = next(value for value in manifest if value["test_id"] == shell["id"])

    assert item["executable"] is False


def test_rollup_combines_both_sources_and_creates_observations(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    data_test = _data_test(ws, row)
    data_result = data_tests.run(ws, data_test["id"])
    document_test = _document_test(ws, row, exception=True)

    rolled = rcm_execution.rollup(ws)
    row_rollup = rolled["rows"][0]

    assert row_rollup["tests"] == 2
    assert {item["kind"] for item in row_rollup["test_rollups"]} == {
        "datatest",
        "doctest",
    }
    assert row_rollup["exceptions"] == data_result["exception_count"] + 1
    assert len(ws.observations) == 2
    assert f"datatest:{data_test['id']}" in (row["test_refs"] or [])
    assert f"doctest:{document_test['id']}" in (row["test_refs"] or [])


def test_row_rollup_reports_a_passed_and_failed_tally(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    _document_test(ws, row, exception=True)
    _document_test(ws, row)

    rolled = rcm_execution.rollup(ws)
    row_rollup = rolled["rows"][0]

    # The row-level conclusion is the tally over its linked tests.
    assert row_rollup["tests"] == 2
    assert row_rollup["completed"] == 2
    assert row_rollup["failed"] == 1
    assert row_rollup["passed"] == 1


def test_exception_observation_is_final_without_a_disposition(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    observation = ws.observations[0]

    rcm_execution.rollup(ws)

    assert observation["outcome"] == "exception"
    assert data_tests._record(ws, item["id"])["open_exception_count"] > 0


def test_rollup_reconciles_a_legacy_data_test_observation_reference(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    result = data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    observation = ws.observations[0]
    observation["execution_ref"] = f"datatest:{item['id']}:DTR-LEGACY"
    ws.observations.append(
        {
            **observation,
            "id": "OBS-DUPLICATE",
            "execution_ref": f"datatest:{item['id']}:{result['id']}",
        }
    )

    rcm_execution.rollup(ws, persist=False)

    assert len(ws.observations) == 1
    assert ws.observations[0]["execution_ref"] == f"datatest:{item['id']}:{result['id']}"


def test_completed_test_without_durable_execution_is_rejected(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests._record(ws, item["id"])["status"] = "completed_no_exception"

    result = rcm_execution.coverage(ws)

    assert result["completed_without_durable_result"] == [
        {"rcm_id": row["id"], "test_id": item["id"]}
    ]


def test_effective_conclusion_with_open_exception_is_inconsistent(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    data_tests._record(ws, item["id"])["control_conclusion"] = "effective"

    result = rcm_execution.coverage(ws)

    assert result["inconsistent_conclusions"][0]["test_id"] == item["id"]


def test_rcm_working_paper_contains_data_and_document_result_hashes(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_result = data_tests.run(ws, item["id"])
    document_test = _document_test(ws, row)

    paper = working_papers.generate_rcm(ws, row["id"])

    assert f"Data Test {item['id']}" in paper["markdown"]
    assert f"Document Test {document_test['id']}" in paper["markdown"]
    assert data_result["result_sha1"] in paper["markdown"]
    # Generation rolls results up first, which refines the test's own outcome
    # and therefore its content hash; the paper cites the current one.
    current = doc_tests.load_test(ws, document_test["id"])
    assert current["sha1"] in paper["markdown"]
    assert (ws.root / "WorkingPapers" / f"{row['id']}.json").is_file()
    assert "<script" not in paper["html"]


def test_completion_uses_execution_and_outcome_gates(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning(
        {
            "context": {
                "objective": "Test procurement controls.",
                "scope": "Invoice population.",
            }
        }
    )
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])

    open_result = rcm_execution.completion(ws)
    assert open_result["status"] == "completed_with_open_items"
    assert ws.observations[0]["outcome"] == "exception"
    data_tests.update(
        ws,
        item["id"],
        {
            "conclusion": "The exception was recorded for follow-up.",
            "control_conclusion": "partially_effective",
        },
    )
    completed = rcm_execution.completion(ws)
    assert completed["status"] == "completed"
