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
import tokenize
import uuid
from datetime import date
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


class WorkspaceError(ValueError):
    """A user-facing workspace problem (bad name, missing table, bad join)."""


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
        }
        self.definition_path.write_text(
            json.dumps(definition, indent=2), encoding="utf-8"
        )

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

        entry = {
            "name": name,
            "left": left,
            "right": right,
            "how": how,
            "left_on": left_on,
            "right_on": right_on,
        }
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

        tile = {
            "id": uuid.uuid4().hex[:10],
            "title": title,
            "kind": kind,
            "table": table,
            "spec": dict(payload.get("spec") or {}),
            "viz": dict(payload.get("viz") or {"type": "table"}),
            "note": str(payload.get("note") or "").strip(),
            "created": date.today().isoformat(),
        }
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

        analysis = {
            "id": uuid.uuid4().hex[:10],
            "title": title,
            "kind": kind,
            "table": table,
            "spec": dict(payload.get("spec") or {}),
            "viz": dict(payload.get("viz") or {"type": "table"}),
            "note": str(payload.get("note") or "").strip(),
            "source": payload.get("source") or ("ai" if kind == "python" else "library"),
            "created": date.today().isoformat(),
        }
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
        ruleset = {
            "id": uuid.uuid4().hex[:10],
            "title": title,
            "table": table,
            "rules": self._normalize_rules(payload.get("rules") or []),
            "note": str(payload.get("note") or "").strip(),
            "created": date.today().isoformat(),
        }
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


def create_workspace(name: str, description: str = "") -> Workspace:
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
