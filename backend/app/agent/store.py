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
GENERATION_MODES = ("reuse_existing", "force")
WORKFLOW_ENGINE = "workflow"
ACTION_ENGINE = "action"

# ``intake`` is a justified protocol engine in the target schema: folder intake
# is a single-unit protocol over a staged batch rather than a capability graph
# (``docs/agent-protocol-runner-decisions.md``). ``analysis`` is the legacy
# fixed-stage pipeline, retired in Phase 12. Neither is ever inferred from
# ``kind`` when a run is loaded or dispatched.
LEGACY_ANALYSIS_ENGINE = "analysis"
INTAKE_ENGINE = "intake"

COMMAND_ENGINES = frozenset({WORKFLOW_ENGINE, ACTION_ENGINE})
RUN_ENGINES = frozenset(
    {
        *COMMAND_ENGINES,
        LEGACY_ANALYSIS_ENGINE,
        INTAKE_ENGINE,
    }
)
ENGINE_BY_RUN_KIND = {
    "analysis": LEGACY_ANALYSIS_ENGINE,
    "intake": INTAKE_ENGINE,
}
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
TERMINAL_STATUSES = (
    "completed", "completed_with_open_items", "completed_with_issues", "failed", "cancelled"
)

_event_locks: dict[str, threading.Lock] = {}
_event_locks_guard = threading.Lock()
_run_locks: dict[str, threading.RLock] = {}
_run_locks_guard = threading.Lock()


def utcnow() -> str:
    # Millisecond precision matches the assistant-chat store so interleaved
    # transcript items sort stably by timestamp string.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def elapsed_ms(started: str | None, finished: str | None = None) -> int | None:
    """Return a bounded wall-clock duration for run/activity projections."""
    if not started:
        return None
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished) if finished else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0, int((end - start).total_seconds() * 1000))


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
    chat_id: str | None = None,
    source_message_id: str | None = None,
) -> dict:
    if mode not in MODES:
        raise WorkspaceError(f"Agent mode must be one of: {', '.join(MODES)}.")
    if kind not in ("analysis", "intake"):
        raise WorkspaceError("Unknown agent run kind.")
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = {
        "schema_version": 1,
        "engine": ENGINE_BY_RUN_KIND[kind],
        "id": run_id,
        "workspace_id": workspace.id,
        "parent_run_id": parent_run_id,
        "chat_id": chat_id,
        "source_message_id": source_message_id,
        "kind": kind,
        "mode": mode,
        "context": dict(context or {}),
        "status": "queued",
        "created": utcnow(),
        "started": None,
        "finished": None,
        "activity": None,
        "activity_revision": 0,
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


def new_command_run(
    workspace: Workspace,
    mode: str,
    command: dict,
    *,
    parent_run_id: str | None = None,
    limits: dict | None = None,
    context: dict | None = None,
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
    generation_mode = str(command.get("generation_mode") or "reuse_existing")
    if generation_mode not in GENERATION_MODES:
        raise WorkspaceError(
            "Workflow generation_mode must be 'reuse_existing' or 'force'."
        )
    run = {
        "schema_version": 2,
        # Unresolved commands enter the workflow router. Deterministic local
        # action routing changes this to ACTION_ENGINE before thread launch.
        "engine": WORKFLOW_ENGINE,
        "id": run_id,
        "workspace_id": workspace.id,
        "parent_run_id": parent_run_id,
        "planning_basis_run_id": str(
            command.get("planning_basis_run_id")
            or (context or {}).get("planning_basis_run_id")
            or ""
        ).strip() or None,
        "chat_id": str(command.get("chat_id") or "").strip() or None,
        "source_message_id": str(command.get("source_message_id") or "").strip() or None,
        "kind": "audit",
        "mode": mode,
        "context": dict(context or {}),
        "command": {
            "id": command_id, "source": source, "text": text,
            "goal_template": template, "submitted_at": utcnow(),
            "status": "queued", "parent_command_id": command.get("parent_command_id"),
            "chat_id": str(command.get("chat_id") or "").strip() or None,
            "source_message_id": str(command.get("source_message_id") or "").strip() or None,
            "context_refs": list(command.get("context_refs") or []),
            "requested_outcomes": [str(value) for value in command.get("requested_outcomes") or []],
            "target_refs": [str(value) for value in command.get("target_refs") or []],
            "generation_mode": generation_mode,
            "constraints": [str(value) for value in command.get("constraints") or []],
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
        "activity": None, "activity_revision": 0,
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
        "cancellation": None,
    }
    save_run(workspace, run)
    return run


def save_run(workspace: Workspace, run: dict) -> None:
    path = run_dir(workspace, run["id"]) / "run.json"
    with _run_lock(path):
        before = None
        if path.exists():
            try:
                before = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                before = None
        write_json_atomic(path, run)
        try:
            from .. import debug_store
            with debug_store.trace_context(workspace_root=str(workspace.root)):
                debug_store.record_run_save(workspace.id, run["id"], before, run)
        except Exception:
            pass


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
    run.setdefault("chat_id", None)
    run.setdefault("source_message_id", None)
    run.setdefault("activity", None)
    run.setdefault("activity_revision", 0)
    for stage in (run.get("plan") or {}).get("stages") or []:
        for task in stage.get("tasks") or []:
            task.setdefault("context_notes", list(task.get("disclosure") or []))
            task.setdefault("started_at", None)
            task.setdefault("finished_at", None)
    if run["schema_version"] >= 2:
        run.setdefault("planning_basis_run_id", None)
        run.setdefault("cancellation", None)
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
        command = run.setdefault("command", {})
        command.setdefault("chat_id", run.get("chat_id"))
        command.setdefault("source_message_id", run.get("source_message_id"))
        command.setdefault("context_refs", [])
        command.setdefault("requested_outcomes", [])
        command.setdefault("target_refs", [])
        command.setdefault("generation_mode", "reuse_existing")
        command.setdefault("constraints", [])
        for pending in run.get("pending_commands") or []:
            pending.setdefault("chat_id", None)
            pending.setdefault("source_message_id", None)
            pending.setdefault("context_refs", [])
    if run["schema_version"] >= 3:
        workflow = run.setdefault("workflow", {})
        workflow.setdefault("definition", "audit_workflow_v2")
        workflow.setdefault("requested_outcomes", [])
        workflow.setdefault("target_refs", ["workspace:current"])
        workflow.setdefault("generation_mode", "reuse_existing")
        workflow.setdefault(
            "reused_capability_details",
            [
                {
                    "capability": capability_id,
                    "currency_status": "not_assessed",
                }
                for capability_id in workflow.get("reused_capabilities") or []
            ],
        )
        workflow.setdefault("workflow_explanation", "")
        workflow.setdefault("next_outcomes", [])
        workflow.setdefault("pending_checkpoint", None)
        workflow.setdefault("resolved_capabilities", [])
        workflow.setdefault("reused_capabilities", [])
        workflow.setdefault("stages", [])


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
    workflow_units = [
        unit
        for stage in (run.get("workflow") or {}).get("stages") or []
        for unit in stage.get("units") or []
    ]
    if workflow_units:
        counts = {
            "total": len(workflow_units),
            "completed": sum(1 for unit in workflow_units if unit.get("status") in {"succeeded", "skipped"}),
            "failed": sum(1 for unit in workflow_units if unit.get("status") in {"failed", "conflict"}),
            "blocked": sum(1 for unit in workflow_units if unit.get("status") in {"blocked", "awaiting_input", "awaiting_confirmation"}),
        }
    elif actions:
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
        "planning_basis_run_id": run.get("planning_basis_run_id"),
        "chat_id": run.get("chat_id"),
        "source_message_id": run.get("source_message_id"),
        "engine": run.get("engine"),
        "kind": run.get("kind", "analysis"),
        "mode": run["mode"],
        "status": run["status"],
        "created": run["created"],
        "started": run.get("started"),
        "finished": run.get("finished"),
        "duration_ms": elapsed_ms(run.get("started"), run.get("finished")),
        "activity": run.get("activity"),
        "activity_revision": int(run.get("activity_revision") or 0),
        # Aggregate counters explain historical runs without fabricating the
        # raw calls that predate workspace Debug tracing.
        "usage": dict(run.get("usage") or {}),
        "domain": (run.get("discovery") or {}).get("domain"),
        "task_counts": counts,
        "error": run.get("error"),
        "cancellation": run.get("cancellation"),
        "has_summary": bool(run.get("summary_markdown")),
        "requested_outcomes": list((run.get("workflow") or {}).get("requested_outcomes") or []),
        "next_outcomes": list((run.get("workflow") or {}).get("next_outcomes") or []),
        "workflow_explanation": (run.get("workflow") or {}).get("workflow_explanation"),
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
