"""Field pairs proposed from the schemas, and the review that keeps or declines them.

The pairs are a floor. A proposal turn asked to write a cycle's rules from the
requirements' words never compared a goods receipt's quantity with its order's
— the one comparison that finds goods billed and paid for but not delivered —
because "matched to the order and the receipt" names no quantity. Which field of
one document is which field of another is a property of the schemas, so it is
proposed from them; a pair the review does not decline is a rule.
"""

from __future__ import annotations

import json

import pytest

from app import cycle_field_pairs, cycle_rulesets, document_schemas, workspaces
from app.agent.workers.model import WorkerResponseValidationError
from app.agent.workers import cycle_rules, planning


def _field(name, value_type="text", role="attribute", label=None):
    return {"name": name, "role": role, "value_type": value_type,
            "label": label or name.replace("_", " ").capitalize()}


ORDER = {"document_type": "purchase_order", "fields": [
    _field("order_reference", "identifier", "identifier"),
    _field("vendor_name", role="party"),
    _field("vendor_address", role="party"),
    _field("quantity", "number"),
    _field("po_total", "amount", label="PO total (PKR)"),
    _field("po_date", "date"),
    _field("approved_by", role="control"),
]}
RECEIPT = {"document_type": "goods_receipt", "fields": [
    _field("grn_number", "identifier", "identifier"),
    _field("purchase_order_number", "identifier", "identifier"),
    _field("vendor_name", role="party"),
    _field("quantity_received", "number"),
    _field("receipt_date", "date"),
    _field("received_by", role="control"),
]}
INVOICE = {"document_type": "vendor_invoice", "fields": [
    _field("invoice_number", "identifier", "identifier"),
    _field("purchase_reference", "identifier", "identifier"),
    _field("invoice_amount", "amount", label="Invoice amount"),
    _field("invoice_currency"),
    _field("due_date", "date"),
]}
REQUISITION = {"document_type": "purchase_requisition", "fields": [
    _field("requisition_id", "identifier", "identifier"),
    _field("proposed_vendor", role="party"),
    _field("currency"),
]}
SCHEMAS = [REQUISITION, ORDER, RECEIPT, INVOICE]
ROLES = [
    {"name": "requisition", "document_type": "purchase_requisition"},
    {"name": "financial_approval", "document_type": "purchase_requisition"},
    {"name": "order", "document_type": "purchase_order"},
    {"name": "receipt", "document_type": "goods_receipt"},
    {"name": "invoice", "document_type": "vendor_invoice"},
]


def _pairs(schemas=SCHEMAS, roles=ROLES):
    return {
        (pair["left"]["role"], pair["left"]["field"],
         pair["right"]["role"], pair["right"]["field"]): pair
        for pair in cycle_field_pairs.candidates(schemas, roles)["pairs"]
    }


# ------------------------------------------------------------------ the floor
def test_a_shared_word_pairs_receipt_quantity_with_order_quantity():
    pair = _pairs()[("order", "quantity", "receipt", "quantity_received")]
    assert pair["basis"] == "shared_word"
    assert pair["words"] == ["quantity"]


def test_the_same_name_on_two_documents_is_a_pair():
    assert _pairs()[("order", "vendor_name", "receipt", "vendor_name")]["basis"] == "same_name"


def test_identifiers_and_controls_are_never_paired():
    """References are joins, and a control records who did one step."""

    fields = {
        side["field"]
        for pair in _pairs().values()
        for side in (pair["left"], pair["right"])
    }
    assert not fields & {"order_reference", "grn_number", "approved_by", "received_by"}


def test_dates_pair_only_under_one_name():
    """A due date and a payment date share a word and must differ."""

    assert not any(
        pair["value_type"] == "date" and pair["basis"] != "same_name"
        for pair in _pairs().values()
    )


def test_a_field_s_own_document_type_does_not_make_a_match():
    """'invoice' in an invoice's invoice_currency names the document."""

    assert ("financial_approval", "currency", "invoice", "invoice_currency") in _pairs()
    assert not any(
        pair["words"] == ["invoice"] for pair in _pairs().values()
    )


def test_another_type_s_word_still_matches():
    """'vendor' names the invoice type, and still pairs a requisition's vendor."""

    assert ("financial_approval", "proposed_vendor", "order", "vendor_name") in _pairs()


def test_ties_are_all_kept():
    """Keeping one of a tie chose bank_name over counterparty for a
    confirmation's counterparty_bank, hiding a deal booked to the wrong bank."""

    deal = {"document_type": "deal_ticket", "fields": [
        _field("deal_reference", "identifier", "identifier"),
        _field("counterparty", role="party"),
        _field("bank_name", role="party"),
    ]}
    confirmation = {"document_type": "fx_contract", "fields": [
        _field("our_reference", "identifier", "identifier"),
        _field("counterparty_bank", role="party"),
    ]}
    pairs = _pairs([deal, confirmation], [
        {"name": "deal", "document_type": "deal_ticket"},
        {"name": "confirmation", "document_type": "fx_contract"},
    ])
    assert ("deal", "counterparty", "confirmation", "counterparty_bank") in pairs
    assert ("deal", "bank_name", "confirmation", "counterparty_bank") in pairs


def test_roles_sharing_a_type_pair_from_the_last_of_them():
    """A requisition raised and then approved is one record; the approved state
    is what a later document must preserve, and pairing the two positions with
    each other would compare a field with itself."""

    roles = {pair["left"]["role"] for pair in _pairs().values()} | {
        pair["right"]["role"] for pair in _pairs().values()
    }
    assert "financial_approval" in roles
    assert "requisition" not in roles


def test_a_total_and_an_amount_pair_as_synonyms():
    """A PO total and an invoice amount are one value under two words."""

    pair = _pairs()[("order", "po_total", "invoice", "invoice_amount")]
    assert pair["words"] == ["amount/total"]
    assertion = cycle_field_pairs.as_assertion(pair, SCHEMAS, ROLES)
    assert "the synonyms 'amount' and 'total'" in assertion["rationale"]


def test_an_invoice_supplier_pairs_with_an_order_vendor():
    invoice = {**INVOICE, "fields": [*INVOICE["fields"], _field("supplier_name", role="party")]}
    pairs = _pairs([REQUISITION, ORDER, RECEIPT, invoice])
    assert ("order", "vendor_name", "invoice", "supplier_name") in pairs


def test_a_currency_code_in_a_label_is_not_a_shared_word():
    requisition = {**REQUISITION, "fields": [
        *REQUISITION["fields"],
        _field("estimated_unit_cost", "amount", label="Estimated unit cost (PKR)"),
    ]}
    pairs = _pairs([requisition, ORDER, RECEIPT, INVOICE])
    # "PKR" is all the unit cost and the PO total have in common.
    assert ("financial_approval", "estimated_unit_cost", "order", "po_total") not in pairs


def _unpaired(schemas=SCHEMAS, roles=ROLES):
    candidates = cycle_field_pairs.candidates(schemas, roles)["pairs"]
    return {
        (item["role"], item["field"]): item["no_pair_with"]
        for item in cycle_field_pairs.unpaired_fields(schemas, roles, candidates)
    }


def test_a_field_paired_with_one_document_is_still_listed_for_another():
    """Paired with the requisition is not paired with the invoice."""

    requisition = {**REQUISITION, "fields": [
        *REQUISITION["fields"], _field("approved_budget", "amount", label="Approved budget"),
    ]}
    order = {**ORDER, "fields": [
        *(field for field in ORDER["fields"] if field["name"] != "po_total"),
        _field("order_budget", "amount", label="Order budget"),
    ]}
    unpaired = _unpaired([requisition, order, RECEIPT, INVOICE])
    assert unpaired[("order", "order_budget")] == ["invoice"]


def test_a_field_paired_wherever_its_type_appears_is_not_listed():
    unpaired = _unpaired()
    assert ("order", "quantity") not in unpaired
    assert ("order", "po_total") not in unpaired
    # Dates pair only under one name, so each is asked about.
    assert unpaired[("order", "po_date")] == ["receipt", "invoice"]


def test_a_large_vocabulary_is_capped_and_says_how_much_it_left_out(monkeypatch):
    monkeypatch.setattr(cycle_field_pairs, "MAX_CANDIDATES", 2)
    result = cycle_field_pairs.candidates(SCHEMAS, ROLES)
    assert len(result["pairs"]) == 2
    assert result["omitted"] > 0
    # The strongest first: a same-name pair is never the one left out of a
    # shared-word one.
    assert result["pairs"][0]["basis"] == "same_name"


def test_a_kept_pair_is_worded_from_the_schema_labels():
    pair = _pairs()[("order", "quantity", "receipt", "quantity_received")]
    assertion = cycle_field_pairs.as_assertion(pair, SCHEMAS, ROLES)
    assert assertion["id"] == pair["id"]
    assert assertion["requirement"] == (
        "The quantity on the order agrees with the quantity received on the receipt."
    )
    assert assertion["proposed_by"] == "schema_names"
    assert cycle_rulesets.valid_rule_id(assertion["id"])


# -------------------------------------------------------------- the review
# The review is the cycle design's: the same turn places the roles and keeps
# or declines the pairs, so the pairs are proposed between document types and
# bound to the roles the response declares.
class _Item:
    def __init__(self, source_id, content):
        self.source_id, self.content = source_id, content


class _Context:
    def __init__(self, items):
        self.items = items

    def to_dict(self):
        return {"items": [{"source_id": item.source_id} for item in self.items]}


class _Request:
    """A ``planning.cycle`` request: types, one anchor table, and the schemas."""

    def __init__(self, schemas=(ORDER, RECEIPT, INVOICE)):
        self.unit_input = {
            "risk_themes": [],
            "document_types": [
                {"document_type": schema["document_type"], "documents": 1}
                for schema in schemas
            ],
        }
        self.context = _Context([
            _Item("current_apm", "# APM"),
            _Item("planning_context", {"context": {}}),
            _Item("cycle_schemas", {"schemas": list(schemas)}),
            _Item("table_metadata", {"table": "invoices",
                                     "columns": [{"name": "INVOICE_ID"}]}),
        ])


QUANTITY = "pair_purchase_order_quantity_goods_receipt_quantity_received"
TOTAL = "pair_purchase_order_po_total_vendor_invoice_invoice_amount"
VENDOR_ADDRESS_TO_NAME = "pair_purchase_order_vendor_address_goods_receipt_vendor_name"


def _design(**rules):
    response = {
        "name": "P2P",
        "steps": [
            {"name": "Purchase order", "themes": [], "populations": [],
             "roles": [{"name": "order", "document_type": "purchase_order"}]},
            {"name": "Goods receipt", "themes": [], "populations": [],
             "roles": [{"name": "receipt", "document_type": "goods_receipt"}]},
            {"name": "Invoice", "themes": [],
             "populations": [{"table": "invoices", "anchor": True}],
             "roles": [{"name": "invoice", "document_type": "vendor_invoice"}]},
        ],
        "rules": {
            "anchor": {"role": "invoice", "field": "invoice_number", "column": "INVOICE_ID"},
            "join_keys": [
                {"id": "order_to_receipt",
                 "left": {"role": "order", "field": "order_reference"},
                 "right": {"role": "receipt", "field": "purchase_order_number"},
                 "rationale": "A receipt records its order."},
                {"id": "order_to_invoice",
                 "left": {"role": "order", "field": "order_reference"},
                 "right": {"role": "invoice", "field": "purchase_reference"},
                 "rationale": "An invoice cites its order."},
            ],
            "assertions": [],
        },
    }
    response["rules"].update(rules)
    return response


def _review(response, request=None):
    """The rules half of a validated design."""

    shaped = planning._cycle_response_schema(json.dumps(response))
    return planning.validate_cycle_proposal(shaped, request or _Request())["rules"]


def _ids(rules):
    return [item["id"] for item in rules["assertions"]]


def test_the_pairs_are_shown_between_document_types():
    """Proposed before any role is placed, so their sides name types."""

    user = json.loads(planning._cycle_user(_Request()))
    pair = next(item for item in user["FIELD PAIRS"] if item["id"] == QUANTITY)
    assert pair["left"] == {"document_type": "purchase_order", "field": "quantity"}
    assert pair["right"] == {"document_type": "goods_receipt", "field": "quantity_received"}


def test_a_review_that_says_nothing_keeps_every_pair():
    """Its worst case is the floor: the receipt quantity is compared anyway."""

    rules = _review(_design())
    kept = next(item for item in rules["assertions"] if item["id"] == QUANTITY)
    # Bound onto the roles the response declared.
    assert kept["left"] == {"role": "order", "field": "quantity"}
    assert kept["right"] == {"role": "receipt", "field": "quantity_received"}
    assert rules["declined_pairs"] == []


def test_a_declined_pair_leaves_the_rules_and_is_recorded_with_its_reason():
    rules = _review(_design(declined_pairs=[{
        "id": VENDOR_ADDRESS_TO_NAME,
        "reason": "An address is not a name.",
    }]))
    assert VENDOR_ADDRESS_TO_NAME not in _ids(rules)
    (declined,) = rules["declined_pairs"]
    assert declined["reason"] == "An address is not a name."
    assert declined["left"] == {"role": "order", "field": "vendor_address"}


def test_a_decline_without_a_reason_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="with no reason"):
        _review(_design(declined_pairs=[{"id": QUANTITY, "reason": ""}]))


def test_a_decline_naming_no_supplied_pair_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="not one of the field pairs"):
        _review(_design(declined_pairs=[{"id": "pair_invented", "reason": "x"}]))


def test_an_authored_rule_over_a_pair_s_fields_replaces_it():
    authored = {
        "id": "received_quantity_agrees", "label": "Received in full",
        "left": {"role": "receipt", "field": "quantity_received"},
        "right": {"role": "order", "field": "quantity"},
        "requirement": "Every unit ordered was received.",
        "rationale": "Billing in full needs receipt in full.",
    }
    rules = _review(_design(assertions=[authored]))
    assert "received_quantity_agrees" in _ids(rules)
    assert QUANTITY not in _ids(rules)


def test_a_pair_both_declined_and_written_is_refused():
    authored = {
        "id": "received_quantity_agrees", "label": "Received in full",
        "left": {"role": "receipt", "field": "quantity_received"},
        "right": {"role": "order", "field": "quantity"},
        "requirement": "Every unit ordered was received.", "rationale": "x",
    }
    with pytest.raises(WorkerResponseValidationError, match="Keep one"):
        _review(_design(assertions=[authored],
                        declined_pairs=[{"id": QUANTITY, "reason": "Not comparable."}]))


def test_a_rule_restating_a_join_key_is_refused():
    """The join made the two references equal; the rule cannot fail."""

    authored = {
        "id": "receipt_cites_order", "label": "Receipt cites order",
        "left": {"role": "receipt", "field": "purchase_order_number"},
        "right": {"role": "order", "field": "order_reference"},
        "requirement": "The receipt names the order.", "rationale": "x",
    }
    with pytest.raises(WorkerResponseValidationError, match="cannot fail"):
        _review(_design(assertions=[authored]))


def test_pairs_on_a_role_the_rules_cannot_reach_are_dropped():
    """A role listed unreachable is out of the rules, and so are its pairs."""

    response = _design(
        join_keys=[{"id": "order_to_invoice",
                    "left": {"role": "order", "field": "order_reference"},
                    "right": {"role": "invoice", "field": "purchase_reference"},
                    "rationale": "x"}],
        unreachable=[{"role": "receipt", "reason": "No identifier."}],
    )
    rules = _review(response)
    assert [role["name"] for role in rules["roles"]] == ["order", "invoice"]
    assert not any("goods_receipt" in assertion_id for assertion_id in _ids(rules))


def test_a_pair_written_out_under_its_own_id_is_the_pair_kept():
    """Measured live: the review restates kept pairs under the ids it was given."""

    authored = {
        "id": QUANTITY, "label": "Received in full",
        "left": {"role": "order", "field": "quantity"},
        "right": {"role": "receipt", "field": "quantity_received"},
        "requirement": "Every unit ordered was received.", "rationale": "x",
    }
    rules = _review(_design(assertions=[authored]))
    (kept,) = [item for item in rules["assertions"] if item["id"] == QUANTITY]
    assert kept["requirement"] == "Every unit ordered was received."


def test_a_pair_s_id_over_other_fields_is_refused():
    authored = {
        "id": QUANTITY, "label": "x",
        "left": {"role": "order", "field": "po_total"},
        "right": {"role": "invoice", "field": "invoice_amount"},
        "requirement": "x", "rationale": "x",
    }
    with pytest.raises(WorkerResponseValidationError, match="compares different fields"):
        _review(_design(assertions=[authored]))


def test_declining_a_pair_on_a_role_it_cannot_reach_is_accepted():
    """Measured live: a statement with no identifier was declined, correctly."""

    response = _design(
        join_keys=[{"id": "order_to_invoice",
                    "left": {"role": "order", "field": "order_reference"},
                    "right": {"role": "invoice", "field": "purchase_reference"},
                    "rationale": "x"}],
        unreachable=[{"role": "receipt", "reason": "No identifier."}],
        declined_pairs=[{"id": QUANTITY, "reason": "The receipt is unreachable."}],
    )
    assert _review(response)["declined_pairs"] == []


# --------------------------------------------- coverage, against kept pairs
# Which of the approved assertions answer a requirement is said when the
# requirement's test is written; the checks are the ones the linkage turn ran.
def _coverage(requirement, entry, rules=None):
    rules = rules or _review(_design())
    return cycle_rules.coverage_problems(
        ("RCM-1", "match"), requirement, entry,
        assertions=rules["assertions"],
        roles={role["name"]: role["document_type"] for role in rules["roles"]},
    )


def test_a_kept_pair_answers_the_reach_check():
    """Citing the floor's pairs reads every document the match names."""

    assert _coverage(
        "The invoice is matched to the order and receipt.",
        {"reads": ["order", "receipt", "invoice"], "assertion_ids": [QUANTITY, TOTAL]},
    ) == []


def test_coverage_citing_a_declined_pair_is_refused():
    rules = _review(_design(declined_pairs=[{"id": QUANTITY, "reason": "Not comparable."}]))
    problems = _coverage(
        "The receipt agrees with the order.",
        {"reads": ["order", "receipt"], "assertion_ids": [QUANTITY]},
        rules,
    )
    assert problems and "do not hold" in problems[0]


def test_a_match_answered_from_the_invoice_alone_is_refused():
    """Measured live: "matched to the purchase order and the goods receipt
    note" answered by two checks that the invoice cites them, with reads naming
    the invoice and nothing else."""

    cites = {
        "id": "invoice_cites_po", "label": "Invoice cites its order",
        "left": {"role": "invoice", "field": "purchase_reference"}, "right": None,
        "requirement": "The invoice states its PO.", "rationale": "x",
    }
    rules = _review(_design(assertions=[cites]))
    message = " ".join(_coverage(
        "The invoice is matched to the purchase order and the goods receipt note.",
        {"reads": ["invoice"], "assertion_ids": ["invoice_cites_po"]},
        rules,
    ))
    assert "names the purchase order (roles 'order')" in message
    assert "names the goods receipt (roles 'receipt')" in message
    assert "names the vendor invoice" not in message


def test_a_named_document_may_be_stated_as_a_gap():
    assert _coverage(
        "The invoice is matched to the order and receipt.",
        {"reads": ["order", "receipt", "invoice"], "assertion_ids": [QUANTITY],
         "gaps": [{"role": "invoice", "reason": "No invoice field states what was billed."}]},
    ) == []


# --------------------------------------------------------------- the store
def test_declines_are_stored_beside_the_rules_and_outside_their_hash():
    workspace = workspaces.create_workspace("Field pair store")
    for schema in (ORDER, INVOICE):
        document_schemas.save_schema(workspace, schema["document_type"], schema["fields"])
    workspace = workspaces.load_workspace(workspace.id)
    payload = {
        "cycle_label": "P2P",
        "roles": [{"name": "order", "document_type": "purchase_order"},
                  {"name": "invoice", "document_type": "vendor_invoice"}],
        "anchor": {"table": "invoices", "column": "INVOICE_ID",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{"id": "order_to_invoice",
                       "left": {"role": "order", "field": "order_reference"},
                       "right": {"role": "invoice", "field": "purchase_reference"}}],
        "assertions": [{"id": "pair_x", "label": "x", "requirement": "x",
                        "left": {"role": "order", "field": "po_total"},
                        "right": {"role": "invoice", "field": "invoice_amount"},
                        "proposed_by": "schema_names"}],
    }
    plain = cycle_rulesets.validate(workspace, payload)
    declined = cycle_rulesets.validate(workspace, {**payload, "declined_pairs": [{
        "id": "pair_y", "left": {"role": "order", "field": "vendor_name"},
        "right": {"role": "invoice", "field": "invoice_currency"},
        "basis": "shared_word", "reason": "A name is not a currency.",
    }]})

    assert declined["declined_pairs"][0]["reason"] == "A name is not a currency."
    assert declined["ruleset_hash"] == plain["ruleset_hash"]
    assert declined["assertions"][0]["proposed_by"] == "schema_names"


# ------------------------------------------------- documents a requirement names
_P2P = ["purchase_requisition", "purchase_order", "goods_receipt", "vendor_invoice",
        "payment_voucher"]


@pytest.mark.parametrize("sentence, expected", [
    ("The invoice is matched to the purchase order and the goods receipt note.",
     ["purchase_order", "goods_receipt", "vendor_invoice"]),
    ("The GRN is signed by the receiving department.", ["goods_receipt"]),
    ("The vendor on the PO is the vendor on the requisition.",
     ["purchase_requisition", "purchase_order"]),
    ("Invoices are approved before vouchers are paid.",
     ["vendor_invoice", "payment_voucher"]),
    # "order", "note" and "request" are ordinary words; "po" is not the PO.
    ("In order to pay, note the request; the po box is stated.", []),
])
def test_the_documents_a_requirement_names_are_read_from_its_words(sentence, expected):
    from app import document_types

    assert document_types.mentioned(sentence, _P2P) == expected


def test_a_local_type_is_named_by_its_own_words():
    from app import document_types

    assert document_types.mentioned(
        "The loan agreement states the facility limit.", ["local.loan_agreement"]
    ) == ["local.loan_agreement"]
