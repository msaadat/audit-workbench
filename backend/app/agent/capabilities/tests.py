"""Test capability group of the audit workflow.

Owns ``tests.specified`` — the single pass that generates the complete,
executable tests one RCM row needs and decides each test's source in one
model turn, per docs/test-capability-merge-plan.md.

A test is one record with one source. Generation writes the audit plan and
the executable part — Polars steps for a Data Test, items and checks for a
Document Test — onto the same record in one commit. Nothing in between is
durable, and the RCM row holds only ``test_refs``.

The capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry keys for its
declared context. The dependency edges come from the authoritative graph in
:mod:`agent.workflows.audit`; this module never restates them.
"""

from __future__ import annotations

from ... import rcm_execution
from ...workspaces import Workspace
from ..workflow import Capability, Readiness, UnitSpec, semantic_unit_id
from ..workflows import audit as audit_workflow
from ._shared import rows as _rows
from ._shared import target_rcm_ids as _target_rcm_ids

CAPABILITY_IDS: tuple[str, ...] = ("tests.specified",)


def _scoped_manifest(workspace: Workspace, scope: dict) -> list[dict]:
    selected = set(_target_rcm_ids(workspace, scope))
    return [
        item
        for item in rcm_execution.test_manifest(workspace)
        if item["rcm_id"] in selected
    ]


def _testable(workspace: Workspace) -> bool:
    """Whether the workspace holds anything a test could actually be run against.

    A test is executable work, not a description, so with no imported table and
    no document there is nothing to generate: the row would get a plan that
    could never be executed.
    """
    return bool(workspace.tables or workspace.documents)


def _by_row(manifest: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in manifest:
        grouped.setdefault(item["rcm_id"], []).append(item)
    return grouped


# --------------------------------------------------------------------------- #
# tests.specified
# --------------------------------------------------------------------------- #
def _specified_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    total = len(rows)
    grouped = _by_row(_scoped_manifest(workspace, scope))
    ready = 0
    review_required = 0
    for row in rows:
        tests = grouped.get(row["id"], [])
        if any(item["executable"] for item in tests):
            ready += 1
            continue
        # A linked agent-created draft is missing generation work, not a
        # blocker; an auditor-created draft agent generation cannot overwrite
        # surfaces for review instead of looking like ordinary missing work.
        if any(
            item["status"] == "draft" and item.get("created_by") != "agent"
            for item in tests
        ):
            review_required += 1
    if total and ready == total:
        return Readiness("satisfied", details={"ready": ready, "total": total})
    if not _testable(workspace):
        return Readiness(
            "blocked",
            ("no imported data or documents are available to test against",),
            details={"ready": ready, "total": total},
        )
    if review_required and ready + review_required == total:
        return Readiness(
            "review_required",
            (
                f"{review_required} RCM row(s) carry an auditor-owned test that "
                "cannot be overwritten",
            ),
            details={"ready": ready, "total": total},
        )
    return Readiness(
        "missing",
        (f"{total - ready} RCM row(s) need at least one executable test",),
        details={"ready": ready, "total": total},
    )


def _generation_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    if not _testable(workspace):
        return []
    grouped = _by_row(_scoped_manifest(workspace, scope))
    force = scope.get("generation_mode") == "force"
    units = []
    for row in _rows(workspace, scope):
        tests = grouped.get(row["id"], [])
        covered = any(item["executable"] for item in tests)
        upgradeable_draft = any(
            item["status"] == "draft" and item.get("created_by") == "agent"
            for item in tests
        )
        auditor_draft_blocks = any(
            item["status"] == "draft" and item.get("created_by") != "agent"
            for item in tests
        )
        if covered:
            if not force and not upgradeable_draft:
                continue
        elif auditor_draft_blocks:
            # Review-required, not a generation gap the executor can fix
            # without explicit overwrite permission.
            continue
        units.append(
            UnitSpec(
                semantic_unit_id("test_generation", row["id"]),
                "test_generation",
                f"Generate tests — {row.get('risk') or row['id']}",
                (f"rcm:{row['id']}",),
                row,
            )
        )
    return units


def _tests_specified() -> Capability:
    return Capability(
        "tests.specified",
        "test_specs",
        "Executable test specifications",
        "test_generation",
        audit_workflow.dependencies("tests.specified"),
        _specified_ready,
        _generation_units,
        context="tests.generate",
        invalidate_on=("rcm",),
    )


_BUILDERS = {
    "tests.specified": _tests_specified,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
