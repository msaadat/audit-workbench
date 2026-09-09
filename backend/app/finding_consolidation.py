"""Suggested consolidations of related findings, and the decisions made on them.

Eighteen drafted findings covering eleven issues is what one-observation-at-a-
time drafting produces: the finding worker sees one observation, its test and
its exception rows, and cannot see that the draft beside it names the same
invoices. Whether two drafts are one finding is sometimes a fact — three tests
flagged the same three invoices — and sometimes a root-cause judgment — three
controls at successive stages failed on one inactive vendor. The first is
collapsed before drafting (``rcm_execution`` and the redundancy marks). The
second is *proposed* by one model turn that sees every draft, and decided by
the auditor.

This module is the durable side of that proposal. A suggestion set is keyed by
its *basis*: the sorted draft finding ids and the hash of each finding's
execution results. Ask the same question of the same findings and the answer
is on disk; change the set — accept a group, redraft a member, confirm and
absorb — and the old answer applies to nothing. That is what lets
``findings.consolidated`` be a capability with honest readiness rather than a
button that re-asks a model every time it is looked at.

Nothing here changes a finding. Merging is :func:`findings.consolidate`; this
records what was suggested and what the auditor said about it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .workspaces import Workspace, WorkspaceError, write_json_atomic

FOLDER = ".consolidation"
SCHEMA = 1
#: What a group claims about its members. ``same_condition``: the same
#: exception observed more than once. ``shared_cause``: different control
#: failures with one root cause.
RELATIONS = ("same_condition", "shared_cause")
#: What a group's claim rests on. ``entity``: the members flag the same
#: records. ``process``: the members sit in one process and share no record.
BASES = ("entity", "process")
DECISIONS = ("accepted", "dismissed")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def folder(workspace: Workspace) -> Path:
    return workspace.root / "Findings" / FOLDER


def path(workspace: Workspace, basis: str) -> Path:
    return folder(workspace) / f"{basis}.json"


def is_absorbed(item: Mapping) -> bool:
    return str((item.get("consolidation") or {}).get("role") or "") == "absorbed"


def is_lead(item: Mapping) -> bool:
    return str((item.get("consolidation") or {}).get("role") or "") == "lead"


def draft_findings(workspace: Workspace) -> list[dict]:
    """The findings a consolidation pass may group.

    Everything that still stands on its own: absorbed findings have already
    been merged into a lead and are not candidates twice. Confirmed findings
    *are* candidates — the pass may still propose them — and the merge asks
    before touching one.
    """
    return [item for item in workspace.findings if not is_absorbed(item)]


def group_id(finding_ids: list[str] | tuple[str, ...]) -> str:
    """A group's id is a function of its members, so the same proposal made
    twice names the same group."""
    encoded = json.dumps(sorted(str(value) for value in finding_ids))
    return "CG-" + hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:6].upper()


def _execution_identity(workspace: Workspace, item: Mapping) -> list[list[str]]:
    """The result hashes behind one finding's own evidence.

    A finding drafted from an observation is identified by that observation's
    result: a lead's unioned ``execution_refs`` include its members', and those
    members are already in the basis under their own ids, so counting them
    here too would move the basis on every accept. A hand-written finding has
    no observation and is identified by the results it cites.
    """
    from . import findings

    refs = list(item.get("execution_refs") or [])
    source_id = str(item.get("source_observation_id") or "")
    if source_id:
        observation = next(
            (value for value in workspace.observations if str(value.get("id")) == source_id),
            None,
        )
        if observation is not None and observation.get("execution_ref"):
            refs = [str(observation["execution_ref"])]
    identity: list[list[str]] = []
    for value in refs:
        kind, _separator, source_id = str(value).partition(":")
        resolved = findings.artifact(workspace, kind, source_id)
        identity.append([str(value), str((resolved or {}).get("sha1") or "")])
    return identity


def basis_sha1(workspace: Workspace) -> str:
    """Identity of the finding set a suggestion was made against.

    The sorted finding ids and each one's execution-result hashes: the same
    pattern ``planning.change_assessed`` keys on. A finding added, removed, or
    re-run against a changed result moves the basis and re-asks the question.
    Deliberately *not* in the basis: confirmation, the narrative, and the
    consolidation decisions themselves. Accepting one group absorbs its
    members but does not unsay the other groups the same turn proposed, so
    absorbed findings stay in the identity and the remaining suggestions stay
    current until the evidence under them moves.
    """
    material = [
        {
            "id": str(item.get("id") or ""),
            "executions": _execution_identity(workspace, item),
        }
        for item in sorted(workspace.findings, key=lambda item: str(item.get("id") or ""))
    ]
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def load(workspace: Workspace, basis: str) -> dict | None:
    """The suggestion set made against this basis, or ``None``."""
    target = path(workspace, str(basis or ""))
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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

    ``known`` narrows the member ids to the drafts on file; the worker's own
    validator has already held the proposal to the supplied drafts, so this is
    the write-time guard against a hand-built or replayed group.
    """
    finding_ids = [str(value) for value in group.get("finding_ids") or [] if str(value or "").strip()]
    finding_ids = list(dict.fromkeys(finding_ids))
    if len(finding_ids) < 2:
        raise WorkspaceError("A consolidation group needs at least two findings.")
    if known is not None:
        unknown = [value for value in finding_ids if value not in known]
        if unknown:
            raise WorkspaceError(f"Consolidation group names unknown finding '{unknown[0]}'.")
    lead = str(group.get("lead_finding_id") or finding_ids[0])
    if lead not in finding_ids:
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
    shared = group.get("shared_entities") or {}
    if not isinstance(shared, Mapping):
        shared = {}
    return {
        "group_id": str(group.get("group_id") or group_id(finding_ids)),
        "finding_ids": finding_ids,
        "lead_finding_id": lead,
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
        "decided_by": str(group.get("decided_by") or "") or None,
        "decided_at": str(group.get("decided_at") or "") or None,
    }


def save(workspace: Workspace, basis: str, suggestion: Mapping, *, run_id: str) -> dict:
    """Write one suggestion set, every group undecided, keyed by its basis."""
    basis = str(basis or "").strip()
    if not basis:
        raise WorkspaceError("A consolidation suggestion needs the basis it was made against.")
    known = {str(item.get("id") or "") for item in draft_findings(workspace)}
    groups = [
        {**normalize_group(group, known=known), "decision": None, "decided_by": None, "decided_at": None}
        for group in _plain(suggestion.get("groups") or [])
        if isinstance(group, Mapping)
    ]
    seen: set[str] = set()
    for group in groups:
        for finding_id in group["finding_ids"]:
            if finding_id in seen:
                raise WorkspaceError(f"Finding '{finding_id}' appears in more than one group.")
            seen.add(finding_id)
    grouped = {finding_id for group in groups for finding_id in group["finding_ids"]}
    singletons = [
        str(value) for value in _plain(suggestion.get("singletons") or [])
        if str(value) in known and str(value) not in grouped
    ]
    singletons.extend(sorted(known - grouped - set(singletons)))
    payload = {
        "schema": SCHEMA,
        "basis_sha1": basis,
        "run_id": str(run_id or ""),
        "suggested_at": _utcnow(),
        "groups": groups,
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
    """Record the auditor's answer to one suggested group."""
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
    """Append an auditor-made group, already accepted, to the basis's set.

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
        **normalize_group(group),
        "decision": "accepted",
        "decided_by": "auditor",
        "decided_at": _utcnow(),
    }
    payload["groups"] = [
        item for item in payload.get("groups") or [] if item.get("group_id") != entry["group_id"]
    ] + [entry]
    members = set(entry["finding_ids"])
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


def summary(workspace: Workspace) -> dict:
    """The page's view: the current basis, its suggestion set, and each
    group's members in the shape the panel renders."""
    drafts = draft_findings(workspace)
    basis = basis_sha1(workspace)
    suggestion = load(workspace, basis)
    by_id = {str(item.get("id")): item for item in workspace.findings}
    rows = {str(row.get("id")): row for row in workspace.rcm}
    semantic = {str(row.get("semantic_id") or ""): row for row in workspace.rcm}

    def process_of(item: Mapping) -> str:
        names: list[str] = []
        for ref in item.get("rcm_refs") or []:
            row = rows.get(str(ref))
            if row and str(row.get("process") or ""):
                names.append(str(row.get("process")))
        for ref in item.get("rcm_semantic_refs") or []:
            row = semantic.get(str(ref))
            if row and str(row.get("process") or ""):
                names.append(str(row.get("process")))
        return "; ".join(dict.fromkeys(names))

    def member(finding_id: str) -> dict:
        item = by_id.get(finding_id)
        if item is None:
            return {"id": finding_id, "title": "", "severity": "medium", "process": "", "test_refs": [], "missing": True}
        return {
            "id": finding_id,
            "title": str(item.get("title") or ""),
            "severity": str(item.get("severity") or "medium"),
            "process": process_of(item),
            "test_refs": [str(value) for value in item.get("test_refs") or []],
            "auditor_confirmed": bool(item.get("auditor_confirmed")),
            "absorbed": is_absorbed(item),
        }

    view = None
    if suggestion is not None:
        view = {
            **suggestion,
            "groups": [
                {**group, "members": [member(value) for value in group.get("finding_ids") or []]}
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
        "drafts": len(drafts),
        "suggestion": view,
        "stale_suggestion": stale,
        "undecided": len(undecided_groups(suggestion)),
    }


__all__ = [
    "BASES",
    "DECISIONS",
    "FOLDER",
    "RELATIONS",
    "basis_sha1",
    "decide",
    "draft_findings",
    "group_id",
    "is_absorbed",
    "is_lead",
    "latest",
    "load",
    "normalize_group",
    "path",
    "record_manual_group",
    "save",
    "summary",
    "undecided_groups",
]
