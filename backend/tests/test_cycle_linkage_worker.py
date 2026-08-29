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

    def __init__(self, *, schemas=(), **unit_input):
        self.unit_input = unit_input
        # One atomic item carrying every type, the shape the adapter supplies:
        # a cycle describes how the whole set relates, so a vocabulary admitted
        # piecemeal would yield a proposal missing a role and not say which.
        self.context = _Context(
            [_ContextItem("cycle_schemas", {"schemas": list(schemas)})]
            if schemas
            else []
        )


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
                        "operator": "numeric_within",
                        "tolerance": {"absolute": 1.0},
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
            "operator": "numeric_within", "tolerance": {"absolute": 1.0},
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
            "operator": "numeric_within", "tolerance": {"absolute": 1.0},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }, proposed_by="agent")

    capability = next(
        item for item in test_capabilities.capabilities()
        if item.id == "tests.cycle_ruleset_proposed"
    )
    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def test_a_bare_number_tolerance_is_refused_before_the_store_sees_it():
    """The first live proposal stated `tolerance: 0.01` and validated cleanly.

    It then failed at the store, which requires the kind to be named — so the
    unit died after its repair allowance was already spent, on a defect the
    loop existed to fix. The worker now holds the shape the store holds.
    """

    with pytest.raises(WorkerResponseValidationError, match="what kind it is"):
        validate_linkage_proposal(
            _proposal(assertions=[{
                "id": "totals", "label": "Totals agree",
                "left": {"role": "invoice", "field": "total_amount"},
                "right": {"role": "order", "field": "total_amount"},
                "operator": "numeric_within", "tolerance": 0.01,
                "rationale": "The amount billed must be the amount ordered.",
            }]),
            _request(),
        )


def test_a_named_tolerance_passes():
    assert validate_linkage_proposal(
        _proposal(assertions=[{
            "id": "totals", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "operator": "numeric_within", "tolerance": {"absolute": 0.01},
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
