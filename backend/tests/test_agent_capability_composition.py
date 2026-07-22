"""Tests for grouped audit capability composition and startup validation (P7.2).

The grouped ``capabilities`` package composes the audit capability registry from
the ``planning``, ``fieldwork``, and ``reporting`` modules and validates the
composition against the authoritative dependency graph in ``workflows.audit``
before any writer relies on it. These tests prove the grouping faithfully
partitions the live registry and that startup validation rejects inconsistent
compositions.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from app.agent import audit_capabilities, capabilities
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
                transitional_batch_executor=lambda *_args, **_kwargs: None,
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


def _locally_owned_ids() -> frozenset[str]:
    owned: set[str] = set()
    for group in capabilities.CAPABILITY_GROUPS:
        owned |= set(getattr(group, "LOCALLY_OWNED", frozenset()))
    return frozenset(owned)


_ALL_LOCALLY_OWNED = sorted(_locally_owned_ids())


def test_composition_from_live_source_selects_or_owns_matching_declarations():
    live = audit_capabilities.REGISTRY
    composed = capabilities.build_audit_registry(source=live)
    composed_ids = [capability.id for capability in composed.all()]
    owned = _locally_owned_ids()

    assert composed_ids == list(capabilities.grouped_capability_ids())
    for cid in composed_ids:
        if cid in owned:
            # A migrated slice constructs its declaration locally; it is no
            # longer the same object but stays declaration-identical to live.
            assert composed.get(cid) is not live.get(cid)
            assert _declaration_fields(composed.get(cid)) == _declaration_fields(
                live.get(cid)
            )
        else:
            # Unmigrated capabilities are still selected as the exact live object.
            assert composed.get(cid) is live.get(cid)


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


@pytest.mark.parametrize("capability_id", sorted(planning.LOCALLY_OWNED))
def test_locally_owned_planning_declaration_matches_live_across_stub_states(capability_id):
    owned = capabilities.AUDIT_REGISTRY.get(capability_id)
    live = audit_capabilities.REGISTRY.get(capability_id)

    # Golden identity: the moved declaration is hash-relevant identical to live.
    assert capability_id in _locally_owned_ids()
    assert _declaration_fields(owned) == _declaration_fields(live)

    # Golden behavior: readiness and unit expansion match live across the
    # planning branches (absent, satisfied, valid rows, invalid row).
    for stub in _PLANNING_STATES:
        assert owned.readiness(stub, {}).payload() == live.readiness(stub, {}).payload()
        assert owned.expand_units(stub, {}) == live.expand_units(stub, {})


@pytest.mark.parametrize("capability_id", _ALL_LOCALLY_OWNED)
def test_locally_owned_declaration_matches_live_on_real_workspace(
    capability_id, workspace_with_data
):
    """Every migrated declaration matches the live registry field-for-field and
    behaves identically (readiness + expansion) against a real workspace.

    Fieldwork and reporting readiness read RCM execution manifests, document
    tests, working papers, findings, and report state, so a real workspace — not
    a stub — is required to exercise them without diverging from live."""
    owned = capabilities.AUDIT_REGISTRY.get(capability_id)
    live = audit_capabilities.REGISTRY.get(capability_id)

    assert _declaration_fields(owned) == _declaration_fields(live)
    assert (
        owned.readiness(workspace_with_data, {}).payload()
        == live.readiness(workspace_with_data, {}).payload()
    )
    assert owned.expand_units(workspace_with_data, {}) == live.expand_units(
        workspace_with_data, {}
    )


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
    live = audit_capabilities.REGISTRY
    tampered = [
        dataclasses.replace(capability, depends_on=("planning.context_ready",))
        if capability.id == "planning.rcm_ready"
        else capability
        for capability in live.all()
    ]
    with pytest.raises(capabilities.AuditCompositionError, match="planning.rcm_ready"):
        capabilities.validate_audit_composition(_registry_of(tampered))


def test_validation_rejects_a_missing_capability():
    live = audit_capabilities.REGISTRY
    partial = [
        capability for capability in live.all() if capability.id != "audit.verified"
    ]
    with pytest.raises(capabilities.AuditCompositionError, match="audit.verified"):
        capabilities.validate_audit_composition(_registry_of(partial))


def test_validation_rejects_an_unregistered_context_preset():
    live = audit_capabilities.REGISTRY
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
