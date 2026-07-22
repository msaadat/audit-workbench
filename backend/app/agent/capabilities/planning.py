"""Planning capability group of the audit workflow.

Owns the planning outcomes of the authoritative audit graph:
``planning.context_ready``, ``planning.apm_ready``, ``planning.rcm_ready``, and
``planning.planned_tests_ready``.

During Phase 7 each slice (P7A–P7D) moves one capability's declaration into this
module and its sibling grouped worker/executor modules. A capability listed in
``LOCALLY_OWNED`` is constructed here from readiness and unit-expansion bodies
that live in this module; the remaining capabilities are still *selected* from
the transitional ``audit_capabilities`` registry until their slice lands. The
capability IDs, ordering, grouping, and normalized definition hashes are stable
across that migration — the golden identity tests pin them to the authoritative
live registry.
"""

from __future__ import annotations

from ...workspaces import Workspace
from ..workflow import Capability, CapabilityRegistry, Readiness, UnitSpec
from ..workflows import audit as audit_workflow

CAPABILITY_IDS: tuple[str, ...] = (
    "planning.context_ready",
    "planning.apm_ready",
    "planning.rcm_ready",
    "planning.planned_tests_ready",
)

# Capabilities whose declaration is constructed locally in this module rather
# than selected from the transitional ``audit_capabilities`` registry. Each
# Phase 7A–7D slice adds one ID here as it moves its readiness and unit
# expansion into this module.
LOCALLY_OWNED: frozenset[str] = frozenset({"planning.context_ready"})


# --------------------------------------------------------------------------- #
# planning.context_ready (P7A)
# --------------------------------------------------------------------------- #
def _context_ready(workspace: Workspace, _scope: dict) -> Readiness:
    context = workspace.planning.get("context") or {}
    missing = [
        field
        for field in ("objective", "scope")
        if not str(context.get(field) or "").strip()
    ]
    if not missing:
        return Readiness("satisfied")
    if not workspace.tables and not workspace.documents:
        return Readiness("blocked", ("no imported data or documents are available",))
    return Readiness(
        "missing", tuple(f"engagement {field} is missing" for field in missing)
    )


def _context_units(_workspace: Workspace, _scope: dict) -> list[UnitSpec]:
    return [
        UnitSpec(
            "planning_context",
            "planning_context",
            "Assemble planning context",
            (),
            (),
        )
    ]


def _planning_context_ready() -> Capability:
    return Capability(
        "planning.context_ready",
        "planning_context",
        "Planning context",
        "planning_context",
        audit_workflow.dependencies("planning.context_ready"),
        _context_ready,
        _context_units,
        invalidate_on=("sources",),
    )


# Locally-owned declaration builders keyed by capability ID.
_BUILDERS = {
    "planning.context_ready": _planning_context_ready,
}


def capabilities(source: CapabilityRegistry | None = None) -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order.

    Locally-owned capabilities are constructed from this module's readiness and
    unit-expansion bodies; the rest are selected from ``source`` (the
    transitional ``audit_capabilities`` registry) until their slice migrates.
    """

    resolved: list[Capability] = []
    registry: CapabilityRegistry | None = None
    for capability_id in CAPABILITY_IDS:
        builder = _BUILDERS.get(capability_id)
        if builder is not None:
            resolved.append(builder())
            continue
        if registry is None:
            if source is not None:
                registry = source
            else:
                from .. import audit_capabilities

                registry = audit_capabilities.build_registry()
        resolved.append(registry.get(capability_id))
    return tuple(resolved)
