"""Planning capability group of the audit workflow.

Owns the planning outcomes of the authoritative audit graph:
``planning.context_ready``, ``planning.apm_ready``, ``planning.rcm_ready``, and
``planning.planned_tests_ready``.

During Phase 7 each slice moves one capability's declaration into this module.
A capability listed in ``LOCALLY_OWNED`` is constructed here from readiness and
unit-expansion bodies that live in this module; any remaining capability is still
*selected* from the transitional ``audit_capabilities`` registry until its slice
lands. The capability IDs, ordering, grouping, and normalized definition hashes
are stable across that migration — the golden identity tests pin them to the
authoritative live registry. This transitional duplication with
``audit_capabilities`` is removed at ``P7.3`` when that module is deleted and the
grouped registry goes live.

The ``.1`` declaration moves here (readiness + unit expansion) are independent of
the ``.2`` execution writer-switches: they populate only the parallel grouped
``capabilities`` package, while the live dispatch path still runs on
``audit_capabilities.REGISTRY`` and the ``audit_execution`` handlers.
"""

from __future__ import annotations

from ... import rcm_execution
from ...workspaces import Workspace
from ..workflow import (
    Capability,
    CapabilityRegistry,
    Readiness,
    UnitSpec,
    semantic_unit_id,
)
from ..workflows import audit as audit_workflow
from ._shared import rows as _rows
from ._shared import single_unit as _single

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
LOCALLY_OWNED: frozenset[str] = frozenset(
    {
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "planning.planned_tests_ready",
    }
)


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


# --------------------------------------------------------------------------- #
# planning.apm_ready (P7B)
# --------------------------------------------------------------------------- #
def _apm_ready(workspace: Workspace, _scope: dict) -> Readiness:
    markdown = str(workspace.planning.get("apm_markdown") or "").strip()
    if not markdown:
        return Readiness("missing", ("APM content is empty",))
    if not any(line.lstrip().startswith("#") for line in markdown.splitlines()):
        return Readiness("review_required", ("APM has no structured headings",))
    # Existence and structural usability only. Whether the APM is substantively
    # current with respect to changed planning sources is not assessed by the
    # framework; the auditor decides when to force a regeneration.
    return Readiness("satisfied", details={"artifact_count": 1})


def _planning_apm_ready() -> Capability:
    return Capability(
        "planning.apm_ready",
        "apm",
        "Audit planning memorandum",
        "apm",
        audit_workflow.dependencies("planning.apm_ready"),
        _apm_ready,
        _single("apm", "Draft audit planning memorandum", "planning:context"),
        context="planning.apm",
        invalidate_on=("planning:context",),
    )


# --------------------------------------------------------------------------- #
# planning.rcm_ready (P7C)
# --------------------------------------------------------------------------- #
def _rcm_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    if not rows:
        return Readiness(
            "missing",
            ("no RCM rows exist for the requested scope",),
            details={"artifact_count": 0},
        )
    invalid = [
        row["id"]
        for row in rows
        if not str(row.get("risk") or "").strip()
        or not str(row.get("control") or "").strip()
    ]
    if invalid:
        return Readiness(
            "review_required",
            (f"{len(invalid)} RCM row(s) lack a risk or control",),
            details={"artifact_count": len(rows)},
        )
    # Structurally usable RCM rows exist; currency relative to the APM is not
    # assessed. Parent hashes remain on the rows for executor CAS/provenance.
    return Readiness("satisfied", details={"artifact_count": len(rows)})


def _planning_rcm_ready() -> Capability:
    return Capability(
        "planning.rcm_ready",
        "rcm",
        "Risk and control matrix",
        "rcm",
        audit_workflow.dependencies("planning.rcm_ready"),
        _rcm_ready,
        _single("rcm", "Draft risk and control matrix", "planning:apm"),
        context="planning.rcm",
        invalidate_on=("planning:apm",),
    )


# --------------------------------------------------------------------------- #
# planning.planned_tests_ready (P7D)
# --------------------------------------------------------------------------- #
def _planned_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    total = len(rows)
    ready = sum(bool(row.get("planned_tests")) for row in rows)
    invalid = [
        planned.get("id")
        for row in rows
        for planned in row.get("planned_tests") or []
        if not planned.get("steps")
        or not rcm_execution.required_execution_kinds(str(planned.get("method") or ""))
    ]
    if invalid:
        return Readiness(
            "review_required",
            (f"{len(invalid)} planned test(s) are not executable",),
            details={"ready": ready, "total": total},
        )
    # Every RCM row has executable planned tests; currency relative to the RCM
    # row is not assessed. Parent hashes remain for executor CAS/provenance.
    if total and ready == total:
        return Readiness("satisfied", details={"ready": ready, "total": total})
    return Readiness(
        "missing",
        (f"{total - ready} RCM row(s) need planned tests",),
        details={"ready": ready, "total": total},
    )


def _planned_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    return [
        UnitSpec(
            semantic_unit_id("planned_test", row["id"]),
            "planned_test_generation",
            f"Draft planned tests — {row.get('risk') or row['id']}",
            (f"rcm:{row['id']}",),
            row,
        )
        for row in _rows(workspace, scope)
        if not row.get("planned_tests") or scope.get("generation_mode") == "force"
    ]


def _planning_planned_tests_ready() -> Capability:
    return Capability(
        "planning.planned_tests_ready",
        "planned_tests",
        "RCM planned tests",
        "planned_test_generation",
        audit_workflow.dependencies("planning.planned_tests_ready"),
        _planned_ready,
        _planned_units,
        invalidate_on=("rcm",),
    )


# Locally-owned declaration builders keyed by capability ID.
_BUILDERS = {
    "planning.context_ready": _planning_context_ready,
    "planning.apm_ready": _planning_apm_ready,
    "planning.rcm_ready": _planning_rcm_ready,
    "planning.planned_tests_ready": _planning_planned_tests_ready,
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
