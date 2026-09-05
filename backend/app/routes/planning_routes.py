"""Planning, template, and RCM-central endpoints."""

from __future__ import annotations

import io
import re

import polars as pl
from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import (
    doc_tests,
    findings,
    planning_cycle_graph,
    projection_cache,
    rcm_execution,
    templates_store,
    uploads,
    workspaces,
    working_papers,
)

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["planning"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "export"


@router.get("/templates/{name}")
def get_template(workspace_id: str, name: str):
    return templates_store.get_template(_ws(workspace_id), name)


@router.put("/templates/{name}")
def put_template(workspace_id: str, name: str, payload: dict = Body(...)):
    return templates_store.put_template(
        _ws(workspace_id), name, payload.get("markdown"), bool(payload.get("reset"))
    )


@router.get("/planning")
def get_planning(workspace_id: str):
    ws = _ws(workspace_id)

    def draw() -> dict:
        # The rollup and the document-test listing below independently resolve
        # the same tests, and cycle-vouching materialization underneath them
        # re-reads the whole evidence corpus per test. Nothing in this handler
        # writes, so a single cache scope lets the second and third pass reuse
        # the first.
        with doc_tests.request_cache_scope():
            # Present current derived statuses without mutating workspace.json.
            rcm_execution.rollup(ws, persist=False)
            return {
                "planning": ws.planning, "rcm": ws.rcm, "procedures": ws.work_program,
                "data_tests": ws.data_tests, "observations": ws.observations,
                "document_tests": doc_tests.list_tests(ws),
                "findings": ws.findings,
                "finding_rollups": findings.rollups(ws),
            }

    # A pure function of the workspace's files, and the largest read the
    # planning surface makes: kept between requests until any of them changes.
    return projection_cache.cached(ws.root, "planning", draw)


@router.patch("/planning")
def patch_planning(workspace_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).update_planning(payload)


@router.get("/planning/cycle/graph")
def get_cycle_graph(workspace_id: str):
    """The cycle drawn: one entry per step, and the edges between their fields.

    Read-only and derived — no model call, and nothing the page then has to
    infer. Cached on the workspace signature because it opens a frame per named
    population to state its size, which is the one expensive thing it does.
    """

    ws = _ws(workspace_id)
    return projection_cache.cached(
        ws.root, "planning_cycle_graph", lambda: planning_cycle_graph.cycle_graph(ws)
    )


@router.get("/planning/apm/export")
def export_apm(workspace_id: str):
    ws = _ws(workspace_id)
    filename = _safe_filename(f"{ws.name}_APM.md")
    return PlainTextResponse(
        ws.planning.get("apm_markdown") or "",
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/planning/apm/import")
async def import_apm(workspace_id: str, file: UploadFile = File(...)):
    ws = _ws(workspace_id)
    content = await uploads.read_upload(file)
    try:
        markdown = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise workspaces.WorkspaceError("The APM file must be UTF-8 text (Markdown).") from error
    return ws.update_planning({"apm_markdown": markdown})


@router.get("/rcm")
def list_rcm(workspace_id: str):
    return {"items": _ws(workspace_id).rcm}


@router.post("/rcm")
def add_rcm(workspace_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).add_rcm(payload)


@router.patch("/rcm/{row_id}")
def patch_rcm(workspace_id: str, row_id: str, payload: dict = Body(...)):
    return _ws(workspace_id).update_rcm(row_id, payload)


@router.delete("/rcm/{row_id}")
def delete_rcm(workspace_id: str, row_id: str):
    ws = _ws(workspace_id)
    ws.remove_rcm(row_id)
    return {"ok": True}


@router.get("/rcm/export")
def export_rcm(workspace_id: str):
    ws = _ws(workspace_id)
    rows = ws.export_rcm_rows()
    columns = ("id", *workspaces.RCM_IMPORT_FIELDS)
    schema = {column: pl.Utf8 for column in columns}
    df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    buffer = io.BytesIO()
    df.write_excel(buffer)
    buffer.seek(0)
    filename = _safe_filename(f"{ws.name}_RCM.xlsx")
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/rcm/import")
async def import_rcm(workspace_id: str, file: UploadFile = File(...)):
    ws = _ws(workspace_id)
    content = await uploads.read_upload(file)
    suffix = (file.filename or "").lower()
    buffer = io.BytesIO(content)
    if suffix.endswith(".csv"):
        df = pl.read_csv(buffer)
    elif suffix.endswith(".tsv"):
        df = pl.read_csv(buffer, separator="\t")
    else:
        df = pl.read_excel(buffer)
    if "id" not in df.columns:
        raise workspaces.WorkspaceError("The import file must include the 'id' column from the export.")
    result = ws.import_rcm(df.to_dicts())
    return {**result, "rcm": ws.rcm}


@router.get("/rcm/coverage")
def get_rcm_coverage(workspace_id: str):
    return rcm_execution.coverage(_ws(workspace_id))


@router.post("/rcm/rollup")
def rollup_rcm(workspace_id: str):
    return rcm_execution.rollup(_ws(workspace_id))


@router.get("/rcm/completion")
def get_rcm_completion(workspace_id: str):
    return rcm_execution.completion(_ws(workspace_id))


@router.get("/rcm/{row_id}/working-paper")
def get_rcm_working_paper(workspace_id: str, row_id: str):
    return working_papers.render_rcm(_ws(workspace_id), row_id)


@router.post("/rcm/{row_id}/working-paper")
def generate_rcm_working_paper(workspace_id: str, row_id: str):
    return working_papers.generate_rcm(_ws(workspace_id), row_id)


@router.get("/observations")
def list_observations(workspace_id: str):
    return {"items": _ws(workspace_id).observations}


@router.get("/procedures")
def list_procedures(workspace_id: str):
    return {"items": _ws(workspace_id).work_program}


@router.post("/procedures")
def add_procedure(workspace_id: str, payload: dict = Body(...)):
    raise workspaces.WorkspaceError(
        "Legacy procedure APIs are read-only; add a Document or Data Test linked to the RCM row instead."
    )


@router.patch("/procedures/{procedure_id}")
def patch_procedure(workspace_id: str, procedure_id: str, payload: dict = Body(...)):
    raise workspaces.WorkspaceError(
        "Legacy procedure APIs are read-only; edit the RCM-linked test instead."
    )


@router.delete("/procedures/{procedure_id}")
def delete_procedure(workspace_id: str, procedure_id: str):
    raise workspaces.WorkspaceError(
        "Legacy procedure APIs are read-only during the migration window."
    )
