"""Authoritative document-analysis workflow definition.

This module is the single source of truth for the document-analysis lifecycle
*structure*: the workflow identity, its hash-identified metadata, the capability
dependency graph, and the goal-template outcome sets. Capability modules attach
readiness, unit expansion, context, workers, and executors to the capability IDs
declared here; they do not redefine the edges.

``documents.text_ready`` extracts document text deterministically,
``documents.analysis_chunks_ready`` maps each bounded source chunk through one
model worker and persists a run-local proposal per chunk,
``documents.analysis_generated`` reduces those proposals into one durable
analysis artifact, and ``documents.analysis_reviewed`` is the separate outcome an
auditor — never the agent — settles.

The first three capabilities are also declared by the audit graph, where
``planning.context_ready`` depends on ``documents.analysis_generated`` so
planning consumes generated analyses rather than falling back to raw document
text. The declarations live here and in :mod:`agent.capabilities.documents`
exactly once; the audit graph reuses them rather than restating a second
implementation.

The generic ``WorkflowRunner`` never imports this module — it receives a
capability registry by composition. Only the document-domain composition in the
grouped ``capabilities`` package reads the graph.
"""

from __future__ import annotations

from ..workflow import canonical_sha1

# Authoritative workflow identity persisted on every standalone document run.
WORKFLOW_ID = "documents_workflow_v1"

# The document-analysis dependency graph. Each capability ID maps to its direct
# dependencies in declaration order. The chain is linear: every later outcome
# consumes exactly what the previous one made durable.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "documents.text_ready": (),
    "documents.analysis_chunks_ready": ("documents.text_ready",),
    "documents.analysis_generated": ("documents.analysis_chunks_ready",),
    "documents.analysis_reviewed": ("documents.analysis_generated",),
}

# The capabilities the audit graph also declares. Auditor review is deliberately
# excluded: an audit run must never wait on, or imply, an auditor's review of a
# generated analysis.
AUDIT_CAPABILITY_IDS: tuple[str, ...] = (
    "documents.text_ready",
    "documents.analysis_chunks_ready",
    "documents.analysis_generated",
)

# "Analyze these documents" requests the generated outcome. Auditor review is
# never requested on the agent's behalf.
FULL_DOCUMENT_OUTCOMES = ["documents.analysis_generated"]

# Goal-template routing to requested outcome sets.
TEMPLATE_OUTCOMES: dict[str, list[str]] = {
    "document_analysis": FULL_DOCUMENT_OUTCOMES,
}


def dependencies(capability_id: str) -> tuple[str, ...]:
    """Return the authoritative direct dependencies for a capability ID."""

    return DEPENDENCIES[capability_id]


def outcomes_for_template(template: str) -> list[str] | None:
    values = TEMPLATE_OUTCOMES.get(str(template or ""))
    return list(values) if values is not None else None


def definition_hash() -> str:
    """Hash-identify the authoritative document workflow definition.

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
