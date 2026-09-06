"""What can be done to one artifact, derived from the two registries.

An auditor asks "this test is wrong, redraft it" and the answer is somewhere in
two places: the action catalog, which declares the artifact kinds each action
targets, and the capability registry, which declares what each outcome produces
and which typed refs it accepts. Both were already true; neither was reachable
from an artifact.

The cost of that showed up on the first live runs of the steering loop. Asked to
redraft one Document Test, it read the artifact ten times and then requested
`doc_tests.definitions_ready`, which defines tests that have no usable
definition and expands nothing for one that has — while `tests.specified`,
accepting the same `doctest:` ref, would have redrafted it in a single unit. The
information needed to tell those apart existed in the registries and nowhere the
model could see.

This module is that join, and nothing more: no policy, no ranking, no model. It
is read at the two places the loop already looks — ``list_artifacts`` and
``get_artifact`` — so finding out what may be done to a thing costs the call it
was going to make anyway rather than a tool of its own.
"""

from __future__ import annotations

from . import actions as action_catalog
from . import capabilities as audit_capabilities

#: Actions that exist but are not the loop's to call, mirrored from
#: :mod:`.loop_tools` by import rather than restated, so the operations index
#: and the tool list can never disagree about what is on offer.
def _unoffered() -> frozenset[str]:
    from .loop_tools import UNOFFERED_ACTIONS

    return UNOFFERED_ACTIONS


def artifact_operations(kind: str) -> dict:
    """Every way the agent may change or re-derive one kind of artifact.

    Returns ``actions`` (immediate, targeted, receipted) and ``outcomes``
    (model-authored, unit-pipelined, approval-gated), each with the one fact
    that decides whether it applies: an action's risk, and whether an outcome
    will accept being pointed at this artifact rather than run over everything.
    """

    wanted = str(kind or "").strip()
    if not wanted:
        return {"actions": [], "outcomes": []}
    unoffered = _unoffered()
    actions = [
        {
            "action": definition.type,
            "does": definition.description,
            "risk": definition.risk,
        }
        for definition in action_catalog.REGISTRY.all()
        if wanted in definition.target_kinds and definition.type not in unoffered
    ]
    seen: set[str] = set()
    outcomes = []
    for workflow_id, registry in sorted(audit_capabilities.REGISTRY_BY_WORKFLOW.items()):
        for capability in registry.all():
            if wanted not in capability.produces or capability.id in seen:
                continue
            seen.add(capability.id)
            outcomes.append(
                {
                    "outcome": capability.id,
                    "does": capability.title,
                    "workflow": workflow_id,
                    # The difference between "redraft this one" and "redo them
                    # all": an outcome that does not accept this ref will run
                    # over its whole scope however narrowly you ask.
                    "accepts_this_ref": wanted in capability.accepts_refs,
                    # And the difference between an outcome that will redraft
                    # what is already there and one that only fills a gap.
                    # Naming the artifact is the whole instruction here: no
                    # ``force``, and no coverage gate.
                    "redraws_when_named": wanted in capability.redoes_named,
                }
            )
    outcomes.sort(
        key=lambda item: (
            not item["redraws_when_named"],
            not item["accepts_this_ref"],
            item["outcome"],
        )
    )
    return {"actions": actions, "outcomes": outcomes}


def creating_actions() -> list[dict]:
    """Actions that make a new artifact, which target no existing one."""

    unoffered = _unoffered()
    return [
        {
            "action": definition.type,
            "does": definition.description,
            "risk": definition.risk,
        }
        for definition in action_catalog.REGISTRY.all()
        if not definition.target_kinds and definition.type not in unoffered
    ]


__all__ = ["artifact_operations", "creating_actions"]
