"""Document-side composition of the domain-neutral capability scheduler.

Every document capability is bound to a native scheduler path: a per-unit
pipeline binding for the two model-backed capabilities (chunk mapping and
reduction) and a per-unit deterministic computation for extraction and the
auditor-review outcome. This module owns only the document-shaped glue the
scheduler must not know about — which worker and executor a unit uses, the
declared context scope it resolves, the chunk proposals a reduction consumes,
the document status projection, the scope checkpoint, and the completion
projection.

Chunk units are the one capability declared with the parallel barrier: they are
independent, they commit nothing, and their durable outcome is the persisted
proposal the reduction reads. That is what lets the scheduler fan them out under
``max_llm_concurrency``, all-settled, so one failed chunk never discards the
proposals its siblings already paid for.
"""

from __future__ import annotations

import uuid

from .. import cycle_vouching, document_analysis, document_media
from ..workspace_transactions import parent_hashes
from ..workspaces import Workspace, WorkspaceError, load_workspace
from . import narration, store, workflow
from .base import BaseRunner
from .workflow import semantic_unit_id
from .capabilities import DOCUMENTS_REGISTRY
from .capabilities.documents import (
    DOCUMENT_SCOPE_CHECKPOINT,
    MAX_SCOPE_DOCUMENTS,
    STAGE_CHECKPOINTS,
    DocumentScope,
    analysis_unit_specs,
    analyzable,
    chunk_specs,
    has_generated_analysis,
    resolve_document_scope,
    visual_page_limit,
)
from .context import (
    ContextResolver,
    document_chunk_scope,
    document_reduction_scope,
    document_visual_page_scope,
    document_voucher_scope,
)
from .executors import EXECUTORS
from .executors.documents import (
    DOCUMENT_REVIEW_REQUIRED,
    DOCUMENT_REQUIRES_VISION,
    DOCUMENT_TEXT_UNAVAILABLE,
    DOCUMENT_VISUAL_SOURCE_UNSUPPORTED,
    PARTIAL_COVERAGE,
    VISUAL_PREPARATION_FAILED,
    DocumentAnalysisExecutorTarget,
    analysis_ref,
    document_ref,
    extract_text,
)
from .execution_support import refresh_workspace, resolve_context, workflow_scope
from .runtime import (
    BoundUnitPipeline,
    CapabilityExecution,
    CapabilityExecutionRegistry,
    DeterministicUnitResult,
    FinishProjection,
    RunRuntime,
    UnitPipeline,
    UnitPipelineRequest,
    UnitSidecarStore,
    WorkflowRunner,
    first_unit_error,
    fold_terminal_status,
    unsettled_capabilities,
)
from .workers import WORKERS
from .workers.documents import (
    CHUNK_WORKER_ID,
    REDUCTION_WORKER_ID,
    VISUAL_WORKER_ID,
    VOUCHER_WORKER_ID,
    merge_voucher_audit_notes,
)

# Text-modality map profiles, by unit kind. A voucher chunk reads exactly what a
# standard chunk reads, so both resolve ``document_chunk_scope`` and differ only
# in which worker consumes it.
_TEXT_MAP_WORKERS = {
    "document_chunk_analysis": CHUNK_WORKER_ID,
    "document_voucher_analysis": VOUCHER_WORKER_ID,
}


class DocumentWorkflowExecution(BaseRunner):
    """Per-unit document execution bindings and projections for the scheduler."""

    stage_titles = {
        "document_text": "Document text",
        "document_chunks": "Document chunk analysis",
        "document_analysis": "Document analysis",
        "document_review": "Document analysis review",
    }

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        state_lock=None,
        context_resolver: ContextResolver | None = None,
    ):
        super().__init__(workspace, run, handle, runtime=runtime, state_lock=state_lock)
        self.context_resolver = context_resolver or ContextResolver()
        self.sidecars = UnitSidecarStore(workspace, run["id"])
        self.unit_pipeline: UnitPipeline | None = None
        self.scheduler: WorkflowRunner | None = None

    # ------------------------------------------------------------- scheduling
    def _refresh_dynamic_limits(self) -> None:
        """Size the model budget from the chunks actually in scope.

        Document analysis is one turn per source chunk plus one reduction per
        document, so the budget follows the resolved scope and the real chunk
        count rather than a fixed constant.
        """
        scope = workflow_scope(self.run)
        document_scope = self.scope()
        specs = [
            item
            for document_id in document_scope.document_ids
            for item in analysis_unit_specs(self.ws, document_id, scope)
        ]
        text_units = sum(item["kind"] in _TEXT_MAP_WORKERS for item in specs)
        visual_units = sum(
            item["kind"] == "document_visual_page_analysis"
            and not item.get("unsupported_reason")
            for item in specs
        )
        calculated = 4 + len(specs) + 2 * max(
            1, len(document_scope.document_ids)
        )
        prompt_allowance = (
            4 * 12_000
            + text_units * 12_000
            + visual_units * 20_480
            + max(1, len(document_scope.document_ids)) * 12_000
        )
        self.update_limits(
            {
                "max_model_turns": calculated,
                "max_estimated_prompt_tokens": prompt_allowance,
                "max_completion_tokens": calculated * 4_000,
                "max_image_parts": max(4, visual_units * 4),
                "max_prepared_image_bytes": max(
                    12 * 1024 * 1024,
                    visual_units * 12 * 1024 * 1024,
                ),
                "max_prepared_image_pixels": max(
                    12_000_000, visual_units * 12_000_000
                ),
            },
            grow_only=True,
        )

    def scope(self) -> DocumentScope:
        return resolve_document_scope(self.ws, workflow_scope(self.run))

    def milestone_projection(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
    ) -> dict | None:
        """Project the completed document outcome without exposing document text."""
        self.ws = subject
        requested = set((run.get("workflow") or {}).get("requested_outcomes") or [])
        if capability.id not in {"documents.analysis_generated", "documents.text_ready"}:
            return None
        if (
            capability.id == "documents.text_ready"
            and capability.id not in requested
        ):
            return None
        scope = self.scope()
        if capability.id == "documents.text_ready":
            done = sum(
                unit.get("status") == "succeeded"
                for unit in stage.get("units") or []
            )
            skipped = sum(
                unit.get("status") == "skipped"
                for unit in stage.get("units") or []
            )
            return {
                "headline": "Document content prepared",
                "summary": (
                    f"Prepared extractable content for {done} of "
                    f"{len(scope.document_ids)} scoped document(s); "
                    f"{skipped} were skipped."
                ),
                "metrics": [
                    {"label": "Documents in scope", "value": len(scope.document_ids)},
                    {"label": "Prepared", "value": done},
                    {"label": "Skipped", "value": skipped},
                ],
            }

        analyzed: list[str] = []
        partial: list[str] = []
        for document_id in scope.document_ids:
            if not has_generated_analysis(subject, document_id):
                continue
            analyzed.append(document_id)
            try:
                coverage = (
                    document_analysis.load_analysis(subject, document_id).get("coverage")
                    or {}
                )
            except WorkspaceError:
                coverage = {}
            if coverage.get("state") != "complete":
                partial.append(document_id)
        skipped = max(0, len(scope.document_ids) - len(analyzed))
        return {
            "status": (
                "completed_with_issues" if partial or skipped else "completed"
            ),
            "headline": "Document analysis complete",
            "summary": (
                f"Generated analyses for {len(analyzed)} of "
                f"{len(scope.document_ids)} scoped document(s). "
                f"{len(partial)} have partial coverage and {skipped} were not analyzed. "
                "Generated analyses remain subject to auditor review."
            ),
            "metrics": [
                {"label": "Documents in scope", "value": len(scope.document_ids)},
                {"label": "Analyses generated", "value": len(analyzed)},
                {"label": "Partial coverage", "value": len(partial)},
                {"label": "Not analyzed", "value": skipped},
            ],
            "highlights": [
                {
                    "severity": "warning",
                    "label": self._title(document_id),
                    "detail": "Only part of this document was covered.",
                    "artifact_ref": f"document:{document_id}",
                }
                for document_id in partial[:3]
            ],
            "artifact_refs": [
                f"document_analysis:{document_id}" for document_id in analyzed
            ],
        }

    def action(self) -> str:
        """``analyze`` or ``refresh`` — the placement rule for a new artifact.

        ``refresh`` is what an explicit regeneration means at the persistence
        boundary: the new artifact becomes a candidate awaiting the auditor's
        acceptance rather than silently replacing the active one.
        """
        if workflow_scope(self.run).get("generation_mode") == "force":
            return "refresh"
        return str((self.run.get("workflow") or {}).get("document_action") or "analyze")

    def _unreadable_document(
        self, document_id: str, result_refs: tuple[str, ...] = ()
    ) -> DeterministicUnitResult:
        """Settle a document that has no text a model could read.

        A scan with no extractable text is a fact about the file, not a
        judgement call, so in auto mode the run skips it and carries on rather
        than stopping to ask a question with one sensible answer. Permission
        mode still asks, because that is what the mode is for.

        Skipping is never silent: the run warns, narrates the skip, and names
        the document in its closing summary, so a document that contributed
        nothing to the analysis is always visible as such.
        """
        if workflow_scope(self.run).get("permission_mode"):
            return DeterministicUnitResult(
                "awaiting_confirmation", result_refs, DOCUMENT_TEXT_UNAVAILABLE
            )
        title = self._title(document_id)
        narration.note(
            self.run,
            self.emit,
            f"Skipped {title} — no readable text, most likely a scan.",
            kind="skipped",
        )
        return DeterministicUnitResult("skipped", result_refs, DOCUMENT_TEXT_UNAVAILABLE)

    @staticmethod
    def _parent(unit: dict, prefix: str) -> str:
        return next(
            ref.split(":", 1)[1]
            for ref in unit["parent_refs"]
            if ref.startswith(f"{prefix}:")
        )

    def _title(self, document_id: str) -> str:
        document = next(
            (item for item in self.ws.documents if str(item.get("id")) == document_id),
            None,
        )
        return str((document or {}).get("title") or document_id)

    # ------------------------------------------------------ documents.text_ready
    def _bind_extraction(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``documents.text_ready``.

        Extraction is content-addressed by source hash, so a repeated or resumed
        unit performs no work and produces no second artifact — which is why this
        capability needs no registered executor or receipt. A document with no
        extractable text is not a run failure: it is a document the auditor needs
        to look at, so the unit settles as ``awaiting_confirmation`` with the
        reason durably recorded.
        """
        self.ws = subject
        document_id = self._parent(unit, "document")
        task = self.add_task(
            "document_text", "workflow:document_text", "Document content"
        )
        self.task_status(task, "running")
        extracted = extract_text(
            self.ws,
            document_id,
            force=workflow_scope(self.run).get("generation_mode") == "force",
        )
        self.ws = load_workspace(self.ws.id)
        state = str(extracted.get("state") or "")
        if state == "failed":
            reason = str(
                extracted.get("error") or "The document has no extractable text."
            )
            self.warn(f"'{self._title(document_id)}' cannot be analyzed: {reason}")
            self.task_detail(task, f"{self._title(document_id)}: {reason}")
            self.task_status(task, "completed")
            return self._unreadable_document(document_id, (document_ref(document_id),))
        pages = len(extracted.get("pages") or [])
        self.task_detail(task, f"{self._title(document_id)}: {pages} page(s) extracted.")
        self.task_status(task, "completed")
        return DeterministicUnitResult("succeeded", (document_ref(document_id),))

    # -------------------------------------- documents.analysis_chunks_ready
    def _bind_chunk(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one source chunk to the shared pipeline as a proposal-only unit.

        A chunk analysis is an input to the reduced artifact, never a durable
        engagement record, so the unit declares no executor: its persisted
        proposal *is* the outcome. The pipeline still resolves and persists the
        content-free manifest before the provider call and still reuses a
        compatible proposal after a restart without re-billing.
        """
        self.ws = subject
        document_id = self._parent(unit, "document")
        chunk = self._map_for_unit(document_id, str(unit["id"]))
        if chunk is None:
            # The extraction this unit was expanded against no longer produces
            # this chunk (a re-extraction, or a narrowed page bound). Skipping is
            # correct: there is no source text left for it to be grounded in.
            return DeterministicUnitResult(
                "skipped",
                (),
                "This source chunk is no longer part of the extracted document.",
            )
        chunk_id = str(chunk["id"])
        task = self.add_task(
            "document_chunks", "workflow:document_chunks", "Document chunk analysis"
        )
        visual = chunk["kind"] == "document_visual_page_analysis"
        handles: list[dict] = []
        if visual:
            unsupported_reason = str(chunk.get("unsupported_reason") or "")
            if unsupported_reason:
                self.task_detail(
                    task,
                    f"{self._title(document_id)}: visual source is unsupported.",
                )
                self.task_status(task, "completed")
                return DeterministicUnitResult(
                    "awaiting_confirmation",
                    (),
                    DOCUMENT_VISUAL_SOURCE_UNSUPPORTED,
                )
            vision = dict(
                (self.run.get("model_profiles") or {}).get("vision") or {}
            )
            if (
                not vision.get("configured")
                or "vision" not in (vision.get("capabilities") or [])
            ):
                self.task_detail(
                    task,
                    f"{self._title(document_id)}: a vision profile is required.",
                )
                self.task_status(task, "completed")
                return DeterministicUnitResult(
                    "awaiting_confirmation",
                    (),
                    DOCUMENT_REQUIRES_VISION,
                )
            try:
                handles = document_media.prepare_document_page(
                    self.ws, document_id, int(chunk["page"])
                )
            except document_media.MediaPreparationError as error:
                self.warn(
                    f"'{self._title(document_id)}' page {chunk['page']} could "
                    f"not be prepared: {error}"
                )
                self.task_detail(task, str(error))
                self.task_status(task, "completed")
                return DeterministicUnitResult(
                    "awaiting_confirmation",
                    (),
                    VISUAL_PREPARATION_FAILED,
                )

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                (
                    document_visual_page_scope(
                        self.ws, document_id, handles
                    )
                    if visual
                    else document_voucher_scope(self.ws, document_id, chunk)
                    if chunk["kind"] == "document_voucher_analysis"
                    else document_chunk_scope(self.ws, document_id, chunk)
                ),
            )

        def on_committed(_stage, _unit, _outcome) -> DeterministicUnitResult:
            self.task_detail(
                task,
                f"{self._title(document_id)}: analyzed page {chunk['page']}.",
            )
            self.task_status(task, "completed")
            return DeterministicUnitResult(
                "succeeded",
                (
                    (
                        f"document_visual_page:{document_id}:{chunk['page']}"
                        if visual
                        else f"document_chunk:{document_id}:{chunk_id}"
                    ),
                ),
            )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id=(
                    VISUAL_WORKER_ID
                    if visual
                    else _TEXT_MAP_WORKERS[str(chunk["kind"])]
                ),
                executor_id=None,
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                    "chunk_id": chunk_id,
                    "page": int(chunk["page"]),
                    "modality": "image" if visual else "text",
                },
                activity={
                    "artifact_refs": list(unit.get("parent_refs") or []),
                    "document_ids": [document_id],
                    "task_id": task["id"],
                    "page_ranges": [int(chunk["page"])],
                },
                expected_revision=self.ws.revision,
                expected_parents={},
                capability_definition_hash=workflow.capability_definition_hash(capability),
                # A chunk analysis is intermediate generated text, never an
                # artifact an auditor approves; the reduced analysis is what the
                # auditor reviews.
                approval_kind=None,
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=None,
                model_profile_hash=(
                    str(
                        (
                            (self.run.get("model_profiles") or {})
                            .get("vision" if visual else "text", {})
                            .get("profile_hash")
                            or ""
                        )
                    )
                    or None
                ),
                input_modalities=(
                    ("text", "image") if visual else ("text",)
                ),
                prepared_media_hashes=tuple(
                    sorted(
                        str(item["prepared_sha256"]) for item in handles
                    )
                ),
                media_policy_hash=(
                    document_media.PREPARATION_POLICY_HASH
                    if visual
                    else None
                ),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=None,
            approval_provider=None,
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _chunk_unit_id(self, document_id: str, chunk_id: str) -> str:
        return semantic_unit_id("document_chunk", document_id, chunk_id)

    def _map_unit_id(self, document_id: str, spec: dict) -> str:
        if spec["kind"] in _TEXT_MAP_WORKERS:
            return self._chunk_unit_id(document_id, str(spec["id"]))
        return semantic_unit_id(
            "document_visual_page",
            document_id,
            spec["page"],
            spec["prepared_set_identity"],
        )

    def _map_for_unit(self, document_id: str, unit_id: str) -> dict | None:
        """The chunk a semantic unit ID names, resolved forwards.

        Unit IDs are slugified, so they are matched by regenerating each current
        chunk's ID rather than by parsing the unit ID back apart.
        """
        for chunk in analysis_unit_specs(
            self.ws, document_id, workflow_scope(self.run)
        ):
            if self._map_unit_id(document_id, chunk) == unit_id:
                return chunk
        return None

    # ------------------------------------- documents.analysis_generated
    def _bind_reduction(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one document's reduction unit to the shared pipeline.

        The reduction's only input is the chunk proposals this run persisted, read
        back from their sidecars in stable chunk order. A document whose chunks
        all failed has nothing to consolidate and settles for auditor attention
        rather than failing the run.
        """
        self.ws = subject
        document_id = self._parent(unit, "document")
        extracted = analyzable(self.ws, document_id)
        if extracted is None:
            return self._unreadable_document(document_id)
        scope = workflow_scope(self.run)
        chunks = analysis_unit_specs(self.ws, document_id, scope)
        analyses, missing = self._chunk_analyses(document_id, chunks)
        if not analyses:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                "No source chunk of this document was analyzed successfully.",
            )
        coverage = self._coverage(
            document_id, extracted, chunks, analyses, missing
        )
        task = self.add_task(
            "document_analysis", "workflow:document_analysis", "Document analysis"
        )
        target = DocumentAnalysisExecutorTarget(
            self.ws,
            self.run["id"],
            document_id,
            extracted=extracted,
            action=self.action(),
        )
        expected = parent_hashes(self.ws, [document_ref(document_id)])
        document_analysis.set_run_state(
            self.ws, document_id, "analyzing", run_id=self.run["id"]
        )

        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id=REDUCTION_WORKER_ID,
            executor_id="documents.analysis",
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
                "chunk_ids": [str(item.get("chunk_id") or "") for item in analyses],
                "generation_profiles": self._generation_profiles(analyses),
                "prepared_media_set_hashes": sorted(
                    {
                        str(item.get("prepared_media_set_hash") or "")
                        for item in analyses
                        if item.get("prepared_media_set_hash")
                    }
                ),
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": [document_id],
                "task_id": task["id"],
            },
            expected_revision=self.ws.revision,
            expected_parents=expected,
            capability_definition_hash=workflow.capability_definition_hash(capability),
            # A generated analysis is never approved into evidence by the agent:
            # it lands as generated content awaiting auditor review, which is the
            # separate ``documents.analysis_reviewed`` outcome.
            approval_kind=None,
            proposal_reference=unit.get("proposal_sidecar"),
            receipt_reference=unit.get("receipt_sidecar"),
        )

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                document_reduction_scope(self.ws, document_id, analyses),
            )

        def accept(proposal):
            """Complete the proposal before it becomes the accepted commit input.

            Coverage is a local fact about which of this document's chunks
            actually settled — not something the worker can know from the
            analyses it was handed — so the binding supplies it here, at the same
            boundary an auditor edit would be applied. The completed proposal is
            re-persisted before the executor sees it, so a resume commits exactly
            what was accepted.
            """
            completed = {
                **dict(proposal),
                "coverage": coverage,
                "generation_profiles": self._generation_profiles(analyses),
                "prepared_media_set_hash": self._prepared_media_set_hash(
                    analyses
                ),
                "vision_used": any(
                    item.get("modality") == "image" for item in analyses
                ),
                # Voucher evidence is reduced here deterministically and only
                # after every chunk proposal settles. Its notes and summary are
                # also consolidated locally below.
            }
            cycle_evidence = self._cycle_evidence(document_id, analyses)
            if cycle_evidence:
                completed.update(cycle_evidence)
                completed.update(
                    summary_markdown=document_analysis.render_voucher_summary(
                        cycle_evidence
                    ),
                    summary_origin="structured_evidence",
                    audit_notes_markdown=merge_voucher_audit_notes(analyses),
                )
            return completed

        def on_committed(_stage, _unit, outcome) -> DeterministicUnitResult:
            self.ws = target.workspace
            document_analysis.set_run_state(self.ws, document_id, "idle")
            self.emit(
                "workspace_changed",
                {"kind": "document_analysis", "id": document_id},
            )
            refs = tuple(outcome.receipt.artifact_refs) if outcome.receipt else ()
            self.record_artifact(
                "document_analysis",
                document_id,
                str((outcome.receipt.output if outcome.receipt else {}).get("analysis_id") or ""),
                "created",
                task,
            )
            if coverage["state"] != "complete":
                self.warn(
                    f"'{self._title(document_id)}' was analyzed to its configured page "
                    f"limit: {len(coverage['analyzed_pages'])} page(s) analyzed, "
                    f"{len(coverage['omitted_pages'])} omitted."
                )
                self.task_status(task, "completed")
                return DeterministicUnitResult(
                    "awaiting_confirmation", refs, PARTIAL_COVERAGE
                )
            self.task_detail(
                task,
                f"{self._title(document_id)}: consolidated {len(analyses)} analyzed "
                f"section{'s' if len(analyses) != 1 else ''}.",
            )
            self.task_status(task, "completed")
            return DeterministicUnitResult("succeeded", refs)

        if len(analyses) == 1:
            # Consolidating one chunk analysis into a document analysis is the
            # identity, so spending a provider turn on it would buy nothing. The
            # commit still goes through the same executor with the same
            # proposal-before-mutation, reconciliation, and receipt guarantees,
            # and folds through the same post-commit callback.
            return self._commit_reduction(
                unit, request, target, accept(analyses[0]), on_committed
            )

        if all(item.get("analysis_profile") == "voucher" for item in analyses):
            # Voucher facts are already structured and the registry reducer is
            # deterministic. Consolidate their notes and render their summary
            # locally instead of paying for a second model call that would only
            # paraphrase the same evidence.
            proposal = {
                "analysis_profile": "voucher",
                "derived_text_markdown": "\n\n".join(
                    str(item.get("derived_text_markdown") or "").strip()
                    for item in analyses
                    if str(item.get("derived_text_markdown") or "").strip()
                ),
                "summary_markdown": "",
                "audit_notes_markdown": "",
                "citations": [
                    dict(citation)
                    for item in analyses
                    for citation in item.get("citations") or []
                    if isinstance(citation, dict)
                ],
                "chunk_ids": [str(item.get("chunk_id") or "") for item in analyses],
            }
            return self._commit_reduction(
                unit,
                request,
                target,
                accept(proposal),
                on_committed,
                origin="voucher_structured_reduction",
            )

        return BoundUnitPipeline(
            request=request,
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=accept,
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _commit_reduction(
        self,
        unit: dict,
        request: UnitPipelineRequest,
        target: DocumentAnalysisExecutorTarget,
        proposal: dict,
        on_committed,
        *,
        origin: str = "single_chunk_reduction",
    ) -> DeterministicUnitResult:
        """Commit a locally derived reduction through the shared pipeline."""

        if self.unit_pipeline is None:
            raise WorkspaceError(
                "The document composition requires a UnitPipeline to commit."
            )

        def record(field: str):
            def persist(reference) -> None:
                unit[field] = dict(reference)
                self.save()

            return persist

        outcome = self.unit_pipeline.commit_local(
            request,
            proposal=proposal,
            target=target,
            origin=origin,
            on_proposal_persisted=record("proposal_sidecar"),
            on_receipt_persisted=record("receipt_sidecar"),
        )
        return on_committed(None, unit, outcome)

    def _chunk_analyses(
        self, document_id: str, chunks: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """Read this run's persisted chunk proposals in stable chunk order.

        A chunk with no compatible proposal is reported as missing rather than
        silently dropped, so the artifact's coverage records exactly which pages
        the analysis is grounded in.
        """
        analyses: list[dict] = []
        missing: list[dict] = []
        for chunk in chunks:
            try:
                payload = self.sidecars.load_proposal(
                    self._map_unit_id(document_id, chunk)
                )
            except WorkspaceError:
                payload = None
            proposal = (payload or {}).get("proposal")
            voucher_ready = (
                isinstance(proposal, dict)
                and proposal.get("analysis_profile") == "voucher"
                and bool(proposal.get("audit_notes_markdown"))
                and bool(proposal.get("registry"))
                and "record_fragments" in proposal
            )
            if not isinstance(proposal, dict) or not (
                proposal.get("summary_markdown") or voucher_ready
            ):
                missing.append(chunk)
                continue
            analyses.append(dict(proposal))
        return analyses, missing

    def _generation_profiles(self, analyses: list[dict]) -> list[dict]:
        profiles = self.run.get("model_profiles") or {}
        keys = {
            "vision" if item.get("modality") == "image" else "text"
            for item in analyses
        }
        if len(analyses) > 1:
            keys.add("text")
        return [
            {
                key: profile.get(key)
                for key in (
                    "name",
                    "provider",
                    "model",
                    "capabilities",
                    "configuration_source",
                    "profile_hash",
                )
            }
            for name in ("text", "vision")
            if name in keys
            for profile in [dict(profiles.get(name) or {})]
        ]

    @staticmethod
    def _cycle_evidence(document_id: str, analyses: list[dict]) -> dict:
        """Reduce voucher fragments after every map proposal has settled."""
        voucher = [
            item for item in analyses if item.get("analysis_profile") == "voucher"
        ]
        if not voucher:
            return {}
        fragments = [
            dict(fragment)
            for item in voucher
            for fragment in item.get("record_fragments") or []
        ]
        # The pack comes from the chunk proposals even when they found no record,
        # so a voucher-profile document that carries no transaction evidence still
        # commits a registry-backed, empty reduction rather than failing the unit.
        reduction = cycle_vouching.reduce_record_fragments(
            document_id,
            fragments,
            registry_ref=voucher[0].get("registry"),
        )
        return {
            "analysis_profile": "voucher",
            "registry": reduction["registry"],
            "record_fragments": fragments,
            "records": reduction["records"],
            "unresolved_fragments": reduction["unresolved_fragments"],
            "conflicts": reduction["conflicts"],
        }

    @staticmethod
    def _prepared_media_set_hash(analyses: list[dict]) -> str:
        values = sorted(
            {
                str(item.get("prepared_media_set_hash") or "")
                for item in analyses
                if item.get("prepared_media_set_hash")
            }
        )
        if not values:
            return ""
        return values[0] if len(values) == 1 else workflow.canonical_sha256(values)

    def _coverage(
        self,
        document_id: str,
        extracted: dict,
        chunks: list[dict],
        analyses: list[dict],
        missing: list[dict],
    ) -> dict:
        text_analyzed_pages = sorted(
            {
                int(page)
                for item in analyses
                if item.get("modality") != "image"
                for page in item.get("pages") or []
            }
        )
        vision_analyzed_pages = sorted(
            {
                int(page)
                for item in analyses
                if item.get("modality") == "image"
                for page in item.get("pages") or []
            }
        )
        analyzed_pages = sorted(
            {int(page) for item in analyses for page in item.get("pages") or []}
        )
        omissions: list[dict] = []
        vision_profile = dict(
            (self.run.get("model_profiles") or {}).get("vision") or {}
        )
        for chunk in missing:
            unit_error = self._unit_error(
                self._map_unit_id(document_id, chunk)
            )
            reason = (
                DOCUMENT_VISUAL_SOURCE_UNSUPPORTED
                if chunk.get("unsupported_reason")
                else DOCUMENT_REQUIRES_VISION
                if chunk.get("modality") == "image"
                and (
                    not vision_profile.get("configured")
                    or "vision"
                    not in (vision_profile.get("capabilities") or [])
                )
                else VISUAL_PREPARATION_FAILED
                if str(unit_error).startswith(VISUAL_PREPARATION_FAILED)
                else "vision_request_rejected"
                if str(unit_error).startswith("vision_request_rejected")
                else "map_unit_failed"
            )
            omissions.append(
                {"page": int(chunk["page"]), "reason": reason}
            )
        scope = workflow_scope(self.run)
        suffix = str(extracted.get("source_suffix") or "")
        visual_candidates = [
            int(page.get("page") or 0)
            for page in extracted.get("pages") or []
            if (
                bool(page.get("image_only"))
                or (
                    suffix == ".pdf"
                    and bool(page.get("no_usable_text_no_image"))
                )
                or document_id
                in set(scope.get("full_visual_document_ids") or [])
            )
        ]
        # Standalone image suffixes live in the document service; avoid an
        # import cycle in document_media by checking the stable set here.
        if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
            visual_candidates = [
                int(page.get("page") or 0)
                for page in extracted.get("pages") or []
            ]
        visual_limit = visual_page_limit(scope)
        for page in visual_candidates[visual_limit:]:
            omissions.append(
                {"page": page, "reason": "visual_page_limit_reached"}
            )
        omitted_pages = sorted(
            {item["page"] for item in omissions} - set(analyzed_pages)
        )
        return {
            "state": "partial" if omitted_pages else "complete",
            "analyzed_pages": analyzed_pages,
            "text_analyzed_pages": text_analyzed_pages,
            "vision_analyzed_pages": vision_analyzed_pages,
            "omitted_pages": omitted_pages,
            "omissions": omissions,
            "reason": "partial_coverage" if omitted_pages else None,
        }

    def _unit_error(self, unit_id: str) -> str:
        for stage in (self.run.get("workflow") or {}).get("stages") or []:
            for unit in stage.get("units") or []:
                if str(unit.get("id")) == unit_id:
                    return str(unit.get("error") or "")
        return ""

    # ------------------------------------- documents.analysis_reviewed
    def _bind_review(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``documents.analysis_reviewed``.

        Nothing the agent does can satisfy this outcome. A generated summary is
        not evidence that a control operated, so the unit records that the
        analysis is awaiting the auditor's own review decision and settles as
        ``awaiting_confirmation`` — never as succeeded.
        """
        self.ws = subject
        document_id = self._parent(unit, "document")
        task = self.add_task(
            "document_review", "workflow:document_review", "Document analysis review"
        )
        self.task_status(task, "running")
        self.task_detail(
            task,
            f"{self._title(document_id)}: generated analysis is awaiting auditor review.",
        )
        self.task_status(task, "completed")
        return DeterministicUnitResult(
            "awaiting_confirmation",
            (analysis_ref(document_id),),
            DOCUMENT_REVIEW_REQUIRED,
        )

    # ------------------------------------------------------------ checkpoint
    def _scope_checkpoint(self) -> None:
        """Settle an ambiguous document scope before any capability fans out.

        The scope is ambiguous only when the request named nothing and the
        workspace holds more planning-relevant documents than the bounded
        fallback analyses. In permission mode that is a question for the auditor;
        in auto mode the bounded deterministic selection stands and is reported as
        a warning, so the run never silently analyses an arbitrary subset.
        """
        state = self.run.setdefault("document_analysis", {})
        if state.get("scope_settled"):
            return
        document_scope = self.scope()
        if document_scope.ambiguity is None:
            return
        state["scope_settled"] = True
        if self.run.get("mode") != "permission":
            self.warn(document_scope.ambiguity)
            self.save()
            return
        interaction = next(
            (
                item
                for item in self.run.get("interactions") or []
                if item.get("type") == "document_scope"
                and item.get("status") == "pending"
            ),
            None,
        )
        eligible = sorted(str(item.get("id")) for item in self.ws.documents)
        if interaction is None:
            interaction = {
                "id": f"int_{uuid.uuid4().hex[:12]}",
                "action_id": "workflow:document_scope",
                "type": "document_scope",
                "prompt": (
                    "Which documents should the analysis cover? "
                    f"Choose up to {MAX_SCOPE_DOCUMENTS}."
                ),
                "options": eligible,
                "payload": {
                    "eligible_documents": eligible,
                    "default_documents": list(document_scope.document_ids),
                    "max_documents": MAX_SCOPE_DOCUMENTS,
                    "reason": document_scope.ambiguity,
                },
                "policy_reason": (
                    "The requested scope is ambiguous and materially changes the "
                    "analysis."
                ),
                "status": "pending",
                "response": None,
                "actor": None,
                "created_at": store.utcnow(),
                "resolved_at": None,
            }
            self.run.setdefault("interactions", []).append(interaction)
            self.run["workflow"]["pending_checkpoint"] = interaction["id"]
            self.save()
            self.emit("checkpoint_request", {"interaction": interaction})
        response = self.runtime.wait_for_interaction(interaction)
        chosen = [
            str(value).strip()
            for value in (response.get("documents") or response.get("options") or [])
            if str(value).strip() in set(eligible)
        ]
        if not chosen:
            text = str(response.get("text") or "")
            chosen = [document_id for document_id in eligible if document_id in text]
        if chosen:
            scope = dict(self.run["workflow"].get("scope") or {})
            scope["document_ids"] = chosen[:MAX_SCOPE_DOCUMENTS]
            self.run["workflow"]["scope"] = scope
        else:
            self.warn(document_scope.ambiguity)
        self.run["workflow"]["pending_checkpoint"] = None
        self.runtime.resolve_interaction(interaction, response)
        self.emit(
            "checkpoint_resolved",
            {"interaction_id": interaction["id"], "count": len(chosen)},
        )

    # ---------------------------------------------------------------- finish
    def _finish_projection(
        self,
        subject: Workspace,
        _workflow_state: dict,
        stages: tuple[dict, ...],
    ) -> FinishProjection:
        """Close the run on real document outcomes, not on unit bookkeeping."""

        self.ws = subject
        document_scope = self.scope()
        units = [unit for stage in stages for unit in stage.get("units") or []]
        failed = sum(unit.get("status") in {"failed", "conflict"} for unit in units)
        open_units = sum(
            unit.get("status")
            in {"blocked", "awaiting_input", "awaiting_confirmation"}
            for unit in units
        )
        analyzed = [
            document_id
            for document_id in document_scope.document_ids
            if has_generated_analysis(subject, document_id)
        ]
        next_outcomes = list(unsettled_capabilities(stages))
        self.run["workflow"]["workspace_revision"] = subject.revision
        for document_id in document_scope.document_ids:
            # No document may be left claiming an analysis is in flight once the
            # run is terminal. Only a document still marked in flight is written,
            # so a terminal run does not churn every index it merely read.
            state = str(
                document_analysis.load_index(subject, document_id).get("run_state") or ""
            )
            if state in {"queued", "analyzing"}:
                document_analysis.set_run_state(
                    subject, document_id, "failed" if failed else "idle"
                )
        terminal = fold_terminal_status(stages)
        if failed:
            self.run["error"] = first_unit_error(
                stages, "One or more document units failed."
            )
        summary = narration.summary_markdown(
            "Document analysis",
            [
                ("Documents in scope", len(document_scope.document_ids)),
                ("Generated analyses", len(analyzed)),
                ("Failed or conflicting units", failed),
                ("Open workflow units", open_units),
            ],
            "Generated content is awaiting auditor review and is not evidence that "
            "controls operated.",
        )
        return FinishProjection(
            next_outcomes=tuple(dict.fromkeys(next_outcomes)),
            summary_markdown=summary,
            terminal_status=terminal,
        )


# Every edge in this graph is partial. One document with no extractable text, or
# one chunk that could not be analyzed, must not stop the documents the run can
# still analyze: each later capability re-expands against what the earlier one
# actually produced.
_PARTIAL_DEPENDENCIES = {
    "documents.analysis_chunks_ready": {"documents.text_ready"},
    "documents.analysis_generated": {"documents.analysis_chunks_ready"},
    "documents.analysis_reviewed": {"documents.analysis_generated"},
}

_PIPELINE_BINDERS = {
    "documents.analysis_chunks_ready": (
        "_bind_chunk",
        {
            "workers_by_unit_kind": {
                **_TEXT_MAP_WORKERS,
                "document_visual_page_analysis": VISUAL_WORKER_ID,
            },
            "executor": None,
        },
    ),
    "documents.analysis_generated": (
        "_bind_reduction",
        {"worker": REDUCTION_WORKER_ID, "executor": "documents.analysis"},
    ),
}
_DETERMINISTIC_BINDERS = {
    "documents.text_ready": ("_bind_extraction", {"deterministic": "documents.extract"}),
    "documents.analysis_reviewed": (
        "_bind_review",
        {"deterministic": "documents.review"},
    ),
}


def build_document_capability_executions(
    adapter: DocumentWorkflowExecution,
    capabilities,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityExecutionRegistry:
    """Register one execution binding per supplied document capability.

    Shared with the audit composition, which declares the same three generation
    capabilities and no auditor-review outcome, so both graphs run one document
    implementation rather than two that could drift.
    """
    executions = executions or CapabilityExecutionRegistry()
    for capability in capabilities:
        pipeline_binding = _PIPELINE_BINDERS.get(capability.id)
        if pipeline_binding is not None:
            binder, identity = pipeline_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=workflow.canonical_sha256(
                        {"capability": capability.id, **identity}
                    ),
                    pipeline_binder=getattr(adapter, binder),
                )
            )
            continue
        binder, identity = _DETERMINISTIC_BINDERS[capability.id]
        executions.register(
            CapabilityExecution(
                capability_id=capability.id,
                implementation_hash=workflow.canonical_sha256(
                    {"capability": capability.id, **identity}
                ),
                deterministic_executor=getattr(adapter, binder),
            )
        )
    return executions


def build_documents_workflow_runner(
    workspace: Workspace,
    run: dict,
    handle,
    *,
    runtime: RunRuntime | None = None,
    context_resolver: ContextResolver | None = None,
) -> WorkflowRunner:
    """Compose the domain-neutral scheduler with the document bindings."""

    adapter = DocumentWorkflowExecution(
        workspace,
        run,
        handle,
        runtime=runtime,
        context_resolver=context_resolver,
    )
    unit_pipeline = UnitPipeline(
        runtime=adapter.runtime,
        gateway=adapter.model_gateway,
        workers=WORKERS,
        executors=EXECUTORS,
        sidecars=adapter.sidecars,
    )
    adapter.unit_pipeline = unit_pipeline
    executions = build_document_capability_executions(
        adapter, DOCUMENTS_REGISTRY.all()
    )

    checkpoint_handlers = {DOCUMENT_SCOPE_CHECKPOINT: adapter._scope_checkpoint}

    def before_stage(
        subject: Workspace,
        capability: workflow.Capability,
        _stage: dict,
    ) -> None:
        adapter.ws = subject
        checkpoint = STAGE_CHECKPOINTS.get(capability.id)
        if checkpoint is not None:
            checkpoint_handlers[checkpoint]()

    def dependency_policy(
        capability_id: str,
        dependency_id: str,
        _dependency_status: str,
    ) -> bool:
        return dependency_id in _PARTIAL_DEPENDENCIES.get(capability_id, set())

    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=adapter.runtime,
        registry=DOCUMENTS_REGISTRY,
        executions=executions,
        unit_pipeline=unit_pipeline,
        refresh_subject=lambda: refresh_workspace(adapter),
        refresh_limits=lambda _subject: adapter._refresh_dynamic_limits(),
        dependency_policy=dependency_policy,
        before_stage=before_stage,
        milestone_projector=adapter.milestone_projection,
        finish_evaluator=adapter._finish_projection,
    )
    adapter.scheduler = scheduler
    scheduler.execution_adapter = adapter
    return scheduler


__all__ = [
    "DocumentWorkflowExecution",
    "build_document_capability_executions",
    "build_documents_workflow_runner",
]
