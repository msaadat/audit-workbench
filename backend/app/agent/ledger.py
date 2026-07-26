"""Action-ledger records, transitions, graph validation, and projections."""

from __future__ import annotations

import hashlib
import json
import uuid

from ..workspaces import WorkspaceError
from . import actions, store

ACTION_STATUSES = {
    "proposed", "awaiting_input", "awaiting_confirmation", "ready", "blocked",
    "running", "succeeded", "failed", "skipped", "cancelled",
}
TRANSITIONS = {
    "proposed": {"awaiting_input", "awaiting_confirmation", "ready", "blocked", "skipped", "cancelled"},
    "awaiting_input": {"proposed", "awaiting_confirmation", "ready", "blocked", "cancelled"},
    "awaiting_confirmation": {"ready", "skipped", "cancelled", "blocked"},
    "ready": {"running", "blocked", "cancelled", "awaiting_confirmation", "awaiting_input"},
    "running": {"succeeded", "failed", "awaiting_input", "cancelled"},
    "blocked": {"ready", "cancelled", "skipped"},
    "failed": {"ready", "blocked"},
    "succeeded": set(), "skipped": set(), "cancelled": set(),
}


def new_action(run: dict, proposal: dict, *, depth: int = 0) -> dict:
    if not isinstance(proposal, dict):
        raise WorkspaceError("Each proposed action must be an object.")
    type_ = str(proposal.get("type") or "")
    version = int(proposal.get("definition_version") or 1)
    definition = actions.REGISTRY.get(type_, version)
    action_id = str(proposal.get("id") or f"act_{uuid.uuid4().hex[:12]}")
    raw_target = proposal.get("target") or {}
    raw_args = proposal.get("args") or {}
    raw_dependencies = proposal.get("depends_on") or []
    if not isinstance(raw_target, dict):
        raise WorkspaceError(f"Action '{action_id}' target must be an object.")
    if not isinstance(raw_args, dict):
        raise WorkspaceError(f"Action '{action_id}' args must be an object.")
    if not isinstance(raw_dependencies, list):
        raise WorkspaceError(f"Action '{action_id}' depends_on must be an array.")
    target = dict(raw_target)
    # A singleton target contract is unambiguous and should not require the
    # model to repeat registry metadata.  Normalize it before strict action
    # validation; incorrect explicit kinds remain errors.
    if definition.target_kinds and not target.get("kind") and len(definition.target_kinds) == 1:
        target["kind"] = definition.target_kinds[0]
    action = {
        "id": action_id, "command_id": run["command"]["id"], "type": type_,
        "definition_version": version, "args": dict(raw_args),
        "target": {
            "kind": target.get("kind"), "selector": target.get("selector"),
            "resolved_id": target.get("resolved_id"),
        },
        "resolution": None, "depends_on": [str(value) for value in raw_dependencies],
        "depth": int(proposal.get("depth", depth)), "status": "proposed", "attempts": 0,
        "idempotency_key": f"{run['id']}:{action_id}",
        "failure_policy": str(proposal.get("failure_policy") or definition.failure_policy),
        "planning_significant": bool(
            proposal.get("planning_significant") or definition.planning_significant
        ),
        "precondition": None, "postcondition": None,
        "prepared_at": None, "started_at": None, "finished_at": None,
        "result_refs": [], "receipt": None, "error": None,
    }
    actions.allocate_create_id(action)
    # Target validation is intentionally deferred until append_actions has
    # resolved references to artifacts (and child items) created in this same
    # proposal batch. validate_graph still performs the full schema/target
    # validation before the batch is committed.
    return action


def transition(action: dict, status: str) -> None:
    if status not in ACTION_STATUSES:
        raise WorkspaceError(f"Unknown action status '{status}'.")
    current = action["status"]
    if current == status:
        return
    if status not in TRANSITIONS[current]:
        raise WorkspaceError(f"Illegal action transition {current} → {status}.")
    action["status"] = status


def validate_graph(run: dict) -> None:
    action_list = run.get("actions") or []
    limit = int((run.get("limits") or {}).get("max_actions", 60))
    if len(action_list) > limit:
        raise WorkspaceError(f"Action graph exceeds the {limit}-action limit.")
    by_id = {action["id"]: action for action in action_list}
    if len(by_id) != len(action_list):
        raise WorkspaceError("Action graph contains duplicate ids.")
    identities = set()
    max_depth = int((run.get("limits") or {}).get("max_depth", 10))
    for action in action_list:
        actions.validate_action(action)
        if action["depth"] > max_depth:
            raise WorkspaceError(f"Action graph exceeds depth {max_depth}.")
        missing = [dep for dep in action["depends_on"] if dep not in by_id]
        if missing:
            raise WorkspaceError(f"Action '{action['id']}' depends on unknown action '{missing[0]}'.")
        identity = hashlib.sha1(json.dumps({
            "type": action["type"], "args": action["args"], "target": action["target"],
        }, sort_keys=True, default=str).encode()).hexdigest()
        if identity in identities:
            raise WorkspaceError("Action graph contains duplicate action intent.")
        identities.add(identity)
    visiting: list[str] = []
    active, visited = set(), set()

    def visit(action_id: str):
        if action_id in active:
            start = visiting.index(action_id)
            cycle = [*visiting[start:], action_id]
            raise WorkspaceError(f"Action graph contains a cycle: {' -> '.join(cycle)}.")
        if action_id in visited:
            return
        visiting.append(action_id); active.add(action_id)
        for dependency in by_id[action_id]["depends_on"]:
            visit(dependency)
        visiting.pop(); active.remove(action_id); visited.add(action_id)

    for action_id in by_id:
        visit(action_id)


def append_actions(
    run: dict,
    proposals: list[dict],
    *,
    depth: int = 0,
) -> list[dict]:
    existing_actions = list(run.get("actions") or [])
    existing_ids = {action["id"] for action in existing_actions}
    existing_state = {
        action["id"]: {
            "depends_on": list(action.get("depends_on") or []),
            "target": dict(action.get("target") or {}),
        }
        for action in existing_actions
    }
    created = []
    for proposal in proposals:
        item = new_action(run, proposal, depth=depth)
        if item["id"] in existing_ids:
            raise WorkspaceError(f"Duplicate proposed action id '{item['id']}'.")
        created.append(item); existing_ids.add(item["id"])
    run.setdefault("actions", []).extend(created)
    adjustments = []
    try:
        adjustments.extend(actions.normalize_created_targets(run, created))
        validate_graph(run)
    except WorkspaceError:
        # Keep the append atomic: a rejected batch must not leave a
        # partially-extended graph behind in the run.
        if created:
            del run["actions"][-len(created):]
        for action in run.get("actions") or []:
            before = existing_state.get(action["id"])
            if before is not None:
                action["depends_on"] = before["depends_on"]
                action["target"] = before["target"]
        raise
    if adjustments:
        run.setdefault("target_adjustments", []).extend(adjustments)
    run["graph_revision"] = int(run.get("graph_revision") or 0) + 1
    project_action_plan(run)
    return created


def project_action_plan(run: dict) -> None:
    """Project the action graph into the shared stage/task UI contract."""

    status_map = {
        "proposed": "queued", "awaiting_input": "awaiting_approval",
        "awaiting_confirmation": "awaiting_approval", "ready": "queued",
        "blocked": "failed", "running": "running", "succeeded": "completed",
        "failed": "failed", "skipped": "skipped", "cancelled": "cancelled",
    }
    tasks = []
    for action in run.get("actions") or []:
        definition = actions.REGISTRY.get(action["type"], action["definition_version"])
        tasks.append({
            "id": action["id"], "stage": "actions", "title": definition.description,
            "detail": action["type"], "status": status_map[action["status"]],
            "error": action.get("error"), "result_refs": action.get("result_refs") or [],
        })
    existing = [
        stage for stage in (run.get("plan") or {}).get("stages") or []
        if stage.get("id") != "actions"
    ]
    action_stage = [{"id": "actions", "title": "Command actions", "tasks": tasks}] if tasks else []
    run["plan"] = {"stages": [*existing, *action_stage]}


def interaction(run: dict, action: dict, type_: str, prompt: str, *, options=None, payload=None, policy_reason="") -> dict:
    if type_ not in {"clarification", "target_choice", "confirmation", "proposal_approval", "conflict_resolution"}:
        raise WorkspaceError("Unknown interaction type.")
    item = {
        "id": f"int_{uuid.uuid4().hex[:12]}", "action_id": action["id"],
        "type": type_, "prompt": prompt, "options": list(options or []),
        "payload": payload or {}, "policy_reason": policy_reason,
        "status": "pending", "response": None, "actor": None,
        "created_at": store.utcnow(), "resolved_at": None,
    }
    run.setdefault("interactions", []).append(item)
    return item
