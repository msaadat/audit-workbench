"""Focused tests for the registered finding worker and executor (P7H.2).

The worker owns the prompt, the bundle-to-message transformation, and the
response contract; every reference a finding carries — RCM row, planned test,
execution result, and immutable evidence anchor — is derived by the executor
from the current observation, not from the proposal.
"""

from __future__ import annotations

import json

import pytest

from app import data_tests, rcm_execution, workspaces
from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.reporting import (
    FINDING_EXECUTOR,
    FindingExecutorTarget,
    finding_ref,
    finding_semantic_id,
)
from app.agent.workers import WORKERS, WorkerRequest, WorkerRunError
from app.agent.workers import reporting as reporting_worker
from app.workspace_transactions import parent_hashes


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1):
        self.calls.append(
            {"system": system, "user": user, "activity": activity, "attempt": attempt}
        )
        return self.responses.pop(0)


# The shipped finding template, reduced to the part the worker contract needs:
# the section headings the narrative must answer, and one guidance comment that
# must not survive into the narrative.
TEMPLATE = """# Finding

## Condition

<!-- section: What was found, quantified, in the past tense. -->

## Criteria

## Root Cause

## Risk

## Recommendation
"""

NARRATIVE = """## Condition

Of the 1,284 payments released in the period, 37 were duplicates.

## Criteria

Invoice identifiers are required to be unique.

## Root Cause

Lack of a duplicate check at invoice entry.

## Risk

Financial loss through duplicate payment.

## Recommendation

Enforce a uniqueness constraint on the invoice identifier.
"""


def _bundle(template: str = TEMPLATE):
    values = [
        (
            "observation",
            "observation:OBS-1",
            ContextRepresentation("current_artifact"),
            {
                "id": "OBS-1",
                "summary": "A duplicate invoice identifier was processed.",
                "outcome": "exception",
            },
        ),
        (
            "rcm_row",
            "rcm:RCM-1",
            ContextRepresentation("current_artifact"),
            {"id": "RCM-1", "risk": "Duplicate payments"},
        ),
        (
            "planned_test",
            "planned_test:PT-1",
            ContextRepresentation("current_artifact"),
            {"id": "PT-1", "title": "Test duplicates"},
        ),
        (
            "execution_result",
            "datatest:DAT-1",
            ContextRepresentation("current_artifact"),
            {
                "execution_ref": "datatest:DAT-1",
                "immutable_execution_result": {"exception_count": 1},
                "evidence_anchor": {"source_kind": "datatest"},
            },
        ),
        (
            "finding_template",
            "template:finding",
            ContextRepresentation("artifact_template"),
            template,
        ),
    ]
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=representation,
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(
        capability_id="findings.drafted",
        unit_id="finding:OBS-1",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request():
    return WorkerRequest(
        worker_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id="finding:OBS-1",
        context=_bundle(),
        unit_input={"input_sha1": "finding-input"},
        activity={"artifact_refs": ["observation:OBS-1"]},
    )


def _draft(**overrides):
    value = {
        "title": "Duplicate invoice processing",
        "severity": "medium",
        "narrative": NARRATIVE,
        "cause_pending": False,
    }
    value.update(overrides)
    return value


def _without_section(narrative: str, heading: str) -> str:
    """The narrative with one section's body emptied, its heading intact."""
    start = narrative.index(f"## {heading}")
    end = narrative.find("\n## ", start)
    return narrative[:start] + f"## {heading}\n\n" + narrative[end + 1:]


def test_finding_worker_uses_only_the_supplied_observation_and_execution():
    gateway = _Gateway([json.dumps({"finding": _draft()})])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["finding"]["severity"] == "medium"
    assert gateway.calls[0]["system"] == reporting_worker.FINDING_SYSTEM
    assert "duplicate invoice identifier" in gateway.calls[0]["user"].casefold()
    assert (
        gateway.calls[0]["activity"]["context_metrics"]["worker_kind"]
        == "finding_draft"
    )


def test_finding_worker_repairs_an_empty_template_section_by_name():
    gateway = _Gateway(
        [
            json.dumps(
                {"finding": _draft(narrative=_without_section(NARRATIVE, "Criteria"))}
            ),
            json.dumps({"finding": _draft()}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert "narrative section 'Criteria' is empty" in gateway.calls[1]["user"]


def test_finding_worker_accepts_an_open_root_cause_only_when_it_is_deferred():
    deferred = _draft(
        narrative=_without_section(NARRATIVE, "Root Cause"), cause_pending=True
    )
    gateway = _Gateway([json.dumps({"finding": deferred})])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["finding"]["cause_pending"] is True
    assert result.repaired is False


def test_finding_worker_repairs_an_undeferred_empty_root_cause():
    silent = json.dumps(
        {"finding": _draft(narrative=_without_section(NARRATIVE, "Root Cause"))}
    )
    gateway = _Gateway([silent, json.dumps({"finding": _draft()})])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert "unless cause_pending is true" in gateway.calls[1]["user"]


def test_finding_worker_takes_its_required_sections_from_the_supplied_template():
    # A firm that renames a section moves the contract with it; nothing in the
    # worker enumerates the shipped headings.
    request = WorkerRequest(
        worker_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id="finding:OBS-1",
        context=_bundle(template="## Observation\n\n## Impact\n"),
        unit_input={"input_sha1": "finding-input"},
        activity={"artifact_refs": ["observation:OBS-1"]},
    )
    narrative = "## Observation\n\nA duplicate was released.\n\n## Impact\n\nLoss.\n"
    gateway = _Gateway([json.dumps({"finding": _draft(narrative=narrative)})])

    result = WORKERS.execute(request, gateway)

    assert result.repaired is False
    assert "Observation" in gateway.calls[0]["user"]


def test_finding_worker_rejects_an_unsupported_severity():
    invalid = json.dumps({"finding": _draft(severity="catastrophic")})
    gateway = _Gateway([invalid, invalid])

    with pytest.raises(WorkerRunError, match="severity is unsupported"):
        WORKERS.execute(_request(), gateway)


def _observed_workspace(workspace_with_data):
    """Roll up one executed data test into an eligible exception observation."""
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    data_tests.run(ws, test["id"])
    rcm_execution.rollup(ws)
    observation = ws.observations[0]
    return ws, ws.observations[0]


def _finding_request(ws, observation, draft=None):
    return ExecutorRequest(
        executor_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id=f"finding:{observation['id']}",
        proposal={"finding": draft or _draft()},
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"observation:{observation['id']}"]),
        activity={"artifact_refs": [f"observation:{observation['id']}"]},
    )


def test_finding_executor_derives_every_reference_from_the_observation(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    # References in the proposal are ignored; the observation is authoritative.
    request = _finding_request(
        ws, observation, _draft(rcm_refs=["RCM-SMUGGLED"], auditor_confirmed=True)
    )
    target = FindingExecutorTarget(ws, "run-finding", observation["id"])

    receipt = EXECUTORS.execute(request, target)

    committed = target.workspace.findings[0]
    assert committed["rcm_refs"] == [observation["rcm_id"]]
    assert committed["test_refs"] == [observation["test_id"]]
    assert committed["execution_refs"] == [observation["execution_ref"]]
    assert committed["evidence_refs"]
    assert committed["auditor_confirmed"] is False
    assert committed["semantic_id"] == finding_semantic_id(observation["id"])
    assert receipt.artifact_refs == (finding_ref(committed["id"]),)
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [finding_ref(committed["id"])]
    )


def test_finding_executor_refuses_a_draft_that_fails_support_validation(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    request = _finding_request(
        ws, observation, _draft(narrative=_without_section(NARRATIVE, "Recommendation"))
    )
    target = FindingExecutorTarget(ws, "run-unsupported", observation["id"])

    with pytest.raises(workspaces.WorkspaceError, match="support validation"):
        FINDING_EXECUTOR.implementation(request, target)

    assert workspaces.load_workspace(ws.id).findings == []


def test_finding_executor_reconciles_an_interrupted_commit_idempotently(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    request = _finding_request(ws, observation)
    target = FindingExecutorTarget(ws, "run-reconcile", observation["id"])

    # The commit does not change the observation, so an absent finding is what
    # proves it never landed.
    assert FINDING_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    FINDING_EXECUTOR.implementation(request, target)
    recovered = FINDING_EXECUTOR.reconciler(request, target)

    assert recovered.disposition == "already_applied"
    assert recovered.result.output["id"] == target.workspace.findings[0]["id"]
    # The observation-derived id keeps a repeated commit from drafting twice.
    assert len(target.workspace.findings) == 1
