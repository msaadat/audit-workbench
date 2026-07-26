"""Focused tests for the deterministic executors of the test capability group.

``tests.draft`` commits one RCM row's plans as durable Document/Data Test records
in ``draft`` status; ``tests.data_spec`` and ``tests.document_spec`` write the
executable specification onto one of those records.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app import data_tests, doc_tests, documents, workspaces
from app.agent import capabilities as audit_capabilities
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.tests import (
    DATA_SPEC_EXECUTOR,
    DOCUMENT_SPEC_EXECUTOR,
    DRAFT_EXECUTOR,
    DraftExecutorTarget,
    SpecExecutorTarget,
    semantic_test_id,
    stable_test_id,
)
from app.workspace_transactions import ParentConflict, parent_hashes


def _draft_workspace(name="Test draft executor"):
    workspace = workspaces.create_workspace(name)
    workspace.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate payments are processed",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    return workspace, workspace.rcm[0]["id"]


def _draft(**overrides):
    value = {
        "title": "Test duplicate payments",
        "objective": "Determine whether duplicate payments occurred",
        "criteria": "Each invoice is paid once.",
        "steps": ["Identify repeated invoice identifiers."],
        "source": "data",
        "expected_evidence": "Duplicate listing",
    }
    value.update(overrides)
    return value


def _draft_request(workspace, rcm_id, tests):
    return ExecutorRequest(
        executor_id="tests.draft",
        capability_id="tests.drafted",
        unit_id=f"test_draft:{rcm_id.lower()}",
        proposal={"tests": tests},
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [f"rcm:{rcm_id}"]),
        activity={"artifact_refs": [f"rcm:{rcm_id}"]},
    )


# --------------------------------------------------------------------------- #
# tests.draft
# --------------------------------------------------------------------------- #
def test_draft_executor_creates_a_linked_draft_with_parent_hash():
    workspace, rcm_id = _draft_workspace()
    request = _draft_request(workspace, rcm_id, [_draft()])
    target = DraftExecutorTarget(workspace, "run-draft", rcm_id)

    receipt = EXECUTORS.execute(request, target)

    committed = target.workspace.data_tests[0]
    assert committed["title"] == "Test duplicate payments"
    assert committed["status"] == "draft"
    assert committed["engine"] is None
    assert committed["rcm_id"] == rcm_id
    assert committed["created_by"] == "agent"
    assert committed["agent_run_id"] == "run-draft"
    assert committed["workflow_parent_sha1"] == (
        audit_capabilities.rcm_row_sha1(target.workspace.rcm[0])
    )
    # The RCM row holds only the link.
    assert target.workspace.rcm[0]["test_refs"] == [f"datatest:{committed['id']}"]
    assert receipt.artifact_refs == (f"datatest:{committed['id']}",)
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [f"datatest:{committed['id']}"]
    )
    assert receipt.output["tests"][0]["action"] == "created"


def test_draft_executor_routes_each_source_to_its_own_store():
    workspace, rcm_id = _draft_workspace("Test draft sources")
    request = _draft_request(
        workspace,
        rcm_id,
        [_draft(), _draft(title="Inspect approvals", source="document")],
    )
    target = DraftExecutorTarget(workspace, "run-sources", rcm_id)
    before = workspace.revision

    receipt = EXECUTORS.execute(request, target)

    assert [item["title"] for item in target.workspace.data_tests] == [
        "Test duplicate payments"
    ]
    assert [item["title"] for item in doc_tests.list_tests(target.workspace)] == [
        "Inspect approvals"
    ]
    # One transaction: both tests share the single committed revision.
    assert receipt.workspace_revision_before == before
    assert len(receipt.artifact_refs) == 2


def test_draft_executor_writes_only_declared_plan_fields():
    workspace, rcm_id = _draft_workspace("Test draft fields")
    request = _draft_request(
        workspace,
        rcm_id,
        [
            _draft(
                status="completed_no_exception",
                conclusion="The control operated",
                exception_count=7,
                engine="analytics",
            )
        ],
    )
    target = DraftExecutorTarget(workspace, "run-fields", rcm_id)

    EXECUTORS.execute(request, target)

    committed = target.workspace.data_tests[0]
    # A proposal cannot smuggle execution state or a specification into the plan.
    assert committed["status"] == "draft"
    assert committed["conclusion"] == ""
    assert committed["exception_count"] == 0
    assert committed["engine"] is None


def test_draft_executor_updates_a_matched_test_and_preserves_its_id():
    workspace, rcm_id = _draft_workspace("Test draft update")
    first = _draft_request(workspace, rcm_id, [_draft()])
    target = DraftExecutorTarget(workspace, "run-update", rcm_id)
    EXECUTORS.execute(first, target)
    committed_id = target.workspace.data_tests[0]["id"]

    second = _draft_request(
        target.workspace, rcm_id, [_draft(objective="Revised objective")]
    )
    receipt = EXECUTORS.execute(second, target)

    assert len(target.workspace.data_tests) == 1
    assert target.workspace.data_tests[0]["id"] == committed_id
    assert target.workspace.data_tests[0]["objective"] == "Revised objective"
    assert receipt.output["tests"][0]["action"] == "updated"


def test_draft_executor_preserves_an_auditor_owned_test_without_permission():
    workspace, rcm_id = _draft_workspace("Test draft preserve")
    semantic = semantic_test_id("datatest", rcm_id, "Test duplicate payments")
    data_tests.create_draft(
        workspace,
        {
            "id": stable_test_id("datatest", semantic),
            "semantic_id": semantic,
            "title": "Auditor test",
            "objective": "Auditor objective",
            "rcm_id": rcm_id,
        },
    )
    request = _draft_request(workspace, rcm_id, [_draft()])
    target = DraftExecutorTarget(workspace, "run-preserve", rcm_id)

    receipt = EXECUTORS.execute(request, target)

    assert target.workspace.data_tests[0]["objective"] == "Auditor objective"
    assert receipt.output["tests"][0]["action"] == "preserved"


def test_draft_executor_replaces_an_auditor_test_with_permission():
    workspace, rcm_id = _draft_workspace("Test draft permission")
    semantic = semantic_test_id("datatest", rcm_id, "Test duplicate payments")
    data_tests.create_draft(
        workspace,
        {
            "id": stable_test_id("datatest", semantic),
            "semantic_id": semantic,
            "title": "Auditor test",
            "objective": "Auditor objective",
            "rcm_id": rcm_id,
        },
    )
    request = _draft_request(workspace, rcm_id, [_draft()])
    target = DraftExecutorTarget(
        workspace, "run-permission", rcm_id, allow_auditor_overwrite=True
    )

    receipt = EXECUTORS.execute(request, target)

    assert target.workspace.data_tests[0]["objective"] == (
        "Determine whether duplicate payments occurred"
    )
    assert receipt.output["tests"][0]["action"] == "updated"


def test_draft_executor_parent_hash_rejects_a_concurrent_rcm_change():
    workspace, rcm_id = _draft_workspace("Test draft parent")
    request = _draft_request(workspace, rcm_id, [_draft()])
    workspace.update_rcm(rcm_id, {"control": "Auditor rewrote the control"})
    target = DraftExecutorTarget(workspace, "run-parent", rcm_id)

    with pytest.raises(ParentConflict):
        DRAFT_EXECUTOR.implementation(request, target)


def test_draft_executor_reconciles_an_interrupted_commit():
    workspace, rcm_id = _draft_workspace("Test draft reconcile")
    request = _draft_request(workspace, rcm_id, [_draft()])
    target = DraftExecutorTarget(workspace, "run-reconcile", rcm_id)

    # Before the commit lands the guarded row is unchanged: safe to (re)execute.
    assert DRAFT_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    DRAFT_EXECUTOR.implementation(request, target)
    committed_id = target.workspace.data_tests[0]["id"]

    recovered = DRAFT_EXECUTOR.reconciler(request, target)
    assert recovered.disposition == "already_applied"
    assert recovered.result.postcondition_hashes == parent_hashes(
        target.workspace, [f"datatest:{committed_id}"]
    )


# --------------------------------------------------------------------------- #
# tests.data_spec / tests.document_spec
# --------------------------------------------------------------------------- #
def _spec_request(workspace, kind, test_id, proposal):
    return ExecutorRequest(
        executor_id=f"tests.{'data' if kind == 'datatest' else 'document'}_spec",
        capability_id="tests.specified",
        unit_id=f"{kind}_spec:{test_id}",
        proposal=proposal,
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [f"{kind}:{test_id}"]),
        activity={"artifact_refs": [f"{kind}:{test_id}"]},
    )


def _data_draft(workspace_with_data):
    row = workspace_with_data.add_rcm(
        {"process": "AP", "risk": "Duplicates", "control": "Validation"}
    )
    draft = data_tests.create_draft(
        workspace_with_data,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "rcm_id": row["id"],
            "agent_run_id": "seed",
        },
    )
    return row, draft


_CODE = "result = transactions.filter(transactions['invoice_no'].is_duplicated())"


def test_data_spec_executor_fills_in_a_drafted_test(workspace_with_data):
    ws = workspace_with_data
    row, draft = _data_draft(ws)
    request = _spec_request(
        ws, "datatest", draft["id"], {"engine": "polars", "table_refs": ["transactions"], "spec": {"code": _CODE}}
    )
    target = SpecExecutorTarget(ws, "run-spec", row["id"], draft["id"], "datatest")

    receipt = EXECUTORS.execute(request, target)

    committed = data_tests._record(target.workspace, draft["id"])
    assert committed["status"] == "ready"
    assert committed["engine"] == "polars"
    assert committed["spec"]["code"] == _CODE
    # The plan written by the draft pass survives the specification pass.
    assert committed["objective"] == "Identify repeated invoice identifiers."
    assert receipt.output["action"] == "created"


def test_data_spec_executor_performs_the_authoritative_sandbox_validation(
    workspace_with_data,
):
    ws = workspace_with_data
    row, draft = _data_draft(ws)
    # The worker's static gate cannot run the code against the real frames; the
    # executor is where an unrunnable definition is rejected.
    request = _spec_request(
        ws,
        "datatest",
        draft["id"],
        {"engine": "polars", "table_refs": ["transactions"], "spec": {"code": "import os"}},
    )
    target = SpecExecutorTarget(ws, "run-bad", row["id"], draft["id"], "datatest")

    with pytest.raises(workspaces.WorkspaceError):
        DATA_SPEC_EXECUTOR.implementation(request, target)

    assert data_tests._record(ws, draft["id"])["status"] == "draft"


def test_document_spec_executor_blocks_and_registers_missing_evidence(
    workspace_with_data,
):
    ws = workspace_with_data
    row = ws.add_rcm({"process": "AP", "risk": "Approval", "control": "Sign-off"})
    draft = doc_tests.create_draft(
        ws,
        {
            "title": "Approval review",
            "objective": "Inspect the approval evidence.",
            "rcm_id": row["id"],
            "agent_run_id": "seed",
        },
    )
    request = _spec_request(
        ws,
        "doctest",
        draft["id"],
        {
            "kind": "qa",
            "items": [
                {"label": "Approval", "document_ids": [], "question": "Who approved?"}
            ],
            "missing_evidence": "No approval evidence has been imported.",
        },
    )
    target = SpecExecutorTarget(ws, "run-doc", row["id"], draft["id"], "doctest")

    receipt = EXECUTORS.execute(request, target)

    committed = doc_tests.load_test(target.workspace, draft["id"])
    assert committed["status"] == "blocked"
    assert "No approval evidence" in committed["scope_limitations"]
    requested = target.workspace.evidence_requests
    assert [item["document_test_id"] for item in requested] == [committed["id"]]
    # The blocking unit is recorded so a later import can unblock exactly it.
    assert requested[0]["blocked_unit_id"] == request.unit_id
    assert receipt.artifact_refs == (f"doctest:{committed['id']}",)


def test_document_spec_executor_reconciles_an_interrupted_commit(workspace_with_data):
    ws = workspace_with_data
    row = ws.add_rcm({"process": "AP", "risk": "Approval", "control": "Sign-off"})
    document = documents.add_document(ws, "Approval.txt", b"Management approved.")
    draft = doc_tests.create_draft(
        ws,
        {
            "title": "Approval review",
            "objective": "Inspect the approval evidence.",
            "rcm_id": row["id"],
            "agent_run_id": "seed",
        },
    )
    request = _spec_request(
        ws,
        "doctest",
        draft["id"],
        {
            "kind": "qa",
            "items": [
                {
                    "label": "Approval",
                    "document_ids": [document["id"]],
                    "question": "Who approved?",
                }
            ],
        },
    )
    target = SpecExecutorTarget(
        ws, "run-doc-reconcile", row["id"], draft["id"], "doctest"
    )

    assert (
        DOCUMENT_SPEC_EXECUTOR.reconciler(request, target).disposition == "not_applied"
    )

    DOCUMENT_SPEC_EXECUTOR.implementation(request, target)
    recovered = DOCUMENT_SPEC_EXECUTOR.reconciler(request, target)

    assert recovered.disposition == "already_applied"
    assert recovered.result.output["action"] == "created"


def test_tests_executor_has_no_gateway_worker_or_provider_dependency():
    from app.agent.executors import tests as tests_executors

    source = inspect.getsource(tests_executors)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(name.endswith(("model_gateway", "workers")) for name in imported)
    assert "app.llm" not in source
    assert "ModelGateway" not in source
    assert ".complete(" not in source
