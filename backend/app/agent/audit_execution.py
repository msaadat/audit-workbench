"""Audit-side composition of the domain-neutral capability scheduler.

Every audit capability is bound to a native scheduler path here: a per-unit
pipeline binding for the model-backed ones and a per-unit deterministic
computation for the rest. This module owns only the audit-shaped glue the
scheduler must not know about — which worker and executor a unit uses, the
declared context scope it resolves, the approval items an auditor sees, the
post-commit bookkeeping, the declared checkpoint handlers, and the audit
completion projection.

The class still inherits ``ActionRunner`` for the shared task, artifact, and
approval helpers that Phase 12 consolidates; it no longer implements any stage
handler.
"""

from __future__ import annotations

import uuid

from .. import doc_tests, rcm_execution
from ..workspace_transactions import mutate, parent_hashes
from ..workspaces import (
    OBSERVATION_DISPOSITIONS,
    Workspace,
    WorkspaceConflict,
    WorkspaceError,
    slugify,
)
from . import capabilities as audit_capabilities
from . import narration, store, workflow
from .action_runner import ActionRunner
from .capabilities.documents import (
    DOCUMENT_SCOPE_CHECKPOINT,
    STAGE_CHECKPOINTS as DOCUMENT_STAGE_CHECKPOINTS,
    analysis_unit_specs,
    resolve_document_scope,
)
from .capabilities.reporting import (
    OBSERVATION_DISPOSITION_CHECKPOINT,
    STAGE_CHECKPOINTS,
)
from .capabilities._shared import scoped_observations, target_rcm_ids
from .doc_tests_execution import bind_document_test_unit
from .documents_execution import (
    DocumentWorkflowExecution,
    build_document_capability_executions,
)
from .context import (
    ContextResolver,
    apm_document_methodology_scope,
    finding_draft_scope,
    planning_context_scope,
    rcm_scope,
    test_generate_scope,
)
from .executors import EXECUTORS
from .executors.fieldwork import roll_up_results, run_data_test
from .executors.planning import (
    AUDITOR_EDIT_PRESERVED,
    ApmExecutorTarget,
    PlanningContextExecutorTarget,
    RcmExecutorTarget,
)
from .executors.tests import TestGenerateExecutorTarget
from .executors.reporting import (
    VERIFICATION_REF,
    FindingExecutorTarget,
    curate_dashboard,
    generate_report_draft,
    generate_working_paper,
    output_issues,
    verify_audit,
)
from .execution_support import refresh_workspace, resolve_context, workflow_scope
from .runtime import (
    BoundUnitPipeline,
    CapabilityExecution,
    DeterministicUnitResult,
    FinishProjection,
    RunRuntime,
    UnitPipeline,
    UnitPipelineRequest,
    UnitSidecarStore,
    WorkflowRunner,
)
from .workers import WORKERS

ELIGIBLE_DISPOSITIONS = {"confirmed_control_exception", "draft_finding_candidate"}


class AuditWorkflowExecution(ActionRunner):
    """Per-unit audit execution bindings and projections for the scheduler."""

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        context_resolver: ContextResolver | None = None,
    ):
        """Create the audit execution adapter with an injectable per-run runtime.

        The optional dependencies preserve the existing three-argument
        construction API while letting a caller supply its own runtime or context
        resolver.
        """
        super().__init__(workspace, run, handle, runtime=runtime)
        self.context_resolver = context_resolver or ContextResolver()
        self.scheduler: WorkflowRunner | None = None

    # ------------------------------------------------------------ ledger
    def _refresh_dynamic_limits(self) -> None:
        test_count = len(self.ws.data_tests) + len(doc_tests.list_tests(self.ws))
        qa_pairs = sum(
            len(item.get("document_ids") or [])
            for summary in doc_tests.list_tests(self.ws)
            if summary.get("kind") == "qa"
            for item in doc_tests.load_test(self.ws, summary["id"]).get("items") or []
        )
        eligible_findings = sum(
            item.get("status") == "disposed"
            and item.get("disposition") in ELIGIBLE_DISPOSITIONS
            for item in self.ws.observations
        )
        calculated = (
            20 + 4 * len(self.ws.rcm) + 4 * test_count
            + 2 * qa_pairs + 2 * eligible_findings
        )
        document_scope = dict(
            (self.run.get("workflow") or {}).get("scope") or {}
        )
        resolved_documents = resolve_document_scope(
            self.ws, document_scope
        )
        document_specs = [
            spec
            for document_id in resolved_documents.document_ids
            for spec in analysis_unit_specs(
                self.ws, document_id, document_scope
            )
        ]
        text_units = sum(
            spec["kind"] == "document_chunk_analysis"
            for spec in document_specs
        )
        visual_units = sum(
            spec["kind"] == "document_visual_page_analysis"
            and not spec.get("unsupported_reason")
            for spec in document_specs
        )
        document_turns = len(document_specs) + max(
            1, len(resolved_documents.document_ids)
        )
        calculated += document_turns
        prompt_allowance = (
            calculated * 10_000
            + text_units * 2_000
            + visual_units * 10_480
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

    def _curated_document_ids(self) -> list[str] | None:
        """The auditor's explicit planning-document curation, if any.

        Curation lives on the run, not in a derived snapshot: when the auditor
        named documents, every model-backed planning capability is restricted to
        exactly those. Otherwise each capability's declared bounded selector
        decides which documents are relevant.
        """
        curated = [
            str(value).strip()
            for value in (self.context.get("document_ids") or [])
            if str(value).strip()
        ]
        return curated or None

    def _bind_planning_context(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind planning-context synthesis to the shared ``UnitPipeline``.

        Synthesis reads only the declared context — the current planning context
        and bounded material from the selected documents — and the registered
        ``planning.context`` executor merges the accepted fields under the
        planning-context parent guard, preserving auditor-entered fields.

        When no document supplies usable material there is nothing to synthesize
        from, so the unit settles without a model call and the existing context
        stands. Producing durable document analyses is the documents subsystem's
        own capability, not a side effect of planning.
        """
        self.ws = subject
        document_ids = self._curated_document_ids()
        scope = planning_context_scope(self.ws, document_ids=document_ids)
        supplied = scope.candidates.get("planning_documents") or ()
        task = self.add_task(
            "context",
            "planning:context",
            "Assemble planning context",
            "Reviewing workspace data and available documents…",
        )
        if not supplied:
            self.warn(
                "No document material is available to synthesize planning context; "
                "the current context stands."
            )
            self.task_status(task, "completed")
            return DeterministicUnitResult("succeeded", ("planning:context",))
        expected_context = parent_hashes(self.ws, ["planning:context"])
        target = PlanningContextExecutorTarget(self.ws, self.run["id"])
        self.task_detail(
            task,
            f"Synthesizing planning context from {len(supplied)} "
            f"document{'s' if len(supplied) != 1 else ''}…",
        )

        def context_provider():
            return resolve_context(self, self.context_resolver, capability, unit, scope)

        def approval_provider(proposal):
            accepted = self.request_approval(
                "context",
                task,
                [
                    self.proposal_item(
                        "Planning context from imported documents",
                        "Grounded facts extracted from the selected engagement "
                        "material.",
                        {"context": dict(proposal.get("context") or {})},
                        {"document_ids": list(document_ids or [])},
                    )
                ],
            )
            return {"context": dict(accepted[0]["spec"]["context"])} if accepted else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            if outcome.receipt is not None and dict(outcome.receipt.output).get(
                "recovered_from_labelled_facts"
            ):
                self.warn(
                    "Planning-context synthesis returned no usable fields; "
                    "recovered labelled facts from the supplied documents."
                )
            self.record_artifact(
                "planning", "context", "planning:context", "updated", task
            )
            self.task_status(task, "completed")

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="planning.context",
                executor_id="planning.context",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": ["planning:context"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_context,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "context" if self.run["mode"] == "permission" else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            # Deliberately not a post-commit postcondition: this capability's
            # readiness requires a stated objective and scope, and documents alone
            # may not supply both. A committed synthesis is a real outcome even
            # when the auditor still has to state the objective, so the commit is
            # not failed for it.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _bind_apm(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind the APM capability to the shared ``UnitPipeline``.

        This is the native pipeline binding for ``planning.apm_ready``: the
        domain-neutral :class:`WorkflowRunner` owns manifest/proposal/receipt
        persistence, proposal reuse, approval, and readiness reevaluation. The
        binding supplies only the APM-specific context, executor target, and two
        domain callbacks — post-commit bookkeeping and the auditor-edit-preserved
        conflict translation — that the scheduler invokes.
        """
        self.ws = subject
        expected_context = parent_hashes(self.ws, ["planning:context"])
        target = ApmExecutorTarget(
            self.ws,
            self.run["id"],
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task("apm", "workflow:apm", "Audit planning memorandum")

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                apm_document_methodology_scope(
                    self.ws,
                    document_ids=self._curated_document_ids(),
                ),
            )

        def approval_provider(proposal):
            proposals = [
                self.proposal_item(
                    "Audit planning memorandum",
                    "Drafted from the current planning basis.",
                    dict(proposal),
                )
            ]
            accepted = self.request_approval("apm", task, proposals)
            return dict(accepted[0]["spec"]) if accepted else None

        def on_committed(_stage, _unit, _outcome) -> None:
            self.ws = target.workspace
            self.run["planning_changes"]["apm_updated"] += 1
            self.record_artifact("planning", "apm", "planning:apm", "updated", task)

        def conflict_handler(_stage, _unit, error) -> tuple[str, str] | None:
            if str(error) != AUDITOR_EDIT_PRESERVED:
                return None
            self.run.setdefault("planning_revisions", []).append(
                {
                    "kind": "apm",
                    "status": "proposed",
                    "sidecar": dict(error.proposal_reference or {}),
                }
            )
            self.run["planning_changes"]["apm_proposed"] += 1
            return ("awaiting_confirmation", "Auditor-owned APM was preserved.")

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="planning.apm",
                executor_id="planning.apm",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": ["planning:apm"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_context,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=("apm" if self.run["mode"] == "permission" else None),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            readiness_provider=lambda: capability.readiness(target.workspace, {}),
            on_committed=on_committed,
            conflict_handler=conflict_handler,
        )

    def _bind_rcm(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind the RCM capability to the shared ``UnitPipeline``.

        The domain-neutral scheduler owns manifest/proposal/receipt persistence,
        proposal reuse, approval, and readiness reevaluation. This binding
        supplies only the RCM-specific declared context, the row-commit executor
        target, per-row approval items, and the post-commit bookkeeping callback
        that translates receipt row actions into planning-change accounting.
        """
        self.ws = subject
        expected_apm = parent_hashes(self.ws, ["planning:apm"])
        target = RcmExecutorTarget(
            self.ws,
            self.run["id"],
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task("rcm", "workflow:rcm", "Risk and control matrix")

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                rcm_scope(
                    self.ws,
                    document_ids=self._curated_document_ids(),
                ),
            )

        def approval_provider(proposal):
            proposed = []
            for raw in proposal.get("rows") or []:
                spec = dict(raw)
                spec["semantic_id"] = str(
                    spec.get("semantic_id")
                    or f"rcm:{slugify(str(spec.get('process') or ''))}:{slugify(str(spec.get('risk') or ''))}"
                )
                proposed.append(
                    self.proposal_item(
                        str(spec.get("risk")), "Risk/control matrix revision.", spec
                    )
                )
            accepted = self.request_approval("rcm", task, proposed)
            rows = [dict(item["spec"]) for item in accepted]
            return {"rows": rows} if rows else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            for row in (outcome.receipt.output.get("rows") or []) if outcome.receipt else []:
                action = str(row.get("action"))
                if action == "preserved":
                    self.warn(f"Preserved auditor-owned RCM row '{row['id']}'.")
                    self.run["planning_changes"]["rcm_preserved"] += 1
                    continue
                self.run["planning_changes"][f"rcm_{action}"] += 1
                self.record_artifact(
                    "rcm", str(row["id"]), str(row["semantic_id"]), action, task
                )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="planning.rcm",
                executor_id="planning.rcm",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": ["planning:apm"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_apm,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=("rcm" if self.run["mode"] == "permission" else None),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            readiness_provider=lambda: capability.readiness(target.workspace, {}),
            on_committed=on_committed,
        )

    def _bind_test_generate(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind one RCM row's test-generation unit to the shared ``UnitPipeline``.

        The domain-neutral scheduler owns manifest/proposal/receipt persistence,
        proposal reuse, approval, and readiness reevaluation. This binding
        supplies only the row-scoped declared context, the generation commit
        target, per-test approval items, and the post-commit bookkeeping that
        translates receipt actions into planning-change accounting.

        Replaces ``_bind_test_draft``/``_bind_test_spec``: the RCM row is the
        sole guarded parent, since one model turn now decides every test's
        source and writes its complete executable definition in one commit
        (docs/test-capability-merge-plan.md, section 6).
        """
        self.ws = subject
        rcm_id = unit["parent_refs"][0].split(":", 1)[1]
        expected_row = parent_hashes(self.ws, [f"rcm:{rcm_id}"])
        target = TestGenerateExecutorTarget(
            self.ws,
            self.run["id"],
            rcm_id,
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task(
            "test_specs", "workflow:test_specs", "Executable test specifications"
        )

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                test_generate_scope(
                    self.ws,
                    rcm_id,
                    document_ids=self._curated_document_ids(),
                ),
            )

        def approval_provider(proposal):
            proposed = [
                self.proposal_item(
                    str(spec.get("title") or spec.get("objective")),
                    f"{spec.get('source')} test for {rcm_id}.",
                    dict(spec),
                )
                for spec in proposal.get("tests") or []
            ]
            accepted = self.request_approval("test_specs", task, proposed)
            tests = [dict(item["spec"]) for item in accepted]
            return {"tests": tests} if tests else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            for item in (
                (outcome.receipt.output.get("tests") or []) if outcome.receipt else []
            ):
                action = str(item.get("action"))
                self.run["planning_changes"][f"test_{action}"] += 1
                self.record_artifact(
                    str(item["kind"]), str(item["id"]), "", action, None
                )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="tests.generate",
                executor_id="tests.generate",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": [f"rcm:{rcm_id}"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_row,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "test_specs" if self.run["mode"] == "permission" else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            # Generation fans out one unit per RCM row, so this capability's
            # readiness is only satisfied once every row's unit has committed.
            # Post-commit readiness is therefore evaluated by the stage fold,
            # not per unit.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _bind_execution(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one fieldwork-execution unit to the execution it actually needs.

        This capability fans out into four unit kinds and only one of them —
        document Q&A — asks the model anything. The binding therefore returns a
        ``BoundUnitPipeline`` for that kind and a ``DeterministicUnitResult`` for
        the local ones, which is how a capability with mixed units keeps exactly
        one execution binding.

        Only the datatest branch is audit-specific. Every Document Test unit kind
        is bound by :func:`doc_tests_execution.bind_document_test_unit`, the same
        function the standalone ``doc_tests_workflow_v1`` composition uses, so a
        worklist behaves identically whichever graph scheduled it.
        """
        self.ws = subject
        # Existing execution services combine compute and mutation and check the
        # revision they were handed, so each unit runs against a freshly loaded
        # workspace rather than the subject the stage started with.
        refresh_workspace(self)
        artifact_ref = unit["parent_refs"][-1]
        kind, item_id = artifact_ref.split(":", 1)
        if kind != "datatest":
            task = self.add_task(
                "execution", "workflow:execution", "Fieldwork execution"
            )
            return bind_document_test_unit(self, capability, unit, task=task)
        try:
            outcome = run_data_test(self.ws, item_id)
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        if outcome.executed:
            self.emit(
                "workspace_changed",
                {"kind": kind, "id": item_id, "action": "executed"},
            )
        return DeterministicUnitResult(
            outcome.status, (outcome.artifact_ref,), outcome.error
        )

    def _bind_rollup(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``results.rolled_up``.

        Roll-up recomputes each RCM row's derived result and its observations from
        the current execution artifacts and persists only material changes.
        Observation identities are keyed on ``execution_ref``, so a repeated
        roll-up reuses the same observation rows rather than creating duplicates.
        On success the binder emits the ``workspace_changed`` roll-up signal the
        generic deterministic path does not. No model call or approval is involved;
        the auditor's observation disposition runs as a declared checkpoint before
        finding creation (see ``STAGE_CHECKPOINTS``), not here.
        """
        self.ws = subject
        try:
            refs = roll_up_results(
                self.ws,
                rcm_ids=set(target_rcm_ids(self.ws, workflow_scope(self.run))),
            )
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        self.emit(
            "workspace_changed", {"kind": "rcm", "id": "rollup", "action": "updated"}
        )
        return DeterministicUnitResult("succeeded", tuple(refs))

    def _observation_checkpoint(self) -> None:
        open_items = [
            item
            for item in scoped_observations(self.ws, workflow_scope(self.run))
            if item.get("status") != "disposed"
        ]
        if not open_items:
            return
        interaction = next((item for item in self.run.get("interactions") or [] if item.get("type") == "observation_disposition" and item.get("status") == "pending"), None)
        if interaction is None:
            interaction = {
                "id": f"int_{uuid.uuid4().hex[:12]}", "action_id": "workflow:rollup",
                "type": "observation_disposition",
                "prompt": "Review and disposition the observations before finding drafts are prepared.",
                "options": [],
                "payload": {
                    "observations": [
                        {key: item.get(key) for key in ("id", "rcm_id", "test_id", "execution_ref", "summary", "exception_count", "suggested_disposition")}
                        for item in open_items
                    ],
                    "allowed_values": sorted(OBSERVATION_DISPOSITIONS),
                },
                "policy_reason": "Observation disposition is authoritative auditor judgment.",
                "status": "pending", "response": None, "actor": None,
                "created_at": store.utcnow(), "resolved_at": None,
            }
            self.run.setdefault("interactions", []).append(interaction)
            self.run["workflow"]["pending_checkpoint"] = interaction["id"]
            self.save()
            self.emit("checkpoint_request", {"interaction": interaction})
        response = self._wait_interaction_response(interaction)
        decisions = response.get("decisions") or []
        by_id = {item["id"]: item for item in open_items}
        if not isinstance(decisions, list):
            raise WorkspaceError("Observation decisions must be an array.")

        def commit(fresh: Workspace):
            changed = []
            for decision in decisions:
                observation_id = str(decision.get("observation_id") or decision.get("id") or "")
                if observation_id not in by_id:
                    continue
                current = next((item for item in fresh.observations if item.get("id") == observation_id), None)
                if current is None or current.get("execution_ref") != by_id[observation_id].get("execution_ref"):
                    raise WorkspaceConflict(fresh.revision, fresh.revision)
                changed.append(rcm_execution.disposition(fresh, observation_id, str(decision.get("disposition") or ""), str(decision.get("auditor_note") or "")))
            return changed

        result = mutate(self.ws, commit)
        self.ws = result.workspace
        rcm_execution.rollup(
            self.ws,
            rcm_ids=set(target_rcm_ids(self.ws, workflow_scope(self.run))),
        )
        self.run["workflow"]["pending_checkpoint"] = None
        self._resolve_interaction_record(interaction, response)
        self.emit("checkpoint_resolved", {"interaction_id": interaction["id"], "count": len(result.value)})

    def _bind_finding(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind one eligible observation's finding-draft unit to the pipeline.

        The binding supplies only the observation-scoped declared context, the
        finding commit target, the approval item, and the post-commit workspace
        signal. Evidence linkage and support validation live in the executor,
        where they run against the committed workspace state.
        """
        self.ws = subject
        observation_id = unit["parent_refs"][0].split(":", 1)[1]
        expected_observation = parent_hashes(self.ws, [f"observation:{observation_id}"])
        target = FindingExecutorTarget(self.ws, self.run["id"], observation_id)
        task = self.add_task("findings", "workflow:findings", "Eligible finding drafts")

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                finding_draft_scope(self.ws, observation_id),
            )

        def approval_provider(proposal):
            accepted = self.request_approval(
                "finding_drafts",
                task,
                [
                    self.proposal_item(
                        unit["title"],
                        "Draft from an auditor-dispositioned observation.",
                        dict(proposal.get("finding") or {}),
                    )
                ],
            )
            return {"finding": dict(accepted[0]["spec"])} if accepted else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            if outcome.receipt is None:
                return
            self.emit(
                "workspace_changed",
                {
                    "kind": "finding",
                    "id": str(outcome.receipt.output["id"]),
                    "action": "created",
                },
            )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="reporting.finding",
                executor_id="reporting.finding",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": list(unit.get("parent_refs") or []),
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_observation,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "finding_drafts" if self.run["mode"] == "permission" else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            # Findings fan out one unit per eligible observation, so this
            # capability's readiness only holds once every unit has committed.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _bind_working_papers(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``working_papers.generated``.

        Each unit renders and commits one RCM working paper. Generation is a pure
        projection of current RCM/execution state, and the commit is parent-hash
        guarded, so a changed RCM parent surfaces as a conflict rather than an
        overwrite. No model call or approval is involved.
        """
        self.ws = subject
        rcm_id = unit["parent_refs"][0].split(":", 1)[1]
        try:
            ref = generate_working_paper(self.ws, rcm_id)
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        return DeterministicUnitResult("succeeded", (ref,))

    def _bind_dashboard(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``dashboard.curated``.

        Curation scores the current RCM-linked results and pins the strongest
        tiles under a commit guarded on the RCM's material hash, so a changed RCM
        basis surfaces as a conflict rather than pinning against a stale matrix.
        On success the binder emits the ``workspace_changed`` dashboard signal the
        generic deterministic path does not. No model call or approval is involved.
        """
        self.ws = subject
        try:
            refs = curate_dashboard(self.ws, run_id=self.run["id"])
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        self.emit(
            "workspace_changed",
            {"kind": "dashboard", "id": "curation", "action": "updated"},
        )
        return DeterministicUnitResult("succeeded", tuple(refs))

    def _bind_report(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``report.working_draft``.

        The workflow assembles the draft from current planning, results, and
        findings without a model call, so this capability has no worker. An
        auditor-edited draft is preserved and its regenerated candidate is left
        for reconciliation, which is what ``awaiting_confirmation`` records. On
        success the binder emits the ``workspace_changed`` report signal the
        generic deterministic path does not.
        """
        self.ws = subject
        ref, requires_reconcile = generate_report_draft(
            self.ws,
            run_id=self.run["id"],
            workflow=self.run.get("workflow"),
        )
        self.emit(
            "workspace_changed", {"kind": "report", "id": "draft", "action": "updated"}
        )
        if requires_reconcile:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (ref,),
                "Auditor-edited report preserved; reconcile the generated candidate.",
            )
        return DeterministicUnitResult("succeeded", (ref,))

    def _bind_verify(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``audit.verified``.

        Verification is read-only: it computes the completion/quality/output
        outcome, records it on the run for the completion projection, and derives
        the terminal unit status from it. No workspace mutation, model call, or
        approval is involved.
        """
        self.ws = subject
        outcome = verify_audit(self.ws)
        self.run["audit_outcome"] = outcome
        if outcome["audit_complete"]:
            return DeterministicUnitResult("succeeded", (VERIFICATION_REF,))
        error = (
            f"Completion status: {outcome['completion_status']}; "
            f"report errors: {outcome['report_quality_errors']}; "
            f"output gates: {len(output_issues(outcome))}."
        )
        return DeterministicUnitResult("blocked", (VERIFICATION_REF,), error)

    # --------------------------------------------------------- interactions
    def _wait_interaction_response(self, interaction: dict) -> dict:
        return self.runtime.wait_for_interaction(interaction)

    def _resolve_interaction_record(self, interaction: dict, response: dict) -> None:
        self.runtime.resolve_interaction(interaction, response)

    # ------------------------------------------------------------ finish
    def _finish_projection(
        self,
        subject: Workspace,
        _workflow_state: dict,
        stages: tuple[dict, ...],
    ) -> FinishProjection:
        """Close the run on real audit outcomes, not on unit bookkeeping.

        A run that executed every unit cleanly can still be incomplete — open
        evidence requests, undispositioned observations, or an unreconciled
        report. Those become `next_outcomes`, the exact input "Continue audit"
        replays, and they are why `completed_with_open_items` is a distinct
        terminal status from `completed`.
        """
        self.ws = subject
        completion = rcm_execution.completion(subject)
        failed = sum(unit.get("status") in {"failed", "conflict"} for stage in stages for unit in stage.get("units") or [])
        open_units = sum(unit.get("status") in {"blocked", "awaiting_input", "awaiting_confirmation"} for stage in stages for unit in stage.get("units") or [])
        open_observations = [
            item
            for item in scoped_observations(subject, workflow_scope(self.run))
            if item.get("status") != "disposed"
        ]
        open_evidence = [item for item in subject.evidence_requests if item.get("status") == "open"]
        next_outcomes = []
        execution_open = bool(open_evidence) or any(
            stage.get("capability") == "fieldwork.executed"
            and any(
                unit.get("status") in {"blocked", "awaiting_input", "awaiting_confirmation"}
                for unit in stage.get("units") or []
            )
            for stage in stages
        )
        if execution_open:
            next_outcomes.extend(["fieldwork.executed", "results.rolled_up"])
        if open_observations:
            next_outcomes.append("findings.drafted")
        reconciliation_open = any(
            stage.get("capability") == "report.working_draft"
            and any(unit.get("status") == "awaiting_confirmation" for unit in stage.get("units") or [])
            for stage in stages
        )
        if execution_open or open_observations or reconciliation_open:
            next_outcomes.extend(["report.working_draft", "audit.verified"])
        self.run["workflow"]["workspace_revision"] = subject.revision
        requires_full_completion = "audit.verified" in self.run["workflow"].get("requested_outcomes", [])
        if failed:
            terminal = "failed"
            failed_errors = [
                str(unit.get("error"))
                for stage in stages
                for unit in stage.get("units") or []
                if unit.get("status") in {"failed", "conflict"} and unit.get("error")
            ]
            self.run["error"] = failed_errors[0] if failed_errors else "One or more workflow units failed."
        elif not requires_full_completion:
            terminal = "completed" if not open_units else "completed_with_open_items"
        else:
            terminal = "completed" if completion["status"] == "completed" and not open_units else "completed_with_open_items"
        summary = narration.summary_markdown(
            "Audit workflow",
            [
                ("Requested", [
                    narration.humanize(item)
                    for item in self.run["workflow"]["requested_outcomes"]
                ]),
                ("Completion", None if completion["status"] == "completed" else completion["status"]),
                ("Failed or conflicting units", failed),
                ("Open workflow units", open_units),
                ("Open observations", len(open_observations)),
                ("Open evidence requests", len(open_evidence)),
            ],
        )
        return FinishProjection(
            next_outcomes=tuple(dict.fromkeys(next_outcomes)),
            summary_markdown=summary,
            terminal_status=terminal,
        )


_PARTIAL_DEPENDENCIES = {
    # A document the run could not extract or analyze must never stop the audit:
    # planning consumes the document material that exists, which is exactly what
    # the scoped document dependency is for. The document chain's own edges are
    # partial for the same reason — one unanalyzable document does not withhold
    # the analyses of the others.
    "documents.analysis_chunks_ready": {"documents.text_ready"},
    "documents.analysis_generated": {"documents.analysis_chunks_ready"},
    "planning.context_ready": {"documents.analysis_generated"},
    "fieldwork.executed": {"tests.specified"},
    "results.rolled_up": {"fieldwork.executed"},
    "report.working_draft": {"findings.drafted"},
    "audit.verified": {
        "working_papers.generated",
        "dashboard.curated",
        "report.working_draft",
    },
}


def build_audit_workflow_runner(
    workspace: Workspace,
    run: dict,
    handle,
    *,
    runtime: RunRuntime | None = None,
    context_resolver: ContextResolver | None = None,
) -> WorkflowRunner:
    """Compose the domain-neutral scheduler with temporary audit adapters."""

    adapter = AuditWorkflowExecution(
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
        sidecars=UnitSidecarStore(workspace, run["id"]),
    )
    # Every audit capability is bound to a native scheduler path. A
    # pipeline-backed capability supplies a per-unit binding the domain-neutral
    # scheduler drives through ``UnitPipeline``; a deterministic one supplies a
    # per-unit computation with no model call. There is no transitional batch
    # handler left.
    _PIPELINE_BINDERS = {
        "planning.context_ready": (
            adapter._bind_planning_context,
            {"worker": "planning.context", "executor": "planning.context"},
        ),
        "planning.apm_ready": (
            adapter._bind_apm,
            {"worker": "planning.apm", "executor": "planning.apm"},
        ),
        "planning.rcm_ready": (
            adapter._bind_rcm,
            {"worker": "planning.rcm", "executor": "planning.rcm"},
        ),
        "tests.specified": (
            adapter._bind_test_generate,
            {"worker": "tests.generate", "executor": "tests.generate"},
        ),
        "fieldwork.executed": (
            adapter._bind_execution,
            {
                "worker": "fieldwork.document_qa",
                "executor": "fieldwork.document_qa",
                "deterministic": (
                    "fieldwork.data_test_run|fieldwork.document_test_run|"
                    "fieldwork.document_test_review"
                ),
            },
        ),
        "findings.drafted": (
            adapter._bind_finding,
            {"worker": "reporting.finding", "executor": "reporting.finding"},
        ),
    }
    # Capabilities whose every unit is deterministic, bound through the
    # scheduler's deterministic execution path.
    _DETERMINISTIC_BINDERS = {
        "results.rolled_up": (
            adapter._bind_rollup,
            {"deterministic": "fieldwork.rollup"},
        ),
        "working_papers.generated": (
            adapter._bind_working_papers,
            {"deterministic": "reporting.working_paper"},
        ),
        "dashboard.curated": (
            adapter._bind_dashboard,
            {"deterministic": "reporting.dashboard"},
        ),
        "report.working_draft": (
            adapter._bind_report,
            {"deterministic": "reporting.report_draft"},
        ),
        "audit.verified": (
            adapter._bind_verify,
            {"deterministic": "reporting.verify"},
        ),
    }
    # The three document generation capabilities ``planning.context_ready``
    # depends on are declared and implemented once, in the document group. The
    # audit composition binds them through the same execution adapter rather than
    # through a second implementation, sharing this run's runtime and ledger lock
    # so both write one durable record under one lock.
    document_adapter = DocumentWorkflowExecution(
        workspace,
        run,
        handle,
        runtime=adapter.runtime,
        state_lock=adapter._state_lock,
        context_resolver=adapter.context_resolver,
    )
    document_adapter.unit_pipeline = unit_pipeline
    executions = build_document_capability_executions(
        document_adapter, audit_capabilities.AUDIT_DOCUMENT_GROUP.capabilities()
    )
    for capability in audit_capabilities.REGISTRY.all():
        if capability.id in audit_capabilities.AUDIT_DOCUMENT_GROUP.CAPABILITY_IDS:
            continue
        pipeline_binding = _PIPELINE_BINDERS.get(capability.id)
        if pipeline_binding is not None:
            binder, identity = pipeline_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=workflow.canonical_sha256(
                        {"capability": capability.id, **identity}
                    ),
                    pipeline_binder=binder,
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
                deterministic_executor=binder,
            )
        )

    def dependency_policy(
        capability_id: str,
        dependency_id: str,
        _dependency_status: str,
    ) -> bool:
        return dependency_id in _PARTIAL_DEPENDENCIES.get(capability_id, set())

    # Declared auditor-judgment checkpoints resolved to their blocking handlers.
    # The capability -> checkpoint-name declaration lives with the audit
    # capability group (``STAGE_CHECKPOINTS``); the composition owns the concrete
    # handler here because it needs the live run/runtime (interaction wait,
    # persistence, and events). The checkpoint gates its capability's units and
    # runs only in permission mode.
    checkpoint_handlers = {
        OBSERVATION_DISPOSITION_CHECKPOINT: adapter._observation_checkpoint,
        DOCUMENT_SCOPE_CHECKPOINT: document_adapter._scope_checkpoint,
    }
    stage_checkpoints = {**DOCUMENT_STAGE_CHECKPOINTS, **STAGE_CHECKPOINTS}

    def before_stage(
        subject: Workspace,
        capability: workflow.Capability,
        _stage: dict,
    ) -> None:
        adapter.ws = subject
        document_adapter.ws = subject
        checkpoint = stage_checkpoints.get(capability.id)
        if checkpoint is not None and run.get("mode") == "permission":
            checkpoint_handlers[checkpoint]()

    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=adapter.runtime,
        registry=audit_capabilities.REGISTRY,
        executions=executions,
        unit_pipeline=unit_pipeline,
        refresh_subject=lambda: refresh_workspace(adapter),
        refresh_limits=lambda _subject: adapter._refresh_dynamic_limits(),
        dependency_policy=dependency_policy,
        before_stage=before_stage,
        finish_evaluator=adapter._finish_projection,
    )
    adapter.scheduler = scheduler
    scheduler.execution_adapter = adapter
    return scheduler
