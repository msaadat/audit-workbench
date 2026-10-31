"""The record's stage status, stored and refreshed instead of swept per read.

A workspace created with the ``status_store`` flag keeps the readiness sweep in
``.status/stages.json``; one created before it sweeps on every read, exactly as
before. These pin both sides, the writers that must announce a change, and the
property everything else rests on: a stored answer is the sweep, not an
approximation of it.
"""

from __future__ import annotations

import json

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import engagement_record, stage_status, workspaces
from app.agent import execution_support
from app.main import create_app


@pytest.fixture(autouse=True)
def store_on(monkeypatch):
    monkeypatch.setattr(stage_status, "ENABLED", True)
    monkeypatch.setattr(stage_status, "SERVE_STALE", True)
    monkeypatch.setattr(stage_status, "DEBOUNCE_SECONDS", 0.0)
    yield
    stage_status.wait_idle()


def _csv(columns: dict) -> bytes:
    return pl.DataFrame(columns).write_csv().encode()


def _engagement(name="Stored status"):
    ws = workspaces.create_workspace(name)
    ws.add_table("invoices.csv", _csv({
        "INVOICE_ID": [f"INV{i}" for i in range(12)],
        "PO_NUMBER": [f"PO{i % 6}" for i in range(12)],
        "AMOUNT": [100.0 + i for i in range(12)],
    }))
    ws.add_table("orders.csv", _csv({
        "PO_NUMBER": [f"PO{i}" for i in range(6)],
        "VENDOR": ["A", "B", "C"] * 2,
    }))
    stage_status.wait_idle()
    return ws.reload()


def _hold_refresher(monkeypatch):
    """Leave an out-of-date answer out of date for the rest of the test."""
    monkeypatch.setattr(stage_status, "schedule_refresh", lambda _root: None)


def _states(payload: dict) -> dict:
    return {stage["capability"]: stage["readiness"] for stage in payload["stages"]}


def _legacy(ws) -> dict:
    """The record as a workspace without the flag draws it."""
    definition = json.loads(ws.definition_path.read_text(encoding="utf-8"))
    definition.pop(stage_status.FLAG)
    ws.definition_path.write_text(json.dumps(definition), encoding="utf-8")
    try:
        return engagement_record.record(ws.reload())
    finally:
        definition[stage_status.FLAG] = True
        ws.definition_path.write_text(json.dumps(definition), encoding="utf-8")


def test_a_new_workspace_carries_the_flag_and_keeps_it_across_saves():
    ws = workspaces.create_workspace("Flagged")
    assert ws.status_store
    ws.add_table("t.csv", _csv({"A": [1, 2]}))
    stored = json.loads(ws.definition_path.read_text(encoding="utf-8"))
    assert stored[stage_status.FLAG] is True


def test_a_workspace_without_the_flag_sweeps_and_stores_nothing(monkeypatch):
    monkeypatch.setattr(stage_status, "ENABLED", False)
    ws = workspaces.create_workspace("Legacy")
    ws.add_table("t.csv", _csv({"A": [1, 2]}))
    sweeps = []
    sweep = engagement_record._sweep
    monkeypatch.setattr(engagement_record, "_sweep", lambda w: sweeps.append(1) or sweep(w))

    first = engagement_record.record(ws.reload())
    engagement_record.record(ws.reload())

    assert not ws.status_store
    assert first["refreshing"] is False
    assert len(sweeps) == 2
    assert not (ws.root / stage_status.DIRNAME).exists()


def _unflag(ws) -> int:
    """Strip the flag as a workspace created before the store has none."""
    definition = json.loads(ws.definition_path.read_text(encoding="utf-8"))
    definition.pop(stage_status.FLAG)
    ws.definition_path.write_text(json.dumps(definition), encoding="utf-8")
    return int(definition["revision"])


def test_opening_an_older_workspace_gives_it_the_flag_without_a_new_revision():
    """An open tab's ETag must not go stale because somebody else opened it."""
    ws = _engagement("Predates the store")
    revision = _unflag(ws)

    opened = workspaces.load_workspace(ws.id)

    assert opened.status_store
    stored = json.loads(ws.definition_path.read_text(encoding="utf-8"))
    assert stored[stage_status.FLAG] is True
    assert stored["revision"] == revision


def test_an_adopted_workspace_stores_its_answer_and_reads_it_back(monkeypatch):
    ws = _engagement("Adopted")
    _unflag(ws)
    client = TestClient(create_app())

    client.get(f"/api/workspaces/{ws.id}/engagement/record")

    assert stage_status.lookup(ws.root).current

    def refuse(_workspace):
        raise AssertionError("a current stored answer must not be swept again")

    monkeypatch.setattr(engagement_record, "_sweep", refuse)
    engagement_record.record(workspaces.load_workspace(ws.id))


def test_an_instance_loaded_before_adoption_keeps_the_flag_when_it_saves():
    """An agent run holds its workspace for minutes. Saving from a copy read
    before the flag landed would otherwise write the flag back out."""
    ws = _engagement("Held across adoption")
    _unflag(ws)
    held = ws.reload()
    assert not held.status_store

    workspaces.load_workspace(ws.id)
    held.description = "Edited by a run that started earlier."
    held.save()

    stored = json.loads(ws.definition_path.read_text(encoding="utf-8"))
    assert stored[stage_status.FLAG] is True


def test_with_the_store_off_an_older_workspace_is_left_unflagged(monkeypatch):
    monkeypatch.setattr(stage_status, "ENABLED", False)
    ws = workspaces.create_workspace("Store off")

    assert not workspaces.load_workspace(ws.id).status_store
    stored = json.loads(ws.definition_path.read_text(encoding="utf-8"))
    assert stage_status.FLAG not in stored


def test_the_stored_answer_is_the_sweep():
    ws = _engagement()
    stored = engagement_record.record(ws)
    assert stage_status.lookup(ws.root).current
    assert _states(stored) == _states(_legacy(ws))
    assert stored["refreshing"] is False


def test_a_current_answer_is_read_without_sweeping(monkeypatch):
    ws = _engagement()
    expected = engagement_record.record(ws)
    assert stage_status.lookup(ws.root).current

    def refuse(_workspace):
        raise AssertionError("a current stored answer must not be swept again")

    monkeypatch.setattr(engagement_record, "_sweep", refuse)
    again = engagement_record.record(ws.reload())
    assert _states(again) == _states(expected)


def test_a_save_marks_the_answer_out_of_date_and_the_refresh_catches_up():
    ws = _engagement()
    engagement_record.record(ws)
    ws.add_table("staff.csv", _csv({"STAFF_ID": ["S1", "S2"], "GRADE": ["A", "B"]}))
    assert not stage_status.lookup(ws.root).current

    assert stage_status.wait_idle()
    assert stage_status.lookup(ws.root).current
    assert _states(engagement_record.record(ws.reload())) == _states(_legacy(ws))


def test_an_out_of_date_answer_is_served_as_refreshing(monkeypatch):
    ws = _engagement()
    before = engagement_record.record(ws)
    # Hold the refresher back so the read lands while the answer is stale.
    _hold_refresher(monkeypatch)
    stage_status.mark_dirty(ws.root)

    stale = engagement_record.record(ws.reload())

    assert stale["refreshing"] is True
    assert _states(stale) == _states(before)


def test_without_serve_stale_an_out_of_date_answer_is_swept_in_the_request(monkeypatch):
    ws = _engagement()
    engagement_record.record(ws)
    monkeypatch.setattr(stage_status, "SERVE_STALE", False)
    _hold_refresher(monkeypatch)
    stage_status.mark_dirty(ws.root)

    fresh = engagement_record.record(ws.reload())

    assert fresh["refreshing"] is False
    assert stage_status.lookup(ws.root).current


def test_any_state_changing_request_marks_the_answer_out_of_date(monkeypatch):
    ws = _engagement()
    engagement_record.record(ws)
    _hold_refresher(monkeypatch)
    client = TestClient(create_app())

    # Announced by the middleware, whatever the route itself wrote.
    response = client.post(
        f"/api/workspaces/{ws.id}/engagement/brief", json={"client": "Acme"},
    )

    assert response.status_code == 200
    assert not stage_status.lookup(ws.root).current
    record = client.get(f"/api/workspaces/{ws.id}/engagement/record").json()
    assert record["refreshing"] is True


def test_reading_the_record_does_not_mark_it_out_of_date():
    ws = _engagement()
    client = TestClient(create_app())
    client.get(f"/api/workspaces/{ws.id}/engagement/record")
    assert stage_status.wait_idle()
    generation = json.loads((ws.root / stage_status.DIRNAME / stage_status.FILENAME).read_text())["generation"]

    client.get(f"/api/workspaces/{ws.id}/engagement/record")

    assert stage_status.lookup(ws.root).current
    stored = json.loads((ws.root / stage_status.DIRNAME / stage_status.FILENAME).read_text())
    assert stored["generation"] == generation


def test_a_run_commit_marks_the_answer_out_of_date(monkeypatch):
    ws = _engagement()
    engagement_record.record(ws)
    _hold_refresher(monkeypatch)

    class Host:
        run: dict = {"workflow": {"workspace_revision": ws.revision}}

        def __init__(self, workspace):
            self.ws = workspace

        def emit(self, *_args, **_kwargs):
            pass

    execution_support.refresh_workspace(Host(ws))

    assert not stage_status.lookup(ws.root).current


def test_a_deleted_workspace_is_not_recreated_by_a_late_refresh():
    ws = _engagement()
    root = ws.root
    workspaces.delete_workspace(ws.id)
    stage_status.schedule_refresh(root)
    assert stage_status.wait_idle()
    assert not (root / stage_status.DIRNAME).exists()
