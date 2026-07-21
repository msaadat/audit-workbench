"""Versioned local methodology packs with a small lexical index."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import workspaces
from .workspaces import Workspace, WorkspaceError, slugify, write_json_atomic

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root(workspace: Workspace, scope: str) -> Path:
    if scope == "workspace":
        return workspace.root / "KnowledgePacks"
    if scope == "reusable":
        return workspaces.WORKSPACES_DIR.parent / "KnowledgePacks"
    raise WorkspaceError("Knowledge-pack scope must be workspace or reusable.")


def _meta_path(workspace: Workspace, scope: str, pack_id: str) -> Path:
    root = _root(workspace, scope).resolve()
    path = (root / pack_id / "pack.json").resolve()
    if not path.is_relative_to(root):
        raise WorkspaceError("Unsafe knowledge-pack reference.")
    return path


def _sections(markdown: str) -> list[dict]:
    sections: list[dict] = []
    title = "Overview"
    lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("#") and line.lstrip("#").startswith(" "):
            if lines or sections:
                text = "\n".join(lines).strip()
                if text:
                    sections.append({"section": title, "text": text})
            title = line.lstrip("#").strip() or "Untitled"
            lines = []
        else:
            lines.append(line)
    text = "\n".join(lines).strip()
    if text:
        sections.append({"section": title, "text": text})
    return sections or [{"section": "Overview", "text": markdown.strip()}]


def _index(markdown: str) -> list[dict]:
    return [
        {"section": section["section"], "tokens": sorted(set(token.lower() for token in TOKEN_RE.findall(section["text"]))), "text": section["text"]}
        for section in _sections(markdown)
    ]


def save_pack(workspace: Workspace, name: str, content: str, *, scope: str = "workspace",
              source: str = "", pack_id: str | None = None) -> dict:
    name = str(name or "").strip()
    content = str(content or "").strip()
    if not name or not content:
        raise WorkspaceError("Knowledge-pack name and Markdown content are required.")
    pack_id = str(pack_id or slugify(name))
    path = _meta_path(workspace, scope, pack_id)
    prior = None
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
    sha1 = hashlib.sha1(content.encode("utf-8")).hexdigest()
    if prior and prior.get("sha1") == sha1:
        return prior
    version = int(prior.get("version") or 0) + 1 if prior else 1
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    version_file = f"v{version}-{sha1[:10]}.md"
    (folder / version_file).write_text(content, encoding="utf-8")
    prior_versions = list(prior.get("versions") or []) if prior else []
    meta = {
        "id": pack_id, "name": name, "scope": scope, "version": version,
        "sha1": sha1, "file": version_file, "source": str(source or ""),
        "created": prior.get("created") if prior else utcnow(), "updated": utcnow(),
        "versions": [*prior_versions, {"version": version, "sha1": sha1, "file": version_file, "created": utcnow()}],
    }
    write_json_atomic(path, meta)
    write_json_atomic(folder / "index.json", {"sha1": sha1, "sections": _index(content)})
    return meta


def _read_pack(workspace: Workspace, scope: str, pack_id: str, *, include_content: bool = False) -> dict:
    path = _meta_path(workspace, scope, pack_id)
    if not path.exists():
        raise WorkspaceError(f"Knowledge pack '{pack_id}' not found.")
    meta = json.loads(path.read_text(encoding="utf-8"))
    if include_content:
        meta["markdown"] = (path.parent / meta["file"]).read_text(encoding="utf-8")
    return meta


def list_packs(workspace: Workspace) -> list[dict]:
    items = []
    for scope in ("workspace", "reusable"):
        root = _root(workspace, scope)
        if not root.exists():
            continue
        for path in root.glob("*/pack.json"):
            try: items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError): continue
    return sorted(items, key=lambda item: (item.get("name") or "").lower())


def get_pack(workspace: Workspace, scope: str, pack_id: str) -> dict:
    return _read_pack(workspace, scope, pack_id, include_content=True)


def remove_pack(workspace: Workspace, scope: str, pack_id: str) -> None:
    path = _meta_path(workspace, scope, pack_id)
    if not path.exists():
        raise WorkspaceError(f"Knowledge pack '{pack_id}' not found.")
    shutil.rmtree(path.parent)


def context_sections(workspace: Workspace) -> list[dict]:
    """Return the local, indexed methodology sections used by context search.

    Context adapters use this inventory rather than reopening pack Markdown or
    reimplementing the pack index.  The returned text remains local until a
    resolver applies a declared representation and hard budget.
    """
    results = []
    for pack in list_packs(workspace):
        path = _meta_path(workspace, pack["scope"], pack["id"])
        index_path = path.parent / "index.json"
        try:
            sections = json.loads(index_path.read_text(encoding="utf-8"))["sections"]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
        for position, section in enumerate(sections, start=1):
            title = str(section.get("section") or "Overview")
            results.append({
                "pack_id": pack["id"],
                "pack_name": pack["name"],
                "scope": pack["scope"],
                "version": pack["version"],
                "sha1": pack["sha1"],
                "section": title,
                "section_index": position,
                "tokens": list(section.get("tokens") or []),
                "text": str(section.get("text") or ""),
                "citation": f"{pack['name']} v{pack['version']}, {title}",
            })
    return results


def search(workspace: Workspace, query: str, *, limit: int = 10) -> list[dict]:
    tokens = set(token.lower() for token in TOKEN_RE.findall(str(query or "")))
    if not tokens:
        return []
    results = []
    for section in context_sections(workspace):
        overlap = tokens & set(section.get("tokens") or [])
        if not overlap:
            continue
        results.append({
            "pack_id": section["pack_id"],
            "pack_name": section["pack_name"],
            "scope": section["scope"],
            "version": section["version"],
            "sha1": section["sha1"],
            "section": section["section"],
            "excerpt": section["text"][:500],
            "score": len(overlap) / len(tokens),
            "citation": section["citation"],
        })
    return sorted(results, key=lambda item: (-item["score"], item["pack_name"], item["section"]))[:max(1, min(int(limit), 50))]
