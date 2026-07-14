"""Shipped Markdown templates with per-workspace overrides."""

from __future__ import annotations

from pathlib import Path

from .workspaces import Workspace, WorkspaceError

TEMPLATE_NAMES = ("apm", "rcm", "interview", "workpaper", "report")
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
