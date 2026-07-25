"""Fieldwork capability group of the audit workflow.

Owns the fieldwork and roll-up outcomes of the authoritative audit graph:
``fieldwork.definitions_ready``, ``fieldwork.executed``, and
``results.rolled_up``.

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
    "fieldwork.definitions_ready",
    "fieldwork.executed",
    "results.rolled_up",
)



# --------------------------------------------------------------------------- #
# fieldwork.definitions_ready (P7E)
# --------------------------------------------------------------------------- #
def _definitions_ready(workspace: Workspace, scope: dict) -> Readiness:
    selected = set(_target_rcm_ids(workspace, scope))
    manifest = [
        item
        for item in rcm_execution.execution_manifest(workspace)
        if item["rcm_id"] in selected
    ]
    missing = sum(len(item.get("missing_execution") or []) for item in manifest)
    if not manifest:
        return Readiness("missing", ("no planned tests are available to translate",))
    if missing:
        return Readiness(
            "missing",
            (f"{missing} required execution definition(s) are missing",),
            details={"missing": missing, "total": len(manifest)},
        )
    # Required execution definitions all exist; currency relative to their
    # planned test is not assessed. Parent hashes remain for executor CAS.
    return Readiness("satisfied", details={"total": len(manifest)})


def _definition_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    selected = set(_target_rcm_ids(workspace, scope))
    units = []
    for item in rcm_execution.execution_manifest(workspace):
        if item["rcm_id"] not in selected:
            continue
        missing = set(item.get("missing_execution") or [])
        for kind in item.get("required_execution") or []:
            # Generate only missing definitions, or everything on explicit force.
            # A definition that predates its planned test (currency) is not
            # regenerated automatically; the auditor forces regeneration instead.
            needs = kind in missing or (
                kind == "datatest" and "validation_datatest" in missing
            )
            if not needs and scope.get("generation_mode") != "force":
                continue
            worker_kind = "data_test_spec" if kind == "datatest" else "document_test_spec"
            units.append(
                UnitSpec(
                    semantic_unit_id(worker_kind, item["planned_test_id"]),
                    worker_kind,
                    f"Create {kind} definition — {item['title']}",
                    (f"rcm:{item['rcm_id']}", f"planned_test:{item['planned_test_id']}"),
                    item,
                )
            )
    return units


def _fieldwork_definitions_ready() -> Capability:
    return Capability(
        "fieldwork.definitions_ready",
        "execution_definitions",
        "Execution definitions",
        "execution_spec",
        audit_workflow.dependencies("fieldwork.definitions_ready"),
        _definitions_ready,
        _definition_units,
        context="fieldwork.execution_definitions",
        invalidate_on=("planned_test",),
    )


# --------------------------------------------------------------------------- #
# fieldwork.executed (P7F)
# --------------------------------------------------------------------------- #
def _execution_ready(workspace: Workspace, scope: dict) -> Readiness:
    selected = set(_target_rcm_ids(workspace, scope))
    manifest = [
        item
        for item in rcm_execution.execution_manifest(workspace)
        if item["rcm_id"] in selected
    ]
    pending = []
    review = []
    blocked = []
    for requirement in manifest:
        for artifact in requirement.get("existing_execution") or []:
            # An artifact with a durable result counts as executed. Whether that
            # result predates changed inputs (``result_stale``) is currency, not
            # structural usability, and is not assessed by the framework.
            if not artifact.get("executable"):
                review.append(artifact["id"])
            elif artifact.get("has_durable_result"):
                continue
            elif artifact.get("status") == "blocked":
                blocked.append(artifact["id"])
            elif artifact.get("status") == "review_required" and artifact["kind"] == "doctest":
                review.append(artifact["id"])
            else:
                pending.append(artifact["id"])
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
    selected = set(_target_rcm_ids(workspace, scope))
    units = []
    for item in rcm_execution.execution_manifest(workspace):
        if item["rcm_id"] not in selected:
            continue
        for artifact in item.get("existing_execution") or []:
            if artifact.get("has_durable_result") and scope.get("generation_mode") != "force":
                continue
            if artifact["kind"] == "datatest":
                units.append(
                    UnitSpec(
                        semantic_unit_id("data_test_execution", artifact["id"]),
                        "data_test_execution",
                        f"Execute datatest — {item['title']}",
                        (f"planned_test:{item['planned_test_id']}", f"datatest:{artifact['id']}"),
                        artifact,
                    )
                )
                continue
            # One expansion, two graphs. A Document Test fans out the same way
            # whether an audit run or a standalone request scheduled it; the only
            # audit-specific parts are the planned test it implements and the
            # manifest title the auditor sees.
            units.extend(
                document_test_units(
                    workspace,
                    artifact["id"],
                    forced=scope.get("generation_mode") == "force",
                    title=item["title"],
                    parent_refs=(f"planned_test:{item['planned_test_id']}",),
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
        invalidate_on=("definition", "evidence"),
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
    "fieldwork.definitions_ready": _fieldwork_definitions_ready,
    "fieldwork.executed": _fieldwork_executed,
    "results.rolled_up": _results_rolled_up,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
