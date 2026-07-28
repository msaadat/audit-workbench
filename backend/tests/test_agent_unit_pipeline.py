from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app import workspaces
from app.agent import runner as agent_runner
from app.agent import store, workflow
from app.agent.context import ContextBundle, ContextManifest, ContextSize
from app.agent.executors import (
    ExecutorConcurrency,
    ExecutorDefinition,
    ExecutorReconciliation,
    ExecutorRegistry,
    ExecutorResult,
)
from app.agent.runtime import DefaultRunRuntime
from app.agent.runtime.unit_pipeline import (
    UnitPipeline,
    UnitPipelineError,
    UnitPipelineRequest,
    UnitSidecarStore,
)
from app.agent.runtime.workflow_runner import (
    BoundUnitPipeline,
    CapabilityExecution,
    CapabilityExecutionRegistry,
    WorkflowRunner,
)
from app.agent.workers import (
    WorkerDefinition,
    WorkerRegistry,
    WorkerRepairPolicy,
    WorkerResponseSchema,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
PARENT = "c" * 40
POST = "d" * 40


class _Gateway:
    def complete(self, system, user, activity=None, *, attempt=1):
        return "unused"


def _context(capability="planning.apm_ready", unit="planning.apm"):
    size = ContextSize(items=0, characters=0, estimated_tokens=0)
    return (
        ContextManifest(
            capability_id=capability,
            unit_id=unit,
            context_spec_hash=HASH_A,
            resolver_hash=HASH_B,
            selections=(),
            omissions=(),
            truncations=(),
            privacy_decisions=(),
            supplied_size=size,
        ),
        ContextBundle(
            capability_id=capability,
            unit_id=unit,
            items=(),
            supplied_size=size,
        ),
    )


def _committed_result(request, *, before=None, after=None):
    revision_before = request.expected_revision if before is None else before
    revision_after = revision_before + 1 if after is None else after
    return ExecutorResult(
        executor_id=request.executor_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=revision_after,
        artifact_refs=["planning:apm"],
        applied_parents=dict(request.expected_parents),
        postcondition_hashes={"planning:apm": POST},
    )


def _pipeline(workspace, events, *, executor=None, reconciler=None):
    run = store.new_command_run(
        workspace,
        "auto",
        {"source": "chat", "text": "Draft the APM"},
    )
    runtime = DefaultRunRuntime(
        workspace=workspace,
        run=run,
        state_lock=threading.RLock(),
    )
    workers = WorkerRegistry()
    workers.register(
        WorkerDefinition(
            worker_id="planning.apm",
            implementation_hash=HASH_A,
            prompt_hash=HASH_B,
            response_schema=WorkerResponseSchema(
                "planning.apm.response",
                HASH_A,
                lambda response: {"apm_markdown": response},
            ),
            repair_policy=WorkerRepairPolicy(0),
            implementation=lambda request, gateway, attempt: events.append("worker")
            or "# Draft APM",
        )
    )
    executors = ExecutorRegistry()

    def default_executor(request, target):
        events.append("executor")
        return _committed_result(request)

    executors.register(
        ExecutorDefinition(
            executor_id="planning.apm",
            implementation_hash=HASH_A,
            reconciliation_hash=HASH_B,
            concurrency=ExecutorConcurrency("parent_hashes"),
            implementation=executor or default_executor,
            reconciler=reconciler
            or (lambda request, target: ExecutorReconciliation("not_applied")),
        )
    )
    return (
        UnitPipeline(
            runtime=runtime,
            gateway=_Gateway(),
            workers=workers,
            executors=executors,
            sidecars=UnitSidecarStore(workspace, run["id"]),
        ),
        run,
    )


def _request(workspace):
    return UnitPipelineRequest(
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        worker_id="planning.apm",
        executor_id="planning.apm",
        unit_input={"input_sha1": "unit-input"},
        activity={"artifact_refs": ["planning:apm"]},
        expected_revision=workspace.revision,
        expected_parents={"planning:context": PARENT},
        capability_definition_hash=HASH_A,
        approval_kind="apm",
    )


def _context_identity(manifest):
    return {
        "manifest_hash": manifest.manifest_hash,
        "context_spec_hash": manifest.context_spec_hash,
        "resolver_hash": manifest.resolver_hash,
        "selector_definition_hashes": [],
    }


def test_pipeline_orders_manifest_worker_proposal_approval_executor_receipt_readiness():
    workspace = workspaces.create_workspace("Unit pipeline")
    events = []
    pipeline, run = _pipeline(workspace, events)
    run_root = store.run_dir(workspace, run["id"])

    def context_provider():
        events.append("context")
        return _context()

    def approve(proposal):
        events.append("approval")
        manifest_path = run_root / "contexts" / "planning.apm.json"
        proposal_path = run_root / "proposals" / "planning.apm.json"
        assert manifest_path.is_file()
        assert json.loads(proposal_path.read_text(encoding="utf-8"))["status"] == "proposed"
        assert not (run_root / "receipts" / "planning.apm.json").exists()
        return {**dict(proposal), "apm_markdown": "# Auditor-edited APM"}

    def readiness():
        events.append("readiness")
        assert (run_root / "receipts" / "planning.apm.json").is_file()
        return {"state": "satisfied"}

    outcome = pipeline.run(
        _request(workspace),
        context_provider=context_provider,
        context_identity_provider=_context_identity,
        target=workspace,
        approval_provider=approve,
        readiness_provider=readiness,
    )

    assert events == ["context", "worker", "approval", "executor", "readiness"]
    assert outcome.status == "succeeded"
    assert outcome.receipt is not None
    assert outcome.receipt_reference["receipt_hash"] == outcome.receipt.receipt_hash
    proposal = json.loads(
        (run_root / outcome.proposal_reference["path"]).read_text(encoding="utf-8")
    )
    assert proposal["status"] == "accepted"
    assert proposal["proposal"]["apm_markdown"] == "# Auditor-edited APM"


def test_runtime_scheduler_resolves_capability_through_registered_unit_pipeline():
    workspace = workspaces.create_workspace("Scheduler pipeline registry")
    events = []
    pipeline, run = _pipeline(workspace, events)
    pipeline.runtime.handle = agent_runner.RunHandle(workspace.id, run["id"])
    registry = workflow.CapabilityRegistry()
    registry.register(
        workflow.Capability(
            id="planning.apm_ready",
            stage_id="stage:planning-apm",
            title="Draft the APM",
            worker_kind="planning.apm",
            depends_on=(),
            readiness=lambda _subject, _scope: workflow.Readiness("missing"),
            expand_units=lambda _subject, _scope: [
                workflow.UnitSpec(
                    id="planning.apm",
                    kind="apm",
                    title="Draft the APM",
                    input_payload={"input_sha1": "unit-input"},
                )
            ],
            context="planning.apm",
        )
    )
    executions = CapabilityExecutionRegistry()
    executions.register(
        CapabilityExecution(
            capability_id="planning.apm_ready",
            implementation_hash=HASH_A,
            pipeline_binder=lambda subject, _run, _capability, _stage, _unit: (
                BoundUnitPipeline(
                    request=_request(subject),
                    context_provider=_context,
                    context_identity_provider=_context_identity,
                    target=subject,
                    readiness_provider=lambda: {"state": "satisfied"},
                )
            ),
        )
    )
    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=pipeline.runtime,
        registry=registry,
        executions=executions,
        unit_pipeline=pipeline,
    )

    scheduler.materialize(["planning.apm_ready"])
    scheduler.execute()

    unit = run["workflow"]["stages"][0]["units"][0]
    assert events == ["worker", "executor"], (run.get("error"), unit)
    assert unit["status"] == "succeeded"
    assert unit["context_manifest"]["path"] == "contexts/planning.apm.json"
    assert unit["proposal_sidecar"]["path"] == "proposals/planning.apm.json"
    assert unit["receipt_sidecar"]["path"] == "receipts/planning.apm.json"
    assert unit["result_refs"] == ["planning:apm"]
    assert run["status"] == "completed"


def test_serial_pipeline_units_rebind_after_each_commit():
    workspace = workspaces.create_workspace("Scheduler serial refresh")
    events = []
    pipeline, run = _pipeline(workspace, events)
    pipeline.runtime.handle = agent_runner.RunHandle(workspace.id, run["id"])
    registry = workflow.CapabilityRegistry()
    registry.register(
        workflow.Capability(
            id="planning.apm_ready",
            stage_id="stage:planning-apm",
            title="Draft the APM",
            worker_kind="planning.apm",
            depends_on=(),
            readiness=lambda _subject, _scope: workflow.Readiness("missing"),
            expand_units=lambda _subject, _scope: [
                workflow.UnitSpec(
                    id="planning.apm:first",
                    kind="apm",
                    title="Draft the first APM",
                    input_payload={"input_sha1": "first"},
                ),
                workflow.UnitSpec(
                    id="planning.apm:second",
                    kind="apm",
                    title="Draft the second APM",
                    input_payload={"input_sha1": "second"},
                ),
            ],
            context="planning.apm",
        )
    )
    bound_subject_ids = []
    executions = CapabilityExecutionRegistry()
    executions.register(
        CapabilityExecution(
            capability_id="planning.apm_ready",
            implementation_hash=HASH_A,
            pipeline_binder=lambda subject, _run, _capability, _stage, unit: (
                bound_subject_ids.append(id(subject))
                or BoundUnitPipeline(
                    request=UnitPipelineRequest(
                        **{**_request(subject).__dict__, "unit_id": unit["id"]}
                    ),
                    context_provider=lambda: _context(unit=unit["id"]),
                    context_identity_provider=_context_identity,
                    target=subject,
                    readiness_provider=lambda: {"state": "satisfied"},
                )
            ),
        )
    )
    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=pipeline.runtime,
        registry=registry,
        executions=executions,
        unit_pipeline=pipeline,
        refresh_subject=lambda: workspaces.load_workspace(workspace.id),
    )

    scheduler.materialize(["planning.apm_ready"])
    scheduler.execute()

    assert len(bound_subject_ids) == 2
    assert len(set(bound_subject_ids)) == 2


def test_pipeline_percent_encodes_windows_reserved_semantic_unit_characters():
    workspace = workspaces.create_workspace("Portable unit sidecars")
    events = []
    pipeline, run = _pipeline(workspace, events)
    request = UnitPipelineRequest(
        **{**_request(workspace).__dict__, "unit_id": "planning.apm:workspace"}
    )

    outcome = pipeline.run(
        request,
        context_provider=lambda: _context(unit=request.unit_id),
        context_identity_provider=_context_identity,
        target=workspace,
    )

    assert outcome.manifest_reference["path"] == (
        "contexts/planning.apm%3Aworkspace.json"
    )
    assert outcome.proposal_reference["path"] == (
        "proposals/planning.apm%3Aworkspace.json"
    )
    assert outcome.receipt_reference["path"] == (
        "receipts/planning.apm%3Aworkspace.json"
    )
    run_root = store.run_dir(workspace, run["id"])
    assert (run_root / outcome.manifest_reference["path"]).is_file()
    assert (run_root / outcome.proposal_reference["path"]).is_file()
    assert (run_root / outcome.receipt_reference["path"]).is_file()


def test_pipeline_persists_proposal_before_rejected_approval_and_skips_executor():
    workspace = workspaces.create_workspace("Rejected unit pipeline")
    events = []
    pipeline, run = _pipeline(workspace, events)

    outcome = pipeline.run(
        _request(workspace),
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
        approval_provider=lambda proposal: events.append("approval") or None,
    )

    assert events == ["worker", "approval"]
    assert outcome.status == "approval_rejected"
    assert outcome.receipt is None
    assert (store.run_dir(workspace, run["id"]) / outcome.proposal_reference["path"]).is_file()
    assert not (store.run_dir(workspace, run["id"]) / "receipts").exists()


def test_pipeline_rejects_context_identity_before_worker_or_side_effects():
    workspace = workspaces.create_workspace("Mismatched unit pipeline")
    events = []
    pipeline, run = _pipeline(workspace, events)

    with pytest.raises(UnitPipelineError, match="identity does not match"):
        pipeline.run(
            _request(workspace),
            context_provider=lambda: _context(unit="other"),
            context_identity_provider=_context_identity,
            target=workspace,
        )

    assert events == []
    assert not (store.run_dir(workspace, run["id"]) / "proposals").exists()


def test_pipeline_persists_receipt_before_failed_readiness_recheck():
    workspace = workspaces.create_workspace("Readiness pipeline")
    events = []
    pipeline, run = _pipeline(workspace, events)

    with pytest.raises(UnitPipelineError, match="readiness is not satisfied"):
        pipeline.run(
            _request(workspace),
            context_provider=_context,
            context_identity_provider=_context_identity,
            target=workspace,
            readiness_provider=lambda: {"state": "missing"},
        )

    assert events == ["worker", "executor"]
    assert (store.run_dir(workspace, run["id"]) / "receipts" / "planning.apm.json").is_file()


def test_exact_proposal_identity_reuses_sidecar_without_worker_rebilling():
    workspace = workspaces.create_workspace("Proposal reuse")
    first_events = []
    first, run = _pipeline(workspace, first_events)
    request = _request(workspace)
    first.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
    )
    (store.run_dir(workspace, run["id"]) / "receipts" / "planning.apm.json").unlink()

    second_events = []
    runtime = DefaultRunRuntime(
        workspace=workspace,
        run=run,
        state_lock=threading.RLock(),
    )
    second_pipeline, _unused = _pipeline(workspace, second_events)
    second_pipeline.runtime = runtime
    second_pipeline.sidecars = UnitSidecarStore(workspace, run["id"])
    outcome = second_pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
    )

    assert second_events == ["executor"]
    assert outcome.proposal_reused is True
    assert outcome.proposal_reuse_rejection_reasons == ()


@pytest.mark.parametrize(
    "change, expected_reason",
    [
        ("capability", "capability_definition_changed"),
        ("unit_input", "unit_input_changed"),
        ("context", "exact_context_changed"),
        ("selector", "selector_definitions_changed"),
        ("prompt", "prompt_changed"),
    ],
)
def test_proposal_reuse_reports_exact_incompatibility_reason(change, expected_reason):
    workspace = workspaces.create_workspace(f"Proposal rejection {change}")
    events = []
    pipeline, run = _pipeline(workspace, events)
    request = _request(workspace)
    pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
    )
    (store.run_dir(workspace, run["id"]) / "receipts" / "planning.apm.json").unlink()
    events.clear()

    next_request = request
    context_provider = _context
    identity_provider = _context_identity
    if change == "capability":
        next_request = UnitPipelineRequest(
            **{**request.__dict__, "capability_definition_hash": HASH_B}
        )
    elif change == "unit_input":
        next_request = UnitPipelineRequest(
            **{**request.__dict__, "unit_input": {"input_sha1": "changed"}}
        )
    elif change == "context":
        context_provider = lambda: _context_with_resolver(HASH_A)
    elif change == "selector":
        identity_provider = lambda manifest: {
            **_context_identity(manifest),
            "selector_definition_hashes": [HASH_A],
        }
    elif change == "prompt":
        pipeline.workers._definitions["planning.apm"] = WorkerDefinition(
            worker_id="planning.apm",
            implementation_hash=HASH_A,
            prompt_hash=HASH_A,
            response_schema=WorkerResponseSchema(
                "planning.apm.response", HASH_A, lambda response: {"apm_markdown": response}
            ),
            repair_policy=WorkerRepairPolicy(0),
            implementation=lambda request, gateway, attempt: events.append("worker") or "# New",
        )

    outcome = pipeline.run(
        next_request,
        context_provider=context_provider,
        context_identity_provider=identity_provider,
        target=workspace,
    )

    assert events[0] == "worker"
    assert outcome.proposal_reused is False
    assert expected_reason in outcome.proposal_reuse_rejection_reasons
    payload = json.loads(
        (store.run_dir(workspace, run["id"]) / "proposals" / "planning.apm.json").read_text(
            encoding="utf-8"
        )
    )
    assert "artifact_currency" not in payload
    assert "freshness" not in payload


def _context_with_resolver(resolver_hash):
    manifest, bundle = _context()
    return (
        ContextManifest(
            capability_id=manifest.capability_id,
            unit_id=manifest.unit_id,
            context_spec_hash=manifest.context_spec_hash,
            resolver_hash=resolver_hash,
            selections=manifest.selections,
            omissions=manifest.omissions,
            truncations=manifest.truncations,
            privacy_decisions=manifest.privacy_decisions,
            supplied_size=manifest.supplied_size,
        ),
        bundle,
    )


def test_recovery_after_manifest_or_interrupted_provider_calls_worker_again():
    workspace = workspaces.create_workspace("Interrupted provider")
    events = []
    pipeline, run = _pipeline(workspace, events)
    calls = {"worker": 0}
    original = pipeline.workers.get("planning.apm")

    def interrupted(request, gateway, attempt):
        calls["worker"] += 1
        if calls["worker"] == 1:
            raise RuntimeError("provider interrupted")
        return "# Recovered APM"

    pipeline.workers._definitions["planning.apm"] = WorkerDefinition(
        worker_id=original.worker_id,
        implementation_hash=original.implementation_hash,
        prompt_hash=original.prompt_hash,
        response_schema=original.response_schema,
        repair_policy=original.repair_policy,
        implementation=interrupted,
    )
    request = _request(workspace)

    with pytest.raises(RuntimeError, match="provider interrupted"):
        pipeline.run(
            request,
            context_provider=_context,
            context_identity_provider=_context_identity,
            target=workspace,
        )
    run_root = store.run_dir(workspace, run["id"])
    assert (run_root / "contexts" / "planning.apm.json").is_file()
    assert not (run_root / "proposals" / "planning.apm.json").exists()

    outcome = pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
    )
    assert outcome.status == "succeeded"
    assert calls["worker"] == 2


def test_recovery_restores_proposed_approval_without_rebilling_worker():
    workspace = workspaces.create_workspace("Approval recovery")
    events = []
    pipeline, _run = _pipeline(workspace, events)
    request = _request(workspace)
    first = pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
        approval_provider=lambda proposal: None,
    )
    events.clear()
    approvals = []

    second = pipeline.run(
        UnitPipelineRequest(
            **{**request.__dict__, "proposal_reference": first.proposal_reference}
        ),
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
        approval_provider=lambda proposal: approvals.append(dict(proposal)) or proposal,
    )

    assert events == ["executor"]
    assert len(approvals) == 1
    assert second.proposal_reused is True


def test_recovery_after_accepted_proposal_skips_approval_and_retries_commit():
    workspace = workspaces.create_workspace("Accepted proposal recovery")
    events = []
    calls = {"executor": 0}

    def executor(request, target):
        calls["executor"] += 1
        if calls["executor"] == 1:
            raise RuntimeError("stopped before commit")
        return _committed_result(request)

    pipeline, _run = _pipeline(workspace, events, executor=executor)
    request = _request(workspace)
    with pytest.raises(RuntimeError, match="stopped before commit"):
        pipeline.run(
            request,
            context_provider=_context,
            context_identity_provider=_context_identity,
            target=workspace,
            approval_provider=lambda proposal: proposal,
        )

    outcome = pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=workspace,
        approval_provider=lambda proposal: pytest.fail("approval must not repeat"),
    )
    assert outcome.status == "succeeded"
    assert outcome.proposal_reused is True
    assert calls["executor"] == 2


def test_recovery_reconciles_commit_without_receipt_before_retrying_executor():
    workspace = workspaces.create_workspace("Commit reconciliation")
    events = []
    target = {"applied": False, "conflict": False}
    calls = {"executor": 0}

    def executor(request, state):
        calls["executor"] += 1
        state["applied"] = True
        raise RuntimeError("crash after commit")

    def reconciler(request, state):
        if state["applied"]:
            return ExecutorReconciliation(
                "already_applied", result=_committed_result(request)
            )
        return ExecutorReconciliation("not_applied")

    pipeline, run = _pipeline(
        workspace, events, executor=executor, reconciler=reconciler
    )
    request = _request(workspace)
    with pytest.raises(RuntimeError, match="crash after commit"):
        pipeline.run(
            request,
            context_provider=_context,
            context_identity_provider=_context_identity,
            target=target,
        )
    assert not (store.run_dir(workspace, run["id"]) / "receipts").exists()

    outcome = pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=target,
    )
    assert calls["executor"] == 1
    assert outcome.executor_reconciled is True
    assert outcome.receipt.reconciled is True


def test_recovery_reuses_valid_receipt_only_when_postcondition_still_holds():
    workspace = workspaces.create_workspace("Receipt recovery")
    events = []
    target = {"applied": False, "conflict": False}
    calls = {"executor": 0}

    def executor(request, state):
        calls["executor"] += 1
        state["applied"] = True
        return _committed_result(request)

    def reconciler(request, state):
        if state["applied"]:
            return ExecutorReconciliation(
                "already_applied", result=_committed_result(request)
            )
        if state["conflict"]:
            return ExecutorReconciliation("conflict", reason="target changed")
        return ExecutorReconciliation("not_applied")

    pipeline, _run = _pipeline(
        workspace, events, executor=executor, reconciler=reconciler
    )
    request = _request(workspace)
    first = pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=target,
    )
    events.clear()
    second = pipeline.run(
        UnitPipelineRequest(
            **{
                **request.__dict__,
                "proposal_reference": first.proposal_reference,
                "receipt_reference": first.receipt_reference,
            }
        ),
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=target,
    )
    assert calls["executor"] == 1
    assert events == []
    assert second.receipt_reused is True

    target["applied"] = False
    target["conflict"] = True
    with pytest.raises(UnitPipelineError, match="target changed"):
        pipeline.run(
            UnitPipelineRequest(
                **{
                    **request.__dict__,
                    "proposal_reference": first.proposal_reference,
                    "receipt_reference": first.receipt_reference,
                }
            ),
            context_provider=_context,
            context_identity_provider=_context_identity,
            target=target,
        )
    assert calls["executor"] == 1


def test_corrupt_proposal_regenerates_and_corrupt_receipt_reconciles():
    workspace = workspaces.create_workspace("Sidecar validation")
    events = []
    target = {"applied": False}

    def executor(request, state):
        state["applied"] = True
        events.append("executor")
        return _committed_result(request)

    def reconciler(request, state):
        return (
            ExecutorReconciliation(
                "already_applied", result=_committed_result(request)
            )
            if state["applied"]
            else ExecutorReconciliation("not_applied")
        )

    pipeline, run = _pipeline(
        workspace, events, executor=executor, reconciler=reconciler
    )
    request = _request(workspace)
    first = pipeline.run(
        request,
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=target,
    )
    run_root = store.run_dir(workspace, run["id"])
    proposal_path = run_root / "proposals" / "planning.apm.json"
    receipt_path = run_root / "receipts" / "planning.apm.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal["proposal"]["apm_markdown"] = "tampered"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["receipt_hash"] = HASH_A
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    events.clear()

    outcome = pipeline.run(
        UnitPipelineRequest(
            **{
                **request.__dict__,
                "proposal_reference": first.proposal_reference,
                "receipt_reference": first.receipt_reference,
            }
        ),
        context_provider=_context,
        context_identity_provider=_context_identity,
        target=target,
    )
    assert events == ["worker"]
    assert "proposal_sidecar_invalid" in outcome.proposal_reuse_rejection_reasons
    assert outcome.executor_reconciled is True


def test_unit_pipeline_module_has_no_scheduler_or_audit_domain_dependency():
    source = (
        Path(__file__).parents[1] / "app" / "agent" / "runtime" / "unit_pipeline.py"
    ).read_text(encoding="utf-8")
    assert "workflow_runner" not in source
    assert "action_runner" not in source
    assert "audit_capabilities" not in source
    assert "audit_workers" not in source
