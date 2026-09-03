"""Redundancy between Data Tests, read from the records they actually flagged.

Two tests are redundant when they put the same records in front of the auditor,
not when they are written alike.  Nothing on the creation paths can see this.
Promotion from an exploratory analysis keys its idempotency on the analysis it
came from (``datatest:promoted:<analysis_id>``), and RCM test generation keys
its reuse on ``(rcm_id, slug(title))``; both are answering "have I already
turned *this source* into a test", which is a different question from "does a
test already flag these records".  So a second analysis that finds the same
exceptions, or a second RCM row describing the same control, produces a second
test and no guard fires.

Comparing definitions does not recover the answer.  Duplicates reach the same
rows through genuinely different predicates and join paths — ``SENT_DATE >
VALUE_DATE`` and ``SENT_DATE > SETTLEMENT_DATE`` selected an identical 34 deals
in one engagement — and normalising code far enough to match those also matches
tests that are not duplicates at all.  The evidence that survives both problems
is the executed exception frame, which exists only after a run.  Hence a
post-run detector.

It marks and never deletes.  A redundant test is still a real procedure with its
own working paper, conclusion and sign-off, and choosing which member of a
duplicate group to retire is an audit judgement about populations and scope —
one test may be the better-scoped statement of the control even though both flag
the same rows today.  The mark says "these agree"; the auditor says what to do
about it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import data_tests, exception_profile
from .workspaces import Workspace, WorkspaceError

# Two tests sharing fewer records than this are reported as overlapping only
# when neither contains the other.  A pair that shares half its records is
# worth an auditor's eye; a pair sharing one row out of ninety is the ordinary
# consequence of two controls touching the same population.
_MIN_JACCARD = 0.5

IDENTICAL = "identical"
SUBSUMES = "subsumes"
SUBSUMED_BY = "subsumed_by"
OVERLAPS = "overlaps"

# What one test's mark says about it, strongest claim first.
DUPLICATE = "duplicate"
SUBSUMED = "subsumed"
SUBSUMING = "subsumes"
OVERLAPPING = "overlaps"
CLEAR = "clear"
NOT_COMPARABLE = "not_comparable"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Signature:
    """One test's exception frame, reduced to the identities it flagged.

    Every identifier column is kept, not just the entity key the profile
    resolved.  That is the point: duplicates routinely disagree about which
    population they are testing — one declares ``06_settlements`` and keys on
    ``SETTLEMENT_ID``, another declares ``05_confirmations`` and keys on
    ``CONFIRMATION_ID`` — while both carry the ``DEAL_ID`` that shows they
    flagged the same deals.  Comparing only declared entity keys would miss
    exactly the duplicates this exists to find.
    """

    __slots__ = ("test_id", "entity_key", "row_count", "identifiers", "result_sha1")

    def __init__(
        self,
        test_id: str,
        entity_key: str,
        row_count: int,
        identifiers: dict[str, frozenset[str]],
        result_sha1: str,
    ) -> None:
        self.test_id = test_id
        self.entity_key = entity_key
        self.row_count = row_count
        self.identifiers = identifiers
        self.result_sha1 = result_sha1

    def identifies_records(self, key: str) -> bool:
        """Whether ``key`` names one record per exception row in this frame.

        A key repeated down the frame is a grouping — a dealer, a counterparty —
        not the record that was flagged.  Matching on one is weaker evidence
        than matching on a per-row identifier, and the confidence of a pairing
        turns on it.
        """
        values = self.identifiers.get(key)
        return bool(values) and len(values) == self.row_count


def _identifier_values(result: dict) -> dict[str, frozenset[str]]:
    frame = result.get("exception_frame") or {}
    columns = list(frame.get("columns") or [])
    rows = frame.get("rows") or []
    if not columns or not rows:
        return {}
    values: dict[str, frozenset[str]] = {}
    for position, name in enumerate(columns):
        if name in exception_profile.INTERNAL_COLUMNS:
            continue
        if not exception_profile.key_rank(name):
            continue
        column = [row[position] for row in rows if position < len(row)]
        if len(column) != len(rows) or any(value is None for value in column):
            # A key that is null anywhere in the frame identifies nothing there,
            # and a set built from the rows that do have one would compare a
            # test against a silently smaller version of itself.
            continue
        values[name] = frozenset(str(value) for value in column)
    return values


def signature(workspace: Workspace, item: dict) -> Signature | None:
    """One test's signature, or ``None`` when there is nothing to compare.

    A test that has not run, passed cleanly, or whose result no longer reads is
    not evidence of anything about another test.  Passing tests are the honest
    gap here: two tests that both find nothing may well be duplicates, but their
    results cannot say so, and the definition comparison that could is the one
    already established as unreliable.
    """
    last_run = item.get("last_run") or {}
    run_id = str(last_run.get("id") or "")
    if not run_id:
        return None
    try:
        result = data_tests._read_result(workspace, str(item["id"]), run_id)
    except WorkspaceError:
        # An unreadable or superseded result is a fact about that test, not a
        # reason to abandon the scan of every other one.
        return None
    identifiers = _identifier_values(result)
    if not identifiers:
        return None
    profile = result.get("exception_profile") or {}
    return Signature(
        test_id=str(item["id"]),
        entity_key=str(profile.get("entity_key") or ""),
        row_count=len(((result.get("exception_frame") or {}).get("rows")) or []),
        identifiers=identifiers,
        result_sha1=str(result.get("result_sha1") or ""),
    )


def _comparison_key(left: Signature, right: Signature) -> str:
    """The shared identifier that makes the most specific claim about the pair.

    Finest grain wins: two tests agreeing on which *deals* they flagged say far
    more than two tests agreeing they both concern dealer ``TS-005``.  Ranking
    by the size of the combined value set picks the record-level key over the
    grouping one without needing to know which table each came from.
    """
    shared = set(left.identifiers) & set(right.identifiers)
    if not shared:
        return ""
    return max(
        shared,
        key=lambda name: (
            # Identifying one record per row on both sides outranks everything:
            # it is the difference between "these flagged the same records" and
            # "these both concern dealer TS-005".
            left.identifies_records(name) and right.identifies_records(name),
            len(left.identifiers[name] | right.identifiers[name]),
            exception_profile.key_rank(name),
            name,
        ),
    )


def relate(left: Signature, right: Signature) -> dict | None:
    """How two tests' flagged records stand to each other, or ``None``."""
    key = _comparison_key(left, right)
    if not key:
        return None
    a, b = left.identifiers[key], right.identifiers[key]
    shared = a & b
    if not shared:
        return None
    if a == b:
        relation = IDENTICAL
    elif shared == b:
        relation = SUBSUMES
    elif shared == a:
        relation = SUBSUMED_BY
    elif len(shared) / len(a | b) >= _MIN_JACCARD:
        relation = OVERLAPS
    else:
        return None
    return {
        "relation": relation,
        "key": key,
        "shared": len(shared),
        "left_records": len(a),
        "right_records": len(b),
        "jaccard": round(len(shared) / len(a | b), 4),
        # Whether the key counts one record per flagged row on both sides. A
        # pairing made on a grouping column is real but weaker, and saying so is
        # what keeps an advisory mark honest rather than merely confident.
        #
        # Agreement on a single record is held to the higher bar of both tests
        # having resolved this column as the record they are about. Every
        # column is trivially one-per-row in a one-row frame, so without that
        # bar any two tests flagging one row each are "identical" the moment
        # they share a dealer or a counterparty carried in by a join.
        "confidence": (
            "confirmed"
            if left.identifies_records(key)
            and right.identifies_records(key)
            and (len(shared) > 1 or key == left.entity_key == right.entity_key)
            else "possible"
        ),
    }


_INVERSE = {
    IDENTICAL: IDENTICAL,
    SUBSUMES: SUBSUMED_BY,
    SUBSUMED_BY: SUBSUMES,
    OVERLAPS: OVERLAPS,
}


def _group_id(members: list[str]) -> str:
    return "DUP-" + data_tests._sha1(sorted(members))[:8].upper()


def _state(test_id: str, peers: list[dict], groups: dict[str, str]) -> str:
    if test_id in groups:
        return DUPLICATE
    # Only a pairing made on a per-record key sets this test's state. Pairings
    # on a grouping column stay visible in ``peers`` — an auditor may want to
    # look — but they are not a claim about the test, and letting them set one
    # would report every test touching a single dealer as related to the rest.
    relations = {
        peer["relation"] for peer in peers if peer["confidence"] == "confirmed"
    }
    if SUBSUMED_BY in relations:
        return SUBSUMED
    if SUBSUMES in relations:
        return SUBSUMING
    if OVERLAPS in relations:
        return OVERLAPPING
    return CLEAR


def scan(workspace: Workspace) -> dict:
    """Compare every runnable Data Test against every other. Read-only.

    Deliberately whole-workspace rather than per-RCM-row: the duplicates that
    matter most are the ones spread across different RCM rows, which a scan
    scoped to one row could never see.
    """
    items = list(workspace.data_tests)
    signatures = {}
    for item in items:
        found = signature(workspace, item)
        if found is not None:
            signatures[str(item["id"])] = found

    peers: dict[str, list[dict]] = {test_id: [] for test_id in signatures}
    identical_edges: list[tuple[str, str]] = []
    ordered = sorted(signatures)
    for index, left_id in enumerate(ordered):
        for right_id in ordered[index + 1 :]:
            found = relate(signatures[left_id], signatures[right_id])
            if found is None:
                continue
            peers[left_id].append({"test_id": right_id, **found})
            peers[right_id].append(
                {
                    **found,
                    "test_id": left_id,
                    "relation": _INVERSE[found["relation"]],
                    "left_records": found["right_records"],
                    "right_records": found["left_records"],
                }
            )
            if found["relation"] == IDENTICAL and found["confidence"] == "confirmed":
                identical_edges.append((left_id, right_id))

    # Duplicate groups are the connected components of "flagged exactly the
    # same records". Union-find over a handful of edges, kept explicit because
    # the transitive case is real: four tests each identical to the others.
    #
    # Only confirmed edges may merge. Transitivity is what makes this strict:
    # one weak edge between two genuine groups would fuse them, so a single
    # coincidence on a grouping column could otherwise swallow half the
    # workspace into one bogus "duplicate group".
    parent = {test_id: test_id for test_id in signatures}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left_id, right_id in identical_edges:
        left_root, right_root = find(left_id), find(right_id)
        if left_root != right_root:
            parent[right_root] = left_root

    components: dict[str, list[str]] = {}
    for test_id in signatures:
        components.setdefault(find(test_id), []).append(test_id)

    membership: dict[str, str] = {}
    groups = []
    for members in components.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        group_id = _group_id(members)
        for test_id in members:
            membership[test_id] = group_id
        first = signatures[members[0]]
        key = _comparison_key(first, signatures[members[1]])
        groups.append(
            {
                "group_id": group_id,
                "members": members,
                "key": key,
                "records": len(first.identifiers.get(key) or ()),
            }
        )

    titles = {str(item["id"]): str(item.get("title") or "") for item in items}
    marks = {}
    for test_id, found in peers.items():
        found.sort(key=lambda peer: (peer["relation"], -peer["shared"], peer["test_id"]))
        marks[test_id] = {
            "state": _state(test_id, found, membership),
            "group_id": membership.get(test_id, ""),
            "peers": [{**peer, "title": titles.get(peer["test_id"], "")} for peer in found],
            "result_sha1": signatures[test_id].result_sha1,
            "checked_at": _utcnow(),
        }
    for item in items:
        test_id = str(item["id"])
        if test_id not in marks:
            marks[test_id] = {
                "state": NOT_COMPARABLE,
                "group_id": "",
                "peers": [],
                "result_sha1": "",
                "checked_at": _utcnow(),
            }
    return {
        "groups": sorted(groups, key=lambda group: (-len(group["members"]), group["group_id"])),
        "marks": marks,
        "compared": len(signatures),
        "total": len(items),
    }


def _material(mark: dict) -> tuple:
    """The part of a mark worth a workspace revision.

    ``checked_at`` moves on every scan and says nothing an auditor acts on, so
    a re-scan that finds the same redundancy must not advance the revision or
    dirty the artifact for reconciliation.
    """
    return (
        mark.get("state"),
        mark.get("group_id"),
        mark.get("result_sha1"),
        tuple(
            (peer["test_id"], peer["relation"], peer["shared"], peer["confidence"])
            for peer in mark.get("peers") or []
        ),
    )


def annotate(workspace: Workspace, *, persist: bool = True) -> dict:
    """Scan, then write each test's mark onto its record.

    Returns the scan payload either way, so a caller that only wants to show
    the finding can pass ``persist=False`` and leave the workspace untouched.
    """
    outcome = scan(workspace)
    changed = False
    for item in workspace.data_tests:
        mark = outcome["marks"].get(str(item["id"]))
        if mark is None:
            continue
        existing = item.get("redundancy") or {}
        if _material(existing) != _material(mark):
            changed = True
            item["redundancy"] = mark
        elif existing:
            # Same finding: keep the stored timestamp so the record does not
            # churn, but let a first-ever scan through.
            item["redundancy"] = {**mark, "checked_at": existing.get("checked_at") or mark["checked_at"]}
    if persist and changed:
        workspace.save()
    outcome["persisted"] = bool(persist and changed)
    return outcome
