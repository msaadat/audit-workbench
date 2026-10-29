"""The engagement record's stage status, stored instead of re-derived per read.

Every row on the record takes its state from its capability's ``readiness()``.
That function answers the scheduler's question — what work is left, unit by
unit — and some of those answers are expensive: the analysis stages resolve
their table scope, and resolving it builds every join route between every pair
of tables. The record only wants the state, and it asked on every open.

A workspace whose manifest carries ``status_store`` keeps that answer in
``.status/stages.json``. The answer is the same ``readiness()`` sweep, with no
rule of its own; what changes is when it is computed. Nothing is compared to
decide whether it is current. Writers announce themselves instead: every write
path calls :func:`mark_dirty`, which advances a generation counter, and a
background worker recomputes the sweep and records which generation it read.
The answer is current while the two agree.

The writers that announce are the ones that can change a workspace: the HTTP
middleware after any state-changing request on a workspace, ``Workspace.save``,
an agent run after each commit and when it ends, and the document indexer. A
writer that does not announce leaves a stored answer looking current when it is
not — which is why the hooks are on those shared paths rather than on
individual artifact writers.

Workspaces created before this module have no flag and are read exactly as they
were: the record sweeps readiness on every read.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

#: Manifest key marking a workspace that keeps its status here.
FLAG = "status_store"
DIRNAME = ".status"
FILENAME = "stages.json"

#: Whether ``create_workspace`` sets the flag. Off only in tests that exercise
#: the record without the store.
ENABLED_FOR_NEW = True

#: Serve a stored answer that is out of date, marked as refreshing, rather than
#: recompute it inside the request. Off, a read of an out-of-date answer waits
#: for the sweep as every read did before this module.
SERVE_STALE = True

#: How long a scheduled refresh waits, so that the burst of writes one import
#: or one run stage makes is read once. A refresh is never pushed back further
#: by later writes, so a steady stream of commits cannot starve it.
DEBOUNCE_SECONDS = 1.0


@dataclass(frozen=True)
class Stored:
    """What the store holds: the last sweep, and whether it is still current."""

    stages: dict | None
    current: bool


def _key(root: Path | str) -> str:
    return str(Path(root).resolve())


def _path(root: Path | str) -> Path:
    return Path(root) / DIRNAME / FILENAME


def enabled(root: Path | str) -> bool:
    """Whether the workspace at ``root`` keeps a stored status."""
    try:
        definition = json.loads((Path(root) / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(definition, dict) and bool(definition.get(FLAG))


def _read(root: Path | str) -> dict:
    try:
        value = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(root: Path | str, data: dict) -> None:
    from .workspaces import write_json_atomic

    write_json_atomic(_path(root), data)


_file_locks: dict[str, threading.Lock] = {}
_refresh_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(table: dict[str, threading.Lock], root: Path | str) -> threading.Lock:
    with _locks_guard:
        return table.setdefault(_key(root), threading.Lock())


def lookup(root: Path | str) -> Stored:
    data = _read(root)
    stages = data.get("stages")
    if not isinstance(stages, dict):
        return Stored(None, False)
    return Stored(stages, data.get("computed") == int(data.get("generation") or 0))


def mark_dirty(target, *, refresh: bool = True) -> None:
    """Record that a workspace changed, and schedule a refresh.

    ``target`` is a ``Workspace`` — whose flag is already loaded, so the hot
    callers (every save, every run commit) read nothing — or a root, whose
    manifest is read for it. A no-op for a workspace without the flag, and for
    one that no longer exists: a delete is a state-changing request too.
    """
    # Checked by type, not by attribute: a ``Path`` has a ``root`` of its own.
    if isinstance(target, (str, Path)):
        root, flagged = Path(target), enabled(target)
    else:
        root, flagged = target.root, bool(target.status_store)
    if not flagged:
        return
    with _lock(_file_locks, root):
        data = _read(root)
        data["generation"] = int(data.get("generation") or 0) + 1
        _write(root, data)
    if refresh:
        schedule_refresh(root)


def refresh(root: Path | str, compute: Callable[[], dict] | None = None) -> dict:
    """Sweep readiness now and store it against the generation it read.

    Single-flight per workspace: a caller that arrives while another sweep is
    running waits for it, and returns that sweep if nothing was written since.
    ``compute`` lets the record sweep inside the read scope it already holds;
    without it the workspace is loaded fresh from ``root``.
    """
    with _lock(_refresh_locks, root):
        stored = lookup(root)
        if stored.current and stored.stages is not None:
            return stored.stages
        with _lock(_file_locks, root):
            generation = int(_read(root).get("generation") or 0)
        if compute is None:
            from .engagement_record import compute_readiness
            from .workspaces import Workspace

            stages = compute_readiness(Workspace(Path(root)))
        else:
            stages = compute()
        # Stored and served in the one shape, so an answer read back from disk
        # and one computed in this request cannot differ.
        stages = json.loads(json.dumps(stages, default=str))
        with _lock(_file_locks, root):
            if not (Path(root) / "workspace.json").exists():
                return stages
            data = _read(root)
            data.update(
                stages=stages,
                computed=generation,
                computed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            _write(root, data)
            again = int(data.get("generation") or 0) != generation
    if again:
        schedule_refresh(root)
    return stages


# --------------------------------------------------------------------------- #
# Background refresher: one daemon thread for the process, one queue entry per
# workspace, so two refreshes of the same workspace never run at once.
# --------------------------------------------------------------------------- #
_pending: dict[str, float] = {}
_running: set[str] = set()
_cond = threading.Condition()
_worker: threading.Thread | None = None


def schedule_refresh(root: Path | str) -> None:
    key = _key(root)
    with _cond:
        _pending.setdefault(key, time.monotonic() + DEBOUNCE_SECONDS)
        global _worker
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="stage-status-refresher", daemon=True)
            _worker.start()
        _cond.notify_all()


def _next_due() -> str:
    with _cond:
        while True:
            if not _pending:
                _cond.wait()
                continue
            key, due = min(_pending.items(), key=lambda item: item[1])
            remaining = due - time.monotonic()
            if remaining > 0:
                _cond.wait(remaining)
                continue
            del _pending[key]
            _running.add(key)
            return key


def _work() -> None:
    while True:
        key = _next_due()
        try:
            if (Path(key) / "workspace.json").exists():
                refresh(key)
        except Exception:
            # The answer stays marked out of date, so the next read schedules
            # another attempt; a failure here must not end the thread.
            log.exception("Stage status refresh failed for %s", key)
        finally:
            with _cond:
                _running.discard(key)
                _cond.notify_all()


def wait_idle(timeout: float = 30.0) -> bool:
    """Block until no refresh is pending or running. For tests."""
    with _cond:
        return _cond.wait_for(lambda: not _pending and not _running, timeout)
