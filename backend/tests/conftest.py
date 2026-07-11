import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import assistant_settings, loader, workspaces  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_workspaces(tmp_path, monkeypatch):
    """Point workspace storage at a temp folder and clear the frame cache."""
    monkeypatch.setattr(workspaces, "WORKSPACES_DIR", tmp_path / "Workspaces")
    monkeypatch.setattr(assistant_settings, "SETTINGS_PATH", tmp_path / "settings.json")
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
