"""The ruleset review API: propose, measure, edit, approve."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import cycle_rulesets, document_analysis, document_classification as dc
from app import document_schemas, documents, workspaces
from app.main import create_app

INVOICE_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "vendor_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]
ORDER_FIELDS = [
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "vendor_id", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def ws():
    workspace = workspaces.create_workspace("Ruleset review")
    document_schemas.save_schema(workspace, "vendor_invoice", INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", ORDER_FIELDS)
    return workspace


def _extract(ws, name, document_type, **values):
    document = documents.add_document(ws, name, b"source", category="voucher")
    dc.assign(ws, str(document["id"]), document_type, assigned_by="model")
    schema = document_schemas.get_schema(ws, document_type)
    document_analysis.persist_analysis(
        ws, document, {"pages": [{"page": 1, "text": "source"}]},
        {
            "analysis_profile": "structured",
            "summary_markdown": "s", "audit_notes_markdown": "n",
            "schema_ref": {
                "document_type": schema["document_type"],
                "schema_version": schema["schema_version"],
                "schema_hash": schema["schema_hash"],
            },
            "records": [{
                "fields": [
                    {"name": key, "entry": 1, "value": value, "citation": "c1"}
                    for key, value in values.items()
                ],
                "additional_fields": [],
            }],
        },
        provider="local", model="test",
    )


def _payload(join_field: str = "order_number") -> dict:
    return {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice"},
            {"name": "order", "document_type": "purchase_order"},
        ],
        "anchor": {"table": "register", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk", "match": "normalized_equal",
            "left": {"role": "invoice", "field": join_field},
            "right": {"role": "order", "field": join_field},
        }],
        "assertions": [{
            "id": "as_total", "operator": "numeric_within",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "tolerance": {"absolute": 1},
        }],
    }


def _base(ws) -> str:
    return f"/api/workspaces/{ws.id}/cycle-rulesets"


def _seed(ws, count: int = 3, shared_vendor: bool = False, start: int = 0):
    for index in range(start, start + count):
        _extract(ws, f"inv{index}.txt", "vendor_invoice",
                 invoice_number=f"INV-{index}", order_number=f"PO-{index}",
                 vendor_id="V-1" if shared_vendor else f"V-{index}",
                 total_amount="100")
        _extract(ws, f"po{index}.txt", "purchase_order",
                 order_number=f"PO-{index}",
                 vendor_id="V-1" if shared_vendor else f"V-{index}",
                 total_amount="100")


# ------------------------------------------------------------------ propose
def test_proposing_stores_rules_and_measures_them(client, ws):
    _seed(ws)
    body = client.post(_base(ws), json=_payload()).json()
    assert body["status"] == "proposed"
    assert body["measured"]["join_keys"]["jk"]["fan_out_p95"] == 1
    assert body["concerns"] == []


def test_measurement_is_recomputed_on_read_not_frozen_into_the_rules(client, ws):
    """A fan-out that was true a hundred documents ago is not a fact to approve."""

    _seed(ws, count=1)
    created = client.post(_base(ws), json=_payload()).json()
    assert created["measured"]["join_keys"]["jk"]["matched_pairs"] == 1

    _seed(ws, count=2, start=1)
    reread = client.get(f"{_base(ws)}/{created['ruleset_id']}").json()
    assert reread["measured"]["join_keys"]["jk"]["matched_pairs"] == 3
    assert reread["ruleset_hash"] == created["ruleset_hash"]


def test_an_entity_join_key_surfaces_as_a_concern(client, ws):
    _seed(ws, count=6, shared_vendor=True)
    body = client.post(_base(ws), json=_payload("vendor_id")).json()
    assert [item["concern"] for item in body["concerns"]] == ["entity_fan_out"]
    assert body["measured"]["join_keys"]["jk"]["fan_out_p95"] == 6


def test_a_rule_naming_an_absent_field_is_refused(client, ws):
    payload = _payload()
    payload["assertions"][0]["left"] = {"role": "invoice", "field": "vat_amount"}
    assert client.post(_base(ws), json=payload).status_code >= 400


# ------------------------------------------------------------------- listing
def test_listing_reports_what_a_proposal_can_be_written_against(client, ws):
    body = client.get(_base(ws)).json()
    assert body["effective_ruleset_id"] is None
    assert {item["document_type"] for item in body["schemas"]} == {
        "purchase_order", "vendor_invoice",
    }


# ------------------------------------------------------------------- editing
def test_an_auditor_may_replace_a_proposal_s_rules(client, ws):
    _seed(ws)
    created = client.post(_base(ws), json=_payload()).json()
    edited = client.patch(
        f"{_base(ws)}/{created['ruleset_id']}",
        json={**_payload(), "cycle_label": "Renamed"},
    ).json()
    assert edited["cycle_label"] == "Renamed"
    assert edited["status"] == "proposed"


# ------------------------------------------------------------------ approval
def test_approval_requires_an_identity(client, ws):
    _seed(ws)
    created = client.post(_base(ws), json=_payload()).json()
    assert client.post(
        f"{_base(ws)}/{created['ruleset_id']}/approve", json={}
    ).status_code >= 400
    assert cycle_rulesets.effective(workspaces.load_workspace(ws.id)) is None


def test_approval_makes_one_ruleset_effective(client, ws):
    _seed(ws)
    created = client.post(_base(ws), json=_payload()).json()
    approved = client.post(
        f"{_base(ws)}/{created['ruleset_id']}/approve",
        json={"approved_by": "auditor@example.com"},
    ).json()
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "auditor@example.com"

    listed = client.get(_base(ws)).json()
    assert listed["effective_ruleset_id"] == created["ruleset_id"]


def test_approving_a_successor_supersedes_the_previous(client, ws):
    _seed(ws)
    first = client.post(_base(ws), json=_payload()).json()
    client.post(f"{_base(ws)}/{first['ruleset_id']}/approve",
                json={"approved_by": "auditor"})
    second = client.post(_base(ws), json={**_payload(), "cycle_label": "Successor"}).json()
    client.post(f"{_base(ws)}/{second['ruleset_id']}/approve",
                json={"approved_by": "auditor"})

    listed = client.get(_base(ws)).json()
    assert listed["effective_ruleset_id"] == second["ruleset_id"]
    statuses = {item["ruleset_id"]: item["status"] for item in listed["items"]}
    assert statuses[first["ruleset_id"]] == "superseded"


def test_an_approved_ruleset_cannot_be_edited_or_rejected(client, ws):
    _seed(ws)
    created = client.post(_base(ws), json=_payload()).json()
    client.post(f"{_base(ws)}/{created['ruleset_id']}/approve",
                json={"approved_by": "auditor"})
    assert client.patch(
        f"{_base(ws)}/{created['ruleset_id']}", json=_payload()
    ).status_code >= 400
    assert client.post(
        f"{_base(ws)}/{created['ruleset_id']}/reject"
    ).status_code >= 400


def test_measurement_can_be_stored_without_moving_the_hash(client, ws):
    _seed(ws)
    created = client.post(_base(ws), json=_payload()).json()
    measured = client.post(f"{_base(ws)}/{created['ruleset_id']}/measure").json()
    assert measured["ruleset_hash"] == created["ruleset_hash"]
    stored = cycle_rulesets.get(workspaces.load_workspace(ws.id), created["ruleset_id"])
    assert stored["join_keys"][0]["measured"]["fan_out_p95"] == 1
