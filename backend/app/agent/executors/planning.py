"""Deterministic executors for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
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


# --------------------------------------------------------------------------- #
# planning.context executor (P7A)
# --------------------------------------------------------------------------- #
PLANNING_CONTEXT_EXECUTOR_ID = "planning.context"
PLANNING_CONTEXT_REF = "planning:context"
# The synthesizable structured planning-context fields. Matches the fields the
# synthesis worker is permitted to write; provenance and free-form keys stay out.
PLANNING_CONTEXT_FIELDS = frozenset(
    {
        "objective",
        "entity",
        "period",
        "scope",
        "materiality",
        "key_contacts",
        "background_notes",
    }
)


@dataclass
class PlanningContextExecutorTarget:
    """Mutable target for the deterministic planning-context commit."""

    workspace: Workspace
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Planning-context executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("Planning-context executor target requires a run_id.")


def _validated_context(
    request: ExecutorRequest,
    target: object,
) -> tuple[PlanningContextExecutorTarget, dict[str, str]]:
    if not isinstance(target, PlanningContextExecutorTarget):
        raise WorkspaceError(
            "Planning-context executor requires a PlanningContextExecutorTarget."
        )
    if set(request.expected_parents) != {PLANNING_CONTEXT_REF}:
        raise WorkspaceError(
            "Planning-context executor requires exactly the planning-context parent hash."
        )
    raw = request.proposal.get("context")
    if not isinstance(raw, Mapping):
        raise WorkspaceError("The accepted planning-context proposal must carry a context object.")
    context = {
        key: str(value)
        for key, value in raw.items()
        if key in PLANNING_CONTEXT_FIELDS and str(value or "").strip()
    }
    if not context:
        raise WorkspaceError("The accepted planning context is empty.")
    return target, context


def _context_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    context: dict[str, str],
) -> ExecutorResult:
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[PLANNING_CONTEXT_REF],
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, [PLANNING_CONTEXT_REF]),
        output={"status": "updated", "fields": sorted(context)},
    )


def execute_planning_context(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Merge one accepted planning context under the parent-hash guard.

    The synthesized fields are merged into ``planning.context`` (auditor-entered
    fields outside the proposal are preserved by the merge), guarded by the
    planning-context parent hash so a concurrent context change is a conflict
    rather than a silent overwrite.
    """
    target, context = _validated_context(request, raw_target)

    def commit(fresh: Workspace) -> None:
        fresh.update_planning({"context": context}, agent=True)

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _context_result(
        request,
        committed.workspace,
        revision_before=committed.revision - 1,
        context=context,
    )


def reconcile_planning_context(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted planning-context commit without mutating state."""
    target, context = _validated_context(request, raw_target)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [PLANNING_CONTEXT_REF])[PLANNING_CONTEXT_REF]
    expected_parent = request.expected_parents[PLANNING_CONTEXT_REF]
    if current_parent == expected_parent:
        # The context material is unchanged: the guarded merge has not landed,
        # so a normal (idempotent) execution is safe.
        return ExecutorReconciliation("not_applied")
    current_context = current.planning.get("context") or {}
    if all(str(current_context.get(key)) == value for key, value in context.items()):
        if current.revision <= request.expected_revision:
            return ExecutorReconciliation(
                "conflict",
                reason="Planning-context commit revision did not advance.",
            )
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_context_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
                context=context,
            ),
            reason="The accepted planning context already holds.",
        )
    return ExecutorReconciliation(
        "conflict",
        reason="Planning context changed before the interrupted commit was reconciled.",
    )


PLANNING_CONTEXT_EXECUTOR = ExecutorDefinition(
    executor_id=PLANNING_CONTEXT_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_planning_context)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_planning_context)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_planning_context,
    reconciler=reconcile_planning_context,
)

EXECUTORS.register(PLANNING_CONTEXT_EXECUTOR)


__all__ = [
    "APM_EXECUTOR",
    "APM_EXECUTOR_ID",
    "AUDITOR_EDIT_PRESERVED",
    "ApmEditPreserved",
    "ApmExecutorTarget",
    "PLANNING_CONTEXT_EXECUTOR",
    "PLANNING_CONTEXT_EXECUTOR_ID",
    "PLANNING_CONTEXT_FIELDS",
    "PLANNING_CONTEXT_REF",
    "PlanningContextExecutorTarget",
    "execute_apm",
    "execute_planning_context",
    "reconcile_apm",
    "reconcile_planning_context",
]
