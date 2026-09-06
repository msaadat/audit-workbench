"""Assessments of what new evidence changes for the planning artifacts.

"I uploaded document XX, revise the APM and RCM as appropriate" is the request
the framework was built to refuse: currency is `not_assessed` by design, nothing
watches for staleness, and a capability that quietly decided an artifact had
gone out of date would be the framework assessing currency on its own. So the
judgment is made the only way it may be — as a requested outcome, by a worker,
against declared context — and its answer is written here.

An assessment is keyed by its *basis*: the analyses of the documents it read,
the memorandum it read, and the matrix rows it read. Ask the same question of
the same material and the answer is already on disk; change any of the three and
the old answer no longer applies to anything. That is what makes
``planning.change_assessed`` a capability with honest readiness rather than a
button that re-asks a model every time it is looked at.

The file is small, derived, and reproducible. It is not an audit artifact: it is
the reason one was, or was not, revised.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from .workspaces import Workspace, WorkspaceError, planning_apm_sha1, write_json_atomic

FOLDER = ".delta"
SCHEMA = 1
#: What an assessment may conclude, and what the auditor is offered next.
IMPACTS = ("none", "apm", "rcm", "both")


def folder(workspace: Workspace) -> Path:
    return workspace.root / "Planning" / FOLDER


def path(workspace: Workspace, basis: str) -> Path:
    return folder(workspace) / f"{basis}.json"


def basis_sha1(workspace: Workspace, document_ids: list[str] | tuple[str, ...]) -> str:
    """Identity of the material an assessment was made against.

    Three parts, because an assessment answers a question about all three: what
    the new documents say, what the memorandum currently says, and what the
    matrix currently says. A change to any one of them makes the stored answer
    an answer to a different question.

    Document *analyses* rather than document text: the assessment reads the
    analysis, so re-reading a document that has since been re-analysed is a new
    question, and re-uploading identical text is not.
    """

    wanted = {str(value) for value in document_ids}
    analyses = {
        str(item.get("id")): _analysis_identity(workspace, str(item.get("id")))
        for item in workspace.documents
        if str(item.get("id")) in wanted
    }
    material = {
        "documents": dict(sorted(analyses.items())),
        "apm_sha1": planning_apm_sha1(workspace),
        "rcm": [_row_identity(row) for row in workspace.rcm],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _analysis_identity(workspace: Workspace, document_id: str) -> str:
    """What this document currently *says*, as the assessment would read it.

    The analysis the planning turn is shown, not the file's bytes: a document
    re-analysed under a corrected type is new material to assess, and one
    re-uploaded unchanged is not.
    """

    from . import document_context

    try:
        context = document_context.apm_document_context(
            workspace, document_id, include_audit_notes=False
        )
    except Exception:
        context = {}
    analysis_id = str(context.get("analysis_id") or "")
    if analysis_id:
        return f"{analysis_id}:{context.get('analysis_validity_state') or ''}"
    # No analysis yet: the document's own bytes are all there is to key on, and
    # an assessment made from raw text is still an assessment of *that* text.
    document = next(
        (item for item in workspace.documents if str(item.get("id")) == document_id), {}
    )
    return str(document.get("sha1") or "")


def _row_identity(row: dict) -> list[str]:
    from .agent.capabilities._shared import rcm_row_sha1

    return [str(row.get("id") or ""), rcm_row_sha1(row)]


def load(workspace: Workspace, basis: str) -> dict | None:
    """The assessment made against this basis, or ``None``."""

    target = path(workspace, str(basis or ""))
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def latest(workspace: Workspace) -> dict | None:
    """The newest assessment on file, whatever basis it was made against."""

    try:
        files = sorted(
            folder(workspace).glob("*.json"), key=lambda item: item.stat().st_mtime
        )
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


def save(
    workspace: Workspace,
    basis: str,
    assessment: dict,
    *,
    document_ids: list[str],
    run_id: str,
) -> dict:
    """Write one assessment, keyed by the basis it was made against."""

    basis = str(basis or "").strip()
    if not basis:
        raise WorkspaceError("A change assessment needs the basis it was made against.")
    impact = str(assessment.get("impact") or "")
    if impact not in IMPACTS:
        raise WorkspaceError(f"Unknown change-assessment impact '{impact}'.")
    payload = {
        "schema": SCHEMA,
        "basis_sha1": basis,
        "document_ids": [str(value) for value in document_ids],
        "run_id": str(run_id),
        "assessed_at": _utcnow(),
        "impact": impact,
        "summary": str(assessment.get("summary") or ""),
        "apm_changes": _plain(assessment.get("apm_changes") or []),
        "rcm_changes": _plain(assessment.get("rcm_changes") or []),
    }
    write_json_atomic(path(workspace, basis), payload)
    return payload


def _plain(value: object) -> object:
    """Project a frozen worker proposal back to plain JSON containers.

    A proposal arrives recursively frozen — ``MappingProxyType`` and tuples —
    because nothing downstream may edit what the model actually said. That is
    the right guarantee and the wrong type to hand a JSON writer, so the
    conversion happens here, at the one boundary where the assessment becomes a
    file.
    """

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["IMPACTS", "basis_sha1", "latest", "load", "path", "save"]
