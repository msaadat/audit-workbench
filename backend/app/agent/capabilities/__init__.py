"""Grouped capability composition and startup validation.

This package composes each workflow's capability registry from its grouped
modules and validates the composition against the authoritative dependency graph
that owns it — :mod:`agent.workflows.audit` for the audit lifecycle and
:mod:`agent.workflows.analysis` for the exploratory data-analysis workflow —
before any writer relies on it.

``AUDIT_REGISTRY`` and ``ANALYSIS_REGISTRY`` are the live registries for routing
and dispatch. Both are built and validated at import (startup validation), so an
inconsistent grouping, a graph mismatch, an undeclared context preset, or a
dependency cycle fails fast rather than at run time. The audit artifact hashes
the executors stamp and reconcile against are re-exported here from
:mod:`._shared`.
"""

from __future__ import annotations

from typing import Iterable

from ..context import PRESETS
from ..runtime import CapabilityExecutionRegistry
from ..workflow import CapabilityRegistry
from ..workflow import Capability
from ..workflows import analysis as analysis_workflow
from ..workflows import audit as audit_workflow
from ..workflows import doc_tests as doc_tests_workflow
from ..workflows import documents as documents_workflow
from . import (
    analysis,
    doc_tests,
    documents,
    fieldwork,
    planning,
    reporting,
    sources,
    tests as tests_group,
)
from ._shared import (
    all_tests,
    apm_sha1,
    planning_basis_sha1,
    rcm_row_sha1,
)


class CapabilityGroupView:
    """A subset of one grouped module's declarations, for a second workflow.

    The document capabilities are declared exactly once, in
    :mod:`agent.capabilities.documents`. The audit graph declares three of the
    four — generation, never auditor review — so it composes them through this
    view rather than through a second set of declarations that could drift.
    """

    def __init__(self, module, capability_ids: tuple[str, ...]):
        self._module = module
        self.CAPABILITY_IDS = tuple(capability_ids)
        declared = {value for value in module.CAPABILITY_IDS}
        unknown = sorted(set(self.CAPABILITY_IDS) - declared)
        if unknown:
            raise ValueError(
                f"Capability group view names undeclared capabilities: {unknown}."
            )

    def capabilities(self) -> tuple[Capability, ...]:
        wanted = set(self.CAPABILITY_IDS)
        return tuple(
            capability
            for capability in self._module.capabilities()
            if capability.id in wanted
        )

# The audit dependency graph and outcome sets are authoritative in
# ``workflows.audit``; these re-exports keep routing call sites reading from one
# audit-domain package.
FULL_AUDIT_OUTCOMES = audit_workflow.FULL_AUDIT_OUTCOMES
TEMPLATE_OUTCOMES = audit_workflow.TEMPLATE_OUTCOMES
outcomes_for_template = audit_workflow.outcomes_for_template

FULL_ANALYSIS_OUTCOMES = analysis_workflow.FULL_ANALYSIS_OUTCOMES
ANALYSIS_TEMPLATE_OUTCOMES = analysis_workflow.TEMPLATE_OUTCOMES
analysis_outcomes_for_template = analysis_workflow.outcomes_for_template

FULL_DOCUMENT_OUTCOMES = documents_workflow.FULL_DOCUMENT_OUTCOMES
DOCUMENT_TEMPLATE_OUTCOMES = documents_workflow.TEMPLATE_OUTCOMES
document_outcomes_for_template = documents_workflow.outcomes_for_template

FULL_DOC_TEST_OUTCOMES = doc_tests_workflow.FULL_DOC_TEST_OUTCOMES
DOC_TEST_TEMPLATE_OUTCOMES = doc_tests_workflow.TEMPLATE_OUTCOMES
doc_test_outcomes_for_template = doc_tests_workflow.outcomes_for_template

# Grouped capability modules in authoritative order. Each module owns a disjoint
# slice of its workflow graph and exposes ``CAPABILITY_IDS`` plus
# ``capabilities()``. Order matters: a group's dependencies must be registered
# before its dependents so the readiness projection can resolve them, which is
# why the audit composition includes both document analysis and the independent
# data-analysis branch that the full-audit outcome order completes before APM
# preparation.
AUDIT_DOCUMENT_GROUP = CapabilityGroupView(documents, documents.AUDIT_CAPABILITY_IDS)
CAPABILITY_GROUPS = (
    sources,
    AUDIT_DOCUMENT_GROUP,
    analysis,
    planning,
    tests_group,
    fieldwork,
    reporting,
)
ANALYSIS_CAPABILITY_GROUPS = (analysis,)
DOCUMENT_CAPABILITY_GROUPS = (documents,)
DOC_TEST_CAPABILITY_GROUPS = (doc_tests,)


class AuditCompositionError(ValueError):
    """Raised when a grouped capability composition is internally inconsistent."""


def _group_ids(groups: tuple) -> tuple[str, ...]:
    return tuple(
        capability_id for group in groups for capability_id in group.CAPABILITY_IDS
    )


def grouped_capability_ids() -> tuple[str, ...]:
    """Every capability ID contributed by the audit groups, in order."""

    return _group_ids(CAPABILITY_GROUPS)


def grouped_analysis_capability_ids() -> tuple[str, ...]:
    """Every capability ID contributed by the analysis groups, in order."""

    return _group_ids(ANALYSIS_CAPABILITY_GROUPS)


def _build_registry(groups: tuple) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for group in groups:
        for capability in group.capabilities():
            registry.register(capability)
    return registry


def build_audit_registry() -> CapabilityRegistry:
    """Compose the audit capability registry from the grouped modules."""

    return _build_registry(CAPABILITY_GROUPS)


def build_analysis_registry() -> CapabilityRegistry:
    """Compose the analysis capability registry from the grouped modules."""

    return _build_registry(ANALYSIS_CAPABILITY_GROUPS)


def build_documents_registry() -> CapabilityRegistry:
    """Compose the document capability registry from the grouped modules."""

    return _build_registry(DOCUMENT_CAPABILITY_GROUPS)


def build_doc_tests_registry() -> CapabilityRegistry:
    """Compose the document-test capability registry from the grouped modules."""

    return _build_registry(DOC_TEST_CAPABILITY_GROUPS)


def _registered_preset_ids() -> frozenset[str]:
    return frozenset(preset.preset_id for preset in PRESETS.all())


def validate_composition(
    registry: CapabilityRegistry,
    *,
    dependencies: dict[str, tuple[str, ...]],
    groups: tuple,
    label: str,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityRegistry:
    """Validate a composed registry against its authoritative dependency graph.

    Checks that the grouped modules partition the graph exactly once, that the
    composed registry declares precisely the authoritative capabilities with the
    authoritative edges, that the dependency closure is acyclic, that every
    declared context preset is registered, and — when supplied — that an
    execution binding exists for each capability.
    """

    authoritative_ids = frozenset(dependencies)

    grouped = _group_ids(groups)
    duplicates = sorted({cid for cid in grouped if grouped.count(cid) > 1})
    if duplicates:
        raise AuditCompositionError(
            "Capability groups overlap on: " + ", ".join(duplicates) + "."
        )
    grouped_set = frozenset(grouped)
    if grouped_set != authoritative_ids:
        missing = sorted(authoritative_ids - grouped_set)
        extra = sorted(grouped_set - authoritative_ids)
        raise AuditCompositionError(
            f"Grouped capability partition does not cover the {label} graph "
            f"(missing: {missing}; extra: {extra})."
        )

    declared = [capability.id for capability in registry.all()]
    declared_set = frozenset(declared)
    if declared_set != authoritative_ids:
        missing = sorted(authoritative_ids - declared_set)
        extra = sorted(declared_set - authoritative_ids)
        raise AuditCompositionError(
            f"Composed registry does not match the authoritative {label} graph "
            f"(missing: {missing}; extra: {extra})."
        )

    for capability in registry.all():
        expected = dependencies[capability.id]
        if tuple(capability.depends_on) != expected:
            raise AuditCompositionError(
                f"Capability '{capability.id}' declares dependencies "
                f"{tuple(capability.depends_on)} but the authoritative graph "
                f"requires {expected}."
            )

    # Acyclicity: closure over every capability raises on a dependency cycle.
    registry.closure(declared)

    known_presets = _registered_preset_ids()
    for capability in registry.all():
        declaration = capability.context
        preset_ids = (
            tuple(declaration.values())
            if isinstance(declaration, dict)
            else (() if declaration is None else (declaration,))
        )
        for preset_id in preset_ids:
            if str(preset_id) not in known_presets:
                raise AuditCompositionError(
                    f"Capability '{capability.id}' declares unregistered context "
                    f"preset '{preset_id}'."
                )

    if executions is not None:
        # CapabilityExecutionRegistry.validate raises on missing/unknown bindings.
        executions.validate(registry)

    return registry


def validate_audit_composition(
    registry: CapabilityRegistry,
    *,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityRegistry:
    """Validate the composed registry against the authoritative audit graph."""

    return validate_composition(
        registry,
        dependencies=audit_workflow.DEPENDENCIES,
        groups=CAPABILITY_GROUPS,
        label="audit",
        executions=executions,
    )


def validate_analysis_composition(
    registry: CapabilityRegistry,
    *,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityRegistry:
    """Validate the composed registry against the authoritative analysis graph."""

    return validate_composition(
        registry,
        dependencies=analysis_workflow.DEPENDENCIES,
        groups=ANALYSIS_CAPABILITY_GROUPS,
        label="analysis",
        executions=executions,
    )


def validate_documents_composition(
    registry: CapabilityRegistry,
    *,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityRegistry:
    """Validate the composed registry against the authoritative document graph."""

    return validate_composition(
        registry,
        dependencies=documents_workflow.DEPENDENCIES,
        groups=DOCUMENT_CAPABILITY_GROUPS,
        label="documents",
        executions=executions,
    )


def validate_doc_tests_composition(
    registry: CapabilityRegistry,
    *,
    executions: CapabilityExecutionRegistry | None = None,
) -> CapabilityRegistry:
    """Validate the composed registry against the authoritative doc-test graph."""

    return validate_composition(
        registry,
        dependencies=doc_tests_workflow.DEPENDENCIES,
        groups=DOC_TEST_CAPABILITY_GROUPS,
        label="document tests",
        executions=executions,
    )


# Startup validation: build and validate each grouped composition at import time.
AUDIT_REGISTRY = validate_audit_composition(build_audit_registry())
ANALYSIS_REGISTRY = validate_analysis_composition(build_analysis_registry())
DOCUMENTS_REGISTRY = validate_documents_composition(build_documents_registry())
DOC_TESTS_REGISTRY = validate_doc_tests_composition(build_doc_tests_registry())
# The live registry name used by routing and audit dispatch.
REGISTRY = AUDIT_REGISTRY

# Every registry a workflow run may be scheduled against, keyed by the
# authoritative workflow definition ID persisted on the run.
REGISTRY_BY_WORKFLOW = {
    audit_workflow.WORKFLOW_ID: AUDIT_REGISTRY,
    analysis_workflow.WORKFLOW_ID: ANALYSIS_REGISTRY,
    documents_workflow.WORKFLOW_ID: DOCUMENTS_REGISTRY,
    doc_tests_workflow.WORKFLOW_ID: DOC_TESTS_REGISTRY,
}


def workflow_for_outcomes(outcomes) -> str | None:
    """The workflow definition ID that declares every requested outcome.

    Returns ``None`` when no registered workflow declares every requested
    outcome, so a caller fails closed instead of materializing a mixed closure.

    The document capabilities are declared by two graphs — their own workflow and,
    for generation only, the audit graph that depends on them — so a match is
    resolved to the *narrowest* workflow that declares everything requested.
    "Analyze these documents" is then the document workflow rather than an audit
    whose other outcomes were never asked for, while an APM-plus-analysis set
    resolves to the audit graph that deliberately declares both.
    """

    requested = set(str(value) for value in outcomes or [])
    if not requested:
        return None
    matches = [
        (len({capability.id for capability in registry.all()}), workflow_id)
        for workflow_id, registry in REGISTRY_BY_WORKFLOW.items()
        if requested <= {capability.id for capability in registry.all()}
    ]
    if not matches:
        return None
    return min(matches)[1]


def workflow_state(
    workspace, scope: dict | None = None, *, only: Iterable[str] | None = None
) -> dict[str, dict]:
    """Deterministic readiness projection for every audit capability.

    ``only`` narrows the sweep to those capabilities and their dependency
    closure — see :meth:`CapabilityRegistry.workflow_state`.
    """

    return REGISTRY.workflow_state(workspace, scope, only=only)


def analysis_workflow_state(
    workspace, scope: dict | None = None, *, only: Iterable[str] | None = None
) -> dict[str, dict]:
    """Deterministic readiness projection for every analysis capability."""

    return ANALYSIS_REGISTRY.workflow_state(workspace, scope, only=only)


def documents_workflow_state(
    workspace, scope: dict | None = None, *, only: Iterable[str] | None = None
) -> dict[str, dict]:
    """Deterministic readiness projection for every document capability."""

    return DOCUMENTS_REGISTRY.workflow_state(workspace, scope, only=only)


def doc_tests_workflow_state(
    workspace, scope: dict | None = None, *, only: Iterable[str] | None = None
) -> dict[str, dict]:
    """Deterministic readiness projection for every document-test capability."""

    return DOC_TESTS_REGISTRY.workflow_state(workspace, scope, only=only)


__all__ = [
    "ANALYSIS_CAPABILITY_GROUPS",
    "ANALYSIS_REGISTRY",
    "ANALYSIS_TEMPLATE_OUTCOMES",
    "AUDIT_DOCUMENT_GROUP",
    "AUDIT_REGISTRY",
    "AuditCompositionError",
    "CAPABILITY_GROUPS",
    "CapabilityGroupView",
    "DOC_TESTS_REGISTRY",
    "DOC_TEST_CAPABILITY_GROUPS",
    "DOC_TEST_TEMPLATE_OUTCOMES",
    "DOCUMENT_CAPABILITY_GROUPS",
    "DOCUMENT_TEMPLATE_OUTCOMES",
    "DOCUMENTS_REGISTRY",
    "FULL_ANALYSIS_OUTCOMES",
    "FULL_DOC_TEST_OUTCOMES",
    "FULL_AUDIT_OUTCOMES",
    "FULL_DOCUMENT_OUTCOMES",
    "REGISTRY",
    "REGISTRY_BY_WORKFLOW",
    "TEMPLATE_OUTCOMES",
    "analysis_outcomes_for_template",
    "analysis_workflow_state",
    "apm_sha1",
    "build_analysis_registry",
    "build_audit_registry",
    "build_doc_tests_registry",
    "build_documents_registry",
    "doc_test_outcomes_for_template",
    "doc_tests_workflow_state",
    "document_outcomes_for_template",
    "documents_workflow_state",
    "grouped_analysis_capability_ids",
    "grouped_capability_ids",
    "outcomes_for_template",
    "all_tests",
    "planning_basis_sha1",
    "rcm_row_sha1",
    "validate_analysis_composition",
    "validate_audit_composition",
    "validate_composition",
    "validate_doc_tests_composition",
    "validate_documents_composition",
    "workflow_for_outcomes",
    "workflow_state",
]
