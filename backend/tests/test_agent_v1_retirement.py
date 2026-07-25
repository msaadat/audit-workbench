"""Phase 12 gate: the fixed-stage v1 analysis pipeline is gone (`P12.3`).

The v1 runner drove a fixed stage skeleton (discovery → planning → joins →
validation → analyses → dashboard → verify → summary) with its own prompts,
validators, limits, and summary projection. Phase 8 replaced the useful half of
it with the declared ``analysis_workflow_v1`` graph; this phase deleted the
runner outright rather than keeping a second implementation.

These tests prove there is no live caller, engine value, import, API response,
or UI path left, and that the one supported exploratory-analysis entry point —
the generic creation endpoint — now starts the declared workflow.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.agent import prompts, runner, store
from app.agent.workflows import analysis as analysis_workflow
from app.main import create_app
from app.routes import agent_routes


FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


@pytest.fixture
def client():
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# No v1 implementation
# --------------------------------------------------------------------------- #
def test_the_v1_runner_class_and_its_stage_skeleton_are_deleted():
    for attribute in (
        "_Runner",
        "STAGES",
        "MAX_CUSTOM_ANALYSES",
        "MAX_QUERY_TILES",
        "_validate_planning_payload",
        "_validate_rules_payload",
        "_validate_analyses_payload",
        "_validate_dashboard_payload",
        "_validate_summary_payload",
    ):
        assert not hasattr(runner, attribute), attribute


def test_the_v1_summary_module_is_deleted_without_a_replacement_shim():
    assert importlib.util.find_spec("app.agent.summary") is None


def test_the_v1_stage_prompts_are_deleted():
    for attribute in (
        "PLANNING_SYSTEM",
        "planning_user",
        "RULES_SYSTEM",
        "rules_user",
        "ANALYSES_SYSTEM",
        "analyses_user",
        "DASHBOARD_SYSTEM",
        "dashboard_user",
        "SUMMARY_SYSTEM",
        "summary_user",
    ):
        assert not hasattr(prompts, attribute), attribute


def test_the_run_process_module_imports_no_analysis_domain_module():
    """``runner.py`` is a process layer: no compute or domain imports remain."""
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)

    forbidden = {
        "analytics",
        "assistant",
        "dashboard",
        "explore",
        "sandbox",
        "validation",
        "suggest",
        "joins",
        "prompts",
        "summary",
    }
    assert not (imported & forbidden), sorted(imported & forbidden)


# --------------------------------------------------------------------------- #
# No v1 writer, engine value, or projection
# --------------------------------------------------------------------------- #
def test_the_legacy_engine_value_is_not_supported_and_has_no_constant():
    assert not hasattr(store, "LEGACY_ANALYSIS_ENGINE")
    assert "analysis" not in store.RUN_ENGINES
    assert set(store.PROTOCOL_ENGINE_BY_RUN_KIND) == {"intake"}


def test_no_writer_can_create_a_v1_record(workspace_with_data):
    with pytest.raises(workspaces.WorkspaceError, match="run kind"):
        store.new_run(workspace_with_data, "auto", None, kind="analysis")
    with pytest.raises(workspaces.WorkspaceError, match="run kind"):
        runner.start_run(workspace_with_data, "auto", {}, kind="analysis")


def test_records_and_summaries_carry_no_v1_projection(workspace_with_data):
    protocol = store.new_run(workspace_with_data, "auto", None, kind="intake")
    command = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
    )

    for record in (protocol, command):
        assert "discovery" not in record
        assert "custom_analyses" not in record["usage"]
        assert "domain" not in store.run_summary(record)


def test_a_run_record_kind_is_never_inferred(workspace_with_data):
    """``analysis`` was the read-time default for a record without a kind."""
    run = store.new_run(workspace_with_data, "auto", None, kind="intake")
    path = store.run_dir(workspace_with_data, run["id"]) / "run.json"
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("kind")
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load_run(workspace_with_data, run["id"])
    assert "kind" not in loaded
    assert store.run_summary(loaded)["kind"] is None


# --------------------------------------------------------------------------- #
# The supported exploratory-analysis caller
# --------------------------------------------------------------------------- #
def test_the_generic_endpoint_starts_the_declared_analysis_workflow(
    client, workspace_with_data, monkeypatch
):
    started = {}

    def capture(workspace, mode, command, parent_run_id=None, context=None):
        started.update(
            mode=mode, command=command, parent_run_id=parent_run_id, context=context
        )
        return {"id": "captured-analysis-run"}

    def forbidden(*_args, **_kwargs):  # pragma: no cover - the assertion is the point
        raise AssertionError("the endpoint must not create a protocol run")

    monkeypatch.setattr(runner, "start_command_run", capture)
    monkeypatch.setattr(runner, "start_run", forbidden)

    response = client.post(
        f"/api/workspaces/{workspace_with_data.id}/agent/runs",
        json={"mode": "auto", "context": {"objective": "revenue completeness"}},
    )

    assert response.status_code == 200
    assert started["command"]["goal_template"] == "data_analysis"
    assert started["context"] == {"objective": "revenue completeness"}
    # The template resolves to the declared workflow's full outcome set.
    assert analysis_workflow.outcomes_for_template(
        started["command"]["goal_template"]
    ) == analysis_workflow.FULL_ANALYSIS_OUTCOMES


def test_an_explicit_analysis_kind_routes_to_the_workflow_too(
    client, workspace_with_data, monkeypatch
):
    started = {}
    monkeypatch.setattr(
        runner,
        "start_command_run",
        lambda *args, **kwargs: started.update(command=args[2]) or {"id": "run"},
    )

    response = client.post(
        f"/api/workspaces/{workspace_with_data.id}/agent/runs",
        json={"mode": "auto", "kind": "analysis"},
    )

    assert response.status_code == 200
    assert started["command"]["goal_template"] == "data_analysis"
    assert agent_routes.ANALYSIS_COMMAND_TEXT == started["command"]["text"]


def test_an_unknown_run_kind_fails_closed(client, workspace_with_data):
    response = client.post(
        f"/api/workspaces/{workspace_with_data.id}/agent/runs",
        json={"mode": "auto", "kind": "v1"},
    )

    assert response.status_code == 400
    assert "run kind" in response.json()["detail"]


def test_no_backend_module_references_the_v1_runner():
    package = Path(runner.__file__).parents[1]
    hits = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "_Runner(" in path.read_text(encoding="utf-8")
    ]
    assert hits == []


# --------------------------------------------------------------------------- #
# No UI path
# --------------------------------------------------------------------------- #
def test_the_frontend_contract_has_no_v1_engine_kind_or_launcher():
    types = (FRONTEND / "types.ts").read_text(encoding="utf-8")
    assert "'workflow' | 'action' | 'intake' | null" in types
    assert "'audit' | 'analysis' | 'intake'" not in types
    assert "AgentDiscovery" not in types

    run_store = (FRONTEND / "composables" / "useAgentRun.ts").read_text(encoding="utf-8")
    assert "startRun" not in run_store

    for path in FRONTEND.rglob("*.vue"):
        assert "startRun" not in path.read_text(encoding="utf-8"), path.name


def test_the_only_engine_dispatch_branches_are_the_supported_ones():
    source = inspect.getsource(runner._execute)
    branches = sorted(
        line.split("engine ==")[1].strip().rstrip(":")
        for line in source.splitlines()
        if " engine ==" in line
    )
    assert branches == [
        "store.ACTION_ENGINE",
        "store.INTAKE_ENGINE",
        "store.WORKFLOW_ENGINE",
    ]
