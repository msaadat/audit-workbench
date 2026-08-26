"""Deterministic executors for audit-planning capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ... import cycle_vouching
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
        try:
            spec["control_attributes"] = cycle_vouching.validate_control_attributes(
                spec.get("control_attributes")
            )
        except cycle_vouching.CycleSchemaError as error:
            raise WorkspaceError(str(error)) from error
        transaction_packs = {
            str(attribute["registry"]["pack_id"])
            for attribute in spec["control_attributes"]
            if attribute.get("evidence_kind") == "transaction_cycle"
        }
        if len(transaction_packs) > 1:
            raise WorkspaceError("An accepted RCM row mixes transaction-cycle packs.")
        expected_cycle = next(iter(transaction_packs), "")
        if str(spec.get("business_cycle") or "").strip() != expected_cycle:
            raise WorkspaceError(
                "An accepted RCM row business_cycle does not match its control attributes."
            )
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
    quarantined = _quarantined_rows(request)
    output: dict[str, object] = {"status": "updated", "rows": outcomes}
    if quarantined:
        output["quarantined"] = quarantined
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
        parent_sha1 = audit_hashes.apm_sha1(fresh)
        outcomes: list[dict] = []
        for spec in rows:
            # Resolved inside the transaction, against the documents as they
            # stand at commit.
            spec = {**spec, "criteria_refs": _resolved_criteria_refs(fresh, spec)}
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
                _plain_json(existing.get(key)) == _plain_json(spec.get(key))
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
    "rcm_semantic_id",
    "reconcile_apm",
    "reconcile_planning_context",
    "reconcile_rcm",
]
