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

    def __init__(self, *, planning=None, tables=None, documents=None):
        self.planning = planning or {}
        self.tables = tables or {}
        self.documents = documents or []


_CONTEXT_READY_STATES = (
    _StubWorkspace(planning={"context": {"objective": "o", "scope": "s"}}),
    _StubWorkspace(planning={"context": {"objective": "o"}}, tables={"t": 1}),
    _StubWorkspace(planning={}),  # no objective/scope, no data or documents
    _StubWorkspace(planning={"context": {"scope": "s"}}, documents=[{"id": "d"}]),
)


def test_locally_owned_context_ready_matches_live_declaration_and_behavior():
    owned = capabilities.AUDIT_REGISTRY.get("planning.context_ready")
    live = audit_capabilities.REGISTRY.get("planning.context_ready")

    # Golden identity: the moved declaration is hash-relevant identical to live.
    assert "planning.context_ready" in _locally_owned_ids()
    assert _declaration_fields(owned) == _declaration_fields(live)

    # Golden behavior: readiness matches live across representative states.
    for stub in _CONTEXT_READY_STATES:
        assert owned.readiness(stub, {}).payload() == live.readiness(stub, {}).payload()

    # Golden behavior: unit expansion produces identical semantic units.
    stub = _CONTEXT_READY_STATES[0]
    assert owned.expand_units(stub, {}) == live.expand_units(stub, {})


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
