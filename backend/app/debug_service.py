"""Read models, timing metrics, and deterministic causal analysis for Debug."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from . import debug_store
from .agent import store as agent_store
from .workspaces import Workspace, WorkspaceError


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Debug record '{path.name}' is unreadable.") from error


def _dt(value: str | None) -> datetime | None:
    if not value: return None
    try: return datetime.fromisoformat(value)
    except (TypeError, ValueError): return None


def _ms(start: str | None, end: str | None) -> float:
    left, right = _dt(start), _dt(end)
    if not left or not right: return 0.0
    return max(0.0, (right - left).total_seconds() * 1000)


def _page(items: list, cursor: int, limit: int) -> dict:
    cursor = max(0, int(cursor or 0)); limit = min(250, max(1, int(limit or 100)))
    selected = items[cursor:cursor + limit]
    return {"items": selected, "cursor": cursor,
            "next_cursor": cursor + len(selected) if cursor + len(selected) < len(items) else None,
            "total": len(items)}


def list_calls(workspace: Workspace, *, cursor: int = 0, limit: int = 100,
               run_id: str | None = None, status: str | None = None,
               stage: str | None = None, purpose: str | None = None) -> dict:
    debug_store.recover_interrupted(workspace.id)
    root = debug_store.debug_root(workspace.id) / "LLMCalls"
    items = []
    if root.exists():
        for path in root.glob("*.json"):
            try: item = _read(path)
            except WorkspaceError: continue
            correlation = item.get("correlation") or {}
            if run_id and correlation.get("run_id") != run_id: continue
            if status and item.get("status") != status: continue
            if stage and correlation.get("stage") != stage: continue
            if purpose and correlation.get("purpose") != purpose: continue
            items.append({
                "id": item["id"], "status": item.get("status"),
                "started_at": item.get("started_at"), "finished_at": item.get("finished_at"),
                "duration_ms": item.get("duration_ms"), "provider": item.get("provider"),
                "model": item.get("model"), "profile": item.get("profile"),
                "finish_reason": item.get("finish_reason"), "usage": item.get("usage"),
                "attempt_count": len(item.get("attempts") or []),
                "retry_count": max(0, len(item.get("attempts") or []) - 1),
                "error": item.get("terminal_error"), "correlation": correlation,
                "request_size_bytes": item.get("request_size_bytes"),
                "response_size_bytes": item.get("response_size_bytes"),
            })
    items.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    return _page(items, cursor, limit)


def get_call(workspace: Workspace, call_id: str) -> dict:
    if not call_id.startswith("call_"):
        raise WorkspaceError("Invalid debug call reference.")
    path = debug_store.debug_root(workspace.id) / "LLMCalls" / f"{call_id}.json"
    if not path.exists(): raise WorkspaceError(f"Debug call '{call_id}' not found.")
    return _read(path)


def list_events(workspace: Workspace, *, cursor: int = 0, limit: int = 100,
                type_: str | None = None, run_id: str | None = None,
                call_id: str | None = None) -> dict:
    path = debug_store.debug_root(workspace.id) / "events.jsonl"
    items = []
    if path.exists():
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip(): continue
            try: item = json.loads(line)
            except json.JSONDecodeError: continue
            item["seq"] = index + 1
            data = item.get("data") or {}
            correlation = data.get("correlation") or {}
            if type_ and item.get("type") != type_: continue
            if run_id and correlation.get("run_id") != run_id: continue
            if call_id and data.get("call_id") != call_id: continue
            items.append(item)
    return _page(items, cursor, limit)


def _transitions(workspace: Workspace, *, run_id: str | None = None) -> list[dict]:
    root = debug_store.debug_root(workspace.id) / "StateTransitions"
    result = []
    if root.exists():
        for path in root.glob("*.json"):
            try: item = _read(path)
            except WorkspaceError: continue
            if run_id and (item.get("correlation") or {}).get("run_id") != run_id: continue
            result.append(item)
    return sorted(result, key=lambda item: item.get("at") or "")


def _graph_snapshots(workspace: Workspace, run_id: str) -> list[dict]:
    root = debug_store.debug_root(workspace.id) / "GraphSnapshots" / run_id
    if not root.exists(): return []
    return [_read(path) for path in sorted(root.glob("*.json"))]


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
        "retention": "Full local telemetry is retained until explicitly cleared or the workspace is deleted.",
    }


def snapshot(workspace: Workspace, digest: str) -> dict:
    if len(digest) != 40 or any(ch not in "0123456789abcdef" for ch in digest): raise WorkspaceError("Invalid state snapshot reference.")
    path = debug_store.debug_root(workspace.id) / "StateSnapshots" / f"{digest}.json"
    if not path.exists(): raise WorkspaceError(f"State snapshot '{digest}' not found.")
    return _read(path)


def transition(workspace: Workspace, transition_id: str) -> dict:
    if not transition_id.startswith("transition_"): raise WorkspaceError("Invalid state transition reference.")
    path = debug_store.debug_root(workspace.id) / "StateTransitions" / f"{transition_id}.json"
    if not path.exists(): raise WorkspaceError(f"State transition '{transition_id}' not found.")
    return _read(path)
