"""Typed, durable evidence anchors used across the audit cycle."""

from __future__ import annotations

import hashlib
import uuid

from .workspaces import WorkspaceError

SOURCE_KINDS = {"document", "table", "analysis", "ruleset", "doctest", "procedure"}


def text_hash(value: str) -> str:
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()


def legacy_anchor(value: str) -> dict:
    """Lift an old opaque reference into the typed shape without losing it."""
    raw = str(value or "").strip()
    kind, separator, source_id = raw.partition(":")
    if not separator or kind not in SOURCE_KINDS:
        kind, source_id = "analysis", raw
    return {
        "id": f"EV-{uuid.uuid4().hex[:10].upper()}",
        "source_kind": kind,
        "source_id": source_id,
        "source_sha1": None,
        "page": None,
        "excerpt": "",
        "excerpt_hash": None,
        "item_id": None,
        "field": None,
        "generated_by": None,
        "confirmed_by": None,
        "confirmed_at": None,
        "legacy_ref": raw,
    }


def normalize_anchor(value: object, *, require_hash: bool = False) -> dict:
    if isinstance(value, str):
        if require_hash:
            raise WorkspaceError("New evidence references must use the typed anchor shape.")
        return legacy_anchor(value)
    if not isinstance(value, dict):
        raise WorkspaceError("Evidence references must be typed objects.")
    kind = str(value.get("source_kind") or "").strip().lower()
    source_id = str(value.get("source_id") or "").strip()
    if kind not in SOURCE_KINDS:
        raise WorkspaceError("Evidence source_kind is not supported.")
    if not source_id:
        raise WorkspaceError("Evidence source_id is required.")
    source_sha1 = str(value.get("source_sha1") or "").strip() or None
    if require_hash and not source_sha1:
        raise WorkspaceError("New evidence references require a source hash.")
    excerpt = str(value.get("excerpt") or "")
    page = value.get("page")
    if page not in (None, ""):
        try:
            page = int(page)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Evidence page must be a positive integer.") from error
        if page < 1:
            raise WorkspaceError("Evidence page must be a positive integer.")
    else:
        page = None
    return {
        "id": str(value.get("id") or f"EV-{uuid.uuid4().hex[:10].upper()}"),
        "source_kind": kind,
        "source_id": source_id,
        "source_sha1": source_sha1,
        "page": page,
        "excerpt": excerpt,
        "excerpt_hash": str(value.get("excerpt_hash") or "").strip() or (text_hash(excerpt) if excerpt else None),
        "item_id": str(value.get("item_id") or "").strip() or None,
        "field": str(value.get("field") or "").strip() or None,
        "generated_by": str(value.get("generated_by") or "").strip() or None,
        "confirmed_by": str(value.get("confirmed_by") or "").strip() or None,
        "confirmed_at": value.get("confirmed_at"),
        **({"legacy_ref": str(value.get("legacy_ref"))} if value.get("legacy_ref") else {}),
    }


def normalize_many(values: object, *, require_hash: bool = False) -> list[dict]:
    if values in (None, ""):
        return []
    if not isinstance(values, list):
        raise WorkspaceError("Evidence references must be an array.")
    return [normalize_anchor(value, require_hash=require_hash) for value in values]


def document_anchor(document: dict, page: int, excerpt: str, *, generated_by: str | None = None) -> dict:
    return normalize_anchor(
        {
            "source_kind": "document",
            "source_id": document["id"],
            "source_sha1": document["sha1"],
            "page": page,
            "excerpt": excerpt,
            "generated_by": generated_by,
        },
        require_hash=True,
    )
