"""Workspace layout and the registry that indexes it.

This module owns two things that must agree: *where* a workspace lives on disk,
and the control-plane row that maps its globally unique ``uid`` to that
location.  Every path under the data root is derived here, so the resolver in
:mod:`.workspaces` is the only way a workspace ID becomes a filesystem root.

    <DATA_ROOT>/Users/<owner_id>/Workspaces/<dir_name>/workspace.json

Identity and location are deliberately separate.  ``uid`` is globally unique,
opaque, appears in URLs, and never changes — it is what sharing will key on.
``dir_name`` is a readable slug, unique only within one owner's home, and names
the directory.  ``name`` is free display text and changes whenever the auditor
likes.

The registry is an index, not the source of truth: every manifest carries its
own ``uid`` and ``owner_id``, so :func:`reconcile` can rebuild the table by
walking the disk.  That keeps SQLite from becoming a single point of
catastrophic loss for audit content, and it is what reduces migrating existing
workspaces to moving folders.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

WORKSPACES_DIRNAME = "Workspaces"
USERS_DIRNAME = "Users"

# The only shape a directory name or a URL-supplied reference may take.  This
# is what stops a reference like ``../bob/engagement`` from ever reaching a
# path join.
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RegistryError(ValueError):
    """A user-facing problem with a workspace reference or its registration."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_workspace_uid() -> str:
    """Globally unique, with a time prefix that makes ids roughly age-ordered.

    Only *roughly*: two workspaces created inside the same millisecond fall back
    to their random suffix, so nothing may rely on uid order.  Listings order by
    ``created_at`` instead, which is exact.  The prefix earns its place by making
    a uid legible when reading a folder or a log.
    """
    return f"ws_{int(time.time() * 1000):011x}{secrets.token_hex(6)}"


# ----------------------------------------------------------------- filesystem
def users_dir() -> Path:
    return config.data_root() / USERS_DIRNAME


def user_home(owner_id: str) -> Path:
    if not SAFE_REF.match(str(owner_id or "")):
        raise RegistryError("Invalid user identifier.")
    return users_dir() / str(owner_id)


def user_workspaces_dir(owner_id: str) -> Path:
    return user_home(owner_id) / WORKSPACES_DIRNAME


def workspace_root(owner_id: str, dir_name: str) -> Path:
    """The root for one workspace, proven to stay inside its owner's home."""
    if not SAFE_REF.match(str(dir_name or "")):
        raise RegistryError(f"Invalid workspace reference '{dir_name}'.")
    home = user_workspaces_dir(owner_id)
    root = (home / str(dir_name)).resolve()
    # Defense in depth.  ``SAFE_REF`` already excludes separators and dots, so
    # this cannot currently fail — it is here so that a future relaxation of the
    # pattern cannot silently turn into a tenant escape.
    if not root.is_relative_to(home.resolve()):
        raise RegistryError(f"Invalid workspace reference '{dir_name}'.")
    return root


# ------------------------------------------------------------------- registry
def _project(row) -> dict:
    return {
        "uid": row["uid"],
        "owner_id": row["owner_id"],
        "dir_name": row["dir_name"],
        "name": row["name"],
        "legacy_slug": row["legacy_slug"],
        "created_at": row["created_at"],
    }


def register(owner_id: str, uid: str, dir_name: str, name: str = "",
             legacy_slug: str | None = None, created_at: str | None = None) -> dict:
    db.execute(
        "INSERT INTO workspaces (uid, owner_id, dir_name, name, legacy_slug, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(uid) DO UPDATE SET"
        "   owner_id=excluded.owner_id, dir_name=excluded.dir_name,"
        "   name=excluded.name, legacy_slug=excluded.legacy_slug, deleted_at=NULL",
        (str(uid), str(owner_id), str(dir_name), str(name or ""),
         legacy_slug, created_at or utcnow()),
    )
    return require(uid)


def rename(uid: str, name: str) -> None:
    db.execute("UPDATE workspaces SET name = ? WHERE uid = ?", (str(name or ""), str(uid)))


def unregister(uid: str) -> None:
    db.execute("DELETE FROM workspaces WHERE uid = ?", (str(uid),))


def find(uid: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM workspaces WHERE uid = ? AND deleted_at IS NULL", (str(uid),)
    )
    return _project(row) if row is not None else None


def require(uid: str) -> dict:
    row = find(uid)
    if row is None:
        raise RegistryError(f"Workspace '{uid}' not found.")
    return row


def list_for_owner(owner_id: str) -> list[dict]:
    return [
        _project(row)
        for row in db.query(
            "SELECT * FROM workspaces WHERE owner_id = ? AND deleted_at IS NULL"
            " ORDER BY created_at, dir_name",
            (str(owner_id),),
        )
    ]


def list_visible(user_id: str) -> list[dict]:
    """Everything this user may open: owned, plus anything shared with them.

    The membership join is empty today.  It is written now so that turning on
    sharing is an INSERT rather than a change to every listing and resolution
    path.
    """
    return [
        _project(row)
        for row in db.query(
            "SELECT w.* FROM workspaces w"
            " LEFT JOIN workspace_members m"
            "   ON m.workspace_uid = w.uid AND m.user_id = ?"
            " WHERE w.deleted_at IS NULL AND (w.owner_id = ? OR m.user_id IS NOT NULL)"
            " ORDER BY w.created_at, w.dir_name",
            (str(user_id), str(user_id)),
        )
    ]


def resolve_ref(owner_id: str, ref: str) -> dict | None:
    """Find a workspace by uid, then by the pre-migration slug, then by folder.

    The fallbacks are what keep links made before the migration working: those
    URLs carry the old slug, which is recorded as ``legacy_slug``.
    """
    reference = str(ref or "")
    if not SAFE_REF.match(reference):
        return None
    found = find(reference)
    if found is not None:
        return found
    row = db.query_one(
        "SELECT * FROM workspaces WHERE owner_id = ? AND deleted_at IS NULL"
        "  AND (legacy_slug = ? OR dir_name = ?) ORDER BY created_at LIMIT 1",
        (str(owner_id), reference, reference),
    )
    return _project(row) if row is not None else None


def locate(ref: str) -> dict | None:
    """Find a workspace by any of its references, ignoring ownership.

    For internal callers that have *already* been authorized upstream and only
    need to get back to a root — debug telemetry is the case that exists.  This
    performs no access check and must never back a user-facing route; those go
    through ``workspaces.open_workspace``.
    """
    reference = str(ref or "")
    if not SAFE_REF.match(reference):
        return None
    found = find(reference)
    if found is not None:
        return found
    row = db.query_one(
        "SELECT * FROM workspaces WHERE deleted_at IS NULL"
        "  AND (legacy_slug = ? OR dir_name = ?) ORDER BY created_at LIMIT 1",
        (reference, reference),
    )
    return _project(row) if row is not None else None


def accessible(principal, row: dict) -> bool:
    """Owner today; the membership table is consulted so sharing is a data change."""
    if row["owner_id"] == principal.user_id:
        return True
    member = db.query_one(
        "SELECT 1 FROM workspace_members WHERE workspace_uid = ? AND user_id = ?",
        (row["uid"], principal.user_id),
    )
    return member is not None


# ------------------------------------------------------------------ reconcile
def _read_manifest(path: Path) -> dict | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _stamp(path: Path, definition: dict, updates: dict) -> dict:
    from .workspaces import write_json_atomic

    stamped = {**definition, **updates}
    write_json_atomic(path, stamped)
    return stamped


_reconciled: set[str] = set()
_reconcile_guard = threading.Lock()


def ensure_reconciled() -> None:
    """Run :func:`reconcile` once per data root per process.

    The app factory is evaluated at import (``app = create_app()``), which is
    far too early to touch storage — a test suite has not repointed the data
    root yet, and an import should not create files.  Reconciliation is instead
    triggered by the first request, where the data root is settled.
    """
    key = str(config.data_root())
    if key in _reconciled:
        return
    with _reconcile_guard:
        if key in _reconciled:
            return
        reconcile()
        _reconciled.add(key)


def forget_reconciled() -> None:
    """Drop the once-per-root memo (tests repoint the data root)."""
    with _reconcile_guard:
        _reconciled.clear()


def reconcile() -> dict:
    """Rebuild the registry from disk, stamping identity into any manifest missing it.

    Idempotent: a manifest that already carries a ``uid`` is left alone.  This
    is both the disaster-recovery path for a lost database and the one-time
    migration step for workspaces moved into an owner's home by hand — start
    the app and the folders register themselves.
    """
    root = users_dir()
    summary = {"scanned": 0, "stamped": 0, "registered": 0}
    if not root.exists():
        return summary
    known = {row["uid"] for row in db.query("SELECT uid FROM workspaces")}
    for home in sorted(root.iterdir()):
        if not home.is_dir() or not SAFE_REF.match(home.name):
            continue
        owner_id = home.name
        if db.query_one("SELECT 1 FROM users WHERE id = ?", (owner_id,)) is None:
            # A home with no account: leave it untouched rather than attaching
            # it to the wrong owner. An admin restores the account, restarts,
            # and this pass picks it up.
            continue
        for folder in sorted((home / WORKSPACES_DIRNAME).glob("*")):
            manifest = folder / "workspace.json"
            if not folder.is_dir() or not manifest.is_file():
                continue
            definition = _read_manifest(manifest)
            if definition is None:
                continue
            summary["scanned"] += 1
            uid = str(definition.get("uid") or "")
            if not uid:
                uid = new_workspace_uid()
                legacy = str(definition.get("id") or folder.name)
                definition = _stamp(manifest, definition, {
                    # ``id`` is set too, so the manifest is self-consistent the
                    # moment it is stamped rather than only after the next save.
                    # The slug it used to hold survives as ``legacy_slug``,
                    # which is what keeps pre-migration links resolving.
                    "id": uid, "uid": uid,
                    "owner_id": owner_id, "legacy_slug": legacy,
                })
                summary["stamped"] += 1
            if uid not in known:
                register(
                    owner_id, uid, folder.name,
                    name=str(definition.get("name") or folder.name),
                    legacy_slug=definition.get("legacy_slug"),
                    created_at=str(definition.get("created") or "") or None,
                )
                known.add(uid)
                summary["registered"] += 1
    return summary
