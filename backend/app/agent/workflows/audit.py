"""Authoritative audit workflow definition.

This module is the single source of truth for the audit lifecycle *structure*:
the workflow identity, its hash-identified metadata, the baseline capability
dependency graph, and the goal-template outcome sets. Capability modules attach
readiness, unit expansion, context, workers, and executors to the capability IDs
declared here; they do not redefine the edges.

The generic ``WorkflowRunner`` never imports this module — it receives a
capability registry by composition. Only the audit-domain composition in the
grouped ``capabilities`` package reads the graph.
"""

from __future__ import annotations

from ..workflow import canonical_sha1
from . import analysis as analysis_workflow
from . import documents as documents_workflow

# Authoritative workflow identity persisted on every audit run. Bumped to v3
# when the two-pass tests.drafted -> tests.specified flow merged into a
# single tests.specified capability (docs/test-capability-merge-plan.md).
WORKFLOW_ID = "audit_workflow_v3"

# The baseline audit dependency graph. Each capability ID maps to its direct
# dependencies in declaration order. Working papers, dashboard curation, and
# finding/report work all branch after ``results.rolled_up``, so the audit
# workflow is a DAG rather than a linear chain; those parallel branches are
# intentional.
#
# Phase 9 added the scoped document-analysis edge: ``planning.context_ready``
# depends on ``documents.analysis_generated`` so planning consumes generated
# analyses rather than falling back to raw document text. The three document
# capabilities are declared once, in :mod:`agent.workflows.documents` and
# :mod:`agent.capabilities.documents`; this graph reuses those declarations with
# the same edges rather than restating a second implementation. Document analysis
# is not a universal prerequisite: with no planning-relevant document in scope
# every document capability's readiness is satisfied and no unit expands, so an
# audit that carries no documents runs exactly as before.
#
# A full audit also schedules the exploratory data-analysis branch. The full
# outcome set requests that branch before the planning outcomes, so the
# sequential workflow scheduler completes it before APM preparation. It is *not*
# a dependency of either planning capability: an APM request on its own remains
# independent of data analysis.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "documents.text_ready": documents_workflow.dependencies("documents.text_ready"),
    "documents.analysis_chunks_ready": documents_workflow.dependencies(
        "documents.analysis_chunks_ready"
    ),
    "documents.analysis_generated": documents_workflow.dependencies(
        "documents.analysis_generated"
    ),
    "data.relationships_inferred": analysis_workflow.dependencies(
        "data.relationships_inferred"
    ),
    "data.joins_ready": analysis_workflow.dependencies("data.joins_ready"),
    "analysis.definitions_ready": analysis_workflow.dependencies(
        "analysis.definitions_ready"
    ),
    "analysis.executed": analysis_workflow.dependencies("analysis.executed"),
    "planning.context_ready": ("documents.analysis_generated",),
    "planning.apm_ready": ("planning.context_ready",),
    "planning.rcm_ready": ("planning.apm_ready",),
    "tests.specified": ("planning.rcm_ready",),
    "fieldwork.executed": ("tests.specified",),
    "results.rolled_up": ("fieldwork.executed",),
    "findings.drafted": ("results.rolled_up",),
    "working_papers.generated": ("results.rolled_up",),
    "dashboard.curated": ("results.rolled_up",),
    "report.working_draft": (
        "planning.apm_ready",
        "results.rolled_up",
        "findings.drafted",
    ),
    "audit.verified": (
        "working_papers.generated",
        "dashboard.curated",
        "report.working_draft",
    ),
}

# Complete-audit outcome set requested by "complete the audit" style goals. The
# transitive closure of these outcomes is the whole graph above.
FULL_AUDIT_OUTCOMES = [
    "analysis.executed",
    "findings.drafted",
    "working_papers.generated",
    "dashboard.curated",
    "report.working_draft",
    "audit.verified",
]

# Goal-template routing to requested outcome sets. Running one named Document
# Test is not an RCM workflow goal, so it is intentionally absent here: it is a
# request against the standalone ``doc_tests_workflow_v1`` graph, which reaches
# the same units through the same binder.
TEMPLATE_OUTCOMES: dict[str, list[str]] = {
    "full_audit_working_draft": FULL_AUDIT_OUTCOMES,
    "planning": [
        "planning.apm_ready",
        "planning.rcm_ready",
        "tests.specified",
    ],
    "apm_only": ["planning.apm_ready"],
    "finding_draft": ["findings.drafted"],
    # Preparing an RCM row's Document Tests is the ``tests.specified``
    # deliverable, not a document-test run.
    "document_test_preparation": ["tests.specified"],
    "report": ["report.working_draft", "audit.verified"],
}


def dependencies(capability_id: str) -> tuple[str, ...]:
    """Return the authoritative direct dependencies for a capability ID."""

    return DEPENDENCIES[capability_id]


def unblocked_by(capability_id: str) -> tuple[str, ...]:
    """Capabilities that name this one as a direct dependency.

    The reverse of :data:`DEPENDENCIES`, used to tell an auditor what a pending
    decision actually releases. Direct dependents only: the transitive closure
    of a mid-graph capability is most of the workflow, which says nothing useful
    about the decision in front of them.
    """

    return tuple(
        dependent
        for dependent, deps in DEPENDENCIES.items()
        if capability_id in deps
    )


def downstream_of(capability_id: str) -> tuple[str, ...]:
    """Every capability reachable from this one, in declaration order.

    Used for "this blocks N later steps" counts, where the whole tail is the
    honest number even though only the direct dependents are worth naming.
    """

    seen: set[str] = set()
    frontier = [capability_id]
    while frontier:
        for dependent in unblocked_by(frontier.pop()):
            if dependent in seen:
                continue
            seen.add(dependent)
            frontier.append(dependent)
    return tuple(item for item in DEPENDENCIES if item in seen)


def outcomes_for_template(template: str) -> list[str] | None:
    values = TEMPLATE_OUTCOMES.get(str(template or ""))
    return list(values) if values is not None else None


def definition_hash() -> str:
    """Hash-identify the authoritative audit workflow definition.

    The hash covers the workflow identity and the full normalized dependency
    graph, so any edge change, capability addition, or reordering of a
    capability's dependencies changes the workflow definition hash. Behavior
    attached to capability IDs (readiness, workers, executors) is hashed
    separately at the capability level and is intentionally not folded in here.
    """

    return canonical_sha1(
        {
            "workflow_id": WORKFLOW_ID,
            "dependencies": {
                capability_id: list(deps)
                for capability_id, deps in DEPENDENCIES.items()
            },
        }
    )
