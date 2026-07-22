"""Content-free context manifest identity, metrics, and persistence.

The resolver owns selection and ordering.  This module owns the deterministic
records produced from those decisions and the only durable context sidecar
shape.  It deliberately accepts :class:`ContextManifest`, never
:class:`ContextBundle`, so worker content cannot be written through this
boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ...workspaces import Workspace, WorkspaceError, write_json_atomic
from .. import store
from .model import (
    ContextManifest,
    ContextOmission,
    ContextSize,
    ContextTruncation,
)

_MISSING = object()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Context source must be deterministically hashable.") from error


def source_hash(value: object) -> str:
    """Hash source material with a stable type-aware SHA-256 identity."""
    if isinstance(value, bytes):
        encoded = b"bytes\0" + value
    elif isinstance(value, str):
        encoded = b"text\0" + value.encode("utf-8")
    else:
        encoded = b"json\0" + _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def supplied_size(content: object, *, items: int = 1) -> ContextSize:
    """Measure the exact local representation that will be supplied.

    Character counts use Unicode code points.  Token counts are a stable,
    conservative four-characters-per-token estimate used for pre-call budgets;
    provider-reported usage remains authoritative after a call.
    """
    if isinstance(items, bool) or not isinstance(items, int) or items < 0:
        raise ValueError("Supplied item count must be a non-negative integer.")
    if content is None:
        characters = 0
    elif isinstance(content, str):
        characters = len(content)
    else:
        characters = len(_canonical_json(content))
    estimated_tokens = (characters + 3) // 4
    return ContextSize(
        items=items,
        characters=characters,
        estimated_tokens=estimated_tokens,
    )


def total_supplied_size(sizes: Iterable[ContextSize]) -> ContextSize:
    """Aggregate supplied-size records without inspecting bundle content."""
    items = characters = estimated_tokens = 0
    for size in sizes:
        if not isinstance(size, ContextSize):
            raise ValueError("Supplied sizes must contain only ContextSize values.")
        items += size.items
        characters += size.characters
        estimated_tokens += size.estimated_tokens
    return ContextSize(
        items=items,
        characters=characters,
        estimated_tokens=estimated_tokens,
    )


def omission_record(
    *,
    source_id: str,
    reason: str,
    source_ref: str | None = None,
    source: object = _MISSING,
) -> ContextOmission:
    """Create a content-free omission record, hashing a candidate if known."""
    return ContextOmission(
        source_id=source_id,
        source_ref=source_ref,
        source_hash=None if source is _MISSING else source_hash(source),
        reason=reason,
    )


def truncation_record(
    *,
    source_id: str,
    source_ref: str,
    reason: str,
    original_content: object,
    supplied_content: object,
) -> ContextTruncation:
    """Create a content-free truncation record with before/after metrics."""
    return ContextTruncation(
        source_id=source_id,
        source_ref=source_ref,
        reason=reason,
        original_size=supplied_size(original_content),
        supplied_size=supplied_size(supplied_content),
    )


def manifest_identity(manifest: ContextManifest) -> str:
    """Return the deterministic identity of a normalized manifest."""
    if not isinstance(manifest, ContextManifest):
        raise ValueError("Only ContextManifest values have a durable manifest identity.")
    return manifest.manifest_hash


def _unit_filename(unit_id: str) -> str:
    # Unit IDs are semantic and may contain Windows-reserved characters such as
    # colons. Percent-encoding them keeps the one-unit/one-file layout portable
    # without lossy slugging.
    filename = quote(unit_id, safe="._-")
    if filename in {".", ".."}:
        filename = "".join(f"%{byte:02X}" for byte in unit_id.encode("utf-8"))
    return f"{filename}.json"


def _contexts_dir(workspace: Workspace, run_id: str) -> Path:
    run_folder = store.run_dir(workspace, run_id)
    if (
        run_folder.parent != store.runs_dir(workspace)
        or run_folder.name != run_id
        or not (run_folder / "run.json").is_file()
    ):
        raise WorkspaceError(f"Agent run '{run_id}' not found.")
    return run_folder / "contexts"


def persist_manifest(
    workspace: Workspace,
    run_id: str,
    manifest: ContextManifest,
) -> dict[str, str]:
    """Atomically replace one unit's content-free manifest sidecar."""
    if not isinstance(manifest, ContextManifest):
        raise ValueError(
            "Only ContextManifest values may be persisted; bundle content is local-only."
        )
    folder = _contexts_dir(workspace, run_id)
    path = folder / _unit_filename(manifest.unit_id)
    identity = manifest_identity(manifest)
    write_json_atomic(path, manifest.to_dict())
    return {
        "manifest_hash": identity,
        "path": f"contexts/{path.name}",
        "unit_id": manifest.unit_id,
    }


def load_manifest(
    workspace: Workspace,
    run_id: str,
    reference: Mapping[str, Any],
) -> ContextManifest:
    """Load and integrity-check a manifest persisted by :func:`persist_manifest`."""
    if not isinstance(reference, Mapping):
        raise WorkspaceError("Context manifest reference is invalid.")
    expected_hash = str(reference.get("manifest_hash") or "")
    expected_unit = str(reference.get("unit_id") or "")
    folder = _contexts_dir(workspace, run_id)
    path = store.run_dir(workspace, run_id) / str(reference.get("path") or "")
    if (
        not expected_hash.startswith("sha256:")
        or not expected_unit
        or not path.is_file()
        or path.parent != folder
        or path.name != _unit_filename(expected_unit)
    ):
        raise WorkspaceError("Context manifest not found.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = ContextManifest.from_dict(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise WorkspaceError("Context manifest is invalid.") from error
    if manifest.unit_id != expected_unit or manifest_identity(manifest) != expected_hash:
        raise WorkspaceError("Context manifest identity does not match its reference.")
    return manifest


__all__ = [
    "load_manifest",
    "manifest_identity",
    "omission_record",
    "persist_manifest",
    "source_hash",
    "supplied_size",
    "total_supplied_size",
    "truncation_record",
]
