"""Focused tests for the registered planning-context synthesis worker (P7A.2)."""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app import documents, workspaces
from app.agent.context import ContextResolver, planning_context_scope
from app.agent.workers import WORKERS, WorkerRequest
from app.agent.workers.model import WorkerRunError
from app.agent.workers.planning import (
    PLANNING_CONTEXT_SYSTEM,
    PLANNING_CONTEXT_WORKER,
    _planning_context_response_schema,
    validate_planning_context_proposal,
)

CAPABILITY_ID = "planning.context_ready"
WORKER_ID = "planning.context"


class _Gateway:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, int]] = []

    def complete(self, system, user, activity, *, attempt=1, **_kwargs):
        self.calls.append((system, user, attempt))
        return self.responses[min(attempt, len(self.responses)) - 1]


def _bundle(*, analysis_summary: str | None = None):
    ws = workspaces.create_workspace("Planning context worker")
    policy = documents.add_document(
        ws,
        "Procurement Policy.txt",
        b"Procurement Policy: purchases require documented approval before commitment.",
        category="policy",
    )
    if analysis_summary is not None:
        from app import document_analysis

        extracted = documents.extract_document(ws, policy["id"])
        document_analysis.persist_analysis(
            ws,
            policy,
            extracted,
            {
                "summary_markdown": analysis_summary,
                "audit_notes_markdown": "",
                "citations": [],
            },
            provider="fake",
            model="fake",
        )
    capability = type(
        "_Capability",
        (),
        {"id": CAPABILITY_ID, "context": "planning.context"},
    )()
    unit = {"id": "planning_context"}
    _manifest, bundle = ContextResolver().resolve(
        ws, capability, unit, planning_context_scope(ws)
    )
    return WorkerRequest(
        worker_id=WORKER_ID,
        capability_id=CAPABILITY_ID,
        unit_id=unit["id"],
        context=bundle,
        activity={"artifact_refs": ["planning:context"]},
    )


def test_planning_context_worker_uses_only_the_supplied_bundle():
    request = _bundle()
    gateway = _Gateway(
        '{"context": {"objective": "Assess procurement approvals", '
        '"scope": "Requisition to payment"}}'
    )

    result = WORKERS.execute(request, gateway)

    system, user, _attempt = gateway.calls[0]
    assert system == PLANNING_CONTEXT_SYSTEM
    assert "CURRENT PLANNING CONTEXT:" in user
    assert "INCLUDED DOCUMENT CONTENT:" in user
    # A document with no current analysis still grounds synthesis, through its
    # bounded leading pages rather than a generated summary.
    assert "purchases require documented approval" in user
    assert dict(result.proposal["context"]) == {
        "objective": "Assess procurement approvals",
        "scope": "Requisition to payment",
    }


def test_planning_context_response_schema_normalizes_a_flat_payload():
    normalized = _planning_context_response_schema(
        '{"objective": "Assess approvals", "scope": "Procurement", "notes": 3}'
    )

    # A provider that flattens the wrapper still yields usable fields, and the
    # undeclared key is simply not a planning field.
    assert normalized["context"] == {
        "objective": "Assess approvals",
        "scope": "Procurement",
    }


def test_planning_context_response_schema_rejects_a_non_string_field():
    with pytest.raises(ValueError, match="context.scope must be a string"):
        _planning_context_response_schema('{"context": {"scope": ["a", "b"]}}')


def test_planning_context_worker_recovers_labelled_facts_from_the_supplied_summary():
    request = _bundle(
        analysis_summary=(
            "- **Objective:** Review procurement approvals\n"
            "- **Scope:** Procurement authorization controls"
        )
    )
    gateway = _Gateway('{"context": {}}')

    result = WORKERS.execute(request, gateway)

    # A valid response with no usable field is not repaired by asking again: the
    # labelled facts already supplied are the better answer and cost nothing.
    assert len(gateway.calls) == 1
    assert dict(result.proposal["context"]) == {
        "objective": "Review procurement approvals",
        "scope": "Procurement authorization controls",
    }
    assert result.proposal["recovered_from_labelled_facts"] is True


def test_planning_context_worker_fails_when_nothing_can_be_grounded():
    request = _bundle()
    gateway = _Gateway('{"context": {}}', '{"context": {}}')

    # No model field and no labelled facts is a real contract violation, so the
    # bounded repair runs and then the unit fails rather than committing nothing.
    with pytest.raises(WorkerRunError):
        WORKERS.execute(request, gateway)

    assert len(gateway.calls) == 2
    assert "could not be used" in gateway.calls[1][1]


def test_planning_context_worker_drops_undeclared_and_blank_fields():
    request = _bundle()

    proposal = validate_planning_context_proposal(
        {
            "context": {
                "objective": "  Assess approvals  ",
                "scope": "   ",
                "audit_opinion": "unqualified",
            }
        },
        request,
    )

    assert proposal == {"context": {"objective": "Assess approvals"}}


def test_the_planning_context_worker_module_takes_no_workspace_dependency():
    source = pathlib.Path(inspect.getsourcefile(PLANNING_CONTEXT_WORKER.implementation))
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported = {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert not imported & {
        "Workspace",
        "mutate",
        "parent_hashes",
        "ContextResolver",
        "WorkflowRunner",
        "store",
    }
