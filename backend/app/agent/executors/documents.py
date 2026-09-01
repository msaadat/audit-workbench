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
from collections.abc import Mapping
from dataclasses import dataclass, field

from collections.abc import Sequence

from ... import (
    cycle_vouching,
    document_analysis,
    document_classification,
    document_masters,
    document_schemas,
    documents as document_service,
)
from ..capabilities import documents as document_capabilities
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
CATEGORY_EXECUTOR_ID = "documents.category"
CLASSIFICATION_EXECUTOR_ID = "documents.classification"
READ_EXECUTOR_ID = "documents.read"
STAMP_EXECUTOR_ID = "documents.stamp"

DOCUMENT_TEXT_UNAVAILABLE = "document_has_no_extractable_text"
DOCUMENT_REQUIRES_VISION = "document_requires_vision"
DOCUMENT_VISUAL_SOURCE_UNSUPPORTED = "document_visual_source_unsupported"
VISUAL_PREPARATION_FAILED = "visual_preparation_failed"
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


def _plain_json(value: object) -> object:
    """Detach recursively frozen executor input before durable persistence."""

    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


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
        "derived_text_markdown": str(
            request.proposal.get("derived_text_markdown")
            or request.proposal.get("transcription_markdown")
            or ""
        ).strip(),
        "summary_markdown": summary,
        "summary_origin": str(
            request.proposal.get("summary_origin") or "model"
        ),
        "audit_notes_markdown": notes,
        "citations": [
            _plain_json(citation)
            for citation in request.proposal.get("citations") or []
            if isinstance(citation, Mapping)
        ],
        "coverage": _plain_json(coverage),
        # Retained for non-cycle/simple-vouching analyses already using this
        # generic document field surface. New cycle evidence is persisted in
        # the exact registry-backed collections below.
        "fields": _plain_json(request.proposal.get("fields") or {}),
        "registry": _plain_json(request.proposal.get("registry") or {}),
        "record_fragments": _plain_json(
            request.proposal.get("record_fragments") or []
        ),
        "records": _plain_json(request.proposal.get("records") or []),
        "unresolved_fragments": _plain_json(
            request.proposal.get("unresolved_fragments") or []
        ),
        "conflicts": _plain_json(request.proposal.get("conflicts") or []),
        "analysis_profile": str(
            request.proposal.get("analysis_profile") or "standard"
        ),
        "schema_ref": _plain_json(request.proposal.get("schema_ref") or {}),
        "vision_used": bool(request.proposal.get("vision_used")),
        "generation_profiles": [
            _plain_json(profile)
            for profile in request.proposal.get("generation_profiles") or []
            if isinstance(profile, Mapping)
        ],
        "prepared_media_set_hash": str(
            request.proposal.get("prepared_media_set_hash") or ""
        ),
    }
    if payload["analysis_profile"] == "structured":
        # The stamp is checked at commit time, not only on read: a schema
        # re-derived while this run was in flight means the extraction was made
        # against fields that are no longer current, and storing it would leave
        # an analysis nothing can safely interpret.
        #
        # An auditor retyping the document mid-run opens the same window from
        # the other side — the schema this was read against is untouched and
        # still current, it is simply no longer this document's schema — so the
        # type is compared here too. Readiness would re-expand the chunks on the
        # next run either way; committing an extraction already known to be
        # under the wrong type and reporting the unit as succeeded would not.
        current_type = document_classification.document_type(
            target.workspace, target.document_id
        )
        if not document_schemas.is_current_for(
            target.workspace, payload.get("schema_ref"), current_type
        ):
            raise WorkspaceError(
                "This extraction was made against a schema that is no longer "
                f"current for '{current_type or 'this document'}'."
            )
    if payload["analysis_profile"] == "voucher":
        if payload["summary_origin"] != "structured_evidence":
            raise WorkspaceError(
                "Voucher summaries must be derived from structured evidence."
            )
        try:
            reduction = cycle_vouching.validate_evidence_reduction(
                {
                    "registry": payload["registry"],
                    "records": payload["records"],
                    "unresolved_fragments": payload["unresolved_fragments"],
                    "conflicts": payload["conflicts"],
                }
            )
            if any(
                str(record.get("document_id") or "") != target.document_id
                for record in reduction["records"]
            ):
                raise WorkspaceError(
                    "A reduced evidence record names a different document."
                )
        except cycle_vouching.CycleSchemaError as error:
            raise WorkspaceError(str(error)) from error
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
            # The structured half needs its own counts. A receipt that reported
            # only citations could not distinguish a committed analysis that
            # produced five records from one that produced none, so a repair that
            # quietly dropped evidence left no trace in the audit trail.
            "record_fragments": len(list(artifact.get("record_fragments") or [])),
            "records": len(list(artifact.get("records") or [])),
            "unresolved_fragments": len(
                list(artifact.get("unresolved_fragments") or [])
            ),
            "conflicts": len(list(artifact.get("conflicts") or [])),
            "vision_used": bool(artifact.get("vision_used")),
            "derived_text_sha256": str(
                artifact.get("derived_text_sha256") or ""
            ),
            "prepared_media_set_hash": str(
                artifact.get("prepared_media_set_hash") or ""
            ),
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
        profiles = list(payload.get("generation_profiles") or [])
        legacy_profile = next(
            (
                profile
                for profile in profiles
                if str(profile.get("name") or "") == "agent"
            ),
            profiles[0] if profiles else {},
        )
        document_analysis.persist_analysis(
            fresh,
            document,
            dict(target.extracted),
            payload,
            provider=legacy_profile.get("provider"),
            model=legacy_profile.get("model"),
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


# --------------------------------------------------------------------------- #
# document category
# --------------------------------------------------------------------------- #
@dataclass
class DocumentCategoryExecutorTarget:
    """Mutable target for one document's category assignment."""

    workspace: Workspace
    run_id: str
    document_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Document category target requires a Workspace.")
        for field_name in ("run_id", "document_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Document category target requires a {field_name}.")
            setattr(self, field_name, value)


def _category_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    document_id: str,
    record: Mapping[str, object],
) -> ExecutorResult:
    refs = [document_ref(document_id)]
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
            # ``category_assigned_by`` is reported for the same reason the type
            # half reports its assigner: an auditor deciding while the run was in
            # flight leaves their answer standing, and the receipt has to say so.
            "status": "committed",
            "document_id": document_id,
            "category": str(record.get("category") or ""),
            "assigned_by": str(record.get("category_assigned_by") or ""),
            "confidence": str(record.get("category_confidence") or ""),
        },
    )


def execute_document_category(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit what this engagement holds one document as.

    The commit writes the sidecar and mirrors the value onto the document entry,
    both inside one ``mutate()``. The entry write is why this capability is
    serialized: it lands on the shared ``documents`` collection, and two units
    committing at once would race on it.
    """

    if not isinstance(raw_target, DocumentCategoryExecutorTarget):
        raise WorkspaceError("Unsupported document category target.")
    proposal = request.proposal
    if not isinstance(proposal, Mapping):
        raise WorkspaceError("Document category requires a proposal.")
    value = str(proposal.get("category") or "")
    if not value:
        raise WorkspaceError("Document category proposal names no category.")
    target = raw_target
    state: dict[str, object] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        return document_classification.assign_category(
            fresh,
            target.document_id,
            value,
            assigned_by="model",
            confidence=str(proposal.get("confidence") or "medium"),
            rationale=str(proposal.get("rationale") or ""),
            agent_run_id=target.run_id,
            unit_id=request.unit_id,
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _category_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        document_id=target.document_id,
        record=dict(committed.value),
    )


def reconcile_document_category(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted category commit.

    This commit touches the document entry, so the parent hash *does* move when
    it lands — which makes parent equality say "nothing was written here", not
    "the commit never ran": something else could have changed the document. The
    sidecar's own provenance is what settles it, exactly as for the type half.

    An auditor's category reconciles as applied whatever this unit proposed. The
    commit path refuses to overwrite one, so the unit's outcome is that their
    decision stands.
    """

    if not isinstance(raw_target, DocumentCategoryExecutorTarget):
        raise WorkspaceError("Unsupported document category target.")
    target = raw_target
    current = Workspace(target.workspace.root)
    record = document_classification.classification(current, target.document_id)
    by = str(record.get("category_assigned_by") or "")
    applied = by == "auditor" or (
        str(record.get("category_unit_id") or "") == request.unit_id
        and str(record.get("category_agent_run_id") or "") == target.run_id
    )
    if not applied:
        parent_ref = document_ref(target.document_id)
        actual = parent_hashes(current, [parent_ref])[parent_ref]
        expected = request.expected_parents.get(parent_ref)
        if actual != expected:
            return ExecutorReconciliation(
                "conflict",
                reason=str(
                    ParentConflict(parent_ref, str(expected), actual, current.revision)
                ),
            )
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_category_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            document_id=target.document_id,
            record=record,
        ),
        reason="This run's document category already holds.",
    )


CATEGORY_EXECUTOR = ExecutorDefinition(
    executor_id=CATEGORY_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_category,
    reconciler=reconcile_document_category,
)

EXECUTORS.register(CATEGORY_EXECUTOR)


# --------------------------------------------------------------------------- #
# document type classification
# --------------------------------------------------------------------------- #
@dataclass
class DocumentClassificationExecutorTarget:
    """Mutable target for one document's type assignment."""

    workspace: Workspace
    run_id: str
    document_id: str
    catalog_sha1: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Document classification target requires a Workspace.")
        for field_name in ("run_id", "document_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(
                    f"Document classification target requires a {field_name}."
                )
            setattr(self, field_name, value)


def execute_document_classification(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit one document's type under the document's own parent guard.

    The assignment is written with ``assigned_by="model"``, which is what makes
    it revisable by a later rerun — and what makes it *unable* to overwrite an
    auditor's retyping. That rule lives in ``document_classification.assign``
    rather than here, so the same guarantee holds however the assignment is
    reached.
    """

    if not isinstance(raw_target, DocumentClassificationExecutorTarget):
        raise WorkspaceError("Unsupported document classification target.")
    proposal = request.proposal
    if not isinstance(proposal, Mapping):
        raise WorkspaceError("Document classification requires a proposal.")
    document_type = str(proposal.get("document_type") or "")
    if not document_type:
        raise WorkspaceError("Document classification proposal names no document type.")
    target = raw_target
    state: dict[str, object] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        return document_classification.assign(
            fresh,
            target.document_id,
            document_type,
            assigned_by="model",
            confidence=str(proposal.get("confidence") or "medium"),
            rationale=str(proposal.get("rationale") or ""),
            other_label=str(proposal.get("document_type_other") or ""),
            agent_run_id=target.run_id,
            unit_id=request.unit_id,
            catalog_sha1=target.catalog_sha1,
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _classification_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        document_id=target.document_id,
        record=dict(committed.value),
    )


def _classification_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    document_id: str,
    record: Mapping[str, object],
) -> ExecutorResult:
    refs = [document_ref(document_id)]
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
            # ``assigned_by`` is reported because it may not be what this unit
            # asked for: an auditor retyping while the run was in flight leaves
            # their decision standing, and the receipt has to show that.
            "status": "committed",
            "document_id": document_id,
            "document_type": str(record.get("document_type") or ""),
            "assigned_by": str(record.get("assigned_by") or ""),
            "confidence": str(record.get("confidence") or ""),
        },
    )


def reconcile_document_classification(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted type-assignment commit.

    The assignment lands in a sidecar, so — as with a generated analysis —
    parent equality cannot prove the commit never ran. The sidecar's own identity
    does: an assignment carrying this run and this unit proves it applied.

    An auditor assignment reconciles as applied whatever this unit proposed. The
    commit path refuses to overwrite one, so the unit's outcome is that their
    decision stands, and re-running would only reconfirm it.
    """

    if not isinstance(raw_target, DocumentClassificationExecutorTarget):
        raise WorkspaceError("Unsupported document classification target.")
    target = raw_target
    parent_ref = document_ref(target.document_id)
    current = Workspace(target.workspace.root)
    actual = parent_hashes(current, [parent_ref])[parent_ref]
    expected = request.expected_parents.get(parent_ref)
    if actual != expected:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(parent_ref, str(expected), actual, current.revision)
            ),
        )
    record = document_classification.classification(current, target.document_id)
    by = str(record.get("assigned_by") or "")
    if by != "auditor" and (
        str(record.get("unit_id") or "") != request.unit_id
        or str(record.get("agent_run_id") or "") != target.run_id
    ):
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_classification_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            document_id=target.document_id,
            record=record,
        ),
        reason="This run's document type already holds.",
    )


CLASSIFICATION_EXECUTOR = ExecutorDefinition(
    executor_id=CLASSIFICATION_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_classification,
    reconciler=reconcile_document_classification,
)

EXECUTORS.register(CLASSIFICATION_EXECUTOR)


# --------------------------------------------------------------------------- #
# document schema freeze
# --------------------------------------------------------------------------- #
def schema_ref(document_type: str) -> str:
    return f"document_schema:{document_type}"


def _schema_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    record: Mapping[str, object],
    readings_stamped: int | None = None,
) -> ExecutorResult:
    ref = schema_ref(str(record.get("document_type") or ""))
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[ref],
        applied_parents=dict(request.expected_parents),
        # A schema lives in a side store, so ``parent_hashes`` — which projects
        # workspace artifacts — has nothing to say about it. Its own content hash
        # is the honest postcondition: after this commit, the schema for this
        # type is exactly this. Restated as sha1 because the receipt contract
        # takes that shape.
        postcondition_hashes={
            ref: hashlib.sha1(
                str(record.get("schema_hash") or "").encode("utf-8")
            ).hexdigest()
        },
        output={
            "status": "committed",
            "document_type": str(record.get("document_type") or ""),
            "schema_version": record.get("schema_version"),
            "schema_hash": str(record.get("schema_hash") or ""),
            "fields": len(list(record.get("fields") or [])),
            # Both are how a reader tells a corroborated schema from a guess.
            "low_confidence": bool(record.get("low_confidence")),
            "reconciled": bool(record.get("reconciled")),
            # How many readings this stamp turned into evidence. Absent for the
            # freeze, which stamps nothing.
            **(
                {"readings_stamped": int(readings_stamped)}
                if readings_stamped is not None
                else {}
            ),
        },
    )




@dataclass
class DocumentReadExecutorTarget:
    """Mutable target for one evidence document's whole-document reading."""

    workspace: Workspace
    run_id: str
    document_id: str
    document_type: str
    extracted: Mapping[str, object] = field(default_factory=dict)
    action: str = "analyze"
    #: A frozen master reports what it cannot take rather than applying it. That
    #: is ``refresh``'s job: do the common repair, and recognize the case it
    #: cannot handle instead of reading the document a second time under the same
    #: blind spot. Every other mode lets the master grow, which is the ordinary
    #: path and therefore the default.
    vocabulary_mode: str = "accumulate"

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Document read target requires a Workspace.")
        for field_name in ("run_id", "document_id", "document_type"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Document read target requires a {field_name}.")
            setattr(self, field_name, value)
        if self.vocabulary_mode not in document_capabilities.VOCABULARY_MODES:
            raise ValueError(
                "Document read vocabulary_mode must be frozen or rebuild."
            )
        if not isinstance(self.extracted, Mapping) or not (
            self.extracted.get("pages")
        ):
            raise ValueError("Document read target requires an extraction payload.")


def _reading_payload(
    proposal: Mapping[str, object], master: Mapping[str, object]
) -> dict:
    """Fold ``new_fields`` back onto the records they were filled on.

    The response contract splits a new field's *descriptor* from the records
    array because the ``fields`` enum is fixed when the call is made — a field
    the type does not carry yet cannot be named in it. Storage has no such
    split: a record is the fields it states, whichever call learned their names.
    So the value travels back onto its record here, and what reaches
    ``persist_analysis`` is the same record shape the corpus already consumes.
    """

    records = [
        {"fields": [dict(field) for field in record.get("fields") or []]}
        for record in proposal.get("records") or []
    ]
    renames = {
        str(item.get("from")): str(item.get("to"))
        for item in proposal.get("renames") or []
    }
    for field in proposal.get("fields_for_records") or []:
        for value in field.get("values") or []:
            index = int(value.get("record") or 1) - 1
            if 0 <= index < len(records):
                records[index]["fields"].append(
                    {
                        "name": str(field.get("name")),
                        "entry": value.get("entry"),
                        "value": value.get("value"),
                        "citation": value.get("citation"),
                    }
                )
    # A renamed field's value travelled under the old name, because that is the
    # only name the enum offered. The master is renamed at commit and the stored
    # record has to follow in the same act, or the reading holds a value under a
    # name its own vocabulary no longer explains.
    for record in records:
        for stored in record["fields"]:
            name = str(stored.get("name"))
            if name in renames:
                stored["name"] = renames[name]
    return {
        "analysis_profile": "structured",
        "records": records,
        "citations": [dict(item) for item in proposal.get("citations") or []],
        "audit_notes": [str(item) for item in proposal.get("audit_notes") or []],
    }


def read_ref(document_id: str) -> str:
    return analysis_ref(document_id)


def _read_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    document_id: str,
    artifact: Mapping[str, object],
    master: Mapping[str, object],
    refused: Sequence[Mapping[str, object]] = (),
) -> ExecutorResult:
    refs = [analysis_ref(document_id)]
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
            "document_type": str(master.get("document_type") or ""),
            "analysis_id": str(artifact.get("id") or ""),
            "content_sha1": str(artifact.get("content_sha1") or ""),
            "records": len(list(artifact.get("records") or [])),
            "citations": len(list(artifact.get("citations") or [])),
            # What the vocabulary now is, and how much of it this document was
            # the first to state. Both are what a reader needs to tell an
            # accumulating master from a drifting one.
            "master_ref": str(master.get("master_ref") or ""),
            "master_fields": len(list(master.get("fields") or [])),
            "documents_read": len(list(master.get("documents_read") or [])),
            "renames": len(list(master.get("renames") or [])),
            # A frozen master's refusals. Empty on the ordinary path; on a
            # ``refresh`` these name what the re-read wanted to add and could
            # not, which is the case the cheap action exists to recognize.
            "refused_fields": [dict(item) for item in refused],
        },
    )


def execute_document_read(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit one evidence document's reading and its type's master together.

    **One mutate.** The reading commits three things — the analysis artifact,
    the master update, and the ``documents_read`` append that assigns this
    document's ``introduced_at`` — and they have to land together. A partial
    write leaves indices that no longer describe what any document was asked,
    which does not fail; it makes the late-field sweep run over the wrong set,
    silently.

    The artifact carries a ``master_ref`` and no ``schema_ref``. It cannot carry
    one: the version it would name does not exist until every document of the
    type has been read. Until the stamp adds one, this is a reading and not yet
    evidence — which is exactly why nothing goes stale mid-run, and why a
    re-derivation orphaning sixty-five completed extractions is structurally
    impossible here rather than merely unlikely.
    """

    if not isinstance(raw_target, DocumentReadExecutorTarget):
        raise WorkspaceError("Unsupported document read target.")
    target = raw_target
    proposal = request.proposal
    if not isinstance(proposal, Mapping):
        raise WorkspaceError("Document read requires a proposal.")
    parent_ref = document_ref(target.document_id)
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Document read executor requires exactly its document parent hash."
        )

    declared = [dict(item) for item in proposal.get("new_fields") or []]
    renames = [dict(item) for item in proposal.get("renames") or []]
    frozen = target.vocabulary_mode == "frozen"
    # A frozen master takes no addition and no rename. Reporting rather than
    # dropping is the whole value of the refusal: a refresh is asked for because
    # something looks wrong, and the master having no place for what this
    # document states is one of the things that can be wrong.
    applied = [] if frozen else declared
    applied_renames = [] if frozen else renames
    refused = [
        {
            "name": str(item.get("name")),
            "reason": str(item.get("reason") or ""),
            "action": "revise_vocabulary",
        }
        for item in (declared + [{"name": item.get("to"), "reason": item.get("reason")} for item in renames])
    ] if frozen else []

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
        stated = {
            str(field.get("name"))
            for record in proposal.get("records") or []
            for field in record.get("fields") or []
        }
        master = document_masters.apply_reading(
            fresh,
            target.document_type,
            document_id=target.document_id,
            new_fields=applied,
            renames=applied_renames,
            filled=stated,
        )
        payload = _reading_payload(
            {
                **dict(proposal),
                "fields_for_records": applied,
                "renames": applied_renames,
            },
            master,
        )
        payload["master_ref"] = str(master.get("master_ref") or "")
        payload["master_type"] = target.document_type
        payload["summary_markdown"] = document_analysis.render_structured_summary(
            payload["records"]
        )
        payload["summary_origin"] = "structured_evidence"
        # The renderer consolidates *analyses*, each carrying its own
        # ``audit_notes``. A whole-document read is one analysis, so it is wrapped
        # rather than passed as the bare list of notes.
        payload["audit_notes_markdown"] = (
            document_analysis.render_structured_audit_notes(
                [{"audit_notes": payload["audit_notes"]}]
            )
        )
        document_analysis.persist_analysis(
            fresh,
            document,
            dict(target.extracted),
            payload,
            provider=None,
            model=None,
            action=target.action,
            coverage={
                "state": "complete",
                "analyzed_pages": [
                    int(page.get("page") or 0)
                    for page in target.extracted.get("pages") or []
                ],
                "omitted_pages": [],
            },
            agent_run_id=target.run_id,
            unit_id=request.unit_id,
        )
        state["master"] = master
        return document_analysis.generated_record(fresh, target.document_id) or {}

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _read_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        document_id=target.document_id,
        artifact=dict(committed.value),
        master=dict(state.get("master") or {}),
        refused=refused,
    )


def reconcile_document_read(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted reading.

    The master is the record that settles it. A document appears in its type's
    ``documents_read`` exactly once and only after its reading committed, so a
    document already listed there *is* this commit — whoever wrote it — and
    re-running would fold the same reading in a second time, double-counting
    every fill it contributed.
    """

    if not isinstance(raw_target, DocumentReadExecutorTarget):
        raise WorkspaceError("Unsupported document read target.")
    target = raw_target
    current = Workspace(target.workspace.root)
    if not document_masters.has_read(
        current, target.document_type, target.document_id
    ):
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_read_result(
            request,
            current,
            revision_before=current.revision,
            document_id=target.document_id,
            artifact=dict(
                document_analysis.generated_record(current, target.document_id) or {}
            ),
            master=document_masters.master(current, target.document_type),
        ),
    )


@dataclass
class DocumentStampExecutorTarget:
    """Mutable target for one type's schema stamp."""

    workspace: Workspace
    run_id: str
    document_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Document stamp target requires a Workspace.")
        for field_name in ("run_id", "document_type"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Document stamp target requires a {field_name}.")
            setattr(self, field_name, value)


def execute_document_stamp(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Write one finished master into a schema and back-stamp its readings.

    No model turn. The stamped version is provenance, not a contract: *this is
    the vocabulary that emerged from reading these N documents.* It is written
    once per type per run, which is what leaves the staleness family nothing to
    fire on mid-run.
    """

    if not isinstance(raw_target, DocumentStampExecutorTarget):
        raise WorkspaceError("Unsupported document stamp target.")
    target = raw_target
    state: dict[str, object] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        master = document_masters.master(fresh, target.document_type)
        fields = document_masters.schema_fields(master)
        if not fields:
            raise WorkspaceError(
                f"'{target.document_type}' has no master to stamp; its documents "
                "were never read."
            )
        read = [str(value) for value in master.get("documents_read") or []]
        record = document_schemas.save_schema(
            fresh,
            target.document_type,
            fields,
            derived_from=read,
            reconciled=bool(master.get("renames")),
            # Not because a two-sample agreement check could not run — there is
            # no such check. It means one document's phrasing is this type's
            # entire vocabulary, which is the same warning the live 4a run
            # produced when ``fx_contract`` induced 29 fields from one document.
            low_confidence=len(read) < 2,
        )
        stamp = {
            "document_type": record["document_type"],
            "schema_version": record["schema_version"],
            "schema_hash": record["schema_hash"],
        }
        # Until this lands, a reading is a reading. Back-stamping is what turns
        # the type's readings into evidence, and it moves no content: the
        # artifact's ``content_sha1`` covers what the document states and
        # deliberately not which vocabulary version names it.
        for document_id in read:
            document_analysis.stamp_schema_ref(fresh, document_id, stamp)
        state["stamped"] = len(read)
        return record

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _schema_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        record=dict(committed.value),
        readings_stamped=int(state.get("stamped") or 0),
    )


def reconcile_document_stamp(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted stamp.

    The schema store is content-addressed, so a stored schema whose hash equals
    what this master would produce *is* this commit. Back-stamping is idempotent
    for the same reason: it writes the same reference onto the same readings.
    """

    if not isinstance(raw_target, DocumentStampExecutorTarget):
        raise WorkspaceError("Unsupported document stamp target.")
    target = raw_target
    current = Workspace(target.workspace.root)
    stored = document_schemas.load_schema(current, target.document_type)
    if stored is None:
        return ExecutorReconciliation("not_applied")
    master = document_masters.master(current, target.document_type)
    fields = document_masters.schema_fields(master)
    if not fields:
        return ExecutorReconciliation("not_applied")
    expected = document_schemas.canonical_sha256(
        document_schemas.meaning(
            target.document_type, document_schemas.validate_fields(fields)
        )
    )
    if str(stored.get("schema_hash") or "") != expected:
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_schema_result(
            request,
            current,
            revision_before=current.revision,
            record=stored,
        ),
    )


READ_EXECUTOR = ExecutorDefinition(
    executor_id=READ_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_read,
    reconciler=reconcile_document_read,
)

EXECUTORS.register(READ_EXECUTOR)


STAMP_EXECUTOR = ExecutorDefinition(
    executor_id=STAMP_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_stamp,
    reconciler=reconcile_document_stamp,
)

EXECUTORS.register(STAMP_EXECUTOR)


ANALYSIS_EXECUTOR = ExecutorDefinition(
    executor_id=ANALYSIS_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_analysis,
    reconciler=reconcile_document_analysis,
)

EXECUTORS.register(ANALYSIS_EXECUTOR)


__all__ = [
    "ANALYSIS_EXECUTOR",
    "ANALYSIS_EXECUTOR_ID",
    "DOCUMENT_REVIEW_REQUIRED",
    "DOCUMENT_REQUIRES_VISION",
    "DOCUMENT_TEXT_UNAVAILABLE",
    "DOCUMENT_VISUAL_SOURCE_UNSUPPORTED",
    "PARTIAL_COVERAGE",
    "VISUAL_PREPARATION_FAILED",
    "DocumentAnalysisExecutorTarget",
    "analysis_ref",
    "document_ref",
    "execute_document_analysis",
    "extract_text",
    "reconcile_document_analysis",
]
