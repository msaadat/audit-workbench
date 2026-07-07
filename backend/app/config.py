"""Application configuration helpers.

Loads simple dotenv files with the standard library so the portable build does
not need an extra dependency. Existing process environment variables win over
values in files.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_FILES = (
    PROJECT_ROOT / ".env",
    PROJECT_ROOT / "backend" / ".env",
)


def load_env_files() -> None:
    for path in DOTENV_FILES:
        if path.exists():
            _load_env_file(path)


def _load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def _parse_env_line(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[len("export "):].lstrip()
    if "=" not in text:
        return None

    key, value = text.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None

    return key, _clean_env_value(value.strip())


def _clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]

    result: list[str] = []
    quote: str | None = None
    escaped = False
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            result.append(char)
            continue
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
            result.append(char)
            continue
        if char == "#" and quote is None:
            break
        result.append(char)
    return "".join(result).strip()


load_env_files()
