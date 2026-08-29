"""Cycle ruleset validation, approval, and measurement."""

from __future__ import annotations

import pytest

from app import cycle_rulesets, document_schemas, workspaces
from app.cycle_rulesets import RulesetError


INVOICE_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "purchase_order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]

ORDER_FIELDS = [
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "approval", "role": "control", "value_type": "text",
     "cardinality": "many", "verbatim": False, "confidence": "medium"},
]


@pytest.fixture
def ws() -> workspaces.Workspace:
    workspace = workspaces.create_workspace("Ruleset Engagement")
    document_schemas.save_schema(workspace, "vendor_invoice", INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", ORDER_FIELDS)
    return workspace


def _payload(**overrides) -> dict:
    payload = {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice", "cardinality": "one"},
            {"name": "order", "document_type": "purchase_order", "cardinality": "one"},
        ],
        "anchor": {"table": "expense_register", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [
            {"id": "jk_po", "left": {"role": "invoice", "field": "purchase_order_number"},
             "right": {"role": "order", "field": "order_number"},
             "match": "normalized_equal", "rationale": "An invoice cites its order."},
        ],
        "assertions": [
            {"id": "as_total", "requirement": "The records must agree.", "label": "Totals agree", "left": {"role": "invoice", "field": "total_amount"},
             "right": {"role": "order", "field": "total_amount"}},
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------- happy path
def test_save_validates_and_hashes(ws):
    record = cycle_rulesets.save(ws, _payload())
    assert record["status"] == "proposed"
    assert record["ruleset_hash"].startswith("sha256:")
    assert record["approved_by"] is None
    assert [ref["document_type"] for ref in record["schema_refs"]] == [
        "purchase_order", "vendor_invoice"
    ]
    assert cycle_rulesets.get(ws, record["ruleset_id"]) == record


def test_hash_covers_rules_only(ws):
    """Two rulesets with identical rules hash the same regardless of label."""

    first = cycle_rulesets.save(ws, _payload())
    second = cycle_rulesets.save(ws, _payload(cycle_label="Totally different name"))
    assert second["ruleset_hash"] == first["ruleset_hash"]


# --------------------------------------------------------------- join keys
def test_join_key_must_use_identifier_fields(ws):
    """Joining on an amount would fuse unrelated transactions."""

    payload = _payload()
    payload["join_keys"][0]["left"] = {"role": "invoice", "field": "total_amount"}
    with pytest.raises(RulesetError, match="only identifier fields can join"):
        cycle_rulesets.save(ws, payload)


def test_rule_naming_a_field_the_schema_lacks_fails_closed(ws):
    payload = _payload()
    payload["assertions"][0]["left"] = {"role": "invoice", "field": "vat_amount"}
    with pytest.raises(RulesetError, match="does not state"):
        cycle_rulesets.save(ws, payload)


def test_unreachable_role_is_rejected(ws):
    """A role no join key reaches would silently never bind."""

    payload = _payload(join_keys=[])
    with pytest.raises(RulesetError, match="No join key reaches 'order'"):
        cycle_rulesets.save(ws, payload)


def test_anchor_must_be_an_identifier(ws):
    payload = _payload()
    payload["anchor"]["field"] = "total_amount"
    with pytest.raises(RulesetError, match="anchor must name an identifier"):
        cycle_rulesets.save(ws, payload)


# --------------------------------------------------------------- assertions
def test_an_assertion_reading_one_field_needs_no_right_operand(ws):
    """Omitting the right side is the requirement that a field be stated."""

    payload = _payload()
    payload["assertions"][0] = {
        "id": "as_approval",
        "requirement": "The order carries an approval.",
        "left": {"role": "order", "field": "approval"},
        "right": None,
    }
    record = cycle_rulesets.save(ws, payload)

    assert record["assertions"][0]["right"] is None


def test_an_assertion_states_what_the_fields_must_show(ws):
    """An approved rule a reader cannot act on is not an approved rule.

    The requirement carries what the comparison operator used to pretend to
    carry. "The invoice is settled for the amount the order committed" is
    something an auditor can approve and a reader can apply; `equal_exact` was
    a string operation neither of them asked for.
    """

    payload = _payload()
    payload["assertions"][0].pop("requirement", None)
    payload["assertions"][0].pop("rationale", None)

    with pytest.raises(RulesetError, match="states no requirement"):
        cycle_rulesets.save(ws, payload)


def test_an_assertion_may_not_state_how_to_compare(ws):
    """Approving a ruleset is approving what the cycle must demonstrate.

    Exact against normalized equality is not the auditor's judgment to make,
    and could not be made here in any case: no value has been read yet.
    """

    payload = _payload()
    payload["assertions"][0]["operator"] = "equal_exact"
    record = cycle_rulesets.save(ws, payload)

    assert "operator" not in record["assertions"][0]
    assert "tolerance" not in record["assertions"][0]


def test_duplicate_role_and_rule_ids_are_rejected(ws):
    with pytest.raises(RulesetError, match="declared twice"):
        cycle_rulesets.save(ws, _payload(roles=[
            {"name": "invoice", "document_type": "vendor_invoice"},
            {"name": "invoice", "document_type": "purchase_order"},
        ]))


def test_two_roles_may_share_a_document_type(ws):
    """The addressing scheme is roles, so an original and a revision both fit."""

    payload = _payload(
        roles=[
            {"name": "invoice", "document_type": "vendor_invoice"},
            {"name": "revised_invoice", "document_type": "vendor_invoice"},
        ],
        join_keys=[{
            "id": "jk_same", "match": "normalized_equal",
            "left": {"role": "invoice", "field": "purchase_order_number"},
            "right": {"role": "revised_invoice", "field": "purchase_order_number"},
        }],
        assertions=[{
            "id": "as_total", "requirement": "The records must agree.", "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "revised_invoice", "field": "total_amount"},
        }],
    )
    record = cycle_rulesets.save(ws, payload)
    assert {role["document_type"] for role in record["roles"]} == {"vendor_invoice"}


def test_other_cannot_fill_a_role(ws):
    with pytest.raises(RulesetError, match="retype first"):
        cycle_rulesets.save(ws, _payload(roles=[
            {"name": "invoice", "document_type": "other"},
        ]))


# --------------------------------------------------------------- approval
def test_approval_requires_an_identity_and_supersedes_the_previous(ws):
    first = cycle_rulesets.save(ws, _payload())
    approved = cycle_rulesets.approve(ws, first["ruleset_id"], approved_by="auditor@example.com")
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "auditor@example.com"
    assert cycle_rulesets.effective(ws)["ruleset_id"] == first["ruleset_id"]

    second = cycle_rulesets.save(ws, _payload(cycle_label="Successor"))
    cycle_rulesets.approve(ws, second["ruleset_id"], approved_by="auditor@example.com")
    assert cycle_rulesets.effective(ws)["ruleset_id"] == second["ruleset_id"]
    assert cycle_rulesets.get(ws, first["ruleset_id"])["status"] == "superseded"


def test_approval_without_an_identity_is_refused(ws):
    record = cycle_rulesets.save(ws, _payload())
    with pytest.raises(RulesetError, match="approved_by is required"):
        cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="")


def test_approved_ruleset_is_immutable(ws):
    record = cycle_rulesets.save(ws, _payload())
    cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="auditor")
    with pytest.raises(RulesetError, match="propose a successor"):
        cycle_rulesets.save(ws, _payload(), ruleset_id=record["ruleset_id"])
    with pytest.raises(RulesetError, match="supersede it instead"):
        cycle_rulesets.reject(ws, record["ruleset_id"])


def test_approval_blocks_when_a_referenced_field_has_vanished(ws):
    """A schema that moved under a proposal must block approval, naming the field."""

    record = cycle_rulesets.save(ws, _payload())
    document_schemas.save_schema(
        ws, "vendor_invoice",
        [field for field in INVOICE_FIELDS if field["name"] != "total_amount"],
    )
    with pytest.raises(RulesetError, match="'total_amount', which 'vendor_invoice' does not state"):
        cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="auditor")
    assert cycle_rulesets.effective(ws) is None


def test_approval_refreshes_schema_refs_when_a_schema_merely_grew(ws):
    """Every rule still resolves, so approval proceeds — but the stored refs must
    describe what was actually checked, not the version the proposal saw."""

    record = cycle_rulesets.save(ws, _payload())
    proposed_version = {
        ref["document_type"]: ref["schema_version"] for ref in record["schema_refs"]
    }
    assert proposed_version["vendor_invoice"] == 1
    document_schemas.save_schema(
        ws, "vendor_invoice",
        [*INVOICE_FIELDS,
         {"name": "currency", "role": "attribute", "value_type": "text",
          "cardinality": "one", "verbatim": True, "confidence": "medium"}],
    )
    approved = cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="auditor")
    refreshed = {ref["document_type"]: ref["schema_version"] for ref in approved["schema_refs"]}
    assert refreshed["vendor_invoice"] == 2
    assert approved["ruleset_hash"] == record["ruleset_hash"]


def test_no_effective_ruleset_before_approval(ws):
    cycle_rulesets.save(ws, _payload())
    assert cycle_rulesets.effective(ws) is None
    assert not cycle_rulesets.is_current(ws, "sha256:anything")


def test_is_current_tracks_the_effective_hash(ws):
    record = cycle_rulesets.save(ws, _payload())
    cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="auditor")
    assert cycle_rulesets.is_current(ws, record["ruleset_hash"])
    assert not cycle_rulesets.is_current(ws, "sha256:stale")


# --------------------------------------------------------------- measurement
def test_measurement_attaches_without_moving_the_hash(ws):
    record = cycle_rulesets.save(ws, _payload())
    measured = cycle_rulesets.set_measured(
        ws, record["ruleset_id"],
        join_keys={"jk_po": {"matched_pairs": 171, "left_unmatched": 11, "fan_out_p95": 1}},
        assertions={"as_total": {"evaluable_items": 168}},
    )
    assert measured["ruleset_hash"] == record["ruleset_hash"]
    assert measured["join_keys"][0]["measured"]["fan_out_p95"] == 1
    assert measured["assertions"][0]["measured"]["evaluable_items"] == 168


def test_measurement_survives_approval_and_stays_out_of_the_hash(ws):
    record = cycle_rulesets.save(ws, _payload())
    cycle_rulesets.set_measured(
        ws, record["ruleset_id"], join_keys={"jk_po": {"fan_out_p95": 400}}
    )
    approved = cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="auditor")
    assert approved["ruleset_hash"] == record["ruleset_hash"]
    assert approved["join_keys"][0]["measured"]["fan_out_p95"] == 400


# --------------------------------------------------------------- listing
def test_listing_and_index(ws):
    first = cycle_rulesets.save(ws, _payload())
    assert [item["ruleset_id"] for item in cycle_rulesets.list_rulesets(ws)] == [
        first["ruleset_id"]
    ]
    entries = cycle_rulesets.index(ws)["rulesets"]
    assert entries[0]["status"] == "proposed"


def test_missing_ruleset_reads_as_absent(ws):
    assert cycle_rulesets.load(ws, "lnk-nothing") is None
    with pytest.raises(RulesetError, match="not found"):
        cycle_rulesets.get(ws, "lnk-nothing")


def test_unsafe_ruleset_id_is_refused(ws):
    with pytest.raises(RulesetError, match="Invalid ruleset id"):
        cycle_rulesets.load(ws, "../escape")
