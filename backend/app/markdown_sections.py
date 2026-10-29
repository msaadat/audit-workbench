"""The section structure of a written work product, read from its Markdown.

The memorandum, a finding's narrative and the report are documents whose
shape is their ``#``/``##`` headings — the template defines them, completeness
is checked against them, and the report numbers them. Anything that edits one
of these documents in parts needs the same answer to "where does this section
start and end", and needs to know when an edit has changed the shape rather
than the words. This module is that answer. It is pure text: no workspace, no
model, so a worker's validator, an executor's commit and a loop tool can all
read a document the same way.

A *section* is a heading at level one or two and the lines under it, to the
next heading at level one or two. Deeper headings belong to the section they
sit in. Lines inside fenced code blocks are never headings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
#: The deepest heading that starts a section rather than sitting inside one.
SECTION_LEVEL = 2


@dataclass(frozen=True)
class Section:
    heading: str
    level: int
    #: Zero-based index of the heading line.
    start: int
    #: Zero-based index one past the section's last line.
    end: int


def heading_key(heading: str) -> str:
    """How two headings are compared: case and spacing do not make a new one."""

    return " ".join(str(heading or "").split()).casefold()


def headings(markdown: str, *, max_level: int = 6) -> list[tuple[int, int, str]]:
    """Every heading as ``(line_index, level, text)``, outside code fences."""

    found: list[tuple[int, int, str]] = []
    fenced = False
    for index, line in enumerate(str(markdown or "").splitlines()):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _HEADING.match(line)
        if match and len(match.group(1)) <= max_level:
            found.append((index, len(match.group(1)), match.group(2).strip()))
    return found


def sections(markdown: str) -> list[Section]:
    """The document's sections, in order."""

    lines = str(markdown or "").splitlines()
    starts = headings(markdown, max_level=SECTION_LEVEL)
    result = []
    for position, (index, level, text) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        result.append(Section(text, level, index, end))
    return result


def section_headings(markdown: str) -> list[str]:
    """The document's shape: its section headings, in order."""

    return [section.heading for section in sections(markdown)]


def section_at(markdown: str, line_index: int) -> str:
    """The heading of the section a line sits in, or ``""`` before the first."""

    current = ""
    for section in sections(markdown):
        if section.start <= line_index:
            current = section.heading
        else:
            break
    return current


def section_body(markdown: str, heading: str) -> str | None:
    """One section's body, without its heading line, or ``None`` if absent."""

    lines = str(markdown or "").splitlines()
    wanted = heading_key(heading)
    for section in sections(markdown):
        if heading_key(section.heading) == wanted:
            return "\n".join(lines[section.start + 1 : section.end]).strip("\n")
    return None


def breaks_structure(body: str) -> bool:
    """Whether a section body would start a section of its own."""

    return bool(headings(body, max_level=SECTION_LEVEL))


def replace_bodies(markdown: str, bodies: dict[str, str]) -> str:
    """Rewrite the named sections' bodies, leaving every other line as it was.

    Headings are matched by :func:`heading_key`. Each heading line is kept
    verbatim; only the lines under it change. A heading named here that the
    document does not have is an error, not a silent no-op.
    """

    lines = str(markdown or "").splitlines()
    wanted = {heading_key(key): str(value or "").strip("\n") for key, value in bodies.items()}
    found = {heading_key(section.heading) for section in sections(markdown)}
    unknown = sorted(set(wanted) - found)
    if unknown:
        raise ValueError(f"the document has no section '{unknown[0]}'")
    output: list[str] = []
    cursor = 0
    for section in sections(markdown):
        key = heading_key(section.heading)
        if key not in wanted:
            continue
        output.extend(lines[cursor : section.start + 1])
        body = wanted.pop(key)
        output.append("")
        if body:
            output.extend(body.splitlines())
            output.append("")
        cursor = section.end
    output.extend(lines[cursor:])
    text = "\n".join(output)
    return text + "\n" if str(markdown or "").endswith("\n") else text


def numbered(markdown: str, start: int = 1, limit: int = 200) -> list[str]:
    """Lines ``start``..``start+limit-1`` (one-based), each prefixed by its number."""

    lines = str(markdown or "").splitlines()
    first = max(1, int(start))
    chosen = lines[first - 1 : first - 1 + max(1, int(limit))]
    return [f"{first + offset}\t{line}" for offset, line in enumerate(chosen)]


__all__ = [
    "SECTION_LEVEL",
    "Section",
    "breaks_structure",
    "heading_key",
    "headings",
    "numbered",
    "replace_bodies",
    "section_at",
    "section_body",
    "section_headings",
    "sections",
]
