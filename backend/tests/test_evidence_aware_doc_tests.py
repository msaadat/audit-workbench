import polars as pl

from app import doc_tests, documents, rcm_execution, workspaces


def _workspace_with_procurement_population():
    ws = workspaces.create_workspace("Evidence-aware procurement")
    population = pl.DataFrame(
        {
            "requisition_id": ["REQ2024009", "REQ2024010"],
            "po_id": ["PO2024004", "PO2024005"],
            "grn_id": ["GRN2024004", "GRN2024005"],
            "invoice_id": ["INV2024004", "INV2024005"],
            "amount": [1250.0, 900.0],
        }
    )
    ws.add_table("procurement.csv", population.write_csv().encode())
    row = ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Purchases may not be supported by a complete evidence chain.",
            "risk_rating": "high",
            "control": "Requisition, PO, receipt, and invoice are retained.",
        }
    )
    return ws, row


def test_procurement_package_is_selected_before_uncovered_transactions():
    ws, row = _workspace_with_procurement_population()
    package = [
        documents.add_document(
            ws,
            "REQ2024009-requisition.txt",
            b"Purchase requisition REQ2024009 links to purchase order PO2024004 and was approved.",
            category="evidence",
        ),
        documents.add_document(
            ws,
            "PO2024004-purchase-order.txt",
            b"Purchase order PO2024004 was raised from requisition REQ2024009 for invoice INV2024004.",
            category="evidence",
        ),
        documents.add_document(
            ws,
            "GRN2024004-goods-receipt.txt",
            b"Goods receipt GRN2024004 records receipt for PO2024004 and invoice INV2024004.",
            category="evidence",
        ),
        documents.add_document(
            ws,
            "INV2024004-invoice.txt",
            b"Supplier invoice INV2024004 references purchase order PO2024004 and receipt GRN2024004.",
            category="evidence",
        ),
    ]

    test = doc_tests.prepare_evidence_aware_vouching(
        ws,
        {
            "title": "Procurement evidence chain",
            "table": "procurement",
            "size": 2,
            "rcm_id": row["id"],
            
            "identifier_fields": ["requisition_id", "po_id", "grn_id", "invoice_id"],
            "frozen_fields": ["requisition_id", "po_id", "grn_id", "invoice_id", "amount"],
            "required_document_types": [
                "requisition", "purchase_order", "goods_receipt", "invoice"
            ],
        },
    )

    first = test["items"][0]
    assert "REQ2024009" in first["label"]
    assert set(first["document_ids"]) == {document["id"] for document in package}
    assert first["evidence_coverage"]["missing_document_types"] == []
    second = test["items"][1]
    assert "REQ2024010" in second["label"]
    assert second["document_ids"] == []
    assert len(second["evidence_request_ids"]) == 1
    request = test["evidence_requests"][0]
    assert request["transaction_identifier"].startswith("REQ2024010")
    assert request["missing_document_types"] == [
        "requisition", "purchase_order", "goods_receipt", "invoice"
    ]
    assert request["next_action"]
    assert test["status"] == "review_required"
    assert test["spec"]["evidence_coverage"] == {
        "selected": 2,
        "evidence_covered": 1,
        "evidence_requested": 1,
        "image_only": 0,
    }


def test_no_available_evidence_creates_blocked_test_with_requests_and_limitation():
    ws, row = _workspace_with_procurement_population()

    test = doc_tests.prepare_evidence_aware_vouching(
        ws,
        {
            "title": "Unavailable invoice evidence",
            "table": "procurement",
            "size": 1,
            "rcm_id": row["id"],
            
            "identifier_fields": ["invoice_id"],
            "required_document_types": ["invoice"],
        },
    )

    assert test["status"] == "blocked"
    assert test["scope_limitations"]
    assert len(ws.evidence_requests) == 1
    assert ws.evidence_requests[0]["status"] == "open"
    rolled = rcm_execution.rollup(ws)
    test_rollup = rolled["rows"][0]["test_rollups"][0]
    assert test_rollup["status"] == "blocked"


def test_image_only_matched_evidence_routes_item_to_manual_review():
    ws, row = _workspace_with_procurement_population()
    image = documents.add_document(
        ws,
        "INV2024004-invoice.png",
        b"not-a-real-image-but-stored-as-image-evidence",
        category="evidence",
    )

    test = doc_tests.prepare_evidence_aware_vouching(
        ws,
        {
            "title": "Image invoice evidence",
            "table": "procurement",
            "size": 1,
            "rcm_id": row["id"],
            
            "identifier_fields": ["invoice_id"],
            "required_document_types": ["invoice"],
        },
    )

    assert test["items"][0]["document_ids"] == [image["id"]]
    assert test["items"][0]["state"] == "manual_review"
    assert test["items"][0]["evidence_coverage"]["image_only"] is True
    assert test["status"] == "review_required"


def test_evidence_request_status_is_auditable():
    ws, row = _workspace_with_procurement_population()
    test = doc_tests.prepare_evidence_aware_vouching(
        ws,
        {
            "title": "Evidence request lifecycle",
            "table": "procurement",
            "size": 1,
            "rcm_id": row["id"],
            
            "identifier_fields": ["invoice_id"],
            "required_document_types": ["invoice"],
        },
    )
    request_id = test["evidence_requests"][0]["id"]

    updated = doc_tests.update_evidence_request(
        ws, request_id, status="received", note="Received from Accounts Payable."
    )

    assert updated["status"] == "received"
    assert updated["auditor_note"] == "Received from Accounts Payable."
    assert doc_tests.load_test(ws, test["id"])["scope_limitations"] == ""
