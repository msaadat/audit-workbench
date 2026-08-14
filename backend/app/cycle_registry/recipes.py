"""Named comparison recipes: the audit tests worth naming, as data.

A transaction-cycle control attribute has to say what would answer its
requirement. Expressed as free-form DSL that is four nested objects, an operator,
a tolerance, and two field selectors per comparison — authored inside a turn
whose real work is judging risk. Most of what auditors actually want there is a
handful of shapes: this amount agrees with that one, this event precedes that
one, this approval exists.

A recipe names one of those shapes. The model picks a recipe id and binds its
placeholders to record kinds; local code expands it into canonical comparisons.
The expansion then goes through exactly the same validation as a hand-authored
comparison, so a recipe is a shortcut through the *authoring*, never through the
gate.

Recipes are deliberately **not** part of ``CyclePackDefinition``: a pack's
``definition_hash`` is stored on RCM rows and revalidated on load, so folding a
new catalog into the pack identity would make every stored reference stale.
"""

from __future__ import annotations

from dataclasses import dataclass

from .operators import operator as _operator


@dataclass(frozen=True)
class RecipeOperand:
    """One side of a recipe comparison, in terms of a placeholder role."""

    role: str
    group: str
    kind: str
    attribute: str

    def identity(self) -> dict:
        return {
            "role": self.role,
            "field": {
                "group": self.group,
                "kind": self.kind,
                "attribute": self.attribute,
            },
        }


@dataclass(frozen=True)
class RecipeComparison:
    """One canonical comparison a recipe expands into."""

    key: str
    #: ``{placeholder}`` tokens are replaced with the bound record kind's label.
    label: str
    operator: str
    left: RecipeOperand
    right: RecipeOperand | None = None
    tolerance: object | None = None

    def identity(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "operator": self.operator,
            "left": self.left.identity(),
            "right": self.right.identity() if self.right is not None else None,
            "tolerance": self.tolerance,
        }


@dataclass(frozen=True)
class ComparisonRecipeDefinition:
    """A named, reusable evidence contract parameterized by record kinds."""

    id: str
    label: str
    #: What audit question the recipe answers. Prompt text.
    purpose: str
    #: Placeholder names the caller must bind, in the order a reader expects.
    roles: tuple[str, ...]
    comparisons: tuple[RecipeComparison, ...]
    #: Packs the recipe is offered for. Empty means every pack, which is correct
    #: for recipes built only from field kinds every record kind carries.
    pack_ids: tuple[str, ...] = ()

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "purpose": self.purpose,
            "roles": list(self.roles),
            "comparisons": [item.identity() for item in self.comparisons],
            "pack_ids": list(self.pack_ids),
        }


def _amount_agreement(
    recipe_id: str,
    label: str,
    purpose: str,
    group: str,
    kind: str,
    *,
    key: str,
    absolute: float,
    pack_ids: tuple[str, ...] = (),
) -> ComparisonRecipeDefinition:
    return ComparisonRecipeDefinition(
        id=recipe_id,
        label=label,
        purpose=purpose,
        roles=("source", "target"),
        comparisons=(
            RecipeComparison(
                key=key,
                label="{source} " + kind.replace("_", " ") + " agrees to {target}",
                operator="numeric_within",
                left=RecipeOperand("source", group, kind, "value"),
                right=RecipeOperand("target", group, kind, "value"),
                tolerance={"absolute": absolute, "percent": 0},
            ),
        ),
        pack_ids=pack_ids,
    )


COMPARISON_RECIPES: tuple[ComparisonRecipeDefinition, ...] = (
    # ---- available to every pack: built only from common field kinds ---------
    _amount_agreement(
        "common.total_amount_agreement",
        "Total amount agreement",
        "the total amount on one record agrees with another record's",
        "amounts",
        "total",
        key="total_amount_agreement",
        absolute=0.01,
    ),
    _amount_agreement(
        "common.unit_price_agreement",
        "Unit price agreement",
        "the unit price on one record agrees with another record's",
        "amounts",
        "unit_price",
        key="unit_price_agreement",
        absolute=0.01,
    ),
    _amount_agreement(
        "common.quantity_agreement",
        "Quantity agreement",
        "the quantity on one record agrees exactly with another record's",
        "quantities",
        "total",
        key="quantity_agreement",
        absolute=0,
    ),
    ComparisonRecipeDefinition(
        id="common.party_agreement",
        label="Party agreement",
        purpose=(
            "the party named on one record is the party named on another — a "
            "payment reaching a different counterparty than the one ordered from"
        ),
        roles=("source", "target"),
        comparisons=(
            RecipeComparison(
                key="party_agreement",
                label="{source} names the same counterparty as {target}",
                operator="equal_normalized",
                # Deliberately the single-valued counterparty rather than
                # ``parties.name``: a record names several parties, and an
                # agreement between two *sets* of names is neither expressible
                # nor meaningful. See ``common.party.counterparty``.
                left=RecipeOperand("source", "parties", "counterparty", "name"),
                right=RecipeOperand("target", "parties", "counterparty", "name"),
            ),
        ),
    ),
    ComparisonRecipeDefinition(
        id="common.document_sequence",
        label="Document sequence",
        purpose=(
            "one record was raised on or before another — a downstream document "
            "predating the record that authorizes it"
        ),
        roles=("earlier", "later"),
        comparisons=(
            RecipeComparison(
                key="document_sequence",
                label="{earlier} is dated on or before {later}",
                operator="date_on_or_before",
                left=RecipeOperand("earlier", "dates", "document_date", "value"),
                right=RecipeOperand("later", "dates", "document_date", "value"),
            ),
        ),
    ),
    ComparisonRecipeDefinition(
        id="common.approval_before_document",
        label="Approval precedes a later record",
        purpose=(
            "the approval on one record was given before a later record was "
            "raised — after-the-fact authorization"
        ),
        roles=("approved", "later"),
        comparisons=(
            RecipeComparison(
                key="approval_before_document",
                label="{approved} is approved before {later} is raised",
                operator="date_on_or_before",
                left=RecipeOperand("approved", "approvals", "approval", "date"),
                right=RecipeOperand("later", "dates", "document_date", "value"),
            ),
        ),
    ),
    ComparisonRecipeDefinition(
        id="common.approval_present",
        label="Approval is present",
        purpose=(
            "the record carries an approver at all. Combine with a linking "
            "comparison — on its own it does not use a second record kind"
        ),
        roles=("record",),
        comparisons=(
            RecipeComparison(
                key="approval_present",
                label="{record} carries an approver",
                operator="present",
                left=RecipeOperand("record", "approvals", "approval", "approver"),
            ),
        ),
    ),
    ComparisonRecipeDefinition(
        id="common.attachment_present",
        label="Supporting attachment is present",
        purpose=(
            "the record encloses its supporting document. Combine with a "
            "linking comparison"
        ),
        roles=("record",),
        comparisons=(
            RecipeComparison(
                key="attachment_present",
                label="{record} encloses its supporting attachment",
                operator="present",
                left=RecipeOperand("record", "attachments", "attachment", "present"),
            ),
        ),
    ),
    # ---- procure to pay -----------------------------------------------------
    ComparisonRecipeDefinition(
        id="procure_to_pay.three_way_match",
        label="Three-way match",
        purpose=(
            "an invoice agrees to the purchase order it was raised under and to "
            "the goods actually received"
        ),
        roles=("purchase_order", "goods_receipt", "vendor_invoice"),
        comparisons=(
            RecipeComparison(
                key="invoice_amount_to_order",
                label="{vendor_invoice} total agrees to {purchase_order}",
                operator="numeric_within",
                left=RecipeOperand("vendor_invoice", "amounts", "total", "value"),
                right=RecipeOperand("purchase_order", "amounts", "total", "value"),
                tolerance={"absolute": 0.01, "percent": 0},
            ),
            RecipeComparison(
                key="receipt_quantity_to_order",
                label="{goods_receipt} quantity agrees to {purchase_order}",
                operator="numeric_within",
                left=RecipeOperand("goods_receipt", "quantities", "total", "value"),
                right=RecipeOperand("purchase_order", "quantities", "total", "value"),
                tolerance={"absolute": 0, "percent": 0},
            ),
        ),
        pack_ids=("procure_to_pay",),
    ),
    ComparisonRecipeDefinition(
        id="procure_to_pay.receipt_before_payment",
        label="Goods received before payment",
        purpose="payment was not made before the goods were received",
        roles=("receipt_record", "payment_record"),
        comparisons=(
            RecipeComparison(
                key="receipt_before_payment",
                label="{receipt_record} receipt date falls on or before "
                "{payment_record} payment date",
                operator="date_on_or_before",
                left=RecipeOperand("receipt_record", "dates", "receipt_date", "value"),
                right=RecipeOperand("payment_record", "dates", "payment_date", "value"),
            ),
        ),
        pack_ids=("procure_to_pay",),
    ),
    ComparisonRecipeDefinition(
        id="procure_to_pay.invoice_before_payment",
        label="Invoice precedes payment",
        purpose="payment was not made before the invoice supporting it existed",
        roles=("invoice", "payment_record"),
        comparisons=(
            RecipeComparison(
                key="invoice_before_payment",
                label="{invoice} is dated on or before {payment_record} payment date",
                operator="date_on_or_before",
                left=RecipeOperand("invoice", "dates", "document_date", "value"),
                right=RecipeOperand("payment_record", "dates", "payment_date", "value"),
            ),
        ),
        pack_ids=("procure_to_pay",),
    ),
    # ---- payroll ------------------------------------------------------------
    _amount_agreement(
        "payroll.net_pay_agreement",
        "Net pay agreement",
        "the net pay on one payroll record agrees with another's",
        "amounts",
        "net_pay",
        key="net_pay_agreement",
        absolute=0.01,
        pack_ids=("payroll",),
    ),
    _amount_agreement(
        "payroll.gross_pay_agreement",
        "Gross pay agreement",
        "the gross pay on one payroll record agrees with another's",
        "amounts",
        "gross_pay",
        key="gross_pay_agreement",
        absolute=0.01,
        pack_ids=("payroll",),
    ),
    ComparisonRecipeDefinition(
        id="payroll.net_pay_to_payment",
        label="Net pay reaches the bank",
        purpose=(
            "the net pay a payroll record states is the amount actually paid out"
        ),
        roles=("payroll_record", "payment_record"),
        comparisons=(
            RecipeComparison(
                key="net_pay_to_payment",
                label="{payroll_record} net pay agrees to {payment_record}",
                operator="numeric_within",
                # The two sides read different field kinds on purpose: a payment
                # states one total, and what it must equal is the payroll
                # record's *net* figure, never its gross.
                left=RecipeOperand("payroll_record", "amounts", "net_pay", "value"),
                right=RecipeOperand("payment_record", "amounts", "total", "value"),
                tolerance={"absolute": 0.01, "percent": 0},
            ),
        ),
        pack_ids=("payroll",),
    ),
    ComparisonRecipeDefinition(
        id="payroll.pay_period_agreement",
        label="Pay period agreement",
        purpose="two payroll records cover the same pay period",
        roles=("source", "target"),
        comparisons=(
            RecipeComparison(
                key="pay_period_agreement",
                label="{source} covers the same pay period as {target}",
                operator="equal_exact",
                left=RecipeOperand("source", "dates", "pay_period_end", "value"),
                right=RecipeOperand("target", "dates", "pay_period_end", "value"),
            ),
        ),
        pack_ids=("payroll",),
    ),
)

_BY_ID = {recipe.id: recipe for recipe in COMPARISON_RECIPES}


def _validate_catalog() -> None:
    """Fail at import if a recipe names an operator or shape that cannot run."""

    if len(_BY_ID) != len(COMPARISON_RECIPES):
        raise ValueError("Comparison recipe ids must be unique.")
    # Comparison keys are unique across the whole catalog, not merely within one
    # recipe. Two recipes applied to one control attribute expand into one
    # comparison list, so a key shared between them would collide there — and the
    # collision would surface as a confusing duplicate-key rejection of a payload
    # whose author did nothing wrong.
    seen: dict[str, str] = {}
    for recipe in COMPARISON_RECIPES:
        for comparison in recipe.comparisons:
            owner = seen.setdefault(comparison.key, recipe.id)
            if owner != recipe.id:
                raise ValueError(
                    f"Comparison key '{comparison.key}' is used by both "
                    f"'{owner}' and '{recipe.id}'."
                )
    for recipe in COMPARISON_RECIPES:
        if not recipe.roles or len(set(recipe.roles)) != len(recipe.roles):
            raise ValueError(f"Recipe '{recipe.id}' needs unique role placeholders.")
        if not recipe.comparisons:
            raise ValueError(f"Recipe '{recipe.id}' declares no comparisons.")
        keys = [item.key for item in recipe.comparisons]
        if len(set(keys)) != len(keys):
            raise ValueError(f"Recipe '{recipe.id}' has duplicate comparison keys.")
        declared = set(recipe.roles)
        for comparison in recipe.comparisons:
            definition = _operator(comparison.operator)
            if definition is None:
                raise ValueError(
                    f"Recipe '{recipe.id}' names unknown operator "
                    f"'{comparison.operator}'."
                )
            if definition.arity == "unary" and comparison.right is not None:
                raise ValueError(
                    f"Recipe '{recipe.id}' supplies a right operand to unary "
                    f"'{comparison.operator}'."
                )
            if definition.arity == "binary" and comparison.right is None:
                raise ValueError(
                    f"Recipe '{recipe.id}' omits the right operand of "
                    f"'{comparison.operator}'."
                )
            if definition.tolerance == "forbidden" and comparison.tolerance is not None:
                raise ValueError(
                    f"Recipe '{recipe.id}' supplies a tolerance to "
                    f"'{comparison.operator}'."
                )
            if definition.tolerance != "forbidden" and comparison.tolerance is None:
                raise ValueError(
                    f"Recipe '{recipe.id}' omits the tolerance of "
                    f"'{comparison.operator}'."
                )
            used = {comparison.left.role} | (
                {comparison.right.role} if comparison.right is not None else set()
            )
            unknown = used - declared
            if unknown:
                raise ValueError(
                    f"Recipe '{recipe.id}' names undeclared role "
                    f"'{sorted(unknown)[0]}'."
                )
        covered = {
            role
            for comparison in recipe.comparisons
            for role in (
                {comparison.left.role}
                | (
                    {comparison.right.role}
                    if comparison.right is not None
                    else set()
                )
            )
        }
        if covered != declared:
            raise ValueError(
                f"Recipe '{recipe.id}' declares role "
                f"'{sorted(declared - covered)[0]}' that no comparison reads."
            )


_validate_catalog()


def recipe(recipe_id: object) -> ComparisonRecipeDefinition | None:
    """Return the recipe for an exact id, or ``None``."""

    return _BY_ID.get(str(recipe_id or "").strip())


def recipes_for_pack(pack_id: object) -> tuple[ComparisonRecipeDefinition, ...]:
    """Return every recipe offered for one pack, generic recipes included."""

    value = str(pack_id or "")
    return tuple(
        item
        for item in COMPARISON_RECIPES
        if not item.pack_ids or value in item.pack_ids
    )


def required_selectors(
    definition: ComparisonRecipeDefinition,
) -> dict[str, set[tuple[str, str, str]]]:
    """Field selectors each placeholder role must be able to answer.

    A recipe fixes its selectors and leaves only the record kinds open, so this
    is exactly what decides whether a candidate binding is possible against the
    evidence a workspace actually holds. Binding a quantity agreement to a
    record kind whose extracted invoices carry no quantity is the one mistake
    the shape itself cannot prevent.
    """

    selectors: dict[str, set[tuple[str, str, str]]] = {
        role: set() for role in definition.roles
    }
    for comparison in definition.comparisons:
        for operand in (comparison.left, comparison.right):
            if operand is None:
                continue
            selectors.setdefault(operand.role, set()).add(
                (operand.group, operand.kind, operand.attribute)
            )
    return selectors


__all__ = [
    "COMPARISON_RECIPES",
    "ComparisonRecipeDefinition",
    "RecipeComparison",
    "RecipeOperand",
    "recipe",
    "recipes_for_pack",
    "required_selectors",
]
