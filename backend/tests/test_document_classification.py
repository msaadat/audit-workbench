"""Document type assignment, retyping, and the classification worker contract."""

from __future__ import annotations

import pytest

from app import document_classification as dc
from app import document_schemas, document_types, documents, workspaces
from app.agent.workers.documents import (
    _classification_response_schema,
    validate_classification_proposal,
)
from app.agent.workers.model import WorkerResponseValidationError
from app.workspaces import WorkspaceError


@pytest.fixture
def ws() -> workspaces.Workspace:
    workspace = workspaces.create_workspace("Classification Engagement")
    for name, body in (
        ("invoice-1.txt", b"Invoice No. INV-1042\nTotal Due USD 12,480.00"),
        ("invoice-2.txt", b"Invoice No. INV-1043\nTotal Due USD 900.00"),
        ("odd-1.txt", b"Letter of indemnity in respect of a missing bill of lading."),
    ):
        documents.add_document(workspace, name, body, category="evidence")
    return workspace


def _ids(ws) -> list[str]:
    return [str(item.get("id")) for item in ws.documents]


# --------------------------------------------------------------- assignment
def test_an_intermediarys_confirmation_is_its_own_type(ws):
    """A broker's note is a different document from the counterparty's confirmation.

    Observed on a real treasury engagement: nine broker notes were typed as the
    `fx_contract` and `investment_confirmation` documents they accompany, split
    by the *instrument of the deal* rather than by what the paper is. The
    catalog caused it — `investment_confirmation` carried "contract note" as an
    alias, so the nearest listed type was also the wrong one, and there was no
    right one to reach for.
    """

    broker = document_types.BY_ID["broker_confirmation"]
    assert broker.area == "treasury_banking"
    assert "contract note" in broker.aliases

    # The alias must not also pull toward the counterparty's own confirmation:
    # that ambiguity is what a discriminator cannot recover from.
    for neighbour in ("investment_confirmation", "fx_contract"):
        assert "contract note" not in document_types.BY_ID[neighbour].aliases


def test_a_coined_type_carries_what_distinguishes_it(ws):
    """A shipped type gets a discriminator from the catalog; a coined one has to
    be given one, or it is the least described entry in the vocabulary while
    being the only one no reader has seen before."""

    dc.retype(
        ws,
        _ids(ws)[0],
        coin="Internal deal confirmation",
        discriminator="Entity's own system print of a deal, carrying no counterparty "
                      "letterhead, reference, or signature.",
    )
    coined = {item["id"]: item for item in document_schemas.local_types(ws)}
    entry = coined["local.internal_deal_confirmation"]
    assert "no counterparty letterhead" in entry["discriminator"]


def test_a_confident_label_is_correctable_and_not_only_an_other(ws):
    """`other` is where a document lands when the model knew nothing fitted.

    A label it was confident and wrong about never goes there, so the review
    listing has to cover every assignment or the wrong ones stay unreachable.
    """

    first, second, third = _ids(ws)
    dc.assign(ws, first, "other", assigned_by="model", other_label="Unclear")
    dc.assign(ws, second, "vendor_invoice", assigned_by="model")

    assert [item["document_id"] for item in dc.other_bucket(ws)] == [first]
    listed = {item["document_id"] for item in dc.assignments(ws)}
    assert listed == {first, second}
    assert third not in listed  # nothing classified it

    dc.retype(ws, second, type_id="goods_receipt")
    assert dc.document_type(ws, second) == "goods_receipt"
    assert dc.is_auditor_assigned(ws, second)


def test_assignment_records_type_and_provenance(ws):
    document_id = _ids(ws)[0]
    record = dc.assign(
        ws, document_id, "vendor_invoice",
        assigned_by="model", confidence="high", rationale="Header reads Invoice.",
    )
    assert record["document_type"] == "vendor_invoice"
    assert record["assigned_by"] == "model"
    assert record["assigned_at"]
    assert dc.classification(ws, document_id) == record


def test_assignment_survives_a_reload(ws):
    """Stored in a sidecar, so a workspace handle several revisions behind still
    reads what actually happened — which is what capability readiness does."""

    document_id = _ids(ws)[0]
    dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    reopened = workspaces.load_workspace(ws.id)
    assert dc.document_type(reopened, document_id) == "vendor_invoice"


def test_a_stale_workspace_handle_still_sees_the_assignment(ws):
    """The regression that moved this off the document artifact: readiness runs
    against whatever handle its caller holds, and a lazily hydrated collection
    read from a behind handle reported a document unclassified moments after it
    was classified."""

    document_id = _ids(ws)[0]
    stale = ws
    fresh = workspaces.load_workspace(ws.id)
    dc.assign(fresh, document_id, "vendor_invoice", assigned_by="model")
    assert dc.document_type(stale, document_id) == "vendor_invoice"
    assert dc.unclassified_ids(stale) == [_ids(ws)[1], _ids(ws)[2]]


def test_other_must_name_what_the_document_is(ws):
    document_id = _ids(ws)[2]
    with pytest.raises(WorkspaceError, match="must name what the document is"):
        dc.assign(ws, document_id, "other", assigned_by="model")
    record = dc.assign(
        ws, document_id, "other", assigned_by="model", other_label="Letter of indemnity"
    )
    assert record["document_type_other"] == "Letter of indemnity"


def test_unknown_type_and_assigner_are_rejected(ws):
    document_id = _ids(ws)[0]
    with pytest.raises(document_types.DocumentTypeError):
        dc.assign(ws, document_id, "not_a_type", assigned_by="model")
    with pytest.raises(WorkspaceError, match="Unknown assigner"):
        dc.assign(ws, document_id, "vendor_invoice", assigned_by="robot")


# --------------------------------------------------------------- provenance
def test_a_model_rerun_never_overwrites_an_auditor_assignment(ws):
    """The rerun that makes retyping useful must not undo it."""

    document_id = _ids(ws)[0]
    dc.assign(ws, document_id, "other", assigned_by="model", other_label="Unclear")
    dc.retype(ws, document_id, type_id="goods_receipt")
    unchanged = dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    assert unchanged["document_type"] == "goods_receipt"
    assert unchanged["assigned_by"] == "auditor"


def test_an_auditor_may_overwrite_an_auditor_assignment(ws):
    document_id = _ids(ws)[0]
    dc.retype(ws, document_id, type_id="goods_receipt")
    record = dc.retype(ws, document_id, type_id="delivery_note")
    assert record["document_type"] == "delivery_note"
    assert record["previous_document_type"] == "goods_receipt"


def test_reclassifiable_excludes_auditor_assigned_others(ws):
    first, second, third = _ids(ws)
    dc.assign(ws, first, "other", assigned_by="model", other_label="Unclear")
    dc.assign(ws, second, "other", assigned_by="model", other_label="Unclear")
    dc.assign(ws, third, "other", assigned_by="auditor", other_label="Genuinely nothing")
    assert set(dc.reclassifiable_ids(ws)) == {first, second}


def test_an_other_chosen_from_the_current_catalog_is_not_swept_again(ws):
    """Re-posing the same question against the same list has the same answer, so
    sweeping unconditionally would leave classification re-running every run."""

    first = _ids(ws)[0]
    signature = dc.catalog_signature(ws)
    dc.assign(ws, first, "other", assigned_by="model",
              other_label="Unclear", catalog_sha1=signature)
    assert dc.reclassifiable_ids(ws) == []


def test_coining_a_type_makes_the_bucket_worth_sweeping_again(ws):
    first, second, _ = _ids(ws)
    for document_id in (first, second):
        dc.assign(ws, document_id, "other", assigned_by="model",
                  other_label="Unclear", catalog_sha1=dc.catalog_signature(ws))
    assert dc.reclassifiable_ids(ws) == []
    document_schemas.coin_local_type(ws, "Letter of Indemnity")
    assert set(dc.reclassifiable_ids(ws)) == {first, second}


# --------------------------------------------------------------- retyping
def test_retyping_to_a_coined_type_extends_the_effective_list(ws):
    document_id = _ids(ws)[2]
    dc.assign(ws, document_id, "other", assigned_by="model", other_label="Letter of indemnity")
    record = dc.retype(ws, document_id, coin="Letter of Indemnity")
    assert record["document_type"] == "local.letter_of_indemnity"
    assert record["assigned_by"] == "auditor"
    assert record["previous_document_type"] == "other"
    assert "local.letter_of_indemnity" in document_schemas.effective_type_ids(ws)


def test_a_coined_type_can_then_be_assigned_by_the_model(ws):
    """Coining is what lets the rerun sweep the rest of the bucket onto it."""

    first, second, _ = _ids(ws)
    dc.assign(ws, first, "other", assigned_by="model", other_label="LOI")
    dc.retype(ws, first, coin="Letter of Indemnity")
    record = dc.assign(ws, second, "local.letter_of_indemnity", assigned_by="model")
    assert record["document_type"] == "local.letter_of_indemnity"


def test_retype_needs_exactly_one_of_a_type_or_a_name(ws):
    document_id = _ids(ws)[0]
    with pytest.raises(WorkspaceError, match="exactly one"):
        dc.retype(ws, document_id)
    with pytest.raises(WorkspaceError, match="exactly one"):
        dc.retype(ws, document_id, type_id="goods_receipt", coin="Something")


def test_other_bucket_lists_what_an_auditor_must_review(ws):
    first, second, _ = _ids(ws)
    dc.assign(ws, first, "vendor_invoice", assigned_by="model")
    dc.assign(ws, second, "other", assigned_by="model", other_label="Letter of indemnity")
    bucket = dc.other_bucket(ws)
    assert [item["document_id"] for item in bucket] == [second]
    assert bucket[0]["document_type_other"] == "Letter of indemnity"


# --------------------------------------------------------------- selection
def test_types_present_excludes_other(ws):
    first, second, third = _ids(ws)
    dc.assign(ws, first, "vendor_invoice", assigned_by="model")
    dc.assign(ws, second, "vendor_invoice", assigned_by="model")
    dc.assign(ws, third, "other", assigned_by="model", other_label="Unclear")
    assert dc.types_present(ws) == ["vendor_invoice"]
    assert len(dc.documents_of_type(ws, "vendor_invoice")) == 2


def test_the_read_expands_over_transaction_evidence_only(ws):
    """A procurement policy is a correct ``delegation_of_authority`` and is still
    prose to this engagement.

    The regression this guards: induction expanded over every classified type,
    so an approval matrix and a set of board minutes were read under invented
    fields. Type says what a document is; category says whether this engagement
    holds it as transaction evidence, and both have to say yes.
    """

    invoice = _ids(ws)[0]
    dc.assign(ws, invoice, "vendor_invoice", assigned_by="model")
    matrix = documents.add_document(
        ws, "approval-matrix.txt",
        b"Financial Approval Matrix\nManager: up to USD 10,000.",
        category="policy",
    )
    dc.assign(ws, matrix["id"], "delegation_of_authority", assigned_by="model")

    # Classification reports both, because both are true.
    assert dc.types_present(ws) == ["delegation_of_authority", "vendor_invoice"]
    # The read expands over one.
    assert dc.types_for_induction(ws) == ["vendor_invoice"]
    assert dc.types_awaiting_schema(ws) == ["vendor_invoice"]


def test_a_type_carried_only_by_planning_material_is_never_read(ws):
    """Not merely deprioritized: the read is what builds the vocabulary, so a
    planning copy contributing fields would put them in front of every rule
    written against that type."""

    minutes = documents.add_document(
        ws, "minutes.txt",
        b"Minutes of Meeting - Procurement Planning\nThe committee approved the plan.",
        category="minutes",
    )
    dc.assign(ws, minutes["id"], "board_minutes", assigned_by="model")
    assert dc.types_for_induction(ws) == []
    assert dc.types_awaiting_schema(ws) == []
    assert "board_minutes" not in dc.types_for_induction(ws)


def test_transaction_evidence_needs_an_explicit_category(ws):
    """Explicit only, so the gate opens on a decision rather than on a gap."""

    uncategorized = documents.add_document(ws, "unknown.txt", b"Some text.")
    assert uncategorized["id"] not in {
        str(item.get("id")) for item in dc.transaction_evidence(ws)
    }


def test_unclassified_ids_shrink_as_documents_are_assigned(ws):
    assert len(dc.unclassified_ids(ws)) == 3
    dc.assign(ws, _ids(ws)[0], "vendor_invoice", assigned_by="model")
    assert len(dc.unclassified_ids(ws)) == 2


def test_summary_counts_what_readiness_reports(ws):
    first, second, third = _ids(ws)
    dc.assign(ws, first, "vendor_invoice", assigned_by="model")
    dc.assign(ws, second, "other", assigned_by="model", other_label="Unclear")
    dc.retype(ws, third, coin="Letter of Indemnity")
    summary = dc.summary(ws)
    assert summary["documents"] == 3
    assert summary["classified"] == 3
    assert summary["unclassified"] == 0
    assert summary["other"] == 1
    assert summary["auditor_assigned"] == 1
    assert summary["local_types"] == ["local.letter_of_indemnity"]


def test_classification_text_is_bounded_and_page_one_only(ws):
    document_id = _ids(ws)[0]
    text = dc.classification_text(ws, document_id, characters=12)
    assert text and len(text) <= 12
    assert dc.classification_text(ws, "no-such-document") == ""


# --------------------------------------------------------------- worker contract
def test_response_schema_requires_a_type_and_a_confidence():
    parsed = _classification_response_schema(
        '{"document_type": "vendor_invoice", "confidence": "high", "rationale": "Header."}'
    )
    assert parsed["document_type"] == "vendor_invoice"
    with pytest.raises(WorkerResponseValidationError, match="name a document_type"):
        _classification_response_schema('{"confidence": "high"}')
    with pytest.raises(WorkerResponseValidationError, match="confidence must be"):
        _classification_response_schema('{"document_type": "vendor_invoice"}')


class _Request:
    def __init__(self, **unit_input):
        self.unit_input = unit_input


def test_semantic_validator_rejects_a_type_outside_the_offered_catalog():
    """Validating against a different list than the prompt offered would reject
    a correct answer, so the offered ids travel on the unit input."""

    request = _Request(selectable_types=["purchase_order", "other"])
    with pytest.raises(WorkerResponseValidationError, match="not one of the offered"):
        validate_classification_proposal({"document_type": "vendor_invoice"}, request)
    assert validate_classification_proposal(
        {"document_type": "purchase_order"}, request
    )["document_type"] == "purchase_order"


def test_semantic_validator_accepts_a_coined_type_when_it_was_offered():
    request = _Request(selectable_types=["local.letter_of_indemnity", "other"])
    assert validate_classification_proposal(
        {"document_type": "local.letter_of_indemnity"}, request
    )["document_type"] == "local.letter_of_indemnity"


def test_semantic_validator_requires_a_name_for_other():
    request = _Request(selectable_types=["vendor_invoice", "other"])
    with pytest.raises(WorkerResponseValidationError, match="must name what the document is"):
        validate_classification_proposal(
            {"document_type": "other", "document_type_other": ""}, request
        )


def test_semantic_validator_drops_stray_other_text_on_a_named_type():
    """A named id plus free text is two answers; the stored one must be single."""

    request = _Request(selectable_types=["vendor_invoice", "other"])
    cleaned = validate_classification_proposal(
        {"document_type": "vendor_invoice", "document_type_other": "something else"},
        request,
    )
    assert cleaned["document_type_other"] == ""
