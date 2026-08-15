"""Durable exploratory or RCM-linked Data Test CRUD and execution."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body

from .. import data_tests, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["data tests"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


@router.get("/data-tests")
async def list_data_tests(workspace_id: str):
    return {"items": data_tests.list_payload(_ws(workspace_id))}


@router.post("/data-tests")
async def create_data_test(workspace_id: str, payload: dict = Body(...)):
    return data_tests.create(_ws(workspace_id), payload)


@router.get("/data-tests/{data_test_id}")
async def get_data_test(workspace_id: str, data_test_id: str):
    ws = _ws(workspace_id)
    return next(
        (item for item in data_tests.list_payload(ws) if item.get("id") == data_test_id),
        data_tests._record(ws, data_test_id),
    )


@router.patch("/data-tests/{data_test_id}")
async def patch_data_test(
    workspace_id: str, data_test_id: str, payload: dict = Body(...)
):
    return data_tests.update(_ws(workspace_id), data_test_id, payload)


@router.delete("/data-tests/{data_test_id}")
async def delete_data_test(workspace_id: str, data_test_id: str):
    data_tests.remove(_ws(workspace_id), data_test_id)
    return {"ok": True}


@router.post("/data-tests/{data_test_id}/exception-dispositions")
async def post_data_test_exception_disposition(
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
async def post_data_test_semantic_review(
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
async def get_data_test_result(workspace_id: str, data_test_id: str, run_id: str):
    return data_tests.load_result(_ws(workspace_id), data_test_id, run_id)


@router.post("/data-tests/{data_test_id}/pin")
async def pin_data_test(
    workspace_id: str, data_test_id: str, payload: dict = Body(default={})
):
    ws = _ws(workspace_id)
    item = data_tests._record(ws, data_test_id)
    if not item.get("last_run"):
        raise workspaces.WorkspaceError("Run the Data Test before pinning it.")
    kind = {"polars": "python", "analytics": "analytics", "validation": "validation"}[
        item["engine"]
    ]
    spec = dict(item["spec"])
    if kind == "analytics":
        spec = {"test": spec["test_id"], "params": spec.get("params") or {}}
    elif kind == "python":
        spec = {"code": data_tests.spec_as_python_code(item["spec"])}
    tile = ws.add_tile(
        {
            "kind": kind,
            "table": item["table_refs"][0] if item["table_refs"] else None,
            "title": str(payload.get("title") or item["title"]),
            "note": str(payload.get("note") or ""),
            "spec": spec,
            "viz": dict(payload.get("viz") or {"type": "table"}),
            "data_test_id": item["id"],
            "rcm_id": item["rcm_id"],
            "result_ref": f"datatest:{item['id']}:{item['last_run']['id']}",
        }
    )
    return tile
