"""The linkage proposal contract: what the model may propose, and what it may not."""

from __future__ import annotations

import json

import pytest

from app.agent.workers.model import WorkerResponseValidationError
from app.agent.workers.tests import (
    _linkage_response_schema,
    validate_linkage_proposal,
)

SCHEMAS = [
    {
        "document_type": "vendor_invoice",
        "fields": [
            {"name": "invoice_number", "role": "identifier", "value_type": "identifier"},
            {"name": "order_number", "role": "identifier", "value_type": "identifier"},
            {"name": "total_amount", "role": "attribute", "value_type": "number"},
            {"name": "approval", "role": "control", "value_type": "text"},
        ],
    },
    {
        "document_type": "purchase_order",
        "fields": [
            {"name": "order_number", "role": "identifier", "value_type": "identifier"},
            {"name": "total_amount", "role": "attribute", "value_type": "number"},
        ],
    },
]


class _Request:
    def __init__(self, **unit_input):
        self.unit_input = unit_input


def _request():
    return _Request(schemas=SCHEMAS)


def _proposal(**overrides) -> dict:
    proposal = {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "anchor": {"table": "register", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk_order", "match": "normalized_equal",
            "left": {"role": "invoice", "field": "order_number"},
            "right": {"role": "order", "field": "order_number"},
            "rationale": "An invoice cites the order it bills against.",
        }],
        "assertions": [{
            "id": "as_total", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "operator": "numeric_within", "tolerance": {"absolute": 1},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }
    proposal.update(overrides)
    return proposal


# ------------------------------------------------------------ response shape
def test_a_complete_proposal_parses():
    parsed = _linkage_response_schema(json.dumps(_proposal()))
    assert parsed["cycle_label"] == "Procure to pay"
    assert parsed["join_keys"][0]["match"] == "normalized_equal"
    assert parsed["assertions"][0]["operator"] == "numeric_within"


def test_a_cycle_that_tests_nothing_is_refused():
    """A cycle with no assertion links documents and concludes nothing."""

    with pytest.raises(WorkerResponseValidationError, match="not a cycle"):
        _linkage_response_schema(json.dumps(_proposal(assertions=[])))


def test_a_proposal_without_roles_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="at least one role"):
        _linkage_response_schema(json.dumps(_proposal(roles=[])))


def test_a_proposal_without_an_anchor_is_refused():
    payload = _proposal()
    del payload["anchor"]
    with pytest.raises(WorkerResponseValidationError, match="must name an anchor"):
        _linkage_response_schema(json.dumps(payload))


# -------------------------------------------------------- semantic contract
def test_joining_on_an_amount_is_refused_with_the_reason_stated():
    """The one mistake worth spending a repair turn on: it reads perfectly and
    fuses every transaction sharing that amount."""

    payload = _proposal()
    payload["join_keys"][0]["left"] = {"role": "invoice", "field": "total_amount"}
    payload["join_keys"][0]["right"] = {"role": "order", "field": "total_amount"}
    with pytest.raises(WorkerResponseValidationError, match="coincidence, not a link"):
        validate_linkage_proposal(payload, _request())


def test_joining_on_a_control_field_is_refused_too():
    payload = _proposal()
    payload["join_keys"][0]["left"] = {"role": "invoice", "field": "approval"}
    with pytest.raises(WorkerResponseValidationError, match="is a control field"):
        validate_linkage_proposal(payload, _request())


def test_a_field_the_type_does_not_carry_is_refused():
    """A rule naming an absent field reads as a passing test forever."""

    payload = _proposal()
    payload["assertions"][0]["left"] = {"role": "invoice", "field": "vat_amount"}
    with pytest.raises(WorkerResponseValidationError, match="does not carry"):
        validate_linkage_proposal(payload, _request())


def test_a_role_naming_an_uninduced_type_is_refused():
    payload = _proposal()
    payload["roles"][1]["document_type"] = "bank_statement"
    with pytest.raises(WorkerResponseValidationError, match="no schema for"):
        validate_linkage_proposal(payload, _request())


def test_an_unknown_role_is_refused():
    payload = _proposal()
    payload["assertions"][0]["left"] = {"role": "receipt", "field": "total_amount"}
    with pytest.raises(WorkerResponseValidationError, match="unknown role 'receipt'"):
        validate_linkage_proposal(payload, _request())


def test_the_anchor_is_checked_like_any_other_operand():
    payload = _proposal()
    payload["anchor"]["field"] = "not_a_field"
    with pytest.raises(WorkerResponseValidationError, match="The anchor names field"):
        validate_linkage_proposal(payload, _request())


def test_a_valid_proposal_passes_both_gates():
    parsed = _linkage_response_schema(json.dumps(_proposal()))
    assert validate_linkage_proposal(parsed, _request()) is parsed


def test_two_roles_may_share_a_document_type():
    """Roles are positions, not types — an original and a revised invoice."""

    payload = _proposal(
        roles=[
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "revised_invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": False},
        ],
        join_keys=[{
            "id": "jk_same", "match": "normalized_equal",
            "left": {"role": "invoice", "field": "order_number"},
            "right": {"role": "revised_invoice", "field": "order_number"},
            "rationale": "Both bill the same order.",
        }],
        assertions=[{
            "id": "as_total", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "revised_invoice", "field": "total_amount"},
            "operator": "numeric_within", "tolerance": {"absolute": 0},
            "rationale": "A revision must not change the amount billed.",
        }],
    )
    assert validate_linkage_proposal(payload, _request())


def test_a_present_assertion_needs_no_right_operand():
    payload = _proposal(assertions=[{
        "id": "as_approval", "label": "Approved",
        "left": {"role": "invoice", "field": "approval"}, "right": None,
        "operator": "present", "tolerance": None,
        "rationale": "Approval evidences the authorization control operated.",
    }])
    parsed = _linkage_response_schema(json.dumps(payload))
    assert parsed["assertions"][0]["right"] is None
    assert validate_linkage_proposal(parsed, _request())
