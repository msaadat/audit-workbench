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
    # A cycle read off a field vocabulary, with every requirement it must
    # answer supplied. Middling: choosing join keys is genuinely a judgement,
    # and joining on the wrong field fuses unrelated transactions.
    "cycle_linkage": "medium",
    # The two below are declared ahead of the calls that will carry them — the
    # split matrix turns — so the budget arrives with the prompt rather than
    # being noticed missing afterwards. Neither kind is emitted yet, and an
    # entry nothing sends is inert.
    #
    # Closed-vocabulary classification against a supplied catalogue. The call
    # that spent 56,944 of its 65,853 completion tokens reasoning was doing
    # this and the rest of the matrix at once; on its own it is a lookup.
    "rcm_attributes": "low",
    # Naming the processes a memorandum already names, and assigning themes to
    # them. Recall from one document, not deduction.
    "rcm_scope": "low",
}


def budget_for(worker_kind: object) -> str | None:
    """This kind's deliberation budget, or ``None`` to use the configured one."""

    return REASONING_BY_WORKER_KIND.get(str(worker_kind or ""))


__all__ = ["REASONING_BY_WORKER_KIND", "budget_for"]
