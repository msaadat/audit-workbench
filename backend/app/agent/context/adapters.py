"""Domain adapters that populate local context-resolver candidate scopes.

These functions translate existing document and methodology context builders
to the generic data-only candidate contract.  They do not select sources,
enforce context policy, call a model, or duplicate domain retrieval logic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace

from ... import (
    assistant,
    cycle_linking,
    cycle_vouching,
    doc_tests,
    document_context,
    documents,
    intake,
    methodology,
    model_context,
    templates_store,
)
from ...analysis_memo import flatten_embeds
from ...text import relevance_tokens
from ...workspaces import Workspace, WorkspaceError
from .. import joins as join_diagnostics
from ..workflows import analysis as analysis_workflow
from .resolver import ContextCandidate, ContextScope


APM_DOCUMENT_SOURCE_ID = "documents"
APM_METHODOLOGY_SOURCE_ID = "methodology"
APM_TABLE_METADATA_SOURCE_ID = "table_metadata"
APM_TABLE_PROFILE_SOURCE_ID = "table_profiles"
APM_PLANNING_SOURCE_ID = "planning_context"
APM_TEMPLATE_SOURCE_ID = "apm_template"
APM_CURRENT_ARTIFACT_SOURCE_ID = "current_apm"
APM_SUMMARY_SOURCE_ID = "analysis_summary"
APM_POPULATION_SOURCE_ID = "population_summary"


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


def _document_display_name(
    document: Mapping[str, object] | None, fallback: str = ""
) -> str:
    """The name a reader — or a drafting model — recognises.

    Delegates to :func:`documents.display_name`, which owns the precedence, so
    a worker, a narration line and this adapter cannot drift apart on what a
    document is called. The intake slug is still indexed for lexical selection
    wherever it was before, so naming a document differently never changes
    which documents are chosen.
    """
    return documents.display_name(document, fallback)


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
    include_audit_notes: bool = True,
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

    ``include_audit_notes`` is passed through to the document-context boundary;
    see :func:`document_context.apm_document_context` for why a downstream turn
    that already inherits the APM asks for the process description alone.
    """
    curated = document_ids is not None
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}
    candidates = []
    for document_id in _normalized_document_ids(workspace, document_ids):
        document = documents_by_id[document_id]
        context = document_context.apm_document_context(
            workspace,
            document_id,
            include_audit_notes=include_audit_notes,
        )
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
            "title": _document_display_name(document, document_id),
            "source": document.get("source") or "",
            "category": category,
            "text_state": document.get("text_state") or "",
            "analysis_id": context.get("analysis_id"),
            "analysis_validity_state": context.get("analysis_validity_state"),
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
                        # The intake slug stays indexed even though it is no
                        # longer the name shown: dropping it would quietly
                        # change which documents a lexical selector matches.
                        document.get("title"),
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


def apm_analysis_summary_candidates(
    workspace: Workspace,
) -> tuple[ContextCandidate, ...]:
    """The EDA memo, if one has been written, as planning can consume it.

    Embed directives are flattened to inline citations first. Planning has no
    renderer for them, and a raw ``embed`` fence copied into an APM would print
    as stray text — so the reference survives as prose instead of as markup the
    reader cannot resolve.

    The memo is supplied whether or not it is current against the latest
    results. A memo written from slightly older results still describes this
    engagement's data far better than nothing; the Summary screen is where its
    freshness is reported and acted on.
    """
    summary = dict(workspace.analysis_summary or {})
    markdown = str(summary.get("markdown") or "").strip()
    if not markdown:
        return ()
    titles = {
        str(item.get("id") or ""): str(item.get("title") or "")
        for item in workspace.analyses
    }
    flattened = flatten_embeds(markdown, titles)
    content = {
        "markdown": flattened,
        "generated_at": summary.get("generated_at"),
        "cited_analysis_ids": list(summary.get("cited_analysis_ids") or []),
    }
    return (
        ContextCandidate(
            source_ref="analysis_summary:current",
            source=content,
            representations={"analysis_summary": content},
            metadata={"artifact": "analysis_summary"},
            lexical_text=flattened,
        ),
    )


def _imported_table_names(workspace: Workspace) -> set[str]:
    """The tables imported from a source file, without the derived join frames.

    ``Workspace.table_names()`` is both: imported tables *and* the frames join
    inference derived from them. A planning turn describes the populations
    received, and a derived frame is a downstream analysis artifact carrying
    the same data under another name. Admitting them is not merely redundant —
    a budgeted source fills in candidate-name order, so on this workspace 14
    join frames crowded 4 of the 6 real populations out of the APM turn
    entirely.
    """
    return {str(table.get("name") or "").strip() for table in workspace.tables}


def apm_table_metadata_candidates(
    workspace: Workspace,
    *,
    imported_only: bool = False,
) -> tuple[ContextCandidate, ...]:
    """Expose schema-only table metadata through the existing assistant builder."""
    imported = _imported_table_names(workspace) if imported_only else None
    candidates = []
    for table in assistant.schema_brief(workspace):
        table_name = str(table.get("table") or "").strip()
        if not table_name or table.get("error"):
            continue
        if imported is not None and table_name not in imported:
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


def apm_table_profile_candidates(
    workspace: Workspace,
    *,
    imported_only: bool = False,
) -> tuple[ContextCandidate, ...]:
    """Expose bounded statistical profiles, plus category values where safe.

    A value list is kept on a column only when it is a *category domain*, not
    its rows restated: the table has a population and each value recurs
    across it (``MIN_CATEGORY_ROWS``, ``MIN_CATEGORY_REPETITION`` — the same
    gate ``test_generate_table_metadata_candidates`` applies), and the
    underlying frequency list holds one entry per distinct value rather than a
    truncated top-N. Anything narrower carries its distinct count alone.
    """
    imported = _imported_table_names(workspace) if imported_only else None
    candidates = []
    for table_name in workspace.table_names():
        if imported is not None and table_name not in imported:
            continue
        try:
            profile = assistant.table_metadata(
                workspace,
                table_name,
                include_category_values=True,
            )
        except (OSError, WorkspaceError):
            continue
        rows = profile.get("rows")
        rows = rows if isinstance(rows, int) else 0
        columns = []
        for column in profile.get("columns") or []:
            entry = dict(column)
            distinct = entry.get("distinct")
            values = entry.get("values")
            keep_values = (
                isinstance(values, list)
                and isinstance(distinct, int)
                and len(values) == distinct
                and rows >= MIN_CATEGORY_ROWS
                and distinct * MIN_CATEGORY_REPETITION <= rows
            )
            if not keep_values:
                entry.pop("values", None)
            columns.append(entry)
        profile = {**profile, "columns": columns}
        candidates.append(
            ContextCandidate(
                source_ref=f"table:{table_name}",
                source=profile,
                representations={"table_profile": profile},
                metadata={"table": table_name},
                lexical_text=" ".join(
                    (table_name, *(str(column.get("name") or "") for column in columns))
                ),
            )
        )
    return tuple(candidates)


# Per table, per kind. A wide workspace can carry more dated or valued columns
# than a planning turn has any use for, and an over-budget block is dropped
# whole rather than trimmed — which is exactly what happened the first time
# this shipped — so the cap is applied here where what it removed can still be
# counted.
MAX_SUMMARY_COLUMNS = 12


def population_summary_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """Expose the scale of the imported populations in one block.

    What the per-table profiles cannot state is the engagement's size: how many
    records were received in total, and what the valued columns add up to. A
    memo covering billions in payments should be able to say so, and no profile
    beside it carries a total.

    It deliberately computes no overall period. The first version did, and on
    the procurement workspace it returned 2010-01-15 — a staff hire date, not
    the start of anything auditable. Spanning every date column conflates
    master data with transactions, and a single range hides which column it
    came from. Per-table ranges are supplied instead and the memo proposes the
    period from them, which is what the model already does well when it can see
    the columns.

    Read through the same projection the profiles themselves travel under, so
    the summary can never state a figure the profile beside it contradicts. No
    row-level content is reachable from here, which is why it carries the
    profile permission rather than one of its own.
    """
    tables: list[dict] = []
    total_rows = 0
    for table_name in sorted(_imported_table_names(workspace)):
        try:
            profile = assistant.table_metadata(
                workspace,
                table_name,
                include_category_values=False,
            )
        except (OSError, WorkspaceError):
            continue
        rows = profile.get("rows")
        rows = rows if isinstance(rows, int) else 0
        total_rows += rows
        dated: list[dict] = []
        valued: list[dict] = []
        for column in profile.get("columns") or []:
            name = str(column.get("name") or "")
            if column.get("type") == "date" and (
                column.get("min") is not None or column.get("max") is not None
            ):
                dated.append(
                    {"column": name, "min": column.get("min"), "max": column.get("max")}
                )
            elif column.get("type") == "numeric" and column.get("sum") is not None:
                valued.append({"column": name, "total": column["sum"]})
        entry = {"table": table_name, "rows": rows}
        for key, columns in (("date_columns", dated), ("numeric_columns", valued)):
            entry[key] = columns[:MAX_SUMMARY_COLUMNS]
            if len(columns) > MAX_SUMMARY_COLUMNS:
                entry[f"{key}_omitted"] = len(columns) - MAX_SUMMARY_COLUMNS
        tables.append(entry)
    if not tables:
        return ()
    content: dict[str, object] = {"tables": tables, "total_rows": total_rows}
    return (
        ContextCandidate(
            source_ref="workspace:populations",
            source=content,
            representations={"population_summary": content},
            metadata={"scope": "workspace"},
            lexical_text=" ".join(str(item["table"]) for item in tables),
        ),
    )


# A table this small is one an aggregate profile cannot describe faithfully:
# min/max/null statistics over a handful of rows lose exactly the correlation
# (which row holds which value) that a reference or dimension table is for.
# Below this ceiling the whole table is supplied instead of its profile.
MAX_SMALL_TABLE_ROWS = 50


def small_table_row_candidates(
    workspace: Workspace,
    *,
    imported_only: bool = False,
) -> tuple[ContextCandidate, ...]:
    """Expose the complete rows of tables at or under the small-table ceiling.

    Row count is read from the cached profile so this never triggers a fresh
    scan; only a table already known to be small loads its frame at all.
    """
    imported = _imported_table_names(workspace) if imported_only else None
    candidates = []
    for table_name in workspace.table_names():
        if imported is not None and table_name not in imported:
            continue
        try:
            profile = workspace.get_profile(table_name)
        except (OSError, WorkspaceError):
            continue
        rows = profile.get("rows")
        if not isinstance(rows, int) or rows <= 0 or rows > MAX_SMALL_TABLE_ROWS:
            continue
        try:
            frame = workspace.get_frame(table_name)
        except (OSError, WorkspaceError):
            continue
        content = {"table": table_name, **model_context.project_frame(frame, row_limit=MAX_SMALL_TABLE_ROWS)}
        candidates.append(
            ContextCandidate(
                source_ref=f"table:{table_name}",
                source=content,
                representations={"table_rows_small": content},
                metadata={"table": table_name},
                lexical_text=table_name,
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
            APM_SUMMARY_SOURCE_ID: apm_analysis_summary_candidates(workspace),
            APM_POPULATION_SOURCE_ID: population_summary_candidates(workspace),
            APM_TABLE_METADATA_SOURCE_ID: apm_table_metadata_candidates(
                workspace, imported_only=True
            ),
            APM_TABLE_PROFILE_SOURCE_ID: apm_table_profile_candidates(
                workspace, imported_only=True
            ),
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
            "title": _document_display_name(document, document_id),
            "category": category,
            "text_state": document.get("text_state") or "",
            "analysis_id": analysis.get("analysis_id"),
            "analysis_validity_state": analysis.get("analysis_validity_state"),
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
                    for value in (
                        metadata["title"],
                        document.get("title"),
                        category,
                        *representations.values(),
                    )
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
RCM_SMALL_TABLE_ROWS_SOURCE_ID = "small_table_rows"

# The bounded identification and matching fields supplied per current RCM row.
# Planned tests, roll-ups, and evidence references stay out of the provider
# context; row revision only needs the narrative and ownership fields.
_RCM_ROW_CONTEXT_FIELDS = (
    "id",
    "semantic_id",
    "process",
    "risk",
    "risk_rating",
    "business_cycle",
    "control_attributes",
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
            RCM_TABLE_METADATA_SOURCE_ID: apm_table_metadata_candidates(
                workspace, imported_only=True
            ),
            RCM_TABLE_PROFILE_SOURCE_ID: apm_table_profile_candidates(
                workspace, imported_only=True
            ),
            RCM_SMALL_TABLE_ROWS_SOURCE_ID: small_table_row_candidates(
                workspace, imported_only=True
            ),
            RCM_DOCUMENT_SOURCE_ID: apm_document_candidates(
                workspace,
                document_ids=document_ids,
                excerpt_query=rcm_query,
                # The RCM turn takes the APM as its parent, and the APM already
                # carries every audit note forward. Supplying the numbered
                # deficiency list a second time here is what makes the turn
                # transcribe observations into rows instead of deriving the
                # process risk set; the process description alone is what a
                # risk-and-control matrix is built from.
                include_audit_notes=False,
            ),
            RCM_METHODOLOGY_SOURCE_ID: apm_methodology_candidates(workspace),
        },
        selector_context={**context, "rcm_query": rcm_query},
    )


# The bounded fields supplied for the one RCM row a generation unit plans
# against. Roll-ups, evidence references, and execution state stay out;
# generation needs the risk/control narrative and the row's current tests so
# a re-run revises them rather than duplicating them.
_TEST_DRAFT_ROW_FIELDS = (
    "id",
    "semantic_id",
    "process",
    "risk",
    "risk_rating",
    "business_cycle",
    "control_attributes",
    "control",
    "control_type",
    "control_owner",
    "criteria",
)
_TEST_DRAFT_EXISTING_FIELDS = (
    "id",
    "title",
    "objective",
    "criteria",
    "steps",
    "created_by",
)


def linked_test_projections(workspace: Workspace, rcm_id: str) -> list[dict]:
    """Bounded plan projections of every test currently linked to one row."""
    projections = [
        {
            **{key: item.get(key) for key in _TEST_DRAFT_EXISTING_FIELDS},
            "source": "data",
        }
        for item in workspace.data_tests
        if str(item.get("rcm_id") or "") == str(rcm_id)
    ]
    projections.extend(
        {
            **{key: summary.get(key) for key in _TEST_DRAFT_EXISTING_FIELDS},
            "source": "document",
        }
        for summary in doc_tests.list_tests(workspace)
        if str(summary.get("rcm_id") or "") == str(rcm_id)
    )
    return projections


def test_draft_row_candidates(
    workspace: Workspace,
    rcm_id: str,
) -> tuple[ContextCandidate, ...]:
    """Expose the one target RCM row, with the tests already linked to it."""
    row = next(
        (item for item in workspace.rcm if str(item.get("id")) == str(rcm_id)), None
    )
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    projection = {
        **{key: row.get(key) for key in _TEST_DRAFT_ROW_FIELDS},
        "existing_tests": linked_test_projections(workspace, rcm_id),
    }
    return (
        ContextCandidate(
            source_ref=f"rcm:{row['id']}",
            source=projection,
            representations={"current_artifact": projection},
            metadata={"rcm_id": str(row["id"])},
        ),
    )


def test_draft_methodology_candidates(
    workspace: Workspace,
) -> tuple[ContextCandidate, ...]:
    """Expose methodology sections that carry their citation with the excerpt.

    A committed test persists ``methodology_refs``, so the supplied
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


def _spec_test_record(workspace: Workspace, kind: str, test_id: str) -> dict:
    """Load one durable test record by kind, for scopes that reference it."""
    if kind == "datatest":
        record = next(
            (item for item in workspace.data_tests if str(item.get("id")) == str(test_id)),
            None,
        )
        if record is None:
            raise WorkspaceError(f"Data Test '{test_id}' not found.")
        return record
    return doc_tests.load_test(workspace, str(test_id))


def document_test_document_candidates(
    workspace: Workspace,
    *,
    document_ids: Iterable[str] | None = None,
    include_audit_notes: bool = True,
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
        context = document_context.apm_document_context(
            workspace, document_id, include_audit_notes=include_audit_notes
        )
        metadata = {
            "document_id": document_id,
            "title": _document_display_name(document, document_id),
            "category": document.get("category") or "",
            "text_state": document.get("text_state") or "",
        }
        content = {
            **{key: document.get(key) for key in _DOCUMENT_TEST_DOCUMENT_FIELDS},
            # The worker is handed both fields; naming them the same thing
            # removes the chance of it quoting the slug as the document's name.
            "title": metadata["title"],
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
                        document.get("title"),
                        metadata["category"],
                        content["summary"],
                    )
                ),
            )
        )
    return tuple(candidates)


TEST_GENERATE_PLANNING_SOURCE_ID = "planning_context"
TEST_GENERATE_ROW_SOURCE_ID = "rcm_row"
TEST_GENERATE_TABLE_METADATA_SOURCE_ID = "table_metadata"
TEST_GENERATE_DOCUMENT_SOURCE_ID = "documents"
TEST_GENERATE_METHODOLOGY_SOURCE_ID = "methodology"
TEST_GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID = "transaction_evidence"

# A column's value set is only a *category domain* — as opposed to its rows
# restated — when there is a population and each value recurs across it. Below
# either bound the values are withheld and the column carries its distinct count
# alone, so a narrow or near-unique column can never become a row disclosure.
MIN_CATEGORY_ROWS = 20
MIN_CATEGORY_REPETITION = 4


#: How much more a match on the table's own name is worth than a match on one
#: of its columns. The same weighting the test worker's own schema ranking
#: applies, so the selector that decides which schemas survive the budget and
#: the worker that decides which of the survivors to prompt with cannot reach
#: different conclusions about the same row.
_TABLE_NAME_WEIGHT = 4
#: The weight a derived frame's name carries instead. A join frame's name is
#: the concatenation of the names of everything it was built from, so at equal
#: weight it matches every query its parents match and outranks all of them —
#: six joins *over* the vendor master crowded out the vendor master itself.
#: Ranking a population above a view over that population is the right default
#: where both answer the query; where the view is what the test needs, it is
#: still ahead of every frame that does not mention the subject at all.
_DERIVED_TABLE_NAME_WEIGHT = 2


def _table_lexical_text(
    table_name: str,
    columns: Iterable[Mapping[str, object]],
    *,
    derived: bool = False,
) -> str:
    """The words of a table, weighted the way relevance is scored downstream.

    Two things have to be undone for a generic lexical scorer to rank tables
    usefully. It reads ``vendor_master_file`` as one atomic term, which matches
    no phrase an auditor writes, so the identifiers are split into words with
    the shared :func:`relevance_tokens`. And it scores by term *occurrence*,
    which on a schema is a proxy for column count — a 47-column join frame
    outscored the 14-column vendor master on a vendor-master risk purely on
    width. Emitting each column word once removes that, and repeating the
    table's own words restores the intended emphasis.
    """
    weight = _DERIVED_TABLE_NAME_WEIGHT if derived else _TABLE_NAME_WEIGHT
    name_terms = sorted(relevance_tokens(table_name))
    column_terms: set[str] = set()
    for column in columns:
        column_terms |= relevance_tokens(column.get("name"))
    return " ".join([*(name_terms * weight), *sorted(column_terms)])


def test_generate_table_metadata_candidates(
    workspace: Workspace,
    *,
    document_ids: Iterable[str] | None = None,
) -> tuple[ContextCandidate, ...]:
    """Expose table schemas plus the complete value set of each category column.

    A generated Data Test is a *predicate*, not a description, so a step written
    against column names alone has to guess what a status column holds. A guess
    that is wrong does not fail loudly: it either matches every row, which reads
    as a total control failure, or none, which reads as a clean control. Neither
    is visible to the schema-only validation the generation worker runs.

    Two conditions keep this category metadata rather than row data, and both
    matter:

    A value is a category only when many rows share it. In a narrow table the
    "complete set of values" of a column is just its rows restated, so a column
    qualifies only when the table is large enough to have a population and each
    value recurs across it (``MIN_CATEGORY_ROWS``, ``MIN_CATEGORY_REPETITION``).
    An identifier, a name, or a free-text field fails this by construction.

    A vocabulary is supplied only when it is provably complete. The profile keeps
    a bounded number of the most frequent values, so a column with more distinct
    values than that yields a *truncated* list the model would have no way to
    recognise as partial — worse than supplying nothing, because it would license
    excluding a value that does occur. A list is therefore passed on only when it
    holds one entry per distinct value; every other column carries its distinct
    count alone, which says "do not guess" without implying a domain.
    """
    candidates = []
    for table in assistant.schema_brief(workspace):
        table_name = str(table.get("table") or "").strip()
        if not table_name or table.get("error"):
            continue
        try:
            profile = assistant.table_metadata(
                workspace, table_name, include_category_values=True
            )
        except (OSError, WorkspaceError):
            profile = {"columns": []}
        profiled = {
            str(column.get("name")): column for column in profile.get("columns") or []
        }
        rows = table.get("rows")
        rows = rows if isinstance(rows, int) else 0
        columns = []
        for column in table.get("columns") or []:
            entry = dict(column)
            column_profile = profiled.get(str(column.get("name"))) or {}
            distinct = column_profile.get("distinct")
            values = column_profile.get("values")
            if isinstance(distinct, int) and column.get("type") in {"categorical", "text"}:
                entry["distinct"] = distinct
                if (
                    isinstance(values, list)
                    and len(values) == distinct
                    and rows >= MIN_CATEGORY_ROWS
                    and distinct * MIN_CATEGORY_REPETITION <= rows
                ):
                    entry["values"] = values
            columns.append(entry)
        # Which population a frame has one row of. A generated step cannot
        # judge whether it is asserting about the right population from a
        # column list — the requisition columns are present on the
        # invoice-grained join frame exactly as they are on ``requisitions``,
        # and only their reach differs.
        grain = join_diagnostics.frame_grain(workspace, table_name)
        content = {
            **table,
            "columns": columns,
            "grain": grain,
            "derived": grain != table_name,
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"table:{table_name}",
                source=content,
                representations={"table_metadata": content},
                metadata={"table": table_name, "grain": grain},
                lexical_text=_table_lexical_text(
                    table_name, columns, derived=grain != table_name
                ),
            )
        )
    return tuple(candidates)


def test_generate_scope(
    workspace: Workspace,
    rcm_id: str,
    *,
    planning_context: Mapping[str, object] | None = None,
    document_ids: Iterable[str] | None = None,
) -> ContextScope:
    """Build the local candidate scope for one merged test-generation unit.

    Replaces the separate ``tests.draft``/``tests.spec`` scopes: one RCM row's
    generation turn needs the row narrative, duplicate-avoidance context, table
    schemas, and every candidate document in one bundle, since the model chooses
    data vs document sources itself rather than a unit kind choosing them
    beforehand. The document source is
    :func:`document_test_document_candidates` only — the planning-relevant
    filter :func:`apm_document_candidates` applies would force every Document
    Test into ``missing_evidence``.
    """
    context = dict(planning_context or workspace.planning.get("context") or {})
    row = next(
        (item for item in workspace.rcm if str(item.get("id")) == str(rcm_id)), None
    )
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    # The attribute requirements carry the nouns that name a population — "bank
    # account", "vendor record", "goods receipt" — where the row narrative
    # often stays at the level of the process. They are what lets the table
    # selector rank the population a requirement is about above the frame that
    # merely sorts first.
    attribute_terms = [
        str(attribute.get(key) or "")
        for attribute in row.get("control_attributes") or []
        if isinstance(attribute, Mapping)
        for key in ("key", "requirement")
    ]
    test_generate_query = " ".join(
        str(value or "")
        for value in (
            row.get("process"),
            row.get("risk"),
            row.get("control"),
            row.get("criteria"),
            *attribute_terms,
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
    # The approved cycle rules, and what they reach across the population they
    # are anchored on. One candidate, not a list of them: the anchor is part of
    # what the auditor approved.
    transaction_manifest = cycle_linking.candidate(workspace)
    return ContextScope(
        candidates={
            TEST_GENERATE_PLANNING_SOURCE_ID: (
                ContextCandidate(
                    source_ref="planning:context",
                    source=planning_content,
                    representations={"planning_context": planning_content},
                    metadata={"artifact": "planning_context"},
                ),
            ),
            TEST_GENERATE_ROW_SOURCE_ID: test_draft_row_candidates(workspace, rcm_id),
            TEST_GENERATE_TABLE_METADATA_SOURCE_ID: (
                test_generate_table_metadata_candidates(
                    workspace, document_ids=document_ids
                )
            ),
            TEST_GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"transaction-evidence:{rcm_id}",
                    source=transaction_manifest,
                    representations={"table_metadata": transaction_manifest},
                    metadata={"rcm_id": str(rcm_id)},
                ),
            ),
            # A test obtains evidence about a control; the audit-notes block is a
            # numbered list of conclusions already drawn about the document, each
            # ending in a follow-up. Supplied here, it is the most test-shaped
            # content in the turn, and the turn writes it back as objectives that
            # re-confirm a known deficiency instead of testing whether a control
            # operated. The deficiency is already carried by the RCM row driving
            # this unit, so nothing is lost by reasoning from the process
            # description alone.
            TEST_GENERATE_DOCUMENT_SOURCE_ID: document_test_document_candidates(
                workspace,
                document_ids=document_ids,
                include_audit_notes=False,
            ),
            TEST_GENERATE_METHODOLOGY_SOURCE_ID: test_draft_methodology_candidates(
                workspace
            ),
        },
        selector_context={**context, "test_generate_query": test_generate_query},
    )


DOCUMENT_QA_ITEM_SOURCE_ID = "qa_item"
DOCUMENT_QA_PAGE_SOURCE_ID = "document_pages"

# The item fields a Q&A answer is derived from. Existing
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
        # Search returns passage-level results, and several non-overlapping
        # passages may legitimately belong to the same page.  The Q&A worker
        # and its evidence contract are page-oriented, so preserve every
        # distinct passage while collapsing them into one candidate per page.
        # This also keeps ``source_ref`` unique within the declared source.
        excerpts_by_page: dict[int, list[str]] = {}
        seen_text_by_page: dict[int, set[str]] = {}
        for citation in context.get("citations") or []:
            page_number = int(citation["page"])
            text = str(citation.get("excerpt") or "").strip()
            if not text:
                continue
            seen_text = seen_text_by_page.setdefault(page_number, set())
            if text in seen_text:
                continue
            seen_text.add(text)
            excerpts_by_page.setdefault(page_number, []).append(text)
        included = [
            {"page": page_number, "text": "\n\n".join(excerpts)}
            for page_number, excerpts in sorted(excerpts_by_page.items())
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
    """Build the local candidate scope for one bounded document-assessment unit."""
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
    kind = str(test.get("kind") or "")
    question = str(item.get("question") or "").strip()
    if kind == "attribute":
        requested = "; ".join(
            f"{value.get('name')}: expected {value.get('expected', 'present')}"
            for value in item.get("attributes") or []
        )
        question = "Assess these document attributes and cite supporting pages: " + requested
    elif kind == "review":
        question = str(item.get("instruction") or item.get("label") or "Review this document evidence.")
    if not question:
        raise WorkspaceError(f"Document Test item '{item_id}' has no assessable instruction.")
    projection = {
        **{key: item.get(key) for key in _DOCUMENT_QA_ITEM_FIELDS},
        "document_test_id": str(test_id),
        "document_id": str(document_id),
        "question": question,
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
FINDING_TEST_SOURCE_ID = "test"
FINDING_EXECUTION_SOURCE_ID = "execution_result"
FINDING_TEMPLATE_SOURCE_ID = "finding_template"
FINDING_EXCEPTION_ROWS_SOURCE_ID = "exception_rows"

# The cap on the exception table one finding may be drafted from. A finding
# names the records that failed; it does not reproduce a population. Past the
# cap the model is given the leading rows and told how many were withheld, so
# the narrative reports a truncated illustration rather than implying it saw
# everything.
FINDING_EXCEPTION_ROW_LIMIT = 25
FINDING_EXCEPTION_ROW_CHARACTERS = 8_000

_FINDING_ROW_FIELDS = ("id", "risk", "control", "criteria", "risk_rating")
_FINDING_TEST_FIELDS = (
    "id",
    "title",
    "objective",
    "criteria",
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


def _document_titles(workspace: Workspace, document_ids: Iterable[str]) -> list[dict]:
    """Name the documents behind a tested item, in stable order.

    A finding that says "the supplied documentation did not establish X" is not
    actionable; one that names the document is. This resolves the id the
    evidence anchor carries into the file the auditor recognizes — not the
    intake slug, which a drafting model will quote verbatim into the finding.
    """
    by_id = {str(item.get("id")): item for item in workspace.documents}
    named: dict[str, dict] = {}
    for value in document_ids:
        key = str(value or "")
        document = by_id.get(key)
        if key and key not in named:
            named[key] = {
                "id": key,
                "title": _document_display_name(document, key),
                "sha1": (document or {}).get("sha1"),
            }
    return list(named.values())


def _item_document_ids(item: Mapping[str, object]) -> list[str]:
    """Every document one tested item is grounded in, declared or cited."""
    ids = [str(value) for value in item.get("document_ids") or []]
    ids.extend(
        str(anchor.get("source_id"))
        for anchor in item.get("evidence_refs") or []
        if isinstance(anchor, Mapping) and anchor.get("source_kind") == "document"
    )
    return ids


def finding_exception_rows(workspace: Workspace, execution_ref: str) -> dict | None:
    """The capped exception table one Data Test flagged, or None.

    This is the row-level source class in the finding contract, admitted under
    ``allow_datatest_exception_rows`` alone. It is capped twice — by row count
    and by serialized size — and always states how many rows were withheld, so
    a narrative drafted from a truncated table cannot silently read as a
    complete one. Document Tests have no tabular exception population and
    return None rather than an empty table.
    """
    from ... import data_tests

    kind, _separator, source_id = str(execution_ref or "").partition(":")
    if kind != "datatest":
        return None
    artifact = data_tests.result_artifact(workspace, source_id)
    if not artifact:
        return None
    result = artifact["item"]
    frame = result.get("exception_frame") or {}
    columns = [str(value) for value in frame.get("columns") or []]
    rows = list(frame.get("rows") or [])
    if not columns or not rows:
        return None
    supplied: list[list] = []
    characters = 0
    for row in rows[:FINDING_EXCEPTION_ROW_LIMIT]:
        size = len(json.dumps(row, default=str))
        if supplied and characters + size > FINDING_EXCEPTION_ROW_CHARACTERS:
            break
        supplied.append(list(row))
        characters += size
    withheld = len(rows) - len(supplied)
    definition = next(
        (
            item
            for item in workspace.data_tests
            if str(item.get("id")) == str(result.get("data_test_id") or "")
        ),
        None,
    )
    instructions = {
        str(step.get("step_id") or ""): step.get("instruction")
        for step in ((definition or {}).get("spec") or {}).get("steps") or []
    }
    return {
        "execution_ref": execution_ref,
        "result_sha1": result.get("result_sha1"),
        "semantic_valid": result.get("semantic_valid"),
        "exception_count": result.get("exception_count"),
        "columns": columns,
        "rows": supplied,
        "rows_supplied": len(supplied),
        "rows_withheld": withheld,
        "truncated": bool(withheld) or bool(frame.get("truncated")),
        # What each step was looking for, so the rows can be read as evidence
        # of a specific failure rather than as an undifferentiated table. The
        # instruction comes from the test definition; the outcome from the run.
        "steps": [
            {
                "step_id": step.get("step_id"),
                "label": step.get("step_label"),
                "instruction": instructions.get(str(step.get("step_id") or "")),
                "exception_count": step.get("exception_count"),
            }
            for step in result.get("step_results") or []
        ],
    }


def _finding_execution_projection(
    workspace: Workspace,
    execution_ref: str,
    *,
    cycle_item_id: str | None = None,
) -> dict | None:
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
        if doc_tests.is_cycle_test(test):
            selected = next(
                (
                    item
                    for item in test.get("items") or []
                    if str(item.get("id") or "") == str(cycle_item_id or "")
                ),
                None,
            )
            if selected is None:
                return None
            return {
                "id": test.get("id"),
                "kind": "cycle_vouch",
                "status": test.get("status"),
                "sha1": test.get("sha1"),
                "definition_sha1": cycle_vouching.cycle_definition_sha1(test),
                "assurance_scope": doc_tests.assurance_scope(test),
                "rollup": doc_tests.result_rollup(test),
                "item": {
                    "id": selected.get("id"),
                    "evaluation": dict(selected.get("evaluation") or {}),
                    "disposition": dict(selected.get("disposition") or {}),
                    "documents": _document_titles(
                        workspace, _item_document_ids(selected)
                    ),
                    "assertion_results": [
                        {
                            "key": key,
                            "verdict": result.get("verdict"),
                            "assertion_sha1": result.get("assertion_sha1"),
                            "result_sha1": result.get("result_sha1"),
                            "evidence": [
                                {
                                    field: anchor.get(field)
                                    for field in (
                                        "source_kind", "source_id", "source_sha1",
                                        "page", "item_id", "field",
                                    )
                                }
                                for anchor in result.get("evidence_refs") or []
                            ],
                        }
                        for key, result in (selected.get("result_by_assertion") or {}).items()
                    ],
                },
            }
        return {
            "id": test.get("id"),
            "status": test.get("status"),
            "sha1": test.get("sha1"),
            "rollup": doc_tests.result_rollup(test),
            "items": [
                {
                    **{
                        key: item.get(key)
                        for key in (
                            "id", "label", "state", "runner_note", "question",
                        )
                    },
                    # Which documents the item was answered from. Without these
                    # a finding can only say "the supplied documentation", which
                    # management cannot act on.
                    "documents": _document_titles(workspace, _item_document_ids(item)),
                    "checks": [
                        {
                            key: check.get(key)
                            for key in ("field", "expected", "found", "verdict")
                        }
                        for check in item.get("checks") or []
                        if check.get("verdict") in ("mismatch", "missing")
                    ],
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
    test_id = str(observation.get("test_id") or "")
    kind, _separator, _source_id = str(observation.get("execution_ref") or "").partition(":")
    test = _spec_test_record(workspace, kind or "doctest", test_id)
    row = next(
        (
            item
            for item in workspace.rcm
            if str(item.get("id")) == str(observation.get("rcm_id") or "")
        ),
        None,
    )
    if row is None:
        raise WorkspaceError(
            f"Observation '{observation_id}' does not resolve to an RCM row."
        )
    finding_template = templates_store.get_template(workspace, "finding")["markdown"]
    execution_ref = str(observation.get("execution_ref") or "")
    exception_rows = finding_exception_rows(workspace, execution_ref)
    execution = {
        "execution_ref": execution_ref,
        "immutable_execution_result": _finding_execution_projection(
            workspace,
            execution_ref,
            cycle_item_id=str(observation.get("cycle_item_id") or "") or None,
        ),
        "evidence_anchor": findings.anchor_from_ref(workspace, execution_ref),
    }
    row_projection = {key: row.get(key) for key in _FINDING_ROW_FIELDS}
    test_projection = {key: test.get(key) for key in _FINDING_TEST_FIELDS}
    observation_projection = {
        key: observation.get(key)
        for key in (
            "id", "rcm_id", "test_id", "execution_ref", "exception_count",
            "summary", "classification", "outcome", "cycle_item_id",
            "assurance_scope", "definition_sha1", "evaluation_result_sha1",
            "evaluation_state", "disposition_state", "assertion_keys",
            "assertion_mismatch_count",
        )
    }
    return ContextScope(
        candidates={
            FINDING_OBSERVATION_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"observation:{observation['id']}",
                    source=observation_projection,
                    representations={"current_artifact": observation_projection},
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
            FINDING_TEST_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"{kind}:{test['id']}",
                    source=test_projection,
                    representations={"current_artifact": test_projection},
                    metadata={"test_id": str(test["id"]), "kind": kind},
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
            # The finding template is the narrative's contract: its headings are
            # the sections the draft must answer and its guidance comments are
            # how the firm's house style reaches the turn.
            FINDING_TEMPLATE_SOURCE_ID: (
                ContextCandidate(
                    source_ref="template:finding",
                    source=finding_template,
                    representations={"artifact_template": finding_template},
                    metadata={"template": "finding"},
                ),
            ),
            # The rows the test flagged, so the finding names the records that
            # failed instead of only counting them. Empty for a Document Test,
            # which has no tabular exception population.
            FINDING_EXCEPTION_ROWS_SOURCE_ID: (
                (
                    ContextCandidate(
                        source_ref=f"{execution_ref}:exceptions",
                        source=exception_rows,
                        representations={"datatest_exception_rows": exception_rows},
                        metadata={"execution_ref": execution_ref},
                    ),
                )
                if exception_rows
                else ()
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
ANALYSIS_HYPOTHESIS_SOURCE_ID = "join_hypotheses"
ANALYSIS_RELATIONSHIP_SOURCE_ID = "relationship_evidence"
ANALYSIS_REGISTRY_SOURCE_ID = "analytics_registry"
ANALYSIS_LOOKUP_SOURCE_ID = "lookup_candidates"
ANALYSIS_PROBE_SOURCE_ID = "probe_findings"
ANALYSIS_DOMAIN_SOURCE_ID = "value_domains"
ANALYSIS_CURRENT_SOURCE_ID = "current_analyses"
ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS = (
    analysis_workflow.EXCLUDED_ANALYTICS_TEST_IDS
)

# Aggregate columns are bounded so a wide table cannot consume the declaration's
# whole character budget before the resolver even sees it.
MAX_AGGREGATE_COLUMNS = 24

# Profiler fields that are literal values drawn from rows. ``top_values`` is
# category-domain content that ``apm_table_profile_candidates`` admits under a
# gate (a real population where every value recurs); this adapter's aggregates
# stay value-free regardless, since analysis-definition context is metadata
# and aggregates only, by design, independent of that gate.
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


# A lookup key must actually identify rows of its frame. Measured from the
# cached profile rather than by loading frames: a column is a key when it is
# populated throughout and distinct on every row.
def _key_columns(workspace: Workspace, table: str) -> list[str]:
    try:
        profile = workspace.get_profile(table)
    except (OSError, WorkspaceError):
        return []
    rows = profile.get("rows")
    if not isinstance(rows, int) or rows <= 0:
        return []
    keys = []
    for column in profile.get("column_profiles") or []:
        distinct = column.get("distinct_count")
        blanks = column.get("blank_count") or 0
        name = str(column.get("name") or "").strip()
        if name and isinstance(distinct, int) and distinct == rows and not blanks:
            keys.append(name)
    return keys


# How many key columns of one lookup frame are worth offering. The submission
# schema spends a branch per lookup table and repeats the target's own column
# enum inside each, so this is the term that decides whether a wide workspace
# turns the reconciliation test into a third of the prompt.
MAX_LOOKUP_KEYS = 3


def analysis_lookup_candidates(
    workspace: Workspace, target: str
) -> tuple[ContextCandidate, ...]:
    """Frames a reconciliation from ``target`` may read, and their key columns.

    Base tables only, and never one the target already contains: reconciling a
    joined frame against a table it was built from asks whether a key matches
    itself. A frame with no key column is omitted — there is nothing on it for a
    reference to resolve against.

    Narrowed further to the frames the target plausibly *references*, by the same
    schema-only name affinity the join diagnostics use before they measure
    anything. Two reasons, and the second is the one that matters. Offering a
    staff master to a frame carrying no person-shaped column invites a
    reconciliation that can only report that two unrelated columns do not match.
    And the offer is not free: each candidate costs a schema branch carrying the
    target's whole column enum, so an unfiltered sweep of a twenty-table
    workspace would spend more of the definition turn on this one test than on
    every other test combined.
    """
    lineage = join_diagnostics.frame_lineage(workspace, target)
    try:
        columns = [
            str(column.get("name"))
            for column in workspace.get_profile(target).get("column_profiles") or []
            if str(column.get("name") or "").strip()
        ]
    except (OSError, WorkspaceError):
        columns = []
    entities = join_diagnostics.entity_tokens(workspace.table_names())
    candidates = []
    for item in workspace.tables:
        name = str(item.get("name") or "")
        if not name or name in lineage or name == target:
            continue
        keys = _key_columns(workspace, name)
        if not keys:
            continue
        referenced = list(
            dict.fromkeys(
                key
                for _, key in join_diagnostics.reference_candidates(
                    columns, keys, name, entities
                )
            )
        )
        # No column of the target looks like a reference to this frame. A
        # reconciliation between them would be a comparison of two populations
        # that were never meant to meet.
        if not referenced:
            continue
        content = {"table": name, "key_columns": referenced[:MAX_LOOKUP_KEYS]}
        candidates.append(
            ContextCandidate(
                source_ref=f"lookup:{name}",
                source=content,
                representations={"table_metadata": content},
                metadata={"table": name},
                lexical_text=" ".join([name, *content["key_columns"]]),
            )
        )
    return tuple(candidates)


def analysis_domain_candidates(
    domains: Iterable[Mapping[str, object]],
) -> tuple[ContextCandidate, ...]:
    """Declare the value vocabularies the sweep read off the frame.

    Computed there rather than here for the same reason the sample is: reading
    a frame is the sweep's job, and this module is held to metadata and cached
    profiles by a boundary test that checks the whole file.
    """
    return tuple(
        ContextCandidate(
            source_ref=f"domain:{item.get('table')}:{item.get('column')}",
            source=dict(item),
            representations={"value_domain": dict(item)},
            metadata={
                "table": str(item.get("table") or ""),
                "column": str(item.get("column") or ""),
            },
            lexical_text=f"{item.get('table')} {item.get('column')}",
        )
        for item in domains
    )


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

    An empty ``target`` asks for the whole map rather than for nothing. That is
    what the reading turn needs: the relationships that were *not* materialized
    are as much a part of the map as the ones that were, and a pair whose keys
    mostly fail to match is exactly where an unreconciled population lives.
    """
    candidates = []
    for record in relationships or ():
        left = str(record.get("left") or "")
        right = str(record.get("right") or "")
        if target and target not in {left, right}:
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


def join_utility_scope(
    workspace: Workspace,
    relationships: Iterable[Mapping[str, object]],
) -> ContextScope:
    """Bounded, row-free catalog for the pre-materialization join gate."""
    candidates: list[dict[str, object]] = []
    tables: set[str] = set()
    for record in relationships:
        for item in record.get("candidates") or []:
            if not isinstance(item, Mapping) or item.get("strength") not in {"strong", "moderate"}:
                continue
            ref = str(item.get("ref") or "").strip()
            if not ref:
                continue
            left, right = str(item.get("left") or ""), str(item.get("right") or "")
            tables.update((left, right))
            candidates.append({
                "ref": ref,
                "left": left,
                "right": right,
                "left_on": list(item.get("left_on") or []),
                "right_on": list(item.get("right_on") or []),
                "role_key": bool(item.get("role_key")),
                "strength": item.get("strength"),
                "diagnostics": dict(item.get("diagnostics") or {}),
            })
    candidates.sort(key=lambda item: str(item["ref"]))
    schemas = [
        item for item in assistant.schema_brief(workspace)
        if str(item.get("table") or "") in tables and not item.get("error")
    ]
    table_columns = {
        str(item["table"]): [
            str(column.get("name"))
            for column in item.get("columns") or []
            if str(column.get("name") or "").strip()
        ]
        for item in schemas
    }
    # The model must nominate concrete schema-visible columns from both sides
    # of a retained relationship.  This supports deterministic validation of
    # the stated hypothesis without ever disclosing a row or category value.
    catalog = {"candidates": candidates, "table_columns": table_columns}
    schema_candidates = tuple(
        ContextCandidate(
            source_ref=f"table:{item['table']}", source=item,
            representations={"table_metadata": item}, metadata={"table": item["table"]},
            lexical_text=str(item["table"]),
        )
        for item in schemas
    )
    return ContextScope(candidates={
        "join_candidates": (ContextCandidate(
            source_ref="analysis:join_candidates", source=catalog,
            representations={"table_aggregate": catalog}, metadata={"scope": "join_utility"},
            lexical_text="join utility candidates",
        ),),
        "join_tables": schema_candidates,
    })


READING_FRAMES_SOURCE_ID = "frame_map"
READING_NOMINATIONS_SOURCE_ID = "nominations"
READING_RELATIONSHIP_SOURCE_ID = "relationship_map"
READING_HYPOTHESIS_SOURCE_ID = "join_hypotheses"
READING_DOMAIN_SOURCE_ID = "value_domains"
READING_REGISTRY_SOURCE_ID = "analytics_registry"


def analysis_reading_scope(
    workspace: Workspace,
    frames: Iterable[str],
    *,
    nominations: Iterable[Mapping[str, object]] = (),
    relationships: Iterable[Mapping[str, object]] | None = None,
    hypotheses: Iterable[Mapping[str, object]] = (),
    value_domains: Iterable[Mapping[str, object]] = (),
    analytics_registry: object = None,
) -> ContextScope:
    """The whole map, once: every frame's columns and every measured nomination.

    This is the one analysis bundle that is not frame-scoped, and the breadth is
    the point. A turn holding every frame's column inventory can say what no
    per-frame turn structurally can — that no field anywhere in the engagement
    records a competitive bid — and a turn holding every nomination at once can
    decide which frame a measurement belongs on instead of leaving that to
    whichever frame happened to run first.

    The privacy class is unchanged from the per-frame definition bundle it sits
    beside: schemas, cached profiles' column names and types, aggregate counts,
    and low-cardinality value vocabularies. Nothing here is a row, and breadth
    across frames does not widen the class — a column inventory of twenty frames
    discloses exactly what a column inventory of one frame discloses, twenty
    times.
    """
    from ... import analytics as analytics_module

    known = set(workspace.table_names())
    scoped = [str(name) for name in frames if str(name) in known]
    inventory = {
        str(item.get("table")): item
        for item in assistant.schema_brief(workspace)
        if str(item.get("table") or "") in set(scoped) and not item.get("error")
    }
    frame_candidates = []
    for name in scoped:
        schema = inventory.get(name)
        if schema is None:
            continue
        described = {
            **schema,
            "column_origins": join_diagnostics.column_origins(workspace, name),
            "frame_root": join_diagnostics.frame_root(workspace, name),
            "frame_route": join_diagnostics.frame_route(workspace, name),
        }
        frame_candidates.append(
            ContextCandidate(
                source_ref=f"table:{name}",
                source=described,
                representations={"table_metadata": described},
                metadata={"table": name},
                lexical_text=name,
            )
        )
    registry = (
        analytics_registry
        if analytics_registry is not None
        else [
            item
            for item in analytics_module.registry_payload()
            if item["id"] not in ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS
        ]
    )
    return ContextScope(
        candidates={
            READING_FRAMES_SOURCE_ID: tuple(frame_candidates),
            # Every measured nomination in the engagement, already deduplicated
            # and already ranked. Each carries the reference the reading turn
            # keeps or declines it by, so a decision names a measurement rather
            # than restating one.
            READING_NOMINATIONS_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=f"nomination:{item.get('ref')}",
                    source=dict(item),
                    representations={"table_aggregate": dict(item)},
                    metadata={
                        "table": str(item.get("frame") or ""),
                        "ref": str(item.get("ref") or ""),
                    },
                    lexical_text=str(item.get("reading") or ""),
                )
                for item in nominations
            ),
            READING_RELATIONSHIP_SOURCE_ID: analysis_relationship_candidates(
                workspace, "", relationships
            ),
            READING_HYPOTHESIS_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=f"hypothesis:{item.get('ref')}",
                    source=dict(item),
                    representations={"current_artifact": dict(item)},
                    metadata={"ref": str(item.get("ref") or "")},
                    lexical_text=str(item.get("hypothesis") or ""),
                )
                for item in hypotheses
            ),
            READING_DOMAIN_SOURCE_ID: analysis_domain_candidates(value_domains),
            READING_REGISTRY_SOURCE_ID: (
                ContextCandidate(
                    source_ref="registry:analytics",
                    source=registry,
                    representations={"current_artifact": registry},
                    metadata={"registry": "analytics"},
                ),
            ),
        },
        selector_context={"analysis_query": " ".join(scoped)},
    )


def analysis_definition_scope(
    workspace: Workspace,
    target: str,
    *,
    related: Iterable[str] = (),
    relationships: Iterable[Mapping[str, object]] | None = None,
    hypotheses: Iterable[Mapping[str, object]] = (),
    probe_findings: Iterable[Mapping[str, object]] = (),
    value_domains: Iterable[Mapping[str, object]] = (),
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
    # A frame whose lineage the target already contains has no column the
    # target does not: sending its schema costs a description of the same data
    # under another name, and invites a proposal that reads one side of the
    # target through a frame that is only part of it.
    lineage = join_diagnostics.frame_lineage(workspace, target)
    related_names = [
        name
        for name in dict.fromkeys(str(value) for value in related)
        if name != target
        and name in workspace.table_names()
        and not join_diagnostics.frame_lineage(workspace, name) <= lineage
    ]
    related_candidates = tuple(
        candidate
        for candidate in apm_table_metadata_candidates(workspace)
        if candidate.metadata.get("table") in set(related_names)
    )
    # The complete public contract for every workflow-eligible library test.
    # Low-impact digit-pattern scans remain available in the manual analytics
    # library, but the autonomous workflow does not propose them.  The worker
    # receives parameter defaults, allowed select values, optionality, and
    # column type constraints — not merely parameter names.
    registry = (
        analytics_registry
        if analytics_registry is not None
        else [
            item
            for item in analytics_module.registry_payload()
            if item["id"] not in ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS
        ]
    )
    # Analyses already saved anywhere in this frame's join family, not only on
    # the frame itself. A join is a view over its sides, so the same
    # computation is reachable from every frame sharing a base table with the
    # target; showing only the target's own analyses is what let one invoice
    # date-lag check be proposed three times, once per frame that could see the
    # invoice columns. A proposal can only avoid repeating what it was shown.
    family = {
        name
        for name in workspace.table_names()
        if join_diagnostics.frame_lineage(workspace, name) & lineage
    }
    current = [
        {
            key: item.get(key)
            for key in ("id", "title", "kind", "table", "spec", "semantic_id", "created_by")
        }
        for item in workspace.analyses
        if str(item.get("table") or "") in family
    ]
    # Which base table each column comes from. Identity for an analysis is the
    # computation — these columns, from these tables — rather than the frame it
    # was written against, so the worker needs the mapping to recognise its
    # own proposal in a sibling frame's saved analysis.
    schema = {
        **schema,
        "column_origins": join_diagnostics.column_origins(workspace, target),
        # The population the computation was asked over: which table the frame's
        # rows are rows of, and which key each joined-in table was reached by.
        # Identity above says which computation a proposal is; without these it
        # cannot tell a repeat from a sibling frame's different question about
        # the same column, and the difference is the finding.
        "frame_root": join_diagnostics.frame_root(workspace, target),
        "frame_route": join_diagnostics.frame_route(workspace, target),
    }
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
            # Why this frame was materialized at all. The utility gate admitted
            # each retained relationship against a stated, falsifiable test; a
            # frame asked to invent one from schemas alone reinvents a worse
            # question than the one already asked of it.
            ANALYSIS_HYPOTHESIS_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=f"hypothesis:{item.get('ref')}",
                    source=dict(item),
                    representations={"current_artifact": dict(item)},
                    metadata={"ref": str(item.get("ref") or "")},
                    lexical_text=str(item.get("hypothesis") or ""),
                )
                for item in hypotheses
                if set(str(name) for name in item.get("requires") or ()) <= lineage
            ),
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
            ANALYSIS_LOOKUP_SOURCE_ID: analysis_lookup_candidates(workspace, target),
            # What this frame's own data already asserts, measured before the
            # turn rather than guessed during it. Each is a runnable spec with
            # the counts it produced, so the model is choosing among measured
            # questions instead of deriving candidate questions from the schema
            # and finding out afterwards which of them had anything in them.
            # Counts and column names only — the rows behind a nomination stay
            # in the evidence sidecar its execution writes.
            ANALYSIS_PROBE_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=(
                        f"probe:{item.get('frame')}:{item.get('test')}:{index}"
                    ),
                    source=dict(item),
                    representations={"table_aggregate": dict(item)},
                    metadata={
                        "table": str(item.get("frame") or ""),
                        "family": str(item.get("family") or ""),
                    },
                    lexical_text=str(item.get("reading") or ""),
                )
                for index, item in enumerate(probe_findings)
            ),
            ANALYSIS_DOMAIN_SOURCE_ID: analysis_domain_candidates(value_domains),
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


# --------------------------------------------------------------------------- #
# analysis.summary
# --------------------------------------------------------------------------- #
SUMMARY_RESULTS_SOURCE_ID = "analysis_results"
SUMMARY_EXCEPTIONS_SOURCE_ID = "analysis_exceptions"
SUMMARY_ANOMALIES_SOURCE_ID = "analysis_anomalies"
SUMMARY_GAPS_SOURCE_ID = "coverage_gaps"
SUMMARY_JOINS_SOURCE_ID = "table_joins"
SUMMARY_TABLE_METADATA_SOURCE_ID = "table_metadata"
SUMMARY_TABLE_PROFILE_SOURCE_ID = "table_profiles"
SUMMARY_PLANNING_SOURCE_ID = "planning_context"

# How many flagged rows one procedure contributes to a summary turn. The
# durable sidecar retains far more (``analysis_results.EXCEPTION_ROWS``); a memo
# needs enough rows to characterise an exception and name its worst instances,
# not the whole population — the reader clicks through for that. With dozens of
# flagging procedures in a real engagement this bound is what keeps the bundle
# inside its token budget.
MAX_SUMMARY_EXCEPTION_ROWS = 12

# Flagged rows are split across two declared sources by severity, and that split
# is what makes truncation safe. Deterministic selectors order candidates by
# source_ref, so a single source dropped whichever procedures sorted late by
# analysis ID — a failed backdating test could be cut while a weekend-activity
# warning survived. Failures now have their own budget and cannot be crowded out
# by the merely unusual.
_FAILURE_VERDICTS = frozenset({"fail"})


# A procedure's Polars source, bounded. Long enough for every analysis this
# workflow writes and for an auditor's own one-expression checks; a genuinely
# long script is truncated rather than allowed to crowd out other procedures,
# and says so where it is cut.
MAX_SUMMARY_CODE_CHARACTERS = 1_500


def _bounded_code(spec: Mapping[str, object]) -> str | None:
    code = str(spec.get("code") or "").strip()
    if not code:
        return None
    if len(code) <= MAX_SUMMARY_CODE_CHARACTERS:
        return code
    return code[:MAX_SUMMARY_CODE_CHARACTERS] + "\n… truncated"


def _summary_result_projection(
    workspace: Workspace, analysis: Mapping[str, object]
) -> dict[str, object]:
    """What one saved procedure concluded, as the memo needs to read it."""
    from ...analysis_results import analysis_state

    result = dict(analysis.get("last_result") or {})
    spec = dict(analysis.get("spec") or {})
    state = analysis_state(workspace, analysis)
    return {
        "analysis_id": str(analysis.get("id") or ""),
        "title": analysis.get("title"),
        # The authored rationale. A memo that can say *why* a procedure was run
        # reads like an auditor wrote it; one that only has titles does not.
        "note": analysis.get("note"),
        "table": analysis.get("table"),
        "kind": analysis.get("kind"),
        "test": spec.get("test"),
        # What the procedure actually did, not only what it was called. A title
        # is authored text and can describe a test the spec does not implement:
        # a duplicates test over two join keys titled as a mismatch check tests
        # for repeated key groups, and a date-lag test reads as backdating in
        # whichever direction its parameters happen to name. Without the
        # parameters those are indistinguishable from the outside, and a memo
        # that cannot distinguish them restates the title as a finding.
        "parameters": spec.get("params") or {},
        # The same problem in its sharpest form. A python procedure's audit
        # meaning is its code together with its outcome policy: under
        # ``exception_rows`` every returned row counts as a potential
        # exception, so code that returns its frame unfiltered reports the
        # population as exceptions. Both travel so the memo can tell the
        # difference.
        "code": _bounded_code(spec),
        "outcome_policy": dict(analysis.get("outcome_policy") or {}),
        "created_by": analysis.get("created_by"),
        "classification": state["classification"],
        "state": state["state"],
        "verdict": result.get("verdict"),
        "verdict_text": result.get("verdict_text"),
        # ``row_count`` is the size of the result frame, not of anything the
        # conclusion is about. The three below are the denominators — see
        # ``analysis_results.bounded_result``. Results recorded before they
        # were computed carry none, which reads correctly as unknown.
        "row_count": result.get("row_count"),
        "population": result.get("population"),
        "tested": result.get("tested"),
        "not_tested": result.get("not_tested"),
        "exception_count": result.get("exception_count"),
        "exception_rate": result.get("exception_rate"),
        "exception_rate_of": result.get("exception_rate_of"),
        # Whether the result establishes anything, decided locally rather than
        # left to be noticed. A saturated result is the shape most likely to be
        # written up as a systemic finding, because everything about it except
        # the denominator looks like one.
        "informative": result.get("informative"),
        "uninformative_reason": result.get("uninformative_reason"),
        "statistics": result.get("stats") or [],
        "error": result.get("error"),
    }


def analysis_summary_result_candidates(
    workspace: Workspace,
) -> tuple[ContextCandidate, ...]:
    """Every saved procedure and the outcome it recorded.

    Deliberately every one, not only the workflow's own: a procedure the
    auditor wrote is part of the EDA the memo describes.
    """
    return tuple(
        ContextCandidate(
            source_ref=f"analysis:{item['id']}",
            source=projection,
            representations={"analysis_result": projection},
            metadata={"analysis_id": str(item.get("id") or "")},
            lexical_text=" ".join(
                str(value or "")
                for value in (item.get("title"), item.get("table"), item.get("note"))
            ),
        )
        for item in workspace.analyses
        for projection in (_summary_result_projection(workspace, item),)
    )


def analysis_summary_exception_candidates(
    workspace: Workspace, *, failures: bool
) -> tuple[ContextCandidate, ...]:
    """The rows each flagging procedure identified, bounded per procedure.

    ``failures`` selects the severity half: the procedures that concluded a
    failure, or the ones that merely flagged something unusual.
    """
    from ...analysis_results import read_exception_evidence

    candidates = []
    for item in workspace.analyses:
        verdict = str((item.get("last_result") or {}).get("verdict") or "")
        if (verdict in _FAILURE_VERDICTS) is not failures:
            continue
        evidence = read_exception_evidence(workspace, item)
        if evidence is None:
            continue
        frame = dict(evidence.get("frame") or {})
        rows = list(frame.get("rows") or [])[:MAX_SUMMARY_EXCEPTION_ROWS]
        flagged = {
            "analysis_id": str(item.get("id") or ""),
            "title": item.get("title"),
            "table": item.get("table"),
            "verdict": verdict,
            "verdict_text": (item.get("last_result") or {}).get("verdict_text"),
            "exception_count": evidence.get("exception_count"),
            "rows_supplied": len(rows),
            "columns": list(frame.get("columns") or []),
            "rows": rows,
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"analysis:{item['id']}",
                source=flagged,
                representations={"analysis_exception_rows": flagged},
                metadata={"analysis_id": str(item.get("id") or "")},
                lexical_text=str(item.get("title") or ""),
            )
        )
    return tuple(candidates)


def analysis_join_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """Every join the engagement defined, with the keys it was actually built on.

    A procedure runs against a frame, and for a joined frame what that frame
    *is* — which columns were matched, in which direction, how many left rows
    found a match, and whether the match multiplied rows — is not recoverable
    from the frame's name. Supplied because a memo asked to describe the
    relationships tested will otherwise describe plausible ones: join keys are
    exactly the kind of detail that reads as established fact and is invented
    silently. It also makes a duplicate-key result over a joined frame legible
    as the fan-out it may be rather than as a mismatch between two systems.
    """
    candidates = []
    for join in workspace.joins:
        name = str(join.get("name") or "")
        left, right = str(join.get("left") or ""), str(join.get("right") or "")
        left_on = [str(item) for item in (join.get("left_on") or [])]
        right_on = [str(item) for item in (join.get("right_on") or [])]
        content: dict[str, object] = {
            "frame": name,
            "how": join.get("how"),
            "left": left,
            "right": right,
            "left_on": left_on,
            "right_on": right_on,
        }
        # Match quality, where the pair is single-key: ``diagnose`` reports one
        # key pair, and a composite key is not the sum of its columns. A
        # composite join still supplies its definition, which is the part a
        # memo cannot otherwise know.
        if len(left_on) == 1 and len(right_on) == 1:
            try:
                content["match"] = join_diagnostics.diagnose(
                    workspace.get_frame(left),
                    workspace.get_frame(right),
                    left_on[0],
                    right_on[0],
                )
            except (OSError, WorkspaceError, ValueError, KeyError):
                # A frame that will not resolve leaves the definition standing
                # without measurements, which reads correctly as unmeasured.
                pass
        candidates.append(
            ContextCandidate(
                source_ref=f"join:{name}",
                source=content,
                representations={"table_metadata": content},
                metadata={"join": name, "table": name},
                lexical_text=" ".join([name, left, right, *left_on, *right_on]),
            )
        )
    return tuple(candidates)


def analysis_coverage_gaps(workspace: Workspace) -> dict[str, object]:
    """Locally computed inventory of what the analysis has not established.

    Supplied as fact rather than left to the model's imagination: an auditor's
    "further work" section is only trustworthy if the plainly outstanding items
    are in it, and those are knowable without a model.
    """
    from ...analysis_results import analysis_state

    outstanding: list[dict[str, object]] = []
    errored: list[dict[str, object]] = []
    for item in workspace.analyses:
        state = analysis_state(workspace, item)
        entry = {
            "analysis_id": str(item.get("id") or ""),
            "title": item.get("title"),
            "table": item.get("table"),
            "reason": state["classification"],
        }
        if state["classification"] in {"not_run", "stale"}:
            outstanding.append(entry)
        elif state["classification"] == "execution_error":
            entry["error"] = (item.get("last_result") or {}).get("error")
            errored.append(entry)

    covered = {str(item.get("table") or "") for item in workspace.analyses}
    frames = list(workspace.table_names())
    uncovered = sorted(name for name in frames if name not in covered)

    joined: set[frozenset[str]] = set()
    for join in workspace.joins:
        joined.add(frozenset({str(join.get("left")), str(join.get("right"))}))
    base = sorted(str(item.get("name") or "") for item in workspace.tables)
    unjoined = sorted(
        f"{left} + {right}"
        for index, left in enumerate(base)
        for right in base[index + 1 :]
        if frozenset({left, right}) not in joined
    )
    return {
        "outstanding_procedures": outstanding,
        "errored_procedures": errored,
        "frames_without_any_procedure": uncovered,
        "table_pairs_never_joined": unjoined,
        "totals": {
            "analyses": len(workspace.analyses),
            "outstanding": len(outstanding),
            "errored": len(errored),
            "frames": len(frames),
            "joins": len(workspace.joins),
        },
    }


def analysis_summary_scope(workspace: Workspace) -> ContextScope:
    """Build the local candidate scope for the one analysis-summary unit."""
    gaps = analysis_coverage_gaps(workspace)
    planning_context = dict(workspace.planning.get("context") or {})
    # Base tables only. A join is a view over its sides, so its profile restates
    # populations already described and — because deterministic selectors order
    # by source_ref — enough joins would crowd the base tables out of the budget
    # entirely. The memo describes the data received; the joins built over it
    # are their own section.
    base_tables = {str(item.get("name") or "") for item in workspace.tables}
    return ContextScope(
        candidates={
            SUMMARY_RESULTS_SOURCE_ID: analysis_summary_result_candidates(workspace),
            SUMMARY_EXCEPTIONS_SOURCE_ID: analysis_summary_exception_candidates(
                workspace, failures=True
            ),
            SUMMARY_ANOMALIES_SOURCE_ID: analysis_summary_exception_candidates(
                workspace, failures=False
            ),
            SUMMARY_GAPS_SOURCE_ID: (
                ContextCandidate(
                    source_ref="analysis:coverage_gaps",
                    source=gaps,
                    representations={"analysis_result": gaps},
                    metadata={"kind": "coverage_gaps"},
                ),
            ),
            SUMMARY_JOINS_SOURCE_ID: analysis_join_candidates(workspace),
            SUMMARY_TABLE_METADATA_SOURCE_ID: tuple(
                candidate
                for candidate in apm_table_metadata_candidates(workspace)
                if candidate.metadata.get("table") in base_tables
            ),
            SUMMARY_TABLE_PROFILE_SOURCE_ID: tuple(
                candidate
                for candidate in apm_table_profile_candidates(workspace)
                if candidate.metadata.get("table") in base_tables
            ),
            SUMMARY_PLANNING_SOURCE_ID: (
                (
                    ContextCandidate(
                        source_ref="planning:context",
                        source=planning_context,
                        representations={"planning_context": planning_context},
                        metadata={"artifact": "planning_context"},
                    ),
                )
                if any(str(value or "").strip() for value in planning_context.values())
                else ()
            ),
        },
        selector_context={},
    )


PROMOTION_SUBJECT_SOURCE_ID = "promotion_subject"
PROMOTION_RCM_SOURCE_ID = "rcm_rows"
PROMOTION_TABLE_SOURCE_ID = "table_metadata"

# What a fitting turn is shown of an RCM row. The narrative and the control
# requirements decide which control a flagged condition would be a failure of;
# planned tests, rollups and evidence references say nothing about that and
# would put the row's existing conclusions in front of a turn whose whole job
# is to judge the row's coverage independently of them.
_PROMOTION_ROW_FIELDS = (
    "id",
    "process",
    "risk",
    "risk_rating",
    "control",
    "control_attributes",
)


def promotion_scope(workspace: Workspace, analysis_id: str) -> ContextScope:
    """Build the candidate scope for fitting one saved procedure to the matrix.

    The procedure is supplied through ``analysis_promotion.fitting_subject``
    rather than through the memo's result projection: that projection carries
    bounded statistics and verdict text a memo narrates, and this turn needs
    the *definition* — the catalog test and its parameters, or the code — which
    is what decides whether the step can be carried through unchanged.
    """
    from ...analysis_promotion import fitting_subject

    analysis = next(
        (
            item
            for item in workspace.analyses
            if str(item.get("id")) == str(analysis_id)
        ),
        None,
    )
    subject = fitting_subject(analysis or {})
    return ContextScope(
        candidates={
            PROMOTION_SUBJECT_SOURCE_ID: (
                (
                    ContextCandidate(
                        source_ref=f"analysis:{analysis_id}",
                        source=subject,
                        representations={"analysis_result": subject},
                        metadata={"analysis_id": str(analysis_id)},
                    ),
                )
                if analysis is not None
                else ()
            ),
            PROMOTION_RCM_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=f"rcm:{row['id']}",
                    source=projection,
                    representations={"current_artifact": projection},
                    metadata={"rcm_id": str(row["id"])},
                )
                for row in workspace.rcm
                for projection in (
                    {key: row.get(key) for key in _PROMOTION_ROW_FIELDS},
                )
            ),
            # The same schema projection test generation is given, and for the
            # same two reasons: a step written against column names alone has
            # to guess what a status column holds, and each frame's declared
            # ``grain`` is what lets the anchor rule be checked here rather
            # than discovered at commit.
            #
            # Scoped, though, rather than handed the whole workspace. That
            # projection carries category values and is large, so twenty frames
            # overran the source budget and the resolver dropped eight of them
            # by reference order — including two base tables and the joined
            # frame one carried procedure actually reads. The turn then
            # declined it, reporting that no supplied table held the columns,
            # which was true of what it saw and false of the engagement. A
            # false decline is worse than a missing one: it is recorded as a
            # judgement about the audit.
            #
            # What a fitting turn can need is bounded and knowable: the frame
            # the procedure runs against, and the base populations it might
            # declare as its grain. Nothing else is reachable from a carried
            # procedure or a catalog test.
            PROMOTION_TABLE_SOURCE_ID: tuple(
                candidate
                for candidate in test_generate_table_metadata_candidates(workspace)
                if candidate.metadata.get("table")
                in {
                    str(subject.get("source_frame_name") or ""),
                    *(str(table.get("name") or "") for table in workspace.tables),
                }
            ),
        },
        selector_context={},
    )


DOCUMENT_ANALYSIS_METADATA_SOURCE_ID = "document_metadata"
DOCUMENT_ANALYSIS_IDENTITY_SOURCE_ID = "document_identity"
DOCUMENT_ANALYSIS_CHUNK_SOURCE_ID = "document_chunk"
DOCUMENT_CATEGORY_SOURCE_ID = "document_category"
DOCUMENT_CLASSIFICATION_SOURCE_ID = "document_classification"
DOCUMENT_STRUCTURED_CHUNK_SOURCE_ID = "document_structured_chunk"
DOCUMENT_ANALYSIS_VISUAL_SOURCE_ID = "document_page_images"
DOCUMENT_READ_TEXT_SOURCE_ID = "document_pages"
#: The read takes both modalities in one call, so it reuses the visual source
#: id rather than coining a second one — the prepared-media handle shape and
#: the ``page_image`` representation are the same ones the page-at-a-time path
#: already resolves.
DOCUMENT_READ_IMAGE_SOURCE_ID = DOCUMENT_ANALYSIS_VISUAL_SOURCE_ID
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


def document_category_scope(
    workspace: Workspace,
    document_id: str,
    text: str,
) -> ContextScope:
    """Build the local scope for one document category unit.

    The opening page and nothing else, for the reason the type unit gets nothing
    else: a filename like ``PO-2025-17.pdf`` answers the question outright, and
    supplying it would let the worker report what it read off the path. That
    matters more here than for the type, because the filename is exactly the
    evidence this stage exists to stop relying on.
    """

    payload = {"document_id": str(document_id), "page": 1, "text": str(text or "")}
    return ContextScope(
        candidates={
            DOCUMENT_CATEGORY_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"document:{document_id}:page:1",
                    source=payload,
                    representations={"raw_pages": payload},
                    metadata={"document_id": str(document_id), "page": 1},
                ),
            ),
        },
    )


def document_classification_scope(
    workspace: Workspace,
    document_id: str,
    text: str,
) -> ContextScope:
    """Build the local scope for one document type-classification unit.

    The opening page and nothing else — no metadata projection, no filename.
    A filename like ``PO-2025-17.pdf`` names the type outright, and supplying it
    would let the worker report a label it read off the path rather than off the
    document. The same reasoning already keeps the voucher profile on
    ``document_identity_candidate``; here it is stricter still, because there is
    no identifier the classifier legitimately needs.
    """

    payload = {"document_id": str(document_id), "page": 1, "text": str(text or "")}
    return ContextScope(
        candidates={
            DOCUMENT_CLASSIFICATION_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"document:{document_id}:page:1",
                    source=payload,
                    representations={"raw_pages": payload},
                    metadata={"document_id": str(document_id), "page": 1},
                ),
            ),
        },
    )


CYCLE_SCHEMA_SOURCE_ID = "cycle_schemas"
CYCLE_REQUIREMENT_SOURCE_ID = "cycle_requirements"


def _cycle_requirement_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """What the matrix asks this engagement's cycle to demonstrate.

    Supplied because a ruleset exists to answer the matrix, and a worker shown
    only the vocabulary was being asked to guess the question. It guessed a
    reasonable cycle and answered a different one: the matrix required the goods
    receipt to name its purchase order, the proposal bound the pair by the
    order naming its receipt, and nothing tested the inspection the matrix asked
    for. Neither turn was wrong on its own; they were never shown each other.

    Carried as the comparisons themselves rather than the rows, so the worker
    reads operands in the same vocabulary it writes assertions in.
    """

    from ... import cycle_linking

    required: list[dict] = []
    for row in workspace.rcm or []:
        for attribute in row.get("control_attributes") or []:
            if not isinstance(attribute, Mapping):
                continue
            if attribute.get("evidence_kind") != "transaction_cycle":
                continue
            for comparison in attribute.get("required_comparisons") or []:
                if not isinstance(comparison, Mapping):
                    continue
                left = comparison.get("left") or {}
                right = comparison.get("right")
                required.append({
                    "rcm_id": str(row.get("id") or ""),
                    "control_attribute": str(attribute.get("key") or ""),
                    "requirement": str(attribute.get("requirement") or ""),
                    "comparison": str(comparison.get("key") or ""),
                    "left": {
                        "document_type": str(left.get("document_type") or ""),
                        "field": str(left.get("field") or ""),
                    },
                    "right": (
                        {
                            "document_type": str((right or {}).get("document_type") or ""),
                            "field": str((right or {}).get("field") or ""),
                        }
                        if isinstance(right, Mapping)
                        else None
                    ),
                    "why": str(comparison.get("rationale") or ""),
                })
    if not required:
        return ()
    content = {"required_comparisons": required}
    return (
        ContextCandidate(
            source_ref="workspace:rcm",
            source=content,
            representations={"planning_context": content},
            metadata={"comparisons": len(required)},
        ),
    )


def cycle_linkage_scope(workspace: Workspace) -> ContextScope:
    """The vocabulary a cycle proposal may be written against, and what it must answer.

    One candidate per induced schema, carrying the type and its fields — the
    whole vocabulary, because a rule naming anything else cannot be evaluated.
    No rows and no document text: rules describe what the documents *are*, and
    a proposal authored from what they happen to contain would fit itself to
    this corpus rather than to the cycle.
    """

    from ... import cycle_linking

    schemas = cycle_linking.schema_catalog(workspace)
    requirements = _cycle_requirement_candidates(workspace)
    if not schemas:
        return ContextScope(
            candidates={
                CYCLE_SCHEMA_SOURCE_ID: (),
                CYCLE_REQUIREMENT_SOURCE_ID: requirements,
            }
        )
    # One item carrying every type, not one per type. A cycle is a statement
    # about how the whole set relates, so the vocabulary is atomic: a budget
    # that dropped one schema would leave a proposal describing a cycle with a
    # role missing and no way to say which one, which is worse than not
    # proposing at all.
    content = {"schemas": schemas}
    return ContextScope(
        candidates={
            CYCLE_SCHEMA_SOURCE_ID: (
                ContextCandidate(
                    source_ref="workspace:schemas",
                    source=content,
                    representations={"cycle_schema": content},
                    metadata={
                        "document_types": [
                            item["document_type"] for item in schemas
                        ]
                    },
                ),
            ),
            CYCLE_REQUIREMENT_SOURCE_ID: requirements,
        }
    )


def document_structured_chunk_scope(
    workspace: Workspace,
    document_id: str,
    chunk: Mapping[str, object],
) -> ContextScope:
    """Build the local scope for one schema-guided extraction unit.

    Exactly one chunk, and no metadata projection: the schema names the fields,
    so a filename carrying an identifier would only invite the worker to report a
    value it read off the path. The same narrowing the pack profile already
    applies through ``document_identity_candidate``, taken one step further now
    that the vocabulary no longer has to be chosen from the document.
    """

    payload = {
        "id": str(chunk.get("id") or ""),
        "page": int(chunk.get("page") or 0),
        "text": str(chunk.get("text") or ""),
    }
    return ContextScope(
        candidates={
            DOCUMENT_STRUCTURED_CHUNK_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"document:{document_id}:chunk:{payload['id']}",
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


def document_evidence_read_scope(
    workspace: Workspace,
    document_id: str,
    text: str,
    pages: Iterable[int] = (),
    handles: Iterable[Mapping[str, object]] = (),
) -> ContextScope:
    """Build the local scope for one whole-document evidence read.

    Two sources, because a scanned page is where a control signature lives. A
    text-only read fails two ways: a fully scanned confirmation contributes
    nothing and produces no records, silently; and the commoner case is worse —
    a mostly-digital PDF with one scanned approval page is read for everything
    except the page the control is on. ``second_approver`` on three of eighteen
    payment instructions is exactly the kind of field that lives in a signature
    block, and absence there is the finding.

    No metadata projection and no filename, for the reason the schema sample got
    none: a path like ``PMT-2025-00074.pdf`` names a value the document may not
    actually print, and a reading that took it would cite a filename.

    Media is optional. Most evidence is digital and routes no page visually, so
    an empty handle set is an ordinary reading rather than a contract breach —
    unlike ``document_visual_page_scope``, whose whole unit *is* the page.
    """

    payload = {
        "document_id": str(document_id),
        "text": str(text or ""),
        "pages": [int(page) for page in pages],
    }
    media = [dict(item) for item in handles]
    candidates: dict[str, tuple] = {
        DOCUMENT_READ_TEXT_SOURCE_ID: (
            ContextCandidate(
                source_ref=f"document:{document_id}:reading",
                source=payload,
                representations={"raw_pages": payload},
                metadata={"document_id": str(document_id)},
            ),
        ),
    }
    if media:
        candidates[DOCUMENT_READ_IMAGE_SOURCE_ID] = tuple(
            ContextCandidate(
                source_ref=str(item.get("source_ref") or ""),
                source=dict(item),
                representations={"page_image": dict(item)},
                metadata={
                    "document_id": str(document_id),
                    "page": item.get("page"),
                },
            )
            for item in media
        )
    return ContextScope(candidates=candidates)


def document_visual_page_scope(
    workspace: Workspace,
    document_id: str,
    handles: Iterable[Mapping[str, object]],
) -> ContextScope:
    """Supply only safe prepared-media handles for one visual map unit."""

    media = [dict(item) for item in handles]
    if not media:
        raise WorkspaceError(
            f"Document '{document_id}' has no prepared visual page parts."
        )
    return ContextScope(
        candidates={
            DOCUMENT_ANALYSIS_METADATA_SOURCE_ID: (
                document_metadata_candidate(workspace, document_id),
            ),
            DOCUMENT_ANALYSIS_VISUAL_SOURCE_ID: tuple(
                ContextCandidate(
                    source_ref=str(item.get("source_ref") or ""),
                    source={
                        key: value
                        for key, value in item.items()
                        if key != "cache_key"
                    },
                    representations={"page_image": item},
                    metadata={
                        "document_id": str(document_id),
                        "page": int(item.get("page") or 0),
                        "frame": int(item.get("frame") or 0),
                        "variant": str(item.get("variant") or ""),
                        "tile_order": int(item.get("tile_order") or 0),
                    },
                )
                for item in sorted(
                    media,
                    key=lambda value: (
                        int(value.get("page") or 0),
                        int(value.get("tile_order") or 0),
                    ),
                )
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
    "FINDING_TEST_SOURCE_ID",
    "linked_test_projections",
    "ANALYSIS_CURRENT_SOURCE_ID",
    "ANALYSIS_REGISTRY_SOURCE_ID",
    "ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS",
    "ANALYSIS_RELATED_FRAMES_SOURCE_ID",
    "ANALYSIS_RELATIONSHIP_SOURCE_ID",
    "ANALYSIS_TARGET_AGGREGATE_SOURCE_ID",
    "ANALYSIS_TARGET_PROFILE_SOURCE_ID",
    "ANALYSIS_TARGET_SCHEMA_SOURCE_ID",
    "MAX_AGGREGATE_COLUMNS",
    "analysis_aggregate_candidates",
    "analysis_definition_scope",
    "analysis_reading_scope",
    "analysis_relationship_candidates",
    "PROMOTION_SUBJECT_SOURCE_ID",
    "PROMOTION_RCM_SOURCE_ID",
    "PROMOTION_TABLE_SOURCE_ID",
    "promotion_scope",
    "APM_DOCUMENT_SOURCE_ID",
    "APM_METHODOLOGY_SOURCE_ID",
    "APM_TABLE_METADATA_SOURCE_ID",
    "APM_TABLE_PROFILE_SOURCE_ID",
    "APM_PLANNING_SOURCE_ID",
    "APM_POPULATION_SOURCE_ID",
    "APM_TEMPLATE_SOURCE_ID",
    "APM_CURRENT_ARTIFACT_SOURCE_ID",
    "CYCLE_SCHEMA_SOURCE_ID",
    "DOCUMENT_ANALYSIS_CHUNK_SOURCE_ID",
    "DOCUMENT_ANALYSIS_CHUNKS_SOURCE_ID",
    "DOCUMENT_ANALYSIS_IDENTITY_SOURCE_ID",
    "DOCUMENT_ANALYSIS_METADATA_SOURCE_ID",
    "DOCUMENT_ANALYSIS_VISUAL_SOURCE_ID",
    "DOCUMENT_QA_ITEM_SOURCE_ID",
    "DOCUMENT_QA_PAGE_SOURCE_ID",
    "FINDING_EXECUTION_SOURCE_ID",
    "FINDING_OBSERVATION_SOURCE_ID",
    "FINDING_ROW_SOURCE_ID",
    "INTAKE_STAGED_FILE_SOURCE_ID",
    "PLANNING_CONTEXT_CURRENT_SOURCE_ID",
    "PLANNING_CONTEXT_DOCUMENT_SOURCE_ID",
    "TEST_GENERATE_PLANNING_SOURCE_ID",
    "TEST_GENERATE_ROW_SOURCE_ID",
    "TEST_GENERATE_TABLE_METADATA_SOURCE_ID",
    "TEST_GENERATE_TABLE_PROFILE_SOURCE_ID",
    "TEST_GENERATE_DOCUMENT_SOURCE_ID",
    "TEST_GENERATE_METHODOLOGY_SOURCE_ID",
    "TEST_GENERATE_TRANSACTION_EVIDENCE_SOURCE_ID",
    "test_generate_scope",
    "RCM_CURRENT_APM_SOURCE_ID",
    "RCM_CURRENT_ROWS_SOURCE_ID",
    "RCM_DOCUMENT_SOURCE_ID",
    "RCM_METHODOLOGY_SOURCE_ID",
    "RCM_PLANNING_SOURCE_ID",
    "RCM_TABLE_METADATA_SOURCE_ID",
    "RCM_TABLE_PROFILE_SOURCE_ID",
    "RCM_SMALL_TABLE_ROWS_SOURCE_ID",
    "RCM_TEMPLATE_SOURCE_ID",
    "apm_document_candidates",
    "apm_document_methodology_scope",
    "apm_methodology_candidates",
    "supplied_source_provenance",
    "apm_table_metadata_candidates",
    "apm_table_profile_candidates",
    "population_summary_candidates",
    "MAX_SUMMARY_COLUMNS",
    "small_table_row_candidates",
    "MAX_SMALL_TABLE_ROWS",
    "document_chunk_scope",
    "document_metadata_candidate",
    "document_qa_page_candidates",
    "document_qa_scope",
    "document_reduction_scope",
    "document_evidence_read_scope",
    "document_visual_page_scope",
    "document_test_document_candidates",
    "finding_draft_scope",
    "document_category_scope",
    "intake_classification_scope",
    "intake_staged_file_candidates",
    "planning_context_document_candidates",
    "planning_context_scope",
    "test_draft_methodology_candidates",
    "test_draft_row_candidates",
    "rcm_current_row_candidates",
    "rcm_scope",
]


CYCLE_VOUCH_ITEM_SOURCE_ID = "cycle_item"


def cycle_vouch_scope(workspace: Workspace, test_id: str, item_id: str) -> ContextScope:
    """The pending checks of one linked cycle, with the values each one reads.

    One candidate carrying the whole item, not one per check: a reader deciding
    whether an amount agrees needs the rest of the cycle in front of it, and
    splitting the grid would ask each half of a comparison separately.
    """
    test = doc_tests.load_test(workspace, str(test_id))
    projection = cycle_vouching.judgment_request(workspace, test, str(item_id))
    return ContextScope(
        candidates={
            CYCLE_VOUCH_ITEM_SOURCE_ID: (
                ContextCandidate(
                    source_ref=f"cycleitem:{item_id}",
                    source=projection,
                    representations={"current_artifact": projection},
                    metadata={
                        "document_test_id": str(test_id),
                        "item_id": str(item_id),
                        "checks": len(projection.get("checks") or []),
                    },
                ),
            ),
        },
        selector_context={"cycle_vouch_item": str(item_id)},
    )
