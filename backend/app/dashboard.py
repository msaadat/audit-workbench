"""Dashboard tile computation.

Tiles store specs, not data — this module re-executes each tile's spec
against the current frames and returns render-ready payloads. A broken tile
(deleted table, renamed column) degrades to an error card instead of failing
the whole dashboard.

Row caps depend on the visualization: charts get few points, tables a page.
"""

from __future__ import annotations

from . import analytics, explore, sandbox
from .workspaces import Workspace

VIZ_ROW_CAPS = {"bar": 30, "pie": 12, "line": 500, "table": 50}


def _cap_for(viz: dict) -> int:
    return VIZ_ROW_CAPS.get(str(viz.get("type") or "table"), 50)


def _python_frames(workspace: Workspace) -> dict:
    frames = {}
    for name in workspace.table_names():
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            continue
    return frames


def compute_payload(workspace: Workspace, item: dict) -> dict:
    """Recompute a stored spec (tile or saved analysis) into a render-ready
    payload. Both collections store specs, not data, and share this logic; a
    broken item degrades to an error card instead of raising."""
    payload = {
        key: item.get(key)
        for key in ("id", "title", "kind", "table", "note", "viz", "created", "source", "spec")
    }
    try:
        if item["kind"] == "python":
            code = (item.get("spec") or {}).get("code") or ""
            payload["code"] = code
            result, stdout = sandbox.run(code, _python_frames(workspace))
            payload["stdout"] = stdout or None
            payload["total_rows"] = result.height
            payload["frame"] = explore.frame_payload(result, _cap_for(payload["viz"]))
            payload["error"] = None
            return payload

        frame = workspace.get_frame(item["table"])
        if item["kind"] == "query":
            result, _ = explore.run_query_full(frame, item.get("spec") or {})
            payload["total_rows"] = result.height
            payload["frame"] = explore.frame_payload(result, _cap_for(payload["viz"]))
        elif item["kind"] == "pivot":
            # Legacy pivot tiles (rows/columns/values) render through the query
            # engine's cross-tab now that pivot.py is gone; new cross-tabs pin as
            # 'query' tiles with split_by.
            spec = item.get("spec") or {}
            columns = spec.get("columns") or []
            if isinstance(columns, str):
                columns = [columns]
            wide, _grand, _meta = explore.build_crosstab(
                frame,
                filters=spec.get("filters"),
                row_fields=spec.get("rows") or [],
                split_field=columns[0] if columns else None,
                value_specs=spec.get("values"),
                totals=spec.get("totals", True),
            )
            payload["total_rows"] = wide.height
            payload["frame"] = explore.frame_payload(wide, _cap_for(payload["viz"]))
        else:
            spec = item.get("spec") or {}
            result = analytics.run_test(frame, spec.get("test"), spec.get("params"))
            # Analytics tiles use the test's own suggested visualization —
            # it tracks parameter changes (e.g. Benford digit count).
            payload["viz"] = result.viz or {"type": "table"}
            payload["verdict"] = result.verdict
            payload["verdict_text"] = result.verdict_text
            payload["stats"] = result.stats
            source = result.summary if result.summary is not None else result.detail
            if source is not None:
                payload["total_rows"] = source.height
                payload["frame"] = explore.frame_payload(source, _cap_for(payload["viz"]))
            else:
                payload["frame"] = None
        payload["error"] = None
    except Exception as error:
        payload["error"] = str(error)
    return payload


def tile_payload(workspace: Workspace, tile: dict) -> dict:
    return compute_payload(workspace, tile)


def dashboard_payload(workspace: Workspace) -> dict:
    return {"tiles": [compute_payload(workspace, tile) for tile in workspace.tiles]}


def analysis_payload(workspace: Workspace, analysis: dict) -> dict:
    return compute_payload(workspace, analysis)


def analyses_payload(workspace: Workspace) -> dict:
    return {"analyses": [compute_payload(workspace, a) for a in workspace.analyses]}
