"""Temporary audit capability execution adapters for the runtime scheduler."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid

from .. import (
    data_tests,
    doc_tests,
    documents,
    rcm_execution,
)
from ..workspace_transactions import mutate, parent_hashes
from ..workspaces import (
    OBSERVATION_DISPOSITIONS,
    Workspace,
    WorkspaceConflict,
    WorkspaceError,
    load_workspace,
    slugify,
)
from . import capabilities as audit_capabilities
from . import context_bundles, store, workflow
from .base import Cancelled, LimitExceeded
from .action_runner import ActionRunner
from .capabilities.reporting import (
    OBSERVATION_DISPOSITION_CHECKPOINT,
    STAGE_CHECKPOINTS,
)
from .context import (
    ContextResolver,
    apm_document_methodology_scope,
    data_test_spec_scope,
    document_test_spec_scope,
    finding_draft_scope,
    planned_test_scope,
    rcm_scope,
)
from .executors import EXECUTORS
from .executors.fieldwork import (
    DataTestExecutorTarget,
    DocumentTestExecutorTarget,
    roll_up_results,
)
from .executors.planning import (
    AUDITOR_EDIT_PRESERVED,
    ApmExecutorTarget,
    PlannedTestExecutorTarget,
    RcmExecutorTarget,
)
from .executors.reporting import (
    VERIFICATION_REF,
    FindingExecutorTarget,
    curate_dashboard,
    generate_report_draft,
    generate_working_paper,
    output_issues,
    verify_audit,
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
)
from .workers import WORKERS

ELIGIBLE_DISPOSITIONS = {"confirmed_control_exception", "draft_finding_candidate"}


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _capability_definition_hash(capability: workflow.Capability) -> str:
    return _sha256_json(
        {
            "id": capability.id,
            "stage_id": capability.stage_id,
            "title": capability.title,
            "worker_kind": capability.worker_kind,
            "depends_on": list(capability.depends_on),
            "context": capability.context,
            "barrier": capability.barrier,
            "commit_policy": capability.commit_policy,
            "approval_policy": capability.approval_policy,
            "invalidate_on": list(capability.invalidate_on),
        }
    )



class AuditWorkflowExecution(ActionRunner):
    """Audit execution behavior awaiting grouped Phase 7 registrations."""

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        context_resolver: ContextResolver | None = None,
    ):
        """Create a workflow scheduler with an injectable per-run runtime.

        The optional dependency preserves the existing three-argument
        construction API while the current audit-specific stage handlers and
        temporary ``ActionRunner`` inheritance remain unchanged.
        """
        super().__init__(workspace, run, handle, runtime=runtime)
        self.context_resolver = context_resolver or ContextResolver()
        self.scheduler: WorkflowRunner | None = None

    # ------------------------------------------------------------ ledger
    def _refresh_workspace(self) -> Workspace:
        previous = int(self.run["workflow"].get("workspace_revision") or 0)
        self.ws = load_workspace(self.ws.id)
        self.run["workflow"]["workspace_revision"] = self.ws.revision
        if self.ws.revision != previous:
            self.emit(
                "workspace_revision",
                {"previous_revision": previous, "workspace_revision": self.ws.revision},
            )
        return self.ws

    def _refresh_dynamic_limits(self) -> None:
        planned_count = sum(
            len(row.get("planned_tests") or []) for row in self.ws.rcm
        )
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
            20 + 4 * len(self.ws.rcm) + 4 * planned_count
            + 2 * qa_pairs + 2 * eligible_findings
        )
        self.update_limits(
            {
                "max_model_turns": calculated,
                "max_estimated_prompt_tokens": calculated * 10_000,
                "max_completion_tokens": calculated * 4_000,
            },
            grow_only=True,
        )

    def _unit(self, stage: dict, unit_id: str) -> dict:
        return next(item for item in stage.get("units") or [] if item["id"] == unit_id)

    def _set_unit(self, stage: dict, unit: dict, status: str, *, error: str | None = None, result_refs: list[str] | None = None) -> None:
        if self.scheduler is not None:
            self.scheduler.set_unit(
                stage,
                unit,
                status,
                error=error,
                result_refs=result_refs,
            )
            return
        was_attempted = int(unit.get("attempts") or 0)
        maximum_attempts = int(
            (self.run.get("limits") or {}).get("max_execution_attempts") or 2
        )
        if status == "running" and was_attempted >= maximum_attempts:
            message = (
                f"Unit '{unit['id']}' reached its {maximum_attempts}-attempt limit."
            )
            workflow.transition_unit(unit, "failed", error=message)
            self.save()
            self.emit("unit_update", {"stage_id": stage["id"], "unit": copy.deepcopy(unit)})
            raise LimitExceeded(message)
        workflow.transition_unit(unit, status, error=error, result_refs=result_refs)
        self.save()
        self.emit("unit_update", {"stage_id": stage["id"], "unit": copy.deepcopy(unit)})
        if status == "running" and was_attempted:
            self.emit(
                "unit_retry",
                {"stage_id": stage["id"], "unit_id": unit["id"], "attempt": unit["attempts"]},
            )

    def _planning_basis(self, stage: dict, units: list[dict]) -> None:
        unit = units[0]
        self._set_unit(stage, unit, "running")
        try:
            basis = self.stage_context()
            projection = context_bundles.planning_basis_projection(basis)
            sidecar = store.write_sidecar(self.ws, self.run["id"], projection)
            self.run["planning_basis"] = basis
            self.run["planning_basis_projection"] = sidecar
            self._set_unit(stage, unit, "succeeded", result_refs=["planning:context"])
        except (Cancelled, LimitExceeded):
            raise
        except WorkspaceConflict as error:
            self._set_unit(stage, unit, "conflict", error=str(error))
        except Exception as error:
            self._set_unit(stage, unit, "failed", error=str(error))

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
        basis = self.run.get("planning_basis") or {
            "planning": self.ws.planning, "tables": [], "documents": [], "methodology": []
        }
        selected_document_ids = [
            str(item.get("document_id"))
            for item in basis.get("document_analyses") or []
            if item.get("document_id")
        ]
        target = ApmExecutorTarget(
            self.ws,
            self.run["id"],
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task("apm", "workflow:apm", "Audit planning memorandum")

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                apm_document_methodology_scope(
                    self.ws,
                    planning_context=(basis.get("planning") or {}).get("context") or {},
                    document_ids=(
                        selected_document_ids
                        if "document_analyses" in basis
                        else None
                    ),
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
                capability_definition_hash=_capability_definition_hash(capability),
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
        basis = self.run.get("planning_basis") or {"planning": self.ws.planning}
        selected_document_ids = [
            str(item.get("document_id"))
            for item in basis.get("document_analyses") or []
            if item.get("document_id")
        ]
        target = RcmExecutorTarget(
            self.ws,
            self.run["id"],
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task("rcm", "workflow:rcm", "Risk and control matrix")

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                rcm_scope(
                    self.ws,
                    planning_context=(basis.get("planning") or {}).get("context") or {},
                    document_ids=(
                        selected_document_ids
                        if "document_analyses" in basis
                        else None
                    ),
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
                capability_definition_hash=_capability_definition_hash(capability),
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

    def _bind_planned_tests(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind one RCM row's planned-test unit to the shared ``UnitPipeline``.

        The domain-neutral scheduler owns manifest/proposal/receipt persistence,
        proposal reuse, approval, and readiness reevaluation. This binding
        supplies only the row-scoped declared context, the planned-test commit
        target, per-test approval items, and the post-commit bookkeeping that
        translates receipt actions into planning-change accounting.
        """
        self.ws = subject
        rcm_id = unit["parent_refs"][0].split(":", 1)[1]
        expected_row = parent_hashes(self.ws, [f"rcm:{rcm_id}"])
        basis = self.run.get("planning_basis") or {"planning": self.ws.planning}
        selected_document_ids = [
            str(item.get("document_id"))
            for item in basis.get("document_analyses") or []
            if item.get("document_id")
        ]
        target = PlannedTestExecutorTarget(
            self.ws,
            self.run["id"],
            rcm_id,
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task("work_program", "workflow:planned_tests", "RCM planned tests")

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                planned_test_scope(
                    self.ws,
                    rcm_id,
                    planning_context=(basis.get("planning") or {}).get("context") or {},
                    document_ids=(
                        selected_document_ids
                        if "document_analyses" in basis
                        else None
                    ),
                ),
            )

        def approval_provider(proposal):
            proposed = [
                self.proposal_item(
                    str(spec.get("title") or spec.get("objective")),
                    f"Planned test for {rcm_id}.",
                    dict(spec),
                )
                for spec in proposal.get("planned_tests") or []
            ]
            accepted = self.request_approval("planned_tests", task, proposed)
            tests = [dict(item["spec"]) for item in accepted]
            return {"planned_tests": tests} if tests else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            for item in (
                (outcome.receipt.output.get("planned_tests") or [])
                if outcome.receipt
                else []
            ):
                action = str(item.get("action"))
                self.run["planning_changes"][f"planned_test_{action}"] += 1
                self.record_artifact(
                    "planned_test",
                    str(item["id"]),
                    str(item["semantic_id"]),
                    action,
                    None,
                )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="planning.planned_tests",
                executor_id="planning.planned_tests",
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
                capability_definition_hash=_capability_definition_hash(capability),
                approval_kind=(
                    "planned_tests" if self.run["mode"] == "permission" else None
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
            # Unlike the single-unit APM/RCM capabilities, planned tests fan out
            # one unit per RCM row, so this capability's readiness is only
            # satisfied once every row's unit has committed. Post-commit
            # readiness is therefore evaluated by the stage fold, not per unit.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _bind_definitions(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind one execution-definition unit to the shared ``UnitPipeline``.

        The capability fans out into two unit kinds, so the binding selects the
        Data Test or Document Test worker/executor pair from ``unit["kind"]``.
        Everything else is common: the planned-test parent guard, the declared
        row-scoped context, one approval item, and the post-commit artifact
        record.
        """
        self.ws = subject
        planned_id = next(
            ref.split(":", 1)[1]
            for ref in unit["parent_refs"]
            if ref.startswith("planned_test:")
        )
        rcm_id = next(
            ref.split(":", 1)[1]
            for ref in unit["parent_refs"]
            if ref.startswith("rcm:")
        )
        data_test = unit["kind"] == "data_test_spec"
        artifact_kind = "datatest" if data_test else "doctest"
        expected_planned = parent_hashes(self.ws, [f"planned_test:{planned_id}"])
        target_type = (
            DataTestExecutorTarget if data_test else DocumentTestExecutorTarget
        )
        target = target_type(
            self.ws,
            self.run["id"],
            rcm_id,
            planned_id,
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        scope_builder = data_test_spec_scope if data_test else document_test_spec_scope
        proposal_key = "data_test" if data_test else "document_test"
        task = self.add_task(
            "execution_definitions", "workflow:definitions", "Execution definitions"
        )

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                scope_builder(self.ws, rcm_id, planned_id),
            )

        def approval_provider(proposal):
            accepted = self.request_approval(
                "execution_definitions",
                task,
                [
                    self.proposal_item(
                        unit["title"],
                        "Executable definition derived from a required kind.",
                        dict(proposal.get(proposal_key) or {}),
                    )
                ],
            )
            return {proposal_key: dict(accepted[0]["spec"])} if accepted else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            if outcome.receipt is None:
                return
            output = outcome.receipt.output
            self.record_artifact(
                artifact_kind,
                str(output["id"]),
                str(output["semantic_id"]),
                str(output["action"]),
                None,
            )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id=(
                    "fieldwork.data_test_spec"
                    if data_test
                    else "fieldwork.document_test_spec"
                ),
                executor_id=(
                    "fieldwork.data_test" if data_test else "fieldwork.document_test"
                ),
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
                expected_parents=expected_planned,
                capability_definition_hash=_capability_definition_hash(capability),
                approval_kind=(
                    "execution_definitions"
                    if self.run["mode"] == "permission"
                    else None
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
            # Definitions fan out per required execution kind, so this
            # capability's readiness only holds once every unit has committed.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _executions(self, stage: dict, units: list[dict]) -> None:
        """Execute every fieldwork unit and record the outcome as a status.

        The status vocabulary carries audit meaning, not just success/failure:
        `awaiting_confirmation` means the agent produced an answer that only an
        auditor may disposition, and `blocked` means evidence is missing —
        which registers the unit against its evidence request so uploading the
        document can unblock it later.
        """
        # Existing services still combine compute and mutation, so execution is
        # deliberately serial. Model planning/spec calls above are safe to fan out.
        for unit in sorted(units, key=lambda item: item["id"]):
            if unit["status"] in {"succeeded", "skipped"}:
                continue
            self.checkpoint()
            self._set_unit(stage, unit, "running")
            try:
                if unit["kind"] == "document_test_review":
                    artifact_ref = next(
                        ref for ref in unit["parent_refs"] if ref.startswith("doctest:")
                    )
                    self._set_unit(
                        stage, unit, "awaiting_confirmation",
                        error="Auditor review or disposition is required.",
                        result_refs=[artifact_ref],
                    )
                    continue
                if unit["kind"] == "document_qa_execution":
                    test_id = next(
                        ref.split(":", 1)[1]
                        for ref in unit["parent_refs"] if ref.startswith("doctest:")
                    )
                    item_id = next(
                        ref.split(":", 1)[1]
                        for ref in unit["parent_refs"] if ref.startswith("docitem:")
                    )
                    document_id = next(
                        ref.split(":", 1)[1]
                        for ref in unit["parent_refs"] if ref.startswith("document:")
                    )
                    self._refresh_workspace()
                    test = doc_tests.load_test(self.ws, test_id)
                    test_item = next(
                        item for item in test.get("items") or [] if item["id"] == item_id
                    )
                    self._model_context.unit_id = unit["id"]
                    self._model_context.parent_refs = tuple(unit.get("parent_refs") or [])
                    try:
                        answer = documents.document_chat(
                            self.ws, document_id, str(test_item.get("question") or ""),
                            test_item.get("pages"), run_id=self.run["id"],
                            model_adapter=self._document_qa_adapter,
                        )
                    finally:
                        self._model_context.unit_id = None
                        self._model_context.parent_refs = None
                    doc_tests.commit_qa_answer(
                        self.ws, test_id, item_id, document_id, answer
                    )
                    self._set_unit(
                        stage, unit, "awaiting_confirmation",
                        error="The cited answer requires auditor disposition.",
                        result_refs=[f"doctest:{test_id}:item:{item_id}:document:{document_id}"],
                    )
                    self.emit(
                        "workspace_changed",
                        {"kind": "doctest", "id": test_id, "action": "qa_answered"},
                    )
                    continue
                artifact_ref = unit["parent_refs"][-1]
                kind, item_id = artifact_ref.split(":", 1)
                self._refresh_workspace()
                if kind == "datatest":
                    result = data_tests.run(self.ws, item_id)
                    ref = f"datatest:{item_id}:{result['id']}"
                    status = "succeeded" if result.get("semantic_valid") else "blocked"
                    self._set_unit(stage, unit, status, error=None if status == "succeeded" else "; ".join(result.get("semantic_issues") or []), result_refs=[ref])
                else:
                    test = doc_tests.load_test(self.ws, item_id)
                    if doc_tests.evidence_blocked(test):
                        changed_request = False
                        for request in self.ws.evidence_requests:
                            if (
                                request.get("document_test_id") == item_id
                                and request.get("status") == "open"
                            ):
                                request["blocked_unit_id"] = unit["id"]
                                request["updated"] = self.ws._updated_now()
                                changed_request = True
                        if changed_request:
                            self.ws.save()
                        self._set_unit(stage, unit, "blocked", error=test.get("scope_limitations") or "Evidence is unavailable.", result_refs=[artifact_ref])
                        continue
                    for test_item in test.get("items") or []:
                        if test_item.get("state") in {"confirmed", "exception"}:
                            continue
                        doc_tests.run_item(
                            self.ws, item_id, test_item["id"], run_id=self.run["id"],
                            model_adapter=self._document_qa_adapter,
                        )
                    test = doc_tests.load_test(self.ws, item_id)
                    rollup = doc_tests.result_rollup(test)
                    if rollup["pending"] or rollup["manual_review"]:
                        test["status"] = "review_required"
                        status = "awaiting_confirmation"
                    else:
                        test["status"] = "completed"
                        status = "succeeded"
                    doc_tests.save_test(self.ws, test)
                    self._set_unit(stage, unit, status, error="Auditor review or disposition is required." if status != "succeeded" else None, result_refs=[artifact_ref])
                self.emit("workspace_changed", {"kind": kind, "id": item_id, "action": "executed"})
            except (Cancelled, LimitExceeded):
                raise
            except WorkspaceConflict as error:
                self._set_unit(stage, unit, "conflict", error=str(error))
            except Exception as error:
                self._set_unit(stage, unit, "failed", error=str(error))

    def _document_qa_adapter(self, messages: list[dict], activity: dict) -> dict:
        activity = dict(activity or {})
        attempt = int(activity.pop("retry_number", 1) or 1)
        content = self._llm_content(
            str(messages[0].get("content") or ""),
            str(messages[1].get("content") or ""),
            activity,
            attempt=attempt,
        )
        return {"content": content}

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
            refs = roll_up_results(self.ws)
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        self.emit(
            "workspace_changed", {"kind": "rcm", "id": "rollup", "action": "updated"}
        )
        return DeterministicUnitResult("succeeded", tuple(refs))

    def _observation_checkpoint(self) -> None:
        open_items = [item for item in self.ws.observations if item.get("status") != "disposed"]
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
                        {key: item.get(key) for key in ("id", "rcm_id", "planned_test_id", "execution_ref", "summary", "exception_count", "suggested_disposition")}
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
        rcm_execution.rollup(self.ws)
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
            return self.context_resolver.resolve(
                self.ws,
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
                capability_definition_hash=_capability_definition_hash(capability),
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

    # --------------------------------------------------------- concurrency
    def _parallel_candidates(self, stage: dict, units: list[dict], worker_fn):
        """Generate one model proposal per unit, concurrently, then hand back
        deterministic (unit, candidate) pairs for serialized commit.

        Generation is the expensive, parallelizable half; commit is the
        ordering-sensitive half and stays with the caller. A unit whose
        proposal was already generated in an earlier attempt is restored from
        its sidecar instead of being re-billed to the model.
        """
        pending = []
        candidates = []
        for unit in units:
            if unit.get("status") in {"succeeded", "skipped"}:
                continue
            sidecar = unit.get("proposal_sidecar")
            if sidecar:
                try:
                    value = store.read_sidecar(self.ws, self.run["id"], sidecar)
                    self._set_unit(stage, unit, "running")
                    candidates.append((unit, value))
                    self.emit(
                        "proposal_reused",
                        {"stage_id": stage["id"], "unit_id": unit["id"], "sidecar": sidecar},
                    )
                    continue
                except (OSError, ValueError, WorkspaceError):
                    unit["proposal_sidecar"] = None
            pending.append(unit)
        for unit in pending:
            self.checkpoint()
            self._set_unit(stage, unit, "running")

        # Thread-local correlation: _llm_content reads these to attribute
        # concurrent provider calls to the right unit in the provenance ledger.
        def correlated_worker(unit: dict):
            self._model_context.unit_id = unit["id"]
            self._model_context.parent_refs = tuple(unit.get("parent_refs") or [])
            try:
                return worker_fn(unit)
            finally:
                self._model_context.unit_id = None
                self._model_context.parent_refs = None

        # Persist each proposal as it settles, before any commit runs. A crash
        # between generation and commit then resumes from the sidecar rather
        # than paying for the same completion twice.
        def persist_proposal(unit: dict, value, error) -> None:
            if error is not None:
                return
            unit["proposal_sidecar"] = store.write_sidecar(
                self.ws, self.run["id"], value
            )
            self.save()

        settled = workflow.stable_all_settled(
            pending, correlated_worker,
            max_workers=int((self.run.get("limits") or {}).get("max_llm_concurrency") or 4),
            on_settled=persist_proposal,
        )
        # Per-unit failures are absorbed so siblings still commit; cancellation
        # and budget exhaustion are run-level and must propagate.
        for unit, value, error in settled:
            if error is not None:
                if isinstance(error, (Cancelled, LimitExceeded)):
                    raise error
                self._set_unit(stage, unit, "failed", error=str(error))
                continue
            candidates.append((unit, value))
        return sorted(candidates, key=lambda item: item[0]["id"])

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
        open_observations = [item for item in subject.observations if item.get("status") != "disposed"]
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
        summary = (
            "# Audit workflow summary\n\n"
            f"- Requested outcomes: {', '.join(self.run['workflow']['requested_outcomes'])}\n"
            f"- Completion status: {completion['status']}\n"
            f"- Failed/conflict units: {failed}\n"
            f"- Open workflow units: {open_units}\n"
            f"- Open observations: {len(open_observations)}\n"
            f"- Open evidence requests: {len(open_evidence)}\n"
        )
        return FinishProjection(
            next_outcomes=tuple(dict.fromkeys(next_outcomes)),
            summary_markdown=summary,
            terminal_status=terminal,
        )


# Capabilities still executed by the transitional batch adapter. Migrated
# capabilities (native pipeline bindings) are handled explicitly below and are
# intentionally absent from this map.
_AUDIT_HANDLER_NAMES = {
    "planning.context_ready": "_planning_basis",
    "fieldwork.executed": "_executions",
}

_PARTIAL_DEPENDENCIES = {
    "fieldwork.definitions_ready": {"planning.planned_tests_ready"},
    "fieldwork.executed": {"fieldwork.definitions_ready"},
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
    # Capabilities that have completed their Phase 7 execution migration are
    # registered as native pipeline bindings; the domain-neutral scheduler drives
    # their UnitPipeline. Everything else still uses the transitional batch adapter.
    _PIPELINE_BINDERS = {
        "planning.apm_ready": (
            adapter._bind_apm,
            {"worker": "planning.apm", "executor": "planning.apm"},
        ),
        "planning.rcm_ready": (
            adapter._bind_rcm,
            {"worker": "planning.rcm", "executor": "planning.rcm"},
        ),
        "planning.planned_tests_ready": (
            adapter._bind_planned_tests,
            {
                "worker": "planning.planned_tests",
                "executor": "planning.planned_tests",
            },
        ),
        "fieldwork.definitions_ready": (
            adapter._bind_definitions,
            {
                "worker": "fieldwork.data_test_spec|fieldwork.document_test_spec",
                "executor": "fieldwork.data_test|fieldwork.document_test",
            },
        ),
        "findings.drafted": (
            adapter._bind_finding,
            {"worker": "reporting.finding", "executor": "reporting.finding"},
        ),
    }
    # Deterministic (no-model) capabilities bound through the scheduler's
    # deterministic execution path instead of the transitional batch adapter.
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
    executions = CapabilityExecutionRegistry()
    for capability in audit_capabilities.REGISTRY.all():
        pipeline_binding = _PIPELINE_BINDERS.get(capability.id)
        if pipeline_binding is not None:
            binder, identity = pipeline_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=_sha256_json(
                        {"capability": capability.id, **identity}
                    ),
                    pipeline_binder=binder,
                )
            )
            continue
        deterministic_binding = _DETERMINISTIC_BINDERS.get(capability.id)
        if deterministic_binding is not None:
            binder, identity = deterministic_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=_sha256_json(
                        {"capability": capability.id, **identity}
                    ),
                    deterministic_executor=binder,
                )
            )
            continue
        handler_name = _AUDIT_HANDLER_NAMES[capability.id]
        handler = getattr(adapter, handler_name)

        def execute_batch(
            _scheduler: WorkflowRunner,
            stage: dict,
            units: list[dict],
            *,
            _handler=handler,
        ) -> None:
            _handler(stage, units)

        executions.register(
            CapabilityExecution(
                capability_id=capability.id,
                implementation_hash=_sha256_json(
                    {
                        "capability": capability.id,
                        "adapter": handler_name,
                    }
                ),
                transitional_batch_executor=execute_batch,
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
    }

    def before_stage(
        subject: Workspace,
        capability: workflow.Capability,
        _stage: dict,
    ) -> None:
        adapter.ws = subject
        checkpoint = STAGE_CHECKPOINTS.get(capability.id)
        if checkpoint is not None and run.get("mode") == "permission":
            checkpoint_handlers[checkpoint]()

    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=adapter.runtime,
        registry=audit_capabilities.REGISTRY,
        executions=executions,
        unit_pipeline=unit_pipeline,
        refresh_subject=adapter._refresh_workspace,
        refresh_limits=lambda _subject: adapter._refresh_dynamic_limits(),
        dependency_policy=dependency_policy,
        before_stage=before_stage,
        finish_evaluator=adapter._finish_projection,
    )
    adapter.scheduler = scheduler
    scheduler.execution_adapter = adapter
    return scheduler

