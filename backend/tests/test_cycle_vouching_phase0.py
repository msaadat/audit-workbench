"""Phase 0/0.1 contract gate for registry-backed cycle evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app import cycle_vouching, doc_tests, documents, workspaces
from app.agent import runner
from app.agent.capabilities import doc_tests as doc_test_capabilities
from app.agent.workflows import doc_tests as doc_tests_workflow
from app.cycle_registry import CycleRegistry, DEFAULT_REGISTRY
from app.cycle_registry.common import BASE_FIELD_KIND_IDS
from conftest import wait_run


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def procurement_contract() -> dict:
    return json.loads(
        (FIXTURES / "procurement_cycle_phase0.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def payroll_contract() -> dict:
    return json.loads(
        (FIXTURES / "payroll_cycle_phase0.json").read_text(encoding="utf-8")
    )


def _validate_contract(contract: dict) -> tuple[dict, dict]:
    for fragment in contract["record_fragments"]:
        cycle_vouching.validate_evidence_record_fragment(fragment)
    reduction = cycle_vouching.validate_evidence_reduction(contract["reduction"])
    cycle_vouching.validate_control_attributes(contract["control_attributes"])
    test = cycle_vouching.validate_cycle_test(contract["cycle_test"])
    return reduction, test


def test_procurement_fixture_uses_five_exact_record_kinds_and_typed_contracts(
    procurement_contract,
):
    reduction, test = _validate_contract(procurement_contract)

    assert {record["record_kind"] for record in reduction["records"]} == {
        "procure_to_pay.purchase_requisition",
        "procure_to_pay.purchase_order",
        "procure_to_pay.goods_receipt",
        "procure_to_pay.vendor_invoice",
        "procure_to_pay.payment_voucher",
    }
    assert test["definition"]["population"]["selection"]["assurance_scope"] == (
        "targeted_evidence_only"
    )
    assert all(
        isinstance(assertion["left"], dict)
        for assertion in test["definition"]["assertions"]
    )
    assert not any(
        "document_type" in fragment for fragment in procurement_contract["record_fragments"]
    )


def test_payroll_fixture_uses_the_same_domain_neutral_contracts(payroll_contract):
    reduction, test = _validate_contract(payroll_contract)

    assert test["registry"]["pack_id"] == "payroll"
    assert {record["record_kind"] for record in reduction["records"]} == {
        "payroll.employment_contract",
        "payroll.time_record",
        "payroll.payroll_register",
        "payroll.payslip",
        "payroll.bank_payment",
    }
    assert test["definition"]["assertions"][0]["operator"] == "numeric_within"


def test_registry_separates_transaction_edges_from_shared_entities(
    procurement_contract,
):
    reference = procurement_contract["registry"]
    pack_id = reference["pack_id"]
    assert (
        DEFAULT_REGISTRY.identifier_kind(
            pack_id, "procure_to_pay.purchase_order_number"
        ).edge_policy
        == "transaction"
    )
    assert (
        DEFAULT_REGISTRY.identifier_kind(pack_id, "common.vendor_id").edge_policy
        == "non_linking"
    )
    assert (
        DEFAULT_REGISTRY.identifier_kind(pack_id, "common.buyer_id").edge_policy
        == "non_linking"
    )
    assert cycle_vouching.normalized_identifier(
        "procure_to_pay.purchase_order_number",
        " PO2024004 ",
        registry_ref=reference,
    ) == "po2024004"
    assert cycle_vouching.normalized_identifier(
        "procure_to_pay.purchase_order_number",
        "P02024004",
        registry_ref=reference,
    ) != cycle_vouching.normalized_identifier(
        "procure_to_pay.purchase_order_number",
        "PO2024004",
        registry_ref=reference,
    )


def test_registry_metadata_is_dynamic_and_hash_identified():
    cycle_meta = cycle_vouching.metadata()["registry"]
    packs = {item["id"]: item for item in cycle_meta["packs"]}

    assert set(packs) == {"procure_to_pay", "payroll"}
    assert all(item["definition_hash"].startswith("sha256:") for item in packs.values())
    assert any(
        item["id"] == "payroll.payslip"
        for item in packs["payroll"]["record_kinds"]
    )


def test_shared_base_fields_reach_every_record_kind_of_every_pack():
    """A record can state a party, date, approval, or amount in any pack.

    A field kind a pack declares but no record kind offers is unusable: the
    extraction worker reads the fact, cites it, and then has to drop it or
    relabel it as an unrelated registered field. The shared base is therefore
    available on every bindable record kind rather than opted into per kind.
    """

    quantity = DEFAULT_REGISTRY.field_kind("procure_to_pay", "quantities", "total")
    status = DEFAULT_REGISTRY.field_kind("procure_to_pay", "statuses", "status")
    party = DEFAULT_REGISTRY.field_kind("procure_to_pay", "parties", "name")

    assert quantity.id == "common.quantity.total"
    assert status.id == "common.status"
    assert party.id == "common.party.name"

    for pack_id in ("procure_to_pay", "payroll"):
        pack = DEFAULT_REGISTRY.pack(pack_id)
        # Nothing may be declared by a pack and reachable from no record kind.
        offered = {
            field_id
            for record_id in pack.record_kind_ids
            for field_id in DEFAULT_REGISTRY.record_kinds[record_id].available_field_kinds
        }
        assert set(pack.field_kind_ids) == offered
        for record_id in pack.record_kind_ids:
            record = DEFAULT_REGISTRY.record_kinds[record_id]
            if not record.bindable:
                continue
            assert set(BASE_FIELD_KIND_IDS) <= set(record.available_field_kinds)


def test_pack_specific_fields_still_fail_closed_across_packs():
    """Sharing the base does not merge the packs' own vocabularies."""

    with pytest.raises(ValueError, match="not registered for 'payroll'"):
        DEFAULT_REGISTRY.field_kind("payroll", "dates", "receipt_date")
    with pytest.raises(ValueError, match="not registered for 'procure_to_pay'"):
        DEFAULT_REGISTRY.field_kind("procure_to_pay", "amounts", "net_pay")


def test_expanded_pack_definition_changes_its_hash():
    payroll = replace(DEFAULT_REGISTRY.pack("payroll"), label="Payroll revised")
    changed = CycleRegistry(
        normalizers=DEFAULT_REGISTRY.normalizers.values(),
        identifier_kinds=DEFAULT_REGISTRY.identifier_kinds.values(),
        field_kinds=DEFAULT_REGISTRY.field_kinds.values(),
        record_kinds=DEFAULT_REGISTRY.record_kinds.values(),
        evidence_kinds=DEFAULT_REGISTRY.evidence_kinds.values(),
        packs=(DEFAULT_REGISTRY.pack("procure_to_pay"), payroll),
    )

    assert changed.reference("payroll").definition_hash != (
        DEFAULT_REGISTRY.reference("payroll").definition_hash
    )


def test_stale_registry_reference_and_cross_pack_kinds_fail_closed(
    payroll_contract,
):
    stale = copy.deepcopy(payroll_contract["cycle_test"])
    stale["registry"]["definition_hash"] = "sha256:stale"
    with pytest.raises(cycle_vouching.CycleSchemaError, match="stale"):
        cycle_vouching.validate_cycle_test(stale)

    invalid_version = copy.deepcopy(payroll_contract["cycle_test"])
    invalid_version["registry"]["pack_version"] = True
    with pytest.raises(cycle_vouching.CycleSchemaError, match="invalid pack version"):
        cycle_vouching.validate_cycle_test(invalid_version)

    cross_pack = copy.deepcopy(payroll_contract["cycle_test"])
    cross_pack["definition"]["roles"][0]["record_kind"] = (
        "procure_to_pay.purchase_order"
    )
    with pytest.raises(cycle_vouching.CycleSchemaError, match="not registered"):
        cycle_vouching.validate_cycle_test(cross_pack)


def test_assertion_results_are_bound_to_the_pack_definition(payroll_contract):
    item = copy.deepcopy(payroll_contract["cycle_test"]["items"][0])
    item["result_by_assertion"] = {
        "hours_present": {
            "verdict": "match",
            "registry_definition_hash": "sha256:stale",
        }
    }
    with pytest.raises(cycle_vouching.CycleSchemaError, match="stale registry"):
        cycle_vouching.normalize_cycle_item(
            item, registry_ref=payroll_contract["registry"]
        )


def test_materialized_item_identifiers_cannot_cross_packs(payroll_contract):
    item = copy.deepcopy(payroll_contract["cycle_test"]["items"][0])
    item["cycle_identifiers"][0]["kind"] = (
        "procure_to_pay.purchase_order_number"
    )
    with pytest.raises(cycle_vouching.CycleSchemaError, match="not registered"):
        cycle_vouching.normalize_cycle_item(
            item, registry_ref=payroll_contract["registry"]
        )


def test_fragment_cannot_blend_distinct_primary_identifiers(procurement_contract):
    fragment = copy.deepcopy(procurement_contract["record_fragments"][1])
    fragment["identifiers"].append(
        {
            "kind": "procure_to_pay.purchase_order_number",
            "value": {
                "raw_value": "PO-OTHER",
                "value": "PO-OTHER",
                "normalization_status": "normalized",
                "normalization_error": None,
                "citation": "PO-C2",
            },
        }
    )
    with pytest.raises(cycle_vouching.CycleSchemaError, match="cannot blend"):
        cycle_vouching.validate_evidence_record_fragment(fragment)


def test_invalid_normalization_is_present_evidence_not_a_null_normalized_value():
    accepted = cycle_vouching.validate_normalized_value(
        {
            "raw_value": "29-Apr -2024",
            "value": None,
            "normalization_status": "invalid",
            "normalization_error": "unrecognized date format",
            "citation": "GRN-C2",
        }
    )
    assert accepted["normalization_status"] == "invalid"
    with pytest.raises(cycle_vouching.CycleSchemaError, match="normalization_error"):
        cycle_vouching.validate_normalized_value(
            {
                "raw_value": "29-Apr -2024",
                "value": None,
                "normalization_status": "invalid",
                "normalization_error": None,
                "citation": "GRN-C2",
            }
        )


def test_control_attribute_keys_are_unique(procurement_contract):
    attributes = copy.deepcopy(procurement_contract["control_attributes"])
    attributes[1]["key"] = attributes[0]["key"]
    with pytest.raises(cycle_vouching.CycleSchemaError, match="Duplicate"):
        cycle_vouching.validate_control_attributes(attributes)


def test_non_cycle_evidence_strategies_do_not_require_a_domain_pack():
    attributes = cycle_vouching.validate_control_attributes(
        [
            {
                "key": "policy_terms",
                "assertion": "Compliance",
                "requirement": "The policy contains the required terms.",
                "evidence_kind": "document_content",
            }
        ]
    )
    assert attributes[0]["evidence_kind"] == "document_content"

    invalid = copy.deepcopy(attributes)
    invalid[0]["comparison_recipes"] = [{"recipe_id": "common.party_agreement"}]
    with pytest.raises(cycle_vouching.CycleSchemaError, match="does not accept"):
        cycle_vouching.validate_control_attributes(invalid)


def test_role_fields_must_exist_on_the_selected_pack_record(payroll_contract):
    test = copy.deepcopy(payroll_contract["cycle_test"])
    test["definition"]["assertions"][0]["left"] = {
        "source": "role",
        "role": "employment_contract",
        "field": {"group": "amounts", "kind": "net_pay", "attribute": "value"},
    }
    with pytest.raises(cycle_vouching.CycleSchemaError, match="unavailable"):
        cycle_vouching.validate_cycle_test(test)


@pytest.mark.parametrize(
    "identifier_kind",
    ["common.vendor_id", "common.buyer_id", "common.account_number"],
)
def test_entity_identifiers_are_rejected_as_cycle_keys(
    procurement_contract, identifier_kind
):
    test = copy.deepcopy(procurement_contract["cycle_test"])
    test["definition"]["population"]["cycle_keys"][0][
        "identifier_kind"
    ] = identifier_kind
    with pytest.raises(cycle_vouching.CycleSchemaError, match="Entity identifiers"):
        cycle_vouching.validate_cycle_test(test)


def test_assertions_reject_dotted_paths_and_untyped_tolerances(procurement_contract):
    dotted = copy.deepcopy(procurement_contract["cycle_test"])
    dotted["definition"]["assertions"][0]["left"] = "vendor_invoice.amount.total"
    with pytest.raises(cycle_vouching.CycleSchemaError, match="must be an object"):
        cycle_vouching.validate_cycle_test(dotted)

    untyped = copy.deepcopy(procurement_contract["cycle_test"])
    untyped["definition"]["assertions"][0]["tolerance"] = "0.01"
    with pytest.raises(cycle_vouching.CycleSchemaError, match="must be an object"):
        cycle_vouching.validate_cycle_test(untyped)


def test_assurance_scope_is_structural_and_sampling_contract_is_closed(
    procurement_contract,
):
    test = copy.deepcopy(procurement_contract["cycle_test"])
    test["definition"]["population"]["selection"]["assurance_scope"] = (
        "sampled_population"
    )
    with pytest.raises(cycle_vouching.CycleSchemaError, match="does not match"):
        cycle_vouching.validate_cycle_test(test)

    sample = copy.deepcopy(procurement_contract["cycle_test"])
    sample["definition"]["population"]["selection"] = {
        "mode": "sample",
        "method": "random",
        "size": 25,
        "seed": 42,
    }
    validated = cycle_vouching.validate_cycle_test(sample)
    assert validated["definition"]["population"]["selection"]["assurance_scope"] == (
        "sampled_population"
    )


def _persist_cycle_test(contract: dict):
    ws = workspaces.create_workspace("Phase 0 cycle workflow")
    ws.add_rcm(
        {
            "id": "RCM-P2P-01",
            "process": "Procure to pay",
            "risk": "Payments may lack a complete procurement cycle.",
            "control": "Payments are supported by the procurement record pack.",
        }
    )
    ws = workspaces.load_workspace(ws.id)
    document_ids = {}
    for fixture_id in ("DOC-PR", "DOC-PO", "DOC-GRN", "DOC-INV", "DOC-PAY"):
        document = documents.add_document(
            ws, f"{fixture_id}.txt", f"Evidence for {fixture_id}".encode()
        )
        document_ids[fixture_id] = document["id"]
        ws = workspaces.load_workspace(ws.id)
    payload = copy.deepcopy(contract["cycle_test"])
    payload.update(
        title="Five-document procurement cycle",
        rcm_refs=["RCM-P2P-01"],
    )
    for binding in payload["items"][0]["role_bindings"]:
        binding["document_id"] = document_ids[binding["document_id"]]
    return ws, doc_tests.create_test(ws, payload)


def test_cycle_execution_expands_once_then_waits_only_for_auditor_disposition(
    procurement_contract,
):
    ws, test = _persist_cycle_test(procurement_contract)
    assert test["kind"] == "cycle_vouch"
    assert "state" not in test["items"][0]
    assert doc_test_capabilities.unexecuted_items(test) == 1
    assert [
        unit.kind
        for unit in doc_test_capabilities._execution_units(
            ws, {"test_ids": [test["id"]]}
        )
    ] == ["document_test_execution"]

    stored = doc_tests.load_test(ws, test["id"])
    stored["items"][0]["evaluation"]["state"] = "incomplete"
    stored["items"][0]["result_by_assertion"] = {
        "receipt_before_payment": {
            "verdict": "invalid_extraction",
            "registry_definition_hash": stored["registry"]["definition_hash"],
            "assertion_sha1": "sha1:assertion",
            "input_hashes": ["sha1:receipt"],
            "comparisons": [],
            "evidence_refs": [],
        }
    }
    doc_tests.save_test(ws, stored)
    current = doc_tests.load_test(ws, test["id"])

    assert doc_test_capabilities.unexecuted_items(current) == 0
    assert doc_test_capabilities._execution_units(
        ws, {"test_ids": [test["id"]]}
    ) == []
    disposition = doc_test_capabilities._dispositioned_ready(
        ws, {"test_ids": [test["id"]]}
    )
    assert disposition.state == "review_required"
    units = doc_test_capabilities._disposition_units(
        ws, {"test_ids": [test["id"]]}
    )
    assert len(units) == 1
    assert units[0].kind == "document_test_disposition"

    current["items"][0]["disposition"].update(
        state="confirmed",
        evaluated_definition_sha1=current["items"][0]["evaluation"]["definition_sha1"],
        stale=False,
    )
    doc_tests.save_test(ws, current)
    ready = doc_test_capabilities._dispositioned_ready(
        ws, {"test_ids": [test["id"]]}
    )
    assert ready.state == "satisfied"


def test_disposition_workflow_exposes_review_without_signing_off(
    procurement_contract, fake_agent_llm
):
    ws, test = _persist_cycle_test(procurement_contract)
    stored = doc_tests.load_test(ws, test["id"])
    stored["items"][0]["evaluation"]["state"] = "failed"
    stored["items"][0]["result_by_assertion"] = {
        "invoice_amount_to_payment": {
            "verdict": "mismatch",
            "registry_definition_hash": stored["registry"]["definition_hash"],
            "assertion_sha1": "sha1:assertion",
            "input_hashes": ["sha1:invoice", "sha1:payment"],
            "comparisons": [],
            "evidence_refs": [],
        }
    }
    doc_tests.save_test(ws, stored)

    created = runner.start_command_run(
        ws,
        "auto",
        {
            "text": "Review the document test result.",
            "goal_template": "document_test_disposition",
            "requested_outcomes": ["doc_tests.dispositioned"],
            "target_refs": [f"doctest:{test['id']}"],
        },
        context={"test_id": test["id"]},
    )
    finished = wait_run(ws, created["id"])
    stage = next(
        item
        for item in finished["workflow"]["stages"]
        if item["capability"] == "doc_tests.dispositioned"
    )
    assert finished["status"] == "completed_with_open_items"
    assert len(stage["units"]) == 1
    assert stage["units"][0]["status"] == "awaiting_confirmation"
    reloaded = doc_tests.load_test(ws, test["id"])
    assert reloaded["items"][0]["disposition"]["state"] == "pending"


def test_fifth_kind_meta_and_legacy_mutations_fail_closed(procurement_contract):
    ws, test = _persist_cycle_test(procurement_contract)
    assert "cycle_vouch" in doc_tests.meta_payload()["kinds"]
    item_id = test["items"][0]["id"]
    document_id = test["items"][0]["role_bindings"][0]["document_id"]
    with pytest.raises(workspaces.WorkspaceError, match="typed role binding"):
        doc_tests.attach_document(ws, test["id"], item_id, document_id)
    with pytest.raises(workspaces.WorkspaceError, match="only to vouching"):
        doc_tests.update_comparisons(ws, test["id"], item_id, [])
    with pytest.raises(workspaces.WorkspaceError, match="typed auditor disposition"):
        doc_tests.update_item(ws, test["id"], item_id, {"summary": "legacy write"})


def test_state_accessors_read_the_split_fields_for_the_four_existing_kinds():
    # Item-first tests carry the same evaluation/disposition pair as cycle
    # items, so these accessors read one shape for both.
    for kind in ("vouching", "attribute", "review", "qa"):
        test = {"kind": kind}
        assert doc_tests.item_execution_pending(
            test, {"evaluation": doc_tests.new_evaluation("not_run")}
        )
        for state in ("agent_checked", "passed", "failed", "inconclusive"):
            item = {"evaluation": doc_tests.new_evaluation(state)}
            assert doc_tests.item_execution_current(test, item)
            # A runner verdict is never itself an auditor disposition.
            assert not doc_tests.item_disposition_current(test, item)
        for state in ("confirmed", "exception"):
            item = {
                "evaluation": doc_tests.new_evaluation("passed"),
                "disposition": doc_tests.new_disposition(state),
            }
            assert doc_tests.item_disposition_current(test, item)
            assert not doc_tests.item_disposition_current(
                test,
                {**item, "disposition": doc_tests.new_disposition(state, stale=True)},
            )
        # Parking an item defers the decision rather than settling it.
        assert not doc_tests.item_disposition_current(
            test,
            {
                "evaluation": doc_tests.new_evaluation("passed"),
                "disposition": doc_tests.new_disposition("needs_review"),
            },
        )


def test_standalone_workflow_v2_declares_execution_then_auditor_disposition():
    assert doc_tests_workflow.WORKFLOW_ID == "doc_tests_workflow_v2"
    assert doc_tests_workflow.DEPENDENCIES == {
        "doc_tests.definitions_ready": (),
        "doc_tests.executed": ("doc_tests.definitions_ready",),
        "doc_tests.dispositioned": ("doc_tests.executed",),
    }
    assert doc_tests_workflow.FULL_DOC_TEST_OUTCOMES == ["doc_tests.executed"]
