"""The isolated side of the sandbox: one snippet, one process, then gone.

Started by :mod:`.sandbox` with a working directory containing ``request.json``
and the Arrow frames the snippet referenced.  It writes ``response.json`` (and
``result.arrow`` on success) back into the same directory and exits.

Nothing here trusts the snippet.  Resource limits are applied *before* the
frames are read, so a snippet that allocates without bound is killed by the
kernel rather than by anything this process has to notice.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

REQUEST_NAME = "request.json"
RESPONSE_NAME = "response.json"
RESULT_NAME = "result.arrow"


def apply_limits(limits: dict) -> None:
    """Cap address space, CPU seconds, output size, and descriptors.

    Applied in the child rather than through ``preexec_fn`` because that hook
    runs between fork and exec in a process that may hold locks from any of the
    parent's threads — and this application runs one daemon thread per agent
    run.  POSIX only; on a platform without ``resource`` the process isolation
    and the environment scrub still stand.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return

    def cap(what: int, value: int) -> None:
        try:
            soft, hard = resource.getrlimit(what)
            ceiling = value if hard in (resource.RLIM_INFINITY, -1) else min(value, hard)
            resource.setrlimit(what, (ceiling, ceiling))
        except (ValueError, OSError):
            pass

    if limits.get("address_space_bytes"):
        cap(resource.RLIMIT_AS, int(limits["address_space_bytes"]))
    if limits.get("cpu_seconds"):
        cap(resource.RLIMIT_CPU, int(limits["cpu_seconds"]))
    if limits.get("file_size_bytes"):
        cap(resource.RLIMIT_FSIZE, int(limits["file_size_bytes"]))
    # No snippet has any business forking.
    cap(resource.RLIMIT_NPROC, 0) if limits.get("no_fork") else None


def main() -> int:
    workdir = Path.cwd()
    response_path = workdir / RESPONSE_NAME
    try:
        request = json.loads((workdir / REQUEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        response_path.write_text(
            json.dumps({"ok": False, "error": f"Unreadable sandbox request: {error}"}),
            encoding="utf-8",
        )
        return 1

    apply_limits(request.get("limits") or {})

    try:
        import polars as pl

        from app.sandbox import execute_locally

        frames = {
            name: pl.read_ipc(workdir / filename)
            for name, filename in (request.get("frames") or {}).items()
        }
        result, stdout = execute_locally(str(request.get("code") or ""), frames)
        result.write_ipc(workdir / RESULT_NAME)
        payload = {"ok": True, "stdout": stdout, "result": RESULT_NAME}
    except MemoryError:
        payload = {"ok": False, "error": "The snippet ran out of memory."}
    except BaseException as error:  # noqa: BLE001 - the boundary reports everything
        from app.sandbox import SandboxError

        if isinstance(error, SandboxError):
            payload = {"ok": False, "error": str(error)}
        else:
            payload = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(limit=5),
            }

    try:
        response_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return 1
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
