"""Natural-language assistant endpoints.

  GET  /api/assistant/status                        is the LLM configured?
  POST /api/workspaces/{id}/assistant               ask a question (tool loop)
  POST /api/workspaces/{id}/run-python              execute an edited snippet

All computation is local; only metadata reaches the LLM (see :mod:`.assistant`).
"""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import assistant, llm, workspaces

router = APIRouter(prefix="/api", tags=["assistant"])


@router.get("/assistant/status")
async def assistant_status():
    return llm.status()


@router.post("/workspaces/{workspace_id}/assistant")
async def ask(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    return assistant.ask(ws, payload.get("question", ""))


@router.post("/workspaces/{workspace_id}/run-python")
async def run_python(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    return assistant.run_python_snippet(ws, payload.get("code", ""))
