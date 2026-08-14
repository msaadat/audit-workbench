"""Deterministic join-candidate inference and diagnostics.

Given two frames, propose key pairs worth testing (by column name affinity and
value overlap) and measure the evidence for each: datatype compatibility, key
uniqueness on both sides, match rate, unmatched populations, and the row-count
effect of a left join. All computation is local Polars; only the resulting
aggregate metrics may be shown to the model or the user.

The agent creates a join automatically only when the evidence is *strong*
(see :func:`classify`); weaker candidates become approval proposals or
warnings, never silent joins. When several candidates tie, the best-ranked one
is applied and :func:`decisive` decides whether that choice was forced by the
evidence or merely by rank — a tie between keys that mean different things is
reported so it is never silent.

Two shapes of relationship need more than a same-name key comparison:

*Role-qualified foreign keys.* A transaction table references a person
dimension through the person's role in the workflow — ``APPROVED_BY_ID``,
``FIN_APPROVED_BY_ID``, ``REQUESTER_ID`` all point at ``staff_details.STAFF_ID``
— so pure name equality scores them zero and the dimension stays orphaned.
:func:`_name_affinity` recognizes them, but only against the workspace's own
table names: a stem that names another imported table is that table's key, not
a role. One pair can then hold several equally-strong role keys, and the choice
between them changes what a downstream test *means*, so :func:`decisive` marks
that particular tie as unresolved by evidence and the run says so.

*Chained joins.* A dimension is sometimes two hops away — a requisition's
approval limit lives in the approval matrix, reachable only through the
approver's job title in the staff table. :func:`find_candidates` therefore
offers materialized joins as fact-side frames as well, bounded three ways: by
:data:`MAX_JOIN_LINEAGE` so a chain cannot grow without limit, by lineage so one
set of base tables yields one frame however many paths reach it, and by
:func:`chain_extends_reach` so a chain is only built when it makes a pair of
tables testable that pairwise joins leave apart.
"""

from __future__ import annotations

import re

import polars as pl

from ..workspaces import Workspace, join_suffix

# Candidate discovery caps: comparing every column pair is quadratic, so only
# plausible key columns (ids, codes, low-null, reasonable cardinality) enter.
# The cap is per table pair; a transaction table can reference one person
# dimension through several roles, so it must hold more than a handful.
MAX_CANDIDATES_PER_PAIR = 4
SAMPLE_VALUES = 50_000

# How many base tables a frame may already be built from and still be offered
# as the fact side of a further join. Two keeps chains at three tables — enough
# for transaction → person → policy, short enough that the pair count stays
# bounded and every chain remains explainable.
MAX_JOIN_LINEAGE = 2

STRONG_MATCH_RATE = 0.98
GOOD_MATCH_RATE = 0.90
# A join that multiplies the left row count is almost never what an auditor
# wants created silently.
MAX_ROW_MULTIPLICATION = 1.001

_KEY_NAME_RE = re.compile(r"(?i)(id|code|key|no|num|number|ref)$")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9]+")
# The trailing segment that marks a column as a frame's own identifier.
_ID_SEGMENTS = ("id", "key", "code")
# Segments that only ever say "this is a reference", carrying no noun of their
# own. Stripped from the tail before a column's meaning is read, and never
# mistaken for an agent noun — "number" ends like one without being one.
_KEY_SEGMENTS = (
    "id",
    "key",
    "code",
    "no",
    "num",
    "number",
    "ref",
    "reference",
    "link",
    "fk",
)
# An agent noun ("requester", "supervisor", "buyer") names the person playing a
# role, so a column built on one references a person dimension. Entity names
# ending the same way ("vendor") are excluded by the workspace's own table
# names, not by a word list.
_AGENT_NOUN_SUFFIXES = ("er", "or")
_MIN_AGENT_NOUN_LENGTH = 4
# Affinities a role reference scores, both below every direct name match so a
# frame's own key always wins. Identified here so a candidate built from one can
# be marked as a role key, which is what makes a tie between two of them a
# question for the auditor rather than a ranking.
ROLE_KEY_AFFINITY = 0.7
ROLE_ATTRIBUTE_AFFINITY = 0.65
ROLE_AFFINITIES = (ROLE_KEY_AFFINITY, ROLE_ATTRIBUTE_AFFINITY)


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _segments(name: str) -> list[str]:
    return [segment.lower() for segment in _SEGMENT_RE.findall(str(name))]


def _relationship_base(name: str) -> str:
    normalized = _normalize(name)
    for suffix in ("reference", "link", "ref", "fk"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[: -len(suffix)]
    return normalized


def entity_tokens(names: object) -> frozenset[str]:
    """Every word the workspace's own table names are built from.

    Used as the negative test for a role reference: a column stem that names an
    imported table is that table's foreign key, not a role played by a person.
    Singular and plural forms are both recorded so ``requisitions`` also blocks
    ``requisition``.
    """
    tokens: set[str] = set()
    for name in names or ():
        for segment in _segments(name):
            tokens.add(segment)
            tokens.add(segment.rstrip("s"))
    tokens.discard("")
    return frozenset(tokens)


def _identity_stem(right_column: str, right_table: str) -> str:
    """The entity a right-hand column identifies, when it is that frame's key.

    ``staff_details.STAFF_ID`` identifies ``staff``; ``po_data.PO_TOTAL_AMOUNT``
    identifies nothing. The stem must be recognisable in the frame's own name,
    which is what makes this a dimension key rather than a stray ``*_ID``.
    """
    segments = _segments(right_column)
    if len(segments) < 2 or segments[-1] not in _ID_SEGMENTS:
        return ""
    stem = "".join(segments[:-1])
    table_segments = _segments(right_table)
    if stem in table_segments or "".join(table_segments).startswith(stem):
        return stem
    return ""


def _role_affinity(left_column: str, entities: frozenset[str]) -> float:
    """How strongly a column names a person by their role rather than by entity.

    Two spellings carry a role: an explicit ``_BY`` segment (``APPROVED_BY``,
    ``FIN_APPROVED_BY_ID``) and an agent noun (``REQUESTER_ID``,
    ``SUPERVISOR_APPROVAL_ID``). Either way the head noun must not be one of the
    workspace's own table names — that is what keeps ``VENDOR_ID``, an entity
    key that happens to end in "or", out.

    A role column that is not shaped like an identifier scores lower:
    ``REQUESTER_DEPARTMENT`` names the requester's department, not the
    requester, and outranking a real key with it wastes the pair's candidate
    budget on a column that cannot join.
    """
    raw = _segments(left_column)
    segments = list(raw)
    while segments and segments[-1] in _KEY_SEGMENTS:
        segments = segments[:-1]
    named = [segment for segment in segments if segment != "by"]
    if not named or named[0] in entities:
        return 0.0
    role = "by" in segments or any(
        len(segment) >= _MIN_AGENT_NOUN_LENGTH
        and segment.endswith(_AGENT_NOUN_SUFFIXES)
        and segment not in _KEY_SEGMENTS
        for segment in named
    )
    if not role:
        return 0.0
    identifier_shaped = bool(raw) and (raw[-1] in _KEY_SEGMENTS or raw[-1] == "by")
    return ROLE_KEY_AFFINITY if identifier_shaped else ROLE_ATTRIBUTE_AFFINITY


def _name_affinity(
    left: str,
    right: str,
    right_table: str,
    entities: frozenset[str] = frozenset(),
) -> float:
    """How strongly two column names suggest a relationship.

    ``entities`` are the workspace's own table-name words. Role-reference
    scoring needs them to tell a role apart from an entity key, so it stays off
    when a caller supplies none.
    """
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
    # e.g. requisitions.FIN_APPROVED_BY_ID ↔ staff_details.STAFF_ID. Ranked
    # below every direct name match so a table's own key always wins.
    if entities and _identity_stem(right, right_table):
        role = _role_affinity(left, entities)
        if role:
            return role
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


def _is_plausible_key(df: pl.DataFrame, column: str, *, dimension: bool = True) -> bool:
    """Whether a column could carry a join key on the given side.

    The two sides want opposite things of cardinality. A dimension key must be
    close to unique, so a low-cardinality column there is a category, not a key.
    On the fact side low cardinality is the *expected* shape of a foreign key —
    112 requisitions approved by four job titles repeat each title 28 times —
    so applying the dimension's floor there hides exactly the many-to-one
    relationships joins exist to express. The fact side is instead held to what
    it must actually satisfy: a real column with more than one value, whose
    claim to be a key is then proved by name affinity and value overlap.
    """
    series = df[column]
    if series.null_count() == df.height:
        return False
    if df.schema[column].is_float():
        return False  # float keys are a data smell, not a join key
    non_null = df.height - series.null_count()
    if not non_null:
        return False
    distinct = series.n_unique()
    if distinct <= 1:
        return False
    if not dimension:
        return True
    return distinct / non_null >= 0.05 or bool(_KEY_NAME_RE.search(column))


def candidate_keys(
    left_df: pl.DataFrame,
    right_df: pl.DataFrame,
    right_table: str,
    entities: frozenset[str] = frozenset(),
) -> list[tuple[str, str, float]]:
    """(left_column, right_column, name_affinity) pairs worth diagnosing,
    strongest name affinity first, deduplicated per left column.

    ``entities`` enables role-reference scoring; see :func:`entity_tokens`.
    """
    pairs = []
    for left_col in left_df.columns:
        if not _is_plausible_key(left_df, left_col, dimension=False):
            continue
        for right_col in right_df.columns:
            if not _is_plausible_key(right_df, right_col):
                continue
            affinity = _name_affinity(left_col, right_col, right_table, entities)
            if affinity > 0:
                pairs.append((left_col, right_col, affinity))
    # Ties break on column name so the candidate set is stable across runs.
    pairs.sort(key=lambda p: (-p[2], p[0], p[1]))
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


_STRENGTH_ORDER = {"strong": 0, "moderate": 1, "weak": 2}


def evidence_rank(candidate: dict) -> tuple:
    """How good one candidate's evidence is, lowest first.

    The single ordering every caller sorts by and :func:`decisive` compares on;
    if the two ever disagreed, a candidate could be called decisive on the
    strength of its column name's place in the alphabet. Key coverage is part
    of the rank because a key that is null on part of the population buys fewer
    matched rows than one that is populated throughout, at equal match rate.
    """
    diagnostics = candidate.get("diagnostics") or {}
    return (
        _STRENGTH_ORDER.get(str(candidate.get("strength")), len(_STRENGTH_ORDER)),
        -float(diagnostics.get("match_rate") or 0.0),
        float(diagnostics.get("row_multiplication") or 1.0),
        int(diagnostics.get("left_null_keys") or 0),
    )


def _stable_order(candidate: dict) -> tuple:
    """Evidence first, then names, so equal evidence still sorts repeatably."""
    return (
        evidence_rank(candidate),
        str(candidate.get("left")),
        str(candidate.get("right")),
        tuple(str(value) for value in candidate.get("left_on") or []),
        tuple(str(value) for value in candidate.get("right_on") or []),
    )


def decisive(candidates: list[dict]) -> bool:
    """Whether the evidence names one candidate, rather than merely ordering them.

    Ties only change meaning when the tied keys mean different things. A frame
    reaching one person dimension as requester, verifier, and approver has
    several perfect keys whose rank order is an artifact of sorting, and the
    choice decides what every downstream test measures: an approval-limit check
    against the requester's limit is not a weaker answer, it is the wrong one.

    Two entity keys that tie — ``PO_NUMBER`` and ``REQUISITION_ID`` both linking
    purchase orders to requisitions, or the same key seen from either side — are
    a different case. They express one relationship, so which is applied is a
    choice of route, not of meaning.

    This reports the distinction; it does not decide what to do about it. A run
    still materializes the best-ranked candidate either way — an unattended run
    that left the pair unjoined would remove the frame rather than protect it —
    and uses this to say plainly when a real alternative was passed over.
    """
    if not candidates:
        return False
    ranked = sorted(candidates, key=evidence_rank)
    if len(ranked) == 1:
        return True
    best = evidence_rank(ranked[0])
    tied = [item for item in ranked if evidence_rank(item) == best]
    if len(tied) == 1:
        return True
    return sum(1 for item in tied if item.get("role_key")) < 2


def frame_lineage(workspace: Workspace, name: str, _seen: frozenset = frozenset()) -> frozenset[str]:
    """The base tables a frame is built from — itself, for a base table.

    A join over a join carries both sides' lineage, which is what keeps
    :func:`find_candidates` from offering a table the chain already contains.
    """
    if name in _seen:
        return frozenset()
    if any(str(item.get("name")) == name for item in workspace.tables):
        return frozenset({name})
    join = next(
        (item for item in workspace.joins if str(item.get("name")) == name), None
    )
    if join is None:
        return frozenset()
    seen = _seen | {name}
    return frame_lineage(workspace, str(join.get("left")), seen) | frame_lineage(
        workspace, str(join.get("right")), seen
    )


def frame_grain(workspace: Workspace, name: str, _seen: frozenset = frozenset()) -> str:
    """The base table whose rows a frame has one of — itself, for a base table.

    Every materialized join is a left join, so a frame has exactly the rows of
    the left-most base table in its chain, however many dimensions hang off it.
    That table is the population a test written against the frame is actually
    asserting about: a step filtering
    ``invoice_data_po_data_joined_requisitions_joined`` reaches only the 93 of
    112 requisitions that carry an invoice, so a requisition-level assertion
    written there is not a statement about requisitions at all.
    """
    if name in _seen:
        return name
    if any(str(item.get("name")) == name for item in workspace.tables):
        return name
    join = next(
        (item for item in workspace.joins if str(item.get("name")) == name), None
    )
    if join is None:
        return name
    return frame_grain(workspace, str(join.get("left")), _seen | {name})


def _frame_columns(workspace: Workspace, name: str) -> list[str]:
    """A frame's column names, from the cached profile where one exists.

    The profile is cached on disk by content signature, so this avoids reading
    and re-joining the frame just to learn its column names.
    """
    try:
        profile = workspace.get_profile(name)
    except Exception:
        try:
            return list(workspace.get_frame(name).columns)
        except Exception:
            return []
    columns = [
        str(item.get("name"))
        for item in profile.get("column_profiles") or []
        if str(item.get("name") or "").strip()
    ]
    if columns:
        return columns
    try:
        return list(workspace.get_frame(name).columns)
    except Exception:
        return []


def column_origins(
    workspace: Workspace, name: str, _seen: frozenset = frozenset()
) -> dict[str, str]:
    """Which base table each of a frame's columns came from.

    A join is a view over its sides, so ``invoice_data.INVOICE_DATE`` and
    ``invoice_data_po_data_joined.INVOICE_DATE`` are one column seen twice.
    Resolving a column to the table it originates in is what lets a caller
    recognise two analyses as the same computation across a join family rather
    than treating the frame name as part of the analysis's identity.

    Columns Polars had to rename on collision (``VENDOR_ID_right``) resolve to
    the side they actually came from.
    """
    if name in _seen:
        return {}
    columns = _frame_columns(workspace, name)
    if any(str(item.get("name")) == name for item in workspace.tables):
        return {column: name for column in columns}
    join = next(
        (item for item in workspace.joins if str(item.get("name")) == name), None
    )
    if join is None:
        return {}
    seen = _seen | {name}
    left = column_origins(workspace, str(join.get("left")), seen)
    right = column_origins(workspace, str(join.get("right")), seen)
    # The same suffix the join itself was materialized with, recomputed from the
    # two sides rather than stored, so this stays correct for frames built
    # before the suffix could vary.
    suffix = join_suffix(
        list(left),
        list(right),
        join.get("right_on") or (),
        str(join.get("right")),
    )
    origins: dict[str, str] = {}
    for column in columns:
        if column in left:
            origins[column] = left[column]
        elif column in right:
            origins[column] = right[column]
        elif column.endswith(suffix) and column[: -len(suffix)] in right:
            origins[column] = right[column[: -len(suffix)]]
    return origins


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

    entities = entity_tokens(item["name"] for item in workspace.tables)
    candidates = []
    for fact, dimension in ((left, right), (right, left)):
        for left_on, right_on, affinity in candidate_keys(
            frames[fact], frames[dimension], dimension, entities
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
                    "role_key": affinity in ROLE_AFFINITIES,
                    "diagnostics": diagnostics,
                }
            )
    candidates.sort(key=_stable_order)
    return candidates


def direct_join(workspace: Workspace, left: str, right: str) -> dict | None:
    """The materialized join directly connecting two frames, if any."""
    sides = {left, right}
    return next(
        (
            join
            for join in workspace.joins
            if {str(join.get("left")), str(join.get("right"))} == sides
        ),
        None,
    )


def chain_extends_reach(workspace: Workspace, lineage: frozenset[str], table: str) -> bool:
    """Whether adding ``table`` to a chain connects something pairwise joins cannot.

    A chain earns its place by making a previously untestable pair testable —
    requisition amounts against the approval matrix, which share no key and can
    only meet through the approver's staff record. When every table in the
    lineage already joins directly to the one being added, the chain restates
    reachability that pairwise frames already provide, and each such frame costs
    a model call to analyse for nothing.
    """
    return any(
        direct_join(workspace, member, table) is None
        for member in lineage
        if member != table
    )


def chain_fact_frames(workspace: Workspace) -> list[str]:
    """Materialized joins short enough to carry one more hop, in stable order.

    A dimension can sit two hops from the transaction that needs it — a
    requisition's approval limit is held against the approver's job title, so it
    is reachable only once the approver's staff record is attached. Offering the
    materialized join as a fact side is what lets the second hop be discovered
    at all; :data:`MAX_JOIN_LINEAGE` is what stops it recurring indefinitely.
    """
    return [
        str(join["name"])
        for join in sorted(workspace.joins, key=lambda item: str(item.get("name")))
        if 0 < len(frame_lineage(workspace, str(join["name"]))) <= MAX_JOIN_LINEAGE
    ]


def find_candidates(workspace: Workspace) -> list[dict]:
    """Diagnosed join candidates across the workspace's frames, best first.

    Every ordered pair of base tables is diagnosed, plus every pairing of a
    short-lineage materialized join with a base table it does not already
    contain — the chained relationships a purely pairwise sweep cannot reach.

    Each entry is a ready-to-apply join spec plus its evidence:
    ``{left, right, left_on, right_on, how, strength, diagnostics}``.
    """
    names = [t["name"] for t in workspace.tables]
    derived = chain_fact_frames(workspace)
    frames: dict[str, pl.DataFrame] = {}
    for name in (*names, *derived):
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            continue

    existing = {
        (j["left"], j["right"], tuple(j["left_on"]), tuple(j["right_on"]))
        for j in workspace.joins
    }
    entities = entity_tokens(names)

    # Base tables pair in both directions; a derived frame is only ever the fact
    # side, and only against a table its lineage does not already hold — joining
    # a chain back onto its own member duplicates columns and proves nothing.
    ordered_pairs = [(left, right) for left in names for right in names]
    # One chain per set of base tables: the same three tables reached by two
    # different first hops would otherwise be diagnosed, and materialized,
    # twice over.
    claimed = {frame_lineage(workspace, name) for name in workspace.table_names()}
    for chain in derived:
        lineage = frame_lineage(workspace, chain)
        for right in names:
            combined = lineage | {right}
            if right in lineage or combined in claimed:
                continue
            if not chain_extends_reach(workspace, lineage, right):
                continue
            claimed.add(combined)
            ordered_pairs.append((chain, right))

    candidates = []
    for left, right in ordered_pairs:
        if left == right or left not in frames or right not in frames:
            continue
        for left_on, right_on, affinity in candidate_keys(
            frames[left], frames[right], right, entities
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
                    "role_key": affinity in ROLE_AFFINITIES,
                    "diagnostics": diagnostics,
                }
            )

    # Prefer strong evidence, then higher match rates and better key coverage;
    # that ordering keeps the direction with the fact table on the left.
    candidates.sort(key=_stable_order)
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
