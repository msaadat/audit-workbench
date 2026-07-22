"""Outcome-driven, thin orchestration for composable audit workflows."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid

from .. import (
    dashboard,
    data_tests,
    doc_tests,
    documents,
    findings,
    rcm_execution,
    report,
    templates_store,
    working_papers,
)
from ..workspace_transactions import canonical_sha1, mutate, parent_hashes
from ..workspaces import (
    OBSERVATION_DISPOSITIONS,
    Workspace,
    WorkspaceConflict,
    WorkspaceError,
    load_workspace,
    slugify,
)
from . import audit_capabilities, audit_workers, context_bundles, store, workflow
from .base import Cancelled, LimitExceeded
from .action_runner import ActionRunner
from .context import ContextResolver, apm_document_methodology_scope
from .executors import EXECUTORS
from .executors.planning import (
    AUDITOR_EDIT_PRESERVED,
    ApmExecutorTarget,
)
from .runtime import (
    RunRuntime,
    UnitPipeline,
    UnitPipelineConflict,
    UnitPipelineRequest,
    UnitSidecarStore,
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


def _local_resolution(command: dict) -> dict | None:
    """Route a command to capability outcomes without calling the model.

    Tried in confidence order — explicit outcomes (follow-up/retry runs), then
    goal templates, then command phrasing, then markers that mean "this is an
    isolated mutation, hand it to ActionRunner". Returning None is what sends
    the command to the LLM router, so every phrase added here removes a model
    call from the common path.
    """
    direct = command.get("requested_outcomes")
    if isinstance(direct, list) and direct:
        requested = [str(item) for item in direct]
        # Validate through the local registry before the run is launched.
        audit_capabilities.REGISTRY.closure(requested)
        return {
            "route": "workflow", "requested_outcomes": requested,
            "objective": str(command.get("text") or "Continue the requested audit outcomes.").strip(),
            "target_refs": [str(item) for item in command.get("target_refs") or ["workspace:current"]],
            "refresh_policy": str(command.get("refresh_policy") or "missing_or_stale"),
            "action_intent": None, "constraints": [str(item) for item in command.get("constraints") or []],
            "needs_clarification": False, "clarification": None,
        }
    template = str(command.get("goal_template") or "")
    if template in {"data_analysis", "document_testing"}:
        return {
            "route": "generic_action", "requested_outcomes": [],
            "objective": str(command.get("text") or "").strip(), "target_refs": [],
            "refresh_policy": "missing_or_stale", "action_intent": template,
            "constraints": [], "needs_clarification": False, "clarification": None,
        }
    outcomes = audit_capabilities.outcomes_for_template(template)
    if outcomes is not None:
        refresh_policy = "force" if template in {"planning", "apm_only"} else "missing_or_stale"
        return {
            "route": "workflow", "requested_outcomes": outcomes,
            "objective": str(command.get("text") or template.replace("_", " ")).strip(),
            "target_refs": ["workspace:current"], "refresh_policy": refresh_policy,
            "action_intent": None, "constraints": [], "needs_clarification": False,
            "clarification": None,
        }
    text = str(command.get("text") or "").casefold()
    mappings = [
        (("full audit", "complete the audit", "end-to-end audit", "end to end audit"), audit_capabilities.FULL_AUDIT_OUTCOMES),
        (("draft the apm", "update the apm", "generate apm", "generate the apm", "audit planning memorandum"), ["planning.apm_ready"]),
        (("generate the rcm", "draft the rcm", "update the rcm", "risk and control matrix"), ["planning.rcm_ready"]),
        (("testing procedures", "planned procedures", "planned tests"), ["planning.planned_tests_ready"]),
        (("translate planned", "executable tests", "execution definitions"), ["fieldwork.definitions_ready"]),
        (("run the rcm tests", "execute the rcm tests", "run rcm tests", "execute planned tests"), ["fieldwork.executed", "results.rolled_up"]),
        (("draft eligible findings", "draft findings"), ["findings.drafted"]),
        (("generate the report", "draft the report", "audit report"), ["report.working_draft"]),
    ]
    for phrases, requested in mappings:
        if any(phrase in text for phrase in phrases):
            # "run planned tests" is execution, even though it contains the
            # narrower authorship phrase.
            if requested == ["planning.planned_tests_ready"] and any(word in text for word in ("run ", "execute ")):
                continue
            return {
                "route": "workflow", "requested_outcomes": list(requested),
                "objective": str(command.get("text") or "").strip(),
                "target_refs": ["workspace:current"], "refresh_policy": "missing_or_stale",
                "action_intent": None, "constraints": [], "needs_clarification": False,
                "clarification": None,
            }
    generic_markers = (
        "join ", "rename ", "remove ", "delete ", "add a finding", "create a finding",
        "validate ", "validation", "check report quality", "pin ", "rerun ",
        "analyze ", "analyse ", "analysis", "upload ", "attach ", "detach ",
        "document test", "prepare report", "finding", " undo ", "review the apm",
    )
    if any(marker in text for marker in generic_markers):
        return {
            "route": "generic_action", "requested_outcomes": [],
            "objective": str(command.get("text") or "").strip(), "target_refs": [],
            "refresh_policy": "missing_or_stale", "action_intent": "isolated_mutation",
            "constraints": [], "needs_clarification": False, "clarification": None,
        }
    return None


def initialize_known_workflow(workspace: Workspace, run: dict) -> bool:
    """Persist template/local routing before the worker starts (no LLM call)."""
    resolution = _local_resolution(run.get("command") or {})
    if resolution is None:
        return False
    if resolution.get("route") == "generic_action":
        run["engine"] = store.ACTION_ENGINE
        run["command_route"] = resolution
        store.save_run(workspace, run)
        return False
    if resolution.get("route") != "workflow":
        return False
    run["engine"] = store.WORKFLOW_ENGINE
    _install_resolution(workspace, run, resolution)
    store.save_run(workspace, run)
    return True


def _explanation(resolved: list[str], stages: list[dict], reused: list[str], requested: list[str]) -> str:
    running = [stage["capability"] for stage in stages]
    automatically_added = [item for item in resolved if item not in requested]
    parts = [f"Requested outcome(s): {', '.join(requested)}."]
    if automatically_added:
        parts.append("Added prerequisite(s): " + ", ".join(automatically_added) + ".")
    if reused:
        parts.append("Reusing current capability output(s): " + ", ".join(reused) + ".")
    if running:
        parts.append("Running in dependency order: " + " → ".join(running) + ".")
    return " ".join(parts)


def _install_resolution(workspace: Workspace, run: dict, resolution: dict) -> None:
    """Materialize the capability graph onto the run and size its budgets.

    Called either before the worker thread starts (local routing) or from
    `execute` after the LLM router answers. Promotes the run to schema v3 —
    from that point `run["workflow"]` is the authoritative execution record and
    the v2 action ledger stays empty.
    """
    scope = {
        "target_refs": list(resolution.get("target_refs") or ["workspace:current"]),
        "refresh_policy": resolution.get("refresh_policy") or "missing_or_stale",
        "permission_mode": run.get("mode") == "permission",
    }
    requested = list(resolution.get("requested_outcomes") or [])
    resolved, stages, reused = workflow.materialize(
        audit_capabilities.REGISTRY, workspace, requested, scope
    )
    maximum_units = int(run.get("limits", {}).get("max_units_per_stage") or 250)
    oversized = next(
        (stage for stage in stages if len(stage.get("units") or []) > maximum_units),
        None,
    )
    if oversized is not None:
        raise LimitExceeded(
            f"Stage '{oversized['title']}' requires {len(oversized['units'])} units, "
            f"above its {maximum_units}-unit limit."
        )
    explanation = _explanation(resolved, stages, reused, requested)
    run["schema_version"] = 3
    # Budgets are sized from the work actually discovered, not a fixed ceiling:
    # an engagement with 40 RCM rows legitimately needs far more model turns
    # than one with 3. _refresh_dynamic_limits re-runs this before every stage
    # because the RCM grows while the run is in flight.
    planned_count = sum(len(row.get("planned_tests") or []) for row in workspace.rcm)
    qa_pairs = sum(
        len(item.get("document_ids") or [])
        for summary in doc_tests.list_tests(workspace)
        for item in doc_tests.load_test(workspace, summary["id"]).get("items") or []
        if summary.get("kind") == "qa"
    )
    eligible_findings = sum(
        item.get("status") == "disposed"
        and item.get("disposition") in ELIGIBLE_DISPOSITIONS
        for item in workspace.observations
    )
    calculated_model_turns = (
        20 + 4 * len(workspace.rcm) + 4 * planned_count
        + 2 * qa_pairs + 2 * eligible_findings
    )
    run.setdefault("limits", {}).update(
        max_llm_concurrency=int(run.get("limits", {}).get("max_llm_concurrency") or 4),
        max_compute_concurrency=int(run.get("limits", {}).get("max_compute_concurrency") or 2),
        max_model_turns=calculated_model_turns,
        max_execution_attempts=2,
        max_units_per_stage=maximum_units,
        max_estimated_prompt_tokens=max(
            int(run.get("limits", {}).get("max_estimated_prompt_tokens") or 0),
            calculated_model_turns * 10_000,
        ),
        max_completion_tokens=max(
            int(run.get("limits", {}).get("max_completion_tokens") or 0),
            calculated_model_turns * 4_000,
        ),
    )
    run["goal"] = {
        "objective": resolution.get("objective") or (run.get("command") or {}).get("text") or "",
        "constraints": list(resolution.get("constraints") or []),
        "completion_criteria": requested,
    }
    run["workflow"] = {
        "definition": workflow.WORKFLOW_DEFINITION,
        "revision": 1,
        "route": "workflow",
        "requested_outcomes": requested,
        "target_refs": scope["target_refs"],
        "refresh_policy": scope["refresh_policy"],
        "workflow_explanation": explanation,
        "next_outcomes": [],
        "pending_checkpoint": None,
        "resolved_capabilities": resolved,
        "reused_capabilities": reused,
        "workspace_revision": workspace.revision,
        "state_at_resolution": audit_capabilities.workflow_state(workspace, scope),
        "stages": stages,
        "legacy_adoptions": [],
    }
    run["workflow_explanation"] = explanation
    run["command"]["status"] = "resolved"
    # Retain the compact planning-change counters used by existing activity
    # views while the workflow ledger remains the authoritative execution
    # record.
    run.setdefault(
        "planning_changes",
        {
            "apm_updated": 0,
            "apm_proposed": 0,
            "rcm_created": 0,
            "rcm_updated": 0,
            "rcm_preserved": 0,
            "planned_test_created": 0,
            "planned_test_updated": 0,
            "planned_test_preserved": 0,
        },
    )


class WorkflowRunner(ActionRunner):
    """Generic scheduler backed by the audit capability registry."""

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

    def execute(self) -> None:
        """Resolve the command to outcomes, then run the capability graph.

        Three exits before any audit work happens: an isolated mutation is
        handed down to ActionRunner, an unanswerable request completes
        with an explanation, and anything else installs a capability graph.
        Stages then run strictly in dependency order — parallelism lives
        inside a stage, never across them.
        """
        if not self.run.get("started"):
            self.mark_started()
        try:
            if not self.run.get("workflow"):
                resolution = self._resolve()
                if resolution.get("route") == "generic_action":
                    self.run["engine"] = store.ACTION_ENGINE
                    self.run["command_route"] = resolution
                    self.run["schema_version"] = 2
                    self.save()
                    ActionRunner.execute(self)
                    return
                if resolution.get("route") in {"question", "unsupported"}:
                    self.run["summary_markdown"] = resolution.get("clarification") or (
                        "This request is not available as an audit workflow."
                    )
                    self.run["command"]["status"] = "completed"
                    self.mark_finished()
                    self.set_status("completed_with_open_items")
                    return
                _install_resolution(self.ws, self.run, resolution)
                self.save()
                self.emit("workflow_resolved", {"workflow": self.run["workflow"]})
                self.emit("workflow_explanation", {"text": self.run["workflow_explanation"]})
            # Requeue units interrupted mid-flight, and backfill workflow
            # hashes onto artifacts created before this run (or by hand), so
            # readiness does not treat pre-existing work as missing.
            workflow.recovery(self.run["workflow"])
            self._adopt_legacy()
            self.save()
            self.set_status("executing")
            scheduled = {
                stage["capability"]: stage
                for stage in self.run["workflow"].get("stages") or []
            }
            for stage in self.run["workflow"].get("stages") or []:
                if stage.get("status") in {"succeeded", "skipped"}:
                    continue
                # Reload between stages: the auditor, other tabs, and the
                # previous stage all write to the workspace, and unit expansion
                # below must see the current state.
                self.checkpoint()
                self._refresh_workspace()
                self._refresh_dynamic_limits()
                capability = audit_capabilities.REGISTRY.get(stage["capability"])
                if (
                    capability.id == "findings.drafted"
                    and self.run.get("mode") == "permission"
                ):
                    self._observation_checkpoint()
                    self._refresh_workspace()
                # Dependencies that may be only partially satisfied. Fieldwork
                # is naturally ragged — one unusable planned test or one
                # evidence-blocked document test must not sink the stages
                # behind it, so these edges tolerate a review_required parent.
                partial_dependencies = {
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
                blocking = [
                    dependency
                    for dependency in capability.depends_on
                    if dependency in scheduled
                    and scheduled[dependency].get("status") not in {"succeeded", "skipped"}
                    and dependency not in partial_dependencies.get(capability.id, set())
                ]
                if blocking:
                    units = self._ensure_stage_units(stage)
                    reason = "Blocked by prerequisite stage(s): " + ", ".join(blocking)
                    for unit in units:
                        if unit.get("status") == "queued":
                            self._set_unit(stage, unit, "blocked", error=reason)
                    self._stage_event(stage, "blocked")
                    self.emit(
                        "stage_summary",
                        {"stage_id": stage["id"], "status": "blocked", "counts": workflow.stage_counts(stage)},
                    )
                    continue
                self._run_stage(stage)
            self._finish_workflow()
        except Cancelled:
            self._cancel_remaining()
            self.mark_finished()
            self.run["command"]["status"] = "cancelled"
            self.set_status("cancelled")
        except (LimitExceeded, WorkspaceConflict) as error:
            self.run["error"] = str(error)
            if isinstance(error, WorkspaceConflict):
                self._mark_running_conflict(str(error))
            self.mark_finished()
            self.run["command"]["status"] = "failed"
            self.set_status("failed")
        except Exception as error:
            self.run["error"] = str(error)
            self._mark_running_failed(str(error))
            self.mark_finished()
            self.run["command"]["status"] = "failed"
            self.set_status("failed")

    # --------------------------------------------------------- resolution
    def _resolve(self) -> dict:
        local = _local_resolution(self.run.get("command") or {})
        if local is not None:
            return local
        self.set_status("interpreting")
        state = audit_capabilities.workflow_state(self.ws)
        bundle = context_bundles.command_router(
            self.run.get("command") or {}, state,
            [item.id for item in audit_capabilities.REGISTRY.all()],
            permission_mode=self.run["mode"],
        )
        resolution = audit_workers.resolve_command(
            self, bundle, {item.id for item in audit_capabilities.REGISTRY.all()}
        )
        self.run["partial_resolution"] = resolution
        self.save()
        if resolution.get("needs_clarification"):
            answer = self._clarification(str(resolution.get("clarification") or "Please clarify the intended audit outcome."))
            command = dict(self.run.get("command") or {})
            command["text"] = f"{command.get('text') or ''}\n\nClarification: {answer}".strip()
            state = audit_capabilities.workflow_state(load_workspace(self.ws.id))
            bundle = context_bundles.command_router(
                command, state, [item.id for item in audit_capabilities.REGISTRY.all()],
                permission_mode=self.run["mode"],
            )
            resolution = audit_workers.resolve_command(
                self, bundle, {item.id for item in audit_capabilities.REGISTRY.all()}
            )
            if resolution.get("needs_clarification"):
                raise WorkspaceError("The command still needs clarification after the supplied answer.")
        return resolution

    def _clarification(self, prompt: str) -> str:
        interaction = next(
            (item for item in self.run.get("interactions") or [] if item.get("type") == "clarification" and item.get("status") == "pending"),
            None,
        )
        if interaction is None:
            interaction = {
                "id": f"int_{uuid.uuid4().hex[:12]}", "action_id": "workflow:resolver",
                "type": "clarification", "prompt": prompt, "options": [],
                "payload": {"original_command": (self.run.get("command") or {}).get("text")},
                "policy_reason": "The answer materially changes the requested audit outcome.",
                "status": "pending", "response": None, "actor": None,
                "created_at": store.utcnow(), "resolved_at": None,
            }
            self.run.setdefault("interactions", []).append(interaction)
            self.save()
            self.emit("checkpoint_request", {"interaction": interaction})
        response = self._wait_interaction_response(interaction)
        text = str(response.get("text") or "").strip()
        if not text:
            raise WorkspaceError("A clarification response is required.")
        self._resolve_interaction_record(interaction, response)
        return text

    # ------------------------------------------------------------ ledger
    def _refresh_workspace(self) -> None:
        previous = int(self.run["workflow"].get("workspace_revision") or 0)
        self.ws = load_workspace(self.ws.id)
        self.run["workflow"]["workspace_revision"] = self.ws.revision
        if self.ws.revision != previous:
            self.emit(
                "workspace_revision",
                {"previous_revision": previous, "workspace_revision": self.ws.revision},
            )

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

    def _stage_event(self, stage: dict, status: str) -> None:
        stage["status"] = status
        if status == "running":
            stage["started_at"] = stage.get("started_at") or store.utcnow()
        if status in {"succeeded", "failed", "review_required", "blocked", "skipped", "cancelled"}:
            stage["finished_at"] = store.utcnow()
        self.save()
        self.emit("stage_update", {"stage": copy.deepcopy(stage)})

    def _ensure_stage_units(self, stage: dict) -> list[dict]:
        capability = audit_capabilities.REGISTRY.get(stage["capability"])
        scope = {
            "target_refs": self.run["workflow"].get("target_refs") or [],
            "refresh_policy": self.run["workflow"].get("refresh_policy") or "missing_or_stale",
        }
        specs = capability.expand_units(self.ws, scope)
        maximum = int((self.run.get("limits") or {}).get("max_units_per_stage") or 250)
        if len(specs) > maximum:
            raise LimitExceeded(
                f"Stage '{stage['title']}' requires {len(specs)} units, above its {maximum}-unit limit."
            )
        units = stage.setdefault("units", [])
        existing = {unit["id"]: unit for unit in units}
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
            self.save()
            self.emit("stage_update", {"stage": copy.deepcopy(stage)})
        return stage["units"]

    # ------------------------------------------------------------- stages
    def _run_stage(self, stage: dict) -> None:
        """Re-expand, dispatch to the capability's handler, then fold statuses.

        Units are re-expanded here rather than trusted from resolution time,
        because upstream stages have since changed the workspace this stage
        fans out over.
        """
        self._stage_event(stage, "running")
        units = self._ensure_stage_units(stage)
        capability = stage["capability"]
        # Nothing to do: either the capability is genuinely satisfied, or its
        # inputs never materialized and downstream stages must see it blocked.
        if not units:
            readiness = audit_capabilities.REGISTRY.get(capability).readiness(self.ws, {})
            self._stage_event(stage, "succeeded" if readiness.satisfied else "blocked")
            return
        handlers = {
            "planning.context_ready": self._planning_basis,
            "planning.apm_ready": self._apm,
            "planning.rcm_ready": self._rcm,
            "planning.planned_tests_ready": self._planned_tests,
            "fieldwork.definitions_ready": self._definitions,
            "fieldwork.executed": self._executions,
            "results.rolled_up": self._rollup,
            "findings.drafted": self._finding_drafts,
            "working_papers.generated": self._working_papers,
            "dashboard.curated": self._dashboard,
            "report.working_draft": self._report,
            "audit.verified": self._verify,
        }
        handlers[capability](stage, units)
        # Stage status is derived, never set by a handler. `review_required` is
        # a distinct outcome from failure: the work landed but needs an
        # auditor, which is what _finish_workflow turns into next_outcomes.
        statuses = {unit["status"] for unit in units}
        if statuses <= {"succeeded", "skipped"}:
            final = "succeeded"
        elif "failed" in statuses or "conflict" in statuses:
            final = "failed"
        elif statuses & {"blocked", "awaiting_input", "awaiting_confirmation"}:
            final = "review_required"
        else:
            final = "failed"
        self._stage_event(stage, final)
        self.emit("stage_summary", {"stage_id": stage["id"], "status": final, "counts": workflow.stage_counts(stage)})

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

    def _apm(self, stage: dict, units: list[dict]) -> None:
        unit = units[0]
        self._set_unit(stage, unit, "running")
        task = self.add_task("apm", "workflow:apm", "Audit planning memorandum")
        try:
            expected_context = parent_hashes(self.ws, ["planning:context"])
            basis = self.run.get("planning_basis") or {
                "planning": self.ws.planning, "tables": [], "documents": [], "methodology": []
            }
            capability = audit_capabilities.REGISTRY.get(stage["capability"])
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

            def record_reference(field):
                def record(reference):
                    unit[field] = dict(reference)
                    self.save()

                return record

            pipeline = UnitPipeline(
                runtime=self.runtime,
                gateway=self.model_gateway,
                workers=WORKERS,
                executors=EXECUTORS,
                sidecars=UnitSidecarStore(self.ws, self.run["id"]),
            )
            outcome = pipeline.run(
                UnitPipelineRequest(
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
                on_manifest_persisted=record_reference("context_manifest"),
                on_proposal_persisted=record_reference("proposal_sidecar"),
                on_receipt_persisted=record_reference("receipt_sidecar"),
            )
            if outcome.status == "approval_rejected":
                self._set_unit(
                    stage,
                    unit,
                    "blocked",
                    error="The APM proposal was not approved.",
                )
                return
            self.ws = target.workspace
            if outcome.proposal_reused:
                self.emit(
                    "proposal_reused",
                    {
                        "stage_id": stage["id"],
                        "unit_id": unit["id"],
                        "sidecar": dict(outcome.proposal_reference),
                    },
                )
            if outcome.proposal_reuse_rejection_reasons:
                self.emit(
                    "proposal_reuse_rejected",
                    {
                        "stage_id": stage["id"],
                        "unit_id": unit["id"],
                        "reasons": list(outcome.proposal_reuse_rejection_reasons),
                    },
                )
            self.run["planning_changes"]["apm_updated"] += 1
            self.record_artifact("planning", "apm", "planning:apm", "updated", task)
            self._set_unit(stage, unit, "succeeded", result_refs=["planning:apm"])
        except (Cancelled, LimitExceeded):
            raise
        except UnitPipelineConflict as error:
            if error.manifest_reference:
                unit["context_manifest"] = dict(error.manifest_reference)
            if error.proposal_reference:
                unit["proposal_sidecar"] = dict(error.proposal_reference)
            if error.receipt_reference:
                unit["receipt_sidecar"] = dict(error.receipt_reference)
            if error.proposal_reuse_rejection_reasons:
                self.emit(
                    "proposal_reuse_rejected",
                    {
                        "stage_id": stage["id"],
                        "unit_id": unit["id"],
                        "reasons": list(error.proposal_reuse_rejection_reasons),
                    },
                )
            if str(error) == AUDITOR_EDIT_PRESERVED:
                self.run.setdefault("planning_revisions", []).append(
                    {
                        "kind": "apm",
                        "status": "proposed",
                        "sidecar": dict(error.proposal_reference or {}),
                    }
                )
                self.run["planning_changes"]["apm_proposed"] += 1
                self._set_unit(
                    stage,
                    unit,
                    "awaiting_confirmation",
                    error="Auditor-owned APM was preserved.",
                )
            else:
                self._set_unit(stage, unit, "conflict", error=str(error))
        except WorkspaceConflict as error:
            self._set_unit(stage, unit, "conflict", error=str(error))
        except Exception as error:
            self._set_unit(stage, unit, "failed", error=str(error))

    def _rcm(self, stage: dict, units: list[dict]) -> None:
        unit = units[0]
        self._set_unit(stage, unit, "running")
        try:
            expected_apm = parent_hashes(self.ws, ["planning:apm"])
            basis = self.run.get("planning_basis") or {"planning": self.ws.planning}
            template = templates_store.get_template(self.ws, "rcm")["markdown"]
            cached = None
            if unit.get("proposal_sidecar"):
                cached = store.read_sidecar(
                    self.ws, self.run["id"], unit["proposal_sidecar"]
                )
                self.emit(
                    "proposal_reused",
                    {"stage_id": stage["id"], "unit_id": unit["id"], "sidecar": unit["proposal_sidecar"]},
                )
            if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
                payload = cached
            else:
                bundle = context_bundles.rcm(
                    basis, template=template,
                    apm_markdown=str(self.ws.planning.get("apm_markdown") or ""),
                    current_rows=self.ws.rcm,
                )
                payload = self.llm_json(
                    audit_workers.prompts.RCM_SYSTEM,
                    bundle.serialized(),
                    activity={"artifact_refs": ["planning:apm"], "context_metrics": bundle.metrics()},
                )
                error = self._rcm_quality(payload, {str(row.get("id")) for row in self.ws.rcm})
                if error:
                    payload = self.llm_json(
                        audit_workers.prompts.RCM_SYSTEM,
                        bundle.serialized() + f"\n\nCorrect this quality error: {error}",
                        activity={"artifact_refs": ["planning:apm"], "context_metrics": bundle.metrics()},
                    )
                    error = self._rcm_quality(payload, {str(row.get("id")) for row in self.ws.rcm})
                    if error:
                        raise WorkspaceError(f"The RCM draft failed the quality gate: {error}")
                unit["proposal_sidecar"] = store.write_sidecar(
                    self.ws, self.run["id"], payload
                )
                self.save()
            proposed = []
            for raw in payload.get("rows") or []:
                spec = dict(raw)
                spec["semantic_id"] = f"rcm:{slugify(spec.get('process'))}:{slugify(spec.get('risk'))}"
                proposed.append(self.proposal_item(str(spec.get("risk")), "Risk/control matrix revision.", spec))
            task = self.add_task("rcm", "workflow:rcm", "Risk and control matrix")
            accepted = self.request_approval("rcm", task, proposed) if self.run["mode"] == "permission" else proposed
            refs = []
            for proposal in accepted:
                self.checkpoint()
                spec = proposal["spec"]
                def commit(fresh: Workspace):
                    existing, ambiguous = self._match_rcm_revision(
                        spec,
                        spec["semantic_id"],
                        workspace=fresh,
                    )
                    if ambiguous:
                        raise WorkspaceError(
                            f"Ambiguous RCM revision for '{spec.get('risk')}'."
                        )
                    if (
                        existing
                        and existing.get("created_by") != "agent"
                        and self.run["mode"] != "permission"
                    ):
                        return existing, "preserved"
                    if existing:
                        return (
                            fresh.update_rcm(
                                existing["id"],
                                {
                                    **{
                                        key: spec.get(key)
                                        for key in (
                                            "process", "risk", "risk_rating", "assertion",
                                            "control", "control_type", "test_procedure",
                                        )
                                    },
                                    "workflow_parent_sha1": audit_capabilities.apm_sha1(fresh),
                                },
                                agent=True,
                            ),
                            "updated",
                        )
                    return (
                        fresh.add_rcm(
                            {
                                **spec,
                                "agent_run_id": self.run["id"],
                                "workflow_parent_sha1": audit_capabilities.apm_sha1(fresh),
                            }
                        ),
                        "created",
                    )

                committed = mutate(
                    self.ws,
                    commit,
                    expected_parents=expected_apm,
                )
                self.ws = committed.workspace
                item, action = committed.value
                if action == "preserved":
                    self.warn(f"Preserved auditor-owned RCM row '{item['id']}'.")
                    self.run["planning_changes"]["rcm_preserved"] += 1
                    continue
                self.run["planning_changes"][f"rcm_{action}"] += 1
                refs.append(self.record_artifact("rcm", item["id"], item["semantic_id"], action, task))
            self._set_unit(stage, unit, "succeeded", result_refs=refs)
        except (Cancelled, LimitExceeded):
            raise
        except WorkspaceConflict as error:
            self._set_unit(stage, unit, "conflict", error=str(error))
        except Exception as error:
            self._set_unit(stage, unit, "failed", error=str(error))

    def _planned_tests(self, stage: dict, units: list[dict]) -> None:
        candidates = self._parallel_candidates(
            stage, units,
            lambda unit: audit_workers.planned_tests(
                self,
                context_bundles.planned_test(
                    self.ws,
                    next(row for row in self.ws.rcm if row["id"] == unit["parent_refs"][0].split(":", 1)[1]),
                    self.run.get("planning_basis"),
                ),
                unit["parent_refs"][0].split(":", 1)[1],
            ),
        )
        proposals = []
        for unit, value in candidates:
            for spec in value:
                spec.setdefault(
                    "methodology_refs",
                    [
                        {
                            key: item[key]
                            for key in (
                                "pack_id", "pack_name", "version", "sha1",
                                "section", "citation",
                            )
                            if key in item
                        }
                        for item in (self.run.get("planning_basis") or {}).get("methodology") or []
                    ],
                )
                proposals.append(self.proposal_item(spec["title"], f"Planned test for {spec['rcm_id']}.", {**spec, "_unit_id": unit["id"]}))
        task = self.add_task("work_program", "workflow:planned_tests", "RCM planned tests")
        accepted = self.request_approval("planned_tests", task, proposals) if self.run["mode"] == "permission" else proposals
        accepted_units = set()
        for proposal in accepted:
            self.checkpoint()
            spec = dict(proposal["spec"])
            unit_id = spec.pop("_unit_id")
            unit = self._unit(stage, unit_id)
            try:
                item = self._commit_planned_test(spec)
                accepted_units.add(unit_id)
                refs = list(unit.get("result_refs") or []) + [f"planned_test:{item['id']}"]
                self._set_unit(stage, unit, "succeeded", result_refs=refs)
            except WorkspaceConflict as error:
                self._set_unit(stage, unit, "conflict", error=str(error))
            except Exception as error:
                self._set_unit(stage, unit, "failed", error=str(error))
        for unit, _value in candidates:
            if unit["id"] not in accepted_units and unit["status"] == "running":
                self._set_unit(stage, unit, "blocked", error="No planned-test proposal was approved.")

    def _commit_planned_test(self, spec: dict) -> dict:
        rcm_id = str(spec["rcm_id"])
        semantic = f"planned-test:{rcm_id}:{slugify(spec.get('stable_slug') or spec['objective'])}"
        spec["semantic_id"] = semantic
        row = next(item for item in self.ws.rcm if item["id"] == rcm_id)
        spec["workflow_parent_sha1"] = audit_capabilities.rcm_row_sha1(row)
        fields = (
            "title", "objective", "criteria", "steps", "method",
            "expected_evidence", "sampling", "thresholds", "methodology_refs",
            "workflow_parent_sha1",
        )
        expected = parent_hashes(self.ws, [f"rcm:{rcm_id}"])

        def commit(fresh: Workspace):
            row = next(item for item in fresh.rcm if item["id"] == rcm_id)
            existing = next((item for item in row.get("planned_tests") or [] if item.get("id") == spec.get("planned_test_id") or item.get("semantic_id") == semantic), None)
            if existing and existing.get("created_by") != "agent" and self.run["mode"] != "permission":
                return existing, "preserved"
            if existing:
                return (
                    fresh.update_planned_test(
                        rcm_id,
                        existing["id"],
                        {key: spec.get(key) for key in fields if key in spec},
                        agent=True,
                    ),
                    "updated",
                )
            stable_id = "PT-" + hashlib.sha1(semantic.encode()).hexdigest()[:10].upper()
            return (
                fresh.add_planned_test(
                    rcm_id,
                    {**spec, "id": stable_id, "agent_run_id": self.run["id"]},
                ),
                "created",
            )

        result = mutate(self.ws, commit, expected_parents=expected)
        self.ws = result.workspace
        item, action = result.value
        self.run["planning_changes"][f"planned_test_{action}"] += 1
        self.record_artifact("planned_test", item["id"], semantic, action, None)
        return item

    def _definitions(self, stage: dict, units: list[dict]) -> None:
        def worker(unit: dict):
            planned_id = next(ref.split(":", 1)[1] for ref in unit["parent_refs"] if ref.startswith("planned_test:"))
            row, planned = self.ws.planned_test(planned_id)
            if unit["kind"] == "data_test_spec":
                bundle = context_bundles.data_test_spec(self.ws, row, planned)
                candidate = audit_workers.data_test_spec(self, bundle, unit["parent_refs"])
                if planned.get("method") == "validation" and candidate.get("engine") != "validation":
                    raise WorkspaceError(
                        "A validation planned test requires a validation-engine Data Test."
                    )
                return candidate
            bundle = context_bundles.document_test_spec(self.ws, row, planned)
            return audit_workers.document_test_spec(self, bundle, unit["parent_refs"])

        candidates = self._parallel_candidates(stage, units, worker)
        proposals = [
            self.proposal_item(unit["title"], "Executable definition derived from a required kind.", {**value, "_unit_id": unit["id"]})
            for unit, value in candidates
        ]
        task = self.add_task("execution_definitions", "workflow:definitions", "Execution definitions")
        accepted = self.request_approval("execution_definitions", task, proposals) if self.run["mode"] == "permission" else proposals
        accepted_ids = set()
        for proposal in accepted:
            self.checkpoint()
            candidate = dict(proposal["spec"])
            unit_id = candidate.pop("_unit_id")
            unit = self._unit(stage, unit_id)
            try:
                ref = self._commit_definition(unit, candidate)
                accepted_ids.add(unit_id)
                self._set_unit(stage, unit, "succeeded", result_refs=[ref])
            except WorkspaceConflict as error:
                self._set_unit(stage, unit, "conflict", error=str(error))
            except Exception as error:
                self._set_unit(stage, unit, "failed", error=str(error))
        for unit, _value in candidates:
            if unit["id"] not in accepted_ids and unit["status"] == "running":
                self._set_unit(stage, unit, "blocked", error="The execution definition was not approved.")

    def _commit_definition(self, unit: dict, candidate: dict) -> str:
        planned_id = next(ref.split(":", 1)[1] for ref in unit["parent_refs"] if ref.startswith("planned_test:"))
        rcm_id = next(ref.split(":", 1)[1] for ref in unit["parent_refs"] if ref.startswith("rcm:"))
        expected = parent_hashes(self.ws, [f"planned_test:{planned_id}"])
        stable_slug = slugify(candidate.get("title") or planned_id)
        required_kind = "datatest" if unit["kind"] == "data_test_spec" else "doctest"
        semantic = f"{required_kind}:{rcm_id}:{planned_id}:{stable_slug}"

        def commit(fresh: Workspace):
            payload = {
                **candidate, "rcm_id": rcm_id, "planned_test_id": planned_id,
                "semantic_id": semantic, "agent_run_id": self.run["id"],
                "workflow_parent_sha1": audit_capabilities.planned_test_sha1(
                    fresh.planned_test(planned_id)[1]
                ),
            }
            if required_kind == "datatest":
                payload["id"] = "DAT-" + hashlib.sha1(semantic.encode()).hexdigest()[:10].upper()
                existing = next(
                    (
                        item
                        for item in fresh.data_tests
                        if item.get("planned_test_id") == planned_id
                        and (
                            item.get("semantic_id") == semantic
                            or item.get("id") == payload["id"]
                        )
                    ),
                    None,
                )
                if existing:
                    if existing.get("created_by") != "agent" and self.run["mode"] != "permission":
                        return existing, "preserved"
                    updated = data_tests.update(
                        fresh,
                        existing["id"],
                        {
                            key: payload[key]
                            for key in (
                                "title", "objective", "engine", "table_refs", "spec",
                                "rcm_id", "planned_test_id", "workflow_parent_sha1",
                            )
                        },
                        agent=True,
                    )
                    return updated, "updated"
                return data_tests.create(fresh, payload), "created"
            payload["id"] = "DT-" + hashlib.sha1(semantic.encode()).hexdigest()[:8].upper()
            payload["rcm_refs"] = [rcm_id]
            existing_summary = next(
                (
                    item
                    for item in doc_tests.list_tests(fresh)
                    if item.get("planned_test_id") == planned_id
                    and (
                        item.get("semantic_id") == semantic
                        or item.get("id") == payload["id"]
                    )
                ),
                None,
            )
            if existing_summary and existing_summary.get("created_by") != "agent" and self.run["mode"] != "permission":
                return doc_tests.load_test(fresh, existing_summary["id"]), "preserved"
            if existing_summary:
                payload["id"] = existing_summary["id"]
                payload["created"] = existing_summary.get("created")
            test = doc_tests.create_test(fresh, payload)
            action = "updated" if existing_summary else "created"
            missing = dict(candidate.get("missing_evidence") or {})
            no_documents = any(not item.get("document_ids") for item in test.get("items") or [])
            if missing or no_documents:
                test["status"] = "blocked"
                test["scope_limitations"] = str(missing.get("rationale") or "Required evidence is not yet available.")
                evidence_hash = canonical_sha1([
                    {key: item.get(key) for key in ("id", "sha1", "category", "title")}
                    for item in fresh.documents
                ])
                for item in test.get("items") or []:
                    if item.get("document_ids"):
                        continue
                    request = {
                        "id": f"ER-{uuid.uuid4().hex[:10].upper()}",
                        "rcm_id": rcm_id, "planned_test_id": planned_id,
                        "document_test_id": test["id"], "item_id": item["id"],
                        "transaction_identifier": str(missing.get("identifiers") or item.get("label") or ""),
                        "missing_document_types": list(missing.get("document_types") or ["supporting_evidence"]),
                        "status": "open", "reason": test["scope_limitations"],
                        "next_action": "Import or attach matching evidence, then continue the audit.",
                        "blocked_unit_id": unit["id"],
                        "evidence_availability_sha1": evidence_hash,
                        "created": fresh._updated_now(), "updated": fresh._updated_now(),
                    }
                    fresh.evidence_requests.append(request)
                    item.setdefault("evidence_request_ids", []).append(request["id"])
                doc_tests.save_test(fresh, test)
                fresh.save()
            return test, action

        result = mutate(self.ws, commit, expected_parents=expected)
        self.ws = result.workspace
        item, action = result.value
        self.record_artifact(required_kind, item["id"], semantic, action, None)
        return f"{required_kind}:{item['id']}"

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

    def _rollup(self, stage: dict, units: list[dict]) -> None:
        unit = units[0]
        self._set_unit(stage, unit, "running")
        try:
            self._refresh_workspace()
            result = rcm_execution.rollup(self.ws)
            refs = [f"rcm:{item['rcm_id']}" for item in result["rows"]]
            self._set_unit(stage, unit, "succeeded", result_refs=refs)
            self.emit("workspace_changed", {"kind": "rcm", "id": "rollup", "action": "updated"})
            if self.run["mode"] == "permission":
                self._observation_checkpoint()
        except (Cancelled, LimitExceeded):
            raise
        except WorkspaceConflict as error:
            self._set_unit(stage, unit, "conflict", error=str(error))
        except Exception as error:
            self._set_unit(stage, unit, "failed", error=str(error))

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

    def _finding_drafts(self, stage: dict, units: list[dict]) -> None:
        candidates = self._parallel_candidates(
            stage, units,
            lambda unit: audit_workers.finding(
                self,
                context_bundles.finding(
                    self.ws,
                    next(item for item in self.ws.observations if item["id"] == unit["parent_refs"][0].split(":", 1)[1]),
                ),
                unit["parent_refs"],
            ),
        )
        proposals = [self.proposal_item(unit["title"], "Draft from an auditor-dispositioned observation.", {**value, "_unit_id": unit["id"]}) for unit, value in candidates]
        task = self.add_task("findings", "workflow:findings", "Eligible finding drafts")
        accepted = self.request_approval("finding_drafts", task, proposals) if self.run["mode"] == "permission" else proposals
        accepted_ids = set()
        for proposal in accepted:
            self.checkpoint()
            spec = dict(proposal["spec"])
            unit_id = spec.pop("_unit_id")
            unit = self._unit(stage, unit_id)
            observation_id = unit["parent_refs"][0].split(":", 1)[1]
            observation = next(item for item in self.ws.observations if item["id"] == observation_id)
            try:
                execution_ref = str(observation["execution_ref"])
                anchor = findings.anchor_from_ref(self.ws, execution_ref, run_id=self.run["id"])
                draft = {
                    **spec, "semantic_id": f"finding:observation:{observation_id}",
                    "agent_run_id": self.run["id"], "source_observation_id": observation_id,
                    "rcm_refs": [observation["rcm_id"]],
                    "procedure_refs": [],
                    "planned_test_refs": [observation["planned_test_id"]],
                    "execution_refs": [execution_ref],
                    "evidence_refs": [anchor] if anchor else [],
                    "auditor_confirmed": False,
                }
                issues = findings.support_issues(self.ws, draft)
                if issues:
                    raise WorkspaceError("Finding draft failed support validation: " + "; ".join(issues))
                item = findings.add(self.ws, draft, source="agent")
                accepted_ids.add(unit_id)
                self._set_unit(stage, unit, "succeeded", result_refs=[f"finding:{item['id']}"])
                self.emit("workspace_changed", {"kind": "finding", "id": item["id"], "action": "created"})
            except WorkspaceConflict as error:
                self._set_unit(stage, unit, "conflict", error=str(error))
            except Exception as error:
                self._set_unit(stage, unit, "failed", error=str(error))
        for unit, _value in candidates:
            if unit["id"] not in accepted_ids and unit["status"] == "running":
                self._set_unit(stage, unit, "blocked", error="The finding draft was not approved.")

    def _working_papers(self, stage: dict, units: list[dict]) -> None:
        for unit in units:
            self.checkpoint()
            self._set_unit(stage, unit, "running")
            try:
                rcm_id = unit["parent_refs"][0].split(":", 1)[1]
                paper = working_papers.generate_rcm(self.ws, rcm_id)
                self._set_unit(stage, unit, "succeeded", result_refs=[f"working_paper:{rcm_id}"])
            except WorkspaceConflict as error:
                self._set_unit(stage, unit, "conflict", error=str(error))
            except Exception as error:
                self._set_unit(stage, unit, "failed", error=str(error))

    def _dashboard(self, stage: dict, units: list[dict]) -> None:
        unit = units[0]
        self._set_unit(stage, unit, "running")
        try:
            result = dashboard.curate_rcm_tiles(self.ws, run_id=self.run["id"])
            self._set_unit(stage, unit, "succeeded", result_refs=[f"tile:{item['id']}" for item in result["tiles"]])
            self.emit("workspace_changed", {"kind": "dashboard", "id": "curation", "action": "updated"})
        except Exception as error:
            self._set_unit(stage, unit, "failed", error=str(error))

    def _report(self, stage: dict, units: list[dict]) -> None:
        unit = units[0]
        self._set_unit(stage, unit, "running")
        try:
            result = report.generate(
                self.ws,
                use_model=False,
                run_id=self.run["id"],
                workflow=self.run.get("workflow"),
            )
            if result.get("requires_reconcile"):
                self._set_unit(stage, unit, "awaiting_confirmation", error="Auditor-edited report preserved; reconcile the generated candidate.", result_refs=["report:draft"])
            else:
                self._set_unit(stage, unit, "succeeded", result_refs=["report:draft"])
            self.emit("workspace_changed", {"kind": "report", "id": "draft", "action": "updated"})
        except Exception as error:
            self._set_unit(stage, unit, "failed", error=str(error))

    def _verify(self, stage: dict, units: list[dict]) -> None:
        unit = units[0]
        self._set_unit(stage, unit, "running")
        try:
            completion = rcm_execution.completion(self.ws)
            quality = report.quality_checks(self.ws)
            errors = [item for item in quality.get("issues") or [] if item.get("severity") == "error"]
            output_states = {
                capability: audit_capabilities.REGISTRY.get(capability).readiness(
                    self.ws, {}
                ).payload()
                for capability in (
                    "working_papers.generated", "dashboard.curated", "report.working_draft"
                )
            }
            output_issues = [
                capability
                for capability, readiness in output_states.items()
                if readiness.get("state") != "satisfied"
            ]
            self.run["audit_outcome"] = {
                "audit_complete": completion["status"] == "completed" and not errors and not output_issues,
                "completion_status": completion["status"],
                "planned_tests_total": sum(len(row.get("planned_tests") or []) for row in self.ws.rcm),
                "planned_tests_completed": sum(str(item.get("status") or "").startswith("completed") for row in self.ws.rcm for item in row.get("planned_tests") or []),
                "planned_tests_review_required": sum(item.get("status") == "review_required" for row in self.ws.rcm for item in row.get("planned_tests") or []),
                "planned_tests_blocked": sum(item.get("status") == "blocked" for row in self.ws.rcm for item in row.get("planned_tests") or []),
                "data_tests_required": sum("datatest" in rcm_execution.required_execution_kinds(item.get("method") or "") for row in self.ws.rcm for item in row.get("planned_tests") or []),
                "data_tests_executed": sum(bool(item.get("last_run")) for item in self.ws.data_tests if item.get("planned_test_id")),
                "document_tests_required": sum("doctest" in rcm_execution.required_execution_kinds(item.get("method") or "") for row in self.ws.rcm for item in row.get("planned_tests") or []),
                "document_tests_executed": sum(item.get("status") == "completed" for item in doc_tests.list_tests(self.ws)),
                "open_observations": len(completion.get("open_observations") or []),
                "supported_findings": sum(item.get("auditor_confirmed") and not findings.support_issues(self.ws, item) for item in self.ws.findings),
                "draft_findings": sum(not item.get("auditor_confirmed") for item in self.ws.findings),
                "report_quality_ok": not errors,
                "report_quality_errors": len(errors),
                "output_readiness": output_states,
                "open_gate_count": len(completion.get("open_observations") or []) + int((completion.get("coverage") or {}).get("issue_count") or 0) + len(errors) + len(output_issues),
            }
            status = "succeeded" if self.run["audit_outcome"]["audit_complete"] else "blocked"
            self._set_unit(
                stage, unit, status,
                error=None if status == "succeeded" else (
                    f"Completion status: {completion['status']}; report errors: {len(errors)}; "
                    f"output gates: {len(output_issues)}."
                ),
                result_refs=["audit:verification"],
            )
        except Exception as error:
            self._set_unit(stage, unit, "failed", error=str(error))

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
    def _finish_workflow(self) -> None:
        """Close the run on real audit outcomes, not on unit bookkeeping.

        A run that executed every unit cleanly can still be incomplete — open
        evidence requests, undispositioned observations, or an unreconciled
        report. Those become `next_outcomes`, the exact input "Continue audit"
        replays, and they are why `completed_with_open_items` is a distinct
        terminal status from `completed`.
        """
        self._refresh_workspace()
        completion = rcm_execution.completion(self.ws)
        stages = self.run["workflow"].get("stages") or []
        failed = sum(unit.get("status") in {"failed", "conflict"} for stage in stages for unit in stage.get("units") or [])
        open_units = sum(unit.get("status") in {"blocked", "awaiting_input", "awaiting_confirmation"} for stage in stages for unit in stage.get("units") or [])
        open_observations = [item for item in self.ws.observations if item.get("status") != "disposed"]
        open_evidence = [item for item in self.ws.evidence_requests if item.get("status") == "open"]
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
        self.run["workflow"]["next_outcomes"] = list(dict.fromkeys(next_outcomes))
        self.run["workflow"]["workspace_revision"] = self.ws.revision
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
        self.run["summary_markdown"] = (
            "# Audit workflow summary\n\n"
            f"- Requested outcomes: {', '.join(self.run['workflow']['requested_outcomes'])}\n"
            f"- Completion status: {completion['status']}\n"
            f"- Failed/conflict units: {failed}\n"
            f"- Open workflow units: {open_units}\n"
            f"- Open observations: {len(open_observations)}\n"
            f"- Open evidence requests: {len(open_evidence)}\n"
        )
        self.mark_finished()
        self.run["command"]["status"] = terminal
        self.set_status(terminal)
        self.emit("summary_ready", {"run_id": self.run["id"]})

    def _adopt_legacy(self) -> None:
        workflow_state = self.run["workflow"]
        if workflow_state.get("legacy_adoptions"):
            return
        adoptions = []
        planning = self.ws.planning
        workspace_changed = False
        apm_readiness = audit_capabilities.REGISTRY.get("planning.apm_ready").readiness(self.ws, {})
        if apm_readiness.satisfied and not planning.get("workflow_basis_sha1"):
            planning["workflow_basis_sha1"] = audit_capabilities.planning_basis_sha1(self.ws)
            workspace_changed = True
            payload = {
                "artifact_ref": "planning:apm",
                "content_sha1": canonical_sha1(planning.get("apm_markdown")),
                "prerequisite_hashes": {"planning_basis": planning["workflow_basis_sha1"]},
                "currency": "unverified", "ownership": planning.get("created_by"),
                "review_state": planning.get("review_status"), "adopted_at": store.utcnow(),
            }
            payload["sidecar"] = store.write_sidecar(self.ws, self.run["id"], payload)
            adoptions.append(payload)
        for row in self.ws.rcm:
            if str(row.get("risk") or "").strip() and str(row.get("control") or "").strip():
                if not row.get("workflow_parent_sha1"):
                    row["workflow_parent_sha1"] = audit_capabilities.apm_sha1(self.ws)
                    workspace_changed = True
                    payload = {
                        "artifact_ref": f"rcm:{row['id']}",
                        "content_sha1": canonical_sha1(row),
                        "prerequisite_hashes": {"planning:apm": row["workflow_parent_sha1"]},
                        "currency": "unverified", "ownership": row.get("created_by"),
                        "review_state": row.get("review_status"), "adopted_at": store.utcnow(),
                    }
                    payload["sidecar"] = store.write_sidecar(self.ws, self.run["id"], payload)
                    adoptions.append(payload)
            for planned in row.get("planned_tests") or []:
                if planned.get("workflow_parent_sha1") or not planned.get("steps"):
                    continue
                planned["workflow_parent_sha1"] = audit_capabilities.rcm_row_sha1(row)
                workspace_changed = True
                payload = {
                    "artifact_ref": f"planned_test:{planned['id']}",
                    "content_sha1": canonical_sha1(planned),
                    "prerequisite_hashes": {f"rcm:{row['id']}": planned["workflow_parent_sha1"]},
                    "currency": "unverified", "ownership": planned.get("created_by"),
                    "review_state": planned.get("status"), "adopted_at": store.utcnow(),
                }
                payload["sidecar"] = store.write_sidecar(self.ws, self.run["id"], payload)
                adoptions.append(payload)
        for item in self.ws.data_tests:
            if item.get("workflow_parent_sha1") or not item.get("planned_test_id"):
                continue
            try:
                _row, planned = self.ws.planned_test(item["planned_test_id"])
            except WorkspaceError:
                continue
            item["workflow_parent_sha1"] = audit_capabilities.planned_test_sha1(planned)
            workspace_changed = True
            payload = {
                "artifact_ref": f"datatest:{item['id']}", "content_sha1": canonical_sha1(item),
                "prerequisite_hashes": {f"planned_test:{planned['id']}": item["workflow_parent_sha1"]},
                "currency": "unverified", "ownership": item.get("created_by"),
                "review_state": item.get("status"), "adopted_at": store.utcnow(),
            }
            payload["sidecar"] = store.write_sidecar(self.ws, self.run["id"], payload)
            adoptions.append(payload)
        for item in self.ws.findings:
            if item.get("workflow_adoption_sha1"):
                continue
            # Findings that are not yet support-complete remain visible drafts;
            # adoption records their ownership without upgrading their status.
            content_sha1 = canonical_sha1(
                {key: value for key, value in item.items() if key != "workflow_adoption_sha1"}
            )
            prerequisites = {}
            for ref in item.get("execution_refs") or []:
                kind, separator, source_id = str(ref).partition(":")
                resolved = findings.artifact(self.ws, kind, source_id) if separator else None
                prerequisites[str(ref)] = (
                    resolved.get("sha1") if resolved else canonical_sha1(ref)
                )
            item["workflow_adoption_sha1"] = content_sha1
            workspace_changed = True
            payload = {
                "artifact_ref": f"finding:{item['id']}",
                "content_sha1": content_sha1,
                "prerequisite_hashes": prerequisites,
                "currency": "unverified", "ownership": item.get("created_by") or item.get("source"),
                "review_state": "confirmed" if item.get("auditor_confirmed") else "draft",
                "support_issues": findings.support_issues(self.ws, item),
                "adopted_at": store.utcnow(),
            }
            payload["sidecar"] = store.write_sidecar(self.ws, self.run["id"], payload)
            adoptions.append(payload)
        current_report = report.hydrate(self.ws)
        if (
            str(current_report.get("markdown") or current_report.get("generated_markdown") or "").strip()
            and not current_report.get("workflow_parent_sha1")
        ):
            current_report["workflow_parent_sha1"] = canonical_sha1(
                report.build_context(self.ws)
            )
            self.ws.report = current_report
            workspace_changed = True
            payload = {
                "artifact_ref": "report:draft",
                "content_sha1": canonical_sha1(current_report.get("generated_markdown") or current_report.get("markdown")),
                "prerequisite_hashes": {
                    "report_context": current_report["workflow_parent_sha1"]
                },
                "currency": "unverified", "ownership": "auditor" if current_report.get("edited") else "legacy",
                "review_state": "reconciliation_required" if current_report.get("edited") else "draft",
                "adopted_at": store.utcnow(),
            }
            payload["sidecar"] = store.write_sidecar(self.ws, self.run["id"], payload)
            adoptions.append(payload)
        if workspace_changed:
            self.ws.save()
        for summary in doc_tests.list_tests(self.ws):
            if summary.get("workflow_parent_sha1") or not summary.get("planned_test_id"):
                continue
            try:
                _row, planned = self.ws.planned_test(summary["planned_test_id"])
            except WorkspaceError:
                continue
            test = doc_tests.load_test(self.ws, summary["id"])
            test["workflow_parent_sha1"] = audit_capabilities.planned_test_sha1(planned)
            doc_tests.save_test(self.ws, test)
            payload = {
                "artifact_ref": f"doctest:{test['id']}", "content_sha1": test.get("sha1"),
                "prerequisite_hashes": {f"planned_test:{planned['id']}": test["workflow_parent_sha1"]},
                "currency": "unverified", "ownership": test.get("created_by"),
                "review_state": test.get("status"), "adopted_at": store.utcnow(),
            }
            payload["sidecar"] = store.write_sidecar(self.ws, self.run["id"], payload)
            adoptions.append(payload)
        workflow_state["legacy_adoptions"] = adoptions
        if adoptions:
            self.warn(f"Adopted {len(adoptions)} valid legacy planning artifact(s) with unverified currency; auditor-owned content was preserved.")

    def _cancel_remaining(self) -> None:
        for stage in self.run.get("workflow", {}).get("stages") or []:
            for unit in stage.get("units") or []:
                if unit.get("status") in {"queued", "running", "blocked", "awaiting_input", "awaiting_confirmation"}:
                    workflow.transition_unit(unit, "cancelled")
            if stage.get("status") in {"queued", "running"}:
                stage["status"] = "cancelled"
        self.save()

    def _mark_running_failed(self, error: str) -> None:
        for stage in self.run.get("workflow", {}).get("stages") or []:
            for unit in stage.get("units") or []:
                if unit.get("status") == "running":
                    workflow.transition_unit(unit, "failed", error=error)
            if stage.get("status") == "running":
                stage["status"] = "failed"
        self.save()

    def _mark_running_conflict(self, error: str) -> None:
        for stage in self.run.get("workflow", {}).get("stages") or []:
            for unit in stage.get("units") or []:
                if unit.get("status") == "running":
                    workflow.transition_unit(unit, "conflict", error=error)
            if stage.get("status") == "running":
                stage["status"] = "failed"
        self.save()
