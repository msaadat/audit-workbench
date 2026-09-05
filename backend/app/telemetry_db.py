"""Per-workspace SQLite store for telemetry and event logs.

This owns the *append-mostly, read-by-query* records: the Debug console's LLM
call traces, its event log, state snapshots and transitions, the AI activity
feed, and each agent run's replayable event stream.  They are the bulk of what
a working engagement writes — tens of thousands of records against a few
hundred audit artifacts — and every one of them is written once and then read
back filtered, paged, or pruned.  Files answered none of those questions
without reading everything: listing calls parsed every response body to build a
summary, paging an event log read the whole log, and retention meant rewriting
it.

Audit *content* deliberately stays in files.  Nothing here is a record of the
engagement; a lost telemetry database costs history, not evidence, which is why
this can be one database per workspace while :mod:`.db` stays the control plane
and workspace artifacts stay independently replaceable sidecars.

The database is a file inside the workspace folder, so moving or copying a
workspace still moves its telemetry with it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

DB_FILENAME = "telemetry.db"

_local = threading.local()
_migration_guard = threading.Lock()
_import_guard = threading.Lock()

# Retention caps, applied by :func:`prune`.  The store is local and append-only,
# so without a bound it grows for the life of the engagement.
MAX_CALL_RECORDS = 500
MAX_TRANSITION_RECORDS = 500
# Two per transition (before and after), so this cap is deliberately double the
# transition cap; a transition whose snapshot has aged out still carries its own
# inline ``changes`` list.
MAX_SNAPSHOT_FILES = 1000
MAX_EVENT_LINES = 20_000
# Checkpoints are bounded per run rather than per workspace: a run's own step
# history is what the console offers to roll back, and a cap shared across runs
# would let one long run evict every restore point of the run beside it.  Ten is
# what keeps the pathological case — a stage that rewrites a multi-megabyte
# artifact family on every re-run — inside the space telemetry already occupies.
MAX_CHECKPOINTS_PER_RUN = 10

MIGRATIONS: list[str] = [
    # -- 1 ---------------------------------------------------------------
    """
    -- Summary columns first and the full trace last: SQLite lays a row out in
    -- declaration order and spills the overflow onto separate pages, so a
    -- listing that selects only the leading columns never pages in the
    -- megabyte-scale request and response bodies stored beside them.
    CREATE TABLE llm_calls (
        id                  TEXT PRIMARY KEY,
        session_id          TEXT NOT NULL DEFAULT '',
        status              TEXT NOT NULL DEFAULT '',
        started_at          TEXT NOT NULL DEFAULT '',
        finished_at         TEXT,
        duration_ms         REAL,
        provider            TEXT,
        model               TEXT,
        profile             TEXT,
        finish_reason       TEXT,
        attempt_count       INTEGER NOT NULL DEFAULT 0,
        terminal_error      TEXT,
        run_id              TEXT,
        stage               TEXT,
        purpose             TEXT,
        request_size_bytes  INTEGER,
        response_size_bytes INTEGER,
        usage               TEXT,
        correlation         TEXT NOT NULL DEFAULT '{}',
        record              TEXT NOT NULL
    );
    CREATE INDEX llm_calls_started ON llm_calls(started_at DESC);
    CREATE INDEX llm_calls_run ON llm_calls(run_id);

    -- ``seq`` replaces the line number the JSONL reader derived at read time.
    -- It is a durable identity, so pruning no longer renumbers the events a
    -- reconnecting SSE client is holding a cursor into.
    CREATE TABLE debug_events (
        seq     INTEGER PRIMARY KEY AUTOINCREMENT,
        id      TEXT NOT NULL,
        at      TEXT NOT NULL,
        type    TEXT NOT NULL,
        run_id  TEXT,
        call_id TEXT,
        data    TEXT NOT NULL
    );
    CREATE INDEX debug_events_type ON debug_events(type);
    CREATE INDEX debug_events_run ON debug_events(run_id);
    CREATE INDEX debug_events_call ON debug_events(call_id);

    CREATE TABLE state_snapshots (
        sha1        TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        payload     TEXT NOT NULL
    );

    CREATE TABLE state_transitions (
        id      TEXT PRIMARY KEY,
        at      TEXT NOT NULL,
        kind    TEXT NOT NULL,
        trigger TEXT NOT NULL,
        run_id  TEXT,
        record  TEXT NOT NULL
    );
    CREATE INDEX state_transitions_at ON state_transitions(at);
    CREATE INDEX state_transitions_run ON state_transitions(run_id);

    CREATE TABLE graph_snapshots (
        run_id   TEXT NOT NULL,
        revision INTEGER NOT NULL,
        payload  TEXT NOT NULL,
        PRIMARY KEY (run_id, revision)
    );

    -- Content hashes of source files, keyed by path/size/mtime.  A cache, not
    -- a record: dropping it costs one rehash.
    CREATE TABLE file_signatures (
        key  TEXT PRIMARY KEY,
        sha1 TEXT NOT NULL
    );

    -- The document AI activity feed.
    CREATE TABLE activity_events (
        seq     INTEGER PRIMARY KEY AUTOINCREMENT,
        at      TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL
    );

    -- One agent run's replayable event stream.  ``seq`` is per run and dense,
    -- because reconnecting clients read forward from the last seq they saw.
    CREATE TABLE run_events (
        run_id TEXT NOT NULL,
        seq    INTEGER NOT NULL,
        at     TEXT NOT NULL,
        type   TEXT NOT NULL,
        data   TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );

    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # -- 2 ---------------------------------------------------------------
    """
    -- Workspace checkpoints: the manifest half of the restore points the
    -- Debug console rolls a run's completed steps back to.  The content itself
    -- lives in a content-addressed blob store under the workspace folder, so
    -- what is stored here is a path -> sha1 listing per checkpoint and nothing
    -- larger.  This is the one telemetry table a workspace's *files* depend on:
    -- losing it costs the ability to roll back, which is why the blob store is
    -- swept from these rows rather than the other way round.
    CREATE TABLE checkpoints (
        id          TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL,
        stage_id    TEXT NOT NULL DEFAULT '',
        capability  TEXT NOT NULL DEFAULT '',
        label       TEXT NOT NULL DEFAULT '',
        captured_at TEXT NOT NULL,
        revision    INTEGER NOT NULL DEFAULT 0,
        file_count  INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        new_bytes   INTEGER NOT NULL DEFAULT 0,
        restored_at TEXT
    );
    CREATE INDEX checkpoints_run ON checkpoints(run_id);
    CREATE INDEX checkpoints_captured ON checkpoints(captured_at DESC);

    CREATE TABLE checkpoint_files (
        checkpoint_id TEXT NOT NULL,
        path          TEXT NOT NULL,
        sha1          TEXT NOT NULL,
        size          INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (checkpoint_id, path)
    );
    -- Blob sweeping asks "is this content still referenced by any manifest",
    -- which is this index and nothing else.
    CREATE INDEX checkpoint_files_sha1 ON checkpoint_files(sha1);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def database_path(root: Path) -> Path:
    return Path(root) / DB_FILENAME


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    # Telemetry is disposable enough to trade a crash-window fsync for the
    # write rate a traced run actually produces.
    connection.execute("PRAGMA synchronous=NORMAL")


def migrate(connection: sqlite3.Connection) -> int:
    with _migration_guard:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        for version in range(current, len(MIGRATIONS)):
            connection.executescript(MIGRATIONS[version])
            # Pragmas do not accept bound parameters; the value is a loop index.
            connection.execute(f"PRAGMA user_version={version + 1}")
            connection.commit()
        return len(MIGRATIONS)


def connect(root: Path) -> sqlite3.Connection:
    """The calling thread's connection for one workspace's telemetry."""
    path = database_path(root)
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
        import_legacy(root, connection)
    return connection


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


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: str | None, default: object = None) -> object:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


# --------------------------------------------------------------- legacy import
LEGACY_IMPORT_KEY = "legacy_files_imported"


def import_legacy(root: Path, connection: sqlite3.Connection) -> dict[str, int]:
    """Load a workspace's pre-SQLite telemetry files into the database once.

    The files are read and left exactly where they are.  Telemetry is not
    evidence, but it is the operator's debugging history, so this never deletes
    the source; once the import has run the legacy trees are inert and can be
    removed by hand.
    """
    with _import_guard:
        done = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (LEGACY_IMPORT_KEY,)
        ).fetchone()
        if done is not None:
            return {}
        counts = _import_legacy_files(Path(root), connection)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (LEGACY_IMPORT_KEY, dumps(counts)),
        )
        connection.commit()
        return counts


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path):
    if not path.is_file():
        return
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # A torn final line is what an interrupted append leaves.
                continue


def _import_legacy_files(root: Path, connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    debug = root / "Debug"

    calls = []
    for path in sorted((debug / "LLMCalls").glob("*.json")):
        record = _read_json(path)
        if isinstance(record, dict) and record.get("id"):
            calls.append(call_row(record))
    if calls:
        connection.executemany(CALL_UPSERT, calls)
        counts["calls"] = len(calls)

    events = [
        (
            str(item.get("id") or ""),
            str(item.get("at") or ""),
            str(item.get("type") or ""),
            *_event_correlation(item),
            dumps(item.get("data") or {}),
        )
        for item in _read_jsonl(debug / "events.jsonl")
        if isinstance(item, dict)
    ]
    if events:
        connection.executemany(
            "INSERT INTO debug_events(id, at, type, run_id, call_id, data)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            events,
        )
        counts["events"] = len(events)

    snapshots = []
    for path in sorted((debug / "StateSnapshots").glob("*.json")):
        record = _read_json(path)
        if isinstance(record, dict) and record.get("sha1"):
            snapshots.append((
                str(record["sha1"]), str(record.get("kind") or ""),
                str(record.get("captured_at") or ""), dumps(record.get("payload")),
            ))
    if snapshots:
        connection.executemany(
            "INSERT OR REPLACE INTO state_snapshots(sha1, kind, captured_at, payload)"
            " VALUES(?, ?, ?, ?)",
            snapshots,
        )
        counts["snapshots"] = len(snapshots)

    transitions = []
    for path in sorted((debug / "StateTransitions").glob("*.json")):
        record = _read_json(path)
        if isinstance(record, dict) and record.get("id"):
            transitions.append(transition_row(record))
    if transitions:
        connection.executemany(TRANSITION_UPSERT, transitions)
        counts["transitions"] = len(transitions)

    graphs = []
    for folder in sorted((debug / "GraphSnapshots").glob("*")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            record = _read_json(path)
            if isinstance(record, dict):
                graphs.append((folder.name, int(record.get("revision") or 0), dumps(record)))
    if graphs:
        connection.executemany(
            "INSERT OR REPLACE INTO graph_snapshots(run_id, revision, payload)"
            " VALUES(?, ?, ?)",
            graphs,
        )
        counts["graph_snapshots"] = len(graphs)

    signatures = _read_json(debug / "FileSignatures.json")
    if isinstance(signatures, dict) and signatures:
        connection.executemany(
            "INSERT OR REPLACE INTO file_signatures(key, sha1) VALUES(?, ?)",
            [(str(k), str(v)) for k, v in signatures.items() if isinstance(v, str)],
        )
        counts["file_signatures"] = len(signatures)

    activity = [
        (str(item.get("at") or ""), dumps(item))
        for item in _read_jsonl(root / "AIActivity" / "events.jsonl")
        if isinstance(item, dict)
    ]
    if activity:
        connection.executemany(
            "INSERT INTO activity_events(at, payload) VALUES(?, ?)", activity
        )
        counts["activity"] = len(activity)

    run_events = []
    for folder in sorted((root / "AgentRuns").glob("*")):
        if not folder.is_dir():
            continue
        # Sequence numbers are reassigned densely in append order rather than
        # trusted.  A run whose process restarted mid-flight could repeat one:
        # the old writer recovered its counter by counting lines, so events
        # written after a truncation reused numbers already on disk.  Keeping
        # the file's order and renumbering preserves every event, where honouring
        # the stored number would drop whichever one lost the collision.
        for seq, item in enumerate(_read_jsonl(folder / "events.jsonl"), start=1):
            if not isinstance(item, dict):
                continue
            run_events.append((
                folder.name, seq, str(item.get("at") or ""),
                str(item.get("type") or ""), dumps(item.get("data") or {}),
            ))
    if run_events:
        connection.executemany(
            "INSERT OR REPLACE INTO run_events(run_id, seq, at, type, data)"
            " VALUES(?, ?, ?, ?, ?)",
            run_events,
        )
        counts["run_events"] = len(run_events)

    return counts


# ------------------------------------------------------------------ row shapes
CALL_COLUMNS = (
    "id", "session_id", "status", "started_at", "finished_at", "duration_ms",
    "provider", "model", "profile", "finish_reason", "attempt_count",
    "terminal_error", "run_id", "stage", "purpose", "request_size_bytes",
    "response_size_bytes", "usage", "correlation", "record",
)
CALL_UPSERT = (
    f"INSERT OR REPLACE INTO llm_calls({', '.join(CALL_COLUMNS)})"
    f" VALUES({', '.join('?' * len(CALL_COLUMNS))})"
)


def call_row(record: dict) -> tuple:
    correlation = record.get("correlation") or {}
    if not isinstance(correlation, dict):
        correlation = {}
    duration = record.get("duration_ms")
    return (
        str(record.get("id") or ""),
        str(record.get("session_id") or ""),
        str(record.get("status") or ""),
        str(record.get("started_at") or ""),
        record.get("finished_at"),
        float(duration) if isinstance(duration, (int, float)) else None,
        record.get("provider"),
        record.get("model"),
        record.get("profile"),
        record.get("finish_reason"),
        len(record.get("attempts") or []),
        record.get("terminal_error"),
        correlation.get("run_id"),
        correlation.get("stage"),
        correlation.get("purpose"),
        record.get("request_size_bytes"),
        record.get("response_size_bytes"),
        dumps(record.get("usage")) if record.get("usage") is not None else None,
        dumps(correlation),
        dumps(record),
    )


TRANSITION_UPSERT = (
    "INSERT OR REPLACE INTO state_transitions(id, at, kind, trigger, run_id, record)"
    " VALUES(?, ?, ?, ?, ?, ?)"
)


def transition_row(record: dict) -> tuple:
    correlation = record.get("correlation") or {}
    if not isinstance(correlation, dict):
        correlation = {}
    return (
        str(record.get("id") or ""),
        str(record.get("at") or ""),
        str(record.get("kind") or ""),
        str(record.get("trigger") or ""),
        correlation.get("run_id"),
        dumps(record),
    )


def _event_correlation(item: dict) -> tuple[str | None, str | None]:
    data = item.get("data") or {}
    if not isinstance(data, dict):
        return None, None
    correlation = data.get("correlation") or {}
    run_id = correlation.get("run_id") if isinstance(correlation, dict) else None
    return run_id, data.get("call_id")
