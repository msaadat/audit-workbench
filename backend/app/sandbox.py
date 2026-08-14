"""A restricted execution environment for LLM- (or auditor-) authored Polars.

The natural-language assistant writes **visible, editable Python** that runs
on the auditor's own machine. That code is shown in the UI and can be edited
and re-run, so the auditor is always the final reviewer — but we still refuse
the obviously dangerous moves (imports, file/OS access, dunder tunnelling)
so a bad suggestion can't quietly touch the filesystem.

This is a guard-rail, not a security boundary: Python can't be perfectly
sandboxed in-process. It runs locally, under the user who launched the app,
against data that is already on their machine.

Contract: the snippet receives ``pl`` (Polars), every workspace table by name
and via ``tables['name']``, and ``df`` (the first table). It must assign its
output to ``result`` — a Polars DataFrame (a Series or scalar is coerced).
"""

from __future__ import annotations

import ast
import contextlib
import io
import keyword

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


def run(code: str, frames: dict[str, pl.DataFrame]) -> tuple[pl.DataFrame, str]:
    """Execute ``code`` against ``frames``; return (result frame, captured stdout).

    ``frames`` maps table name -> DataFrame. Each is also exposed as a bare
    variable when its name is a valid identifier. Raises :class:`SandboxError`
    on unsafe constructs or a runtime failure.
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
            exec(compiled, namespace)  # noqa: S102 - guarded above; local-only tool
    except SandboxError:
        raise
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
