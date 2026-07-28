"""Durable Data Tests and their current execution result.

Definitions live in ``workspace.json`` so they participate in engagement
migration and reconciliation. Each test owns one replaceable result under
``DataTestResults``; the workspace stores only its latest-run metadata. A test
may be exploratory or linked to one RCM row. Creating or editing a definition
never counts as execution.
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
from .workspaces import (
    CONTROL_CONCLUSIONS,
    TEST_STATUSES,
    Workspace,
    WorkspaceError,
    slugify,
    write_json_atomic,
)

ENGINES = {"analytics", "validation", "polars"}
STATUSES = TEST_STATUSES
AUDITOR_DISPOSITIONS = {
    "pending",
    "follow_up",
    "accepted",
    "invalid_test_or_result",
    "not_applicable",
}
CURRENT_RESULT_ID = "DTR-CURRENT"
SUMMARY_ROWS = 500
EXCEPTION_ROWS = 200


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha1(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _step_id(semantic_id: str, label: str, ordinal: int) -> str:
    return f"STEP-{_sha1([semantic_id, label, ordinal])[:10].upper()}"


def _normalize_steps(values: object) -> list[dict]:
    """Preserve declared step fields as objects; reject a step that is not an object."""
    steps: list[dict] = []
    for value in values or []:
        if not value:
            continue
        if not isinstance(value, dict):
            raise WorkspaceError("Each test step must be an object.")
        steps.append(dict(value))
    return steps


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


def _finding_result_ids(workspace: Workspace, data_test_id: str) -> set[str]:
    """Legacy result IDs that must remain available as finding evidence."""
    ids: set[str] = set()
    for finding in workspace.findings:
        for anchor in finding.get("evidence_refs") or []:
            if not isinstance(anchor, dict) or anchor.get("source_kind") != "datatest":
                continue
            test_id, separator, result_id = str(anchor.get("source_id") or "").partition(":")
            if separator and test_id == data_test_id and result_id:
                ids.add(result_id)
    return ids


def _discard_superseded_results(workspace: Workspace, data_test_id: str) -> None:
    """Remove prior non-evidence results after the current result is committed."""
    retained = _finding_result_ids(workspace, data_test_id)
    folder = workspace.root / "DataTestResults" / data_test_id
    if not folder.is_dir():
        return
    for path in folder.glob("DTR-*.json"):
        if path.stem != CURRENT_RESULT_ID and path.stem not in retained:
            path.unlink()


def _record(workspace: Workspace, data_test_id: str) -> dict:
    item = next(
        (value for value in workspace.data_tests if value.get("id") == data_test_id),
        None,
    )
    if item is None:
        raise WorkspaceError(f"Data Test '{data_test_id}' not found.")
    # Execution history was retired. Old workspaces can still carry this field
    # until their next mutation, but it is never returned or persisted again.
    item.pop("runs", None)
    item.setdefault("last_run", None)
    item.setdefault("auditor_disposition", "pending")
    item.setdefault("evidence_refs", [])
    item.setdefault("criteria", "")
    item.setdefault("steps", [])
    item.setdefault("methodology_refs", [])
    item.setdefault("conclusion", "")
    item.setdefault("control_conclusion", "no_conclusion")
    item.setdefault("result_summary", "")
    item.setdefault("scope_limitations", "")
    item.setdefault("next_action", "")
    item.setdefault("exception_count", 0)
    item.setdefault("open_exception_count", 0)
    item.setdefault("finding_refs", [])
    return item


def _validate_rcm_id(workspace: Workspace, rcm_id: object) -> str | None:
    """Resolve the optional RCM row a test covers.

    Leaving it blank is exploration; an exploratory result is a durable analysis
    artifact but never RCM coverage.
    """
    value = str(rcm_id or "").strip() or None
    if value and not any(row.get("id") == value for row in workspace.rcm):
        raise WorkspaceError(f"RCM row '{value}' not found.")
    return value


def _table_refs(workspace: Workspace, values: object, *, required: bool = True) -> list[str]:
    refs = list(dict.fromkeys(str(value).strip() for value in (values or []) if str(value).strip()))
    if not refs and required:
        raise WorkspaceError("A Data Test needs at least one table.")
    missing = next((value for value in refs if value not in workspace.table_names()), None)
    if missing:
        raise WorkspaceError(f"Unknown table '{missing}'.")
    return refs


def _validate_spec(
    workspace: Workspace, engine: str, refs: list[str], spec: object, *, semantic_id: str = ""
) -> tuple[dict, list[str], list[str]]:
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
        if "code" in value:
            raise WorkspaceError("A Polars Data Test uses spec.steps, not a single spec.code.")
        if value.get("schema_version") != 2:
            raise WorkspaceError("A Polars Data Test needs spec.schema_version 2.")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise WorkspaceError("A Polars Data Test needs at least one step.")
        steps: list[dict] = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                raise WorkspaceError("Each Polars Data Test step must be an object.")
            label = str(raw_step.get("label") or "").strip()
            instruction = str(raw_step.get("instruction") or "").strip()
            if not label:
                raise WorkspaceError(f"Data Test step {index + 1} needs a label.")
            if not instruction:
                raise WorkspaceError(f"Data Test step '{label}' needs an instruction.")
            code = str(raw_step.get("code") or "").strip()
            if not code:
                raise WorkspaceError(f"Data Test step '{label}' needs code.")
            try:
                sandbox.validate(code)
            except ValueError as error:
                raise WorkspaceError(str(error)) from error
            step_id = str(raw_step.get("step_id") or "").strip() or _step_id(semantic_id, label, index)
            steps.append(
                {
                    "step_id": step_id,
                    "label": label,
                    "instruction": instruction,
                    "code": code,
                }
            )
        value = {"schema_version": 2, "steps": steps}
    return value, warnings, refs


def _link(workspace: Workspace, item: dict) -> None:
    """Keep ``rcm[].test_refs`` in step with this test's RCM link."""
    ref = f"datatest:{item['id']}"
    for row in workspace.rcm:
        refs = row.setdefault("test_refs", [])
        if row.get("id") == item.get("rcm_id"):
            if ref not in refs:
                refs.append(ref)
        elif ref in refs:
            row["test_refs"] = [value for value in refs if value != ref]


def _base_record(workspace: Workspace, payload: dict, *, title: str, now: str) -> dict:
    item_id = str(payload.get("id") or f"DAT-{uuid.uuid4().hex[:10].upper()}")
    semantic_id = str(payload.get("semantic_id") or f"datatest:{slugify(title)}")
    if any(
        value.get("id") == item_id or value.get("semantic_id") == semantic_id
        for value in workspace.data_tests
    ):
        raise WorkspaceError("A Data Test with that ID or semantic ID already exists.")
    return {
        "id": item_id,
        "semantic_id": semantic_id,
        "rcm_id": _validate_rcm_id(workspace, payload.get("rcm_id")),
        "title": title,
        "objective": str(payload.get("objective") or "").strip(),
        # Audit plan — the fields that used to live on the RCM planned test.
        "criteria": str(payload.get("criteria") or ""),
        "steps": _normalize_steps(payload.get("steps")),
        "methodology_refs": list(payload.get("methodology_refs") or []),
        # Outcome.
        "conclusion": "",
        "control_conclusion": "no_conclusion",
        "result_summary": "",
        "scope_limitations": "",
        "next_action": "",
        "exception_count": 0,
        "open_exception_count": 0,
        "finding_refs": [],
        "last_run": None,
        "auditor_disposition": "pending",
        "evidence_refs": [],
        "created_by": "agent" if payload.get("agent_run_id") else "user",
        "agent_run_id": payload.get("agent_run_id"),
        "workflow_parent_sha1": str(payload.get("workflow_parent_sha1") or "") or None,
        "created": now,
        "updated": now,
    }


def create(workspace: Workspace, payload: dict) -> dict:
    title = str(payload.get("title") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    if not title or not objective:
        raise WorkspaceError("Data Test title and objective are required.")
    engine = str(payload.get("engine") or "").strip().lower()
    if engine not in ENGINES:
        raise WorkspaceError("Data Test engine must be analytics, validation, or polars.")
    semantic_id = str(payload.get("semantic_id") or f"datatest:{slugify(title)}")
    refs = [] if engine == "polars" else _table_refs(workspace, payload.get("table_refs"))
    spec, warnings, refs = _validate_spec(
        workspace, engine, refs, payload.get("spec"), semantic_id=semantic_id
    )
    item = {
        **_base_record(workspace, {**payload, "semantic_id": semantic_id}, title=title, now=utcnow()),
        "engine": engine,
        "table_refs": refs,
        "spec": spec,
        "status": "ready",
        "semantic_warnings": warnings,
    }
    workspace.data_tests.append(item)
    _link(workspace, item)
    workspace.save()
    return item


def create_draft(workspace: Workspace, payload: dict) -> dict:
    """Create a planned-but-unspecified Data Test.

    This is what the draft pass of test generation commits: the audit plan for
    one test, with no engine, tables, or code yet. :func:`apply_spec` fills those
    in and moves the record out of ``draft``.
    """
    title = str(payload.get("title") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    if not title or not objective:
        raise WorkspaceError("Data Test title and objective are required.")
    item = {
        **_base_record(workspace, payload, title=title, now=utcnow()),
        "engine": None,
        "table_refs": [],
        "spec": {},
        "status": "draft",
        "semantic_warnings": [],
    }
    workspace.data_tests.append(item)
    _link(workspace, item)
    workspace.save()
    return item


PLAN_FIELDS = (
    "title",
    "objective",
    "criteria",
    "steps",
    "methodology_refs",
)


def update_plan(workspace: Workspace, data_test_id: str, payload: dict) -> dict:
    """Rewrite one test's audit plan, leaving its spec and results untouched.

    This is what a re-run of the draft pass commits onto a test it has already
    created; the executable spec belongs to :func:`apply_spec`.
    """
    item = _record(workspace, data_test_id)
    for key in PLAN_FIELDS:
        if key not in payload:
            continue
        if key == "steps":
            item["steps"] = _normalize_steps(payload["steps"])
        elif key == "methodology_refs":
            item["methodology_refs"] = list(payload["methodology_refs"] or [])
        else:
            item[key] = str(payload[key] or "")
    if "rcm_id" in payload:
        item["rcm_id"] = _validate_rcm_id(workspace, payload["rcm_id"])
        _link(workspace, item)
    for key in ("agent_run_id", "workflow_parent_sha1"):
        if payload.get(key):
            item[key] = str(payload[key])
    item["updated"] = utcnow()
    workspace.save()
    return item


def apply_spec(workspace: Workspace, data_test_id: str, payload: dict) -> dict:
    """Write the executable spec onto an existing test.

    The current result is retained until the updated definition is run again.
    """
    item = _record(workspace, data_test_id)
    engine = str(payload.get("engine") or "polars").strip().lower()
    if engine not in ENGINES:
        raise WorkspaceError("Data Test engine must be analytics, validation, or polars.")
    refs = [] if engine == "polars" else _table_refs(workspace, payload.get("table_refs"))
    spec, warnings, refs = _validate_spec(
        workspace, engine, refs, payload.get("spec"), semantic_id=item["semantic_id"]
    )
    item.update(
        engine=engine,
        table_refs=refs,
        spec=spec,
        semantic_warnings=warnings,
        status="ready",
        updated=utcnow(),
    )
    if payload.get("title"):
        item["title"] = str(payload["title"]).strip()
    if payload.get("workflow_parent_sha1"):
        item["workflow_parent_sha1"] = str(payload["workflow_parent_sha1"])
    if payload.get("agent_run_id"):
        item["agent_run_id"] = str(payload["agent_run_id"])
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
        "auditor_disposition", "workflow_parent_sha1", "criteria", "steps",
        "methodology_refs", "conclusion", "control_conclusion",
        "scope_limitations", "next_action",
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
    rcm_id = _validate_rcm_id(workspace, changes.get("rcm_id", item["rcm_id"]))
    refs = (
        []
        if engine == "polars"
        else _table_refs(workspace, changes.get("table_refs", item["table_refs"]))
    )
    spec, warnings, refs = _validate_spec(
        workspace, engine, refs, changes.get("spec", item["spec"]), semantic_id=item["semantic_id"]
    )
    disposition = str(
        changes.get("auditor_disposition", item.get("auditor_disposition") or "pending")
    )
    if disposition not in AUDITOR_DISPOSITIONS:
        raise WorkspaceError("Unknown Data Test auditor disposition.")
    conclusion = str(
        changes.get("control_conclusion", item.get("control_conclusion") or "no_conclusion")
    )
    if conclusion not in CONTROL_CONCLUSIONS:
        raise WorkspaceError("Unknown control conclusion.")
    item.update(
        title=title,
        objective=objective,
        engine=engine,
        rcm_id=rcm_id,
        table_refs=refs,
        spec=spec,
        semantic_warnings=warnings,
        auditor_disposition=disposition,
        control_conclusion=conclusion,
        criteria=str(changes.get("criteria", item.get("criteria") or "")),
        steps=_normalize_steps(changes.get("steps", item.get("steps"))),
        methodology_refs=list(changes.get("methodology_refs", item.get("methodology_refs")) or []),
        conclusion=str(changes.get("conclusion", item.get("conclusion") or "")),
        scope_limitations=str(
            changes.get("scope_limitations", item.get("scope_limitations") or "")
        ),
        next_action=str(changes.get("next_action", item.get("next_action") or "")),
        workflow_parent_sha1=str(
            changes.get("workflow_parent_sha1", item.get("workflow_parent_sha1")) or ""
        ) or None,
        updated=utcnow(),
    )
    if not agent and item.get("created_by") == "agent":
        item["created_by"] = "user"
    # A changed *definition* must be executed again; history remains immutable.
    # Editing the plan or recording an outcome is not a definition change, so it
    # must not discard the status the current result established.
    if any(key in changes for key in ("engine", "table_refs", "spec")):
        item["status"] = "ready"
    _link(workspace, item)
    workspace.save()
    return item


def remove(workspace: Workspace, data_test_id: str) -> None:
    item = _record(workspace, data_test_id)
    workspace.data_tests.remove(item)
    ref = f"datatest:{data_test_id}"
    for row in workspace.rcm:
        row["test_refs"] = [value for value in row.get("test_refs", []) if value != ref]
    workspace.save()


def _dataset_fingerprints(workspace: Workspace, refs: list[str]) -> dict[str, str]:
    return {name: _sha1(workspace._table_signature(name)) for name in refs}


def _execution_table_refs(workspace: Workspace, item: dict) -> list[str]:
    """Return every frame available to a Polars test at execution time.

    A Polars definition deliberately carries no table selection. This keeps its
    sandbox environment in step with the workspace as tables and joins change,
    and makes the result provenance cover every frame that its code could read.
    """
    if item.get("engine") == "polars":
        return workspace.table_names()
    return list(item.get("table_refs") or [])


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


def _run_polars_steps(
    workspace: Workspace, item: dict
) -> tuple[dict, pl.DataFrame | None, pl.DataFrame | None, int, list[str]]:
    """Run every step independently and roll the results up deterministically."""
    steps = item["spec"]["steps"]
    issues: list[str] = []
    step_results: list[dict] = []
    summary_frames: list[pl.DataFrame] = []
    exception_frames: list[pl.DataFrame] = []
    stdout_parts: list[str] = []
    total_exceptions = 0
    any_step_failed = False
    frames = {name: workspace.get_frame(name) for name in workspace.table_names()}
    for step in steps:
        try:
            result, stdout = sandbox.run(step["code"], frames)
        except Exception as exc:
            any_step_failed = True
            issues.append(f"Step '{step['label']}' failed to execute: {exc}")
            step_results.append(
                {
                    "step_id": step["step_id"],
                    "step_label": step["label"],
                    "status": "error",
                    "exception_count": 0,
                    "error": str(exc),
                }
            )
            continue
        stdout_parts.append(stdout)
        step_null_columns = _all_null_columns(result)
        if step_null_columns:
            issues.append(
                f"Step '{step['label']}' result columns are entirely null: "
                f"{', '.join(step_null_columns)}."
            )
        step_exception_count = result.height
        total_exceptions += step_exception_count
        summary_frames.append(result)
        if step_exception_count:
            exception_frames.append(
                result.with_columns(
                    pl.lit(step["step_id"]).alias("_step_id"),
                    pl.lit(step["label"]).alias("_step_label"),
                )
            )
        step_results.append(
            {
                "step_id": step["step_id"],
                "step_label": step["label"],
                "status": "completed_with_exception" if step_exception_count else "completed_no_exception",
                "exception_count": step_exception_count,
                "error": None,
            }
        )
    summary = pl.concat(summary_frames, how="diagonal_relaxed") if summary_frames else None
    exceptions = pl.concat(exception_frames, how="diagonal_relaxed") if exception_frames else None
    if any_step_failed:
        issues.append("One or more steps failed to execute; see step results for detail.")
    output = {
        "verdict": "error" if any_step_failed else ("fail" if total_exceptions else "ok"),
        "statistics": [
            {"label": "Steps", "value": str(len(steps))},
            {"label": "Exception rows", "value": str(total_exceptions)},
        ],
        "verdict_text": f"{total_exceptions} exception row(s) across {len(steps)} step(s).",
        "viz": {"type": "table"},
        "stdout": "\n".join(part for part in stdout_parts if part),
        "step_results": step_results,
    }
    return output, summary, exceptions, total_exceptions, issues


def _run_engine(workspace: Workspace, item: dict) -> tuple[dict, pl.DataFrame | None, pl.DataFrame | None, int, list[str]]:
    engine = item["engine"]
    issues = list(item.get("semantic_warnings") or [])
    if engine == "analytics":
        frame = workspace.get_frame(item["table_refs"][0])
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
        frame = workspace.get_frame(item["table_refs"][0])
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
        output, summary, exceptions, exception_count, step_issues = _run_polars_steps(workspace, item)
        issues.extend(step_issues)
        return output, summary, exceptions, exception_count, list(dict.fromkeys(issues))
    null_columns = _all_null_columns(summary)
    if engine == "validation":
        # ``validation.summary_frame`` always includes the optional diagnostic
        # error column. A null value means the rule evaluated normally, so it
        # must not invalidate an otherwise substantive result frame.
        null_columns = [name for name in null_columns if name != "error"]
    if null_columns:
        issues.append(f"Result columns are entirely null: {', '.join(null_columns)}.")
    return output, summary, exceptions, exception_count, list(dict.fromkeys(issues))


def compute(workspace: Workspace, data_test_id: str) -> dict:
    """Compute an immutable result candidate without mutating the workspace."""
    item = _record(workspace, data_test_id)
    if item.get("status") == "draft" or not item.get("engine"):
        raise WorkspaceError(
            f"Data Test '{data_test_id}' has no executable specification yet."
        )
    run_id = CURRENT_RESULT_ID
    run_at = utcnow()
    execution_refs = _execution_table_refs(workspace, item)
    fingerprints = _dataset_fingerprints(workspace, execution_refs)
    source_sha1 = _sha1(
        {"engine": item["engine"], "table_refs": execution_refs, "spec": item["spec"]}
    )
    join_issues, diagnostics = _join_issues(workspace, execution_refs)
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
            or "failed to execute" in issue
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
        "step_results": output.get("step_results") or [],
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
        latest = {
            key: candidate[key]
            for key in (
                "id", "run_at", "status", "verdict", "exception_count", "semantic_valid",
                "dataset_fingerprints", "source_sha1", "result_sha1",
            )
        }
        item.pop("runs", None)
        item["last_run"] = latest
        for tile in fresh.tiles:
            if tile.get("data_test_id") == data_test_id:
                tile["result_ref"] = f"datatest:{data_test_id}:{candidate['id']}"
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
        _discard_superseded_results(committed.workspace, data_test_id)
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


def run_all_rcm_linked(workspace: Workspace) -> dict:
    """Run every Data Test linked to an RCM row, one at a time.

    Each test has its own durable result and guarded commit, so a bad or
    incomplete definition must not prevent the other RCM-linked tests from
    running.  The returned payload is intentionally a compact batch summary;
    callers can open individual results through the normal test endpoint.
    """
    test_ids = [
        str(item["id"])
        for item in workspace.data_tests
        if item.get("rcm_id")
    ]
    completed: list[dict] = []
    failed: list[dict] = []
    for data_test_id in test_ids:
        try:
            result = run(workspace, data_test_id)
            completed.append(
                {
                    "data_test_id": data_test_id,
                    "status": result["status"],
                    "exception_count": result["exception_count"],
                }
            )
        except Exception as error:
            failed.append({"data_test_id": data_test_id, "error": str(error)})

    # Keep the central RCM projection current even when a subset of tests
    # could not run.  Import here to avoid a module-level cycle.
    from . import rcm_execution

    rcm_execution.rollup(workspace)
    return {
        "total": len(test_ids),
        "completed": completed,
        "failed": failed,
    }


def load_result(workspace: Workspace, data_test_id: str, run_id: str) -> dict:
    item = _record(workspace, data_test_id)
    if not item.get("last_run") or run_id != item["last_run"].get("id"):
        raise WorkspaceError(f"Data Test result '{run_id}' is not the current result.")
    return _read_result(workspace, data_test_id, run_id)


def _read_result(workspace: Workspace, data_test_id: str, run_id: str) -> dict:
    """Read a result file, including a legacy result retained as evidence."""
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
        result = _read_result(workspace, data_test_id, run_id)
    except WorkspaceError:
        return None
    return {"item": result, "sha1": result["result_sha1"]}


def spec_as_python_code(spec: dict) -> str:
    """Flatten a Polars spec's steps into one script for ad-hoc dashboard tiles.

    Dashboard tiles run one code block; a multi-step Data Test has none, so
    this is a display convenience only, not a second executable definition.
    """
    steps = (spec or {}).get("steps") or []
    if not steps:
        return str((spec or {}).get("code") or "")
    return "\n\n".join(
        f"# Step: {step.get('label') or index + 1}\n{step.get('code') or ''}"
        for index, step in enumerate(steps)
    )


def list_payload(workspace: Workspace) -> list[dict]:
    return [
        {key: value for key, value in item.items() if key != "runs"}
        for item in sorted(workspace.data_tests, key=lambda item: item.get("updated") or "", reverse=True)
    ]
