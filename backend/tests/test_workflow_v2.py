import asyncio
import inspect
import json
import threading
import time
from dataclasses import replace

import httpx
import polars as pl
import pytest

from app import dashboard, data_tests, doc_tests, document_analysis, documents, llm, methodology, rcm_execution, report, working_papers, workspaces
from app.agent import action_runner, audit_capabilities, audit_workers, context_bundles, runner, store, workflow
from app.agent import workflow_runner as workflow_runner_module
from app.agent.context import (
    PRESETS,
    SELECTORS,
    ContextPreset,
    ContextResolver,
    PresetRegistry,
    SelectorRegistry,
)
from app.agent.workflow_runner import WorkflowRunner, _local_resolution, initialize_known_workflow
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
    assert persisted["engine"] == store.WORKFLOW_ENGINE
    assert persisted["schema_version"] == 3
    assert persisted["workflow"]["requested_outcomes"] == audit_capabilities.FULL_AUDIT_OUTCOMES
    assert persisted["workflow"]["resolved_capabilities"] == audit_capabilities.REGISTRY.closure(
        audit_capabilities.FULL_AUDIT_OUTCOMES
    )
    assert "prepared_planning" not in persisted
    assert persisted["usage"]["llm_turns"] == 0


@pytest.mark.parametrize(
    ("template", "route", "outcomes"),
    [
        ("full_audit_working_draft", "workflow", audit_capabilities.FULL_AUDIT_OUTCOMES),
        (
            "planning",
            "workflow",
            ["planning.apm_ready", "planning.rcm_ready", "planning.planned_tests_ready"],
        ),
        ("apm_only", "workflow", ["planning.apm_ready"]),
        ("report", "workflow", ["report.working_draft", "audit.verified"]),
        ("data_analysis", "generic_action", []),
        ("document_testing", "generic_action", []),
    ],
)
def test_every_registered_goal_template_has_a_deterministic_local_route(
    template, route, outcomes
):
    assert set(action_runner.GOAL_TEMPLATES) == {
        "full_audit_working_draft",
        "planning",
        "apm_only",
        "data_analysis",
        "document_testing",
        "report",
    }

    resolution = _local_resolution(
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
        {
            "source": "goal_template",
            "text": "Analyze the available data",
            "goal_template": "data_analysis",
        },
    )

    assert initialize_known_workflow(workspace_with_data, run) is False
    persisted = store.load_run(workspace_with_data, run["id"])
    assert persisted["engine"] == store.ACTION_ENGINE
    assert persisted["command_route"]["route"] == "generic_action"


@pytest.mark.parametrize(
    ("text", "outcomes"),
    [
        ("Do a full audit", audit_capabilities.FULL_AUDIT_OUTCOMES),
        ("Complete the audit", audit_capabilities.FULL_AUDIT_OUTCOMES),
        ("Run an end-to-end audit", audit_capabilities.FULL_AUDIT_OUTCOMES),
        ("Draft the audit planning memorandum", ["planning.apm_ready"]),
        ("Generate the risk and control matrix", ["planning.rcm_ready"]),
        ("Create the planned procedures", ["planning.planned_tests_ready"]),
        ("Translate planned work into executable tests", ["fieldwork.definitions_ready"]),
        ("Execute the RCM tests", ["fieldwork.executed", "results.rolled_up"]),
        ("Draft eligible findings", ["findings.drafted"]),
        ("Generate the audit report", ["report.working_draft"]),
    ],
)
def test_common_broad_audit_phrases_fail_closed_to_workflow(text, outcomes):
    resolution = _local_resolution({"source": "chat", "text": text})

    assert resolution is not None
    assert resolution["route"] == "workflow"
    assert resolution["requested_outcomes"] == outcomes
    assert resolution["action_intent"] is None


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
                "route": "generic_action",
                "requested_outcomes": [],
                "objective": "Check the report quality",
                "target_refs": [],
                "refresh_policy": "missing_or_stale",
                "action_intent": "quality_check",
                "constraints": [],
                "needs_clarification": False,
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
    assert completed["command_route"]["route"] == "generic_action"
    assert [call["tag"] for call in fake.calls] == [
        "agent:workflow_router",
        "agent:command_interpreter",
    ]
    assert [action["type"] for action in completed["actions"]] == [
        "run_report_quality"
    ]


def test_bounded_router_cannot_send_broad_audit_into_action_planner(
    monkeypatch, workspace_with_data,
):
    fake = FakeAgentLLM(
        {
            "agent:workflow_router": {
                "route": "generic_action",
                "requested_outcomes": [],
                "objective": "Perform the entire audit",
                "target_refs": [],
                "refresh_policy": "missing_or_stale",
                "action_intent": "generic",
                "constraints": [],
                "needs_clarification": False,
                "clarification": None,
            },
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
        {"source": "chat", "text": "Perform the entire audit"},
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["engine"] == store.ACTION_ENGINE
    assert completed["status"] == "failed"
    assert "must use workflow routing" in completed["error"]
    assert completed["actions"] == []
    assert [call["tag"] for call in fake.calls] == ["agent:workflow_router"]


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

    assert initialize_known_workflow(ws, run) is True
    persisted = store.load_run(ws, run["id"])
    assert persisted["workflow"]["requested_outcomes"] == ["planning.apm_ready"]
    assert [
        stage["capability"] for stage in persisted["workflow"]["stages"]
    ] == ["planning.context_ready", "planning.apm_ready"]
    assert persisted["interactions"] == []
    assert persisted["usage"]["llm_turns"] == 0


def test_audit_workflow_declares_the_complete_lifecycle_graph():
    expected_dependencies = {
        "planning.context_ready": (),
        "planning.apm_ready": ("planning.context_ready",),
        "planning.rcm_ready": ("planning.apm_ready",),
        "planning.planned_tests_ready": ("planning.rcm_ready",),
        "fieldwork.definitions_ready": ("planning.planned_tests_ready",),
        "fieldwork.executed": ("fieldwork.definitions_ready",),
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
        "planning.context_ready",
        "planning.apm_ready",
        "planning.rcm_ready",
        "planning.planned_tests_ready",
        "fieldwork.definitions_ready",
        "fieldwork.executed",
        "results.rolled_up",
        "findings.drafted",
        "working_papers.generated",
        "dashboard.curated",
        "report.working_draft",
        "audit.verified",
    ]
    position = {capability_id: index for index, capability_id in enumerate(resolved)}
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


def test_repeated_materialization_preserves_semantic_unit_identity():
    ws = _planning_workspace("Stable semantic units")
    scope = {"target_refs": ["workspace:current"], "refresh_policy": "force"}

    first = workflow.materialize(
        audit_capabilities.REGISTRY,
        ws,
        ["planning.planned_tests_ready"],
        scope,
    )
    second = workflow.materialize(
        audit_capabilities.REGISTRY,
        workspaces.load_workspace(ws.id),
        ["planning.planned_tests_ready"],
        dict(scope),
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


def test_workflow_planned_test_repair_reports_all_contract_errors(monkeypatch):
    ws = _planning_workspace("Planned test contract repair")
    attempts = 0

    def planned_tests(user):
        nonlocal attempts
        attempts += 1
        base = {
            "operation": "create",
            "stable_slug": "duplicate-payments",
            "title": "Test duplicate payments",
            "objective": "Determine whether duplicate payments occurred",
            "criteria": "Each invoice is paid once.",
            "method": "data_analytics",
            "expected_evidence": "Duplicate listing",
        }
        if attempts == 1:
            return {
                "planned_tests": [{
                    **base,
                    "steps": "Identify repeated invoice identifiers.",
                    "sampling": "Full population",
                    "thresholds": "Zero duplicates",
                }]
            }
        assert "planned_tests[0].steps" in user
        assert "planned_tests[0].sampling must be an object" in user
        assert "planned_tests[0].thresholds must be an object" in user
        return {
            "planned_tests": [{
                **base,
                "steps": ["Identify repeated invoice identifiers."],
                "sampling": {"strategy": "full_population", "size": None, "seed": 42},
                "thresholds": {"maximum_duplicates": 0},
            }]
        }

    fake = FakeAgentLLM({"agent:work_program": planned_tests})
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
            "text": "Draft planned tests",
            "requested_outcomes": ["planning.planned_tests_ready"],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert attempts == 2
    assert current.rcm[0]["planned_tests"][0]["sampling"] == {
        "strategy": "full_population", "size": None, "seed": 42, "stratify_by": None,
    }


def test_planned_test_validator_rejects_noncanonical_sampling_fields():
    with pytest.raises(ValueError, match=r"planned_tests\[0\]\.sampling\.sample_size is not supported"):
        audit_workers.validate_planned_tests(
            {
                "planned_tests": [{
                    "operation": "create",
                    "stable_slug": "duplicates",
                    "title": "Test duplicates",
                    "objective": "Identify duplicates",
                    "criteria": "Identifiers are unique.",
                    "steps": ["Identify repeated identifiers."],
                    "method": "data_analytics",
                    "expected_evidence": "Duplicate listing",
                    "sampling": {"sample_size": 10},
                }]
            },
            "RCM-TEST",
        )


def test_document_test_validator_rejects_non_object_spec():
    ws = _planning_workspace("Document spec validation")
    with pytest.raises(ValueError, match="document_test spec must be an object"):
        audit_workers.validate_document_test(
            {
                "document_test": {
                    "title": "Review policy",
                    "kind": "review",
                    "spec": "Review the policy",
                    "items": [{"label": "Policy", "summary": "Review"}],
                }
            },
            ws,
        )


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


def test_data_test_context_contains_schema_metadata_but_no_table_rows():
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
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Test duplicate invoice IDs",
            "objective": "Identify duplicate invoices",
            "criteria": "Invoice IDs should be unique",
            "steps": ["Run duplicate analysis."],
            "method": "data_analytics",
            "expected_evidence": "Duplicate-analysis output",
        },
    )

    bundle = context_bundles.data_test_spec(ws, row, planned)
    serialized = bundle.serialized()

    assert "private_ledger" in serialized
    assert "invoice_id" in serialized
    assert "ROW_SECRET_NEVER_SEND_7F4C" not in serialized
    assert bundle.total_characters <= bundle.character_budget
    assert set(bundle.sections["table_schemas"][0]) == {"table", "rows", "columns"}


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


def test_parallel_candidate_reuses_durable_sidecar_without_model_rebilling():
    ws = _planning_workspace("Proposal sidecar reuse")
    run = store.new_command_run(
        ws, "auto", {"source": "chat", "text": "generate planned tests"}
    )
    proposal = {"planned_tests": [{"title": "Cached proposal"}]}
    unit = {
        "id": "unit:cached",
        "kind": "planned_tests",
        "title": "Cached planned-test proposal",
        "capability": "planning.planned_tests_ready",
        "parent_refs": [],
        "status": "queued",
        "attempts": 0,
        "input_sha1": "input-sha1",
        "proposal_sidecar": store.write_sidecar(ws, run["id"], proposal),
        "result_refs": [],
        "error": None,
        "started_at": None,
        "finished_at": None,
    }
    stage = {
        "id": "stage:planned-tests",
        "capability": "planning.planned_tests_ready",
        "units": [unit],
    }
    run["schema_version"] = 3
    run["workflow"] = {"stages": [stage]}
    store.save_run(ws, run)
    worker_calls = 0

    def worker(_unit):
        nonlocal worker_calls
        worker_calls += 1
        raise AssertionError("a valid proposal sidecar must bypass the model worker")

    command = WorkflowRunner(ws, run, runner.RunHandle(ws.id, run["id"]))
    before_turns = run["usage"]["llm_turns"]
    candidates = command._parallel_candidates(stage, stage["units"], worker)

    assert candidates == [(unit, proposal)]
    assert worker_calls == 0
    assert run["usage"]["llm_turns"] == before_turns
    assert unit["status"] == "running"
    assert "proposal_reused" in {
        event["type"] for event in store.read_events(ws, run["id"])
    }


def _apm_only_runner(workspace, *, context_resolver=None):
    run = store.new_command_run(
        workspace,
        "auto",
        {
            "source": "follow_up",
            "text": "Regenerate the APM",
            "requested_outcomes": ["planning.apm_ready"],
            "refresh_policy": "force",
        },
    )
    assert initialize_known_workflow(workspace, run) is True
    run = store.load_run(workspace, run["id"])
    stage = next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == "planning.apm_ready"
    )
    command = WorkflowRunner(
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
            "refresh_policy": "missing_or_stale",
        },
    )
    assert initialize_known_workflow(ws, run) is True
    run = store.load_run(ws, run["id"])
    command = WorkflowRunner(
        ws,
        run,
        runner.RunHandle(ws.id, run["id"]),
        context_resolver=ResolverMustNotRun(),
    )

    command.execute()

    assert command.run["status"] == "completed"
    assert "planning.apm_ready" in command.run["workflow"]["reused_capabilities"]


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
            workflow_runner_module,
            "mutate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            command._apm(stage, [unit])

    first_sidecar = store.read_sidecar(
        ws,
        command.run["id"],
        unit["proposal_sidecar"],
    )
    first_identity = first_sidecar["execution_identity"]
    assert [call["tag"] for call in fake.calls] == ["agent:apm"]

    workflow.recovery(command.run["workflow"])
    resumed = WorkflowRunner(
        ws,
        command.run,
        runner.RunHandle(ws.id, command.run["id"]),
        context_resolver=_changed_apm_context_resolver(change),
    )
    resumed._apm(stage, [unit])

    second_sidecar = store.read_sidecar(
        resumed.ws,
        resumed.run["id"],
        unit["proposal_sidecar"],
    )
    assert second_sidecar["execution_identity"] != first_identity
    assert [call["tag"] for call in fake.calls] == ["agent:apm", "agent:apm"]
    assert unit["status"] == "succeeded"
    assert "proposal_reuse_rejected" in {
        event["type"] for event in store.read_events(resumed.ws, resumed.run["id"])
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
        "## Key risks and planned response\nTest approval evidence."
    )
    command, stage, unit = _apm_only_runner(ws)
    captured = {}

    def draft(user):
        captured["user"] = user
        reference = unit.get("context_manifest")
        assert reference
        manifest = command.load_context_manifest(reference)
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
    command._apm(stage, [unit])

    assert methodology_text in captured["user"]
    assert "private_ledger" in captured["user"]
    assert "invoice_id" in captured["user"]
    assert sentinel not in captured["user"]
    assert methodology_text not in captured["manifest"]
    assert sentinel not in captured["manifest"]
    assert sentinel not in json.dumps(command.run.get("provenance") or [])
    assert "context_bundles.apm" not in inspect.getsource(WorkflowRunner._apm)
    worker_source = inspect.getsource(audit_workers.apm)
    assert ".ws" not in worker_source
    assert "load_workspace" not in worker_source
    assert list(inspect.signature(audit_workers.apm).parameters) == [
        "model_call",
        "bundle",
        "quality_gate",
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


def test_partial_workflow_report_discloses_failed_and_missing_coverage(monkeypatch):
    ws = _planning_workspace("Partial workflow report coverage")
    defined_row = ws.add_rcm({
        "process": "Payments",
        "risk": "Duplicate invoices are paid",
        "control": "Duplicate invoice check",
        "risk_rating": "high",
    })
    ws.add_planned_test(defined_row["id"], {
        "title": "Test duplicate invoices",
        "objective": "Determine whether duplicate invoices were paid",
        "criteria": "Each invoice is paid once.",
        "steps": ["Identify duplicate invoice identifiers."],
        "method": "data_analytics",
        "expected_evidence": "Exception listing",
    })
    fake = FakeAgentLLM({
        "agent:work_program": {
            "planned_tests": [{
                "operation": "create",
                "stable_slug": "approval-workflow",
                "title": "Test approval workflow",
                "objective": "Determine whether invoices were approved",
                "criteria": "Invoices require approval.",
                "steps": ["Inspect invoice approval status."],
                "method": "data_analytics",
                "expected_evidence": "Approval exception listing",
                "sampling": "Full population",
                "thresholds": {},
            }]
        },
        "agent:data_test_spec": {"data_test": "not an object"},
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
            "source": "follow_up",
            "text": "Continue through a preliminary report.",
            "requested_outcomes": ["report.working_draft"],
            "refresh_policy": "missing_or_stale",
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)

    assert completed["status"] == "failed"
    assert current.report["markdown"]
    assert current.report["generation_warnings"] == [
        "Incomplete planning coverage: 1 planning workflow unit(s) failed and "
        "1 required planning item(s) are missing.",
        "Incomplete execution-definition coverage: 1 execution-definition workflow "
        "unit(s) failed and 1 required execution definition(s) are missing.",
    ]
    assert "# Preliminary Internal Audit Working Draft" in current.report["markdown"]
    assert "Incomplete planning coverage" in current.report["markdown"]
    assert "Incomplete execution-definition coverage" in current.report["markdown"]


def test_stage_units_expand_after_upstream_planned_tests_are_created(monkeypatch):
    ws = _planning_workspace("Dynamic definition expansion")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1, 2]}).write_csv().encode(),
    )
    existing_row = ws.add_rcm({
        "process": "Payments",
        "risk": "Duplicate invoices",
        "control": "Duplicate invoice check",
        "risk_rating": "high",
    })
    ws.add_planned_test(
        existing_row["id"],
        {
            "title": "Existing duplicate test",
            "objective": "Identify duplicate invoices",
            "criteria": "Invoice identifiers are unique.",
            "steps": ["Run duplicate analysis."],
            "method": "data_analytics",
            "expected_evidence": "Duplicate listing",
        },
    )
    fake = FakeAgentLLM({
        "agent:work_program": {
            "planned_tests": [{
                "operation": "create",
                "stable_slug": "new-duplicate-test",
                "title": "New duplicate test",
                "objective": "Identify duplicate payments",
                "criteria": "Payment identifiers are unique.",
                "steps": ["Run duplicate analysis."],
                "method": "data_analytics",
                "expected_evidence": "Duplicate listing",
                "sampling": {"strategy": "full_population", "seed": 42},
                "thresholds": {"maximum_duplicates": 0},
            }]
        },
        "agent:data_test_spec": {
            "data_test": {
                "title": "Duplicate identifiers",
                "objective": "Identify duplicate identifiers",
                "engine": "analytics",
                "table_refs": ["transactions"],
                "spec": {"test_id": "duplicates", "params": {"columns": ["invoice"]}},
            }
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
            "text": "Complete execution definitions",
            "requested_outcomes": ["fieldwork.definitions_ready"],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)
    definitions = next(
        stage for stage in completed["workflow"]["stages"]
        if stage["capability"] == "fieldwork.definitions_ready"
    )

    assert completed["status"] == "completed"
    assert len(definitions["units"]) == 2
    assert {unit["status"] for unit in definitions["units"]} == {"succeeded"}
    assert len(current.data_tests) == 2


def test_data_test_definition_repairs_unknown_analytics_id(monkeypatch):
    ws = _planning_workspace("Analytics definition repair")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1, 2], "amount": [10.0, 10.0, 20.0]}).write_csv().encode(),
    )
    row = ws.rcm[0]
    planned = ws.add_planned_test(
        row["id"],
        {
            "title": "Test duplicate invoices",
            "objective": "Identify duplicate invoices",
            "criteria": "Invoice identifiers are unique.",
            "steps": ["Run duplicate analysis."],
            "method": "data_analytics",
            "expected_evidence": "Duplicate listing",
        },
    )
    attempts = 0

    def definition(user):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            test_id = planned["id"]
        elif attempts == 2:
            assert "Unknown analytics test" in user
            test_id = "still-not-a-test"
        else:
            assert "Unknown analytics test" in user
            test_id = "duplicates"
        return {
            "data_test": {
                "title": "Duplicate invoices",
                "objective": "Identify duplicate invoice identifiers",
                "engine": "analytics",
                "table_refs": ["transactions"],
                "spec": {"test_id": test_id, "params": {"columns": ["invoice"]}},
            }
        }

    fake = FakeAgentLLM({"agent:data_test_spec": definition})
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
            "text": "Create execution definitions",
            "requested_outcomes": ["fieldwork.definitions_ready"],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)
    stage = next(
        value for value in completed["workflow"]["stages"]
        if value["capability"] == "fieldwork.definitions_ready"
    )
    proposal = store.read_sidecar(current, completed["id"], stage["units"][0]["proposal_sidecar"])

    assert completed["status"] == "completed"
    assert attempts == 3
    assert current.data_tests[0]["spec"]["test_id"] == "duplicates"
    assert proposal["spec"]["test_id"] == "duplicates"


def test_data_test_definition_repairs_sql_like_validation_rule(monkeypatch):
    ws = _planning_workspace("Validation definition repair")
    ws.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, None, 2]}).write_csv().encode(),
    )
    row = ws.rcm[0]
    ws.add_planned_test(
        row["id"],
        {
            "title": "Validate invoice identifiers",
            "objective": "Identify missing invoice identifiers",
            "criteria": "Invoice identifiers are present.",
            "steps": ["Validate invoice identifiers."],
            "method": "validation",
            "expected_evidence": "Validation exceptions",
        },
    )
    attempts = 0

    def definition(user):
        nonlocal attempts
        attempts += 1
        rules = (
            [{"name": "invoice_required", "code": "SELECT * FROM transactions"}]
            if attempts == 1
            else [{"check": "required", "column": "invoice", "params": {}}]
        )
        if attempts == 2:
            assert "Unknown check" in user
        return {
            "data_test": {
                "title": "Required invoice identifiers",
                "objective": "Identify missing invoice identifiers",
                "engine": "validation",
                "table_refs": ["transactions"],
                "spec": {"rules": rules},
            }
        }

    fake = FakeAgentLLM({"agent:data_test_spec": definition})
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
            "text": "Create validation definition",
            "requested_outcomes": ["fieldwork.definitions_ready"],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)

    assert completed["status"] == "completed"
    assert attempts == 2
    assert current.data_tests[0]["spec"]["rules"][0]["check"] == "required"


def test_document_test_definition_repairs_comparison_method(monkeypatch):
    ws = _planning_workspace("Document definition repair")
    document = documents.add_document(ws, "Approval.txt", b"Management approved the request.")
    row = ws.rcm[0]
    ws.add_planned_test(
        row["id"],
        {
            "title": "Inspect approval evidence",
            "objective": "Determine whether approval was documented",
            "criteria": "Management approval is present.",
            "steps": ["Inspect the approval evidence."],
            "method": "document_inspection",
            "expected_evidence": "Approval document",
        },
    )
    attempts = 0

    def definition(user):
        nonlocal attempts
        attempts += 1
        method = "Document Content Analysis" if attempts == 1 else "normalized"
        if attempts == 2:
            assert "Unknown comparison method" in user
        return {
            "document_test": {
                "title": "Approval evidence",
                "kind": "vouching",
                "spec": {},
                "items": [{
                    "label": "Management approval",
                    "document_ids": [document["id"]],
                    "checks": [{
                        "field": "approval",
                        "expected": "Management approved",
                        "method": method,
                    }],
                }],
            }
        }

    fake = FakeAgentLLM({"agent:document_test_spec": definition})
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
            "text": "Create document definition",
            "requested_outcomes": ["fieldwork.definitions_ready"],
        },
    )
    completed = wait_run(ws, started["id"])
    current = workspaces.load_workspace(ws.id)
    test = doc_tests.load_test(current, doc_tests.list_tests(current)[0]["id"])

    assert completed["status"] == "completed"
    assert attempts == 2
    assert test["items"][0]["checks"][0]["method"] == "normalized"


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
