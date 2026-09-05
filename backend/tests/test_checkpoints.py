"""Workspace checkpoints and the Debug console's step rollback."""

import json

import pytest
from fastapi.testclient import TestClient

from app import checkpoints, workspaces
from app.main import create_app
from app.workspace_transactions import _write_text_atomic


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def _capture(ws, **overrides):
    return checkpoints.capture(ws, **{
        "run_id": "run-1", "stage_id": "stage-1",
        "capability": "planning.rcm_ready", "label": "Risk and control matrix",
        **overrides,
    })


def test_capture_covers_artifacts_and_excludes_sources_and_history(workspace_with_data):
    """The surface is the audit's artifacts, not the whole workspace folder.

    Imported data, the agent run records the console reads, and the telemetry
    database are all deliberately outside it — a rollback that erased the run
    history offering it would destroy its own audit trail.
    """
    ws = workspace_with_data
    (ws.root / "AgentRuns" / "run-1").mkdir(parents=True, exist_ok=True)
    (ws.root / "AgentRuns" / "run-1" / "run.json").write_text("{}", encoding="utf-8")
    (ws.root / "AssistantChats").mkdir(exist_ok=True)
    (ws.root / "AssistantChats" / "c1.json").write_text("{}", encoding="utf-8")

    captured = {
        str(path.relative_to(ws.root)).replace("\\", "/")
        for path in checkpoints.surface(ws)
    }
    assert "workspace.json" in captured
    assert not any(path.startswith("Data/") for path in captured)
    assert not any(path.startswith("AgentRuns/") for path in captured)
    assert not any(path.startswith("AssistantChats/") for path in captured)
    assert not any(path.startswith(checkpoints.CHECKPOINT_DIRNAME) for path in captured)
    assert not any(path.startswith("telemetry.db") for path in captured)


def test_second_checkpoint_of_an_unchanged_workspace_stores_no_new_content(
    workspace_with_data,
):
    """Content addressing is what makes a per-step restore point affordable."""
    first = _capture(workspace_with_data)
    second = _capture(workspace_with_data, stage_id="stage-2")

    assert first["file_count"] == second["file_count"]
    assert first["new_bytes"] > 0
    assert second["new_bytes"] == 0


def test_rollback_restores_reverts_and_removes_exactly(workspace_with_data):
    """A rollback returns the surface to the captured state — in all three ways.

    Files the step changed go back, files it deleted come back, and files that
    did not exist at capture are removed. The last is the destructive one, and
    is why the console shows the plan before asking.
    """
    ws = workspace_with_data
    planning = ws.root / "Planning"
    planning.mkdir(exist_ok=True)
    _write_text_atomic(planning / "APM.md", "original memorandum\n")
    workspaces.write_json_atomic(planning / "keep.json", {"value": "before"})
    checkpoint = _capture(ws)

    _write_text_atomic(planning / "APM.md", "rewritten by the step\n")
    (planning / "keep.json").unlink()
    workspaces.write_json_atomic(planning / "created-after.json", {"new": True})

    plan = checkpoints.plan(ws, checkpoint["id"])
    assert "Planning/APM.md" in plan["changed"]
    assert "Planning/keep.json" in plan["restored"]
    assert plan["removed"] == ["Planning/created-after.json"]
    assert plan["restorable"]

    checkpoints.restore(ws, checkpoint["id"])

    assert (planning / "APM.md").read_text(encoding="utf-8") == "original memorandum\n"
    assert json.loads((planning / "keep.json").read_text(encoding="utf-8")) == {"value": "before"}
    assert not (planning / "created-after.json").exists()


def test_restore_advances_the_revision_past_the_one_it_restored(workspace_with_data):
    """The definition is stamped forward, never back.

    ``workspace.json`` carries the optimistic-concurrency revision, and the
    parsed-artifact cache is keyed on it. Restoring the captured number verbatim
    would let a cache entry from before the rollback answer for the state after
    it, so the rollback has to leave a revision nothing has ever cached.
    """
    ws = workspace_with_data
    checkpoint = _capture(ws)
    captured_revision = checkpoint["revision"]

    ws.description = "changed after the checkpoint"
    ws.save()
    later_revision = workspaces.load_workspace(ws.id).revision
    assert later_revision > captured_revision

    result = checkpoints.restore(ws, checkpoint["id"])

    restored = workspaces.load_workspace(ws.id)
    assert result["revision"] > later_revision
    assert restored.revision == result["revision"]
    assert restored.description != "changed after the checkpoint"


def test_restoring_twice_is_a_no_op_the_second_time(workspace_with_data):
    """The forward revision stamp must not read as outstanding work.

    A restored workspace never byte-matches its checkpoint, because the
    definition's revision was deliberately moved on. Comparing on the hash alone
    would leave the console reporting one file still to write, forever.
    """
    ws = workspace_with_data
    checkpoint = _capture(ws)
    workspaces.write_json_atomic(ws.root / "Planning" / "extra.json", {"a": 1})
    checkpoints.restore(ws, checkpoint["id"])

    plan = checkpoints.plan(workspaces.load_workspace(ws.id), checkpoint["id"])
    assert plan["changed"] == []
    assert plan["restored"] == []
    assert plan["removed"] == []
    assert plan["unchanged"] == checkpoint["file_count"]


def test_a_damaged_blob_refuses_the_restore_rather_than_writing_wrong_bytes(
    workspace_with_data,
):
    """Content is verified before it is written back, not trusted by name.

    Every writer in the application replaces files by rename, so a hardlinked
    blob cannot be mutated in place — but the store cannot prove that of a
    future writer, and a half-restored engagement is a state that never existed.
    """
    ws = workspace_with_data
    target = ws.root / "Planning" / "APM.md"
    target.parent.mkdir(exist_ok=True)
    _write_text_atomic(target, "original\n")
    checkpoint = _capture(ws)
    digest = next(
        row["sha1"]
        for row in checkpoints.debug_store.connection(ws.id).execute(
            "SELECT path, sha1 FROM checkpoint_files WHERE checkpoint_id = ?",
            (checkpoint["id"],),
        )
        if row["path"] == "Planning/APM.md"
    )
    checkpoints._blob_path(ws, digest).write_text("corrupted\n", encoding="utf-8")
    _write_text_atomic(target, "moved on\n")

    assert not checkpoints.plan(ws, checkpoint["id"])["restorable"]
    with pytest.raises(workspaces.WorkspaceError, match="no longer retrievable"):
        checkpoints.restore(ws, checkpoint["id"])
    assert target.read_text(encoding="utf-8") == "moved on\n"


def test_retention_drops_the_oldest_checkpoints_and_sweeps_their_blobs(
    workspace_with_data, monkeypatch,
):
    ws = workspace_with_data
    monkeypatch.setattr(checkpoints.telemetry_db, "MAX_CHECKPOINTS_PER_RUN", 3)
    for index in range(5):
        workspaces.write_json_atomic(ws.root / "Planning" / f"p{index}.json", {"i": index})
        _capture(ws, stage_id=f"stage-{index}")

    kept = checkpoints.list_for_run(ws, "run-1")
    assert [item["stage_id"] for item in kept] == ["stage-2", "stage-3", "stage-4"]
    # A blob only reachable from an evicted manifest is gone; one the survivors
    # still reference is not.
    assert checkpoints.usage(ws)["checkpoints"] == 3


def test_steps_endpoint_pairs_each_stage_with_its_rollback_target(
    client, workspace_with_data,
):
    """The console's step list, and the gap it must not hide.

    A stage whose checkpoint never existed reports ``checkpoint: None`` rather
    than being omitted: an operator needs to be told a step cannot be rolled
    back, which is not the same as not being shown the step.
    """
    ws = workspace_with_data
    from app.agent import store as agent_store

    run = agent_store.new_command_run(ws, "auto", {"source": "chat", "text": "go"})
    run["engine"] = "workflow"
    run["workflow"] = {"stages": [
        {"id": "stage-1", "capability": "planning.rcm_ready", "title": "Matrix",
         "status": "succeeded", "units": [{"id": "rcm", "result_refs": ["rcm:R-1"]}]},
        {"id": "stage-2", "capability": "tests.specified", "title": "Tests",
         "status": "succeeded", "units": [{"id": "tests", "result_refs": []}]},
    ]}
    agent_store.save_run(ws, run)
    _capture(ws, run_id=run["id"], stage_id="stage-1")

    payload = client.get(
        f"/api/workspaces/{ws.id}/debug/runs/{run['id']}/steps"
    ).json()

    assert [step["capability"] for step in payload["steps"]] == [
        "planning.rcm_ready", "tests.specified",
    ]
    assert payload["steps"][0]["checkpoint"]["stage_id"] == "stage-1"
    assert payload["steps"][0]["result_refs"] == ["rcm:R-1"]
    assert payload["steps"][1]["checkpoint"] is None
    assert all(step["settled"] for step in payload["steps"])


def test_restore_over_the_api_requires_the_typed_confirmation(
    client, workspace_with_data,
):
    """Rolling back rewrites audit content, so a bare POST must not do it."""
    ws = workspace_with_data
    workspaces.write_json_atomic(ws.root / "Planning" / "keep.json", {"v": 1})
    checkpoint = _capture(ws)
    (ws.root / "Planning" / "keep.json").unlink()

    refused = client.post(
        f"/api/workspaces/{ws.id}/debug/checkpoints/{checkpoint['id']}/restore"
    )
    assert refused.status_code >= 400
    assert not (ws.root / "Planning" / "keep.json").exists()

    accepted = client.post(
        f"/api/workspaces/{ws.id}/debug/checkpoints/{checkpoint['id']}/restore"
        f"?confirm={ws.id}"
    )
    assert accepted.status_code == 200
    assert (ws.root / "Planning" / "keep.json").exists()


def test_capture_is_skipped_when_checkpointing_is_switched_off(
    workspace_with_data, monkeypatch,
):
    monkeypatch.setenv(checkpoints.ENV_VAR, "off")
    assert checkpoints.enabled() is False
    assert _capture(workspace_with_data) is None
