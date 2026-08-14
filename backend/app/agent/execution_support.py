"""Shared application glue for workflow execution compositions.

The domain execution adapters remain independent compositions. This module
centralizes the small mechanics they all need without introducing another
runner base class or moving application policy into the generic scheduler.
"""

from __future__ import annotations

from typing import Protocol

from ..workspaces import Workspace, load_workspace
from .context import (
    ContextBundle,
    ContextManifest,
    ContextResolver,
    ContextScope,
    supplied_source_provenance,
)
from .workflow import Capability


class ExecutionHost(Protocol):
    ws: Workspace
    run: dict

    def emit(self, type_: str, data: dict) -> None: ...

    def record_model_source(self, source: dict) -> None: ...

    def warn(self, text: str) -> None: ...


# The resolver's own phrasing for a source that had candidates and admitted
# none of them. A source with nothing to offer omits for a different reason
# ("matched no local candidates"), which is routine and must not warn.
_STARVED_SOURCE = "supplied no permitted items"


def _starved_sources(manifest: ContextManifest) -> list[str]:
    """Declared sources whose candidates all failed to fit their budget."""
    return [
        str(omission.source_id)
        for omission in (getattr(manifest, "omissions", None) or [])
        if _STARVED_SOURCE in str(getattr(omission, "reason", "") or "")
    ]


def refresh_workspace(host: ExecutionHost) -> Workspace:
    """Reload the workspace and project its revision onto the durable workflow."""

    state = host.run.setdefault("workflow", {})
    previous = int(state.get("workspace_revision") or 0)
    host.ws = load_workspace(host.ws.id)
    state["workspace_revision"] = host.ws.revision
    if host.ws.revision != previous:
        host.emit(
            "workspace_revision",
            {
                "previous_revision": previous,
                "workspace_revision": host.ws.revision,
            },
        )
    return host.ws


def workflow_scope(run: dict) -> dict:
    """Return the normalized scope persisted by workflow routing."""

    state = run.get("workflow") or {}
    scope = dict(state.get("scope") or {})
    scope.setdefault("target_refs", list(state.get("target_refs") or []))
    scope.setdefault(
        "generation_mode", state.get("generation_mode") or "reuse_existing"
    )
    return scope


def resolve_context(
    host: ExecutionHost,
    resolver: ContextResolver,
    capability: Capability,
    unit: dict,
    scope: ContextScope,
) -> tuple[ContextManifest, ContextBundle]:
    """Resolve declared context and record the sources actually supplied."""

    manifest, bundle = resolver.resolve(host.ws, capability, unit, scope)
    for source in supplied_source_provenance(host.ws, manifest):
        host.record_model_source(source)
    # Degradation is acceptable; silent degradation is not. A capability that
    # declared a source, built candidates for it, and then ran without any of
    # them has quietly lost evidence it was designed around — which is how a
    # planning turn came to describe populations it had never been shown. The
    # omission is already in the manifest and the narration; this raises it to
    # a run warning so it survives into the record an auditor reads.
    for source_id in _starved_sources(manifest):
        host.warn(
            f"{capability.title}: context source '{source_id}' had candidates "
            "but none fitted its budget, so the turn ran without it."
        )
    return manifest, bundle
