"""The decision queue merges six sources without losing or duplicating work.

The queue replaces five screens an auditor previously had to visit to find
pending work. These gates hold the two properties that make that replacement
safe: nothing a source produces disappears, and nothing arrives twice.
"""

from __future__ import annotations

import pytest

from app import decisions
from app.agent.workflows import audit as audit_workflow


# --------------------------------------------------------------------------- #
# Consequence line — what a decision releases
# --------------------------------------------------------------------------- #
def test_unblocked_by_is_the_exact_reverse_of_the_dependency_graph():
    for capability_id in audit_workflow.DEPENDENCIES:
        for dependent in audit_workflow.unblocked_by(capability_id):
            assert capability_id in audit_workflow.DEPENDENCIES[dependent], (
                f"{dependent} is reported as unblocked by {capability_id} "
                "but does not declare it as a dependency"
            )


def test_every_declared_dependency_edge_appears_in_the_reverse_walk():
    for dependent, deps in audit_workflow.DEPENDENCIES.items():
        for dependency in deps:
            assert dependent in audit_workflow.unblocked_by(dependency), (
                f"{dependent} declares {dependency} but the reverse walk omits it"
            )


def test_the_terminal_outcome_unblocks_nothing():
    assert audit_workflow.unblocked_by("audit.verified") == ()
    assert audit_workflow.downstream_of("audit.verified") == ()


def test_downstream_counts_grow_toward_the_start_of_the_graph():
    # A decision earlier in the audit holds up strictly more later work, which
    # is the property the queue's ordering relies on.
    assert len(audit_workflow.downstream_of("planning.rcm_ready")) > len(
        audit_workflow.downstream_of("results.rolled_up")
    )
    assert len(audit_workflow.downstream_of("results.rolled_up")) > len(
        audit_workflow.downstream_of("report.working_draft")
    )


def test_no_capability_is_reachable_from_itself():
    for capability_id in audit_workflow.DEPENDENCIES:
        assert capability_id not in audit_workflow.downstream_of(capability_id)


@pytest.mark.parametrize("capability_id", sorted(audit_workflow.DEPENDENCIES))
def test_every_capability_resolves_a_consequence_without_raising(capability_id):
    payload = decisions._unblocks(capability_id)
    assert payload["capability"] == capability_id
    assert isinstance(payload["next"], list)
    assert payload["downstream"] >= len(payload["next"])


def test_an_unknown_capability_degrades_instead_of_raising():
    # Non-audit workflows (documents, doc-tests, analysis) have their own
    # graphs; a decision from one of those must not blow up the queue.
    assert decisions._unblocks("doc_tests.executed") == {
        "capability": "doc_tests.executed", "next": [], "downstream": 0,
    }
    assert decisions._unblocks("") == {"capability": "", "next": [], "downstream": 0}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_an_engagement_with_no_work_returns_an_empty_queue(workspace_with_data):
    payload = decisions.decisions_payload(workspace_with_data)
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["by_kind"] == {kind: 0 for kind in decisions.KINDS}
    assert payload["run"] == {"id": "", "status": "", "waiting": False}


def test_open_observations_become_decisions_and_close_when_disposed(workspace_with_data):
    workspace_with_data.observations.append({
        "id": "OBS-1", "rcm_id": "RCM-1", "test_id": "DAT-1",
        "summary": "Three-way match exceptions", "exception_count": 4,
        "disposition": None, "status": "open", "created": "2026-07-30T00:00:00Z",
    })

    payload = decisions.decisions_payload(workspace_with_data)
    assert payload["by_kind"]["observation"] == 1
    item = next(item for item in payload["items"] if item["kind"] == "observation")
    assert item["severity"] == "critical"
    assert item["target"] == {"tab": "rcm", "query": {"rcm": "RCM-1", "observation": "OBS-1"}}
    # Disposition is what this queue exists to prompt, so it must unblock.
    assert item["unblocks"]["next"] == list(
        audit_workflow.unblocked_by("results.rolled_up")
    )

    workspace_with_data.observations[0]["status"] = "disposed"
    assert decisions.decisions_payload(workspace_with_data)["by_kind"]["observation"] == 0


def test_totals_agree_with_the_per_kind_and_per_severity_breakdowns(workspace_with_data):
    before = decisions.decisions_payload(workspace_with_data)["by_severity"]
    workspace_with_data.observations.extend([
        {"id": "OBS-1", "rcm_id": "RCM-1", "summary": "With exceptions",
         "exception_count": 2, "status": "open", "created": "2026-07-30T00:00:00Z"},
        {"id": "OBS-2", "rcm_id": "RCM-2", "summary": "Without exceptions",
         "exception_count": 0, "status": "open", "created": "2026-07-30T00:00:00Z"},
    ])
    payload = decisions.decisions_payload(workspace_with_data)

    assert payload["total"] == len(payload["items"])
    assert payload["total"] == sum(payload["by_kind"].values())
    assert payload["total"] == sum(payload["by_severity"].values())
    # An observation with exceptions is critical; one without is a warning.
    assert payload["by_severity"]["critical"] == before["critical"] + 1
    assert payload["by_severity"]["warning"] == before["warning"] + 1


def test_items_are_ordered_worst_and_most_blocking_first(workspace_with_data):
    workspace_with_data.observations.extend([
        {"id": "OBS-LOW", "rcm_id": "R1", "summary": "No exceptions",
         "exception_count": 0, "status": "open", "created": "2026-07-01T00:00:00Z"},
        {"id": "OBS-HIGH", "rcm_id": "R2", "summary": "Four exceptions",
         "exception_count": 4, "status": "open", "created": "2026-07-30T00:00:00Z"},
    ])
    items = decisions.decisions_payload(workspace_with_data)["items"]
    severities = [item["severity"] for item in items]
    ranks = [decisions.SEVERITIES.index(value) for value in severities]
    assert ranks == sorted(ranks), "severity must not regress down the queue"
    assert items[0]["source_ref"]["observation_id"] == "OBS-HIGH"


def test_every_item_carries_a_unique_id(workspace_with_data):
    workspace_with_data.observations.extend([
        {"id": f"OBS-{index}", "rcm_id": "R", "summary": "Observation",
         "exception_count": index, "status": "open", "created": "2026-07-30T00:00:00Z"}
        for index in range(5)
    ])
    items = decisions.decisions_payload(workspace_with_data)["items"]
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))


def test_dashboard_attention_contributes_only_rows_no_other_source_owns():
    # Document-test attention rows restate items this queue already carries at
    # item granularity. Admitting them would double-count every review item.
    assert not any(
        prefix.startswith("doctest") for prefix in decisions._ATTENTION_PREFIXES
    )
    assert set(decisions._ATTENTION_PREFIXES) == {"table:", "quality:", "tile:"}


def test_an_open_observation_is_one_decision_not_two(workspace_with_data):
    """Report quality raises `unresolved_exception` per open observation.

    That is the same work as the observation row itself, so admitting both put
    every pending disposition in the queue twice — once where it can be acted
    on, once as an unactionable report warning.
    """
    workspace_with_data.observations.append({
        "id": "OBS-1", "rcm_id": "RCM-1", "summary": "Needs disposition",
        "exception_count": 2, "status": "open", "created": "2026-07-30T00:00:00Z",
    })
    payload = decisions.decisions_payload(workspace_with_data)

    assert payload["by_kind"]["observation"] == 1
    assert not [
        item for item in payload["items"]
        if item["kind"] == "quality" and "OBS-1" in item["context"]
    ]
    assert "unresolved_exception" in decisions._DUPLICATED_QUALITY_CODES


def test_quality_code_reads_the_code_out_of_an_attention_id():
    assert decisions.quality_code("quality:unresolved_exception:3") == "unresolved_exception"
    assert decisions.quality_code("quality:report_arithmetic:0") == "report_arithmetic"
    assert decisions.quality_code("table:0") == "0"
    assert decisions.quality_code("") == ""


def test_every_item_shape_is_stable(workspace_with_data):
    workspace_with_data.observations.append({
        "id": "OBS-1", "rcm_id": "RCM-1", "summary": "Something to disposition",
        "exception_count": 1, "status": "open", "created": "2026-07-30T00:00:00Z",
    })
    for item in decisions.decisions_payload(workspace_with_data)["items"]:
        assert set(item) == {
            "id", "kind", "severity", "title", "context",
            "created_at", "target", "unblocks", "source_ref",
        }
        assert item["kind"] in decisions.KINDS
        assert item["severity"] in decisions.SEVERITIES
        assert set(item["target"]) == {"tab", "query"}
        assert set(item["unblocks"]) == {"capability", "next", "downstream"}
