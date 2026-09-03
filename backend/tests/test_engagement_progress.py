"""The index's phase strip: four colours, and what they are allowed to cost.

The strip restates the console rail's states on a surface that draws many
engagements at once, so these pin the two properties that make that safe. It
never disagrees with the rail, and it never disagrees in the direction that
would matter most — reading green over a file the rail calls amber.

The cheap screen is the whole reason the module exists, so the tests that
matter most are the ones that prove it settled a phase *without* falling back
to the derivation it is standing in for.
"""

import pytest

from app import data_tests, doc_tests, engagement_progress, rcm_execution
from app import engagement_progress, workspaces as workspace_module


@pytest.fixture(autouse=True)
def _forget_progress():
    engagement_progress.clear_cache()
    yield
    engagement_progress.clear_cache()


@pytest.fixture
def no_escalation(monkeypatch):
    """Make the expensive derivation an error, so a screened phase must screen."""

    def explode(_workspace):
        raise AssertionError("the screen should have settled every phase")

    monkeypatch.setattr(engagement_progress, "_confirm", explode)


def _console_states(workspace) -> dict:
    return {
        phase["id"]: phase["state"]
        for phase in engagement_progress.engagement_status_payload(workspace)["phases"]
    }


def _planned(workspace, **overrides):
    """One RCM row with one linked, runnable data test."""
    row = workspace.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})
    test = data_tests.create(workspace, {
        "title": "Required transaction identifiers",
        "objective": "Identify missing IDs.",
        "steps": [{"label": "Scan transaction amounts.", "instruction": "Scan transaction amounts."}],
        "engine": "analytics",
        "table_refs": ["transactions"],
        "rcm_id": row["id"],
        "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
        **overrides,
    })
    return row, test


def _completed(workspace):
    """The file the dashboard's own completion test builds, phase for phase."""
    workspace.update_planning({
        "apm_markdown": "# Planning",
        "context": {"objective": "Test revenue controls.", "scope": "Recorded transactions."},
    })
    _row, test = _planned(workspace)
    data_tests.run(workspace, test["id"])
    rcm_execution.rollup(workspace)
    data_tests.update(workspace, test["id"], {
        "conclusion": "No missing transaction identifiers were identified.",
        "control_conclusion": "effective",
    })
    workspace.report = {"markdown": "# Audit report\n\nThere are 0 findings."}
    workspace.save()
    return test


def test_an_empty_workspace_reads_as_nothing_started(workspace_with_data, no_escalation):
    assert engagement_progress.progress(workspace_with_data) == {
        "planning": "not_started",
        "fieldwork": "not_started",
        "report": "not_started",
    }


def test_a_planned_but_unrun_test_is_amber_without_the_derivation(
    workspace_with_data, no_escalation
):
    """A draft test is an incomplete outcome, which stored fields already say."""
    _planned(workspace_with_data)

    states = engagement_progress.progress(workspace_with_data)

    assert states["fieldwork"] == "attention"
    assert states["planning"] == "in_progress"


def test_a_conclusion_left_blank_is_amber_without_the_derivation(
    workspace_with_data, no_escalation
):
    _row, test = _planned(workspace_with_data)
    data_tests.run(workspace_with_data, test["id"])

    assert engagement_progress.progress(workspace_with_data)["fieldwork"] == "attention"


def test_an_untested_rcm_row_holds_planning_open_without_the_derivation(
    workspace_with_data, no_escalation
):
    """The coverage pass reads no materialized item, so the screen runs it."""
    workspace_with_data.update_planning({"apm_markdown": "# Planning"})
    workspace_with_data.add_rcm({"process": "Revenue", "risk": "Revenue may be misstated"})

    assert engagement_progress.progress(workspace_with_data)["planning"] == "in_progress"


def test_a_register_of_tests_is_not_fieldwork_having_run(workspace_with_data):
    """The completeness gate asks whether the work happened, and a test that
    exists has only been *written*.

    Read as a register length, one drafted test satisfied "fieldwork started"
    and the phase then turned on the completion status alone — which is
    vacuously "completed" until an item exists to fail it. The state still says
    the phase has begun, because a plan is visible work; only the gate is
    narrowed.
    """
    _planned(workspace_with_data)
    state = engagement_progress._engagement_state_uncached(workspace_with_data)

    assert state["fieldwork_planned"] is True
    assert state["fieldwork_ran"] is False
    assert state["fieldwork_complete"] is False
    assert _console_states(workspace_with_data)["fieldwork"] != "not_started"


def test_a_planned_test_still_reads_as_a_plan_that_exists(workspace_with_data):
    """The wording the gate used to share. Narrowing "started" to "ran" without
    splitting the two told an engagement holding a test programme that no tests
    had been planned yet."""
    _planned(workspace_with_data)
    payload = engagement_progress.engagement_status_payload(workspace_with_data)
    fieldwork = next(p for p in payload["phases"] if p["id"] == "fieldwork")

    assert "No tests have been planned yet." not in fieldwork["summary"]


def test_a_retired_data_test_is_not_work_the_tab_still_owes(workspace_with_data):
    """The Data tests badge asks what is left on the tab, which is why it is not
    folded onto `data_test_has_durable_result` like the fieldwork gate is: a
    test retired at `not_applicable` has no result and no work either, and the
    durable-result reading would park a closed tab at "in progress" forever."""
    _row, test = _planned(workspace_with_data)
    data_tests.update(workspace_with_data, test["id"], {
        "control_conclusion": "not_applicable",
        "conclusion": "Control retired for the period.",
    })
    state = engagement_progress._engagement_state_uncached(workspace_with_data)

    assert state["fieldwork_ran"] is False
    assert state["sections"]["data-tests"]["state"] == "complete"


def test_the_terminal_statuses_are_one_set_shared_with_the_status_vocabulary():
    """Two copies of it is how the three readings of "has this test run" drifted
    apart in the first place."""
    assert (
        engagement_progress.TERMINAL_TEST_STATUSES
        is workspace_module.TERMINAL_TEST_STATUSES
        is rcm_execution._DURABLE_DOC_TEST_STATUSES
    )
    assert engagement_progress.TERMINAL_TEST_STATUSES <= workspace_module.TEST_STATUSES


def test_a_finished_file_agrees_with_the_console(workspace_with_data):
    _completed(workspace_with_data)

    assert engagement_progress.progress(workspace_with_data) == _console_states(
        workspace_with_data
    )
    assert engagement_progress.progress(workspace_with_data)["fieldwork"] == "complete"


def test_the_screen_never_reads_greener_than_the_console(workspace_with_data):
    """The one direction that matters, over every state this file passes through.

    A green tick over an amber file is the failure this design exists to
    prevent: an index that under-reports trouble is worse than one that costs
    more to draw.
    """
    ranked = {"complete": 0, "not_started": 1, "in_progress": 2, "attention": 3}
    workspace_id = workspace_with_data.id
    stages = [
        lambda ws: ws.update_planning(
            {"apm_markdown": "# Planning", "context": {"objective": "Controls."}}
        ),
        lambda ws: _planned(ws),
        lambda ws: data_tests.run(ws, ws.data_tests[0]["id"]),
        lambda ws: rcm_execution.rollup(ws),
        lambda ws: data_tests.update(ws, ws.data_tests[0]["id"], {
            "conclusion": "Nothing was identified.", "control_conclusion": "effective",
        }),
    ]
    for advance in stages:
        # Each stage writes, so it starts from the revision the last one left.
        advance(workspace_module.load_workspace(workspace_id))
        engagement_progress.clear_cache()
        ws = workspace_module.load_workspace(workspace_id)
        screened = engagement_progress._screen(ws)
        console = _console_states(ws)
        for phase, state in screened.items():
            if state is None:
                continue
            assert ranked[state] >= ranked[console[phase]], (
                f"{phase}: screen said {state}, console said {console[phase]}"
            )


def test_a_second_read_of_an_unchanged_workspace_recomputes_nothing(
    workspace_with_data, monkeypatch
):
    _completed(workspace_with_data)
    engagement_progress.progress(workspace_with_data)

    monkeypatch.setattr(engagement_progress, "_screen", lambda _ws: pytest.fail("recomputed"))
    assert engagement_progress.progress(workspace_with_data)["fieldwork"] == "complete"


def test_a_write_invalidates_the_memo(workspace_with_data):
    _completed(workspace_with_data)
    assert engagement_progress.progress(workspace_with_data)["fieldwork"] == "complete"

    fresh = workspace_module.load_workspace(workspace_with_data.id)
    fresh.add_rcm({"process": "Procurement", "risk": "Orders may be unauthorised"})

    assert engagement_progress.progress(
        workspace_module.load_workspace(workspace_with_data.id)
    )["fieldwork"] == "attention"


def test_a_workspace_whose_status_cannot_be_derived_still_lists(
    workspace_with_data, monkeypatch
):
    monkeypatch.setattr(
        engagement_progress,
        "progress",
        lambda _ws: (_ for _ in ()).throw(RuntimeError("unreadable")),
    )

    listed = workspace_module.list_workspaces()

    assert [item["progress"] for item in listed] == [None]


def test_stored_tests_read_the_file_without_rebuilding_its_population(
    workspace_with_data, monkeypatch
):
    """The screen's cost rests on this: hydration, never materialization."""
    _planned(workspace_with_data)
    monkeypatch.setattr(
        doc_tests.cycle_vouching,
        "materialize_cycle_items",
        lambda *_args, **_kwargs: pytest.fail("materialized"),
    )

    assert doc_tests.stored_tests(workspace_with_data) == []
