import http.client
import json
import urllib.error

import polars as pl
import pytest

from app import assistant, assistant_settings, documents, llm, tooling, workspaces
from app.agent import store
from app.dashboard import tile_payload
from app.routes import assistant_routes
from app.sandbox import SandboxError, run as sandbox_run
from app.workspaces import WorkspaceError


# ------------------------------------------------------------------- sandbox
def test_sandbox_runs_polars_and_captures_stdout():
    frames = {"transactions": pl.DataFrame({"amount": [1, 2, 3]})}
    result, stdout = sandbox_run(
        "print('hi')\nresult = transactions.select(pl.col('amount').sum())", frames
    )
    assert result.item() == 6
    assert "hi" in stdout


def test_sandbox_exposes_tables_mapping_and_df():
    frames = {"transactions": pl.DataFrame({"a": [1, 2]})}
    result, _ = sandbox_run("result = tables['transactions'].height + df.height", frames)
    assert result.item() == 4


def test_sandbox_collects_lazyframe_result():
    frames = {"transactions": pl.DataFrame({"amount": [1, 2, 3]})}
    result, _ = sandbox_run(
        "result = transactions.lazy().select(pl.col('amount').sum())", frames
    )
    assert result.item() == 6


@pytest.mark.parametrize(
    "code, match",
    [
        ("import os\nresult = df", "Imports are not allowed"),
        ("result = ().__class__", "not allowed"),
        ("result = open('x')", "not allowed"),
        ("result = eval('1')", "not allowed"),
        ("result = pl.read_parquet('x.parquet')", "File I/O is not allowed"),
        ("df.write_parquet('x.parquet')\nresult = df", "File I/O is not allowed"),
        ("total = 1", "assigning `result`"),
    ],
)
def test_sandbox_blocks_unsafe_code(code, match):
    with pytest.raises(SandboxError, match=match):
        sandbox_run(code, {"df": pl.DataFrame({"a": [1]})})


def test_sandbox_runtime_error_is_user_facing():
    with pytest.raises(SandboxError, match="ColumnNotFound|nonexistent"):
        sandbox_run("result = df.select(pl.col('nope'))", {"df": pl.DataFrame({"a": [1]})})


# -------------------------------------------------------------- model context
def test_table_metadata_includes_unmasked_low_cardinality_values(workspace_with_data):
    meta = assistant.table_metadata(workspace_with_data, "transactions")
    assert meta["rows"] == 6
    by_name = {c["name"]: c for c in meta["columns"]}
    assert set(by_name["amount"]) >= {"nulls_pct", "distinct", "min", "max", "mean"}
    assert "rows" not in by_name["amount"]
    assert set(by_name["cust_id"]["values"]) == {"C1", "C2", "C3"}


def test_frame_for_model_returns_bounded_unmasked_rows():
    raw = pl.DataFrame({"amount": list(range(10))})
    shown = assistant._frame_for_model(raw)
    assert shown["rows"] == [[value] for value in range(10)]
    assert shown["truncated"] is False
    assert shown["numeric_summary"]["amount"]["max"] == 9

    agg = pl.DataFrame({"branch": ["A", "B"], "total": [10.0, 20.0]})
    preview = assistant._frame_for_model(agg)
    assert preview["rows"] == [["A", 10.0], ["B", 20.0]]


# ------------------------------------------------------------------- tools
def test_read_tools_are_registered_once_with_matching_handlers():
    names = [tool.name for tool in assistant.READ_TOOLS]

    assert len(names) == len(set(names))
    assert set(names) == set(assistant.READ_TOOL_REGISTRY)
    assert {
        "get_table_schemas",
        "get_table_profile",
        "get_audit_progress",
        "get_latest_run",
        "inspect_audit_artifacts",
        "search_documents",
        "list_tables",
        "describe_table",
        "query_table",
        "run_analytics",
        "run_python",
    } == set(names)
    assert [schema["function"]["name"] for schema in assistant.TOOLS] == names
    assert tooling.TABLE_SCHEMAS_TOOL in assistant.TOOLS
    assert tooling.TABLE_PROFILE_TOOL in assistant.TOOLS


def test_latest_run_tool_prefers_the_current_chat(workspace_with_data):
    other = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Other chat", "chat_id": "chat_other"},
    )
    other["status"] = "completed"
    store.save_run(workspace_with_data, other)

    linked = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Generate the APM", "chat_id": "chat_current"},
    )
    linked["status"] = "completed"
    linked["messages"] = [
        {"role": "agent", "content": "Done — audit planning memorandum."}
    ]
    linked["artifacts"] = [
        {
            "kind": "planning",
            "id": "apm",
            "semantic_id": "planning:apm",
            "action": "updated",
        }
    ]
    store.save_run(workspace_with_data, linked)

    session = assistant._Session(workspace_with_data, chat_id="chat_current")
    content, artifact = session.get_latest_run({})

    assert artifact is None
    assert content["scope"] == "chat"
    assert content["run"]["id"] == linked["id"]
    assert content["run"]["command"] == "Generate the APM"
    assert content["run"]["closing_message"] == "Done — audit planning memorandum."
    assert content["run"]["artifacts"][0]["semantic_id"] == "planning:apm"


def test_audit_progress_tool_exposes_read_only_lifecycle_and_run_context(
    workspace_with_data,
):
    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Prepare planning", "chat_id": "chat_progress"},
    )
    run["status"] = "completed"
    store.save_run(workspace_with_data, run)

    session = assistant._Session(workspace_with_data, chat_id="chat_progress")
    content, artifact = session.get_audit_progress({})

    assert artifact is None
    assert content["workspace"]["available_domains"]
    assert content["latest_run"]["id"] == run["id"]
    by_capability = {
        item["capability"]: item for item in content["lifecycle"]
    }
    assert "planning.apm_ready" in by_capability
    assert "state" in by_capability["planning.apm_ready"]


def test_audit_artifact_tool_is_bounded_and_non_mutating(workspace_with_data):
    workspace_with_data.planning["context"]["objective"] = "Review procurement"
    workspace_with_data.planning["apm_markdown"] = "# APM\n\nPlanning basis."
    before_revision = workspace_with_data.revision

    content, artifact = assistant._Session(workspace_with_data).inspect_audit_artifacts(
        {"area": "planning"}
    )

    assert artifact is None
    assert content["context"]["objective"] == "Review procurement"
    assert content["apm"]["excerpt"].startswith("# APM")
    assert workspace_with_data.revision == before_revision


def test_query_tool_shows_bounded_rows_for_aggregated_and_raw_results(workspace_with_data):
    session = assistant._Session(workspace_with_data)
    agg, artifact = session.query_table(
        {"table": "transactions", "group_by": ["cust_id"], "aggregates": [{"column": "amount", "func": "sum"}]}
    )
    assert artifact["kind"] == "query"
    assert agg["result"]["rows"]
    assert artifact["total_rows"] == 3

    raw, _ = session.query_table({"table": "transactions"})
    assert raw["result"]["rows"]
    assert raw["result"]["rows"][0][0] == 1001


def test_analytics_tool_returns_verdict_and_artifact(workspace_with_data):
    session = assistant._Session(workspace_with_data)
    content, artifact = session.run_analytics(
        {"table": "transactions", "test": "duplicates", "params": {"columns": ["invoice_no"]}}
    )
    assert content["verdict"] == "fail"
    assert artifact["kind"] == "analytics"
    assert artifact["spec"] == {"test": "duplicates", "params": {"columns": ["invoice_no"]}}


def test_python_tool_makes_pinnable_artifact(workspace_with_data):
    session = assistant._Session(workspace_with_data)
    content, artifact = session.run_python(
        {"code": "result = transactions.group_by('cust_id').agg(pl.col('amount').sum())"}
    )
    assert artifact["kind"] == "python"
    assert artifact["spec"]["code"].startswith("result =")
    assert content["result"]["rows"]  # 3 rows ≤ small-result cap → visible


# --------------------------------------------------------------- python tiles
def test_python_tile_computes_live(workspace_with_data):
    ws = workspace_with_data
    tile = ws.add_tile(
        {
            "kind": "python",
            "title": "Totals by customer",
            "spec": {"code": "result = transactions.group_by('cust_id').agg(pl.col('amount').sum())"},
        }
    )
    assert tile["table"] is None  # python tiles need no bound table
    payload = tile_payload(ws, tile)
    assert payload["error"] is None
    assert payload["frame"]["columns"] == ["cust_id", "amount"]
    assert payload["code"].startswith("result =")


def test_python_tile_requires_code(workspace_with_data):
    with pytest.raises(WorkspaceError, match="needs code"):
        workspace_with_data.add_tile({"kind": "python", "title": "Empty", "spec": {}})


# ----------------------------------------------------------------- llm status
def test_assistant_status_unconfigured(monkeypatch):
    assistant_settings.save({"provider": "mistral", "model": "mistral-small-latest"})
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    body = llm.status()
    assert body["configured"] is False
    assert body["backend"] == "mistral"
    assert body["model"]
    assert any(provider["id"] == "mistral" for provider in body["providers"])
    opencode = next(provider for provider in body["providers"] if provider["id"] == "opencode")
    assert opencode["label"] == "OpenCode Zen"
    assert opencode["default_model"] == "deepseek-v4-flash-free"
    cerebras = next(provider for provider in body["providers"] if provider["id"] == "cerebras")
    assert cerebras["label"] == "Cerebras"
    assert cerebras["default_model"] == "gpt-oss-120b"


def test_assistant_status_lmstudio_configured_without_cloud_key(monkeypatch):
    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)

    body = llm.status()

    assert body["configured"] is True
    assert body["backend"] == "lmstudio"
    assert body["model"] == ""
    assert body["base_url"] == "http://localhost:1234/v1"


@pytest.mark.anyio
async def test_assistant_settings_endpoint_persists_provider_model(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key")

    body = await assistant_routes.assistant_settings(
        {"provider": "mistral", "model": "mistral-small-latest"}
    )

    assert body["configured"] is True
    assert body["backend"] == "mistral"
    assert body["model"] == "mistral-small-latest"
    assert body["base_url"] == "https://api.mistral.ai/v1"
    assert assistant_settings.load() == {
        "provider": "mistral",
        "model": "mistral-small-latest",
    }


def test_ask_without_key_raises(monkeypatch, workspace_with_data):
    # Keep this provider-agnostic as the supported profile list grows.
    for provider in assistant_settings.PROVIDERS.values():
        monkeypatch.delenv(str(provider["api_key_env"]), raising=False)
    monkeypatch.setenv("LLM_BACKEND", "mistral")
    monkeypatch.setattr(assistant_settings, "DEFAULT_PROVIDER", "mistral")
    monkeypatch.setattr(assistant_settings, "DEFAULT_SETTINGS", {
        "provider": "mistral", "model": assistant_settings.PROVIDERS["mistral"]["default_model"],
    })
    with pytest.raises(llm.LLMError, match="not configured"):
        assistant.ask(workspace_with_data, "anything")


def test_llm_chat_sends_user_agent(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["headers"]["User-agent"] == llm.USER_AGENT
    assert captured["timeout"] == llm.REQUEST_TIMEOUT


def test_llm_chat_wraps_remote_disconnect(monkeypatch):
    def fake_urlopen(request, timeout):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(llm.LLMError, match="Remote end closed connection"):
        llm.chat([{"role": "user", "content": "hello"}])


def test_llm_chat_supports_openrouter(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    assistant_settings.save({"provider": "openrouter", "model": "openai/test-model"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer router-key"
    assert captured["body"]["model"] == "openai/test-model"


def test_llm_chat_supports_mistral(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    assistant_settings.save({"provider": "mistral", "model": "mistral-large-latest"})
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer mistral-key"
    assert captured["body"]["model"] == "mistral-large-latest"


def test_llm_chat_supports_opencode_zen_with_default_model(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    assistant_settings.save({"provider": "opencode", "model": ""})
    monkeypatch.setenv("OPENCODE_API_KEY", "zen-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["url"] == "https://opencode.ai/zen/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer zen-key"
    assert captured["body"]["model"] == "deepseek-v4-flash-free"


def test_llm_chat_supports_cerebras_with_default_model(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    assistant_settings.save({"provider": "cerebras", "model": ""})
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["url"] == "https://api.cerebras.ai/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer cerebras-key"
    assert captured["body"]["model"] == "gpt-oss-120b"


def test_llm_chat_supports_lmstudio(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer lm-studio"
    assert captured["body"]["model"] == ""
    assert captured["timeout"] == llm.LOCAL_REQUEST_TIMEOUT


def test_llm_request_timeout_can_be_overridden(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        return FakeResponse()

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "900")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["timeout"] == 900


def test_llm_retries_empty_choices_then_succeeds(monkeypatch):
    responses = [
        {"choices": []},
        {"choices": [{"message": {"content": "ok"}}]},
    ]

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse(responses.pop(0)),
    )
    monkeypatch.setattr(llm.time, "sleep", lambda delay: None)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert responses == []


def test_llm_empty_choices_exhaustion_is_explicit(monkeypatch):
    calls = 0

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[]}'

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse()

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda delay: None)

    with pytest.raises(llm.LLMError, match="after 3 attempts"):
        llm.chat([{"role": "user", "content": "hello"}])
    assert calls == llm.MAX_REQUEST_ATTEMPTS


def test_llm_surfaces_http_success_provider_error_without_retrying(monkeypatch):
    calls = 0

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "error": {
                        "code": 502,
                        "message": (
                            "Upstream error from Nvidia: ValueError: The provided "
                            "JSON schema contains features not supported by xgrammar."
                        ),
                    }
                }
            ).encode()

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse()

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(
        llm.LLMError,
        match=r"request failed \(502\).*not supported by xgrammar",
    ):
        llm.chat([{"role": "user", "content": "hello"}])
    assert calls == 1


def test_llm_error_detail_keeps_plain_text_body():
    error = urllib.error.HTTPError(
        url="https://example.test",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=None,
    )
    error.read = lambda: b"error code: 1010"

    assert llm._error_detail(error) == "error code: 1010"


def test_openrouter_rate_limit_error_includes_retry_hint(monkeypatch):
    def fake_urlopen(request, timeout):
        error = urllib.error.HTTPError(
            url=request.full_url,
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "60"},
            fp=None,
        )
        error.read = lambda: json.dumps(
            {
                "error": {
                    "code": 429,
                    "message": "Rate limit exceeded",
                    "metadata": {"error_type": "rate_limit_exceeded"},
                }
            }
        ).encode()
        raise error

    assistant_settings.save({"provider": "openrouter", "model": "openai/test-model"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda delay: None)

    with pytest.raises(llm.LLMError) as raised:
        llm.chat([{"role": "user", "content": "hello"}])

    message = str(raised.value)
    assert "Rate limit exceeded (rate_limit_exceeded)" in message
    assert "Retry after 60 seconds" in message
    assert "Assistant settings" in message


def test_lmstudio_model_error_includes_local_hint(monkeypatch):
    def fake_urlopen(request, timeout):
        error = urllib.error.HTTPError(
            url=request.full_url,
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        error.read = lambda: json.dumps(
            {"error": {"code": 404, "message": "Model not found"}}
        ).encode()
        raise error

    assistant_settings.save({"provider": "lmstudio", "model": "missing-model"})
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(llm.LLMError) as raised:
        llm.chat([{"role": "user", "content": "hello"}])

    message = str(raised.value)
    assert "Model not found" in message
    assert "Assistant settings" in message
    assert "missing-model" in message


# --------------------------------------------------------------- full loop (mocked)
def test_ask_coordinator_can_select_audit_progress_without_data_bias(
    monkeypatch, workspace_with_data,
):
    calls = []

    def fake_chat(messages, tools=None, temperature=0.0):
        calls.append({"messages": list(messages), "tools": tools})
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "progress",
                        "type": "function",
                        "function": {
                            "name": "get_audit_progress",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        return {"content": "The planning memorandum is complete; review the remaining lifecycle work."}

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    result = assistant.ask(
        workspace_with_data,
        "What's the next task to be done?",
        chat_id="chat_current",
    )

    assert result["steps"] == [
        {"tool": "get_audit_progress", "args": {}, "ok": True}
    ]
    assert "planning memorandum" in result["answer"]
    system = calls[0]["messages"][0]["content"]
    assert "read-only audit assistant" in system
    assert "audit data-analysis assistant" not in system
    assert "get_audit_progress" in {
        item["function"]["name"] for item in calls[0]["tools"]
    }
    # Detailed columns are now discovered through list_tables, not injected
    # into every audit, document, planning, or reporting question.
    assert "invoice_no" not in system


def test_ask_runs_tool_loop(monkeypatch, workspace_with_data):
    calls = []

    def fake_chat(messages, tools=None, temperature=0.0):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "query_table",
                            "arguments": json.dumps(
                                {
                                    "table": "transactions",
                                    "group_by": ["cust_id"],
                                    "aggregates": [{"column": "amount", "func": "sum"}],
                                    "sort": [{"column": "amount_sum", "desc": True}],
                                }
                            ),
                        },
                    }
                ],
            }
        return {"content": "C2 has the highest total spend."}

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    result = assistant.ask(workspace_with_data, "Who has the highest total spend?")

    assert result["answer"] == "C2 has the highest total spend."
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["kind"] == "query"
    assert result["steps"][0]["tool"] == "query_table"
    assert result["steps"][0]["ok"] is True
    # The tool result fed back to the model includes the bounded result preview.
    tool_msg = next(m for m in calls[1] if m.get("role") == "tool")
    assert '"rows"' in tool_msg["content"]


def test_ask_reports_tool_error_to_model(monkeypatch, workspace_with_data):
    def fake_chat(messages, tools=None, temperature=0.0):
        # Always ask for a broken query; loop should feed the error back, then
        # we stop it by returning a final answer on the second call.
        if not any(m.get("role") == "tool" for m in messages):
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c",
                        "function": {"name": "query_table",
                                     "arguments": json.dumps({"table": "ghost"})},
                    }
                ],
            }
        return {"content": "That table does not exist."}

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    result = assistant.ask(workspace_with_data, "query the ghost table")
    assert result["steps"][0]["ok"] is False
    assert result["answer"] == "That table does not exist."


def test_ask_handles_non_object_tool_arguments(monkeypatch, workspace_with_data):
    def fake_chat(messages, tools=None, temperature=0.0):
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "content": "",
                "tool_calls": [{
                    "id": "bad-args",
                    "function": {
                        "name": "query_table",
                        "arguments": ["transactions"],
                    },
                }],
            }
        return {"content": "The tool arguments were invalid."}

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    result = assistant.ask(workspace_with_data, "Run the malformed query")

    assert result["steps"][0]["args"] == {}
    assert result["steps"][0]["ok"] is False
    assert result["answer"] == "The tool arguments were invalid."


def test_ask_with_documents_includes_context_and_returns_validated_citation(
    monkeypatch, workspace_with_data,
):
    doc = documents.add_document(
        workspace_with_data,
        "approval-policy.txt",
        b"The finance director approves invoices before payment.",
    )
    calls = []

    def fake_chat(messages, tools=None, temperature=0.0):
        calls.append(messages)
        return {"content": json.dumps({
            "answer": "The finance director approves invoices.",
            "citations": [{
                "document_id": doc["id"], "page": 1,
                "excerpt": "The finance director approves invoices before payment.",
            }],
        })}

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    monkeypatch.setattr(assistant.llm, "status", lambda: {
        "configured": True, "provider": "fake", "model": "fake",
    })
    result = assistant.ask(workspace_with_data, "Who approves invoices?", [doc["id"]])

    assert result["answer"] == "The finance director approves invoices."
    assert result["citations"][0]["source_id"] == doc["id"]
    assert result["document_context"]["trimmed"] is False
    assert "finance director" in calls[0][0]["content"]
    activity = documents.activities(workspace_with_data)["items"][0]
    assert activity["document_ids"] == [doc["id"]]
    assert "finance director" not in json.dumps(activity)


def test_ask_with_documents_requires_no_workspace_setting(monkeypatch, workspace_with_data):
    doc = documents.add_document(workspace_with_data, "local.txt", b"Always available")
    monkeypatch.setattr(assistant.llm, "chat", lambda *args, **kwargs: {
        "content": json.dumps({"answer": "Always available", "citations": []}),
    })
    result = assistant.ask(workspace_with_data, "What does it say?", [doc["id"]])
    assert result["answer"] == "Always available"
