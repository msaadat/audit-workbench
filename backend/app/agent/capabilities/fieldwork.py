"""Fieldwork capability group of the audit workflow.

Owns the fieldwork and roll-up outcomes of the authoritative audit graph:
``fieldwork.executed`` and ``results.rolled_up``. The tests themselves — their
plans and their executable specifications — belong to the tests capability group.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry keys for its
declared context. The dependency edges come from the authoritative graph in
:mod:`agent.workflows.audit`; this module never restates them.
"""

from __future__ import annotations

from ... import rcm_execution
from ...workspaces import Workspace
from ..workflow import (
    Capability,
    Readiness,
    UnitSpec,
    semantic_unit_id,
)
from ..workflows import audit as audit_workflow
from .doc_tests import document_test_units
from ._shared import rows as _rows
from ._shared import single_unit as _single
from ._shared import target_rcm_ids as _target_rcm_ids

CAPABILITY_IDS: tuple[str, ...] = (
    "fieldwork.executed",
    "results.rolled_up",
)


def _scoped_manifest(workspace: Workspace, scope: dict) -> list[dict]:
    selected = set(_target_rcm_ids(workspace, scope))
    return [
        item
        for item in rcm_execution.test_manifest(workspace)
        if item["rcm_id"] in selected and item["specified"]
    ]


# --------------------------------------------------------------------------- #
# fieldwork.executed (P7F)
# --------------------------------------------------------------------------- #
def _execution_ready(workspace: Workspace, scope: dict) -> Readiness:
    pending = []
    review = []
    blocked = []
    for test in _scoped_manifest(workspace, scope):
        # A test with a durable result counts as executed. Whether that result
        # predates changed inputs (``result_stale``) is currency, not structural
        # usability, and is not assessed by the framework.
        if not test.get("executable"):
            review.append(test["test_id"])
        elif test.get("has_durable_result"):
            continue
        elif test.get("status") == "blocked":
            blocked.append(test["test_id"])
        elif test.get("status") == "review_required" and test["kind"] == "doctest":
            review.append(test["test_id"])
        else:
            pending.append(test["test_id"])
    details = {
        "pending": len(pending),
        "review_required": len(review),
        "blocked": len(blocked),
    }
    if pending:
        return Readiness(
            "missing",
            (f"{len(pending)} execution artifact(s) have not run",),
            details=details,
        )
    if review:
        return Readiness(
            "review_required",
            (f"{len(review)} execution artifact(s) require auditor review",),
            details=details,
        )
    if blocked:
        return Readiness(
            "review_required",
            (f"{len(blocked)} execution artifact(s) are blocked on evidence",),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _execution_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    units = []
    for test in _scoped_manifest(workspace, scope):
        if test.get("has_durable_result") and scope.get("generation_mode") != "force":
            continue
        if test["kind"] == "datatest":
            units.append(
                UnitSpec(
                    semantic_unit_id("data_test_execution", test["test_id"]),
                    "data_test_execution",
                    f"Execute datatest — {test['title']}",
                    (f"rcm:{test['rcm_id']}", f"datatest:{test['test_id']}"),
                    test,
                )
            )
            continue
        # One expansion, two graphs. A Document Test fans out the same way
        # whether an audit run or a standalone request scheduled it; the only
        # audit-specific parts are the RCM row it covers and the manifest title
        # the auditor sees.
        units.extend(
            document_test_units(
                workspace,
                test["test_id"],
                forced=scope.get("generation_mode") == "force",
                title=test["title"],
                parent_refs=(f"rcm:{test['rcm_id']}",),
            )
        )
    return units


def _fieldwork_executed() -> Capability:
    return Capability(
        "fieldwork.executed",
        "execution",
        "Fieldwork execution",
        "mixed_execution",
        audit_workflow.dependencies("fieldwork.executed"),
        _execution_ready,
        _execution_units,
        # Only the document Q&A unit kind calls the model, so this is the one
        # declaration the capability needs; the other kinds are deterministic
        # local computation with no model-facing context at all.
        context="fieldwork.document_qa",
        invalidate_on=("test", "evidence"),
    )


# --------------------------------------------------------------------------- #
# results.rolled_up (P7G)
# --------------------------------------------------------------------------- #
def _rollup_ready(workspace: Workspace, scope: dict) -> Readiness:
    rcm_rows = _rows(workspace, scope)
    missing = [row["id"] for row in rcm_rows if not row.get("execution_rollup")]
    if missing:
        return Readiness(
            "missing", (f"{len(missing)} RCM row(s) have not been rolled up",)
        )
    return Readiness("satisfied", details={"artifact_count": len(rcm_rows)})


def _results_rolled_up() -> Capability:
    return Capability(
        "results.rolled_up",
        "rollup",
        "Results and observations",
        "rollup",
        audit_workflow.dependencies("results.rolled_up"),
        _rollup_ready,
        _single("rollup", "Roll up RCM results"),
        invalidate_on=("execution",),
    )


_BUILDERS = {
    "fieldwork.executed": _fieldwork_executed,
    "results.rolled_up": _results_rolled_up,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
