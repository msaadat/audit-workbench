"""Workspace model and storage.

A workspace is the unit of an engagement: a folder holding the auditor's data
files plus a JSON definition of the tables and joins built on them::

    Workspaces/<id>/
        workspace.json   ← meta + table entries + join definitions
        Data/            ← the uploaded data files

Base tables map 1:1 to files; joins are named, derived tables that can
reference base tables or other joins. Frames are resolved lazily through
:mod:`.loader`, so nothing is parsed until a tab actually needs data.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import keyword
import os
import re
import shutil
import time
import tokenize
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from . import config  # noqa: F401  # load .env before reading WORKBENCH_DATA
from . import loader, profiler

SCHEMA_VERSION = 1
JOIN_TYPES = ("inner", "left", "full", "semi", "anti", "cross")

WORKSPACES_DIR = Path(
    os.environ.get("WORKBENCH_DATA", "")
    or Path(__file__).resolve().parents[2] / "Workspaces"
)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "item"


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON via a temp file + rename so a crash mid-write can never
    leave a truncated definition behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Windows can briefly deny a replace while antivirus/indexing software or
    # a concurrent API read has the destination open. Retrying preserves the
    # atomic-write guarantee without surfacing a spurious engagement error.
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.02 * (attempt + 1))


# Provenance keys accepted on saved items (tiles/analyses/rulesets/joins).
# An item carrying agent_run_id was created by an agent run; semantic_id is a
# stable slug the agent uses to reconcile reruns instead of duplicating work.
def _apply_provenance(item: dict, payload: dict) -> dict:
    if payload.get("agent_run_id"):
        item["agent_run_id"] = str(payload["agent_run_id"])
        item["created_by"] = "agent"
    if payload.get("semantic_id"):
        item["semantic_id"] = str(payload["semantic_id"])
    return item


def _user_touch(item: dict) -> None:
    """A manual edit of an agent-created item makes it user-owned: reruns of
    the agent must no longer update or replace it."""
    if item.get("created_by") == "agent":
        item["created_by"] = "user"


class WorkspaceError(ValueError):
    """A user-facing workspace problem (bad name, missing table, bad join)."""


def _validate_test_refs(workspace: "Workspace", refs: object) -> list[str]:
    values = [str(ref) for ref in (refs or [])]
    from . import doc_tests
    for ref in values:
        kind, separator, item_id = ref.partition(":")
        if kind == "doctest" and (
            not separator or not item_id or not doc_tests.exists(workspace, item_id)
        ):
            raise WorkspaceError(f"Document test reference '{ref}' does not exist.")
    return values


def _normalized_table_name(text: str) -> str:
    if not str(text or "").strip():
        return ""
    return slugify(text).replace("-", "_")


def _is_bare_table_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _python_reference_token_positions(
    code: str, old: str, new: str
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Find exact table-name references in saved Python without reparsing text.

    The string positions cover explicit ``tables["old"]`` and
    ``tables.get("old")`` lookups. Bare-variable positions are only returned
    when the old name is used as an injected table variable, not assigned inside
    the snippet; that avoids changing a user's local alias.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return set(), set()

    string_positions: set[tuple[int, int]] = set()
    bare_positions: set[tuple[int, int]] = set()
    assigned_old = False

    def is_tables_name(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "tables"

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id == old and isinstance(node.ctx, (ast.Store, ast.Del)):
                assigned_old = True
            continue
        if isinstance(node, ast.arg) and node.arg == old:
            assigned_old = True
            continue
        if isinstance(node, ast.ExceptHandler) and node.name == old:
            assigned_old = True
            continue
        if isinstance(node, ast.Subscript) and is_tables_name(node.value):
            if isinstance(node.slice, ast.Constant) and node.slice.value == old:
                string_positions.add((node.slice.lineno, node.slice.col_offset))
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and is_tables_name(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == old
        ):
            string_positions.add((node.args[0].lineno, node.args[0].col_offset))

    if (
        _is_bare_table_identifier(old)
        and _is_bare_table_identifier(new)
        and not assigned_old
    ):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and node.id == old
                and isinstance(node.ctx, ast.Load)
            ):
                bare_positions.add((node.lineno, node.col_offset))

    return string_positions, bare_positions


def _rewrite_python_table_references(code: str, old: str, new: str) -> tuple[str, bool]:
    if not code or old == new:
        return code, False

    string_positions, bare_positions = _python_reference_token_positions(code, old, new)
    if not string_positions and not bare_positions:
        return code, False

    changed = False
    rewritten = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        for token in tokens:
            value = token.string
            if token.type == tokenize.STRING and token.start in string_positions:
                value = repr(new)
                changed = True
            elif token.type == tokenize.NAME and token.start in bare_positions:
                value = new
                changed = True
            rewritten.append(
                tokenize.TokenInfo(token.type, value, token.start, token.end, token.line)
            )
    except tokenize.TokenError:
        return code, False

    return tokenize.untokenize(rewritten), changed


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.definition_path = self.root / "workspace.json"
        definition = json.loads(self.definition_path.read_text(encoding="utf-8"))
        self.id: str = definition.get("id") or self.root.name
        self.name: str = definition.get("name") or self.id
        self.description: str = definition.get("description") or ""
        self.created: str = definition.get("created") or ""
        self.tables: list[dict] = list(definition.get("tables") or [])
        self.joins: list[dict] = list(definition.get("joins") or [])
        self.tiles: list[dict] = list(definition.get("tiles") or [])
        self.analyses: list[dict] = list(definition.get("analyses") or [])
        self.rulesets: list[dict] = list(definition.get("rulesets") or [])
        # Full-audit-cycle records hydrate defensively so every pre-extension
        # workspace remains readable without a migration step.
        self.settings: dict = {
            "doc_llm_optin": False,
            "doc_llm_optin_at": None,
            "doc_pii_masking": False,
            **dict(definition.get("settings") or {}),
        }
        self.documents: list[dict] = list(definition.get("documents") or [])
        self.planning: dict = {
            "status": "draft",
            "context": {
                "objective": "",
                "entity": "",
                "period": "",
                "scope": "",
                "materiality": "",
                "key_contacts": "",
                "background_notes": "",
                "interview_answers": {},
            },
            "apm_markdown": "",
            "created_by": "user",
            "agent_run_id": None,
            "updated": None,
            **dict(definition.get("planning") or {}),
        }
        self.planning["context"] = {
            "objective": "",
            "entity": "",
            "period": "",
            "scope": "",
            "materiality": "",
            "key_contacts": "",
            "background_notes": "",
            "interview_answers": {},
            **dict(self.planning.get("context") or {}),
        }
        self.rcm: list[dict] = list(definition.get("rcm") or [])
        self.work_program: list[dict] = list(definition.get("work_program") or [])
        self.findings: list[dict] = list(definition.get("findings") or [])
        self.dashboard_advice: dict = dict(definition.get("dashboard_advice") or {})
        # Legacy evidence strings remain represented through a typed wrapper;
        # all subsequent writes validate the durable anchor shape.
        from .evidence import normalize_many
        for item in self.work_program:
            item["evidence_refs"] = normalize_many(item.get("evidence_refs") or [])
            item.setdefault("methodology_refs", [])
        for item in self.findings:
            item["evidence_refs"] = normalize_many(item.get("evidence_refs") or [])
            item.setdefault("rcm_refs", [])
            item.setdefault("procedure_refs", [])
            item.setdefault("status", "draft")
            item.setdefault("source", "manual")
        self.report: dict = dict(definition.get("report") or {})

    # ------------------------------------------------------------- persistence
    @property
    def data_dir(self) -> Path:
        return self.root / "Data"

    def save(self) -> None:
        definition = {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created": self.created,
            "tables": self.tables,
            "joins": self.joins,
            "tiles": self.tiles,
            "analyses": self.analyses,
            "rulesets": self.rulesets,
            "settings": self.settings,
            "documents": self.documents,
            "planning": self.planning,
            "rcm": self.rcm,
            "work_program": self.work_program,
            "findings": self.findings,
            "report": self.report,
            "dashboard_advice": self.dashboard_advice,
        }
        write_json_atomic(self.definition_path, definition)

    # ------------------------------------------------------------------ tables
    def table_names(self) -> list[str]:
        return [t["name"] for t in self.tables] + [j["name"] for j in self.joins]

    def _table_entry(self, name: str) -> dict | None:
        return next((t for t in self.tables if t["name"] == name), None)

    def _join_entry(self, name: str) -> dict | None:
        return next((j for j in self.joins if j["name"] == name), None)

    def add_table(self, filename: str, content: bytes) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in loader.SUPPORTED_SUFFIXES:
            raise WorkspaceError(
                f"Unsupported file type '{suffix}'. "
                f"Supported: {', '.join(loader.SUPPORTED_SUFFIXES)}"
            )

        base = slugify(Path(filename).stem).replace("-", "_")
        name = base
        counter = 1
        while name in self.table_names():
            counter += 1
            name = f"{base}_{counter}"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / f"{name}{suffix}"
        target.write_bytes(content)

        # Fail fast on unreadable files: parse once now, before registering.
        try:
            frame = loader.read_table(target)
        except Exception as error:
            target.unlink(missing_ok=True)
            raise WorkspaceError(f"Could not read '{filename}': {error}") from error
        if frame.width == 0:
            target.unlink(missing_ok=True)
            raise WorkspaceError(f"'{filename}' appears to be empty.")

        entry = {"name": name, "file": target.name, "source": filename}
        self.tables.append(entry)
        self.save()
        return entry

    def replace_table(self, name: str, filename: str, content: bytes) -> dict:
        """Swap the data behind an existing base table, keeping its ``name``.

        Saved queries/tiles/analyses/joins link to a table by name and recompute
        live, so replacing the file content updates every one of them at once.
        The new file is validated in a temp file first; only a successful parse
        commits the swap, so a bad upload never destroys the existing data.
        """
        entry = self._table_entry(name)
        if entry is None:
            join = self._join_entry(name)
            if join is not None:
                raise WorkspaceError(f"'{name}' is a join, not a data table.")
            raise WorkspaceError(f"No table named '{name}'.")

        suffix = Path(filename).suffix.lower()
        if suffix not in loader.SUPPORTED_SUFFIXES:
            raise WorkspaceError(
                f"Unsupported file type '{suffix}'. "
                f"Supported: {', '.join(loader.SUPPORTED_SUFFIXES)}"
            )

        # Snapshot the current columns for the schema diff (best-effort: the old
        # file may itself be unreadable, in which case there's nothing to diff).
        try:
            old_columns = self.get_frame(name).columns
        except Exception:
            old_columns = []

        # Validate the new content in a temp file before touching the live one.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.data_dir / f".{name}.upload{suffix}"
        tmp.write_bytes(content)
        try:
            frame = loader.read_table(tmp)
        except Exception as error:
            loader.clear_cache(tmp)
            tmp.unlink(missing_ok=True)
            raise WorkspaceError(f"Could not read '{filename}': {error}") from error
        if frame.width == 0:
            loader.clear_cache(tmp)
            tmp.unlink(missing_ok=True)
            raise WorkspaceError(f"'{filename}' appears to be empty.")
        loader.clear_cache(tmp)  # drop the temp path's entry before committing

        old_path = self.data_dir / entry["file"]
        new_path = self.data_dir / f"{name}{suffix}"
        loader.clear_cache(old_path)
        os.replace(tmp, new_path)
        if new_path != old_path:
            # Format changed (e.g. csv → xlsx): retire the old file.
            old_path.unlink(missing_ok=True)
            entry["file"] = new_path.name
        loader.clear_cache(new_path)
        self._clear_profile_cache(name)

        entry["source"] = filename
        self.save()

        new_columns = self.get_frame(name).columns
        return {
            "name": name,
            "file": entry["file"],
            "source": filename,
            "added_columns": [c for c in new_columns if c not in old_columns],
            "removed_columns": [c for c in old_columns if c not in new_columns],
        }

    def rename_table(self, name: str, new_name: str) -> dict:
        """Rename a base table or join and migrate saved references.

        Stored work links by table name, so this updates the workspace metadata
        in one commit: joins that depend on the table, saved dashboard tiles,
        saved analyses, and validation rulesets. Python snippets are edited
        conservatively for exact table lookups and unshadowed bare table names.
        """
        entry = self._table_entry(name)
        join = self._join_entry(name)
        if entry is None and join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        target = _normalized_table_name(new_name)
        if not target:
            raise WorkspaceError("Table name is required.")
        if target == name:
            return {
                "old_name": name,
                "name": target,
                "updated": {
                    "joins": 0,
                    "tiles": 0,
                    "analyses": 0,
                    "rulesets": 0,
                    "python_snippets": 0,
                },
            }
        if target in self.table_names():
            raise WorkspaceError(f"A table named '{target}' already exists.")

        updated = {
            "joins": 0,
            "tiles": 0,
            "analyses": 0,
            "rulesets": 0,
            "python_snippets": 0,
        }

        if entry is not None:
            entry["name"] = target
        else:
            join["name"] = target

        for existing_join in self.joins:
            touched = False
            if existing_join.get("left") == name:
                existing_join["left"] = target
                touched = True
            if existing_join.get("right") == name:
                existing_join["right"] = target
                touched = True
            if touched:
                updated["joins"] += 1

        for collection_name, collection in (
            ("tiles", self.tiles),
            ("analyses", self.analyses),
        ):
            for item in collection:
                touched = False
                if item.get("table") == name:
                    item["table"] = target
                    touched = True
                if item.get("kind") == "python" and isinstance(item.get("spec"), dict):
                    code = str(item["spec"].get("code") or "")
                    rewritten, changed = _rewrite_python_table_references(code, name, target)
                    if changed:
                        item["spec"]["code"] = rewritten
                        updated["python_snippets"] += 1
                        touched = True
                if touched:
                    updated[collection_name] += 1

        for ruleset in self.rulesets:
            if ruleset.get("table") == name:
                ruleset["table"] = target
                updated["rulesets"] += 1

        self._clear_profile_cache(name)
        self._clear_profile_cache(target)
        self.save()
        return {"old_name": name, "name": target, "updated": updated}

    def remove_table(self, name: str) -> None:
        entry = self._table_entry(name)
        join = self._join_entry(name)
        if entry is None and join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        dependents = [
            j["name"]
            for j in self.joins
            if j["name"] != name and name in (j.get("left"), j.get("right"))
        ]
        if dependents:
            raise WorkspaceError(
                f"'{name}' is used by join(s): {', '.join(dependents)}. Remove those first."
            )

        if entry is not None:
            path = self.data_dir / entry["file"]
            if path.exists():
                loader.clear_cache(path)
            path.unlink(missing_ok=True)
            self.tables.remove(entry)
        if join is not None:
            self.joins.remove(join)
        self._clear_profile_cache(name)
        self.save()

    # ------------------------------------------------------------------- joins
    def add_join(self, spec: dict) -> dict:
        name = slugify(spec.get("name") or "").replace("-", "_")
        if not name:
            raise WorkspaceError("Join name is required.")
        if name in self.table_names():
            raise WorkspaceError(f"A table named '{name}' already exists.")

        how = spec.get("how") or "left"
        if how not in JOIN_TYPES:
            raise WorkspaceError(f"Unknown join type '{how}'.")

        left, right = spec.get("left"), spec.get("right")
        for side in (left, right):
            if side not in self.table_names():
                raise WorkspaceError(f"Unknown table '{side}'.")
        if name in (left, right):
            raise WorkspaceError("A join cannot reference itself.")

        left_on = [c for c in (spec.get("left_on") or []) if c]
        right_on = [c for c in (spec.get("right_on") or []) if c]
        if how != "cross":
            if not left_on or len(left_on) != len(right_on):
                raise WorkspaceError("Join keys are required and must pair up.")
            for column, table in [(c, left) for c in left_on] + [
                (c, right) for c in right_on
            ]:
                if column not in self.get_frame(table).columns:
                    raise WorkspaceError(f"Column '{column}' not found in '{table}'.")

        entry = _apply_provenance(
            {
                "name": name,
                "left": left,
                "right": right,
                "how": how,
                "left_on": left_on,
                "right_on": right_on,
            },
            spec,
        )
        # Validate by executing once before persisting.
        self.joins.append(entry)
        try:
            self.get_frame(name)
        except Exception:
            self.joins.remove(entry)
            raise
        self.save()
        return entry

    def remove_join(self, name: str) -> None:
        entry = self._join_entry(name)
        if entry is None:
            raise WorkspaceError(f"No join named '{name}'.")
        self.remove_table(name)

    # ------------------------------------------------------------------- tiles
    # A tile pins a *spec* (a query or an analytics run), never data: the
    # dashboard recomputes tiles on load, so it stays live when files change
    # and every tile is reproducible.
    def add_tile(self, payload: dict) -> dict:
        kind = payload.get("kind")
        if kind not in ("query", "analytics", "python", "pivot", "validation"):
            raise WorkspaceError(
                "Tile kind must be 'query', 'pivot', 'analytics', 'python' or 'validation'."
            )
        table = payload.get("table")
        # Python tiles carry their own code and may reference any table(s), so
        # a bound table is optional (and only used as a label) for them.
        if kind == "python":
            table = table if table in self.table_names() else None
        elif table not in self.table_names():
            raise WorkspaceError(f"Unknown table '{table}'.")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise WorkspaceError("Tile title is required.")
        if kind == "python" and not str((payload.get("spec") or {}).get("code") or "").strip():
            raise WorkspaceError("A Python tile needs code.")

        tile = _apply_provenance(
            {
                "id": uuid.uuid4().hex[:10],
                "title": title,
                "kind": kind,
                "table": table,
                "spec": dict(payload.get("spec") or {}),
                "viz": dict(payload.get("viz") or {"type": "table"}),
                "note": str(payload.get("note") or "").strip(),
                "created": date.today().isoformat(),
            },
            payload,
        )
        self.tiles.append(tile)
        self.save()
        return tile

    def _tile(self, tile_id: str) -> dict:
        tile = next((t for t in self.tiles if t["id"] == tile_id), None)
        if tile is None:
            raise WorkspaceError("Tile not found.")
        return tile

    def update_tile(self, tile_id: str, changes: dict) -> dict:
        tile = self._tile(tile_id)
        _user_touch(tile)
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise WorkspaceError("Tile title is required.")
            tile["title"] = title
        if "note" in changes:
            tile["note"] = str(changes["note"] or "").strip()
        if "viz" in changes and isinstance(changes["viz"], dict):
            tile["viz"] = dict(changes["viz"])
        if "move" in changes:
            step = int(changes["move"])
            index = self.tiles.index(tile)
            target = max(0, min(len(self.tiles) - 1, index + step))
            self.tiles.insert(target, self.tiles.pop(index))
        self.save()
        return tile

    def remove_tile(self, tile_id: str) -> None:
        self.tiles.remove(self._tile(tile_id))
        self.save()

    # --------------------------------------------------------------- analyses
    # A saved analysis is the working-set sibling of a tile: same spec-not-data
    # model (recomputed live), but it lives in the Analysis tab's rail rather
    # than the dashboard. It comes from either the predefined library
    # (kind 'analytics') or AI-assisted code (kind 'python'). Pinning promotes
    # a copy to a dashboard tile; the two collections stay independent.
    def add_analysis(self, payload: dict) -> dict:
        kind = payload.get("kind")
        if kind not in ("analytics", "python"):
            raise WorkspaceError("Analysis kind must be 'analytics' or 'python'.")
        table = payload.get("table")
        # Python analyses carry their own code and may reference any table(s),
        # so a bound table is optional (and only a label) for them.
        if kind == "python":
            table = table if table in self.table_names() else None
        elif table not in self.table_names():
            raise WorkspaceError(f"Unknown table '{table}'.")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise WorkspaceError("Analysis title is required.")
        if kind == "python" and not str((payload.get("spec") or {}).get("code") or "").strip():
            raise WorkspaceError("A Python analysis needs code.")

        analysis = _apply_provenance(
            {
                "id": uuid.uuid4().hex[:10],
                "title": title,
                "kind": kind,
                "table": table,
                "spec": dict(payload.get("spec") or {}),
                "viz": dict(payload.get("viz") or {"type": "table"}),
                "note": str(payload.get("note") or "").strip(),
                "source": payload.get("source") or ("ai" if kind == "python" else "library"),
                "created": date.today().isoformat(),
            },
            payload,
        )
        self.analyses.append(analysis)
        self.save()
        return analysis

    def _analysis(self, analysis_id: str) -> dict:
        analysis = next((a for a in self.analyses if a["id"] == analysis_id), None)
        if analysis is None:
            raise WorkspaceError("Analysis not found.")
        return analysis

    def update_analysis(self, analysis_id: str, changes: dict) -> dict:
        analysis = self._analysis(analysis_id)
        _user_touch(analysis)
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise WorkspaceError("Analysis title is required.")
            analysis["title"] = title
        if "note" in changes:
            analysis["note"] = str(changes["note"] or "").strip()
        if "viz" in changes and isinstance(changes["viz"], dict):
            analysis["viz"] = dict(changes["viz"])
        # Unlike a tile, an analysis is an editing surface: params (library) and
        # code (AI) are re-saved by rewriting its spec.
        if "spec" in changes and isinstance(changes["spec"], dict):
            if analysis["kind"] == "python" and not str(changes["spec"].get("code") or "").strip():
                raise WorkspaceError("A Python analysis needs code.")
            analysis["spec"] = dict(changes["spec"])
        self.save()
        return analysis

    def remove_analysis(self, analysis_id: str) -> None:
        self.analyses.remove(self._analysis(analysis_id))
        self.save()

    # ---------------------------------------------------------------- rulesets
    # A rule set is the validation sibling of an analysis: field-wise checks
    # bound to a table by name, stored as a spec and recomputed live — so a
    # replaced or refreshed table re-validates with the same saved rules.
    def _normalize_rules(self, rules: list) -> list[dict]:
        from . import validation

        normalized = []
        for rule in rules or []:
            check = (rule or {}).get("check")
            meta = validation.CHECKS.get(check)
            if meta is None:
                raise WorkspaceError(f"Unknown check '{check}'.")
            column = str(rule.get("column") or "").strip() or None
            if meta["scope"] == "column" and not column:
                raise WorkspaceError(f"Check '{check}' needs a column.")
            severity = rule.get("severity")
            normalized.append(
                {
                    "id": rule.get("id") or uuid.uuid4().hex[:10],
                    "column": column if meta["scope"] == "column" else None,
                    "check": check,
                    "params": dict(rule.get("params") or {}),
                    "severity": severity if severity in validation.SEVERITIES else "fail",
                    "enabled": rule.get("enabled") is not False,
                }
            )
        return normalized

    def add_ruleset(self, payload: dict) -> dict:
        table = payload.get("table")
        if table not in self.table_names():
            raise WorkspaceError(f"Unknown table '{table}'.")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise WorkspaceError("Rule set title is required.")
        ruleset = _apply_provenance(
            {
                "id": uuid.uuid4().hex[:10],
                "title": title,
                "table": table,
                "rules": self._normalize_rules(payload.get("rules") or []),
                "note": str(payload.get("note") or "").strip(),
                "created": date.today().isoformat(),
            },
            payload,
        )
        self.rulesets.append(ruleset)
        self.save()
        return ruleset

    def _ruleset(self, ruleset_id: str) -> dict:
        ruleset = next((r for r in self.rulesets if r["id"] == ruleset_id), None)
        if ruleset is None:
            raise WorkspaceError("Rule set not found.")
        return ruleset

    def update_ruleset(self, ruleset_id: str, changes: dict) -> dict:
        ruleset = self._ruleset(ruleset_id)
        _user_touch(ruleset)
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise WorkspaceError("Rule set title is required.")
            ruleset["title"] = title
        if "note" in changes:
            ruleset["note"] = str(changes["note"] or "").strip()
        # Rebinding to another table is allowed even when some rule columns
        # don't exist there — missing columns degrade to per-rule errors at
        # run time, which is the point of re-running on evolving data.
        if "table" in changes:
            if changes["table"] not in self.table_names():
                raise WorkspaceError(f"Unknown table '{changes['table']}'.")
            ruleset["table"] = changes["table"]
        if "rules" in changes:
            ruleset["rules"] = self._normalize_rules(changes["rules"])
        self.save()
        return ruleset

    def remove_ruleset(self, ruleset_id: str) -> None:
        self.rulesets.remove(self._ruleset(ruleset_id))
        self.save()

    RUN_HISTORY_MAX = 20

    def record_run(self, ruleset_id: str, run: dict) -> list[dict]:
        """Append a summary-only entry (never row data) to the rule set's run
        history — enough for a 2025-vs-2026 trend without a second store of
        results. Only runs of the *saved* spec are recorded; draft runs are not
        evidence."""
        ruleset = self._ruleset(ruleset_id)
        runs = ruleset.setdefault("runs", [])
        runs.append(
            {
                "run_at": run["run_at"],
                "table": run["table"],
                "rows": run["rows"],
                "verdict": run["verdict"],
                "counts": dict(run["counts"]),
            }
        )
        del runs[: -self.RUN_HISTORY_MAX]
        self.save()
        return runs

    # -------------------------------------------------------------- provenance
    def find_semantic(self, collection: str, semantic_id: str) -> dict | None:
        """Find a saved item by its agent semantic id ('tiles', 'analyses',
        'rulesets', 'joins', 'rcm', or 'procedures'). Used by agent reruns to reconcile instead of
        duplicating outputs."""
        items = {
            "tiles": self.tiles,
            "analyses": self.analyses,
            "rulesets": self.rulesets,
            "joins": self.joins,
            "rcm": self.rcm,
            "procedures": self.work_program,
            "work_program": self.work_program,
            "findings": self.findings,
        }.get(collection, [])
        return next((i for i in items if i.get("semantic_id") == semantic_id), None)

    # --------------------------------------------------------------- planning
    @staticmethod
    def _updated_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def update_planning(self, changes: dict, *, agent: bool = False) -> dict:
        allowed = {"status", "context", "apm_markdown", "agent_run_id", "created_by"}
        unknown = set(changes) - allowed
        if unknown:
            raise WorkspaceError(f"Unknown planning field: {sorted(unknown)[0]}.")
        if not agent and ({"agent_run_id", "created_by"} & set(changes)):
            raise WorkspaceError("Planning provenance is managed by the workbench.")
        if "status" in changes and changes["status"] not in ("draft", "final"):
            raise WorkspaceError("Planning status must be 'draft' or 'final'.")
        if "context" in changes:
            context = changes["context"]
            if not isinstance(context, dict):
                raise WorkspaceError("Planning context must be an object.")
            self.planning["context"].update(context)
        for key in ("status", "apm_markdown", "agent_run_id", "created_by"):
            if key in changes:
                self.planning[key] = changes[key]
        if not agent and self.planning.get("created_by") == "agent":
            self.planning["created_by"] = "user"
        self.planning["updated"] = self._updated_now()
        self.save()
        return self.planning

    def _planning_record(self, collection: list[dict], item_id: str, label: str) -> dict:
        item = next((row for row in collection if row.get("id") == item_id), None)
        if item is None:
            raise WorkspaceError(f"{label} '{item_id}' not found.")
        return item

    def add_rcm(self, payload: dict) -> dict:
        process = str(payload.get("process") or "").strip()
        risk = str(payload.get("risk") or "").strip()
        if not risk:
            raise WorkspaceError("RCM risk is required.")
        item = {
            "id": str(payload.get("id") or f"RCM-{uuid.uuid4().hex[:6].upper()}"),
            "semantic_id": str(payload.get("semantic_id") or f"rcm:{slugify(process)}:{slugify(risk)}"),
            "created_by": "agent" if payload.get("agent_run_id") else "user",
            "agent_run_id": payload.get("agent_run_id"),
            "process": process,
            "risk": risk,
            "risk_rating": str(payload.get("risk_rating") or "medium").lower(),
            "assertion": str(payload.get("assertion") or ""),
            "control": str(payload.get("control") or ""),
            "control_type": str(payload.get("control_type") or ""),
            "test_procedure": str(payload.get("test_procedure") or ""),
            "test_refs": _validate_test_refs(self, payload.get("test_refs") or []),
        }
        if item["risk_rating"] not in ("low", "medium", "high", "critical"):
            raise WorkspaceError("Risk rating must be low, medium, high, or critical.")
        self.rcm.append(item)
        self.save()
        return item

    def update_rcm(self, item_id: str, changes: dict, *, agent: bool = False) -> dict:
        item = self._planning_record(self.rcm, item_id, "RCM row")
        allowed = {"process", "risk", "risk_rating", "assertion", "control", "control_type", "test_procedure", "test_refs"}
        if set(changes) - allowed:
            raise WorkspaceError("Unknown RCM field.")
        if "risk_rating" in changes and changes["risk_rating"] not in ("low", "medium", "high", "critical"):
            raise WorkspaceError("Risk rating must be low, medium, high, or critical.")
        if "test_refs" in changes:
            changes = {**changes, "test_refs": _validate_test_refs(self, changes["test_refs"])}
        for key, value in changes.items():
            item[key] = [str(ref) for ref in value] if key == "test_refs" else str(value or "")
        if not agent:
            _user_touch(item)
        self.save()
        return item

    def remove_rcm(self, item_id: str) -> None:
        item = self._planning_record(self.rcm, item_id, "RCM row")
        self.rcm.remove(item)
        for procedure in self.work_program:
            procedure["rcm_refs"] = [ref for ref in procedure.get("rcm_refs", []) if ref != item_id]
        for finding in self.findings:
            finding["rcm_refs"] = [ref for ref in finding.get("rcm_refs", []) if ref != item_id]
        self.save()

    def add_procedure(self, payload: dict) -> dict:
        from .evidence import normalize_many
        objective = str(payload.get("objective") or "").strip()
        if not objective:
            raise WorkspaceError("Procedure objective is required.")
        item = {
            "id": str(payload.get("id") or f"PROC-{uuid.uuid4().hex[:6].upper()}"),
            "semantic_id": str(payload.get("semantic_id") or f"procedure:{slugify(objective)}"),
            "created_by": "agent" if payload.get("agent_run_id") else "user",
            "agent_run_id": payload.get("agent_run_id"),
            "rcm_refs": [str(ref) for ref in (payload.get("rcm_refs") or [])],
            "objective": objective,
            "criteria": str(payload.get("criteria") or ""),
            "steps": [str(step) for step in (payload.get("steps") or []) if str(step).strip()],
            "method": str(payload.get("method") or ""),
            "expected_evidence": str(payload.get("expected_evidence") or ""),
            "test_refs": _validate_test_refs(self, payload.get("test_refs") or []),
            "evidence_refs": normalize_many(payload.get("evidence_refs") or [], require_hash=True),
            "methodology_refs": list(payload.get("methodology_refs") or []),
            "result_summary": str(payload.get("result_summary") or ""),
            "conclusion": str(payload.get("conclusion") or ""),
            "scope_limitations": str(payload.get("scope_limitations") or ""),
            "updated": self._updated_now(),
        }
        self.work_program.append(item)
        self.save()
        return item

    def update_procedure(self, item_id: str, changes: dict, *, agent: bool = False) -> dict:
        from .evidence import normalize_many
        item = self._planning_record(self.work_program, item_id, "Procedure")
        allowed = {"rcm_refs", "objective", "criteria", "steps", "method", "expected_evidence", "test_refs", "evidence_refs", "methodology_refs", "result_summary", "conclusion", "scope_limitations"}
        if set(changes) - allowed:
            raise WorkspaceError("Unknown procedure field.")
        if "test_refs" in changes:
            changes = {**changes, "test_refs": _validate_test_refs(self, changes["test_refs"])}
        for key, value in changes.items():
            if key in ("rcm_refs", "steps", "test_refs"):
                item[key] = [str(entry) for entry in (value or [])]
            elif key == "evidence_refs":
                item[key] = normalize_many(value or [], require_hash=True)
            elif key == "methodology_refs":
                item[key] = list(value or [])
            else:
                item[key] = str(value or "")
        if not agent:
            _user_touch(item)
        item["updated"] = self._updated_now()
        self.save()
        return item

    def remove_procedure(self, item_id: str) -> None:
        item = self._planning_record(self.work_program, item_id, "Procedure")
        self.work_program.remove(item)
        for finding in self.findings:
            finding["procedure_refs"] = [ref for ref in finding.get("procedure_refs", []) if ref != item_id]
        self.save()

    # ------------------------------------------------------------------ frames
    def get_frame(self, name: str, _seen: frozenset = frozenset()) -> pl.DataFrame:
        if name in _seen:
            raise WorkspaceError(f"Join '{name}' references itself in a cycle.")

        entry = self._table_entry(name)
        if entry is not None:
            return loader.read_table(self.data_dir / entry["file"])

        join = self._join_entry(name)
        if join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        seen = _seen | {name}
        left = self.get_frame(join["left"], seen)
        right = self.get_frame(join["right"], seen)
        if join["how"] == "cross":
            return left.join(right, how="cross")
        return left.join(
            right,
            how=join["how"],
            left_on=join["left_on"],
            right_on=join["right_on"],
            coalesce=True,
        )

    def _table_signature(self, name: str, _seen: frozenset = frozenset()) -> tuple:
        """A hashable fingerprint of a table's content: the source file's
        (size, mtime) for base tables, or the join spec plus both sides'
        signatures, recursively. Used to key the on-disk profile cache."""
        if name in _seen:
            raise WorkspaceError(f"Join '{name}' references itself in a cycle.")

        entry = self._table_entry(name)
        if entry is not None:
            return ("file", loader.file_signature(self.data_dir / entry["file"]))

        join = self._join_entry(name)
        if join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        seen = _seen | {name}
        return (
            "join",
            join["how"],
            tuple(join["left_on"]),
            tuple(join["right_on"]),
            self._table_signature(join["left"], seen),
            self._table_signature(join["right"], seen),
        )

    # ---------------------------------------------------------------- profile
    def _cache_dir(self) -> Path:
        return self.data_dir / loader.CACHE_DIRNAME

    def _profile_cache_path(self, name: str, sig: tuple) -> Path:
        digest = hashlib.sha1(repr(sig).encode()).hexdigest()[:16]
        return self._cache_dir() / f"{name}.{digest}.profile.json"

    def _clear_profile_cache(self, name: str) -> None:
        cache_dir = self._cache_dir()
        if not cache_dir.exists():
            return
        for stale in cache_dir.glob(f"{name}.*.profile.json"):
            stale.unlink(missing_ok=True)

    def get_profile(self, name: str) -> dict:
        """Column/dataset profile for a table, cached on disk by content
        signature — profiling a large frame is expensive and the result
        never changes until the underlying file (or join input) does."""
        sig = self._table_signature(name)
        cache_file = self._profile_cache_path(name, sig)
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        profile = profiler.profile_table(self.get_frame(name))
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(profile), encoding="utf-8")
            for stale in cache_file.parent.glob(f"{name}.*.profile.json"):
                if stale != cache_file:
                    stale.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort: an unwritable cache dir shouldn't break profiling
        return profile

    # ----------------------------------------------------------------- summary
    def summary(self) -> dict:
        tables = []
        for entry in self.tables:
            info = {"name": entry["name"], "kind": "file", "source": entry.get("source", entry["file"])}
            try:
                frame = self.get_frame(entry["name"])
                info.update(rows=frame.height, columns=frame.width, error=None)
            except Exception as error:
                info.update(rows=None, columns=None, error=str(error))
            tables.append(info)
        for join in self.joins:
            info = {
                "name": join["name"],
                "kind": "join",
                "source": f"{join['left']} {join['how']} join {join['right']}",
                "join": join,
            }
            try:
                frame = self.get_frame(join["name"])
                info.update(rows=frame.height, columns=frame.width, error=None)
            except Exception as error:
                info.update(rows=None, columns=None, error=str(error))
            tables.append(info)
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created": self.created,
            "tables": tables,
            "tile_count": len(self.tiles),
            "document_count": len(self.documents),
            "finding_count": len(self.findings),
            "settings": self.settings,
        }


# -------------------------------------------------------------------- registry
def list_workspaces() -> list[dict]:
    if not WORKSPACES_DIR.exists():
        return []
    items = []
    for folder in sorted(WORKSPACES_DIR.iterdir()):
        if not (folder / "workspace.json").exists():
            continue
        try:
            ws = Workspace(folder)
        except Exception:
            continue
        items.append(
            {
                "id": ws.id,
                "name": ws.name,
                "description": ws.description,
                "created": ws.created,
                "table_count": len(ws.tables) + len(ws.joins),
            }
        )
    return items


def load_workspace(workspace_id: str) -> Workspace:
    root = WORKSPACES_DIR / workspace_id
    if not (root / "workspace.json").exists():
        raise WorkspaceError(f"Workspace '{workspace_id}' not found.")
    return Workspace(root)


def create_workspace(
    name: str, description: str = "", doc_llm_optin: bool = False
) -> Workspace:
    name = str(name).strip()
    if not name:
        raise WorkspaceError("Workspace name is required.")
    workspace_id = slugify(name)
    root = WORKSPACES_DIR / workspace_id
    if root.exists():
        raise WorkspaceError(f"A workspace named '{workspace_id}' already exists.")
    (root / "Data").mkdir(parents=True)
    (root / "workspace.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "id": workspace_id,
                "name": name,
                "description": str(description).strip(),
                "created": date.today().isoformat(),
                "settings": {
                    "doc_llm_optin": bool(doc_llm_optin),
                    "doc_llm_optin_at": (
                        datetime.now(timezone.utc).isoformat(timespec="seconds")
                        if doc_llm_optin
                        else None
                    ),
                    "doc_pii_masking": False,
                },
                "tables": [],
                "joins": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return Workspace(root)


def delete_workspace(workspace_id: str) -> None:
    ws = load_workspace(workspace_id)
    for entry in ws.tables:
        path = ws.data_dir / entry["file"]
        if path.exists():
            loader.clear_cache(path)
    shutil.rmtree(ws.root, ignore_errors=True)
