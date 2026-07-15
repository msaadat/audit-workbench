"""Local document storage, extraction, disclosure, chat, and provenance."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

from . import llm
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
MIN_TEXT_CHARACTERS = 40
ASSISTANT_DOCUMENT_CONTEXT_MAX_CHARACTERS = 80_000
_append_lock = threading.Lock()


class DocPrivacyError(WorkspaceError):
    """Document content was requested without engagement-level permission."""


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


def add_document(workspace: Workspace, filename: str, content: bytes, *, category: str = "other", note: str = "") -> dict:
    source = Path(filename or "document").name
    suffix = Path(source).suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        raise WorkspaceError(f"Unsupported document type '{suffix or '<none>'}'.")
    category = str(category or "other").lower()
    if category not in CATEGORIES:
        raise WorkspaceError("Unknown document category.")
    sha1 = _sha1_bytes(content)
    latest = max(
        (doc for doc in workspace.documents if str(doc.get("source") or "").lower() == source.lower()),
        key=lambda doc: int(doc.get("version") or 1),
        default=None,
    )
    if latest and latest.get("sha1") == sha1:
        return latest
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
        "version": int(latest.get("version") or 1) + 1 if latest else 1,
        "supersedes": latest.get("id") if latest else None,
        "text_state": "image_only" if suffix in IMAGE_SUFFIXES else "pending",
        "note": str(note or ""),
        "created": utcnow(),
        "created_by": "user",
        "agent_run_id": None,
    }
    workspace.documents.append(doc)
    workspace.save()
    extract_document(workspace, doc_id)
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
            pages = [{"page": 1, "text": "", "characters": 0, "embedded_images": 1, "image_only": True}]
        else:
            raise WorkspaceError(f"Unsupported document type '{suffix}'.")
        image_only_count = sum(bool(page["image_only"]) for page in pages)
        text_count = sum(page["characters"] > 0 for page in pages)
        state = "image_only" if pages and image_only_count == len(pages) else "partial" if image_only_count else "extracted"
        if not pages or (not text_count and not image_only_count):
            state = "failed"
        payload = {"document_id": doc_id, "source_sha1": doc["sha1"], "state": state, "pages": pages, "extracted_at": utcnow(), "error": None}
    except Exception as error:
        payload = {"document_id": doc_id, "source_sha1": doc.get("sha1"), "state": "failed", "pages": [], "extracted_at": utcnow(), "error": str(error)}
    write_json_atomic(cache, payload)
    doc.update(pages=len(payload["pages"]) or None, text_state=payload["state"])
    workspace.save()
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


def _append_jsonl(path: Path, event: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
    return event


def _mask_pii(text: str) -> str:
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email masked]", text)
    return re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[number masked]", text)


def disclosable_content(workspace: Workspace, source_ref: str, purpose: str, run_id: str | None = None,
                         pages: list[int] | None = None, *, mask_pii: bool = False,
                         max_characters: int | None = None) -> dict:
    """The only function allowed to return prompt-ready document content."""
    if not workspace.settings.get("doc_llm_optin"):
        raise DocPrivacyError("Document AI is off for this engagement. Enable it before disclosing document content.")
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
    prepared = [
        {**page, "text": _mask_pii(page.get("text") or "") if mask_pii else page.get("text") or ""}
        for page in selected
    ]
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
    event = {
        "id": f"DISC-{uuid.uuid4().hex[:10].upper()}", "at": utcnow(), "source_ref": source_ref,
        "document_id": doc["id"], "source_sha1": doc.get("sha1"), "pages": included,
        "purpose": str(purpose or "document_use"), "run_id": run_id, "pii_masked": bool(mask_pii),
        "characters_disclosed": sum(len(str(page.get("text") or "")) for page in content_pages),
        "truncated_pages": truncated_pages, "omitted_pages": sorted(set(omitted_pages)),
    }
    disclosure = _append_jsonl(_documents_dir(workspace) / "disclosures.jsonl", event) if content_pages else None
    return {
        "source_ref": source_ref, "source_sha1": doc.get("sha1"), "pages": content_pages,
        "disclosure": disclosure, "truncated_pages": truncated_pages,
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
    mask_pii: bool = False,
    max_characters: int = ASSISTANT_DOCUMENT_CONTEXT_MAX_CHARACTERS,
) -> dict:
    """Build prompt-ready, disclosure-logged context for attached documents."""
    unique_ids = list(dict.fromkeys(str(value or "").strip() for value in document_ids))
    unique_ids = [value for value in unique_ids if value]
    docs = [_document(workspace, document_id) for document_id in unique_ids]
    extracted = [extract_document(workspace, doc["id"]) for doc in docs]
    lengths = [sum(len(str(page.get("text") or "")) for page in item.get("pages") or []) for item in extracted]
    allocations = _fair_character_allocations(lengths, max_characters)
    prompt_documents = []
    manifest = []
    disclosure_events = []
    for doc, item, allocation in zip(docs, extracted, allocations, strict=True):
        disclosed = disclosable_content(
            workspace, doc["id"], "assistant_chat", pages=None,
            mask_pii=mask_pii, max_characters=allocation,
        )
        if disclosed["disclosure"]:
            disclosure_events.append(disclosed["disclosure"])
        total_pages = [int(page["page"]) for page in item.get("pages") or []]
        included_pages = [int(page["page"]) for page in disclosed["pages"]]
        entry = {
            "document_id": doc["id"], "title": doc.get("title") or doc.get("source") or doc["id"],
            "source": doc.get("source") or "", "pages": disclosed["pages"],
        }
        prompt_documents.append(entry)
        omitted = sorted(set(total_pages) - set(included_pages))
        manifest.append({
            "document_id": doc["id"], "title": entry["title"], "total_pages": len(total_pages),
            "included_pages": included_pages, "truncated_pages": disclosed["truncated_pages"],
            "omitted_pages": omitted, "characters_disclosed": sum(
                len(str(page.get("text") or "")) for page in disclosed["pages"]
            ),
            "trimmed": bool(disclosed["truncated_pages"] or omitted),
            "text_state": doc.get("text_state") or item.get("state"),
        })
    return {
        "documents": prompt_documents, "manifest": manifest, "disclosures": disclosure_events,
        "trimmed": any(item["trimmed"] for item in manifest),
        "character_budget": max_characters,
    }


def assistant_document_citations(
    workspace: Workspace,
    citations: list[dict],
    context: dict,
    *,
    mask_pii: bool = False,
) -> list[dict]:
    """Validate model citations against the exact document text it received."""
    disclosed = {
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
        allowed_text = disclosed.get((document_id, page))
        if allowed_text is None or (document_id, page) in seen:
            continue
        excerpt = str(citation.get("excerpt") or "").strip()
        if not excerpt or excerpt not in allowed_text:
            excerpt = allowed_text[:240].strip()
        doc = _document(workspace, document_id)
        anchor_excerpt = excerpt
        if mask_pii:
            original = preview(workspace, document_id, [page])["pages"][0].get("text") or ""
            if excerpt not in original:
                anchor_excerpt = str(original)[:240].strip()
        anchors.append(document_anchor(doc, page, anchor_excerpt, generated_by="assistant-chat"))
        seen.add((document_id, page))
    return anchors


def append_activity(workspace: Workspace, **fields) -> dict:
    event = {"id": f"AI-{uuid.uuid4().hex[:10].upper()}", "at": utcnow(), **fields}
    return _append_jsonl(workspace.root / "AIActivity" / "events.jsonl", event)


def list_jsonl(path: Path, cursor: int = 0, limit: int = 100) -> dict:
    cursor = max(0, int(cursor or 0)); limit = min(250, max(1, int(limit or 100)))
    if not path.exists():
        return {"items": [], "next_cursor": None}
    lines = path.read_text(encoding="utf-8").splitlines()
    items = [json.loads(line) for line in lines[cursor:cursor + limit] if line.strip()]
    next_cursor = cursor + len(items) if cursor + len(items) < len(lines) else None
    return {"items": items, "next_cursor": next_cursor}


def disclosures(workspace: Workspace, cursor: int = 0, limit: int = 100) -> dict:
    return list_jsonl(_documents_dir(workspace) / "disclosures.jsonl", cursor, limit)


def activities(workspace: Workspace, cursor: int = 0, limit: int = 100, document_id: str | None = None) -> dict:
    result = list_jsonl(workspace.root / "AIActivity" / "events.jsonl", cursor, limit)
    if document_id:
        result["items"] = [item for item in result["items"] if document_id in (item.get("document_ids") or [])]
    return result


def versions(workspace: Workspace, doc_id: str) -> list[dict]:
    doc = _document(workspace, doc_id)
    chain = {doc["id"]}
    changed = True
    while changed:
        changed = False
        for candidate in workspace.documents:
            if candidate.get("id") in chain or candidate.get("supersedes") in chain:
                before = len(chain); chain.add(candidate["id"]); changed = changed or len(chain) != before
            if candidate.get("id") in chain and candidate.get("supersedes"):
                before = len(chain); chain.add(candidate["supersedes"]); changed = changed or len(chain) != before
    return sorted((item for item in workspace.documents if item.get("id") in chain), key=lambda item: int(item.get("version") or 1), reverse=True)


def document_chat(workspace: Workspace, doc_id: str, question: str, pages: list[int] | None,
                  *, mask_pii: bool = False, run_id: str | None = None) -> dict:
    question = str(question or "").strip()
    if not question:
        raise WorkspaceError("A document question is required.")
    doc = _document(workspace, doc_id)
    disclosed = disclosable_content(workspace, doc_id, "document_qa", run_id, pages, mask_pii=mask_pii)
    if not disclosed["pages"]:
        raise WorkspaceError("The selected document pages contain no extractable text.")
    system = """[agent:document_qa]\nAnswer only from the disclosed pages. Return JSON with answer and citations. Each citation has page and a short verbatim excerpt. If the answer is absent, say so. Do not invent facts."""
    page_text = "\n\n".join(f"--- Page {page['page']} ---\n{page['text']}" for page in disclosed["pages"])
    user = f"Question: {question}\n\nDisclosed document pages:\n{page_text}"
    profile = llm.agent_status()
    response_text = ""
    try:
        message = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], profile="agent")
        response_text = message.get("content") or ""
        parsed = prompts.parse_json_object(response_text)
        answer = str(parsed.get("answer") or "")
        allowed_pages = {page["page"]: page for page in disclosed["pages"]}
        original_pages = {
            page["page"]: page for page in preview(workspace, doc_id, list(allowed_pages))["pages"]
        } if mask_pii else allowed_pages
        anchors = []
        for citation in parsed.get("citations") or []:
            try: page = int(citation.get("page"))
            except (TypeError, ValueError): continue
            if page not in allowed_pages: continue
            excerpt = str(citation.get("excerpt") or "").strip()
            if excerpt and excerpt not in allowed_pages[page]["text"]:
                excerpt = ""
            if not excerpt:
                excerpt = allowed_pages[page]["text"][:240].strip()
            anchor_excerpt = excerpt
            if mask_pii and excerpt not in original_pages[page]["text"]:
                anchor_excerpt = original_pages[page]["text"][:240].strip()
            anchors.append(document_anchor(doc, page, anchor_excerpt, generated_by=run_id or "doc-chat"))
        result = {"answer": answer, "citations": anchors, "disclosure": disclosed["disclosure"]}
        disposition = "generated"
    except Exception:
        disposition = "fallback"
        raise
    finally:
        append_activity(
            workspace, run_id=run_id, stage="document_qa", task=None, purpose="document_qa",
            provider=profile.get("provider"), model=profile.get("model"), vision_used=False,
            prompt_version=hashlib.sha1(system.encode()).hexdigest(), template_versions=[], knowledge_packs=[],
            document_ids=[doc_id], page_ranges=disclosed["disclosure"]["pages"], source_hashes=[doc["sha1"]],
            response_at=utcnow(), response_hash=hashlib.sha1(response_text.encode()).hexdigest() if response_text else None,
            artifact_ref=f"document_qa:{doc_id}", disposition=disposition,
        )
    return result
