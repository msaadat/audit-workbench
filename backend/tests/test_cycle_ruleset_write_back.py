"""The cycle design writes its contract back onto the matrix rows that asked.

The matrix says a requirement needs several source records read together and
stops there; this stage, the one holding the induced schemas, decides which
fields must then agree. ``required_comparisons`` stays exactly where it has
always been and everything downstream keeps reading it there — only the author
moved.
"""

from __future__ import annotations

import pytest

from app import cycle_linking, cycle_rulesets, document_schemas, workspaces
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.tests import CycleRulesetExecutorTarget
from app.workspace_transactions import parent_hashes
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


def _proposal(coverage, assertions=None):
    return {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "anchor": {"table": "expense_register", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk_po",
            "left": {"role": "invoice", "field": "purchase_order_number"},
            "right": {"role": "order", "field": "order_number"},
            "match": "normalized_equal",
            "rationale": "An invoice cites the order it bills against.",
        }],
        "assertions": assertions if assertions is not None else [{
            "id": "as_total",
            "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "requirement": "The amount billed must be the amount ordered.",
            "rationale": "The amount billed must be the amount ordered.",
        }],
        "coverage": coverage,
    }


def _request(workspace, rcm_id, proposal):
    return ExecutorRequest(
        executor_id="tests.cycle_ruleset",
        capability_id="tests.cycle_ruleset_proposed",
        unit_id="cycle_ruleset:proposal",
        proposal=proposal,
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [f"rcm:{rcm_id}"]),
        activity={"artifact_refs": [f"rcm:{rcm_id}"]},
    )


def test_a_covered_requirement_gains_the_assertion_s_own_operands():
    """The comparison is the assertion, restated in the row's vocabulary.

    A rule names a *position* in the cycle; a row names the document type
    filling it. Translating between the two is the whole of the write-back, and
    it is local code because nothing about it is a judgment.
    """

    workspace, rcm_id = _workspace(_attribute())
    target = CycleRulesetExecutorTarget(workspace, "run-covered")

    EXECUTORS.execute(
        _request(workspace, rcm_id, _proposal([{
            "rcm_id": rcm_id,
            "control_attribute": "three_way_match",
            "assertion_id": "as_total",
        }])),
        target,
    )

    (row,) = target.workspace.rcm
    (attribute,) = row["control_attributes"]
    (comparison,) = attribute["required_comparisons"]
    assert comparison["key"] == "as_total"
    assert comparison["left"] == {
        "document_type": "vendor_invoice", "field": "total_amount"
    }
    assert comparison["right"] == {
        "document_type": "purchase_order", "field": "total_amount"
    }
    assert comparison["rationale"] == "The amount billed must be the amount ordered."
    # Downstream reads the field it has always read.
    assert cycle_linking.schema_backed(attribute)
    assert not cycle_linking.uncontracted(attribute)


def test_a_one_sided_assertion_writes_a_one_sided_comparison():
    """A requirement that a field be *stated* names one operand, not two."""

    workspace, rcm_id = _workspace(_attribute(
        key="approval_present",
        assertion="Authorization",
        requirement="The invoice carries an approval.",
    ))
    target = CycleRulesetExecutorTarget(workspace, "run-one-sided")

    EXECUTORS.execute(
        _request(workspace, rcm_id, _proposal(
            [{
                "rcm_id": rcm_id,
                "control_attribute": "approval_present",
                "assertion_id": "as_number",
            }],
            assertions=[{
                "id": "as_number",
                "label": "Invoice number stated",
                "left": {"role": "invoice", "field": "invoice_number"},
                "right": None,
                "requirement": "Every invoice states its own number.",
                "rationale": "An unnumbered invoice cannot be traced.",
            }],
        )),
        target,
    )

    (attribute,) = target.workspace.rcm[0]["control_attributes"]
    (comparison,) = attribute["required_comparisons"]
    assert comparison["right"] is None


def test_an_unsupported_requirement_is_downgraded_and_reported():
    """The schemas' limit is not the requirement's limit.

    The attribute keeps its requirement and takes the strongest path still open
    to it. Leaving it classified ``transaction_cycle`` with nothing behind it
    would leave a row nothing will ever test and nothing will ever explain.
    """

    workspace, rcm_id = _workspace(_attribute())
    target = CycleRulesetExecutorTarget(workspace, "run-unsupported")

    receipt = EXECUTORS.execute(
        _request(workspace, rcm_id, _proposal([{
            "rcm_id": rcm_id,
            "control_attribute": "three_way_match",
            "unsupported": True,
            "reason": "No induced type states a received quantity.",
        }])),
        target,
    )

    (attribute,) = target.workspace.rcm[0]["control_attributes"]
    # No table bears on this row, so the documents are what is left.
    assert attribute["evidence_kind"] == "document_content"
    assert "required_comparisons" not in attribute
    assert attribute["requirement"] == (
        "The invoice agrees to the order it bills against."
    )
    (reported,) = receipt.output["downgraded"]
    assert dict(reported) == {
        "rcm_id": rcm_id,
        "control_attribute": "three_way_match",
        "reason": "No induced type states a received quantity.",
    }


def test_a_comparison_naming_a_field_no_schema_states_fails_the_commit():
    """Exact, against this engagement's own schemas, at the turn that holds it.

    ``update_rcm`` checks shape alone — it has no workspace to check against —
    so without this the row would persist a requirement pointing at nothing and
    surface three stages on as a cycle test that cannot be generated.
    """

    workspace, rcm_id = _workspace(_attribute())
    target = CycleRulesetExecutorTarget(workspace, "run-bad-field")
    proposal = _proposal(
        [{
            "rcm_id": rcm_id,
            "control_attribute": "three_way_match",
            "assertion_id": "as_total",
        }],
        assertions=[{
            "id": "as_total",
            "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "requirement": "The amount billed must be the amount ordered.",
            "rationale": "The amount billed must be the amount ordered.",
        }],
    )
    # A role whose document type does not carry the field the rule reads.
    proposal["roles"][1]["document_type"] = "vendor_invoice"
    proposal["assertions"][0]["right"]["field"] = "order_number"

    with pytest.raises(WorkspaceError, match="does not state"):
        EXECUTORS.execute(_request(workspace, rcm_id, proposal), target)


def test_an_already_contracted_attribute_is_not_rewritten():
    """A contract already on the row was answered by an earlier proposal.

    Only uncontracted attributes reach this worker as requirements, so a
    coverage entry naming one that is already contracted is stale rather than
    authoritative — and overwriting would silently replace a comparison an
    auditor may already have approved rules against.
    """

    workspace, rcm_id = _workspace(_attribute(required_comparisons=[{
        "key": "settled_total",
        "left": {"document_type": "vendor_invoice", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "rationale": "Decided by an earlier proposal.",
    }]))
    target = CycleRulesetExecutorTarget(workspace, "run-already")

    EXECUTORS.execute(
        _request(workspace, rcm_id, _proposal([{
            "rcm_id": rcm_id,
            "control_attribute": "three_way_match",
            "assertion_id": "as_total",
        }])),
        target,
    )

    (attribute,) = target.workspace.rcm[0]["control_attributes"]
    assert [item["key"] for item in attribute["required_comparisons"]] == [
        "settled_total"
    ]


def test_a_proposal_answering_nothing_leaves_every_row_alone():
    """Rules may be proposed before a matrix asks anything of them."""

    workspace, rcm_id = _workspace(_attribute())
    target = CycleRulesetExecutorTarget(workspace, "run-nothing")

    receipt = EXECUTORS.execute(
        _request(workspace, rcm_id, _proposal([])), target
    )

    (attribute,) = target.workspace.rcm[0]["control_attributes"]
    assert cycle_linking.uncontracted(attribute)
    assert not receipt.output["downgraded"]
    assert cycle_rulesets.list_rulesets(target.workspace)


def test_the_written_back_contract_is_covered_by_the_rules_it_came_from():
    """The step-1 invariant, stated as the property that makes it hold.

    By the time ``tests.specified`` runs, the rows carry comparisons again and
    a cycle test builds from them exactly as before. And a gap between the
    matrix and the rules is now impossible rather than merely repaired: the
    comparison *is* the assertion, restated, so a requirement the design
    answered cannot be one the approved rules fail to cover.
    """

    workspace, rcm_id = _workspace(_attribute())
    target = CycleRulesetExecutorTarget(workspace, "run-chain")

    receipt = EXECUTORS.execute(
        _request(workspace, rcm_id, _proposal([{
            "rcm_id": rcm_id,
            "control_attribute": "three_way_match",
            "assertion_id": "as_total",
        }])),
        target,
    )
    cycle_rulesets.approve(
        target.workspace,
        receipt.output["ruleset_id"],
        approved_by="auditor@example.com",
    )
    workspace = workspaces.load_workspace(target.workspace.id)
    (row,) = workspace.rcm

    # What ``tests.generate`` reads to build a cycle test.
    (comparison,) = cycle_linking.required_comparisons_for(
        row, [f"{rcm_id}:three_way_match"]
    )
    assert comparison["key"] == "as_total"
    # And nothing is left untested: no degradation note survives the round trip.
    assert cycle_linking.unanswerable_cycle_requirements(workspace, row) == []


def test_an_uncontracted_attribute_is_reported_as_untested_until_it_is_designed():
    """The silence between the matrix and the cycle design has to be said.

    ``tests.generate`` falls back to a document question in this state, which
    is right — but a run reporting the row as generated over it has reported
    success across a requirement nothing answers.
    """

    workspace, _rcm_id = _workspace(_attribute())

    (note,) = cycle_linking.unanswerable_cycle_requirements(
        workspace, workspace.rcm[0]
    )

    assert "no cycle design has run for it" in note
    assert "untested" in note
