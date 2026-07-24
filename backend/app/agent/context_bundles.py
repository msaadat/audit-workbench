"""Intentional, budgeted context builders for audit workflow workers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..workspaces import Workspace, WorkspaceError

CHARACTER_BUDGETS = {
    "command_router": 6_000,
    "document_qa_execution": 30_000,
    "finding_report_section": 12_000,
    "report": 80_000,
}


@dataclass(frozen=True)
class ContextBundle:
    worker_kind: str
    sections: dict[str, Any]
    character_budget: int
    section_characters: dict[str, int]
    total_characters: int
    reducer_ran: bool = False

    def serialized(self) -> str:
        return json.dumps(self.sections, indent=1, default=str)

    def metrics(self) -> dict:
        return {
            "worker_kind": self.worker_kind,
            "character_budget": self.character_budget,
            "section_characters": dict(self.section_characters),
            "total_characters": self.total_characters,
            "estimated_tokens": max(1, self.total_characters // 4),
            "context_reducer_ran": self.reducer_ran,
        }


def _bundle(worker_kind: str, sections: dict[str, Any], *, reducer_ran: bool = False) -> ContextBundle:
    budget = CHARACTER_BUDGETS[worker_kind]
    sizes = {
        key: len(json.dumps(value, sort_keys=True, default=str))
        for key, value in sections.items()
    }
    total = len(json.dumps(sections, indent=1, default=str))
    if total > budget:
        raise WorkspaceError(
            f"The {worker_kind} context is {total:,} characters, above its "
            f"{budget:,}-character budget. Narrow the selected sources."
        )
    return ContextBundle(worker_kind, sections, budget, sizes, total, reducer_ran)


def command_router(
    command: dict,
    workflow_state: dict,
    capabilities: list[str],
    *,
    permission_mode: str,
) -> ContextBundle:
    """The router deliberately excludes schemas, profiles, registries and artifacts."""
    counts = {
        capability: {
            key: value
            for key, value in state.items()
            if key in {"state", "artifact_count", "ready", "total", "missing", "eligible", "blocking_on", "reasons"}
        }
        for capability, state in workflow_state.items()
    }
    return _bundle(
        "command_router",
        {
            "command": {
                "text": str(command.get("text") or "")[:2_000],
                "context_refs": list(command.get("context_refs") or [])[:20],
            },
            "supported_outcomes": capabilities,
            "workflow_state": counts,
            "permission_mode": permission_mode,
        },
    )


def planning_basis_projection(basis: dict) -> dict:
    return {
        "planning": basis.get("planning") or {},
        "tables": basis.get("tables") or [],
        "documents": basis.get("documents") or [],
        "methodology": basis.get("methodology") or [],
        "document_sources": [
            {
                "document_id": item.get("document_id"),
                "title": item.get("title"),
                "source_sha1": item.get("source_sha1"),
                "analysis_id": item.get("analysis_id"),
                "coverage": item.get("coverage"),
                "citations": item.get("citations"),
            }
            for item in basis.get("document_analyses") or []
        ],
    }
