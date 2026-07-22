"""Authoritative audit workflow definition.

This module is the single source of truth for the audit lifecycle *structure*:
the workflow identity, its hash-identified metadata, the baseline capability
dependency graph, and the goal-template outcome sets. Capability modules attach
readiness, unit expansion, context, workers, and executors to the capability IDs
declared here; they do not redefine the edges.

The generic ``WorkflowRunner`` never imports this module — it receives a
capability registry by composition. Only audit-domain composition
(``audit_capabilities`` today, the grouped capability modules after Phase 7)
reads the graph.
"""

from __future__ import annotations

from ..workflow import canonical_sha1

# Authoritative workflow identity persisted on every audit run.
WORKFLOW_ID = "audit_workflow_v2"

# The baseline audit dependency graph. Each capability ID maps to its direct
# dependencies in declaration order. Working papers, dashboard curation, and
# finding/report work all branch after ``results.rolled_up``, so the audit
# workflow is a DAG rather than a linear chain; those parallel branches are
# intentional.
#
# Phase 9 may change the workflow definition hash by adding scoped
# ``documents.analysis_generated`` edges where a specific capability declares a
# dependency on document analysis. It must not make document analysis a universal
# audit prerequisite: only capabilities that genuinely require an analyzed
# document add the edge, and the global graph shape above stays intact for audits
# that carry no documents.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "planning.context_ready": (),
    "planning.apm_ready": ("planning.context_ready",),
    "planning.rcm_ready": ("planning.apm_ready",),
    "planning.planned_tests_ready": ("planning.rcm_ready",),
    "fieldwork.definitions_ready": ("planning.planned_tests_ready",),
    "fieldwork.executed": ("fieldwork.definitions_ready",),
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
    "findings.drafted",
    "working_papers.generated",
    "dashboard.curated",
    "report.working_draft",
    "audit.verified",
]

# Goal-template routing to requested outcome sets. The legacy data-analysis and
# single-document-test buttons are isolated operations, not RCM workflow goals,
# so they are intentionally absent here and handled by ActionRunner/DocTestRunner
# during the compatibility window.
TEMPLATE_OUTCOMES: dict[str, list[str]] = {
    "full_audit_working_draft": FULL_AUDIT_OUTCOMES,
    "planning": [
        "planning.apm_ready",
        "planning.rcm_ready",
        "planning.planned_tests_ready",
    ],
    "apm_only": ["planning.apm_ready"],
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
