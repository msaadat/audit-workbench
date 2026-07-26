"""Command routing: one classification, one persisted route, one engine.

This module is the only place a request is turned into an execution decision.
It has two halves and a strict boundary between them:

* **Deterministic classification** (:func:`classify_command` and the pure
  helpers above it) reads only the durable command dict. It never loads a
  workspace, executes an action, gathers domain context, or mutates anything.
  It returns a normalized route or ``None`` — ``None`` means "the bounded
  router worker has to decide".
* **Route installation** (:func:`resolve_route`, :func:`resolve_pending_route`,
  :func:`install_resolution`) persists exactly one normalized route and the
  selected engine on the durable run. ``resolve_route`` runs synchronously in
  ``runner.start_command_run`` before the worker thread launches;
  ``resolve_pending_route`` runs on the worker thread and is the *only* path
  that spends a model turn on routing.

A request is classified once. Deterministic classification happens in
``start_command_run``; the bounded router runs only when it returned ``None``,
and it never re-runs the deterministic pass. Neither scheduler classifies, and
neither scheduler calls the other.

Routing precedence (``agent-architecture.md`` §Routing):

1. Explicit registered outcomes.
2. A registered goal template.
3. A lifecycle-wide completion request.
4. Generation or refresh of a workflow-owned deliverable.
5. A target-specific operation — CRUD, attach/detach, pin, manual edit, or a
   rerun of one identified existing artifact.
6. Scope-wide declared execution.
7. A weak isolated-operation marker (the last deterministic fallback).
8. Otherwise the bounded router worker.

A compound request whose segments genuinely need both engines is never split
by a scheduler: it resolves to ``clarification`` so the auditor restates it as
separate runs.
"""

from __future__ import annotations

import os
import re
import uuid

from .. import doc_tests
from ..workspaces import Workspace, WorkspaceError, load_workspace
from . import actions as action_catalog
from . import capabilities as audit_capabilities
from . import context_bundles, narration, prompts, store, workflow
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

# The four normalized routing results. ``workflow`` and ``action`` select an
# engine; ``clarification`` and ``unsupported`` finish the run without one.
ROUTE_WORKFLOW = "workflow"
ROUTE_ACTION = "action"
ROUTE_CLARIFICATION = "clarification"
ROUTE_UNSUPPORTED = "unsupported"
ROUTES = (ROUTE_WORKFLOW, ROUTE_ACTION, ROUTE_CLARIFICATION, ROUTE_UNSUPPORTED)
TERMINAL_ROUTES = frozenset({ROUTE_CLARIFICATION, ROUTE_UNSUPPORTED})
ENGINE_BY_ROUTE = {
    ROUTE_WORKFLOW: store.WORKFLOW_ENGINE,
    ROUTE_ACTION: store.ACTION_ENGINE,
    ROUTE_CLARIFICATION: None,
    ROUTE_UNSUPPORTED: None,
}

# The deterministic intent an action route carries when no single registered
# action type names the request. The action interpreter still plans the DAG;
# this is the routing-level assertion that the request is an isolated mutation.
ISOLATED_ACTION_INTENT = "isolated_mutation"


# --------------------------------------------------------------------------- #
# Registered goal templates
#
# A goal template is a caller-supplied routing shortcut. Every registered
# template resolves to a declared workflow outcome set; there is no template
# that routes to the action catalog, because an isolated artifact operation is
# described by its own text, not by a lifecycle goal.
# --------------------------------------------------------------------------- #
GOAL_TEMPLATES: dict[str, dict] = {
    "full_audit_working_draft": {
        "objective": (
            "Execute RCM-linked planned tests through an evidence-linked report "
            "working draft."
        ),
        "constraints": [
            "Do not assert a formal audit opinion.",
            "Preserve auditor edits.",
        ],
    },
    "planning": {
        "objective": (
            "Prepare or improve engagement planning and structured RCM planned "
            "tests."
        ),
    },
    "apm_only": {"objective": "Prepare or revise only the audit planning memorandum."},
    "report": {
        "objective": (
            "Prepare evidence-linked audit report working content and run "
            "quality checks."
        ),
    },
    "data_analysis": {
        "objective": "Analyze available structured data and preserve useful validated work.",
    },
    "table_relationships": {
        "objective": "Infer table relationships and materialize supported joins.",
    },
    "document_analysis": {"objective": "Analyze the documents in scope."},
    "document_test_preparation": {
        "objective": "Translate RCM planned tests into executable test definitions.",
    },
    "document_test_execution": {"objective": "Execute the Document Tests in scope."},
}

# Run-context keys a template may carry from a caller. Anything else is
# rejected: run context is scope, never a routing override.
TEMPLATE_RUN_CONTEXT_KEYS: dict[str, frozenset[str]] = {
    "planning": frozenset({"document_ids"}),
    "document_analysis": frozenset({"document_ids", "action"}),
    "document_test_preparation": frozenset(),
    "document_test_execution": frozenset({"test_id", "test_ids"}),
}


def template_outcomes(template: str) -> list[str] | None:
    """Registered outcome set for a goal template, or ``None`` if unknown."""

    return (
        audit_capabilities.outcomes_for_template(template)
        or audit_capabilities.analysis_outcomes_for_template(template)
        or audit_capabilities.document_outcomes_for_template(template)
        or audit_capabilities.doc_test_outcomes_for_template(template)
    )


# --------------------------------------------------------------------------- #
# Deterministic phrase tables
# --------------------------------------------------------------------------- #

# 3. Lifecycle-wide completion.
LIFECYCLE_PHRASES = (
    "full audit",
    "complete the audit",
    "complete audit",
    "entire audit",
    "end-to-end audit",
    "end to end audit",
)

# 4. Generation or refresh of a workflow-owned deliverable. Ordered: the first
# matching rule wins, so a narrower deliverable is declared before a broader
# phrase that would also match it.
GENERATION_RULES: tuple[tuple[tuple[str, ...], str, list[str]], ...] = (
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
        audit_workflow.WORKFLOW_ID,
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
        audit_workflow.WORKFLOW_ID,
        ["planning.rcm_ready"],
    ),
    (
        (
            "prepare engagement planning",
            "prepare the engagement planning",
            "prepare planning",
            "plan the audit",
        ),
        audit_workflow.WORKFLOW_ID,
        [
            "planning.apm_ready",
            "planning.rcm_ready",
            "planning.planned_tests_ready",
        ],
    ),
    # Declared before the planned-test rule: "prepare the next required Document
    # Tests from the RCM planned tests" is a request for executable definitions,
    # not for the planned tests those definitions come from.
    (
        (
            "translate planned",
            "executable tests",
            "execution definitions",
            "prepare document tests",
            "prepare the document tests",
            "prepare the next required document tests",
            "required document tests",
        ),
        audit_workflow.WORKFLOW_ID,
        ["fieldwork.definitions_ready"],
    ),
    (
        ("testing procedures", "planned procedures", "planned tests"),
        audit_workflow.WORKFLOW_ID,
        ["planning.planned_tests_ready"],
    ),
    (
        ("draft eligible findings", "draft findings"),
        audit_workflow.WORKFLOW_ID,
        ["findings.drafted"],
    ),
    (
        ("generate the report", "draft the report", "audit report"),
        audit_workflow.WORKFLOW_ID,
        ["report.working_draft"],
    ),
    # Document analysis is workflow-owned generation. "Attach", "upload", and
    # "delete" a document stay isolated operations and are deliberately absent.
    (
        (
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
        ),
        documents_workflow.WORKFLOW_ID,
        list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
    ),
    # Executing a named Document Test is workflow-owned: the worklist fans out
    # into declared units, and a Q&A item reaches the provider only through the
    # registered ``fieldwork.document_qa`` worker. The action catalog no longer
    # registers ``run_document_test`` (P11.2A), so these phrases must be
    # declared above the target-specific "rerun"/"run this" markers.
    (
        (
            "run document test",
            "run the document test",
            "run this document test",
            "run these document tests",
            "rerun document test",
            "rerun the document test",
            "re-run the document test",
            "execute document test",
            "execute the document test",
            "run the document tests",
            "execute the document tests",
        ),
        doc_tests_workflow.WORKFLOW_ID,
        list(doc_tests_workflow.FULL_DOC_TEST_OUTCOMES),
    ),
)

# A generation phrase is vetoed when the request names one concrete part of the
# artifact: "replace this APM paragraph" is a target-specific edit even though
# it mentions the APM.
SPECIFIC_EDIT_VERBS = (
    "replace",
    "edit",
    "rename",
    "remove",
    "delete",
    "attach",
    "detach",
)
SPECIFIC_TARGET_MARKERS = (
    "paragraph",
    "sentence",
    "section",
    "field",
    "row",
    "title",
    "item",
)

# "Prepare the planned tests" is generation; "run the planned tests" is
# execution and belongs to the scope-wide rule below.
EXECUTION_VERBS = ("run ", "execute ")

# 5. Target-specific operations on one identified artifact.
TARGET_OPERATION_MARKERS = (
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

# 6. Scope-wide declared execution.
SCOPE_EXECUTION_RULES: tuple[tuple[tuple[str, ...], str, list[str]], ...] = (
    (
        (
            "run the rcm tests",
            "execute the rcm tests",
            "run rcm tests",
            "execute planned tests",
            "run the planned tests",
        ),
        audit_workflow.WORKFLOW_ID,
        ["fieldwork.executed", "results.rolled_up"],
    ),
    (
        (
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
        ),
        analysis_workflow.WORKFLOW_ID,
        list(analysis_workflow.FULL_ANALYSIS_OUTCOMES),
    ),
)

# 7. The weak fallback: recognizable isolated-operation vocabulary that has not
# matched anything stronger. Everything here is an artifact operation the action
# catalog can plan; a miss here falls through to the bounded router worker.
ISOLATED_OPERATION_MARKERS = (
    "join ",
    "add a finding",
    "create a finding",
    "validate ",
    "validation",
    "check report quality",
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

# Strong separators for a compound request. A bare " and " is deliberately not
# one: "join the tables and analyse them" is a single scope-wide analysis
# request, not two runs.
COMPOUND_SEPARATORS = re.compile(
    r"(?:\s+and\s+then\s+|\s+then\s+|\s*;\s*|\s+also\s+|\.\s+|\n)"
)


# --------------------------------------------------------------------------- #
# Pure validation
# --------------------------------------------------------------------------- #
def supported_outcomes() -> set[str]:
    """Every capability ID declared by a registered workflow."""

    return {
        capability.id
        for registry in audit_capabilities.REGISTRY_BY_WORKFLOW.values()
        for capability in registry.all()
    }


def validate_requested_outcomes(outcomes: list[str]) -> str:
    """Return the one registered workflow that owns every requested outcome."""

    requested = [str(item) for item in outcomes]
    if not requested:
        raise WorkspaceError("A workflow route needs at least one requested outcome.")
    unknown = sorted(set(requested) - supported_outcomes())
    if unknown:
        raise WorkspaceError(
            "Unknown workflow outcome(s): " + ", ".join(unknown) + "."
        )
    definition = audit_capabilities.workflow_for_outcomes(requested)
    if definition is None:
        raise WorkspaceError(
            "The requested outcomes do not belong to one registered workflow."
        )
    # Reject an outcome set the owning workflow cannot close over.
    audit_capabilities.REGISTRY_BY_WORKFLOW[definition].closure(requested)
    return definition


def validate_action_intent(intent: object) -> str:
    """Normalize an action intent against the registered action catalog.

    A router-proposed intent must name a registered action type. The
    deterministic classifier does not choose a type — it asserts only that the
    request is an isolated mutation — so the generic sentinel is accepted too.
    """

    value = str(intent or "").strip() or ISOLATED_ACTION_INTENT
    if value == ISOLATED_ACTION_INTENT:
        return value
    if value not in {definition.type for definition in action_catalog.REGISTRY.all()}:
        raise WorkspaceError(f"Unknown action intent '{value}'.")
    return value


def normalize_route(
    route: str,
    *,
    decided_by: str,
    workflow_definition: str | None = None,
    requested_outcomes: list[str] | None = None,
    objective: str = "",
    target_refs: list[str] | None = None,
    generation_mode: str = "reuse_existing",
    action_intent: object = None,
    constraints: list[str] | None = None,
    clarification: str | None = None,
) -> dict:
    """Validate and normalize one routing result into its persisted shape."""

    if route not in ROUTES:
        raise WorkspaceError(f"Unsupported command route '{route}'.")
    outcomes = [str(item) for item in requested_outcomes or []]
    definition = str(workflow_definition or "") or None
    intent = None
    if route == ROUTE_WORKFLOW:
        owner = validate_requested_outcomes(outcomes)
        if definition is not None and definition != owner:
            raise WorkspaceError(
                f"Requested outcomes belong to '{owner}', not '{definition}'."
            )
        definition = owner
    else:
        definition = None
        outcomes = []
    if route == ROUTE_ACTION:
        intent = validate_action_intent(action_intent)
    text = str(clarification or "").strip() or None
    if route == ROUTE_CLARIFICATION and not text:
        raise WorkspaceError("A clarification route needs a clarification question.")
    return {
        "status": "resolved",
        "route": route,
        "engine": ENGINE_BY_ROUTE[route],
        "decided_by": str(decided_by),
        "workflow_definition": definition,
        "requested_outcomes": outcomes,
        "objective": str(objective or "").strip(),
        "target_refs": [str(item) for item in target_refs or []],
        "generation_mode": workflow.normalize_generation_mode(generation_mode),
        "action_intent": intent,
        "constraints": [str(item) for item in constraints or []],
        "clarification": text,
    }


def pending_route() -> dict:
    """The persisted shape of a run whose route the bounded router still owns."""

    return {
        "status": "pending",
        "route": None,
        "engine": None,
        "decided_by": None,
        "workflow_definition": None,
        "requested_outcomes": [],
        "objective": "",
        "target_refs": [],
        "generation_mode": "reuse_existing",
        "action_intent": None,
        "constraints": [],
        "clarification": None,
    }


# --------------------------------------------------------------------------- #
# Deterministic classification (pure)
# --------------------------------------------------------------------------- #
def _target_refs(command: dict) -> list[str]:
    return [str(item) for item in command.get("target_refs") or ["workspace:current"]]


def _specific_artifact_operation(text: str) -> bool:
    return any(verb in text for verb in SPECIFIC_EDIT_VERBS) and any(
        marker in text for marker in SPECIFIC_TARGET_MARKERS
    )


def _workflow_route(
    command: dict,
    definition: str,
    outcomes: list[str],
    decided_by: str,
    *,
    default_objective: str = "",
) -> dict:
    return normalize_route(
        ROUTE_WORKFLOW,
        decided_by=decided_by,
        workflow_definition=definition,
        requested_outcomes=list(outcomes),
        objective=str(command.get("text") or default_objective),
        target_refs=_target_refs(command),
        generation_mode=workflow.command_generation_mode(command),
        constraints=list(command.get("constraints") or []),
    )


def _action_route(command: dict, decided_by: str) -> dict:
    return normalize_route(
        ROUTE_ACTION,
        decided_by=decided_by,
        objective=str(command.get("text") or ""),
        action_intent=ISOLATED_ACTION_INTENT,
    )


def _single_intent(text: str, command: dict) -> dict | None:
    """Classify one request segment through precedence steps 3-7."""

    if any(phrase in text for phrase in LIFECYCLE_PHRASES):
        return _workflow_route(
            command,
            audit_workflow.WORKFLOW_ID,
            list(audit_capabilities.FULL_AUDIT_OUTCOMES),
            "lifecycle_completion",
        )
    specific = _specific_artifact_operation(text)
    if not specific:
        for phrases, definition, outcomes in GENERATION_RULES:
            if not any(phrase in text for phrase in phrases):
                continue
            # "Run the planned tests" is execution, not generation.
            if outcomes == ["planning.planned_tests_ready"] and any(
                verb in text for verb in EXECUTION_VERBS
            ):
                continue
            return _workflow_route(
                command, definition, outcomes, "workflow_generation"
            )
    # A named part of an artifact — "replace this APM paragraph" — is a manual
    # edit, which is why the same test also vetoes the generation rules above.
    if specific or any(marker in text for marker in TARGET_OPERATION_MARKERS):
        return _action_route(command, "target_operation")
    for phrases, definition, outcomes in SCOPE_EXECUTION_RULES:
        if any(phrase in text for phrase in phrases):
            return _workflow_route(command, definition, outcomes, "scope_execution")
    if any(marker in text for marker in ISOLATED_OPERATION_MARKERS):
        return _action_route(command, "isolated_operation")
    return None


def _segments(text: str) -> list[str]:
    return [segment.strip() for segment in COMPOUND_SEPARATORS.split(text) if segment.strip()]


def classify_command(command: dict) -> dict | None:
    """Deterministically classify one command, or ``None`` for the router.

    Pure: reads the command dict only. It never loads a workspace, executes an
    action, or mutates state.
    """

    direct = command.get("requested_outcomes")
    if isinstance(direct, list) and direct:
        return _workflow_route(
            command,
            validate_requested_outcomes([str(item) for item in direct]),
            [str(item) for item in direct],
            "explicit_outcomes",
            default_objective="Continue the requested audit outcomes.",
        )
    template = str(command.get("goal_template") or "").strip()
    if template:
        outcomes = template_outcomes(template)
        if outcomes is None:
            raise WorkspaceError(f"Unknown goal template '{template}'.")
        return _workflow_route(
            command,
            validate_requested_outcomes(outcomes),
            outcomes,
            "goal_template",
            default_objective=template.replace("_", " "),
        )
    text = str(command.get("text") or "").casefold()
    segments = _segments(text)
    if len(segments) > 1:
        routed = [
            result
            for result in (_single_intent(segment, command) for segment in segments)
            if result is not None
        ]
        engines = {result["route"] for result in routed}
        if ROUTE_WORKFLOW in engines and ROUTE_ACTION in engines:
            return normalize_route(
                ROUTE_CLARIFICATION,
                decided_by="compound_request",
                objective=str(command.get("text") or ""),
                clarification=(
                    "This request combines declared workflow outcomes with an "
                    "isolated artifact operation. Send them as two requests so "
                    "each runs as its own durable run."
                ),
            )
    return _single_intent(text, command)


def workflow_owned_request(command: dict) -> bool:
    """True when a command must not be planned as an isolated action graph.

    The action scheduler calls this as its defensive boundary. It is the same
    deterministic classification the router uses, so the guard and the route can
    never disagree.
    """

    if isinstance(command.get("requested_outcomes"), list) and command["requested_outcomes"]:
        return True
    template = str(command.get("goal_template") or "").strip()
    if template and template_outcomes(template) is not None:
        return True
    try:
        route = classify_command({**command, "goal_template": None})
    except WorkspaceError:
        return False
    return bool(route and route["route"] == ROUTE_WORKFLOW)


# --------------------------------------------------------------------------- #
# Bounded router worker
#
# Single-turn classifier over the command and the current capability readiness
# projection. It proposes no actions, workers, or dependencies, and it only ever
# names registered outcome IDs or registered action types.
# --------------------------------------------------------------------------- #
ROUTER_SYSTEM = f"""[agent:workflow_router]
Classify one audit-assistant command. You are a router, not a planner. Return
route (workflow|action|clarification|unsupported), requested_outcomes (only
supported outcome IDs, required and non-empty when route is workflow),
objective, target_refs, generation_mode (reuse_existing|force), action_intent
(a registered action type, or null), constraints, and clarification (required
when route is clarification). Never propose actions, workers, dependencies,
tests, columns, or execution steps. {prompts.JSON_RULES}"""


def validate_router_result(payload: dict, supported: set[str]) -> dict:
    """Validate one bounded router-worker result into a normalized route."""

    route = str(payload.get("route") or "")
    if route not in ROUTES:
        raise ValueError("route is unsupported")
    outcomes = payload.get("requested_outcomes") or []
    if not isinstance(outcomes, list) or any(
        str(item) not in supported for item in outcomes
    ):
        raise ValueError("requested_outcomes contains an unsupported capability")
    generation_mode = str(payload.get("generation_mode") or "reuse_existing")
    if generation_mode not in store.GENERATION_MODES:
        raise ValueError("generation_mode is unsupported")
    if route == ROUTE_WORKFLOW and not outcomes:
        raise ValueError("a workflow route needs at least one requested outcome")
    if route == ROUTE_CLARIFICATION and not str(payload.get("clarification") or "").strip():
        raise ValueError("a clarification route needs a clarification question")
    try:
        return normalize_route(
            route,
            decided_by="router_worker",
            requested_outcomes=[str(item) for item in outcomes],
            objective=str(payload.get("objective") or ""),
            target_refs=[str(item) for item in payload.get("target_refs") or []],
            generation_mode=generation_mode,
            action_intent=payload.get("action_intent"),
            constraints=[str(item) for item in payload.get("constraints") or []],
            clarification=payload.get("clarification"),
        )
    except WorkspaceError as error:
        raise ValueError(str(error)) from error


def _resolve_command(runner, bundle: context_bundles.ContextBundle, supported: set[str]) -> dict:
    return runner.llm_json(
        ROUTER_SYSTEM,
        bundle.serialized(),
        activity={"context_metrics": bundle.metrics()},
        validator=lambda payload: validate_router_result(payload, supported),
    )


# --------------------------------------------------------------------------- #
# Route installation
# --------------------------------------------------------------------------- #
def _explanation(
    registry: workflow.CapabilityRegistry,
    resolved: list[str],
    stages: list[dict],
    reused: list[str],
    requested: list[str],
) -> str:
    """The plan, in the words the auditor uses.

    This string is read directly in the chat transcript, so it is built from
    capability *titles* rather than capability ids. The dependency closure that
    produced it is still fully recoverable from ``resolved_capabilities`` and
    ``reused_capabilities`` on the same record.
    """

    def title(capability_id: str) -> str:
        try:
            return registry.get(capability_id).title
        except WorkspaceError:
            return narration.humanize(capability_id)

    return narration.plan_sentence(
        [str(stage.get("title") or title(stage["capability"])) for stage in stages],
        [title(item) for item in reused],
        added_prerequisites=any(item not in requested for item in resolved),
    )


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
        or validate_requested_outcomes(list(resolution.get("requested_outcomes") or []))
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
    explanation = _explanation(registry, resolved, stages, reused, requested)
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
        "route": ROUTE_WORKFLOW,
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


def resolve_route(workspace: Workspace, run: dict) -> str | None:
    """Classify once and persist the route and engine before thread launch.

    Returns the selected engine, or ``None`` when the deterministic pass could
    not decide (the bounded router owns the run) or decided the request needs a
    clarification instead of an engine.
    """

    route = classify_command(run.get("command") or {})
    if route is None:
        run["route"] = pending_route()
        run["engine"] = None
        store.save_run(workspace, run)
        return None
    run["route"] = route
    run["engine"] = route["engine"]
    if route["route"] == ROUTE_WORKFLOW:
        install_resolution(workspace, run, route)
    store.save_run(workspace, run)
    return route["engine"]


class CommandRouter(BaseRunner):
    """Bounded pre-dispatch router using the shared runtime and gateway."""

    def resolve(self) -> dict:
        self.set_status("interpreting")
        # The router classifies against every registered workflow, so an
        # unresolved data-analysis request can name an analysis outcome instead
        # of falling through to the generic action interpreter.
        supported = supported_outcomes()
        state = self._state(self.ws)
        bundle = context_bundles.command_router(
            self.run.get("command") or {},
            state,
            sorted(supported),
            permission_mode=self.run["mode"],
        )
        resolution = _resolve_command(self, bundle, supported)
        if resolution["route"] == ROUTE_CLARIFICATION:
            answer = self._clarification(str(resolution["clarification"]))
            command = dict(self.run.get("command") or {})
            command["text"] = (
                f"{command.get('text') or ''}\n\nClarification: {answer}".strip()
            )
            fresh = load_workspace(self.ws.id)
            bundle = context_bundles.command_router(
                command,
                self._state(fresh),
                sorted(supported),
                permission_mode=self.run["mode"],
            )
            resolution = _resolve_command(self, bundle, supported)
            if resolution["route"] == ROUTE_CLARIFICATION:
                raise WorkspaceError(
                    "The command still needs clarification after the supplied answer."
                )
        return resolution

    @staticmethod
    def _state(workspace: Workspace) -> dict:
        return {
            **audit_capabilities.workflow_state(workspace),
            **audit_capabilities.analysis_workflow_state(workspace),
            **audit_capabilities.documents_workflow_state(workspace),
            **audit_capabilities.doc_tests_workflow_state(workspace),
        }

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

    def finish_without_engine(self, route: dict) -> None:
        """Complete a run whose route selects no engine."""

        if not self.run.get("started"):
            self.mark_started()
        text = route.get("clarification") or (
            "I can't do that one as an audit workflow. Tell me which part of the "
            "engagement you want me to work on and I'll take it from there."
        )
        self.run["summary_markdown"] = text
        self.run["command"]["status"] = "completed"
        self.mark_finished()
        self.set_status("completed_with_open_items")
        # A route that selects no engine produces no stages, so the answer to
        # the command *is* the reply — it belongs in the transcript, not only
        # in a card the auditor has to expand.
        narration.say(self.run, self.emit, text)
        self.save()


def resolve_pending_route(workspace: Workspace, run: dict, handle: object) -> str | None:
    """Spend one bounded router turn, persist the route, and return its engine.

    This is the only routing path that calls the provider, and it runs only for
    a command the deterministic pass could not classify. It does not repeat the
    deterministic pass.
    """

    router = CommandRouter(workspace, run, handle)
    if not run.get("started"):
        router.mark_started()
    route = router.resolve()
    run["route"] = route
    run["engine"] = route["engine"]
    if route["route"] in TERMINAL_ROUTES:
        router.finish_without_engine(route)
        return None
    if route["route"] == ROUTE_WORKFLOW:
        install_resolution(workspace, run, route)
        router.save()
        router.emit("workflow_resolved", {"workflow": run["workflow"]})
        router.emit("workflow_explanation", {"text": run["workflow_explanation"]})
        return store.WORKFLOW_ENGINE
    run["schema_version"] = 2
    router.save()
    return store.ACTION_ENGINE


def dispatch_engine(workspace: Workspace, run: dict, handle: object) -> str | None:
    """Return the engine a loaded run must be dispatched to, or ``None``.

    ``None`` means the run needs no scheduler: it was finished here because its
    route selects no engine. The engine is never inferred from ``kind``,
    ``schema_version``, or the presence of a workflow record, and a record whose
    engine is absent or outside the supported set fails closed.
    """

    route = run.get("route")
    if isinstance(route, dict):
        if route.get("status") == "pending":
            return resolve_pending_route(workspace, run, handle)
        if route.get("route") in TERMINAL_ROUTES:
            CommandRouter(workspace, run, handle).finish_without_engine(route)
            return None
    engine = run.get("engine")
    if engine not in store.RUN_ENGINES:
        label = "missing" if engine is None else repr(engine)
        raise WorkspaceError(f"Agent run engine is {label} or unsupported.")
    return engine


__all__ = [
    "CommandRouter",
    "GOAL_TEMPLATES",
    "ROUTES",
    "TEMPLATE_RUN_CONTEXT_KEYS",
    "classify_command",
    "dispatch_engine",
    "install_resolution",
    "normalize_route",
    "pending_route",
    "resolve_pending_route",
    "resolve_route",
    "template_outcomes",
    "validate_action_intent",
    "validate_requested_outcomes",
    "validate_router_result",
    "workflow_owned_request",
]
