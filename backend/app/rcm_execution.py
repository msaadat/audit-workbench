"""Deterministic RCM coverage, result roll-up, observations, and completion gates.

A test is one durable record with one source, so this module folds test results
straight into the RCM row that links them. There is no intermediate planned-test
layer and nothing to reconcile between a plan and its execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import data_tests, doc_tests
from .workspace_transactions import canonical_sha1, material_projection
from .workspaces import Workspace


_DURABLE_DOC_TEST_STATUSES = frozenset({
    "completed",
    "completed_no_exception",
    "completed_with_exception",
    "not_applicable",
})
# The auditor's control-level disposition is the authoritative conclusion for
# completion gates. The explanatory free-text note remains useful workpaper
# context, but it is not required to close a test.
CONCLUDED_CONTROL_CONCLUSIONS = frozenset({
    "effective",
    "partially_effective",
    "ineffective",
    "not_applicable",
})


@dataclass(frozen=True)
class DocumentTestIndex:
    """One request-local, fully hydrated view of the document-test worklist."""

    tests: tuple[dict, ...]
    by_rcm_id: dict[str, tuple[dict, ...]]
    summaries: tuple[dict, ...]


def document_test_index(workspace: Workspace) -> DocumentTestIndex:
    """Load document tests once and group them by their linked RCM row.

    The index deliberately lives only for the current calculation. Durable
    document-test mutations write their own files, so rebuilding it for each
    request keeps status surfaces current without cache invalidation state.
    """
    summaries = tuple(doc_tests.list_tests(workspace))
    tests = tuple(doc_tests.load_test(workspace, item["id"]) for item in summaries)
    grouped: dict[str, list[dict]] = {}
    for test in tests:
        rcm_id = str(test.get("rcm_id") or "")
        if rcm_id:
            grouped.setdefault(rcm_id, []).append(test)
    return DocumentTestIndex(
        tests=tests,
        by_rcm_id={rcm_id: tuple(items) for rcm_id, items in grouped.items()},
        summaries=summaries,
    )


def _tests(
    workspace: Workspace,
    rcm_id: str,
    document_tests: DocumentTestIndex | None = None,
) -> list[dict]:
    """Every durable test linked to one RCM row, in a stable order."""
    tests = [
        {"kind": "datatest", "id": item["id"], "item": item}
        for item in workspace.data_tests
        if item.get("rcm_id") == rcm_id
    ]
    tests.extend(
        {"kind": "doctest", "id": item["id"], "item": item}
        for item in (
            document_tests or document_test_index(workspace)
        ).by_rcm_id.get(rcm_id, ())
    )
    return sorted(tests, key=lambda test: (test["kind"], test["id"]))


def _specified(test: dict) -> bool:
    """Whether a test has an executable specification yet."""
    item = test["item"]
    if item.get("status") == "draft":
        return False
    return bool(item.get("engine")) if test["kind"] == "datatest" else bool(item.get("kind"))


def _executable(test: dict) -> bool:
    if test["kind"] != "doctest":
        return _specified(test)
    item = test["item"]
    # Preserve auditor-completed historical work even when its older definition
    # predates the stronger runner preflight metadata.
    return (
        item.get("status") in _DURABLE_DOC_TEST_STATUSES
        or doc_tests.evidence_blocked(item)
        or not doc_tests.execution_issues(item)
    )


def test_manifest(workspace: Workspace) -> list[dict]:
    """Build a bounded, model-safe view of every RCM row's linked tests.

    The manifest carries plan specifications and artifact metadata only; it never
    includes table rows or document text. It records whether a test has a durable
    result rather than merely whether a definition exists.
    """
    manifest = []
    document_tests = document_test_index(workspace)
    for row in workspace.rcm:
        for test in _tests(workspace, row["id"], document_tests):
            item = test["item"]
            if test["kind"] == "datatest":
                has_result = bool(item.get("last_run"))
                execution_refs = data_tests._execution_table_refs(workspace, item)
                current_source_sha1 = data_tests._sha1(
                    {
                        "engine": item.get("engine"),
                        "table_refs": execution_refs,
                        "spec": item.get("spec") or {},
                    }
                )
                current_fingerprints = data_tests._dataset_fingerprints(
                    workspace, execution_refs
                )
                result_stale = bool(
                    item.get("last_run")
                    and (
                        item["last_run"].get("source_sha1") != current_source_sha1
                        or item["last_run"].get("dataset_fingerprints") != current_fingerprints
                    )
                )
            else:
                current_items = bool(item.get("items")) and all(
                    doc_tests.item_execution_current(item, test_item)
                    for test_item in item.get("items") or []
                )
                has_result = (
                    current_items
                    if doc_tests.is_cycle_test(item)
                    else item.get("status") in _DURABLE_DOC_TEST_STATUSES
                    or current_items
                )
                result_stale = False
            manifest.append({
                "rcm_id": row["id"],
                "rcm_risk": str(row.get("risk") or ""),
                "risk_rating": str(row.get("risk_rating") or ""),
                "kind": test["kind"],
                "test_id": test["id"],
                "title": str(item.get("title") or ""),
                "objective": str(item.get("objective") or ""),
                "criteria": str(item.get("criteria") or ""),
                "steps": list(item.get("steps") or []),
                "test_kind": str(item.get("kind") or ""),
                "requirement_refs": list(item.get("requirement_refs") or []),
                "procedure_key": str(item.get("procedure_key") or ""),
                "definition": dict(item.get("definition") or {}),
                "coverage": dict(item.get("coverage") or {}),
                "assurance_scope": str(
                    (item.get("coverage") or {}).get("assurance_scope") or ""
                ),
                "status": str(item.get("status") or ""),
                "created_by": str(item.get("created_by") or ""),
                "specified": _specified(test),
                "has_durable_result": has_result,
                "executable": _executable(test),
                "workflow_parent_sha1": item.get("workflow_parent_sha1"),
                "result_stale": result_stale,
                **(
                    {"execution_issues": doc_tests.execution_issues(item)}
                    if test["kind"] == "doctest" else {}
                ),
                **(
                    {"engine": str(item.get("engine") or "")}
                    if test["kind"] == "datatest" else {}
                ),
            })
    return manifest


def coverage(
    workspace: Workspace,
    *,
    document_tests: DocumentTestIndex | None = None,
) -> dict:
    test_index = document_tests or document_test_index(workspace)
    rows_without_tests = []
    unspecified_tests = []
    invalid_test_parents = []
    high_risks_without_executable_work = []
    completed_without_durable_result = []
    inconsistent_conclusions = []
    known_rows = {row["id"] for row in workspace.rcm}

    for row in workspace.rcm:
        tests = _tests(workspace, row["id"], test_index)
        if not tests:
            rows_without_tests.append(row["id"])
        usable = False
        for test in tests:
            item = test["item"]
            if not _specified(test):
                unspecified_tests.append({"rcm_id": row["id"], "test_id": test["id"]})
            if item.get("status") not in {"blocked", "draft"}:
                usable = True
            if str(item.get("status") or "").startswith("completed"):
                missing_result = (
                    not item.get("last_run")
                    if test["kind"] == "datatest"
                    else item.get("status") != "completed"
                    and not str(item.get("status") or "").startswith("completed")
                )
                if missing_result:
                    completed_without_durable_result.append(
                        {"rcm_id": row["id"], "test_id": test["id"]}
                    )
            if (
                item.get("control_conclusion") == "effective"
                and (
                    int(item.get("open_exception_count") or 0) > 0
                    or str(item.get("scope_limitations") or "").strip()
                )
            ):
                inconsistent_conclusions.append(
                    {
                        "rcm_id": row["id"],
                        "test_id": test["id"],
                        "reason": "Effective conclusion conflicts with open exceptions or limitations.",
                    }
                )
        if row.get("risk_rating") in {"high", "critical"} and tests and not usable:
            high_risks_without_executable_work.append(row["id"])

    for item in [*workspace.data_tests, *test_index.tests]:
        rcm_id = item.get("rcm_id")
        # An unlinked test is exploration or standalone document work, not a
        # coverage defect. Only a link that does not resolve is.
        if rcm_id and rcm_id not in known_rows:
            invalid_test_parents.append(
                {"id": item.get("id"), "reason": "Linked RCM row does not exist."}
            )

    issues = (
        len(rows_without_tests)
        + len(unspecified_tests)
        + len(invalid_test_parents)
        + len(high_risks_without_executable_work)
        + len(completed_without_durable_result)
        + len(inconsistent_conclusions)
    )
    return {
        "ok": issues == 0,
        "issue_count": issues,
        "rows_without_tests": rows_without_tests,
        "unspecified_tests": unspecified_tests,
        "invalid_test_parents": invalid_test_parents,
        "high_risks_without_executable_work": high_risks_without_executable_work,
        "completed_without_durable_result": completed_without_durable_result,
        "inconsistent_conclusions": inconsistent_conclusions,
    }


def _observation(
    workspace: Workspace,
    *,
    rcm_id: str,
    test_id: str,
    execution_ref: str,
    exception_count: int,
    classification: str,
    summary: str,
) -> dict:
    existing = next(
        (item for item in workspace.observations if item.get("execution_ref") == execution_ref),
        None,
    )
    # Data Tests retain only their current durable result. Older workspaces may
    # still point an observation at the pre-current result ID, which otherwise
    # makes a read-only rollup project the same test result as a second
    # observation. Reuse that test's observation and refresh its reference.
    if execution_ref.startswith(f"datatest:{test_id}:"):
        matches = [
            item
            for item in workspace.observations
            if item.get("rcm_id") == rcm_id
            and item.get("test_id") == test_id
            and str(item.get("execution_ref") or "").startswith(
                f"datatest:{test_id}:"
            )
        ]
        if matches:
            existing = existing or matches[0]
            if len(matches) > 1:
                duplicate_ids = {id(item) for item in matches if item is not existing}
                workspace.observations[:] = [
                    item for item in workspace.observations if id(item) not in duplicate_ids
                ]
    if existing is None:
        existing = {
            "id": f"OBS-{uuid.uuid4().hex[:10].upper()}",
            "rcm_id": rcm_id,
            "test_id": test_id,
            "execution_ref": execution_ref,
            "exception_count": exception_count,
            "summary": summary,
            "classification": classification,
            "outcome": "exception" if exception_count else "needs_manual_check",
            "created": workspace._updated_now(),
            "updated": workspace._updated_now(),
        }
        workspace.observations.append(existing)
    else:
        existing.update(
            execution_ref=execution_ref,
            exception_count=exception_count,
            summary=summary,
            classification=classification,
            outcome="exception" if exception_count else "needs_manual_check",
            updated=workspace._updated_now(),
        )
    return existing


def _rollup_datatest(workspace: Workspace, row: dict, item: dict) -> tuple[str, int, int, int]:
    """Fold one Data Test's latest run into its own record."""
    last_run = item.get("last_run")
    exceptions = int((last_run or {}).get("exception_count") or 0)
    open_exceptions = 0
    executed = 0
    if last_run:
        executed = 1
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
                test_id=item["id"],
                execution_ref=f"datatest:{item['id']}:{last_run['id']}",
                exception_count=exceptions,
                classification=suggestion,
                summary=run.get("verdict_text") or item["title"],
            )
            if observation.get("outcome") == "exception":
                open_exceptions = exceptions
    status = str(item.get("status") or "draft")
    return status, exceptions, open_exceptions, executed


def _rollup_doctest(workspace: Workspace, row: dict, item: dict) -> tuple[str, int, int, int, list]:
    rollup = doc_tests.result_rollup(item)
    status = str(item.get("status") or "draft")
    # A deterministic mismatch is an evaluation result, not an auditor-owned
    # exception disposition.  Cycle tests keep those concepts separate all the
    # way through the rollup; downstream Phase 7 work can enrich the assurance
    # presentation without reviving the old double count.
    exceptions = int(
        rollup["exceptions"]
        if doc_tests.is_cycle_test(item)
        else rollup["exceptions"] + rollup["mismatched"]
    )
    open_exceptions = 0
    current_items = bool(item.get("items")) and all(
        doc_tests.item_execution_current(item, test_item)
        for test_item in item.get("items") or []
    )
    executed = int(
        current_items
        if doc_tests.is_cycle_test(item)
        else status in _DURABLE_DOC_TEST_STATUSES or current_items
    )
    if doc_tests.is_cycle_test(item) and current_items and not all(
        doc_tests.item_disposition_current(item, test_item)
        for test_item in item.get("items") or []
    ):
        status = "review_required"
    evidence_refs = [
        anchor
        for test_item in item.get("items") or []
        for anchor in test_item.get("evidence_refs") or []
    ]
    if exceptions:
        observation = _observation(
            workspace,
            rcm_id=row["id"],
            test_id=item["id"],
            execution_ref=f"doctest:{item['id']}",
            exception_count=exceptions,
            classification="draft_finding_candidate",
            summary=f"{exceptions} document-test exception or mismatch result(s).",
        )
        if observation.get("outcome") == "exception":
            open_exceptions = exceptions
    if status == "completed":
        status = (
            "completed_with_exception" if exceptions else "completed_no_exception"
        )
    return status, exceptions, open_exceptions, executed, evidence_refs


def _rollup_test(workspace: Workspace, row: dict, test: dict) -> dict:
    """Recompute one test's outcome from its own durable results."""
    item = test["item"]
    evidence_refs = list(item.get("evidence_refs") or [])
    if test["kind"] == "datatest":
        status, exceptions, open_exceptions, executed = _rollup_datatest(
            workspace, row, item
        )
    else:
        status, exceptions, open_exceptions, executed, anchors = _rollup_doctest(
            workspace, row, item
        )
        evidence_refs.extend(anchors)
    if not _specified(test):
        status = "draft"

    item.update(
        status=status,
        exception_count=exceptions,
        open_exception_count=open_exceptions,
        evidence_refs=list(
            {anchor.get("id") or str(anchor): anchor for anchor in evidence_refs}.values()
        ),
        result_summary=(
            f"{executed} run(s); {exceptions} exception result(s), "
            f"{open_exceptions} open."
        ),
        updated=workspace._updated_now(),
    )
    return {
        "test_id": test["id"],
        "kind": test["kind"],
        "title": str(item.get("title") or ""),
        "executed_count": executed,
        "exception_count": exceptions,
        "open_exception_count": open_exceptions,
        "evidence_count": len(item["evidence_refs"]),
        "status": status,
        "result_summary": item["result_summary"],
        "conclusion": item.get("conclusion") or "",
        "control_conclusion": item.get("control_conclusion") or "no_conclusion",
        "scope_limitations": item.get("scope_limitations") or "",
        "finding_refs": item.get("finding_refs") or [],
    }


def rollup(
    workspace: Workspace,
    *,
    persist: bool = True,
    rcm_ids: set[str] | None = None,
    document_tests: DocumentTestIndex | None = None,
) -> dict:
    """Recompute RCM outcomes, persisting only material changes.

    Status surfaces call this with ``persist=False`` so GET requests remain
    read-only.  Explicit roll-up commands still persist, but repeated calls do
    not advance the workspace revision when only volatile timestamps changed.
    """
    test_index = document_tests or document_test_index(workspace)
    before_sha1 = canonical_sha1(
        material_projection(
            {"rcm": workspace.rcm, "observations": workspace.observations}
        )
    )
    documents_to_write: dict[str, dict] = {}
    rows = []
    selected_rcm_ids = None if rcm_ids is None else set(rcm_ids)
    for row in workspace.rcm:
        if selected_rcm_ids is not None and row["id"] not in selected_rcm_ids:
            continue
        tests = _tests(workspace, row["id"], test_index)
        test_rollups = [_rollup_test(workspace, row, test) for test in tests]
        for test in tests:
            if test["kind"] == "doctest":
                documents_to_write[test["id"]] = test["item"]
        conclusions = [item["control_conclusion"] for item in test_rollups]
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
        # The row-level conclusion is this tally: how much of the linked work
        # completed, how much of it passed, and what it produced.
        row_rollup = {
            "tests": len(test_rollups),
            "completed": sum(item["status"].startswith("completed") for item in test_rollups),
            "passed": sum(item["status"] == "completed_no_exception" for item in test_rollups),
            "failed": sum(item["status"] == "completed_with_exception" for item in test_rollups),
            "blocked": sum(item["status"] == "blocked" for item in test_rollups),
            "review_required": sum(item["status"] == "review_required" for item in test_rollups),
            "draft": sum(item["status"] == "draft" for item in test_rollups),
            "exceptions": sum(item["exception_count"] for item in test_rollups),
            "open_exceptions": sum(item["open_exception_count"] for item in test_rollups),
            "control_conclusion": control_conclusion,
            "findings": len(row.get("finding_refs") or []),
            "review_status": row.get("review_status") or "draft",
            "test_rollups": test_rollups,
        }
        row["execution_rollup"] = row_rollup
        row["updated"] = workspace._updated_now()
        rows.append({"rcm_id": row["id"], **row_rollup})
    after_sha1 = canonical_sha1(
        material_projection(
            {"rcm": workspace.rcm, "observations": workspace.observations}
        )
    )
    if persist and after_sha1 != before_sha1:
        # Document Tests are separate files, so their recomputed outcome is
        # written back explicitly rather than riding the workspace save. The
        # single coordinated revision bump is the workspace save below.
        for test in documents_to_write.values():
            doc_tests.write_test(workspace, test)
        workspace.save()
    return {"rows": rows, "coverage": coverage(workspace, document_tests=test_index)}


def completion(
    workspace: Workspace,
    *,
    document_tests: DocumentTestIndex | None = None,
) -> dict:
    # Completion is used by dashboard/report GET paths. It must derive current
    # outcomes without turning a read into an optimistic-concurrency write.
    document_tests = document_tests or document_test_index(workspace)
    rolled = rollup(workspace, persist=False, document_tests=document_tests)
    cov = rolled["coverage"]
    linked = [
        (row, test)
        for row in workspace.rcm
        for test in _tests(workspace, row["id"], document_tests)
    ]
    incomplete_outcomes = [
        {"rcm_id": row["id"], "test_id": test["id"], "status": test["item"].get("status")}
        for row, test in linked
        if test["item"].get("status")
        not in {
            "blocked", "review_required", "completed", "completed_no_exception",
            "completed_with_exception", "not_applicable",
        }
    ]
    blank_conclusions = [
        {"rcm_id": row["id"], "test_id": test["id"]}
        for row, test in linked
        if str(test["item"].get("status") or "").startswith("completed")
        and test["item"].get("control_conclusion") not in CONCLUDED_CONTROL_CONCLUSIONS
    ]
    blocked_without_plan = [
        {
            "rcm_id": row["id"], "test_id": test["id"],
            "missing": [
                label
                for label, present in (
                    ("scope limitation", bool(str(test["item"].get("scope_limitations") or "").strip())),
                    ("next action", bool(str(test["item"].get("next_action") or "").strip())),
                    ("evidence request", any(
                        request.get("document_test_id") == test["id"]
                        and request.get("status") in {"open", "requested"}
                        for request in workspace.evidence_requests
                    )),
                )
                if not present
            ],
        }
        for row, test in linked
        if test["item"].get("status") == "blocked"
    ]
    blocked_without_plan = [item for item in blocked_without_plan if item["missing"]]
    rcm_without_conclusion = [
        row["id"] for row in workspace.rcm
        if (row.get("execution_rollup") or {}).get("control_conclusion") == "no_conclusion"
        and not all(
            str(test["item"].get("scope_limitations") or "").strip()
            for test in _tests(workspace, row["id"], document_tests)
        )
    ]
    pending_cycle_dispositions = [
        {
            "rcm_id": row["id"],
            "test_id": test["id"],
            "item_id": str(item.get("id") or "") or None,
        }
        for row, test in linked
        if test["kind"] == "doctest"
        and doc_tests.is_cycle_test(test["item"])
        for item in (test["item"].get("items") or [{}])
        if not item
        or not doc_tests.item_disposition_current(test["item"], item)
    ]
    technical = bool(cov["invalid_test_parents"] or cov["completed_without_durable_result"])
    open_items = bool(
        cov["issue_count"]
        or incomplete_outcomes
        or blank_conclusions
        or blocked_without_plan
        or rcm_without_conclusion
        or pending_cycle_dispositions
        or any(
            test["item"].get("status") in {"blocked", "review_required"}
            for _row, test in linked
        )
    )
    status = "completed_with_issues" if technical else "completed_with_open_items" if open_items else "completed"
    return {
        "status": status,
        "coverage": cov,
        "incomplete_outcomes": incomplete_outcomes,
        "blank_conclusions": blank_conclusions,
        "blocked_without_plan": blocked_without_plan,
        "rcm_without_conclusion": rcm_without_conclusion,
        "pending_cycle_dispositions": pending_cycle_dispositions,
    }
