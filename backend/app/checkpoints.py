"""Workspace checkpoints: capture the artifact surface, and put it back.

A checkpoint is a restore point for the engagement's *artifacts* — everything a
workflow step can write — taken immediately before a step runs so the Debug
console can roll that step back afterwards.  It is deliberately not a snapshot
of the whole workspace folder:

===================  ==========================================================
Captured             ``workspace.json`` and every artifact family the audit
                     writes: Planning, DocTests, DataTests, DataTestResults,
                     Findings, Observations, Reports, Analyses, Imports, the
                     document schemas and masters, and the derived state under
                     ``Documents/``.
Not captured         ``Data/`` (imported source files — no step rewrites them),
                     ``AgentRuns/`` (the run records the console is reading, so
                     a rollback must not erase the history that offered it),
                     ``AssistantChats/`` (the conversation is not a step's
                     output), and the telemetry database itself.
===================  ==========================================================

**Storage.** Content-addressed.  Each distinct file content is stored once under
``.Checkpoints/blobs/<sha1[:2]>/<sha1>`` and every checkpoint that contains that
content links to the same blob, so the second checkpoint of an engagement costs
only the files that actually changed.  The manifest — path, sha1, size — lives
in the workspace's telemetry database.

**Why hardlinks are safe here.** Every artifact write in this application is a
temp file plus ``os.replace`` (:func:`workspaces.write_json_atomic` and the
journal in :mod:`workspace_transactions`, without exception).  A rewrite
therefore replaces the directory entry and leaves the old inode alone, so a blob
linked from a checkpoint keeps the bytes it was captured with.  If in-place
rewriting were ever introduced, this store would have to copy instead — which is
what :func:`_store_blob` already falls back to when a link cannot be made.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from . import debug_store, telemetry_db
from .workspaces import (
    Workspace,
    WorkspaceError,
    clear_artifact_cache,
    workspace_write_lock,
)

CHECKPOINT_DIRNAME = ".Checkpoints"
BLOBS_DIRNAME = "blobs"
ENV_VAR = "WORKBENCH_CHECKPOINTS"

#: Top-level names never captured, each for its own reason (see the module
#: docstring).  ``Debug`` is the retired telemetry folder; it is inert.
EXCLUDED_TOP_LEVEL = frozenset({
    "Data", "AgentRuns", "AssistantChats", "Debug",
    CHECKPOINT_DIRNAME, ".Transactions",
})

#: The one file captured from the workspace root.  Everything else there is
#: either the telemetry database or a lock/temp artifact.
ROOT_FILE = "workspace.json"


def enabled() -> bool:
    """Whether new checkpoints are written.

    On by default: a rollback offered for a step that was never checkpointed is
    not a feature, and the measured cost of capture is a hash of a few hundred
    small files against a telemetry database an order of magnitude larger.
    Read per call so an operator can turn it off without a restart.
    """
    return str(os.environ.get(ENV_VAR) or "").strip().lower() not in {"0", "off", "false"}


def _checkpoint_root(workspace: Workspace) -> Path:
    return workspace.root / CHECKPOINT_DIRNAME


def _blob_path(workspace: Workspace, digest: str) -> Path:
    return _checkpoint_root(workspace) / BLOBS_DIRNAME / digest[:2] / digest


def _is_temp(name: str) -> bool:
    """Whether a name is one of the atomic-write temp files, mid-rename."""
    return name.startswith(".") and name.endswith(".tmp")


def surface(workspace: Workspace) -> list[Path]:
    """Every file a checkpoint covers, as absolute paths.

    Order is not significant to correctness but is made deterministic so two
    captures of an unchanged workspace produce identical manifests.
    """
    root = workspace.root
    found: list[Path] = []
    definition = root / ROOT_FILE
    if definition.is_file():
        found.append(definition)
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.name in EXCLUDED_TOP_LEVEL:
            continue
        for path in sorted(entry.rglob("*")):
            if path.is_file() and not _is_temp(path.name):
                found.append(path)
    return found


def _sha1(path: Path) -> str | None:
    digest = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _store_blob(workspace: Workspace, path: Path, digest: str) -> int:
    """Link ``path``'s content into the blob store; return the bytes it added.

    Already-stored content costs nothing, which is what makes a per-step
    checkpoint cheap: only the files a step actually rewrote are new.
    """
    target = _blob_path(workspace, digest)
    if target.exists():
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{digest}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        os.link(path, staged)
    except (OSError, NotImplementedError, AttributeError):
        # Cross-device, a filesystem without links, or a platform that refuses
        # them. Correctness does not depend on the link, only the disk saving.
        try:
            shutil.copyfile(path, staged)
        except OSError as error:
            raise WorkspaceError(f"Checkpoint could not store '{path.name}': {error}") from error
    try:
        os.replace(staged, target)
    except OSError:
        Path(staged).unlink(missing_ok=True)
        raise
    try:
        return target.stat().st_size
    except OSError:
        return 0


def capture(
    workspace: Workspace,
    *,
    run_id: str,
    stage_id: str = "",
    capability: str = "",
    label: str = "",
) -> dict | None:
    """Take a checkpoint of the artifact surface. Returns its summary row.

    Returns ``None`` when checkpointing is switched off, so the caller can treat
    "not captured" and "captured nothing" as the same non-event.
    """
    if not enabled():
        return None
    handle = debug_store.connection(workspace.id)
    checkpoint_id = f"ckpt_{uuid.uuid4().hex[:12]}"
    rows: list[tuple[str, str, str, int]] = []
    total_bytes = 0
    new_bytes = 0
    # The lock keeps a concurrent artifact write from being captured half-done.
    # It is the same lock every workspace writer takes, so this cannot interleave
    # with a save in progress.
    with workspace_write_lock(workspace.root):
        for path in surface(workspace):
            digest = _sha1(path)
            if digest is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            new_bytes += _store_blob(workspace, path, digest)
            total_bytes += size
            rows.append((
                checkpoint_id,
                str(path.relative_to(workspace.root)).replace(os.sep, "/"),
                digest,
                size,
            ))
    summary = {
        "id": checkpoint_id,
        "run_id": str(run_id or ""),
        "stage_id": str(stage_id or ""),
        "capability": str(capability or ""),
        "label": str(label or ""),
        "captured_at": debug_store.utcnow(),
        "revision": int(workspace.revision or 0),
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "new_bytes": new_bytes,
        "restored_at": None,
    }
    handle.execute(
        "INSERT INTO checkpoints(id, run_id, stage_id, capability, label,"
        " captured_at, revision, file_count, total_bytes, new_bytes)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            summary["id"], summary["run_id"], summary["stage_id"],
            summary["capability"], summary["label"], summary["captured_at"],
            summary["revision"], summary["file_count"], summary["total_bytes"],
            summary["new_bytes"],
        ),
    )
    handle.executemany(
        "INSERT INTO checkpoint_files(checkpoint_id, path, sha1, size)"
        " VALUES(?, ?, ?, ?)",
        rows,
    )
    handle.commit()
    prune_run(workspace, summary["run_id"])
    debug_store.append_event(workspace.id, "checkpoint_captured", {
        "checkpoint_id": summary["id"], "run_id": summary["run_id"],
        "capability": summary["capability"], "file_count": summary["file_count"],
        "new_bytes": summary["new_bytes"],
    })
    return summary


def _summary_row(row) -> dict:
    return {
        "id": row["id"], "run_id": row["run_id"], "stage_id": row["stage_id"],
        "capability": row["capability"], "label": row["label"],
        "captured_at": row["captured_at"], "revision": row["revision"],
        "file_count": row["file_count"], "total_bytes": row["total_bytes"],
        "new_bytes": row["new_bytes"], "restored_at": row["restored_at"],
    }


def list_for_run(workspace: Workspace, run_id: str) -> list[dict]:
    """A run's checkpoints, oldest first — the order its steps ran in."""
    return [
        _summary_row(row)
        for row in debug_store.connection(workspace.id).execute(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY captured_at, rowid",
            (str(run_id),),
        )
    ]


def get(workspace: Workspace, checkpoint_id: str) -> dict:
    if not str(checkpoint_id).startswith("ckpt_"):
        raise WorkspaceError("Invalid checkpoint reference.")
    row = debug_store.connection(workspace.id).execute(
        "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceError(f"Checkpoint '{checkpoint_id}' not found.")
    return _summary_row(row)


def _manifest(workspace: Workspace, checkpoint_id: str) -> dict[str, tuple[str, int]]:
    return {
        row["path"]: (row["sha1"], int(row["size"] or 0))
        for row in debug_store.connection(workspace.id).execute(
            "SELECT path, sha1, size FROM checkpoint_files WHERE checkpoint_id = ?",
            (checkpoint_id,),
        )
    }


def _matches(workspace: Workspace, relative: str, live: Path, digest: str) -> bool:
    """Whether the live file already holds the checkpoint's content.

    ``workspace.json`` is compared with its ``revision`` set aside. A restore
    deliberately stamps the definition *forward* past every revision the artifact
    cache may hold (see :func:`restore`), so the workspace left behind by a
    successful rollback never byte-matches the checkpoint it was rolled back to.
    Comparing on the hash alone would make that restored state read as one file
    still outstanding, and offer to write a definition differing only in the
    number the rollback was obliged to change.
    """
    if _sha1(live) == digest:
        return True
    if relative != ROOT_FILE:
        return False
    blob = _blob_path(workspace, digest)
    try:
        current = json.loads(live.read_text(encoding="utf-8"))
        captured = json.loads(blob.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    current.pop("revision", None)
    captured.pop("revision", None)
    return current == captured


def plan(workspace: Workspace, checkpoint_id: str) -> dict:
    """What restoring this checkpoint would do, without doing any of it.

    The console shows this before asking for confirmation, because an exact
    restore *removes* files created since the checkpoint — including documents
    an auditor uploaded after the step ran. Naming them is what makes the
    confirmation an informed one rather than a formality.
    """
    checkpoint = get(workspace, checkpoint_id)
    manifest = _manifest(workspace, checkpoint_id)
    if not manifest:
        raise WorkspaceError(
            f"Checkpoint '{checkpoint_id}' has no retained manifest and cannot be restored."
        )
    current = {
        str(path.relative_to(workspace.root)).replace(os.sep, "/"): path
        for path in surface(workspace)
    }
    changed, removed, restored, missing_blobs = [], [], [], []
    for path, (digest, _size) in sorted(manifest.items()):
        live = current.get(path)
        if live is not None and _matches(workspace, path, live, digest):
            continue
        # Only now is the blob's own content worth reading: this is a file the
        # restore would actually write. Verifying rather than trusting the name
        # is what stops a blob damaged by an in-place writer — which nothing in
        # this application is, but the store cannot prove that of a future one —
        # from being written back as if it were the captured content.
        blob = _blob_path(workspace, digest)
        if not blob.is_file() or _sha1(blob) != digest:
            missing_blobs.append(path)
        elif live is None:
            restored.append(path)
        else:
            changed.append(path)
    for path in sorted(current):
        if path not in manifest:
            removed.append(path)
    return {
        "checkpoint": checkpoint,
        "changed": changed,
        "removed": removed,
        "restored": restored,
        "missing_blobs": missing_blobs,
        "unchanged": len(manifest) - len(changed) - len(restored) - len(missing_blobs),
        "restorable": not missing_blobs,
    }


def restore(workspace: Workspace, checkpoint_id: str) -> dict:
    """Put the artifact surface back exactly as the checkpoint recorded it.

    Files that moved are replaced through the same atomic rename every other
    writer uses, files created since are deleted, and files deleted since are
    written back. The manifest's own ``workspace.json`` carries the revision the
    workspace had at capture; restoring it verbatim would let a parsed-artifact
    cache entry keyed on that revision answer with post-rollback content, so the
    definition is re-stamped forward to a revision nothing has cached and the
    cache for this root is dropped outright.
    """
    root = workspace.root
    written, deleted = 0, 0
    # The plan is surveyed inside the lock that acts on it. Deciding what to
    # write and then writing it under two separate acquisitions would let a
    # save land in between, and the rollback would apply a plan describing a
    # workspace that had already moved.
    with workspace_write_lock(root):
        outcome = plan(workspace, checkpoint_id)
        if not outcome["restorable"]:
            raise WorkspaceError(
                f"{len(outcome['missing_blobs'])} of this checkpoint's files are no longer"
                " retrievable, so the step cannot be rolled back. Restoring the rest would"
                " leave the engagement in a state that never existed."
            )
        manifest = _manifest(workspace, checkpoint_id)
        live_revision = _current_revision(workspace)
        for path in [*outcome["changed"], *outcome["restored"]]:
            digest, _size = manifest[path]
            _materialize(root / path, _blob_path(workspace, digest))
            written += 1
        for path in outcome["removed"]:
            try:
                (root / path).unlink()
                deleted += 1
            except OSError as error:
                raise WorkspaceError(f"Could not remove '{path}': {error}") from error
        _prune_empty_dirs(root)
        restored_revision = _restamp_revision(
            root, max(live_revision, int(outcome["checkpoint"]["revision"] or 0)) + 1
        )
    clear_artifact_cache(root)
    debug_store.connection(workspace.id).execute(
        "UPDATE checkpoints SET restored_at = ? WHERE id = ?",
        (debug_store.utcnow(), checkpoint_id),
    )
    debug_store.connection(workspace.id).commit()
    debug_store.append_event(workspace.id, "checkpoint_restored", {
        "checkpoint_id": checkpoint_id,
        "run_id": outcome["checkpoint"]["run_id"],
        "capability": outcome["checkpoint"]["capability"],
        "files_written": written, "files_deleted": deleted,
        "revision": restored_revision,
    })
    return {
        "checkpoint_id": checkpoint_id,
        "files_written": written,
        "files_deleted": deleted,
        "revision": restored_revision,
        "capability": outcome["checkpoint"]["capability"],
    }


def _materialize(target: Path, blob: Path) -> None:
    """Place blob content at ``target`` through a temp file and one rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        try:
            os.link(blob, staged)
        except (OSError, NotImplementedError, AttributeError):
            shutil.copyfile(blob, staged)
        os.replace(staged, target)
    except OSError as error:
        Path(staged).unlink(missing_ok=True)
        raise WorkspaceError(f"Could not restore '{target.name}': {error}") from error


def _current_revision(workspace: Workspace) -> int:
    try:
        stored = json.loads((workspace.root / ROOT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return int(workspace.revision or 0)
    return int(stored.get("revision") or 0)


def _restamp_revision(root: Path, revision: int) -> int:
    """Advance the restored definition past every revision anything has cached."""
    path = root / ROOT_FILE
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Restored workspace definition is unreadable: {error}") from error
    definition["revision"] = int(revision)
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    staged.write_text(json.dumps(definition, indent=2), encoding="utf-8")
    os.replace(staged, path)
    return int(revision)


def _prune_empty_dirs(root: Path) -> None:
    """Remove directories a restore emptied, leaving the captured families."""
    for name in sorted(entry.name for entry in root.iterdir() if entry.is_dir()):
        if name in EXCLUDED_TOP_LEVEL:
            continue
        top = root / name
        for path in sorted(top.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()


# ------------------------------------------------------------------ retention
def prune_run(workspace: Workspace, run_id: str) -> int:
    """Drop a run's oldest checkpoints past the cap, then sweep unused blobs."""
    handle = debug_store.connection(workspace.id)
    stale = [
        row["id"]
        for row in handle.execute(
            "SELECT id FROM checkpoints WHERE run_id = ? AND id NOT IN ("
            " SELECT id FROM checkpoints WHERE run_id = ?"
            # Timestamps are millisecond-resolution and stages can settle inside
            # one, so ``captured_at`` alone leaves ties for SQLite to break as it
            # likes — retention would then evict an arbitrary member of the tie
            # rather than the oldest. ``rowid`` is the insertion order.
            " ORDER BY captured_at DESC, rowid DESC LIMIT ?)",
            (str(run_id), str(run_id), telemetry_db.MAX_CHECKPOINTS_PER_RUN),
        )
    ]
    if stale:
        forget(workspace, stale)
    return len(stale)


def forget(workspace: Workspace, checkpoint_ids: list[str]) -> None:
    """Remove manifests and every blob no surviving manifest still references."""
    if not checkpoint_ids:
        return
    handle = debug_store.connection(workspace.id)
    marks = ",".join("?" * len(checkpoint_ids))
    orphans = [
        row["sha1"]
        for row in handle.execute(
            f"SELECT DISTINCT sha1 FROM checkpoint_files WHERE checkpoint_id IN ({marks})",  # noqa: S608 - bound marks
            checkpoint_ids,
        )
    ]
    handle.execute(
        f"DELETE FROM checkpoint_files WHERE checkpoint_id IN ({marks})",  # noqa: S608
        checkpoint_ids,
    )
    handle.execute(
        f"DELETE FROM checkpoints WHERE id IN ({marks})", checkpoint_ids  # noqa: S608
    )
    handle.commit()
    for digest in orphans:
        still_used = handle.execute(
            "SELECT 1 FROM checkpoint_files WHERE sha1 = ? LIMIT 1", (digest,)
        ).fetchone()
        if still_used is None:
            _blob_path(workspace, digest).unlink(missing_ok=True)


def clear(workspace: Workspace) -> None:
    """Drop every checkpoint and the whole blob store for one workspace."""
    handle = debug_store.connection(workspace.id)
    handle.execute("DELETE FROM checkpoint_files")
    handle.execute("DELETE FROM checkpoints")
    handle.commit()
    shutil.rmtree(_checkpoint_root(workspace), ignore_errors=True)


def usage(workspace: Workspace) -> dict:
    """What the checkpoint store currently costs, for the console to report."""
    handle = debug_store.connection(workspace.id)
    count = handle.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    blobs = _checkpoint_root(workspace) / BLOBS_DIRNAME
    stored = 0
    if blobs.is_dir():
        for path in blobs.rglob("*"):
            if path.is_file():
                try:
                    stored += path.stat().st_size
                except OSError:
                    continue
    return {"checkpoints": int(count), "blob_bytes": stored, "enabled": enabled()}
