"""Rebuildable local hybrid indexes for extracted engagement documents."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from array import array
from collections import Counter
from pathlib import Path

from . import embedding
from .evidence import document_anchor
from .workspaces import Workspace, WorkspaceError, write_json_atomic

SEARCH_CHUNKER_VERSION = "page-paragraph-v1"
SEARCH_CHUNK_CHARACTERS = 1_600
SEARCH_OVERLAP_CHARACTERS = 200
TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.I)


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# One daemon worker serves every workspace in this local process. Keeping both
# document and embedding concurrency at one avoids a large upload monopolising
# CPU or memory while the auditor continues working elsewhere in the app.
INDEX_DOCUMENT_PACE_SECONDS = _env_float("DOCUMENT_INDEX_PACE_SECONDS", 0.15)
INDEX_EMBEDDING_BATCH_SIZE = _env_int("DOCUMENT_INDEX_EMBEDDING_BATCH_SIZE", 16)
INDEX_EMBEDDING_BATCH_PAUSE_SECONDS = _env_float("DOCUMENT_INDEX_BATCH_PAUSE_SECONDS", 0.01)
_index_queue: queue.Queue[tuple[Workspace, dict]] = queue.Queue()
_queue_lock = threading.RLock()
_queued_documents: set[tuple[str, str]] = set()
_active_job_ids: set[str] = set()
_worker: threading.Thread | None = None


def _normalized(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    return [float(value) / norm for value in vector] if norm else [0.0 for value in vector]


def _root(workspace: Workspace, document_id: str) -> Path:
    return workspace.root / "Documents" / ".search" / document_id


def _manifest_path(workspace: Workspace, document_id: str) -> Path:
    return _root(workspace, document_id) / "manifest.json"


def _chunks_path(workspace: Workspace, document_id: str) -> Path:
    return _root(workspace, document_id) / "chunks.jsonl"


def _vectors_path(workspace: Workspace, document_id: str) -> Path:
    return _root(workspace, document_id) / "vectors.f32"


def _jobs_root(workspace: Workspace) -> Path:
    return workspace.root / "Documents" / ".search" / "jobs"


def _job_path(workspace: Workspace, job_id: str) -> Path:
    return _jobs_root(workspace) / f"{job_id}.json"


def _queue_key(workspace: Workspace, document_id: str) -> tuple[str, str]:
    return str(workspace.root.resolve()), document_id


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(str(text or "").casefold())


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _manifest_identity(text_sha1: str, analysis_content_sha1: str = "") -> dict:
    backend = embedding.get_backend()
    return {
        "extracted_text_sha1": text_sha1,
        "embedding_model_sha256": getattr(backend, "model_sha256", None),
        "embedding_model": getattr(backend, "model_id", None),
        "embedding_dimension": getattr(backend, "dimension", 0) if backend else 0,
        "tokenizer_version": getattr(backend, "tokenizer_version", "audit-tokenizer-v1"),
        "search_chunker_version": SEARCH_CHUNKER_VERSION,
        "analysis_content_sha1": analysis_content_sha1,
    }


def index_identity(text_sha1: str, analysis_content_sha1: str = "") -> str:
    return hashlib.sha1(
        json.dumps(
            _manifest_identity(text_sha1, analysis_content_sha1),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def search_chunks(extracted: dict, max_characters: int = SEARCH_CHUNK_CHARACTERS,
                  overlap: int = SEARCH_OVERLAP_CHARACTERS) -> list[dict]:
    """Create page-contained chunks whose offsets resolve to exact cache text."""
    output: list[dict] = []
    size = max(128, int(max_characters)); overlap = min(size // 3, max(0, int(overlap)))
    for page in extracted.get("pages") or []:
        text = str(page.get("text") or "")
        if not text:
            continue
        start, page_no = 0, int(page["page"])
        while start < len(text):
            hard_end = min(len(text), start + size)
            end = hard_end
            if hard_end < len(text):
                boundaries = [text.rfind("\n\n", start, hard_end), text.rfind(". ", start, hard_end)]
                boundary = max(boundaries)
                if boundary > start + size // 2:
                    end = boundary + (2 if text[boundary:boundary + 2] in {"\n\n", ". "} else 1)
            exact = text[start:end]
            terms = _tokens(exact)
            output.append({"chunk_id": f"DC-{len(output)+1:06d}", "page": page_no,
                           "start_character": start, "end_character": end,
                           "text_sha1": hashlib.sha1(exact.encode()).hexdigest(),
                           "token_count": len(terms), "term_frequencies": dict(Counter(terms)),
                           "origin": str(page.get("origin") or "extracted_text"),
                           "analysis_id": page.get("analysis_id")})
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return output


def _resolve(extracted: dict, chunk: dict) -> str | None:
    page = next(
        (
            value
            for value in extracted.get("pages") or []
            if int(value.get("page") or 0) == int(chunk["page"])
            and str(value.get("origin") or "extracted_text")
            == str(chunk.get("origin") or "extracted_text")
        ),
        None,
    )
    if page is None:
        return None
    text = str(page.get("text") or "")
    excerpt = text[int(chunk["start_character"]):int(chunk["end_character"])]
    return excerpt if hashlib.sha1(excerpt.encode()).hexdigest() == chunk.get("text_sha1") else None


def _index_payload(extracted: dict, analysis: dict | None) -> dict:
    pages = [
        {**dict(page), "origin": "extracted_text", "analysis_id": None}
        for page in extracted.get("pages") or []
        if str(page.get("text") or "")
    ]
    derived = str((analysis or {}).get("derived_text_markdown") or "").strip()
    if derived:
        citation_pages = [
            int(item.get("page") or 0)
            for item in (analysis or {}).get("citations") or []
            if int(item.get("page") or 0) > 0
        ]
        pages.append(
            {
                "page": min(citation_pages) if citation_pages else 1,
                "text": derived,
                "characters": len(derived),
                "origin": "vision_transcript",
                "analysis_id": (analysis or {}).get("id"),
            }
        )
    return {**dict(extracted), "pages": pages}


def _current_analysis(
    workspace: Workspace, document: dict
) -> dict | None:
    from . import document_analysis

    detail = document_analysis.load_analysis(
        workspace, str(document["id"]), document=document
    )
    if detail["status"]["analysis_validity_state"] != "current":
        return None
    effective = detail.get("effective")
    return dict(effective) if isinstance(effective, dict) else None


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def _read_chunks(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def build_index(workspace: Workspace, document_id: str) -> dict:
    """Build one index locally. This function never invokes the chat model."""
    from . import document_analysis, documents
    document = next((item for item in workspace.documents if item.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    with document_analysis.document_lock(workspace, document_id):
        analysis = _current_analysis(workspace, document)
        derived_text = str(
            (analysis or {}).get("derived_text_markdown") or ""
        ).strip()
        if document.get("text_state") == "image_only" and not derived_text:
            manifest = {"schema_version": 1, "document_id": document_id,
                        "source_sha1": document.get("sha1"), "state": "unsupported",
                        "error": "No extractable text; OCR is not available."}
            write_json_atomic(_manifest_path(workspace, document_id), manifest)
            document_analysis.update_status(workspace, document_id, search_index_state="unsupported")
            return manifest
        document_analysis.update_status(workspace, document_id, search_index_state="indexing")
        extracted = documents.extract_document(workspace, document_id)
        if extracted.get("state") == "failed":
            raise WorkspaceError(extracted.get("error") or "Document extraction failed.")
        text_sha = document_analysis.extracted_text_sha(extracted)
        analysis_content_sha1 = str(
            (analysis or {}).get("content_sha1") or ""
        )
        indexed_payload = _index_payload(extracted, analysis)
        chunks = search_chunks(indexed_payload)
        if not chunks:
            manifest = {"schema_version": 1, "document_id": document_id,
                        "source_sha1": document.get("sha1"), **_manifest_identity(text_sha, analysis_content_sha1),
                        "identity": index_identity(text_sha, analysis_content_sha1), "state": "unsupported",
                        "chunk_count": 0, "error": "The document contains no searchable text."}
            write_json_atomic(_manifest_path(workspace, document_id), manifest)
            document_analysis.update_status(workspace, document_id, search_index_state="unsupported")
            return manifest
        texts = [_resolve(indexed_payload, chunk) or "" for chunk in chunks]
        backend = embedding.get_backend()
        try:
            vectors = []
            if backend:
                for start in range(0, len(texts), INDEX_EMBEDDING_BATCH_SIZE):
                    batch = texts[start:start + INDEX_EMBEDDING_BATCH_SIZE]
                    vectors.extend(_normalized(vector) for vector in backend.encode(batch))
                    if start + INDEX_EMBEDDING_BATCH_SIZE < len(texts) and INDEX_EMBEDDING_BATCH_PAUSE_SECONDS:
                        time.sleep(INDEX_EMBEDDING_BATCH_PAUSE_SECONDS)
        except Exception as error:
            vectors = []
            vector_error = str(error)
        else:
            vector_error = None
        root = _root(workspace, document_id); root.mkdir(parents=True, exist_ok=True)
        _write_jsonl(_chunks_path(workspace, document_id), chunks)
        vector_tmp = _vectors_path(workspace, document_id).with_name(f".vectors.{uuid.uuid4().hex}.tmp")
        with vector_tmp.open("wb") as handle:
            for vector in vectors:
                array("f", vector).tofile(handle)
        vector_tmp.replace(_vectors_path(workspace, document_id))
        manifest = {"schema_version": 1, "document_id": document_id,
                    "source_sha1": document.get("sha1"), **_manifest_identity(text_sha, analysis_content_sha1),
                    "identity": index_identity(text_sha, analysis_content_sha1), "state": "ready",
                    "chunk_count": len(chunks), "vector_count": len(vectors),
                    "created_at": documents.utcnow(), "error": vector_error,
                    "lexical_only": not bool(vectors)}
        write_json_atomic(_manifest_path(workspace, document_id), manifest)
        document_analysis.update_status(workspace, document_id, search_index_state="ready")
        return manifest


def manifest_state(workspace: Workspace, document_id: str) -> dict:
    from . import document_analysis, documents
    document = next((item for item in workspace.documents if item.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    manifest = _read_json(_manifest_path(workspace, document_id))
    if not manifest:
        analysis = _current_analysis(workspace, document)
        return {
            "document_id": document_id,
            "state": (
                "unsupported"
                if document.get("text_state") == "image_only"
                and not str(
                    (analysis or {}).get("derived_text_markdown") or ""
                ).strip()
                else "pending"
            ),
        }
    extracted = documents.extract_document(workspace, document_id)
    analysis = _current_analysis(workspace, document)
    expected = index_identity(
        document_analysis.extracted_text_sha(extracted),
        str((analysis or {}).get("content_sha1") or ""),
    )
    state = manifest.get("state") or "pending"
    if manifest.get("identity") != expected or manifest.get("source_sha1") != document.get("sha1"):
        state = "stale"
    return {**manifest, "state": state}


def _load_vectors(path: Path, count: int, dimension: int) -> list[list[float]]:
    if not count or not dimension or not path.exists():
        return []
    values = array("f")
    try:
        with path.open("rb") as handle:
            values.fromfile(handle, count * dimension)
    except (OSError, EOFError):
        return []
    return [list(values[index:index + dimension]) for index in range(0, len(values), dimension)]


def _bm25(query_terms: list[str], term_frequencies: dict[str, int], length: int,
          document_frequencies: Counter, corpus_size: int, average_length: float) -> float:
    score, k1, b = 0.0, 1.5, .75
    for term in set(query_terms):
        tf = int(term_frequencies.get(term) or 0)
        if not tf:
            continue
        df = document_frequencies[term]
        idf = math.log(1 + (corpus_size - df + .5) / (df + .5))
        score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / max(1.0, average_length)))
    return score


def search(workspace: Workspace, query: str, *, document_ids: list[str] | None = None,
           top_k: int = 6, max_characters: int = 8_000, build_missing: bool = True) -> dict:
    recover_indexing(workspace)
    started = time.monotonic(); query = str(query or "").strip()
    if not query:
        raise WorkspaceError("Enter document text to search for.")
    scope = list(dict.fromkeys(str(value) for value in (document_ids or [doc["id"] for doc in workspace.documents])))
    known = {doc["id"]: doc for doc in workspace.documents}
    missing = [value for value in scope if value not in known]
    if missing:
        raise WorkspaceError(f"Document '{missing[0]}' not found.")
    indexes = []
    for document_id in scope:
        state = manifest_state(workspace, document_id)
        if (build_missing and state["state"] in {"pending", "stale", "failed"}
                and not is_indexing(workspace, document_id)):
            try:
                build_index(workspace, document_id); state = manifest_state(workspace, document_id)
            except Exception:
                state = {**state, "state": "failed"}
        if state["state"] != "ready":
            continue
        chunks = _read_chunks(_chunks_path(workspace, document_id))
        if chunks:
            indexes.append((document_id, state, chunks))
    all_chunks = [chunk for _, _, chunks in indexes for chunk in chunks]
    query_terms = _tokens(query)
    document_frequencies = Counter(term for chunk in all_chunks for term in set((chunk.get("term_frequencies") or {}).keys()))
    average_length = sum(int(chunk.get("token_count") or 0) for chunk in all_chunks) / max(1, len(all_chunks))
    backend = embedding.get_backend(); query_vector = _normalized(backend.encode([query])[0]) if backend else None
    candidates = []
    from . import document_analysis, documents
    for document_id, manifest, chunks in indexes:
        extracted = documents.extract_document(workspace, document_id)
        analysis = _current_analysis(workspace, known[document_id])
        indexed_payload = _index_payload(extracted, analysis)
        dimension = int(manifest.get("embedding_dimension") or 0)
        vectors = _load_vectors(_vectors_path(workspace, document_id), int(manifest.get("vector_count") or 0), dimension)
        for index, chunk in enumerate(chunks):
            excerpt = _resolve(indexed_payload, chunk)
            if excerpt is None:
                continue
            lexical = _bm25(query_terms, chunk.get("term_frequencies") or {}, int(chunk.get("token_count") or 0),
                            document_frequencies, len(all_chunks), average_length)
            vector_score = sum(a * b for a, b in zip(query_vector, vectors[index])) if query_vector and index < len(vectors) else 0.0
            folded = excerpt.casefold(); exact = 1.5 if query.casefold() in folded else 0.0
            identifiers = [term for term in query_terms if any(char.isdigit() for char in term) or len(term) >= 8]
            identifier_boost = .35 * sum(term in folded for term in identifiers)
            score = lexical + max(0.0, vector_score) * .75 + exact + identifier_boost
            if score > 0:
                candidates.append({"document_id": document_id, "page": int(chunk["page"]),
                                   "start_character": int(chunk["start_character"]), "end_character": int(chunk["end_character"]),
                                   "excerpt": excerpt, "score": score, "lexical_score": lexical,
                                   "vector_score": vector_score,
                                   "origin": str(chunk.get("origin") or "extracted_text"),
                                   "analysis_id": chunk.get("analysis_id")})
    candidates.sort(key=lambda value: (-value["score"], value["document_id"], value["page"], value["start_character"]))
    selected, used, total = [], set(), 0
    for result in candidates:
        overlap_key = (
            result["document_id"],
            result["page"],
            result["origin"],
        )
        ranges = [
            item
            for item in selected
            if (
                item["document_id"],
                item["page"],
                item["origin"],
            )
            == overlap_key
        ]
        if any(result["start_character"] < item["end_character"] and item["start_character"] < result["end_character"] for item in ranges):
            continue
        remaining = max_characters - total
        if remaining <= 0 or len(selected) >= min(20, max(1, int(top_k))):
            break
        if len(result["excerpt"]) > remaining:
            result["excerpt"] = result["excerpt"][:remaining]
            result["end_character"] = result["start_character"] + remaining
        document = known[result["document_id"]]
        anchor = document_anchor(document, result["page"], result["excerpt"], generated_by="document-search")
        if result["origin"] == "vision_transcript":
            anchor.update(
                evidence_kind="model_generated_transcription",
                analysis_id=result.get("analysis_id"),
                auditor_confirmed=False,
            )
        result.update(title=document.get("title") or document.get("source"), citation_id=anchor["id"], citation=anchor)
        selected.append(result); total += len(result["excerpt"]); used.add(result["document_id"])
    return {"results": selected, "trimmed": len(selected) < len(candidates),
            "query_scope": scope, "indexed_documents": len(indexes),
            "characters": total, "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "embedding": embedding.status()}


def _ensure_worker() -> None:
    global _worker
    with _queue_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_index_worker, name="document-search-indexer", daemon=True)
        _worker.start()


def _index_worker() -> None:
    from . import document_analysis
    from .documents import utcnow

    while True:
        workspace, job = _index_queue.get()
        job_id = str(job["id"])
        try:
            job.update(state="indexing", started_at=job.get("started_at") or utcnow())
            write_json_atomic(_job_path(workspace, job_id), job)
            for position, document_id in enumerate(job["document_ids"]):
                job["active_document_id"] = document_id
                write_json_atomic(_job_path(workspace, job_id), job)
                current_workspace = workspace
                try:
                    # Reload the durable definition for every item. API
                    # requests use separate Workspace objects, so this keeps a
                    # long queue from indexing a source that was replaced or
                    # deleted after the upload response returned.
                    current_workspace = Workspace(workspace.root)
                    manifest = build_index(current_workspace, document_id)
                    job["results"].append({
                        "document_id": document_id,
                        "state": manifest.get("state"),
                        "chunk_count": int(manifest.get("chunk_count") or 0),
                    })
                except Exception as error:
                    job["errors"].append({"document_id": document_id, "error": str(error)})
                    try:
                        current_workspace = Workspace(workspace.root)
                        if any(str(item.get("id")) == document_id for item in current_workspace.documents):
                            document_analysis.update_status(current_workspace, document_id, search_index_state="failed")
                    except Exception:
                        pass
                finally:
                    with _queue_lock:
                        _queued_documents.discard(_queue_key(workspace, document_id))
                    job["active_document_id"] = None
                    write_json_atomic(_job_path(workspace, job_id), job)
                if position + 1 < len(job["document_ids"]) and INDEX_DOCUMENT_PACE_SECONDS:
                    time.sleep(INDEX_DOCUMENT_PACE_SECONDS)
            job.update(
                state="completed_with_issues" if job["errors"] else "completed",
                completed_at=utcnow(), active_document_id=None,
            )
            write_json_atomic(_job_path(workspace, job_id), job)
        except Exception as error:
            active_document_id = job.get("active_document_id")
            job["errors"].append({
                "document_id": active_document_id,
                "error": f"Index worker error: {error}",
            })
            job.update(state="completed_with_issues", completed_at=utcnow(), active_document_id=None)
            try:
                write_json_atomic(_job_path(workspace, job_id), job)
            except Exception:
                pass
        finally:
            with _queue_lock:
                _active_job_ids.discard(job_id)
                for document_id in job.get("document_ids") or []:
                    _queued_documents.discard(_queue_key(workspace, str(document_id)))
            _index_queue.task_done()


def enqueue_indexing(workspace: Workspace, document_ids: list[str], *, reason: str = "upload") -> dict:
    """Queue local indexing and return immediately.

    Jobs are persisted before they are handed to the single paced worker. A
    repeated enqueue while a document is already queued is safely coalesced.
    """
    from . import document_analysis
    from .documents import utcnow

    requested = list(dict.fromkeys(str(value) for value in document_ids if str(value)))
    known = {str(document.get("id")) for document in workspace.documents}
    missing = [document_id for document_id in requested if document_id not in known]
    if missing:
        raise WorkspaceError(f"Document '{missing[0]}' not found.")
    accepted = []
    with _queue_lock:
        for document_id in requested:
            key = _queue_key(workspace, document_id)
            if key not in _queued_documents:
                _queued_documents.add(key)
                accepted.append(document_id)
        job = {
            "id": f"DI-{uuid.uuid4().hex[:12].upper()}",
            "document_ids": accepted,
            "requested_document_ids": requested,
            "coalesced_document_ids": [value for value in requested if value not in accepted],
            "reason": reason,
            "state": "queued" if accepted else "completed",
            "created_at": utcnow(),
            "started_at": None,
            "completed_at": None if accepted else utcnow(),
            "active_document_id": None,
            "results": [],
            "errors": [],
        }
        write_json_atomic(_job_path(workspace, job["id"]), job)
        if accepted:
            _active_job_ids.add(job["id"])
            for document_id in accepted:
                document_analysis.update_status(workspace, document_id, search_index_state="indexing")
            _index_queue.put((workspace, job))
    if accepted:
        _ensure_worker()
    # The worker owns and mutates its copy while API serialization reads this
    # stable snapshot.
    return json.loads(json.dumps(job))


def reindex_job(workspace: Workspace, document_ids: list[str]) -> dict:
    """Compatibility entry point for an explicit, now-background reindex."""
    return enqueue_indexing(workspace, document_ids, reason="manual_reindex")


def is_indexing(workspace: Workspace, document_id: str) -> bool:
    with _queue_lock:
        return _queue_key(workspace, document_id) in _queued_documents


def recover_indexing(workspace: Workspace) -> None:
    """Resume unfinished persisted jobs after an application restart."""
    root = _jobs_root(workspace)
    if not root.exists():
        return
    remaining: list[str] = []
    for path in sorted(root.glob("*.json")):
        job = _read_json(path)
        if job.get("state") not in {"queued", "indexing"}:
            continue
        with _queue_lock:
            if str(job.get("id")) in _active_job_ids:
                continue
        finished = {
            str(item.get("document_id"))
            for item in [*(job.get("results") or []), *(job.get("errors") or [])]
        }
        remaining.extend(
            str(value) for value in (job.get("document_ids") or [])
            if str(value) not in finished
        )
        job.update(state="interrupted", completed_at=None, active_document_id=None)
        write_json_atomic(path, job)
    if remaining:
        enqueue_indexing(workspace, remaining, reason="restart_recovery")


def queue_status(workspace: Workspace) -> dict:
    recover_indexing(workspace)
    jobs = []
    root = _jobs_root(workspace)
    if root.exists():
        for path in root.glob("*.json"):
            job = _read_json(path)
            if job.get("state") in {"queued", "indexing"}:
                jobs.append(job)
    jobs.sort(key=lambda value: str(value.get("created_at") or ""))
    total = sum(len(job.get("document_ids") or []) for job in jobs)
    completed = sum(len(job.get("results") or []) + len(job.get("errors") or []) for job in jobs)
    active = next((job.get("active_document_id") for job in jobs if job.get("active_document_id")), None)
    return {
        "state": "indexing" if jobs else "idle",
        "job_count": len(jobs),
        "total_documents": total,
        "completed_documents": completed,
        "remaining_documents": max(0, total - completed),
        "active_document_id": active,
        "pace_seconds": INDEX_DOCUMENT_PACE_SECONDS,
    }


def remove_sidecars(workspace: Workspace, document_id: str) -> None:
    import shutil
    with _queue_lock:
        # Queue items are immutable, so the worker will still encounter this
        # ID, notice that the source is gone, and record an issue. Removing the
        # key now prevents a deleted document from keeping the UI in indexing.
        _queued_documents.discard(_queue_key(workspace, document_id))
    shutil.rmtree(_root(workspace, document_id), ignore_errors=True)


def invalidate(workspace: Workspace, document_id: str) -> None:
    from . import document_analysis
    manifest = _read_json(_manifest_path(workspace, document_id))
    if manifest:
        manifest["state"] = "stale"
        write_json_atomic(_manifest_path(workspace, document_id), manifest)
    document_analysis.update_status(workspace, document_id, search_index_state="pending")
