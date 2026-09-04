"""Shipped Markdown templates with per-workspace overrides.

A template's ``##`` headings are its section contract. Artifacts that are
authored as free Markdown against a template — the finding narrative — are
checked for completeness against those headings rather than against a field
list in Python, so a firm that renames or reorders its sections moves the gate
with them. The parsing helpers below are that shared vocabulary; they are kept
here rather than in a consumer so ``findings`` and ``report`` can both use them
without importing each other.
"""

from __future__ import annotations

import re
from pathlib import Path

from .workspaces import Workspace, WorkspaceError

# Guidance lives in HTML comments so it reaches the model and the auditor
# without ever reaching the artifact. Comments are stripped before any heading
# is parsed: a comment may legitimately contain a line that looks like one.
_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

TEMPLATE_NAMES = (
    "apm",
    "rcm",
    # The attribute rules, split out of ``rcm`` when the matrix turn split into
    # a rows pass and an attributes pass. Its own name so a firm that
    # customised those rules can still override exactly them, and so the pass
    # that writes attributes carries only the guidance for the fields it writes.
    "rcm_attributes",
    "interview",
    "workpaper",
    "report",
    "finding",
)
DEFAULTS_DIR = Path(__file__).resolve().parent / "templates"


def _name(name: str) -> str:
    value = str(name or "").strip().lower().removesuffix(".md")
    if value not in TEMPLATE_NAMES:
        raise WorkspaceError(f"Unknown template '{name}'.")
    return value


def override_path(workspace: Workspace, name: str) -> Path:
    return workspace.root / "Templates" / f"{_name(name)}.md"


def get_template(workspace: Workspace, name: str) -> dict:
    template_name = _name(name)
    override = override_path(workspace, template_name)
    default = DEFAULTS_DIR / f"{template_name}.md"
    path = override if override.exists() else default
    if not path.exists():
        raise WorkspaceError(f"Default template '{template_name}' is unavailable.")
    return {
        "name": template_name,
        "markdown": path.read_text(encoding="utf-8"),
        "source": "workspace" if override.exists() else "default",
    }


def put_template(workspace: Workspace, name: str, markdown: str | None, reset: bool = False) -> dict:
    template_name = _name(name)
    path = override_path(workspace, template_name)
    if reset or markdown is None:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return get_template(workspace, template_name)
    content = str(markdown)
    if not content.strip():
        raise WorkspaceError("Template Markdown cannot be empty; reset it instead.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return get_template(workspace, template_name)


def strip_guidance(markdown: str) -> str:
    """The Markdown without its authoring comments."""
    return _COMMENT_PATTERN.sub("", str(markdown or ""))


def section_key(heading: str) -> str:
    """The comparison key for one section heading."""
    return " ".join(str(heading or "").split()).casefold()


def sections(markdown: str) -> list[str]:
    """The ``##`` headings of a template or artifact, in declared order."""
    return _SECTION_PATTERN.findall(strip_guidance(markdown))


def section_bodies(markdown: str) -> dict[str, str]:
    """Map each ``##`` section key to its body text, guidance removed.

    A heading repeated in the same document keeps its first body: an artifact
    with two sections of one name has one section as far as completeness goes.
    """
    text = strip_guidance(markdown)
    matches = list(_SECTION_PATTERN.finditer(text))
    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        bodies.setdefault(section_key(match.group(1)), text[match.end():end].strip())
    return bodies


def scaffold(markdown: str) -> str:
    """An empty artifact carrying the template's headings and nothing else.

    The template's own title and guidance are dropped: the result is what an
    auditor starts typing into, and what the finding narrative is seeded with.
    """
    return "\n\n".join(f"## {heading}" for heading in sections(markdown)) + "\n"
