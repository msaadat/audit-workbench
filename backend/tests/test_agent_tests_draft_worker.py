"""Focused tests for the registered ``tests.draft`` worker.

The worker owns only the test-plan prompt, bundle-to-message transformation,
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
from app.agent.workers import tests as tests_workers


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


def _bundle(
    *,
    rcm_rows=("RCM-1",),
    methodology=(_METHODOLOGY_SECTION,),
    existing=(),
    tables=("transactions",),
    documents=("DOC-1",),
):
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
                    "existing_tests": list(existing),
                },
            )
        )
    for table in tables:
        values.append(
            (
                "table_metadata",
                f"table:{table}",
                ContextRepresentation("table_metadata"),
                {"table": table, "rows": 3, "columns": [{"name": "invoice"}]},
            )
        )
    for document_id in documents:
        values.append(
            (
                "documents",
                f"document:{document_id}",
                ContextRepresentation("summary"),
                {"id": document_id, "title": document_id, "summary": "Policy."},
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
        capability_id="tests.drafted",
        unit_id="test_draft:RCM-1",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="tests.draft",
        capability_id="tests.drafted",
        unit_id="test_draft:RCM-1",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "test-draft-input"},
        activity={"artifact_refs": ["rcm:RCM-1"]},
    )


def _test(**overrides):
    value = {
        "title": "Test duplicate payments",
        "objective": "Determine whether duplicate payments occurred",
        "criteria": "Each invoice is paid once.",
        "steps": ["Identify repeated invoice identifiers."],
        "source": "data",
        "expected_evidence": "Duplicate listing",
    }
    value.update(overrides)
    return value


def test_draft_worker_uses_only_bundle_and_validates_the_proposal():
    gateway = _Gateway([json.dumps({"tests": [_test()]})])

    result = WORKERS.execute(_request(), gateway)

    proposed = result.proposal["tests"]
    assert [item["title"] for item in proposed] == ["Test duplicate payments"]
    # The durable RCM link comes from the one supplied target row, not the model.
    assert proposed[0]["rcm_id"] == "RCM-1"
    assert proposed[0]["source"] == "data"
    assert gateway.calls[0]["system"] == tests_workers.DRAFT_SYSTEM
    assert (
        gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == "test_draft"
    )
    assert "Duplicate payments are processed" in gateway.calls[0]["user"]


def test_draft_worker_carries_supplied_methodology_citations():
    gateway = _Gateway([json.dumps({"tests": [_test()]})])

    result = WORKERS.execute(_request(), gateway)

    refs = result.proposal["tests"][0]["methodology_refs"]
    assert [ref["pack_name"] for ref in refs] == ["Firm AP Guide"]
    assert refs[0]["section"] == "Duplicate payments"
    assert refs[0]["sha1"] == "a" * 40
    assert "text" not in refs[0]


def test_draft_worker_accepts_a_fenced_response():
    gateway = _Gateway(["```json\n" + json.dumps({"tests": [_test()]}) + "\n```"])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["tests"][0]["title"] == "Test duplicate payments"


def test_draft_worker_reports_every_contract_error_in_one_repair():
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "tests": [
                        _test(
                            steps="Identify repeated invoice identifiers.",
                            source="both",
                            criteria="",
                        )
                    ]
                }
            ),
            json.dumps({"tests": [_test()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    guidance = gateway.calls[1]["user"]
    assert "tests[0].steps" in guidance
    assert "tests[0].source must be 'data' or 'document'" in guidance
    assert "tests[0].criteria must be a non-empty string" in guidance


def test_draft_worker_rejects_an_unsupported_source():
    invalid = json.dumps({"tests": [_test(source="interview")]})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match=r"tests\[0\]\.source must be"):
        WORKERS.execute(_request(), gateway)


def test_draft_worker_rejects_an_empty_test_array():
    invalid = json.dumps({"tests": []})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="tests must be a non-empty array"):
        WORKERS.execute(_request(), gateway)


def test_draft_worker_rejects_a_source_the_workspace_cannot_supply():
    # A data test against a workspace with no tables could never be specified,
    # so it is a contract violation now rather than a unit that fails later.
    invalid = json.dumps({"tests": [_test(source="data")]})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="no table is available"):
        WORKERS.execute(_request(_bundle(tables=())), gateway)


def test_draft_worker_requires_exactly_one_target_row():
    gateway = _Gateway([json.dumps({"tests": [_test()]})])

    with pytest.raises(WorkerContractError, match="'rcm_row' must supply exactly one item"):
        WORKERS.execute(_request(_bundle(rcm_rows=("RCM-1", "RCM-2"))), gateway)
