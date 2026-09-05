"""Measured route catalog for EDA. No utility model and no durable join writes."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from itertools import permutations
from threading import RLock

import polars as pl

from ..analysis_inputs import InputWorkspace, validate_input
from ..workspaces import WorkspaceError, join_suffix
from . import joins, probes

MAX_ROUTES_PER_PAIR = 32
MAX_ALIGNMENTS = 192
MAX_HOPS = 2
_CACHE = OrderedDict()
_CACHE_LOCK = RLock()


def catalog(workspace, tables):
    tables = tuple(sorted(tables))
    key = (str(workspace.root), tuple((n, workspace.content_signature(n)) for n in tables),
           json.dumps(workspace.joins, sort_keys=True, default=str))
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    entities = joins.entity_tokens(tables)
    frames = {n: workspace.get_frame(n) for n in tables}
    routes = []
    omitted = 0
    for left, right in permutations(tables, 2):
        lf, rf = frames[left], frames[right]
        # Retain alternate right keys, including value-supported mixed-domain
        # links that naming alone cannot infer. Only metadata leaves this module.
        right_keys = [c for c in rf.columns if joins._is_plausible_key(rf, c)]
        candidates = []
        for lc in lf.columns:
            if not (probes.reference_shaped(lc) or joins._is_plausible_key(lf, lc, dimension=False)):
                continue
            for rc in right_keys:
                affinity = joins._name_affinity(lc, rc, right, entities)
                diag = joins.diagnose(lf, rf, lc, rc)
                value_supported = (
                    probes.reference_shaped(lc) and probes.reference_shaped(rc)
                    and diag["matched_keys"]
                )
                if affinity <= 0 and not value_supported:
                    continue
                candidates.append({"left": left, "right": right, "left_on": [lc],
                    "right_on": [rc], "how": "left", "strength": joins.classify(diag),
                    "diagnostics": diag, "compatible": lf.schema[lc] == rf.schema[rc],
                    "affinity": affinity, "role_key": affinity in joins.ROLE_AFFINITIES,
                    "ref": f'relationship:{left}:{right}:{lc}:{rc}'})
        candidates.sort(key=lambda r: (-r["affinity"], -r["diagnostics"]["match_rate"], r["ref"]))
        omitted += max(0, len(candidates) - MAX_ROUTES_PER_PAIR)
        routes.extend(candidates[:MAX_ROUTES_PER_PAIR])
    durable = set()
    by_name = {j["name"]: j for j in workspace.joins}
    for name in by_name:
        spine = []
        seen = set()
        while name in by_name and name not in seen:
            seen.add(name)
            hop = by_name[name]
            if hop["right"] not in frames:
                break
            spine.insert(0, (hop["right"], tuple(hop.get("left_on", [])),
                             tuple(hop.get("right_on", [])), hop["how"]))
            name = hop["left"]
        if name in frames:
            durable.add((name, tuple(spine)))
    recipes = []
    frontier = [(n, [], {n}) for n in tables]
    for _ in range(MAX_HOPS):
        next_frontier = []
        for root, hops, used in frontier:
            last_table = hops[-1]["right"] if hops else root
            for route in routes:
                if route["left"] != last_table or route["right"] in used:
                    continue
                d = route["diagnostics"]
                if not route["compatible"] or not d["right_key_unique"] or d["row_multiplication"] > 1.001:
                    continue
                # Chain via a dimension column only when it survives the prior
                # coalescing join; its schema is validated before any proposal.
                left_name = hops[-1]["name"] if hops else root
                candidate = {k: route[k] for k in ("right", "left_on", "right_on", "how")}
                candidate["left"] = left_name
                if hops:
                    # A key in the last dimension may have been coalesced into
                    # a differently named root key, or suffixed on collision.
                    projection = {(root, c): c for c in frames[root].columns}
                    current_columns = list(frames[root].columns)
                    for hop in hops:
                        right_columns = frames[hop["right"]].columns
                        suffix = join_suffix(current_columns, right_columns, hop["right_on"], hop["right"])
                        for c in right_columns:
                            actual = (hop["left_on"][hop["right_on"].index(c)] if c in hop["right_on"]
                                      else c + suffix if c in current_columns else c)
                            projection[(hop["right"], c)] = actual
                        current_columns += [
                            projection[(hop["right"], c)]
                            for c in right_columns if c not in hop["right_on"]
                        ]
                    candidate["left_on"] = [projection[(last_table, c)] for c in route["left_on"]]
                digest = hashlib.sha256(
                    json.dumps([root, hops, candidate], sort_keys=True).encode()
                ).hexdigest()[:12]
                candidate["name"] = f'aligned_{root}_{digest}'
                new_hops = hops + [candidate]
                if len(recipes) >= MAX_ALIGNMENTS:
                    omitted += 1
                    continue
                recipe = {"version": 1, "root": root, "name": candidate["name"], "joins": new_hops}
                try:
                    recipe = validate_input(workspace, recipe)
                except (ValueError, WorkspaceError, pl.exceptions.PolarsError):
                    continue
                signature = (root, tuple(
                    (h["right"], tuple(h["left_on"]), tuple(h["right_on"]), h["how"])
                    for h in new_hops
                ))
                if signature not in durable:
                    recipes.append(recipe)
                next_frontier.append((root, new_hops, used | {route["right"]}))
        frontier = next_frontier
    result = {"routes": routes, "recipes": recipes, "omitted": omitted}
    with _CACHE_LOCK:
        _CACHE[key] = result
        while len(_CACHE) > 8:
            _CACHE.popitem(last=False)
    return result


def view(workspace, tables):
    recipes = catalog(workspace, tables)["recipes"]
    cache_key = (workspace.revision, tuple(tables), id(recipes))
    cached = getattr(workspace, "_analysis_input_view_cache", None)
    if cached and cached[0] == cache_key:
        return cached[1]
    saved = [a["alignment"] for a in workspace.analyses if a.get("alignment")
             and a["alignment"]["name"] not in {r["name"] for r in recipes}]
    result = InputWorkspace(workspace, [*recipes, *saved], validated=True)
    workspace._analysis_input_view_cache = (cache_key, result)
    return result


def recipe_for(workspace, tables, name):
    return next((r for r in catalog(workspace, tables)["recipes"] if r["name"] == name), None)


# Separate from the durable-join gate's declined-pair ledger. Diagnosing an EDA
# route must not imply that an explicit future request to build it was declined.
def _pair_ledger_path(workspace):
    from .. import loader
    return workspace.data_dir / loader.CACHE_DIRNAME / "analysis_relationships.json"


def _pair_basis(workspace, left, right):
    return hashlib.sha256(repr((1, workspace.content_signature(left),
                               workspace.content_signature(right))).encode()).hexdigest()


def _read_pairs(workspace):
    try:
        value = json.loads(_pair_ledger_path(workspace).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def pending_pairs(workspace, pairs):
    recorded = _read_pairs(workspace)
    return tuple((left, right) for left, right in pairs
                 if recorded.get(f"{left}:{right}") != _pair_basis(workspace, left, right))


def settle_pair(workspace, left, right):
    from ..workspaces import write_json_atomic
    recorded = _read_pairs(workspace)
    recorded[f"{left}:{right}"] = _pair_basis(workspace,left,right)
    path = _pair_ledger_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, recorded)
