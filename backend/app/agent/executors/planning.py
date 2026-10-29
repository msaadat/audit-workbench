"""Deterministic executors for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ... import cycle_vouching, markdown_sections, planning_delta, planning_revision
from ...workspaces import Workspace, WorkspaceError, slugify, validate_cycle
from ..capabilities import _shared as audit_hashes
from ..artifact_index import canonical_id
from .model import (
    EXECUTORS,
    ExecutorConcurrency,
    ExecutorDefinition,
    ExecutorReconciliation,
    ExecutorRequest,
    ExecutorResult,
)


APM_EXECUTOR_ID = "planning.apm"
APM_PARENT_REF = "planning:context"
APM_ARTIFACT_REF = "planning:apm"
AUDITOR_EDIT_PRESERVED = "auditor_owned_apm_preserved"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


class ApmEditPreserved(WorkspaceError):
    """The accepted proposal cannot silently replace an auditor-owned APM."""


@dataclass
class ApmExecutorTarget:
    workspace: Workspace
    run_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("APM executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("APM executor target requires a run_id.")
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def _validated_input(
    request: ExecutorRequest,
    target: object,
) -> tuple[ApmExecutorTarget, str]:
    if not isinstance(target, ApmExecutorTarget):
        raise WorkspaceError("APM executor requires an ApmExecutorTarget.")
    if set(request.expected_parents) != {APM_PARENT_REF}:
        raise WorkspaceError(
            "APM executor requires exactly the planning-context parent hash."
        )
    markdown = str(request.proposal.get("apm_markdown") or "").strip()
    if not markdown:
        raise WorkspaceError("The accepted APM proposal is empty.")
    return target, markdown


def _result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
) -> ExecutorResult:
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[APM_ARTIFACT_REF],
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, [APM_ARTIFACT_REF]),
        output={
            "status": "updated",
            "created_by": "agent",
            "auditor_edits_preserved": True,
        },
    )


def execute_apm(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one accepted APM under the declared parent-hash guard."""
    target, markdown = _validated_input(request, raw_target)

    def commit(fresh: Workspace) -> None:
        existing = str(fresh.planning.get("apm_markdown") or "").strip()
        if (
            existing
            and fresh.planning.get("created_by") == "user"
            and not target.allow_auditor_overwrite
        ):
            raise ApmEditPreserved(AUDITOR_EDIT_PRESERVED)
        fresh.update_planning(
            {
                "apm_markdown": markdown,
                "created_by": "agent",
                "agent_run_id": target.run_id,
                "workflow_basis_sha1": audit_hashes.planning_basis_sha1(
                    fresh
                ),
                # The hashes this commit was guarded on, kept on the artifact.
                # Staleness and commit conflicts then agree by construction:
                # the memorandum is stale exactly when redrafting it would have
                # conflicted had the draft been in flight.
                "workflow_parents": dict(request.expected_parents),
            },
            agent=True,
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _result(
        request,
        committed.workspace,
        revision_before=committed.revision - 1,
    )


def reconcile_apm(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted APM commit without changing workspace state."""
    target, markdown = _validated_input(request, raw_target)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [APM_PARENT_REF])[APM_PARENT_REF]
    expected_parent = request.expected_parents[APM_PARENT_REF]
    if current_parent != expected_parent:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    APM_PARENT_REF,
                    expected_parent,
                    current_parent,
                    current.revision,
                )
            ),
        )
    existing = str(current.planning.get("apm_markdown") or "").strip()
    if (
        existing == markdown
        and current.planning.get("created_by") == "agent"
        and current.planning.get("agent_run_id") == target.run_id
        and current.planning.get("workflow_basis_sha1")
        == audit_hashes.planning_basis_sha1(current)
    ):
        if current.revision <= request.expected_revision:
            return ExecutorReconciliation(
                "conflict", reason="APM commit revision did not advance."
            )
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
            ),
            reason="The accepted APM postcondition already holds.",
        )
    if (
        existing
        and current.planning.get("created_by") == "user"
        and not target.allow_auditor_overwrite
    ):
        return ExecutorReconciliation(
            "conflict",
            reason=AUDITOR_EDIT_PRESERVED,
        )
    return ExecutorReconciliation("not_applied")


APM_EXECUTOR = ExecutorDefinition(
    executor_id=APM_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_apm,
    reconciler=reconcile_apm,
)

EXECUTORS.register(APM_EXECUTOR)


# --------------------------------------------------------------------------- #
# planning.cycle executor (step 5 of docs/rcm-generation-redesign.md)
# --------------------------------------------------------------------------- #
CYCLE_EXECUTOR_ID = "planning.cycle"
CYCLE_PARENT_REF = "planning:apm"
CYCLE_ARTIFACT_REF = "planning:cycle"
CYCLE_EDIT_PRESERVED = "auditor_owned_cycle_preserved"


class CycleEditPreserved(WorkspaceError):
    """The accepted shape cannot silently replace an auditor-owned cycle."""


@dataclass
class CycleExecutorTarget:
    workspace: Workspace
    run_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Cycle executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("Cycle executor target requires a run_id.")
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def _validated_cycle(
    request: ExecutorRequest,
    target: object,
) -> tuple[CycleExecutorTarget, dict]:
    if not isinstance(target, CycleExecutorTarget):
        raise WorkspaceError("Cycle executor requires a CycleExecutorTarget.")
    if set(request.expected_parents) != {CYCLE_PARENT_REF}:
        raise WorkspaceError(
            "Cycle executor requires exactly the memorandum parent hash."
        )
    shape = {
        key: request.proposal.get(key)
        for key in ("name", "steps", "cross_cutting")
    }
    if not shape.get("steps"):
        raise WorkspaceError("The accepted cycle proposal has no steps.")
    return target, shape


def _cycle_material(cycle: Mapping | None) -> dict | None:
    if not cycle:
        return None
    return {key: cycle.get(key) for key in ("name", "steps", "cross_cutting")}


def _cycle_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
) -> ExecutorResult:
    cycle = workspace.planning.get("cycle") or {}
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[CYCLE_ARTIFACT_REF],
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, [CYCLE_ARTIFACT_REF]),
        output={
            "status": "updated",
            "created_by": "agent",
            "steps": [
                str(step.get("name") or "") for step in cycle.get("steps") or []
            ],
            "auditor_edits_preserved": True,
        },
    )


def execute_cycle(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one accepted cycle shape under the memorandum parent-hash guard."""
    target, shape = _validated_cycle(request, raw_target)

    def commit(fresh: Workspace) -> None:
        existing = fresh.planning.get("cycle") or {}
        if (
            existing.get("steps")
            and existing.get("created_by") == "user"
            and not target.allow_auditor_overwrite
        ):
            raise CycleEditPreserved(CYCLE_EDIT_PRESERVED)
        # ``update_planning`` re-validates the shape against what this workspace
        # actually holds. The drafting gate checked it against the lists the
        # turn was handed; between the two a type could have been retyped or a
        # table dropped, and this is where that is caught rather than stored.
        fresh.update_planning(
            {
                "cycle": {
                    **shape,
                    "agent_run_id": target.run_id,
                    "created_by": "agent",
                    "workflow_parents": dict(request.expected_parents),
                }
            },
            agent=True,
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _cycle_result(
        request,
        committed.workspace,
        revision_before=committed.revision - 1,
    )


def reconcile_cycle(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted cycle commit without changing workspace state."""
    target, shape = _validated_cycle(request, raw_target)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [CYCLE_PARENT_REF])[CYCLE_PARENT_REF]
    expected_parent = request.expected_parents[CYCLE_PARENT_REF]
    if current_parent != expected_parent:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    CYCLE_PARENT_REF,
                    expected_parent,
                    current_parent,
                    current.revision,
                )
            ),
        )
    stored = current.planning.get("cycle") or {}
    # The proposal is compared through the same validator the commit used, so a
    # shape that normalizes to what is stored is recognised as already applied
    # rather than re-committed against a parent that has not moved.
    try:
        normalized = _cycle_material(validate_cycle(current, shape))
    except WorkspaceError:
        normalized = None
    if (
        normalized is not None
        and _cycle_material(stored) == normalized
        and stored.get("created_by") == "agent"
        and stored.get("agent_run_id") == target.run_id
    ):
        if current.revision <= request.expected_revision:
            return ExecutorReconciliation(
                "conflict", reason="Cycle commit revision did not advance."
            )
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_cycle_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
            ),
            reason="The accepted cycle postcondition already holds.",
        )
    if (
        stored.get("steps")
        and stored.get("created_by") == "user"
        and not target.allow_auditor_overwrite
    ):
        return ExecutorReconciliation("conflict", reason=CYCLE_EDIT_PRESERVED)
    return ExecutorReconciliation("not_applied")


CYCLE_EXECUTOR = ExecutorDefinition(
    executor_id=CYCLE_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_cycle,
    reconciler=reconcile_cycle,
)

EXECUTORS.register(CYCLE_EXECUTOR)



# --------------------------------------------------------------------------- #
# planning.context executor (P7A)
# --------------------------------------------------------------------------- #
PLANNING_CONTEXT_EXECUTOR_ID = "planning.context"
PLANNING_CONTEXT_REF = "planning:context"
# The synthesizable structured planning-context fields. Matches the fields the
# synthesis worker is permitted to write; provenance and free-form keys stay out.
PLANNING_CONTEXT_FIELDS = frozenset(
    {
        "objective",
        "entity",
        "period",
        "scope",
        "materiality",
        "key_contacts",
        "background_notes",
    }
)


@dataclass
class PlanningContextExecutorTarget:
    """Mutable target for the deterministic planning-context commit."""

    workspace: Workspace
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Planning-context executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("Planning-context executor target requires a run_id.")


def _validated_context(
    request: ExecutorRequest,
    target: object,
) -> tuple[PlanningContextExecutorTarget, dict[str, str], bool]:
    if not isinstance(target, PlanningContextExecutorTarget):
        raise WorkspaceError(
            "Planning-context executor requires a PlanningContextExecutorTarget."
        )
    if set(request.expected_parents) != {PLANNING_CONTEXT_REF}:
        raise WorkspaceError(
            "Planning-context executor requires exactly the planning-context parent hash."
        )
    raw = request.proposal.get("context")
    if not isinstance(raw, Mapping):
        raise WorkspaceError("The accepted planning-context proposal must carry a context object.")
    context = {
        key: str(value)
        for key, value in raw.items()
        if key in PLANNING_CONTEXT_FIELDS and str(value or "").strip()
    }
    if not context:
        raise WorkspaceError("The accepted planning context is empty.")
    # How the accepted fields were derived is recorded on the receipt so the
    # caller can surface a recovered synthesis without re-reading the proposal.
    recovered = bool(request.proposal.get("recovered_from_labelled_facts"))
    return target, context, recovered


def _context_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    context: dict[str, str],
    recovered: bool = False,
) -> ExecutorResult:
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[PLANNING_CONTEXT_REF],
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, [PLANNING_CONTEXT_REF]),
        output={
            "status": "updated",
            "fields": sorted(context),
            "recovered_from_labelled_facts": recovered,
        },
    )


def execute_planning_context(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Merge one accepted planning context under the parent-hash guard.

    The synthesized fields are merged into ``planning.context`` (auditor-entered
    fields outside the proposal are preserved by the merge), guarded by the
    planning-context parent hash so a concurrent context change is a conflict
    rather than a silent overwrite.
    """
    target, context, recovered = _validated_context(request, raw_target)

    def commit(fresh: Workspace) -> None:
        fresh.update_planning({"context": context}, agent=True)

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _context_result(
        request,
        committed.workspace,
        revision_before=committed.revision - 1,
        context=context,
        recovered=recovered,
    )


def reconcile_planning_context(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted planning-context commit without mutating state."""
    target, context, recovered = _validated_context(request, raw_target)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [PLANNING_CONTEXT_REF])[PLANNING_CONTEXT_REF]
    expected_parent = request.expected_parents[PLANNING_CONTEXT_REF]
    if current_parent == expected_parent:
        # The context material is unchanged: the guarded merge has not landed,
        # so a normal (idempotent) execution is safe.
        return ExecutorReconciliation("not_applied")
    current_context = current.planning.get("context") or {}
    if all(str(current_context.get(key)) == value for key, value in context.items()):
        if current.revision <= request.expected_revision:
            return ExecutorReconciliation(
                "conflict",
                reason="Planning-context commit revision did not advance.",
            )
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_context_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
                context=context,
                recovered=recovered,
            ),
            reason="The accepted planning context already holds.",
        )
    return ExecutorReconciliation(
        "conflict",
        reason="Planning context changed before the interrupted commit was reconciled.",
    )


PLANNING_CONTEXT_EXECUTOR = ExecutorDefinition(
    executor_id=PLANNING_CONTEXT_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_planning_context,
    reconciler=reconcile_planning_context,
)

EXECUTORS.register(PLANNING_CONTEXT_EXECUTOR)


# --------------------------------------------------------------------------- #
# planning.rcm executor (P7C)
# --------------------------------------------------------------------------- #
RCM_EXECUTOR_ID = "planning.rcm"
RCM_PARENT_REF = "planning:apm"
# The revision fields an accepted proposal may write onto a matched row.
RCM_ROW_FIELDS = (
    "process",
    "risk",
    "risk_rating",
    "business_cycle",
    "control_attributes",
    "control",
    "control_type",
)
# Narrative fields the model supplies only when the planning basis carries them.
# They are written on revision only when the proposal actually provides one, so
# a run that cannot cite a criterion never blanks an existing citation.
RCM_OPTIONAL_ROW_FIELDS = (
    "criteria",
    "criteria_refs",
    "control_owner",
)


def _resolved_criteria_refs(workspace: Workspace, spec: Mapping) -> list[dict]:
    """Freeze each cited criterion into a typed evidence anchor.

    The worker chooses a document and a citation id out of the register it was
    supplied; neither carries a page or the sentence itself. Resolving them
    here makes the row self-contained — a reader opens the criterion at the
    page it rests on without a second lookup — and freezes the excerpt and the
    source hash at the moment the criterion was set, so a later change to the
    document is visible as drift rather than silently rewriting the criterion.
    """
    from ... import document_analysis
    from ...evidence import document_anchor

    references = spec.get("criteria_refs")
    if not isinstance(references, list) or not references:
        return []
    by_id = {str(item.get("id")): item for item in workspace.documents}
    citations_by_document: dict[str, dict[str, Mapping]] = {}
    anchors: list[dict] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        document_id = str(reference.get("document_id") or "")
        citation_id = str(reference.get("citation_id") or "")
        document = by_id.get(document_id)
        if not document or not citation_id:
            continue
        if document_id not in citations_by_document:
            try:
                analysis = document_analysis.load_analysis(
                    workspace, document_id, document=document
                )
            except WorkspaceError:
                citations_by_document[document_id] = {}
            else:
                citations_by_document[document_id] = {
                    str(item.get("id")): item
                    for item in ((analysis.get("effective") or {}).get("citations") or [])
                }
        citation = citations_by_document[document_id].get(citation_id)
        if citation is None:
            # An id the register offered but the analysis no longer carries is
            # a stale citation, not a criterion to invent a page for.
            continue
        anchors.append({
            **document_anchor(
                document,
                int(citation.get("page") or 1),
                str(citation.get("excerpt") or ""),
                generated_by="planning.rcm",
            ),
            "citation_id": citation_id,
        })
    return anchors


@dataclass
class RcmExecutorTarget:
    """Mutable target for the deterministic RCM row commit."""

    workspace: Workspace
    run_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("RCM executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("RCM executor target requires a run_id.")
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def rcm_semantic_id(spec: Mapping[str, object]) -> str:
    return str(
        spec.get("semantic_id")
        or f"rcm:{slugify(str(spec.get('process') or ''))}:{slugify(str(spec.get('risk') or ''))}"
    )


def _words(value: object) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _similarity(left: object, right: object) -> float:
    left_text = " ".join(sorted(_words(left)))
    right_text = " ".join(sorted(_words(right)))
    left_words, right_words = _words(left), _words(right)
    overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
    return max(overlap, SequenceMatcher(None, left_text, right_text).ratio())


def _ranked_rcm_matches(
    workspace: Workspace,
    spec: Mapping[str, object],
) -> list[tuple[float, dict]]:
    """Rank existing rows against one proposed row's narrative, best first."""
    narrative = f"{spec.get('process', '')} {spec.get('risk', '')}"
    return sorted(
        (
            (
                _similarity(
                    narrative, f"{row.get('process', '')} {row.get('risk', '')}"
                ),
                row,
            )
            for row in workspace.rcm
        ),
        key=lambda item: (-item[0], str(item[1].get("id"))),
    )


def match_rcm_revision(
    workspace: Workspace,
    spec: Mapping[str, object],
    semantic: str,
) -> tuple[dict | None, bool]:
    """Match one proposed revision to an existing row: exact id, semantic id,
    then bounded narrative similarity with an explicit ambiguity outcome."""
    explicit = canonical_id(spec.get("rcm_id") or spec.get("id"), "rcm")
    if explicit:
        return (
            next((row for row in workspace.rcm if row.get("id") == explicit), None),
            False,
        )
    exact = workspace.find_semantic("rcm", semantic)
    if exact:
        return exact, False
    ranked = _ranked_rcm_matches(workspace, spec)
    if not ranked or ranked[0][0] < 0.72:
        return None, False
    ambiguous = len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08
    return (None, True) if ambiguous else (ranked[0][1], False)


def match_retired_rcm(
    workspace: Workspace,
    spec: Mapping[str, object],
) -> dict | None:
    """The retirement a proposed new row would undo, if any.

    The same bar :func:`match_rcm_revision` holds a revision to — exact semantic
    id, then narrative similarity — because a redraft rewords a risk as readily
    as it restates one, and a retired risk proposed again in new words is still
    the risk someone retired.
    """
    retired = workspace.retired_rcm_rows()
    if not retired:
        return None
    semantic = rcm_semantic_id(spec)
    exact = next(
        (entry for entry in retired if str(entry.get("semantic_id") or "") == semantic),
        None,
    )
    if exact is not None:
        return exact
    narrative = f"{spec.get('process', '')} {spec.get('risk', '')}"
    best = max(
        retired,
        key=lambda entry: _similarity(
            narrative, f"{entry.get('process', '')} {entry.get('risk', '')}"
        ),
    )
    score = _similarity(narrative, f"{best.get('process', '')} {best.get('risk', '')}")
    return best if score >= RETIRED_MATCH_THRESHOLD else None


#: The similarity at which a proposed row is taken to restate a retired one.
#: The revision matcher's own threshold, for the reason given above.
RETIRED_MATCH_THRESHOLD = 0.72


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _quarantined_rows(request: ExecutorRequest) -> list[dict]:
    """Return the rows the worker could not repair, for the receipt.

    An RCM is a set of independent rows, so one row that will not validate is not
    a reason to commit none of them. The dropped rows travel to the receipt with
    their reasons, which is what puts them in front of the auditor instead of
    leaving a silently shorter matrix.
    """

    raw = request.proposal.get("quarantined")
    if not isinstance(raw, (list, tuple)):
        return []
    return [_plain_json(item) for item in raw if isinstance(item, Mapping)]


def _ambiguous_quarantine(workspace: Workspace, spec: Mapping[str, object]) -> dict:
    """Project a row the matcher could not place into a quarantine entry.

    The candidate rows are named because the decision the auditor is being asked
    to make is which existing row this revises, if any. A bare "ambiguous" leaves
    them to re-derive the ranking by hand against the whole matrix.
    """

    # Ambiguity is only ever declared over two or more candidates, so the
    # re-ranking here has them. The fallback keeps the entry readable rather
    # than naming an empty list if this is ever called off that path.
    ranked = _ranked_rcm_matches(workspace, spec)
    named = [str(row.get("id")) for _score, row in ranked[:2] if row.get("id")]
    candidates = " and ".join(named) if named else "more than one existing row"
    return {
        "process": str(spec.get("process") or ""),
        "risk": str(spec.get("risk") or ""),
        "errors": [
            f"Ambiguous RCM revision: this row matches {candidates} equally "
            "well, so committing it would have overwritten one of them on a "
            "guess. Set rcm_id to revise a specific row, or reword the risk so "
            "it stands apart as a new one."
        ],
    }


def _validated_rcm(
    request: ExecutorRequest,
    target: object,
) -> tuple[RcmExecutorTarget, list[dict]]:
    if not isinstance(target, RcmExecutorTarget):
        raise WorkspaceError("RCM executor requires an RcmExecutorTarget.")
    # The cycle joins the memorandum as a guarded parent: the shape is where a
    # row's ``process`` vocabulary comes from, so a cycle reshaped under an
    # in-flight matrix must conflict this commit rather than let rows land
    # naming steps that no longer exist. It is optional only for the state
    # before any cycle has been designed.
    if not {RCM_PARENT_REF} <= set(request.expected_parents) <= {
        RCM_PARENT_REF,
        CYCLE_ARTIFACT_REF,
    }:
        raise WorkspaceError(
            "RCM executor requires the APM parent hash, and optionally the cycle's."
        )
    raw = request.proposal.get("rows")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise WorkspaceError("The accepted RCM proposal has no rows.")
    rows: list[dict] = []
    for value in raw:
        if not isinstance(value, Mapping):
            raise WorkspaceError("Each accepted RCM row must be an object.")
        spec = _plain_json(value)
        if not str(spec.get("risk") or "").strip():
            raise WorkspaceError("An accepted RCM row is missing its risk.")
        spec["risk_rating"] = str(spec.get("risk_rating") or "").lower()
        try:
            # The commit is where a schema-backed attribute becomes exact: this
            # is the turn that has the engagement in hand, so a field no schema
            # states is refused here rather than surfacing later as a cycle
            # test that cannot be generated.
            spec["control_attributes"] = cycle_vouching.validate_control_attributes(
                spec.get("control_attributes"), workspace=target.workspace
            )
        except cycle_vouching.CycleSchemaError as error:
            raise WorkspaceError(str(error)) from error
        spec["semantic_id"] = rcm_semantic_id(spec)
        rows.append(spec)
    return target, rows


def _rcm_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    outcomes: list[dict],
    ambiguous: list[dict] | None = None,
    retired_skipped: list[dict] | None = None,
) -> ExecutorResult:
    changed = [item for item in outcomes if item["action"] != "preserved"]
    # Preserved rows enter the receipt refs only when nothing changed, so the
    # receipt always has a verifiable postcondition set.
    refs = list(
        dict.fromkeys(f"rcm:{item['id']}" for item in (changed or outcomes))
    )
    # Rows the worker could not repair and rows the commit could not place are
    # the same thing to the auditor: drafted, not in the matrix, needs a look.
    quarantined = _quarantined_rows(request) + list(ambiguous or [])
    output: dict[str, object] = {"status": "updated", "rows": outcomes}
    # Carried from the proposal so the run can report them. Judgements the gate
    # declined to enforce: a control asserting a system mechanism, two rows
    # stating one risk, a criterion quote that matched no supplied sentence.
    flags = request.proposal.get("flags")
    if isinstance(flags, (list, tuple)) and flags:
        output["flags"] = [_plain_json(item) for item in flags]
    if quarantined:
        output["quarantined"] = quarantined
    if retired_skipped:
        output["retired_skipped"] = [_plain_json(item) for item in retired_skipped]
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output=output,
    )


def execute_rcm(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit accepted RCM row revisions under the APM parent-hash guard.

    Row matching, auditor-edit preservation, updates, and creations happen in
    one atomic transaction; a changed APM parent is a conflict rather than a
    silent overwrite, and an ambiguous narrative match fails the commit.
    """
    target, rows = _validated_rcm(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> list[dict]:
        state["revision_before"] = fresh.revision
        # Both parents the matrix declares, from the same projection the commit
        # is guarded on. This replaces ``apm_sha1`` as the row stamp: that hash
        # folded in ``workflow_basis_sha1``, so it moved when a table was
        # imported and said the matrix was drafted against a memorandum that
        # had not changed a word.
        stamps = parent_hashes(fresh, [RCM_PARENT_REF, CYCLE_ARTIFACT_REF])
        parent_sha1 = stamps[RCM_PARENT_REF]
        outcomes: list[dict] = []
        deferred: list[dict] = []
        skipped: list[dict] = []
        for spec in rows:
            # Resolved inside the transaction, against the documents as they
            # stand at commit.
            spec = {**spec, "criteria_refs": _resolved_criteria_refs(fresh, spec)}
            existing, ambiguous = match_rcm_revision(fresh, spec, spec["semantic_id"])
            if ambiguous:
                # The rows are independent, so one the matcher cannot place is
                # not a reason to drop the rest of the matrix. It travels to the
                # receipt with its candidates for the auditor to place by hand,
                # which is the same treatment a row that will not validate gets.
                deferred.append(_ambiguous_quarantine(fresh, spec))
                continue
            if (
                existing
                and existing.get("created_by") != "agent"
                and not target.allow_auditor_overwrite
            ):
                outcomes.append(
                    {
                        "id": existing["id"],
                        "semantic_id": existing.get("semantic_id")
                        or spec["semantic_id"],
                        "action": "preserved",
                    }
                )
                continue
            if existing:
                item = fresh.update_rcm(
                    existing["id"],
                    {
                        **{key: spec.get(key) for key in RCM_ROW_FIELDS},
                        # A list field is present when it has entries; a string
                        # field when it has non-blank text. Either way an empty
                        # value leaves the existing citation alone.
                        **{
                            key: spec[key]
                            for key in RCM_OPTIONAL_ROW_FIELDS
                            if (
                                bool(spec.get(key))
                                if isinstance(spec.get(key), list)
                                else str(spec.get(key) or "").strip()
                            )
                        },
                        "workflow_parent_sha1": parent_sha1,
                        "workflow_parents": stamps,
                    },
                    agent=True,
                )
                outcomes.append(
                    {
                        "id": item["id"],
                        "semantic_id": item.get("semantic_id") or spec["semantic_id"],
                        "action": "updated",
                    }
                )
                continue
            retired = match_retired_rcm(fresh, spec)
            if retired is not None:
                # Someone took this risk out of the matrix. A redraft from the
                # memorandum does not get to put it back; only a person asking
                # for it does (an auditor's own add, or a requested revision).
                skipped.append(
                    {
                        "semantic_id": spec["semantic_id"],
                        "risk": str(spec.get("risk") or ""),
                        "retired_semantic_id": str(retired.get("semantic_id") or ""),
                    }
                )
                continue
            item = fresh.add_rcm(
                {
                    **spec,
                    "agent_run_id": target.run_id,
                    "workflow_parent_sha1": parent_sha1,
                    "workflow_parents": stamps,
                }
            )
            outcomes.append(
                {
                    "id": item["id"],
                    "semantic_id": item["semantic_id"],
                    "action": "created",
                }
            )
        if not outcomes:
            # No row could be placed, so there is no partial matrix worth
            # keeping. Raising inside the transaction leaves the workspace
            # untouched rather than saving an empty commit.
            if not deferred:
                raise WorkspaceError(
                    "Every proposed RCM row restates a risk that was retired "
                    "from the matrix; nothing was added."
                )
            raise WorkspaceError(
                f"Ambiguous RCM revision for '{deferred[0]['risk']}'."
            )
        return outcomes, deferred, skipped

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    outcomes, deferred, skipped = committed.value
    return _rcm_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        outcomes=outcomes,
        ambiguous=deferred,
        retired_skipped=skipped,
    )


def reconcile_rcm(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted RCM commit without changing workspace state.

    The commit itself does not change the APM parent, so parent equality cannot
    prove the commit never ran. Instead each accepted row is re-matched against
    the current workspace: when every non-preserved row already carries the
    accepted values under the current APM hash and the revision advanced, the
    commit is proven applied. Any unapplied row returns ``not_applied`` — the
    commit is idempotent per row, so re-execution is always safe.
    """
    target, rows = _validated_rcm(request, raw_target)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [RCM_PARENT_REF])[RCM_PARENT_REF]
    expected_parent = request.expected_parents[RCM_PARENT_REF]
    if current_parent != expected_parent:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    RCM_PARENT_REF,
                    expected_parent,
                    current_parent,
                    current.revision,
                )
            ),
        )
    # Both stamps are accepted. Rows committed before the matrix moved onto
    # parent-ref hashes carry ``apm_sha1``, and an interrupted commit from that
    # build must still be recognised as applied rather than replayed.
    accepted_parents = {current_parent, audit_hashes.apm_sha1(current)}
    outcomes: list[dict] = []
    for spec in rows:
        existing, ambiguous = match_rcm_revision(current, spec, spec["semantic_id"])
        if ambiguous:
            # Execution quarantines this row rather than committing it, so it is
            # never part of the applied set and can neither prove nor disprove
            # an interrupted commit. A matrix where every row is ambiguous still
            # reconciles as not applied, on the empty ``changed`` set below.
            continue
        if existing is None and match_retired_rcm(current, spec) is not None:
            # Skipped at commit for the same reason, so it proves nothing either.
            continue
        if (
            existing
            and existing.get("created_by") != "agent"
            and not target.allow_auditor_overwrite
        ):
            outcomes.append(
                {
                    "id": existing["id"],
                    "semantic_id": existing.get("semantic_id") or spec["semantic_id"],
                    "action": "preserved",
                }
            )
            continue
        applied = (
            existing is not None
            and all(
                _plain_json(existing.get(key)) == _plain_json(spec.get(key))
                for key in RCM_ROW_FIELDS
            )
            and existing.get("workflow_parent_sha1") in accepted_parents
        )
        if not applied:
            return ExecutorReconciliation("not_applied")
        action = (
            "created"
            if existing.get("created_by") == "agent"
            and existing.get("agent_run_id") == target.run_id
            else "updated"
        )
        outcomes.append(
            {
                "id": existing["id"],
                "semantic_id": existing.get("semantic_id") or spec["semantic_id"],
                "action": action,
            }
        )
    changed = [item for item in outcomes if item["action"] != "preserved"]
    if not changed or current.revision <= request.expected_revision:
        # Preservation-only outcomes and unadvanced revisions cannot prove an
        # interrupted commit; idempotent re-execution is the safe path.
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_rcm_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            outcomes=outcomes,
        ),
        reason="The accepted RCM revisions already hold.",
    )


RCM_EXECUTOR = ExecutorDefinition(
    executor_id=RCM_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_rcm,
    reconciler=reconcile_rcm,
)

EXECUTORS.register(RCM_EXECUTOR)


__all__ = [
    "APM_EXECUTOR",
    "APM_EXECUTOR_ID",
    "AUDITOR_EDIT_PRESERVED",
    "ApmEditPreserved",
    "ApmExecutorTarget",
    "PLANNING_CONTEXT_EXECUTOR",
    "PLANNING_CONTEXT_EXECUTOR_ID",
    "PLANNING_CONTEXT_FIELDS",
    "PLANNING_CONTEXT_REF",
    "PlanningContextExecutorTarget",
    "RCM_EXECUTOR",
    "RCM_EXECUTOR_ID",
    "RCM_PARENT_REF",
    "RCM_ROW_FIELDS",
    "RcmExecutorTarget",
    "execute_apm",
    "execute_planning_context",
    "execute_rcm",
    "match_rcm_revision",
    "match_retired_rcm",
    "rcm_semantic_id",
    "reconcile_apm",
    "reconcile_planning_context",
    "reconcile_rcm",
]


# --------------------------------------------------------------------------- #
# planning.delta — write one change assessment (step 9)
# --------------------------------------------------------------------------- #
DELTA_EXECUTOR_ID = "planning.delta"


@dataclass
class DeltaExecutorTarget:
    """Where one assessment is written, and what it was made against."""

    workspace: Workspace
    run_id: str
    document_ids: tuple[str, ...] = ()
    basis_sha1: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Delta executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("Delta executor target requires a run_id.")
        self.document_ids = tuple(
            text for value in self.document_ids if (text := str(value or "").strip())
        )
        if not self.document_ids:
            raise ValueError("Delta executor target requires the documents assessed.")
        self.basis_sha1 = str(self.basis_sha1 or "").strip()
        if not self.basis_sha1:
            raise ValueError("Delta executor target requires the assessment basis.")


def _validated_delta(
    request: ExecutorRequest, target: object
) -> tuple[DeltaExecutorTarget, dict]:
    if not isinstance(target, DeltaExecutorTarget):
        raise WorkspaceError("Delta executor requires a DeltaExecutorTarget.")
    proposal = dict(request.proposal)
    if str(proposal.get("impact") or "") not in planning_delta.IMPACTS:
        raise WorkspaceError("The accepted assessment carries no usable impact.")
    return target, proposal


def _delta_result(
    request: ExecutorRequest,
    workspace: Workspace,
    target: DeltaExecutorTarget,
    assessment: dict,
    *,
    revision_before: int,
) -> ExecutorResult:
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        # The documents it read. The assessment names the memorandum and the
        # matrix but does not change them, and nothing downstream may treat it
        # as having revised either.
        artifact_refs=[f"document:{value}" for value in target.document_ids],
        applied_parents=dict(request.expected_parents),
        # The documents are unchanged by an assessment of them, so their
        # post-state hashes are their current ones. Saying so explicitly is
        # what lets a later unit see that nothing it depends on moved.
        postcondition_hashes=parent_hashes(
            workspace, [f"document:{value}" for value in target.document_ids]
        ),
        output={
            "impact": assessment["impact"],
            "basis_sha1": target.basis_sha1,
            "apm_changes": len(assessment.get("apm_changes") or []),
            "rcm_changes": len(assessment.get("rcm_changes") or []),
        },
    )


def execute_delta(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Write one assessment beside the planning artifacts it is about.

    The one executor in the audit workflow that commits no artifact. It records
    a judgment — what these documents change, and where — under the basis it was
    made against, so asking the same question of the same material is answered
    from disk rather than by a second model turn. Revising the memorandum or the
    matrix on the strength of it is a separate, requested, reviewable run.
    """

    target, proposal = _validated_delta(request, raw_target)
    if set(request.expected_parents) != {APM_ARTIFACT_REF}:
        raise WorkspaceError(
            "Delta executor requires exactly the memorandum's parent hash."
        )
    written: dict = {}

    def commit(fresh: Workspace) -> None:
        written.update(
            planning_delta.save(
                fresh,
                target.basis_sha1,
                proposal,
                document_ids=list(target.document_ids),
                run_id=target.run_id,
            )
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _delta_result(
        request,
        committed.workspace,
        target,
        written,
        revision_before=committed.revision - 1,
    )


def reconcile_delta(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    """Classify an interrupted assessment write; the file is the whole state."""

    target, proposal = _validated_delta(request, raw_target)
    current = Workspace(target.workspace.root)
    existing = planning_delta.load(current, target.basis_sha1)
    if existing is None:
        return ExecutorReconciliation("not_applied")
    return ExecutorReconciliation(
        "already_applied",
        result=_delta_result(
            request,
            current,
            target,
            existing,
            revision_before=max(request.expected_revision, current.revision - 1),
        ),
        reason="The assessment for this basis is already on file.",
    )


DELTA_EXECUTOR = ExecutorDefinition(
    executor_id=DELTA_EXECUTOR_ID,
    # No workspace artifact is written, but the memorandum it read is still
    # guarded: an assessment landing against a memorandum that moved while the
    # turn ran is an assessment of something else, and the basis it is filed
    # under would name material that never produced it.
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_delta,
    reconciler=reconcile_delta,
)

EXECUTORS.register(DELTA_EXECUTOR)


# --------------------------------------------------------------------------- #
# planning.rcm_revision — apply a revision the auditor asked for
# --------------------------------------------------------------------------- #
RCM_REVISION_EXECUTOR_ID = "planning.rcm_revision"


@dataclass
class RcmRevisionExecutorTarget:
    """The rows a revision decided about, and the request it answers."""

    workspace: Workspace
    run_id: str
    request_key: str
    row_ids: tuple[str, ...]
    target_refs: tuple[str, ...] = ()
    instruction: str = ""
    assessment: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("RCM revision target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("RCM revision target requires a run_id.")
        self.request_key = str(self.request_key or "").strip()
        if not self.request_key:
            raise ValueError("RCM revision target requires the request it answers.")
        self.row_ids = tuple(str(value) for value in self.row_ids if str(value or "").strip())
        if not self.row_ids:
            raise ValueError("RCM revision target requires the rows in scope.")
        self.target_refs = tuple(str(value) for value in self.target_refs)


def _validated_rcm_revision(
    request: ExecutorRequest, target: object
) -> tuple[RcmRevisionExecutorTarget, list[dict], list[dict]]:
    if not isinstance(target, RcmRevisionExecutorTarget):
        raise WorkspaceError("RCM revision executor requires an RcmRevisionExecutorTarget.")
    expected = {RCM_PARENT_REF, *(f"rcm:{row_id}" for row_id in target.row_ids)}
    if not expected <= set(request.expected_parents) <= {*expected, CYCLE_ARTIFACT_REF}:
        raise WorkspaceError(
            "RCM revision executor requires the memorandum's hash and every "
            "revised row's own."
        )
    decisions = [_plain_json(item) for item in request.proposal.get("decisions") or []]
    decided = {str(item.get("rcm_id")) for item in decisions}
    if decided != set(target.row_ids):
        raise WorkspaceError("The accepted revision does not decide about every row in scope.")
    for item in decisions:
        if item.get("decision") not in planning_revision.RCM_DECISIONS:
            raise WorkspaceError(f"Unknown revision decision '{item.get('decision')}'.")
    additions = [
        _plain_json(item.get("row") or {})
        for item in request.proposal.get("additions") or []
        if isinstance(item, Mapping)
    ]
    return target, decisions, additions


def _revision_scope_rows(workspace: Workspace, target: RcmRevisionExecutorTarget) -> list[dict]:
    """The rows the revision covers now, by the rule readiness reads them by."""

    return audit_hashes.rows(workspace, {"target_refs": list(target.target_refs)})


def _exact_attributes(
    workspace: Workspace, values: dict, flags: list[dict], label: str
) -> dict:
    """Make a revised control's attributes exact, or leave the old ones."""

    if "control_attributes" not in values:
        return values
    try:
        values["control_attributes"] = cycle_vouching.validate_control_attributes(
            values["control_attributes"], workspace=workspace
        )
    except cycle_vouching.CycleSchemaError as error:
        values.pop("control_attributes")
        flags.append({"row": label, "message": f"control attributes not applied: {error}"})
    return values


def _rcm_revision_result(
    request: ExecutorRequest,
    workspace: Workspace,
    target: RcmRevisionExecutorTarget,
    record: dict,
    *,
    revision_before: int,
) -> ExecutorResult:
    outcomes = list(record.get("outcomes") or [])
    touched = [item for item in outcomes if item.get("action") != "kept"]
    refs = list(
        dict.fromkeys(
            f"rcm:{item['id']}" for item in (touched or outcomes) if item.get("id")
        )
    )
    counts: dict[str, int] = {}
    for item in outcomes:
        counts[str(item.get("action"))] = counts.get(str(item.get("action")), 0) + 1
    output: dict[str, object] = {
        "status": "updated",
        "summary": record.get("summary") or "",
        "rows": outcomes,
        "counts": counts,
        "rows_before": len(target.row_ids),
        "request_key": target.request_key,
    }
    flags = [
        *[_plain_json(item) for item in request.proposal.get("flags") or []],
        *list(record.get("flags") or []),
    ]
    if flags:
        output["flags"] = flags
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output=output,
    )


def execute_rcm_revision(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Apply one accepted revision to the matrix in one guarded transaction.

    Revisions first, so a survivor reads as revised before anything is folded
    into it; then merges, which relink what the merged row carried; then
    retirements; then additions, which may lift a retirement because a person
    asked for this revision. The request's record is written last, inside the
    same lock, stamped with the scope as the commit left it — which is what
    readiness reads to say the revision holds.
    """

    target, decisions, additions = _validated_rcm_revision(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        stamps = parent_hashes(fresh, [RCM_PARENT_REF, CYCLE_ARTIFACT_REF])
        provenance = {
            "workflow_parent_sha1": stamps[RCM_PARENT_REF],
            "workflow_parents": stamps,
        }
        outcomes: list[dict] = []
        flags: list[dict] = []
        for item in decisions:
            if item["decision"] != "revise":
                continue
            changes = _exact_attributes(
                fresh, dict(item.get("changes") or {}), flags, item["rcm_id"]
            )
            fresh.update_rcm(item["rcm_id"], {**changes, **provenance}, agent=True)
            outcomes.append(
                {
                    "id": item["rcm_id"],
                    "action": "revised",
                    "fields": sorted(key for key in changes if key != "control_attributes"),
                    "reason": item.get("reason") or "",
                }
            )
        for item in decisions:
            reason = str(item.get("reason") or "")
            if item["decision"] == "merge":
                into = str(item.get("into") or "")
                fresh.merge_rcm(item["rcm_id"], into, reason=reason, run_id=target.run_id)
                outcomes.append(
                    {"id": item["rcm_id"], "action": "merged", "into": into, "reason": reason}
                )
            elif item["decision"] == "retire":
                fresh.remove_rcm(item["rcm_id"], reason=reason, run_id=target.run_id)
                outcomes.append({"id": item["rcm_id"], "action": "retired", "reason": reason})
            elif item["decision"] == "keep":
                outcomes.append({"id": item["rcm_id"], "action": "kept"})
        for spec in additions:
            spec = _exact_attributes(fresh, dict(spec), flags, str(spec.get("risk") or ""))
            spec["semantic_id"] = rcm_semantic_id(spec)
            retired = match_retired_rcm(fresh, spec)
            if retired is not None:
                # The auditor asked for this revision, so restoring a risk they
                # or someone before them retired is theirs to decide.
                fresh.lift_rcm_retirements({str(retired.get("semantic_id") or "")})
            item = fresh.add_rcm({**spec, "agent_run_id": target.run_id, **provenance})
            outcomes.append(
                {"id": item["id"], "action": "created", "restored": retired is not None}
            )
        record = planning_revision.save(
            fresh,
            "rcm",
            target.request_key,
            run_id=target.run_id,
            unit_id=request.unit_id,
            applied_sha1=planning_revision.rcm_scope_sha1(
                _revision_scope_rows(fresh, target)
            ),
            summary=str(request.proposal.get("summary") or ""),
            outcomes=outcomes,
            instruction=target.instruction,
            assessment=target.assessment,
        )
        return {**record, "flags": flags}

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _rcm_revision_result(
        request,
        committed.workspace,
        target,
        committed.value,
        revision_before=state["revision_before"],
    )


def reconcile_rcm_revision(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    """Classify an interrupted revision: its record is written last, in the lock."""

    target, _decisions, _additions = _validated_rcm_revision(request, raw_target)
    current = Workspace(target.workspace.root)
    record = planning_revision.load(current, "rcm", target.request_key)
    if (
        record is not None
        and record.get("run_id") == target.run_id
        and record.get("unit_id") == request.unit_id
    ):
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_rcm_revision_result(
                request,
                current,
                target,
                record,
                revision_before=max(request.expected_revision, current.revision - 1),
            ),
            reason="The revision is already on file.",
        )
    return _parent_conflict(current, request) or ExecutorReconciliation("not_applied")


def _parent_conflict(
    current: Workspace, request: ExecutorRequest
) -> ExecutorReconciliation | None:
    """A conflict naming the first guarded parent that moved, if one did."""

    now = parent_hashes(current, list(request.expected_parents))
    for ref, expected in request.expected_parents.items():
        if now[ref] != expected:
            return ExecutorReconciliation(
                "conflict",
                reason=str(ParentConflict(ref, expected, now[ref], current.revision)),
            )
    return None


# --------------------------------------------------------------------------- #
# planning.apm_revision — rewrite the sections an auditor asked about
# --------------------------------------------------------------------------- #
APM_REVISION_EXECUTOR_ID = "planning.apm_revision"


@dataclass
class ApmRevisionExecutorTarget:
    """The memorandum a revision rewrites sections of, and the request."""

    workspace: Workspace
    run_id: str
    request_key: str
    instruction: str = ""
    assessment: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("APM revision target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("APM revision target requires a run_id.")
        self.request_key = str(self.request_key or "").strip()
        if not self.request_key:
            raise ValueError("APM revision target requires the request it answers.")


def _validated_apm_revision(
    request: ExecutorRequest, target: object
) -> tuple[ApmRevisionExecutorTarget, list[dict]]:
    if not isinstance(target, ApmRevisionExecutorTarget):
        raise WorkspaceError("APM revision executor requires an ApmRevisionExecutorTarget.")
    if set(request.expected_parents) != {APM_ARTIFACT_REF}:
        raise WorkspaceError("APM revision executor requires exactly the memorandum's hash.")
    replaced = [
        _plain_json(item)
        for item in request.proposal.get("sections") or []
        if isinstance(item, Mapping)
    ]
    for item in replaced:
        if markdown_sections.breaks_structure(str(item.get("markdown") or "")):
            raise WorkspaceError(
                f"The revision of '{item.get('heading')}' would start a section of its own."
            )
    return target, replaced


def _apm_revision_result(
    request: ExecutorRequest,
    workspace: Workspace,
    record: dict,
    *,
    revision_before: int,
) -> ExecutorResult:
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[APM_ARTIFACT_REF],
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, [APM_ARTIFACT_REF]),
        output={
            "status": "updated",
            "summary": record.get("summary") or "",
            "sections": list(record.get("outcomes") or []),
        },
    )


def execute_apm_revision(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Splice the rewritten sections in, and change nothing else.

    Every other line of the memorandum stays byte for byte, and the heading set
    must come out exactly as it went in. Ownership is left alone — a revision
    made at the auditor's request does not make their memorandum the agent's —
    and so is the parent stamp, so a memorandum behind its planning context
    stays honestly behind it.
    """

    target, replaced = _validated_apm_revision(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        current = str(fresh.planning.get("apm_markdown") or "")
        try:
            revised = markdown_sections.replace_bodies(
                current, {item["heading"]: item["markdown"] for item in replaced}
            )
        except ValueError as error:
            raise WorkspaceError(f"The revision does not fit the memorandum: {error}.") from error
        if markdown_sections.section_headings(revised) != markdown_sections.section_headings(current):
            raise WorkspaceError("The revision changed the memorandum's sections.")
        if revised != current:
            fresh.update_planning(
                {"apm_markdown": revised, "agent_run_id": target.run_id}, agent=True
            )
        return planning_revision.save(
            fresh,
            "apm",
            target.request_key,
            run_id=target.run_id,
            unit_id=request.unit_id,
            applied_sha1=planning_revision.apm_scope_sha1(fresh),
            summary=str(request.proposal.get("summary") or ""),
            outcomes=[
                {"section": item["heading"], "action": "replaced", "reason": item.get("reason") or ""}
                for item in replaced
            ],
            instruction=target.instruction,
            assessment=target.assessment,
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _apm_revision_result(
        request,
        committed.workspace,
        committed.value,
        revision_before=state["revision_before"],
    )


def reconcile_apm_revision(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    target, _replaced = _validated_apm_revision(request, raw_target)
    current = Workspace(target.workspace.root)
    record = planning_revision.load(current, "apm", target.request_key)
    if (
        record is not None
        and record.get("run_id") == target.run_id
        and record.get("unit_id") == request.unit_id
    ):
        target.workspace = current
        return ExecutorReconciliation(
            "already_applied",
            result=_apm_revision_result(
                request,
                current,
                record,
                revision_before=max(request.expected_revision, current.revision - 1),
            ),
            reason="The revision is already on file.",
        )
    return _parent_conflict(current, request) or ExecutorReconciliation("not_applied")


APM_REVISION_EXECUTOR = ExecutorDefinition(
    executor_id=APM_REVISION_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_apm_revision,
    reconciler=reconcile_apm_revision,
)

EXECUTORS.register(APM_REVISION_EXECUTOR)


RCM_REVISION_EXECUTOR = ExecutorDefinition(
    executor_id=RCM_REVISION_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_rcm_revision,
    reconciler=reconcile_rcm_revision,
)

EXECUTORS.register(RCM_REVISION_EXECUTOR)
