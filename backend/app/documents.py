"""Local document storage, extraction, model context, chat, and provenance."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
import zipfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

from . import debug_store, llm, telemetry_db
from .agent import prompts
from .evidence import document_anchor
from .workspaces import Workspace, WorkspaceError, write_json_atomic

DOCUMENT_SUFFIXES = {
    ".pdf", ".txt", ".md", ".markdown", ".docx",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
CATEGORIES = {
    "background", "policy", "regulation", "contract", "minutes", "voucher",
    "evidence", "prior_report", "correspondence", "other",
}


def display_name(document: object, fallback: str = "") -> str:
    """The name a reader — or a model reading a document — recognises.

    ``source`` is the file as it arrived, "Minutes of Meeting - CFO.docx".
    ``title`` is a slug derived from it at intake, and anything handed the slug
    quotes it back: findings once read "the documents titled
    `minutes_of_meeting_head_procurment`".
    """
    record = document if isinstance(document, Mapping) else {}
    return str(record.get("source") or record.get("title") or fallback or "")


MIN_TEXT_CHARACTERS = 40
ASSISTANT_DOCUMENT_CONTEXT_MAX_CHARACTERS = 80_000


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha1_bytes(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()


def _documents_dir(workspace: Workspace) -> Path:
    path = workspace.root / "Documents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _document(workspace: Workspace, doc_id: str) -> dict:
    doc = next((item for item in workspace.documents if item.get("id") == doc_id), None)
    if doc is None:
        raise WorkspaceError(f"Document '{doc_id}' not found.")
    return doc


def document_path(workspace: Workspace, document: dict) -> Path:
    root = _documents_dir(workspace).resolve()
    path = (root / str(document.get("file") or "")).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise WorkspaceError("The document file is missing or unsafe.")
    return path


def cache_path(workspace: Workspace, doc_id: str) -> Path:
    return _documents_dir(workspace) / ".extracted" / f"{doc_id}.json"


def cached_extraction(workspace: Workspace, doc_id: str) -> dict | None:
    """Return the cached extraction for the document's current source, or None.

    Read-only: unlike :func:`extract_document` this never extracts, writes, or
    touches the workspace. Deterministic capability readiness and unit expansion
    need to know what text already exists without producing it as a side effect
    of asking.
    """
    document = next(
        (item for item in workspace.documents if item.get("id") == doc_id), None
    )
    if document is None:
        return None
    path = cache_path(workspace, doc_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("source_sha1") != document.get("sha1"):
        return None
    return payload


def _validate_upload(filename: str, category: str) -> tuple[str, str, str]:
    source = Path(filename or "document").name
    suffix = Path(source).suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        raise WorkspaceError(f"Unsupported document type '{suffix or '<none>'}'.")
    category = str(category or "other").lower()
    if category not in CATEGORIES:
        raise WorkspaceError("Unknown document category.")
    return source, suffix, category


def document_by_source(workspace: Workspace, filename: str) -> dict | None:
    source = Path(filename or "document").name.casefold()
    return next(
        (doc for doc in workspace.documents if str(doc.get("source") or "").casefold() == source),
        None,
    )


def add_document(
    workspace: Workspace,
    filename: str,
    content: bytes,
    *,
    category: str = "other",
    note: str = "",
    replace: bool = False,
) -> dict:
    source, suffix, category = _validate_upload(filename, category)
    existing = document_by_source(workspace, source)
    if existing is not None:
        if not replace:
            raise WorkspaceError(
                f"Document '{source}' already exists. Confirm replacement to overwrite it."
            )
        return replace_document(workspace, existing["id"], source, content)

    sha1 = _sha1_bytes(content)
    doc_id = uuid.uuid4().hex[:10]
    target = _documents_dir(workspace) / f"{doc_id}{suffix}"
    target.write_bytes(content)
    doc = {
        "id": doc_id,
        "file": target.name,
        "source": source,
        "source_id": None,
        "relative_path": None,
        "title": Path(source).stem,
        "category": category,
        "pages": None,
        "sha1": sha1,
        "text_state": "image_only" if suffix in IMAGE_SUFFIXES else "pending",
        "note": str(note or ""),
        "created": utcnow(),
        "updated": None,
        "created_by": "user",
        "agent_run_id": None,
    }
    workspace.documents.append(doc)
    workspace.save()
    extract_document(workspace, doc_id)
    from . import document_analysis
    document_analysis.update_status(workspace, doc_id, search_index_state="pending")
    return _document(workspace, doc_id)


def replace_document(workspace: Workspace, doc_id: str, filename: str, content: bytes) -> dict:
    """Replace a document's content in place after the caller confirms it.

    The stable document ID lets existing links resolve to the current file while
    their stored source hash continues to reveal that the evidence has changed.
    """
    from . import document_analysis, document_search
    with document_analysis.document_lock(workspace, doc_id):
        doc = _document(workspace, doc_id)
        source, suffix, _ = _validate_upload(filename, str(doc.get("category") or "other"))
        target = _documents_dir(workspace) / f"{doc_id}{suffix}"
        temporary = _documents_dir(workspace) / f".{doc_id}.upload{suffix}"
        temporary.write_bytes(content)
        old_path = None
        try:
            old_path = document_path(workspace, doc)
        except WorkspaceError:
            pass
        temporary.replace(target)
        if old_path is not None and old_path != target:
            old_path.unlink(missing_ok=True)
        cache_path(workspace, doc_id).unlink(missing_ok=True)
        doc.update(
            file=target.name,
            source=source,
            pages=None,
            sha1=_sha1_bytes(content),
            text_state="image_only" if suffix in IMAGE_SUFFIXES else "pending",
            updated=utcnow(),
        )
        doc.pop("version", None)
        doc.pop("supersedes", None)
        workspace.save()
        document_analysis.invalidate_for_replacement(workspace, doc_id)
        document_search.invalidate(workspace, doc_id)
        extract_document(workspace, doc_id, force=True)
        return _document(workspace, doc_id)


def update_document(workspace: Workspace, doc_id: str, changes: dict) -> dict:
    doc = _document(workspace, doc_id)
    allowed = {"title", "category", "note"}
    if set(changes) - allowed:
        raise WorkspaceError("Unknown document field.")
    if "category" in changes and changes["category"] not in CATEGORIES:
        raise WorkspaceError("Unknown document category.")
    for key in allowed & set(changes):
        doc[key] = str(changes[key] or "")
    workspace.save()
    return doc


def remove_document(workspace: Workspace, doc_id: str) -> None:
    doc = _document(workspace, doc_id)
    try:
        document_path(workspace, doc).unlink(missing_ok=True)
    except WorkspaceError:
        pass
    cache_path(workspace, doc_id).unlink(missing_ok=True)
    from . import document_analysis, document_classification, document_search
    document_analysis.remove_sidecars(workspace, doc_id)
    document_classification.remove_sidecars(workspace, doc_id)
    document_search.remove_sidecars(workspace, doc_id)
    workspace.documents.remove(doc)
    workspace.save()


def _pdf_pages(path: Path) -> list[dict]:
    reader = PdfReader(str(path))
    pages = []
    for number, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        images = 0
        try:
            resources = page.get("/Resources") or {}
            xobjects = resources.get("/XObject") or {}
            xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
            images = sum(
                1 for value in xobjects.values()
                if str((value.get_object() if hasattr(value, "get_object") else value).get("/Subtype")) == "/Image"
            )
        except Exception:
            images = 0
        pages.append({
            "page": number,
            "text": text,
            "characters": len(text),
            "embedded_images": images,
            "image_only": len(text) < MIN_TEXT_CHARACTERS and images > 0,
            "no_usable_text_no_image": (
                len(text) < MIN_TEXT_CHARACTERS and images == 0
            ),
        })
    return pages


def _docx_page(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        paragraphs = []
        for paragraph in (node for node in root.iter() if node.tag.endswith("}p")):
            text = "".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t")).strip()
            if text:
                paragraphs.append(text)
        text = "\n\n".join(paragraphs)
        images = sum(1 for name in archive.namelist() if name.startswith("word/media/"))
    return [{"page": 1, "text": text, "characters": len(text), "embedded_images": images,
             "image_only": len(text) < MIN_TEXT_CHARACTERS and images > 0}]


def extract_document(workspace: Workspace, doc_id: str, *, force: bool = False) -> dict:
    doc = _document(workspace, doc_id)
    cache = cache_path(workspace, doc_id)
    if cache.exists() and not force:
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if payload.get("source_sha1") == doc.get("sha1"):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    path = document_path(workspace, doc)
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            pages = _pdf_pages(path)
        elif suffix in {".txt", ".md", ".markdown"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            pages = [{"page": 1, "text": text, "characters": len(text.strip()), "embedded_images": 0, "image_only": False}]
        elif suffix == ".docx":
            pages = _docx_page(path)
        elif suffix in IMAGE_SUFFIXES:
            from . import document_media

            try:
                frames = document_media.image_frame_count(path)
            except document_media.MediaPreparationError:
                # Extraction records the structural visual source without
                # decoding its bytes. Deterministic media preparation owns the
                # typed corrupt/unsupported verdict at execution time.
                frames = 1
            pages = [
                {
                    "page": page,
                    "text": "",
                    "characters": 0,
                    "embedded_images": 1,
                    "image_only": True,
                }
                for page in range(1, frames + 1)
            ]
        else:
            raise WorkspaceError(f"Unsupported document type '{suffix}'.")
        image_only_count = sum(bool(page["image_only"]) for page in pages)
        usable_text_count = sum(
            int(page.get("characters") or 0) >= MIN_TEXT_CHARACTERS
            for page in pages
        )
        visually_routable_count = sum(
            bool(page.get("image_only"))
            or (
                suffix == ".pdf"
                and int(page.get("characters") or 0) < MIN_TEXT_CHARACTERS
                and int(page.get("embedded_images") or 0) == 0
            )
            for page in pages
        )
        state = (
            "image_only"
            if pages and not usable_text_count and visually_routable_count
            else "partial"
            if visually_routable_count
            else "extracted"
        )
        if not pages:
            state = "failed"
        payload = {
            "document_id": doc_id,
            "source_sha1": doc["sha1"],
            "source_suffix": suffix,
            "state": state,
            "pages": pages,
            "extracted_at": utcnow(),
            "error": None,
        }
    except Exception as error:
        payload = {
            "document_id": doc_id,
            "source_sha1": doc.get("sha1"),
            "source_suffix": suffix,
            "state": "failed",
            "pages": [],
            "extracted_at": utcnow(),
            "error": str(error),
        }
    digest = hashlib.sha1()
    for page in payload.get("pages") or []:
        digest.update(f"\n\fPAGE:{int(page.get('page') or 0)}\n".encode())
        digest.update(str(page.get("text") or "").encode("utf-8"))
    payload["extracted_text_sha1"] = digest.hexdigest()
    write_json_atomic(cache, payload)
    doc.update(pages=len(payload["pages"]) or None, text_state=payload["state"])
    workspace.save()
    from . import document_analysis
    document_analysis.repair_status(workspace, doc_id)
    current_analysis = document_analysis.generated_record(workspace, doc_id)
    has_derived_text = bool(
        (current_analysis or {}).get("derived_text_markdown")
    )
    document_analysis.update_status(
        workspace,
        doc_id,
        search_index_state=(
            "pending"
            if payload["state"] != "image_only" or has_derived_text
            else "unsupported"
        ),
    )
    return payload


def preview(workspace: Workspace, doc_id: str, pages: list[int] | None = None) -> dict:
    doc = _document(workspace, doc_id)
    extracted = extract_document(workspace, doc_id)
    selected = _select_pages(extracted, pages)
    return {"document": doc, "pages": selected, "state": extracted["state"], "error": extracted.get("error")}


def _select_pages(extracted: dict, pages: list[int] | None) -> list[dict]:
    available = {int(page["page"]): page for page in extracted.get("pages") or []}
    requested = sorted(set(int(page) for page in (available.keys() if pages is None else pages)))
    if not requested:
        raise WorkspaceError("Select at least one document page.")
    missing = [page for page in requested if page not in available]
    if missing:
        raise WorkspaceError(f"Document page {missing[0]} does not exist.")
    return [available[page] for page in requested]


def prompt_content(
    workspace: Workspace,
    source_ref: str,
    pages: list[int] | None = None,
    *,
    max_characters: int | None = None,
) -> dict:
    """Return bounded, prompt-ready document content and source provenance."""
    source_ref = str(source_ref or "")
    if source_ref.startswith("pack:"):
        from . import methodology
        parts = source_ref.split(":", 2)
        if len(parts) != 3:
            raise WorkspaceError("Invalid methodology-pack reference.")
        pack = methodology.get_pack(workspace, parts[1], parts[2])
        text = str(pack.get("markdown") or "")
        selected = _select_pages({"pages": [{"page": 1, "text": text, "characters": len(text), "embedded_images": 0, "image_only": False}]}, pages)
        doc = {"id": source_ref, "sha1": pack["sha1"]}
    elif source_ref.startswith("intake:"):
        from . import intake
        parts = source_ref.split(":")
        if len(parts) != 3:
            raise WorkspaceError("Invalid intake document reference.")
        batch = intake.load_batch(workspace, parts[1])
        item = next((entry for entry in batch["items"] if entry.get("id") == parts[2]), None)
        if item is None:
            raise WorkspaceError("Intake document reference not found.")
        path = intake.staging_path(workspace, batch, item)
        temp_doc = {"id": source_ref, "sha1": item.get("sha1"), "file": path.name}
        # Staging extraction is isolated from imported-document caches.
        suffix = path.suffix.lower()
        if suffix == ".pdf": selected = _select_pages({"pages": _pdf_pages(path)}, pages)
        elif suffix == ".docx": selected = _select_pages({"pages": _docx_page(path)}, pages)
        elif suffix in {".txt", ".md", ".markdown"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            selected = _select_pages({"pages": [{"page": 1, "text": text, "characters": len(text), "embedded_images": 0, "image_only": False}]}, pages)
        else:
            selected = _select_pages({"pages": [{"page": 1, "text": "", "characters": 0, "embedded_images": 1, "image_only": True}]}, pages)
        doc = temp_doc
    else:
        doc = _document(workspace, source_ref)
        selected = _select_pages(extract_document(workspace, doc["id"]), pages)
    prepared = [{**page, "text": page.get("text") or ""} for page in selected]
    content_pages: list[dict] = []
    truncated_pages: list[int] = []
    omitted_pages: list[int] = []
    remaining = None if max_characters is None else max(0, int(max_characters))
    for page in prepared:
        text = str(page.get("text") or "")
        page_number = int(page["page"])
        if not text:
            omitted_pages.append(page_number)
            continue
        if remaining is None or len(text) <= remaining:
            content_pages.append(page)
            if remaining is not None:
                remaining -= len(text)
            continue
        if remaining > 0:
            content_pages.append({**page, "text": text[:remaining]})
            truncated_pages.append(page_number)
            remaining = 0
        else:
            omitted_pages.append(page_number)
    included = [int(page["page"]) for page in content_pages]
    omitted_pages.extend(
        int(page["page"]) for page in prepared
        if int(page["page"]) not in included and int(page["page"]) not in omitted_pages
    )
    return {
        "source_ref": source_ref,
        "document_id": doc["id"],
        "source_sha1": doc.get("sha1"),
        "pages": content_pages,
        "truncated_pages": truncated_pages,
        "omitted_pages": sorted(set(omitted_pages)),
    }


def _fair_character_allocations(lengths: list[int], budget: int) -> list[int]:
    """Share a fixed prompt budget across documents, redistributing spare room."""
    allocations = [0] * len(lengths)
    remaining_lengths = [max(0, int(value)) for value in lengths]
    active = {index for index, value in enumerate(remaining_lengths) if value > 0}
    remaining_budget = max(0, int(budget))
    while active and remaining_budget:
        share = max(1, remaining_budget // len(active))
        progressed = False
        for index in list(active):
            amount = min(share, remaining_lengths[index], remaining_budget)
            allocations[index] += amount
            remaining_lengths[index] -= amount
            remaining_budget -= amount
            progressed = progressed or amount > 0
            if remaining_lengths[index] == 0:
                active.remove(index)
            if remaining_budget == 0:
                break
        if not progressed:
            break
    return allocations


def assistant_document_context(
    workspace: Workspace,
    document_ids: list[str],
    *,
    max_characters: int = ASSISTANT_DOCUMENT_CONTEXT_MAX_CHARACTERS,
) -> dict:
    """Build bounded prompt context for attached documents."""
    unique_ids = list(dict.fromkeys(str(value or "").strip() for value in document_ids))
    unique_ids = [value for value in unique_ids if value]
    docs = [_document(workspace, document_id) for document_id in unique_ids]
    extracted = [extract_document(workspace, doc["id"]) for doc in docs]
    lengths = [sum(len(str(page.get("text") or "")) for page in item.get("pages") or []) for item in extracted]
    allocations = _fair_character_allocations(lengths, max_characters)
    prompt_documents = []
    manifest = []
    for doc, item, allocation in zip(docs, extracted, allocations, strict=True):
        included = prompt_content(
            workspace, doc["id"], pages=None, max_characters=allocation,
        )
        total_pages = [int(page["page"]) for page in item.get("pages") or []]
        included_pages = [int(page["page"]) for page in included["pages"]]
        entry = {
            "document_id": doc["id"], "title": doc.get("title") or doc.get("source") or doc["id"],
            "source": doc.get("source") or "", "source_sha1": included.get("source_sha1"),
            "pages": included["pages"],
        }
        prompt_documents.append(entry)
        omitted = sorted(set(total_pages) - set(included_pages))
        manifest.append({
            "document_id": doc["id"], "title": entry["title"], "total_pages": len(total_pages),
            "included_pages": included_pages, "truncated_pages": included["truncated_pages"],
            "omitted_pages": omitted, "characters_included": sum(
                len(str(page.get("text") or "")) for page in included["pages"]
            ),
            "trimmed": bool(included["truncated_pages"] or omitted),
            "text_state": doc.get("text_state") or item.get("state"),
        })
    return {
        "documents": prompt_documents, "manifest": manifest,
        "trimmed": any(item["trimmed"] for item in manifest),
        "character_budget": max_characters,
    }


def assistant_document_citations(
    workspace: Workspace,
    citations: list[dict],
    context: dict,
) -> list[dict]:
    """Validate model citations against the exact document text it received."""
    included_text = {
        (str(doc["document_id"]), int(page["page"])): str(page.get("text") or "")
        for doc in context.get("documents") or [] for page in doc.get("pages") or []
    }
    anchors = []
    seen = set()
    for citation in citations or []:
        document_id = str(citation.get("document_id") or "")
        try:
            page = int(citation.get("page"))
        except (TypeError, ValueError):
            continue
        allowed_text = included_text.get((document_id, page))
        if allowed_text is None or (document_id, page) in seen:
            continue
        excerpt = str(citation.get("excerpt") or "").strip()
        if not excerpt or excerpt not in allowed_text:
            excerpt = allowed_text[:240].strip()
        doc = _document(workspace, document_id)
        anchors.append(document_anchor(doc, page, excerpt, generated_by="assistant-chat"))
        seen.add((document_id, page))
    return anchors


def append_activity(workspace: Workspace, **fields) -> dict:
    event = {"id": f"AI-{uuid.uuid4().hex[:10].upper()}", "at": utcnow(), **fields}
    handle = telemetry_db.connect(workspace.root)
    handle.execute(
        "INSERT INTO activity_events(at, payload) VALUES(?, ?)",
        (event["at"], telemetry_db.dumps(event)),
    )
    handle.commit()
    return event


def activities(workspace: Workspace, cursor: int = 0, limit: int = 100, document_id: str | None = None) -> dict:
    """One page of the AI activity feed, oldest first.

    Paging used to read and split the whole log to hand back a hundred rows.
    The document filter still applies to the page rather than to the query,
    which is the behaviour the tab was built against.
    """
    cursor = max(0, int(cursor or 0))
    limit = min(250, max(1, int(limit or 100)))
    handle = telemetry_db.connect(workspace.root)
    total = handle.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    items = [
        telemetry_db.loads(row["payload"], {})
        for row in handle.execute(
            "SELECT payload FROM activity_events ORDER BY seq LIMIT ? OFFSET ?",
            (limit, cursor),
        )
    ]
    next_cursor = cursor + len(items) if cursor + len(items) < total else None
    if document_id:
        items = [item for item in items if document_id in (item.get("document_ids") or [])]
    return {"items": items, "next_cursor": next_cursor}


def document_chat(workspace: Workspace, doc_id: str, question: str, pages: list[int] | None,
                  *, run_id: str | None = None, model_adapter=None) -> dict:
    question = str(question or "").strip()
    if not question:
        raise WorkspaceError("A document question is required.")
    doc = _document(workspace, doc_id)
    from . import document_context
    context_mode = "pages" if pages else "search_excerpts"
    context = document_context.get_document_context(
        workspace, doc_id, context_mode, query=question if not pages else None,
        pages=pages, purpose="document_qa", run_id=run_id, stage="document_qa",
        record_activity=False,
    )
    if context_mode == "pages":
        included_pages = context.get("page_items") or []
    else:
        included_pages = [
            {"page": citation["page"], "text": citation["excerpt"]}
            for citation in context.get("citations") or []
        ]
    included = {"pages": included_pages}
    if not included["pages"]:
        raise WorkspaceError("The selected document pages contain no extractable text.")
    system = """[agent:document_qa]\nAnswer only from the included pages. Return one JSON object only, with `answer` as a string and `citations` as an array of objects. Each citation object has `page` as an integer and `excerpt` as a short verbatim string. If the answer is absent, say so. Do not invent facts. Do not return prose outside the JSON object or a Markdown fence."""
    page_text = "\n\n".join(f"--- Page {page['page']} ---\n{page['text']}" for page in included["pages"])
    user = f"Question: {question}\n\nIncluded document pages:\n{page_text}"
    if model_adapter is not None and len(system) + len(user) > 30_000:
        raise WorkspaceError(
            "The document Q&A context exceeds the 30,000-character workflow budget."
        )
    profile = llm.agent_status()
    response_text = ""
    try:
        parsed = None
        attempt_user = user
        last_error = ""
        for attempt in range(1, 3):
            with debug_store.trace_context(
                workspace_id=workspace.id, workspace_root=str(workspace.root), run_id=run_id, stage="document_qa",
                purpose="document_qa", document_ids=[doc_id],
                artifact_refs=[f"document_qa:{doc_id}"],
            ):
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": attempt_user},
                ]
                message = (
                    model_adapter(messages, {
                        "document_ids": [doc_id],
                        "artifact_refs": [f"document_qa:{doc_id}"],
                        "context_metrics": {
                            "worker_kind": "document_qa_execution",
                            "character_budget": 30_000,
                            "total_characters": len(system) + len(attempt_user),
                            "section_characters": {
                                "system": len(system), "question_and_pages": len(attempt_user),
                            },
                            "estimated_tokens": max(1, (len(system) + len(attempt_user)) // 4),
                            "context_reducer_ran": False,
                        },
                        "retry_number": attempt,
                    })
                    if model_adapter is not None
                    else llm.chat(messages, profile="agent")
                )
            response_text = message.get("content") or ""
            try:
                candidate = prompts.parse_json_object(response_text)
                prompts.validate_json_shape(
                    candidate, object_arrays=("citations",), string_fields=("answer",)
                )
                parsed = candidate
                break
            except (ValueError, json.JSONDecodeError) as error:
                last_error = str(error)
                attempt_user = (
                    f"{user}\n\nYour previous response could not be used: {last_error}. "
                    "Return exactly one JSON object with a string `answer` and an array of "
                    "citation objects containing integer `page` and string `excerpt`."
                )
        if parsed is None:
            raise WorkspaceError(
                f"The document answer did not satisfy the structured response contract: {last_error}"
            )
        answer = str(parsed.get("answer") or "")
        allowed_pages = {page["page"]: page for page in included["pages"]}
        anchors = []
        for citation in parsed.get("citations") or []:
            if not isinstance(citation.get("excerpt"), str):
                continue
            try: page = int(citation.get("page"))
            except (TypeError, ValueError): continue
            if page not in allowed_pages: continue
            excerpt = str(citation.get("excerpt") or "").strip()
            if excerpt and excerpt not in allowed_pages[page]["text"]:
                excerpt = ""
            if not excerpt:
                excerpt = allowed_pages[page]["text"][:240].strip()
            anchors.append(document_anchor(doc, page, excerpt, generated_by=run_id or "doc-chat"))
        result = {"answer": answer, "citations": anchors}
        disposition = "generated"
    except Exception:
        disposition = "fallback"
        raise
    finally:
        append_activity(
            workspace, run_id=run_id, stage="document_qa", task=None, purpose="document_qa",
            provider=profile.get("provider"), model=profile.get("model"), vision_used=False,
            prompt_version=hashlib.sha1(system.encode()).hexdigest(), template_versions=[], knowledge_packs=[],
            document_ids=[doc_id], page_ranges=[page["page"] for page in included["pages"]], source_hashes=[doc["sha1"]],
            response_at=utcnow(), response_hash=hashlib.sha1(response_text.encode()).hexdigest() if response_text else None,
            artifact_ref=f"document_qa:{doc_id}", disposition=disposition,
            representation="raw_pages" if pages else "excerpt",
            search_query_hash=hashlib.sha1(question.encode()).hexdigest() if not pages else None,
            characters_supplied=context.get("characters", 0), cache_hit=not bool(pages),
            retrieval_duration_ms=context.get("retrieval_duration_ms"), model_duration_ms=None,
            context_outcome=context.get("outcome"),
        )
    return result
