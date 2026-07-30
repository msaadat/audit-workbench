"""Focused tests for the registered ``tests.generate`` worker.

The worker owns only the merged generation prompt, bundle-to-message
transformation, response schema, and the full-contract quality gate from
docs/test-capability-merge-plan.md section 4. It is exercised with
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
    tables=("transactions",),
    table_columns=("invoice", "amount"),
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
                    "existing_tests": [],
                },
            )
        )
    for table in tables:
        columns = (
            table_columns.get(table, ())
            if isinstance(table_columns, dict)
            else table_columns
        )
        values.append(
            (
                "table_metadata",
                f"table:{table}",
                ContextRepresentation("table_metadata"),
                {
                    "table": table,
                    "rows": 3,
                    "columns": [{"name": column} for column in columns],
                },
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
        capability_id="tests.specified",
        unit_id="test_generation:RCM-1",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="tests.generate",
        capability_id="tests.specified",
        unit_id="test_generation:RCM-1",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "test-generate-input"},
        activity={"artifact_refs": ["rcm:RCM-1"]},
    )


def _data_step(**overrides):
    value = {
        "label": "Find duplicate invoice keys",
        "instruction": "Compare invoice numbers for duplicates.",
        "code": "result = transactions.filter(pl.col('invoice').is_duplicated())",
    }
    value.update(overrides)
    return value


def _question_step(**overrides):
    value = {
        "label": "Inspect approval evidence",
        "instruction": "Determine whether the payment was approved.",
        "mode": "question",
        "document_ids": ["DOC-1"],
        "question": "Was this payment approved before release?",
        "missing_evidence": "",
    }
    value.update(overrides)
    return value


def _vouch_step(**overrides):
    value = {
        "label": "Compare approved amount",
        "instruction": "Compare the approved amount with the payment record.",
        "mode": "vouch",
        "document_ids": ["DOC-1"],
        "checks": [{"field": "approved_amount", "expected": "12500.00"}],
        "missing_evidence": "",
    }
    value.update(overrides)
    return value


def _data_test(**overrides):
    value = {
        "source": "data",
        "title": "Duplicate payment detection",
        "objective": "Determine whether duplicate payments were prevented.",
        "steps": [_data_step()],
    }
    value.update(overrides)
    return value


def _document_test(**overrides):
    value = {
        "source": "document",
        "title": "Payment approval review",
        "objective": "Determine whether selected payments were approved.",
        "steps": [_question_step()],
    }
    value.update(overrides)
    return value


def test_generate_worker_produces_a_ready_data_test():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    result = WORKERS.execute(_request(), gateway)

    proposed = result.proposal["tests"]
    assert [item["title"] for item in proposed] == ["Duplicate payment detection"]
    assert proposed[0]["source"] == "data"
    assert proposed[0]["rcm_id"] == "RCM-1"
    assert "table_refs" not in proposed[0]["steps"][0]
    assert "result" in proposed[0]["steps"][0]["code"]
    assert gateway.calls[0]["system"] == tests_workers.GENERATE_SYSTEM
    assert (
        gateway.calls[0]["activity"]["context_metrics"]["worker_kind"]
        == "test_generation"
    )


def test_generate_worker_sends_a_compact_context_projection():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    WORKERS.execute(_request(), gateway)

    payload = json.loads(gateway.calls[0]["user"])
    assert set(payload) == {
        "target_rcm_row",
        "planning_context",
        "other_rcm_rows",
        "table_schemas",
        "documents",
        "methodology",
        "instructions",
    }
    assert payload["target_rcm_row"]["id"] == "RCM-1"
    assert payload["planning_context"] == {"objective": "Assess procurement approvals"}
    assert payload["table_schemas"][0]["table"] == "transactions"
    assert "table_profiles" not in payload
    assert "supplied_size" not in gateway.calls[0]["user"]
    assert "representation" not in gateway.calls[0]["user"]


def test_generate_worker_produces_a_ready_document_question_test():
    gateway = _Gateway([json.dumps({"tests": [_document_test()]})])

    result = WORKERS.execute(_request(), gateway)

    proposed = result.proposal["tests"][0]
    assert proposed["source"] == "document"
    step = proposed["steps"][0]
    assert step["mode"] == "question"
    assert step["question"]
    assert "checks" not in step


def test_generate_worker_produces_a_ready_document_vouch_test():
    gateway = _Gateway([json.dumps({"tests": [_document_test(steps=[_vouch_step()])]})])

    result = WORKERS.execute(_request(), gateway)

    step = result.proposal["tests"][0]["steps"][0]
    assert step["mode"] == "vouch"
    assert [dict(check) for check in step["checks"]] == [
        {"field": "approved_amount", "expected": "12500.00"}
    ]
    assert "question" not in step


def test_generate_worker_carries_supplied_methodology_citations():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    result = WORKERS.execute(_request(), gateway)

    refs = result.proposal["tests"][0]["methodology_refs"]
    assert [ref["pack_name"] for ref in refs] == ["Firm AP Guide"]
    assert "text" not in refs[0]


def test_generate_worker_accepts_mixed_source_tests_in_one_response():
    gateway = _Gateway(
        [json.dumps({"tests": [_data_test(), _document_test()]})]
    )

    result = WORKERS.execute(_request(), gateway)

    sources = {item["source"] for item in result.proposal["tests"]}
    assert sources == {"data", "document"}


def test_generate_worker_accepts_a_fenced_response():
    gateway = _Gateway(["```json\n" + json.dumps({"tests": [_data_test()]}) + "\n```"])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["tests"][0]["title"] == "Duplicate payment detection"


def test_generate_worker_rejects_document_only_field_on_a_data_step():
    invalid = json.dumps(
        {"tests": [_data_test(steps=[_data_step(mode="question")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="document-only field 'mode'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_data_only_field_on_a_document_step():
    invalid = json.dumps(
        {"tests": [_document_test(steps=[_question_step(code="result = df")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="data-only field 'code'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_mixed_document_modes_within_one_test():
    invalid = json.dumps(
        {"tests": [_document_test(steps=[_question_step(), _vouch_step()])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="mixes document modes"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_discards_legacy_step_table_refs():
    proposed = _data_test(steps=[_data_step(table_refs=["ghost_table"])])

    result = WORKERS.execute(_request(), _Gateway([json.dumps({"tests": [proposed]})]))

    assert "table_refs" not in result.proposal["tests"][0]["steps"][0]


def test_generate_worker_rejects_an_unknown_column():
    invalid = json.dumps(
        {
            "tests": [
                _data_test(
                    steps=[
                        _data_step(
                            code="result = transactions.filter(pl.col('ghost_column') > 0)"
                        )
                    ]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown column 'ghost_column'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_accepts_columns_introduced_by_a_join():
    bundle = _bundle(
        tables=("requisitions", "po_data"),
        table_columns={
            "requisitions": ("REQUISITION_ID", "ITEM_DESCRIPTION"),
            "po_data": ("REQUISITION_ID", "ITEM_DESCRIPTION"),
        },
    )
    proposed = _data_test(
        steps=[
            _data_step(
                code=(
                    'joined = requisitions.join(po_data, on="REQUISITION_ID", how="inner")\n'
                    'result = joined.filter(pl.col("ITEM_DESCRIPTION") '
                    '!= pl.col("ITEM_DESCRIPTION_right"))'
                )
            )
        ]
    )

    result = WORKERS.execute(
        _request(bundle), _Gateway([json.dumps({"tests": [proposed]})])
    )

    assert result.proposal["tests"][0]["steps"][0]["code"].endswith(
        'pl.col("ITEM_DESCRIPTION_right"))'
    )


def test_generate_worker_rejects_an_unknown_document_id():
    invalid = json.dumps(
        {"tests": [_document_test(steps=[_question_step(document_ids=["DOC-GHOST"])])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="unknown document 'DOC-GHOST'"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_accepts_missing_evidence_as_a_concrete_blocked_step():
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "tests": [
                        _document_test(
                            steps=[
                                _question_step(
                                    document_ids=[], missing_evidence="Signed approval memo"
                                )
                            ]
                        )
                    ]
                }
            )
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    step = result.proposal["tests"][0]["steps"][0]
    assert list(step["document_ids"]) == []
    assert step["missing_evidence"] == "Signed approval memo"


def test_generate_worker_rejects_documents_that_also_claim_missing_evidence():
    invalid = json.dumps(
        {
            "tests": [
                _document_test(
                    steps=[_question_step(missing_evidence="Signed approval memo")]
                )
            ]
        }
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="also claims missing_evidence"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_sandbox_invalid_code():
    invalid = json.dumps(
        {"tests": [_data_test(steps=[_data_step(code="import os\nresult = df")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="not allowed in the sandbox"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_rejects_code_without_result():
    invalid = json.dumps(
        {"tests": [_data_test(steps=[_data_step(code="output = transactions.head(1)")])]}
    )
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="assign the exception rows to `result`"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_reports_every_contract_error_in_one_repair():
    gateway = _Gateway(
        [
            json.dumps(
                {
                    "tests": [
                        _data_test(
                            objective="",
                            steps=[_data_step(code="result = transactions.filter(pl.col('ghost') > 0)")],
                        ),
                        _document_test(
                            steps=[_question_step(question=""), _vouch_step()]
                        ),
                    ]
                }
            ),
            json.dumps({"tests": [_data_test(), _document_test()]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    guidance = gateway.calls[1]["user"]
    assert "tests[0].objective" in guidance
    assert "unknown column 'ghost'" in guidance
    assert "mixes document modes" in guidance


def test_generate_worker_rejects_a_source_the_workspace_cannot_supply():
    invalid = json.dumps({"tests": [_data_test()]})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="no table is available"):
        WORKERS.execute(_request(_bundle(tables=())), gateway)


def test_generate_worker_rejects_an_empty_test_array():
    invalid = json.dumps({"tests": []})
    gateway = _Gateway([invalid, invalid, invalid])

    with pytest.raises(WorkerRunError, match="tests must be a non-empty array"):
        WORKERS.execute(_request(), gateway)


def test_generate_worker_requires_exactly_one_target_row():
    gateway = _Gateway([json.dumps({"tests": [_data_test()]})])

    with pytest.raises(WorkerContractError, match="'rcm_row' must supply exactly one item"):
        WORKERS.execute(_request(_bundle(rcm_rows=("RCM-1", "RCM-2"))), gateway)
