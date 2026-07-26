"""Deterministic executors for the test capability group.

Three executors, matching the three workers. ``tests.draft`` commits one RCM
row's planned tests as durable Document/Data Test records in ``draft`` status;
``tests.data_spec`` and ``tests.document_spec`` write the executable
specification onto one of those records.

Both passes write the same record, so the draft executor guards on the RCM row
it expands and the spec executors guard on the test they fill in. Only declared
fields are written: a proposal cannot smuggle workspace state (status,
conclusions, results, evidence links) into a committed test.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from ... import data_tests, doc_tests
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

DRAFT_EXECUTOR_ID = "tests.draft"
DATA_SPEC_EXECUTOR_ID = "tests.data_spec"
DOCUMENT_SPEC_EXECUTOR_ID = "tests.document_spec"

# The plan fields an accepted draft proposal may write onto a matched test.
DRAFT_FIELDS = (
    "title",
    "objective",
    "criteria",
    "steps",
    "expected_evidence",
    "methodology_refs",
)


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def artifact_test_ref(kind: str, test_id: str) -> str:
    """The stable artifact reference for one durable test."""

    return f"{kind}:{test_id}"


def semantic_test_id(kind: str, rcm_id: str, title: str) -> str:
    return f"{kind}:{rcm_id}:{slugify(title)}"


def stable_test_id(kind: str, semantic: str) -> str:
    digest = hashlib.sha1(semantic.encode()).hexdigest()
    return (
        f"DAT-{digest[:10].upper()}" if kind == "datatest" else f"DT-{digest[:8].upper()}"
    )


# --------------------------------------------------------------------------- #
# tests.draft executor
# --------------------------------------------------------------------------- #
@dataclass
class DraftExecutorTarget:
    """Mutable target for one RCM row's deterministic test-draft commit."""

    workspace: Workspace
    run_id: str
    rcm_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Test-draft executor target requires a Workspace.")
        for field_name in ("run_id", "rcm_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Test-draft executor target requires a {field_name}.")
            setattr(self, field_name, value)
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def _linked_tests(workspace: Workspace, rcm_id: str) -> list[dict]:
    """Every test linked to one RCM row, as ``{kind, id, record}`` summaries."""
    linked = [
        {"kind": "datatest", "id": item["id"], "record": item}
        for item in workspace.data_tests
        if item.get("rcm_id") == rcm_id
    ]
    linked.extend(
        {"kind": "doctest", "id": summary["id"], "record": summary}
        for summary in doc_tests.list_tests(workspace)
        if summary.get("rcm_id") == rcm_id
    )
    return linked


def match_test_revision(
    workspace: Workspace, rcm_id: str, kind: str, semantic: str
) -> dict | None:
    """Match one proposed test to an existing one on the same RCM row.

    The stable semantic id identifies the same test across regenerations;
    unmatched proposals are created.
    """
    stable_id = stable_test_id(kind, semantic)
    return next(
        (
            item
            for item in _linked_tests(workspace, rcm_id)
            if item["kind"] == kind
            and (
                item["record"].get("semantic_id") == semantic
                or item["id"] == stable_id
            )
        ),
        None,
    )


def _validated_drafts(
    request: ExecutorRequest,
    target: object,
) -> tuple[DraftExecutorTarget, list[dict]]:
    if not isinstance(target, DraftExecutorTarget):
        raise WorkspaceError("Test-draft executor requires a DraftExecutorTarget.")
    parent_ref = f"rcm:{target.rcm_id}"
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Test-draft executor requires exactly its RCM row parent hash."
        )
    raw = request.proposal.get("tests")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise WorkspaceError("The accepted test-draft proposal has no tests.")
    specs: list[dict] = []
    for value in raw:
        if not isinstance(value, Mapping):
            raise WorkspaceError("Each accepted test must be an object.")
        spec = _plain_json(value)
        if not str(spec.get("objective") or "").strip():
            raise WorkspaceError("An accepted test is missing its objective.")
        source = str(spec.get("source") or "")
        if source not in {"data", "document"}:
            raise WorkspaceError("An accepted test has no usable source.")
        spec["kind"] = "datatest" if source == "data" else "doctest"
        spec["title"] = str(spec.get("title") or spec["objective"])
        spec["rcm_id"] = target.rcm_id
        spec["semantic_id"] = semantic_test_id(
            spec["kind"], target.rcm_id, spec["title"]
        )
        specs.append(spec)
    return target, specs


def _draft_result(
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
        dict.fromkeys(
            artifact_test_ref(item["kind"], item["id"])
            for item in (changed or outcomes)
        )
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
        output={"status": "updated", "tests": outcomes},
    )


def _current_row(workspace: Workspace, rcm_id: str) -> dict:
    row = next((item for item in workspace.rcm if str(item.get("id")) == rcm_id), None)
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    return row


def execute_test_draft(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one RCM row's drafted tests under its parent-hash guard.

    Matching, auditor-edit preservation, updates, and creations run inside one
    locked transaction guarded on the RCM row, so a concurrent row change is a
    conflict rather than a silent overwrite.
    """
    target, specs = _validated_drafts(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> list[dict]:
        state["revision_before"] = fresh.revision
        row = _current_row(fresh, target.rcm_id)
        parent_sha1 = audit_hashes.rcm_row_sha1(row)
        outcomes: list[dict] = []
        for spec in specs:
            kind = str(spec["kind"])
            semantic = str(spec["semantic_id"])
            existing = match_test_revision(fresh, target.rcm_id, kind, semantic)
            if (
                existing
                and existing["record"].get("created_by") != "agent"
                and not target.allow_auditor_overwrite
            ):
                outcomes.append(
                    {"kind": kind, "id": existing["id"], "action": "preserved"}
                )
                continue
            payload = {
                **{key: spec[key] for key in DRAFT_FIELDS if key in spec},
                "rcm_id": target.rcm_id,
                "agent_run_id": target.run_id,
                "workflow_parent_sha1": parent_sha1,
            }
            store = data_tests if kind == "datatest" else doc_tests
            if existing:
                store.update_plan(fresh, existing["id"], payload)
                outcomes.append(
                    {"kind": kind, "id": existing["id"], "action": "updated"}
                )
                continue
            item = store.create_draft(
                fresh,
                {
                    **payload,
                    "id": stable_test_id(kind, semantic),
                    "semantic_id": semantic,
                },
            )
            outcomes.append({"kind": kind, "id": item["id"], "action": "created"})
        return outcomes

    committed = mutate(
        target.workspace, commit, expected_parents=request.expected_parents
    )
    target.workspace = committed.workspace
    return _draft_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        outcomes=committed.value,
    )


def reconcile_test_draft(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    """Classify an interrupted test-draft commit without mutating state.

    Committing tests changes the guarded ``rcm:<id>`` projection through
    ``test_refs``, so an unchanged parent proves the commit never landed. When
    the parent has moved, each accepted test is re-matched: the commit is proven
    applied only when every non-preserved test already carries the accepted plan
    under the row's current material hash.
    """
    target, specs = _validated_drafts(request, raw_target)
    parent_ref = f"rcm:{target.rcm_id}"
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [parent_ref])[parent_ref]
    expected_parent = request.expected_parents[parent_ref]
    if current_parent == expected_parent:
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
        kind = str(spec["kind"])
        existing = match_test_revision(
            current, target.rcm_id, kind, str(spec["semantic_id"])
        )
        if (
            existing
            and existing["record"].get("created_by") != "agent"
            and not target.allow_auditor_overwrite
        ):
            outcomes.append({"kind": kind, "id": existing["id"], "action": "preserved"})
            continue
        record = existing["record"] if existing else None
        applied = (
            record is not None
            and str(record.get("title") or "") == str(spec.get("title") or "")
            and record.get("workflow_parent_sha1") == parent_sha1
        )
        if not applied:
            return ExecutorReconciliation(
                "conflict",
                reason=(
                    "The RCM row changed before the interrupted test-draft commit "
                    "was reconciled."
                ),
            )
        outcomes.append(
            {
                "kind": kind,
                "id": existing["id"],
                "action": (
                    "created"
                    if record.get("agent_run_id") == target.run_id
                    else "updated"
                ),
            }
        )
    changed = [item for item in outcomes if item["action"] != "preserved"]
    if not changed or current.revision <= request.expected_revision:
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The RCM row changed before the interrupted test-draft commit was "
                "reconciled."
            ),
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_draft_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            outcomes=outcomes,
        ),
        reason="The accepted tests already hold.",
    )


DRAFT_EXECUTOR = ExecutorDefinition(
    executor_id=DRAFT_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_test_draft)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_test_draft)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_test_draft,
    reconciler=reconcile_test_draft,
)

EXECUTORS.register(DRAFT_EXECUTOR)


# --------------------------------------------------------------------------- #
# tests.data_spec / tests.document_spec executors
# --------------------------------------------------------------------------- #
@dataclass
class SpecExecutorTarget:
    """Mutable target for writing one test's executable specification."""

    workspace: Workspace
    run_id: str
    rcm_id: str
    test_id: str
    kind: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Test-spec executor target requires a Workspace.")
        for field_name in ("run_id", "rcm_id", "test_id", "kind"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Test-spec executor target requires a {field_name}.")
            setattr(self, field_name, value)
        if self.kind not in {"datatest", "doctest"}:
            raise ValueError("Test-spec executor target kind must be a test kind.")
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")

    @property
    def parent_ref(self) -> str:
        return artifact_test_ref(self.kind, self.test_id)


def _validated_spec(
    request: ExecutorRequest, target: object, *, kind: str
) -> tuple[SpecExecutorTarget, dict]:
    if not isinstance(target, SpecExecutorTarget):
        raise WorkspaceError("Test-spec executor requires a SpecExecutorTarget.")
    if target.kind != kind:
        raise WorkspaceError(f"This executor commits a {kind} specification only.")
    if set(request.expected_parents) != {target.parent_ref}:
        raise WorkspaceError("Test-spec executor requires exactly its test parent hash.")
    definition = _plain_json(dict(request.proposal))
    if not definition:
        raise WorkspaceError("The accepted test specification is empty.")
    return target, definition


def _spec_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    test: Mapping[str, object],
    kind: str,
    action: str,
) -> ExecutorResult:
    refs = [artifact_test_ref(kind, str(test["id"]))]
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
            "kind": kind,
            "action": action,
        },
    )


def execute_data_spec(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one generated Polars specification onto its drafted Data Test.

    ``data_tests.apply_spec`` is the authoritative gate: it re-validates the code
    through the sandbox against the real frames, so a proposal the worker could
    only check statically still cannot write an unrunnable definition.
    """
    target, definition = _validated_spec(request, raw_target, kind="datatest")
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> tuple[dict, str]:
        state["revision_before"] = fresh.revision
        existing = data_tests._record(fresh, target.test_id)
        if (
            existing.get("created_by") != "agent"
            and not target.allow_auditor_overwrite
        ):
            return existing, "preserved"
        was_draft = existing.get("status") == "draft"
        item = data_tests.apply_spec(
            fresh,
            target.test_id,
            {
                **definition,
                "agent_run_id": target.run_id,
                "workflow_parent_sha1": audit_hashes.test_plan_sha1(existing),
            },
        )
        return item, ("created" if was_draft else "updated")

    committed = mutate(
        target.workspace, commit, expected_parents=request.expected_parents
    )
    target.workspace = committed.workspace
    test, action = committed.value
    return _spec_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        test=test,
        kind="datatest",
        action=action,
    )


def _item_identifier(label: str) -> str:
    """The transaction identifier an item's label names, if it names one.

    Evidence matching is keyed on the identifier a request is about, so the
    generated label — "Was REQ-404 approved?" — is mined for its one
    document-shaped token rather than asking the model for a separate field.
    """
    for token in re.split(r"\s+", str(label or "")):
        cleaned = token.strip(".,;:?!()[]{}\"'")
        normalized = re.sub(r"[^a-z0-9]+", "", cleaned.casefold())
        if len(normalized) >= 4 and any(char.isdigit() for char in normalized):
            return cleaned
    return str(label or "")


def _record_missing_evidence(
    fresh: Workspace,
    test: dict,
    definition: Mapping[str, object],
    *,
    target: SpecExecutorTarget,
    unit_id: str,
) -> None:
    """Block the test and register one evidence request per unattached item.

    Registering the blocking unit is what lets a later document import unblock
    the exact workflow unit, so this stays with the durable write.
    """
    missing = str(definition.get("missing_evidence") or "").strip()
    no_documents = any(not item.get("document_ids") for item in test.get("items") or [])
    if not missing and not no_documents:
        return
    test["status"] = "blocked"
    test["scope_limitations"] = missing or "Required evidence is not yet available."
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
            "document_test_id": test["id"],
            "item_id": item["id"],
            "transaction_identifier": _item_identifier(item.get("label") or ""),
            "missing_document_types": ["supporting_evidence"],
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


def execute_document_spec(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Commit one generated item set onto its drafted Document Test.

    The write is a linked one: the test body lives in its own sidecar while the
    workspace records the link and any evidence requests, so both move to the
    same revision. An item with no attached document blocks the test and
    registers an evidence request against this unit.
    """
    target, definition = _validated_spec(request, raw_target, kind="doctest")
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> tuple[dict, str]:
        state["revision_before"] = fresh.revision
        existing = doc_tests.load_test(fresh, target.test_id)
        if (
            existing.get("created_by") != "agent"
            and not target.allow_auditor_overwrite
        ):
            return existing, "preserved"
        was_draft = existing.get("status") == "draft"
        test = doc_tests.apply_spec(
            fresh,
            target.test_id,
            {
                **definition,
                "agent_run_id": target.run_id,
                "workflow_parent_sha1": audit_hashes.test_plan_sha1(existing),
            },
        )
        _record_missing_evidence(
            fresh, test, definition, target=target, unit_id=request.unit_id
        )
        return test, ("created" if was_draft else "updated")

    committed = mutate(
        target.workspace, commit, expected_parents=request.expected_parents
    )
    target.workspace = committed.workspace
    test, action = committed.value
    return _spec_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        test=test,
        kind="doctest",
        action=action,
    )


def _reconcile_spec(
    request: ExecutorRequest, raw_target: object, *, kind: str
) -> ExecutorReconciliation:
    """Classify an interrupted specification commit without mutating state."""
    target, _definition = _validated_spec(request, raw_target, kind=kind)
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [target.parent_ref])[target.parent_ref]
    expected_parent = request.expected_parents[target.parent_ref]
    if current_parent == expected_parent:
        return ExecutorReconciliation("not_applied")
    try:
        test = (
            data_tests._record(current, target.test_id)
            if kind == "datatest"
            else doc_tests.load_test(current, target.test_id)
        )
    except WorkspaceError:
        test = None
    if test is None or current.revision <= request.expected_revision:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    target.parent_ref,
                    expected_parent,
                    current_parent,
                    current.revision,
                )
            ),
        )
    if test.get("created_by") != "agent" and not target.allow_auditor_overwrite:
        return ExecutorReconciliation(
            "already_applied",
            result=_spec_result(
                request,
                current,
                revision_before=max(request.expected_revision, current.revision - 1),
                test=test,
                kind=kind,
                action="preserved",
            ),
            reason="An auditor-owned test was preserved.",
        )
    if test.get("status") == "draft":
        return ExecutorReconciliation(
            "conflict",
            reason=(
                "The test is still unspecified, so the interrupted commit did not "
                "land."
            ),
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_spec_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            test=test,
            kind=kind,
            action=(
                "created" if test.get("agent_run_id") == target.run_id else "updated"
            ),
        ),
        reason="The accepted specification already holds.",
    )


def reconcile_data_spec(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    return _reconcile_spec(request, raw_target, kind="datatest")


def reconcile_document_spec(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    return _reconcile_spec(request, raw_target, kind="doctest")


DATA_SPEC_EXECUTOR = ExecutorDefinition(
    executor_id=DATA_SPEC_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_data_spec)),
    reconciliation_hash=_sha256_text(inspect.getsource(_reconcile_spec)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_data_spec,
    reconciler=reconcile_data_spec,
)

DOCUMENT_SPEC_EXECUTOR = ExecutorDefinition(
    executor_id=DOCUMENT_SPEC_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_document_spec)),
    reconciliation_hash=_sha256_text(inspect.getsource(_reconcile_spec)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_document_spec,
    reconciler=reconcile_document_spec,
)

EXECUTORS.register(DATA_SPEC_EXECUTOR)
EXECUTORS.register(DOCUMENT_SPEC_EXECUTOR)


__all__ = [
    "DATA_SPEC_EXECUTOR",
    "DATA_SPEC_EXECUTOR_ID",
    "DOCUMENT_SPEC_EXECUTOR",
    "DOCUMENT_SPEC_EXECUTOR_ID",
    "DRAFT_EXECUTOR",
    "DRAFT_EXECUTOR_ID",
    "DRAFT_FIELDS",
    "DraftExecutorTarget",
    "SpecExecutorTarget",
    "execute_data_spec",
    "execute_document_spec",
    "execute_test_draft",
    "match_test_revision",
    "reconcile_data_spec",
    "reconcile_document_spec",
    "reconcile_test_draft",
    "artifact_test_ref",
    "semantic_test_id",
    "stable_test_id",
]
