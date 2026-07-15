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


class CommandRunner(BaseRunner):
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
            self.warn(str(error))
            self._finish(force_issue=True)
        except Exception as error:
            self.run["error"] = str(error)
            self.run["finished"] = store.utcnow()
            self.set_status("failed")

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
        payload = self.llm_json(
            prompts.COMMAND_INTERPRETER_SYSTEM,
            prompts.command_interpreter_user(command, template, artifact_index.compact(index), self._catalog(), self.run["limits"]),
        )
        objective = str(payload.get("objective") or (template or {}).get("objective") or command.get("text") or "").strip()
        self.run["goal"] = {
            "objective": objective,
            "constraints": [str(value) for value in payload.get("constraints") or (template or {}).get("constraints") or []],
            "completion_criteria": [str(value) for value in payload.get("completion_criteria") or []],
        }
        proposals = payload.get("actions") or []
        if not isinstance(proposals, list):
            raise WorkspaceError("Command interpreter actions must be a list.")
        created = ledger.append_actions(self.run, proposals)
        if payload.get("needs_planning_wave") and not any(item.get("planning_significant") for item in created):
            for item in reversed(created):
                definition = actions.REGISTRY.get(item["type"], item["definition_version"])
                if definition.risk in {"read", "compute"}:
                    item["planning_significant"] = True
                    break
        self.run["command"]["status"] = "planned"
        self.save()
        self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})

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

    def _resolve_and_gate(self, action: dict) -> None:
        definition = actions.validate_action(action)
        target = action["target"]
        if definition.target_kinds:
            if not target.get("kind") and len(definition.target_kinds) == 1:
                target["kind"] = definition.target_kinds[0]
            defaults = {"planning": "apm", "report": "working"}
            if not target.get("resolved_id") and target.get("kind") in defaults and not target.get("selector"):
                target["resolved_id"] = defaults[target["kind"]]
            index = artifact_index.build(self.ws)
            resolution = artifact_index.resolve(index, target["kind"], target.get("selector"), target.get("resolved_id"))
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
            if definition.risk not in {"read", "compute"}:
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
        # Re-resolve immediately before mutations and capture a hashed before snapshot.
        if definition.target_kinds:
            index = artifact_index.build(self.ws)
            resolution = artifact_index.resolve(index, action["target"]["kind"], None, action["target"].get("resolved_id"))
            previous = action.get("resolution") or {}
            if not resolution.get("resolved_id") or (previous.get("sha1") and previous.get("sha1") != resolution.get("sha1")):
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
            if not action.get("precondition"):
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
        try:
            receipt = definition.executor(self.ws, action, self.run)
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

    def _expand_after(self, action: dict, safe_result: dict | None = None) -> None:
        usage = self.run["usage"]
        if usage["planner_waves"] >= int(self.run["limits"].get("max_waves", 8)):
            self.warn("Planning-wave limit reached; the current graph will finish without further expansion.")
            return
        safe_result = safe_result if safe_result is not None else ((action.get("receipt") or {}).get("result") or {})
        usage["planner_waves"] += 1; self.save()
        index = artifact_index.build(self.ws)
        payload = self.llm_json(
            prompts.COMMAND_PLANNER_SYSTEM,
            prompts.command_planner_user(
                self.run["goal"],
                [{"id": item["id"], "type": item["type"], "status": item["status"], "result_refs": item["result_refs"]} for item in self.run["actions"]],
                [{"action_id": action["id"], "result": safe_result}],
                artifact_index.compact(index), self._catalog(), self.run["limits"],
            ),
        )
        proposals = payload.get("actions") or []
        if proposals:
            created = ledger.append_actions(self.run, proposals, depth=action["depth"] + 1)
            self.save(); self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})

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
