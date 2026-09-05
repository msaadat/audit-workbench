"""A forced run must not spend a turn on a type the auditor already set.

Found on the expenses engagement. The auditor retyped one payment voucher by
hand; a later request routed to ``tests.specified`` under ``generation_mode:
"force"``, which re-expands the whole dependency closure, and
``documents.types_classified`` fanned out over every transaction-evidence
document — that one included. ``document_classification.assign`` refuses to
overwrite an auditor's decision and returns what is stored, so the commit moved
no revision, and ``reconcile_document_classification`` reported the auditor's
record as ``already_applied`` with ``revision_before == revision_after``. The
``ExecutorResult`` invariant rejected it, the auditor read
"executor_result.workspace_revision_after must be greater than
workspace_revision_before", the stage failed, and cycle design, the matrix,
twelve evidence readings, the schema stamp and all seventeen test-generation
units were blocked behind it.

Three guards, outermost first: the forced sweep does not name the document, the
binder settles it before the model turn, and the reconciler reports a conflict
the binder turns into a skip.
"""

from __future__ import annotations

import pytest

from app import document_classification as dc, documents, llm, workspaces
from app.agent import capabilities as capability_registries
from app.agent import runner
from app.agent.executors import EXECUTORS
from app.agent.executors import documents as document_executors
from app.agent.executors.documents import DOCUMENT_TYPE_PRESERVED, document_ref
from app.agent.executors.model import ExecutorRequest
from app.agent.workflows import documents as documents_workflow
from app.workspace_transactions import parent_hashes
from conftest import FakeAgentLLM, wait_run

CLASSIFY_TAG = "agent:document_classification"


def _voucher_workspace(name: str):
    ws = workspaces.create_workspace(name)
    document = documents.add_document(
        ws,
        "EXP-2025-027_PV-2025-027.txt",
        b"EMPLOYEE EXPENSE PAYMENT VOUCHER\nVoucher PV-2025-027\nAmount paid PKR 18,400",
        category="evidence",
    )
    documents.extract_document(ws, str(document["id"]))
    return ws, str(document["id"])


def _forced_scope(document_id: str) -> dict:
    return {
        "target_refs": [f"document:{document_id}"],
        "generation_mode": "force",
    }


def _classification_request(ws, document_id: str) -> ExecutorRequest:
    return ExecutorRequest(
        executor_id=document_executors.CLASSIFICATION_EXECUTOR_ID,
        capability_id="documents.types_classified",
        unit_id=f"document_classification:{document_id}",
        proposal={
            "document_type": "payment_voucher",
            "confidence": "high",
            "rationale": "An employee expense payment voucher.",
        },
        expected_parents=parent_hashes(ws, [document_ref(document_id)]),
        expected_revision=ws.revision,
    )


def test_a_forced_sweep_does_not_expand_an_auditor_typed_document():
    ws, document_id = _voucher_workspace("Forced sweep")
    registry = capability_registries.build_documents_registry()
    capability = registry.get("documents.types_classified")
    scope = _forced_scope(document_id)

    # Before the retype the forced sweep names it: force is what re-asks a
    # question the model already answered.
    dc.assign(ws, document_id, "payment_voucher", assigned_by="model")
    assert [unit.id for unit in capability.expand_units(ws, scope)] == [
        f"document_classification:{document_id}"
    ]

    dc.retype(ws, document_id, type_id="payment_voucher")
    assert capability.expand_units(ws, scope) == []


def test_the_reconciler_reports_an_auditor_type_rather_than_a_broken_invariant():
    ws, document_id = _voucher_workspace("Reconcile an auditor type")
    dc.assign(ws, document_id, "payment_voucher", assigned_by="auditor")
    request = _classification_request(ws, document_id)
    target = document_executors.DocumentClassificationExecutorTarget(
        ws, "20260905-181549-404aaa", document_id,
        catalog_sha1=dc.catalog_signature(ws),
    )

    reconciliation = EXECUTORS.reconcile(request, target)

    assert reconciliation.disposition == "conflict"
    assert reconciliation.reason == DOCUMENT_TYPE_PRESERVED


def test_this_runs_own_assignment_still_reconciles_as_applied():
    """The interrupted-commit case the reconciler exists for is untouched."""
    ws, document_id = _voucher_workspace("Reconcile an interrupted commit")
    request = _classification_request(ws, document_id)
    target = document_executors.DocumentClassificationExecutorTarget(
        ws, "run-1", document_id, catalog_sha1=dc.catalog_signature(ws),
    )
    # The commit this run's crash is imagined to have interrupted, replayed in
    # full so the assignment carries the revision a real commit advances.
    document_executors.execute_document_classification(request, target)

    assert EXECUTORS.reconcile(request, target).disposition == "already_applied"


def test_a_forced_run_over_an_auditor_typed_document_finishes(monkeypatch):
    """The whole defect, end to end: the stage settles instead of failing."""
    ws, document_id = _voucher_workspace("Forced run")
    dc.assign(ws, document_id, "payment_voucher", assigned_by="auditor")
    fake = FakeAgentLLM({})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )

    run = wait_run(ws, runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyze 1 selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": ["documents.types_classified"],
            "target_refs": [f"document:{document_id}"],
            "generation_mode": "force",
        },
        context={"document_ids": [document_id], "action": "refresh"},
    )["id"])

    assert run["status"] == "completed"
    assert "workspace_revision_after" not in str(run.get("error") or "")
    # No turn was spent asking a question whose answer could not be committed.
    assert [call for call in fake.calls if call["tag"] == CLASSIFY_TAG] == []
    assert dc.classification(
        workspaces.load_workspace(ws.id), document_id
    )["assigned_by"] == "auditor"
