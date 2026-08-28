"""Read models, timing metrics, and deterministic causal analysis for Debug."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import debug_store, telemetry_db
from .agent import store as agent_store
from .workspaces import Workspace, WorkspaceError


def _dt(value: str | None) -> datetime | None:
    if not value: return None
    try: return datetime.fromisoformat(value)
    except (TypeError, ValueError): return None


def _ms(start: str | None, end: str | None) -> float:
    left, right = _dt(start), _dt(end)
    if not left or not right: return 0.0
    return max(0.0, (right - left).total_seconds() * 1000)


# The summary the console lists.  Deliberately every column except ``record``:
# the stored request and response bodies are the bulk of a trace and no listing
# needs them.
_CALL_SUMMARY_COLUMNS = (
    "id, status, started_at, finished_at, duration_ms, provider, model, profile,"
    " finish_reason, usage, attempt_count, terminal_error, correlation,"
    " request_size_bytes, response_size_bytes"
)


def _call_filters(run_id, status, stage, purpose) -> tuple[str, list]:
    clauses, params = [], []
    for column, value in (
        ("run_id", run_id), ("status", status), ("stage", stage), ("purpose", purpose),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _call_summary(row) -> dict:
    attempts = int(row["attempt_count"] or 0)
    return {
        "id": row["id"], "status": row["status"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "duration_ms": row["duration_ms"], "provider": row["provider"],
        "model": row["model"], "profile": row["profile"],
        "finish_reason": row["finish_reason"],
        "usage": telemetry_db.loads(row["usage"]),
        "attempt_count": attempts,
        "retry_count": max(0, attempts - 1),
        "error": row["terminal_error"],
        "correlation": telemetry_db.loads(row["correlation"], {}),
        "request_size_bytes": row["request_size_bytes"],
        "response_size_bytes": row["response_size_bytes"],
    }


def list_calls(workspace: Workspace, *, cursor: int = 0, limit: int = 100,
               run_id: str | None = None, status: str | None = None,
               stage: str | None = None, purpose: str | None = None) -> dict:
    debug_store.recover_interrupted(workspace.id)
    handle = debug_store.connection(workspace.id)
    where, params = _call_filters(run_id, status, stage, purpose)
    cursor = max(0, int(cursor or 0))
    limit = min(250, max(1, int(limit or 100)))
    total = handle.execute(
        f"SELECT COUNT(*) FROM llm_calls{where}", params  # noqa: S608 - fixed clauses
    ).fetchone()[0]
    rows = handle.execute(
        f"SELECT {_CALL_SUMMARY_COLUMNS} FROM llm_calls{where}"  # noqa: S608
        " ORDER BY started_at DESC LIMIT ? OFFSET ?",
        [*params, limit, cursor],
    ).fetchall()
    items = [_call_summary(row) for row in rows]
    return {
        "items": items, "cursor": cursor, "total": total,
        "next_cursor": cursor + len(items) if cursor + len(items) < total else None,
    }


def get_call(workspace: Workspace, call_id: str) -> dict:
    if not call_id.startswith("call_"):
        raise WorkspaceError("Invalid debug call reference.")
    row = debug_store.connection(workspace.id).execute(
        "SELECT record FROM llm_calls WHERE id = ?", (call_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceError(f"Debug call '{call_id}' not found.")
    return telemetry_db.loads(row["record"], {})


def list_events(workspace: Workspace, *, cursor: int = 0, limit: int = 100,
                type_: str | None = None, run_id: str | None = None,
                call_id: str | None = None) -> dict:
    """One page of the debug event log, reading forward from ``cursor``.

    ``cursor`` is a sequence number, not an offset into the filtered list.  The
    two used to coincide because ``seq`` was the line number, which is why the
    SSE stream could feed the last ``seq`` it emitted back in as a cursor; now
    that ``seq`` is durable, forward-reading is what that contract needs, and a
    filtered page no longer skips events by counting them twice.
    """
    handle = debug_store.connection(workspace.id)
    cursor = max(0, int(cursor or 0))
    limit = min(250, max(1, int(limit or 100)))
    filters, filter_params = [], []
    for column, value in (("type", type_), ("run_id", run_id), ("call_id", call_id)):
        if value:
            filters.append(f"{column} = ?")
            filter_params.append(value)
    # ``total`` counts everything the filter matches, as the file-backed reader
    # reported it; ``remaining`` is what is still ahead of the cursor and is what
    # decides whether there is a next page.
    filter_where = (" WHERE " + " AND ".join(filters)) if filters else ""
    total = handle.execute(
        f"SELECT COUNT(*) FROM debug_events{filter_where}",  # noqa: S608 - fixed clauses
        filter_params,
    ).fetchone()[0]
    where = " WHERE " + " AND ".join(["seq > ?", *filters])
    params = [cursor, *filter_params]
    remaining = handle.execute(
        f"SELECT COUNT(*) FROM debug_events{where}", params  # noqa: S608
    ).fetchone()[0]
    rows = handle.execute(
        f"SELECT seq, id, at, type, data FROM debug_events{where}"  # noqa: S608
        " ORDER BY seq LIMIT ?",
        [*params, limit],
    ).fetchall()
    items = [
        {
            "seq": row["seq"], "id": row["id"], "at": row["at"], "type": row["type"],
            "data": telemetry_db.loads(row["data"], {}),
        }
        for row in rows
    ]
    return {
        "items": items, "cursor": cursor, "total": total,
        "next_cursor": items[-1]["seq"] if len(items) < remaining else None,
    }


def _transitions(workspace: Workspace, *, run_id: str | None = None) -> list[dict]:
    where, params = ("", [])
    if run_id:
        where, params = " WHERE run_id = ?", [run_id]
    return [
        telemetry_db.loads(row["record"], {})
        for row in debug_store.connection(workspace.id).execute(
            f"SELECT record FROM state_transitions{where} ORDER BY at",  # noqa: S608
            params,
        )
    ]


def _graph_snapshots(workspace: Workspace, run_id: str) -> list[dict]:
    return [
        telemetry_db.loads(row["payload"], {})
        for row in debug_store.connection(workspace.id).execute(
            "SELECT payload FROM graph_snapshots WHERE run_id = ? ORDER BY revision",
            (run_id,),
        )
    ]


def _agent_events(workspace: Workspace, run_id: str) -> list[dict]:
    try: return agent_store.read_events(workspace, run_id, 0)
    except Exception: return []


def _interval_union(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals: return 0.0
    total = 0.0
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]: merged.append([start, end])
        elif end > merged[-1][1]: merged[-1][1] = end
    for start, end in merged: total += (end - start).total_seconds() * 1000
    return max(0.0, total)


def _wait_metrics(run: dict, events: list[dict], end: datetime) -> dict:
    statuses = {"awaiting_approval": "approval_wait_ms", "awaiting_input": "input_wait_ms", "paused": "paused_ms"}
    totals = {value: 0.0 for value in statuses.values()}
    current = None; started = None
    initial = _dt(run.get("started") or run.get("created"))
    for event in events:
        if event.get("type") != "run_status": continue
        at = _dt(event.get("at")); status = (event.get("data") or {}).get("status")
        if at and current in statuses and started: totals[statuses[current]] += max(0, (at - started).total_seconds() * 1000)
        current, started = status, at or started or initial
    if current in statuses and started: totals[statuses[current]] += max(0, (end - started).total_seconds() * 1000)
    return totals


def timing_metrics(run: dict, calls: list[dict], events: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    created = _dt(run.get("created")); started = _dt(run.get("started")) or created
    finished = _dt(run.get("finished")) or now
    call_intervals = [(a, b) for item in calls if (a := _dt(item.get("started_at"))) and (b := _dt(item.get("finished_at")))]
    summed_llm = sum(float(item.get("duration_ms") or _ms(item.get("started_at"), item.get("finished_at"))) for item in calls)
    union_llm = _interval_union(call_intervals)
    retry = sum(float(attempt.get("retry_delay_ms") or 0) for item in calls for attempt in item.get("attempts") or [])
    actions = run.get("actions") or []
    tasks = [task for stage in (run.get("plan") or {}).get("stages") or [] for task in stage.get("tasks") or []]
    action_ms = [(_ms(item.get("started_at"), item.get("finished_at")), item) for item in actions]
    task_ms = [(_ms(item.get("started_at"), item.get("finished_at")), item) for item in tasks]
    waits = _wait_metrics(run, events, finished)
    end_to_end = max(0.0, (finished - created).total_seconds() * 1000) if created else 0.0
    active = max(0.0, (finished - started).total_seconds() * 1000) if started else 0.0
    wait_total = sum(waits.values())
    longest_action = max(action_ms, default=(0, None), key=lambda value: value[0])
    longest_task = max(task_ms, default=(0, None), key=lambda value: value[0])
    longest_call = max(((float(item.get("duration_ms") or 0), item) for item in calls), default=(0, None), key=lambda value: value[0])
    return {
        "queue_ms": _ms(run.get("created"), run.get("started")), "end_to_end_ms": end_to_end,
        "active_ms": max(0.0, active - wait_total), **waits,
        "summed_llm_ms": summed_llm, "llm_wall_union_ms": union_llm,
        "overlap_saved_ms": max(0.0, summed_llm - union_llm),
        "parallelism_factor": round(summed_llm / union_llm, 3) if union_llm else 0,
        "retry_wait_ms": retry,
        "local_action_ms": sum(value for value, _ in action_ms),
        "longest_action": {"id": longest_action[1].get("id"), "duration_ms": longest_action[0]} if longest_action[1] else None,
        "longest_task": {"id": longest_task[1].get("id"), "duration_ms": longest_task[0]} if longest_task[1] else None,
        "longest_call": {"id": longest_call[1].get("id"), "duration_ms": longest_call[0]} if longest_call[1] else None,
        "retry_waste_ratio": round(retry / end_to_end, 4) if end_to_end else 0,
    }


def _dependency_chain(action: dict, by_id: dict[str, dict], seen: set[str] | None = None) -> list[dict]:
    seen = set(seen or ())
    if action.get("id") in seen: return []
    seen.add(action.get("id"))
    for ref in action.get("depends_on") or action.get("dependencies") or []:
        dependency = by_id.get(ref)
        if dependency and dependency.get("status") in {"failed", "blocked", "skipped", "cancelled"}:
            return [{"id": dependency.get("id"), "status": dependency.get("status"), "error": dependency.get("error")}, *_dependency_chain(dependency, by_id, seen)]
    return []


def causal_analysis(run: dict, calls: list[dict], transitions: list[dict] | None = None,
                    metrics: dict | None = None) -> dict:
    actions = run.get("actions") or []
    by_id = {item.get("id"): item for item in actions}
    failed = next((item for item in actions if item.get("status") == "failed"), None)
    blocked = [{"id": item.get("id"), "chain": _dependency_chain(item, by_id)} for item in actions if item.get("status") == "blocked"]
    failed_calls = [{"id": item.get("id"), "attempts": item.get("attempts"), "terminal_error": item.get("terminal_error")} for item in calls if item.get("status") in {"failed", "interrupted"}]
    active_action = next((item for item in actions if item.get("status") in {"running", "awaiting_input", "awaiting_confirmation"}), None) or failed
    active_task = next((task for stage in (run.get("plan") or {}).get("stages") or [] for task in stage.get("tasks") or [] if task.get("status") in {"running", "awaiting_approval"}), None)
    active_call = next((item for item in calls if item.get("status") == "running"), None) or next((item for item in reversed(calls) if item.get("status") in {"failed", "interrupted"}), None)
    action_transitions = [item for item in (transitions or []) if (item.get("correlation") or {}).get("action_id")]
    partial_by_action = {
        action_id: any(
            item.get("kind") in {"workspace", "artifact_state"} and item.get("changed_paths")
            for item in action_transitions
            if (item.get("correlation") or {}).get("action_id") == action_id
        )
        for action_id in by_id
    }
    metrics = metrics or {}
    return {
        "run_failure": run.get("error"),
        "first_failed_action": ({"id": failed.get("id"), "type": failed.get("type"), "error": failed.get("error"), "partial_state_changed": partial_by_action.get(failed.get("id"), False) or bool((failed.get("receipt") or {}).get("result_refs")), "correlated_call_ids": [item.get("id") for item in calls if (item.get("correlation") or {}).get("action_id") == failed.get("id")]} if failed else None),
        "blocked_actions": blocked, "failed_calls": failed_calls,
        "active_at_failure": {"action_id": active_action.get("id") if active_action else None, "task_id": active_task.get("id") if active_task else None, "call_id": active_call.get("id") if active_call else None},
        "performance_diagnosis": {
            "sequential_bottleneck": len(calls) > 1 and float(metrics.get("parallelism_factor") or 0) <= 1.05,
            "concurrency_overlap_ms": metrics.get("overlap_saved_ms", 0),
            "retry_waste_ms": metrics.get("retry_wait_ms", 0),
            "dominant_split": max(
                ((key, float(metrics.get(key) or 0)) for key in ("llm_wall_union_ms", "local_action_ms", "approval_wait_ms", "input_wait_ms", "paused_ms", "retry_wait_ms")),
                key=lambda item: item[1], default=(None, 0),
            )[0],
        },
    }


def run_detail(workspace: Workspace, run_id: str) -> dict:
    run = agent_store.load_run(workspace, run_id)
    call_summaries = list_calls(workspace, run_id=run_id, limit=250)["items"]
    calls = [get_call(workspace, item["id"]) for item in reversed(call_summaries)]
    events = _agent_events(workspace, run_id)
    graphs = _graph_snapshots(workspace, run_id)
    transitions = _transitions(workspace, run_id=run_id)
    metrics = timing_metrics(run, calls, events)
    return {
        "run": run, "events": events, "calls": calls,
        "state_transitions": transitions,
        "graph_snapshots": graphs,
        "graph_telemetry": {"available": bool(graphs), "legacy_notice": None if graphs else ("This historical run predates immutable graph snapshots; the final saved graph is shown." if int(run.get("schema_version") or 1) >= 2 else "Schema-v1 runs use the stage/task tree and predate action graphs.")},
        "metrics": metrics,
        "causal_analysis": causal_analysis(run, calls, transitions, metrics),
        "telemetry_gaps": [] if calls else ["No raw LLM telemetry was recorded for this historical run."],
    }


def overview(workspace: Workspace) -> dict:
    call_page = list_calls(workspace, limit=250)
    calls = call_page["items"]
    runs = agent_store.list_runs(workspace)
    transitions = _transitions(workspace)
    return {
        "workspace": {"id": workspace.id, "name": workspace.name},
        "counts": {"runs": len(runs), "calls": call_page["total"], "transitions": len(transitions), "failed_calls": sum(item.get("status") in {"failed", "interrupted"} for item in calls)},
        "running_calls": [item for item in calls if item.get("status") == "running"],
        "recent_runs": runs[:10], "recent_calls": calls[:10],
        "recent_transitions": transitions[-20:][::-1],
        "telemetry_level": debug_store.telemetry_level(),
        "retention": _retention_notice(),
    }


def _retention_notice() -> str:
    """State plainly what is being recorded and what ages out.

    The console must not present a gated or pruned history as complete, so the
    notice is derived from the active level rather than being a fixed string.
    """
    level = debug_store.telemetry_level()
    caps = (
        f"The newest {debug_store.MAX_CALL_RECORDS} calls, "
        f"{debug_store.MAX_TRANSITION_RECORDS} state transitions, and "
        f"{debug_store.MAX_EVENT_LINES} events are retained locally; older "
        "records are pruned automatically."
    )
    if level == debug_store.TELEMETRY_OFF:
        return (
            "Telemetry is off, so no new records are being written. Set "
            f"{debug_store.TELEMETRY_ENV_VAR}=calls or =full to record again. "
            + caps
        )
    if level == debug_store.TELEMETRY_CALLS:
        return (
            "LLM calls and events are recorded. State snapshots and transitions "
            f"are not: set {debug_store.TELEMETRY_ENV_VAR}=full to record them, "
            "at a substantial write cost per run. " + caps
        )
    return "Full local telemetry is recorded. " + caps


def snapshot(workspace: Workspace, digest: str) -> dict:
    if len(digest) != 40 or any(ch not in "0123456789abcdef" for ch in digest):
        raise WorkspaceError("Invalid state snapshot reference.")
    row = debug_store.connection(workspace.id).execute(
        "SELECT sha1, kind, captured_at, payload FROM state_snapshots WHERE sha1 = ?",
        (digest,),
    ).fetchone()
    if row is None:
        raise WorkspaceError(f"State snapshot '{digest}' not found.")
    return {
        "sha1": row["sha1"], "kind": row["kind"], "captured_at": row["captured_at"],
        "payload": telemetry_db.loads(row["payload"]),
    }


def transition(workspace: Workspace, transition_id: str) -> dict:
    if not transition_id.startswith("transition_"):
        raise WorkspaceError("Invalid state transition reference.")
    row = debug_store.connection(workspace.id).execute(
        "SELECT record FROM state_transitions WHERE id = ?", (transition_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceError(f"State transition '{transition_id}' not found.")
    return telemetry_db.loads(row["record"], {})
