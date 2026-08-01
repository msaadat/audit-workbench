"""Focused tests for the ``fieldwork.executed`` migration (P7F.2/P7F.3).

P7F.2 covers the deterministic halves — local Polars data-test computation and
local document comparison — and P7F.3 the one model-backed unit kind, document
Q&A, which now runs through a registered worker on the injected gateway and a
registered executor that owns the guarded merge.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app import data_tests, doc_tests, document_context, documents, workspaces
from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    ContextResolver,
    document_qa_scope,
    supplied_size,
    total_supplied_size,
)
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.fieldwork import (
    DOCUMENT_QA_EXECUTOR,
    DocumentQaExecutorTarget,
    document_test_ref,
    run_data_test,
    run_document_test,
)
from app.agent.workers import WORKERS, WorkerContractError, WorkerRequest
from app.agent.workers.fieldwork import (
    DOCUMENT_QA_SYSTEM,
    DOCUMENT_QA_WORKER,
    validate_document_qa_proposal,
)
from app.workspace_transactions import parent_hashes

CAPABILITY_ID = "fieldwork.executed"


class _Gateway:
    """Minimal ModelGateway stub that records the prompt it was handed."""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, int]] = []

    def complete(self, system, user, activity, *, attempt=1, **_kwargs):
        self.calls.append((system, user, attempt))
        return self.responses[min(attempt, len(self.responses)) - 1]


def _rcm_row(ws, *, method="data_analytics"):
    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    return row["id"]


# --------------------------------------------------------------------------- #
# P7F.2: deterministic execution
# --------------------------------------------------------------------------- #
def test_run_data_test_commits_the_result_and_returns_its_immutable_ref(
    workspace_with_data,
):
    ws = workspace_with_data
    rcm_id = _rcm_row(ws)
    definition = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": rcm_id,
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )

    outcome = run_data_test(ws, definition["id"])

    committed = workspaces.load_workspace(ws.id).data_tests[0]
    assert committed["last_run"]
    assert outcome.status == "succeeded"
    assert outcome.error is None
    assert outcome.executed is True
    # The reference names the immutable result, not just the definition.
    assert outcome.artifact_ref == f"datatest:{definition['id']}:{committed['last_run']['id']}"


def test_run_document_test_completes_a_local_review_without_a_model(
    workspace_with_data,
):
    ws = workspace_with_data
    rcm_id = _rcm_row(ws, method="inspection")
    document = documents.add_document(ws, "Approval.txt", b"Approval was documented.")
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval review",
            "kind": "review",
            "rcm_id": rcm_id,
            "items": [
                {
                    "label": "Approval",
                    "document_ids": [document["id"]],
                    "summary": "Review the approval",
                }
            ],
        },
    )

    outcome = run_document_test(ws, test["id"], unit_id="unit-1", run_id="run-local")

    # A review-kind item is deterministically marked for auditor judgment.
    assert outcome.status == "succeeded"
    assert outcome.artifact_ref == document_test_ref(test["id"])
    assert doc_tests.load_test(ws, test["id"])["status"] == "completed"


def test_run_document_test_registers_the_blocked_unit_on_open_evidence_requests(
    workspace_with_data,
):
    ws = workspace_with_data
    rcm_id = _rcm_row(ws, method="inspection")
    test = doc_tests.create_test(
        ws,
        {
            "title": "Missing evidence",
            "kind": "review",
            "rcm_id": rcm_id,
            "items": [{"label": "Approval", "summary": "Review the approval"}],
        },
    )
    test["status"] = "blocked"
    test["scope_limitations"] = "The signed approval was never provided."
    doc_tests.save_test(ws, test)
    ws.evidence_requests.append(
        {
            "id": "ER-1",
            "rcm_id": rcm_id,
            "document_test_id": test["id"],
            "status": "open",
        }
    )
    ws.save()

    outcome = run_document_test(ws, test["id"], unit_id="unit-blocked", run_id="run-1")

    assert outcome.status == "blocked"
    assert outcome.error == "The signed approval was never provided."
    # Nothing ran, so the caller must not announce a workspace change.
    assert outcome.executed is False
    # The open request now points at the unit a later import would unblock.
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.evidence_requests[0]["blocked_unit_id"] == "unit-blocked"

    # Re-running the still-blocked unit records nothing, so it must not advance
    # the workspace revision.
    revision = reloaded.revision
    run_document_test(reloaded, test["id"], unit_id="unit-blocked", run_id="run-1")
    assert workspaces.load_workspace(ws.id).revision == revision


def test_run_document_test_refuses_a_model_backed_qa_test(workspace_with_data):
    ws = workspace_with_data
    rcm_id = _rcm_row(ws, method="inquiry")
    document = documents.add_document(ws, "Approval.txt", b"Approval was documented.")
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval Q&A",
            "kind": "qa",
            "rcm_id": rcm_id,
            "items": [
                {
                    "label": "Was approval documented?",
                    "question": "Was approval documented?",
                    "document_ids": [document["id"]],
                }
            ],
        },
    )

    # A Q&A test expands into document_qa_execution units, which go through the
    # gateway. Answering one here would be an unbudgeted provider call.
    with pytest.raises(workspaces.WorkspaceError):
        run_document_test(ws, test["id"], unit_id="unit-qa", run_id="run-1")


# --------------------------------------------------------------------------- #
# P7F.3: document Q&A worker
# --------------------------------------------------------------------------- #
def _qa_workspace(*, pages: tuple[int, ...] | None = (1,)):
    ws = workspaces.create_workspace("Document Q&A execution")
    rcm_id = _rcm_row(ws, method="inquiry")
    document = documents.add_document(
        ws,
        "Approval.txt",
        b"The purchase order was approved by the controller on 3 March.",
    )
    item = {
        "label": "Who approved the order?",
        "question": "Who approved the purchase order?",
        "document_ids": [document["id"]],
    }
    if pages is not None:
        item["pages"] = list(pages)
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval Q&A",
            "kind": "qa",
            "rcm_id": rcm_id,
            "items": [item],
        },
    )
    return ws, test, test["items"][0]["id"], document["id"]


def _qa_bundle(ws, test, item_id, document_id):
    unit = {"id": f"document_qa_execution:{test['id']}:{item_id}:{document_id}"}
    capability = type(
        "_Capability",
        (),
        {"id": CAPABILITY_ID, "context": "fieldwork.document_qa"},
    )()
    _manifest, bundle = ContextResolver().resolve(
        ws,
        capability,
        unit,
        document_qa_scope(ws, test["id"], item_id, document_id),
    )
    return unit, bundle


def test_document_qa_worker_answers_only_from_the_supplied_pages():
    ws, test, item_id, document_id = _qa_workspace()
    unit, bundle = _qa_bundle(ws, test, item_id, document_id)
    gateway = _Gateway(
        '{"answer": "The controller approved it.", "outcome": "accepted", "citations": '
        '[{"page": 1, "excerpt": "approved by the controller"}]}'
    )

    result = WORKERS.execute(
        WorkerRequest(
            worker_id="fieldwork.document_qa",
            capability_id=CAPABILITY_ID,
            unit_id=unit["id"],
            context=bundle,
            activity={"artifact_refs": [document_test_ref(test["id"])]},
        ),
        gateway,
    )

    system, user, _attempt = gateway.calls[0]
    assert system == DOCUMENT_QA_SYSTEM
    assert "Who approved the purchase order?" in user
    assert "--- Page 1 ---" in user
    assert result.proposal["answer"] == "The controller approved it."
    assert result.proposal["outcome"] == "accepted"
    assert [dict(item) for item in result.proposal["citations"]] == [
        {"page": 1, "excerpt": "approved by the controller"}
    ]


def test_document_qa_scope_coalesces_unscoped_excerpts_by_page(monkeypatch):
    ws, test, item_id, document_id = _qa_workspace(pages=None)
    monkeypatch.setattr(
        document_context,
        "get_document_context",
        lambda *_args, **_kwargs: {
            "source_sha1": "source-sha1",
            "citations": [
                {"page": 1, "excerpt": "The controller approved the order."},
                {"page": 1, "excerpt": "Approval was retained in the file."},
                {"page": 2, "excerpt": "The CFO reviews exceptions."},
            ],
        },
    )

    _unit, bundle = _qa_bundle(ws, test, item_id, document_id)
    pages = [item for item in bundle.items if item.source_id == "document_pages"]

    assert [item.source_ref for item in pages] == [
        f"document:{document_id}:page:00001",
        f"document:{document_id}:page:00002",
    ]
    assert pages[0].content == {
        "page": 1,
        "text": (
            "The controller approved the order.\n\n"
            "Approval was retained in the file."
        ),
    }


def test_document_qa_worker_rejects_duplicate_pages_in_a_bundle():
    contents = (
        {"page": 1, "text": "First excerpt."},
        {"page": 1, "text": "Second excerpt."},
    )
    items = tuple(
        ContextBundleItem(
            source_id="document_pages",
            source_ref=f"document:approval:page:00001:excerpt:{index}",
            representation=ContextRepresentation("excerpt"),
            content=content,
            supplied_size=supplied_size(content),
        )
        for index, content in enumerate(contents, start=1)
    )
    bundle = ContextBundle(
        capability_id=CAPABILITY_ID,
        unit_id="document_qa_execution:duplicate-pages",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )
    request = WorkerRequest(
        worker_id="fieldwork.document_qa",
        capability_id=CAPABILITY_ID,
        unit_id=bundle.unit_id,
        context=bundle,
    )

    with pytest.raises(WorkerContractError, match="same page more than once"):
        validate_document_qa_proposal({"answer": "", "citations": []}, request)


def test_document_qa_worker_drops_a_citation_to_a_page_it_never_saw():
    ws, test, item_id, document_id = _qa_workspace()
    unit, bundle = _qa_bundle(ws, test, item_id, document_id)
    request = WorkerRequest(
        worker_id="fieldwork.document_qa",
        capability_id=CAPABILITY_ID,
        unit_id=unit["id"],
        context=bundle,
    )

    proposal = validate_document_qa_proposal(
        {
            "answer": "The controller approved it.",
            "outcome": "accepted",
            "citations": [
                {"page": 99, "excerpt": "fabricated"},
                {"page": 1, "excerpt": "not verbatim in the page at all"},
            ],
        },
        request,
    )

    # The unsupplied page is gone, and the non-verbatim excerpt is replaced by
    # the exact supplied text so the executor can anchor it.
    assert [citation["page"] for citation in proposal["citations"]] == [1]
    assert proposal["citations"][0]["excerpt"] in (
        "The purchase order was approved by the controller on 3 March."
    )


def test_document_qa_worker_repairs_one_invalid_response_then_succeeds():
    ws, test, item_id, document_id = _qa_workspace()
    unit, bundle = _qa_bundle(ws, test, item_id, document_id)
    gateway = _Gateway(
        "Sorry, no JSON here.",
        '{"answer": "The controller approved it.", '
        '"outcome": "needs_manual_check", "citations": []}',
    )

    result = WORKERS.execute(
        WorkerRequest(
            worker_id="fieldwork.document_qa",
            capability_id=CAPABILITY_ID,
            unit_id=unit["id"],
            context=bundle,
        ),
        gateway,
    )

    assert result.attempts == 2
    assert result.repaired is True
    assert "could not be used" in gateway.calls[1][1]


def test_the_document_qa_worker_module_takes_no_workspace_dependency():
    source = pathlib.Path(inspect.getsourcefile(DOCUMENT_QA_WORKER.implementation))
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported = {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert not imported & {
        "Workspace",
        "mutate",
        "parent_hashes",
        "ContextResolver",
        "WorkflowRunner",
        "store",
    }


# --------------------------------------------------------------------------- #
# P7F.3: document Q&A executor
# --------------------------------------------------------------------------- #
def _qa_request(ws, test, item_id, document_id, *, answer="The controller approved it."):
    return ExecutorRequest(
        executor_id="fieldwork.document_qa",
        capability_id=CAPABILITY_ID,
        unit_id=f"document_qa_execution:{test['id']}:{item_id}:{document_id}",
        proposal={
            "answer": answer,
            "conclusion": "The cited evidence supports the requested assessment.",
            "control_conclusion": "effective",
            "outcome": "accepted",
            "citations": [{"page": 1, "excerpt": "approved by the controller"}],
        },
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [document_test_ref(test["id"])]),
        activity={"artifact_refs": [document_test_ref(test["id"])]},
    )


def test_document_qa_executor_commits_the_answer_with_derived_evidence_anchors():
    ws, test, item_id, document_id = _qa_workspace()
    request = _qa_request(ws, test, item_id, document_id)
    target = DocumentQaExecutorTarget(ws, "run-qa", test["id"], item_id, document_id)

    receipt = EXECUTORS.execute(request, target)

    committed = doc_tests.load_test(target.workspace, test["id"])
    item = committed["items"][0]
    assert item["qa_answers"][document_id]["answer"] == "The controller approved it."
    assert item["qa_answers"][document_id]["conclusion"] == (
        "The cited evidence supports the requested assessment."
    )
    assert committed["conclusion"] == "The cited evidence supports the requested assessment."
    assert committed["control_conclusion"] == "effective"
    assert item["qa_answers"][document_id]["outcome"] == "accepted"
    assert item["state"] == "confirmed"
    # The anchor is built from the document at commit time, so it carries the
    # real source hash rather than anything the proposal could claim.
    anchor = item["qa_answers"][document_id]["citations"][0]
    assert anchor["source_id"] == document_id
    assert anchor["source_sha1"] == next(
        entry["sha1"]
        for entry in target.workspace.documents
        if entry["id"] == document_id
    )
    assert receipt.artifact_refs == (document_test_ref(test["id"]),)
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [document_test_ref(test["id"])]
    )
    assert receipt.output["answer_ref"] == (
        f"doctest:{test['id']}:item:{item_id}:document:{document_id}"
    )


def test_document_qa_executor_rejects_a_document_the_item_does_not_attach():
    ws, test, item_id, _document_id = _qa_workspace()
    other = documents.add_document(ws, "Unrelated.txt", b"Unrelated content.")
    request = _qa_request(ws, test, item_id, other["id"])
    target = DocumentQaExecutorTarget(
        ws, "run-qa-wrong", test["id"], item_id, other["id"]
    )

    with pytest.raises(workspaces.WorkspaceError):
        DOCUMENT_QA_EXECUTOR.implementation(request, target)


def test_document_qa_executor_reconciles_an_interrupted_commit():
    ws, test, item_id, document_id = _qa_workspace()
    request = _qa_request(ws, test, item_id, document_id)
    target = DocumentQaExecutorTarget(
        ws, "run-qa-reconcile", test["id"], item_id, document_id
    )

    # Before the commit the guarded Document Test summary is untouched, which
    # proves the commit never landed.
    assert (
        DOCUMENT_QA_EXECUTOR.reconciler(request, target).disposition == "not_applied"
    )

    DOCUMENT_QA_EXECUTOR.implementation(request, target)
    recovered = DOCUMENT_QA_EXECUTOR.reconciler(request, target)

    assert recovered.disposition == "already_applied"
    assert recovered.result.output["action"] == "answered"


def test_document_qa_executor_reports_a_later_edit_as_a_conflict():
    ws, test, item_id, document_id = _qa_workspace()
    request = _qa_request(ws, test, item_id, document_id)
    target = DocumentQaExecutorTarget(
        ws, "run-qa-conflict", test["id"], item_id, document_id
    )

    # A different answer landed for the same item/document after this proposal
    # was accepted: the parent moved and the durable answer is not this one.
    current = workspaces.load_workspace(ws.id)
    doc_tests.commit_qa_answer(
        current,
        test["id"],
        item_id,
        document_id,
        {"answer": "An auditor wrote this instead.", "citations": []},
    )

    outcome = DOCUMENT_QA_EXECUTOR.reconciler(request, target)

    assert outcome.disposition == "conflict"
    assert "changed before" in outcome.reason
