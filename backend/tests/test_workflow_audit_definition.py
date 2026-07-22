"""Golden tests for the authoritative audit workflow definition (P7.1/P7.1A).

``workflows.audit`` is the single source of truth for the audit lifecycle
structure: workflow identity, hash-identified metadata, and the baseline
capability dependency graph. These tests pin the exact edges, the topological
closure, the intentional parallel branches after roll-up, and the stability of
the workflow definition hash. Behavior attached to each capability ID is tested
separately; here we only characterize the graph and its metadata.

Phase 9 may extend the graph with scoped ``documents.analysis_generated`` edges
where a specific capability declares a document-analysis dependency. Such a
change deliberately changes ``definition_hash()`` (see
``test_definition_hash_changes_when_an_edge_is_added``) but must not add a
universal document-analysis prerequisite: only the capabilities that require an
analyzed document gain the edge, and audits without documents keep the shape
asserted here.
"""

from __future__ import annotations

from app.agent import audit_capabilities
from app.agent.workflows import audit


EXPECTED_DEPENDENCIES = {
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


def test_authoritative_graph_matches_baseline_edges():
    assert audit.DEPENDENCIES == EXPECTED_DEPENDENCIES


def test_registry_derives_every_edge_from_the_authoritative_graph():
    registry = audit_capabilities.build_registry()
    assert {
        capability.id: capability.depends_on for capability in registry.all()
    } == audit.DEPENDENCIES


def test_full_audit_closure_is_topological():
    registry = audit_capabilities.build_registry()
    resolved = registry.closure(audit.FULL_AUDIT_OUTCOMES)

    assert resolved == [
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "planning.planned_tests_ready",
        "fieldwork.definitions_ready",
        "fieldwork.executed",
        "results.rolled_up",
        "findings.drafted",
        "working_papers.generated",
        "dashboard.curated",
        "report.working_draft",
        "audit.verified",
    ]
    position = {capability_id: index for index, capability_id in enumerate(resolved)}
    assert all(
        position[dependency] < position[capability_id]
        for capability_id, deps in audit.DEPENDENCIES.items()
        for dependency in deps
    )


def test_rollup_fans_out_into_parallel_branches():
    # Working papers, dashboard curation, and finding drafts all depend only on
    # results.rolled_up — they are intentionally independent siblings, not a
    # linear chain.
    assert audit.dependencies("findings.drafted") == ("results.rolled_up",)
    assert audit.dependencies("working_papers.generated") == ("results.rolled_up",)
    assert audit.dependencies("dashboard.curated") == ("results.rolled_up",)


def test_template_outcomes_reference_declared_capabilities():
    registry = audit_capabilities.build_registry()
    declared = {capability.id for capability in registry.all()}
    for outcomes in audit.TEMPLATE_OUTCOMES.values():
        assert set(outcomes) <= declared
    # The audit_capabilities re-exports are the same authoritative objects.
    assert audit_capabilities.FULL_AUDIT_OUTCOMES == audit.FULL_AUDIT_OUTCOMES
    assert audit_capabilities.outcomes_for_template("apm_only") == ["planning.apm_ready"]
    assert audit_capabilities.outcomes_for_template("unknown-template") is None


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
