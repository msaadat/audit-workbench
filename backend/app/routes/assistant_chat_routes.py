"""Durable unified assistant chat API."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from .. import assistant_chats, workspaces

router = APIRouter(prefix="/api", tags=["assistant-chats"])


@router.get("/assistant/capabilities")
def chat_capabilities():
    return assistant_chats.capabilities()


@router.get("/workspaces/{workspace_id}/assistant/chats")
def list_chats(workspace_id: str, limit: int = Query(50, ge=1, le=100), cursor: int = Query(0, ge=0)):
    return assistant_chats.list_chats(workspaces.load_workspace(workspace_id), limit=limit, cursor=cursor)


@router.post("/workspaces/{workspace_id}/assistant/chats")
def create_chat(workspace_id: str):
    return assistant_chats.create_chat(workspaces.load_workspace(workspace_id))


@router.get("/workspaces/{workspace_id}/assistant/chats/{chat_id}")
def get_chat(
    workspace_id: str,
    chat_id: str,
    include_suggestions: bool = Query(True),
):
    return assistant_chats.get_chat(
        workspaces.load_workspace(workspace_id),
        chat_id,
        include_suggestions=include_suggestions,
    )


@router.patch("/workspaces/{workspace_id}/assistant/chats/{chat_id}")
def patch_chat(workspace_id: str, chat_id: str, payload: dict = Body(...)):
    return assistant_chats.update_chat(workspaces.load_workspace(workspace_id), chat_id, payload)


@router.delete("/workspaces/{workspace_id}/assistant/chats/{chat_id}", status_code=204)
def remove_chat(workspace_id: str, chat_id: str):
    assistant_chats.delete_chat(workspaces.load_workspace(workspace_id), chat_id)
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/assistant/mentions")
def list_mentions(
    workspace_id: str,
    q: str = Query(""),
    limit: int = Query(30, ge=1, le=80),
):
    return {
        "items": assistant_chats.mention_candidates(
            workspaces.load_workspace(workspace_id), query=q, limit=limit
        )
    }


@router.post("/workspaces/{workspace_id}/assistant/chats/{chat_id}/messages")
async def send_message(workspace_id: str, chat_id: str, payload: dict = Body(...)):
    return await asyncio.to_thread(
        assistant_chats.send_message, workspaces.load_workspace(workspace_id), chat_id, payload
    )


@router.post("/workspaces/{workspace_id}/assistant/chats/{chat_id}/messages/stream")
async def send_message_stream(
    workspace_id: str, chat_id: str, request: Request, payload: dict = Body(...)
):
    ws = workspaces.load_workspace(workspace_id)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
    cancel_event = threading.Event()
    worker_done = threading.Event()

    def push(item: tuple[str, dict] | None) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass

    def on_event(kind: str, data: dict) -> None:
        push((kind, data))

    def worker() -> None:
        try:
            result = assistant_chats.send_message(
                ws, chat_id, payload, on_event=on_event, cancel_event=cancel_event
            )
            try:
                revision = workspaces.read_revision(workspace_id)
                push(("revision", {"revision": revision}))
            except workspaces.WorkspaceError:
                pass
            kind = "cancelled" if (result.get("outcome") or {}).get("kind") == "cancelled" else "done"
            push((kind, result))
        except Exception as error:
            push(("error", {"detail": str(error)}))
        finally:
            worker_done.set()
            push(None)

    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(worker,),
        daemon=True,
        name=f"assistant-stream-{chat_id}",
    ).start()

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    return
                event_type, data = item
                yield f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            # Starlette cancels the streaming generator on client disconnect
            # (ASGI spec < 2.4) while suspended on queue.get(); ensure the
            # worker always receives the cancellation signal.
            if not worker_done.is_set():
                cancel_event.set()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.patch("/workspaces/{workspace_id}/assistant/chats/{chat_id}/artifacts/{artifact_id}")
def patch_artifact(workspace_id: str, chat_id: str, artifact_id: str, payload: dict = Body(...)):
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
