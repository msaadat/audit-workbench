"""Domain adapters that populate local context-resolver candidate scopes.

These functions translate existing document and methodology context builders
to the generic data-only candidate contract.  They do not select sources,
enforce context policy, call a model, or duplicate domain retrieval logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ... import assistant, doc_tests, document_context, methodology, templates_store
from ...workspaces import Workspace, WorkspaceError
from .resolver import ContextCandidate, ContextScope


APM_DOCUMENT_SOURCE_ID = "documents"
APM_METHODOLOGY_SOURCE_ID = "methodology"
APM_TABLE_METADATA_SOURCE_ID = "table_metadata"
APM_TABLE_PROFILE_SOURCE_ID = "table_profiles"
APM_PLANNING_SOURCE_ID = "planning_context"
APM_TEMPLATE_SOURCE_ID = "apm_template"
APM_CURRENT_ARTIFACT_SOURCE_ID = "current_apm"


def _normalized_document_ids(
    workspace: Workspace,
    document_ids: Iterable[str] | None,
) -> tuple[str, ...]:
    available = {str(item.get("id")): item for item in workspace.documents}
    requested = (
        tuple(available)
        if document_ids is None
        else tuple(dict.fromkeys(str(value).strip() for value in document_ids if str(value).strip()))
    )
    missing = [document_id for document_id in requested if document_id not in available]
    if missing:
        raise WorkspaceError(f"Document '{missing[0]}' not found.")
    return requested


def apm_document_candidates(
    workspace: Workspace,
    *,
    document_ids: Iterable[str] | None = None,
) -> tuple[ContextCandidate, ...]:
    """Expose current bounded document analyses as APM candidates."""
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}
    candidates = []
    for document_id in _normalized_document_ids(workspace, document_ids):
        document = documents_by_id[document_id]
        context = document_context.apm_document_context(workspace, document_id)
        representations = (
            {"summary": context["content"]}
            if context.get("outcome") == "supplied" and context.get("content")
            else {}
        )
        metadata = {
            "document_id": document_id,
            "title": document.get("title") or document.get("source") or document_id,
            "source": document.get("source") or "",
            "category": document.get("category") or "",
            "text_state": document.get("text_state") or "",
            "analysis_id": context.get("analysis_id"),
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"document:{document_id}",
                source={
                    **metadata,
                    "source_sha1": document.get("sha1"),
                    "analysis_id": context.get("analysis_id"),
                },
                representations=representations,
                metadata=metadata,
                lexical_text="\n".join(
                    str(value or "")
                    for value in (
                        metadata["title"],
                        metadata["source"],
                        metadata["category"],
                        context.get("content"),
                    )
                ),
            )
        )
    return tuple(candidates)


def apm_methodology_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """Expose the methodology module's indexed sections as APM candidates."""
    candidates = []
    for section in methodology.context_sections(workspace):
        source_ref = (
            f"methodology:{section['scope']}:{section['pack_id']}:"
            f"{int(section['section_index'])}"
        )
        metadata = {
            "pack_id": section["pack_id"],
            "pack_name": section["pack_name"],
            "scope": section["scope"],
            "version": section["version"],
            "section": section["section"],
            "section_index": section["section_index"],
        }
        candidates.append(
            ContextCandidate(
                source_ref=source_ref,
                source={
                    **metadata,
                    "sha1": section["sha1"],
                },
                representations={"excerpt": section["text"]},
                metadata=metadata,
                lexical_text="\n".join(
                    str(value or "")
                    for value in (
                        section["pack_name"],
                        section["section"],
                        section["text"],
                    )
                ),
            )
        )
    return tuple(candidates)


def apm_table_metadata_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """Expose schema-only table metadata through the existing assistant builder."""
    candidates = []
    for table in assistant.schema_brief(workspace):
        table_name = str(table.get("table") or "").strip()
        if not table_name or table.get("error"):
            continue
        columns = [str(column.get("name") or "") for column in table.get("columns") or []]
        candidates.append(
            ContextCandidate(
                source_ref=f"table:{table_name}",
                source=table,
                representations={"table_metadata": table},
                metadata={"table": table_name},
                lexical_text=" ".join((table_name, *columns)),
            )
        )
    return tuple(candidates)


def apm_table_profile_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """Expose bounded statistical profiles without category or row values."""
    candidates = []
    for table_name in workspace.table_names():
        try:
            profile = assistant.table_metadata(
                workspace,
                table_name,
                include_category_values=False,
            )
        except (OSError, WorkspaceError):
            continue
        columns = [str(column.get("name") or "") for column in profile.get("columns") or []]
        candidates.append(
            ContextCandidate(
                source_ref=f"table:{table_name}",
                source=profile,
                representations={"table_profile": profile},
                metadata={"table": table_name},
                lexical_text=" ".join((table_name, *columns)),
            )
        )
    return tuple(candidates)


def apm_document_methodology_scope(
    workspace: Workspace,
    *,
    planning_context: Mapping[str, object] | None = None,
    document_ids: Iterable[str] | None = None,
) -> ContextScope:
    """Build the complete local candidate scope for the live APM capability."""
    context = dict(planning_context or workspace.planning.get("context") or {})
    apm_query = " ".join(
        str(context.get(key) or "")
        for key in ("objective", "scope", "background_notes", "entity", "period")
    ).strip() or "internal audit risk controls procedures"
    planning_content = {
        "context": context,
        "ownership": {
            key: workspace.planning.get(key)
            for key in ("created_by", "agent_run_id", "updated")
        },
    }
    template = templates_store.get_template(workspace, "apm")["markdown"]
    current_apm = str(workspace.planning.get("apm_markdown") or "")
    return ContextScope(
        candidates={
            APM_PLANNING_SOURCE_ID: (
                ContextCandidate(
                    source_ref="planning:context",
                    source=planning_content,
                    representations={"planning_context": planning_content},
                    metadata={"artifact": "planning_context"},
                ),
            ),
            APM_TEMPLATE_SOURCE_ID: (
                ContextCandidate(
                    source_ref="template:apm",
                    source=template,
                    representations={"artifact_template": template},
                    metadata={"template": "apm"},
                ),
            ),
            APM_CURRENT_ARTIFACT_SOURCE_ID: (
                ContextCandidate(
                    source_ref="planning:apm",
                    source=current_apm,
                    representations={"current_artifact": current_apm},
                    metadata={"artifact": "apm"},
                ),
            ),
            APM_TABLE_METADATA_SOURCE_ID: apm_table_metadata_candidates(workspace),
            APM_TABLE_PROFILE_SOURCE_ID: apm_table_profile_candidates(workspace),
            APM_DOCUMENT_SOURCE_ID: apm_document_candidates(
                workspace,
                document_ids=document_ids,
            ),
            APM_METHODOLOGY_SOURCE_ID: apm_methodology_candidates(workspace),
        },
        selector_context={**context, "apm_query": apm_query},
    )


RCM_PLANNING_SOURCE_ID = "planning_context"
RCM_TEMPLATE_SOURCE_ID = "rcm_template"
RCM_CURRENT_APM_SOURCE_ID = "current_apm"
RCM_CURRENT_ROWS_SOURCE_ID = "current_rcm"
RCM_DOCUMENT_SOURCE_ID = "documents"
RCM_METHODOLOGY_SOURCE_ID = "methodology"
RCM_TABLE_METADATA_SOURCE_ID = "table_metadata"
RCM_TABLE_PROFILE_SOURCE_ID = "table_profiles"

# The bounded identification and matching fields supplied per current RCM row.
# Planned tests, roll-ups, and evidence references stay out of the provider
# context; row revision only needs the narrative and ownership fields.
_RCM_ROW_CONTEXT_FIELDS = (
    "id",
    "semantic_id",
    "process",
    "risk",
    "risk_rating",
    "assertion",
    "control",
    "control_type",
    "control_owner",
    "criteria",
    "test_procedure",
    "review_status",
    "created_by",
)


def rcm_current_row_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """Expose each current RCM row as one bounded revision candidate."""
    candidates = []
    for row in workspace.rcm:
        projection = {key: row.get(key) for key in _RCM_ROW_CONTEXT_FIELDS}
        candidates.append(
            ContextCandidate(
                source_ref=f"rcm:{row['id']}",
                source=projection,
                representations={"current_artifact": projection},
                metadata={"rcm_id": str(row["id"])},
            )
        )
    return tuple(candidates)


def rcm_scope(
    workspace: Workspace,
    *,
    planning_context: Mapping[str, object] | None = None,
    document_ids: Iterable[str] | None = None,
) -> ContextScope:
    """Build the complete local candidate scope for the live RCM capability."""
    context = dict(planning_context or workspace.planning.get("context") or {})
    rcm_query = " ".join(
        str(context.get(key) or "")
        for key in ("objective", "scope", "background_notes", "entity", "period")
    ).strip() or "internal audit risk controls procedures"
    planning_content = {
        "context": context,
        "ownership": {
            key: workspace.planning.get(key)
            for key in ("created_by", "agent_run_id", "updated")
        },
    }
    template = templates_store.get_template(workspace, "rcm")["markdown"]
    current_apm = str(workspace.planning.get("apm_markdown") or "")
    return ContextScope(
        candidates={
            RCM_PLANNING_SOURCE_ID: (
                ContextCandidate(
                    source_ref="planning:context",
                    source=planning_content,
                    representations={"planning_context": planning_content},
                    metadata={"artifact": "planning_context"},
                ),
            ),
            RCM_TEMPLATE_SOURCE_ID: (
                ContextCandidate(
                    source_ref="template:rcm",
                    source=template,
                    representations={"artifact_template": template},
                    metadata={"template": "rcm"},
                ),
            ),
            RCM_CURRENT_APM_SOURCE_ID: (
                ContextCandidate(
                    source_ref="planning:apm",
                    source=current_apm,
                    representations={"current_artifact": current_apm},
                    metadata={"artifact": "apm"},
                ),
            ),
            RCM_CURRENT_ROWS_SOURCE_ID: rcm_current_row_candidates(workspace),
            RCM_TABLE_METADATA_SOURCE_ID: apm_table_metadata_candidates(workspace),
            RCM_TABLE_PROFILE_SOURCE_ID: apm_table_profile_candidates(workspace),
            RCM_DOCUMENT_SOURCE_ID: apm_document_candidates(
                workspace,
                document_ids=document_ids,
            ),
            RCM_METHODOLOGY_SOURCE_ID: apm_methodology_candidates(workspace),
        },
        selector_context={**context, "rcm_query": rcm_query},
    )


PLANNED_TEST_PLANNING_SOURCE_ID = "planning_context"
PLANNED_TEST_ROW_SOURCE_ID = "rcm_row"
PLANNED_TEST_OTHER_ROWS_SOURCE_ID = "other_rcm_rows"
PLANNED_TEST_TABLE_METADATA_SOURCE_ID = "table_metadata"
PLANNED_TEST_DOCUMENT_SOURCE_ID = "documents"
PLANNED_TEST_METHODOLOGY_SOURCE_ID = "methodology"

# The bounded fields supplied for the one RCM row a planned-test unit drafts
# against. Roll-ups, evidence references, and execution state stay out; drafting
# needs the risk/control narrative and the row's current planned tests so an
# update can name a durable planned-test id.
_PLANNED_TEST_ROW_FIELDS = (
    "id",
    "semantic_id",
    "process",
    "risk",
    "risk_rating",
    "assertion",
    "control",
    "control_type",
    "control_owner",
    "criteria",
    "test_procedure",
)
_PLANNED_TEST_EXISTING_FIELDS = (
    "id",
    "semantic_id",
    "title",
    "objective",
    "criteria",
    "method",
    "steps",
    "expected_evidence",
    "sampling",
    "thresholds",
    "created_by",
)
# The duplicate-avoidance projection of every other RCM row in scope.
_PLANNED_TEST_OTHER_ROW_FIELDS = ("id", "semantic_id", "risk")


def planned_test_row_candidates(
    workspace: Workspace,
    rcm_id: str,
) -> tuple[ContextCandidate, ...]:
    """Expose the one target RCM row, with its current planned tests."""
    row = next(
        (item for item in workspace.rcm if str(item.get("id")) == str(rcm_id)), None
    )
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    projection = {
        **{key: row.get(key) for key in _PLANNED_TEST_ROW_FIELDS},
        "planned_tests": [
            {key: planned.get(key) for key in _PLANNED_TEST_EXISTING_FIELDS}
            for planned in row.get("planned_tests") or []
        ],
    }
    return (
        ContextCandidate(
            source_ref=f"rcm:{row['id']}",
            source=projection,
            representations={"current_artifact": projection},
            metadata={"rcm_id": str(row["id"])},
        ),
    )


def planned_test_other_row_candidates(
    workspace: Workspace,
    rcm_id: str,
) -> tuple[ContextCandidate, ...]:
    """Expose every other RCM row as a bounded duplicate-avoidance candidate."""
    candidates = []
    for row in workspace.rcm:
        if str(row.get("id")) == str(rcm_id):
            continue
        projection = {key: row.get(key) for key in _PLANNED_TEST_OTHER_ROW_FIELDS}
        candidates.append(
            ContextCandidate(
                source_ref=f"rcm:{row['id']}",
                source=projection,
                representations={"current_artifact": projection},
                metadata={"rcm_id": str(row["id"])},
            )
        )
    return tuple(candidates)


def planned_test_methodology_candidates(
    workspace: Workspace,
) -> tuple[ContextCandidate, ...]:
    """Expose methodology sections that carry their citation with the excerpt.

    A committed planned test persists ``methodology_refs``, so the supplied
    content includes the pack/version/section citation alongside the text rather
    than the bare excerpt string used by the APM and RCM presets. The citation
    fields come from the same indexed inventory, not a second index.
    """
    candidates = []
    for section in methodology.context_sections(workspace):
        source_ref = (
            f"methodology:{section['scope']}:{section['pack_id']}:"
            f"{int(section['section_index'])}"
        )
        metadata = {
            "pack_id": section["pack_id"],
            "pack_name": section["pack_name"],
            "scope": section["scope"],
            "version": section["version"],
            "section": section["section"],
            "section_index": section["section_index"],
        }
        content = {
            "pack_id": section["pack_id"],
            "pack_name": section["pack_name"],
            "version": section["version"],
            "sha1": section["sha1"],
            "section": section["section"],
            "citation": section["citation"],
            "text": section["text"],
        }
        candidates.append(
            ContextCandidate(
                source_ref=source_ref,
                source={**metadata, "sha1": section["sha1"]},
                representations={"excerpt": content},
                metadata=metadata,
                lexical_text="\n".join(
                    str(value or "")
                    for value in (
                        section["pack_name"],
                        section["section"],
                        section["text"],
                    )
                ),
            )
        )
    return tuple(candidates)


def planned_test_scope(
    workspace: Workspace,
    rcm_id: str,
    *,
    planning_context: Mapping[str, object] | None = None,
    document_ids: Iterable[str] | None = None,
) -> ContextScope:
    """Build the local candidate scope for one planned-test generation unit."""
    context = dict(planning_context or workspace.planning.get("context") or {})
    row = next(
        (item for item in workspace.rcm if str(item.get("id")) == str(rcm_id)), None
    )
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    planned_test_query = " ".join(
        str(value or "")
        for value in (
            row.get("process"),
            row.get("risk"),
            row.get("control"),
            row.get("test_procedure"),
            row.get("criteria"),
            context.get("objective"),
            context.get("scope"),
        )
    ).strip() or "internal audit risk controls procedures"
    planning_content = {
        "context": context,
        "ownership": {
            key: workspace.planning.get(key)
            for key in ("created_by", "agent_run_id", "updated")
        },
    }
    return ContextScope(
        candidates={
            PLANNED_TEST_PLANNING_SOURCE_ID: (
                ContextCandidate(
                    source_ref="planning:context",
                    source=planning_content,
                    representations={"planning_context": planning_content},
                    metadata={"artifact": "planning_context"},
                ),
            ),
            PLANNED_TEST_ROW_SOURCE_ID: planned_test_row_candidates(workspace, rcm_id),
            PLANNED_TEST_OTHER_ROWS_SOURCE_ID: planned_test_other_row_candidates(
                workspace, rcm_id
            ),
            PLANNED_TEST_TABLE_METADATA_SOURCE_ID: apm_table_metadata_candidates(
                workspace
            ),
            PLANNED_TEST_DOCUMENT_SOURCE_ID: apm_document_candidates(
                workspace,
                document_ids=document_ids,
            ),
            PLANNED_TEST_METHODOLOGY_SOURCE_ID: planned_test_methodology_candidates(
                workspace
            ),
        },
        selector_context={**context, "planned_test_query": planned_test_query},
    )


DATA_TEST_ROW_SOURCE_ID = "rcm_row"
DATA_TEST_PLANNED_SOURCE_ID = "planned_test"
DATA_TEST_TABLE_METADATA_SOURCE_ID = "table_metadata"
DATA_TEST_CURRENT_SOURCE_ID = "current_data_tests"

# The bounded RCM narrative a definition needs; a definition is derived from its
# planned test, so the row supplies only the risk it must address.
_DEFINITION_ROW_FIELDS = ("id", "risk", "control", "criteria", "risk_rating")
_DEFINITION_PLANNED_FIELDS = (
    "id",
    "semantic_id",
    "title",
    "objective",
    "criteria",
    "method",
    "steps",
    "expected_evidence",
    "sampling",
    "thresholds",
)
_CURRENT_DATA_TEST_FIELDS = (
    "id",
    "semantic_id",
    "title",
    "objective",
    "engine",
    "table_refs",
    "spec",
    "created_by",
)


def _definition_parents(
    workspace: Workspace,
    rcm_id: str,
    planned_test_id: str,
) -> tuple[dict, dict]:
    row, planned = workspace.planned_test(str(planned_test_id))
    if str(row.get("id")) != str(rcm_id):
        raise WorkspaceError(
            f"Planned test '{planned_test_id}' does not belong to RCM row '{rcm_id}'."
        )
    return row, planned


def _definition_parent_candidates(
    row: Mapping[str, object],
    planned: Mapping[str, object],
) -> tuple[tuple[ContextCandidate, ...], tuple[ContextCandidate, ...]]:
    row_projection = {key: row.get(key) for key in _DEFINITION_ROW_FIELDS}
    planned_projection = {key: planned.get(key) for key in _DEFINITION_PLANNED_FIELDS}
    return (
        (
            ContextCandidate(
                source_ref=f"rcm:{row['id']}",
                source=row_projection,
                representations={"current_artifact": row_projection},
                metadata={"rcm_id": str(row["id"])},
            ),
        ),
        (
            ContextCandidate(
                source_ref=f"planned_test:{planned['id']}",
                source=planned_projection,
                representations={"current_artifact": planned_projection},
                metadata={"planned_test_id": str(planned["id"])},
            ),
        ),
    )


def _definition_query(
    row: Mapping[str, object],
    planned: Mapping[str, object],
) -> str:
    return " ".join(
        str(value or "")
        for value in (
            row.get("risk"),
            row.get("control"),
            planned.get("title"),
            planned.get("objective"),
            planned.get("criteria"),
            " ".join(str(step) for step in planned.get("steps") or []),
            planned.get("expected_evidence"),
        )
    ).strip() or "internal audit test evidence"


def current_data_test_candidates(
    workspace: Workspace,
    planned_test_id: str,
) -> tuple[ContextCandidate, ...]:
    """Expose the planned test's current Data Tests as revision candidates."""
    candidates = []
    for item in workspace.data_tests:
        if str(item.get("planned_test_id") or "") != str(planned_test_id):
            continue
        projection = {key: item.get(key) for key in _CURRENT_DATA_TEST_FIELDS}
        candidates.append(
            ContextCandidate(
                source_ref=f"datatest:{item['id']}",
                source=projection,
                representations={"current_artifact": projection},
                metadata={"data_test_id": str(item["id"])},
            )
        )
    return tuple(candidates)


def data_test_spec_scope(
    workspace: Workspace,
    rcm_id: str,
    planned_test_id: str,
) -> ContextScope:
    """Build the local candidate scope for one Data Test definition unit."""
    row, planned = _definition_parents(workspace, rcm_id, planned_test_id)
    row_candidates, planned_candidates = _definition_parent_candidates(row, planned)
    return ContextScope(
        candidates={
            DATA_TEST_ROW_SOURCE_ID: row_candidates,
            DATA_TEST_PLANNED_SOURCE_ID: planned_candidates,
            DATA_TEST_TABLE_METADATA_SOURCE_ID: apm_table_metadata_candidates(workspace),
            DATA_TEST_CURRENT_SOURCE_ID: current_data_test_candidates(
                workspace, planned_test_id
            ),
        },
        selector_context={"definition_query": _definition_query(row, planned)},
    )


DOCUMENT_TEST_ROW_SOURCE_ID = "rcm_row"
DOCUMENT_TEST_PLANNED_SOURCE_ID = "planned_test"
DOCUMENT_TEST_DOCUMENT_SOURCE_ID = "documents"
DOCUMENT_TEST_CURRENT_SOURCE_ID = "current_document_tests"

# The identity and citation fields a Document Test item must be able to
# reference. Content still comes through the single document-context boundary.
_DOCUMENT_TEST_DOCUMENT_FIELDS = (
    "id",
    "title",
    "category",
    "source",
    "pages",
    "text_state",
    "sha1",
)
_MAX_DOCUMENT_TEST_CITATIONS = 12


def document_test_document_candidates(
    workspace: Workspace,
    *,
    document_ids: Iterable[str] | None = None,
) -> tuple[ContextCandidate, ...]:
    """Expose every document with the identity and citations an item may cite.

    Unlike the APM projection, a document with no current analysis is still a
    candidate: a Document Test item must be able to attach it as evidence even
    when no summary exists. Content is still composed through
    :func:`document_context.apm_document_context`, the single model-facing
    document boundary.
    """
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}
    candidates = []
    for document_id in _normalized_document_ids(workspace, document_ids):
        document = documents_by_id[document_id]
        context = document_context.apm_document_context(workspace, document_id)
        metadata = {
            "document_id": document_id,
            "title": document.get("title") or document.get("source") or document_id,
            "category": document.get("category") or "",
            "text_state": document.get("text_state") or "",
        }
        content = {
            **{key: document.get(key) for key in _DOCUMENT_TEST_DOCUMENT_FIELDS},
            "analysis_id": context.get("analysis_id"),
            "citations": [
                {key: citation.get(key) for key in ("id", "page", "excerpt")}
                for citation in (context.get("citations") or [])[
                    :_MAX_DOCUMENT_TEST_CITATIONS
                ]
            ],
            "summary": context.get("content") or "",
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"document:{document_id}",
                source=content,
                representations={"summary": content},
                metadata=metadata,
                lexical_text="\n".join(
                    str(value or "")
                    for value in (
                        metadata["title"],
                        metadata["category"],
                        content["summary"],
                    )
                ),
            )
        )
    return tuple(candidates)


def current_document_test_candidates(
    workspace: Workspace,
    planned_test_id: str,
) -> tuple[ContextCandidate, ...]:
    """Expose the planned test's current Document Tests as revision candidates."""
    candidates = []
    for summary in doc_tests.list_tests(workspace):
        if str(summary.get("planned_test_id") or "") != str(planned_test_id):
            continue
        projection = {
            key: summary.get(key)
            for key in ("id", "semantic_id", "title", "kind", "status", "created_by")
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"doctest:{summary['id']}",
                source=projection,
                representations={"current_artifact": projection},
                metadata={"document_test_id": str(summary["id"])},
            )
        )
    return tuple(candidates)


def document_test_spec_scope(
    workspace: Workspace,
    rcm_id: str,
    planned_test_id: str,
) -> ContextScope:
    """Build the local candidate scope for one Document Test definition unit."""
    row, planned = _definition_parents(workspace, rcm_id, planned_test_id)
    row_candidates, planned_candidates = _definition_parent_candidates(row, planned)
    return ContextScope(
        candidates={
            DOCUMENT_TEST_ROW_SOURCE_ID: row_candidates,
            DOCUMENT_TEST_PLANNED_SOURCE_ID: planned_candidates,
            DOCUMENT_TEST_DOCUMENT_SOURCE_ID: document_test_document_candidates(
                workspace
            ),
            DOCUMENT_TEST_CURRENT_SOURCE_ID: current_document_test_candidates(
                workspace, planned_test_id
            ),
        },
        selector_context={"definition_query": _definition_query(row, planned)},
    )


FINDING_OBSERVATION_SOURCE_ID = "observation"
FINDING_ROW_SOURCE_ID = "rcm_row"
FINDING_PLANNED_SOURCE_ID = "planned_test"
FINDING_EXECUTION_SOURCE_ID = "execution_result"

_FINDING_ROW_FIELDS = ("id", "risk", "control", "criteria", "risk_rating")
_FINDING_PLANNED_FIELDS = (
    "id",
    "title",
    "objective",
    "criteria",
    "method",
    "result_summary",
    "scope_limitations",
)
_FINDING_DATA_TEST_FIELDS = (
    "id",
    "status",
    "verdict",
    "exception_count",
    "semantic_valid",
    "dataset_fingerprints",
    "source_sha1",
    "result_sha1",
    "statistics",
    "verdict_text",
    "semantic_issues",
    "error",
)


def _finding_execution_projection(workspace: Workspace, execution_ref: str) -> dict | None:
    """Project the immutable execution result a finding must be grounded in."""
    from ... import data_tests

    kind, _separator, source_id = str(execution_ref or "").partition(":")
    if kind == "datatest":
        artifact = data_tests.result_artifact(workspace, source_id)
        if not artifact:
            return None
        result = artifact["item"]
        return {key: result.get(key) for key in _FINDING_DATA_TEST_FIELDS}
    if kind == "doctest" and doc_tests.exists(workspace, source_id):
        test = doc_tests.load_test(workspace, source_id)
        return {
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
                for item in test.get("items") or []
            ],
        }
    return None


def finding_draft_scope(workspace: Workspace, observation_id: str) -> ContextScope:
    """Build the local candidate scope for one finding-draft unit."""
    from ... import findings

    observation = next(
        (
            item
            for item in workspace.observations
            if str(item.get("id")) == str(observation_id)
        ),
        None,
    )
    if observation is None:
        raise WorkspaceError(f"Observation '{observation_id}' not found.")
    row, planned = workspace.planned_test(
        str(observation.get("planned_test_id") or "")
    )
    execution_ref = str(observation.get("execution_ref") or "")
    execution = {
        "execution_ref": execution_ref,
        "immutable_execution_result": _finding_execution_projection(
            workspace, execution_ref
        ),
        "evidence_anchor": findings.anchor_from_ref(workspace, execution_ref),
    }
    row_projection = {key: row.get(key) for key in _FINDING_ROW_FIELDS}
    planned_projection = {key: planned.get(key) for key in _FINDING_PLANNED_FIELDS}
    return ContextScope(
        candidates={
            FINDING_OBSERVATION_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"observation:{observation['id']}",
                    source=dict(observation),
                    representations={"current_artifact": dict(observation)},
                    metadata={"observation_id": str(observation["id"])},
                ),
            ),
            FINDING_ROW_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"rcm:{row['id']}",
                    source=row_projection,
                    representations={"current_artifact": row_projection},
                    metadata={"rcm_id": str(row["id"])},
                ),
            ),
            FINDING_PLANNED_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"planned_test:{planned['id']}",
                    source=planned_projection,
                    representations={"current_artifact": planned_projection},
                    metadata={"planned_test_id": str(planned["id"])},
                ),
            ),
            FINDING_EXECUTION_SOURCE_ID: (
                ContextCandidate(
                    source_ref=execution_ref or f"observation:{observation['id']}",
                    source=execution,
                    representations={"current_artifact": execution},
                    metadata={"execution_ref": execution_ref},
                ),
            ),
        },
        selector_context={
            "finding_query": " ".join(
                str(value or "")
                for value in (observation.get("summary"), row.get("risk"))
            )
        },
    )


__all__ = [
    "APM_DOCUMENT_SOURCE_ID",
    "APM_METHODOLOGY_SOURCE_ID",
    "APM_TABLE_METADATA_SOURCE_ID",
    "APM_TABLE_PROFILE_SOURCE_ID",
    "APM_PLANNING_SOURCE_ID",
    "APM_TEMPLATE_SOURCE_ID",
    "APM_CURRENT_ARTIFACT_SOURCE_ID",
    "DATA_TEST_CURRENT_SOURCE_ID",
    "DATA_TEST_PLANNED_SOURCE_ID",
    "DATA_TEST_ROW_SOURCE_ID",
    "DATA_TEST_TABLE_METADATA_SOURCE_ID",
    "DOCUMENT_TEST_CURRENT_SOURCE_ID",
    "DOCUMENT_TEST_DOCUMENT_SOURCE_ID",
    "DOCUMENT_TEST_PLANNED_SOURCE_ID",
    "DOCUMENT_TEST_ROW_SOURCE_ID",
    "FINDING_EXECUTION_SOURCE_ID",
    "FINDING_OBSERVATION_SOURCE_ID",
    "FINDING_PLANNED_SOURCE_ID",
    "FINDING_ROW_SOURCE_ID",
    "PLANNED_TEST_DOCUMENT_SOURCE_ID",
    "PLANNED_TEST_METHODOLOGY_SOURCE_ID",
    "PLANNED_TEST_OTHER_ROWS_SOURCE_ID",
    "PLANNED_TEST_PLANNING_SOURCE_ID",
    "PLANNED_TEST_ROW_SOURCE_ID",
    "PLANNED_TEST_TABLE_METADATA_SOURCE_ID",
    "RCM_CURRENT_APM_SOURCE_ID",
    "RCM_CURRENT_ROWS_SOURCE_ID",
    "RCM_DOCUMENT_SOURCE_ID",
    "RCM_METHODOLOGY_SOURCE_ID",
    "RCM_PLANNING_SOURCE_ID",
    "RCM_TABLE_METADATA_SOURCE_ID",
    "RCM_TABLE_PROFILE_SOURCE_ID",
    "RCM_TEMPLATE_SOURCE_ID",
    "apm_document_candidates",
    "apm_document_methodology_scope",
    "apm_methodology_candidates",
    "apm_table_metadata_candidates",
    "apm_table_profile_candidates",
    "current_data_test_candidates",
    "current_document_test_candidates",
    "data_test_spec_scope",
    "document_test_document_candidates",
    "document_test_spec_scope",
    "finding_draft_scope",
    "planned_test_methodology_candidates",
    "planned_test_other_row_candidates",
    "planned_test_row_candidates",
    "planned_test_scope",
    "rcm_current_row_candidates",
    "rcm_scope",
]
