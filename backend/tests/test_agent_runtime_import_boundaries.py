"""Static import boundaries for the domain-neutral agent runtime package."""

from __future__ import annotations

import ast
from pathlib import Path

import app.agent.runtime as runtime_package
from app.agent.action_runner import ActionRunner
from app.agent.runtime import WorkflowRunner


FORBIDDEN_DOMAIN_PREFIXES = (
    "app.analysis_payloads",
    "app.data_tests",
    "app.doc_tests",
    "app.document_analysis",
    "app.document_context",
    "app.document_search",
    "app.documents",
    "app.engagement_progress",
    "app.findings",
    "app.planning",
    "app.rcm_execution",
    "app.report",
    "app.templates_store",
    "app.working_papers",
    "app.agent.audit_capabilities",
    "app.agent.audit_execution",
    "app.agent.audit_workers",
    "app.agent.capabilities",
    "app.agent.context_bundles",
    "app.agent.workflows",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = ["app", "agent", "runtime"]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package_parts) - (node.level - 1)
            base = package_parts[:keep]
        else:
            base = []
        if node.module:
            base.extend(node.module.split("."))
            imported.add(".".join(base))
        else:
            imported.update(".".join([*base, alias.name]) for alias in node.names)
    return imported


def test_runtime_modules_do_not_import_audit_or_product_domains():
    runtime_root = Path(runtime_package.__file__).parent
    violations: list[str] = []

    for path in sorted(runtime_root.glob("*.py")):
        for imported in sorted(_imported_modules(path)):
            if any(
                imported == prefix or imported.startswith(prefix + ".")
                for prefix in FORBIDDEN_DOMAIN_PREFIXES
            ):
                violations.append(f"{path.name}: {imported}")

    assert violations == []


def test_workflow_runner_has_no_action_inheritance_or_domain_stage_methods():
    domain_stage_methods = {
        "_planning_basis",
        "_apm",
        "_rcm",
        "_planned_tests",
        "_definitions",
        "_executions",
        "_rollup",
        "_finding_drafts",
        "_working_papers",
        "_dashboard",
        "_report",
        "_verify",
    }

    assert not issubclass(WorkflowRunner, ActionRunner)
    assert domain_stage_methods.isdisjoint(WorkflowRunner.__dict__)
    assert not (Path(runtime_package.__file__).parent.parent / "workflow_runner.py").exists()
