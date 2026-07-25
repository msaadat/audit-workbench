"""Phase 7 gate: the audit lifecycle has one authoritative home (P7.4).

The phase objective is that the audit dependency graph appears only in
``workflows/audit.py``, that every capability's declaration lives in a grouped
``capabilities`` module, that every capability executes on a native scheduler
path, and that the generic runtime imports no audit module. No capability uses
the transitional batch adapter any more, and this gate proves that binding kind
is gone rather than merely unused.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import app.agent as agent_package
from app.agent import audit_execution, capabilities
from app.agent.audit_execution import build_audit_workflow_runner
from app.agent.executors import EXECUTORS
from app.agent.workers import WORKERS
from app.agent.workflows import audit as audit_workflow


def test_no_capability_is_executed_by_a_transitional_batch_handler():
    source = inspect.getsource(audit_execution)

    # The whitelist of deferred capabilities, its handler map, and the
    # batch-binding branch are deleted, not merely empty.
    assert not hasattr(audit_execution, "_AUDIT_HANDLER_NAMES")
    assert "transitional_batch_executor" not in source
    assert not hasattr(audit_execution.AuditWorkflowExecution, "_planning_basis")
    assert not hasattr(audit_execution.AuditWorkflowExecution, "_executions")


def test_every_capability_has_exactly_one_execution_binding(workspace_with_data):
    from app.agent import runner, store

    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "goal_template", "text": "Do the full audit"},
    )
    scheduler = build_audit_workflow_runner(
        workspace_with_data,
        run,
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    declared = [capability.id for capability in capabilities.REGISTRY.all()]
    kinds: dict[str, str] = {}
    for capability_id in declared:
        execution = scheduler.executions.get(capability_id)
        assert execution.pipeline_backed or execution.deterministic_backed
        kinds[capability_id] = (
            "pipeline" if execution.pipeline_backed else "deterministic"
        )

    assert {cid for cid, kind in kinds.items() if kind == "pipeline"} == {
        # Phase 9: the scoped document capabilities planning depends on are bound
        # through the same document implementation the standalone workflow uses.
        "documents.analysis_chunks_ready",
        "documents.analysis_generated",
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "planning.planned_tests_ready",
        "fieldwork.definitions_ready",
        # Mixed units: the document Q&A kind is model-backed and the rest are
        # deterministic, so the capability binds each unit through one binder.
        "fieldwork.executed",
        "findings.drafted",
    }
    assert {cid for cid, kind in kinds.items() if kind == "deterministic"} == {
        "documents.text_ready",
        "results.rolled_up",
        "working_papers.generated",
        "dashboard.curated",
        "report.working_draft",
        "audit.verified",
    }


def test_every_declared_worker_and_executor_key_is_registered():
    workers = {definition.worker_id for definition in WORKERS.all()}
    executors = {definition.executor_id for definition in EXECUTORS.all()}

    assert {
        "planning.apm",
        "planning.context",
        "planning.rcm",
        "planning.planned_tests",
        "fieldwork.data_test_spec",
        "fieldwork.document_test_spec",
        "fieldwork.document_qa",
        "reporting.finding",
    } <= workers
    assert {
        "planning.apm",
        "planning.context",
        "planning.rcm",
        "planning.planned_tests",
        "fieldwork.data_test",
        "fieldwork.document_test",
        "fieldwork.document_qa",
        "reporting.finding",
    } <= executors


def test_the_audit_dependency_graph_is_declared_in_exactly_one_module():
    agent_root = pathlib.Path(agent_package.__file__).parent
    authoritative = agent_root / "workflows" / "audit.py"
    declaring: list[str] = []

    for path in sorted(agent_root.rglob("*.py")):
        if path == authoritative:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            if not names & {"DEPENDENCIES", "FULL_AUDIT_OUTCOMES", "TEMPLATE_OUTCOMES"}:
                continue
            # A re-export of the authoritative object is not a second graph.
            if isinstance(node.value, ast.Attribute):
                continue
            declaring.append(f"{path.relative_to(agent_root)}:{sorted(names)}")

    assert declaring == []
    assert set(audit_workflow.DEPENDENCIES) == set(
        capabilities.grouped_capability_ids()
    )


def _required_interface_fields(source: str, name: str) -> set[str]:
    """Field names an `export interface <name>` declares as non-optional."""

    body = re.search(
        rf"^export interface {name} \{{\n(.*?)^\}}", source, re.S | re.M
    )
    assert body is not None, f"interface {name} not found in frontend/src/types.ts"
    return {
        match.group(1)
        for line in body.group(1).splitlines()
        if (match := re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)(\??):", line))
        and match.group(2) != "?"
    }


def test_the_run_projection_still_carries_every_field_the_frontend_requires():
    """P7.4 frontend gate: moving the declarations into grouped modules must not
    drop a key the UI types declare as required. Extra backend keys are fine —
    TypeScript reads the projection structurally — so this checks one direction."""

    from app import workspaces
    from app.agent import store
    from app.agent.routing import initialize_known_workflow

    types_ts = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "types.ts"
    ).read_text(encoding="utf-8")

    workspace = workspaces.create_workspace("Phase 7 projection gate")
    run = store.new_command_run(
        workspace,
        "auto",
        {"source": "goal_template", "text": "Do the full audit"},
    )
    assert initialize_known_workflow(workspace, run) is True
    workflow = store.load_run(workspace, run["id"])["workflow"]

    assert _required_interface_fields(types_ts, "AgentWorkflow") <= set(workflow)
    stage_fields = _required_interface_fields(types_ts, "WorkflowStage")
    unit_fields = _required_interface_fields(types_ts, "WorkflowUnit")
    assert workflow["stages"], "the full audit must stage at least one capability"
    for stage in workflow["stages"]:
        assert stage_fields <= set(stage)
        for unit in stage["units"]:
            assert unit_fields <= set(unit)

    # The stage ids the UI keys on come from the grouped declarations.
    assert {stage["id"] for stage in workflow["stages"]} == {
        capabilities.REGISTRY.get(stage["capability"]).stage_id
        for stage in workflow["stages"]
    }


def test_the_full_audit_closure_covers_every_declared_capability():
    closure = capabilities.REGISTRY.closure(list(audit_workflow.FULL_AUDIT_OUTCOMES))

    assert set(closure) == set(audit_workflow.DEPENDENCIES)
    # Topological: a capability never precedes one of its dependencies.
    for position, capability_id in enumerate(closure):
        for dependency in audit_workflow.dependencies(capability_id):
            assert closure.index(dependency) < position
