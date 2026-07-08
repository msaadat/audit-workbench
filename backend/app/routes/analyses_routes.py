"""Saved analyses: create, edit (params/code), remove, and compute.

The working-set sibling of dashboard tiles — same spec-not-data model, but
these live in the Analysis tab's rail. Pinning promotes a copy to a tile via
the existing /tiles routes; the two collections stay independent.
"""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import dashboard, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["analyses"])


@router.get("/analyses")
async def get_analyses(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return dashboard.analyses_payload(ws)


@router.post("/analyses")
async def add_analysis(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    analysis = ws.add_analysis(payload)
    return dashboard.analysis_payload(ws, analysis)


@router.patch("/analyses/{analysis_id}")
async def update_analysis(workspace_id: str, analysis_id: str, changes: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    analysis = ws.update_analysis(analysis_id, changes)
    return dashboard.analysis_payload(ws, analysis)


@router.delete("/analyses/{analysis_id}")
async def remove_analysis(workspace_id: str, analysis_id: str):
    ws = workspaces.load_workspace(workspace_id)
    ws.remove_analysis(analysis_id)
    return {"ok": True}
