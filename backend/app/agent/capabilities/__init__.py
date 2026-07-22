"""Grouped audit capability composition and startup validation.

This package composes the audit capability registry from the grouped
``planning``, ``fieldwork``, and ``reporting`` modules, and validates that the
composition matches the authoritative dependency graph in
:mod:`agent.workflows.audit` before any writer relies on it.

During Phase 7 the grouped modules still *select* their declarations from the
transitional :mod:`agent.audit_capabilities` registry; the composition and
validation here are the target structure that each capability-family slice fills
in. ``AUDIT_REGISTRY`` below is validated at import (startup validation), so an
inconsistent grouping, a graph mismatch, an undeclared context preset, or a
dependency cycle fails fast rather than at run time.
"""

from __future__ import annotations

from .. import audit_capabilities
from ..context import PRESETS
from ..runtime import CapabilityExecutionRegistry
from ..workflow import Capability, CapabilityRegistry
from ..workflows import audit as audit_workflow
from . import fieldwork, planning, reporting

# Grouped capability modules in authoritative order. Each module owns a disjoint
# slice of the audit graph and exposes ``CAPABILITY_IDS`` plus ``capabilities()``.
CAPABILITY_GROUPS = (planning, fieldwork, reporting)


class AuditCompositionError(ValueError):
    """Raised when the grouped audit composition is internally inconsistent."""


def grouped_capability_ids() -> tuple[str, ...]:
    """Every capability ID contributed by the grouped modules, in order."""

    return tuple(
        capability_id
        for group in CAPABILITY_GROUPS
        for capability_id in group.CAPABILITY_IDS
    )


def build_audit_registry(source: CapabilityRegistry | None = None) -> CapabilityRegistry:
    """Compose the audit capability registry from the grouped modules.

    ``source`` supplies the declaration bodies while Phase 7 slices are still in
    flight; it defaults to the transitional ``audit_capabilities`` registry.
    """

    resolved_source = source if source is not None else audit_capabilities.build_registry()
    registry = CapabilityRegistry()
    for group in CAPABILITY_GROUPS:
        for capability in group.capabilities(resolved_source):
            registry.register(capability)
    return registry


def _registered_preset_ids() -> frozenset[str]:
    return frozenset(preset.preset_id for preset in PRESETS.all())


def validate_audit_composition(
    registry: CapabilityRegistry,
    *,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityRegistry:
    """Validate the composed registry against the authoritative audit graph.

    Checks that the grouped modules partition the audit graph exactly once, that
    the composed registry declares precisely the authoritative capabilities with
    the authoritative edges, that the dependency closure is acyclic, that every
    declared context preset is registered, and — when supplied — that an
    execution binding exists for each capability.
    """

    authoritative_ids = frozenset(audit_workflow.DEPENDENCIES)

    grouped = grouped_capability_ids()
    duplicates = sorted({cid for cid in grouped if grouped.count(cid) > 1})
    if duplicates:
        raise AuditCompositionError(
            "Capability groups overlap on: " + ", ".join(duplicates) + "."
        )
    grouped_set = frozenset(grouped)
    if grouped_set != authoritative_ids:
        missing = sorted(authoritative_ids - grouped_set)
        extra = sorted(grouped_set - authoritative_ids)
        raise AuditCompositionError(
            "Grouped capability partition does not cover the audit graph "
            f"(missing: {missing}; extra: {extra})."
        )

    declared = [capability.id for capability in registry.all()]
    declared_set = frozenset(declared)
    if declared_set != authoritative_ids:
        missing = sorted(authoritative_ids - declared_set)
        extra = sorted(declared_set - authoritative_ids)
        raise AuditCompositionError(
            "Composed registry does not match the authoritative audit graph "
            f"(missing: {missing}; extra: {extra})."
        )

    for capability in registry.all():
        expected = audit_workflow.dependencies(capability.id)
        if tuple(capability.depends_on) != expected:
            raise AuditCompositionError(
                f"Capability '{capability.id}' declares dependencies "
                f"{tuple(capability.depends_on)} but the authoritative graph "
                f"requires {expected}."
            )

    # Acyclicity: closure over every capability raises on a dependency cycle.
    registry.closure(declared)

    known_presets = _registered_preset_ids()
    for capability in registry.all():
        preset_id = capability.context
        if preset_id is not None and str(preset_id) not in known_presets:
            raise AuditCompositionError(
                f"Capability '{capability.id}' declares unregistered context "
                f"preset '{preset_id}'."
            )

    if executions is not None:
        # CapabilityExecutionRegistry.validate raises on missing/unknown bindings.
        executions.validate(registry)

    return registry


# Startup validation: build and validate the grouped composition at import time.
AUDIT_REGISTRY = validate_audit_composition(build_audit_registry())


__all__ = [
    "AUDIT_REGISTRY",
    "AuditCompositionError",
    "CAPABILITY_GROUPS",
    "build_audit_registry",
    "grouped_capability_ids",
    "validate_audit_composition",
]
