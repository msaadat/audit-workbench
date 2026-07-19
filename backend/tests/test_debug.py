import json
import io
from datetime import datetime, timedelta, timezone
import urllib.error

from fastapi.testclient import TestClient

from app import assistant_settings, debug_service, debug_store, llm
from app.agent import store as agent_store
from app.main import create_app


class FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json", "X-Request-ID": "provider-123", "Set-Cookie": "never"}

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def read(self): return json.dumps(self.payload).encode()


def test_central_llm_trace_is_complete_safe_and_image_bodies_are_referenced(
    workspace_with_data, monkeypatch,
):
    assistant_settings.save({"provider": "groq", "model": "test-model"})
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-key")
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse({
            "id": "provider-response", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "complete answer"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        }),
    )
    image = llm.image_part(b"binary-image", "image/png")
    with debug_store.trace_context(
        workspace_id=workspace_with_data.id, workspace_root=str(workspace_with_data.root),
        run_id="run-1", action_id="action-1", chat_id="chat-1", stage="test.stage",
        purpose="test", document_ids=["doc-1"], image_source_ref="Documents/doc-1.png",
    ):
        message = llm.chat([{"role": "user", "content": [{"type": "text", "text": "inspect"}, image]}])
    assert message["content"] == "complete answer"

    calls = debug_service.list_calls(workspace_with_data)["items"]
    assert len(calls) == 1
    record = debug_service.get_call(workspace_with_data, calls[0]["id"])
    assert record["status"] == "completed"
    assert record["raw_response"]["id"] == "provider-response"
    assert record["normalized_message"]["content"] == "complete answer"
    assert record["usage"]["total_tokens"] == 15
    assert record["finish_reason"] == "stop"
    assert record["attempts"][0]["response_headers"] == {
        "content-type": "application/json", "x-request-id": "provider-123",
    }
    assert record["correlation"]["action_id"] == "action-1"
    image_ref = record["request"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_ref["representation"] == "binary_reference"
    assert image_ref["source_ref"] == "Documents/doc-1.png"
    serialized = json.dumps(record)
    assert "super-secret-key" not in serialized
    assert "Authorization" not in serialized
    assert "binary-image" not in serialized
    assert "Set-Cookie" not in serialized


def test_workspace_and_run_saves_create_deduplicated_transitions_and_graph_revisions(
    workspace_with_data,
):
    workspace_with_data.description = "Changed for debug"
    with debug_store.trace_context(
        workspace_id=workspace_with_data.id, workspace_root=str(workspace_with_data.root),
        trigger="test.workspace_update", run_id="run-debug",
    ):
        workspace_with_data.save()
    transitions = debug_service._transitions(workspace_with_data)
    transition = next(item for item in transitions if item["trigger"] == "test.workspace_update")
    assert "$.description" in transition["changed_paths"]
    assert debug_service.snapshot(workspace_with_data, transition["after_ref"]["sha1"])["payload"]["description"] == "Changed for debug"

    run = agent_store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Inspect controls"}
    )
    run["graph_revision"] = 1
    run["actions"] = [{"id": "action_1", "type": "inspect", "status": "ready", "dependencies": []}]
    with debug_store.trace_context(
        workspace_id=workspace_with_data.id, workspace_root=str(workspace_with_data.root),
        run_id=run["id"], trigger="graph.updated",
    ):
        agent_store.save_run(workspace_with_data, run)
    graphs = debug_service._graph_snapshots(workspace_with_data, run["id"])
    assert [item["revision"] for item in graphs] == [0, 1]
    assert graphs[-1]["actions"][0]["id"] == "action_1"


def test_retry_attempts_retain_http_error_payload_and_delay(workspace_with_data, monkeypatch):
    assistant_settings.save({"provider": "groq", "model": "test-model"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    responses = [
        urllib.error.HTTPError(
            "https://example.test", 429, "rate limited",
            {"Content-Type": "application/json", "Retry-After": "1"},
            io.BytesIO(b'{"error":{"message":"slow down"}}'),
        ),
        FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}),
    ]
    def fake_open(request, timeout):
        response = responses.pop(0)
        if isinstance(response, Exception): raise response
        return response
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(llm.time, "sleep", lambda delay: None)
    with debug_store.trace_context(
        workspace_id=workspace_with_data.id, workspace_root=str(workspace_with_data.root),
        stage="retry.test", purpose="retry_test",
    ):
        assert llm.chat([{"role": "user", "content": "retry"}])["content"] == "ok"
    record = debug_service.get_call(workspace_with_data, debug_service.list_calls(workspace_with_data)["items"][0]["id"])
    assert len(record["attempts"]) == 2
    assert record["attempts"][0]["http_status"] == 429
    assert record["attempts"][0]["error_response"]["error"]["message"] == "slow down"
    assert record["attempts"][0]["retry_delay_ms"] == 1000


def test_timing_metrics_show_parallel_overlap_retry_and_waits():
    base = datetime(2026, 7, 19, tzinfo=timezone.utc)
    iso = lambda seconds: (base + timedelta(seconds=seconds)).isoformat()
    run = {"created": iso(0), "started": iso(1), "finished": iso(12), "actions": [], "plan": {"stages": []}}
    calls = [
        {"id": "a", "started_at": iso(2), "finished_at": iso(8), "duration_ms": 6000, "attempts": [{"retry_delay_ms": 500}]},
        {"id": "b", "started_at": iso(4), "finished_at": iso(10), "duration_ms": 6000, "attempts": []},
    ]
    events = [
        {"type": "run_status", "at": iso(1), "data": {"status": "executing"}},
        {"type": "run_status", "at": iso(8), "data": {"status": "awaiting_approval"}},
        {"type": "run_status", "at": iso(10), "data": {"status": "executing"}},
    ]
    metrics = debug_service.timing_metrics(run, calls, events)
    assert metrics["summed_llm_ms"] == 12000
    assert metrics["llm_wall_union_ms"] == 8000
    assert metrics["overlap_saved_ms"] == 4000
    assert metrics["parallelism_factor"] == 1.5
    assert metrics["approval_wait_ms"] == 2000
    assert metrics["retry_wait_ms"] == 500


def test_debug_apis_support_history_detail_state_and_confirmed_clear(workspace_with_data):
    run = agent_store.new_run(workspace_with_data, "auto")
    client = TestClient(create_app())
    base = f"/api/workspaces/{workspace_with_data.id}/debug"
    assert client.get(f"{base}/overview").status_code == 200
    run_page = client.get(f"{base}/runs").json()
    assert run_page["total"] == 1
    assert run_page["items"][0]["usage"]["llm_turns"] == 0
    detail = client.get(f"{base}/runs/{run['id']}").json()
    assert detail["run"]["schema_version"] == 1
    assert "Schema-v1" in detail["graph_telemetry"]["legacy_notice"]
    assert detail["telemetry_gaps"]
    assert client.get(f"{base}/events?limit=1").status_code == 200
    assert client.delete(base).status_code == 400
    assert client.delete(f"{base}?confirm={workspace_with_data.id}").json() == {"cleared": True}
