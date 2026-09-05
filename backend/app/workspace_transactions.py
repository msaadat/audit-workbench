"""Revision-safe, narrow workspace transactions shared by routes and runs.

The desktop server normally has one process, but correctness does not depend on
one long-lived ``Workspace`` instance.  A transaction locks the workspace,
reloads the authoritative definition, checks the caller's revision and material
parent hashes, applies a narrow callback, and returns the fresh committed view.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from .workspaces import (
    Workspace,
    WorkspaceConflict,
    WorkspaceError,
    clear_artifact_cache,
    load_workspace,
    workspace_write_lock,
    write_json_atomic,
)

T = TypeVar("T")
JOURNAL_DIRNAME = ".Transactions"


def canonical_sha1(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TransactionResult:
    value: Any
    workspace: Workspace
    revision: int


@dataclass(frozen=True)
class LinkedWrite:
    journal_path: Path
    target_path: Path


@dataclass(frozen=True)
class ArtifactWrite:
    journal_path: Path


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def prepare_artifact_writes(
    workspace: Workspace,
    writes: list[tuple[Path, str, object]],
) -> ArtifactWrite:
    """Journal a group of JSON/text sidecars for one workspace revision.

    The manifest revision is the commit marker. If the process stops before it
    advances, recovery restores all recorded preimages; if it advanced, the
    sidecars are the committed state and only the journal is discarded.
    """
    root = workspace.root.resolve()
    entries = []
    for target, kind, _ in writes:
        target = target.resolve()
        if root not in target.parents or kind not in {"json", "text", "delete"}:
            raise WorkspaceError("Linked transaction target is invalid.")
        restore_kind = "json" if kind in {"json", "delete"} else "text"
        before_exists = target.is_file()
        before: object = None
        if before_exists:
            try:
                before = (
                    json.loads(target.read_text(encoding="utf-8"))
                    if restore_kind == "json"
                    else target.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise WorkspaceError(f"Linked artifact '{target.name}' is unreadable.") from error
        entries.append({
            "target": str(target.relative_to(root)), "kind": restore_kind,
            "before_exists": before_exists, "before": before,
        })
    journal_dir = root / JOURNAL_DIRNAME
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / f"txn-{uuid.uuid4().hex}.json"
    write_json_atomic(journal_path, {
        "state": "prepared", "expected_revision": workspace.revision,
        "targets": entries,
    })
    return ArtifactWrite(journal_path)


def apply_artifact_writes(writes: list[tuple[Path, str, object]]) -> None:
    for target, kind, value in writes:
        if kind == "delete":
            target.unlink(missing_ok=True)
        elif kind == "json":
            write_json_atomic(target, value)
        else:
            _write_text_atomic(target, str(value))


def rollback_artifact_writes(root: Path, write: ArtifactWrite) -> None:
    try:
        journal = json.loads(write.journal_path.read_text(encoding="utf-8"))
        for entry in reversed(journal.get("targets") or []):
            target = (root / str(entry.get("target") or "")).resolve()
            if root not in target.parents:
                continue
            if entry.get("before_exists"):
                if entry.get("kind") == "json":
                    write_json_atomic(target, entry.get("before") or {})
                else:
                    _write_text_atomic(target, str(entry.get("before") or ""))
            else:
                target.unlink(missing_ok=True)
    finally:
        write.journal_path.unlink(missing_ok=True)


def complete_artifact_writes(write: ArtifactWrite) -> None:
    write.journal_path.unlink(missing_ok=True)


def prepare_linked_write(workspace: Workspace, target: Path, payload: dict) -> LinkedWrite:
    """Journal a JSON sidecar write that must share a workspace revision."""
    target = target.resolve()
    root = workspace.root.resolve()
    if root not in target.parents:
        raise WorkspaceError("Linked transaction target must be inside the workspace.")
    before = None
    before_exists = target.is_file()
    if before_exists:
        try:
            before = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError(f"Linked artifact '{target.name}' is unreadable.") from error
    journal_dir = root / JOURNAL_DIRNAME
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / f"txn-{uuid.uuid4().hex}.json"
    write_json_atomic(
        journal_path,
        {
            "state": "prepared",
            "expected_revision": workspace.revision,
            "target": str(target.relative_to(root)),
            "before_exists": before_exists,
            "before": before,
            "after_sha1": canonical_sha1(payload),
        },
    )
    return LinkedWrite(journal_path, target)


def complete_linked_write(write: LinkedWrite) -> None:
    write.journal_path.unlink(missing_ok=True)


def rollback_linked_write(write: LinkedWrite) -> None:
    try:
        journal = json.loads(write.journal_path.read_text(encoding="utf-8"))
        if journal.get("before_exists"):
            write_json_atomic(write.target_path, journal.get("before") or {})
        else:
            write.target_path.unlink(missing_ok=True)
    finally:
        write.journal_path.unlink(missing_ok=True)


def recover_linked_writes(root: Path) -> None:
    """Resolve prepared linked writes left by an interrupted desktop process.

    This is the one path that restores artifact files without advancing the
    workspace revision, so anything it resolves has to invalidate the parsed
    artifacts cached against that revision.
    """
    root = root.resolve()
    with workspace_write_lock(root):
        journal_dir = root / JOURNAL_DIRNAME
        if not journal_dir.is_dir():
            return
        if any(journal_dir.glob("txn-*.json")):
            clear_artifact_cache(root)
        try:
            current_revision = int(
                json.loads((root / "workspace.json").read_text(encoding="utf-8")).get("revision")
                or 0
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        for journal_path in sorted(journal_dir.glob("txn-*.json")):
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                if journal.get("targets") is not None:
                    write = ArtifactWrite(journal_path)
                    if current_revision <= int(journal.get("expected_revision") or 0):
                        rollback_artifact_writes(root, write)
                    else:
                        complete_artifact_writes(write)
                    continue
                target = (root / str(journal.get("target") or "")).resolve()
                if root not in target.parents:
                    journal_path.unlink(missing_ok=True)
                    continue
                if current_revision <= int(journal.get("expected_revision") or 0):
                    rollback_linked_write(LinkedWrite(journal_path, target))
                else:
                    # The workspace revision advanced after the prepared write;
                    # the linked mutation committed before the process stopped.
                    journal_path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                journal_path.unlink(missing_ok=True)


class ParentConflict(WorkspaceConflict):
    """The workspace revision may be current but a material parent changed."""

    def __init__(self, parent_ref: str, expected_sha1: str, current_sha1: str, revision: int):
        self.parent_ref = parent_ref
        self.expected_sha1 = expected_sha1
        self.current_sha1 = current_sha1
        super().__init__(revision, revision)
        self.args = (
            f"The parent artifact '{parent_ref}' changed while this operation was in "
            "progress. Reload and reconcile the affected workflow unit.",
        )


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


def mutate(
    workspace_or_id: Workspace | str,
    callback: Callable[[Workspace], T],
    *,
    expected_revision: int | None = None,
    expected_parents: Mapping[str, str] | None = None,
) -> TransactionResult:
    """Apply ``callback`` to a freshly loaded workspace under its write lock.

    Workspace methods currently persist their own narrow changes.  The lock is
    re-entrant, so those saves remain safe.  If a callback only mutates memory,
    this helper performs the final save.  Callers therefore can migrate to the
    coordinator incrementally without two storage paths.
    """
    seed = workspace_or_id if isinstance(workspace_or_id, Workspace) else load_workspace(workspace_or_id)
    with workspace_write_lock(seed.root):
        fresh = Workspace(seed.root)
        if (
            expected_revision is not None
            and fresh.revision != int(expected_revision)
            and not expected_parents
        ):
            raise WorkspaceConflict(int(expected_revision), fresh.revision)
        for ref, expected in (expected_parents or {}).items():
            current = canonical_sha1(material_projection(artifact_projection(fresh, ref)))
            if current != expected:
                raise ParentConflict(ref, expected, current, fresh.revision)
        before = fresh.revision
        value = callback(fresh)
        if fresh.revision == before:
            fresh.save(expected_revision=before)
        return TransactionResult(value=value, workspace=fresh, revision=fresh.revision)


def revision_payload(workspace: Workspace) -> dict[str, int]:
    return {"workspace_revision": int(workspace.revision)}
