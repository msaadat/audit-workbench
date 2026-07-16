"""Durable unified command/action-graph runner."""

from __future__ import annotations

import json
import time

from .. import llm
from ..workspaces import Workspace, WorkspaceError
from . import actions, artifact_index, ledger, prompts, store
from .base import BaseRunner, Cancelled, LimitExceeded

GOAL_TEMPLATES = {
    "full_audit_working_draft": {
        "objective": "Prepare complete audit working content through an evidence-linked report draft.",
        "constraints": ["Do not assert a formal audit opinion.", "Preserve auditor edits."],
    },
    "planning": {"objective": "Prepare or improve engagement planning, RCM, and audit procedures."},
    "apm_only": {"objective": "Prepare or revise only the audit planning memorandum."},
    "data_analysis": {"objective": "Analyze available structured data and preserve useful validated work."},
    "document_testing": {"objective": "Prepare and execute relevant document tests and working papers."},
    "report": {"objective": "Prepare evidence-linked audit report working content and run quality checks."},
}

SEMANTIC_PROPOSAL_ATTEMPTS = 2


class CommandRunner(BaseRunner):
    PLANNING_ACTION_TYPES = {
        "update_planning_context", "generate_apm", "edit_apm",
        "create_rcm_row", "edit_rcm_row", "create_procedure", "edit_procedure",
    }

    def _drain_inbox(self) -> None:
        """General chat is durable follow-up work, never active-graph steering."""
        with self.handle.lock:
            queued, self.handle.command_queue = self.handle.command_queue[:], []
        self.handle.command_queued.clear()
        if queued:
            self.run.setdefault("pending_commands", []).extend(queued)
            self.save()

    def execute(self) -> None:
        if not self.run.get("started"):
            self.run["started"] = store.utcnow()
            self.save()
        try:
            self._recover_running_actions()
            if not self.run.get("actions"):
                if self._requests_full_audit():
                    self._prepare_full_audit_planning()
                self._interpret()
            self._drive_graph()
            self._finish()
        except Cancelled:
            for action in self.run.get("actions") or []:
                if action["status"] in {"proposed", "ready", "awaiting_input", "awaiting_confirmation"}:
                    ledger.transition(action, "cancelled")
            ledger.project_legacy_plan(self.run)
            self.run["finished"] = store.utcnow()
            self.set_status("cancelled")
        except (LimitExceeded, llm.LLMError) as error:
            self._fail_running_plan_tasks(str(error))
            self.warn(str(error))
            self._finish(force_issue=True)
        except Exception as error:
            self._fail_running_plan_tasks(str(error))
            self.run["error"] = str(error)
            self.run["finished"] = store.utcnow()
            self.set_status("failed")

    def _fail_running_plan_tasks(self, error: str) -> None:
        """Close embedded planning tasks when a command run ends abruptly.

        Full-audit commands prepare their APM, RCM, and audit program before
        the action graph is interpreted.  Those tasks live in the legacy plan
        projection, so a transport error would otherwise leave the current
        planning task permanently displayed as running after the run failed.
        """
        for stage in self.run.get("plan", {}).get("stages", []):
            for task in stage.get("tasks", []):
                if task.get("status") == "running":
                    self.task_status(task, "failed", error)

    def _catalog(self) -> list[dict]:
        return [
            {
                "type": value.type, "version": value.version,
                "description": value.description, "input_schema": value.input_schema,
                "target_kinds": list(value.target_kinds), "risk": value.risk,
                "model_usage": value.model_usage,
            }
            for value in actions.REGISTRY.all()
        ]

    def _interpret(self) -> None:
        self.set_status("interpreting")
        index = artifact_index.build(self.ws)
        command = self.run["command"]
        template = GOAL_TEMPLATES.get(command.get("goal_template"))
        if command.get("goal_template") and template is None:
            raise WorkspaceError("Unknown goal template.")
        base_user = prompts.command_interpreter_user(
            command, template, artifact_index.compact(index), self._catalog(), self.run["limits"],
            prepared_planning=self.run.get("prepared_planning"),
        )
        attempt_user = base_user
        created = []
        payload = {}
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            payload = self.llm_json(prompts.COMMAND_INTERPRETER_SYSTEM, attempt_user)
            objective = str(payload.get("objective") or (template or {}).get("objective") or command.get("text") or "").strip()
            goal = {
                "objective": objective,
                "constraints": [str(value) for value in payload.get("constraints") or (template or {}).get("constraints") or []],
                "completion_criteria": [str(value) for value in payload.get("completion_criteria") or []],
            }
            self.run["goal"] = goal
            proposals = payload.get("actions") or []
            if self.run.get("prepared_planning") and isinstance(proposals, list):
                proposals = self._remove_redundant_planning_actions(proposals)
            try:
                if not isinstance(proposals, list):
                    raise WorkspaceError("Command interpreter actions must be a list.")
                created = ledger.append_actions(
                    self.run, proposals, audit_lifecycle=self._is_full_audit_goal(goal)
                )
                break
            except WorkspaceError as error:
                self._record_rejected_proposals("command_interpreter", proposals, error)
                if attempt + 1 >= SEMANTIC_PROPOSAL_ATTEMPTS:
                    raise
                attempt_user = self._proposal_repair_user(base_user, payload, error)
        if payload.get("needs_planning_wave") and not any(item.get("planning_significant") for item in created):
            for item in reversed(created):
                definition = actions.REGISTRY.get(item["type"], item["definition_version"])
                if definition.risk in {"read", "compute"}:
                    item["planning_significant"] = True
                    break
        self.run["command"]["status"] = "planned"
        self.save()
        self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})

    def _requests_full_audit(self) -> bool:
        command = self.run.get("command") or {}
        if command.get("goal_template") == "full_audit_working_draft":
            return True
        text = str(command.get("text") or "").casefold()
        return any(phrase in text for phrase in (
            "full audit", "complete the audit", "complete audit", "entire audit",
            "end-to-end audit", "end to end audit",
        ))

    def _prepare_full_audit_planning(self) -> None:
        """Prepare grounded planning before asking for downstream audit work."""
        if self.run.get("prepared_planning"):
            return
        from .planning_runner import PlanningRunner

        self.set_status("executing")
        self.run.setdefault("context", {})["require_planning_quality"] = True
        planning = PlanningRunner(self.ws, self.run, self.handle)
        basis = planning.stage_context()
        apm = planning.stage_apm(basis)
        rcm = planning.stage_rcm(basis, apm)
        planning.stage_work_program(basis, rcm)
        self.run["prepared_planning"] = {
            "apm": bool(str(self.ws.planning.get("apm_markdown") or "").strip()),
            "rcm_refs": [item["id"] for item in self.ws.rcm],
            "procedure_refs": [item["id"] for item in self.ws.work_program],
            "document_content_disclosed": bool(basis.get("document_content_disclosed")),
        }
        self.save()

    def _remove_redundant_planning_actions(self, proposals: list[dict]) -> list[dict]:
        removed_ids = {
            str(item.get("id") or "") for item in proposals
            if isinstance(item, dict) and item.get("type") in self.PLANNING_ACTION_TYPES
        }
        cleaned = []
        for proposal in proposals:
            if not isinstance(proposal, dict) or proposal.get("type") in self.PLANNING_ACTION_TYPES:
                continue
            item = dict(proposal)
            dependencies = item.get("depends_on") or []
            if isinstance(dependencies, list):
                item["depends_on"] = [str(value) for value in dependencies if str(value) not in removed_ids]
            cleaned.append(item)
        return cleaned

    def _is_full_audit_goal(self, goal: dict | None = None) -> bool:
        command = self.run.get("command") or {}
        if self._requests_full_audit():
            return True
        goal = goal or self.run.get("goal") or {}
        combined = " ".join([
            str(command.get("text") or ""),
            str(goal.get("objective") or ""),
            *[str(value) for value in goal.get("completion_criteria") or []],
        ]).casefold()
        wants_report = "report" in combined and any(word in combined for word in ("draft", "generate", "complete"))
        wants_full_cycle = any(phrase in combined for phrase in (
            "complete the audit", "full audit", "all steps", "all procedures", "all_procedures",
        ))
        return wants_report and wants_full_cycle

    @staticmethod
    def _proposal_repair_user(base_user: str, payload: dict, error: Exception) -> str:
        return (
            f"{base_user}\n\nYour previous JSON parsed, but its action graph violated the registered "
            f"contract: {error}. Return a corrected complete JSON object. Preserve the intended goal and "
            f"valid dependencies, use only catalog target kinds and required args. Previous JSON: "
            f"{json.dumps(payload, default=str)}"
        )

    def _record_rejected_proposals(self, stage: str, proposals: object, error: Exception) -> None:
        safe_actions = []
        if isinstance(proposals, list):
            for proposal in proposals[: int(self.run["limits"].get("max_actions", 60))]:
                if not isinstance(proposal, dict):
                    safe_actions.append({"value_type": type(proposal).__name__})
                    continue
                target = proposal.get("target") if isinstance(proposal.get("target"), dict) else {}
                args = proposal.get("args") if isinstance(proposal.get("args"), dict) else {}
                dependencies = proposal.get("depends_on") if isinstance(proposal.get("depends_on"), list) else []
                safe_actions.append({
                    "id": str(proposal.get("id") or ""),
                    "type": str(proposal.get("type") or ""),
                    "target": {
                        "kind": target.get("kind"),
                        "selector": str(target.get("selector"))[:200] if target.get("selector") else None,
                        "resolved_id": str(target.get("resolved_id"))[:200] if target.get("resolved_id") else None,
                    },
                    "depends_on": [str(value) for value in dependencies],
                    "arg_keys": sorted(str(key) for key in args),
                })
        diagnostic = {
            "at": store.utcnow(), "stage": stage, "error": str(error), "actions": safe_actions,
        }
        self.run.setdefault("rejected_proposals", []).append(diagnostic)
        self.save()
        self.emit("proposal_rejected", diagnostic)

    def _recover_running_actions(self) -> None:
        changed = False
        for action in self.run.get("actions") or []:
            if action["status"] != "running":
                continue
            definition = actions.validate_action(action)
            outcome = definition.reconciler(self.ws, action) if definition.reconciler else "retry"
            if outcome == "already_applied":
                ledger.transition(action, "succeeded")
                action["finished_at"] = store.utcnow()
            elif outcome == "retry":
                ledger.transition(action, "failed")
                ledger.transition(action, "ready")
            else:
                ledger.transition(action, "awaiting_input")
                interaction = ledger.interaction(
                    self.run, action, "conflict_resolution",
                    "This artifact changed while the action was interrupted. Choose how to proceed.",
                    options=[{"value": "skip", "label": "Keep current and skip"}, {"value": "retry", "label": "Revalidate and retry"}],
                    policy_reason="The current artifact matches neither the prepared before nor after state.",
                )
                self.emit("action_conflict", {"action_id": action["id"], "interaction_id": interaction["id"]})
            changed = True
        if changed:
            ledger.project_legacy_plan(self.run); self.save()

    def _drive_graph(self) -> None:
        self.set_status("executing")
        while True:
            self.checkpoint()
            self._block_failed_dependencies()
            pending_interaction = next((item for item in self.run.get("interactions") or [] if item["status"] == "pending"), None)
            if pending_interaction:
                action = self._action(pending_interaction["action_id"])
                if self._dismiss_obsolete_interaction(action, pending_interaction):
                    continue
                self._wait_interaction(action, pending_interaction)
                continue
            proposed = next((action for action in self.run["actions"] if action["status"] == "proposed"), None)
            if proposed:
                self._resolve_and_gate(proposed)
                continue
            ready = next((action for action in self.run["actions"] if action["status"] == "ready" and self._dependencies_succeeded(action)), None)
            if ready:
                self._execute_action(ready)
                continue
            if any(action["status"] in {"running", "awaiting_input", "awaiting_confirmation"} for action in self.run["actions"]):
                time.sleep(0.05)
                continue
            break

    def _pending_target_producer(self, action: dict) -> dict | None:
        """Return a dependency that will create this action's target locally."""
        target = action.get("target") or {}
        target_kind = str(target.get("kind") or "")
        target_id = str(target.get("resolved_id") or "")
        if not target_kind or not target_id:
            return None
        by_id = {item["id"]: item for item in self.run.get("actions") or []}
        pending = list(action.get("depends_on") or [])
        seen = set()
        while pending:
            dependency_id = pending.pop()
            if dependency_id in seen or dependency_id not in by_id:
                continue
            seen.add(dependency_id)
            dependency = by_id[dependency_id]
            if dependency["status"] != "succeeded":
                expected = actions.expected_postcondition(dependency)
                produced_kind = str(expected.get("kind") or "")
                produced_id = str(expected.get("id") or "")
                exact_match = produced_kind == target_kind and produced_id == target_id
                child_match = (
                    target_kind == "doctest_item"
                    and produced_kind == "doctest"
                    and bool(produced_id)
                    and target_id.startswith(f"{produced_id}:")
                )
                generated_report = (
                    target_kind == "report" and target_id == "working"
                    and dependency["type"] in {"generate_report", "edit_report"}
                )
                if exact_match or child_match or generated_report:
                    return dependency
            pending.extend(dependency.get("depends_on") or [])
        return None

    def _dismiss_obsolete_interaction(self, action: dict, interaction: dict) -> bool:
        """Resume runs paused by interactions made obsolete by local dependencies."""
        if interaction.get("type") == "conflict_resolution":
            target = action.get("target") or {}
            if not target.get("kind") or not target.get("resolved_id"):
                return False
            resolution = artifact_index.resolve(
                artifact_index.build(self.ws), target["kind"], None, target["resolved_id"]
            )
            dependency = self._dependency_post_state_source(action, resolution)
            if dependency is None:
                return False
            snapshot = actions.artifact_snapshot(self.ws, target["kind"], target["resolved_id"])
            interaction.update(
                status="resolved",
                response={
                    "decision": "rebased_to_dependency",
                    "dependency_action_id": dependency["id"],
                },
                actor="orchestrator",
                resolved_at=store.utcnow(),
            )
            action["resolution"] = resolution
            action["precondition"] = {
                "artifact_sha1": artifact_index.canonical_sha1(snapshot) if snapshot is not None else None,
                "snapshot": store.write_sidecar(self.ws, self.run["id"], snapshot) if snapshot is not None else None,
            }
            if action["status"] == "awaiting_input":
                ledger.transition(action, "ready")
            self._save_action(action)
            self.emit("interaction_resolved", {
                "interaction_id": interaction["id"], "action_id": action["id"],
                "decision": "rebased_to_dependency",
            })
            return True
        if interaction.get("type") not in {"clarification", "target_choice"}:
            return False
        adjustments = ledger.normalize_created_targets(self.run, [action])
        if adjustments:
            self.run.setdefault("lifecycle_adjustments", []).extend(adjustments)
        producer = self._pending_target_producer(action)
        if producer is None:
            return False
        interaction.update(
            status="resolved",
            response={"decision": "deferred_to_dependency", "producer_action_id": producer["id"]},
            actor="orchestrator",
            resolved_at=store.utcnow(),
        )
        action["target"]["selector"] = None
        action["resolution"] = None
        action["precondition"] = None
        if action["status"] == "awaiting_input":
            ledger.transition(action, "proposed")
        self._save_action(action)
        self.emit("interaction_resolved", {
            "interaction_id": interaction["id"], "action_id": action["id"],
            "decision": "deferred_to_dependency",
        })
        return True

    def _resolve_and_gate(self, action: dict) -> None:
        target = action["target"]
        definition = actions.REGISTRY.get(action["type"], action["definition_version"])
        # Also normalize pre-fix persisted proposals when an interrupted run
        # is resumed. New proposals are normalized by the ledger.
        adjustments = ledger.normalize_created_targets(self.run, [action])
        if adjustments:
            self.run.setdefault("lifecycle_adjustments", []).extend(adjustments)
        if definition.target_kinds and not target.get("kind") and len(definition.target_kinds) == 1:
            target["kind"] = definition.target_kinds[0]
        definition = actions.validate_action(action)
        if definition.target_kinds:
            defaults = {"planning": "apm", "report": "working"}
            if not target.get("resolved_id") and target.get("kind") in defaults and not target.get("selector"):
                target["resolved_id"] = defaults[target["kind"]]
            index = artifact_index.build(self.ws)
            producer = self._pending_target_producer(action)
            if producer is not None:
                resolution = {
                    "index_revision": index["revision"],
                    "resolved_id": target["resolved_id"],
                    "resolved_ref": f"{target['kind']}:{target['resolved_id']}",
                    "confidence": 1.0,
                    "reason": f"target will be created by dependency '{producer['id']}'",
                    "candidates": [], "sha1": None,
                    "title": target["resolved_id"], "created_by": "agent",
                    "producer_action_id": producer["id"],
                }
            else:
                resolution = artifact_index.resolve(
                    index, target["kind"], target.get("selector"), target.get("resolved_id")
                )
            action["resolution"] = resolution
            if not resolution.get("resolved_id"):
                if resolution.get("candidates"):
                    ledger.transition(action, "awaiting_input")
                    interaction = ledger.interaction(
                        self.run, action, "target_choice", "Choose the exact artifact this command should affect.",
                        options=resolution["candidates"], policy_reason=resolution["reason"],
                    )
                else:
                    ledger.transition(action, "awaiting_input")
                    interaction = ledger.interaction(
                        self.run, action, "clarification", "Which artifact should this action affect?",
                        policy_reason="No local artifact matched the selector.",
                    )
                self._save_action(action)
                self.emit("interaction_request", {"interaction": interaction})
                return
            target["resolved_id"] = resolution["resolved_id"]
            if producer is None and definition.risk not in {"read", "compute"}:
                snapshot = actions.artifact_snapshot(self.ws, target["kind"], target["resolved_id"])
                action["precondition"] = {
                    "artifact_sha1": artifact_index.canonical_sha1(snapshot) if snapshot is not None else None,
                    "snapshot": store.write_sidecar(self.ws, self.run["id"], snapshot) if snapshot is not None else None,
                }
        if actions.approval_required(definition, self.run, action):
            ledger.transition(action, "awaiting_confirmation")
            type_ = "confirmation" if definition.risk == "destructive" else "proposal_approval"
            resolution = action.get("resolution") or {}
            prompt = (
                f"Confirm {definition.description.lower()} for {resolution.get('title') or action['type']}."
                if type_ == "confirmation" else f"Review and approve: {definition.description}."
            )
            payload = {"action_type": action["type"], "args": action["args"], "target": action["target"], "resolution": resolution, "before": action.get("precondition")}
            ref = store.write_sidecar(self.ws, self.run["id"], payload)
            interaction = ledger.interaction(
                self.run, action, type_, prompt,
                options=[{"value": "approve", "label": "Approve"}, {"value": "reject", "label": "Reject"}],
                payload={"sidecar": ref, "preview": {"action_type": action["type"], "target": resolution.get("resolved_ref"), "title": resolution.get("title"), "args": action["args"] if len(json.dumps(action["args"], default=str)) <= 4000 else "Stored in sidecar"}},
                policy_reason=f"{definition.risk} actions require review under the selected mode.",
            )
            self._save_action(action); self.emit("interaction_request", {"interaction": interaction})
            return
        ledger.transition(action, "ready")
        self._save_action(action)

    def _execute_action(self, action: dict) -> None:
        definition = actions.validate_action(action)
        self.checkpoint()
        if action["type"] == "classify_import_batch":
            from .. import intake
            from .intake_runner import IntakeRunner

            batch = intake.load_batch(self.ws, action["args"]["batch_id"])
            IntakeRunner(self.ws, self.run, self.handle)._classify(batch)
            intake.save_batch(self.ws, batch)
        # Re-resolve immediately before mutations and capture a hashed before snapshot.
        if definition.target_kinds:
            index = artifact_index.build(self.ws)
            resolution = artifact_index.resolve(index, action["target"]["kind"], None, action["target"].get("resolved_id"))
            previous = action.get("resolution") or {}
            resolution_changed = bool(
                previous.get("sha1") and previous.get("sha1") != resolution.get("sha1")
            )
            dependency_source = (
                self._dependency_post_state_source(action, resolution)
                if resolution_changed else None
            )
            if not resolution.get("resolved_id") or (resolution_changed and dependency_source is None):
                ledger.transition(action, "awaiting_input")
                current_snapshot = actions.artifact_snapshot(self.ws, action["target"]["kind"], action["target"]["resolved_id"])
                comparison = store.write_sidecar(self.ws, self.run["id"], {
                    "before": store.read_sidecar(self.ws, self.run["id"], (action.get("precondition") or {}).get("snapshot")) if (action.get("precondition") or {}).get("snapshot") else None,
                    "current": current_snapshot, "proposed_args": action["args"],
                })
                interaction = ledger.interaction(
                    self.run, action, "conflict_resolution", "The target changed after planning. Review before continuing.",
                    options=[{"value": "retry", "label": "Use current version"}, {"value": "skip", "label": "Skip action"}],
                    payload={"target": resolution, "comparison_sidecar": comparison}, policy_reason="Optimistic precondition failed.",
                )
                self._save_action(action); self.emit("action_conflict", {"action_id": action["id"], "interaction_id": interaction["id"]})
                return
            action["resolution"] = resolution
            snapshot = actions.artifact_snapshot(self.ws, action["target"]["kind"], action["target"]["resolved_id"])
            if dependency_source is not None or not action.get("precondition"):
                action["precondition"] = {
                    "artifact_sha1": artifact_index.canonical_sha1(snapshot) if snapshot is not None else None,
                    "snapshot": store.write_sidecar(self.ws, self.run["id"], snapshot) if snapshot is not None else None,
                }
        if definition.risk not in {"read", "compute"}:
            action["postcondition"] = actions.expected_postcondition(action)
        ledger.transition(action, "running")
        action["attempts"] += 1; action["prepared_at"] = store.utcnow(); action["started_at"] = store.utcnow()
        self.run["usage"]["actions_started"] += 1
        self._save_action(action)
        # Only the executor call may route to the failure path. Once the
        # action commits as succeeded, post-success bookkeeping and planning
        # waves must never flip its outcome — succeeded is terminal.
        try:
            receipt = definition.executor(self.ws, action, self.run)
        except Exception as error:
            action["error"] = str(error); action["finished_at"] = store.utcnow()
            ledger.transition(action, "failed")
            attempts = int(self.run["limits"].get("max_execution_attempts", 2))
            if action["attempts"] < attempts and definition.failure_policy != "stop_run":
                ledger.transition(action, "ready")
            self._save_action(action)
            if action.get("planning_significant") and action["status"] == "failed":
                self._expand_after(action, {"error": str(error)})
            if definition.failure_policy == "stop_run":
                raise
            return
        action["receipt"] = receipt
        action["result_refs"] = list(receipt.get("result_refs") or [])
        action["finished_at"] = store.utcnow()
        ledger.transition(action, "succeeded")
        self.run["artifacts"].extend(
            {"kind": ref.partition(":")[0], "id": ref.partition(":")[2], "semantic_id": "", "action": "updated"}
            for ref in action["result_refs"]
            if not any(item.get("kind") == ref.partition(":")[0] and item.get("id") == ref.partition(":")[2] for item in self.run["artifacts"])
        )
        self._save_action(action)
        warning = (receipt.get("result") or {}).get("warning")
        if warning:
            self.warn(str(warning))
        for ref in action["result_refs"]:
            kind, _, item_id = ref.partition(":")
            self.emit("workspace_changed", {"kind": kind, "id": item_id, "action": "removed" if definition.risk == "destructive" else "updated"})
        if action.get("planning_significant"):
            self._expand_after(action)

    def _expand_after(self, action: dict, safe_result: dict | None = None) -> None:
        usage = self.run["usage"]
        if usage["planner_waves"] >= int(self.run["limits"].get("max_waves", 8)):
            self.warn("Planning-wave limit reached; the current graph will finish without further expansion.")
            return
        safe_result = safe_result if safe_result is not None else ((action.get("receipt") or {}).get("result") or {})
        usage["planner_waves"] += 1; self.save()
        index = artifact_index.build(self.ws)
        base_user = prompts.command_planner_user(
            self.run["goal"],
            [{"id": item["id"], "type": item["type"], "status": item["status"], "result_refs": item["result_refs"]} for item in self.run["actions"]],
            [{"action_id": action["id"], "result": safe_result}],
            artifact_index.compact(index), self._catalog(), self.run["limits"],
        )
        attempt_user = base_user
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            payload = self.llm_json(prompts.COMMAND_PLANNER_SYSTEM, attempt_user)
            proposals = payload.get("actions") or []
            if not proposals:
                return
            # Expansion is opportunistic follow-up planning; even repeated
            # invalid proposals must not fail work that already committed.
            try:
                created = ledger.append_actions(
                    self.run, proposals, depth=action["depth"] + 1,
                    audit_lifecycle=self._is_full_audit_goal(),
                )
            except WorkspaceError as error:
                self._record_rejected_proposals("command_planner", proposals, error)
                if attempt + 1 >= SEMANTIC_PROPOSAL_ATTEMPTS:
                    self.warn(f"Discarded an invalid planning proposal: {error}")
                    return
                attempt_user = self._proposal_repair_user(base_user, payload, error)
                continue
            self.save()
            self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})
            return

    def _wait_interaction(self, action: dict, interaction: dict) -> None:
        self.set_status("awaiting_input" if interaction["type"] in {"clarification", "target_choice", "conflict_resolution"} else "awaiting_approval")
        waited_from = time.monotonic()
        response = None
        while response is None:
            if self.handle.cancel.is_set():
                raise Cancelled()
            if self.handle.command_queued.is_set():
                self._drain_inbox()
            if self.handle.interaction_resolved.wait(0.05):
                self.handle.interaction_resolved.clear()
                response = self.handle.interaction_responses.pop(interaction["id"], None)
        self.deadline += time.monotonic() - waited_from
        decision = str(response.get("decision") or response.get("choice") or "").strip()
        interaction.update(status="resolved", response=response, actor="auditor", resolved_at=store.utcnow())
        if interaction["type"] == "clarification":
            text = str(response.get("text") or "").strip()
            if not text:
                raise WorkspaceError("A clarification response is required.")
            action["target"]["selector"] = text; ledger.transition(action, "proposed")
        elif interaction["type"] == "target_choice":
            choice = str(response.get("choice") or "")
            option = next((item for item in interaction["options"] if item.get("ref") == choice or item.get("id") == choice), None)
            if option is None:
                raise WorkspaceError("Choose one of the offered targets.")
            action["target"]["resolved_id"] = option["id"]; action["target"]["selector"] = None
            ledger.transition(action, "proposed")
        elif interaction["type"] in {"confirmation", "proposal_approval"}:
            if decision == "approve":
                if isinstance(response.get("args"), dict):
                    action["args"] = response["args"]; actions.validate_action(action)
                ledger.transition(action, "ready")
            else:
                ledger.transition(action, "skipped")
        else:
            if decision == "retry":
                action["resolution"] = None; action["precondition"] = None
                ledger.transition(action, "proposed")
            else:
                ledger.transition(action, "skipped")
        self.set_status("executing")
        self._save_action(action)
        self.emit("interaction_resolved", {"interaction_id": interaction["id"], "action_id": action["id"], "decision": decision})

    def _block_failed_dependencies(self) -> None:
        by_id = {action["id"]: action for action in self.run["actions"]}
        changed = False
        for action in self.run["actions"]:
            if action["status"] not in {"proposed", "ready"}:
                continue
            if any(by_id[dep]["status"] in {"failed", "blocked", "cancelled", "skipped"} for dep in action["depends_on"]):
                ledger.transition(action, "blocked"); action["error"] = "A required action did not succeed."; changed = True
        if changed:
            ledger.project_legacy_plan(self.run); self.save()

    def _dependencies_succeeded(self, action: dict) -> bool:
        by_id = {item["id"]: item for item in self.run["actions"]}
        return all(by_id[value]["status"] == "succeeded" for value in action["depends_on"])

    def _dependency_post_state_source(self, action: dict, resolution: dict) -> dict | None:
        """Identify a dependency that produced the target's current version.

        Proposed actions are resolved before ready actions execute, so two
        dependent mutations of the same artifact initially retain the same
        optimistic snapshot.  The second action may safely rebase only when
        the current artifact exactly matches a succeeded dependency's durable
        receipt; any later auditor or external edit still becomes a conflict.
        """
        current_sha1 = resolution.get("sha1")
        target_ref = resolution.get("resolved_ref")
        if not current_sha1 or not target_ref:
            return None
        by_id = {item["id"]: item for item in self.run.get("actions") or []}
        pending = list(action.get("depends_on") or [])
        seen = set()
        while pending:
            dependency_id = pending.pop()
            if dependency_id in seen or dependency_id not in by_id:
                continue
            seen.add(dependency_id)
            dependency = by_id[dependency_id]
            receipt = dependency.get("receipt") or {}
            if (
                dependency.get("status") == "succeeded"
                and receipt.get("post_sha1") == current_sha1
                and target_ref in (dependency.get("result_refs") or [])
            ):
                return dependency
            pending.extend(dependency.get("depends_on") or [])
        return None

    def _action(self, action_id: str) -> dict:
        return next(item for item in self.run["actions"] if item["id"] == action_id)

    def _save_action(self, action: dict) -> None:
        ledger.project_legacy_plan(self.run); self.save()
        self.emit("action_update", {"action": {k: action.get(k) for k in ("id", "type", "status", "error", "result_refs", "attempts")}})

    def _finish(self, force_issue: bool = False) -> None:
        if self.run["status"] in store.TERMINAL_STATUSES:
            return
        self._drain_inbox()
        self.set_status("verifying")
        failed = [item for item in self.run.get("actions") or [] if item["status"] in {"failed", "blocked", "cancelled"}]
        succeeded = [item for item in self.run.get("actions") or [] if item["status"] == "succeeded"]
        skipped = [item for item in self.run.get("actions") or [] if item["status"] == "skipped"]
        lines = [f"## Command result", "", self.run["goal"].get("objective") or self.run["command"].get("text") or "Audit command", ""]
        lines.append(f"Completed {len(succeeded)} action(s); {len(failed)} failed or blocked; {len(skipped)} skipped.")
        if succeeded:
            lines.extend(["", "### Committed work", *[f"- {actions.REGISTRY.get(item['type'], item['definition_version']).description}" for item in succeeded]])
        if failed:
            lines.extend(["", "### Issues", *[f"- {item['type']}: {item.get('error') or item['status']}" for item in failed]])
        lines.extend(["", "This is assistant working content, not a formal audit-stage conclusion or audit opinion."])
        self.run["summary_markdown"] = "\n".join(lines)
        self.run["finished"] = store.utcnow(); self.run["command"]["status"] = "completed"
        status = "completed_with_issues" if force_issue or failed else "completed"
        self.set_status(status); self.emit("summary_ready", {"run_id": self.run["id"]})
