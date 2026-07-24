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
"""

from __future__ import annotations

import hashlib
import inspect
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from ... import data_tests, doc_tests, rcm_execution
from ...workspace_transactions import (
    ParentConflict,
    canonical_sha1,
    mutate,
    parent_hashes,
)
from ...workspaces import Workspace, WorkspaceError, slugify
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


__all__ = [
    "DATA_TEST_EXECUTOR",
    "DATA_TEST_EXECUTOR_ID",
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
