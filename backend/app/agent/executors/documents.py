"""Deterministic execution for the document-analysis workflow.

Two concerns live here, neither of which calls a model:

``extract_text`` is the deterministic readiness computation behind
``documents.text_ready``. It adapts the existing document service rather than
re-implementing extraction, and it is idempotent by source hash.

``documents.analysis`` is the single persistence executor for a generated
document analysis. It guards on the document's material parent hash, writes the
durable artifact through the existing ``document_analysis`` sidecar contract, and
reconciles an interrupted commit by proving the artifact on disk carries this
run's exact unit and content identity — so an interruption never leaves a second
generated artifact behind.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field

from ... import document_analysis, documents as document_service, llm
from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ...workspaces import Workspace, WorkspaceError
from .model import (
    EXECUTORS,
    ExecutorConcurrency,
    ExecutorDefinition,
    ExecutorReconciliation,
    ExecutorRequest,
    ExecutorResult,
)

ANALYSIS_EXECUTOR_ID = "documents.analysis"

DOCUMENT_TEXT_UNAVAILABLE = "document_has_no_extractable_text"
DOCUMENT_REVIEW_REQUIRED = "generated_analysis_awaits_auditor_review"
PARTIAL_COVERAGE = "analysis_coverage_is_partial"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def document_ref(document_id: str) -> str:
    """The stable material-parent reference for one engagement document."""

    return f"document:{document_id}"


def analysis_ref(document_id: str) -> str:
    """The stable artifact reference for one document's generated analysis."""

    return f"document_analysis:{document_id}"


# --------------------------------------------------------------------------- #
# documents.text_ready (P9.3)
# --------------------------------------------------------------------------- #
def extract_text(workspace: Workspace, document_id: str, *, force: bool = False) -> dict:
    """Extract one document's text through the existing document service.

    Extraction is content-addressed: a cached payload whose ``source_sha1``
    matches the current document is returned untouched, so a repeated or resumed
    unit performs no work and produces no second artifact. That idempotence is
    why extraction needs no registered executor, receipt, or reconciler — there
    is no state a repeat could corrupt.
    """
    return document_service.extract_document(workspace, document_id, force=force)


# --------------------------------------------------------------------------- #
# documents.analysis_generated (P9.7)
# --------------------------------------------------------------------------- #
@dataclass
class DocumentAnalysisExecutorTarget:
    """Mutable target for one document's generated-analysis commit."""

    workspace: Workspace
    run_id: str
    document_id: str
    extracted: Mapping[str, object] = field(default_factory=dict)
    action: str = "analyze"

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Document analysis target requires a Workspace.")
        for field_name in ("run_id", "document_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Document analysis target requires a {field_name}.")
            setattr(self, field_name, value)
        if self.action not in {"analyze", "refresh"}:
            raise ValueError("Document analysis action must be analyze or refresh.")
        if not isinstance(self.extracted, Mapping) or not (
            self.extracted.get("pages")
        ):
            raise ValueError("Document analysis target requires an extraction payload.")


def _validated_analysis(
    request: ExecutorRequest, target: object
) -> tuple[DocumentAnalysisExecutorTarget, dict]:
    if not isinstance(target, DocumentAnalysisExecutorTarget):
        raise WorkspaceError(
            "Document analysis executor requires a DocumentAnalysisExecutorTarget."
        )
    parent_ref = document_ref(target.document_id)
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Document analysis executor requires exactly its document parent hash."
        )
    summary = str(request.proposal.get("summary_markdown") or "").strip()
    notes = str(request.proposal.get("audit_notes_markdown") or "").strip()
    if not summary or not notes:
        raise WorkspaceError("The accepted document analysis is incomplete.")
    coverage = request.proposal.get("coverage")
    if not isinstance(coverage, Mapping) or not str(coverage.get("state") or ""):
        raise WorkspaceError("The accepted document analysis declares no coverage.")
    payload = {
        "summary_markdown": summary,
        "audit_notes_markdown": notes,
        "citations": [
            dict(citation)
            for citation in request.proposal.get("citations") or []
            if isinstance(citation, Mapping)
        ],
        "coverage": dict(coverage),
    }
    return target, payload


def _analysis_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    document_id: str,
    artifact: Mapping[str, object],
) -> ExecutorResult:
    refs = [analysis_ref(document_id)]
    coverage = dict(artifact.get("coverage") or {})
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output={
            "status": "committed",
            "document_id": document_id,
            "analysis_id": str(artifact.get("id") or ""),
            "content_sha1": str(artifact.get("content_sha1") or ""),
            "coverage_state": str(coverage.get("state") or ""),
            "omitted_pages": [int(page) for page in coverage.get("omitted_pages") or []],
            "citations": len(list(artifact.get("citations") or [])),
        },
    )


def execute_document_analysis(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit one document's generated analysis under its own parent guard.

    The artifact lands in the existing ``Documents/.analysis`` sidecars through
    ``document_analysis.persist_analysis``, which already owns candidate-versus-
    active placement, auditor-override preservation, review-state transitions,
    and the status catalog. The transaction exists to guard the commit on the
    document's material parent hash and to publish one workspace revision, so a
    concurrent replacement of the source is a conflict rather than an analysis of
    text that no longer exists.
    """
    target, payload = _validated_analysis(request, raw_target)
    profile = llm.agent_status()
    state: dict[str, object] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        document = next(
            (
                item
                for item in fresh.documents
                if str(item.get("id")) == target.document_id
            ),
            None,
        )
        if document is None:
            raise WorkspaceError(f"Document '{target.document_id}' not found.")
        document_analysis.persist_analysis(
            fresh,
            document,
            dict(target.extracted),
            payload,
            provider=profile.get("provider"),
            model=profile.get("model"),
            action=target.action,
            coverage=dict(payload["coverage"]),
            agent_run_id=target.run_id,
            unit_id=request.unit_id,
        )
        return document_analysis.generated_record(fresh, target.document_id) or {}

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _analysis_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        document_id=target.document_id,
        artifact=dict(committed.value),
    )


def reconcile_document_analysis(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted document-analysis commit.

    Persisting an analysis does not change the document entry, so parent equality
    cannot prove the commit never ran. The artifact's own identity does: a
    generated record carrying this run id, this semantic unit, and this proposal's
    content hash proves the commit applied. Anything else — including a stale
    analysis from an earlier run that an explicit regeneration is replacing — is
    ``not_applied``, so the executor runs exactly once and never appends a second
    artifact for the same unit.
    """
    target, payload = _validated_analysis(request, raw_target)
    parent_ref = document_ref(target.document_id)
    current = Workspace(target.workspace.root)
    actual = parent_hashes(current, [parent_ref])[parent_ref]
    expected = request.expected_parents[parent_ref]
    if actual != expected:
        return ExecutorReconciliation(
            "conflict",
            reason=str(ParentConflict(parent_ref, expected, actual, current.revision)),
        )
    artifact = document_analysis.generated_record(current, target.document_id)
    if artifact is None:
        return ExecutorReconciliation("not_applied")
    if (
        artifact.get("agent_run_id") != target.run_id
        or artifact.get("unit_id") != request.unit_id
        or artifact.get("content_sha1")
        != document_analysis.analysis_content_sha1(payload)
    ):
        return ExecutorReconciliation("not_applied")
    if current.revision <= request.expected_revision:
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_analysis_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            document_id=target.document_id,
            artifact=artifact,
        ),
        reason="This run's generated document analysis already holds.",
    )


ANALYSIS_EXECUTOR = ExecutorDefinition(
    executor_id=ANALYSIS_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_document_analysis)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_document_analysis)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_analysis,
    reconciler=reconcile_document_analysis,
)

EXECUTORS.register(ANALYSIS_EXECUTOR)


__all__ = [
    "ANALYSIS_EXECUTOR",
    "ANALYSIS_EXECUTOR_ID",
    "DOCUMENT_REVIEW_REQUIRED",
    "DOCUMENT_TEXT_UNAVAILABLE",
    "PARTIAL_COVERAGE",
    "DocumentAnalysisExecutorTarget",
    "analysis_ref",
    "document_ref",
    "execute_document_analysis",
    "extract_text",
    "reconcile_document_analysis",
]
