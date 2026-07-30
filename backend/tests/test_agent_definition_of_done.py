"""Phase 13 gate: the migration's definition of done (`P13.4`).

One test per bullet of §13 of
[agent-architecture-implementation-plan.md](../../docs/agent-architecture-implementation-plan.md).
Where an earlier phase already proved a bullet in depth, the assertion here is
the durable one-line statement of it, so the whole definition can be re-checked
in one run rather than reconstructed from thirteen phase gates.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

import app.agent as agent_package
from app.agent import capabilities, doc_tests_execution, ledger, prompts, runner, store
from app.agent import documents_execution, workflow, workflow_dispatch
from app.agent.action_runner import ActionRunner
from app.agent.base import BaseRunner
from app.agent.context import PRESETS
from app.agent.intake_runner import IntakeRunner
from app.agent.runtime import DefaultRunRuntime, RunRuntime, WorkflowRunner
from app.agent.workflows import analysis as analysis_workflow
from app.agent.workflows import audit as audit_workflow
from app.workspaces import WorkspaceError


AGENT_ROOT = Path(agent_package.__file__).parent


# --------------------------------------------------------------------------- #
# Schedulers
# --------------------------------------------------------------------------- #
def test_only_two_general_schedulers_plus_one_justified_protocol_runner():
    engines = {
        store.WORKFLOW_ENGINE: WorkflowRunner,
        store.ACTION_ENGINE: ActionRunner,
        store.INTAKE_ENGINE: IntakeRunner,
    }
    assert set(engines) == set(store.RUN_ENGINES)
    # Intake is the sole retained protocol runner; the other two engines are
    # the general-purpose workflow and action schedulers.
    assert set(engines) == {"workflow", "action", "intake"}


def test_schedulers_share_runtime_services_by_composition(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
    )
    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    scheduler = ActionRunner(workspace_with_data, run, handle)

    assert isinstance(scheduler.runtime, DefaultRunRuntime)
    assert isinstance(scheduler.runtime, RunRuntime)
    # WorkflowRunner takes every dependency by composition and inherits nothing.
    assert WorkflowRunner.__mro__[1:] == (object,)
    assert not issubclass(WorkflowRunner, (ActionRunner, BaseRunner))


def test_the_workflow_scheduler_is_domain_neutral():
    source = inspect.getsource(WorkflowRunner)
    for domain_word in ("apm", "rcm", "planned_test", "finding", "report", "document"):
        assert f"def stage_{domain_word}" not in source
        assert f"def _{domain_word}" not in source
    # No method names an audit artifact; the scheduler only knows capabilities,
    # units, and the bindings it was composed with.
    assert not [name for name in dir(WorkflowRunner) if "audit" in name]


# --------------------------------------------------------------------------- #
# Declarations
# --------------------------------------------------------------------------- #
def test_the_audit_lifecycle_is_declared_exactly_once():
    declared = {
        capability.id: capability.depends_on
        for capability in capabilities.REGISTRY.all()
    }
    assert declared == {
        key: tuple(value) for key, value in audit_workflow.DEPENDENCIES.items()
    }
    # The grouped modules derive their edges; they never restate them.
    for module in (AGENT_ROOT / "capabilities").glob("*.py"):
        assert "depends_on=(" not in module.read_text(encoding="utf-8"), module.name


def test_the_action_ledger_holds_no_audit_lifecycle_policy():
    assert not hasattr(ledger, "AUDIT_LIFECYCLE_STAGES")
    assert not hasattr(ledger, "enforce_audit_lifecycle")
    source = Path(ledger.__file__).read_text(encoding="utf-8").casefold()
    for policy_word in ("apm", "rcm", "working paper", "audit lifecycle"):
        assert policy_word not in source


def test_every_declared_context_preset_is_registered():
    declared = {
        str(preset_id)
        for registry in capabilities.REGISTRY_BY_WORKFLOW.values()
        for capability in registry.all()
        for preset_id in (
            capability.context.values()
            if isinstance(capability.context, dict)
            else (capability.context,)
        )
        if preset_id
    }
    assert declared
    for preset in declared:
        assert PRESETS.compile(preset) is not None


def test_generic_data_analysis_is_a_declared_workflow_ending_in_execution():
    assert analysis_workflow.FULL_ANALYSIS_OUTCOMES == ["analysis.executed"]
    assert "analysis.executed" in capabilities.ANALYSIS_REGISTRY.closure(
        analysis_workflow.FULL_ANALYSIS_OUTCOMES
    )


def test_document_analysis_and_document_tests_have_one_implementation_each():
    """The audit graph reaches both through the standalone graphs' own code."""
    import app.agent.audit_execution as audit_execution

    assert (
        audit_execution.DocumentWorkflowExecution
        is documents_execution.DocumentWorkflowExecution
    )
    assert (
        audit_execution.bind_document_test_unit
        is doc_tests_execution.bind_document_test_unit
    )
    # A Document Test worklist expands in exactly one place; the audit
    # fieldwork slice imports that expander rather than restating it.
    from app.agent.capabilities import doc_tests as doc_test_capabilities
    from app.agent.capabilities import fieldwork as fieldwork_capabilities

    assert (
        fieldwork_capabilities.document_test_units
        is doc_test_capabilities.document_test_units
    )


# --------------------------------------------------------------------------- #
# No superseded implementation or compatibility layer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "retired",
    [
        "app.agent.command_runner",
        "app.agent.workflow_runner",
        "app.agent.summary",
        "app.agent.audit_capabilities",
        "app.agent.audit_workers",
        "app.agent.document_analysis_runner",
        "app.agent.doc_test_runner",
    ],
)
def test_no_superseded_module_remains(retired):
    assert importlib.util.find_spec(retired) is None


def test_no_compatibility_package_or_alias_exists():
    offenders = [
        path.name
        for path in AGENT_ROOT.rglob("*")
        if "compat" in path.name.casefold()
    ]
    assert offenders == []
    assert not hasattr(agent_package, "CommandRunner")
    assert not hasattr(runner, "_Runner")


def test_execution_cleanup_has_one_shared_support_surface():
    import app.agent.analysis_execution as analysis_execution
    import app.agent.audit_execution as audit_execution

    execution_modules = (
        analysis_execution,
        audit_execution,
        documents_execution,
        doc_tests_execution,
    )
    for module in execution_modules:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "def _sha256_json" not in source
        assert "def _capability_definition_hash" not in source
        assert "def _refresh_workspace" not in source
        assert "def workflow_scope" not in source
        assert "def _resolve_context" not in source

    assert callable(workflow.canonical_sha256)
    assert callable(workflow.capability_definition_hash)
    base_source = Path(inspect.getsourcefile(BaseRunner) or "").read_text(
        encoding="utf-8"
    )
    assert "_model_context" not in base_source
    assert "agent:document_selection" not in base_source
    assert not hasattr(BaseRunner, "model_adapter")
    assert not hasattr(prompts, "parse_markdown_response")


def test_action_run_shape_and_statuses_use_current_names(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Rename an artifact"}
    )

    assert "target_adjustments" in run
    assert "lifecycle_adjustments" not in run
    assert hasattr(ledger, "project_action_plan")
    assert not hasattr(ledger, "project_legacy_plan")
    assert "discovering" not in store.ACTIVE_STATUSES
    assert "planning" not in store.ACTIVE_STATUSES
    assert "summarizing" not in store.ACTIVE_STATUSES


# --------------------------------------------------------------------------- #
# Fail-closed dispatch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine", [None, "", "analysis", "doc_test", "v2"])
def test_a_record_without_a_supported_engine_fails_closed(workspace_with_data, engine):
    run = store.new_run(workspace_with_data, "auto", None, kind="intake")
    if engine is None:
        run.pop("engine")
    else:
        run["engine"] = engine
    store.save_run(workspace_with_data, run)

    runner._execute(
        workspace_with_data.id,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    failed = store.load_run(workspace_with_data, run["id"])
    assert failed["status"] == "failed"
    assert "engine" in failed["error"]


def test_a_workflow_record_without_a_definition_fails_closed(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
    )
    run["schema_version"] = 3
    run["engine"] = store.WORKFLOW_ENGINE
    run["workflow"] = {"requested_outcomes": ["planning.apm_ready"], "stages": []}
    store.save_run(workspace_with_data, run)

    loaded = store.load_run(workspace_with_data, run["id"])
    assert "definition" not in loaded["workflow"], "no default may be inferred"
    with pytest.raises(WorkspaceError, match="missing"):
        workflow_dispatch.build_workflow_runner(
            workspace_with_data,
            loaded,
            runner.RunHandle(workspace_with_data.id, run["id"]),
        )
