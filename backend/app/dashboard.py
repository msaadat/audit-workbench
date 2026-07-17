"""Dashboard tile computation.

Tiles store specs, not data — this module re-executes each tile's spec
against the current frames and returns render-ready payloads. A broken tile
(deleted table, renamed column) degrades to an error card instead of failing
the whole dashboard.

Row caps depend on the visualization: charts get few points, tables a page.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import analytics, doc_tests, explore, llm, report, sandbox, validation
from .agent.prompts import parse_json_object
from .workspaces import Workspace

VIZ_ROW_CAPS = {"bar": 30, "pie": 12, "line": 500, "table": 50}
PHASE_TABS = {"planning": "planning", "fieldwork": "doc-tests", "report": "report"}
ALLOWED_ACTION_TABS = {
    "dashboard", "planning", "documents", "doc-tests", "data", "query",
    "validation", "analysis", "findings", "report",
}
AI_ADVICE_MAX = 3


def _cap_for(viz: dict) -> int:
    return VIZ_ROW_CAPS.get(str(viz.get("type") or "table"), 50)


def _python_frames(workspace: Workspace) -> dict:
    frames = {}
    for name in workspace.table_names():
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            continue
    return frames


def compute_payload(workspace: Workspace, item: dict) -> dict:
    """Recompute a stored spec (tile or saved analysis) into a render-ready
    payload. Both collections store specs, not data, and share this logic; a
    broken item degrades to an error card instead of raising."""
    payload = {
        key: item.get(key)
        for key in ("id", "title", "kind", "table", "note", "viz", "created", "source", "spec")
    }
    try:
        if item["kind"] == "python":
            code = (item.get("spec") or {}).get("code") or ""
            payload["code"] = code
            result, stdout = sandbox.run(code, _python_frames(workspace))
            payload["stdout"] = stdout or None
            payload["total_rows"] = result.height
            payload["frame"] = explore.frame_payload(result, _cap_for(payload["viz"]))
            payload["error"] = None
            return payload

        frame = workspace.get_frame(item["table"])
        if item["kind"] == "query":
            result, _ = explore.run_query_full(frame, item.get("spec") or {})
            payload["total_rows"] = result.height
            payload["frame"] = explore.frame_payload(result, _cap_for(payload["viz"]))
        elif item["kind"] == "pivot":
            # Legacy pivot tiles (rows/columns/values) render through the query
            # engine's cross-tab now that pivot.py is gone; new cross-tabs pin as
            # 'query' tiles with split_by.
            spec = item.get("spec") or {}
            columns = spec.get("columns") or []
            if isinstance(columns, str):
                columns = [columns]
            wide, _grand, _meta = explore.build_crosstab(
                frame,
                filters=spec.get("filters"),
                row_fields=spec.get("rows") or [],
                split_field=columns[0] if columns else None,
                value_specs=spec.get("values"),
                totals=spec.get("totals", True),
            )
            payload["total_rows"] = wide.height
            payload["frame"] = explore.frame_payload(wide, _cap_for(payload["viz"]))
        elif item["kind"] == "validation":
            spec = item.get("spec") or {}
            run = validation.run_rules(
                frame, spec.get("rules") or [], item["table"], resolve=workspace.get_frame
            )
            counts = run["counts"]
            payload["viz"] = {"type": "table"}
            # 'error' isn't a tile verdict — an errored rule reads as fail.
            payload["verdict"] = "fail" if run["verdict"] == "fail" else run["verdict"]
            payload["verdict_text"] = (
                f"{counts['passed']} passed · {counts['warned']} warned · "
                f"{counts['failed'] + counts['errored']} failed"
            )
            payload["stats"] = [
                {"label": "Rows checked", "value": f"{run['rows']:,}"},
                {"label": "Rules passed", "value": str(counts["passed"])},
                {"label": "Warnings", "value": str(counts["warned"])},
                {"label": "Failed", "value": str(counts["failed"] + counts["errored"])},
            ]
            summary = validation.summary_frame(run)
            payload["total_rows"] = summary.height
            payload["frame"] = explore.frame_payload(summary, _cap_for(payload["viz"]))
        else:
            spec = item.get("spec") or {}
            result = analytics.run_test(frame, spec.get("test"), spec.get("params"))
            # Analytics tiles use the test's own suggested visualization —
            # it tracks parameter changes (e.g. Benford digit count).
            payload["viz"] = result.viz or {"type": "table"}
            payload["verdict"] = result.verdict
            payload["verdict_text"] = result.verdict_text
            payload["stats"] = result.stats
            source = result.summary if result.summary is not None else result.detail
            if source is not None:
                payload["total_rows"] = source.height
                payload["frame"] = explore.frame_payload(source, _cap_for(payload["viz"]))
            else:
                payload["frame"] = None
        payload["error"] = None
    except Exception as error:
        payload["error"] = str(error)
    return payload


def tile_payload(workspace: Workspace, tile: dict) -> dict:
    return compute_payload(workspace, tile)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target(tab: str, **query: str) -> dict:
    return {"tab": tab, "query": {key: value for key, value in query.items() if value}}


def _action(action_id: str, title: str, reason: str, tab: str, *,
            priority: str = "medium", object_id: str | None = None) -> dict:
    query = {}
    if object_id:
        key = {"doc-tests": "test", "findings": "finding", "planning": "procedure"}.get(tab)
        if key:
            query[key] = object_id
    return {
        "id": action_id, "title": title, "reason": reason,
        "priority": priority, "source": "deterministic", "target": _target(tab, **query),
    }


def _phase(phase_id: str, state: str, complete: bool, summary: str,
           counts: dict, issues: list[str]) -> dict:
    return {
        "id": phase_id, "label": phase_id.title(), "state": state,
        "complete": complete, "summary": summary, "counts": counts,
        "issues": issues, "target": _target(PHASE_TABS[phase_id]),
    }


def _engagement_state(workspace: Workspace) -> dict:
    tests = doc_tests.list_tests(workspace)
    quality = report.quality_checks(workspace)
    current_report = report.hydrate(workspace)
    state_counts = {
        state: sum(int(test.get("state_counts", {}).get(state, 0)) for test in tests)
        for state in ("pending", "agent_checked", "confirmed", "exception", "manual_review")
    }
    broken_analyses = [
        item for item in workspace.analyses
        if item.get("kind") != "python" and item.get("table") not in workspace.table_names()
    ]
    latest_validation = [
        ruleset.get("runs", [])[-1] for ruleset in workspace.rulesets if ruleset.get("runs")
    ]
    failed_validation = [run for run in latest_validation if run.get("verdict") in {"warn", "fail"}]

    context = workspace.planning.get("context") or {}
    planning_started = bool(
        workspace.planning.get("apm_markdown") or workspace.rcm or workspace.work_program
        or any(str(value or "").strip() for key, value in context.items() if key != "interview_answers")
        or context.get("interview_answers")
    )
    planning_issues = []
    if not workspace.rcm:
        planning_issues.append("No risks or controls are recorded in the RCM.")
    if not workspace.work_program:
        planning_issues.append("No audit procedures are defined.")
    planning_complete = not planning_issues
    planning_state = "complete" if planning_complete else ("in_progress" if planning_started else "not_started")
    planning_summary = (
        "Planning includes an RCM and audit program."
        if planning_complete else f"{len(workspace.rcm)} RCM row(s) and {len(workspace.work_program)} procedure(s)."
    )

    incomplete_procedures = [
        item for item in workspace.work_program if not str(item.get("conclusion") or "").strip()
    ]
    incomplete_tests = [test for test in tests if test.get("status") != "completed"]
    fieldwork_started = bool(
        tests or workspace.analyses or workspace.rulesets or workspace.tiles
        or any(str(item.get("result_summary") or "").strip() for item in workspace.work_program)
    )
    fieldwork_issues = []
    if not workspace.work_program:
        fieldwork_issues.append("No audit procedures are available for fieldwork.")
    if incomplete_procedures:
        fieldwork_issues.append(f"{len(incomplete_procedures)} procedure(s) do not have a conclusion.")
    if incomplete_tests:
        fieldwork_issues.append(f"{len(incomplete_tests)} document test(s) are incomplete.")
    if state_counts["manual_review"]:
        fieldwork_issues.append(f"{state_counts['manual_review']} test item(s) require manual review.")
    unresolved_exceptions = [issue for issue in quality["issues"] if issue["code"] == "unresolved_exception"]
    fieldwork_issues.extend(issue["message"] for issue in unresolved_exceptions)
    if broken_analyses:
        fieldwork_issues.append(f"{len(broken_analyses)} saved analysis item(s) reference a missing table.")
    fieldwork_complete = bool(workspace.work_program) and not incomplete_procedures and not incomplete_tests
    fieldwork_attention = bool(
        state_counts["manual_review"] or unresolved_exceptions or broken_analyses or failed_validation
    )
    fieldwork_state = (
        "attention" if fieldwork_attention else "complete" if fieldwork_complete
        else "in_progress" if fieldwork_started or workspace.work_program else "not_started"
    )
    fieldwork_summary = (
        "All procedures have conclusions and all document tests are complete."
        if fieldwork_complete else f"{len(tests)} test(s), {state_counts['pending'] + state_counts['manual_review']} item(s) awaiting review."
    )

    report_started = bool(current_report.get("markdown") or workspace.findings)
    report_errors = [issue for issue in quality["issues"] if issue["severity"] == "error"]
    report_issues = []
    if not str(current_report.get("markdown") or "").strip():
        report_issues.append("The report has no Markdown content.")
    report_issues.extend(issue["message"] for issue in report_errors[:3])
    report_complete = bool(str(current_report.get("markdown") or "").strip()) and not report_errors
    report_state = (
        "attention" if report_errors else "complete" if report_complete
        else "in_progress" if report_started else "not_started"
    )
    report_summary = (
        "The report has content and no quality errors."
        if report_complete else f"{len(workspace.findings)} finding(s), {quality['counts']['error']} quality error(s)."
    )

    phases = [
        _phase("planning", planning_state, planning_complete, planning_summary,
               {"rcm_rows": len(workspace.rcm), "procedures": len(workspace.work_program)}, planning_issues),
        _phase("fieldwork", fieldwork_state, fieldwork_complete, fieldwork_summary,
               {"tests": len(tests), "pending_items": state_counts["pending"],
                "exceptions": state_counts["exception"]}, fieldwork_issues),
        _phase("report", report_state, report_complete, report_summary,
               {"findings": len(workspace.findings), "quality_errors": len(report_errors)},
               report_issues),
    ]

    return {
        "tests": tests,
        "quality": quality,
        "current_report": current_report,
        "state_counts": state_counts,
        "broken_analyses": broken_analyses,
        "planning_started": planning_started,
        "planning_complete": planning_complete,
        "planning_issues": planning_issues,
        "incomplete_procedures": incomplete_procedures,
        "incomplete_tests": incomplete_tests,
        "fieldwork_started": fieldwork_started,
        "fieldwork_complete": fieldwork_complete,
        "fieldwork_issues": fieldwork_issues,
        "unresolved_exceptions": unresolved_exceptions,
        "report_errors": report_errors,
        "phases": phases,
    }


def engagement_status_payload(workspace: Workspace) -> dict:
    return {"phases": _engagement_state(workspace)["phases"]}


def _engagement_snapshot(workspace: Workspace, tiles: list[dict]) -> dict:
    state = _engagement_state(workspace)
    tests = state["tests"]
    quality = state["quality"]
    current_report = state["current_report"]
    state_counts = state["state_counts"]
    planning_started = state["planning_started"]
    planning_complete = state["planning_complete"]
    planning_issues = state["planning_issues"]
    incomplete_procedures = state["incomplete_procedures"]
    incomplete_tests = state["incomplete_tests"]
    fieldwork_started = state["fieldwork_started"]
    fieldwork_complete = state["fieldwork_complete"]
    fieldwork_issues = state["fieldwork_issues"]
    unresolved_exceptions = state["unresolved_exceptions"]
    report_errors = state["report_errors"]

    table_errors: list[str] = []
    readable_tables = 0
    total_rows = 0
    for name in workspace.table_names():
        try:
            frame = workspace.get_frame(name)
            readable_tables += 1
            total_rows += frame.height
        except Exception as error:
            table_errors.append(f"{name}: {error}")

    broken_tiles = [tile for tile in tiles if tile.get("error")]
    failed_tiles = [
        tile for tile in tiles
        if not tile.get("error") and tile.get("verdict") in {"warn", "fail"}
    ]
    overview = {
        "tables": len(workspace.table_names()), "readable_tables": readable_tables,
        "table_errors": len(table_errors), "rows": total_rows,
        "documents": len(workspace.documents), "rcm_rows": len(workspace.rcm),
        "procedures": len(workspace.work_program), "document_tests": len(tests),
        "analyses": len(workspace.analyses), "rulesets": len(workspace.rulesets),
        "findings": len(workspace.findings),
        "pinned_tiles": len(workspace.tiles),
        "report_errors": quality["counts"]["error"],
        "report_warnings": quality["counts"]["warning"],
    }
    has_sources = bool(readable_tables or workspace.documents)

    actions = []
    if not has_sources:
        actions.append(_action("import-sources", "Import audit files", "Add a folder or individual files to begin the engagement.", "data", priority="high"))
    elif not planning_started:
        actions.append(_action("start-planning", "Start engagement planning", "Record the objective and scope, then build the RCM and audit program.", "planning", priority="high"))
    elif not planning_complete:
        actions.append(_action("complete-planning", "Complete engagement planning", planning_issues[0], "planning", priority="high"))
    if incomplete_tests:
        actions.append(_action("review-doc-test", "Continue document testing", f"{len(incomplete_tests)} test(s) still require work.", "doc-tests", priority="high", object_id=incomplete_tests[0]["id"]))
    elif incomplete_procedures:
        actions.append(_action("conclude-procedure", "Conclude fieldwork procedures", f"Record results and conclusions for {len(incomplete_procedures)} procedure(s).", "planning", priority="high", object_id=incomplete_procedures[0]["id"]))
    elif planning_complete and not fieldwork_started:
        actions.append(_action("start-fieldwork", "Start fieldwork", "Run document tests, validation rules, or data analyses against the planned procedures.", "doc-tests", priority="high"))
    if state_counts["manual_review"] or unresolved_exceptions:
        actions.append(_action("resolve-exceptions", "Resolve fieldwork exceptions", fieldwork_issues[-1], "doc-tests", priority="high"))
    if fieldwork_complete and not current_report.get("markdown"):
        actions.append(_action("generate-report", "Generate the audit report", "Fieldwork is ready to be summarized in an evidence-linked report.", "report", priority="medium"))
    if report_errors:
        actions.append(_action("fix-report-quality", "Resolve report quality errors", report_errors[0]["message"], "report", priority="high"))
    if broken_tiles:
        actions.append(_action("repair-dashboard", "Repair broken pinned items", f"{len(broken_tiles)} pinned item(s) could not be recomputed.", "dashboard", priority="medium"))
    elif failed_tiles:
        actions.append(_action("review-pinned-results", "Review flagged analytics", f"{len(failed_tiles)} pinned result(s) need attention.", "dashboard", priority="medium"))
    actions = actions[:5]

    attention = []
    for message in table_errors:
        attention.append({"id": f"table:{len(attention)}", "severity": "error", "title": "Unreadable table", "message": message, "target": _target("data")})
    for test in incomplete_tests:
        pending = int(test.get("state_counts", {}).get("pending", 0)) + int(test.get("state_counts", {}).get("manual_review", 0))
        if pending:
            attention.append({"id": f"doctest:{test['id']}", "severity": "warning", "title": test["title"], "message": f"{pending} item(s) await auditor review.", "target": _target("doc-tests", test=test["id"])})
    for issue in quality["issues"]:
        if issue["severity"] in {"error", "warning"} and issue["code"] != "report_empty":
            attention.append({"id": f"quality:{issue['code']}:{len(attention)}", "severity": issue["severity"], "title": "Report quality", "message": issue["message"], "target": _target("report")})
    for tile in broken_tiles:
        attention.append({"id": f"tile:{tile['id']}", "severity": "error", "title": tile["title"], "message": tile["error"], "target": _target("dashboard")})

    return {
        "overview": overview,
        "phases": state["phases"],
        "actions": actions,
        "attention": attention[:10],
    }


def _advice_snapshot(snapshot: dict) -> dict:
    """Focused dashboard facts for the configured model."""
    return {
        "overview": snapshot["overview"],
        "phases": [
            {key: phase[key] for key in ("id", "state", "complete", "summary", "counts", "issues")}
            for phase in snapshot["phases"]
        ],
        "deterministic_actions": [
            {key: action[key] for key in ("id", "title", "reason", "priority")}
            for action in snapshot["actions"]
        ],
        "attention": [
            {key: item[key] for key in ("severity", "title", "message")}
            for item in snapshot["attention"]
        ],
    }


def _snapshot_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cached_advice(workspace: Workspace, snapshot: dict) -> dict | None:
    if not workspace.dashboard_advice:
        return None
    result = dict(workspace.dashboard_advice)
    result["stale"] = result.get("input_hash") != _snapshot_hash(_advice_snapshot(snapshot))
    return result


def dashboard_payload(workspace: Workspace) -> dict:
    tiles = [compute_payload(workspace, tile) for tile in workspace.tiles]
    snapshot = _engagement_snapshot(workspace, tiles)
    return {**snapshot, "ai_advice": _cached_advice(workspace, snapshot), "tiles": tiles}


def generate_advice(workspace: Workspace) -> dict:
    tiles = [compute_payload(workspace, tile) for tile in workspace.tiles]
    snapshot = _engagement_snapshot(workspace, tiles)
    model_view = _advice_snapshot(snapshot)
    system = """[agent:dashboard_advice]
You advise an internal auditor on the next useful engagement actions. You receive only
artifact counts, workflow states, and deterministic issue summaries; no raw rows,
document text, evidence excerpts, or report body are present. Do not invent audit
results. Return one JSON object with a `suggestions` array of at most three items.
Each item must contain: title, reason, priority (high|medium|low), and tab. Allowed tabs:
planning, documents, doc-tests, data, query, validation, analysis, findings, report.
Prefer specific, non-duplicative advice that adds judgment beyond the deterministic actions."""
    message = llm.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": json.dumps(model_view, ensure_ascii=False)}],
        temperature=0.0, profile="agent",
    )
    parsed = parse_json_object(message.get("content") or "")
    suggestions = []
    for index, item in enumerate(parsed.get("suggestions") or []):
        if not isinstance(item, dict) or len(suggestions) >= AI_ADVICE_MAX:
            continue
        title = str(item.get("title") or "").strip()[:120]
        reason = str(item.get("reason") or "").strip()[:400]
        priority = str(item.get("priority") or "medium").lower()
        tab = str(item.get("tab") or "").lower()
        if not title or not reason or priority not in {"high", "medium", "low"} or tab not in ALLOWED_ACTION_TABS - {"dashboard"}:
            continue
        suggestions.append({
            "id": f"ai-{index + 1}", "title": title, "reason": reason,
            "priority": priority, "source": "ai", "target": _target(tab),
        })
    status = llm.agent_status()
    advice = {
        "items": suggestions, "generated_at": _now(),
        "provider": status.get("provider") or status.get("backend") or "",
        "model": status.get("model") or "", "input_hash": _snapshot_hash(model_view),
    }
    # The model call can be slow. Reload before writing so planning/report edits
    # made while it was in flight are never overwritten by this cached add-on.
    latest = Workspace(workspace.root)
    latest_tiles = [compute_payload(latest, tile) for tile in latest.tiles]
    latest_snapshot = _engagement_snapshot(latest, latest_tiles)
    stale = _snapshot_hash(_advice_snapshot(latest_snapshot)) != advice["input_hash"]
    latest.dashboard_advice = advice
    latest.save()
    workspace.dashboard_advice = advice
    return {**advice, "stale": stale}


def analysis_payload(workspace: Workspace, analysis: dict) -> dict:
    return compute_payload(workspace, analysis)


def analyses_payload(workspace: Workspace) -> dict:
    return {"analyses": [compute_payload(workspace, a) for a in workspace.analyses]}
