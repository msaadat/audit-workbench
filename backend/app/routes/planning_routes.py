"""Planning, template, RCM, and audit-program endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import doc_tests, findings, templates_store, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["planning"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


@router.get("/templates/{name}")
async def get_template(workspace_id: str, name: str):
    return templates_store.get_template(_ws(workspace_id), name)


@router.put("/templates/{name}")
async def put_template(workspace_id: str, name: str, payload: dict = Body(...)):
    return templates_store.put_template(
        _ws(workspace_id), name, payload.get("markdown"), bool(payload.get("reset"))
    )


@router.get("/planning")
async def get_planning(workspace_id: str):
    ws = _ws(workspace_id)
    return {
        "planning": ws.planning, "rcm": ws.rcm, "procedures": ws.work_program,
        "document_tests": doc_tests.list_tests(ws),
        "findings": ws.findings,
        "finding_rollups": findings.rollups(ws),
    }


@router.patch("/planning")
async def patch_planning(workspace_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).update_planning(payload)


@router.get("/rcm")
async def list_rcm(workspace_id: str):
    return {"items": _ws(workspace_id).rcm}


@router.post("/rcm")
async def add_rcm(workspace_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).add_rcm(payload)


@router.patch("/rcm/{row_id}")
async def patch_rcm(workspace_id: str, row_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).update_rcm(row_id, payload)


@router.delete("/rcm/{row_id}")
async def delete_rcm(workspace_id: str, row_id: str):
    ws = _ws(workspace_id)
    ws.remove_rcm(row_id)
    return {"ok": True}


@router.get("/procedures")
async def list_procedures(workspace_id: str):
    return {"items": _ws(workspace_id).work_program}


@router.post("/procedures")
async def add_procedure(workspace_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).add_procedure(payload)


@router.patch("/procedures/{procedure_id}")
async def patch_procedure(workspace_id: str, procedure_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).update_procedure(procedure_id, payload)


@router.delete("/procedures/{procedure_id}")
async def delete_procedure(workspace_id: str, procedure_id: str):
    ws = _ws(workspace_id)
    ws.remove_procedure(procedure_id)
    return {"ok": True}
