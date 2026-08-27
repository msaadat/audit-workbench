"""The assertion register: what this engagement should hold, decided once.

Before this stage existed, every question the run asked was decided inside one
frame's keyhole. A frame's definition turn saw that frame's columns, that
frame's nominations, and the analyses its join family already held — and from
there it chose what to test. Two consequences followed and both were measured.
A cross-frame reading was structurally unavailable: no unit held enough of the
engagement to say that no column anywhere records a competitive bid. And where
several frames could compute the same thing, nothing arbitrated between them;
whichever frame ran first won, alphabetically.

The register is the artifact that fixes the second and makes the first
possible. It is built in two passes:

* **The floor is deterministic.** Every frame is swept, every nomination that
  flagged rows is collected, and the collection is deduplicated by
  ``analysis_semantic_id`` — which is identity over the *computation and the
  population*, so an invoice-only check reachable from six invoice-rooted
  frames is one entry, while an approval-matrix reconciliation asked over the
  staff master and over invoices keyed to their approver stays two. On the
  procurement engagement that is 116 flagged nominations resolving to 44
  distinct computations.

* **The reading turn may keep, add, or decline — and silence is keeping.**
  This is the ``analysis.reading`` contract in one line. A nomination the turn
  never mentions is kept with a title and note derived from what the sweep
  measured, so the floor holds whether the turn is thoughtful, careless, or
  absent altogether. Subtracting requires a written reason, which is recorded.

That asymmetry is deliberate and it is the whole safety argument for spending a
single model turn on the whole engagement. One turn over everything is a single
point of failure; a turn that can only *add* to a set already established is
not, because its worst case is the floor.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import joins as join_diagnostics, probes
from .analysis_identity import analysis_semantic_id
from .. import loader
from ..workspaces import Workspace, WorkspaceError, write_json_atomic


# How the sweep's own families read as a procedure title. The nomination
# already carries a measured sentence in ``reading``; what it does not carry is
# a name, and a saved analysis needs one an auditor can scan a list by.
_COMPARISON_WORDS: dict[str, str] = {
    "lt": "is not before",
    "le": "is after",
    "gt": "is not after",
    "ge": "is before",
    "eq": "differs from",
    "ne": "is the same as",
}


@dataclass(frozen=True)
class Nomination:
    """One measured spec, addressable by a stable reference.

    ``frame`` is the narrowest frame that measures it. Where a computation is
    reachable from several frames they return the same counts by construction —
    the join added columns the spec never reads — so the narrowest is the one
    whose population the spec was actually diagnosed against, and the others are
    recorded in ``also_on`` rather than discarded silently.
    """

    ref: str
    frame: str
    root: str
    test: str
    params: Mapping[str, object]
    family: str
    signal: str
    tested: int
    flagged: int
    reading: str
    semantic_id: str
    also_on: tuple[str, ...] = ()
    #: True where this is a reference that resolves in no imported key at all.
    #: The lookup its spec names is then undetermined by the data — every
    #: candidate master fails identically — so the name must not be read as a
    #: claim about which master was the referent.
    unreferenced: bool = False

    @property
    def spec(self) -> dict[str, object]:
        return {"test": self.test, "params": dict(self.params)}

    @property
    def columns(self) -> tuple[str, ...]:
        params = self.params
        named: list[str] = []
        for key in ("column", "other"):
            value = str(params.get(key) or "").strip()
            if value:
                named.append(value)
        for value in params.get("columns") or ():
            text = str(value or "").strip()
            if text:
                named.append(text)
        return tuple(dict.fromkeys(named))

    def derived_title(self) -> str:
        """A name for this procedure, from the measurement alone."""
        params = self.params
        column = str(params.get("column") or "")
        other = str(params.get("other") or "")
        if self.family == probes.REFERENTIAL:
            if self.unreferenced:
                # Naming the lookup here would state as fact the one thing the
                # measurement could not establish. Every candidate master failed
                # identically, so the spec names one to be runnable and the
                # title says only what was found.
                return f"{column} reconciles to no imported master"
            return (
                f"{column} not found in "
                f"{params.get('lookup_table')}.{params.get('lookup_column')}"
            )
        if self.family in {probes.COMPARISON, probes.EQUALITY}:
            word = _COMPARISON_WORDS.get(str(params.get("op") or ""), "breaches")
            return f"{column} {word} {other}"
        if self.family == probes.VALUES:
            values = ", ".join(str(value) for value in params.get("values") or ())
            verb = "is not" if str(params.get("mode")) == "allow" else "is"
            return f"{column} {verb} {values}"
        if self.family == probes.DUPLICATES:
            return f"{', '.join(self.columns)} repeats"
        if self.family == probes.FORMAT:
            return f"{column} built unlike the majority"
        return f"{self.test} on {column or self.frame}"

    def derived_note(self) -> str:
        """Why this is worth saving, in the sweep's own measured words.

        The note on a model-authored analysis states a relationship the author
        expects to hold and why. A nomination's does not have to be argued for:
        it was run, and the sentence the sweep wrote is what it found. Saying
        that plainly is more use to a reader than a rationale nobody composed.
        """
        return self.reading or f"{self.flagged} of {self.tested} rows flagged."


@dataclass(frozen=True)
class Kept:
    """A nomination that survived the reading turn, with the name it carries."""

    nomination: Nomination
    title: str
    note: str
    #: ``sweep`` where the reading turn never mentioned it. The floor holding is
    #: the normal case, not an error, but the distinction is what lets a run
    #: report how much of its register the turn actually read.
    origin: str = "sweep"

    @property
    def ref(self) -> str:
        return self.nomination.ref

    def definition(self) -> dict[str, object]:
        """The saved-analysis definition this entry commits as."""
        return {
            "title": self.title,
            "kind": "analytics",
            "table": self.nomination.frame,
            "spec": self.nomination.spec,
            "note": self.note,
            "semantic_id": self.nomination.semantic_id,
        }


@dataclass(frozen=True)
class Assertion:
    """Something the reading turn says should hold that no nomination measures.

    This is the half of the register a sweep cannot reach: a comparison across
    two frames, a control the columns imply but no single measurement states.
    It carries no spec — writing one is what the definition turn is for.
    """

    ref: str
    frame: str
    columns: tuple[str, ...]
    assertion: str
    why: str


@dataclass(frozen=True)
class Declined:
    """A nomination set aside, and the reason that had to be written for it."""

    ref: str
    reason: str
    title: str = ""


@dataclass(frozen=True)
class Unanswerable:
    """A question the engagement's data cannot answer at all.

    Negative space is a finding an auditor needs and no procedure produces: a
    control the file gives no column to test. It reaches the memo through the
    register rather than through a result, because there is no result to have.
    """

    question: str
    why: str


@dataclass(frozen=True)
class Register:
    """The ordered register, after the reading turn has had its say."""

    kept: tuple[Kept, ...] = ()
    authored: tuple[Assertion, ...] = ()
    declined: tuple[Declined, ...] = ()
    unanswerable: tuple[Unanswerable, ...] = ()
    #: True where a reading turn actually contributed. A register standing on
    #: its floor alone is a valid register and a different thing to report.
    read: bool = False

    def frames(self) -> tuple[str, ...]:
        """Frames carrying an authored assertion, in register order.

        Only authored assertions expand a definition turn. A kept nomination is
        already a complete spec with its counts measured, so re-deriving it
        through a model call would spend a turn to reproduce an exact answer —
        and, measured across four runs, would sometimes decline to.
        """
        return tuple(dict.fromkeys(item.frame for item in self.authored))

    def assertions_for(self, frame: str) -> tuple[Assertion, ...]:
        return tuple(item for item in self.authored if item.frame == frame)

    def payload(self) -> dict[str, object]:
        """The durable projection persisted on the run record."""
        return {
            "read": self.read,
            "kept": [
                {
                    "ref": item.ref,
                    "frame": item.nomination.frame,
                    "title": item.title,
                    "note": item.note,
                    "origin": item.origin,
                    "spec": item.nomination.spec,
                    "flagged": item.nomination.flagged,
                    "tested": item.nomination.tested,
                    "semantic_id": item.nomination.semantic_id,
                    **(
                        {"also_on": list(item.nomination.also_on)}
                        if item.nomination.also_on
                        else {}
                    ),
                }
                for item in self.kept
            ],
            "authored": [
                {
                    "ref": item.ref,
                    "frame": item.frame,
                    "columns": list(item.columns),
                    "assertion": item.assertion,
                    "why": item.why,
                }
                for item in self.authored
            ],
            "declined": [
                {"ref": item.ref, "title": item.title, "reason": item.reason}
                for item in self.declined
            ],
            "unanswerable": [
                {"question": item.question, "why": item.why}
                for item in self.unanswerable
            ],
        }


def _narrowness(workspace: Workspace, frame: str) -> tuple[int, str]:
    """How few tables a frame is built from, then its name for determinism."""
    try:
        lineage = join_diagnostics.frame_lineage(workspace, frame)
    except Exception:  # noqa: BLE001 - an unresolvable frame sorts last
        return (99, frame)
    return (len(lineage), frame)


def _unreferenced(item: Mapping[str, object]) -> bool:
    """Whether a reconciliation found its values in no imported key at all."""
    if str(item.get("test") or "") != "referential":
        return False
    evidence = item.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    return bool(evidence.get("checked_against")) and not evidence.get("resolves_in")


def _exhaustiveness(item: Mapping[str, object]) -> int:
    evidence = item.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    return len(evidence.get("checked_against") or ())


def build_floor(
    workspace: Workspace, swept: Mapping[str, Sequence[Mapping[str, object]]]
) -> tuple[Nomination, ...]:
    """The deterministic register floor over every swept frame.

    Only nominations that flagged rows enter. A nomination flagging none is a
    confirmed invariant — worth a line in a memo, never a procedure competing
    for a slot — and the sweep already ranks it last for the same reason.

    Two collapses happen here and they are different. The first is identity:
    ``analysis_semantic_id`` folds the same computation over the same
    population, so an invoice-only check reachable from six invoice-rooted
    frames is one entry. The second is narrower and is about a question the
    data leaves open. Where a reference resolves in *no* imported key, every
    candidate master fails at exactly the same rate, and which one the sweep
    named is decided by nothing better than alphabetical order among the tables
    outside that frame's lineage. Eight nominations on the procurement
    engagement said "BUYER_ID reconciles to nothing" against four different
    masters over two populations. They are folded on ``(root, column)``, and
    the survivor is the one that ruled out the most masters — the strongest
    statement of the same finding, rather than the first-sorted one.
    """
    grouped: dict[
        object, list[tuple[tuple[int, str], Mapping[str, object], str, str]]
    ] = {}
    for frame in sorted(swept):
        try:
            origins = join_diagnostics.column_origins(workspace, frame)
            root = join_diagnostics.frame_root(workspace, frame)
            route = join_diagnostics.frame_route(workspace, frame)
        except Exception:  # noqa: BLE001 - a frame that will not resolve is skipped
            continue
        for item in swept[frame] or ():
            if int(item.get("flagged") or 0) <= 0:
                continue
            spec = {"test": str(item.get("test")), "params": dict(item.get("params") or {})}
            semantic = analysis_semantic_id(
                "analytics", frame, spec, origins, root, route
            )
            key: object = semantic
            if _unreferenced(item):
                key = ("unreferenced", root, str((item.get("params") or {}).get("column")))
            grouped.setdefault(key, []).append(
                (_narrowness(workspace, frame), item, frame, root)
            )

    entries: list[tuple] = []
    for key, members in grouped.items():
        if isinstance(key, tuple):
            # An exhaustive negative outranks a partial one; then the narrowest
            # frame, then the lookup name, so the choice is reproducible.
            members.sort(
                key=lambda member: (
                    -_exhaustiveness(member[1]),
                    member[0],
                    str((member[1].get("params") or {}).get("lookup_table")),
                )
            )
        else:
            members.sort(key=lambda member: member[0])
        _, item, frame, root = members[0]
        also_on = tuple(
            other for _, _, other, _ in members[1:] if other != frame
        )
        semantic = key if isinstance(key, str) else analysis_semantic_id(
            "analytics",
            frame,
            {"test": str(item.get("test")), "params": dict(item.get("params") or {})},
            join_diagnostics.column_origins(workspace, frame),
            root,
            join_diagnostics.frame_route(workspace, frame),
        )
        entries.append(
            (probes.rank(item), semantic, item, frame, root, also_on)
        )
    # The sweep's own ranking, applied across the engagement rather than within
    # one frame: what does not reconcile, then what does not hold, then what
    # repeats, then what does not look like the rest.
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return tuple(
        Nomination(
            ref=f"N{index:02d}",
            frame=frame,
            root=root,
            test=str(item.get("test")),
            params=dict(item.get("params") or {}),
            family=str(item.get("family") or ""),
            signal=str(item.get("signal") or ""),
            tested=int(item.get("tested") or 0),
            flagged=int(item.get("flagged") or 0),
            reading=str(item.get("reading") or ""),
            semantic_id=semantic,
            also_on=also_on,
            unreferenced=_unreferenced(item),
        )
        for index, (_, semantic, item, frame, root, also_on) in enumerate(entries, 1)
    )


def default_register(floor: Iterable[Nomination]) -> Register:
    """The register with no reading turn: every nomination, kept as measured.

    This is what the run holds if ``analysis.reading`` is skipped, fails after
    its repair turn, or returns nothing usable. It is a complete, committable
    register — which is the property that makes one cross-cutting model turn
    safe to depend on.
    """
    return Register(
        kept=tuple(
            Kept(item, item.derived_title(), item.derived_note(), "sweep")
            for item in floor
        ),
        read=False,
    )


def merge(floor: Sequence[Nomination], decisions: Mapping[str, object]) -> Register:
    """Apply one reading turn's keep / add / decline over the floor.

    Additive by default: a nomination the turn neither kept nor declined is
    kept anyway, under the title and note the sweep's own measurement derives.
    The turn's contribution is the naming, the ordering, what it adds, and the
    small number of things it can argue should not be tested at all.
    """
    by_ref = {item.ref: item for item in floor}
    kept: list[Kept] = []
    declined: list[Declined] = []
    named: dict[str, tuple[str, str]] = {}
    order: list[str] = []

    for entry in decisions.get("keep") or ():
        if not isinstance(entry, Mapping):
            continue
        ref = str(entry.get("ref") or "").strip()
        if ref not in by_ref or ref in named:
            continue
        title = str(entry.get("title") or "").strip()
        note = str(entry.get("note") or "").strip()
        named[ref] = (title, note)
        order.append(ref)

    dropped: dict[str, str] = {}
    for entry in decisions.get("decline") or ():
        if not isinstance(entry, Mapping):
            continue
        ref = str(entry.get("ref") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        # A decline with no reason is not a decline. Requiring the reason is
        # what stops "subtract with a recorded reason" from decaying into
        # "subtract", and the validator rejects it before this point; this is
        # the second guard rather than the first.
        if ref not in by_ref or ref in named or not reason:
            continue
        dropped[ref] = reason

    # Register order: what the turn spoke about first, then the floor's own
    # ranking for everything it did not mention.
    for ref in [*order, *(item.ref for item in floor if item.ref not in named)]:
        if ref in dropped:
            continue
        nomination = by_ref[ref]
        title, note = named.get(ref, ("", ""))
        kept.append(
            Kept(
                nomination,
                title or nomination.derived_title(),
                note or nomination.derived_note(),
                "reading" if ref in named else "sweep",
            )
        )
    declined = [
        Declined(ref, reason, by_ref[ref].derived_title())
        for ref, reason in dropped.items()
    ]

    authored = []
    for index, entry in enumerate(decisions.get("add") or (), 1):
        if not isinstance(entry, Mapping):
            continue
        authored.append(
            Assertion(
                ref=f"R{index:02d}",
                frame=str(entry.get("frame") or ""),
                columns=tuple(
                    str(value) for value in entry.get("columns") or () if str(value)
                ),
                assertion=str(entry.get("assertion") or ""),
                why=str(entry.get("why") or ""),
            )
        )

    unanswerable = [
        Unanswerable(str(entry.get("question") or ""), str(entry.get("why") or ""))
        for entry in decisions.get("unanswerable") or ()
        if isinstance(entry, Mapping) and str(entry.get("question") or "").strip()
    ]
    return Register(
        kept=tuple(kept),
        authored=tuple(authored),
        declined=tuple(declined),
        unanswerable=tuple(unanswerable),
        read=True,
    )


def from_payload(payload: Mapping[str, object] | None) -> Register:
    """Rehydrate a register persisted on the run record.

    A resumed run must not re-sweep and must not re-bill the reading turn, so
    the persisted projection is the authoritative register once written.
    """
    if not isinstance(payload, Mapping):
        return Register()
    kept = []
    for entry in payload.get("kept") or ():
        if not isinstance(entry, Mapping):
            continue
        spec = entry.get("spec")
        spec = spec if isinstance(spec, Mapping) else {}
        nomination = Nomination(
            ref=str(entry.get("ref") or ""),
            frame=str(entry.get("frame") or ""),
            root="",
            test=str(spec.get("test") or ""),
            params=dict(spec.get("params") or {}),
            family="",
            signal="",
            tested=int(entry.get("tested") or 0),
            flagged=int(entry.get("flagged") or 0),
            reading=str(entry.get("note") or ""),
            semantic_id=str(entry.get("semantic_id") or ""),
            also_on=tuple(str(name) for name in entry.get("also_on") or ()),
        )
        kept.append(
            Kept(
                nomination,
                str(entry.get("title") or ""),
                str(entry.get("note") or ""),
                str(entry.get("origin") or "sweep"),
            )
        )
    authored = tuple(
        Assertion(
            ref=str(entry.get("ref") or ""),
            frame=str(entry.get("frame") or ""),
            columns=tuple(str(value) for value in entry.get("columns") or ()),
            assertion=str(entry.get("assertion") or ""),
            why=str(entry.get("why") or ""),
        )
        for entry in payload.get("authored") or ()
        if isinstance(entry, Mapping)
    )
    declined = tuple(
        Declined(
            str(entry.get("ref") or ""),
            str(entry.get("reason") or ""),
            str(entry.get("title") or ""),
        )
        for entry in payload.get("declined") or ()
        if isinstance(entry, Mapping)
    )
    unanswerable = tuple(
        Unanswerable(str(entry.get("question") or ""), str(entry.get("why") or ""))
        for entry in payload.get("unanswerable") or ()
        if isinstance(entry, Mapping)
    )
    return Register(
        kept=tuple(kept),
        authored=authored,
        declined=declined,
        unanswerable=unanswerable,
        read=bool(payload.get("read")),
    )


# --------------------------------------------------------------------------- #
# Settled frames
# --------------------------------------------------------------------------- #
# A frame the definition stage deliberately left carrying nothing — because the
# register placed no assertion on it, because every analysis it supports is
# already saved against a frame built from the same tables, or because every
# proposal made for it flagged so much of its population that it established
# nothing — is decided, not outstanding. Nothing in the workspace recorded that
# decision, and both ``analysis.register_ready`` and
# ``analysis.definitions_ready`` are phrased as "does every scoped frame carry
# an analysis". So a completed engagement read as unfinished on every later
# run: the register stage re-expanded, re-spent its whole-engagement reading
# turn, and the definition, execution and summary stages re-expanded behind it.
# Measured on the procurement engagement, 7 of 17 definable frames were
# deliberately empty, the chain never settled, and a rerun asked only for a
# summary re-read the whole map first. This ledger is that missing record, and
# it is the frame-scoped sibling of :func:`joins.settle_pair`.
#
# It lives beside the profile cache rather than in the manifest for the same
# reason relationship evidence does: a judgement about what a frame can be
# asked is a recomputable diagnostic about data files, not an engagement
# artifact an auditor owns. Each record carries the frame's content signature,
# so replacing a table — or rebuilding the join above it — reopens exactly the
# question that data answered.
_SETTLED_FRAMES_FILENAME = "analysis_frames.json"


def _settled_frames_path(workspace: Workspace) -> Path:
    return workspace.data_dir / loader.CACHE_DIRNAME / _SETTLED_FRAMES_FILENAME


def _frame_signature(workspace: Workspace, frame: str) -> str:
    digest = repr(workspace.content_signature(frame))
    return hashlib.sha1(digest.encode()).hexdigest()[:16]


def _read_settled_frames(workspace: Workspace) -> dict:
    try:
        payload = json.loads(
            _settled_frames_path(workspace).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    frames = payload.get("frames")
    return dict(frames) if isinstance(frames, dict) else {}


def settled_frames(workspace: Workspace) -> set[str]:
    """Frames recorded as carrying no analysis on purpose.

    A record whose frame has changed, or whose frame is gone, is ignored rather
    than deleted: the question it answered is open again, and the next run
    answers it from the data that is there now.
    """
    settled: set[str] = set()
    for name, record in _read_settled_frames(workspace).items():
        if not isinstance(record, dict):
            continue
        try:
            current = _frame_signature(workspace, str(name))
        except (WorkspaceError, OSError):
            continue
        if record.get("signature") == current:
            settled.add(str(name))
    return settled


def settle_frame(workspace: Workspace, frame: str, reason: str) -> None:
    """Record that this frame is meant to carry no analysis, and why."""
    try:
        signature = _frame_signature(workspace, frame)
    except (WorkspaceError, OSError):
        return
    frames = _read_settled_frames(workspace)
    frames[str(frame)] = {
        "signature": signature,
        "reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = _settled_frames_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"frames": frames})
    except OSError:
        pass  # best-effort, like the profile cache it sits beside


def settled_reasons(workspace: Workspace) -> dict[str, str]:
    """Why each currently-settled frame carries nothing, for the run record."""
    current = settled_frames(workspace)
    return {
        name: str(record.get("reason") or "")
        for name, record in _read_settled_frames(workspace).items()
        if name in current and isinstance(record, dict)
    }


__all__ = [
    "Assertion",
    "Declined",
    "Kept",
    "Nomination",
    "Register",
    "Unanswerable",
    "build_floor",
    "default_register",
    "from_payload",
    "merge",
    "settle_frame",
    "settled_frames",
    "settled_reasons",
]
