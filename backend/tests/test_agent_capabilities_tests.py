"""Readiness gates owned by the tests capability group."""

from __future__ import annotations

from app import cycle_measurement, documents, workspaces
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
