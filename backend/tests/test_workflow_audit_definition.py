"""Golden tests for the authoritative audit workflow definition (P7.1/P7.1A).

``workflows.audit`` is the single source of truth for the audit lifecycle
structure: workflow identity, hash-identified metadata, and the baseline
capability dependency graph. These tests pin the exact edges, the topological
closure, the intentional parallel branches after roll-up, and the stability of
the workflow definition hash. Behavior attached to each capability ID is tested
separately; here we only characterize the graph and its metadata.

Phase 9 extended the graph with the scoped document-analysis edge:
``planning.context_ready`` depends on ``documents.analysis_generated``, so
planning consumes generated analyses rather than falling back to raw document
text. That deliberately changed ``definition_hash()``. It is not a universal
prerequisite: only the capability that requires an analyzed document carries the
edge, and every document capability's readiness is satisfied with no document in
scope, so an audit without documents expands no document unit at all.
"""

from __future__ import annotations

from app.agent import capabilities as audit_capabilities
from app.agent.workflows import audit


EXPECTED_DEPENDENCIES = {
    "sources.imported": (),
    "documents.text_ready": (),
    "documents.categorized": ("documents.text_ready",),
    "documents.types_classified": ("documents.categorized",),
    "documents.schemas_sampled": ("documents.types_classified",),
    "documents.schemas_induced": ("documents.schemas_sampled",),
    "documents.analysis_chunks_ready": (
        "documents.text_ready",
        "documents.schemas_induced",
    ),
    "documents.analysis_generated": ("documents.analysis_chunks_ready",),
    "data.relationships_inferred": (),
    "data.join_utility_ready": ("data.relationships_inferred",),
    "data.joins_ready": ("data.join_utility_ready",),
    "analysis.register_ready": ("data.joins_ready",),
    "analysis.definitions_ready": ("analysis.register_ready",),
    "analysis.executed": ("analysis.definitions_ready",),
    "analysis.summarized": ("analysis.executed",),
    "planning.context_ready": ("sources.imported", "documents.analysis_generated"),
    "planning.apm_ready": ("planning.context_ready",),
    "planning.rcm_ready": (
            "planning.apm_ready",
            "documents.categorized",
            "documents.types_classified",
            "documents.schemas_induced",
        ),
    "tests.cycle_ruleset_proposed": (
        "planning.rcm_ready",
        "documents.schemas_induced",
    ),
    "tests.specified": ("planning.rcm_ready", "tests.cycle_ruleset_proposed"),
    "tests.promoted_from_analysis": ("tests.specified",),
    "fieldwork.executed": ("tests.specified", "tests.promoted_from_analysis"),
    "results.rolled_up": ("fieldwork.executed",),
    "findings.drafted": ("results.rolled_up",),
    "working_papers.generated": ("results.rolled_up",),
    "report.working_draft": (
        "planning.apm_ready",
        "results.rolled_up",
        "findings.drafted",
    ),
    "audit.verified": (
        "working_papers.generated",
        "report.working_draft",
    ),
}


def test_authoritative_graph_matches_baseline_edges():
    assert audit.DEPENDENCIES == EXPECTED_DEPENDENCIES


def test_registry_derives_every_edge_from_the_authoritative_graph():
    registry = audit_capabilities.build_audit_registry()
    assert {
        capability.id: capability.depends_on for capability in registry.all()
    } == audit.DEPENDENCIES


def test_full_audit_closure_is_topological():
    registry = audit_capabilities.build_audit_registry()
    resolved = registry.closure(audit.FULL_AUDIT_OUTCOMES)

    assert resolved == [
        "data.relationships_inferred",
        "data.join_utility_ready",
        "data.joins_ready",
        "analysis.register_ready",
        "analysis.definitions_ready",
        "analysis.executed",
        "analysis.summarized",
        "sources.imported",
        "documents.text_ready",
        "documents.categorized",
        "documents.types_classified",
        "documents.schemas_sampled",
        "documents.schemas_induced",
        "documents.analysis_chunks_ready",
        "documents.analysis_generated",
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "tests.cycle_ruleset_proposed",
        "tests.specified",
        "tests.promoted_from_analysis",
        "fieldwork.executed",
        "results.rolled_up",
        "findings.drafted",
        "working_papers.generated",
        "report.working_draft",
        "audit.verified",
    ]
    position = {capability_id: index for index, capability_id in enumerate(resolved)}
    assert all(
        position[dependency] < position[capability_id]
        for capability_id, deps in audit.DEPENDENCIES.items()
        for dependency in deps
    )
    # The EDA branch, memo included, completes before planning reads it.
    assert resolved.index("analysis.summarized") < resolved.index("planning.apm_ready")


def test_rollup_fans_out_into_parallel_branches():
    # Working papers and finding drafts both depend only on results.rolled_up —
    # they are intentionally independent siblings, not a linear chain.
    assert audit.dependencies("findings.drafted") == ("results.rolled_up",)
    assert audit.dependencies("working_papers.generated") == ("results.rolled_up",)


def test_template_outcomes_reference_declared_capabilities():
    registry = audit_capabilities.build_audit_registry()
    declared = {capability.id for capability in registry.all()}
    for outcomes in audit.TEMPLATE_OUTCOMES.values():
        assert set(outcomes) <= declared
    # The capabilities-package re-exports are the same authoritative objects.
    assert audit_capabilities.FULL_AUDIT_OUTCOMES == audit.FULL_AUDIT_OUTCOMES
    assert audit_capabilities.outcomes_for_template("apm_only") == ["planning.apm_ready"]
    assert audit_capabilities.outcomes_for_template("unknown-template") is None
    # APM keeps its semantic planning-only dependency; analysis is scheduled
    # earlier in the full-audit stage order rather than made a prerequisite.
    assert audit.dependencies("planning.apm_ready") == ("planning.context_ready",)


def test_definition_hash_is_stable_and_covers_identity_and_edges():
    first = audit.definition_hash()
    second = audit.definition_hash()
    assert first == second
    assert isinstance(first, str) and len(first) == 40  # sha1 hex


def test_definition_hash_changes_when_an_edge_is_added(monkeypatch):
    # A Phase 9 scoped document-analysis edge (or any structural change) must
    # change the workflow definition hash.
    baseline = audit.definition_hash()
    extended = dict(audit.DEPENDENCIES)
    extended["fieldwork.executed"] = (
        *extended["fieldwork.executed"],
        "documents.analysis_generated",
    )
    monkeypatch.setattr(audit, "DEPENDENCIES", extended)
    assert audit.definition_hash() != baseline
