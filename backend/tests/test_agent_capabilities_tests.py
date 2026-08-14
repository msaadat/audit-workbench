"""Readiness gates owned by the tests capability group."""

from __future__ import annotations

from app import document_analysis, documents, workspaces
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


def _extracted(monkeypatch, *record_kinds):
    def records(_workspace, reference, **_kwargs):
        if str(reference.get("pack_id")) != "procure_to_pay":
            return []
        return [{"record_kind": kind} for kind in record_kinds]

    monkeypatch.setattr(document_analysis, "registry_evidence_records", records)


def test_records_extracted_for_a_pack_and_never_vouched_are_reported(monkeypatch):
    """The bypass is silent by construction, so something has to say it.

    Cycle vouching is reachable only through a ``transaction_cycle`` control
    attribute. When the matrix classifies every attribute some other way the
    registry packs and the evaluator are never called at all, and the run
    reports success.
    """
    workspace = _workspace_with_a_voucher()
    _extracted(
        monkeypatch,
        "procure_to_pay.goods_receipt",
        "procure_to_pay.vendor_invoice",
    )

    unvouched = test_capabilities._unvouched_packs(workspace)

    assert unvouched == [
        (
            "procure_to_pay",
            ["procure_to_pay.goods_receipt", "procure_to_pay.vendor_invoice"],
        )
    ]


def test_a_workspace_whose_documents_yield_no_pack_records_is_not_a_bypass(
    monkeypatch,
):
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch)

    assert test_capabilities._unvouched_packs(workspace) == []


def test_a_workspace_with_no_documents_has_nothing_to_vouch(monkeypatch):
    workspace = workspaces.create_workspace("No documents")
    _extracted(monkeypatch, "procure_to_pay.vendor_invoice")

    assert test_capabilities._unvouched_packs(workspace) == []


def test_readiness_reports_the_pack_and_the_record_kinds_left_unvouched(monkeypatch):
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch, "procure_to_pay.payment_voucher")
    monkeypatch.setattr(
        test_capabilities,
        "_rows",
        lambda *_args, **_kwargs: [{"id": "RCM-1"}],
    )
    monkeypatch.setattr(
        test_capabilities,
        "_scoped_manifest",
        lambda *_args, **_kwargs: [{"rcm_id": "RCM-1", "executable": True, "status": "completed"}],
    )

    readiness = test_capabilities._specified_ready(workspace, {})

    assert readiness.state == "review_required"
    (reason,) = readiness.reasons
    assert "procure_to_pay" in reason
    assert "procure_to_pay.payment_voucher" in reason
    assert "transaction_cycle" in reason
