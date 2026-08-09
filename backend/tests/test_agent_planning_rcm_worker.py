"""Focused tests for the registered ``planning.rcm`` model worker (P7C.2).

The worker owns only the RCM prompt, bundle-to-message transformation, response
schema, and the engagement quality gate. It is exercised with constructed
bundles and a gateway stub and must not touch a workspace, store, resolver, or
scheduler.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import WORKERS, WorkerRequest, WorkerRunError
from app.agent.workers import planning


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(
        self, system, user, activity=None, *, attempt=1, conversation=None
    ):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "activity": activity,
                "attempt": attempt,
                "conversation": conversation,
            }
        )
        return self.responses.pop(0)


def _bundle(*, current_rows=None):
    values = [
        (
            "rcm_template",
            "template:rcm",
            ContextRepresentation("artifact_template"),
            "# Risk and control matrix\n",
        ),
        (
            "current_apm",
            "planning:apm",
            ContextRepresentation("current_artifact"),
            "# APM\n\nAssess procurement approvals.",
        ),
        (
            "planning_context",
            "planning:context",
            ContextRepresentation("planning_context"),
            {"context": {"objective": "Assess procurement approvals"}},
        ),
    ]
    for row in current_rows or []:
        values.append(
            (
                "current_rcm",
                f"rcm:{row['id']}",
                ContextRepresentation("current_artifact"),
                row,
            )
        )
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=representation,
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="planning.rcm",
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "rcm-input"},
        activity={"artifact_refs": ["planning:apm"]},
    )


def _row(**overrides):
    row = {
        "operation": "create",
        "process": "Accounts payable",
        "risk": "Duplicate payments are processed",
        "risk_rating": "high",
        "business_cycle": "",
        "control_attributes": [
            {
                "key": "duplicate_payment_prevention",
                "assertion": "Operational",
                "requirement": "Duplicate invoice validation operates before payment.",
                "evidence_kind": "manual_inspection",
            }
        ],
        "control": "Duplicate invoice validation",
        "control_type": "Automated preventive",
        "test_procedure": "Test invoice and amount duplicates.",
        "new_risk_reason": "No existing RCM row covers duplicate payments.",
    }
    row.update(overrides)
    return row


def _three_way_required_comparisons():
    return [
        {
            "key": "invoice_po_amount",
            "label": "Invoice amount agrees to purchase order",
            "left": {
                "record_kind": "procure_to_pay.vendor_invoice",
                "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            },
            "right": {
                "record_kind": "procure_to_pay.purchase_order",
                "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            },
            "operator": "numeric_within",
            "tolerance": {"absolute": 0.01, "percent": 0},
        },
        {
            "key": "po_grn_quantity",
            "label": "Purchase order quantity agrees to goods receipt",
            "left": {
                "record_kind": "procure_to_pay.purchase_order",
                "field": {"group": "quantities", "kind": "total", "attribute": "value"},
            },
            "right": {
                "record_kind": "procure_to_pay.goods_receipt",
                "field": {"group": "quantities", "kind": "total", "attribute": "value"},
            },
            "operator": "numeric_within",
            "tolerance": {"absolute": 0, "percent": 0},
        },
    ]


def test_rcm_worker_uses_only_bundle_and_returns_validated_rows():
    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway)

    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Duplicate payments are processed"
    ]
    assert gateway.calls[0]["system"] == planning.RCM_SYSTEM
    assert gateway.calls[0]["attempt"] == 1
    assert gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == "rcm"
    # The APM narrative and template reach the model.
    assert "Assess procurement approvals" in gateway.calls[0]["user"]
    assert "Risk and control matrix" in gateway.calls[0]["user"]


def test_rcm_worker_accepts_json_fenced_response():
    gateway = _Gateway(["```json\n" + json.dumps({"rows": [_row()]}) + "\n```"])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["operation"] == "create"


def test_rcm_worker_repairs_a_quality_failure_with_specific_guidance():
    first_response = json.dumps({"rows": [_row(risk_rating="urgent")]})
    gateway = _Gateway([first_response, json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert [call["attempt"] for call in gateway.calls] == [1, 2]
    assert gateway.calls[1]["conversation"] == [
        {"role": "user", "content": gateway.calls[1]["user"]},
        {"role": "assistant", "content": first_response},
        {
            "role": "user",
            "content": (
                "Correct every listed quality-gate error in the prior RCM draft "
                "while preserving all otherwise-valid rows and fields: RCM row 1 "
                "has an unsupported risk rating. Return the complete corrected "
                "JSON object."
            ),
        },
    ]


def test_rcm_worker_derives_business_cycle_and_drops_non_durable_reason():
    registry = planning.cycle_vouching.DEFAULT_REGISTRY.reference(
        "procure_to_pay"
    ).to_dict()
    row = _row(
        business_cycle="wrong_model_value",
        new_risk_reason=None,
        control_attributes=[
            {
                "key": "three_way_match",
                "assertion": "Accuracy",
                "requirement": "Invoices agree to purchase and receipt records.",
                "evidence_kind": "transaction_cycle",
                "registry": registry,
                    "required_record_kinds": [
                        "procure_to_pay.purchase_order",
                        "procure_to_pay.goods_receipt",
                        "procure_to_pay.vendor_invoice",
                    ],
                    "required_comparisons": _three_way_required_comparisons(),
            }
        ],
    )

    result = WORKERS.execute(
        _request(), _Gateway([json.dumps({"rows": [row]})])
    )

    normalized = result.proposal["rows"][0]
    assert normalized["business_cycle"] == "procure_to_pay"
    assert "new_risk_reason" not in normalized


def test_rcm_worker_aggregates_quality_errors_across_rows():
    with pytest.raises(planning.WorkerResponseValidationError) as raised:
        planning.validate_rcm_proposal(
            {
                "rows": [
                    _row(risk_rating="urgent"),
                    _row(operation="replace"),
                ]
            },
            _request(),
        )

    assert raised.value.errors == (
        "RCM row 1 has an unsupported risk rating",
        "RCM row 2 has an unsupported operation",
    )


@pytest.mark.parametrize(
    ("malformed_registry", "sibling_record_kinds", "expected_error"),
    [
        ("procure_to_pay", True, "registry must be an object"),
        (
            {
                "pack_id": "procure_to_pay",
                "required_record_kinds": [
                    "procure_to_pay.purchase_order",
                    "procure_to_pay.goods_receipt",
                    "procure_to_pay.vendor_invoice",
                ],
            },
            False,
            "invalid pack version",
        ),
    ],
)
def test_rcm_worker_repairs_transaction_cycle_registry_to_canonical_reference(
    malformed_registry, sibling_record_kinds, expected_error
):
    registry = planning.cycle_vouching.DEFAULT_REGISTRY.reference(
        "procure_to_pay"
    ).to_dict()
    malformed_attribute = {
        "key": "three_way_match",
        "assertion": "Accuracy",
        "requirement": "Invoices agree to the purchase order and goods receipt.",
        "evidence_kind": "transaction_cycle",
        "registry": malformed_registry,
    }
    record_kinds = [
        "procure_to_pay.purchase_order",
        "procure_to_pay.goods_receipt",
        "procure_to_pay.vendor_invoice",
    ]
    if sibling_record_kinds:
        malformed_attribute["required_record_kinds"] = record_kinds
    corrected_attribute = {
        **malformed_attribute,
        "registry": registry,
        "required_record_kinds": record_kinds,
        "required_comparisons": _three_way_required_comparisons(),
    }
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "rows": [
                        _row(
                            business_cycle="procure_to_pay",
                            control_attributes=[malformed_attribute],
                        )
                    ]
                }
            ),
            json.dumps(
                {
                    "rows": [
                        _row(
                            business_cycle="procure_to_pay",
                            control_attributes=[corrected_attribute],
                        )
                    ]
                }
            ),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert result.proposal["rows"][0]["control_attributes"][0]["registry"] == registry
    assert expected_error in gateway.calls[1]["conversation"][2]["content"]
    assert "pack_id, pack_version, and" in planning.RCM_SYSTEM
    assert "required_record_kinds is a\n  sibling of registry" in planning.RCM_SYSTEM
    assert json.dumps(registry, sort_keys=True, separators=(",", ":")) in (
        planning.RCM_SYSTEM
    )


def test_rcm_worker_rejects_update_to_unknown_existing_row():
    current = _row(id="RCM-EXISTING", operation="update")
    gateway = _Gateway(
        [
            json.dumps({"rows": [_row(operation="update", rcm_id="RCM-MISSING")]}),
            json.dumps({"rows": [_row(operation="update", rcm_id="RCM-MISSING")]}),
        ]
    )

    with pytest.raises(WorkerRunError, match="does not identify an existing RCM row"):
        WORKERS.execute(_request(_bundle(current_rows=[current])), gateway)


def test_rcm_worker_accepts_update_matching_a_supplied_current_row():
    current = _row(id="RCM-1")
    gateway = _Gateway(
        [json.dumps({"rows": [_row(operation="update", rcm_id="rcm:RCM-1")]})]
    )

    result = WORKERS.execute(_request(_bundle(current_rows=[current])), gateway)

    assert result.proposal["rows"][0]["rcm_id"] == "rcm:RCM-1"


def test_rcm_worker_has_no_workspace_store_resolver_or_scheduler_dependency():
    source = inspect.getsource(planning)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name.endswith(
            (
                "workspaces",
                "workspace_transactions",
                "store",
                "resolver",
                "workflow_runner",
                "action_runner",
            )
        )
        for name in imported
    )
    assert ".ws" not in source
    assert "load_workspace" not in source
