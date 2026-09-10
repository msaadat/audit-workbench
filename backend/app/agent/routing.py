"""Command routing: one classification, one persisted route, one engine.

This module is the only place a request is turned into an execution decision,
and since step 7 of ``docs/agent-loop-redesign.md`` it makes that decision
without a model turn and without guessing from wording.

* **Deterministic classification** (:func:`classify_command` and the pure
  helpers above it) reads only the durable command dict. It never loads a
  workspace, executes an action, gathers domain context, or mutates anything.
  It always returns a normalized route.
* **Route installation** (:func:`resolve_route`, :func:`install_resolution`)
  persists exactly one normalized route and the selected engine on the durable
  run, synchronously in ``runner.start_command_run``, before the worker thread
  launches.

Routing precedence:

1. ``source == "loop"`` — the coordinator handed this to the steering loop.
2. Explicit registered outcomes.
3. A registered goal template.
4. A lifecycle-wide completion request.
5. Anything else — a sentence — routes to the steering loop, which reads the
   workspace before deciding and can ask.

Everything between 4 and 5 used to be phrase tables: generation and refresh
rules, target-operation markers, scope-wide execution rules, isolated-operation
markers, a compound-request splitter, and a bounded router worker for whatever
was left. Across 45 recorded runs none of them decided anything a person had
typed. They are gone, and the decision they were making badly is now made by
something that can look at the engagement first.

Neither scheduler classifies, and neither scheduler calls the other.
"""

from __future__ import annotations

import os

from .. import doc_tests
from ..workspaces import Workspace, WorkspaceError
from . import capabilities as audit_capabilities
from . import narration, store, workflow
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

# The five normalized routing results. ``workflow``, ``action`` and ``agent``
# select an engine; ``clarification`` and ``unsupported`` finish the run without
# one.
ROUTE_WORKFLOW = "workflow"
# The steering loop. Unlike the other two engine routes this one is never
# inferred from what a request says: the coordinator asks for it explicitly by
# handing the request over as a ``loop`` command, and the classification below
# only reads that back.
ROUTE_AGENT = "agent"
ROUTE_CLARIFICATION = "clarification"
ROUTE_UNSUPPORTED = "unsupported"
ROUTES = (
    ROUTE_WORKFLOW,
    ROUTE_AGENT,
    ROUTE_CLARIFICATION,
    ROUTE_UNSUPPORTED,
)
TERMINAL_ROUTES = frozenset({ROUTE_CLARIFICATION, ROUTE_UNSUPPORTED})
ENGINE_BY_ROUTE = {
    ROUTE_WORKFLOW: store.WORKFLOW_ENGINE,
    ROUTE_AGENT: store.AGENT_ENGINE,
    ROUTE_CLARIFICATION: None,
    ROUTE_UNSUPPORTED: None,
}

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
            "Execute RCM-linked tests through an evidence-linked report "
            "working draft."
        ),
        "constraints": [
            "Do not assert a formal audit opinion.",
            "Preserve auditor edits.",
        ],
    },
    "planning": {
        "objective": (
            "Prepare or improve engagement planning and the RCM tests that "
            "cover it."
        ),
    },
    "apm_only": {"objective": "Prepare or revise only the audit planning memorandum."},
    "rcm_only": {"objective": "Prepare or revise only the risk and control matrix."},
    "finding_draft": {
        "objective": "Draft evidence-linked findings for the selected observation or risk.",
    },
    "finding_consolidation": {
        "objective": (
            "Propose which draft findings report one issue, for the auditor "
            "to accept or dismiss."
        ),
        "constraints": ["Do not rewrite any finding.", "Preserve auditor edits."],
    },
    "report": {
        "objective": (
            "Prepare evidence-linked audit report working content and run "
            "quality checks."
        ),
    },
    "data_analysis": {
        "objective": "Analyse available structured data and preserve useful validated work.",
    },
    "analysis_execution": {
        "objective": "Execute the saved analysis procedures and record their results.",
        "constraints": [
            "Do not propose new analysis definitions.",
            "Preserve auditor edits.",
        ],
    },
    "table_relationships": {
        "objective": "Infer table relationships and materialize supported joins.",
    },
    "document_analysis": {"objective": "Analyse the documents in scope."},
    "document_test_preparation": {
        # See the command's note in agent/commands.py: the specification is
        # written at draft time, so this objective is the drafting itself.
        "objective": "Draft the executable tests the RCM rows still need.",
    },
    "document_test_execution": {"objective": "Execute the Document Tests in scope."},
}

# Run-context keys a template may carry from a caller. Anything else is
# rejected: run context is scope, never a routing override.
TEMPLATE_RUN_CONTEXT_KEYS: dict[str, frozenset[str]] = {
    "planning": frozenset({"document_ids"}),
    # ``rcm_ids`` is the batch form of ``rcm_id``: a test tab knows every row
    # whose exceptions are still undrafted, and naming them is what keeps the
    # button from widening into an unscoped workspace sweep.
    "finding_draft": frozenset({"observation_id", "rcm_id", "rcm_ids"}),
    "document_analysis": frozenset({"document_ids", "action"}),
    "document_test_preparation": frozenset(),
    "document_test_execution": frozenset({"test_id", "test_ids"}),
    # The Analysis tab knows which frames the auditor is looking at. Passing
    # them as scope is what keeps an unscoped run from sweeping the workspace
    # and then asking the auditor to settle a scope it could have been told.
    "data_analysis": frozenset({"tables"}),
    "table_relationships": frozenset({"tables"}),
    "analysis_execution": frozenset({"analysis_ids", "tables"}),
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
# The one deterministic phrase table
# --------------------------------------------------------------------------- #
# "Do the whole audit" names the entire registered lifecycle and nothing else
# could mean anything different, so it is worth answering without a model turn.
# Every other phrase table this module used to carry — generation, scope-wide
# execution, target operations, isolated operations, compound separators —
# decided nothing across 45 recorded runs and is gone; a request those tables
# would have argued over now goes to the steering loop, which can read the
# workspace before deciding rather than guessing from the words alone.
LIFECYCLE_PHRASES = (
    "full audit",
    "complete the audit",
    "complete audit",
    "entire audit",
    "end-to-end audit",
    "end to end audit",
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
    if action_intent:
        raise WorkspaceError(
            "No route carries an action intent: registered actions are tools "
            "the steering loop calls, not an engine."
        )
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


# --------------------------------------------------------------------------- #
# Deterministic classification (pure)
# --------------------------------------------------------------------------- #
def _target_refs(command: dict) -> list[str]:
    return [str(item) for item in command.get("target_refs") or ["workspace:current"]]


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


def classify_command(command: dict) -> dict:
    """Classify one command into exactly one route. Pure.

    Four cases, in this order, and no model turn in any of them:

    1. ``source == "loop"`` — the coordinator has already decided this needs
       the steering loop, and everything below reads the request's *words*.
    2. Explicit ``requested_outcomes`` — a tab button or a suggestion naming
       what it wants.
    3. A registered goal template — a slash command or a chat shortcut.
    4. A lifecycle phrase — "do the full audit" can mean nothing else.

    Anything else is a sentence, and a sentence is the loop's. That is the
    whole of the change step 7 makes: the phrase tables that used to guess an
    outcome set from wording, and the bounded router turn that guessed when
    they could not, decided nothing across the recorded history. The loop
    decides the same question with the workspace in front of it, and can ask.

    It never loads a workspace, executes an action, or mutates state.
    """

    if str(command.get("source") or "") == store.LOOP_COMMAND_SOURCE:
        return _agent_route(command, "loop_source")
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
    if any(phrase in text for phrase in LIFECYCLE_PHRASES):
        return _workflow_route(
            command,
            audit_workflow.WORKFLOW_ID,
            list(audit_capabilities.FULL_AUDIT_OUTCOMES),
            "lifecycle_completion",
        )
    return _agent_route(command, "text_request")


def _agent_route(command: dict, decided_by: str) -> dict:
    return normalize_route(
        ROUTE_AGENT,
        decided_by=decided_by,
        objective=str(command.get("text") or ""),
        target_refs=_target_refs(command),
        generation_mode=workflow.command_generation_mode(command),
        constraints=list(command.get("constraints") or []),
    )


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

    Reuse is named only where the auditor asked for something already done.
    Every request drags in a closure, and most of that closure is settled, so
    listing all of it opened every run by naming twelve capabilities it would
    *not* run before the one it would — an answer to a question nobody asked.
    Asking to regenerate something already in place is a different case: there
    the reuse *is* the answer, and saying so is the difference between a run
    that decided to reuse and one that quietly did nothing.
    """

    def title(capability_id: str) -> str:
        try:
            return registry.get(capability_id).title
        except WorkspaceError:
            return narration.humanize(capability_id)

    named_reuse = [item for item in reused if item in set(requested)]
    # Stale work is named wherever it appears, requested or not: an artifact
    # being rewritten because its parent moved is the one thing in the plan the
    # auditor did not ask for and would otherwise not expect.
    stale = [
        stage for stage in stages if stage.get("scheduled_because") == "stale"
    ]
    return narration.plan_sentence(
        [str(stage.get("title") or title(stage["capability"])) for stage in stages],
        [title(item) for item in named_reuse],
        added_prerequisites=any(item not in requested for item in resolved),
        stale_titles=[
            str(stage.get("title") or title(stage["capability"])) for stage in stale
        ],
        stale_parents=[
            str(ref)
            for stage in stale
            for ref in stage.get("scheduled_because_refs") or []
        ],
    )


#: Room in the execution stage for the units that are not one record's
#: assessment — a blocked worklist, a deterministic comparison, a review.
_DOC_TEST_UNIT_HEADROOM = 50


def _document_test_unit_ceiling(workspace: Workspace, scope: dict) -> int:
    """The per-stage unit cap a Q&A population needs, or zero for the default.

    The 250-unit cap is a guard against an unbounded expansion, and a resolved
    population is bounded — by ``document_population.MAX_POPULATION_RECORDS``,
    and by the auditor's own selection. Refusing to schedule it would refuse the
    feature rather than bound it, so the cap is raised to what this engagement's
    populations actually resolve to and the run pays for exactly that.
    """

    from .capabilities.doc_tests import assessment_pairs, scoped_tests

    try:
        pairs = assessment_pairs(scoped_tests(workspace, scope))
    except WorkspaceError:
        return 0
    if not pairs:
        return 0
    return pairs + _DOC_TEST_UNIT_HEADROOM


def _audit_model_turns(workspace: Workspace) -> int:
    """Size the audit model budget from real RCM, test, and Q&A counts."""

    from .. import doc_tests as doc_test_service
    from .capabilities.doc_tests import assessment_pairs

    test_count = len(workspace.data_tests) + len(doc_test_service.list_tests(workspace))
    qa_pairs = assessment_pairs(
        doc_tests.load_test(workspace, summary["id"])
        for summary in doc_tests.list_tests(workspace)
    )
    eligible_findings = sum(
        item.get("outcome") == "exception"
        for item in workspace.observations
    )
    # One turn for the consolidation pass, which only runs over two or more
    # drafts. Counted from the observations that will become drafts, since
    # the budget is sized before the drafts exist.
    consolidation_turn = 1 if eligible_findings >= 2 or len(workspace.findings) >= 2 else 0
    return (
        20
        + 4 * len(workspace.rcm)
        + 4 * test_count
        + 2 * qa_pairs
        + 2 * eligible_findings
        + consolidation_turn
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


def default_llm_concurrency() -> int:
    """Model-call fan-out width for stages declared parallel.

    Document chunk units are independent, commit nothing, and settle
    all-settled, so this width is what decides whether a parallel stage is
    actually parallel. Pinned at 1 the barrier bought failure isolation and no
    throughput: eight one-page documents ran strictly one after another, and a
    run's wall time was the sum of its model calls. Capped because the ceiling
    here is the provider's rate limit, not local capacity.
    """

    try:
        return max(1, min(int(os.environ.get("AGENT_LLM_CONCURRENCY") or 4), 8))
    except ValueError:
        return 4


def document_visual_page_limit() -> int:
    """Durable default bound for image-bearing document map units."""

    try:
        return max(
            1,
            int(os.environ.get("DOCUMENT_VISUAL_PAGE_LIMIT") or 20),
        )
    except ValueError:
        return 20


def _document_model_turns(workspace: Workspace, scope: dict) -> int:
    """Size the document budget from the chunks actually in scope.

    Document analysis is one turn per bounded source chunk plus one reduction per
    document, so the budget follows the resolved scope and the real chunk count.
    A document with no cached extraction yet contributes a conservative estimate;
    the composition refreshes the budget once extraction has run.

    Plus what the run spends before it analyzes anything. Classification and
    schema induction are model-backed stages of the same workflow, and leaving
    them out of the arithmetic meant a run could exhaust its whole allowance on
    preparation and fail the limit before the analysis it was started for.
    """

    from .capabilities.documents import (
        analysis_unit_specs,
        preparation_model_turns,
        resolve_document_scope,
    )

    document_scope = resolve_document_scope(workspace, scope)
    chunks = sum(
        len(analysis_unit_specs(workspace, document_id, scope)) or 1
        for document_id in document_scope.document_ids
    )
    return (
        4
        + chunks
        + 2 * max(1, len(document_scope.document_ids))
        + preparation_model_turns(workspace, scope)
    )


def _doc_test_model_turns(workspace: Workspace, scope: dict) -> int:
    """Size the document-test budget from the assessments actually in scope.

    Only the Q&A unit kind calls the model, once per unanswered assessment unit
    — an attached document, or one record of a resolved population;
    deterministic comparison and review units never do.
    """

    from .capabilities.doc_tests import assessment_pairs, scoped_tests

    return 4 + 2 * assessment_pairs(scoped_tests(workspace, scope))


def _analysis_model_turns(workspace: Workspace, scope: dict) -> int:
    """Size the analysis model budget from the frames actually in scope.

    Only ``analysis.definitions_ready`` calls the model, once per target frame,
    so the budget follows the resolved scope rather than the whole workspace.
    """

    from .capabilities.analysis import resolve_table_scope

    table_scope = resolve_table_scope(workspace, scope)
    return 10 + 2 * max(1, len(table_scope.targets))


def resolution_scope(
    workspace: Workspace, run: dict, resolution: dict
) -> tuple[str, dict]:
    """The workflow definition and the scope a resolution would materialize.

    Split out of :func:`install_resolution` so that a caller who wants to know
    what a request *would* schedule — the steering loop's ``plan_outcomes`` —
    reads the same scope the run would execute under. A preview built from its
    own idea of scope would be a second answer to a question that already has
    one, and would drift the first time a capability learned a new narrowing.
    """

    definition_id = str(
        resolution.get("workflow_definition")
        or validate_requested_outcomes(list(resolution.get("requested_outcomes") or []))
    )
    if definition_id not in WORKFLOW_MODULES:
        raise WorkspaceError(f"Unsupported workflow definition '{definition_id}'.")
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
    # What the auditor said they wanted changed, if they said anything. It
    # lives on the run's context, which is where it survives a resume, and is
    # copied here so a binder reads it from the same place it reads every other
    # narrowing rather than reaching back into the record.
    instruction = str((run.get("context") or {}).get("instruction") or "").strip()
    if instruction:
        scope["instruction"] = instruction
    if document_scope_route:
        # A resolved document scope is durable on the workflow record, so a
        # checkpoint answer or an explicitly selected document survives a resume.
        # Explicitly named documents normally arrive as ``document:<id>`` target
        # refs, which the scope resolver reads directly.
        # A standalone document-analysis command covers the full inventory;
        # document generation scheduled by the audit workflow remains limited to
        # planning-relevant material unless the auditor explicitly names a file.
        scope["document_scope_mode"] = (
            "planning"
            if definition_id == audit_workflow.WORKFLOW_ID
            else "all"
        )
        scope["document_ids"] = [
            str(value)
            for value in (
                resolution.get("document_ids")
                or (run.get("context") or {}).get("document_ids")
                or []
            )
        ]
        # Which of the two forced actions this is. ``generation_mode`` says
        # *whether* to redo work; this says whether the vocabulary may move
        # while it happens, which is the question a master makes separable.
        # Derived from the action rather than carried beside it. Two keys
        # saying the same thing is how a stale one survives: the Documents tab
        # sends one action, and what it is allowed to do to the vocabulary
        # follows from it.
        scope["vocabulary_mode"] = (
            "rebuild"
            if str((run.get("context") or {}).get("action") or "")
            == "revise_vocabulary"
            else "frozen"
        )
        scope["page_limit"] = document_page_limit()
        scope["visual_page_limit"] = document_visual_page_limit()
        scope["full_visual_document_ids"] = [
            str(value)
            for value in (
                resolution.get("full_visual_document_ids")
                or (run.get("context") or {}).get(
                    "full_visual_document_ids"
                )
                or []
            )
        ]
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
    return definition_id, scope


def install_resolution(workspace: Workspace, run: dict, resolution: dict) -> None:
    """Materialize a validated workflow route on the durable run."""

    definition_id, scope = resolution_scope(workspace, run, resolution)
    definition = WORKFLOW_MODULES[definition_id]
    registry = audit_capabilities.REGISTRY_BY_WORKFLOW[definition_id]
    analysis_route = definition_id == analysis_workflow.WORKFLOW_ID
    document_route = definition_id == documents_workflow.WORKFLOW_ID
    doc_test_route = definition_id == doc_tests_workflow.WORKFLOW_ID
    document_scope_route = document_route or definition_id == audit_workflow.WORKFLOW_ID
    generation_mode = scope["generation_mode"]
    requested = list(resolution.get("requested_outcomes") or [])
    plan = workflow.materialize(
        registry,
        workspace,
        requested,
        scope,
        generation_mode=generation_mode,
    )
    resolved, stages, reused = plan.resolved, plan.stages, plan.reused
    maximum_units = max(
        int(run.get("limits", {}).get("max_units_per_stage") or 250),
        _document_test_unit_ceiling(workspace, scope),
    )
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
        # A full audit pays for its scoped document analyses and the independent
        # data-analysis branch it schedules before APM preparation.
        calculated_model_turns = _audit_model_turns(workspace) + _document_model_turns(
            workspace, scope
        ) + _analysis_model_turns(workspace, scope)
    run.setdefault("limits", {}).update(
        max_llm_concurrency=int(
            run.get("limits", {}).get("max_llm_concurrency")
            or default_llm_concurrency()
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
        "reused_capability_details": plan.reused_details,
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
                # Resolved, not raw. ``scope["vocabulary_mode"]`` only says which
                # of the two *forced* actions this would be; an unforced run
                # never consults it and accumulates. Recording the raw key made
                # every ordinary run claim in its own audit trail to have been
                # frozen — the stale second key the comment above warns about.
                "vocabulary_mode": audit_capabilities.documents.vocabulary_mode(
                    scope
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
            "cycle_updated": 0,
            "cycle_proposed": 0,
            "rcm_created": 0,
            "rcm_updated": 0,
            "rcm_preserved": 0,
            "test_created": 0,
            "test_updated": 0,
            "test_preserved": 0,
        },
    )


def resolve_route(workspace: Workspace, run: dict) -> str:
    """Classify once and persist the route and engine before thread launch.

    Always returns an engine. Since step 7 there is no pending route and no
    router turn: every command is one of the four cases
    :func:`classify_command` decides, and a sentence is the loop's.
    """

    route = classify_command(run.get("command") or {})
    run["route"] = route
    run["engine"] = route["engine"]
    if route["route"] == ROUTE_WORKFLOW:
        install_resolution(workspace, run, route)
    store.save_run(workspace, run)
    return route["engine"]


class _TerminalRouteRun(BaseRunner):
    """Just enough runner to finish a record whose route selects no engine.

    Nothing produces a ``clarification`` or ``unsupported`` route any more —
    the compound-request rule and the router worker that raised them are gone.
    A record persisted before that still carries one, and must reach a terminal
    status with a reply rather than failing closed on an engine it never had.
    """

    def execute(self) -> None:  # pragma: no cover - not scheduled
        raise NotImplementedError

    def finish(self, route: dict) -> None:
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


def finish_without_engine(workspace: Workspace, run: dict, handle: object) -> None:
    """Complete a persisted run whose route selects no engine."""

    _TerminalRouteRun(workspace, run, handle).finish(run.get("route") or {})


def dispatch_engine(workspace: Workspace, run: dict, handle: object) -> str | None:
    """Return the engine a loaded run must be dispatched to, or ``None``.

    ``None`` means the run needs no scheduler: it was finished here because its
    route selects no engine. The engine is never inferred from ``kind``,
    ``schema_version``, or the presence of a workflow record, and a record whose
    engine is absent or outside the supported set fails closed.
    """

    route = run.get("route")
    if isinstance(route, dict) and route.get("route") in TERMINAL_ROUTES:
        finish_without_engine(workspace, run, handle)
        return None
    engine = run.get("engine")
    if engine not in store.RUN_ENGINES:
        label = "missing" if engine is None else repr(engine)
        raise WorkspaceError(f"Agent run engine is {label} or unsupported.")
    return engine


__all__ = [
    "GOAL_TEMPLATES",
    "ROUTE_AGENT",
    "ROUTES",
    "TEMPLATE_RUN_CONTEXT_KEYS",
    "classify_command",
    "dispatch_engine",
    "install_resolution",
    "finish_without_engine",
    "normalize_route",
    "resolution_scope",
    "resolve_route",
    "template_outcomes",
    "validate_requested_outcomes",
]
