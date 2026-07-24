from __future__ import annotations

import ast
import inspect

import pytest

from app import workspaces
from app.agent import capabilities as audit_capabilities
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.planning import (
    APM_EXECUTOR,
    APM_PARENT_REF,
    AUDITOR_EDIT_PRESERVED,
    ApmEditPreserved,
    ApmExecutorTarget,
    PLANNED_TEST_EXECUTOR,
    PLANNING_CONTEXT_EXECUTOR,
    PLANNING_CONTEXT_REF,
    PlannedTestExecutorTarget,
    PlanningContextExecutorTarget,
    RCM_EXECUTOR,
    RCM_PARENT_REF,
    RcmExecutorTarget,
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


def _context_request(workspace, context=None):
    return ExecutorRequest(
        executor_id="planning.context",
        capability_id="planning.context_ready",
        unit_id="planning_context",
        proposal={
            "context": context
            or {"scope": "Purchase commitments and approvals", "materiality": "$50k"}
        },
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [PLANNING_CONTEXT_REF]),
        activity={"artifact_refs": [PLANNING_CONTEXT_REF]},
    )


def test_planning_context_executor_merges_and_preserves_auditor_fields():
    workspace = _planning_workspace("Context commit")  # objective + scope present
    request = _context_request(workspace, {"scope": "Updated scope", "materiality": "$50k"})
    target = PlanningContextExecutorTarget(workspace, "run-ctx")

    receipt = EXECUTORS.execute(request, target)

    context = target.workspace.planning["context"]
    assert context["objective"] == "Assess procurement approvals"  # auditor field kept
    assert context["scope"] == "Updated scope"  # updated by merge
    assert context["materiality"] == "$50k"  # added by merge
    assert receipt.workspace_revision_after == receipt.workspace_revision_before + 1
    assert receipt.expected_parents == request.expected_parents
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [PLANNING_CONTEXT_REF]
    )


def test_planning_context_executor_parent_hash_rejects_concurrent_context_change():
    workspace = _planning_workspace("Context guard")
    request = _context_request(workspace, {"materiality": "$10k"})
    workspace.update_planning({"context": {"background_notes": "Late auditor note"}})
    before = dict(workspace.planning["context"])

    with pytest.raises(ParentConflict):
        PLANNING_CONTEXT_EXECUTOR.implementation(
            request, PlanningContextExecutorTarget(workspace, "run-conflict")
        )
    assert workspaces.load_workspace(workspace.id).planning["context"] == before


def test_planning_context_executor_reconciles_interrupted_commit_and_detects_later_edit():
    workspace = _planning_workspace("Context reconcile")
    request = _context_request(workspace, {"materiality": "$25k"})
    target = PlanningContextExecutorTarget(workspace, "run-reconcile")

    # Before the commit lands the guard still matches: safe to (re)execute.
    assert PLANNING_CONTEXT_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    PLANNING_CONTEXT_EXECUTOR.implementation(request, target)
    recovered = PLANNING_CONTEXT_EXECUTOR.reconciler(request, target)
    assert recovered.disposition == "already_applied"
    assert recovered.result.postcondition_hashes == parent_hashes(
        target.workspace, [PLANNING_CONTEXT_REF]
    )

    target.workspace.update_planning({"context": {"materiality": "$99k"}})
    conflict = PLANNING_CONTEXT_EXECUTOR.reconciler(request, target)
    assert conflict.disposition == "conflict"


def test_planning_context_executor_rejects_empty_accepted_context():
    workspace = _planning_workspace("Context empty")
    request = _context_request(workspace, {"not_a_context_field": "value"})

    with pytest.raises(workspaces.WorkspaceError):
        PLANNING_CONTEXT_EXECUTOR.implementation(
            request, PlanningContextExecutorTarget(workspace, "run-empty")
        )


def _rcm_workspace(name="RCM executor"):
    workspace = workspaces.create_workspace(name)
    workspace.update_planning(
        {
            "context": {"objective": "Assess procurement approvals", "scope": "Purchasing"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nPurchasing.",
        }
    )
    return workspace


def _rcm_row(**overrides):
    row = {
        "operation": "create",
        "process": "Accounts payable",
        "risk": "Duplicate payments are processed",
        "risk_rating": "high",
        "assertion": "Occurrence",
        "control": "Duplicate invoice validation",
        "control_type": "Automated preventive",
        "test_procedure": "Test invoice and amount duplicates.",
    }
    row.update(overrides)
    return row


def _rcm_request(workspace, rows):
    return ExecutorRequest(
        executor_id="planning.rcm",
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        proposal={"rows": rows},
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [RCM_PARENT_REF]),
        activity={"artifact_refs": ["planning:apm"]},
    )


def test_rcm_executor_creates_rows_with_parent_hash_and_receipt_postcondition():
    workspace = _rcm_workspace()
    request = _rcm_request(workspace, [_rcm_row()])
    target = RcmExecutorTarget(workspace, "run-rcm")

    receipt = EXECUTORS.execute(request, target)

    rows = target.workspace.rcm
    assert len(rows) == 1
    created = rows[0]
    assert created["created_by"] == "agent"
    assert created["agent_run_id"] == "run-rcm"
    assert created["workflow_parent_sha1"] == audit_capabilities.apm_sha1(target.workspace)
    assert receipt.artifact_refs == (f"rcm:{created['id']}",)
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [f"rcm:{created['id']}"]
    )
    assert receipt.workspace_revision_after > receipt.workspace_revision_before


def test_rcm_executor_updates_a_matched_row_and_preserves_the_id():
    workspace = _rcm_workspace("RCM update")
    existing = workspace.add_rcm(
        {"process": "Accounts payable", "risk": "Duplicate payments are processed",
         "control": "Manual review", "risk_rating": "medium", "agent_run_id": "prior"}
    )
    request = _rcm_request(
        workspace,
        [_rcm_row(operation="update", rcm_id=existing["id"], risk_rating="critical",
                  control="Duplicate invoice validation")],
    )
    target = RcmExecutorTarget(workspace, "run-update")

    receipt = EXECUTORS.execute(request, target)

    rows = target.workspace.rcm
    assert len(rows) == 1
    assert rows[0]["id"] == existing["id"]
    assert rows[0]["risk_rating"] == "critical"
    assert rows[0]["control"] == "Duplicate invoice validation"
    assert receipt.output["rows"][0]["action"] == "updated"


def test_rcm_executor_preserves_auditor_owned_row_without_permission():
    workspace = _rcm_workspace("RCM preserve")
    existing = workspace.add_rcm(
        {"process": "Accounts payable", "risk": "Duplicate payments are processed",
         "control": "Auditor manual control", "risk_rating": "medium"}
    )
    assert existing["created_by"] == "user"
    request = _rcm_request(
        workspace,
        [_rcm_row(operation="update", rcm_id=existing["id"], control="Agent override")],
    )
    target = RcmExecutorTarget(workspace, "run-preserve")

    receipt = EXECUTORS.execute(request, target)

    reloaded = workspaces.load_workspace(workspace.id)
    assert reloaded.rcm[0]["control"] == "Auditor manual control"
    assert receipt.output["rows"][0]["action"] == "preserved"


def test_rcm_executor_can_replace_auditor_row_with_permission():
    workspace = _rcm_workspace("RCM replace")
    existing = workspace.add_rcm(
        {"process": "Accounts payable", "risk": "Duplicate payments are processed",
         "control": "Auditor manual control", "risk_rating": "medium"}
    )
    request = _rcm_request(
        workspace,
        [_rcm_row(operation="update", rcm_id=existing["id"], control="Agent override")],
    )
    target = RcmExecutorTarget(workspace, "run-replace", allow_auditor_overwrite=True)

    EXECUTORS.execute(request, target)

    assert target.workspace.rcm[0]["control"] == "Agent override"


def test_rcm_executor_parent_hash_rejects_concurrent_apm_change():
    workspace = _rcm_workspace("RCM guard")
    request = _rcm_request(workspace, [_rcm_row()])
    workspace.update_planning({"apm_markdown": "# Changed APM\n\n## Scope\nNew."})

    with pytest.raises(ParentConflict):
        RCM_EXECUTOR.implementation(request, RcmExecutorTarget(workspace, "run-guard"))
    assert workspaces.load_workspace(workspace.id).rcm == []


def test_rcm_executor_fails_on_ambiguous_narrative_match():
    # Two existing rows with distinct semantic ids but near-identical narratives,
    # and a proposed row that matches neither exactly, so narrative ranking runs
    # and the top two candidates are within the ambiguity margin.
    workspace = _rcm_workspace("RCM ambiguous")
    workspace.add_rcm(
        {"process": "Payments", "risk": "Duplicate payment risk exists here",
         "control": "Control A", "risk_rating": "medium", "agent_run_id": "a"}
    )
    workspace.add_rcm(
        {"process": "Payments", "risk": "Duplicate payment risk exists now",
         "control": "Control B", "risk_rating": "medium", "agent_run_id": "b"}
    )
    request = _rcm_request(
        workspace,
        [_rcm_row(process="Payments", risk="Duplicate payment risk exists today")],
    )

    with pytest.raises(workspaces.WorkspaceError, match="Ambiguous"):
        RCM_EXECUTOR.implementation(request, RcmExecutorTarget(workspace, "run-amb"))


def test_rcm_executor_reconciles_interrupted_commit_and_detects_later_edit():
    workspace = _rcm_workspace("RCM reconcile")
    request = _rcm_request(workspace, [_rcm_row()])
    target = RcmExecutorTarget(workspace, "run-reconcile")

    # Before the commit lands, the guard still matches: safe to (re)execute.
    assert RCM_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    RCM_EXECUTOR.implementation(request, target)
    committed_id = target.workspace.rcm[0]["id"]

    recovered = RCM_EXECUTOR.reconciler(request, target)
    assert recovered.disposition == "already_applied"
    assert recovered.result.postcondition_hashes == parent_hashes(
        target.workspace, [f"rcm:{committed_id}"]
    )

    target.workspace.update_rcm(committed_id, {"control": "Auditor edit"})
    # The auditor edit is not an agent write, so it is now preserved, not applied.
    assert RCM_EXECUTOR.reconciler(request, target).disposition == "not_applied"


# --------------------------------------------------------------------------- #
# planning.planned_tests executor (P7D.2)
# --------------------------------------------------------------------------- #
def _planned_test_workspace(name="Planned-test executor"):
    workspace = _rcm_workspace(name)
    workspace.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate payments are processed",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    return workspace, workspace.rcm[0]["id"]


def _planned_test(**overrides):
    value = {
        "operation": "create",
        "stable_slug": "duplicate-payments",
        "title": "Test duplicate payments",
        "objective": "Determine whether duplicate payments occurred",
        "criteria": "Each invoice is paid once.",
        "steps": ["Identify repeated invoice identifiers."],
        "method": "data_analytics",
        "expected_evidence": "Duplicate listing",
    }
    value.update(overrides)
    return value


def _planned_test_request(workspace, rcm_id, tests):
    return ExecutorRequest(
        executor_id="planning.planned_tests",
        capability_id="planning.planned_tests_ready",
        unit_id=f"planned_test:{rcm_id.lower()}",
        proposal={"planned_tests": tests},
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [f"rcm:{rcm_id}"]),
        activity={"artifact_refs": [f"rcm:{rcm_id}"]},
    )


def test_planned_test_executor_commits_with_parent_hash_and_postcondition():
    workspace, rcm_id = _planned_test_workspace()
    request = _planned_test_request(workspace, rcm_id, [_planned_test()])
    target = PlannedTestExecutorTarget(workspace, "run-pt", rcm_id)

    receipt = EXECUTORS.execute(request, target)

    committed = target.workspace.rcm[0]["planned_tests"]
    assert [item["title"] for item in committed] == ["Test duplicate payments"]
    assert committed[0]["semantic_id"] == f"planned-test:{rcm_id}:duplicate-payments"
    assert committed[0]["created_by"] == "agent"
    assert committed[0]["agent_run_id"] == "run-pt"
    assert committed[0]["workflow_parent_sha1"] == (
        audit_capabilities.rcm_row_sha1(target.workspace.rcm[0])
    )
    assert receipt.artifact_refs == (f"planned_test:{committed[0]['id']}",)
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [f"planned_test:{committed[0]['id']}"]
    )
    assert receipt.output["planned_tests"][0]["action"] == "created"


def test_planned_test_executor_commits_every_test_in_one_transaction():
    workspace, rcm_id = _planned_test_workspace("Planned-test atomic")
    request = _planned_test_request(
        workspace,
        rcm_id,
        [
            _planned_test(),
            _planned_test(stable_slug="cut-off", title="Test cut-off", objective="Test cut-off"),
        ],
    )
    target = PlannedTestExecutorTarget(workspace, "run-atomic", rcm_id)
    before = workspace.revision

    receipt = EXECUTORS.execute(request, target)

    assert len(target.workspace.rcm[0]["planned_tests"]) == 2
    # One transaction: both tests share the single committed revision.
    assert receipt.workspace_revision_before == before
    assert len(receipt.artifact_refs) == 2


def test_planned_test_executor_writes_only_declared_fields():
    workspace, rcm_id = _planned_test_workspace("Planned-test fields")
    request = _planned_test_request(
        workspace,
        rcm_id,
        [
            _planned_test(
                status="completed_no_exception",
                conclusion="The control operated",
                execution_refs=["datatest:DAT-SMUGGLED"],
                exception_count=7,
            )
        ],
    )
    target = PlannedTestExecutorTarget(workspace, "run-fields", rcm_id)

    EXECUTORS.execute(request, target)

    committed = target.workspace.rcm[0]["planned_tests"][0]
    # A proposal cannot smuggle execution state into the committed record.
    assert committed["status"] == "not_ready"
    assert committed["conclusion"] == ""
    assert committed["execution_refs"] == []
    assert committed["exception_count"] == 0


def test_planned_test_executor_updates_a_matched_test_and_preserves_its_id():
    workspace, rcm_id = _planned_test_workspace("Planned-test update")
    first = _planned_test_request(workspace, rcm_id, [_planned_test()])
    target = PlannedTestExecutorTarget(workspace, "run-update", rcm_id)
    EXECUTORS.execute(first, target)
    committed_id = target.workspace.rcm[0]["planned_tests"][0]["id"]

    second = _planned_test_request(
        target.workspace,
        rcm_id,
        [_planned_test(operation="update", objective="Revised objective")],
    )
    receipt = EXECUTORS.execute(second, target)

    planned = target.workspace.rcm[0]["planned_tests"]
    assert len(planned) == 1
    assert planned[0]["id"] == committed_id
    assert planned[0]["objective"] == "Revised objective"
    assert receipt.output["planned_tests"][0]["action"] == "updated"


def test_planned_test_executor_preserves_auditor_owned_test_without_permission():
    workspace, rcm_id = _planned_test_workspace("Planned-test preserve")
    workspace.add_planned_test(
        rcm_id,
        {
            "title": "Auditor test",
            "objective": "Auditor objective",
            "method": "inquiry",
            "semantic_id": f"planned-test:{rcm_id}:duplicate-payments",
        },
    )
    request = _planned_test_request(workspace, rcm_id, [_planned_test()])
    target = PlannedTestExecutorTarget(workspace, "run-preserve", rcm_id)

    receipt = EXECUTORS.execute(request, target)

    planned = target.workspace.rcm[0]["planned_tests"]
    assert planned[0]["objective"] == "Auditor objective"
    assert receipt.output["planned_tests"][0]["action"] == "preserved"


def test_planned_test_executor_replaces_auditor_test_with_permission():
    workspace, rcm_id = _planned_test_workspace("Planned-test permission")
    workspace.add_planned_test(
        rcm_id,
        {
            "title": "Auditor test",
            "objective": "Auditor objective",
            "method": "inquiry",
            "semantic_id": f"planned-test:{rcm_id}:duplicate-payments",
        },
    )
    request = _planned_test_request(workspace, rcm_id, [_planned_test()])
    target = PlannedTestExecutorTarget(
        workspace, "run-permission", rcm_id, allow_auditor_overwrite=True
    )

    receipt = EXECUTORS.execute(request, target)

    planned = target.workspace.rcm[0]["planned_tests"]
    assert planned[0]["objective"] == "Determine whether duplicate payments occurred"
    assert receipt.output["planned_tests"][0]["action"] == "updated"


def test_planned_test_executor_parent_hash_rejects_concurrent_rcm_change():
    workspace, rcm_id = _planned_test_workspace("Planned-test parent")
    request = _planned_test_request(workspace, rcm_id, [_planned_test()])
    workspace.update_rcm(rcm_id, {"control": "Auditor rewrote the control"})
    target = PlannedTestExecutorTarget(workspace, "run-parent", rcm_id)

    with pytest.raises(ParentConflict):
        PLANNED_TEST_EXECUTOR.implementation(request, target)


def test_planned_test_executor_reconciles_interrupted_commit_and_detects_later_edit():
    workspace, rcm_id = _planned_test_workspace("Planned-test reconcile")
    request = _planned_test_request(workspace, rcm_id, [_planned_test()])
    target = PlannedTestExecutorTarget(workspace, "run-reconcile", rcm_id)

    # Before the commit lands the guarded row is unchanged: safe to (re)execute.
    assert PLANNED_TEST_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    PLANNED_TEST_EXECUTOR.implementation(request, target)
    committed_id = target.workspace.rcm[0]["planned_tests"][0]["id"]

    recovered = PLANNED_TEST_EXECUTOR.reconciler(request, target)
    assert recovered.disposition == "already_applied"
    assert recovered.result.postcondition_hashes == parent_hashes(
        target.workspace, [f"planned_test:{committed_id}"]
    )

    target.workspace.update_planned_test(
        rcm_id, committed_id, {"objective": "Auditor rewrote the objective"}
    )
    later = PLANNED_TEST_EXECUTOR.reconciler(request, target)
    assert later.disposition == "conflict"


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
