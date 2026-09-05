"""Recomputing a saved analysis into something renderable.

An analysis stores a *spec*, never data, so this is what makes one live: the
spec is re-run against the current frames each time it is read. A broken spec —
a deleted table, a renamed column — degrades to an error card rather than
failing the request around it.

Row caps depend on the visualization: charts get few points, tables a page.

This and `engagement_progress` were one module called `dashboard` until the
dashboard was removed. They shared a filename and nothing else: the two halves
had disjoint consumers, and only `counted` in common.
"""

from __future__ import annotations

from . import analysis_results, analytics, explore, sandbox
from .workspaces import Workspace
from .text import counted

VIZ_ROW_CAPS = {"bar": 30, "pie": 12, "line": 500, "table": 50}


def _cap_for(viz: dict) -> int:
    return VIZ_ROW_CAPS.get(str(viz.get("type") or "table"), 50)


def _frame_height(workspace: Workspace, name: object) -> int | None:
    """The row count of a named frame, or nothing if it will not resolve.

    A missing denominator is reported as missing. Substituting a plausible one
    would be worse than having none: it would read as measured.
    """
    text = str(name or "").strip()
    if not text:
        return None
    try:
        return workspace.get_frame(text).height
    except Exception:
        return None


def compute_payload(workspace: Workspace, item: dict) -> dict:
    """Recompute a saved analysis's stored spec into a render-ready payload.

    An analysis stores a spec, not data, so this is what makes it live: a
    broken item degrades to an error card instead of raising."""
    payload = {
        key: item.get(key)
        for key in ("id", "title", "kind", "table", "note", "viz", "created", "source", "spec", "alignment")
    }
    payload["exceptions"] = None
    payload["exception_rows"] = 0
    # The two denominators a conclusion needs. ``population`` is the frame the
    # procedure ran against; ``tested`` is how much of it the procedure could
    # actually evaluate, which is smaller wherever a key is null or a date
    # unparseable. Neither is derivable from ``total_rows`` — that is the size
    # of the *result*, which for an analytics test is its aggregate summary.
    payload["population"] = None
    payload["tested"] = None
    try:
        from .analysis_inputs import for_analysis, execution_frames
        workspace = for_analysis(workspace, item)
        if item["kind"] == "python":
            code = (item.get("spec") or {}).get("code") or ""
            payload["code"] = code
            result, stdout = sandbox.run(code, execution_frames(workspace, item))
            payload["stdout"] = stdout or None
            payload["total_rows"] = result.height
            payload["frame"] = explore.frame_payload(result, _cap_for(payload["viz"]))
            # Python code receives every frame and may use any of them, so the
            # declared frame is the best available population — and the one the
            # rows it returned are being counted against. Where it resolves, a
            # result that returned its whole declared frame becomes visible as
            # exactly that rather than as an exception count.
            payload["population"] = _frame_height(workspace, item.get("table"))
            # A python analysis has no detail frame of its own: its declared
            # outcome policy is what says whether the rows it returned are
            # exceptions, the same policy ``bounded_result`` reads for the
            # verdict. Under any other policy the rows are a result, not a
            # finding, and this payload carries no exceptions at all.
            if str((item.get("outcome_policy") or {}).get("mode") or "") == "exception_rows":
                payload["exceptions"] = explore.frame_payload(
                    result, analysis_results.EXCEPTION_ROWS
                )
                payload["exception_rows"] = result.height
            payload["error"] = None
            return payload

        frame = workspace.get_frame(item["table"])
        payload["population"] = frame.height
        # Only 'analytics' is left beside 'python': a saved analysis is one or
        # the other. The 'query', 'pivot' and 'validation' branches that stood
        # here served dashboard tiles, which were the only writers of those
        # kinds, and went with them.
        spec = item.get("spec") or {}
        result = analytics.run_test(
            frame,
            spec.get("test"),
            spec.get("params"),
            source=workspace.frame_source(),
        )
        # Analytics tiles use the test's own suggested visualization —
        # it tracks parameter changes (e.g. Benford digit count).
        payload["viz"] = result.viz or {"type": "table"}
        payload["verdict"] = result.verdict
        payload["verdict_text"] = result.verdict_text
        payload["stats"] = result.stats
        payload["tested"] = result.tested
        source = result.summary if result.summary is not None else result.detail
        if source is not None:
            payload["total_rows"] = source.height
            payload["frame"] = explore.frame_payload(source, _cap_for(payload["viz"]))
        else:
            payload["frame"] = None
        # ``frame`` is what the test concluded — the aggregate a chart is
        # drawn from. ``detail`` is which rows it concluded it about, and
        # every exception-producing test computes one. Collapsing the two
        # into a single frame is what previously left an auditor reading
        # "1 row is backdated" with no way to learn which invoice.
        if result.detail is not None:
            payload["exceptions"] = explore.frame_payload(
                result.detail, analysis_results.EXCEPTION_ROWS
            )
            payload["exception_rows"] = result.detail.height
        payload["error"] = None
    except Exception as error:
        payload["error"] = str(error)
    return payload


def analysis_export_frame(workspace: Workspace, analysis: dict):
    """Recompute one saved analysis into the full frame an export writes.

    The same spec, through the same services, without the display row caps —
    an export is the way the auditor takes the whole result off the screen, so
    it must not be the previewed slice of it.
    """
    import polars as pl

    from .analysis_inputs import for_analysis, execution_frames
    workspace = for_analysis(workspace, analysis)
    kind = str(analysis.get("kind") or "")
    if kind == "python":
        result, _ = sandbox.run(
            (analysis.get("spec") or {}).get("code") or "", execution_frames(workspace, analysis)
        )
        return result
    frame = workspace.get_frame(analysis["table"])
    spec = analysis.get("spec") or {}
    outcome = analytics.run_test(
        frame, spec.get("test"), spec.get("params"), source=workspace.frame_source()
    )
    exported = outcome.export_frame()
    if exported is not None:
        return exported
    # A test with no frame still has its statistics, which is what it concluded.
    return pl.DataFrame(
        {"stat": [item["label"] for item in outcome.stats],
         "value": [item["value"] for item in outcome.stats]}
    )


def analysis_listing(workspace: Workspace, analysis: dict) -> dict:
    """Describe one saved analysis without executing it.

    A rail entry needs identity, definition, and the outcome the last execution
    recorded — never result rows. Recomputing a spec is what the detail endpoint
    is for, so listing an engagement's procedures costs no Polars work at all.
    """
    listing = {
        key: analysis.get(key)
        for key in (
            "id", "title", "kind", "table", "note", "viz", "created", "source", "spec", "alignment",
        )
    }
    listing["outcome_policy"] = dict(analysis.get("outcome_policy") or {})
    listing["created_by"] = analysis.get("created_by")
    last_result = analysis.get("last_result")
    if isinstance(last_result, dict):
        listing["last_result"] = dict(last_result)
    return {**listing, **analysis_results.analysis_state(workspace, analysis)}


def analysis_payload(
    workspace: Workspace, analysis: dict, *, computed: dict | None = None
) -> dict:
    """One saved analysis, computed, with the outcome it durably recorded.

    ``computed`` lets a caller that has just executed the spec pass the result
    it already has, so recording an outcome does not cost a second computation
    of the same frame.
    """
    payload = dict(computed) if computed is not None else compute_payload(workspace, analysis)
    # The recomputation above is a live preview of what the spec returns *now*.
    # The bounded result record is what an execution durably concluded, and the
    # two can legitimately disagree — a live recomputation can fail after a
    # successful execution, or succeed after a failed one. Both travel, clearly
    # separated, and the recorded outcome is the one the UI presents as the
    # procedure's status.
    last_result = analysis.get("last_result")
    if isinstance(last_result, dict):
        payload["last_result"] = dict(last_result)
    payload["outcome_policy"] = dict(analysis.get("outcome_policy") or {})
    return {**payload, **analysis_results.analysis_state(workspace, analysis)}


def analyses_payload(workspace: Workspace) -> dict:
    """List every saved analysis. Definitions and outcomes only — no compute."""

    return {"analyses": [analysis_listing(workspace, a) for a in workspace.analyses]}
