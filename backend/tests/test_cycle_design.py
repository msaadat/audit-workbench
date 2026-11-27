"""The cycle is designed whole: its steps, and the rules linking its documents.

One turn, after the schemas exist and before the matrix. It names the steps
from the memorandum, places the document types on them as roles, and writes
the anchor, join keys and assertions in the induced field vocabulary. The
commit stores the shape and proposes the ruleset in one transaction; which of
the rules answer a matrix requirement is decided later, when that row's test is
written (``test_cycle_contract_write_back``).

These are the checks the retired ``tests.cycle_linkage`` turn ran, minus
coverage, now run against the roles the same response declared.
"""

from __future__ import annotations

import json

import pytest

from app import cycle_rulesets, document_schemas, workspaces
from app.agent.capabilities import REGISTRY
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.planning import CycleExecutorTarget
from app.agent.workers import planning
from app.agent.workers.model import WorkerResponseValidationError
from app.workspace_transactions import parent_hashes

INVOICE = {"document_type": "vendor_invoice", "fields": [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier"},
    {"name": "order_number", "role": "identifier", "value_type": "identifier"},
    {"name": "total_amount", "role": "attribute", "value_type": "number"},
    {"name": "approval", "role": "control", "value_type": "text"},
]}
ORDER = {"document_type": "purchase_order", "fields": [
    {"name": "order_number", "role": "identifier", "value_type": "identifier"},
    {"name": "total_amount", "role": "attribute", "value_type": "number"},
]}
STATEMENT = {"document_type": "bank_statement", "fields": [
    {"name": "closing_balance", "role": "attribute", "value_type": "number"},
]}


class _Item:
    def __init__(self, source_id, content):
        self.source_id, self.content = source_id, content


class _Context:
    def __init__(self, items):
        self.items = items

    def to_dict(self):
        return {"items": [{"source_id": item.source_id} for item in self.items]}


class _Request:
    def __init__(self, schemas=(INVOICE, ORDER), types=None):
        types = types or [schema["document_type"] for schema in schemas]
        self.unit_input = {
            "risk_themes": [],
            "document_types": [{"document_type": kind, "documents": 1} for kind in types],
        }
        items = [
            _Item("current_apm", "# APM"),
            _Item("planning_context", {"context": {}}),
            _Item("table_metadata", {"table": "register",
                                     "columns": [{"name": "INVOICE_NO"}]}),
        ]
        if schemas:
            items.append(_Item("cycle_schemas", {"schemas": list(schemas)}))
        self.context = _Context(items)


def _design(**rules):
    design = {
        "name": "Procure to pay",
        "steps": [
            {"name": "Order", "themes": [], "populations": [],
             "roles": [{"name": "order", "document_type": "purchase_order"}]},
            {"name": "Invoice", "themes": [],
             "populations": [{"table": "register", "anchor": True}],
             "roles": [{"name": "invoice", "document_type": "vendor_invoice"}]},
        ],
        "rules": {
            "anchor": {"role": "invoice", "field": "invoice_number", "column": "INVOICE_NO"},
            "join_keys": [{
                "id": "jk_order",
                "left": {"role": "invoice", "field": "order_number"},
                "right": {"role": "order", "field": "order_number"},
                "rationale": "An invoice cites the order it bills against.",
            }],
            "assertions": [],
        },
    }
    design["rules"].update(rules)
    return design


def _validate(design, request=None):
    shaped = planning._cycle_response_schema(json.dumps(design))
    return planning.validate_cycle_proposal(shaped, request or _Request())


# ------------------------------------------------------------------ the gate
def test_a_design_carries_its_rules_with_the_roles_read_off_the_steps():
    proposal = _validate(_design())
    rules = proposal["rules"]
    assert rules["roles"] == [
        {"name": "order", "document_type": "purchase_order", "cardinality": "one", "required": True},
        {"name": "invoice", "document_type": "vendor_invoice", "cardinality": "one", "required": True},
    ]
    assert rules["cycle_label"] == "Procure to pay"
    # The shape is stored without them: the rules go to their own store.
    assert "rules" not in {key for key in proposal["steps"][0]}
    # The total pair was proposed by name and kept by silence.
    assert any(item.get("proposed_by") == "schema_names" for item in rules["assertions"])


def test_a_role_s_cardinality_is_carried_to_the_rules():
    design = _design()
    design["steps"][0]["roles"][0]["cardinality"] = "many"
    (order, _invoice) = _validate(design)["rules"]["roles"]
    assert order["cardinality"] == "many"


def test_with_no_schemas_no_rules_are_owed():
    design = _design()
    del design["rules"]
    proposal = _validate(design, _Request(schemas=(), types=["vendor_invoice", "purchase_order"]))
    assert "rules" not in proposal


def test_schemas_supplied_and_no_rules_returned_is_refused():
    design = _design()
    del design["rules"]
    with pytest.raises(WorkerResponseValidationError, match="has no rules"):
        _validate(design)


def test_rules_need_a_flagged_anchor_population():
    design = _design()
    design["steps"][1]["populations"] = [{"table": "register"}]
    with pytest.raises(WorkerResponseValidationError, match="anchor: true"):
        _validate(design)


def test_an_anchor_column_the_table_lacks_is_refused_in_the_turn():
    with pytest.raises(WorkerResponseValidationError, match="not a column"):
        _validate(_design(anchor={"role": "invoice", "field": "invoice_number",
                                  "column": "invoice_number"}))


def test_a_join_on_an_amount_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="Only identifier fields can join"):
        _validate(_design(join_keys=[{
            "id": "jk_total",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
        }]))


def test_a_rule_on_a_role_no_step_holds_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="no step of the cycle holds"):
        _validate(_design(assertions=[{
            "id": "as_receipt", "label": "x", "requirement": "x",
            "left": {"role": "receipt", "field": "total_amount"}, "right": None,
        }]))


def test_a_field_the_type_does_not_carry_is_refused():
    with pytest.raises(WorkerResponseValidationError, match="does not carry"):
        _validate(_design(assertions=[{
            "id": "as_terms", "label": "x", "requirement": "x",
            "left": {"role": "order", "field": "delivery_terms"}, "right": None,
        }]))


def test_an_assertion_must_say_what_it_shows():
    with pytest.raises(WorkerResponseValidationError, match="states no requirement"):
        _validate(_design(assertions=[{
            "id": "as_stated", "label": "x", "requirement": "",
            "left": {"role": "invoice", "field": "total_amount"}, "right": None,
        }]))


def test_every_malformed_id_is_reported_at_once():
    with pytest.raises(WorkerResponseValidationError) as raised:
        _validate(_design(
            join_keys=[{
                "id": "JK.order",
                "left": {"role": "invoice", "field": "order_number"},
                "right": {"role": "order", "field": "order_number"},
            }],
            assertions=[{
                "id": "RCM-1.total", "label": "x", "requirement": "x",
                "left": {"role": "invoice", "field": "total_amount"}, "right": None,
            }],
        ))
    message = str(raised.value)
    assert "'JK.order' should be 'jk_order'" in message
    assert "'RCM-1.total' should be 'rcm_1_total'" in message


def test_a_role_no_join_key_reaches_is_refused_in_the_turn_that_can_fix_it():
    with pytest.raises(WorkerResponseValidationError, match="No join key reaches role 'order'"):
        _validate(_design(join_keys=[]))


def test_a_role_declared_unreachable_leaves_the_rules_and_keeps_its_step():
    proposal = _validate(_design(
        join_keys=[],
        unreachable=[{"role": "order", "reason": "No identifier the invoice cites."}],
        assertions=[{
            "id": "as_stated", "label": "x", "requirement": "The invoice states a total.",
            "left": {"role": "invoice", "field": "total_amount"}, "right": None,
        }],
    ))
    assert [role["name"] for role in proposal["rules"]["roles"]] == ["invoice"]
    assert proposal["steps"][0]["roles"][0]["name"] == "order"


def test_an_unreachable_role_needs_a_reason():
    with pytest.raises(WorkerResponseValidationError, match="states no reason"):
        _validate(_design(join_keys=[], unreachable=[{"role": "order", "reason": ""}]))


def test_a_role_whose_type_has_no_schema_needs_no_rules():
    """A held type nothing was induced for can be a role; the rules leave it out."""

    design = _design()
    design["steps"].append({
        "name": "Bank", "themes": [], "populations": [],
        "roles": [{"name": "statement", "document_type": "bank_statement"}],
    })
    request = _Request(types=["vendor_invoice", "purchase_order", "bank_statement"])
    proposal = _validate(design, request)
    assert "statement" not in {role["name"] for role in proposal["rules"]["roles"]}


def test_the_prompt_carries_the_schemas_and_pairs_between_types():
    user = json.loads(planning._cycle_user(_Request()))
    assert [schema["document_type"] for schema in user["DOCUMENT SCHEMAS"]] == [
        "vendor_invoice", "purchase_order",
    ]
    (pair,) = user["FIELD PAIRS"]
    assert {pair["left"]["document_type"], pair["right"]["document_type"]} == {
        "vendor_invoice", "purchase_order",
    }


# ---------------------------------------------------------------- the commit
def _workspace():
    workspace = workspaces.create_workspace("Cycle design commit")
    for schema in (INVOICE, ORDER):
        document_schemas.save_schema(workspace, schema["document_type"], schema["fields"])
    workspace = workspaces.load_workspace(workspace.id)
    workspace.add_table("register.csv", b"INVOICE_NO,PO\nI1,P1\n")
    return workspaces.load_workspace(workspace.id)


def _commit(workspace, proposal, run_id="run-cycle"):
    request = ExecutorRequest(
        executor_id="planning.cycle",
        capability_id="planning.cycle_ready",
        unit_id="cycle",
        proposal=proposal,
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, ["planning:apm"]),
        activity={"artifact_refs": ["planning:cycle"]},
    )
    target = CycleExecutorTarget(workspace, run_id)
    receipt = EXECUTORS.execute(request, target)
    return target.workspace, receipt


def test_the_commit_stores_the_shape_and_proposes_the_rules_together():
    workspace = _workspace()
    proposal = _validate(_design())

    workspace, receipt = _commit(workspace, proposal)

    assert [step["name"] for step in workspace.planning["cycle"]["steps"]] == ["Order", "Invoice"]
    (record,) = cycle_rulesets.list_rulesets(workspace)
    assert record["status"] == "proposed"
    assert record["anchor"] == {"table": "register", "column": "INVOICE_NO",
                                "role": "invoice", "field": "invoice_number"}
    # Written against the shape stored in the same commit.
    assert record["cycle_sha1"] == parent_hashes(workspace, ["planning:cycle"])["planning:cycle"]
    assert receipt.output["rules"]["ruleset_id"] == record["ruleset_id"]
    assert receipt.output["rules"]["ruleset_action"] == "proposed"
    assert workspace.planning["cycle"]["schema_basis"] == cycle_rulesets.schema_basis(workspace)


def test_a_design_restating_stored_rules_files_no_second_proposal():
    workspace = _workspace()
    proposal = _validate(_design())
    workspace, _ = _commit(workspace, proposal, "run-one")
    workspace = workspaces.load_workspace(workspace.id)

    workspace, receipt = _commit(workspace, proposal, "run-two")

    assert len(cycle_rulesets.list_rulesets(workspace)) == 1
    assert receipt.output["rules"]["ruleset_action"] == "unchanged"


# --------------------------------------------------------------- readiness
def _cycle_readiness(workspace):
    return REGISTRY.get("planning.cycle_ready").readiness(workspace, {})


def test_a_cycle_with_steps_and_no_rules_is_missing_them_once_schemas_exist():
    """Treasury's state: a cycle drawn before the design proposed its rules."""

    workspace = _workspace()
    workspace.update_planning({"cycle": {
        "name": "P2P",
        "steps": [{"name": "Invoice", "roles": [], "populations": [], "themes": []}],
        "cross_cutting": None,
    }}, agent=True)
    workspace = workspaces.load_workspace(workspace.id)

    readiness = _cycle_readiness(workspace)

    assert readiness.state == "missing"
    assert "no rules" in readiness.reasons[0]


def test_a_schema_induced_after_the_design_makes_the_cycle_stale():
    workspace = _workspace()
    workspace, _ = _commit(workspace, _validate(_design()))
    assert _cycle_readiness(workspace).state == "satisfied"

    document_schemas.save_schema(workspace, "bank_statement", STATEMENT["fields"])
    workspace = workspaces.load_workspace(workspace.id)

    readiness = _cycle_readiness(workspace)
    assert readiness.state == "stale"
    assert "schemas" in readiness.reasons[0]


def test_the_cycle_now_waits_for_the_schemas():
    from app.agent.workflows import audit

    assert "documents.schemas_stamped" in audit.dependencies("planning.cycle_ready")
    assert audit.dependencies("tests.cycle_ruleset_approved") == ("planning.cycle_ready",)
    assert "tests.cycle_ruleset_proposed" not in {item.id for item in REGISTRY.all()}
