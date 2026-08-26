import http.client
import json
import urllib.error

import polars as pl
import pytest

from app import analysis_results, assistant, assistant_settings, documents, llm, tooling, workspaces
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
    # The mutating tools are lent, never registered: a caller that passes no
    # commander must not even see them advertised.
    assert assistant._command_schemas(None) == []
    assert "start_command" not in names


def _commander(**overrides) -> assistant.Commander:
    calls: list = overrides.setdefault("calls", [])

    def launch_command(command_id):
        calls.append(("command", command_id))
        return {"kind": "run_started", "run_id": "20260801-000000-abcdef"}

    def launch_action(request):
        calls.append(("action", request))
        return {"kind": "run_started", "run_id": "20260801-000000-abcdef"}

    return assistant.Commander(
        catalog=({"id": "generate_report", "label": "Generate report", "description": "…"},),
        launch_command=overrides.get("launch_command", launch_command),
        launch_action=overrides.get("launch_action", launch_action),
    )


def test_command_schemas_are_offered_only_when_a_commander_is_lent():
    commander = _commander()
    schemas = assistant._command_schemas(commander)

    assert [item["function"]["name"] for item in schemas] == ["start_command", "start_action"]
    enum = schemas[0]["function"]["parameters"]["properties"]["command_id"]["enum"]
    assert enum == ["generate_report"]
    # The catalog is described to the model, not just enumerated.
    assert "generate_report" in schemas[0]["function"]["description"]


def test_start_command_records_the_run_and_refuses_a_second(workspace_with_data):
    calls: list = []
    session = assistant._Session(
        workspace_with_data, commander=_commander(calls=calls),
    )

    content, artifact = session.dispatch("start_command", {"command_id": "generate_report"})

    assert content["kind"] == "run_started"
    assert artifact is None
    assert session.started_run == content
    assert calls == [("command", "generate_report")]
    # One message, one run: a second call must not silently queue more work.
    with pytest.raises(WorkspaceError, match="already started"):
        session.dispatch("start_command", {"command_id": "generate_report"})
    assert calls == [("command", "generate_report")]


def test_start_command_rejects_an_unregistered_command(workspace_with_data):
    session = assistant._Session(workspace_with_data, commander=_commander())

    with pytest.raises(WorkspaceError, match="Unknown command"):
        session.dispatch("start_command", {"command_id": "delete_everything"})
    assert session.started_run is None


def test_mutating_tools_are_unavailable_without_a_commander(workspace_with_data):
    session = assistant._Session(workspace_with_data)

    with pytest.raises(WorkspaceError, match="cannot change the workspace"):
        session.dispatch("start_command", {"command_id": "generate_report"})
    with pytest.raises(WorkspaceError, match="cannot change the workspace"):
        session.dispatch("start_action", {"request": "delete the Q3 join"})
    assert session.started_run is None


def test_start_action_requires_a_request(workspace_with_data):
    session = assistant._Session(workspace_with_data, commander=_commander())

    with pytest.raises(WorkspaceError, match="needs a request"):
        session.dispatch("start_action", {"request": "  "})


def test_ask_loop_can_start_a_run_and_reports_it(monkeypatch, workspace_with_data):
    """The whole seam: schemas reach the model, the tool call dispatches, and
    the started run comes back out of ``ask``."""

    calls = []
    launched = []

    def fake_chat(messages, tools=None, temperature=0.0):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "start_command",
                        "arguments": json.dumps({"command_id": "generate_report"}),
                    },
                }],
            }
        return {"content": "Started the report."}

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    result = assistant.ask(
        workspace_with_data, "Pull the report together please",
        commander=_commander(calls=launched),
    )

    assert result["answer"] == "Started the report."
    assert result["started_run"] == {"kind": "run_started", "run_id": "20260801-000000-abcdef"}
    assert launched == [("command", "generate_report")]
    offered = [item["function"]["name"] for item in calls[0]["tools"]]
    assert "start_command" in offered and "start_action" in offered
    assert "start a run only when" in calls[0]["messages"][0]["content"].casefold()


def test_ask_stays_read_only_without_a_commander(monkeypatch, workspace_with_data):
    calls = []

    def fake_chat(messages, tools=None, temperature=0.0):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "start_command",
                        "arguments": json.dumps({"command_id": "generate_report"}),
                    },
                }],
            }
        return {"content": "I can't change the workspace."}

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    result = assistant.ask(workspace_with_data, "Generate the report")

    assert result["started_run"] is None
    assert [item["function"]["name"] for item in calls[0]["tools"]] == [
        tool.name for tool in assistant.READ_TOOLS
    ]
    # A model that invents the tool anyway is refused, and told so.
    assert result["steps"][0] == {
        "tool": "start_command", "args": {"command_id": "generate_report"}, "ok": False,
    }
    tool_msg = next(item for item in calls[1]["messages"] if item.get("role") == "tool")
    assert "cannot change the workspace" in tool_msg["content"]


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


def test_analysis_area_reports_recorded_outcomes_without_re_running(workspace_with_data):
    """"What did the analyses find?" is answered by reading, not by computing."""
    ws = workspace_with_data
    analysis = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Duplicate invoices",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
        }
    )
    analysis_results.execute_and_record(ws, analysis["id"])
    before_revision = workspaces.load_workspace(ws.id).revision

    content, artifact = assistant._Session(
        workspaces.load_workspace(ws.id)
    ).inspect_audit_artifacts({"area": "analysis"})

    assert artifact is None
    assert content["total"] == 1
    reported = content["analyses"][0]
    assert reported["title"] == "Duplicate invoices"
    assert reported["state"] == "current"
    assert reported["classification"] in assistant.analysis_results.SUMMARY_CLASSES
    # Bounded: statistics and a verdict, never rows or code.
    assert "rows" not in reported and "code" not in reported and "spec" not in reported
    assert workspaces.load_workspace(ws.id).revision == before_revision
    assert sum(content["counts"].values()) == 1


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


def test_llm_chat_returns_provider_usage_with_the_message(monkeypatch):
    """Usage is a sibling of `choices`, and the budget ledger reads it off the
    returned message.

    Returning the message alone meant every call metered as zero completion
    tokens, so `max_completion_tokens` was checked against a total that could
    never grow — and a provider that genuinely reports no usage looked exactly
    like this plumbing being broken.
    """

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 4962,
                        "completion_tokens": 21148,
                        "completion_tokens_details": {"reasoning_tokens": 19930},
                    },
                }
            ).encode()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    message = llm.chat([{"role": "user", "content": "hello"}])

    assert message["content"] == "ok"
    assert message["usage"]["completion_tokens"] == 21148
    assert message["usage"]["prompt_tokens"] == 4962
    assert message["usage"]["completion_tokens_details"]["reasoning_tokens"] == 19930


def test_llm_chat_omits_usage_when_the_provider_reports_none(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}


def test_llm_chat_reports_why_the_provider_stopped(monkeypatch):
    """`finish_reason` is a sibling of `choices` and the caller needs it.

    Without it an empty completion is indistinguishable from a model that
    replied with nothing to say — and from malformed output, which is what one
    truncated RCM turn was reported as while the real cause was a reasoning
    loop that never reached the answer.
    """

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {"finish_reason": "length", "message": {"content": ""}}
                    ],
                    "usage": {
                        "completion_tokens": 65536,
                        "completion_tokens_details": {"reasoning_tokens": 65090},
                    },
                }
            ).encode()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    message = llm.chat([{"role": "user", "content": "hello"}])

    assert message["finish_reason"] == "length"
    assert message["content"] == ""
    assert message["usage"]["completion_tokens_details"]["reasoning_tokens"] == 65090


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


def test_llm_retries_transient_http_success_provider_error(monkeypatch):
    """A saturated upstream reported in an HTTP 200 body must still retry.

    An aggregator returns the upstream failure in the response body, so the
    transport-level retry never sees it. Without this the first transient
    rate-limit ends the unit that asked for the call — and, because a workflow
    run folds an unsettled unit into its own terminal status, the run with it.
    """
    calls = 0
    transient = json.dumps(
        {
            "error": {
                "code": 502,
                "message": (
                    "Upstream error from Nvidia: ResourceExhausted: Worker "
                    "local total request limit reached (33/32)"
                ),
            }
        }
    ).encode()
    success = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._body

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse(transient if calls == 1 else success)

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda delay: None)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert calls == 2


def test_llm_surfaces_http_success_provider_error_without_retrying(monkeypatch):
    """A deterministic upstream fault is terminal even under a retryable code.

    The aggregator labels an unsupported-schema rejection ``502`` as well, and
    that answer will not change, so retrying it only spends the budget three
    times over.
    """
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
    clock = [0.0]
    sleeps = []

    def advance_clock(delay):
        sleeps.append(delay)
        clock[0] += delay

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
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(llm.time, "sleep", advance_clock)

    with pytest.raises(llm.LLMError) as raised:
        llm.chat([{"role": "user", "content": "hello"}])

    message = str(raised.value)
    assert "Rate limit exceeded (rate_limit_exceeded)" in message
    assert "Retry after 60 seconds" in message
    assert "Assistant settings" in message
    assert sleeps == [60.0, 60.0]


def test_llm_429_pauses_all_new_requests_until_the_cooldown_expires(monkeypatch):
    """A terminal 429 gates the next caller, not only its own retry."""
    clock = [0.0]
    sleeps = []
    calls = 0

    def advance_clock(delay):
        sleeps.append(delay)
        clock[0] += delay

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            error = urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {}, None,
            )
            error.read = lambda: b'{"error":{"message":"slow down"}}'
            raise error
        return FakeResponse()

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.setattr(llm, "MAX_REQUEST_ATTEMPTS", 1)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(llm.time, "sleep", advance_clock)

    with pytest.raises(llm.LLMError, match=r"request failed \(429\)"):
        llm.chat([{"role": "user", "content": "first"}])
    assert llm.chat([{"role": "user", "content": "second"}]) == {"content": "ok"}
    assert calls == 2
    assert sleeps == [60.0]


def test_llm_in_band_429_uses_the_shared_cooldown(monkeypatch):
    """Aggregators sometimes return the rate-limit payload with HTTP 200."""
    clock = [0.0]
    sleeps = []
    calls = 0

    def advance_clock(delay):
        sleeps.append(delay)
        clock[0] += delay

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse(
                {"error": {"code": 429, "message": "Rate limit exceeded"}}
            )
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    assistant_settings.save({"provider": "lmstudio", "model": ""})
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(llm.time, "sleep", advance_clock)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert calls == 2
    assert sleeps == [60.0]


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
