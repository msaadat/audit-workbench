"""Deterministic executors for the exploratory data-analysis workflow.

Four concerns live here, none of which calls a model:

``infer_relationship`` diagnoses one table pair from local Polars evidence and
returns the aggregate metrics only — no relationship fact is ever generated.

``execute_join`` materializes at most one join per pair under a parent-hash CAS
on both source tables, and only when the evidence is strong enough to apply
without asking. ``execute_analysis_definitions`` commits accepted, rerunnable
analysis specs for one frame, deduplicated by their semantic identity and
preserving any definition the auditor took over. ``execute_analysis_run``
executes one saved definition through the existing local compute services and
commits only the bounded result contract — never result data.

Every mutating executor is registered with a reconciler, so an interrupted
commit is classified rather than repeated.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ... import dashboard
from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ...workspaces import Workspace, WorkspaceError, slugify
from .. import joins as join_diagnostics
from .model import (
    EXECUTORS,
    ExecutorConcurrency,
    ExecutorDefinition,
    ExecutorReconciliation,
    ExecutorRequest,
    ExecutorResult,
)

JOIN_EXECUTOR_ID = "analysis.join"
DEFINITIONS_EXECUTOR_ID = "analysis.definitions"
EXECUTION_EXECUTOR_ID = "analysis.execution"

AUDITOR_ANALYSIS_PRESERVED = "auditor_owned_analysis_preserved"
AMBIGUOUS_RELATIONSHIP = "ambiguous_relationship_requires_confirmation"
NO_SAFE_RELATIONSHIP = "no_safe_join_evidence"

# Stats are the only result payload that crosses into durable state, and only
# as the bounded label/value pairs the analytics service already computed.
MAX_RESULT_STATS = 8


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _canonical_sha1(value: object) -> str:
    encoded = json.dumps(
        _plain_json(value), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# data.relationships_inferred (P8.4)
# --------------------------------------------------------------------------- #
def relationship_ref(record: Mapping[str, object]) -> str:
    """The stable reference for one diagnosed relationship candidate."""

    left_on = "-".join(str(value) for value in record.get("left_on") or [])
    right_on = "-".join(str(value) for value in record.get("right_on") or [])
    return (
        f"relationship:{record.get('left')}:{record.get('right')}:{left_on}:{right_on}"
    )


def infer_relationship(workspace: Workspace, left: str, right: str) -> dict:
    """Diagnose one table pair from deterministic local evidence (read-only).

    Every value in the result is an aggregate metric produced by
    :mod:`agent.joins`: datatype compatibility, key uniqueness, match rate,
    unmatched populations, and the row-count effect of the join. No model is
    consulted and no relationship fact is generated — the classification is the
    same deterministic rule the manual join dialog uses.
    """

    existing = _existing_join(workspace, left, right)
    candidates = [
        {**candidate, "ref": relationship_ref(candidate)}
        for candidate in join_diagnostics.pair_candidates(workspace, left, right)
    ]
    strong = [item for item in candidates if item["strength"] == "strong"]
    moderate = [item for item in candidates if item["strength"] == "moderate"]
    return {
        "left": left,
        "right": right,
        "candidates": candidates,
        "strong": strong,
        "moderate": moderate,
        # An already-materialized join is itself the established relationship;
        # ``pair_candidates`` excludes its key pair from the candidate list.
        "join": str(existing.get("name")) if existing else None,
    }


def _existing_join(workspace: Workspace, left: str, right: str) -> dict | None:
    sides = {left, right}
    return next(
        (
            join
            for join in workspace.joins
            if {str(join.get("left")), str(join.get("right"))} == sides
        ),
        None,
    )


# --------------------------------------------------------------------------- #
# data.joins_ready (P8.5)
# --------------------------------------------------------------------------- #
def join_ref(name: str) -> str:
    """The stable artifact reference for one materialized join."""

    return f"join:{name}"


def join_semantic_id(record: Mapping[str, object]) -> str:
    return relationship_ref(record)


def join_name(workspace: Workspace, left: str, right: str) -> str:
    """A deterministic, collision-free name for a new join over two tables."""

    base = slugify(f"{left}-{right}-joined").replace("-", "_") or "joined"
    taken = set(workspace.table_names())
    if base not in taken:
        return base
    for suffix in range(2, 50):
        candidate = f"{base}_{suffix}"
        if candidate not in taken:
            return candidate
    raise WorkspaceError(f"Cannot derive a unique join name for '{base}'.")


@dataclass
class JoinExecutorTarget:
    """Mutable target for one table pair's join commit."""

    workspace: Workspace
    run_id: str
    left: str
    right: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Join executor target requires a Workspace.")
        for field_name in ("run_id", "left", "right"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Join executor target requires a {field_name}.")
            setattr(self, field_name, value)


def _validated_join(
    request: ExecutorRequest, target: object
) -> tuple[JoinExecutorTarget, dict]:
    if not isinstance(target, JoinExecutorTarget):
        raise WorkspaceError("Join executor requires a JoinExecutorTarget.")
    expected = {f"table:{target.left}", f"table:{target.right}"}
    if set(request.expected_parents) != expected:
        raise WorkspaceError(
            "Join executor requires exactly both source-table parent hashes."
        )
    raw = request.proposal.get("join")
    if not isinstance(raw, Mapping):
        raise WorkspaceError("The accepted join proposal is empty.")
    spec = {
        "left": str(raw.get("left") or ""),
        "right": str(raw.get("right") or ""),
        "how": str(raw.get("how") or "left"),
        "left_on": [str(value) for value in raw.get("left_on") or []],
        "right_on": [str(value) for value in raw.get("right_on") or []],
    }
    if {spec["left"], spec["right"]} != {target.left, target.right}:
        raise WorkspaceError("The accepted join proposal targets other tables.")
    if not spec["left_on"] or len(spec["left_on"]) != len(spec["right_on"]):
        raise WorkspaceError("The accepted join proposal has no paired keys.")
    return target, spec


def _matching_join(workspace: Workspace, spec: Mapping[str, object]) -> dict | None:
    semantic = join_semantic_id(spec)
    return next(
        (
            join
            for join in workspace.joins
            if join.get("semantic_id") == semantic
            or (
                str(join.get("left")) == spec["left"]
                and str(join.get("right")) == spec["right"]
                and list(join.get("left_on") or []) == list(spec["left_on"])
                and list(join.get("right_on") or []) == list(spec["right_on"])
            )
        ),
        None,
    )


def _join_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    entry: Mapping[str, object],
    action: str,
) -> ExecutorResult:
    refs = [join_ref(str(entry["name"]))]
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output={
            "status": action,
            "action": action,
            "name": str(entry["name"]),
            "semantic_id": str(entry.get("semantic_id") or ""),
            "left": str(entry.get("left") or ""),
            "right": str(entry.get("right") or ""),
        },
    )


def execute_join(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Materialize one diagnosed join under both source tables' parent guard.

    The join is created from the accepted key pair only. ``Workspace.add_join``
    executes the join once before persisting it, so an unexecutable spec fails
    the unit instead of leaving a broken derived table behind. The commit is
    idempotent by construction: the reconciler proves an interrupted attempt
    already applied before this ever runs a second time.
    """
    target, spec = _validated_join(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        existing = _matching_join(fresh, spec)
        if existing is not None:
            raise WorkspaceError(
                f"A join over '{spec['left']}' and '{spec['right']}' already exists."
            )
        return fresh.add_join(
            {
                **spec,
                # Named for the direction the evidence supports (fact table
                # first), not for the order the unit happened to pair them in.
                "name": join_name(fresh, spec["left"], spec["right"]),
                "agent_run_id": target.run_id,
                "semantic_id": join_semantic_id(spec),
            }
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _join_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        entry=committed.value,
        action="created",
    )


def reconcile_join(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted join commit without changing workspace state.

    Creating a join does not change either source table, so parent equality
    cannot prove the commit never ran. The join's semantic identity does: when a
    join for exactly this key pair exists, the commit is proven applied.
    """
    target, spec = _validated_join(request, raw_target)
    current = Workspace(target.workspace.root)
    for ref, expected in request.expected_parents.items():
        actual = parent_hashes(current, [ref])[ref]
        if actual != expected:
            return ExecutorReconciliation(
                "conflict",
                reason=str(ParentConflict(ref, expected, actual, current.revision)),
            )
    existing = _matching_join(current, spec)
    if existing is None:
        return ExecutorReconciliation("not_applied")
    if existing.get("agent_run_id") != target.run_id:
        return ExecutorReconciliation(
            "conflict",
            reason="A different join already connects these tables.",
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_join_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            entry=existing,
            action="created",
        ),
        reason="The accepted join already holds.",
    )


# --------------------------------------------------------------------------- #
# analysis.definitions_ready (P8.8)
# --------------------------------------------------------------------------- #
# The declared fields an accepted analysis proposal may write. Provenance,
# identity, and result state are owned by the executor, never the proposal.
ANALYSIS_FIELDS = ("title", "kind", "table", "spec", "note")


def analysis_ref(analysis_id: str) -> str:
    """The stable artifact reference for one saved analysis definition."""

    return f"analysis:{analysis_id}"


def analysis_stable_id(semantic_id: str) -> str:
    return "A-" + hashlib.sha1(semantic_id.encode("utf-8")).hexdigest()[:8].upper()


@dataclass
class AnalysisDefinitionExecutorTarget:
    """Mutable target for one frame's analysis-definition commit."""

    workspace: Workspace
    run_id: str
    target_frame: str
    parent_ref: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Analysis definition target requires a Workspace.")
        for field_name in ("run_id", "target_frame", "parent_ref"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Analysis definition target requires a {field_name}.")
            setattr(self, field_name, value)
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


class AnalysisEditPreserved(WorkspaceError):
    """The accepted proposal cannot silently replace an auditor-owned analysis."""


def _validated_definitions(
    request: ExecutorRequest, target: object
) -> tuple[AnalysisDefinitionExecutorTarget, list[dict]]:
    if not isinstance(target, AnalysisDefinitionExecutorTarget):
        raise WorkspaceError(
            "Analysis definition executor requires an AnalysisDefinitionExecutorTarget."
        )
    if set(request.expected_parents) != {target.parent_ref}:
        raise WorkspaceError(
            "Analysis definition executor requires exactly its target-frame parent hash."
        )
    raw = request.proposal.get("analyses")
    items = list(raw) if isinstance(raw, (list, tuple)) else []
    accepted: list[dict] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        definition = {key: _plain_json(entry[key]) for key in ANALYSIS_FIELDS if key in entry}
        definition["table"] = target.target_frame
        semantic = str(entry.get("semantic_id") or "").strip()
        if not semantic:
            raise WorkspaceError("An accepted analysis definition has no semantic id.")
        definition["semantic_id"] = semantic
        if not str(definition.get("title") or "").strip():
            raise WorkspaceError("An accepted analysis definition has no title.")
        accepted.append(definition)
    if not accepted:
        raise WorkspaceError("The accepted analysis proposal is empty.")
    return target, accepted


def _existing_analysis(workspace: Workspace, semantic_id: str) -> dict | None:
    stable = analysis_stable_id(semantic_id)
    return next(
        (
            item
            for item in workspace.analyses
            if item.get("semantic_id") == semantic_id or item.get("id") == stable
        ),
        None,
    )


def _definitions_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    written: list[dict],
    preserved: list[str],
) -> ExecutorResult:
    refs = [analysis_ref(str(item["id"])) for item in written]
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output={
            "status": "committed",
            "analyses": [
                {
                    "id": str(item["id"]),
                    "semantic_id": str(item.get("semantic_id") or ""),
                    "action": str(item.get("action") or "created"),
                    "title": str(item.get("title") or ""),
                }
                for item in written
            ],
            "preserved": list(preserved),
        },
    )


def execute_analysis_definitions(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit one frame's accepted analysis definitions in a single transaction.

    Definitions are specs, not data: each is rerunnable against the current
    frames. Identity is the proposal's semantic id, so re-running the workflow
    updates the same saved analysis instead of adding a second one, and a
    definition the auditor edited (``created_by`` flipped to ``user``) is
    preserved unless the run explicitly permits replacement.
    """
    target, accepted = _validated_definitions(request, raw_target)
    state: dict[str, object] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        written: list[dict] = []
        preserved: list[str] = []
        creates: list[dict] = []
        for definition in accepted:
            semantic = str(definition["semantic_id"])
            existing = _existing_analysis(fresh, semantic)
            if existing is None:
                creates.append(definition)
                continue
            if (
                existing.get("created_by") != "agent"
                and not target.allow_auditor_overwrite
            ):
                preserved.append(str(existing["id"]))
                continue
            existing.update(
                {
                    "title": definition["title"],
                    "kind": definition["kind"],
                    "table": definition["table"],
                    "spec": dict(definition.get("spec") or {}),
                    "note": str(definition.get("note") or ""),
                    "semantic_id": semantic,
                    "agent_run_id": target.run_id,
                    "created_by": "agent",
                }
            )
            written.append({**existing, "action": "updated"})
        if not written and not creates:
            raise AnalysisEditPreserved(AUDITOR_ANALYSIS_PRESERVED)
        for definition in creates:
            semantic = str(definition["semantic_id"])
            entry = fresh.add_analysis(
                {
                    **{key: definition.get(key) for key in ANALYSIS_FIELDS},
                    "id": analysis_stable_id(semantic),
                    "semantic_id": semantic,
                    "agent_run_id": target.run_id,
                    "source": "ai",
                }
            )
            written.append({**entry, "action": "created"})
        state["preserved"] = preserved
        return written

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _definitions_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        written=list(committed.value),
        preserved=list(state.get("preserved") or []),
    )


def reconcile_analysis_definitions(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted analysis-definition commit.

    Writing an analysis does not change the guarded frame, so parent equality
    cannot prove the commit never ran. The proposal's semantic identities do:
    when every accepted definition already exists under this run, the commit is
    proven applied. When every one exists but is auditor-owned, the run must not
    silently replace it, which is a conflict rather than a repeat.
    """
    target, accepted = _validated_definitions(request, raw_target)
    parent_ref = target.parent_ref
    current = Workspace(target.workspace.root)
    actual = parent_hashes(current, [parent_ref])[parent_ref]
    expected = request.expected_parents[parent_ref]
    if actual != expected:
        return ExecutorReconciliation(
            "conflict",
            reason=str(ParentConflict(parent_ref, expected, actual, current.revision)),
        )
    matches = [
        (definition, _existing_analysis(current, str(definition["semantic_id"])))
        for definition in accepted
    ]
    if any(existing is None for _definition, existing in matches):
        return ExecutorReconciliation("not_applied")
    if all(
        existing is not None and existing.get("created_by") != "agent"
        for _definition, existing in matches
    ) and not target.allow_auditor_overwrite:
        return ExecutorReconciliation("conflict", reason=AUDITOR_ANALYSIS_PRESERVED)
    if any(
        existing is not None and existing.get("agent_run_id") != target.run_id
        for _definition, existing in matches
    ):
        return ExecutorReconciliation("not_applied")
    if current.revision <= request.expected_revision:
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_definitions_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            written=[
                {**existing, "action": "created"}
                for _definition, existing in matches
                if existing is not None
            ],
            preserved=[],
        ),
        reason="The accepted analysis definitions already hold.",
    )


# --------------------------------------------------------------------------- #
# analysis.executed (P8.9)
# --------------------------------------------------------------------------- #
@dataclass
class AnalysisExecutionExecutorTarget:
    """Mutable target for one saved analysis' bounded-result commit."""

    workspace: Workspace
    run_id: str
    analysis_id: str
    result: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Analysis execution target requires a Workspace.")
        for field_name in ("run_id", "analysis_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Analysis execution target requires a {field_name}.")
            setattr(self, field_name, value)


def bounded_result(payload: Mapping[str, object], *, run_id: str) -> dict:
    """Project a computed analysis payload onto the durable result contract.

    Only shape, verdict, and the analytics service's own bounded label/value
    statistics are retained. The computed frame, its rows, and any stdout stay
    local: a saved analysis is a rerunnable spec, and re-running it is how the
    data is seen.
    """
    frame = payload.get("frame") if isinstance(payload.get("frame"), Mapping) else {}
    stats = [
        {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
        for item in (payload.get("stats") or [])
        if isinstance(item, Mapping)
    ]
    error = payload.get("error")
    record = {
        "run_id": run_id,
        "executed_at": _utcnow(),
        "status": "error" if error else "ok",
        "error": str(error) if error else None,
        "verdict": str(payload.get("verdict") or "") or None,
        "verdict_text": str(payload.get("verdict_text") or "") or None,
        "row_count": int(payload.get("total_rows") or 0),
        "column_count": len(list((frame or {}).get("columns") or [])),
        "stat_count": len(stats),
        "stats": stats[:MAX_RESULT_STATS],
    }
    record["result_sha1"] = _canonical_sha1(
        {key: record[key] for key in ("status", "verdict", "row_count", "column_count", "stats")}
    )
    return record


def run_analysis(workspace: Workspace, analysis: Mapping[str, object], *, run_id: str) -> dict:
    """Compute one saved analysis locally and return its bounded result.

    Execution goes through ``dashboard.compute_payload``, the same spec-not-data
    recomputation the Analysis tab and dashboard tiles use, so analytics run
    through the analytics registry and Polars code through the guarded local
    sandbox. Nothing leaves the machine.
    """
    payload = dashboard.compute_payload(workspace, dict(analysis))
    return bounded_result(payload, run_id=run_id)


def _validated_execution(
    request: ExecutorRequest, target: object
) -> tuple[AnalysisExecutionExecutorTarget, dict]:
    if not isinstance(target, AnalysisExecutionExecutorTarget):
        raise WorkspaceError(
            "Analysis execution executor requires an AnalysisExecutionExecutorTarget."
        )
    parent_ref = analysis_ref(target.analysis_id)
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Analysis execution executor requires exactly its analysis parent hash."
        )
    result = request.proposal.get("result")
    if not isinstance(result, Mapping) or not str(result.get("result_sha1") or ""):
        raise WorkspaceError("The analysis execution result is not a bounded record.")
    return target, dict(_plain_json(result))


def _execution_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    analysis_id: str,
    result: Mapping[str, object],
) -> ExecutorResult:
    refs = [analysis_ref(analysis_id)]
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output={
            "status": str(result.get("status") or "ok"),
            "id": analysis_id,
            "action": "executed",
            "result_sha1": str(result.get("result_sha1") or ""),
        },
    )


def execute_analysis_run(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one analysis' bounded result under its own parent guard.

    The computation already happened locally before the transaction opened, so
    the guarded section only records the bounded contract. That keeps Polars
    work outside the workspace write lock and makes the commit trivially
    replayable.
    """
    target, result = _validated_execution(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        analysis = next(
            (item for item in fresh.analyses if str(item.get("id")) == target.analysis_id),
            None,
        )
        if analysis is None:
            raise WorkspaceError(f"Analysis '{target.analysis_id}' not found.")
        analysis["last_result"] = dict(result)
        return analysis

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _execution_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        analysis_id=target.analysis_id,
        result=result,
    )


def reconcile_analysis_run(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted analysis-result commit.

    Recording the result changes the guarded analysis itself, so an unchanged
    parent proves the commit never ran. A changed parent is only proven applied
    when the current record carries this run's exact bounded result; anything
    else is an edit this run must not overwrite.
    """
    target, result = _validated_execution(request, raw_target)
    parent_ref = analysis_ref(target.analysis_id)
    current = Workspace(target.workspace.root)
    actual = parent_hashes(current, [parent_ref])[parent_ref]
    if actual == request.expected_parents[parent_ref]:
        return ExecutorReconciliation("not_applied")
    analysis = next(
        (item for item in current.analyses if str(item.get("id")) == target.analysis_id),
        None,
    )
    recorded = dict((analysis or {}).get("last_result") or {})
    if (
        analysis is not None
        and recorded.get("run_id") == target.run_id
        and recorded.get("result_sha1") == result.get("result_sha1")
    ):
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_execution_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
                analysis_id=target.analysis_id,
                result=recorded,
            ),
            reason="This run's analysis result already holds.",
        )
    return ExecutorReconciliation(
        "conflict",
        reason=str(
            ParentConflict(
                parent_ref,
                request.expected_parents[parent_ref],
                actual,
                current.revision,
            )
        ),
    )


JOIN_EXECUTOR = ExecutorDefinition(
    executor_id=JOIN_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_join)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_join)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_join,
    reconciler=reconcile_join,
)
DEFINITIONS_EXECUTOR = ExecutorDefinition(
    executor_id=DEFINITIONS_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_analysis_definitions)),
    reconciliation_hash=_sha256_text(
        inspect.getsource(reconcile_analysis_definitions)
    ),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_analysis_definitions,
    reconciler=reconcile_analysis_definitions,
)
EXECUTION_EXECUTOR = ExecutorDefinition(
    executor_id=EXECUTION_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_analysis_run)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_analysis_run)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_analysis_run,
    reconciler=reconcile_analysis_run,
)

EXECUTORS.register(JOIN_EXECUTOR)
EXECUTORS.register(DEFINITIONS_EXECUTOR)
EXECUTORS.register(EXECUTION_EXECUTOR)


__all__ = [
    "AMBIGUOUS_RELATIONSHIP",
    "ANALYSIS_FIELDS",
    "AUDITOR_ANALYSIS_PRESERVED",
    "AnalysisDefinitionExecutorTarget",
    "AnalysisEditPreserved",
    "AnalysisExecutionExecutorTarget",
    "DEFINITIONS_EXECUTOR",
    "DEFINITIONS_EXECUTOR_ID",
    "EXECUTION_EXECUTOR",
    "EXECUTION_EXECUTOR_ID",
    "JOIN_EXECUTOR",
    "JOIN_EXECUTOR_ID",
    "JoinExecutorTarget",
    "MAX_RESULT_STATS",
    "NO_SAFE_RELATIONSHIP",
    "analysis_ref",
    "analysis_stable_id",
    "bounded_result",
    "execute_analysis_definitions",
    "execute_analysis_run",
    "execute_join",
    "infer_relationship",
    "join_name",
    "join_ref",
    "join_semantic_id",
    "reconcile_analysis_definitions",
    "reconcile_analysis_run",
    "reconcile_join",
    "relationship_ref",
    "run_analysis",
]
