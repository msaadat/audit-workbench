"""Dashboard tiles: pin, rename/annotate/reorder, remove, and compute."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body

from .. import dashboard, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["dashboard"])


@router.get("/dashboard")
async def get_dashboard(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return dashboard.dashboard_payload(ws)


@router.get("/dashboard/status")
async def get_engagement_status(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return dashboard.engagement_status_payload(ws)


@router.post("/dashboard/advice")
async def generate_dashboard_advice(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return await asyncio.to_thread(dashboard.generate_advice, ws)


@router.post("/tiles")
async def add_tile(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    tile = ws.add_tile(payload)
    return dashboard.tile_payload(ws, tile)


@router.patch("/tiles/{tile_id}")
async def update_tile(workspace_id: str, tile_id: str, changes: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    tile = ws.update_tile(tile_id, changes)
    return dashboard.tile_payload(ws, tile)


@router.delete("/tiles/{tile_id}")
async def remove_tile(workspace_id: str, tile_id: str):
    ws = workspaces.load_workspace(workspace_id)
    ws.remove_tile(tile_id)
    return {"ok": True}
