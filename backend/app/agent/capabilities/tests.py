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

from ... import (
    analysis_promotion,
    cycle_measurement,
    doc_tests,
    rcm_execution,
)
from ...text import counted, verb
from ...workspaces import Workspace
from ..workflow import Capability, Readiness, UnitSpec, semantic_unit_id
from ..workflows import audit as audit_workflow
from ._shared import rows as _rows
from ._shared import target_rcm_ids as _target_rcm_ids

CAPABILITY_IDS: tuple[str, ...] = (
    "tests.specified",
    "tests.promoted_from_analysis",
)


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


def _unvouched_types(workspace: Workspace) -> list[str]:
    """Document types whose records were extracted and never vouched against.

    The bypass this reports is silent by construction. Cycle vouching is
    reachable only through a ``transaction_cycle`` control attribute; when the
    matrix classifies every attribute some other way, no cycle test is
    requested, the linker and the evaluator are never called, and the run
    reports success. An engagement whose documents were read against induced
    schemas and then never tie-matched has degraded from the strongest evidence
    path available to it, and must say so.

    Deliberately keyed on the extracted records rather than on the matrix: it
    is the matrix's classification that is in question, so it cannot also be
    the thing that decides whether the question gets asked.
    """
    if not workspace.documents:
        return []
    if any(
        doc_tests.is_cycle_test(test) for test in doc_tests.list_tests(workspace)
    ):
        return []
    try:
        records = cycle_measurement.structured_records(workspace)
    except Exception:
        # Readiness reports; it never fails the run on evidence it cannot read.
        return []
    return sorted({
        str(row.get("document_type") or "")
        for row in records
        if row.get("document_type")
    })


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
        unvouched = _unvouched_types(workspace)
        if unvouched:
            return Readiness(
                "review_required",
                (
                    f"{counted(len(unvouched), 'document type')} "
                    f"{verb(len(unvouched), 'was', 'were')} extracted against an "
                    f"induced schema ({', '.join(unvouched)}) and no cycle test "
                    "vouches them; the matrix classified no control attribute as "
                    "transaction_cycle",
                ),
                details={"ready": ready, "total": total},
            )
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
                f"{counted(review_required, 'RCM row')} {verb(review_required, 'carries', 'carry')} an auditor-owned test that "
                "cannot be overwritten",
            ),
            details={"ready": ready, "total": total},
        )
    return Readiness(
        "missing",
        (f"{counted(total - ready, 'RCM row')} {verb(total - ready)} at least one executable test",),
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


# --------------------------------------------------------------------------- #
# tests.promoted_from_analysis
# --------------------------------------------------------------------------- #
def _promotion_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Whether every procedure that found something has been answered for.

    Satisfied when nothing is pending, including the case where no analysis
    ever ran: this capability adds no work of its own and must not turn an
    engagement that did no exploratory analysis into an incomplete one.

    Deliberately not scoped by table. A procedure's disposition is a statement
    about the audit's coverage of it, and narrowing an unscoped request to six
    base tables would leave the rest silently unanswered — which is the exact
    shape of the loss this capability exists to close.
    """
    if not workspace.rcm:
        return Readiness(
            "blocked",
            ("no RCM rows exist to fit a promoted procedure to",),
            blocking_on=("planning.rcm_ready",),
        )
    pending = analysis_promotion.candidates(workspace)
    details = {
        "pending": len(pending),
        "exceptions": sum(
            analysis_promotion.exception_count(item) for item in pending
        ),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (
            f"{counted(len(pending), 'saved analysis', 'saved analyses')} holding "
            f"exceptions {verb(len(pending), 'has', 'have')} neither become a test "
            "nor been recorded as declined",
        ),
        details=details,
    )


def _promotion_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    if not workspace.rcm:
        return []
    return [
        UnitSpec(
            semantic_unit_id("analysis_promotion", str(item["id"])),
            "analysis_promotion",
            f"Place analysis — {item.get('title') or item['id']}",
            (f"analysis:{item['id']}",),
            {"analysis_id": str(item["id"])},
        )
        for item in analysis_promotion.candidates(workspace)
    ]


def _analysis_promoted() -> Capability:
    return Capability(
        "tests.promoted_from_analysis",
        "analysis_promotion",
        "Analyses placed in the matrix",
        "analysis_promotion",
        audit_workflow.dependencies("tests.promoted_from_analysis"),
        _promotion_ready,
        _promotion_units,
        context="analysis.promotion",
        # One unit is one saved procedure. The units read no shared state and
        # each commits under its own analysis' parent hash, so the ordering
        # between them is immaterial and the stage is free to fan out.
        barrier="all_settled_parallel",
        # A rewritten RCM invalidates every fit: a procedure placed against a
        # row that no longer exists is a placement nobody made.
        invalidate_on=("rcm",),
    )


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
        # One unit is one RCM row: the rows are independent, the turn reads no
        # other unit's output, and the commit guards exactly its own row's parent
        # hash against a freshly read workspace, so nothing here depends on a
        # sibling having landed first. Serialized, a row-per-unit expansion is
        # also the one capability that cannot finish inside the run budget —
        # seventy turns at a minute each exhaust it before the stage completes.
        barrier="all_settled_parallel",
        invalidate_on=("rcm",),
    )


_BUILDERS = {
    "tests.specified": _tests_specified,
    "tests.promoted_from_analysis": _analysis_promoted,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
