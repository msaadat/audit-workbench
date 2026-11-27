"""A row's cycle test writes its contract back onto the row that asked.

The matrix says a requirement needs several source records read together and
stops there. The cycle design proposes the rules linking the documents; when a
row's tests are written against the approved rules, the turn says which
assertions answer each of its requirements, and the commit writes them back as
``required_comparisons`` — exactly where they have always been, so everything
downstream keeps reading them there. Only the author moved, twice: from the
matrix to a ruleset proposal, and from there to the row's own test.
"""

from __future__ import annotations

import pytest

from app import cycle_linking, document_schemas, workspaces
from app.agent.executors.tests import _contract_row
from app.agent.workers import cycle_rules
from app.workspaces import WorkspaceError

_INVOICE_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "purchase_order_number", "role": "identifier",
     "value_type": "identifier", "cardinality": "one", "verbatim": True,
     "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]
_ORDER_FIELDS = [
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]
ROLES = {
    "invoice": {"name": "invoice", "document_type": "vendor_invoice"},
    "order": {"name": "order", "document_type": "purchase_order"},
}
TOTAL = {
    "id": "as_total",
    "label": "Totals agree",
    "left": {"role": "invoice", "field": "total_amount"},
    "right": {"role": "order", "field": "total_amount"},
    "requirement": "The amount billed must be the amount ordered.",
    "rationale": "The amount billed must be the amount ordered.",
}
ORDER_CITED = {
    "id": "as_order_cited",
    "label": "Order cited",
    "left": {"role": "invoice", "field": "purchase_order_number"},
    "right": None,
    "requirement": "The invoice names the order it bills against.",
    "rationale": "An invoice with no order cannot be matched to one.",
}


def _workspace(*attributes):
    workspace = workspaces.create_workspace("Cycle contract write-back")
    document_schemas.save_schema(workspace, "vendor_invoice", _INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", _ORDER_FIELDS)
    workspace = workspaces.load_workspace(workspace.id)
    workspace.add_rcm({
        "process": "Procure to pay",
        "risk": "An invoice is paid for more than was ordered.",
        "control": "Finance matches each invoice to its order before payment.",
        "control_attributes": list(attributes),
    })
    workspace = workspaces.load_workspace(workspace.id)
    return workspace, workspace.rcm[0]["id"]


def _attribute(**overrides):
    attribute = {
        "key": "three_way_match",
        "assertion": "Accuracy",
        "requirement": "The invoice agrees to the order it bills against.",
        "evidence_kind": "transaction_cycle",
    }
    attribute.update(overrides)
    return attribute


def _spec(key="three_way_match", assertions=(TOTAL,), gaps=()):
    """A cycle test as the generation gate normalizes it: its contract."""

    return {"contract": {key: {
        "comparisons": [cycle_rules.derived_comparison(item, ROLES) for item in assertions],
        "gaps": list(gaps),
    }}}


def _commit(workspace, rcm_id, specs, unsupported=()):
    report = _contract_row(workspace, rcm_id, list(specs), list(unsupported))
    return workspaces.load_workspace(workspace.id), report


def test_a_covered_requirement_gains_the_assertion_s_own_operands():
    """The comparison is the assertion, restated in the row's vocabulary.

    A rule names a *position* in the cycle; a row names the document type
    filling it. Translating between the two is the whole of the write-back.
    """

    workspace, rcm_id = _workspace(_attribute())
    workspace, report = _commit(workspace, rcm_id, [_spec()])

    (attribute,) = workspace.rcm[0]["control_attributes"]
    (comparison,) = attribute["required_comparisons"]
    assert comparison["key"] == "as_total"
    assert comparison["left"] == {"document_type": "vendor_invoice", "field": "total_amount"}
    assert comparison["right"] == {"document_type": "purchase_order", "field": "total_amount"}
    assert comparison["rationale"] == "The amount billed must be the amount ordered."
    assert report == {"partial": [], "downgraded": []}


def test_a_one_sided_assertion_writes_a_one_sided_comparison():
    workspace, rcm_id = _workspace(_attribute())
    workspace, _ = _commit(workspace, rcm_id, [_spec(assertions=(ORDER_CITED,))])

    (attribute,) = workspace.rcm[0]["control_attributes"]
    (comparison,) = attribute["required_comparisons"]
    assert comparison["right"] is None


def test_an_unsupported_requirement_is_downgraded_and_reported():
    """The approved rules' limit is not the requirement's limit.

    The attribute keeps its requirement and takes the strongest path still open
    to it. Leaving it classified ``transaction_cycle`` with nothing behind it
    would leave a row nothing will ever test and nothing will ever explain.
    """

    workspace, rcm_id = _workspace(_attribute())
    workspace, report = _commit(workspace, rcm_id, [], [{
        "requirement_ref": f"{rcm_id}:three_way_match",
        "reason": "No approved rule states a received quantity.",
    }])

    (attribute,) = workspace.rcm[0]["control_attributes"]
    # No table bears on this row, so the documents are what is left.
    assert attribute["evidence_kind"] == "document_content"
    assert "required_comparisons" not in attribute
    assert report["downgraded"] == [{
        "rcm_id": rcm_id,
        "control_attribute": "three_way_match",
        "reason": "No approved rule states a received quantity.",
    }]


def test_a_comparison_naming_a_field_no_schema_states_fails_the_commit():
    workspace, rcm_id = _workspace(_attribute())
    wrong = {**TOTAL, "right": {"role": "order", "field": "delivery_terms"}}

    with pytest.raises(WorkspaceError, match="does not state"):
        _commit(workspace, rcm_id, [_spec(assertions=(wrong,))])


def test_an_already_contracted_attribute_is_not_rewritten():
    """A contract already on the row was decided earlier, and rules may have
    been approved against it; overwriting would silently replace it."""

    workspace, rcm_id = _workspace(_attribute(required_comparisons=[{
        "key": "settled_total",
        "left": {"document_type": "vendor_invoice", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "rationale": "Decided earlier.",
    }]))
    workspace, _ = _commit(workspace, rcm_id, [_spec()])

    (attribute,) = workspace.rcm[0]["control_attributes"]
    assert [item["key"] for item in attribute["required_comparisons"]] == ["settled_total"]


def test_tests_answering_nothing_leave_the_row_alone():
    workspace, rcm_id = _workspace(_attribute())
    before = workspace.revision
    workspace, report = _commit(workspace, rcm_id, [{"steps": []}])

    (attribute,) = workspace.rcm[0]["control_attributes"]
    assert cycle_linking.uncontracted(attribute)
    assert workspace.revision == before
    assert report == {"partial": [], "downgraded": []}


def test_every_assertion_a_requirement_names_is_written_back():
    """Treasury's five-term confirmation match was once contracted as its rate
    alone; a requirement needing two comparisons is contracted with both."""

    workspace, rcm_id = _workspace(_attribute())
    workspace, _ = _commit(workspace, rcm_id, [_spec(assertions=(TOTAL, ORDER_CITED))])

    comparisons = cycle_linking.required_comparisons_for(
        workspace.rcm[0], [f"{rcm_id}:three_way_match"]
    )
    assert [item["key"] for item in comparisons] == ["as_total", "as_order_cited"]


def test_a_stated_gap_is_reported_beside_what_was_contracted():
    workspace, rcm_id = _workspace(_attribute())
    workspace, report = _commit(workspace, rcm_id, [_spec(gaps=[{
        "role": "order", "reason": "The order states no delivery terms to compare.",
    }])])

    (attribute,) = workspace.rcm[0]["control_attributes"]
    assert [item["key"] for item in attribute["required_comparisons"]] == ["as_total"]
    assert report["partial"] == [{
        "rcm_id": rcm_id,
        "control_attribute": "three_way_match",
        "role": "order",
        "reason": "The order states no delivery terms to compare.",
    }]


def test_the_written_back_contract_is_covered_by_the_rules_it_came_from():
    """The comparison *is* the approved assertion, restated, so a requirement
    a cycle test answered cannot be one the approved rules fail to cover."""

    workspace, rcm_id = _workspace(_attribute())
    workspace, _ = _commit(workspace, rcm_id, [_spec(assertions=(TOTAL, ORDER_CITED))])
    comparisons = cycle_linking.required_comparisons_for(
        workspace.rcm[0], [f"{rcm_id}:three_way_match"]
    )
    ruleset = {
        "roles": list(ROLES.values()),
        "assertions": [TOTAL, ORDER_CITED],
        "join_keys": [],
    }
    assert cycle_linking.uncovered_comparisons(ruleset, comparisons) == []


def test_an_uncontracted_attribute_without_approved_rules_says_so():
    """The silence between the matrix and the row's cycle test has to be said.

    ``tests.generate`` falls back to a document question in this state, which
    is right — but a run reporting the row as generated over it has reported
    success across a requirement nothing answers.
    """

    workspace, _rcm_id = _workspace(_attribute())

    (note,) = cycle_linking.unanswerable_cycle_requirements(workspace, workspace.rcm[0])

    assert "no approved cycle rules" in note
    assert "untested" in note
