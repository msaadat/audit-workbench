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

from . import analytics, data_tests, debug_store, doc_tests, explore, llm, rcm_execution, report, sandbox, validation
from .agent.prompts import parse_json_object, validate_json_shape
from .workspaces import Workspace, WorkspaceConflict, sync_workspace

VIZ_ROW_CAPS = {"bar": 30, "pie": 12, "line": 500, "table": 50}
PHASE_TABS = {"planning": "planning", "fieldwork": "doc-tests", "report": "report"}
ALLOWED_ACTION_TABS = {
    "dashboard", "planning", "documents", "doc-tests", "data-tests", "data",
    "query", "findings", "report",
}
AI_ADVICE_MAX = 3
CURATED_TILE_MIN = 4
CURATED_TILE_MAX = 6
_TERMINAL_TEST_STATUSES = {
    "completed",
    "completed_no_exception",
    "completed_with_exception",
    "not_applicable",
}


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
        key = {"doc-tests": "test", "findings": "finding", "planning": "rcm"}.get(tab)
        if key:
            query[key] = object_id
    return {
        "id": action_id, "title": title, "reason": reason,
        "priority": priority, "source": "deterministic", "target": _target(tab, **query),
    }


def _phase(phase_id: str, state: str, complete: bool, summary: str,
           counts: dict, issues: list[str], sub: list[dict] | None = None) -> dict:
    return {
        "id": phase_id, "label": phase_id.title(), "state": state,
        "complete": complete, "summary": summary, "counts": counts,
        "issues": issues, "target": _target(PHASE_TABS[phase_id]),
        "sub": sub or [],
    }


def _subphase(sub_id: str, label: str, started: bool, issues: list[str], target: dict) -> dict:
    complete = not issues
    state = "complete" if complete else ("in_progress" if started else "not_started")
    return {"id": sub_id, "label": label, "state": state, "complete": complete, "target": target}


def _engagement_state(workspace: Workspace) -> dict:
    tests = doc_tests.list_tests(workspace)
    completion = rcm_execution.completion(workspace)
    quality = report.quality_checks(workspace)
    current_report = report.hydrate(workspace)
    state_counts = {
        state: sum(int(test.get("state_counts", {}).get(state, 0)) for test in tests)
        for state in ("pending", "agent_checked", "confirmed", "exception", "manual_review")
    }
    broken_analyses = []

    context = workspace.planning.get("context") or {}
    apm_started = bool(
        workspace.planning.get("apm_markdown")
        or any(str(value or "").strip() for key, value in context.items() if key != "interview_answers")
        or context.get("interview_answers")
    )
    apm_issues = [
        f"Planning context is missing {field}." for field in ("objective", "scope")
        if not str(context.get(field) or "").strip()
    ]
    rows_without_tests = completion["coverage"]["rows_without_tests"]
    rcm_started = bool(workspace.rcm)
    rcm_issues = []
    if not workspace.rcm:
        rcm_issues.append("No risks or controls are recorded in the RCM.")
    if rows_without_tests:
        rcm_issues.append(f"{len(rows_without_tests)} RCM row(s) have no test.")

    planning_started = apm_started or rcm_started
    planning_issues = [*apm_issues, *rcm_issues]
    planning_complete = not planning_issues
    planning_state = "complete" if planning_complete else ("in_progress" if planning_started else "not_started")
    linked_rows = {row["id"] for row in workspace.rcm}
    linked_tests = [
        item
        for item in [*workspace.data_tests, *tests]
        if item.get("rcm_id") in linked_rows
    ]
    planning_summary = (
        "Planning context and RCM test coverage are complete."
        if planning_complete else (
            f"{len(workspace.rcm)} RCM row(s) and {len(linked_tests)} test(s)."
        )
    )

    incomplete_linked_tests = [
        item
        for item in linked_tests
        if item.get("status") not in {
            "completed_no_exception", "completed_with_exception", "not_applicable",
        }
        or not str(item.get("conclusion") or item.get("scope_limitations") or "").strip()
    ]
    incomplete_tests = [
        test for test in tests
        if test.get("status") not in {*_TERMINAL_TEST_STATUSES, "blocked", "review_required"}
    ]
    fieldwork_started = bool(
        tests or workspace.data_tests
        or any(item.get("last_run") for item in workspace.data_tests)
    )
    fieldwork_issues = [
        f"Coverage gate: {completion['coverage']['issue_count']} issue(s)."
        for _ in [0] if completion["coverage"]["issue_count"]
    ]
    if incomplete_linked_tests:
        fieldwork_issues.append(f"{len(incomplete_linked_tests)} test(s) have open execution or outcomes.")
    if incomplete_tests:
        fieldwork_issues.append(f"{len(incomplete_tests)} document test(s) are incomplete.")
    if state_counts["manual_review"]:
        fieldwork_issues.append(f"{state_counts['manual_review']} test item(s) require manual review.")
    unresolved_exceptions = [issue for issue in quality["issues"] if issue["code"] == "unresolved_exception"]
    fieldwork_issues.extend(issue["message"] for issue in unresolved_exceptions)
    if broken_analyses:
        fieldwork_issues.append(f"{len(broken_analyses)} saved analysis item(s) reference a missing table.")
    fieldwork_complete = completion["status"] == "completed"
    fieldwork_attention = bool(
        completion["status"] in {"completed_with_open_items", "completed_with_issues"}
        or state_counts["manual_review"] or unresolved_exceptions
    )
    fieldwork_state = (
        "not_started" if not fieldwork_started and not workspace.rcm
        else "attention" if fieldwork_attention
        else "complete" if fieldwork_complete
        else "in_progress"
    )
    fieldwork_summary = (
        "All RCM tests passed deterministic execution and outcome gates."
        if fieldwork_complete else (
            f"{len(workspace.data_tests)} Data Test(s), {len(tests)} Document Test(s), "
            f"{sum(item.get('outcome') == 'exception' for item in workspace.observations)} exception observation(s)."
        )
    )

    report_started = bool(current_report.get("markdown") or workspace.findings)
    report_errors = [issue for issue in quality["issues"] if issue["severity"] == "error"]
    report_issues = []
    if not str(current_report.get("markdown") or "").strip():
        report_issues.append("The report has no Markdown content.")
    report_issues.extend(issue["message"] for issue in report_errors[:3])
    report_complete = (
        bool(str(current_report.get("markdown") or "").strip())
        and not report_errors and fieldwork_complete
    )
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
               {"rcm_rows": len(workspace.rcm), "tests": len(linked_tests)}, planning_issues,
               sub=[
                   _subphase("apm", "APM", apm_started, apm_issues, _target("planning")),
                   _subphase("rcm", "RCM", rcm_started, rcm_issues, _target("planning", view="rcm")),
               ]),
        _phase("fieldwork", fieldwork_state, fieldwork_complete, fieldwork_summary,
               {"data_tests": len(workspace.data_tests), "document_tests": len(tests),
                "exception_observations": sum(
                    item.get("outcome") == "exception"
                    for item in workspace.observations
                )}, fieldwork_issues),
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
        "linked_tests": linked_tests,
        "incomplete_linked_tests": incomplete_linked_tests,
        "incomplete_tests": incomplete_tests,
        "fieldwork_started": fieldwork_started,
        "fieldwork_complete": fieldwork_complete,
        "fieldwork_issues": fieldwork_issues,
        "unresolved_exceptions": unresolved_exceptions,
        "report_errors": report_errors,
        "completion": completion,
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
    incomplete_linked_tests = state["incomplete_linked_tests"]
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
        "tests": len(state["linked_tests"]),
        "data_tests": len(workspace.data_tests), "document_tests": len(tests),
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
        actions.append(_action("start-planning", "Start engagement planning", "Record the objective and scope, then build the RCM and its tests.", "planning", priority="high"))
    elif not planning_complete:
        actions.append(_action("complete-planning", "Complete engagement planning", planning_issues[0], "planning", priority="high"))
    if incomplete_tests:
        actions.append(_action("review-doc-test", "Continue document testing", f"{len(incomplete_tests)} test(s) still require work.", "doc-tests", priority="high", object_id=incomplete_tests[0]["id"]))
    elif incomplete_linked_tests:
        actions.append(_action("conclude-test", "Conclude RCM tests", f"Record results, limitations, and conclusions for {len(incomplete_linked_tests)} test(s).", "planning", priority="high", object_id=incomplete_linked_tests[0].get("rcm_id")))
    elif planning_complete and not fieldwork_started:
        actions.append(_action("start-fieldwork", "Start fieldwork", "Run the RCM-linked Data Tests and Document Tests.", "data-tests", priority="high"))
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


def _candidate_score(workspace: Workspace, item: dict, result: dict) -> tuple[int, list[str]]:
    """Score only semantically valid, reproducible RCM-linked Data Test results."""
    if not item.get("rcm_id"):
        return -100, ["exploratory result is not linked to an RCM row"]
    if not result.get("semantic_valid") or result.get("status") == "review_required":
        return -100, ["semantic validation did not pass"]
    row = next((value for value in workspace.rcm if value.get("id") == item.get("rcm_id")), {})
    score = {"critical": 6, "high": 5, "medium": 3, "low": 1}.get(
        str(row.get("risk_rating") or "medium"), 2
    )
    reasons = [f"{row.get('risk_rating') or 'medium'} RCM risk"]
    exceptions = int(result.get("exception_count") or 0)
    if exceptions:
        score += min(7, 3 + exceptions)
        reasons.append(f"{exceptions} exception(s)")
    viz = result.get("viz") or item.get("spec", {}).get("viz") or {}
    if viz.get("type") and viz.get("type") != "table":
        score += 2
        reasons.append("useful visualization")
    text = " ".join(
        str(value or "")
        for value in (item.get("title"), item.get("objective"), row.get("risk"), row.get("control"))
    ).casefold()
    management_terms = (
        "approval", "segregation", "three-way", "match", "missing", "invalid",
        "backdat", "cycle time", "timeliness", "vendor", "integrity",
    )
    if any(term in text for term in management_terms):
        score += 3
        reasons.append("management-relevant control signal")
    if item.get("spec", {}).get("test_id") in {"benford", "last_two_digits", "outliers"}:
        score -= 4
        reasons.append("screening-only analytic")
    return score, reasons


def curate_rcm_tiles(workspace: Workspace, *, run_id: str | None = None) -> dict:
    """Deterministically pin the strongest RCM-linked durable results.

    Existing user/agent tiles are preserved. A Data Test is never pinned twice,
    and an invalid or review-required execution is never promoted to the dashboard.

    Curation is a projection of the whole RCM, so the tile writes and the curation
    record are committed under a compare-and-swap guarded on the RCM's material
    hash: a concurrent RCM edit surfaces as a :class:`WorkspaceConflict` instead
    of pinning tiles selected against a stale matrix.
    """
    from .workspace_transactions import canonical_sha1, material_projection, mutate

    existing_ids = {item.get("data_test_id") for item in workspace.tiles if item.get("data_test_id")}
    candidates = []
    rejected = []
    for item in workspace.data_tests:
        if not item.get("last_run") or item.get("id") in existing_ids:
            continue
        result = data_tests.load_result(workspace, item["id"], item["last_run"]["id"])
        score, reasons = _candidate_score(workspace, item, result)
        candidate = {"item": item, "result": result, "score": score, "reasons": reasons}
        if score <= 2:
            rejected.append({"data_test_id": item["id"], "score": score, "reason": "; ".join(reasons)})
        else:
            candidates.append(candidate)
    candidates.sort(
        key=lambda value: (
            value["score"], int(value["result"].get("exception_count") or 0),
            value["item"].get("updated") or "",
        ),
        reverse=True,
    )
    available = max(0, CURATED_TILE_MAX - len(workspace.tiles))
    selected = candidates[:available]
    actionable = len(candidates)
    # The whole RCM is the curation basis; commit only if it is unchanged.
    expected_rcm_sha1 = canonical_sha1({"rcm": material_projection(workspace.rcm)})

    def commit(fresh: Workspace) -> dict:
        current_rcm_sha1 = canonical_sha1({"rcm": material_projection(fresh.rcm)})
        if current_rcm_sha1 != expected_rcm_sha1:
            raise WorkspaceConflict(fresh.revision, fresh.revision)
        created = []
        for candidate in selected:
            item, result = candidate["item"], candidate["result"]
            kind = {"polars": "python", "analytics": "analytics", "validation": "validation"}[item["engine"]]
            spec = dict(item["spec"])
            if kind == "analytics":
                spec = {"test": spec["test_id"], "params": spec.get("params") or {}}
            elif kind == "python":
                spec = {"code": data_tests.spec_as_python_code(item["spec"])}
            tile = fresh.add_tile(
                {
                    "id": f"rcm-{item['id'].casefold()}",
                    "kind": kind,
                    "table": item["table_refs"][0] if item.get("table_refs") else None,
                    "title": item["title"],
                    "note": "RCM-curated: " + "; ".join(candidate["reasons"]),
                    "spec": spec,
                    "viz": dict(result.get("viz") or {"type": "table"}),
                    "data_test_id": item["id"],
                    "rcm_id": item["rcm_id"],
                    "result_ref": f"datatest:{item['id']}:{result['id']}",
                    "agent_run_id": run_id,
                }
            )
            created.append(tile)
        reason = None
        if not created:
            reason = (
                "No semantically valid, non-duplicate RCM-linked result scored above the curation threshold."
                if not actionable else "Relevant results are already pinned or the dashboard is at its six-tile curation cap."
            )
        curation = {
            "completed_at": _now(),
            "run_id": run_id,
            "candidate_count": actionable,
            "created_count": len(created),
            "tile_ids": [item["id"] for item in created],
            "no_tile_reason": reason,
            "coverage_ok": bool(created or reason or not actionable),
            "below_recommended_minimum": bool(actionable >= CURATED_TILE_MIN and len(fresh.tiles) < CURATED_TILE_MIN),
            "rejected": rejected[:20],
            "workflow_parent_sha1": expected_rcm_sha1,
        }
        fresh.planning["dashboard_curation"] = curation
        fresh.save()
        return {"tiles": created, "curation": curation}

    result = mutate(workspace, commit)
    sync_workspace(workspace, result.workspace)
    return result.value


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
planning, documents, data-tests, doc-tests, data, query, findings, report.
Prefer specific, non-duplicative advice that adds judgment beyond the deterministic actions."""
    with debug_store.trace_context(
        workspace_id=workspace.id, workspace_root=str(workspace.root), stage="dashboard.advice",
        purpose="dashboard_advice", artifact_refs=["dashboard:advice"],
    ):
        message = llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": json.dumps(model_view, ensure_ascii=False)}],
            temperature=0.0, profile="agent",
        )
    parsed = parse_json_object(message.get("content") or "")
    validate_json_shape(parsed, object_arrays=("suggestions",))
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
    sync_workspace(workspace, latest)
    return {**advice, "stale": stale}


def analysis_payload(workspace: Workspace, analysis: dict) -> dict:
    payload = compute_payload(workspace, analysis)
    # Workflow execution retains a deliberately bounded result record on the
    # saved analysis.  Keep it distinct from the live, rerunnable payload
    # above: a live recomputation can fail after a successful execution (or
    # vice versa), and the UI needs to present the outcome the run recorded.
    last_result = analysis.get("last_result")
    if isinstance(last_result, dict):
        payload["last_result"] = dict(last_result)
    return payload


def analyses_payload(workspace: Workspace) -> dict:
    # Keep the collection endpoint consistent with the single-analysis
    # endpoint. The Analysis tab loads this collection, so omitting
    # ``last_result`` here made an executed workflow appear unrun after a
    # refresh even though its bounded result was safely persisted.
    return {"analyses": [analysis_payload(workspace, a) for a in workspace.analyses]}
