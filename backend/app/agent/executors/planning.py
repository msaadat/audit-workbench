"""Deterministic executors for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ...workspaces import Workspace, WorkspaceError, slugify
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
    implementation_hash=_sha256_text(inspect.getsource(execute_apm)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_apm)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_apm,
    reconciler=reconcile_apm,
)

EXECUTORS.register(APM_EXECUTOR)


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
) -> tuple[PlanningContextExecutorTarget, dict[str, str]]:
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
    return target, context


def _context_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    context: dict[str, str],
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
        output={"status": "updated", "fields": sorted(context)},
    )


def execute_planning_context(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Merge one accepted planning context under the parent-hash guard.

    The synthesized fields are merged into ``planning.context`` (auditor-entered
    fields outside the proposal are preserved by the merge), guarded by the
    planning-context parent hash so a concurrent context change is a conflict
    rather than a silent overwrite.
    """
    target, context = _validated_context(request, raw_target)

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
    )


def reconcile_planning_context(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted planning-context commit without mutating state."""
    target, context = _validated_context(request, raw_target)
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
            ),
            reason="The accepted planning context already holds.",
        )
    return ExecutorReconciliation(
        "conflict",
        reason="Planning context changed before the interrupted commit was reconciled.",
    )


PLANNING_CONTEXT_EXECUTOR = ExecutorDefinition(
    executor_id=PLANNING_CONTEXT_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_planning_context)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_planning_context)),
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
    "assertion",
    "control",
    "control_type",
    "test_procedure",
)


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
    narrative = f"{spec.get('process', '')} {spec.get('risk', '')}"
    ranked = sorted(
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
    if not ranked or ranked[0][0] < 0.72:
        return None, False
    ambiguous = len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08
    return (None, True) if ambiguous else (ranked[0][1], False)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _validated_rcm(
    request: ExecutorRequest,
    target: object,
) -> tuple[RcmExecutorTarget, list[dict]]:
    if not isinstance(target, RcmExecutorTarget):
        raise WorkspaceError("RCM executor requires an RcmExecutorTarget.")
    if set(request.expected_parents) != {RCM_PARENT_REF}:
        raise WorkspaceError("RCM executor requires exactly the APM parent hash.")
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
        spec["semantic_id"] = rcm_semantic_id(spec)
        rows.append(spec)
    return target, rows


def _rcm_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    outcomes: list[dict],
) -> ExecutorResult:
    changed = [item for item in outcomes if item["action"] != "preserved"]
    # Preserved rows enter the receipt refs only when nothing changed, so the
    # receipt always has a verifiable postcondition set.
    refs = list(
        dict.fromkeys(f"rcm:{item['id']}" for item in (changed or outcomes))
    )
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output={"status": "updated", "rows": outcomes},
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
        parent_sha1 = audit_hashes.apm_sha1(fresh)
        outcomes: list[dict] = []
        for spec in rows:
            existing, ambiguous = match_rcm_revision(fresh, spec, spec["semantic_id"])
            if ambiguous:
                raise WorkspaceError(
                    f"Ambiguous RCM revision for '{spec.get('risk')}'."
                )
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
                        "workflow_parent_sha1": parent_sha1,
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
            item = fresh.add_rcm(
                {
                    **spec,
                    "agent_run_id": target.run_id,
                    "workflow_parent_sha1": parent_sha1,
                }
            )
            outcomes.append(
                {
                    "id": item["id"],
                    "semantic_id": item["semantic_id"],
                    "action": "created",
                }
            )
        return outcomes

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _rcm_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        outcomes=committed.value,
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
    parent_sha1 = audit_hashes.apm_sha1(current)
    outcomes: list[dict] = []
    for spec in rows:
        existing, ambiguous = match_rcm_revision(current, spec, spec["semantic_id"])
        if ambiguous:
            # Execution will surface the ambiguity as a durable unit failure.
            return ExecutorReconciliation("not_applied")
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
                str(existing.get(key) or "") == str(spec.get(key) or "")
                for key in RCM_ROW_FIELDS
            )
            and existing.get("workflow_parent_sha1") == parent_sha1
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
    implementation_hash=_sha256_text(inspect.getsource(execute_rcm)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_rcm)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_rcm,
    reconciler=reconcile_rcm,
)

EXECUTORS.register(RCM_EXECUTOR)


# --------------------------------------------------------------------------- #
# planning.planned_tests executor (P7D)
# --------------------------------------------------------------------------- #
PLANNED_TEST_EXECUTOR_ID = "planning.planned_tests"
# The revision fields an accepted proposal may write onto a matched planned test.
PLANNED_TEST_FIELDS = (
    "title",
    "objective",
    "criteria",
    "steps",
    "method",
    "expected_evidence",
    "sampling",
    "thresholds",
    "methodology_refs",
)


@dataclass
class PlannedTestExecutorTarget:
    """Mutable target for one RCM row's deterministic planned-test commit."""

    workspace: Workspace
    run_id: str
    rcm_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Planned-test executor target requires a Workspace.")
        self.run_id = str(self.run_id or "").strip()
        if not self.run_id:
            raise ValueError("Planned-test executor target requires a run_id.")
        self.rcm_id = str(self.rcm_id or "").strip()
        if not self.rcm_id:
            raise ValueError("Planned-test executor target requires an rcm_id.")
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def planned_test_semantic_id(rcm_id: str, spec: Mapping[str, object]) -> str:
    return str(
        spec.get("semantic_id")
        or f"planned-test:{rcm_id}:"
        f"{slugify(str(spec.get('stable_slug') or spec.get('objective') or ''))}"
    )


def planned_test_stable_id(semantic: str) -> str:
    return "PT-" + hashlib.sha1(semantic.encode()).hexdigest()[:10].upper()


def match_planned_test_revision(
    row: Mapping[str, object],
    spec: Mapping[str, object],
    semantic: str,
) -> dict | None:
    """Match one proposed revision to an existing planned test on ``row``.

    The durable ``planned_test_id`` wins; otherwise the stable semantic id
    identifies the same test across regenerations. Unmatched specs are created,
    which is what the runner-era commit did.
    """
    explicit = canonical_id(
        spec.get("planned_test_id") or spec.get("id"), "planned_test"
    )
    planned_tests = row.get("planned_tests") or []
    if explicit:
        matched = next(
            (item for item in planned_tests if item.get("id") == explicit), None
        )
        if matched is not None:
            return matched
    return next(
        (item for item in planned_tests if item.get("semantic_id") == semantic), None
    )


def _validated_planned_tests(
    request: ExecutorRequest,
    target: object,
) -> tuple[PlannedTestExecutorTarget, list[dict]]:
    if not isinstance(target, PlannedTestExecutorTarget):
        raise WorkspaceError(
            "Planned-test executor requires a PlannedTestExecutorTarget."
        )
    parent_ref = f"rcm:{target.rcm_id}"
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Planned-test executor requires exactly its RCM row parent hash."
        )
    raw = request.proposal.get("planned_tests")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise WorkspaceError("The accepted planned-test proposal has no tests.")
    specs: list[dict] = []
    for value in raw:
        if not isinstance(value, Mapping):
            raise WorkspaceError("Each accepted planned test must be an object.")
        spec = _plain_json(value)
        if not str(spec.get("objective") or "").strip():
            raise WorkspaceError("An accepted planned test is missing its objective.")
        spec["rcm_id"] = target.rcm_id
        spec["rcm_refs"] = [target.rcm_id]
        spec["title"] = str(spec.get("title") or spec["objective"])
        spec["semantic_id"] = planned_test_semantic_id(target.rcm_id, spec)
        specs.append(spec)
    return target, specs


def _planned_test_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    outcomes: list[dict],
) -> ExecutorResult:
    changed = [item for item in outcomes if item["action"] != "preserved"]
    # Preserved tests enter the receipt refs only when nothing changed, so the
    # receipt always has a verifiable postcondition set.
    refs = list(
        dict.fromkeys(f"planned_test:{item['id']}" for item in (changed or outcomes))
    )
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=refs,
        applied_parents=dict(request.expected_parents),
        postcondition_hashes=parent_hashes(workspace, refs),
        output={"status": "updated", "planned_tests": outcomes},
    )


def _current_row(workspace: Workspace, rcm_id: str) -> dict:
    row = next(
        (item for item in workspace.rcm if str(item.get("id")) == rcm_id), None
    )
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    return row


def execute_planned_tests(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorResult:
    """Commit one RCM row's accepted planned tests under its parent-hash guard.

    Matching, auditor-edit preservation, updates, and creations run inside one
    locked transaction guarded on the RCM row, so a concurrent row change is a
    conflict rather than a silent overwrite. Only the declared planned-test
    fields are written: a proposal cannot smuggle workspace state (status,
    conclusions, execution or evidence links) into the committed record, which
    also keeps every write in the loop well formed.
    """
    target, specs = _validated_planned_tests(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> list[dict]:
        state["revision_before"] = fresh.revision
        # ``row`` is the live record, so a test added earlier in this loop is
        # visible to the next spec's match and cannot be duplicated.
        row = _current_row(fresh, target.rcm_id)
        parent_sha1 = audit_hashes.rcm_row_sha1(row)
        outcomes: list[dict] = []
        for spec in specs:
            semantic = str(spec["semantic_id"])
            existing = match_planned_test_revision(row, spec, semantic)
            if (
                existing
                and existing.get("created_by") != "agent"
                and not target.allow_auditor_overwrite
            ):
                outcomes.append(
                    {
                        "id": existing["id"],
                        "semantic_id": existing.get("semantic_id") or semantic,
                        "action": "preserved",
                    }
                )
                continue
            payload = {
                **{key: spec[key] for key in PLANNED_TEST_FIELDS if key in spec},
                "semantic_id": semantic,
                "workflow_parent_sha1": parent_sha1,
            }
            if existing:
                item = fresh.update_planned_test(
                    target.rcm_id,
                    existing["id"],
                    {
                        key: payload[key]
                        for key in (*PLANNED_TEST_FIELDS, "workflow_parent_sha1")
                        if key in payload
                    },
                    agent=True,
                )
                outcomes.append(
                    {
                        "id": item["id"],
                        "semantic_id": item.get("semantic_id") or semantic,
                        "action": "updated",
                    }
                )
                continue
            item = fresh.add_planned_test(
                target.rcm_id,
                {
                    **payload,
                    "id": planned_test_stable_id(semantic),
                    "agent_run_id": target.run_id,
                },
            )
            outcomes.append(
                {
                    "id": item["id"],
                    "semantic_id": item.get("semantic_id") or semantic,
                    "action": "created",
                }
            )
        return outcomes

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _planned_test_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        outcomes=committed.value,
    )


def reconcile_planned_tests(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted planned-test commit without mutating state.

    Committing planned tests changes the guarded ``rcm:<id>`` projection, so an
    unchanged parent proves the commit never landed. When the parent has moved,
    each accepted test is re-matched against the current row: the commit is
    proven applied only when every non-preserved test already carries the
    accepted values under the row's current material hash.
    """
    target, specs = _validated_planned_tests(request, raw_target)
    parent_ref = f"rcm:{target.rcm_id}"
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [parent_ref])[parent_ref]
    expected_parent = request.expected_parents[parent_ref]
    if current_parent == expected_parent:
        # The guarded row is unchanged: nothing was committed, so an ordinary
        # execution under the same guard is safe.
        return ExecutorReconciliation("not_applied")
    row = next(
        (item for item in current.rcm if str(item.get("id")) == target.rcm_id), None
    )
    if row is None:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    parent_ref, expected_parent, current_parent, current.revision
                )
            ),
        )
    parent_sha1 = audit_hashes.rcm_row_sha1(row)
    outcomes: list[dict] = []
    for spec in specs:
        semantic = str(spec["semantic_id"])
        existing = match_planned_test_revision(row, spec, semantic)
        if (
            existing
            and existing.get("created_by") != "agent"
            and not target.allow_auditor_overwrite
        ):
            outcomes.append(
                {
                    "id": existing["id"],
                    "semantic_id": existing.get("semantic_id") or semantic,
                    "action": "preserved",
                }
            )
            continue
        applied = (
            existing is not None
            and all(
                str(existing.get(key) or "") == str(spec.get(key) or "")
                for key in PLANNED_TEST_FIELDS
                if key in spec
            )
            and existing.get("workflow_parent_sha1") == parent_sha1
        )
        if not applied:
            return ExecutorReconciliation(
                "conflict",
                reason=(
                    "The RCM row changed before the interrupted planned-test "
                    "commit was reconciled."
                ),
            )
        action = (
            "created"
            if existing.get("agent_run_id") == target.run_id
            else "updated"
        )
        outcomes.append(
            {
                "id": existing["id"],
                "semantic_id": existing.get("semantic_id") or semantic,
                "action": action,
            }
        )
    changed = [item for item in outcomes if item["action"] != "preserved"]
    if not changed or current.revision <= request.expected_revision:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The RCM row changed before the interrupted planned-test commit "
                "was reconciled."
            ),
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_planned_test_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            outcomes=outcomes,
        ),
        reason="The accepted planned tests already hold.",
    )


PLANNED_TEST_EXECUTOR = ExecutorDefinition(
    executor_id=PLANNED_TEST_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_planned_tests)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_planned_tests)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_planned_tests,
    reconciler=reconcile_planned_tests,
)

EXECUTORS.register(PLANNED_TEST_EXECUTOR)


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
    "PLANNED_TEST_EXECUTOR",
    "PLANNED_TEST_EXECUTOR_ID",
    "PLANNED_TEST_FIELDS",
    "PlannedTestExecutorTarget",
    "PlanningContextExecutorTarget",
    "RCM_EXECUTOR",
    "RCM_EXECUTOR_ID",
    "RCM_PARENT_REF",
    "RCM_ROW_FIELDS",
    "RcmExecutorTarget",
    "execute_apm",
    "execute_planned_tests",
    "execute_planning_context",
    "execute_rcm",
    "match_planned_test_revision",
    "match_rcm_revision",
    "planned_test_semantic_id",
    "planned_test_stable_id",
    "rcm_semantic_id",
    "reconcile_apm",
    "reconcile_planned_tests",
    "reconcile_planning_context",
    "reconcile_rcm",
]
