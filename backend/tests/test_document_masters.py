"""The accumulating master: what removes field drift, and what it costs.

These pin 4b.1's stated gate — *one name per fact across a type; no field the
corpus never stated* — at the store, which is where the accumulation actually
happens. The workflow that drives it is covered in
``test_workflow_evidence_read.py``.

The measured failures behind each of these are in
``docs/agentic-vouching-plan.md``: ``fx_contract`` induced 44 fields from three
samples of one identical template, ``payment_instruction`` carried
``approved_by_id`` *and* ``approved_by_employee_id`` for one fact, and an RCM
turn wrote a comparison on ``fx_contract.received_date`` at 0 of 11.
"""

from __future__ import annotations

import pytest

from app import document_classification as dc
from app import document_masters, document_schemas, documents, workspaces
from app.workspaces import WorkspaceError


@pytest.fixture
def ws() -> workspaces.Workspace:
    return workspaces.create_workspace("Master Engagement")


def _declared(name: str, **overrides) -> dict:
    field = {
        "name": name,
        "role": "attribute",
        "value_type": "text",
        "cardinality": "one",
        "verbatim": True,
        "confidence": "high",
        "label": "",
        "values": [{"record": 1, "entry": 1, "value": "stated", "citation": "1"}],
    }
    field.update(overrides)
    return field


def _add(ws, name: str, *, body: bytes = b"Invoice text here") -> str:
    return str(documents.add_document(ws, name, body, category="evidence")["id"])


# ------------------------------------------------------------------ accumulate
def test_a_type_with_nothing_read_has_an_empty_master(ws):
    master = document_masters.master(ws, "vendor_invoice")
    assert master["fields"] == []
    assert master["documents_read"] == []
    assert document_masters.field_names(ws, "vendor_invoice") == []


def test_the_first_document_contributes_its_whole_vocabulary(ws):
    master = document_masters.apply_reading(
        ws,
        "vendor_invoice",
        document_id="doc-1",
        new_fields=[_declared("invoice_number"), _declared("total_amount")],
    )
    assert [field["name"] for field in master["fields"]] == [
        "invoice_number",
        "total_amount",
    ]
    assert master["documents_read"] == ["doc-1"]


def test_a_field_enters_with_a_fill_count_of_one(ws):
    """The zero-fill field cannot exist, and that is the response contract.

    ``new_fields`` carries the value and citation alongside the descriptor, so a
    field is declared only by being filled. Induction had no such guarantee and
    the gap was measured: an RCM turn chose ``fx_contract.received_date`` at 0 of
    11 — a field the samples proposed and the corpus did not carry.
    """
    master = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    assert master["fields"][0]["fill_count"] == 1
    assert all(field["fill_count"] >= 1 for field in master["fields"])


def test_fill_counts_rise_per_document_not_per_record(ws):
    """Breadth is what distinguishes a type-level field from an unusual document.

    A selector written against a name fourteen of eighteen documents state is a
    different thing from one four of them do, and an authoring turn shown names
    without frequencies cannot tell them apart.
    """
    document_masters.apply_reading(
        ws, "payment_instruction", document_id="doc-1",
        new_fields=[_declared("approved_by_id")],
    )
    master = document_masters.apply_reading(
        ws, "payment_instruction", document_id="doc-2",
        # Stated twice on one document — two records, one document.
        filled=["approved_by_id", "approved_by_id"],
    )
    assert master["fields"][0]["fill_count"] == 2
    assert master["documents_read"] == ["doc-1", "doc-2"]


def test_a_second_document_reusing_a_name_adds_no_field(ws):
    """The whole point. ``approved_by_id`` and ``approved_by_employee_id``
    cannot both enter a master that already holds one of them, because the
    second document is shown the first's names before it reads."""

    document_masters.apply_reading(
        ws, "payment_instruction", document_id="doc-1",
        new_fields=[_declared("approved_by_id")],
    )
    master = document_masters.apply_reading(
        ws, "payment_instruction", document_id="doc-2", filled=["approved_by_id"],
    )
    assert [field["name"] for field in master["fields"]] == ["approved_by_id"]


def test_declaring_a_field_the_master_already_holds_counts_as_stating_it(ws):
    """Not an error the read can be blamed for — a field a rename just freed, or
    one another entry in the same response added. Counting it as stated is the
    truthful outcome."""

    document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    master = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-2",
        new_fields=[_declared("total_amount", role="attribute")],
    )
    assert [field["name"] for field in master["fields"]] == ["total_amount"]
    assert master["fields"][0]["fill_count"] == 2


# --------------------------------------------------------------- cardinality
def test_a_second_document_stating_a_field_twice_widens_it(ws):
    """"One sample seeing two proves the type can carry two", kept after the
    union that held it was deleted.

    A field's cardinality is a guess made from whichever document introduced it.
    Without widening, document one silently fixes a constraint document two
    cannot satisfy and the failure is charged to document two: measured on the
    treasury corpus, the first dealing ticket declared ``rate`` as ``one``, the
    second states it twice, and the *second* document failed outright — taking
    its type's stamp with it, because the read edge is the one that blocks.
    """

    document_masters.apply_reading(
        ws, "treasury_deal_ticket", document_id="doc-1",
        new_fields=[_declared("rate", value_type="number")],
    )
    assert document_masters.master(ws, "treasury_deal_ticket")["fields"][0][
        "cardinality"
    ] == "one"

    master = document_masters.apply_reading(
        ws, "treasury_deal_ticket", document_id="doc-2", filled={"rate": 2},
    )
    assert master["fields"][0]["cardinality"] == "many"
    assert master["widened"] == [
        {"name": "rate", "document_id": "doc-2", "at_index": 1}
    ]


def test_cardinality_never_narrows(ws):
    """The master only grows, and that includes what a field is allowed to be.
    Narrowing would make a prior reading's second entry unexplainable."""

    document_masters.apply_reading(
        ws, "treasury_deal_ticket", document_id="doc-1",
        new_fields=[_declared("rate", value_type="number", cardinality="many")],
    )
    master = document_masters.apply_reading(
        ws, "treasury_deal_ticket", document_id="doc-2", filled={"rate": 1},
    )
    assert master["fields"][0]["cardinality"] == "many"
    assert master["widened"] == []


def test_a_field_introduced_on_several_records_arrives_as_many(ws):
    """A statement declaring one column filled on twenty lines carries ``many``
    from the moment it enters, not after a second document proves it."""

    master = document_masters.apply_reading(
        ws, "bank_statement", document_id="doc-1",
        new_fields=[
            _declared(
                "transaction_date",
                value_type="date",
                values=[
                    {"record": 1, "entry": 1, "value": "01 Jan", "citation": "1"},
                    {"record": 2, "entry": 2, "value": "02 Jan", "citation": "1"},
                ],
            )
        ],
    )
    assert master["fields"][0]["cardinality"] == "many"


# ------------------------------------------------------------------ late fields
def test_introduced_at_names_the_documents_that_were_never_asked(ws):
    """The one integer that makes the late-field sweep computable.

    ``second_approver`` escaped the schema on 3 of 18 payment instructions, and
    D5 is "released under a single signature above the dual-signature
    threshold": the absence of a second approver *is* the exception. Absence on
    a document read before the field existed means *nobody looked*, which is a
    different answer, and in an audit the difference is the finding.
    """
    for index in range(3):
        document_masters.apply_reading(
            ws, "payment_instruction", document_id=f"doc-{index}",
            new_fields=[_declared("approved_by_id")] if index == 0 else [],
            filled=["approved_by_id"] if index else [],
        )
    master = document_masters.apply_reading(
        ws, "payment_instruction", document_id="doc-3",
        new_fields=[_declared("second_approver", role="control")],
    )

    assert document_masters.unread_for_field(master, "second_approver") == [
        "doc-0", "doc-1", "doc-2",
    ]
    assert document_masters.unread_for_field(master, "approved_by_id") == []
    assert document_masters.late_fields(master) == [
        {
            "name": "second_approver",
            "introduced_at": 3,
            "unread": ["doc-0", "doc-1", "doc-2"],
        }
    ]


# ---------------------------------------------------------------------- renames
def test_a_rename_moves_the_field_and_is_recorded_with_its_document(ws):
    """Applied and *recorded*, never judged.

    Whether a proposed rename is a genuine correction or a preferred synonym is
    not a question code can settle, and a validator that tried would either wave
    everything through or refuse corrections that were right. So the asymmetry is
    enforced by cost: a rename re-opens every prior reading on exactly the terms
    a late-added field does.
    """
    document_masters.apply_reading(
        ws, "payment_instruction", document_id="doc-1",
        new_fields=[_declared("approver", role="control")],
    )
    master = document_masters.apply_reading(
        ws, "payment_instruction", document_id="doc-2",
        renames=[{"from": "approver", "to": "approved_by_id", "reason": "holds an id"}],
        filled=["approver"],
    )

    assert [field["name"] for field in master["fields"]] == ["approved_by_id"]
    assert master["renames"] == [
        {
            "from": "approver",
            "to": "approved_by_id",
            "reason": "holds an id",
            "document_id": "doc-2",
            "at_index": 1,
        }
    ]
    # The value travelled under the old name, so the fill counts follow the
    # rename rather than starting the new name at zero.
    assert master["fields"][0]["fill_count"] == 2


def test_a_rename_onto_an_existing_name_is_refused(ws):
    """Two names for one fact is what the vocabulary exists to prevent, and
    merging is not expressible: it needs removal, which nothing in the read may
    do."""

    document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1",
        new_fields=[_declared("total_amount"), _declared("net_amount")],
    )
    with pytest.raises(WorkspaceError, match="already carries"):
        document_masters.apply_reading(
            ws, "vendor_invoice", document_id="doc-2",
            renames=[{"from": "net_amount", "to": "total_amount", "reason": "same"}],
        )


def test_renaming_a_field_the_master_never_had_is_refused(ws):
    with pytest.raises(WorkspaceError, match="does not carry"):
        document_masters.apply_reading(
            ws, "vendor_invoice", document_id="doc-1",
            renames=[{"from": "nothing", "to": "something", "reason": "why"}],
        )


# ------------------------------------------------------------------- the stamp
def test_schema_fields_drop_the_master_only_keys(ws):
    """``validate_fields`` knows nothing about fill counts and would reject a
    field for carrying them, so the projection is what the stamp writes."""

    master = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    fields = document_masters.schema_fields(master)
    assert set(fields[0]) == {
        "name", "role", "value_type", "cardinality", "verbatim", "confidence", "label",
    }
    # And it is exactly what ``save_schema`` takes, which is the interlock: a
    # master can never accumulate into something the stamp would refuse at the
    # end of the type, stranding every reading of it.
    stored = document_schemas.save_schema(ws, "vendor_invoice", fields)
    assert [field["name"] for field in stored["fields"]] == ["total_amount"]


def test_a_master_is_validated_as_it_accumulates(ws):
    """An identifier field must carry ``value_type: identifier``, or a join key
    would compare "0042" against "42" under numeric rules. Refused when the field
    enters, not at the end of the type."""

    with pytest.raises(WorkspaceError, match="identifier"):
        document_masters.apply_reading(
            ws, "vendor_invoice", document_id="doc-1",
            new_fields=[_declared("invoice_number", role="identifier", value_type="text")],
        )


def test_the_master_ref_moves_with_the_vocabulary_and_not_with_a_fill(ws):
    """A reading carries this until its type is stamped. It has to move when the
    *names a document was read under* move, and not when a later document states
    the same field — a reading is not made stale by that."""

    first = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    refilled = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-2", filled=["total_amount"]
    )
    assert refilled["master_ref"] == first["master_ref"]

    widened = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-3", new_fields=[_declared("invoice_date")]
    )
    assert widened["master_ref"] != first["master_ref"]


def test_other_carries_no_master(ws):
    """``other`` is a transient state under 4b.2, not a bucket with a shared
    vocabulary: fusing the fields of unrelated documents is a bad join key by
    another route."""

    assert document_masters.load_master(ws, "other") is None
    with pytest.raises(WorkspaceError):
        document_masters.apply_reading(
            ws, "other", document_id="doc-1", new_fields=[_declared("thing")]
        )


# ------------------------------------------------------------------ rebuilding
def test_reset_discards_the_master_so_a_pass_rebuilds_it(ws):
    """``revise_vocabulary`` rebuilds rather than appends, and the difference is
    ``introduced_at``: appending to an existing master would leave indices that
    no longer describe what any document was asked. That does not fail — it makes
    the sweep run over the wrong set, silently."""

    document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    document_masters.reset(ws, "vendor_invoice")

    assert document_masters.load_master(ws, "vendor_invoice") is None
    rebuilt = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    assert rebuilt["fields"][0]["introduced_at"] == 0
    assert rebuilt["documents_read"] == ["doc-1"]


def test_re_reading_one_document_does_not_renumber_or_double_count(ws):
    """A run that resumes mid-type appends: the readings already taken stand and
    the indices they were assigned still describe what they were asked."""

    document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-2", filled=["total_amount"]
    )
    again = document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", filled=["total_amount"]
    )
    assert again["documents_read"] == ["doc-1", "doc-2"]
    assert document_masters.has_read(ws, "vendor_invoice", "doc-1")


def test_the_index_lists_every_type_carrying_a_master(ws):
    document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1", new_fields=[_declared("total_amount")]
    )
    document_masters.apply_reading(
        ws, "purchase_order", document_id="doc-2", new_fields=[_declared("order_number")]
    )
    assert document_masters.types_with_master(ws) == [
        "purchase_order",
        "vendor_invoice",
    ]
    assert set(document_masters.index(ws)["masters"]) == {
        "purchase_order",
        "vendor_invoice",
    }


# --------------------------------------------------------------------- selection
def test_types_awaiting_schema_shrinks_as_schemas_are_stamped(ws):
    first = _add(ws, "inv.txt")
    second = _add(ws, "po.txt")
    dc.assign(ws, first, "vendor_invoice", assigned_by="model")
    dc.assign(ws, second, "purchase_order", assigned_by="model")
    assert dc.types_awaiting_schema(ws) == ["purchase_order", "vendor_invoice"]

    document_schemas.save_schema(
        ws, "vendor_invoice", [_declared("invoice_number")]
    )
    assert dc.types_awaiting_schema(ws) == ["purchase_order"]


def test_other_never_awaits_a_schema(ws):
    document_id = _add(ws, "odd.txt")
    dc.assign(ws, document_id, "other", assigned_by="model", other_label="Unclear")
    assert dc.types_awaiting_schema(ws) == []


# ------------------------------------------------------- the reviewer's surface
def test_a_vocabulary_that_cannot_carry_a_rule_is_flagged_thin(ws):
    """The measured silence this exists for.

    A dealing ticket carrying fourteen labelled fields returned one,
    ``deal_reference``. The type's whole vocabulary became that field, the
    second ticket then agreed with it, and the master read *1 field, stated by
    2 of 2* — indistinguishable from a corroborated vocabulary. Nothing refuses
    that and nothing should: the reading is truthful about what it was given.
    What was missing was anywhere to see it.

    The test is functional rather than a field-count threshold, because any
    number would be a guess and types genuinely differ in size. A vocabulary
    earns its place by supporting a cycle rule, and a rule needs an identifier
    to join on plus something that is not an identifier to assert about. The
    one-field ticket could be joined and had nothing to say.
    """

    for document_id in ("doc-1", "doc-2"):
        document_masters.apply_reading(
            ws, "treasury_deal_ticket", document_id=document_id,
            new_fields=[
                _declared("deal_reference", role="identifier",
                          value_type="identifier")
            ] if document_id == "doc-1" else [],
            filled=["deal_reference"] if document_id == "doc-2" else [],
        )

    view = document_masters.vocabulary(ws, "treasury_deal_ticket")
    assert len(view["fields"]) == 1
    # Corroborated *and* useless, which is exactly why corroboration alone
    # cannot be the signal.
    assert view["corroborated_fields"] == 1
    assert view["joinable"] is True
    assert view["comparable"] is False
    assert view["thin"] is True


def test_a_vocabulary_that_can_carry_a_rule_is_not_flagged(ws):
    for document_id in ("doc-1", "doc-2"):
        document_masters.apply_reading(
            ws, "payment_instruction", document_id=document_id,
            new_fields=[
                _declared("instruction_reference", role="identifier",
                          value_type="identifier"),
                _declared("pay_amount", value_type="number"),
            ] if document_id == "doc-1" else [],
            filled=["instruction_reference", "pay_amount"]
            if document_id == "doc-2" else [],
        )

    view = document_masters.vocabulary(ws, "payment_instruction")
    assert view["corroborated_fields"] == 2
    assert view["joinable"] and view["comparable"]
    assert view["thin"] is False


def test_one_document_is_thin_however_good_its_reading(ws):
    """A vocabulary nothing corroborates is a guess, whatever its size."""

    document_masters.apply_reading(
        ws, "fx_contract", document_id="doc-1",
        new_fields=[
            _declared("our_reference", role="identifier", value_type="identifier"),
            _declared("amount", value_type="number"),
        ],
    )
    assert document_masters.vocabulary(ws, "fx_contract")["thin"] is True


def test_the_catalog_carries_every_type_with_a_master(ws):
    document_masters.apply_reading(
        ws, "vendor_invoice", document_id="doc-1",
        new_fields=[_declared("invoice_number", role="identifier",
                              value_type="identifier")],
    )
    assert [item["document_type"] for item in document_masters.catalog(ws)] == [
        "vendor_invoice"
    ]
