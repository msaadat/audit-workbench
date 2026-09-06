"""The structured-record representation the chat reads documents through.

The chat could already reach a document's *prose* — search excerpts, attached
pages — and could not reach the readings the pipeline had already taken off it
against its type's frozen schema. These prove the representation that closes
that gap, and prove the two ways it can lie: a value the reading does not state
must be named rather than omitted, and a document whose reading no longer
stands must be named rather than silently dropped from a count.
"""

from __future__ import annotations

import pytest

from app import (
    assistant,
    document_analysis,
    document_classification as dc,
    document_context,
    document_schemas,
    documents,
    telemetry_db,
    workspaces,
)
from app.workspaces import WorkspaceError

VOUCHER_FIELDS = [
    {"name": "claim_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Claim ID"},
    {"name": "payee", "role": "party", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Payee"},
    {"name": "amount_paid", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Amount paid"},
    {"name": "approved_by", "role": "control", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Approved by"},
]


@pytest.fixture
def ws() -> workspaces.Workspace:
    workspace = workspaces.create_workspace("Expenses")
    document_schemas.save_schema(workspace, "payment_voucher", VOUCHER_FIELDS)
    return workspace


def extract(ws, name: str, document_type: str, *, additional=(), **values) -> str:
    """Store one structured reading the way the reduction executor would."""

    document = documents.add_document(ws, name, b"source text", category="evidence")
    dc.assign(ws, str(document["id"]), document_type, assigned_by="model")
    schema = document_schemas.get_schema(ws, document_type)
    document_analysis.persist_analysis(
        ws, document, {"pages": [{"page": 1, "text": "source text"}]},
        {
            "analysis_profile": "structured",
            "summary_markdown": "s",
            "audit_notes_markdown": "n",
            "schema_ref": {
                "document_type": schema["document_type"],
                "schema_version": schema["schema_version"],
                "schema_hash": schema["schema_hash"],
            },
            "records": [{
                "fields": [
                    {"name": key, "entry": 1, "value": value, "citation": "c1"}
                    for key, value in values.items()
                ],
                "additional_fields": [
                    {"name": key, "entry": 1, "value": value}
                    for key, value in additional
                ],
            }],
            "citations": [{
                "id": "c1", "page": 1, "excerpt": "the line it was read from",
                "excerpt_hash": "sha1:" + "0" * 40,
            }],
        },
        provider="local", model="test",
    )
    return str(document["id"])


def typed_without_reading(ws, name: str, document_type: str) -> str:
    document = documents.add_document(ws, name, b"source text", category="evidence")
    dc.assign(ws, str(document["id"]), document_type, assigned_by="model")
    return str(document["id"])


# --------------------------------------------------------------- catalogue
def test_catalogue_reports_the_vocabulary_and_the_unread_gap(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1", payee="A")
    typed_without_reading(ws, "v2.txt", "payment_voucher")

    catalog = document_context.record_type_catalog(ws)
    entry = next(item for item in catalog if item["document_type"] == "payment_voucher")

    assert entry["documents"] == 2
    assert entry["documents_with_records"] == 1
    assert entry["records"] == 1
    assert entry["schema_version"] == 1
    assert [field["name"] for field in entry["fields"]] == [
        "amount_paid", "approved_by", "claim_id", "payee",
    ]
    assert entry["fields"][2] == {
        "name": "claim_id", "label": "Claim ID",
        "role": "identifier", "value_type": "identifier",
    }
    assert entry["fields_truncated"] is False


# ------------------------------------------------------------------ reading
def test_a_reading_returns_stated_values_with_the_page_they_stand_on(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1",
            payee="Hamza Ali", amount_paid="2,850")

    result = document_context.structured_records(
        ws, document_type="payment_voucher", record_activity=False,
    )

    assert result["population_records"] == 1
    assert result["returned_records"] == 1
    assert result["truncated"] is False
    record = result["records"][0]
    assert record["fields"]["payee"] == [
        {"value": "Hamza Ali", "page": 1, "excerpt": "the line it was read from"}
    ]
    assert record["document_title"] == "v1.txt"
    assert record["record_id"].startswith("REC-")
    # The reading states three of the type's four fields, and the fourth is
    # what the question was probably about.
    assert record["missing_fields"] == ["approved_by"]


def test_a_field_outside_the_schema_is_reported_not_refused(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1")

    result = document_context.structured_records(
        ws, document_type="payment_voucher",
        fields=["claim_id", "policy_self_assessment"], record_activity=False,
    )

    assert result["unknown_fields"] == ["policy_self_assessment"]
    assert list(result["records"][0]["fields"]) == ["claim_id"]


def test_values_outside_the_schema_still_reach_the_reader(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1",
            additional=[("facilitator_note", "Ride to personal residence")])

    result = document_context.structured_records(
        ws, document_type="payment_voucher", record_activity=False,
    )

    assert result["records"][0]["additional_fields"] == [
        {"name": "facilitator_note", "value": "Ride to personal residence"}
    ]


def test_naming_documents_without_a_type_reads_their_own_vocabulary(ws):
    first = extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1", payee="A")
    extract(ws, "v2.txt", "payment_voucher", claim_id="EXP-2", payee="B")

    result = document_context.structured_records(
        ws, document_ids=[first], record_activity=False,
    )

    assert result["document_type"] is None
    assert result["returned_records"] == 1
    assert sorted(result["records"][0]["fields"]) == ["claim_id", "payee"]
    # Nothing was projected against, so nothing can be missing.
    assert result["records"][0]["missing_fields"] == []


def test_a_cap_reports_the_population_it_left_out(ws):
    for index in range(3):
        extract(ws, f"v{index}.txt", "payment_voucher", claim_id=f"EXP-{index}")

    result = document_context.structured_records(
        ws, document_type="payment_voucher", limit=2, record_activity=False,
    )

    assert result["returned_records"] == 2
    assert result["population_records"] == 3
    assert result["truncated"] is True


# ------------------------------------------------------------- the two lies
def test_a_typed_document_with_no_reading_is_named(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1")
    silent = typed_without_reading(ws, "v2.txt", "payment_voucher")

    result = document_context.structured_records(
        ws, document_type="payment_voucher", record_activity=False,
    )

    assert result["unread_documents"] == [
        {"document_id": silent, "title": "v2.txt"}
    ]


def test_a_reading_taken_against_a_moved_schema_is_absent_not_reinterpreted(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1", payee="A")
    document_schemas.save_schema(
        ws, "payment_voucher", [*VOUCHER_FIELDS,
                                {"name": "bank_reference", "role": "identifier",
                                 "value_type": "identifier", "cardinality": "one",
                                 "verbatim": True, "confidence": "high"}],
    )

    result = document_context.structured_records(
        workspaces.Workspace(ws.root), document_type="payment_voucher",
        record_activity=False,
    )

    assert result["records"] == []
    assert result["population_records"] == 0
    assert [item["title"] for item in result["unread_documents"]] == ["v1.txt"]


def test_an_id_filter_does_not_make_the_rest_of_the_type_look_unread(ws):
    first = extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1")
    extract(ws, "v2.txt", "payment_voucher", claim_id="EXP-2")

    result = document_context.structured_records(
        ws, document_type="payment_voucher", document_ids=[first],
        record_activity=False,
    )

    assert result["returned_records"] == 1
    assert result["unread_documents"] == []


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"document_type": "vendor_invoice"}, "no stamped schema"),
        ({"document_ids": ["nope"]}, "Unknown document id"),
    ],
)
def test_a_read_that_names_nothing_real_is_refused(ws, kwargs, match):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1")

    with pytest.raises(WorkspaceError, match=match):
        document_context.structured_records(ws, record_activity=False, **kwargs)


# ---------------------------------------------------------------- the tool
def test_the_tool_lists_types_when_nothing_is_named(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1")
    content, artifact = assistant._Session(ws).get_document_records({})

    assert artifact is None
    assert [item["document_type"] for item in content["types"]] == ["payment_voucher"]
    # Discovery costs no document content.
    assert "records" not in content


def test_the_tool_carries_the_catalogue_alongside_the_records(ws):
    extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1")
    content, _ = assistant._Session(ws).get_document_records(
        {"document_type": "payment_voucher"}
    )

    assert content["returned_records"] == 1
    assert [item["document_type"] for item in content["types"]] == ["payment_voucher"]


def test_reading_records_appends_content_free_provenance(ws):
    document_id = extract(ws, "v1.txt", "payment_voucher", claim_id="EXP-1")
    assistant._Session(ws).get_document_records({"document_type": "payment_voucher"})

    handle = telemetry_db.connect(ws.root)
    events = [
        telemetry_db.loads(row["payload"], {})
        for row in handle.execute("SELECT payload FROM activity_events")
    ]
    event = next(
        item for item in events if item.get("representation") == "structured_record"
    )

    assert event["purpose"] == "assistant_document_records"
    assert event["disposition"] == "context"
    assert event["document_ids"] == [document_id]
    assert event["page_ranges"] == [1]
    assert event["context_outcome"] == "supplied"
    assert event["characters_supplied"] > 0
    assert event["response_hash"] is None


def test_the_loop_may_read_records_and_still_may_not_query_tables():
    from app import assistant_tools

    assert "get_document_records" in assistant_tools.LOOP_READ_TOOL_NAMES
    assert "get_document_records" in assistant_tools.TOOL_LABELS
    assert "query_table" not in assistant_tools.LOOP_READ_TOOL_NAMES
    assert "get_document_records" in {
        schema["function"]["name"] for schema in assistant_tools.loop_tool_schemas()
    }


# ------------------------------------------------------- the artifact reads
def test_the_chat_reads_an_artifact_without_the_action_vocabulary(ws):
    from app import findings

    findings.add(ws, {"title": "Personal expense reimbursed"})
    session = assistant._Session(ws)

    listed, artifact = session.list_artifacts({"kinds": ["finding"]})

    assert artifact is None
    assert [item["kind"] for item in listed["artifacts"]] == ["finding"]
    # Read-only: what may be *done* to a finding is planner context.
    assert "operations_by_kind" not in listed
    assert "creating" not in listed

    got, _ = session.get_artifact({"ref": listed["artifacts"][0]["ref"]})

    assert got["artifact"]["title"] == "Personal expense reimbursed"
    assert got["record"]["title"] == "Personal expense reimbursed"
    assert "operations" not in got


def test_a_truncated_listing_tells_the_chat_what_it_did_not_show(ws):
    from app import findings

    for index in range(3):
        findings.add(ws, {"title": f"Finding {index}"})

    listed, _ = assistant._Session(ws).list_artifacts({"limit": 1})

    assert listed["truncated"] is True
    assert listed["kind_counts"]["finding"] == 3


def test_an_artifact_the_workspace_does_not_hold_is_refused(ws):
    with pytest.raises(WorkspaceError, match="was not found"):
        assistant._Session(ws).get_artifact({"ref": "datatest:DAT-NOPE"})


def test_the_listing_bound_is_one_number_both_callers_advertise():
    from app import tooling
    from app.agent import action_tools

    assert action_tools.MAX_ARTIFACTS == tooling.MAX_ARTIFACTS
    schema = next(
        item for item in assistant.TOOLS
        if item["function"]["name"] == "list_artifacts"
    )
    assert (
        schema["function"]["parameters"]["properties"]["limit"]["maximum"]
        == tooling.MAX_ARTIFACTS
    )


def test_the_loop_keeps_the_operations_block_the_chat_gives_up():
    """Both registries define these two names; the loop's variant must win.

    The coordinator's read-only variant drops the operations block, and
    ``ReadToolSession`` used to resolve names against the coordinator first —
    which, the moment the chat registered these, would have silently taken the
    steering loop's action vocabulary away from it.
    """

    from app import assistant_tools, findings
    from app.agent import operations

    workspace = workspaces.create_workspace("Loop reads")
    findings.add(workspace, {"title": "Duplicate invoices"})
    session = assistant_tools.ReadToolSession(workspace)

    listed = session.dispatch("list_artifacts", {"kinds": ["finding"]})
    got = session.dispatch("get_artifact", {"ref": listed["artifacts"][0]["ref"]})

    assert listed["operations_by_kind"]["finding"] == operations.artifact_operations(
        "finding"
    )
    assert listed["creating"]
    assert got["operations"] == operations.artifact_operations("finding")
    # And the wording the loop is shown is the planner's, not the chat's.
    schema = next(
        item for item in assistant_tools.loop_tool_schemas()
        if item["function"]["name"] == "get_artifact"
    )
    assert "typed ref" in schema["function"]["description"]
    assert schema["function"]["description"] != next(
        item["function"]["description"] for item in assistant.TOOLS
        if item["function"]["name"] == "get_artifact"
    )
