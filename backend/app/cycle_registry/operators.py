"""The comparison-operator vocabulary, as data.

This table is the single source of truth for the assertion DSL. Validation reads
it (``cycle_vouching.validate_assertions``), and every prompt that asks a model
to author a comparison renders its contract from it
(``agent.prompts.comparison_contract``).

Before this module the vocabulary existed only inside the validator's control
flow and inside one worker's prose. The ``planning.rcm`` prompt never stated it
at all, so every comparison the model authored used an invented operator name and
every RCM generation failed. A table that both sides read cannot drift like that:
a new operator is a new entry here, and both the gate and the prompts move with
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Arity = Literal["unary", "binary"]
#: ``forbidden`` — the operator takes no tolerance at all.
#: ``numeric_object`` — ``{"absolute": <number>, "percent": <number>}``.
#: ``integer_days`` — a non-negative whole number of days.
ToleranceKind = Literal["forbidden", "numeric_object", "integer_days"]


@dataclass(frozen=True)
class OperatorDefinition:
    """One comparison verb: how it is shaped and what it is for."""

    id: str
    label: str
    arity: Arity
    #: The semantic type both operands must carry, or ``None`` where the
    #: operator is type-agnostic and only requires that the two operands agree.
    operand_type: str | None
    tolerance: ToleranceKind
    #: Whether operand order carries meaning. A direction-sensitive operator is
    #: additionally checked against the pack's declared chronology, so stating it
    #: backwards is rejected rather than silently inverting the test.
    directional: bool
    #: What this operator is *for*, in one line, written for a model that is
    #: choosing between them. This is prompt text: keep it concrete.
    guidance: str

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "arity": self.arity,
            "operand_type": self.operand_type,
            "tolerance": self.tolerance,
            "directional": self.directional,
        }


OPERATOR_DEFINITIONS: tuple[OperatorDefinition, ...] = (
    OperatorDefinition(
        id="equal_exact",
        label="Exact agreement",
        arity="binary",
        operand_type=None,
        tolerance="forbidden",
        directional=False,
        guidance=(
            "two values agree character for character. Use for identifiers, "
            "document numbers, and codes."
        ),
    ),
    OperatorDefinition(
        id="equal_normalized",
        label="Agreement after normalization",
        arity="binary",
        operand_type=None,
        tolerance="forbidden",
        directional=False,
        guidance=(
            "two values agree once case, spacing, and punctuation are "
            "normalized. Use for names and free-text references that two "
            "records print differently."
        ),
    ),
    OperatorDefinition(
        id="numeric_within",
        label="Numeric agreement within a tolerance",
        arity="binary",
        operand_type="number",
        tolerance="numeric_object",
        directional=False,
        guidance=(
            "two numbers agree within a tolerance. Use for every amount, "
            "quantity, and price comparison — never equal_exact, which fails on "
            "rounding. tolerance is an object: "
            '{"absolute": 0.01, "percent": 0} for a currency amount, '
            '{"absolute": 0, "percent": 0} for a quantity that must match exactly.'
        ),
    ),
    OperatorDefinition(
        id="date_on_or_before",
        label="Date sequence",
        arity="binary",
        operand_type="date",
        tolerance="forbidden",
        directional=True,
        guidance=(
            "the left date falls on or before the right date. Use for cycle "
            "sequence. Operand order is the audit meaning and is checked "
            "against the pack's chronology: put the EARLIER event on the left. "
            '"goods received before payment" is left=receipt date, '
            "right=payment date. There is no greater-than operator — express the "
            "requirement in the earlier-to-later direction instead."
        ),
    ),
    OperatorDefinition(
        id="date_within",
        label="Date proximity",
        arity="binary",
        operand_type="date",
        tolerance="integer_days",
        directional=False,
        guidance=(
            "two dates fall within a number of days of each other. Use for "
            "proximity rather than sequence. tolerance is a whole number of "
            "days, not an object."
        ),
    ),
    OperatorDefinition(
        id="present",
        label="Attribute is present",
        arity="unary",
        operand_type=None,
        tolerance="forbidden",
        directional=False,
        guidance=(
            "one value is stated at all. Unary: supply left and omit right. "
            "Accepted only on an approval or attachment attribute, because "
            "those exist on a record only when someone performed the control "
            "step; a record prints its own amounts and statuses regardless, so "
            "asserting those are present proves nothing."
        ),
    ),
)

OPERATORS: frozenset[str] = frozenset(
    definition.id for definition in OPERATOR_DEFINITIONS
)

_BY_ID = {definition.id: definition for definition in OPERATOR_DEFINITIONS}

#: Wrong operator names seen in real responses, mapped to what the author almost
#: certainly meant plus any correction the rename alone does not cover.
#:
#: These are used **only to phrase a rejection**. They are deliberately never
#: applied as a rewrite: ``equals`` on an amount should become ``numeric_within``
#: with a tolerance, not ``equal_exact``, and ``greater_than_or_equal`` on a pair
#: of dates only becomes ``date_on_or_before`` if the operands are also swapped.
#: Both are audit-design decisions, and silently guessing at either would
#: substitute a different test for the one the requirement asked for.
_NEAR_MISSES: dict[str, tuple[str, str]] = {
    "equals": (
        "equal_exact",
        "or numeric_within with a tolerance if the values are amounts or quantities",
    ),
    "equal": (
        "equal_exact",
        "or numeric_within with a tolerance if the values are amounts or quantities",
    ),
    "eq": (
        "equal_exact",
        "or numeric_within with a tolerance if the values are amounts or quantities",
    ),
    "==": ("equal_exact", ""),
    "exact_match": ("equal_exact", ""),
    "match": ("equal_exact", ""),
    "matches": ("equal_exact", ""),
    "same": ("equal_exact", ""),
    "equals_ignore_case": ("equal_normalized", ""),
    "iequals": ("equal_normalized", ""),
    "approximately": ("numeric_within", ""),
    "approx": ("numeric_within", ""),
    "close_to": ("numeric_within", ""),
    "near": ("numeric_within", ""),
    "amount_match": ("numeric_within", ""),
    "within": ("numeric_within", "or date_within if the operands are dates"),
    "sum_equals": ("numeric_within", ""),
    "greater_than_or_equal": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    "greater_than": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    "gte": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    "gt": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    ">=": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    ">": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    "after": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    "on_or_after": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    "date_after": (
        "date_on_or_before",
        "with the operands swapped so the earlier event is on the left",
    ),
    "less_than_or_equal": ("date_on_or_before", ""),
    "lte": ("date_on_or_before", ""),
    "<=": ("date_on_or_before", ""),
    "less_than": ("date_on_or_before", ""),
    "lt": ("date_on_or_before", ""),
    "<": ("date_on_or_before", ""),
    "before": ("date_on_or_before", ""),
    "on_or_before": ("date_on_or_before", ""),
    "date_before": ("date_on_or_before", ""),
    "precedes": ("date_on_or_before", ""),
    "date_order": ("date_on_or_before", ""),
    "chronological": ("date_on_or_before", ""),
    "days_between": ("date_within", ""),
    "within_days": ("date_within", ""),
    "date_diff": ("date_within", ""),
    "not_null": ("present", ""),
    "notnull": ("present", ""),
    "exists": ("present", ""),
    "is_present": ("present", ""),
    "populated": ("present", ""),
    "non_empty": ("present", ""),
    "not_empty": ("present", ""),
}


def operator(operator_id: object) -> OperatorDefinition | None:
    """Return the definition for an exact operator id, or ``None``.

    Only an exact match counts. Surrounding whitespace is ignored because it is
    a transport artefact rather than a choice; case is not, because a table of
    lowercase ids is what every prompt states.
    """

    return _BY_ID.get(str(operator_id or "").strip())


def unsupported_operator_message(value: object, *, label: str) -> str:
    """Phrase a rejected operator so the next attempt can actually fix it.

    Names the offending value, the complete legal vocabulary, and — where the
    value is a recognizable near-miss — the operator that was probably meant,
    together with whatever the rename alone would not fix. The previous message
    named only the offending value, which is why a repair turn told ``eq`` was
    unsupported answered with ``equals``.
    """

    text = str(value or "").strip()
    allowed = ", ".join(sorted(OPERATORS))
    message = (
        f"{label} operator '{text}' is not a supported operator. "
        f"Supported operators are exactly: {allowed}."
    )
    suggestion = _NEAR_MISSES.get(text.casefold())
    if suggestion is not None:
        target, qualifier = suggestion
        message += f" For '{text}' use '{target}'"
        message += f" {qualifier}." if qualifier else "."
    return message


__all__ = [
    "Arity",
    "OPERATORS",
    "OPERATOR_DEFINITIONS",
    "OperatorDefinition",
    "ToleranceKind",
    "operator",
    "unsupported_operator_message",
]
