"""SQLite control plane: connections, pragmas, and schema migrations.

The control plane owns *identity, authorization, and indexes*: users, sessions,
the workspace registry, invites, and usage/auth accounting.  It deliberately
does **not** own audit content.  Workspace artifacts stay as independently
replaceable files under the data root, because that is what gives them atomic
writes, revisioning, interrupted-write recovery, and portability without the
app.  Nothing in this module should ever grow a table of engagement data.

The database is a single file beside the data root.  If the data root is ever
moved to a network share, point ``WORKBENCH_DB`` at local disk instead: SQLite
locking over NFS/SMB is not reliable.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from . import config  # noqa: F401  # load .env before reading WORKBENCH_DB

# One connection per (thread, database path).  ``sqlite3`` connections are not
# safe to share across threads, and this application runs one daemon thread per
# agent run, each of which may write usage rows.
_local = threading.local()
_migration_guard = threading.Lock()


def database_path() -> Path:
    override = str(os.environ.get("WORKBENCH_DB", "")).strip()
    return Path(override) if override else config.data_root() / "workbench.db"


# Ordered schema migrations.  The applied version is tracked in SQLite's own
# ``user_version`` pragma, so there is no bookkeeping table and no dependency
# on a migration framework for a schema this small.
MIGRATIONS: list[str] = [
    # -- 1 ---------------------------------------------------------------
    """
    CREATE TABLE users (
        id            TEXT PRIMARY KEY,
        email         TEXT NOT NULL UNIQUE,
        display_name  TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL DEFAULT '',
        is_admin      INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL DEFAULT 'active',
        created_at    TEXT NOT NULL
    );

    CREATE TABLE sessions (
        token_hash   TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );
    CREATE INDEX sessions_user ON sessions(user_id);

    -- ``uid`` is the globally unique workspace identity that appears in URLs
    -- and will key sharing.  ``dir_name`` is where it lives inside its owner's
    -- home and is unique only per owner; ``legacy_slug`` keeps pre-migration
    -- links resolving.
    CREATE TABLE workspaces (
        uid         TEXT PRIMARY KEY,
        owner_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        dir_name    TEXT NOT NULL,
        name        TEXT NOT NULL DEFAULT '',
        legacy_slug TEXT,
        created_at  TEXT NOT NULL,
        deleted_at  TEXT,
        UNIQUE (owner_id, dir_name)
    );
    CREATE INDEX workspaces_owner ON workspaces(owner_id);
    CREATE INDEX workspaces_legacy ON workspaces(owner_id, legacy_slug);

    -- Created now and left empty on purpose.  The resolver consults it from
    -- day one with the owner as an implicit member, so enabling sharing later
    -- is a data change rather than a schema migration plus a resolver rewrite.
    CREATE TABLE workspace_members (
        workspace_uid TEXT NOT NULL REFERENCES workspaces(uid) ON DELETE CASCADE,
        user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role          TEXT NOT NULL DEFAULT 'member',
        created_at    TEXT NOT NULL,
        PRIMARY KEY (workspace_uid, user_id)
    );

    CREATE TABLE invites (
        token_hash  TEXT PRIMARY KEY,
        email       TEXT NOT NULL,
        created_by  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        accepted_at TEXT
    );

    -- One shared admin-owned provider key across many users makes per-user
    -- attribution the only way to answer who consumed the budget.
    CREATE TABLE llm_usage (
        id                TEXT PRIMARY KEY,
        user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        workspace_uid     TEXT,
        run_id            TEXT,
        provider          TEXT NOT NULL DEFAULT '',
        model             TEXT NOT NULL DEFAULT '',
        turns             INTEGER NOT NULL DEFAULT 0,
        prompt_tokens     INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        at                TEXT NOT NULL
    );
    CREATE INDEX llm_usage_user ON llm_usage(user_id, at);

    CREATE TABLE auth_events (
        id      TEXT PRIMARY KEY,
        user_id TEXT,
        email   TEXT NOT NULL DEFAULT '',
        event   TEXT NOT NULL,
        detail  TEXT NOT NULL DEFAULT '',
        at      TEXT NOT NULL
    );
    CREATE INDEX auth_events_at ON auth_events(at);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    # Off by default in SQLite; the registry's owner references depend on it.
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")


def migrate(connection: sqlite3.Connection) -> int:
    """Apply outstanding migrations. Idempotent, and safe to call on every open."""
    with _migration_guard:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        for version in range(current, len(MIGRATIONS)):
            connection.executescript(MIGRATIONS[version])
            # Pragmas do not accept bound parameters; the value is a loop index.
            connection.execute(f"PRAGMA user_version={version + 1}")
            connection.commit()
        return len(MIGRATIONS)


def connect() -> sqlite3.Connection:
    """The calling thread's connection for the current data root."""
    path = database_path()
    cache = getattr(_local, "connections", None)
    if cache is None:
        cache = _local.connections = {}
    key = str(path)
    connection = cache.get(key)
    if connection is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(key, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        _configure(connection)
        migrate(connection)
        cache[key] = connection
    return connection


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(connect().execute(sql, params).fetchall())


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    connection = connect()
    cursor = connection.execute(sql, params)
    connection.commit()
    return cursor


def close_all() -> None:
    """Drop this thread's cached connections (tests repoint the data root)."""
    cache = getattr(_local, "connections", None)
    if not cache:
        return
    for connection in cache.values():
        try:
            connection.close()
        except sqlite3.Error:
            pass
    cache.clear()
