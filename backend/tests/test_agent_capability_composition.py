"""Tests for grouped audit capability composition and startup validation (P7.2).

The grouped ``capabilities`` package composes the audit capability registry from
the ``planning``, ``fieldwork``, and ``reporting`` modules and validates the
composition against the authoritative dependency graph in ``workflows.audit``
before any writer relies on it. Since ``P7.3`` this composed registry *is* the
live registry, so these tests pin the declarations themselves — the grouping,
ordering, and hash-relevant fields — and prove that startup validation rejects
inconsistent compositions.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from app.agent import capabilities
from app.agent.capabilities import planning
from app.agent.runtime import CapabilityExecution, CapabilityExecutionRegistry
from app.agent.workflow import Capability, CapabilityRegistry
from app.agent.workflows import audit as audit_workflow


def _sha256() -> str:
    return "sha256:" + hashlib.sha256(b"test").hexdigest()


def _registry_of(capabilities_list) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for capability in capabilities_list:
        registry.register(capability)
    return registry


def _executions_for(capability_ids) -> CapabilityExecutionRegistry:
    executions = CapabilityExecutionRegistry()
    for capability_id in capability_ids:
        executions.register(
            CapabilityExecution(
                capability_id=capability_id,
                implementation_hash=_sha256(),
                deterministic_executor=lambda *_args, **_kwargs: None,
            )
        )
    return executions


def test_groups_partition_the_authoritative_graph_exactly_once():
    grouped = capabilities.grouped_capability_ids()
    assert len(grouped) == len(set(grouped))  # disjoint groups
    assert set(grouped) == set(audit_workflow.DEPENDENCIES)


def _declaration_fields(capability: Capability) -> dict:
    """Normalized, hash-relevant declaration fields (excludes the callables)."""

    return {
        field.name: getattr(capability, field.name)
        for field in dataclasses.fields(capability)
        if field.name not in {"readiness", "expand_units"}
    }


def test_build_audit_registry_is_deterministic_and_matches_the_startup_registry():
    rebuilt = capabilities.build_audit_registry()

    assert [capability.id for capability in rebuilt.all()] == list(
        capabilities.grouped_capability_ids()
    )
    assert [
        _declaration_fields(capability) for capability in rebuilt.all()
    ] == [
        _declaration_fields(capability)
        for capability in capabilities.AUDIT_REGISTRY.all()
    ]
    # ``REGISTRY`` is the live alias used by routing and audit dispatch.
    assert capabilities.REGISTRY is capabilities.AUDIT_REGISTRY


# The declaration fields that participate in a capability's normalized identity.
# Pinning them here is the golden test that a later refactor cannot silently
# change a stage id, worker kind, declared context preset, or invalidation set.
_DECLARED = {
    "planning.context_ready": (
        "planning_context",
        "planning_context",
        "planning.context",
        ("sources",),
    ),
    "planning.apm_ready": ("apm", "apm", "planning.apm", ("planning:context",)),
    "planning.rcm_ready": ("rcm", "rcm", "planning.rcm", ("planning:apm",)),
    "planning.planned_tests_ready": (
        "planned_tests",
        "planned_test_generation",
        "planning.planned_tests",
        ("rcm",),
    ),
    "fieldwork.definitions_ready": (
        "execution_definitions",
        "execution_spec",
        "fieldwork.execution_definitions",
        ("planned_test",),
    ),
    "fieldwork.executed": (
        "execution",
        "mixed_execution",
        "fieldwork.document_qa",
        ("definition", "evidence"),
    ),
    "results.rolled_up": ("rollup", "rollup", None, ("execution",)),
    "findings.drafted": (
        "findings",
        "finding_draft",
        "reporting.finding_draft",
        ("observation",),
    ),
    "working_papers.generated": ("working_papers", "working_paper", None, ("rollup",)),
    "dashboard.curated": ("dashboard", "dashboard", None, ("rollup",)),
    "report.working_draft": (
        "report",
        "report",
        None,
        ("planning:apm", "rollup", "findings"),
    ),
    "audit.verified": ("verify", "verify", None, ("outputs",)),
}


@pytest.mark.parametrize("capability_id", sorted(_DECLARED))
def test_declared_capability_identity_fields_are_pinned(capability_id):
    stage_id, worker_kind, context, invalidate_on = _DECLARED[capability_id]
    capability = capabilities.AUDIT_REGISTRY.get(capability_id)

    assert capability.stage_id == stage_id
    assert capability.worker_kind == worker_kind
    assert capability.context == context
    assert tuple(capability.invalidate_on) == invalidate_on
    assert tuple(capability.depends_on) == audit_workflow.dependencies(capability_id)


class _StubWorkspace:
    """Minimal stand-in exposing only what planning readiness/expansion read."""

    def __init__(self, *, planning=None, tables=None, documents=None, rcm=None):
        self.planning = planning or {}
        self.tables = tables or {}
        self.documents = documents or []
        self.rcm = list(rcm or [])


# Representative states exercising each planning capability's readiness branches:
# absent inputs, satisfied context + structured APM, valid RCM rows with
# executable planned tests, and an invalid RCM row.
_PLANNING_STATES = (
    _StubWorkspace(planning={}),
    _StubWorkspace(
        planning={
            "context": {"objective": "o", "scope": "s"},
            "apm_markdown": "# Objective\n\nbody",
        },
        tables={"t": 1},
    ),
    _StubWorkspace(
        planning={"context": {"objective": "o"}},
        documents=[{"id": "d"}],
        rcm=[
            {
                "id": "R1",
                "risk": "r",
                "control": "c",
                "planned_tests": [
                    {"id": "P1", "method": "validation", "steps": ["s"]}
                ],
            }
        ],
    ),
    _StubWorkspace(rcm=[{"id": "R2", "risk": "", "control": ""}]),
)


@pytest.mark.parametrize("capability_id", sorted(planning.CAPABILITY_IDS))
def test_planning_readiness_and_units_are_stable_across_representative_states(
    capability_id,
):
    """Planning readiness and expansion stay deterministic across the branches
    they were characterized on: absent inputs, satisfied context + structured
    APM, valid RCM rows with executable planned tests, and an invalid RCM row."""
    capability = capabilities.AUDIT_REGISTRY.get(capability_id)

    for stub in _PLANNING_STATES:
        first = capability.readiness(stub, {}).payload()
        assert first == capability.readiness(stub, {}).payload()
        assert first["state"] in {
            "satisfied",
            "missing",
            "review_required",
            "blocked",
        }
        assert capability.expand_units(stub, {}) == capability.expand_units(stub, {})


@pytest.mark.parametrize(
    "capability_id", sorted(capabilities.grouped_capability_ids())
)
def test_every_declaration_is_deterministic_on_a_real_workspace(
    capability_id, workspace_with_data
):
    """Fieldwork and reporting readiness read RCM execution manifests, document
    tests, working papers, findings, and report state, so a real workspace — not
    a stub — is required to exercise them."""
    capability = capabilities.AUDIT_REGISTRY.get(capability_id)

    readiness = capability.readiness(workspace_with_data, {}).payload()
    assert readiness == capability.readiness(workspace_with_data, {}).payload()
    units = capability.expand_units(workspace_with_data, {})
    assert units == capability.expand_units(workspace_with_data, {})
    assert len({unit.id for unit in units}) == len(units)


def test_startup_registry_matches_authoritative_graph():
    registry = capabilities.AUDIT_REGISTRY
    assert {capability.id: capability.depends_on for capability in registry.all()} == {
        capability_id: audit_workflow.dependencies(capability_id)
        for capability_id in audit_workflow.DEPENDENCIES
    }
    # Re-validating the startup registry is a no-op that returns it unchanged.
    assert capabilities.validate_audit_composition(registry) is registry


def test_validation_accepts_matching_execution_bindings():
    registry = capabilities.AUDIT_REGISTRY
    executions = _executions_for(audit_workflow.DEPENDENCIES)
    assert (
        capabilities.validate_audit_composition(registry, executions=executions)
        is registry
    )


def test_validation_rejects_missing_execution_binding():
    registry = capabilities.AUDIT_REGISTRY
    incomplete = [cid for cid in audit_workflow.DEPENDENCIES if cid != "audit.verified"]
    with pytest.raises(ValueError, match="audit.verified"):
        capabilities.validate_audit_composition(
            registry, executions=_executions_for(incomplete)
        )


def test_validation_rejects_a_changed_edge():
    live = capabilities.AUDIT_REGISTRY
    tampered = [
        dataclasses.replace(capability, depends_on=("planning.context_ready",))
        if capability.id == "planning.rcm_ready"
        else capability
        for capability in live.all()
    ]
    with pytest.raises(capabilities.AuditCompositionError, match="planning.rcm_ready"):
        capabilities.validate_audit_composition(_registry_of(tampered))


def test_validation_rejects_a_missing_capability():
    live = capabilities.AUDIT_REGISTRY
    partial = [
        capability for capability in live.all() if capability.id != "audit.verified"
    ]
    with pytest.raises(capabilities.AuditCompositionError, match="audit.verified"):
        capabilities.validate_audit_composition(_registry_of(partial))


def test_validation_rejects_an_unregistered_context_preset():
    live = capabilities.AUDIT_REGISTRY
    tampered = [
        dataclasses.replace(capability, context="does.not.exist")
        if capability.id == "planning.apm_ready"
        else capability
        for capability in live.all()
    ]
    with pytest.raises(capabilities.AuditCompositionError, match="does.not.exist"):
        capabilities.validate_audit_composition(_registry_of(tampered))


def test_declared_context_presets_are_registered():
    from app.agent.context import PRESETS

    known = {preset.preset_id for preset in PRESETS.all()}
    for capability in capabilities.AUDIT_REGISTRY.all():
        if capability.context is not None:
            assert str(capability.context) in known


def test_the_retired_audit_modules_have_no_module_alias_or_reexport():
    """`P7.3` deletion gate: the grouped package is the only audit registry."""
    import importlib
    import pathlib

    import app.agent as agent_package

    for name in ("audit_capabilities", "audit_workers"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"app.agent.{name}")
        assert not hasattr(agent_package, name)

    agent_dir = pathlib.Path(agent_package.__file__).parent
    assert not (agent_dir / "audit_capabilities.py").exists()
    assert not (agent_dir / "audit_workers.py").exists()
    assert not list(agent_dir.glob("compat*"))
    # The router moved to routing.py, its only caller.
    from app.agent import routing

    assert routing.ROUTER_SYSTEM.startswith("[agent:workflow_router]")
    assert callable(routing.validate_router_result)
