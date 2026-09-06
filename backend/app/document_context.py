"""The sole model-facing boundary for engagement document content."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping

from . import document_analysis, document_search, documents
from .agent.prompts import document_summary_heading
from .workspaces import Workspace, WorkspaceError

SMALL_DOCUMENT_CHARACTERS = 32_000
MAX_EXCERPT_CHARACTERS = 8_000
#: Bounds for the structured-record representation. A reading is dense — one
#: record is a couple of dozen short values — so the caps exist to keep a whole
#: corpus out of one turn's context, not to withhold anything: what a cap left
#: out is always counted back in ``population_records``.
MODEL_RECORD_LIMIT = 25
MODEL_RECORD_FIELDS = 30
MODEL_RECORD_VALUE_CHARACTERS = 240
MODEL_RECORD_EXCERPT_CHARACTERS = 160
MODEL_ADDITIONAL_FIELDS = 10


def apm_document_context(
    workspace: Workspace,
    document_id: str,
    *,
    max_characters: int = SMALL_DOCUMENT_CHARACTERS,
    include_audit_notes: bool = True,
) -> dict:
    """Adapt a document analysis to one bounded APM representation.

    The adapter deliberately composes :func:`get_document_context` instead of
    reading analysis sidecars or extracted pages itself.  That keeps document
    validity, auditor overrides, and the model-facing privacy boundary in one
    place while giving the generic agent context resolver a plain local value.

    ``include_audit_notes`` is off for turns that must reason from the document's
    own process description rather than from conclusions already drawn about it.
    The audit-notes block is a numbered deficiency list, and a turn that produces
    rows will transcribe such a list one observation per row instead of deriving
    its own set.  The APM is the artifact that carries those observations
    forward, so a turn taking the APM as a parent already has them.
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
    audit_notes = (
        get_document_context(
            workspace,
            document_id,
            "audit_notes",
            max_characters=max_chars,
            purpose="apm_context",
            record_activity=False,
        )
        if include_audit_notes
        else {}
    )
    record = next(
        (item for item in workspace.documents if item.get("id") == document_id), None
    )
    name = documents.display_name(record, document_id)
    sections = []
    if summary.get("content"):
        sections.append(
            f"{document_summary_heading(name)}\n{summary['content']}"
        )
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


# ------------------------------------------------------- structured readings
def record_type_catalog(workspace: Workspace) -> list[dict]:
    """One entry per document type: what it holds and the vocabulary it holds it in.

    Both document counts are reported because their difference answers a
    question the records alone cannot. ``documents`` is what the corpus is
    typed as; ``documents_with_records`` is what produced a reading against the
    type's current schema. A document in the gap — never analysed, retyped, or
    extracted against a schema that has since moved — contributes nothing, and
    is indistinguishable from one that had nothing to say unless the gap is
    stated.
    """

    from . import cycle_linking, document_classification, document_schemas

    records, _hashes = cycle_linking.structured_evidence(workspace)
    by_type: dict[str, list[dict]] = {}
    for record in records:
        by_type.setdefault(str(record.get("document_type") or ""), []).append(record)
    typed: dict[str, int] = {}
    for document in workspace.documents:
        document_type = document_classification.document_type(
            workspace, str(document.get("id") or "")
        )
        if document_type:
            typed[document_type] = typed.get(document_type, 0) + 1
    catalog = []
    for document_type in sorted(set(typed) | set(by_type)):
        rows = by_type.get(document_type) or []
        schema = document_schemas.load_schema(workspace, document_type) or {}
        declared = list(schema.get("fields") or [])
        projected = [
            {
                key: str(field.get(key) or "")
                for key in ("name", "label", "role", "value_type")
                if field.get(key)
            }
            for field in declared[:MODEL_RECORD_FIELDS]
        ]
        catalog.append({
            "document_type": document_type,
            "documents": typed.get(document_type, 0),
            "documents_with_records": len({
                str(row.get("document_id") or "") for row in rows
            }),
            "records": len(rows),
            "schema_version": schema.get("schema_version"),
            "fields": projected,
            "fields_truncated": len(declared) > len(projected),
        })
    return catalog


def _record_value(entry: Mapping, anchors: Mapping[str, Mapping]) -> dict:
    """One stated value with the page and excerpt its extractor cited for it."""

    value = {"value": str(entry.get("value") or "")[:MODEL_RECORD_VALUE_CHARACTERS]}
    citation = anchors.get(str(entry.get("citation") or "")) or {}
    if citation:
        value["page"] = int(citation.get("page") or 1)
        value["excerpt"] = str(citation.get("excerpt") or "")[
            :MODEL_RECORD_EXCERPT_CHARACTERS
        ]
    return value


def structured_records(
    workspace: Workspace,
    *,
    document_type: str = "",
    document_ids: Iterable[str] = (),
    fields: Iterable[str] = (),
    limit: int = MODEL_RECORD_LIMIT,
    purpose: str = "structured_records",
    run_id: str | None = None,
    stage: str | None = None,
    record_activity: bool = True,
) -> dict:
    """The readings taken off evidence documents, as values rather than prose.

    A reading is what the document pipeline already extracted against the
    type's frozen schema, and every value carries the citation the extractor
    recorded for it — so an answer drawn from here stands on a page without a
    page ever being fetched, at a few hundred characters where the excerpt path
    costs tens of thousands.

    Records extracted against a schema that has since moved are not
    reinterpreted under today's vocabulary: they are absent, and the documents
    holding them are named in ``unread_documents`` rather than left to be
    inferred from a count that came back smaller than expected.
    """

    from . import cycle_linking, document_classification, document_schemas

    started = time.monotonic()
    document_type = str(document_type or "").strip()
    wanted_ids = [str(value).strip() for value in document_ids or [] if str(value).strip()]
    requested = [str(value).strip() for value in fields or [] if str(value).strip()]
    limit = min(MODEL_RECORD_LIMIT, max(1, int(limit or MODEL_RECORD_LIMIT)))

    known = {str(item.get("id") or ""): item for item in workspace.documents}
    unknown_ids = sorted({value for value in wanted_ids if value not in known})
    if unknown_ids:
        raise WorkspaceError("Unknown document id(s): " + ", ".join(unknown_ids) + ".")

    records, _hashes = cycle_linking.structured_evidence(workspace)
    if document_type:
        schema = document_schemas.load_schema(workspace, document_type)
        if schema is None:
            available = sorted({
                str(row.get("document_type") or "") for row in records
            } - {""})
            raise WorkspaceError(
                f"Document type '{document_type}' has no stamped schema. "
                + (
                    "Types carrying readings: " + ", ".join(available) + "."
                    if available
                    else "No document type carries a current reading yet."
                )
            )
        rows = [
            row for row in records
            if str(row.get("document_type") or "") == document_type
        ]
    else:
        schema = None
        rows = list(records)
    # Which documents of the type have a reading is a property of the type, not
    # of the caller's id filter: computing it after the filter would report
    # every unasked-for document as unread.
    with_records = {str(row.get("document_id") or "") for row in rows}
    if wanted_ids:
        selected = set(wanted_ids)
        rows = [row for row in rows if str(row.get("document_id") or "") in selected]

    declared = [
        str(field.get("name") or "") for field in (schema or {}).get("fields") or []
    ]
    unknown_fields = sorted({name for name in requested if declared and name not in declared})
    names = (
        [name for name in requested if not declared or name in declared]
        if requested
        else declared[:MODEL_RECORD_FIELDS]
    )
    fields_truncated = not requested and len(declared) > len(names)

    population = len(rows)
    anchors_by_document: dict[str, dict] = {}
    projected = []
    for row in rows[:limit]:
        document_id = str(row.get("document_id") or "")
        if document_id not in anchors_by_document:
            detail = document_analysis.load_analysis(
                workspace, document_id, document=known.get(document_id), with_status=False
            )
            artifact = detail.get("effective") or {}
            anchors_by_document[document_id] = {
                "analysis_id": artifact.get("id"),
                "anchors": {
                    str(item.get("id") or ""): item
                    for item in artifact.get("citations") or []
                },
            }
        anchors = anchors_by_document[document_id]["anchors"]
        # Without a named type there is no declared vocabulary to project
        # against, so the record's own stated names stand in for it.
        wanted = names or list(
            dict.fromkeys(
                str(field.get("name") or "")
                for field in row.get("fields") or []
                if str(field.get("name") or "")
            )
        )[:MODEL_RECORD_FIELDS]
        stated: dict[str, list[dict]] = {}
        missing: list[str] = []
        for name in wanted:
            entries = cycle_linking.stated(row, name)
            if not entries:
                missing.append(name)
                continue
            stated[name] = [_record_value(entry, anchors) for entry in entries]
        projected.append({
            "document_id": document_id,
            "document_title": documents.display_name(known.get(document_id), document_id),
            "document_type": str(row.get("document_type") or ""),
            "record_index": int(row.get("record_index") or 0),
            "record_id": str(row.get("record_id") or ""),
            "analysis_id": anchors_by_document[document_id]["analysis_id"],
            "fields": stated,
            # Named rather than omitted: a field the reading does not state is
            # often the thing the question was about, and silence about it
            # reads as a value.
            "missing_fields": missing,
            "additional_fields": [
                {
                    "name": str(field.get("name") or ""),
                    "value": str(field.get("value") or "")[:MODEL_RECORD_VALUE_CHARACTERS],
                }
                for field in (row.get("additional_fields") or [])[:MODEL_ADDITIONAL_FIELDS]
                if str(field.get("value") or "").strip()
            ],
        })

    unread = []
    if document_type:
        unread = [
            {
                "document_id": str(document.get("id") or ""),
                "title": documents.display_name(document, str(document.get("id") or "")),
            }
            for document in document_classification.documents_of_type(
                workspace, document_type
            )
            if str(document.get("id") or "") not in with_records
        ]

    result = {
        "document_type": document_type or None,
        "records": projected,
        "population_records": population,
        "returned_records": len(projected),
        "truncated": population > len(projected),
        "unread_documents": unread,
        "unknown_fields": unknown_fields,
        "fields_truncated": fields_truncated,
    }
    duration = round((time.monotonic() - started) * 1000, 2)
    result["retrieval_duration_ms"] = duration
    if record_activity and projected:
        pages = sorted({
            int(value["page"])
            for record in projected
            for entries in record["fields"].values()
            for value in entries
            if value.get("page") is not None
        })
        documents.append_activity(
            workspace, run_id=run_id, stage=stage, task=None, purpose=purpose,
            provider=None, model=None, vision_used=False, prompt_version=None,
            template_versions=[], knowledge_packs=[],
            document_ids=sorted({record["document_id"] for record in projected}),
            page_ranges=pages,
            source_hashes=sorted({
                str((known.get(record["document_id"]) or {}).get("sha1") or "")
                for record in projected
            } - {""}),
            response_at=documents.utcnow(), response_hash=None, artifact_ref=None,
            disposition="context", representation="structured_record",
            analysis_id=None, search_query_hash=None,
            characters_supplied=len(json.dumps(projected, default=str)),
            cache_hit=True, retrieval_duration_ms=duration, model_duration_ms=None,
            context_outcome="trimmed" if result["truncated"] else "supplied",
        )
    return result
