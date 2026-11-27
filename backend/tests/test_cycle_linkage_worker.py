"""The linkage proposal contract: what the model may propose, and what it may not."""

from __future__ import annotations

import json

import pytest

from app import cycle_rulesets
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


class _ContextItem:
    def __init__(self, source_id, content):
        self.source_id = source_id
        self.content = content


class _Context:
    def __init__(self, items):
        self.items = list(items)


class _Request:
    """The schemas reach this worker as declared context, not as unit input.

    They are a supplied source like any other, so the manifest records what the
    proposal was actually shown; the unit input carries their hashes instead,
    which is what re-expands the unit when one is re-derived.
    """

    def __init__(self, *, schemas=(), requirements=None, **unit_input):
        self.unit_input = unit_input
        # One atomic item carrying every type, the shape the adapter supplies:
        # a cycle describes how the whole set relates, so a vocabulary admitted
        # piecemeal would yield a proposal missing a role and not say which.
        items = []
        if schemas:
            items.append(_ContextItem("cycle_schemas", {"schemas": list(schemas)}))
        if requirements is not None:
            items.append(
                _ContextItem(
                    "cycle_requirements", {"cycle_requirements": list(requirements)}
                )
            )
        self.context = _Context(items)


def _request(requirements=None):
    return _Request(schemas=SCHEMAS, requirements=requirements)


def _requirement(**overrides) -> dict:
    """One thing the matrix has decided this cycle must demonstrate.

    In words. The matrix names no fields — it has read none of these documents,
    and this worker is the one turn that has their schemas in front of it.
    """
    item = {
        "rcm_id": "RCM-1",
        "control_attribute": "invoice_match",
        "assertion": "Accuracy",
        "requirement": "The invoice agrees to the order it bills against.",
        "control": "Invoices are matched to their orders before payment.",
        "risk": "Invoices are paid for amounts never ordered.",
        "process": "Procure to pay",
    }
    item.update(overrides)
    return item


def _covered(**overrides) -> dict:
    entry = {
        "rcm_id": "RCM-1",
        "control_attribute": "invoice_match",
        "reads": ["invoice", "order"],
        "assertion_ids": ["as_total"],
    }
    entry.update(overrides)
    return entry


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
            "id": "as_total", "requirement": "The records must agree.", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
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
    assert parsed["assertions"][0]["requirement"] == "The records must agree."
    # Absent where nothing was asked, which is the shape a proposal written from
    # the vocabulary alone has always had.
    assert parsed["coverage"] == []


def test_coverage_parses_into_both_of_its_two_shapes():
    parsed = _linkage_response_schema(
        json.dumps(
            _proposal(
                coverage=[
                    _covered(),
                    {
                        "rcm_id": "RCM-2",
                        "control_attribute": "receipt_match",
                        "unsupported": True,
                        "reason": "No induced type states a delivery quantity.",
                    },
                ]
            )
        )
    )

    answered, unsupported = parsed["coverage"]
    assert answered["assertion_ids"] == ["as_total"]
    assert answered["reads"] == ["invoice", "order"]
    assert "assertion_ids" not in unsupported
    assert unsupported["reason"] == "No induced type states a delivery quantity."


def test_a_cycle_that_tests_nothing_is_refused():
    """A cycle with no assertion links documents and concludes nothing.

    Refused after the field-pair floor rather than at parse time: a response
    that writes no rule of its own and keeps the supplied pairs is complete.
    """

    parsed = _linkage_response_schema(json.dumps(_proposal(assertions=[])))
    with pytest.raises(WorkerResponseValidationError, match="not a cycle"):
        validate_linkage_proposal(parsed, _request())


def test_a_proposal_without_roles_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="at least one role"):
        _linkage_response_schema(json.dumps(_proposal(roles=[])))


def test_a_proposal_without_an_anchor_is_refused():
    payload = _proposal()
    del payload["anchor"]
    with pytest.raises(WorkerResponseValidationError, match="must name an anchor"):
        _linkage_response_schema(json.dumps(payload))


# -------------------------------------------------------- semantic contract
@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda p: p["join_keys"][0].update(
                left={"role": "invoice", "field": "total_amount"},
                right={"role": "order", "field": "total_amount"},
            ),
            "coincidence, not a link",
        ),
        (
            lambda p: p["join_keys"][0].update(left={"role": "invoice", "field": "approval"}),
            "is a control field",
        ),
        (
            lambda p: p["assertions"][0].update(left={"role": "invoice", "field": "vat_amount"}),
            "does not carry",
        ),
        (
            lambda p: p["roles"][1].update(document_type="bank_statement"),
            "no schema for",
        ),
        (
            lambda p: p["assertions"][0].update(left={"role": "receipt", "field": "total_amount"}),
            "unknown role 'receipt'",
        ),
        (
            lambda p: p["anchor"].update(field="not_a_field"),
            "The anchor names field",
        ),
    ],
    ids=[
        "join-on-amount",
        "join-on-control-field",
        "missing-field",
        "uninduced-type",
        "unknown-role",
        "missing-anchor-field",
    ],
)
def test_invalid_linkage_proposals_are_refused(mutate, expected_error):
    payload = _proposal()
    mutate(payload)
    with pytest.raises(WorkerResponseValidationError, match=expected_error):
        validate_linkage_proposal(payload, _request())


def test_a_valid_proposal_passes_both_gates():
    parsed = _linkage_response_schema(json.dumps(_proposal()))
    # Equal rather than the same object: the validator returns the proposal
    # with the field-pair floor applied, which without a cycle adds nothing.
    validated = validate_linkage_proposal(parsed, _request())
    assert validated["assertions"] == parsed["assertions"]
    assert validated["declined_pairs"] == []


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
            "id": "as_total", "requirement": "The records must agree.", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "revised_invoice", "field": "total_amount"},
            "rationale": "A revision must not change the amount billed.",
        }],
    )
    assert validate_linkage_proposal(payload, _request())


def test_a_present_assertion_needs_no_right_operand():
    payload = _proposal(assertions=[{
        "id": "as_approval", "requirement": "The records must agree.", "label": "Approved",
        "left": {"role": "invoice", "field": "approval"}, "right": None,
        "tolerance": None,
        "rationale": "Approval evidences the authorization control operated.",
    }])
    parsed = _linkage_response_schema(json.dumps(payload))
    assert parsed["assertions"][0]["right"] is None
    assert validate_linkage_proposal(parsed, _request())


# ------------------------------------------------- the capability wiring (P6)
# The worker existed, was registered, was tested, and had no caller anywhere in
# the application: `POST /cycle-rulesets` stored whatever payload it was handed
# and nothing ever generated one. These cover the wiring rather than the
# proposal, because the wiring is what was missing.

def _cycle_workspace():
    """An engagement whose matrix asks for linked evidence it has fields for."""

    from app import document_schemas, workspaces

    ws = workspaces.create_workspace("Cycle ruleset wiring")
    document_schemas.save_schema(ws, "vendor_invoice", [
        {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
        {"name": "total_amount", "role": "attribute", "value_type": "number",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ])
    document_schemas.save_schema(ws, "purchase_order", [
        {"name": "order_number", "role": "identifier", "value_type": "identifier",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
        {"name": "total_amount", "role": "attribute", "value_type": "number",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ])
    return workspaces.load_workspace(ws.id)


def _cycle_row():
    return {
        "id": "RCM-001",
        "process": "Invoice processing",
        "risk": "An invoice is paid for more than was ordered.",
        "control": "Finance matches the invoice to the order.",
        "control_attributes": [
            {
                "key": "amount_agrees",
                "assertion": "Valuation",
                "requirement": "The invoice total must agree to the order total.",
                "evidence_kind": "transaction_cycle",
                "required_comparisons": [
                    {
                        "key": "totals",
                        "left": {"document_type": "vendor_invoice",
                                 "field": "total_amount"},
                        "right": {"document_type": "purchase_order",
                                  "field": "total_amount"},
                        "rationale": "The amount billed must be the amount ordered.",
                    }
                ],
            }
        ],
    }


def _approve_ruleset(ws):
    """Propose and approve the cycle rules this workspace's fields support."""

    from app import cycle_rulesets

    record = cycle_rulesets.save(ws, {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "anchor": {"table": "invoice_data", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk", "left": {"role": "invoice", "field": "invoice_number"},
            "right": {"role": "order", "field": "order_number"},
            "match": "normalized_equal", "rationale": "An invoice cites its order.",
        }],
        "assertions": [{
            "id": "as", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }, proposed_by="agent")
    return cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="auditor")


def test_a_matrix_asking_for_linked_evidence_expands_a_proposal_unit():
    from app.agent.capabilities import tests as test_capabilities

    ws = _cycle_workspace()
    ws.rcm = [_cycle_row()]
    capability = next(
        item for item in test_capabilities.capabilities()
        if item.id == "tests.cycle_ruleset_proposed"
    )

    readiness = capability.readiness(ws, {})
    assert readiness.state == "missing"
    units = capability.expand_units(ws, {})
    assert len(units) == 1, "a workspace holds one cycle, so one unit"
    # The guarded parents are the matrix rows the rules answer. A schema is not
    # a workspace artifact, so it cannot be one — its staleness rides on the
    # input hash instead, which is what re-expands this unit when a schema is
    # re-derived under an auditor who would otherwise approve stale rules.
    assert set(units[0].parent_refs) == {"rcm:RCM-001"}
    hashes = {item["document_type"]: item["schema_hash"]
              for item in units[0].input_payload["schemas"]}
    assert set(hashes) == {"purchase_order", "vendor_invoice"}
    assert all(value.startswith("sha256:") for value in hashes.values())

    # Re-deriving one schema to different fields moves the unit's identity.
    from app import document_schemas, workspaces
    before = units[0].input_sha1
    document_schemas.save_schema(ws, "vendor_invoice", [
        {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
        {"name": "total_amount", "role": "attribute", "value_type": "number",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
        {"name": "vendor_name", "role": "party", "value_type": "text",
         "cardinality": "one", "verbatim": True, "confidence": "high"},
    ])
    moved = workspaces.load_workspace(ws.id)
    moved.rcm = [_cycle_row()]
    assert capability.expand_units(moved, {})[0].input_sha1 != before


def test_a_matrix_asking_for_no_linked_evidence_expands_nothing():
    """The gate that keeps every other engagement off this path.

    Cycle rules exist to answer transaction-cycle attributes. A matrix that
    classifies none has no question for them, and staging a proposal nobody
    would read would make every audit wait on a cycle it does not have.
    """

    from app.agent.capabilities import tests as test_capabilities

    ws = _cycle_workspace()
    ws.rcm = [{"id": "RCM-002", "control_attributes": [
        {"key": "manual", "evidence_kind": "manual_inspection",
         "requirement": "Someone looks at it."}
    ]}]
    capability = next(
        item for item in test_capabilities.capabilities()
        if item.id == "tests.cycle_ruleset_proposed"
    )

    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def _linkage_ruleset_payload() -> dict:
    """The smallest ruleset this engagement's schemas support."""
    return {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "anchor": {"table": "invoice_data", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk", "left": {"role": "invoice", "field": "invoice_number"},
            "right": {"role": "order", "field": "order_number"},
            "match": "normalized_equal", "rationale": "An invoice cites its order.",
        }],
        "assertions": [{
            "id": "as", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "requirement": "The amount billed must be the amount ordered.",
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }


def test_a_proposal_that_already_exists_is_not_proposed_again():
    from app.agent.capabilities import tests as test_capabilities
    from app import cycle_rulesets

    ws = _cycle_workspace()
    ws.rcm = [_cycle_row()]
    cycle_rulesets.save(ws, {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "anchor": {"table": "invoice_data", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk", "left": {"role": "invoice", "field": "invoice_number"},
            "right": {"role": "order", "field": "order_number"},
            "match": "normalized_equal", "rationale": "An invoice cites its order.",
        }],
        "assertions": [{
            "id": "as", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }, proposed_by="agent")

    capability = next(
        item for item in test_capabilities.capabilities()
        if item.id == "tests.cycle_ruleset_proposed"
    )
    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def test_an_approved_ruleset_asks_for_nothing_further():
    """Approval is the auditor's to give, and the run does not wait for it.

    Proposing the rules is the whole of what this outcome owes. Approval decides
    whether they become *effective* — which is what a cycle test is built
    against — but an audit run that gated on it would be a run waiting on a
    person, and it is deliberately not one.
    """
    from app.agent.capabilities import tests as test_capabilities
    from app import cycle_rulesets

    ws = _cycle_workspace()
    ws.rcm = [_cycle_row()]
    record = cycle_rulesets.save(ws, _linkage_ruleset_payload(), proposed_by="agent")
    cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="auditor@example.com")

    capability = next(
        item for item in test_capabilities.capabilities()
        if item.id == "tests.cycle_ruleset_proposed"
    )

    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def test_an_assertion_states_what_the_fields_must_show():
    """The worker holds the shape the store holds, so a repair is spent here.

    This gate replaces a tolerance check. A bare `tolerance: 0.01` used to
    validate cleanly and then fail at the store, killing the unit after its
    repair allowance was spent — but tolerance went with the operator that took
    one. How close two amounts must be is part of the sentence the rule states,
    read alongside the values, not a number parsed out of the rule.
    """

    with pytest.raises(WorkerResponseValidationError, match="states no requirement"):
        validate_linkage_proposal(
            _proposal(assertions=[{
                "id": "totals", "label": "Totals agree",
                "left": {"role": "invoice", "field": "total_amount"},
                "right": {"role": "order", "field": "total_amount"},
            }]),
            _request(),
        )


def test_a_named_tolerance_passes():
    assert validate_linkage_proposal(
        _proposal(assertions=[{
            "id": "totals", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "rationale": "The amount billed must be the amount ordered.",
        }]),
        _request(),
    )


def test_approving_rules_reopens_the_rows_that_settled_for_a_fallback():
    """Approval must change what the matrix's cycle rows are answered with.

    Both readiness and expansion asked only whether a row had *an* executable
    test. A transaction-cycle row that generated a prose fallback while no
    ruleset was approved therefore counted as covered, so approving one left
    every such row exactly as it was — the approval had no effect at all, and
    nothing said so.
    """

    from app import workspaces
    from app.agent.capabilities import tests as test_capabilities

    ws = _cycle_workspace()
    ws.rcm = [_cycle_row()]
    fallback = [{"test_kind": "qa", "executable": True, "status": "ready",
                 "created_by": "agent"}]

    # No approved ruleset: the fallback is the honest answer and stands.
    assert test_capabilities._awaits_cycle_test(ws, ws.rcm[0], fallback) is False

    _approve_ruleset(ws)
    ws = workspaces.load_workspace(ws.id)
    ws.rcm = [_cycle_row()]

    # Approved: the row is answerable by linked evidence and is not covered.
    assert test_capabilities._awaits_cycle_test(ws, ws.rcm[0], fallback) is True
    # A row that already carries the cycle test is left alone.
    cycle = [{"test_kind": "cycle_vouch", "executable": True, "status": "ready",
              "created_by": "agent"}]
    assert test_capabilities._awaits_cycle_test(ws, ws.rcm[0], cycle) is False
    # And a row asking for no linked evidence is never reopened by an approval.
    plain = {"id": "RCM-9", "control_attributes": [
        {"key": "manual", "evidence_kind": "manual_inspection"}]}
    assert test_capabilities._awaits_cycle_test(ws, plain, fallback) is False


# ------------------------------------------------- answering what was asked
def test_a_requirement_with_no_coverage_entry_is_refused():
    """The check that used to fire three stages downstream fires here.

    It fired at ``tests.specified``, by which time the ruleset had been approved
    and an approved ruleset is immutable — so repairing a gap cost a successor
    proposal and a second signature, for a defect this worker's own bounded loop
    could fix for one attempt. The proposal below asserts something real and
    says nothing about the requirement it was handed.
    """

    with pytest.raises(WorkerResponseValidationError) as raised:
        validate_linkage_proposal(
            _proposal(),
            _request(requirements=[_requirement()]),
        )

    message = str(raised.value)
    assert "answers 0 of 1 supplied requirement" in message
    # The requirement in its own words, so the repair turn knows what to answer.
    assert "The invoice agrees to the order it bills against." in message


def test_coverage_naming_an_assertion_that_does_not_exist_is_refused():
    """The one gap that reads as coverage from every other angle.

    The requirement is answered, the proposal validates, and the executor then
    finds no assertion to derive a comparison from — so the row is committed
    uncontracted and nothing says why.
    """

    with pytest.raises(WorkerResponseValidationError, match="does not contain"):
        validate_linkage_proposal(
            _proposal(coverage=[_covered(assertion_ids=["as_invented"])]),
            _request(requirements=[_requirement()]),
        )


def test_an_answered_requirement_passes():
    proposal = validate_linkage_proposal(
        _proposal(coverage=[_covered()]),
        _request(requirements=[_requirement()]),
    )

    assert proposal["coverage"] == [_covered()]


def test_a_requirement_these_schemas_cannot_express_is_declared_not_guessed():
    """Saying so is the honest outcome, and the row is re-routed on it.

    Reaching for a nearby field instead — vendor names when the requirement is
    about amounts — proves something else, and the control then reads as tested
    when it is not.
    """

    proposal = validate_linkage_proposal(
        _proposal(coverage=[{
            "rcm_id": "RCM-1",
            "control_attribute": "invoice_match",
            "unsupported": True,
            "reason": "No induced type states a delivery quantity.",
        }]),
        _request(requirements=[_requirement()]),
    )

    assert proposal["coverage"][0]["unsupported"] is True


def test_an_unsupported_requirement_without_a_reason_is_refused():
    """An auditor has to be told which field the schemas would have to carry."""

    with pytest.raises(WorkerResponseValidationError, match="unsupported with no reason"):
        validate_linkage_proposal(
            _proposal(coverage=[{
                "rcm_id": "RCM-1",
                "control_attribute": "invoice_match",
                "unsupported": True,
            }]),
            _request(requirements=[_requirement()]),
        )


def test_two_controls_wanting_the_same_evidence_name_one_assertion():
    """A matrix demands the same test from every control that depends on it.

    An amount wanted by both ``invoice_match`` and ``payment_terms`` is one
    assertion, named twice. Writing a second rule over the same fields answers
    nothing the first did not and files a duplicate test.
    """

    proposal = validate_linkage_proposal(
        _proposal(coverage=[
            _covered(),
            _covered(control_attribute="payment_terms"),
        ]),
        _request(requirements=[
            _requirement(),
            _requirement(control_attribute="payment_terms"),
        ]),
    )

    assert len(proposal["assertions"]) == 1
    assert len(proposal["coverage"]) == 2


def test_one_requirement_answered_twice_is_refused():
    """Two entries for one requirement is two answers, and one of them is wrong."""

    with pytest.raises(WorkerResponseValidationError, match="two coverage entries"):
        validate_linkage_proposal(
            _proposal(coverage=[_covered(), _covered()]),
            _request(requirements=[_requirement()]),
        )


# ------------------------------------- a requirement is held to what it reads
#
# Each shape below is one an approved ruleset actually took. A procurement
# proposal answered a three-way match with a rule that the voucher is signed,
# so a short delivery only the receipt states was never compared; a treasury
# one answered a five-term confirmation match with its rate alone. Neither is
# about procurement or treasury: both are a coverage entry naming rules that do
# not read the records its requirement is about.
MATCH_SCHEMAS = SCHEMAS + [
    {
        "document_type": "goods_receipt",
        "fields": [
            {"name": "receipt_number", "role": "identifier", "value_type": "identifier"},
            {"name": "order_number", "role": "identifier", "value_type": "identifier"},
            {"name": "quantity_received", "role": "attribute", "value_type": "number"},
        ],
    },
    {
        "document_type": "payment_voucher",
        "fields": [
            {"name": "invoice_number", "role": "identifier", "value_type": "identifier"},
            {"name": "signature", "role": "control", "value_type": "text"},
        ],
    },
]
# The order carries a quantity only in this vocabulary, so the receipt has
# something of the order's to agree with.
MATCH_SCHEMAS[1] = {
    "document_type": "purchase_order",
    "fields": [
        {"name": "order_number", "role": "identifier", "value_type": "identifier"},
        {"name": "total_amount", "role": "attribute", "value_type": "number"},
        {"name": "quantity", "role": "attribute", "value_type": "number"},
    ],
}


def _match_proposal(*, extra_assertions=(), coverage=(), **overrides) -> dict:
    proposal = _proposal(
        roles=[
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
            {"name": "receipt", "document_type": "goods_receipt",
             "cardinality": "one", "required": True},
            {"name": "payment", "document_type": "payment_voucher",
             "cardinality": "one", "required": True},
        ],
        join_keys=[
            {"id": "jk_order", "match": "normalized_equal",
             "left": {"role": "invoice", "field": "order_number"},
             "right": {"role": "order", "field": "order_number"},
             "rationale": "An invoice cites the order it bills against."},
            {"id": "jk_receipt", "match": "normalized_equal",
             "left": {"role": "order", "field": "order_number"},
             "right": {"role": "receipt", "field": "order_number"},
             "rationale": "A receipt records the order it fulfils."},
            {"id": "jk_payment", "match": "normalized_equal",
             "left": {"role": "invoice", "field": "invoice_number"},
             "right": {"role": "payment", "field": "invoice_number"},
             "rationale": "A voucher records the invoice it settles."},
        ],
        assertions=[
            {"id": "as_total", "label": "Totals agree",
             "left": {"role": "invoice", "field": "total_amount"},
             "right": {"role": "order", "field": "total_amount"},
             "requirement": "The amount billed is the amount ordered.",
             "rationale": "An invoice bills what was ordered."},
            {"id": "as_signed", "label": "Voucher signed",
             "left": {"role": "payment", "field": "signature"}, "right": None,
             "requirement": "The voucher carries a signature.",
             "rationale": "A signature shows the payment was authorised."},
            *extra_assertions,
        ],
        coverage=list(coverage),
        **overrides,
    )
    return proposal


RECEIPT_QUANTITY = {
    "id": "as_received", "label": "Received quantity agrees with order",
    "left": {"role": "receipt", "field": "quantity_received"},
    "right": {"role": "order", "field": "quantity"},
    "requirement": "The quantity received is the quantity ordered.",
    "rationale": "An order billed in full must have been received in full.",
}


def _match_request():
    return _Request(
        schemas=MATCH_SCHEMAS,
        requirements=[_requirement(
            requirement=(
                "The invoice is matched to the purchase order and the goods "
                "receipt before it is approved for payment."
            ),
        )],
    )


def test_a_requirement_answered_by_a_rule_over_another_record_is_refused():
    with pytest.raises(WorkerResponseValidationError) as raised:
        validate_linkage_proposal(
            _match_proposal(coverage=[_covered(
                reads=["invoice", "order", "receipt"],
                assertion_ids=["as_signed"],
            )]),
            _match_request(),
        )

    message = str(raised.value)
    assert "answers 0 of 1 supplied requirement" in message
    assert "'as_signed', which reads payment" in message
    for role in ("invoice", "order", "receipt"):
        assert f"'{role}'" in message.split("is about ", 1)[1]


def test_a_match_that_never_reads_one_of_its_records_is_refused():
    """Invoice against order is a real comparison; it still says nothing of the
    receipt the requirement also names."""

    with pytest.raises(WorkerResponseValidationError) as raised:
        validate_linkage_proposal(
            _match_proposal(coverage=[_covered(
                reads=["invoice", "order", "receipt"],
                assertion_ids=["as_total"],
            )]),
            _match_request(),
        )

    message = str(raised.value)
    assert "is about 'receipt', and none of its assertions (as_total)" in message
    assert "not that what it states agrees" in message


def test_a_match_reading_every_record_it_names_passes():
    proposal = validate_linkage_proposal(
        _match_proposal(
            extra_assertions=[RECEIPT_QUANTITY],
            coverage=[_covered(
                reads=["invoice", "order", "receipt"],
                assertion_ids=["as_total", "as_received"],
            )],
        ),
        _match_request(),
    )

    assert proposal["coverage"][0]["assertion_ids"] == ["as_total", "as_received"]


def test_a_part_no_field_pair_can_express_is_stated_as_a_gap():
    proposal = validate_linkage_proposal(
        _match_proposal(coverage=[_covered(
            reads=["invoice", "order", "receipt"],
            assertion_ids=["as_total"],
            gaps=[{"role": "receipt",
                   "reason": "The order states no quantity to compare against."}],
        )]),
        _match_request(),
    )

    assert proposal["coverage"][0]["gaps"][0]["role"] == "receipt"


def test_a_gap_must_say_why():
    with pytest.raises(WorkerResponseValidationError, match="with no reason"):
        validate_linkage_proposal(
            _match_proposal(coverage=[_covered(
                reads=["invoice", "order", "receipt"],
                assertion_ids=["as_total"],
                gaps=[{"role": "receipt", "reason": ""}],
            )]),
            _match_request(),
        )


def test_a_gap_on_a_role_the_entry_does_not_read_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="does not list in reads"):
        validate_linkage_proposal(
            _match_proposal(coverage=[_covered(
                reads=["invoice", "order"],
                assertion_ids=["as_total"],
                gaps=[{"role": "receipt", "reason": "No quantity on the order."}],
            )]),
            _match_request(),
        )


def test_a_requirement_about_one_record_is_answered_by_a_one_sided_rule():
    """Nothing here counts roles. A single-document cycle — a voucher reached
    from its population row — is a cycle, and a field that must be stated is a
    one-sided assertion on that one record."""

    proposal = validate_linkage_proposal(
        _match_proposal(coverage=[_covered(
            reads=["payment"], assertion_ids=["as_signed"],
        )]),
        _Request(
            schemas=MATCH_SCHEMAS,
            requirements=[_requirement(
                requirement="The payment voucher is signed before it is paid.",
            )],
        ),
    )

    assert proposal["coverage"][0]["reads"] == ["payment"]


def test_one_requirement_may_need_several_assertions():
    """Five terms that must agree are five assertions, all of them named."""

    proposal = _proposal(
        assertions=[
            {"id": "as_total", "label": "Totals agree",
             "left": {"role": "invoice", "field": "total_amount"},
             "right": {"role": "order", "field": "total_amount"},
             "requirement": "The amount billed is the amount ordered.",
             "rationale": "An invoice bills what was ordered."},
            {"id": "as_order", "label": "Order reference stated",
             "left": {"role": "invoice", "field": "order_number"}, "right": None,
             "requirement": "The invoice names its order.",
             "rationale": "An invoice with no order cannot be matched."},
        ],
        coverage=[_covered(assertion_ids=["as_total", "as_order"])],
    )

    validated = validate_linkage_proposal(proposal, _request([_requirement()]))

    assert validated["coverage"][0]["assertion_ids"] == ["as_total", "as_order"]


def test_an_entry_declaring_no_roles_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="declares no roles"):
        validate_linkage_proposal(
            _proposal(coverage=[_covered(reads=[])]),
            _request(requirements=[_requirement()]),
        )


def test_a_role_in_reads_the_proposal_does_not_hold_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="not a role of this"):
        validate_linkage_proposal(
            _proposal(coverage=[_covered(reads=["invoice", "order", "ledger"])]),
            _request(requirements=[_requirement()]),
        )


def test_a_role_no_join_key_can_reach_is_already_a_stated_gap():
    proposal = validate_linkage_proposal(
        _proposal(
            coverage=[_covered(reads=["invoice", "order", "statement"])],
            unreachable=[{"role": "statement",
                          "reason": "The statement carries no identifier field."}],
        ),
        _request(requirements=[_requirement()]),
    )

    assert proposal["coverage"][0]["reads"][-1] == "statement"


def test_the_earlier_single_assertion_shape_still_parses():
    parsed = _linkage_response_schema(json.dumps(_proposal(coverage=[{
        "rcm_id": "RCM-1", "control_attribute": "invoice_match",
        "reads": ["invoice", "order"], "assertion_id": "as_total",
    }])))

    assert parsed["coverage"][0]["assertion_ids"] == ["as_total"]


def test_a_proposal_is_still_valid_where_the_matrix_asks_nothing():
    """Rules may be proposed before a matrix asks anything of them."""

    proposal = validate_linkage_proposal(_proposal(), _request())

    assert proposal["assertions"]
    assert not proposal.get("coverage")


def test_a_malformed_id_is_refused_where_a_repair_can_still_fix_it():
    """The store's id rule, asked one turn earlier.

    ``cycle_rulesets`` raises on a malformed id at commit — after the model turn
    has succeeded, outside the repair loop, costing the whole run. Asked here it
    costs one turn, and the message carries the corrected id so the repair is
    mechanical rather than a guess.
    """

    with pytest.raises(WorkerResponseValidationError) as raised:
        validate_linkage_proposal(
            _proposal(assertions=[{
                "id": "invoice_match.totals_agree", "label": "Totals agree",
                "left": {"role": "invoice", "field": "total_amount"},
                "right": {"role": "order", "field": "total_amount"},
                "requirement": "The invoice agrees to the order.",
            }]),
            _request(),
        )

    message = str(raised.value)
    assert "invoice_match_totals_agree" in message
    assert not cycle_rulesets.valid_rule_id("invoice_match.totals_agree")


def test_every_id_is_reported_at_once():
    """A proposal that got the convention wrong got it wrong everywhere.

    Naming the first and stopping would spend a repair turn per id; the treasury
    proposal that surfaced this carried 54.
    """

    with pytest.raises(WorkerResponseValidationError) as raised:
        validate_linkage_proposal(
            _proposal(
                join_keys=[{
                    "id": "jk.order", "match": "normalized_equal",
                    "left": {"role": "invoice", "field": "order_number"},
                    "right": {"role": "order", "field": "order_number"},
                    "rationale": "An invoice cites the order it bills against.",
                }],
                assertions=[{
                    "id": "invoice_match.totals_agree", "label": "Totals agree",
                    "left": {"role": "invoice", "field": "total_amount"},
                    "right": {"role": "order", "field": "total_amount"},
                    "requirement": "The invoice agrees to the order.",
                }],
            ),
            _request(),
        )

    message = str(raised.value)
    assert "2 ids" in message
    assert "jk_order" in message and "invoice_match_totals_agree" in message


# --------------------------------------------------------------------------- #
# The roles are given (step 5e of docs/rcm-generation-redesign.md)
# --------------------------------------------------------------------------- #
CYCLE = {
    "name": "Procure-to-pay",
    "steps": [
        {
            "name": "Purchase order",
            "roles": [{"name": "order", "document_type": "purchase_order"}],
            "populations": [{"table": "po_data", "anchor": True}],
            "themes": [],
        },
        {
            "name": "Invoice processing",
            "roles": [{"name": "invoice", "document_type": "vendor_invoice"}],
            "populations": [],
            "themes": [],
        },
    ],
    "cross_cutting": None,
}


def _cycle_request(cycle=None, requirements=None):
    return _Request(
        schemas=SCHEMAS,
        requirements=requirements,
        cycle=CYCLE if cycle is None else cycle,
    )


def test_a_proposal_using_exactly_the_cycle_s_roles_is_accepted():
    validate_linkage_proposal(_proposal(), _cycle_request())


def test_a_role_the_cycle_does_not_declare_is_refused():
    proposal = _proposal()
    proposal["roles"].append({
        "name": "receipt", "document_type": "purchase_order",
        "cardinality": "one", "required": True,
    })

    with pytest.raises(WorkerResponseValidationError) as error:
        validate_linkage_proposal(proposal, _cycle_request())

    message = str(error.value)
    assert "not one the cycle declares" in message
    assert "order, invoice" in message


def test_a_role_filled_by_a_different_type_than_the_cycle_states_is_refused():
    proposal = _proposal()
    proposal["roles"][0]["document_type"] = "purchase_order"

    with pytest.raises(WorkerResponseValidationError) as error:
        validate_linkage_proposal(proposal, _cycle_request())

    assert "is filled by 'vendor_invoice' in the cycle" in str(error.value)


def test_a_declared_role_the_response_drops_is_refused():
    proposal = _proposal()
    proposal["roles"] = [proposal["roles"][0]]
    proposal["join_keys"] = []
    proposal["assertions"] = [{
        "id": "as_stated", "label": "Approval is stated",
        "requirement": "The invoice records an approval.",
        "left": {"role": "invoice", "field": "approval"},
        "right": None,
        "rationale": "An unapproved invoice should not be paid.",
    }]

    with pytest.raises(WorkerResponseValidationError) as error:
        validate_linkage_proposal(proposal, _cycle_request())

    message = str(error.value)
    assert "the response omits it" in message
    assert "unreachable" in message


def test_a_dropped_role_declared_unreachable_is_accepted_with_its_reason():
    """The ``purchase_requisition`` case: a type induced with no identifier
    field can be reached by no join key, so the whole ruleset would be refused
    over it. Saying so is what makes the gap visible instead."""

    proposal = _proposal()
    proposal["roles"] = [proposal["roles"][0]]
    proposal["join_keys"] = []
    proposal["assertions"] = [{
        "id": "as_stated", "label": "Approval is stated",
        "requirement": "The invoice records an approval.",
        "left": {"role": "invoice", "field": "approval"},
        "right": None,
        "rationale": "An unapproved invoice should not be paid.",
    }]
    proposal["unreachable"] = [
        {"role": "order", "reason": "purchase_order induced no identifier field"}
    ]

    validate_linkage_proposal(proposal, _cycle_request())


def test_an_unreachable_role_without_a_reason_is_refused():
    proposal = _proposal()
    proposal["roles"] = [proposal["roles"][0]]
    proposal["join_keys"] = []
    proposal["unreachable"] = [{"role": "order", "reason": ""}]

    with pytest.raises(WorkerResponseValidationError) as error:
        validate_linkage_proposal(proposal, _cycle_request())

    assert "states no reason" in str(error.value)


def test_an_unreachable_role_the_cycle_never_declared_is_refused():
    proposal = _proposal()
    proposal["unreachable"] = [{"role": "ghost", "reason": "nothing to join on"}]

    with pytest.raises(WorkerResponseValidationError) as error:
        validate_linkage_proposal(proposal, _cycle_request())

    assert "Unreachable role 'ghost' is not one the cycle declares" in str(error.value)


def test_the_unreachable_list_is_parsed_off_the_response():
    parsed = _linkage_response_schema(
        json.dumps(_proposal(unreachable=[{"role": "order", "reason": "no identifier"}]))
    )
    assert parsed["unreachable"] == [{"role": "order", "reason": "no identifier"}]
    assert _linkage_response_schema(json.dumps(_proposal()))["unreachable"] == []


def test_a_role_no_join_key_reaches_is_refused_in_the_turn_that_can_fix_it():
    """``cycle_rulesets.validate`` enforces this too, at commit — after the turn
    has succeeded and outside any repair loop, so it costs the whole run. It
    cost one, on treasuryfull, for exactly this role shape."""

    proposal = _proposal()
    proposal["roles"].append({
        "name": "receipt", "document_type": "purchase_order",
        "cardinality": "one", "required": True,
    })

    with pytest.raises(WorkerResponseValidationError) as error:
        validate_linkage_proposal(proposal, _request())

    message = str(error.value)
    assert "No join key reaches role 'receipt'" in message
    # Two honest ways out, and the repair is told both.
    assert "join key" in message and "unreachable" in message


def test_a_role_reached_through_another_role_is_accepted():
    """Reachability is transitive: the walk follows join keys outward from the
    anchor rather than requiring every role to touch it directly.

    Asked without a designed cycle, which also pins that such an engagement
    still proposes roles of its own: the gate has no vocabulary to hold it to.
    """

    proposal = _proposal()
    proposal["roles"].append({
        "name": "receipt", "document_type": "purchase_order",
        "cardinality": "one", "required": True,
    })
    proposal["join_keys"].append({
        "id": "jk_receipt", "match": "normalized_equal",
        "left": {"role": "order", "field": "order_number"},
        "right": {"role": "receipt", "field": "order_number"},
        "rationale": "A receipt cites the order it fulfils.",
    })

    validate_linkage_proposal(proposal, _request())
