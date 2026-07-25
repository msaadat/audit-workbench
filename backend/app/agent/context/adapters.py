"""Domain adapters that populate local context-resolver candidate scopes.

These functions translate existing document and methodology context builders
to the generic data-only candidate contract.  They do not select sources,
enforce context policy, call a model, or duplicate domain retrieval logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from ... import (
    assistant,
    doc_tests,
    document_context,
    intake,
    methodology,
    templates_store,
)
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


def _planning_relevant(document: Mapping[str, object], curated: bool) -> bool:
    """Whether a document is planning material under the declared category rule.

    The vocabulary is the one intake already suggests planning documents from:
    background, policy, regulation, contract, minutes, prior reports, and
    correspondence, plus anything uncategorized. Explicit auditor curation is
    authoritative over the rule.
    """
    category = str(document.get("category") or "")
    return bool(
        curated or not category or category in intake.PLANNING_DOCUMENT_CATEGORIES
    )


def _document_excerpts(
    workspace: Workspace,
    document_id: str,
    query: str,
    *,
    purpose: str,
) -> str:
    """Locally retrieved passages for a document with no current analysis."""
    excerpts = document_context.get_document_context(
        workspace,
        document_id,
        "search_excerpts",
        query=query,
        purpose=purpose,
        stage=purpose,
        record_activity=False,
    )
    return str(excerpts.get("content") or "")


def apm_document_candidates(
    workspace: Workspace,
    *,
    document_ids: Iterable[str] | None = None,
    excerpt_query: str | None = None,
) -> tuple[ContextCandidate, ...]:
    """Expose bounded document material as planning candidates.

    A document with a current analysis contributes that analysis' bounded
    ``summary``. When ``excerpt_query`` is supplied, one without a current
    analysis is still a candidate through locally retrieved ``excerpt`` passages,
    so an imported document informs planning without a durable analysis having
    been generated first. Nothing here generates one.

    Each candidate also carries the ``planning_relevant`` flag the planning
    presets constrain their selectors on, so a transaction voucher or raw
    evidence file is not offered as planning material even though it is a valid
    engagement document. Explicit auditor curation overrides the rule.
    """
    curated = document_ids is not None
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}
    candidates = []
    for document_id in _normalized_document_ids(workspace, document_ids):
        document = documents_by_id[document_id]
        context = document_context.apm_document_context(workspace, document_id)
        representations: dict[str, object] = {}
        if context.get("outcome") == "supplied" and context.get("content"):
            representations = {"summary": context["content"]}
        elif excerpt_query:
            excerpt = _document_excerpts(
                workspace, document_id, excerpt_query, purpose="apm_context"
            )
            if excerpt:
                representations = {"excerpt": excerpt}
        category = str(document.get("category") or "")
        metadata = {
            "document_id": document_id,
            "title": document.get("title") or document.get("source") or document_id,
            "source": document.get("source") or "",
            "category": category,
            "text_state": document.get("text_state") or "",
            "analysis_id": context.get("analysis_id"),
            "planning_relevant": _planning_relevant(document, curated),
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
                        *representations.values(),
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


def supplied_source_provenance(
    workspace: Workspace,
    manifest: object,
) -> tuple[dict[str, object], ...]:
    """Derive document and methodology-pack provenance from one manifest.

    Manifest selections are content-free, so they carry a source reference and a
    content hash but not the document or pack identity a provenance ledger
    records. This resolves each selected reference against the same deterministic
    inventories the candidates came from, so a model turn's recorded sources are
    exactly what the resolver supplied to it.
    """
    sections = {
        (
            f"methodology:{section['scope']}:{section['pack_id']}:"
            f"{int(section['section_index'])}"
        ): section
        for section in methodology.context_sections(workspace)
    }
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}
    entries: dict[str, dict[str, object]] = {}
    for selection in getattr(manifest, "selections", ()) or ():
        source_type = getattr(selection, "source_type", "")
        source_ref = getattr(selection, "source_ref", "")
        if source_type == "methodology":
            section = sections.get(source_ref)
            if section is None:
                continue
            pack_ref = f"pack:{section['scope']}:{section['pack_id']}"
            entries.setdefault(
                pack_ref,
                {
                    "source_ref": pack_ref,
                    "document_id": None,
                    "source_sha1": section["sha1"],
                    "pages": [],
                },
            )
        elif source_type == "documents":
            document_id = str(source_ref).split(":")[1] if ":" in source_ref else ""
            document = documents_by_id.get(document_id)
            if document is None:
                continue
            entries.setdefault(
                document_id,
                {
                    "source_ref": document_id,
                    "document_id": document_id,
                    "source_sha1": document.get("sha1"),
                    "pages": [],
                },
            )
    return tuple(entries[key] for key in sorted(entries))


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
                excerpt_query=apm_query,
            ),
            APM_METHODOLOGY_SOURCE_ID: apm_methodology_candidates(workspace),
        },
        selector_context={**context, "apm_query": apm_query},
    )


PLANNING_CONTEXT_CURRENT_SOURCE_ID = "current_planning_context"
PLANNING_CONTEXT_DOCUMENT_SOURCE_ID = "planning_documents"
# Per-document share of the declared planning-document character budget, so one
# long document cannot consume the whole synthesis context.
MAX_PLANNING_CONTEXT_DOCUMENT_CHARACTERS = 5_000


def planning_context_document_candidates(
    workspace: Workspace,
    *,
    document_ids: Iterable[str] | None = None,
) -> tuple[ContextCandidate, ...]:
    """Expose bounded document material for planning-context synthesis.

    A document with a current analysis contributes that analysis' bounded
    ``summary``. One without falls back to its bounded leading ``raw_pages``
    rather than to query-matched excerpts: this capability *produces* the
    objective and scope, so at this point there is no meaningful query to
    retrieve against. Nothing here generates a document analysis.

    Each candidate carries the ``planning_relevant`` flag the declared selector
    matches on — a category rule over the same planning vocabulary intake
    suggests from. An explicitly curated document is always relevant, because the
    auditor's curation is authoritative over the rule.
    """
    curated = document_ids is not None
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}
    candidates = []
    for document_id in _normalized_document_ids(workspace, document_ids):
        document = documents_by_id[document_id]
        analysis = document_context.apm_document_context(workspace, document_id)
        if analysis.get("outcome") == "supplied" and analysis.get("content"):
            representations: dict[str, object] = {"summary": analysis["content"]}
        else:
            pages = document_context.get_document_context(
                workspace,
                document_id,
                "pages",
                max_characters=MAX_PLANNING_CONTEXT_DOCUMENT_CHARACTERS,
                purpose="planning_context",
                stage="planning_context",
                record_activity=False,
            )
            content = str(pages.get("content") or "")
            representations = {"raw_pages": content} if content else {}
        if not representations:
            continue
        category = str(document.get("category") or "")
        metadata = {
            "document_id": document_id,
            "title": document.get("title") or document.get("source") or document_id,
            "category": category,
            "text_state": document.get("text_state") or "",
            "analysis_id": analysis.get("analysis_id"),
            "planning_relevant": _planning_relevant(document, curated),
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"document:{document_id}",
                source={**metadata, "source_sha1": document.get("sha1")},
                representations=representations,
                metadata=metadata,
                lexical_text="\n".join(
                    str(value or "")
                    for value in (metadata["title"], category, *representations.values())
                ),
            )
        )
    return tuple(candidates)


def planning_context_scope(
    workspace: Workspace,
    *,
    document_ids: Iterable[str] | None = None,
) -> ContextScope:
    """Build the local candidate scope for the planning-context synthesis unit."""
    context = dict(workspace.planning.get("context") or {})
    return ContextScope(
        candidates={
            PLANNING_CONTEXT_CURRENT_SOURCE_ID: (
                ContextCandidate(
                    source_ref="planning:context",
                    source=context,
                    representations={"planning_context": context},
                    metadata={"artifact": "planning_context"},
                ),
            ),
            PLANNING_CONTEXT_DOCUMENT_SOURCE_ID: planning_context_document_candidates(
                workspace,
                document_ids=document_ids,
            ),
        },
        selector_context=dict(context),
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
                excerpt_query=rcm_query,
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
                excerpt_query=planned_test_query,
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


DOCUMENT_QA_ITEM_SOURCE_ID = "qa_item"
DOCUMENT_QA_PAGE_SOURCE_ID = "document_pages"

# The item fields a Q&A answer is derived from. Auditor dispositions, existing
# answers, and evidence anchors are deliberately excluded: the worker answers the
# question from the supplied pages and nothing else.
_DOCUMENT_QA_ITEM_FIELDS = ("id", "label", "question", "pages")


def document_qa_page_candidates(
    workspace: Workspace,
    document_id: str,
    *,
    question: str,
    pages: Iterable[int] | None = None,
) -> tuple[ContextCandidate, ...]:
    """Expose one candidate per included document page.

    Content is composed through :func:`document_context.get_document_context`,
    the single model-facing document boundary: scoped pages resolve as
    ``raw_pages`` and an unscoped question as locally retrieved ``excerpt``
    passages. Page numbers are zero-padded in the source reference so the
    deterministic ascending tie-break is page order, which is also the order a
    budget truncation should keep.
    """
    scoped = [int(value) for value in pages or []]
    mode = "pages" if scoped else "search_excerpts"
    context = document_context.get_document_context(
        workspace,
        str(document_id),
        mode,
        query=None if scoped else str(question),
        pages=scoped or None,
        purpose="document_qa",
        stage="document_qa",
        record_activity=False,
    )
    if scoped:
        included = [
            {"page": int(page["page"]), "text": str(page.get("text") or "")}
            for page in context.get("page_items") or []
        ]
        representation = "raw_pages"
    else:
        included = [
            {
                "page": int(citation["page"]),
                "text": str(citation.get("excerpt") or ""),
            }
            for citation in context.get("citations") or []
        ]
        representation = "excerpt"
    return tuple(
        ContextCandidate(
            source_ref=f"document:{document_id}:page:{page['page']:05d}",
            source=page,
            representations={representation: page},
            metadata={
                "document_id": str(document_id),
                "page": page["page"],
                "source_sha1": context.get("source_sha1") or "",
            },
        )
        for page in included
        if page["text"]
    )


def document_qa_scope(
    workspace: Workspace,
    test_id: str,
    item_id: str,
    document_id: str,
) -> ContextScope:
    """Build the local candidate scope for one document Q&A unit."""
    test = doc_tests.load_test(workspace, str(test_id))
    item = next(
        (
            value
            for value in test.get("items") or []
            if str(value.get("id")) == str(item_id)
        ),
        None,
    )
    if item is None:
        raise WorkspaceError(
            f"Document Test '{test_id}' has no item '{item_id}'."
        )
    if str(document_id) not in [str(value) for value in item.get("document_ids") or []]:
        raise WorkspaceError(
            f"Document '{document_id}' is not attached to Document Test item "
            f"'{item_id}'."
        )
    question = str(item.get("question") or "").strip()
    if not question:
        raise WorkspaceError(f"Document Test item '{item_id}' has no question.")
    projection = {
        **{key: item.get(key) for key in _DOCUMENT_QA_ITEM_FIELDS},
        "document_test_id": str(test_id),
        "document_id": str(document_id),
    }
    return ContextScope(
        candidates={
            DOCUMENT_QA_ITEM_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"docitem:{item_id}",
                    source=projection,
                    representations={"current_artifact": projection},
                    metadata={"document_test_id": str(test_id), "item_id": str(item_id)},
                ),
            ),
            DOCUMENT_QA_PAGE_SOURCE_ID: document_qa_page_candidates(
                workspace,
                str(document_id),
                question=question,
                pages=item.get("pages"),
            ),
        },
        selector_context={"document_qa_query": question},
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


# --------------------------------------------------------------------------- #
# analysis.definitions (P8.6)
# --------------------------------------------------------------------------- #
ANALYSIS_TARGET_SCHEMA_SOURCE_ID = "target_schema"
ANALYSIS_TARGET_PROFILE_SOURCE_ID = "target_profile"
ANALYSIS_TARGET_AGGREGATE_SOURCE_ID = "target_aggregates"
ANALYSIS_RELATED_FRAMES_SOURCE_ID = "related_frames"
ANALYSIS_RELATIONSHIP_SOURCE_ID = "relationship_evidence"
ANALYSIS_REGISTRY_SOURCE_ID = "analytics_registry"
ANALYSIS_CURRENT_SOURCE_ID = "current_analyses"

# Aggregate columns are bounded so a wide table cannot consume the declaration's
# whole character budget before the resolver even sees it.
MAX_AGGREGATE_COLUMNS = 24

# Profiler fields that are literal values drawn from rows. Text ``min``/``max``
# and ``top_values`` are exactly the category literals the planning presets
# already withhold, so the aggregate projection drops them; numeric and date
# summaries stay, matching the established table-profile policy.
_LITERAL_PROFILE_FIELDS = ("top_values",)


def _aggregate_column(profile: Mapping[str, object]) -> dict[str, object]:
    """One value-free column aggregate derived from the cached profile."""
    inferred = str(profile.get("inferred_type") or "")
    aggregate: dict[str, object] = {
        "column": profile.get("name"),
        "dtype": profile.get("dtype"),
        "inferred_type": inferred,
        "rows": profile.get("total"),
        "blank_count": profile.get("blank_count"),
        "blank_pct": profile.get("blank_pct"),
        "distinct_count": profile.get("distinct_count"),
        "distinct_pct": profile.get("distinct_pct"),
    }
    if inferred in {"numeric", "date"}:
        aggregate.update(
            minimum=profile.get("min"),
            maximum=profile.get("max"),
            mean=profile.get("mean"),
        )
    return aggregate


def analysis_aggregate_candidates(
    workspace: Workspace,
    target: str,
) -> tuple[ContextCandidate, ...]:
    """Bounded, value-free aggregates for one analysis target frame.

    Derived from the workspace's cached profile rather than a fresh Polars pass,
    so this adapter reuses the existing deterministic service instead of
    duplicating it. Category literals and top-value samples are dropped: an
    aggregate describes shape and distribution, never the population's values.
    """
    try:
        profile = workspace.get_profile(target)
    except (OSError, WorkspaceError):
        return ()
    columns = list(profile.get("column_profiles") or [])[:MAX_AGGREGATE_COLUMNS]
    overview = {
        "table": target,
        "scope": "table",
        "rows": profile.get("rows"),
        "columns": profile.get("columns"),
        "duplicate_rows": profile.get("duplicate_rows"),
        "sampled": profile.get("sampled"),
        "profiled_columns": len(columns),
    }
    candidates = [
        ContextCandidate(
            source_ref=f"aggregate:{target}",
            source=overview,
            representations={"table_aggregate": overview},
            metadata={"table": target, "scope": "table"},
            lexical_text=target,
        )
    ]
    for column_profile in columns:
        if any(field in column_profile for field in _LITERAL_PROFILE_FIELDS):
            column_profile = {
                key: value
                for key, value in column_profile.items()
                if key not in _LITERAL_PROFILE_FIELDS
            }
        aggregate = {"table": target, "scope": "column", **_aggregate_column(column_profile)}
        candidates.append(
            ContextCandidate(
                source_ref=f"aggregate:{target}:{aggregate['column']}",
                source=aggregate,
                representations={"table_aggregate": aggregate},
                metadata={"table": target, "column": aggregate["column"]},
                lexical_text=f"{target} {aggregate['column']}",
            )
        )
    return tuple(candidates)


def analysis_relationship_candidates(
    workspace: Workspace,
    target: str,
    relationships: Iterable[Mapping[str, object]] | None = None,
) -> tuple[ContextCandidate, ...]:
    """Deterministic join evidence involving the target frame.

    ``relationships`` are the diagnostics the relationship capability already
    recorded on the run. Nothing is re-derived here and no relationship fact is
    ever asked of the model; this only exposes the aggregate evidence for the
    joins the target participates in.
    """
    candidates = []
    for record in relationships or ():
        left = str(record.get("left") or "")
        right = str(record.get("right") or "")
        if target not in {left, right}:
            continue
        evidence = {
            "scope": "relationship",
            "left": left,
            "right": right,
            "left_on": list(record.get("left_on") or []),
            "right_on": list(record.get("right_on") or []),
            "how": record.get("how"),
            "strength": record.get("strength"),
            "materialized_join": record.get("join"),
            "diagnostics": dict(record.get("diagnostics") or {}),
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"relationship:{left}:{right}:"
                f"{'-'.join(evidence['left_on'])}:{'-'.join(evidence['right_on'])}",
                source=evidence,
                representations={"table_aggregate": evidence},
                metadata={"left": left, "right": right},
                lexical_text=f"{left} {right}",
            )
        )
    return tuple(candidates)


def analysis_definition_scope(
    workspace: Workspace,
    target: str,
    *,
    related: Iterable[str] = (),
    relationships: Iterable[Mapping[str, object]] | None = None,
    analytics_registry: object = None,
) -> ContextScope:
    """Build the local candidate scope for one analysis-definition unit.

    Every model-facing input is metadata, a bounded statistical profile, or a
    value-free aggregate. ``table_rows`` is never produced, and the declaration
    denies the permission as well, so row-level data cannot reach the worker
    even if an adapter regressed.
    """
    from ... import analytics as analytics_module

    if target not in workspace.table_names():
        raise WorkspaceError(f"Unknown table '{target}'.")
    schema = next(
        (
            item
            for item in assistant.schema_brief(workspace)
            if str(item.get("table") or "") == target and not item.get("error")
        ),
        None,
    )
    if schema is None:
        raise WorkspaceError(f"Table '{target}' has no readable schema.")
    profile_candidates = tuple(
        candidate
        for candidate in apm_table_profile_candidates(workspace)
        if candidate.metadata.get("table") == target
    )
    related_names = [
        name
        for name in dict.fromkeys(str(value) for value in related)
        if name != target and name in workspace.table_names()
    ]
    related_candidates = tuple(
        candidate
        for candidate in apm_table_metadata_candidates(workspace)
        if candidate.metadata.get("table") in set(related_names)
    )
    # A compact projection of the analytics catalog: enough for the worker to
    # name a real test with real parameters, without spending the declaration's
    # character budget on prose descriptions.
    registry = (
        analytics_registry
        if analytics_registry is not None
        else [
            {
                "id": item["id"],
                "label": item.get("label"),
                "params": [
                    {"name": parameter.get("name"), "kind": parameter.get("kind")}
                    for parameter in item.get("params") or []
                ],
            }
            for item in analytics_module.registry_payload()
        ]
    )
    current = [
        {
            key: item.get(key)
            for key in ("id", "title", "kind", "table", "spec", "semantic_id", "created_by")
        }
        for item in workspace.analyses
        if str(item.get("table") or "") == target
    ]
    return ContextScope(
        candidates={
            ANALYSIS_TARGET_SCHEMA_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"table:{target}",
                    source=schema,
                    representations={"table_metadata": schema},
                    metadata={"table": target},
                    lexical_text=target,
                ),
            ),
            ANALYSIS_TARGET_PROFILE_SOURCE_ID: profile_candidates,
            ANALYSIS_TARGET_AGGREGATE_SOURCE_ID: analysis_aggregate_candidates(
                workspace, target
            ),
            ANALYSIS_RELATED_FRAMES_SOURCE_ID: related_candidates,
            ANALYSIS_RELATIONSHIP_SOURCE_ID: analysis_relationship_candidates(
                workspace, target, relationships
            ),
            ANALYSIS_REGISTRY_SOURCE_ID: (
                ContextCandidate(
                    source_ref="registry:analytics",
                    source=registry,
                    representations={"current_artifact": registry},
                    metadata={"registry": "analytics"},
                ),
            ),
            ANALYSIS_CURRENT_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=f"analysis:{item['id']}",
                    source=item,
                    representations={"current_artifact": item},
                    metadata={"analysis_id": str(item["id"])},
                )
                for item in current
            ),
        },
        selector_context={"analysis_query": target},
    )


DOCUMENT_ANALYSIS_METADATA_SOURCE_ID = "document_metadata"
DOCUMENT_ANALYSIS_CHUNK_SOURCE_ID = "document_chunk"
DOCUMENT_ANALYSIS_CHUNKS_SOURCE_ID = "chunk_analyses"

# The document fields a chunk or reduction turn is allowed to see as
# classification context. Storage paths, sizes, and internal filenames are
# excluded; the source hash is included because a citation is bound to it.
_DOCUMENT_ANALYSIS_FIELDS = (
    "id",
    "title",
    "source",
    "category",
    "note",
    "relative_path",
)


def _document_entry(workspace: Workspace, document_id: str) -> Mapping[str, object]:
    document = next(
        (
            item
            for item in workspace.documents
            if str(item.get("id")) == str(document_id)
        ),
        None,
    )
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    return document


def document_metadata_candidate(
    workspace: Workspace, document_id: str
) -> ContextCandidate:
    """Expose one document's classification metadata as a single candidate.

    Metadata is explicitly not citation evidence: both document workers treat it
    as a fallible classification hint, and the map worker binds every citation to
    the supplied chunk text instead.
    """
    document = _document_entry(workspace, document_id)
    projection = {
        **{key: document.get(key) for key in _DOCUMENT_ANALYSIS_FIELDS},
        "document_id": str(document_id),
        "source_sha1": str(document.get("sha1") or ""),
    }
    return ContextCandidate(
        source_ref=f"document:{document_id}",
        source=projection,
        representations={"current_artifact": projection},
        metadata={
            "document_id": str(document_id),
            "category": str(document.get("category") or ""),
            "source_sha1": str(document.get("sha1") or ""),
        },
    )


def document_chunk_scope(
    workspace: Workspace,
    document_id: str,
    chunk: Mapping[str, object],
) -> ContextScope:
    """Build the local candidate scope for one document chunk-analysis unit.

    Exactly one chunk is supplied. The chunk text is the only evidence the map
    worker may cite, so no sibling chunk, retrieved passage, or generated
    orientation enters this scope — that is what makes each chunk unit
    independent enough for the scheduler to run concurrently and to resume from a
    persisted proposal without re-billing.
    """
    payload = {
        "id": str(chunk.get("id") or ""),
        "page": int(chunk.get("page") or 0),
        "pages": [int(page) for page in chunk.get("pages") or []],
        "start_character": int(chunk.get("start_character") or 0),
        "end_character": int(chunk.get("end_character") or 0),
        "text": str(chunk.get("text") or ""),
    }
    return ContextScope(
        candidates={
            DOCUMENT_ANALYSIS_METADATA_SOURCE_ID: (
                document_metadata_candidate(workspace, document_id),
            ),
            DOCUMENT_ANALYSIS_CHUNK_SOURCE_ID: (
                ContextCandidate(
                    source_ref=(
                        f"document:{document_id}:chunk:{payload['id']}"
                    ),
                    source=payload,
                    representations={"raw_pages": payload},
                    metadata={
                        "document_id": str(document_id),
                        "chunk_id": payload["id"],
                        "page": payload["page"],
                    },
                ),
            ),
        },
    )


def document_reduction_scope(
    workspace: Workspace,
    document_id: str,
    chunk_analyses: Iterable[Mapping[str, object]],
) -> ContextScope:
    """Build the local candidate scope for one document reduction unit.

    The reduction sees no raw source at all — only the validated chunk proposals
    the map worker already bound to supplied text. Chunk references are the
    stable chunk identifiers, so the deterministic ascending tie-break is chunk
    order, which is also the order a budget truncation should keep.
    """
    analyses = [dict(item) for item in chunk_analyses]
    if not analyses:
        raise WorkspaceError(
            f"Document '{document_id}' has no analyzed source chunks to consolidate."
        )
    return ContextScope(
        candidates={
            DOCUMENT_ANALYSIS_METADATA_SOURCE_ID: (
                document_metadata_candidate(workspace, document_id),
            ),
            DOCUMENT_ANALYSIS_CHUNKS_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=(
                        f"document:{document_id}:chunk:{item.get('chunk_id') or ''}"
                    ),
                    source=item,
                    representations={"summary": item},
                    metadata={
                        "document_id": str(document_id),
                        "chunk_id": str(item.get("chunk_id") or ""),
                    },
                )
                for item in sorted(
                    analyses, key=lambda value: str(value.get("chunk_id") or "")
                )
            ),
        },
    )


INTAKE_STAGED_FILE_SOURCE_ID = "staged_files"


def intake_staged_file_candidates(
    workspace: Workspace,
    batch: Mapping[str, object],
) -> tuple[ContextCandidate, ...]:
    """Expose one uploaded staged file per candidate, metadata only.

    The projection is exactly ``intake.classification_payload_for_model``'s per
    item shape, which is the existing privacy choke point for folder intake: no
    staging path, absolute path, cell value, row preview, formula, comment, or
    extracted document text crosses it. Building the candidates through that
    function rather than reading the batch directly keeps the projection rule in
    one place.
    """
    payload = intake.classification_payload_for_model(workspace, dict(batch))
    return tuple(
        ContextCandidate(
            source_ref=f"staged_file:{item['id']}",
            source=item,
            representations={"file_metadata": item},
            metadata={
                "item_id": str(item.get("id") or ""),
                "relative_path": str(item.get("relative_path") or ""),
                "route": str(
                    (item.get("deterministic") or {}).get("route") or ""
                ),
            },
        )
        for item in sorted(
            payload.get("items") or [],
            key=lambda value: str(value.get("id") or ""),
        )
    )


def intake_classification_scope(
    workspace: Workspace,
    batch: Mapping[str, object],
) -> ContextScope:
    """Build the local candidate scope for one folder-intake classification."""

    candidates = intake_staged_file_candidates(workspace, batch)
    if not candidates:
        raise WorkspaceError(
            f"Import batch '{batch.get('id')}' has no uploaded file to classify."
        )
    return ContextScope(candidates={INTAKE_STAGED_FILE_SOURCE_ID: candidates})


__all__ = [
    "ANALYSIS_CURRENT_SOURCE_ID",
    "ANALYSIS_REGISTRY_SOURCE_ID",
    "ANALYSIS_RELATED_FRAMES_SOURCE_ID",
    "ANALYSIS_RELATIONSHIP_SOURCE_ID",
    "ANALYSIS_TARGET_AGGREGATE_SOURCE_ID",
    "ANALYSIS_TARGET_PROFILE_SOURCE_ID",
    "ANALYSIS_TARGET_SCHEMA_SOURCE_ID",
    "MAX_AGGREGATE_COLUMNS",
    "analysis_aggregate_candidates",
    "analysis_definition_scope",
    "analysis_relationship_candidates",
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
    "DOCUMENT_ANALYSIS_CHUNK_SOURCE_ID",
    "DOCUMENT_ANALYSIS_CHUNKS_SOURCE_ID",
    "DOCUMENT_ANALYSIS_METADATA_SOURCE_ID",
    "DOCUMENT_QA_ITEM_SOURCE_ID",
    "DOCUMENT_QA_PAGE_SOURCE_ID",
    "DOCUMENT_TEST_CURRENT_SOURCE_ID",
    "DOCUMENT_TEST_DOCUMENT_SOURCE_ID",
    "DOCUMENT_TEST_PLANNED_SOURCE_ID",
    "DOCUMENT_TEST_ROW_SOURCE_ID",
    "FINDING_EXECUTION_SOURCE_ID",
    "FINDING_OBSERVATION_SOURCE_ID",
    "FINDING_PLANNED_SOURCE_ID",
    "FINDING_ROW_SOURCE_ID",
    "INTAKE_STAGED_FILE_SOURCE_ID",
    "PLANNING_CONTEXT_CURRENT_SOURCE_ID",
    "PLANNING_CONTEXT_DOCUMENT_SOURCE_ID",
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
    "supplied_source_provenance",
    "apm_table_metadata_candidates",
    "apm_table_profile_candidates",
    "current_data_test_candidates",
    "current_document_test_candidates",
    "data_test_spec_scope",
    "document_chunk_scope",
    "document_metadata_candidate",
    "document_qa_page_candidates",
    "document_qa_scope",
    "document_reduction_scope",
    "document_test_document_candidates",
    "document_test_spec_scope",
    "finding_draft_scope",
    "intake_classification_scope",
    "intake_staged_file_candidates",
    "planning_context_document_candidates",
    "planning_context_scope",
    "planned_test_methodology_candidates",
    "planned_test_other_row_candidates",
    "planned_test_row_candidates",
    "planned_test_scope",
    "rcm_current_row_candidates",
    "rcm_scope",
]
