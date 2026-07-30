"""Starting an engagement: what the agent would do, and recording the brief."""

from __future__ import annotations

from fastapi import APIRouter, Body, Query

from .. import engagement, workspaces

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


@router.post("/workspaces/{workspace_id}/engagement/brief")
async def put_engagement_brief(workspace_id: str, brief: dict = Body(...)):
    """Record a brief as planning context.

    `objective` and `scope` are what `planning.context_ready` requires, so a
    completed brief lets the agent start at the memorandum rather than opening
    with an interview the auditor has already answered.
    """
    ws = workspaces.load_workspace(workspace_id)
    return engagement.apply_brief(ws, brief)
