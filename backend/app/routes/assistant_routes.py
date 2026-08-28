"""Natural-language assistant endpoints.

  GET  /api/assistant/status                        is the LLM configured?
  PATCH /api/assistant/settings                     update provider/model
  POST /api/workspaces/{id}/assistant               ask a question (tool loop)
  POST /api/workspaces/{id}/run-python              execute an edited snippet

Computations are local and model-facing result previews are bounded.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from .. import assistant, auth, llm, workspaces

router = APIRouter(prefix="/api", tags=["assistant"])


@router.get("/assistant/status")
async def assistant_status():
    """Readable by everyone: the UI has to know whether a model is configured."""
    return llm.status()


@router.patch("/assistant/settings")
# ``request`` follows ``payload`` because FastAPI resolves parameters by
# annotation rather than position, and callers in-process pass the payload
# positionally.
async def assistant_settings(payload: dict = Body(...), request: Request = None):
    """Administrator-only: assistant configuration is global.

    Provider credentials, model, sampling, and the vision profile are one
    document shared by everyone on this server, so an ordinary user changing
    them would retarget every other auditor's assistant.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise auth.AuthError("This request has no authenticated user.")
    auth.require_admin(principal)
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
    """Execute an auditor-reviewed snippet locally.

    ``sandbox.run`` refuses outside single-user mode. The gate lives there
    rather than here because this route is not the only way model-authored
    Python reaches the interpreter — data tests, dashboard tiles, and agent
    analyses all execute through the same function.
    """
    ws = workspaces.load_workspace(workspace_id)
    return assistant.run_python_snippet(ws, payload.get("code", ""))
