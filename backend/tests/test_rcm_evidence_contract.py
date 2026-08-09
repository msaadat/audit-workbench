"""The RCM evidence contract: the DSL, its gate, and the real failure it lost to.

Every rule here exists because one live procurement RCM generation failed twice
and took thirteen rows with it. The verbatim response is
``fixtures/rcm_operator_rejection.json``; the root cause was that the operator
vocabulary appeared nowhere in the turn that had to author it, and the rejection
that came back named the offending value without ever naming a legal one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import cycle_vouching
from app.agent import prompts
from app.agent.workers import planning, tests as tests_worker
from app.cycle_registry import DEFAULT_REGISTRY, operators, recipes

FIXTURE = Path(__file__).parent / "fixtures" / "rcm_operator_rejection.json"


@pytest.fixture
def rejected_rows() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]


def _registry() -> dict:
    return DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()


def _attribute(**overrides) -> dict:
    attribute = {
        "key": "invoice_match",
        "assertion": "Accuracy",
        "requirement": "The invoice agrees to the purchase order.",
        "evidence_kind": "transaction_cycle",
        "registry": _registry(),
        "required_record_kinds": [
            "procure_to_pay.vendor_invoice",
            "procure_to_pay.purchase_order",
        ],
        "required_comparisons": [
            {
                "key": "invoice_to_order",
                "label": "Invoice total agrees to the order",
                "operator": "numeric_within",
                "left": {
                    "record_kind": "procure_to_pay.vendor_invoice",
                    "field": {"group": "amounts", "kind": "total", "attribute": "value"},
                },
                "right": {
                    "record_kind": "procure_to_pay.purchase_order",
                    "field": {"group": "amounts", "kind": "total", "attribute": "value"},
                },
                "tolerance": {"absolute": 0.01, "percent": 0},
            }
        ],
    }
    attribute.update(overrides)
    return attribute


def _errors(attribute: dict) -> tuple[str, ...]:
    with pytest.raises(cycle_vouching.CycleSchemaError) as raised:
        cycle_vouching.validate_control_attributes([attribute])
    return raised.value.errors


# --- the vocabulary is stated, once, where both prompts can read it ----------


def test_the_gate_and_the_operator_table_cannot_drift_apart():
    assert cycle_vouching.OPERATORS is operators.OPERATORS
    assert cycle_vouching.OPERATORS == {
        definition.id for definition in operators.OPERATOR_DEFINITIONS
    }


def test_the_authoring_prompt_states_the_whole_operator_vocabulary():
    """Whichever prompt authors comparisons must state every operator.

    That is now exactly one prompt. ``tests.generate`` used to restate the
    vocabulary because it authored assertions too, and the two prompts had to be
    kept in parity. It no longer authors them: a cycle procedure's comparisons
    are compiled from the ``required_comparisons`` this prompt produces, so the
    operator table belongs here alone.
    """

    for operator_id in sorted(cycle_vouching.OPERATORS):
        assert operator_id in planning.RCM_EVIDENCE_SYSTEM, operator_id


def test_the_generation_prompt_no_longer_authors_comparisons():
    """The operator vocabulary is absent from generation because it is unused."""

    assert "operand1" not in tests_worker.GENERATE_SYSTEM
    assert "entry_quantifier" not in tests_worker.GENERATE_SYSTEM
    assert "It has no assertions" in tests_worker.GENERATE_SYSTEM


def test_the_rcm_prompt_names_the_operators_it_rejects():
    table = prompts.operator_table()
    assert "equals" in table and "greater_than_or_equal" in table
    assert table in planning.RCM_EVIDENCE_SYSTEM
    # And it tells the author which direction a date comparison runs, which is
    # the correction a rename alone does not make.
    assert "EARLIER event on the left" in planning.RCM_EVIDENCE_SYSTEM


# --- replaying the live failure ---------------------------------------------


def test_the_live_rejection_now_reports_every_invalid_operator_at_once(
    rejected_rows,
):
    """Six invalid operators produced two error messages.

    Each row stopped at its first bad comparison, so a bounded repair turn was
    told about two problems out of six. Correcting both would still have failed.
    """

    errors = _all_errors(rejected_rows)
    operator_errors = [item for item in errors if "not a supported operator" in item]
    assert len(operator_errors) == 6
    assert sum("'equals'" in item for item in operator_errors) == 4
    assert sum("'greater_than_or_equal'" in item for item in operator_errors) == 2


def test_the_live_rejection_also_reveals_the_invented_key_the_prompt_caused(
    rejected_rows,
):
    """Every failing comparison also carried an ``operator_tolerance`` key.

    The old prompt asked for "key, label, operator, left, optional right, and
    operator tolerance", which reads as a field name — so the model wrote one.
    The key was silently ignored, meaning the tolerance was silently dropped, and
    nothing said so. Both defects on one comparison are now reported together,
    because fixing the operator alone would leave the tolerance still missing.
    """

    errors = _all_errors(rejected_rows)
    misplaced = [item for item in errors if "'operator_tolerance'" in item]
    assert len(misplaced) == 6
    assert all("It accepts exactly: key, label, left, operator" in item for item in misplaced)
    # The replacement prompt cannot be read that way.
    assert "operator_tolerance" not in planning.RCM_EVIDENCE_SYSTEM
    unwrapped = " ".join(planning.RCM_EVIDENCE_SYSTEM.split())
    assert "the tolerance its operator requires — and no other keys" in unwrapped


def _all_errors(rows: list[dict]) -> list[str]:
    return [
        message
        for index, row in enumerate(rows, start=1)
        for message in _row_errors(row, index)
    ]


def _row_errors(row: dict, index: int) -> list[str]:
    try:
        planning._normalized_rcm_row(row, index, set())
    except planning.WorkerResponseValidationError as error:
        return list(error.errors)
    return []


def test_the_live_rejection_message_now_carries_the_legal_vocabulary(
    rejected_rows,
):
    """The old message named the wrong value and stopped.

    The repair turn was told ``eq`` was unsupported and answered ``equals``. The
    message now states the closed set, so the correction is available.
    """

    errors = _all_errors(rejected_rows)
    operator_error = next(
        item
        for item in errors
        if "'equals'" in item and "not a supported operator" in item
    )
    for operator_id in sorted(cycle_vouching.OPERATORS):
        assert operator_id in operator_error
    assert "For 'equals' use 'equal_exact'" in operator_error


def test_the_live_rejection_keeps_the_rows_that_were_never_wrong(rejected_rows):
    """Eleven of thirteen rows were fine and all thirteen were discarded."""

    valid = [
        row
        for index, row in enumerate(rejected_rows, start=1)
        if not _row_errors(row, index)
    ]
    assert len(valid) == 11
    assert len(rejected_rows) == 13


# --- a rename is a suggestion, never a rewrite ------------------------------


def test_the_live_document_validates_once_its_contracts_come_from_recipes():
    """The end the live run never reached.

    The same thirteen rows, with each transaction-cycle attribute's contract
    authored as a recipe reference rather than hand-rolled DSL. Nothing about the
    audit judgment changes; the four attributes that needed comparisons get them
    from the catalog, and the document validates on the first attempt.
    """

    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    judged = [_strategy_only(row) for row in rows]
    contracted = [_with_recipe_contracts(row) for row in judged]

    normalized, failures = planning._partition_rcm_rows(contracted, _blank_request())

    assert failures == []
    assert len(normalized) == 13
    cycle_rows = [
        row
        for row in normalized
        if any(
            attribute["evidence_kind"] == "transaction_cycle"
            for attribute in row["control_attributes"]
        )
    ]
    assert len(cycle_rows) == 2
    assert all(row["business_cycle"] == "procure_to_pay" for row in cycle_rows)


def _strategy_only(row: dict) -> dict:
    """Strip a row back to what the judgment pass now returns."""

    return {
        **row,
        "control_attributes": [
            {
                key: value
                for key, value in attribute.items()
                if key in {"key", "assertion", "requirement", "evidence_kind"}
            }
            for attribute in row["control_attributes"]
        ],
    }


def _with_recipe_contracts(row: dict) -> dict:
    """Attach the contract the evidence pass would author, via recipes."""

    return {
        **row,
        "control_attributes": [
            attribute
            if attribute["evidence_kind"] != "transaction_cycle"
            else {
                **attribute,
                "registry": _registry(),
                "required_record_kinds": [
                    "procure_to_pay.vendor_invoice",
                    "procure_to_pay.purchase_order",
                ],
                "comparison_recipes": [
                    {
                        "recipe_id": "common.total_amount_agreement",
                        "bindings": {
                            "source": "procure_to_pay.vendor_invoice",
                            "target": "procure_to_pay.purchase_order",
                        },
                    }
                ],
            }
            for attribute in row["control_attributes"]
        ],
    }


def _blank_request():
    """A worker request whose bundle supplies no current RCM rows."""

    from test_agent_planning_rcm_worker import _request

    return _request()


def test_a_near_miss_operator_is_rejected_rather_than_guessed():
    """``equals`` on an amount is not ``equal_exact``.

    Choosing between exact agreement and a tolerance is an audit-design decision.
    The message says what was probably meant and what the rename alone does not
    fix; the payload is still rejected.
    """

    message = operators.unsupported_operator_message("equals", label="c[0]")
    assert "use 'equal_exact'" in message
    assert "numeric_within with a tolerance" in message
    assert operators.operator("equals") is None
    assert operators.operator("Equal_Exact") is None
    # Whitespace is transport, not a choice.
    assert operators.operator("  equal_exact ").id == "equal_exact"


def test_a_reversed_date_operator_is_told_to_swap_its_operands():
    """The live failure wrote ``greater_than_or_equal`` for payment-after-receipt.

    Mapping that to ``date_on_or_before`` without swapping the operands would
    silently assert the opposite of the requirement.
    """

    message = operators.unsupported_operator_message(
        "greater_than_or_equal", label="c[0]"
    )
    assert "use 'date_on_or_before'" in message
    assert "operands swapped so the earlier event is on the left" in message


def test_a_backwards_date_comparison_is_rejected_against_the_pack_chronology():
    errors = _errors(
        _attribute(
            required_record_kinds=[
                "procure_to_pay.goods_receipt",
                "procure_to_pay.payment_voucher",
            ],
            required_comparisons=[
                {
                    "key": "payment_before_receipt",
                    "label": "Stated backwards on purpose",
                    "operator": "date_on_or_before",
                    "left": {
                        "record_kind": "procure_to_pay.payment_voucher",
                        "field": {
                            "group": "dates",
                            "kind": "payment_date",
                            "attribute": "value",
                        },
                    },
                    "right": {
                        "record_kind": "procure_to_pay.goods_receipt",
                        "field": {
                            "group": "dates",
                            "kind": "receipt_date",
                            "attribute": "value",
                        },
                    },
                }
            ],
        )
    )

    assert any("registered cycle order puts it later" in item for item in errors)


# --- every independent violation, with a path -------------------------------


def test_every_malformed_comparison_in_one_attribute_is_reported():
    comparisons = [
        {
            "key": f"bad_{index}",
            "label": "Invented operator",
            "operator": name,
            "left": {
                "record_kind": "procure_to_pay.vendor_invoice",
                "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            },
            "right": {
                "record_kind": "procure_to_pay.purchase_order",
                "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            },
        }
        for index, name in enumerate(("equals", "eq", "gte", "matches"))
    ]

    errors = _errors(_attribute(required_comparisons=comparisons))

    assert len(errors) == 4
    for index in range(4):
        assert any(
            f"required_comparisons[{index}]" in item for item in errors
        ), index


def test_every_malformed_attribute_of_one_control_is_reported():
    errors = _errors_for(
        [
            _attribute(key="first", assertion="Correctness"),
            _attribute(key="second", evidence_kind="table_scan"),
        ]
    )

    assert len(errors) == 2
    assert "control_attributes[0].assertion 'Correctness'" in errors[0]
    assert "control_attributes[1].evidence_kind 'table_scan'" in errors[1]


def _errors_for(attributes: list[dict]) -> tuple[str, ...]:
    with pytest.raises(cycle_vouching.CycleSchemaError) as raised:
        cycle_vouching.validate_control_attributes(attributes)
    return raised.value.errors


def test_an_error_path_reaches_the_exact_comparison_and_side():
    errors = _errors(
        _attribute(
            required_comparisons=[
                {
                    "key": "bad_field",
                    "label": "Unknown field selector",
                    "operator": "equal_exact",
                    "left": {
                        "record_kind": "procure_to_pay.vendor_invoice",
                        "field": {
                            "group": "amounts",
                            "kind": "invented",
                            "attribute": "value",
                        },
                    },
                    "right": {
                        "record_kind": "procure_to_pay.purchase_order",
                        "field": {
                            "group": "amounts",
                            "kind": "total",
                            "attribute": "value",
                        },
                    },
                }
            ]
        )
    )

    assert any(
        "required_comparisons[0].left.field" in item for item in errors
    ), errors


# --- closed key sets where placement carries meaning ------------------------


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"confidence": "high"}, "control_attributes[0] has unexpected key"),
        (
            {"registry": {**{"note": "copied"}, **_registry()}},
            "control_attributes[0].registry has unexpected key 'note'",
        ),
    ],
)
def test_an_unknown_key_in_the_evidence_contract_is_rejected(mutation, expected):
    errors = _errors(_attribute(**mutation))

    assert any(expected in item for item in errors), errors


def test_a_misplaced_record_kinds_key_is_named_as_a_misplacement():
    """The live prompt warned about this shape, and the gate mis-reported it.

    Nested inside ``registry`` it produced a stale-reference complaint, which
    pointed the author at the pack version rather than the misplaced key.
    """

    errors = _errors(
        _attribute(
            registry={
                "pack_id": "procure_to_pay",
                "required_record_kinds": ["procure_to_pay.vendor_invoice"],
            }
        )
    )

    assert any("unexpected key 'required_record_kinds'" in item for item in errors)
    assert any("siblings of registry" in item for item in errors)


def test_an_unknown_comparison_key_is_rejected_with_the_accepted_set():
    errors = _errors(
        _attribute(
            required_comparisons=[
                {
                    "key": "extra",
                    "label": "Carries an invented field",
                    "operator": "equal_exact",
                    "operand1": {"record_kind": "procure_to_pay.vendor_invoice"},
                    "left": {
                        "record_kind": "procure_to_pay.vendor_invoice",
                        "field": {
                            "group": "amounts",
                            "kind": "total",
                            "attribute": "value",
                        },
                    },
                    "right": {
                        "record_kind": "procure_to_pay.purchase_order",
                        "field": {
                            "group": "amounts",
                            "kind": "total",
                            "attribute": "value",
                        },
                    },
                }
            ]
        )
    )

    assert any("unexpected key 'operand1'" in item for item in errors)
    assert any("key, label, left, operator, right, tolerance" in item for item in errors)


# --- a declared record kind nothing reads -----------------------------------


def test_a_required_record_kind_no_comparison_reads_is_rejected():
    errors = _errors(
        _attribute(
            required_record_kinds=[
                "procure_to_pay.vendor_invoice",
                "procure_to_pay.purchase_order",
                "procure_to_pay.goods_receipt",
            ]
        )
    )

    assert any(
        "required record kind 'procure_to_pay.goods_receipt' is never read"
        in item
        for item in errors
    )


# --- recipes --------------------------------------------------------------


def test_a_recipe_expands_into_canonical_comparisons():
    attributes = cycle_vouching.validate_control_attributes(
        [
            _attribute(
                required_comparisons=None,
                comparison_recipes=[
                    {
                        "recipe_id": "common.total_amount_agreement",
                        "bindings": {
                            "source": "procure_to_pay.vendor_invoice",
                            "target": "procure_to_pay.purchase_order",
                        },
                    }
                ],
            )
        ]
    )

    comparisons = attributes[0]["required_comparisons"]
    assert [item["key"] for item in comparisons] == ["total_amount_agreement"]
    assert comparisons[0]["operator"] == "numeric_within"
    assert comparisons[0]["tolerance"] == {"absolute": 0.01, "percent": 0}
    # The label is rendered from the bound record kinds, not left as a template.
    assert "Vendor invoice" in comparisons[0]["label"]
    assert "{source}" not in comparisons[0]["label"]


def test_a_recipe_and_a_hand_written_comparison_compose():
    attributes = cycle_vouching.validate_control_attributes(
        [
            _attribute(
                comparison_recipes=[
                    {
                        "recipe_id": "common.party_agreement",
                        "bindings": {
                            "source": "procure_to_pay.vendor_invoice",
                            "target": "procure_to_pay.purchase_order",
                        },
                    }
                ]
            )
        ]
    )

    assert [item["key"] for item in attributes[0]["required_comparisons"]] == [
        "party_agreement",
        "invoice_to_order",
    ]


def test_validating_an_expanded_attribute_again_is_a_no_op():
    """Run 20260809-133225-658b03: 16 good rows, lost at the commit step.

    A row is validated more than once in its life — the worker normalizes the
    proposal, the executor re-validates before committing, and the workspace
    re-validates on load. Expanding into ``required_comparisons`` while leaving
    ``comparison_recipes`` in place made the second pass expand the same recipes
    again and collide with its own first expansion:

        control_attributes[0].required_comparisons[2]: duplicate required
        comparison key 'total_amount_agreement'.

    Nothing was wrong with the response. Validation has to be idempotent.
    """

    authored = _attribute(
        required_comparisons=None,
        comparison_recipes=[
            {
                "recipe_id": "common.total_amount_agreement",
                "bindings": {
                    "source": "procure_to_pay.vendor_invoice",
                    "target": "procure_to_pay.purchase_order",
                },
            },
            {
                "recipe_id": "common.quantity_agreement",
                "bindings": {
                    "source": "procure_to_pay.vendor_invoice",
                    "target": "procure_to_pay.purchase_order",
                },
            },
        ],
    )

    once = cycle_vouching.validate_control_attributes([authored])
    twice = cycle_vouching.validate_control_attributes(once)
    thrice = cycle_vouching.validate_control_attributes(twice)

    assert [item["key"] for item in once[0]["required_comparisons"]] == [
        "total_amount_agreement",
        "quantity_agreement",
    ]
    assert once == twice == thrice
    # The recipe list is retired to its applied form, which is never expanded.
    assert "comparison_recipes" not in once[0]
    assert [
        item["recipe_id"] for item in once[0][cycle_vouching.APPLIED_RECIPES_KEY]
    ] == ["common.total_amount_agreement", "common.quantity_agreement"]


def test_re_validating_a_mixed_recipe_and_hand_written_contract_is_stable():
    """The exact shape of the failing row: two recipes plus one hand-written."""

    once = cycle_vouching.validate_control_attributes([_attribute(
        comparison_recipes=[
            {
                "recipe_id": "common.total_amount_agreement",
                "bindings": {
                    "source": "procure_to_pay.vendor_invoice",
                    "target": "procure_to_pay.purchase_order",
                },
            },
            {
                "recipe_id": "common.quantity_agreement",
                "bindings": {
                    "source": "procure_to_pay.vendor_invoice",
                    "target": "procure_to_pay.purchase_order",
                },
            },
        ]
    )])

    assert [item["key"] for item in once[0]["required_comparisons"]] == [
        "total_amount_agreement",
        "quantity_agreement",
        "invoice_to_order",
    ]
    assert cycle_vouching.validate_control_attributes(once) == once


def test_one_recipe_applied_twice_qualifies_its_keys_by_binding():
    """An invoice agreeing to its order *and* its receipt is two uses of one shape."""

    validated = cycle_vouching.validate_control_attributes(
        [
            _attribute(
                required_record_kinds=[
                    "procure_to_pay.vendor_invoice",
                    "procure_to_pay.purchase_order",
                    "procure_to_pay.goods_receipt",
                ],
                required_comparisons=None,
                comparison_recipes=[
                    {
                        "recipe_id": "common.total_amount_agreement",
                        "bindings": {
                            "source": "procure_to_pay.vendor_invoice",
                            "target": "procure_to_pay.purchase_order",
                        },
                    },
                    {
                        "recipe_id": "common.total_amount_agreement",
                        "bindings": {
                            "source": "procure_to_pay.vendor_invoice",
                            "target": "procure_to_pay.goods_receipt",
                        },
                    },
                ],
            )
        ]
    )

    assert [item["key"] for item in validated[0]["required_comparisons"]] == [
        "total_amount_agreement_vendor_invoice_purchase_order",
        "total_amount_agreement_vendor_invoice_goods_receipt",
    ]
    assert cycle_vouching.validate_control_attributes(validated) == validated


def test_catalogued_comparison_keys_are_unique_across_recipes():
    """Two recipes on one attribute expand into one list, so keys cannot clash."""

    keys = [
        comparison.key
        for definition in recipes.COMPARISON_RECIPES
        for comparison in definition.comparisons
    ]
    assert len(keys) == len(set(keys))


def test_a_recipe_expansion_is_validated_like_any_comparison():
    """A recipe is a shortcut through authoring, never through the gate.

    Bound to record kinds whose pack does not offer the field, the expansion
    fails exactly as a hand-written comparison naming that field would.
    """

    errors = _errors(
        _attribute(
            registry=DEFAULT_REGISTRY.reference("payroll").to_dict(),
            required_record_kinds=[
                "payroll.bank_payment",
                "payroll.employment_contract",
            ],
            required_comparisons=None,
            comparison_recipes=[
                {
                    "recipe_id": "payroll.net_pay_agreement",
                    "bindings": {
                        "source": "payroll.bank_payment",
                        "target": "payroll.employment_contract",
                    },
                }
            ],
        )
    )

    assert any("is unavailable on role" in item for item in errors)


def test_an_unknown_recipe_is_rejected_with_the_available_ones():
    errors = _errors(
        _attribute(
            required_comparisons=None,
            comparison_recipes=[
                {"recipe_id": "common.invented", "bindings": {"a": "b"}}
            ],
        )
    )

    assert any("is not a comparison recipe offered for pack" in item for item in errors)
    assert any("common.total_amount_agreement" in item for item in errors)


def test_a_recipe_offered_for_another_pack_is_not_available():
    errors = _errors(
        _attribute(
            required_comparisons=None,
            comparison_recipes=[
                {
                    "recipe_id": "payroll.net_pay_agreement",
                    "bindings": {
                        "source": "procure_to_pay.vendor_invoice",
                        "target": "procure_to_pay.purchase_order",
                    },
                }
            ],
        )
    )

    assert any("is not a comparison recipe offered for pack" in item for item in errors)


def test_recipe_bindings_must_cover_exactly_the_declared_roles():
    errors = _errors(
        _attribute(
            required_comparisons=None,
            comparison_recipes=[
                {
                    "recipe_id": "procure_to_pay.three_way_match",
                    "bindings": {
                        "purchase_order": "procure_to_pay.purchase_order",
                        "vendor_invoice": "procure_to_pay.vendor_invoice",
                    },
                }
            ],
        )
    )

    assert any("must bind exactly" in item for item in errors)


def test_a_recipe_binding_must_name_a_required_record_kind():
    errors = _errors(
        _attribute(
            required_comparisons=None,
            comparison_recipes=[
                {
                    "recipe_id": "common.total_amount_agreement",
                    "bindings": {
                        "source": "procure_to_pay.vendor_invoice",
                        "target": "procure_to_pay.goods_receipt",
                    },
                }
            ],
        )
    )

    assert any(
        "not one of the attribute's required record kinds" in item for item in errors
    )


def test_the_three_way_match_recipe_expands_to_a_real_three_record_contract():
    attributes = cycle_vouching.validate_control_attributes(
        [
            _attribute(
                required_record_kinds=[
                    "procure_to_pay.purchase_order",
                    "procure_to_pay.goods_receipt",
                    "procure_to_pay.vendor_invoice",
                ],
                required_comparisons=None,
                comparison_recipes=[
                    {
                        "recipe_id": "procure_to_pay.three_way_match",
                        "bindings": {
                            "purchase_order": "procure_to_pay.purchase_order",
                            "goods_receipt": "procure_to_pay.goods_receipt",
                            "vendor_invoice": "procure_to_pay.vendor_invoice",
                        },
                    }
                ],
            )
        ]
    )

    comparisons = attributes[0]["required_comparisons"]
    assert [item["key"] for item in comparisons] == [
        "invoice_amount_to_order",
        "receipt_quantity_to_order",
    ]


def test_every_catalogued_recipe_expands_into_a_contract_the_gate_accepts():
    """The catalog cannot ship a recipe that cannot validate.

    Each recipe is bound to record kinds its pack offers the fields on, and the
    expansion is put through the real gate.
    """

    checked = 0
    for definition in recipes.COMPARISON_RECIPES:
        for pack_id in definition.pack_ids or ("procure_to_pay",):
            kinds = _bindable_kinds(pack_id, definition)
            if kinds is None:
                continue
            attribute = {
                "key": "recipe_probe",
                "assertion": "Accuracy",
                "requirement": f"Probe for {definition.id}.",
                "evidence_kind": "transaction_cycle",
                "registry": DEFAULT_REGISTRY.reference(pack_id).to_dict(),
                "required_record_kinds": sorted(set(kinds.values())),
                "comparison_recipes": [
                    {"recipe_id": definition.id, "bindings": kinds}
                ],
            }
            if len(set(kinds.values())) < cycle_vouching.MIN_CYCLE_RECORD_KINDS:
                # A single-role recipe cannot stand alone: it reads one record,
                # and a cycle needs a link. Composition is covered below.
                continue
            validated = cycle_vouching.validate_control_attributes([attribute])
            assert validated[0]["required_comparisons"]
            checked += 1
    single_role = [
        definition.id
        for definition in recipes.COMPARISON_RECIPES
        if len(definition.roles) == 1
    ]
    assert checked == len(recipes.COMPARISON_RECIPES) - len(single_role)
    assert single_role == ["common.approval_present", "common.attachment_present"]


@pytest.mark.parametrize(
    "recipe_id", ["common.approval_present", "common.attachment_present"]
)
def test_a_single_role_recipe_composes_with_a_linking_one(recipe_id):
    """It cannot stand alone, and combined with a link it validates.

    Alone it would leave the second required record kind unread, which is the
    defect the unused-kind rule exists to catch.
    """

    link = {
        "recipe_id": "common.total_amount_agreement",
        "bindings": {
            "source": "procure_to_pay.vendor_invoice",
            "target": "procure_to_pay.purchase_order",
        },
    }
    alone = {
        "recipe_id": recipe_id,
        "bindings": {"record": "procure_to_pay.vendor_invoice"},
    }

    errors = _errors(_attribute(required_comparisons=None, comparison_recipes=[alone]))
    assert any("is never read by a comparison" in item for item in errors)

    validated = cycle_vouching.validate_control_attributes(
        [_attribute(required_comparisons=None, comparison_recipes=[link, alone])]
    )
    assert len(validated[0]["required_comparisons"]) == 2


def _bindable_kinds(pack_id: str, definition) -> dict[str, str] | None:
    """Bind each recipe role to a record kind that offers every field it reads."""

    pack = DEFAULT_REGISTRY.pack(pack_id)
    candidates = [
        record_id
        for record_id in pack.record_kind_ids
        if DEFAULT_REGISTRY.record_kind(pack_id, record_id).bindable
    ]
    needed: dict[str, set[tuple[str, str]]] = {role: set() for role in definition.roles}
    for comparison in definition.comparisons:
        for operand in (comparison.left, comparison.right):
            if operand is not None:
                needed[operand.role].add((operand.group, operand.kind))
    bindings: dict[str, str] = {}
    used: set[str] = set()
    for role, selectors in needed.items():
        match = next(
            (
                record_id
                for record_id in candidates
                if record_id not in used
                and all(
                    DEFAULT_REGISTRY.field_kind(pack_id, group, kind).id
                    in DEFAULT_REGISTRY.record_kind(
                        pack_id, record_id
                    ).available_field_kinds
                    for group, kind in selectors
                )
            ),
            None,
        )
        if match is None:
            return None
        bindings[role] = match
        used.add(match)
    return bindings
