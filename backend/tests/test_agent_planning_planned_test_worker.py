"""Focused tests for the registered ``planning.planned_tests`` worker (P7D.2).

The worker owns only the planned-test prompt, bundle-to-message transformation,
response schema, and the engagement quality gate. It is exercised with
constructed bundles and a gateway stub and must not touch a workspace, store,
resolver, or scheduler.
"""

from __future__ import annotations

import json

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import WORKERS, WorkerContractError, WorkerRequest, WorkerRunError
from app.agent.workers import planning


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1):
        self.calls.append(
            {"system": system, "user": user, "activity": activity, "attempt": attempt}
        )
        return self.responses.pop(0)


_METHODOLOGY_SECTION = {
    "pack_id": "PK-1",
    "pack_name": "Firm AP Guide",
    "version": 2,
    "sha1": "a" * 40,
    "section": "Duplicate payments",
    "citation": "Firm AP Guide v2, Duplicate payments",
    "text": "Audit procedures should address duplicate-payment risk.",
}


def _bundle(*, rcm_rows=("RCM-1",), methodology=(_METHODOLOGY_SECTION,), planned_tests=()):
    values = [
        (
            "planning_context",
            "planning:context",
            ContextRepresentation("planning_context"),
            {"context": {"objective": "Assess procurement approvals"}},
        ),
    ]
    for rcm_id in rcm_rows:
        values.append(
            (
                "rcm_row",
                f"rcm:{rcm_id}",
                ContextRepresentation("current_artifact"),
                {
                    "id": rcm_id,
                    "risk": "Duplicate payments are processed",
                    "control": "Duplicate invoice validation",
                    "planned_tests": list(planned_tests),
                },
            )
        )
    for index, section in enumerate(methodology, start=1):
        values.append(
            (
                "methodology",
                f"methodology:firm:{section['pack_id']}:{index}",
                ContextRepresentation("excerpt"),
                section,
            )
        )
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
        capability_id="planning.planned_tests_ready",
        unit_id="planned_test:RCM-1",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="planning.planned_tests",
        capability_id="planning.planned_tests_ready",
        unit_id="planned_test:RCM-1",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "planned-test-input"},
        activity={"artifact_refs": ["rcm:RCM-1"]},
    )


def _test(**overrides):
    value = {
        "operation": "create",
        "stable_slug": "duplicate-payments",
        "title": "Test duplicate payments",
        "objective": "Determine whether duplicate payments occurred",
        "criteria": "Each invoice is paid once.",
        "steps": ["Identify repeated invoice identifiers."],
        "method": "data_analytics",
        "expected_evidence": "Duplicate listing",
    }
    value.update(overrides)
    return value


def test_planned_test_worker_uses_only_bundle_and_validates_the_proposal():
    gateway = _Gateway([json.dumps({"planned_tests": [_test()]})])

    result = WORKERS.execute(_request(), gateway)

    proposed = result.proposal["planned_tests"]
    assert [item["title"] for item in proposed] == ["Test duplicate payments"]
    # The durable RCM link comes from the one supplied target row, not the model.
    assert proposed[0]["rcm_id"] == "RCM-1"
    assert proposed[0]["rcm_refs"] == ("RCM-1",)
    assert gateway.calls[0]["system"] == planning.PLANNED_TEST_SYSTEM
    assert (
        gateway.calls[0]["activity"]["context_metrics"]["worker_kind"]
        == "planned_test_generation"
    )
    assert "Duplicate payments are processed" in gateway.calls[0]["user"]


def test_planned_test_worker_carries_supplied_methodology_citations():
    gateway = _Gateway([json.dumps({"planned_tests": [_test()]})])

    result = WORKERS.execute(_request(), gateway)

    refs = result.proposal["planned_tests"][0]["methodology_refs"]
    assert [ref["pack_name"] for ref in refs] == ["Firm AP Guide"]
    assert refs[0]["section"] == "Duplicate payments"
    assert refs[0]["sha1"] == "a" * 40
    assert "text" not in refs[0]


def test_planned_test_worker_accepts_the_legacy_procedures_response_key():
    gateway = _Gateway(["```json\n" + json.dumps({"procedures": [_test()]}) + "\n```"])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["planned_tests"][0]["operation"] == "create"


def test_planned_test_worker_reports_every_contract_error_in_one_repair():
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "planned_tests": [
                        _test(
                            steps="Identify repeated invoice identifiers.",
                            sampling="Full population",
                            thresholds="Zero duplicates",
                        )
                    ]
                }
            ),
            json.dumps({"planned_tests": [_test()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    guidance = gateway.calls[1]["user"]
    assert "planned_tests[0].steps" in guidance
    assert "planned_tests[0].sampling must be an object" in guidance
    assert "planned_tests[0].thresholds must be an object" in guidance


def test_planned_test_worker_rejects_noncanonical_sampling_fields():
    invalid = json.dumps(
        {"planned_tests": [_test(sampling={"sample_size": 10})]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(
        WorkerRunError, match=r"planned_tests\[0\]\.sampling\.sample_size is not supported"
    ):
        WORKERS.execute(_request(), gateway)


def test_planned_test_worker_requires_a_planned_test_id_for_updates():
    invalid = json.dumps({"planned_tests": [_test(operation="update")]})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="planned_test_id is required for update"):
        WORKERS.execute(_request(), gateway)


def test_planned_test_worker_requires_exactly_one_target_row():
    gateway = _Gateway([json.dumps({"planned_tests": [_test()]})])

    with pytest.raises(WorkerContractError, match="'rcm_row' must supply exactly one item"):
        WORKERS.execute(_request(_bundle(rcm_rows=("RCM-1", "RCM-2"))), gateway)
