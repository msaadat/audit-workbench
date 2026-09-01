"""The induction pass end to end: sample, union, reconcile, freeze."""

from __future__ import annotations

import pytest

from app import document_analysis, document_classification as dc
from app import document_schemas, documents, llm, workspaces
from app.agent import runner
from app.agent.capabilities import documents as document_capabilities
from app.agent.workflows import documents as documents_workflow
from conftest import FakeAgentLLM, wait_run

CLASSIFY_TAG = "agent:document_classification"
SAMPLE_TAG = "agent:document_schema_sample"
RECONCILE_TAG = "agent:document_schema_reconcile"
MAP_TAG = "agent:document_analysis_map"
STRUCTURED_TAG = "agent:document_analysis_structured"


def _field(name: str, **overrides) -> dict:
    field = {
        "name": name,
        "role": "attribute",
        "value_type": "text",
        "cardinality": "one",
        "verbatim": True,
        "confidence": "high",
        "label": name.replace("_", " ").title(),
    }
    field.update(overrides)
    return field


def _workspace(count: int = 2):
    ws = workspaces.create_workspace("Induction workflow")
    created = [
        documents.add_document(
            ws,
            f"invoice-{index}.txt",
            f"Invoice No. INV-104{index}\nTotal Due USD {index}00.00".encode(),
            # Transaction evidence, because that is what induction expands
            # over: a schema describes the fields a *voucher* carries, and
            # planning material keeps its narrative analysis instead.
            category="evidence",
        )
        for index in range(count)
    ]
    return ws, created


def _fake(monkeypatch, sample_responses, *, reconcile=None):
    """A model that names every document a vendor invoice, then answers samples."""

    calls = {"sample": 0}

    def sample(_messages):
        index = min(calls["sample"], len(sample_responses) - 1)
        calls["sample"] += 1
        return {"fields": sample_responses[index]}

    overrides = {
        CLASSIFY_TAG: {
            "document_type": "vendor_invoice",
            "document_type_other": "",
            "confidence": "high",
            "rationale": "The header reads Invoice.",
        },
        SAMPLE_TAG: sample,
        MAP_TAG: {
            "summary_markdown": "An invoice. [1]",
            "audit_notes_markdown": "Nothing is demonstrated by the invoice alone.",
            "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
        },
        # Once a schema exists the documents route to the structured profile,
        # which is the point of inducing one. These tests are about induction,
        # so the record states its fact under additional_fields: that keeps them
        # independent of whichever fields a case happened to induce, while
        # still stating a record. It cannot be an empty extraction — induction
        # read these documents' own fields, so returning nothing for them is the
        # contradiction ``validate_structured_proposal`` now refuses.
        STRUCTURED_TAG: {
            "records": [
                {
                    "fields": [],
                    "additional_fields": [
                        {
                            "name": "stated_reference",
                            "value_type": "identifier",
                            "value": "INV-1040",
                            "entry": 1,
                            "citation": "1",
                        }
                    ],
                }
            ],
            "audit_notes": [],
            "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
        },
    }
    if reconcile is not None:
        overrides[RECONCILE_TAG] = reconcile
    fake = FakeAgentLLM(overrides)
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    return fake


def _run(ws, created):
    run = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyse the documents.",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{item['id']}" for item in created],
        },
        context={"document_ids": [item["id"] for item in created], "action": "analyze"},
    )
    return wait_run(ws, run["id"])


def _tags(fake) -> list[str]:
    return [call["tag"] for call in fake.calls]


# --------------------------------------------------------------- agreement
def test_agreeing_samples_freeze_a_schema_without_a_reconciliation_call(monkeypatch):
    """Agreement is the common case, and it must cost no extra model turn."""

    ws, created = _workspace()
    fake = _fake(monkeypatch, [
        [_field("invoice_number", role="identifier", value_type="identifier"),
         _field("total_amount", value_type="number")],
        [_field("invoice_number", role="identifier", value_type="identifier"),
         _field("currency")],
    ])
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    assert RECONCILE_TAG not in _tags(fake)

    reloaded = workspaces.load_workspace(ws.id)
    schema = document_schemas.get_schema(reloaded, "vendor_invoice")
    # Unioned, not intersected: `currency` was seen by one sample only.
    assert [field["name"] for field in schema["fields"]] == [
        "currency", "invoice_number", "total_amount",
    ]
    assert schema["low_confidence"] is False
    assert schema["reconciled"] is False
    assert len(schema["derived_from"]) == 2


def test_each_sample_is_read_independently(monkeypatch):
    """One call per sample. Handing a worker every sample at once would produce a
    tidier answer and destroy the only signal worth having."""

    ws, created = _workspace(count=2)
    fake = _fake(monkeypatch, [
        [_field("invoice_number", role="identifier", value_type="identifier")],
        [_field("invoice_number", role="identifier", value_type="identifier")],
    ])
    _run(ws, created)
    assert _tags(fake).count(SAMPLE_TAG) == 2


# --------------------------------------------------------------- conflict
def test_a_disputed_field_is_reconciled_and_recorded_as_such(monkeypatch):
    ws, created = _workspace()
    fake = _fake(
        monkeypatch,
        [
            [_field("reference", role="identifier", value_type="identifier")],
            [_field("reference", role="attribute", value_type="text")],
        ],
        reconcile={
            "resolutions": [
                {"name": "reference", "attribute": "role", "value": "identifier",
                 "reason": "It ties the invoice to its order."},
                {"name": "reference", "attribute": "value_type", "value": "identifier",
                 "reason": "It is a reference, not prose."},
            ]
        },
    )
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    assert _tags(fake).count(RECONCILE_TAG) == 1

    reloaded = workspaces.load_workspace(ws.id)
    schema = document_schemas.get_schema(reloaded, "vendor_invoice")
    assert schema["reconciled"] is True
    field = next(item for item in schema["fields"] if item["name"] == "reference")
    assert field["role"] == "identifier"
    assert field["value_type"] == "identifier"


# --------------------------------------------------------------- readiness
def test_a_second_run_reuses_the_induced_schema(monkeypatch):
    ws, created = _workspace()
    fake = _fake(monkeypatch, [
        [_field("invoice_number", role="identifier", value_type="identifier")],
        [_field("invoice_number", role="identifier", value_type="identifier")],
    ])
    _run(ws, created)
    before = _tags(fake).count(SAMPLE_TAG)

    reloaded = workspaces.load_workspace(ws.id)
    second = _run(reloaded, created)

    assert second["status"] == "completed"
    assert _tags(fake).count(SAMPLE_TAG) == before
    assert "documents.schemas_induced" in second["workflow"]["reused_capabilities"]


def test_a_workspace_of_unidentified_documents_induces_nothing(monkeypatch):
    """Nothing to induce is satisfied, not blocked: the gap is a classification
    gap, and that capability is what reports it."""

    ws, created = _workspace()
    fake = FakeAgentLLM({
        CLASSIFY_TAG: {
            "document_type": "other", "document_type_other": "Unclear",
            "confidence": "low", "rationale": "Nothing matched.",
        },
        MAP_TAG: {
            "summary_markdown": "Text. [1]",
            "audit_notes_markdown": "Nothing is demonstrated.",
            "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
        },
    })
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    assert SAMPLE_TAG not in _tags(fake)
    reloaded = workspaces.load_workspace(ws.id)
    assert document_schemas.list_schemas(reloaded) == []
    assert dc.types_awaiting_schema(reloaded) == []


def test_planning_material_is_neither_classified_nor_induced(monkeypatch):
    """The shape of a real procurement engagement: vouchers alongside an
    approval matrix and a set of minutes.

    Classification, induction and structured extraction all expand over
    transaction evidence and nothing else, so only the voucher is typed. An
    approval matrix genuinely *is* a ``delegation_of_authority``, but the label
    is inert on it: it cannot fill a cycle role, induction skips it, and an RCM
    comparison naming ``{document_type, field}`` needs a schema it will never
    have. Asking cost a model call nothing read.

    The planning documents keep the written narrative the APM selectors consume,
    which is the half that has to survive the gate.
    """

    ws = workspaces.create_workspace("Procurement")
    created = [
        documents.add_document(
            ws, "invoice.txt",
            b"Invoice No. INV-1042\nTotal Due USD 12,480.00",
            category="evidence",
        ),
        documents.add_document(
            ws, "approval-matrix.txt",
            b"Financial Approval Matrix\nManager: up to USD 10,000.",
            category="policy",
        ),
        documents.add_document(
            ws, "minutes.txt",
            b"Minutes of Meeting\nThe committee approved the procurement plan.",
            category="minutes",
        ),
    ]

    HEADINGS = ("Financial Approval Matrix", "Minutes of Meeting", "Invoice No.")

    def classify(messages):
        text = str(messages)
        if "Approval Matrix" in text:
            type_id, reason = "delegation_of_authority", "It is an authority matrix."
        elif "Minutes of Meeting" in text:
            type_id, reason = "board_minutes", "It minutes a committee decision."
        else:
            type_id, reason = "vendor_invoice", "The header reads Invoice."
        return {
            "document_type": type_id, "document_type_other": "",
            "confidence": "high", "rationale": reason,
        }

    def narrate(messages):
        # An excerpt has to appear verbatim in the chunk it cites, which is the
        # rule the narrative worker is held to and one worth keeping here.
        text = str(messages)
        excerpt = next(
            line for line in HEADINGS if line in text
        )
        return {
            "summary_markdown": f"The document opens '{excerpt}'. [1]",
            "audit_notes_markdown": "Nothing is demonstrated by it alone.",
            "citations": [{"id": "1", "page": 1, "excerpt": excerpt}],
        }

    fake = FakeAgentLLM({
        CLASSIFY_TAG: classify,
        SAMPLE_TAG: {
            "fields": [_field("invoice_number", role="identifier",
                              value_type="identifier")]
        },
        MAP_TAG: narrate,
        # The invoice is the only document induction sampled, so it is the only
        # one routed here — and it must state its record for the same reason as
        # above. The two planning documents never reach this worker at all.
        STRUCTURED_TAG: {
            "records": [
                {
                    "fields": [],
                    "additional_fields": [
                        {
                            "name": "stated_reference",
                            "value_type": "identifier",
                            "value": "INV-1040",
                            "entry": 1,
                            "citation": "1",
                        }
                    ],
                }
            ],
            "audit_notes": [],
            "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
        },
    })
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    reloaded = workspaces.load_workspace(ws.id)

    assert dc.types_present(reloaded) == ["vendor_invoice"]
    assert [record["document_type"] for record in
            document_schemas.list_schemas(reloaded)] == ["vendor_invoice"]
    assert _tags(fake).count(SAMPLE_TAG) == 1

    # The gate is what saves the turn, so count it rather than inferring it from
    # the absence of a type: one classification call for one voucher.
    assert _tags(fake).count(CLASSIFY_TAG) == 1
    assert not dc.is_classified(reloaded, created[1]["id"])
    assert not dc.is_classified(reloaded, created[2]["id"])

    # Readiness is measured over the same set, so an engagement whose prose is
    # untyped is complete rather than permanently part-classified.
    assert dc.unclassified_ids(reloaded) == []
    assert dc.summary(reloaded)["documents"] == 1

    # Each planning document was read as prose and carries a written summary,
    # which is the form the planning selectors consume.
    for document in created[1:]:
        assert document_capabilities.analysis_profile(
            reloaded, document["id"]
        ) == "standard"
        analysis = document_analysis.load_analysis(reloaded, document["id"])
        generated = analysis["generated"]
        assert generated["analysis_profile"] == "standard"
        assert generated["summary_markdown"].strip()


def test_a_single_document_type_freezes_low_confidence(monkeypatch):
    """One sample cannot corroborate anything, but a real schema still beats
    none — the escape-rate metric is what catches it later."""

    ws, created = _workspace(count=1)
    _fake(monkeypatch, [
        [_field("invoice_number", role="identifier", value_type="identifier")],
    ])
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    reloaded = workspaces.load_workspace(ws.id)
    assert document_schemas.get_schema(reloaded, "vendor_invoice")["low_confidence"] is True


# ----------------------------------------- what the extraction is actually sent
def test_the_structured_extraction_is_sent_the_chunk_it_must_quote(monkeypatch):
    """It was sent 68 bytes of identifiers and no text at all.

    The worker is told to extract what the chunk states and to quote it
    character for character, and its user turn carried only document id, chunk
    id and page. An empty records array was then the only honest answer
    available to it — which is what five vouchers returned on a live run, and
    what got stored as their completed structured analysis.
    """

    ws, created = _workspace(count=1)
    fake = _fake(monkeypatch, [
        [_field("invoice_number", role="identifier", value_type="identifier")],
    ])
    _run(ws, created)

    sent = [call for call in fake.calls if call["tag"] == STRUCTURED_TAG]
    assert sent, "the structured worker never ran"
    user = "\n".join(
        str(message.get("content"))
        for call in sent
        for message in call["messages"]
        if message.get("role") == "user"
    )
    assert "Invoice No. INV-1040" in user
    assert "RAW SOURCE CHUNK" in user


def test_the_structured_extraction_is_shape_enforced_by_the_provider(monkeypatch):
    """Prose asking for JSON is what the analysis workers stopped relying on.

    A forced submission call constrains the shape at the provider instead, so a
    bare token where a value belongs cannot be returned at all. Field names are
    an enum of this type's own fields, which retires "names a field this type
    does not carry" as something the model is able to do.
    """

    ws, created = _workspace(count=1)
    fake = _fake(monkeypatch, [
        [_field("invoice_number", role="identifier", value_type="identifier")],
    ])
    _run(ws, created)

    call = next(call for call in fake.calls if call["tag"] == STRUCTURED_TAG)
    assert call["tool_choice"]["function"]["name"] == "submit_structured_extraction"
    parameters = call["tools"][0]["function"]["parameters"]
    assert parameters["required"] == ["records", "citations", "audit_notes"]
    assert parameters["additionalProperties"] is False
    stated = parameters["properties"]["records"]["items"]["properties"]["fields"]
    assert stated["items"]["properties"]["name"]["enum"] == ["invoice_number"]
    # The escape hatch keeps a free name: it exists for facts the schema has no
    # room for, so constraining it to the schema would close the hatch.
    escaped = parameters["properties"]["records"]["items"]["properties"][
        "additional_fields"
    ]
    assert "enum" not in escaped["items"]["properties"]["name"]
