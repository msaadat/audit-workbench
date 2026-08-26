"""Phase 7 gates for assurance-aware downstream Cycle-vouch outputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app import (
    cycle_vouching,
    doc_tests,
    documents,
    findings,
    rcm_execution,
    report,
    working_papers,
)
from app.agent.context import adapters
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.reporting import FindingExecutorTarget, verify_audit
from app.evidence import document_anchor
from app.workspace_transactions import parent_hashes
from app.workspaces import WorkspaceError
from test_cycle_vouching_phase2 import _manifest
from test_cycle_vouching_phase3 import _workspace


FIXTURE = Path(__file__).parent / "fixtures" / "procurement_cycle_phase0.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _signed_cycle(
    contract: dict,
    monkeypatch,
    *,
    disposition: str = "confirmed",
    selection: dict | None = None,
) -> tuple[object, dict]:
    workspace, test, _current = _workspace(
        contract, monkeypatch, selection=selection
    )
    evaluated = cycle_vouching.evaluate_cycle_test(workspace, test)
    doc_tests.save_test(workspace, evaluated)
    signed = doc_tests.update_item(
        workspace,
        evaluated["id"],
        evaluated["items"][0]["id"],
        {"state": disposition},
    )
    return workspace, signed


def _exception_cycle_with_citations(contract: dict, monkeypatch) -> tuple[object, dict, dict]:
    workspace, test, _current = _workspace(contract, monkeypatch)
    evaluated = cycle_vouching.evaluate_cycle_test(workspace, test)
    source = documents.add_document(
        workspace, "cycle-evidence.txt", b"Immutable local Cycle vouch evidence."
    )
    anchor = document_anchor(
        source, 1, "Immutable local Cycle vouch evidence.", generated_by="phase7-test"
    )
    item = evaluated["items"][0]
    assertion_keys = list(item["result_by_assertion"])
    for key in assertion_keys[:2]:
        result = item["result_by_assertion"][key]
        result["verdict"] = "mismatch"
        result["evidence_refs"] = [{**anchor, "field": key, "item_id": item["id"]}]
    item["evaluation"]["state"] = "failed"
    item["evaluation"]["result_sha1"] = cycle_vouching._sha1_hash(
        item["result_by_assertion"]
    )
    doc_tests.save_test(workspace, evaluated)
    signed = doc_tests.update_item(
        workspace, test["id"], item["id"], {"state": "exception"}
    )
    return workspace, signed, source


def test_rcm_counts_distinct_items_and_keeps_assertion_diagnostics_separate(
    contract, monkeypatch
):
    workspace, test, _source = _exception_cycle_with_citations(contract, monkeypatch)

    rolled = rcm_execution.rollup(workspace)
    test_rollup = rolled["rows"][0]["test_rollups"][0]
    row_rollup = rolled["rows"][0]

    assert test_rollup["tested_items"] == 1
    assert test_rollup["failed_items"] == 1
    assert test_rollup["exception_count"] == 1
    assert test_rollup["open_exception_count"] == 1
    assert test_rollup["assertion_mismatches"] == 2
    assert row_rollup["failed_items"] == 1
    assert row_rollup["exceptions"] == 1
    assert row_rollup["open_exceptions"] == 1
    assert row_rollup["assertion_mismatches"] == 2
    assert "3 exception" not in test_rollup["result_summary"]
    assert doc_tests.load_test(workspace, test["id"])["exception_count"] == 1


def test_failed_and_incomplete_counts_can_name_the_same_distinct_item(
    contract, monkeypatch
):
    workspace, test, _current = _workspace(contract, monkeypatch)
    evaluated = cycle_vouching.evaluate_cycle_test(workspace, test)
    item = evaluated["items"][0]
    results = list(item["result_by_assertion"].values())
    results[0]["verdict"] = "mismatch"
    results[1]["verdict"] = "invalid_extraction"
    # Aggregate evaluation prioritizes a mismatch, but Phase 7 item metrics are
    # independent predicates rather than mutually exclusive state buckets.
    item["evaluation"]["state"] = "failed"

    rollup = doc_tests.result_rollup(evaluated)

    assert rollup["tested_items"] == 1
    assert rollup["failed_items"] == 1
    assert rollup["incomplete_items"] == 1
    assert rollup["assertion_mismatches"] == 1


def test_targeted_evidence_carries_an_auditor_control_conclusion(
    contract, monkeypatch
):
    """Selection breadth is reported, not enforced.

    ``targeted_evidence_only`` stays on the roll-up as a description of how the
    items were reached, but it no longer refuses the auditor's conclusion, caps
    the row, or writes a scope limitation nobody asked for. Narrow evidence is a
    judgment to disclose, and the auditor is the one who makes it.
    """

    workspace, test, _source = _exception_cycle_with_citations(contract, monkeypatch)

    doc_tests.update_test(
        workspace, test["id"], {"control_conclusion": "ineffective"}
    )

    rolled = rcm_execution.rollup(workspace)
    test_rollup = rolled["rows"][0]["test_rollups"][0]
    completion = rcm_execution.completion(workspace)
    context = report.build_context(workspace)

    assert test_rollup["assurance_scope"] == "targeted_evidence_only"
    assert test_rollup["conclusion_eligible"] is True
    assert test_rollup["control_conclusion"] == "ineffective"
    assert test_rollup["control_conclusion_source"] == "auditor"
    assert test_rollup["open_exception_count"] == 1
    assert rolled["rows"][0]["control_conclusion"] == "ineffective"
    assert "assurance_gaps" not in completion
    report_test = context["rcm"][0]["tests"][0]
    assert report_test["control_conclusion"] == "ineffective"
    assert report_test["rollup"]["open_exceptions"] == 1
    # No synthetic limitation is injected on the test's behalf; the section
    # carries only what an auditor actually wrote.
    assert not [
        item
        for item in context["scope_limitations"]
        if item["test_id"] == test["id"]
    ]


def test_evidence_aware_simple_vouching_is_concludable_the_same_way(
    contract, monkeypatch
):
    workspace, cycle_test, _current = _workspace(contract, monkeypatch)
    test = doc_tests.create_test(
        workspace,
        {
            "kind": "vouching",
            "title": "Availability-biased supporting evidence",
            "rcm_id": cycle_test["rcm_id"],
            "status": "completed",
            "spec": {"sampling": {"method": "evidence_covered_first"}},
            "items": [
                {
                    "label": "One evidenced transaction",
                    "state": "exception",
                    "checks": [
                        {
                            "field": "approval",
                            "expected": "approved",
                            "found": "missing",
                            "method": "exact",
                            "verdict": "mismatch",
                        }
                    ],
                }
            ],
        },
    )

    doc_tests.update_test(
        workspace, test["id"], {"control_conclusion": "ineffective"}
    )
    rollup = doc_tests.result_rollup(doc_tests.load_test(workspace, test["id"]))

    assert rollup["assurance_scope"] == "targeted_evidence_only"
    assert rollup["conclusion_eligible"] is True
    assert rollup["control_conclusion"] == "ineffective"
    assert rollup["open_exceptions"] == 1


def test_sampled_current_signed_cycle_can_carry_auditor_control_conclusion(
    contract, monkeypatch
):
    workspace, test = _signed_cycle(
        contract,
        monkeypatch,
        selection={
            "mode": "sample",
            "method": "random",
            "size": 1,
            "seed": 17,
            "assurance_scope": "sampled_population",
        },
    )

    updated = doc_tests.update_test(
        workspace, test["id"], {"control_conclusion": "effective"}
    )
    rolled = rcm_execution.rollup(workspace)

    assert doc_tests.result_rollup(updated)["conclusion_eligible"] is True
    assert rolled["rows"][0]["conclusion_eligible_tests"] == 1
    assert rolled["rows"][0]["supplemental_tests"] == 0
    assert rolled["rows"][0]["control_conclusion"] == "effective"


def test_item_observation_finding_context_and_staleness_follow_canonical_hashes(
    contract, monkeypatch
):
    workspace, test, source = _exception_cycle_with_citations(contract, monkeypatch)
    rcm_execution.rollup(workspace)
    observation = workspace.observations[0]
    item = doc_tests.load_test(workspace, test["id"])["items"][0]

    assert observation["cycle_item_id"] == item["id"]
    assert observation["definition_sha1"] == item["evaluation"]["definition_sha1"]
    assert observation["evaluation_result_sha1"] == item["evaluation"]["result_sha1"]
    assert observation["exception_count"] == 1
    assert observation["assertion_mismatch_count"] == 2
    assert {anchor["source_id"] for anchor in observation["evidence_refs"]} == {
        source["id"]
    }
    assert all(anchor["item_id"] == item["id"] for anchor in observation["evidence_refs"])
    projection = adapters._finding_execution_projection(
        workspace, observation["execution_ref"], cycle_item_id=item["id"]
    )
    encoded = json.dumps(projection, sort_keys=True)
    assert projection["definition_sha1"] == observation["definition_sha1"]
    assert projection["item"]["evaluation"]["result_sha1"] == observation[
        "evaluation_result_sha1"
    ]
    for forbidden in ("frozen_row", "raw_value", "display", "excerpt"):
        assert forbidden not in encoded

    request = ExecutorRequest(
        executor_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id=f"finding:{observation['id']}",
        proposal={"finding": {
            "title": "One tested cycle was dispositioned as an exception",
            "cause_pending": True,
            "narrative": (
                "## Condition\n\nOne tested item contains an auditor-confirmed "
                "exception.\n\n"
                "## Criteria\n\nThe tested item should satisfy the declared "
                "assertions.\n\n"
                "## Root Cause\n\n"
                "## Risk\n\nThe item requires follow-up.\n\n"
                "## Recommendation\n\nResolve the item-specific exception.\n"
            ),
        }},
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(
            workspace, [f"observation:{observation['id']}"]
        ),
        activity={"artifact_refs": [f"observation:{observation['id']}"]},
    )
    target = FindingExecutorTarget(workspace, "run-phase7", observation["id"])
    EXECUTORS.execute(request, target)
    workspace = target.workspace
    finding = target.workspace.findings[0]
    assert finding["source_observation_id"] == observation["id"]
    assert any(anchor.get("item_id") == item["id"] for anchor in finding["evidence_refs"])
    assert findings.support_issues(workspace, finding) == []

    changed = copy.deepcopy(test["definition"]["assertions"][0])
    changed["label"] = str(changed["label"]) + " (revised)"
    manifest = _manifest(contract)
    monkeypatch.setattr(
        cycle_vouching,
        "transaction_evidence_manifest",
        lambda *_args, **_kwargs: copy.deepcopy(manifest),
    )
    workspace.update_rcm(
        test["rcm_id"], {"control_attributes": contract["control_attributes"]}
    )
    current = doc_tests.load_test(workspace, test["id"])
    doc_tests.append_cycle_assertions(
        workspace,
        test["id"],
        expected_test_sha1=current["sha1"],
        assertions=[changed],
    )
    rcm_execution.rollup(workspace)

    observation = next(
        value for value in workspace.observations if value["id"] == observation["id"]
    )
    assert observation["outcome"] == "needs_manual_check"
    assert observation["classification"] == "stale_cycle_disposition"
    assert any(
        "not a current exception" in issue
        for issue in findings.support_issues(workspace, finding)
    )


def test_grid_rcm_working_paper_and_report_inputs_share_current_results(
    contract, monkeypatch
):
    workspace, test = _signed_cycle(contract, monkeypatch)
    grid = cycle_vouching.grid_projection(test)
    rolled = rcm_execution.rollup(workspace)
    paper = working_papers.render_rcm(workspace, test["rcm_id"])
    context = report.build_context(workspace)
    current = doc_tests.load_test(workspace, test["id"])
    item = current["items"][0]
    rollup = doc_tests.result_rollup(current)

    assert grid["definition_sha1"] == item["evaluation"]["definition_sha1"]
    assert {
        key: cell["verdict"] for key, cell in grid["rows"][0]["cells"].items()
    } == {
        key: result["verdict"]
        for key, result in item["result_by_assertion"].items()
    }
    row_rollup = rolled["rows"][0]
    assert row_rollup["tested_items"] == rollup["tested_items"] == 1
    assert row_rollup["assertion_mismatches"] == rollup["assertion_mismatches"]
    assert "Targeted evidence - not a sample" in paper["markdown"]
    assert "Tested cycle grid" in paper["markdown"]
    assert "Missing required roles: none" in paper["markdown"]
    assert current["sha1"] in paper["markdown"]
    assert item["evaluation"]["result_sha1"] in paper["markdown"]
    assert context["statistics"]["tested_items"] == 1
    assert context["statistics"]["assertion_mismatches"] == rollup[
        "assertion_mismatches"
    ]
    # The row carries its own assurance scope; the report context holds one view
    # of each test rather than a row list, a roll-up list, and a doc-test list.
    assert context["rcm"][0]["assurance_scopes"] == ["targeted_evidence_only"]
    assert "rcm_rollup" not in context
    assert "document_tests" not in context
    assert "steps" not in context["rcm"][0]["tests"][0]
    assert "steps" not in context["rcm"][0]["tests"][0]["rollup"]
