"""Workspace CRUD: create/list/delete workspaces, upload tables, define joins."""

from __future__ import annotations

from fastapi import APIRouter, Body, File, UploadFile

from .. import workspaces

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.get("")
async def list_workspaces():
    return workspaces.list_workspaces()


@router.post("")
async def create_workspace(payload: dict = Body(...)):
    doc_llm_optin = payload.get("doc_llm_optin", False)
    if not isinstance(doc_llm_optin, bool):
        raise workspaces.WorkspaceError("Document AI choice must be true or false.")
    ws = workspaces.create_workspace(
        payload.get("name", ""),
        payload.get("description", ""),
        doc_llm_optin=doc_llm_optin,
    )
    return ws.summary()


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str):
    return workspaces.load_workspace(workspace_id).summary()


@router.delete("/{workspace_id}")
async def delete_workspace(workspace_id: str):
    workspaces.delete_workspace(workspace_id)
    return {"ok": True}


@router.post("/{workspace_id}/tables")
async def upload_tables(workspace_id: str, files: list[UploadFile] = File(...)):
    ws = workspaces.load_workspace(workspace_id)
    added = []
    for file in files:
        content = await file.read()
        added.append(ws.add_table(file.filename or "upload.csv", content))
    return {"added": added, "workspace": ws.summary()}


@router.put("/{workspace_id}/tables/{table_name}")
async def replace_table(
    workspace_id: str, table_name: str, file: UploadFile = File(...)
):
    ws = workspaces.load_workspace(workspace_id)
    content = await file.read()
    result = ws.replace_table(table_name, file.filename or "upload.csv", content)
    return {"replaced": result, "workspace": ws.summary()}


@router.patch("/{workspace_id}/tables/{table_name}")
async def rename_table(workspace_id: str, table_name: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    result = ws.rename_table(table_name, payload.get("name", ""))
    return {"renamed": result, "workspace": ws.summary()}


@router.delete("/{workspace_id}/tables/{table_name}")
async def delete_table(workspace_id: str, table_name: str):
    ws = workspaces.load_workspace(workspace_id)
    ws.remove_table(table_name)
    return ws.summary()


@router.post("/{workspace_id}/joins")
async def add_join(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    ws.add_join(payload)
    return ws.summary()


@router.delete("/{workspace_id}/joins/{join_name}")
async def delete_join(workspace_id: str, join_name: str):
    ws = workspaces.load_workspace(workspace_id)
    ws.remove_join(join_name)
    return ws.summary()
