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
class FakeAgentLLM:
    """Scripted model for agent-run tests: dispatches on the stable
    ``[agent:<stage>]`` tag each prompt starts with. Override any stage's
    response (a dict, or a callable receiving the user message) per test."""

    DEFAULTS = {
        "agent:planning": {
            "domain": "sales",
            "confidence": "high",
            "table_roles": {
                "transactions": "fact: invoice lines",
                "customers": "dimension: customer master",
            },
            "assumptions": ["Amounts are in a single currency."],
            "warnings": [],
            "analysis_tasks": [
                {
                    "table": "transactions",
                    "title": "Check duplicate invoices",
                    "detail": "invoice_no repeats would signal double postings.",
                }
            ],
        },
        "agent:rules": {
            "rules": [
                {
                    "column": "amount",
                    "check": "range",
                    "params": {"min": 0, "max": 100000},
                    "severity": "warn",
                    "rationale": "Amounts outside this band look implausible.",
                }
            ]
        },
        "agent:analyses": {
            "library": [
                {
                    "table": "transactions",
                    "test": "duplicates",
                    "params": {"columns": ["invoice_no"]},
                    "title": "Duplicate invoices",
                    "rationale": "Reused invoice numbers.",
                }
            ],
            "custom": [
                {
                    "table": "transactions",
                    "title": "Spend by customer",
                    "code": (
                        "result = transactions.group_by('cust_id')"
                        ".agg(pl.col('amount').sum())"
                    ),
                    "rationale": "Concentration by customer.",
                }
            ],
        },
        "agent:dashboard": {
            "queries": [
                {
                    "table": "transactions",
                    "title": "Amount by customer",
                    "spec": {
                        "group_by": ["cust_id"],
                        "aggs": [{"column": "amount", "func": "sum"}],
                        "sort": [{"column": "amount_sum", "desc": True}],
                    },
                    "viz": {"type": "bar", "x": "cust_id", "y": ["amount_sum"]},
                    "rationale": "Concentration view.",
                }
            ]
        },
        "agent:fix_code": {"code": "result = transactions.head(0)"},
        "agent:summary": {
            "findings": [
                {
                    "severity": "medium",
                    "statement": "Invoice 1006 appears twice for 150.00.",
                    "basis": "observed",
                    "evidence_refs": [],
                }
            ],
            "summary_markdown": "# Analyst Summary\n\nScripted test summary.",
        },
    }

    def __init__(self, overrides: dict | None = None):
        self.overrides = overrides or {}
        self.calls: list[dict] = []

    def __call__(self, messages, tools=None, temperature=0.0, profile="assistant"):
        system = messages[0]["content"]
        tag = system[1 : system.index("]")] if system.startswith("[") else ""
        self.calls.append({"tag": tag, "profile": profile, "messages": messages})
        response = self.overrides.get(tag, self.DEFAULTS.get(tag))
        if callable(response):
            response = response(messages[-1]["content"])
        if response is None:
            raise llm.LLMError(f"FakeAgentLLM has no script for '{tag}'.")
        return {"content": json.dumps(response)}


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
