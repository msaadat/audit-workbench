"""The read-only workspace tools, as one surface two callers share.

The Ask coordinator (:mod:`.assistant`) and the steering loop
(:mod:`.agent.agent_loop`) both need to look at the engagement before they
decide anything, and they must see the same thing when they do. This module is
where the schemas and the dispatch for that live: one registry, one set of
bounds, one place a new read is added.

Only reads. Nothing here starts a run, commits an artifact, or reaches the
provider — a caller that wants to change the workspace uses its own mutating
tools, which stay with the caller that owns the policy for them.

The handler bodies still live on :class:`assistant._Session`, which also holds
the coordinator's chat-artifact channel; this module composes that session with
the artifact-index reads the action planner already had, and returns plain
JSON-safe dicts rather than the coordinator's ``(content, artifact)`` pair.
"""

from __future__ import annotations

from typing import Any

from .workspaces import Workspace

#: The reads the loop is given. Deliberately a subset of the coordinator's:
#: the loop steers capabilities and reads state, and the three compute tools
#: (``query_table``, ``run_analytics``, ``run_python``) answer a different kind
#: of question — they preview real rows and produce chat artifacts, neither of
#: which a loop turn has anywhere to put. A loop that needs data analysed runs
#: the analysis capability, which commits its answer.
LOOP_READ_TOOL_NAMES = (
    "get_audit_progress",
    "get_latest_run",
    "inspect_audit_artifacts",
    "search_documents",
    "list_tables",
    "describe_table",
    "get_table_schemas",
    "get_table_profile",
    "list_artifacts",
    "get_artifact",
)

#: Labels for the activity strip, keyed by tool name.
TOOL_LABELS = {
    "get_audit_progress": "Reading audit progress",
    "get_latest_run": "Reading the latest run",
    "inspect_audit_artifacts": "Reading audit artifacts",
    "search_documents": "Searching documents",
    "list_tables": "Listing tables",
    "describe_table": "Describing a table",
    "get_table_schemas": "Reading table schemas",
    "get_table_profile": "Profiling a table",
    "list_artifacts": "Listing current artifacts",
    "get_artifact": "Reading an artifact",
}


def loop_tool_schemas() -> list[dict]:
    """Wire schemas for the reads the loop may make, in a stable order."""

    from . import assistant
    from .agent import action_tools

    available = {
        str(schema["function"]["name"]): schema
        for schema in [*assistant.TOOLS, *action_tools.TOOL_SCHEMAS]
    }
    missing = [name for name in LOOP_READ_TOOL_NAMES if name not in available]
    if missing:
        raise RuntimeError(
            "Loop read tools name schemas that are not registered: "
            + ", ".join(missing)
        )
    return [available[name] for name in LOOP_READ_TOOL_NAMES]


class ReadToolSession:
    """Dispatch for one caller's reads, with a per-request frame cache."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        chat_id: str | None = None,
        names: tuple[str, ...] = LOOP_READ_TOOL_NAMES,
    ):
        from . import assistant
        from .agent import action_tools

        self.workspace = workspace
        self.names = tuple(names)
        self._assistant = assistant._Session(workspace, chat_id=chat_id)
        # The action planner's catalog argument only feeds
        # ``get_action_definitions``, which is not one of the reads offered
        # here; the artifact reads it also owns need nothing from it.
        self._artifacts = action_tools.ActionToolSession(workspace, [])

    def dispatch(self, name: str, args: dict) -> dict[str, Any]:
        """Run one read and return its model-facing content.

        Raises :class:`WorkspaceError` (or whatever the handler raised) rather
        than swallowing it: the caller decides whether a failed read is a tool
        error handed back to the model or a run-ending fault.
        """

        from . import assistant

        if name not in self.names:
            raise LookupError(f"Unknown read tool '{name}'.")
        if name in assistant.READ_TOOL_REGISTRY:
            handler = assistant.READ_TOOL_REGISTRY[name].handler
            content, _artifact = getattr(self._assistant, handler)(args)
            return content if isinstance(content, dict) else {"result": content}
        result = self._artifacts.dispatch(name, args)
        # ``ActionToolSession`` reports failures as a payload; the loop wants
        # the same shape every other read raises with.
        if isinstance(result, dict) and set(result) == {"error"}:
            raise ValueError(str(result["error"]))
        return result


__all__ = [
    "LOOP_READ_TOOL_NAMES",
    "TOOL_LABELS",
    "ReadToolSession",
    "loop_tool_schemas",
]
