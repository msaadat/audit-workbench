"""Authoritative exploratory data-analysis workflow definition.

This module is the single source of truth for the analysis lifecycle
*structure*: the workflow identity, its hash-identified metadata, the capability
dependency graph, and the goal-template outcome sets. Capability modules attach
readiness, unit expansion, context, workers, and executors to the capability IDs
declared here; they do not redefine the edges.

The workflow answers requests such as "see the two tables, perform relevant
joins and data analysis" as a durable outcome workflow rather than as an ad-hoc
action DAG:

``data.relationships_inferred`` diagnoses alternate routes from local Polars
evidence. ``analysis.register_ready`` reads the bounded map and settles the
assertion register; ``analysis.definitions_ready`` authors specs only for work
the register could not already express as a measured spec.
``analysis.inputs_ready`` validates and loads the accepted definitions' saved
alignment recipes, then ``analysis.executed`` records their local results and
``analysis.summarized`` writes the memo. Probes can evaluate candidate alignments
locally before selection; none become user-visible durable joins.

The separate ``data.join_utility_ready`` → ``data.joins_ready`` branch remains
available for explicit requests to create durable joins. Full EDA does not
depend on that branch. The audit workflow retains its original dependencies.

``analysis.register_ready`` is the only capability in this graph whose model
turn is optional to its own outcome. Its floor is the deterministic sweep, so a
run whose reading turn is skipped or fails still holds a complete, committable
register — which is what makes one turn over the whole engagement safe to
depend on rather than a single point of failure.

The generic ``WorkflowRunner`` never imports this module — it receives a
capability registry by composition. Only the analysis-domain composition in the
grouped ``capabilities`` package reads the graph.
"""

from __future__ import annotations

from ..workflow import canonical_sha1

# Authoritative workflow identity persisted on every analysis run.
WORKFLOW_ID = "analysis_workflow_v1"

# These tests remain available in the manual analytics library but are not
# proposed or re-executed by the autonomous analysis workflow. All three are
# `descriptive` in the registry's own classification: a stratification, a period
# trend and a drawn sample have no exception concept at all, so an autonomous run
# proposing one spends a definition turn and an execution to produce a chart that
# nothing downstream can promote, cite as an exception, or conclude from. Drawing
# a sample is fieldwork besides, and `doc_tests` calls that one directly.
#
# The digit-pattern tests used to sit here too. They were deleted from the
# registry outright rather than merely withheld: measured across an engagement,
# Benford and the last-two-digit test refused on half the numeric columns for
# want of 100 values and returned no rows on the rest, while the round-number
# test compared business amounts against a null model meant for random ones and
# flagged 35–40% of every amount column as a warning.
#
# Kept as a literal rather than derived from ``analytics.signal_for`` because a
# workflow definition may import only graph primitives — which is also the right
# boundary: *which* tests an autonomous run proposes is a decision belonging to
# this workflow's hash-identified definition, not a property of the registry.
# ``test_the_workflow_proposes_no_descriptive_test`` holds the two in agreement.
EXCLUDED_ANALYTICS_TEST_IDS = frozenset(
    {"period_compare", "stratify", "sampling"}
)

# The analysis dependency graph. Each capability ID maps to its direct
# dependencies in declaration order. Unlike the audit graph this one is a linear
# chain: every later outcome consumes the artifacts the previous one made
# durable, and there is no parallel branch to express.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "data.relationships_inferred": (),
    "data.join_utility_ready": ("data.relationships_inferred",),
    "data.joins_ready": ("data.join_utility_ready",),
    "analysis.register_ready": ("data.relationships_inferred",),
    "analysis.definitions_ready": ("analysis.register_ready",),
    "analysis.inputs_ready": ("analysis.definitions_ready",),
    "analysis.executed": ("analysis.inputs_ready",),
    "analysis.summarized": ("analysis.executed",),
}

# Complete-analysis outcome set requested by "analyze these tables" style goals.
# The transitive closure of this outcome is the whole graph above.
#
# The terminal outcome is the memo, not the results: a request to analyse the
# data is answered by what the analysis *found*, and a screen of executed
# procedures is not that answer. The memo is what an auditor reads, and what
# the APM later consumes.
FULL_ANALYSIS_OUTCOMES = ["analysis.summarized"]

# Goal-template routing to requested outcome sets. ``data_analysis`` was
# previously an isolated ActionRunner intent; Phase 8 makes it a declared
# workflow goal. Isolated "run this saved analysis" and "pin this result"
# operations stay with ``ActionRunner`` and are intentionally absent here.
#
# ``analysis_execution`` requests the same terminal outcome as ``data_analysis``
# but is not a synonym for it: the scheduler reuses every earlier capability
# that is already satisfied, so with definitions in place the run executes them
# and spends no model turn at all. It is the declared way to say "bring the
# saved analyses up to date" without inviting new ones to be proposed.
#
# ``analysis_execution`` deliberately stops at ``analysis.executed`` rather than
# following ``data_analysis`` to the memo. Its whole point is that it spends no
# model turn when there is nothing left to propose, and summarising is a model
# turn. Re-running the procedures does make an existing memo stale; that is
# reported on the Summary screen, where regenerating is one deliberate click.
TEMPLATE_OUTCOMES: dict[str, list[str]] = {
    "data_analysis": FULL_ANALYSIS_OUTCOMES,
    "analysis_execution": ["analysis.executed"],
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
            "excluded_analytics_test_ids": sorted(EXCLUDED_ANALYTICS_TEST_IDS),
            "dependencies": {
                capability_id: list(deps)
                for capability_id, deps in DEPENDENCIES.items()
            },
        }
    )
