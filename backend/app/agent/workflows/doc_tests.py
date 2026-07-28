"""Authoritative standalone document-test workflow definition.

This module is the single source of truth for the standalone document-test
lifecycle *structure*: the workflow identity, its hash-identified metadata, the
capability dependency graph, and the goal-template outcome sets. Capability
modules attach readiness, unit expansion, context, workers, and executors to the
capability IDs declared here; they do not redefine the edges.

``doc_tests.definitions_ready`` reports whether each scoped Document Test can
perform even its bounded local work, ``doc_tests.executed`` runs the outstanding
items — deterministic comparisons locally, cited Q&A answers through the
registered ``fieldwork.document_qa`` worker — and ``doc_tests.dispositioned`` is
the separate settlement outcome. Auto mode can apply a cited worker outcome;
deterministic, inconclusive, and permission-mode results remain with the auditor.

`P10.5` replaced ``DocTestRunner`` with this graph. The audit lifecycle continues
to schedule document tests through ``fieldwork.executed``; both graphs bind the
same unit kinds through the one binder in :mod:`agent.doc_tests_execution`, so
there is no second execution implementation. The reasoning is recorded in
``docs/agent-protocol-runner-decisions.md``.

The generic ``WorkflowRunner`` never imports this module — it receives a
capability registry by composition. Only the document-test composition in the
grouped ``capabilities`` package reads the graph.
"""

from __future__ import annotations

from ..workflow import canonical_sha1

# Authoritative workflow identity persisted on every standalone document-test run.
WORKFLOW_ID = "doc_tests_workflow_v1"

# The document-test dependency graph. Each capability ID maps to its direct
# dependencies in declaration order. The chain is linear: a test that cannot run
# is not executed, and nothing is dispositioned that was not executed.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "doc_tests.definitions_ready": (),
    "doc_tests.executed": ("doc_tests.definitions_ready",),
    "doc_tests.dispositioned": ("doc_tests.executed",),
}

# "Run this document test" requests execution. Auto-mode disposition is part of
# the model-backed execution binding; remaining auditor disposition is not added
# to the requested outcomes.
FULL_DOC_TEST_OUTCOMES = ["doc_tests.executed"]

# Goal-template routing to requested outcome sets.
TEMPLATE_OUTCOMES: dict[str, list[str]] = {
    "document_test_execution": FULL_DOC_TEST_OUTCOMES,
}


def dependencies(capability_id: str) -> tuple[str, ...]:
    """Return the authoritative direct dependencies for a capability ID."""

    return DEPENDENCIES[capability_id]


def outcomes_for_template(template: str) -> list[str] | None:
    values = TEMPLATE_OUTCOMES.get(str(template or ""))
    return list(values) if values is not None else None


def definition_hash() -> str:
    """Hash-identify the authoritative document-test workflow definition.

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
