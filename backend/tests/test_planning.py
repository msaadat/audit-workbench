import csv
import io
import threading
import time

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.agent import runner, store
from app.agent.workers import planning as planning_worker
from app.main import create_app
from app import doc_tests, document_analysis, documents, llm, methodology, templates_store, workspaces

from conftest import FakeAgentLLM, wait_run


PLANNING_RESPONSES = {
    "agent:document_context": {
        "context": {
            "scope": "Procurement policy governance",
            "background_notes": "Procurement approvals are governed by the disclosed policy.",
        }
    },
    "agent:apm": {
        "apm_markdown": (
            "# Audit Planning Memorandum\n\n"
            "## Engagement\nScope covers accounts payable.\n\n"
            "## Introduction and background\nPlanning background.\n\n"
            "## Process flow and understanding\nAccounts payable processing.\n\n"
            "## Prior audit findings\nNo information available.\n\n"
            "## Data analytics performed\nNo data analysis has been performed.\n\n"
            "## Fraud risk and management override\nManagement override is presumed.\n\n"
            "## Key risks and planned response\nTest duplicate payments."
            + "\n\n## Planning assumptions and matters reported\n- The approved policy version was not provided; its currency is assumed."
        )
    },
    "agent:rcm": {
        "rows": [
            {
                "operation": "create",
                "process": "Accounts payable",
                "risk": "Duplicate payments are processed",
                "risk_rating": "high",
                "control_attributes": [
                    {
                        "key": "duplicate_payment_prevention",
                        "assertion": "Operational",
                        "requirement": (
                            "Duplicate invoice validation operates before payment."
                        ),
                        "evidence_kind": "manual_inspection",
                    }
                ],
                "control": "Duplicate invoice validation",
                "control_type": "Automated preventive",
                "test_refs": [],
            }
        ]
    },
    "agent:test_generate": {
        "tests": [
            {
                "source": "document",
                "title": "Test duplicate payments",
                "objective": "Determine whether duplicate payments occurred",
                "steps": [
                    {
                        "label": "Approval",
                        "instruction": "Determine who approved the duplicate payment.",
                        "mode": "question",
                        "document_ids": [],
                        "question": "Who approved?",
                        "missing_evidence": "No approval documentation available.",
                    }
                ],
            }
        ]
    },
}


def configure_planning_llm(monkeypatch, overrides=None):
    responses = dict(PLANNING_RESPONSES)
    responses.update(overrides or {})
    fake = FakeAgentLLM(responses)
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    return fake


def start_planning(ws, mode="auto", context=None, *, generation_mode=None):
    return runner.start_command_run(
        ws,
        mode,
        {
            "source": "goal_template",
            "text": "Update planning using current engagement evidence.",
            "goal_template": "planning",
            **({"generation_mode": generation_mode} if generation_mode else {}),
        },
        context=context or {},
    )


def test_templates_default_override_and_reset():
    ws = workspaces.create_workspace("Planning templates")
    default = templates_store.get_template(ws, "apm")
    assert default["source"] == "default"
    assert "{{entity}}" in default["markdown"]

    override = templates_store.put_template(ws, "apm", "# Firm APM\n{{scope}}")
    assert override == {"name": "apm", "markdown": "# Firm APM\n{{scope}}", "source": "workspace"}
    reset = templates_store.put_template(ws, "apm", None, reset=True)
    assert reset["source"] == "default"


def test_template_sections_are_parsed_without_their_guidance():
    markdown = (
        "<!-- A preamble comment.\n\n## Not a heading, only guidance prose. -->\n\n"
        "# Finding\n\n"
        "## Condition\n\n<!-- section: What was found. -->\n\nObserved text.\n\n"
        "##   Root Cause  \n\n"
    )

    # Comments are stripped before headings are read, so guidance that happens
    # to contain a heading-shaped line cannot invent a section.
    assert templates_store.sections(markdown) == ["Condition", "Root Cause"]
    assert templates_store.section_bodies(markdown) == {
        "condition": "Observed text.",
        "root cause": "",
    }
    assert templates_store.scaffold(markdown) == "## Condition\n\n## Root Cause\n"


def test_planning_crud_and_user_touch():
    ws = workspaces.create_workspace("Planning CRUD")
    planning = ws.update_planning({"context": {"entity": "Example Co"}, "apm_markdown": "# Draft"})
    assert planning["context"]["entity"] == "Example Co"

    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue is incomplete", "agent_run_id": "run-1"})
    assert row["created_by"] == "agent"
    row = ws.update_rcm(row["id"], {"control": "Monthly reconciliation"})
    assert row["created_by"] == "user"
    procedure = ws.add_procedure({"objective": "Test revenue completeness", "rcm_refs": [row["id"]], "agent_run_id": "run-1"})
    assert ws.find_semantic("procedures", procedure["semantic_id"]) == procedure
    ws.remove_rcm(row["id"])
    assert ws.work_program[0]["rcm_refs"] == []


def test_unchanged_apm_save_preserves_agent_ownership():
    ws = workspaces.create_workspace("Planning provenance")
    ws.update_planning(
        {
            "apm_markdown": "# Agent draft",
            "created_by": "agent",
            "agent_run_id": "run-1",
        },
        agent=True,
    )

    unchanged = ws.update_planning(
        {
            "context": {"entity": "Example Co"},
            "apm_markdown": "# Agent draft",
        }
    )
    assert unchanged["created_by"] == "agent"
    assert unchanged["agent_run_id"] == "run-1"

    edited = ws.update_planning({"apm_markdown": "# Auditor revision"})
    assert edited["created_by"] == "user"


def test_planning_run_without_tables_and_user_safe_rerun(monkeypatch):
    fake = configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("No-table planning")
    first = start_planning(ws)
    completed = wait_run(ws, first["id"])
    ws = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert completed["schema_version"] == 3
    assert completed["kind"] == "audit"
    assert completed["actions"] == []
    assert ws.planning["apm_markdown"].startswith("# Audit Planning Memorandum")
    assert len(ws.rcm) == 1
    assert ws.work_program == []
    # A test is executable work, so a workspace with no table and no document
    # has nothing to draft against: the RCM lands, the tests wait for material.
    assert ws.rcm[0]["test_refs"] == []
    assert {call["tag"] for call in fake.calls} >= {"agent:apm", "agent:rcm"}
    assert "agent:test_generate" not in {call["tag"] for call in fake.calls}

    row_id = ws.rcm[0]["id"]
    ws.update_rcm(row_id, {"control": "Auditor-confirmed manual review"})
    second = start_planning(ws)
    wait_run(ws, second["id"])
    ws = workspaces.load_workspace(ws.id)
    assert len(ws.rcm) == 1
    assert ws.rcm[0]["control"] == "Auditor-confirmed manual review"


def test_planning_rerun_receives_and_updates_current_drafts(monkeypatch):
    configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Planning revisions")
    documents.add_document(
        ws, "Procurement Policy.txt", b"Purchases require documented approval.",
        category="policy",
    )
    ws = workspaces.load_workspace(ws.id)
    first = wait_run(ws, start_planning(ws)["id"])
    assert first["status"] == "completed"
    ws = workspaces.load_workspace(ws.id)
    row_id = ws.rcm[0]["id"]
    test_id = doc_tests.list_tests(ws)[0]["id"]

    def revised_apm(user):
        assert "CURRENT APM TO REVISE" in user
        assert "# Audit Planning Memorandum" in user
        return {"apm_markdown": PLANNING_RESPONSES["agent:apm"]["apm_markdown"] + "\n\nRevised."}

    def revised_rcm(user):
        assert "CURRENT RCM TO REVISE" in user and row_id in user
        return {
            "rows": [{
                "operation": "update", "rcm_id": row_id,
                "process": "Accounts payable", "risk": "Duplicate payments are processed",
                "risk_rating": "critical",
                "control_attributes": [
                    {
                        "key": "duplicate_payment_prevention",
                        "assertion": "Operational",
                        "requirement": (
                            "Duplicate invoice validation operates before payment."
                        ),
                        "evidence_kind": "manual_inspection",
                    }
                ],
                "control": "Duplicate invoice validation", "control_type": "Automated preventive",
            }]
        }

    def revised_tests(user):
        assert test_id in user
        return {
            "tests": [{
                "source": "document",
                "title": "Test duplicate payments",
                "objective": "Determine whether duplicate payments occurred",
                "steps": [
                    {
                        "label": "Approval",
                        "instruction": "Determine who approved the duplicate payment.",
                        "mode": "question",
                        "document_ids": [],
                        "question": "Who approved?",
                        "missing_evidence": "No approval documentation available.",
                    }
                ],
            }]
        }

    configure_planning_llm(monkeypatch, {
        "agent:apm": revised_apm, "agent:rcm": revised_rcm,
        "agent:test_generate": revised_tests,
    })
    completed = wait_run(
        ws,
        start_planning(ws, generation_mode="force")["id"],
    )
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert len(reloaded.rcm) == 1 and reloaded.rcm[0]["id"] == row_id
    assert reloaded.rcm[0]["risk_rating"] == "critical"
    assert len(doc_tests.list_tests(reloaded)) == 1
    assert doc_tests.list_tests(reloaded)[0]["id"] == test_id
    assert completed["planning_changes"]["rcm_updated"] == 1
    assert completed["planning_changes"]["test_updated"] == 1
    milestones = {
        item["capability"]: item for item in completed["milestones"]
    }
    assert {
        "planning.apm_ready",
        "planning.rcm_ready",
        "tests.specified",
    } <= set(milestones)
    assert milestones["planning.rcm_ready"]["headline"] == (
        "Risk and control matrix ready"
    )
    # Each milestone is handed off in the agent's own voice, so a long run reads
    # as work arriving piece by piece rather than as a block of cards at the end.
    #
    # What the memorandum hands off *to* moved again when the matrix stopped
    # authoring evidence contracts. It needs to know which record *kinds* the
    # engagement holds — hence the type classification between the two — and no
    # longer what fields they carry, so the expensive evidence read now follows
    # the matrix rather than standing between it and the memorandum.
    said = [item["content"] for item in completed["messages"] if item["role"] == "agent"]
    assert (
        "Audit planning memorandum is done — now working on document types."
        in said
    )
    assert (
        "Risk and control matrix is done — now working on evidence readings."
        in said
    )
    # The last stage hands off to nothing: the closing turn is the run's last word.
    assert not any(
        item.startswith("Executable test specifications is done") for item in said
    )


def test_planning_failure_preserves_valid_apm_and_rcm_checkpoints(monkeypatch):
    ws = workspaces.create_workspace("Atomic planning")
    # Seeded as agent-owned so the run reaches the draft stage instead of
    # stopping at the auditor-owned-APM review gate.
    ws.update_planning(
        {
            "context": {"objective": "Assess purchasing", "scope": "Original scope"},
            "apm_markdown": "# Original APM",
            "created_by": "agent",
            "agent_run_id": "seed",
        },
        agent=True,
    )
    ws.add_rcm({
        "process": "Purchasing", "risk": "Original risk", "risk_rating": "medium",
        "control": "Purchases require approval",
    })
    documents.add_document(
        ws, "Procurement Policy.txt", b"Purchases require documented approval.",
        category="policy",
    )
    ws = workspaces.load_workspace(ws.id)
    def disconnect(_user):
        raise llm.LLMError("test drafting disconnected")

    configure_planning_llm(monkeypatch, {
        "agent:test_generate": disconnect,
    })
    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "follow_up",
            "text": "Complete the missing tests.",
            "requested_outcomes": ["tests.specified"],
            "generation_mode": "reuse_existing",
        },
    )
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    units = [
        unit
        for stage in completed["workflow"]["stages"]
        for unit in stage["units"]
    ]

    # Partial, not failed — which is the same point the checkpoint assertions
    # below make: this run committed an APM and an RCM before it stopped.
    assert completed["status"] == "completed_with_failures"
    assert any(unit["kind"] == "test_generation" and unit["status"] == "failed" for unit in units)
    # The APM and RCM this run committed before the failure are checkpoints: a
    # later failed stage never rolls them back, and the auditor's own row
    # survives untouched alongside them.
    assert reloaded.planning["apm_markdown"].startswith("# Audit Planning Memorandum")
    assert any(row["control"] == "Purchases require approval" for row in reloaded.rcm)
    assert all(row["test_refs"] == [] for row in reloaded.rcm)


def test_planning_llm_failure_is_not_reported_as_completed(monkeypatch):
    ws = workspaces.create_workspace("Planning provider failure")

    def disconnect(_user):
        raise llm.LLMError("LLM request failed: Remote end closed connection without response")

    configure_planning_llm(monkeypatch, {"agent:apm": disconnect})
    completed = wait_run(ws, start_planning(ws)["id"])

    tasks = {
        task["id"]: task
        for stage in completed["plan"]["stages"]
        for task in stage["tasks"]
    }
    # The planning context unit settled before the APM call failed, so the run
    # is partial. What matters to this test is that it is never plain
    # "completed" and still carries the provider error.
    assert completed["status"] == "completed_with_failures"
    assert completed["command"]["status"] == "completed_with_failures"
    assert completed["error"] == "LLM request failed: Remote end closed connection without response"
    assert "Failed or conflicting units: 1" in completed["summary_markdown"]
    assert tasks["planning:context"]["status"] == "completed"
    assert any(
        unit["kind"] == "apm" and unit["status"] == "failed"
        for stage in completed["workflow"]["stages"]
        for unit in stage["units"]
    )
    assert "planning:rcm" not in tasks


def test_planning_repairs_a_malformed_test_plan(monkeypatch):
    attempts = 0

    def drafted_tests(_user):
        nonlocal attempts
        attempts += 1
        base = {
            "source": "document",
            "title": "Test duplicate payments",
            "objective": "Determine whether duplicate payments occurred",
            "steps": [
                {
                    "label": "Approval",
                    "instruction": "Determine who approved the duplicate payment.",
                    "mode": "question",
                    "document_ids": [],
                    "question": "Who approved?",
                    "missing_evidence": "No approval documentation available.",
                }
            ],
        }
        if attempts == 1:
            # A source the workspace cannot supply is decidable from the bundle:
            # no table is available in this document-only workspace.
            return {"tests": [{**base, "source": "data"}]}
        return {"tests": [base]}

    fake = configure_planning_llm(monkeypatch, {"agent:test_generate": drafted_tests})
    ws = workspaces.create_workspace("Repair test plan")
    documents.add_document(
        ws, "Procurement Policy.txt", b"Purchases require documented approval.",
        category="policy",
    )
    ws = workspaces.load_workspace(ws.id)
    completed = wait_run(ws, start_planning(ws)["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert attempts == 2
    assert [call["tag"] for call in fake.calls].count("agent:test_generate") == 2
    assert len(doc_tests.list_tests(reloaded)) == 1


def test_planning_context_response_schema_requires_string_field_values():
    # The response contract moved from the runner to the registered
    # ``planning.context`` worker's response schema (P7A.2).
    with pytest.raises(ValueError, match="context.key_contacts must be a string"):
        planning_worker._planning_context_response_schema(
            '{"context": {"key_contacts": ["CFO", "Head of Procurement"]}}'
        )


def test_planning_context_response_schema_normalizes_flat_provider_payload():
    normalized = planning_worker._planning_context_response_schema(
        '{"objective": "Review procurement controls",'
        ' "scope": "Requisitions through payment", "entity": "Example Bank"}'
    )

    assert normalized["context"] == {
        "objective": "Review procurement controls",
        "scope": "Requisitions through payment",
        "entity": "Example Bank",
    }


def test_auto_planning_selects_planning_relevant_documents_deterministically(
    monkeypatch,
):
    # P7A.2: which documents ground planning context is a declared deterministic
    # category rule, not a model turn. The synthesis worker then sees the bounded
    # document material and only that.
    ws = workspaces.create_workspace("Auto document selection")
    policy_text = b"Procurement Policy: purchases require documented approval before commitment."
    policy = documents.add_document(ws, "Procurement Policy.txt", policy_text, category="policy")
    voucher = documents.add_document(
        ws,
        "Invoice 42.txt",
        b"Invoice 42 was paid on 3 March for stationery supplied by Acme Limited.",
        category="evidence",
    )

    fake = configure_planning_llm(monkeypatch)
    started = start_planning(ws)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["context"]["scope"] == "Procurement policy governance"
    # No model turn selects documents: relevance is a declared category rule.
    call_tags = [call["tag"] for call in fake.calls]
    assert "agent:document_selection" not in call_tags
    # The planning-relevant policy is analyzed first, as the declared
    # ``documents.analysis_generated`` dependency of planning context; the
    # voucher is excluded from the audit workflow's planning scope.
    assert "agent:document_analysis_map" in call_tags
    assert document_analysis.compact_artifact(reloaded, policy["id"]) is not None
    assert document_analysis.compact_artifact(reloaded, voucher["id"]) is None
    # Synthesis is grounded in the generated analysis of the policy, not in the
    # voucher and not in a document nobody analyzed.
    context_call = next(call for call in fake.calls if call["tag"] == "agent:document_context")
    supplied = context_call["messages"][-1]["content"]
    assert "Invoice 42 was paid" not in supplied
    # Provenance records the document that was actually supplied. The category
    # rule governs the planning stages; test generation is deliberately not
    # planning-relevant filtered (merge plan section 7), since a Document Test
    # must be able to vouch to a transaction voucher, so the exclusion is
    # asserted where the rule actually applies.
    activity = documents.activities(reloaded, limit=250)["items"]
    planning_stages = {
        "agent:document_context", "agent:apm", "agent:rcm",
    }
    planning_activity = [
        item for item in activity if item.get("stage") in planning_stages
    ]
    assert any(policy["id"] in item.get("document_ids", []) for item in planning_activity)
    # The voucher now appears in planning-stage provenance as a *listing* — its
    # name and category — because it has extracted text at all, which it did not
    # before: text extraction was bounded by the planning scope, so an audit run
    # never read a voucher and it could not be a candidate for anything. That
    # bound is what left schemas_induced satisfied having induced nothing.
    #
    # The guarantee is about content, not candidacy, so it is asserted on what
    # every planning stage was actually shown.
    for call in fake.calls:
        if call["tag"] in planning_stages:
            assert "Invoice 42 was paid" not in call["messages"][-1]["content"]


def test_planning_recovers_labelled_context_when_synthesis_returns_empty(monkeypatch):
    ws = workspaces.create_workspace("Planning context fallback")
    policy = documents.add_document(
        ws, "Procurement Policy.txt", b"Procurement approvals require documented authorization.",
        category="policy",
    )
    extracted = documents.extract_document(ws, policy["id"])
    document_analysis.persist_analysis(
        ws,
        policy,
        extracted,
        {
            "summary_markdown": (
                "- **Objective:** Review procurement approvals\n"
                "- **Scope:** Procurement authorization controls"
            ),
            "audit_notes_markdown": "",
            "citations": [],
        },
        provider="fake",
        model="fake",
    )
    fake = configure_planning_llm(monkeypatch, {"agent:document_context": {}})

    completed = wait_run(
        ws,
        start_planning(ws, context={"document_ids": [policy["id"]]})["id"],
    )
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["context"]["objective"] == "Review procurement approvals"
    assert reloaded.planning["context"]["scope"] == "Procurement authorization controls"
    # The labelled facts already in the supplied summary are the better answer, so
    # the worker recovers them instead of paying for a second synthesis turn.
    assert [call["tag"] for call in fake.calls].count("agent:document_context") == 1
    assert any("recovered labelled facts" in warning for warning in completed["warnings"])


def test_planning_generates_document_analyses_as_a_declared_dependency(monkeypatch):
    # P9.9: planning-context synthesis declares ``documents.analysis_generated``
    # as a scoped dependency, so the scheduler generates the analyses first
    # through the one document implementation. Planning itself still never runs a
    # map/reduce of its own.
    ws = workspaces.create_workspace("Planning with document analysis")
    selected = [
        documents.add_document(
            ws,
            f"Policy {index}.txt",
            f"Policy {index}: documented approval is required before procurement commitment."
            .encode(),
            category="policy",
        )
        for index in range(1, 4)
    ]
    fake = configure_planning_llm(monkeypatch)

    completed = wait_run(
        ws,
        start_planning(
            ws, context={"document_ids": [item["id"] for item in selected]}
        )["id"],
    )
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    # One map turn per document, and each document carries a durable analysis.
    assert [call["tag"] for call in fake.calls].count("agent:document_analysis_map") == 3
    assert all(
        document_analysis.compact_artifact(reloaded, item["id"]) is not None
        for item in selected
    )
    # Synthesis then consumed those generated analyses rather than raw text.
    context_call = next(call for call in fake.calls if call["tag"] == "agent:document_context")
    supplied = context_call["messages"][-1]["content"]
    assert "The requirements need operating evidence" in supplied
    assert reloaded.planning["context"]["scope"] == "Procurement policy governance"


def test_apm_accepts_malformed_legacy_json_wrapper_without_retry(monkeypatch):
    fake = configure_planning_llm(monkeypatch)
    valid_apm = PLANNING_RESPONSES["agent:apm"]["apm_markdown"]
    apm_calls = []

    def chat(messages, tools=None, temperature=0.0, profile="assistant", on_delta=None,
             tool_choice=None, response_format=None):
        system = messages[0]["content"]
        tag = system[1 : system.index("]")] if system.startswith("[") else ""
        if tag == "agent:apm":
            apm_calls.append(messages)
            # Literal newlines make this invalid JSON, but the Markdown itself
            # is complete and should not cost another provider turn.
            return {"content": '{"apm_markdown":"' + valid_apm + '"}'}
        return fake(
            messages,
            tools=tools,
            temperature=temperature,
            profile=profile,
            on_delta=on_delta,
        )

    monkeypatch.setattr(llm, "chat", chat)
    ws = workspaces.create_workspace("Direct Markdown APM")
    completed = wait_run(ws, start_planning(ws)["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["apm_markdown"] == valid_apm
    assert len(apm_calls) == 1
    # The instruction matters, not where the prompt happens to wrap: assert on
    # the normalized text so reflowing the system prompt cannot fail this.
    prompt = " ".join(apm_calls[0][0]["content"].casefold().split())
    assert "return the memorandum as markdown only" in prompt


def test_permission_planning_uses_three_editable_approval_gates(monkeypatch):
    configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Permission planning")
    started = start_planning(ws, "permission")
    seen = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run = store.load_run(ws, started["id"])
        if run["status"] == "awaiting_approval":
            pending = next(item for item in run["approvals"] if item["status"] == "pending")
            if pending["id"] not in seen:
                seen.append(pending["id"])
                runner.resolve_approval(
                    ws,
                    run["id"],
                    pending["id"],
                    [{"item_id": item["id"], "action": "approve"} for item in pending["items"]],
                )
        elif run["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    completed = wait_run(ws, started["id"])
    assert completed["status"] == "completed"
    assert [approval["kind"] for approval in completed["approvals"]] == ["apm", "rcm"]


def test_apm_unfilled_placeholders_become_not_available_notes(monkeypatch):
    configure_planning_llm(monkeypatch, {
        "agent:apm": {
            "apm_markdown": (
                "# Audit Planning Memorandum\n\n"
                "## Engagement\n- Entity: {{entity}}\n- Period: {{period}}\n\n"
                "## Introduction and background\n{{introduction}}\n\n"
                "## Process flow and understanding\n{{process_flow}}\n\n"
                "## Prior audit findings\n{{prior_audit_findings}}\n\n"
                "## Data analytics performed\n{{data_analytics}}\n\n"
                "## Fraud risk and management override\n{{fraud_risk}}\n\n"
                "## Key risks and planned response\n{{key_risks}}"
                + "\n\n## Planning assumptions and matters reported\n- The approved policy version was not provided; its currency is assumed."
            )
        },
    })
    ws = workspaces.create_workspace("Placeholder planning")
    started = start_planning(ws)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    apm = reloaded.planning["apm_markdown"]
    assert "{{" not in apm and "}}" not in apm
    assert "_[entity - context not available]_" in apm
    assert "_[prior audit findings - context not available]_" in apm


def test_permission_planning_confirms_the_synthesized_planning_context(monkeypatch):
    ws = workspaces.create_workspace("Permission planning context")
    policy = documents.add_document(
        ws, "Procurement Policy.txt",
        b"Procurement Policy: purchases require documented approval.", category="policy",
    )
    configure_planning_llm(monkeypatch)
    started = start_planning(ws, "permission")
    kinds, seen = [], []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run = store.load_run(ws, started["id"])
        if run["status"] == "awaiting_approval":
            pending = next(item for item in run["approvals"] if item["status"] == "pending")
            if pending["id"] not in seen:
                seen.append(pending["id"])
                kinds.append(pending["kind"])
                runner.resolve_approval(
                    ws, run["id"], pending["id"],
                    [{"item_id": item["id"], "action": "approve"} for item in pending["items"]],
                )
        elif run["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)
    assert completed["status"] == "completed"
    # Documents are chosen by a declared deterministic rule, so the first thing
    # the auditor approves is the synthesized context itself.
    assert kinds[0] == "context"
    assert reloaded.planning["context"]["scope"] == "Procurement policy governance"
    assert any(policy["id"] in item.get("document_ids", []) for item in documents.activities(reloaded, limit=250)["items"])


def test_planning_routes():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "Planning API"}).json()
    base = f"/api/workspaces/{created['id']}"

    response = client.patch(f"{base}/planning", json={"context": {"scope": "Head office"}})
    assert response.status_code == 200
    assert response.json()["context"]["scope"] == "Head office"
    assert "status" not in response.json()
    assert client.patch(f"{base}/planning", json={"status": "final"}).status_code == 400
    row = client.post(f"{base}/rcm", json={"process": "Cash", "risk": "Cash is misstated"}).json()
    assert client.patch(f"{base}/rcm/{row['id']}", json={"risk_rating": "high"}).json()["risk_rating"] == "high"
    legacy = client.post(f"{base}/procedures", json={"objective": "Test cash", "rcm_refs": [row["id"]]})
    assert legacy.status_code == 400
    assert client.post(
        f"{base}/rcm/{row['id']}/planned-tests",
        json={"title": "Test cash"},
    ).status_code == 405
    created = client.post(
        f"{base}/doc-tests",
        json={
            "kind": "qa", "title": "Test cash", "objective": "Test cash",
            "rcm_id": row["id"], "items": [],
        },
    ).json()
    payload = client.get(f"{base}/planning").json()
    assert payload["rcm"][0]["id"] == row["id"]
    assert payload["rcm"][0]["test_refs"] == [f"doctest:{created['id']}"]
    assert payload["procedures"] == []
    template = client.put(f"{base}/templates/apm", json={"markdown": "# Custom"})
    assert template.json()["source"] == "workspace"


def test_planning_cites_methodology_pack(monkeypatch):
    configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Methodology planning")
    documents.add_document(
        ws, "Procurement Policy.txt", b"Purchases require documented approval.",
        category="policy",
    )
    ws = workspaces.load_workspace(ws.id)
    methodology.save_pack(
        ws,
        "Firm AP Guide",
        "# Duplicate payments\nAudit procedures should address duplicate-payment risk and preventive controls.",
    )
    started = start_planning(ws)
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)
    assert completed["status"] == "completed"
    drafted = doc_tests.load_test(reloaded, doc_tests.list_tests(reloaded)[0]["id"])
    refs = drafted["methodology_refs"]
    assert refs[0]["pack_name"] == "Firm AP Guide"
    assert refs[0]["section"] == "Duplicate payments"
    activity = documents.activities(reloaded)["items"]
    assert any(item.get("artifact_ref") == "planning:apm" for item in activity)
    apm_call = next(item for item in activity if item.get("stage") == "agent:apm")
    assert apm_call["template_versions"][0]["name"] == "apm"
    assert refs[0]["sha1"] == methodology.list_packs(reloaded)[0]["sha1"]


def test_planning_update_includes_selected_imported_documents(monkeypatch):
    fake = configure_planning_llm(monkeypatch)
    ws = workspaces.create_workspace("Policy planning update")
    policy_text = b"Procurement Policy: purchases require documented approval before commitment."
    policy = documents.add_document(ws, "Procurement Policy.txt", policy_text, category="policy")

    started = start_planning(ws, context={"document_ids": [policy["id"]]})
    completed = wait_run(ws, started["id"])
    reloaded = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert reloaded.planning["context"]["scope"] == "Procurement policy governance"
    # P9.9: the selected document is analyzed first, so synthesis is grounded in
    # its generated summary rather than in raw extracted text.
    context_call = next(call for call in fake.calls if call["tag"] == "agent:document_context")
    supplied = context_call["messages"][-1]["content"]
    assert document_analysis.compact_artifact(reloaded, policy["id"]) is not None
    assert "The requirements need operating evidence" in supplied
    activity = documents.activities(reloaded, limit=250)["items"]
    assert any(policy["id"] in item.get("document_ids", []) for item in activity)


def _rcm_import_csv(rows: list[dict]) -> bytes:
    columns = [
        "id", "process", "risk", "risk_rating", "assertion", "control",
        "control_type", "control_owner", "criteria", "prepared_by",
        "review_status",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, "") for col in columns})
    return buffer.getvalue().encode()


def test_apm_export_import_routes():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "APM Export"}).json()
    base = f"/api/workspaces/{created['id']}"
    client.patch(f"{base}/planning", json={"apm_markdown": "# Draft APM\nSome content."})

    export = client.get(f"{base}/planning/apm/export")
    assert export.status_code == 200
    assert export.text == "# Draft APM\nSome content."
    assert "attachment" in export.headers["content-disposition"]

    imported = client.post(
        f"{base}/planning/apm/import",
        files={"file": ("APM.md", b"# Edited APM\nNew content.", "text/markdown")},
    )
    assert imported.status_code == 200
    assert imported.json()["apm_markdown"] == "# Edited APM\nNew content."
    assert imported.json()["created_by"] == "user"


def test_rcm_export_import_round_trip():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "RCM Export"}).json()
    base = f"/api/workspaces/{created['id']}"
    row = client.post(
        f"{base}/rcm",
        json={"process": "Cash", "risk": "Cash is misstated", "risk_rating": "medium"},
    ).json()

    export = client.get(f"{base}/rcm/export")
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    df = pl.read_excel(io.BytesIO(export.content))
    assert df["id"][0] == row["id"]
    assert df["process"][0] == "Cash"

    content = _rcm_import_csv([{
        "id": row["id"], "process": "Cash", "risk": "Cash is misstated",
        "risk_rating": "high", "assertion": "Existence",
        "control": "Bank reconciliation", "control_type": "Manual detective",
        "control_owner": "Controller", "review_status": "reviewed",
    }])
    imported = client.post(f"{base}/rcm/import", files={"file": ("RCM.csv", content, "text/csv")})
    assert imported.status_code == 200
    body = imported.json()
    assert body["updated"] == 1
    assert body["unmatched"] == []
    updated_row = next(item for item in body["rcm"] if item["id"] == row["id"])
    assert updated_row["risk_rating"] == "high"
    assert updated_row["control"] == "Bank reconciliation"
    assert updated_row["review_status"] == "reviewed"
    assert updated_row["created_by"] == "user"


def test_rcm_import_does_not_add_or_remove_rows():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "RCM Import Unmatched"}).json()
    base = f"/api/workspaces/{created['id']}"
    row = client.post(f"{base}/rcm", json={"process": "Cash", "risk": "Cash is misstated"}).json()

    content = _rcm_import_csv([
        {"id": row["id"], "process": "Cash", "risk": "Cash is misstated", "risk_rating": "high"},
        {"id": "RCM-UNKNOWN", "process": "Ghost", "risk": "Ghost risk", "risk_rating": "medium"},
    ])
    imported = client.post(f"{base}/rcm/import", files={"file": ("RCM.csv", content, "text/csv")})
    assert imported.status_code == 200
    body = imported.json()
    assert body["updated"] == 1
    assert body["unmatched"] == ["RCM-UNKNOWN"]
    assert len(body["rcm"]) == 1
    assert body["rcm"][0]["risk_rating"] == "high"


def test_rcm_import_rejects_invalid_enum_values():
    client = TestClient(create_app())
    created = client.post("/api/workspaces", json={"name": "RCM Import Invalid"}).json()
    base = f"/api/workspaces/{created['id']}"
    row = client.post(f"{base}/rcm", json={"process": "Cash", "risk": "Cash is misstated"}).json()

    content = _rcm_import_csv([
        {"id": row["id"], "process": "Cash", "risk": "Cash is misstated", "risk_rating": "severe"},
    ])
    imported = client.post(f"{base}/rcm/import", files={"file": ("RCM.csv", content, "text/csv")})
    assert imported.status_code == 400

    # Sign-off is two states, and storing an unrecognised one as `draft` would
    # hide what the auditor meant by it.
    content = _rcm_import_csv([
        {"id": row["id"], "process": "Cash", "risk": "Cash is misstated", "review_status": "signed"},
    ])
    imported = client.post(f"{base}/rcm/import", files={"file": ("RCM.csv", content, "text/csv")})
    assert imported.status_code == 400

    unchanged = client.get(f"{base}/rcm").json()["items"][0]
    assert unchanged["risk_rating"] == "medium"
    assert unchanged["review_status"] == "draft"
