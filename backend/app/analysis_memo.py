"""The analysis memo's embed grammar — one definition, several readers.

The memo is Markdown carrying fenced ``embed`` directives that name a saved
analysis to render in place. Three components need that grammar and none may
import the others: the worker that writes it (which cannot reach a workspace),
the context adapter that hands it to the APM (which cannot reach a worker), and
the frontend renderer. A drifted copy would not fail loudly — it would silently
render a directive as stray text — so the parser and the flattener live here,
in a module with no dependencies at all.

The frontend keeps its own implementation in ``MemoView.vue`` because it must
run in the browser; that one is held to this grammar by test, not by import.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# An embedded result:
#
#     ```embed
#     analysis: A-0DAB063C
#     as: exception_table
#     caption: the backdated invoice
#     ```
#
# A fenced block rather than a JSON block list because the memo has to survive
# being read as plain text — in the APM, in a report, in an export — and a
# fence degrades to something legible rather than to broken markup.
EMBED_FENCE = "embed"
EMBED_KINDS: tuple[str, ...] = ("chart", "summary_table", "exception_table", "stats")

EMBED_BLOCK = re.compile(
    r"^```embed[ \t]*\n(.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_EMBED_FIELD = re.compile(r"^([a-z_]+)\s*:\s*(.*)$")


def parse_embeds(markdown: str) -> list[dict[str, str]]:
    """Extract the embed directives from a memo, in document order."""
    embeds: list[dict[str, str]] = []
    for match in EMBED_BLOCK.finditer(markdown or ""):
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            field = _EMBED_FIELD.match(line.strip())
            if field:
                fields[field.group(1)] = field.group(2).strip()
        embeds.append(fields)
    return embeds


def flatten_embeds(markdown: str, titles: Mapping[str, str] | None = None) -> str:
    """Replace every embed directive with a readable inline citation.

    A consumer that cannot resolve embeds — the APM, a report, an export — must
    not be handed the raw directive: its renderer would print the fence as
    stray text and the reference would be worse than absent. Flattening keeps
    the citation as prose, so the reader still learns which procedure the
    statement rests on.
    """
    names = dict(titles or {})

    def replace(match: re.Match[str]) -> str:
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            field = _EMBED_FIELD.match(line.strip())
            if field:
                fields[field.group(1)] = field.group(2).strip()
        analysis_id = fields.get("analysis") or ""
        if not analysis_id:
            return ""
        title = names.get(analysis_id)
        label = f"{title} ({analysis_id})" if title else analysis_id
        caption = fields.get("caption")
        return f"_See analysis {label}{f' — {caption}' if caption else ''}._"

    flattened = EMBED_BLOCK.sub(replace, markdown or "")
    # Collapse the blank-line runs the removed fences leave behind.
    return re.sub(r"\n{3,}", "\n\n", flattened).strip()


__all__ = [
    "EMBED_BLOCK",
    "EMBED_FENCE",
    "EMBED_KINDS",
    "flatten_embeds",
    "parse_embeds",
]
