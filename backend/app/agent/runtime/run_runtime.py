"""Durable per-run service boundary for agent schedulers.

This module defines the public contract only.  ``BaseRunner`` remains the
active implementation until the runtime behavior is extracted in P3.3-P3.5.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RunRuntime(Protocol):
    """Provide durable state, controls, and auditor checkpoints for one run."""

    def save(self) -> None: ...

    def emit(self, type_: str, data: dict[str, Any]) -> None: ...

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

    def warn(self, text: str) -> None: ...

    def checkpoint(self) -> None: ...

    def wait_for_input(self, question: str) -> str: ...

    def request_approval(
        self,
        kind: str,
        task: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...
