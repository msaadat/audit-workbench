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
    model_context,
    templates_store,
)
from ...analysis_memo import flatten_embeds
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
            "title": document.get("title") or document.get("source") or document_id,
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
    """Expose bounded statistical profiles, plus category values where safe.

    A value list is kept on a column only when it is a *category domain*, not
    its rows restated: the table has a population and each value recurs
    across it (``MIN_CATEGORY_ROWS``, ``MIN_CATEGORY_REPETITION`` — the same
    gate ``test_generate_table_metadata_candidates`` applies), and the
    underlying frequency list holds one entry per distinct value rather than a
    truncated top-N. Anything narrower carries its distinct count alone.
    """
    candidates = []
    for table_name in workspace.table_names():
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


# A table this small is one an aggregate profile cannot describe faithfully:
# min/max/null statistics over a handful of rows lose exactly the correlation
# (which row holds which value) that a reference or dimension table is for.
# Below this ceiling the whole table is supplied instead of its profile.
MAX_SMALL_TABLE_ROWS = 50


def small_table_row_candidates(workspace: Workspace) -> tuple[ContextCandidate, ...]:
    """Expose the complete rows of tables at or under the small-table ceiling.

    Row count is read from the cached profile so this never triggers a fresh
    scan; only a table already known to be small loads its frame at all.
    """
    candidates = []
    for table_name in workspace.table_names():
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
            RCM_SMALL_TABLE_ROWS_SOURCE_ID: small_table_row_candidates(workspace),
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
    "assertion",
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


def _test_generate_vouch_profile(
    workspace: Workspace,
    document_id: str,
) -> dict[str, object] | None:
    """Project extracted voucher fields without their transaction values.

    Generation needs the document type and the path keys extraction actually
    produced.  It does not need the identifiers, amounts, dates, or parties
    themselves: those remain local and are resolved by the deterministic cycle
    executor.  Supplying only path suffixes prevents the model from inventing a
    type or field key while preserving the row-data privacy boundary.
    """

    return doc_tests.voucher_document_profile(workspace, document_id)


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
    include_vouch_profile: bool = False,
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
        if include_vouch_profile:
            content["vouch_profile"] = _test_generate_vouch_profile(
                workspace, document_id
            )
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




TEST_GENERATE_PLANNING_SOURCE_ID = "planning_context"
TEST_GENERATE_ROW_SOURCE_ID = "rcm_row"
TEST_GENERATE_TABLE_METADATA_SOURCE_ID = "table_metadata"
TEST_GENERATE_DOCUMENT_SOURCE_ID = "documents"
TEST_GENERATE_METHODOLOGY_SOURCE_ID = "methodology"

# A column's value set is only a *category domain* — as opposed to its rows
# restated — when there is a population and each value recurs across it. Below
# either bound the values are withheld and the column carries its distinct count
# alone, so a narrow or near-unique column can never become a row disclosure.
MIN_CATEGORY_ROWS = 20
MIN_CATEGORY_REPETITION = 4


def _test_generate_anchor_candidates(
    workspace: Workspace,
    table_name: str,
    columns: list[dict],
    document_ids: set[str],
) -> list[dict[str, object]]:
    """Return safe aggregate table/document identifier overlaps.

    The values are compared locally and never leave the machine.  A candidate
    exposes only the table and column names, matched-row/document counts, and
    extracted document types, which is enough to stop the model guessing an
    anchor that can never link.
    """

    return doc_tests.voucher_anchor_candidates(
        workspace,
        table_name,
        columns,
        document_ids=document_ids,
    )


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
    allowed_documents = set(_normalized_document_ids(workspace, document_ids))
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
        content = {
            **table,
            "columns": columns,
            "vouch_anchor_candidates": _test_generate_anchor_candidates(
                workspace,
                table_name,
                columns,
                allowed_documents,
            ),
        }
        candidates.append(
            ContextCandidate(
                source_ref=f"table:{table_name}",
                source=content,
                representations={"table_metadata": content},
                metadata={"table": table_name},
                lexical_text=" ".join(
                    (table_name, *(str(column.get("name") or "") for column in columns))
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
    test_generate_query = " ".join(
        str(value or "")
        for value in (
            row.get("process"),
            row.get("risk"),
            row.get("control"),
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
                include_vouch_profile=True,
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
                        for key in ("id", "label", "state", "runner_note")
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
    execution_ref = str(observation.get("execution_ref") or "")
    execution = {
        "execution_ref": execution_ref,
        "immutable_execution_result": _finding_execution_projection(
            workspace, execution_ref
        ),
        "evidence_anchor": findings.anchor_from_ref(workspace, execution_ref),
    }
    row_projection = {key: row.get(key) for key in _FINDING_ROW_FIELDS}
    test_projection = {key: test.get(key) for key in _FINDING_TEST_FIELDS}
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
    target_lineage = join_diagnostics.frame_lineage(workspace, target)
    family = {
        name
        for name in workspace.table_names()
        if join_diagnostics.frame_lineage(workspace, name) & target_lineage
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


DOCUMENT_ANALYSIS_METADATA_SOURCE_ID = "document_metadata"
DOCUMENT_ANALYSIS_IDENTITY_SOURCE_ID = "document_identity"
DOCUMENT_ANALYSIS_CHUNK_SOURCE_ID = "document_chunk"
DOCUMENT_ANALYSIS_VISUAL_SOURCE_ID = "document_page_images"
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


def document_identity_candidate(
    workspace: Workspace, document_id: str
) -> ContextCandidate:
    """Expose only the identity a citation binds to — never descriptive metadata.

    The voucher profile extracts transaction identifiers from the record's own
    text. A filename like ``EXP-2025-003_PV-2025-003.pdf`` contains exactly those
    identifiers, so supplying the standard metadata projection would let a worker
    report a value it read off the filename and attach a loosely related excerpt.
    This candidate carries the document id, its source hash, and its category and
    nothing else, which is the minimum citation validation needs.
    """
    document = _document_entry(workspace, document_id)
    projection = {
        "document_id": str(document_id),
        "source_sha1": str(document.get("sha1") or ""),
        "category": str(document.get("category") or ""),
    }
    return ContextCandidate(
        source_ref=f"document:{document_id}",
        source=projection,
        # The same registered representation the metadata candidate uses: the
        # narrowing here is what the projection *contains*, not a new privacy
        # class. Adding one would change ``ContextPrivacy`` and rehash every
        # declared spec in the system for no additional protection.
        representations={"current_artifact": projection},
        metadata=dict(projection),
    )


def document_voucher_scope(
    workspace: Workspace,
    document_id: str,
    chunk: Mapping[str, object],
) -> ContextScope:
    """The voucher map unit's scope: one chunk plus bare document identity.

    Deliberately not ``document_chunk_scope``: this profile withholds the
    descriptive metadata that profile supplies, so that every identifier in the
    structured result is one the worker read out of the record itself.
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
            DOCUMENT_ANALYSIS_IDENTITY_SOURCE_ID: (
                document_identity_candidate(workspace, document_id),
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
    "analysis_relationship_candidates",
    "APM_DOCUMENT_SOURCE_ID",
    "APM_METHODOLOGY_SOURCE_ID",
    "APM_TABLE_METADATA_SOURCE_ID",
    "APM_TABLE_PROFILE_SOURCE_ID",
    "APM_PLANNING_SOURCE_ID",
    "APM_TEMPLATE_SOURCE_ID",
    "APM_CURRENT_ARTIFACT_SOURCE_ID",
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
    "small_table_row_candidates",
    "MAX_SMALL_TABLE_ROWS",
    "document_chunk_scope",
    "document_identity_candidate",
    "document_voucher_scope",
    "document_metadata_candidate",
    "document_qa_page_candidates",
    "document_qa_scope",
    "document_reduction_scope",
    "document_visual_page_scope",
    "document_test_document_candidates",
    "finding_draft_scope",
    "intake_classification_scope",
    "intake_staged_file_candidates",
    "planning_context_document_candidates",
    "planning_context_scope",
    "test_draft_methodology_candidates",
    "test_draft_row_candidates",
    "rcm_current_row_candidates",
    "rcm_scope",
]
