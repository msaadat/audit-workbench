"""Command routing before workflow or action scheduler dispatch."""

from __future__ import annotations

import os
import uuid

from .. import doc_tests
from ..workspaces import Workspace, WorkspaceError, load_workspace
from . import capabilities as audit_capabilities
from . import context_bundles, prompts, store, workflow
from .base import BaseRunner, LimitExceeded
from .workflows import analysis as analysis_workflow
from .workflows import audit as audit_workflow
from .workflows import doc_tests as doc_tests_workflow
from .workflows import documents as documents_workflow


WORKFLOW_MODULES = {
    audit_workflow.WORKFLOW_ID: audit_workflow,
    analysis_workflow.WORKFLOW_ID: analysis_workflow,
    documents_workflow.WORKFLOW_ID: documents_workflow,
    doc_tests_workflow.WORKFLOW_ID: doc_tests_workflow,
}

ELIGIBLE_DISPOSITIONS = {
    "confirmed_control_exception",
    "draft_finding_candidate",
}


# --------------------------------------------------------------------------- #
# Bounded router worker
#
# The router is a single-turn classifier over the command and the current
# capability readiness projection. It proposes no actions, workers, or
# dependencies, and it only ever names registered outcome IDs.
# --------------------------------------------------------------------------- #
ROUTER_SYSTEM = f"""[agent:workflow_router]
Classify one audit-assistant command. You are a router, not a planner. Return
route (workflow|generic_action|question|unsupported), requested_outcomes (only
supported outcome IDs), objective, target_refs, generation_mode
(reuse_existing|force), action_intent (null or a short normalized operation),
constraints, needs_clarification, and clarification. Never propose actions,
workers, dependencies, tests, columns, or execution steps. {prompts.JSON_RULES}"""

def validate_route(payload: dict, supported: set[str]) -> dict:
    route = str(payload.get("route") or "")
    if route not in {"workflow", "generic_action", "question", "unsupported"}:
        raise ValueError("route is unsupported")
    outcomes = payload.get("requested_outcomes") or []
    if not isinstance(outcomes, list) or any(str(item) not in supported for item in outcomes):
        raise ValueError("requested_outcomes contains an unsupported capability")
    generation_mode = str(payload.get("generation_mode") or "reuse_existing")
    if generation_mode not in {"reuse_existing", "force"}:
        raise ValueError("generation_mode is unsupported")
    needs = bool(payload.get("needs_clarification"))
    if route == "workflow" and not outcomes and not needs:
        raise ValueError("a workflow route needs at least one requested outcome")
    return {
        "route": route,
        "requested_outcomes": [str(item) for item in outcomes],
        "objective": str(payload.get("objective") or "").strip(),
        "target_refs": [str(item) for item in payload.get("target_refs") or []],
        "generation_mode": generation_mode,
        "action_intent": str(payload.get("action_intent") or "").strip() or None,
        "constraints": [str(item) for item in payload.get("constraints") or []],
        "needs_clarification": needs,
        "clarification": str(payload.get("clarification") or "").strip() or None,
    }


def _resolve_command(runner, bundle: context_bundles.ContextBundle, supported: set[str]) -> dict:
    return runner.llm_json(
        ROUTER_SYSTEM,
        bundle.serialized(),
        activity={"context_metrics": bundle.metrics()},
        validator=lambda payload: validate_route(payload, supported),
    )


# Phrases that keep an "analysis" request an isolated ActionRunner operation.
# Running or pinning an existing saved analysis is a single mutation, not a
# request to derive relationships, joins, and new definitions.
ISOLATED_ANALYSIS_MARKERS = (
    "pin ",
    "rerun ",
    "re-run ",
    "run this",
    "run the saved",
    "run the existing",
    "delete ",
    "remove ",
    "rename ",
    "undo ",
)

# Deterministic phrases for the exploratory data-analysis workflow. These are
# checked before the generic-action markers so "join the tables and analyse
# them" becomes a declared workflow rather than an action DAG.
ANALYSIS_PHRASES = (
    "relevant joins",
    "perform relevant joins",
    "joins and data analysis",
    "join the tables",
    "join these tables",
    "relationships between tables",
    "relationships between the tables",
    "relate the tables",
    "data analysis",
    "analyse the data",
    "analyze the data",
    "analyse the tables",
    "analyze the tables",
    "analyse these tables",
    "analyze these tables",
    "analyse the two tables",
    "analyze the two tables",
    "explore the data",
    "explore the tables",
)

# Deterministic phrases for the document-analysis workflow. Checked before the
# generic-action markers so "analyse these documents" becomes a declared workflow
# rather than an action DAG. "Attach", "upload", and "delete" a document stay
# isolated ActionRunner operations and are deliberately absent.
DOCUMENT_ANALYSIS_PHRASES = (
    "analyse the documents",
    "analyze the documents",
    "analyse these documents",
    "analyze these documents",
    "analyse the selected documents",
    "analyze the selected documents",
    "analyse this document",
    "analyze this document",
    "document analysis",
    "analyse the policies",
    "analyze the policies",
    "summarise the documents",
    "summarize the documents",
    "read the documents",
)


def _workflow_definition_for(outcomes: list[str]) -> str:
    definition = audit_capabilities.workflow_for_outcomes(outcomes)
    if definition is None:
        raise WorkspaceError(
            "The requested outcomes do not belong to one registered workflow."
        )
    return definition


def local_resolution(command: dict) -> dict | None:
    """Resolve known outcomes and isolated actions without a provider call."""

    direct = command.get("requested_outcomes")
    if isinstance(direct, list) and direct:
        requested = [str(item) for item in direct]
        definition = _workflow_definition_for(requested)
        audit_capabilities.REGISTRY_BY_WORKFLOW[definition].closure(requested)
        return {
            "route": "workflow",
            "workflow_definition": definition,
            "requested_outcomes": requested,
            "objective": str(
                command.get("text") or "Continue the requested audit outcomes."
            ).strip(),
            "target_refs": [
                str(item)
                for item in command.get("target_refs") or ["workspace:current"]
            ],
            "generation_mode": workflow.command_generation_mode(command),
            "action_intent": None,
            "constraints": [str(item) for item in command.get("constraints") or []],
            "needs_clarification": False,
            "clarification": None,
        }
    template = str(command.get("goal_template") or "")
    if template == "document_testing":
        return {
            "route": "generic_action",
            "requested_outcomes": [],
            "objective": str(command.get("text") or "").strip(),
            "target_refs": [],
            "generation_mode": "reuse_existing",
            "action_intent": template,
            "constraints": [],
            "needs_clarification": False,
            "clarification": None,
        }
    outcomes = (
        audit_capabilities.outcomes_for_template(template)
        or audit_capabilities.analysis_outcomes_for_template(template)
        or audit_capabilities.document_outcomes_for_template(template)
        or audit_capabilities.doc_test_outcomes_for_template(template)
    )
    if outcomes is not None:
        return {
            "route": "workflow",
            "workflow_definition": _workflow_definition_for(outcomes),
            "requested_outcomes": outcomes,
            "objective": str(
                command.get("text") or template.replace("_", " ")
            ).strip(),
            "target_refs": [
                str(item)
                for item in command.get("target_refs") or ["workspace:current"]
            ],
            "generation_mode": workflow.command_generation_mode(command),
            "action_intent": None,
            "constraints": [],
            "needs_clarification": False,
            "clarification": None,
        }
    text = str(command.get("text") or "").casefold()
    if any(phrase in text for phrase in DOCUMENT_ANALYSIS_PHRASES):
        return {
            "route": "workflow",
            "workflow_definition": documents_workflow.WORKFLOW_ID,
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "objective": str(command.get("text") or "").strip(),
            "target_refs": [
                str(item)
                for item in command.get("target_refs") or ["workspace:current"]
            ],
            "generation_mode": workflow.command_generation_mode(command),
            "action_intent": None,
            "constraints": [],
            "needs_clarification": False,
            "clarification": None,
        }
    if not any(marker in text for marker in ISOLATED_ANALYSIS_MARKERS) and any(
        phrase in text for phrase in ANALYSIS_PHRASES
    ):
        return {
            "route": "workflow",
            "workflow_definition": analysis_workflow.WORKFLOW_ID,
            "requested_outcomes": list(analysis_workflow.FULL_ANALYSIS_OUTCOMES),
            "objective": str(command.get("text") or "").strip(),
            "target_refs": [
                str(item)
                for item in command.get("target_refs") or ["workspace:current"]
            ],
            "generation_mode": workflow.command_generation_mode(command),
            "action_intent": None,
            "constraints": [],
            "needs_clarification": False,
            "clarification": None,
        }
    mappings = [
        (
            ("full audit", "complete the audit", "end-to-end audit", "end to end audit"),
            audit_capabilities.FULL_AUDIT_OUTCOMES,
        ),
        (
            (
                "draft the apm",
                "update the apm",
                "generate apm",
                "generate the apm",
                "regenerate the apm",
                "refresh the apm",
                "improve the apm",
                "audit planning memorandum",
            ),
            ["planning.apm_ready"],
        ),
        (
            (
                "generate the rcm",
                "draft the rcm",
                "update the rcm",
                "regenerate the rcm",
                "refresh the rcm",
                "risk and control matrix",
            ),
            ["planning.rcm_ready"],
        ),
        (("testing procedures", "planned procedures", "planned tests"), ["planning.planned_tests_ready"]),
        (("translate planned", "executable tests", "execution definitions"), ["fieldwork.definitions_ready"]),
        (("run the rcm tests", "execute the rcm tests", "run rcm tests", "execute planned tests"), ["fieldwork.executed", "results.rolled_up"]),
        (("draft eligible findings", "draft findings"), ["findings.drafted"]),
        (("generate the report", "draft the report", "audit report"), ["report.working_draft"]),
    ]
    for phrases, requested in mappings:
        if any(phrase in text for phrase in phrases):
            if requested == ["planning.planned_tests_ready"] and any(
                word in text for word in ("run ", "execute ")
            ):
                continue
            return {
                "route": "workflow",
                "workflow_definition": audit_workflow.WORKFLOW_ID,
                "requested_outcomes": list(requested),
                "objective": str(command.get("text") or "").strip(),
                "target_refs": ["workspace:current"],
                "generation_mode": workflow.command_generation_mode(command),
                "action_intent": None,
                "constraints": [],
                "needs_clarification": False,
                "clarification": None,
            }
    generic_markers = (
        "join ",
        "rename ",
        "remove ",
        "delete ",
        "add a finding",
        "create a finding",
        "validate ",
        "validation",
        "check report quality",
        "pin ",
        "rerun ",
        "analyze ",
        "analyse ",
        "analysis",
        "upload ",
        "attach ",
        "detach ",
        "document test",
        "prepare report",
        "finding",
        " undo ",
        "review the apm",
    )
    if any(marker in text for marker in generic_markers):
        return {
            "route": "generic_action",
            "requested_outcomes": [],
            "objective": str(command.get("text") or "").strip(),
            "target_refs": [],
            "generation_mode": "reuse_existing",
            "action_intent": "isolated_mutation",
            "constraints": [],
            "needs_clarification": False,
            "clarification": None,
        }
    return None


def _explanation(
    resolved: list[str],
    stages: list[dict],
    reused: list[str],
    requested: list[str],
) -> str:
    running = [stage["capability"] for stage in stages]
    automatically_added = [item for item in resolved if item not in requested]
    parts = [f"Requested outcome(s): {', '.join(requested)}."]
    if automatically_added:
        parts.append("Added prerequisite(s): " + ", ".join(automatically_added) + ".")
    if reused:
        parts.append(
            "Reusing capability output(s) with currency not assessed: "
            + ", ".join(reused)
            + "."
        )
    if running:
        parts.append("Running in dependency order: " + " → ".join(running) + ".")
    return " ".join(parts)


def _audit_model_turns(workspace: Workspace) -> int:
    """Size the audit model budget from real RCM, planned-test, and Q&A counts."""

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
    return (
        20
        + 4 * len(workspace.rcm)
        + 4 * planned_count
        + 2 * qa_pairs
        + 2 * eligible_findings
    )


def document_page_limit() -> int:
    """The configured per-analysis page bound, or 0 when unbounded.

    Resolved once at routing time and persisted on the run's scope, so a run's
    coverage bound is durable and cannot change under a resume because the
    environment did.
    """
    try:
        return max(0, int(os.environ.get("DOCUMENT_ANALYSIS_PAGE_LIMIT") or 0))
    except ValueError:
        return 0


def _document_model_turns(workspace: Workspace, scope: dict) -> int:
    """Size the document budget from the chunks actually in scope.

    Document analysis is one turn per bounded source chunk plus one reduction per
    document, so the budget follows the resolved scope and the real chunk count.
    A document with no cached extraction yet contributes a conservative estimate;
    the composition refreshes the budget once extraction has run.
    """

    from .capabilities.documents import chunk_specs, resolve_document_scope

    document_scope = resolve_document_scope(workspace, scope)
    chunks = sum(
        len(chunk_specs(workspace, document_id, scope)) or 1
        for document_id in document_scope.document_ids
    )
    return 4 + chunks + 2 * max(1, len(document_scope.document_ids))


def _doc_test_model_turns(workspace: Workspace, scope: dict) -> int:
    """Size the document-test budget from the Q&A pairs actually in scope.

    Only the Q&A unit kind calls the model, once per unanswered item/document
    pair; deterministic comparison, review, and disposition units never do.
    """

    from .capabilities.doc_tests import scoped_tests

    qa_pairs = sum(
        len(item.get("document_ids") or [])
        for test in scoped_tests(workspace, scope)
        if test.get("kind") == "qa"
        for item in test.get("items") or []
    )
    return 4 + 2 * qa_pairs


def _analysis_model_turns(workspace: Workspace, scope: dict) -> int:
    """Size the analysis model budget from the frames actually in scope.

    Only ``analysis.definitions_ready`` calls the model, once per target frame,
    so the budget follows the resolved scope rather than the whole workspace.
    """

    from .capabilities.analysis import resolve_table_scope

    table_scope = resolve_table_scope(workspace, scope)
    return 10 + 2 * max(1, len(table_scope.targets))


def install_resolution(workspace: Workspace, run: dict, resolution: dict) -> None:
    """Materialize a validated workflow route on the durable run."""

    definition_id = str(
        resolution.get("workflow_definition")
        or _workflow_definition_for(list(resolution.get("requested_outcomes") or []))
    )
    if definition_id not in WORKFLOW_MODULES:
        raise WorkspaceError(f"Unsupported workflow definition '{definition_id}'.")
    definition = WORKFLOW_MODULES[definition_id]
    registry = audit_capabilities.REGISTRY_BY_WORKFLOW[definition_id]
    analysis_route = definition_id == analysis_workflow.WORKFLOW_ID
    document_route = definition_id == documents_workflow.WORKFLOW_ID
    doc_test_route = definition_id == doc_tests_workflow.WORKFLOW_ID
    # The audit graph declares the scoped document capabilities, so an audit run
    # also carries the document scope and coverage bound.
    document_scope_route = document_route or definition_id == audit_workflow.WORKFLOW_ID
    generation_mode = workflow.normalize_generation_mode(
        resolution.get("generation_mode") or "reuse_existing"
    )
    scope = {
        "target_refs": list(resolution.get("target_refs") or ["workspace:current"]),
        "permission_mode": run.get("mode") == "permission",
        "generation_mode": generation_mode,
    }
    if document_scope_route:
        # A resolved document scope is durable on the workflow record, so a
        # checkpoint answer or an explicitly selected document survives a resume.
        # Explicitly named documents normally arrive as ``document:<id>`` target
        # refs, which the scope resolver reads directly.
        scope["document_ids"] = [
            str(value)
            for value in (
                resolution.get("document_ids")
                or (run.get("context") or {}).get("document_ids")
                or []
            )
        ]
        scope["page_limit"] = document_page_limit()
    if doc_test_route:
        # A resolved Document Test scope is durable on the workflow record, so a
        # resumed run executes exactly the worklists the request named.
        # Explicitly named tests normally arrive as ``doctest:<id>`` target refs,
        # which the scope resolver reads directly.
        scope["test_ids"] = [
            str(value)
            for value in (
                resolution.get("test_ids")
                or (run.get("context") or {}).get("test_ids")
                or ([(run.get("context") or {}).get("test_id")]
                    if (run.get("context") or {}).get("test_id")
                    else [])
            )
        ]
    if analysis_route:
        # A resolved table scope is durable on the workflow record, so a
        # checkpoint answer or a router-supplied selection survives a resume.
        # Explicitly named tables normally arrive as ``table:<name>`` target
        # refs, which the scope resolver reads directly.
        scope["tables"] = [str(value) for value in resolution.get("tables") or []]
    requested = list(resolution.get("requested_outcomes") or [])
    resolved, stages, reused = workflow.materialize(
        registry,
        workspace,
        requested,
        scope,
        generation_mode=generation_mode,
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
    if analysis_route:
        calculated_model_turns = _analysis_model_turns(workspace, scope)
    elif doc_test_route:
        calculated_model_turns = _doc_test_model_turns(workspace, scope)
    elif document_route:
        calculated_model_turns = _document_model_turns(workspace, scope)
    else:
        # An audit run also pays for the scoped document analyses planning
        # depends on, so its budget covers both.
        calculated_model_turns = _audit_model_turns(workspace) + _document_model_turns(
            workspace, scope
        )
    run.setdefault("limits", {}).update(
        max_llm_concurrency=int(
            run.get("limits", {}).get("max_llm_concurrency") or 4
        ),
        max_compute_concurrency=int(
            run.get("limits", {}).get("max_compute_concurrency") or 2
        ),
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
        "objective": resolution.get("objective")
        or (run.get("command") or {}).get("text")
        or "",
        "constraints": list(resolution.get("constraints") or []),
        "completion_criteria": requested,
    }
    run["workflow"] = {
        "definition": definition.WORKFLOW_ID,
        "definition_hash": definition.definition_hash(),
        "revision": 1,
        "route": "workflow",
        "requested_outcomes": requested,
        "target_refs": scope["target_refs"],
        "scope": scope,
        "generation_mode": generation_mode,
        "workflow_explanation": explanation,
        "next_outcomes": [],
        "pending_checkpoint": None,
        "resolved_capabilities": resolved,
        "reused_capabilities": reused,
        "reused_capability_details": [
            {
                "capability": capability_id,
                "currency_status": "not_assessed",
            }
            for capability_id in reused
        ],
        "workspace_revision": workspace.revision,
        "state_at_resolution": registry.workflow_state(workspace, scope),
        "stages": stages,
        "legacy_adoptions": [],
    }
    run["workflow_explanation"] = explanation
    run["command"]["status"] = "resolved"
    if document_scope_route:
        run.setdefault(
            "document_analysis",
            {
                "document_ids": list(scope.get("document_ids") or []),
                "action": str(
                    (run.get("context") or {}).get("action")
                    or ("refresh" if generation_mode == "force" else "analyze")
                ),
                "scope_settled": False,
            },
        )
        run["workflow"]["document_action"] = run["document_analysis"]["action"]
    if analysis_route:
        run.setdefault("analysis", {"relationships": []})
        return
    if document_route or doc_test_route:
        return
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


def initialize_known_workflow(workspace: Workspace, run: dict) -> bool:
    """Persist a deterministic route before the worker thread starts."""

    resolution = local_resolution(run.get("command") or {})
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
    install_resolution(workspace, run, resolution)
    store.save_run(workspace, run)
    return True


class CommandRouter(BaseRunner):
    """Bounded pre-dispatch router using the shared runtime and gateway."""

    def resolve(self) -> dict:
        local = local_resolution(self.run.get("command") or {})
        if local is not None:
            return local
        self.set_status("interpreting")
        # The router classifies against every registered workflow, so an
        # unresolved data-analysis request can name an analysis outcome instead
        # of falling through to the generic action interpreter.
        state = {
            **audit_capabilities.workflow_state(self.ws),
            **audit_capabilities.analysis_workflow_state(self.ws),
            **audit_capabilities.documents_workflow_state(self.ws),
            **audit_capabilities.doc_tests_workflow_state(self.ws),
        }
        supported = {
            capability.id
            for registry in audit_capabilities.REGISTRY_BY_WORKFLOW.values()
            for capability in registry.all()
        }
        bundle = context_bundles.command_router(
            self.run.get("command") or {},
            state,
            sorted(supported),
            permission_mode=self.run["mode"],
        )
        resolution = _resolve_command(self, bundle, supported)
        self.run["partial_resolution"] = resolution
        self.save()
        if resolution.get("needs_clarification"):
            answer = self._clarification(
                str(
                    resolution.get("clarification")
                    or "Please clarify the intended audit outcome."
                )
            )
            command = dict(self.run.get("command") or {})
            command["text"] = (
                f"{command.get('text') or ''}\n\nClarification: {answer}".strip()
            )
            fresh = load_workspace(self.ws.id)
            state = {
                **audit_capabilities.workflow_state(fresh),
                **audit_capabilities.analysis_workflow_state(fresh),
                **audit_capabilities.documents_workflow_state(fresh),
                **audit_capabilities.doc_tests_workflow_state(fresh),
            }
            bundle = context_bundles.command_router(
                command,
                state,
                sorted(supported),
                permission_mode=self.run["mode"],
            )
            resolution = _resolve_command(self, bundle, supported)
            if resolution.get("needs_clarification"):
                raise WorkspaceError(
                    "The command still needs clarification after the supplied answer."
                )
        return resolution

    def _clarification(self, prompt: str) -> str:
        interaction = next(
            (
                item
                for item in self.run.get("interactions") or []
                if item.get("type") == "clarification"
                and item.get("status") == "pending"
            ),
            None,
        )
        if interaction is None:
            interaction = {
                "id": f"int_{uuid.uuid4().hex[:12]}",
                "action_id": "workflow:resolver",
                "type": "clarification",
                "prompt": prompt,
                "options": [],
                "payload": {
                    "original_command": (self.run.get("command") or {}).get("text")
                },
                "policy_reason": (
                    "The answer materially changes the requested audit outcome."
                ),
                "status": "pending",
                "response": None,
                "actor": None,
                "created_at": store.utcnow(),
                "resolved_at": None,
            }
            self.run.setdefault("interactions", []).append(interaction)
            self.save()
            self.emit("checkpoint_request", {"interaction": interaction})
        response = self.runtime.wait_for_interaction(interaction)
        text = str(response.get("text") or "").strip()
        if not text:
            raise WorkspaceError("A clarification response is required.")
        self.runtime.resolve_interaction(interaction, response)
        return text


def route_unresolved_run(
    workspace: Workspace,
    run: dict,
    handle: object,
) -> str | None:
    """Persist one route before scheduler selection and return its engine."""

    router = CommandRouter(workspace, run, handle)
    if not run.get("started"):
        router.mark_started()
    resolution = router.resolve()
    route = resolution.get("route")
    run["command_route"] = resolution
    if route == "generic_action":
        run["engine"] = store.ACTION_ENGINE
        run["schema_version"] = 2
        router.save()
        return store.ACTION_ENGINE
    if route in {"question", "unsupported"}:
        run["summary_markdown"] = resolution.get("clarification") or (
            "This request is not available as an audit workflow."
        )
        run["command"]["status"] = "completed"
        router.mark_finished()
        router.set_status("completed_with_open_items")
        return None
    if route != "workflow":
        raise WorkspaceError(f"Unsupported command route '{route}'.")
    run["engine"] = store.WORKFLOW_ENGINE
    install_resolution(workspace, run, resolution)
    router.save()
    router.emit("workflow_resolved", {"workflow": run["workflow"]})
    router.emit("workflow_explanation", {"text": run["workflow_explanation"]})
    return store.WORKFLOW_ENGINE


__all__ = [
    "CommandRouter",
    "initialize_known_workflow",
    "install_resolution",
    "local_resolution",
    "route_unresolved_run",
]
