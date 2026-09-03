"""Where an engagement stands, at two prices.

One question — what state is each phase in — asked by two surfaces that can
afford very different answers, so it is answered twice and both answers live
here.

**The derivation** (``engagement_status_payload``) is the authority. The console
rail asks the whole engagement question of one workspace and pays for it: a
phase tick costs around ninety milliseconds because ``rcm_execution.completion``
is defined at the *item* level, and proving a cycle-vouching test finished means
rebuilding every sampled record's closure out of the document analyses behind
it. That is the right price for the surface an auditor works on.

**The screen** (``progress``) is the cheap approximation. The engagement index
asks a smaller question of every workspace at once — four colours per card, no
figures — so it screens first with the fields the artifacts already store, and
pays the full price only where the screen cannot answer.

The screen is deliberately one-directional. Every signal it fires on restates a
clause of ``completion`` over stored fields, so a phase it calls ``attention``
is a phase the derivation calls ``attention`` too. It can never call a phase
complete: that is the verdict item-level truth exists to give, and the stored
fields cannot support it. Whatever it cannot settle escalates to the derivation
above and takes that answer verbatim, which is what stops the index ever reading
greener than the file it links to.

The derivation used to live in ``dashboard.py``, which the screen then imported.
Nothing about that module was the dashboard — see ``analysis_payloads`` for the
other half — and having the authority and its approximation in one file is what
makes the one-directional rule above checkable by reading.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from . import analysis_results, data_tests, doc_tests, rcm_execution, report
from .agent import capabilities as audit_capabilities
from .workspaces import TERMINAL_TEST_STATUSES, Workspace
from .text import counted, verb

#: The phases the index draws, in the order it draws them. "Data" is not here:
#: the card answers it from the table count the listing already carries.
PHASES = ("planning", "fieldwork", "report")

_CACHE_LIMIT = 512
_cache: "OrderedDict[tuple[str, int], dict[str, str]]" = OrderedDict()
_cache_guard = threading.Lock()


# --------------------------------------------------------------------------- #
# The derivation: item-level truth, at the console's price
# --------------------------------------------------------------------------- #
# Where each phase opens. These name the frontend's navigation destinations
# (see `useWorkspaceNavigation`), not the tab shell they used to name: the
# frontend resolves a target straight to a route and no longer translates a
# retired vocabulary on the way.
PHASE_DESTINATIONS = {"planning": "apm", "fieldwork": "doc-tests", "report": "report"}

# `TERMINAL_TEST_STATUSES` — the statuses a test rests at — is imported above
# rather than declared here. Every reader of a phase gate needs the same list,
# and a second copy of it beside a third reading of "has this test run" is how
# they drifted apart: `workspaces` declares it once, beside the vocabulary it is
# a subset of.


def _target(destination: str, **query: str) -> dict:
    return {
        "tab": destination,
        "query": {key: value for key, value in query.items() if value},
    }


def _phase(phase_id: str, state: str, complete: bool, summary: str,
           counts: dict, issues: list[str], sub: list[dict] | None = None) -> dict:
    return {
        "id": phase_id, "label": phase_id.title(), "state": state,
        "complete": complete, "summary": summary, "counts": counts,
        "issues": issues, "target": _target(PHASE_DESTINATIONS[phase_id]),
        "sub": sub or [],
    }


def _subphase(sub_id: str, label: str, started: bool, issues: list[str], target: dict) -> dict:
    complete = not issues
    state = "complete" if complete else ("in_progress" if started else "not_started")
    return {"id": sub_id, "label": label, "state": state, "complete": complete, "target": target}


def apm_started(workspace: Workspace) -> bool:
    """Whether anybody has begun the planning memorandum."""
    context = workspace.planning.get("context") or {}
    return bool(
        workspace.planning.get("apm_markdown")
        or any(str(value or "").strip() for key, value in context.items() if key != "interview_answers")
        or context.get("interview_answers")
    )


def apm_issues(workspace: Workspace) -> list[str]:
    """What holds the APM back, in the words the rail shows.

    Objective and scope are derived during planning; they are not setup
    requirements for a new workspace.

    Keep the APM badge tied to the same live readiness projection used by
    workflow scheduling.  A generated run is historical evidence; it
    must not keep the badge complete after the current APM is emptied or
    becomes structurally unusable.
    """
    readiness = audit_capabilities.REGISTRY.get(
        "planning.apm_ready"
    ).readiness(workspace, {}).payload()
    issues = list(readiness.get("reasons") or [])
    if readiness.get("state") == "blocked":
        issues.extend(
            f"Blocked by {dependency}."
            for dependency in readiness.get("blocking_on") or []
        )
    if not issues and readiness.get("state") != "satisfied":
        issues.append("The APM is not ready.")
    return issues


def rcm_issues(workspace: Workspace, rows_without_tests: list) -> list[str]:
    """What holds the RCM back, given the coverage pass over its rows."""
    issues = []
    if not workspace.rcm:
        issues.append("No risks or controls are recorded in the RCM.")
    if rows_without_tests:
        issues.append(
            f"{counted(len(rows_without_tests), 'RCM row')} "
            f"{verb(len(rows_without_tests), 'has', 'have')} no test."
        )
    return issues


def planning_state(started: bool, issues: list[str]) -> str:
    return "complete" if not issues else ("in_progress" if started else "not_started")


def _engagement_state(workspace: Workspace) -> dict:
    # Every reader below resolves Document Tests for itself, and cycle-vouching
    # materialization is the expensive step under all of them: without one
    # shared scope the index is rebuilt for the completion pass and again for
    # every finding the quality checks walk.
    with doc_tests.request_cache_scope():
        return _engagement_state_uncached(workspace)


def _engagement_state_uncached(workspace: Workspace) -> dict:
    document_tests = rcm_execution.document_test_index(workspace)
    tests = list(document_tests.summaries)
    completion = rcm_execution.completion(workspace, document_tests=document_tests)
    quality = report.quality_checks(workspace, document_tests=document_tests)
    current_report = report.hydrate(workspace)
    state_counts = {
        state: sum(int(test.get("state_counts", {}).get(state, 0)) for test in tests)
        for state in ("pending", "agent_checked", "confirmed", "exception", "manual_review")
    }
    broken_analyses = []

    analysis_memo = workspace.analysis_summary or {}
    eda_markdown = str(analysis_memo.get("markdown") or "").strip()
    eda_started = bool(workspace.analyses) or bool(eda_markdown)
    eda_issues = []
    if not workspace.analyses:
        eda_issues.append("No analyses have been run yet.")
    elif not eda_markdown:
        eda_issues.append("The analysis summary has not been written yet.")
    elif str(analysis_memo.get("basis_sha1") or "") != analysis_results.summary_basis_digest(workspace):
        eda_issues.append("The analysis summary is stale.")

    started_apm = apm_started(workspace)
    issues_apm = apm_issues(workspace)
    rows_without_tests = completion["coverage"]["rows_without_tests"]
    rcm_started = bool(workspace.rcm)
    issues_rcm = rcm_issues(workspace, rows_without_tests)

    planning_started = started_apm or rcm_started
    planning_issues = [*issues_apm, *issues_rcm]
    planning_complete = not planning_issues
    phase_planning_state = planning_state(planning_started, planning_issues)
    linked_rows = {row["id"] for row in workspace.rcm}
    linked_tests = [
        item
        for item in [*workspace.data_tests, *tests]
        if item.get("rcm_id") in linked_rows
    ]
    planning_summary = (
        "Planning context and RCM test coverage are complete."
        if planning_complete else (
            f"{counted(len(workspace.rcm), 'RCM row')} and {counted(len(linked_tests), 'test')}."
        )
    )

    incomplete_linked_tests = [
        item
        for item in linked_tests
        if item.get("status") not in TERMINAL_TEST_STATUSES
        or item.get("control_conclusion") not in rcm_execution.CONCLUDED_CONTROL_CONCLUSIONS
    ]
    incomplete_tests = [
        test for test in tests
        if test.get("status") not in {*TERMINAL_TEST_STATUSES, "blocked", "review_required"}
    ]
    # Two questions, and one variable used to answer both. A test that exists is
    # a test that was *written*: that settles whether there is a plan to speak
    # of, and it is the wrong answer to whether any of it has run — which is
    # what the completeness gate below is asking. `rcm_execution` owns the
    # second, and both registers are asked in the shape that can answer it,
    # which for a document test is the loaded record rather than its summary.
    fieldwork_planned = bool(tests or workspace.data_tests)
    fieldwork_ran = any(
        rcm_execution.data_test_has_durable_result(item)
        for item in workspace.data_tests
    ) or any(
        rcm_execution.doc_test_has_durable_result(test)
        for test in document_tests.tests
    )
    fieldwork_issues = [
        f"Coverage gate: {counted(completion['coverage']['issue_count'], 'issue')}."
        for _ in [0] if completion["coverage"]["issue_count"]
    ]
    if incomplete_linked_tests:
        fieldwork_issues.append(f"{counted(len(incomplete_linked_tests), 'test')} {verb(len(incomplete_linked_tests), 'has', 'have')} open execution or outcomes.")
    if incomplete_tests:
        fieldwork_issues.append(f"{counted(len(incomplete_tests), 'document test')} {verb(len(incomplete_tests), 'is', 'are')} incomplete.")
    if state_counts["manual_review"]:
        fieldwork_issues.append(f"{counted(state_counts['manual_review'], 'test item')} {verb(state_counts['manual_review'], 'requires', 'require')} manual review.")
    unresolved_exceptions = [issue for issue in quality["issues"] if issue["code"] == "unresolved_exception"]
    fieldwork_issues.extend(issue["message"] for issue in unresolved_exceptions)
    if broken_analyses:
        fieldwork_issues.append(f"{counted(len(broken_analyses), 'saved analysis item')} {verb(len(broken_analyses), 'references', 'reference')} a missing table.")
    # Every gate is vacuously satisfied before a single test exists, so the
    # completion status alone reads "completed" on an empty engagement. Requiring
    # that fieldwork actually happened is what stops the rail claiming every RCM
    # test passed on a workspace that has none — and, downstream, stops the
    # dashboard offering to write the report off the back of it. "Actually
    # happened" is a durable result and never a status: a register of tests the
    # runner refused to start satisfies this as readily as an empty one.
    fieldwork_complete = fieldwork_ran and completion["status"] == "completed"
    fieldwork_attention = bool(
        completion["status"] in {"completed_with_open_items", "completed_with_issues"}
        or state_counts["manual_review"] or unresolved_exceptions
    )
    fieldwork_state = (
        # Planned, not run: a matrix and a test programme are visible work, and
        # a phase holding both has started whether or not the runner has been.
        "not_started" if not fieldwork_planned and not workspace.rcm
        else "attention" if fieldwork_attention
        else "complete" if fieldwork_complete
        else "in_progress"
    )
    fieldwork_summary = (
        "All RCM tests passed deterministic execution and outcome gates."
        if fieldwork_complete
        else "No tests have been planned yet."
        if not fieldwork_planned else (
            f"{counted(len(workspace.data_tests), 'data test')}, "
            f"{counted(len(tests), 'document test')}, "
            f"{counted(sum(item.get('outcome') == 'exception' for item in workspace.observations), 'exception observation')}."
        )
    )

    report_started = bool(current_report.get("markdown") or workspace.findings)
    report_errors = [issue for issue in quality["issues"] if issue["severity"] == "error"]
    # Neither an open root cause nor a missing management response is a quality
    # error, so a file can pass every report gate with the follow-up on every
    # finding still outstanding. The rail says so beside the tick rather than
    # withholding it — moving the gate is a separate decision.
    findings_awaiting_followup = [
        item for item in workspace.findings
        if item.get("cause_pending")
        or not str(item.get("management_response") or "").strip()
    ]
    report_issues = []
    if not str(current_report.get("markdown") or "").strip():
        report_issues.append("The report has not been drafted yet.")
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
        if report_complete else f"{counted(len(workspace.findings), 'finding')}, {counted(quality['counts']['error'], 'quality error')}."
    )

    # The rail badges a phase against the tab that opens it, so "Fieldwork"
    # landed on Document tests and a data-test gap or an untested RCM row lit a
    # warning there. A section state answers the narrower question the badge
    # appears to be asking: is there document-test work outstanding?
    doc_test_issues: list[str] = []
    unresolved_doc_items = state_counts["manual_review"]
    doc_tests_needing_review = [
        test for test in tests
        if str(test.get("status") or "") in {"blocked", "review_required"}
    ]
    doc_tests_unconcluded = [
        test for test in tests
        if str(test.get("status") or "") in TERMINAL_TEST_STATUSES
        and test.get("control_conclusion") not in rcm_execution.CONCLUDED_CONTROL_CONCLUSIONS
    ]
    if unresolved_doc_items:
        doc_test_issues.append(
            f"{counted(unresolved_doc_items, 'test item')} "
            f"{verb(unresolved_doc_items, 'is', 'are')} unresolved."
        )
    if doc_tests_needing_review:
        doc_test_issues.append(
            f"{counted(len(doc_tests_needing_review), 'document test')} "
            f"{verb(len(doc_tests_needing_review), 'is', 'are')} blocked or awaiting review."
        )
    if doc_tests_unconcluded:
        doc_test_issues.append(
            f"{counted(len(doc_tests_unconcluded), 'document test')} "
            f"{verb(len(doc_tests_unconcluded), 'has', 'have')} no control conclusion."
        )
    doc_tests_concluded = [
        test for test in tests
        if str(test.get("status") or "") in TERMINAL_TEST_STATUSES
        and test.get("control_conclusion") in rcm_execution.CONCLUDED_CONTROL_CONCLUSIONS
    ]
    doc_tests_running = state_counts["pending"] + state_counts["agent_checked"]
    doc_test_state = (
        "not_started" if not tests
        else "attention" if doc_test_issues
        else "in_progress" if doc_tests_running
        else "complete"
    )
    # The same narrowing for the Data tests tab: what it badges is data-test
    # work, not the whole of fieldwork.
    data_test_issues: list[str] = []
    data_tests_needing_review = [
        item for item in workspace.data_tests
        if str(item.get("status") or "") == "review_required"
    ]
    data_tests_unconcluded = [
        item for item in workspace.data_tests
        if str(item.get("status") or "") in TERMINAL_TEST_STATUSES
        and item.get("control_conclusion") not in rcm_execution.CONCLUDED_CONTROL_CONCLUSIONS
    ]
    # A run that no longer describes its basis is evidence going out from under
    # a conclusion, which is exactly what this badge should catch early.
    stale_data_tests = [
        item for item in workspace.data_tests
        if data_tests.result_stale(workspace, item)
    ]
    if data_tests_needing_review:
        data_test_issues.append(
            f"{counted(len(data_tests_needing_review), 'data test')} "
            f"{verb(len(data_tests_needing_review), 'requires', 'require')} review."
        )
    if data_tests_unconcluded:
        data_test_issues.append(
            f"{counted(len(data_tests_unconcluded), 'data test')} "
            f"{verb(len(data_tests_unconcluded), 'has', 'have')} no control conclusion."
        )
    if stale_data_tests:
        data_test_issues.append(
            f"{counted(len(stale_data_tests), 'data test result')} "
            f"{verb(len(stale_data_tests), 'is', 'are')} stale."
        )
    # Deliberately *not* `data_test_has_durable_result`, which the fieldwork gate
    # above takes. This badge asks whether the tab has work left on it, and a
    # test retired at `not_applicable` has none and no result either — folding
    # the two would park a closed tab at "in progress" forever. The spelling is
    # safe here because a data test's status is derived (`project_status`) and
    # only ever rests at these two before a run; `blocked`, the status that broke
    # the gate above, is a document-test state that this list cannot see.
    data_tests_unrun = [
        item for item in workspace.data_tests
        if str(item.get("status") or "") in {"draft", "ready"}
    ]
    data_tests_concluded = [
        item for item in workspace.data_tests
        if str(item.get("status") or "") in TERMINAL_TEST_STATUSES
        and item.get("control_conclusion") in rcm_execution.CONCLUDED_CONTROL_CONCLUSIONS
    ]
    data_test_state = (
        "not_started" if not workspace.data_tests
        else "attention" if data_test_issues
        else "in_progress" if data_tests_unrun
        else "complete"
    )
    # `concluded` and `total` are what a rail chip needs to read "36/39" without
    # counting the same population a second time in the browser and reaching a
    # different answer than the section state it sits beside.
    sections = {
        "data-tests": {
            "id": "data-tests",
            "label": "Data tests",
            "state": data_test_state,
            "complete": data_test_state == "complete",
            "issues": data_test_issues,
            "counts": {
                "total": len(workspace.data_tests),
                "concluded": len(data_tests_concluded),
            },
            # The rail runs these itself through the scoped `run-all`, so it
            # needs the ids rather than a number it would have to go and resolve.
            # Both populations are deterministic work: no agent is involved.
            "unrun_test_ids": [str(item["id"]) for item in data_tests_unrun],
            "stale_test_ids": [str(item["id"]) for item in stale_data_tests],
        },
        "doc-tests": {
            "id": "doc-tests",
            "label": "Document tests",
            "state": doc_test_state,
            "complete": doc_test_state == "complete",
            "issues": doc_test_issues,
            "counts": {
                "total": len(tests),
                "concluded": len(doc_tests_concluded),
            },
        },
    }

    phases = [
        _phase("planning", phase_planning_state, planning_complete, planning_summary,
               {"rcm_rows": len(workspace.rcm), "tests": len(linked_tests)}, planning_issues,
               sub=[
                   _subphase("eda", "EDA", eda_started, eda_issues, _target("analysis")),
                   _subphase("apm", "APM", started_apm, issues_apm, _target("apm")),
                   _subphase("rcm", "RCM", rcm_started, issues_rcm, _target("rcm")),
               ]),
        _phase("fieldwork", fieldwork_state, fieldwork_complete, fieldwork_summary,
               {"data_tests": len(workspace.data_tests), "document_tests": len(tests),
                "exception_observations": sum(
                    item.get("outcome") == "exception"
                    for item in workspace.observations
                ),
                # The RCM bar's denominator, so the rail and the page it links to
                # never disagree about how much fieldwork is left.
                "tests_linked": len(linked_tests),
                "tests_concluded": len(linked_tests) - len(incomplete_linked_tests),
                "unreviewed_agent_conclusions": len(
                    completion["unreviewed_agent_conclusions"]
                )}, fieldwork_issues),
        _phase("report", report_state, report_complete, report_summary,
               {"findings": len(workspace.findings), "quality_errors": len(report_errors),
                "findings_awaiting_followup": len(findings_awaiting_followup)},
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
        # Two keys because they are two answers: whether a plan exists, and
        # whether any of it has run. One name for both is what let a phase gate
        # read a register's size as work performed.
        "fieldwork_planned": fieldwork_planned,
        "fieldwork_ran": fieldwork_ran,
        "fieldwork_complete": fieldwork_complete,
        "fieldwork_issues": fieldwork_issues,
        "unresolved_exceptions": unresolved_exceptions,
        "report_errors": report_errors,
        "completion": completion,
        "phases": phases,
        "sections": sections,
    }


def engagement_status_payload(workspace: Workspace) -> dict:
    state = _engagement_state(workspace)
    return {"phases": state["phases"], "sections": state["sections"]}



# --------------------------------------------------------------------------- #
# The screen: stored fields only, at the index's price
# --------------------------------------------------------------------------- #
def _stored_index(tests: list[dict]) -> rcm_execution.DocumentTestIndex:
    """A document-test index over stored fields only.

    ``coverage`` reads nothing from a test's materialized population — every
    field it touches is on the file — so it is exact against this index. It is
    the only pass that may be given one: ``completion`` reads items, and would
    answer from a projection that has gone out of date.
    """
    grouped: dict[str, list[dict]] = {}
    for test in tests:
        rcm_id = str(test.get("rcm_id") or "")
        if rcm_id:
            grouped.setdefault(rcm_id, []).append(test)
    return rcm_execution.DocumentTestIndex(
        tests=tuple(tests),
        by_rcm_id={rcm_id: tuple(items) for rcm_id, items in grouped.items()},
        summaries=tuple(tests),
    )


def _fieldwork_is_open(coverage: dict, linked: list[tuple[str, dict]]) -> bool:
    """Whether stored fields alone already prove fieldwork owes work.

    Each clause restates a clause of ``rcm_execution.completion`` over fields
    the artifacts carry, so a ``True`` here means ``completion`` reports open
    items as well. The converse does not hold, which is the point: this settles
    amber, never green.
    """
    if coverage["issue_count"]:
        return True
    for kind, item in linked:
        status = str(item.get("status") or "")
        if status not in TERMINAL_TEST_STATUSES:
            # Draft or running is an incomplete outcome; blocked and
            # review_required are open items in their own right.
            return True
        if kind != "datatest":
            # A document test's blank conclusion is an open item only where the
            # test is *eligible* to conclude, and eligibility is settled by
            # every item carrying a current disposition. That is exactly the
            # question stored fields cannot answer, so it goes to the
            # escalation rather than being guessed at here.
            continue
        if status.startswith("completed") and (
            item.get("control_conclusion")
            not in rcm_execution.CONCLUDED_CONTROL_CONCLUSIONS
            # A conclusion reached against evidence that has since moved is a
            # record of what somebody once thought, not a current conclusion.
            # Hydration stamps this, so it costs nothing to honour it.
            or item.get("control_conclusion_stale")
        ):
            return True
    return False


def _screen(workspace: Workspace) -> dict[str, str | None]:
    """Each phase's state where stored fields settle it, ``None`` where not."""
    stored = doc_tests.stored_tests(workspace)
    coverage = rcm_execution.coverage(
        workspace, document_tests=_stored_index(stored)
    )
    rows = {str(row["id"]) for row in workspace.rcm}
    linked: list[tuple[str, dict]] = [
        ("datatest", item)
        for item in workspace.data_tests
        if str(item.get("rcm_id") or "") in rows
    ]
    linked.extend(
        ("doctest", test)
        for test in stored
        if str(test.get("rcm_id") or "") in rows
    )

    # Planning is settled outright. Its gates are the APM readiness projection
    # and the coverage pass, and neither reads an item, so the screen is not
    # approximating the console here — it is running the same two checks.
    planning = planning_state(
        apm_started(workspace) or bool(workspace.rcm),
        [
            *apm_issues(workspace),
            *rcm_issues(workspace, coverage["rows_without_tests"]),
        ],
    )

    fieldwork: str | None
    if not stored and not workspace.data_tests and not workspace.rcm:
        fieldwork = "not_started"
    elif _fieldwork_is_open(coverage, linked):
        fieldwork = "attention"
    else:
        fieldwork = None

    # The report phase is settled only while nothing exists that could put an
    # error on it. Its quality checks walk every finding and read the
    # materialized items of every document test, so a file with either goes to
    # the escalation rather than being called green here.
    report_state: str | None = None
    drafted = str((report.hydrate(workspace) or {}).get("markdown") or "").strip()
    if not drafted and not workspace.findings and not stored:
        report_state = "not_started"

    return {"planning": planning, "fieldwork": fieldwork, "report": report_state}


def _confirm(workspace: Workspace) -> dict[str, str]:
    """The console's own answer, taken verbatim."""
    return {
        phase["id"]: phase["state"]
        for phase in engagement_status_payload(workspace)["phases"]
    }


def progress(workspace: Workspace) -> dict[str, str]:
    """Where this engagement stands, one state per phase.

    Memoized against the workspace revision, which advances on every write, so
    a listing that recomputes nothing is the normal case and a changed
    workspace is recomputed exactly once.
    """
    key = (str(workspace.root.resolve()), int(workspace.revision))
    with _cache_guard:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return dict(cached)

    screened = _screen(workspace)
    settled = (
        {phase: str(screened[phase]) for phase in PHASES}
        if all(screened[phase] is not None for phase in PHASES)
        else _confirm(workspace)
    )
    states = {phase: str(settled.get(phase) or "not_started") for phase in PHASES}

    with _cache_guard:
        _cache[key] = states
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return dict(states)


def clear_cache(root: Path | None = None) -> None:
    """Forget memoized states, for one workspace root or for all of them."""
    with _cache_guard:
        if root is None:
            _cache.clear()
            return
        prefix = str(Path(root).resolve())
        for key in [key for key in _cache if key[0] == prefix]:
            del _cache[key]
