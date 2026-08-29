"""RCM control attributes addressed by schema field, and the coverage they gate.

The pack design could not let a planning turn name fields: the record kinds were
decided later, against evidence the turn was never shown. Schemas exist by the
time the RCM is written — that is what the two new graph edges buy — so a row
names the fields outright, and coverage goes back to being selector-exact.
"""

from __future__ import annotations

import polars as pl
import pytest

from app import cycle_linking, cycle_rulesets, cycle_vouching, document_schemas, workspaces

from tests.test_cycle_linking import (  # noqa: F401 - helpers reused
    INVOICE_FIELDS,
    ORDER_FIELDS,
    approved,
    extract,
    ruleset_payload,
)


def attribute(**overrides) -> dict:
    payload = {
        "key": "invoice_match",
        "assertion": "Accuracy",
        "requirement": "The invoice agrees to the purchase order it bills against.",
        "evidence_kind": "transaction_cycle",
        "required_comparisons": [{
            "key": "totals_agree",
            "left": {"document_type": "vendor_invoice", "field": "total_amount"},
            "right": {"document_type": "purchase_order", "field": "total_amount"},
            "operator": "numeric_within",
            "tolerance": {"absolute": 1},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def engagement():
    ws = workspaces.create_workspace("Schema-addressed RCM")
    document_schemas.save_schema(ws, "vendor_invoice", INVOICE_FIELDS)
    document_schemas.save_schema(ws, "purchase_order", ORDER_FIELDS)
    ws.add_table(
        "invoices.csv",
        pl.DataFrame({"INVOICE_NO": ["INV-1"], "AMOUNT": [100.0]}).write_csv().encode(),
    )
    extract(ws, "inv1.txt", "vendor_invoice", invoice_number="INV-1",
            order_number="PO-1", total_amount="100")
    extract(ws, "po1.txt", "purchase_order", order_number="PO-1", total_amount="100")
    return ws


def errors_for(ws, value) -> list[str]:
    try:
        cycle_vouching.validate_control_attributes([value], workspace=ws)
    except cycle_vouching.CycleSchemaError as error:
        return list(error.errors)
    return []


# ------------------------------------------------------------------ contract
def test_a_row_names_the_fields_that_must_agree(engagement):
    validated = cycle_vouching.validate_control_attributes(
        [attribute()], workspace=engagement
    )

    assert validated[0]["required_comparisons"][0]["left"] == {
        "document_type": "vendor_invoice", "field": "total_amount"
    }
    assert "registry" not in validated[0]
    assert "comparison_recipes" not in validated[0]


def test_a_field_no_schema_states_fails_closed(engagement):
    """Not swapped for a related one: a requirement pointing at a field the
    schema does not state is a requirement nothing can answer."""

    errors = errors_for(engagement, attribute(required_comparisons=[{
        "key": "totals_agree",
        "left": {"document_type": "vendor_invoice", "field": "grand_total"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "operator": "numeric_within",
    }]))

    assert any("does not state" in item and "grand_total" in item for item in errors)


def test_a_document_type_with_no_schema_says_what_to_do(engagement):
    errors = errors_for(engagement, attribute(required_comparisons=[{
        "key": "totals_agree",
        "left": {"document_type": "goods_receipt", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "operator": "numeric_within",
    }]))

    assert any("Classify and induce" in item for item in errors)


def test_shape_alone_is_checked_without_an_engagement():
    """The response validator has no workspace in hand; the commit does."""

    validated = cycle_vouching.validate_control_attributes([attribute()])

    assert validated[0]["required_comparisons"][0]["operator"] == "numeric_within"


def test_an_unsupported_operator_is_named_with_the_ones_that_are(engagement):
    errors = errors_for(engagement, attribute(required_comparisons=[{
        "key": "totals_agree",
        "left": {"document_type": "vendor_invoice", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "operator": "equals",
    }]))

    assert any("'equals' is not supported" in item for item in errors)
    assert any("numeric_within" in item for item in errors)


def test_every_unexpected_key_is_reported_not_just_the_first(engagement):
    """A row carrying three is repaired in one pass or in three, and the model
    doing the repairing gets one look at each error."""

    errors = errors_for(engagement, attribute(
        registry={"pack_id": "procure_to_pay", "pack_version": 1, "definition_hash": "x"},
        required_record_kinds=["procure_to_pay.vendor_invoice"],
    ))

    assert any("unexpected key 'registry'" in item for item in errors)
    assert any("unexpected key 'required_record_kinds'" in item for item in errors)


def test_only_a_transaction_cycle_attribute_requires_comparisons(engagement):
    errors = errors_for(engagement, attribute(evidence_kind="inquiry"))

    assert any("only a transaction_cycle attribute may do" in item for item in errors)


# ------------------------------------------------------------------ coverage
def test_an_approved_assertion_covers_the_comparison_it_answers(engagement):
    ruleset = approved(engagement)
    comparisons = cycle_linking.required_comparisons_for(
        {"id": "RCM-1", "control_attributes": [attribute()]}, ["RCM-1:invoice_match"]
    )

    assert cycle_linking.uncovered_comparisons(ruleset, comparisons) == []


def test_the_operands_may_be_written_either_way_round(engagement):
    """An equality stated in the other order is the same requirement."""

    ruleset = approved(engagement)
    comparisons = cycle_linking.required_comparisons_for(
        {"id": "RCM-1", "control_attributes": [attribute(required_comparisons=[{
            "key": "totals_agree",
            "left": {"document_type": "purchase_order", "field": "total_amount"},
            "right": {"document_type": "vendor_invoice", "field": "total_amount"},
            "operator": "numeric_within",
            "tolerance": {"absolute": 1},
        }])]},
        ["RCM-1:invoice_match"],
    )

    assert cycle_linking.uncovered_comparisons(ruleset, comparisons) == []


def test_a_neighbouring_assertion_does_not_count_as_coverage(engagement):
    """A related test over neighbouring fields is a different test, and
    accepting it would let a control read as covered by work that did not
    address it."""

    ruleset = approved(engagement)
    comparisons = cycle_linking.required_comparisons_for(
        {"id": "RCM-1", "control_attributes": [attribute(required_comparisons=[{
            "key": "vendors_agree",
            "left": {"document_type": "vendor_invoice", "field": "vendor_id"},
            "right": {"document_type": "purchase_order", "field": "vendor_id"},
            "operator": "equal_normalized",
        }])]},
        ["RCM-1:invoice_match"],
    )

    uncovered = cycle_linking.uncovered_comparisons(ruleset, comparisons)
    assert [item["key"] for item in uncovered] == ["vendors_agree"]


def test_a_different_tolerance_is_a_different_requirement(engagement):
    ruleset = approved(engagement)
    comparisons = cycle_linking.required_comparisons_for(
        {"id": "RCM-1", "control_attributes": [attribute(required_comparisons=[{
            "key": "totals_agree",
            "left": {"document_type": "vendor_invoice", "field": "total_amount"},
            "right": {"document_type": "purchase_order", "field": "total_amount"},
            "operator": "numeric_within",
            "tolerance": {"absolute": 500},
        }])]},
        ["RCM-1:invoice_match"],
    )

    assert cycle_linking.uncovered_comparisons(ruleset, comparisons)


def test_only_the_cited_requirements_are_demanded(engagement):
    row = {"id": "RCM-1", "control_attributes": [
        attribute(),
        attribute(key="other_match", required_comparisons=[{
            "key": "vendors_agree",
            "left": {"document_type": "vendor_invoice", "field": "vendor_id"},
            "right": {"document_type": "purchase_order", "field": "vendor_id"},
            "operator": "equal_normalized",
        }]),
    ]}

    comparisons = cycle_linking.required_comparisons_for(row, ["RCM-1:invoice_match"])

    assert [item["key"] for item in comparisons] == ["totals_agree"]


# ------------------------------------------------------------ the build gate
def build(ws, row, refs):
    return cycle_vouching.build_cycle_vouch_test(ws, {
        "title": "Vouch invoices to orders",
        "objective": "Vouch each invoice to its approved order.",
        "rcm_id": str(row["id"]),
        "procedure_key": "match_invoice_to_order",
        "requirement_refs": refs,
        "definition": {"population": {"selection": {"mode": "evidence_linked"}}},
    })


def test_a_covered_requirement_builds(engagement):
    approved(engagement)
    row = engagement.add_rcm({
        "process": "Procure to pay", "risk": "Unordered purchases",
        "risk_rating": "high", "control": "Invoices are matched to orders.",
        "control_attributes": [attribute()],
    })

    test = build(engagement, row, [f"{row['id']}:invoice_match"])

    assert test["definition"]["ruleset_id"]


def test_an_uncovered_requirement_refuses_the_build_and_says_where_to_fix_it(engagement):
    approved(engagement)
    row = engagement.add_rcm({
        "process": "Procure to pay", "risk": "Unordered purchases",
        "risk_rating": "high", "control": "Invoices are matched to orders.",
        "control_attributes": [attribute(required_comparisons=[{
            "key": "vendors_agree",
            "left": {"document_type": "vendor_invoice", "field": "vendor_id"},
            "right": {"document_type": "purchase_order", "field": "vendor_id"},
            "operator": "equal_normalized",
        }])],
    })

    with pytest.raises(cycle_vouching.CycleSchemaError) as raised:
        build(engagement, row, [f"{row['id']}:invoice_match"])

    assert "vendor_invoice.vendor_id equal_normalized" in str(raised.value)
    assert "cycle rules review" in str(raised.value)


# --------------------------------------------------------------- degradation
def test_a_requirement_no_ruleset_answers_is_reported_not_silently_passed(engagement):
    row = {
        "id": "RCM-1",
        "control_attributes": [attribute(required_comparisons=[{
            "key": "vendors_agree",
            "left": {"document_type": "vendor_invoice", "field": "vendor_id"},
            "right": {"document_type": "purchase_order", "field": "vendor_id"},
            "operator": "equal_normalized",
        }])],
    }
    approved(engagement)

    notes = cycle_vouching.unanswerable_cycle_requirements(engagement, row)

    assert notes
    assert "is untested" in notes[0]


def test_no_approved_ruleset_at_all_is_its_own_note(engagement):
    cycle_rulesets.save(engagement, ruleset_payload(), proposed_by="agent")
    row = {"id": "RCM-1", "control_attributes": [attribute()]}

    notes = cycle_vouching.unanswerable_cycle_requirements(engagement, row)

    assert any("no approved cycle ruleset" in note for note in notes)


def test_a_field_the_schemas_lost_is_reported_as_an_evidence_gap(engagement):
    """Separate from an uncovered rule: they are repaired in different places."""

    approved(engagement)
    document_schemas.save_schema(engagement, "purchase_order", [
        field for field in ORDER_FIELDS if field["name"] != "total_amount"
    ])
    row = {"id": "RCM-1", "control_attributes": [attribute()]}

    notes = cycle_vouching.unanswerable_cycle_requirements(engagement, row)

    assert any("cannot express" in note for note in notes)


def test_a_row_with_no_cycle_attribute_reports_nothing(engagement):
    row = {"id": "RCM-1", "control_attributes": [
        {"key": "k", "assertion": "Operational", "requirement": "r",
         "evidence_kind": "manual_inspection"},
    ]}

    assert cycle_vouching.unanswerable_cycle_requirements(engagement, row) == []


# ------------------------------------------------------------------- ordering
def test_the_rcm_waits_for_types_and_schemas():
    """Both run over the whole document set rather than the planning-scoped
    subset, which is what puts voucher schemas in hand when the RCM is written."""

    from app.agent.workflows import audit

    assert audit.DEPENDENCIES["planning.rcm_ready"] == (
        "planning.apm_ready",
        "documents.types_classified",
        "documents.schemas_induced",
    )


# ----------------------------------------------------- the authoring turn
def test_the_catalog_shows_only_what_a_comparison_can_address(engagement):
    """Not how many samples induced the schema or how confident it was: a
    requirement is written against what the documents state."""

    catalog = cycle_linking.schema_catalog(engagement)

    assert sorted(item["document_type"] for item in catalog) == [
        "purchase_order", "vendor_invoice"
    ]
    field = next(
        item for item in catalog if item["document_type"] == "vendor_invoice"
    )["fields"][0]
    assert sorted(field) == ["label", "name", "role", "value_type"]


def test_the_schema_evidence_prompt_states_the_operator_vocabulary():
    from app.agent.workers import planning as planning_worker

    prompt = planning_worker.RCM_SCHEMA_EVIDENCE_SYSTEM

    assert "required_comparisons" in prompt
    assert "numeric_within" in prompt
    assert "document_type" in prompt
    # The vocabulary is per workspace, so it must not be baked into the prompt.
    assert "vendor_invoice" not in prompt


def test_the_contract_is_written_onto_the_attribute_that_asked_for_it():
    import json

    from app.agent.workers import planning as planning_worker

    rows = [{
        "process": "Procure to pay",
        "control_attributes": [{
            "key": "invoice_match",
            "assertion": "Accuracy",
            "requirement": "The invoice agrees to the order.",
            "evidence_kind": "transaction_cycle",
        }],
    }]
    response = json.dumps({"contracts": [{
        "row_index": 1,
        "attribute_key": "invoice_match",
        "required_comparisons": [{
            "key": "totals_agree",
            "left": {"document_type": "vendor_invoice", "field": "total_amount"},
            "right": {"document_type": "purchase_order", "field": "total_amount"},
            "operator": "numeric_within",
            "tolerance": {"absolute": 1},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }]})

    merged = planning_worker._merge_evidence_contracts(rows, response)
    attribute = merged[0]["control_attributes"][0]

    assert attribute["required_comparisons"][0]["key"] == "totals_agree"
    assert "registry" not in attribute
    assert "comparison_recipes" not in attribute


def test_an_unsupported_requirement_is_left_uncontracted():
    """Quietly inventing a comparison here would answer a different question
    than the requirement asked."""

    import json

    from app.agent.workers import planning as planning_worker

    rows = [{"control_attributes": [{
        "key": "invoice_match", "assertion": "Accuracy",
        "requirement": "The buyer visited the vendor's premises.",
        "evidence_kind": "transaction_cycle",
    }]}]
    response = json.dumps({"contracts": [{
        "row_index": 1, "attribute_key": "invoice_match",
        "unsupported": True, "reason": "No document states a site visit.",
    }]})

    merged = planning_worker._merge_evidence_contracts(rows, response)

    assert "required_comparisons" not in merged[0]["control_attributes"][0]


def test_an_already_contracted_attribute_is_not_asked_again():
    from app.agent.workers import planning as planning_worker

    rows = [{"control_attributes": [attribute()]}]

    assert planning_worker._cycle_attribute_requests(rows) == []


# ------------------------------------------- a linkage requirement is a join
# The matrix authored `link_purchase_order` — that a goods receipt references
# its order — as a required comparison. The approved rules expressed it as the
# join key that binds the two, and the build refused for want of an assertion.

def _linkage_ruleset():
    return {
        "roles": [
            {"name": "receipt", "document_type": "goods_receipt",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "join_keys": [{
            "id": "po_to_grn", "match": "exact_equal",
            "left": {"role": "order", "field": "order_number"},
            "right": {"role": "receipt", "field": "purchase_order_number"},
        }],
        "assertions": [],
    }


def _linkage_comparison(**overrides):
    comparison = {
        "key": "link_purchase_order",
        "left": {"document_type": "goods_receipt",
                 "field": "purchase_order_number"},
        "right": {"document_type": "purchase_order", "field": "order_number"},
        "operator": "equal_exact",
    }
    comparison.update(overrides)
    return comparison


def test_a_join_key_answers_the_requirement_that_two_documents_reference_each_other():
    """The assertion it would otherwise demand is one that cannot fail.

    The pair an assertion would test exists only because the join already
    matched, so repeating the equality files a test incapable of finding an
    exception — coverage that proves nothing, which is the same defect the
    data-test validity gate refuses.
    """

    assert cycle_linking.uncovered_comparisons(
        _linkage_ruleset(), [_linkage_comparison()]
    ) == []


def test_a_normalized_join_does_not_answer_an_exact_requirement():
    """It bound the pair on folded values and says nothing about the printed ones."""

    ruleset = _linkage_ruleset()
    ruleset["join_keys"][0]["match"] = "normalized_equal"
    assert cycle_linking.uncovered_comparisons(
        ruleset, [_linkage_comparison()]
    ) != []
    # The normalized requirement it does answer.
    assert cycle_linking.uncovered_comparisons(
        ruleset, [_linkage_comparison(operator="equal_normalized")]
    ) == []


def test_a_join_key_answers_nothing_but_equality():
    """It says these two references are the same, and nothing about an amount."""

    for operator in ("numeric_within", "date_on_or_before", "present"):
        assert cycle_linking.uncovered_comparisons(
            _linkage_ruleset(), [_linkage_comparison(operator=operator)]
        ) != [], f"{operator} must not be covered by a join"


def test_a_join_key_over_other_fields_answers_nothing():
    """Selector-exact still: a neighbouring join is a different linkage."""

    assert cycle_linking.uncovered_comparisons(
        _linkage_ruleset(),
        [_linkage_comparison(
            left={"document_type": "goods_receipt", "field": "grn_number"}
        )],
    ) != []
