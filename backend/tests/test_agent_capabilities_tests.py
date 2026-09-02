"""Readiness gates owned by the tests capability group."""

from __future__ import annotations

from app import (
    cycle_measurement,
    cycle_rulesets,
    document_schemas,
    documents,
    workspaces,
)
from app.agent.capabilities import tests as test_capabilities


def _workspace_with_a_voucher():
    workspace = workspaces.create_workspace("Unvouched cycle")
    documents.add_document(
        workspace,
        "goods-receipt.txt",
        b"Goods received and inspected.",
        category="evidence",
    )
    return workspaces.load_workspace(workspace.id)


def _extracted(monkeypatch, *document_types):
    monkeypatch.setattr(
        cycle_measurement,
        "structured_records",
        lambda _workspace: [
            {
                "document_type": value,
                "document_id": "d1",
                "record_index": 0,
                "record": {},
            }
            for value in document_types
        ],
    )


def test_records_extracted_and_never_vouched_are_reported(monkeypatch):
    """The bypass is silent by construction, so something has to say it.

    Cycle vouching is reachable only through a ``transaction_cycle`` control
    attribute. When the matrix classifies every attribute some other way the
    linker and the evaluator are never called at all, and the run reports
    success.
    """
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch, "goods_receipt", "vendor_invoice")

    assert test_capabilities._unvouched_types(workspace) == [
        "goods_receipt",
        "vendor_invoice",
    ]


def test_a_workspace_whose_documents_yield_no_records_is_not_a_bypass(monkeypatch):
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch)

    assert test_capabilities._unvouched_types(workspace) == []


def test_a_workspace_with_no_documents_has_nothing_to_vouch(monkeypatch):
    workspace = workspaces.create_workspace("No documents")
    _extracted(monkeypatch, "vendor_invoice")

    assert test_capabilities._unvouched_types(workspace) == []


def test_readiness_names_the_types_left_unvouched(monkeypatch):
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch, "payment_voucher")
    monkeypatch.setattr(
        test_capabilities,
        "_rows",
        lambda *_args, **_kwargs: [{"id": "RCM-1"}],
    )
    monkeypatch.setattr(
        test_capabilities,
        "_scoped_manifest",
        lambda *_args, **_kwargs: [
            {"rcm_id": "RCM-1", "executable": True, "status": "completed"}
        ],
    )

    readiness = test_capabilities._specified_ready(workspace, {})

    assert readiness.state == "review_required"
    (reason,) = readiness.reasons
    assert "payment_voucher" in reason
    assert "transaction_cycle" in reason


# ------------------------------------------------------- why nothing vouched
# The gap is detected from the records; the *cause* has to be asked separately,
# because an explanation nobody checked sends the reader to the wrong repair.
_INVOICE_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "purchase_order_number", "role": "identifier",
     "value_type": "identifier", "cardinality": "one", "verbatim": True,
     "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]

_ORDER_FIELDS = [
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


def _cycle_matrix(workspace):
    """A workspace whose matrix asks for transaction-cycle evidence."""

    document_schemas.save_schema(workspace, "vendor_invoice", _INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", _ORDER_FIELDS)
    workspace.add_rcm({
        "process": "Procure to pay",
        "risk": "An invoice is paid for more than was ordered.",
        "control_attributes": [{
            "key": "three_way_match",
            "assertion": "Accuracy",
            "requirement": "The invoice agrees to the order.",
            "evidence_kind": "transaction_cycle",
            "required_comparisons": [{
                "key": "total",
                "left": {"document_type": "vendor_invoice", "field": "total_amount"},
                "right": {"document_type": "purchase_order", "field": "total_amount"},
                "rationale": "The amount billed must be the amount ordered.",
            }],
        }],
    })
    return workspaces.load_workspace(workspace.id)


def _ruleset_payload():
    return {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one"},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one"},
        ],
        "anchor": {"table": "expense_register", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk_po",
            "left": {"role": "invoice", "field": "purchase_order_number"},
            "right": {"role": "order", "field": "order_number"},
            "match": "normalized_equal",
            "rationale": "An invoice cites its order.",
        }],
        "assertions": [{
            "id": "as_total",
            "label": "Totals agree",
            "requirement": "The amount billed must be the amount ordered.",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
        }],
    }


def test_cause_is_the_matrix_when_no_attribute_asks_for_a_cycle():
    workspace = _workspace_with_a_voucher()

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "no_cycle_attribute"
    assert "transaction_cycle" in sentence
    assert ruleset_id == ""


def test_cause_is_the_missing_proposal_when_the_matrix_asks():
    workspace = _cycle_matrix(_workspace_with_a_voucher())

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "no_ruleset"
    assert "has been proposed" in sentence
    assert ruleset_id == ""


def test_cause_is_the_pending_approval_and_it_names_the_ruleset():
    """The case that reads like success from every other angle.

    An agent proposed the rules, every stage reported completion, and the rules
    sit unapplied because approving them is not an agent's to do. Reported as
    the matrix's fault, the reader goes and audits a matrix that is correct.
    """

    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "ruleset_unapproved"
    assert "proposed and unapproved" in sentence
    assert "approve" in sentence
    assert ruleset_id == record["ruleset_id"]


def test_cause_is_the_missing_test_once_a_ruleset_is_approved():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    cycle_rulesets.approve(
        workspace, record["ruleset_id"], approved_by="auditor@example.com"
    )

    cause, _sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "no_cycle_test"
    assert ruleset_id == record["ruleset_id"]


def test_cause_is_stated_when_every_proposal_was_rejected():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    cycle_rulesets.reject(workspace, record["ruleset_id"])

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "ruleset_rejected"
    assert "rejected" in sentence
    assert ruleset_id == ""


def test_readiness_sends_the_reader_to_the_unapproved_ruleset(monkeypatch):
    """Regression: the reason used to blame the matrix unconditionally.

    A treasury engagement whose matrix declared four ``transaction_cycle``
    attributes was told the matrix declared none, so the one thing outstanding
    — an auditor's signature on rules the agent had already written — was the
    one thing the message did not mention.
    """

    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    _extracted(monkeypatch, "vendor_invoice")
    monkeypatch.setattr(
        test_capabilities, "_rows", lambda *_a, **_k: [{"id": "RCM-1"}]
    )
    monkeypatch.setattr(
        test_capabilities,
        "_scoped_manifest",
        lambda *_a, **_k: [
            {"rcm_id": "RCM-1", "executable": True, "status": "completed"}
        ],
    )

    readiness = test_capabilities._specified_ready(workspace, {})

    assert readiness.state == "review_required"
    (reason,) = readiness.reasons
    assert "vendor_invoice" in reason
    assert "proposed and unapproved" in reason
    assert "the matrix classified no control attribute" not in reason
    assert readiness.details["unvouched_cause"] == "ruleset_unapproved"
    assert readiness.details["ruleset_id"] == record["ruleset_id"]
