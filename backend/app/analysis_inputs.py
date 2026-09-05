"""Reproducible, analysis-owned inputs. Recipes persist; aligned rows do not.

A read-only Workspace view lets existing Polars services consume an alignment
without adding anything to the engagement's user-visible join collection.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping

import polars as pl

from . import profiler
from .workspaces import Workspace, WorkspaceError, join_suffix

MAX_INPUT_JOINS = 3


def validate_input(workspace: Workspace, raw: Mapping) -> dict:
    """Validate an ordered recipe against actual sources, never model claims."""
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise WorkspaceError("Unsupported analysis input recipe.")
    root = str(raw.get("root") or "")
    base = {t["name"] for t in workspace.tables}
    if root not in base:
        raise WorkspaceError(f"Unknown analysis input root '{root}'.")
    items = raw.get("joins")
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_INPUT_JOINS:
        raise WorkspaceError("An analysis input requires one to three ordered joins.")
    available = set(base)
    used = {root}
    previous = root
    normalized = []
    for item in items:
        if not isinstance(item, Mapping):
            raise WorkspaceError("Invalid analysis input join.")
        j = {k: str(item.get(k) or "") for k in ("name", "left", "right", "how")}
        j.update({k: list(item.get(k) or []) for k in ("left_on", "right_on")})
        if (j["left"] != previous or j["right"] not in base or j["right"] in used
                or j["how"] != "left" or not j["name"] or j["name"] in available
                or not j["left_on"] or len(j["left_on"]) != len(j["right_on"])
                or any(not isinstance(c, str) for c in j["left_on"] + j["right_on"])):
            raise WorkspaceError("Analysis inputs require distinct sources and ordered left joins.")
        normalized.append(j)
        available.add(j["name"])
        used.add(j["right"])
        previous = j["name"]
    if raw.get("name") != previous:
        raise WorkspaceError("Analysis input name does not match its final alignment.")
    recipe = {"version": 1, "name": previous, "root": root, "joins": normalized}
    # Validate schemas and uniqueness locally. A changed key cannot silently
    # turn one transaction into several rows on rerun.
    view = InputWorkspace(workspace, [recipe], validated=True)
    try:
        for j in normalized:
            left = view.get_frame(j["left"])
            right = workspace.get_frame(j["right"])
            if (not set(j["left_on"]) <= set(left.columns)
                    or not set(j["right_on"]) <= set(right.columns)):
                raise WorkspaceError("Analysis input references a missing column.")
            present = right.filter(pl.all_horizontal(
                [pl.col(c).is_not_null() for c in j["right_on"]]
            ))
            if present.select(j["right_on"]).unique().height != present.height:
                raise WorkspaceError("Analysis input lookup keys are no longer unique.")
            left.lazy().join(
                right.lazy(), how="left", left_on=j["left_on"], right_on=j["right_on"],
                coalesce=True,
                suffix=join_suffix(left.columns, right.columns, j["right_on"], j["right"]),
            ).collect_schema()
    except pl.exceptions.PolarsError as error:
        raise WorkspaceError(f"Invalid analysis input schema: {error}") from error
    return recipe


class InputWorkspace(Workspace):
    """Read-only local view; saving it would leak proposed joins into the record."""
    def __init__(self, workspace: Workspace, recipes=(), *, validated=False):
        self.__dict__.update(workspace.__dict__)
        self._table_signature_cache = {}
        self._input_frames = {}
        self._input_profiles = {}
        self._input_recipes = dict(getattr(workspace, "_input_recipes", {}))
        self.joins = copy.deepcopy(workspace.joins)
        known = {j["name"]: j for j in self.joins}
        for raw in recipes:
            recipe = dict(raw) if validated else validate_input(workspace, raw)
            self._input_recipes[recipe["name"]] = recipe
            for j in recipe["joins"]:
                if j["name"] in known and known[j["name"]] != j:
                    raise WorkspaceError("Conflicting analysis input names.")
                if j["name"] not in known:
                    self.joins.append(dict(j))
                    known[j["name"]] = j

    def save(self, *args, **kwargs):
        raise WorkspaceError("Analysis input views cannot be saved.")

    def get_frame(self, name, _seen=frozenset()):
        if name not in self._input_frames:
            self._input_frames[name] = super().get_frame(name, _seen)
        return self._input_frames[name]

    def get_profile(self, name):
        if name not in self._input_profiles:
            self._input_profiles[name] = profiler.profile_table(self.get_frame(name))
        return self._input_profiles[name]


def for_analysis(workspace: Workspace, analysis: Mapping, *, validate=True) -> Workspace:
    raw = analysis.get("alignment")
    if not raw:
        return workspace
    recipe = validate_input(workspace, raw) if validate else raw
    if analysis.get("table") != recipe["name"]:
        raise WorkspaceError("Analysis table and input recipe disagree.")
    return InputWorkspace(workspace, [recipe], validated=True)


def source_refs(recipe: Mapping) -> tuple[str, ...]:
    return tuple(f'table:{name}' for name in sorted(
        {recipe["root"], *(j["right"] for j in recipe["joins"])}))


def saved_input(workspace: Workspace, name: str) -> dict | None:
    return next((a["alignment"] for a in workspace.analyses
                 if a.get("table") == name and a.get("alignment")), None)


def frame_names(workspace: Workspace, analysis: Mapping) -> list[str]:
    """The sandbox dependency set, with a conservative fallback for dynamic access."""
    from .sandbox import referenced_frames
    available = dict.fromkeys(workspace.table_names())
    if analysis.get("alignment"):
        available[analysis["table"]] = None
    if analysis.get("kind") == "python":
        return list(referenced_frames(str((analysis.get("spec") or {}).get("code") or ""), available))
    names = {analysis.get("table")}
    lookup = (analysis.get("spec") or {}).get("params", {}).get("lookup_table")
    if lookup:
        names.add(lookup)
    return sorted(name for name in names if name)


def execution_frames(workspace: Workspace, analysis: Mapping) -> dict:
    if analysis.get("alignment"):
        return {name: workspace.get_frame(name) for name in frame_names(workspace, analysis)}
    frames = {}
    for name in workspace.table_names():
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            continue  # Preserve legacy tolerance of unrelated broken joins.
    return frames


def compact_frame_map(workspace: Workspace, frames) -> dict:
    """Schema-only descriptors. Joined columns are reconstructed from sources."""
    known = set(workspace.table_names())
    scoped = [str(name) for name in frames if str(name) in known]
    joins_by_name = {j["name"]: j for j in workspace.joins}
    # Include dependency descriptors before their consumers. A compact alignment
    # is usable only when both source schemas survived the context budget.
    described_by_name = {}
    columns_by_name = {}

    def describe(name):
        if name in described_by_name:
            return
        join = joins_by_name.get(name)
        if join and join.get("how") in {"left", "inner", "full", "cross"}:
            describe(join["left"])
            describe(join["right"])
            left = columns_by_name[join["left"]]
            right = columns_by_name[join["right"]]
            keys = (join.get("right_on") or []) if join["how"] != "cross" else []
            suffix = join_suffix(left, right, keys, join["right"])
            renamed = {c: c + suffix for c in right if c in left and c not in keys}
            described = {"table": name, "join": {k: join[k] for k in
                ("left", "right", "how", "left_on", "right_on") if k in join}}
            if renamed:
                described["renamed"] = renamed
            columns_by_name[name] = left + [renamed.get(c, c) for c in right if c not in keys]
        else:
            frame = workspace.get_frame(name)
            described = {"table": name, "rows": frame.height,
                         "columns": [{"name": c, "dtype": str(t)} for c,t in frame.schema.items()]}
            columns_by_name[name] = frame.columns
        described_by_name[name] = described
    for name in scoped:
        describe(name)
    return described_by_name


def carry_input_code(workspace: Workspace, analysis: Mapping, code: str) -> str:
    """Carry an EDA input into a standalone promoted Data Test as local Polars."""
    if not analysis.get("alignment"):
        return code
    recipe = validate_input(workspace, analysis["alignment"])
    view = InputWorkspace(workspace, [recipe], validated=True)
    lines = []
    for hop in recipe["joins"]:
        left = view.get_frame(hop["left"])
        right = view.get_frame(hop["right"])
        suffix = join_suffix(left.columns, right.columns, hop["right_on"], hop["right"])
        lines.append(
            f"tables[{hop["name"]!r}] = tables[{hop["left"]!r}].join("
            f"tables[{hop["right"]!r}], how='left', left_on={hop["left_on"]!r}, "
            f"right_on={hop["right_on"]!r}, coalesce=True, suffix={suffix!r}, validate='m:1')"
        )
        if hop["name"].isidentifier():
            lines.append(f"{hop["name"]} = tables[{hop["name"]!r}]")
    return "\n".join(lines) + "\n" + code
