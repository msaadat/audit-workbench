"""Revisions an auditor asked for, recorded by the question they answered.

A revision is a request — "trim the matrix to the core risks", "rewrite the
fraud section around the new policy" — applied by a registered worker that sees
the whole artifact in scope and decides something about every part of it. It is
a requested outcome, off every template, like ``planning.change_assessed``, and
for the same reason it needs a record: the scheduler asks a capability whether
its outcome already holds, and "has this revision been made" is answerable only
by remembering it was.

The record is keyed by the *request* — what was asked, of which artifact, over
which rows — and stamped with the hash of the scope as the commit left it. Ask
the same thing again and the matrix still reads as the revision wrote it, and
the answer is already on disk; touch the matrix since, and the question is new.

The file is small, derived, and reproducible. It is not an audit artifact: it is
the account of why one changed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from .workspaces import Workspace, WorkspaceError, write_json_atomic

FOLDER = ".revisions"
SCHEMA = 1
#: What a revision may be asked of.
KINDS = ("rcm", "apm")
#: What a revision may decide about one existing RCM row.
RCM_DECISIONS = ("keep", "revise", "merge", "retire")
#: The row fields a revision may rewrite. Ownership, links and provenance are
#: the workbench's; a revision decides what a row *says*.
RCM_REVISABLE_FIELDS = (
    "process",
    "risk",
    "risk_rating",
    "control",
    "control_type",
    "control_owner",
    "criteria",
)
RISK_RATINGS = ("low", "medium", "high", "critical")


def folder(workspace: Workspace) -> Path:
    return workspace.root / "Planning" / FOLDER


def path(workspace: Workspace, kind: str, key: str) -> Path:
    if kind not in KINDS:
        raise WorkspaceError(f"Unknown revision kind '{kind}'.")
    return folder(workspace) / f"{kind}-{key}.json"


def request_key(
    kind: str,
    *,
    instruction: str = "",
    assessment: str = "",
    target_refs: Iterable[str] = (),
) -> str:
    """Identity of one revision request: what was asked, of what."""

    material = {
        "kind": str(kind),
        "instruction": " ".join(str(instruction or "").split()),
        "assessment": str(assessment or ""),
        "targets": sorted(
            str(ref) for ref in target_refs if str(ref) != "workspace:current"
        ),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def rcm_scope_sha1(rows: Iterable[Mapping]) -> str:
    """The rows a revision covers, as their material identity.

    Ids and material hashes, in matrix order. A row retired, merged, added or
    reworded moves it; a sign-off does not, for the reason the row hash itself
    excludes ``review_status``.
    """

    from .agent.capabilities._shared import rcm_row_sha1

    material = [[str(row.get("id") or ""), rcm_row_sha1(dict(row))] for row in rows]
    encoded = json.dumps(material, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def apm_scope_sha1(workspace: Workspace) -> str:
    """The memorandum's text, and nothing about who wrote it or when."""

    text = str(workspace.planning.get("apm_markdown") or "")
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load(workspace: Workspace, kind: str, key: str) -> dict | None:
    """The revision made for this request, or ``None``."""

    target = path(workspace, kind, str(key or ""))
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save(
    workspace: Workspace,
    kind: str,
    key: str,
    *,
    run_id: str,
    unit_id: str,
    applied_sha1: str,
    summary: str,
    outcomes: list[dict],
    instruction: str = "",
    assessment: str = "",
) -> dict:
    """Write one revision record, stamped with what the commit left behind."""

    key = str(key or "").strip()
    if not key:
        raise WorkspaceError("A revision record needs the request it answered.")
    payload = {
        "schema": SCHEMA,
        "kind": kind,
        "request_key": key,
        "run_id": str(run_id),
        "unit_id": str(unit_id),
        "revised_at": _utcnow(),
        "instruction": str(instruction or ""),
        "assessment": str(assessment or ""),
        "applied_sha1": str(applied_sha1),
        "summary": str(summary or ""),
        "outcomes": _plain(outcomes),
    }
    write_json_atomic(path(workspace, kind, key), payload)
    return payload


def assessment_instruction(workspace: Workspace, basis: str, kind: str) -> str:
    """A change assessment's findings for one artifact, as a revision brief.

    The bridge from ``planning.change_assessed``: the assessment names what the
    new evidence changes and where, and the revision is the run that makes those
    changes. Rendered as text because it reaches the worker the way any other
    instruction does — declared, hashed and budgeted.
    """

    from . import planning_delta

    assessment = planning_delta.load(workspace, str(basis or ""))
    if assessment is None:
        raise WorkspaceError(
            "That change assessment is not on file; assess the documents again."
        )
    changes = assessment.get("rcm_changes" if kind == "rcm" else "apm_changes") or []
    if not changes:
        return ""
    lines = [
        "Apply this change assessment. It was made against the documents "
        f"{', '.join(assessment.get('document_ids') or [])}.",
        f"Summary: {assessment.get('summary') or ''}",
    ]
    for entry in changes:
        if kind == "rcm":
            target = entry.get("rcm_id") or "a new row"
            lines.append(
                f"- {entry.get('change')} {target}: {entry.get('summary') or ''} "
                f"(reason: {entry.get('reason') or ''})"
            )
        else:
            lines.append(
                f"- Section '{entry.get('section')}': {entry.get('change') or ''} "
                f"(reason: {entry.get('reason') or ''}; source: {entry.get('citation') or ''})"
            )
    return "\n".join(lines)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
