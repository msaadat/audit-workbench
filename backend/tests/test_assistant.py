import json
import urllib.error

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import assistant, llm, workspaces
from app.dashboard import tile_payload
from app.main import create_app
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


@pytest.mark.parametrize(
    "code, match",
    [
        ("import os\nresult = df", "Imports are not allowed"),
        ("result = ().__class__", "not allowed"),
        ("result = open('x')", "not allowed"),
        ("result = eval('1')", "not allowed"),
        ("total = 1", "assigning `result`"),
    ],
)
def test_sandbox_blocks_unsafe_code(code, match):
    with pytest.raises(SandboxError, match=match):
        sandbox_run(code, {"df": pl.DataFrame({"a": [1]})})


def test_sandbox_runtime_error_is_user_facing():
    with pytest.raises(SandboxError, match="ColumnNotFound|nonexistent"):
        sandbox_run("result = df.select(pl.col('nope'))", {"df": pl.DataFrame({"a": [1]})})


# ---------------------------------------------------------------- metadata only
def test_table_metadata_is_aggregate_only(workspace_with_data):
    meta = assistant.table_metadata(workspace_with_data, "transactions")
    assert meta["rows"] == 6
    by_name = {c["name"]: c for c in meta["columns"]}
    # numeric column exposes ranges, not values
    assert set(by_name["amount"]) >= {"nulls_pct", "distinct", "min", "max", "mean"}
    assert "rows" not in by_name["amount"]
    # low-cardinality string exposes its category labels
    assert set(by_name["cust_id"]["values"]) == {"C1", "C2", "C3"}


def test_frame_for_model_withholds_raw_rows():
    raw = pl.DataFrame({"amount": list(range(10))})
    withheld = assistant._frame_for_model(raw, allow_rows=False)
    assert "rows" not in withheld
    assert "note" in withheld
    assert withheld["numeric_summary"]["amount"]["max"] == 9

    agg = pl.DataFrame({"branch": ["A", "B"], "total": [10.0, 20.0]})
    shown = assistant._frame_for_model(agg, allow_rows=True)
    assert shown["rows"] == [["A", 10.0], ["B", 20.0]]


# ------------------------------------------------------------------- tools
def test_query_tool_aggregated_shows_rows_raw_hides(workspace_with_data):
    session = assistant._Session(workspace_with_data)
    agg, artifact = session.query_table(
        {"table": "transactions", "group_by": ["cust_id"], "aggregates": [{"column": "amount", "func": "sum"}]}
    )
    assert artifact["kind"] == "query"
    assert agg["result"]["rows"]  # aggregated → model sees the summary
    assert artifact["total_rows"] == 3

    raw, _ = session.query_table({"table": "transactions"})
    assert "rows" not in raw["result"]  # raw rows withheld from the model


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
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = TestClient(create_app())
    body = client.get("/api/assistant/status").json()
    assert body["configured"] is False
    assert body["backend"] == "groq"
    assert body["model"]


def test_assistant_status_lmstudio_configured_without_cloud_key(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)

    body = llm.status()

    assert body["configured"] is True
    assert body["backend"] == "lmstudio"
    assert body["model"] == ""
    assert body["base_url"] == "http://localhost:1234/v1"


def test_ask_without_key_raises(monkeypatch, workspace_with_data):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
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

    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["headers"]["User-agent"] == llm.USER_AGENT
    assert captured["timeout"] == llm.REQUEST_TIMEOUT


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

    monkeypatch.setenv("LLM_BACKEND", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/test-model")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_APP_TITLE", "Audit Workbench")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://audit-workbench.local")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer router-key"
    assert captured["headers"]["x-openrouter-title"] == "Audit Workbench"
    assert captured["headers"]["http-referer"] == "https://audit-workbench.local"
    assert captured["body"]["model"] == "openai/test-model"


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
        return FakeResponse()

    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    assert llm.chat([{"role": "user", "content": "hello"}]) == {"content": "ok"}
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer lm-studio"
    assert captured["body"]["model"] == ""


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

    monkeypatch.setenv("LLM_BACKEND", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/test-model")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(llm.LLMError) as raised:
        llm.chat([{"role": "user", "content": "hello"}])

    message = str(raised.value)
    assert "Rate limit exceeded (rate_limit_exceeded)" in message
    assert "Retry after 60 seconds" in message
    assert "OPENROUTER_MODEL" in message


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

    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    monkeypatch.setenv("LMSTUDIO_MODEL", "missing-model")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(llm.LLMError) as raised:
        llm.chat([{"role": "user", "content": "hello"}])

    message = str(raised.value)
    assert "Model not found" in message
    assert "LMSTUDIO_MODEL" in message
    assert "missing-model" in message


# --------------------------------------------------------------- full loop (mocked)
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
    # The tool result fed back to the model must be aggregated JSON, no raw dump.
    tool_msg = next(m for m in calls[1] if m.get("role") == "tool")
    assert '"rows"' in tool_msg["content"]  # aggregated summary allowed
    assert "disclosure" in result


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
