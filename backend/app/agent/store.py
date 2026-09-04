"""Durable agent-run storage.

Each run lives in its own folder next to the workspace definition::

    Workspaces/<workspace-id>/AgentRuns/<run-id>/
        run.json       ← full run state, rewritten atomically per transition

``run.json`` is the authoritative record (plan, approvals, messages, findings,
summary) and stays a file: it is the run's durable output, replaceable and
readable without the application.

The replayable event feed the frontend streams over SSE is not — it lives in
the workspace's telemetry database, keyed by run.  Reconnecting clients pass
the last seen ``seq`` and read forward, which is an indexed range read there
and was a full scan of a growing log here.

Only the run's worker thread writes ``run.json`` while the run is live; the
API layer goes through the runner's control surface instead of writing here.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .. import telemetry_db
from ..workspaces import Workspace, WorkspaceError, write_json_atomic

RUNS_DIRNAME = "AgentRuns"

MODES = ("auto", "permission")
GENERATION_MODES = ("reuse_existing", "force")
WORKFLOW_ENGINE = "workflow"
ACTION_ENGINE = "action"

# ``intake`` is a justified protocol engine in the target schema: folder intake
# is a single-unit protocol over a staged batch rather than a capability graph
# (``docs/agent-protocol-runner-decisions.md``). It is the only one left —
# Phase 12 retired the fixed-stage ``analysis`` pipeline and its engine value —
# and it is never inferred from ``kind`` when a run is loaded or dispatched.
INTAKE_ENGINE = "intake"
INTAKE_RUN_KIND = "intake"

COMMAND_ENGINES = frozenset({WORKFLOW_ENGINE, ACTION_ENGINE})
# The final supported engine set (P11.1, narrowed by P12.2). Dispatch accepts
# exactly these values; a record whose engine is missing or outside this set
# fails closed.
RUN_ENGINES = frozenset({*COMMAND_ENGINES, INTAKE_ENGINE})
# ``start_run`` selects a protocol engine from its explicit ``kind`` argument at
# *creation* time and persists it. This mapping is a creation-time contract, not
# a dispatch fallback: nothing reads it while loading, resuming, or dispatching.
PROTOCOL_ENGINE_BY_RUN_KIND = {INTAKE_RUN_KIND: INTAKE_ENGINE}
PROTOCOL_RUN_KINDS = frozenset(PROTOCOL_ENGINE_BY_RUN_KIND)
COMMAND_RUN_KIND = "audit"


def is_command_run(run: dict) -> bool:
    """True for a run created by ``new_command_run``.

    Command-ness is a durable record shape — the presence of the command record
    — and never an engine inference: a command run whose route is still pending
    has no engine yet, but can already queue follow-up commands.
    """

    command = run.get("command")
    return isinstance(command, dict) and bool(command.get("id"))
# Statuses that mean "a worker thread should be driving this run".
ACTIVE_STATUSES = (
    "queued",
    "interpreting",
    "executing",
    "awaiting_approval",
    "awaiting_input",
    "verifying",
)
# Statuses a run can rest in with no thread attached.
RESUMABLE_STATUSES = ("paused", "interrupted")
# ``completed_with_failures`` is distinct from ``failed``: the run committed
# real work and some units did not settle. ``failed`` means nothing settled.
TERMINAL_STATUSES = (
    "completed", "completed_with_open_items", "completed_with_issues",
    "completed_with_failures", "failed", "cancelled",
)
# Terminal statuses that still produced durable output.
PARTIAL_STATUSES = frozenset(
    {"completed_with_open_items", "completed_with_issues", "completed_with_failures"}
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


# Unit sidecars (contexts, proposals, receipts, rejections) are named after the
# semantic unit ID they hold, one unit per file. Percent-encoding keeps that
# layout portable: unit IDs are colon-separated, and a colon is reserved on
# Windows.
#
# The encoded name is capped because some unit IDs grow with their scope — a
# join-utility unit names every table it covers — while Windows resolves any
# path over 260 characters as ENOENT, and ``write_json_atomic`` spends a further
# 12 characters on its ``.<name>.<hex>.tmp`` sidecar before the destination is
# ever reached. Over the cap the readable prefix is kept and the full ID is
# carried by a digest, so long units stay diagnosable and stay distinct.
#
# The cap is a fixed character count rather than a budget derived from the run
# folder's own length, so one unit ID names one file on every host a workspace
# is copied to.
UNIT_FILENAME_LIMIT = 100

# ``+`` marks an elided name. ``quote`` always percent-encodes it, so a capped
# name can never collide with an uncapped one.
_UNIT_ELISION = "+"
_UNIT_DIGEST_CHARS = 16


def _encode_unit_id(unit_id: str) -> str:
    encoded = quote(unit_id, safe="._-")
    if encoded in {".", ".."}:
        encoded = "".join(f"%{byte:02X}" for byte in unit_id.encode("utf-8"))
    return encoded


def unit_filename(unit_id: str) -> str:
    """Name the one file that holds ``unit_id``'s sidecar, within the path cap."""
    value = str(unit_id or "").strip()
    if not value:
        raise ValueError("Unit id must be non-empty.")
    encoded = _encode_unit_id(value)
    name = f"{encoded}.json"
    if len(name) <= UNIT_FILENAME_LIMIT:
        return name
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_UNIT_DIGEST_CHARS]
    suffix = f"{_UNIT_ELISION}{digest}.json"
    head = encoded[: UNIT_FILENAME_LIMIT - len(suffix)]
    # Cutting mid-escape would leave a dangling "%" or "%3"; drop the fragment.
    if "%" in head[-2:]:
        head = head[: head.rindex("%")]
    return f"{head}{suffix}"


def legacy_unit_filename(unit_id: str) -> str:
    """Name a sidecar as it was written before :data:`UNIT_FILENAME_LIMIT`.

    Only readers use this. Runs written on a filesystem without the 260-character
    ceiling may hold uncapped names, and their references stay resolvable.
    """
    value = str(unit_id or "").strip()
    if not value:
        raise ValueError("Unit id must be non-empty.")
    return f"{_encode_unit_id(value)}.json"


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
    kind: str = INTAKE_RUN_KIND,
    chat_id: str | None = None,
    source_message_id: str | None = None,
) -> dict:
    """Create the one retained protocol run record (folder intake)."""
    if mode not in MODES:
        raise WorkspaceError(f"Agent mode must be one of: {', '.join(MODES)}.")
    if kind not in PROTOCOL_RUN_KINDS:
        raise WorkspaceError("Unknown agent run kind.")
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = {
        "schema_version": 1,
        "engine": PROTOCOL_ENGINE_BY_RUN_KIND[kind],
        "route": None,
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
        "usage": {"llm_turns": 0, "tool_calls": 0},
        "plan": {"stages": []},
        "approvals": [],
        "messages": [],
        "narration": [],
        "milestones": [],
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
        # No engine is chosen at creation. ``routing.resolve_route`` classifies
        # the command and persists both the normalized route and the selected
        # engine before the worker thread launches; a command the deterministic
        # pass cannot classify keeps ``route.status == "pending"`` until the
        # bounded router worker decides on the thread.
        "engine": None,
        "route": None,
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
        "kind": COMMAND_RUN_KIND,
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
        "target_adjustments": [],
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
        # Shared drawer projections written by whichever engine runs the record.
        "plan": {"stages": []}, "approvals": [],
        "messages": [], "narration": [], "milestones": [], "artifacts": [], "findings": [],
        "warnings": [], "summary_markdown": None, "error": None,
        "cancellation": None,
    }
    save_run(workspace, run)
    return run


def save_run(workspace: Workspace, run: dict) -> None:
    path = run_dir(workspace, run["id"]) / "run.json"
    from .. import debug_store

    with _run_lock(path):
        # Reading the prior record exists only to diff it for state telemetry.
        # A live run saves on every transition, so at the default telemetry
        # level this read is the difference between one full parse of a growing
        # record per save and none.
        before = None
        if debug_store.state_enabled() and path.exists():
            try:
                before = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                before = None
        write_json_atomic(path, run)
        try:
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
    """Fill in optional fields of the supported record shapes.

    This is not a converter: nothing here infers an engine, a run kind, or a
    schema version from record contents. A record that predates the supported
    shapes stays as it is on disk and fails closed at dispatch.
    """
    run.setdefault("schema_version", 1)
    run.setdefault("route", None)
    run.setdefault("chat_id", None)
    run.setdefault("source_message_id", None)
    run.setdefault("activity", None)
    run.setdefault("activity_revision", 0)
    # Narration is additive: runs written before it existed simply have none,
    # and their cards fall back to the status projection.
    run.setdefault("narration", [])
    # Milestones are durable, deterministic stage-result projections. Older
    # runs have no milestone history and continue to use their closing turn.
    run.setdefault("milestones", [])
    for stage in (run.get("plan") or {}).get("stages") or []:
        for task in stage.get("tasks") or []:
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
        # The workflow definition is never defaulted: routing persists the
        # authoritative id when it materializes the graph, and a record without
        # one fails closed in ``workflow_dispatch`` rather than being guessed
        # into the audit composition.
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
        "route": run.get("route"),
        "kind": run.get("kind"),
        "mode": run["mode"],
        "status": run["status"],
        "created": run["created"],
        "started": run.get("started"),
        "finished": run.get("finished"),
        "duration_ms": elapsed_ms(run.get("started"), run.get("finished")),
        "activity": run.get("activity"),
        "activity_revision": int(run.get("activity_revision") or 0),
        # Aggregate counters explain a run without fabricating the raw calls
        # that workspace Debug tracing records separately.
        "usage": dict(run.get("usage") or {}),
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
    """Append one event and return it, with its per-run sequence number.

    The next sequence number comes from the run's own rows inside the insert's
    transaction, so it stays contiguous across process restarts without the
    in-memory counter the append-only log needed to avoid re-counting its lines
    on every write.
    """
    handle = telemetry_db.connect(workspace.root)
    with _event_lock(run_dir(workspace, run_id)):
        seq = int(handle.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0])
        event = {"seq": seq, "at": utcnow(), "type": type_, "data": data}
        handle.execute(
            "INSERT INTO run_events(run_id, seq, at, type, data) VALUES(?, ?, ?, ?, ?)",
            (run_id, seq, event["at"], type_, telemetry_db.dumps(data)),
        )
        handle.commit()
    return event


def read_events(workspace: Workspace, run_id: str, after: int = 0) -> list[dict]:
    """All events with ``seq > after``, in order."""
    return [
        {
            "seq": row["seq"], "at": row["at"], "type": row["type"],
            "data": telemetry_db.loads(row["data"], {}),
        }
        for row in telemetry_db.connect(workspace.root).execute(
            "SELECT seq, at, type, data FROM run_events"
            " WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, int(after or 0)),
        )
    ]


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
