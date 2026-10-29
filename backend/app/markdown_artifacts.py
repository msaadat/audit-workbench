"""The written work products, read and edited the way a person edits a file.

The memorandum, a finding's narrative and the report are long Markdown
documents. A caller that can only replace one whole cannot safely edit it: it
has to restate tens of thousands of characters it was shown a slice of, and
whatever it did not see it drops. These functions give a caller what an editor
gives a person instead — find the passage, read the lines around it, replace an
exact span — and refuse the edits that would go wrong quietly: a span that is
not there, one that occurs twice, a document that changed since it was read,
and an edit that adds or removes a section the template defines.

Every read reports the document's ``sha1``; every edit must name the one it was
made against.
"""

from __future__ import annotations

import hashlib
import re

from . import markdown_sections
from .workspaces import Workspace, WorkspaceError

#: The documents an edit may change, and the read-only one beside them.
EDITABLE_KINDS = ("planning", "finding", "report")
READABLE_KINDS = (*EDITABLE_KINDS, "analysis_summary")
#: A read window, in characters of numbered text: below the loop's per-result
#: cap, so a window is never cut in the middle by the caller.
READ_CHARACTERS = 4_500
SEARCH_LINE_CHARACTERS = 240
SEARCH_LIMIT = 20


def text_sha1(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()


def _split(ref: str) -> tuple[str, str]:
    kind, separator, item_id = str(ref or "").strip().partition(":")
    if not separator or not item_id:
        raise WorkspaceError(
            "Name a document as planning:apm, finding:<id>, report:working or "
            "analysis_summary:current."
        )
    return kind, item_id


def document_text(workspace: Workspace, ref: str) -> tuple[str, str]:
    """The document's current Markdown and a label to show for it."""

    kind, item_id = _split(ref)
    if kind == "planning" and item_id == "apm":
        return str(workspace.planning.get("apm_markdown") or ""), "Audit planning memorandum"
    if kind == "finding":
        finding = next(
            (item for item in workspace.findings if str(item.get("id")) == item_id), None
        )
        if finding is None:
            raise WorkspaceError(f"Finding '{item_id}' was not found.")
        return str(finding.get("narrative") or ""), str(finding.get("title") or item_id)
    if kind == "report" and item_id in ("working", "current"):
        from . import report

        return str(report.hydrate(workspace).get("markdown") or ""), "Report working draft"
    if kind == "analysis_summary" and item_id == "current":
        return str((workspace.analysis_summary or {}).get("markdown") or ""), "Analysis memo"
    raise WorkspaceError(f"'{ref}' is not a document that can be read this way.")


def outline(markdown: str) -> list[dict]:
    """Every heading down to level three, with the line it starts on."""

    return [
        {"line": index + 1, "level": level, "heading": text}
        for index, level, text in markdown_sections.headings(markdown, max_level=3)
    ]


def search(
    workspace: Workspace,
    ref: str,
    pattern: str,
    *,
    regex: bool = False,
    limit: int = SEARCH_LIMIT,
) -> dict:
    """Lines matching ``pattern``, each with its number and its section."""

    text, label = document_text(workspace, ref)
    pattern = str(pattern or "")
    if not pattern.strip():
        raise WorkspaceError("Say what to search for.")
    try:
        matcher = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE)
    except re.error as error:
        raise WorkspaceError(f"That pattern is not a valid regular expression: {error}.") from error
    lines = text.splitlines()
    matches = []
    total = 0
    for index, line in enumerate(lines):
        if not matcher.search(line):
            continue
        total += 1
        if len(matches) < max(1, int(limit)):
            matches.append(
                {
                    "line": index + 1,
                    "section": markdown_sections.section_at(text, index),
                    "text": line[:SEARCH_LINE_CHARACTERS],
                }
            )
    return {
        "ref": ref,
        "document": label,
        "sha1": text_sha1(text),
        "total_lines": len(lines),
        "matches": matches,
        "match_count": total,
        "shown": len(matches),
    }


def read(
    workspace: Workspace,
    ref: str,
    *,
    section: str | None = None,
    offset: int = 1,
) -> dict:
    """A numbered window of the document: one section, or lines from ``offset``.

    Bounded by characters rather than lines, and always says where it stopped,
    so a caller reads a long document in windows instead of being handed a
    truncated one that looks whole.
    """

    text, label = document_text(workspace, ref)
    lines = text.splitlines()
    first, last = max(1, int(offset or 1)), len(lines)
    if section:
        wanted = markdown_sections.heading_key(section)
        match = next(
            (
                item
                for item in markdown_sections.sections(text)
                if markdown_sections.heading_key(item.heading) == wanted
            ),
            None,
        )
        if match is None:
            raise WorkspaceError(
                f"The document has no section '{section}'. Sections: "
                + "; ".join(markdown_sections.section_headings(text))
            )
        first, last = match.start + 1, match.end
    shown: list[str] = []
    used = 0
    line_number = first
    while line_number <= last:
        rendered = f"{line_number}\t{lines[line_number - 1]}"
        if shown and used + len(rendered) + 1 > READ_CHARACTERS:
            break
        shown.append(rendered[:READ_CHARACTERS])
        used += len(rendered) + 1
        line_number += 1
    payload = {
        "ref": ref,
        "document": label,
        "sha1": text_sha1(text),
        "total_lines": len(lines),
        "from_line": first,
        "to_line": line_number - 1,
        "text": "\n".join(shown),
    }
    if line_number <= last:
        payload["next_offset"] = line_number
    if first == 1 and not section:
        payload["outline"] = outline(text)
    return payload


def edited_text(text: str, old: str, new: str) -> str:
    """``text`` with the single occurrence of ``old`` replaced by ``new``."""

    old = str(old or "")
    if not old:
        raise WorkspaceError("old_str is empty; copy the exact text to replace.")
    if old == str(new or ""):
        raise WorkspaceError("old_str and new_str are the same; there is nothing to change.")
    count = text.count(old)
    if count == 0:
        raise WorkspaceError(
            "old_str does not occur in the document. Copy it exactly from "
            "read_document, without the line numbers."
        )
    if count > 1:
        raise WorkspaceError(
            f"old_str occurs {count} times; include more of the surrounding text "
            "so it names one place."
        )
    revised = text.replace(old, str(new or ""), 1)
    if markdown_sections.section_headings(revised) != markdown_sections.section_headings(text):
        raise WorkspaceError(
            "That edit adds, removes or renames a section heading. The sections "
            "are set by the template; change the text inside them instead."
        )
    return revised


def edit(
    workspace: Workspace,
    ref: str,
    old: str,
    new: str,
    *,
    expected_sha1: str,
    run_id: str | None = None,
) -> str:
    """Replace one exact span of an editable document and save it."""

    kind, item_id = _split(ref)
    if kind not in EDITABLE_KINDS:
        raise WorkspaceError(f"'{ref}' cannot be edited.")
    text, _label = document_text(workspace, ref)
    if str(expected_sha1 or "") != text_sha1(text):
        raise WorkspaceError(
            "The document changed since it was read. Read it again and make the "
            "edit against what it says now."
        )
    revised = edited_text(text, old, new)
    if kind == "planning":
        # Ownership is left alone: an edit made on the auditor's behalf does not
        # make their memorandum the agent's, and theirs is what protects it.
        workspace.update_planning(
            {"apm_markdown": revised, "agent_run_id": run_id}, agent=True
        )
    elif kind == "finding":
        from . import findings

        findings.update(workspace, item_id, {"narrative": revised})
    else:
        from . import report

        report.update(workspace, {"markdown": revised})
    return revised


__all__ = [
    "EDITABLE_KINDS",
    "READABLE_KINDS",
    "document_text",
    "edit",
    "edited_text",
    "outline",
    "read",
    "search",
    "text_sha1",
]
