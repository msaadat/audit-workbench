"""Durable per-run services shared by agent schedulers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from .. import store

DEFAULT_MAX_MODEL_TURNS = 40
DEFAULT_MAX_RUNTIME_SECONDS = 1800


class Cancelled(Exception):
    """Stop the active run at its next runtime checkpoint."""


class LimitExceeded(Exception):
    """Stop work when a durable run budget or deadline is exhausted."""


@runtime_checkable
class RunRuntime(Protocol):
    """Provide durable state, events, timing, and user-facing projections."""

    def save(self) -> None: ...

    def emit(self, type_: str, data: dict[str, Any]) -> None: ...

    def utcnow(self) -> str: ...

    def mark_started(self) -> str: ...

    def mark_finished(self) -> str: ...

    def set_status(self, status: str) -> None: ...

    def set_activity(
        self,
        phase: str,
        label: str,
        *,
        detail: str | None = None,
        current: int | None = None,
        total: int | None = None,
        attempt: int | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
    ) -> None: ...

    def set_model_wait(
        self,
        phase: str,
        label: str,
        *,
        started: bool,
        attempt: int = 1,
    ) -> None: ...

    def warn(self, text: str) -> None: ...

    @property
    def deadline(self) -> float: ...

    @deadline.setter
    def deadline(self, value: float) -> None: ...

    def update_limits(
        self,
        updates: dict[str, int],
        *,
        grow_only: bool = False,
    ) -> dict[str, Any]: ...

    def reserve_model_turn(
        self,
        *,
        request_characters: int,
        estimated_input_tokens: int,
        attempt: int = 1,
    ) -> dict[str, int]: ...

    def record_model_usage(
        self,
        *,
        worker: str,
        request_characters: int,
        estimated_input_tokens: int,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        attempt: int,
        context_metrics: Any = None,
        budget: dict[str, int],
    ) -> bool: ...

    def checkpoint(
        self,
        *,
        drain_inbox: Callable[[], None] | None = None,
    ) -> None: ...

    def drain_inbox(self, *, queue_commands: bool = False) -> None: ...

    def wait_for_input(self, question: str) -> str: ...

    def request_approval(
        self,
        kind: str,
        task: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


class DefaultRunRuntime:
    """Own durable state, budgets, projections, and per-run controls."""

    def __init__(
        self,
        *,
        workspace: Any,
        run: dict[str, Any],
        state_lock: threading.RLock,
        clock: Callable[[], str] | None = None,
        handle: Any | None = None,
        monotonic: Callable[[], float] | None = None,
        max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
        limit_error: type[Exception] = LimitExceeded,
        cancelled_error: type[Exception] = Cancelled,
    ):
        self.workspace = workspace
        self.run = run
        self._state_lock = state_lock
        self._clock = clock
        self.handle = handle
        self._monotonic = monotonic or time.monotonic
        self._limit_error = limit_error
        self._cancelled_error = cancelled_error
        self._deadline = self._monotonic() + max_runtime_seconds

    def save(self) -> None:
        with self._state_lock:
            store.save_run(self.workspace, self.run)

    def emit(self, type_: str, data: dict[str, Any]) -> None:
        with self._state_lock:
            store.append_event(self.workspace, self.run["id"], type_, data)

    def utcnow(self) -> str:
        return self._clock() if self._clock is not None else store.utcnow()

    def mark_started(self) -> str:
        with self._state_lock:
            if not self.run.get("started"):
                self.run["started"] = self.utcnow()
                self.save()
            return str(self.run["started"])

    def mark_finished(self) -> str:
        with self._state_lock:
            self.run["finished"] = self.utcnow()
            self.save()
            return str(self.run["finished"])

    def set_status(self, status: str) -> None:
        with self._state_lock:
            if self.run["status"] == status:
                return
            self.run["status"] = status
            self.save()
            self.emit("run_status", {"status": status})

    def set_activity(
        self,
        phase: str,
        label: str,
        *,
        detail: str | None = None,
        current: int | None = None,
        total: int | None = None,
        attempt: int | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
    ) -> None:
        """Persist one safe, user-facing description of the work in flight."""
        now = self.utcnow()
        with self._state_lock:
            previous = self.run.get("activity") or {}
            same_phase = previous.get("phase") == phase
            activity = {
                "phase": phase,
                "label": label,
                "detail": detail,
                "current": current,
                "total": total,
                "attempt": attempt,
                "task_id": task_id,
                "action_id": action_id,
                "started_at": previous.get("started_at") if same_phase else now,
                "updated_at": now,
                "waiting_on": previous.get("waiting_on") if same_phase else None,
                "model_calls_active": (
                    int(previous.get("model_calls_active") or 0) if same_phase else 0
                ),
                "model_started_at": (
                    previous.get("model_started_at") if same_phase else None
                ),
            }
            if activity["model_calls_active"] <= 0:
                activity["waiting_on"] = None
                activity["model_started_at"] = None
            self._publish_activity(activity)

    def set_model_wait(
        self,
        phase: str,
        label: str,
        *,
        started: bool,
        attempt: int = 1,
    ) -> None:
        """Project concurrent provider waits without persisting model content."""
        now = self.utcnow()
        with self._state_lock:
            activity = dict(self.run.get("activity") or {})
            if not activity:
                activity = {
                    "phase": phase,
                    "label": label,
                    "detail": None,
                    "current": None,
                    "total": None,
                    "task_id": None,
                    "action_id": None,
                    "started_at": now,
                }
            active = int(activity.get("model_calls_active") or 0)
            if started:
                active += 1
                activity["model_started_at"] = (
                    activity.get("model_started_at") or now
                )
                activity["waiting_on"] = "model"
                activity["attempt"] = attempt
            else:
                active = max(0, active - 1)
                if active == 0:
                    activity["waiting_on"] = None
                    activity["model_started_at"] = None
            activity["model_calls_active"] = active
            activity["updated_at"] = now
            usage = self.run.setdefault("usage", {})
            usage["max_concurrent_model_calls"] = max(
                int(usage.get("max_concurrent_model_calls") or 0),
                active,
            )
            self._publish_activity(activity)

    def _publish_activity(self, activity: dict[str, Any]) -> None:
        self.run["activity"] = activity
        self.run["activity_revision"] = (
            int(self.run.get("activity_revision") or 0) + 1
        )
        self.save()
        self.emit(
            "activity_update",
            {
                "activity": activity,
                "revision": self.run["activity_revision"],
            },
        )

    def warn(self, text: str) -> None:
        with self._state_lock:
            self.run.setdefault("warnings", []).append(text)
            self.save()
            self.emit("warning", {"text": text})

    @property
    def deadline(self) -> float:
        return self._deadline

    @deadline.setter
    def deadline(self, value: float) -> None:
        self._deadline = float(value)

    def update_limits(
        self,
        updates: dict[str, int],
        *,
        grow_only: bool = False,
    ) -> dict[str, Any]:
        """Apply scheduler-calculated limits through one durable boundary."""
        with self._state_lock:
            limits = self.run.setdefault("limits", {})
            for key, value in updates.items():
                normalized = int(value)
                if grow_only:
                    normalized = max(int(limits.get(key) or 0), normalized)
                limits[key] = normalized
            self.save()
            return dict(limits)

    def reserve_model_turn(
        self,
        *,
        request_characters: int,
        estimated_input_tokens: int,
        attempt: int = 1,
    ) -> dict[str, int]:
        """Charge a model request before any provider tokens are spent."""
        with self._state_lock:
            usage = self.run.setdefault("usage", {})
            limits = self.run.get("limits") or {}
            maximum_turns = int(
                limits.get("max_model_turns") or DEFAULT_MAX_MODEL_TURNS
            )
            if int(usage.get("llm_turns") or 0) >= maximum_turns:
                raise self._limit_error("model turn limit reached")
            maximum_prompt_tokens = int(
                limits.get("max_estimated_prompt_tokens")
                or maximum_turns * 10_000
            )
            projected_tokens = (
                int(usage.get("estimated_prompt_tokens") or 0)
                + estimated_input_tokens
            )
            if projected_tokens > maximum_prompt_tokens:
                raise self._limit_error("estimated prompt-token limit reached")
            maximum_completion_tokens = int(
                limits.get("max_completion_tokens") or maximum_turns * 4_000
            )
            usage["llm_turns"] = int(usage.get("llm_turns") or 0) + 1
            usage["estimated_prompt_tokens"] = projected_tokens
            usage["request_characters"] = (
                int(usage.get("request_characters") or 0) + request_characters
            )
            if attempt > 1:
                usage["retries"] = int(usage.get("retries") or 0) + 1
            self.save()
            return {
                "max_prompt_tokens": maximum_prompt_tokens,
                "max_completion_tokens": maximum_completion_tokens,
            }

    def record_model_usage(
        self,
        *,
        worker: str,
        request_characters: int,
        estimated_input_tokens: int,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        attempt: int,
        context_metrics: Any = None,
        budget: dict[str, int],
    ) -> bool:
        """Reconcile provider usage and indicate whether a token cap was crossed."""
        with self._state_lock:
            usage = self.run.setdefault("usage", {})
            usage["prompt_tokens"] = (
                int(usage.get("prompt_tokens") or 0) + prompt_tokens
            )
            usage["completion_tokens"] = (
                int(usage.get("completion_tokens") or 0) + completion_tokens
            )
            token_budget_exceeded = (
                usage["prompt_tokens"] > int(budget["max_prompt_tokens"])
                or usage["completion_tokens"]
                > int(budget["max_completion_tokens"])
            )
            usage["model_calls_by_worker"] = dict(
                usage.get("model_calls_by_worker") or {}
            )
            usage["model_calls_by_worker"][worker] = int(
                usage["model_calls_by_worker"].get(worker) or 0
            ) + 1
            worker_totals = usage.setdefault(
                "model_usage_by_worker", {}
            ).setdefault(
                worker,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "request_characters": 0,
                    "latency_ms": 0.0,
                    "retries": 0,
                },
            )
            worker_totals["calls"] += 1
            worker_totals["prompt_tokens"] += prompt_tokens
            worker_totals["completion_tokens"] += completion_tokens
            worker_totals["request_characters"] += request_characters
            worker_totals["latency_ms"] = round(
                float(worker_totals["latency_ms"]) + latency_ms,
                3,
            )
            worker_totals["retries"] += int(attempt > 1)
            usage.setdefault("model_call_metrics", []).append(
                {
                    "worker": worker,
                    "request_characters": request_characters,
                    "estimated_input_tokens": estimated_input_tokens,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "latency_ms": latency_ms,
                    "retry_number": attempt,
                    "context_metrics": context_metrics,
                }
            )
            self.save()
            return token_budget_exceeded

    def checkpoint(
        self,
        *,
        drain_inbox: Callable[[], None] | None = None,
    ) -> None:
        """Honor cancellation and pause controls, then enforce the deadline."""
        if self.handle is None:
            raise RuntimeError("RunRuntime checkpoint requires a live run handle")
        if self.handle.cancel.is_set():
            raise self._cancelled_error()
        if self.handle.pause_requested.is_set():
            self.set_status("paused")
            waited_from = self._monotonic()
            while not self.handle.resume.wait(0.2):
                if self.handle.cancel.is_set():
                    raise self._cancelled_error()
            self.handle.resume.clear()
            self.handle.pause_requested.clear()
            self._deadline += self._monotonic() - waited_from
            self.set_status("executing")
        (drain_inbox or self.drain_inbox)()
        if self._monotonic() > self._deadline:
            raise self._limit_error("run time limit reached")

    def drain_inbox(self, *, queue_commands: bool = False) -> None:
        """Move pending steering or follow-up commands into durable run state."""
        if self.handle is None:
            return
        if queue_commands:
            with self.handle.lock:
                pending, self.handle.command_queue = (
                    self.handle.command_queue[:],
                    [],
                )
            self.handle.command_queued.clear()
            if pending:
                self.run.setdefault("pending_commands", []).extend(pending)
                self.save()
            return
        with self.handle.lock:
            pending, self.handle.inbox = self.handle.inbox[:], []
        for content in pending:
            self.run.setdefault("messages", []).append(
                {
                    "role": "user",
                    "content": content,
                    "at": self.utcnow(),
                    "handled": True,
                }
            )
        if pending:
            self.save()
