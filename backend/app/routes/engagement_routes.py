"""Starting an engagement: what the agent would do, and recording the brief."""

from __future__ import annotations

from fastapi import APIRouter, Body, Query

from .. import engagement, engagement_record, workspaces

router = APIRouter(prefix="/api", tags=["engagement"])


@router.get("/engagement/plan")
async def get_engagement_plan(
    template: str = Query(engagement.DEFAULT_TEMPLATE),
    mode: str = Query("auto"),
):
    """The outcomes, gates, destination, and measured cost of a goal template.

    Read-only and workspace-independent: this is what an auditor sees *before*
    a workspace exists.
    """
    return engagement.plan_preview(template, mode)


@router.get("/workspaces/{workspace_id}/engagement/record")
async def get_engagement_record(workspace_id: str):
    """What this engagement filed, in the order each work product settled.

    A projection of runs and their milestones — no state of its own.
    """
    ws = workspaces.load_workspace(workspace_id)
    return engagement_record.record(ws)


@router.post("/workspaces/{workspace_id}/engagement/brief")
async def put_engagement_brief(workspace_id: str, brief: dict = Body(...)):
    """Record a brief as planning context.

    The brief contains optional engagement details only. Planning derives its
    objective and scope from the engagement material.
    """
    ws = workspaces.load_workspace(workspace_id)
    return engagement.apply_brief(ws, brief)
