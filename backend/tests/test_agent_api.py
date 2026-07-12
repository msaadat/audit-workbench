import json

import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.agent import store
from app.main import create_app
from conftest import wait_run


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def ws_id(workspace_with_data):
    return workspace_with_data.id


def _gate(ws, run_id, decided=frozenset(), timeout=15.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.load_run(ws, run_id)
        if run["status"] in store.TERMINAL_STATUSES:
            return run, None
        pending = [
            a
            for a in run["approvals"]
            if a["status"] == "pending" and a["id"] not in decided
        ]
        if run["status"] == "awaiting_approval" and pending:
            return run, pending[0]
        time.sleep(0.02)
    raise AssertionError("no gate reached")


def test_agent_status_endpoint(client):
    payload = client.get("/api/agent/status").json()
    assert "configured" in payload and "model" in payload


def test_suggest_rules_endpoint(client, ws_id):
    payload = client.get(
        f"/api/workspaces/{ws_id}/tables/transactions/suggest-rules"
    ).json()
    checks = {s["check"] for s in payload["suggestions"]}
    assert "required" in checks
    missing = client.get(f"/api/workspaces/{ws_id}/tables/nope/suggest-rules")
    assert missing.status_code == 404


def test_create_run_requires_llm(client, ws_id, monkeypatch):
    from app import llm

    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})
    response = client.post(f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "auto"})
    assert response.status_code == 503


def test_full_run_over_api(client, ws_id, workspace_with_data, fake_agent_llm):
    created = client.post(
        f"/api/workspaces/{ws_id}/agent/runs",
        json={"mode": "auto", "context": {"objective": "revenue completeness"}},
    )
    assert created.status_code == 200
    run_id = created.json()["id"]
    wait_run(workspace_with_data, run_id)

    run = client.get(f"/api/workspaces/{ws_id}/agent/runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["summary_markdown"]

    listing = client.get(f"/api/workspaces/{ws_id}/agent/runs").json()["runs"]
    assert listing and listing[0]["id"] == run_id

    # 409 when a second run starts while one is active is covered in the
    # runner tests; here confirm a finished run frees the slot.
    again = client.post(f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "auto"})
    assert again.status_code == 200
    wait_run(workspace_with_data, again.json()["id"])


def test_busy_returns_409(client, ws_id, workspace_with_data, fake_agent_llm):
    run = client.post(
        f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "permission"}
    ).json()
    _gate(workspace_with_data, run["id"])
    second = client.post(f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "auto"})
    assert second.status_code == 409
    client.post(f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/cancel")
    wait_run(workspace_with_data, run["id"])


def test_approval_round_trip_over_api(client, ws_id, workspace_with_data, fake_agent_llm):
    run = client.post(
        f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "permission"}
    ).json()
    decided = set()
    while True:
        _state, approval = _gate(workspace_with_data, run["id"], decided)
        if approval is None:
            break
        decided.add(approval["id"])
        response = client.post(
            f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/approvals/{approval['id']}",
            json={
                "decisions": [
                    {"item_id": i["id"], "action": "approve"}
                    for i in approval["items"]
                ]
            },
        )
        assert response.status_code == 200
    done = wait_run(workspace_with_data, run["id"])
    assert done["status"] == "completed"
    ws = workspaces.load_workspace(ws_id)
    assert ws.rulesets and ws.tiles


def test_message_endpoint_steers_and_follows_up(
    client, ws_id, workspace_with_data, fake_agent_llm
):
    run = client.post(
        f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "auto"}
    ).json()
    wait_run(workspace_with_data, run["id"])
    response = client.post(
        f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/messages",
        json={"content": "now check round numbers"},
    ).json()
    assert response["handled"] == "follow_up_run"
    follow_id = response["run"]["id"]
    assert response["run"]["parent_run_id"] == run["id"]
    wait_run(workspace_with_data, follow_id)


def test_sse_replays_events_with_cursor(client, ws_id, workspace_with_data, fake_agent_llm):
    run = client.post(
        f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "auto"}
    ).json()
    wait_run(workspace_with_data, run["id"])

    events = []
    with client.stream(
        "GET", f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/events?cursor=0"
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: ") and not line.startswith("data: {}"):
                events.append(json.loads(line[len("data: "):]))
            if line.startswith("event: stream_end"):
                break
    assert events
    assert events[0]["seq"] == 1
    types = {e["type"] for e in events}
    assert {"run_status", "plan_update", "summary_ready"} <= types

    # Cursor replay: ask for events after the midpoint only.
    midpoint = events[len(events) // 2]["seq"]
    tail = []
    with client.stream(
        "GET",
        f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/events?cursor={midpoint}",
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data: ") and not line.startswith("data: {}"):
                tail.append(json.loads(line[len("data: "):]))
            if line.startswith("event: stream_end"):
                break
    assert tail and tail[0]["seq"] == midpoint + 1


def test_pause_and_resume_over_api(client, ws_id, workspace_with_data, fake_agent_llm):
    import time

    def slow_planning(_user):
        time.sleep(0.5)
        return fake_agent_llm.DEFAULTS["agent:planning"]

    fake_agent_llm.overrides["agent:planning"] = slow_planning
    run = client.post(
        f"/api/workspaces/{ws_id}/agent/runs", json={"mode": "auto"}
    ).json()
    assert (
        client.post(
            f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/pause"
        ).status_code
        == 200
    )
    paused = wait_run(
        workspace_with_data, run["id"],
        statuses=("paused", *store.TERMINAL_STATUSES),
    )
    if paused["status"] == "paused":  # raced completion is fine; else resume
        assert (
            client.post(
                f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/resume"
            ).status_code
            == 200
        )
    done = wait_run(workspace_with_data, run["id"])
    assert done["status"] == "completed"
