"""Deterministic executors for audit reporting/verification capabilities.

These executors perform no model calls. ``verify_audit`` is a pure, read-only
computation of the audit-verification outcome; ``generate_working_paper`` renders
and commits one RCM working paper; ``curate_dashboard`` pins RCM-linked result
tiles. All bind through the scheduler's deterministic execution path —
``audit.verified`` (read-only), ``working_papers.generated`` (per-RCM commit),
``dashboard.curated`` (RCM-parent-guarded curation), and
``report.working_draft`` (deterministic assembly with auditor-edit
reconciliation).

``execute_finding`` is the one registered pipeline executor here: it commits a
finding draft under its observation parent guard after deriving every reference
from the observation itself.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass

from .. import capabilities as audit_capabilities
from ... import dashboard, doc_tests, findings, rcm_execution, report, working_papers
from ...workspace_transactions import ParentConflict, mutate, parent_hashes
from ...workspaces import Workspace, WorkspaceError
from .model import (
    EXECUTORS,
    ExecutorConcurrency,
    ExecutorDefinition,
    ExecutorReconciliation,
    ExecutorRequest,
    ExecutorResult,
)

VERIFICATION_REF = "audit:verification"
WORKING_PAPER_REF_PREFIX = "working_paper"
DASHBOARD_TILE_REF_PREFIX = "tile"


def working_paper_ref(rcm_id: str) -> str:
    """The stable artifact reference for one RCM working paper."""

    return f"{WORKING_PAPER_REF_PREFIX}:{rcm_id}"


def generate_working_paper(workspace: Workspace, rcm_id: str) -> str:
    """Render and commit one RCM working paper; return its artifact reference.

    Deterministic and self-committing: ``working_papers.generate_rcm`` renders the
    paper from current RCM and linked execution state, then writes it under a
    parent-hash-guarded ``mutate``. A changed RCM parent therefore surfaces as a
    :class:`WorkspaceConflict` instead of overwriting a paper generated against a
    different basis. No model call is involved.
    """

    working_papers.generate_rcm(workspace, rcm_id)
    return working_paper_ref(rcm_id)


def dashboard_tile_ref(tile_id: str) -> str:
    """The stable artifact reference for one curated dashboard tile."""

    return f"{DASHBOARD_TILE_REF_PREFIX}:{tile_id}"


def curate_dashboard(workspace: Workspace, run_id: str | None = None) -> list[str]:
    """Curate RCM dashboard tiles; return their stable artifact references.

    Deterministic and self-committing: ``dashboard.curate_rcm_tiles`` scores the
    current RCM-linked results and pins the strongest under a commit guarded on
    the RCM's material hash, so a changed RCM basis surfaces as a
    :class:`WorkspaceConflict` instead of pinning against a stale matrix. No model
    call is involved.
    """

    result = dashboard.curate_rcm_tiles(workspace, run_id=run_id)
    return [dashboard_tile_ref(tile["id"]) for tile in result["tiles"]]


REPORT_DRAFT_REF = "report:draft"


def generate_report_draft(
    workspace: Workspace,
    *,
    run_id: str | None = None,
    workflow: Mapping[str, object] | None = None,
) -> tuple[str, bool]:
    """Assemble and commit the report working draft; return its ref and whether
    an auditor-edited draft still needs reconciliation.

    Deterministic: the workflow path assembles the draft from current planning,
    results, and findings without a model call (``use_model=False``), so this
    capability has no worker. ``report.generate`` preserves an auditor-edited
    ``markdown`` and records the new candidate as ``generated_markdown``, which
    is what ``requires_reconcile`` reports.
    """

    result = report.generate(
        workspace,
        use_model=False,
        run_id=run_id,
        workflow=dict(workflow or {}) or None,
    )
    return REPORT_DRAFT_REF, bool(result.get("requires_reconcile"))


# The output capabilities whose structural readiness gates audit completion.
_OUTPUT_CAPABILITIES = (
    "working_papers.generated",
    "dashboard.curated",
    "report.working_draft",
)


def output_issues(outcome: dict) -> list[str]:
    """Output capabilities that are not structurally satisfied."""

    return [
        capability
        for capability, readiness in (outcome.get("output_readiness") or {}).items()
        if readiness.get("state") != "satisfied"
    ]


def verify_audit(workspace: Workspace) -> dict:
    """Compute the deterministic audit-verification outcome (read-only).

    The outcome combines RCM completion, report quality, and the structural
    readiness of the audit's output capabilities. It mutates nothing; the caller
    records it on the run and derives the terminal unit status from it.
    """

    completion = rcm_execution.completion(workspace)
    quality = report.quality_checks(workspace)
    errors = [
        item for item in quality.get("issues") or [] if item.get("severity") == "error"
    ]
    output_states = {
        capability: audit_capabilities.REGISTRY.get(capability)
        .readiness(workspace, {})
        .payload()
        for capability in _OUTPUT_CAPABILITIES
    }
    issues = [
        capability
        for capability, readiness in output_states.items()
        if readiness.get("state") != "satisfied"
    ]
    return {
        "audit_complete": completion["status"] == "completed"
        and not errors
        and not issues,
        "completion_status": completion["status"],
        "planned_tests_total": sum(
            len(row.get("planned_tests") or []) for row in workspace.rcm
        ),
        "planned_tests_completed": sum(
            str(item.get("status") or "").startswith("completed")
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "planned_tests_review_required": sum(
            item.get("status") == "review_required"
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "planned_tests_blocked": sum(
            item.get("status") == "blocked"
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "data_tests_required": sum(
            "datatest"
            in rcm_execution.required_execution_kinds(item.get("method") or "")
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "data_tests_executed": sum(
            bool(item.get("last_run"))
            for item in workspace.data_tests
            if item.get("planned_test_id")
        ),
        "document_tests_required": sum(
            "doctest"
            in rcm_execution.required_execution_kinds(item.get("method") or "")
            for row in workspace.rcm
            for item in row.get("planned_tests") or []
        ),
        "document_tests_executed": sum(
            item.get("status") == "completed"
            for item in doc_tests.list_tests(workspace)
        ),
        "open_observations": len(completion.get("open_observations") or []),
        "supported_findings": sum(
            item.get("auditor_confirmed")
            and not findings.support_issues(workspace, item)
            for item in workspace.findings
        ),
        "draft_findings": sum(
            not item.get("auditor_confirmed") for item in workspace.findings
        ),
        "report_quality_ok": not errors,
        "report_quality_errors": len(errors),
        "output_readiness": output_states,
        "open_gate_count": len(completion.get("open_observations") or [])
        + int((completion.get("coverage") or {}).get("issue_count") or 0)
        + len(errors)
        + len(issues),
    }


# --------------------------------------------------------------------------- #
# reporting.finding executor (P7H.2)
# --------------------------------------------------------------------------- #
FINDING_EXECUTOR_ID = "reporting.finding"
# The narrative fields an accepted proposal may write. Every reference field —
# RCM, planned test, execution, and evidence — is derived from the observation.
FINDING_FIELDS = (
    "title",
    "severity",
    "condition",
    "criteria",
    "cause",
    "cause_pending",
    "effect",
    "recommendation",
    "severity_rationale",
)


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def finding_ref(finding_id: str) -> str:
    """The stable artifact reference for one durable finding."""

    return f"finding:{finding_id}"


def finding_semantic_id(observation_id: str) -> str:
    return f"finding:observation:{observation_id}"


def finding_stable_id(semantic: str) -> str:
    return "F-" + hashlib.sha1(semantic.encode()).hexdigest()[:6].upper()


@dataclass
class FindingExecutorTarget:
    """Mutable target for one observation's finding-draft commit."""

    workspace: Workspace
    run_id: str
    observation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Workspace):
            raise ValueError("Finding executor target requires a Workspace.")
        for field_name in ("run_id", "observation_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Finding executor target requires a {field_name}.")
            setattr(self, field_name, value)


def _validated_finding(
    request: ExecutorRequest,
    target: object,
) -> tuple[FindingExecutorTarget, dict, str]:
    if not isinstance(target, FindingExecutorTarget):
        raise WorkspaceError("Finding executor requires a FindingExecutorTarget.")
    parent_ref = f"observation:{target.observation_id}"
    if set(request.expected_parents) != {parent_ref}:
        raise WorkspaceError(
            "Finding executor requires exactly its observation parent hash."
        )
    raw = request.proposal.get("finding")
    if not isinstance(raw, Mapping):
        raise WorkspaceError("The accepted finding proposal is empty.")
    draft = {key: raw[key] for key in FINDING_FIELDS if key in raw}
    if not str(draft.get("title") or "").strip():
        raise WorkspaceError("The accepted finding proposal has no title.")
    return target, draft, finding_semantic_id(target.observation_id)


def _observation(workspace: Workspace, observation_id: str) -> dict:
    observation = next(
        (
            item
            for item in workspace.observations
            if str(item.get("id")) == observation_id
        ),
        None,
    )
    if observation is None:
        raise WorkspaceError(f"Observation '{observation_id}' not found.")
    return observation


def _finding_result(
    request: ExecutorRequest,
    workspace: Workspace,
    *,
    revision_before: int,
    item: Mapping[str, object],
) -> ExecutorResult:
    refs = [finding_ref(str(item["id"]))]
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
            "status": "created",
            "id": str(item["id"]),
            "semantic_id": str(item.get("semantic_id") or ""),
            "action": "created",
        },
    )


def execute_finding(request: ExecutorRequest, raw_target: object) -> ExecutorResult:
    """Commit one accepted finding draft under its observation parent guard.

    Every reference the finding carries — RCM row, planned test, execution
    result, and the immutable evidence anchor — is derived from the current
    observation rather than taken from the proposal, and the draft must pass
    deterministic support validation before it is written. The finding id is
    derived from the observation, so a repeated commit is idempotent instead of
    creating a second draft for the same observation.
    """
    target, draft, semantic = _validated_finding(request, raw_target)
    state: dict[str, int] = {}

    def commit(fresh: Workspace) -> dict:
        state["revision_before"] = fresh.revision
        observation = _observation(fresh, target.observation_id)
        execution_ref = str(observation["execution_ref"])
        anchor = findings.anchor_from_ref(fresh, execution_ref, run_id=target.run_id)
        item = {
            **draft,
            "id": finding_stable_id(semantic),
            "semantic_id": semantic,
            "agent_run_id": target.run_id,
            "source_observation_id": target.observation_id,
            "rcm_refs": [observation["rcm_id"]],
            "procedure_refs": [],
            "planned_test_refs": [observation["planned_test_id"]],
            "execution_refs": [execution_ref],
            "evidence_refs": [anchor] if anchor else [],
            "auditor_confirmed": False,
        }
        issues = findings.support_issues(fresh, item)
        if issues:
            raise WorkspaceError(
                "Finding draft failed support validation: " + "; ".join(issues)
            )
        return findings.add(fresh, item, source="agent")

    committed = mutate(
        target.workspace,
        commit,
        expected_parents=request.expected_parents,
    )
    target.workspace = committed.workspace
    return _finding_result(
        request,
        committed.workspace,
        revision_before=state["revision_before"],
        item=committed.value,
    )


def reconcile_finding(
    request: ExecutorRequest,
    raw_target: object,
) -> ExecutorReconciliation:
    """Classify an interrupted finding commit without changing workspace state.

    Writing a finding does not change the guarded observation, so parent
    equality cannot prove the commit never ran. The observation-derived finding
    id does: when this run's draft for that observation already exists, the
    commit is proven applied.
    """
    target, _draft, semantic = _validated_finding(request, raw_target)
    parent_ref = f"observation:{target.observation_id}"
    current = Workspace(target.workspace.root)
    current_parent = parent_hashes(current, [parent_ref])[parent_ref]
    expected_parent = request.expected_parents[parent_ref]
    if current_parent != expected_parent:
        return ExecutorReconciliation(
            "conflict",
            reason=str(
                ParentConflict(
                    parent_ref, expected_parent, current_parent, current.revision
                )
            ),
        )
    existing = next(
        (
            item
            for item in current.findings
            if item.get("semantic_id") == semantic
            or item.get("id") == finding_stable_id(semantic)
        ),
        None,
    )
    if existing is None or current.revision <= request.expected_revision:
        return ExecutorReconciliation("not_applied")
    if existing.get("agent_run_id") != target.run_id:
        return ExecutorReconciliation(
            "conflict",
            reason="A different finding already covers this observation.",
        )
    target.workspace = current
    return ExecutorReconciliation(
        "already_applied",
        result=_finding_result(
            request,
            current,
            revision_before=max(request.expected_revision, current.revision - 1),
            item=existing,
        ),
        reason="The accepted finding draft already holds.",
    )


FINDING_EXECUTOR = ExecutorDefinition(
    executor_id=FINDING_EXECUTOR_ID,
    implementation_hash=_sha256_text(inspect.getsource(execute_finding)),
    reconciliation_hash=_sha256_text(inspect.getsource(reconcile_finding)),
    concurrency=ExecutorConcurrency("parent_hashes"),
    implementation=execute_finding,
    reconciler=reconcile_finding,
)

EXECUTORS.register(FINDING_EXECUTOR)


__all__ = [
    "DASHBOARD_TILE_REF_PREFIX",
    "FINDING_EXECUTOR",
    "FINDING_EXECUTOR_ID",
    "FINDING_FIELDS",
    "FindingExecutorTarget",
    "REPORT_DRAFT_REF",
    "VERIFICATION_REF",
    "WORKING_PAPER_REF_PREFIX",
    "curate_dashboard",
    "dashboard_tile_ref",
    "execute_finding",
    "finding_ref",
    "finding_semantic_id",
    "finding_stable_id",
    "generate_report_draft",
    "generate_working_paper",
    "output_issues",
    "reconcile_finding",
    "verify_audit",
    "working_paper_ref",
]
