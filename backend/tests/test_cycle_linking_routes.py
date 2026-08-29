"""Building, running and reading a ruleset-backed cycle test over the API."""

from __future__ import annotations

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import cycle_rulesets, document_schemas, workspaces
from app.main import create_app

from tests.test_cycle_linking import (  # noqa: F401 - helpers reused
    INVOICE_FIELDS,
    ORDER_FIELDS,
    approved,
    extract,
    ruleset_payload,
)


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def engagement():
    ws = workspaces.create_workspace("Cycle API")
    document_schemas.save_schema(ws, "vendor_invoice", INVOICE_FIELDS)
    document_schemas.save_schema(ws, "purchase_order", ORDER_FIELDS)
    ws.add_table(
        "invoices.csv",
        pl.DataFrame({
            "INVOICE_NO": ["INV-1", "INV-2"],
            "AMOUNT": [100.0, 200.0],
        }).write_csv().encode(),
    )
    row = ws.add_rcm({
        "process": "Procure to pay",
        "risk": "Payments may be made for goods never ordered.",
        "risk_rating": "high",
        "control": "Every invoice is matched to an approved purchase order.",
    })
    extract(ws, "inv1.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po1.txt", "purchase_order", order_number="PO-1", total_amount="100")
    extract(ws, "inv2.txt", "vendor_invoice", invoice_number="INV-2",
            order_number="PO-2", total_amount="200")
    extract(ws, "po2.txt", "purchase_order", order_number="PO-2", total_amount="180")
    return ws, row


def base(ws) -> str:
    return f"/api/workspaces/{ws.id}"


def test_candidates_answer_from_the_approved_rules(client, engagement):
    ws, row = engagement
    approved(ws)

    response = client.post(
        f"{base(ws)}/doc-tests/cycle-vouch/candidates", json={"rcm_id": row["id"]}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "ruleset"
    assert payload["anchor"] == {
        "table": "invoices", "column": "INVOICE_NO",
        "role": "invoice", "field": "invoice_number",
    }
    assert payload["reach"]["complete_cycles"] == 2
    assert payload["selection_confirmation"] is None


def test_candidates_say_when_no_rules_are_approved(client, engagement):
    ws, row = engagement
    cycle_rulesets.save(ws, ruleset_payload(), proposed_by="agent")

    payload = client.post(
        f"{base(ws)}/doc-tests/cycle-vouch/candidates", json={"rcm_id": row["id"]}
    ).json()

    # A proposal is not rules. Saying which of the two reasons there is nothing
    # to build on is what lets the caller act: approve the proposal, or write
    # one.
    assert payload["ruleset"] is None
    assert payload["reason"] == "no_approved_ruleset"


def test_a_cycle_test_is_built_run_and_read_back(client, engagement):
    ws, row = engagement
    approved(ws)

    built = client.post(f"{base(ws)}/doc-tests/build/cycle-vouch", json={
        "title": "Vouch invoices to orders",
        "objective": "Vouch each invoice to its approved order.",
        "rcm_id": row["id"],
        "procedure_key": "match_invoice_to_order",
        "requirement_refs": [f"{row['id']}:matched"],
        "definition": {"population": {"selection": {"mode": "evidence_linked"}}},
    })
    assert built.status_code == 200, built.text
    test_id = built.json()["id"]

    read = client.get(f"{base(ws)}/doc-tests/{test_id}")
    assert read.status_code == 200
    assert len(read.json()["items"]) == 2

    grid = client.get(f"{base(ws)}/doc-tests/{test_id}/grid")
    assert grid.status_code == 200
    assert [column["key"] for column in grid.json()["columns"]] == ["as_total"]


def test_editing_assertions_on_the_test_points_at_the_rules(client, engagement):
    ws, row = engagement
    approved(ws)
    built = client.post(f"{base(ws)}/doc-tests/build/cycle-vouch", json={
        "title": "Vouch invoices to orders",
        "objective": "Vouch each invoice to its approved order.",
        "rcm_id": row["id"],
        "procedure_key": "match_invoice_to_order",
        "requirement_refs": [f"{row['id']}:matched"],
        "definition": {"population": {"selection": {"mode": "evidence_linked"}}},
    }).json()

    current = client.get(f"{base(ws)}/doc-tests/{built['id']}").json()
    response = client.post(
        f"{base(ws)}/doc-tests/{built['id']}/assertions",
        json={
            "expected_test_sha1": current["sha1"],
            "assertion": {
                "key": "as_new", "operator": "present",
                "left": {"source": "role", "role": "invoice"},
            },
        },
    )

    assert response.status_code >= 400
    assert "cycle rules review" in response.text
