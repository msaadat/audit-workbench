"""The engagement record: what the engagement holds, and what it still owes.

The record is one row per work product, drawn from the audit graph rather than
from the run history. A stage appears whether or not a run ever filed it, which
is what keeps the ledger standing when the run folder is lost, when a stage
produces its artifact without narrating, and while a stage is still running.

Run history is layered on: cost, attempts, and the milestone's own narrative are
the things only a run knows. These pin the collapse of attempts into one block,
the arithmetic behind the cost, the presence rules that decide what is owed, and
the places the projection deliberately refuses to state something it cannot
stand behind.
"""

import json
from pathlib import Path

import pytest

from app import engagement, engagement_record, rcm_execution
from app.agent import routing
from app.agent.workflows import documents as documents_workflow
from app.workspaces import TEST_STATUSES


class _Workspace:
    """Only the surface the record's counters and presence tests touch."""

    def __init__(self, *, rcm=(), findings=(), documents=(), analyses=(),
                 data_tests=(), doc_tests=(), tiles=(), tables=(), apm="",
                 planning=None, root=None):
        # Only the document-analysis presence test reaches the disk, and it
        # reads a missing folder as "no analysis", so a workspace with no
        # documents never needs a real one.
        self.root = Path(root) if root else Path("/nonexistent")
        self.rcm = list(rcm)
        self.findings = list(findings)
        self.documents = list(documents)
        self.analyses = list(analyses)
        self.data_tests = list(data_tests)
        # Read through `doc_tests.list_tests`, which `stub_store` points back
        # here rather than at a workspace on disk.
        self.doc_tests = list(doc_tests)
        self.tiles = list(tiles)
        self._tables = list(tables)
        self.planning = dict(planning or {})
        if apm:
            self.planning["apm_markdown"] = apm

    def table_names(self):
        return list(self._tables)


def _ran(test, at="2026-08-14T20:05:00+00:00"):
    """A Data Test that really ran.

    The status beside a data test is a label; the durable result is the run
    recorded in `last_run`, and `rcm_execution.coverage` reports the pair
    without it as `completed_without_durable_result`. A fixture that sets only
    the status describes a defect, not an executed test.
    """
    return {**test, "last_run": {"id": "DTR-CURRENT", "run_at": at}}


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


def _rows(result):
    """The record's stages, keyed by capability."""
    return {stage["capability"]: stage for stage in result["stages"]}


def _owed(result):
    """The capabilities drawn as work the engagement still owes."""
    return {
        capability for capability, stage in _rows(result).items()
        if stage["start"] is not None
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
        # The register in both the shapes the real one has. `list_tests` returns
        # summaries, which carry no `items`; `load_test` returns the record. A
        # fixture that made them the same dict would hide the whole class of bug
        # where a presence test is handed the shape that cannot answer it.
        def _summaries(workspace):
            return [
                {key: value for key, value in test.items() if key != "items"}
                for test in getattr(workspace, "doc_tests", ())
            ]

        def _load(workspace, test_id):
            for test in getattr(workspace, "doc_tests", ()):
                if test["id"] == test_id:
                    return dict(test)
            raise KeyError(test_id)

        monkeypatch.setattr(engagement_record.doc_tests, "list_tests", _summaries)
        monkeypatch.setattr(engagement_record.doc_tests, "load_test", _load)
        # The record reads the report and the conclusion rollup; neither exists
        # for an in-memory workspace.
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


# --------------------------------------------------------------------------- #
# The spine stands without the runs
# --------------------------------------------------------------------------- #
def test_every_stage_is_drawn_whether_or_not_a_run_filed_it(stub_store):
    """The ledger is the audit graph, not the run history.

    A row used to exist because a milestone said so. That is why a workspace
    holding eleven work products rendered "Nothing filed yet" once its run
    folder was lost, and why a stage that produced its artifact without
    narrating appeared nowhere at all.
    """
    stub_store([])
    rows = _rows(engagement_record.record(_Workspace()))

    for capability in engagement_record._SPINE:
        assert capability in rows, capability


def test_a_work_product_survives_the_loss_of_the_run_that_filed_it(stub_store):
    """No runs at all, and the engagement still reports what it holds."""
    stub_store([])
    workspace = _Workspace(rcm=[{}] * 22, findings=[{}] * 30, analyses=[{}] * 24)
    result = engagement_record.record(workspace)
    rows = _rows(result)

    assert rows["planning.rcm_ready"]["held"] is True
    assert rows["planning.rcm_ready"]["filed"]["count"] == 22
    assert rows["planning.rcm_ready"]["history"] is None
    assert rows["findings.drafted"]["held"] is True
    assert result["totals"]["runs"] == 0
    # What the engagement holds, which is no longer what a run was seen to file.
    assert result["totals"]["work_products"] >= 3


def test_stages_come_back_in_plan_order(stub_store):
    stub_store([])
    order = [stage["capability"] for stage in engagement_record.record(_Workspace())["stages"]]

    assert order.index("planning.apm_ready") < order.index("planning.rcm_ready")
    assert order.index("planning.rcm_ready") < order.index("tests.specified")


def test_a_stage_outside_the_audit_plan_sits_beside_the_work_it_belongs_to(stub_store):
    """Document tests are their own workflow, so the plan gives them no place.

    Sorting them last put a register of executed document tests below the
    stages that have not run. They take the place after the stage declared
    before them instead, which is the work they are part of.
    """
    stub_store([])
    result = engagement_record.record(_Workspace())
    rows = _rows(result)
    order = [stage["capability"] for stage in result["stages"]]

    assert rows["doc_tests.executed"]["filed"]["label"] == "Document test results"
    assert order.index("tests.specified") < order.index("doc_tests.executed")
    assert order.index("doc_tests.executed") < order.index("fieldwork.executed")


# --------------------------------------------------------------------------- #
# The phase each row is drawn under
# --------------------------------------------------------------------------- #
def test_every_stage_names_a_phase_the_payload_declares(stub_store):
    """The view draws its sections from `phases` and files rows into them.

    A row naming a phase the payload does not list would vanish from the
    screen, so the two are checked against each other rather than assumed.
    """
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("working_papers.generated", "2026-08-14T06:01:00+00:00")]),
    ])
    result = engagement_record.record(_Workspace())
    declared = {phase["id"] for phase in result["phases"]}

    assert declared
    for stage in result["stages"]:
        assert stage["phase"] in declared, stage["capability"]


def test_no_stage_on_the_default_spine_falls_into_the_catch_all(stub_store):
    """`Further steps` is where an unmapped domain shows itself.

    Document test results sat there because `_PHASE_OF_DOMAIN` had no entry for
    `doc_tests` — a step of the fieldwork labelled as something left over.
    """
    stub_store([])
    result = engagement_record.record(_Workspace())

    unplaced = [
        stage["capability"] for stage in result["stages"]
        if stage["phase"] == engagement._UNGROUPED_PHASE
    ]
    assert not unplaced, f"unmapped capability domains: {unplaced}"
    assert engagement._UNGROUPED_PHASE not in {phase["id"] for phase in result["phases"]}


def test_the_default_spine_groups_into_the_five_audit_phases(stub_store):
    stub_store([])
    result = engagement_record.record(_Workspace())
    grouped: dict[str, list[str]] = {}
    for stage in result["stages"]:
        grouped.setdefault(stage["phase"], []).append(stage["capability"])

    assert grouped == {
        "sources": ["sources.imported", "analysis.executed"],
        "documents": ["documents.analysis_generated"],
        "planning": [
            "planning.apm_ready",
            # The shape of the process, between the memorandum it is read from
            # and the matrix that takes its process names from it.
            "planning.cycle_ready",
            "planning.rcm_ready",
            "tests.specified",
        ],
        "fieldwork": [
            "doc_tests.executed",
            "fieldwork.executed",
            "results.rolled_up",
            "findings.drafted",
        ],
        "writeup": ["report.working_draft", "audit.verified"],
    }


def test_the_document_test_register_is_part_of_the_fieldwork(stub_store):
    stub_store([])
    rows = _rows(engagement_record.record(_Workspace()))

    assert rows["doc_tests.executed"]["phase"] == "fieldwork"


def test_phases_come_back_in_plan_order_and_only_where_stages_sit(stub_store):
    stub_store([])
    result = engagement_record.record(_Workspace())
    order = [phase["id"] for phase in engagement.PLAN_PHASES]
    listed = [phase["id"] for phase in result["phases"]]

    assert listed == [item for item in order if item in set(listed)]
    for phase in result["phases"]:
        assert phase["title"] and phase["summary"]
        assert any(stage["phase"] == phase["id"] for stage in result["stages"])


def test_a_stage_outside_the_spine_is_still_given_its_phase(stub_store):
    """Working papers are registered and have no spine row, and still belong."""
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("working_papers.generated", "2026-08-14T06:01:00+00:00")]),
    ])
    rows = _rows(engagement_record.record(_Workspace()))

    assert rows["working_papers.generated"]["phase"] == "writeup"


# --------------------------------------------------------------------------- #
# Run history, layered on
# --------------------------------------------------------------------------- #
def test_repeated_attempts_at_one_capability_collapse_to_one_row(stub_store):
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
    history = _rows(result)["findings.drafted"]["history"]

    assert len(history["attempts"]) == 3
    # The latest attempt is the state the engagement is in.
    assert history["headline"] == "Finding drafts prepared"
    assert history["at"] == "2026-08-15T12:10:00+00:00"
    assert history["first_at"] == "2026-08-14T11:05:00+00:00"
    assert result["totals"]["attempts"] == 3


def test_cost_is_every_attempt_summed_not_only_the_one_that_stuck(stub_store):
    """Three tries at the RCM cost what three tries cost."""
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.rcm_ready", "2026-08-14T06:02:00+00:00")]),
        _run("r2", "2026-08-14T08:00:00+00:00",
             [_milestone("planning.rcm_ready", "2026-08-14T08:03:00+00:00")]),
    ])
    history = _rows(engagement_record.record(_Workspace(rcm=[{}] * 27)))[
        "planning.rcm_ready"]["history"]

    assert history["elapsed_ms"] == (2 + 3) * 60_000
    assert history["measured_attempts"] == 2


def test_a_stage_is_timed_from_the_previous_stage_in_its_own_run(stub_store):
    """Two stages settling together give the second one nothing, not the run."""
    stub_store([
        _run("r1", "2026-08-14T20:00:00+00:00", [
            _milestone("fieldwork.executed", "2026-08-14T20:05:00+00:00"),
            _milestone("results.rolled_up", "2026-08-14T20:05:00+00:00"),
        ]),
    ])
    rows = _rows(engagement_record.record(_Workspace()))

    assert rows["fieldwork.executed"]["history"]["elapsed_ms"] == 5 * 60_000
    assert rows["results.rolled_up"]["history"]["elapsed_ms"] == 0


def test_a_cancelled_run_is_counted_as_an_attempt_but_never_timed(stub_store):
    """Its wall clock counts however long it sat waiting for a person."""
    stub_store([
        _run("r1", "2026-08-14T19:36:00+00:00",
             [_milestone("fieldwork.executed", "2026-08-14T19:36:20+00:00")],
             status="cancelled"),
        _run("r2", "2026-08-14T20:00:00+00:00",
             [_milestone("fieldwork.executed", "2026-08-14T20:01:00+00:00")]),
    ])
    history = _rows(engagement_record.record(_Workspace()))["fieldwork.executed"]["history"]

    assert len(history["attempts"]) == 2
    assert history["measured_attempts"] == 1
    assert history["elapsed_ms"] == 60_000
    assert [a["run_status"] for a in history["attempts"]] == ["cancelled", "completed"]


def test_history_with_no_timed_attempt_states_no_duration_rather_than_zero(stub_store):
    stub_store([
        _run("r1", "2026-08-14T19:36:00+00:00",
             [_milestone("fieldwork.executed", "2026-08-14T19:36:20+00:00")],
             status="failed"),
    ])
    history = _rows(engagement_record.record(_Workspace()))["fieldwork.executed"]["history"]

    assert history["elapsed_ms"] is None
    assert history["measured_attempts"] == 0


def test_an_unparseable_timestamp_leaves_the_duration_unstated(stub_store):
    stub_store([
        _run("r1", "not a date",
             [_milestone("planning.apm_ready", "2026-08-14T06:01:00+00:00")]),
    ])
    history = _rows(engagement_record.record(_Workspace()))["planning.apm_ready"]["history"]

    assert history["elapsed_ms"] is None


def test_runs_that_filed_nothing_are_counted_but_leave_no_history(stub_store):
    """Thirteen of the demo engagement's thirty-two runs committed no milestone."""
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("planning.apm_ready", "2026-08-14T06:01:00+00:00")]),
        _run("r2", "2026-08-14T07:00:00+00:00", []),
        _run("r3", "2026-08-14T08:00:00+00:00", []),
    ])
    result = engagement_record.record(_Workspace(apm="# APM"))

    assert result["totals"]["runs"] == 3
    assert result["totals"]["runs_that_filed"] == 1
    assert sum(1 for stage in result["stages"] if stage["history"]) == 1


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
    result = engagement_record.record(_Workspace(apm="# APM"))

    assert _rows(result)["planning.apm_ready"]["history"] is not None


def test_a_stage_the_record_has_not_learned_yet_shows_its_own_briefing(stub_store):
    """A stage the workflows declare and the record has no entry for.

    `working_papers.generated` is exactly that today: a real capability of the
    audit graph with no row of its own here. It shows what it filed rather than
    vanishing because the record is behind the graph.
    """
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("working_papers.generated", "2026-08-14T06:01:00+00:00",
                         headline="A stage the record has never seen")]),
    ])
    row = _rows(engagement_record.record(_Workspace()))["working_papers.generated"]

    assert "working_papers.generated" not in engagement_record._SPINE
    assert row["filed"] is None
    assert row["history"]["headline"] == "A stage the record has never seen"
    # Present and empty, not absent. A reader of one row is entitled to the
    # shape of every other: the record view reads `stage.links.length` and
    # `stage.action` unguarded because the contract says they are always there.
    # Omitted, this row emptied the whole Record view the first time an
    # engagement filed working papers — which is a registered capability with
    # no spine row, so every complete engagement reaches it.
    assert row["links"] == []
    assert row["action"] == ""


def test_every_stage_row_carries_the_same_keys(stub_store):
    """The spine row is the shape; a row outside it must not be a subset.

    Two constructors build these rows and only one of them was complete, which
    is exactly the divergence a reader cannot see and a template cannot survive.
    """
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("working_papers.generated", "2026-08-14T06:01:00+00:00")]),
    ])
    rows = _rows(engagement_record.record(_Workspace()))
    spine = set(rows["sources.imported"])
    assert set(rows["working_papers.generated"]) == spine

    for capability, row in rows.items():
        for key in ("stats", "highlights", "links", "open_points"):
            assert isinstance(row[key], list), f"{capability}.{key}"
        assert isinstance(row["action"], str), f"{capability}.action"


def test_a_stage_the_workflows_no_longer_declare_is_not_drawn(stub_store):
    """Dashboard curation was retired from the audit graph.

    An engagement that ran it while it existed still carries the milestone, and
    a row for it would offer a step the product no longer has. The opposite of
    the case above, and told apart by whether any workflow still declares it.
    """
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [_milestone("dashboard.curated", "2026-08-14T06:01:00+00:00")]),
    ])
    result = engagement_record.record(_Workspace(tiles=[{}] * 6))

    assert "dashboard.curated" not in _rows(result)
    assert "dashboard.curated" not in engagement_record._registered()
    # The run is still counted; it is the row that is gone, not the history.
    assert result["totals"]["runs"] == 1


# --------------------------------------------------------------------------- #
# What the engagement holds, and what that is not
# --------------------------------------------------------------------------- #
def test_counts_come_from_the_workspace_not_the_last_milestones_delta(stub_store):
    """The final findings milestone reads "1" against a register holding 35."""
    stub_store([
        _run("r1", "2026-08-15T12:00:00+00:00", [
            dict(_milestone("findings.drafted", "2026-08-15T12:10:00+00:00"),
                 metrics=[{"label": "Drafts prepared", "value": 1}]),
        ]),
    ])
    row = _rows(engagement_record.record(_Workspace(findings=[{}] * 35)))["findings.drafted"]

    assert row["filed"]["label"] == "Findings register"
    assert row["filed"]["count"] == 35
    assert row["filed"]["unit"] == "finding"
    assert row["filed"]["destination"] == "findings"
    # The milestone's own delta is still carried, just not used as the size.
    assert row["history"]["metrics"] == [{"label": "Drafts prepared", "value": 1}]


def test_a_work_product_with_no_meaningful_size_states_none(stub_store):
    """The memorandum is one document; "1" would be noise, not information."""
    stub_store([])
    row = _rows(engagement_record.record(_Workspace(apm="# APM")))["planning.apm_ready"]

    assert row["filed"]["label"] == "Audit planning memorandum"
    assert row["filed"]["count"] is None
    assert row["held"] is True


def test_an_irregular_plural_is_declared_rather_than_guessed(stub_store):
    """Appending "s" to the unit produced "28 analysiss" on the demo engagement."""
    stub_store([])
    filed = _rows(engagement_record.record(_Workspace(analyses=[{}] * 28)))[
        "analysis.executed"]["filed"]

    assert (filed["unit"], filed["unit_plural"]) == ("analysis", "analyses")


def test_a_regular_unit_gets_its_plural_filled_in(stub_store):
    stub_store([])
    filed = _rows(engagement_record.record(_Workspace(rcm=[{}] * 27)))[
        "planning.rcm_ready"]["filed"]

    assert (filed["unit"], filed["unit_plural"]) == ("row", "rows")


def test_fieldwork_does_not_borrow_the_document_test_registers_size(stub_store):
    """It schedules and rolls up tests another stage filed; it has no register.

    Sizing it by `document_tests` put the same number on two rows and credited
    fieldwork with a count it never produced.
    """
    stub_store([])
    rows = _rows(engagement_record.record(_Workspace()))

    assert rows["fieldwork.executed"]["filed"]["count"] is None
    assert rows["fieldwork.executed"]["filed"]["label"] == "Fieldwork results"
    assert rows["doc_tests.executed"]["filed"]["count"] == 0


def _with_documents(root, count, *, analysed=0):
    """A workspace holding `count` documents, `analysed` of which have one filed."""
    documents = [{"id": f"D{index}"} for index in range(count)]
    for document in documents[:analysed]:
        folder = Path(root) / "Documents" / ".analysis" / document["id"]
        (folder / "generated").mkdir(parents=True, exist_ok=True)
        (folder / "generated" / "a1.json").write_text(
            json.dumps({"summary_markdown": "Filed."}), encoding="utf-8"
        )
        (folder / "index.json").write_text(
            json.dumps({"active_analysis_id": "a1"}), encoding="utf-8"
        )
    return _Workspace(documents=documents, root=root)


def test_importing_documents_does_not_file_analyses_of_them(stub_store, tmp_path):
    """Sizing the analyses row by `documents` read the import as the work.

    Eight files landed and the ledger drew "Document analyses — 8" as filed
    work, one line above the row's own readiness saying eight documents had no
    generated analysis, and counted it among the work products held.
    """
    stub_store([])
    result = engagement_record.record(_with_documents(tmp_path, 8))
    rows = _rows(result)

    assert rows["documents.analysis_generated"]["held"] is False
    assert rows["documents.analysis_generated"]["filed"]["count"] == 0
    assert rows["documents.analysis_generated"]["readiness"]["details"]["generated"] == 0
    assert result["totals"]["work_products"] == 1
    # The catalogue beside the sources still counts every document imported.
    assert result["counts"]["documents"] == 8


def test_the_analyses_row_is_sized_by_the_analyses_that_exist(stub_store, tmp_path):
    stub_store([])
    rows = _rows(engagement_record.record(_with_documents(tmp_path, 8, analysed=3)))

    assert rows["documents.analysis_generated"]["filed"]["count"] == 3
    assert rows["documents.analysis_generated"]["held"] is True


def test_the_conclusions_row_is_not_sized_into_existence_by_the_matrix(stub_store):
    """It shares the matrix's count, which would make it held the moment rows exist."""
    stub_store([])
    rows = _rows(engagement_record.record(_Workspace(apm="# APM", rcm=[{"id": "R1"}])))

    assert rows["planning.rcm_ready"]["held"] is True
    assert rows["results.rolled_up"]["held"] is False
    assert rows["results.rolled_up"]["filed"]["count"] == 1


# --------------------------------------------------------------------------- #
# Readiness is carried, never collapsed into what the engagement holds
# --------------------------------------------------------------------------- #
def test_readiness_is_reported_beside_what_is_held_rather_than_instead_of_it(
    stub_store, monkeypatch,
):
    """Two true sentences about one register, and the ledger states both.

    Readiness answers the scheduler's question — is there work left — so a
    register holding thirty findings with two observations still undrafted
    reads "missing". Letting that decide the row would report an empty findings
    register to an auditor looking at thirty findings.
    """
    stub_store([])
    monkeypatch.setattr(
        engagement_record, "_readiness",
        lambda workspace: {
            "findings.drafted": {
                "state": "missing",
                "reasons": ["2 eligible observations need finding drafts"],
                "eligible": 32,
            },
        },
    )
    row = _rows(engagement_record.record(_Workspace(findings=[{}] * 30)))["findings.drafted"]

    assert row["held"] is True
    assert row["filed"]["count"] == 30
    assert row["readiness"]["state"] == "missing"
    assert row["readiness"]["reasons"] == ["2 eligible observations need finding drafts"]
    assert row["readiness"]["details"] == {"eligible": 32}


def test_a_readiness_cascade_never_makes_a_held_stage_look_unwritten(
    stub_store, monkeypatch,
):
    """`workflow_state` marks a capability blocked when a dependency is unmet.

    That is right for scheduling and wrong for a ledger: a report of sixty
    thousand characters read "blocked" because an earlier stage still had work.
    """
    stub_store([])
    monkeypatch.setattr(
        engagement_record, "_readiness",
        lambda workspace: {"report.working_draft": {"state": "blocked", "reasons": []}},
    )
    engagement_record.report.hydrate = lambda ws: {"markdown": "# Report\n\nreal content"}
    try:
        row = _rows(engagement_record.record(_Workspace()))["report.working_draft"]
    finally:
        del engagement_record.report.hydrate

    assert row["held"] is True
    assert row["start"] is None
    assert row["readiness"]["state"] == "blocked"


def test_a_rows_body_describes_the_workspace_not_the_run_that_wrote_it(
    stub_store, monkeypatch,
):
    """The matrix filed "25 rows … 22 high" and now holds 22 rows rated 5 high.

    The count in the artifact block was already read from the workspace, so the
    row stated 22 and then described 25 in the sentence beside it, over a
    severity strip a reader takes in at a glance. Both now come from the same
    place.
    """
    stub_store([
        _run("r1", "2026-08-14T06:00:00+00:00",
             [dict(_milestone("planning.rcm_ready", "2026-08-14T06:02:00+00:00"),
                   summary="25 rows covering 4 processes.",
                   stats=[{"label": "high", "value": 22, "severity": "warning"}])],
             ),
    ])
    monkeypatch.setattr(
        engagement_record, "_live_bodies",
        lambda workspace: {"planning.rcm_ready": {
            "summary": "22 rows covering 4 processes.",
            "stats": [{"label": "high", "value": 5, "severity": "warning"}],
            "highlights": [],
        }},
    )
    row = _rows(engagement_record.record(_Workspace(rcm=[{}] * 22)))["planning.rcm_ready"]

    assert row["filed"]["count"] == 22
    assert row["summary"] == "22 rows covering 4 processes."
    assert row["stats"] == [{"label": "high", "value": 5, "severity": "warning"}]
    assert row["live_body"] is True
    # What the run said it did is not lost, it is just not the row's claim.
    assert row["history"]["summary"] == "25 rows covering 4 processes."


def test_a_stage_with_no_live_projection_keeps_what_its_run_reported(stub_store):
    """Fieldwork's projection counts the units a run scheduled, so re-running it
    with no run to describe reports "0 scheduled tests". A stale sentence that
    was true when written beats a fresh one that is false."""
    stub_store([
        _run("r1", "2026-08-14T20:00:00+00:00",
             [dict(_milestone("fieldwork.executed", "2026-08-14T20:05:00+00:00"),
                   summary="Completed 77 of 81 scheduled tests.")]),
    ])
    workspace = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        data_tests=[_ran({"id": "T1", "status": "completed_with_exception"})],
    )
    row = _rows(engagement_record.record(workspace))["fieldwork.executed"]

    assert engagement_record._SPINE["fieldwork.executed"].get("live_body") is None
    assert row["summary"] == "Completed 77 of 81 scheduled tests."
    assert row["live_body"] is False


def test_an_owed_stage_describes_nothing_rather_than_describing_zero(stub_store):
    """The live body of an empty matrix is a sentence about no rows at all."""
    stub_store([])
    row = _rows(engagement_record.record(_Workspace()))["planning.rcm_ready"]

    assert row["held"] is False
    assert row["summary"] == ""
    assert row["stats"] == []


def test_a_registry_that_cannot_answer_leaves_the_row_standing(stub_store, monkeypatch):
    """State read from the workspace is the half that has to survive."""
    stub_store([])

    def explode(workspace, scope=None):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(engagement_record.audit_capabilities, "workflow_state", explode)
    rows = _rows(engagement_record.record(_Workspace(rcm=[{}] * 22)))

    assert rows["planning.rcm_ready"]["held"] is True
    assert rows["planning.rcm_ready"]["readiness"]["state"] == ""


# --------------------------------------------------------------------------- #
# What is owed, and what waits for what
# --------------------------------------------------------------------------- #
def test_a_stage_whose_work_product_exists_is_never_owed(stub_store):
    """The report files no milestone on the demo engagement and still exists.

    Diffing the plan against the milestones would advertise "report not yet
    written" against 78,000 characters of report. Presence is the test.
    """
    stub_store([])
    workspace = _Workspace(findings=[{}] * 35)
    engagement_record.report.hydrate = lambda ws: {"markdown": "# Report\n\nreal content"}
    try:
        owed = _owed(engagement_record.record(workspace))
    finally:
        del engagement_record.report.hydrate

    assert "report.working_draft" not in owed


def test_a_stage_with_no_work_product_is_owed(stub_store):
    stub_store([])

    assert "planning.apm_ready" in _owed(engagement_record.record(_Workspace()))


def test_a_stage_the_record_cannot_ask_for_is_never_drawn_as_owed(stub_store):
    """Verification commits nothing and document tests narrate nothing here.

    As rows they belong on the ledger; as debts they would be permanent, since
    no button and no absence a reader could act on corresponds to them.
    """
    stub_store([])
    result = engagement_record.record(_Workspace())
    owed = _owed(result)

    for capability in ("audit.verified", "doc_tests.executed"):
        assert capability not in owed, capability
        assert _rows(result)[capability]["runnable"] is False


def test_a_stage_with_nothing_of_its_kind_is_neither_held_nor_owed(stub_store, tmp_path):
    """An engagement carrying one kind of source owes nothing on the other.

    Sized by its count alone, the analyses row reads absent on a
    documents-only engagement and the ledger offers a run over tables that were
    never imported. Answering "held" instead would be the opposite mistake: it
    puts a work product in the totals of an engagement that filed one.
    """
    stub_store([])
    result = engagement_record.record(_with_documents(tmp_path, 8))
    row = _rows(result)["analysis.executed"]

    assert row["held"] is False
    assert row["runnable"] is False
    assert "analysis.executed" not in _owed(result)
    assert result["totals"]["work_products"] == 1


def test_analysing_the_sources_is_the_next_step_once_they_are_imported(stub_store):
    """The two analysis stages had no imperative, so the record skipped them.

    Every other absent stage carries one. Without it neither could ever be
    `runnable`, and the first stage the ledger could offer on a freshly
    imported engagement was the memorandum — three stages down, and blocked.
    """
    stub_store([])
    result = engagement_record.record(_Workspace(tables=["ledger"]))
    rows = _rows(result)

    assert rows["analysis.executed"]["runnable"] is True
    assert rows["analysis.executed"]["start"] == {
        "prompt": "Analyse the imported tables.",
        "outcomes": ["analysis.summarized"],
        "alternates": [],
    }
    assert result["next"]["capability"] == "analysis.executed"


def test_the_analysis_button_asks_for_the_memo_not_just_the_procedures(stub_store):
    """The row is sized by the library; the button asks for the write-up.

    These are two different capabilities and the record needs both. Asking for
    the row key alone sent `requested_outcomes: ["analysis.executed"]`, which
    the router takes verbatim — `explicit_outcomes` never reads the prompt — so
    the run stopped at the executed procedures and no summary was ever written.
    Nothing on the record said one was owed, because the row it would be owed
    against was now held.
    """
    stub_store([])
    # A document as well as a table: the documents row is only owed once there
    # is something to analyse, and it is the other row that overrides.
    rows = _rows(engagement_record.record(
        _Workspace(tables=["ledger"], documents=[{"id": "d1"}])
    ))

    assert rows["analysis.executed"]["start"]["outcomes"] == ["analysis.summarized"]
    # Every other stage is terminal for its own workflow, so its button asks for
    # exactly the row it stands on. Two override, for different reasons: this
    # row stops one stage short of its own terminus, and the documents row is
    # one of two termini rather than the only one.
    # A set: row order is the plan's, not the spine's, and this is a statement
    # about which rows override rather than about where they sit.
    assert {
        row["capability"] for row in rows.values()
        if row["start"] and row["start"]["outcomes"] != [row["capability"]]
    } == {"documents.analysis_generated", "analysis.executed"}


def test_the_documents_button_asks_for_both_branches_not_just_the_prose(stub_store):
    """The row stands on one of two termini, and has to ask for both.

    The row key alone closed over the prose branch and reported satisfied over
    exactly the half it asked for: nine documents classified, eight of them
    evidence, one analysis filed, run completed with no warning.
    """
    stub_store([])
    rows = _rows(engagement_record.record(
        _Workspace(documents=[{"id": "d1"}], tables=["ledger"])
    ))

    start = rows["documents.analysis_generated"]["start"]
    assert start["outcomes"] == list(documents_workflow.FULL_DOCUMENT_OUTCOMES)
    assert "documents.schemas_stamped" in start["outcomes"]


def test_the_documents_button_offers_the_prose_only_run_as_an_alternate(stub_store):
    """The cheaper answer is reachable, and is not what a click gets.

    Deferring the sequential evidence read is a real thing to want; skipping it
    by accident is not. The note is what keeps those apart.
    """
    stub_store([])
    rows = _rows(engagement_record.record(
        _Workspace(documents=[{"id": "d1"}], tables=["ledger"])
    ))

    alternates = rows["documents.analysis_generated"]["start"]["alternates"]
    assert [item["outcomes"] for item in alternates] == [
        ["documents.analysis_generated"]
    ]
    assert alternates[0]["label"] == "Planning documents only"
    assert alternates[0]["note"]
    # Strictly narrower, or the menu is the complete answer and the button the
    # partial one — the arrangement this row exists to have corrected.
    primary = set(rows["documents.analysis_generated"]["start"]["outcomes"])
    assert set(alternates[0]["outcomes"]) < primary


def test_every_alternate_belongs_to_its_row_s_own_workflow(stub_store):
    """A menu item that could not route would fail at the click, not here."""
    stub_store([])
    for stage in engagement_record.record(_Workspace())["stages"]:
        for alternate in (stage["start"] or {}).get("alternates") or []:
            assert routing.validate_requested_outcomes(alternate["outcomes"])


def test_a_stage_waiting_on_an_earlier_one_says_so_instead_of_offering_a_button(stub_store):
    stub_store([])
    rows = _rows(engagement_record.record(
        _Workspace(tables=["ledger"], analyses=[{}])
    ))

    assert rows["planning.apm_ready"]["runnable"] is True
    assert rows["planning.apm_ready"]["blocked_reason"] == ""
    assert rows["planning.rcm_ready"]["runnable"] is False
    assert rows["planning.rcm_ready"]["blocked_reason"] == (
        "Waits for the memorandum and the cycle design."
    )


def test_a_stage_carries_what_to_ask_the_assistant_for(stub_store):
    stub_store([])
    rows = _rows(engagement_record.record(_Workspace()))

    assert rows["planning.apm_ready"]["start"] == {
        "prompt": "Draft the APM.", "outcomes": ["planning.apm_ready"],
        "alternates": [],
    }


def test_a_held_stage_offers_nothing_to_start(stub_store):
    stub_store([])
    rows = _rows(engagement_record.record(_Workspace(apm="# APM")))

    assert rows["planning.apm_ready"]["held"] is True
    assert rows["planning.apm_ready"]["start"] is None
    assert rows["planning.apm_ready"]["blocked_reason"] == ""


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
    rows = _rows(engagement_record.record(_specified_not_run()))

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
    rows = _rows(engagement_record.record(_specified_not_run()))

    assert rows["findings.drafted"]["runnable"] is False
    assert rows["findings.drafted"]["blocked_reason"] == "Waits for the conclusions."


def test_a_stage_with_several_prerequisites_names_all_the_missing_ones(stub_store):
    stub_store([])
    rows = _rows(engagement_record.record(_specified_not_run()))

    # `report.working_draft` depends on the memorandum, the conclusions and the
    # findings; the memorandum is filed, so only the other two are owed.
    assert rows["report.working_draft"]["blocked_reason"] == (
        "Waits for the conclusions and the findings."
    )


def test_a_blocker_behind_a_machine_step_is_still_named(stub_store, tmp_path):
    """The APM depends on `planning.context_ready`, which is not a row here.

    Looking one hop deep found nothing nameable and stopped, so an engagement
    holding eighty-four unanalysed documents was told the memorandum had
    nothing blocking it — and the memorandum was offered as the next stage,
    ahead of the analyses the graph says it reads.
    """
    stub_store([])
    rows = _rows(engagement_record.record(_with_documents(tmp_path, 8)))

    assert rows["planning.apm_ready"]["blocked_reason"] == (
        "Waits for the document analyses."
    )
    assert rows["planning.apm_ready"]["runnable"] is False


def test_the_walk_through_a_machine_step_reaches_the_sources(stub_store):
    """Two machine steps deep, and the head of the graph is still the answer.

    `sources.imported` earns its edge so that an empty engagement says planning
    is waiting rather than offering to write a memorandum about nothing. The
    one-hop walk lost that too.
    """
    stub_store([])
    rows = _rows(engagement_record.record(_Workspace()))

    assert rows["planning.apm_ready"]["blocked_reason"] == "Waits for the sources."


def test_a_prerequisite_the_record_cannot_see_is_not_named_as_a_blocker(stub_store):
    """`fieldwork.executed` also depends on `tests.promoted_from_analysis`.

    That stage has no presence test here, so naming it would tell a reader to
    wait for something they can neither observe nor start.
    """
    stub_store([])
    rows = _rows(engagement_record.record(_specified_not_run()))

    assert "promoted" not in rows["fieldwork.executed"]["blocked_reason"]
    assert rows["fieldwork.executed"]["blocked_reason"] == ""


def test_every_stage_that_can_block_another_has_a_readable_noun():
    """A missing noun degrades to "an earlier stage", which names nothing."""
    for capability, spec in engagement_record._SPINE.items():
        if spec.get("headline"):
            assert capability in engagement_record._NOUNS, capability


def test_every_stage_on_the_spine_has_a_readable_label(stub_store):
    """A row printed a capability id at the reader.

    Two registries carried the vocabulary and disagreed: one held
    `report.drafted`, which is not a capability the pipeline has, and had no
    entry for the dashboard stage at all, so both fell back to the raw id.
    """
    stub_store([])
    for stage in engagement_record.record(_Workspace())["stages"]:
        label = (stage["filed"] or {}).get("label", "")
        assert label != stage["capability"], label
        assert "." not in label, label


def test_the_durable_statuses_are_real_members_of_the_shared_vocabulary():
    """A renamed status would silently make every document test look executed."""
    assert rcm_execution._DURABLE_DOC_TEST_STATUSES <= TEST_STATUSES


def test_a_half_run_test_register_counts_as_fieldwork_having_run(stub_store):
    """One executed test is a result the stage produced; the rest is coverage.

    Drawing fieldwork as never started because some tests remain would invite a
    reader to redo work the filed row already reports.
    """
    stub_store([])
    workspace = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        data_tests=[
            _ran({"id": "T1", "status": "completed_with_exception"}),
            {"id": "T2", "status": "ready"},
        ],
    )

    assert "fieldwork.executed" not in _owed(engagement_record.record(workspace))


def test_a_test_the_runner_refused_to_start_is_not_a_test_that_ran(stub_store):
    """`blocked` says the runner could not start, which is the opposite of a
    result — and it is not `draft` or `ready` either, so a presence test written
    as "any status outside draft/ready" read it as executed.

    Three document tests blocked for want of evidence drew a whole engagement's
    fieldwork as done: 47 tests specified, none run, and no Run button anywhere
    on the ledger to start them.
    """
    stub_store([])
    workspace = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        data_tests=[{"id": "T1", "status": "ready"}],
        doc_tests=[
            {"id": "D1", "status": "ready"},
            {"id": "D2", "status": "blocked"},
        ],
    )
    result = engagement_record.record(workspace)
    rows = _rows(result)

    assert rows["fieldwork.executed"]["held"] is False
    assert rows["fieldwork.executed"]["runnable"] is True
    assert "fieldwork.executed" in _owed(result)


def test_a_data_test_completed_without_a_result_has_not_run(stub_store):
    """The status is a label; the result is the run that produced it.

    `rcm_execution.coverage` reports exactly this pair as
    `completed_without_durable_result` — a defect it names, not a result the
    ledger may stand on.
    """
    stub_store([])
    workspace = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        data_tests=[{"id": "T1", "status": "completed_no_exception"}],
    )

    assert _rows(engagement_record.record(workspace))["fieldwork.executed"]["held"] is False


def test_document_test_results_are_not_sized_by_the_tests_that_were_specified(
    stub_store,
):
    """The row is labelled "Document test results" and fell through to its
    `count` — `document_tests`, the size of the register `tests.specified`
    fills. It went green, stating the number of results it held, the moment the
    tests were *written*: 32 specifications drawn as 32 results on an
    engagement that had executed none of them.
    """
    stub_store([])
    specified = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        doc_tests=[{"id": "D1", "status": "ready"}, {"id": "D2", "status": "blocked"}],
    )
    executed = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        doc_tests=[{"id": "D1", "status": "completed"}, {"id": "D2", "status": "ready"}],
    )

    assert _rows(engagement_record.record(specified))["doc_tests.executed"]["held"] is False
    assert _rows(engagement_record.record(executed))["doc_tests.executed"]["held"] is True


def test_a_cycle_test_that_ran_is_read_from_its_items_not_its_summary(stub_store):
    """A cycle test's result is *only* its items, and a `list_tests` summary
    carries none — it has `state_counts` and a status and nothing to execute.

    Asking `doc_test_has_durable_result` for one therefore reads every cycle
    test as never run, however much work stands behind it, and would report
    fieldwork as owed on an engagement that had finished it. The presence tests
    take the loaded record; the count still takes the summary.
    """
    stub_store([])
    workspace = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        doc_tests=[{
            # Not a terminal status: nothing but the items says this ran.
            "id": "D1", "kind": "cycle_vouch", "status": "review_required",
            "items": [{"id": "I1", "evaluation": {"state": "passed"}}],
        }],
    )
    rows = _rows(engagement_record.record(workspace))

    assert rows["doc_tests.executed"]["held"] is True
    assert rows["fieldwork.executed"]["held"] is True
    # The register is still sized by what it contains, not by what has run.
    assert rows["doc_tests.executed"]["filed"]["count"] == 1


def test_the_register_is_read_once_in_both_of_its_shapes(stub_store, monkeypatch):
    """Loading every test on top of listing them is free only while the index is
    built once for the whole call."""
    stub_store([])
    calls = {"list": 0, "load": 0}
    listed = engagement_record.doc_tests.list_tests
    loaded = engagement_record.doc_tests.load_test

    def count_list(workspace):
        calls["list"] += 1
        return listed(workspace)

    def count_load(workspace, test_id):
        calls["load"] += 1
        return loaded(workspace, test_id)

    monkeypatch.setattr(engagement_record.doc_tests, "list_tests", count_list)
    monkeypatch.setattr(engagement_record.doc_tests, "load_test", count_load)
    monkeypatch.setattr(engagement_record, "_readiness", lambda workspace: {})
    workspace = _Workspace(
        apm="# APM", rcm=[{"id": "R1"}],
        doc_tests=[{"id": "D1", "status": "completed", "rcm_id": "R1"}],
    )

    engagement_record.record(workspace)

    assert calls == {"list": 1, "load": 1}


def test_an_engagement_with_no_document_test_neither_holds_nor_owes_results(
    stub_store,
):
    """The same answer `analysis.executed` gives an engagement with no tables:
    a stage with nothing of its kind to work on is not a debt."""
    stub_store([])
    result = engagement_record.record(
        _Workspace(apm="# APM", rcm=[{"id": "R1"}], data_tests=[{"id": "T1"}]),
    )
    row = _rows(result)["doc_tests.executed"]

    assert row["held"] is False
    assert "doc_tests.executed" not in _owed(result)
    assert row["capability"] not in {
        stage["capability"] for stage in result["stages"] if stage["runnable"]
    }


def test_an_empty_matrix_is_not_a_matrix_whose_rows_are_concluded(stub_store):
    """`rcm_without_conclusion` is empty either way; only one means concluded."""
    stub_store([])
    workspace = _Workspace(apm="# APM", data_tests=[{"id": "T1", "status": "completed"}])

    assert "results.rolled_up" in _owed(engagement_record.record(workspace))


def test_a_presence_test_that_raises_does_not_invent_absent_work(stub_store):
    """"Cannot answer" must never collapse into "absent".

    Inviting an auditor to redo work that may already exist is the worse of the
    two failures, so the row is drawn as neither held nor owed.
    """
    stub_store([])

    def explode(workspace):
        raise RuntimeError("cannot read the report")

    engagement_record.report.hydrate = explode
    try:
        row = _rows(engagement_record.record(_Workspace()))["report.working_draft"]
    finally:
        del engagement_record.report.hydrate

    assert row["held"] is False
    assert row["start"] is None
    assert row["runnable"] is False


# --------------------------------------------------------------------------- #
# What a stage left open, and what the record asks for first
# --------------------------------------------------------------------------- #
def test_open_points_attach_to_the_stage_that_left_them(stub_store):
    stub_store([])
    workspace = _Workspace(
        findings=[{"cause_pending": True, "auditor_confirmed": True}] * 35
    )
    result = engagement_record.record(workspace)
    row = _rows(result)["findings.drafted"]

    assert [point["key"] for point in row["open_points"]] == ["findings_followup"]
    assert "35 of 35 findings" in row["open_points"][0]["message"]


def test_an_unconfirmed_register_is_asked_to_confirm_before_anything_else(stub_store):
    """The findings row carries one debt, and this is the one that comes first.

    Only a confirmed finding reaches the report. Asked for root causes instead,
    an auditor writes them into twenty-four findings the report still states
    were never identified.
    """
    stub_store([])
    workspace = _Workspace(findings=[{"cause_pending": True}] * 24)
    result = engagement_record.record(workspace)
    row = _rows(result)["findings.drafted"]

    assert [point["key"] for point in row["open_points"]] == ["unconfirmed_findings"]
    assert row["open_points"][0]["message"] == (
        "24 of 24 findings are not confirmed for reporting — the report will "
        "carry none of them."
    )
    assert row["open_points"][0]["action"] == "Confirm findings"
    assert row["open_points"][0]["destination"] == "findings"
    assert result["next"]["key"] == "unconfirmed_findings"


def test_a_part_confirmed_register_says_what_is_left_out(stub_store):
    """The report carries the confirmed ones, so the absolute claim is dropped."""
    stub_store([])
    result = engagement_record.record(_Workspace(findings=[
        {"auditor_confirmed": True, "management_response": "Accepted."},
        {"auditor_confirmed": False},
    ]))

    point = next(
        item for item in result["open_points"] if item["key"] == "unconfirmed_findings"
    )
    assert point["message"] == (
        "1 of 2 findings are not confirmed for reporting and will be left out "
        "of the report."
    )


def test_confirming_the_register_hands_the_row_back_to_the_cause_debt(stub_store):
    """One slot, two debts: the smaller one surfaces once the larger is settled."""
    stub_store([])
    result = engagement_record.record(_Workspace(findings=[
        {"auditor_confirmed": True, "cause_pending": True},
    ]))

    assert [point["key"] for point in result["open_points"]] == ["findings_followup"]


def test_a_settled_register_leaves_no_findings_debt(stub_store):
    stub_store([])
    result = engagement_record.record(_Workspace(findings=[
        {"auditor_confirmed": True, "management_response": "Accepted."},
    ]))

    assert [point["key"] for point in result["open_points"]] == []


def test_a_debt_always_has_a_row_to_sit_on(stub_store):
    """The rows are draft and no `planning.rcm_ready` milestone survives.

    A debt used to be orphaned when the stage that left it had never filed,
    which on a lost run folder meant every debt on the engagement.
    """
    stub_store([])
    result = engagement_record.record(_Workspace(rcm=[{"id": "R1", "review_status": "draft"}]))
    row = _rows(result)["planning.rcm_ready"]

    assert row["history"] is None
    assert [point["key"] for point in row["open_points"]] == ["draft_rcm"]
    # The absolute claim is only made where it holds.
    assert row["open_points"][0]["message"] == (
        "1 of 1 rows are still marked draft. None has been reviewed."
    )
    attached = {
        point["key"] for stage in result["stages"] for point in stage["open_points"]
    }
    assert {point["key"] for point in result["open_points"]} <= attached


def test_a_part_reviewed_matrix_does_not_claim_nothing_was_reviewed(stub_store):
    """The count and the claim after it must describe the same matrix."""
    stub_store([])
    result = engagement_record.record(_Workspace(rcm=[
        {"id": "R1", "review_status": "reviewed"},
        {"id": "R2", "review_status": "draft"},
    ]))

    point = next(item for item in result["open_points"] if item["key"] == "draft_rcm")
    assert point["message"] == "1 of 2 rows are still marked draft."


def test_a_fully_reviewed_matrix_leaves_no_sign_off_debt(stub_store):
    stub_store([])
    result = engagement_record.record(_Workspace(rcm=[
        {"id": "R1", "review_status": "reviewed"},
    ]))

    assert [point["key"] for point in result["open_points"]] == []


def test_review_outranks_unstarted_work_as_the_next_step(stub_store):
    """Auto mode runs the next stage by itself; only a person can read."""
    stub_store([])
    workspace = _Workspace(
        findings=[{"cause_pending": True, "auditor_confirmed": True}] * 35
    )
    result = engagement_record.record(workspace)

    assert result["next"]["kind"] == "open_point"
    assert result["next"]["key"] == "findings_followup"
    # ...and there was runnable work it deliberately did not choose.
    assert any(stage["runnable"] for stage in result["stages"])


def test_with_nothing_open_the_next_step_is_the_first_runnable_stage(stub_store):
    stub_store([])
    result = engagement_record.record(
        _Workspace(tables=["ledger"], analyses=[{}])
    )

    assert result["next"]["kind"] == "stage"
    assert result["next"]["capability"] == "planning.apm_ready"


def test_an_empty_engagement_is_asked_to_import_before_anything_else(stub_store):
    """The first thing a new workspace was told to do was plan an audit of
    nothing. Sources is the head of the spine, so it is the first runnable
    stage, and it asks for an import rather than offering to run something —
    the assistant cannot import."""
    stub_store([])
    result = engagement_record.record(_Workspace())

    assert result["next"]["capability"] == "sources.imported"
    assert result["next"]["action"] == "import"
    assert result["next"]["headline"] == "Bring in the audit file"
    # It carries no prompt: there is no assistant command that imports.
    assert result["next"]["start"] is None


def test_a_finished_and_reviewed_engagement_proposes_nothing(stub_store):
    stub_store([])
    workspace = _Workspace(
        apm="# APM",
        # A finished engagement has a cycle: the matrix's process names come
        # from it, so a matrix that is reviewed and concluded was written
        # against one.
        planning={"cycle": {"name": "Cycle", "steps": [{"name": "Process"}]}},
        rcm=[{"id": "R1", "review_status": "reviewed", "conclusion": "effective"}],
        data_tests=[
            _ran({"id": "T1", "rcm_id": "R1", "status": "completed_no_exception"}),
        ],
        analyses=[{}], tiles=[{}], tables=["ledger"],
        findings=[{
            "management_response": "Agreed.", "cause_pending": False,
            "auditor_confirmed": True,
        }],
    )
    engagement_record.report.hydrate = lambda ws: {"markdown": "# Report"}
    try:
        result = engagement_record.record(workspace)
    finally:
        del engagement_record.report.hydrate

    assert _owed(result) == set()
    assert result["open_points"] == []
    assert result["next"] is None
    # ...and the ledger still draws every stage, now reporting what it holds.
    assert len(result["stages"]) == len(engagement_record._SPINE)


# --------------------------------------------------------------------------- #
# Reading the workspace once
# --------------------------------------------------------------------------- #
def test_the_record_reads_each_expensive_source_once(stub_store, monkeypatch):
    """The record asks the same two questions repeatedly; it may read once.

    Every stage's presence is tested, `_blocked_by` re-tests each one's
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
    # Readiness reads the same sources, through the shared cache rather than
    # this memo. Its own discipline is pinned separately; counting invocations
    # here would count reads the cache underneath it already absorbs.
    monkeypatch.setattr(engagement_record, "_readiness", lambda workspace: {})

    engagement_record.record(_Workspace(rcm=[{"id": "R1"}], apm="# APM"))

    assert calls == {"tests": 1, "completion": 1}


def test_the_record_holds_the_document_test_cache_open(stub_store, monkeypatch):
    """What makes reading readiness affordable at all.

    `_findings_ready` asks `support_issues` about every finding, and each of
    those lists every document test, which materializes every cycle item from
    its evidence records. Thirty findings paid for that thirty times: the whole
    projection cost 2131ms outside this scope and 123ms inside it.
    """
    stub_store([])
    seen = {}

    def capture(workspace):
        seen["scope"] = engagement_record.doc_tests._cache.get()
        return {}

    monkeypatch.setattr(engagement_record, "_readiness", capture)
    engagement_record.record(_Workspace())

    assert seen["scope"] is not None
    # ...and it is released with the call that opened it.
    assert engagement_record.doc_tests._cache.get() is None


def test_readiness_is_projected_once_for_the_whole_record(stub_store, monkeypatch):
    """It is the most expensive thing the record reads, and every row wants it."""
    stub_store([])
    calls = {"n": 0, "only": None}

    def count(workspace, scope=None, *, only=None):
        calls["n"] += 1
        calls["only"] = only
        return {}

    monkeypatch.setattr(engagement_record.audit_capabilities, "workflow_state", count)
    engagement_record.record(_Workspace())

    assert calls["n"] == 1
    # ...and only for the rows it draws. The registries declare forty-six
    # capabilities; the ledger reads twelve, and the rest were computed for
    # nothing — the disposition of every document test among them.
    assert tuple(calls["only"]) == tuple(engagement_record._SPINE)


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
