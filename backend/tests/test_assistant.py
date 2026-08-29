import http.client
import json
import urllib.error

import polars as pl
import pytest

from app import analysis_results, assistant, assistant_settings, documents, llm, tooling, workspaces
from app.agent import store
from app.analysis_payloads import compute_payload
from app.routes import assistant_routes
from app.sandbox import SandboxError, run as sandbox_run
from app.workspaces import WorkspaceError


def _admin_request():
    """The minimum a route needs: a request whose state names an admin."""
    from types import SimpleNamespace

    from app.auth import Principal

    return SimpleNamespace(
        state=SimpleNamespace(principal=Principal(user_id="local", is_admin=True))
    )


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


# ------------------------------------------------------------ python analyses
def test_python_analysis_computes_live(workspace_with_data):
    ws = workspace_with_data
    analysis = ws.add_analysis(
        {
            "kind": "python",
            "title": "Totals by customer",
            "spec": {"code": "result = transactions.group_by('cust_id').agg(pl.col('amount').sum())"},
        }
    )
    assert analysis["table"] is None  # python analyses need no bound table
    payload = compute_payload(ws, analysis)
    assert payload["error"] is None
    assert payload["frame"]["columns"] == ["cust_id", "amount"]
    assert payload["code"].startswith("result =")


def test_python_analysis_requires_code(workspace_with_data):
    with pytest.raises(WorkspaceError, match="needs code"):
        workspace_with_data.add_analysis({"kind": "python", "title": "Empty", "spec": {}})


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

    # Assistant configuration is administrator-only, so the route needs a
    # request carrying an admin principal.
    body = await assistant_routes.assistant_settings(
        {"provider": "mistral", "model": "mistral-small-latest"},
        request=_admin_request(),
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


def _capturing_transport(monkeypatch, sent: dict):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ).encode()

    def _capture(request, timeout):
        sent.clear()
        sent.update(json.loads(request.data.decode()))
        return FakeResponse()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", _capture)


def test_no_temperature_is_sent_when_none_is_configured(monkeypatch):
    """Absent is a different request from zero, and it is now the default.

    An omitted sampling parameter is forwarded as omitted, so the model answers
    at its own vendor's default — which for one model in use here is 1.0, where
    this code used to assert 0.0 over every call it made.
    """

    sent: dict = {}
    _capturing_transport(monkeypatch, sent)

    llm.chat([{"role": "user", "content": "hello"}])

    assert "temperature" not in sent
    assert llm.configured_temperature() is None


def test_a_configured_temperature_is_sent_and_can_be_cleared(monkeypatch):
    sent: dict = {}
    _capturing_transport(monkeypatch, sent)

    assistant_settings.save({"temperature": 0.7})
    assert llm.configured_temperature() == 0.7
    llm.chat([{"role": "user", "content": "hello"}])
    assert sent["temperature"] == 0.7

    # Explicit null is how a setting returns to "let the model decide"; the key
    # being absent from `changes` must instead leave it alone.
    assistant_settings.save({"model": "llama-3.1-8b-instant"})
    assert llm.configured_temperature() == 0.7

    assistant_settings.save({"temperature": None})
    assert llm.configured_temperature() is None
    llm.chat([{"role": "user", "content": "hello"}])
    assert "temperature" not in sent


def test_the_environment_overrides_a_stored_temperature(monkeypatch):
    assistant_settings.save({"temperature": 0.2})
    monkeypatch.setenv("LLM_TEMPERATURE", "1.0")

    assert llm.configured_temperature() == 1.0


@pytest.mark.parametrize("value", ["hot", -0.5, 2.5])
def test_an_out_of_range_temperature_is_refused(value):
    with pytest.raises(assistant_settings.SettingsError, match="[Tt]emperature"):
        assistant_settings.save({"temperature": value})


def test_a_caller_may_still_override_the_configured_temperature(monkeypatch):
    """The dashboard and the agent defer to the setting; a caller may not have to."""

    sent: dict = {}
    _capturing_transport(monkeypatch, sent)
    assistant_settings.save({"temperature": 0.7})

    llm.chat([{"role": "user", "content": "hello"}], temperature=0.0)

    assert sent["temperature"] == 0.0


def test_llm_chat_asks_for_an_explicit_output_ceiling(monkeypatch):
    """Every request carries one, so the room to answer is not routing luck.

    Unset, the ceiling is the routed provider's own default, and those differ
    by more than an order of magnitude for a single model — an answer that fits
    on one provider is truncated on another for no reason the run can see.
    """

    sent: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ).encode()

    def _capture(request, timeout):
        sent.update(json.loads(request.data.decode()))
        return FakeResponse()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", _capture)

    llm.chat([{"role": "user", "content": "hello"}])

    assert sent["max_tokens"] == llm.MAX_OUTPUT_TOKENS
    # Far above the largest legitimate completion this pipeline has produced,
    # because truncating real work costs a whole run and a runaway costs cents.
    assert llm.MAX_OUTPUT_TOKENS >= 65_536


def test_llm_output_ceiling_is_configurable_and_validated(monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "8192")
    assert llm._max_output_tokens() == 8192

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "0")
    with pytest.raises(llm.LLMError, match="positive integer"):
        llm._max_output_tokens()

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "lots")
    with pytest.raises(llm.LLMError, match="positive integer"):
        llm._max_output_tokens()


def _sse(*frames: dict) -> list[bytes]:
    return [f"data: {json.dumps(frame)}\n".encode() for frame in frames] + [b"data: [DONE]\n"]


def test_llm_chat_asks_the_provider_for_its_reasoning(monkeypatch):
    """Billed either way, so not asking only buys a blind spot."""

    sent: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ).encode()

    def _capture(request, timeout):
        sent.update(json.loads(request.data.decode()))
        return FakeResponse()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", _capture)

    llm.chat([{"role": "user", "content": "hello"}])

    assert sent["include_reasoning"] is True


def test_llm_reasoning_can_be_switched_off_for_the_whole_request(monkeypatch):
    """Off is non-thinking mode, not a hidden trace: the tokens go unspent."""

    monkeypatch.setenv("LLM_REASONING", "off")
    assert llm._reasoning_parameters() == {"reasoning": {"enabled": False}}
    # Asking for the trace back would contradict asking for no trace at all.
    assert "include_reasoning" not in llm._reasoning_parameters()

    monkeypatch.setenv("LLM_REASONING", "on")
    assert llm._reasoning_parameters() == {"include_reasoning": True}

    monkeypatch.setenv("LLM_REASONING", "low")
    assert llm._reasoning_parameters() == {
        "reasoning": {"max_tokens": llm.REASONING_EFFORT_TOKENS["low"]}
    }
    # An effort implies reasoning is on, so it does not also ask for the trace.
    assert "include_reasoning" not in llm._reasoning_parameters()

    # Every effort is an absolute budget, well under the output ceiling. A
    # provider's own effort levels are a *share* of that ceiling, and this one
    # is enormous on purpose: "medium" against it let one test-generation call
    # spend 160,880 tokens deliberating against a 131,072 limit and return
    # nothing at all.
    for effort in llm.REASONING_EFFORTS:
        monkeypatch.setenv("LLM_REASONING", effort)
        sent = llm._reasoning_parameters()["reasoning"]
        assert sent["max_tokens"] == llm.REASONING_EFFORT_TOKENS[effort]
        assert sent["max_tokens"] < llm.MAX_OUTPUT_TOKENS
        # The provider refuses a request carrying both, so a budget is
        # expressed one way only: "Only one of reasoning.effort and
        # reasoning.max_tokens can be specified".
        assert "effort" not in sent

    monkeypatch.setenv("LLM_REASONING", "4096")
    assert llm._reasoning_parameters() == {"reasoning": {"max_tokens": 4096}}

    monkeypatch.setenv("LLM_REASONING", "quietly")
    with pytest.raises(llm.LLMError, match="must be"):
        llm._reasoning_parameters()


def test_streamed_reasoning_is_kept_for_the_record_and_out_of_the_answer():
    """It reaches the debug payload, and neither the text nor the reader."""

    shown: list[str] = []
    payload, raw = llm._read_stream(
        _sse(
            {"choices": [{"delta": {"role": "assistant", "reasoning": "weighing "}}]},
            {"choices": [{"delta": {"reasoning": "the options"}}]},
            {"choices": [{"delta": {"content": "the answer"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ),
        shown.append,
    )

    message = payload["choices"][0]["message"]
    assert message["reasoning"] == "weighing the options"
    # The answer is the answer: deliberation is not spliced into it, and a
    # reader following the turn is never shown the model thinking out loud.
    assert message["content"] == "the answer"
    assert shown == ["the answer"]
    assert raw == b"the answer"


def test_a_streamed_runaway_is_still_an_empty_completion():
    """The shape that matters: all reasoning, no answer.

    Capturing the trace must not disturb how a runaway is recognised. If
    reasoning counted as content, a model that deliberated to its limit and
    answered nothing would read as a model that answered at length.
    """

    payload, _ = llm._read_stream(
        _sse(
            {"choices": [{"delta": {"role": "assistant", "reasoning": "round and "}}]},
            {"choices": [{"delta": {"reasoning": "round it goes"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ),
        lambda piece: None,
    )

    assert payload["choices"][0]["message"]["content"] == ""
    assert payload["choices"][0]["finish_reason"] == "length"
    assert payload["choices"][0]["message"]["reasoning"] == "round and round it goes"


def test_llm_chat_does_not_hand_reasoning_back_to_its_caller(monkeypatch):
    """Durable in the debug record, absent from what flows into run state."""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": "ok",
                                "reasoning": "a" * 5000,
                                "reasoning_details": [{"text": "b" * 5000}],
                            },
                        }
                    ]
                }
            ).encode()

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    message = llm.chat([{"role": "user", "content": "hello"}])

    assert message["content"] == "ok"
    assert not set(message) & set(llm.REASONING_KEYS)


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
