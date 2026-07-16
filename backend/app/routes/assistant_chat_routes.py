"""Durable unified assistant chat API."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, HTTPException, Query, Response

from .. import assistant_chats, workspaces

router = APIRouter(prefix="/api", tags=["assistant-chats"])


@router.get("/assistant/capabilities")
async def chat_capabilities():
    return assistant_chats.capabilities()


@router.get("/workspaces/{workspace_id}/assistant/chats")
async def list_chats(workspace_id: str, limit: int = Query(50, ge=1, le=100), cursor: int = Query(0, ge=0)):
    return assistant_chats.list_chats(workspaces.load_workspace(workspace_id), limit=limit, cursor=cursor)


@router.post("/workspaces/{workspace_id}/assistant/chats")
async def create_chat(workspace_id: str):
    return assistant_chats.create_chat(workspaces.load_workspace(workspace_id))


@router.get("/workspaces/{workspace_id}/assistant/chats/{chat_id}")
async def get_chat(workspace_id: str, chat_id: str):
    return assistant_chats.get_chat(workspaces.load_workspace(workspace_id), chat_id)


@router.patch("/workspaces/{workspace_id}/assistant/chats/{chat_id}")
async def patch_chat(workspace_id: str, chat_id: str, payload: dict = Body(...)):
    return assistant_chats.update_chat(workspaces.load_workspace(workspace_id), chat_id, payload)


@router.delete("/workspaces/{workspace_id}/assistant/chats/{chat_id}", status_code=204)
async def remove_chat(workspace_id: str, chat_id: str):
    assistant_chats.delete_chat(workspaces.load_workspace(workspace_id), chat_id)
    return Response(status_code=204)


@router.post("/workspaces/{workspace_id}/assistant/chats/{chat_id}/messages")
async def send_message(workspace_id: str, chat_id: str, payload: dict = Body(...)):
    return await asyncio.to_thread(
        assistant_chats.send_message, workspaces.load_workspace(workspace_id), chat_id, payload
    )


@router.patch("/workspaces/{workspace_id}/assistant/chats/{chat_id}/artifacts/{artifact_id}")
async def patch_artifact(workspace_id: str, chat_id: str, artifact_id: str, payload: dict = Body(...)):
    try:
        return assistant_chats.update_artifact(
            workspaces.load_workspace(workspace_id), chat_id, artifact_id,
            payload.get("changes") or {}, payload.get("revision"),
        )
    except assistant_chats.RevisionConflict as error:
        raise HTTPException(409, detail=str(error)) from error


@router.post("/workspaces/{workspace_id}/assistant/chats/{chat_id}/artifacts/{artifact_id}/rerun")
async def rerun_artifact(workspace_id: str, chat_id: str, artifact_id: str, payload: dict = Body(...)):
    try:
        return await asyncio.to_thread(
            assistant_chats.rerun_artifact, workspaces.load_workspace(workspace_id),
            chat_id, artifact_id, payload.get("revision"),
        )
    except assistant_chats.RevisionConflict as error:
        raise HTTPException(409, detail=str(error)) from error
