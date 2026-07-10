"""Validation endpoints: check registry, rule set CRUD, and stateless runs.

Rule sets persist in workspace.json (spec-not-data); every run loads the
frame through the loader cache and computes fresh. The run/detail/export
endpoints take the rules in the request body so the SPA can validate an
unsaved draft and run a saved set against any table (the "Run against…"
override) with the same routes.
"""

from __future__ import annotations

import polars as pl
from fastapi import APIRouter, Body

from .. import validation, workspaces
from .analysis_routes import _excel_response

router = APIRouter(prefix="/api", tags=["validation"])


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
    return validation.run_rules(ws.get_frame(table), ruleset["rules"], table)


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate")
async def validate_table(workspace_id: str, table_name: str, payload: dict = Body(...)):
    df = _frame(workspace_id, table_name)
    return validation.run_rules(df, payload.get("rules") or [], table_name)


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate/detail")
async def validation_detail(workspace_id: str, table_name: str, payload: dict = Body(...)):
    df = _frame(workspace_id, table_name)
    return validation.detail_payload(df, payload.get("rule") or {})


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate/export")
async def export_failures(workspace_id: str, table_name: str, payload: dict = Body(...)):
    rule = payload.get("rule") or {}
    failures = validation.rule_failures(_frame(workspace_id, table_name), rule)
    slug = workspaces.slugify(validation.rule_label(rule)).replace("-", "_")
    return _excel_response(failures, f"{table_name}_{slug}_failures.xlsx")


@router.post("/workspaces/{workspace_id}/tables/{table_name}/validate/report")
async def export_report(workspace_id: str, table_name: str, payload: dict = Body(...)):
    df = _frame(workspace_id, table_name)
    run = validation.run_rules(df, payload.get("rules") or [], table_name)
    return _excel_response(
        validation.report_frame(run), f"{table_name}_validation_report.xlsx"
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
