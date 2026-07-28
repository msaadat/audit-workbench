"""Typed command-action registry and domain-specific executors.

The registry is executable policy: persisted actions pin a definition version,
while only these local functions may mutate engagement artifacts.

**Catalog boundary (P11.2A).** The catalog holds target-specific operations:
CRUD, attachments, pins, manual edits, and the rerun of one identified existing
artifact. It may not generate or refresh an artifact family a registered
workflow outcome owns, because that would give one request two engines. The
generators removed for that reason, and the outcome that now owns each of
them, are:

===============================  ==================================
Removed action                   Owning workflow outcome
===============================  ==================================
``generate_apm``                 ``planning.apm_ready``
``infer_relationships``          ``data.relationships_inferred``
``run_document_test``            ``doc_tests.executed``
``rollup_rcm_results``           ``results.rolled_up``
``generate_all_rcm_working_papers``  ``working_papers.generated``
``generate_report``              ``report.working_draft``
``curate_dashboard``             ``dashboard.curated``
``verify_audit_completion``      ``audit.verified``
===============================  ==================================

``run_document_test`` is the one removal that also closed a privacy and budget
hole: it called ``doc_tests.run_item`` with no model adapter, so a Q&A worklist
reached the provider outside the registered ``fieldwork.document_qa`` worker,
its declared page context, and the run's model budget.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Callable

from .. import (
    analytics, data_tests, doc_tests, explore, findings, intake, model_context,
    rcm_execution, report, sandbox, validation, working_papers,
)
from ..field_names import resolve_columns
from ..workspaces import (
    JOIN_TYPES, OBSERVATION_DISPOSITIONS,
    Workspace, WorkspaceError, slugify,
)
from . import artifact_index

RISKS = {"read", "compute", "create", "reversible_mutation", "broad_rewrite", "destructive"}
MODEL_USAGES = {"none", "plan", "draft", "interpret_result"}
FAILURE_POLICIES = {"stop_dependents", "continue", "stop_run"}

Executor = Callable[[Workspace, dict, dict], dict]
Reconciler = Callable[[Workspace, dict], str]


@dataclass(frozen=True)
class ActionDefinition:
    type: str
    version: int
    description: str
    input_schema: dict
    output_schema: dict
    target_kinds: tuple[str, ...]
    risk: str
    model_usage: str
    failure_policy: str
    executor: Executor
    reconciler: Reconciler | None = None
    result_sanitizer: Callable[[dict], dict] = lambda value: value
    planning_significant: bool = False


class ActionRegistry:
    def __init__(self):
        self._definitions: dict[tuple[str, int], ActionDefinition] = {}

    def register(self, definition: ActionDefinition) -> None:
        key = (definition.type, definition.version)
        if key in self._definitions:
            raise RuntimeError(f"Duplicate action definition {definition.type}@{definition.version}.")
        if definition.risk not in RISKS or definition.model_usage not in MODEL_USAGES:
            raise RuntimeError(f"Invalid action policy on {definition.type}.")
        if definition.failure_policy not in FAILURE_POLICIES:
            raise RuntimeError(f"Invalid failure policy on {definition.type}.")
        if not isinstance(definition.input_schema, dict) or not isinstance(definition.output_schema, dict):
            raise RuntimeError(f"Action {definition.type} must declare schemas.")
        if definition.risk in {"create", "reversible_mutation", "broad_rewrite", "destructive"} and definition.reconciler is None:
            raise RuntimeError(f"Mutation action {definition.type} needs a reconciler.")
        self._definitions[key] = definition

    def get(self, type_: str, version: int = 1) -> ActionDefinition:
        try:
            return self._definitions[(type_, version)]
        except KeyError as error:
            if any(key[0] == type_ for key in self._definitions):
                raise WorkspaceError(f"Action '{type_}' version {version} is incompatible.") from error
            raise WorkspaceError(f"Unknown agent action '{type_}'.") from error

    def all(self) -> list[ActionDefinition]:
        return sorted(self._definitions.values(), key=lambda value: value.type)


REGISTRY = ActionRegistry()


def validate_schema(value: object, schema: dict, path: str = "args") -> None:
    """Small strict JSON-schema subset sufficient for persisted action args."""
    type_ = schema.get("type")
    expected = {
        "object": dict, "array": list, "string": str, "number": (int, float),
        "integer": int, "boolean": bool,
    }.get(type_)
    if expected and (not isinstance(value, expected) or type_ in {"number", "integer"} and isinstance(value, bool)):
        raise WorkspaceError(f"{path} must be {type_}.")
    if type_ == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            raise WorkspaceError(f"{path}.{missing[0]} is required.")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise WorkspaceError(f"Unknown {path} field '{sorted(unknown)[0]}'.")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")
    elif type_ == "array" and schema.get("items"):
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")
    if "enum" in schema and value not in schema["enum"]:
        raise WorkspaceError(f"{path} has an unsupported value.")


def validate_action(action: dict) -> ActionDefinition:
    definition = REGISTRY.get(str(action.get("type") or ""), int(action.get("definition_version") or 1))
    validate_schema(action.get("args") or {}, definition.input_schema)
    if definition.type == "create_custom_analysis":
        try:
            sandbox.validate(str(((action.get("args") or {}).get("spec") or {}).get("code") or ""))
        except sandbox.SandboxError as error:
            raise WorkspaceError(f"Invalid custom analysis code: {error}") from error
    elif definition.type == "edit_custom_analysis":
        spec = (((action.get("args") or {}).get("changes") or {}).get("spec"))
        if isinstance(spec, dict) and "code" in spec:
            try:
                sandbox.validate(str(spec.get("code") or ""))
            except sandbox.SandboxError as error:
                raise WorkspaceError(f"Invalid custom analysis code: {error}") from error
    elif definition.type == "create_document_test":
        _validate_document_test_action(action.get("args") or {})
    target = action.get("target") or {}
    if definition.target_kinds and target.get("kind") not in definition.target_kinds:
        raise WorkspaceError(f"Action '{definition.type}' requires target kind: {', '.join(definition.target_kinds)}.")
    return definition


def _validate_document_test_action(args: dict) -> None:
    """Reject description-only document work before it enters the ledger."""
    kind = str(args.get("kind") or "")
    items = list(args.get("items") or [])
    # Convenience builders are allowed when they have enough source detail to
    # create substantive items themselves.
    if not items:
        if kind == "vouching" and str(args.get("table") or "").strip():
            return
        if kind == "review" and str(args.get("document_id") or "").strip():
            return
        if kind == "qa" and args.get("document_ids") and args.get("questions"):
            return
        raise WorkspaceError(
            f"A {kind or 'document'} test needs executable items or complete builder arguments."
        )
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise WorkspaceError(f"Document-test item {index} must be an object.")
        if kind == "vouching" and not item.get("checks"):
            raise WorkspaceError(f"Vouching item {index} needs comparison checks.")
        if kind == "attribute" and not item.get("attributes"):
            raise WorkspaceError(f"Attribute item {index} needs attributes.")
        if kind == "review" and not (
            item.get("page") not in (None, "")
            or str(item.get("excerpt") or "").strip()
            or str(item.get("summary") or "").strip()
        ):
            raise WorkspaceError(
                f"Review item {index} needs a page, excerpt, or review summary."
            )
        if kind == "qa" and not str(item.get("question") or "").strip():
            raise WorkspaceError(f"Q&A item {index} needs a question.")


def allocate_create_id(action: dict) -> None:
    args = action.setdefault("args", {})
    prefixes = {
        "create_rcm_row": "RCM-", "create_procedure": "PROC-", "create_finding": "F-",
        "draft_finding_from_observation": "F-",
        "create_document_test": "DT-",
        "create_data_test": "DAT-",
    }
    if not args.get("id") and action["type"] in prefixes:
        args["id"] = prefixes[action["type"]] + uuid.uuid4().hex[:6].upper()
    elif not args.get("id") and action["type"] in {"create_validation_rules", "create_custom_analysis", "pin_dashboard_tile"}:
        args["id"] = uuid.uuid4().hex[:10]
    if action["type"] == "create_document_test":
        for item in args.get("items") or []:
            if isinstance(item, dict) and not item.get("id"):
                item["id"] = f"ITEM-{uuid.uuid4().hex[:8].upper()}"


CREATE_TARGET_KINDS = {
    "create_rcm_row": "rcm",
    "create_procedure": "procedure",
    "create_data_test": "datatest",
    "create_validation_rules": "ruleset",
    "create_custom_analysis": "analysis",
    "create_document_test": "doctest",
    "create_finding": "finding",
    "draft_finding_from_observation": "finding",
    "pin_dashboard_tile": "tile",
}

# Some mutation actions write to a stable singleton rather than allocating an
# id in their arguments. The action catalog owns these domain-specific producer
# contracts; the ledger only asks the catalog to normalize references.
FIXED_ACTION_TARGETS = {
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
            definition = REGISTRY.get(action["type"], action["definition_version"])
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


def approval_required(definition: ActionDefinition, run: dict, action: dict) -> bool:
    # Auto mode is an explicit authorization to execute every locally
    # validated action without an approval checkpoint. Permission mode keeps
    # approval gates for every mutation, including destructive actions and
    # broad rewrites.
    if run["mode"] == "auto":
        return False
    return definition.risk not in {"read", "compute"}


def artifact_snapshot(workspace: Workspace, kind: str, item_id: str) -> dict | None:
    if kind == "planning" and item_id == "apm":
        return copy.deepcopy(workspace.planning)
    collections = {
        "rcm": workspace.rcm, "procedure": workspace.work_program,
        "finding": workspace.findings, "analysis": workspace.analyses,
        "ruleset": workspace.rulesets, "tile": workspace.tiles,
        "document": workspace.documents, "datatest": workspace.data_tests,
        "observation": workspace.observations,
    }
    if kind in collections:
        return copy.deepcopy(next((item for item in collections[kind] if str(item.get("id")) == item_id), None))
    if kind == "doctest":
        return copy.deepcopy(doc_tests.load_test(workspace, item_id)) if doc_tests.exists(workspace, item_id) else None
    if kind == "doctest_item":
        test_id, _, child_id = item_id.partition(":")
        if not child_id or not doc_tests.exists(workspace, test_id):
            return None
        test = doc_tests.load_test(workspace, test_id)
        return copy.deepcopy(next((item for item in test.get("items") or [] if item.get("id") == child_id), None))
    if kind == "report" and item_id == "working":
        return copy.deepcopy(report.hydrate(workspace))
    if kind == "table":
        return copy.deepcopy(next((item for item in [*workspace.tables, *workspace.joins] if item.get("name") == item_id), None))
    return None


def expected_postcondition(action: dict) -> dict:
    """Deterministic postcondition persisted before a mutation is called."""
    type_, args = action["type"], action.get("args") or {}
    if type_.startswith("delete_") or type_ == "remove_dashboard_tile":
        return {"absent": True}
    create_kinds = {
        "create_rcm_row": "rcm", "create_procedure": "procedure", "create_finding": "finding",
        "draft_finding_from_observation": "finding",
        "create_document_test": "doctest", "create_validation_rules": "ruleset",
        "create_custom_analysis": "analysis", "pin_dashboard_tile": "tile", "create_join": "table",
        "create_data_test": "datatest",
    }
    if type_ in create_kinds:
        item_id = args.get("name") if type_ == "create_join" else args.get("id")
        ignored = {"agent_run_id", "created_by", "created", "updated", "source", "sha1"}
        return {"kind": create_kinds[type_], "id": item_id, "fields": {k: v for k, v in args.items() if k not in ignored}}
    if type_ == "update_planning_context":
        return {"fields": {"context": args.get("changes") or {}}}
    if type_ == "edit_apm":
        return {"fields": {"apm_markdown": args.get("apm_markdown")}}
    if type_ in {"edit_rcm_row", "edit_procedure", "edit_finding", "edit_validation_rules", "edit_custom_analysis", "edit_dashboard_tile", "edit_document_test", "update_test_disposition", "edit_data_test"}:
        return {"fields": dict(args.get("changes") or {})}
    if type_ == "update_test_comparisons":
        return {"fields": {"checks": args.get("checks") or []}}
    if type_ in {"attach_document_to_test", "detach_document_from_test"}:
        return {"document_id": args.get("document_id"), "attached": type_.startswith("attach_")}
    if type_ == "edit_report":
        return {"fields": dict(args.get("changes") or {})}
    if type_ == "reconcile_report":
        return {"reconcile": args.get("action")}
    return {}


def postcondition_matches(current: dict | None, expected: dict) -> bool:
    if expected.get("absent"):
        return current is None
    if current is None:
        return False
    for key, value in (expected.get("fields") or {}).items():
        if key == "context" and isinstance(value, dict):
            if any((current.get("context") or {}).get(name) != field for name, field in value.items()):
                return False
        elif current.get(key) != value:
            return False
    document_id = expected.get("document_id")
    if document_id:
        attached = document_id in (current.get("document_ids") or [])
        if attached != bool(expected.get("attached")):
            return False
    if expected.get("reconcile") == "replace" and current.get("markdown") != current.get("generated_markdown"):
        return False
    return bool(expected)


def _target_id(action: dict) -> str:
    value = (action.get("target") or {}).get("resolved_id") or (action.get("resolution") or {}).get("resolved_id")
    if not value:
        raise WorkspaceError("Action target has not been resolved.")
    return str(value)


def _receipt(action: dict, item: dict | None = None, *, refs=None, result=None) -> dict:
    return {
        "idempotency_key": action["idempotency_key"],
        "result_refs": list(refs or []),
        "post_sha1": artifact_index.canonical_sha1(item) if item is not None else None,
        "result": result or {},
    }


def canonicalize_action_fields(workspace: Workspace, action: dict) -> None:
    """Canonicalize field-bearing declarative action args before approval/run."""
    type_ = str(action.get("type") or "")
    args = action.get("args")
    if not isinstance(args, dict):
        return
    try:
        parent_args = args.get("changes") if type_ in {"edit_data_test", "edit_document_test"} else args
        if isinstance(parent_args, dict):
            if "rcm_id" in parent_args:
                parent_args["rcm_id"] = artifact_index.canonical_id(
                    parent_args.get("rcm_id"), "rcm"
                )

        if type_ in {"create_data_test", "edit_data_test"}:
            values = parent_args if isinstance(parent_args, dict) else args
            current = None
            if type_ == "edit_data_test":
                target_id = artifact_index.canonical_id(
                    (action.get("target") or {}).get("resolved_id"), "datatest"
                )
                if any(item.get("id") == target_id for item in workspace.data_tests):
                    current = data_tests._record(workspace, target_id)
                    values = {**current, **values}
            # Durable parents can be checked now. Producer-action ids are
            # resolved by the ledger and checked again before execution.
            rcm_id = str(values.get("rcm_id") or "")
            if rcm_id and any(row.get("id") == rcm_id for row in workspace.rcm):
                data_tests._validate_rcm_id(workspace, rcm_id)
            if type_ == "create_data_test":
                engine = str(args.get("engine") or "").strip().lower()
                refs = [] if engine == "polars" else data_tests._table_refs(workspace, args.get("table_refs"))
                spec, _warnings, refs = data_tests._validate_spec(
                    workspace, engine, refs,
                    args.get("spec"),
                )
                args["table_refs"] = refs
                args["spec"] = spec
            elif current is not None and any(
                key in parent_args for key in ("engine", "table_refs", "spec")
            ):
                engine = str(values.get("engine") or "").strip().lower()
                refs = [] if engine == "polars" else data_tests._table_refs(workspace, values.get("table_refs"))
                spec, _warnings, refs = data_tests._validate_spec(
                    workspace, engine, refs,
                    values.get("spec"),
                )
                if "table_refs" in parent_args:
                    parent_args["table_refs"] = refs
                if "spec" in parent_args or "engine" in parent_args or "table_refs" in parent_args:
                    parent_args["spec"] = spec
        elif type_ in {"create_document_test", "edit_document_test"}:
            values = parent_args if isinstance(parent_args, dict) else args
            if type_ == "edit_document_test":
                target_id = artifact_index.canonical_id(
                    (action.get("target") or {}).get("resolved_id"), "doctest"
                )
                try:
                    current = doc_tests.load_test(workspace, target_id)
                except WorkspaceError:
                    current = None
                if current is not None:
                    values = {**current, **values}
            rcm_id = str(values.get("rcm_id") or "")
            if rcm_id and any(row.get("id") == rcm_id for row in workspace.rcm):
                doc_tests._validate_rcm_id(workspace, rcm_id)
        elif type_ == "link_test_to_rcm_row":
            rcm_id = str(args.get("rcm_id") or "")
            if rcm_id and not any(row.get("id") == rcm_id for row in workspace.rcm):
                raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
        elif type_ == "create_join":
            left, right = args.get("left"), args.get("right")
            if left in workspace.table_names() and right in workspace.table_names():
                args["left_on"] = resolve_columns(
                    args.get("left_on"), workspace.get_frame(left).columns,
                    table=left, error_type=WorkspaceError,
                )
                args["right_on"] = resolve_columns(
                    args.get("right_on"), workspace.get_frame(right).columns,
                    table=right, error_type=WorkspaceError,
                )
        elif type_ == "run_analytics":
            table = args.get("table")
            if table in workspace.table_names():
                args["test"] = analytics.canonical_test_id(args.get("test"))
                args["params"] = analytics.canonicalize_params(
                    workspace.get_frame(table), args.get("test"), args.get("params")
                )
        elif type_ == "create_validation_rules":
            table = args.get("table")
            if table in workspace.table_names() and isinstance(args.get("rules"), list):
                args["rules"] = validation.canonicalize_rules(
                    workspace.get_frame(table), args.get("rules") or [],
                    resolve=workspace.get_frame, strict=True,
                )
                issues = validation.generated_rule_issues(
                    workspace.get_frame(table), args["rules"], resolve=workspace.get_frame
                )
                if issues:
                    raise WorkspaceError(
                        "Generated validation rule failed semantic preflight: " + "; ".join(issues)
                    )
        elif type_ == "pin_dashboard_tile":
            table = args.get("table")
            if table not in workspace.table_names():
                return
            frame = workspace.get_frame(table)
            kind = args.get("kind")
            spec = dict(args.get("spec") or {})
            if kind == "query":
                args["spec"] = explore.canonicalize_query_spec(frame, spec)
            elif kind == "analytics":
                spec["params"] = analytics.canonicalize_params(
                    frame, spec.get("test"), spec.get("params")
                )
                args["spec"] = spec
            elif kind == "validation":
                spec["rules"] = validation.canonicalize_rules(
                    frame, spec.get("rules") or [], resolve=workspace.get_frame, strict=True
                )
                args["spec"] = spec
    except WorkspaceError:
        raise
    except ValueError as error:
        raise WorkspaceError(str(error)) from error


def _execute(workspace: Workspace, action: dict, run: dict) -> dict:
    canonicalize_action_fields(workspace, action)
    type_, args = action["type"], action.get("args") or {}
    target_id = None
    if REGISTRY.get(type_).target_kinds:
        target_id = _target_id(action)

    if type_ == "update_planning_context":
        item = workspace.update_planning({"context": args["changes"], "agent_run_id": run["id"], "created_by": "agent"}, agent=True)
        return _receipt(action, item, refs=["planning:apm"])
    if type_ == "edit_apm":
        item = workspace.update_planning({"apm_markdown": args["apm_markdown"], "agent_run_id": run["id"], "created_by": "agent"}, agent=True)
        return _receipt(action, item, refs=["planning:apm"])
    if type_ == "create_rcm_row":
        item = workspace.add_rcm({**args, "agent_run_id": run["id"]})
        return _receipt(action, item, refs=[f"rcm:{item['id']}"])
    if type_ == "edit_rcm_row":
        item = workspace.update_rcm(target_id, args["changes"], agent=True)
        return _receipt(action, item, refs=[f"rcm:{target_id}"])
    if type_ == "delete_rcm_row":
        workspace.remove_rcm(target_id); return _receipt(action, refs=[f"rcm:{target_id}"])
    if type_ == "create_procedure":
        item = workspace.add_procedure({**args, "agent_run_id": run["id"]})
        return _receipt(action, item, refs=[f"procedure:{item['id']}"])
    if type_ == "edit_procedure":
        item = workspace.update_procedure(target_id, args["changes"], agent=True)
        return _receipt(action, item, refs=[f"procedure:{target_id}"])
    if type_ == "delete_procedure":
        workspace.remove_procedure(target_id); return _receipt(action, refs=[f"procedure:{target_id}"])
    if type_ == "create_finding":
        item = findings.add(workspace, {**args, "agent_run_id": run["id"]}, source="agent")
        support = findings.support_issues(workspace, item)
        return _receipt(
            action, item, refs=[f"finding:{item['id']}"],
            result={
                "support_complete": not support,
                "report_eligible": bool(item.get("auditor_confirmed")) and not support,
                "support_issues": support,
            },
        )
    if type_ == "draft_finding_from_observation":
        observation = next(
            (item for item in workspace.observations if item.get("id") == target_id),
            None,
        )
        if observation is None:
            raise WorkspaceError(f"Observation '{target_id}' not found.")
        if observation.get("status") != "disposed" or observation.get("disposition") not in {
            "confirmed_control_exception", "draft_finding_candidate",
        }:
            raise WorkspaceError(
                "A finding draft requires an auditor-dispositioned finding-candidate observation."
            )
        execution_ref = str(observation.get("execution_ref") or "")
        anchor = findings.anchor_from_ref(workspace, execution_ref, run_id=run["id"])
        if anchor is None:
            raise WorkspaceError(
                f"Observation '{target_id}' has no resolvable immutable execution evidence."
            )
        item = findings.add(
            workspace,
            {
                **args,
                "agent_run_id": run["id"],
                "rcm_refs": [observation["rcm_id"]],
                "test_refs": [observation["test_id"]],
                "execution_refs": [execution_ref],
                "evidence_refs": [anchor],
                "auditor_confirmed": False,
            },
            source="agent",
        )
        support = findings.support_issues(workspace, item)
        return _receipt(
            action, item, refs=[f"finding:{item['id']}"],
            result={
                "support_complete": not support,
                "report_eligible": False,
                "support_issues": support,
                "auditor_confirmation_required": True,
            },
        )
    if type_ == "edit_finding":
        item = findings.update(workspace, target_id, args["changes"])
        return _receipt(action, item, refs=[f"finding:{target_id}"])
    if type_ == "delete_finding":
        findings.remove(workspace, target_id); return _receipt(action, refs=[f"finding:{target_id}"])
    if type_ == "promote_agent_finding":
        item = findings.promote(workspace, args["run_id"], args["finding_id"])
        return _receipt(action, item, refs=[f"finding:{item['id']}"])
    if type_ == "create_join":
        item = workspace.add_join({**args, "agent_run_id": run["id"]})
        return _receipt(action, item, refs=[f"table:{item['name']}"])
    if type_ == "create_validation_rules":
        item = workspace.add_ruleset({**args, "agent_run_id": run["id"]})
        return _receipt(action, item, refs=[f"ruleset:{item['id']}"])
    if type_ == "edit_validation_rules":
        item = workspace.update_ruleset(target_id, args["changes"])
        return _receipt(action, item, refs=[f"ruleset:{target_id}"])
    if type_ == "run_validation_rules":
        ruleset = artifact_snapshot(workspace, "ruleset", target_id)
        result = validation.run_rules(workspace.get_frame(ruleset["table"]), ruleset["rules"], ruleset["table"], resolve=workspace.get_frame)
        workspace.record_run(target_id, result)
        summary = {key: result.get(key) for key in ("run_at", "table", "rows", "verdict", "counts")}
        return _receipt(action, ruleset, refs=[f"ruleset:{target_id}"], result=summary)
    if type_ == "run_analytics":
        result = analytics.run_test(workspace.get_frame(args["table"]), args["test"], args.get("params") or {})
        model_result = {"title": result.title, "verdict": result.verdict, "verdict_text": result.verdict_text, "stats": result.stats}
        if result.summary is not None:
            model_result["summary"] = model_context.project_frame(result.summary)
        return _receipt(action, result=model_result)
    if type_ == "create_data_test":
        item = data_tests.create(workspace, {**args, "agent_run_id": run["id"]})
        return _receipt(action, item, refs=[f"datatest:{item['id']}"])
    if type_ == "edit_data_test":
        item = data_tests.update(workspace, target_id, args["changes"])
        return _receipt(action, item, refs=[f"datatest:{target_id}"])
    if type_ == "run_data_test":
        item = data_tests._record(workspace, target_id)
        result = data_tests.run(workspace, target_id)
        safe = {
            key: result.get(key)
            for key in (
                "id", "data_test_id", "rcm_id", "status",
                "verdict", "verdict_text", "statistics", "exception_count",
                "semantic_valid", "semantic_issues", "result_sha1",
            )
        }
        return _receipt(
            action, item,
            refs=[f"datatest:{target_id}", f"datatest:{target_id}:{result['id']}"],
            result=safe,
        )
    if type_ == "link_test_to_rcm_row":
        rcm_id = str(args.get("rcm_id") or "") or None
        if (action.get("target") or {}).get("kind") == "datatest":
            item = data_tests.update(workspace, target_id, {"rcm_id": rcm_id})
            ref = f"datatest:{target_id}"
        else:
            item = doc_tests.update_test(workspace, target_id, {"rcm_id": rcm_id})
            ref = f"doctest:{target_id}"
        return _receipt(
            action, item, refs=[ref, *( [f"rcm:{rcm_id}"] if rcm_id else [] )]
        )
    if type_ == "create_custom_analysis":
        sandbox.run(str((args.get("spec") or {}).get("code") or ""), {name: workspace.get_frame(name) for name in workspace.table_names()})
        item = workspace.add_analysis({**args, "kind": "python", "agent_run_id": run["id"], "source": "ai"})
        return _receipt(action, item, refs=[f"analysis:{item['id']}"])
    if type_ == "edit_custom_analysis":
        changes = args["changes"]
        if "spec" in changes:
            sandbox.run(str((changes["spec"] or {}).get("code") or ""), {name: workspace.get_frame(name) for name in workspace.table_names()})
        item = workspace.update_analysis(target_id, changes)
        return _receipt(action, item, refs=[f"analysis:{target_id}"])
    if type_ == "run_custom_analysis":
        item = artifact_snapshot(workspace, "analysis", target_id)
        result, stdout = sandbox.run(item["spec"]["code"], {name: workspace.get_frame(name) for name in workspace.table_names()})
        return _receipt(action, item, refs=[f"analysis:{target_id}"], result={"frame": model_context.project_frame(result), "stdout": (stdout or "")[:4_000], "stdout_truncated": len(stdout or "") > 4_000})
    if type_ == "pin_dashboard_tile":
        item = workspace.add_tile({**args, "agent_run_id": run["id"]})
        return _receipt(action, item, refs=[f"tile:{item['id']}"])
    if type_ == "edit_dashboard_tile":
        item = workspace.update_tile(target_id, args["changes"])
        return _receipt(action, item, refs=[f"tile:{target_id}"])
    if type_ == "remove_dashboard_tile":
        workspace.remove_tile(target_id); return _receipt(action, refs=[f"tile:{target_id}"])
    if type_ == "create_document_test":
        kind = args.get("kind")
        # Explicitly planned items already have durable IDs allocated by the
        # ledger so later actions can target them. Preserve those items rather
        # than invoking a convenience builder that would replace them and
        # invalidate the graph's child-item references.
        builder = doc_tests.create_test if args.get("items") else {
            "vouching": doc_tests.build_vouching,
            "attribute": doc_tests.build_attribute,
            "review": doc_tests.build_review,
            "qa": doc_tests.build_qa,
        }.get(kind, doc_tests.create_test)
        item = builder(workspace, args)
        return _receipt(action, item, refs=[f"doctest:{item['id']}"])
    if type_ == "edit_document_test":
        item = doc_tests.update_test(workspace, target_id, args["changes"])
        return _receipt(action, item, refs=[f"doctest:{target_id}"])
    if type_ == "delete_document_test":
        doc_tests.remove_test(workspace, target_id); return _receipt(action, refs=[f"doctest:{target_id}"])
    if type_ in {"attach_document_to_test", "detach_document_from_test", "update_test_comparisons", "update_test_disposition"}:
        test_id, _, item_id = target_id.partition(":")
        if type_ == "attach_document_to_test": item = doc_tests.attach_document(workspace, test_id, item_id, args["document_id"])
        elif type_ == "detach_document_from_test": item = doc_tests.detach_document(workspace, test_id, item_id, args["document_id"])
        elif type_ == "update_test_comparisons": item = doc_tests.update_comparisons(workspace, test_id, item_id, args["checks"])
        else: item = doc_tests.update_item(workspace, test_id, item_id, args["changes"])
        return _receipt(action, item, refs=[f"doctest:{test_id}", f"doctest_item:{target_id}"])
    if type_ == "disposition_observation":
        item = rcm_execution.disposition(
            workspace, target_id, args["disposition"], args.get("auditor_note") or ""
        )
        return _receipt(action, item, refs=[f"observation:{target_id}"])
    if type_ == "generate_rcm_working_paper":
        item = working_papers.generate_rcm(workspace, target_id)
        return _receipt(
            action, item, refs=[f"rcm:{target_id}"],
            result={key: item[key] for key in ("rcm_id", "generated_at", "source_sha1")},
        )
    if type_ == "generate_working_paper":
        item = working_papers.draft_results(workspace, target_id)
        rendered = working_papers.render(workspace, target_id)
        return _receipt(action, item, refs=[f"procedure:{target_id}"], result={k: rendered[k] for k in ("generated_at", "source_sha1", "procedure_id")})
    if type_ == "edit_report":
        item = report.update(workspace, args["changes"])
        return _receipt(action, report.hydrate(workspace), refs=["report:working"], result={"edited": item.get("edited")})
    if type_ == "reconcile_report":
        item = report.reconcile(workspace, args["action"])
        return _receipt(action, report.hydrate(workspace), refs=["report:working"], result={"edited": item.get("edited")})
    if type_ == "run_report_quality":
        result = report.quality_checks(workspace)
        return _receipt(action, result={"ok": result["ok"], "issues": result["issues"]})
    if type_ == "classify_import_batch":
        batch = intake.load_batch(workspace, args["batch_id"])
        missing = [item for item in batch.get("items") or [] if not item.get("classification")]
        if missing:
            intake.merge_model_classifications(
                batch, [intake.deterministic_classification(item) for item in missing]
            )
        intake.save_batch(workspace, batch)
        return _receipt(action, result={"batch_id": batch["id"], "items": len(batch.get("items") or [])})
    if type_ == "apply_import_batch":
        result = intake.apply_batch(workspace, args["batch_id"], args.get("decisions"))
        return _receipt(action, result={key: result.get(key) for key in ("id", "status", "summary")})
    if type_ == "undo_action":
        from . import store
        source_run_id = str(args.get("run_id") or run["id"])
        source_run = run if source_run_id == run["id"] else store.load_run(workspace, source_run_id)
        source = next((item for item in source_run.get("actions") or [] if item["id"] == args["action_id"]), None)
        if source is None or source.get("status") != "succeeded":
            raise WorkspaceError("The action to undo was not found or did not succeed.")
        source_definition = REGISTRY.get(source["type"], source["definition_version"])
        if source_definition.risk != "reversible_mutation":
            raise WorkspaceError("That action is not eligible for undo.")
        snapshot_ref = ((source.get("precondition") or {}).get("snapshot"))
        if not snapshot_ref:
            raise WorkspaceError("That action has no retained before snapshot.")
        kind = (source.get("target") or {}).get("kind")
        item_id = (source.get("target") or {}).get("resolved_id")
        current = artifact_snapshot(workspace, kind, item_id)
        if artifact_index.canonical_sha1(current) != (source.get("receipt") or {}).get("post_sha1"):
            raise WorkspaceError("The artifact changed after the original action; undo would overwrite newer work.")
        before = store.read_sidecar(workspace, source_run_id, snapshot_ref)
        restored = _restore_snapshot(workspace, kind, item_id, before)
        return _receipt(action, restored, refs=[f"{kind}:{item_id}"], result={"undid_action_id": source["id"]})
    raise WorkspaceError(f"Action '{type_}' has no executor.")


def _reconcile(workspace: Workspace, action: dict) -> str:
    target = action.get("target") or {}
    kind = target.get("kind")
    item_id = target.get("resolved_id") or (action.get("resolution") or {}).get("resolved_id")
    args = action.get("args") or {}
    create_kinds = {
        "create_rcm_row": "rcm", "create_procedure": "procedure", "create_finding": "finding",
        "draft_finding_from_observation": "finding",
        "create_document_test": "doctest", "create_validation_rules": "ruleset",
        "create_custom_analysis": "analysis", "pin_dashboard_tile": "tile",
        "create_data_test": "datatest",
    }
    if action["type"] == "create_join":
        create_kinds[action["type"]] = "table"
    if action["type"] in create_kinds:
        identity = args.get("name") if action["type"] == "create_join" else args.get("id")
        existing = artifact_snapshot(workspace, create_kinds[action["type"]], str(identity or ""))
        if existing is None:
            return "retry"
        return "already_applied" if postcondition_matches(existing, action.get("postcondition") or {}) else "conflict"
    current = artifact_snapshot(workspace, str(kind), str(item_id)) if kind and item_id else None
    if action["type"].startswith("delete_") or action["type"] == "remove_dashboard_tile":
        return "already_applied" if current is None else "retry"
    if postcondition_matches(current, action.get("postcondition") or {}):
        return "already_applied"
    receipt = action.get("receipt") or {}
    if current is not None and receipt.get("post_sha1") == artifact_index.canonical_sha1(current):
        return "already_applied"
    pre = action.get("precondition") or {}
    if current is not None and pre.get("artifact_sha1") == artifact_index.canonical_sha1(current):
        return "retry"
    return "conflict" if pre else "retry"


def _restore_snapshot(workspace: Workspace, kind: str, item_id: str, before: dict) -> dict:
    if kind == "planning":
        workspace.planning = copy.deepcopy(before); workspace.save(); return workspace.planning
    collections = {
        "rcm": workspace.rcm, "procedure": workspace.work_program, "finding": workspace.findings,
        "analysis": workspace.analyses, "ruleset": workspace.rulesets, "tile": workspace.tiles,
        "datatest": workspace.data_tests, "observation": workspace.observations,
    }
    if kind in collections:
        collection = collections[kind]
        index = next((position for position, item in enumerate(collection) if str(item.get("id")) == item_id), None)
        if index is None:
            raise WorkspaceError("The artifact to restore no longer exists.")
        collection[index] = copy.deepcopy(before); workspace.save(); return collection[index]
    if kind == "doctest":
        return doc_tests.save_test(workspace, copy.deepcopy(before))
    if kind == "doctest_item":
        test_id, _, child_id = item_id.partition(":")
        test = doc_tests.load_test(workspace, test_id)
        index = next((position for position, item in enumerate(test["items"]) if item["id"] == child_id), None)
        if index is None:
            raise WorkspaceError("The document-test item to restore no longer exists.")
        test["items"][index] = copy.deepcopy(before); doc_tests.save_test(workspace, test); return test["items"][index]
    if kind == "report":
        workspace.report = copy.deepcopy(before); workspace.save(); return workspace.report
    raise WorkspaceError("This artifact kind cannot be restored by undo.")


def _schema(required=(), properties=None) -> dict:
    return {"type": "object", "required": list(required), "properties": properties or {}, "additionalProperties": True}


OBJ = {"type": "object"}; STR = {"type": "string"}; ARR = {"type": "array"}
ARR_STR = {"type": "array", "items": STR}
PYTHON_SPEC = {
    "type": "object", "required": ["code"],
    "properties": {"code": {
        "type": "string",
        "description": (
            "In-memory Polars only: pl and workspace tables are already available; "
            "no imports or file I/O; assign the aggregate output to result."
        ),
    }},
    "additionalProperties": True,
}
RULE_SPEC = {
    "type": "object", "required": ["check"],
    "properties": {
        "column": STR,
        "check": {"type": "string", "enum": list(validation.CHECKS)},
        "params": OBJ,
    },
    "additionalProperties": True,
}


def _register(
    type_: str,
    description: str,
    risk: str,
    targets=(),
    required=(),
    properties=None,
    model="none",
    failure="stop_dependents",
    planning_significant=False,
):
    REGISTRY.register(ActionDefinition(
        type_, 1, description, _schema(required, properties), {"type": "object"}, tuple(targets),
        risk, model, failure, _execute,
        _reconcile if risk not in {"read", "compute"} else None,
        lambda result: result,
        planning_significant,
    ))


_register("update_planning_context", "Update engagement planning context", "reversible_mutation", ("planning",), ("changes",), {"changes": OBJ})
_register("edit_apm", "Edit APM content", "reversible_mutation", ("planning",), ("apm_markdown",), {"apm_markdown": STR}, model="draft")
_register(
    "create_rcm_row", "Create one complete RCM row", "create", required=("risk",),
    properties={
        "process": STR, "risk": STR,
        "risk_rating": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "assertion": STR, "control": STR, "control_type": STR, "test_procedure": STR,
    },
)
_register("edit_rcm_row", "Edit one RCM row", "reversible_mutation", ("rcm",), ("changes",), {"changes": OBJ})
_register("delete_rcm_row", "Delete one RCM row", "destructive", ("rcm",))
_register(
    "create_procedure", "Legacy compatibility: create an audit procedure", "create", required=("objective",),
    properties={
        "rcm_refs": ARR, "objective": STR, "criteria": STR, "steps": ARR,
        "method": STR, "expected_evidence": STR,
    },
)
_register("edit_procedure", "Legacy compatibility: edit an audit procedure", "reversible_mutation", ("procedure",), ("changes",), {"changes": OBJ})
_register("delete_procedure", "Legacy compatibility: delete an audit procedure", "destructive", ("procedure",))
_register(
    "create_join", "Create a validated table join", "create",
    required=("name", "left", "right", "left_on", "right_on"),
    properties={
        "name": STR, "left": STR, "right": STR,
        "left_on": ARR_STR, "right_on": ARR_STR,
        "how": {"type": "string", "enum": list(JOIN_TYPES)},
    },
)
_register(
    "create_validation_rules", "Create validation rules", "create",
    required=("title", "table", "rules"),
    properties={"title": STR, "table": STR, "rules": {"type": "array", "items": RULE_SPEC}},
)
_register("edit_validation_rules", "Edit validation rules", "reversible_mutation", ("ruleset",), ("changes",), {"changes": OBJ})
_register("run_validation_rules", "Run a saved validation ruleset", "compute", ("ruleset",))
_register(
    "run_analytics", "Run a library audit analytic", "compute",
    required=("table", "test"),
    properties={
        "table": STR,
        "test": {"type": "string", "enum": list(analytics.ANALYTICS)},
        "params": OBJ,
    },
)
_register(
    "create_data_test", "Create an exploratory or RCM-linked durable Data Test definition", "create",
    required=("title", "objective", "engine", "table_refs", "spec"),
    properties={
        "rcm_id": STR, "title": STR, "objective": STR, "criteria": STR,
        "steps": ARR,
        "engine": {"type": "string", "enum": sorted(data_tests.ENGINES)},
        "table_refs": ARR_STR, "spec": OBJ,
    },
)
_register("edit_data_test", "Edit a durable Data Test definition", "reversible_mutation", ("datatest",), ("changes",), {"changes": OBJ})
_register(
    "run_data_test",
    "Execute a Data Test and preserve its immutable bounded result",
    "compute",
    ("datatest",),
    model="interpret_result",
    planning_significant=True,
)
_register(
    "link_test_to_rcm_row",
    "Link a test to an RCM row, or unlink it by passing an empty rcm_id",
    "reversible_mutation", ("datatest", "doctest"), ("rcm_id",),
    {"rcm_id": STR},
)
_register("create_custom_analysis", "Create an in-memory sandboxed Polars analysis (no imports or file I/O; assign result)", "create", required=("title", "spec"), properties={"title": STR, "spec": PYTHON_SPEC}, model="draft")
_register("edit_custom_analysis", "Edit a custom analysis", "reversible_mutation", ("analysis",), ("changes",), {"changes": OBJ}, model="draft")
_register("run_custom_analysis", "Run a saved custom analysis", "compute", ("analysis",))
_register("pin_dashboard_tile", "Pin a dashboard tile", "create", required=("kind", "title", "spec"))
_register("edit_dashboard_tile", "Edit a dashboard tile", "reversible_mutation", ("tile",), ("changes",), {"changes": OBJ})
_register("remove_dashboard_tile", "Remove a dashboard tile", "destructive", ("tile",))
_register(
    "create_document_test", "Create a document test", "create",
    required=("kind", "title"),
    properties={
        "kind": {"type": "string", "enum": ["vouching", "attribute", "review", "qa"]},
        "title": STR, "items": ARR, "rcm_id": STR, "objective": STR,
        "criteria": STR, "steps": ARR,
        "table": STR, "frozen_fields": ARR_STR, "identifier_fields": ARR_STR,
        "size": {"type": "integer"}, "seed": {"type": "integer"},
        "direction": {"type": "string", "enum": ["vouching", "tracing"]},
        "document_id": STR, "document_ids": ARR_STR, "pages": ARR,
        "questions": ARR_STR, "review_kind": STR, "attributes": ARR,
    },
)
_register("edit_document_test", "Edit a document test", "reversible_mutation", ("doctest",), ("changes",), {"changes": OBJ})
_register("delete_document_test", "Delete a document test", "destructive", ("doctest",))
_register("attach_document_to_test", "Attach a document to a test item", "reversible_mutation", ("doctest_item",), ("document_id",), {"document_id": STR})
_register("detach_document_from_test", "Detach a document from a test item", "reversible_mutation", ("doctest_item",), ("document_id",), {"document_id": STR})
_register("update_test_comparisons", "Update document-test comparisons", "reversible_mutation", ("doctest_item",), ("checks",), {"checks": ARR})
_register("update_test_disposition", "Update a test-item disposition", "reversible_mutation", ("doctest_item",), ("changes",), {"changes": OBJ})
_register(
    "disposition_observation", "Record the auditor disposition for an execution observation",
    "reversible_mutation", ("observation",), ("disposition",),
    {"disposition": {"type": "string", "enum": sorted(OBSERVATION_DISPOSITIONS)}, "auditor_note": STR},
)
_register("generate_rcm_working_paper", "Generate an RCM-linked working paper", "compute", ("rcm",))
_register("generate_working_paper", "Legacy compatibility: generate a procedure working paper", "reversible_mutation", ("procedure",))
FINDING_PROPERTIES = {
    "title": STR,
    "severity": {"type": "string", "enum": list(findings.SEVERITIES)},
    "condition": STR, "criteria": STR, "cause": STR, "cause_pending": {"type": "boolean"},
    "effect": STR, "recommendation": STR, "severity_rationale": STR,
    "management_response": STR, "rcm_refs": ARR_STR, "test_refs": ARR_STR,
    "procedure_refs": ARR_STR, "execution_refs": ARR_STR, "evidence_refs": ARR,
}
_register("create_finding", "Create an evidence-linked finding", "create", required=("title",), properties=FINDING_PROPERTIES, model="draft")
_register(
    "draft_finding_from_observation",
    "Draft a fully supported finding from an auditor-dispositioned observation",
    "create", ("observation",),
    required=(
        "title", "severity", "condition", "criteria", "effect",
        "recommendation", "severity_rationale",
    ),
    properties=FINDING_PROPERTIES,
    model="draft",
)
_register("edit_finding", "Edit one finding", "reversible_mutation", ("finding",), ("changes",), {"changes": OBJ}, model="draft")
_register("delete_finding", "Delete one finding", "destructive", ("finding",))
_register("promote_agent_finding", "Promote an agent observation", "create", required=("run_id", "finding_id"))
_register("edit_report", "Edit report working content", "reversible_mutation", ("report",), ("changes",), {"changes": OBJ}, model="draft")
_register("reconcile_report", "Reconcile generated and edited report content", "broad_rewrite", ("report",), ("action",), {"action": {"type": "string", "enum": ["keep", "replace"]}})
_register("run_report_quality", "Run deterministic report quality checks", "compute")
_register("classify_import_batch", "Classify a staged import batch", "compute", required=("batch_id",), properties={"batch_id": STR}, model="plan")
_register("apply_import_batch", "Apply a classified import batch", "create", required=("batch_id",), properties={"batch_id": STR})
_register("undo_action", "Undo an eligible reversible action", "reversible_mutation", required=("action_id",), properties={"action_id": STR, "run_id": STR})


ACTION_COVERAGE = [
    {"operation": definition.description, "action": definition.type, "risk": definition.risk, "version": definition.version}
    for definition in REGISTRY.all()
]
