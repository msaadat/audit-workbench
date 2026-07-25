"""Deterministic executors for audit fieldwork capabilities.

None of these performs a model call. ``roll_up_results`` recomputes each RCM
row's derived result and its observations from the current execution artifacts and
commits only material changes; it binds through the scheduler's deterministic
execution path for ``results.rolled_up``. As with the reporting siblings, the
observation/disposition auditor judgment is a declared checkpoint that runs
between roll-up and finding creation, not part of this executor.

``execute_data_test`` is the registered commit for ``fieldwork.definitions_ready``
Data Test units: it owns the planned-test parent guard, auditor-edit
preservation, and the authoritative data-dependent validation that the worker's
bundle-only gate cannot perform.

``run_data_test`` and ``run_document_test`` are the deterministic halves of
``fieldwork.executed``: local Polars computation and local document comparison.
Neither calls a model — the one model-backed unit kind of that capability
(document Q&A) has its own registered worker and the ``fieldwork.document_qa``
executor below.
"""

from __future__ import annotations

import hashlib
import inspect
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from ... import data_tests, doc_tests, rcm_execution
from ...evidence import document_anchor
from ...workspace_transactions import (
    ParentConflict,
    canonical_sha1,
    mutate,
    parent_hashes,
)
from ...workspaces import Workspace, WorkspaceError, slugify, sync_workspace
from ..capabilities import _shared as audit_hashes
from .model import (
    EXECUTORS,
    ExecutorConcurrency,
    ExecutorDefinition,
    ExecutorReconciliation,
    ExecutorRequest,
    ExecutorResult,
)

RESULT_REF_PREFIX = "rcm"


def result_ref(rcm_id: str) -> str:
    """The stable artifact reference for one rolled-up RCM row result."""

    return f"{RESULT_REF_PREFIX}:{rcm_id}"


def roll_up_results(workspace: Workspace) -> list[str]:
    """Recompute RCM results and observations; return stable per-row refs.

    Deterministic and self-committing: ``rcm_execution.rollup`` recomputes every
    RCM row's derived result and its observations from the current execution
    artifacts and persists only material changes. Observation identities are keyed
    on ``execution_ref``, so a repeated roll-up reuses the same observation rows
    rather than creating duplicates — the result and observation identities stay
    stable across runs. No model call is involved; the auditor's observation
    disposition runs as a declared checkpoint before finding creation, not here.
    """

    result = rcm_execution.rollup(workspace)
    return [result_ref(row["rcm_id"]) for row in result["rows"]]


# --------------------------------------------------------------------------- #
# fieldwork.executed deterministic execution (P7F.2)
# --------------------------------------------------------------------------- #
EVIDENCE_UNAVAILABLE = "Evidence is unavailable."
DOCUMENT_REVIEW_REQUIRED = "Auditor review or disposition is required."


@dataclass(frozen=True)
class ExecutionOutcome:
    """The durable result of one deterministic execution unit."""

    artifact_ref: str
    status: str
    error: str | None = None
    # False when nothing was executed, so the caller emits no change signal.
    executed: bool = True


def data_test_result_ref(data_test_id: str, result_id: str) -> str:
    """The stable reference for one immutable Data Test result."""

    return f"datatest:{data_test_id}:{result_id}"


def run_data_test(workspace: Workspace, data_test_id: str) -> ExecutionOutcome:
    """Run one durable Data Test locally and classify its result.

    Deterministic and self-committing: ``data_tests.run`` computes the candidate
    with Polars and commits it under the Data Test definition's own parent-hash
    guard, so a definition changed since the run started surfaces as a conflict
    rather than a result attributed to the wrong basis. A structurally computed
    result whose semantic contract fails is ``blocked``, not ``failed``: the run
    happened and the auditor decides what to do about it.
    """

    result = data_tests.run(workspace, data_test_id)
    if result.get("semantic_valid"):
        return ExecutionOutcome(
            data_test_result_ref(data_test_id, str(result["id"])), "succeeded"
        )
    return ExecutionOutcome(
        data_test_result_ref(data_test_id, str(result["id"])),
        "blocked",
        "; ".join(result.get("semantic_issues") or []),
    )


def _register_blocked_unit(workspace: Workspace, test_id: str, unit_id: str) -> None:
    """Point this test's open evidence requests at the unit they block.

    Recording the blocked unit is what lets a later document import unblock the
    exact workflow unit, so it belongs with the durable write rather than with
    the scheduler.
    """

    def outstanding(candidate: Workspace) -> list[dict]:
        return [
            request
            for request in candidate.evidence_requests
            if request.get("document_test_id") == test_id
            and request.get("status") == "open"
            and request.get("blocked_unit_id") != unit_id
        ]

    # Nothing to record must not advance the workspace revision: a blocked unit
    # is re-evaluated on every continuation, and a no-op write would make each
    # one look like a change to anything holding a revision.
    if not outstanding(workspace):
        return

    def commit(fresh: Workspace) -> None:
        for request in outstanding(fresh):
            request["blocked_unit_id"] = unit_id
            request["updated"] = fresh._updated_now()

    committed = mutate(workspace, commit)
    sync_workspace(workspace, committed.workspace)


def run_document_test(
    workspace: Workspace,
    test_id: str,
    *,
    unit_id: str,
    run_id: str,
) -> ExecutionOutcome:
    """Run one durable Document Test's outstanding items locally.

    Every unit kind reaching this function is deterministic: vouching items are
    compared against extracted page text locally, and the remaining kinds are
    marked for manual review. Document Q&A answers are *not* produced here — a
    Q&A test expands into ``document_qa_execution`` units that go through the
    registered worker and the injected gateway, so a Q&A test that is not waiting
    on evidence is a contract violation rather than an unbudgeted provider call.
    """

    reference = document_test_ref(test_id)
    test = doc_tests.load_test(workspace, test_id)
    if doc_tests.evidence_blocked(test):
        _register_blocked_unit(workspace, test_id, unit_id)
        return ExecutionOutcome(
            reference,
            "blocked",
            str(test.get("scope_limitations") or "") or EVIDENCE_UNAVAILABLE,
            executed=False,
        )
    if test.get("kind") == "qa":
        raise WorkspaceError(
            f"Document Test '{test_id}' answers questions with the model and must "
            "execute as document Q&A units, not as a local document test."
        )
    for item in test.get("items") or []:
        if item.get("state") in {"confirmed", "exception"}:
            continue
        doc_tests.run_item(workspace, test_id, item["id"], run_id=run_id)
    test = doc_tests.load_test(workspace, test_id)
    rollup = doc_tests.result_rollup(test)
    if rollup["pending"] or rollup["manual_review"]:
        test["status"] = "review_required"
        outcome = ExecutionOutcome(
            reference, "awaiting_confirmation", DOCUMENT_REVIEW_REQUIRED
        )
    else:
        test["status"] = "completed"
        outcome = ExecutionOutcome(reference, "succeeded")
    doc_tests.save_test(workspace, test)
    return outcome


# --------------------------------------------------------------------------- #
# fieldwork.data_test executor (P7E.2)
# --------------------------------------------------------------------------- #
DATA_TEST_EXECUTOR_ID = "fieldwork.data_test"
# The definition fields an accepted proposal may write onto a matched Data Test.
DATA_TEST_FIELDS = ("title", "objective", "engine", "table_refs", "spec")


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def data_test_ref(data_test_id: str) -> str:
    """The stable artifact reference for one durable Data Test."""

    return f"datatest:{data_test_id}"


@dataclass
class DataTestExecutorTarget:
    """Mutable target for one planned test's Data Test definition commit."""

    workspace: Workspace
    run_id: str
    rcm_id: str
    planned_test_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Data Test executor target requires a Workspace.")
        for field_name in ("run_id", "rcm_id", "planned_test_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Data Test executor target requires a {field_name}.")
            setattr(self, field_name, value)
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def data_test_semantic_id(
    rcm_id: str,
    planned_test_id: str,
    title: str,
) -> str:
    return f"datatest:{rcm_id}:{planned_test_id}:{slugify(title or planned_test_id)}"


def data_test_stable_id(semantic: str) -> str:
    return "DAT-" + hashlib.sha1(semantic.encode()).hexdigest()[:10].upper()


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _validated_data_test(
    request: ExecutorRequest,
    target: object,
) -> tuple[DataTestExecutorTarget, dict, str]:
    if not isinstance(target, DataTestExecutorTarget):
        raise WorkspaceError("Data Test executor requires a DataTestExecutorTarget.")
    parent_ref = f"planned_test:{target.planned_test_id}"
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Data Test executor requires exactly its planned-test parent hash."
        )
    raw = request.proposal.get("data_test")
    if not isinstance(raw, Mapping):
        raise WorkspaceError("The accepted Data Test proposal is empty.")
    definition = _plain_json(raw)
    title = str(definition.get("title") or "").strip()
    if not title:
        raise WorkspaceError("The accepted Data Test proposal has no title.")
    semantic = data_test_semantic_id(target.rcm_id, target.planned_test_id, title)
    return target, definition, semantic


def _match_data_test(
    workspace: Workspace,
    target: DataTestExecutorTarget,
    semantic: str,
) -> dict | None:
    stable_id = data_test_stable_id(semantic)
    return next(
        (
            item
            for item in workspace.data_tests
            if str(item.get("planned_test_id") or "") == target.planned_test_id
            and (item.get("semantic_id") == semantic or item.get("id") == stable_id)
        ),
        None,
    )


def _data_test_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    item: Mapping[str, object],
    action: str,
) -> ExecutorResult:
    refs = [data_test_ref(str(item["id"]))]
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
            "status": "updated",
            "id": str(item["id"]),
            "semantic_id": str(item.get("semantic_id") or ""),
            "action": action,
        },
    )


def execute_data_test(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one accepted Data Test under its planned-test parent guard.

    This is where the authoritative, data-dependent validation happens:
    ``data_tests.create``/``update`` resolve table references, canonicalize
    analytics parameters and validation rules against the real frames, and check
    the Polars sandbox. The worker's bundle-only gate cannot see frames, so an
    invalid definition surfaces here as a durable unit failure.
    """
    target, definition, semantic = _validated_data_test(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> tuple[dict, str]:
        state["revision_before"] = fresh.revision
        _row, planned = fresh.planned_test(target.planned_test_id)
        payload = {
            **{key: definition[key] for key in DATA_TEST_FIELDS if key in definition},
            "rcm_id": target.rcm_id,
            "planned_test_id": target.planned_test_id,
            "semantic_id": semantic,
            "agent_run_id": target.run_id,
            "workflow_parent_sha1": audit_hashes.planned_test_sha1(planned),
        }
        existing = _match_data_test(fresh, target, semantic)
        if (
            existing
            and existing.get("created_by") != "agent"
            and not target.allow_auditor_overwrite
        ):
            return existing, "preserved"
        if existing:
            return (
                data_tests.update(
                    fresh,
                    existing["id"],
                    {
                        key: payload[key]
                        for key in (
                            *DATA_TEST_FIELDS,
                            "rcm_id",
                            "planned_test_id",
                            "workflow_parent_sha1",
                        )
                        if key in payload
                    },
                    agent=True,
                ),
                "updated",
            )
        return (
            data_tests.create(
                fresh, {**payload, "id": data_test_stable_id(semantic)}
            ),
            "created",
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    item, action = committed.value
    return _data_test_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        item=item,
        action=action,
    )


def reconcile_data_test(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted Data Test commit without mutating state.

    Committing links the new Data Test into its planned test's execution refs, so
    the guarded projection changes on success: an unchanged parent proves the
    commit never landed, and a changed parent is only reconcilable when the
    matched Data Test already carries this run's accepted definition.
    """
    target, definition, semantic = _validated_data_test(request, raw_target)
    parent_ref = f"planned_test:{target.planned_test_id}"
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [parent_ref])[parent_ref]
    expected_parent = request.expected_parents[parent_ref]
    if current_parent == expected_parent:
        return ExecutorReconciliation("not_applied")
    existing = _match_data_test(current, target, semantic)
    if existing is None or current.revision <= request.expected_revision:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    parent_ref, expected_parent, current_parent, current.revision
                )
            ),
        )
    if (
        existing.get("created_by") != "agent"
        and not target.allow_auditor_overwrite
    ):
        return ExecutorReconciliation(
            "already_applied",
            result=_data_test_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
                item=existing,
                action="preserved",
            ),
            reason="An auditor-owned Data Test was preserved.",
        )
    applied = all(
        existing.get(key) == definition.get(key)
        for key in ("title", "objective", "engine")
    )
    if not applied:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The Data Test changed before the interrupted commit was reconciled."
            ),
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_data_test_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            item=existing,
            action=(
                "created"
                if existing.get("agent_run_id") == target.run_id
                else "updated"
            ),
        ),
        reason="The accepted Data Test definition already holds.",
    )


DATA_TEST_EXECUTOR = ExecutorDefinition(
    executor_id=DATA_TEST_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_data_test)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_data_test)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_data_test,
    reconciler=reconcile_data_test,
)

EXECUTORS.register(DATA_TEST_EXECUTOR)


# --------------------------------------------------------------------------- #
# fieldwork.document_test executor (P7E.3)
# --------------------------------------------------------------------------- #
DOCUMENT_TEST_EXECUTOR_ID = "fieldwork.document_test"


def document_test_ref(document_test_id: str) -> str:
    """The stable artifact reference for one durable Document Test."""

    return f"doctest:{document_test_id}"


@dataclass
class DocumentTestExecutorTarget:
    """Mutable target for one planned test's Document Test definition commit."""

    workspace: Workspace
    run_id: str
    rcm_id: str
    planned_test_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Document Test executor target requires a Workspace.")
        for field_name in ("run_id", "rcm_id", "planned_test_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(
                    f"Document Test executor target requires a {field_name}."
                )
            setattr(self, field_name, value)
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def document_test_semantic_id(
    rcm_id: str,
    planned_test_id: str,
    title: str,
) -> str:
    return f"doctest:{rcm_id}:{planned_test_id}:{slugify(title or planned_test_id)}"


def document_test_stable_id(semantic: str) -> str:
    return "DT-" + hashlib.sha1(semantic.encode()).hexdigest()[:8].upper()


def _validated_document_test(
    request: ExecutorRequest,
    target: object,
) -> tuple[DocumentTestExecutorTarget, dict, str]:
    if not isinstance(target, DocumentTestExecutorTarget):
        raise WorkspaceError(
            "Document Test executor requires a DocumentTestExecutorTarget."
        )
    parent_ref = f"planned_test:{target.planned_test_id}"
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Document Test executor requires exactly its planned-test parent hash."
        )
    raw = request.proposal.get("document_test")
    if not isinstance(raw, Mapping):
        raise WorkspaceError("The accepted Document Test proposal is empty.")
    definition = _plain_json(raw)
    title = str(definition.get("title") or "").strip()
    if not title:
        raise WorkspaceError("The accepted Document Test proposal has no title.")
    semantic = document_test_semantic_id(target.rcm_id, target.planned_test_id, title)
    return target, definition, semantic


def _match_document_test(
    workspace: Workspace,
    target: DocumentTestExecutorTarget,
    semantic: str,
) -> dict | None:
    stable_id = document_test_stable_id(semantic)
    return next(
        (
            summary
            for summary in doc_tests.list_tests(workspace)
            if str(summary.get("planned_test_id") or "") == target.planned_test_id
            and (summary.get("semantic_id") == semantic or summary.get("id") == stable_id)
        ),
        None,
    )


def _record_missing_evidence(
    fresh: Workspace,
    test: dict,
    definition: Mapping[str, object],
    *,
    target: DocumentTestExecutorTarget,
    unit_id: str,
) -> None:
    """Block the test and register one evidence request per unattached item.

    Registering the blocking unit is what lets a later document import unblock
    the exact workflow unit, so this stays with the durable write.
    """
    missing = dict(definition.get("missing_evidence") or {})
    no_documents = any(not item.get("document_ids") for item in test.get("items") or [])
    if not missing and not no_documents:
        return
    test["status"] = "blocked"
    test["scope_limitations"] = str(
        missing.get("rationale") or "Required evidence is not yet available."
    )
    evidence_hash = canonical_sha1(
        [
            {key: item.get(key) for key in ("id", "sha1", "category", "title")}
            for item in fresh.documents
        ]
    )
    for item in test.get("items") or []:
        if item.get("document_ids"):
            continue
        request = {
            "id": f"ER-{uuid.uuid4().hex[:10].upper()}",
            "rcm_id": target.rcm_id,
            "planned_test_id": target.planned_test_id,
            "document_test_id": test["id"],
            "item_id": item["id"],
            "transaction_identifier": str(
                missing.get("identifiers") or item.get("label") or ""
            ),
            "missing_document_types": list(
                missing.get("document_types") or ["supporting_evidence"]
            ),
            "status": "open",
            "reason": test["scope_limitations"],
            "next_action": (
                "Import or attach matching evidence, then continue the audit."
            ),
            "blocked_unit_id": unit_id,
            "evidence_availability_sha1": evidence_hash,
            "created": fresh._updated_now(),
            "updated": fresh._updated_now(),
        }
        fresh.evidence_requests.append(request)
        item.setdefault("evidence_request_ids", []).append(request["id"])
    doc_tests.save_test(fresh, test)
    fresh.save()


def _document_test_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    test: Mapping[str, object],
    action: str,
) -> ExecutorResult:
    refs = [document_test_ref(str(test["id"]))]
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
            "status": str(test.get("status") or "ready"),
            "id": str(test["id"]),
            "semantic_id": str(test.get("semantic_id") or ""),
            "action": action,
        },
    )


def execute_document_test(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorResult:
    """Commit one accepted Document Test under its planned-test parent guard.

    The write is a linked one: the test body lives in its own sidecar while the
    workspace records the link and any evidence requests, so both move to the
    same revision. An item with no attached document blocks the test and
    registers an evidence request against this unit.
    """
    target, definition, semantic = _validated_document_test(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> tuple[dict, str]:
        state["revision_before"] = fresh.revision
        _row, planned = fresh.planned_test(target.planned_test_id)
        existing = _match_document_test(fresh, target, semantic)
        if (
            existing
            and existing.get("created_by") != "agent"
            and not target.allow_auditor_overwrite
        ):
            return doc_tests.load_test(fresh, existing["id"]), "preserved"
        payload = {
            **definition,
            "rcm_id": target.rcm_id,
            "planned_test_id": target.planned_test_id,
            "rcm_refs": [target.rcm_id],
            "semantic_id": semantic,
            "agent_run_id": target.run_id,
            "workflow_parent_sha1": audit_hashes.planned_test_sha1(planned),
            "id": (
                existing["id"] if existing else document_test_stable_id(semantic)
            ),
        }
        if existing:
            payload["created"] = existing.get("created")
        test = doc_tests.create_test(fresh, payload)
        _record_missing_evidence(
            fresh,
            test,
            definition,
            target=target,
            unit_id=request.unit_id,
        )
        return test, ("updated" if existing else "created")

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    test, action = committed.value
    return _document_test_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        test=test,
        action=action,
    )


def reconcile_document_test(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted Document Test commit without mutating state."""
    target, definition, semantic = _validated_document_test(request, raw_target)
    parent_ref = f"planned_test:{target.planned_test_id}"
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [parent_ref])[parent_ref]
    expected_parent = request.expected_parents[parent_ref]
    if current_parent == expected_parent:
        return ExecutorReconciliation("not_applied")
    existing = _match_document_test(current, target, semantic)
    if existing is None or current.revision <= request.expected_revision:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    parent_ref, expected_parent, current_parent, current.revision
                )
            ),
        )
    if (
        existing.get("created_by") != "agent"
        and not target.allow_auditor_overwrite
    ):
        return ExecutorReconciliation(
            "already_applied",
            result=_document_test_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
                test=existing,
                action="preserved",
            ),
            reason="An auditor-owned Document Test was preserved.",
        )
    if str(existing.get("title") or "") != str(definition.get("title") or "") or str(
        existing.get("kind") or ""
    ) != str(definition.get("kind") or ""):
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The Document Test changed before the interrupted commit was "
                "reconciled."
            ),
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_document_test_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            test=existing,
            action=(
                "created"
                if existing.get("agent_run_id") == target.run_id
                else "updated"
            ),
        ),
        reason="The accepted Document Test definition already holds.",
    )


DOCUMENT_TEST_EXECUTOR = ExecutorDefinition(
    executor_id=DOCUMENT_TEST_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_document_test)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_document_test)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_test,
    reconciler=reconcile_document_test,
)

EXECUTORS.register(DOCUMENT_TEST_EXECUTOR)


# --------------------------------------------------------------------------- #
# fieldwork.document_qa executor (P7F.3)
# --------------------------------------------------------------------------- #
DOCUMENT_QA_EXECUTOR_ID = "fieldwork.document_qa"
DOCUMENT_QA_DISPOSITION_REQUIRED = "The cited answer requires auditor disposition."


def document_qa_answer_ref(test_id: str, item_id: str, document_id: str) -> str:
    """The stable reference for one item/document Q&A answer."""

    return f"doctest:{test_id}:item:{item_id}:document:{document_id}"


@dataclass
class DocumentQaExecutorTarget:
    """Mutable target for one item/document Q&A answer commit."""

    workspace: Workspace
    run_id: str
    test_id: str
    item_id: str
    document_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Document Q&A executor target requires a Workspace.")
        for field_name in ("run_id", "test_id", "item_id", "document_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(
                    f"Document Q&A executor target requires a {field_name}."
                )
            setattr(self, field_name, value)


def _validated_document_qa(
    request: ExecutorRequest,
    target: object,
) -> tuple[DocumentQaExecutorTarget, str, list[dict]]:
    if not isinstance(target, DocumentQaExecutorTarget):
        raise WorkspaceError(
            "Document Q&A executor requires a DocumentQaExecutorTarget."
        )
    parent_ref = document_test_ref(target.test_id)
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Document Q&A executor requires exactly its Document Test parent hash."
        )
    answer = request.proposal.get("answer")
    if not isinstance(answer, str):
        raise WorkspaceError("The accepted document Q&A proposal has no answer.")
    citations = [
        {"page": int(citation["page"]), "excerpt": str(citation.get("excerpt") or "")}
        for citation in _plain_json(request.proposal.get("citations") or [])
        if isinstance(citation, Mapping) and citation.get("page") is not None
    ]
    return target, answer, citations


def _document_qa_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    target: DocumentQaExecutorTarget,
    item: Mapping[str, object],
) -> ExecutorResult:
    refs = [document_test_ref(target.test_id)]
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
            "status": "answered",
            "id": target.test_id,
            "item_id": target.item_id,
            "document_id": target.document_id,
            "answer_ref": document_qa_answer_ref(
                target.test_id, target.item_id, target.document_id
            ),
            "state": str(item.get("state") or ""),
            "action": "answered",
        },
    )


def execute_document_qa(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one cited Q&A answer under its Document Test parent guard.

    The evidence anchors are built here, from the document as it exists at commit
    time, rather than taken from the proposal: the worker only decides which
    supplied page and excerpt support the answer, so a proposal cannot introduce a
    citation to a document or hash it never saw. The commit is a merge in stable
    document order, so answering one attached document never discards another's.
    """
    target, answer, citations = _validated_document_qa(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        document = next(
            (
                item
                for item in fresh.documents
                if str(item.get("id")) == target.document_id
            ),
            None,
        )
        if document is None:
            raise WorkspaceError(f"Document '{target.document_id}' not found.")
        anchors = [
            document_anchor(
                document,
                int(citation["page"]),
                str(citation["excerpt"]),
                generated_by=target.run_id,
            )
            for citation in citations
        ]
        return doc_tests.commit_qa_answer(
            fresh,
            target.test_id,
            target.item_id,
            target.document_id,
            {"answer": answer, "citations": anchors},
        )

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _document_qa_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        target=target,
        item=committed.value,
    )


def reconcile_document_qa(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted Q&A commit without mutating state.

    Committing an answer rewrites the guarded Document Test summary (its ``sha1``
    and ``status``), so an unchanged parent proves the commit never landed. A
    changed parent is reconcilable only when this exact answer is already the
    durable one for that item and document; anything else is a real conflict.
    """
    target, answer, _citations = _validated_document_qa(request, raw_target)
    parent_ref = document_test_ref(target.test_id)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [parent_ref])[parent_ref]
    expected_parent = request.expected_parents[parent_ref]
    if current_parent == expected_parent:
        return ExecutorReconciliation("not_applied")
    if current.revision <= request.expected_revision:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    parent_ref, expected_parent, current_parent, current.revision
                )
            ),
        )
    test = doc_tests.load_test(current, target.test_id)
    item = next(
        (
            value
            for value in test.get("items") or []
            if str(value.get("id")) == target.item_id
        ),
        None,
    )
    durable = ((item or {}).get("qa_answers") or {}).get(target.document_id) or {}
    if str(durable.get("answer") or "") != answer:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The Document Test changed before the interrupted Q&A commit was "
                "reconciled."
            ),
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_document_qa_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            target=target,
            item=item or {},
        ),
        reason="The accepted document Q&A answer already holds.",
    )


DOCUMENT_QA_EXECUTOR = ExecutorDefinition(
    executor_id=DOCUMENT_QA_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_document_qa)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_document_qa)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_qa,
    reconciler=reconcile_document_qa,
)

EXECUTORS.register(DOCUMENT_QA_EXECUTOR)


__all__ = [
    "DATA_TEST_EXECUTOR",
    "DATA_TEST_EXECUTOR_ID",
    "DOCUMENT_QA_DISPOSITION_REQUIRED",
    "DOCUMENT_QA_EXECUTOR",
    "DOCUMENT_QA_EXECUTOR_ID",
    "DOCUMENT_REVIEW_REQUIRED",
    "DocumentQaExecutorTarget",
    "EVIDENCE_UNAVAILABLE",
    "ExecutionOutcome",
    "data_test_result_ref",
    "document_qa_answer_ref",
    "execute_document_qa",
    "reconcile_document_qa",
    "run_data_test",
    "run_document_test",
    "DOCUMENT_TEST_EXECUTOR",
    "DOCUMENT_TEST_EXECUTOR_ID",
    "DocumentTestExecutorTarget",
    "document_test_ref",
    "document_test_semantic_id",
    "document_test_stable_id",
    "execute_document_test",
    "reconcile_document_test",
    "DATA_TEST_FIELDS",
    "DataTestExecutorTarget",
    "RESULT_REF_PREFIX",
    "data_test_ref",
    "data_test_semantic_id",
    "data_test_stable_id",
    "execute_data_test",
    "reconcile_data_test",
    "result_ref",
    "roll_up_results",
]
