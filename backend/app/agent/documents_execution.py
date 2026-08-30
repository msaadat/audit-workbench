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
from .capabilities.documents import (
    DOCUMENT_SCOPE_CHECKPOINT,
    MAX_SCOPE_DOCUMENTS,
    STAGE_CHECKPOINTS,
    DocumentScope,
    analysis_profile,
    analysis_unit_specs,
    analyzable,
    chunk_specs,
    has_generated_analysis,
    preparation_model_turns,
    resolve_document_scope,
    visual_page_limit,
)
from .context import (
    ContextResolver,
    document_chunk_scope,
    document_classification_scope,
    document_schema_sample_scope,
    document_structured_chunk_scope,
    document_reduction_scope,
    document_visual_page_scope,
)
from .executors import EXECUTORS
from .executors.documents import (
    DocumentClassificationExecutorTarget,
    DocumentSchemaExecutorTarget,
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
    CLASSIFY_WORKER_ID,
    INDUCE_WORKER_ID,
    RECONCILE_WORKER_ID,
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

    def _schema_samples(
        self, document_type: str, sample_ids: list[str]
    ) -> list[tuple[str, dict]]:
        """Read back what each sample unit proposed, in stable document order.

        Paired with the document each reading came from, rather than returned as
        a bare list. A sample that failed leaves no proposal, so the readings are
        a *subsequence* of the samples — and a schema is a claim about the
        documents it was actually read from. Positional recovery would attribute
        it to a prefix of the samples instead, which is the wrong set the moment
        anything but the last one fails.
        """

        readings: list[tuple[str, dict]] = []
        for document_id in sample_ids:
            unit_id = semantic_unit_id(
                "document_schema_sample", document_type, document_id
            )
            try:
                payload = self.sidecars.load_proposal(unit_id)
            except WorkspaceError:
                payload = None
            proposal = (payload or {}).get("proposal")
            if isinstance(proposal, dict) and proposal.get("fields"):
                readings.append((document_id, dict(proposal)))
        return readings

    def _bind_schema_sample(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one sample document to be read for the fields its type carries."""

        self.ws = subject
        unit_input = dict(unit.get("input_payload") or {})
        document_id = self._parent(unit, "document")
        document_type = str(
            unit_input.get("document_type") or ""
        ) or document_classification.document_type(self.ws, document_id)
        text = str(unit_input.get("text") or "") or document_classification.induction_text(
            self.ws, document_id
        )
        if not text:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                "This document has no extracted text to read a schema from.",
            )
        task = self.add_task(
            "document_schemas", "workflow:document_schemas", "Document schema"
        )
        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id=INDUCE_WORKER_ID,
            executor_id=None,
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
                "document_type": document_type,
                "document_id": document_id,
                "title": str(unit_input.get("title") or ""),
                "text": text,
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": [document_id],
                "task_id": task["id"],
            },
            expected_revision=self.ws.revision,
            expected_parents={},
            capability_definition_hash=workflow.capability_definition_hash(capability),
            # A sample reading is an input to the frozen schema, never a durable
            # record of its own: its persisted proposal is the outcome, the way a
            # chunk analysis works.
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
                document_schema_sample_scope(self.ws, document_id, text),
            )

        return BoundUnitPipeline(
            request=request,
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=None,
        )

    def _bind_schema(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Union one type's sample readings and freeze the schema they agree on.

        The samples are unioned by local code, and a model is called a second
        time *only* if two of them named one field as two different things.
        Agreement is the common case and costs nothing, which is what makes
        reading the samples independently affordable in the first place.
        """

        self.ws = subject
        unit_input = dict(unit.get("input_payload") or {})
        document_type = str(unit_input.get("document_type") or "")
        # ``input_payload`` does not reach a binder — the scheduler stores only
        # kind, title, parent_refs, and input_sha1 — so the freeze unit recovers
        # both its samples and its type from the parents it was expanded against.
        # Those are the sample documents themselves, which makes the recovery
        # exact rather than a re-derivation that could pick a different sample.
        sample_ids = [
            str(ref).split(":", 1)[1]
            for ref in unit.get("parent_refs") or []
            if str(ref).startswith("document:")
        ]
        if not document_type and sample_ids:
            document_type = document_classification.document_type(self.ws, sample_ids[0])
        if not sample_ids:
            sample_ids = document_classification.sample_for_induction(
                self.ws, document_type
            )
        readings = self._schema_samples(document_type, sample_ids)
        if not readings:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (),
                f"No sample of '{document_type}' could be read for its fields.",
            )
        contributing = [document_id for document_id, _ in readings]
        fields, conflicts = document_schemas.union_fields(
            [proposal.get("fields") or [] for _, proposal in readings]
        )
        task = self.add_task(
            "document_schemas", "workflow:document_schemas", "Document schema"
        )
        target = DocumentSchemaExecutorTarget(
            self.ws,
            self.run["id"],
            document_type,
            sample_document_ids=tuple(contributing),
            reconciled=bool(conflicts),
        )
        # The documents the schema was read from. A schema is a claim about
        # them, so replacing one under this commit is a real conflict rather
        # than a race to ignore.
        expected = parent_hashes(
            self.ws, [document_ref(document_id) for document_id in contributing]
        )
        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id=RECONCILE_WORKER_ID,
            executor_id="documents.schema",
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
                "document_type": document_type,
                "sample_document_ids": contributing,
                "conflicts": conflicts,
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": contributing,
                "task_id": task["id"],
            },
            expected_revision=self.ws.revision,
            expected_parents=expected,
            capability_definition_hash=workflow.capability_definition_hash(capability),
            approval_kind=None,
            proposal_reference=unit.get("proposal_sidecar"),
            receipt_reference=unit.get("receipt_sidecar"),
        )

        if not conflicts:
            # The samples agree. Freezing needs no model, so none is billed — the
            # reconciliation turn exists for disagreement, not as a step. The
            # commit still goes through the shared pipeline, so it keeps the same
            # proposal-before-mutation, reconciliation, and receipt guarantees a
            # model-backed unit gets.
            return self._commit_schema(unit, request, target, {"fields": fields})

        def accept(proposal):
            """Apply the chosen readings, then re-union to the settled fields.

            Re-unioning rather than patching the merged list keeps one code path
            deciding what a schema is: the resolution changes what a sample said,
            and the union then follows from it exactly as it would have if the
            samples had agreed.
            """

            chosen = {
                (str(item.get("name")), str(item.get("attribute"))): str(item.get("value"))
                for item in (dict(proposal).get("resolutions") or [])
            }
            settled = [
                [
                    {
                        **dict(field),
                        **{
                            attribute: chosen[(str(field.get("name")), attribute)]
                            for attribute in document_schemas.CONFLICTING_ATTRIBUTES
                            if (str(field.get("name")), attribute) in chosen
                        },
                    }
                    for field in proposal_fields
                ]
                for proposal_fields in (
                    reading.get("fields") or [] for _, reading in readings
                )
            ]
            merged, remaining = document_schemas.union_fields(settled)
            if remaining:
                raise WorkspaceError(
                    f"Reconciliation left '{remaining[0]['name']}' unsettled."
                )
            return {**dict(proposal), "fields": merged}

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                document_schema_sample_scope(
                    self.ws,
                    contributing[0],
                    document_classification.induction_text(self.ws, contributing[0]),
                ),
            )

        return BoundUnitPipeline(
            request=request,
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=accept,
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

    def _commit_schema(
        self,
        unit: dict,
        request: UnitPipelineRequest,
        target: DocumentSchemaExecutorTarget,
        proposal: dict,
    ) -> DeterministicUnitResult:
        """Commit an agreed schema through the shared pipeline, with no model."""

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
            origin="agreed_schema_union",
            on_proposal_persisted=record("proposal_sidecar"),
            on_receipt_persisted=record("receipt_sidecar"),
        )
        return DeterministicUnitResult(
            "succeeded", (f"document_schema:{target.document_type}",)
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
    def _warn_unstructured_vouchers(self) -> None:
        """Name the transaction evidence about to be read without its schema.

        ``analysis_profile`` reads a voucher under ``structured`` only where its
        type has an induced schema, and under ``standard`` otherwise. The second
        is a readable narrative analysis and explicitly not cycle evidence, so a
        voucher that lands there has dropped out of the strongest evidence path
        the engagement has — and nothing about the analysis it does get says so.

        That downgrade was unreachable while a failed sample blocked every later
        stage; completing ``_PARTIAL_DEPENDENCIES`` is what lets these documents
        through, so this is the other half of that change rather than a separate
        improvement. Reported per document type, because the repair is per type:
        induce the schema, and the vouchers of that type are read under it.

        Warned before chunking rather than after analysis, so the run says what
        is about to happen while an auditor can still stop it.
        """

        state = self.run.setdefault("document_analysis", {})
        if state.get("unstructured_vouchers_reported"):
            return
        document_scope = self.scope()
        downgraded: dict[str, list[str]] = {}
        for document_id in document_scope.document_ids:
            document = next(
                (
                    item
                    for item in self.ws.documents
                    if str(item.get("id")) == document_id
                ),
                None,
            )
            if document is None:
                continue
            if str(document.get("category") or "") not in intake.VOUCHER_DOCUMENT_CATEGORIES:
                continue
            if analysis_profile(self.ws, document_id) == "structured":
                continue
            document_type = (
                document_classification.document_type(self.ws, document_id) or "unclassified"
            )
            downgraded.setdefault(document_type, []).append(self._title(document_id))
        if not downgraded:
            return
        state["unstructured_vouchers_reported"] = True
        for document_type, titles in sorted(downgraded.items()):
            self.warn(
                f"{counted(len(titles), 'document')} held as transaction evidence "
                f"({', '.join(sorted(titles)[:3])}"
                f"{', …' if len(titles) > 3 else ''}) will be analysed as narrative "
                f"because document type '{document_type}' has no induced schema. "
                "A narrative analysis is not cycle evidence, so these documents "
                "cannot be tie-matched until the schema is induced."
            )
        self.save()

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
# ``schemas_sampled`` stage, blocked induction, blocked chunking, and blocked
# every analysis in the run — the whole engagement starved by one document.
#
# Completing it is only half the repair. Unblocking a stage that used to stop
# means a voucher whose type has no schema now *reaches* analysis, and
# ``analysis_profile`` reads it under ``standard`` rather than ``structured``:
# a narrative analysis, which is not cycle evidence. That is a quieter failure
# than the starvation it replaces, so ``_warn_unstructured_vouchers`` reports it
# by name. Removing a blockage without reporting what now flows past it would
# have traded a loud stop for a silent downgrade.
_PARTIAL_DEPENDENCIES = {
    "documents.types_classified": {"documents.text_ready"},
    "documents.schemas_sampled": {"documents.types_classified"},
    "documents.schemas_induced": {"documents.schemas_sampled"},
    "documents.analysis_chunks_ready": {
        "documents.text_ready",
        "documents.schemas_induced",
    },
    "documents.analysis_generated": {"documents.analysis_chunks_ready"},
    "documents.analysis_reviewed": {"documents.analysis_generated"},
}

_PIPELINE_BINDERS = {
    "documents.schemas_sampled": (
        "_bind_schema_sample",
        {"worker": INDUCE_WORKER_ID, "executor": None},
    ),
    "documents.schemas_induced": (
        "_bind_schema",
        {"worker": RECONCILE_WORKER_ID, "executor": "documents.schema"},
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
        if capability.id == "documents.analysis_chunks_ready":
            adapter._warn_unstructured_vouchers()
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
