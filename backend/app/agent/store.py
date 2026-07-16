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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..workspaces import Workspace, WorkspaceError, write_json_atomic

RUNS_DIRNAME = "AgentRuns"

MODES = ("auto", "permission")
# Statuses that mean "a worker thread should be driving this run".
ACTIVE_STATUSES = (
    "queued",
    "interpreting",
    "discovering",
    "planning",
    "executing",
    "awaiting_approval",
    "awaiting_input",
    "verifying",
    "summarizing",
)
# Statuses a run can rest in with no thread attached.
RESUMABLE_STATUSES = ("paused", "interrupted")
TERMINAL_STATUSES = ("completed", "completed_with_issues", "failed", "cancelled")

_event_locks: dict[str, threading.Lock] = {}
_event_locks_guard = threading.Lock()
_run_locks: dict[str, threading.RLock] = {}
_run_locks_guard = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_lock(run_dir: Path) -> threading.Lock:
    key = str(run_dir)
    with _event_locks_guard:
        if key not in _event_locks:
            _event_locks[key] = threading.Lock()
        return _event_locks[key]


def _run_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _run_locks_guard:
        if key not in _run_locks:
            _run_locks[key] = threading.RLock()
        return _run_locks[key]


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
    kind: str = "analysis",
) -> dict:
    if mode not in MODES:
        raise WorkspaceError(f"Agent mode must be one of: {', '.join(MODES)}.")
    if kind not in ("analysis", "intake", "planning", "doc_test"):
        raise WorkspaceError("Unknown agent run kind.")
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = {
        "schema_version": 1,
        "id": run_id,
        "workspace_id": workspace.id,
        "parent_run_id": parent_run_id,
        "kind": kind,
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
    if kind == "planning":
        run["interview"] = {"captured": {}, "turns": 0, "pending_question": None}
    elif kind == "doc_test":
        run["doc_test"] = {"test_id": str((context or {}).get("test_id") or "")}
    save_run(workspace, run)
    return run


def new_command_run(
    workspace: Workspace,
    mode: str,
    command: dict,
    *,
    parent_run_id: str | None = None,
    limits: dict | None = None,
) -> dict:
    """Create a schema-v2 audit run driven by a command/action ledger."""
    if mode not in MODES:
        raise WorkspaceError(f"Agent mode must be one of: {', '.join(MODES)}.")
    source = str(command.get("source") or "chat")
    if source not in ("chat", "goal_template", "tab_button", "follow_up"):
        raise WorkspaceError("Unknown command source.")
    text = str(command.get("text") or "").strip()
    template = str(command.get("goal_template") or "").strip() or None
    if not text and not template:
        raise WorkspaceError("A command or goal template is required.")
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    command_id = f"cmd_{uuid.uuid4().hex[:12]}"
    run = {
        "schema_version": 2,
        "id": run_id,
        "workspace_id": workspace.id,
        "parent_run_id": parent_run_id,
        "kind": "audit",
        "mode": mode,
        "context": {},
        "command": {
            "id": command_id, "source": source, "text": text,
            "goal_template": template, "submitted_at": utcnow(),
            "status": "queued", "parent_command_id": command.get("parent_command_id"),
        },
        "goal": {"objective": text, "constraints": [], "completion_criteria": []},
        "graph_revision": 0,
        "actions": [],
        "rejected_proposals": [],
        "lifecycle_adjustments": [],
        "interactions": [],
        "pending_commands": [],
        "status": "queued",
        "created": utcnow(), "started": None, "finished": None,
        "usage": {"llm_turns": 0, "tool_calls": 0, "planner_waves": 0, "actions_started": 0},
        "limits": {
            "max_actions": 60, "max_waves": 8, "max_depth": 10,
            "max_model_turns": 40, "max_execution_attempts": 2,
            **dict(limits or {}),
        },
        # Compatibility projection consumed by older history/drawer clients.
        "discovery": {}, "plan": {"stages": []}, "approvals": [],
        "messages": [], "artifacts": [], "findings": [],
        "warnings": [], "summary_markdown": None, "error": None,
    }
    save_run(workspace, run)
    return run


def save_run(workspace: Workspace, run: dict) -> None:
    path = run_dir(workspace, run["id"]) / "run.json"
    with _run_lock(path):
        write_json_atomic(path, run)


def load_run(workspace: Workspace, run_id: str) -> dict:
    path = run_dir(workspace, run_id) / "run.json"
    if not path.exists():
        raise WorkspaceError(f"Agent run '{run_id}' not found.")
    with _run_lock(path):
        for attempt in range(10):
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))
    _hydrate_run(run)
    return run


def _hydrate_run(run: dict) -> None:
    """Read-compatible defaults; schema-v1 history is never rewritten."""
    run.setdefault("schema_version", 1)
    run.setdefault("kind", "analysis")
    if run["schema_version"] >= 2:
        run.setdefault("actions", [])
        run.setdefault("interactions", [])
        run.setdefault("pending_commands", [])
        run.setdefault("graph_revision", 0)
        run.setdefault("goal", {"objective": "", "constraints": [], "completion_criteria": []})
        run.setdefault("plan", {"stages": []})
        run.setdefault("approvals", [])
        run.setdefault("messages", [])
        run.setdefault("artifacts", [])
        run.setdefault("findings", [])
        run.setdefault("warnings", [])


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
            _hydrate_run(run)
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append(run_summary(run))
    return summaries


def run_summary(run: dict) -> dict:
    tasks = [t for s in (run.get("plan") or {}).get("stages", []) for t in s.get("tasks", [])]
    actions = run.get("actions") or []
    if actions:
        counts = {
            "total": len(actions),
            "completed": sum(1 for action in actions if action["status"] == "succeeded"),
            "failed": sum(1 for action in actions if action["status"] == "failed"),
            "blocked": sum(1 for action in actions if action["status"] == "blocked"),
        }
    else:
        counts = {
            "total": len(tasks),
            "completed": sum(1 for t in tasks if t["status"] == "completed"),
            "failed": sum(1 for t in tasks if t["status"] == "failed"),
            "blocked": 0,
        }
    return {
        "id": run["id"],
        "workspace_id": run["workspace_id"],
        "parent_run_id": run.get("parent_run_id"),
        "kind": run.get("kind", "analysis"),
        "mode": run["mode"],
        "status": run["status"],
        "created": run["created"],
        "started": run.get("started"),
        "finished": run.get("finished"),
        "domain": (run.get("discovery") or {}).get("domain"),
        "task_counts": counts,
        "error": run.get("error"),
        "has_summary": bool(run.get("summary_markdown")),
    }


def write_sidecar(workspace: Workspace, run_id: str, payload: object) -> dict:
    """Store large/sensitive interaction or undo content by immutable hash."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = __import__("hashlib").sha1(encoded.encode("utf-8")).hexdigest()
    folder = run_dir(workspace, run_id) / "sidecars"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.json"
    if not path.exists():
        write_json_atomic(path, payload)
    return {"sha1": digest, "path": f"sidecars/{digest}.json"}


def read_sidecar(workspace: Workspace, run_id: str, ref: dict) -> object:
    path = run_dir(workspace, run_id) / str(ref.get("path") or "")
    if not path.is_file() or path.parent != run_dir(workspace, run_id) / "sidecars":
        raise WorkspaceError("Run sidecar not found.")
    return json.loads(path.read_text(encoding="utf-8"))


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
            run.setdefault("kind", "analysis")
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
