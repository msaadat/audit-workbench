"""Focused tests for the registered ``planning.rcm`` model worker (P7C.2).

The worker owns only the RCM prompt, bundle-to-message transformation, response
schema, and the engagement quality gate. It is exercised with constructed
bundles and a gateway stub and must not touch a workspace, store, resolver, or
scheduler.
"""

from __future__ import annotations

import ast
import inspect
import json

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
    WorkerRepairSeed,
    WorkerRequest,
    WorkerRunError,
)
from app.agent.workers import planning


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(
        self, system, user, activity=None, *, attempt=1, conversation=None
    ):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "activity": activity,
                "attempt": attempt,
                "conversation": conversation,
            }
        )
        return self.responses.pop(0)


def _bundle(*, current_rows=None):
    values = [
        (
            "rcm_template",
            "template:rcm",
            ContextRepresentation("artifact_template"),
            "# Risk and control matrix\n",
        ),
        (
            "current_apm",
            "planning:apm",
            ContextRepresentation("current_artifact"),
            "# APM\n\nAssess procurement approvals.",
        ),
        (
            "planning_context",
            "planning:context",
            ContextRepresentation("planning_context"),
            {"context": {"objective": "Assess procurement approvals"}},
        ),
    ]
    for row in current_rows or []:
        values.append(
            (
                "current_rcm",
                f"rcm:{row['id']}",
                ContextRepresentation("current_artifact"),
                row,
            )
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
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="planning.rcm",
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "rcm-input"},
        activity={"artifact_refs": ["planning:apm"]},
    )


def _row(**overrides):
    row = {
        "operation": "create",
        "process": "Accounts payable",
        "risk": "Duplicate payments are processed",
        "risk_rating": "high",
        "business_cycle": "",
        "control_attributes": [
            {
                "key": "duplicate_payment_prevention",
                "assertion": "Operational",
                "requirement": "Duplicate invoice validation operates before payment.",
                "evidence_kind": "manual_inspection",
            }
        ],
        "control": "Duplicate invoice validation",
        "control_type": "Automated preventive",
        "test_procedure": "Test invoice and amount duplicates.",
        "new_risk_reason": "No existing RCM row covers duplicate payments.",
    }
    row.update(overrides)
    return row


def _registry():
    return planning.cycle_vouching.DEFAULT_REGISTRY.reference(
        "procure_to_pay"
    ).to_dict()


def _cycle_attribute(**overrides):
    """A transaction-cycle attribute as the judgment pass returns it.

    The judgment pass states the evidence *strategy* only; registry, record
    kinds, and comparisons come from the second pass.
    """

    attribute = {
        "key": "three_way_match",
        "assertion": "Accuracy",
        "requirement": "Invoices agree to purchase and receipt records.",
        "evidence_kind": "transaction_cycle",
    }
    attribute.update(overrides)
    return attribute


def _contract(**overrides):
    """One evidence-pass contracts entry for the attribute above."""

    contract = {
        "row_index": 1,
        "attribute_key": "three_way_match",
        "registry": _registry(),
        "required_record_kinds": [
            "procure_to_pay.purchase_order",
            "procure_to_pay.goods_receipt",
            "procure_to_pay.vendor_invoice",
        ],
        "required_comparisons": _three_way_required_comparisons(),
    }
    contract.update(overrides)
    return contract


def _three_way_required_comparisons():
    return [
        {
            "key": "invoice_po_amount",
            "label": "Invoice amount agrees to purchase order",
            "left": {
                "record_kind": "procure_to_pay.vendor_invoice",
                "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            },
            "right": {
                "record_kind": "procure_to_pay.purchase_order",
                "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            },
            "operator": "numeric_within",
            "tolerance": {"absolute": 0.01, "percent": 0},
        },
        {
            "key": "po_grn_quantity",
            "label": "Purchase order quantity agrees to goods receipt",
            "left": {
                "record_kind": "procure_to_pay.purchase_order",
                "field": {"group": "quantities", "kind": "total", "attribute": "value"},
            },
            "right": {
                "record_kind": "procure_to_pay.goods_receipt",
                "field": {"group": "quantities", "kind": "total", "attribute": "value"},
            },
            "operator": "numeric_within",
            "tolerance": {"absolute": 0, "percent": 0},
        },
    ]


def test_rcm_worker_uses_only_bundle_and_returns_validated_rows():
    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway)

    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Duplicate payments are processed"
    ]
    assert gateway.calls[0]["system"] == planning.RCM_SYSTEM
    assert gateway.calls[0]["attempt"] == 1
    assert gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == "rcm"
    # The APM narrative and template reach the model.
    assert "Assess procurement approvals" in gateway.calls[0]["user"]
    assert "Risk and control matrix" in gateway.calls[0]["user"]


def test_rcm_worker_accepts_json_fenced_response():
    gateway = _Gateway(["```json\n" + json.dumps({"rows": [_row()]}) + "\n```"])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["operation"] == "create"


def test_rcm_worker_repairs_only_the_failing_row_and_preserves_the_rest():
    """A repair is scoped to the rows that failed.

    The whole document used to be regenerated for any single row error, which
    cost every correct row a second, differently-worded draft — and burned the
    one bounded correction turn on rows that had nothing wrong with them.
    """

    good = _row(process="Vendor onboarding", risk="Unapproved vendors are created")
    bad = _row(risk_rating="urgent")
    gateway = _Gateway(
        [
            json.dumps({"rows": [good, bad]}),
            json.dumps({"rows": [{**bad, "risk_rating": "high", "row_index": 2}]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert [call["attempt"] for call in gateway.calls] == [1, 2]
    repair_request = json.loads(gateway.calls[1]["user"])
    # Only the failing row travels, carrying its index and its own reasons.
    assert [entry["row_index"] for entry in repair_request["ROWS TO CORRECT"]] == [2]
    assert repair_request["ROWS TO CORRECT"][0]["errors"] == [
        "RCM row 2 has an unsupported risk rating; it must be exactly one of "
        "critical, high, low, medium"
    ]
    rows = result.proposal["rows"]
    assert [row["risk"] for row in rows] == [
        "Unapproved vendors are created",
        "Duplicate payments are processed",
    ]
    assert rows[1]["risk_rating"] == "high"


def test_rcm_worker_preserves_untouched_rows_byte_for_byte_through_a_repair():
    """The untouched row is the object that was parsed, not a re-serialization."""

    good = _row(process="Vendor onboarding", risk="Unapproved vendors are created")
    gateway = _Gateway(
        [
            json.dumps({"rows": [good, _row(risk_rating="urgent")]}),
            json.dumps(
                {
                    "rows": [
                        {
                            **_row(risk_rating="high"),
                            # A repair that tries to reword an accepted row cannot:
                            # only the indices that failed are spliced.
                            "row_index": 1,
                            "risk": "Rewritten by the repair turn",
                        },
                        {**_row(risk_rating="high"), "row_index": 2},
                    ]
                }
            ),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    accepted = planning._plain_json(result.proposal["rows"][0])
    expected = planning._normalized_rcm_row(good, 1, set())
    assert json.dumps(accepted, sort_keys=True) == json.dumps(
        expected, sort_keys=True
    )
    assert accepted["risk"] == "Unapproved vendors are created"


def test_rcm_worker_derives_business_cycle_and_drops_non_durable_keys():
    row = _row(
        business_cycle="wrong_model_value",
        control_attributes=[_cycle_attribute()],
    )
    gateway = _Gateway(
        [
            json.dumps({"rows": [row]}),
            json.dumps({"contracts": [_contract()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    normalized = result.proposal["rows"][0]
    assert normalized["business_cycle"] == "procure_to_pay"
    # Narrative keys the model likes to add are dropped rather than failing the
    # row; the workspace discards them anyway.
    assert "new_risk_reason" not in normalized
    assert "test_procedure" not in normalized
    attribute = normalized["control_attributes"][0]
    assert attribute["registry"] == _registry()
    assert [item["key"] for item in attribute["required_comparisons"]] == [
        "invoice_po_amount",
        "po_grn_quantity",
    ]


def test_rcm_worker_runs_the_evidence_pass_only_for_cycle_attributes():
    """The pack catalog is carried by the call that needs it, and no other.

    It was 11kB of the judgment prompt on every RCM turn, including the great
    majority of attributes that never reference a record kind.
    """

    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    WORKERS.execute(_request(), gateway)

    # One call only: no attribute asked for an evidence contract.
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["system"] == planning.RCM_SYSTEM
    assert "procure_to_pay.purchase_order" not in planning.RCM_SYSTEM
    assert "pack_id, pack_version, and definition_hash" in (
        planning.RCM_EVIDENCE_SYSTEM
    )
    assert json.dumps(_registry(), sort_keys=True, separators=(",", ":")) in (
        planning.RCM_EVIDENCE_SYSTEM
    )


def test_rcm_worker_completes_a_partial_evidence_contract_from_the_second_pass():
    """A judgment pass that half-writes a contract is completed, not rejected."""

    partial = _cycle_attribute(registry="procure_to_pay")
    gateway = _Gateway(
        [
            json.dumps({"rows": [_row(control_attributes=[partial])]}),
            json.dumps({"contracts": [_contract()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is False
    assert result.proposal["rows"][0]["control_attributes"][0]["registry"] == (
        _registry()
    )


def test_rcm_worker_reports_an_attribute_the_evidence_pass_declined():
    """An attribute the pack cannot express fails; it is never given a stand-in.

    Silently substituting a comparison the pack *can* express would answer a
    different question than the requirement asked, and the row would commit
    looking correct.
    """

    gateway = _Gateway(
        [
            json.dumps({"rows": [_row(control_attributes=[_cycle_attribute()])]}),
            json.dumps(
                {
                    "contracts": [
                        {
                            "row_index": 1,
                            "attribute_key": "three_way_match",
                            "unsupported": True,
                            "reason": "The pack states no delivery-note record.",
                        }
                    ]
                }
            ),
        ]
        * 2
    )

    with pytest.raises(WorkerRunError, match="names no evidence contract"):
        WORKERS.execute(_request(), gateway)


def test_rcm_worker_aggregates_quality_errors_across_rows():
    with pytest.raises(planning.WorkerResponseValidationError) as raised:
        planning.validate_rcm_proposal(
            {
                "rows": [
                    _row(risk_rating="urgent"),
                    _row(operation="replace"),
                ]
            },
            _request(),
        )

    assert raised.value.errors == (
        "RCM row 1 has an unsupported risk rating; it must be exactly one of "
        "critical, high, low, medium",
        "RCM row 2 has an unsupported operation; it must be exactly 'update' "
        "or 'create'",
    )


@pytest.mark.parametrize(
    ("malformed_registry", "expected_error"),
    [
        ("procure_to_pay", "registry must be an object"),
        (
            {
                "pack_id": "procure_to_pay",
                "required_record_kinds": [
                    "procure_to_pay.purchase_order",
                    "procure_to_pay.vendor_invoice",
                ],
            },
            "has unexpected key 'required_record_kinds'",
        ),
        (
            {"pack_id": "procure_to_pay", "pack_version": 6},
            "is stale or inconsistent",
        ),
    ],
)
def test_rcm_worker_repairs_transaction_cycle_registry_to_canonical_reference(
    malformed_registry, expected_error
):
    """A misplaced key is named as a misplaced key.

    ``required_record_kinds`` nested inside ``registry`` used to surface as a
    stale-reference or bad-version error, sending the next attempt to look at the
    pack version — the one thing that was not wrong.
    """

    gateway = _Gateway(
        [
            json.dumps({"rows": [_row(control_attributes=[_cycle_attribute()])]}),
            json.dumps(
                {"contracts": [_contract(registry=malformed_registry)]}
            ),
            json.dumps(
                {
                    "rows": [
                        {
                            **_row(control_attributes=[_cycle_attribute()]),
                            "row_index": 1,
                        }
                    ]
                }
            ),
            json.dumps({"contracts": [_contract()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert result.proposal["rows"][0]["control_attributes"][0]["registry"] == (
        _registry()
    )
    repair_request = json.loads(gateway.calls[2]["user"])
    assert any(
        expected_error in error
        for entry in repair_request["ROWS TO CORRECT"]
        for error in entry["errors"]
    )
    assert "required_record_kinds is a sibling of registry" in (
        planning.RCM_EVIDENCE_SYSTEM
    )


def test_rcm_worker_tolerates_stray_characters_after_a_complete_object():
    """Run 20260809-140906-19a740: a valid 24-row draft lost to a trailing `]}`.

    ``json.loads`` requires the entire string to be one value, so a complete
    object followed by two brackets the model had already closed was discarded
    whole — and, because the rows never parsed, the evidence pass never ran.
    """

    gateway = _Gateway([json.dumps({"rows": [_row()]}) + "]}"])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is False
    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Duplicate payments are processed"
    ]


def test_rcm_worker_tolerates_prose_before_the_object():
    gateway = _Gateway(
        ["Here is the revised matrix:\n" + json.dumps({"rows": [_row()]})]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["operation"] == "create"


def test_rcm_worker_still_rejects_a_truncated_object():
    """Surplus is tolerated; shortfall is not.

    A truncated draft must never be read as a complete one — that would commit a
    matrix that silently stops halfway.
    """

    truncated = json.dumps({"rows": [_row(), _row()]})[:-40]
    gateway = _Gateway([truncated, truncated])

    with pytest.raises(WorkerRunError, match="not a valid JSON object"):
        WORKERS.execute(_request(), gateway)


def test_a_whole_document_re_ask_still_runs_the_evidence_pass():
    """Run 20260809-140906-19a740, second half.

    When the prior draft never parsed there are no rows to scope a repair to, so
    the whole document is re-asked. That response is a judgment-pass response like
    any other and must go through the evidence pass; returning it directly left
    all 13 transaction-cycle attributes without the contract the judgment prompt
    had just told the model not to write.
    """

    gateway = _Gateway(
        [
            "{ truncated",
            json.dumps({"rows": [_row(control_attributes=[_cycle_attribute()])]}),
            json.dumps({"contracts": [_contract()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    # judgment, whole-document re-ask, then the evidence pass on its output.
    assert len(gateway.calls) == 3
    assert gateway.calls[2]["system"] == planning.RCM_EVIDENCE_SYSTEM
    attribute = result.proposal["rows"][0]["control_attributes"][0]
    assert attribute["registry"] == _registry()
    assert [item["key"] for item in attribute["required_comparisons"]] == [
        "invoice_po_amount",
        "po_grn_quantity",
    ]


def test_rcm_worker_hands_unparseable_output_to_the_registry_to_repair():
    """The worker returns text; the registry owns rejection and repair.

    The two-pass implementation parses the model's output in order to run the
    second pass over it. Raising from there would escape the bounded repair loop
    altogether — `WorkerRegistry.execute` only guards the schema and semantic
    validators — and surface as an unhandled error instead of a repaired draft.
    """

    gateway = _Gateway(["not json at all", json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Duplicate payments are processed"
    ]


def test_rcm_worker_survives_an_unparseable_evidence_pass():
    """A malformed second pass leaves the attributes without a contract.

    Which the gate then reports, and the repair turn can act on — rather than the
    worker raising past the loop that exists to fix it.
    """

    gateway = _Gateway(
        [
            json.dumps({"rows": [_row(control_attributes=[_cycle_attribute()])]}),
            "```\nnot a contracts object\n```",
            json.dumps(
                {
                    "rows": [
                        {
                            **_row(
                                control_attributes=[
                                    {
                                        **_cycle_attribute(),
                                        "registry": _registry(),
                                        "required_record_kinds": [
                                            "procure_to_pay.vendor_invoice",
                                            "procure_to_pay.purchase_order",
                                        ],
                                        "required_comparisons": [
                                            _three_way_required_comparisons()[0]
                                        ],
                                    }
                                ]
                            ),
                            "row_index": 1,
                        }
                    ]
                }
            ),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    repair_request = json.loads(gateway.calls[2]["user"])
    assert any(
        "names no evidence contract" in error
        for entry in repair_request["ROWS TO CORRECT"]
        for error in entry["errors"]
    )


def test_rcm_worker_re_asks_the_document_when_the_prior_draft_never_parsed():
    """A linked retry can start from a draft the schema rejected.

    There are no rows to scope a repair to, so the whole document is re-asked
    rather than the worker failing on a partition it cannot compute.
    """

    seed = WorkerRepairSeed(
        previous_response="{ truncated",
        validation_errors=("the response is not a valid JSON object",),
    )
    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway, repair_seed=seed)

    assert result.proposal["rows"][0]["risk"] == "Duplicate payments are processed"
    assert "could not be parsed" in gateway.calls[0]["user"]


def test_rcm_worker_quarantines_an_unrepairable_row_and_keeps_the_rest():
    """One row that will not validate no longer costs the whole matrix.

    This is the shape of the live failure: twelve good rows discarded because one
    comparison named an operator that does not exist.
    """

    good = _row(process="Vendor onboarding", risk="Unapproved vendors are created")
    stubborn = _row(risk_rating="urgent")
    gateway = _Gateway(
        [
            json.dumps({"rows": [good, stubborn]}),
            # The repair turn does not fix it either.
            json.dumps({"rows": [{**stubborn, "row_index": 2}]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Unapproved vendors are created"
    ]
    quarantined = result.proposal["quarantined"]
    assert [item["risk"] for item in quarantined] == [
        "Duplicate payments are processed"
    ]
    assert "unsupported risk rating" in quarantined[0]["errors"][0]


def test_rcm_worker_still_fails_when_no_row_survives():
    """Quarantine salvages a partial matrix; it never manufactures an empty one."""

    stubborn = _row(risk_rating="urgent")
    gateway = _Gateway(
        [
            json.dumps({"rows": [stubborn]}),
            json.dumps({"rows": [{**stubborn, "row_index": 1}]}),
        ]
    )

    with pytest.raises(WorkerRunError, match="unsupported risk rating"):
        WORKERS.execute(_request(), gateway)


def test_rcm_worker_repairs_an_evidence_contract_without_retouching_judgment():
    """An invalid comparison is corrected; the risk and control are not reopened."""

    attribute = _cycle_attribute()
    row = _row(control_attributes=[attribute])
    broken = _contract(
        required_comparisons=[
            {
                **_three_way_required_comparisons()[0],
                "operator": "equals",
            }
        ]
    )
    gateway = _Gateway(
        [
            json.dumps({"rows": [row]}),
            json.dumps({"contracts": [broken]}),
            json.dumps(
                {
                    "rows": [
                        {
                            **_row(
                                control_attributes=[
                                    {
                                        **attribute,
                                        "registry": _registry(),
                                        "required_record_kinds": [
                                            "procure_to_pay.vendor_invoice",
                                            "procure_to_pay.purchase_order",
                                        ],
                                        "required_comparisons": [
                                            _three_way_required_comparisons()[0]
                                        ],
                                    }
                                ]
                            ),
                            "row_index": 1,
                        }
                    ]
                }
            ),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    repair_request = json.loads(gateway.calls[2]["user"])
    errors = repair_request["ROWS TO CORRECT"][0]["errors"]
    assert any("not a supported operator" in error for error in errors)
    # The repair turn is given the vocabulary, not just the violation.
    assert "equal_exact" in gateway.calls[2]["system"]
    assert result.proposal["rows"][0]["risk"] == "Duplicate payments are processed"


def test_rcm_worker_rejects_update_to_unknown_existing_row():
    current = _row(id="RCM-EXISTING", operation="update")
    gateway = _Gateway(
        [
            json.dumps({"rows": [_row(operation="update", rcm_id="RCM-MISSING")]}),
            json.dumps({"rows": [_row(operation="update", rcm_id="RCM-MISSING")]}),
        ]
    )

    with pytest.raises(WorkerRunError, match="does not identify an existing RCM row"):
        WORKERS.execute(_request(_bundle(current_rows=[current])), gateway)


def test_rcm_worker_accepts_update_matching_a_supplied_current_row():
    current = _row(id="RCM-1")
    gateway = _Gateway(
        [json.dumps({"rows": [_row(operation="update", rcm_id="rcm:RCM-1")]})]
    )

    result = WORKERS.execute(_request(_bundle(current_rows=[current])), gateway)

    assert result.proposal["rows"][0]["rcm_id"] == "rcm:RCM-1"


def test_rcm_worker_has_no_workspace_store_resolver_or_scheduler_dependency():
    source = inspect.getsource(planning)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name.endswith(
            (
                "workspaces",
                "workspace_transactions",
                "store",
                "resolver",
                "workflow_runner",
                "action_runner",
            )
        )
        for name in imported
    )
    assert ".ws" not in source
    assert "load_workspace" not in source
