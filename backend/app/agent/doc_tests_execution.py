"""Document-test composition of the domain-neutral capability scheduler.

Two things live here.

:func:`bind_document_test_unit` is the *one* place a Document Test unit is bound
to the execution it needs — deterministic local comparison, a bounded Q&A model
turn through the registered worker, or an auditor-review settlement. Both graphs
that schedule document tests use it: the standalone ``doc_tests_workflow_v1``
composition below, and the audit graph's ``fieldwork.executed`` binder, which
keeps only its datatest branch and its own task identity. That is what makes the
`P10.5` migration a deletion rather than a second implementation.

:class:`DocTestWorkflowExecution` is the standalone composition itself: the
document-test-shaped glue the scheduler must not know about — stage titles,
scope, the model budget, the definition and disposition settlements, and the
completion projection.

``DocTestRunner`` was deleted here. The reasoning, measured against the workflow
scheduler's own contract, is recorded in
``docs/agent-protocol-runner-decisions.md``.
"""

from __future__ import annotations

import threading

from .. import doc_tests
from ..workspace_transactions import parent_hashes
from ..workspaces import Workspace, WorkspaceConflict
from . import narration, workflow
from .base import BaseRunner
from .capabilities import DOC_TESTS_REGISTRY
from .capabilities.doc_tests import (
    DocTestScope,
    resolve_doc_test_scope,
    scoped_tests,
    unexecuted_items,
)
from .context import ContextResolver, document_qa_scope
from .executors import EXECUTORS
from .executors.fieldwork import (
    DOCUMENT_QA_DISPOSITION_REQUIRED,
    DOCUMENT_REVIEW_REQUIRED,
    DocumentQaExecutorTarget,
    document_qa_answer_ref,
    document_test_ref,
    run_document_test,
)
from .execution_support import refresh_workspace, resolve_context, workflow_scope
from .runtime import (
    BoundUnitPipeline,
    CapabilityExecution,
    CapabilityExecutionRegistry,
    DeterministicUnitResult,
    FinishProjection,
    RunRuntime,
    UnitPipeline,
    UnitPipelineRequest,
    UnitSidecarStore,
    WorkflowRunner,
)
from .workers import WORKERS

DEFINITION_REVIEW_REQUIRED = "document_test_definition_needs_auditor_attention"
DISPOSITION_REQUIRED = "document_test_results_await_auditor_disposition"


def unit_ref(unit: dict, prefix: str) -> str:
    """The first parent reference of a given kind, whatever else precedes it."""

    return next(ref for ref in unit["parent_refs"] if ref.startswith(prefix))


# --------------------------------------------------------------------------- #
# The one Document Test unit binding, shared by both graphs
# --------------------------------------------------------------------------- #
def bind_document_qa(
    adapter,
    capability: workflow.Capability,
    unit: dict,
    *,
    task: dict,
) -> BoundUnitPipeline:
    """Bind one item/document Q&A unit to the shared ``UnitPipeline``.

    The answer comes from the registered ``fieldwork.document_qa`` worker through
    the injected gateway and the declared page context, and the registered
    executor owns the guarded merge into the Document Test. In permission mode a
    cited answer remains an auditor checkpoint. In auto mode, the worker's
    complete cited outcome is applied and a conclusive unit settles successfully.
    """
    test_id = unit_ref(unit, "doctest:").split(":", 1)[1]
    item_id = unit_ref(unit, "docitem:").split(":", 1)[1]
    document_id = unit_ref(unit, "document:").split(":", 1)[1]
    expected_test = parent_hashes(adapter.ws, [f"doctest:{test_id}"])
    target = DocumentQaExecutorTarget(
        adapter.ws, adapter.run["id"], test_id, item_id, document_id
    )

    def context_provider():
        return resolve_context(
            adapter,
            adapter.context_resolver,
            capability,
            unit,
            document_qa_scope(adapter.ws, test_id, item_id, document_id),
        )

    def on_committed(_stage, _unit, _outcome) -> DeterministicUnitResult:
        adapter.ws = target.workspace
        adapter.emit(
            "workspace_changed",
            {"kind": "doctest", "id": test_id, "action": "qa_answered"},
        )
        if adapter.run["mode"] == "auto":
            doc_tests.auto_dispose_llm_assessment(
                adapter.ws, test_id, item_id
            )
            target.workspace = adapter.ws
            adapter.emit(
                "workspace_changed",
                {"kind": "doctest", "id": test_id, "action": "auto_disposed"},
            )
            # A complete worker assessment is the auto-mode disposition for
            # this pair, including ``needs_manual_check``.  The latter is a
            # durable outcome (and leaves the item in manual_review), not an
            # instruction to wait for a human.  For multi-document items the
            # last pair applies the aggregate disposition; earlier pairs still
            # settle successfully so the stage does not retain stale open units.
            return DeterministicUnitResult(
                "succeeded",
                (document_qa_answer_ref(test_id, item_id, document_id),),
            )
        return DeterministicUnitResult(
            "awaiting_confirmation",
            (document_qa_answer_ref(test_id, item_id, document_id),),
            DOCUMENT_QA_DISPOSITION_REQUIRED,
        )

    return BoundUnitPipeline(
        request=UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id="fieldwork.document_qa",
            executor_id="fieldwork.document_qa",
            unit_input={
                "kind": unit.get("kind"),
                "input_sha1": unit.get("input_sha1"),
                "parent_refs": list(unit.get("parent_refs") or []),
            },
            activity={
                "artifact_refs": list(unit.get("parent_refs") or []),
                "document_ids": [document_id],
                "task_id": task["id"],
            },
            expected_revision=adapter.ws.revision,
            expected_parents=expected_test,
            capability_definition_hash=workflow.capability_definition_hash(capability),
            # The assessment is not an artifact the auditor pre-approves, so no
            # approval batch is requested even in permission mode.
            approval_kind=None,
            proposal_reference=unit.get("proposal_sidecar"),
            receipt_reference=unit.get("receipt_sidecar"),
        ),
        context_provider=context_provider,
        context_identity_provider=lambda manifest: (
            adapter.context_resolver.execution_identity(capability, manifest)
        ),
        target=target,
        approval_provider=None,
        # Execution fans out one unit per question and document, so this
        # capability's readiness only holds once every unit has settled.
        readiness_provider=None,
        on_committed=on_committed,
    )


def bind_document_test_unit(
    adapter,
    capability: workflow.Capability,
    unit: dict,
    *,
    task: dict,
) -> BoundUnitPipeline | DeterministicUnitResult:
    """Bind one Document Test unit, whichever graph expanded it.

    The status vocabulary carries audit meaning, not just success/failure:
    ``awaiting_confirmation`` means the agent produced something only an auditor
    may disposition, and ``blocked`` means evidence is missing — which registers
    the unit against its evidence request so uploading the document can unblock it
    later.

    ``adapter`` supplies the live run surface (``ws``, ``run``, ``emit``,
    ``_resolve_context``, ``context_resolver``); this function owns the decision.
    """
    if unit["kind"] == "document_test_review":
        return DeterministicUnitResult(
            "awaiting_confirmation",
            (unit_ref(unit, "doctest:"),),
            DOCUMENT_REVIEW_REQUIRED,
        )
    if unit["kind"] in {"document_qa_execution", "document_llm_execution"}:
        return bind_document_qa(adapter, capability, unit, task=task)
    test_id = unit_ref(unit, "doctest:").split(":", 1)[1]
    try:
        outcome = run_document_test(
            adapter.ws, test_id, unit_id=unit["id"], run_id=adapter.run["id"]
        )
    except WorkspaceConflict as error:
        return DeterministicUnitResult("conflict", error=str(error))
    if outcome.executed:
        adapter.emit(
            "workspace_changed",
            {"kind": "doctest", "id": test_id, "action": "executed"},
        )
    return DeterministicUnitResult(
        outcome.status, (outcome.artifact_ref,), outcome.error
    )


# --------------------------------------------------------------------------- #
# Standalone composition
# --------------------------------------------------------------------------- #
class DocTestWorkflowExecution(BaseRunner):
    """Per-unit document-test execution bindings and projections."""

    stage_titles = {
        "doc_test_definitions": "Document test definitions",
        "doc_test_execution": "Document test execution",
        "doc_test_disposition": "Document test disposition",
    }

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        state_lock: threading.RLock | None = None,
        context_resolver: ContextResolver | None = None,
    ):
        super().__init__(workspace, run, handle, runtime=runtime, state_lock=state_lock)
        self.context_resolver = context_resolver or ContextResolver()
        self.sidecars = UnitSidecarStore(workspace, run["id"])
        self.unit_pipeline: UnitPipeline | None = None
        self.scheduler: WorkflowRunner | None = None

    # ------------------------------------------------------------- scheduling
    def _refresh_dynamic_limits(self) -> None:
        """Size the model budget from the Q&A pairs actually in scope.

        Only the Q&A unit kind calls the model, once per unanswered
        item/document pair, so the budget follows the resolved scope rather than
        the whole worklist library.
        """
        qa_pairs = sum(
            len(item.get("document_ids") or [])
            for test in scoped_tests(self.ws, workflow_scope(self.run))
            if test.get("kind") == "qa"
            for item in test.get("items") or []
        )
        calculated = 4 + 2 * qa_pairs
        self.update_limits(
            {
                "max_model_turns": calculated,
                "max_estimated_prompt_tokens": calculated * 10_000,
                "max_completion_tokens": calculated * 4_000,
            },
            grow_only=True,
        )

    def scope(self) -> DocTestScope:
        return resolve_doc_test_scope(self.ws, workflow_scope(self.run))

    # -------------------------------------------- doc_tests.definitions_ready
    def _bind_definition(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """A definition that cannot run is reported, never invented.

        This workflow schedules Document Tests the auditor (or the audit graph's
        ``fieldwork.definitions_ready``) already authored. A worklist with no
        attached evidence, no comparison checks, or no question is something only
        its author can fix, so the unit settles for auditor attention with the
        deterministic issues recorded.
        """
        self.ws = subject
        test_id = unit_ref(unit, "doctest:").split(":", 1)[1]
        test = doc_tests.load_test(self.ws, test_id)
        issues = doc_tests.execution_issues(test)
        task = self.add_task(
            "doc_test_definitions",
            "workflow:doc_test_definitions",
            "Document test definitions",
        )
        self.task_status(task, "running")
        self.task_detail(
            task,
            f"{test.get('title') or test_id}: " + "; ".join(issues),
        )
        self.task_status(task, "completed")
        return DeterministicUnitResult(
            "awaiting_confirmation",
            (document_test_ref(test_id),),
            DEFINITION_REVIEW_REQUIRED,
        )

    # ---------------------------------------------------- doc_tests.executed
    def _bind_execution(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        self.ws = subject
        # The existing execution services combine compute and mutation and check
        # the revision they were handed, so each unit runs against a freshly
        # loaded workspace rather than the subject the stage started with.
        refresh_workspace(self)
        task = self.add_task(
            "doc_test_execution",
            "workflow:doc_test_execution",
            "Document test execution",
        )
        return bind_document_test_unit(self, capability, unit, task=task)

    # ----------------------------------------------- doc_tests.dispositioned
    def _bind_disposition(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Return any disposition still requiring authoritative auditor input."""

        self.ws = subject
        test_id = unit_ref(unit, "doctest:").split(":", 1)[1]
        test = doc_tests.load_test(self.ws, test_id)
        task = self.add_task(
            "doc_test_disposition",
            "workflow:doc_test_disposition",
            "Document test disposition",
        )
        self.task_status(task, "running")
        rollup = doc_tests.result_rollup(test)
        self.task_detail(
            task,
            f"{test.get('title') or test_id}: {rollup['items']} item(s) awaiting "
            "auditor disposition.",
        )
        self.task_status(task, "completed")
        return DeterministicUnitResult(
            "awaiting_confirmation",
            (document_test_ref(test_id),),
            DISPOSITION_REQUIRED,
        )

    # ---------------------------------------------------------------- finish
    def _finish_projection(
        self,
        subject: Workspace,
        _workflow_state: dict,
        stages: tuple[dict, ...],
    ) -> FinishProjection:
        """Close the run on real test outcomes, not on unit bookkeeping."""

        self.ws = subject
        tests = scoped_tests(subject, workflow_scope(self.run))
        units = [unit for stage in stages for unit in stage.get("units") or []]
        failed = sum(unit.get("status") in {"failed", "conflict"} for unit in units)
        open_units = sum(
            unit.get("status")
            in {"blocked", "awaiting_input", "awaiting_confirmation"}
            for unit in units
        )
        rollups = [doc_tests.result_rollup(test) for test in tests]
        totals = {
            key: sum(rollup[key] for rollup in rollups)
            for key in (
                "items",
                "matched",
                "mismatched",
                "confirmed",
                "exceptions",
                "manual_review",
            )
        }
        unexecuted = sum(unexecuted_items(test) for test in tests)
        next_outcomes = [
            str(stage["capability"])
            for stage in stages
            if stage.get("status") in {"failed", "blocked", "review_required"}
        ]
        self.run["workflow"]["workspace_revision"] = subject.revision
        self.run["doc_tests"] = {
            "test_ids": [str(test["id"]) for test in tests],
            "rollup": totals,
            "unexecuted_items": unexecuted,
        }
        if failed:
            terminal = "failed"
            errors = [
                str(unit.get("error"))
                for unit in units
                if unit.get("status") in {"failed", "conflict"} and unit.get("error")
            ]
            self.run["error"] = (
                errors[0] if errors else "One or more document-test units failed."
            )
        else:
            terminal = "completed" if not open_units else "completed_with_open_items"
        summary = narration.summary_markdown(
            "Document tests",
            [
                ("Tests in scope", len(tests)),
                ("Items", totals["items"]),
                ("Deterministic matches", totals["matched"]),
                ("Mismatch or missing", totals["mismatched"]),
                ("Confirmed", totals["confirmed"]),
                ("Exceptions", totals["exceptions"]),
                ("Manual review", totals["manual_review"]),
                ("Unchecked items", unexecuted),
            ],
            (
                "Auto mode applies conclusive cited worker outcomes; unresolved "
                "results remain subject to auditor disposition."
            ),
        )
        return FinishProjection(
            next_outcomes=tuple(dict.fromkeys(next_outcomes)),
            summary_markdown=summary,
            terminal_status=terminal,
        )


# Every edge in this graph is partial. One unusable definition, or one item that
# could not be checked, must not stop the tests the run can still execute: each
# later capability re-expands against what the earlier one actually produced.
_PARTIAL_DEPENDENCIES = {
    "doc_tests.executed": {"doc_tests.definitions_ready"},
    "doc_tests.dispositioned": {"doc_tests.executed"},
}

_PIPELINE_BINDERS = {
    "doc_tests.executed": (
        "_bind_execution",
        {
            "worker": "fieldwork.document_qa",
            "executor": "fieldwork.document_qa",
            "deterministic": (
                "fieldwork.document_test_run|fieldwork.document_test_review"
            ),
        },
    ),
}
_DETERMINISTIC_BINDERS = {
    "doc_tests.definitions_ready": (
        "_bind_definition",
        {"deterministic": "doc_tests.definition_review"},
    ),
    "doc_tests.dispositioned": (
        "_bind_disposition",
        {"deterministic": "doc_tests.disposition"},
    ),
}


def build_doc_test_capability_executions(
    adapter: DocTestWorkflowExecution,
    capabilities,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityExecutionRegistry:
    """Register one execution binding per supplied document-test capability."""

    executions = executions or CapabilityExecutionRegistry()
    for capability in capabilities:
        pipeline_binding = _PIPELINE_BINDERS.get(capability.id)
        if pipeline_binding is not None:
            binder, identity = pipeline_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=workflow.canonical_sha256(
                        {"capability": capability.id, **identity}
                    ),
                    pipeline_binder=getattr(adapter, binder),
                )
            )
            continue
        binder, identity = _DETERMINISTIC_BINDERS[capability.id]
        executions.register(
            CapabilityExecution(
                capability_id=capability.id,
                implementation_hash=workflow.canonical_sha256(
                    {"capability": capability.id, **identity}
                ),
                deterministic_executor=getattr(adapter, binder),
            )
        )
    return executions


def build_doc_tests_workflow_runner(
    workspace: Workspace,
    run: dict,
    handle,
    *,
    runtime: RunRuntime | None = None,
    context_resolver: ContextResolver | None = None,
) -> WorkflowRunner:
    """Compose the domain-neutral scheduler with the document-test bindings."""

    adapter = DocTestWorkflowExecution(
        workspace,
        run,
        handle,
        runtime=runtime,
        context_resolver=context_resolver,
    )
    unit_pipeline = UnitPipeline(
        runtime=adapter.runtime,
        gateway=adapter.model_gateway,
        workers=WORKERS,
        executors=EXECUTORS,
        sidecars=adapter.sidecars,
    )
    adapter.unit_pipeline = unit_pipeline
    executions = build_doc_test_capability_executions(
        adapter, DOC_TESTS_REGISTRY.all()
    )

    def dependency_policy(
        capability_id: str,
        dependency_id: str,
        _dependency_status: str,
    ) -> bool:
        return dependency_id in _PARTIAL_DEPENDENCIES.get(capability_id, set())

    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=adapter.runtime,
        registry=DOC_TESTS_REGISTRY,
        executions=executions,
        unit_pipeline=unit_pipeline,
        refresh_subject=lambda: refresh_workspace(adapter),
        refresh_limits=lambda _subject: adapter._refresh_dynamic_limits(),
        dependency_policy=dependency_policy,
        finish_evaluator=adapter._finish_projection,
    )
    adapter.scheduler = scheduler
    scheduler.execution_adapter = adapter
    return scheduler


__all__ = [
    "DEFINITION_REVIEW_REQUIRED",
    "DISPOSITION_REQUIRED",
    "DocTestWorkflowExecution",
    "bind_document_qa",
    "bind_document_test_unit",
    "build_doc_test_capability_executions",
    "build_doc_tests_workflow_runner",
    "unit_ref",
]
