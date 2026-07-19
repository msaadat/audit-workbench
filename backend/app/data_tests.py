"""Durable Data Tests and immutable execution results.

Definitions live in ``workspace.json`` so they participate in engagement
migration and reconciliation. Each run is written separately under
``DataTestResults``; the workspace stores only bounded latest-run metadata and
history references. Tests may be exploratory or linked to exactly one RCM
planned test. Creating or editing a definition never counts as execution.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from . import analytics, explore, sandbox, validation
from .agent import joins as join_diagnostics
from .workspaces import Workspace, WorkspaceError, slugify, write_json_atomic

ENGINES = {"analytics", "validation", "polars"}
STATUSES = {
    "not_ready",
    "ready",
    "in_progress",
    "review_required",
    "blocked",
    "completed_no_exception",
    "completed_with_exception",
    "not_applicable",
}
AUDITOR_DISPOSITIONS = {
    "pending",
    "follow_up",
    "accepted",
    "invalid_test_or_result",
    "not_applicable",
}
RUN_HISTORY_MAX = 20
SUMMARY_ROWS = 500
EXCEPTION_ROWS = 200


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha1(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def results_dir(workspace: Workspace, data_test_id: str | None = None) -> Path:
    path = workspace.root / "DataTestResults"
    if data_test_id:
        path = path / data_test_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _result_path(workspace: Workspace, data_test_id: str, run_id: str) -> Path:
    if not data_test_id.startswith("DAT-") or not run_id.startswith("DTR-"):
        raise WorkspaceError("Invalid Data Test result reference.")
    return results_dir(workspace, data_test_id) / f"{run_id}.json"


def _record(workspace: Workspace, data_test_id: str) -> dict:
    item = next(
        (value for value in workspace.data_tests if value.get("id") == data_test_id),
        None,
    )
    if item is None:
        raise WorkspaceError(f"Data Test '{data_test_id}' not found.")
    item.setdefault("runs", [])
    item.setdefault("last_run", None)
    item.setdefault("auditor_disposition", "pending")
    item.setdefault("evidence_refs", [])
    return item


def _validate_parent(
    workspace: Workspace, rcm_id: object, planned_test_id: object
) -> tuple[str | None, str | None]:
    rcm_value = str(rcm_id or "").strip()
    planned_value = str(planned_test_id or "").strip()
    if not rcm_value and not planned_value:
        return None, None
    if not rcm_value or not planned_value:
        raise WorkspaceError(
            "Provide both an RCM row and planned test, or leave both blank for exploration."
        )
    row, planned = workspace.planned_test(planned_value)
    if row.get("id") != rcm_value:
        raise WorkspaceError(
            f"Planned test '{planned_value}' does not belong to RCM row '{rcm_value}'."
        )
    if planned.get("method") not in {"data_analytics", "validation", "hybrid"}:
        raise WorkspaceError(
            f"Planned-test method '{planned.get('method')}' does not permit a Data Test."
        )
    return rcm_value, planned_value


def _table_refs(workspace: Workspace, values: object) -> list[str]:
    refs = list(dict.fromkeys(str(value).strip() for value in (values or []) if str(value).strip()))
    if not refs:
        raise WorkspaceError("A Data Test needs at least one table.")
    missing = next((value for value in refs if value not in workspace.table_names()), None)
    if missing:
        raise WorkspaceError(f"Unknown table '{missing}'.")
    return refs


def _validate_spec(workspace: Workspace, engine: str, refs: list[str], spec: object) -> tuple[dict, list[str]]:
    value = dict(spec or {})
    warnings: list[str] = []
    if engine == "analytics":
        test_id = analytics.canonical_test_id(value.get("test_id"))
        if test_id not in analytics.ANALYTICS:
            raise WorkspaceError(f"Unknown analytics test '{test_id}'.")
        try:
            params = analytics.canonicalize_params(
                workspace.get_frame(refs[0]), test_id, dict(value.get("params") or {})
            )
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        value = {**value, "test_id": test_id, "params": params}
        if test_id == "rare_values":
            column = str(params.get("column") or "")
            frame = workspace.get_frame(refs[0])
            if column and frame.height and frame[column].n_unique() / frame.height > 0.9:
                warnings.append("Rare-value screening targets a naturally unique field.")
    elif engine == "validation":
        rules = value.get("rules") or []
        if not isinstance(rules, list) or not rules:
            raise WorkspaceError("A validation Data Test needs at least one rule.")
        frame = workspace.get_frame(refs[0])
        try:
            rules = validation.canonicalize_rules(
                frame, rules, resolve=workspace.get_frame, strict=True
            )
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        value = {**value, "rules": rules}
        warnings.extend(validation.generated_rule_issues(frame, rules, workspace.get_frame))
    else:
        code = str(value.get("code") or "").strip()
        if not code:
            raise WorkspaceError("A Polars Data Test needs code.")
        try:
            sandbox.validate(code)
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        value["code"] = code
    return value, warnings


def _link(workspace: Workspace, item: dict) -> None:
    ref = f"datatest:{item['id']}"
    for row in workspace.rcm:
        for planned in row.get("planned_tests") or []:
            refs = planned.setdefault("execution_refs", [])
            if planned.get("id") == item.get("planned_test_id") and ref not in refs:
                refs.append(ref)
            elif planned.get("id") != item.get("planned_test_id"):
                planned["execution_refs"] = [value for value in refs if value != ref]


def create(workspace: Workspace, payload: dict) -> dict:
    title = str(payload.get("title") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    if not title or not objective:
        raise WorkspaceError("Data Test title and objective are required.")
    engine = str(payload.get("engine") or "").strip().lower()
    if engine not in ENGINES:
        raise WorkspaceError("Data Test engine must be analytics, validation, or polars.")
    rcm_id, planned_test_id = _validate_parent(
        workspace, payload.get("rcm_id"), payload.get("planned_test_id")
    )
    refs = _table_refs(workspace, payload.get("table_refs"))
    spec, warnings = _validate_spec(workspace, engine, refs, payload.get("spec"))
    item_id = str(payload.get("id") or f"DAT-{uuid.uuid4().hex[:10].upper()}")
    semantic_id = str(payload.get("semantic_id") or f"datatest:{slugify(title)}")
    if any(
        value.get("id") == item_id or value.get("semantic_id") == semantic_id
        for value in workspace.data_tests
    ):
        raise WorkspaceError("A Data Test with that ID or semantic ID already exists.")
    now = utcnow()
    item = {
        "id": item_id,
        "semantic_id": semantic_id,
        "rcm_id": rcm_id,
        "planned_test_id": planned_test_id,
        "title": title,
        "objective": objective,
        "engine": engine,
        "table_refs": refs,
        "spec": spec,
        "status": "ready",
        "semantic_warnings": warnings,
        "last_run": None,
        "runs": [],
        "auditor_disposition": "pending",
        "evidence_refs": [],
        "created_by": "agent" if payload.get("agent_run_id") else "user",
        "agent_run_id": payload.get("agent_run_id"),
        "workflow_parent_sha1": str(payload.get("workflow_parent_sha1") or "") or None,
        "created": now,
        "updated": now,
    }
    workspace.data_tests.append(item)
    _link(workspace, item)
    workspace.save()
    return item


def update(
    workspace: Workspace,
    data_test_id: str,
    changes: dict,
    *,
    agent: bool = False,
) -> dict:
    item = _record(workspace, data_test_id)
    allowed = {
        "title", "objective", "engine", "table_refs", "spec", "rcm_id",
        "planned_test_id", "auditor_disposition", "workflow_parent_sha1",
    }
    unknown = set(changes) - allowed
    if "workflow_parent_sha1" in changes and not agent:
        unknown.add("workflow_parent_sha1")
    if unknown:
        raise WorkspaceError(f"Unknown Data Test field: {sorted(unknown)[0]}.")
    title = str(changes.get("title", item["title"]) or "").strip()
    objective = str(changes.get("objective", item["objective"]) or "").strip()
    if not title or not objective:
        raise WorkspaceError("Data Test title and objective are required.")
    engine = str(changes.get("engine", item["engine"]) or "").lower()
    if engine not in ENGINES:
        raise WorkspaceError("Data Test engine must be analytics, validation, or polars.")
    rcm_id, planned_test_id = _validate_parent(
        workspace,
        changes.get("rcm_id", item["rcm_id"]),
        changes.get("planned_test_id", item["planned_test_id"]),
    )
    refs = _table_refs(workspace, changes.get("table_refs", item["table_refs"]))
    spec, warnings = _validate_spec(
        workspace, engine, refs, changes.get("spec", item["spec"])
    )
    disposition = str(
        changes.get("auditor_disposition", item.get("auditor_disposition") or "pending")
    )
    if disposition not in AUDITOR_DISPOSITIONS:
        raise WorkspaceError("Unknown Data Test auditor disposition.")
    item.update(
        title=title,
        objective=objective,
        engine=engine,
        rcm_id=rcm_id,
        planned_test_id=planned_test_id,
        table_refs=refs,
        spec=spec,
        semantic_warnings=warnings,
        auditor_disposition=disposition,
        workflow_parent_sha1=str(
            changes.get("workflow_parent_sha1", item.get("workflow_parent_sha1")) or ""
        ) or None,
        updated=utcnow(),
    )
    if not agent and item.get("created_by") == "agent":
        item["created_by"] = "user"
    # A changed definition must be executed again; history remains immutable.
    item["status"] = "ready"
    _link(workspace, item)
    workspace.save()
    return item


def remove(workspace: Workspace, data_test_id: str) -> None:
    item = _record(workspace, data_test_id)
    if item.get("runs"):
        raise WorkspaceError("A Data Test with execution history cannot be removed.")
    workspace.data_tests.remove(item)
    ref = f"datatest:{data_test_id}"
    for row in workspace.rcm:
        for planned in row.get("planned_tests") or []:
            planned["execution_refs"] = [
                value for value in planned.get("execution_refs", []) if value != ref
            ]
    workspace.save()


def _dataset_fingerprints(workspace: Workspace, refs: list[str]) -> dict[str, str]:
    return {name: _sha1(workspace._table_signature(name)) for name in refs}


def _join_issues(workspace: Workspace, refs: list[str]) -> tuple[list[str], list[dict]]:
    issues: list[str] = []
    diagnostics: list[dict] = []
    for name in refs:
        join = next((value for value in workspace.joins if value.get("name") == name), None)
        if not join or join.get("how") == "cross" or len(join.get("left_on") or []) != 1:
            continue
        detail = join_diagnostics.diagnose(
            workspace.get_frame(join["left"]),
            workspace.get_frame(join["right"]),
            join["left_on"][0],
            join["right_on"][0],
        )
        diagnostics.append({"table": name, **detail})
        if detail["match_rate"] == 0:
            issues.append(f"Join '{name}' has 0% key match coverage.")
        elif detail["match_rate"] < join_diagnostics.GOOD_MATCH_RATE:
            issues.append(
                f"Join '{name}' key match coverage is only {detail['match_rate']:.1%}."
            )
        if detail["row_multiplication"] > join_diagnostics.MAX_ROW_MULTIPLICATION:
            issues.append(
                f"Join '{name}' multiplies rows by {detail['row_multiplication']:.2f}."
            )
    return issues, diagnostics


def _all_null_columns(frame: pl.DataFrame | None) -> list[str]:
    if frame is None or frame.height == 0:
        return []
    return [name for name in frame.columns if frame[name].null_count() == frame.height]


def _run_engine(workspace: Workspace, item: dict) -> tuple[dict, pl.DataFrame | None, pl.DataFrame | None, int, list[str]]:
    engine = item["engine"]
    frame = workspace.get_frame(item["table_refs"][0])
    issues = list(item.get("semantic_warnings") or [])
    if engine == "analytics":
        result = analytics.run_test(frame, item["spec"]["test_id"], item["spec"].get("params") or {})
        payload = result.payload()
        summary, exceptions = result.summary, result.detail
        exception_count = result.detail.height if result.detail is not None else 0
        verdict = result.verdict
        if item["spec"]["test_id"] in {"benford", "last_two_digits", "outliers"} and verdict in {"warn", "fail"}:
            issues.append("Screening result requires corroboration before it can support a control exception.")
        output = {
            "verdict": verdict,
            "statistics": payload["stats"],
            "verdict_text": payload["verdict_text"],
            "viz": payload.get("viz"),
        }
    elif engine == "validation":
        rules = item["spec"]["rules"]
        run = validation.run_rules(frame, rules, item["table_refs"][0], workspace.get_frame)
        summary = validation.summary_frame(run)
        failing = []
        for rule, result in zip(rules, run["results"]):
            if result["fail_count"] and result["verdict"] not in {"error", "skipped"}:
                detail = validation.rule_failures(frame, rule, workspace.get_frame).head(EXCEPTION_ROWS)
                failing.append(detail.with_columns(pl.lit(result["rule_id"]).alias("_rule_id")))
        exceptions = pl.concat(failing, how="diagonal_relaxed") if failing else None
        exception_count = sum(result["fail_count"] for result in run["results"])
        output = {
            "verdict": run["verdict"],
            "statistics": [
                {"label": label.replace("_", " ").title(), "value": str(value)}
                for label, value in run["counts"].items()
            ],
            "verdict_text": f"{exception_count} validation exception(s).",
            "viz": {"type": "table"},
        }
        issues.extend(validation.generated_rule_issues(frame, rules, workspace.get_frame))
    else:
        frames = {name: workspace.get_frame(name) for name in item["table_refs"]}
        result, stdout = sandbox.run(item["spec"]["code"], frames)
        summary = result
        exceptions = result if item["spec"].get("result_mode", "exceptions") == "exceptions" else None
        exception_count = result.height if exceptions is not None else int(item["spec"].get("exception_count") or 0)
        output = {
            "verdict": "fail" if exception_count else "ok",
            "statistics": [{"label": "Result rows", "value": str(result.height)}],
            "verdict_text": f"{exception_count} exception row(s).",
            "viz": dict(item["spec"].get("viz") or {"type": "table"}),
            "stdout": stdout,
        }
    null_columns = _all_null_columns(summary)
    if null_columns:
        issues.append(f"Result columns are entirely null: {', '.join(null_columns)}.")
    return output, summary, exceptions, exception_count, list(dict.fromkeys(issues))


def compute(workspace: Workspace, data_test_id: str) -> dict:
    """Compute an immutable result candidate without mutating the workspace."""
    item = _record(workspace, data_test_id)
    run_id = f"DTR-{uuid.uuid4().hex[:12].upper()}"
    run_at = utcnow()
    fingerprints = _dataset_fingerprints(workspace, item["table_refs"])
    source_sha1 = _sha1(
        {"engine": item["engine"], "table_refs": item["table_refs"], "spec": item["spec"]}
    )
    join_issues, diagnostics = _join_issues(workspace, item["table_refs"])
    try:
        output, summary, exceptions, exception_count, semantic_issues = _run_engine(workspace, item)
        semantic_issues = list(dict.fromkeys([*join_issues, *semantic_issues]))
        semantic_valid = not any(
            "0% key match" in issue
            or "multiplies rows" in issue
            or "entirely null" in issue
            or "naturally unique" in issue
            or "conditional trigger matches zero" in issue
            or "allowed values have no overlap" in issue
            for issue in semantic_issues
        )
        status = (
            "review_required"
            if not semantic_valid
            else "completed_with_exception"
            if exception_count or output["verdict"] in {"warn", "fail"}
            else "completed_no_exception"
        )
        error = None
    except Exception as exc:
        output = {"verdict": "error", "statistics": [], "verdict_text": str(exc), "viz": None}
        summary = exceptions = None
        exception_count = 0
        semantic_issues = list(dict.fromkeys([*join_issues, str(exc)]))
        semantic_valid = False
        status = "review_required"
        error = str(exc)
    result = {
        "id": run_id,
        "data_test_id": data_test_id,
        "rcm_id": item["rcm_id"],
        "planned_test_id": item["planned_test_id"],
        "run_at": run_at,
        "status": status,
        "verdict": output["verdict"],
        "verdict_text": output.get("verdict_text") or "",
        "statistics": output.get("statistics") or [],
        "viz": output.get("viz"),
        "stdout": output.get("stdout") or "",
        "dataset_fingerprints": fingerprints,
        "source_sha1": source_sha1,
        "summary_frame": explore.frame_payload(summary, SUMMARY_ROWS) if summary is not None else None,
        "exception_frame": explore.frame_payload(exceptions, EXCEPTION_ROWS) if exceptions is not None else None,
        "exception_count": exception_count,
        "semantic_valid": semantic_valid,
        "semantic_issues": semantic_issues,
        "join_diagnostics": diagnostics,
        "error": error,
    }
    result["result_sha1"] = _sha1(result)
    return result


def commit_result(
    workspace: Workspace,
    data_test_id: str,
    result: dict,
    *,
    expected_definition_sha1: str | None = None,
) -> dict:
    """Commit one computed candidate under parent-hash/revision coordination."""
    from .workspace_transactions import (
        canonical_sha1,
        complete_linked_write,
        material_projection,
        mutate,
        parent_hashes,
        prepare_linked_write,
        rollback_linked_write,
    )

    expected = expected_definition_sha1 or parent_hashes(
        workspace, [f"datatest:{data_test_id}"]
    )[f"datatest:{data_test_id}"]

    linked_writes = []

    def commit(fresh: Workspace) -> dict:
        item = _record(fresh, data_test_id)
        candidate = dict(result)
        supplied_sha1 = candidate.get("result_sha1")
        actual_sha1 = _sha1({key: value for key, value in candidate.items() if key != "result_sha1"})
        if supplied_sha1 != actual_sha1:
            raise WorkspaceError("The Data Test result candidate failed its integrity check.")
        if canonical_sha1(material_projection(item)) != expected:
            # ``mutate`` normally catches this first. Keep the check next to
            # the file write so future direct callers cannot bypass it.
            raise WorkspaceError("The Data Test definition changed before its result could be committed.")
        result_path = _result_path(fresh, data_test_id, str(candidate["id"]))
        linked_write = prepare_linked_write(fresh, result_path, candidate)
        linked_writes.append(linked_write)
        write_json_atomic(result_path, candidate)
        history = {
            key: candidate[key]
            for key in (
                "id", "run_at", "status", "verdict", "exception_count", "semantic_valid",
                "dataset_fingerprints", "source_sha1", "result_sha1",
            )
        }
        item.setdefault("runs", []).append(history)
        del item["runs"][:-RUN_HISTORY_MAX]
        item["last_run"] = history
        item["status"] = candidate["status"]
        item["updated"] = candidate["run_at"]
        return candidate

    try:
        committed = mutate(
            workspace,
            commit,
            expected_parents={f"datatest:{data_test_id}": expected},
        )
    except Exception:
        for linked_write in reversed(linked_writes):
            rollback_linked_write(linked_write)
        raise
    else:
        for linked_write in linked_writes:
            complete_linked_write(linked_write)
    from .workspaces import sync_workspace

    # Preserve the long-standing service contract for callers that retain row,
    # planned-test, or Data Test references after ``run``.
    sync_workspace(workspace, committed.workspace)
    return committed.value


def run(workspace: Workspace, data_test_id: str) -> dict:
    expected = None
    from .workspace_transactions import parent_hashes

    expected = parent_hashes(workspace, [f"datatest:{data_test_id}"])[
        f"datatest:{data_test_id}"
    ]
    result = compute(workspace, data_test_id)
    return commit_result(
        workspace,
        data_test_id,
        result,
        expected_definition_sha1=expected,
    )


def load_result(workspace: Workspace, data_test_id: str, run_id: str) -> dict:
    _record(workspace, data_test_id)
    path = _result_path(workspace, data_test_id, run_id)
    if not path.exists():
        raise WorkspaceError(f"Data Test result '{run_id}' not found.")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Data Test result '{run_id}' is unreadable.") from error
    expected = result.get("result_sha1")
    actual = _sha1({key: value for key, value in result.items() if key != "result_sha1"})
    if expected != actual:
        raise WorkspaceError(f"Data Test result '{run_id}' failed its integrity check.")
    return result


def result_artifact(workspace: Workspace, source_id: str) -> dict | None:
    data_test_id, separator, run_id = str(source_id).partition(":")
    if not separator:
        item = next((value for value in workspace.data_tests if value.get("id") == source_id), None)
        if not item or not item.get("last_run"):
            return None
        run_id = item["last_run"]["id"]
        data_test_id = item["id"]
    try:
        result = load_result(workspace, data_test_id, run_id)
    except WorkspaceError:
        return None
    return {"item": result, "sha1": result["result_sha1"]}


def list_payload(workspace: Workspace) -> list[dict]:
    return sorted(workspace.data_tests, key=lambda item: item.get("updated") or "", reverse=True)
