import json

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import (
    dashboard,
    data_tests,
    doc_tests,
    documents,
    findings,
    rcm_execution,
    report,
    workspaces,
)
from app.agent.context import adapters
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
    assert first["control_conclusion"] == "ineffective"
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


def test_auditor_can_record_a_conclusion_on_a_completed_exception_result(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))
    result = data_tests.run(ws, item["id"])
    assert result["status"] == "completed_with_exception"
    assert item["conclusion"] == ""
    assert item["control_conclusion"] == "ineffective"

    updated = data_tests.update(
        ws, item["id"],
        {"conclusion": "Exceptions were investigated and are not indicative of a control failure.",
         "control_conclusion": "effective"},
    )
    assert updated["control_conclusion"] == "effective"
    assert updated["conclusion"].startswith("Exceptions were investigated")
    # The engine's own read of the run is untouched by the auditor's conclusion.
    assert updated["status"] == "completed_with_exception"

    with pytest.raises(workspaces.WorkspaceError):
        data_tests.update(ws, item["id"], {"control_conclusion": "bogus"})


def test_deterministic_run_marks_no_exception_as_effective(workspace_with_data):
    ws = workspace_with_data
    item = data_tests.create(ws, {
        "title": "No exception test",
        "objective": "Confirm the empty exception population.",
        "engine": "polars",
        "table_refs": ["transactions"],
        "spec": _polars_spec(
            table_refs=["transactions"],
            code="result = transactions.head(0)",
        ),
    })

    result = data_tests.run(ws, item["id"])

    assert result["status"] == "completed_no_exception"
    assert result["exception_count"] == 0
    assert result["control_conclusion"] == "effective"
    assert item["control_conclusion"] == "effective"


def test_run_all_includes_exploratory_and_rcm_tests(workspace_with_data):
    ws = workspace_with_data
    linked = data_tests.create(ws, _analytics_payload(_rcm_row(ws)))
    exploratory = data_tests.create(ws, {
        "title": "Exploratory no exception test",
        "objective": "Confirm the empty exception population.",
        "engine": "polars",
        "table_refs": ["transactions"],
        "spec": _polars_spec(
            table_refs=["transactions"],
            code="result = transactions.head(0)",
        ),
    })

    batch = data_tests.run_all(ws)

    assert batch["total"] == 2
    assert len(batch["completed"]) == 2
    assert batch["failed"] == []
    assert {item["data_test_id"] for item in batch["completed"]} == {
        linked["id"], exploratory["id"],
    }


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
    assert polars_run["exception_frame"]["columns"] == [
        *ws.get_frame("transactions").columns,
        "_step_id",
        "_step_label",
        "_reason",
    ]
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
    assert result["control_conclusion"] == "ineffective"
    assert not any("entirely null" in issue for issue in result["semantic_issues"])


def test_finding_exception_rows_name_the_records_that_failed(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))
    result = data_tests.run(ws, item["id"])

    rows = adapters.finding_exception_rows(ws, f"datatest:{item['id']}:{result['id']}")

    # The whole point of admitting these rows: a finding can say which invoice
    # was duplicated rather than only that one was.
    assert "invoice_no" in rows["columns"]
    assert rows["rows_supplied"] == result["exception_count"] == len(rows["rows"])
    assert rows["rows_withheld"] == 0
    assert rows["truncated"] is False
    assert rows["result_sha1"] == result["result_sha1"]
    # The identifier itself reaches the draft, which is what lets the narrative
    # name the record instead of counting it.
    invoice = rows["columns"].index("invoice_no")
    assert {str(value[invoice]) for value in rows["rows"]} == {"1006"}


def _lifecycle_test(ws, code, *, extra_step=None):
    steps = [
        {
            "label": "Stage sequence",
            "instruction": "Return records that break the required sequence.",
            "table_refs": ["transactions"],
            "code": code,
        }
    ]
    if extra_step:
        steps.append(extra_step)
    return data_tests.create(ws, {
        "title": "Transaction lifecycle",
        "objective": "Identify transactions that break the required sequence.",
        "rcm_id": _rcm_row(ws)["id"],
        "engine": "polars",
        "spec": {"schema_version": 2, "steps": steps},
    })


def test_each_exception_row_names_the_condition_it_failed(workspace_with_data):
    ws = workspace_with_data
    item = _lifecycle_test(
        ws,
        "result = transactions.filter(\n"
        "    pl.col('cust_id').is_null()\n"
        "    | (pl.col('amount') > 500)\n"
        "    | (pl.col('invoice_no') == 1003)\n"
        ")",
    )

    result = data_tests.run(ws, item["id"])
    profile = result["exception_profile"]

    # A filter is several alternative conditions. Which one a row met is the
    # first thing the auditor needs, and the returned frame never says it.
    assert profile["reason_source"] == "predicate"
    assert {reason["label"] for reason in profile["reasons"]} == {
        "amount is greater than 500",
        "invoice_no is 1003",
    }
    reasons = result["exception_frame"]["columns"].index("_reason")
    assert all(row[reasons] for row in result["exception_frame"]["rows"])


def test_a_condition_that_cannot_be_reconstructed_falls_back_to_the_step(
    workspace_with_data,
):
    ws = workspace_with_data
    # The filter reads a column the step then drops, so the predicate cannot be
    # re-evaluated against what came back. Naming the step is the honest answer.
    item = _lifecycle_test(
        ws,
        "result = transactions.filter(\n"
        "    (pl.col('amount') > 500) | pl.col('cust_id').is_null()\n"
        ").select('invoice_no')",
    )

    profile = data_tests.run(ws, item["id"])["exception_profile"]

    assert profile["reason_source"] == "step"
    assert [reason["label"] for reason in profile["reasons"]] == ["Stage sequence"]


def test_a_reason_names_only_the_fields_its_condition_reads(workspace_with_data):
    ws = workspace_with_data
    item = _lifecycle_test(
        ws,
        "joined = transactions.join(customers, left_on='cust_id', right_on='id', how='left')\n"
        "result = joined.filter((pl.col('amount') > 500) | pl.col('customer').is_null())",
    )

    profile = data_tests.run(ws, item["id"])["exception_profile"]

    # A step returns a whole joined record; almost none of it is what the step
    # was looking at. Showing every populated column is how the old table got to
    # be unreadable.
    reasons = {reason["label"]: reason["columns"] for reason in profile["reasons"]}
    assert reasons["amount is greater than 500"] == ["amount"]


def test_a_step_that_cannot_be_split_still_narrows_to_the_fields_it_reads(
    workspace_with_data,
):
    ws = workspace_with_data
    # The threshold is a variable, so the branch cannot be re-evaluated on its
    # own and the rows keep the step's label instead of a condition.
    item = _lifecycle_test(
        ws,
        "limit = 500\n"
        "result = transactions.filter((pl.col('amount') > limit) | pl.col('cust_id').is_null())",
    )

    profile = data_tests.run(ws, item["id"])["exception_profile"]

    # No condition to name, but the step's source still says which fields it
    # read — a far better answer than every column that holds a value.
    assert profile["reason_source"] == "step"
    assert profile["reasons"][0]["columns"] == ["cust_id", "amount"]


def test_records_are_counted_once_across_the_steps_that_catch_them(
    workspace_with_data,
):
    ws = workspace_with_data
    item = _lifecycle_test(
        ws,
        "result = customers.filter((pl.col('id') == 'C1') | pl.col('customer').is_null())",
        extra_step={
            "label": "Named customers",
            "instruction": "Return the customer the register duplicates.",
            "table_refs": ["customers"],
            "code": "result = customers.filter(pl.col('customer') == 'Alpha')",
        },
    )

    result = data_tests.run(ws, item["id"])
    profile = result["exception_profile"]

    # Both steps catch the same customer, so the two rows they return are one
    # exception. Reporting the rows doubles the exception rate.
    assert profile["row_count"] == 2
    assert profile["record_count"] == 1
    assert profile["entity_key"] == "id"
    assert "1 of 3 record(s) in customers failed (33%)" in result["verdict_text"]


def test_the_population_the_exceptions_were_drawn_from_is_stated(workspace_with_data):
    ws = workspace_with_data
    item = _lifecycle_test(
        ws,
        "result = customers.filter((pl.col('id') == 'C1') | pl.col('customer').is_null())",
    )

    result = data_tests.run(ws, item["id"])
    profile = result["exception_profile"]

    # "1 exception" is unreadable without the population it came out of.
    assert profile["population_table"] == "customers"
    assert profile["population"] == 3
    assert "1 of 3 record(s) in customers failed (33%)" in result["verdict_text"]


def test_steps_over_unrelated_populations_are_left_without_a_record_count(
    workspace_with_data,
):
    ws = workspace_with_data
    item = _lifecycle_test(
        ws,
        "result = transactions.filter((pl.col('amount') > 500) | pl.col('cust_id').is_null())",
        extra_step={
            "label": "Customer register",
            "instruction": "Return customers missing a name.",
            "table_refs": ["customers"],
            "code": "result = customers.filter((pl.col('id') == 'C1') | pl.col('customer').is_null())",
        },
    )

    profile = data_tests.run(ws, item["id"])["exception_profile"]

    # The steps test different things. A column both happen to carry is not the
    # record, and a rate computed against it would not mean anything.
    assert profile["entity_key"] is None
    assert profile["population"] is None


def test_exception_rows_carry_what_each_step_was_looking_for(workspace_with_data):
    ws = workspace_with_data
    item = data_tests.create(ws, {
        "title": "Large transactions",
        "objective": "Identify transactions greater than 500.",
        "rcm_id": _rcm_row(ws)["id"],
        "engine": "polars",
        "spec": _polars_spec(
            label="Large transactions",
            instruction="Filter transactions greater than 500.",
            table_refs=["transactions"],
            code="result = transactions.filter(pl.col('amount') > 500)",
        ),
    })
    result = data_tests.run(ws, item["id"])

    rows = adapters.finding_exception_rows(ws, f"datatest:{item['id']}:{result['id']}")

    # Rows alone are an undifferentiated table; the step's instruction is what
    # makes them evidence of a specific failure.
    step = rows["steps"][0]
    assert step["label"] == "Large transactions"
    assert step["instruction"] == "Filter transactions greater than 500."
    assert step["exception_count"] == result["exception_count"]


def test_finding_exception_rows_are_capped_and_disclose_the_withheld_count(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))
    result = data_tests.run(ws, item["id"])
    assert result["exception_count"] > 1
    monkeypatch.setattr(adapters, "FINDING_EXCEPTION_ROW_LIMIT", 1)

    rows = adapters.finding_exception_rows(ws, f"datatest:{item['id']}:{result['id']}")

    # A truncated table must never be draftable as a complete population.
    assert rows["rows_supplied"] == 1
    assert rows["rows_withheld"] == result["exception_count"] - 1
    assert rows["truncated"] is True
    assert rows["exception_count"] == result["exception_count"]


def test_a_character_budget_caps_the_exception_table_before_the_row_limit(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = data_tests.create(ws, _analytics_payload(row))
    result = data_tests.run(ws, item["id"])
    monkeypatch.setattr(adapters, "FINDING_EXCEPTION_ROW_CHARACTERS", 1)

    rows = adapters.finding_exception_rows(ws, f"datatest:{item['id']}:{result['id']}")

    # One row is always supplied: a table of nothing is not evidence, and the
    # withheld count is what keeps the narrative honest about the rest.
    assert rows["rows_supplied"] == 1
    assert rows["truncated"] is True


def test_document_tests_supply_no_exception_table(workspace_with_data):
    ws = workspace_with_data
    test = doc_tests.create_test(ws, {
        "kind": "review", "title": "Procedure review",
        "objective": "Assess the documented procedure.",
        "rcm_id": _rcm_row(ws)["id"],
        "items": [{"label": "Review the SOP", "state": "exception"}],
    })

    # A Document Test has no tabular exception population; the optional source
    # resolving to nothing is the normal shape, not a missing projection.
    assert adapters.finding_exception_rows(ws, f"doctest:{test['id']}") is None


def test_document_test_findings_can_name_the_documents_they_rest_on(
    workspace_with_data,
):
    ws = workspace_with_data
    source = documents.add_document(ws, "procurement_sop.txt", b"The procedure text.")
    test = doc_tests.create_test(ws, {
        "kind": "review", "title": "Procedure review",
        "objective": "Assess the documented procedure.",
        "rcm_id": _rcm_row(ws)["id"],
        "items": [
            {
                "label": "Review the SOP",
                "state": "exception",
                "document_ids": [source["id"]],
            }
        ],
    })

    projection = adapters._finding_execution_projection(ws, f"doctest:{test['id']}")

    # "The supplied documentation did not establish X" is not actionable; the
    # document's name is what makes it so.
    assert projection["items"][0]["documents"] == [
        {"id": source["id"], "title": source["title"], "sha1": source["sha1"]}
    ]


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
            "cause_pending": True,
            "narrative": (
                "## Condition\n\nOne invoice identifier was repeated.\n\n"
                "## Criteria\n\nInvoice identifiers must be unique.\n\n"
                "## Root Cause\n\n"
                "## Risk\n\nDuplicate payment risk.\n\n"
                "## Recommendation\n\nReview and block duplicate identifiers.\n"
            ),
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
    # An exploratory test is not audit coverage, so it reaches no RCM row and
    # therefore never reaches the report.
    assert item["id"] not in {
        test["id"]
        for row in report.build_context(ws)["rcm"]
        for test in row["tests"]
    }


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


# ------------------------------------------------------------------ reality gate
def _workspace_with_population() -> workspaces.Workspace:
    """A workspace whose tables are wide enough for a population-level signal."""
    ws = workspaces.create_workspace("Reality Gate")
    orders = pl.DataFrame(
        {
            "order_id": [f"O{index:03d}" for index in range(40)],
            "buyer_id": [f"B{index % 4:03d}" for index in range(40)],
            "status": ["Closed"] * 38 + ["Open"] * 2,
        }
    )
    approvals = pl.DataFrame(
        {
            "order_id": [f"O{index:03d}" for index in range(40)],
            "approver_id": [2000 + (index % 5) for index in range(40)],
        }
    )
    ws.add_table("orders.csv", orders.write_csv().encode())
    ws.add_table("approvals.csv", approvals.write_csv().encode())
    return ws


def _run_step(ws, code: str) -> dict:
    row = _rcm_row(ws)
    item = data_tests.create(
        ws,
        {
            "title": "Generated step",
            "objective": "Exercise the generated predicate.",
            "rcm_id": row["id"],
            "engine": "polars",
            "spec": _polars_spec(label="Generated step", table_refs=["orders"], code=code),
        },
    )
    return data_tests.compute(ws, item["id"])


def test_step_filtering_on_a_value_the_column_never_holds_cannot_conclude():
    """The dead-literal half of a guessed category value.

    The generating turn is given column names and dtypes, so a literal it guessed
    wrong matches nothing and the run would otherwise report the control as
    operating effectively.
    """
    result = _run_step(
        _workspace_with_population(),
        "result = orders.filter(pl.col('status') == 'Cancelled')",
    )

    assert result["exception_count"] == 0
    assert result["semantic_valid"] is False
    assert result["status"] == "review_required"
    assert result["control_conclusion"] == "no_conclusion"
    assert any("cannot match the rows it describes" in issue for issue in result["semantic_issues"])


def test_step_excepting_nearly_the_whole_population_cannot_conclude():
    """The saturated half: a wrong literal that matches every row instead of none."""
    result = _run_step(
        _workspace_with_population(),
        "result = orders.filter(pl.col('status') != 'Approved')",
    )

    assert result["exception_count"] == 40
    assert result["semantic_valid"] is False
    assert result["status"] == "review_required"
    assert any("mis-specified predicate" in issue for issue in result["semantic_issues"])


def test_step_comparing_identifiers_from_different_schemes_cannot_conclude():
    """A segregation-of-duties step whose two sides can never be equal."""
    result = _run_step(
        _workspace_with_population(),
        "joined = orders.join(approvals, on='order_id', how='inner')\n"
        "result = joined.filter(pl.col('buyer_id') == pl.col('approver_id').cast(pl.String))",
    )

    assert result["exception_count"] == 0
    assert result["semantic_valid"] is False
    assert any("share no value" in issue for issue in result["semantic_issues"])


def test_a_sound_step_that_finds_no_exception_still_concludes():
    """The gate must not turn every clean result into a review item."""
    result = _run_step(
        _workspace_with_population(),
        "result = orders.filter(pl.col('status') == 'Open').filter(pl.col('buyer_id').is_null())",
    )

    assert result["exception_count"] == 0
    assert result["semantic_valid"] is True
    assert result["status"] == "completed_no_exception"
    assert result["control_conclusion"] == "effective"


def test_a_sound_step_that_finds_real_exceptions_still_concludes():
    result = _run_step(
        _workspace_with_population(),
        "result = orders.filter(pl.col('status') == 'Open')",
    )

    assert result["exception_count"] == 2
    assert result["semantic_valid"] is True
    assert result["control_conclusion"] == "ineffective"
