"""Workspace Debug Console APIs and replayable live event stream."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from .. import debug_service, debug_store, workspaces
from ..agent import store as agent_store

router = APIRouter(prefix="/api/workspaces/{workspace_id}/debug", tags=["debug"])


@router.get("/overview")
async def overview(workspace_id: str):
    return debug_service.overview(workspaces.load_workspace(workspace_id))


@router.get("/runs")
async def runs(workspace_id: str, cursor: int = 0, limit: int = 50,
               status: str | None = None, kind: str | None = None,
               schema_version: int | None = None):
    ws = workspaces.load_workspace(workspace_id)
    items = agent_store.list_runs(ws)
    if status: items = [item for item in items if item.get("status") == status]
    if kind: items = [item for item in items if item.get("kind") == kind]
    if schema_version is not None: items = [item for item in items if int(item.get("schema_version") or 1) == schema_version]
    cursor = max(0, cursor); limit = min(250, max(1, limit)); selected = items[cursor:cursor + limit]
    return {"items": selected, "cursor": cursor, "total": len(items),
            "next_cursor": cursor + len(selected) if cursor + len(selected) < len(items) else None}


@router.get("/runs/{run_id}")
async def run_detail(workspace_id: str, run_id: str):
    return debug_service.run_detail(workspaces.load_workspace(workspace_id), run_id)


@router.get("/calls")
async def calls(workspace_id: str, cursor: int = 0, limit: int = 100,
                run_id: str | None = None, status: str | None = None,
                stage: str | None = None, purpose: str | None = None):
    return debug_service.list_calls(
        workspaces.load_workspace(workspace_id), cursor=cursor, limit=limit,
        run_id=run_id, status=status, stage=stage, purpose=purpose,
    )


@router.get("/calls/{call_id}")
async def call_detail(workspace_id: str, call_id: str):
    return debug_service.get_call(workspaces.load_workspace(workspace_id), call_id)


@router.get("/events")
async def events(workspace_id: str, cursor: int = 0, limit: int = 100,
                 type: str | None = Query(default=None), run_id: str | None = None,
                 call_id: str | None = None):
    return debug_service.list_events(
        workspaces.load_workspace(workspace_id), cursor=cursor, limit=limit,
        type_=type, run_id=run_id, call_id=call_id,
    )


@router.get("/events/stream")
async def event_stream(workspace_id: str, request: Request, cursor: int = 0):
    ws = workspaces.load_workspace(workspace_id)
    last = request.headers.get("last-event-id")
    if last:
        try: cursor = max(cursor, int(last))
        except ValueError: pass

    async def stream(after: int):
        idle = 0
        while True:
            if await request.is_disconnected(): return
            page = await asyncio.to_thread(debug_service.list_events, ws, cursor=after, limit=250)
            for event in page["items"]:
                after = event["seq"]
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
            await asyncio.sleep(0.4); idle += 1
            if idle >= 38:
                idle = 0; yield ": keep-alive\n\n"

    return StreamingResponse(stream(cursor), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/snapshots/{sha1}")
async def snapshot(workspace_id: str, sha1: str):
    return debug_service.snapshot(workspaces.load_workspace(workspace_id), sha1)


@router.get("/transitions/{transition_id}")
async def transition(workspace_id: str, transition_id: str):
    return debug_service.transition(workspaces.load_workspace(workspace_id), transition_id)


@router.delete("")
async def clear(workspace_id: str, confirm: str = ""):
    ws = workspaces.load_workspace(workspace_id)
    if confirm != workspace_id:
        raise workspaces.WorkspaceError("Type the workspace ID to confirm clearing debug telemetry.")
    debug_store.clear(workspace_id)
    return {"cleared": True}
