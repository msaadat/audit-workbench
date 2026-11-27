"""How long each kind of turn is worth deliberating over.

Policy, held apart from the gateway that applies it. The gateway owns the
mechanism — read the worker kind off the activity, pass a budget to the
provider — and knows nothing about which kinds exist; this module is the one
place that does, and it is deliberately small enough to read in full.

In code rather than in settings, because a budget is a property of the prompt
it serves: a turn rewritten to ask for more judgement should be reviewed
together with the allowance it is given, and an operator tuning a deployment
has no way to know which turn was which.
"""

from __future__ import annotations

#: Only kinds that differ from the deployment's ``LLM_REASONING`` appear.
#: Absent, a kind gets whatever the operator configured, which is the right
#: default for the turns whose whole job is judgement.
REASONING_BY_WORKER_KIND: dict[str, str] = {
    # Closed-vocabulary classification against a supplied catalogue. The call
    # that spent 56,944 of its 65,853 completion tokens reasoning was doing
    # this and the rest of the matrix at once; on its own it is a lookup.
    "rcm_attributes": "low",
    # The steps a memorandum already names, each one's document types and
    # population from supplied lists — recall — and the rules linking those
    # documents, read off their field vocabulary. Middling, for the second
    # half: choosing join keys is genuinely a judgement, and joining on the
    # wrong field fuses unrelated transactions. ``low`` while the turn drew
    # the steps alone; ``medium`` is what the linkage turn it absorbed ran at.
    "planning_cycle": "medium",
}


def budget_for(worker_kind: object) -> str | None:
    """This kind's deliberation budget, or ``None`` to use the configured one."""

    return REASONING_BY_WORKER_KIND.get(str(worker_kind or ""))


__all__ = ["REASONING_BY_WORKER_KIND", "budget_for"]
