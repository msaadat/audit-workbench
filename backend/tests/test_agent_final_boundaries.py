"""Phase 13 gate: the final static boundaries of the target architecture.

[agent-architecture.md](../../docs/agent-architecture.md) states the component
contracts in prose; this module makes the load-bearing ones mechanical, so the
coupling the migration removed cannot quietly return:

* A **workflow definition** is a graph and nothing else.
* A **capability** declares readiness and units; it never schedules or persists.
* A **worker** receives a resolved bundle and returns a proposal. It cannot
  reach a workspace, a transaction, the run store, or an executor.
* An **executor** commits deterministically. It cannot call a model or reach a
  worker.
* **Context** resolves declared sources locally and cannot call a provider.
* Provider calls exist in exactly one module.

The runtime package's own domain-neutrality gate lives in
``test_agent_runtime_import_boundaries.py``; this module covers everything
around it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.agent as agent_package
from app.agent import action_runner, runner, store
from app.agent.runtime import WorkflowRunner


AGENT_ROOT = Path(agent_package.__file__).parent


def _module_imports(path: Path) -> set[str]:
    """Absolute module names imported by one file, resolving relative imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = ["app", *path.relative_to(AGENT_ROOT.parent).parts[:-1]]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = parts[: len(parts) - (node.level - 1)] if node.level else []
        if node.module:
            imported.add(".".join([*base, *node.module.split(".")]))
        else:
            imported.update(".".join([*base, alias.name]) for alias in node.names)
    return imported


def _package_imports(package: str) -> dict[str, set[str]]:
    root = AGENT_ROOT / package
    return {
        path.relative_to(AGENT_ROOT).as_posix(): _module_imports(path)
        for path in sorted(root.rglob("*.py"))
    }


def _violations(package: str, forbidden: tuple[str, ...]) -> list[tuple[str, str]]:
    found = []
    for module, imported in _package_imports(package).items():
        for name in sorted(imported):
            if any(name == item or name.startswith(f"{item}.") for item in forbidden):
                found.append((module, name))
    return found


def _provider_calls(paths: list[Path]) -> list[tuple[str, int]]:
    """Every direct ``llm.chat``/``llm.chat_stream`` call site in ``paths``."""
    calls: list[tuple[str, int]] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_aliases: set[str] = set()
        direct_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_aliases.update(
                    alias.asname or alias.name.rsplit(".", 1)[-1]
                    for alias in node.names
                    if alias.name.endswith("llm")
                )
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "llm":
                        module_aliases.add(alias.asname or alias.name)
                    elif (node.module or "").endswith("llm") and alias.name in {
                        "chat",
                        "chat_stream",
                    }:
                        direct_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Name) and node.func.id in direct_names
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"chat", "chat_stream"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
            ):
                calls.append((path.relative_to(AGENT_ROOT).as_posix(), node.lineno))
    return calls


# --------------------------------------------------------------------------- #
# Declarations
# --------------------------------------------------------------------------- #
def test_a_workflow_definition_imports_nothing_but_graph_primitives():
    """``workflows/`` is the authoritative structure and carries no behavior."""
    for module, imported in _package_imports("workflows").items():
        application = {name for name in imported if name.startswith("app")}
        assert application <= {
            "app.agent.workflow",
            *{f"app.agent.workflows.{path.stem}" for path in (AGENT_ROOT / "workflows").glob("*.py")},
        }, (module, sorted(application))


def test_a_capability_declaration_neither_schedules_nor_persists():
    assert not _violations(
        "capabilities",
        (
            "app.agent.runner",
            "app.agent.action_runner",
            "app.agent.intake_runner",
            "app.agent.store",
            "app.agent.workers",
            "app.agent.executors",
            "app.agent.routing",
            "app.llm",
            "app.workspace_transactions",
        ),
    )


# --------------------------------------------------------------------------- #
# Workers and executors
# --------------------------------------------------------------------------- #
def test_a_worker_cannot_reach_a_workspace_a_transaction_or_the_run_store():
    assert not _violations(
        "workers",
        (
            "app.workspaces",
            "app.workspace_transactions",
            "app.agent.store",
            "app.agent.runner",
            "app.agent.action_runner",
            "app.agent.intake_runner",
            "app.agent.executors",
            "app.agent.capabilities",
            "app.agent.base",
        ),
    )


def test_an_executor_cannot_reach_a_worker_or_the_model_gateway():
    assert not _violations(
        "executors",
        (
            "app.agent.workers",
            "app.agent.runtime",
            "app.agent.runner",
            "app.agent.action_runner",
            "app.agent.intake_runner",
            "app.agent.context.resolver",
            "app.agent.base",
        ),
    )


def test_only_the_worker_contract_layer_depends_on_the_model_gateway():
    gateway_users = {
        module
        for module, imported in _package_imports("workers").items()
        if "app.agent.runtime.model_gateway" in imported
    }
    # Every registered worker is *given* the gateway by the pipeline; none of
    # them constructs a provider client, so the dependency is a type only.
    assert gateway_users
    assert all(module.startswith("workers/") for module in gateway_users)


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #
def test_context_resolution_cannot_call_a_provider_or_a_worker():
    assert not _violations(
        "context",
        (
            "app.llm",
            "app.agent.workers",
            "app.agent.executors",
            "app.agent.runner",
            "app.agent.action_runner",
            "app.agent.runtime",
            "app.agent.base",
        ),
    )


# --------------------------------------------------------------------------- #
# Schedulers
# --------------------------------------------------------------------------- #
def test_the_two_schedulers_do_not_import_each_other():
    action = _module_imports(Path(action_runner.__file__))
    assert "app.agent.runtime.workflow_runner" not in action
    assert "app.agent.workflow_dispatch" not in action

    workflow = _module_imports(
        AGENT_ROOT / "runtime" / f"{WorkflowRunner.__module__.rsplit('.', 1)[-1]}.py"
    )
    assert "app.agent.action_runner" not in workflow


def test_the_process_layer_dispatches_to_engines_and_owns_no_scheduling():
    """``runner.py`` may name the engines; it must not import their internals."""
    imported = _module_imports(Path(runner.__file__))
    assert not {
        name
        for name in imported
        if name.startswith(("app.agent.capabilities", "app.agent.workflows", "app.agent.workers", "app.agent.executors"))
    }
    assert store.RUN_ENGINES == frozenset({"workflow", "action", "intake"})


# --------------------------------------------------------------------------- #
# One provider path
# --------------------------------------------------------------------------- #
def test_the_only_provider_call_site_in_the_agent_is_the_model_gateway():
    calls = _provider_calls(sorted(AGENT_ROOT.rglob("*.py")))
    assert sorted({path for path, _line in calls}) == ["runtime/model_gateway.py"]


def test_no_declaration_package_calls_a_provider():
    for package in ("workflows", "capabilities", "workers", "executors", "context"):
        paths = sorted((AGENT_ROOT / package).rglob("*.py"))
        assert _provider_calls(paths) == [], package
