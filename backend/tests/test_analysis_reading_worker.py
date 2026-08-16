"""The ``analysis.reading`` worker contract.

One turn reads the whole engagement, and the only thing it does that no later
stage can undo is decline a measurement. The validator is therefore deliberately
asymmetric, and that asymmetry is what these tests hold: a malformed *keep*
costs an entry its better name, a malformed *decline* costs a repair turn.
"""

from __future__ import annotations

import pytest

import polars as pl

from app import workspaces
from app.agent.capabilities import ANALYSIS_REGISTRY
from app.agent.context import ContextResolver, analysis_reading_scope
from app.agent.workers import analysis as analysis_worker
from app.agent.workers.model import WorkerRequest, WorkerResponseValidationError


@pytest.fixture
def reading_request() -> WorkerRequest:
    """A real resolved bundle: two frames and two measured nominations."""
    ws = workspaces.create_workspace("Reading worker")
    ws.add_table(
        "invoices.csv",
        pl.DataFrame(
            {
                "invoice_no": ["A", "B", "C"],
                "amount": [1.0, 2.0, 3.0],
                "po_link": ["P1", "P2", "P3"],
            }
        )
        .write_csv()
        .encode(),
    )
    ws.add_table(
        "orders.csv",
        pl.DataFrame({"po_number": ["P1", "P2", "P3"], "total": [9.0, 9.0, 9.0]})
        .write_csv()
        .encode(),
    )
    capability = ANALYSIS_REGISTRY.get("analysis.register_ready")
    _manifest, bundle = ContextResolver().resolve(
        ws,
        capability,
        {"id": "analysis_reading"},
        analysis_reading_scope(
            ws,
            ["invoices", "orders"],
            nominations=[
                {
                    "ref": "N01",
                    "frame": "invoices",
                    "test": "duplicates",
                    "params": {"columns": ["invoice_no"]},
                    "flagged": 3,
                    "tested": 3,
                    "reading": "invoice_no repeats on 3 rows",
                },
                {
                    "ref": "N02",
                    "frame": "orders",
                    "test": "referential",
                    "params": {
                        "column": "po_number",
                        "lookup_table": "invoices",
                        "lookup_column": "po_link",
                    },
                    "flagged": 5,
                    "tested": 5,
                    "reading": "5 of 5 po_number values do not exist",
                },
            ],
        ),
    )
    return WorkerRequest(
        worker_id=analysis_worker.ANALYSIS_READING_WORKER_ID,
        capability_id=capability.id,
        unit_id="analysis_reading",
        context=bundle,
    )


@pytest.fixture
def validate(reading_request):
    def run(proposal: dict) -> dict:
        return analysis_worker.validate_reading_proposal(proposal, reading_request)

    return run


def _empty() -> dict:
    return {"keep": [], "add": [], "decline": [], "unanswerable": []}


def test_an_empty_answer_is_valid_because_the_floor_already_stands(validate):
    """A turn that adds nothing has still read the map, and the register it was
    handed is complete. Charging a repair turn for that would be charging for
    compliance."""
    assert validate(_empty()) == _empty()


def test_a_decline_naming_no_supplied_nomination_is_rejected(validate):
    with pytest.raises(WorkerResponseValidationError) as error:
        validate(
            {**_empty(), "decline": [{"ref": "N99", "reason": "x" * 30}]}
        )
    assert "names no supplied nomination" in str(error.value)


def test_a_decline_with_no_reason_is_rejected(validate):
    with pytest.raises(WorkerResponseValidationError) as error:
        validate({**_empty(), "decline": [{"ref": "N01", "reason": "   "}]})
    assert "no reason" in str(error.value)


def test_a_malformed_keep_is_dropped_rather_than_repaired(validate):
    """The asymmetry. A keep that names nothing usable loses only the better
    name it was offering — the nomination survives on the floor either way — so
    spending the one repair turn on it would buy nothing."""
    settled = validate(
        {
            **_empty(),
            "keep": [
                {"ref": "N99", "title": "x", "note": "y"},
                {"ref": "N01", "title": "", "note": "y"},
                {"ref": "N02", "title": "Real name", "note": "What it means."},
            ],
        }
    )
    assert settled["keep"] == [
        {"ref": "N02", "title": "Real name", "note": "What it means."}
    ]


def test_one_nomination_cannot_be_both_kept_and_declined(validate):
    settled = validate(
        {
            **_empty(),
            "decline": [{"ref": "N01", "reason": "Ordinary operations, not a control."}],
            "keep": [{"ref": "N01", "title": "Name", "note": "Meaning."}],
        }
    )
    # The decline is read first and wins: a turn that argued for removing a
    # measurement has said the more consequential of the two things.
    assert [item["ref"] for item in settled["decline"]] == ["N01"]
    assert settled["keep"] == []


def test_declining_the_same_nomination_twice_is_rejected(validate):
    with pytest.raises(WorkerResponseValidationError) as error:
        validate(
            {
                **_empty(),
                "decline": [
                    {"ref": "N01", "reason": "A reason long enough to count."},
                    {"ref": "N01", "reason": "A different reason entirely here."},
                ],
            }
        )
    assert "a second time" in str(error.value)


def test_an_assertion_must_name_a_supplied_frame_and_its_own_columns(validate):
    with pytest.raises(WorkerResponseValidationError) as error:
        validate(
            {
                **_empty(),
                "add": [
                    {
                        "frame": "invoices",
                        "columns": ["amount", "nope"],
                        "assertion": "An amount never exceeds its order total.",
                        "why": "Paying above the order is the control failing.",
                    }
                ],
            }
        )
    assert "not a column of 'invoices'" in str(error.value)

    with pytest.raises(WorkerResponseValidationError) as error:
        validate(
            {
                **_empty(),
                "add": [
                    {
                        "frame": "ghost_frame",
                        "columns": ["amount"],
                        "assertion": "An amount never exceeds its order total.",
                        "why": "Paying above the order is the control failing.",
                    }
                ],
            }
        )
    assert "names no supplied frame" in str(error.value)


def test_negative_space_is_carried_through(validate):
    """The one output no result can ever represent: a control the file gives no
    column to test. It reaches the memo through the register or not at all."""
    settled = validate(
        {
            **_empty(),
            "unanswerable": [
                {
                    "question": "Was competitive bidding performed?",
                    "why": "No column in any frame records a bid or a quotation.",
                },
                {"question": "   ", "why": "dropped"},
            ],
        }
    )
    assert [item["question"] for item in settled["unanswerable"]] == [
        "Was competitive bidding performed?"
    ]


def test_the_submission_schema_makes_an_unknown_reference_unrepresentable(
    reading_request,
):
    tool = analysis_worker._reading_submission_tool(reading_request)
    properties = tool["function"]["parameters"]["properties"]
    assert properties["decline"]["items"]["properties"]["ref"]["enum"] == ["N01", "N02"]
    assert properties["keep"]["items"]["properties"]["ref"]["enum"] == ["N01", "N02"]
    assert properties["add"]["items"]["properties"]["frame"]["enum"] == [
        "invoices",
        "orders",
    ]
    assert (
        properties["add"]["maxItems"] == analysis_worker.MAX_AUTHORED_ASSERTIONS
    )


def test_a_missing_section_is_read_as_an_empty_one():
    """Providers omit empty arrays. Reading that as malformed would spend both
    attempts telling a compliant model its compliant answer was invalid — the
    exact failure the definition worker's own schema note records."""
    assert analysis_worker._reading_response_schema('{"add": []}') == _empty()


def test_a_duplicate_submission_is_read_rather_than_discarded():
    """The defect that cost run 8 its memo, held for every forced-tool worker.

    A model that submits twice has repeated itself, not contradicted itself.
    Requiring exactly one match and falling through to ``content`` — empty or
    whitespace on a tool-call response — turned that into "the response is not
    a valid JSON object: Expecting value at character 0". The run discarded two
    complete, valid memos of 11,253 and 4,708 characters and read three tab
    characters instead.
    """
    for tool, extract in (
        (analysis_worker.SUMMARY_SUBMISSION_TOOL,
         analysis_worker._summary_submission_response),
        (analysis_worker.READING_SUBMISSION_TOOL,
         analysis_worker._reading_submission_response),
        (analysis_worker.ANALYSIS_SUBMISSION_TOOL,
         analysis_worker._submission_response),
        (analysis_worker.JOIN_UTILITY_SUBMISSION_TOOL,
         analysis_worker._join_utility_submission_response),
    ):
        message = {
            "content": "\t\t\t",
            "tool_calls": [
                {"function": {"name": tool, "arguments": '{"first": true}'}},
                {"function": {"name": tool, "arguments": '{"second": true}'}},
            ],
        }
        # The first complete submission is the answer, deterministically.
        assert extract(message) == '{"first": true}', tool


def test_an_empty_first_submission_falls_through_to_the_next():
    tool = analysis_worker.SUMMARY_SUBMISSION_TOOL
    message = {
        "content": "",
        "tool_calls": [
            {"function": {"name": tool, "arguments": "   "}},
            {"function": {"name": tool, "arguments": '{"real": true}'}},
        ],
    }
    assert analysis_worker._summary_submission_response(message) == '{"real": true}'


def test_content_is_still_the_fallback_when_no_tool_call_matches():
    """A provider that ignores forced tools is still answering the question."""
    message = {"content": '{"keep": []}', "tool_calls": []}
    assert analysis_worker._reading_submission_response(message) == '{"keep": []}'
