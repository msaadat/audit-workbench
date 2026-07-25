"""Authoritative exploratory data-analysis workflow definition.

This module is the single source of truth for the analysis lifecycle
*structure*: the workflow identity, its hash-identified metadata, the capability
dependency graph, and the goal-template outcome sets. Capability modules attach
readiness, unit expansion, context, workers, and executors to the capability IDs
declared here; they do not redefine the edges.

The workflow answers requests such as "see the two tables, perform relevant
joins and data analysis" as a durable outcome workflow rather than as an ad-hoc
action DAG:

``data.relationships_inferred`` diagnoses table relationships from deterministic
local Polars evidence, ``data.joins_ready`` materializes only the joins that
evidence supports, ``analysis.definitions_ready`` proposes rerunnable analysis
specs from declared metadata context, and ``analysis.executed`` runs them
locally and records a bounded result contract.

The generic ``WorkflowRunner`` never imports this module — it receives a
capability registry by composition. Only the analysis-domain composition in the
grouped ``capabilities`` package reads the graph.
"""

from __future__ import annotations

from ..workflow import canonical_sha1

# Authoritative workflow identity persisted on every analysis run.
WORKFLOW_ID = "analysis_workflow_v1"

# The analysis dependency graph. Each capability ID maps to its direct
# dependencies in declaration order. Unlike the audit graph this one is a linear
# chain: every later outcome consumes the artifacts the previous one made
# durable, and there is no parallel branch to express.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "data.relationships_inferred": (),
    "data.joins_ready": ("data.relationships_inferred",),
    "analysis.definitions_ready": ("data.joins_ready",),
    "analysis.executed": ("analysis.definitions_ready",),
}

# Complete-analysis outcome set requested by "analyze these tables" style goals.
# The transitive closure of this outcome is the whole graph above.
FULL_ANALYSIS_OUTCOMES = ["analysis.executed"]

# Goal-template routing to requested outcome sets. ``data_analysis`` was
# previously an isolated ActionRunner intent; Phase 8 makes it a declared
# workflow goal. Isolated "run this saved analysis" and "pin this result"
# operations stay with ``ActionRunner`` and are intentionally absent here.
TEMPLATE_OUTCOMES: dict[str, list[str]] = {
    "data_analysis": FULL_ANALYSIS_OUTCOMES,
    "table_relationships": ["data.joins_ready"],
}


def dependencies(capability_id: str) -> tuple[str, ...]:
    """Return the authoritative direct dependencies for a capability ID."""

    return DEPENDENCIES[capability_id]


def outcomes_for_template(template: str) -> list[str] | None:
    values = TEMPLATE_OUTCOMES.get(str(template or ""))
    return list(values) if values is not None else None


def definition_hash() -> str:
    """Hash-identify the authoritative analysis workflow definition.

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
