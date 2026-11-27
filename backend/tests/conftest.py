import sys
import time
from pathlib import Path

import polars as pl
import pytest

_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR.parent))
sys.path.insert(0, str(_TESTS_DIR))

from app import accounts, assistant_settings, config, db, llm, loader, registry, stage_status, telemetry_db, usage, workspaces  # noqa: E402
from app.agent import runner as agent_runner  # noqa: E402
from app.agent import store as agent_store  # noqa: E402
from helpers.fake_llm import FakeAgentLLM  # noqa: E402, F401


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
    "test_agent_capability_composition.py",
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


@pytest.fixture(autouse=True)
def isolated_workspaces(tmp_path, monkeypatch):
    """Point every durable path at a temp folder and clear caches.

    The control-plane SQLite database and local account are bootstrapped lazily
    on the first ``db.connect()`` call so pure in-memory unit tests pay zero
    SQLite creation/migration overhead.
    """
    db.close_all()
    telemetry_db.close_all()
    registry.forget_reconciled()
    usage.forget_owners()
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.delenv("WORKBENCH_DB", raising=False)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.setattr(assistant_settings, "SETTINGS_PATH", tmp_path / "settings.json")

    real_db_connect = db.connect
    bootstrapped_paths: set[str] = set()

    def lazy_db_connect():
        connection = real_db_connect()
        key = str(db.database_path())
        if key not in bootstrapped_paths:
            bootstrapped_paths.add(key)
            accounts.ensure_local_user()
        return connection

    monkeypatch.setattr(db, "connect", lazy_db_connect)

    for provider in assistant_settings.PROVIDERS:
        monkeypatch.delenv(f"{provider.upper()}_MODEL", raising=False)
        monkeypatch.delenv(f"{provider.upper()}_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("LLM_REASONING", raising=False)
    monkeypatch.delenv("LLM_TOOLS_API", raising=False)
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("DEBUG_TELEMETRY", raising=False)
    loader.clear_cache()
    workspaces.clear_artifact_cache()
    monkeypatch.setattr(stage_status, "ENABLED", False)
    yield
    _stop_leftover_runs()
    db.close_all()
    telemetry_db.close_all()
    registry.forget_reconciled()
    usage.forget_owners()
    workspaces.clear_artifact_cache()


def _stop_leftover_runs(timeout: float = 10.0) -> None:
    """Stop any agent run a test left running, before the next test starts."""
    with agent_runner._HANDLES_LOCK:
        handles = list(agent_runner._HANDLES.values())
    for handle in handles:
        handle.cancel.set()
        for event in (
            handle.resume,
            handle.approval_resolved,
            handle.interaction_resolved,
            handle.command_queued,
        ):
            event.set()
    deadline = time.monotonic() + timeout
    for handle in handles:
        thread = handle.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    with agent_runner._HANDLES_LOCK:
        agent_runner._HANDLES.clear()


def stamp_planning_cycle(workspace, *, steps=None):
    """Give a workspace a cycle shape current with its memorandum."""
    from app import document_classification, planning_cycle

    names = steps or list(
        dict.fromkeys(
            str(row.get("process") or "").strip()
            for row in workspace.rcm
            if str(row.get("process") or "").strip()
        )
    ) or ["Process"]
    held = [
        entry["document_type"]
        for entry in document_classification.evidence_type_counts(workspace)
        if entry["document_type"] != "other"
    ]
    tables = [str(table.get("name") or "") for table in workspace.tables]
    shape = {
        "name": "Engagement cycle",
        "steps": [
            {
                "name": name,
                "roles": (
                    [{"name": f"record_{index}", "document_type": held[0]}]
                    if index == 0 and held
                    else []
                ),
                "populations": (
                    [{"table": tables[0], "anchor": True}]
                    if index == 0 and tables
                    else []
                ),
                "themes": [],
            }
            for index, name in enumerate(names[: planning_cycle.MAX_CYCLE_STEPS])
        ],
        "cross_cutting": {"name": "Cycle operations", "themes": []},
        "agent_run_id": "fixture",
    }
    workspace.update_planning({"cycle": shape}, agent=True)
    return workspace.planning["cycle"]


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


def wait_run(workspace, run_id, statuses=agent_store.TERMINAL_STATUSES, timeout=60.0):
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
