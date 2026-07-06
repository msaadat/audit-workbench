"""Typed tabular file reading with a signature-based in-memory cache.

Unlike a validation tool (where everything is read as text and rules cast),
the workbench wants *typed* frames: numeric columns must aggregate, date
columns must truncate. Types are inferred on read; the profiler and query
engine handle the remaining ambiguity per column.

The cache is keyed by (resolved path, size, mtime), so editing or replacing
a file invalidates its entry automatically. Files can be 100MB+ so avoiding
repeated parses matters more than the memory of keeping frames around.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

SUPPORTED_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xlsm", ".xls")

_cache: dict[tuple, pl.DataFrame] = {}


def _signature(path: Path) -> tuple:
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def clear_cache(path: Path | None = None) -> None:
    """Drop cached frames — all of them, or just the ones for ``path``."""
    if path is None:
        _cache.clear()
        return
    key = str(path.resolve())
    for cached in [k for k in _cache if k[0] == key]:
        _cache.pop(cached, None)


def read_table(path: Path) -> pl.DataFrame:
    """Read a CSV/TSV/Excel file into a typed DataFrame, cached by signature."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported file type: {path.suffix}")

    sig = _signature(path)
    if sig in _cache:
        return _cache[sig]

    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        df = pl.read_csv(
            path,
            separator="\t" if suffix == ".tsv" else ",",
            try_parse_dates=True,
            infer_schema_length=10_000,
            ignore_errors=True,
        )
    else:
        df = pl.read_excel(path)

    # Drop stale entries for the same file before caching the new read.
    clear_cache(path)
    _cache[sig] = df
    return df
