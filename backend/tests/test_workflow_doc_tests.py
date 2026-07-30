"""Phase 10 gate for the standalone document-test workflow (`P10.6`).

`DocTestRunner` was deleted because standalone document-test execution is fan-out
over semantic items — the scheduler's own description of itself — and because the
audit lifecycle already scheduled the same units. These tests pin what that
migration has to preserve: per-item resume, deterministic comparisons and their
evidence anchors, cited Q&A answers through the registered worker and executor,
evidence blocking, final outcomes, and one
implementation shared with `fieldwork.executed`.
"""

from __future__ import annotations

import inspect
import json

import pytest

from app import doc_tests, documents, workspaces
from app.agent import audit_execution, doc_tests_execution, runner, store
from app.agent import capabilities as capability_registries
from app.agent.capabilities import doc_tests as doc_test_capabilities
from app.agent.routing import classify_command
from app.agent.workflow_dispatch import build_workflow_runner
from app.agent.workflows import doc_tests as doc_tests_workflow
from conftest import wait_run


EVIDENCE = b"Invoice: 1001\nAmount: 150.00\nApproved by: the controller\n"


def _workspace(label="Document tests"):
    return workspaces.create_workspace(label)


def _vouching(ws, *, title="Invoice support"):
    document = documents.add_document(ws, "evidence.txt", EVIDENCE)
    return document, doc_tests.create_test(
        ws,
        {
            "kind": "vouching",
            "title": title,
            "items": [
                {
                    "label": "Invoice 1001",
                    "document_ids": [document["id"]],
                    "checks": [
                        {"field": "invoice", "expected": 1001, "method": "normalized"},
                        {
                            "field": "amount",
                            "expected": 150,
                            "method": "numeric_tolerance",
                        },
                    ],
                }
            ],
        },
    )


def _run(ws, test_id, mode="auto", generation_mode="reuse_existing"):
    return runner.start_command_run(
        ws,
        mode,
        {
            "text": "Run the document test.",
            "goal_template": "document_test_execution",
            "requested_outcomes": list(doc_tests_workflow.FULL_DOC_TEST_OUTCOMES),
            "target_refs": [f"doctest:{test_id}"],
            "generation_mode": generation_mode,
        },
        context={"test_id": test_id},
    )


def _units(run, capability):
    stage = next(
        (
            item
            for item in run["workflow"]["stages"]
            if item["capability"] == capability
        ),
        None,
    )
    return list((stage or {}).get("units") or [])


# --------------------------------------------------------------------------- #
# The declared graph
# --------------------------------------------------------------------------- #
def test_the_document_test_graph_is_declared_once_and_hash_identified():
    assert doc_tests_workflow.DEPENDENCIES == {
        "doc_tests.definitions_ready": (),
        "doc_tests.executed": ("doc_tests.definitions_ready",),
    }
    assert doc_tests_workflow.definition_hash().startswith("sha1:") or (
        len(doc_tests_workflow.definition_hash()) >= 40
    )
    assert doc_tests_workflow.definition_hash() == doc_tests_workflow.definition_hash()
    registry = capability_registries.REGISTRY_BY_WORKFLOW[
        doc_tests_workflow.WORKFLOW_ID
    ]
    assert [capability.id for capability in registry.all()] == list(
        doc_test_capabilities.CAPABILITY_IDS
    )


def test_execution_outcomes_route_to_the_document_test_workflow():
    resolution = classify_command(
        {
            "text": "Run the document test.",
            "requested_outcomes": ["doc_tests.executed"],
            "target_refs": ["doctest:DT-1"],
        }
    )

    assert resolution["route"] == "workflow"
    assert resolution["workflow_definition"] == doc_tests_workflow.WORKFLOW_ID


def test_the_run_endpoint_starts_a_document_test_workflow_run():
    ws = _workspace("Doc test endpoint")
    _document, test = _vouching(ws)

    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.post(
            f"/api/workspaces/{ws.id}/doc-tests/{test['id']}/run", json={}
        )
    assert response.status_code == 200
    created = response.json()
    assert created["engine"] == store.WORKFLOW_ENGINE
    assert created["workflow"]["definition"] == doc_tests_workflow.WORKFLOW_ID
    assert created["workflow"]["scope"]["test_ids"] == [test["id"]]
    finished = wait_run(ws, created["id"])
    assert finished["status"] == "completed", finished.get("error")


# --------------------------------------------------------------------------- #
# Deterministic comparison, anchors, and per-item resume
# --------------------------------------------------------------------------- #
def test_deterministic_comparison_records_anchors_and_settles_directly():
    ws = _workspace("Doc test comparison")
    document, test = _vouching(ws)

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    assert finished["status"] == "completed", finished.get("error")
    # No model turn: a vouching worklist is a local comparison end to end.
    assert int(finished["usage"].get("llm_turns") or 0) == 0
    executed = _units(finished, "doc_tests.executed")
    assert [unit["kind"] for unit in executed] == ["document_test_execution"]
    assert executed[0]["status"] == "succeeded"
    assert executed[0]["result_refs"] == [f"doctest:{test['id']}"]

    saved = doc_tests.load_test(ws, test["id"])
    item = saved["items"][0]
    assert item["state"] == "confirmed"
    assert saved["status"] == "completed"
    assert [check["verdict"] for check in item["checks"]] == ["match", "match"]
    anchors = [anchor["source_sha1"] for anchor in item["evidence_refs"]]
    assert anchors and set(anchors) == {document["sha1"]}
    assert finished["doc_tests"]["rollup"]["matched"] == 2
    milestone = next(
        item for item in finished["milestones"]
        if item["capability"] == "doc_tests.executed"
    )
    assert milestone["headline"] == "Document testing complete"
    assert next(
        item["value"] for item in milestone["metrics"]
        if item["label"] == "Matches"
    ) == 2


def test_a_second_run_reuses_the_executed_result_and_re_runs_nothing():
    ws = _workspace("Doc test reuse")
    _document, test = _vouching(ws)
    wait_run(ws, _run(ws, test["id"])["id"])

    second = wait_run(ws, _run(ws, test["id"])["id"])

    # Execution is satisfied by existence, so no unit expands and nothing is
    # re-compared; currency of the earlier result is deliberately not assessed.
    assert second["status"] == "completed", second.get("error")
    assert _units(second, "doc_tests.executed") == []
    assert "doc_tests.executed" in second["workflow"]["reused_capabilities"]
    assert second["workflow"]["reused_capability_details"][0][
        "currency_status"
    ] == "not_assessed"


def test_a_final_result_item_is_not_re_run():
    ws = _workspace("Doc test disposition resume")
    _document, test = _vouching(ws)
    stored = doc_tests.load_test(ws, test["id"])
    stored["items"][0]["state"] = "confirmed"
    stored["status"] = "completed"
    doc_tests.save_test(ws, stored)

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    assert finished["status"] == "completed", finished.get("error")
    assert _units(finished, "doc_tests.executed") == []
    saved = doc_tests.load_test(ws, test["id"])
    assert saved["items"][0]["state"] == "confirmed"


def test_explicit_force_re_runs_an_already_executed_test():
    ws = _workspace("Doc test force")
    _document, test = _vouching(ws)
    wait_run(ws, _run(ws, test["id"])["id"])

    forced = wait_run(
        ws, _run(ws, test["id"], generation_mode="force")["id"]
    )

    assert forced["status"] == "completed", forced.get("error")
    assert [unit["kind"] for unit in _units(forced, "doc_tests.executed")] == [
        "document_test_execution"
    ]


# --------------------------------------------------------------------------- #
# Definition readiness and evidence blocking
# --------------------------------------------------------------------------- #
def test_an_unusable_definition_is_reported_and_not_executed():
    ws = _workspace("Doc test unusable definition")
    test = doc_tests.create_test(
        ws,
        {
            "kind": "vouching",
            "title": "No evidence attached",
            "items": [{"label": "Invoice 1001", "document_ids": [], "checks": []}],
        },
    )

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    assert finished["status"] == "completed_with_open_items", finished.get("error")
    definitions = _units(finished, "doc_tests.definitions_ready")
    assert [unit["kind"] for unit in definitions] == ["document_test_definition"]
    assert definitions[0]["status"] == "awaiting_confirmation"
    assert definitions[0]["error"] == doc_tests_execution.DEFINITION_REVIEW_REQUIRED
    # The agent never authors a fix, and never executes a worklist that cannot
    # perform even its bounded local work.
    assert _units(finished, "doc_tests.executed") == []


def test_an_evidence_blocked_test_blocks_against_its_request(monkeypatch):
    ws = _workspace("Doc test evidence blocked")
    test = doc_tests.create_test(
        ws,
        {
            "kind": "vouching",
            "title": "Awaiting evidence",
            "items": [
                {
                    "label": "Voucher",
                    "document_ids": [
                        documents.add_document(ws, "partial.txt", b"nothing useful")[
                            "id"
                        ]
                    ],
                    "checks": [
                        {"field": "amount", "expected": 10, "method": "normalized"}
                    ],
                }
            ],
        },
    )
    # A worklist intentionally waiting on requested evidence.
    stored = doc_tests.load_test(ws, test["id"])
    stored["status"] = "blocked"
    stored["scope_limitations"] = "The signed voucher has not been provided."
    doc_tests.save_test(ws, stored)

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    executed = _units(finished, "doc_tests.executed")
    assert [unit["status"] for unit in executed] == ["blocked"]
    assert "signed voucher" in str(executed[0]["error"])


# --------------------------------------------------------------------------- #
# Q&A through the registered worker, executor, and gateway
# --------------------------------------------------------------------------- #
def _qa_workspace(monkeypatch, fake_agent_llm):
    ws = _workspace("Doc test Q&A")
    document = documents.add_document(ws, "policy.txt", EVIDENCE)
    test = doc_tests.build_qa(
        ws,
        {
            "title": "Approval question",
            "document_ids": [document["id"]],
            "questions": ["Who approved the purchase?"],
        },
    )
    fake_agent_llm.overrides["agent:document_qa"] = {
        "answer": "The controller approved it.",
        "outcome": "accepted",
        "citations": [{"page": 1, "excerpt": "Approved by: the controller"}],
    }
    return ws, document, test


def test_qa_execution_fans_out_per_item_document_pair_through_the_pipeline(
    monkeypatch, fake_agent_llm
):
    ws, document, test = _qa_workspace(monkeypatch, fake_agent_llm)

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    assert finished["status"] == "completed", finished.get("error")
    executed = _units(finished, "doc_tests.executed")
    assert [unit["kind"] for unit in executed] == ["document_qa_execution"]
    unit = executed[0]
    assert unit["status"] == "succeeded"
    # The pipeline persisted a content-free manifest, a proposal, and a receipt.
    assert unit["context_manifest"]["unit_id"] == unit["id"]
    assert unit["proposal_sidecar"]["unit_id"] == unit["id"]
    assert unit["receipt_sidecar"]["unit_id"] == unit["id"]
    assert (
        store.run_dir(ws, finished["id"]) / unit["receipt_sidecar"]["path"]
    ).is_file()
    # The turn was charged to this run's budget through the shared gateway.
    assert finished["usage"]["llm_turns"] == 1
    assert [call["tag"] for call in fake_agent_llm.calls] == ["agent:document_qa"]

    saved = doc_tests.load_test(ws, test["id"])
    stored = saved["items"][0]["qa_answers"][document["id"]]
    assert stored["answer"] == "The controller approved it."
    assert saved["items"][0]["state"] == "confirmed"


def test_auto_mode_applies_the_workers_exception_outcome(
    monkeypatch, fake_agent_llm
):
    ws, _document, test = _qa_workspace(monkeypatch, fake_agent_llm)
    fake_agent_llm.overrides["agent:document_qa"] = {
        "answer": "No, the supplied policy does not establish the required approval.",
        "outcome": "exception",
        "citations": [{"page": 1, "excerpt": "Approved by: the controller"}],
    }

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    assert finished["status"] == "completed", finished.get("error")
    saved = doc_tests.load_test(ws, test["id"])
    item = saved["items"][0]
    assert item["state"] == "exception"
    assert item["qa_answers"][_document["id"]]["outcome"] == "exception"


def test_auto_mode_settles_a_workers_manual_check_outcome(
    monkeypatch, fake_agent_llm
):
    ws, _document, test = _qa_workspace(monkeypatch, fake_agent_llm)
    fake_agent_llm.overrides["agent:document_qa"] = {
        "answer": "The evidence is inconclusive.",
        "outcome": "needs_manual_check",
        "citations": [{"page": 1, "excerpt": "Approved by: the controller"}],
    }

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    assert finished["status"] == "completed", finished.get("error")
    assert _units(finished, "doc_tests.executed")[0]["status"] == "succeeded"
    item = doc_tests.load_test(ws, test["id"])["items"][0]
    assert item["state"] == "manual_review"


def test_an_answered_qa_pair_is_not_re_billed_on_a_later_run(
    monkeypatch, fake_agent_llm
):
    ws, _document, test = _qa_workspace(monkeypatch, fake_agent_llm)
    wait_run(ws, _run(ws, test["id"])["id"])
    assert len(fake_agent_llm.calls) == 1

    second = wait_run(ws, _run(ws, test["id"])["id"])

    assert second["status"] == "completed", second.get("error")
    assert len(fake_agent_llm.calls) == 1
    assert _units(second, "doc_tests.executed") == []


def test_permission_mode_settles_a_cited_qa_answer_directly(
    monkeypatch, fake_agent_llm
):
    ws, _document, test = _qa_workspace(monkeypatch, fake_agent_llm)

    finished = wait_run(ws, _run(ws, test["id"], mode="permission")["id"])

    unit = _units(finished, "doc_tests.executed")[0]
    assert finished["status"] == "completed", finished.get("error")
    assert unit["status"] == "succeeded"
    saved = doc_tests.load_test(ws, test["id"])
    assert saved["items"][0]["state"] == "confirmed"


def test_forced_qa_execution_answers_the_pair_again(monkeypatch, fake_agent_llm):
    ws, _document, test = _qa_workspace(monkeypatch, fake_agent_llm)
    wait_run(ws, _run(ws, test["id"])["id"])

    forced = wait_run(ws, _run(ws, test["id"], generation_mode="force")["id"])

    assert [unit["kind"] for unit in _units(forced, "doc_tests.executed")] == [
        "document_qa_execution"
    ]
    assert len(fake_agent_llm.calls) == 2


@pytest.mark.parametrize("kind", ["attribute", "review"])
def test_attribute_and_review_execution_use_the_cited_model_pipeline(
    monkeypatch, fake_agent_llm, kind
):
    ws = _workspace(f"LLM {kind}")
    document = documents.add_document(ws, "evidence.txt", EVIDENCE)
    if kind == "attribute":
        test = doc_tests.build_attribute(
            ws,
            {"title": "Approval attribute", "document_ids": [document["id"]],
             "attributes": [{"name": "approval", "expected": "present"}]},
        )
    else:
        test = doc_tests.build_review(
            ws, {"title": "Invoice review", "document_id": document["id"]},
        )
    fake_agent_llm.overrides["agent:document_qa"] = {
        "answer": "The supplied evidence supports the requested assessment.",
        "outcome": "accepted",
        "citations": [{"page": 1, "excerpt": "Approved by: the controller"}],
    }

    finished = wait_run(ws, _run(ws, test["id"])["id"])

    unit = _units(finished, "doc_tests.executed")[0]
    assert unit["kind"] == "document_llm_execution"
    assert unit["status"] == "succeeded"
    saved = doc_tests.load_test(ws, test["id"])
    assert saved["items"][0]["llm_answers"][document["id"]]["answer"]
    assert saved["items"][0]["state"] == "confirmed"


# --------------------------------------------------------------------------- #
# One implementation, two graphs — and the deletion
# --------------------------------------------------------------------------- #
def test_both_graphs_bind_document_test_units_through_one_function():
    audit_source = inspect.getsource(audit_execution.AuditWorkflowExecution._bind_execution)
    standalone_source = inspect.getsource(
        doc_tests_execution.DocTestWorkflowExecution._bind_execution
    )

    assert "bind_document_test_unit" in audit_source
    assert "bind_document_test_unit" in standalone_source
    # The audit binder keeps only its datatest branch; every Document Test unit
    # kind is decided in the shared function.
    assert "run_document_test" not in audit_source
    assert "document_qa" not in audit_source


def test_both_graphs_expand_document_tests_through_one_function():
    from app.agent.capabilities import fieldwork as fieldwork_capabilities

    fieldwork_source = inspect.getsource(fieldwork_capabilities)
    assert "document_test_units" in fieldwork_source
    assert "document_qa_execution" not in fieldwork_source.replace(
        "document_test_units", ""
    )


def test_the_document_test_leaf_runner_and_engine_are_gone():
    import app.agent as agent_package

    with pytest.raises(ImportError):
        __import__("app.agent.doc_test_runner")
    assert not hasattr(agent_package, "doc_test_runner")
    assert not hasattr(store, "DOC_TEST_ENGINE")
    assert "doc_test" not in store.RUN_ENGINES
    assert "doc_test" not in store.PROTOCOL_ENGINE_BY_RUN_KIND
    with pytest.raises(Exception):
        store.new_run(_workspace("Retired kind"), "auto", kind="doc_test")


def test_dispatch_selects_the_document_test_composition_by_definition():
    ws = _workspace("Doc test dispatch")
    _document, test = _vouching(ws)
    run = store.new_command_run(ws, "auto", {"source": "chat", "text": "run it"})
    run["engine"] = store.WORKFLOW_ENGINE
    run["workflow"] = {"definition": doc_tests_workflow.WORKFLOW_ID}
    store.save_run(ws, run)

    scheduler = build_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )

    assert isinstance(
        scheduler.execution_adapter, doc_tests_execution.DocTestWorkflowExecution
    )
    assert scheduler.registry is capability_registries.DOC_TESTS_REGISTRY
