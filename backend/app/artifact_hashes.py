"""Material artifact projections and the parent hashes computed from them.

One artifact ref, one identity. Executors guard their commits on these hashes,
receipts prove their postconditions with them, and capability readiness asks
whether an artifact's stamped parents still hash to the same thing — which only
agrees by construction because all three call the same projection.

It lives apart from :mod:`workspace_transactions` for exactly that reason: the
hashes are a read-only reading of the workspace, and the layers that may not
touch the transaction subsystem — the declarative capability layer above all —
must still be able to ask what an artifact hashes to today. Nothing here
writes, locks, or journals.
"""

from __future__ import annotations

import hashlib
import json

from .workspaces import Workspace, WorkspaceError


def canonical_sha1(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def material_projection(value: object) -> object:
    """Remove volatile timestamps before artifact-parent hashing."""
    if isinstance(value, dict):
        return {
            key: material_projection(item)
            for key, item in value.items()
            if key not in {"updated", "updated_at", "generated_at", "completed_at"}
        }
    if isinstance(value, list):
        return [material_projection(item) for item in value]
    return value


# ``review_status`` is deliberately absent. Sign-off is an annotation the
# auditor makes *about* a row, not part of what the row asserts, and hashing it
# made marking a row reviewed read as "the evidence changed" — conflicting an
# in-flight commit against that row and staling any finding anchored to it.
_RCM_MATERIAL_FIELDS = (
    "id", "process", "risk", "risk_rating", "business_cycle",
    "control_attributes", "control",
    "control_type", "control_owner", "criteria", "criteria_refs",
    "evidence_refs",
)


def rcm_material_projection(row: dict | None) -> dict | None:
    """Return the auditor-editable basis of an RCM row.

    Links to generated tests, execution rollups, and finding links are derived
    workflow output.  They must not invalidate work that was generated from the
    row's risk-and-control definition.
    """
    if row is None:
        return None
    return {field: row.get(field) for field in _RCM_MATERIAL_FIELDS}


# A Data Test's marking is derived from its runs and its rulings, and is
# re-projected on every read. Hashing it would make reading a test change its
# own parent hash, and would let recording a disposition abort a run already in
# flight against the definition it was recorded on.
_DATATEST_DERIVED_FIELDS = frozenset({
    "status",
    "evaluation",
    "exception_dispositions",
    "semantic_review",
    "conclusion_source",
    "control_conclusion_source",
    "control_conclusion_input_sha1",
    "control_conclusion_stale",
    "open_exception_count",
})


def datatest_material_projection(item: dict | None) -> dict | None:
    """Return the basis of a Data Test, without what its runs derived from it."""
    if item is None:
        return None
    return {
        key: value
        for key, value in item.items()
        if key not in _DATATEST_DERIVED_FIELDS
    }


def artifact_projection(workspace: Workspace, ref: str) -> object:
    kind, separator, item_id = str(ref).partition(":")
    if not separator:
        raise WorkspaceError(f"Invalid parent reference '{ref}'.")
    if kind == "planning":
        if item_id == "context":
            return workspace.planning.get("context") or {}
        if item_id == "apm":
            return {
                "markdown": workspace.planning.get("apm_markdown") or "",
                "created_by": workspace.planning.get("created_by"),
                "updated": workspace.planning.get("updated"),
            }
        if item_id == "cycle":
            # The shape a matrix row's ``process`` and the ruleset's roles are
            # chosen from — what the artifact asserts, without who wrote it or
            # when. A cycle reshaped under an in-flight matrix must conflict
            # that commit; an auditor re-saving the same steps must not.
            cycle = workspace.planning.get("cycle") or {}
            return material_projection(
                {
                    key: cycle.get(key)
                    for key in ("name", "steps", "cross_cutting")
                }
                if cycle
                else None
            )
    # Data-workbench artifacts. The exploratory analysis workflow guards its
    # commits on the tables it read, the joins it materialized, and the analysis
    # definitions it executed, so those need material parent projections too.
    # A table's projection is its durable entry plus the loader signature, so a
    # replaced or re-imported source is a parent change rather than a silent
    # basis swap.
    if kind == "table":
        entry = next(
            (item for item in workspace.tables if item.get("name") == item_id), None
        )
        if entry is None:
            return None
        try:
            signature = list(workspace._table_signature(item_id))
        except Exception as error:
            signature = [type(error).__name__, str(error)]
        return material_projection({"table": entry, "signature": signature})
    if kind == "join":
        return material_projection(
            next((item for item in workspace.joins if item.get("name") == item_id), None)
        )
    if kind == "analysis":
        return material_projection(
            next((item for item in workspace.analyses if item.get("id") == item_id), None)
        )
    if kind == "rcm":
        return rcm_material_projection(
            next((item for item in workspace.rcm if item.get("id") == item_id), None)
        )
    if kind == "document":
        # The document-analysis workflow guards its commits on the document
        # entry, so a replaced source or a re-extraction is a parent change
        # rather than a silent basis swap under a running analysis.
        return material_projection(
            next((item for item in workspace.documents if item.get("id") == item_id), None)
        )
    if kind == "document_analysis":
        # A generated analysis lives in its own sidecar, so the postcondition a
        # receipt proves is that sidecar's material content, not a workspace
        # collection entry. The import is local because ``document_analysis``
        # depends on ``workspaces`` rather than the other way round.
        from .document_analysis import generated_projection

        return material_projection(generated_projection(workspace, item_id))
    if kind == "datatest":
        return datatest_material_projection(
            next((item for item in workspace.data_tests if item.get("id") == item_id), None)
        )
    if kind == "doctest":
        # Document tests live in their own sidecars, so the projection is the
        # summary the workspace itself indexes; the import is local because
        # ``doc_tests`` depends on this module for its linked writes.
        from .doc_tests import list_tests

        return next(
            (item for item in list_tests(workspace) if item.get("id") == item_id),
            None,
        )
    if kind == "observation":
        return next((item for item in workspace.observations if item.get("id") == item_id), None)
    if kind == "finding":
        return next((item for item in workspace.findings if item.get("id") == item_id), None)
    if kind == "report":
        return workspace.report
    if kind == "analysis_summary":
        # The EDA memo is a single derived artifact, so the only reference it
        # takes is ``analysis_summary:current``. Its material projection
        # deliberately excludes the generation stamp: what makes a memo the same
        # memo is its prose and the result basis it was written against, not
        # which run produced it.
        if item_id == "current":
            summary = workspace.analysis_summary or {}
            return material_projection(
                {
                    "markdown": summary.get("markdown") or "",
                    "basis_sha1": summary.get("basis_sha1") or "",
                }
            )
    raise WorkspaceError(f"Unsupported parent reference '{ref}'.")


def parent_hashes(workspace: Workspace, refs: list[str] | tuple[str, ...]) -> dict[str, str]:
    return {
        ref: canonical_sha1(material_projection(artifact_projection(workspace, ref)))
        for ref in refs
    }
