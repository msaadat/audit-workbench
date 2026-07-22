"""Fieldwork capability group of the audit workflow.

Owns the fieldwork and roll-up outcomes of the authoritative audit graph:
``fieldwork.definitions_ready``, ``fieldwork.executed``, and
``results.rolled_up``.

As with :mod:`planning`, these declarations are selected from the transitional
``audit_capabilities`` registry during Phase 7; slices P7E–P7G move each
capability's readiness, unit expansion, workers, and executors into this module
and its sibling grouped modules while keeping the IDs and ordering stable.
"""

from __future__ import annotations

from .. import audit_capabilities
from ..workflow import Capability, CapabilityRegistry

CAPABILITY_IDS: tuple[str, ...] = (
    "fieldwork.definitions_ready",
    "fieldwork.executed",
    "results.rolled_up",
)


def capabilities(source: CapabilityRegistry | None = None) -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    registry = source if source is not None else audit_capabilities.build_registry()
    return tuple(registry.get(capability_id) for capability_id in CAPABILITY_IDS)
