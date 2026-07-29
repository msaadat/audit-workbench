"""Deterministic join-candidate inference and diagnostics.

Given two base tables, propose key pairs worth testing (by column name
affinity and value overlap) and measure the evidence for each: datatype
compatibility, key uniqueness on both sides, match rate, unmatched
populations, and the row-count effect of a left join. All computation is
local Polars; only the resulting aggregate metrics may be shown to the model
or the user.

The agent creates a join automatically only when the evidence is *strong*
(see :func:`classify`); weaker candidates become approval proposals or
warnings, never silent joins.
"""

from __future__ import annotations

import re

import polars as pl

from ..workspaces import Workspace

# Candidate discovery caps: comparing every column pair is quadratic, so only
# plausible key columns (ids, codes, low-null, reasonable cardinality) enter.
MAX_CANDIDATES_PER_PAIR = 3
SAMPLE_VALUES = 50_000

STRONG_MATCH_RATE = 0.98
GOOD_MATCH_RATE = 0.90
# A join that multiplies the left row count is almost never what an auditor
# wants created silently.
MAX_ROW_MULTIPLICATION = 1.001

_KEY_NAME_RE = re.compile(r"(?i)(id|code|key|no|num|number|ref)$")


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _relationship_base(name: str) -> str:
    normalized = _normalize(name)
    for suffix in ("reference", "link", "ref", "fk"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _name_affinity(left: str, right: str, right_table: str) -> float:
    """How strongly two column names suggest a relationship."""
    l, r = _normalize(left), _normalize(right)
    if l == r:
        return 1.0
    if _relationship_base(left) == _relationship_base(right):
        # Explicit foreign-key naming such as PO_NUMBER_LINK -> PO_NUMBER.
        return 0.9
    table = _normalize(right_table).rstrip("s")
    # e.g. transactions.cust_id ↔ customers.id
    if r in ("id", "key", "code") and table and table[:4] in l:
        return 0.8
    if l.endswith(r) or r.endswith(l):
        return 0.6
    return 0.0


def _key_series(df: pl.DataFrame, column: str) -> pl.Series:
    """Key values as trimmed strings so an int code matches its text twin."""
    return (
        df.head(SAMPLE_VALUES)[column]
        .cast(pl.String)
        .str.strip_chars()
        .drop_nulls()
    )


def _is_plausible_key(df: pl.DataFrame, column: str) -> bool:
    series = df[column]
    if series.null_count() == df.height:
        return False
    if df.schema[column].is_float():
        return False  # float keys are a data smell, not a join key
    non_null = df.height - series.null_count()
    if not non_null:
        return False
    distinct = series.n_unique()
    return distinct > 1 and (
        distinct / non_null >= 0.05 or bool(_KEY_NAME_RE.search(column))
    )


def candidate_keys(
    left_df: pl.DataFrame,
    right_df: pl.DataFrame,
    right_table: str,
) -> list[tuple[str, str, float]]:
    """(left_column, right_column, name_affinity) pairs worth diagnosing,
    strongest name affinity first, deduplicated per left column."""
    pairs = []
    for left_col in left_df.columns:
        if not _is_plausible_key(left_df, left_col):
            continue
        for right_col in right_df.columns:
            if not _is_plausible_key(right_df, right_col):
                continue
            affinity = _name_affinity(left_col, right_col, right_table)
            if affinity > 0:
                pairs.append((left_col, right_col, affinity))
    pairs.sort(key=lambda p: -p[2])
    seen: set[str] = set()
    unique = []
    for pair in pairs:
        if pair[0] in seen:
            continue
        seen.add(pair[0])
        unique.append(pair)
    return unique[:MAX_CANDIDATES_PER_PAIR]


def diagnose(
    left_df: pl.DataFrame,
    right_df: pl.DataFrame,
    left_on: str,
    right_on: str,
) -> dict:
    """Aggregate join evidence for one key pair (no row values)."""
    left_keys = _key_series(left_df, left_on)
    right_keys = _key_series(right_df, right_on)
    left_total = len(left_keys)
    right_distinct = right_keys.n_unique()
    right_unique = right_distinct == len(right_keys)

    right_set = right_keys.unique().implode()
    matched = int(left_keys.is_in(right_set).sum()) if left_total else 0
    match_rate = matched / left_total if left_total else 0.0

    # Row-count effect of a left join: each left key multiplied by how many
    # right rows share it (1 when the right side is unique on the key).
    if right_unique:
        joined_rows = left_df.height
    else:
        counts = right_keys.value_counts()
        count_col = [c for c in counts.columns if c != right_on][0]
        joined_rows = int(
            pl.DataFrame({right_on: left_keys})
            .join(counts, on=right_on, how="left")
            .select(pl.col(count_col).fill_null(1).sum())
            .item()
            or 0
        )
        joined_rows += left_df.height - left_total  # null keys pass through
    multiplication = joined_rows / left_df.height if left_df.height else 1.0

    relationship = "many_to_one" if right_unique else "many_to_many"
    if left_keys.n_unique() == left_total and right_unique:
        relationship = "one_to_one"

    return {
        "left_on": left_on,
        "right_on": right_on,
        "left_rows": left_df.height,
        "right_rows": right_df.height,
        "left_keys_non_null": left_total,
        "left_null_keys": left_df.height - left_total,
        "right_key_unique": right_unique,
        "right_key_distinct": right_distinct,
        "matched_keys": matched,
        "unmatched_keys": left_total - matched,
        "match_rate": round(match_rate, 4),
        "relationship": relationship,
        "expected_rows": joined_rows,
        "row_multiplication": round(multiplication, 4),
    }


def classify(diagnostics: dict) -> str:
    """'strong' (auto-creatable), 'moderate' (propose only), or 'weak'."""
    if diagnostics["row_multiplication"] > MAX_ROW_MULTIPLICATION:
        return "weak"
    if not diagnostics["right_key_unique"]:
        return "weak"
    if diagnostics["match_rate"] >= STRONG_MATCH_RATE:
        return "strong"
    if diagnostics["match_rate"] >= GOOD_MATCH_RATE:
        return "moderate"
    return "weak"


def pair_candidates(workspace: Workspace, left: str, right: str) -> list[dict]:
    """Diagnosed join candidates for one unordered table pair, best first.

    Both directions are diagnosed because which side is the fact table is not
    known up front; the ordering rule is the same one :func:`find_candidates`
    applies globally (strongest evidence, then highest match rate). Every value
    in the result is an aggregate metric — no row values leave this module.

    Unlike :func:`find_candidates` this keeps every direction and key pair for
    the requested tables, so a caller can report ambiguity instead of silently
    taking the first candidate.
    """
    frames: dict[str, pl.DataFrame] = {}
    for name in (left, right):
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            return []

    existing = {
        (j["left"], j["right"], tuple(j["left_on"]), tuple(j["right_on"]))
        for j in workspace.joins
    }

    candidates = []
    for fact, dimension in ((left, right), (right, left)):
        for left_on, right_on, _ in candidate_keys(
            frames[fact], frames[dimension], dimension
        ):
            if (fact, dimension, (left_on,), (right_on,)) in existing:
                continue
            diagnostics = diagnose(frames[fact], frames[dimension], left_on, right_on)
            strength = classify(diagnostics)
            if strength == "weak" and diagnostics["match_rate"] < 0.5:
                continue  # noise, not a candidate worth surfacing
            candidates.append(
                {
                    "left": fact,
                    "right": dimension,
                    "left_on": [left_on],
                    "right_on": [right_on],
                    "how": "left",
                    "strength": strength,
                    "diagnostics": diagnostics,
                }
            )
    order = {"strong": 0, "moderate": 1, "weak": 2}
    candidates.sort(
        key=lambda c: (
            order[c["strength"]],
            -c["diagnostics"]["match_rate"],
            c["diagnostics"]["row_multiplication"],
            str(c["left"]),
            str(c["right"]),
            tuple(str(value) for value in c["left_on"]),
            tuple(str(value) for value in c["right_on"]),
        )
    )
    return candidates


def find_candidates(workspace: Workspace) -> list[dict]:
    """Diagnosed join candidates across every pair of base tables, best first.

    Each entry is a ready-to-apply join spec plus its evidence:
    ``{left, right, left_on, right_on, how, strength, diagnostics}``.
    """
    names = [t["name"] for t in workspace.tables]
    frames: dict[str, pl.DataFrame] = {}
    for name in names:
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            continue

    existing = {
        (j["left"], j["right"], tuple(j["left_on"]), tuple(j["right_on"]))
        for j in workspace.joins
    }

    candidates = []
    for left in names:
        for right in names:
            if left == right or left not in frames or right not in frames:
                continue
            for left_on, right_on, _ in candidate_keys(
                frames[left], frames[right], right
            ):
                if (left, right, (left_on,), (right_on,)) in existing:
                    continue
                diagnostics = diagnose(frames[left], frames[right], left_on, right_on)
                strength = classify(diagnostics)
                if strength == "weak" and diagnostics["match_rate"] < 0.5:
                    continue  # noise, not a candidate worth surfacing
                candidates.append(
                    {
                        "left": left,
                        "right": right,
                        "left_on": [left_on],
                        "right_on": [right_on],
                        "how": "left",
                        "strength": strength,
                        "diagnostics": diagnostics,
                    }
                )

    # Prefer strong evidence, then higher match rates; keep the direction with
    # the fact table on the left (more matched keys → earlier).
    order = {"strong": 0, "moderate": 1, "weak": 2}
    candidates.sort(
        key=lambda c: (order[c["strength"]], -c["diagnostics"]["match_rate"])
    )
    # One candidate per table pair (either direction) — the best one.
    seen_pairs: set[frozenset] = set()
    unique = []
    for candidate in candidates:
        pair = frozenset((candidate["left"], candidate["right"]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        unique.append(candidate)
    return unique
