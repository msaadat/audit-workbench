"""Agent-run endpoints.

  GET  /api/agent/status                                   agent LLM profile
  POST /api/workspaces/{id}/agent/runs                     start a run
  GET  /api/workspaces/{id}/agent/runs                     run history
  GET  /api/workspaces/{id}/agent/runs/{run}               full run record
  GET  /api/workspaces/{id}/agent/runs/{run}/events        SSE (cursor replay)
  POST /api/workspaces/{id}/agent/runs/{run}/pause|resume|cancel
  POST /api/workspaces/{id}/agent/runs/{run}/messages      steer / follow up
  POST /api/workspaces/{id}/agent/runs/{run}/approvals/{approval}
  GET  /api/workspaces/{id}/tables/{t}/suggest-rules       deterministic rules

The SSE stream tails the run's on-disk event log, so replay after reconnect
or backend restart is inherent: pass ``cursor`` (or ``Last-Event-ID``) and
events after it stream first, then the tail is followed until the run ends.
"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import llm, workspaces
from ..agent import actions, runner, store, suggest

router = APIRouter(prefix="/api", tags=["agent"])

POLL_SECONDS = 0.4
HEARTBEAT_SECONDS = 15.0

# The generic creation endpoint's historical default was the fixed v1 analysis
# pipeline. Phase 12 retired that runner: an exploratory analysis request is now
# the declared ``analysis_workflow_v1`` graph, requested through its registered
# goal template.
ANALYSIS_RUN_KIND = "analysis"
ANALYSIS_GOAL_TEMPLATE = "data_analysis"
ANALYSIS_COMMAND_TEXT = (
    "Analyze the available tables: infer relationships, materialize the joins "
    "the evidence supports, and run the resulting analyses."
)


@router.get("/agent/status")
def agent_status():
    return llm.agent_status()


@router.get("/workspaces/{workspace_id}/tables/{table}/suggest-rules")
def suggest_rules(workspace_id: str, table: str):
    ws = workspaces.load_workspace(workspace_id)
    if table not in ws.table_names():
        raise HTTPException(404, detail=f"No table named '{table}'.")
    return {"suggestions": suggest.suggest_rules(ws, table)}


@router.post("/workspaces/{workspace_id}/agent/runs")
async def create_run(workspace_id: str, payload: dict = Body(default={})):
    ws = workspaces.load_workspace(workspace_id)
    try:
        if isinstance(payload.get("command"), dict) or payload.get("requested_outcomes"):
            command = dict(payload.get("command") or {})
            if payload.get("requested_outcomes"):
                command.update(
                    source=command.get("source") or "tab_button",
                    text=command.get("text") or "Run the selected audit outcomes.",
                    requested_outcomes=payload.get("requested_outcomes"),
                    target_refs=payload.get("target_refs") or ["workspace:current"],
                    generation_mode=payload.get("generation_mode") or "reuse_existing",
                )
            run = await asyncio.to_thread(
                runner.start_command_run, ws, payload.get("mode") or "auto",
                command, payload.get("parent_run_id"),
                payload.get("context") or {},
            )
        elif (payload.get("kind") or ANALYSIS_RUN_KIND) == ANALYSIS_RUN_KIND:
            # Exploratory data analysis is a declared workflow, not a fixed
            # pipeline: the request becomes a command run requesting
            # ``analysis.executed`` through the registered ``data_analysis``
            # goal template. Routing, not this endpoint, selects the engine.
            context = dict(payload.get("context") or {})
            objective = str(context.get("objective") or "").strip()
            run = await asyncio.to_thread(
                runner.start_command_run,
                ws,
                payload.get("mode") or "auto",
                {
                    "source": "tab_button",
                    "text": objective or ANALYSIS_COMMAND_TEXT,
                    "goal_template": ANALYSIS_GOAL_TEMPLATE,
                    "target_refs": payload.get("target_refs") or ["workspace:current"],
                    "generation_mode": payload.get("generation_mode") or "reuse_existing",
                },
                payload.get("parent_run_id"),
                context,
            )
        else:
            run = await asyncio.to_thread(
                runner.start_run,
                ws,
                payload.get("mode") or "auto",
                payload.get("context") or {},
                kind=payload.get("kind"),
            )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error
    return run


@router.get("/agent/actions")
def action_coverage():
    return {"actions": actions.ACTION_COVERAGE}


@router.get("/workspaces/{workspace_id}/agent/runs")
def list_runs(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    runner.recover_workspace(ws)
    return {"runs": store.list_runs(ws)}


@router.get("/workspaces/{workspace_id}/agent/runs/{run_id}")
def get_run(workspace_id: str, run_id: str):
    ws = workspaces.load_workspace(workspace_id)
    runner.recover_workspace(ws)
    return store.load_run(ws, run_id)


@router.get("/workspaces/{workspace_id}/agent/runs/{run_id}/sidecars/{sha1}")
def get_run_sidecar(workspace_id: str, run_id: str, sha1: str):
    ws = workspaces.load_workspace(workspace_id)
    run = store.load_run(ws, run_id)
    if not re.fullmatch(r"[0-9a-f]{40}", sha1):
        raise workspaces.WorkspaceError("Invalid run sidecar reference.")
    # A hash is readable only when the run record actually references it.
    serialized = json.dumps(run, default=str)
    if sha1 not in serialized:
        raise workspaces.WorkspaceError("Run sidecar is not referenced by this run.")
    return store.read_sidecar(ws, run_id, {"path": f"sidecars/{sha1}.json"})


@router.post("/workspaces/{workspace_id}/agent/runs/{run_id}/pause")
def pause_run(workspace_id: str, run_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return runner.pause_run(ws, run_id)


@router.post("/workspaces/{workspace_id}/agent/runs/{run_id}/resume")
async def resume_run(workspace_id: str, run_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return await asyncio.to_thread(runner.resume_run, ws, run_id)


@router.post("/workspaces/{workspace_id}/agent/runs/{run_id}/retry")
async def retry_run(workspace_id: str, run_id: str, payload: dict = Body(default={})):
    ws = workspaces.load_workspace(workspace_id)
    raw_refs = payload.get("target_refs")
    if raw_refs is not None and (
        not isinstance(raw_refs, list)
        or any(not isinstance(value, str) or not value.strip() for value in raw_refs)
    ):
        raise HTTPException(400, detail="target_refs must be a list of non-empty strings.")
    instruction = payload.get("instruction")
    if instruction is not None and not isinstance(instruction, str):
        raise HTTPException(400, detail="instruction must be a string.")
    try:
        return await asyncio.to_thread(
            lambda: runner.retry_run(
                ws, run_id, target_refs=raw_refs, instruction=instruction
            )
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.post("/workspaces/{workspace_id}/agent/runs/{run_id}/continue")
async def continue_audit(workspace_id: str, run_id: str):
    ws = workspaces.load_workspace(workspace_id)
    try:
        return await asyncio.to_thread(runner.continue_audit, ws, run_id)
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.post("/workspaces/{workspace_id}/agent/runs/{run_id}/cancel")
def cancel_run(workspace_id: str, run_id: str, payload: dict = Body(default={})):
    ws = workspaces.load_workspace(workspace_id)
    return runner.cancel_run(
        ws, run_id, reason=payload.get("reason") or "",
        actor=payload.get("actor") or "auditor", source="api",
    )


@router.post("/workspaces/{workspace_id}/agent/runs/{run_id}/messages")
async def send_message(workspace_id: str, run_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    try:
        return await asyncio.to_thread(
            runner.steer, ws, run_id, payload.get("content") or ""
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.post(
    "/workspaces/{workspace_id}/agent/runs/{run_id}/approvals/{approval_id}"
)
def decide_approval(
    workspace_id: str, run_id: str, approval_id: str, payload: dict = Body(...)
):
    ws = workspaces.load_workspace(workspace_id)
    return runner.resolve_approval(
        ws, run_id, approval_id, payload.get("decisions") or []
    )


@router.post(
    "/workspaces/{workspace_id}/agent/runs/{run_id}/interactions/{interaction_id}/respond"
)
def respond_interaction(
    workspace_id: str, run_id: str, interaction_id: str, payload: dict = Body(...)
):
    ws = workspaces.load_workspace(workspace_id)
    return runner.resolve_interaction(ws, run_id, interaction_id, payload)


@router.get("/workspaces/{workspace_id}/agent/runs/{run_id}/events")
async def run_events(
    workspace_id: str, run_id: str, request: Request, cursor: int = 0
):
    ws = workspaces.load_workspace(workspace_id)
    store.load_run(ws, run_id)  # 400 early when the run doesn't exist

    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass

    async def stream(after: int):
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return
            events = await asyncio.to_thread(store.read_events, ws, run_id, after)
            for event in events:
                after = event["seq"]
                idle = 0.0
                yield (
                    f"id: {event['seq']}\n"
                    f"event: {event['type']}\n"
                    f"data: {json.dumps(event, default=str)}\n\n"
                )
            run = await asyncio.to_thread(store.load_run, ws, run_id)
            if not events and run["status"] in (
                *store.TERMINAL_STATUSES,
                "interrupted",
            ):
                yield "event: stream_end\ndata: {}\n\n"
                return
            await asyncio.sleep(POLL_SECONDS)
            idle += POLL_SECONDS
            if idle >= HEARTBEAT_SECONDS:
                idle = 0.0
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
