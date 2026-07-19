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
RESULT_ACTIONS = {
    "run_data_test", "run_document_test", "rollup_rcm_results",
}

# Broad audit commands may be proposed in any array order by the model.  The
# ledger turns that flat proposal into a conservative lifecycle: each populated
# stage waits for the nearest populated stage before it.  Model-supplied
# dependencies are preserved and graph validation still rejects cycles.
AUDIT_LIFECYCLE_STAGES = (
    {
        "update_planning_context", "generate_apm", "edit_apm",
    },
    {
        "create_rcm_row", "edit_rcm_row", "create_rcm_planned_test",
        "edit_rcm_planned_test", "create_procedure", "edit_procedure",
    },
    {
        "infer_relationships", "create_join", "create_validation_rules",
        "edit_validation_rules", "create_custom_analysis", "edit_custom_analysis",
        "create_data_test", "edit_data_test", "link_execution_to_planned_test",
        "create_document_test", "edit_document_test", "attach_document_to_test",
        "detach_document_from_test",
    },
    {
        "run_validation_rules", "run_analytics", "run_custom_analysis",
        "run_data_test", "run_document_test", "update_test_comparisons",
        "update_test_disposition",
    },
    {
        "rollup_rcm_results", "disposition_observation",
    },
    {
        "generate_rcm_working_paper", "generate_working_paper",
        "create_finding", "edit_finding",
        "promote_agent_finding",
    },
    {"curate_dashboard"},
    {
        "generate_report", "edit_report",
    },
    {
        "reconcile_report",
    },
    {
        "run_report_quality", "verify_audit_completion",
    },
)
AUDIT_LIFECYCLE_RANK = {
    type_: rank
    for rank, types in enumerate(AUDIT_LIFECYCLE_STAGES)
    for type_ in types
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
            proposal.get("planning_significant") or type_ in RESULT_ACTIONS
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


CREATE_TARGET_KINDS = {
    "create_rcm_row": "rcm",
    "create_rcm_planned_test": "planned_test",
    "create_procedure": "procedure",
    "create_data_test": "datatest",
    "create_validation_rules": "ruleset",
    "create_custom_analysis": "analysis",
    "create_document_test": "doctest",
    "create_finding": "finding",
    "pin_dashboard_tile": "tile",
}

# Some mutation actions write to a stable singleton rather than allocating an
# id in their arguments.  The model may still reference the producing action
# id, just as it does for create_* actions, so normalize those references to
# the durable artifact address before target resolution runs.
FIXED_ACTION_TARGETS = {
    "generate_report": ("report", "working"),
    "edit_report": ("report", "working"),
}


def normalize_created_targets(run: dict, created: list[dict]) -> list[dict]:
    """Resolve producer-action ids into durable artifact ids."""
    adjustments = []
    creators = {
        item["id"]: item
        for item in run.get("actions") or []
        if (
            item["type"] in FIXED_ACTION_TARGETS
            or (item["type"] in CREATE_TARGET_KINDS and item.get("args", {}).get("id"))
        )
    }
    argument_refs = {
        "rcm_id": "create_rcm_row",
        "planned_test_id": "create_rcm_planned_test",
    }
    for action in created:
        args = action.get("args") or {}
        for field, creator_type in argument_refs.items():
            reference = str(args.get(field) or "")
            creator = creators.get(reference)
            if creator is None or creator.get("type") != creator_type:
                continue
            durable_id = creator.get("args", {}).get("id")
            if durable_id:
                args[field] = durable_id
                if creator["id"] not in action["depends_on"]:
                    action["depends_on"].append(creator["id"])
                adjustments.append({
                    "action_id": action["id"], "kind": "argument_action_reference",
                    "field": field, "from": reference, "to": durable_id,
                })
    for action in created:
        target = action.get("target") or {}
        reference = str(target.get("resolved_id") or "")
        creator = creators.get(reference)
        if creator is not None:
            fixed_target = FIXED_ACTION_TARGETS.get(creator["type"])
            expected_kind = fixed_target[0] if fixed_target else CREATE_TARGET_KINDS[creator["type"]]
            definition = actions.REGISTRY.get(action["type"], action["definition_version"])
            targets_doctest_item = definition.target_kinds == ("doctest_item",)
            if creator["type"] == "create_document_test" and targets_doctest_item:
                items = creator.get("args", {}).get("items") or []
                if len(items) != 1 or not isinstance(items[0], dict) or not items[0].get("id"):
                    raise WorkspaceError(
                        f"Action '{action['id']}' targets an item in create action '{creator['id']}', "
                        "which must define exactly one test item for that reference."
                    )
                target["kind"] = "doctest_item"
                target["resolved_id"] = f"{creator['args']['id']}:{items[0]['id']}"
            elif target.get("kind") != expected_kind:
                raise WorkspaceError(
                    f"Action '{action['id']}' target kind '{target.get('kind')}' cannot reference "
                    f"create action '{creator['id']}' ({expected_kind})."
                )
            else:
                target["resolved_id"] = fixed_target[1] if fixed_target else creator["args"]["id"]
            if creator["id"] not in action["depends_on"]:
                action["depends_on"].append(creator["id"])
            adjustments.append({
                "action_id": action["id"], "kind": "target_action_reference",
                "from": reference, "to": target["resolved_id"],
            })

        selector = str(target.get("selector") or "")
        if target.get("kind") != "doctest_item" or not selector.startswith("test_id:"):
            continue
        creator_id = selector.partition(":")[2]
        creator = creators.get(creator_id)
        if creator is None or creator["type"] != "create_document_test":
            continue
        items = creator.get("args", {}).get("items") or []
        if len(items) != 1 or not isinstance(items[0], dict) or not items[0].get("id"):
            raise WorkspaceError(
                f"Action '{action['id']}' targets an item in create action '{creator_id}', "
                "which must define exactly one test item for that reference."
            )
        target["selector"] = None
        target["resolved_id"] = f"{creator['args']['id']}:{items[0]['id']}"
        if creator_id not in action["depends_on"]:
            action["depends_on"].append(creator_id)
        adjustments.append({
            "action_id": action["id"], "kind": "target_action_reference",
            "from": selector, "to": target["resolved_id"],
        })
    return adjustments


def _depends_on(by_id: dict[str, dict], start_id: str, target_id: str) -> bool:
    """Whether start already depends transitively on target."""
    pending = [start_id]
    seen = set()
    while pending:
        action_id = pending.pop()
        if action_id == target_id:
            return True
        if action_id in seen or action_id not in by_id:
            continue
        seen.add(action_id)
        pending.extend(by_id[action_id]["depends_on"])
    return False


def enforce_audit_lifecycle(run: dict, created: list[dict]) -> list[dict]:
    """Normalize backward edges and add deterministic stage dependencies."""
    all_actions = list(run.get("actions") or [])
    by_id = {action["id"]: action for action in all_actions}
    adjustments = []
    by_rank: dict[int, list[dict]] = {}
    for action in all_actions:
        rank = AUDIT_LIFECYCLE_RANK.get(action["type"])
        if rank is not None:
            by_rank.setdefault(rank, []).append(action)

    # The local lifecycle is authoritative. A model edge from an earlier
    # stage to a later stage reverses that lifecycle and can cause a cycle
    # once prerequisites are injected, so remove it deterministically.
    for action in created:
        rank = AUDIT_LIFECYCLE_RANK.get(action["type"])
        if rank is None:
            continue
        kept = []
        for dependency_id in action["depends_on"]:
            dependency = by_id.get(dependency_id)
            dependency_rank = AUDIT_LIFECYCLE_RANK.get(dependency["type"]) if dependency else None
            if dependency_rank is not None and dependency_rank > rank:
                adjustments.append({
                    "action_id": action["id"], "kind": "removed_backward_dependency",
                    "dependency_id": dependency_id,
                })
                continue
            kept.append(dependency_id)
        action["depends_on"] = kept

    for action in created:
        rank = AUDIT_LIFECYCLE_RANK.get(action["type"])
        if rank is None:
            continue
        prior_ranks = [value for value in by_rank if value < rank]
        if prior_ranks:
            nearest = max(prior_ranks)
            for dependency in by_rank[nearest]:
                if dependency["id"] != action["id"] and dependency["id"] not in action["depends_on"]:
                    if _depends_on(by_id, dependency["id"], action["id"]):
                        adjustments.append({
                            "action_id": action["id"], "kind": "skipped_cyclic_dependency",
                            "dependency_id": dependency["id"],
                        })
                        continue
                    action["depends_on"].append(dependency["id"])

    # When one new document test is the only possible target, bind its run
    # action to the allocated durable id instead of asking the auditor to
    # resolve an artifact the graph itself is about to create.
    creators = [item for item in all_actions if item["type"] == "create_document_test"]
    for action in created:
        target = action.get("target") or {}
        if action["type"] != "run_document_test" or target.get("selector") or target.get("resolved_id"):
            continue
        dependent_creators = [item for item in creators if item["id"] in action["depends_on"]]
        candidates = dependent_creators or creators
        if len(candidates) == 1:
            target["resolved_id"] = candidates[0]["args"]["id"]
            adjustments.append({
                "action_id": action["id"], "kind": "target_action_reference",
                "from": None, "to": target["resolved_id"],
            })
    return adjustments


def append_actions(
    run: dict,
    proposals: list[dict],
    *,
    depth: int = 0,
    audit_lifecycle: bool = False,
) -> list[dict]:
    existing_ids = {action["id"] for action in run.get("actions") or []}
    created = []
    for proposal in proposals:
        item = new_action(run, proposal, depth=depth)
        if item["id"] in existing_ids:
            raise WorkspaceError(f"Duplicate proposed action id '{item['id']}'.")
        created.append(item); existing_ids.add(item["id"])
    run.setdefault("actions", []).extend(created)
    adjustments = []
    try:
        adjustments.extend(normalize_created_targets(run, created))
        if audit_lifecycle:
            adjustments.extend(enforce_audit_lifecycle(run, created))
        validate_graph(run)
    except WorkspaceError:
        # Keep the append atomic: a rejected batch must not leave a
        # partially-extended graph behind in the run.
        if created:
            del run["actions"][-len(created):]
        raise
    if adjustments:
        run.setdefault("lifecycle_adjustments", []).extend(adjustments)
    run["graph_revision"] = int(run.get("graph_revision") or 0) + 1
    project_legacy_plan(run)
    return created


def project_legacy_plan(run: dict) -> None:
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
            "context_notes": [],
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
