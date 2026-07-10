"""Validation endpoints: check registry, rule set CRUD, and stateless runs.

Rule sets persist in workspace.json (spec-not-data); every run loads the
frame through the loader cache and computes fresh. The run/detail/export
endpoints take the rules in the request body so the SPA can validate an
unsaved draft and run a saved set against any table (the "Run against…"
override) with the same routes.
"""

from __future__ import annotations

import io
import re

import polars as pl
import xlsxwriter
from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse

from .. import validation, workspaces
from .analysis_routes import _excel_response

router = APIRouter(prefix="/api", tags=["validation"])

# Failing-row sheets in the report workbook are capped; the per-rule export
# endpoint streams the full set.
REPORT_SHEET_MAX_ROWS = 10_000


def _frame(workspace_id: str, table_name: str) -> pl.DataFrame:
    return workspaces.load_workspace(workspace_id).get_frame(table_name)


@router.get("/validation/checks")
async def checks_registry():
    return validation.registry_payload()


# ---------------------------------------------------------------- rule sets
@router.get("/workspaces/{workspace_id}/rulesets")
async def get_rulesets(workspace_id: str):
    return {"rulesets": workspaces.load_workspace(workspace_id).rulesets}


@router.post("/workspaces/{workspace_id}/rulesets")
async def add_ruleset(workspace_id: str, payload: dict = Body(...)):
    return workspaces.load_workspace(workspace_id).add_ruleset(payload)


@router.patch("/workspaces/{workspace_id}/rulesets/{ruleset_id}")
async def update_ruleset(workspace_id: str, ruleset_id: str, changes: dict = Body(...)):
    return workspaces.load_workspace(workspace_id).update_ruleset(ruleset_id, changes)


@router.delete("/workspaces/{workspace_id}/rulesets/{ruleset_id}")
async def remove_ruleset(workspace_id: str, ruleset_id: str):
    workspaces.load_workspace(workspace_id).remove_ruleset(ruleset_id)
    return {"ok": True}


# --------------------------------------------------------------------- runs
@router.post("/workspaces/{workspace_id}/rulesets/{ruleset_id}/run")
async def run_ruleset(workspace_id: str, ruleset_id: str, payload: dict = Body(default={})):
    ws = workspaces.load_workspace(workspace_id)
    ruleset = ws._ruleset(ruleset_id)
    table = payload.get("table") or ruleset["table"]
    run = validation.run_rules(ws.get_frame(table), ruleset["rules"], table, ws.get_frame)
    # Runs of the saved spec are evidence — record a summary-only history
    # entry. Draft runs (the /tables/{t}/validate route) are not recorded.
    run["history"] = ws.record_run(ruleset_id, run)
    return run


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate")
async def validate_table(workspace_id: str, table_name: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    df = ws.get_frame(table_name)
    return validation.run_rules(df, payload.get("rules") or [], table_name, ws.get_frame)


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate/detail")
async def validation_detail(workspace_id: str, table_name: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    df = ws.get_frame(table_name)
    return validation.detail_payload(df, payload.get("rule") or {}, ws.get_frame)


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate/export")
async def export_failures(workspace_id: str, table_name: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    rule = payload.get("rule") or {}
    failures = validation.rule_failures(ws.get_frame(table_name), rule, ws.get_frame)
    slug = workspaces.slugify(validation.rule_label(rule)).replace("-", "_")
    return _excel_response(failures, f"{table_name}_{slug}_failures.xlsx")


def _sheet_name(index: int, base: str, used: set[str]) -> str:
    clean = re.sub(r"[\[\]:*?/\\']", " ", base).strip() or "rule"
    name = f"{index} {clean}"[:31]
    while name in used:
        name = f"{name[:29]}~{index}"[:31]
    used.add(name)
    return name


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate/report")
async def export_report(workspace_id: str, table_name: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    df = ws.get_frame(table_name)
    rules = payload.get("rules") or []
    run = validation.run_rules(df, rules, table_name, ws.get_frame)

    # One workbook: a Summary sheet plus one sheet of failing rows per
    # rule that actually failed (capped — the per-rule export has them all).
    rules_by_id = {r.get("id"): r for r in rules}
    buffer = io.BytesIO()
    with xlsxwriter.Workbook(buffer, {"in_memory": True}) as workbook:
        validation.report_frame(run).write_excel(workbook=workbook, worksheet="Summary")
        used: set[str] = set()
        for index, result in enumerate(run["results"], start=1):
            rule = rules_by_id.get(result["rule_id"])
            if rule is None or result["fail_count"] == 0 or result["check"] == "row_count":
                continue
            if result["verdict"] in ("error", "skipped"):
                continue
            failures = validation.rule_failures(df, rule, ws.get_frame).head(
                REPORT_SHEET_MAX_ROWS
            )
            sheet = _sheet_name(index, f"{result['column'] or 'table'}", used)
            failures.write_excel(workbook=workbook, worksheet=sheet)
    buffer.seek(0)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{table_name}_validation_report.xlsx")
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


# ----------------------------------------------------------- values picker
@router.get("/workspaces/{workspace_id}/tables/{table_name}/columns/{column}/values")
async def column_values(
    workspace_id: str, table_name: str, column: str, limit: int = 200
):
    df = _frame(workspace_id, table_name)
    if column not in df.columns:
        raise workspaces.WorkspaceError(f"Column '{column}' not found.")
    distinct = (
        df.select(pl.col(column).cast(pl.String).str.strip_chars().drop_nulls().unique())
        .to_series()
        .sort()
    )
    return {
        "distinct": distinct.len(),
        "truncated": distinct.len() > limit,
        "values": distinct.head(limit).to_list(),
    }
