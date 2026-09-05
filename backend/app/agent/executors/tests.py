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
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from ... import (
    cycle_linking,
    cycle_rulesets,
    cycle_vouching,
    data_tests,
    doc_tests,
)
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
    workspace: Workspace,
    rcm_id: str,
    kind: str,
    semantic: str,
    *,
    revises: str = "",
) -> dict | None:
    """Match one proposed test to an existing one on the same RCM row.

    ``revises`` is the generator naming the test it rewrote, and it is the only
    identity that survives a rewrite.  The semantic id below is derived from the
    title, so a row regenerated with its control phrased even slightly
    differently matched nothing and created a *second* test instead of revising
    the first — one engagement carried both "Confirmation traceability to the
    recorded deal register" and "Confirmation traceability to recorded deals and
    settlement state" on one row, the second a strict superset of the first.
    A key meant to identify a test across regenerations cannot be built from the
    one field regeneration is free to change.  Generation is shown every linked
    test's id precisely so it can point at the one it is replacing.

    The title-derived match stays as the fallback: proposals that name nothing,
    and tests created before generation was asked to, still have to match.
    """
    linked = [item for item in _linked_tests(workspace, rcm_id) if item["kind"] == kind]
    if revises:
        named = next((item for item in linked if item["id"] == revises), None)
        if named is not None:
            return named
    stable_id = stable_test_id(kind, semantic)
    return next(
        (
            item
            for item in linked
            if item["record"].get("semantic_id") == semantic or item["id"] == stable_id
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
    #: Tests the auditor named by id. Naming a test is the permission to replace
    #: it, whoever authored it — the auditor is looking at the record and asking
    #: for this one to be rewritten. It is not permission to touch the rest of
    #: the row, which is why this is a list of ids rather than a wider flag.
    regenerate_test_ids: tuple[str, ...] = ()

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
        self.regenerate_test_ids = tuple(
            text
            for value in (self.regenerate_test_ids or ())
            if (text := str(value or "").strip())
        )


def _may_overwrite(
    target: TestGenerateExecutorTarget, existing: Mapping[str, object]
) -> bool:
    """Whether this commit may replace a test that already exists.

    An agent-authored test is the run's own previous answer and is always
    replaceable. An auditor-authored one is someone's work, and is not — unless
    the auditor named it, which is them pointing at the record and asking for it
    to be redrafted, or unless the run is in permission mode, where a person
    approves each commit before it lands.
    """
    record = existing.get("record") or {}
    if record.get("created_by") == "agent" or target.allow_auditor_overwrite:
        return True
    return str(existing.get("id") or "") in target.regenerate_test_ids


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
        cycle = source == "document" and spec.get("kind") == "cycle_vouch"
        steps = spec.get("steps")
        if cycle:
            if steps not in (None, []):
                raise WorkspaceError("A cycle_vouch test cannot carry steps.")
            spec["steps"] = []
        elif not isinstance(steps, (list, tuple)) or not steps:
            raise WorkspaceError("An accepted test has no steps.")
        elif source == "document" and any(
            str(step.get("mode") or "") == "vouch"
            for step in steps
            if isinstance(step, Mapping)
        ):
            raise WorkspaceError(
                "The removed vouch-step cycle schema cannot be committed; use a "
                "canonical cycle_vouch definition."
            )
        kind = "datatest" if source == "data" else "doctest"
        spec["kind"] = kind
        spec["rcm_id"] = target.rcm_id
        # Carried verbatim from the proposal. Whether it names a test that is
        # still on this row is settled at match time against the locked
        # workspace, not here against a read that may already be stale.
        spec["revises"] = str(spec.get("revises") or "").strip()
        spec["semantic_id"] = (
            cycle_vouching.stable_test_semantic_id(
                {**spec, "kind": "cycle_vouch"}
            )
            if cycle
            else semantic_test_id(kind, target.rcm_id, str(spec["title"]))
        )
        spec["document_kind"] = "cycle_vouch" if cycle else None
        specs.append(spec)
    return target, specs


def _sourced_steps(
    steps: list[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    """Split generated steps into those with attached documents and those without.

    Generation habitually pairs a sourced question with a document-less step
    naming what else would be needed — "Assess discrepancy resolution evidence"
    beside "Identify missing discrepancy resolution evidence". That second step
    is a scope note about the first, not an item anyone can execute, and reading
    it as an unattached item blocked whole tests that had four documents on
    their real question.
    """
    sourced = [step for step in steps if step.get("document_ids")]
    return sourced, [step for step in steps if not step.get("document_ids")]


def _document_items_from_steps(steps: list[Mapping[str, object]]) -> list[dict]:
    """Normalize each generated question step into the existing item model.

    Only question steps map one-to-one onto items. A vouch step is a *plan* for
    items, not an item: its items are one per population row that linked to a
    document, which only the workspace can produce.
    """
    items = []
    for step in steps:
        item = {
            "label": str(step.get("label") or ""),
            "instruction": str(step.get("instruction") or ""),
            "document_ids": [str(value) for value in step.get("document_ids") or []],
        }
        if str(step.get("mode") or "") == "question":
            item["question"] = str(step.get("question") or "")
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
    if common.get("document_kind") == "cycle_vouch":
        return cycle_vouching.build_cycle_vouch_test(
            fresh,
            {
                "id": (
                    existing["id"]
                    if existing is not None
                    else stable_test_id("doctest", semantic)
                ),
                "title": common["title"],
                "objective": common["objective"],
                "rcm_id": common["rcm_id"],
                "registry": common["registry"],
                "requirement_refs": common["requirement_refs"],
                "procedure_key": common["procedure_key"],
                "definition": common["definition"],
                # The manifest the proposal was validated against. The service
                # refuses the commit if evidence moved since, rather than
                # persisting a selection made on facts that no longer hold.
                "context_manifest_sha256": common["context_manifest_sha256"],
                "selection_confirmation": common["selection_confirmation"],
                "methodology_refs": common["methodology_refs"],
                "agent_run_id": common["agent_run_id"],
                "workflow_parent_sha1": common["workflow_parent_sha1"],
            },
        )
    kind = doc_tests.kind_from_steps(steps)
    # ``steps`` stays the complete durable plan, including a step that only
    # names absent evidence. ``items`` is the executable subset: an unattached
    # step alongside sourced ones is a scope note, and carrying it as an item
    # made ``execution_issues`` refuse the whole test.
    sourced, _unsourced = _sourced_steps(steps)
    items = _document_items_from_steps(sourced or steps)
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
    """Register one evidence request per unattached step, blocking only if none is attached.

    Each request carries its own step's ``missing_evidence`` text. A test whose
    every step lacks documents cannot be attempted at all, so it blocks, keyed
    the way ``doc_tests.execution_issues`` and ``rcm_execution._executable``
    already expect (merge plan section 5). A test that *does* have sourced
    questions stays runnable: the evidence is still requested and the gap is
    recorded in ``scope_limitations``, but the auditor gets the answers the
    attached documents can give instead of a wholly blocked test.
    """
    sourced, unsourced = _sourced_steps(steps)
    if not unsourced:
        return
    reasons = list(
        dict.fromkeys(
            str(step.get("missing_evidence") or "").strip()
            for step in unsourced
            if str(step.get("missing_evidence") or "").strip()
        )
    )
    limitation = "; ".join(reasons) or "Required evidence is not yet available."
    # Items mirror the sourced steps, or every step when none was sourced —
    # the same choice ``_commit_document_test`` made when it built them.
    items_by_step = {
        id(step): item
        for item, step in zip(test.get("items") or [], sourced or steps)
    }
    if not sourced:
        test["status"] = "blocked"
        test["scope_limitations"] = limitation
    else:
        existing = str(test.get("scope_limitations") or "").strip()
        test["scope_limitations"] = "; ".join(
            part for part in (existing, limitation) if part
        )
    evidence_hash = canonical_sha1(
        [
            {key: item.get(key) for key in ("id", "sha1", "category", "title")}
            for item in fresh.documents
        ]
    )
    for step in unsourced:
        reason = (
            str(step.get("missing_evidence") or "").strip()
            or "Required evidence is not yet available."
        )
        item = items_by_step.get(id(step))
        label = str(
            (item or {}).get("label") or step.get("label") or ""
        )
        evidence_request = {
            "id": f"ER-{uuid.uuid4().hex[:10].upper()}",
            "rcm_id": target.rcm_id,
            "document_test_id": test["id"],
            "item_id": (item or {}).get("id"),
            "transaction_identifier": _item_identifier(label),
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
        if item is not None:
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
            existing = match_test_revision(
                fresh,
                target.rcm_id,
                kind,
                semantic,
                revises=str(spec.get("revises") or ""),
            )
            if existing and not _may_overwrite(target, existing):
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
                "document_kind": spec.get("document_kind"),
                "registry": spec.get("registry"),
                "requirement_refs": spec.get("requirement_refs") or [],
                "procedure_key": spec.get("procedure_key"),
                "definition": spec.get("definition"),
                "context_manifest_sha256": spec.get("context_manifest_sha256"),
                "selection_confirmation": spec.get("selection_confirmation"),
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

    def changed_parent_reason() -> str:
        return (
            "The RCM row's material fields changed after this test proposal was "
            "generated; no commit was applied. Regenerate the unit. "
            f"Expected parent hash {expected_parent[:12]}, current parent hash "
            f"{current_parent[:12]}."
        )

    # Test creation changes only derived RCM links, which deliberately do not
    # participate in the material parent hash. A same-parent, same-revision
    # record therefore proves no commit landed; a later revision must still
    # inspect the generated tests to distinguish a completed commit from an
    # unrelated workspace write.
    if current_parent == expected_parent and current.revision <= request.expected_revision:
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
    outcomes: list[dict] = []
    for spec in specs:
        kind = str(spec["kind"])
        existing = match_test_revision(
            current,
            target.rcm_id,
            kind,
            str(spec["semantic_id"]),
            revises=str(spec.get("revises") or ""),
        )
        if existing and not _may_overwrite(target, existing):
            outcomes.append({"kind": kind, "id": existing["id"], "action": "preserved"})
            continue
        record = existing["record"] if existing else None
        applied = (
            record is not None
            and str(record.get("title") or "") == str(spec.get("title") or "")
            # The generated record is stamped with the proposal's parent, not
            # the current row. This also recognizes a commit that landed just
            # before a later auditor edit changed the material RCM row.
            and record.get("workflow_parent_sha1") == expected_parent
        )
        if not applied:
            if current_parent == expected_parent:
                return ExecutorReconciliation("not_applied")
            return ExecutorReconciliation(
                "conflict",
                reason=changed_parent_reason(),
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
            reason=changed_parent_reason(),
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


# --------------------------------------------------------------------------- #
# tests.cycle_ruleset
# --------------------------------------------------------------------------- #
CYCLE_RULESET_EXECUTOR_ID = "tests.cycle_ruleset"


@dataclass
class CycleRulesetExecutorTarget:
    """Mutable target for one proposed cycle ruleset."""

    workspace: Workspace
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Cycle ruleset target requires a Workspace.")
        run_id = str(self.run_id or "").strip()
        if not run_id:
            raise ValueError("Cycle ruleset target requires a run_id.")
        self.run_id = run_id


def cycle_ruleset_ref(ruleset_id: str) -> str:
    return f"cycle_ruleset:{ruleset_id}"


def _cycle_ruleset_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    record: Mapping[str, object],
    downgraded: list[dict] | None = None,
    unreachable: list[dict] | None = None,
) -> ExecutorResult:
    ref = cycle_ruleset_ref(str(record.get("ruleset_id") or ""))
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=workspace.revision,
        artifact_refs=[ref],
        applied_parents=dict(request.expected_parents),
        # A ruleset lives in a side store, so ``parent_hashes`` has nothing to
        # say about it; its own rules hash is the honest postcondition. The
        # hash deliberately excludes ``measured``, so a fan-out that moved
        # because documents arrived does not read as a different commit.
        postcondition_hashes={
            ref: hashlib.sha1(
                str(record.get("ruleset_hash") or "").encode("utf-8")
            ).hexdigest()
        },
        output={
            "status": "proposed",
            "ruleset_id": str(record.get("ruleset_id") or ""),
            "ruleset_hash": str(record.get("ruleset_hash") or ""),
            "cycle_label": str(record.get("cycle_label") or ""),
            "roles": len(list(record.get("roles") or [])),
            "join_keys": len(list(record.get("join_keys") or [])),
            "assertions": len(list(record.get("assertions") or [])),
            # Requirements no field these schemas carry could express. The
            # attribute kept its requirement and took the strongest path still
            # open to it, and an auditor is told which control that happened on.
            "downgraded": list(downgraded or []),
            # A position the cycle holds that no join key can reach, because its
            # document type carries no identifier field. Dropped from the rules
            # so the rest of the ruleset can stand, kept on the shape because
            # the step still happens, and reported so it is not a silent gap.
            "unreachable": list(unreachable or []),
        },
    )


def _derived_comparison(
    assertion: Mapping[str, object], roles: Mapping[str, Mapping[str, object]]
) -> dict | None:
    """One matrix comparison, read off the assertion that answers a requirement.

    The matrix says what must be shown; the assertion says which fields show
    it; this restates the second in the vocabulary the first is stored in. The
    translation is the role: a rule names a *position* in the cycle, and a row
    names the document type filling it.
    """

    def operand(side: object) -> dict | None:
        if not isinstance(side, Mapping):
            return None
        role = roles.get(str(side.get("role") or ""))
        document_type = str((role or {}).get("document_type") or "")
        field = str(side.get("field") or "")
        if not document_type or not field:
            return None
        return {"document_type": document_type, "field": field}

    left = operand(assertion.get("left"))
    if left is None:
        return None
    raw_right = assertion.get("right")
    right = None
    if raw_right is not None:
        right = operand(raw_right)
        if right is None:
            return None
    return {
        "key": str(assertion.get("id") or ""),
        "left": left,
        "right": right,
        # The requirement in the terms the control is written in, which is what
        # an auditor reading the row needs and what the rule already carries.
        "rationale": str(
            assertion.get("requirement") or assertion.get("rationale") or ""
        ),
    }


def _contract_matrix_rows(
    fresh: Workspace, proposal: Mapping[str, object]
) -> list[dict]:
    """Write each answered requirement back onto the matrix row that asked it.

    The row schema does not change and neither does anything reading it: the
    matrix has always carried ``required_comparisons`` and downstream has
    always read them there. Only the author moved — to the one turn that has
    this engagement's induced schemas in front of it.

    Returns the downgrades, which the run reports: a requirement no field these
    schemas carry can express is a real limit and an auditor has to be told
    which control it landed on.
    """

    coverage = [
        entry for entry in proposal.get("coverage") or [] if isinstance(entry, Mapping)
    ]
    if not coverage:
        return []
    roles = {
        str(role.get("name") or ""): role
        for role in proposal.get("roles") or []
        if isinstance(role, Mapping)
    }
    assertions = {
        str(item.get("id") or ""): item
        for item in proposal.get("assertions") or []
        if isinstance(item, Mapping)
    }
    by_row: dict[str, list[Mapping[str, object]]] = {}
    for entry in coverage:
        by_row.setdefault(str(entry.get("rcm_id") or ""), []).append(entry)
    rows = {str(row.get("id") or ""): row for row in fresh.rcm or []}
    # Profiled once. The fallback asks which population bears on a row, and
    # answering that per attribute would re-profile every table each time.
    answers = cycle_linking.tabular_evidence_answers(fresh)
    downgraded: list[dict] = []
    for rcm_id, entries in by_row.items():
        row = rows.get(rcm_id)
        if row is None:
            # The matrix moved under the proposal. Not this executor's to
            # repair: the unit's guarded parents are these rows, so a rewritten
            # one conflicts the commit rather than reaching here.
            continue
        working = dict(row)
        for entry in entries:
            attribute_key = str(entry.get("control_attribute") or "")
            if entry.get("unsupported"):
                working = cycle_linking.downgrade_uncontracted(
                    fresh, working, attribute_key, answers=answers
                )
                downgraded.append({
                    "rcm_id": rcm_id,
                    "control_attribute": attribute_key,
                    "reason": str(entry.get("reason") or ""),
                })
                continue
            assertion = assertions.get(str(entry.get("assertion_id") or ""))
            comparison = (
                _derived_comparison(assertion, roles) if assertion is not None else None
            )
            if comparison is None:
                continue
            working = {
                **working,
                "control_attributes": [
                    (
                        {**attribute, "required_comparisons": [comparison]}
                        if isinstance(attribute, Mapping)
                        and str(attribute.get("key") or "") == attribute_key
                        and cycle_linking.uncontracted(attribute)
                        else attribute
                    )
                    for attribute in working.get("control_attributes") or []
                ],
            }
        try:
            # Exact, against this engagement's schemas. ``update_rcm`` validates
            # shape alone — it has no workspace to check against — so a field a
            # schema does not state would otherwise be persisted and surface
            # three stages on as a cycle test that cannot be generated.
            attributes = cycle_vouching.validate_control_attributes(
                working.get("control_attributes"), workspace=fresh
            )
        except cycle_vouching.CycleSchemaError as error:
            raise WorkspaceError(
                f"RCM row '{rcm_id}': {error}"
            ) from error
        fresh.update_rcm(rcm_id, {"control_attributes": attributes}, agent=True)
    return downgraded


def _anchor_population(cycle: Mapping[str, object]) -> dict:
    """The population a cycle test starts from, and the step that holds it."""

    for step in cycle.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        for population in step.get("populations") or []:
            if isinstance(population, Mapping) and population.get("anchor"):
                return {"step": step, "population": population}
    return {}


def _bound_to_the_cycle(proposal: Mapping[str, object], cycle: object) -> dict:
    """Fill in from the shape what the model was told not to write.

    The anchor's table and column are the shape's, not the response's: the
    governing rule of the redesign is that the model names things and local code
    finds them, and a table name copied by a model is an identifier it can get
    wrong. A role the response declared unreachable is dropped from the stored
    rules — ``cycle_rulesets.validate`` would refuse the whole ruleset over a
    role no join key reaches — while the shape keeps it, because it is still a
    step of the process.
    """

    bound = _plain_json(proposal)
    unreachable = {
        str(item.get("role"))
        for item in bound.pop("unreachable", None) or []
        if isinstance(item, Mapping) and item.get("role")
    }
    if unreachable:
        bound["roles"] = [
            role
            for role in bound.get("roles") or []
            if str(role.get("name")) not in unreachable
        ]
    if not isinstance(cycle, Mapping) or not cycle.get("steps"):
        return bound
    anchored = _anchor_population(cycle)
    population = anchored.get("population") or {}
    table = str(population.get("table") or "")
    if not table:
        return bound
    anchor = dict(bound.get("anchor") or {})
    anchor["table"] = table
    # Only the *table* is the shape's. Which of its columns carries the
    # identifier is a judgement about the data, and the proposing turn is shown
    # every column to make it — so the response keeps that field, except where
    # the shape has already named the columns a borrowed population lives in.
    #
    # Deriving it instead cost a treasuryfull ruleset: the role's document field
    # (`deal_reference`) was written in as the column, `04_deals` carries
    # `DEAL_ID`, and nothing refused it. `cycle_rulesets.validate` now does.
    columns = [str(value) for value in population.get("columns") or []]
    if columns:
        anchor["column"] = columns[0]
    bound["anchor"] = anchor
    return bound


def execute_cycle_ruleset(
    request: ExecutorRequest, raw_target: object
) -> ExecutorResult:
    """Store one model-proposed cycle ruleset. Proposing is never approving.

    The record lands with ``status: proposed`` and no approver, which is the
    whole point of the separation: an agent authors rules, and only an auditor
    reading the measured fan-out can make them able to produce a result.
    """

    if not isinstance(raw_target, CycleRulesetExecutorTarget):
        raise WorkspaceError("Unsupported cycle ruleset target.")
    proposal = request.proposal
    if not isinstance(proposal, Mapping):
        raise WorkspaceError("A cycle ruleset commit requires a proposal.")
    target = raw_target
    state: dict[str, object] = {}
    unreachable = [
        _plain_json(item)
        for item in (proposal.get("unreachable") or [])
        if isinstance(item, Mapping)
    ]

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        # Read inside the guarded callback, like everything else this commit
        # rests on: ``planning:cycle`` is one of the unit's parents, so the
        # shape the anchor is bound against here is the one the proposal was
        # written against or the commit does not happen at all.
        bound = _bound_to_the_cycle(proposal, fresh.planning.get("cycle"))
        record = cycle_rulesets.save(fresh, bound, proposed_by="agent")
        # Inside the same guarded callback as the save: the unit's parents are
        # the matrix rows these comparisons land on, so a row rewritten since
        # the proposal was authored conflicts the whole commit rather than
        # being quietly overwritten with a contract answering the old wording.
        state["downgraded"] = _contract_matrix_rows(fresh, proposal)
        return record

    # Through the transaction even though the ruleset lands in a side store,
    # for the reason the schema freeze is: it takes the write lock, re-checks
    # the schemas it was written against have not moved underneath, and
    # publishes a revision so the proposal is an event rather than a file.
    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _cycle_ruleset_result(
        request,
        committed.workspace,
        revision_before=int(state["revision_before"]),
        record=dict(committed.value),
        downgraded=list(state.get("downgraded") or []),
        unreachable=unreachable,
    )


def reconcile_cycle_ruleset(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted proposal commit.

    Keyed on the rules hash rather than on the ruleset id: the id is minted at
    save time, so a resumed run cannot know the one its interrupted attempt
    produced. A stored proposal asserting exactly these rules *is* this commit,
    and re-running would file a second identical proposal for an auditor to
    choose between for no reason.
    """

    if not isinstance(raw_target, CycleRulesetExecutorTarget):
        raise WorkspaceError("Unsupported cycle ruleset target.")
    target = raw_target
    proposal = request.proposal
    if not isinstance(proposal, Mapping):
        return ExecutorReconciliation("not_applied")
    current = Workspace(target.workspace.root)
    try:
        expected = cycle_rulesets.validate(current, _plain_json(proposal))
    except WorkspaceError:
        # The schemas moved under the proposal, so it is not this commit and
        # cannot become it. Re-running is what reports that honestly.
        return ExecutorReconciliation("not_applied")
    stored = next(
        (
            record
            for record in cycle_rulesets.list_rulesets(current)
            if str(record.get("ruleset_hash") or "")
            == str(expected.get("ruleset_hash") or "")
        ),
        None,
    )
    if stored is None:
        return ExecutorReconciliation("not_applied")
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_cycle_ruleset_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            record=stored,
        ),
        reason="This run's cycle ruleset proposal already holds.",
    )


CYCLE_RULESET_EXECUTOR = ExecutorDefinition(
    executor_id=CYCLE_RULESET_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_cycle_ruleset,
    reconciler=reconcile_cycle_ruleset,
)

EXECUTORS.register(CYCLE_RULESET_EXECUTOR)


GENERATE_EXECUTOR = ExecutorDefinition(
    executor_id=GENERATE_EXECUTOR_ID,
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_test_generation,
    reconciler=reconcile_test_generation,
)

EXECUTORS.register(GENERATE_EXECUTOR)


__all__ = [
    "CYCLE_RULESET_EXECUTOR",
    "CYCLE_RULESET_EXECUTOR_ID",
    "CycleRulesetExecutorTarget",
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
