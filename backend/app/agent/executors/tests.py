"""Deterministic executors for the test capability group.

One executor, ``tests.generate``, commits one RCM row's complete, executable
Data and Document Tests atomically. Only declared fields are written: a
proposal cannot smuggle workspace state (status, conclusions, results,
evidence links) into a committed test. Replaces the retired two-pass
``tests.draft`` / ``tests.data_spec`` / ``tests.document_spec`` executors
(docs/test-capability-merge-plan.md).
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


def _generation_result(
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


# --------------------------------------------------------------------------- #
# tests.generate executor
#
# Commits a row's complete Data and Document Tests atomically, per the merge
# plan (docs/test-capability-merge-plan.md, sections 6 and 8).
# --------------------------------------------------------------------------- #
GENERATE_EXECUTOR_ID = "tests.generate"


@dataclass
class TestGenerateExecutorTarget:
    """Mutable target for one RCM row's deterministic test-generation commit."""

    __test__ = False  # not a pytest test class despite the name prefix

    workspace: Workspace
    run_id: str
    rcm_id: str
    allow_auditor_overwrite: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Test-generation executor target requires a Workspace.")
        for field_name in ("run_id", "rcm_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Test-generation executor target requires a {field_name}.")
            setattr(self, field_name, value)
        if not isinstance(self.allow_auditor_overwrite, bool):
            raise ValueError("allow_auditor_overwrite must be a boolean.")


def _validated_generation(
    request: ExecutorRequest,
    target: object,
) -> tuple[TestGenerateExecutorTarget, list[dict]]:
    if not isinstance(target, TestGenerateExecutorTarget):
        raise WorkspaceError(
            "Test-generation executor requires a TestGenerateExecutorTarget."
        )
    parent_ref = f"rcm:{target.rcm_id}"
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Test-generation executor requires exactly its RCM row parent hash."
        )
    raw = request.proposal.get("tests")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise WorkspaceError("The accepted test-generation proposal has no tests.")
    specs: list[dict] = []
    for value in raw:
        if not isinstance(value, Mapping):
            raise WorkspaceError("Each accepted test must be an object.")
        spec = _plain_json(value)
        source = str(spec.get("source") or "")
        if source not in {"data", "document"}:
            raise WorkspaceError("An accepted test has no usable source.")
        if not str(spec.get("title") or "").strip():
            raise WorkspaceError("An accepted test is missing its title.")
        if not str(spec.get("objective") or "").strip():
            raise WorkspaceError("An accepted test is missing its objective.")
        steps = spec.get("steps")
        if not isinstance(steps, (list, tuple)) or not steps:
            raise WorkspaceError("An accepted test has no steps.")
        kind = "datatest" if source == "data" else "doctest"
        spec["kind"] = kind
        spec["rcm_id"] = target.rcm_id
        spec["semantic_id"] = semantic_test_id(kind, target.rcm_id, str(spec["title"]))
        specs.append(spec)
    return target, specs


def _document_items_from_steps(steps: list[Mapping[str, object]]) -> list[dict]:
    """Normalize each generated document step into the existing item model."""
    items = []
    for step in steps:
        item = {
            "label": str(step.get("label") or ""),
            "instruction": str(step.get("instruction") or ""),
            "document_ids": [str(value) for value in step.get("document_ids") or []],
        }
        mode = str(step.get("mode") or "")
        if mode == "question":
            item["question"] = str(step.get("question") or "")
        elif mode == "vouch":
            item["checks"] = [
                {"field": check.get("field"), "expected": check.get("expected")}
                for check in step.get("checks") or []
                if isinstance(check, Mapping)
            ]
        items.append(item)
    return items


def _commit_data_test(fresh: Workspace, existing: dict | None, common: dict, semantic: str) -> dict:
    payload = {
        "title": common["title"],
        "objective": common["objective"],
        "steps": common["steps"],
        "methodology_refs": common["methodology_refs"],
        "rcm_id": common["rcm_id"],
        "workflow_parent_sha1": common["workflow_parent_sha1"],
        "engine": "polars",
        "spec": {"schema_version": 2, "steps": common["steps"]},
    }
    if existing is None:
        return data_tests.create(
            fresh,
            {
                **payload,
                "id": stable_test_id("datatest", semantic),
                "semantic_id": semantic,
                "agent_run_id": common["agent_run_id"],
            },
        )
    return data_tests.update(fresh, existing["id"], payload, agent=True)


def _commit_document_test(
    fresh: Workspace,
    existing: dict | None,
    common: dict,
    semantic: str,
    *,
    unit_id: str,
    target: TestGenerateExecutorTarget,
) -> dict:
    steps = common["steps"]
    kind = doc_tests.kind_from_steps(steps)
    items = _document_items_from_steps(steps)
    payload = {
        "title": common["title"],
        "objective": common["objective"],
        "steps": steps,
        "methodology_refs": common["methodology_refs"],
        "rcm_id": common["rcm_id"],
        "agent_run_id": common["agent_run_id"],
        "workflow_parent_sha1": common["workflow_parent_sha1"],
        "kind": kind,
        "items": items,
    }
    if existing is None:
        test = doc_tests.create_test(
            fresh,
            {
                **payload,
                "id": stable_test_id("doctest", semantic),
                "semantic_id": semantic,
            },
        )
    else:
        doc_tests.update_plan(fresh, existing["id"], payload)
        test = doc_tests.apply_spec(fresh, existing["id"], payload)
    _generate_missing_evidence(fresh, test, steps, target=target, unit_id=unit_id)
    return test


def _generate_missing_evidence(
    fresh: Workspace,
    test: dict,
    steps: list[Mapping[str, object]],
    *,
    target: TestGenerateExecutorTarget,
    unit_id: str,
) -> None:
    """Block the test and register one evidence request per unattached step.

    Each request carries its own step's ``missing_evidence`` text; blocking
    itself stays test-level, matching what ``doc_tests.execution_issues`` and
    ``rcm_execution._executable`` already key on (merge plan section 5).
    """
    pairs = list(zip(test.get("items") or [], steps))
    blocked = [(item, step) for item, step in pairs if not item.get("document_ids")]
    if not blocked:
        return
    test["status"] = "blocked"
    reasons = list(
        dict.fromkeys(
            str(step.get("missing_evidence") or "").strip()
            for _, step in blocked
            if str(step.get("missing_evidence") or "").strip()
        )
    )
    test["scope_limitations"] = "; ".join(reasons) or "Required evidence is not yet available."
    evidence_hash = canonical_sha1(
        [
            {key: item.get(key) for key in ("id", "sha1", "category", "title")}
            for item in fresh.documents
        ]
    )
    for item, step in blocked:
        reason = (
            str(step.get("missing_evidence") or "").strip()
            or "Required evidence is not yet available."
        )
        evidence_request = {
            "id": f"ER-{uuid.uuid4().hex[:10].upper()}",
            "rcm_id": target.rcm_id,
            "document_test_id": test["id"],
            "item_id": item["id"],
            "transaction_identifier": _item_identifier(item.get("label") or ""),
            "missing_document_types": ["supporting_evidence"],
            "status": "open",
            "reason": reason,
            "next_action": (
                "Import or attach matching evidence, then continue the audit."
            ),
            "blocked_unit_id": unit_id,
            "evidence_availability_sha1": evidence_hash,
            "created": fresh._updated_now(),
            "updated": fresh._updated_now(),
        }
        fresh.evidence_requests.append(evidence_request)
        item.setdefault("evidence_request_ids", []).append(evidence_request["id"])
    doc_tests.save_test(fresh, test)
    fresh.save()


def execute_test_generation(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one RCM row's generated tests under its parent-hash guard.

    Matching, auditor-edit preservation, and every create/update run inside one
    locked transaction guarded on the RCM row, so a concurrent row change is a
    conflict rather than a silent overwrite, and no test becomes durably
    visible with an intermediate ``draft`` status (merge plan sections 2.1, 8).
    """
    target, specs = _validated_generation(request, raw_target)
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
            common = {
                "title": spec["title"],
                "objective": spec["objective"],
                "steps": spec["steps"],
                "methodology_refs": spec.get("methodology_refs") or [],
                "rcm_id": target.rcm_id,
                "agent_run_id": target.run_id,
                "workflow_parent_sha1": parent_sha1,
            }
            if kind == "datatest":
                item = _commit_data_test(fresh, existing, common, semantic)
            else:
                item = _commit_document_test(
                    fresh, existing, common, semantic,
                    unit_id=request.unit_id, target=target,
                )
            outcomes.append(
                {
                    "kind": kind,
                    "id": item["id"],
                    "action": "created" if existing is None else "updated",
                }
            )
        return outcomes

    committed = mutate(
        target.workspace, commit, expected_parents=request.expected_parents
    )
    target.workspace = committed.workspace
    return _generation_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        outcomes=committed.value,
    )


def reconcile_test_generation(
    request: ExecutorRequest, raw_target: object
) -> ExecutorReconciliation:
    """Classify an interrupted test-generation commit without mutating state.

    Follows the same reconciliation contract as the retired draft executor
    (merge plan section 6): an unchanged RCM-row parent means the commit never
    landed; a changed parent requires every accepted test to match by source,
    semantic id, definition, and workflow parent hash before reporting
    ``already_applied``.
    """
    target, specs = _validated_generation(request, raw_target)
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
                    "The RCM row changed before the interrupted test-generation "
                    "commit was reconciled."
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
                "The RCM row changed before the interrupted test-generation "
                "commit was reconciled."
            ),
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_generation_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            outcomes=outcomes,
        ),
        reason="The accepted tests already hold.",
    )


GENERATE_EXECUTOR = ExecutorDefinition(
    executor_id=GENERATE_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_test_generation)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_test_generation)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_test_generation,
    reconciler=reconcile_test_generation,
)

EXECUTORS.register(GENERATE_EXECUTOR)


__all__ = [
    "GENERATE_EXECUTOR",
    "GENERATE_EXECUTOR_ID",
    "TestGenerateExecutorTarget",
    "execute_test_generation",
    "match_test_revision",
    "reconcile_test_generation",
    "artifact_test_ref",
    "semantic_test_id",
    "stable_test_id",
]
