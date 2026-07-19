import asyncio
import threading
import time

import httpx
import polars as pl
import pytest

from app import dashboard, data_tests, doc_tests, document_analysis, documents, llm, rcm_execution, report, working_papers, workspaces
from app.agent import audit_capabilities, context_bundles, runner, store, workflow
from app.agent.workflow_runner import initialize_known_workflow
from app.main import create_app
from app.workspace_transactions import (
    ParentConflict,
    mutate,
    parent_hashes,
    prepare_linked_write,
    recover_linked_writes,
)
from conftest import FakeAgentLLM, wait_run


def _planning_workspace(name: str = "Workflow planning") -> workspaces.Workspace:
    ws = workspaces.create_workspace(name)
    ws.update_planning(
        {
            "context": {"objective": "Assess payments", "scope": "Accounts payable"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nAccounts payable.",
        }
    )
    ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate payments",
            "control": "Invoice duplicate check",
            "risk_rating": "high",
        }
    )
    return ws


def test_full_template_materializes_locally_without_command_interpreter():
    ws = workspaces.create_workspace("Local full route")
    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "goal_template",
            "text": "Do the full audit",
            "goal_template": "full_audit_working_draft",
        },
    )

    assert initialize_known_workflow(ws, run) is True
    persisted = store.load_run(ws, run["id"])
    assert persisted["schema_version"] == 3
    assert persisted["workflow"]["requested_outcomes"] == audit_capabilities.FULL_AUDIT_OUTCOMES
    assert persisted["workflow"]["resolved_capabilities"] == audit_capabilities.REGISTRY.closure(
        audit_capabilities.FULL_AUDIT_OUTCOMES
    )
    assert persisted["usage"]["llm_turns"] == 0


def test_partial_goal_prunes_current_prerequisites():
    ws = _planning_workspace()
    resolved, stages, reused = workflow.materialize(
        audit_capabilities.REGISTRY,
        ws,
        ["planning.planned_tests_ready"],
        {"target_refs": ["workspace:current"], "refresh_policy": "missing_or_stale"},
    )

    assert resolved == [
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "planning.planned_tests_ready",
    ]
    assert reused == resolved[:-1]
    assert [stage["capability"] for stage in stages] == ["planning.planned_tests_ready"]
    assert len(stages[0]["units"]) == 1


def test_router_bundle_is_small_and_excludes_domain_catalogs():
    state = {
        capability.id: capability.readiness(workspaces.create_workspace(f"Router {index}"), {}).payload()
        for index, capability in enumerate(audit_capabilities.REGISTRY.all())
    }
    bundle = context_bundles.command_router(
        {"text": "Complete the RCM testing procedures", "context_refs": []},
        state,
        [capability.id for capability in audit_capabilities.REGISTRY.all()],
        permission_mode="permission",
    )
    serialized = bundle.serialized()

    assert bundle.total_characters <= 6_000
    assert "analytics_registry" not in serialized
    assert "validation_registry" not in serialized
    assert "column_profiles" not in serialized
    assert "artifact_index" not in serialized


def test_transaction_merges_unrelated_revision_and_rejects_parent_change():
    ws = _planning_workspace("Transaction merge")
    rcm_id = ws.rcm[0]["id"]
    expected = parent_hashes(ws, [f"rcm:{rcm_id}"])
    old_revision = ws.revision

    other = workspaces.load_workspace(ws.id)
    other.update_planning({"context": {"materiality": "100,000"}})

    result = mutate(
        ws,
        lambda fresh: fresh.update_planning({"context": {"period": "FY2026"}}),
        expected_revision=old_revision,
        expected_parents=expected,
    )
    assert result.workspace.planning["context"]["materiality"] == "100,000"
    assert result.workspace.planning["context"]["period"] == "FY2026"

    changed_parent = workspaces.load_workspace(ws.id)
    changed_parent.update_rcm(rcm_id, {"control": "Auditor-edited duplicate check"})
    with pytest.raises(ParentConflict):
        mutate(
            result.workspace,
            lambda fresh: fresh.update_planning({"context": {"entity": "Example Co"}}),
            expected_parents=expected,
        )


def test_api_revision_headers_accept_current_write_and_reject_stale_write():
    ws = workspaces.create_workspace("Revision API")
    ws.add_table("transactions.csv", b"invoice,amount\n1,10\n")

    async def scenario():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get(f"/api/workspaces/{ws.id}")
            revision = first.headers["x-workspace-revision"]
            renamed = await client.patch(
                f"/api/workspaces/{ws.id}/tables/transactions",
                headers={"If-Match": f'"rev-{revision}"'},
                json={"name": "ledger"},
            )
            stale = await client.patch(
                f"/api/workspaces/{ws.id}/tables/ledger",
                headers={"If-Match": f'"rev-{revision}"'},
                json={"name": "ledger_again"},
            )
        return first, renamed, stale

    first, renamed, stale = asyncio.run(scenario())

    assert first.status_code == 200
    assert renamed.status_code == 200
    assert int(renamed.headers["x-workspace-revision"]) > int(
        first.headers["x-workspace-revision"]
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "workspace_revision_conflict"
    assert stale.json()["current_revision"] == int(
        renamed.headers["x-workspace-revision"]
    )


def test_planning_and_engagement_status_reads_do_not_advance_revision():
    ws = _planning_workspace("Pure planning reads")
    starting_revision = workspaces.load_workspace(ws.id).revision

    async def scenario():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status, planning = await asyncio.gather(
                client.get(f"/api/workspaces/{ws.id}/dashboard/status"),
                client.get(f"/api/workspaces/{ws.id}/planning"),
            )
            first_rollup = await client.post(f"/api/workspaces/{ws.id}/rcm/rollup")
            second_rollup = await client.post(f"/api/workspaces/{ws.id}/rcm/rollup")
        return status, planning, first_rollup, second_rollup

    status, planning, first_rollup, second_rollup = asyncio.run(scenario())

    assert status.status_code == 200
    assert planning.status_code == 200
    assert int(status.headers["x-workspace-revision"]) == starting_revision
    assert int(planning.headers["x-workspace-revision"]) == starting_revision
    assert int(first_rollup.headers["x-workspace-revision"]) == starting_revision + 1
    assert second_rollup.headers["x-workspace-revision"] == first_rollup.headers[
        "x-workspace-revision"
    ]
    assert workspaces.load_workspace(ws.id).revision == starting_revision + 1


def test_planning_context_commit_merges_unrelated_concurrent_write(monkeypatch):
    ws = workspaces.create_workspace("Concurrent planning context")
    policy = documents.add_document(
        ws,
        "Procurement Policy.txt",
        b"Procurement approvals are required before a purchase commitment.",
        category="policy",
    )
    extracted = documents.extract_document(ws, policy["id"])
    document_analysis.persist_analysis(
        ws,
        policy,
        extracted,
        {
            "summary_markdown": "The policy requires approval before commitment.",
            "audit_notes_markdown": "Use the approval requirement as planning criteria.",
            "citations": [],
        },
        provider="fake",
        model="fake",
    )
    interleaved = False

    def context_response(_user):
        nonlocal interleaved
        if not interleaved:
            concurrent = workspaces.load_workspace(ws.id)
            concurrent.add_table("concurrent.csv", b"id,amount\n1,10\n")
            interleaved = True
        return {
            "context": {
                "objective": "Assess procurement approval compliance",
                "scope": "Purchase commitments and approvals",
            }
        }

    fake = FakeAgentLLM({"agent:document_context": context_response})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {
            "configured": True,
            "backend": "fake",
            "provider": "fake",
            "model": "fake",
        },
    )

    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Update planning context",
            "requested_outcomes": ["planning.context_ready"],
        },
        context={"document_ids": [policy["id"]]},
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert current.planning["context"]["objective"] == (
        "Assess procurement approval compliance"
    )
    assert "concurrent" in current.table_names()
    assert not completed.get("error")


def test_all_settled_is_bounded_failure_isolated_and_stably_ordered():
    units = [
        {"id": f"unit-{index}"}
        for index in range(6)
    ]
    gate = threading.Barrier(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(unit):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if unit["id"] in {"unit-0", "unit-1"}:
                gate.wait(timeout=2)
            time.sleep(0.01)
            if unit["id"] == "unit-3":
                raise RuntimeError("isolated")
            return unit["id"]
        finally:
            with lock:
                active -= 1

    settled = workflow.stable_all_settled(units, worker, max_workers=2)

    assert peak == 2
    assert [unit["id"] for unit, _value, _error in settled] == [unit["id"] for unit in units]
    assert sum(error is not None for _unit, _value, error in settled) == 1
    assert [value for _unit, value, error in settled if error is None] == [
        "unit-0", "unit-1", "unit-2", "unit-4", "unit-5"
    ]


def test_data_test_compute_is_pure_and_commit_is_revisioned():
    ws = workspaces.create_workspace("Pure Data Test")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1, 2], "amount": [10.0, 10.0, 20.0]}).write_csv().encode(),
    )
    item = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify duplicates",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice"]}},
        },
    )
    before_revision = ws.revision

    candidate = data_tests.compute(ws, item["id"])
    assert ws.revision == before_revision
    assert data_tests._record(ws, item["id"])["last_run"] is None

    committed = data_tests.commit_result(ws, item["id"], candidate)
    assert ws.revision > before_revision
    assert data_tests.load_result(ws, item["id"], committed["id"])["result_sha1"] == committed["result_sha1"]


def test_document_test_linked_write_rolls_back_if_workspace_commit_fails(monkeypatch):
    ws = _planning_workspace("Document Test transaction")
    row = ws.rcm[0]
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Inspect approval",
            "objective": "Determine whether approval exists",
            "criteria": "Approval is documented.",
            "steps": ["Inspect evidence."],
            "method": "document_inspection",
            "expected_evidence": "Approval record",
        },
    )
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval inspection",
            "kind": "review",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "items": [{"label": "Approval", "summary": ""}],
        },
    )
    before_revision = ws.revision
    before_bytes = doc_tests._test_path(ws, test["id"]).read_bytes()

    def fail_save(_self, *args, **kwargs):
        raise RuntimeError("simulated workspace commit failure")

    monkeypatch.setattr(workspaces.Workspace, "save", fail_save)
    with pytest.raises(RuntimeError, match="simulated workspace commit failure"):
        doc_tests.update_test(ws, test["id"], {"title": "Uncommitted title"})

    assert doc_tests._test_path(ws, test["id"]).read_bytes() == before_bytes
    assert ws.revision == before_revision
    assert not list((ws.root / ".Transactions").glob("txn-*.json"))


def test_linked_write_recovery_restores_uncommitted_sidecar():
    ws = _planning_workspace("Linked write recovery")
    row = ws.rcm[0]
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Inspect evidence",
            "objective": "Determine whether evidence exists",
            "criteria": "Evidence is retained.",
            "steps": ["Inspect the record."],
            "method": "inspection",
            "expected_evidence": "Supporting record",
        },
    )
    test = doc_tests.create_test(
        ws,
        {
            "title": "Evidence review",
            "kind": "review",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "items": [{"label": "Evidence", "summary": "Pending review"}],
        },
    )
    path = doc_tests._test_path(ws, test["id"])
    uncommitted = {**test, "title": "Uncommitted sidecar"}
    prepare_linked_write(ws, path, uncommitted)
    workspaces.write_json_atomic(path, uncommitted)

    recover_linked_writes(ws.root)

    assert doc_tests.load_test(ws, test["id"])["title"] == "Evidence review"
    assert not list((ws.root / ".Transactions").glob("txn-*.json"))


def test_document_qa_expands_per_document_and_merges_in_attachment_order():
    ws = _planning_workspace("Q&A units")
    row = ws.rcm[0]
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Read approval evidence",
            "objective": "Determine whether approvals were documented",
            "criteria": "Approval is present.",
            "steps": ["Ask the evidence question."],
            "method": "inquiry",
            "expected_evidence": "Approval records",
        },
    )
    first = documents.add_document(ws, "First.txt", b"First approval was documented.")
    second = documents.add_document(ws, "Second.txt", b"Second approval was documented.")
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval Q&A",
            "kind": "qa",
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
            "items": [
                {
                    "label": "Was approval documented?",
                    "question": "Was approval documented?",
                    "document_ids": [first["id"], second["id"]],
                }
            ],
        },
    )
    item_id = test["items"][0]["id"]
    units = audit_capabilities.REGISTRY.get("fieldwork.executed").expand_units(
        ws, {"target_refs": [f"rcm:{row['id']}"]}
    )

    assert [unit.kind for unit in units] == [
        "document_qa_execution", "document_qa_execution"
    ]
    assert {unit.parent_refs[-1] for unit in units} == {
        f"document:{first['id']}", f"document:{second['id']}"
    }

    doc_tests.commit_qa_answer(
        ws, test["id"], item_id, second["id"], {"answer": "Second", "citations": []}
    )
    merged = doc_tests.commit_qa_answer(
        ws, test["id"], item_id, first["id"], {"answer": "First", "citations": []}
    )
    assert merged["response"] == "First\n\nSecond"
    assert merged["auditor_disposition"] == "pending"


def test_output_readiness_detects_changed_parents():
    ws = _planning_workspace("Output invalidation")
    row_id = ws.rcm[0]["id"]
    working_papers.generate_rcm(ws, row_id)
    dashboard.curate_rcm_tiles(ws, run_id="run-output")
    report.generate(ws, use_model=False, run_id="run-output")

    assert audit_capabilities.REGISTRY.get("working_papers.generated").readiness(
        ws, {}
    ).state == "satisfied"
    assert audit_capabilities.REGISTRY.get("dashboard.curated").readiness(
        ws, {}
    ).state == "satisfied"
    assert audit_capabilities.REGISTRY.get("report.working_draft").readiness(
        ws, {}
    ).state == "satisfied"

    ws.update_rcm(row_id, {"control": "Auditor changed the control after output generation"})
    ws.update_planning({"context": {"entity": "Changed entity"}})

    assert audit_capabilities.REGISTRY.get("working_papers.generated").readiness(
        ws, {}
    ).state == "stale"
    assert audit_capabilities.REGISTRY.get("dashboard.curated").readiness(
        ws, {}
    ).state == "stale"
    assert audit_capabilities.REGISTRY.get("report.working_draft").readiness(
        ws, {}
    ).state == "stale"


def test_missing_document_evidence_blocks_only_execution_branch(monkeypatch):
    ws = _planning_workspace("Missing workflow evidence")
    row = ws.rcm[0]
    ws.add_planned_test(
        row["id"],
        {
            "title": "Inspect approval package",
            "objective": "Determine whether approval evidence exists",
            "criteria": "Approval is documented.",
            "steps": ["Inspect the approval package."],
            "method": "inquiry",
            "expected_evidence": "Approval package REQ-404",
        },
    )
    fake = FakeAgentLLM(
        {
            "agent:document_test_spec": {
                "document_test": {
                    "title": "Approval evidence inquiry",
                    "kind": "qa",
                    "spec": {},
                    "items": [
                        {
                            "label": "Was REQ-404 approved?",
                            "question": "Was REQ-404 approved before commitment?",
                            "document_ids": [],
                        }
                    ],
                    "missing_evidence": {
                        "document_types": ["approved_requisition"],
                        "identifiers": "REQ-404",
                        "rationale": "The approval package is not imported.",
                    },
                }
            }
        }
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "provider": "fake", "model": "fake"},
    )

    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Run the RCM tests",
            "requested_outcomes": [
                "fieldwork.executed", "results.rolled_up", "report.working_draft",
                "audit.verified",
            ],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)
    execution = next(
        stage for stage in completed["workflow"]["stages"]
        if stage["capability"] == "fieldwork.executed"
    )

    assert completed["status"] == "completed_with_open_items"
    assert execution["units"][0]["status"] == "blocked"
    assert current.evidence_requests[0]["blocked_unit_id"] == execution["units"][0]["id"]
    assert completed["workflow"]["next_outcomes"][0] == "fieldwork.executed"
    assert current.report.get("markdown")

    previous_hash = current.evidence_requests[0]["evidence_availability_sha1"]
    received = documents.add_document(
        current, "REQ-404 approved_requisition.txt", b"REQ-404 was approved."
    )
    notification = runner.notify_evidence_available(
        current, document_ids=[received["id"]], reason="test_evidence_arrival"
    )
    refreshed = workspaces.load_workspace(ws.id)
    request = refreshed.evidence_requests[0]
    linked_test = doc_tests.load_test(refreshed, request["document_test_id"])

    assert notification["blocked_unit_ids"] == [execution["units"][0]["id"]]
    assert request["status"] == "received"
    assert request["evidence_availability_sha1"] != previous_hash
    assert received["id"] in linked_test["items"][0]["document_ids"]


def test_permission_mode_dispositions_resume_into_finding_batch(monkeypatch):
    ws = _planning_workspace("Permission dispositions")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1], "amount": [10.0, 10.0]}).write_csv().encode(),
    )
    row = ws.rcm[0]
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Test duplicate invoices",
            "objective": "Identify duplicate invoices",
            "criteria": "Invoices are unique.",
            "steps": ["Run duplicate analysis."],
            "method": "data_analytics",
            "expected_evidence": "Duplicate listing",
        },
    )
    test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify duplicates",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice"]}},
            "rcm_id": row["id"],
            "planned_test_id": planned["id"],
        },
    )
    data_tests.run(ws, test["id"])
    rcm_execution.rollup(ws)
    observation = ws.observations[0]
    fake = FakeAgentLLM(
        {
            "agent:finding": {
                "finding": {
                    "title": "Duplicate invoice processing",
                    "severity": "medium",
                    "condition": "A duplicate invoice identifier was processed.",
                    "criteria": "Invoice identifiers should be unique.",
                    "cause_pending": True,
                    "effect": "Duplicate payment risk.",
                    "recommendation": "Prevent duplicate invoice identifiers.",
                    "severity_rationale": "The exception can cause financial loss.",
                }
            }
        }
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "provider": "fake", "model": "fake"},
    )

    started = runner.start_command_run(
        ws,
        "permission",
        {
            "source": "tab_button",
            "text": "Draft eligible findings",
            "requested_outcomes": ["findings.drafted"],
        },
    )

    deadline = time.time() + 5
    interaction = None
    while time.time() < deadline and interaction is None:
        current_run = store.load_run(ws, started["id"])
        interaction = next(
            (
                item for item in current_run.get("interactions") or []
                if item.get("type") == "observation_disposition"
                and item.get("status") == "pending"
            ),
            None,
        )
        time.sleep(0.02)
    assert interaction is not None
    runner.resolve_interaction(
        ws,
        started["id"],
        interaction["id"],
        {
            "decisions": [
                {
                    "observation_id": observation["id"],
                    "disposition": "draft_finding_candidate",
                    "auditor_note": "Draft for review.",
                }
            ]
        },
    )

    approval = None
    deadline = time.time() + 5
    while time.time() < deadline and approval is None:
        current_run = store.load_run(ws, started["id"])
        approval = next(
            (
                item for item in current_run.get("approvals") or []
                if item.get("kind") == "finding_drafts" and item.get("status") == "pending"
            ),
            None,
        )
        time.sleep(0.02)
    assert approval is not None
    runner.resolve_approval(
        ws,
        started["id"],
        approval["id"],
        [{"item_id": item["id"], "action": "approve"} for item in approval["items"]],
    )
    completed = wait_run(ws, started["id"])
    refreshed = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert refreshed.observations[0]["status"] == "disposed"
    assert refreshed.findings[0]["source_observation_id"] == observation["id"]
    assert refreshed.findings[0]["auditor_confirmed"] is False


def test_full_workflow_runs_capability_closure_and_stops_for_auditor_judgment(monkeypatch):
    ws = workspaces.create_workspace("Workflow integration")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1, 2], "amount": [10.0, 10.0, 20.0]}).write_csv().encode(),
    )
    fake = FakeAgentLLM(
        {
            "agent:apm": {
                "apm_markdown": (
                    "# Audit Planning Memorandum\n\n"
                    "## Engagement\nAccounts payable.\n\n"
                    "## Introduction and background\nPayment processing.\n\n"
                    "## Process flow and understanding\nInvoices are approved and paid.\n\n"
                    "## Prior audit findings\nNo information available.\n\n"
                    "## Key risks and planned response\nTest duplicate invoices."
                )
            },
            "agent:rcm": {
                "rows": [
                    {
                        "operation": "create",
                        "process": "Accounts payable",
                        "risk": "Duplicate invoices may be paid",
                        "risk_rating": "high",
                        "assertion": "Occurrence",
                        "control": "Duplicate invoice validation",
                        "control_type": "Automated preventive",
                        "test_procedure": "Test duplicate invoice identifiers.",
                        "new_risk_reason": "No current RCM row covers duplicate invoices.",
                    }
                ]
            },
            "agent:work_program": {
                "planned_tests": [
                    {
                        "operation": "create",
                        "stable_slug": "duplicate-invoices",
                        "title": "Test duplicate invoices",
                        "objective": "Determine whether invoice identifiers repeat",
                        "criteria": "Each invoice is paid once.",
                        "steps": ["Identify repeated invoice identifiers."],
                        "method": "data_analytics",
                        "expected_evidence": "Durable duplicate listing",
                        "sampling": {"strategy": "full_population", "seed": 42},
                        "thresholds": {"maximum_duplicates": 0},
                    }
                ]
            },
            "agent:data_test_spec": {
                "data_test": {
                    "title": "Duplicate invoices",
                    "objective": "Identify repeated invoice identifiers",
                    "engine": "analytics",
                    "table_refs": ["transactions"],
                    "spec": {"test_id": "duplicates", "params": {"columns": ["invoice"]}},
                }
            },
        }
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "provider": "fake", "model": "fake"},
    )

    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "goal_template",
            "text": "Do the full audit",
            "goal_template": "full_audit_working_draft",
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed_with_open_items"
    assert "agent:command_interpreter" not in {call["tag"] for call in fake.calls}
    assert {call["tag"] for call in fake.calls} >= {
        "agent:apm", "agent:rcm", "agent:work_program", "agent:data_test_spec",
    }
    assert current.data_tests[0]["last_run"]
    assert current.observations and current.observations[0]["status"] != "disposed"
    assert not current.findings
    assert current.report.get("markdown")
    assert completed["workflow"]["next_outcomes"][0] == "findings.drafted"
    assert completed["usage"]["model_call_metrics"]
    assert completed["usage"]["request_characters"] > 0
    assert completed["usage"]["prompt_tokens"] >= completed["usage"]["estimated_prompt_tokens"]
    model_stages = {
        stage["capability"]: stage
        for stage in completed["workflow"]["stages"]
        if stage["capability"] in {
            "planning.apm_ready", "planning.rcm_ready",
            "planning.planned_tests_ready", "fieldwork.definitions_ready",
        }
    }
    assert all(
        unit["proposal_sidecar"]
        for stage in model_stages.values()
        for unit in stage["units"]
    )
