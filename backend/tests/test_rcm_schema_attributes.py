"""RCM control attributes addressed by schema field, and the coverage they gate.

The pack design could not let a planning turn name fields: the record kinds were
decided later, against evidence the turn was never shown. Schemas exist by the
time the RCM is written — that is what the two new graph edges buy — so a row
names the fields outright, and coverage goes back to being selector-exact.
"""

from __future__ import annotations

import polars as pl
import pytest

from app import (
    cycle_linking,
    cycle_rulesets,
    cycle_vouching,
    document_classification,
    document_schemas,
    document_types,
    workspaces,
)

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
            "rationale": "The records must agree.",
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
        "rationale": "The records must agree.",
    }]))

    assert any("does not state" in item and "grand_total" in item for item in errors)


def test_a_document_type_with_no_schema_says_what_to_do(engagement):
    errors = errors_for(engagement, attribute(required_comparisons=[{
        "key": "totals_agree",
        "left": {"document_type": "goods_receipt", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "rationale": "The records must agree.",
    }]))

    assert any("Classify and induce" in item for item in errors)


def test_shape_alone_is_checked_without_an_engagement():
    """The response validator has no workspace in hand; the commit does."""

    validated = cycle_vouching.validate_control_attributes([attribute()])

    assert validated[0]["required_comparisons"][0]["key"] == "totals_agree"


def test_a_comparison_that_says_only_which_fields_is_refused(engagement):
    """The rationale carries what the operator used to pretend to carry.

    An attribute naming two fields and nothing else describes no requirement: a
    reader cannot tell what the fields were supposed to show, and neither can
    the pass that judges them.
    """

    errors = errors_for(engagement, attribute(required_comparisons=[{
        "key": "totals_agree",
        "left": {"document_type": "vendor_invoice", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
    }]))

    assert any("rationale is required" in item for item in errors)


def test_a_comparison_may_not_state_how_to_compare(engagement):
    """Deciding exact against normalized equality is not an audit judgment, and
    cannot be made at authoring time in any case - no value has been seen."""

    errors = errors_for(engagement, attribute(required_comparisons=[{
        "key": "totals_agree",
        "left": {"document_type": "vendor_invoice", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "rationale": "The amount billed must be the amount ordered.",
        # Built by name so the fixture sweep cannot quietly drop the very key
        # this test exists to see refused.
        **{"oper" + "ator": "equal_exact"},
    }]))

    assert any("unexpected key 'operator'" in item for item in errors)


def test_every_unexpected_key_is_reported_not_just_the_first(engagement):
    """A row carrying three is repaired in one pass or in three, and the model
    doing the repairing gets one look at each error."""

    errors = errors_for(engagement, attribute(
        registry={"pack_id": "procure_to_pay", "pack_version": 1, "definition_hash": "x"},
        required_record_kinds=["procure_to_pay.vendor_invoice"],
    ))

    assert any("unexpected key 'registry'" in item for item in errors)
    assert any("unexpected key 'required_record_kinds'" in item for item in errors)


def test_a_cycle_attribute_naming_no_comparison_is_uncontracted_not_invalid(
    engagement,
):
    """The state every matrix row now commits its cycle attributes in.

    The matrix decides that a requirement needs several source records read
    together; the cycle design, which is the stage holding the induced schemas,
    decides which fields must then agree. Between the two the attribute is
    complete and answerable and names no comparison — and refusing that would
    refuse every matrix this engagement writes.
    """

    raw = attribute()
    raw.pop("required_comparisons")

    assert errors_for(engagement, raw) == []

    (validated,) = cycle_vouching.validate_control_attributes(
        [raw], workspace=engagement
    )
    assert "required_comparisons" not in validated
    assert cycle_linking.uncontracted(validated)
    assert not cycle_linking.schema_backed(validated)


def test_an_empty_contract_is_still_refused(engagement):
    """Absent and empty are different statements.

    Absent means the cycle design has not run. Empty means a contract was
    authored and says nothing must agree, which describes no work at all.
    """

    errors = errors_for(engagement, attribute(required_comparisons=[]))

    assert any("names no evidence contract" in item for item in errors)


def test_a_contracted_attribute_is_not_uncontracted(engagement):
    (validated,) = cycle_vouching.validate_control_attributes(
        [attribute()], workspace=engagement
    )

    assert cycle_linking.schema_backed(validated)
    assert not cycle_linking.uncontracted(validated)


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
            "rationale": "The records must agree.",
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
            "rationale": "The records must agree.",
        }])]},
        ["RCM-1:invoice_match"],
    )

    uncovered = cycle_linking.uncovered_comparisons(ruleset, comparisons)
    assert [item["key"] for item in uncovered] == ["vendors_agree"]


def test_an_assertion_over_the_named_fields_covers_the_requirement(engagement):
    """Coverage is about which fields are read, not about how they compare.

    The approved assertion and the requirement name the same two fields, so the
    requirement is answered. Coverage used to demand the operators match too,
    and that is what a matrix could not satisfy: the author had to guess exact
    against normalized before any value existed to look at.
    """

    ruleset = approved(engagement)
    comparisons = cycle_linking.required_comparisons_for(
        {"id": "RCM-1", "control_attributes": [attribute(required_comparisons=[{
            "key": "totals_agree",
            "left": {"document_type": "vendor_invoice", "field": "total_amount"},
            "right": {"document_type": "purchase_order", "field": "total_amount"},
            "rationale": "The amount billed must be the amount ordered.",
        }])]},
        ["RCM-1:invoice_match"],
    )

    assert cycle_linking.uncovered_comparisons(ruleset, comparisons) == []


def test_only_the_cited_requirements_are_demanded(engagement):
    row = {"id": "RCM-1", "control_attributes": [
        attribute(),
        attribute(key="other_match", required_comparisons=[{
            "key": "vendors_agree",
            "left": {"document_type": "vendor_invoice", "field": "vendor_id"},
            "right": {"document_type": "purchase_order", "field": "vendor_id"},
            "rationale": "The records must agree.",
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
            "rationale": "The records must agree.",
        }])],
    })

    with pytest.raises(cycle_vouching.CycleSchemaError) as raised:
        build(engagement, row, [f"{row['id']}:invoice_match"])

    assert "vendor_invoice.vendor_id agrees with purchase_order.vendor_id" in str(raised.value)
    assert "cycle rules review" in str(raised.value)


# --------------------------------------------------------------- degradation
def test_a_requirement_no_ruleset_answers_is_reported_not_silently_passed(engagement):
    row = {
        "id": "RCM-1",
        "control_attributes": [attribute(required_comparisons=[{
            "key": "vendors_agree",
            "left": {"document_type": "vendor_invoice", "field": "vendor_id"},
            "right": {"document_type": "purchase_order", "field": "vendor_id"},
            "rationale": "The records must agree.",
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
def test_the_rcm_waits_for_types_but_no_longer_for_schemas():
    """The matrix needs to know which record kinds exist, not what fields they
    carry: it says a requirement needs linked source records and stops, and the
    cycle design names the fields once the schemas are induced. The dropped edge
    is what stops a re-derived schema invalidating the whole matrix."""

    from app.agent.workflows import audit

    assert audit.DEPENDENCIES["planning.rcm_ready"] == (
        "planning.apm_ready",
        # The cycle shape is a vocabulary rather than a wait: it is drafted from
        # the memorandum alone, so the matrix reaches it without the extraction
        # pass this test is about.
        "planning.cycle_ready",
        "documents.categorized",
        "documents.types_classified",
    )
    assert "documents.schemas_stamped" not in audit.DEPENDENCIES["planning.rcm_ready"]
    assert audit.DEPENDENCIES["tests.cycle_ruleset_proposed"] == (
        "planning.rcm_ready",
        "planning.cycle_ready",
        "documents.schemas_stamped",
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


def test_the_catalog_states_the_population_and_what_distinguishes_the_type(engagement):
    """Both were added after a matrix that validated perfectly and meant something
    else: every operand named a real field on a real type, which is all
    selector-exactness can check. A type carrying one document was chosen as the
    deal-record side of population-wide comparisons on the strength of its name."""

    catalog = {
        item["document_type"]: item
        for item in cycle_linking.schema_catalog(engagement)
    }

    assert catalog["vendor_invoice"]["documents"] == len(
        document_classification.documents_of_type(engagement, "vendor_invoice")
    )
    assert catalog["vendor_invoice"]["discriminator"] == (
        document_types.BY_ID["vendor_invoice"].discriminator
    )


def test_a_coined_types_discriminator_reaches_the_authoring_turn(engagement):
    """The coined type is the one the author has never seen before, so a blank
    discriminator leaves it described by its name alone."""

    document_schemas.coin_local_type(
        engagement,
        "Internal deal confirmation",
        discriminator="Entity's own system print, carrying no counterparty reference.",
    )
    document_schemas.save_schema(
        engagement,
        "local.internal_deal_confirmation",
        [{"name": "deal_reference", "role": "identifier", "value_type": "identifier"}],
        derived_from=["doc-x"],
    )
    entry = next(
        item
        for item in cycle_linking.schema_catalog(engagement)
        if item["document_type"] == "local.internal_deal_confirmation"
    )
    assert "no counterparty reference" in entry["discriminator"]
    assert entry["documents"] == 0  # coined, nothing classified onto it yet


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
        "rationale": "The records must agree.",
                 "field": "purchase_order_number"},
        "right": {"document_type": "purchase_order", "field": "order_number"},
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


def test_a_join_answers_the_requirement_whatever_mode_bound_it():
    """A check reading the fields the cycle was linked on cannot fail.

    Coverage used to ask what the join's ``match`` mode *proved* - a normalized
    join established nothing about the printed values, so it answered only a
    normalized requirement. That algebra went with the operators it compared,
    and what remains is the guard that always mattered: this pair exists only
    because those two fields already matched.
    """

    for mode in ("normalized_equal", "exact_equal"):
        ruleset = _linkage_ruleset()
        ruleset["join_keys"][0]["match"] = mode
        assert cycle_linking.uncovered_comparisons(
            ruleset, [_linkage_comparison()]
        ) == [], f"a {mode} join binds the pair"


def test_a_join_does_not_answer_the_requirement_that_a_field_be_stated():
    """A join binds two references. That one of them is present is another
    question, and one a cycle can genuinely fail."""

    assert cycle_linking.uncovered_comparisons(
        _linkage_ruleset(), [_linkage_comparison(right=None)]
    ) != []


def test_a_join_key_over_other_fields_answers_nothing():
    """Selector-exact still: a neighbouring join is a different linkage."""

    assert cycle_linking.uncovered_comparisons(
        _linkage_ruleset(),
        [_linkage_comparison(
            left={"document_type": "goods_receipt", "field": "grn_number"}
        )],
    ) != []
