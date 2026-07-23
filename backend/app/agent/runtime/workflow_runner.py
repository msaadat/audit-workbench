"""Domain-neutral capability workflow scheduling.

This module owns the generic durable scheduler mechanics.  Domain packages
provide capability definitions and, during the Phase 6 transition, injected
stage handlers.  Active production dispatch moves here only after the
remaining routing and registry boundaries are in place.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import workflow
from .run_runtime import Cancelled, LimitExceeded, RunRuntime
from .unit_pipeline import (
    ContextIdentityProvider,
    ContextProvider,
    UnitPipeline,
    UnitPipelineConflict,
    UnitPipelineOutcome,
    UnitPipelineRequest,
)


RefreshSubject = Callable[[], Any]
RefreshLimits = Callable[[Any], None]
DependencyPolicy = Callable[[str, str, str], bool]
BeforeStage = Callable[[Any, workflow.Capability, dict[str, Any]], None]


@dataclass(frozen=True)
class FinishProjection:
    """Optional domain projection applied after generic terminal folding."""

    next_outcomes: tuple[str, ...] = ()
    summary_markdown: str | None = None
    terminal_status: str | None = None


FinishEvaluator = Callable[
    [Any, dict[str, Any], tuple[dict[str, Any], ...]], FinishProjection
]


OutcomeHandler = Callable[
    [dict[str, Any], dict[str, Any], UnitPipelineOutcome], None
]
ConflictHandler = Callable[
    [dict[str, Any], dict[str, Any], UnitPipelineConflict],
    tuple[str, str] | None,
]


@dataclass(frozen=True)
class BoundUnitPipeline:
    """All local providers needed to execute one registered pipeline unit."""

    request: UnitPipelineRequest
    context_provider: ContextProvider
    context_identity_provider: ContextIdentityProvider
    target: object
    approval_provider: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None
    readiness_provider: Callable[[], object] | None = None
    # Optional domain callbacks. These keep the scheduler domain-neutral: the
    # binding supplies them, the scheduler merely invokes them. ``on_committed``
    # runs after a successful commit and before the unit is marked succeeded;
    # ``conflict_handler`` may translate a domain-recognized ``UnitPipelineConflict``
    # into a ``(status, error)`` override instead of the default ``conflict``.
    on_committed: OutcomeHandler | None = None
    conflict_handler: ConflictHandler | None = None


PipelineBinder = Callable[
    [Any, dict[str, Any], workflow.Capability, dict[str, Any], dict[str, Any]],
    BoundUnitPipeline,
]
BatchExecutor = Callable[
    ["WorkflowRunner", dict[str, Any], list[dict[str, Any]]], None
]


@dataclass(frozen=True)
class CapabilityExecution:
    """Hash-identified execution binding for one capability declaration."""

    capability_id: str
    implementation_hash: str
    pipeline_binder: PipelineBinder | None = None
    transitional_batch_executor: BatchExecutor | None = None

    def __post_init__(self) -> None:
        capability_id = str(self.capability_id or "").strip()
        implementation_hash = str(self.implementation_hash or "").strip()
        if not capability_id:
            raise ValueError("Capability execution requires a capability_id.")
        if not implementation_hash.startswith("sha256:") or len(implementation_hash) != 71:
            raise ValueError(
                "Capability execution implementation_hash must be a sha256 hash."
            )
        bindings = sum(
            value is not None
            for value in (self.pipeline_binder, self.transitional_batch_executor)
        )
        if bindings != 1:
            raise ValueError(
                "Capability execution requires exactly one pipeline or transitional binding."
            )
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "implementation_hash", implementation_hash)

    @property
    def pipeline_backed(self) -> bool:
        return self.pipeline_binder is not None


class CapabilityExecutionRegistry:
    """Validated lookup boundary between declarations and unit execution."""

    def __init__(self) -> None:
        self._values: dict[str, CapabilityExecution] = {}

    def register(self, execution: CapabilityExecution) -> CapabilityExecution:
        if not isinstance(execution, CapabilityExecution):
            raise ValueError("Execution registry values must be CapabilityExecution.")
        if execution.capability_id in self._values:
            raise ValueError(
                f"Capability execution '{execution.capability_id}' is already registered."
            )
        self._values[execution.capability_id] = execution
        return execution

    def get(self, capability_id: str) -> CapabilityExecution:
        try:
            return self._values[str(capability_id)]
        except KeyError as error:
            raise ValueError(
                f"Capability '{capability_id}' has no registered execution."
            ) from error

    def validate(self, registry: workflow.CapabilityRegistry) -> None:
        declared = {capability.id for capability in registry.all()}
        unknown = sorted(set(self._values) - declared)
        missing = sorted(declared - set(self._values))
        if unknown or missing:
            parts = []
            if missing:
                parts.append("missing: " + ", ".join(missing))
            if unknown:
                parts.append("unknown: " + ", ".join(unknown))
            raise ValueError("Capability execution registry mismatch (" + "; ".join(parts) + ").")


class WorkflowRunner:
    """Schedule a declared capability graph through injected runtime services.

    The scheduler has no workspace or audit imports.  ``subject`` is the local
    object assessed by capability readiness functions; applications may
    refresh it between stages through ``refresh_subject``.  Stage behavior is
    temporarily injected as handlers so the generic mechanics can be verified
    before active dispatch changes. Pipeline-backed execution resolves declared
    context and delegates worker/executor lookup to :class:`UnitPipeline`.
    """

    def __init__(
        self,
        *,
        subject: Any,
        run: dict[str, Any],
        runtime: RunRuntime,
        registry: workflow.CapabilityRegistry,
        executions: CapabilityExecutionRegistry,
        unit_pipeline: UnitPipeline | None = None,
        refresh_subject: RefreshSubject | None = None,
        refresh_limits: RefreshLimits | None = None,
        dependency_policy: DependencyPolicy | None = None,
        before_stage: BeforeStage | None = None,
        finish_evaluator: FinishEvaluator | None = None,
    ) -> None:
        self.subject = subject
        self.run = run
        self.runtime = runtime
        self.registry = registry
        if not isinstance(executions, CapabilityExecutionRegistry):
            raise ValueError("WorkflowRunner requires a CapabilityExecutionRegistry.")
        executions.validate(registry)
        self.executions = executions
        self.unit_pipeline = unit_pipeline
        self.refresh_subject = refresh_subject
        self.refresh_limits = refresh_limits
        self.dependency_policy = dependency_policy
        self.before_stage = before_stage
        self.finish_evaluator = finish_evaluator

    def materialize(
        self,
        requested_outcomes: list[str],
        *,
        scope: dict[str, Any] | None = None,
        workflow_id: str = workflow.DEFAULT_WORKFLOW_ID,
        generation_mode: str = "reuse_existing",
    ) -> dict[str, Any]:
        """Install a deterministic capability closure on the durable run."""

        normalized_scope = dict(scope or {})
        resolved, stages, reused = workflow.materialize(
            self.registry,
            self.subject,
            list(requested_outcomes),
            normalized_scope,
            generation_mode=generation_mode,
        )
        state = {
            "id": workflow_id,
            "requested_outcomes": list(requested_outcomes),
            "resolved_outcomes": resolved,
            "scope": normalized_scope,
            "generation_mode": workflow.normalize_generation_mode(generation_mode),
            "reused_outcomes": reused,
            "reused_outcome_details": [
                {
                    "capability": capability_id,
                    "currency_status": "not_assessed",
                }
                for capability_id in reused
            ],
            "stages": stages,
            "next_outcomes": [],
        }
        self.run["workflow"] = state
        self.runtime.save()
        self.runtime.emit("workflow_resolved", {"workflow": copy.deepcopy(state)})
        return state

    def execute(self) -> None:
        """Recover and execute the installed graph in dependency order."""

        if not self.run.get("started"):
            self.runtime.mark_started()
        try:
            state = self.run.get("workflow")
            if not isinstance(state, dict):
                raise ValueError("A materialized workflow is required before execution.")
            workflow.recovery(state)
            self.runtime.save()
            self.runtime.set_status("executing")
            scheduled = {
                str(stage["capability"]): stage for stage in state.get("stages") or []
            }
            for stage in state.get("stages") or []:
                if stage.get("status") in {"succeeded", "skipped"}:
                    continue
                self.runtime.checkpoint()
                self._refresh()
                capability = self.registry.get(str(stage["capability"]))
                if self.before_stage is not None:
                    self.before_stage(self.subject, capability, stage)
                    self._refresh()
                blocking = [
                    dependency
                    for dependency in capability.depends_on
                    if dependency in scheduled
                    and not self._dependency_satisfied(
                        capability.id,
                        dependency,
                        str(scheduled[dependency].get("status") or "queued"),
                    )
                ]
                if blocking:
                    self._block_stage(stage, blocking)
                    continue
                self._run_stage(stage)
            self._finish()
        except Cancelled:
            self._cancel_remaining()
            self.runtime.mark_finished()
            self._set_command_status("cancelled")
            self.runtime.set_status("cancelled")
        except LimitExceeded as error:
            self.run["error"] = str(error)
            self._mark_running("failed", str(error))
            self.runtime.mark_finished()
            self._set_command_status("failed")
            self.runtime.set_status("failed")
        except Exception as error:
            self.run["error"] = str(error)
            self._mark_running("failed", str(error))
            self.runtime.mark_finished()
            self._set_command_status("failed")
            self.runtime.set_status("failed")

    def stable_all_settled(
        self,
        units: list[dict[str, Any]],
        worker: Callable[[dict[str, Any]], Any],
        *,
        on_settled: Callable[
            [dict[str, Any], Any | None, Exception | None], None
        ]
        | None = None,
    ) -> list[tuple[dict[str, Any], Any | None, Exception | None]]:
        """Run independent work all-settled and return semantic-unit order."""

        maximum = int(
            (self.run.get("limits") or {}).get("max_llm_concurrency") or 4
        )
        return workflow.stable_all_settled(
            units,
            worker,
            max_workers=maximum,
            on_settled=on_settled,
        )

    def set_unit(
        self,
        stage: dict[str, Any],
        unit: dict[str, Any],
        status: str,
        *,
        error: str | None = None,
        result_refs: list[str] | None = None,
    ) -> None:
        """Apply and durably publish a validated semantic-unit transition."""

        previous_attempts = int(unit.get("attempts") or 0)
        maximum_attempts = int(
            (self.run.get("limits") or {}).get("max_execution_attempts") or 2
        )
        if status == "running" and previous_attempts >= maximum_attempts:
            message = (
                f"Unit '{unit['id']}' reached its {maximum_attempts}-attempt limit."
            )
            workflow.transition_unit(unit, "failed", error=message)
            self.runtime.save()
            self.runtime.emit(
                "unit_update", {"stage_id": stage["id"], "unit": copy.deepcopy(unit)}
            )
            raise LimitExceeded(message)
        workflow.transition_unit(
            unit,
            status,
            error=error,
            result_refs=result_refs,
        )
        self.runtime.save()
        self.runtime.emit(
            "unit_update", {"stage_id": stage["id"], "unit": copy.deepcopy(unit)}
        )
        if status == "running" and previous_attempts:
            self.runtime.emit(
                "unit_retry",
                {
                    "stage_id": stage["id"],
                    "unit_id": unit["id"],
                    "attempt": unit["attempts"],
                },
            )

    def ensure_stage_units(self, stage: dict[str, Any]) -> list[dict[str, Any]]:
        """Re-expand a stage against the current subject with stable IDs."""

        capability = self.registry.get(str(stage["capability"]))
        scope = self._workflow_scope()
        specs = capability.expand_units(self.subject, scope)
        maximum = int(
            (self.run.get("limits") or {}).get("max_units_per_stage") or 250
        )
        if len(specs) > maximum:
            raise LimitExceeded(
                f"Stage '{stage['title']}' requires {len(specs)} units, "
                f"above its {maximum}-unit limit."
            )
        units = stage.setdefault("units", [])
        existing = {str(unit["id"]): unit for unit in units}
        changed = False
        for spec in specs:
            current = existing.get(spec.id)
            if current is None:
                units.append(workflow.new_unit(spec, capability.id))
                changed = True
                continue
            if current.get("status") == "queued":
                refreshed = {
                    "kind": spec.kind,
                    "title": spec.title,
                    "parent_refs": list(spec.parent_refs),
                    "input_sha1": spec.input_sha1,
                }
                if any(current.get(key) != value for key, value in refreshed.items()):
                    current.update(refreshed)
                    changed = True
        if changed:
            self.runtime.save()
            self.runtime.emit("stage_update", {"stage": copy.deepcopy(stage)})
        return units

    def _refresh(self) -> None:
        if self.refresh_subject is not None:
            self.subject = self.refresh_subject()
        if self.refresh_limits is not None:
            self.refresh_limits(self.subject)

    def _workflow_scope(self) -> dict[str, Any]:
        state = self.run.get("workflow") or {}
        scope = dict(state.get("scope") or {})
        if "target_refs" not in scope and state.get("target_refs") is not None:
            scope["target_refs"] = list(state.get("target_refs") or [])
        scope.setdefault(
            "generation_mode",
            workflow.normalize_generation_mode(
                state.get("generation_mode") or "reuse_existing"
            ),
        )
        return scope

    def _dependency_satisfied(
        self,
        capability_id: str,
        dependency_id: str,
        dependency_status: str,
    ) -> bool:
        if dependency_status in {"succeeded", "skipped"}:
            return True
        if self.dependency_policy is None:
            return False
        return bool(
            self.dependency_policy(
                capability_id,
                dependency_id,
                dependency_status,
            )
        )

    def _block_stage(self, stage: dict[str, Any], blocking: list[str]) -> None:
        units = self.ensure_stage_units(stage)
        reason = "Blocked by prerequisite stage(s): " + ", ".join(blocking)
        for unit in units:
            if unit.get("status") == "queued":
                self.set_unit(stage, unit, "blocked", error=reason)
        self._set_stage(stage, "blocked")
        self.runtime.emit(
            "stage_summary",
            {
                "stage_id": stage["id"],
                "status": "blocked",
                "counts": workflow.stage_counts(stage),
            },
        )

    def _run_stage(self, stage: dict[str, Any]) -> None:
        self._set_stage(stage, "running")
        units = self.ensure_stage_units(stage)
        capability = self.registry.get(str(stage["capability"]))
        if not units:
            readiness = capability.readiness(
                self.subject,
                self._workflow_scope(),
            )
            self._set_stage(stage, "succeeded" if readiness.satisfied else "blocked")
            return
        execution = self.executions.get(capability.id)
        if execution.pipeline_backed:
            self._run_pipeline_units(execution, capability, stage, units)
        else:
            assert execution.transitional_batch_executor is not None
            execution.transitional_batch_executor(self, stage, units)
        final = self._fold_stage(units)
        self._set_stage(stage, final)
        self.runtime.emit(
            "stage_summary",
            {
                "stage_id": stage["id"],
                "status": final,
                "counts": workflow.stage_counts(stage),
            },
        )

    def _run_pipeline_units(
        self,
        execution: CapabilityExecution,
        capability: workflow.Capability,
        stage: dict[str, Any],
        units: list[dict[str, Any]],
    ) -> None:
        if self.unit_pipeline is None:
            raise ValueError(
                f"Pipeline-backed capability '{capability.id}' requires UnitPipeline."
            )
        assert execution.pipeline_binder is not None
        for unit in sorted(units, key=lambda item: str(item["id"])):
            if unit.get("status") in {"succeeded", "skipped"}:
                continue
            self.runtime.checkpoint()
            self.set_unit(stage, unit, "running")
            bound: BoundUnitPipeline | None = None
            try:
                bound = execution.pipeline_binder(
                    self.subject,
                    self.run,
                    capability,
                    stage,
                    unit,
                )
                if not isinstance(bound, BoundUnitPipeline):
                    raise ValueError(
                        "Pipeline binder must return BoundUnitPipeline."
                    )

                def record(field: str):
                    def persist(reference: Mapping[str, str]) -> None:
                        unit[field] = dict(reference)
                        self.runtime.save()

                    return persist

                outcome = self.unit_pipeline.run(
                    bound.request,
                    context_provider=bound.context_provider,
                    context_identity_provider=bound.context_identity_provider,
                    target=bound.target,
                    approval_provider=bound.approval_provider,
                    readiness_provider=bound.readiness_provider,
                    on_manifest_persisted=record("context_manifest"),
                    on_proposal_persisted=record("proposal_sidecar"),
                    on_receipt_persisted=record("receipt_sidecar"),
                )
                self._fold_pipeline_outcome(stage, unit, outcome, bound)
            except UnitPipelineConflict as error:
                self._record_pipeline_error_references(unit, error)
                override = (
                    bound.conflict_handler(stage, unit, error)
                    if bound is not None and bound.conflict_handler is not None
                    else None
                )
                if override is not None:
                    status, message = override
                    self.set_unit(stage, unit, status, error=message)
                else:
                    self.set_unit(stage, unit, "conflict", error=str(error))
            except (Cancelled, LimitExceeded):
                raise
            except Exception as error:
                self.set_unit(stage, unit, "failed", error=str(error))

    def _fold_pipeline_outcome(
        self,
        stage: dict[str, Any],
        unit: dict[str, Any],
        outcome: UnitPipelineOutcome,
        bound: BoundUnitPipeline,
    ) -> None:
        if outcome.proposal_reused:
            self.runtime.emit(
                "proposal_reused",
                {
                    "stage_id": stage["id"],
                    "unit_id": unit["id"],
                    "sidecar": dict(outcome.proposal_reference),
                },
            )
        for reason in outcome.proposal_reuse_rejection_reasons:
            self.runtime.emit(
                "proposal_reuse_rejected",
                {
                    "stage_id": stage["id"],
                    "unit_id": unit["id"],
                    "reason": reason,
                },
            )
        if outcome.status == "approval_rejected":
            self.set_unit(
                stage,
                unit,
                "blocked",
                error="The capability proposal was not approved.",
            )
            return
        if bound.on_committed is not None:
            bound.on_committed(stage, unit, outcome)
        result_refs = (
            list(outcome.receipt.artifact_refs) if outcome.receipt is not None else []
        )
        self.set_unit(stage, unit, "succeeded", result_refs=result_refs)

    @staticmethod
    def _record_pipeline_error_references(
        unit: dict[str, Any], error: UnitPipelineConflict
    ) -> None:
        for field, reference in (
            ("context_manifest", error.manifest_reference),
            ("proposal_sidecar", error.proposal_reference),
            ("receipt_sidecar", error.receipt_reference),
        ):
            if reference is not None:
                unit[field] = dict(reference)

    @staticmethod
    def _fold_stage(units: list[dict[str, Any]]) -> str:
        statuses = {str(unit.get("status") or "queued") for unit in units}
        if statuses <= {"succeeded", "skipped"}:
            return "succeeded"
        if "failed" in statuses or "conflict" in statuses:
            return "failed"
        if statuses & {"blocked", "awaiting_input", "awaiting_confirmation"}:
            return "review_required"
        return "failed"

    def _set_stage(self, stage: dict[str, Any], status: str) -> None:
        stage["status"] = status
        if status == "running":
            stage["started_at"] = stage.get("started_at") or self.runtime.utcnow()
        if status in {
            "succeeded",
            "failed",
            "review_required",
            "blocked",
            "skipped",
            "cancelled",
        }:
            stage["finished_at"] = self.runtime.utcnow()
        self.runtime.save()
        self.runtime.emit("stage_update", {"stage": copy.deepcopy(stage)})

    def _finish(self) -> None:
        self._refresh()
        state = self.run["workflow"]
        stages = tuple(state.get("stages") or ())
        failed = sum(
            unit.get("status") in {"failed", "conflict"}
            for stage in stages
            for unit in stage.get("units") or []
        )
        open_units = sum(
            unit.get("status")
            in {"blocked", "awaiting_input", "awaiting_confirmation"}
            for stage in stages
            for unit in stage.get("units") or []
        )
        default_next = tuple(
            str(stage["capability"])
            for stage in stages
            if stage.get("status") in {"failed", "blocked", "review_required"}
        )
        projection = (
            self.finish_evaluator(self.subject, state, stages)
            if self.finish_evaluator is not None
            else FinishProjection(next_outcomes=default_next)
        )
        terminal = projection.terminal_status
        if terminal is None:
            terminal = (
                "failed"
                if failed
                else "completed_with_open_items"
                if open_units
                else "completed"
            )
        state["next_outcomes"] = list(dict.fromkeys(projection.next_outcomes))
        self.run["summary_markdown"] = projection.summary_markdown or (
            "# Workflow summary\n\n"
            f"- Requested outcomes: {', '.join(state.get('requested_outcomes') or [])}\n"
            f"- Failed/conflict units: {failed}\n"
            f"- Open workflow units: {open_units}\n"
        )
        if terminal == "failed" and not self.run.get("error"):
            errors = [
                str(unit.get("error"))
                for stage in stages
                for unit in stage.get("units") or []
                if unit.get("status") in {"failed", "conflict"} and unit.get("error")
            ]
            self.run["error"] = errors[0] if errors else "One or more workflow units failed."
        self.runtime.mark_finished()
        self._set_command_status(terminal)
        self.runtime.set_status(terminal)
        self.runtime.emit("summary_ready", {"run_id": self.run["id"]})

    def _set_command_status(self, status: str) -> None:
        command = self.run.get("command")
        if isinstance(command, dict):
            command["status"] = status

    def _cancel_remaining(self) -> None:
        for stage in (self.run.get("workflow") or {}).get("stages") or []:
            for unit in stage.get("units") or []:
                if unit.get("status") in {
                    "queued",
                    "running",
                    "blocked",
                    "awaiting_input",
                    "awaiting_confirmation",
                }:
                    workflow.transition_unit(unit, "cancelled")
            if stage.get("status") in {"queued", "running"}:
                stage["status"] = "cancelled"
        self.runtime.save()

    def _mark_running(self, status: str, error: str) -> None:
        for stage in (self.run.get("workflow") or {}).get("stages") or []:
            for unit in stage.get("units") or []:
                if unit.get("status") == "running":
                    workflow.transition_unit(unit, status, error=error)
            if stage.get("status") == "running":
                stage["status"] = "failed"
        self.runtime.save()
