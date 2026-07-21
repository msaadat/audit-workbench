"""Domain adapters that populate local context-resolver candidate scopes.

These functions translate existing document and methodology context builders
to the generic data-only candidate contract.  They do not select sources,
enforce context policy, call a model, or duplicate domain retrieval logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ... import document_context, methodology
from ...workspaces import Workspace, WorkspaceError
from .resolver import ContextCandidate, ContextScope


APM_DOCUMENT_SOURCE_ID = "documents"
APM_METHODOLOGY_SOURCE_ID = "methodology"


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


def apm_document_methodology_scope(
    workspace: Workspace,
    *,
    planning_context: Mapping[str, object] | None = None,
    document_ids: Iterable[str] | None = None,
) -> ContextScope:
    """Build only the document/methodology portion of the future APM scope.

    Live APM execution is intentionally not wired here; P4.8 owns that vertical
    slice.  P4.6 will add table candidates to this scope.
    """
    context = dict(planning_context or workspace.planning.get("context") or {})
    context["apm_query"] = " ".join(
        str(context.get(key) or "")
        for key in ("objective", "scope", "background_notes", "entity", "period")
    ).strip() or "internal audit risk controls procedures"
    return ContextScope(
        candidates={
            APM_DOCUMENT_SOURCE_ID: apm_document_candidates(
                workspace,
                document_ids=document_ids,
            ),
            APM_METHODOLOGY_SOURCE_ID: apm_methodology_candidates(workspace),
        },
        selector_context=context,
    )


__all__ = [
    "APM_DOCUMENT_SOURCE_ID",
    "APM_METHODOLOGY_SOURCE_ID",
    "apm_document_candidates",
    "apm_document_methodology_scope",
    "apm_methodology_candidates",
]
