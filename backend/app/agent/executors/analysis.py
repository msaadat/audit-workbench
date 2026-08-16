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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ... import analysis_promotion, analysis_results, data_tests, sandbox
from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ...workspaces import Workspace, WorkspaceError, slugify
from .. import joins as join_diagnostics
from ..capabilities.analysis import ANALYSIS_SUMMARY_REF, frame_ref
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
REGISTER_EXECUTOR_ID = "analysis.register"
EXECUTION_EXECUTOR_ID = "analysis.execution"
SUMMARY_EXECUTOR_ID = "analysis.summary"
PROMOTION_EXECUTOR_ID = "analysis.promotion"

AUDITOR_ANALYSIS_PRESERVED = "auditor_owned_analysis_preserved"
# Every accepted definition was run and none of them separated anything. Like
# ``NOTHING_NEW_TO_ANALYSE`` this is an answer about the data rather than a
# contract violation — the frame supports no procedure that distinguishes some
# of its rows from the rest — so the unit settles instead of failing the run.
NO_INFORMATIVE_ANALYSIS = "no_informative_analysis"
# Every accepted definition already exists against another frame built from the
# same tables. Nothing is written and nothing is wrong: the computation is
# saved, just not here.
ANALYSIS_COVERED_ELSEWHERE = "analysis_covered_by_related_frame"
AMBIGUOUS_RELATIONSHIP = "ambiguous_relationship_requires_confirmation"
NO_SAFE_RELATIONSHIP = "no_safe_join_evidence"

# Re-exported from the result contract, which owns the bound.
MAX_RESULT_STATS = analysis_results.MAX_RESULT_STATS


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


def _side_refs(workspace: Workspace, target: JoinExecutorTarget) -> set[str]:
    """Both source frames' parent references, by what each frame is.

    A chained join's fact side is itself a join, whose durable entry projects
    under ``join:``; guarding it as ``table:`` would hash a missing entry and
    leave the commit unguarded.
    """
    return {
        frame_ref(workspace, target.left),
        frame_ref(workspace, target.right),
    }


def _validated_join(
    request: ExecutorRequest, target: object
) -> tuple[JoinExecutorTarget, dict]:
    if not isinstance(target, JoinExecutorTarget):
        raise WorkspaceError("Join executor requires a JoinExecutorTarget.")
    if set(request.expected_parents) != _side_refs(target.workspace, target):
        raise WorkspaceError(
            "Join executor requires exactly both source-frame parent hashes."
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
ANALYSIS_FIELDS = ("title", "kind", "table", "spec", "note", "outcome_policy")


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


def _validate_generated_python_definitions(
    workspace: Workspace, definitions: list[dict]
) -> tuple[list[dict], list[str]]:
    """Run generated Polars snippets locally before making them durable.

    Static sandbox validation prevents unsafe code, but it cannot detect a
    Polars API mismatch, an invalid expression, or a join-schema collision.
    Execute each accepted generated snippet against the local workspace frames
    before it becomes an analysis record.  This is local-only and uses exactly
    the same guarded sandbox that later renders and runs saved analyses.

    Dropped rather than raised, like a repeat or a single-sided spec: a bug in
    one generated snippet is not evidence against the others in the same
    response, and the worker had no execution feedback to have caught it
    itself. Raising here would cost the whole frame's harvest — including
    every analytics-kind proposal, which cannot fail this check at all — over
    one broken python spec.
    """
    frames = {}
    for name in workspace.table_names():
        try:
            frames[name] = workspace.get_frame(name)
        except Exception:
            continue
    kept: list[dict] = []
    dropped: list[str] = []
    for definition in definitions:
        if definition.get("kind") != "python":
            kept.append(definition)
            continue
        code = str((definition.get("spec") or {}).get("code") or "")
        try:
            sandbox.run(code, frames)
        except sandbox.SandboxError as error:
            title = str(definition.get("title") or "generated Python analysis")
            dropped.append(f"'{title}' failed local validation: {error}")
            continue
        kept.append(definition)
    return kept, dropped


def _screen_uninformative_definitions(
    workspace: Workspace, definitions: list[dict]
) -> tuple[list[dict], list[str]]:
    """Run each proposal once and drop the ones that establish nothing.

    A proposal is a hypothesis about the data, and whether it holds is not
    knowable from the schema the worker was shown: two date columns are two
    date columns whether or not the events they record are in the same chain.
    Running it is what answers that, and running it here — before it becomes a
    saved procedure with a verdict, a row count, and a place in the memo — is
    what stops a comparison between unrelated columns from being narrated as a
    systemic finding downstream.

    Dropped rather than raised, like a repeat or a single-sided spec: the rest
    of the response is still good work.
    """
    kept: list[dict] = []
    dropped: list[str] = []
    for definition in definitions:
        try:
            result = analysis_results.execute_analysis(
                workspace, definition, run_id="screening"
            ).result
        except Exception:
            # A proposal that will not run is not this screen's business. It
            # either failed python validation above or will record its own
            # error when the execution stage runs it.
            kept.append(definition)
            continue
        if result.get("informative", True):
            kept.append(definition)
            continue
        dropped.append(
            f"'{definition.get('title') or definition.get('semantic_id')}' "
            f"{result.get('uninformative_reason')}"
        )
    return kept, dropped


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
    covered: list[str] | None = None,
    dropped: list[str] | None = None,
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
            "covered": list(covered or []),
            "dropped": list(dropped or []),
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
    accepted, broken = _validate_generated_python_definitions(
        target.workspace, accepted
    )
    if not accepted:
        # Distinct from NO_INFORMATIVE_ANALYSIS below: every candidate here
        # failed to run at all, which is a bug in the generated code, not a
        # legitimate answer that this frame supports nothing. It stays a hard
        # failure so it is surfaced rather than read as a quiet "nothing here".
        raise WorkspaceError(
            "Generated Python analysis failed local validation: "
            + "; ".join(broken)
        )
    accepted, uninformative = _screen_uninformative_definitions(
        target.workspace, accepted
    )
    if not accepted:
        raise WorkspaceError(
            f"{NO_INFORMATIVE_ANALYSIS}: every proposed analysis was run and "
            "established nothing — " + "; ".join(broken + uninformative)
        )
    # The validator's own removals travel with the executor's, because they are
    # the same fact to a reader — the frame wrote more analyses than it kept —
    # and only this path runs in every mode. Reported from the approval callback
    # instead, they were silent on any run the auditor was not approving each
    # item by hand, which is every unattended run.
    dropped = [
        *(str(item) for item in request.proposal.get("declined") or []),
        *broken,
        *uninformative,
    ]
    state: dict[str, object] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        written, preserved, covered = _write_definitions(
            fresh,
            accepted,
            run_id=target.run_id,
            allow_auditor_overwrite=target.allow_auditor_overwrite,
        )
        state["preserved"] = preserved
        state["covered"] = covered
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
        covered=list(state.get("covered") or []),
        dropped=dropped,
    )


def _write_definitions(
    fresh: Workspace,
    accepted: list[dict],
    *,
    run_id: str,
    allow_auditor_overwrite: bool,
) -> tuple[list[dict], list[str], list[str]]:
    """Create or update each definition, honouring the auditor-edit boundary.

    Shared by the per-frame definition commit and the register commit. The two
    differ only in where their definitions came from and how many frames they
    span; what it means to write one — identity is the semantic id, an
    auditor-owned record is preserved, a computation already saved against a
    sibling frame stays where it is — is one rule and lives here.
    """
    written: list[dict] = []
    preserved: list[str] = []
    covered: list[str] = []
    creates: list[dict] = []
    for definition in accepted:
        semantic = str(definition["semantic_id"])
        existing = _existing_analysis(fresh, semantic)
        if existing is None:
            creates.append(definition)
            continue
        if existing.get("created_by") != "agent" and not allow_auditor_overwrite:
            preserved.append(str(existing["id"]))
            continue
        if str(existing.get("table") or "") != str(definition["table"]):
            # Identity is provenance-based, so this computation is already
            # saved against another frame built from the same tables. The
            # analysis stays where it is: rebinding it here would move a
            # result the auditor has already seen onto a different frame
            # without adding anything, since both frames compute it from
            # the same columns.
            covered.append(str(existing["id"]))
            continue
        existing.update(
            {
                "title": definition["title"],
                "kind": definition["kind"],
                "table": definition["table"],
                # Identity is the semantic id, derived from the kind, the
                # spec, and the tables its columns come from — a match here
                # means this spec is the one already stored, so there is no
                # viz to refresh: only a spec that actually changed would
                # need that, and a changed spec never matches an existing
                # semantic id, it creates a new analysis instead.
                "spec": dict(definition.get("spec") or {}),
                "note": str(definition.get("note") or ""),
                **(
                    {"outcome_policy": dict(definition["outcome_policy"])}
                    if isinstance(definition.get("outcome_policy"), Mapping)
                    else {}
                ),
                "semantic_id": semantic,
                "agent_run_id": run_id,
                "created_by": "agent",
            }
        )
        existing.pop("last_result", None)
        written.append({**existing, "action": "updated"})
    if not written and not creates:
        raise AnalysisEditPreserved(
            ANALYSIS_COVERED_ELSEWHERE if covered else AUDITOR_ANALYSIS_PRESERVED
        )
    for definition in creates:
        semantic = str(definition["semantic_id"])
        entry = fresh.add_analysis(
            {
                **{key: definition.get(key) for key in ANALYSIS_FIELDS},
                "id": analysis_stable_id(semantic),
                "semantic_id": semantic,
                "agent_run_id": run_id,
                "source": "ai",
            }
        )
        written.append({**entry, "action": "created"})
    return written, preserved, covered


@dataclass
class AnalysisRegisterExecutorTarget:
    """Mutable target for the register's own commit, across every frame.

    Unlike the definition target this one is not bound to a frame: the register
    is settled over the whole map at once, and each of its entries already
    names the frame its computation was measured on.
    """

    workspace: Workspace
    run_id: str
    frames: tuple[str, ...]
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Analysis register target requires a Workspace.")
        if not str(self.run_id or "").strip():
            raise ValueError("Analysis register target requires a run_id.")
        self.run_id = str(self.run_id).strip()
        self.frames = tuple(
            str(name).strip() for name in self.frames if str(name or "").strip()
        )
        if not self.frames:
            raise ValueError("Analysis register target requires at least one frame.")
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def execute_analysis_register(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit every kept register entry, across every frame, in one transaction.

    The entries are not proposals in the sense the definition executor's are.
    Each was run by the sweep before this stage existed, so there is no spec to
    validate against a schema and no generated code to try. What is still worth
    doing is the informativeness screen: a nomination measured on one frame is
    re-run here against the frame as it now stands, and one that turns out to
    flag its whole population establishes nothing about any row in it and is
    dropped with its reason recorded — the same rule, and the same code, that
    governs a model-authored proposal.

    One transaction rather than one per frame because the register is one
    decision. A crash halfway through would otherwise leave an engagement whose
    register says forty-three things and whose workspace holds nineteen of them.
    """
    if not isinstance(raw_target, AnalysisRegisterExecutorTarget):
        raise WorkspaceError(
            "Analysis register executor requires an AnalysisRegisterExecutorTarget."
        )
    target = raw_target
    raw = request.proposal.get("analyses")
    items = list(raw) if isinstance(raw, (list, tuple)) else []
    accepted: list[dict] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        definition = {
            key: _plain_json(entry[key]) for key in ANALYSIS_FIELDS if key in entry
        }
        semantic = str(entry.get("semantic_id") or "").strip()
        if not semantic:
            raise WorkspaceError("A register entry has no semantic id.")
        if not str(definition.get("table") or "").strip():
            raise WorkspaceError("A register entry names no frame.")
        if not str(definition.get("title") or "").strip():
            raise WorkspaceError("A register entry has no title.")
        definition["semantic_id"] = semantic
        accepted.append(definition)
    if not accepted:
        raise WorkspaceError("The assertion register is empty.")
    accepted, uninformative = _screen_uninformative_definitions(
        target.workspace, accepted
    )
    if not accepted:
        raise WorkspaceError(
            f"{NO_INFORMATIVE_ANALYSIS}: every register entry was re-run and "
            "established nothing — " + "; ".join(uninformative)
        )
    dropped = [
        *(str(item) for item in request.proposal.get("declined") or []),
        *uninformative,
    ]
    state: dict[str, object] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        written, preserved, covered = _write_definitions(
            fresh,
            accepted,
            run_id=target.run_id,
            allow_auditor_overwrite=target.allow_auditor_overwrite,
        )
        state["preserved"] = preserved
        state["covered"] = covered
        return written

    committed = mutate(
        target.workspace, commit, expected_parents=request.expected_parents
    )
    target.workspace = committed.workspace
    return _definitions_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        written=list(committed.value),
        preserved=list(state.get("preserved") or []),
        covered=list(state.get("covered") or []),
        dropped=dropped,
    )


def reconcile_analysis_register(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    """Classify an interrupted register commit.

    Writing analyses does not change the guarded frames, so a parent match
    cannot distinguish "not yet applied" from "applied". Identity does: every
    entry carries a semantic id, and the commit is complete exactly when each
    of them is present and stamped with this run.
    """
    if not isinstance(raw_target, AnalysisRegisterExecutorTarget):
        raise WorkspaceError(
            "Analysis register executor requires an AnalysisRegisterExecutorTarget."
        )
    raw = request.proposal.get("analyses")
    items = [item for item in (raw or ()) if isinstance(item, Mapping)]
    wanted = {
        str(item.get("semantic_id") or "").strip()
        for item in items
        if str(item.get("semantic_id") or "").strip()
    }
    current = Workspace(raw_target.workspace.root)
    present = {
        str(item.get("semantic_id") or "")
        for item in current.analyses
        if str(item.get("agent_run_id") or "") == raw_target.run_id
    }
    # A partial match is ``not_applied``: the commit is one transaction, so it
    # either landed whole or did not land, and re-running it is idempotent by
    # semantic id. Screening can legitimately drop entries between the proposal
    # and the commit, which is why the test is coverage rather than equality.
    if not wanted or not wanted <= present:
        return ExecutorReconciliation("not_applied")
    if current.revision <= request.expected_revision:
        return ExecutorReconciliation("not_applied")
    raw_target.workspace = current
    saved = [
        item
        for item in current.analyses
        if str(item.get("semantic_id") or "") in wanted
    ]
    return ExecutorReconciliation(
        "already_applied",
        result=_definitions_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            written=[{**item, "action": "created"} for item in saved],
            preserved=[],
        ),
        reason="The assertion register is already committed.",
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


# The bounded result contract is owned by ``app.analysis_results`` so the two
# origins of a result — this executor and the auditor's Run button — produce the
# same record by construction. Re-exported here because the executor, its
# reconciler, and the analysis bindings all address it under these names.
bounded_result = analysis_results.bounded_result
run_analysis = analysis_results.execute_analysis


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
    # The flagged rows travel in the proposal, so a resumed run commits the
    # evidence its own execution produced rather than recomputing the frame.
    raw_evidence = request.proposal.get("evidence")
    evidence = dict(_plain_json(raw_evidence)) if isinstance(raw_evidence, Mapping) else None
    writer = analysis_results.EvidenceWriter()

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        analysis = next(
            (item for item in fresh.analyses if str(item.get("id")) == target.analysis_id),
            None,
        )
        if analysis is None:
            raise WorkspaceError(f"Analysis '{target.analysis_id}' not found.")
        analysis["last_result"] = dict(result)
        writer.stage(fresh, target.analysis_id, evidence)
        return analysis

    try:
        committed = mutate(
            target.workspace,
            commit,
            expected_parents=request.expected_parents,
        )
    except Exception:
        writer.rollback()
        raise
    writer.finish()
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


# --------------------------------------------------------------------------- #
# analysis.summarized
# --------------------------------------------------------------------------- #
@dataclass
class AnalysisSummaryExecutorTarget:
    """Mutable target for the one analysis-summary commit."""

    workspace: Workspace
    run_id: str

    def __post_init__(self) -> None:
        if not str(self.run_id or "").strip():
            raise ValueError("Analysis summary target requires a run_id.")


def _validated_summary(
    request: ExecutorRequest, target: object
) -> tuple[AnalysisSummaryExecutorTarget, dict]:
    if not isinstance(target, AnalysisSummaryExecutorTarget):
        raise WorkspaceError(
            "Analysis summary executor requires an AnalysisSummaryExecutorTarget."
        )
    if set(request.expected_parents) != {ANALYSIS_SUMMARY_REF}:
        raise WorkspaceError(
            "Analysis summary executor requires exactly its summary parent hash."
        )
    markdown = str(request.proposal.get("markdown") or "").strip()
    if not markdown:
        raise WorkspaceError("The analysis summary proposal carries no markdown.")
    cited = [
        str(value)
        for value in (request.proposal.get("cited_analysis_ids") or [])
        if str(value or "").strip()
    ]
    return target, {"markdown": markdown, "cited_analysis_ids": cited}


def execute_analysis_summary(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit the analysis memo under its own parent guard.

    The memo is derived, not authored: there is no auditor-edit branch here
    because the artifact is read-only in the product. Regenerating replaces it
    outright, which is what makes it always agree with the results it cites.
    """
    target, proposal = _validated_summary(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        summary = {
            **proposal,
            # Stamped with the exact result set it was written from, so the
            # capability can tell "written and current" from "written, then the
            # results moved" without re-reading the prose.
            "basis_sha1": analysis_results.summary_basis_digest(fresh),
            "generated_at": _utcnow(),
            "run_id": target.run_id,
        }
        fresh.analysis_summary.clear()
        fresh.analysis_summary.update(summary)
        return summary

    committed = mutate(
        target.workspace, commit, expected_parents=request.expected_parents
    )
    target.workspace = committed.workspace
    refs = [ANALYSIS_SUMMARY_REF]
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=state["revision_before"],
        workspace_revision_after=committed.workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(committed.workspace, refs),
        output={
            "status": "ok",
            "action": "summarized",
            "cited": len(proposal["cited_analysis_ids"]),
            "characters": len(proposal["markdown"]),
        },
    )


def reconcile_analysis_summary(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    """Classify an interrupted summary commit.

    The commit changes the guarded artifact itself, so an unchanged parent
    proves it never ran. A changed parent is proven applied only when the memo
    now on the workspace is the exact prose this run proposed.
    """
    target, proposal = _validated_summary(request, raw_target)
    current = Workspace(target.workspace.root)
    actual = parent_hashes(current, [ANALYSIS_SUMMARY_REF])[ANALYSIS_SUMMARY_REF]
    if actual == request.expected_parents[ANALYSIS_SUMMARY_REF]:
        return ExecutorReconciliation("not_applied")
    stored = current.analysis_summary or {}
    if (
        str(stored.get("markdown") or "") == proposal["markdown"]
        and str(stored.get("run_id") or "") == target.run_id
    ):
        target.workspace = current
        refs = [ANALYSIS_SUMMARY_REF]
        return ExecutorReconciliation(
            "already_applied",
            result=ExecutorResult(
                executor_id=request.executor_id,
                capability_id=request.capability_id,
                unit_id=request.unit_id,
                workspace_revision_before=max(
                    request.expected_revision, current.revision - 1
                ),
                workspace_revision_after=current.revision,
                artifact_refs=refs,
                applied_parents=dict(request.expected_parents),
                postcondition_hashes=parent_hashes(current, refs),
                output={
                    "status": "ok",
                    "action": "summarized",
                    "cited": len(proposal["cited_analysis_ids"]),
                    "characters": len(proposal["markdown"]),
                },
            ),
            reason="This run's analysis summary already holds.",
        )
    return ExecutorReconciliation(
        "conflict",
        reason=str(
            ParentConflict(
                ANALYSIS_SUMMARY_REF,
                request.expected_parents[ANALYSIS_SUMMARY_REF],
                actual,
                current.revision,
            )
        ),
    )


# --------------------------------------------------------------------------- #
# analysis.promotion
# --------------------------------------------------------------------------- #
@dataclass
class PromotionExecutorTarget:
    """One saved procedure's fitting decision, and where it commits."""

    workspace: Workspace
    run_id: str
    analysis_id: str

    @property
    def parent_ref(self) -> str:
        return analysis_ref(self.analysis_id)


def _promotion_analysis(workspace: Workspace, analysis_id: str) -> dict:
    analysis = next(
        (
            item
            for item in workspace.analyses
            if str(item.get("id")) == str(analysis_id)
        ),
        None,
    )
    if analysis is None:
        raise WorkspaceError(f"Analysis '{analysis_id}' no longer exists.")
    return analysis


def _validated_promotion(
    request: ExecutorRequest, raw_target: object
) -> tuple[PromotionExecutorTarget, Mapping]:
    if not isinstance(raw_target, PromotionExecutorTarget):
        raise WorkspaceError("Analysis promotion requires its own target.")
    decision = request.proposal if isinstance(request.proposal, Mapping) else {}
    if "promote" not in decision:
        raise WorkspaceError("A promotion proposal must carry a decision.")
    return raw_target, decision


def execute_analysis_promotion(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit one procedure's disposition, and the test it became.

    Both halves land in one transaction guarded on the analysis. A promotion
    that wrote the test and not the disposition would re-promote on the next
    run; one that wrote the disposition and not the test would record the
    procedure as answered while nothing tests it. The second failure is the one
    this capability exists to prevent, so it may not be reachable through the
    capability's own commit.
    """
    target, decision = _validated_promotion(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        analysis = _promotion_analysis(fresh, target.analysis_id)
        sha1 = analysis_promotion.result_sha1(analysis)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not decision.get("promote"):
            analysis[analysis_promotion.PROMOTION_FIELD] = (
                analysis_promotion.declined_record(
                    result_sha1=sha1,
                    reason=str(decision.get("reason") or ""),
                    agent_run_id=target.run_id,
                    decided_at=now,
                )
            )
            fresh.save()
            return {"state": analysis_promotion.DECLINED, "test_id": ""}
        step = dict(decision.get("step") or {})
        semantic = f"datatest:promoted:{target.analysis_id}"
        item = data_tests.create(
            fresh,
            {
                "id": f"DAT-{analysis_stable_id(semantic)[4:]}",
                "semantic_id": semantic,
                "title": str(decision.get("title") or ""),
                "objective": str(decision.get("objective") or ""),
                "rcm_id": str(decision.get("rcm_id") or ""),
                "engine": "polars",
                "steps": [step],
                "spec": {"schema_version": 2, "steps": [step]},
                "agent_run_id": target.run_id,
                # Provenance the report and the coverage assertion both read:
                # this test exists because a saved procedure found something.
                "source_analysis_id": target.analysis_id,
            },
        )
        analysis = _promotion_analysis(fresh, target.analysis_id)
        analysis[analysis_promotion.PROMOTION_FIELD] = (
            analysis_promotion.promoted_record(
                result_sha1=sha1,
                test_id=str(item["id"]),
                rcm_id=str(decision.get("rcm_id") or ""),
                agent_run_id=target.run_id,
                decided_at=now,
            )
        )
        fresh.save()
        return {"state": analysis_promotion.PROMOTED, "test_id": str(item["id"])}

    committed = mutate(
        target.workspace, commit, expected_parents=request.expected_parents
    )
    target.workspace = committed.workspace
    outcome = committed.value
    refs = [target.parent_ref]
    if outcome["test_id"]:
        refs.append(f"datatest:{outcome['test_id']}")
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=state["revision_before"],
        workspace_revision_after=committed.workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(committed.workspace, refs),
        output={
            "status": "committed",
            "analysis_id": target.analysis_id,
            **outcome,
        },
    )


def reconcile_analysis_promotion(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    """Classify an interrupted promotion commit.

    The disposition is the evidence. It is written in the same transaction as
    the test and stamped with the ``result_sha1`` it answered, so its presence
    proves the whole commit landed and its absence proves none of it did.
    """
    target, _decision = _validated_promotion(request, raw_target)
    current = Workspace(target.workspace.root)
    parent_ref = target.parent_ref
    analysis = next(
        (
            item
            for item in current.analyses
            if str(item.get("id")) == str(target.analysis_id)
        ),
        None,
    )
    if analysis is None:
        return ExecutorReconciliation(
            "conflict", reason=f"Analysis '{target.analysis_id}' no longer exists."
        )
    if analysis_promotion.disposition(analysis) is not None:
        return ExecutorReconciliation(
            "applied", postcondition_hashes=parent_hashes(current, [parent_ref])
        )
    return ExecutorReconciliation("not_applied")


PROMOTION_EXECUTOR = ExecutorDefinition(
    executor_id=PROMOTION_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_analysis_promotion,
    reconciler=reconcile_analysis_promotion,
)


JOIN_EXECUTOR = ExecutorDefinition(
    executor_id=JOIN_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_join,
    reconciler=reconcile_join,
)
DEFINITIONS_EXECUTOR = ExecutorDefinition(
    executor_id=DEFINITIONS_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_analysis_definitions,
    reconciler=reconcile_analysis_definitions,
)
REGISTER_EXECUTOR = ExecutorDefinition(
    executor_id=REGISTER_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_analysis_register,
    reconciler=reconcile_analysis_register,
)
EXECUTION_EXECUTOR = ExecutorDefinition(
    executor_id=EXECUTION_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_analysis_run,
    reconciler=reconcile_analysis_run,
)

SUMMARY_EXECUTOR = ExecutorDefinition(
    executor_id=SUMMARY_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_analysis_summary,
    reconciler=reconcile_analysis_summary,
)

EXECUTORS.register(JOIN_EXECUTOR)
EXECUTORS.register(DEFINITIONS_EXECUTOR)
EXECUTORS.register(REGISTER_EXECUTOR)
EXECUTORS.register(EXECUTION_EXECUTOR)
EXECUTORS.register(SUMMARY_EXECUTOR)
EXECUTORS.register(PROMOTION_EXECUTOR)


__all__ = [
    "AMBIGUOUS_RELATIONSHIP",
    "ANALYSIS_FIELDS",
    "AUDITOR_ANALYSIS_PRESERVED",
    "AnalysisDefinitionExecutorTarget",
    "AnalysisEditPreserved",
    "AnalysisExecutionExecutorTarget",
    "AnalysisSummaryExecutorTarget",
    "DEFINITIONS_EXECUTOR",
    "DEFINITIONS_EXECUTOR_ID",
    "EXECUTION_EXECUTOR",
    "EXECUTION_EXECUTOR_ID",
    "JOIN_EXECUTOR",
    "JOIN_EXECUTOR_ID",
    "JoinExecutorTarget",
    "SUMMARY_EXECUTOR",
    "SUMMARY_EXECUTOR_ID",
    "MAX_RESULT_STATS",
    "NO_SAFE_RELATIONSHIP",
    "analysis_ref",
    "analysis_stable_id",
    "bounded_result",
    "execute_analysis_definitions",
    "execute_analysis_summary",
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
