"""Shared bounded workspace-metadata tools for model-facing profiles.

Profiles decide which tools they expose. This module owns the schemas and the
local implementations for metadata reads that are safe for both chat and
action planning; it intentionally contains no row-preview, query, sandbox, or
mutation capability.
"""

from __future__ import annotations

from . import model_context, profiler
from .workspaces import Workspace, WorkspaceError


CATEGORY_SAMPLE_MAX = 30


def function_tool(name: str, description: str, parameters: dict) -> dict:
    """Build one OpenAI-compatible function-tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


TABLE_SCHEMAS_TOOL = function_tool(
    "get_table_schemas",
    "Get column schemas for named tables. Omit tables to list all table schemas.",
    {
        "type": "object",
        "properties": {"tables": {"type": "array", "items": {"type": "string"}}},
    },
)

TABLE_PROFILE_TOOL = function_tool(
    "get_table_profile",
    "Get the bounded aggregate profile for one table, including observed category values where available. Never invent values not returned here.",
    {
        "type": "object",
        "properties": {"table": {"type": "string"}},
        "required": ["table"],
    },
)


def _column_meta(profile: dict, *, include_category_values: bool = True) -> dict:
    return model_context.project_column_profile(
        profile,
        category_limit=CATEGORY_SAMPLE_MAX,
        include_category_values=include_category_values,
    )


def table_profile(
    workspace: Workspace,
    table: str,
    *,
    include_category_values: bool = True,
) -> dict:
    """Return a compact profile with no row-level values."""
    if table not in workspace.table_names():
        raise WorkspaceError(f"Unknown table '{table}'.")
    profile = workspace.get_profile(table)
    return {
        "table": table,
        "rows": profile["rows"],
        "columns": [
            _column_meta(column, include_category_values=include_category_values)
            for column in profile["column_profiles"]
        ],
    }


def table_schemas(workspace: Workspace, tables: list[str] | None = None) -> list[dict]:
    """Return table/column metadata for the selected tables, in stable order."""
    requested = [str(value) for value in (tables or []) if str(value)]
    names = requested or workspace.table_names()
    available = set(workspace.table_names())
    unknown = next((name for name in names if name not in available), None)
    if unknown:
        raise WorkspaceError(f"Unknown table '{unknown}'.")
    brief = []
    for name in names:
        try:
            frame = workspace.get_frame(name)
        except Exception as error:
            brief.append({"table": name, "error": str(error)})
            continue
        brief.append(
            {
                "table": name,
                "rows": frame.height,
                "columns": [
                    {"name": column["name"], "dtype": column["dtype"], "type": column["kind"]}
                    for column in profiler.schema_payload(frame)
                ],
            }
        )
    return brief
