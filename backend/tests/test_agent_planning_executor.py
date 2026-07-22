from __future__ import annotations

import ast
import inspect

import pytest

from app import workspaces
from app.agent import audit_capabilities
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.planning import (
    APM_EXECUTOR,
    APM_PARENT_REF,
    AUDITOR_EDIT_PRESERVED,
    ApmEditPreserved,
    ApmExecutorTarget,
)
from app.workspace_transactions import ParentConflict, parent_hashes


def _planning_workspace(name="Planning executor"):
    workspace = workspaces.create_workspace(name)
    workspace.update_planning(
        {
            "context": {
                "objective": "Assess procurement approvals",
                "scope": "Purchase commitments",
            }
        }
    )
    return workspace


def _request(workspace, markdown="# Engagement\n\nProcurement approvals."):
    return ExecutorRequest(
        executor_id="planning.apm",
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        proposal={"apm_markdown": markdown},
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [APM_PARENT_REF]),
        activity={"artifact_refs": ["planning:apm"]},
    )


def test_apm_executor_commits_exact_workspace_shape_and_receipt_postcondition():
    workspace = _planning_workspace()
    request = _request(workspace)
    target = ApmExecutorTarget(workspace, "run-apm")

    receipt = EXECUTORS.execute(request, target)

    assert target.workspace.planning["apm_markdown"] == request.proposal["apm_markdown"]
    assert target.workspace.planning["created_by"] == "agent"
    assert target.workspace.planning["agent_run_id"] == "run-apm"
    assert target.workspace.planning["workflow_basis_sha1"] == (
        audit_capabilities.planning_basis_sha1(target.workspace)
    )
    assert receipt.workspace_revision_after == receipt.workspace_revision_before + 1
    assert receipt.expected_parents == request.expected_parents
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, ["planning:apm"]
    )


def test_apm_executor_parent_hash_allows_unrelated_write_but_rejects_context_change():
    workspace = _planning_workspace("Parent guards")
    request = _request(workspace)
    workspace.update_planning(
        {"apm_markdown": "# Existing generated", "created_by": "agent"},
        agent=True,
    )
    target = ApmExecutorTarget(workspace, "run-parent")

    result = APM_EXECUTOR.implementation(request, target)
    assert result.workspace_revision_before > request.expected_revision
    assert target.workspace.planning["apm_markdown"] == request.proposal["apm_markdown"]

    changed_request = _request(target.workspace, "# Changed proposal")
    target.workspace.update_planning({"context": {"scope": "Changed scope"}})
    before = target.workspace.planning["apm_markdown"]
    with pytest.raises(ParentConflict):
        APM_EXECUTOR.implementation(changed_request, target)
    assert workspaces.load_workspace(target.workspace.id).planning["apm_markdown"] == before


def test_apm_executor_preserves_auditor_owned_content_without_permission():
    workspace = _planning_workspace("Auditor preservation")
    workspace.update_planning({"apm_markdown": "# Auditor APM"})
    request = _request(workspace, "# Agent proposal")
    target = ApmExecutorTarget(workspace, "run-preserve")

    reconciliation = APM_EXECUTOR.reconciler(request, target)
    assert reconciliation.disposition == "conflict"
    assert reconciliation.reason == AUDITOR_EDIT_PRESERVED
    with pytest.raises(ApmEditPreserved, match=AUDITOR_EDIT_PRESERVED):
        APM_EXECUTOR.implementation(request, target)
    assert workspaces.load_workspace(workspace.id).planning["apm_markdown"] == "# Auditor APM"


def test_apm_executor_can_replace_auditor_content_after_explicit_permission():
    workspace = _planning_workspace("Approved replacement")
    workspace.update_planning({"apm_markdown": "# Auditor APM"})
    request = _request(workspace, "# Approved replacement")
    target = ApmExecutorTarget(
        workspace,
        "run-approved",
        allow_auditor_overwrite=True,
    )

    assert APM_EXECUTOR.reconciler(request, target).disposition == "not_applied"
    APM_EXECUTOR.implementation(request, target)
    assert target.workspace.planning["apm_markdown"] == "# Approved replacement"
    assert target.workspace.planning["created_by"] == "agent"


def test_apm_executor_reconciles_interrupted_commit_and_detects_later_edit():
    workspace = _planning_workspace("APM reconciliation")
    request = _request(workspace)
    target = ApmExecutorTarget(workspace, "run-reconcile")
    APM_EXECUTOR.implementation(request, target)

    recovered = APM_EXECUTOR.reconciler(request, target)
    assert recovered.disposition == "already_applied"
    assert recovered.result.postcondition_hashes == parent_hashes(
        target.workspace, ["planning:apm"]
    )

    target.workspace.update_planning({"apm_markdown": "# Auditor edit"})
    conflict = APM_EXECUTOR.reconciler(request, target)
    assert conflict.disposition == "conflict"
    assert conflict.reason == AUDITOR_EDIT_PRESERVED


def test_planning_executor_has_no_gateway_worker_or_provider_dependency():
    from app.agent.executors import planning

    source = inspect.getsource(planning)
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
