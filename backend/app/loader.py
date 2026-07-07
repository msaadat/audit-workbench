"""Typed tabular file reading with a signature-based in-memory cache.

Unlike a validation tool (where everything is read as text and rules cast),
the workbench wants *typed* frames: numeric columns must aggregate, date
columns must truncate. Types are inferred on read; the profiler and query
engine handle the remaining ambiguity per column.

The cache is keyed by (resolved path, size, mtime), so editing or replacing
a file invalidates its entry automatically. Files can be 100MB+ so avoiding
repeated parses matters more than the memory of keeping frames around.

Excel reads also get a parquet sidecar in a ``.cache`` folder next to the
source file: header-detection plus multi-sheet consolidation is real work,
and unlike the in-memory cache it survives process restarts. The sidecar's
filename embeds the file signature, so a changed workbook simply misses and
the stale file is swept away once the fresh one is written.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import polars as pl

SUPPORTED_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xlsm", ".xls")
EXCEL_HEADER_SCAN_ROWS = 50
CACHE_DIRNAME = ".cache"

_cache: dict[tuple, pl.DataFrame] = {}


@dataclass(frozen=True)
class _ExcelSheetFrame:
    name: str
    frame: pl.DataFrame
    header_row: int


def _signature(path: Path) -> tuple:
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def file_signature(path: Path) -> tuple:
    """Public signature for a source file, for callers that cache alongside it."""
    return _signature(Path(path))


def _parquet_cache_path(path: Path, sig: tuple) -> Path:
    _, size, mtime_ns = sig
    return path.parent / CACHE_DIRNAME / f"{path.stem}.{size}-{mtime_ns}.parquet"


def _clear_parquet_cache(path: Path, keep: Path | None = None) -> None:
    cache_dir = path.parent / CACHE_DIRNAME
    if not cache_dir.exists():
        return
    for stale in cache_dir.glob(f"{path.stem}.*.parquet"):
        if stale != keep:
            stale.unlink(missing_ok=True)


def _clear_memory_cache(path: Path) -> None:
    key = str(path.resolve())
    for cached in [k for k in _cache if k[0] == key]:
        _cache.pop(cached, None)


def clear_cache(path: Path | None = None) -> None:
    """Drop cached frames (memory and on-disk parquet) — all, or just ``path``."""
    if path is None:
        _cache.clear()
        return
    _clear_memory_cache(path)
    _clear_parquet_cache(path)


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_numeric_text(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\(?\$?\d[\d,]*(?:\.\d+)?%?\)?", value))


def _is_date_text(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", value))


def _is_label_text(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 100
        and not _is_numeric_text(value)
        and not _is_date_text(value)
        and bool(re.search(r"[A-Za-z_]", value))
    )


def _filled_positions(row: tuple[object, ...]) -> list[int]:
    return [index for index, value in enumerate(row) if _cell_text(value)]


def _header_row_score(rows: list[tuple[object, ...]], index: int) -> float:
    row = rows[index]
    positions = _filled_positions(row)
    if len(positions) < 2:
        return -1

    values = [_cell_text(row[position]) for position in positions]
    normalized = [re.sub(r"\s+", " ", value).casefold() for value in values]
    unique_ratio = len(set(normalized)) / len(normalized)
    label_ratio = sum(1 for value in values if _is_label_text(value)) / len(values)

    minimum_width = max(2, math.ceil(len(positions) * 0.5))
    supported_rows: list[tuple[object, ...]] = []
    blank_run = 0
    for following in rows[index + 1 : index + 11]:
        if not _filled_positions(following):
            blank_run += 1
            if blank_run >= 2:
                break
            continue
        blank_run = 0
        if len(_filled_positions(following)) >= minimum_width:
            supported_rows.append(following)

    if not supported_rows:
        return -1

    data_like_cells = 0
    checked_cells = 0
    for following in supported_rows[:5]:
        for position in positions:
            if position >= len(following):
                continue
            value = _cell_text(following[position])
            if not value:
                continue
            checked_cells += 1
            if _is_numeric_text(value) or _is_date_text(value) or not _is_label_text(value):
                data_like_cells += 1
    data_ratio = data_like_cells / checked_cells if checked_cells else 0

    previous_empty = index == 0 or not _filled_positions(rows[index - 1])
    return (
        len(positions) * 10
        + len(supported_rows) * 8
        + unique_ratio * 10
        + label_ratio * 8
        + data_ratio * 6
        + (4 if previous_empty else 0)
        - index * 0.05
    )


def _detect_header_row_from_sample(sample: pl.DataFrame) -> int:
    rows = sample.rows()
    if not rows:
        return 0

    best_index = 0
    best_score = -1.0
    for index in range(min(len(rows), EXCEL_HEADER_SCAN_ROWS)):
        score = _header_row_score(rows, index)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index if best_score >= 0 else 0


def _sample_excel_sheet(path: Path, sheet_name: str | None = None) -> pl.DataFrame:
    options = {
        "read_options": {"header_row": None, "n_rows": EXCEL_HEADER_SCAN_ROWS},
        "drop_empty_rows": False,
        "drop_empty_cols": False,
        "raise_if_empty": False,
    }
    if sheet_name is not None:
        return pl.read_excel(path, sheet_name=sheet_name, **options)
    return pl.read_excel(path, **options)


def _detect_excel_header_row(path: Path, sheet_name: str | None = None) -> int:
    sample = _sample_excel_sheet(path, sheet_name)
    return _detect_header_row_from_sample(sample)


def _read_excel_sheet(path: Path, sheet_name: str | None = None) -> _ExcelSheetFrame:
    header_row = _detect_excel_header_row(path, sheet_name)
    options = {
        "read_options": {"header_row": header_row},
        "raise_if_empty": False,
    }
    if sheet_name is not None:
        frame = pl.read_excel(path, sheet_name=sheet_name, **options)
        name = sheet_name
    else:
        frame = pl.read_excel(path, **options)
        name = ""
    return _ExcelSheetFrame(name=name, frame=frame, header_row=header_row)


def _read_all_excel_sheets(path: Path) -> list[_ExcelSheetFrame]:
    samples = pl.read_excel(
        path,
        sheet_id=0,
        read_options={"header_row": None, "n_rows": EXCEL_HEADER_SCAN_ROWS},
        drop_empty_rows=False,
        drop_empty_cols=False,
        raise_if_empty=False,
    )
    if not isinstance(samples, dict):
        return [_read_excel_sheet(path)]
    return [_read_excel_sheet(path, sheet_name) for sheet_name in samples]


def _consolidate_excel_sheets(sheets: list[_ExcelSheetFrame]) -> pl.DataFrame:
    """Merge workbook sheets that represent the same detected table shape."""
    non_empty = [sheet for sheet in sheets if sheet.frame.width > 0]
    if not non_empty:
        return sheets[0].frame if sheets else pl.DataFrame()

    groups: dict[tuple[str, ...], list[_ExcelSheetFrame]] = {}
    for sheet in non_empty:
        groups.setdefault(tuple(sheet.frame.columns), []).append(sheet)

    first_key = tuple(non_empty[0].frame.columns)
    if len(groups[first_key]) > 1:
        parts = groups[first_key]
    else:
        repeated_groups = [group for group in groups.values() if len(group) > 1]
        if not repeated_groups:
            return non_empty[0].frame
        parts = max(repeated_groups, key=lambda group: (len(group), sum(s.frame.height for s in group)))

    return pl.concat([sheet.frame for sheet in parts], how="vertical_relaxed")


def _read_excel(path: Path) -> pl.DataFrame:
    sheets = _read_all_excel_sheets(path)
    if not sheets:
        return pl.DataFrame()
    return _consolidate_excel_sheets(sheets)


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
        parquet_cache = _parquet_cache_path(path, sig)
        if parquet_cache.exists():
            df = pl.read_parquet(parquet_cache)
        else:
            df = _read_excel(path)
            try:
                parquet_cache.parent.mkdir(parents=True, exist_ok=True)
                df.write_parquet(parquet_cache)
                _clear_parquet_cache(path, keep=parquet_cache)
            except OSError:
                pass  # best-effort: an unwritable cache dir shouldn't break reads

    # Drop stale in-memory entries for the same file before caching the new read.
    _clear_memory_cache(path)
    _cache[sig] = df
    return df
