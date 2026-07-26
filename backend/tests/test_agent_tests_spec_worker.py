"""Focused tests for the registered test-specification workers.

Both workers own only their prompt, the bundle-to-message transformation, the
response schema, and the part of the specification contract the supplied context
can decide. The authoritative, frame-dependent validation belongs to the
executor, so these tests use constructed bundles and a gateway stub without a
workspace.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import WORKERS, WorkerRequest, WorkerRunError
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


def _bundle(*, unit_id, kind="datatest", tables=(), documents=()):
    values = [
        (
            "rcm_row",
            "rcm:RCM-1",
            ContextRepresentation("current_artifact"),
            {"id": "RCM-1", "risk": "Duplicate payments", "control": "Duplicate check"},
        ),
        (
            "test",
            f"{kind}:T-1",
            ContextRepresentation("current_artifact"),
            {
                "id": "T-1",
                "title": "Test duplicate payments",
                "objective": "Identify duplicates",
                "steps": ["Identify repeated invoice identifiers."],
            },
        ),
    ]
    for table, columns in tables:
        values.append(
            (
                "table_metadata",
                f"table:{table}",
                ContextRepresentation("table_metadata"),
                {
                    "table": table,
                    "rows": 3,
                    "columns": [{"name": name} for name in columns],
                },
            )
        )
    for document_id in documents:
        values.append(
            (
                "documents",
                f"document:{document_id}",
                ContextRepresentation("summary"),
                {"id": document_id, "title": document_id, "summary": "Approval."},
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
        capability_id="tests.specified",
        unit_id=unit_id,
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _data_spec_request(**kwargs):
    return WorkerRequest(
        worker_id="tests.data_spec",
        capability_id="tests.specified",
        unit_id="data_test_spec:T-1",
        context=_bundle(
            unit_id="data_test_spec:T-1",
            tables=(("transactions", ("invoice", "amount")),),
            **kwargs,
        ),
        unit_input={"input_sha1": "spec-input"},
        activity={"artifact_refs": ["datatest:T-1"]},
    )


def _document_spec_request(**kwargs):
    return WorkerRequest(
        worker_id="tests.document_spec",
        capability_id="tests.specified",
        unit_id="document_test_spec:T-1",
        context=_bundle(
            unit_id="document_test_spec:T-1",
            kind="doctest",
            documents=("DOC-1",),
            **kwargs,
        ),
        unit_input={"input_sha1": "spec-input"},
        activity={"artifact_refs": ["doctest:T-1"]},
    )


_CODE = (
    "result = transactions.filter(transactions['invoice'].is_duplicated())"
)


def _data_spec(**overrides):
    value = {"table_refs": ["transactions"], "code": _CODE}
    value.update(overrides)
    return value


def _document_spec(**overrides):
    value = {
        "mode": "question",
        "items": [
            {
                "label": "Approval",
                "document_ids": ["DOC-1"],
                "question": "Who approved the invoice?",
            }
        ],
    }
    value.update(overrides)
    return value


# --------------------------------------------------------------------------- #
# tests.data_spec
# --------------------------------------------------------------------------- #
def test_data_spec_worker_returns_a_polars_definition_from_the_bundle_alone():
    gateway = _Gateway([json.dumps(_data_spec())])

    result = WORKERS.execute(_data_spec_request(), gateway)

    assert result.proposal["engine"] == "polars"
    assert result.proposal["table_refs"] == ("transactions",)
    assert result.proposal["spec"]["code"] == _CODE
    assert gateway.calls[0]["system"] == tests_workers.DATA_SPEC_SYSTEM
    assert "Identify duplicates" in gateway.calls[0]["user"]


def test_data_spec_prompt_carries_no_registry_payload():
    # The registries are what made the old definition prompt large; a generated
    # Data Test is Polars code, so neither belongs in the turn.
    assert "ANALYTICS REGISTRY" not in tests_workers.DATA_SPEC_SYSTEM
    assert "VALIDATION REGISTRY" not in tests_workers.DATA_SPEC_SYSTEM
    gateway = _Gateway([json.dumps(_data_spec())])

    WORKERS.execute(_data_spec_request(), gateway)

    assert "ANALYTICS REGISTRY" not in gateway.calls[0]["user"]
    assert "VALIDATION REGISTRY" not in gateway.calls[0]["user"]


def test_data_spec_worker_rejects_a_table_outside_the_supplied_schemas():
    invalid = json.dumps(_data_spec(table_refs=["ledger"]))
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown table 'ledger'"):
        WORKERS.execute(_data_spec_request(), gateway)


def test_data_spec_worker_rejects_code_the_sandbox_refuses():
    invalid = json.dumps(_data_spec(code="import os\nresult = os.listdir('.')"))
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="not allowed in the sandbox"):
        WORKERS.execute(_data_spec_request(), gateway)


def test_data_spec_worker_requires_the_result_assignment():
    invalid = json.dumps(_data_spec(code="transactions.head()"))
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="must assign the exception rows"):
        WORKERS.execute(_data_spec_request(), gateway)


def test_data_spec_worker_repairs_once_with_specific_guidance():
    gateway = _Gateway(
        [json.dumps(_data_spec(table_refs=["ledger"])), json.dumps(_data_spec())]
    )

    result = WORKERS.execute(_data_spec_request(), gateway)

    assert result.repaired is True
    assert "unknown table 'ledger'" in gateway.calls[1]["user"]


def test_data_spec_worker_unwraps_a_named_object_wrapper():
    gateway = _Gateway([json.dumps({"data_test": _data_spec()})])

    result = WORKERS.execute(_data_spec_request(), gateway)

    assert result.proposal["spec"]["code"] == _CODE


# --------------------------------------------------------------------------- #
# tests.document_spec
# --------------------------------------------------------------------------- #
def test_document_spec_worker_maps_a_question_mode_to_the_qa_builder():
    gateway = _Gateway([json.dumps(_document_spec())])

    result = WORKERS.execute(_document_spec_request(), gateway)

    assert result.proposal["kind"] == "qa"
    assert result.proposal["items"][0]["question"] == "Who approved the invoice?"


def test_document_spec_worker_maps_a_vouch_mode_to_the_vouching_builder():
    gateway = _Gateway(
        [
            json.dumps(
                _document_spec(
                    mode="vouch",
                    items=[
                        {
                            "label": "Amount",
                            "document_ids": ["DOC-1"],
                            "checks": [{"field": "amount", "expected": "1,200"}],
                        }
                    ],
                )
            )
        ]
    )

    result = WORKERS.execute(_document_spec_request(), gateway)

    assert result.proposal["kind"] == "vouching"
    assert result.proposal["items"][0]["checks"][0]["field"] == "amount"


def test_document_spec_worker_offers_only_the_two_executable_modes():
    # ``attribute`` and ``review`` items are always marked for manual review, so
    # generating them costs a call and returns work the agent never does.
    assert set(tests_workers._MODES) == {"question", "vouch"}
    invalid = json.dumps(_document_spec(mode="attribute"))
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="mode must be 'question' or 'vouch'"):
        WORKERS.execute(_document_spec_request(), gateway)


def test_document_spec_worker_rejects_an_unsupplied_document_reference():
    invalid = json.dumps(
        _document_spec(
            items=[
                {
                    "label": "Approval",
                    "document_ids": ["DOC-9"],
                    "question": "Who approved?",
                }
            ]
        )
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown document 'DOC-9'"):
        WORKERS.execute(_document_spec_request(), gateway)


def test_document_spec_worker_accepts_an_item_with_no_evidence_yet():
    # An unattached item is how a missing-evidence request becomes specific; the
    # executor turns it into a blocked test plus an evidence request.
    gateway = _Gateway(
        [
            json.dumps(
                _document_spec(
                    items=[
                        {
                            "label": "Approval memo",
                            "document_ids": [],
                            "question": "Who approved?",
                        }
                    ],
                    missing_evidence="The signed approval memo was not provided.",
                )
            )
        ]
    )

    result = WORKERS.execute(_document_spec_request(), gateway)

    assert result.proposal["items"][0]["document_ids"] == ()
    assert "approval memo" in result.proposal["missing_evidence"].casefold()


def test_document_spec_worker_requires_a_question_or_checks():
    invalid = json.dumps(
        _document_spec(items=[{"label": "Approval", "document_ids": ["DOC-1"]}])
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="needs a question"):
        WORKERS.execute(_document_spec_request(), gateway)


def test_test_workers_have_no_workspace_store_or_scheduler_dependency():
    source = inspect.getsource(tests_workers)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name.endswith(
            (
                "workspaces",
                "workspace_transactions",
                "store",
                "resolver",
                "workflow_runner",
                "action_runner",
            )
        )
        for name in imported
    )
    assert ".ws" not in source
    assert "load_workspace" not in source
    # The sandbox validator is a static analysis of the proposed code, not a
    # read of engagement data.
    assert "get_frame" not in source
    assert "workspace." not in source
