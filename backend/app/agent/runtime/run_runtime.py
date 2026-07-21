"""Durable per-run services shared by agent schedulers."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from .. import store


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

    def checkpoint(self) -> None: ...

    def wait_for_input(self, question: str) -> str: ...

    def request_approval(
        self,
        kind: str,
        task: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


class DefaultRunRuntime:
    """Own the active durable state, event, status, and activity operations."""

    def __init__(
        self,
        *,
        workspace: Any,
        run: dict[str, Any],
        state_lock: threading.RLock,
        clock: Callable[[], str] | None = None,
    ):
        self.workspace = workspace
        self.run = run
        self._state_lock = state_lock
        self._clock = clock

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
