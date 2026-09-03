"""Read-only projections kept between requests, keyed by what could change them.

The engagement record and the engagement status are pure functions of the
files under a workspace. Each was recomputed from those files on every request
— the whole readiness sweep, the whole document-test register — and the shell
asks for both when a workspace opens and again after every commit, several
requests at once, each slowing the others under one interpreter lock.

The key is the workspace itself: every file under its root by path, size and
modification time. Every artifact writer goes through ``write_json_atomic``,
which replaces the file rather than rewriting it in place, and a replaced file
carries a new size or time. The manifest is one of those files, so the
revision is in the key without being read. Nothing here has to be told about
a write, which is the property the request-scoped caches this sits above
could not have: they read files outside the manifest and had to die with the
call. This one reads the same files and dies with the first change to any.

Reads are single-flight. Two requests that miss on the same key wait on one
computation rather than each starting their own, which is what turns the
shell's fan-out from three slow answers into one.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

#: Files whose churn says nothing about a projection: the telemetry database
#: and its journal are written on every request, including the one asking.
_IGNORED_PREFIXES = ("telemetry.db",)

#: Enough for every open workspace's handful of projections. A payload is a
#: few tens of kilobytes; the bound is against a long-lived process browsing
#: many workspaces, not against size.
MAX_ENTRIES = 64

_entries: "OrderedDict[tuple[str, str], tuple[str, object]]" = OrderedDict()
_entries_lock = threading.Lock()
_inflight: dict[tuple[str, str], threading.Lock] = {}


def workspace_signature(root: Path | str) -> str:
    """One hash over every file under ``root``: its path, size and mtime.

    A directory walk with the metadata the listing already carries — on
    Windows ``DirEntry.stat`` costs no further call — so a three-thousand-file
    engagement signs in well under a tenth of a second, against the seconds
    the projections it guards take to draw.
    """
    digest = hashlib.sha1()
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    if entry.name.startswith(_IGNORED_PREFIXES):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        # A file that vanished between the listing and the
                        # stat is a write in progress; its absence signs too.
                        digest.update(f"{entry.path}|gone\n".encode("utf-8"))
                        continue
                    digest.update(
                        f"{entry.path}|{info.st_size}|{info.st_mtime_ns}\n".encode("utf-8")
                    )
        except OSError:
            digest.update(f"{current}|unreadable\n".encode("utf-8"))
    return digest.hexdigest()


def cached(root: Path | str, name: str, compute: Callable[[], T]) -> T:
    """``compute()``'s value for the workspace at ``root`` as it stands now.

    Answered from memory while nothing under ``root`` has changed since it was
    computed; computed once, by the first of any concurrent callers, otherwise.
    The value handed back is shared: a caller must treat it as read-only.

    The signature is taken before computing and the value is filed under it.
    A write that lands while the projection is being drawn leaves a value that
    may describe either side of it, filed under a signature the write has
    already invalidated — so the next caller draws again rather than reading
    a projection that might straddle a change.
    """
    key = (str(root), name)
    signature = workspace_signature(root)
    with _entries_lock:
        hit = _entries.get(key)
        if hit is not None and hit[0] == signature:
            _entries.move_to_end(key)
            return hit[1]  # type: ignore[return-value]
        gate = _inflight.setdefault(key, threading.Lock())
    with gate:
        with _entries_lock:
            hit = _entries.get(key)
            if hit is not None and hit[0] == signature:
                _entries.move_to_end(key)
                return hit[1]  # type: ignore[return-value]
        value = compute()
        with _entries_lock:
            _entries[key] = (signature, value)
            _entries.move_to_end(key)
            while len(_entries) > MAX_ENTRIES:
                _entries.popitem(last=False)
        return value


def clear() -> None:
    """Forget every cached projection. For tests, and for a process that
    knows it is about to read a workspace it did not write."""
    with _entries_lock:
        _entries.clear()
