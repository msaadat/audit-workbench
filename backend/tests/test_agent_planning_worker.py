from __future__ import annotations

import ast
import json
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
from app.agent.workers.model import WorkerResponseValidationError
from app.agent.workers.planning import validate_delta_proposal


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1):
        self.calls.append(
            {"system": system, "user": user, "activity": activity, "attempt": attempt}
        )
        return self.responses.pop(0)


def _bundle(
    *, planning_context=None, template=None, current="", population=None,
    instruction=None,
):
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
    ) + (
        (("population_summary", "workspace:populations", population),)
        if population is not None
        else ()
    ) + (
        (("instruction", "instruction:abcdef123456", instruction),)
        if instruction is not None
        else ()
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
    # The repair corrects the rejected draft rather than regenerating from the
    # context alone, so the sections that passed survive their sibling's failure.
    assert "First draft omits scope." in gateway.calls[1]["user"]
    assert "PREVIOUS APM DRAFT:" in gateway.calls[1]["user"]


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


_SHIPPED_TEMPLATE = (
    "# Audit Planning Memorandum\n\n"
    "## Engagement\n\n"
    "- Entity: {{entity}}\n"
    "- Period: {{period}}\n"
    "- Objective & Scope: The objective of this audit is to review and assess "
    "the entity's performance against the established controls.\n\n"
    "## Introduction and background\n\n{{introduction}}\n"
)


def test_a_source_documents_missing_scope_section_is_not_the_memo_disowning_its_own():
    """Observed on a treasury engagement, and the second time this shape has bitten.

    The memo remarked, accurately, that the policy extract it was given held only
    sections 4-8 — so the policy's *own* sections 1-3 ``(scope, definitions,
    governance) ... are not available``. Seventy-nine characters, and a whole-memo
    scan read it as the engagement disowning its scope. ``period`` was dropped
    from this gate for exactly this reason; scoping to where the template asks
    for the field fixes the remaining two without giving up the check.
    """

    memo = (
        "# Audit Planning Memorandum\n\n"
        "## Engagement\n\n"
        "- Entity: Meridian Bank Limited\n"
        "- Period: Half year ended 30 June 2025\n"
        "- **Objective & Scope:** Risk-based review of treasury dealing, "
        "confirmation and settlement.\n\n"
        "## Introduction and background\n\n"
        "The policy extract covers sections 4-8; sections 1-3 (scope, "
        "definitions, governance) and 9-11 are not available.\n"
    )
    result = WORKERS.execute(
        _request(_bundle(template=_SHIPPED_TEMPLATE)), _Gateway([memo])
    )
    assert "sections 1-3" in result.proposal["apm_markdown"]


def test_disowning_the_field_where_the_template_asks_for_it_still_fails():
    memo = (
        "# Audit Planning Memorandum\n\n"
        "## Engagement\n\n"
        "- Entity: Meridian Bank Limited\n"
        "- **Objective & Scope:** not available.\n\n"
        "## Introduction and background\n\nBackground.\n"
    )
    gateway = _Gateway([memo, memo])
    with pytest.raises(WorkerRunError, match="objective is unavailable"):
        WORKERS.execute(_request(_bundle(template=_SHIPPED_TEMPLATE)), gateway)


_DATED = {
    "tables": [
        {
            "table": "invoices",
            "rows": 118,
            "date_columns": [
                {"column": "invoice_date", "min": "2023-01-10", "max": "2025-07-30"}
            ],
            "numeric_columns": [{"column": "invoice_amount", "total": "3,103,467,230"}],
        }
    ],
    "total_rows": 118,
}


def test_prose_about_missing_evidence_does_not_disown_a_stated_period():
    """The false positive that discarded a complete, valid memorandum.

    The memo states its period in the Engagement section and, far below in the
    risk assessment, plans for evidence that may not have been retained. A scan
    for "period" within eighty characters of "not available" read the second as
    a retraction of the first and spent the whole repair allowance on it.
    """
    gateway = _Gateway(
        [
            "# Engagement\n\nPeriod: 1 January 2024 to 31 December 2024, proposed "
            "from INVOICE_DATE and marked for confirmation.\n\n"
            "# Scope\n\nCommitments. The audit establishes completeness of GRN "
            "entry in the audit period; if not available, the gap is escalated."
        ]
    )
    request = _request(
        _bundle(
            planning_context={"context": {"objective": "Assess approvals"}},
            population=_DATED,
        )
    )

    result = WORKERS.execute(request, gateway)

    assert len(gateway.calls) == 1
    assert "1 January 2024" in result.proposal["apm_markdown"]


def test_the_period_is_steered_by_the_prompt_rather_than_gated():
    """Reporting the period as unavailable is not a validation failure.

    Proposing one from the observed ranges is asked for in ``APM_SYSTEM``. It is
    not enforced here: the period is proposed rather than asserted, and an
    auditor corrects a wrong one in place — which does not justify discarding an
    otherwise complete draft, nor the model call that produced it.
    """
    gateway = _Gateway(
        ["# Engagement\n\nPeriod: not available.\n\n# Scope\n\nCommitments."]
    )
    request = _request(
        _bundle(
            planning_context={"context": {"objective": "Assess approvals"}},
            population=_DATED,
        )
    )

    result = WORKERS.execute(request, gateway)

    assert len(gateway.calls) == 1
    assert "not available" in result.proposal["apm_markdown"]


def test_a_declared_section_may_not_be_a_heading_with_nothing_under_it():
    gateway = _Gateway(
        [
            "# Engagement\n\nApprovals.\n\n# Fraud risk\n\n# Scope\n\nCommitments.",
            "# Engagement\n\nApprovals.\n\n"
            "# Fraud risk\n\nManagement override is presumed.\n\n"
            "# Scope\n\nCommitments.",
        ]
    )
    request = _request(
        _bundle(template="# Engagement\n\n# Fraud risk\n\n# Scope\n")
    )

    result = WORKERS.execute(request, gateway)

    assert result.repaired is True
    assert "'fraud risk' is present but has no content" in gateway.calls[1]["user"]


def test_a_section_answered_only_by_its_subsections_is_answered():
    gateway = _Gateway(
        [
            "# Engagement\n\nApprovals.\n\n"
            "# Fraud risk\n\n## Management override\n\nPresumed present.\n\n"
            "# Scope\n\nCommitments."
        ]
    )
    request = _request(
        _bundle(template="# Engagement\n\n# Fraud risk\n\n# Scope\n")
    )

    result = WORKERS.execute(request, gateway)

    assert result.repaired is False
    assert len(gateway.calls) == 1


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
                "action_execution",
            )
        )
        for name in imported
    )
    assert ".ws" not in source
    assert "load_workspace" not in source


# --------------------------------------------------------------------------- #
# The auditor's instruction reaches the turn (step 3)
# --------------------------------------------------------------------------- #
def test_the_apm_turn_is_told_what_the_auditor_asked_for():
    text = "Name the two subsidiaries in scope in every section that mentions scope."
    gateway = _Gateway(["# Engagement\n\nx\n\n# Scope\n\ny"])

    WORKERS.execute(_request(_bundle(instruction=text)), gateway)

    sent = json.loads(gateway.calls[0]["user"])
    assert sent["auditor_instruction"] == text
    # Promoted once, not also buried in the serialized bundle beneath it.
    assert gateway.calls[0]["user"].count(text) == 1


def test_an_apm_turn_with_no_instruction_carries_no_empty_key():
    gateway = _Gateway(["# Engagement\n\nx\n\n# Scope\n\ny"])

    WORKERS.execute(_request(), gateway)

    assert "auditor_instruction" not in json.loads(gateway.calls[0]["user"])


def test_the_apm_prompt_states_where_an_instruction_ranks():
    """Above the default instructions, below the response contract."""
    assert "auditor_instruction" in planning.APM_SYSTEM
    assert "never over the response contract" in planning.APM_SYSTEM


# --------------------------------------------------------------------------- #
# planning.delta_review — what new evidence changes (step 9)
# --------------------------------------------------------------------------- #
APM_WITH_HEADINGS = (
    "# Audit Planning Memorandum\n\n## Engagement\n\nProcurement.\n\n"
    "## Key risks and planned response\n\nApproval compliance.\n"
)


def delta_bundle(
    *, documents=("Policy now requires two approvers above 50,000.",),
    apm=APM_WITH_HEADINGS, rows=None, instruction=None,
):
    rows = [{"id": "RCM-1", "risk": "Purchases bypass approval"}] if rows is None else rows
    values = [
        ("new_document_analyses", f"document:doc{index}", content)
        for index, content in enumerate(documents)
    ]
    values.append(("current_apm", "planning:apm", apm))
    values.extend(("current_rcm", f"rcm:{row['id']}", row) for row in rows)
    values.append(
        (
            "planning_context",
            "planning:context",
            {"context": {"objective": "Assess procurement approvals"}},
        )
    )
    if instruction is not None:
        values.append(("instruction", "instruction:abcdef123456", instruction))
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
        capability_id="planning.change_assessed",
        unit_id="change_assessment",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def delta_request(bundle=None):
    return WorkerRequest(
        worker_id="planning.delta_review",
        capability_id="planning.change_assessed",
        unit_id="change_assessment",
        context=bundle or delta_bundle(),
        unit_input={"document_ids": ["doc0"], "basis_sha1": "basis"},
        activity={"artifact_refs": ["document:doc0"]},
    )


def _assessment(**overrides):
    return {
        "impact": "apm",
        "summary": "The policy raises the approval threshold.",
        "apm_changes": [
            {
                "section": "Key risks and planned response",
                "change": "Note the new threshold.",
                "reason": "The policy changed.",
                "citation": "doc0",
            }
        ],
        "rcm_changes": [],
        **overrides,
    }


def test_delta_worker_reads_only_its_bundle_and_returns_a_validated_assessment():
    gateway = _Gateway([json.dumps(_assessment())])

    result = WORKERS.execute(delta_request(), gateway)

    assert result.proposal["impact"] == "apm"
    assert result.proposal["apm_changes"][0]["section"] == "Key risks and planned response"
    sent = json.loads(gateway.calls[0]["user"])
    assert sent["new_documents"] == ["Policy now requires two approvers above 50,000."]
    assert sent["current_apm"] == APM_WITH_HEADINGS
    assert [row["id"] for row in sent["current_rcm"]] == ["RCM-1"]


def test_delta_worker_refuses_a_section_the_memorandum_does_not_have():
    with pytest.raises(WorkerResponseValidationError, match="does not have"):
        validate_delta_proposal(
            _assessment(
                apm_changes=[
                    {"section": "Data analytics performed", "change": "x", "reason": "y"}
                ]
            ),
            delta_request(),
        )


def test_delta_worker_refuses_a_row_the_matrix_does_not_have():
    with pytest.raises(WorkerResponseValidationError, match="does not have"):
        validate_delta_proposal(
            _assessment(
                impact="rcm",
                apm_changes=[],
                rcm_changes=[
                    {"rcm_id": "RCM-404", "change": "revise", "summary": "x", "reason": "y"}
                ],
            ),
            delta_request(),
        )


def test_delta_worker_refuses_an_impact_that_disagrees_with_its_own_lists():
    """"Nothing needs changing" with four changes attached is the failure."""

    with pytest.raises(WorkerResponseValidationError, match="does not agree"):
        validate_delta_proposal(_assessment(impact="none"), delta_request())
    with pytest.raises(WorkerResponseValidationError, match="does not agree"):
        validate_delta_proposal(
            _assessment(impact="both"), delta_request()
        )


def test_delta_worker_accepts_no_change_as_a_complete_answer():
    """Confirming the plan is a real answer, and the common one."""

    validated = validate_delta_proposal(
        {
            "impact": "none",
            "summary": "The policy restates what the memorandum already assumes.",
            "apm_changes": [],
            "rcm_changes": [],
        },
        delta_request(),
    )

    assert validated["impact"] == "none"
    assert validated["apm_changes"] == []


def test_delta_worker_reads_the_auditors_instruction_when_one_is_supplied():
    gateway = _Gateway([json.dumps(_assessment())])

    WORKERS.execute(
        delta_request(delta_bundle(instruction="Focus on approval thresholds.")),
        gateway,
    )

    sent = json.loads(gateway.calls[0]["user"])
    assert sent["auditor_instruction"] == "Focus on approval thresholds."
