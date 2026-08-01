"""Persistent, revisioned document analysis sidecars and status catalog."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from pathlib import Path

from . import embedding
from .workspaces import Workspace, WorkspaceError, write_json_atomic

ANALYSIS_SCHEMA_VERSION = "3"
ANALYSIS_PROMPT_VERSION = "document-analysis-v4-voucher-fields"
STATUS_SCHEMA_VERSION = 1
ANALYSIS_CHUNK_CHARACTERS = 24_000


class AnalysisConflict(WorkspaceError):
    """A revision-checked analysis mutation used stale client state."""

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_catalog_lock = threading.RLock()


def document_lock(workspace: Workspace, document_id: str) -> threading.RLock:
    key = f"{workspace.root.resolve()}:{document_id}"
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _root(workspace: Workspace, document_id: str) -> Path:
    return workspace.root / "Documents" / ".analysis" / document_id


def _index_path(workspace: Workspace, document_id: str) -> Path:
    return _root(workspace, document_id) / "index.json"


def _review_path(workspace: Workspace, document_id: str) -> Path:
    return _root(workspace, document_id) / "review.json"


def _generated_path(workspace: Workspace, document_id: str, analysis_id: str) -> Path:
    return _root(workspace, document_id) / "generated" / f"{analysis_id}.json"


def status_path(workspace: Workspace) -> Path:
    return workspace.root / "Documents" / ".status.json"


def default_status() -> dict:
    return {
        "analysis_run_state": "idle", "analysis_coverage_state": "none",
        "analysis_validity_state": None, "analysis_updated_at": None,
        "analysis_review_state": "not_applicable", "has_analysis_overrides": False,
        "candidate_analysis_id": None, "analysis_resumable_run_id": None,
        "search_index_state": "pending",
        "analysis_vision_used": False,
    }


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def _empty_index(document_id: str) -> dict:
    return {"schema_version": 1, "document_id": document_id, "revision": 0,
            "active_analysis_id": None, "candidate_analysis_id": None,
            "run_state": "idle", "resumable_run_id": None, "updated_at": None}


def _empty_review(document_id: str) -> dict:
    return {"schema_version": 1, "document_id": document_id, "revision": 0,
            "summary_override": None, "audit_notes_override": None,
            "review_state": "not_applicable", "reviewed_at": None,
            "updated_at": None, "decisions": []}


def load_index(workspace: Workspace, document_id: str) -> dict:
    return _read_json(_index_path(workspace, document_id), _empty_index(document_id))


def load_review(workspace: Workspace, document_id: str) -> dict:
    return _read_json(_review_path(workspace, document_id), _empty_review(document_id))


def extracted_text_sha(extracted: dict) -> str:
    existing = str(extracted.get("extracted_text_sha1") or "")
    if existing:
        return existing
    digest = hashlib.sha1()
    for page in extracted.get("pages") or []:
        digest.update(f"\n\fPAGE:{int(page.get('page') or 0)}\n".encode())
        digest.update(str(page.get("text") or "").encode("utf-8"))
    return digest.hexdigest()


def cache_identity(source_sha1: str, text_sha1: str,
                   schema_version: str = ANALYSIS_SCHEMA_VERSION,
                   prompt_version: str = ANALYSIS_PROMPT_VERSION,
                   prepared_media_set_hash: str = "") -> str:
    return hashlib.sha1(
        "\0".join(
            (
                source_sha1,
                text_sha1,
                schema_version,
                prompt_version,
                prepared_media_set_hash,
            )
        ).encode()
    ).hexdigest()


def _load_generated(workspace: Workspace, document_id: str, analysis_id: str | None) -> dict | None:
    if not analysis_id:
        return None
    value = _read_json(_generated_path(workspace, document_id, analysis_id), {})
    return value or None


def analysis_content_sha1(payload: dict) -> str:
    """Stable identity of one generated analysis' human-facing content.

    Covers exactly the fields a reduction produces, so a workflow reconciler can
    prove that the artifact on disk is the one its accepted proposal describes
    without comparing volatile identifiers or timestamps.
    """
    material = {
        "summary_markdown": str(payload.get("summary_markdown") or "").strip(),
        "audit_notes_markdown": str(payload.get("audit_notes_markdown") or "").strip(),
        "derived_text_markdown": str(
            payload.get("derived_text_markdown") or ""
        ).strip(),
        "derived_text_sha256": str(
            payload.get("derived_text_sha256")
            or hashlib.sha256(
                str(payload.get("derived_text_markdown") or "")
                .strip()
                .encode("utf-8")
            ).hexdigest()
        ),
        "citations": [
            dict(item) for item in payload.get("citations") or []
        ],
        # The voucher profile's structured half is part of the artifact's
        # content identity: without it a reconciler could not tell a committed
        # voucher analysis from one carrying only the narrative fields.
        "fields": dict(payload.get("fields") or {}),
        "prepared_media_set_hash": str(
            payload.get("prepared_media_set_hash") or ""
        ),
        "coverage": dict(payload.get("coverage") or {}),
    }
    return hashlib.sha1(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def generated_record(workspace: Workspace, document_id: str) -> dict | None:
    """The newest durable generated analysis for a document, if one exists.

    A candidate supersedes the active artifact because it is what the most recent
    generation produced; the auditor decides whether it replaces the active one.
    """
    index = load_index(workspace, document_id)
    for key in ("candidate_analysis_id", "active_analysis_id"):
        artifact = _load_generated(workspace, document_id, index.get(key))
        if artifact:
            return artifact
    return None


# The generated-analysis fields that identify the outcome. Provider, model, and
# generation timestamps are excluded so a parent/postcondition projection stays
# material rather than volatile.
_GENERATED_PROJECTION_FIELDS = (
    "id",
    "document_id",
    "source_sha1",
    "extracted_text_sha1",
    "derived_text_sha256",
    "prepared_media_set_hash",
    "cache_identity",
    "content_sha1",
    "agent_run_id",
    "summary_markdown",
    "audit_notes_markdown",
    "derived_text_markdown",
    "vision_used",
    "generation_profiles",
    "citations",
    "coverage",
)


def generated_projection(workspace: Workspace, document_id: str) -> dict | None:
    """Material projection of a document's generated analysis for hashing."""

    artifact = generated_record(workspace, document_id)
    if artifact is None:
        return None
    return {key: artifact.get(key) for key in _GENERATED_PROJECTION_FIELDS}


def _authoritative_status(workspace: Workspace, document: dict) -> dict:
    result = default_status()
    index = load_index(workspace, document["id"])
    review = load_review(workspace, document["id"])
    active = _load_generated(workspace, document["id"], index.get("active_analysis_id"))
    result.update(
        analysis_run_state=index.get("run_state") or "idle",
        analysis_updated_at=index.get("updated_at"),
        analysis_review_state=review.get("review_state") or "not_applicable",
        has_analysis_overrides=bool(review.get("summary_override") is not None or review.get("audit_notes_override") is not None),
        candidate_analysis_id=index.get("candidate_analysis_id"),
        analysis_resumable_run_id=index.get("resumable_run_id"),
    )
    if active:
        result["analysis_vision_used"] = bool(active.get("vision_used"))
        result["analysis_coverage_state"] = (active.get("coverage") or {}).get("state", "none")
        extraction = _read_json(workspace.root / "Documents" / ".extracted" / f"{document['id']}.json", {})
        current_text_sha = extracted_text_sha(extraction) if extraction else ""
        current_identity = cache_identity(
            str(document.get("sha1") or ""),
            current_text_sha,
            prepared_media_set_hash=str(
                active.get("prepared_media_set_hash") or ""
            ),
        )
        result["analysis_validity_state"] = (
            "current" if active.get("cache_identity") == current_identity and active.get("source_sha1") == document.get("sha1") else "stale"
        )
    search_manifest = _read_json(workspace.root / "Documents" / ".search" / document["id"] / "manifest.json", {})
    if search_manifest:
        runtime = embedding.status()
        runtime_mismatch = (
            search_manifest.get("embedding_model_sha256") != runtime.get("model_sha256")
            or int(search_manifest.get("embedding_dimension") or 0) != int(runtime.get("dimension") or 0)
            or search_manifest.get("tokenizer_version") != runtime.get("tokenizer_version")
        )
        analysis_mismatch = search_manifest.get(
            "analysis_content_sha1", ""
        ) != str((active or {}).get("content_sha1") or "")
        if (
            search_manifest.get("source_sha1") != document.get("sha1")
            or runtime_mismatch
            or analysis_mismatch
        ):
            result["search_index_state"] = "stale"
        else:
            result["search_index_state"] = search_manifest.get("state") or "pending"
    elif (
        document.get("text_state") == "image_only"
        and not bool((active or {}).get("derived_text_markdown"))
    ):
        result["search_index_state"] = "unsupported"
    return result


def rebuild_status_catalog(workspace: Workspace) -> dict:
    with _catalog_lock:
        entries = {doc["id"]: _authoritative_status(workspace, doc) for doc in workspace.documents}
        payload = {"schema_version": STATUS_SCHEMA_VERSION,
                   "search_runtime_identity": embedding.runtime_identity(),
                   "analysis_prompt_version": ANALYSIS_PROMPT_VERSION, "entries": entries}
        write_json_atomic(status_path(workspace), payload)
        return payload


def status_catalog(workspace: Workspace) -> dict:
    payload = _read_json(status_path(workspace), {})
    if (payload.get("schema_version") != STATUS_SCHEMA_VERSION
            or payload.get("search_runtime_identity") != embedding.runtime_identity()
            or payload.get("analysis_prompt_version") != ANALYSIS_PROMPT_VERSION
            or not isinstance(payload.get("entries"), dict)):
        return rebuild_status_catalog(workspace)
    return payload


def update_status(workspace: Workspace, document_id: str, **changes) -> dict:
    with _catalog_lock:
        catalog = status_catalog(workspace)
        entry = {**default_status(), **dict(catalog["entries"].get(document_id) or {}), **changes}
        catalog["entries"][document_id] = entry
        write_json_atomic(status_path(workspace), catalog)
        return entry


def remove_status(workspace: Workspace, document_id: str) -> None:
    with _catalog_lock:
        catalog = status_catalog(workspace)
        catalog["entries"].pop(document_id, None)
        write_json_atomic(status_path(workspace), catalog)


def inventory(workspace: Workspace) -> list[dict]:
    entries = status_catalog(workspace)["entries"]
    return [{**doc, **default_status(), **dict(entries.get(doc["id"]) or {})} for doc in workspace.documents]


def repair_status(workspace: Workspace, document_id: str) -> dict:
    document = next((item for item in workspace.documents if item.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    return update_status(workspace, document_id, **_authoritative_status(workspace, document))


def analysis_chunks(extracted: dict, max_characters: int = ANALYSIS_CHUNK_CHARACTERS) -> list[dict]:
    """Build complete, non-overlapping model chunks with exact page offsets."""
    chunks: list[dict] = []
    for page in extracted.get("pages") or []:
        text = str(page.get("text") or "")
        if not text:
            continue
        page_no = int(page["page"])
        start = 0
        while start < len(text):
            hard_end = min(len(text), start + max(1, int(max_characters)))
            end = hard_end
            if hard_end < len(text):
                boundary = max(text.rfind("\n\n", start, hard_end), text.rfind("\n", start, hard_end))
                if boundary > start + max_characters // 2:
                    end = boundary + (2 if text[boundary:boundary + 2] == "\n\n" else 1)
            chunks.append({"id": f"AC-{len(chunks)+1:04d}", "pages": [page_no],
                           "page": page_no, "start_character": start,
                           "end_character": end, "text": text[start:end]})
            start = end
    return chunks


def validate_citations(citations: list[dict], chunks: list[dict], source_sha1: str) -> list[dict]:
    allowed: dict[int, list[str]] = {}
    for chunk in chunks:
        for page in chunk.get("pages") or []:
            allowed.setdefault(int(page), []).append(str(chunk.get("text") or ""))
    output, seen = [], set()
    for value in citations or []:
        try:
            page = int(value.get("page"))
        except (TypeError, ValueError):
            continue
        excerpt = str(value.get("excerpt") or "").strip()
        if not excerpt or not any(excerpt in text for text in allowed.get(page, [])):
            continue
        key = (page, excerpt)
        if key in seen:
            continue
        seen.add(key)
        output.append({"id": str(value.get("id") or len(output) + 1), "page": page,
                       "excerpt": excerpt, "excerpt_hash": hashlib.sha1(excerpt.encode()).hexdigest(),
                       "source_sha1": source_sha1})
    return output


# --------------------------------------------------------------------------- #
# Voucher profile: structured, citation-anchored fields
#
# The profile is document-centric on purpose. It records what the voucher *is* —
# its identifiers, parties, dates, amounts, lines, approvals, and attachments —
# not what any one test wants to compare, so one extraction serves every test
# that touches the document. A vouching comparison reads these groups through
# its own ``<role>.<field>`` path resolver.
# --------------------------------------------------------------------------- #

# Each group maps to the keys an entry must carry to be usable at all. An entry
# missing one of them is dropped rather than stored half-formed, because a
# comparison that reads it would otherwise silently compare against nothing.
VOUCHER_FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "identifiers": ("kind", "value"),
    "parties": ("role", "name"),
    "dates": ("kind", "value"),
    "amounts": ("kind", "value"),
    "line_items": ("description",),
    "approvals": ("approver",),
    "attachments": ("kind",),
}

# Value normalization per group. Everything keeps its verbatim ``raw`` alongside
# the typed value, so an unparseable amount or date stays visible as evidence
# instead of disappearing, and a comparison can route it to manual review.
_VOUCHER_NUMERIC_FIELDS = {"amounts": ("value",), "line_items": ("amount",)}
_VOUCHER_DATE_FIELDS = {
    "dates": ("value",),
    "approvals": ("date",),
    "line_items": ("date",),
}
_VOUCHER_BOOLEAN_FIELDS = {"attachments": ("present",)}

_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d",
    "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
)
_AMOUNT_RE = re.compile(r"-?\d[\d,\s]*(?:\.\d+)?")
_TRUE_TOKENS = {"true", "yes", "y", "present", "attached", "1"}
_FALSE_TOKENS = {"false", "no", "n", "absent", "missing", "not attached", "0"}


def _iso_date(value: object) -> str | None:
    """Normalize a model-reported date to ISO, or ``None`` if unparseable."""
    from datetime import datetime

    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _numeric(value: object) -> float | None:
    """Normalize a model-reported amount, tolerating separators and symbols."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _AMOUNT_RE.search(str(value or ""))
    if match is None:
        return None
    try:
        return float(re.sub(r"[,\s]", "", match.group(0)))
    except ValueError:
        return None


def _boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return None


def _voucher_entry(group: str, raw: dict, allowed_citations: set[str]) -> dict | None:
    """Normalize one entry, or drop it when it is ungrounded or incomplete.

    Grounding is the hard rule: an entry whose ``citation`` does not name a
    citation that survived :func:`validate_citations` is discarded. That keeps
    every structured field anchored to an excerpt proven verbatim in the supplied
    source, exactly as the narrative half already is.
    """
    if not isinstance(raw, dict):
        return None
    citation = str(raw.get("citation") or "").strip()
    if citation not in allowed_citations:
        return None
    entry: dict = {"citation": citation}
    for key, value in raw.items():
        if key == "citation":
            continue
        entry[str(key)] = value
    for key in VOUCHER_FIELD_GROUPS[group]:
        if str(entry.get(key) or "").strip() == "" and entry.get(key) is not True:
            return None
    for key in _VOUCHER_NUMERIC_FIELDS.get(group, ()):
        if key in entry:
            entry["raw_" + key] = str(entry[key])
            entry[key] = _numeric(entry[key])
    for key in _VOUCHER_DATE_FIELDS.get(group, ()):
        if key in entry:
            entry["raw_" + key] = str(entry[key])
            entry[key] = _iso_date(entry[key])
    for key in _VOUCHER_BOOLEAN_FIELDS.get(group, ()):
        if key in entry:
            entry["raw_" + key] = str(entry[key])
            entry[key] = _boolean(entry[key])
    for key, value in list(entry.items()):
        if isinstance(value, str):
            entry[key] = value.strip()
    return entry


def validate_voucher_fields(payload: dict, citations: list[dict]) -> dict:
    """Normalize one voucher profile's structured fields against its citations.

    Returns the ``fields`` record. Unknown groups are dropped, every entry is
    type-normalized, and any entry that is not anchored to a validated citation
    is discarded — so a model cannot introduce a comparable value that no excerpt
    supports.
    """
    allowed = {str(item.get("id") or "") for item in citations or []}
    allowed.discard("")
    source = payload if isinstance(payload, dict) else {}
    fields: dict = {
        "document_type": str(source.get("document_type") or "").strip(),
    }
    for group in VOUCHER_FIELD_GROUPS:
        entries = []
        for raw in source.get(group) or []:
            entry = _voucher_entry(group, raw, allowed)
            if entry is not None:
                entries.append(entry)
        fields[group] = entries
    return fields


def _voucher_entry_identity(group: str, entry: dict) -> str:
    """Dedupe identity for a merged entry: its content, ignoring which chunk saw it."""
    material = {
        key: value
        for key, value in entry.items()
        if key != "citation" and not key.startswith("raw_")
    }
    return json.dumps(
        [group, material], sort_keys=True, separators=(",", ":"), default=str
    )


def merge_voucher_fields(values: list[dict]) -> dict:
    """Consolidate per-chunk voucher fields deterministically.

    Reduction of the structured half is a union with de-duplication, never a
    model turn: merging typed records through a model would be lossy and would
    cost a provider call per document for no gain. The narrative half is still
    reduced by the existing reduction worker.
    """
    merged: dict = {"document_type": ""}
    for group in VOUCHER_FIELD_GROUPS:
        merged[group] = []
    seen: set[str] = set()
    for value in values or []:
        if not isinstance(value, dict):
            continue
        if not merged["document_type"]:
            merged["document_type"] = str(value.get("document_type") or "").strip()
        for group in VOUCHER_FIELD_GROUPS:
            for entry in value.get(group) or []:
                if not isinstance(entry, dict):
                    continue
                identity = _voucher_entry_identity(group, entry)
                if identity in seen:
                    continue
                seen.add(identity)
                merged[group].append(entry)
    return merged


def validate_analysis_text(payload: dict) -> dict:
    """Normalize the required human-facing fields or reject an incomplete model response."""
    summary = str(payload.get("summary_markdown") or "").strip()
    audit_notes = str(payload.get("audit_notes_markdown") or "").strip()
    missing = []
    if not summary:
        missing.append("summary_markdown")
    if not audit_notes:
        missing.append("audit_notes_markdown")
    if missing:
        raise ValueError(f"Required analysis field(s) were blank: {', '.join(missing)}")
    return {"summary_markdown": summary, "audit_notes_markdown": audit_notes}


def validate_analysis_map(payload: dict, chunks: list[dict], source_sha1: str) -> dict:
    """Validate map text and require at least one exact citation from supplied source."""
    output = validate_analysis_text(payload)
    citations = validate_citations(payload.get("citations") or [], chunks, source_sha1)
    if not citations:
        raise ValueError(
            "citations contained no exact excerpt from the supplied source chunk"
        )
    output["citations"] = citations
    return output


def persist_analysis(workspace: Workspace, document: dict, extracted: dict, output: dict,
                     *, provider: str | None, model: str | None, action: str = "analyze",
                     coverage: dict | None = None, agent_run_id: str | None = None,
                     unit_id: str | None = None) -> dict:
    from .documents import utcnow
    document_id = document["id"]
    with document_lock(workspace, document_id):
        index, review = load_index(workspace, document_id), load_review(workspace, document_id)
        analysis_id = f"DA-{uuid.uuid4().hex[:12].upper()}"
        text_sha = extracted_text_sha(extracted)
        derived_text = str(output.get("derived_text_markdown") or "").strip()
        derived_text_sha256 = hashlib.sha256(
            derived_text.encode("utf-8")
        ).hexdigest()
        prepared_media_set_hash = str(
            output.get("prepared_media_set_hash") or ""
        )
        artifact = {
            "schema_version": ANALYSIS_SCHEMA_VERSION, "id": analysis_id,
            "document_id": document_id, "source_sha1": document["sha1"],
            "extracted_text_sha1": text_sha,
            "derived_text_markdown": derived_text,
            "derived_text_sha256": derived_text_sha256,
            "prepared_media_set_hash": prepared_media_set_hash,
            "cache_identity": cache_identity(
                document["sha1"],
                text_sha,
                prepared_media_set_hash=prepared_media_set_hash,
            ),
            "prompt_version": ANALYSIS_PROMPT_VERSION, "provider": provider,
            "model": model, "generated_at": utcnow(),
            "vision_used": bool(output.get("vision_used")),
            "generation_profiles": list(
                output.get("generation_profiles") or []
            ),
            # Workflow provenance: which durable run and semantic unit produced
            # this artifact, so an interrupted commit is proven applied rather
            # than repeated into a second artifact.
            "agent_run_id": agent_run_id, "unit_id": unit_id,
            "summary_markdown": str(output.get("summary_markdown") or "").strip(),
            "audit_notes_markdown": str(output.get("audit_notes_markdown") or "").strip(),
            "citations": list(output.get("citations") or []),
            # Empty for every profile but the voucher one, which is what makes
            # the structured half optional rather than a second artifact kind.
            "fields": dict(output.get("fields") or {}),
            "analysis_profile": str(output.get("analysis_profile") or "standard"),
            "coverage": coverage or {"state": "complete", "analyzed_pages": [int(p["page"]) for p in extracted.get("pages") or [] if p.get("text")], "omitted_pages": []},
        }
        artifact["content_sha1"] = analysis_content_sha1(artifact)
        write_json_atomic(_generated_path(workspace, document_id, analysis_id), artifact)
        if action == "refresh" and index.get("active_analysis_id"):
            index["candidate_analysis_id"] = analysis_id
        elif index.get("active_analysis_id") and (review.get("summary_override") is not None or review.get("audit_notes_override") is not None):
            index["candidate_analysis_id"] = analysis_id
        else:
            index["active_analysis_id"] = analysis_id
            index["candidate_analysis_id"] = None
            if review["review_state"] == "not_applicable":
                review["review_state"] = "needs_review"
                review["revision"] += 1
                review["updated_at"] = utcnow()
                write_json_atomic(_review_path(workspace, document_id), review)
        index.update(revision=int(index.get("revision") or 0) + 1, run_state="idle",
                     resumable_run_id=None, updated_at=utcnow())
        write_json_atomic(_index_path(workspace, document_id), index)
        if derived_text:
            update_status(
                workspace, document_id, search_index_state="pending"
            )
        update_status(workspace, document_id, **_authoritative_status(workspace, document))
        return load_analysis(workspace, document_id, document=document)


def load_analysis(workspace: Workspace, document_id: str, *, document: dict | None = None) -> dict:
    if document is None:
        document = next((item for item in workspace.documents if item.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    index, review = load_index(workspace, document_id), load_review(workspace, document_id)
    generated = _load_generated(workspace, document_id, index.get("active_analysis_id"))
    candidate = _load_generated(workspace, document_id, index.get("candidate_analysis_id"))
    effective = None
    if generated:
        effective = {**generated,
                     "summary_markdown": review.get("summary_override") if review.get("summary_override") is not None else generated.get("summary_markdown", ""),
                     "audit_notes_markdown": review.get("audit_notes_override") if review.get("audit_notes_override") is not None else generated.get("audit_notes_markdown", "")}
    return {"document_id": document_id, "index_revision": index["revision"],
            "review_revision": review["revision"], "generated": generated,
            "effective": effective, "candidate": candidate, "review": review,
            "status": _authoritative_status(workspace, document)}


def patch_review(workspace: Workspace, document_id: str, payload: dict) -> dict:
    document = next((item for item in workspace.documents if item.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    with document_lock(workspace, document_id):
        if not load_index(workspace, document_id).get("active_analysis_id"):
            raise WorkspaceError("Analyze the document before editing its analysis.")
        review = load_review(workspace, document_id)
        if int(payload.get("review_revision", -1)) != int(review["revision"]):
            raise AnalysisConflict("Document analysis review changed; reload it before saving.")
        for request_key, storage_key in (("summary_markdown", "summary_override"), ("audit_notes_markdown", "audit_notes_override")):
            if request_key in payload:
                value = payload[request_key]
                review[storage_key] = None if value is None else str(value)
        if "review_state" in payload:
            state = str(payload["review_state"])
            if state not in {"needs_review", "reviewed"}:
                raise WorkspaceError("Unknown document analysis review state.")
            review["review_state"] = state
        from .documents import utcnow
        review.update(revision=review["revision"] + 1, updated_at=utcnow(),
                      reviewed_at=utcnow() if review.get("review_state") == "reviewed" else review.get("reviewed_at"))
        write_json_atomic(_review_path(workspace, document_id), review)
        update_status(workspace, document_id, **_authoritative_status(workspace, document))
        return load_analysis(workspace, document_id, document=document)


def accept_candidate(workspace: Workspace, document_id: str, payload: dict) -> dict:
    document = next((item for item in workspace.documents if item.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    with document_lock(workspace, document_id):
        index, review = load_index(workspace, document_id), load_review(workspace, document_id)
        if int(payload.get("index_revision", -1)) != index["revision"] or int(payload.get("review_revision", -1)) != review["revision"]:
            raise AnalysisConflict("Document analysis changed; reload it before accepting the candidate.")
        candidate = index.get("candidate_analysis_id")
        if not candidate or not _generated_path(workspace, document_id, candidate).exists():
            raise WorkspaceError("No analysis candidate is awaiting review.")
        candidate_artifact = _load_generated(workspace, document_id, candidate)
        if not candidate_artifact or candidate_artifact.get("source_sha1") != document.get("sha1"):
            raise WorkspaceError("The analysis candidate belongs to an earlier document source and cannot be accepted.")
        from .documents import utcnow
        review.setdefault("decisions", []).append({"candidate_analysis_id": candidate, "decision": "accepted", "at": utcnow()})
        review.update(revision=review["revision"] + 1, review_state="needs_review", updated_at=utcnow())
        write_json_atomic(_review_path(workspace, document_id), review)
        index.update(active_analysis_id=candidate, candidate_analysis_id=None,
                     revision=index["revision"] + 1, updated_at=utcnow())
        write_json_atomic(_index_path(workspace, document_id), index)
        update_status(workspace, document_id, **_authoritative_status(workspace, document))
        return load_analysis(workspace, document_id, document=document)


def set_run_state(workspace: Workspace, document_id: str, state: str,
                  *, run_id: str | None = None, resumable: bool = False) -> None:
    with document_lock(workspace, document_id):
        index = load_index(workspace, document_id)
        index.update(run_state=state, resumable_run_id=run_id if resumable else None,
                     revision=index["revision"] + 1)
        from .documents import utcnow
        index["updated_at"] = utcnow()
        write_json_atomic(_index_path(workspace, document_id), index)
        update_status(workspace, document_id, analysis_run_state=state,
                      analysis_resumable_run_id=index["resumable_run_id"], analysis_updated_at=index["updated_at"])


def invalidate_for_replacement(workspace: Workspace, document_id: str) -> None:
    index = load_index(workspace, document_id)
    if index.get("active_analysis_id"):
        update_status(workspace, document_id, analysis_validity_state="stale", search_index_state="pending")
    else:
        update_status(workspace, document_id, search_index_state="pending")


def remove_sidecars(workspace: Workspace, document_id: str) -> None:
    import shutil
    shutil.rmtree(_root(workspace, document_id), ignore_errors=True)
    remove_status(workspace, document_id)


def compact_artifact(workspace: Workspace, document_id: str) -> dict | None:
    analysis = load_analysis(workspace, document_id)
    if not analysis["effective"] or analysis["status"]["analysis_validity_state"] != "current":
        return None
    effective = analysis["effective"]
    document = next(item for item in workspace.documents if item["id"] == document_id)
    return {"document_id": document_id, "title": document.get("title"),
            "source_sha1": document.get("sha1"), "analysis_id": effective["id"],
            "review_state": analysis["review"].get("review_state"),
            "summary_markdown": effective.get("summary_markdown", ""),
            "audit_notes_markdown": effective.get("audit_notes_markdown", ""),
            "derived_text_markdown": effective.get(
                "derived_text_markdown", ""
            ),
            "derived_text_sha256": effective.get(
                "derived_text_sha256", ""
            ),
            "vision_used": bool(effective.get("vision_used")),
            "generation_profiles": effective.get(
                "generation_profiles", []
            ),
            "citations": effective.get("citations", []), "coverage": effective.get("coverage", {}),
            "has_overrides": analysis["status"]["has_analysis_overrides"],
            "summary_overridden": analysis["review"].get("summary_override") is not None,
            "audit_notes_overridden": analysis["review"].get("audit_notes_override") is not None}
