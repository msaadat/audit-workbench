from app import dashboard, data_tests, doc_tests, documents, rcm_execution, working_papers


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


def test_test_manifest_treats_refined_document_test_statuses_as_durable_results(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _row(ws)
    document = documents.add_document(ws, "approval-review.txt", b"Approval evidence")
    test = doc_tests.create_test(
        ws,
        {
            "kind": "review",
            "title": "Completed approval review",
            "rcm_id": row["id"],
            "items": [{"document_ids": [document["id"]], "page": 1}],
        },
    )
    saved = doc_tests.load_test(ws, test["id"])
    saved["status"] = "completed_with_exception"
    doc_tests.save_test(ws, saved)

    manifest = rcm_execution.test_manifest(ws)
    item = next(value for value in manifest if value["test_id"] == test["id"])

    assert item["has_durable_result"] is True


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


def test_dashboard_status_loads_the_document_test_worklist_once(
    workspace_with_data, monkeypatch,
):
    """Status checks reuse one request-local document-test index across RCM rows."""
    ws = workspace_with_data
    rows = [_row(ws) for _ in range(3)]
    for row in rows:
        _document_test(ws, row)

    list_calls = 0
    load_calls = 0
    original_list_tests = doc_tests.list_tests
    original_load_test = doc_tests.load_test

    def tracked_list_tests(*args, **kwargs):
        nonlocal list_calls
        list_calls += 1
        return original_list_tests(*args, **kwargs)

    def tracked_load_test(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return original_load_test(*args, **kwargs)

    monkeypatch.setattr(doc_tests, "list_tests", tracked_list_tests)
    monkeypatch.setattr(doc_tests, "load_test", tracked_load_test)

    dashboard.engagement_status_payload(ws)

    assert list_calls == 1
    assert load_calls == len(rows)


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


def test_an_undispositioned_exception_stays_open_across_rollups(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    observation = ws.observations[0]

    rcm_execution.rollup(ws)

    assert observation["outcome"] == "exception"
    assert data_tests._record(ws, item["id"])["open_exception_count"] > 0


def test_accepting_every_exception_group_closes_the_observation(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    record = data_tests._record(ws, item["id"])
    assert record["open_exception_count"] > 0

    for reason in record["evaluation"]["reasons"]:
        data_tests.record_exception_disposition(
            ws, item["id"], reason["label"], "accepted",
            note="Pre-approved emergency purchases; supported on inspection.",
        )

    rcm_execution.rollup(ws)
    record = data_tests._record(ws, item["id"])

    # The run still says what it found; the rulings say what still stands.
    assert record["exception_count"] > 0
    assert record["open_exception_count"] == 0
    assert record["status"] == "completed_no_exception"
    # An exception nobody is carrying forward is not a finding candidate.
    assert ws.observations[0]["outcome"] == "resolved"


def test_completion_discloses_conclusions_no_auditor_reviewed(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning(
        {"context": {"objective": "Test procurement controls.", "scope": "Invoice population."}}
    )
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    data_tests.auto_disposition(ws, item["id"])

    completion = rcm_execution.completion(ws)

    # The unattended conclusion closes the gate — that is what lets an auto run
    # finish — but the file still records that nobody read it.
    assert completion["blank_conclusions"] == []
    assert completion["unreviewed_agent_conclusions"] == [
        {"rcm_id": row["id"], "test_id": item["id"]}
    ]

    data_tests.update(ws, item["id"], {"control_conclusion": "ineffective"})
    assert rcm_execution.completion(ws)["unreviewed_agent_conclusions"] == []


def test_a_rerun_does_not_disturb_the_auditors_conclusion(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    data_tests.update(
        ws,
        item["id"],
        {
            "control_conclusion": "effective",
            "conclusion": "Exceptions are pre-approved emergency buys; the control operated.",
        },
    )

    data_tests.run(ws, item["id"])
    record = data_tests._record(ws, item["id"])

    # Re-running the same definition over the same data re-reads the evidence.
    # It is not a second opinion about the control.
    assert record["control_conclusion"] == "effective"
    assert record["control_conclusion_source"] == "auditor"
    assert record["control_conclusion_stale"] is False
    assert record["conclusion"].startswith("Exceptions are pre-approved")


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


def test_a_disclosed_scope_limitation_is_not_an_inconsistent_conclusion(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _row(ws)
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    rcm_execution.rollup(ws)
    record = data_tests._record(ws, item["id"])
    record["control_conclusion"] = "effective"
    record["open_exception_count"] = 0
    # Disclosing the boundary a conclusion was reached within is what the field
    # is for; it must not read as a contradiction of the conclusion itself.
    record["scope_limitations"] = "Vendor master was out of scope this period."

    result = rcm_execution.coverage(ws)

    assert result["inconsistent_conclusions"] == []


def _rollup_stub(variant, subject, **overrides):
    return {
        "variant": variant,
        "subject_tokens": sorted(subject),
        "conclusion_eligible": True,
        **overrides,
    }


def test_design_inquiry_alone_cannot_conclude_a_control_effective():
    """Whether a policy exists is not whether the population complies with it."""
    row = {
        "control_attributes": [
            {
                "key": "approval_before_commitment",
                "requirement": "Purchases are approved before commitment.",
            }
        ]
    }
    contributing = [
        _rollup_stub("qa", {"purchases", "approved", "before", "commitment"})
    ]

    ceiling = rcm_execution._evidence_ceiling(row, contributing)

    assert "documentation describes the control" in ceiling


def test_a_substantive_test_beside_design_inquiry_lifts_the_ceiling():
    row = {
        "control_attributes": [
            {
                "key": "approval_before_commitment",
                "requirement": "Purchases are approved before commitment.",
            }
        ]
    }
    contributing = [
        _rollup_stub("qa", {"purchases", "approved"}),
        _rollup_stub("data", {"purchases", "approved", "before", "commitment"}),
    ]

    assert rcm_execution._evidence_ceiling(row, contributing) == ""


def test_a_requirement_no_executed_test_names_blocks_effective(workspace_with_data):
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Vendor management",
            "risk": "Payments are directed to unapproved bank details",
            "risk_rating": "critical",
            "control": "Vendor master amendments are independently reviewed.",
            "control_attributes": [
                {
                    "key": "vendor_id_unique",
                    "assertion": "Completeness",
                    "requirement": "Vendor identifiers are unique.",
                    "evidence_kind": "tabular_population",
                },
                {
                    "key": "bank_account_amendment",
                    "assertion": "Authorization",
                    "requirement": (
                        "Changes to vendor bank account details are approved "
                        "independently of the requester."
                    ),
                    "evidence_kind": "tabular_population",
                },
            ],
        }
    )
    item = data_tests.create(
        ws,
        {
            "title": "Vendor identifier uniqueness",
            "objective": "Confirm vendor identifiers are unique.",
            "rcm_id": row["id"],
            "engine": "polars",
            "spec": {
                "schema_version": 2,
                "steps": [
                    {
                        "label": "Unique ids",
                        "instruction": "Check uniqueness.",
                        "table_refs": ["transactions"],
                        "code": "result = transactions.head(0)",
                    }
                ],
            },
        },
    )
    data_tests.run(ws, item["id"])
    # Agent-derived: nobody has judged this, so the ceiling still decides.
    data_tests.update(
        ws, item["id"], {"control_conclusion": "effective"}, agent=True
    )

    rolled = rcm_execution.rollup(ws)
    (rolled_row,) = [entry for entry in rolled["rows"] if entry["rcm_id"] == row["id"]]

    # The uniqueness test is real evidence for uniqueness and says nothing
    # about the bank-account requirement sitting beside it.
    assert rolled_row["control_conclusion"] == "partially_effective"
    assert "bank_account_amendment" in rolled_row["evidence_ceiling"]
    assert rolled_row["evidence_ceiling_applied"] is True


def test_an_auditor_conclusion_is_reported_beside_the_ceiling_not_capped_by_it(
    workspace_with_data,
):
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Vendor management",
            "risk": "Payments are directed to unapproved bank details",
            "risk_rating": "critical",
            "control": "Vendor master amendments are independently reviewed.",
            "control_attributes": [
                {
                    "key": "vendor_id_unique",
                    "assertion": "Completeness",
                    "requirement": "Vendor identifiers are unique.",
                    "evidence_kind": "tabular_population",
                },
                {
                    "key": "bank_account_amendment",
                    "assertion": "Authorization",
                    "requirement": (
                        "Changes to vendor bank account details are approved "
                        "independently of the requester."
                    ),
                    "evidence_kind": "tabular_population",
                },
            ],
        }
    )
    item = data_tests.create(
        ws,
        {
            "title": "Vendor identifier uniqueness",
            "objective": "Confirm vendor identifiers are unique.",
            "rcm_id": row["id"],
            "engine": "polars",
            "spec": {
                "schema_version": 2,
                "steps": [
                    {
                        "label": "Unique ids",
                        "instruction": "Check uniqueness.",
                        "table_refs": ["transactions"],
                        "code": "result = transactions.head(0)",
                    }
                ],
            },
        },
    )
    data_tests.run(ws, item["id"])
    data_tests.update(ws, item["id"], {"control_conclusion": "effective"})

    rolled = rcm_execution.rollup(ws)
    (rolled_row,) = [entry for entry in rolled["rows"] if entry["rcm_id"] == row["id"]]

    # The auditor concluded this by hand. The limitation stays on the row as a
    # disclosure, but the roll-up reports their judgment rather than revising it.
    assert rolled_row["control_conclusion"] == "effective"
    assert "bank_account_amendment" in rolled_row["evidence_ceiling"]
    assert rolled_row["evidence_ceiling_applied"] is False
    # And it stops reading as an outstanding coverage issue.
    assert rcm_execution.completion(ws)["evidence_ceilings"] == []


def test_a_row_whose_requirements_were_all_tested_still_concludes_effective(
    workspace_with_data,
):
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Unsupported purchases",
            "risk_rating": "high",
            "control": "Large transactions are screened.",
            "control_attributes": [
                {
                    "key": "large_transaction_screening",
                    "assertion": "Operational",
                    "requirement": "Large transaction screening identifies purchases.",
                    "evidence_kind": "tabular_population",
                }
            ],
        }
    )
    item = _data_test(ws, row)
    data_tests.run(ws, item["id"])
    data_tests.update(
        ws,
        item["id"],
        {
            "control_conclusion": "effective",
            "conclusion": "The screened purchases were all approved on review.",
        },
    )

    rolled = rcm_execution.rollup(ws)
    (rolled_row,) = [entry for entry in rolled["rows"] if entry["rcm_id"] == row["id"]]

    assert rolled_row["control_conclusion"] == "effective"
    assert rolled_row["evidence_ceiling"] == ""


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


def test_completion_uses_control_conclusion_without_free_text(
    workspace_with_data,
):
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
    rcm_execution.rollup(ws)

    # Agreeing with the run needs no argument, which is what lets this record a
    # control conclusion and no prose at all.
    data_tests.update(ws, item["id"], {"control_conclusion": "ineffective"})

    completion = rcm_execution.completion(ws)
    assert completion["blank_conclusions"] == []
