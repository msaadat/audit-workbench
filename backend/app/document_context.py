"""The sole model-facing boundary for engagement document content."""

from __future__ import annotations

import hashlib
import time

from . import document_analysis, document_search, documents
from .workspaces import Workspace, WorkspaceError

SMALL_DOCUMENT_CHARACTERS = 32_000
MAX_EXCERPT_CHARACTERS = 8_000


def apm_document_context(
    workspace: Workspace,
    document_id: str,
    *,
    max_characters: int = SMALL_DOCUMENT_CHARACTERS,
) -> dict:
    """Adapt a document analysis to one bounded APM representation.

    The adapter deliberately composes :func:`get_document_context` instead of
    reading analysis sidecars or extracted pages itself.  That keeps document
    validity, auditor overrides, and the model-facing privacy boundary in one
    place while giving the generic agent context resolver a plain local value.
    """
    max_chars = max(1, int(max_characters))
    summary = get_document_context(
        workspace,
        document_id,
        "summary",
        max_characters=max_chars,
        purpose="apm_context",
        record_activity=False,
    )
    audit_notes = get_document_context(
        workspace,
        document_id,
        "audit_notes",
        max_characters=max_chars,
        purpose="apm_context",
        record_activity=False,
    )
    sections = []
    if summary.get("content"):
        sections.append(f"DOCUMENT SUMMARY\n{summary['content']}")
    if audit_notes.get("content"):
        sections.append(f"AUDIT NOTES\n{audit_notes['content']}")
    content = "\n\n".join(sections)
    citations = []
    seen = set()
    for item in [*(summary.get("citations") or []), *(audit_notes.get("citations") or [])]:
        key = (
            item.get("id"),
            item.get("page"),
            item.get("excerpt_hash"),
            item.get("excerpt"),
        )
        if key not in seen:
            seen.add(key)
            citations.append(item)
    available = bool(content)
    return {
        "document_id": document_id,
        "title": summary.get("title") or audit_notes.get("title"),
        "source_sha1": summary.get("source_sha1") or audit_notes.get("source_sha1"),
        "analysis_id": summary.get("analysis_id") or audit_notes.get("analysis_id"),
        "analysis_validity_state": (
            summary.get("analysis_validity_state")
            or audit_notes.get("analysis_validity_state")
        ),
        "content": content,
        "characters": len(content),
        "citations": citations,
        "outcome": "supplied" if available else "unavailable",
        "trimmed": bool(summary.get("trimmed") or audit_notes.get("trimmed")),
    }


def get_document_context(workspace: Workspace, document_id: str, mode: str, *,
                         query: str | None = None, pages: list[int] | None = None,
                         max_characters: int | None = None, purpose: str = "document_context",
                         run_id: str | None = None, stage: str | None = None,
                         record_activity: bool = True) -> dict:
    """Resolve one bounded representation and append content-free provenance."""
    started = time.monotonic(); max_chars = int(max_characters or SMALL_DOCUMENT_CHARACTERS)
    document = next((item for item in workspace.documents if item.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    result = {"document_id": document_id, "title": document.get("title") or document.get("source"),
              "source_sha1": document.get("sha1"), "mode": mode, "content": "",
              "pages": [], "citations": [], "characters": 0, "outcome": "unavailable",
              "analysis_id": None, "analysis_validity_state": None, "trimmed": False}
    if mode in {"summary", "audit_notes", "derived_text", "vision_transcript"}:
        analysis = document_analysis.load_analysis(workspace, document_id, document=document)
        if analysis["effective"]:
            key = (
                "summary_markdown"
                if mode == "summary"
                else "audit_notes_markdown"
                if mode == "audit_notes"
                else "derived_text_markdown"
            )
            content = str(analysis["effective"].get(key) or "")
            result.update(content=content[:max_chars], characters=min(len(content), max_chars),
                          outcome="trimmed" if len(content) > max_chars else "supplied",
                          trimmed=len(content) > max_chars, analysis_id=analysis["effective"]["id"],
                          analysis_validity_state=analysis["status"]["analysis_validity_state"],
                          citations=analysis["effective"].get("citations") or [])
    elif mode == "search_excerpts":
        if not str(query or "").strip():
            raise WorkspaceError("A concrete query is required to search document excerpts.")
        found = document_search.search(workspace, str(query), document_ids=[document_id], max_characters=min(max_chars, MAX_EXCERPT_CHARACTERS))
        content = "\n\n".join(f"[Page {item['page']}]\n{item['excerpt']}" for item in found["results"])
        result.update(content=content, characters=len(content), outcome="trimmed" if found["trimmed"] else ("supplied" if content else "unavailable"),
                      trimmed=found["trimmed"], pages=sorted({item["page"] for item in found["results"]}),
                      citations=[item["citation"] for item in found["results"]])
    elif mode in {"pages", "full"}:
        extracted = documents.extract_document(workspace, document_id)
        total = sum(len(str(page.get("text") or "")) for page in extracted.get("pages") or [])
        if total == 0:
            analysis = document_analysis.load_analysis(
                workspace, document_id, document=document
            )
            derived = (
                str(
                    (analysis.get("effective") or {}).get(
                        "derived_text_markdown"
                    )
                    or ""
                )
                if analysis.get("effective")
                else ""
            )
            if derived:
                page = next(
                    (
                        int(item.get("page") or 1)
                        for item in (
                            analysis.get("effective") or {}
                        ).get("citations")
                        or []
                    ),
                    1,
                )
                bounded = derived[:max_chars]
                result.update(
                    content=f"--- Page {page} (AI-derived visual transcription) ---\n{bounded}",
                    characters=len(bounded),
                    pages=[page],
                    page_items=[
                        {
                            "page": page,
                            "text": bounded,
                            "origin": "vision_transcript",
                            "analysis_id": (
                                analysis.get("effective") or {}
                            ).get("id"),
                            "auditor_confirmed": False,
                        }
                    ],
                    outcome=(
                        "trimmed" if len(derived) > max_chars else "supplied"
                    ),
                    trimmed=len(derived) > max_chars,
                    analysis_id=(analysis.get("effective") or {}).get("id"),
                    analysis_validity_state=analysis["status"]["analysis_validity_state"],
                    citations=(
                        analysis.get("effective") or {}
                    ).get("citations")
                    or [],
                )
                total = -1
        if total == -1:
            pass
        elif mode == "full" and total > min(max_chars, SMALL_DOCUMENT_CHARACTERS):
            result.update(outcome="scope_required")
        else:
            included = documents.prompt_content(workspace, document_id, pages if mode == "pages" else None,
                                                max_characters=max_chars)
            content = "\n\n".join(f"--- Page {page['page']} ---\n{page['text']}" for page in included["pages"])
            result.update(content=content, characters=len(content), pages=[int(page["page"]) for page in included["pages"]],
                          page_items=included["pages"],
                          outcome="trimmed" if included["truncated_pages"] or included["omitted_pages"] else "supplied",
                          trimmed=bool(included["truncated_pages"] or included["omitted_pages"]))
    else:
        raise WorkspaceError("Unknown document context mode.")
    duration = round((time.monotonic() - started) * 1000, 2)
    result["retrieval_duration_ms"] = duration
    if record_activity:
        representation = {
            "full": "raw_pages",
            "pages": "raw_pages",
            "search_excerpts": "excerpt",
            "derived_text": "vision_transcript",
            "vision_transcript": "vision_transcript",
        }.get(mode, mode)
        documents.append_activity(
            workspace, run_id=run_id, stage=stage, task=None, purpose=purpose,
            provider=None, model=None, vision_used=False, prompt_version=None,
            template_versions=[], knowledge_packs=[], document_ids=[document_id],
            page_ranges=result["pages"], source_hashes=[document.get("sha1")], response_at=documents.utcnow(),
            response_hash=None, artifact_ref=result.get("analysis_id"), disposition="context",
            representation=representation, analysis_id=result.get("analysis_id"),
            search_query_hash=hashlib.sha1(str(query).encode()).hexdigest() if query else None,
            characters_supplied=result["characters"], cache_hit=mode in {"summary", "audit_notes"},
            retrieval_duration_ms=duration, model_duration_ms=None, context_outcome=result["outcome"],
        )
    return result


def assistant_attachments(workspace: Workspace, document_ids: list[str], *,
                          max_characters: int = documents.ASSISTANT_DOCUMENT_CONTEXT_MAX_CHARACTERS) -> dict:
    """Resolve unscoped assistant attachments without prefix truncation."""
    unique = list(dict.fromkeys(str(value).strip() for value in document_ids if str(value).strip()))
    prompt_documents, manifest, remaining = [], [], max(0, int(max_characters))
    for document_id in unique:
        result = get_document_context(
            workspace, document_id, "full", max_characters=min(remaining, SMALL_DOCUMENT_CHARACTERS),
            purpose="assistant_attachment", record_activity=False,
        )
        supplied = result["outcome"] == "supplied" and result["characters"] <= remaining
        pages = result.get("page_items") or [] if supplied else []
        if supplied:
            remaining -= result["characters"]
            prompt_documents.append({"document_id": document_id, "title": result["title"],
                                     "source": result["title"], "source_sha1": result["source_sha1"],
                                     "pages": pages})
        document = next(item for item in workspace.documents if item["id"] == document_id)
        total_pages = int(document.get("pages") or 0)
        included_pages = [int(page["page"]) for page in pages]
        manifest.append({"document_id": document_id, "title": result["title"],
                         "source_sha1": result["source_sha1"],
                         "total_pages": total_pages, "included_pages": included_pages,
                         "truncated_pages": [],
                         "omitted_pages": [] if supplied else list(range(1, total_pages + 1)),
                         "characters_included": result["characters"] if supplied else 0,
                         "trimmed": not supplied, "text_state": document.get("text_state"),
                         "context_outcome": result["outcome"] if not supplied else "supplied"})
    return {"documents": prompt_documents, "manifest": manifest,
            "trimmed": any(item["trimmed"] for item in manifest),
            "scope_required": any(item["context_outcome"] == "scope_required" for item in manifest),
            "character_budget": max_characters}
