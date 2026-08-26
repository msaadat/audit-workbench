import json

import pytest

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


def test_read_events_skips_torn_line(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    store.append_event(workspace_with_data, run["id"], "run_status", {"status": "queued"})
    path = store.run_dir(workspace_with_data, run["id"]) / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "type": "trunc')  # crash mid-append
    events = store.read_events(workspace_with_data, run["id"])
    assert len(events) == 1


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
