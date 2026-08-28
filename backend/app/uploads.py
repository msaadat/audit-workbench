"""Bounded upload reading and per-user storage quota.

Every upload route reads a whole file into memory before handing it to the
loader or the document store.  On a single-user desktop that is merely
wasteful; on a shared server it is a way to exhaust the box with one request,
so reads are capped and refused rather than truncated.

The quota is opt-in.  When ``WORKBENCH_USER_QUOTA_MB`` is unset nothing walks
the filesystem and this costs nothing, which keeps a local installation exactly
as fast as it was.
"""

from __future__ import annotations

import os
from pathlib import Path

from .workspaces import WorkspaceError

# Generous by intent: audit populations of 100MB+ are ordinary here, so the cap
# exists to stop a runaway or hostile request, not to police normal work.
DEFAULT_MAX_UPLOAD_MB = 512
_CHUNK = 1 << 20


def _megabytes(name: str, default: int | None) -> int | None:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return value if value > 0 else None


def max_upload_bytes() -> int:
    return (_megabytes("WORKBENCH_MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB)
            or DEFAULT_MAX_UPLOAD_MB) * 1024 * 1024


def quota_bytes() -> int | None:
    """The per-user storage ceiling, or ``None`` when unlimited (the default)."""
    configured = _megabytes("WORKBENCH_USER_QUOTA_MB", None)
    return configured * 1024 * 1024 if configured else None


def describe_bytes(value: int) -> str:
    megabytes = value / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} GB"
    return f"{megabytes:.0f} MB"


async def read_upload(file, *, limit: int | None = None) -> bytes:
    """Read an ``UploadFile`` fully, refusing anything past the cap.

    Read in chunks so an oversized upload is rejected partway rather than after
    the whole thing is already resident.
    """
    cap = limit if limit is not None else max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            name = getattr(file, "filename", None) or "upload"
            raise WorkspaceError(
                f"'{name}' is larger than the {describe_bytes(cap)} upload limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def directory_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            # A file removed mid-walk is not an error worth failing an upload for.
            continue
    return total


def usage_for(owner_id: str) -> int:
    from . import registry

    return directory_bytes(registry.user_home(owner_id))


def check_quota(owner_id: str, incoming: int) -> None:
    """Refuse a write that would take this user past their ceiling."""
    ceiling = quota_bytes()
    if ceiling is None:
        return
    used = usage_for(owner_id)
    if used + incoming > ceiling:
        raise WorkspaceError(
            f"This upload would exceed your {describe_bytes(ceiling)} storage "
            f"limit ({describe_bytes(used)} already in use). Delete data you no "
            "longer need, or ask an administrator to raise the limit."
        )
