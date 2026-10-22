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
    run.  POSIX through ``resource``; Windows through a job object, which has no
    output-size limit.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        if sys.platform == "win32":
            apply_windows_limits(limits)
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


# Held for the life of the process: closing the last handle to a job does not
# release the process from it, but there is no reason to find out.
_JOB_HANDLE = None


def apply_windows_limits(limits: dict) -> None:  # pragma: no cover - Windows
    """Cap memory and CPU time by placing this process in a job object.

    The job also allows one active process. Windows subprocess mode has no
    namespace around it, so anything the snippet managed to spawn would run
    outside every other limit here. A failure leaves the process unlimited,
    as a refused ``setrlimit`` does on POSIX.
    """
    global _JOB_HANDLE
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    limit_process_time = 0x0002
    limit_active_process = 0x0008
    limit_process_memory = 0x0100
    limit_die_on_unhandled_exception = 0x0400
    extended_limit_information = 9

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    info = ExtendedLimits()
    flags = limit_active_process | limit_die_on_unhandled_exception
    info.BasicLimitInformation.ActiveProcessLimit = 1
    if limits.get("cpu_seconds"):
        flags |= limit_process_time
        # In 100-nanosecond ticks.
        info.BasicLimitInformation.PerProcessUserTimeLimit = int(limits["cpu_seconds"]) * 10_000_000
    if limits.get("address_space_bytes"):
        flags |= limit_process_memory
        info.ProcessMemoryLimit = int(limits["address_space_bytes"])
    info.BasicLimitInformation.LimitFlags = flags

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    if not kernel32.SetInformationJobObject(
        job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return
    if kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        _JOB_HANDLE = job


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
