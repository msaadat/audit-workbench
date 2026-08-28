"""A restricted execution environment for LLM- (or auditor-) authored Polars.

The natural-language assistant writes **visible, editable Python** that runs
on the auditor's own machine. That code is shown in the UI and can be edited
and re-run, so the auditor is always the final reviewer — but we still refuse
the obviously dangerous moves (imports, file/OS access, dunder tunnelling)
so a bad suggestion can't quietly touch the filesystem.

This is a guard-rail, not a security boundary: Python can't be perfectly
sandboxed in-process. That premise held while the app ran locally, under the
user who launched it, against data already on their machine.

On a shared server it does not. An escape reads every other tenant's audit data
and the administrator's provider credentials in the environment, so execution is
refused in multi-user mode unless an operator opts back in explicitly. See
:func:`execution_allowed`.

Contract: the snippet receives ``pl`` (Polars), every workspace table by name
and via ``tables['name']``, and ``df`` (the first table). It must assign its
output to ``result`` — a Polars DataFrame (a Series or scalar is coerced).
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import keyword
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import polars as pl

MAX_CODE_CHARS = 20_000
MAX_STDOUT_CHARS = 4_000

# Names that would escape the guard-rail if reachable.
_DENIED_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "input", "__import__", "breakpoint",
        "globals", "locals", "vars", "getattr", "setattr", "delattr",
        "memoryview", "help", "exit", "quit", "classmethod", "staticmethod",
    }
)

# A deliberately small builtin surface — enough for real data wrangling.
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "int", "len", "list", "map", "max", "min",
    "print", "range", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "zip",
)

# Polars can reach the filesystem without Python's ``open`` builtin.  Keep the
# advertised in-memory contract honest by rejecting its I/O entry points too.
_DENIED_ATTRIBUTE_PREFIXES = ("read_", "scan_", "write_", "sink_")
_DENIED_ATTRIBUTES = frozenset({"serialize", "deserialize"})


class SandboxError(ValueError):
    """A user-facing problem with the snippet (unsafe construct or runtime error)."""


def _safe_builtins() -> dict:
    import builtins

    allowed = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES}
    allowed.update({"True": True, "False": False, "None": None})
    return allowed


def validate(code: str) -> None:
    """Validate the static sandbox contract without executing the snippet."""
    if len(code) > MAX_CODE_CHARS:
        raise SandboxError("Snippet is too long.")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as error:
        raise SandboxError(f"Syntax error: {error.msg} (line {error.lineno}).") from error

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SandboxError("Imports are not allowed — `pl` (Polars) is already available.")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise SandboxError("`global` / `nonlocal` are not allowed.")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SandboxError(f"Access to '{node.attr}' is not allowed.")
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith(_DENIED_ATTRIBUTE_PREFIXES)
            or node.attr in _DENIED_ATTRIBUTES
        ):
            raise SandboxError(
                "File I/O is not allowed — use the in-memory workspace table variables."
            )
        if isinstance(node, ast.Name) and node.id in _DENIED_NAMES:
            raise SandboxError(f"Use of '{node.id}' is not allowed.")

    if not any(
        isinstance(node, ast.Name)
        and node.id == "result"
        and isinstance(node.ctx, ast.Store)
        for node in ast.walk(tree)
    ):
        raise SandboxError(
            "Snippet finished without assigning `result`. Assign your output "
            "DataFrame to a variable named `result`."
        )


def _coerce_result(value) -> pl.DataFrame:
    if isinstance(value, pl.DataFrame):
        return value
    if isinstance(value, pl.LazyFrame):
        # Models often write .lazy() pipelines and forget .collect().
        return value.collect()
    if isinstance(value, pl.Series):
        return value.to_frame()
    if isinstance(value, dict):
        return pl.DataFrame(value)
    # A scalar: present it as a single-cell frame, keeping JSON-safe primitives.
    if value is None or isinstance(value, (int, float, bool, str)):
        return pl.DataFrame({"result": [value]})
    return pl.DataFrame({"result": [str(value)]})


def execute_locally(code: str, frames: dict[str, pl.DataFrame]) -> tuple[pl.DataFrame, str]:
    """Run ``code`` in *this* process. The guard-rail, with no isolation.

    Called directly only in single-user mode, where the snippet already runs
    under the account that owns the data.  Everywhere else this is what the
    isolated child process calls, on the other side of the boundary.
    """
    validate(code)

    namespace: dict = {"__builtins__": _safe_builtins(), "pl": pl}
    namespace["tables"] = dict(frames)
    for name, frame in frames.items():
        if name.isidentifier() and not keyword.iskeyword(name):
            namespace[name] = frame
    if frames:
        namespace["df"] = next(iter(frames.values()))

    stdout = io.StringIO()
    try:
        compiled = compile(code, "<assistant>", "exec")
        with contextlib.redirect_stdout(stdout):
            exec(compiled, namespace)  # noqa: S102 - guarded above and isolated by `run`
    except SandboxError:
        raise
    except MemoryError as error:
        # Carries no message of its own, and "MemoryError: " tells the reader
        # nothing about what to do next.
        raise SandboxError(
            "The snippet ran out of memory. Narrow the rows or columns it "
            "materialises."
        ) from error
    except RecursionError as error:
        raise SandboxError("The snippet recursed too deeply.") from error
    except Exception as error:  # surface the runtime error to the user/model
        raise SandboxError(f"{type(error).__name__}: {error}") from error

    if "result" not in namespace:
        raise SandboxError(
            "Snippet finished without assigning `result`. Assign your output "
            "DataFrame to a variable named `result`."
        )

    captured = stdout.getvalue()
    if len(captured) > MAX_STDOUT_CHARS:
        captured = captured[:MAX_STDOUT_CHARS] + "\n… (output truncated)"
    return _coerce_result(namespace["result"]), captured


def referenced_frames(code: str, available: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    """The subset of ``available`` a snippet could actually reach.

    Only relevant to the isolated path, where every frame handed over is
    serialised and copied.  Sending a whole workspace when the snippet names one
    table would turn a cheap call into hundreds of megabytes of I/O.

    Conservative by construction: anything that touches ``tables`` in a way this
    cannot resolve statically — iteration, a computed key, passing it along —
    gets the full set rather than a confusing ``KeyError``.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return dict(available)

    # ``tables`` reached through a literal string key is resolvable; every other
    # mention of it is not, and forfeits the narrowing.
    resolved_subscripts: set[int] = set()
    named: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        target, index = node.value, node.slice
        if not (isinstance(target, ast.Name) and target.id == "tables"):
            continue
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            resolved_subscripts.add(id(target))
            if index.value in available:
                named.add(index.value)

    uses_df = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name):
            continue
        if node.id in available:
            named.add(node.id)
        elif node.id == "df":
            uses_df = True
        elif node.id == "tables" and id(node) not in resolved_subscripts:
            return dict(available)

    if uses_df and available:
        # ``df`` binds to the first frame handed over, so the first frame of the
        # full set has to stay in the narrowed set — and stay first. The
        # comprehension below preserves ``available``'s order, which is what
        # keeps ``df`` bound to the same frame it would be in-process.
        named.add(next(iter(available)))
    return {name: frame for name, frame in available.items() if name in named}


ALLOW_ENV_VAR = "WORKBENCH_ALLOW_INPROCESS_PYTHON"
MODE_ENV_VAR = "WORKBENCH_SANDBOX_MODE"

MODE_INPROCESS = "inprocess"
MODE_SUBPROCESS = "subprocess"
MODE_CONTAINER = "container"

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MEMORY_MB = 2048
_EXCHANGE_PREFIX = "aw-sandbox-"


def _int_env(name: str, default: int) -> int:
    try:
        value = int(float(str(os.environ.get(name) or "").strip() or default))
    except ValueError:
        return default
    return value if value > 0 else default


def timeout_seconds() -> int:
    return _int_env("WORKBENCH_SANDBOX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)


def memory_bytes() -> int:
    return _int_env("WORKBENCH_SANDBOX_MEMORY_MB", DEFAULT_MEMORY_MB) * 1024 * 1024


def bubblewrap_path() -> str | None:
    """The ``bwrap`` binary, if this machine has one.

    Bubblewrap is what turns "another process" into an actual boundary: a
    private mount namespace holding only the interpreter, Polars, and the
    exchange directory, plus no network at all.  It is a system package rather
    than a Python dependency, so it may simply be absent.
    """
    override = str(os.environ.get("WORKBENCH_BWRAP") or "").strip()
    if override:
        return override if Path(override).exists() else None
    return shutil.which("bwrap")


def isolation_mode() -> str:
    """How the next snippet will run.

    ``container`` is the only mode that isolates one auditor's files from
    another's.  ``subprocess`` bounds memory, CPU, and blast radius, and hides
    the provider credentials, but the child still runs as the same OS user and
    can read what that user can read.  ``inprocess`` is the original guard-rail
    and no boundary at all.
    """
    from . import auth

    configured = str(os.environ.get(MODE_ENV_VAR) or "").strip().lower()
    if configured in {MODE_INPROCESS, MODE_SUBPROCESS, MODE_CONTAINER}:
        if configured == MODE_CONTAINER and bubblewrap_path() is None:
            raise SandboxError(
                f"{MODE_ENV_VAR}=container, but bubblewrap (bwrap) is not "
                "installed on this machine."
            )
        return configured
    if auth.single_user_mode():
        # The snippet runs under the account that owns the data, which is the
        # threat model the guard-rail was written for. Keep it fast.
        return MODE_INPROCESS
    return MODE_CONTAINER if bubblewrap_path() else MODE_SUBPROCESS


def execution_allowed() -> bool:
    """Whether a snippet may run at all.

    Multi-user needs a real boundary.  ``container`` is one; a bare subprocess
    is not, because the child shares the server's UID and can read the ``.env``
    the parent's environment was scrubbed of.  An operator whose users are all
    trusted with each other's data can still opt in explicitly.
    """
    from . import auth

    if auth.single_user_mode():
        return True
    if str(os.environ.get(ALLOW_ENV_VAR) or "").strip().lower() in {"1", "true", "yes"}:
        return True
    try:
        return isolation_mode() == MODE_CONTAINER
    except SandboxError:
        return False


def require_execution() -> None:
    if not execution_allowed():
        raise SandboxError(
            "Running Python is disabled on this server. Isolating a snippet from "
            "other auditors' data needs bubblewrap (bwrap), which is not "
            "installed. Install it, or set WORKBENCH_ALLOW_INPROCESS_PYTHON=1 if "
            "every user here is trusted with everyone else's data."
        )


def _child_environment() -> dict[str, str]:
    """A deliberately bare environment.

    The provider credentials live in this process's environment, so the child
    is given a new one rather than a filtered copy — a filter has to be right
    about every future variable, an allow-list only about the few that matter.
    """
    site_packages = str(Path(pl.__file__).resolve().parent.parent)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": os.pathsep.join([str(_APP_PARENT), site_packages]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        # Polars sizes its thread pool from the visible CPU count; a snippet
        # should not be able to claim the whole box.
        "POLARS_MAX_THREADS": str(_int_env("WORKBENCH_SANDBOX_THREADS", 2)),
    }


_APP_DIR = Path(__file__).resolve().parent
_APP_PARENT = _APP_DIR.parent


def _bwrap_command(exchange: Path, interpreter: str) -> list[str]:
    """Build the jail: read-only interpreter and Polars, one writable directory.

    Nothing else is mounted.  The workspace tree, other users' homes, and the
    ``.env`` holding the provider keys are simply not present in the child's
    filesystem, which is what makes this a boundary rather than a convention.
    """
    bwrap = bubblewrap_path()
    assert bwrap is not None
    site_packages = Path(pl.__file__).resolve().parent.parent

    command = [
        bwrap,
        # No network, no IPC, no PIDs, no user namespace sharing.
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    # System libraries, following whatever shape this distribution uses.
    for path in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        source = Path(path)
        if not source.exists():
            continue
        if source.is_symlink():
            command += ["--symlink", os.readlink(source), path]
        else:
            command += ["--ro-bind", path, path]

    seen: set[str] = set()
    for source in (Path(sys.base_prefix), site_packages, _APP_DIR):
        resolved = str(Path(source).resolve())
        if resolved in seen or resolved.startswith(("/usr/", "/lib/", "/bin/")):
            continue
        seen.add(resolved)
        # The app package is mounted under a synthetic root so that the rest of
        # the repository — including the dotenv files — stays invisible.
        destination = "/sandbox/app" if source == _APP_DIR else resolved
        command += ["--ro-bind", resolved, destination]

    command += [
        "--bind", str(exchange), str(exchange),
        "--chdir", str(exchange),
        # Without this the child inherits the parent's whole environment and the
        # --setenv flags below merely add to it — which would hand the snippet
        # the provider credentials this jail exists to keep from it.
        "--clearenv",
    ]
    for key, value in _child_environment().items():
        command += ["--setenv", key, value]
    command += ["--setenv", "PYTHONPATH",
                os.pathsep.join(["/sandbox", str(site_packages)])]
    return command + [interpreter, "-m", "app.sandbox_child"]


def _interpreter() -> str:
    """The real interpreter, not the venv shim.

    A venv launcher finds its site-packages through ``pyvenv.cfg`` beside it,
    which is not mounted in the jail; the child is told where Polars lives
    through ``PYTHONPATH`` instead.
    """
    return os.path.realpath(sys.executable)


def run_isolated(code: str, frames: dict[str, pl.DataFrame]) -> tuple[pl.DataFrame, str]:
    """Execute a snippet in a short-lived child and return its result.

    Frames go in and the result comes back as Arrow files in a private exchange
    directory rather than over a pipe: a population of a few hundred megabytes
    would otherwise have to fit through a pipe buffer while both processes hold
    a copy of it.
    """
    validate(code)
    mode = isolation_mode()
    narrowed = referenced_frames(code, frames)
    exchange = Path(tempfile.mkdtemp(prefix=_EXCHANGE_PREFIX))
    try:
        manifest: dict[str, str] = {}
        for index, (name, frame) in enumerate(narrowed.items()):
            filename = f"frame{index}.arrow"
            frame.write_ipc(exchange / filename)
            manifest[name] = filename
        (exchange / "request.json").write_text(
            json.dumps({
                "code": code,
                "frames": manifest,
                "limits": {
                    "address_space_bytes": memory_bytes(),
                    "cpu_seconds": timeout_seconds(),
                    "file_size_bytes": memory_bytes(),
                },
            }),
            encoding="utf-8",
        )

        interpreter = _interpreter()
        if mode == MODE_CONTAINER:
            command = _bwrap_command(exchange, interpreter)
            env = {}
        else:
            command = [interpreter, "-m", "app.sandbox_child"]
            env = _child_environment()

        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                command,
                cwd=str(exchange),
                env=env or None,
                capture_output=True,
                timeout=timeout_seconds(),
            )
        except subprocess.TimeoutExpired as error:
            raise SandboxError(
                f"The snippet ran longer than {timeout_seconds()}s and was stopped."
            ) from error

        response_path = exchange / "response.json"
        if not response_path.exists():
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            # A child killed by the kernel — OOM or CPU limit — never gets to
            # write a response, so the exit signal is all there is to report.
            if completed.returncode and completed.returncode < 0:
                raise SandboxError(
                    "The snippet exceeded its memory or CPU limit and was stopped."
                )
            raise SandboxError(
                f"The sandbox did not run: {detail[-400:] or 'no output'}"
            )
        payload = json.loads(response_path.read_text(encoding="utf-8"))
        if not payload.get("ok"):
            raise SandboxError(str(payload.get("error") or "The snippet failed."))
        return pl.read_ipc(exchange / str(payload["result"])), str(payload.get("stdout") or "")
    finally:
        shutil.rmtree(exchange, ignore_errors=True)


def run(code: str, frames: dict[str, pl.DataFrame]) -> tuple[pl.DataFrame, str]:
    """Execute ``code`` against ``frames``; return (result frame, captured stdout).

    ``frames`` maps table name -> DataFrame. Each is also exposed as a bare
    variable when its name is a valid identifier. Raises :class:`SandboxError`
    on unsafe constructs, a runtime failure, or when execution is disabled.
    """
    require_execution()
    if isolation_mode() == MODE_INPROCESS:
        return execute_locally(code, frames)
    return run_isolated(code, frames)


def empty_frame_dtype(dtype: str):
    """Map a compact context dtype into a safe, useful empty-frame dtype."""
    normalized = str(dtype or "").casefold().replace(" ", "")
    if normalized.startswith("uint"):
        return pl.UInt64
    if normalized.startswith("int"):
        return pl.Int64
    if normalized.startswith(("float", "decimal")):
        return pl.Float64
    if normalized in {"bool", "boolean"}:
        return pl.Boolean
    if normalized == "date":
        return pl.Date
    if normalized.startswith("datetime"):
        return pl.Datetime
    if normalized == "time":
        return pl.Time
    return pl.String


def empty_schema_frames(tables) -> dict:
    """Zero-row frames preserving the supplied table schemas only.

    Running a candidate step against these is how a stage learns that its code
    is not merely *safe* but *runnable* — that every name resolves and every
    column exists — without reading or exposing a single row.

    It lives here rather than beside either caller because two stages must
    decide runnability identically. Test generation validated steps this way
    from the start; analysis promotion did not, and shipped four steps that
    passed the static safety check, referenced a frame variable that does not
    exist, and failed at execution with the audit already believing them
    written. Same reason ``relevance_tokens`` is shared rather than duplicated.
    """
    return {
        table: pl.DataFrame(
            schema={
                column: empty_frame_dtype(dtype) for column, dtype in columns.items()
            }
        )
        for table, columns in tables.items()
    }
