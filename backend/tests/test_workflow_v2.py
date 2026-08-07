import asyncio
import inspect
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import polars as pl
import pytest

from app import dashboard, data_tests, doc_tests, document_analysis, documents, llm, methodology, rcm_execution, report, working_papers, workspaces
from app.agent import action_runner, context_bundles, routing, runner, store, workflow
from app.agent import capabilities as audit_capabilities
from app.agent.audit_execution import (
    AuditWorkflowExecution,
    build_audit_workflow_runner,
)
from app.agent.doc_tests_execution import bind_document_qa
import app.agent.context.adapters as context_adapters
from app.agent.executors import fieldwork as fieldwork_executor
from app.agent.executors import planning as planning_executor
from app.agent.executors import tests as tests_executor
from app.agent.runtime import UnitSidecarStore, WorkflowRunner
from app.agent.workers import planning as planning_worker
from app.agent.context import (
    PRESETS,
    SELECTORS,
    ContextPreset,
    ContextResolver,
    PresetRegistry,
    SelectorRegistry,
)
from app.agent.routing import (
    classify_command as _classify_command,
    resolve_route,
)
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
    # A test is executable work, so drafting one needs material to test against.
    ws.add_table(
        "transactions.csv",
        b"invoice,amount\n1001,100\n1001,100\n1002,50\n",
    )
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

    assert resolve_route(ws, run) == "workflow"
    persisted = store.load_run(ws, run["id"])
    assert persisted["engine"] == store.WORKFLOW_ENGINE
    assert persisted["schema_version"] == 3
    assert persisted["workflow"]["requested_outcomes"] == audit_capabilities.FULL_AUDIT_OUTCOMES
    assert persisted["workflow"]["resolved_capabilities"] == audit_capabilities.REGISTRY.closure(
        audit_capabilities.FULL_AUDIT_OUTCOMES
    )
    assert "prepared_planning" not in persisted
    assert persisted["usage"]["llm_turns"] == 0


def test_finding_draft_scope_expands_only_the_selected_observation():
    ws = _planning_workspace("Scoped finding drafts")
    first_row = ws.rcm[0]
    second_row = ws.add_rcm(
        {
            "process": "Payroll",
            "risk": "Unauthorized payroll changes",
            "control": "Payroll changes require independent approval",
            "risk_rating": "high",
        }
    )
    ws.observations.extend([
        {
            "id": "OBS-ONE", "rcm_id": first_row["id"], "test_id": "DT-ONE",
            "execution_ref": "datatest:DT-ONE:RUN-ONE", "summary": "First exception",
            "outcome": "exception", "classification": "draft_finding_candidate",
        },
        {
            "id": "OBS-TWO", "rcm_id": second_row["id"], "test_id": "DT-TWO",
            "execution_ref": "datatest:DT-TWO:RUN-TWO", "summary": "Second exception",
            "outcome": "exception", "classification": "draft_finding_candidate",
        },
    ])

    capability = audit_capabilities.REGISTRY.get("findings.drafted")
    units = capability.expand_units(
        ws, {"target_refs": ["observation:OBS-ONE"]}
    )

    assert [unit.id for unit in units] == ["finding:obs-one"]
    assert units[0].parent_refs[0] == "observation:OBS-ONE"

    rcm_units = capability.expand_units(
        ws, {"target_refs": [f"rcm:{second_row['id']}"]}
    )
    assert [unit.parent_refs[0] for unit in rcm_units] == ["observation:OBS-TWO"]

    rolled = rcm_execution.rollup(ws, rcm_ids={first_row["id"]})
    assert [item["rcm_id"] for item in rolled["rows"]] == [first_row["id"]]


@pytest.mark.parametrize(
    ("template", "route", "outcomes"),
    [
        ("full_audit_working_draft", "workflow", audit_capabilities.FULL_AUDIT_OUTCOMES),
        (
            "planning",
            "workflow",
            [
                "planning.apm_ready",
                "planning.rcm_ready",
                "tests.specified",
            ],
        ),
        ("apm_only", "workflow", ["planning.apm_ready"]),
        ("rcm_only", "workflow", ["planning.rcm_ready"]),
        ("finding_draft", "workflow", ["findings.drafted"]),
        ("report", "workflow", ["report.working_draft", "audit.verified"]),
        # Phase 8 made data analysis a declared workflow goal; Phase 11 did the
        # same for both halves of the former ``document_testing`` template.
        ("data_analysis", "workflow", ["analysis.summarized"]),
        # Same terminal outcome as data_analysis, different request: with the
        # definitions already in place the scheduler reuses them and executes
        # without proposing more, which is what "run the saved analyses" means.
        ("analysis_execution", "workflow", ["analysis.executed"]),
        ("table_relationships", "workflow", ["data.joins_ready"]),
        ("document_analysis", "workflow", ["documents.analysis_generated"]),
        ("document_test_preparation", "workflow", ["tests.specified"]),
        ("document_test_execution", "workflow", ["doc_tests.executed"]),
    ],
)
def test_every_registered_goal_template_has_a_deterministic_local_route(
    template, route, outcomes
):
    # Every registered template resolves to a declared workflow outcome set.
    # There is no template that routes to the action catalog: an isolated
    # artifact operation is described by its own text, not by a lifecycle goal.
    assert set(routing.GOAL_TEMPLATES) == {
        "full_audit_working_draft",
        "planning",
        "apm_only",
        "rcm_only",
        "finding_draft",
        "data_analysis",
        "analysis_execution",
        "table_relationships",
        "document_analysis",
        "document_test_preparation",
        "document_test_execution",
        "report",
    }

    resolution = _classify_command(
        {
            "source": "goal_template",
            "text": f"Run {template}",
            "goal_template": template,
        }
    )

    assert resolution is not None
    assert resolution["route"] == route
    assert resolution["requested_outcomes"] == outcomes


def test_known_isolated_action_is_persisted_as_action_before_launch(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Attach the signed policy document to DT-1"},
    )

    assert resolve_route(workspace_with_data, run) == store.ACTION_ENGINE
    persisted = store.load_run(workspace_with_data, run["id"])
    assert persisted["engine"] == store.ACTION_ENGINE
    assert persisted["route"]["status"] == "resolved"
    assert persisted["route"]["route"] == "action"
    assert persisted["route"]["action_intent"] == "isolated_mutation"


def test_unclassifiable_command_launches_with_a_pending_route(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Please handle the outstanding work appropriately"},
    )

    assert resolve_route(workspace_with_data, run) is None
    persisted = store.load_run(workspace_with_data, run["id"])
    assert persisted["engine"] is None
    assert persisted["route"]["status"] == "pending"
    assert persisted["route"]["route"] is None


@pytest.mark.parametrize(
    ("text", "outcomes"),
    [
        ("Do a full audit", audit_capabilities.FULL_AUDIT_OUTCOMES),
        ("Complete the audit", audit_capabilities.FULL_AUDIT_OUTCOMES),
        ("Perform the entire audit", audit_capabilities.FULL_AUDIT_OUTCOMES),
        ("Run an end-to-end audit", audit_capabilities.FULL_AUDIT_OUTCOMES),
        ("Plan the audit", [
            "planning.apm_ready",
            "planning.rcm_ready",
            "tests.specified",
        ]),
        ("Draft the audit planning memorandum", ["planning.apm_ready"]),
        ("Generate the risk and control matrix", ["planning.rcm_ready"]),
        ("Create the planned procedures", ["tests.specified"]),
        ("Translate planned work into executable tests", ["tests.specified"]),
        ("Execute the RCM tests", ["fieldwork.executed", "results.rolled_up"]),
        ("Draft eligible findings", ["findings.drafted"]),
        ("Generate the audit report", ["report.working_draft"]),
    ],
)
def test_common_broad_audit_phrases_fail_closed_to_workflow(text, outcomes):
    resolution = _classify_command({"source": "chat", "text": text})

    assert resolution is not None
    assert resolution["route"] == "workflow"
    assert resolution["requested_outcomes"] == outcomes
    assert resolution["action_intent"] is None


def test_workflow_routes_use_normalized_generation_modes():
    ordinary = _classify_command({"source": "chat", "text": "Draft the APM"})
    forced = _classify_command(
        {"source": "chat", "text": "Regenerate the APM"}
    )

    assert ordinary is not None
    assert ordinary["generation_mode"] == "reuse_existing"
    assert forced is not None
    assert forced["generation_mode"] == "force"
    assert "refresh_policy" not in ordinary
    assert "refresh_policy" not in forced


def test_unknown_command_uses_bounded_router_then_generic_action_interpreter(
    monkeypatch, workspace_with_data
):
    def interpret(user):
        assert "prepared_planning" not in json.loads(user)
        return {
            "objective": "Check the report quality",
            "constraints": [],
            "completion_criteria": ["Quality results are recorded"],
            "needs_planning_wave": False,
            "actions": [
                {"id": "quality", "type": "run_report_quality", "args": {}}
            ],
        }

    fake = FakeAgentLLM(
        {
            "agent:workflow_router": {
                "route": "action",
                "requested_outcomes": [],
                "objective": "Check the report quality",
                "target_refs": [],
                "generation_mode": "reuse_existing",
                "action_intent": "run_report_quality",
                "constraints": [],
                "clarification": None,
            },
            "agent:command_interpreter": interpret,
        }
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )

    started = runner.start_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Please handle the outstanding work appropriately"},
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["status"] in {"completed", "completed_with_issues"}
    assert completed["schema_version"] == 2
    assert completed["engine"] == store.ACTION_ENGINE
    assert completed["route"]["route"] == "action"
    assert completed["route"]["decided_by"] == "router_worker"
    assert [call["tag"] for call in fake.calls] == [
        "agent:workflow_router",
        "agent:command_interpreter",
    ]
    assert [action["type"] for action in completed["actions"]] == [
        "run_report_quality"
    ]


def test_action_runner_rejects_a_workflow_owned_record(monkeypatch, workspace_with_data):
    """The scheduler's defensive boundary, not a second classification.

    Routing never persists this shape. If a malformed record still reaches the
    action scheduler carrying a workflow-owned request, it fails before the
    action interpreter is invoked and without spending a model turn.
    """
    fake = FakeAgentLLM({})
    monkeypatch.setattr(llm, "chat", fake)

    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Perform the entire audit"},
    )
    run["engine"] = store.ACTION_ENGINE
    run["route"] = routing.normalize_route(
        "action", decided_by="malformed_record_fixture"
    )
    store.save_run(workspace_with_data, run)

    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    action_runner.ActionRunner(workspace_with_data, run, handle).execute()

    completed = store.load_run(workspace_with_data, run["id"])
    assert completed["status"] == "failed"
    assert "must use workflow routing" in completed["error"]
    assert completed["actions"] == []
    assert fake.calls == []


def test_generate_the_apm_materializes_locally_in_auto_mode_without_context():
    ws = workspaces.create_workspace("Local APM route")
    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "chat",
            "text": "generate the APM",
        },
    )

    assert resolve_route(ws, run) == "workflow"
    persisted = store.load_run(ws, run["id"])
    assert persisted["workflow"]["requested_outcomes"] == ["planning.apm_ready"]
    assert [
        stage["capability"] for stage in persisted["workflow"]["stages"]
    ] == ["planning.context_ready", "planning.apm_ready"]
    assert persisted["interactions"] == []
    assert persisted["usage"]["llm_turns"] == 0


def test_audit_workflow_declares_the_complete_lifecycle_graph():
    expected_dependencies = {
        # Phase 9: planning consumes generated document analyses through the
        # scoped document-analysis edge rather than raw document text.
        "documents.text_ready": (),
        "documents.analysis_chunks_ready": ("documents.text_ready",),
        "documents.analysis_generated": ("documents.analysis_chunks_ready",),
        "data.relationships_inferred": (),
        "data.joins_ready": ("data.relationships_inferred",),
        "analysis.definitions_ready": ("data.joins_ready",),
        "analysis.executed": ("analysis.definitions_ready",),
        "analysis.summarized": ("analysis.executed",),
        "planning.context_ready": ("documents.analysis_generated",),
        "planning.apm_ready": ("planning.context_ready",),
        "planning.rcm_ready": ("planning.apm_ready",),
        "tests.specified": ("planning.rcm_ready",),
        "fieldwork.executed": ("tests.specified",),
        "results.rolled_up": ("fieldwork.executed",),
        "findings.drafted": ("results.rolled_up",),
        "working_papers.generated": ("results.rolled_up",),
        "dashboard.curated": ("results.rolled_up",),
        "report.working_draft": (
            "planning.apm_ready",
            "results.rolled_up",
            "findings.drafted",
        ),
        "audit.verified": (
            "working_papers.generated",
            "dashboard.curated",
            "report.working_draft",
        ),
    }

    assert {
        capability.id: capability.depends_on
        for capability in audit_capabilities.REGISTRY.all()
    } == expected_dependencies


def test_full_audit_closure_is_topological_and_preserves_parallel_branches():
    resolved = audit_capabilities.REGISTRY.closure(
        audit_capabilities.FULL_AUDIT_OUTCOMES
    )

    assert resolved == [
        "data.relationships_inferred",
        "data.joins_ready",
        "analysis.definitions_ready",
        "analysis.executed",
        "analysis.summarized",
        "documents.text_ready",
        "documents.analysis_chunks_ready",
        "documents.analysis_generated",
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "tests.specified",
        "fieldwork.executed",
        "results.rolled_up",
        "findings.drafted",
        "working_papers.generated",
        "dashboard.curated",
        "report.working_draft",
        "audit.verified",
    ]
    position = {capability_id: index for index, capability_id in enumerate(resolved)}
    assert position["analysis.summarized"] < position["planning.apm_ready"]
    assert all(
        position[dependency] < position[capability.id]
        for capability in audit_capabilities.REGISTRY.all()
        for dependency in capability.depends_on
    )
    assert {
        capability_id: audit_capabilities.REGISTRY.get(capability_id).depends_on
        for capability_id in (
            "findings.drafted",
            "working_papers.generated",
            "dashboard.curated",
        )
    } == {
        "findings.drafted": ("results.rolled_up",),
        "working_papers.generated": ("results.rolled_up",),
        "dashboard.curated": ("results.rolled_up",),
    }


def test_partial_goal_prunes_current_prerequisites():
    ws = _planning_workspace()
    resolved, stages, reused = workflow.materialize(
        audit_capabilities.REGISTRY,
        ws,
        ["tests.specified"],
        {"target_refs": ["workspace:current"]},
    )

    assert resolved == [
        # The workspace carries no document, so every document capability is
        # satisfied and reused without expanding a single unit.
        "documents.text_ready",
        "documents.analysis_chunks_ready",
        "documents.analysis_generated",
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "tests.specified",
    ]
    assert reused == resolved[:-1]
    assert [stage["capability"] for stage in stages] == ["tests.specified"]
    assert len(stages[0]["units"]) == 1


def test_repeated_materialization_preserves_semantic_unit_identity():
    ws = _planning_workspace("Stable semantic units")
    scope = {"target_refs": ["workspace:current"]}

    first = workflow.materialize(
        audit_capabilities.REGISTRY,
        ws,
        ["tests.specified"],
        scope,
        generation_mode="force",
    )
    second = workflow.materialize(
        audit_capabilities.REGISTRY,
        workspaces.load_workspace(ws.id),
        ["tests.specified"],
        dict(scope),
        generation_mode="force",
    )

    def identities(materialized):
        _resolved, stages, _reused = materialized
        return [
            (
                stage["capability"],
                [(unit["id"], unit["kind"], unit["input_sha1"]) for unit in stage["units"]],
            )
            for stage in stages
        ]

    assert identities(first) == identities(second)
    assert all(unit_id for _capability, units in identities(first) for unit_id, _kind, _sha1 in units)


def test_workflow_test_generate_repair_reports_all_contract_errors(monkeypatch):
    ws = _planning_workspace("Test generate contract repair")
    attempts = 0

    def generated_tests(user):
        nonlocal attempts
        attempts += 1
        base = {
            "title": "Test duplicate payments",
            "objective": "Determine whether duplicate payments occurred",
        }
        if attempts == 1:
            return {
                "tests": [{
                    **base,
                    "source": "document",
                    "steps": "Identify repeated invoice identifiers.",
                }]
            }
        assert "tests[0].steps" in user
        assert "no document is available" in user
        return {
            "tests": [{
                **base,
                "source": "data",
                "steps": [
                    {
                        "label": "Identify duplicates",
                        "instruction": "Identify repeated invoice identifiers.",
                        "table_refs": ["transactions"],
                        "code": (
                            "result = transactions.filter("
                            "transactions['invoice'].is_duplicated())"
                        ),
                    }
                ],
            }]
        }

    fake = FakeAgentLLM({"agent:test_generate": generated_tests})
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
            "text": "Generate the tests",
            "requested_outcomes": ["tests.specified"],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed", completed.get("error")
    assert attempts == 2
    assert current.data_tests[0]["title"] == "Test duplicate payments"
    assert current.data_tests[0]["status"] == "ready"


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


def test_test_generate_definition_context_has_schema_metadata_but_no_table_rows():
    ws = workspaces.create_workspace("Row privacy boundary")
    ws.add_table(
        "private_ledger.csv",
        pl.DataFrame(
            {
                "invoice_id": ["ROW_SECRET_NEVER_SEND_7F4C"],
                "amount": [123.45],
            }
        ).write_csv().encode(),
    )
    row = ws.add_rcm(
        {
            "risk": "Duplicate private-ledger invoices may be paid",
            "control": "Duplicate invoice monitoring",
        }
    )

    capability = audit_capabilities.REGISTRY.get("tests.specified")
    manifest, bundle = ContextResolver().resolve(
        ws,
        capability,
        SimpleNamespace(id=f"test_generation:{row['id']}"),
        context_adapters.test_generate_scope(ws, row["id"]),
    )
    serialized = bundle.to_json()

    assert "private_ledger" in serialized
    assert "invoice_id" in serialized
    assert "ROW_SECRET_NEVER_SEND_7F4C" not in serialized
    # The manifest records what was supplied without carrying any content.
    assert "ROW_SECRET_NEVER_SEND_7F4C" not in manifest.to_json()
    assert {selection.source_id for selection in manifest.selections} >= {
        "rcm_row",
        "table_metadata",
    }
    # No document exists in this workspace, so the declared document source
    # supplies nothing.
    assert not [item for item in bundle.items if item.source_id == "documents"]


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

    # This route requests only planning context. Its concurrent table write
    # must survive the context commit, and no full-audit review outcome is in
    # scope to leave the run open.
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


def test_results_rolled_up_through_deterministic_scheduler_path(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning(
        {
            "context": {"objective": "Assess payments", "scope": "Accounts payable"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nAccounts payable.",
        }
    )
    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    data_test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
                        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    data_tests.run(ws, data_test["id"])

    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "follow_up",
            "text": "Roll up the results",
            "requested_outcomes": ["results.rolled_up"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(ws, run) == "workflow"
    run = store.load_run(ws, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "results.rolled_up"
    )
    command = build_audit_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )

    # execute() refreshes the subject from disk before each stage; mirror that
    # here so the stage runs against a disk-consistent workspace.
    command._refresh()
    command._run_stage(stage)

    # The deterministic path recomputed the roll-up and folded a succeeded unit
    # whose ref is the stable ``rcm:<id>`` result reference.
    unit = stage["units"][0]
    assert unit["status"] == "succeeded"
    assert unit["result_refs"] == [f"rcm:{row['id']}"]
    assert stage["status"] == "succeeded"
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.rcm[0]["execution_rollup"]["tests"] == 1
    assert reloaded.observations  # the exception raised an observation
    # The binder emits the roll-up ``workspace_changed`` signal the generic
    # deterministic path does not.
    assert {
        (event["data"].get("kind"), event["data"].get("id"))
        for event in store.read_events(ws, run["id"])
        if event["type"] == "workspace_changed"
    } >= {("rcm", "rollup")}
    # Deterministic execution makes no provider call.
    assert run["usage"]["llm_turns"] == 0


def _planning_context_stage_runner(ws):
    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "follow_up",
            "text": "Assemble the planning context",
            "requested_outcomes": ["planning.context_ready"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(ws, run) == "workflow"
    run = store.load_run(ws, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "planning.context_ready"
    )
    command = build_audit_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )
    command._refresh()
    return command, stage, stage["units"][0]


def test_live_planning_context_commits_through_pipeline_binding(monkeypatch):
    # P7A.2: planning-context synthesis runs on the scheduler's native pipeline
    # path — declared context in, registered worker, registered executor commit.
    ws = workspaces.create_workspace("Live planning-context binding")
    policy = documents.add_document(
        ws,
        "Procurement Policy.txt",
        b"Procurement Policy: purchases require documented approval before commitment.",
        category="policy",
    )
    captured = {}

    def synthesize(user):
        captured["user"] = user
        return {
            "context": {
                "objective": "Assess procurement approvals",
                "scope": "Requisition through payment",
            }
        }

    monkeypatch.setattr(
        llm, "chat", FakeAgentLLM({"agent:document_context": synthesize})
    )
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command, stage, unit = _planning_context_stage_runner(ws)

    command._run_stage(stage)

    assert unit["status"] == "succeeded"
    assert stage["status"] == "succeeded"
    assert unit["result_refs"] == ["planning:context"]
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.planning["context"]["objective"] == "Assess procurement approvals"
    assert reloaded.planning["context"]["scope"] == "Requisition through payment"
    # The declared context supplied the document material, and provenance records
    # the document that was actually supplied.
    assert "purchases require documented approval" in captured["user"]
    assert any(
        item["stage"] == "agent:document_context"
        and policy["id"] in item["document_ids"]
        for item in documents.activities(reloaded, limit=250)["items"]
    )
    # The pipeline persisted content-free manifest, proposal, and receipt sidecars.
    assert unit["context_manifest"]["unit_id"] == unit["id"]
    assert unit["proposal_sidecar"]["unit_id"] == unit["id"]
    assert unit["receipt_sidecar"]["unit_id"] == unit["id"]
    assert (
        store.run_dir(ws, command.run["id"]) / unit["receipt_sidecar"]["path"]
    ).is_file()
    # The durable sidecars are the contract; implementation helper names are not.
    assert "planning_basis" not in command.run


def test_planning_context_settles_without_a_model_call_when_no_document_material():
    # There is nothing to synthesize from, so the unit settles locally and the
    # existing context stands rather than the stage failing.
    ws = workspaces.create_workspace("Planning context with no documents")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice_id": ["INV-1"], "amount": [10.0]})
        .write_csv()
        .encode(),
    )
    command, stage, unit = _planning_context_stage_runner(ws)

    command._run_stage(stage)

    assert unit["status"] == "succeeded"
    assert unit["result_refs"] == ["planning:context"]
    assert command.run["usage"]["llm_turns"] == 0
    assert any(
        "No document material is available" in warning
        for warning in command.run.get("warnings") or []
    )


def _executed_stage_runner(ws, text="Execute the fieldwork"):
    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "follow_up",
            "text": text,
            "requested_outcomes": ["fieldwork.executed"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(ws, run) == "workflow"
    run = store.load_run(ws, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "fieldwork.executed"
    )
    command = build_audit_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )
    # ``execute()`` refreshes the subject from disk before every stage; mirror
    # that so the stage runs against a disk-consistent workspace.
    command._refresh()
    return command, stage


def test_data_test_execution_runs_through_the_mixed_execution_binding(
    workspace_with_data,
):
    # P7F.2: a data-test unit of ``fieldwork.executed`` is deterministic — the
    # binder computes locally through the extracted executor and folds a
    # succeeded unit whose ref names the immutable result, with no model call.
    ws = workspace_with_data
    ws.update_planning(
        {
            "context": {"objective": "Assess payments", "scope": "Accounts payable"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nAccounts payable.",
        }
    )
    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    data_test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
                        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    command, stage = _executed_stage_runner(ws)

    command._run_stage(stage)

    unit = stage["units"][0]
    assert unit["kind"] == "data_test_execution"
    assert unit["status"] == "succeeded"
    reloaded = workspaces.load_workspace(ws.id)
    committed = reloaded.data_tests[0]
    assert committed["last_run"]
    assert unit["result_refs"] == [
        f"datatest:{data_test['id']}:{committed['last_run']['id']}"
    ]
    assert {
        (event["data"].get("kind"), event["data"].get("action"))
        for event in store.read_events(ws, command.run["id"])
        if event["type"] == "workspace_changed"
    } >= {("datatest", "executed")}
    # A deterministic unit of a pipeline-bound capability still bills nothing.
    assert command.run["usage"]["llm_turns"] == 0
    # The old batch handler and its inline model adapter are gone.
    assert not hasattr(AuditWorkflowExecution, "_executions")
    assert not hasattr(AuditWorkflowExecution, "_document_qa_adapter")


def test_document_qa_execution_commits_through_the_pipeline_binding(monkeypatch):
    # P7F.3: the one model-backed unit kind of ``fieldwork.executed`` runs
    # through the registered worker on the injected gateway and the registered
    # executor, and auto mode applies the worker's supported outcome.
    ws = _planning_workspace("Live document Q&A binding")
    row = ws.rcm[0]
    document = documents.add_document(
        ws,
        "Approval.txt",
        b"The purchase order was approved by the controller on 3 March.",
    )
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval Q&A",
            "kind": "qa",
            "rcm_id": row["id"],
                        "items": [
                {
                    "label": "Who approved the order?",
                    "question": "Who approved the purchase order?",
                    "document_ids": [document["id"]],
                    "pages": [1],
                }
            ],
        },
    )
    item_id = test["items"][0]["id"]
    captured = {}

    def answer(user):
        captured["user"] = user
        return {
            "answer": "The controller approved it.",
            "outcome": "accepted",
            "citations": [
                {"page": 1, "excerpt": "approved by the controller"},
                {"page": 42, "excerpt": "a page that was never supplied"},
            ],
        }

    monkeypatch.setattr(llm, "chat", FakeAgentLLM({"agent:document_qa": answer}))
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command, stage = _executed_stage_runner(ws, text="Answer the document questions")

    command._run_stage(stage)

    unit = stage["units"][0]
    assert unit["kind"] == "document_qa_execution"
    assert unit["status"] == "succeeded"
    assert unit["result_refs"] == [
        f"doctest:{test['id']}:item:{item_id}:document:{document['id']}"
    ]
    assert stage["status"] == "succeeded"
    committed = doc_tests.load_test(workspaces.load_workspace(ws.id), test["id"])
    stored = committed["items"][0]["qa_answers"][document["id"]]
    assert stored["answer"] == "The controller approved it."
    # The worker bound citations to supplied pages; the fabricated one is gone.
    assert [anchor["page"] for anchor in stored["citations"]] == [1]
    assert "Who approved the purchase order?" in captured["user"]
    # The pipeline persisted content-free manifest, proposal, and receipt sidecars.
    assert unit["context_manifest"]["unit_id"] == unit["id"]
    assert unit["proposal_sidecar"]["unit_id"] == unit["id"]
    assert unit["receipt_sidecar"]["unit_id"] == unit["id"]
    assert (
        store.run_dir(ws, command.run["id"]) / unit["receipt_sidecar"]["path"]
    ).is_file()
    assert {
        (event["data"].get("kind"), event["data"].get("action"))
        for event in store.read_events(ws, command.run["id"])
        if event["type"] == "workspace_changed"
    } >= {("doctest", "qa_answered")}


def test_document_qa_resume_reuses_the_durable_proposal_without_rebilling(monkeypatch):
    ws = _planning_workspace("Document Q&A proposal no rebilling")
    row = ws.rcm[0]
    document = documents.add_document(
        ws, "Approval.txt", b"The controller approved the purchase order."
    )
    doc_tests.create_test(
        ws,
        {
            "title": "Approval Q&A",
            "kind": "qa",
            "rcm_id": row["id"],
                        "items": [
                {
                    "label": "Who approved the order?",
                    "question": "Who approved the purchase order?",
                    "document_ids": [document["id"]],
                    "pages": [1],
                }
            ],
        },
    )
    fake = FakeAgentLLM(
        {
            "agent:document_qa": {
                "answer": "The controller approved it.",
                "outcome": "accepted",
                "citations": [{"page": 1, "excerpt": "The controller approved"}],
            }
        }
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command, stage = _executed_stage_runner(ws)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            fieldwork_executor,
            "mutate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            command._run_stage(stage)

    unit = stage["units"][0]
    assert unit["proposal_sidecar"]
    turns_after_generation = command.run["usage"]["llm_turns"]
    assert turns_after_generation == 1

    # The proposal is durable, so the resumed unit commits from the sidecar.
    command._refresh()
    command._run_stage(stage)

    assert unit["status"] == "succeeded"
    assert command.run["usage"]["llm_turns"] == turns_after_generation


def test_working_papers_generate_through_deterministic_scheduler_path():
    ws = _planning_workspace("Working paper stage")
    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "follow_up",
            "text": "Generate the working papers",
            "requested_outcomes": ["working_papers.generated"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(ws, run) == "workflow"
    run = store.load_run(ws, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "working_papers.generated"
    )
    command = build_audit_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )

    # execute() refreshes the subject from disk before each stage; mirror that
    # here so the stage runs against a disk-consistent workspace.
    command._refresh()
    command._run_stage(stage)

    rcm_id = ws.rcm[0]["id"]
    # The deterministic path committed the paper file and folded a succeeded unit.
    assert (ws.root / "WorkingPapers" / f"{rcm_id}.json").is_file()
    unit = stage["units"][0]
    assert unit["status"] == "succeeded"
    assert unit["result_refs"] == [f"working_paper:{rcm_id}"]
    assert stage["status"] == "succeeded"
    # Deterministic execution makes no provider call.
    assert run["usage"]["llm_turns"] == 0


def test_dashboard_curated_through_deterministic_scheduler_path(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning(
        {
            "context": {"objective": "Assess payments", "scope": "Accounts payable"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nAccounts payable.",
        }
    )
    row = ws.add_rcm(
        {
            "process": "Procurement",
            "risk": "Vendor approval risk",
            "control": "Vendor master control",
            "risk_rating": "high",
        }
    )
    data_test = data_tests.create(
        ws,
        {
            "title": "Vendor integrity result",
            "objective": "Identify management-relevant vendor integrity signals.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
                        "spec": {"test_id": "sign_scan", "params": {"column": "amount"}},
        },
    )
    data_tests.run(ws, data_test["id"])

    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "follow_up",
            "text": "Curate the dashboard",
            "requested_outcomes": ["dashboard.curated"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(ws, run) == "workflow"
    run = store.load_run(ws, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "dashboard.curated"
    )
    command = build_audit_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )

    # execute() refreshes the subject from disk before each stage; mirror that
    # here so the stage runs against a disk-consistent workspace.
    command._refresh()
    command._run_stage(stage)

    tile_id = f"rcm-{data_test['id'].casefold()}"
    # The deterministic path pinned the tile and folded a succeeded unit whose
    # ref is the stable ``tile:<id>``.
    unit = stage["units"][0]
    assert unit["status"] == "succeeded"
    assert unit["result_refs"] == [f"tile:{tile_id}"]
    assert stage["status"] == "succeeded"
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.planning["dashboard_curation"]["created_count"] == 1
    assert [tile["id"] for tile in reloaded.tiles] == [tile_id]
    # Deterministic execution makes no provider call.
    assert run["usage"]["llm_turns"] == 0


def _apm_only_runner(workspace, *, context_resolver=None):
    run = store.new_command_run(
        workspace,
        "auto",
        {
            "source": "follow_up",
            "text": "Regenerate the APM",
            "requested_outcomes": ["planning.apm_ready"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(workspace, run) == "workflow"
    run = store.load_run(workspace, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "planning.apm_ready"
    )
    command = build_audit_workflow_runner(
        workspace,
        run,
        runner.RunHandle(workspace.id, run["id"]),
        context_resolver=context_resolver,
    )
    return command, stage, stage["units"][0]


def _changed_apm_context_resolver(change):
    selectors = SelectorRegistry()
    for definition in SELECTORS.all():
        if change == "selector" and definition.selector_id == "planning.current":
            definition = replace(
                definition,
                implementation_hash="sha256:" + "f" * 64,
            )
        selectors.register(definition)
    presets = PresetRegistry(selectors)
    for preset in PRESETS.all():
        if change == "spec" and preset.preset_id == "planning.apm":
            spec = replace(
                preset.spec,
                budget=replace(
                    preset.spec.budget,
                    max_characters=preset.spec.budget.max_characters - 1,
                ),
            )
            preset = ContextPreset(preset.preset_id, spec)
        presets.register(preset)
    return ContextResolver(selectors=selectors, presets=presets)


def test_reused_apm_artifact_does_not_run_context_selection():
    ws = _planning_workspace("Reuse skips APM resolution")

    class ResolverMustNotRun:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("reused artifacts must not rerun context selection")

    run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "follow_up",
            "text": "Use the current APM",
            "requested_outcomes": ["planning.apm_ready"],
            "generation_mode": "reuse_existing",
        },
    )
    assert resolve_route(ws, run) == "workflow"
    run = store.load_run(ws, run["id"])
    command = build_audit_workflow_runner(
        ws,
        run,
        runner.RunHandle(ws.id, run["id"]),
        context_resolver=ResolverMustNotRun(),
    )

    command.execute()

    assert command.run["status"] == "completed"
    assert "planning.apm_ready" in command.run["workflow"]["reused_capabilities"]
    assert {
        "capability": "planning.apm_ready",
        "currency_status": "not_assessed",
    } in command.run["workflow"]["reused_capability_details"]


def test_apm_resume_reuses_durable_proposal_without_rebilling(monkeypatch):
    ws = workspaces.create_workspace("APM proposal no rebilling")
    ws.update_planning(
        {
            "context": {
                "objective": "Assess procurement approvals",
                "scope": "Purchasing",
            }
        }
    )
    response = (
        "# Audit Planning Memorandum\n\n"
        "## Engagement\nProcurement approvals.\n\n"
        "## Introduction and background\nPurchasing.\n\n"
        "## Process flow and understanding\nApprovals precede commitment.\n\n"
        "## Prior audit findings\nNo information available.\n\n"
        "## Data analytics performed\nNo data analysis has been performed.\n\n"
        "## Key risks and planned response\nTest approval evidence."
    )
    fake = FakeAgentLLM({"agent:apm": {"apm_markdown": response}})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command, stage, unit = _apm_only_runner(ws)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            planning_executor,
            "mutate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            command._run_stage(stage)

    run_root = store.run_dir(ws, command.run["id"])
    assert [call["tag"] for call in fake.calls] == ["agent:apm"]
    assert (run_root / unit["proposal_sidecar"]["path"]).is_file()
    assert not (run_root / "receipts" / "apm.json").exists()

    workflow.recovery(command.run["workflow"])
    resumed = build_audit_workflow_runner(
        ws,
        command.run,
        runner.RunHandle(ws.id, command.run["id"]),
    )
    resumed._run_stage(stage)

    assert [call["tag"] for call in fake.calls] == ["agent:apm"]
    assert unit["status"] == "succeeded"
    assert (run_root / unit["receipt_sidecar"]["path"]).is_file()
    assert "proposal_reused" in {
        event["type"] for event in store.read_events(ws, resumed.run["id"])
    }


@pytest.mark.parametrize("change", ["spec", "selector"])
def test_apm_proposal_reuse_rejects_changed_context_execution_identity(
    monkeypatch,
    change,
):
    ws = workspaces.create_workspace(f"APM proposal identity {change}")
    ws.update_planning(
        {
            "context": {
                "objective": "Assess procurement approvals",
                "scope": "Purchasing",
            }
        }
    )
    response = (
        "# Audit Planning Memorandum\n\n"
        "## Engagement\nProcurement approvals.\n\n"
        "## Introduction and background\nPurchasing.\n\n"
        "## Process flow and understanding\nApprovals precede commitment.\n\n"
        "## Prior audit findings\nNo information available.\n\n"
        "## Data analytics performed\nNo data analysis has been performed.\n\n"
        "## Key risks and planned response\nTest approval evidence."
    )
    fake = FakeAgentLLM({"agent:apm": {"apm_markdown": response}})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command, stage, unit = _apm_only_runner(ws)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            planning_executor,
            "mutate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            command._run_stage(stage)

    first_sidecar = UnitSidecarStore(
        ws, command.run["id"]
    ).load_proposal(
        unit["id"], unit["proposal_sidecar"]
    )
    first_identity = first_sidecar["execution_identity"]
    assert [call["tag"] for call in fake.calls] == ["agent:apm"]

    workflow.recovery(command.run["workflow"])
    resumed = build_audit_workflow_runner(
        ws,
        command.run,
        runner.RunHandle(ws.id, command.run["id"]),
        context_resolver=_changed_apm_context_resolver(change),
    )
    resumed._run_stage(stage)

    second_sidecar = UnitSidecarStore(
        resumed.subject, resumed.run["id"]
    ).load_proposal(
        unit["id"], unit["proposal_sidecar"]
    )
    assert second_sidecar["execution_identity"] != first_identity
    assert [call["tag"] for call in fake.calls] == ["agent:apm", "agent:apm"]
    assert unit["status"] == "succeeded"
    assert "proposal_reuse_rejected" in {
        event["type"] for event in store.read_events(resumed.subject, resumed.run["id"])
    }


def test_live_apm_capability_uses_only_resolved_private_context(monkeypatch):
    ws = workspaces.create_workspace("Live resolver privacy")
    sentinel = "ROW_SECRET_NEVER_SEND_P48"
    methodology_text = "P48_METHODOLOGY_PRIVATE: test approval evidence."
    ws.update_planning(
        {
            "context": {
                "objective": "Assess procurement approvals",
                "scope": "Purchasing",
            }
        }
    )
    ws.add_table(
        "private-ledger.csv",
        pl.DataFrame(
            {"invoice_id": [sentinel, "INV-2"], "amount": [100.0, 200.0]}
        ).write_csv().encode(),
    )
    methodology.save_pack(
        ws,
        "Procurement Methodology",
        f"# Approval testing\n{methodology_text}",
    )
    response = (
        "# Audit Planning Memorandum\n\n"
        "## Engagement\nProcurement approvals.\n\n"
        "## Introduction and background\nPurchasing.\n\n"
        "## Process flow and understanding\nApprovals precede commitment.\n\n"
        "## Prior audit findings\nNo information available.\n\n"
        "## Data analytics performed\nNo data analysis has been performed.\n\n"
        "## Key risks and planned response\nTest approval evidence."
    )
    command, stage, unit = _apm_only_runner(ws)
    captured = {}

    def draft(user):
        captured["user"] = user
        reference = unit.get("context_manifest")
        assert reference
        manifest = command.runtime.load_context_manifest(reference)
        captured["manifest"] = manifest.to_json()
        assert documents.activities(ws)["items"] == []
        return {"apm_markdown": response}

    fake = FakeAgentLLM({"agent:apm": draft})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command._run_stage(stage)

    assert methodology_text in captured["user"]
    assert "private_ledger" in captured["user"]
    assert "invoice_id" in captured["user"]
    assert sentinel not in captured["user"]
    assert methodology_text not in captured["manifest"]
    assert sentinel not in captured["manifest"]
    assert sentinel not in json.dumps(command.run.get("provenance") or [])
    assert unit["proposal_sidecar"]["path"] == "proposals/apm.json"
    assert unit["receipt_sidecar"]["path"] == "receipts/apm.json"
    assert (
        store.run_dir(ws, command.run["id"])
        / unit["receipt_sidecar"]["path"]
    ).is_file()


def _rcm_only_runner(workspace):
    run = store.new_command_run(
        workspace,
        "auto",
        {
            "source": "follow_up",
            "text": "Draft the RCM",
            "requested_outcomes": ["planning.rcm_ready"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(workspace, run) == "workflow"
    run = store.load_run(workspace, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "planning.rcm_ready"
    )
    command = build_audit_workflow_runner(
        workspace,
        run,
        runner.RunHandle(workspace.id, run["id"]),
    )
    return command, stage, stage["units"][0]


_RCM_RESPONSE = {
    "rows": [
        {
            "operation": "create",
            "process": "Accounts payable",
            "risk": "Duplicate payments are processed",
            "risk_rating": "high",
            "assertion": "Occurrence",
            "control": "Duplicate invoice validation",
            "control_type": "Automated preventive",
            "test_procedure": "Test invoice and amount duplicates.",
            "new_risk_reason": "No existing RCM row covers duplicate payments.",
        }
    ]
}


def test_live_rcm_capability_commits_through_pipeline_binding(monkeypatch):
    ws = workspaces.create_workspace("Live RCM binding")
    sentinel = "ROW_SECRET_NEVER_SEND_P7C2"
    ws.update_planning(
        {
            "context": {"objective": "Assess procurement approvals", "scope": "Purchasing"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nPurchasing.",
        }
    )
    ws.add_table(
        "private-ledger.csv",
        pl.DataFrame(
            {"invoice_id": [sentinel, "INV-2"], "amount": [100.0, 200.0]}
        ).write_csv().encode(),
    )
    command, stage, unit = _rcm_only_runner(ws)
    captured = {}

    def draft(user):
        captured["user"] = user
        return _RCM_RESPONSE

    fake = FakeAgentLLM({"agent:rcm": draft})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command._run_stage(stage)

    assert unit["status"] == "succeeded"
    assert stage["status"] == "succeeded"
    reloaded = workspaces.load_workspace(ws.id)
    assert len(reloaded.rcm) == 1
    created = reloaded.rcm[0]
    assert created["risk"] == "Duplicate payments are processed"
    assert created["created_by"] == "agent"
    assert unit["result_refs"] == [f"rcm:{created['id']}"]
    assert command.run["planning_changes"]["rcm_created"] == 1
    # Row-level table values never reach the provider or the durable provenance.
    assert sentinel not in captured["user"]
    assert sentinel not in json.dumps(command.run.get("provenance") or [])
    # The pipeline persisted content-free manifest, proposal, and receipt sidecars.
    assert unit["proposal_sidecar"]["path"] == "proposals/rcm.json"
    assert unit["receipt_sidecar"]["path"] == "receipts/rcm.json"
    assert (
        store.run_dir(ws, command.run["id"]) / unit["receipt_sidecar"]["path"]
    ).is_file()


def test_rcm_resume_reuses_durable_proposal_without_rebilling(monkeypatch):
    ws = workspaces.create_workspace("RCM proposal no rebilling")
    ws.update_planning(
        {
            "context": {"objective": "Assess procurement approvals", "scope": "Purchasing"},
            "apm_markdown": "# Audit Planning Memorandum\n\n## Scope\nPurchasing.",
        }
    )
    fake = FakeAgentLLM({"agent:rcm": _RCM_RESPONSE})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command, stage, unit = _rcm_only_runner(ws)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            planning_executor,
            "mutate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            command._run_stage(stage)

    run_root = store.run_dir(ws, command.run["id"])
    assert [call["tag"] for call in fake.calls] == ["agent:rcm"]
    assert (run_root / unit["proposal_sidecar"]["path"]).is_file()
    assert not (run_root / "receipts" / "rcm.json").exists()

    workflow.recovery(command.run["workflow"])
    resumed = build_audit_workflow_runner(
        ws,
        command.run,
        runner.RunHandle(ws.id, command.run["id"]),
    )
    resumed._run_stage(stage)

    # Resume reused the durable proposal; no second provider call was billed.
    assert [call["tag"] for call in fake.calls] == ["agent:rcm"]
    assert unit["status"] == "succeeded"
    assert (run_root / unit["receipt_sidecar"]["path"]).is_file()
    assert "proposal_reused" in {
        event["type"] for event in store.read_events(ws, resumed.run["id"])
    }
    assert len(workspaces.load_workspace(ws.id).rcm) == 1


def _test_generate_only_runner(workspace):
    run = store.new_command_run(
        workspace,
        "auto",
        {
            "source": "follow_up",
            "text": "Generate the tests",
            "requested_outcomes": ["tests.specified"],
            "generation_mode": "force",
        },
    )
    assert resolve_route(workspace, run) == "workflow"
    run = store.load_run(workspace, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "tests.specified"
    )
    command = build_audit_workflow_runner(
        workspace,
        run,
        runner.RunHandle(workspace.id, run["id"]),
    )
    # ``execute()`` refreshes the subject from disk before every stage; the
    # in-memory workspace diverges during materialization (readiness populates
    # each row's rollup projection), so a stage-driving test must do the same.
    command._refresh()
    return command, stage, stage["units"][0]


_TEST_GENERATE_RESPONSE = {
    "tests": [
        {
            "source": "data",
            "title": "Test duplicate payments",
            "objective": "Determine whether duplicate payments occurred",
            "steps": [
                {
                    "label": "Identify duplicates",
                    "instruction": "Identify repeated invoice identifiers.",
                    "table_refs": ["private_ledger"],
                    "code": (
                        "result = private_ledger.filter("
                        "private_ledger['invoice_id'].is_duplicated())"
                    ),
                }
            ],
        }
    ]
}


def test_live_test_generate_capability_commits_through_pipeline_binding(monkeypatch):
    ws = _planning_workspace("Live generated-test binding")
    sentinel = "ROW_SECRET_NEVER_SEND_P7D2"
    ws.add_table(
        "private-ledger.csv",
        pl.DataFrame(
            {"invoice_id": [sentinel, "INV-2"], "amount": [100.0, 200.0]}
        ).write_csv().encode(),
    )
    methodology.save_pack(
        ws,
        "Firm AP Guide",
        "# Duplicate payments\nProcedures should address duplicate-payment risk.",
    )
    command, stage, unit = _test_generate_only_runner(ws)
    captured = {}

    def generate(user):
        captured["user"] = user
        return _TEST_GENERATE_RESPONSE

    fake = FakeAgentLLM({"agent:test_generate": generate})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command._run_stage(stage)

    assert unit["status"] == "succeeded"
    assert stage["status"] == "succeeded"
    reloaded = workspaces.load_workspace(ws.id)
    rcm_id = reloaded.rcm[0]["id"]
    generated = reloaded.data_tests
    assert [item["title"] for item in generated] == ["Test duplicate payments"]
    assert generated[0]["created_by"] == "agent"
    assert generated[0]["status"] == "ready"
    assert generated[0]["semantic_id"] == f"datatest:{rcm_id}:test-duplicate-payments"
    assert generated[0]["workflow_parent_sha1"]
    # Methodology citations come from the declared context, not a run-scoped basis.
    assert generated[0]["methodology_refs"][0]["pack_name"] == "Firm AP Guide"
    assert unit["result_refs"] == [f"datatest:{generated[0]['id']}"]
    assert command.run["planning_changes"]["test_created"] == 1
    # Row-level table values never reach the provider or the durable provenance.
    assert sentinel not in captured["user"]
    assert sentinel not in json.dumps(command.run.get("provenance") or [])
    # The pipeline persisted content-free manifest, proposal, and receipt sidecars.
    assert unit["proposal_sidecar"]["unit_id"] == unit["id"]
    assert unit["receipt_sidecar"]["unit_id"] == unit["id"]
    assert (
        store.run_dir(ws, command.run["id"]) / unit["receipt_sidecar"]["path"]
    ).is_file()


def test_test_generate_resume_reuses_durable_proposal_without_rebilling(monkeypatch):
    ws = _planning_workspace("Generated-test proposal no rebilling")
    ws.add_table(
        "private-ledger.csv",
        pl.DataFrame(
            {"invoice_id": ["INV-1", "INV-2"], "amount": [100.0, 200.0]}
        ).write_csv().encode(),
    )
    fake = FakeAgentLLM({"agent:test_generate": _TEST_GENERATE_RESPONSE})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    command, stage, unit = _test_generate_only_runner(ws)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            tests_executor,
            "mutate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            command._run_stage(stage)

    run_root = store.run_dir(ws, command.run["id"])
    assert [call["tag"] for call in fake.calls] == ["agent:test_generate"]
    assert (run_root / unit["proposal_sidecar"]["path"]).is_file()

    workflow.recovery(command.run["workflow"])
    resumed = build_audit_workflow_runner(
        ws,
        command.run,
        runner.RunHandle(ws.id, command.run["id"]),
    )
    resumed._refresh()
    resumed._run_stage(stage)

    # Resume reused the durable proposal; no second provider call was billed.
    assert [call["tag"] for call in fake.calls] == ["agent:test_generate"]
    assert unit["status"] == "succeeded"
    assert (run_root / unit["receipt_sidecar"]["path"]).is_file()
    assert "proposal_reused" in {
        event["type"] for event in store.read_events(ws, resumed.run["id"])
    }
    assert len(workspaces.load_workspace(ws.id).data_tests) == 1


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
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval inspection",
            "kind": "review",
            "rcm_id": row["id"],
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
    test = doc_tests.create_test(
        ws,
        {
            "title": "Evidence review",
            "kind": "review",
            "rcm_id": row["id"],
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
    first = documents.add_document(ws, "First.txt", b"First approval was documented.")
    second = documents.add_document(ws, "Second.txt", b"Second approval was documented.")
    test = doc_tests.create_test(
        ws,
        {
            "title": "Approval Q&A",
            "kind": "qa",
            "rcm_id": row["id"],
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
    assert merged["state"] == "manual_review"


def test_output_readiness_is_existence_structural_and_not_currency():
    # P7.2A: readiness reports existence and structural usability only. Changing
    # an output's source parents does not make an existing, structurally usable
    # output "stale"; currency is not assessed by the framework. The auditor
    # forces regeneration when changed sources warrant it.
    ws = _planning_workspace("Output invalidation")
    row_id = ws.rcm[0]["id"]
    working_papers.generate_rcm(ws, row_id)
    dashboard.curate_rcm_tiles(ws, run_id="run-output")
    report.generate(ws, use_model=False, run_id="run-output")

    for outcome in ("working_papers.generated", "dashboard.curated", "report.working_draft"):
        assert audit_capabilities.REGISTRY.get(outcome).readiness(ws, {}).state == "satisfied"

    ws.update_rcm(row_id, {"control": "Auditor changed the control after output generation"})
    ws.update_planning({"context": {"entity": "Changed entity"}})

    # The outputs still exist and are structurally usable, so they stay
    # satisfied — no "stale" state is produced by the audit declarations.
    for outcome in ("working_papers.generated", "dashboard.curated", "report.working_draft"):
        assert audit_capabilities.REGISTRY.get(outcome).readiness(ws, {}).state == "satisfied"


def test_partial_workflow_report_discloses_failed_and_missing_coverage(monkeypatch):
    ws = _planning_workspace("Partial workflow report coverage")
    defined_row = ws.add_rcm({
        "process": "Payments",
        "risk": "Duplicate invoices are paid",
        "control": "Duplicate invoice check",
        "risk_rating": "high",
    })
    # An auditor-owned draft on one row is a linked-but-unexecutable test:
    # generation cannot overwrite it without permission, so it surfaces as a
    # missing execution definition rather than a missing plan. The other row
    # (from ``_planning_workspace``) has no test at all, and generation for it
    # fails outright with no worker script — both gap types appear together.
    data_tests.create_draft(
        ws,
        {
            "title": "Test approval workflow",
            "objective": "Determine whether invoices were approved",
            "rcm_id": defined_row["id"],
        },
    )
    ws = workspaces.load_workspace(ws.id)
    fake = FakeAgentLLM({})
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
            "source": "follow_up",
            "text": "Continue through a preliminary report.",
            "requested_outcomes": ["report.working_draft"],
            "generation_mode": "reuse_existing",
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)

    # The run failed units and still committed a labelled preliminary report,
    # which is exactly the partial outcome this status distinguishes.
    assert completed["status"] == "completed_with_failures"
    assert current.report["markdown"]
    assert current.report["generation_warnings"] == [
        "Incomplete planning coverage: 0 planning workflow unit(s) failed and 1 "
        "required planning item(s) are missing.",
        "Incomplete execution-definition coverage: 1 execution-definition workflow "
        "unit(s) failed and 1 required execution definition(s) are missing.",
    ]
    assert "# Preliminary Internal Audit Working Draft" in current.report["markdown"]
    assert "Incomplete execution-definition coverage" in current.report["markdown"]


def test_stage_units_expand_one_per_uncovered_rcm_row(monkeypatch):
    ws = _planning_workspace("One unit per row expansion")
    ws.add_rcm({
        "process": "Payments",
        "risk": "Duplicate invoices",
        "control": "Duplicate invoice check",
        "risk_rating": "high",
    })
    fake = FakeAgentLLM({
        "agent:test_generate": {
            "tests": [{
                "source": "data",
                "title": "New duplicate test",
                "objective": "Identify duplicate payments",
                "steps": [
                    {
                        "label": "Run duplicate analysis",
                        "instruction": "Identify duplicate payment identifiers.",
                        "table_refs": ["transactions"],
                        "code": (
                            "result = transactions.filter("
                            "transactions['invoice'].is_duplicated())"
                        ),
                    }
                ],
            }]
        },
    })
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
            "text": "Write the test specifications",
            "requested_outcomes": ["tests.specified"],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)
    definitions = next(
        stage for stage in completed["workflow"]["stages"]
        if stage["capability"] == "tests.specified"
    )

    assert completed["status"] == "completed"
    assert len(definitions["units"]) == 2
    assert {unit["status"] for unit in definitions["units"]} == {"succeeded"}
    assert len(current.data_tests) == 2


def test_missing_document_evidence_blocks_only_execution_branch(monkeypatch):
    ws = _planning_workspace("Missing workflow evidence")
    # An unrelated, non-planning-relevant document makes the document source
    # available at all without perturbing planning readiness (a voucher is
    # excluded from planning-context synthesis by the same category rule);
    # the required REQ-404 evidence is still missing, which is the condition
    # under test.
    documents.add_document(
        ws, "Invoice 99.txt", b"Invoice 99 was paid to a vendor.",
        category="voucher",
    )
    ws = workspaces.load_workspace(ws.id)
    fake = FakeAgentLLM(
        {
            "agent:test_generate": {
                "tests": [
                    {
                        "source": "document",
                        "title": "Approval evidence inquiry",
                        "objective": "Determine whether REQ-404 was approved.",
                        "steps": [
                            {
                                "label": "Was REQ-404 approved?",
                                "instruction": (
                                    "Determine whether REQ-404 was approved "
                                    "before commitment."
                                ),
                                "mode": "question",
                                "document_ids": [],
                                "question": "Was REQ-404 approved before commitment?",
                                "missing_evidence": (
                                    "The approval package for REQ-404 is not imported."
                                ),
                            }
                        ],
                    }
                ]
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
                "tests.specified", "fieldwork.executed", "results.rolled_up",
                "report.working_draft", "audit.verified",
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


def test_permission_mode_exceptions_proceed_directly_to_finding_batch(monkeypatch):
    ws = _planning_workspace("Permission dispositions")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1], "amount": [10.0, 10.0]}).write_csv().encode(),
    )
    row = ws.rcm[0]
    test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify duplicates",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice"]}},
            "rcm_id": row["id"],
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
    assert not any(item.get("type") == "observation_disposition" for item in completed.get("interactions") or [])
    assert refreshed.observations[0]["outcome"] == "exception"
    assert refreshed.findings[0]["source_observation_id"] == observation["id"]
    assert refreshed.findings[0]["auditor_confirmed"] is False


def test_observation_exceptions_do_not_create_a_checkpoint(monkeypatch):
    """An exception directly reaches the normal finding-approval batch."""
    ws = _planning_workspace("Checkpoint pause/resume")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1], "amount": [10.0, 10.0]}).write_csv().encode(),
    )
    row = ws.rcm[0]
    test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify duplicates",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice"]}},
            "rcm_id": row["id"],
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

    def _await(predicate, *, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = store.load_run(ws, started["id"])
            if predicate(current):
                return current
            time.sleep(0.02)
        raise AssertionError("Timed out waiting on run condition.")

    # No observation interaction pauses the run. The model exception is
    # immediately eligible for the ordinary finding approval batch.
    resolved = _await(
        lambda run: any(
            item.get("kind") == "finding_drafts" and item.get("status") == "pending"
            for item in run.get("approvals") or []
        )
    )
    approval = next(
        item
        for item in resolved["approvals"]
        if item.get("kind") == "finding_drafts" and item.get("status") == "pending"
    )
    runner.resolve_approval(
        ws,
        started["id"],
        approval["id"],
        [{"item_id": item["id"], "action": "approve"} for item in approval["items"]],
    )
    completed = wait_run(ws, started["id"])
    refreshed = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert not any(item.get("type") == "observation_disposition" for item in completed.get("interactions") or [])
    assert refreshed.observations[0]["outcome"] == "exception"
    assert refreshed.findings[0]["source_observation_id"] == observation["id"]


def test_full_workflow_runs_capability_closure_and_records_exception_observations(monkeypatch):
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
                    "## Data analytics performed\nNo data analysis has been performed.\n\n"
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
                        "new_risk_reason": "No current RCM row covers duplicate invoices.",
                    }
                ]
            },
            "agent:test_generate": {
                "tests": [
                    {
                        "source": "data",
                        "title": "Test duplicate invoices",
                        "objective": "Determine whether invoice identifiers repeat",
                        "steps": [
                            {
                                "label": "Identify duplicates",
                                "instruction": "Identify repeated invoice identifiers.",
                                "table_refs": ["transactions"],
                                "code": (
                                    "result = transactions.filter("
                                    "transactions['invoice'].is_duplicated())"
                                ),
                            }
                        ],
                    }
                ]
            },
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

    # The generated finding remains a draft until an auditor confirms it, so
    # report-quality verification correctly leaves the run open. Auto mode
    # records the model exception directly and drafts the finding.
    assert completed["status"] == "completed_with_open_items"
    assert "agent:command_interpreter" not in {call["tag"] for call in fake.calls}
    assert {call["tag"] for call in fake.calls} >= {
        "agent:apm", "agent:rcm", "agent:test_generate",
    }
    assert current.data_tests[0]["last_run"]
    assert current.observations and current.observations[0]["outcome"] == "exception"
    assert current.findings
    assert current.report.get("markdown")
    assert completed["usage"]["model_call_metrics"]
    assert completed["usage"]["request_characters"] > 0
    assert completed["usage"]["prompt_tokens"] >= completed["usage"]["estimated_prompt_tokens"]
    model_stages = {
        stage["capability"]: stage
        for stage in completed["workflow"]["stages"]
        if stage["capability"] in {
            "planning.apm_ready", "planning.rcm_ready", "tests.specified",
        }
    }
    assert all(
        unit["proposal_sidecar"]
        for stage in model_stages.values()
        for unit in stage["units"]
    )
