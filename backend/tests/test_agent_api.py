import json

import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.agent import runner, store
from app.main import create_app
from conftest import wait_run


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


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
    # Phase 12: the endpoint's exploratory-analysis path is the declared
    # workflow, requested through its registered goal template, not a fixed
    # pipeline. The record is a routed command run like any other.
    assert run["engine"] == "workflow"
    assert run["kind"] == "audit"
    assert run["workflow"]["definition"] == "analysis_workflow_v1"
    assert run["workflow"]["requested_outcomes"] == ["analysis.summarized"]
    assert run["command"]["goal_template"] == "data_analysis"
    assert run["route"]["route"] == "workflow"
    assert run["context"]["objective"] == "revenue completeness"

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
    assert ws.joins and ws.analyses


def test_offline_control_responses_are_durable_before_resume(
    client, ws_id, workspace_with_data
):
    run = store.new_command_run(
        workspace_with_data,
        "permission",
        {"source": "chat", "text": "wait for auditor controls"},
    )
    run["status"] = "awaiting_approval"
    run["approvals"] = [
        {
            "id": "approval-offline",
            "kind": "proposal_approval",
            "status": "pending",
            "items": [{"id": "proposal-1"}],
        }
    ]
    run["interactions"] = [
        {
            "id": "interaction-offline",
            "action_id": "action-1",
            "type": "clarification",
            "status": "pending",
            "prompt": "Which artifact?",
        }
    ]
    store.save_run(workspace_with_data, run)

    approval_response = client.post(
        f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/approvals/approval-offline",
        json={"decisions": [{"item_id": "proposal-1", "action": "approve"}]},
    )
    interaction_response = client.post(
        f"/api/workspaces/{ws_id}/agent/runs/{run['id']}/interactions/interaction-offline/respond",
        json={"text": "Use the working report"},
    )

    assert approval_response.status_code == 200
    assert interaction_response.status_code == 200
    durable = store.load_run(workspace_with_data, run["id"])
    assert durable["status"] == "interrupted"
    assert durable["approvals"][0]["submitted_decisions"] == [
        {"item_id": "proposal-1", "action": "approve"}
    ]
    assert durable["interactions"][0]["submitted_response"] == {
        "text": "Use the working report"
    }
    assert [event["type"] for event in store.read_events(workspace_with_data, run["id"])] == [
        "approval_response_stored",
        "interaction_response_stored",
    ]


def test_continue_endpoint_creates_linked_run_from_next_outcomes(
    client, ws_id, workspace_with_data, fake_agent_llm, monkeypatch
):
    previous = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "run the audit"},
    )
    previous["schema_version"] = 3
    previous["engine"] = store.WORKFLOW_ENGINE
    previous["status"] = "completed_with_open_items"
    previous["finished"] = store.utcnow()
    previous["workflow"] = {
        "requested_outcomes": ["planning.apm_ready"],
        "target_refs": ["workspace:current"],
        "next_outcomes": ["planning.apm_ready"],
        "stages": [],
    }
    store.save_run(workspace_with_data, previous)
    monkeypatch.setattr(runner, "_launch", lambda *_args: None)

    response = client.post(
        f"/api/workspaces/{ws_id}/agent/runs/{previous['id']}/continue"
    )

    assert response.status_code == 200
    continued = store.load_run(workspace_with_data, response.json()["id"])
    assert continued["parent_run_id"] == previous["id"]
    assert continued["schema_version"] == 3
    assert continued["command"]["source"] == "follow_up"
    assert continued["workflow"]["requested_outcomes"] == ["planning.apm_ready"]
    assert continued["workflow"]["target_refs"] == ["workspace:current"]
    assert [event["type"] for event in store.read_events(workspace_with_data, continued["id"])] == [
        "run_status"
    ]


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
    assert {"run_status", "stage_update", "task_update", "summary_ready"} <= types

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

    def slow_definitions(user):
        time.sleep(0.5)
        return fake_agent_llm.DEFAULTS["agent:analysis_definitions"](user)

    fake_agent_llm.overrides["agent:analysis_definitions"] = slow_definitions
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
