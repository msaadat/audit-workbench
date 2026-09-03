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
                    "cycle_requirements", {"required_comparisons": list(requirements)}
                )
            )
        self.context = _Context(items)


def _request(requirements=None):
    return _Request(schemas=SCHEMAS, requirements=requirements)


def _requirement(**overrides) -> dict:
    """One thing the matrix has decided this cycle must demonstrate."""
    item = {
        "rcm_id": "RCM-1",
        "control_attribute": "invoice_match",
        "requirement": "The invoice agrees to the order it bills against.",
        "comparison": "totals_agree",
        "left": {"document_type": "vendor_invoice", "field": "total_amount"},
        "right": {"document_type": "purchase_order", "field": "total_amount"},
        "why": "The amount billed must be the amount ordered.",
    }
    item.update(overrides)
    return item


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
def test_a_proposal_leaving_a_matrix_requirement_unanswered_is_refused():
    """The check that used to fire three stages downstream fires here.

    It fired at ``tests.specified``, by which time the ruleset had been approved
    and an approved ruleset is immutable — so repairing a gap cost a successor
    proposal and a second signature, for a defect this worker's own bounded loop
    could fix for one attempt. The proposal below binds the documents and
    asserts nothing about the amounts the matrix asked it to compare.
    """

    with pytest.raises(WorkerResponseValidationError, match="answers 0 of 1 required field pair"):
        validate_linkage_proposal(
            _proposal(assertions=[{
                "id": "approval_present", "label": "Approval present",
                "left": {"role": "invoice", "field": "approval"},
                "right": None,
                "requirement": "The invoice carries an approval.",
            }]),
            _request(requirements=[_requirement()]),
        )


def test_the_refusal_names_the_fields_the_assertion_has_to_read():
    """A repair turn acts on operands, not on a count."""

    with pytest.raises(WorkerResponseValidationError) as raised:
        validate_linkage_proposal(
            _proposal(assertions=[{
                "id": "approval_present", "label": "Approval present",
                "left": {"role": "invoice", "field": "approval"},
                "right": None,
                "requirement": "The invoice carries an approval.",
            }]),
            _request(requirements=[_requirement()]),
        )

    message = str(raised.value)
    assert "vendor_invoice.total_amount agrees with purchase_order.total_amount" in message


def test_the_refusal_does_not_name_the_matrix_key():
    """The message must not hand back an identifier the store would reject.

    It used to name each gap ``control_attribute.comparison``, and a proposer
    reading "add an assertion for each of these" took those dotted strings for
    the ids it was being asked to write. One treasury run wrote 54 of them,
    passed coverage, and died at commit on the first dot — a whole run lost to
    a format this message taught it.
    """

    with pytest.raises(WorkerResponseValidationError) as raised:
        validate_linkage_proposal(
            _proposal(assertions=[{
                "id": "approval_present", "label": "Approval present",
                "left": {"role": "invoice", "field": "approval"},
                "right": None,
                "requirement": "The invoice carries an approval.",
            }]),
            _request(requirements=[_requirement()]),
        )

    assert "invoice_match.totals_agree" not in str(raised.value)


def test_two_controls_wanting_the_same_pair_are_one_assertion():
    """Coverage is by operands, so the same pair twice is one piece of work.

    A matrix names the same comparison from every control that depends on it —
    an amount wanted by both ``invoice_match`` and ``payment_terms`` is one
    test, demanded twice. Counting them apart overstated the work by more than
    half on a real engagement and asked for duplicate rules distinguishable
    only by an id no coverage check reads.
    """

    proposal = validate_linkage_proposal(
        _proposal(assertions=[{
            "id": "totals", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "requirement": "The invoice agrees to the order.",
        }]),
        _request(requirements=[
            _requirement(),
            _requirement(control_attribute="payment_terms", comparison="amount_agrees"),
        ]),
    )

    assert len(proposal["assertions"]) == 1


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


def test_an_answered_requirement_passes():
    """The assertion reads exactly the two fields the comparison names."""

    proposal = validate_linkage_proposal(
        _proposal(assertions=[{
            "id": "totals", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "requirement": "The amount billed must be the amount ordered.",
        }]),
        _request(requirements=[_requirement()]),
    )

    assert proposal["assertions"][0]["id"] == "totals"


def test_a_requirement_a_join_already_binds_needs_no_assertion():
    """Repeating a join key files a check that cannot fail.

    The pair exists only because those two fields matched, so an assertion over
    them would read as coverage while being incapable of finding an exception —
    and demanding one is how a gate turns into busywork.
    """

    proposal = validate_linkage_proposal(
        _proposal(assertions=[{
            "id": "totals", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "requirement": "The amount billed must be the amount ordered.",
        }]),
        _request(requirements=[
            _requirement(),
            _requirement(
                comparison="links_to_order",
                left={"document_type": "vendor_invoice", "field": "order_number"},
                right={"document_type": "purchase_order", "field": "order_number"},
            ),
        ]),
    )

    assert proposal["assertions"][0]["id"] == "totals"


def test_a_requirement_outside_this_cycle_is_not_this_proposal_to_answer():
    """A comparison naming a document type the cycle does not carry."""

    proposal = validate_linkage_proposal(
        _proposal(assertions=[{
            "id": "totals", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "requirement": "The amount billed must be the amount ordered.",
        }]),
        _request(requirements=[
            _requirement(),
            _requirement(
                comparison="payroll_rate",
                left={"document_type": "payslip", "field": "gross_pay"},
                right={"document_type": "employment_contract", "field": "rate"},
            ),
        ]),
    )

    assert proposal["assertions"][0]["id"] == "totals"


def test_a_proposal_is_still_valid_where_the_matrix_asks_nothing():
    """Rules may be proposed before a matrix asks anything of them."""

    proposal = validate_linkage_proposal(_proposal(), _request())

    assert proposal["assertions"]


def test_the_prompt_tells_the_worker_what_the_requirements_are_for():
    from app.agent.workers.tests import LINKAGE_SYSTEM

    assert "must answer every one" in LINKAGE_SYSTEM
    # Answering an equivalent pair is what produced the live mismatch: the
    # matrix wanted the receipt to name its order, the proposal bound the order
    # naming its receipt, and neither turn had seen the other.
    assert "in the matrix's own operands" in LINKAGE_SYSTEM
    assert "needs no assertion" in LINKAGE_SYSTEM
