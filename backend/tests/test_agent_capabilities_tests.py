"""Readiness gates owned by the tests capability group."""

from __future__ import annotations

import pytest

from app import (
    cycle_measurement,
    cycle_rulesets,
    document_schemas,
    documents,
    workspaces,
)
from app import data_tests
from app.agent.capabilities import tests as test_capabilities
from app.agent.capabilities import _shared


def _workspace_with_a_voucher():
    workspace = workspaces.create_workspace("Unvouched cycle")
    documents.add_document(
        workspace,
        "goods-receipt.txt",
        b"Goods received and inspected.",
        category="evidence",
    )
    return workspaces.load_workspace(workspace.id)


def _extracted(monkeypatch, *document_types):
    monkeypatch.setattr(
        cycle_measurement,
        "structured_records",
        lambda _workspace: [
            {
                "document_type": value,
                "document_id": "d1",
                "record_index": 0,
                "record": {},
            }
            for value in document_types
        ],
    )


def test_records_extracted_and_never_vouched_are_reported(monkeypatch):
    """The bypass is silent by construction, so something has to say it.

    Cycle vouching is reachable only through a ``transaction_cycle`` control
    attribute. When the matrix classifies every attribute some other way the
    linker and the evaluator are never called at all, and the run reports
    success.
    """
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch, "goods_receipt", "vendor_invoice")

    assert test_capabilities._unvouched_types(workspace) == [
        "goods_receipt",
        "vendor_invoice",
    ]


def test_a_workspace_whose_documents_yield_no_records_is_not_a_bypass(monkeypatch):
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch)

    assert test_capabilities._unvouched_types(workspace) == []


def test_a_workspace_with_no_documents_has_nothing_to_vouch(monkeypatch):
    workspace = workspaces.create_workspace("No documents")
    _extracted(monkeypatch, "vendor_invoice")

    assert test_capabilities._unvouched_types(workspace) == []


def test_readiness_names_the_types_left_unvouched(monkeypatch):
    workspace = _workspace_with_a_voucher()
    _extracted(monkeypatch, "payment_voucher")
    monkeypatch.setattr(
        test_capabilities,
        "_rows",
        lambda *_args, **_kwargs: [{"id": "RCM-1"}],
    )
    monkeypatch.setattr(
        test_capabilities,
        "_scoped_manifest",
        lambda *_args, **_kwargs: [
            {"rcm_id": "RCM-1", "executable": True, "status": "completed"}
        ],
    )

    readiness = test_capabilities._specified_ready(workspace, {})

    assert readiness.state == "review_required"
    (reason,) = readiness.reasons
    assert "payment_voucher" in reason
    assert "transaction_cycle" in reason


# ------------------------------------------------------- why nothing vouched
# The gap is detected from the records; the *cause* has to be asked separately,
# because an explanation nobody checked sends the reader to the wrong repair.
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


def _cycle_matrix(workspace):
    """A workspace whose matrix asks for transaction-cycle evidence."""

    document_schemas.save_schema(workspace, "vendor_invoice", _INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", _ORDER_FIELDS)
    workspace.add_rcm({
        "process": "Procure to pay",
        "risk": "An invoice is paid for more than was ordered.",
        "control_attributes": [{
            "key": "three_way_match",
            "assertion": "Accuracy",
            "requirement": "The invoice agrees to the order.",
            "evidence_kind": "transaction_cycle",
            "required_comparisons": [{
                "key": "total",
                "left": {"document_type": "vendor_invoice", "field": "total_amount"},
                "right": {"document_type": "purchase_order", "field": "total_amount"},
                "rationale": "The amount billed must be the amount ordered.",
            }],
        }],
    })
    return workspaces.load_workspace(workspace.id)


def _ruleset_payload():
    return {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one"},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one"},
        ],
        "anchor": {"table": "expense_register", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk_po",
            "left": {"role": "invoice", "field": "purchase_order_number"},
            "right": {"role": "order", "field": "order_number"},
            "match": "normalized_equal",
            "rationale": "An invoice cites its order.",
        }],
        "assertions": [{
            "id": "as_total",
            "label": "Totals agree",
            "requirement": "The amount billed must be the amount ordered.",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
        }],
    }


def _uncontracted_matrix(workspace):
    """A matrix as it now commits: the strategy, and no contract."""

    document_schemas.save_schema(workspace, "vendor_invoice", _INVOICE_FIELDS)
    document_schemas.save_schema(workspace, "purchase_order", _ORDER_FIELDS)
    workspace.add_rcm({
        "process": "Procure to pay",
        "risk": "An invoice is paid for more than was ordered.",
        "control_attributes": [{
            "key": "three_way_match",
            "assertion": "Accuracy",
            "requirement": "The invoice agrees to the order.",
            "evidence_kind": "transaction_cycle",
        }],
    })
    return workspaces.load_workspace(workspace.id)


def test_the_stage_is_keyed_on_the_strategy_not_on_the_contract():
    """What this stage exists to write is the very thing it used to require.

    Keyed on ``schema_backed``, an uncontracted matrix reported the stage
    satisfied and proposed for nobody — which is every matrix, now that the
    contract is authored here.
    """

    workspace = _uncontracted_matrix(_workspace_with_a_voucher())

    (attribute,) = test_capabilities._cycle_attributes(workspace)
    assert attribute["key"] == "three_way_match"

    readiness = test_capabilities._ruleset_ready(workspace, {})
    assert readiness.state == "missing"

    (unit,) = test_capabilities._ruleset_units(workspace, {})
    assert unit.kind == "cycle_ruleset_proposal"
    # The guarded parents are the rows the comparisons will land on.
    (row,) = workspace.rcm
    assert unit.parent_refs == (f"rcm:{row['id']}",)


def test_a_committed_proposal_does_not_re_expand_after_the_rows_change():
    """The write-back rewrites the rows this unit is guarded on.

    ``invalidate_on=("rcm",)`` therefore fires on the stage's own commit, and
    without the existing-ruleset check that would be a loop: propose, write
    back, invalidate, propose again.
    """

    workspace = _uncontracted_matrix(_workspace_with_a_voucher())
    assert test_capabilities._ruleset_units(workspace, {})

    cycle_rulesets.save(workspace, _ruleset_payload())
    workspace = workspaces.load_workspace(workspace.id)

    assert test_capabilities._ruleset_units(workspace, {}) == []
    assert test_capabilities._ruleset_ready(workspace, {}).state == "satisfied"


def test_cause_is_the_matrix_when_no_attribute_asks_for_a_cycle():
    workspace = _workspace_with_a_voucher()

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "no_cycle_attribute"
    assert "transaction_cycle" in sentence
    assert ruleset_id == ""


def test_cause_is_the_missing_proposal_when_the_matrix_asks():
    workspace = _cycle_matrix(_workspace_with_a_voucher())

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "no_ruleset"
    assert "has been proposed" in sentence
    assert ruleset_id == ""


def test_cause_is_the_pending_approval_and_it_names_the_ruleset():
    """The case that reads like success from every other angle.

    An agent proposed the rules, every stage reported completion, and the rules
    sit unapplied because approving them is not an agent's to do. Reported as
    the matrix's fault, the reader goes and audits a matrix that is correct.
    """

    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "ruleset_unapproved"
    assert "proposed and unapproved" in sentence
    assert "approve" in sentence
    assert ruleset_id == record["ruleset_id"]


def test_cause_is_the_missing_test_once_a_ruleset_is_approved():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    cycle_rulesets.approve(
        workspace, record["ruleset_id"], approved_by="auditor@example.com"
    )

    cause, _sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "no_cycle_test"
    assert ruleset_id == record["ruleset_id"]


def test_cause_is_stated_when_every_proposal_was_rejected():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    cycle_rulesets.reject(workspace, record["ruleset_id"])

    cause, sentence, ruleset_id = test_capabilities._unvouched_cause(workspace)

    assert cause == "ruleset_rejected"
    assert "rejected" in sentence
    assert ruleset_id == ""


def test_readiness_sends_the_reader_to_the_unapproved_ruleset(monkeypatch):
    """Regression: the reason used to blame the matrix unconditionally.

    A treasury engagement whose matrix declared four ``transaction_cycle``
    attributes was told the matrix declared none, so the one thing outstanding
    — an auditor's signature on rules the agent had already written — was the
    one thing the message did not mention.
    """

    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    _extracted(monkeypatch, "vendor_invoice")
    monkeypatch.setattr(
        test_capabilities, "_rows", lambda *_a, **_k: [{"id": "RCM-1"}]
    )
    monkeypatch.setattr(
        test_capabilities,
        "_scoped_manifest",
        lambda *_a, **_k: [
            {"rcm_id": "RCM-1", "executable": True, "status": "completed"}
        ],
    )

    readiness = test_capabilities._specified_ready(workspace, {})

    assert readiness.state == "review_required"
    (reason,) = readiness.reasons
    assert "vendor_invoice" in reason
    assert "proposed and unapproved" in reason
    assert "the matrix classified no control attribute" not in reason
    assert readiness.details["unvouched_cause"] == "ruleset_unapproved"
    assert readiness.details["ruleset_id"] == record["ruleset_id"]


# ------------------------------------------------------- auto-mode approval
# Selecting ``mode: auto`` delegates the run's approvals, and that now includes
# the cycle rules the same run wrote. Permission mode keeps the human gate.
def _approval_capability():
    return test_capabilities._cycle_ruleset_approved()


_AUTO = {"permission_mode": False}
_PERMISSION = {"permission_mode": True}


def test_nothing_to_approve_where_the_matrix_asks_for_no_cycle():
    """An engagement without a cycle must not wait on an approval either."""

    workspace = _workspace_with_a_voucher()
    capability = _approval_capability()

    for scope in (_AUTO, _PERMISSION):
        assert capability.readiness(workspace, scope).state == "satisfied"
        assert capability.expand_units(workspace, scope) == []


def test_permission_mode_reports_the_gate_and_expands_no_unit():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    cycle_rulesets.save(workspace, _ruleset_payload())
    capability = _approval_capability()

    readiness = capability.readiness(workspace, _PERMISSION)

    assert readiness.state == "review_required"
    assert "auditor" in readiness.reasons[0]
    assert capability.expand_units(workspace, _PERMISSION) == []


def test_a_readiness_read_outside_a_run_defaults_to_the_human_gate():
    """The status endpoint has no run and therefore no delegation in force.

    ``permission_mode`` absent must not read as auto: the standing state of a
    workspace holding an unapproved proposal is "waiting for an auditor", which
    is what a status screen should show.
    """

    workspace = _cycle_matrix(_workspace_with_a_voucher())
    cycle_rulesets.save(workspace, _ruleset_payload())
    capability = _approval_capability()

    assert capability.readiness(workspace, {}).state == "review_required"
    assert capability.expand_units(workspace, {}) == []


def test_auto_mode_expands_one_unit_naming_the_proposal():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    capability = _approval_capability()

    readiness = capability.readiness(workspace, _AUTO)
    (unit,) = capability.expand_units(workspace, _AUTO)

    assert readiness.state == "missing"
    assert unit.kind == "cycle_ruleset_approval"
    assert unit.parent_refs == (f"cycle_ruleset:{record['ruleset_id']}",)
    assert unit.input_payload["ruleset_id"] == record["ruleset_id"]
    assert unit.input_payload["ruleset_hash"] == record["ruleset_hash"]


def test_editing_the_rules_makes_it_a_different_approval_unit():
    """Approving is bound to the rules that were read, not to the id.

    A proposal re-expanded against moved schemas is a different question; an
    approval unit that reused its identity would let a run approve rules it
    never saw.
    """

    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    (before,) = _approval_capability().expand_units(workspace, _AUTO)

    edited = _ruleset_payload()
    edited["assertions"][0]["requirement"] = "The billed amount must be ordered."
    cycle_rulesets.save(workspace, edited, ruleset_id=record["ruleset_id"])
    (after,) = _approval_capability().expand_units(workspace, _AUTO)

    assert before.id == after.id
    assert before.input_sha1 != after.input_sha1


def test_an_approved_ruleset_settles_the_capability_in_both_modes():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())
    cycle_rulesets.approve(
        workspace, record["ruleset_id"], approved_by="auditor@example.com"
    )
    capability = _approval_capability()

    for scope in (_AUTO, _PERMISSION):
        assert capability.readiness(workspace, scope).state == "satisfied"
        assert capability.expand_units(workspace, scope) == []


def test_an_auto_approval_is_recorded_as_the_agent_not_a_person():
    """The delegation is legible in the file, not inferred from a name."""

    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())

    approved = cycle_rulesets.approve(
        workspace,
        record["ruleset_id"],
        approved_by=test_capabilities.AGENT_APPROVER,
        approved_by_kind="agent",
    )

    assert approved["status"] == "approved"
    assert approved["approved_by"] == test_capabilities.AGENT_APPROVER
    assert cycle_rulesets.approver_kind(approved) == "agent"


def test_an_auditor_approval_stays_an_auditor_approval():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())

    approved = cycle_rulesets.approve(
        workspace, record["ruleset_id"], approved_by="auditor@example.com"
    )

    assert cycle_rulesets.approver_kind(approved) == "auditor"


def test_a_ruleset_approved_before_the_distinction_existed_reads_as_auditor():
    """Every one of those went through the review screen, so this is a fact."""

    assert cycle_rulesets.approver_kind({"approved_by": "auditor@example.com"}) == "auditor"
    assert cycle_rulesets.approver_kind({"approved_by_kind": "nonsense"}) == "auditor"


def test_an_unknown_approver_kind_is_refused():
    workspace = _cycle_matrix(_workspace_with_a_voucher())
    record = cycle_rulesets.save(workspace, _ruleset_payload())

    with pytest.raises(cycle_rulesets.RulesetError, match="approved_by_kind"):
        cycle_rulesets.approve(
            workspace,
            record["ruleset_id"],
            approved_by="someone",
            approved_by_kind="robot",
        )
    assert cycle_rulesets.get(workspace, record["ruleset_id"])["status"] == "proposed"


# --------------------------------------------------------------------------- #
# Addressing what lives below the RCM row (step 2)
# --------------------------------------------------------------------------- #
def _row_with_two_tests():
    """A settled row: two executable Data Tests, nothing left to generate."""
    workspace = workspaces.create_workspace("Below the row")
    workspace.add_table("transactions.csv", b"invoice,amount\n1001,100\n1002,50\n")
    row = workspace.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate payments",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    made = [
        data_tests.create(
            workspace,
            {
                "title": title,
                "objective": f"{title}.",
                "criteria": "No duplicate is paid.",
                "steps": [{"label": title, "instruction": title}],
                "rcm_id": row["id"],
                "engine": "polars",
                "spec": {
                    "schema_version": 2,
                    "steps": [
                        {
                            "label": title,
                            "instruction": title,
                            "table_refs": ["transactions"],
                            "code": "result = transactions.filter(pl.col('amount') > 50)",
                        }
                    ],
                },
            },
        )
        for title in ("First screening", "Second screening")
    ]
    return workspaces.load_workspace(workspace.id), row, made


def test_a_scope_naming_a_test_selects_the_row_that_test_sits_on():
    workspace, row, tests = _row_with_two_tests()

    scope = _shared.target_scope(
        workspace, {"target_refs": [f"datatest:{tests[0]['id']}"]}
    )

    assert scope.test_ids == (tests[0]["id"],)
    assert scope.rcm_ids == (row["id"],)
    assert scope.explicit is True
    # Every caller that only understands rows keeps working against it.
    assert _shared.target_rcm_ids(
        workspace, {"target_refs": [f"datatest:{tests[0]['id']}"]}
    ) == [row["id"]]


def test_an_unscoped_request_still_selects_every_row():
    workspace, row, _tests = _row_with_two_tests()

    scope = _shared.target_scope(workspace, {})

    assert scope == _shared.TargetScope()
    assert _shared.target_rcm_ids(workspace, {}) == [row["id"]]
    assert _shared.target_rcm_ids(
        workspace, {"target_refs": ["workspace:current"]}
    ) == [row["id"]]


def test_a_scope_naming_a_test_that_no_longer_exists_selects_nothing():
    """A stale button narrows to nothing rather than widening to everything."""
    workspace, _row, _tests = _row_with_two_tests()

    assert _shared.target_rcm_ids(
        workspace, {"target_refs": ["datatest:DAT-GONE"]}
    ) == []


def test_a_settled_row_expands_nothing_until_a_test_is_named():
    workspace, row, tests = _row_with_two_tests()
    capability = test_capabilities.capabilities()
    specified = next(item for item in capability if item.id == "tests.specified")

    assert specified.expand_units(workspace, {}) == []

    units = specified.expand_units(
        workspace, {"target_refs": [f"datatest:{tests[0]['id']}"]}
    )

    # One unit, on the owning row — not one per test and not one per row.
    assert [unit.parent_refs for unit in units] == [(f"rcm:{row['id']}",)]


def test_naming_a_test_is_the_force_so_it_need_not_be_asked_for_twice():
    workspace, _row, tests = _row_with_two_tests()
    specified = next(
        item for item in test_capabilities.capabilities()
        if item.id == "tests.specified"
    )

    named = specified.expand_units(
        workspace, {"target_refs": [f"datatest:{tests[0]['id']}"]}
    )

    assert len(named) == 1


def test_a_redraft_unit_is_a_different_unit_from_a_whole_row_regeneration():
    """Otherwise a proposal that rewrote the whole row could be reused as one
    that rewrites a single test of it, and the siblings would vanish."""
    workspace, _row, tests = _row_with_two_tests()
    specified = next(
        item for item in test_capabilities.capabilities()
        if item.id == "tests.specified"
    )

    whole_row = specified.expand_units(workspace, {"generation_mode": "force"})
    one_test = specified.expand_units(
        workspace, {"target_refs": [f"datatest:{tests[0]['id']}"]}
    )

    assert whole_row[0].id == one_test[0].id, "the same row, so the same unit id"
    assert whole_row[0].input_sha1 != one_test[0].input_sha1

    other_test = specified.expand_units(
        workspace, {"target_refs": [f"datatest:{tests[1]['id']}"]}
    )
    assert one_test[0].input_sha1 != other_test[0].input_sha1
