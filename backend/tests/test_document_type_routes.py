"""The retyping API: catalog, the unidentified bucket, and auditor assignment."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import document_classification as dc
from app import documents, workspaces
from app.main import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def ws():
    workspace = workspaces.create_workspace("Retyping Engagement")
    for name, body in (
        ("loi-1.txt", b"Letter of indemnity for a missing bill of lading."),
        ("loi-2.txt", b"Letter of indemnity, second consignment."),
        ("inv-1.txt", b"Invoice No. INV-1042\nTotal Due USD 12,480.00"),
    ):
        documents.add_document(workspace, name, body, category="voucher")
    return workspace


def _ids(ws) -> list[str]:
    return [str(item.get("id")) for item in ws.documents]


def _base(ws) -> str:
    return f"/api/workspaces/{ws.id}/documents"


# --------------------------------------------------------------- the catalog
def test_types_endpoint_serves_the_catalog_and_the_engagement_summary(client, ws):
    body = client.get(f"{_base(ws)}/types").json()
    ids = {entry["id"] for entry in body["types"]}
    assert {"vendor_invoice", "bank_confirmation", "other"} <= ids
    assert body["local_prefix"] == "local."
    assert body["local_types"] == []
    assert body["summary"]["documents"] == 3
    assert body["summary"]["unclassified"] == 3


def test_types_endpoint_lists_coined_types_once_they_exist(client, ws):
    dc.retype(ws, _ids(ws)[0], coin="Letter of Indemnity")
    body = client.get(f"{_base(ws)}/types").json()
    assert [item["id"] for item in body["local_types"]] == ["local.letter_of_indemnity"]
    assert body["summary"]["local_types"] == ["local.letter_of_indemnity"]


# --------------------------------------------------------- the other bucket
def test_unidentified_lists_only_the_other_bucket(client, ws):
    first, second, third = _ids(ws)
    dc.assign(ws, first, "other", assigned_by="model", other_label="Letter of indemnity")
    dc.assign(ws, third, "vendor_invoice", assigned_by="model")
    body = client.get(f"{_base(ws)}/unidentified").json()
    assert [item["document_id"] for item in body["items"]] == [first]
    assert body["items"][0]["document_type_other"] == "Letter of indemnity"


# ------------------------------------------------- every assignment, not just `other`
def test_classifications_lists_confident_labels_the_bucket_never_shows(client, ws):
    first, second, third = _ids(ws)
    dc.assign(ws, first, "other", assigned_by="model", other_label="Letter of indemnity")
    dc.assign(ws, second, "vendor_invoice", assigned_by="model")
    dc.assign(ws, third, "goods_receipt", assigned_by="model")

    bucket = client.get(f"{_base(ws)}/unidentified").json()
    assert [item["document_id"] for item in bucket["items"]] == [first]

    body = client.get(f"{_base(ws)}/classifications").json()
    assert {item["document_id"] for item in body["items"]} == {first, second, third}
    assert [item["document_type"] for item in body["items"]] == [
        "goods_receipt", "other", "vendor_invoice",
    ]


def test_classifications_omits_a_document_nothing_has_classified(client, ws):
    first, _second, third = _ids(ws)
    dc.assign(ws, first, "vendor_invoice", assigned_by="model")
    body = client.get(f"{_base(ws)}/classifications").json()
    assert [item["document_id"] for item in body["items"]] == [first]
    assert third not in {item["document_id"] for item in body["items"]}


def test_a_confidently_wrong_label_can_be_corrected(client, ws):
    """The store always permitted this; nothing surfaced the document to fix."""

    document_id = _ids(ws)[1]
    dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    listed = client.get(f"{_base(ws)}/classifications").json()["items"]
    assert document_id in {item["document_id"] for item in listed}

    body = client.patch(
        f"{_base(ws)}/{document_id}/type", json={"type_id": "goods_receipt"}
    ).json()
    assert body["classification"]["document_type"] == "goods_receipt"
    assert body["classification"]["assigned_by"] == "auditor"
    assert body["classification"]["previous_document_type"] == "vendor_invoice"


# ------------------------------------------------------------------ retyping
def test_retype_to_a_listed_type(client, ws):
    document_id = _ids(ws)[2]
    dc.assign(ws, document_id, "other", assigned_by="model", other_label="Unclear")
    response = client.patch(
        f"{_base(ws)}/{document_id}/type", json={"type_id": "vendor_invoice"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["classification"]["document_type"] == "vendor_invoice"
    assert body["classification"]["assigned_by"] == "auditor"
    assert body["summary"]["types_present"] == ["vendor_invoice"]


def test_retype_by_coining_registers_the_type_for_the_engagement(client, ws):
    document_id = _ids(ws)[0]
    dc.assign(ws, document_id, "other", assigned_by="model", other_label="LOI")
    body = client.patch(
        f"{_base(ws)}/{document_id}/type", json={"coin": "Letter of Indemnity"}
    ).json()
    assert body["classification"]["document_type"] == "local.letter_of_indemnity"
    catalog = client.get(f"{_base(ws)}/types").json()
    assert [item["id"] for item in catalog["local_types"]] == ["local.letter_of_indemnity"]


def test_coining_opens_the_rest_of_the_bucket_for_re_examination(client, ws):
    """The point of coining: the sweep that follows is what stops one retyped
    document leaving forty like it unidentified."""

    first, second, _ = _ids(ws)
    signature = dc.catalog_signature(ws)
    for document_id in (first, second):
        dc.assign(ws, document_id, "other", assigned_by="model",
                  other_label="LOI", catalog_sha1=signature)
    assert client.get(f"{_base(ws)}/unidentified").json()["reclassifiable"] == []

    body = client.patch(
        f"{_base(ws)}/{first}/type", json={"coin": "Letter of Indemnity"}
    ).json()
    # The retyped document is settled; the one still in the bucket is now worth
    # re-asking, because the list it was chosen from has grown.
    assert body["reclassifiable"] == [second]


def test_retype_rejects_an_unknown_type(client, ws):
    document_id = _ids(ws)[0]
    response = client.patch(
        f"{_base(ws)}/{document_id}/type", json={"type_id": "not_a_type"}
    )
    assert response.status_code >= 400


def test_retype_rejects_naming_both_a_type_and_a_new_name(client, ws):
    document_id = _ids(ws)[0]
    response = client.patch(
        f"{_base(ws)}/{document_id}/type",
        json={"type_id": "vendor_invoice", "coin": "Something"},
    )
    assert response.status_code >= 400


def test_retype_rejects_coining_a_name_that_shadows_a_listed_type(client, ws):
    document_id = _ids(ws)[0]
    response = client.patch(f"{_base(ws)}/{document_id}/type", json={"coin": "cheque"})
    assert response.status_code >= 400


def test_an_auditor_assignment_is_not_undone_by_a_model_rerun(client, ws):
    document_id = _ids(ws)[0]
    client.patch(f"{_base(ws)}/{document_id}/type", json={"type_id": "goods_receipt"})
    reloaded = workspaces.load_workspace(ws.id)
    dc.assign(reloaded, document_id, "vendor_invoice", assigned_by="model")
    assert dc.document_type(reloaded, document_id) == "goods_receipt"


# --------------------------------------------------------------- reclassify
def test_reclassify_refuses_when_nothing_needs_re_examining(client, ws):
    document_id = _ids(ws)[0]
    dc.assign(ws, document_id, "other", assigned_by="model",
              other_label="LOI", catalog_sha1=dc.catalog_signature(ws))
    response = client.post(f"{_base(ws)}/reclassify", json={})
    assert response.status_code >= 400
    assert "already chosen from the current list" in response.text


def test_reclassify_requests_only_the_classification_outcome(client, ws, monkeypatch):
    """Re-examining what a document *is* must not re-run its analysis, which the
    catalog has no bearing on."""

    captured: dict = {}
    from app.agent import runner

    def fake_start(workspace, mode, command, context=None):
        captured.update(command=command, context=context)
        return {"id": "run-1"}

    monkeypatch.setattr(runner, "start_command_run", fake_start)
    first, second, _ = _ids(ws)
    for document_id in (first, second):
        dc.assign(ws, document_id, "other", assigned_by="model", other_label="LOI")

    assert client.post(f"{_base(ws)}/reclassify", json={}).status_code == 200
    assert captured["command"]["requested_outcomes"] == ["documents.types_classified"]
    assert set(captured["context"]["document_ids"]) == {first, second}


# ------------------------------------------------------------------ listing
def test_document_listing_carries_the_assigned_type(client, ws):
    document_id = _ids(ws)[2]
    dc.assign(ws, document_id, "vendor_invoice", assigned_by="model")
    items = client.get(_base(ws)).json()["items"]
    listed = {item["id"]: item for item in items}
    assert listed[document_id]["classification"]["document_type"] == "vendor_invoice"
    assert listed[_ids(ws)[0]]["classification"]["document_type"] is None
