"""Natural-language assistant endpoints.

  GET  /api/assistant/status                        is the LLM configured?
  PATCH /api/assistant/settings                     update provider/model
  POST /api/workspaces/{id}/assistant               ask a question (tool loop)
  POST /api/workspaces/{id}/run-python              execute an edited snippet

Computations are local and model-facing result previews are bounded.
"""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import assistant, llm, workspaces

router = APIRouter(prefix="/api", tags=["assistant"])


@router.get("/assistant/status")
async def assistant_status():
    return llm.status()


@router.patch("/assistant/settings")
async def assistant_settings(payload: dict = Body(...)):
    return llm.update_settings(payload)


@router.post("/workspaces/{workspace_id}/assistant")
async def ask(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    return assistant.ask(
        ws,
        payload.get("question", ""),
        payload.get("document_ids"),
    )


@router.post("/workspaces/{workspace_id}/run-python")
async def run_python(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    return assistant.run_python_snippet(ws, payload.get("code", ""))
