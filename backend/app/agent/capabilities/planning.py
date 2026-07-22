"""Planning capability group of the audit workflow.

Owns the planning outcomes of the authoritative audit graph:
``planning.context_ready``, ``planning.apm_ready``, ``planning.rcm_ready``, and
``planning.planned_tests_ready``.

During Phase 7 these declarations are still *selected* from the transitional
``audit_capabilities`` registry: the readiness, unit expansion, context, worker,
and executor bodies continue to live there. Each slice (P7A–P7D) moves one
capability's declaration into this module and its sibling grouped worker/executor
modules, at which point ``capabilities`` below constructs the declaration locally
instead of selecting it. The capability IDs, ordering, and grouping are stable
across that migration.
"""

from __future__ import annotations

from .. import audit_capabilities
from ..workflow import Capability, CapabilityRegistry

CAPABILITY_IDS: tuple[str, ...] = (
    "planning.context_ready",
    "planning.apm_ready",
    "planning.rcm_ready",
    "planning.planned_tests_ready",
)


def capabilities(source: CapabilityRegistry | None = None) -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    registry = source if source is not None else audit_capabilities.build_registry()
    return tuple(registry.get(capability_id) for capability_id in CAPABILITY_IDS)
