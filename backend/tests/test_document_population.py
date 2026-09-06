"""A document test written against a typed population, not a list of ids.

The engagement these exercise is the one in
``docs/document-test-population-design.md``: twelve payment vouchers, an
expense policy, and an RCM row asking that the category and description match
the underlying transaction. The old shape produced a blocked test in a
workspace holding twelve of exactly the document it asked for.
"""

from __future__ import annotations

import pytest

from app import (
    doc_tests,
    document_analysis,
    document_classification as dc,
    document_population,
    document_schemas,
    documents,
    workspaces,
)
from app.workspaces import WorkspaceError

VOUCHER_FIELDS = [
    {"name": "voucher_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "employee_name", "role": "party", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "expense_category", "role": "attribute", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "expense_description", "role": "attribute", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "amount_paid", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


def _voucher(ws, name: str, records: list[dict]) -> str:
    """Store one voucher with however many line-item records it holds."""

    document = documents.add_document(ws, name, b"voucher text", category="evidence")
    dc.assign(ws, str(document["id"]), "payment_voucher", assigned_by="model")
    schema = document_schemas.get_schema(ws, "payment_voucher")
    document_analysis.persist_analysis(
        ws,
        document,
        {"pages": [{"page": 1, "text": "voucher text"}]},
        {
            "analysis_profile": "structured",
            "summary_markdown": "s",
            "audit_notes_markdown": "n",
            "schema_ref": {
                "document_type": schema["document_type"],
                "schema_version": schema["schema_version"],
                "schema_hash": schema["schema_hash"],
            },
            "citations": [
                {"id": "c1", "page": 1, "excerpt": "voucher text"},
            ],
            "records": [
                {
                    "fields": [
                        {"name": key, "entry": 1, "value": value, "citation": "c1"}
                        for key, value in record.items()
                    ],
                    "additional_fields": [],
                }
                for record in records
            ],
        },
        provider="local",
        model="test",
    )
    return str(document["id"])


@pytest.fixture
def ws() -> workspaces.Workspace:
    workspace = workspaces.create_workspace("Expenses")
    document_schemas.save_schema(workspace, "payment_voucher", VOUCHER_FIELDS)
    documents.add_document(
        workspace,
        "Expense-policy.txt",
        b"Section 4.2. Alcohol is not a reimbursable expense.",
        category="policy",
    )
    _voucher(
        workspace,
        "PV-2025-001.txt",
        [{
            "voucher_id": "PV-2025-001",
            "employee_name": "Ayesha Khan",
            "expense_category": "Transport",
            "expense_description": "Client meeting travel",
            "amount_paid": "4500",
        }],
    )
    # A voucher pack: three line items, three transactions, three assessments.
    _voucher(
        workspace,
        "PV-2025-002.txt",
        [
            {
                "voucher_id": "PV-2025-002",
                "employee_name": "Bilal Ahmed",
                "expense_category": "Meals",
                "expense_description": "Client dinner",
                "amount_paid": "9000",
            },
            {
                "voucher_id": "PV-2025-002",
                "employee_name": "Bilal Ahmed",
                "expense_category": "Meals",
                "expense_description": "Client dinner - wine",
                "amount_paid": "6000",
            },
            {
                "voucher_id": "PV-2025-002",
                "employee_name": "Bilal Ahmed",
                "expense_category": "Transport",
                "expense_description": "Taxi home",
                "amount_paid": "1200",
            },
        ],
    )
    return workspaces.load_workspace(workspace.id)


def _policy_id(ws) -> str:
    return next(
        str(document["id"])
        for document in ws.documents
        if str(document.get("category")) == "policy"
    )


def _population(ws, **overrides) -> dict:
    value = {
        "document_type": "payment_voucher",
        "fields": ["expense_category", "expense_description"],
        "criteria_refs": [{"document_id": _policy_id(ws), "section": "4"}],
    }
    value.update(overrides)
    return value


def _test(ws, **overrides) -> dict:
    return doc_tests.build_qa(
        ws,
        {
            "title": "Expense classification",
            "questions": ["Does the description describe an expense the SOP permits?"],
            "population": _population(ws, **overrides),
        },
    )


# ------------------------------------------------------------------ the unit
def test_the_unit_of_assessment_is_the_record_not_the_document(ws):
    """A voucher pack's three line items are three transactions.

    Assessing the pack as one unit lets two clean lines carry a third that is
    not, which is the aggregation this whole design removes.
    """

    test = _test(ws)
    item = test["items"][0]

    units = document_population.assessment_units(item)
    assert len(units) == 4
    assert {unit["document_id"] for unit in units} == set(item["document_ids"])
    pack = _pack_document(test)
    assert sorted(
        unit["record_index"] for unit in units if unit["document_id"] == pack
    ) == [0, 1, 2]
    assert [unit["key"] for unit in units if unit["document_id"] == pack] == [
        f"{pack}#0", f"{pack}#1", f"{pack}#2"
    ]

    # ``document_ids`` stays the executable list every existing reader iterates.
    assert len(item["document_ids"]) == 2


def test_the_population_is_resolved_from_the_type_not_enumerated(ws):
    test = _test(ws)
    population = test["items"][0]["population"]

    assert population["document_type"] == "payment_voucher"
    assert population["population_records"] == 4
    assert population["population_documents"] == 2
    assert population["omitted_records"] == 0
    assert doc_tests.assurance_scope(test) == "full_population"


def test_a_voucher_imported_afterwards_joins_the_population_on_the_next_read(ws):
    test = _test(ws)
    before = test["items"][0]["population"]["inputs_sha1"]

    _voucher(
        workspaces.load_workspace(ws.id),
        "PV-2025-003.txt",
        [{
            "voucher_id": "PV-2025-003",
            "employee_name": "Sana Malik",
            "expense_category": "Other",
            "expense_description": "Misc.",
            "amount_paid": "800",
        }],
    )

    reloaded = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    population = reloaded["items"][0]["population"]
    assert population["population_records"] == 5
    assert population["inputs_sha1"] != before
    assert len(reloaded["items"][0]["document_ids"]) == 3


def test_a_document_of_the_type_with_no_reading_is_named_not_dropped(ws):
    """A silent contributor is the failure mode this design exists to remove."""

    unread = documents.add_document(
        ws, "PV-2025-009.txt", b"scan", category="evidence"
    )
    dc.assign(ws, str(unread["id"]), "payment_voucher", assigned_by="model")

    test = _test(workspaces.load_workspace(ws.id))
    population = test["items"][0]["population"]

    assert [entry["document_id"] for entry in population["unread_documents"]] == [
        str(unread["id"])
    ]
    # Three documents carry the type and one contributes nothing, so the run
    # covers a population it can name but not all of one.
    assert population["population_documents"] == 3
    assert doc_tests.assurance_scope(test) == "sampled_population"


# ------------------------------------------------------------- what it refuses
def test_a_population_over_a_type_with_no_schema_is_refused_by_name(ws):
    with pytest.raises(WorkspaceError) as error:
        _test(ws, document_type="delivery_note")

    assert "holds no schema for" in str(error.value)


def test_a_field_the_schema_does_not_list_is_refused_as_a_field_not_a_gap(ws):
    with pytest.raises(WorkspaceError) as error:
        _test(ws, fields=["expense_category", "approver_grade"])

    assert "not a field of the 'payment_voucher' schema" in str(error.value)


def test_an_item_names_a_population_or_attaches_documents_but_not_both(ws):
    with pytest.raises(WorkspaceError) as error:
        doc_tests.build_qa(
            ws,
            {
                "title": "Both",
                "questions": ["Is it approved?"],
                "document_ids": [_policy_id(ws)],
                "population": _population(ws),
            },
        )

    assert "not both" in str(error.value)


def test_criteria_may_name_the_rcm_row_when_the_rule_lives_only_there(ws):
    row = ws.add_rcm({
        "process": "Expenses",
        "risk": "Non-reimbursable expenses are paid",
        "control": "Category review",
        "criteria": "Alcohol is not reimbursable.",
    })
    ws.save()

    test = _test(
        workspaces.load_workspace(ws.id),
        criteria_refs=[f"rcm:{row['id']}#criteria"],
    )

    assert test["items"][0]["population"]["criteria_refs"] == [
        {"kind": "rcm", "rcm_id": row["id"], "field": "criteria"}
    ]


def test_an_unknown_criteria_document_is_refused(ws):
    with pytest.raises(WorkspaceError) as error:
        _test(ws, criteria_refs=[{"document_id": "not-a-document"}])

    assert "unknown document" in str(error.value)


# ----------------------------------------------------------- identity fields
def test_identifier_fields_lead_a_grid_row(ws):
    schema = document_schemas.get_schema(ws, "payment_voucher")

    assert document_population.identifier_fields(schema) == ["voucher_id"]


def test_without_an_identifier_role_the_first_verbatim_fields_stand_in():
    schema = {
        "fields": [
            {"name": "note", "role": "attribute", "verbatim": False},
            {"name": "paid_on", "role": "attribute", "verbatim": True},
            {"name": "payee", "role": "party", "verbatim": True},
            {"name": "memo", "role": "attribute", "verbatim": True},
        ]
    }

    assert document_population.identifier_fields(schema) == ["paid_on", "payee"]


# --------------------------------------------------------------- assessments
def _answer(outcome: str, **overrides) -> dict:
    value = {
        "answer": "Alcohol is excluded under SOP 4.2.",
        "conclusion": "Excluded expense reimbursed.",
        "control_conclusion": "ineffective" if outcome == "exception" else "effective",
        "outcome": outcome,
        "citations": [],
    }
    value.update(overrides)
    return value


def _pack_document(test: dict) -> str:
    """The voucher holding three records."""

    counts: dict[str, int] = {}
    for unit in document_population.assessment_units(test["items"][0]):
        counts[unit["document_id"]] = counts.get(unit["document_id"], 0) + 1
    return max(counts, key=lambda key: counts[key])


def test_one_record_may_be_an_exception_while_its_siblings_are_accepted(ws):
    test = _test(ws)
    item_id = test["items"][0]["id"]
    pack = _pack_document(test)

    current = ws
    for unit in document_population.assessment_units(test["items"][0]):
        current = workspaces.load_workspace(ws.id)
        outcome = (
            "exception"
            if unit["document_id"] == pack and unit["record_index"] == 1
            else "accepted"
        )
        doc_tests.commit_qa_answer(
            current,
            test["id"],
            item_id,
            unit["document_id"],
            _answer(outcome),
            record_index=unit["record_index"],
        )

    settled = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    summary = doc_tests.population_summary(settled, settled["items"][0])
    assert summary["assessed"] == 4
    assert summary["outcome_counts"] == {
        "accepted": 3,
        "exception": 1,
        "needs_manual_check": 0,
    }
    # Any exception wins for the item, without erasing the three that passed.
    assert settled["items"][0]["evaluation"]["state"] == "failed"


def test_an_answer_for_a_record_outside_the_population_is_refused(ws):
    test = _test(ws)

    with pytest.raises(WorkspaceError) as error:
        doc_tests.commit_qa_answer(
            ws,
            test["id"],
            test["items"][0]["id"],
            test["items"][0]["document_ids"][0],
            _answer("accepted"),
            record_index=97,
        )

    assert "resolved population" in str(error.value)


def test_an_answer_survives_a_population_that_grew_and_one_that_moved(ws):
    test = _test(ws)
    item_id = test["items"][0]["id"]
    first = document_population.assessment_units(test["items"][0])[0]
    doc_tests.commit_qa_answer(
        workspaces.load_workspace(ws.id),
        test["id"],
        item_id,
        first["document_id"],
        _answer("accepted"),
        record_index=first["record_index"],
    )

    _voucher(
        workspaces.load_workspace(ws.id),
        "PV-2025-004.txt",
        [{
            "voucher_id": "PV-2025-004",
            "employee_name": "Zara Iqbal",
            "expense_category": "Transport",
            "expense_description": "Airport transfer",
            "amount_paid": "3000",
        }],
    )
    reloaded = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    summary = doc_tests.population_summary(reloaded, reloaded["items"][0])

    # The answer already given is kept; the new record is simply unassessed, and
    # the run is reported as not caught up rather than as complete.
    assert summary["assessed"] == 1
    assert summary["resolved"] == 5
    assert summary["run_current"] is False


# ------------------------------------------------------------- dispositioning
def test_the_item_disposition_is_folded_from_the_record_calls(ws):
    test = _test(ws)
    item_id = test["items"][0]["id"]
    units = document_population.assessment_units(test["items"][0])

    doc_tests.update_population_dispositions(
        workspaces.load_workspace(ws.id),
        test["id"],
        item_id,
        [{"key": units[0]["key"], "state": "confirmed"}],
    )
    partial = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    # One row settled is not the item settled.
    assert partial["items"][0]["disposition"]["state"] == "pending"

    doc_tests.update_population_dispositions(
        workspaces.load_workspace(ws.id),
        test["id"],
        item_id,
        [{"key": unit["key"], "state": "confirmed"} for unit in units[1:-1]]
        + [{"key": units[-1]["key"], "state": "exception", "note": "Alcohol."}],
    )
    settled = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    assert settled["items"][0]["disposition"]["state"] == "exception"


def test_a_record_that_left_the_population_takes_its_disposition_with_it(ws):
    test = _test(ws)
    item_id = test["items"][0]["id"]
    units = document_population.assessment_units(test["items"][0])
    doc_tests.update_population_dispositions(
        workspaces.load_workspace(ws.id),
        test["id"],
        item_id,
        [{"key": unit["key"], "state": "confirmed"} for unit in units],
    )

    # Retyped: the vouchers are no longer payment vouchers, so the population
    # they were dispositioned under no longer reaches them.
    current = workspaces.load_workspace(ws.id)
    doc_tests.load_test(current, test["id"])
    for document_id in {unit["document_id"] for unit in units}:
        dc.assign(current, document_id, "delivery_note", assigned_by="auditor")

    reloaded = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    assert reloaded["items"][0][doc_tests.POPULATION_DISPOSITIONS_KEY] == {}
    assert doc_tests.execution_issues(reloaded) == [
        "item 1 resolves no current payment_voucher record"
    ]


# ---------------------------------------------------------------------- grid
def test_the_grid_reads_as_the_rows_that_matter_then_the_rest(ws):
    test = _test(ws)
    item_id = test["items"][0]["id"]
    pack = _pack_document(test)
    for unit in document_population.assessment_units(test["items"][0]):
        outcome = (
            "exception"
            if unit["document_id"] == pack and unit["record_index"] == 1
            else "accepted"
        )
        doc_tests.commit_qa_answer(
            workspaces.load_workspace(ws.id),
            test["id"],
            item_id,
            unit["document_id"],
            _answer(outcome),
            record_index=unit["record_index"],
        )

    current = workspaces.load_workspace(ws.id)
    grid = doc_tests.population_grid(
        current, doc_tests.load_test(current, test["id"]), item_id
    )

    assert grid["identifier_columns"] == ["voucher_id"]
    assert grid["field_columns"] == ["expense_category", "expense_description"]
    assert [row["outcome"] for row in grid["rows"]][0] == "exception"
    assert grid["rows"][0]["values"]["expense_description"] == "Client dinner - wine"
    assert grid["page"]["total"] == 4
    # The criteria the rows were judged against travel with the grid, so the
    # auditor reads the verdict beside the rule rather than beside the question.
    assert grid["criteria"]


def test_the_grid_can_be_filtered_to_one_outcome(ws):
    test = _test(ws)
    item_id = test["items"][0]["id"]
    unit = document_population.assessment_units(test["items"][0])[0]
    doc_tests.commit_qa_answer(
        workspaces.load_workspace(ws.id),
        test["id"],
        item_id,
        unit["document_id"],
        _answer("accepted"),
        record_index=unit["record_index"],
    )

    current = workspaces.load_workspace(ws.id)
    loaded = doc_tests.load_test(current, test["id"])
    unrun = doc_tests.population_grid(current, loaded, item_id, outcome="not_run")

    assert len(unrun["rows"]) == 3
    assert {row["outcome"] for row in unrun["rows"]} == {"not_run"}


# -------------------------------------------------------------------- sample
def test_a_sample_states_that_it_is_one(ws):
    test = _test(ws, selection={"mode": "sample", "method": "interval", "size": 2})
    population = test["items"][0]["population"]

    assert len(document_population.assessment_units(test["items"][0])) == 2
    assert population["omitted_records"] == 2
    assert doc_tests.assurance_scope(test) == "sampled_population"
    assert doc_tests.result_rollup(test)["assurance_label"] == "Sampled population"


def test_a_sample_needs_a_method_the_engine_actually_draws(ws):
    with pytest.raises(WorkspaceError) as error:
        _test(ws, selection={"mode": "sample", "method": "first_n", "size": 2})

    assert "sample method" in str(error.value)


def test_evidence_cannot_be_hand_attached_to_a_population_item(ws):
    """Silently allowing it is worse: the next read would erase it."""

    test = _test(ws)

    with pytest.raises(WorkspaceError) as error:
        doc_tests.attach_document(
            ws, test["id"], test["items"][0]["id"], _policy_id(ws)
        )

    assert "written against every 'payment_voucher' record" in str(error.value)


def test_an_auditor_call_is_not_read_as_the_evidence_changing(ws):
    """A finding citing this test rests on what was tested, not on who signed."""

    test = _test(ws)
    before = doc_tests.test_evidence_sha1(test)
    units = document_population.assessment_units(test["items"][0])
    doc_tests.update_population_dispositions(
        workspaces.load_workspace(ws.id),
        test["id"],
        test["items"][0]["id"],
        [{"key": unit["key"], "state": "confirmed"} for unit in units],
    )

    settled = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    assert doc_tests.test_evidence_sha1(settled) == before


def test_the_item_call_is_made_on_the_records_not_over_them(ws):
    test = _test(ws)

    with pytest.raises(WorkspaceError) as error:
        doc_tests.update_item(
            ws, test["id"], test["items"][0]["id"], {"state": "confirmed"}
        )

    assert "record your call on the records" in str(error.value)
