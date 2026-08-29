import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.main import create_app
from app.workspaces import WorkspaceError


def test_create_list_delete_workspace():
    ws = workspaces.create_workspace("My Audit", "FY26 revenue testing")
    # Identity and location are separate: the id is a globally unique uid, and
    # the readable slug names the directory inside the owner's home.
    assert ws.dir_name == "my-audit"
    assert ws.id == ws.uid and ws.uid.startswith("ws_")
    assert ws.root.parent.parent.name == "local"
    listed = workspaces.list_workspaces()
    assert [w["id"] for w in listed] == [ws.uid]
    assert listed[0]["description"] == "FY26 revenue testing"

    workspaces.delete_workspace("my-audit")
    assert workspaces.list_workspaces() == []


def test_create_duplicate_and_blank_name_rejected():
    workspaces.create_workspace("Audit")
    with pytest.raises(WorkspaceError):
        workspaces.create_workspace("audit")
    with pytest.raises(WorkspaceError):
        workspaces.create_workspace("   ")


def test_create_workspace_has_no_document_ai_settings():
    ws = workspaces.create_workspace("AI-enabled audit")
    assert not hasattr(ws, "settings")
    assert "settings" not in ws.summary()


def test_create_workspace_api_has_no_workspace_privacy_settings():
    client = TestClient(create_app())
    response = client.post(
        "/api/workspaces",
        json={"name": "API AI audit"},
    )
    assert response.status_code == 200
    assert "settings" not in response.json()


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


def test_replace_table_keeps_name_and_updates_data(workspace_with_data):
    ws = workspace_with_data
    # A saved analysis bound to the table by name — the point of replacing.
    ws.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "By customer",
            "spec": {"test": "sign_scan", "params": {"column": "amount"}},
        }
    )
    assert ws.get_frame("transactions").height == 6

    new_csv = b"invoice_no,cust_id,amount,tx_date\n2001,C1,10.0,2027-01-01\n2002,C2,20.0,2027-01-02\n"
    result = ws.replace_table("transactions", "transactions.csv", new_csv)

    assert result["name"] == "transactions"
    assert result["removed_columns"] == [] and result["added_columns"] == []
    # Same name, new data — reload from disk to prove it persisted.
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.get_frame("transactions").height == 2
    # The saved analysis still resolves against the fresh data.
    assert "transactions" in reloaded.table_names()


def test_replace_table_reports_schema_diff(workspace_with_data):
    ws = workspace_with_data
    result = ws.replace_table(
        "transactions", "transactions.csv", b"invoice_no,amount,branch\n1,10.0,North\n"
    )
    assert set(result["removed_columns"]) == {"cust_id", "tx_date"}
    assert result["added_columns"] == ["branch"]


def test_replace_table_rolls_back_on_bad_file(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(WorkspaceError):
        ws.replace_table("transactions", "transactions.xlsx", b"not an excel file")
    # Original data untouched; no temp file left behind.
    assert ws.get_frame("transactions").height == 6
    assert not list(ws.data_dir.glob(".transactions.upload*"))


def test_replace_table_format_change_retires_old_file(workspace_with_data):
    import io

    import polars as pl

    ws = workspace_with_data
    assert (ws.data_dir / "transactions.csv").exists()

    buffer = io.BytesIO()
    pl.DataFrame({"invoice_no": [1, 2], "amount": [5.0, 6.0]}).write_excel(buffer)
    ws.replace_table("transactions", "transactions.xlsx", buffer.getvalue())

    entry = ws._table_entry("transactions")
    assert entry["file"] == "transactions.xlsx"
    assert not (ws.data_dir / "transactions.csv").exists()
    assert ws.get_frame("transactions").height == 2


def test_replace_rejects_join_and_unknown(workspace_with_data):
    ws = workspace_with_data
    ws.add_join(
        {
            "name": "tx_enriched",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    with pytest.raises(WorkspaceError):
        ws.replace_table("tx_enriched", "x.csv", b"a,b\n1,2\n")
    with pytest.raises(WorkspaceError):
        ws.replace_table("nope", "x.csv", b"a,b\n1,2\n")


def test_rename_table_updates_saved_references(workspace_with_data):
    ws = workspace_with_data
    join = ws.add_join(
        {
            "name": "tx enriched",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    bound = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions",
            "title": "Code",
            "spec": {"code": "result = tables['transactions'].head(1)"},
        }
    )
    analysis = ws.add_analysis(
        {
            "kind": "python",
            "table": None,
            "title": "Bare code",
            "spec": {"code": "result = transactions.select(pl.len())"},
        }
    )
    ruleset = ws.add_ruleset({"title": "Rules", "table": "transactions", "rules": []})

    result = ws.rename_table("transactions", "ledger entries")

    assert result["name"] == "ledger_entries"
    assert result["updated"] == {
        "joins": 1,
        "analyses": 2,
        "rulesets": 1,
        "python_snippets": 2,
    }
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.table_names() == ["ledger_entries", "customers", "tx_enriched"]
    assert reloaded._join_entry(join["name"])["left"] == "ledger_entries"
    assert reloaded._analysis(bound["id"])["table"] == "ledger_entries"
    assert reloaded._analysis(bound["id"])["spec"]["code"] == "result = tables['ledger_entries'].head(1)"
    assert reloaded._analysis(analysis["id"])["spec"]["code"] == "result = ledger_entries.select(pl.len())"
    assert reloaded._ruleset(ruleset["id"])["table"] == "ledger_entries"
    assert reloaded.get_frame("tx_enriched").height == 6


def test_rename_table_rewrites_python_without_renaming_local_alias(workspace_with_data):
    ws = workspace_with_data
    analysis = ws.add_analysis(
        {
            "kind": "python",
            "table": None,
            "title": "Alias code",
            "spec": {
                "code": "transactions = tables['transactions']\nresult = transactions.head(1)"
            },
        }
    )

    ws.rename_table("transactions", "ledger")

    code = workspaces.load_workspace(ws.id)._analysis(analysis["id"])["spec"]["code"]
    assert code == "transactions = tables['ledger']\nresult = transactions.head(1)"


def test_rename_table_validation_errors(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(WorkspaceError, match="already exists"):
        ws.rename_table("transactions", "customers")
    with pytest.raises(WorkspaceError, match="required"):
        ws.rename_table("transactions", "   ")
    with pytest.raises(WorkspaceError, match="No table"):
        ws.rename_table("missing", "ledger")


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


def test_join_scalar_keys_are_normalized(workspace_with_data):
    ws = workspace_with_data
    join = ws.add_join(
        {
            "name": "enriched",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": "CUST_ID",
            "right_on": "ID",
        }
    )

    assert join["left_on"] == ["cust_id"]
    assert join["right_on"] == ["id"]
    assert ws.get_frame("enriched").height == ws.get_frame("transactions").height


def test_join_scalar_missing_column_reports_the_whole_name(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(WorkspaceError, match="Column 'missing' not found"):
        ws.add_join(
            {
                "name": "bad",
                "left": "transactions",
                "right": "customers",
                "how": "left",
                "left_on": "missing",
                "right_on": "id",
            }
        )

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
