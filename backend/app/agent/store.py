"""Durable agent-run storage.

Each run lives in its own folder next to the workspace definition::

    Workspaces/<workspace-id>/AgentRuns/<run-id>/
        run.json       ← full run state, rewritten atomically per transition
        events.jsonl   ← append-only event log, one JSON object per line

``run.json`` is the authoritative record (plan, approvals, messages, findings,
summary); ``events.jsonl`` is the replayable feed the frontend streams over
SSE — reconnecting clients pass the last seen ``seq`` and read forward from
disk, so no event is ever lost to a dropped connection or restart.

Only the run's worker thread writes ``run.json`` while the run is live; the
API layer goes through the runner's control surface instead of writing here.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..workspaces import Workspace, WorkspaceError, write_json_atomic

RUNS_DIRNAME = "AgentRuns"

MODES = ("auto", "permission")
# Statuses that mean "a worker thread should be driving this run".
ACTIVE_STATUSES = (
    "queued",
    "discovering",
    "planning",
    "executing",
    "awaiting_approval",
    "verifying",
    "summarizing",
)
# Statuses a run can rest in with no thread attached.
RESUMABLE_STATUSES = ("paused", "interrupted")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")

_event_locks: dict[str, threading.Lock] = {}
_event_locks_guard = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_lock(run_dir: Path) -> threading.Lock:
    key = str(run_dir)
    with _event_locks_guard:
        if key not in _event_locks:
            _event_locks[key] = threading.Lock()
        return _event_locks[key]


def runs_dir(workspace: Workspace) -> Path:
    return workspace.root / RUNS_DIRNAME


def run_dir(workspace: Workspace, run_id: str) -> Path:
    return runs_dir(workspace) / run_id


def new_run(
    workspace: Workspace,
    mode: str,
    context: dict | None = None,
    parent_run_id: str | None = None,
    limits: dict | None = None,
) -> dict:
    if mode not in MODES:
        raise WorkspaceError(f"Agent mode must be one of: {', '.join(MODES)}.")
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = {
        "schema_version": 1,
        "id": run_id,
        "workspace_id": workspace.id,
        "parent_run_id": parent_run_id,
        "mode": mode,
        "context": dict(context or {}),
        "status": "queued",
        "created": utcnow(),
        "started": None,
        "finished": None,
        "limits": dict(limits or {}),
        "usage": {"llm_turns": 0, "tool_calls": 0, "custom_analyses": 0},
        "discovery": {},
        "plan": {"stages": []},
        "approvals": [],
        "messages": [],
        "artifacts": [],
        "findings": [],
        "summary_markdown": None,
        "warnings": [],
        "error": None,
    }
    save_run(workspace, run)
    return run


def save_run(workspace: Workspace, run: dict) -> None:
    write_json_atomic(run_dir(workspace, run["id"]) / "run.json", run)


def load_run(workspace: Workspace, run_id: str) -> dict:
    path = run_dir(workspace, run_id) / "run.json"
    if not path.exists():
        raise WorkspaceError(f"Agent run '{run_id}' not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs(workspace: Workspace) -> list[dict]:
    """Newest-first run summaries (no plan/approval/message payloads)."""
    root = runs_dir(workspace)
    if not root.exists():
        return []
    summaries = []
    for folder in sorted(root.iterdir(), reverse=True):
        path = folder / "run.json"
        if not path.exists():
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append(run_summary(run))
    return summaries


def run_summary(run: dict) -> dict:
    tasks = [t for s in run["plan"]["stages"] for t in s.get("tasks", [])]
    return {
        "id": run["id"],
        "workspace_id": run["workspace_id"],
        "parent_run_id": run.get("parent_run_id"),
        "mode": run["mode"],
        "status": run["status"],
        "created": run["created"],
        "started": run.get("started"),
        "finished": run.get("finished"),
        "domain": (run.get("discovery") or {}).get("domain"),
        "task_counts": {
            "total": len(tasks),
            "completed": sum(1 for t in tasks if t["status"] == "completed"),
            "failed": sum(1 for t in tasks if t["status"] == "failed"),
        },
        "error": run.get("error"),
        "has_summary": bool(run.get("summary_markdown")),
    }


# ------------------------------------------------------------------- events
def append_event(workspace: Workspace, run_id: str, type_: str, data: dict) -> dict:
    """Append one event and return it (with its sequence number). Sequence
    numbers restart from the current line count, so they stay contiguous even
    across process restarts."""
    folder = run_dir(workspace, run_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "events.jsonl"
    with _event_lock(folder):
        seq = _line_count(path) + 1
        event = {"seq": seq, "at": utcnow(), "type": type_, "data": data}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str) + "\n")
    return event


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def read_events(workspace: Workspace, run_id: str, after: int = 0) -> list[dict]:
    """All events with ``seq > after``, in order. Tolerates a torn final line
    (a crash mid-append) by skipping unparseable lines."""
    path = run_dir(workspace, run_id) / "events.jsonl"
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("seq", 0) > after:
                events.append(event)
    return events


# ----------------------------------------------------------------- recovery
def recover_orphans(workspace: Workspace, live_run_ids: set[str]) -> list[str]:
    """Mark runs that claim to be active but have no live worker thread (the
    process restarted mid-run) as interrupted. Returns the affected run ids."""
    recovered = []
    root = runs_dir(workspace)
    if not root.exists():
        return recovered
    for folder in root.iterdir():
        path = folder / "run.json"
        if not path.exists():
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if run["status"] in ACTIVE_STATUSES and run["id"] not in live_run_ids:
            run["status"] = "interrupted"
            run["warnings"].append(
                "The backend stopped while this run was active; it was "
                "recovered as interrupted and can be resumed."
            )
            save_run(workspace, run)
            append_event(
                workspace, run["id"], "run_status", {"status": "interrupted"}
            )
            recovered.append(run["id"])
    return recovered
