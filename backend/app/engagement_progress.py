"""Phase states for the engagement index, at the price the question is worth.

The console rail asks the whole engagement question of one workspace and pays
for it. A phase tick there costs around ninety milliseconds because
``rcm_execution.completion`` is defined at the *item* level: proving that a
cycle-vouching test finished means rebuilding every sampled record's closure
out of the document analyses behind it. That is the right price for the surface
an auditor works on.

The index asks a smaller question of every workspace at once — four colours per
card, no figures — so it screens first with the fields the artifacts already
store, and pays the full price only where the screen cannot answer.

The screen is deliberately one-directional. Every signal it fires on restates a
clause of ``completion`` over stored fields, so a phase it calls ``attention``
is a phase the console calls ``attention`` too. It can never call a phase
complete: that is the verdict item-level truth exists to give, and the stored
fields cannot support it. Whatever it cannot settle escalates to the console's
own derivation and takes that answer verbatim, which is what stops the index
ever reading greener than the file it links to.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from . import dashboard, doc_tests, rcm_execution, report
from .workspaces import Workspace

#: The phases the index draws, in the order it draws them. "Data" is not here:
#: the card answers it from the table count the listing already carries.
PHASES = ("planning", "fieldwork", "report")

_CACHE_LIMIT = 512
_cache: "OrderedDict[tuple[str, int], dict[str, str]]" = OrderedDict()
_cache_guard = threading.Lock()


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
        if status not in dashboard.TERMINAL_TEST_STATUSES:
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
    planning = dashboard.planning_state(
        dashboard.apm_started(workspace) or bool(workspace.rcm),
        [
            *dashboard.apm_issues(workspace),
            *dashboard.rcm_issues(workspace, coverage["rows_without_tests"]),
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
        for phase in dashboard.engagement_status_payload(workspace)["phases"]
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
