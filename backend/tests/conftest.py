import json
import sys
import time
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import accounts, assistant_settings, config, db, llm, loader, registry, usage, workspaces  # noqa: E402
from app.agent import runner as agent_runner  # noqa: E402
from app.agent import store as agent_store  # noqa: E402


@pytest.fixture(autouse=True)
def reset_llm_rate_limit_cooldown():
    """Do not let the transport's process-wide gate leak between tests."""
    llm._reset_rate_limit_cooldown_for_tests()
    yield
    llm._reset_rate_limit_cooldown_for_tests()


# Every test has one primary tier.  Keeping this classification at collection
# time avoids obscuring otherwise straightforward tests with repetitive marker
# decorators, while still making each tier selectable with ``pytest -m``.
_ARCHITECTURE_MODULES = {
    "test_agent_architecture_contracts.py",
    "test_agent_capability_composition.py",
    "test_agent_runtime_import_boundaries.py",
    "test_agent_final_boundaries.py",
}
_E2E_MODULES = {"test_rcm_central_e2e.py"}
_INTEGRATION_PREFIXES = (
    "test_workflow_",
    "test_cycle_vouching",
    "test_document_",
)
_UNIT_MODULES = {
    "test_agent_context_models.py",
    "test_agent_executor_models.py",
    "test_agent_joins.py",
    "test_agent_narration.py",
    "test_agent_routing.py",
    "test_agent_suggest.py",
    "test_agent_worker_models.py",
    "test_analytics.py",
    "test_explore.py",
    "test_field_names.py",
    "test_validation.py",
}


def _test_tier(path: Path) -> str:
    name = path.name
    if name in _ARCHITECTURE_MODULES:
        return "architecture"
    if name in _E2E_MODULES:
        return "e2e"
    if name in _UNIT_MODULES:
        return "unit"
    if name.startswith(_INTEGRATION_PREFIXES):
        return "integration"
    return "contract"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply exactly one documented tier to every collected test item."""
    for item in items:
        item.add_marker(getattr(pytest.mark, _test_tier(Path(str(item.fspath)))))


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
                "note": "A first look at the frame's shape.",
            },
        ]
    }


def _analysis_reading_response(user: str) -> dict:
    """Settle the register: keep every measurement, add one assertion per frame.

    Keeping is the default the contract already guarantees, so an empty answer
    would be valid — and would leave the definition worker with nothing to do on
    fixtures whose frames are too small for the sweep to nominate anything. One
    assertion per frame reproduces the per-frame turn the definition tests are
    written against, which is what a real reading turn does for the frames it
    has something to say about.
    """
    payload = json.loads(user.split("\n\nYour previous response")[0])
    added = []
    for frame in payload.get("FRAME MAP") or []:
        columns = [column["name"] for column in frame.get("columns") or []]
        if not columns:
            continue
        added.append(
            {
                "frame": frame["table"],
                "columns": columns[:1],
                "assertion": f"{frame['table']} carries no repeated {columns[0]}.",
                "why": "A reused key is the shape a double posting takes.",
            }
        )
    return {"keep": [], "add": added[:12], "decline": [], "unanswerable": []}


def _join_utility_response(user: str) -> dict:
    """Retain the first route to each table pair and reject its competitors.

    The worker admits at most one candidate per pair, so a script that retained
    every technically safe relationship would exercise the repair turn rather
    than the workflow. Retaining in catalog order keeps the choice deterministic
    without pretending to judge audit utility.
    """
    payload = json.loads(user.split("\nRepair the prior response")[0])
    catalog = payload["JOIN CANDIDATES"]
    table_columns = catalog.get("table_columns") or {}
    decisions: list[dict] = []
    retained: set[frozenset[str]] = set()
    for candidate in catalog["candidates"]:
        pair = frozenset((candidate["left"], candidate["right"]))
        if pair in retained:
            decisions.append(
                {
                    "ref": candidate["ref"],
                    "decision": "reject",
                    "rationale": "Another route between these tables was retained.",
                }
            )
            continue
        retained.add(pair)
        decisions.append(
            {
                "ref": candidate["ref"],
                "decision": "retain",
                "rationale": "The pair supports a test neither side supports alone.",
                "hypothesis": "Every key on the left side resolves on the right.",
                "columns": [
                    f"{table}.{(table_columns.get(table) or ['id'])[0]}"
                    for table in (candidate["left"], candidate["right"])
                ],
                # The narrowest requirement there is: the two sides and nothing
                # else, so the test lands on the direct join of the pair.
                "requires": [candidate["left"], candidate["right"]],
            }
        )
    return {"decisions": decisions}


def _analysis_summary_response(_user: str) -> dict:
    """A structurally valid EDA memo submission for any workspace.

    The worker takes the memo in parts and assembles the document itself, so
    this answers with the parts. It references no procedure on purpose: a
    default that reached for one would have to know what the bundle supplied,
    and every workspace using this fixture supplies something different.
    """
    return {
        "lead": "The imported population reconciles and nothing requires escalation.",
        "data_received": "The imported tables were profiled.",
        "findings": [
            {"prose": "Nothing requiring escalation.", "procedures": []},
        ],
        "reliance": {"prose": "No material gaps.", "procedures": []},
        "further_work": "Nothing outstanding.",
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
    """Point every durable path at a temp folder and clear the frame cache.

    ``config.DATA_ROOT`` is the single root the workspace tree, the control
    plane database, and the settings document are all derived from, so one
    monkeypatch isolates all three.  The control-plane connection is dropped on
    both sides because it is cached per thread and per database path.
    """
    db.close_all()
    registry.forget_reconciled()
    usage.forget_owners()
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.delenv("WORKBENCH_DB", raising=False)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.setattr(assistant_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    # Single-user mode resolves to this account; creating it up front means a
    # test that never touches a route still has an owner for its workspaces.
    accounts.ensure_local_user()
    for provider in assistant_settings.PROVIDERS:
        monkeypatch.delenv(f"{provider.upper()}_MODEL", raising=False)
    # Sampling is configuration now, and it overrides the stored document, so a
    # developer's own export would otherwise decide what the suite asserts.
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    loader.clear_cache()
    yield
    db.close_all()
    registry.forget_reconciled()
    usage.forget_owners()


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


def _scripted_user_view(messages: list[dict]) -> str:
    """What the model was asked, as the one string a script reads.

    A worker repair is no longer a single restated user message: it replays the
    rejected submission as an assistant tool call answered by a ``tool``
    message, then closes with the prose note. A script keying off "the user
    message" needs both user turns — the first carries the source text it parses
    out, the last carries the faults to repair. The rejected arguments are
    deliberately not spliced in; the tool result states the same faults, and a
    copy of the previous answer sitting between the two would break every script
    that splits this view on a marker.

    A tool-calling loop ends its turn on a ``tool`` message instead, and there
    the latest result is the whole point, so that case is left exactly as it was.
    """

    if not messages or messages[-1].get("role") != "user":
        return messages[-1]["content"] if messages else ""
    return "\n\n".join(
        message["content"]
        for message in messages[1:]
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    )


class FakeAgentLLM:
    """Scripted model for agent-run tests: dispatches on the stable
    ``[agent:<stage>]`` tag each prompt starts with. Override any stage's
    response (a dict, or a callable receiving the joined user turns) per test."""

    DEFAULTS = {
        # The stages the surviving engines actually call. The deleted v1
        # pipeline's planning/rules/analyses/dashboard/summary scripts went
        # with it in Phase 12.
        "agent:join_utility": _join_utility_response,
        "agent:analysis_reading": _analysis_reading_response,
        "agent:analysis_definitions": _analysis_definitions_response,
        "agent:analysis_summary": _analysis_summary_response,
        # A decline is a complete answer and needs no knowledge of a fixture's
        # matrix, so it is the default every existing agent-run test gets. The
        # promotion path is exercised explicitly by the tests that override it.
        "agent:analysis_promotion": {
            "promote": False,
            "reason": "The procedure describes the population rather than a control.",
        },
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
            response = response(_scripted_user_view(messages))
        if response is None:
            raise llm.LLMError(f"FakeAgentLLM has no script for '{tag}'.")
        if isinstance(response, dict) and response and (
            "tool_calls" in response
            or set(response).issubset({"content", "tool_calls", "usage"})
        ):
            return _streamed(response, on_delta)
        if tools and isinstance(tool_choice, dict):
            name = (
                tool_choice.get("function", {}).get("name")
                if isinstance(tool_choice.get("function"), dict)
                else None
            )
            if name:
                return _streamed(
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "fake_tool_call",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(response),
                                },
                            }
                        ],
                    },
                    on_delta,
                )
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
