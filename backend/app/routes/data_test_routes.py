"""Durable exploratory or RCM-linked Data Test CRUD and execution."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body

from .. import data_test_redundancy, data_tests, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["data tests"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


@router.get("/data-tests")
def list_data_tests(workspace_id: str):
    return {"items": data_tests.list_payload(_ws(workspace_id))}


@router.post("/data-tests")
def create_data_test(workspace_id: str, payload: dict = Body(...)):
    return data_tests.create(_ws(workspace_id), payload)


# Declared ahead of ``/data-tests/{data_test_id}``: a literal segment that
# arrives after the path-parameter route is captured by it instead.
@router.get("/data-tests/redundancy")
def get_data_test_redundancy(workspace_id: str):
    """Which tests flag the same records as which others. Read-only."""

    return data_test_redundancy.scan(_ws(workspace_id))


@router.post("/data-tests/redundancy")
def post_data_test_redundancy(workspace_id: str):
    """Re-run the sweep and store the marks.

    Batch runs sweep on their own; this is for a workspace whose tests were run
    before the detector existed, or run one at a time.
    """

    return data_test_redundancy.annotate(_ws(workspace_id), persist=True)


@router.get("/data-tests/{data_test_id}")
def get_data_test(workspace_id: str, data_test_id: str):
    ws = _ws(workspace_id)
    return next(
        (item for item in data_tests.list_payload(ws) if item.get("id") == data_test_id),
        data_tests._record(ws, data_test_id),
    )


@router.patch("/data-tests/{data_test_id}")
def patch_data_test(
    workspace_id: str, data_test_id: str, payload: dict = Body(...)
):
    return data_tests.update(_ws(workspace_id), data_test_id, payload)


@router.delete("/data-tests/{data_test_id}")
def delete_data_test(workspace_id: str, data_test_id: str):
    data_tests.remove(_ws(workspace_id), data_test_id)
    return {"ok": True}


@router.post("/data-tests/{data_test_id}/exception-dispositions")
def post_data_test_exception_disposition(
    workspace_id: str, data_test_id: str, payload: dict = Body(...)
):
    """Rule on one group of this test's exceptions."""

    return data_tests.record_exception_disposition(
        _ws(workspace_id),
        data_test_id,
        str(payload.get("key") or ""),
        str(payload.get("state") or "pending"),
        note=str(payload.get("note") or ""),
    )


@router.post("/data-tests/{data_test_id}/semantic-review")
def post_data_test_semantic_review(
    workspace_id: str, data_test_id: str, payload: dict = Body(...)
):
    """Record that the run's semantic issues were read and judged survivable."""

    return data_tests.record_semantic_review(
        _ws(workspace_id), data_test_id, str(payload.get("note") or "")
    )


@router.post("/data-tests/{data_test_id}/run")
async def run_data_test(workspace_id: str, data_test_id: str):
    return await asyncio.to_thread(data_tests.run, _ws(workspace_id), data_test_id)


@router.post("/data-tests/run-all")
async def run_all_data_tests(workspace_id: str, payload: dict = Body(default={})):
    return await asyncio.to_thread(
        data_tests.run_all, _ws(workspace_id), test_ids=payload.get("test_ids")
    )


@router.post("/data-tests/run-all-rcm")
async def run_all_rcm_data_tests(workspace_id: str, payload: dict = Body(default={})):
    # `test_ids` narrows the batch to a caller-chosen subset, which is how the
    # RCM status bar offers "run the four that have not run" without re-running
    # the thirty-five that have. Ids outside the RCM-linked set are ignored
    # rather than rejected: the caller's view of the workspace can be a moment
    # behind, and a stale id is not a reason to run nothing.
    return await asyncio.to_thread(
        data_tests.run_all_rcm_linked,
        _ws(workspace_id),
        test_ids=payload.get("test_ids"),
    )


@router.get("/data-tests/{data_test_id}/runs/{run_id}")
def get_data_test_result(workspace_id: str, data_test_id: str, run_id: str):
    return data_tests.load_result(_ws(workspace_id), data_test_id, run_id)

