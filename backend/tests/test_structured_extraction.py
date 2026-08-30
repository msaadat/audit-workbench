"""Schema-guided extraction: the contract, the escape hatch, and the stamp."""

from __future__ import annotations

import pytest

from app import document_analysis, document_classification as dc
from app import document_schemas, documents, llm, workspaces
from app.agent import runner
from app.agent.capabilities import documents as document_capabilities
from app.agent.workers.documents import (
    _structured_response_schema,
    schema_descriptor,
    validate_structured_proposal,
)
from app.agent.workers.model import WorkerResponseValidationError
from app.agent.workflows import documents as documents_workflow
from conftest import FakeAgentLLM, wait_run

CLASSIFY_TAG = "agent:document_classification"
SAMPLE_TAG = "agent:document_schema_sample"
STRUCTURED_TAG = "agent:document_analysis_structured"
VOUCHER_TAG = "agent:document_analysis_voucher"

SCHEMA_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "party_name", "role": "party", "value_type": "text",
     "cardinality": "many", "verbatim": True, "confidence": "high"},
    {"name": "approval", "role": "control", "value_type": "text",
     "cardinality": "one", "verbatim": False, "confidence": "medium"},
]


class _Request:
    def __init__(self, **unit_input):
        self.unit_input = unit_input


def _request():
    return _Request(schema_fields=SCHEMA_FIELDS)


def _proposal(fields, additional=(), citations=("c1",)):
    return {
        "records": [{"fields": list(fields), "additional_fields": list(additional)}],
        "citations": [{"id": value, "page": 1, "excerpt": "x"} for value in citations],
    }


# ------------------------------------------------------------- the descriptor
def test_the_descriptor_states_role_cardinality_and_interpretiveness():
    """It goes into the prompt verbatim, which is what keeps the staleness
    interlock exact — a re-derived schema moves this text and the prompt hash."""

    text = schema_descriptor("vendor_invoice", SCHEMA_FIELDS)
    assert "DOCUMENT TYPE vendor_invoice" in text
    assert "invoice_number — identifier; role identifier" in text
    assert "may appear more than once" in text
    assert "interpretive; needs no excerpt" in text


# ------------------------------------------------------------ response schema
def test_a_value_is_taken_as_printed():
    parsed = _structured_response_schema(
        '{"records": [{"fields": [{"name": "invoice_number", "entry": 1,'
        ' "value": "INV-1042", "citation": "c1"}]}]}'
    )
    field = parsed["records"][0]["fields"][0]
    assert field["value"] == "INV-1042"
    # Normalization is applied server-side; the model is never asked for it.
    assert "normalized_value" not in field
    assert parsed["analysis_profile"] == "structured"


def test_an_empty_records_array_is_a_complete_answer():
    """A page of prose inside a transaction document states no record, and
    saying so is truthful rather than a gap."""

    assert _structured_response_schema('{"records": []}')["records"] == []


def test_a_record_stating_nothing_is_rejected():
    with pytest.raises(WorkerResponseValidationError, match="states nothing"):
        _structured_response_schema('{"records": [{"fields": [], "additional_fields": []}]}')


def test_a_valueless_field_is_rejected():
    with pytest.raises(WorkerResponseValidationError, match="needs a value"):
        _structured_response_schema(
            '{"records": [{"fields": [{"name": "x", "value": ""}]}]}'
        )


# --------------------------------------------------------- semantic contract
def test_a_field_outside_the_schema_is_refused_with_the_remedy_named():
    """A value under a field the schema does not state cannot be read back by any
    rule written against that schema — a silent loss, not an extra."""

    with pytest.raises(WorkerResponseValidationError, match="Report it under additional_fields"):
        validate_structured_proposal(
            _proposal([{"name": "vat_amount", "entry": 1, "value": "9", "citation": "c1"}]),
            _request(),
        )


def test_a_schema_field_may_not_be_smuggled_through_the_escape_hatch():
    with pytest.raises(WorkerResponseValidationError, match="report it under fields"):
        validate_structured_proposal(
            _proposal(
                [],
                additional=[{"name": "invoice_number", "entry": 1, "value": "INV-1",
                             "value_type": "identifier", "citation": "c1"}],
            ),
            _request(),
        )


def test_a_single_cardinality_field_may_not_carry_a_second_entry():
    with pytest.raises(WorkerResponseValidationError, match="appears once on this type"):
        validate_structured_proposal(
            _proposal([{"name": "invoice_number", "entry": 2, "value": "INV-1",
                        "citation": "c1"}]),
            _request(),
        )


def test_a_many_cardinality_field_may():
    assert validate_structured_proposal(
        _proposal([
            {"name": "party_name", "entry": 1, "value": "Alpha", "citation": "c1"},
            {"name": "party_name", "entry": 2, "value": "Beta", "citation": "c1"},
        ]),
        _request(),
    )


def test_a_stated_value_needs_a_citation():
    with pytest.raises(WorkerResponseValidationError, match="needs a citation"):
        validate_structured_proposal(
            _proposal([{"name": "invoice_number", "entry": 1, "value": "INV-1",
                        "citation": ""}]),
            _request(),
        )


def test_an_interpretive_field_needs_none():
    """Demanding a quote for a value the record never prints is unsatisfiable."""

    assert validate_structured_proposal(
        _proposal([{"name": "approval", "entry": 1, "value": "approved", "citation": ""}]),
        _request(),
    )


def test_a_citation_that_was_never_declared_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="not a\\s+citation you declared"):
        validate_structured_proposal(
            _proposal([{"name": "invoice_number", "entry": 1, "value": "INV-1",
                        "citation": "c9"}]),
            _request(),
        )


def test_an_additional_field_still_needs_a_citation():
    with pytest.raises(WorkerResponseValidationError, match="Additional field"):
        validate_structured_proposal(
            _proposal(
                [],
                additional=[{"name": "vat_amount", "entry": 1, "value": "9",
                             "value_type": "number", "citation": ""}],
            ),
            _request(),
        )


# ----------------------------------------------------------------- routing
def _workspace(category: str = "voucher"):
    ws = workspaces.create_workspace("Structured extraction")
    document = documents.add_document(
        ws, "invoice.txt",
        b"Invoice No. INV-1042\nVendor: Alpha Supplies\nTotal Due USD 1,000.00",
        category=category,
    )
    return ws, document


def test_a_schema_makes_transaction_evidence_structured():
    ws, document = _workspace()
    assert document_capabilities.analysis_profile(ws, document["id"]) == "standard"

    dc.assign(ws, document["id"], "vendor_invoice", assigned_by="model")
    document_schemas.save_schema(ws, "vendor_invoice", SCHEMA_FIELDS)
    assert document_capabilities.analysis_profile(ws, document["id"]) == "structured"


def test_planning_material_stays_prose_even_where_its_type_has_a_schema():
    """The same document and the same schema as above; only the category
    differs, and that is the whole gate.

    It matters more than a wasted extraction: a structured document's summary is
    rendered from its records rather than written, and the planning selectors
    read exactly that summary. Routing policy material to the structured profile
    replaces the narrative planning consumes with a record dump.
    """

    ws, document = _workspace(category="policy")
    dc.assign(ws, document["id"], "vendor_invoice", assigned_by="model")
    document_schemas.save_schema(ws, "vendor_invoice", SCHEMA_FIELDS)
    assert document_capabilities.analysis_profile(ws, document["id"]) == "standard"


def test_a_type_without_a_schema_is_read_as_prose():
    """Readable and citable, and not cycle evidence — which is the honest
    description of a transaction document nothing has induced fields for."""

    ws, document = _workspace()
    dc.assign(ws, document["id"], "vendor_invoice", assigned_by="model")
    assert document_capabilities.analysis_profile(ws, document["id"]) == "standard"


def test_an_unclassified_document_is_unaffected():
    ws, document = _workspace()
    assert document_capabilities.analysis_profile(ws, document["id"]) == "standard"


# --------------------------------------------------------------- end to end
def test_extraction_runs_against_the_schema_and_carries_its_stamp(monkeypatch):
    ws, document = _workspace()
    fake = FakeAgentLLM({
        CLASSIFY_TAG: {"document_type": "vendor_invoice", "document_type_other": "",
                       "confidence": "high", "rationale": "Header reads Invoice."},
        SAMPLE_TAG: {"fields": SCHEMA_FIELDS},
        STRUCTURED_TAG: {
            "records": [{
                "fields": [
                    {"name": "invoice_number", "entry": 1, "value": "INV-1042",
                     "citation": "c1"},
                    {"name": "party_name", "entry": 1, "value": "Alpha Supplies",
                     "citation": "c1"},
                ],
                "additional_fields": [
                    {"name": "vat_amount", "value_type": "number", "entry": 1,
                     "value": "150.00", "citation": "c1"},
                ],
            }],
            "audit_notes": ["No approval signature is present."],
            "citations": [{"id": "c1", "page": 1, "excerpt": "Invoice No. INV-1042"}],
        },
    })
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    run = runner.start_command_run(
        ws, "auto",
        {
            "source": "tab_button", "text": "Analyse.",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{document['id']}"],
        },
        context={"document_ids": [document["id"]], "action": "analyze"},
    )
    finished = wait_run(ws, run["id"])
    assert finished["status"] == "completed"
    assert VOUCHER_TAG not in [call["tag"] for call in fake.calls]

    reloaded = workspaces.load_workspace(ws.id)
    artifact = document_analysis.load_analysis(reloaded, document["id"])["effective"]
    assert artifact["analysis_profile"] == "structured"
    assert artifact["schema_ref"]["document_type"] == "vendor_invoice"
    assert document_schemas.is_current(reloaded, artifact["schema_ref"])

    record = artifact["records"][0]
    assert {field["name"] for field in record["fields"]} == {
        "invoice_number", "party_name",
    }
    # The escape hatch survives into the stored record, which is what the
    # escape-rate metric later reads.
    assert record["additional_fields"][0]["name"] == "vat_amount"
    assert "INV-1042" in artifact["summary_markdown"]

    measured = document_schemas.escape_rate(reloaded, "vendor_invoice", [artifact])
    assert measured["fields"][0]["name"] == "vat_amount"
    assert measured["rate"] == 1.0


# ------------------------------------------- the empty extraction that was not
# Found by a live run, not by this suite. Three vouchers — a purchase
# requisition, a vendor invoice and a goods receipt — were stored as analysed,
# `analysis_profile: structured`, a valid `schema_ref` stamped, and zero
# records. Two of the three returned the empty envelope on the first attempt
# with no error at all; the third gave up on repair and returned it. The run
# reported success and the documents contributed no evidence to anything.

def _sampled_request(**overrides):
    """A chunk that is the whole of a document induction read its fields from."""

    values = {
        "schema_fields": SCHEMA_FIELDS,
        "document_type": "vendor_invoice",
        "schema_sampled_this_document": True,
        "sole_chunk": True,
    }
    values.update(overrides)
    return _Request(**values)


def test_an_empty_extraction_of_a_sampled_document_is_refused():
    """Induction read this document's fields; nothing to extract contradicts it."""

    with pytest.raises(WorkerResponseValidationError, match="induced\\s+from"):
        validate_structured_proposal({"records": [], "citations": []},
                                     _sampled_request())


def test_an_empty_extraction_is_a_complete_answer_for_any_other_document():
    """A page of prose inside a transaction document states no record, and
    reporting that as a coverage gap would invent a hole extraction never had."""

    assert validate_structured_proposal(
        {"records": [], "citations": []},
        _sampled_request(schema_sampled_this_document=False),
    ) is not None


def test_a_further_page_may_be_empty_even_on_a_sampled_document():
    """The contradiction needs both halves: only where this chunk is the whole
    document is there no other page the records could be on."""

    assert validate_structured_proposal(
        {"records": [], "citations": []},
        _sampled_request(sole_chunk=False),
    ) is not None


def test_a_sampled_document_stating_a_record_passes():
    assert validate_structured_proposal(
        _proposal([{"name": "invoice_number", "entry": 1, "value": "INV-1",
                    "citation": "c1"}]),
        _sampled_request(),
    )


# ------------------------------------------- readiness and the superseded stamp
def test_a_superseded_schema_stamp_makes_an_analysis_unusable():
    """Found on a live engagement, where it cost the whole cycle evidence set.

    Five schemas were re-derived, so every voucher extraction was stamped
    against a superseded version. ``documents.analysis_generated`` still
    reported satisfied, every capability was reused, no unit expanded, and the
    run completed having left no usable cycle evidence. The interlock that
    re-generates a chunk when its schema moves — the descriptor changing the
    unit's input hash — never ran, because the capability was reused whole
    before any unit did.
    """

    from app import document_analysis, document_classification, document_schemas
    from app import documents, workspaces
    from app.agent.capabilities.documents import (
        has_generated_analysis,
        has_usable_analysis,
    )

    ws = workspaces.create_workspace("Superseded stamp")
    document = documents.add_document(
        ws, "invoice.txt", b"Invoice No. INV-1042\nTotal Due USD 100.00",
        category="voucher",
    )
    # The type is part of the fixture, not scenery: an extraction is stamped
    # with the schema of whatever the document was classified as, and usability
    # asks whether that is still the document's type. Nothing reaches the
    # structured profile unclassified, so a fixture that did would be testing a
    # state the workflow cannot produce.
    document_classification.assign(
        ws, str(document["id"]), "vendor_invoice", assigned_by="model"
    )
    document_schemas.save_schema(ws, "vendor_invoice", [
        {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ])
    ws = workspaces.load_workspace(ws.id)
    schema = document_schemas.load_schema(ws, "vendor_invoice")

    document_analysis.persist_analysis(
        ws, document,
        {"pages": [{"page": 1, "text": "Invoice No. INV-1042"}]},
        {
            "summary_markdown": "An invoice.",
            "audit_notes_markdown": "Structured evidence.",
            "analysis_profile": "structured",
            "schema_ref": {
                "document_type": "vendor_invoice",
                "schema_version": schema["schema_version"],
                "schema_hash": schema["schema_hash"],
            },
            "records": [],
            "citations": [],
        },
        provider="test", model="test",
    )
    ws = workspaces.load_workspace(ws.id)
    assert has_usable_analysis(ws, document["id"]) is True

    # The schema gains a field, so the stamp the extraction carries is superseded.
    document_schemas.save_schema(ws, "vendor_invoice", [
        {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
        {"name": "total_amount", "role": "attribute", "value_type": "number",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ])
    ws = workspaces.load_workspace(ws.id)

    # The analysis still exists — that question is unchanged, and an auditor
    # still reviews it. What changed is whether it can serve as evidence.
    assert has_generated_analysis(ws, document["id"]) is True
    assert has_usable_analysis(ws, document["id"]) is False


def test_a_narrative_analysis_is_unaffected_by_any_schema():
    """It carries no stamp, so there is nothing for a schema to supersede."""

    from app import document_analysis, document_schemas, documents, workspaces
    from app.agent.capabilities.documents import has_usable_analysis

    ws = workspaces.create_workspace("Narrative unaffected")
    document = documents.add_document(
        ws, "policy.txt", b"Procurement policy\nApprovals are required.",
        category="policy",
    )
    document_analysis.persist_analysis(
        ws, document,
        {"pages": [{"page": 1, "text": "Procurement policy"}]},
        {
            "summary_markdown": "A policy. [1]",
            "audit_notes_markdown": "Nothing is demonstrated by it alone.",
            "citations": [{"id": "1", "page": 1, "excerpt": "Procurement policy"}],
        },
        provider="test", model="test",
    )
    document_schemas.save_schema(ws, "vendor_invoice", [
        {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ])
    ws = workspaces.load_workspace(ws.id)
    assert has_usable_analysis(ws, document["id"]) is True
