"""Select the composition for one materialized workflow run.

The run persists the authoritative workflow definition ID that routing resolved,
so scheduler selection is a lookup rather than an inference. Both compositions
build the same domain-neutral :class:`WorkflowRunner`; they differ only in the
capability registry, execution bindings, and projections they supply.
"""

from __future__ import annotations

from ..workspaces import Workspace, WorkspaceError
from .runtime import WorkflowRunner
from .workflows import analysis as analysis_workflow
from .workflows import audit as audit_workflow
from .workflows import doc_tests as doc_tests_workflow
from .workflows import documents as documents_workflow


def build_workflow_runner(
    workspace: Workspace,
    run: dict,
    handle,
) -> WorkflowRunner:
    """Compose the scheduler for the workflow definition this run declares."""

    definition = str((run.get("workflow") or {}).get("definition") or "")
    if definition == analysis_workflow.WORKFLOW_ID:
        from .analysis_execution import build_analysis_workflow_runner

        return build_analysis_workflow_runner(workspace, run, handle)
    if definition == doc_tests_workflow.WORKFLOW_ID:
        from .doc_tests_execution import build_doc_tests_workflow_runner

        return build_doc_tests_workflow_runner(workspace, run, handle)
    if definition == documents_workflow.WORKFLOW_ID:
        from .documents_execution import build_documents_workflow_runner

        return build_documents_workflow_runner(workspace, run, handle)
    if definition == audit_workflow.WORKFLOW_ID:
        from .audit_execution import build_audit_workflow_runner

        return build_audit_workflow_runner(workspace, run, handle)
    label = "missing" if not definition else repr(definition)
    raise WorkspaceError(f"Workflow definition is {label} or unsupported.")


__all__ = ["build_workflow_runner"]
