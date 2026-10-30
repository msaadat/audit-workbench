"""Which exception observations report one issue, decided before drafting.

Eighteen drafted findings covering eleven issues is what one-observation-at-a-
time drafting produces: the finding worker sees one observation, its test and
its exception rows, and cannot see that the observation beside it names the
same invoices. Whether two observations are one finding is sometimes a fact —
three tests flagged the same three invoices — and sometimes a root-cause
judgment — three controls at successive stages failed on one inactive vendor.
The first is collapsed on the row by the roll-up (``rcm_execution`` and the
redundancy marks). The second is *proposed* by one model turn that sees every
exception observation, and decided before any finding is written.

That ordering is the point. Grouping drafts after they exist paid for every
member's narrative, threw all but one away, and paid again for the lead's
redraft from every member. Nothing the grouping turn reads comes from a draft —
the row, the test, the records each result flagged and the overlaps between
them all exist once the roll-up has run — so it is asked of the observations
instead, and an accepted group is drafted once, as one finding.

The decision is recorded on the observations themselves. The group's lead
carries ``consolidation.role = "lead"`` and its members; each member carries
``role = "member"`` and ``into``, and finding expansion skips a member the way
it skips a ``covered_by`` duplicate. A suggestion set is keyed by its *basis*:
the exception observations' ids and the hash of each one's execution result.
Accepting, dismissing and drafting move nothing in it, so the same question is
answered from disk; a result that changes, or a new exception, re-asks it.

A group accepted after some of its members were drafted — a shared-cause
suggestion an auto run left for the auditor, say — goes through the merge in
:func:`findings.consolidate`, and the members still undrafted join the lead by
reference. Either way exactly one finding reports the issue.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .workspaces import Workspace, WorkspaceError, write_json_atomic

FOLDER = ".consolidation"
SCHEMA = 2
#: What a group claims about its members. ``same_condition``: the same
#: exception observed more than once. ``shared_cause``: different control
#: failures with one root cause.
RELATIONS = ("same_condition", "shared_cause")
#: What a group's claim rests on. ``entity``: the members flag the same
#: records. ``process``: the members sit in one process and share no record.
BASES = ("entity", "process")
DECISIONS = ("accepted", "dismissed")
#: Who may decide. ``agent`` is an auto run accepting a same-condition group,
#: the one claim the overlap table settles without judgment.
DECIDERS = ("auditor", "agent")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def folder(workspace: Workspace) -> Path:
    return workspace.root / "Findings" / FOLDER


def path(workspace: Workspace, basis: str) -> Path:
    return folder(workspace) / f"{basis}.json"


# --------------------------------------------------------------------------- #
# Roles, on findings and on observations
# --------------------------------------------------------------------------- #
def is_absorbed(item: Mapping) -> bool:
    return str((item.get("consolidation") or {}).get("role") or "") == "absorbed"


def is_lead(item: Mapping) -> bool:
    return str((item.get("consolidation") or {}).get("role") or "") == "lead"


def is_member(observation: Mapping) -> bool:
    """An observation grouped into another, and drafted only as part of it."""
    return str((observation.get("consolidation") or {}).get("role") or "") == "member"


def group_id(member_ids: list[str] | tuple[str, ...]) -> str:
    """A group's id is a function of its members, so the same proposal made
    twice names the same group."""
    encoded = json.dumps(sorted(str(value) for value in member_ids))
    return "CG-" + hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:6].upper()


# --------------------------------------------------------------------------- #
# Which observations the pass is asked about
# --------------------------------------------------------------------------- #
def candidate_observations(workspace: Workspace) -> list[dict]:
    """Every exception observation that could stand behind a finding.

    A ``covered_by`` duplicate is out: the roll-up has already decided it is
    its lead's measurement made twice. Everything else is in, drafted or not,
    grouped or not — this is the set the basis is taken over, and it must not
    move when a decision is made or a draft is written.
    """
    return [
        item
        for item in workspace.observations
        if item.get("outcome") == "exception" and not item.get("covered_by")
    ]


def drafted_observation_ids(workspace: Workspace) -> set[str]:
    """Observations a finding already stands on, as its source or a member."""
    drafted: set[str] = set()
    for item in workspace.findings:
        source = str(item.get("source_observation_id") or "")
        if source:
            drafted.add(source)
        drafted.update(
            str(value) for value in (item.get("consolidation") or {}).get("observations") or []
        )
    return drafted


def groupable_observations(workspace: Workspace) -> list[dict]:
    """The observations a grouping turn is shown: undecided and undrafted.

    An observation already in a decided group, already drafted, or resting on
    a test a finding could not be drafted from is not offered. A drafted one
    may still be merged by the auditor afterwards; asking the model about it
    would buy the saving this pass exists for only after it was spent.
    """
    from . import findings

    drafted = drafted_observation_ids(workspace)
    return [
        item
        for item in candidate_observations(workspace)
        if not (item.get("consolidation") or {}).get("role")
        and str(item.get("id") or "") not in drafted
        and not findings.observation_support_issues(workspace, item)
    ]


def _execution_identity(workspace: Workspace, observation: Mapping) -> list[str]:
    from . import findings

    value = str(observation.get("execution_ref") or "")
    kind, _separator, source_id = value.partition(":")
    resolved = findings.artifact(workspace, kind, source_id) if value else None
    return [value, str((resolved or {}).get("sha1") or "")]


def basis_sha1(workspace: Workspace) -> str:
    """Identity of the observation set a suggestion was made against.

    The sorted candidate observation ids and each one's execution-result hash:
    the same pattern ``planning.change_assessed`` keys on. A new exception, a
    rerun against a changed result, or an exception that clears moves the basis
    and re-asks the question. Deliberately *not* in the basis: the decisions,
    the marks they leave, and whether a finding has been drafted — so accepting
    a group, or drafting it, leaves the remaining suggestions current.
    """
    material = [
        {
            "id": str(item.get("id") or ""),
            "execution": _execution_identity(workspace, item),
        }
        for item in sorted(candidate_observations(workspace), key=lambda item: str(item.get("id") or ""))
    ]
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# The suggestion file
# --------------------------------------------------------------------------- #
def load(workspace: Workspace, basis: str) -> dict | None:
    """The suggestion set made against this basis, or ``None``."""
    target = path(workspace, str(basis or ""))
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("schema") or 0) != SCHEMA:
        return None
    return payload


def latest(workspace: Workspace) -> dict | None:
    """The newest suggestion set on file, whatever basis it was made against."""
    try:
        files = sorted(folder(workspace).glob("*.json"), key=lambda item: item.stat().st_mtime)
    except OSError:
        return None
    for target in reversed(files):
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _plain(value: object) -> object:
    """A frozen worker proposal back to plain JSON containers."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def normalize_group(group: Mapping, *, known: set[str] | None = None) -> dict:
    """One group as the file stores it, validated to the schema.

    ``known`` narrows the member ids to the observations on file; the worker's
    own validator has already held the proposal to the supplied observations,
    so this is the write-time guard against a hand-built or replayed group.
    A manual group — findings the auditor merged on the Findings page — names
    its findings as well, since a hand-written finding has no observation.
    """
    manual = bool(group.get("manual"))
    member_ids = [
        str(value) for value in group.get("observation_ids") or [] if str(value or "").strip()
    ]
    member_ids = list(dict.fromkeys(member_ids))
    finding_ids = list(
        dict.fromkeys(
            str(value) for value in group.get("finding_ids") or [] if str(value or "").strip()
        )
    )
    if len(finding_ids if manual else member_ids) < 2:
        raise WorkspaceError("A consolidation group needs at least two members.")
    if known is not None and not manual:
        unknown = [value for value in member_ids if value not in known]
        if unknown:
            raise WorkspaceError(f"Consolidation group names unknown observation '{unknown[0]}'.")
    lead = str(group.get("lead_observation_id") or (member_ids[0] if member_ids else ""))
    if member_ids and lead not in member_ids:
        raise WorkspaceError("A consolidation group's lead must be one of its members.")
    relation = str(group.get("relation") or "")
    if relation not in RELATIONS:
        raise WorkspaceError(f"Unknown consolidation relation '{relation}'.")
    basis = str(group.get("basis") or "entity")
    if basis not in BASES:
        raise WorkspaceError(f"Unknown consolidation basis '{basis}'.")
    decision = group.get("decision")
    if decision is not None and str(decision) not in DECISIONS:
        raise WorkspaceError(f"Unknown consolidation decision '{decision}'.")
    decided_by = str(group.get("decided_by") or "") or None
    if decided_by is not None and decided_by not in DECIDERS:
        raise WorkspaceError(f"Unknown consolidation decider '{decided_by}'.")
    shared = group.get("shared_entities") or {}
    if not isinstance(shared, Mapping):
        shared = {}
    return {
        "group_id": str(group.get("group_id") or group_id(member_ids or finding_ids)),
        "observation_ids": member_ids,
        "lead_observation_id": lead,
        **({"manual": True, "finding_ids": finding_ids} if manual else {}),
        "relation": relation,
        "basis": basis,
        "proposed_title": str(group.get("proposed_title") or ""),
        "root_cause_hypothesis": str(group.get("root_cause_hypothesis") or ""),
        "rationale": str(group.get("rationale") or ""),
        "shared_entities": {
            str(key): [str(item) for item in values]
            for key, values in _plain(shared).items()
            if isinstance(values, list)
        },
        "decision": str(decision) if decision else None,
        "decided_by": decided_by,
        "decided_at": str(group.get("decided_at") or "") or None,
    }


def save(workspace: Workspace, basis: str, suggestion: Mapping, *, run_id: str) -> dict:
    """Write one suggestion set keyed by its basis.

    A group arrives undecided unless the caller decided it — an auto run's
    same-condition group, or the auditor's answer to the run's approval card —
    and a decision is stamped here. Decided groups already on file for the same
    basis are kept: a forced re-review asks only about what is still
    groupable, and must not forget what was settled before it.
    """
    basis = str(basis or "").strip()
    if not basis:
        raise WorkspaceError("A consolidation suggestion needs the basis it was made against.")
    known = {str(item.get("id") or "") for item in candidate_observations(workspace)}
    now = _utcnow()
    groups = []
    for group in _plain(suggestion.get("groups") or []):
        if not isinstance(group, Mapping):
            continue
        entry = normalize_group(group, known=known)
        if entry["decision"]:
            entry["decided_by"] = entry["decided_by"] or "auditor"
            entry["decided_at"] = entry["decided_at"] or now
        else:
            entry["decided_by"] = entry["decided_at"] = None
        groups.append(entry)
    seen: set[str] = set()
    for group in groups:
        for member_id in group["observation_ids"]:
            if member_id in seen:
                raise WorkspaceError(f"Observation '{member_id}' appears in more than one group.")
            seen.add(member_id)
    previous = load(workspace, basis) or {}
    kept = [
        dict(group)
        for group in previous.get("groups") or []
        if group.get("decision")
        and group.get("group_id") not in {item["group_id"] for item in groups}
        and not set(group.get("observation_ids") or []) & seen
    ]
    singletons = [
        str(value) for value in _plain(suggestion.get("singletons") or [])
        if str(value) in known and str(value) not in seen
    ]
    payload = {
        "schema": SCHEMA,
        "basis_sha1": basis,
        "run_id": str(run_id or ""),
        "suggested_at": now,
        "groups": [*kept, *groups],
        "singletons": list(dict.fromkeys(singletons)),
    }
    write_json_atomic(path(workspace, basis), payload)
    return payload


def _write(workspace: Workspace, payload: dict) -> dict:
    write_json_atomic(path(workspace, str(payload["basis_sha1"])), payload)
    return payload


def decide(
    workspace: Workspace,
    basis: str,
    group_id_value: str,
    decision: str,
    *,
    decided_by: str = "auditor",
) -> dict:
    """Record the answer to one suggested group."""
    if decision not in DECISIONS:
        raise WorkspaceError(f"Unknown consolidation decision '{decision}'.")
    payload = load(workspace, basis)
    if payload is None:
        raise WorkspaceError("No consolidation suggestion exists for this basis.")
    group = next(
        (item for item in payload.get("groups") or [] if item.get("group_id") == group_id_value),
        None,
    )
    if group is None:
        raise WorkspaceError(f"Consolidation group '{group_id_value}' is not in this suggestion.")
    group["decision"] = decision
    group["decided_by"] = decided_by
    group["decided_at"] = _utcnow()
    return _write(workspace, payload)


def record_manual_group(workspace: Workspace, basis: str, group: Mapping) -> dict:
    """Append an auditor-made merge of findings, already accepted.

    A manual merge answers the same question the model was asked, so it is
    filed beside the suggestions rather than in a second place. Where no
    suggestion exists for the basis yet, one is created holding only this
    group, so the decision still outlives the run.
    """
    payload = load(workspace, basis) or {
        "schema": SCHEMA,
        "basis_sha1": str(basis),
        "run_id": "",
        "suggested_at": _utcnow(),
        "groups": [],
        "singletons": [],
    }
    entry = {
        **normalize_group({**group, "manual": True}),
        "decision": "accepted",
        "decided_by": "auditor",
        "decided_at": _utcnow(),
    }
    payload["groups"] = [
        item for item in payload.get("groups") or [] if item.get("group_id") != entry["group_id"]
    ] + [entry]
    members = set(entry["observation_ids"])
    payload["singletons"] = [
        value for value in payload.get("singletons") or [] if value not in members
    ]
    return _write(workspace, payload)


def undecided_groups(suggestion: Mapping | None) -> list[dict]:
    return [
        dict(group)
        for group in (suggestion or {}).get("groups") or []
        if not group.get("decision")
    ]


# --------------------------------------------------------------------------- #
# Acting on an accepted group
# --------------------------------------------------------------------------- #
def _own_finding(workspace: Workspace, observation_id: str) -> dict | None:
    return next(
        (
            item
            for item in workspace.findings
            if str(item.get("source_observation_id") or "") == observation_id
            and not is_absorbed(item)
        ),
        None,
    )


def apply_group(
    workspace: Workspace,
    group: Mapping,
    *,
    basis: str,
    lead_observation_id: str | None = None,
    title: str | None = None,
    decided_by: str = "auditor",
    include_confirmed: bool = False,
    persist: bool = True,
) -> dict | None:
    """Make an accepted group one finding, at whichever point it was decided.

    Before any member is drafted — the ordinary case — the decision is marked
    on the observations and nothing else happens: the lead's own draft unit
    writes the one finding from every member. Once some members have findings,
    those are merged into one lead by :func:`findings.consolidate` and the
    undrafted members join it by reference; the lead's narrative is then
    pending a redraft, as any merge leaves it. Returns the lead finding when a
    merge happened, else ``None``.
    """
    from . import findings

    member_ids = [str(value) for value in group.get("observation_ids") or []]
    if len(member_ids) < 2:
        raise WorkspaceError("A consolidation group needs at least two members.")
    by_id = {str(item.get("id") or ""): item for item in workspace.observations}
    missing = [value for value in member_ids if value not in by_id]
    if missing:
        raise WorkspaceError(f"Observation '{missing[0]}' no longer exists.")
    for value in member_ids:
        role = str((by_id[value].get("consolidation") or {}).get("role") or "")
        if role:
            raise WorkspaceError(f"Observation '{value}' is already in a consolidated group.")
    lead_id = str(lead_observation_id or group.get("lead_observation_id") or member_ids[0])
    if lead_id not in member_ids:
        raise WorkspaceError("The lead must be one of the group's members.")
    drafted = {value: item for value in member_ids if (item := _own_finding(workspace, value))}
    if drafted and lead_id not in drafted:
        # A merge needs a finding to lead it; the first drafted member takes
        # the lead the auditor's pick could not.
        lead_id = next(value for value in member_ids if value in drafted)
    group_value = str(group.get("group_id") or group_id(member_ids))
    relation = str(group.get("relation") or "")
    if relation not in RELATIONS:
        raise WorkspaceError(f"Unknown consolidation relation '{relation}'.")
    chosen_title = str(title or group.get("proposed_title") or "").strip()
    hypothesis = str(group.get("root_cause_hypothesis") or "").strip()
    now = _utcnow()
    decision = {
        "group_id": group_value,
        "relation": relation,
        "basis_sha1": str(basis),
        "decided_by": decided_by,
        "decided_at": now,
    }
    lead_finding = None
    if drafted:
        lead_finding = findings.consolidate(
            workspace,
            finding_ids=[drafted[lead_id]["id"], *(
                drafted[value]["id"] for value in member_ids if value in drafted and value != lead_id
            )],
            lead_id=str(drafted[lead_id]["id"]),
            relation=relation,
            group_id=group_value,
            basis=str(basis),
            title=chosen_title or None,
            root_cause_hypothesis=hypothesis or None,
            decided_by=decided_by,
            include_confirmed=include_confirmed,
            observation_ids=[value for value in member_ids if value not in drafted],
        )
        # ``consolidate`` saved; the marks below are a second write.
    by_id = {str(item.get("id") or ""): item for item in workspace.observations}
    others = [value for value in member_ids if value != lead_id]
    if lead_finding is None:
        by_id[lead_id]["consolidation"] = {
            **decision,
            "role": "lead",
            "members": others,
            "basis": str(group.get("basis") or "entity"),
            "proposed_title": chosen_title,
            "root_cause_hypothesis": hypothesis,
        }
        by_id[lead_id]["updated"] = workspace._updated_now()
    for value in others:
        if value in drafted:
            continue
        by_id[value]["consolidation"] = {**decision, "role": "member", "into": lead_id}
        by_id[value]["updated"] = workspace._updated_now()
    if persist:
        workspace.save()
    return lead_finding


def ungroup_observation(workspace: Workspace, observation_id: str) -> dict:
    """Take one member out of its group so it is drafted on its own.

    The lead keeps the references it took — they are still true of the lead's
    condition, as :func:`findings.unconsolidate` keeps them — and loses only
    the member. A lead left with no members stops being one.
    """
    by_id = {str(item.get("id") or ""): item for item in workspace.observations}
    observation = by_id.get(str(observation_id))
    if observation is None:
        raise WorkspaceError(f"Observation '{observation_id}' not found.")
    if not is_member(observation):
        raise WorkspaceError(f"Observation '{observation_id}' is not grouped into another.")
    lead_id = str((observation.get("consolidation") or {}).get("into") or "")
    lead = by_id.get(lead_id)
    now = workspace._updated_now()
    if lead is not None and str((lead.get("consolidation") or {}).get("role") or "") == "lead":
        remaining = [
            value for value in (lead["consolidation"].get("members") or []) if value != observation_id
        ]
        if remaining:
            lead["consolidation"] = {**lead["consolidation"], "members": remaining}
        else:
            lead.pop("consolidation", None)
        lead["updated"] = now
    for item in workspace.findings:
        consolidation = item.get("consolidation") or {}
        if observation_id not in (consolidation.get("observations") or []):
            continue
        observations = [value for value in consolidation["observations"] if value != observation_id]
        if observations or consolidation.get("members"):
            item["consolidation"] = {**consolidation, "observations": observations}
        else:
            item.pop("consolidation", None)
        item["auditor_confirmed"] = False
        item["updated"] = now
    observation.pop("consolidation", None)
    observation["updated"] = now
    workspace.save()
    return observation


# --------------------------------------------------------------------------- #
# The page's view
# --------------------------------------------------------------------------- #
def summary(workspace: Workspace) -> dict:
    """The current basis, its suggestion set, and each group's members in the
    shape the panel renders."""
    from . import doc_tests

    candidates = candidate_observations(workspace)
    groupable = groupable_observations(workspace)
    basis = basis_sha1(workspace)
    suggestion = load(workspace, basis)
    observations = {str(item.get("id") or ""): item for item in workspace.observations}
    rows = {str(row.get("id")): row for row in workspace.rcm}
    test_titles = {str(item.get("id")): str(item.get("title") or "") for item in workspace.data_tests}
    for item in doc_tests.list_tests(workspace):
        test_titles[str(item.get("id"))] = str(item.get("title") or "")
    standing = {}
    for item in workspace.findings:
        if is_absorbed(item):
            continue
        for value in [
            str(item.get("source_observation_id") or ""),
            *((item.get("consolidation") or {}).get("observations") or []),
        ]:
            if value:
                standing.setdefault(str(value), item)

    def member(observation_id: str) -> dict:
        item = observations.get(observation_id)
        if item is None:
            return {"id": observation_id, "title": "", "process": "", "test_refs": [], "missing": True}
        test_id = str(item.get("test_id") or "")
        finding = standing.get(observation_id)
        return {
            "id": observation_id,
            "title": str(item.get("summary") or test_titles.get(test_id) or test_id),
            "test_title": test_titles.get(test_id, ""),
            "process": str((rows.get(str(item.get("rcm_id") or "")) or {}).get("process") or ""),
            "rcm_id": str(item.get("rcm_id") or ""),
            "test_refs": [test_id] if test_id else [],
            "exception_count": int(item.get("exception_count") or 0),
            "finding_id": str(finding["id"]) if finding else None,
            "severity": str(finding.get("severity") or "") if finding else None,
            "auditor_confirmed": bool(finding.get("auditor_confirmed")) if finding else False,
            "grouped": str((item.get("consolidation") or {}).get("role") or "") or None,
        }

    view = None
    if suggestion is not None:
        view = {
            **suggestion,
            "groups": [
                {
                    **group,
                    "members": [member(value) for value in group.get("observation_ids") or []],
                }
                for group in suggestion.get("groups") or []
            ],
        }
    stale = None
    if suggestion is None:
        newest = latest(workspace)
        if newest is not None:
            stale = {
                "basis_sha1": newest.get("basis_sha1"),
                "groups": len(newest.get("groups") or []),
                "suggested_at": newest.get("suggested_at"),
            }
    return {
        "basis_sha1": basis,
        "current": suggestion is not None,
        "candidates": len(candidates),
        "groupable": len(groupable),
        "drafts": sum(not is_absorbed(item) for item in workspace.findings),
        "suggestion": view,
        "stale_suggestion": stale,
        "undecided": len(undecided_groups(suggestion)),
    }


__all__ = [
    "BASES",
    "DECIDERS",
    "DECISIONS",
    "FOLDER",
    "RELATIONS",
    "apply_group",
    "basis_sha1",
    "candidate_observations",
    "decide",
    "drafted_observation_ids",
    "group_id",
    "groupable_observations",
    "is_absorbed",
    "is_lead",
    "is_member",
    "latest",
    "load",
    "normalize_group",
    "path",
    "record_manual_group",
    "save",
    "summary",
    "undecided_groups",
    "ungroup_observation",
]
