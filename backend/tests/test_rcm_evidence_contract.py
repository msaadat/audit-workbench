"""The evidence contract: what planning decides, what generation decides.

Every rule here exists because one live procurement RCM generation failed twice
and took thirteen rows with it. The verbatim response is
``fixtures/rcm_operator_rejection.json``. The original root cause was that the
operator vocabulary appeared nowhere in the turn that had to author it.

The deeper cause outlasted that fix: the planning turn was authoring an
evidence contract — record kinds, field selectors, operators — while being
deliberately denied any sight of the evidence, so it could name a comparison
against a field the extracted invoices did not carry, and only test generation,
one capability downstream, could discover it. A row now cites the audit *shape*
and stops. Which records fill the shape is bound during generation, against a
manifest of what the workspace actually holds.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
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


def _attribute(recipe_ids=("common.total_amount_agreement",), **overrides) -> dict:
    attribute = {
        "key": "invoice_match",
        "assertion": "Accuracy",
        "requirement": "The invoice agrees to the purchase order.",
        "evidence_kind": "transaction_cycle",
        "registry": _registry(),
        "comparison_recipes": [{"recipe_id": value} for value in recipe_ids],
    }
    attribute.update(overrides)
    return attribute


def _row(attribute: dict) -> dict:
    return {"id": "RCM-1", "control_attributes": [attribute]}


def _expand(attribute: dict, bindings: list[dict]) -> list[dict]:
    """Expand one attribute's cited shapes under the supplied bindings."""

    return cycle_vouching.required_comparisons_for(
        rcm_row=_row(attribute),
        requirement_refs=[f"RCM-1:{attribute['key']}"],
        recipe_bindings=bindings,
    )


def _expand_errors(attribute: dict, bindings: list[dict]) -> tuple[str, ...]:
    with pytest.raises(cycle_vouching.CycleSchemaError) as raised:
        _expand(attribute, bindings)
    return raised.value.errors


def _errors(attribute: dict) -> tuple[str, ...]:
    with pytest.raises(cycle_vouching.CycleSchemaError) as raised:
        cycle_vouching.validate_control_attributes([attribute])
    return raised.value.errors


_PO_TO_INVOICE = {
    "recipe_id": "common.total_amount_agreement",
    "bindings": {
        "source": "procure_to_pay.vendor_invoice",
        "target": "procure_to_pay.purchase_order",
    },
}


# --- the vocabulary lives in the catalog, not in a prompt --------------------


def test_the_gate_and_the_operator_table_cannot_drift_apart():
    assert cycle_vouching.OPERATORS is operators.OPERATORS
    assert cycle_vouching.OPERATORS == {
        definition.id for definition in operators.OPERATOR_DEFINITIONS
    }


def test_no_authoring_prompt_states_an_operator_because_none_authors_one():
    """The vocabulary that caused the live failure is no longer reachable.

    Operators, tolerances, and field selectors are fixed inside the recipe
    catalog. Planning cites a shape by id and generation binds it to records, so
    neither turn can name an operator — and neither can get one wrong.
    """

    # ``present`` is excluded only because it is a substring of the recipe ids
    # that offer it (``common.approval_present``), not because it is authorable.
    authorable = sorted(cycle_vouching.OPERATORS - {"present"})
    assert authorable
    for prompt in (planning.RCM_EVIDENCE_SYSTEM, tests_worker.GENERATE_SYSTEM):
        for operator_id in authorable:
            assert operator_id not in prompt, operator_id
    assert "operator_tolerance" not in planning.RCM_EVIDENCE_SYSTEM
    assert "tolerance" not in planning.RCM_EVIDENCE_SYSTEM


def test_the_evidence_prompt_offers_the_recipe_catalog_and_nothing_lower_level():
    assert (
        prompts.comparison_recipe_catalog(planning._RCM_PACK_IDS)
        in planning.RCM_EVIDENCE_SYSTEM
    )
    unwrapped = " ".join(planning.RCM_EVIDENCE_SYSTEM.split())
    assert "No bindings, no record kinds, no field selectors, no operators" in unwrapped
    assert "unsupported" in planning.RCM_EVIDENCE_SYSTEM


def test_the_generation_prompt_binds_shapes_it_does_not_author_them():
    assert "recipe_bindings" in tests_worker.GENERATE_SYSTEM
    assert "eligible_bindings" in tests_worker.GENERATE_SYSTEM
    assert "It has no assertions" in tests_worker.GENERATE_SYSTEM
    assert "entry_quantifier" not in tests_worker.GENERATE_SYSTEM


# --- replaying the live failure ---------------------------------------------


def _row_errors(row: dict, index: int) -> list[str]:
    try:
        planning._normalized_rcm_row(row, index, set())
    except planning.WorkerResponseValidationError as error:
        return list(error.errors)
    return []


def _all_errors(rows: list[dict]) -> list[str]:
    return [
        message
        for index, row in enumerate(rows, start=1)
        for message in _row_errors(row, index)
    ]


def test_the_live_response_can_no_longer_even_be_expressed(rejected_rows):
    """Six invalid operators, on comparisons a row may no longer author.

    The whole defect class is gone by construction rather than by a better
    error message: the keys those comparisons lived in are not part of a control
    attribute any more.
    """

    errors = _all_errors(rejected_rows)
    assert errors
    rejected_keys = [
        item
        for item in errors
        if "unexpected key 'required_comparisons'" in item
        or "unexpected key 'required_record_kinds'" in item
    ]
    assert rejected_keys
    assert not [item for item in errors if "not a supported operator" in item]


def test_the_live_rejection_keeps_the_rows_that_were_never_wrong(rejected_rows):
    """Eleven of thirteen rows were fine and all thirteen were discarded."""

    valid = [
        row
        for index, row in enumerate(rejected_rows, start=1)
        if not _row_errors(row, index)
    ]
    assert len(valid) == 11
    assert len(rejected_rows) == 13


def test_the_live_document_validates_once_its_contracts_cite_recipes():
    """The end the live run never reached.

    The same thirteen rows, with each transaction-cycle attribute citing a
    recipe rather than hand-rolling DSL. Nothing about the audit judgment
    changes, and the document validates on the first attempt.
    """

    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    contracted = [_with_recipe_citations(_strategy_only(row)) for row in rows]

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


def _with_recipe_citations(row: dict) -> dict:
    """Attach the contract the evidence pass now authors: shapes, unbound."""

    return {
        **row,
        "control_attributes": [
            attribute
            if attribute["evidence_kind"] != "transaction_cycle"
            else {
                **attribute,
                "registry": _registry(),
                "comparison_recipes": [
                    {"recipe_id": "common.total_amount_agreement"}
                ],
            }
            for attribute in row["control_attributes"]
        ],
    }


def _blank_request():
    """A worker request whose bundle supplies no current RCM rows."""

    from test_agent_planning_rcm_worker import _request

    return _request()


# --- what a row may and may not say -----------------------------------------


def test_a_row_cites_a_shape_and_stops():
    validated = cycle_vouching.validate_control_attributes([_attribute()])

    assert validated[0]["comparison_recipes"] == [
        {"recipe_id": "common.total_amount_agreement"}
    ]
    assert "required_comparisons" not in validated[0]
    assert "required_record_kinds" not in validated[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("required_record_kinds", ["procure_to_pay.vendor_invoice"]),
        ("required_comparisons", [{"key": "x"}]),
        ("comparison_recipes_applied", [{"recipe_id": "x"}]),
    ],
)
def test_a_row_cannot_author_the_evidence_contract(field, value):
    """Each of these describes evidence the planning turn was never shown."""

    errors = _errors(_attribute(**{field: value}))

    assert any(f"unexpected key '{field}'" in item for item in errors)


def test_a_citation_carries_nothing_but_its_recipe_id():
    errors = _errors(
        _attribute(
            comparison_recipes=[
                {
                    "recipe_id": "common.total_amount_agreement",
                    "bindings": {"source": "procure_to_pay.vendor_invoice"},
                }
            ]
        )
    )

    assert any("unexpected key 'bindings'" in item for item in errors)


def test_a_transaction_cycle_attribute_must_cite_something():
    errors = _errors(_attribute(comparison_recipes=[]))

    assert any("comparison_recipes" in item for item in errors)


def test_an_unknown_recipe_is_rejected_with_the_available_ones():
    errors = _errors(_attribute(recipe_ids=("common.invented",)))

    assert any("is not a comparison recipe offered for pack" in item for item in errors)
    assert any("common.total_amount_agreement" in item for item in errors)


def test_a_recipe_offered_for_another_pack_is_not_available():
    errors = _errors(_attribute(recipe_ids=("payroll.net_pay_agreement",)))

    assert any("is not a comparison recipe offered for pack" in item for item in errors)


def test_a_shape_is_cited_once_however_many_records_it_will_answer():
    errors = _errors(
        _attribute(
            recipe_ids=(
                "common.total_amount_agreement",
                "common.total_amount_agreement",
            )
        )
    )

    assert any("is cited twice" in item for item in errors)


def test_a_non_cycle_attribute_cannot_cite_a_recipe():
    errors = _errors(
        _attribute(evidence_kind="document_content", registry=None)
    )

    assert any("does not accept comparison recipes" in item for item in errors)


# --- binding a cited shape to real records ----------------------------------


def test_a_recipe_expands_into_canonical_comparisons():
    comparisons = _expand(_attribute(), [_PO_TO_INVOICE])

    assert [item["key"] for item in comparisons] == ["total_amount_agreement"]
    comparison = comparisons[0]
    assert comparison["operator"] == "numeric_within"
    assert comparison["tolerance"] == {"absolute": 0.01, "percent": 0}
    assert comparison["left"]["record_kind"] == "procure_to_pay.vendor_invoice"
    assert comparison["right"]["record_kind"] == "procure_to_pay.purchase_order"


def test_one_shape_bound_twice_qualifies_its_keys_by_binding():
    """An invoice agreeing to its order *and* its payment is one cited shape."""

    comparisons = _expand(
        _attribute(),
        [
            _PO_TO_INVOICE,
            {
                "recipe_id": "common.total_amount_agreement",
                "bindings": {
                    "source": "procure_to_pay.vendor_invoice",
                    "target": "procure_to_pay.payment_voucher",
                },
            },
        ],
    )

    assert [item["key"] for item in comparisons] == [
        "total_amount_agreement_vendor_invoice_purchase_order",
        "total_amount_agreement_vendor_invoice_payment_voucher",
    ]


def test_a_cited_shape_with_no_binding_is_named():
    errors = _expand_errors(
        _attribute(recipe_ids=("common.total_amount_agreement",)), []
    )

    assert any("no binding was supplied for it" in item for item in errors)


def test_a_binding_must_cover_exactly_the_declared_roles():
    errors = _expand_errors(
        _attribute(recipe_ids=("procure_to_pay.three_way_match",)),
        [
            {
                "recipe_id": "procure_to_pay.three_way_match",
                "bindings": {
                    "purchase_order": "procure_to_pay.purchase_order",
                    "vendor_invoice": "procure_to_pay.vendor_invoice",
                },
            }
        ],
    )

    assert any("must bind exactly" in item for item in errors)


def test_a_binding_to_an_unregistered_record_kind_is_a_validation_error():
    """Not an escaping registry exception: the worker repairs validation errors."""

    errors = _expand_errors(
        _attribute(),
        [
            {
                "recipe_id": "common.total_amount_agreement",
                "bindings": {
                    "source": "procure_to_pay.vendor_invoice",
                    "target": "procure_to_pay.bank_account",
                },
            }
        ],
    )

    assert any("is not registered" in item for item in errors)


def test_an_expansion_is_validated_like_any_comparison():
    """A recipe is a shortcut through authoring, never through the gate.

    Bound to record kinds whose pack does not offer the field, the expansion
    fails exactly as a hand-written comparison naming that field would.
    """

    attribute = _attribute(
        recipe_ids=("payroll.net_pay_agreement",),
        registry=DEFAULT_REGISTRY.reference("payroll").to_dict(),
    )
    errors = _expand_errors(
        attribute,
        [
            {
                "recipe_id": "payroll.net_pay_agreement",
                "bindings": {
                    "source": "payroll.bank_payment",
                    "target": "payroll.employment_contract",
                },
            }
        ],
    )

    assert any("is unavailable on role" in item for item in errors)


def test_the_three_way_match_recipe_expands_to_a_real_three_record_contract():
    comparisons = _expand(
        _attribute(recipe_ids=("procure_to_pay.three_way_match",)),
        [
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

    assert [item["key"] for item in comparisons] == [
        "invoice_amount_to_order",
        "receipt_quantity_to_order",
    ]


def test_catalogued_comparison_keys_are_unique_across_recipes():
    """Two recipes on one attribute expand into one list, so keys cannot clash."""

    keys = [
        comparison.key
        for definition in recipes.COMPARISON_RECIPES
        for comparison in definition.comparisons
    ]
    assert len(keys) == len(set(keys))


def test_every_catalogued_recipe_expands_into_a_contract_the_gate_accepts():
    """The catalog cannot ship a recipe that cannot validate."""

    checked = 0
    for definition in recipes.COMPARISON_RECIPES:
        for pack_id in definition.pack_ids or ("procure_to_pay",):
            kinds = _bindable_kinds(pack_id, definition)
            if kinds is None:
                continue
            attribute = _attribute(
                recipe_ids=(definition.id,),
                registry=DEFAULT_REGISTRY.reference(pack_id).to_dict(),
            )
            comparisons = _expand(
                attribute, [{"recipe_id": definition.id, "bindings": kinds}]
            )
            assert comparisons
            checked += 1
    assert checked == len(recipes.COMPARISON_RECIPES)


# --- eligibility: what the evidence can actually answer ----------------------


def _selectors(**by_kind) -> dict[str, set[tuple[str, str, str]]]:
    return {
        kind: {tuple(item.split(".")) for item in selectors}
        for kind, selectors in by_kind.items()
    }


def test_only_record_kinds_carrying_every_read_selector_are_eligible():
    """The mistake the shape itself cannot catch.

    A quantity agreement bound to invoices that carry no quantity is exactly the
    contract the live engagement produced, and it could not be repaired by the
    turn that met it.
    """
    available = _selectors(
        **{
            "procure_to_pay.purchase_order": [
                "quantities.total.value",
                "amounts.total.value",
            ],
            "procure_to_pay.goods_receipt": ["quantities.total.value"],
            "procure_to_pay.vendor_invoice": ["amounts.total.value"],
        }
    )

    eligible = cycle_vouching.eligible_recipe_bindings(
        "common.quantity_agreement",
        reference=DEFAULT_REGISTRY.reference("procure_to_pay"),
        available_selectors=available,
    )

    bound = {tuple(sorted(item.values())) for item in eligible}
    assert bound == {
        ("procure_to_pay.goods_receipt", "procure_to_pay.purchase_order")
    }
    assert all("procure_to_pay.vendor_invoice" not in item.values() for item in eligible)


def test_a_shape_no_record_kind_can_answer_offers_nothing():
    available = _selectors(
        **{"procure_to_pay.vendor_invoice": ["amounts.total.value"]}
    )

    assert (
        cycle_vouching.eligible_recipe_bindings(
            "common.quantity_agreement",
            reference=DEFAULT_REGISTRY.reference("procure_to_pay"),
            available_selectors=available,
        )
        == []
    )


def test_a_binding_whose_comparison_would_have_two_set_operands_is_not_offered():
    """The other mistake the shape cannot catch, and the turn cannot repair.

    A selector the evidence states more than once compiles to a set operand,
    and a comparison with a set on both sides is refused — rightly, since
    "every value here equals every value there" has no reading. The live
    engagement spent its whole retry budget on one: the manifest offered the
    only binding there was, expansion failed locally, and the repair message
    asked for a cycle test the response had already written.
    """
    available = _selectors(
        **{
            "procure_to_pay.purchase_order": ["parties.name.name"],
            "procure_to_pay.purchase_requisition": ["parties.name.name"],
            "procure_to_pay.vendor_invoice": ["parties.name.name"],
        }
    )
    multiplicity = {
        "procure_to_pay.purchase_order": {("parties", "name", "name"): 2},
        "procure_to_pay.purchase_requisition": {("parties", "name", "name"): 3},
        "procure_to_pay.vendor_invoice": {("parties", "name", "name"): 1},
    }
    reference = DEFAULT_REGISTRY.reference("procure_to_pay")

    # A recipe reading the multi-valued selector directly. ``party_agreement``
    # itself reads ``parties.counterparty``, which exists precisely so this
    # comparison stays scalar; the rule has to hold for any recipe.
    definition = next(
        item
        for item in recipes.COMPARISON_RECIPES
        if item.id == "common.party_agreement"
    )
    patched = replace(
        definition,
        comparisons=(
            replace(
                definition.comparisons[0],
                left=recipes.RecipeOperand("source", "parties", "name", "name"),
                right=recipes.RecipeOperand("target", "parties", "name", "name"),
            ),
        ),
    )

    offered = [
        binding
        for binding in itertools.permutations(available, 2)
        if not cycle_vouching._binding_is_set_to_set(
            patched, dict(zip(("source", "target"), binding)), multiplicity
        )
    ]

    pairs = {frozenset(binding) for binding in offered}
    # Every pairing with the single-valued invoice survives; the one pairing
    # where both sides are multi-valued does not.
    assert (
        frozenset(
            {
                "procure_to_pay.purchase_order",
                "procure_to_pay.purchase_requisition",
            }
        )
        not in pairs
    )
    assert (
        frozenset(
            {"procure_to_pay.purchase_order", "procure_to_pay.vendor_invoice"}
        )
        in pairs
    )
    # And the real recipe, reading the counterparty, is offerable for the pair
    # the set-to-set rule refused.
    counterparty = _selectors(
        **{
            kind: ["parties.counterparty.name"]
            for kind in (
                "procure_to_pay.purchase_order",
                "procure_to_pay.purchase_requisition",
            )
        }
    )
    eligible = cycle_vouching.eligible_recipe_bindings(
        "common.party_agreement",
        reference=reference,
        available_selectors=counterparty,
        available_multiplicity={
            kind: {("parties", "counterparty", "name"): 1} for kind in counterparty
        },
    )
    assert {frozenset(item.values()) for item in eligible} == {
        frozenset(
            {
                "procure_to_pay.purchase_order",
                "procure_to_pay.purchase_requisition",
            }
        )
    }


def test_one_record_kind_cannot_fill_two_placeholders_of_one_shape():
    available = _selectors(
        **{"procure_to_pay.vendor_invoice": ["amounts.total.value"]}
    )

    assert (
        cycle_vouching.eligible_recipe_bindings(
            "common.total_amount_agreement",
            reference=DEFAULT_REGISTRY.reference("procure_to_pay"),
            available_selectors=available,
        )
        == []
    )


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
