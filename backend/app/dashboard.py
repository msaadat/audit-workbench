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


def tile_payload(workspace: Workspace, tile: dict) -> dict:
    payload = {
        key: tile.get(key)
        for key in ("id", "title", "kind", "table", "note", "viz", "created")
    }
    try:
        if tile["kind"] == "python":
            code = (tile.get("spec") or {}).get("code") or ""
            payload["code"] = code
            result, stdout = sandbox.run(code, _python_frames(workspace))
            payload["stdout"] = stdout or None
            payload["total_rows"] = result.height
            payload["frame"] = explore.frame_payload(result, _cap_for(payload["viz"]))
            payload["error"] = None
            return payload

        frame = workspace.get_frame(tile["table"])
        if tile["kind"] == "query":
            result, _ = explore.run_query_full(frame, tile.get("spec") or {})
            payload["total_rows"] = result.height
            payload["frame"] = explore.frame_payload(result, _cap_for(payload["viz"]))
        else:
            spec = tile.get("spec") or {}
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


def dashboard_payload(workspace: Workspace) -> dict:
    return {"tiles": [tile_payload(workspace, tile) for tile in workspace.tiles]}
