import json
import sys
import time
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import assistant_settings, llm, loader, workspaces  # noqa: E402
from app.agent import runner as agent_runner  # noqa: E402
from app.agent import store as agent_store  # noqa: E402


def _analysis_definitions_response(user: str) -> dict:
    """Answer for whichever frame the analysis-definitions worker was given."""
    payload = json.loads(user.split("\n\nYour previous response")[0])
    frame = payload["TARGET FRAME"]
    table = frame["table"]
    columns = [column["name"] for column in frame["columns"]]
    key = "invoice_no" if "invoice_no" in columns else columns[0]
    return {
        "analyses": [
            {
                "title": f"Duplicates in {table}",
                "kind": "analytics",
                "spec": {"test": "duplicates", "params": {"columns": [key]}},
                "note": "Reused keys signal double postings.",
            },
            {
                "title": f"Preview {table}",
                "kind": "python",
                "spec": {"code": f"result = tables['{table}'].head(3)"},
            },
        ]
    }


def _analysis_summary_response(_user: str) -> dict:
    """A structurally valid EDA memo for any workspace.

    Returned as a ``content`` message because this worker answers in Markdown;
    anything else is JSON-encoded and arrives as one quoted line. It cites no
    procedures on purpose — embeds are optional, and a default that named an id
    could cite one the bundle never supplied, which the validator rejects.
    """
    return {
        "content": (
            "## Data received and population characteristics\n"
            "The imported tables were profiled.\n"
            "\n## Relationships and joins established\nRelationships were diagnosed.\n"
            "\n## Procedures performed\nThe saved procedures were executed.\n"
            "\n## Exceptions noted\nNothing requiring escalation.\n"
            "\n## Data quality observations\nNo material gaps.\n"
            "\n## Further work required\nNothing outstanding.\n"
        )
    }


def _document_analysis_response(user: str) -> dict:
    source = user.split("RAW SOURCE CHUNK:\n", 1)[-1].strip()
    page = int(user.split("\nPAGE: ", 1)[1].splitlines()[0])
    excerpt = source[:240].strip()
    return {
        "summary_markdown": "## Document summary\n\nThe document describes requirements and responsibilities. [C1]",
        "audit_notes_markdown": (
            "## Audit notes\n\nThe requirements need operating evidence before implementation "
            "can be concluded. [C1]"
        ),
        "citations": [{"id": "C1", "page": page, "excerpt": excerpt}],
    }


@pytest.fixture(autouse=True)
def isolated_workspaces(tmp_path, monkeypatch):
    """Point workspace storage at a temp folder and clear the frame cache."""
    monkeypatch.setattr(workspaces, "WORKSPACES_DIR", tmp_path / "Workspaces")
    monkeypatch.setattr(assistant_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    for provider in assistant_settings.PROVIDERS:
        monkeypatch.delenv(f"{provider.upper()}_MODEL", raising=False)
    loader.clear_cache()
    yield


@pytest.fixture
def transactions_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "invoice_no": [1001, 1002, 1003, 1005, 1006, 1006],
            "cust_id": ["C1", "C2", "C1", "C3", "C2", "C2"],
            "amount": [150.0, 2000.0, 99.5, 1000.0, 150.0, 150.0],
            "tx_date": [
                "2026-01-15",
                "2026-01-20",
                "2026-02-10",
                "2026-02-28",
                "2026-03-05",
                "2026-03-05",
            ],
        }
    )


@pytest.fixture
def workspace_with_data(transactions_df) -> workspaces.Workspace:
    ws = workspaces.create_workspace("Test Engagement")
    ws.add_table("transactions.csv", transactions_df.write_csv().encode())
    customers = pl.DataFrame(
        {"id": ["C1", "C2", "C3"], "customer": ["Alpha", "Beta", "Gamma"]}
    )
    ws.add_table("customers.csv", customers.write_csv().encode())
    return ws


# --------------------------------------------------------------- agent fakes
def _streamed(message: dict, on_delta) -> dict:
    """Deliver a scripted reply the way a streaming provider would.

    Splitting the content means every test that drives the agent also exercises
    the streaming path — coalescing, flushing, and the reassembled message —
    rather than leaving it covered only by its own dedicated test.
    """
    if on_delta is None:
        return message
    content = str(message.get("content") or "")
    half = len(content) // 2 or len(content)
    for piece in (content[:half], content[half:]):
        if piece:
            on_delta(piece)
    return message


class FakeAgentLLM:
    """Scripted model for agent-run tests: dispatches on the stable
    ``[agent:<stage>]`` tag each prompt starts with. Override any stage's
    response (a dict, or a callable receiving the user message) per test."""

    DEFAULTS = {
        # The stages the surviving engines actually call. The deleted v1
        # pipeline's planning/rules/analyses/dashboard/summary scripts went
        # with it in Phase 12.
        "agent:analysis_definitions": _analysis_definitions_response,
        "agent:analysis_summary": _analysis_summary_response,
        "agent:fix_code": {"code": "result = transactions.head(0)"},
        "agent:document_analysis_map": _document_analysis_response,
    }

    def __init__(self, overrides: dict | None = None):
        self.overrides = overrides or {}
        self.calls: list[dict] = []

    def __call__(
        self,
        messages,
        tools=None,
        temperature=0.0,
        profile="assistant",
        tool_choice=None,
        on_delta=None,
    ):
        system = messages[0]["content"]
        tag = system[1 : system.index("]")] if system.startswith("[") else ""
        self.calls.append(
            {
                "tag": tag,
                "profile": profile,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "streamed": on_delta is not None,
            }
        )
        response = self.overrides.get(tag, self.DEFAULTS.get(tag))
        if callable(response):
            response = response(messages[-1]["content"])
        if response is None:
            raise llm.LLMError(f"FakeAgentLLM has no script for '{tag}'.")
        if isinstance(response, dict) and response and (
            "tool_calls" in response
            or set(response).issubset({"content", "tool_calls", "usage"})
        ):
            return _streamed(response, on_delta)
        return _streamed({"content": json.dumps(response)}, on_delta)


@pytest.fixture
def fake_agent_llm(monkeypatch) -> FakeAgentLLM:
    fake = FakeAgentLLM()
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    return fake


def wait_run(workspace, run_id, statuses=agent_store.TERMINAL_STATUSES, timeout=15.0):
    """Poll until the run reaches one of ``statuses``; join its worker."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = agent_store.load_run(workspace, run_id)
        if run["status"] in statuses:
            handle = agent_runner.get_handle(run_id)
            if handle is None or run["status"] in agent_store.TERMINAL_STATUSES:
                if handle is not None and handle.thread is not None:
                    handle.thread.join(timeout=5)
                return run
            return run
        time.sleep(0.02)
    raise AssertionError(
        f"Run did not reach {statuses} in time; last status: {run['status']}"
    )
