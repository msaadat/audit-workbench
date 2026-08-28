"""Workspace-local, credential-safe debug telemetry.

The store is deliberately independent from the agent event ledger: every LLM
caller can participate, including short-lived assistant and report requests.

Records live in the workspace's telemetry database (:mod:`.telemetry_db`).  They
are written once and then read back filtered, paged, and pruned, which is what
a table answers cheaply and a directory of JSON files does not: listing calls
used to parse every stored response body just to build a summary row, and
retention meant restating a directory listing or rewriting a whole log.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any, Iterator

from . import telemetry_db
from .workspaces import WorkspaceError

SESSION_ID = uuid.uuid4().hex
DEBUG_DIRNAME = "Debug"
SECRET_KEYS = re.compile(
    r"(?:authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|bearer[-_]?token|^token$|cookie|secret|password|credential|\.env)",
    re.IGNORECASE,
)
SAFE_RESPONSE_HEADERS = {
    "content-type", "content-length", "date", "retry-after", "server",
    "x-request-id", "request-id", "cf-ray", "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
}

_context: ContextVar[dict[str, Any]] = ContextVar("debug_trace_context", default={})
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

# Telemetry levels, cheapest first. ``calls`` is the default because LLM call
# tracing is what the debug console is actually read for — provider, model,
# retries, latency — and it costs a handful of writes per run. State-transition
# tracing is a different order of magnitude: it snapshots, diffs, and rewrites
# the whole record on *every* durable save, which for one measured run meant 974
# transitions and 80MB of snapshots for 10 model calls. It stays opt-in.
TELEMETRY_OFF = "off"
TELEMETRY_CALLS = "calls"
TELEMETRY_FULL = "full"
TELEMETRY_LEVELS = (TELEMETRY_OFF, TELEMETRY_CALLS, TELEMETRY_FULL)
DEFAULT_TELEMETRY_LEVEL = TELEMETRY_CALLS
TELEMETRY_ENV_VAR = "DEBUG_TELEMETRY"

# Retention caps are declared beside the schema they bound; re-exported here
# because the console's retention notice reads them off this module.
MAX_CALL_RECORDS = telemetry_db.MAX_CALL_RECORDS
MAX_TRANSITION_RECORDS = telemetry_db.MAX_TRANSITION_RECORDS
MAX_SNAPSHOT_FILES = telemetry_db.MAX_SNAPSHOT_FILES
MAX_EVENT_LINES = telemetry_db.MAX_EVENT_LINES
_SWEEP_INTERVAL = 200
_sweep_counters: dict[str, int] = {}
_sweep_guard = threading.Lock()


def telemetry_level() -> str:
    """The active telemetry level, read from the environment on each call.

    Reading per call keeps the setting live for tests and for an operator who
    flips it without restarting; the cost is one dict lookup against the writes
    it decides to skip.
    """
    value = str(os.environ.get(TELEMETRY_ENV_VAR) or "").strip().lower()
    return value if value in TELEMETRY_LEVELS else DEFAULT_TELEMETRY_LEVEL


def calls_enabled() -> bool:
    """True when LLM call and event tracing should be written."""
    return telemetry_level() in (TELEMETRY_CALLS, TELEMETRY_FULL)


def state_enabled() -> bool:
    """True when state snapshots, diffs, and transitions should be written."""
    return telemetry_level() == TELEMETRY_FULL


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _lock(root: Path) -> threading.RLock:
    key = str(root)
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def workspace_root(workspace_id: str) -> Path:
    value = str(workspace_id or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise WorkspaceError("Invalid workspace debug reference.")
    hinted = _context.get().get("workspace_root")
    if hinted:
        candidate = Path(str(hinted))
        if (candidate / "workspace.json").exists():
            try:
                stored = json.loads((candidate / "workspace.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = {}
            if str(stored.get("id") or candidate.name) == value:
                return candidate
    # Telemetry runs on agent daemon threads with no request principal, and the
    # route that reached it has already resolved and authorized the workspace.
    # The registry lookup gets back to the root without either re-authorizing or
    # joining a path under the data root.
    from . import registry

    row = registry.locate(value)
    if row is None:
        raise WorkspaceError(f"Workspace '{value}' not found.")
    root = registry.workspace_root(row["owner_id"], row["dir_name"])
    if not (root / "workspace.json").exists():
        raise WorkspaceError(f"Workspace '{value}' not found.")
    return root


def debug_root(workspace_id: str) -> Path:
    """The legacy telemetry folder.

    Retained because the one-time import in :mod:`.telemetry_db` reads from it
    and :func:`clear` removes it; nothing reads telemetry from here any more.
    """
    return workspace_root(workspace_id) / DEBUG_DIRNAME


def connection(workspace_id: str):
    """This thread's telemetry connection for one workspace."""
    return telemetry_db.connect(workspace_root(workspace_id))


@contextmanager
def trace_context(**values: Any) -> Iterator[dict[str, Any]]:
    """Merge explicit correlations into the current thread/task context."""
    merged = {**_context.get(), **{k: v for k, v in values.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield merged
    finally:
        _context.reset(token)


def current_context(extra: dict | None = None) -> dict:
    return {**_context.get(), **{k: v for k, v in (extra or {}).items() if v is not None}}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def sha1(value: Any) -> str:
    return hashlib.sha1(_canonical(value)).hexdigest()


def _data_url(value: str) -> dict | None:
    if not value.startswith("data:") or ";base64," not in value:
        return None
    prefix, encoded = value.split(",", 1)
    digest = hashlib.sha256(encoded.encode("ascii", errors="ignore")).hexdigest()
    return {
        "representation": "binary_reference",
        "mime": prefix[5:].split(";", 1)[0],
        "encoded_size": len(encoded),
        "sha256": digest,
        "source_ref": current_context().get("image_source_ref"),
    }


def sanitize(value: Any, *, key: str = "") -> Any:
    """Recursively remove credentials and replace inline image bodies."""
    if SECRET_KEYS.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item, key=key) for item in value]
    if isinstance(value, bytes):
        return {"representation": "binary_reference", "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, str):
        image = _data_url(value)
        if image:
            return image
        safe = value
        # Provider keys and other credentials are loaded into the environment
        # by config.py. Redact their values even if a future caller
        # accidentally interpolates one into otherwise ordinary text.
        for name, secret in os.environ.items():
            if secret and len(secret) >= 6 and SECRET_KEYS.search(name):
                safe = safe.replace(secret, "[REDACTED]")
        return safe
    return value


def safe_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    try:
        pairs = headers.items()
    except AttributeError:
        return {}
    return {
        str(key).lower(): str(value)
        for key, value in pairs
        if str(key).lower() in SAFE_RESPONSE_HEADERS and not SECRET_KEYS.search(str(key))
    }


def append_event(workspace_id: str, type_: str, data: dict) -> dict:
    event = {"id": uuid.uuid4().hex, "at": utcnow(), "type": type_, "data": sanitize(data)}
    if not calls_enabled():
        return event
    data = event["data"] if isinstance(event["data"], dict) else {}
    correlation = data.get("correlation") or {}
    connection(workspace_id).execute(
        "INSERT INTO debug_events(id, at, type, run_id, call_id, data)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        (
            event["id"], event["at"], type_,
            correlation.get("run_id") if isinstance(correlation, dict) else None,
            data.get("call_id"),
            telemetry_db.dumps(event["data"]),
        ),
    )
    connection(workspace_id).commit()
    _note_write(workspace_id)
    return event


def start_call(request: dict, settings: Any, *, extra: dict | None = None) -> tuple[str | None, dict | None]:
    context = current_context(extra)
    workspace_id = str(context.get("workspace_id") or "")
    if not workspace_id or not calls_enabled():
        # Every caller guards its later tracing on a non-null call id, so
        # returning none here disables the whole call-tracing path at once.
        return None, None
    call_id = f"call_{uuid.uuid4().hex}"
    safe_request = sanitize(request)
    raw = _canonical(safe_request)
    now = utcnow()
    record = {
        "id": call_id, "session_id": SESSION_ID, "status": "running",
        "started_at": now, "finished_at": None, "duration_ms": None,
        "correlation": sanitize(context),
        "provider": getattr(settings, "backend", None),
        "model": getattr(settings, "model", None),
        "profile": context.get("profile"),
        "endpoint": f"{getattr(settings, 'base_url', '').rstrip('/')}/chat/completions",
        # ``None`` where the request sent none, never 0: the two are different
        # requests, and reading a run's sampling back off this record is how the
        # difference is ever noticed.
        "temperature": request.get("temperature"),
        "timeout_seconds": getattr(settings, "timeout", None),
        "request": safe_request,
        "request_size_bytes": len(raw), "request_sha256": hashlib.sha256(raw).hexdigest(),
        "attempts": [], "raw_response": None, "normalized_message": None,
        "usage": None, "finish_reason": None, "response_headers": {},
        "response_size_bytes": None, "response_sha256": None,
        "terminal_error": None,
    }
    handle = connection(workspace_id)
    handle.execute(telemetry_db.CALL_UPSERT, telemetry_db.call_row(record))
    handle.commit()
    append_event(workspace_id, "llm_call_started", {"call_id": call_id, "correlation": context})
    return call_id, record


def update_call(workspace_id: str, call_id: str, updater) -> dict:
    """Read-modify-write one call trace under the workspace's write lock.

    The lock is what keeps two attempts on the same call from interleaving; the
    surrounding transaction is what keeps a reader from seeing the half-updated
    row.
    """
    handle = connection(workspace_id)
    with _lock(workspace_root(workspace_id)):
        row = handle.execute(
            "SELECT record FROM llm_calls WHERE id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise WorkspaceError(f"Debug call '{call_id}' not found.")
        record = telemetry_db.loads(row["record"], {})
        updater(record)
        stored = sanitize(record)
        handle.execute(telemetry_db.CALL_UPSERT, telemetry_db.call_row(stored))
        handle.commit()
        return record


def add_attempt(workspace_id: str, call_id: str, attempt: dict) -> None:
    update_call(workspace_id, call_id, lambda record: record["attempts"].append(sanitize(attempt)))
    append_event(workspace_id, "llm_attempt", {"call_id": call_id, **attempt})


def finish_call(workspace_id: str, call_id: str, *, payload: Any = None,
                message: dict | None = None, error: str | None = None,
                status: str | None = None, headers: Any = None,
                started_monotonic: float | None = None) -> None:
    safe_payload = sanitize(payload)
    response_raw = _canonical(safe_payload) if payload is not None else None

    def apply(record: dict) -> None:
        record["status"] = status or ("failed" if error else "completed")
        record["finished_at"] = utcnow()
        record["duration_ms"] = max(0, round((time.monotonic() - started_monotonic) * 1000, 3)) if started_monotonic else None
        record["raw_response"] = safe_payload
        record["normalized_message"] = sanitize(message)
        record["terminal_error"] = error
        record["response_headers"] = safe_headers(headers)
        if response_raw is not None:
            record["response_size_bytes"] = len(response_raw)
            record["response_sha256"] = hashlib.sha256(response_raw).hexdigest()
        if isinstance(payload, dict):
            record["usage"] = sanitize(payload.get("usage"))
            choices = payload.get("choices") or []
            record["finish_reason"] = choices[0].get("finish_reason") if choices else None

    update_call(workspace_id, call_id, apply)
    append_event(workspace_id, "llm_call_finished", {"call_id": call_id, "status": status or ("failed" if error else "completed"), "error": error})


def write_snapshot(workspace_id: str, payload: dict, *, kind: str = "workspace") -> dict:
    if not state_enabled():
        return {"sha1": "", "path": "", "kind": kind}
    safe = sanitize(payload)
    digest = sha1(safe)
    handle = connection(workspace_id)
    # Snapshots are content-addressed, so a repeat is the same bytes: the first
    # writer's capture time is the one that means anything.
    handle.execute(
        "INSERT OR IGNORE INTO state_snapshots(sha1, kind, captured_at, payload)"
        " VALUES(?, ?, ?, ?)",
        (digest, kind, utcnow(), telemetry_db.dumps(safe)),
    )
    handle.commit()
    return {"sha1": digest, "path": f"StateSnapshots/{digest}.json", "kind": kind}


def _diff(before: Any, after: Any, path: str = "$") -> list[dict]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}"
            if key not in before:
                changes.append({"path": child, "change": "added", "after": sanitize(after[key])})
            elif key not in after:
                changes.append({"path": child, "change": "removed", "before": sanitize(before[key])})
            else:
                changes.extend(_diff(before[key], after[key], child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        def identity(item: Any) -> str | None:
            if not isinstance(item, dict): return None
            for key in ("id", "name", "semantic_id"):
                if item.get(key) is not None: return f"{key}={item[key]}"
            return None
        old_ids = [identity(item) for item in before]
        new_ids = [identity(item) for item in after]
        if all(old_ids) and all(new_ids) and len(set(old_ids)) == len(old_ids) and len(set(new_ids)) == len(new_ids):
            old_map, new_map = dict(zip(old_ids, before)), dict(zip(new_ids, after))
            changes = []
            for key in sorted(set(old_map) | set(new_map)):
                child = f"{path}[{key}]"
                if key not in old_map: changes.append({"path": child, "change": "added", "after": sanitize(new_map[key])})
                elif key not in new_map: changes.append({"path": child, "change": "removed", "before": sanitize(old_map[key])})
                else: changes.extend(_diff(old_map[key], new_map[key], child))
            return changes
    return [{"path": path, "change": "updated", "before": sanitize(before), "after": sanitize(after)}]


def record_transition(workspace_id: str, before: dict | None, after: dict,
                      *, trigger: str, kind: str = "workspace",
                      correlation: dict | None = None) -> dict | None:
    # Checked before the equality test: comparing two large records is itself
    # part of the cost this level is meant to avoid.
    if not state_enabled():
        return None
    if before == after:
        return None
    before_ref = write_snapshot(workspace_id, before or {}, kind=kind) if before is not None else None
    after_ref = write_snapshot(workspace_id, after, kind=kind)
    changes = _diff(before or {}, after)
    transition_id = f"transition_{uuid.uuid4().hex}"
    record = {
        "id": transition_id, "at": utcnow(), "kind": kind, "trigger": trigger,
        "correlation": sanitize(current_context(correlation)),
        "before_ref": before_ref, "after_ref": after_ref,
        "changed_paths": [item["path"] for item in changes],
        "changes": changes,
        "counts": {name: sum(item["change"] == name for item in changes) for name in ("added", "removed", "updated")},
    }
    handle = connection(workspace_id)
    handle.execute(telemetry_db.TRANSITION_UPSERT, telemetry_db.transition_row(record))
    handle.commit()
    append_event(workspace_id, "state_transition", {"transition_id": transition_id, "trigger": trigger, "kind": kind, "correlation": record["correlation"], "changed_paths": record["changed_paths"]})
    return record


def record_workspace_save(workspace_id: str, before: dict | None, after: dict) -> None:
    context = current_context()
    record_transition(workspace_id, before, after, trigger=str(context.get("trigger") or "workspace.save"), correlation=context)


def record_run_save(workspace_id: str, run_id: str, before: dict | None, after: dict) -> None:
    context = current_context({"run_id": run_id})
    record_transition(workspace_id, before, after, trigger=str(context.get("trigger") or "agent.run.save"), kind="agent_run", correlation=context)
    if state_enabled() and int(after.get("schema_version") or 1) >= 2:
        old_revision = (before or {}).get("graph_revision")
        revision = after.get("graph_revision")
        if before is None or revision != old_revision:
            graph = {
                "run_id": run_id, "schema_version": after.get("schema_version"),
                "revision": revision, "captured_at": utcnow(), "goal": after.get("goal"),
                "command": after.get("command"), "actions": after.get("actions") or [],
            }
            handle = connection(workspace_id)
            handle.execute(
                "INSERT OR IGNORE INTO graph_snapshots(run_id, revision, payload)"
                " VALUES(?, ?, ?)",
                (run_id, int(revision or 0), telemetry_db.dumps(sanitize(graph))),
            )
            handle.commit()


def _file_sha1(path: Path, cache: dict) -> str | None:
    try: stat = path.stat()
    except OSError: return None
    key = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    if key in cache: return cache[key]
    digest = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""): digest.update(block)
    except OSError: return None
    cache[key] = digest.hexdigest()
    return cache[key]


def capture_structural_state(workspace: Any, *, trigger: str, run_id: str | None = None) -> dict:
    """Capture metadata-rich state without copying source rows or binaries."""
    if not state_enabled():
        # This walks every table and join and loads frames to measure them, so
        # it is the single most expensive telemetry call in the store.
        return {"sha1": "", "path": "", "kind": "structural"}
    handle = connection(workspace.id)
    file_cache = {
        row["key"]: row["sha1"]
        for row in handle.execute("SELECT key, sha1 FROM file_signatures")
    }
    known = set(file_cache)
    tables = []
    from . import loader
    for entry in workspace.tables:
        path = workspace.data_dir / entry["file"]
        try:
            stat = path.stat(); signature = loader.file_signature(path)
            cached = any(key[0] == str(path.resolve()) and key[1:] == signature[1:] for key in loader._cache)
            try:
                frame = workspace.get_frame(entry["name"])
                dimensions = {"rows": frame.height, "columns": frame.width,
                              "schema": {name: str(dtype) for name, dtype in frame.schema.items()}}
            except Exception as error:
                dimensions = {"error": str(error)}
            tables.append({**entry, "path": str(path.relative_to(workspace.root)),
                           "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                           "sha1": _file_sha1(path, file_cache), "cache_state": "memory" if cached else "loaded_for_snapshot",
                           "dimensions": dimensions})
        except OSError:
            tables.append({**entry, "missing": True})
    joins = []
    for entry in workspace.joins:
        try:
            frame = workspace.get_frame(entry["name"])
            joins.append({"name": entry["name"], "rows": frame.height, "columns": frame.width,
                          "schema": {name: str(dtype) for name, dtype in frame.schema.items()}})
        except Exception as error: joins.append({"name": entry.get("name"), "error": str(error)})
    artifacts = []
    for folder in workspace.root.iterdir():
        if folder.name in {DEBUG_DIRNAME, "Data", "workspace.json"} or not folder.is_dir(): continue
        for path in folder.rglob("*"):
            if not path.is_file(): continue
            try: stat = path.stat()
            except OSError: continue
            artifacts.append({"path": str(path.relative_to(workspace.root)), "size": stat.st_size,
                              "mtime_ns": stat.st_mtime_ns, "sha1": _file_sha1(path, file_cache)})
    added = [(key, value) for key, value in file_cache.items() if key not in known]
    if added:
        handle.executemany(
            "INSERT OR REPLACE INTO file_signatures(key, sha1) VALUES(?, ?)", added
        )
        handle.commit()
    try:
        definition = json.loads(workspace.definition_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): definition = {}
    try:
        from . import llm
        runtime = {"assistant": llm.status(), "agent": llm.agent_status(),
                   "cpu_count": os.cpu_count(), "document_analysis_concurrency": 4,
                   "request_attempt_limit": 3, "process_session_id": SESSION_ID}
    except Exception as error: runtime = {"error": str(error), "process_session_id": SESSION_ID}
    payload = {"workspace": definition, "tables": tables, "joins": joins,
               "artifact_inventory": artifacts, "runtime": sanitize(runtime)}
    ref = write_snapshot(workspace.id, payload, kind="structural")
    append_event(workspace.id, "structural_snapshot", {"snapshot_ref": ref, "trigger": trigger,
                                                        "correlation": current_context({"run_id": run_id})})
    return ref


def recover_interrupted(workspace_id: str) -> int:
    """Close out calls a previous process left running.

    Selecting the affected ids is now a two-column indexed read rather than a
    parse of every stored trace, which matters because the calls listing runs
    this first on every request.
    """
    stale = [
        row["id"]
        for row in connection(workspace_id).execute(
            "SELECT id FROM llm_calls WHERE status = 'running' AND session_id != ?",
            (SESSION_ID,),
        )
    ]

    def apply(item: dict) -> None:
        item["status"] = "interrupted"
        item["finished_at"] = utcnow()
        item["terminal_error"] = "Backend stopped before this call completed."

    for call_id in stale:
        update_call(workspace_id, call_id, apply)
    return len(stale)


TELEMETRY_TABLES = (
    "llm_calls", "debug_events", "state_snapshots", "state_transitions",
    "graph_snapshots", "file_signatures",
)


def clear(workspace_id: str) -> None:
    """Drop this workspace's debug telemetry, including any legacy folder."""
    import shutil

    handle = connection(workspace_id)
    for table in TELEMETRY_TABLES:
        handle.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
    handle.commit()
    root = debug_root(workspace_id)
    if root.exists():
        shutil.rmtree(root)


def _note_write(workspace_id: str) -> None:
    """Count writes and sweep periodically rather than on every append."""
    with _sweep_guard:
        count = _sweep_counters.get(workspace_id, 0) + 1
        if count < _SWEEP_INTERVAL:
            _sweep_counters[workspace_id] = count
            return
        _sweep_counters[workspace_id] = 0
    try:
        prune(workspace_id)
    except Exception:
        # Retention is housekeeping. It must never fail a traced operation.
        pass


def prune(workspace_id: str) -> dict[str, int]:
    """Bound the local telemetry store to its retention caps.

    Newest-first, so an in-flight call is never the record that gets dropped.
    Returns what was removed so the debug console can say so rather than
    presenting a truncated history as complete.

    Retention used to mean listing a directory to find its oldest files and
    rewriting a whole event log to drop its head; it is now four bounded
    deletes.
    """
    handle = connection(workspace_id)
    removed = {
        "calls": handle.execute(
            "DELETE FROM llm_calls WHERE id NOT IN ("
            " SELECT id FROM llm_calls ORDER BY started_at DESC LIMIT ?)",
            (MAX_CALL_RECORDS,),
        ).rowcount,
        "transitions": handle.execute(
            "DELETE FROM state_transitions WHERE id NOT IN ("
            " SELECT id FROM state_transitions ORDER BY at DESC LIMIT ?)",
            (MAX_TRANSITION_RECORDS,),
        ).rowcount,
        "snapshots": handle.execute(
            "DELETE FROM state_snapshots WHERE sha1 NOT IN ("
            " SELECT sha1 FROM state_snapshots ORDER BY captured_at DESC LIMIT ?)",
            (MAX_SNAPSHOT_FILES,),
        ).rowcount,
        "events": handle.execute(
            "DELETE FROM debug_events WHERE seq NOT IN ("
            " SELECT seq FROM debug_events ORDER BY seq DESC LIMIT ?)",
            (MAX_EVENT_LINES,),
        ).rowcount,
    }
    handle.commit()
    return {key: value for key, value in removed.items() if value > 0}
