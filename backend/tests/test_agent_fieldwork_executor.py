"""Focused tests for the deterministic audit fieldwork executors (P7G.2)."""

from __future__ import annotations

import pytest

from app import data_tests, doc_tests, documents, workspaces
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.fieldwork import (
    DATA_TEST_EXECUTOR,
    DOCUMENT_TEST_EXECUTOR,
    DataTestExecutorTarget,
    DocumentTestExecutorTarget,
    data_test_ref,
    data_test_semantic_id,
    data_test_stable_id,
    document_test_ref,
    result_ref,
    roll_up_results,
)
from app.workspace_transactions import parent_hashes


def _executed_rcm_row(ws):
    """Build one RCM row with an executed data test that raises an exception."""

    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "method": "data_analytics",
            "steps": ["Identify repeated invoice identifiers."],
        },
    )
    data_test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    data_tests.run(ws, data_test["id"])
    return row


def test_roll_up_results_commits_and_returns_stable_row_refs(workspace_with_data):
    ws = workspace_with_data
    row = _executed_rcm_row(ws)
    before = ws.revision

    refs = roll_up_results(ws)

    # One stable ``rcm:<id>`` result reference per RCM row.
    assert refs == [result_ref(row["id"])]
    # Self-committing: the derived roll-up is persisted on the row.
    assert row["execution_rollup"]["planned_tests"] == 1
    assert ws.revision > before
    # The roll-up created an observation for the exception.
    assert ws.observations


def test_roll_up_results_reuses_stable_observation_identities(workspace_with_data):
    ws = workspace_with_data
    _executed_rcm_row(ws)

    roll_up_results(ws)
    first = [
        (item["id"], item.get("execution_ref"), item.get("status"))
        for item in ws.observations
    ]
    assert first

    # A repeated roll-up over unchanged execution artifacts reuses the same
    # observation rows (keyed on ``execution_ref``) rather than duplicating them.
    roll_up_results(ws)
    second = [
        (item["id"], item.get("execution_ref"), item.get("status"))
        for item in ws.observations
    ]
    assert second == first


def test_roll_up_results_is_read_stable_on_a_workspace_without_executions(
    workspace_with_data,
):
    ws = workspace_with_data
    ws.add_rcm({"process": "AP", "risk": "Duplicate payments", "control": "Check"})

    refs = roll_up_results(ws)

    assert refs == [result_ref(ws.rcm[0]["id"])]
    # No execution artifacts means no observations were raised.
    assert ws.observations == []


# --------------------------------------------------------------------------- #
# fieldwork.data_test / fieldwork.document_test executors (P7E.2/P7E.3)
# --------------------------------------------------------------------------- #
def _definition_parents(ws, *, method="data_analytics"):
    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "method": method,
            "steps": ["Identify repeated invoice identifiers."],
        },
    )
    return row["id"], planned["id"]


def _data_test_request(ws, planned_id, definition):
    return ExecutorRequest(
        executor_id="fieldwork.data_test",
        capability_id="fieldwork.definitions_ready",
        unit_id=f"data_test_spec:{planned_id}",
        proposal={"data_test": definition},
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"planned_test:{planned_id}"]),
        activity={"artifact_refs": [f"planned_test:{planned_id}"]},
    )


def _definition(**overrides):
    value = {
        "title": "Duplicate invoices",
        "objective": "Identify repeated invoice identifiers",
        "engine": "analytics",
        "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    }
    value.update(overrides)
    return value


def test_data_test_executor_commits_with_parent_guard_and_postcondition(
    workspace_with_data,
):
    ws = workspace_with_data
    rcm_id, planned_id = _definition_parents(ws)
    request = _data_test_request(ws, planned_id, _definition())
    target = DataTestExecutorTarget(ws, "run-dat", rcm_id, planned_id)

    receipt = EXECUTORS.execute(request, target)

    committed = target.workspace.data_tests[0]
    assert committed["created_by"] == "agent"
    assert committed["planned_test_id"] == planned_id
    assert committed["workflow_parent_sha1"]
    assert receipt.artifact_refs == (data_test_ref(committed["id"]),)
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [data_test_ref(committed["id"])]
    )
    assert receipt.output["action"] == "created"


def test_data_test_executor_performs_the_authoritative_frame_validation(
    workspace_with_data,
):
    ws = workspace_with_data
    rcm_id, planned_id = _definition_parents(ws)
    # The worker's bundle-only gate cannot resolve analytics parameters against
    # the real frame; the executor is where an impossible column is rejected.
    request = _data_test_request(
        ws,
        planned_id,
        _definition(spec={"test_id": "duplicates", "params": {"columns": ["ghost"]}}),
    )
    target = DataTestExecutorTarget(ws, "run-frame", rcm_id, planned_id)

    with pytest.raises(workspaces.WorkspaceError):
        DATA_TEST_EXECUTOR.implementation(request, target)

    assert workspaces.load_workspace(ws.id).data_tests == []


def test_data_test_executor_preserves_an_auditor_owned_definition(
    workspace_with_data,
):
    ws = workspace_with_data
    rcm_id, planned_id = _definition_parents(ws)
    semantic = data_test_semantic_id(rcm_id, planned_id, "Duplicate invoices")
    data_tests.create(
        ws,
        {
            "id": data_test_stable_id(semantic),
            "semantic_id": semantic,
            "title": "Auditor duplicate test",
            "objective": "Auditor objective",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": rcm_id,
            "planned_test_id": planned_id,
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    request = _data_test_request(ws, planned_id, _definition())
    target = DataTestExecutorTarget(ws, "run-preserve", rcm_id, planned_id)

    receipt = EXECUTORS.execute(request, target)

    assert receipt.output["action"] == "preserved"
    assert target.workspace.data_tests[0]["objective"] == "Auditor objective"


def test_data_test_executor_reconciles_an_interrupted_commit(workspace_with_data):
    ws = workspace_with_data
    rcm_id, planned_id = _definition_parents(ws)
    request = _data_test_request(ws, planned_id, _definition())
    target = DataTestExecutorTarget(ws, "run-reconcile", rcm_id, planned_id)

    # Before the commit lands the planned test is unlinked: safe to (re)execute.
    assert DATA_TEST_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    DATA_TEST_EXECUTOR.implementation(request, target)
    recovered = DATA_TEST_EXECUTOR.reconciler(request, target)

    assert recovered.disposition == "already_applied"
    assert recovered.result.output["id"] == target.workspace.data_tests[0]["id"]


def test_document_test_executor_blocks_and_registers_missing_evidence(
    workspace_with_data,
):
    ws = workspace_with_data
    rcm_id, planned_id = _definition_parents(ws, method="document_inspection")
    request = ExecutorRequest(
        executor_id="fieldwork.document_test",
        capability_id="fieldwork.definitions_ready",
        unit_id=f"document_test_spec:{planned_id}",
        proposal={
            "document_test": {
                "title": "Approval review",
                "kind": "review",
                "spec": {"focus": "approval"},
                "items": [{"label": "Approval", "summary": "Review the approval"}],
                "missing_evidence": {
                    "document_types": ["approval"],
                    "rationale": "No approval evidence has been imported.",
                },
            }
        },
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"planned_test:{planned_id}"]),
        activity={"artifact_refs": [f"planned_test:{planned_id}"]},
    )
    target = DocumentTestExecutorTarget(ws, "run-doc", rcm_id, planned_id)

    receipt = EXECUTORS.execute(request, target)

    committed = doc_tests.load_test(target.workspace, receipt.output["id"])
    assert committed["status"] == "blocked"
    assert "No approval evidence" in committed["scope_limitations"]
    requested = target.workspace.evidence_requests
    assert [item["document_test_id"] for item in requested] == [committed["id"]]
    # The blocking unit is recorded so a later import can unblock exactly it.
    assert requested[0]["blocked_unit_id"] == request.unit_id
    assert receipt.artifact_refs == (document_test_ref(committed["id"]),)


def test_document_test_executor_reconciles_an_interrupted_commit(workspace_with_data):
    ws = workspace_with_data
    rcm_id, planned_id = _definition_parents(ws, method="document_inspection")
    document = documents.add_document(ws, "Approval.txt", b"Management approved.")
    request = ExecutorRequest(
        executor_id="fieldwork.document_test",
        capability_id="fieldwork.definitions_ready",
        unit_id=f"document_test_spec:{planned_id}",
        proposal={
            "document_test": {
                "title": "Approval review",
                "kind": "review",
                "spec": {"focus": "approval"},
                "items": [
                    {
                        "label": "Approval",
                        "document_ids": [document["id"]],
                        "summary": "Review the approval",
                    }
                ],
            }
        },
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"planned_test:{planned_id}"]),
        activity={"artifact_refs": [f"planned_test:{planned_id}"]},
    )
    target = DocumentTestExecutorTarget(ws, "run-doc-reconcile", rcm_id, planned_id)

    assert (
        DOCUMENT_TEST_EXECUTOR.reconciler(request, target).disposition == "not_applied"
    )

    DOCUMENT_TEST_EXECUTOR.implementation(request, target)
    recovered = DOCUMENT_TEST_EXECUTOR.reconciler(request, target)

    assert recovered.disposition == "already_applied"
    assert recovered.result.output["action"] == "created"
