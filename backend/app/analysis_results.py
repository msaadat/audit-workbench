"""Bounded, local-only projections of saved analysis execution results.

The analysis workflow persists one compact ``last_result`` per saved analysis.
This module gives that record a consistent freshness and summary meaning without
ever loading a result frame, executing a procedure, or returning its code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .workspaces import Workspace


SUMMARY_CLASSES = (
    "exception",
    "unusual",
    "execution_error",
    "clear",
    "informational",
    "stale",
    "not_run",
)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _sha1(value: object) -> str:
    encoded = json.dumps(
        _plain_json(value), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def analysis_input_sha1(workspace: Workspace, analysis: Mapping[str, object]) -> str:
    """Fingerprint the definition and local frames an execution depended on.

    Python analyses receive all workspace frames in the sandbox.  They may use
    bare frame variables, so their dependency set is deliberately conservative:
    every available frame participates in the fingerprint.  That can request an
    extra rerun after unrelated data changes, but cannot label a result current
    when an untracked input changed.
    """
    kind = str(analysis.get("kind") or "")
    if kind == "python":
        names = sorted(workspace.table_names())
    else:
        table = str(analysis.get("table") or "").strip()
        names = [table] if table else []
    signatures: dict[str, object] = {}
    for name in names:
        try:
            signatures[name] = workspace._table_signature(name)
        except Exception as error:  # A removed/broken frame makes the old result stale.
            signatures[name] = {"unavailable": str(error)}
    return _sha1(
        {
            "kind": kind,
            "spec": analysis.get("spec") or {},
            "outcome_policy": analysis.get("outcome_policy") or {},
            "inputs": signatures,
        }
    )


def analysis_result_state(workspace: Workspace, analysis: Mapping[str, object]) -> str:
    """Return ``not_run``, ``current``, or ``stale`` for an analysis result."""
    result = analysis.get("last_result")
    if not isinstance(result, Mapping):
        return "not_run"
    recorded = str(result.get("input_sha1") or "")
    # Results created before this feature remain usable.  Definition edits
    # clear them; all newly executed results receive a full input fingerprint.
    if not recorded:
        return "current"
    return "current" if recorded == analysis_input_sha1(workspace, analysis) else "stale"


def _classification(result: Mapping[str, object] | None, state: str) -> str:
    if result is None:
        return "not_run"
    if state == "stale":
        return "stale"
    if result.get("status") == "error":
        return "execution_error"
    verdict = str(result.get("verdict") or "")
    if verdict == "fail":
        return "exception"
    if verdict == "warn":
        return "unusual"
    if verdict == "ok":
        return "clear"
    return "informational"


def analyses_summary_payload(workspace: Workspace) -> dict:
    """Return an engagement-level, data-free view of saved analysis outcomes."""
    counts = {
        "needs_review": 0,
        "errors": 0,
        "clear": 0,
        "informational": 0,
        "stale": 0,
        "not_run": 0,
    }
    items: list[dict] = []
    for analysis in workspace.analyses:
        raw_result = analysis.get("last_result")
        result = dict(raw_result) if isinstance(raw_result, Mapping) else None
        state = analysis_result_state(workspace, analysis)
        classification = _classification(result, state)
        bucket = {
            "exception": "needs_review",
            "unusual": "needs_review",
            "execution_error": "errors",
            "clear": "clear",
            "informational": "informational",
            "stale": "stale",
            "not_run": "not_run",
        }[classification]
        counts[bucket] += 1
        stats = [
            {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
            for item in (result.get("stats") or []) if isinstance(item, Mapping)
        ][:8] if result else []
        items.append(
            {
                "analysis_id": str(analysis.get("id") or ""),
                "title": str(analysis.get("title") or "Untitled analysis"),
                "table": analysis.get("table"),
                "kind": str(analysis.get("kind") or ""),
                "source": str(analysis.get("source") or ""),
                "classification": classification,
                "state": state,
                "run_id": result.get("run_id") if result else None,
                "executed_at": result.get("executed_at") if result else None,
                "status": result.get("status") if result else None,
                "verdict": result.get("verdict") if result else None,
                "verdict_text": result.get("verdict_text") if result else None,
                "error": result.get("error") if result else None,
                "row_count": int(result.get("row_count") or 0) if result else 0,
                "stats": stats,
                "result_sha1": result.get("result_sha1") if result else None,
            }
        )
    rank = {
        "exception": 0,
        "unusual": 1,
        "execution_error": 2,
        "stale": 3,
        "not_run": 4,
        "informational": 5,
        "clear": 6,
    }
    # Stable sorts give severity first and the newest result within it.
    items.sort(key=lambda item: str(item.get("executed_at") or ""), reverse=True)
    items.sort(key=lambda item: rank[item["classification"]])
    return {"counts": counts, "items": items}


__all__ = [
    "SUMMARY_CLASSES",
    "analysis_input_sha1",
    "analysis_result_state",
    "analyses_summary_payload",
]
