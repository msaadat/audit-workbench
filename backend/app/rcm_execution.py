"""Deterministic RCM coverage, result roll-up, observations, and completion gates."""

from __future__ import annotations

import uuid

from . import data_tests, doc_tests
from .workspaces import OBSERVATION_DISPOSITIONS, Workspace, WorkspaceError


def _doc_tests(workspace: Workspace) -> list[dict]:
    return [doc_tests.load_test(workspace, item["id"]) for item in doc_tests.list_tests(workspace)]


def required_execution_kinds(method: str) -> set[str]:
    """Return the durable execution kinds required by one planned-test method.

    This mapping is orchestration policy, not model judgment.  Keeping it public
    lets the command runner expose the same requirements used by coverage and
    roll-up instead of asking the model to infer them from a title.
    """
    if method in {"data_analytics", "validation"}:
        return {"datatest"}
    if method in {"document_inspection", "inquiry", "evidence_unavailable"}:
        return {"doctest"}
    if method == "hybrid":
        return {"datatest", "doctest"}
    return set()


def execution_manifest(workspace: Workspace) -> list[dict]:
    """Build a bounded, model-safe plan-to-execution contract for the RCM.

    The manifest contains planning specifications and artifact metadata only;
    it never includes table rows or document text.  It is also useful to local
    semantic validators because it records whether an existing artifact has a
    durable result rather than merely whether a definition exists.
    """
    manifest = []
    for row in workspace.rcm:
        for planned in row.get("planned_tests") or []:
            artifacts = _artifacts(workspace, planned["id"])
            existing = []
            for artifact in artifacts:
                item = artifact["item"]
                if artifact["kind"] == "datatest":
                    has_result = bool(item.get("last_run"))
                else:
                    has_result = item.get("status") == "completed"
                existing.append({
                    "kind": artifact["kind"],
                    "id": artifact["id"],
                    "status": str(item.get("status") or ""),
                    "has_durable_result": has_result,
                    "executable": _artifact_executable(artifact),
                    **(
                        {"execution_issues": doc_tests.execution_issues(item)}
                        if artifact["kind"] == "doctest" else {}
                    ),
                    **(
                        {"engine": str(item.get("engine") or "")}
                        if artifact["kind"] == "datatest" else {}
                    ),
                })
            required = sorted(required_execution_kinds(planned.get("method") or ""))
            linked_kinds = {
                item["kind"] for item in existing if item.get("executable")
            }
            missing = sorted(set(required) - linked_kinds)
            if planned.get("method") == "validation" and not any(
                item["kind"] == "datatest" and item.get("engine") == "validation"
                for item in existing if item.get("executable")
            ):
                missing = ["validation_datatest"]
            manifest.append({
                "rcm_id": row["id"],
                "rcm_risk": str(row.get("risk") or ""),
                "risk_rating": str(row.get("risk_rating") or ""),
                "planned_test_id": planned["id"],
                "title": str(planned.get("title") or ""),
                "objective": str(planned.get("objective") or ""),
                "criteria": str(planned.get("criteria") or ""),
                "method": str(planned.get("method") or ""),
                "steps": [str(value) for value in planned.get("steps") or []],
                "expected_evidence": str(planned.get("expected_evidence") or ""),
                "sampling": dict(planned.get("sampling") or {}),
                "thresholds": dict(planned.get("thresholds") or {}),
                "required_execution": required,
                "required_engine": "validation" if planned.get("method") == "validation" else None,
                "existing_execution": existing,
                "missing_execution": missing,
            })
    return manifest


def _artifacts(workspace: Workspace, planned_test_id: str) -> list[dict]:
    artifacts = [
        {"kind": "datatest", "id": item["id"], "item": item}
        for item in workspace.data_tests
        if item.get("planned_test_id") == planned_test_id
    ]
    artifacts.extend(
        {"kind": "doctest", "id": item["id"], "item": item}
        for item in _doc_tests(workspace)
        if item.get("planned_test_id") == planned_test_id
    )
    return artifacts


def _artifact_executable(artifact: dict) -> bool:
    if artifact["kind"] != "doctest":
        return True
    item = artifact["item"]
    # Preserve auditor-completed historical work even when its older
    # definition predates the stronger runner preflight metadata.
    return (
        item.get("status") == "completed"
        or doc_tests.evidence_blocked(item)
        or not doc_tests.execution_issues(item)
    )


def coverage(workspace: Workspace) -> dict:
    rows_without_planned_tests = []
    planned_tests_without_execution = []
    invalid_execution_parents = []
    high_risks_without_executable_work = []
    completed_without_durable_result = []
    inconsistent_conclusions = []
    all_docs = _doc_tests(workspace)
    planned_index = {
        planned["id"]: row["id"]
        for row in workspace.rcm
        for planned in row.get("planned_tests") or []
    }

    for row in workspace.rcm:
        planned_tests = row.get("planned_tests") or []
        if not planned_tests:
            rows_without_planned_tests.append(row["id"])
        usable = False
        for planned in planned_tests:
            artifacts = _artifacts(workspace, planned["id"])
            executable_artifacts = [
                artifact for artifact in artifacts if _artifact_executable(artifact)
            ]
            linked_kinds = {artifact["kind"] for artifact in executable_artifacts}
            required = required_execution_kinds(planned.get("method") or "")
            missing = sorted(required - linked_kinds)
            if planned.get("method") == "validation" and not any(
                artifact["kind"] == "datatest"
                and artifact["item"].get("engine") == "validation"
                for artifact in executable_artifacts
            ):
                missing = ["validation datatest"]
            if missing:
                planned_tests_without_execution.append(
                    {
                        "rcm_id": row["id"],
                        "planned_test_id": planned["id"],
                        "missing": missing,
                    }
                )
            if planned.get("status") not in {"blocked", "not_ready"}:
                usable = True
            if planned.get("status", "").startswith("completed"):
                missing_result = any(
                    artifact["kind"] == "datatest" and not artifact["item"].get("last_run")
                    for artifact in executable_artifacts
                ) or any(
                    artifact["kind"] == "doctest"
                    and artifact["item"].get("status") != "completed"
                    for artifact in executable_artifacts
                )
                if not executable_artifacts or missing_result:
                    completed_without_durable_result.append(
                        {"rcm_id": row["id"], "planned_test_id": planned["id"]}
                    )
            if (
                planned.get("control_conclusion") == "effective"
                and (
                    int(planned.get("open_exception_count") or 0) > 0
                    or str(planned.get("scope_limitations") or "").strip()
                )
            ):
                inconsistent_conclusions.append(
                    {
                        "rcm_id": row["id"],
                        "planned_test_id": planned["id"],
                        "reason": "Effective conclusion conflicts with open exceptions or limitations.",
                    }
                )
        if row.get("risk_rating") in {"high", "critical"} and planned_tests and not usable:
            high_risks_without_executable_work.append(row["id"])

    for item in workspace.data_tests:
        if not item.get("rcm_id") and not item.get("planned_test_id"):
            # Exploratory Data Tests are durable analysis artifacts, not audit
            # execution, and therefore neither satisfy nor block RCM coverage.
            continue
        parent = planned_index.get(item.get("planned_test_id"))
        if not parent or parent != item.get("rcm_id"):
            invalid_execution_parents.append(
                {"kind": "datatest", "id": item.get("id"), "reason": "Invalid RCM/planned-test parent."}
            )
    for item in all_docs:
        if not item.get("planned_test_id"):
            invalid_execution_parents.append(
                {"kind": "doctest", "id": item.get("id"), "reason": "Unassigned execution artifact."}
            )
        elif planned_index.get(item.get("planned_test_id")) != item.get("rcm_id"):
            invalid_execution_parents.append(
                {"kind": "doctest", "id": item.get("id"), "reason": "Invalid RCM/planned-test parent."}
            )

    issues = (
        len(rows_without_planned_tests)
        + len(planned_tests_without_execution)
        + len(invalid_execution_parents)
        + len(high_risks_without_executable_work)
        + len(completed_without_durable_result)
        + len(inconsistent_conclusions)
    )
    return {
        "ok": issues == 0,
        "issue_count": issues,
        "rows_without_planned_tests": rows_without_planned_tests,
        "planned_tests_without_execution": planned_tests_without_execution,
        "invalid_execution_parents": invalid_execution_parents,
        "high_risks_without_executable_work": high_risks_without_executable_work,
        "completed_without_durable_result": completed_without_durable_result,
        "inconsistent_conclusions": inconsistent_conclusions,
    }


def _observation(
    workspace: Workspace,
    *,
    rcm_id: str,
    planned_test_id: str,
    execution_ref: str,
    exception_count: int,
    suggested_disposition: str,
    summary: str,
) -> dict:
    existing = next(
        (item for item in workspace.observations if item.get("execution_ref") == execution_ref),
        None,
    )
    if existing is None:
        existing = {
            "id": f"OBS-{uuid.uuid4().hex[:10].upper()}",
            "rcm_id": rcm_id,
            "planned_test_id": planned_test_id,
            "execution_ref": execution_ref,
            "exception_count": exception_count,
            "summary": summary,
            "suggested_disposition": suggested_disposition,
            "disposition": None,
            "auditor_note": "",
            "status": "open",
            "created": workspace._updated_now(),
            "updated": workspace._updated_now(),
        }
        workspace.observations.append(existing)
    else:
        existing.update(
            exception_count=exception_count,
            summary=summary,
            suggested_disposition=suggested_disposition,
            updated=workspace._updated_now(),
        )
    return existing


def _rollup_planned(workspace: Workspace, row: dict, planned: dict) -> dict:
    artifacts = [
        artifact
        for artifact in _artifacts(workspace, planned["id"])
        if _artifact_executable(artifact)
    ]
    required = sorted(required_execution_kinds(planned.get("method") or ""))
    states = []
    total_exceptions = 0
    open_exceptions = 0
    evidence_refs = []
    executed = 0
    blocked = False
    review_required = False
    artifact_payload = []
    for artifact in artifacts:
        item = artifact["item"]
        if artifact["kind"] == "datatest":
            last_run = item.get("last_run")
            state = item.get("status") or "ready"
            exceptions = int((last_run or {}).get("exception_count") or 0)
            if last_run:
                executed += 1
                run = data_tests.load_result(workspace, item["id"], last_run["id"])
                if exceptions or not run.get("semantic_valid"):
                    suggestion = (
                        "invalid_test_or_result"
                        if not run.get("semantic_valid")
                        else "screening_follow_up"
                        if any("Screening result" in issue for issue in run.get("semantic_issues") or [])
                        else "draft_finding_candidate"
                    )
                    observation = _observation(
                        workspace,
                        rcm_id=row["id"],
                        planned_test_id=planned["id"],
                        execution_ref=f"datatest:{item['id']}:{last_run['id']}",
                        exception_count=exceptions,
                        suggested_disposition=suggestion,
                        summary=run.get("verdict_text") or item["title"],
                    )
                    if observation.get("status") == "open":
                        open_exceptions += exceptions or 1
            review_required = review_required or state == "review_required"
        else:
            rollup = doc_tests.result_rollup(item)
            state = item.get("status") or "draft"
            exceptions = int(rollup["exceptions"] + rollup["mismatched"])
            if state == "completed":
                executed += 1
            blocked = blocked or state == "blocked"
            review_required = review_required or bool(rollup["manual_review"] or rollup["pending"])
            evidence_refs.extend(
                anchor
                for test_item in item.get("items") or []
                for anchor in test_item.get("evidence_refs") or []
            )
            if exceptions:
                observation = _observation(
                    workspace,
                    rcm_id=row["id"],
                    planned_test_id=planned["id"],
                    execution_ref=f"doctest:{item['id']}",
                    exception_count=exceptions,
                    suggested_disposition="draft_finding_candidate",
                    summary=f"{exceptions} document-test exception or mismatch result(s).",
                )
                if observation.get("status") == "open":
                    open_exceptions += exceptions
        states.append(state)
        total_exceptions += exceptions
        evidence_refs.extend(item.get("evidence_refs") or [])
        artifact_payload.append(
            {"kind": artifact["kind"], "id": item["id"], "status": state, "exception_count": exceptions}
        )

    linked_kinds = {artifact["kind"] for artifact in artifacts}
    missing = set(required) - linked_kinds
    if planned.get("method") == "validation" and not any(
        artifact["kind"] == "datatest" and artifact["item"].get("engine") == "validation"
        for artifact in artifacts
    ):
        missing = {"validation datatest"}
    if missing:
        status = "not_ready"
    elif blocked:
        status = "blocked"
    elif review_required:
        status = "review_required"
    elif artifacts and executed < len(artifacts):
        status = "in_progress" if executed else "ready"
    elif total_exceptions:
        status = "completed_with_exception"
    elif artifacts:
        status = "completed_no_exception"
    else:
        status = "not_ready"

    planned.update(
        status=status,
        exception_count=total_exceptions,
        open_exception_count=open_exceptions,
        evidence_refs=list(
            {anchor.get("id") or str(anchor): anchor for anchor in evidence_refs}.values()
        ),
        result_summary=(
            f"{len(artifacts)} linked execution artifact(s); {executed} executed; "
            f"{total_exceptions} exception result(s), {open_exceptions} open."
        ),
        updated=workspace._updated_now(),
    )
    return {
        "planned_test_id": planned["id"],
        "required_execution": required,
        "linked_execution": artifact_payload,
        "missing_execution": sorted(missing),
        "executed_count": executed,
        "exception_count": total_exceptions,
        "open_exception_count": open_exceptions,
        "evidence_count": len(planned["evidence_refs"]),
        "status": status,
        "result_summary": planned["result_summary"],
        "conclusion": planned.get("conclusion") or "",
        "scope_limitations": planned.get("scope_limitations") or "",
        "finding_refs": planned.get("finding_refs") or [],
    }


def rollup(workspace: Workspace) -> dict:
    rows = []
    for row in workspace.rcm:
        planned_rollups = [
            _rollup_planned(workspace, row, planned)
            for planned in row.get("planned_tests") or []
        ]
        conclusions = [
            planned.get("control_conclusion") or "no_conclusion"
            for planned in row.get("planned_tests") or []
        ]
        if "ineffective" in conclusions:
            control_conclusion = "ineffective"
        elif "partially_effective" in conclusions:
            control_conclusion = "partially_effective"
        elif conclusions and all(value == "effective" for value in conclusions):
            control_conclusion = "effective"
        elif conclusions and all(value == "not_applicable" for value in conclusions):
            control_conclusion = "not_applicable"
        else:
            control_conclusion = "no_conclusion"
        row_rollup = {
            "planned_tests": len(planned_rollups),
            "completed": sum(item["status"].startswith("completed") for item in planned_rollups),
            "blocked": sum(item["status"] == "blocked" for item in planned_rollups),
            "review_required": sum(item["status"] == "review_required" for item in planned_rollups),
            "exceptions": sum(item["exception_count"] for item in planned_rollups),
            "open_exceptions": sum(item["open_exception_count"] for item in planned_rollups),
            "control_conclusion": control_conclusion,
            "finding_count": len(row.get("finding_refs") or []),
            "review_status": row.get("review_status") or "draft",
            "planned_test_rollups": planned_rollups,
        }
        row["execution_rollup"] = row_rollup
        row["updated"] = workspace._updated_now()
        rows.append({"rcm_id": row["id"], **row_rollup})
    workspace.save()
    return {"rows": rows, "coverage": coverage(workspace)}


def disposition(
    workspace: Workspace, observation_id: str, value: str, auditor_note: str = ""
) -> dict:
    item = next(
        (row for row in workspace.observations if row.get("id") == observation_id),
        None,
    )
    if item is None:
        raise WorkspaceError(f"Observation '{observation_id}' not found.")
    if value not in OBSERVATION_DISPOSITIONS:
        raise WorkspaceError("Unknown observation disposition.")
    item["disposition"] = value
    item["auditor_note"] = str(auditor_note or "")
    item["status"] = "disposed"
    item["updated"] = workspace._updated_now()
    workspace.save()
    return item


def completion(workspace: Workspace) -> dict:
    rolled = rollup(workspace)
    cov = rolled["coverage"]
    open_observations = [
        item["id"] for item in workspace.observations if item.get("status") != "disposed"
    ]
    incomplete_outcomes = [
        {"rcm_id": row["id"], "planned_test_id": planned["id"], "status": planned.get("status")}
        for row in workspace.rcm
        for planned in row.get("planned_tests") or []
        if planned.get("status")
        not in {
            "blocked", "review_required", "completed_no_exception",
            "completed_with_exception", "not_applicable",
        }
    ]
    blank_conclusions = [
        {"rcm_id": row["id"], "planned_test_id": planned["id"]}
        for row in workspace.rcm
        for planned in row.get("planned_tests") or []
        if planned.get("status", "").startswith("completed")
        and not str(planned.get("conclusion") or "").strip()
    ]
    missing_planning_context = [
        field for field in ("objective", "scope")
        if not str((workspace.planning.get("context") or {}).get(field) or "").strip()
    ]
    blocked_without_plan = [
        {
            "rcm_id": row["id"], "planned_test_id": planned["id"],
            "missing": [
                label
                for label, present in (
                    ("scope limitation", bool(str(planned.get("scope_limitations") or "").strip())),
                    ("next action", bool(str(planned.get("next_action") or "").strip())),
                    ("evidence request", any(
                        request.get("planned_test_id") == planned["id"]
                        and request.get("status") in {"open", "requested"}
                        for request in workspace.evidence_requests
                    )),
                )
                if not present
            ],
        }
        for row in workspace.rcm
        for planned in row.get("planned_tests") or []
        if planned.get("status") == "blocked"
    ]
    blocked_without_plan = [item for item in blocked_without_plan if item["missing"]]
    rcm_without_conclusion = [
        row["id"] for row in workspace.rcm
        if (row.get("execution_rollup") or {}).get("control_conclusion") == "no_conclusion"
        and not all(
            str(planned.get("scope_limitations") or "").strip()
            for planned in row.get("planned_tests") or []
        )
    ]
    technical = bool(cov["invalid_execution_parents"] or cov["completed_without_durable_result"])
    open_items = bool(
        cov["issue_count"]
        or open_observations
        or incomplete_outcomes
        or blank_conclusions
        or missing_planning_context
        or blocked_without_plan
        or rcm_without_conclusion
        or any(
            planned.get("status") in {"blocked", "review_required"}
            for row in workspace.rcm
            for planned in row.get("planned_tests") or []
        )
    )
    status = "completed_with_issues" if technical else "completed_with_open_items" if open_items else "completed"
    return {
        "status": status,
        "coverage": cov,
        "open_observations": open_observations,
        "incomplete_outcomes": incomplete_outcomes,
        "blank_conclusions": blank_conclusions,
        "missing_planning_context": missing_planning_context,
        "blocked_without_plan": blocked_without_plan,
        "rcm_without_conclusion": rcm_without_conclusion,
    }
