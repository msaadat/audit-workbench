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
# A full audit also schedules the exploratory data-analysis branch, through to
# the memo that branch produces. The full outcome set requests it before the
# planning outcomes, so the sequential workflow scheduler completes it before
# APM preparation and the memo exists by the time planning reads it. It is
# still *not* a dependency of either planning capability: an APM request on its
# own remains independent of data analysis, and planning consumes whatever
# analysis material exists rather than forcing it into being.
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
    "data.join_utility_ready": analysis_workflow.dependencies(
        "data.join_utility_ready"
    ),
    "data.joins_ready": analysis_workflow.dependencies("data.joins_ready"),
    "analysis.definitions_ready": analysis_workflow.dependencies(
        "analysis.definitions_ready"
    ),
    "analysis.executed": analysis_workflow.dependencies("analysis.executed"),
    "analysis.summarized": analysis_workflow.dependencies("analysis.summarized"),
    "planning.context_ready": ("documents.analysis_generated",),
    "planning.apm_ready": ("planning.context_ready",),
    "planning.rcm_ready": ("planning.apm_ready",),
    "tests.specified": ("planning.rcm_ready",),
    # Placing an exploratory procedure needs the matrix to place it in. It sits
    # after test generation so a promoted test is written against a matrix whose
    # own tests already exist, and before fieldwork so a promoted test is
    # executed with everything else — a procedure carried into a test and then
    # never run has not been carried anywhere.
    #
    # ``analysis.executed`` is deliberately *not* an edge here, for the reason
    # the memo is not an edge into planning: it would make exploratory analysis
    # a prerequisite of every request that reaches fieldwork, so asking for one
    # finding draft would schedule the whole branch. Ordering is not at risk
    # from leaving it out — the analysis capabilities are declared first in the
    # registry, so any closure containing them places them before
    # ``tests.specified``, and a closure that omits them has no procedure to
    # place. Readiness is satisfied when nothing is pending, which is exactly
    # the state of a workspace that ran no exploratory analysis.
    "tests.promoted_from_analysis": ("tests.specified",),
    "fieldwork.executed": ("tests.specified", "tests.promoted_from_analysis"),
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
    "analysis.summarized",
    "findings.drafted",
    "working_papers.generated",
    "dashboard.curated",
    "report.working_draft",
    "audit.verified",
]

# Goal-template routing to requested outcome sets. Running one named Document
# Test is not an RCM workflow goal, so it is intentionally absent here: it is a
# request against the standalone ``doc_tests_workflow_v2`` graph, which reaches
# the same units through the same binder.
TEMPLATE_OUTCOMES: dict[str, list[str]] = {
    "full_audit_working_draft": FULL_AUDIT_OUTCOMES,
    "planning": [
        "planning.apm_ready",
        "planning.rcm_ready",
        "tests.specified",
    ],
    "apm_only": ["planning.apm_ready"],
    "rcm_only": ["planning.rcm_ready"],
    "finding_draft": ["findings.drafted"],
    # Preparing an RCM row's Document Tests is the ``tests.specified``
    # deliverable, not a document-test run.
    "document_test_preparation": ["tests.specified"],
    "report": ["report.working_draft", "audit.verified"],
}


def dependencies(capability_id: str) -> tuple[str, ...]:
    """Return the authoritative direct dependencies for a capability ID."""

    return DEPENDENCIES[capability_id]


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
