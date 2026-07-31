"""First-class audit findings with provenance and durable evidence links."""

from __future__ import annotations

import hashlib
import json
import uuid

from . import doc_tests
from .agent import store as agent_store
from .evidence import normalize_anchor, normalize_many
from .workspaces import Workspace, WorkspaceError, slugify

SEVERITIES = ("critical", "high", "medium", "low", "info")
SOURCES = ("agent", "manual", "promoted")


def _now(workspace: Workspace) -> str:
    return workspace._updated_now()


def _record(workspace: Workspace, finding_id: str) -> dict:
    item = next((row for row in workspace.findings if row.get("id") == finding_id), None)
    if item is None:
        raise WorkspaceError(f"Finding '{finding_id}' not found.")
    return item


def _canonical_sha1(value: object) -> str:
    return hashlib.sha1(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def artifact(workspace: Workspace, source_kind: str, source_id: str) -> dict | None:
    """Resolve an evidence source to its current record and immutable hash."""
    if source_kind == "document":
        item = next((row for row in workspace.documents if row.get("id") == source_id), None)
        return {"item": item, "sha1": item.get("sha1")} if item else None
    if source_kind == "analysis":
        item = next((row for row in workspace.analyses if row.get("id") == source_id), None)
    elif source_kind == "ruleset":
        item = next((row for row in workspace.rulesets if row.get("id") == source_id), None)
    elif source_kind == "procedure":
        item = next((row for row in workspace.work_program if row.get("id") == source_id), None)
    elif source_kind == "rcm":
        item = next((row for row in workspace.rcm if row.get("id") == source_id), None)
    elif source_kind == "datatest":
        from . import data_tests

        return data_tests.result_artifact(workspace, source_id)
    elif source_kind == "doctest":
        if not doc_tests.exists(workspace, source_id):
            return None
        item = doc_tests.load_test(workspace, source_id)
        return {"item": item, "sha1": item.get("sha1") or _canonical_sha1(item)}
    elif source_kind == "table":
        item = next((row for row in workspace.tables if row.get("name") == source_id), None)
        if item is None:
            item = next((row for row in workspace.joins if row.get("name") == source_id), None)
        if item is None:
            return None
        return {"item": item, "sha1": _canonical_sha1(workspace._table_signature(source_id))}
    else:
        return None
    return {"item": item, "sha1": _canonical_sha1(item)} if item else None


def validate_evidence(
    workspace: Workspace,
    values: object,
    *,
    require_hash: bool = True,
    allow_changed: bool = False,
) -> list[dict]:
    anchors = normalize_many(values, require_hash=require_hash)
    for anchor in anchors:
        resolved = artifact(workspace, anchor["source_kind"], anchor["source_id"])
        if resolved is None:
            raise WorkspaceError(
                f"Evidence source '{anchor['source_kind']}:{anchor['source_id']}' does not exist."
            )
        if (
            not allow_changed
            and anchor.get("source_sha1")
            and resolved["sha1"] != anchor["source_sha1"]
        ):
            raise WorkspaceError(
                f"Evidence source '{anchor['source_kind']}:{anchor['source_id']}' has changed."
            )
    return anchors


def evidence_warnings(workspace: Workspace, item: dict) -> list[str]:
    """Return stale-evidence warnings without changing a finding's provenance."""
    warnings = []
    for anchor in normalize_many(item.get("evidence_refs"), require_hash=False):
        resolved = artifact(workspace, anchor["source_kind"], anchor["source_id"])
        if resolved is None:
            warnings.append(
                f"Evidence source '{anchor['source_kind']}:{anchor['source_id']}' no longer exists."
            )
        elif anchor.get("source_sha1") and resolved["sha1"] != anchor["source_sha1"]:
            warnings.append(
                f"Evidence source '{anchor['source_kind']}:{anchor['source_id']}' has changed since this finding was drafted."
            )
    return list(dict.fromkeys(warnings))


def _known_test_ids(workspace: Workspace) -> set[str]:
    return {str(item.get("id")) for item in workspace.data_tests} | {
        str(summary.get("id")) for summary in doc_tests.list_tests(workspace)
    }


def _test_rcm_id(workspace: Workspace, test_id: str) -> str | None:
    """The RCM row one test is linked to, or None when it is unlinked."""
    record = next(
        (item for item in workspace.data_tests if str(item.get("id")) == str(test_id)),
        None,
    )
    if record is None and doc_tests.exists(workspace, str(test_id)):
        record = doc_tests.load_test(workspace, str(test_id))
    return str(record.get("rcm_id") or "") or None if record else None


def _validate_links(
    workspace: Workspace,
    rcm_refs: object,
    procedure_refs: object,
    test_refs: object = None,
) -> tuple[list[str], list[str], list[str]]:
    rcm = [str(value) for value in (rcm_refs or [])]
    procedures = [str(value) for value in (procedure_refs or [])]
    tests = [str(value) for value in (test_refs or [])]
    known_rcm = {row.get("id") for row in workspace.rcm}
    known_procedures = {row.get("id") for row in workspace.work_program}
    known_tests = _known_test_ids(workspace)
    missing_rcm = next((value for value in rcm if value not in known_rcm), None)
    missing_procedure = next((value for value in procedures if value not in known_procedures), None)
    missing_test = next((value for value in tests if value not in known_tests), None)
    if missing_rcm:
        raise WorkspaceError(f"RCM reference '{missing_rcm}' does not exist.")
    if missing_procedure:
        raise WorkspaceError(f"Procedure reference '{missing_procedure}' does not exist.")
    if missing_test:
        raise WorkspaceError(f"Test reference '{missing_test}' does not exist.")
    return (
        list(dict.fromkeys(rcm)),
        list(dict.fromkeys(procedures)),
        list(dict.fromkeys(tests)),
    )


def _severity(value: object) -> str:
    result = str(value or "medium").lower()
    if result not in SEVERITIES:
        raise WorkspaceError(f"Finding severity must be one of: {', '.join(SEVERITIES)}.")
    return result


def support_issues(workspace: Workspace, item: dict) -> list[str]:
    """Return deterministic blockers to treating a draft as a formal finding."""
    issues = []
    required = ("condition", "criteria", "effect", "recommendation", "severity_rationale")
    missing = [field for field in required if not str(item.get(field) or "").strip()]
    if not str(item.get("cause") or "").strip() and not item.get("cause_pending"):
        missing.append("cause or cause_pending")
    if missing:
        issues.append("missing " + ", ".join(missing))
    rcm_refs = {str(value) for value in item.get("rcm_refs") or []}
    test_refs = {str(value) for value in item.get("test_refs") or []}
    if not rcm_refs:
        issues.append("no RCM reference")
    if not test_refs:
        issues.append("no test reference")
    if not item.get("execution_refs"):
        issues.append("no execution-result reference")
    if not item.get("evidence_refs"):
        issues.append("no immutable evidence anchor")
    for test_id in test_refs:
        if test_id not in _known_test_ids(workspace):
            issues.append(f"missing test {test_id}")
            continue
        linked_row = _test_rcm_id(workspace, test_id)
        if linked_row not in rcm_refs:
            issues.append(f"test {test_id} is not covered by the finding's RCM links")
    for value in item.get("execution_refs") or []:
        kind, separator, source_id = str(value).partition(":")
        if not separator or kind not in {"datatest", "doctest"}:
            issues.append(f"invalid execution reference {value}")
            continue
        if kind == "datatest":
            from . import data_tests

            data_test_id, run_sep, run_id = source_id.partition(":")
            execution = next(
                (test for test in workspace.data_tests if test.get("id") == data_test_id), None
            )
            if execution is None or str(execution.get("id")) not in test_refs:
                issues.append(f"unlinked Data Test result {value}")
                continue
            if run_sep:
                try:
                    data_tests.load_result(workspace, data_test_id, run_id)
                except WorkspaceError:
                    issues.append(f"missing immutable Data Test result {value}")
            elif not execution.get("last_run"):
                issues.append(f"Data Test {data_test_id} has no durable result")
        else:
            if not doc_tests.exists(workspace, source_id):
                issues.append(f"missing Document Test {source_id}")
                continue
            execution = doc_tests.load_test(workspace, source_id)
            if str(execution.get("id")) not in test_refs:
                issues.append(f"unlinked Document Test {source_id}")
            if not str(execution.get("status") or "").startswith("completed"):
                issues.append(f"Document Test {source_id} is not complete")
    return list(dict.fromkeys(issues))


def add(workspace: Workspace, payload: dict, *, source: str = "manual") -> dict:
    if "status" in payload:
        raise WorkspaceError("Unknown finding field: status.")
    title = str(payload.get("title") or "").strip()
    if not title:
        raise WorkspaceError("Finding title is required.")
    if source not in SOURCES:
        raise WorkspaceError("Finding source is not supported.")
    rcm_refs, procedure_refs, test_refs = _validate_links(
        workspace,
        payload.get("rcm_refs"),
        payload.get("procedure_refs"),
        payload.get("test_refs"),
    )
    execution_refs = [
        str(value) for value in (payload.get("execution_refs") or []) if str(value).strip()
    ]
    evidence_values = list(payload.get("evidence_refs") or [])
    if not evidence_values and execution_refs:
        evidence_values = [
            anchor
            for value in execution_refs
            if (
                anchor := anchor_from_ref(
                    workspace, value, run_id=str(payload.get("agent_run_id") or "") or None
                )
            ) is not None
        ]
    evidence_refs = validate_evidence(workspace, evidence_values)
    created = _now(workspace)
    item = {
        "id": str(payload.get("id") or f"F-{uuid.uuid4().hex[:6].upper()}"),
        "semantic_id": str(payload.get("semantic_id") or f"finding:{slugify(title)}"),
        "created_by": "agent" if source == "agent" else "user",
        "agent_run_id": str(payload.get("agent_run_id") or "") or None,
        "source_observation_id": str(payload.get("source_observation_id") or "") or None,
        "title": title,
        "severity": _severity(payload.get("severity")),
        "condition": str(payload.get("condition") or ""),
        "criteria": str(payload.get("criteria") or ""),
        "cause": str(payload.get("cause") or ""),
        "effect": str(payload.get("effect") or ""),
        "recommendation": str(payload.get("recommendation") or ""),
        "management_response": str(payload.get("management_response") or ""),
        "rcm_refs": rcm_refs,
        "procedure_refs": procedure_refs,
        "test_refs": test_refs,
        "execution_refs": execution_refs,
        "evidence_refs": evidence_refs,
        "cause_pending": bool(payload.get("cause_pending", False)),
        "severity_rationale": str(payload.get("severity_rationale") or ""),
        "auditor_confirmed": bool(payload.get("auditor_confirmed", False)) if source == "manual" else False,
        "source": source,
        "created": created,
        "updated": created,
    }
    if any(row.get("id") == item["id"] for row in workspace.findings):
        raise WorkspaceError(f"Finding '{item['id']}' already exists.")
    if item["auditor_confirmed"]:
        issues = support_issues(workspace, item)
        if issues:
            raise WorkspaceError(
                "A finding cannot be auditor-confirmed: " + "; ".join(issues) + "."
            )
    workspace.findings.append(item)
    workspace.save()
    return item


def update(workspace: Workspace, finding_id: str, changes: dict) -> dict:
    item = _record(workspace, finding_id)
    allowed = {
        "title", "severity", "condition", "criteria", "cause", "effect",
        "recommendation", "management_response", "rcm_refs",
        "procedure_refs", "test_refs", "execution_refs", "evidence_refs",
        "cause_pending", "severity_rationale", "auditor_confirmed",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise WorkspaceError(f"Unknown finding field: {sorted(unknown)[0]}.")
    if "title" in changes and not str(changes["title"] or "").strip():
        raise WorkspaceError("Finding title is required.")
    if "severity" in changes:
        changes = {**changes, "severity": _severity(changes["severity"])}
    if "rcm_refs" in changes or "procedure_refs" in changes or "test_refs" in changes:
        rcm_refs, procedure_refs, test_refs = _validate_links(
            workspace,
            changes.get("rcm_refs", item.get("rcm_refs")),
            changes.get("procedure_refs", item.get("procedure_refs")),
            changes.get("test_refs", item.get("test_refs")),
        )
        changes = {
            **changes,
            "rcm_refs": rcm_refs,
            "procedure_refs": procedure_refs,
            "test_refs": test_refs,
        }
    if "evidence_refs" in changes:
        # A finding retains the source hash it was drafted from. A later test or
        # document change must be visible to the auditor, but must not prevent
        # them from confirming the finding after considering that warning.
        changes = {
            **changes,
            "evidence_refs": validate_evidence(
                workspace, changes["evidence_refs"], allow_changed=True
            ),
        }
    candidate = {**item, **changes}
    candidate["auditor_confirmed"] = bool(candidate.get("auditor_confirmed"))
    candidate["cause_pending"] = bool(candidate.get("cause_pending"))
    if candidate["auditor_confirmed"]:
        issues = support_issues(workspace, candidate)
        if issues:
            raise WorkspaceError(
                "A finding cannot be auditor-confirmed: " + "; ".join(issues) + "."
            )
    for key, value in changes.items():
        if key in ("rcm_refs", "procedure_refs", "test_refs", "execution_refs", "evidence_refs"):
            item[key] = value
        elif key in ("cause_pending", "auditor_confirmed"):
            item[key] = bool(value)
        else:
            item[key] = str(value or "")
    if item.get("created_by") == "agent":
        item["created_by"] = "user"
    item["updated"] = _now(workspace)
    workspace.save()
    return item


def remove(workspace: Workspace, finding_id: str) -> None:
    workspace.findings.remove(_record(workspace, finding_id))
    workspace.save()


def anchor_from_ref(workspace: Workspace, value: str, *, run_id: str | None = None) -> dict | None:
    kind, separator, source_id = str(value or "").partition(":")
    if not separator:
        return None
    # Agent artifacts call validation objects "ruleset" and document tests
    # "doctest"; accept the plural spelling defensively.
    kind = {"rulesets": "ruleset", "analyses": "analysis", "doc_test": "doctest"}.get(kind, kind)
    resolved = artifact(workspace, kind, source_id)
    if resolved is None:
        return None
    return normalize_anchor(
        {
            "source_kind": kind,
            "source_id": source_id,
            "source_sha1": resolved["sha1"],
            "generated_by": run_id,
        },
        require_hash=True,
    )


def promote(workspace: Workspace, run_id: str, agent_finding_id: str) -> dict:
    run = agent_store.load_run(workspace, run_id)
    source = next(
        (row for row in run.get("findings") or [] if str(row.get("id")) == agent_finding_id),
        None,
    )
    if source is None:
        raise WorkspaceError(f"Agent finding '{agent_finding_id}' not found in run '{run_id}'.")
    semantic_id = f"finding:run:{run_id}:{agent_finding_id}"
    existing = next((row for row in workspace.findings if row.get("semantic_id") == semantic_id), None)
    if existing:
        return existing
    anchors = [
        anchor
        for value in source.get("evidence_refs") or []
        if (anchor := anchor_from_ref(workspace, str(value), run_id=run_id)) is not None
    ]
    statement = str(source.get("statement") or "").strip()
    return add(
        workspace,
        {
            "semantic_id": semantic_id,
            "agent_run_id": run_id,
            "title": statement[:120] or "Promoted agent observation",
            "severity": source.get("severity") or "medium",
            "condition": statement,
            "evidence_refs": anchors,
        },
        source="promoted",
    )


def rollups(workspace: Workspace) -> dict:
    by_rcm: dict[str, list[dict]] = {}
    by_procedure: dict[str, list[dict]] = {}
    by_test: dict[str, list[dict]] = {}
    for item in workspace.findings:
        summary = {key: item.get(key) for key in ("id", "title", "severity")}
        for ref in item.get("rcm_refs") or []:
            by_rcm.setdefault(ref, []).append(summary)
        for ref in item.get("procedure_refs") or []:
            by_procedure.setdefault(ref, []).append(summary)
        for ref in item.get("test_refs") or []:
            by_test.setdefault(ref, []).append(summary)
    return {
        "by_rcm": by_rcm,
        "by_test": by_test,
        "by_procedure": by_procedure,
    }
