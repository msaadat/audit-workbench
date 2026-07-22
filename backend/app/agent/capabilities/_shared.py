"""Shared deterministic selectors for the grouped audit capability modules.

These helpers are transitional copies of the RCM-scope and observation selectors
that ``audit_capabilities`` uses for readiness and unit expansion. The planning,
fieldwork, and reporting capability groups all narrow the RCM by requested scope
and select eligible observations, so the logic lives here once rather than being
duplicated per group. Consolidated (with ``audit_capabilities``) at ``P7.3``.
"""

from __future__ import annotations

from ...workspaces import Workspace
from ..workflow import UnitSpec

# Dispositions that make an observation eligible to become a finding draft.
ELIGIBLE_DISPOSITIONS = frozenset(
    {"confirmed_control_exception", "draft_finding_candidate"}
)


def target_rcm_ids(workspace: Workspace, scope: dict) -> list[str]:
    """RCM row IDs in the requested scope (all rows when no target is given)."""

    refs = [str(value) for value in scope.get("target_refs") or []]
    selected = {
        value.split(":", 1)[1]
        for value in refs
        if value.startswith("rcm:") and ":" in value
    }
    return [row["id"] for row in workspace.rcm if not selected or row["id"] in selected]


def rows(workspace: Workspace, scope: dict) -> list[dict]:
    """RCM rows selected by the requested scope."""

    selected = set(target_rcm_ids(workspace, scope))
    return [row for row in workspace.rcm if row["id"] in selected]


def eligible_observations(workspace: Workspace) -> list[dict]:
    """Disposed observations whose disposition makes them finding-eligible."""

    return [
        item
        for item in workspace.observations
        if item.get("status") == "disposed"
        and item.get("disposition") in ELIGIBLE_DISPOSITIONS
    ]


def single_unit(kind: str, title: str, *parents: str):
    """A one-unit expansion for a whole-workspace capability."""

    return lambda _workspace, _scope: [
        UnitSpec(kind, kind, title, tuple(parents), parents)
    ]
