"""Saved analyses: create, edit (params/code), remove, and compute.

The working-set sibling of dashboard tiles — same spec-not-data model, but
these live in the Analysis tab's rail. Pinning promotes a copy to a tile via
the existing /tiles routes; the two collections stay independent.
"""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import analysis_results, dashboard, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["analyses"])


@router.get("/analyses")
async def get_analyses(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return dashboard.analyses_payload(ws)


@router.get("/analyses/summary")
async def get_analyses_summary(workspace_id: str):
    """Read persisted execution metadata without re-running any procedure."""
    ws = workspaces.load_workspace(workspace_id)
    return analysis_results.analyses_summary_payload(ws)


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


@router.post("/analyses/{analysis_id}/pin")
async def pin_analysis(workspace_id: str, analysis_id: str, payload: dict = Body(default={} )):
    """Promote a saved analysis to a live, recomputable dashboard tile."""
    ws = workspaces.load_workspace(workspace_id)
    analysis = ws._analysis(analysis_id)
    last_result = analysis.get("last_result") or {}
    title = str(payload.get("title") or analysis.get("title") or "").strip()
    note = str(payload.get("note") or analysis.get("note") or "").strip()
    tile = ws.add_tile(
        {
            "kind": analysis.get("kind"),
            "table": analysis.get("table"),
            "title": title,
            "note": note,
            "spec": dict(analysis.get("spec") or {}),
            "viz": dict(analysis.get("viz") or {"type": "table"}),
            "analysis_id": analysis_id,
            "result_ref": (
                f"analysis:{analysis_id}:{last_result.get('run_id')}"
                if last_result.get("run_id") else f"analysis:{analysis_id}"
            ),
        }
    )
    return dashboard.tile_payload(ws, tile)


@router.delete("/analyses/{analysis_id}")
async def remove_analysis(workspace_id: str, analysis_id: str):
    ws = workspaces.load_workspace(workspace_id)
    ws.remove_analysis(analysis_id)
    return {"ok": True}
