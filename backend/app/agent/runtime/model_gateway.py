"""Model-call boundary for durable agent work.

This module defines the public contract only.  ``BaseRunner`` remains the
active implementation until the provider behavior is extracted in P3.2.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ModelGateway(Protocol):
    """Execute one budgeted, attributed model turn.

    Callers own prompt construction and response parsing.  The implementation
    owns provider selection, concurrency, retries, budget charging, telemetry,
    and hash-only provenance.
    """

    def complete(
        self,
        system: str,
        user: str,
        activity: dict[str, Any] | None = None,
        *,
        attempt: int = 1,
    ) -> str: ...
