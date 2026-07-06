import pytest

from app import workspaces
from app.workspaces import WorkspaceError


def test_create_list_delete_workspace():
    ws = workspaces.create_workspace("My Audit", "FY26 revenue testing")
    assert ws.id == "my-audit"
    listed = workspaces.list_workspaces()
    assert [w["id"] for w in listed] == ["my-audit"]
    assert listed[0]["description"] == "FY26 revenue testing"

    workspaces.delete_workspace("my-audit")
    assert workspaces.list_workspaces() == []


def test_create_duplicate_and_blank_name_rejected():
    workspaces.create_workspace("Audit")
    with pytest.raises(WorkspaceError):
        workspaces.create_workspace("audit")
    with pytest.raises(WorkspaceError):
        workspaces.create_workspace("   ")


def test_add_table_types_and_uniquifies(workspace_with_data):
    ws = workspace_with_data
    frame = ws.get_frame("transactions")
    assert frame.height == 6
    assert frame.schema["amount"].is_numeric()

    entry = ws.add_table("transactions.csv", b"a,b\n1,2\n")
    assert entry["name"] == "transactions_2"


def test_add_table_rejects_unreadable_and_unsupported(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(WorkspaceError):
        ws.add_table("evil.exe", b"nope")
    before = len(ws.tables)
    with pytest.raises(WorkspaceError):
        ws.add_table("broken.xlsx", b"not an excel file")
    assert len(ws.tables) == before
    assert not list(ws.data_dir.glob("broken*"))


def test_join_and_dependency_guard(workspace_with_data):
    ws = workspace_with_data
    ws.add_join(
        {
            "name": "tx enriched",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    joined = ws.get_frame("tx_enriched")
    assert "customer" in joined.columns
    assert joined.height == 6

    with pytest.raises(WorkspaceError, match="used by join"):
        ws.remove_table("customers")

    ws.remove_join("tx_enriched")
    ws.remove_table("customers")
    assert ws.table_names() == ["transactions"]


def test_join_validation_errors(workspace_with_data):
    ws = workspace_with_data
    base = {
        "name": "bad",
        "left": "transactions",
        "right": "customers",
        "how": "left",
    }
    with pytest.raises(WorkspaceError, match="keys"):
        ws.add_join({**base, "left_on": [], "right_on": []})
    with pytest.raises(WorkspaceError, match="not found"):
        ws.add_join({**base, "left_on": ["missing"], "right_on": ["id"]})
    with pytest.raises(WorkspaceError, match="Unknown table"):
        ws.add_join({**base, "right": "nope", "left_on": ["cust_id"], "right_on": ["id"]})
    # Failed joins must not be persisted.
    assert ws.joins == []


def test_persistence_roundtrip(workspace_with_data):
    ws = workspace_with_data
    ws.add_join(
        {
            "name": "enriched",
            "left": "transactions",
            "right": "customers",
            "how": "inner",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.table_names() == ["transactions", "customers", "enriched"]
    assert reloaded.get_frame("enriched").height == 6
