"""Analysis endpoints: schema/preview/profile, explore queries, analytics tests.

Everything is stateless — each request loads the frame through the loader
cache and computes fresh. Excel exports re-run the same computation and
stream the workbook; nothing is parked on disk server-side.
"""

from __future__ import annotations

import io
import re

import polars as pl
from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse

from .. import analytics, explore, profiler, workspaces

router = APIRouter(prefix="/api", tags=["analysis"])


def _frame(workspace_id: str, table_name: str) -> pl.DataFrame:
    return workspaces.load_workspace(workspace_id).get_frame(table_name)


def excel_response(df: pl.DataFrame, filename: str) -> StreamingResponse:
    buffer = io.BytesIO()
    df.write_excel(buffer)
    buffer.seek(0)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "export.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


# ------------------------------------------------------------------ metadata
@router.get("/workspaces/{workspace_id}/tables/{table_name}/schema")
def table_schema(workspace_id: str, table_name: str):
    df = _frame(workspace_id, table_name)
    return {"rows": df.height, "columns": profiler.schema_payload(df)}


@router.get("/workspaces/{workspace_id}/tables/{table_name}/preview")
def table_preview(workspace_id: str, table_name: str, rows: int = 100):
    df = _frame(workspace_id, table_name)
    return {"total_rows": df.height, **explore.frame_payload(df, min(rows, 500))}


@router.get("/workspaces/{workspace_id}/tables/{table_name}/profile")
def table_profile(workspace_id: str, table_name: str):
    return workspaces.load_workspace(workspace_id).get_profile(table_name)


# ------------------------------------------------------------------- explore
@router.get("/explore/meta")
def explore_meta():
    return {
        "filter_ops": [
            {"value": op, "label": label} for op, label in explore.FILTER_OPS.items()
        ],
        "agg_funcs": list(explore.AGG_FUNCS),
    }


@router.post("/workspaces/{workspace_id}/tables/{table_name}/query")
def run_query(workspace_id: str, table_name: str, spec: dict = Body(...)):
    return explore.run_query(_frame(workspace_id, table_name), spec)


@router.post("/workspaces/{workspace_id}/tables/{table_name}/query/export")
def export_query(workspace_id: str, table_name: str, spec: dict = Body(...)):
    result, _ = explore.run_query_full(_frame(workspace_id, table_name), spec)
    return excel_response(result, f"{table_name}_query.xlsx")


# ----------------------------------------------------------------- analytics
@router.get("/analytics")
def analytics_registry():
    return analytics.registry_payload()


@router.post("/workspaces/{workspace_id}/tables/{table_name}/analytics/{test_id}")
def run_analytics(
    workspace_id: str, table_name: str, test_id: str, params: dict = Body(default={})
):
    result = analytics.run_test(_frame(workspace_id, table_name), test_id, params)
    return result.payload()


@router.post("/workspaces/{workspace_id}/tables/{table_name}/analytics/{test_id}/export")
def export_analytics(
    workspace_id: str, table_name: str, test_id: str, params: dict = Body(default={})
):
    result = analytics.run_test(_frame(workspace_id, table_name), test_id, params)
    frame = result.export_frame()
    if frame is None:
        frame = pl.DataFrame({"stat": [s["label"] for s in result.stats],
                              "value": [s["value"] for s in result.stats]})
    return excel_response(frame, f"{table_name}_{test_id}.xlsx")
