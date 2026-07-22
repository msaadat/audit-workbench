"""Static import boundaries for the domain-neutral agent runtime package."""

from __future__ import annotations

import ast
from pathlib import Path

import app.agent.runtime as runtime_package


FORBIDDEN_DOMAIN_PREFIXES = (
    "app.dashboard",
    "app.data_tests",
    "app.doc_tests",
    "app.document_analysis",
    "app.document_context",
    "app.document_search",
    "app.documents",
    "app.findings",
    "app.planning",
    "app.rcm_execution",
    "app.report",
    "app.templates_store",
    "app.working_papers",
    "app.agent.audit_capabilities",
    "app.agent.audit_execution",
    "app.agent.audit_workers",
    "app.agent.context_bundles",
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
