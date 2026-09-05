"""The cycle graph projection: what the page is handed, and what it is not.

The frontend derives nothing. Every node, every edge and every field ordering
the strip draws is decided here, so these are the tests that say what the page
can show — including the two states it has to render, before any schema exists
and after a ruleset does.
"""

from __future__ import annotations

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import cycle_rulesets, document_schemas, planning_cycle_graph, workspaces
from app.main import create_app


_INVOICE_FIELDS = [
    {"name": "invoice_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "approval", "role": "control", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]
_ORDER_FIELDS = [
    {"name": "order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "total_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


def _workspace(name="Cycle graph"):
    ws = workspaces.create_workspace(name)
    ws.add_table(
        "po_data.csv",
        pl.DataFrame(
            {"PO_NUMBER": ["P1", "P2"], "GRN_ID": ["G1", "G2"]}
        ).write_csv().encode(),
    )
    ws.add_table(
        "invoice_data.csv",
        pl.DataFrame({"INVOICE_NO": ["I1"], "PO_NUMBER": ["P1"]}).write_csv().encode(),
    )
    ws.update_planning({"apm_markdown": "# Memo"}, agent=True)
    return workspaces.load_workspace(ws.id)


def _cycle(**overrides):
    cycle = {
        "name": "Procure-to-pay",
        "steps": [
            {
                "name": "Purchase order",
                "roles": [{"name": "order", "document_type": "purchase_order"}],
                "populations": [{"table": "po_data", "anchor": True}],
                "themes": ["Authorisation against limits"],
            },
            {
                "name": "Goods receipt",
                "roles": [],
                "populations": [{"table": "po_data", "columns": ["GRN_ID"]}],
                "themes": [],
            },
            {
                "name": "Invoice processing",
                "roles": [{"name": "invoice", "document_type": "vendor_invoice"}],
                "populations": [{"table": "invoice_data"}],
                "themes": [],
            },
        ],
        "cross_cutting": {"name": "Procurement operations", "themes": ["Fraud"]},
        "agent_run_id": "run-1",
    }
    cycle.update(overrides)
    return cycle


def _with_cycle(ws):
    ws.update_planning({"cycle": _cycle()}, agent=True)
    return workspaces.load_workspace(ws.id)


def _with_schemas(ws):
    document_schemas.save_schema(ws, "vendor_invoice", _INVOICE_FIELDS)
    document_schemas.save_schema(ws, "purchase_order", _ORDER_FIELDS)
    return workspaces.load_workspace(ws.id)


def _with_ruleset(ws):
    cycle_rulesets.save(ws, {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "anchor": {"table": "po_data", "column": "PO_NUMBER",
                   "role": "order", "field": "order_number"},
        "join_keys": [{
            "id": "jk_order", "match": "normalized_equal",
            "left": {"role": "invoice", "field": "order_number"},
            "right": {"role": "order", "field": "order_number"},
            "rationale": "An invoice cites the order it bills against.",
        }],
        "assertions": [
            {
                "id": "as_total", "label": "Totals agree",
                "left": {"role": "invoice", "field": "total_amount"},
                "right": {"role": "order", "field": "total_amount"},
                "requirement": "The amount billed is the amount ordered.",
                "rationale": "The amount billed must be the amount ordered.",
            },
            {
                "id": "as_approved", "label": "Approval is stated",
                "left": {"role": "invoice", "field": "approval"},
                "right": None,
                "requirement": "The invoice records an approval.",
                "rationale": "An unapproved invoice should not be paid.",
            },
        ],
    })
    return workspaces.load_workspace(ws.id)


# --------------------------------------------------------------------------- #
# Before the schemas
# --------------------------------------------------------------------------- #
def test_a_workspace_with_no_cycle_draws_an_empty_strip():
    graph = planning_cycle_graph.cycle_graph(_workspace())

    assert graph == {
        "name": "", "steps": [], "edges": [], "ruleset": None, "cross_cutting": None
    }


def test_the_shape_alone_draws_steps_populations_and_table_joins():
    """The strip is readable before any document has been extracted; that is the
    whole point of the shape being authored after the memorandum."""

    ws = _with_cycle(_workspace())
    graph = planning_cycle_graph.cycle_graph(ws)

    assert graph["name"] == "Procure-to-pay"
    assert [step["name"] for step in graph["steps"]] == [
        "Purchase order", "Goods receipt", "Invoice processing"
    ]
    order = graph["steps"][0]["documents"][0]
    assert order["document_type"] == "purchase_order"
    assert order["label"] == "Purchase order"
    # No schema has been induced, so a document node carries no fields yet.
    assert order["fields"] == []
    assert graph["steps"][0]["populations"] == [{
        "table": "po_data", "rows": 2,
        "columns": ["PO_NUMBER", "GRN_ID"], "borrowed": False, "anchor": True,
    }]
    # A step whose records live on a neighbour's table says which columns hold
    # them rather than claiming the whole table.
    assert graph["steps"][1]["populations"][0] == {
        "table": "po_data", "rows": 2,
        "columns": ["GRN_ID"], "borrowed": True, "anchor": False,
    }
    assert graph["steps"][1]["documents"] == []
    assert graph["cross_cutting"] == {
        "name": "Procurement operations", "themes": ["Fraud"]
    }
    assert graph["ruleset"] is None
    # No rules, so no field edges — and no anchor edge either, because the
    # anchor's role field is the ruleset's to name.
    assert graph["edges"] == []


def test_a_materialized_join_between_two_drawn_populations_is_an_edge():
    ws = _with_cycle(_workspace())
    ws.add_join({
        "name": "po_invoices", "left": "po_data", "right": "invoice_data",
        "how": "inner", "left_on": ["PO_NUMBER"], "right_on": ["PO_NUMBER"],
    })
    graph = planning_cycle_graph.cycle_graph(workspaces.load_workspace(ws.id))

    joins = [edge for edge in graph["edges"] if edge["kind"] == "table_join"]
    assert len(joins) == 1
    assert joins[0]["from"] == {
        "step": "Purchase order", "node": "po_data", "field": "PO_NUMBER"
    }
    assert joins[0]["to"] == {
        "step": "Invoice processing", "node": "invoice_data", "field": "PO_NUMBER"
    }
    assert joins[0]["label"] == "PO_NUMBER = PO_NUMBER"


# --------------------------------------------------------------------------- #
# After the rules
# --------------------------------------------------------------------------- #
def test_the_rules_become_edges_between_the_fields_they_rest_on():
    ws = _with_ruleset(_with_schemas(_with_cycle(_workspace())))
    graph = planning_cycle_graph.cycle_graph(ws)

    kinds = sorted(edge["kind"] for edge in graph["edges"])
    assert kinds == ["anchor", "assert", "join"]

    join = next(edge for edge in graph["edges"] if edge["kind"] == "join")
    assert join["from"] == {
        "step": "Invoice processing", "node": "invoice", "field": "order_number"
    }
    assert join["to"] == {
        "step": "Purchase order", "node": "order", "field": "order_number"
    }
    assert join["rule_id"] == "jk_order"

    assertion = next(edge for edge in graph["edges"] if edge["kind"] == "assert")
    assert assertion["rule_id"] == "as_total"
    assert assertion["label"] == "The amount billed is the amount ordered."

    anchor = next(edge for edge in graph["edges"] if edge["kind"] == "anchor")
    assert anchor["from"] == {
        "step": "Purchase order", "node": "po_data", "field": "PO_NUMBER"
    }
    assert anchor["to"] == {
        "step": "Purchase order", "node": "order", "field": "order_number"
    }


def test_a_one_operand_assertion_is_a_mark_on_the_field_not_an_edge():
    """It says the field must be stated at all. There is nowhere to draw a line
    to, and drawing one anyway would read as a comparison."""

    ws = _with_ruleset(_with_schemas(_with_cycle(_workspace())))
    graph = planning_cycle_graph.cycle_graph(ws)

    assert not [
        edge for edge in graph["edges"] if edge.get("rule_id") == "as_approved"
    ]
    invoice_step = next(
        step for step in graph["steps"] if step["name"] == "Invoice processing"
    )
    assert invoice_step["stated"] == ["approval"]


def test_only_the_fields_a_rule_touches_are_offered_to_the_page():
    """``vendor_invoice`` carries four fields; three take part in a rule, and
    the page shows the vocabulary of the rules rather than the schema."""

    ws = _with_ruleset(_with_schemas(_with_cycle(_workspace())))
    graph = planning_cycle_graph.cycle_graph(ws)

    ordering = planning_cycle_graph.relationship_fields(graph)
    assert ordering["invoice"] == ["order_number", "total_amount", "approval"]
    assert ordering["order"] == ["order_number", "total_amount"]
    # The full schema still travels, so the page can offer "show all fields".
    invoice = next(
        step for step in graph["steps"] if step["name"] == "Invoice processing"
    )["documents"][0]
    assert sorted(field["name"] for field in invoice["fields"]) == [
        "approval", "invoice_number", "order_number", "total_amount"
    ]


def test_a_role_the_rules_could_not_bind_is_still_a_node_marked_unbound():
    ws = _with_schemas(_with_cycle(_workspace()))
    cycle_rulesets.save(ws, {
        "cycle_label": "Procure to pay",
        "roles": [{"name": "invoice", "document_type": "vendor_invoice",
                   "cardinality": "one", "required": True}],
        "anchor": {"table": "invoice_data", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [],
        "assertions": [{
            "id": "as_stated", "label": "Approval is stated",
            "left": {"role": "invoice", "field": "approval"}, "right": None,
            "requirement": "The invoice records an approval.",
            "rationale": "An unapproved invoice should not be paid.",
        }],
    })
    graph = planning_cycle_graph.cycle_graph(workspaces.load_workspace(ws.id))

    order = graph["steps"][0]["documents"][0]
    assert order["node"] == "order"
    assert order["bound"] is False
    invoice = graph["steps"][2]["documents"][0]
    assert invoice["bound"] is True


def test_a_proposed_ruleset_is_drawn_and_labelled_as_proposed():
    """The page's job is to let an auditor read the rules before approving
    them, so a proposal is what it draws — said to be one."""

    ws = _with_ruleset(_with_schemas(_with_cycle(_workspace())))
    graph = planning_cycle_graph.cycle_graph(ws)

    assert graph["ruleset"]["status"] == "proposed"
    assert graph["ruleset"]["cycle_label"] == "Procure to pay"


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #
def test_the_graph_route_answers_and_reflects_a_later_edit():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "Cycle graph route"}).json()
    base = f"/api/workspaces/{created['id']}"
    ws = workspaces.load_workspace(created["id"])
    ws.add_table("po_data.csv", pl.DataFrame({"PO_NUMBER": ["P1"]}).write_csv().encode())

    empty = client.get(f"{base}/planning/cycle/graph")
    assert empty.status_code == 200
    assert empty.json()["steps"] == []

    client.patch(f"{base}/planning", json={"cycle": {
        "name": "Procure-to-pay",
        "steps": [{
            "name": "Purchase order",
            "roles": [{"name": "order", "document_type": "purchase_order"}],
            "populations": [{"table": "po_data", "anchor": True}],
            "themes": [],
        }],
        "cross_cutting": None,
    }})

    # The projection is cached on the workspace signature, so an edit through
    # the route it shares must be visible on the next read rather than served
    # from before it.
    drawn = client.get(f"{base}/planning/cycle/graph").json()
    assert drawn["name"] == "Procure-to-pay"
    assert drawn["steps"][0]["documents"][0]["document_type"] == "purchase_order"
    assert drawn["created_by"] == "user"
