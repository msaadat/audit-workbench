"""Executing a population item: one record, its reading, and its criteria.

The point of the shape is what a single assessment costs and what it may cite.
A record's reading has already extracted every field with a citation, so the
turn reads a few hundred characters instead of a page, and an answer citing
``expense_description`` resolves to a real page the model never saw.
"""

from __future__ import annotations

import json

from app import doc_tests, document_population, workspaces
from app.agent.capabilities.doc_tests import assessment_pairs, document_test_units
from app.agent.context import ContextResolver, document_qa_scope
from app.agent.doc_tests_execution import unit_record_index
from app.agent.executors.fieldwork import document_qa_answer_ref
from app.agent.workers import WORKERS, WorkerRequest
from app.agent.workers.fieldwork import DOCUMENT_QA_SYSTEM

from test_document_population import _population, _voucher, ws  # noqa: F401

CAPABILITY_ID = "fieldwork.executed"


class _Gateway:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1, conversation=None):
        self.calls.append((system, user, attempt))
        return self.response


def _test(workspace, **overrides):
    return doc_tests.build_qa(
        workspace,
        {
            "title": "Expense classification",
            "questions": [
                "Does expense_description, read with expense_category, describe "
                "an expense the SOP permits?"
            ],
            "population": _population(workspace, **overrides),
        },
    )


def _bundle(workspace, test, unit):
    capability = type(
        "_Capability",
        (),
        {"id": CAPABILITY_ID, "context": "fieldwork.document_qa"},
    )()
    _manifest, bundle = ContextResolver().resolve(
        workspace,
        capability,
        {"id": unit.id},
        document_qa_scope(
            workspace,
            test["id"],
            test["items"][0]["id"],
            unit.parent_refs[-1].split(":")[1],
            unit_record_index({"parent_refs": list(unit.parent_refs)}),
        ),
    )
    return bundle


# ------------------------------------------------------------------ expansion
def test_execution_fans_out_one_unit_per_record(ws):
    test = _test(ws)

    units = document_test_units(
        workspaces.load_workspace(ws.id), test["id"], forced=False, title=test["title"]
    )

    assert len(units) == 4
    assert {unit.kind for unit in units} == {"document_qa_execution"}
    # The record is in the unit's identity and in its lineage, so a re-run
    # answers only what is unanswered and a receipt says which record it settled.
    assert all(
        any(ref.startswith("record:") for ref in unit.parent_refs) for unit in units
    )
    assert len({unit.id for unit in units}) == 4
    assert assessment_pairs([doc_tests.load_test(ws, test["id"])]) == 4


def test_an_answered_record_is_not_expanded_again(ws):
    test = _test(ws)
    unit = document_population.assessment_units(test["items"][0])[0]
    doc_tests.commit_qa_answer(
        workspaces.load_workspace(ws.id),
        test["id"],
        test["items"][0]["id"],
        unit["document_id"],
        {"answer": "a", "outcome": "accepted", "control_conclusion": "effective"},
        record_index=unit["record_index"],
    )

    units = document_test_units(
        workspaces.load_workspace(ws.id), test["id"], forced=False, title=test["title"]
    )

    assert len(units) == 3
    assert unit["key"] not in "".join(unit_spec.id for unit_spec in units)


def test_the_answer_reference_names_the_record(ws):
    assert document_qa_answer_ref("DT-1", "ITEM-1", "doc", record_index=2) == (
        "doctest:DT-1:item:ITEM-1:document:doc:record:2"
    )
    # A document-grained question keeps the reference it has always had.
    assert document_qa_answer_ref("DT-1", "ITEM-1", "doc") == (
        "doctest:DT-1:item:ITEM-1:document:doc"
    )


# -------------------------------------------------------------------- context
def test_one_assessment_reads_the_reading_and_the_criteria_not_the_pages(ws):
    test = _test(ws)
    units = document_test_units(
        workspaces.load_workspace(ws.id), test["id"], forced=False, title=test["title"]
    )
    bundle = _bundle(workspaces.load_workspace(ws.id), test, units[0])

    sources = {item.source_id for item in bundle.items}
    assert "document_reading" in sources
    assert "criteria_excerpt" in sources
    # Every named field is stated by the reading, so no page is fetched at all.
    assert "document_pages" not in sources

    reading = next(
        item.content for item in bundle.items if item.source_id == "document_reading"
    )
    assert set(reading["fields"]) == {
        "voucher_id",
        "expense_category",
        "expense_description",
    }
    assert reading["missing_fields"] == []
    # A reading projection is a fraction of a page excerpt.
    assert bundle.supplied_size.characters < 4_000


def test_a_field_the_reading_leaves_silent_sends_that_unit_to_the_pages(ws):
    """Silence about a field is not a value, so the page is fetched to settle it.

    Only for that record. The other three still answer from their readings,
    which is what keeps a population of eighty-four from costing eighty-four
    page fetches because one voucher was read badly.
    """

    partial = _voucher(
        workspaces.load_workspace(ws.id),
        "PV-2025-005.txt",
        [{
            "voucher_id": "PV-2025-005",
            "employee_name": "Hina Raza",
            "expense_category": "Other",
        }],
    )
    workspace = workspaces.load_workspace(ws.id)
    test = _test(workspace)
    units = document_test_units(
        workspaces.load_workspace(ws.id), test["id"], forced=False, title=test["title"]
    )
    by_document = {unit.parent_refs[-2].split(":")[1]: unit for unit in units}

    silent = _bundle(workspaces.load_workspace(ws.id), test, by_document[partial])
    reading = next(
        item.content for item in silent.items if item.source_id == "document_reading"
    )
    assert reading["missing_fields"] == ["expense_description"]
    assert "document_pages" in {item.source_id for item in silent.items}

    other = next(unit for key, unit in by_document.items() if key != partial)
    complete = _bundle(workspaces.load_workspace(ws.id), test, other)
    assert "document_pages" not in {item.source_id for item in complete.items}


# --------------------------------------------------------------------- worker
def _request(bundle, test):
    return WorkerRequest(
        worker_id="fieldwork.document_qa",
        capability_id=CAPABILITY_ID,
        unit_id=bundle.unit_id,
        context=bundle,
        activity={"artifact_refs": [f"doctest:{test['id']}"]},
    )


def test_the_worker_answers_from_a_reading_and_cites_a_field(ws):
    test = _test(ws)
    units = document_test_units(
        workspaces.load_workspace(ws.id), test["id"], forced=False, title=test["title"]
    )
    bundle = _bundle(workspaces.load_workspace(ws.id), test, units[0])
    gateway = _Gateway(
        json.dumps(
            {
                "answer": "Client meeting travel is a permitted transport expense.",
                "conclusion": "Classification agrees with the SOP.",
                "control_conclusion": "effective",
                "outcome": "accepted",
                "citations": [{"field": "expense_description"}],
            }
        )
    )

    result = WORKERS.execute(_request(bundle, test), gateway)

    system, user, _attempt = gateway.calls[0]
    assert system == DOCUMENT_QA_SYSTEM
    assert "Record:" in user
    assert "Criteria:" in user
    assert "Included document pages" not in user
    assert [dict(item) for item in result.proposal["citations"]] == [
        {"field": "expense_description"}
    ]


def test_a_citation_to_a_field_the_reading_never_stated_is_dropped(ws):
    test = _test(ws)
    units = document_test_units(
        workspaces.load_workspace(ws.id), test["id"], forced=False, title=test["title"]
    )
    bundle = _bundle(workspaces.load_workspace(ws.id), test, units[0])
    gateway = _Gateway(
        json.dumps(
            {
                "answer": "Approved by the head of department.",
                "control_conclusion": "effective",
                "outcome": "accepted",
                "citations": [{"field": "approved_by"}],
            }
        )
    )

    result = WORKERS.execute(_request(bundle, test), gateway)

    # Unbound citation dropped, and an uncited accept becomes a manual check
    # rather than a verdict resting on nothing.
    assert result.proposal["citations"] == ()
    assert result.proposal["outcome"] == "needs_manual_check"


def test_a_field_citation_resolves_to_the_page_the_reading_read_it_from(ws):
    test = _test(ws)
    unit = document_population.assessment_units(test["items"][0])[0]

    anchor = document_population.citation_anchor(ws, unit["document_id"], "c1")

    assert anchor == {"page": 1, "excerpt": "voucher text"}
