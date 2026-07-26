"""Test capability group of the audit workflow.

Owns ``tests.drafted`` and ``tests.specified`` — the two passes that turn an RCM
row into the durable Document and Data Tests that cover it.

A test is one record with one source. The draft pass writes the audit plan for
each test an RCM row needs and chooses its source; the spec pass writes the
executable part — generated Polars code for a Data Test, items and checks for a
Document Test — onto the same record. Nothing in between is durable, and the RCM
row holds only ``test_refs``.

Each capability is declared here: its readiness (existence and structural
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

CAPABILITY_IDS: tuple[str, ...] = (
    "tests.drafted",
    "tests.specified",
)


def _scoped_manifest(workspace: Workspace, scope: dict) -> list[dict]:
    selected = set(_target_rcm_ids(workspace, scope))
    return [
        item
        for item in rcm_execution.test_manifest(workspace)
        if item["rcm_id"] in selected
    ]


# --------------------------------------------------------------------------- #
# tests.drafted
# --------------------------------------------------------------------------- #
def _testable(workspace: Workspace) -> bool:
    """Whether the workspace holds anything a test could actually be run against.

    A test is executable work, not a description, so with no imported table and
    no document there is nothing to draft: the row would get a plan that could
    never be specified.
    """
    return bool(workspace.tables or workspace.documents)


def _drafted_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    total = len(rows)
    linked = {item["rcm_id"] for item in _scoped_manifest(workspace, scope)}
    ready = sum(row["id"] in linked for row in rows)
    # Existence only: every row in scope has at least one test. Whether a test is
    # current with respect to its row is not assessed; the auditor forces a
    # regeneration instead.
    if total and ready == total:
        return Readiness("satisfied", details={"ready": ready, "total": total})
    if not _testable(workspace):
        return Readiness(
            "blocked",
            ("no imported data or documents are available to test against",),
            details={"ready": ready, "total": total},
        )
    return Readiness(
        "missing",
        (f"{total - ready} RCM row(s) need at least one test",),
        details={"ready": ready, "total": total},
    )


def _draft_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    if not _testable(workspace):
        return []
    linked = {item["rcm_id"] for item in _scoped_manifest(workspace, scope)}
    return [
        UnitSpec(
            semantic_unit_id("test_draft", row["id"]),
            "test_draft",
            f"Draft tests — {row.get('risk') or row['id']}",
            (f"rcm:{row['id']}",),
            row,
        )
        for row in _rows(workspace, scope)
        if row["id"] not in linked or scope.get("generation_mode") == "force"
    ]


def _tests_drafted() -> Capability:
    return Capability(
        "tests.drafted",
        "tests",
        "RCM tests",
        "test_draft",
        audit_workflow.dependencies("tests.drafted"),
        _drafted_ready,
        _draft_units,
        context="tests.draft",
        invalidate_on=("rcm",),
    )


# --------------------------------------------------------------------------- #
# tests.specified
# --------------------------------------------------------------------------- #
def _specified_ready(workspace: Workspace, scope: dict) -> Readiness:
    manifest = _scoped_manifest(workspace, scope)
    if not manifest:
        return Readiness("missing", ("no tests are available to specify",))
    pending = [item["test_id"] for item in manifest if not item["specified"]]
    if pending:
        return Readiness(
            "missing",
            (f"{len(pending)} test(s) have no executable specification",),
            details={"pending": len(pending), "total": len(manifest)},
        )
    # Every linked test is executable; currency relative to its plan is not
    # assessed. Parent hashes remain for executor CAS.
    return Readiness("satisfied", details={"total": len(manifest)})


def _spec_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    units = []
    for item in _scoped_manifest(workspace, scope):
        # Specify only what is still a draft, or everything on explicit force. A
        # spec that predates a plan edit is not regenerated automatically; the
        # auditor forces a regeneration instead.
        if item["specified"] and scope.get("generation_mode") != "force":
            continue
        worker_kind = (
            "data_test_spec" if item["kind"] == "datatest" else "document_test_spec"
        )
        units.append(
            UnitSpec(
                semantic_unit_id(worker_kind, item["test_id"]),
                worker_kind,
                f"Specify {item['kind']} — {item['title']}",
                (f"rcm:{item['rcm_id']}", f"{item['kind']}:{item['test_id']}"),
                item,
            )
        )
    return units


def _tests_specified() -> Capability:
    return Capability(
        "tests.specified",
        "test_specs",
        "Executable test specifications",
        "test_spec",
        audit_workflow.dependencies("tests.specified"),
        _specified_ready,
        _spec_units,
        context="tests.spec",
        invalidate_on=("test",),
    )


_BUILDERS = {
    "tests.drafted": _tests_drafted,
    "tests.specified": _tests_specified,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
