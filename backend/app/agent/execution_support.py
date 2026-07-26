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
    return manifest, bundle
