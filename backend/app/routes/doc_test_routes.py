"""Document-test, deterministic matching, and working-paper endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, HTTPException

from .. import cycle_vouching, doc_tests, working_papers, workspaces
from ..workspaces import WorkspaceError
from ..agent import runner
from ..agent.workflows import doc_tests as doc_tests_workflow

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["document tests"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


@router.get("/doc-tests")
async def list_document_tests(workspace_id: str):
    return {"items": doc_tests.list_tests(_ws(workspace_id))}


@router.get("/doc-tests/summary")
async def get_document_tests_summary(workspace_id: str):
    """Every worklist item across the engagement, bucketed for triage."""
    return doc_tests.summary_payload(_ws(workspace_id))


@router.get("/doc-tests/meta")
async def get_document_test_meta(workspace_id: str):
    """Closed vocabularies the create form binds its pickers to."""
    _ws(workspace_id)
    return doc_tests.meta_payload()


@router.post("/doc-tests/build/vouching")
async def build_vouching_test(workspace_id: str, payload: dict = Body(...)):
    return await asyncio.to_thread(doc_tests.build_vouching, _ws(workspace_id), payload)


@router.post("/doc-tests/cycle-vouch/candidates")
async def cycle_vouch_candidates(workspace_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    rcm_id = str(payload.get("rcm_id") or "")
    row = next((item for item in ws.rcm if item.get("id") == rcm_id), None)
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    return await asyncio.to_thread(
        cycle_vouching.transaction_evidence_manifest,
        ws,
        row.get("control_attributes") or [],
    )


@router.post("/doc-tests/build/cycle-vouch")
async def build_cycle_vouch_test(workspace_id: str, payload: dict = Body(...)):
    try:
        return await asyncio.to_thread(
            cycle_vouching.build_cycle_vouch_test, _ws(workspace_id), payload
        )
    except cycle_vouching.SelectionConfirmationRequired as required:
        # Not an error: the eligible reach exceeds the item cap, so the caller
        # confirms or adjusts a deterministic sample. Nothing was persisted.
        return {
            "status": "selection_confirmation",
            "selection_confirmation": required.proposal,
        }
    except cycle_vouching.CycleSchemaError as error:
        raise WorkspaceError(str(error)) from error


@router.post("/doc-tests/prepare-evidence-aware")
async def prepare_evidence_aware_test(workspace_id: str, payload: dict = Body(...)):
    return await asyncio.to_thread(
        doc_tests.prepare_evidence_aware_vouching, _ws(workspace_id), payload
    )


@router.post("/doc-tests/build/attribute")
async def build_attribute_test(workspace_id: str, payload: dict = Body(...)):
    return doc_tests.build_attribute(_ws(workspace_id), payload)


@router.post("/doc-tests/build/review")
async def build_review_test(workspace_id: str, payload: dict = Body(...)):
    return doc_tests.build_review(_ws(workspace_id), payload)


@router.post("/doc-tests/build/qa")
async def build_qa_test(workspace_id: str, payload: dict = Body(...)):
    return doc_tests.build_qa(_ws(workspace_id), payload)


@router.post("/doc-tests")
async def create_document_test(workspace_id: str, payload: dict = Body(...)):
    return doc_tests.create_test(_ws(workspace_id), payload)


@router.get("/doc-tests/{test_id}")
async def get_document_test(workspace_id: str, test_id: str):
    workspace = _ws(workspace_id)
    test = doc_tests.load_test(workspace, test_id)
    evidence_requests = [
        item for item in workspace.evidence_requests
        if item.get("document_test_id") == test_id
    ]
    return {
        **test,
        "rollup": doc_tests.result_rollup(test),
        "evidence_requests": evidence_requests,
    }


@router.get("/doc-tests/{test_id}/grid")
async def get_cycle_vouch_grid(
    workspace_id: str,
    test_id: str,
    offset: int = 0,
    limit: int = 100,
):
    """Project the current Cycle vouch result grid without executing or writing.

    Results that cannot be attributed to the current definition are flagged in
    the payload (``stale_definition``, and ``attribution_stale`` per cell)
    rather than refused: the grid is the view an auditor repairs the drift from.
    """

    test = doc_tests.load_test(_ws(workspace_id), test_id)
    try:
        return cycle_vouching.grid_projection(test, offset=offset, limit=limit)
    except cycle_vouching.CycleSchemaError as error:
        raise WorkspaceError(str(error)) from error


@router.post("/doc-tests/{test_id}/assertions")
async def append_cycle_assertion(
    workspace_id: str, test_id: str, payload: dict = Body(...)
):
    """Append or change one typed Cycle-vouch assertion definition."""

    unknown = set(payload) - {"expected_test_sha1", "assertion", "placement"}
    if unknown:
        raise WorkspaceError(
            f"Unknown assertion-mutation field '{sorted(unknown)[0]}'."
        )
    assertion = payload.get("assertion")
    if not isinstance(assertion, dict):
        raise WorkspaceError("assertion must be an object.")
    outcome = await asyncio.to_thread(
        doc_tests.append_cycle_assertions,
        _ws(workspace_id),
        test_id,
        expected_test_sha1=str(payload.get("expected_test_sha1") or ""),
        assertions=[assertion],
        placement=payload.get("placement"),
        actor="auditor",
    )
    test = outcome["test"]
    return {
        "test_id": str(test.get("id") or test_id),
        "test_sha1": str(test.get("sha1") or ""),
        "definition_sha1": cycle_vouching.cycle_definition_sha1(test),
        "assertion_keys": [
            str(assertion.get("key") or "")
            for assertion in (test.get("definition") or {}).get("assertions") or []
        ],
        "mutation": outcome["mutation"],
    }


@router.patch("/doc-tests/{test_id}")
async def patch_document_test(workspace_id: str, test_id: str, payload: dict = Body(...)):
    return doc_tests.update_test(_ws(workspace_id), test_id, payload)


@router.delete("/doc-tests/{test_id}")
async def delete_document_test(workspace_id: str, test_id: str):
    doc_tests.remove_test(_ws(workspace_id), test_id)
    return {"ok": True}


@router.post("/doc-tests/{test_id}/items/{item_id}/documents")
async def attach_document(workspace_id: str, test_id: str, item_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    document_id = str(payload.get("document_id") or "")
    result = doc_tests.attach_document(ws, test_id, item_id, document_id)
    runner.notify_evidence_available(
        ws, document_ids=[document_id], test_ids=[test_id], reason="document_attached"
    )
    return result


@router.delete("/doc-tests/{test_id}/items/{item_id}/documents/{document_id}")
async def detach_document(workspace_id: str, test_id: str, item_id: str, document_id: str):
    return doc_tests.detach_document(_ws(workspace_id), test_id, item_id, document_id)


@router.patch("/doc-tests/{test_id}/items/{item_id}/comparisons")
async def patch_comparisons(workspace_id: str, test_id: str, item_id: str, payload: dict = Body(...)):
    return doc_tests.update_comparisons(_ws(workspace_id), test_id, item_id, payload.get("checks") or [])


@router.patch("/doc-tests/{test_id}/items/{item_id}")
async def patch_document_test_item(workspace_id: str, test_id: str, item_id: str, payload: dict = Body(...)):
    return doc_tests.update_item(_ws(workspace_id), test_id, item_id, payload)


@router.post("/doc-tests/dispositions")
async def post_document_test_dispositions(workspace_id: str, payload: dict = Body(...)):
    """Record one auditor call across a selection spanning several tests."""

    return doc_tests.update_dispositions(
        _ws(workspace_id), list(payload.get("dispositions") or [])
    )


def _execution_command(ws, test_id: str, mode: str, context: dict) -> dict:
    """Start the declared document-test workflow for one named Document Test.

    Standalone execution and the audit lifecycle request the same units through
    the same binder: this endpoint names the test as a workflow target rather
    than driving a separate runner.
    """
    test = doc_tests.load_test(ws, test_id)
    return runner.start_command_run(
        ws,
        mode if mode in {"auto", "permission"} else "auto",
        {
            "source": "tab_button",
            "text": f"Run document test {test.get('title') or test_id}.",
            "goal_template": "document_test_execution",
            "requested_outcomes": list(doc_tests_workflow.FULL_DOC_TEST_OUTCOMES),
            "target_refs": [f"doctest:{test_id}"],
            "generation_mode": "reuse_existing",
        },
        context={**dict(context or {}), "test_id": test_id},
    )


@router.post("/doc-tests/{test_id}/run")
async def run_document_test(workspace_id: str, test_id: str, payload: dict = Body(default={})):
    ws = _ws(workspace_id)
    try:
        return await asyncio.to_thread(
            _execution_command,
            ws,
            test_id,
            str(payload.get("mode") or "auto"),
            dict(payload.get("context") or {}),
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.post("/procedures/{procedure_id}/draft-results")
async def draft_procedure_results(workspace_id: str, procedure_id: str):
    raise WorkspaceError(
        "Legacy procedure working papers are read-only; generate the working paper from its RCM instead."
    )


@router.get("/procedures/{procedure_id}/working-paper")
async def get_working_paper(workspace_id: str, procedure_id: str):
    return working_papers.render(_ws(workspace_id), procedure_id)


@router.post("/matching/compare")
async def compare_values(payload: dict = Body(...)):
    return doc_tests.compare_values(
        payload.get("expected"), payload.get("found"),
        str(payload.get("method") or "normalized"), payload.get("tolerance"),
    )


@router.get("/evidence-requests")
async def list_evidence_requests(workspace_id: str):
    return {"items": _ws(workspace_id).evidence_requests}


@router.patch("/evidence-requests/{request_id}")
async def patch_evidence_request(
    workspace_id: str, request_id: str, payload: dict = Body(...)
):
    ws = _ws(workspace_id)
    result = doc_tests.update_evidence_request(
        ws,
        request_id,
        status=str(payload.get("status") or ""),
        note=str(payload.get("auditor_note") or ""),
    )
    if result.get("status") == "received":
        runner.notify_evidence_available(
            ws, request_ids=[request_id], reason="evidence_request_received"
        )
    return result
