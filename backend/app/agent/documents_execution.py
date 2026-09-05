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

from .. import (
    document_analysis,
    document_classification,
    document_masters,
    document_media,
    document_schemas,
    document_types,
    intake,
)
from ..text import counted
from ..workspace_transactions import parent_hashes
from ..workspaces import Workspace, WorkspaceError
from . import narration, store, workflow
from .base import BaseRunner
from .workflow import semantic_unit_id
from .capabilities import DOCUMENTS_REGISTRY
from .capabilities import documents as document_capabilities
from .capabilities.documents import (
    DOCUMENT_SCOPE_CHECKPOINT,
    MAX_SCOPE_DOCUMENTS,
    STAGE_CHECKPOINTS,
    DocumentScope,
    analysis_unit_specs,
    analyzable,
    chunk_specs,
    evidence_read_media,
    evidence_read_pages,
    evidence_read_text,
    has_generated_analysis,
    preparation_model_turns,
    read_over_window,
    resolve_document_scope,
    visual_page_limit,
)
from .context import (
    ContextResolver,
    document_category_scope,
    document_chunk_scope,
    document_classification_scope,
    document_evidence_read_scope,
    document_structured_chunk_scope,
    document_reduction_scope,
    document_visual_page_scope,
)
from .executors import EXECUTORS
from .executors.documents import (
    DocumentCategoryExecutorTarget,
    DocumentClassificationExecutorTarget,
    DocumentReadExecutorTarget,
    DocumentStampExecutorTarget,
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
from .execution_support import (
    refresh_workspace,
    resolve_context,
    stage_checkpoint,
    workflow_scope,
)
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
    CATEGORY_WORKER_ID,
    CLASSIFY_WORKER_ID,
    READ_WORKER_ID,
    master_descriptor,
    STRUCTURED_WORKER_ID,
    schema_descriptor,
    CHUNK_WORKER_ID,
    REDUCTION_WORKER_ID,
    VISUAL_WORKER_ID,
)

# Text-modality map profiles, by unit kind. A chunk reads exactly what a
# standard chunk reads, so both resolve ``document_chunk_scope`` and differ only
# in which worker consumes it.
_TEXT_MAP_WORKERS = {
    "document_chunk_analysis": CHUNK_WORKER_ID,
    "document_structured_analysis": STRUCTURED_WORKER_ID,
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
        count rather than a fixed constant — plus the classification and schema
        turns the run spends before it reaches any analysis, which are stages of
        this same workflow and were previously unbudgeted.
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
        preparation = preparation_model_turns(self.ws, scope)
        calculated = 4 + len(specs) + 2 * max(
            1, len(document_scope.document_ids)
        ) + preparation
        prompt_allowance = (
            4 * 12_000
            + text_units * 12_000
            + visual_units * 20_480
            + max(1, len(document_scope.document_ids)) * 12_000
            + preparation * 12_000
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
                    f"{counted(len(scope.document_ids), 'scoped document')}; "
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
        # Observations the analysis recorded against the documents themselves —
        # a policy with no version, minutes that do not say whether they are
        # final. They were once counted on the planning milestone, where they
        # read as defects in the memorandum rather than in the material it was
        # drafted from. They are notes this stage wrote, so they are counted
        # here, against the analyses that hold them.
        noted: list[str] = []
        observations = 0
        for document_id in scope.document_ids:
            if not has_generated_analysis(subject, document_id):
                continue
            analyzed.append(document_id)
            recorded = len(document_analysis.audit_observations(subject, document_id))
            if recorded:
                noted.append(document_id)
                observations += recorded
            try:
                envelope = document_analysis.load_analysis(subject, document_id)
            except WorkspaceError:
                envelope = {}
            # `load_analysis` returns an envelope — document_id, revisions,
            # generated, effective, candidate, review, status. Coverage lives on
            # the analysis record inside it, never at the top level, so reading
            # `envelope["coverage"]` yields None for every document and reports
            # a fully covered run as entirely partial.
            record = envelope.get("effective") or envelope.get("generated") or {}
            state = str((record.get("coverage") or {}).get("state") or "")
            # Only an explicit non-complete state is partial. An artifact with no
            # coverage block at all is unknown, and "only part of this document
            # was covered" would be a claim its record does not support.
            if state and state != "complete":
                partial.append(document_id)
        skipped = max(0, len(scope.document_ids) - len(analyzed))
        return {
            "status": (
                "completed_with_issues" if partial or skipped else "completed"
            ),
            "headline": "Document analysis complete",
            "summary": (
                f"Generated analyses for {len(analyzed)} of "
                f"{counted(len(scope.document_ids), 'scoped document')}. "
                f"{len(partial)} have partial coverage and {skipped} were not analyzed. "
                + (
                    f"They record {counted(observations, 'observation')} across "
                    f"{counted(len(noted), 'document')}. "
                    if observations else ""
                )
                + "Generated analyses remain subject to auditor review."
            ),
            "metrics": [
                {"label": "Documents in scope", "value": len(scope.document_ids)},
                {"label": "Analyses generated", "value": len(analyzed)},
                {"label": "Observations recorded", "value": observations},
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
        self.ws = self.ws.reload()
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
        self.task_detail(task, f"{self._title(document_id)}: {counted(pages, 'page')} extracted.")
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
        structured = chunk["kind"] == "document_structured_analysis"
        structured_input: dict = {}
        if structured:
            document_type = document_classification.document_type(self.ws, document_id)
            schema = document_schemas.load_schema(self.ws, document_type)
            if schema is None:
                # The schema was re-derived away between expansion and binding.
                # Settling rather than falling back to another profile keeps the
                # extraction honest about what it was going to extract against.
                return DeterministicUnitResult(
                    "awaiting_confirmation",
                    (),
                    f"No schema is current for '{document_type}'.",
                )
            chunks = list(
                analysis_unit_specs(self.ws, document_id, workflow_scope(self.run))
            )
            structured_input = {
                "document_type": document_type,
                # Induction read this document's fields, and this chunk is the
                # whole document. Both together are what make an empty
                # extraction a contradiction rather than a quiet page — see
                # ``validate_structured_proposal``.
                "schema_sampled_this_document": document_id
                in list(schema.get("derived_from") or []),
                "sole_chunk": len(chunks) == 1,
                "schema_fields": list(schema.get("fields") or []),
                "schema_descriptor": schema_descriptor(
                    document_type, schema.get("fields") or []
                ),
                # Stamped onto the extraction so a schema that moves afterwards
                # makes this analysis stale rather than silently reinterpreted.
                "schema_ref": {
                    "document_type": schema["document_type"],
                    "schema_version": schema["schema_version"],
                    "schema_hash": schema["schema_hash"],
                },
            }
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
                    else document_structured_chunk_scope(self.ws, document_id, chunk)
                    if structured
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
                    "document_id": document_id,
                    **structured_input,
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
    def _bind_category(
        self,
        subject: Workspace,
        run: dict,
        capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one document's category to the shared pipeline.

        Nothing per-workspace travels on this unit beyond the page itself: the
        four values are the same in every engagement, so unlike the type there is
        no catalog to keep in step between the prompt and the validator.
        """
        self.ws = subject
        document_id = self._parent(unit, "document")
        extracted = analyzable(self.ws, document_id)
        if extracted is None:
            return self._unreadable_document(document_id)
        unit_input = dict(unit.get("input_payload") or {})
        text = str(unit_input.get("text") or "") or document_classification.classification_text(
            self.ws, document_id
        )
        if not text:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                "This document has no extracted text to classify it from.",
            )
        task = self.add_task(
            "document_categories",
            "workflow:document_categories",
            "Document classification",
        )
        target = DocumentCategoryExecutorTarget(self.ws, self.run["id"], document_id)
        expected = parent_hashes(self.ws, [document_ref(document_id)])

        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id=CATEGORY_WORKER_ID,
            executor_id="documents.category",
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
                "document_id": document_id,
                "title": str(unit_input.get("title") or ""),
                "text": text,
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": [document_id],
                "task_id": task["id"],
                "page_ranges": [1],
            },
            expected_revision=self.ws.revision,
            expected_parents=expected,
            capability_definition_hash=workflow.capability_definition_hash(capability),
            # What an engagement holds a document as is a statement, not evidence
            # an auditor signs off. It is revisable — by the same auditor
            # override the type carries — which is where their judgement enters.
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
                document_category_scope(self.ws, document_id, text),
            )

        return BoundUnitPipeline(
            request=request,
            target=target,
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
        )

    def _bind_classification(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one document's type classification to the shared pipeline.

        The offered catalog and selectable ids come from the unit's expansion
        rather than being recomputed here, because they are what the prompt will
        show and what the semantic validator checks against. Recomputing them at
        bind time would let an auditor coining a type mid-run put the two out of
        step, and a correct answer would then be rejected.
        """
        self.ws = subject
        document_id = self._parent(unit, "document")
        extracted = analyzable(self.ws, document_id)
        if extracted is None:
            return self._unreadable_document(document_id)
        unit_input = dict(unit.get("input_payload") or {})
        text = str(unit_input.get("text") or "") or document_classification.classification_text(
            self.ws, document_id
        )
        if not text:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                "This document has no extracted text to identify it from.",
            )
        selectable = list(unit_input.get("selectable_types") or [])
        catalog = str(unit_input.get("catalog") or "")
        if not selectable or not catalog:
            local = document_schemas.local_types(self.ws)
            selectable = list(document_schemas.effective_type_ids(self.ws))
            catalog = document_types.prompt_catalog(local_types=local)
        task = self.add_task(
            "document_types", "workflow:document_types", "Document type"
        )
        target = DocumentClassificationExecutorTarget(
            self.ws,
            self.run["id"],
            document_id,
            catalog_sha1=str(
                unit_input.get("catalog_sha1")
                or document_classification.catalog_signature(self.ws)
            ),
        )
        expected = parent_hashes(self.ws, [document_ref(document_id)])

        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id=CLASSIFY_WORKER_ID,
            executor_id="documents.classification",
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
                "document_id": document_id,
                "title": str(unit_input.get("title") or ""),
                "text": text,
                "selectable_types": selectable,
                "catalog": catalog,
                "catalog_sha1": str(
                    unit_input.get("catalog_sha1")
                    or document_classification.catalog_signature(self.ws)
                ),
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": [document_id],
                "task_id": task["id"],
                "page_ranges": [1],
            },
            expected_revision=self.ws.revision,
            expected_parents=expected,
            capability_definition_hash=workflow.capability_definition_hash(capability),
            # A type is a statement about what a document is, not evidence an
            # auditor signs off. It is revisable by retyping, which is where the
            # auditor's judgement enters.
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
                document_classification_scope(self.ws, document_id, text),
            )

        return BoundUnitPipeline(
            request=request,
            target=target,
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
        )

    def _bind_evidence_read(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one evidence document to be read whole against its type's master.

        Every document binding against state its predecessor committed *is* the
        master accumulating. That is what the sequential barrier buys and why it
        is the mechanism rather than a concession: the parallel path binds every
        unit before running any of them, so a unit's input is resolved at stage
        start and can never see what a sibling settled. Reading the master here,
        at bind time, is the whole design in one line.
        """

        self.ws = subject
        unit_input = dict(unit.get("input_payload") or {})
        document_id = self._parent(unit, "document")
        document_type = str(
            unit_input.get("document_type") or ""
        ) or document_classification.document_type(self.ws, document_id)
        extracted = analyzable(self.ws, document_id)
        if extracted is None:
            return self._unreadable_document(document_id)
        scope = workflow_scope(self.run)
        over = read_over_window(self.ws, document_id, scope)
        if over is not None:
            # Reported, never truncated. A citation binds to text the worker saw,
            # and the master's whole value rests on absence meaning "the document
            # does not state this" rather than "nobody looked at that page".
            return DeterministicUnitResult("awaiting_confirmation", (), over)
        text = evidence_read_text(self.ws, document_id)
        if not text:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                "This document has no extracted text to read.",
            )
        self._reset_master_once(document_type, scope)
        master = document_masters.master(self.ws, document_type)
        media = evidence_read_media(self.ws, document_id, scope)
        handles, unsupported = self._prepared_read_media(document_id, media)
        if unsupported:
            return DeterministicUnitResult("awaiting_confirmation", (), unsupported)
        task = self.add_task(
            "document_analysis", "workflow:document_analysis", "Document analysis"
        )
        mode = document_capabilities.vocabulary_mode(scope)
        target = DocumentReadExecutorTarget(
            self.ws,
            self.run["id"],
            document_id,
            document_type,
            extracted=extracted,
            action=(
                "refresh"
                if str(scope.get("generation_mode") or "") == "force"
                else "analyze"
            ),
            vocabulary_mode=mode,
        )
        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id=READ_WORKER_ID,
            executor_id="documents.read",
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
                "document_id": document_id,
                "document_type": document_type,
                "title": str(unit_input.get("title") or ""),
                # Prior art: what this type's documents before it settled on.
                # It travels on the unit input rather than in the prompt, so the
                # prompt hash stays stable while the vocabulary varies per
                # workspace — and a master that moved re-expands this unit rather
                # than leaving a reading made under names nobody else used.
                "master_ref": str(master.get("master_ref") or ""),
                "master_fields": list(master.get("fields") or []),
                "master_descriptor": master_descriptor(
                    document_type,
                    list(master.get("fields") or []),
                    documents_read=len(list(master.get("documents_read") or [])),
                ),
                "vocabulary_mode": mode,
                # Re-preparing a page moves this and the document is read again,
                # rather than being reduced under images it never saw. Same
                # interlock the page-at-a-time path keeps.
                "prepared_set_identities": [
                    str(item.get("prepared_set_identity") or "") for item in media
                ],
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": [document_id],
                "task_id": task["id"],
            },
            expected_revision=self.ws.revision,
            expected_parents=parent_hashes(self.ws, [document_ref(document_id)]),
            capability_definition_hash=workflow.capability_definition_hash(capability),
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
                document_evidence_read_scope(
                    self.ws,
                    document_id,
                    text,
                    evidence_read_pages(self.ws, document_id),
                    handles,
                ),
            )

        return BoundUnitPipeline(
            request=request,
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
        )

    def _reset_master_once(self, document_type: str, scope: dict) -> None:
        """Discard a type's master the first time this run re-reads it.

        ``revise_vocabulary`` rebuilds rather than appends, and the difference
        matters: reading the type from the start in order is what keeps
        ``introduced_at`` meaningful and the late-field sweep bounded, where
        appending to an existing master would leave indices that no longer
        describe what any document was asked. That does not fail — it makes the
        sweep run over the wrong set, silently.

        Once per type per run, recorded on the run so a resumed pass appends to
        what it has already rebuilt rather than discarding it a second time and
        renumbering the documents it just read.
        """

        if document_capabilities.vocabulary_mode(scope) != "rebuild":
            return
        state = self.run.setdefault("document_analysis", {})
        rebuilt = state.setdefault("vocabulary_rebuilt", [])
        if document_type in rebuilt:
            return
        document_masters.reset(self.ws, document_type)
        rebuilt.append(document_type)
        self.save()
        self.warn(
            f"Rebuilding the vocabulary for '{document_type}': every document of "
            "this type is re-read in order, and the schema is stamped from what "
            "that pass produces."
        )

    def _prepared_read_media(
        self, document_id: str, specs: list[dict]
    ) -> tuple[list[dict], str | None]:
        """Prepare the document's visually-routed pages for the read.

        Optional by design and by measurement: most evidence is digital and
        routes no page here. What is not optional is that a scanned page, where
        one exists, reaches the same call as the text — a fully scanned
        confirmation would otherwise contribute nothing and produce no records,
        silently, and the commoner case is worse: a mostly-digital PDF read for
        everything except the page the signature is on.
        """

        handles: list[dict] = []
        for spec in specs:
            if spec.get("unsupported_reason"):
                # An unreadable page must be stated rather than skipped. The
                # ``.docx`` image-only case still reports
                # ``document_visual_source_unsupported``.
                return [], str(spec["unsupported_reason"])
            try:
                prepared = document_media.prepare_document_page(
                    self.ws, document_id, int(spec.get("page") or 0)
                )
            except document_media.MediaPreparationError as error:
                return [], (
                    f"Page {spec.get('page')} could not be prepared for reading: "
                    f"{error}"
                )
            handles.extend(dict(item) for item in prepared)
        return handles, None

    def _bind_schema_stamp(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Write one finished master into a schema. No model turn.

        The stamp is a dependent capability rather than the last unit of the
        read, and the reason is the one Phase 3 already paid for: units within a
        stage execute in sorted id order, so a capability holding both the
        readings and the freeze would bind the freeze first and read back
        nothing.
        """

        self.ws = subject
        # ``input_payload`` does not reach a binder — the scheduler stores only
        # kind, title, parent_refs and input_sha1 — so the stamp recovers its
        # type from the parents it was expanded against. Those *are* the
        # documents whose reading built the master, which makes the recovery
        # exact rather than a re-derivation that could name a different type.
        read_parents = [
            str(ref).split(":", 1)[1]
            for ref in unit.get("parent_refs") or []
            if str(ref).startswith("document:")
        ]
        document_type = next(
            (
                document_classification.document_type(self.ws, document_id)
                for document_id in read_parents
                if document_classification.document_type(self.ws, document_id)
            ),
            "",
        )
        if not document_type:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                "This stamp names no document type: none of the documents it was "
                "expanded against carries one.",
            )
        master = document_masters.master(self.ws, document_type)
        read = [str(value) for value in master.get("documents_read") or []]
        if not document_masters.schema_fields(master):
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                f"No document of '{document_type}' was read, so it has no "
                "vocabulary to stamp.",
            )
        task = self.add_task(
            "document_schemas", "workflow:document_schemas", "Document schema"
        )
        target = DocumentStampExecutorTarget(self.ws, self.run["id"], document_type)
        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id=None,
            executor_id="documents.stamp",
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
                "document_type": document_type,
                "documents_read": read,
                "master_ref": str(master.get("master_ref") or ""),
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": read,
                "task_id": task["id"],
            },
            expected_revision=self.ws.revision,
            # The documents the vocabulary was read from. A schema is a claim
            # about them, so replacing one under this commit is a real conflict
            # rather than a race to ignore.
            expected_parents=parent_hashes(
                self.ws, [document_ref(document_id) for document_id in read]
            ),
            capability_definition_hash=workflow.capability_definition_hash(capability),
            approval_kind=None,
            proposal_reference=unit.get("proposal_sidecar"),
            receipt_reference=unit.get("receipt_sidecar"),
        )
        return self._commit_stamp(unit, request, target, {"document_type": document_type})

    def _commit_stamp(
        self,
        unit: dict,
        request: UnitPipelineRequest,
        target: DocumentStampExecutorTarget,
        proposal: dict,
    ) -> DeterministicUnitResult:
        """Commit a stamp through the shared pipeline, with no model.

        Through ``commit_local`` rather than as a bare file write, so it keeps
        the same proposal-before-mutation, reconciliation and receipt guarantees
        a model-backed unit gets — the shape the freeze binder already used when
        its samples agreed.
        """

        if self.unit_pipeline is None:
            raise WorkspaceError(
                "The document composition requires a UnitPipeline to commit."
            )

        def record(field: str):
            def persist(reference) -> None:
                unit[field] = dict(reference)
                self.save()

            return persist

        self.unit_pipeline.commit_local(
            request,
            proposal=proposal,
            target=target,
            origin="master_stamp",
            on_proposal_persisted=record("proposal_sidecar"),
            on_receipt_persisted=record("receipt_sidecar"),
        )
        return DeterministicUnitResult(
            "succeeded", (f"document_schema:{target.document_type}",)
        )


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
            }
            if str(proposal.get("analysis_profile") or "") == "structured":
                records = list(proposal.get("records") or [])
                completed.update(
                    summary_markdown=document_analysis.render_structured_summary(
                        records,
                        str((proposal.get("schema_ref") or {}).get("document_type") or ""),
                    ),
                    summary_origin="structured_evidence",
                    audit_notes_markdown=document_analysis.render_structured_audit_notes(
                        analyses
                    ),
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
                    f"limit: {counted(len(coverage['analyzed_pages']), 'page')} analysed, "
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

        if analyses and all(
            item.get("analysis_profile") == "structured" for item in analyses
        ):
            # Structured facts are already typed against the document's schema,
            # so consolidating them is a local concatenation. Paying for a model
            # turn here would only paraphrase evidence that is already exact.
            records = [
                record
                for item in analyses
                for record in item.get("records") or []
            ]
            # Bound here rather than echoed back by the worker, for the same
            # reason every other derived value is: a model transcribing a stamp
            # it cannot verify adds no evidence and one failure mode. The
            # interlock that proves the extraction used *this* schema is
            # stronger and already in place — the schema descriptor travels in
            # the unit input, so a re-derived schema moves the unit's input hash
            # and the chunks re-expand rather than being reduced under fields
            # they never saw.
            document_type = document_classification.document_type(self.ws, document_id)
            schema = document_schemas.load_schema(self.ws, document_type)
            if schema is None:
                return DeterministicUnitResult(
                    "awaiting_confirmation",
                    (),
                    f"No schema is current for '{document_type}'.",
                )
            proposal = {
                "analysis_profile": "structured",
                "schema_ref": {
                    "document_type": schema["document_type"],
                    "schema_version": schema["schema_version"],
                    "schema_hash": schema["schema_hash"],
                },
                "records": records,
                "summary_markdown": "",
                "audit_notes_markdown": "",
                "derived_text_markdown": "",
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
                origin="structured_reduction",
            )

        if len(analyses) == 1:
            # Consolidating one chunk analysis into a document analysis is the
            # identity, so spending a provider turn on it would buy nothing. The
            # commit still goes through the same executor with the same
            # proposal-before-mutation, reconciliation, and receipt guarantees,
            # and folds through the same post-commit callback.
            return self._commit_reduction(
                unit, request, target, accept(analyses[0]), on_committed
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
            # A structured chunk that genuinely carried no record is complete,
            # not missing: an empty ``records`` array is the truthful answer for
            # a page of prose inside a transaction document, and treating it as
            # a gap would report coverage the extraction did not actually lack.
            structured_ready = (
                isinstance(proposal, dict)
                and proposal.get("analysis_profile") == "structured"
                and "records" in proposal
            )
            if not isinstance(proposal, dict) or not (
                proposal.get("summary_markdown") or structured_ready
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


# Every edge in this graph is partial. One document with no extractable text,
# one type whose sample could not be read, or one chunk that could not be
# analyzed, must not stop the documents the run can still analyze: each later
# capability re-expands against what the earlier one actually produced.
#
# The map used to say that and list three of the seven edges, which left the
# four upstream ones fully blocking. One failed sample then failed the
# sampling stage, blocked induction, blocked chunking, and blocked every
# analysis in the run — the whole engagement starved by one document.
#
# Under 4b.1 the downgrade that completion once created is gone with the routing
# question behind it: transaction evidence has its own pass and its own
# readiness, so an unschema'd voucher no longer "reaches analysis as prose" —
# there is no shared stage left for it to fall through. What replaces the
# warning is the one edge kept blocking below.
_PARTIAL_DEPENDENCIES = {
    "documents.categorized": {"documents.text_ready"},
    "documents.types_classified": {"documents.categorized"},
    "documents.evidence_read": {"documents.types_classified"},
    # Partial, and the guarantee it used to carry moved to where it belongs. A
    # master built from eight of eighteen documents is not the type's
    # vocabulary, and stamping it would write a ``schema_version`` claiming
    # otherwise — but expressing that as a *stage* edge made it a claim about
    # the whole corpus. Measured: one bank statement failed on a dangling
    # citation and blocked every stamp in the engagement, including two types
    # whose documents had all read cleanly and agreed with each other.
    #
    # ``_types_awaiting_stamp`` now asks the question per type, which is where
    # the plan puts it: a type with an unread document is not offered for
    # stamping, keeps its ``master_ref`` readings, and is reported — and a type
    # that read cleanly is stamped whatever happened to its neighbours.
    "documents.schemas_stamped": {"documents.evidence_read"},
    "documents.analysis_chunks_ready": {
        "documents.text_ready",
        "documents.categorized",
    },
    "documents.analysis_generated": {"documents.analysis_chunks_ready"},
    "documents.analysis_reviewed": {"documents.analysis_generated"},
}

_PIPELINE_BINDERS = {
    "documents.evidence_read": (
        "_bind_evidence_read",
        {"worker": READ_WORKER_ID, "executor": "documents.read"},
    ),
    "documents.schemas_stamped": (
        "_bind_schema_stamp",
        {"worker": None, "executor": "documents.stamp"},
    ),
    "documents.categorized": (
        "_bind_category",
        {"worker": CATEGORY_WORKER_ID, "executor": "documents.category"},
    ),
    "documents.types_classified": (
        "_bind_classification",
        {"worker": CLASSIFY_WORKER_ID, "executor": "documents.classification"},
    ),
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
        stage_checkpoint=stage_checkpoint(adapter),
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
