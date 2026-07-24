"""Focused tests for the registered fieldwork definition workers (P7E.2/P7E.3).

Both workers own only their prompt, the bundle-to-message transformation, the
response schema, and the part of the definition contract the supplied context
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
from app.agent.workers import fieldwork


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1):
        self.calls.append(
            {"system": system, "user": user, "activity": activity, "attempt": attempt}
        )
        return self.responses.pop(0)


def _bundle(*, capability, unit_id, method="data_analytics", tables=(), documents=()):
    values = [
        (
            "rcm_row",
            "rcm:RCM-1",
            ContextRepresentation("current_artifact"),
            {"id": "RCM-1", "risk": "Duplicate payments", "control": "Duplicate check"},
        ),
        (
            "planned_test",
            "planned_test:PT-1",
            ContextRepresentation("current_artifact"),
            {
                "id": "PT-1",
                "title": "Test duplicate payments",
                "objective": "Identify duplicates",
                "method": method,
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
        capability_id=capability,
        unit_id=unit_id,
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _data_test_request(**kwargs):
    return WorkerRequest(
        worker_id="fieldwork.data_test_spec",
        capability_id="fieldwork.definitions_ready",
        unit_id="data_test_spec:PT-1",
        context=_bundle(
            capability="fieldwork.definitions_ready",
            unit_id="data_test_spec:PT-1",
            tables=(("transactions", ("invoice", "amount")),),
            **kwargs,
        ),
        unit_input={"input_sha1": "definition-input"},
        activity={"artifact_refs": ["planned_test:PT-1"]},
    )


def _document_test_request(**kwargs):
    return WorkerRequest(
        worker_id="fieldwork.document_test_spec",
        capability_id="fieldwork.definitions_ready",
        unit_id="document_test_spec:PT-1",
        context=_bundle(
            capability="fieldwork.definitions_ready",
            unit_id="document_test_spec:PT-1",
            method="document_inspection",
            documents=("DOC-1",),
            **kwargs,
        ),
        unit_input={"input_sha1": "definition-input"},
        activity={"artifact_refs": ["planned_test:PT-1"]},
    )


def _data_test(**overrides):
    value = {
        "title": "Duplicate invoices",
        "objective": "Identify duplicate invoice identifiers",
        "engine": "analytics",
        "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice"]}},
    }
    value.update(overrides)
    return value


def _document_test(**overrides):
    value = {
        "title": "Approval review",
        "kind": "review",
        "spec": {"focus": "approval"},
        "items": [
            {"label": "Approval", "document_ids": ["DOC-1"], "summary": "Approved"}
        ],
    }
    value.update(overrides)
    return value


def test_data_test_worker_uses_only_the_bundle_and_its_own_catalogs():
    gateway = _Gateway([json.dumps({"data_test": _data_test()})])

    result = WORKERS.execute(_data_test_request(), gateway)

    assert result.proposal["data_test"]["engine"] == "analytics"
    assert gateway.calls[0]["system"] == fieldwork.DATA_TEST_SPEC_SYSTEM
    # The catalogs travel with the contract, the schema with the bundle.
    assert "ANALYTICS REGISTRY" in gateway.calls[0]["user"]
    assert "transactions" in gateway.calls[0]["user"]


def test_data_test_worker_rejects_a_table_outside_the_supplied_schemas():
    invalid = json.dumps({"data_test": _data_test(table_refs=["ghost_ledger"])})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown table 'ghost_ledger'"):
        WORKERS.execute(_data_test_request(), gateway)


def test_data_test_worker_rejects_an_unregistered_analytics_id():
    invalid = json.dumps(
        {"data_test": _data_test(spec={"test_id": "not-a-test", "params": {}})}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="is not a registry ID"):
        WORKERS.execute(_data_test_request(), gateway)


def test_data_test_worker_requires_a_validation_engine_for_a_validation_test():
    invalid = json.dumps({"data_test": _data_test()})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(
        WorkerRunError, match="validation planned test requires a validation-engine"
    ):
        WORKERS.execute(_data_test_request(method="validation"), gateway)


def test_data_test_worker_repairs_once_with_specific_guidance():
    gateway = _Gateway(
        [
            json.dumps({"data_test": _data_test(engine="sql")}),
            json.dumps({"data_test": _data_test()}),
        ]
    )

    result = WORKERS.execute(_data_test_request(), gateway)

    assert result.repaired is True
    assert "data_test.engine is unsupported" in gateway.calls[1]["user"]


def test_document_test_worker_validates_items_against_supplied_documents():
    gateway = _Gateway([json.dumps({"document_test": _document_test()})])

    result = WORKERS.execute(_document_test_request(), gateway)

    assert result.proposal["document_test"]["kind"] == "review"
    assert gateway.calls[0]["system"] == fieldwork.DOCUMENT_TEST_SPEC_SYSTEM


def test_document_test_worker_rejects_an_unsupplied_document_reference():
    invalid = json.dumps(
        {
            "document_test": _document_test(
                items=[
                    {
                        "label": "Approval",
                        "document_ids": ["DOC-MISSING"],
                        "summary": "Approved",
                    }
                ]
            )
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown document 'DOC-MISSING'"):
        WORKERS.execute(_document_test_request(), gateway)


def test_document_test_worker_rejects_an_unknown_comparison_method():
    invalid = json.dumps(
        {
            "document_test": _document_test(
                kind="vouching",
                items=[
                    {
                        "label": "Invoice",
                        "document_ids": ["DOC-1"],
                        "checks": [
                            {"field": "total", "expected": "10", "method": "vibes"}
                        ],
                    }
                ],
            )
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="Unknown comparison method 'vibes'"):
        WORKERS.execute(_document_test_request(), gateway)


def test_fieldwork_workers_have_no_workspace_store_or_scheduler_dependency():
    source = inspect.getsource(fieldwork)
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
    # The registry catalogs are application constants; engagement data is not read.
    assert "get_frame" not in source
    assert "workspace." not in source
