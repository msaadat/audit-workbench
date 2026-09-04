import json
import shutil
import tempfile
from pathlib import Path

import pytest

from app import accounts, config, db, telemetry_db, workspaces
from app.agent import store
from app.workspaces import WorkspaceError


def test_new_run_persists_and_loads(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto", {"objective": "revenue audit"})
    loaded = store.load_run(workspace_with_data, run["id"])
    assert loaded["status"] == "queued"
    assert loaded["engine"] == store.INTAKE_ENGINE
    assert loaded["mode"] == "auto"
    assert loaded["context"]["objective"] == "revenue audit"
    assert loaded["plan"] == {"stages": []}
    assert loaded["activity"] is None
    assert loaded["activity_revision"] == 0


def test_new_run_rejects_bad_mode(workspace_with_data):
    with pytest.raises(WorkspaceError, match="mode"):
        store.new_run(workspace_with_data, "yolo")


def test_save_run_is_atomic_no_tmp_left(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    run["status"] = "executing"
    store.save_run(workspace_with_data, run)
    folder = store.run_dir(workspace_with_data, run["id"])
    assert not list(folder.glob("*.tmp"))
    assert store.load_run(workspace_with_data, run["id"])["status"] == "executing"


def test_events_append_and_cursor(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    store.append_event(workspace_with_data, run["id"], "run_status", {"status": "queued"})
    store.append_event(workspace_with_data, run["id"], "task_update", {"task": {"id": "t1"}})
    all_events = store.read_events(workspace_with_data, run["id"])
    assert [e["seq"] for e in all_events] == [1, 2]
    tail = store.read_events(workspace_with_data, run["id"], after=1)
    assert len(tail) == 1 and tail[0]["type"] == "task_update"


def test_events_are_scoped_and_sequenced_per_run(workspace_with_data):
    """What the torn-line test used to cover, in the terms that now apply.

    An interrupted append could leave the JSONL log with half a line, so reading
    it had to tolerate one. A row is written whole or not at all, so the risk
    that remains is a shared sequence: two runs writing at once must not consume
    each other's numbers or read each other's events.
    """
    first = store.new_run(workspace_with_data, "auto")
    second = store.new_run(workspace_with_data, "auto")
    for index in range(3):
        store.append_event(workspace_with_data, first["id"], "run_status", {"n": index})
        store.append_event(workspace_with_data, second["id"], "task_update", {"n": index})

    first_events = store.read_events(workspace_with_data, first["id"])
    second_events = store.read_events(workspace_with_data, second["id"])
    assert [event["seq"] for event in first_events] == [1, 2, 3]
    assert [event["seq"] for event in second_events] == [1, 2, 3]
    assert {event["type"] for event in first_events} == {"run_status"}
    assert {event["type"] for event in second_events} == {"task_update"}


def test_list_runs_newest_first(workspace_with_data):
    first = store.new_run(workspace_with_data, "auto")
    second = store.new_run(workspace_with_data, "permission")
    runs = store.list_runs(workspace_with_data)
    assert [r["id"] for r in runs] == sorted(
        [first["id"], second["id"]], reverse=True
    )
    assert {"id", "engine", "status", "mode", "task_counts"} <= set(runs[0])
    assert {"activity", "activity_revision", "duration_ms"} <= set(runs[0])


def test_recover_orphans_marks_interrupted(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    run["status"] = "executing"
    store.save_run(workspace_with_data, run)
    recovered = store.recover_orphans(workspace_with_data, live_run_ids=set())
    assert recovered == [run["id"]]
    reloaded = store.load_run(workspace_with_data, run["id"])
    assert reloaded["status"] == "interrupted"
    assert any("recovered" in w for w in reloaded["warnings"])
    # idempotent, and live runs are left alone
    assert store.recover_orphans(workspace_with_data, live_run_ids=set()) == []


def test_recover_orphans_spares_live_runs(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    run["status"] = "executing"
    store.save_run(workspace_with_data, run)
    assert store.recover_orphans(workspace_with_data, {run["id"]}) == []
    assert store.load_run(workspace_with_data, run["id"])["status"] == "executing"


def test_load_missing_run_raises(workspace_with_data):
    with pytest.raises(WorkspaceError, match="not found"):
        store.load_run(workspace_with_data, "nope")


def test_run_json_is_valid_json_on_disk(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    path = store.run_dir(workspace_with_data, run["id"]) / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == run["id"]


def test_new_run_persists_the_one_retained_protocol_engine(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto", None, kind="intake")

    assert run["engine"] == store.INTAKE_ENGINE
    assert store.load_run(workspace_with_data, run["id"])["engine"] == store.INTAKE_ENGINE


def test_new_run_rejects_the_retired_analysis_kind(workspace_with_data):
    with pytest.raises(WorkspaceError, match="run kind"):
        store.new_run(workspace_with_data, "auto", None, kind="analysis")


def test_new_command_run_chooses_no_engine_before_routing(workspace_with_data):
    """Creation persists no engine; routing selects one before thread launch."""
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "route this command"}
    )

    assert run["engine"] is None
    assert run["route"] is None
    assert store.run_summary(run)["engine"] is None
    assert store.is_command_run(run) is True
    assert store.is_command_run(
        store.new_run(workspace_with_data, "auto", None, kind="intake")
    ) is False


def test_default_llm_concurrency_fans_out_parallel_stages(monkeypatch):
    """A stage declared parallel needs a width above 1 to actually be parallel.

    Document chunk units are independent and commit nothing, so the width is
    what decides throughput. Pinned at 1 the barrier bought failure isolation
    only: a run's wall time was the sum of its model calls, one after another.
    """

    from app.agent import routing

    monkeypatch.delenv("AGENT_LLM_CONCURRENCY", raising=False)
    assert routing.default_llm_concurrency() == 4

    monkeypatch.setenv("AGENT_LLM_CONCURRENCY", "6")
    assert routing.default_llm_concurrency() == 6

    # Bounded at both ends: the ceiling here is the provider's rate limit.
    monkeypatch.setenv("AGENT_LLM_CONCURRENCY", "99")
    assert routing.default_llm_concurrency() == 8
    monkeypatch.setenv("AGENT_LLM_CONCURRENCY", "0")
    assert routing.default_llm_concurrency() == 1
    monkeypatch.setenv("AGENT_LLM_CONCURRENCY", "not-a-number")
    assert routing.default_llm_concurrency() == 4


def test_explicit_run_limit_still_overrides_the_concurrency_default(monkeypatch):
    from app.agent import routing

    monkeypatch.delenv("AGENT_LLM_CONCURRENCY", raising=False)
    run = {"limits": {"max_llm_concurrency": 2}}
    assert int(
        run.get("limits", {}).get("max_llm_concurrency")
        or routing.default_llm_concurrency()
    ) == 2


# ``write_json_atomic`` writes ".<name>.<6 hex>.tmp" alongside its destination,
# so a sidecar costs its own path plus this before Windows' 260-character
# ceiling is reached.
_ATOMIC_TMP_OVERHEAD = len(".") + len(".abcdef.tmp")


def _wide_unit_id(tables: int, stem: str = "table-with-a-longish-name") -> str:
    """A join-utility unit ID, which names every table in its scope."""
    return "join_utility:" + ":".join(f"{index:02d}-{stem}" for index in range(tables))


def test_unit_filename_keeps_short_semantic_names_verbatim():
    # Nearly every unit names one or two refs and must keep the readable name it
    # has always had, so sidecars written before the cap stay addressable.
    for unit_id in (
        "analysis_reading",
        "relationship:01-employees:02-expense-claims",
        "analysis_definitions:02-expense-claims",
    ):
        assert store.unit_filename(unit_id) == store.legacy_unit_filename(unit_id)
    assert (
        store.unit_filename("relationship:01-employees:02-expense-claims")
        == "relationship%3A01-employees%3A02-expense-claims.json"
    )


def test_unit_filename_caps_names_that_grow_with_their_scope():
    unit_id = _wide_unit_id(40)
    name = store.unit_filename(unit_id)
    assert len(store.legacy_unit_filename(unit_id)) > store.UNIT_FILENAME_LIMIT
    assert len(name) <= store.UNIT_FILENAME_LIMIT
    # The readable prefix survives, cut on a whole percent-escape.
    assert name.startswith("join_utility%3A00-table")
    assert name.endswith(".json")
    assert "%" not in name.split("+")[0][-2:]


def test_unit_filename_is_deterministic_and_collision_free_across_scopes():
    names: dict[str, int] = {}
    for count in range(2, 60):
        unit_id = _wide_unit_id(count)
        name = store.unit_filename(unit_id)
        assert name == store.unit_filename(unit_id)
        assert name not in names, f"{count} tables collides with {names[name]}"
        names[name] = count


def test_capped_unit_filename_clears_the_windows_ceiling_in_a_real_run_folder():
    """The six-table expenses join-utility unit that first hit this ceiling.

    Asserted against a measured run-folder depth rather than the test's own
    temporary directory, which pytest nests far deeper than a real workspace.
    """
    run_folder = (
        r"C:\Users\913300\Desktop\audit-workbench\Workspaces\Users\local"
        r"\Workspaces\expenses\AgentRuns\20260903-122859-fe940f\contexts"
    )
    unit_id = (
        "join_utility:01-employees:02-expense-claims:03-claim-line-items"
        ":04-approval-log:05-payment-vouchers:06-gl-postings"
    )
    legacy = len(run_folder) + 1 + len(store.legacy_unit_filename(unit_id))
    capped = len(run_folder) + 1 + len(store.unit_filename(unit_id))
    assert legacy + _ATOMIC_TMP_OVERHEAD > 260  # the reported failure
    assert capped + _ATOMIC_TMP_OVERHEAD < 260


@pytest.fixture
def shallow_workspace():
    """A workspace at a realistically shallow root.

    pytest nests ``tmp_path`` roughly 50 characters deeper than any real
    installation, which leaves too little of Windows' 260-character budget to
    exercise a scope-wide unit at all.
    """
    root = Path(tempfile.mkdtemp(prefix="awb"))
    previous = config.DATA_ROOT
    db.close_all()
    telemetry_db.close_all()
    config.DATA_ROOT = root
    try:
        accounts.ensure_local_user()
        yield workspaces.create_workspace("Expenses")
    finally:
        db.close_all()
        telemetry_db.close_all()
        config.DATA_ROOT = previous
        shutil.rmtree(root, ignore_errors=True)


def test_unit_sidecar_round_trips_a_scope_wide_unit(shallow_workspace):
    from app.agent.runtime.unit_pipeline import UnitSidecarStore

    run = store.new_run(shallow_workspace, "auto")
    folder = store.run_dir(shallow_workspace, run["id"]) / "proposals"
    unit_id = _wide_unit_id(40)
    assert len(str(folder)) + 1 + len(store.legacy_unit_filename(unit_id)) > 260
    sidecars = UnitSidecarStore(shallow_workspace, run["id"])
    reference = sidecars.persist_proposal(unit_id, {"status": "proposed"})
    assert (folder / reference["path"].split("/", 1)[1]).is_file()
    assert sidecars.load_proposal(unit_id, reference) == {"status": "proposed"}
    assert not list(folder.glob("*.tmp"))


def test_unit_sidecar_still_reads_an_uncapped_name_from_an_earlier_run(
    shallow_workspace,
):
    """A run written where no ceiling applied keeps its uncapped name readable."""
    from app.agent.runtime.unit_pipeline import UnitSidecarStore

    run = store.new_run(shallow_workspace, "auto")
    folder = store.run_dir(shallow_workspace, run["id"]) / "proposals"
    unit_id = _wide_unit_id(12, stem="t")
    legacy_name = store.legacy_unit_filename(unit_id)
    assert len(legacy_name) > store.UNIT_FILENAME_LIMIT
    assert store.unit_filename(unit_id) != legacy_name
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"status": "proposed"}
    (folder / legacy_name).write_text(json.dumps(payload), encoding="utf-8")
    assert UnitSidecarStore(shallow_workspace, run["id"]).load_proposal(
        unit_id
    ) == payload
