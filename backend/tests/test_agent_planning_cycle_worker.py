"""Focused tests for the registered ``planning.cycle`` model worker (step 5b).

The worker owns the cycle prompt, the bundle-to-message transformation, the
response schema, and the gate. It is exercised with constructed bundles and a
gateway stub and must not touch a workspace, store, resolver, or scheduler —
which is why the structural half of its gate is
:func:`app.planning_cycle.validate_cycle_shape`, checked here against the lists
the turn was handed rather than against an engagement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import (
    WORKERS,
    WorkerRequest,
    WorkerResponseValidationError,
    WorkerRunError,
)
from app.agent.workers import planning


APM = (
    "# Audit Planning Memorandum\n\n"
    "## Process flow and understanding\n"
    "1. Purchase order\n2. Goods receipt\n3. Invoice processing\n\n"
    "## Key risks and planned response\n"
    "### Authorisation against limits\nOrders above the limit need approval.\n"
    "### Segregation of duties\nOne person must not order and receive.\n"
)

DOCUMENT_TYPES = [
    {"document_type": "purchase_order", "documents": 4},
    {"document_type": "goods_receipt", "documents": 3},
    {"document_type": "vendor_invoice", "documents": 5},
]

TABLES = [
    {"table": "po_data", "columns": [{"name": "PO_NUMBER"}, {"name": "GRN_ID"}]},
    {"table": "invoice_data", "columns": [{"name": "INVOICE_NO"}]},
]


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, system, user, activity=None, *, attempt=1, conversation=None):
        self.calls.append(
            {"system": system, "user": user, "activity": activity, "attempt": attempt}
        )
        return self.responses.pop(0)


def _bundle(*, apm=APM, tables=TABLES):
    values = [
        (
            "planning_context",
            "planning:context",
            ContextRepresentation("planning_context"),
            {"context": {"objective": "Assess procurement approvals"}},
        ),
        (
            "current_apm",
            "planning:apm",
            ContextRepresentation("current_artifact"),
            apm,
        ),
    ]
    values.extend(
        (
            "table_metadata",
            f"table:{table['table']}",
            ContextRepresentation("table_metadata"),
            table,
        )
        for table in tables
    )
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=representation,
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(
        capability_id="planning.cycle_ready",
        unit_id="cycle",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None, **unit_input):
    payload = {
        "input_sha1": "cycle-input",
        "risk_themes": ["Authorisation against limits", "Segregation of duties"],
        "document_types": DOCUMENT_TYPES,
        "joins": [],
        "existing_steps": [],
        "existing_processes": [],
    }
    payload.update(unit_input)
    return WorkerRequest(
        worker_id="planning.cycle",
        capability_id="planning.cycle_ready",
        unit_id="cycle",
        context=bundle or _bundle(),
        unit_input=payload,
        activity={"artifact_refs": ["planning:cycle"]},
    )


def _shape(**overrides):
    shape = {
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
                "roles": [{"name": "receipt", "document_type": "goods_receipt"}],
                "populations": [{"table": "po_data", "columns": ["GRN_ID"]}],
                "themes": ["Segregation of duties"],
            },
            {
                "name": "Invoice processing",
                "roles": [{"name": "invoice", "document_type": "vendor_invoice"}],
                "populations": [{"table": "invoice_data"}],
                "themes": [],
            },
        ],
        "cross_cutting": {"name": "Procurement operations", "themes": []},
    }
    shape.update(overrides)
    return shape


def _plain(value: object) -> object:
    """Frozen containers back to the plain JSON shapes assertions compare.

    ``ResponseSchema.validate`` freezes every array into a tuple before the
    semantic validator sees it, and the accepted proposal keeps that shape.
    """
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _run(responses, request=None):
    return WORKERS.execute(request or _request(), _Gateway(responses))


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
def test_a_scripted_shape_validates_and_is_returned_normalized():
    result = _run([json.dumps(_shape())])

    assert [step["name"] for step in result.proposal["steps"]] == [
        "Purchase order",
        "Goods receipt",
        "Invoice processing",
    ]
    assert _plain(result.proposal["steps"][0]["populations"]) == [
        {"table": "po_data", "anchor": True}
    ]
    assert result.proposal["cross_cutting"]["name"] == "Procurement operations"


def test_the_call_is_shown_the_memorandum_and_the_vocabularies_it_may_name():
    gateway = _Gateway([json.dumps(_shape())])
    WORKERS.execute(_request(), gateway)

    payload = json.loads(gateway.calls[0]["user"])
    assert "Process flow and understanding" in payload["REVISED APM"]
    assert payload["PLANNED RISK THEMES"] == [
        "Authorisation against limits",
        "Segregation of duties",
    ]
    assert payload["DOCUMENT TYPES HELD"] == DOCUMENT_TYPES
    assert payload["TABLES"] == [
        {"table": "po_data", "columns": ["PO_NUMBER", "GRN_ID"]},
        {"table": "invoice_data", "columns": ["INVOICE_NO"]},
    ]
    # Names and shapes. Nothing this call sees had to be extracted from a
    # document, which is what lets the stage sit before the evidence read.
    assert "table_profile" not in gateway.calls[0]["user"]
    assert gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == (
        "planning_cycle"
    )


def test_existing_names_are_handed_back_for_verbatim_reuse():
    gateway = _Gateway([json.dumps(_shape())])
    WORKERS.execute(
        _request(
            existing_steps=["Purchase order", "Goods receipt"],
            existing_processes=["Invoice processing"],
        ),
        gateway,
    )

    payload = json.loads(gateway.calls[0]["user"])
    assert payload["EXISTING STEP NAMES"] == ["Purchase order", "Goods receipt"]
    assert payload["EXISTING PROCESS NAMES"] == ["Invoice processing"]


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_a_document_type_the_engagement_lacks_is_refused_with_the_allowed_values():
    shape = _shape()
    shape["steps"][0]["roles"][0]["document_type"] = "bank_statement"

    with pytest.raises(WorkerRunError) as error:
        _run([json.dumps(shape), json.dumps(shape)])

    message = str(error.value)
    assert "which this engagement does not hold" in message
    assert "purchase_order" in message


def test_a_table_that_was_not_imported_is_refused_with_the_allowed_values():
    shape = _shape()
    shape["steps"][0]["populations"] = [{"table": "gl_entries"}]

    with pytest.raises(WorkerRunError) as error:
        _run([json.dumps(shape), json.dumps(shape)])

    message = str(error.value)
    assert "is not an imported table" in message
    assert "po_data" in message


def test_a_theme_the_response_left_out_lands_in_the_cross_cutting_bucket():
    """Locally, not by refusing: a theme nobody claimed is what the bucket is
    for, and re-deriving a whole shape to move one string is the largest
    available price for the smallest defect."""

    shape = _shape()
    shape["steps"][1]["themes"] = []

    result = _run([json.dumps(shape)])

    assert _plain(result.proposal["cross_cutting"]["themes"]) == [
        "Segregation of duties"
    ]
    assert _plain(result.proposal["steps"][1]["themes"]) == []


def test_a_theme_placed_twice_is_refused_rather_than_settled_locally():
    """A contradiction the model has to resolve: two steps each entitled to
    assume the other answered it."""

    shape = _shape()
    shape["steps"][2]["themes"] = ["Segregation of duties"]

    with pytest.raises(WorkerRunError) as error:
        _run([json.dumps(shape), json.dumps(shape)])

    assert "assign it to exactly one" in str(error.value)


def test_two_steps_may_not_share_a_role():
    shape = _shape()
    shape["steps"][1]["roles"][0]["name"] = "order"

    with pytest.raises(WorkerRunError) as error:
        _run([json.dumps(shape), json.dumps(shape)])

    assert "a role fills one position in the cycle" in str(error.value)


def test_only_one_population_may_be_the_anchor():
    shape = _shape()
    shape["steps"][2]["populations"] = [{"table": "invoice_data", "anchor": True}]

    with pytest.raises(WorkerRunError) as error:
        _run([json.dumps(shape), json.dumps(shape)])

    assert "at most one anchor population" in str(error.value)


def test_a_step_with_no_held_document_type_is_accepted_with_no_roles():
    """A step the memorandum names but nothing in the corpus records is a fact
    worth seeing, not an error."""

    shape = _shape()
    shape["steps"][1]["roles"] = []

    result = _run([json.dumps(shape)])

    assert _plain(result.proposal["steps"][1]["roles"]) == []
    assert result.proposal["steps"][1]["name"] == "Goods receipt"


def test_a_response_with_no_steps_is_refused_by_the_schema():
    with pytest.raises(WorkerRunError) as error:
        _run([json.dumps({"name": "Procure-to-pay"})] * 2)

    assert "carries no steps" in str(error.value)


# --------------------------------------------------------------------------- #
# Repair
# --------------------------------------------------------------------------- #
def test_one_repair_is_asked_for_and_carries_the_draft_and_its_faults():
    broken = _shape()
    broken["steps"][0]["populations"] = [{"table": "gl_entries"}]
    gateway = _Gateway([json.dumps(broken), json.dumps(_shape())])

    result = WORKERS.execute(_request(), gateway)

    assert len(gateway.calls) == 2
    assert gateway.calls[1]["attempt"] == 2
    assert "PREVIOUS CYCLE DRAFT:" in gateway.calls[1]["user"]
    assert "is not an imported table" in gateway.calls[1]["user"]
    assert [step["name"] for step in result.proposal["steps"]][0] == "Purchase order"


def test_a_second_failure_is_not_repaired_again():
    broken = _shape()
    broken["steps"][0]["populations"] = [{"table": "gl_entries"}]
    gateway = _Gateway([json.dumps(broken), json.dumps(broken)])

    with pytest.raises(WorkerRunError):
        WORKERS.execute(_request(), gateway)

    assert len(gateway.calls) == 2


# --------------------------------------------------------------------------- #
# The gate and the commit are the same validator
# --------------------------------------------------------------------------- #
def test_the_gate_reports_every_problem_at_once():
    """One repair attempt is available, so it should see all of them."""

    shape = _shape()
    shape["steps"][0]["populations"] = [{"table": "gl_entries"}]
    shape["steps"][1]["roles"][0]["document_type"] = "bank_statement"

    with pytest.raises(WorkerResponseValidationError) as error:
        planning.validate_cycle_proposal(shape, _request())

    message = str(error.value)
    assert "is not an imported table" in message
    assert "which this engagement does not hold" in message
