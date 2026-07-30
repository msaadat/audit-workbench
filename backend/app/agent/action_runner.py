"""Durable unified action-graph runner."""

from __future__ import annotations

import json
import time

from .. import analytics, assistant, debug_store, llm, sandbox, validation
from ..workspaces import Workspace, WorkspaceError
from . import action_tools, actions, artifact_index, ledger, narration, prompts, routing, store
from .base import BaseRunner, Cancelled, LimitExceeded
from .runtime import RunRuntime

# Goal templates, deterministic phrase tables, and the workflow-ownership rule
# all live in `agent/routing.py`. The action scheduler classifies nothing; it
# only re-asks routing whether a record it was handed is one it may plan.
GOAL_TEMPLATES = routing.GOAL_TEMPLATES

SEMANTIC_PROPOSAL_ATTEMPTS = 2


def _text_list(value: object) -> list[str]:
    values = [value] if isinstance(value, str) else list(value or [])
    return [str(item) for item in values]


class ActionRunner(BaseRunner):
    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
    ):
        """Create an action scheduler with an injectable per-run runtime.

        The optional dependency preserves the existing three-argument
        construction API while allowing the scheduler to be composed with a
        runtime supplied by its caller.
        """
        super().__init__(workspace, run, handle, runtime=runtime)

    def _drain_inbox(self) -> None:
        """General chat is durable follow-up work, never active-graph steering."""
        self.runtime.drain_inbox(queue_commands=True)

    def execute(self) -> None:
        """Interpret the command into an action graph, then drive it to done.

        Re-entrant: a resumed run finds its actions already on the ledger and
        skips straight to driving the graph.
        """
        if not self.run.get("started"):
            self.mark_started()
        try:
            self._guard_isolated_action_request()
            self._recover_running_actions()
            if not self.run.get("actions"):
                self._interpret()
            self._drive_graph()
            self._finish()
        except Cancelled:
            for action in self.run.get("actions") or []:
                if action["status"] in {
                    "proposed", "ready", "awaiting_input", "awaiting_confirmation", "blocked",
                }:
                    ledger.transition(action, "cancelled")
            ledger.project_action_plan(self.run)
            self.mark_finished()
            self.run["command"]["status"] = "cancelled"
            context = dict(self.handle.cancel_context or {})
            self.run["cancellation"] = {
                "actor": context.get("actor") or "orchestrator",
                "source": context.get("source") or "checkpoint",
                "reason": context.get("reason"),
                "requested_at": context.get("requested_at"),
                "cancelled_at": self.run["finished"],
            }
            self.set_status("cancelled")
        except (LimitExceeded, llm.LLMError) as error:
            self.warn(str(error))
            self._finish(force_issue=True)
        except Exception as error:
            self._fail_run(str(error))

    def _guard_isolated_action_request(self) -> None:
        """Reject workflow-owned requests before planning or action execution.

        Routing normally selects ``WorkflowRunner`` before launch. This is the
        defensive boundary for a malformed record or a bounded-router miss: a
        request for a workflow-owned deliverable must fail without invoking the
        action interpreter or executing a pre-populated action graph. The rule
        is the routing module's own classification, so the guard and the
        persisted route can never disagree.
        """
        if routing.workflow_owned_request(self.run.get("command") or {}):
            raise WorkspaceError(
                "Broad audit and planning requests must use workflow routing; "
                "the action runner accepts only isolated artifact operations."
            )

    def _fail_run(self, error: str) -> None:
        self.run["error"] = error
        self.mark_finished()
        self.run["command"]["status"] = "failed"
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
            if value.type not in {
                "create_procedure", "edit_procedure", "delete_procedure",
                "generate_working_paper",
            }
        ]

    def _table_profiles(self) -> list[dict]:
        profiles = []
        for name in self.ws.table_names():
            try:
                profiles.append(assistant.table_metadata(self.ws, name))
            except Exception as error:
                self.warn(f"Could not profile '{name}' for command planning: {error}")
        return profiles

    def _command_interpreter_json(self, user: str) -> dict:
        """Run a bounded local-read tool loop for one command proposal."""
        session = action_tools.ActionToolSession(self.ws, self._catalog())
        conversation = [{"role": "user", "content": user}]
        tool_calls = 0
        parse_attempts = 0
        tool_limit_announced = False
        while True:
            message = self._llm_message(
                prompts.COMMAND_INTERPRETER_SYSTEM,
                conversation,
                action_tools.TOOL_SCHEMAS,
                attempt=tool_calls + parse_attempts + 1,
            )
            raw_calls = message.get("tool_calls")
            calls = raw_calls if isinstance(raw_calls, list) else []
            conversation.append({
                "role": "assistant",
                "content": str(message.get("content") or ""),
                **({"tool_calls": calls} if calls else {}),
            })
            if not calls:
                try:
                    return prompts.parse_json_object(str(message.get("content") or ""))
                except (ValueError, json.JSONDecodeError) as error:
                    parse_attempts += 1
                    if parse_attempts >= 2:
                        raise llm.LLMError(f"The model did not return usable JSON: {error}") from error
                    conversation.append({
                        "role": "user",
                        "content": (
                            "Your previous response could not be used: "
                            f"{error}. {prompts.JSON_RULES}"
                        ),
                    })
                    continue
            if tool_limit_announced:
                raise llm.LLMError(
                    "Action-planning tool-call limit reached before a usable action graph was returned."
                )
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else {}
                name = str(function.get("name") or "") if isinstance(function, dict) else ""
                raw_args = function.get("arguments") if isinstance(function, dict) else "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        raise ValueError("Tool arguments must be an object.")
                except (TypeError, ValueError, json.JSONDecodeError):
                    args = {}
                tool_calls += 1
                if tool_calls <= action_tools.MAX_TOOL_CALLS:
                    self.set_activity(
                        "command.interpret",
                        action_tools.describe_tool_call(name, args),
                        detail="Reviewing the command, available artifacts, and table schemas…",
                    )
                    result = session.dispatch(name, args)
                else:
                    result = {"error": "Action-planning tool-call limit reached; return the action graph now."}
                conversation.append({
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or f"action-tool-{tool_calls}"),
                    "content": json.dumps(result, default=str),
                })
            self.run.setdefault("usage", {}).setdefault("tool_calls", 0)
            self.run["usage"]["tool_calls"] += len(calls)
            self.save()
            if tool_calls >= action_tools.MAX_TOOL_CALLS:
                tool_limit_announced = True
                conversation.append({
                    "role": "user",
                    "content": "The local read limit is reached. Return the complete action graph now without more tool calls.",
                })

    def _interpret(self) -> None:
        """Turn one command into a validated action DAG through bounded reads.

        The interpreter starts from a compact manifest and uses only the local
        read tools it needs. Everything after its proposal remains deterministic
        — the ledger, not the model, decides what is legal and in what order it
        runs.
        """
        self.set_status("interpreting")
        self.set_activity(
            "command.interpret", "Preparing the action plan",
            detail="Reviewing the command, available artifacts, and table schemas…",
        )
        command = self.run["command"]
        template = GOAL_TEMPLATES.get(command.get("goal_template"))
        if command.get("goal_template") and template is None:
            raise WorkspaceError("Unknown goal template.")
        base_user = prompts.command_interpreter_user(
            command, template, action_tools.workspace_manifest(self.ws), self.run["limits"],
        )
        # One repair round: a batch rejected by the action contracts or graph
        # validator is fed back with the specific error rather than discarded.
        attempt_user = base_user
        created = []
        payload = {}
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            if attempt:
                self.set_activity(
                    "command.interpret.repair", "Repairing the action plan",
                    detail="The first proposal did not satisfy the registered action contracts.",
                    attempt=attempt + 1,
                )
            payload = self._command_interpreter_json(attempt_user)
            objective = str(payload.get("objective") or (template or {}).get("objective") or command.get("text") or "").strip()
            goal = {
                "objective": objective,
                "constraints": _text_list(
                    payload.get("constraints") or (template or {}).get("constraints") or []
                ),
                "completion_criteria": _text_list(payload.get("completion_criteria")),
            }
            self.run["goal"] = goal
            proposals = payload.get("actions") or []
            try:
                if not isinstance(proposals, list):
                    raise WorkspaceError("Command interpreter actions must be a list.")
                self._canonicalize_proposals(proposals)
                created = ledger.append_actions(self.run, proposals)
                break
            except WorkspaceError as error:
                self._record_rejected_proposals("command_interpreter", proposals, error)
                if attempt + 1 >= SEMANTIC_PROPOSAL_ATTEMPTS:
                    raise
                attempt_user = self._proposal_repair_user(base_user, payload, error)
        # The model asked for a replanning wave but marked nothing significant;
        # promote the last read/compute action so the wave can actually fire.
        if payload.get("needs_planning_wave") and not any(item.get("planning_significant") for item in created):
            for item in reversed(created):
                definition = actions.REGISTRY.get(item["type"], item["definition_version"])
                if definition.risk in {"read", "compute"}:
                    item["planning_significant"] = True
                    break
        self.run["command"]["status"] = "planned"
        self.save()
        self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})
        self.set_activity(
            "command.plan.ready", "Action plan ready",
            detail=f"Prepared {len(created)} action{'s' if len(created) != 1 else ''} for execution.",
            current=0, total=len(created),
        )

    def _resolve_rcm_refs(self, refs: list) -> list[str]:
        resolved = []
        for ref in refs:
            value = str(ref)
            row = next(
                (
                    item for item in self.ws.rcm
                    if item.get("id") == value
                    or f"rcm:{item.get('id')}" == value
                    or item.get("semantic_id") == value
                ),
                None,
            )
            if row and row["id"] not in resolved:
                resolved.append(row["id"])
        return resolved

    @staticmethod
    def _proposal_repair_user(base_user: str, payload: dict, error: Exception) -> str:
        validation_guidance = ""
        if "Unknown check" in str(error):
            validation_guidance = (
                " Supported validation check ids are: "
                f"{', '.join(validation.CHECKS)}. Use `required` for null or blank checks."
            )
        analytics_guidance = ""
        if "Unknown analytics test" in str(error):
            analytics_guidance = (
                " Supported analytics test ids are: "
                f"{', '.join(analytics.ANALYTICS)}. Replace every unsupported run_analytics "
                "action with a supported library test or create_custom_analysis; do not only fix "
                "the first named test."
            )
        custom_code_guidance = ""
        if "custom analysis code" in str(error).casefold():
            custom_code_guidance = (
                " Correct every create_custom_analysis snippet, not only the first one. `pl`, "
                "workspace table variables, and `tables['name']` are already available. Remove "
                "all imports and file reads/writes, and assign one summarized DataFrame to `result`."
            )
        return (
            f"{base_user}\n\nYour previous JSON parsed, but its action graph violated the registered "
            f"contract: {error}.{validation_guidance}{analytics_guidance}{custom_code_guidance} "
            f"Return a corrected complete JSON object. "
            f"Preserve the intended goal and "
            f"valid dependencies, use only catalog target kinds and required args. Previous JSON: "
            f"{json.dumps(payload, default=str)}"
        )

    def _canonicalize_proposals(self, proposals: list[object]) -> None:
        """Normalize a proposal batch and report all invented analytics ids."""
        unknown_analytics = set()
        for proposal in proposals:
            if not isinstance(proposal, dict) or proposal.get("type") != "run_analytics":
                continue
            args = proposal.get("args")
            if not isinstance(args, dict):
                continue
            if "test" not in args:
                continue
            test_id = analytics.canonical_test_id(args.get("test"))
            args["test"] = test_id
            if test_id and test_id not in analytics.ANALYTICS:
                unknown_analytics.add(test_id)
        if unknown_analytics:
            names = ", ".join(sorted(unknown_analytics))
            raise WorkspaceError(f"Unknown analytics tests: {names}.")
        for proposal in proposals:
            if isinstance(proposal, dict):
                actions.canonicalize_action_fields(self.ws, proposal)

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
            ledger.project_action_plan(self.run); self.save()

    def _drive_graph(self) -> None:
        """Single-threaded scheduler over the action ledger.

        Each pass re-reads the ledger and takes the highest-priority eligible
        item, so the loop stays correct after a resume, an out-of-band
        interaction response, or a wave that appended new actions mid-run:

            1. an unanswered interaction blocks everything else
            2. a proposed action gets resolved and gated (target, approval)
            3. a ready action whose dependencies all succeeded executes
            4. otherwise wait for in-flight work, or finish
        """
        self.set_status("executing")
        while True:
            self.checkpoint()
            self._block_failed_dependencies()
            # An interaction may have been made obsolete by a dependency that
            # ran after it was raised; dismissing it resumes without the auditor.
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
                    and dependency["type"] == "edit_report"
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
        adjustments = actions.normalize_created_targets(self.run, [action])
        if adjustments:
            self.run.setdefault("target_adjustments", []).extend(adjustments)
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
        actions.canonicalize_action_fields(self.ws, action)
        definition = actions.REGISTRY.get(action["type"], action["definition_version"])
        # Also normalize pre-fix persisted proposals when an interrupted run
        # is resumed. New proposals are normalized by the ledger.
        adjustments = actions.normalize_created_targets(self.run, [action])
        if adjustments:
            self.run.setdefault("target_adjustments", []).extend(adjustments)
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
        """Run one action under an optimistic concurrency check.

        The target was already resolved during gating, but the workspace may
        have moved since — other runs, the UI, and earlier actions all write to
        it. So the target is re-resolved and re-hashed immediately before the
        executor runs, and a drift this run cannot explain becomes a conflict
        interaction rather than a silent overwrite.
        """
        definition = actions.validate_action(action)
        self.checkpoint()
        # Classification is model work owned by the intake runner; borrow it
        # rather than duplicating the registered worker, its declared context,
        # and the batch bookkeeping here. The borrowed runner shares this run's
        # runtime and ledger lock, so both write the one durable record under
        # one lock and one model budget.
        if action["type"] == "classify_import_batch":
            from .. import intake
            from .intake_runner import IntakeRunner

            batch = intake.load_batch(self.ws, action["args"]["batch_id"])
            IntakeRunner(
                self.ws,
                self.run,
                self.handle,
                runtime=self.runtime,
                state_lock=self._state_lock,
            )._classify(batch)
            intake.save_batch(self.ws, batch)
        # Re-resolve immediately before mutations and capture a hashed before snapshot.
        if definition.target_kinds:
            index = artifact_index.build(self.ws)
            resolution = artifact_index.resolve(index, action["target"]["kind"], None, action["target"].get("resolved_id"))
            previous = action.get("resolution") or {}
            resolution_changed = bool(
                previous.get("sha1") and previous.get("sha1") != resolution.get("sha1")
                and definition.risk not in {"read", "compute"}
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
        terminal_statuses = {"succeeded", "failed", "blocked", "skipped", "cancelled"}
        completed_before = sum(
            item.get("status") in terminal_statuses for item in self.run.get("actions") or []
        )
        resolution = action.get("resolution") or {}
        self.set_activity(
            "actions.execute", definition.description,
            detail=str(resolution.get("title") or action["type"]).replace("_", " "),
            current=completed_before + 1,
            total=len(self.run.get("actions") or []),
            attempt=int(action.get("attempts") or 0) + 1,
            action_id=action["id"],
        )
        ledger.transition(action, "running")
        action["attempts"] += 1
        action["error"] = None
        action["finished_at"] = None
        action["prepared_at"] = store.utcnow()
        action["started_at"] = store.utcnow()
        self.run["usage"]["actions_started"] += 1
        self._save_action(action)
        # Only the executor call may route to the failure path. Once the
        # action commits as succeeded, post-success bookkeeping and planning
        # waves must never flip its outcome — succeeded is terminal.
        try:
            debug_store.capture_structural_state(
                self.ws, trigger=f"pre_action:{action['id']}", run_id=self.run["id"]
            )
        except Exception as snapshot_error:
            self.warn(f"Debug pre-action snapshot was unavailable: {snapshot_error}")
        try:
            with debug_store.trace_context(
                workspace_id=self.ws.id, workspace_root=str(self.ws.root), run_id=self.run["id"],
                action_id=action["id"], stage="actions.execute",
                purpose=action["type"], trigger=f"action:{action['id']}",
                artifact_refs=list(action.get("result_refs") or []),
            ):
                receipt = definition.executor(self.ws, action, self.run)
        except Exception as error:
            try:
                debug_store.capture_structural_state(
                    self.ws, trigger=f"post_action_failed:{action['id']}", run_id=self.run["id"]
                )
            except Exception:
                pass
            # Retry policy turns on whether the failure is deterministic: a
            # WorkspaceError/ValueError means the same call would fail again, so
            # only generated Polars code (which can be repaired) earns a second
            # attempt. Deterministic failures also skip the replanning wave —
            # the model has nothing new to react to.
            action["error"] = str(error); action["finished_at"] = store.utcnow()
            ledger.transition(action, "failed")
            attempts = int(self.run["limits"].get("max_execution_attempts", 2))
            custom_repair = action["type"] in {"create_custom_analysis", "edit_custom_analysis"}
            deterministic = isinstance(error, (WorkspaceError, ValueError)) and not custom_repair
            retry = (
                not deterministic
                and action["attempts"] < attempts
                and definition.failure_policy != "stop_run"
            )
            if action["attempts"] < attempts and custom_repair:
                retry = self._repair_custom_analysis(action, error)
            if retry:
                ledger.transition(action, "ready")
            self._save_action(action)
            self.set_activity(
                "actions.retry" if retry else "actions.failed",
                "Retrying an action" if retry else "Action failed; continuing safely",
                detail=str(error),
                current=completed_before + (0 if retry else 1),
                total=len(self.run.get("actions") or []),
                attempt=action["attempts"], action_id=action["id"],
            )
            if (
                action.get("planning_significant")
                and action["status"] == "failed"
                and not deterministic
            ):
                self._expand_after(action, {"error": str(error)})
            elif deterministic:
                self.warn(
                    f"Skipped adaptive replanning after deterministic failure in "
                    f"{action['id']}: {error}"
                )
            if definition.failure_policy == "stop_run":
                raise
            return
        try:
            debug_store.capture_structural_state(
                self.ws, trigger=f"post_action:{action['id']}", run_id=self.run["id"]
            )
        except Exception as snapshot_error:
            self.warn(f"Debug post-action snapshot was unavailable: {snapshot_error}")
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
        completed_now = sum(
            item.get("status") in terminal_statuses for item in self.run.get("actions") or []
        )
        self.set_activity(
            "actions.progress", "Action completed",
            detail=definition.description,
            current=completed_now, total=len(self.run.get("actions") or []),
            action_id=action["id"],
        )
        warning = (receipt.get("result") or {}).get("warning")
        if warning:
            self.warn(str(warning))
        for ref in action["result_refs"]:
            kind, _, item_id = ref.partition(":")
            self.emit("workspace_changed", {"kind": kind, "id": item_id, "action": "removed" if definition.risk == "destructive" else "updated"})
        if action.get("planning_significant"):
            self._expand_after(action)

    def _repair_custom_analysis(self, action: dict, error: Exception) -> bool:
        """Replace failed custom code before the executor's one permitted retry."""
        args = action.get("args") or {}
        if action["type"] == "create_custom_analysis":
            spec = args.get("spec") or {}
        else:
            spec = (args.get("changes") or {}).get("spec") or {}
        code = str(spec.get("code") or "")
        try:
            payload = self.llm_json(
                prompts.FIX_CODE_SYSTEM,
                prompts.fix_code_user(
                    code,
                    str(error),
                    {"tables": assistant.schema_brief(self.ws)},
                ),
            )
            repaired = str(payload.get("code") or "").strip()
            if not repaired:
                return False
            # Reject an unsafe/non-result repair now instead of executing the
            # same deterministic failure as a nominal second attempt.
            sandbox.validate(repaired)
        except Exception as repair_error:
            self.warn(f"Custom analysis repair failed for {action['id']}: {repair_error}")
            return False
        spec["code"] = repaired
        return True

    def _expand_after(self, action: dict, safe_result: dict | None = None) -> None:
        """Run one adaptive planning wave against a locally computed result.

        The only place the graph grows in response to data. Bounded twice — by
        `max_waves` and by the remaining action budget — and fed a *safe*
        result (aggregates, never raw rows), which is also why a failure passes
        `{"error": ...}` rather than the receipt.
        """
        if self.run.get("planning_expansion_disabled"):
            return
        usage = self.run["usage"]
        if usage["planner_waves"] >= int(self.run["limits"].get("max_waves", 8)):
            self.warn("Planning-wave limit reached; the current graph will finish without further expansion.")
            return
        remaining_actions = max(
            0,
            int(self.run["limits"].get("max_actions", 60))
            - len(self.run.get("actions") or []),
        )
        if remaining_actions == 0:
            self.run["planning_expansion_disabled"] = True
            self.warn("Action limit reached; adaptive planning is disabled for this run.")
            return
        safe_result = safe_result if safe_result is not None else ((action.get("receipt") or {}).get("result") or {})
        usage["planner_waves"] += 1; self.save()
        failed_result = bool(safe_result.get("error"))
        max_waves = int(self.run["limits"].get("max_waves", 8))
        self.set_activity(
            "command.replan" if failed_result else "command.expand",
            "Replanning after an action failure" if failed_result else "Reviewing results for follow-up work",
            detail=f"Planning wave {usage['planner_waves']} of {max_waves}…",
            current=usage["planner_waves"], total=max_waves, action_id=action["id"],
        )
        index = artifact_index.build(self.ws)
        base_user = prompts.command_planner_user(
            self.run["goal"],
            [{"id": item["id"], "type": item["type"], "status": item["status"], "result_refs": item["result_refs"]} for item in self.run["actions"]],
            [{"action_id": action["id"], "result": safe_result}],
            artifact_index.compact(index), self._catalog(),
            {**self.run["limits"], "remaining_actions": remaining_actions},
            assistant.schema_brief(self.ws),
            self._table_profiles(),
        )
        attempt_user = base_user
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            if attempt:
                self.set_activity(
                    "command.replan.repair", "Repairing the follow-up action plan",
                    detail="The previous follow-up proposal did not satisfy the action contracts.",
                    attempt=attempt + 1, action_id=action["id"],
                )
            try:
                payload = self.llm_json(prompts.COMMAND_PLANNER_SYSTEM, attempt_user)
            except (LimitExceeded, llm.LLMError) as error:
                # The initial graph is already validated and may contain
                # independent local work. Adaptive expansion is optional, so
                # a provider outage must not strand that work in ready state.
                self.run["planning_expansion_disabled"] = True
                self.warn(f"Further planning expansion skipped: {error}")
                return
            proposals = payload.get("actions") or []
            if not proposals:
                return
            # Expansion is opportunistic follow-up planning; even repeated
            # invalid proposals must not fail work that already committed.
            try:
                self._canonicalize_proposals(proposals)
                created = ledger.append_actions(
                    self.run, proposals, depth=action["depth"] + 1,
                )
            except WorkspaceError as error:
                self._record_rejected_proposals("command_planner", proposals, error)
                if attempt + 1 >= SEMANTIC_PROPOSAL_ATTEMPTS:
                    signature = str(error)
                    failures = self.run.setdefault("planning_failure_signatures", {})
                    failures[signature] = int(failures.get(signature) or 0) + 1
                    if failures[signature] >= 2:
                        self.run["planning_expansion_disabled"] = True
                    self.warn(f"Discarded an invalid planning proposal: {error}")
                    return
                attempt_user = self._proposal_repair_user(base_user, payload, error)
                continue
            self.save()
            self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})
            return

    def _wait_interaction(self, action: dict, interaction: dict) -> None:
        waiting_status = (
            "awaiting_input"
            if interaction["type"]
            in {"clarification", "target_choice", "conflict_resolution"}
            else "awaiting_approval"
        )
        response = self.runtime.wait_for_interaction(
            interaction,
            waiting_status=waiting_status,
            queue_commands=True,
            poll_interval=0.05,
        )
        decision = str(response.get("decision") or response.get("choice") or "").strip()
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
        self.runtime.resolve_interaction(
            interaction,
            response,
            event_data={"action_id": action["id"], "decision": decision},
            persist=lambda: self._save_action(action),
        )

    def _block_failed_dependencies(self) -> None:
        by_id = {action["id"]: action for action in self.run["actions"]}
        changed = False
        for action in self.run["actions"]:
            if action["status"] not in {"proposed", "ready"}:
                continue
            if any(by_id[dep]["status"] in {"failed", "blocked", "cancelled", "skipped"} for dep in action["depends_on"]):
                ledger.transition(action, "blocked"); action["error"] = "A required action did not succeed."; changed = True
        if changed:
            ledger.project_action_plan(self.run); self.save()
            blocked = sum(item["status"] == "blocked" for item in self.run["actions"])
            self.set_activity(
                "actions.blocked", "Blocking dependent actions",
                detail=f"{blocked} action{'s are' if blocked != 1 else ' is'} blocked by earlier failures.",
            )

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
        ledger.project_action_plan(self.run); self.save()
        self.emit("action_update", {"action": {k: action.get(k) for k in ("id", "type", "status", "error", "result_refs", "attempts")}})

    def _finish(self, force_issue: bool = False) -> None:
        if self.run["status"] in store.TERMINAL_STATUSES:
            return
        self._drain_inbox()
        self.set_status("verifying")
        self.set_activity(
            "command.verify", "Verifying fieldwork results",
            detail="Checking committed, failed, blocked, and skipped actions before summary.",
        )
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
        narration.milestone(
            self.run,
            self.emit,
            capability="action.command",
            stage_id="action:result",
            status="completed_with_issues" if force_issue or failed else "completed",
            headline="Requested action complete",
            summary=(
                f"Completed {len(succeeded)} action(s); {len(failed)} failed or "
                f"were blocked; {len(skipped)} were skipped."
            ),
            metrics=[
                {"label": "Completed", "value": len(succeeded)},
                {"label": "Failed or blocked", "value": len(failed)},
                {"label": "Skipped", "value": len(skipped)},
            ],
            highlights=[
                {
                    "severity": "error",
                    "label": str(item.get("type") or "Action"),
                    "detail": str(item.get("error") or item.get("status") or ""),
                }
                for item in failed[:3]
            ],
            artifact_refs=[
                ref for item in succeeded for ref in item.get("result_refs") or []
            ],
        )
        self.mark_finished(); self.run["command"]["status"] = "completed"
        status = "completed_with_issues" if force_issue or failed else "completed"
        self.set_status(status); self.emit("summary_ready", {"run_id": self.run["id"]})
