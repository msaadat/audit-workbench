"""Planning, template, and RCM-central planned-test endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import doc_tests, findings, rcm_execution, templates_store, working_papers, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["planning"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


@router.get("/templates/{name}")
async def get_template(workspace_id: str, name: str):
    return templates_store.get_template(_ws(workspace_id), name)


@router.put("/templates/{name}")
async def put_template(workspace_id: str, name: str, payload: dict = Body(...)):
    return templates_store.put_template(
        _ws(workspace_id), name, payload.get("markdown"), bool(payload.get("reset"))
    )


@router.get("/planning")
async def get_planning(workspace_id: str):
    ws = _ws(workspace_id)
    return {
        "planning": ws.planning, "rcm": ws.rcm, "procedures": ws.work_program,
        "data_tests": ws.data_tests, "observations": ws.observations,
        "rcm_migration": ws.rcm_migration,
        "document_tests": doc_tests.list_tests(ws),
        "findings": ws.findings,
        "finding_rollups": findings.rollups(ws),
    }


@router.patch("/planning")
async def patch_planning(workspace_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).update_planning(payload)


@router.get("/rcm")
async def list_rcm(workspace_id: str):
    return {"items": _ws(workspace_id).rcm}


@router.post("/rcm")
async def add_rcm(workspace_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).add_rcm(payload)


@router.patch("/rcm/{row_id}")
async def patch_rcm(workspace_id: str, row_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).update_rcm(row_id, payload)


@router.delete("/rcm/{row_id}")
async def delete_rcm(workspace_id: str, row_id: str):
    ws = _ws(workspace_id)
    ws.remove_rcm(row_id)
    return {"ok": True}


@router.post("/rcm/{row_id}/planned-tests")
async def add_planned_test(workspace_id: str, row_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).add_planned_test(row_id, payload)


@router.patch("/rcm/{row_id}/planned-tests/{planned_test_id}")
async def patch_planned_test(
    workspace_id: str, row_id: str, planned_test_id: str, payload: dict = Body(...)
):
    return _ws(workspace_id).update_planned_test(row_id, planned_test_id, payload)


@router.delete("/rcm/{row_id}/planned-tests/{planned_test_id}")
async def delete_planned_test(workspace_id: str, row_id: str, planned_test_id: str):
    ws = _ws(workspace_id)
    ws.remove_planned_test(row_id, planned_test_id)
    return {"ok": True}


@router.get("/rcm/coverage")
async def get_rcm_coverage(workspace_id: str):
    return rcm_execution.coverage(_ws(workspace_id))


@router.post("/rcm/rollup")
async def rollup_rcm(workspace_id: str):
    return rcm_execution.rollup(_ws(workspace_id))


@router.get("/rcm/completion")
async def get_rcm_completion(workspace_id: str):
    return rcm_execution.completion(_ws(workspace_id))


@router.get("/rcm/{row_id}/working-paper")
async def get_rcm_working_paper(workspace_id: str, row_id: str):
    return working_papers.render_rcm(_ws(workspace_id), row_id)


@router.post("/rcm/{row_id}/working-paper")
async def generate_rcm_working_paper(workspace_id: str, row_id: str):
    return working_papers.generate_rcm(_ws(workspace_id), row_id)


@router.get("/observations")
async def list_observations(workspace_id: str):
    return {"items": _ws(workspace_id).observations}


@router.patch("/observations/{observation_id}")
async def disposition_observation(
    workspace_id: str, observation_id: str, payload: dict = Body(...)
):
    return rcm_execution.disposition(
        _ws(workspace_id),
        observation_id,
        str(payload.get("disposition") or ""),
        str(payload.get("auditor_note") or ""),
    )


@router.get("/procedures")
async def list_procedures(workspace_id: str):
    return {"items": _ws(workspace_id).work_program}


@router.post("/procedures")
async def add_procedure(workspace_id: str, payload: dict = Body(...)):
    raise workspaces.WorkspaceError(
        "Legacy procedure APIs are read-only; add a planned test under the RCM instead."
    )


@router.patch("/procedures/{procedure_id}")
async def patch_procedure(workspace_id: str, procedure_id: str, payload: dict = Body(...)):
    raise workspaces.WorkspaceError(
        "Legacy procedure APIs are read-only; edit the migrated RCM planned test instead."
    )


@router.delete("/procedures/{procedure_id}")
async def delete_procedure(workspace_id: str, procedure_id: str):
    raise workspaces.WorkspaceError(
        "Legacy procedure APIs are read-only during the migration window."
    )
