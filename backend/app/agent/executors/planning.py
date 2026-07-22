"""Deterministic executors for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass

from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ...workspaces import Workspace, WorkspaceError
from .. import audit_capabilities
from .model import (
    EXECUTORS,
    ExecutorConcurrency,
    ExecutorDefinition,
    ExecutorReconciliation,
    ExecutorRequest,
    ExecutorResult,
)


APM_EXECUTOR_ID = "planning.apm"
APM_PARENT_REF = "planning:context"
APM_ARTIFACT_REF = "planning:apm"
AUDITOR_EDIT_PRESERVED = "auditor_owned_apm_preserved"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


class ApmEditPreserved(WorkspaceError):
    """The accepted proposal cannot silently replace an auditor-owned APM."""


@dataclass
class ApmExecutorTarget:
    workspace: Workspace
    run_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("APM executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("APM executor target requires a run_id.")
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def _validated_input(
    request: ExecutorRequest,
    target: object,
) -> tuple[ApmExecutorTarget, str]:
    if not isinstance(target, ApmExecutorTarget):
        raise WorkspaceError("APM executor requires an ApmExecutorTarget.")
    if set(request.expected_parents) != {APM_PARENT_REF}:
        raise WorkspaceError(
            "APM executor requires exactly the planning-context parent hash."
        )
    markdown = str(request.proposal.get("apm_markdown") or "").strip()
    if not markdown:
        raise WorkspaceError("The accepted APM proposal is empty.")
    return target, markdown


def _result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
) -> ExecutorResult:
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[APM_ARTIFACT_REF],
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, [APM_ARTIFACT_REF]),
        output={
            "status": "updated",
            "created_by": "agent",
            "auditor_edits_preserved": True,
        },
    )


def execute_apm(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one accepted APM under the declared parent-hash guard."""
    target, markdown = _validated_input(request, raw_target)

    def commit(fresh: Workspace) -> None:
        existing = str(fresh.planning.get("apm_markdown") or "").strip()
        if (
            existing
            and fresh.planning.get("created_by") == "user"
            and not target.allow_auditor_overwrite
        ):
            raise ApmEditPreserved(AUDITOR_EDIT_PRESERVED)
        fresh.update_planning(
            {
                "apm_markdown": markdown,
                "created_by": "agent",
                "agent_run_id": target.run_id,
                "workflow_basis_sha1": audit_capabilities.planning_basis_sha1(
                    fresh
                ),
            },
            agent=True,
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _result(
        request,
        committed.workspace,
        revision_before=committed.revision - 1,
    )


def reconcile_apm(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted APM commit without changing workspace state."""
    target, markdown = _validated_input(request, raw_target)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [APM_PARENT_REF])[APM_PARENT_REF]
    expected_parent = request.expected_parents[APM_PARENT_REF]
    if current_parent != expected_parent:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    APM_PARENT_REF,
                    expected_parent,
                    current_parent,
                    current.revision,
                )
            ),
        )
    existing = str(current.planning.get("apm_markdown") or "").strip()
    if (
        existing == markdown
        and current.planning.get("created_by") == "agent"
        and current.planning.get("agent_run_id") == target.run_id
        and current.planning.get("workflow_basis_sha1")
        == audit_capabilities.planning_basis_sha1(current)
    ):
        if current.revision <= request.expected_revision:
            return ExecutorReconciliation(
                "conflict", reason="APM commit revision did not advance."
            )
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
            ),
            reason="The accepted APM postcondition already holds.",
        )
    if (
        existing
        and current.planning.get("created_by") == "user"
        and not target.allow_auditor_overwrite
    ):
        return ExecutorReconciliation(
            "conflict",
            reason=AUDITOR_EDIT_PRESERVED,
        )
    return ExecutorReconciliation("not_applied")


APM_EXECUTOR = ExecutorDefinition(
    executor_id=APM_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_apm)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_apm)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_apm,
    reconciler=reconcile_apm,
)

EXECUTORS.register(APM_EXECUTOR)


__all__ = [
    "APM_EXECUTOR",
    "APM_EXECUTOR_ID",
    "AUDITOR_EDIT_PRESERVED",
    "ApmEditPreserved",
    "ApmExecutorTarget",
    "execute_apm",
    "reconcile_apm",
]
