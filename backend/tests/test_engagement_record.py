"""The engagement record: what was filed, what it cost, how many tries.

The record collapses every attempt at one capability into a single row, which
is the only way a real engagement reads as a pipeline rather than a thrash log
— the demo workspace files nine work products across twenty-three milestones.
These pin the collapse, the arithmetic behind the cost, and the two places the
projection deliberately refuses to state a number it cannot stand behind.
"""

import pytest

from app import engagement_record
from app.workspaces import TEST_STATUSES


class _Workspace:
    """Only the surface the record's counters and presence tests touch."""

    def __init__(self, *, rcm=(), findings=(), documents=(), analyses=(),
                 data_tests=(), tiles=(), apm="", planning=None):
        self.rcm = list(rcm)
        self.findings = list(findings)
        self.documents = list(documents)
        self.analyses = list(analyses)
        self.data_tests = list(data_tests)
        self.tiles = list(tiles)
        self.planning = dict(planning or {})
        if apm:
            self.planning["apm_markdown"] = apm


def _milestone(capability, at, *, status="completed", headline="Done", summary="Filed."):
    return {
        "id": f"{capability}:{at}",
        "capability": capability,
        "status": status,
        "headline": headline,
        "summary": summary,
        "metrics": [],
        "highlights": [],
        "artifact_refs": [],
        "created_at": at,
    }


def _run(run_id, started, milestones, *, status="completed", objective="Draft findings."):
    return {
        "id": run_id,
        "status": status,
        "started": started,
        "created": started,
        "chat_id": f"chat_{run_id}",
        "route": {"objective": objective},
        "milestones": milestones,
    }


@pytest.fixture
def stub_store(monkeypatch):
    """Drive the projection from in-memory runs rather than a workspace on disk."""

    def install(runs):
        monkeypatch.setattr(
            engagement_record.store, "list_runs",
            lambda workspace: [{"id": run["id"]} for run in runs],
        )
        by_id = {run["id"]: run for run in runs}
        monkeypatch.setattr(
            engagement_record.store, "load_run",
            lambda workspace, run_id: by_id[run_id],
        )
        monkeypatch.setattr(engagement_record.doc_tests, "list_tests", lambda workspace: [])
        # The forward half of the record reads the report and the conclusion
        # rollup; neither exists for an in-memory workspace.
        monkeypatch.setattr(engagement_record.report, "hydrate", lambda workspace: {"markdown": ""})
        monkeypatch.setattr(
            engagement_record.rcm_execution, "completion",
            # Derived from the rows rather than fixed, so a fixture's matrix and
            # its conclusions cannot disagree with each other.
            lambda workspace: {
                "unreviewed_agent_conclusions": [],
                "rcm_without_conclusion": [
                    row for row in workspace.rcm
                    if not str(row.get("conclusion") or "").strip()
                ],
            },
        )

    return install


def test_repeated_attempts_at_one_capability_collapse_to_one_entry(stub_store):
    """Six runs at the findings register are one work product, not six rows."""
    stub_store([
        _run("r1", "2026-08-14T11:00:00+00:00",
             [_milestone("findings.drafted", "2026-08-14T11:05:00+00:00")]),
        _run("r2", "2026-08-15T09:00:00+00:00",
             [_milestone("findings.drafted", "2026-08-15T09:04:00+00:00")]),
        _run("r3", "2026-08-15T12:00:00+00:00",
             [_milestone("findings.drafted", "2026-08-15T12:10:00+00:00",
                         headline="Finding drafts prepared")]),
    ])
    result = engagement_record.record(_Workspace(findings=[{}] * 35))

    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert len(entry["attempts"]) == 3
    # The latest attempt is the state the engagement is in.
    assert entry["headline"] == "Finding drafts prepared"
    assert entry["at"] == "2026-08-15T12:10:00+00:00"
    assert entry["first_at"] == "2026-08-14T11:05:00+00:00"
    assert result["totals"]["attempts"] == 3
    assert result["totals"]["work_products"] == 1


def test_cost_is_every_attempt_summed_not_only_the_one_that_stuck(stub_store):
    """Three tries at the RCM cost what three tries cost."""
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.rcm_ready", "2026-08-14T06:02:00+00:00")]),
        _run("r2", "2026-08-14T08:00:00+00:00",
             [_milestone("planning.rcm_ready", "2026-08-14T08:03:00+00:00")]),
    ])
    entry = engagement_record.record(_Workspace(rcm=[{}] * 27))["entries"][0]

    assert entry["elapsed_ms"] == (2 + 3) * 60_000
    assert entry["measured_attempts"] == 2


def test_a_stage_is_timed_from_the_previous_stage_in_its_own_run(stub_store):
    """Two stages settling together give the second one nothing, not the run."""
    stub_store([
        _run("r1", "2026-08-14T20:00:00+00:00", [
            _milestone("fieldwork.executed", "2026-08-14T20:05:00+00:00"),
            _milestone("results.rolled_up", "2026-08-14T20:05:00+00:00"),
        ]),
    ])
    entries = {e["capability"]: e for e in engagement_record.record(_Workspace())["entries"]}

    assert entries["fieldwork.executed"]["elapsed_ms"] == 5 * 60_000
    assert entries["results.rolled_up"]["elapsed_ms"] == 0


def test_a_cancelled_run_is_counted_as_an_attempt_but_never_timed(stub_store):
    """Its wall clock counts however long it sat waiting for a person."""
    stub_store([
        _run("r1", "2026-08-14T19:36:00+00:00",
             [_milestone("fieldwork.executed", "2026-08-14T19:36:20+00:00")],
             status="cancelled"),
        _run("r2", "2026-08-14T20:00:00+00:00",
             [_milestone("fieldwork.executed", "2026-08-14T20:01:00+00:00")]),
    ])
    entry = engagement_record.record(_Workspace())["entries"][0]

    assert len(entry["attempts"]) == 2
    assert entry["measured_attempts"] == 1
    assert entry["elapsed_ms"] == 60_000
    assert [a["run_status"] for a in entry["attempts"]] == ["cancelled", "completed"]


def test_an_entry_with_no_timed_attempt_states_no_duration_rather_than_zero(stub_store):
    stub_store([
        _run("r1", "2026-08-14T19:36:00+00:00",
             [_milestone("fieldwork.executed", "2026-08-14T19:36:20+00:00")],
             status="failed"),
    ])
    entry = engagement_record.record(_Workspace())["entries"][0]

    assert entry["elapsed_ms"] is None
    assert entry["measured_attempts"] == 0


def test_counts_come_from_the_workspace_not_the_last_milestones_delta(stub_store):
    """The final findings milestone reads "1" against a register holding 35."""
    stub_store([
        _run("r1", "2026-08-15T12:00:00+00:00", [
            dict(_milestone("findings.drafted", "2026-08-15T12:10:00+00:00"),
                 metrics=[{"label": "Drafts prepared", "value": 1}]),
        ]),
    ])
    entry = engagement_record.record(_Workspace(findings=[{}] * 35))["entries"][0]

    assert entry["filed"]["label"] == "Findings register"
    assert entry["filed"]["count"] == 35
    assert entry["filed"]["unit"] == "finding"
    assert entry["filed"]["destination"] == "findings"
    # The milestone's own delta is still carried, just not used as the size.
    assert entry["metrics"] == [{"label": "Drafts prepared", "value": 1}]


def test_a_work_product_with_no_meaningful_size_states_none(stub_store):
    """The memorandum is one document; "1" would be noise, not information."""
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.apm_ready", "2026-08-14T06:01:00+00:00")]),
    ])
    entry = engagement_record.record(_Workspace())["entries"][0]

    assert entry["filed"]["label"] == "Audit planning memorandum"
    assert entry["filed"]["count"] is None


def test_an_irregular_plural_is_declared_rather_than_guessed(stub_store):
    """Appending "s" to the unit produced "28 analysiss" on the demo engagement."""
    stub_store([
        _run("r1", "2026-08-13T19:00:00+00:00",
             [_milestone("analysis.executed", "2026-08-13T19:28:00+00:00")]),
    ])
    filed = engagement_record.record(_Workspace(analyses=[{}] * 28))["entries"][0]["filed"]

    assert (filed["unit"], filed["unit_plural"]) == ("analysis", "analyses")


def test_a_regular_unit_gets_its_plural_filled_in(stub_store):
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.rcm_ready", "2026-08-14T06:02:00+00:00")]),
    ])
    filed = engagement_record.record(_Workspace(rcm=[{}] * 27))["entries"][0]["filed"]

    assert (filed["unit"], filed["unit_plural"]) == ("row", "rows")


def test_fieldwork_does_not_borrow_the_document_test_registers_size(stub_store):
    """It schedules and rolls up tests another stage filed; it has no register.

    Sizing it by `document_tests` put the same number on two rows and credited
    fieldwork with a count it never produced.
    """
    stub_store([
        _run("r1", "2026-08-14T20:00:00+00:00", [
            _milestone("doc_tests.executed", "2026-08-14T20:05:00+00:00"),
            _milestone("fieldwork.executed", "2026-08-14T20:06:00+00:00"),
        ]),
    ])
    entries = {e["capability"]: e for e in engagement_record.record(_Workspace())["entries"]}

    assert entries["fieldwork.executed"]["filed"]["count"] is None
    assert entries["fieldwork.executed"]["filed"]["label"] == "Fieldwork results"
    assert entries["doc_tests.executed"]["filed"]["count"] == 0


def test_an_unmapped_capability_still_appears_with_no_filed_artifact(stub_store):
    """A new workflow stage shows its briefing rather than vanishing."""
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("something.new", "2026-08-14T06:01:00+00:00",
                         headline="A stage the record has never seen")]),
    ])
    entry = engagement_record.record(_Workspace())["entries"][0]

    assert entry["filed"] is None
    assert entry["headline"] == "A stage the record has never seen"


def test_entries_are_ordered_by_when_each_work_product_settled(stub_store):
    stub_store([
        _run("r1", "2026-08-13T19:00:00+00:00",
             [_milestone("analysis.executed", "2026-08-13T19:28:00+00:00")]),
        _run("r2", "2026-08-15T12:00:00+00:00",
             [_milestone("findings.drafted", "2026-08-15T12:10:00+00:00")]),
        _run("r3", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.apm_ready", "2026-08-14T06:01:00+00:00")]),
    ])
    order = [e["capability"] for e in engagement_record.record(_Workspace())["entries"]]

    assert order == ["analysis.executed", "planning.apm_ready", "findings.drafted"]


def test_runs_that_filed_nothing_are_counted_but_do_not_become_rows(stub_store):
    """Thirteen of the demo engagement's thirty-two runs committed no milestone."""
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.apm_ready", "2026-08-14T06:01:00+00:00")]),
        _run("r2", "2026-08-14T07:00:00+00:00", []),
        _run("r3", "2026-08-14T08:00:00+00:00", []),
    ])
    totals = engagement_record.record(_Workspace())["totals"]

    assert totals["runs"] == 3
    assert totals["runs_that_filed"] == 1
    assert totals["work_products"] == 1


def test_a_run_that_cannot_be_loaded_is_skipped_rather_than_failing_the_record(
    stub_store, monkeypatch,
):
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.apm_ready", "2026-08-14T06:01:00+00:00")]),
    ])
    monkeypatch.setattr(
        engagement_record.store, "list_runs",
        lambda workspace: [{"id": "r1"}, {"id": "missing"}],
    )
    result = engagement_record.record(_Workspace())

    assert result["totals"]["work_products"] == 1


def test_an_unparseable_timestamp_leaves_the_duration_unstated(stub_store):
    stub_store([
        _run("r1", "not a date",
             [_milestone("planning.apm_ready", "2026-08-14T06:01:00+00:00")]),
    ])
    entry = engagement_record.record(_Workspace())["entries"][0]

    assert entry["elapsed_ms"] is None


# --------------------------------------------------------------------------- #
# The forward half: what has not run, and what a finished stage left open
# --------------------------------------------------------------------------- #
def test_a_stage_whose_work_product_exists_is_never_drawn_as_pending(stub_store):
    """The report files no milestone on the demo engagement and still exists.

    Diffing the plan against the milestones would advertise "report not yet
    written" against 78,000 characters of report. Presence is the test.
    """
    stub_store([])
    workspace = _Workspace(findings=[{}] * 35)
    engagement_record.report.hydrate = lambda ws: {"markdown": "# Report\n\nreal content"}
    try:
        pending = {row["capability"] for row in engagement_record.record(workspace)["pending"]}
    finally:
        del engagement_record.report.hydrate
    assert "report.working_draft" not in pending


def test_a_stage_with_no_work_product_is_drawn_as_pending(stub_store):
    stub_store([])
    pending = engagement_record.record(_Workspace())["pending"]

    assert "planning.apm_ready" in {row["capability"] for row in pending}


def test_a_stage_that_never_narrates_is_never_drawn_as_pending(stub_store):
    """Nine stages file nothing by design; as phantom rows they would be
    permanent debt on a finished engagement."""
    stub_store([])
    pending = {row["capability"] for row in engagement_record.record(_Workspace())["pending"]}

    assert not (pending & set(engagement_record.UNNARRATED_CAPABILITIES))


def test_pending_stages_come_back_in_plan_order(stub_store):
    stub_store([])
    order = [row["capability"] for row in engagement_record.record(_Workspace())["pending"]]

    assert order.index("planning.apm_ready") < order.index("planning.rcm_ready")
    assert order.index("planning.rcm_ready") < order.index("tests.specified")


def test_a_stage_waiting_on_an_earlier_one_says_so_instead_of_offering_a_button(stub_store):
    stub_store([])
    rows = {r["capability"]: r for r in engagement_record.record(_Workspace())["pending"]}

    assert rows["planning.apm_ready"]["runnable"] is True
    assert rows["planning.apm_ready"]["blocked_reason"] == ""
    assert rows["planning.rcm_ready"]["runnable"] is False
    assert rows["planning.rcm_ready"]["blocked_reason"] == "Waits for the memorandum."


def test_a_stage_carries_what_to_ask_the_assistant_for(stub_store):
    stub_store([])
    rows = {r["capability"]: r for r in engagement_record.record(_Workspace())["pending"]}

    assert rows["planning.apm_ready"]["start"] == {
        "prompt": "Draft the APM.", "outcomes": ["planning.apm_ready"],
    }


def test_open_points_attach_to_the_stage_that_left_them(stub_store):
    stub_store([
        _run("r1", "2026-08-15T12:00:00+00:00",
             [_milestone("findings.drafted", "2026-08-15T12:10:00+00:00")]),
    ])
    workspace = _Workspace(findings=[{"cause_pending": True}] * 35)
    result = engagement_record.record(workspace)

    entry = result["entries"][0]
    assert [point["key"] for point in entry["open_points"]] == ["findings_followup"]
    assert "35 of 35 findings" in entry["open_points"][0]["message"]
    assert result["orphaned_points"] == []


def test_a_debt_whose_stage_never_filed_is_still_reported(stub_store):
    """The rows are draft but no `planning.rcm_ready` milestone survives."""
    stub_store([])
    result = engagement_record.record(_Workspace(rcm=[{"id": "R1", "review_status": "draft"}]))

    assert [point["key"] for point in result["orphaned_points"]] == ["draft_rcm"]


def test_review_outranks_unstarted_work_as_the_next_step(stub_store):
    """Auto mode runs the next stage by itself; only a person can read."""
    stub_store([
        _run("r1", "2026-08-15T12:00:00+00:00",
             [_milestone("findings.drafted", "2026-08-15T12:10:00+00:00")]),
    ])
    workspace = _Workspace(findings=[{"cause_pending": True}] * 35)
    result = engagement_record.record(workspace)

    assert result["next"]["kind"] == "open_point"
    assert result["next"]["key"] == "findings_followup"
    # ...and there was runnable work it deliberately did not choose.
    assert any(row["runnable"] for row in result["pending"])


def test_with_nothing_open_the_next_step_is_the_first_runnable_stage(stub_store):
    stub_store([])
    result = engagement_record.record(_Workspace())

    assert result["next"]["kind"] == "stage"
    assert result["next"]["capability"] == "planning.apm_ready"


def test_a_finished_and_reviewed_engagement_proposes_nothing(stub_store):
    stub_store([])
    workspace = _Workspace(
        apm="# APM",
        rcm=[{"id": "R1", "review_status": "reviewed", "conclusion": "effective"}],
        data_tests=[
            {"id": "T1", "rcm_id": "R1", "status": "completed_no_exception"},
        ],
        analyses=[{}], tiles=[{}],
        findings=[{"management_response": "Agreed.", "cause_pending": False}],
    )
    engagement_record.report.hydrate = lambda ws: {"markdown": "# Report"}
    try:
        result = engagement_record.record(workspace)
    finally:
        del engagement_record.report.hydrate

    assert result["pending"] == []
    assert result["open_points"] == []
    assert result["next"] is None


# --------------------------------------------------------------------------- #
# What a stage waits for, read from the graph rather than restated
# --------------------------------------------------------------------------- #
def _specified_not_run():
    """Tests drafted against a reviewed matrix, and not one of them run."""
    return _Workspace(
        apm="# APM",
        rcm=[{"id": "R1", "review_status": "reviewed"}],
        data_tests=[{"id": "T1", "rcm_id": "R1", "status": "ready"}],
        analyses=[{}],
    )


def test_running_the_tests_is_the_next_step_once_they_are_specified(stub_store):
    """The stage between specifying tests and drafting findings is running them.

    `fieldwork.executed` and `results.rolled_up` had no place on the tail at
    all, so the forward ledger jumped from "tests specified" to "draft
    findings" — offering to draft findings from exceptions that could not exist
    because no test had run.
    """
    stub_store([])
    rows = {r["capability"]: r for r in engagement_record.record(_specified_not_run())["pending"]}

    assert rows["fieldwork.executed"]["runnable"] is True
    assert rows["fieldwork.executed"]["blocked_reason"] == ""
    assert rows["results.rolled_up"]["blocked_reason"] == "Waits for the test results."


def test_findings_wait_for_the_conclusions_the_graph_names_not_the_tests(stub_store):
    """The record used to keep its own copy of the dependency graph.

    It said `findings.drafted` waited on `tests.specified`; the graph says it
    waits on `results.rolled_up`. With the tests specified and never run, the
    copy reported findings as runnable with nothing blocking it.
    """
    stub_store([])
    rows = {r["capability"]: r for r in engagement_record.record(_specified_not_run())["pending"]}

    assert rows["findings.drafted"]["runnable"] is False
    assert rows["findings.drafted"]["blocked_reason"] == "Waits for the conclusions."
    # The dashboard drifted the same way: it claimed the analysis library
    # unblocked it while the graph has it branching off the roll-up too.
    assert rows["dashboard.curated"]["runnable"] is False


def test_a_stage_with_several_prerequisites_names_all_the_missing_ones(stub_store):
    stub_store([])
    rows = {r["capability"]: r for r in engagement_record.record(_specified_not_run())["pending"]}

    # `report.working_draft` depends on the memorandum, the conclusions and the
    # findings; the memorandum is filed, so only the other two are owed.
    assert rows["report.working_draft"]["blocked_reason"] == (
        "Waits for the conclusions and the findings."
    )


def test_a_prerequisite_the_record_cannot_see_is_not_named_as_a_blocker(stub_store):
    """`fieldwork.executed` also depends on `tests.promoted_from_analysis`.

    That stage has no presence test here, so naming it would tell a reader to
    wait for something they can neither observe nor start.
    """
    stub_store([])
    rows = {r["capability"]: r for r in engagement_record.record(_specified_not_run())["pending"]}

    assert "promoted" not in rows["fieldwork.executed"]["blocked_reason"]
    assert rows["fieldwork.executed"]["blocked_reason"] == ""


def test_every_stage_that_can_block_another_has_a_readable_noun():
    """A missing noun degrades to "an earlier stage", which names nothing."""
    for capability in engagement_record._PHANTOM:
        assert capability in engagement_record._NOUNS, capability


def test_the_unrun_statuses_are_real_members_of_the_shared_vocabulary():
    """A renamed status would silently make every test look executed."""
    assert engagement_record._UNRUN_TEST_STATUSES - {""} <= TEST_STATUSES


def test_a_half_run_test_register_counts_as_fieldwork_having_run(stub_store):
    """One executed test is a result the stage produced; the rest is coverage.

    Drawing fieldwork as never started because some tests remain would invite a
    reader to redo work the filed row already reports.
    """
    stub_store([])
    workspace = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        data_tests=[
            {"id": "T1", "status": "completed_with_exception"},
            {"id": "T2", "status": "ready"},
        ],
    )
    pending = {row["capability"] for row in engagement_record.record(workspace)["pending"]}

    assert "fieldwork.executed" not in pending


def test_an_empty_matrix_is_not_a_matrix_whose_rows_are_concluded(stub_store):
    """`rcm_without_conclusion` is empty either way; only one means concluded."""
    stub_store([])
    workspace = _Workspace(apm="# APM", data_tests=[{"id": "T1", "status": "completed"}])
    pending = {row["capability"] for row in engagement_record.record(workspace)["pending"]}

    assert "results.rolled_up" in pending


def test_a_presence_test_that_raises_does_not_invent_absent_work(stub_store):
    stub_store([])

    def explode(workspace):
        raise RuntimeError("cannot read the report")

    engagement_record.report.hydrate = explode
    try:
        pending = {row["capability"] for row in engagement_record.record(_Workspace())["pending"]}
    finally:
        del engagement_record.report.hydrate

    assert "report.working_draft" not in pending


def test_every_stage_that_can_be_pending_has_a_readable_label():
    """A phantom row printed `dashboard.curated` at the reader.

    `_FILED` carried `report.drafted`, which is not a capability the pipeline
    has, and no entry for `dashboard.curated` at all — so both fell back to the
    raw id. The two registries have to agree on the vocabulary.
    """
    missing = [
        capability for capability in engagement_record._PHANTOM
        if capability not in engagement_record._FILED
    ]
    assert missing == []


def test_a_pending_row_never_shows_a_capability_id(stub_store):
    stub_store([])
    for row in engagement_record.record(_Workspace())["pending"]:
        label = row["filed"]["label"]
        assert label != row["capability"], label
        assert "." not in label, label


def test_the_record_reads_each_expensive_source_once(stub_store, monkeypatch):
    """The record asks the same two questions repeatedly; it may read once.

    `_pending` tests every stage's presence, `_blocked_by` re-tests each one's
    dependencies, and `_open_points` reads the roll-up again for the unread
    -conclusion debt. Nothing recorded that the answer was already in hand, so
    a real engagement rebuilt the document-test projection nine times and the
    roll-up five times to draw one screen — sixteen seconds, over an input that
    could not change while it was being read.
    """
    stub_store([])
    calls = {"tests": 0, "completion": 0}

    def count_tests(workspace):
        calls["tests"] += 1
        return [{"id": "D1", "status": "completed", "rcm_id": "R1"}]

    def count_completion(workspace):
        calls["completion"] += 1
        return {"rcm_without_conclusion": [], "unreviewed_agent_conclusions": ["R1"]}

    monkeypatch.setattr(engagement_record.doc_tests, "list_tests", count_tests)
    monkeypatch.setattr(engagement_record.rcm_execution, "completion", count_completion)

    engagement_record.record(_Workspace(rcm=[{"id": "R1"}], apm="# APM"))

    assert calls == {"tests": 1, "completion": 1}


def test_the_memo_does_not_outlive_the_call_that_opened_it(stub_store, monkeypatch):
    """Two record calls are two reads: the second must see what changed.

    The sources are files outside the manifest, so a cache kept between calls
    would answer for a workspace that has since been written to.
    """
    stub_store([])
    calls = {"completion": 0}

    def count_completion(workspace):
        calls["completion"] += 1
        return {"rcm_without_conclusion": [], "unreviewed_agent_conclusions": []}

    monkeypatch.setattr(engagement_record.rcm_execution, "completion", count_completion)
    workspace = _Workspace(rcm=[{"id": "R1"}], apm="# APM")

    engagement_record.record(workspace)
    engagement_record.record(workspace)

    assert calls["completion"] == 2
    assert engagement_record._MEMO.get() is None
