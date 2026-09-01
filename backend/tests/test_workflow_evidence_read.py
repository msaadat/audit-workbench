"""The evidence read end to end: one document at a time, one vocabulary per type.

4b.1's gate, in the order the plan states it:

- field drift gone — one name per fact across a type;
- no field the corpus never stated;
- a scanned approval page is read, not skipped;
- a one-document refresh does not silently re-read eighteen.

What makes the first two possible is that the reads are *not* independent. Each
document is shown what its predecessors settled on, which is only reachable
because the units are serialized and a serialized unit rebinds against committed
workspace state. The parallel path binds every unit before running any of them,
so a sibling's master could never reach it — the reads would not be wrong about
the vocabulary, they would never have been shown it.
"""

from __future__ import annotations

import pytest

from app import document_analysis, document_classification as dc
from app import document_masters, document_schemas, documents, llm, workspaces
from app.agent import runner
from app.agent.capabilities import documents as document_capabilities
from app.agent.workflows import documents as documents_workflow
from conftest import FakeAgentLLM, wait_run

CLASSIFY_TAG = "agent:document_classification"
READ_TAG = "agent:document_evidence_read"
MAP_TAG = "agent:document_analysis_map"


def _declared(name: str, **overrides) -> dict:
    field = {
        "name": name,
        "role": "attribute",
        "value_type": "text",
        "cardinality": "one",
        "verbatim": True,
        "confidence": "high",
        "label": name.replace("_", " ").title(),
        "reason": f"The document states its {name.replace('_', ' ')}.",
        # Declared once, filled on every record that states it. A statement with
        # twenty transaction lines declares its columns once and fills each of
        # them twenty times.
        "values": [{"record": 1, "entry": 1, "value": "stated", "citation": "1"}],
    }
    field.update(overrides)
    return field


def _reading(*, fields=(), new_fields=(), renames=()) -> dict:
    return {
        "records": [{"fields": list(fields)}],
        "new_fields": list(new_fields),
        "renames": list(renames),
        "audit_notes": [],
        "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
    }


def _workspace(count: int = 2):
    ws = workspaces.create_workspace("Evidence read workflow")
    created = [
        documents.add_document(
            ws,
            f"invoice-{index}.txt",
            f"Invoice No. INV-104{index}\nTotal Due USD {index}00.00".encode(),
            category="evidence",
        )
        for index in range(count)
    ]
    return ws, created


def _fake(monkeypatch, read_responses, *, classify="vendor_invoice"):
    """A model that names every document one type, then answers each reading."""

    calls = {"read": 0}

    def read(_messages):
        index = min(calls["read"], len(read_responses) - 1)
        calls["read"] += 1
        response = read_responses[index]
        return response(_messages) if callable(response) else response

    fake = FakeAgentLLM(
        {
            CLASSIFY_TAG: {
                "document_type": classify,
                "document_type_other": "",
                "confidence": "high",
                "rationale": "The header reads Invoice.",
            },
            READ_TAG: read,
            MAP_TAG: {
                "summary_markdown": "An invoice. [1]",
                "audit_notes_markdown": "Nothing is demonstrated by it alone.",
                "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
            },
        }
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    return fake


def _run(ws, created, *, action="analyze"):
    run = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyse the documents.",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{item['id']}" for item in created],
            "generation_mode": "reuse_existing" if action == "analyze" else "force",
        },
        context={"document_ids": [item["id"] for item in created], "action": action},
    )
    return wait_run(ws, run["id"])


def _tags(fake) -> list[str]:
    return [call["tag"] for call in fake.calls]


def _read_prompts(fake) -> list[str]:
    return [
        str(call["messages"][0]["content"])
        for call in fake.calls
        if call["tag"] == READ_TAG
    ]


# ------------------------------------------------------------- accumulation
def test_every_evidence_document_is_read_once(monkeypatch):
    """Not a sample. The vocabulary is what the corpus actually said, so every
    document of the type contributes to it — which is also why this pass *is*
    the extraction rather than a step before one."""

    ws, created = _workspace(count=3)
    fake = _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("invoice_number")]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1041", "citation": "1"}]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1042", "citation": "1"}]),
        ],
    )
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    assert _tags(fake).count(READ_TAG) == 3

    reloaded = workspaces.load_workspace(ws.id)
    master = document_masters.master(reloaded, "vendor_invoice")
    # In sorted unit-id order, which is what puts a type's documents in one
    # contiguous run and is never declaration order.
    assert sorted(master["documents_read"]) == sorted(
        item["id"] for item in created
    )
    assert master["fields"][0]["fill_count"] == 3


def test_the_second_document_is_shown_what_the_first_settled(monkeypatch):
    """The mechanism, pinned at the prompt.

    Per-document calls can only agree if they are not independent. Serializing
    them per type and handing each the accumulated master is what makes
    agreement possible at all — so what the second call is *sent* is the test,
    not what it happened to answer.
    """

    ws, created = _workspace(count=2)
    fake = _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("approved_by_id", role="control")]),
            _reading(fields=[{"name": "approved_by_id", "entry": 1,
                              "value": "E-4410", "citation": "1"}]),
        ],
    )
    _run(ws, created)

    first, second = _read_prompts(fake)
    assert "No document of this type has been read yet" in first
    assert "approved_by_id" not in first
    assert "approved_by_id" in second
    assert "stated by 1 of 1" in second


def test_a_synonym_cannot_enter_a_master_that_already_holds_the_fact(monkeypatch):
    """``payment_instruction`` carried ``approved_by_id`` *and*
    ``approved_by_employee_id``: same role, same value type, same fact. No
    document filled both — 14 filled one, 4 the other — and the RCM then wrote a
    segregation-of-duties assertion on one of them that evaluated on 4 of 18
    deals and reported nothing wrong. The approver was printed on all 18.

    The enum is what removes it: the second document is offered the first's
    name, so reporting its value there is the cheap path and coining a synonym
    is the expensive one.
    """

    ws, created = _workspace(count=2)
    fake = _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("approved_by_id", role="control")]),
            _reading(fields=[{"name": "approved_by_id", "entry": 1,
                              "value": "E-4410", "citation": "1"}]),
        ],
    )
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    schema = document_schemas.get_schema(reloaded, "vendor_invoice")
    assert [field["name"] for field in schema["fields"]] == ["approved_by_id"]
    # And the assertion an RCM would write against it reaches both documents.
    assert document_masters.master(reloaded, "vendor_invoice")["fields"][0][
        "fill_count"
    ] == 2

    # The enum is provider-enforced, not merely asked for.
    second = next(
        call for call in reversed(fake.calls) if call["tag"] == READ_TAG
    )
    stated = second["tools"][0]["function"]["parameters"]["properties"]["records"][
        "items"
    ]["properties"]["fields"]["items"]
    assert stated["properties"]["name"]["enum"] == ["approved_by_id"]


def test_the_first_document_of_a_type_cannot_report_a_field_at_all(monkeypatch):
    """The hole the treasury corpus found, closed structurally.

    The enum is what makes "name a field this type does not carry" impossible
    rather than something a validator catches — and for the *first* document of
    a type there is nothing to enumerate, so the constraint evaporated exactly
    where it mattered most. Measured: seven of eight first-of-type reads put
    their fields in ``records`` anyway and failed validation, and because a
    type's first read is what fills its master, the sibling behind it faced the
    same empty master and failed the same way. One repair attempt did not
    recover it.

    With nothing to enumerate the array itself is closed, so the only place a
    value can go is ``new_fields`` — which is where a first document's values
    belong anyway, because that is the channel carrying the descriptor.
    """

    from app.agent.workers.documents import _read_submission_tool

    empty = _read_submission_tool([])
    records = empty["function"]["parameters"]["properties"]["records"]
    assert records["items"]["properties"]["fields"]["maxItems"] == 0

    # And once the type carries a name, the enum takes over and the array opens.
    seeded = _read_submission_tool(["invoice_number"])
    stated = seeded["function"]["parameters"]["properties"]["records"]["items"][
        "properties"
    ]["fields"]
    assert "maxItems" not in stated
    assert stated["items"]["properties"]["name"]["enum"] == ["invoice_number"]


def test_a_multi_record_first_document_declares_once_and_fills_every_record(
    monkeypatch,
):
    """The second thing the treasury corpus found.

    A nostro statement carries one record per transaction line. The enum is
    fixed when the call is made, so a field the type does not carry yet cannot
    be named in ``records[].fields`` at all — and with ``new_fields`` carrying a
    single ``record``/``value``, the first document of a multi-record type could
    fill only its first record. The model's only way out was to declare the same
    field once per record, which the contract refuses; both bank statements
    failed, one each way.

    A field is therefore declared once and filled wherever the document states
    it. ``fill_count`` still counts *documents* — breadth is what distinguishes a
    type-level field from one record repeating it.
    """

    ws = workspaces.create_workspace("Multi-record read")
    document = documents.add_document(
        ws, "statement.txt",
        b"01 Jan  PMT-2025-00074  100.00\n02 Jan  PMT-2025-00075  250.00",
        category="evidence",
    )
    fake = _fake(
        monkeypatch,
        [
            {
                "records": [{"fields": []}, {"fields": []}],
                "new_fields": [
                    _declared(
                        "transaction_date",
                        value_type="date",
                        values=[
                            {"record": 1, "entry": 1, "value": "01 Jan",
                             "citation": "1"},
                            {"record": 2, "entry": 1, "value": "02 Jan",
                             "citation": "1"},
                        ],
                    ),
                ],
                "renames": [],
                "audit_notes": [],
                "citations": [
                    {"id": "1", "page": 1, "excerpt": "01 Jan  PMT-2025-00074"}
                ],
            }
        ],
        classify="bank_statement",
    )
    finished = _run(ws, [document])
    assert finished["status"] == "completed"

    reloaded = workspaces.load_workspace(ws.id)
    master = document_masters.master(reloaded, "bank_statement")
    assert [field["name"] for field in master["fields"]] == ["transaction_date"]
    # One document stated it, however many of its records did.
    assert master["fields"][0]["fill_count"] == 1

    # And the value landed on both records rather than only the first.
    record = document_analysis.generated_record(reloaded, document["id"])
    assert [
        [field["value"] for field in item["fields"]] for item in record["records"]
    ] == [["01 Jan"], ["02 Jan"]]


def test_a_late_field_records_which_documents_were_never_asked(monkeypatch):
    """``second_approver`` escaped the schema on 3 of 18 payment instructions,
    and D5 is a payment released under a single signature above the
    dual-signature threshold: the absence of a second approver *is* the
    exception. Absence on a document read before the field existed means nobody
    looked, and 4c is what sweeps them."""

    ws, created = _workspace(count=3)
    _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("invoice_number")]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1041", "citation": "1"}]),
            _reading(
                fields=[{"name": "invoice_number", "entry": 1,
                         "value": "INV-1042", "citation": "1"}],
                new_fields=[_declared("second_approver", role="control")],
            ),
        ],
    )
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    master = document_masters.master(reloaded, "vendor_invoice")
    # The two read before it, whichever the sort order made those.
    assert document_masters.unread_for_field(master, "second_approver") == (
        master["documents_read"][:2]
    )
    assert len(master["documents_read"]) == 3


# ------------------------------------------------------------------ the stamp
def test_the_schema_is_stamped_once_from_the_finished_master(monkeypatch):
    """Written once per type per run, which leaves the staleness family nothing
    to fire on mid-run. Re-deriving schemas mid-run orphaned 65 completed
    extractions as ``stale_schema_reference`` and took three further runs to
    recover; that failure is structurally impossible here rather than merely
    unlikely, because there is no version at all until the type is done.
    """

    ws, created = _workspace(count=2)
    _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("invoice_number")]),
            _reading(
                fields=[{"name": "invoice_number", "entry": 1,
                         "value": "INV-1041", "citation": "1"}],
                new_fields=[_declared("total_amount", value_type="number")],
            ),
        ],
    )
    finished = _run(ws, created)
    assert finished["status"] == "completed"

    reloaded = workspaces.load_workspace(ws.id)
    schema = document_schemas.get_schema(reloaded, "vendor_invoice")
    assert schema["schema_version"] == 1
    assert [field["name"] for field in schema["fields"]] == [
        "invoice_number",
        "total_amount",
    ]
    assert sorted(schema["derived_from"]) == sorted(item["id"] for item in created)
    assert schema["low_confidence"] is False


def test_readings_are_back_stamped_and_only_then_are_evidence(monkeypatch):
    """A reading carries ``master_ref`` until its type is stamped, then
    ``schema_ref``. Until the stamp adds one it is a reading and not yet
    evidence — which is exactly what ``has_usable_analysis`` reports and what
    the read's own skip predicate has to see past."""

    ws, created = _workspace(count=1)
    _fake(monkeypatch, [_reading(new_fields=[_declared("invoice_number")])])
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    record = document_analysis.generated_record(reloaded, created[0]["id"])
    assert record["analysis_profile"] == "structured"
    assert record["master_ref"]
    assert record["schema_ref"]["document_type"] == "vendor_invoice"
    assert document_capabilities.has_evidence_reading(reloaded, created[0]["id"])
    assert document_capabilities.has_usable_analysis(reloaded, created[0]["id"])


def test_a_single_document_type_is_stamped_low_confidence(monkeypatch):
    """Not because a two-sample agreement check could not run — there is no such
    check. It means one document's phrasing is this type's entire vocabulary,
    which is what ``fx_contract`` inducing 29 fields from one document looked
    like on the live 4a run."""

    ws, created = _workspace(count=1)
    _fake(monkeypatch, [_reading(new_fields=[_declared("invoice_number")])])
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    assert document_schemas.get_schema(reloaded, "vendor_invoice")[
        "low_confidence"
    ] is True


def test_a_second_run_reuses_the_readings_and_the_stamp(monkeypatch):
    ws, created = _workspace(count=2)
    fake = _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("invoice_number")]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1041", "citation": "1"}]),
        ],
    )
    _run(ws, created)
    before = _tags(fake).count(READ_TAG)

    reloaded = workspaces.load_workspace(ws.id)
    second = _run(reloaded, created)

    assert second["status"] == "completed"
    assert _tags(fake).count(READ_TAG) == before
    assert "documents.evidence_read" in second["workflow"]["reused_capabilities"]
    assert "documents.schemas_stamped" in second["workflow"]["reused_capabilities"]


def test_a_workspace_of_unidentified_documents_reads_nothing(monkeypatch):
    """``other`` is out of scope until 4b.2 coins a type for it. Nothing to read
    is satisfied, not blocked: the gap is a classification gap, and that
    capability is what reports it."""

    ws, created = _workspace()
    fake = _fake(monkeypatch, [_reading()], classify="other")
    fake.overrides[CLASSIFY_TAG] = {
        "document_type": "other",
        "document_type_other": "Unclear",
        "confidence": "low",
        "rationale": "Nothing matched.",
    }
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    assert READ_TAG not in _tags(fake)
    reloaded = workspaces.load_workspace(ws.id)
    assert document_schemas.list_schemas(reloaded) == []
    assert document_masters.types_with_master(reloaded) == []


# ---------------------------------------------------------------- the gate
def test_planning_material_is_neither_typed_nor_read(monkeypatch):
    """The shape of a real procurement engagement: a voucher alongside an
    approval matrix and a set of minutes.

    An approval matrix genuinely *is* a ``delegation_of_authority`` and
    genuinely still policy. Both axes have to answer, and only one of them was
    once being asked — which routed policy material to the structured profile
    and replaced the narrative planning consumes with a record dump.

    The planning documents keep the written summary the APM selectors read,
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

    def narrate(messages):
        excerpt = next(line for line in HEADINGS if line in str(messages))
        return {
            "summary_markdown": f"The document opens '{excerpt}'. [1]",
            "audit_notes_markdown": "Nothing is demonstrated by it alone.",
            "citations": [{"id": "1", "page": 1, "excerpt": excerpt}],
        }

    fake = _fake(monkeypatch, [_reading(new_fields=[_declared("invoice_number")])])
    fake.overrides[MAP_TAG] = narrate
    finished = _run(ws, created)

    assert finished["status"] == "completed"
    reloaded = workspaces.load_workspace(ws.id)

    assert dc.types_present(reloaded) == ["vendor_invoice"]
    assert document_masters.types_with_master(reloaded) == ["vendor_invoice"]
    # One classification and one reading, for the one voucher. The gate is what
    # saves the turns, so count them rather than inferring from a missing type.
    assert _tags(fake).count(CLASSIFY_TAG) == 1
    assert _tags(fake).count(READ_TAG) == 1
    assert not dc.is_classified(reloaded, created[1]["id"])
    assert not dc.is_classified(reloaded, created[2]["id"])

    for document in created[1:]:
        generated = document_analysis.generated_record(reloaded, document["id"])
        assert generated["analysis_profile"] == "standard"
        assert generated["summary_markdown"].strip()

    # And the voucher never reached the prose pass, which would have analysed it
    # twice under two vocabularies.
    voucher = document_analysis.generated_record(reloaded, created[0]["id"])
    assert voucher["analysis_profile"] == "structured"


# --------------------------------------------------------- refresh and revise
def test_a_refresh_re_reads_only_the_document_it_was_pointed_at(monkeypatch):
    """The promise ``_pending_types`` was written to make, kept under a master.

    A one-document refresh used to re-derive every schema in the workspace: on
    an 84-document engagement it spent an allowance sized for one document on
    re-sampling schemas it was never pointed at, failed on the turn limit, and
    bumped every schema a version — orphaning 68 completed extractions.
    """

    ws, created = _workspace(count=3)
    fake = _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("invoice_number")]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1041", "citation": "1"}]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1042", "citation": "1"}]),
        ],
    )
    _run(ws, created)
    before = _tags(fake).count(READ_TAG)
    assert before == 3

    reloaded = workspaces.load_workspace(ws.id)
    _run(reloaded, created[:1], action="refresh")

    assert _tags(fake).count(READ_TAG) == before + 1


def test_a_refresh_freezes_the_vocabulary_and_reports_what_it_could_not_take(
    monkeypatch,
):
    """The cheap action's job is to do the common repair and to *recognize* the
    case it cannot handle. A refresh is asked for because something looks wrong,
    and one thing that can be wrong is that the master has no place for what the
    document states — which a frozen re-read cannot fix and would otherwise fail
    at silently, reading the document a second time under the same blind spot."""

    ws, created = _workspace(count=2)
    fake = _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("invoice_number")]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1041", "citation": "1"}]),
        ],
    )
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    before = document_masters.master(reloaded, "vendor_invoice")

    fake.overrides[READ_TAG] = _reading(
        fields=[{"name": "invoice_number", "entry": 1, "value": "INV-1040",
                 "citation": "1"}],
        new_fields=[_declared("second_approver", role="control")],
    )
    _run(reloaded, created[:1], action="refresh")

    after = document_masters.master(workspaces.load_workspace(ws.id), "vendor_invoice")
    assert [field["name"] for field in after["fields"]] == [
        field["name"] for field in before["fields"]
    ]
    assert "second_approver" not in [field["name"] for field in after["fields"]]


def test_revise_vocabulary_re_reads_the_whole_type_and_rebuilds(monkeypatch):
    """The expensive action, and it is only ever reached deliberately.

    Re-reading every document of a type to fix one document's vocabulary is what
    the repair actually costs. The failure to avoid is a small button quietly
    doing it — which is the defect ``_pending_types`` was written to remove.

    It rebuilds rather than appends, because reading the type from the start in
    order is what keeps ``introduced_at`` meaningful and the sweep bounded.
    """

    ws, created = _workspace(count=3)
    fake = _fake(
        monkeypatch,
        [
            _reading(new_fields=[_declared("invoice_number")]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1041", "citation": "1"}]),
            _reading(fields=[{"name": "invoice_number", "entry": 1,
                              "value": "INV-1042", "citation": "1"}]),
        ],
    )
    _run(ws, created)
    before = _tags(fake).count(READ_TAG)

    reloaded = workspaces.load_workspace(ws.id)
    # Pointed at one document; every document of its type is re-read. The first
    # of the pass introduces the field and the rest fill it, which is what an
    # accumulating master looks like from the start.
    rebuilt_calls = {"n": 0}

    def rebuild(_messages):
        rebuilt_calls["n"] += 1
        if rebuilt_calls["n"] == 1:
            return _reading(new_fields=[_declared("second_approver", role="control")])
        return _reading(
            fields=[{"name": "second_approver", "entry": 1, "value": "E-9",
                     "citation": "1"}]
        )

    fake.overrides[READ_TAG] = rebuild
    _run(reloaded, created[:1], action="revise_vocabulary")

    assert _tags(fake).count(READ_TAG) == before + 3

    rebuilt = document_masters.master(
        workspaces.load_workspace(ws.id), "vendor_invoice"
    )
    assert [field["name"] for field in rebuilt["fields"]] == ["second_approver"]
    # Rebuilt from the start, so the indices describe this pass rather than the
    # one before it.
    assert rebuilt["fields"][0]["introduced_at"] == 0
    assert sorted(rebuilt["documents_read"]) == sorted(
        item["id"] for item in created
    )


def test_an_unknown_action_is_refused_rather_than_treated_as_a_refresh():
    from app.routes import document_routes

    assert document_routes.DOCUMENT_ANALYSIS_ACTIONS == (
        "analyze",
        "refresh",
        "revise_vocabulary",
    )


# ------------------------------------------------------------------- bounds
def test_a_document_over_the_read_window_is_reported_rather_than_truncated(
    monkeypatch,
):
    """A citation binds to text the worker saw, and the master's whole value
    rests on absence meaning *the document does not state this*. If a page was
    never read, absence means nobody looked at that page — so the bound is loud
    rather than silent, which is the same rule the chunk budgets keep."""

    ws = workspaces.create_workspace("Over window")
    body = ("Invoice line item repeated for length.\n" * 3000).encode()
    document = documents.add_document(ws, "huge.txt", body, category="evidence")
    dc.assign(ws, document["id"], "vendor_invoice", assigned_by="auditor",
              confidence="high")
    documents.extract_document(ws, document["id"])

    scope = {"document_ids": [document["id"]], "document_scope_mode": "all"}
    reason = document_capabilities.read_over_window(ws, document["id"], scope)
    assert reason is not None
    assert "characters" in reason
