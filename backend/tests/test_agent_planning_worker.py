from __future__ import annotations

import ast
import inspect

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import WORKERS, WorkerRequest, WorkerRunError
from app.agent.workers import planning


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1):
        self.calls.append(
            {"system": system, "user": user, "activity": activity, "attempt": attempt}
        )
        return self.responses.pop(0)


def _bundle(*, planning_context=None, template=None, current=""):
    values = (
        (
            "apm_template",
            "template:apm",
            template or "# Engagement\n\n# Scope\n",
        ),
        ("current_apm", "planning:apm", current),
        (
            "planning_context",
            "planning:context",
            planning_context
            or {
                "context": {
                    "objective": "Assess procurement approvals",
                    "scope": "Purchase commitments",
                }
            },
        ),
    )
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=ContextRepresentation("planning_context"),
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, content in values
    )
    return ContextBundle(
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="planning.apm",
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "apm-input"},
        activity={"artifact_refs": ["planning:apm"]},
    )


def test_registered_apm_worker_uses_only_bundle_and_returns_validated_proposal():
    gateway = _Gateway(
        [
            "# Engagement\n\nAssess procurement approvals.\n\n"
            "# Scope\n\nPurchase commitments for {{entity}}."
        ]
    )

    result = WORKERS.execute(
        _request(_bundle(planning_context={"context": {}})), gateway
    )

    assert result.proposal["apm_markdown"].endswith(
        "_[entity - context not available]_."
    )
    assert gateway.calls[0]["system"] == planning.APM_SYSTEM
    assert gateway.calls[0]["attempt"] == 1
    assert gateway.calls[0]["activity"]["artifact_refs"] == ("planning:apm",)
    assert gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == "apm"


def test_apm_semantic_validation_gets_one_bounded_repair_with_specific_guidance():
    gateway = _Gateway(
        [
            "# Engagement\n\nFirst draft omits scope.",
            "# Engagement\n\nCorrected.\n\n# Scope\n\nPurchase commitments.",
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert [call["attempt"] for call in gateway.calls] == [1, 2]
    assert "missing template section 'scope'" in gateway.calls[1]["user"]


def test_apm_semantic_validation_rejects_structured_context_contradiction():
    gateway = _Gateway(
        [
            "# Engagement\n\nObjective not available.\n\n# Scope\n\nPurchase commitments.",
            "# Engagement\n\nObjective undefined.\n\n# Scope\n\nPurchase commitments.",
        ]
    )

    with pytest.raises(WorkerRunError, match="objective is unavailable"):
        WORKERS.execute(_request(), gateway)
    assert len(gateway.calls) == 2


def test_apm_worker_request_detaches_source_content_and_cannot_mutate_it():
    planning_context = {"context": {"objective": "Original", "scope": "Original"}}
    request = _request(_bundle(planning_context=planning_context))
    planning_context["context"]["objective"] = "Changed after request"
    gateway = _Gateway(
        ["# Engagement\n\nOriginal objective.\n\n# Scope\n\nOriginal scope."]
    )

    WORKERS.execute(request, gateway)

    assert "Changed after request" not in gateway.calls[0]["user"]
    assert planning_context["context"]["objective"] == "Changed after request"


def test_planning_worker_has_no_workspace_store_resolver_or_scheduler_dependency():
    source = inspect.getsource(planning)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name.endswith(
            (
                "workspaces",
                "workspace_transactions",
                "store",
                "resolver",
                "workflow_runner",
                "action_runner",
            )
        )
        for name in imported
    )
    assert ".ws" not in source
    assert "load_workspace" not in source
