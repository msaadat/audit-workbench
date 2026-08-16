"""Deterministic measurement of what a frame's own data already asserts.

A definition turn today proposes from *schema shape*: two date columns exist, so
compare them. That is why eleven of one engagement's twenty-eight procedures were
date-ordering checks and eight of those found nothing, while the comparison that
owned a 35.2M backdating population was never written — nothing distinguished
the pair that carries a finding from the pair that carries arithmetic.

This module measures first. It sweeps a frame's own columns, and the keys of the
frames beside it, and returns **nominations**: concrete specs in the analytics
library's own vocabulary, each carrying the counts it would produce if run. The
model still decides what a nomination *means*, which of them matter, and what
else to test — the sweep only removes the guessing about where to look.

Two properties are deliberate.

*A nomination is the library test, pre-run.* Every count here comes from
:func:`analytics.run_test` on the exact spec being nominated, so a nomination and
the saved analysis it becomes cannot disagree about what was measured. Nothing
reimplements a test.

*Nothing here reads a value into the result.* A nomination carries a spec, two
counts and a sentence. The rows behind it stay where every other flagged row in
this application stays — in the evidence sidecar the execution writes.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping

import polars as pl

from .. import analytics, profiler
from ..field_names import IDENTIFIER_SEGMENTS, subject_tokens
from ..workspaces import Workspace, WorkspaceError
from . import joins as join_diagnostics

# Trailing segments that make a column a reference to something rather than a
# measurement of it. ``by`` joins the identifier endings because an approver
# column is a person, not a quantity: comparing ``UPDATED_BY`` against
# ``APPROVED_BY`` as numbers asks whether one staff number is larger than
# another, and the profiler leaves both as ``numeric`` when their cardinality is
# low enough that its own id rule does not fire. ``link`` and ``fk`` follow
# ``joins._relationship_base``, which already reads them as foreign-key naming.
_REFERENCE_SEGMENTS = IDENTIFIER_SEGMENTS | {"by", "link", "fk"}

# ``by`` marks the column as naming a person by their role, so it is exactly what
# two role columns have in common and must not be read as shared subject matter.
# Stripping it is what separates ``UPDATED_BY``/``APPROVED_BY`` — two roles, and
# their equal rows are the exception — from ``REQUESTER_DEPARTMENT``/
# ``DEPARTMENT``, one attribute stated twice, whose differing rows are.
_ROLE_MARKERS = frozenset({"by"})

# A hold rate is only evidence if there is something to hold over. Below this a
# "94 of 98" and a "4 of 4" are the same claim, and the second is a coincidence.
# Referential and duplicate probes are exempt: an orphan key and a repeated key
# are facts about the rows themselves, not statements about a distribution.
MIN_COMPARISON_ROWS = 20

# How near-universal a relationship must be before its residue reads as an
# exception rather than as the ordinary spread of two unrelated columns. Set
# below the 0.90 the join gate uses for match rates because the interesting
# populations sit just under it: the invoice/PO ordering that owns A16 holds
# 94 of 98 (0.959), and the amount ceiling beside it holds 93 of 98 (0.949).
INVARIANT_HOLD_RATE = 0.85

# A relationship that holds everywhere established something too — that the
# population is consistent about it — and saying so once is how a reader learns
# what was tested rather than only what failed. Bounded hard: a frame's
# confirmed invariants are a footnote, not the report.
MAX_CONFIRMED_PER_FRAME = 3

# Two numeric columns are comparable when they measure the same kind of
# quantity. An ordered quantity is never going to exceed a purchase-order total
# and the arithmetic saying so is not a finding, it is a units mismatch. Medians
# within this factor of each other is the cheapest test of "same kind" that does
# not need to know what either column means.
MAGNITUDE_RATIO = 100.0

# How much of two columns' value domains must coincide before comparing them for
# equality is meaningful. Two columns drawing on one staff register overlap
# heavily; a purchase-order number and a vendor id do not overlap at all.
MIN_DOMAIN_OVERLAP = 0.3

# The band in which a duplicate probe is worth running. Below it the column is a
# category and every value repeats by design; at 1.0 the column is already unique
# and the test can only confirm it.
NEAR_UNIQUE_MIN = 0.80
NEAR_UNIQUE_MAX = 1.0

# Sweep caps. Pair-wise work is quadratic in the column count, so a wide frame
# would otherwise spend the stage on its own arithmetic. Columns are taken in
# frame order, which is the source's own order and therefore stable.
MAX_COLUMNS_PER_CLASS = 16
MAX_LOOKUP_TABLES = 8
MAX_NOMINATIONS_PER_FRAME = 12

# Families, in the order a reader should meet them: what does not reconcile,
# what does not hold, what should not repeat, what does not look like the rest.
REFERENTIAL = "referential"
COMPARISON = "comparison"
EQUALITY = "equality"
VALUES = "values"
DUPLICATES = "duplicates"
FORMAT = "format"
FAMILIES = (REFERENTIAL, COMPARISON, EQUALITY, VALUES, DUPLICATES, FORMAT)

# Per family, because families are not interchangeable and the comparison sweep
# is the one that fans out. A frame with eight date columns yields dozens of
# orderings, many broken by the same handful of rows, and ranked purely by
# flagged count they take every slot — leaving the reconciliation gap, the
# repeated key and the off-pattern identifier unreported on a frame that has all
# three. The budget is spent across the kinds of question, not on the loudest.
MAX_PER_FAMILY: dict[str, int] = {
    REFERENTIAL: 4,
    COMPARISON: 5,
    EQUALITY: 3,
    VALUES: 3,
    DUPLICATES: 3,
    FORMAT: 2,
}

_DATE_TYPES = frozenset({"date"})
_NUMERIC_TYPES = frozenset({"numeric"})
_KEYLIKE_TYPES = frozenset({"id", "categorical", "text"})


def _profile(workspace: Workspace, frame: str) -> dict:
    try:
        return workspace.get_profile(frame) or {}
    except (OSError, WorkspaceError):
        return {}


def reference_shaped(column: str) -> bool:
    """Whether a column's name says it points at something.

    The profiler classifies an integer column as an ``id`` only when it is also
    mostly distinct, which a foreign key never is — 118 invoices verified by a
    dozen people leave ``VERIFIED_BY_ID`` looking like a number. Its name does
    not, and the name is the evidence available here.
    """
    segments = [segment.lower() for segment in re.findall(r"[A-Za-z0-9]+", str(column))]
    return bool(segments) and segments[-1] in _REFERENCE_SEGMENTS


def _column_classes(profile: Mapping[str, object]) -> dict[str, list[str]]:
    """Group a frame's columns by the kind of probe each can carry.

    Read from the cached profile rather than from dtypes, so the profiler's own
    reading of a column travels with it — then corrected in one place: a
    reference-shaped column is a key whatever its storage type. That correction
    does two jobs at once. It keeps role columns out of the numeric sweep, where
    they produce orderings between staff numbers, and it puts them into the
    equality sweep, where two roles of one entity on one row is what segregation
    of duties actually looks like.
    """
    classes: dict[str, list[str]] = {"date": [], "numeric": [], "keylike": []}
    for column in profile.get("column_profiles") or []:
        name = str(column.get("name") or "").strip()
        inferred = str(column.get("inferred_type") or "")
        if not name:
            continue
        if inferred in _DATE_TYPES:
            classes["date"].append(name)
        elif reference_shaped(name):
            classes["keylike"].append(name)
        elif inferred in _NUMERIC_TYPES:
            classes["numeric"].append(name)
        elif inferred in _KEYLIKE_TYPES:
            classes["keylike"].append(name)
    return {key: value[:MAX_COLUMNS_PER_CLASS] for key, value in classes.items()}


def _nomination(
    frame: str,
    family: str,
    test: str,
    params: dict,
    result: analytics.AnalyticsResult,
    reading: str,
    evidence: dict | None = None,
) -> dict:
    """One measured spec, in the shape the definition context will carry it."""
    tested = int(result.tested or 0)
    flagged = int(result.detail.height) if result.detail is not None else 0
    return {
        "frame": frame,
        "family": family,
        "test": test,
        "params": dict(params),
        "signal": analytics.signal_for(test),
        "tested": tested,
        "flagged": flagged,
        "rate": round(flagged / tested, 4) if tested else None,
        # What the measurement says, in one line. The model is being handed a
        # spec and a count; without this it has to re-derive why the spec was
        # worth measuring, which is the reasoning the sweep just did.
        "reading": reading,
        "verdict_text": result.verdict_text,
        **({"evidence": evidence} if evidence else {}),
    }


def _run(
    frame: pl.DataFrame,
    test: str,
    params: dict,
    source: analytics.FrameSource | None = None,
) -> analytics.AnalyticsResult | None:
    """Run one candidate spec, or return ``None`` if it cannot be run.

    A probe is speculative by construction: it asks a question the data may
    refuse. A column that will not parse as a date, a lookup that will not load,
    a comparison with no rows on both sides — each is an answer of "not this
    one", never a reason to fail the sweep.
    """
    try:
        return analytics.run_test(frame, test, params, source=source)
    except Exception:  # noqa: BLE001 - a probe that cannot run is not a failure
        return None


# --------------------------------------------------------------- referential
def _lookup_keys(workspace: Workspace, table: str) -> list[str]:
    """Columns of ``table`` that actually identify its rows.

    The same rule the definition context uses to decide what may be named as a
    lookup: populated throughout and distinct on every row. A column that
    repeats is not something a reference can resolve *to*.
    """
    profile = _profile(workspace, table)
    rows = profile.get("rows")
    if not isinstance(rows, int) or rows <= 0:
        return []
    return [
        str(column.get("name"))
        for column in profile.get("column_profiles") or []
        if str(column.get("name") or "").strip()
        and column.get("distinct_count") == rows
        and not (column.get("blank_count") or 0)
    ]


def _lookup_domain(
    source: analytics.FrameSource, table: str, column: str
) -> set[str]:
    """The distinct values of one lookup key, read through the shared resolver."""
    try:
        return _domain(source.frame(table), column)
    except Exception:  # noqa: BLE001 - an unreadable lookup shares nothing
        return set()


def _resolves_elsewhere(result: analytics.AnalyticsResult) -> str:
    """Where the test found the unmatched values, if it found them anywhere."""
    return next(
        (
            str(stat["value"])
            for stat in result.stats
            if stat["label"] == "Unmatched keys resolve in"
        ),
        "",
    )


def referential_nominations(
    workspace: Workspace,
    frame: str,
    df: pl.DataFrame,
    *,
    source: analytics.FrameSource,
) -> list[dict]:
    """Reconcile every plausible reference this frame makes, and report the gaps.

    This is the half of the sweep that does not care what the join gate decided.
    A relationship whose keys mostly match becomes a join; a relationship whose
    keys mostly *do not* match is discarded as weak evidence — and the second is
    the one an auditor wants, because the unmatched rows are the finding. One
    engagement's 20 invoices referencing a purchase order that does not exist,
    1,054M of them, sat behind a 0.8376 match rate; its 93 purchase orders naming
    a buyer who is on no staff record sat behind a match rate of zero, below the
    floor that calls a candidate noise.

    So the reconciliation is run on name evidence alone, independent of whether
    the relationship was ever good enough to build — and then filtered by the one
    distinction that separates a finding from a mis-aimed probe. Name evidence
    proposes far more pairs than exist: every role column resembles every
    dimension key, so ``VERIFIED_BY_ID`` is offered against the vendor master as
    readily as against the staff master, and *every* value fails to reconcile.
    That is not a finding, it is the wrong question. What tells the two apart is
    already computed — a total mismatch whose values resolve in some other
    imported key means this lookup was never the referent, while a total mismatch
    that resolves nowhere is the strongest reconciliation failure there is.
    """
    lineage = join_diagnostics.frame_lineage(workspace, frame)
    entities = join_diagnostics.entity_tokens(
        [str(item.get("name") or "") for item in workspace.tables]
    )
    tables = [
        str(item.get("name") or "")
        for item in workspace.tables
        if str(item.get("name") or "") and str(item.get("name")) not in lineage
    ][:MAX_LOOKUP_TABLES]
    # One reconciliation per referencing column, across every candidate master.
    # A column is a reference to one thing; reporting it against four masters
    # says the same fact four times and crowds out three other columns.
    best: dict[str, dict] = {}
    attempted: dict[str, list[str]] = {}
    for table in tables:
        keys = _lookup_keys(workspace, table)
        if not keys:
            continue
        for column, key in join_diagnostics.reference_candidates(
            df.columns, keys, table, entities
        ):
            # Name affinity alone offers far more than exists — it will pair a
            # department against an invoice number because both are columns and
            # one of them is a key. A pair earns a measurement when the column
            # is named as a reference, or when the two already share at least
            # one value. Neither is a claim that the reference holds; both rule
            # out asking a question the columns cannot be an answer to.
            if not reference_shaped(column) and not (
                _domain(df, column) & _lookup_domain(source, table, key)
            ):
                continue
            params = {"column": column, "lookup_table": table, "lookup_column": key}
            result = _run(df, "referential", params, source)
            if result is None:
                continue
            orphans = int(result.detail.height) if result.detail is not None else 0
            tested = int(result.tested or 0)
            if not tested:
                continue
            rate = orphans / tested
            elsewhere = _resolves_elsewhere(result)
            if rate >= 1.0 and elsewhere:
                # Every value failed and every value is some other table's key:
                # the sweep aimed at the wrong master. The pair it should have
                # aimed at is a candidate of its own and reconciles cleanly, so
                # dropping this one loses nothing.
                continue
            attempted.setdefault(column, []).append(f"{table}.{key}")
            candidate = (rate, str(table), str(key))
            if column in best and best[column]["_order"] <= candidate:
                continue
            best[column] = {
                "_order": candidate,
                "params": params,
                "result": result,
                "orphans": orphans,
                "tested": tested,
                "elsewhere": elsewhere,
                "table": table,
                "key": key,
            }
    nominations = []
    for column, found in best.items():
        if not found["orphans"]:
            # Every value reconciled. The relationship holds, which is worth
            # knowing and is not worth a nomination against the ones that do not.
            continue
        reading = (
            f"{found['orphans']} of {found['tested']} {column} values do not "
            f"exist in {found['table']}.{found['key']}"
        )
        checked = sorted(set(attempted.get(column, ())))
        evidence: dict[str, object] = {"resolves_in": found["elsewhere"] or None}
        if found["elsewhere"]:
            reading += f"; they resolve in {found['elsewhere']}"
        else:
            # Which master a reference that matches nothing was checked against
            # is undetermined by the data — no candidate matched, so nothing
            # ranks them. Naming one and stopping makes an exhaustive result read
            # as a badly aimed question, and a run declined a genuine finding for
            # exactly that reason: 93 buyer identifiers reconciling to no
            # imported master, reported against the invoice table because its
            # name sorts first. Saying what else was tried is what makes the
            # claim legible as the scope limitation it is.
            reading += "; they resolve in no imported key"
            if len(checked) > 1:
                reading += f" — checked against {', '.join(checked)}"
            evidence["checked_against"] = checked
        nominations.append(
            _nomination(
                frame,
                REFERENTIAL,
                "referential",
                found["params"],
                found["result"],
                reading,
                evidence,
            )
        )
    return nominations


# ---------------------------------------------------------------- comparison
def _hold_rate(
    df: pl.DataFrame, left: str, right: str, mode: str, op: str
) -> tuple[int, int, frozenset[int]] | None:
    """(rows holding ``left op right``, rows comparable, rows breaching it).

    Measured through the test's own coercion so a nomination cannot report a
    rate the spec it nominates fails to reproduce. The breaching rows travel as
    positions rather than as values: two orderings broken by the same rows are
    one finding, and nothing but the identity of those rows can establish that.
    """
    try:
        tagged = df.with_row_index("_i").with_columns(
            analytics.comparable_expr(df, left, mode).alias("_l"),
            analytics.comparable_expr(df, right, mode).alias("_r"),
        ).drop_nulls(subset=["_l", "_r"])
    except Exception:  # noqa: BLE001 - an uncoercible column is not a failure
        return None
    total = tagged.height
    if not total:
        return None
    holds = pl.col("_l") <= pl.col("_r") if op == "le" else pl.col("_l") >= pl.col("_r")
    breached = tagged.filter(~holds)
    return total - breached.height, total, frozenset(breached["_i"].to_list())


def _comparable_magnitude(df: pl.DataFrame, left: str, right: str) -> bool:
    """Whether two numeric columns measure the same kind of quantity."""
    try:
        medians = df.select(
            pl.col(left).cast(pl.Float64, strict=False).median().alias("l"),
            pl.col(right).cast(pl.Float64, strict=False).median().alias("r"),
        ).row(0)
    except Exception:  # noqa: BLE001
        return False
    left_median, right_median = medians
    if not left_median or not right_median:
        return False
    ratio = abs(left_median) / abs(right_median)
    return 1 / MAGNITUDE_RATIO <= ratio <= MAGNITUDE_RATIO


def _chosen_direction(
    usable: list[tuple[str, int, int, frozenset[int]]]
) -> tuple[str, int, int, frozenset[int]] | None:
    """Which way round a pair of columns states something, if either does.

    The obvious rule — take whichever direction holds more often — is wrong in
    the one case that matters most. Two columns that are usually *equal* satisfy
    both directions: an invoice billed at exactly its purchase-order total is
    both at most and at least that total. So ``≥`` holds on every row while
    ``≤`` holds on all but the three that overbill, and taking the higher rate
    picks the direction with nothing in it and discards the ceiling the
    population is actually asserting. That is how a 79.75M overbilling went
    unproposed while its mirror image was recorded as a confirmed invariant.

    A direction that no row breaches, beside one that some rows do, is therefore
    read as the artifact of equality it is. Where neither direction is breached
    the two columns are equal wherever both are present, which is an identity
    rather than an ordering, and is not a comparison worth proposing at all.
    """
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    breached = [item for item in usable if item[2] - item[1]]
    if not breached:
        return None
    if len(breached) < len(usable):
        return max(breached, key=lambda item: (item[1] / item[2], item[0]))
    return max(usable, key=lambda item: (item[1] / item[2], item[0]))


def comparison_nominations(
    frame: str, df: pl.DataFrame, classes: Mapping[str, list[str]]
) -> list[dict]:
    """Orderings and ceilings the population states about itself.

    For every pair of comparable columns, both directions are measured and the
    one that nearly always holds is the relationship the data asserts. Its
    residue is the exception set. A pair that holds half the time each way is two
    unrelated columns and is dropped here rather than proposed, argued against in
    a prompt, and then executed to report that dates differ.
    """
    candidates: list[tuple] = []
    for mode, class_name in (("date", "date"), ("number", "numeric")):
        columns = classes.get(class_name) or []
        for index, left in enumerate(columns):
            for right in columns[index + 1 :]:
                if mode == "number" and not _comparable_magnitude(df, left, right):
                    continue
                usable = [
                    (op, *counts)
                    for op in ("le", "ge")
                    for counts in (_hold_rate(df, left, right, mode, op),)
                    if counts is not None
                    and counts[1] >= MIN_COMPARISON_ROWS
                    and counts[0] / counts[1] >= INVARIANT_HOLD_RATE
                ]
                chosen = _chosen_direction(usable)
                if chosen is None:
                    continue
                op, holds, total, breached = chosen
                candidates.append((left, right, mode, op, holds, total, breached))

    # One finding, however many ways it can be said. Eight date columns on a
    # joined frame produce dozens of orderings and five of them were broken by
    # the identical five rows — the same records arriving before their own
    # purchase order, restated against every later date on the row. Ranked by
    # count they take the whole budget and leave the pair that owns a 35.2M
    # backdating population unproposed one slot below them. Equivalent orderings
    # collapse to one nomination that names the others, so the model still sees
    # that those rows are early on everything, at the cost of one slot.
    groups: dict[frozenset[int], list[tuple]] = {}
    for candidate in sorted(candidates, key=lambda item: (item[0], item[1])):
        groups.setdefault(candidate[6], []).append(candidate)

    nominations: list[dict] = []
    confirmed = 0
    for breached, members in groups.items():
        left, right, mode, op, holds, total, _ = members[0]
        breaches = len(breached)
        if not breaches:
            # No counter-example anywhere. Worth stating that the population is
            # consistent about it, and worth only a footnote's room — and these
            # are not equivalent to each other merely by all being unbroken, so
            # each is kept on its own until the budget runs out.
            for member in members:
                if confirmed >= MAX_CONFIRMED_PER_FRAME:
                    break
                confirmed += 1
                nominations.extend(
                    _comparison_nomination(frame, df, member, ())
                )
            continue
        nominations.extend(
            _comparison_nomination(frame, df, members[0], members[1:])
        )
    return nominations


def _comparison_nomination(
    frame: str, df: pl.DataFrame, candidate: tuple, equivalent: Iterable[tuple]
) -> list[dict]:
    """Run the chosen comparison and shape it, or return nothing if it will not run."""
    left, right, mode, op, holds, total, breached = candidate
    params = {"column": left, "op": op, "other": right, "compare_as": mode}
    result = _run(df, "compare_columns", params)
    if result is None:
        return []
    symbol = "≤" if op == "le" else "≥"
    breaches = len(breached)
    others = [
        f"{item[0]} {'≤' if item[3] == 'le' else '≥'} {item[1]}" for item in equivalent
    ]
    reading = (
        f"{left} {symbol} {right} holds for {holds} of {total} comparable rows"
        + (f"; {breaches} do not" if breaches else " with no counter-example")
    )
    if others:
        reading += (
            f". The same {breaches} rows also breach "
            + ", ".join(others[:3])
            + (f" and {len(others) - 3} more" if len(others) > 3 else "")
        )
    return [
        _nomination(
            frame,
            COMPARISON,
            "compare_columns",
            params,
            result,
            reading,
            {"equivalent_orderings": others} if others else None,
        )
    ]


# ------------------------------------------------------------------ equality
def _domain(df: pl.DataFrame, column: str) -> set[str]:
    try:
        series = (
            df.select(pl.col(column).cast(pl.String).str.strip_chars())
            .to_series()
            .drop_nulls()
        )
    except Exception:  # noqa: BLE001
        return set()
    return {value for value in series.unique().to_list() if value}


def equality_nominations(
    frame: str, df: pl.DataFrame, classes: Mapping[str, list[str]]
) -> list[dict]:
    """Two columns drawn from one domain, and which way they are meant to agree.

    Columns sharing a value domain are the shape segregation of duties lives in,
    and the current pipeline cannot see it at all: its whole model of a
    relationship is *between tables*, so two role columns sitting side by side on
    one row are never compared. One engagement's 98.06M exception — an invoice
    verified and approved by the same person — is a single comparison of two
    columns of the invoice table, needing no join.

    Which comparison, though, is not a statistical question. Two *roles* of one
    entity should differ, and their equal rows are the exception. Two statements
    of one *attribute* should agree, and their differing rows are. Both look
    identical from the counts, so the names decide: subjects that overlap name
    one attribute (``REQUESTER_DEPARTMENT`` inside ``DEPARTMENT``), and subjects
    that are disjoint name two roles (``VERIFIED_BY_ID`` against
    ``SUPERVISOR_APPROVAL_ID``). Both counts travel on the nomination either way,
    so a reader who disagrees with the reading can see the other one.
    """
    nominations: list[dict] = []
    columns = classes.get("keylike") or []
    domains = {column: _domain(df, column) for column in columns}
    for index, left in enumerate(columns):
        for right in columns[index + 1 :]:
            left_domain, right_domain = domains[left], domains[right]
            if not left_domain or not right_domain:
                continue
            union = left_domain | right_domain
            overlap = len(left_domain & right_domain) / len(union) if union else 0.0
            if overlap < MIN_DOMAIN_OVERLAP:
                continue
            counts = _hold_rate(df, left, right, "text", "le")
            if counts is None:
                continue
            comparable = counts[1]
            if comparable < MIN_COMPARISON_ROWS:
                continue
            equal = int(
                df.select(
                    (
                        analytics.comparable_expr(df, left, "text")
                        == analytics.comparable_expr(df, right, "text")
                    ).sum()
                ).item()
                or 0
            )
            differ = comparable - equal
            same_subject = bool(
                (subject_tokens(left) - _ROLE_MARKERS)
                & (subject_tokens(right) - _ROLE_MARKERS)
            )
            if same_subject and not equal:
                # Two columns claiming to state one attribute, agreeing on no
                # row at all, are not stating one attribute — the shared word in
                # their names is a coincidence. ``SUPERVISOR_APPROVAL_ID`` is who
                # approved an invoice and ``SUPERVISOR_ID`` is whom a staff
                # member reports to; read as one attribute they disagree on
                # every row, which is a nomination that can only waste a slot.
                continue
            op = "eq" if same_subject else "ne"
            flagged_by_reading = differ if same_subject else equal
            if not flagged_by_reading:
                continue
            params = {
                "column": left,
                "op": op,
                "other": right,
                "compare_as": "text",
            }
            result = _run(df, "compare_columns", params)
            if result is None:
                continue
            reading = (
                f"{left} and {right} name one attribute and disagree on {differ} "
                f"of {comparable} rows"
                if same_subject
                else f"{left} and {right} are two roles over one domain and are "
                f"the same party on {equal} of {comparable} rows"
            )
            nominations.append(
                _nomination(
                    frame,
                    EQUALITY,
                    "compare_columns",
                    params,
                    result,
                    reading,
                    {
                        "equal_rows": equal,
                        "differing_rows": differ,
                        "domain_overlap": round(overlap, 3),
                        "reading": "same_attribute" if same_subject else "two_roles",
                    },
                )
            )
    return nominations


# -------------------------------------------------------------------- values
# A column with few enough distinct values that naming all of them describes a
# vocabulary rather than a population. The threshold is the profiler's own bar
# for calling a column categorical, so "few" means one thing in this
# application. The share guard is what keeps a four-row lookup from having its
# population described as a vocabulary.
DOMAIN_MAX_DISTINCT = profiler.CATEGORICAL_MAX_DISTINCT
DOMAIN_MAX_SHARE = 0.5
MAX_DOMAIN_COLUMNS = 20
# A vocabulary token is a word or two. Past this the column holds prose — a
# receipt comment, a description — and its few distinct values are short only
# because the population is, not because they name a category. The word count
# is the discriminating half: "Received in full; no exceptions noted." fits in
# forty characters and is plainly a sentence, and a column holding it was being
# published as though its eight comments were eight statuses.
DOMAIN_MAX_VALUE_LENGTH = 40
DOMAIN_MAX_VALUE_WORDS = 4


def _identifies_in_lineage(workspace: Workspace, frame: str, column: str) -> bool:
    """Whether this column identifies rows in the table it actually comes from.

    A join re-profiles a dimension attribute into a category. ``NAME`` is one
    value per row in a 52-row staff table — an identifier by every measure, and
    excluded there. Joined to 118 invoices it becomes eight distinct values over
    a hundred and eighteen rows, and every test this module applies now reads it
    as a status list: few enough to name, short enough to be a token, a small
    share of the population. The column did not change. The population it was
    counted against did.

    So the column is judged where it is a fact about an entity rather than about
    how often that entity was referenced. Every staff name and email address in
    this engagement reached the model this way, on seven joined frames, as
    "vocabularies" the model was invited to write procedures against.
    """
    lineage = sorted(join_diagnostics.frame_lineage(workspace, frame) - {frame})
    # Polars suffixes a collided column on the right-hand side of a join, so the
    # joined name is not always the name its home table knows it by.
    candidates = {column, column[: -len("_right")] if column.endswith("_right") else column}
    for table in lineage:
        profile = _profile(workspace, table)
        rows = profile.get("rows")
        for item in profile.get("column_profiles") or []:
            if str(item.get("name") or "") not in candidates:
                continue
            if str(item.get("inferred_type") or "") == "id":
                return True
            distinct = item.get("distinct_count")
            if (
                isinstance(rows, int)
                and rows
                and isinstance(distinct, int)
                and distinct / rows > DOMAIN_MAX_SHARE
            ):
                return True
    return False


def value_domains(workspace: Workspace, frame: str) -> list[dict]:
    """The complete value vocabulary of each low-cardinality column.

    The one thing a value-free profile cannot supply and a procedure cannot be
    written without. "REQUISITION_STATUS has 3 distinct values" does not let
    anybody test for a requisition that was rejected; the word ``Rejected`` does,
    and no aggregate contains it.

    A vocabulary is not a population. A column with twenty distinct values across
    a thousand rows is a status list and naming its values names no record; a
    column whose values are nearly one-per-row is the population itself, and its
    values are never listed here — its *shape* is what ``format_anomaly``
    reports, and its rows are what the sample shows.

    Nor is a vocabulary a set of identifiers. A column of staff numbers is short
    only because few people appear in the population, and listing it discloses
    who they are while answering no question a procedure asks — what a reference
    column needs is reconciliation, which the sweep already does. Reference-shaped
    columns are therefore excluded even where they are small, along with prose
    columns whose values are sentences rather than categories.
    """
    try:
        profile = workspace.get_profile(frame) or {}
        df = workspace.get_frame(frame)
    except Exception:  # noqa: BLE001 - an unreadable frame has no vocabulary
        return []
    rows = profile.get("rows")
    domains: list[dict] = []
    for column in profile.get("column_profiles") or []:
        if len(domains) >= MAX_DOMAIN_COLUMNS:
            break
        name = str(column.get("name") or "").strip()
        distinct = column.get("distinct_count")
        if not name or not isinstance(distinct, int) or not distinct:
            continue
        if distinct > DOMAIN_MAX_DISTINCT or name not in df.columns:
            continue
        if str(column.get("inferred_type") or "") != "categorical":
            continue
        if reference_shaped(name):
            continue
        if isinstance(rows, int) and rows and distinct / rows > DOMAIN_MAX_SHARE:
            continue
        if _identifies_in_lineage(workspace, frame, name):
            continue
        try:
            values = sorted(
                str(value)
                for value in df[name].drop_nulls().unique().to_list()
                if str(value).strip()
            )
        except Exception:  # noqa: BLE001
            continue
        if not values or len(values) > DOMAIN_MAX_DISTINCT:
            continue
        if max(len(value) for value in values) > DOMAIN_MAX_VALUE_LENGTH:
            continue
        if max(len(value.split()) for value in values) > DOMAIN_MAX_VALUE_WORDS:
            continue
        domains.append(
            {
                "table": frame,
                "column": name,
                "distinct_count": distinct,
                "values": values,
                "blank_count": column.get("blank_count"),
            }
        )
    return domains


# A column whose vocabulary has a usual answer. Below this the column classifies
# rather than reports: eight departments at twelve percent each say what a row
# *is*, and none of them is a minority in it. Above it there is a way things
# normally are, and everything else in the column is a departure from it.
#
# Calibrated, not guessed. Across this engagement's fifteen vocabularies the
# reporting columns sit at 0.83 to 1.00 — payment status, requisition status,
# vendor status — and the classifying ones at 0.18 to 0.36. The only column in
# between is a staff department at 0.54, where "one of fifty-two people works in
# Executive" is an org chart and not an exception. Seven in ten is where the two
# kinds of column separate.
VALUE_DOMINANT_SHARE = 0.7
# Rare enough to be an exception rather than a segment of the population. A
# quarter of the rows is a second normal state; a twentieth is a handful of rows
# somebody would have to explain.
VALUE_MINORITY_SHARE = 0.2
# Per column, because a long tail is a distribution and not a list of findings.
MAX_VALUES_PER_COLUMN = 2


def _home_modal_share(workspace: Workspace, frame: str, column: str) -> float | None:
    """How dominant this column's commonest value is in the table it comes from.

    The join that concentrates a name concentrates a category too. Eight
    departments spread across fifty-two staff — the widest classifier in this
    engagement at a 54% mode — become one department covering four invoice rows
    in five, because the people in it sign far more invoices than the rest. The
    frame's own distribution then reports a normal state that the entity does
    not have, and the sweep offers "twenty rows are Procurement" as an exception.

    Read from the profile's own census, so establishing this costs no scan.

    Where a column name occurs in more than one contributing table the least
    dominant reading wins, and the lineage is walked in a fixed order: a sweep
    whose nominations depended on set iteration would move between runs on
    unchanged data, which is the one thing this module promises it cannot do.
    """
    shares: list[float] = []
    for table in sorted(join_diagnostics.frame_lineage(workspace, frame) - {frame}):
        profile = _profile(workspace, table)
        for item in profile.get("column_profiles") or []:
            if str(item.get("name") or "") != column:
                continue
            top = item.get("top_values") or []
            if not top:
                continue
            populated = int(item.get("total") or 0) - int(item.get("blank_count") or 0)
            count = top[0].get("count")
            pct = top[0].get("pct")
            if populated > 0 and isinstance(count, int):
                shares.append(count / populated)
            elif isinstance(pct, (int, float)):
                shares.append(float(pct) / 100.0)
    return min(shares) if shares else None


def value_nominations(
    workspace: Workspace, frame: str, df: pl.DataFrame
) -> list[dict]:
    """Minority states in a column that has a usual one.

    The sweep's other families ask whether two things agree. This one asks the
    question an auditor asks first and the library could not express until
    ``value_filter`` existed: *is anything in this population in a state it
    should not be in*. A requisition that was rejected and still drew an
    invoice, a vendor under review that still received a purchase order — the
    condition is a single value, and no comparison between two columns reaches
    it.

    The rule is deliberately domain-free. It knows nothing of what ``Rejected``
    means, and there is no list of suspicious words anywhere in it. It knows
    only the shape of a column that reports rather than classifies: one value
    covers most of the population, and the rest are slivers. Both halves are
    load-bearing. Without the dominance floor the sweep nominates every
    department in an evenly-spread column; without the minority ceiling it
    nominates the second-commonest payment method. Together they select the
    columns where the data itself says there is a normal case, and hand over
    what departs from it — leaving what the departure *means* to the reader,
    which is the half that is actually judgment.

    Candidates come from :func:`value_domains` and nowhere else. The definition
    worker may only name a value the vocabulary it was given contains, so a
    nomination drawn from anywhere else would be a spec the model is forbidden
    to restate — measured, offered, and unusable.
    """
    nominations: list[dict] = []
    for domain in value_domains(workspace, frame):
        column = str(domain.get("column") or "")
        vocabulary = {str(value) for value in domain.get("values") or ()}
        if column not in df.columns or not vocabulary:
            continue
        try:
            census = (
                df.select(analytics.key_strings(column).alias("_v"))
                .filter(pl.col("_v").is_not_null() & (pl.col("_v") != ""))
                .group_by("_v")
                .agg(pl.len().alias("rows"))
            )
        except Exception:  # noqa: BLE001 - a column that will not count is not one
            continue
        counts = sorted(
            ((str(value), int(rows)) for value, rows in census.iter_rows()),
            key=lambda item: (item[1], item[0]),
        )
        total = sum(rows for _, rows in counts)
        if not total or len(counts) < 2:
            continue
        usual, usual_rows = counts[-1]
        if usual_rows / total < VALUE_DOMINANT_SHARE:
            continue
        # And dominant where the column lives, not only where it was joined to.
        home = _home_modal_share(workspace, frame, column)
        if home is not None and home < VALUE_DOMINANT_SHARE:
            continue
        for value, rows in counts[:MAX_VALUES_PER_COLUMN]:
            if value not in vocabulary or rows / total > VALUE_MINORITY_SHARE:
                continue
            params = {"column": column, "mode": "flag", "values": [value]}
            result = _run(df, "value_filter", params)
            if result is None or result.detail is None or not result.detail.height:
                continue
            nominations.append(
                _nomination(
                    frame,
                    VALUES,
                    "value_filter",
                    params,
                    result,
                    f"{result.detail.height} of {total} rows hold "
                    f"'{value}' in {column}, where '{usual}' covers "
                    f"{round(100 * usual_rows / total)}% — a minority state in a "
                    f"column that has a usual one",
                    {
                        "usual_value": usual,
                        "usual_share": round(usual_rows / total, 4),
                        "distinct_values": len(counts),
                    },
                )
            )
    return nominations


# ---------------------------------------------------------------- duplicates
def duplicate_nominations(
    workspace: Workspace, frame: str, df: pl.DataFrame, profile: Mapping[str, object]
) -> list[dict]:
    """Columns that are almost a key, and the rows that stop them being one.

    A column unique on every row but a handful is a column something intended to
    be unique. That is where a reused vendor bank account and a re-billed invoice
    reference both live, and neither needs a second column to find.

    Restricted to identifier and text columns on purpose. A near-unique *date* or
    *amount* is not something intended to be unique — it is a continuous
    distribution, and the rows sharing a value share it by arithmetic. Reporting
    that eight purchase orders fall on four dates is a fact about a calendar.
    """
    rows = profile.get("rows")
    if not isinstance(rows, int) or rows < 2:
        return []
    nominations: list[dict] = []
    for column in profile.get("column_profiles") or []:
        name = str(column.get("name") or "").strip()
        distinct = column.get("distinct_count")
        blanks = int(column.get("blank_count") or 0)
        populated = rows - blanks
        if not name or not isinstance(distinct, int) or populated < 2:
            continue
        if str(column.get("inferred_type") or "") not in {"id", "text"}:
            continue
        ratio = distinct / populated
        if not NEAR_UNIQUE_MIN <= ratio < NEAR_UNIQUE_MAX:
            continue
        params = {"columns": [name]}
        result = _run(df, "duplicates", params)
        if result is None or result.detail is None or not result.detail.height:
            continue
        nominations.append(
            _nomination(
                frame,
                DUPLICATES,
                "duplicates",
                params,
                result,
                f"{name} is distinct on {distinct} of {populated} populated rows; "
                f"{result.detail.height} rows share a value with another",
            )
        )
    return nominations


# -------------------------------------------------------------------- format
def format_nominations(
    frame: str, df: pl.DataFrame, profile: Mapping[str, object]
) -> list[dict]:
    """Identifier columns where a minority of values is built differently.

    The test already refuses to conclude where no shape governs the column, so
    the sweep only has to offer it the columns that could carry an identifier
    format and keep the ones it actually flags.
    """
    nominations: list[dict] = []
    for column in profile.get("column_profiles") or []:
        name = str(column.get("name") or "").strip()
        if not name or str(column.get("inferred_type") or "") not in {"id", "text"}:
            continue
        params = {"column": name}
        result = _run(df, "format_anomaly", params)
        if result is None or result.detail is None or not result.detail.height:
            continue
        nominations.append(
            _nomination(
                frame,
                FORMAT,
                "format_anomaly",
                params,
                result,
                f"{result.detail.height} {name} values are built unlike the "
                f"shape that governs the column",
            )
        )
    return nominations


# --------------------------------------------------------------------- sweep
def _rank(nomination: Mapping[str, object]) -> tuple:
    """Most worth a turn first.

    A reconciliation gap outranks a broken ordering, which outranks a repeated
    key, because that is the order in which each is likely to be the auditor's
    finding rather than the auditor's data-quality note. Within a family the
    larger flagged population leads, and a confirmed invariant sorts last: it is
    worth stating and it is not worth choosing over something that failed.

    A minority state ranks with the broken orderings rather than with the
    repeated keys. Rows in a state the population says is unusual are a claim
    about those rows, the same standing as an amount exceeding its limit — not a
    remark about how the data was captured.

    Within the values family the order inverts, because for that family alone
    "larger" means "less exceptional". Every other family counts how badly a
    rule was broken, so more breaches is a stronger nomination. A value check
    counts how much of the population sits in a state, and its entire premise is
    that the *rare* state is the exception — 13 invoices awaiting payment is an
    operational fact, 4 rejected ones are the finding. Ranked the common way,
    ``Pending Payment`` outranked ``Rejected`` on every frame that carried both,
    was taken as the column's one nomination, and A03/A04 stayed out of reach
    through a whole run in which it was nominated eleven times.
    """
    family_rank = {
        REFERENTIAL: 0,
        COMPARISON: 1,
        EQUALITY: 1,
        VALUES: 1,
        DUPLICATES: 2,
        FORMAT: 3,
    }
    family = str(nomination.get("family"))
    flagged = int(nomination.get("flagged") or 0)
    return (
        0 if flagged else 1,
        family_rank.get(family, 9),
        flagged if family == VALUES else -flagged,
        str(nomination.get("test")),
        str(nomination.get("params")),
    )


def probe_frame(workspace: Workspace, frame: str) -> list[dict]:
    """Every measured nomination this frame supports, best first and bounded."""
    try:
        df = workspace.get_frame(frame)
    except Exception:  # noqa: BLE001 - a frame that will not load probes nothing
        return []
    profile = _profile(workspace, frame)
    classes = _column_classes(profile)
    # Named, so the orphan diagnosis cannot report that a column's unmatched
    # values resolve in that column.
    source = dataclasses.replace(workspace.frame_source(), origin=frame)
    nominations = [
        *referential_nominations(workspace, frame, df, source=source),
        *comparison_nominations(frame, df, classes),
        *equality_nominations(frame, df, classes),
        *value_nominations(workspace, frame, df),
        *duplicate_nominations(workspace, frame, df, profile),
        *format_nominations(frame, df, profile),
    ]
    nominations.sort(key=_rank)
    kept: list[dict] = []
    taken: dict[str, int] = {}
    for nomination in nominations:
        family = str(nomination.get("family"))
        if taken.get(family, 0) >= MAX_PER_FAMILY.get(family, MAX_NOMINATIONS_PER_FRAME):
            continue
        taken[family] = taken.get(family, 0) + 1
        kept.append(nomination)
        if len(kept) >= MAX_NOMINATIONS_PER_FRAME:
            break
    return kept


def probe_frames(workspace: Workspace, frames: Iterable[str]) -> dict[str, list[dict]]:
    """Sweep several frames, keeping only those with something to report."""
    swept = {}
    for frame in frames:
        found = probe_frame(workspace, frame)
        if found:
            swept[frame] = found
    return swept


__all__ = [
    "COMPARISON",
    "DUPLICATES",
    "EQUALITY",
    "FAMILIES",
    "FORMAT",
    "INVARIANT_HOLD_RATE",
    "MAX_NOMINATIONS_PER_FRAME",
    "MIN_COMPARISON_ROWS",
    "REFERENTIAL",
    "VALUES",
    "comparison_nominations",
    "duplicate_nominations",
    "equality_nominations",
    "format_nominations",
    "probe_frame",
    "probe_frames",
    "referential_nominations",
    "value_domains",
    "value_nominations",
]
