"""Durable architectural contracts for the active agent engines.

These tests intentionally exercise public registries and runtime outcomes. They
replace migration-era checks for deleted modules, private helper names, prompt
strings, and source-layout branches.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agent import capabilities, runner, store, workflow_dispatch
from app.agent.context import PRESETS
from app.agent.runtime import CapabilityExecution, CapabilityExecutionRegistry
from app.agent.workflow import CapabilityRegistry
from app.agent.workflows import audit as audit_workflow
from app.workspaces import WorkspaceError


def _execution_registry(ids: list[str]) -> CapabilityExecutionRegistry:
    registry = CapabilityExecutionRegistry()
    for capability_id in ids:
        registry.register(
            CapabilityExecution(
                capability_id=capability_id,
                implementation_hash="sha256:" + "0" * 64,
                deterministic_executor=lambda *_args, **_kwargs: None,
            )
        )
    return registry


def test_declared_engines_are_the_only_writable_run_engines(workspace_with_data):
    assert set(store.RUN_ENGINES) == {
        store.WORKFLOW_ENGINE,
        store.ACTION_ENGINE,
        store.INTAKE_ENGINE,
    }
    with pytest.raises(WorkspaceError, match="run kind"):
        store.new_run(workspace_with_data, "auto", None, kind="analysis")


@pytest.mark.parametrize("engine", [None, "", "analysis", "doc_test", "v2"])
def test_unsupported_or_missing_engine_fails_closed(workspace_with_data, engine):
    run = store.new_run(workspace_with_data, "auto", None, kind="intake")
    if engine is None:
        run.pop("engine")
    else:
        run["engine"] = engine
    store.save_run(workspace_with_data, run)

    runner._execute(
        workspace_with_data,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    failed = store.load_run(workspace_with_data, run["id"])
    assert failed["status"] == "failed"
    assert "engine" in failed["error"]


def test_workflow_dispatch_requires_a_persisted_definition(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
    )
    run["engine"] = store.WORKFLOW_ENGINE
    run["workflow"] = {"requested_outcomes": ["planning.apm_ready"], "stages": []}
    store.save_run(workspace_with_data, run)

    with pytest.raises(WorkspaceError, match="missing"):
        workflow_dispatch.build_workflow_runner(
            workspace_with_data,
            store.load_run(workspace_with_data, run["id"]),
            runner.RunHandle(workspace_with_data.id, run["id"]),
        )


def test_audit_registry_is_the_authoritative_graph_partition():
    grouped = capabilities.grouped_capability_ids()
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == set(audit_workflow.DEPENDENCIES)
    assert {
        capability.id: capability.depends_on
        for capability in capabilities.REGISTRY.all()
    } == {
        capability_id: audit_workflow.dependencies(capability_id)
        for capability_id in audit_workflow.DEPENDENCIES
    }


def test_composition_rejects_missing_bindings_and_unknown_context_presets():
    ids = list(audit_workflow.DEPENDENCIES)
    with pytest.raises(ValueError, match="audit.verified"):
        capabilities.validate_audit_composition(
            capabilities.REGISTRY,
            executions=_execution_registry(
                [capability_id for capability_id in ids if capability_id != "audit.verified"]
            ),
        )

    altered = [
        dataclasses.replace(capability, context="does.not.exist")
        if capability.id == "planning.apm_ready"
        else capability
        for capability in capabilities.REGISTRY.all()
    ]
    registry = CapabilityRegistry()
    for capability in altered:
        registry.register(capability)
    with pytest.raises(capabilities.AuditCompositionError, match="does.not.exist"):
        capabilities.validate_audit_composition(registry)


def test_every_declared_context_preset_compiles():
    preset_ids = {
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
    assert preset_ids
    assert all(PRESETS.compile(preset_id) is not None for preset_id in preset_ids)
