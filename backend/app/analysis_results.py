"""The saved-analysis execution contract: run it, bound it, classify it.

One compact ``last_result`` per saved analysis is the durable, audit-meaningful
record of what a procedure concluded. This module owns that contract end to end
— computing it (:func:`execute_analysis`), projecting it
(:func:`bounded_result`), recording it (:func:`record_analysis_result`), and
giving it a consistent freshness and summary meaning — so the two callers that
produce results agree by construction rather than by convention: the auditor
pressing Run in the Analysis tab, and the analysis workflow's execution
capability.

Only the shape, verdict, and the analytics service's own bounded label/value
statistics are retained. The computed frame, its rows, and any stdout stay
local: a saved analysis is a rerunnable spec, and re-running it is how the data
is seen.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .workspace_transactions import (
    complete_linked_write,
    mutate,
    prepare_linked_write,
    rollback_linked_write,
)
from .workspaces import (
    Workspace,
    WorkspaceError,
    analysis_evidence_path,
    sync_workspace,
    write_json_atomic,
)


SUMMARY_CLASSES = (
    "exception",
    "unusual",
    "execution_error",
    "clear",
    "informational",
    "stale",
    "not_run",
)

# Stats are the only aggregate payload that crosses into durable state, and
# only as the bounded label/value pairs the analytics service already computed.
MAX_RESULT_STATS = 8

# Flagged rows are the second, and they are deliberately different in kind: a
# procedure that concludes "1 row is backdated" is not reviewable until the
# auditor can see *which* row. Every exception-producing analytics test already
# computes that frame; the cap matches ``data_tests.EXCEPTION_ROWS`` so a Data
# Test and a saved analysis retain the same amount of evidence for the same
# finding. The rows never live on the definition — see
# ``workspaces.analysis_evidence_path`` for why.
EXCEPTION_ROWS = 200

# An auditor-initiated execution is a first-class result, not a lesser one, but
# it belongs to no agent run. The prefix keeps the two origins distinguishable
# wherever a result is attributed without adding a second contract field.
MANUAL_RUN_PREFIX = "manual_"


def manual_run_id() -> str:
    """Return the run identity for an execution the auditor started."""

    return f"{MANUAL_RUN_PREFIX}{uuid.uuid4().hex[:12]}"


def is_manual_run(run_id: object) -> bool:
    return str(run_id or "").startswith(MANUAL_RUN_PREFIX)


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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bounded_result(
    payload: Mapping[str, object],
    *,
    run_id: str,
    workspace: Workspace,
    analysis: Mapping[str, object],
) -> dict:
    """Project a computed analysis payload onto the durable result contract.

    A python analysis has no verdict of its own — its code returns rows — so its
    declared ``outcome_policy`` supplies the audit meaning: under
    ``exception_rows`` any returned row is a potential exception, otherwise the
    result is informational.
    """
    frame = payload.get("frame") if isinstance(payload.get("frame"), Mapping) else {}
    stats = [
        {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
        for item in (payload.get("stats") or [])
        if isinstance(item, Mapping)
    ]
    error = payload.get("error")
    verdict = str(payload.get("verdict") or "") or None
    verdict_text = str(payload.get("verdict_text") or "") or None
    row_count = int(payload.get("total_rows") or 0)
    exception_count = 0 if error else int(payload.get("exception_rows") or 0)
    if not error and analysis.get("kind") == "python":
        policy = str((analysis.get("outcome_policy") or {}).get("mode") or "informational")
        if policy == "exception_rows":
            verdict = "warn" if row_count else "ok"
            verdict_text = (
                f"{row_count:,} potential exception row(s) returned."
                if row_count
                else "No potential exception rows returned."
            )
        else:
            verdict = "info"
            verdict_text = f"{row_count:,} result row(s) returned."
    record = {
        "run_id": run_id,
        "executed_at": _utcnow(),
        "status": "error" if error else "ok",
        "error": str(error) if error else None,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "row_count": row_count,
        "column_count": len(list((frame or {}).get("columns") or [])),
        "stat_count": len(stats),
        "stats": stats[:MAX_RESULT_STATS],
        # How many rows the procedure flagged, and how many of them the
        # evidence sidecar retained. The count is the population; the retained
        # slice is what an auditor can read back without re-running. Both
        # travel so a truncated sidecar can never be mistaken for the whole.
        "exception_count": exception_count,
        "exception_rows_retained": min(exception_count, EXCEPTION_ROWS),
        "input_sha1": analysis_input_sha1(workspace, analysis),
    }
    record["result_sha1"] = _sha1(
        {
            key: record[key]
            for key in (
                "status",
                "verdict",
                "row_count",
                "column_count",
                "stats",
                "exception_count",
                "input_sha1",
            )
        }
    )
    return record


@dataclass(frozen=True)
class AnalysisRun:
    """One local execution: what it concluded, and what it concluded it about.

    The two travel together because they are committed together. Splitting them
    is what would let a recorded conclusion and its supporting rows drift apart.
    """

    result: dict
    evidence: dict | None


def execute_analysis(
    workspace: Workspace, analysis: Mapping[str, object], *, run_id: str
) -> AnalysisRun:
    """Compute one saved analysis locally and return its bounded result.

    Execution goes through ``dashboard.compute_payload``, the same spec-not-data
    recomputation the Analysis tab and dashboard tiles use, so analytics run
    through the analytics registry and Polars code through the guarded local
    sandbox. Nothing leaves the machine.
    """
    # Imported here rather than at module scope: ``dashboard`` reaches the agent
    # capability registries, which read this module's classification helpers.
    from . import dashboard

    payload = dashboard.compute_payload(workspace, dict(analysis))
    result = bounded_result(
        payload, run_id=run_id, workspace=workspace, analysis=analysis
    )
    return AnalysisRun(
        result=result,
        evidence=exception_evidence(
            payload, analysis_id=str(analysis.get("id") or ""), result=result
        ),
    )


class EvidenceWriter:
    """Journalled exception-row writes bound to one workspace mutation.

    Both commit paths — the auditor's Run and the workflow's execution executor
    — stage their sidecar inside the guarded callback and resolve it on the way
    out, so the rows and the result they support share a single revision. An
    interrupted commit rolls the rows back instead of leaving evidence behind
    for a conclusion that was never recorded.
    """

    def __init__(self) -> None:
        self._writes: list = []

    def stage(
        self,
        workspace: Workspace,
        analysis_id: str,
        evidence: Mapping[str, object] | None,
    ) -> None:
        path = analysis_evidence_path(workspace.root, analysis_id)
        if evidence is None:
            # A procedure that now flags nothing must not keep the rows from
            # when it did.
            path.unlink(missing_ok=True)
            return
        payload = dict(_plain_json(evidence))
        self._writes.append(prepare_linked_write(workspace, path, payload))
        write_json_atomic(path, payload)

    # Named ``finish`` rather than ``complete``: the executor boundary test
    # rejects the substring ``.complete(`` anywhere in the analysis executors,
    # which is how it proves none of them can reach ``ModelGateway.complete``.
    # That guard is worth more than the nicer verb.
    def finish(self) -> None:
        for write in self._writes:
            complete_linked_write(write)
        self._writes.clear()

    def rollback(self) -> None:
        for write in reversed(self._writes):
            rollback_linked_write(write)
        self._writes.clear()


def exception_evidence(
    payload: Mapping[str, object], *, analysis_id: str, result: Mapping[str, object]
) -> dict | None:
    """Project the flagged rows one execution produced into its sidecar record.

    Stamped with the result identity it belongs to, so evidence left behind by
    an interrupted commit is detectably not the evidence for the result the
    definition currently carries.
    """
    frame = payload.get("exceptions")
    if not isinstance(frame, Mapping) or not int(result.get("exception_count") or 0):
        return None
    return {
        "analysis_id": analysis_id,
        "run_id": result.get("run_id"),
        "executed_at": result.get("executed_at"),
        "result_sha1": result.get("result_sha1"),
        "exception_count": int(result.get("exception_count") or 0),
        "frame": _plain_json(frame),
    }


def read_exception_evidence(workspace: Workspace, analysis: Mapping[str, object]) -> dict | None:
    """Read back the flagged rows the analysis' recorded result concluded about.

    Returns nothing unless the sidecar belongs to the result currently on the
    definition: evidence that outlived its conclusion is not evidence.
    """
    result = analysis.get("last_result")
    if not isinstance(result, Mapping) or not int(result.get("exception_count") or 0):
        return None
    path = analysis_evidence_path(workspace.root, str(analysis.get("id") or ""))
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, dict):
        return None
    if stored.get("result_sha1") != result.get("result_sha1"):
        return None
    return stored


def record_analysis_result(
    workspace: Workspace,
    analysis_id: str,
    result: Mapping[str, object],
    *,
    evidence: Mapping[str, object] | None = None,
) -> dict:
    """Persist one bounded result on its analysis under a revision guard.

    This is the auditor-initiated commit. It deliberately does *not* mark the
    analysis user-edited: executing a procedure exercises its definition, it
    does not take ownership of it, so a workflow-authored analysis an auditor
    ran by hand stays workflow-owned.

    ``evidence`` is the flagged rows this result concluded about. It is written
    as a journalled linked write so the sidecar and the recorded result share
    one workspace revision: an interrupted commit rolls the rows back rather
    than leaving evidence for a conclusion that was never recorded.
    """
    record = dict(_plain_json(result))
    writer = EvidenceWriter()

    def commit(fresh: Workspace) -> dict:
        analysis = next(
            (item for item in fresh.analyses if str(item.get("id")) == analysis_id), None
        )
        if analysis is None:
            raise WorkspaceError("Analysis not found.")
        analysis["last_result"] = record
        writer.stage(fresh, analysis_id, evidence)
        return analysis

    try:
        committed = mutate(workspace, commit)
    except Exception:
        writer.rollback()
        raise
    writer.finish()
    sync_workspace(workspace, committed.workspace)
    return committed.value


@dataclass(frozen=True)
class ExecutedAnalysis:
    """One auditor-initiated execution: what ran, what it concluded, what it saw.

    ``payload`` is the live render-ready computation including result rows; it
    stays in the response to the local browser and is never persisted. ``result``
    is the bounded record that was.
    """

    analysis: dict
    result: dict
    payload: dict


def execute_and_record(workspace: Workspace, analysis_id: str) -> ExecutedAnalysis:
    """Run one saved analysis for the auditor and record what it concluded.

    The computation happens before the guarded commit, so Polars work stays
    outside the workspace write lock — the same ordering the workflow executor
    uses.
    """
    from . import dashboard

    analysis = next(
        (item for item in workspace.analyses if str(item.get("id")) == analysis_id), None
    )
    if analysis is None:
        raise WorkspaceError("Analysis not found.")
    payload = dashboard.compute_payload(workspace, dict(analysis))
    result = bounded_result(
        payload, run_id=manual_run_id(), workspace=workspace, analysis=analysis
    )
    recorded = record_analysis_result(
        workspace,
        analysis_id,
        result,
        evidence=exception_evidence(
            payload, analysis_id=analysis_id, result=result
        ),
    )
    return ExecutedAnalysis(analysis=recorded, result=result, payload=payload)


def summary_basis_digest(workspace: Workspace) -> str:
    """Fingerprint the result set an analysis summary would be written from.

    A memo is only as current as the conclusions it read. The digest covers
    every saved analysis and the exact result recorded against it, so adding a
    procedure, deleting one, re-running one, or editing a definition (which
    drops its result) all move the digest and mark the memo stale. Definition
    text is deliberately absent: retitling a procedure does not change what the
    analysis found.
    """
    return _sha1(
        sorted(
            (
                str(item.get("id") or ""),
                str((item.get("last_result") or {}).get("result_sha1") or ""),
            )
            for item in workspace.analyses
        )
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


# Every classification's triage bucket. The Analysis tab's filter pills and the
# summary counts both read this, so a procedure can never sit in one bucket on
# one surface and a different bucket on another. Exception and unusual are
# their own buckets — not merged into one "needs review" bucket — because they
# carry different severity and an auditor triages them separately.
CLASSIFICATION_BUCKETS: dict[str, str] = {
    "exception": "exception",
    "unusual": "unusual",
    "execution_error": "errors",
    "clear": "clear",
    "informational": "informational",
    "stale": "stale",
    "not_run": "not_run",
}


def analysis_state(workspace: Workspace, analysis: Mapping[str, object]) -> dict:
    """Return the durable freshness and triage meaning of one saved analysis.

    This is the single answer to "what did this procedure conclude?". The rail,
    the summary, and the detail header all read it, so none of them can disagree
    with another about the same record.
    """
    raw_result = analysis.get("last_result")
    result = dict(raw_result) if isinstance(raw_result, Mapping) else None
    state = analysis_result_state(workspace, analysis)
    classification = _classification(result, state)
    return {
        "state": state,
        "classification": classification,
        "bucket": CLASSIFICATION_BUCKETS[classification],
    }


def analyses_summary_payload(workspace: Workspace) -> dict:
    """Return an engagement-level, data-free view of saved analysis outcomes."""
    counts = {
        "exception": 0,
        "unusual": 0,
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
        state = analysis_state(workspace, analysis)
        counts[state["bucket"]] += 1
        stats = [
            {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
            for item in (result.get("stats") or []) if isinstance(item, Mapping)
        ][:MAX_RESULT_STATS] if result else []
        items.append(
            {
                "analysis_id": str(analysis.get("id") or ""),
                "title": str(analysis.get("title") or "Untitled analysis"),
                "table": analysis.get("table"),
                "kind": str(analysis.get("kind") or ""),
                "source": str(analysis.get("source") or ""),
                "classification": state["classification"],
                "state": state["state"],
                "run_id": result.get("run_id") if result else None,
                "manual": is_manual_run(result.get("run_id")) if result else False,
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
    "CLASSIFICATION_BUCKETS",
    "EXCEPTION_ROWS",
    "MANUAL_RUN_PREFIX",
    "MAX_RESULT_STATS",
    "SUMMARY_CLASSES",
    "AnalysisRun",
    "EvidenceWriter",
    "ExecutedAnalysis",
    "analyses_summary_payload",
    "analysis_input_sha1",
    "analysis_result_state",
    "analysis_state",
    "bounded_result",
    "exception_evidence",
    "execute_analysis",
    "execute_and_record",
    "is_manual_run",
    "manual_run_id",
    "read_exception_evidence",
    "record_analysis_result",
    "summary_basis_digest",
]
