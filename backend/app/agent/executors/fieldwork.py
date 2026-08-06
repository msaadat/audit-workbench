"""Deterministic executors for audit fieldwork capabilities.

None of these performs a model call. ``roll_up_results`` recomputes each RCM
row's derived result and its observations from the current execution artifacts and
commits only material changes; it binds through the scheduler's deterministic
execution path for ``results.rolled_up``. As with the reporting siblings, the
exception observations are created directly during roll-up and feed finding
creation without a separate checkpoint.

``execute_data_test`` is the registered commit for ``fieldwork.executed``
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
from ...workspaces import Workspace, WorkspaceError, sync_workspace
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


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    """Deep-copy frozen proposal values back to plain JSON containers."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def data_test_ref(data_test_id: str) -> str:
    """The stable artifact reference for one durable Data Test."""

    return f"datatest:{data_test_id}"


def document_test_ref(document_test_id: str) -> str:
    """The stable artifact reference for one durable Document Test."""

    return f"doctest:{document_test_id}"


def result_ref(rcm_id: str) -> str:
    """The stable artifact reference for one rolled-up RCM row result."""

    return f"{RESULT_REF_PREFIX}:{rcm_id}"


def roll_up_results(
    workspace: Workspace, *, rcm_ids: set[str] | None = None
) -> list[str]:
    """Recompute RCM results and observations; return stable per-row refs.

    Deterministic and self-committing: ``rcm_execution.rollup`` recomputes the
    selected RCM rows' derived results and observations from the current execution
    artifacts and persists only material changes. Observation identities are keyed
    on ``execution_ref``, so a repeated roll-up reuses the same observation rows
    rather than creating duplicates — the result and observation identities stay
    stable across runs. No model call is involved; exception observations feed
    finding creation directly.
    """

    result = rcm_execution.rollup(workspace, rcm_ids=rcm_ids)
    return [result_ref(row["rcm_id"]) for row in result["rows"]]


# --------------------------------------------------------------------------- #
# fieldwork.executed deterministic execution (P7F.2)
# --------------------------------------------------------------------------- #
EVIDENCE_UNAVAILABLE = "Evidence is unavailable."


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
    happened but its evidence cannot support a reliable outcome.
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
    if test.get("kind") == "cycle_vouch":
        raise WorkspaceError(
            f"Document Test '{test_id}' uses the Phase 0 cycle_vouch contract; "
            "its deterministic evaluator is introduced in Phase 3."
        )
    for item in test.get("items") or []:
        if not doc_tests.item_execution_pending(test, item):
            continue
        doc_tests.run_item(workspace, test_id, item["id"], run_id=run_id)
    test = doc_tests.load_test(workspace, test_id)
    rollup = doc_tests.result_rollup(test)
    # A manual-check outcome remains visible in the result rollup but is not an
    # auditor sign-off gate. Only missing evidence blocks this workflow unit.
    test["status"] = "completed"
    outcome = ExecutionOutcome(reference, "succeeded")
    doc_tests.save_test(workspace, test)
    return outcome


# --------------------------------------------------------------------------- #
# fieldwork.document_qa executor (P7F.3)
# --------------------------------------------------------------------------- #
DOCUMENT_QA_EXECUTOR_ID = "fieldwork.document_qa"
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
) -> tuple[DocumentQaExecutorTarget, str, str, str, str, list[dict]]:
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
    conclusion = request.proposal.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, str):
        raise WorkspaceError("The accepted document Q&A proposal has no conclusion.")
    control_conclusion = str(
        request.proposal.get("control_conclusion")
        or {"accepted": "effective", "exception": "ineffective"}.get(
            request.proposal.get("outcome"), "no_conclusion"
        )
    )
    if control_conclusion not in doc_tests.CONTROL_CONCLUSIONS:
        raise WorkspaceError(
            "The accepted document Q&A proposal has no valid control conclusion."
        )
    outcome = str(request.proposal.get("outcome") or "")
    if outcome not in {"accepted", "exception", "needs_manual_check"}:
        raise WorkspaceError("The accepted document Q&A proposal has no valid outcome.")
    citations = [
        {"page": int(citation["page"]), "excerpt": str(citation.get("excerpt") or "")}
        for citation in _plain_json(request.proposal.get("citations") or [])
        if isinstance(citation, Mapping) and citation.get("page") is not None
    ]
    return target, answer, str(conclusion or answer), control_conclusion, outcome, citations


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
    target, answer, conclusion, control_conclusion, outcome, citations = _validated_document_qa(
        request, raw_target
    )
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
        return doc_tests.commit_llm_assessment(
            fresh,
            target.test_id,
            target.item_id,
            target.document_id,
            {
                "answer": answer,
                "conclusion": conclusion,
                "control_conclusion": control_conclusion,
                "outcome": outcome,
                "citations": anchors,
            },
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
    target, answer, conclusion, control_conclusion, outcome, _citations = _validated_document_qa(
        request, raw_target
    )
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
    durable = ((item or {}).get("llm_answers") or (item or {}).get("qa_answers") or {}).get(target.document_id) or {}
    if str(durable.get("answer") or "") != answer:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The Document Test changed before the interrupted Q&A commit was "
                "reconciled."
            ),
        )
    if str(durable.get("conclusion") or durable.get("answer") or "") != conclusion:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The Document Test conclusion changed before the interrupted Q&A "
                "commit was reconciled."
            ),
        )
    if str(durable.get("control_conclusion") or "no_conclusion") != control_conclusion:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The Document Test control conclusion changed before the interrupted "
                "Q&A commit was reconciled."
            ),
        )
    if str(durable.get("outcome") or "") != outcome:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The Document Test outcome changed before the interrupted Q&A "
                "commit was reconciled."
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
    "DOCUMENT_QA_EXECUTOR",
    "DOCUMENT_QA_EXECUTOR_ID",
    "DocumentQaExecutorTarget",
    "EVIDENCE_UNAVAILABLE",
    "ExecutionOutcome",
    "data_test_result_ref",
    "document_qa_answer_ref",
    "execute_document_qa",
    "reconcile_document_qa",
    "run_data_test",
    "run_document_test",
    "document_test_ref",
    "RESULT_REF_PREFIX",
    "data_test_ref",
    "result_ref",
    "roll_up_results",
]
