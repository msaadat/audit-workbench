"""The decision queue: everything in an engagement waiting on a person.

Read-only by design. Resolving a decision goes to the endpoint that already
owned it — approvals and interactions to the agent routes, document-test items
to the doc-test routes, observations to planning — so this queue can never
drift from the surface it summarizes.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import decisions, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["decisions"])


@router.get("/decisions")
async def get_decisions(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return decisions.decisions_payload(ws)
