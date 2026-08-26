"""The engagement record: what was filed, what it cost, how many tries.

The record collapses every attempt at one capability into a single row, which
is the only way a real engagement reads as a pipeline rather than a thrash log
— the demo workspace files nine work products across twenty-three milestones.
These pin the collapse, the arithmetic behind the cost, and the two places the
projection deliberately refuses to state a number it cannot stand behind.
"""

import pytest

from app import engagement_record


class _Workspace:
    """Only the surface the record's counters touch."""

    def __init__(self, *, rcm=(), findings=(), documents=(), analyses=(), data_tests=()):
        self.rcm = list(rcm)
        self.findings = list(findings)
        self.documents = list(documents)
        self.analyses = list(analyses)
        self.data_tests = list(data_tests)


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
