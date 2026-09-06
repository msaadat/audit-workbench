"""What can be done to an artifact, answered from the registries.

The live runs' failure mode in one sentence: the loop knew what the artifact
*was* and had to guess what could be done to it. Both registries already held
the answer — the action catalog declares the kinds it targets, and capabilities
now declare what they produce, which refs they accept, and which of those refs
mean "redo this one". These tests hold the join between them.
"""

from __future__ import annotations

import pytest

from app import workspaces
from app.agent import actions, operations
from app.agent import capabilities as audit_capabilities
from app.agent import action_tools, loop_tools


def test_every_capability_declares_what_it_produces():
    """The declaration is the contract; a new capability must fill it in."""

    undeclared = sorted(
        {
            capability.id
            for registry in audit_capabilities.REGISTRY_BY_WORKFLOW.values()
            for capability in registry.all()
            if not capability.produces
        }
    )
    # Two outcomes write no artifact, and both are deliberate: ``audit.verified``
    # reads the engagement and settles, and ``planning.change_assessed`` records
    # a judgment about artifacts rather than changing one.
    assert undeclared == ["audit.verified", "planning.change_assessed"]


def test_declared_refs_are_a_subset_of_the_known_artifact_kinds():
    kinds = {
        kind
        for definition in actions.REGISTRY.all()
        for kind in definition.target_kinds
    } | {"table", "document", "planning", "observation", "doctest_item"}
    for registry in audit_capabilities.REGISTRY_BY_WORKFLOW.values():
        for capability in registry.all():
            assert set(capability.produces) <= kinds, capability.id
            assert set(capability.accepts_refs) <= kinds, capability.id
            # Naming an artifact can only mean "redo it" for a ref the
            # capability accepts in the first place.
            assert set(capability.redoes_named) <= set(capability.accepts_refs), capability.id


def test_the_redraft_outcome_for_a_named_test_is_the_first_one_offered():
    """The second live run's mistake, made impossible to repeat.

    Four outcomes accept a ``doctest:`` ref. Exactly one of them redrafts a
    test that already looks usable, and the loop chose one of the other three
    after reading the artifact ten times.
    """

    outcomes = operations.artifact_operations("doctest")["outcomes"]

    assert outcomes[0]["outcome"] == "tests.specified"
    assert outcomes[0]["redraws_when_named"] is True
    definitions = next(
        item for item in outcomes if item["outcome"] == "doc_tests.definitions_ready"
    )
    assert definitions["accepts_this_ref"] is True
    assert definitions["redraws_when_named"] is False


def test_operations_name_both_ways_to_change_an_artifact():
    ops = operations.artifact_operations("finding")

    assert {item["action"] for item in ops["actions"]} == {
        "edit_finding",
        "delete_finding",
    }
    assert [item["risk"] for item in ops["actions"] if item["action"] == "delete_finding"] == [
        "destructive"
    ]
    assert [item["outcome"] for item in ops["outcomes"]] == ["findings.drafted"]


def test_an_unoffered_action_is_not_advertised():
    """The index and the tool list cannot disagree about what is callable."""

    offered = {schema["function"]["name"] for schema in loop_tools.action_tools()}
    for kind in ("procedure", "rcm", "doctest", "table"):
        for item in operations.artifact_operations(kind)["actions"]:
            assert item["action"] in offered, item
    for item in operations.creating_actions():
        assert item["action"] in offered, item
    # The legacy procedure trio is registered and withheld, so its artifact
    # kind offers nothing.
    assert operations.artifact_operations("procedure")["actions"] == []


def test_an_unknown_kind_answers_empty_rather_than_raising():
    assert operations.artifact_operations("nonsense") == {"actions": [], "outcomes": []}
    assert operations.artifact_operations("") == {"actions": [], "outcomes": []}


# --------------------------------------------------------------------------- #
# Where the loop actually reads it
# --------------------------------------------------------------------------- #
@pytest.fixture
def workspace_with_a_finding(workspace_with_data):
    from app import findings

    findings.add(workspace_with_data, {"title": "Duplicate invoices"})
    return workspace_with_data


def test_get_artifact_carries_the_operations_for_that_artifact(workspace_with_a_finding):
    session = action_tools.ActionToolSession(workspace_with_a_finding, [])
    listed = session.dispatch("list_artifacts", {"kinds": ["finding"]})
    ref = listed["artifacts"][0]["ref"]

    got = session.dispatch("get_artifact", {"ref": ref})

    assert got["operations"] == operations.artifact_operations("finding")


def test_list_artifacts_carries_one_entry_per_kind_not_per_artifact(
    workspace_with_a_finding,
):
    """A list of thirty findings answers the same question once."""

    from app import findings

    for index in range(3):
        findings.add(workspace_with_a_finding, {"title": f"Another {index}"})
    session = action_tools.ActionToolSession(workspace_with_a_finding, [])

    listed = session.dispatch("list_artifacts", {"kinds": ["finding"]})

    assert len(listed["artifacts"]) == 4
    assert list(listed["operations_by_kind"]) == ["finding"]
    assert listed["operations_by_kind"]["finding"] == operations.artifact_operations(
        "finding"
    )
    # And what makes a new one, which targets no existing artifact at all.
    assert "create_finding" in {item["action"] for item in listed["creating"]}
