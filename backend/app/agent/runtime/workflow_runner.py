"""Domain-neutral capability workflow scheduling.

This module owns the generic durable scheduler mechanics.  Domain packages
provide capability declarations and one validated execution binding per
capability; the scheduler itself knows nothing about any domain.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import narration, workflow
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
StageMilestoneProjector = Callable[
    [Any, dict[str, Any], workflow.Capability, dict[str, Any]],
    Mapping[str, Any] | None,
]


@dataclass(frozen=True)
class FinishProjection:
    """Optional domain projection applied after generic terminal folding."""

    next_outcomes: tuple[str, ...] = ()
    summary_markdown: str | None = None
    terminal_status: str | None = None


FinishEvaluator = Callable[
    [Any, dict[str, Any], tuple[dict[str, Any], ...]], FinishProjection
]


FAILED_UNIT_STATUSES = frozenset({"failed", "conflict"})
OPEN_UNIT_STATUSES = frozenset(
    {"blocked", "awaiting_input", "awaiting_confirmation"}
)
SETTLED_UNIT_STATUSES = frozenset({"succeeded", "skipped"})
UNSETTLED_STAGE_STATUSES = frozenset({"failed", "blocked", "review_required"})


def _units(stages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [unit for stage in stages for unit in stage.get("units") or []]


def fold_terminal_status(
    stages: Iterable[dict[str, Any]],
    *,
    complete: bool = True,
) -> str:
    """Choose a run's terminal status from what its units actually settled.

    Every workflow composition folds the same way, so the rule lives here once.
    A failed unit only makes the *run* failed when nothing else settled: a unit
    failing is ordinary — one transient provider error inside a fan-out — and a
    run that committed most of its work must not be reported as if it committed
    none. ``complete`` lets a domain add its own substantive condition (an audit
    asked to verify itself is not "completed" merely because its units ran).
    """
    units = list(_units(stages))
    failed = sum(unit.get("status") in FAILED_UNIT_STATUSES for unit in units)
    open_units = sum(unit.get("status") in OPEN_UNIT_STATUSES for unit in units)
    settled = sum(unit.get("status") in SETTLED_UNIT_STATUSES for unit in units)
    if failed and not settled:
        return "failed"
    if failed:
        return "completed_with_failures"
    if open_units or not complete:
        return "completed_with_open_items"
    return "completed"


def first_unit_error(stages: Iterable[dict[str, Any]], fallback: str) -> str:
    """The first reported unit error, for the run-level error line."""
    for unit in _units(stages):
        if unit.get("status") in FAILED_UNIT_STATUSES and unit.get("error"):
            return str(unit["error"])
    return fallback


def unsettled_capabilities(stages: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Capabilities a follow-up run should reattempt, in scheduled order.

    These become ``next_outcomes``, which is what "Continue" replays. Producing
    them for a partially failed run is what makes resuming the remainder
    possible instead of forcing a full retry.
    """
    return tuple(
        dict.fromkeys(
            str(stage["capability"])
            for stage in stages
            if stage.get("status") in UNSETTLED_STAGE_STATUSES
            and stage.get("capability")
        )
    )


@dataclass(frozen=True)
class DeterministicUnitResult:
    """Outcome of one deterministic (no-model) unit execution."""

    status: str
    result_refs: tuple[str, ...] = ()
    error: str | None = None


OutcomeHandler = Callable[
    [dict[str, Any], dict[str, Any], UnitPipelineOutcome],
    DeterministicUnitResult | None,
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
    # runs after a successful commit — or, for a proposal-only unit whose request
    # declares no executor, after its proposal is durable — and before the unit is
    # marked succeeded. It may return a ``DeterministicUnitResult`` to replace the
    # default ``succeeded`` fold when the domain owns the unit's terminal meaning
    # (for example an answer that needs additional evidence);
    # ``conflict_handler`` may translate a domain-recognized ``UnitPipelineConflict``
    # into a ``(status, error)`` override instead of the default ``conflict``.
    on_committed: OutcomeHandler | None = None
    conflict_handler: ConflictHandler | None = None


# A binder returns a bound pipeline for a unit that needs a model proposal, or a
# ``DeterministicUnitResult`` for a unit of the same capability that does not.
# Capabilities carry exactly one execution binding, so this is how a capability
# whose units are of mixed kinds binds each unit at its own boundary.
PipelineBinder = Callable[
    [Any, dict[str, Any], workflow.Capability, dict[str, Any], dict[str, Any]],
    "BoundUnitPipeline | DeterministicUnitResult",
]
DeterministicExecutor = Callable[
    [Any, dict[str, Any], workflow.Capability, dict[str, Any], dict[str, Any]],
    DeterministicUnitResult,
]


@dataclass(frozen=True)
class CapabilityExecution:
    """Hash-identified execution binding for one capability declaration.

    Exactly one binding kind is supplied: a ``pipeline_binder``, whose units go
    through :class:`UnitPipeline` (or settle locally when a unit of a mixed-kind
    capability needs no model), or a ``deterministic_executor``, a no-model
    per-unit computation and commit.
    """

    capability_id: str
    implementation_hash: str
    pipeline_binder: PipelineBinder | None = None
    deterministic_executor: DeterministicExecutor | None = None

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
            for value in (self.pipeline_binder, self.deterministic_executor)
        )
        if bindings != 1:
            raise ValueError(
                "Capability execution requires exactly one pipeline or "
                "deterministic binding."
            )
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "implementation_hash", implementation_hash)

    @property
    def pipeline_backed(self) -> bool:
        return self.pipeline_binder is not None

    @property
    def deterministic_backed(self) -> bool:
        return self.deterministic_executor is not None


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
    refresh it between stages through ``refresh_subject``.  Unit behavior comes
    from the capability's registered execution binding: pipeline-backed units
    resolve declared context and delegate worker/executor lookup to
    :class:`UnitPipeline`, deterministic units return their own outcome.
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
        milestone_projector: StageMilestoneProjector | None = None,
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
        self.milestone_projector = milestone_projector
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
            self._terminate("cancelled")
        except LimitExceeded as error:
            self.run["error"] = str(error)
            self._mark_running("failed", str(error))
            self._terminate("failed")
        except Exception as error:
            self.run["error"] = str(error)
            self._mark_running("failed", str(error))
            self._terminate("failed")

    def _terminate(self, status: str) -> None:
        """Close an abandoned run: terminal status first, then its closing turn.

        A run that was cancelled or that failed is exactly the run whose reason
        the auditor most needs stated in words, so the narration path is shared
        with the normal ``_finish`` ending rather than skipped here.
        """
        narration.say(
            self.run, self.runtime.emit, narration.closing_text(self.run, status)
        )
        self.runtime.mark_finished()
        self._set_command_status(status)
        self.runtime.set_status(status)

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
        if status != "running":
            self._refresh_stage_activity(stage)

    def _refresh_stage_activity(self, stage: dict[str, Any]) -> None:
        """Keep the run's live activity counter in step with settled units.

        ``_set_stage`` publishes the initial ``current=0`` when a stage starts;
        without this, that count never advances as units settle, so the
        user-facing "(current/total)" label stays frozen even while
        task_counts and the plan spine (which recompute live) progress.
        """
        units = stage.get("units") or []
        if not units:
            return
        stage_id = str(stage.get("id") or stage.get("capability") or "workflow")
        phase = f"workflow.{stage_id}"
        if (self.run.get("activity") or {}).get("phase") != phase:
            return
        settled = sum(unit.get("status") in SETTLED_UNIT_STATUSES for unit in units)
        self.runtime.set_activity(
            phase,
            str(stage.get("title") or "Workflow"),
            current=settled,
            total=len(units),
            task_id=f"workflow:{stage_id}",
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
            self._run_deterministic_units(execution, capability, stage, units)
        final = self._fold_stage(units)
        # Serialized units may have committed a newer workspace revision. The
        # milestone must summarize the durable result, not the stage-start
        # snapshot.
        self._refresh()
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
        pending = [
            unit
            for unit in sorted(units, key=lambda item: str(item["id"]))
            if unit.get("status") not in {"succeeded", "skipped"}
        ]
        if capability.barrier == workflow.PARALLEL_BARRIER:
            self._run_parallel_pipeline_units(execution, capability, stage, pending)
            return
        for unit in pending:
            self.runtime.checkpoint()
            self.set_unit(stage, unit, "running")
            self._run_one_pipeline_unit(execution, capability, stage, unit)
            # Serialized units can commit workspace state. Rebind the next
            # unit against that committed state instead of retaining a stale
            # stage-start subject and producing a false parent conflict.
            self._refresh()

    def _run_parallel_pipeline_units(
        self,
        execution: CapabilityExecution,
        capability: workflow.Capability,
        stage: dict[str, Any],
        units: list[dict[str, Any]],
    ) -> None:
        """Fan a declared-parallel capability's units out, bounded and all-settled.

        A capability declares this barrier only when its units are independent
        and commit nothing, so the concurrency here is confined to context
        resolution, the provider call, and proposal persistence — all of which
        the runtime already serializes internally. One unit's failure never
        cancels its siblings, and every settled unit keeps the proposal it
        already paid for. Binding and folding stay on this thread, and results
        come back in semantic-unit order, so the durable transcript is identical
        regardless of completion timing.
        """
        if not units:
            return
        self.runtime.checkpoint()
        bindings: dict[str, BoundUnitPipeline] = {}
        runnable: list[dict[str, Any]] = []
        for unit in units:
            self.set_unit(stage, unit, "running")
            try:
                binding = execution.pipeline_binder(
                    self.subject,
                    self.run,
                    capability,
                    stage,
                    unit,
                )
            except (Cancelled, LimitExceeded):
                raise
            except Exception as error:
                self.set_unit(stage, unit, "failed", error=str(error))
                continue
            if isinstance(binding, DeterministicUnitResult):
                self._set_deterministic_unit(stage, unit, binding)
                continue
            if not isinstance(binding, BoundUnitPipeline):
                self.set_unit(
                    stage,
                    unit,
                    "failed",
                    error=(
                        "Pipeline binder must return BoundUnitPipeline or "
                        "DeterministicUnitResult."
                    ),
                )
                continue
            bindings[str(unit["id"])] = binding
            runnable.append(unit)

        def execute(unit: dict[str, Any]) -> UnitPipelineOutcome:
            bound = bindings[str(unit["id"])]
            assert self.unit_pipeline is not None
            return self.unit_pipeline.run(
                bound.request,
                context_provider=bound.context_provider,
                context_identity_provider=bound.context_identity_provider,
                target=bound.target,
                approval_provider=bound.approval_provider,
                readiness_provider=bound.readiness_provider,
                on_manifest_persisted=self._reference_recorder(unit, "context_manifest"),
                on_manifest_resolved=self._narrate_context(capability),
                on_proposal_persisted=self._reference_recorder(unit, "proposal_sidecar"),
                on_receipt_persisted=self._reference_recorder(unit, "receipt_sidecar"),
                on_worker_repaired=self._narrate_repair(),
                on_worker_completed=self._narrate_completion(capability),
            )

        settled = self.stable_all_settled(runnable, execute)
        for unit, outcome, failure in settled:
            bound = bindings[str(unit["id"])]
            if failure is None and outcome is not None:
                self._fold_pipeline_outcome(stage, unit, outcome, bound)
                continue
            if isinstance(failure, (Cancelled, LimitExceeded)):
                raise failure
            self._fold_pipeline_failure(stage, unit, bound, failure)

    def _run_one_pipeline_unit(
        self,
        execution: CapabilityExecution,
        capability: workflow.Capability,
        stage: dict[str, Any],
        unit: dict[str, Any],
    ) -> None:
        assert execution.pipeline_binder is not None
        bound: BoundUnitPipeline | None = None
        try:
            binding = execution.pipeline_binder(
                self.subject,
                self.run,
                capability,
                stage,
                unit,
            )
            if isinstance(binding, DeterministicUnitResult):
                # A unit of this capability that needs no model proposal.
                self._set_deterministic_unit(stage, unit, binding)
                return
            if not isinstance(binding, BoundUnitPipeline):
                raise ValueError(
                    "Pipeline binder must return BoundUnitPipeline or "
                    "DeterministicUnitResult."
                )
            bound = binding
            assert self.unit_pipeline is not None
            outcome = self.unit_pipeline.run(
                bound.request,
                context_provider=bound.context_provider,
                context_identity_provider=bound.context_identity_provider,
                target=bound.target,
                approval_provider=bound.approval_provider,
                readiness_provider=bound.readiness_provider,
                on_manifest_persisted=self._reference_recorder(unit, "context_manifest"),
                on_manifest_resolved=self._narrate_context(capability),
                on_proposal_persisted=self._reference_recorder(unit, "proposal_sidecar"),
                on_receipt_persisted=self._reference_recorder(unit, "receipt_sidecar"),
                on_worker_repaired=self._narrate_repair(),
                on_worker_completed=self._narrate_completion(capability),
            )
            self._fold_pipeline_outcome(stage, unit, outcome, bound)
        except (Cancelled, LimitExceeded):
            raise
        except Exception as error:
            self._fold_pipeline_failure(stage, unit, bound, error)

    def _reference_recorder(
        self, unit: dict[str, Any], field: str
    ) -> Callable[[Mapping[str, str]], None]:
        def persist(reference: Mapping[str, str]) -> None:
            unit[field] = dict(reference)
            self.runtime.save()

        return persist

    def _narrate_context(
        self, capability: workflow.Capability
    ) -> Callable[[Any], None]:
        """Say what a unit is about to read, right before its model call.

        Fired from the pipeline only when a model call is actually happening
        (never on a reused proposal), so this is the one place a long,
        single-unit capability gets an opening line instead of going silent
        until it settles. A real conversational turn (``say``), not a log
        entry: it belongs in the transcript the auditor reads back, not only
        in a live status strip that vanishes once the unit settles.
        """

        def resolved(manifest: Any) -> None:
            text = narration.context_note(manifest, self.subject, label=capability.title)
            if text:
                narration.say(self.run, self.runtime.emit, text)

        return resolved

    def _narrate_repair(self) -> Callable[[int, tuple[str, ...]], None]:
        def repaired(_attempt: int, errors: tuple[str, ...]) -> None:
            narration.say(
                self.run,
                self.runtime.emit,
                narration.repair_note(errors[0] if errors else ""),
            )

        return repaired

    def _narrate_completion(
        self, capability: workflow.Capability
    ) -> Callable[[Mapping[str, Any]], None]:
        """Say what a unit actually produced, right as its call returns.

        The live progress checklist reading the same response as it streams
        disappears the moment the call settles; this is its permanent
        counterpart, so what it showed is not lost the instant it is done.
        """

        def completed(proposal: Mapping[str, Any]) -> None:
            text = narration.completion_note(capability.id, proposal)
            if text:
                narration.say(self.run, self.runtime.emit, text)

        return completed

    def _fold_pipeline_failure(
        self,
        stage: dict[str, Any],
        unit: dict[str, Any],
        bound: BoundUnitPipeline | None,
        error: BaseException | None,
    ) -> None:
        if isinstance(error, UnitPipelineConflict):
            self._record_pipeline_error_references(unit, error)
            override = (
                bound.conflict_handler(stage, unit, error)
                if bound is not None and bound.conflict_handler is not None
                else None
            )
            if override is not None:
                status, message = override
                self.set_unit(stage, unit, status, error=message)
                return
            self.set_unit(stage, unit, "conflict", error=str(error))
            return
        self.set_unit(
            stage, unit, "failed", error=str(error) if error else "Unit did not settle."
        )

    def _run_deterministic_units(
        self,
        execution: CapabilityExecution,
        capability: workflow.Capability,
        stage: dict[str, Any],
        units: list[dict[str, Any]],
    ) -> None:
        assert execution.deterministic_executor is not None
        for unit in sorted(units, key=lambda item: str(item["id"])):
            if unit.get("status") in {"succeeded", "skipped"}:
                continue
            self.runtime.checkpoint()
            self.set_unit(stage, unit, "running")
            try:
                result = execution.deterministic_executor(
                    self.subject,
                    self.run,
                    capability,
                    stage,
                    unit,
                )
                if not isinstance(result, DeterministicUnitResult):
                    raise ValueError(
                        "Deterministic executor must return DeterministicUnitResult."
                    )
                self._set_deterministic_unit(stage, unit, result)
            except (Cancelled, LimitExceeded):
                raise
            except Exception as error:
                self.set_unit(stage, unit, "failed", error=str(error))

    def _set_deterministic_unit(
        self,
        stage: dict[str, Any],
        unit: dict[str, Any],
        result: DeterministicUnitResult,
    ) -> None:
        self.set_unit(
            stage,
            unit,
            result.status,
            error=result.error,
            result_refs=list(result.result_refs),
        )

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
        override = (
            bound.on_committed(stage, unit, outcome)
            if bound.on_committed is not None
            else None
        )
        if override is not None:
            if not isinstance(override, DeterministicUnitResult):
                raise ValueError(
                    "A committed-unit override must be a DeterministicUnitResult."
                )
            self._set_deterministic_unit(stage, unit, override)
            return
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
        previous = stage.get("status")
        stage["status"] = status
        if status == "running":
            stage["started_at"] = stage.get("started_at") or self.runtime.utcnow()
            # Model waits retain the current activity.  Publish a new activity
            # at each workflow boundary so a completed legacy task cannot keep
            # labelling later capability work (for example, joins while
            # analysis definitions are being generated).
            units = stage.get("units") or []
            stage_id = str(stage.get("id") or stage.get("capability") or "workflow")
            self.runtime.set_activity(
                f"workflow.{stage_id}",
                str(stage.get("title") or "Workflow"),
                current=0,
                total=len(units) if units else None,
                task_id=f"workflow:{stage_id}",
            )
        if status in {
            "succeeded",
            "failed",
            "review_required",
            "blocked",
            "skipped",
            "cancelled",
        }:
            stage["finished_at"] = self.runtime.utcnow()
        # Stage boundaries are the only points where a domain-neutral scheduler
        # knows something a reader would want narrated, so they are where the
        # live progress log is written from.
        if previous != status:
            if status == "running":
                narration.note(
                    self.run,
                    self.runtime.emit,
                    narration.stage_started(stage),
                    kind="stage_started",
                    stage_id=str(stage.get("id") or ""),
                )
            elif status != "queued":
                narration.note(
                    self.run,
                    self.runtime.emit,
                    narration.stage_settled(stage),
                    kind="stage_settled",
                    stage_id=str(stage.get("id") or ""),
                )
                self._project_milestone(stage, status)
        self.runtime.save()
        self.runtime.emit("stage_update", {"stage": copy.deepcopy(stage)})

    def _next_stage_title(self, stage: dict[str, Any]) -> str:
        """The stage the run moves on to once ``stage`` has settled."""
        stages = list((self.run.get("workflow") or {}).get("stages") or [])
        identifiers = [str(item.get("id") or "") for item in stages]
        try:
            index = identifiers.index(str(stage.get("id") or ""))
        except ValueError:
            return ""
        for item in stages[index + 1:]:
            if str(item.get("status") or "") in {"queued", "running"}:
                return str(item.get("title") or "").strip()
        return ""

    def _project_milestone(self, stage: dict[str, Any], status: str) -> None:
        if self.milestone_projector is None:
            return
        if status in {"blocked", "cancelled"}:
            return
        if status == "failed" and not any(
            unit.get("status") in {"succeeded", "skipped"}
            for unit in stage.get("units") or []
        ):
            return
        capability = self.registry.get(str(stage.get("capability") or ""))
        try:
            projection = self.milestone_projector(
                self.subject, self.run, capability, stage
            )
            if not projection:
                return
            entry = narration.milestone(
                self.run,
                self.runtime.emit,
                capability=capability.id,
                stage_id=str(stage.get("id") or capability.id),
                status=str(projection.get("status") or status),
                headline=str(projection.get("headline") or capability.title),
                summary=str(projection.get("summary") or ""),
                metrics=list(projection.get("metrics") or []),
                highlights=list(projection.get("highlights") or []),
                artifact_refs=list(projection.get("artifact_refs") or []),
            )
            # A milestone that was already on the record is a replay, not new
            # work, so it must not make the agent announce a handoff twice.
            if entry is not None:
                handoff = narration.stage_handoff(stage, self._next_stage_title(stage))
                if handoff:
                    narration.say(self.run, self.runtime.emit, handoff)
        except Exception as error:
            # Milestones are a read-only conversational projection. A summary
            # bug must never turn successfully committed audit work into a
            # failed run.
            self.runtime.emit(
                "milestone_projection_failed",
                {
                    "stage_id": str(stage.get("id") or ""),
                    "capability": capability.id,
                    "error": str(error),
                },
            )

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
        default_next = unsettled_capabilities(stages)
        projection = (
            self.finish_evaluator(self.subject, state, stages)
            if self.finish_evaluator is not None
            else FinishProjection(next_outcomes=default_next)
        )
        terminal = projection.terminal_status or fold_terminal_status(stages)
        state["next_outcomes"] = list(dict.fromkeys(projection.next_outcomes))
        self.run["summary_markdown"] = projection.summary_markdown or narration.summary_markdown(
            "Workflow",
            [
                ("Requested", [
                    narration.humanize(item)
                    for item in state.get("requested_outcomes") or []
                ]),
                ("Failed or conflicting units", failed),
                ("Open workflow units", open_units),
            ],
        )
        if terminal in {"failed", "completed_with_failures"} and not self.run.get("error"):
            self.run["error"] = first_unit_error(
                stages, "One or more workflow units failed."
            )
        # The closing turn is composed against the status about to be published
        # and written before it, so a client reacting to the run finishing never
        # sees a terminal run that has not said anything yet.
        narration.say(
            self.run, self.runtime.emit, narration.closing_text(self.run, terminal)
        )
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
