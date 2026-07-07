import json

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
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    client = TestClient(create_app())
    body = client.get("/api/assistant/status").json()
    assert body["configured"] is False
    assert body["model"]


def test_ask_without_key_raises(monkeypatch, workspace_with_data):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="not configured"):
        assistant.ask(workspace_with_data, "anything")


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
