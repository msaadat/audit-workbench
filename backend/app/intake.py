"""Incremental, browser-selected audit-folder intake.

The browser supplies relative manifest keys; they are never treated as local
filesystem paths.  Uploaded candidates are staged under generated item IDs,
inspected locally, then incorporated idempotently into ``Data`` or
``Documents``. The batch stores technical metadata used by the classification
model.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from . import document_types, loader
from .workspaces import Workspace, WorkspaceError, slugify, write_json_atomic
from .text import counted, verb

IMPORTS_DIRNAME = "Imports"
INDEX_FILENAME = "index.json"
TABLE_SUFFIXES = frozenset(loader.SUPPORTED_SUFFIXES)
DOCUMENT_SUFFIXES = frozenset(
    {".pdf", ".txt", ".md", ".markdown", ".docx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
)
SUPPORTED_SUFFIXES = TABLE_SUFFIXES | DOCUMENT_SUFFIXES
#: What a document is *to this engagement*. Four values, read from page one by
#: ``documents.categorized`` rather than guessed from a filename.
#:
#: The partition is the load-bearing part, not the labels. Three planning values
#: describe how the entity should operate; one evidence value is a record of a
#: transaction. Every consumer tests set membership and none switches on an
#: individual value, so the names are free to read well while the split carries
#: the meaning.
#:
#: Exhaustive by construction: every value belongs to exactly one side, checked
#: on import below. A value in neither is invisible — not planning material, so
#: out of a planning-scoped run; not evidence, so never classified, never
#: induced, never read under fields.
DOCUMENT_CATEGORIES = frozenset(
    {"policy", "minutes", "background", "evidence"}
)
ROUTES = frozenset({"table", "document", "unsupported", "ignore"})
PLANNING_DOCUMENT_CATEGORIES = frozenset({"policy", "minutes", "background"})
# Transaction-level evidence: the only material read under its type's induced
# schema, which extracts the structured records a cycle comparison reads.
# Deliberately disjoint from ``PLANNING_DOCUMENT_CATEGORIES`` — evidence is
# never planning material, and the planning/APM context selectors stay
# constrained to that set so a voucher analysis can never enter a planning
# prompt. The disjointness runs the other way too: planning material keeps its
# narrative analysis, which is the form those selectors consume.
EVIDENCE_DOCUMENT_CATEGORIES = frozenset({"evidence"})
if PLANNING_DOCUMENT_CATEGORIES | EVIDENCE_DOCUMENT_CATEGORIES != DOCUMENT_CATEGORIES:
    raise RuntimeError("Document categories must partition into planning and evidence.")
if PLANNING_DOCUMENT_CATEGORIES & EVIDENCE_DOCUMENT_CATEGORIES:
    raise RuntimeError("A document category cannot be both planning and evidence.")
PLANNING_DOCUMENT_TERMS = re.compile(
    r"\b(policy|policies|procedure|procedures|process|manual|sop|guideline|"
    r"standard|regulation|control|charter|terms of reference|prior audit|minutes)\b",
    re.IGNORECASE,
)
MAX_SUGGESTED_PLANNING_DOCUMENTS = 8
DIRECT_UPLOADS_LABEL = "Direct uploads"
EXCLUDED_NAMES = frozenset({".ds_store", "thumbs.db"})
EXCLUDED_FOLDER_NAMES = frozenset(
    {".git", ".cache", "__pycache__", "node_modules", "staging", ".extracted"}
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def imports_dir(workspace: Workspace) -> Path:
    return workspace.root / IMPORTS_DIRNAME


def _index_path(workspace: Workspace) -> Path:
    return imports_dir(workspace) / INDEX_FILENAME


def _empty_index() -> dict:
    return {"sources": [], "files": []}


def load_index(workspace: Workspace) -> dict:
    path = _index_path(workspace)
    if not path.exists():
        return _empty_index()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Could not read the folder-import index: {error}") from error
    return {
        "sources": list(value.get("sources") or []),
        "files": list(value.get("files") or []),
    }


def save_index(workspace: Workspace, index: dict) -> None:
    write_json_atomic(_index_path(workspace), index)


def list_sources(workspace: Workspace) -> list[dict]:
    return load_index(workspace)["sources"]


def create_source(workspace: Workspace, label: str, root_name: str = "") -> dict:
    label = str(label or "").strip()
    if not label:
        raise WorkspaceError("Folder source name is required.")
    root_name = _clean_root_name(root_name)
    index = load_index(workspace)
    source = {
        "id": uuid.uuid4().hex[:10],
        "label": label,
        "root_name": root_name,
        "created": utcnow(),
        "last_imported": None,
    }
    index["sources"].append(source)
    save_index(workspace, index)
    return source


def resolve_source(workspace: Workspace, root_name: str, manifest: list[dict]) -> dict:
    """Resolve browser-selected folders to an internal incremental source.

    Browser folder pickers deliberately withhold absolute paths, so the best
    stable identity available is the selected root name plus overlap with the
    paths imported previously.  Ambiguous matches create a new source rather
    than risk updating files from an unrelated same-named folder.

    Batches without a root name (loose files, or a mix of files and folders)
    share one durable "Direct uploads" source so re-uploads of the same path
    still dedupe incrementally.
    """
    root_name = _clean_root_name(root_name)
    if not root_name:
        index = load_index(workspace)
        direct = next(
            (
                source
                for source in index["sources"]
                if not source.get("root_name") and source.get("label") == DIRECT_UPLOADS_LABEL
            ),
            None,
        )
        return direct or create_source(workspace, DIRECT_UPLOADS_LABEL)

    selected_paths = {
        normalize_relative_path(item.get("relative_path")) for item in manifest or []
    }
    index = load_index(workspace)
    candidates = [
        source
        for source in index["sources"]
        if str(source.get("root_name") or "").casefold() == root_name.casefold()
    ]
    paths_by_source = {
        source["id"]: {
            item["relative_path"]
            for item in index["files"]
            if item.get("source_id") == source["id"]
        }
        for source in candidates
    }
    overlaps = [
        (len(selected_paths & paths_by_source[source["id"]]), source)
        for source in candidates
    ]
    best_overlap = max((count for count, _ in overlaps), default=0)
    best = [source for count, source in overlaps if count == best_overlap and count > 0]
    if len(best) == 1:
        return best[0]

    empty = [source for source in candidates if not paths_by_source[source["id"]]]
    if best_overlap == 0 and len(empty) == 1:
        return empty[0]

    return create_source(workspace, root_name, root_name)


def _source(index: dict, source_id: str) -> dict:
    source = next((item for item in index["sources"] if item.get("id") == source_id), None)
    if source is None:
        raise WorkspaceError("Folder source not found.")
    return source


def _clean_root_name(value: str) -> str:
    value = str(value or "").replace("\\", "/").strip("/ ")
    return PurePosixPath(value).name if value else ""


def normalize_relative_path(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise WorkspaceError("Folder entries must use a relative path.")
    if any(part in ("", ".", "..") for part in path.parts):
        raise WorkspaceError(f"Unsafe relative path '{raw}'.")
    return path.as_posix()


def exclusion_reason(relative_path: str, size: int) -> str | None:
    parts = PurePosixPath(relative_path).parts
    name = parts[-1]
    lowered = name.lower()
    if size <= 0:
        return "zero_byte"
    if name.startswith("~$"):
        return "office_lock_file"
    if lowered in EXCLUDED_NAMES:
        return "system_file"
    if any(part.lower() in EXCLUDED_FOLDER_NAMES for part in parts[:-1]):
        return "cache_folder"
    return None


def batch_dir(workspace: Workspace, batch_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{10}", str(batch_id or "")):
        raise WorkspaceError("Invalid folder-import batch ID.")
    return imports_dir(workspace) / batch_id


def _batch_path(workspace: Workspace, batch_id: str) -> Path:
    return batch_dir(workspace, batch_id) / "batch.json"


def load_batch(workspace: Workspace, batch_id: str) -> dict:
    path = _batch_path(workspace, batch_id)
    if not path.exists():
        raise WorkspaceError("Folder-import batch not found.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Could not read folder-import batch: {error}") from error


def save_batch(workspace: Workspace, batch: dict) -> None:
    batch["updated"] = utcnow()
    write_json_atomic(_batch_path(workspace, batch["id"]), batch)


def compare_manifest(
    workspace: Workspace,
    source_id: str,
    manifest: list[dict],
    mode: str = "permission",
    force: bool = False,
) -> dict:
    if mode not in ("auto", "permission"):
        raise WorkspaceError("Import mode must be 'auto' or 'permission'.")
    index = load_index(workspace)
    _source(index, source_id)
    prior = {
        item["relative_path"]: item
        for item in index["files"]
        if item.get("source_id") == source_id
    }
    seen: set[str] = set()
    items = []
    unchanged = excluded = unsupported = 0
    for raw in manifest or []:
        relative_path = normalize_relative_path(raw.get("relative_path"))
        if relative_path in seen:
            raise WorkspaceError(f"Duplicate manifest path '{relative_path}'.")
        seen.add(relative_path)
        try:
            size = int(raw.get("size") or 0)
            last_modified = int(raw.get("last_modified") or 0)
        except (TypeError, ValueError) as error:
            raise WorkspaceError(f"Invalid size or timestamp for '{relative_path}'.") from error
        suffix = Path(relative_path).suffix.lower()
        reason = exclusion_reason(relative_path, size)
        old = prior.get(relative_path)
        same = bool(
            old
            and int(old.get("size") or 0) == size
            and int(old.get("last_modified") or 0) == last_modified
        )
        state = "excluded" if reason else "unchanged" if same and not force else "new" if old is None else "changed"
        needs_upload = state in ("new", "changed") and suffix in SUPPORTED_SUFFIXES
        if state == "excluded":
            excluded += 1
        elif state == "unchanged":
            unchanged += 1
        elif suffix not in SUPPORTED_SUFFIXES:
            unsupported += 1
            needs_upload = False
        items.append(
            {
                "id": uuid.uuid4().hex[:10],
                "relative_path": relative_path,
                "size": size,
                "last_modified": last_modified,
                "mime": str(raw.get("mime") or mimetypes.guess_type(relative_path)[0] or ""),
                "state": state,
                "needs_upload": needs_upload,
                "uploaded": False,
                "sha1": None,
                "staged_file": None,
                "local_metadata": {"extension": suffix, "supported": suffix in SUPPORTED_SUFFIXES},
                "classification": None,
                "action": "ignore" if reason or suffix not in SUPPORTED_SUFFIXES else "import",
                "target_ref": None,
                "error": reason or ("unsupported_extension" if suffix not in SUPPORTED_SUFFIXES else None),
            }
        )
    now = utcnow()
    batch = {
        "id": uuid.uuid4().hex[:10],
        "source_id": source_id,
        "mode": mode,
        "status": "uploading",
        "manifest_count": len(items),
        "unchanged_count": unchanged,
        "excluded_count": excluded,
        "unsupported_count": unsupported,
        "items": items,
        "created": now,
        "updated": now,
    }
    save_batch(workspace, batch)
    return {"batch": batch, "upload_paths": [item["relative_path"] for item in items if item["needs_upload"]]}


def staging_path(workspace: Workspace, batch: dict, item: dict) -> Path:
    suffix = Path(item["relative_path"]).suffix.lower()
    target = batch_dir(workspace, batch["id"]) / "Staging" / f"{item['id']}{suffix}"
    root = (batch_dir(workspace, batch["id"]) / "Staging").resolve()
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise WorkspaceError("Unsafe staging path.")
    return target


def requested_item(batch: dict, relative_path: str) -> dict:
    relative_path = normalize_relative_path(relative_path)
    item = next((item for item in batch["items"] if item["relative_path"] == relative_path), None)
    if item is None or not item.get("needs_upload"):
        raise WorkspaceError("This file was not requested for this import batch.")
    return item


def mark_uploaded(workspace: Workspace, batch: dict, item: dict, sha1: str, size: int, target: Path) -> None:
    if int(item["size"]) != size:
        target.unlink(missing_ok=True)
        raise WorkspaceError(f"Uploaded size for '{item['relative_path']}' does not match its manifest.")
    item.update(uploaded=True, sha1=sha1, staged_file=target.name, error=None)
    save_batch(workspace, batch)


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _docx_text_character_count(path: Path) -> tuple[int, int]:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
            root = ElementTree.fromstring(xml)
            text = " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
            media = sum(1 for name in archive.namelist() if name.startswith("word/media/"))
            return len(text.strip()), media
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return 0, 0


def inspect_staged_file(path: Path, relative_path: str) -> dict:
    suffix = path.suffix.lower()
    base = {
        "extension": suffix,
        "mime": mimetypes.guess_type(relative_path)[0] or "application/octet-stream",
        "size": path.stat().st_size,
        "supported": suffix in SUPPORTED_SUFFIXES,
    }
    if suffix in TABLE_SUFFIXES:
        try:
            frame = loader.read_table(path)
            return {
                **base,
                "route": "table",
                "parse_ok": frame.width > 0,
                "rows": frame.height,
                "columns": [{"name": name, "dtype": str(dtype)} for name, dtype in frame.schema.items()],
            }
        except Exception as error:
            return {**base, "route": "table", "parse_ok": False, "error": str(error)}
    if suffix in DOCUMENT_SUFFIXES:
        meta = {**base, "route": "document", "parse_ok": True}
        if suffix in (".txt", ".md", ".markdown"):
            try:
                meta["text_characters"] = len(path.read_text(encoding="utf-8", errors="replace").strip())
            except OSError as error:
                meta.update(parse_ok=False, error=str(error))
        elif suffix == ".docx":
            chars, images = _docx_text_character_count(path)
            meta.update(text_characters=chars, embedded_images=images)
            if chars == 0 and images == 0:
                meta.update(parse_ok=False, error="DOCX content could not be read")
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
            meta["image_only"] = True
        return meta
    return {**base, "route": "unsupported", "parse_ok": False}


def complete_upload(workspace: Workspace, batch_id: str) -> dict:
    batch = load_batch(workspace, batch_id)
    if batch["status"] not in ("uploading", "classifying"):
        raise WorkspaceError("This import batch is no longer accepting uploads.")
    missing = [item["relative_path"] for item in batch["items"] if item["needs_upload"] and not item.get("uploaded")]
    if missing:
        raise WorkspaceError(f"Upload is incomplete; {counted(len(missing), 'requested file')} {verb(len(missing), 'is', 'are')} missing.")
    index = load_index(workspace)
    hashes = {item.get("sha1"): item for item in index["files"] if item.get("sha1")}
    for item in batch["items"]:
        if not item.get("uploaded"):
            continue
        path = staging_path(workspace, batch, item)
        if not path.exists() or _sha1(path) != item["sha1"]:
            raise WorkspaceError(f"Staged upload for '{item['relative_path']}' failed verification.")
        item["local_metadata"] = inspect_staged_file(path, item["relative_path"])
        duplicate = hashes.get(item["sha1"])
        item["classification"] = deterministic_classification(item, duplicate)
        if duplicate is None:
            hashes[item["sha1"]] = {
                "target_id": f"intake:{batch['id']}:{item['id']}"
            }
    batch["status"] = "classifying"
    save_batch(workspace, batch)
    return batch


def deterministic_classification(item: dict, duplicate: dict | None = None) -> dict:
    meta = item.get("local_metadata") or {}
    route = meta.get("route") if meta.get("parse_ok") else "unsupported"
    route = route if route in ROUTES else "unsupported"
    name = slugify(Path(item["relative_path"]).stem).replace("-", "_") or "imported_file"
    # Intake no longer proposes a category. What a document is *to this
    # engagement* is read from its opening page by ``documents.categorized``,
    # after import, because a filename cannot support the answer: the ladder
    # that used to stand here matched tokens on the stem, and its own prompt
    # conceded that filenames "are not evidence of document content". Left
    # unset rather than defaulted, because an unset category is in scope for
    # text extraction and a wrong one is not.
    document_category = None
    if not meta.get("parse_ok"):
        confidence = "low"
        rationale = meta.get("error") or "Unsupported format."
    else:
        confidence = "high"
        rationale = "Supported format parsed locally."
    return {
        "route": route,
        "document_category": document_category,
        "subtype": "",
        "proposed_name": name,
        "confidence": confidence,
        "rationale": rationale,
        "duplicate_ref": duplicate.get("target_id") if duplicate else None,
        "proposed_action": "ignore" if duplicate or route == "unsupported" else "import",
        "deterministic_route": route,
    }


def classification_payload_for_model(workspace: Workspace, batch: dict) -> dict:
    """Build focused model context for folder classification.

    No staging path, absolute path, cell value, row preview, formula, comment,
    or extracted document text is included here.
    """
    return {
        "batch_id": batch["id"],
        "items": [
            {
                "id": item["id"],
                "relative_path": item["relative_path"],
                "size": item["size"],
                "last_modified": item["last_modified"],
                "mime": item.get("mime") or "",
                "state": item["state"],
                "local_metadata": item.get("local_metadata") or {},
                "deterministic": item.get("classification") or {},
            }
            for item in batch["items"]
            if item.get("uploaded")
        ],
    }


def merge_model_classifications(batch: dict, proposals: list[dict]) -> None:
    known = {item["id"]: item for item in batch["items"] if item.get("uploaded")}
    for proposal in proposals or []:
        item = known.get(str(proposal.get("id") or proposal.get("item_id") or ""))
        if item is None:
            continue
        current = dict(item.get("classification") or {})
        route = proposal.get("route")
        confidence = proposal.get("confidence")
        if route in ROUTES:
            current["route"] = route
        # No category is merged. The model here sees filenames and technical
        # metadata and no document text, which is the wrong evidence for the
        # question; ``documents.categorized`` answers it from page one instead.
        if confidence in ("high", "medium", "low"):
            current["confidence"] = confidence
        for key in ("subtype", "proposed_name", "rationale"):
            if key in proposal:
                current[key] = str(proposal.get(key) or "").strip()
        if proposal.get("proposed_action") in ("import", "ignore"):
            current["proposed_action"] = proposal["proposed_action"]
        item["classification"] = current


def approval_specs(batch: dict) -> list[dict]:
    return [
        {
            "item_id": item["id"],
            "relative_path": item["relative_path"],
            **dict(item.get("classification") or {}),
        }
        for item in batch["items"]
        if item.get("uploaded")
    ]


def _validated_decision(item: dict, decision: dict) -> dict:
    base = dict(item.get("classification") or {})
    base.update({key: value for key, value in decision.items() if key not in {"item_id", "relative_path", "deterministic_route", "duplicate_ref"}})
    route = base.get("route")
    action = base.get("proposed_action", base.get("action", "import"))
    if route not in ROUTES or action not in ("import", "ignore"):
        raise WorkspaceError(f"Invalid classification for '{item['relative_path']}'.")
    # A category cannot arrive here: intake does not propose one and the review
    # step no longer offers one. Dropping anything that does keeps the field
    # single-sourced, so a stale caller cannot pin a document to a category
    # chosen before anything read it.
    base["document_category"] = None
    base["proposed_name"] = slugify(base.get("proposed_name") or Path(item["relative_path"]).stem).replace("-", "_")
    return base


def apply_batch(workspace: Workspace, batch_id: str, decisions: list[dict] | None = None) -> dict:
    batch = load_batch(workspace, batch_id)
    if batch["status"] == "completed":
        return batch
    if batch["status"] not in ("classifying", "awaiting_approval", "applying"):
        raise WorkspaceError("This import batch is not ready to apply.")
    by_id = {str(d.get("item_id")): d for d in (decisions or [])}
    batch["status"] = "applying"
    save_batch(workspace, batch)
    index = load_index(workspace)
    source = _source(index, batch["source_id"])
    indexed_by_path = {
        item["relative_path"]: item
        for item in index["files"]
        if item.get("source_id") == batch["source_id"]
    }
    for item in batch["items"]:
        if not item.get("uploaded") or item.get("target_ref"):
            continue
        classification = _validated_decision(item, by_id.get(item["id"], {}))
        item["classification"] = classification
        action = classification.get("proposed_action", "import")
        route = classification["route"]
        if action == "ignore" or route in ("ignore", "unsupported"):
            item["action"] = "ignore"
            continue
        if not (item.get("local_metadata") or {}).get("parse_ok"):
            item["error"] = "The file did not pass local parser validation."
            continue
        existing = indexed_by_path.get(item["relative_path"])
        if existing and existing.get("sha1") == item.get("sha1") and existing.get("target_id"):
            item["target_ref"] = f"{existing.get('route')}:{existing['target_id']}"
            item["action"] = "unchanged"
            continue
        staged = staging_path(workspace, batch, item)
        item["source_id"] = batch["source_id"]
        already = _already_incorporated(workspace, item, route)
        if already is not None:
            target_id = already
        elif route == "table":
            target_id = _incorporate_table_from_path(
                workspace, staged, item, classification, existing
            )
        else:
            target_id = _incorporate_document_from_path(
                workspace, staged, item, classification, existing
            )
        item["target_ref"] = f"{route}:{target_id}"
        item["action"] = "imported"
        record = {
            "source_id": batch["source_id"],
            "relative_path": item["relative_path"],
            "size": item["size"],
            "last_modified": item["last_modified"],
            "sha1": item["sha1"],
            "route": route,
            "category": classification.get("document_category"),
            "target_id": target_id,
            "imported_at": utcnow(),
            "history": [],
        }
        if existing:
            history = list(existing.get("history") or [])
            history.append(
                {
                    key: existing.get(key)
                    for key in (
                        "sha1",
                        "size",
                        "last_modified",
                        "route",
                        "target_id",
                        "imported_at",
                    )
                }
            )
            record["history"] = history
            existing.clear()
            existing.update(record)
        else:
            index["files"].append(record)
            indexed_by_path[item["relative_path"]] = record
        save_index(workspace, index)
        save_batch(workspace, batch)
        staged.unlink(missing_ok=True)
    source["last_imported"] = utcnow()
    save_index(workspace, index)
    workspace.save()
    batch["status"] = "completed"
    batch["summary"] = {
        "imported": sum(item.get("action") == "imported" for item in batch["items"]),
        "unchanged": batch.get("unchanged_count", 0) + sum(item.get("action") == "unchanged" for item in batch["items"]),
        "ignored": sum(item.get("action") == "ignore" for item in batch["items"]),
        "ambiguous": sum(bool(item.get("error")) and item.get("action") != "ignore" for item in batch["items"]),
    }
    batch["suggested_actions"] = suggested_actions(workspace, batch)
    imported_document_ids = [
        str(item["target_ref"]).split(":", 1)[1]
        for item in batch["items"]
        if item.get("action") == "imported"
        and str(item.get("target_ref") or "").startswith("document:")
    ]
    if imported_document_ids:
        from . import document_search
        batch["indexing_job"] = document_search.enqueue_indexing(
            workspace, imported_document_ids, reason="folder_intake",
        )
    save_batch(workspace, batch)
    shutil.rmtree(batch_dir(workspace, batch_id) / "Staging", ignore_errors=True)
    return batch


def suggested_actions(workspace: Workspace, batch: dict) -> list[dict]:
    """Return metadata-based next steps for newly imported material."""
    imported_documents = []
    for item in batch.get("items") or []:
        classification = dict(item.get("classification") or {})
        target_ref = str(item.get("target_ref") or "")
        if item.get("action") != "imported" or not target_ref.startswith("document:"):
            continue
        document_id = target_ref.split(":", 1)[1]
        document = next((doc for doc in workspace.documents if doc.get("id") == document_id), None)
        if document is None:
            continue
        imported_documents.append(
            {
                **document,
                "category": classification.get("document_category") or document.get("category") or "",
                "relative_path": item.get("relative_path"),
                "subtype": classification.get("subtype"),
            }
        )
    return planning_actions_for_documents(imported_documents)


def planning_actions_for_documents(imported_documents: list[dict]) -> list[dict]:
    """Recommend planning only for guidance/context documents, using metadata."""
    planning_documents = []
    for document in imported_documents:
        # Categories are not known at import any more, so this offer falls back
        # to the filename terms it always carried as a second signal. It is a
        # suggestion made before anything has been read, and it says so by
        # suggesting less rather than by guessing.
        category = str(document.get("category") or "")
        searchable = " ".join(
            str(document.get(key) or "")
            for key in ("title", "source", "relative_path", "subtype")
        )
        if category not in PLANNING_DOCUMENT_CATEGORIES and not PLANNING_DOCUMENT_TERMS.search(searchable):
            continue
        planning_documents.append(
            {
                "id": str(document.get("id") or ""),
                "title": document.get("title") or document.get("source") or document.get("relative_path"),
                "category": category,
                "pages": document.get("pages"),
            }
        )
    if not planning_documents:
        return []
    selected = planning_documents[:MAX_SUGGESTED_PLANNING_DOCUMENTS]
    omitted = len(planning_documents) - len(selected)
    return [
        {
            "id": "update_planning",
            "agent_kind": "planning",
            "title": "Update planning from imported guidance",
            "reason": (
                f"{counted(len(planning_documents), 'newly imported policy, procedure, or planning document')} "
                "may affect the engagement context, APM, RCM, and its tests."
            ),
            "document_ids": [item["id"] for item in selected],
            "documents": selected,
            "omitted_document_count": omitted,
            "requires_doc_ai": True,
        }
    ]


def _already_incorporated(
    workspace: Workspace, item: dict, route: str
) -> str | None:
    collection = workspace.tables if route == "table" else workspace.documents
    match = next(
        (
            candidate
            for candidate in collection
            if candidate.get("source_id") == item.get("source_id")
            and candidate.get("relative_path") == item.get("relative_path")
            and candidate.get("source_sha1", candidate.get("sha1")) == item.get("sha1")
        ),
        None,
    )
    if match is None:
        return None
    return match.get("name", match.get("id"))


def _incorporate_table_from_path(workspace: Workspace, staged: Path, item: dict, classification: dict, existing: dict | None) -> str:
    content = staged.read_bytes()
    filename = Path(item["relative_path"]).name
    if existing and existing.get("route") == "table" and workspace._table_entry(existing.get("target_id")):
        name = existing["target_id"]
        workspace.replace_table(name, filename, content)
        entry = workspace._table_entry(name)
    else:
        entry = workspace.add_table(filename, content)
        name = entry["name"]
        proposed = classification.get("proposed_name")
        if proposed and proposed != name and proposed not in workspace.table_names():
            workspace.rename_table(name, proposed)
            name = proposed
            entry = workspace._table_entry(name)
    entry.update(
        source_id=item.get("source_id"),
        relative_path=item["relative_path"],
        source_sha1=item["sha1"],
        imported_at=utcnow(),
    )
    workspace.save()
    return name


def _incorporate_document_from_path(workspace: Workspace, staged: Path, item: dict, classification: dict, existing: dict | None) -> str:
    from . import documents

    documents_dir = workspace.root / "Documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    previous = None
    if existing and existing.get("route") == "document":
        previous = next((doc for doc in workspace.documents if doc.get("id") == existing.get("target_id")), None)
    if previous is not None:
        replaced = documents.replace_document(
            workspace,
            previous["id"],
            Path(item["relative_path"]).name,
            staged.read_bytes(),
        )
        replaced.update(
            source_id=item.get("source_id"),
            relative_path=item["relative_path"],
            title=classification.get("proposed_name") or replaced.get("title") or Path(item["relative_path"]).stem,
            category="",
            created_by="intake",
        )
        workspace.save()
        return replaced["id"]

    doc_id = uuid.uuid4().hex[:10]
    suffix = staged.suffix.lower()
    target = documents_dir / f"{doc_id}{suffix}"
    shutil.copyfile(staged, target)
    doc = {
        "id": doc_id,
        "file": target.name,
        "source": Path(item["relative_path"]).name,
        "source_id": item.get("source_id"),
        "relative_path": item["relative_path"],
        "title": classification.get("proposed_name") or Path(item["relative_path"]).stem,
        # Unset until ``documents.categorized`` reads page one. The document
        # entry keeps the key so nothing downstream has to test for its
        # absence; the answer lives in the classification sidecar.
        "category": "",
        "pages": None,
        "sha1": item["sha1"],
        "text_state": "image_only" if (item.get("local_metadata") or {}).get("image_only") else "pending",
        "note": "",
        "created": utcnow(),
        "updated": None,
        "created_by": "intake",
        "agent_run_id": None,
    }
    workspace.documents.append(doc)
    workspace.save()
    documents.extract_document(workspace, doc_id)
    return doc_id


def cancel_batch(workspace: Workspace, batch_id: str) -> None:
    batch = load_batch(workspace, batch_id)
    if batch.get("status") == "completed":
        raise WorkspaceError("A completed import batch cannot be cancelled.")
    shutil.rmtree(batch_dir(workspace, batch_id), ignore_errors=True)
