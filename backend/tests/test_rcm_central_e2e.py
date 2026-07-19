"""Synthetic Procurement acceptance flow for the RCM-central workflow.

The autouse fixture redirects all workspace writes to ``tmp_path``. This test
must never depend on or mutate a user's real engagement data.
"""

import polars as pl

from app import (
    dashboard,
    data_tests,
    doc_tests,
    documents,
    findings,
    rcm_execution,
    report,
    working_papers,
    workspaces,
)


POPULATION_TESTS = (
    "Approval authority exceptions",
    "Invalid purchase-order references",
    "Segregation of duties conflicts",
    "Three-way match exceptions",
    "Invoice verification timeliness",
    "Vendor master integrity",
    "Missing invoice vendor data",
    "Backdated invoice verification",
    "Duplicate invoice identifiers",
    "Goods-receipt completeness",
    "Purchase-order amount integrity",
)


def _procurement_workspace():
    workspace = workspaces.create_workspace("Synthetic Procurement acceptance")
    population = pl.DataFrame(
        {
            "requisition_id": ["REQ2024009", "REQ2024010", "REQ2024011", "REQ2024012"],
            "po_id": ["PO2024004", "PO2024005", "PO2024006", "PO2024007"],
            "grn_id": ["GRN2024004", "GRN2024005", "GRN2024006", "GRN2024007"],
            "invoice_id": ["INV2024004", "INV2024005", "INV2024006", "INV2024006"],
            "vendor_id": ["V001", None, "V003", "V003"],
            "amount": [1250.0, 900.0, 5000.0, 5000.0],
        }
    )
    workspace.add_table("procurement.csv", population.write_csv().encode())
    authority = pl.DataFrame(
        {"designation": ["Finance Manager", "CFO"], "approval_limit": [10_000.0, 100_000.0]}
    )
    workspace.add_table("finance-authority.csv", authority.write_csv().encode())
    workspace.add_join(
        {
            "name": "invalid_finance_authority",
            "left": "procurement",
            "right": "finance_authority",
            "how": "left",
            "left_on": ["vendor_id"],
            "right_on": ["designation"],
        }
    )
    workspace.update_planning(
        {
            "context": {
                "entity": "Synthetic Procurement Co",
                "period": "2026",
                "objective": "Assess procurement approval, matching, and evidence controls.",
                "scope": "Requisition-to-payment procurement transactions.",
                "materiality": "Risk-based testing of the full supplied population.",
            },
            "apm_markdown": "# Audit Planning Memorandum\n\nProcurement control review.",
        }
    )
    return workspace


def _add_evidence_package(workspace):
    files = {
        "REQ2024009-requisition.txt": (
            b"Approved requisition REQ2024009 links to purchase order PO2024004."
        ),
        "PO2024004-purchase-order.txt": (
            b"Purchase order PO2024004 was raised for REQ2024009 and invoice INV2024004."
        ),
        "GRN2024004-goods-receipt.txt": (
            b"Goods receipt GRN2024004 records receipt for PO2024004 and INV2024004."
        ),
        "INV2024004-invoice.txt": (
            b"Invoice INV2024004 references PO2024004 and goods receipt GRN2024004."
        ),
    }
    return [
        documents.add_document(workspace, name, content, category="evidence")
        for name, content in files.items()
    ]


def test_synthetic_procurement_acceptance_from_population_to_preliminary_report():
    workspace = _procurement_workspace()
    evidence_package = _add_evidence_package(workspace)
    rows = []
    valid_results = []

    for index, title in enumerate(POPULATION_TESTS):
        row = workspace.add_rcm(
            {
                "process": "Procurement",
                "risk": title,
                "risk_rating": "high" if index < 6 else "medium",
                "control": f"Management reviews {title.casefold()}.",
                "review_status": "prepared",
            }
        )
        planned = workspace.add_planned_test(
            row["id"],
            {
                "title": title,
                "objective": f"Identify and assess {title.casefold()}.",
                "criteria": "Procurement transactions must be valid, approved, and supported.",
                "method": "hybrid" if index == 0 else "data_analytics",
                "steps": ["Test the complete procurement population."],
                "expected_evidence": "Durable population results and linked source evidence.",
            },
        )
        test = data_tests.create(
            workspace,
            {
                "title": title,
                "objective": planned["objective"],
                "rcm_id": row["id"],
                "planned_test_id": planned["id"],
                "engine": "analytics",
                "table_refs": ["procurement"],
                "spec": {
                    "test_id": "duplicates",
                    "params": {"columns": ["invoice_id"]},
                },
            },
        )
        result = data_tests.run(workspace, test["id"])
        assert result["semantic_valid"] is True
        assert result["exception_count"] > 0
        rows.append((row, planned, test))
        valid_results.append(result)

    first_row, first_planned, _first_test = rows[0]
    document_test = doc_tests.prepare_evidence_aware_vouching(
        workspace,
        {
            "title": "Procurement four-document evidence chain",
            "table": "procurement",
            "size": 2,
            "rcm_id": first_row["id"],
            "planned_test_id": first_planned["id"],
            "identifier_fields": ["requisition_id", "po_id", "grn_id", "invoice_id"],
            "frozen_fields": ["requisition_id", "po_id", "grn_id", "invoice_id", "amount"],
            "required_document_types": [
                "requisition", "purchase_order", "goods_receipt", "invoice"
            ],
        },
    )
    assert set(document_test["items"][0]["document_ids"]) == {
        item["id"] for item in evidence_package
    }
    assert document_test["items"][1]["evidence_request_ids"]
    assert document_test["status"] == "review_required"

    invalid_test = data_tests.create(
        workspace,
        {
            "title": "Invalid finance-authority join",
            "objective": "Test approval authority only if source keys match.",
            "rcm_id": first_row["id"],
            "planned_test_id": first_planned["id"],
            "engine": "polars",
            "table_refs": ["invalid_finance_authority"],
            "spec": {
                "code": "result = invalid_finance_authority.select(pl.len())",
                "result_mode": "summary",
            },
        },
    )
    invalid_result = data_tests.run(workspace, invalid_test["id"])
    assert invalid_result["semantic_valid"] is False
    assert any("0% key match coverage" in issue for issue in invalid_result["semantic_issues"])

    rcm_execution.rollup(workspace)
    for row, planned, _test in rows:
        changes = {
            "conclusion": "The stored population result was reviewed and retained.",
            "control_conclusion": "partially_effective",
        }
        if planned["id"] == first_planned["id"]:
            changes.update(
                scope_limitations="One selected transaction is awaiting source documents.",
                next_action="Obtain and inspect the open evidence request before final reporting.",
            )
        workspace.update_planned_test(row["id"], planned["id"], changes)
    for observation in list(workspace.observations):
        rcm_execution.disposition(
            workspace,
            observation["id"],
            "invalid_test_or_result"
            if observation["execution_ref"].startswith(f"datatest:{invalid_test['id']}:")
            else "screening_follow_up",
            "Reviewed in the synthetic acceptance workflow.",
        )
    rolled = rcm_execution.rollup(workspace)
    assert rolled["coverage"]["planned_tests_without_execution"] == []
    assert all(item["status"] == "disposed" for item in workspace.observations)

    finding_test = rows[1][2]
    finding_result = valid_results[1]
    finding = findings.add(
        workspace,
        {
            "title": "Duplicate invoice identifier requires follow-up",
            "severity": "medium",
            "condition": "A duplicate invoice identifier exists in the supplied population.",
            "criteria": "Invoice identifiers must be unique before payment processing.",
            "cause_pending": True,
            "effect": "Duplicate payment risk requires investigation.",
            "recommendation": "Investigate and prevent repeated invoice identifiers.",
            "severity_rationale": "The exception could result in a duplicate disbursement.",
            "rcm_refs": [rows[1][0]["id"]],
            "planned_test_refs": [rows[1][1]["id"]],
            "execution_refs": [f"datatest:{finding_test['id']}:{finding_result['id']}"],
            "evidence_refs": [
                {
                    "source_kind": "datatest",
                    "source_id": f"{finding_test['id']}:{finding_result['id']}",
                    "source_sha1": finding_result["result_sha1"],
                }
            ],
            "auditor_confirmed": True,
        },
    )
    assert findings.support_issues(workspace, finding) == []

    papers = [working_papers.generate_rcm(workspace, row["id"]) for row, _planned, _test in rows]
    assert len(papers) == 11
    assert all((workspace.root / "WorkingPapers" / f"{row[0]['id']}.json").exists() for row in rows)

    curated = dashboard.curate_rcm_tiles(workspace, run_id="synthetic-procurement")
    assert 4 <= len(curated["tiles"]) <= 6
    assert all(tile.get("rcm_id") and tile.get("planned_test_id") for tile in curated["tiles"])
    assert all(tile["data_test_id"] != invalid_test["id"] for tile in curated["tiles"])

    generated = report.generate(workspace, use_model=False)
    quality = report.quality_checks(workspace)
    completion = rcm_execution.completion(workspace)
    assert "Preliminary" in generated["markdown"]
    assert finding["id"] in generated["markdown"]
    assert not any(issue["code"] == "unsupported_finding" for issue in quality["issues"])
    assert completion["status"] == "completed_with_open_items"
    assert completion["open_observations"] == []
