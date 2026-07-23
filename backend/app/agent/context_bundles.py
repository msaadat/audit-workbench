"""Intentional, budgeted context builders for audit workflow workers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .. import analytics, assistant, data_tests, doc_tests, document_analysis, findings, validation
from ..workspaces import Workspace, WorkspaceError

CHARACTER_BUDGETS = {
    "command_router": 6_000,
    "rcm": 80_000,
    "planned_test_generation": 60_000,
    "data_test_spec": 22_000,
    "document_test_spec": 22_000,
    "document_qa_execution": 30_000,
    "finding_draft": 16_000,
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


def rcm(basis: dict, *, template: str, apm_markdown: str, current_rows: list[dict]) -> ContextBundle:
    projection = planning_basis_projection(basis)
    sections = {
        "template": template,
        "apm": apm_markdown,
        "planning_basis": projection,
        "CURRENT RCM TO REVISE": current_rows,
    }
    try:
        return _bundle("rcm", sections)
    except WorkspaceError:
        projection["tables"] = [
            {"table": item.get("table"), "rows": item.get("rows"), "columns": [column.get("name") for column in item.get("columns") or []]}
            for item in projection.get("tables") or []
        ]
        return _bundle("rcm", sections, reducer_ran=True)


def _planning_scope(workspace: Workspace) -> dict:
    context = workspace.planning.get("context") or {}
    return {
        key: context.get(key)
        for key in ("objective", "scope", "period", "materiality", "entity")
    }


def _terms(*values: object) -> set[str]:
    return {
        token
        for value in values
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if len(token) >= 3
    }


def _select_metadata(items: list[dict], terms: set[str], *, limit: int) -> tuple[list[dict], list[dict]]:
    scored = []
    for index, item in enumerate(items):
        searchable = _terms(
            item.get("id"), item.get("table"), item.get("title"), item.get("source"),
            item.get("category"), [column.get("name") for column in item.get("columns") or []],
        )
        scored.append((len(terms & searchable), index, item))
    matches = [entry for entry in scored if entry[0] > 0]
    chosen = sorted(matches or scored, key=lambda entry: (-entry[0], entry[1]))[:limit]
    selected_indexes = {entry[1] for entry in chosen}
    omitted = [
        {
            key: item.get(key)
            for key in ("id", "table", "title", "source", "category", "rows", "pages", "text_state")
            if item.get(key) is not None
        }
        for index, item in enumerate(items)
        if index not in selected_indexes
    ]
    return [entry[2] for entry in chosen], omitted


def planned_test(workspace: Workspace, row: dict, basis: dict | None = None) -> ContextBundle:
    other_rows = [
        {"id": item.get("id"), "semantic_id": item.get("semantic_id"), "risk": item.get("risk")}
        for item in workspace.rcm if item.get("id") != row.get("id")
    ]
    terms = _terms(row.get("risk"), row.get("control"), row.get("test_procedure"), row.get("criteria"))
    table_inventory = assistant.schema_brief(workspace)
    document_inventory = [
        {key: item.get(key) for key in ("id", "title", "category", "text_state", "pages", "sha1")}
        for item in workspace.documents
    ]
    selected_tables, omitted_tables = _select_metadata(table_inventory, terms, limit=6)
    selected_documents, omitted_documents = _select_metadata(document_inventory, terms, limit=12)
    sections = {
        "rcm_row": row,
        "apm_scope": _planning_scope(workspace),
        "methodology": (basis or {}).get("methodology") or [],
        "available_tables": selected_tables,
        "available_documents": selected_documents,
        "available_but_not_included": {
            "tables": omitted_tables,
            "documents": omitted_documents,
        },
        "duplicate_avoidance_index": other_rows,
    }
    return _bundle("planned_test_generation", sections)


def data_test_spec(workspace: Workspace, row: dict, planned: dict) -> ContextBundle:
    schemas = assistant.schema_brief(workspace)
    terms = _terms(
        row.get("risk"), row.get("control"), planned.get("title"), planned.get("objective"),
        planned.get("criteria"), planned.get("steps"), planned.get("expected_evidence"),
    )
    selected_schemas, omitted_schemas = _select_metadata(schemas, terms, limit=6)
    sections = {
        "rcm_row": {key: row.get(key) for key in ("id", "risk", "control", "criteria", "risk_rating")},
        "planned_test": planned,
        "table_schemas": selected_schemas,
        "available_but_not_included": {"tables": omitted_schemas},
        "analytics_registry": analytics.registry_payload(),
        "validation_registry": validation.registry_payload(),
        "data_test_schema": {
            "required": ["title", "objective", "engine", "table_refs", "spec"],
            "engines": ["analytics", "validation", "polars"],
            "analytics_spec": {
                "test_id": "exact id from analytics_registry",
                "params": "object matching that registry entry",
            },
            "validation_spec": {
                "rules": [
                    {
                        "check": "exact id from validation_registry",
                        "column": "exact column name when the check is column-scoped",
                        "params": "object matching that registry entry",
                    }
                ],
                "sql_supported": False,
            },
            "polars_spec": {"code": "safe Polars assigning a DataFrame to result"},
        },
        "current_matching_tests": [
            item for item in workspace.data_tests if item.get("planned_test_id") == planned.get("id")
        ],
    }
    try:
        return _bundle("data_test_spec", sections)
    except WorkspaceError:
        # Preserve exact IDs and parameter contracts but remove UI prose.
        sections["analytics_registry"] = [
            {"id": item.get("id"), "params": item.get("params") or []}
            for item in sections["analytics_registry"]
        ]
        sections["validation_registry"] = [
            {"id": item.get("id"), "scope": item.get("scope"), "params": item.get("params") or []}
            for item in sections["validation_registry"]
        ]
        return _bundle("data_test_spec", sections, reducer_ran=True)


def document_test_spec(workspace: Workspace, row: dict, planned: dict) -> ContextBundle:
    documents = [
        {key: item.get(key) for key in ("id", "title", "category", "source", "pages", "text_state", "sha1")}
        for item in workspace.documents
    ]
    terms = _terms(
        row.get("risk"), row.get("control"), planned.get("title"), planned.get("objective"),
        planned.get("criteria"), planned.get("steps"), planned.get("expected_evidence"),
    )
    selected_documents, omitted_documents = _select_metadata(documents, terms, limit=12)
    selected_ids = {str(item.get("id")) for item in selected_documents}
    analyses = []
    for document in workspace.documents:
        if str(document.get("id")) not in selected_ids:
            continue
        compact = document_analysis.compact_artifact(workspace, document["id"])
        if compact:
            analyses.append({
                "document_id": document["id"],
                "analysis_id": compact.get("analysis_id"),
                "coverage": compact.get("coverage"),
                "citations": [
                    {key: citation.get(key) for key in ("id", "page", "excerpt")}
                    for citation in (compact.get("citations") or [])[:12]
                ],
            })
    return _bundle(
        "document_test_spec",
        {
            "rcm_row": {key: row.get(key) for key in ("id", "risk", "control", "criteria", "risk_rating")},
            "planned_test": planned,
            "documents": selected_documents,
            "available_but_not_included": {"documents": omitted_documents},
            "document_analysis_manifest": analyses,
            "supported_builders": ["vouching", "attribute", "review", "qa"],
            "comparison_methods": sorted(doc_tests.METHODS),
            "required_item_contracts": {
                "vouching": {
                    "required": ["label", "document_ids", "checks"],
                    "check": ["field", "expected", "method"],
                },
                "attribute": {
                    "required": ["label", "document_ids", "attributes"],
                },
                "review": "items need a document page/excerpt/summary",
                "qa": "items need document_ids and a question",
            },
        },
    )


def finding(workspace: Workspace, observation: dict) -> ContextBundle:
    row, planned = workspace.planned_test(str(observation.get("planned_test_id") or ""))
    execution_ref = str(observation.get("execution_ref") or "")
    kind, _, source_id = execution_ref.partition(":")
    execution = None
    if kind == "datatest":
        artifact = data_tests.result_artifact(workspace, source_id)
        if artifact:
            result = artifact["item"]
            execution = {
                key: result.get(key)
                for key in (
                    "id", "status", "verdict", "exception_count", "semantic_valid",
                    "dataset_fingerprints", "source_sha1", "result_sha1", "statistics",
                    "verdict_text", "semantic_issues", "error",
                )
            }
    elif kind == "doctest" and doc_tests.exists(workspace, source_id):
        test = doc_tests.load_test(workspace, source_id)
        execution = {
            "id": test.get("id"),
            "status": test.get("status"),
            "sha1": test.get("sha1"),
            "rollup": doc_tests.result_rollup(test),
            "items": [
                {
                    **{
                        key: item.get(key)
                        for key in ("id", "label", "state", "auditor_disposition")
                    },
                    "check_verdicts": {
                        verdict: sum(
                            check.get("verdict") == verdict
                            for check in item.get("checks") or []
                        )
                        for verdict in ("match", "mismatch", "missing", "pending")
                    },
                }
                for item in (test.get("items") or [])
            ],
        }
    anchor = findings.anchor_from_ref(workspace, execution_ref)
    return _bundle(
        "finding_draft",
        {
            "observation": observation,
            "rcm_row": {key: row.get(key) for key in ("id", "risk", "control", "criteria", "risk_rating")},
            "planned_test": {key: planned.get(key) for key in ("id", "title", "objective", "criteria", "method", "result_summary", "scope_limitations")},
            "execution_ref": execution_ref,
            "immutable_execution_result": execution,
            "evidence_anchor": anchor,
            "required_output": ["title", "severity", "condition", "criteria", "cause", "cause_pending", "effect", "recommendation", "severity_rationale"],
        },
    )
