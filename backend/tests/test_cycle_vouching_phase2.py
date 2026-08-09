"""Phase 2 gates for RCM attributes and canonical Cycle Vouch generation."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app import cycle_vouching, doc_tests, workspaces


FIXTURE = Path(__file__).parent / "fixtures" / "procurement_cycle_phase0.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _manifest(contract: dict, *, linked_rows: int = 1) -> dict:
    test = contract["cycle_test"]
    population = test["definition"]["population"]
    roles = test["definition"]["roles"]
    records = cycle_vouching.validate_evidence_reduction(
        contract["reduction"]
    )["records"]
    record_manifests = [
        cycle_vouching._record_manifest(record, cycle_vouching.DEFAULT_REGISTRY)
        for record in records
    ]
    role_kinds = {role["role"]: role["record_kind"] for role in roles}
    by_kind = {record["record_kind"]: record for record in record_manifests}
    # The manifest reports each fact's own registered attribute, so an assertion
    # over a supplied field is present in it as written. This used to patch the
    # missing profile in, because attributes were derived from envelope keys and
    # an approval's `approver` never appeared.
    for assertion in test["definition"]["assertions"]:
        for operand in (assertion.get("left"), assertion.get("right")):
            if not isinstance(operand, dict) or operand.get("source") != "role":
                continue
            field = operand["field"]
            available = by_kind[role_kinds[operand["role"]]]["available_fields"]
            assert any(
                entry["group"] == field["group"]
                and entry["kind"] == field["kind"]
                and field["attribute"] in entry["attributes"]
                for entry in available
            ), f"{field} missing from the manifest for role '{operand['role']}'"
    candidate = {
        "candidate_id": population["candidate_id"],
        "rank": 1,
        "registry": test["registry"],
        "table": population["table"],
        "source_kind": "authoritative",
        "row_key": population["row_key"],
        "cycle_keys": population["cycle_keys"],
        "column_types": {
            "INVOICE_ID": "identifier",
            "VENDOR_INVOICE_NUMBER": "identifier",
            "PO_NUMBER_LINK": "identifier",
            "GRN_ID_LINK": "identifier",
            "INVOICE_AMOUNT": "number",
        },
        "population_rows": max(linked_rows, 1),
        "linked_rows": linked_rows,
        "complete_cycle_count": linked_rows,
        "reachable_record_kinds": sorted(
            {role["record_kind"] for role in roles}
        ),
        "reachable_roles": [role["role"] for role in roles],
        "required_role_coverage": len(roles),
        "relationship_facts": {
            role["role"]: {
                "max_records_per_item": 1,
                "max_items_per_record": 1,
            }
            for role in roles
        },
        "missing_role_counts": {role["role"]: 0 for role in roles},
    }
    group = {
        "registry": test["registry"],
        "requirement_refs": [item["key"] for item in contract["control_attributes"]],
        "required_record_kinds": list(
            dict.fromkeys(
                kind
                for attribute in contract["control_attributes"]
                for kind in attribute["required_record_kinds"]
            )
        ),
        "roles": roles,
        "records": record_manifests,
        "candidates": [candidate],
        "rejected_candidates": [],
    }
    manifest = {"groups": [group]}
    manifest["manifest_sha256"] = cycle_vouching._canonical_hash(manifest)
    return manifest


def _row_payload(contract: dict) -> dict:
    return {
        "id": contract["cycle_test"]["rcm_id"],
        "process": "Procure to pay",
        "risk": "Payments may lack complete transaction evidence.",
        "risk_rating": "high",
        "business_cycle": "procure_to_pay",
        "control": "Payment release requires the complete supporting cycle.",
        "control_type": "Preventive",
        "control_attributes": contract["control_attributes"],
    }


def _test_payload(contract: dict) -> dict:
    return {
        **copy.deepcopy(contract["cycle_test"]),
        "title": "Five-record procurement cycle",
        "objective": "Inspect the complete evidence cycle for selected invoices.",
    }


def test_rcm_persists_discriminated_attributes_without_top_level_assertion(contract):
    workspace = workspaces.create_workspace("Phase 2 RCM")
    row = workspace.add_rcm({**_row_payload(contract), "assertion": "Occurrence"})

    assert row["business_cycle"] == "procure_to_pay"
    assert row["control_attributes"] == contract["control_attributes"]
    assert "assertion" not in row
    exported = workspace.export_rcm_rows()[0]
    assert json.loads(exported["control_attributes"])[0]["evidence_kind"] == (
        "transaction_cycle"
    )


def test_canonical_service_persists_definition_and_reuses_stable_identity(
    contract, monkeypatch
):
    workspace = workspaces.create_workspace("Phase 2 cycle")
    workspace.add_rcm(_row_payload(contract))
    manifest = _manifest(contract)
    monkeypatch.setattr(
        cycle_vouching,
        "transaction_evidence_manifest",
        lambda *_args, **_kwargs: copy.deepcopy(manifest),
    )

    created = cycle_vouching.build_cycle_vouch_test(
        workspace, _test_payload(contract)
    )
    revised = cycle_vouching.build_cycle_vouch_test(
        workspace,
        {**_test_payload(contract), "id": created["id"], "title": "Revised title"},
    )

    assert created["kind"] == "cycle_vouch"
    assert created["items"] == []
    assert created["semantic_id"] == revised["semantic_id"]
    assert created["id"] == revised["id"]
    assert revised["coverage"]["assurance_scope"] == "targeted_evidence_only"
    assert revised["context_manifest_sha256"] == manifest["manifest_sha256"]
    assert len(doc_tests.list_tests(workspace)) == 1


def test_regeneration_replaces_the_definition_without_resetting_the_record(
    contract, monkeypatch
):
    """A new definition is not a new draft.

    Regeneration owns the definition and its derived coverage. The workflow
    status and the auditor's own links belong to the durable Document Test, and
    a reset to ``draft`` would silently take the test back out of
    ``definitions_ready``.
    """
    workspace = workspaces.create_workspace("Phase 2 regeneration")
    workspace.add_rcm(_row_payload(contract))
    manifest = _manifest(contract)
    monkeypatch.setattr(
        cycle_vouching,
        "transaction_evidence_manifest",
        lambda *_args, **_kwargs: copy.deepcopy(manifest),
    )
    created = cycle_vouching.build_cycle_vouch_test(
        workspace, _test_payload(contract)
    )
    assert created["status"] == "ready"

    cycle_vouching.build_cycle_vouch_test(
        workspaces.load_workspace(workspace.id),
        {**_test_payload(contract), "id": created["id"], "title": "Revised title"},
    )
    reloaded = doc_tests.load_test(workspaces.load_workspace(workspace.id), created["id"])

    assert reloaded["status"] == "ready"
    assert reloaded["title"] == "Revised title"
    for field in ("procedure_refs", "criteria", "evidence_refs", "finding_refs"):
        assert field in reloaded


def test_a_definition_generated_against_stale_evidence_is_refused(
    contract, monkeypatch
):
    """Provenance is checked, not assumed.

    The selection was made from one manifest's candidate list. If evidence moved
    between proposal and commit, that choice rests on facts that no longer hold.
    """
    workspace = workspaces.create_workspace("Phase 2 provenance")
    workspace.add_rcm(_row_payload(contract))
    manifest = _manifest(contract)
    monkeypatch.setattr(
        cycle_vouching,
        "transaction_evidence_manifest",
        lambda *_args, **_kwargs: copy.deepcopy(manifest),
    )

    with pytest.raises(cycle_vouching.CycleSchemaError, match="manifest changed"):
        cycle_vouching.build_cycle_vouch_test(
            workspace,
            {
                **_test_payload(contract),
                "context_manifest_sha256": "sha256:" + "0" * 64,
            },
        )
    assert doc_tests.list_tests(workspace) == []


def test_a_transaction_cycle_needs_more_than_one_record_kind(contract):
    """One record kind is not a cycle.

    A requirement reaching a single record has no transaction linkage to test,
    so it belongs to a document or tabular strategy rather than producing a
    cycle whose graph can only reach its own seed.
    """
    attribute = {
        **copy.deepcopy(contract["control_attributes"][0]),
        "required_record_kinds": ["procure_to_pay.payment_voucher"],
    }

    with pytest.raises(cycle_vouching.CycleSchemaError, match="at least 2 record kinds"):
        cycle_vouching.validate_control_attributes([attribute])


def test_transaction_cycle_requires_typed_evidence_comparisons(contract):
    attribute = copy.deepcopy(contract["control_attributes"][0])
    attribute.pop("required_comparisons")

    with pytest.raises(cycle_vouching.CycleSchemaError, match="required_comparisons"):
        cycle_vouching.validate_control_attributes([attribute])


def test_transaction_cycle_rejects_unregistered_bank_account_substitute(contract):
    attribute = copy.deepcopy(contract["control_attributes"][0])
    attribute["required_record_kinds"] = [
        "procure_to_pay.vendor_invoice",
        "procure_to_pay.payment_voucher",
    ]
    attribute["required_comparisons"] = [
        {
            "key": "payment_bank_to_vendor_bank",
            "label": "Payment account agrees to approved vendor account",
            "left": {
                "record_kind": "procure_to_pay.payment_voucher",
                "field": {
                    "group": "identifiers",
                    "kind": "account_number",
                    "attribute": "value",
                },
            },
            "right": {
                "record_kind": "procure_to_pay.vendor_invoice",
                "field": {
                    "group": "identifiers",
                    "kind": "account_number",
                    "attribute": "value",
                },
            },
            "operator": "equal_exact",
        }
    ]

    with pytest.raises(cycle_vouching.CycleSchemaError, match="is not registered"):
        cycle_vouching.validate_control_attributes([attribute])


def test_semantic_gate_rejects_related_assertions_as_requirement_coverage(contract):
    manifest = _manifest(contract)
    test = _test_payload(contract)
    test["definition"]["assertions"] = [
        assertion
        for assertion in test["definition"]["assertions"]
        if assertion["key"] != "invoice_amount_to_payment"
    ]

    with pytest.raises(cycle_vouching.CycleSchemaError, match="requires comparison"):
        cycle_vouching.validate_cycle_test_semantics(
            test,
            rcm_row=_row_payload(contract),
            manifest=manifest,
        )


def test_semantic_gate_accepts_declared_purchase_order_to_receipt_agreement(contract):
    """A PO/GRN agreement becomes a Cycle Vouch only when the RCM says so.

    This guards the exact failure observed in the first live Checkpoint C: a
    model may also propose a tabular population procedure, but that must not
    erase a separately declared source-record agreement requirement.
    """
    comparison = {
        "key": "purchase_order_quantity_to_receipt",
        "label": "Received quantity agrees to the purchase order",
        "left": {
            "record_kind": "procure_to_pay.purchase_order",
            "field": {
                "group": "quantities",
                "kind": "total",
                "attribute": "value",
            },
        },
        "right": {
            "record_kind": "procure_to_pay.goods_receipt",
            "field": {
                "group": "quantities",
                "kind": "total",
                "attribute": "value",
            },
        },
        "operator": "numeric_within",
        "tolerance": {"absolute": 0, "percent": 0},
    }
    attribute = {
        "key": "purchase_order_to_receipt_agreement",
        "assertion": "Accuracy",
        "requirement": "Received quantities agree to the approved purchase order.",
        "evidence_kind": "transaction_cycle",
        "registry": copy.deepcopy(contract["cycle_test"]["registry"]),
        "required_record_kinds": [
            "procure_to_pay.purchase_order",
            "procure_to_pay.goods_receipt",
        ],
        "required_comparisons": [comparison],
    }
    row = _row_payload(contract)
    row["control_attributes"] = [attribute]
    test = _test_payload(contract)
    test["requirement_refs"] = [f"{test['rcm_id']}:{attribute['key']}"]
    test["definition"]["roles"] = [
        role
        for role in test["definition"]["roles"]
        if role["record_kind"] in set(attribute["required_record_kinds"])
    ]
    role_by_kind = {
        role["record_kind"]: role["role"] for role in test["definition"]["roles"]
    }
    test["definition"]["assertions"] = [
        {
            "key": comparison["key"],
            "label": comparison["label"],
            "left": {
                "source": "role",
                "role": role_by_kind[comparison["left"]["record_kind"]],
                "field": copy.deepcopy(comparison["left"]["field"]),
            },
            "right": {
                "source": "role",
                "role": role_by_kind[comparison["right"]["record_kind"]],
                "field": copy.deepcopy(comparison["right"]["field"]),
            },
            "operator": comparison["operator"],
            "tolerance": copy.deepcopy(comparison["tolerance"]),
        }
    ]
    manifest = _manifest(contract)
    manifest["groups"][0]["requirement_refs"] = [attribute["key"]]
    manifest["groups"][0]["required_record_kinds"] = list(
        attribute["required_record_kinds"]
    )
    manifest["groups"][0]["roles"] = copy.deepcopy(test["definition"]["roles"])
    for record in manifest["groups"][0]["records"]:
        if record["record_kind"] not in set(attribute["required_record_kinds"]):
            continue
        record["available_fields"].append(
            {
                "group": "quantities",
                "kind": "total",
                "label": "Total quantity",
                "attributes": ["value"],
                "attribute_types": {"value": "number"},
                "control_evidence_attributes": [],
                "normalization_status": "normalized",
                "entry_count": 1,
                "distinct_value_counts": {"value": 1},
            }
        )

    validated = cycle_vouching.validate_cycle_test_semantics(
        test,
        rcm_row=row,
        manifest=manifest,
    )

    assert validated["definition"]["assertions"][0]["key"] == comparison["key"]


def test_oversized_evidence_selection_returns_confirmation_without_persisting(
    contract, monkeypatch
):
    workspace = workspaces.create_workspace("Phase 2 confirmation")
    workspace.add_rcm(_row_payload(contract))
    manifest = _manifest(contract, linked_rows=501)
    monkeypatch.setattr(
        cycle_vouching,
        "transaction_evidence_manifest",
        lambda *_args, **_kwargs: copy.deepcopy(manifest),
    )

    with pytest.raises(cycle_vouching.SelectionConfirmationRequired) as raised:
        cycle_vouching.build_cycle_vouch_test(workspace, _test_payload(contract))

    assert raised.value.proposal["eligible_row_count"] == 501
    assert raised.value.proposal["suggested_selection"] == {
        "mode": "sample",
        "method": "random",
        "size": 25,
        "seed": 42,
    }
    # A caller that mistakes the proposal for a test cannot silently persist a
    # truncated population: there is no test-shaped return value to commit.
    assert doc_tests.list_tests(workspace) == []


def test_semantic_gate_rejects_unavailable_explicit_field_attribute(contract):
    manifest = _manifest(contract)
    test = _test_payload(contract)
    assertion = test["definition"]["assertions"][0]
    assertion["left"]["field"]["attribute"] = "currency"
    assertion["operator"] = "present"
    assertion.pop("right")
    assertion.pop("tolerance")

    with pytest.raises(cycle_vouching.CycleSchemaError, match="is not present"):
        cycle_vouching.validate_cycle_test_semantics(
            test,
            rcm_row=_row_payload(contract),
            manifest=manifest,
        )


def test_stable_identity_ignores_title_but_changes_with_population(contract):
    first = _test_payload(contract)
    renamed = {**copy.deepcopy(first), "title": "Different title"}
    changed = copy.deepcopy(first)
    changed["definition"]["population"]["row_key"]["column"] = "OTHER_ID"

    assert cycle_vouching.stable_test_semantic_id(first) == (
        cycle_vouching.stable_test_semantic_id(renamed)
    )
    assert cycle_vouching.stable_test_semantic_id(first) != (
        cycle_vouching.stable_test_semantic_id(changed)
    )


def test_business_cycle_is_derived_rather_than_echoed(contract):
    """A caller changes attributes; the projection follows on its own.

    Requiring every caller to restate the derived value turns an ordinary
    attribute edit — including the agent's own ``edit_rcm_row`` — into an error
    whenever the pack changes.
    """
    workspace = workspaces.create_workspace("Phase 2 derivation")
    payload = _row_payload(contract)
    payload.pop("business_cycle")
    row = workspace.add_rcm(payload)
    assert row["business_cycle"] == "procure_to_pay"

    updated = workspace.update_rcm(
        row["id"],
        {
            "control_attributes": [
                {
                    "key": "manual_inspection",
                    "assertion": "Operational",
                    "requirement": "Assessed by inspection.",
                    "evidence_kind": "manual_inspection",
                }
            ]
        },
    )

    assert updated["business_cycle"] == ""


def test_a_stale_pack_reference_flags_its_row_without_closing_the_workspace(
    contract, monkeypatch
):
    """Failing closed must not take the repair UI down with the bad row.

    A pack version change invalidates dependent artifacts by design, but the
    engagement's documents, findings, and the RCM editor needed to fix the row
    have to stay reachable.
    """
    workspace = workspaces.create_workspace("Phase 2 stale pack")
    workspace.add_rcm(_row_payload(contract))
    row_id = workspace.rcm[0]["id"]
    stored = json.loads(
        (workspace.root / "Planning" / "RCM" / f"{row_id}.json").read_text("utf-8")
    )
    stored["control_attributes"][0]["registry"]["pack_version"] = 99
    (workspace.root / "Planning" / "RCM" / f"{row_id}.json").write_text(
        json.dumps(stored), encoding="utf-8"
    )

    reloaded = workspaces.load_workspace(workspace.id)

    assert len(reloaded.rcm) == 1
    assert reloaded.rcm[0]["attributes_status"] == "invalid"
    assert "stale" in reloaded.rcm[0]["attributes_error"]
    # And the row can still be repaired through the ordinary write path.
    repaired = reloaded.update_rcm(
        row_id, {"control_attributes": contract["control_attributes"]}
    )
    assert repaired["attributes_status"] == "valid"
    assert repaired["business_cycle"] == "procure_to_pay"


def test_assurance_scope_is_structurally_derived():
    assert cycle_vouching.assurance_scope_for({"mode": "evidence_linked"}) == (
        "targeted_evidence_only"
    )
    assert cycle_vouching.assurance_scope_for({"mode": "sample"}) == (
        "sampled_population"
    )
