import json
import time

import polars as pl
import pytest

from app import assistant, doc_tests, documents, findings, llm, model_context, workspaces
from app.agent import actions, artifact_index, command_runner, ledger, runner, store
from conftest import FakeAgentLLM, wait_run


def configured(monkeypatch, response):
    fake = FakeAgentLLM({"agent:command_interpreter": response})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"})
    return fake


def test_schema_v2_round_trip_and_legacy_projection(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "run duplicate testing"})
    ledger.append_actions(run, [{"id": "a1", "type": "run_analytics", "args": {"table": "transactions", "test": "duplicates", "params": {"columns": ["invoice_no"]}}}])
    store.save_run(workspace_with_data, run)
    loaded = store.load_run(workspace_with_data, run["id"])
    assert loaded["schema_version"] == 2
    assert loaded["actions"][0]["definition_version"] == 1
    assert loaded["plan"]["stages"][0]["tasks"][0]["id"] == "a1"


def test_registry_and_graph_reject_invalid_contracts(workspace_with_data):
    assert {item.type for item in actions.REGISTRY.all()} >= {"edit_finding", "delete_finding", "generate_report"}
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test"})
    with pytest.raises(workspaces.WorkspaceError, match="Unknown agent action"):
        ledger.append_actions(run, [{"type": "write_json", "args": {}}])
    with pytest.raises(workspaces.WorkspaceError, match="unknown action"):
        ledger.append_actions(run, [{"id": "a", "type": "run_report_quality", "args": {}, "depends_on": ["missing"]}])


def test_singleton_target_kind_is_normalized_before_validation(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "run the document test"})
    action = ledger.append_actions(run, [{
        "id": "run-test", "type": "run_document_test", "args": {},
        "target": {"resolved_id": "DT-EXISTING"},
    }])[0]

    assert action["target"] == {
        "kind": "doctest", "selector": None, "resolved_id": "DT-EXISTING",
    }


def test_command_interpreter_repairs_semantically_invalid_action_graph(monkeypatch, workspace_with_data):
    def response(user):
        common = {
            "objective": "Check report quality", "constraints": [],
            "completion_criteria": ["quality checked"], "needs_planning_wave": False,
        }
        if "previous JSON parsed" not in user:
            return {
                **common,
                "actions": [{
                    "id": "bad-target", "type": "run_document_test", "args": {},
                    "target": {"kind": "document_test", "resolved_id": "DT-1"},
                }],
            }
        return {**common, "actions": [{"id": "quality", "type": "run_report_quality", "args": {}}]}

    fake = FakeAgentLLM({"agent:command_interpreter": response})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"})

    started = runner.start_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "check report quality"}
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["status"] == "completed"
    assert [item["type"] for item in completed["actions"]] == ["run_report_quality"]
    assert [call["tag"] for call in fake.calls] == [
        "agent:command_interpreter", "agent:command_interpreter",
    ]
    rejected = completed["rejected_proposals"][0]
    assert rejected["stage"] == "command_interpreter"
    assert rejected["actions"][0]["target"]["kind"] == "document_test"
    assert "requires target kind: doctest" in rejected["error"]


def test_command_interpreter_receives_schema_and_canonicalizes_fields(
    monkeypatch, workspace_with_data
):
    def interpret(user):
        payload = json.loads(user)
        schemas = {item["table"]: item for item in payload["table_schemas"]}
        transaction_fields = {item["name"] for item in schemas["transactions"]["columns"]}
        assert {"cust_id", "invoice_no", "amount"} <= transaction_fields
        analytics_ids = {item["id"] for item in payload["analytics_tests"]}
        assert {"duplicates", "benford", "sampling"} <= analytics_ids
        run_analytics = next(
            item for item in payload["action_catalog"]
            if item["type"] == "run_analytics"
        )
        test_schema = run_analytics["input_schema"]["properties"]["test"]
        assert set(test_schema["enum"]) == analytics_ids
        return {
            "objective": "Join transactions to customers",
            "constraints": [],
            "completion_criteria": "join created",
            "actions": [
                {
                    "id": "join",
                    "type": "create_join",
                    "args": {
                        "name": "enriched",
                        "left": "transactions",
                        "right": "customers",
                        "left_on": ["CUST_ID"],
                        "right_on": ["ID"],
                        "how": "left",
                    },
                },
                {
                    "id": "duplicates",
                    "type": "run_analytics",
                    "args": {
                        "table": "transactions",
                        "test": "duplicate_detection",
                        "params": {"columns": ["INVOICE_NO"]},
                    },
                },
            ],
        }

    configured(monkeypatch, interpret)
    started = runner.start_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "join customer data"}
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["status"] == "completed"
    assert completed["goal"]["completion_criteria"] == ["join created"]
    assert completed["actions"][0]["args"]["left_on"] == ["cust_id"]
    assert completed["actions"][0]["args"]["right_on"] == ["id"]
    assert completed["actions"][1]["args"]["test"] == "duplicates"
    assert completed["actions"][1]["args"]["params"]["columns"] == ["invoice_no"]
    assert workspaces.load_workspace(workspace_with_data.id).joins[0]["name"] == "enriched"


def test_command_interpreter_exposes_checks_and_canonicalizes_not_null_alias(
    monkeypatch, workspace_with_data
):
    def interpret(user):
        payload = json.loads(user)
        check_ids = {item["id"] for item in payload["validation_checks"]}
        assert {"required", "range", "unique", "referential"} <= check_ids
        create_rules = next(
            item for item in payload["action_catalog"]
            if item["type"] == "create_validation_rules"
        )
        check_schema = (
            create_rules["input_schema"]["properties"]["rules"]
            ["items"]["properties"]["check"]
        )
        assert set(check_schema["enum"]) == check_ids
        return {
            "objective": "Validate transaction completeness",
            "constraints": [],
            "completion_criteria": ["Rules saved"],
            "needs_planning_wave": False,
            "actions": [{
                "id": "rules",
                "type": "create_validation_rules",
                "args": {
                    "title": "Transaction completeness",
                    "table": "transactions",
                    "rules": [{"column": "invoice_no", "check": "is_not_null"}],
                },
            }],
        }

    fake = configured(monkeypatch, interpret)
    started = runner.start_command_run(
        workspace_with_data, "auto",
        {
            "source": "goal_template", "text": "Start data analysis",
            "goal_template": "data_analysis",
        },
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["status"] == "completed"
    assert completed["actions"][0]["args"]["rules"][0]["check"] == "required"
    saved = workspaces.load_workspace(workspace_with_data.id).rulesets[0]
    assert saved["rules"][0]["check"] == "required"
    assert [call["tag"] for call in fake.calls] == ["agent:command_interpreter"]


def test_command_interpreter_repair_lists_supported_validation_checks():
    message = command_runner.CommandRunner._proposal_repair_user(
        "base", {"actions": []}, workspaces.WorkspaceError("Unknown check 'invented'.")
    )

    assert "Supported validation check ids are:" in message
    assert "required" in message
    assert "Use `required` for null or blank checks." in message


def test_command_interpreter_reports_all_unsupported_analytics_and_repairs(
    monkeypatch, workspace_with_data
):
    def interpret(user):
        common = {
            "objective": "Analyze procurement",
            "constraints": [],
            "completion_criteria": ["Analysis planned"],
            "needs_planning_wave": False,
        }
        if "previous JSON parsed" not in user:
            return {
                **common,
                "actions": [
                    {
                        "id": "three-way", "type": "run_analytics",
                        "args": {"table": "transactions", "test": "three_way_match"},
                    },
                    {
                        "id": "approvals", "type": "run_analytics",
                        "args": {"table": "transactions", "test": "approval_limits"},
                    },
                ],
            }
        assert "Unknown analytics tests: approval_limits, three_way_match" in user
        assert "Supported analytics test ids are:" in user
        assert "Replace every unsupported run_analytics action" in user
        assert "create_custom_analysis" in user
        return {**common, "actions": []}

    fake = configured(monkeypatch, interpret)
    started = runner.start_command_run(
        workspace_with_data, "auto",
        {
            "source": "goal_template", "text": "Analyze data",
            "goal_template": "data_analysis",
        },
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["status"] == "completed"
    assert completed["rejected_proposals"][0]["error"] == (
        "Unknown analytics tests: approval_limits, three_way_match."
    )
    assert [call["tag"] for call in fake.calls] == [
        "agent:command_interpreter", "agent:command_interpreter",
    ]


def test_custom_analysis_contract_requires_executable_code(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "analyze data"}
    )
    with pytest.raises(workspaces.WorkspaceError, match="args.spec.code is required"):
        ledger.append_actions(run, [{
            "id": "analysis",
            "type": "create_custom_analysis",
            "args": {"title": "Broken", "spec": {"steps": []}},
        }])


def test_full_audit_lifecycle_dependencies_and_document_test_binding(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto",
        {"source": "goal_template", "goal_template": "full_audit_working_draft"},
    )
    created = ledger.append_actions(run, [
        {"id": "quality", "type": "run_report_quality", "args": {}},
        {"id": "report", "type": "generate_report", "args": {"use_model": False}},
        {"id": "finding", "type": "create_finding", "args": {"title": "Exception"}},
        {"id": "paper", "type": "generate_working_paper", "args": {}},
        {"id": "run-test", "type": "run_document_test", "args": {}},
        {"id": "create-test", "type": "create_document_test", "args": {"kind": "review", "title": "Review sample"}},
        {"id": "procedure", "type": "create_procedure", "args": {"objective": "Review purchases"}},
        {"id": "apm", "type": "update_planning_context", "args": {"changes": {"objective": "Audit procurement"}}},
    ], audit_lifecycle=True)
    by_id = {item["id"]: item for item in created}

    assert by_id["procedure"]["depends_on"] == ["apm"]
    assert by_id["create-test"]["depends_on"] == ["procedure"]
    assert by_id["run-test"]["depends_on"] == ["create-test"]
    assert by_id["run-test"]["target"]["kind"] == "doctest"
    assert by_id["run-test"]["target"]["resolved_id"] == by_id["create-test"]["args"]["id"]
    assert set(by_id["finding"]["depends_on"]) == {"run-test"}
    assert set(by_id["paper"]["depends_on"]) == {"run-test"}
    assert set(by_id["report"]["depends_on"]) == {"finding", "paper"}
    assert by_id["quality"]["depends_on"] == ["report"]


def test_full_audit_normalizes_report_reconcile_quality_cycle(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto",
        {"source": "goal_template", "goal_template": "full_audit_working_draft"},
    )
    created = ledger.append_actions(run, [
        {"id": "report", "type": "generate_report", "args": {"use_model": False}},
        {"id": "quality", "type": "run_report_quality", "args": {}, "depends_on": ["report"]},
        {
            "id": "reconcile", "type": "reconcile_report", "args": {"action": "keep"},
            "depends_on": ["quality"],
        },
    ], audit_lifecycle=True)
    by_id = {item["id"]: item for item in created}

    assert by_id["reconcile"]["depends_on"] == ["report"]
    assert set(by_id["quality"]["depends_on"]) == {"report", "reconcile"}
    assert {
        "action_id": "reconcile", "kind": "removed_backward_dependency",
        "dependency_id": "quality",
    } in run["lifecycle_adjustments"]


def test_create_action_references_resolve_to_allocated_artifact_ids(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "prepare tests"})
    created = ledger.append_actions(run, [
        {"id": "procedure", "type": "create_procedure", "args": {"objective": "Inspect evidence"}},
        {
            "id": "paper", "type": "generate_working_paper", "args": {},
            "target": {"resolved_id": "procedure"},
        },
        {
            "id": "create-test", "type": "create_document_test",
            "args": {"kind": "review", "title": "Evidence review", "items": [{"summary": "Review item"}]},
        },
        {
            "id": "attach", "type": "attach_document_to_test", "args": {"document_id": "DOC-1"},
            "target": {"selector": "test_id:create-test"},
        },
        {
            "id": "run-test", "type": "run_document_test", "args": {},
            "target": {"resolved_id": "create-test"},
        },
    ])
    by_id = {item["id"]: item for item in created}

    assert by_id["paper"]["target"]["resolved_id"] == by_id["procedure"]["args"]["id"]
    assert "procedure" in by_id["paper"]["depends_on"]
    assert by_id["run-test"]["target"]["resolved_id"] == by_id["create-test"]["args"]["id"]
    assert "create-test" in by_id["run-test"]["depends_on"]
    item_id = by_id["create-test"]["args"]["items"][0]["id"]
    assert by_id["attach"]["target"]["resolved_id"] == f"{by_id['create-test']['args']['id']}:{item_id}"
    assert by_id["attach"]["target"]["selector"] is None


def test_generated_report_action_reference_resolves_to_working_report(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "prepare report"})
    created = ledger.append_actions(run, [
        {"id": "generate", "type": "generate_report", "args": {"use_model": False}},
        {
            "id": "reconcile", "type": "reconcile_report", "args": {"action": "keep"},
            "target": {"kind": "report", "resolved_id": "generate"},
            "depends_on": ["generate"],
        },
    ])
    by_id = {item["id"]: item for item in created}

    assert by_id["reconcile"]["target"] == {
        "kind": "report", "selector": None, "resolved_id": "working",
    }
    assert {
        "action_id": "reconcile", "kind": "target_action_reference",
        "from": "generate", "to": "working",
    } in run["lifecycle_adjustments"]


@pytest.mark.parametrize("proposed_kind", ["doctest_item", "doctest"])
def test_created_document_test_resolved_id_binds_sole_item(workspace_with_data, proposed_kind):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "follow_up", "text": "do the full audit"})
    created = ledger.append_actions(run, [
        {
            "id": "create-test", "type": "create_document_test",
            "args": {
                "kind": "vouching", "title": "Invoice, PO, and GRN match",
                "items": [{"label": "Three-way match sample"}],
            },
        },
        {
            "id": "attach-invoice", "type": "attach_document_to_test",
            "target": {"kind": proposed_kind, "resolved_id": "create-test"},
            "args": {"document_id": "DOC-INVOICE"}, "depends_on": ["create-test"],
        },
        {
            "id": "attach-po", "type": "attach_document_to_test",
            "target": {"kind": proposed_kind, "resolved_id": "create-test"},
            "args": {"document_id": "DOC-PO"}, "depends_on": ["create-test"],
        },
    ])
    by_id = {item["id"]: item for item in created}
    durable_target = (
        f"{by_id['create-test']['args']['id']}:"
        f"{by_id['create-test']['args']['items'][0]['id']}"
    )

    assert by_id["attach-invoice"]["target"] == {
        "kind": "doctest_item", "selector": None, "resolved_id": durable_target,
    }
    assert by_id["attach-po"]["target"]["resolved_id"] == durable_target


def test_create_document_test_executor_preserves_planned_item_id(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "create test"})
    action = ledger.append_actions(run, [{
        "id": "create-test", "type": "create_document_test",
        "args": {
            "kind": "vouching", "title": "Three-way match",
            "items": [{"label": "Invoice, PO, and GRN"}],
        },
    }])[0]
    planned_item_id = action["args"]["items"][0]["id"]

    definition = actions.validate_action(action)
    definition.executor(workspace_with_data, action, run)
    created_test = doc_tests.load_test(workspace_with_data, action["args"]["id"])

    assert [item["id"] for item in created_test["items"]] == [planned_item_id]


def test_created_document_test_item_reference_requires_one_item(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "prepare test"})
    with pytest.raises(workspaces.WorkspaceError, match="must define exactly one test item"):
        ledger.append_actions(run, [
            {"id": "create-test", "type": "create_document_test", "args": {"kind": "review", "title": "Empty"}},
            {
                "id": "attach", "type": "attach_document_to_test", "args": {"document_id": "DOC-1"},
                "target": {"selector": "test_id:create-test"}, "depends_on": ["create-test"],
            },
        ])
    assert run["actions"] == []


def test_cycle_error_reports_the_cycle_path(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test graph"})
    with pytest.raises(workspaces.WorkspaceError, match=r"cycle: first -> second -> first"):
        ledger.append_actions(run, [
            {"id": "first", "type": "run_report_quality", "args": {}, "depends_on": ["second"]},
            {"id": "second", "type": "run_report_quality", "args": {"marker": 2}, "depends_on": ["first"]},
        ])


def test_latest_full_audit_graph_shape_is_normalized_without_cycle(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto",
        {"source": "follow_up", "text": "do the full audit"},
    )
    created = ledger.append_actions(run, [
        {
            "id": "a1_planning_init", "type": "edit_apm", "args": {"apm_markdown": "# Plan"},
            "target": {"kind": "planning", "resolved_id": "planning:apm"},
        },
        {"id": "a2_rcm_creation", "type": "create_rcm_row", "args": {"risk": "Approval bypass"}, "depends_on": ["a1_planning_init"]},
        {"id": "a3_procedure_creation", "type": "create_procedure", "args": {"objective": "Test approvals"}, "depends_on": ["a2_rcm_creation"]},
        {
            "id": "a4_test_creation", "type": "create_document_test",
            "args": {"kind": "review", "title": "Approval review", "items": [{"summary": "Review approval evidence"}]},
            "depends_on": ["a3_procedure_creation"],
        },
        {
            "id": "a5_attach_documents", "type": "attach_document_to_test",
            "target": {"kind": "doctest_item", "selector": "test_id:a4_test_creation"},
            "args": {"document_id": "DOC-1"}, "depends_on": ["a4_test_creation"],
        },
        {
            "id": "a6_run_test", "type": "run_document_test",
            "target": {"kind": "doctest", "resolved_id": "a4_test_creation"},
            "args": {}, "depends_on": ["a5_attach_documents"],
        },
        {"id": "a7_create_finding", "type": "create_finding", "args": {"title": "Approval exception"}, "depends_on": ["a6_run_test"]},
        {
            "id": "a8_generate_working_paper", "type": "generate_working_paper",
            "target": {"kind": "procedure", "resolved_id": "a3_procedure_creation"},
            "args": {}, "depends_on": ["a7_create_finding"],
        },
        {"id": "a9_generate_report", "type": "generate_report", "args": {"use_model": False}, "depends_on": ["a8_generate_working_paper"]},
        {"id": "a10_report_quality_check", "type": "run_report_quality", "args": {}, "depends_on": ["a9_generate_report"]},
        {
            "id": "a11_reconcile_report", "type": "reconcile_report",
            "args": {"action": "keep"}, "depends_on": ["a10_report_quality_check"],
        },
    ], audit_lifecycle=True)
    by_id = {item["id"]: item for item in created}

    assert by_id["a11_reconcile_report"]["depends_on"] == ["a9_generate_report"]
    assert "a11_reconcile_report" in by_id["a10_report_quality_check"]["depends_on"]
    assert by_id["a6_run_test"]["target"]["resolved_id"] == by_id["a4_test_creation"]["args"]["id"]
    assert by_id["a8_generate_working_paper"]["target"]["resolved_id"] == by_id["a3_procedure_creation"]["args"]["id"]
    ledger.validate_graph(run)


def test_artifact_resolution_exact_ambiguous_and_no_match(workspace_with_data):
    one = workspace_with_data.add_rcm({"risk": "Duplicate invoices may be paid"})
    workspace_with_data.add_rcm({"risk": "Duplicate suppliers may be created"})
    index = artifact_index.build(workspace_with_data)
    exact = artifact_index.resolve(index, "rcm", None, one["id"])
    assert exact["resolved_id"] == one["id"] and exact["confidence"] == 1.0
    ambiguous = artifact_index.resolve(index, "rcm", "duplicate")
    assert ambiguous["resolved_id"] is None and len(ambiguous["candidates"]) == 2
    assert artifact_index.resolve(index, "rcm", "unrelated treasury hedge")["candidates"] == []

    findings.add(workspace_with_data, {"title": "Same title"})
    findings.add(workspace_with_data, {"title": "Same title"})
    duplicate_titles = artifact_index.resolve(artifact_index.build(workspace_with_data), "finding", "same title")
    assert duplicate_titles["resolved_id"] is None and len(duplicate_titles["candidates"]) == 2


def test_model_context_includes_unmasked_identifier_values():
    frame = pl.DataFrame({"acct_no": [123456789, 987654321], "branch": ["North", "South"], "amount": [10.0, 20.0]})
    projected = model_context.project_frame(frame)
    assert projected["numeric_summary"]["acct_no"]["max"] == 987654321
    assert projected["rows"][0][0] == 123456789
    assert projected["rows"][0][1:] == ["North", 10.0]


def test_narrow_command_executes_only_requested_action(monkeypatch, workspace_with_data):
    response = {
        "objective": "Add one finding",
        "constraints": [], "completion_criteria": ["Finding exists"],
        "actions": [{"id": "create-one", "type": "create_finding", "args": {"title": "Duplicate invoices need review", "severity": "medium"}}],
        "needs_planning_wave": False,
    }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": "add a finding for duplicate invoices"})
    completed = wait_run(workspace_with_data, started["id"])
    reloaded = workspaces.load_workspace(workspace_with_data.id)
    assert completed["status"] == "completed"
    assert [item["type"] for item in completed["actions"]] == ["create_finding"]
    assert reloaded.findings[0]["title"] == "Duplicate invoices need review"


def test_dependent_mutations_rebase_to_succeeded_dependency(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "update planning"}
    )
    created = ledger.append_actions(run, [
        {
            "id": "context", "type": "update_planning_context",
            "args": {"changes": {"objective": "Audit procurement"}},
        },
        {
            "id": "apm", "type": "edit_apm",
            "args": {"apm_markdown": "# Procurement audit plan"},
            "depends_on": ["context"],
        },
    ])
    command = command_runner.CommandRunner(
        workspace_with_data, run, runner.RunHandle(workspace_with_data.id, run["id"])
    )

    # The graph prepares both actions against the original planning artifact.
    for action in created:
        command._resolve_and_gate(action)
    original_sha1 = created[1]["precondition"]["artifact_sha1"]
    assert created[0]["precondition"]["artifact_sha1"] == original_sha1

    command._execute_action(created[0])
    dependency_sha1 = created[0]["receipt"]["post_sha1"]
    assert dependency_sha1 != original_sha1

    command._execute_action(created[1])

    assert created[1]["status"] == "succeeded"
    assert created[1]["precondition"]["artifact_sha1"] == dependency_sha1
    assert run["interactions"] == []
    assert workspace_with_data.planning["context"]["objective"] == "Audit procurement"
    assert workspace_with_data.planning["apm_markdown"] == "# Procurement audit plan"


def test_external_change_still_requires_conflict_resolution(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "edit planning"}
    )
    action = ledger.append_actions(run, [{
        "id": "apm", "type": "edit_apm",
        "args": {"apm_markdown": "# Proposed audit plan"},
    }])[0]
    command = command_runner.CommandRunner(
        workspace_with_data, run, runner.RunHandle(workspace_with_data.id, run["id"])
    )
    command._resolve_and_gate(action)

    workspace_with_data.update_planning({"context": {"objective": "Auditor revision"}})
    command._execute_action(action)

    assert action["status"] == "awaiting_input"
    assert workspace_with_data.planning["apm_markdown"] == ""
    assert run["interactions"][-1]["type"] == "conflict_resolution"


def test_persisted_self_conflict_is_dismissed_on_resume(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "update planning"}
    )
    context, apm = ledger.append_actions(run, [
        {
            "id": "context", "type": "update_planning_context",
            "args": {"changes": {"objective": "Audit procurement"}},
        },
        {
            "id": "apm", "type": "edit_apm",
            "args": {"apm_markdown": "# Procurement audit plan"},
            "depends_on": ["context"],
        },
    ])
    command = command_runner.CommandRunner(
        workspace_with_data, run, runner.RunHandle(workspace_with_data.id, run["id"])
    )
    command._resolve_and_gate(context)
    command._resolve_and_gate(apm)
    command._execute_action(context)

    ledger.transition(apm, "awaiting_input")
    interaction = ledger.interaction(
        run, apm, "conflict_resolution", "The target changed after planning."
    )

    assert command._dismiss_obsolete_interaction(apm, interaction) is True
    assert interaction["status"] == "resolved"
    assert interaction["response"]["dependency_action_id"] == "context"
    assert apm["status"] == "ready"
    assert apm["precondition"]["artifact_sha1"] == context["receipt"]["post_sha1"]

    command._execute_action(apm)
    assert apm["status"] == "succeeded"


def test_destructive_command_executes_without_confirmation_in_auto_mode(monkeypatch, workspace_with_data):
    finding = __import__("app.findings", fromlist=["add"]).add(workspace_with_data, {"title": "Duplicate invoices"})
    response = {
        "objective": "Remove finding", "constraints": [], "completion_criteria": [],
        "actions": [{"id": "delete-one", "type": "delete_finding", "target": {"kind": "finding", "resolved_id": finding["id"]}, "args": {}}],
    }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": f"remove finding {finding['id']}"})
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["status"] == "completed"
    assert completed["interactions"] == []
    assert not workspaces.load_workspace(workspace_with_data.id).findings


def test_destructive_command_requires_confirmation_in_permission_mode(monkeypatch, workspace_with_data):
    finding = findings.add(workspace_with_data, {"title": "Duplicate invoices"})
    response = {
        "objective": "Remove finding", "constraints": [], "completion_criteria": [],
        "actions": [{"id": "delete-one", "type": "delete_finding", "target": {"kind": "finding", "resolved_id": finding["id"]}, "args": {}}],
    }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "permission", {"source": "chat", "text": f"remove finding {finding['id']}"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = store.load_run(workspace_with_data, started["id"])
        pending = next((item for item in state.get("interactions") or [] if item["status"] == "pending"), None)
        if pending:
            break
        time.sleep(0.02)
    assert pending["type"] == "confirmation"
    assert any(item["id"] == finding["id"] for item in workspace_with_data.findings)
    runner.resolve_interaction(workspace_with_data, started["id"], pending["id"], {"decision": "approve"})
    completed = wait_run(workspace_with_data, started["id"])
    assert completed["status"] == "completed"
    assert not workspaces.load_workspace(workspace_with_data.id).findings


def test_auto_mode_waits_for_graph_created_document_test_item(monkeypatch, workspace_with_data):
    document = documents.add_document(workspace_with_data, "invoice.txt", b"Invoice INV-001")
    response = {
        "objective": "Create and run a document test", "constraints": [],
        "completion_criteria": ["Document attached"],
        "actions": [
            {
                "id": "create-test", "type": "create_document_test",
                "args": {
                    "kind": "vouching", "title": "Invoice match",
                    "items": [{"label": "Invoice INV-001"}],
                },
            },
            {
                "id": "attach-document", "type": "attach_document_to_test",
                "target": {"kind": "doctest_item", "resolved_id": "create-test"},
                "args": {"document_id": document["id"]}, "depends_on": ["create-test"],
            },
        ],
    }
    configured(monkeypatch, response)

    started = runner.start_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "create and run the document test"}
    )
    completed = wait_run(workspace_with_data, started["id"])
    created = next(item for item in completed["actions"] if item["id"] == "create-test")
    test = doc_tests.load_test(workspace_with_data, created["args"]["id"])

    assert completed["status"] == "completed"
    assert completed["interactions"] == []
    assert test["items"][0]["document_ids"] == [document["id"]]


def test_stale_graph_target_clarification_is_dismissed(workspace_with_data):
    document = documents.add_document(workspace_with_data, "invoice.txt", b"Invoice INV-001")
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test invoice"})
    created = ledger.append_actions(run, [
        {
            "id": "create-test", "type": "create_document_test",
            "args": {
                "kind": "vouching", "title": "Invoice match",
                "items": [{"label": "Invoice INV-001"}],
            },
        },
        {
            "id": "attach-document", "type": "attach_document_to_test",
            "target": {"kind": "doctest_item", "resolved_id": "create-test"},
            "args": {"document_id": document["id"]}, "depends_on": ["create-test"],
        },
    ])
    by_id = {item["id"]: item for item in created}
    ledger.transition(by_id["create-test"], "ready")
    ledger.transition(by_id["attach-document"], "awaiting_input")
    by_id["attach-document"]["target"]["selector"] = "apm"
    interaction = ledger.interaction(
        run, by_id["attach-document"], "clarification",
        "Which artifact should this action affect?",
        policy_reason="No local artifact matched the selector.",
    )
    store.save_run(workspace_with_data, run)

    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    command_runner.CommandRunner(workspace_with_data, run, handle)._drive_graph()
    test = doc_tests.load_test(workspace_with_data, by_id["create-test"]["args"]["id"])

    assert interaction["status"] == "resolved"
    assert interaction["actor"] == "orchestrator"
    assert by_id["attach-document"]["target"]["selector"] is None
    assert by_id["attach-document"]["status"] == "succeeded"
    assert test["items"][0]["document_ids"] == [document["id"]]


def test_stale_generated_report_clarification_is_dismissed(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "prepare report"})
    created = ledger.append_actions(run, [
        {"id": "generate", "type": "generate_report", "args": {"use_model": False}},
        {
            "id": "reconcile", "type": "reconcile_report", "args": {"action": "keep"},
            "target": {"kind": "report", "resolved_id": "generate"},
            "depends_on": ["generate"],
        },
    ])
    by_id = {item["id"]: item for item in created}
    ledger.transition(by_id["generate"], "ready")
    # Recreate the target and interaction shape persisted by the pre-fix run.
    by_id["reconcile"]["target"]["resolved_id"] = "generate"
    ledger.transition(by_id["reconcile"], "awaiting_input")
    interaction = ledger.interaction(
        run, by_id["reconcile"], "clarification",
        "Which artifact should this action affect?",
        policy_reason="No local artifact matched the selector.",
    )
    store.save_run(workspace_with_data, run)

    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    command_runner.CommandRunner(workspace_with_data, run, handle)._drive_graph()

    assert interaction["status"] == "resolved"
    assert interaction["actor"] == "orchestrator"
    assert by_id["reconcile"]["target"]["resolved_id"] == "working"
    assert by_id["generate"]["status"] == "succeeded"
    assert by_id["reconcile"]["status"] == "succeeded"


def test_general_chat_queues_follow_up_without_altering_graph(monkeypatch, workspace_with_data):
    def response(user):
        command = json.loads(user)["command"]["text"]
        return {
            "objective": command, "constraints": [], "completion_criteria": [],
            "actions": [] if "review the APM" in command else [
                {"id": "rewrite", "type": "generate_report", "args": {"use_model": False}}
            ],
        }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "permission", {"source": "chat", "text": "prepare report"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = store.load_run(workspace_with_data, started["id"])
        if any(item["status"] == "pending" for item in state.get("interactions") or []):
            break
        time.sleep(0.02)
    result = runner.steer(workspace_with_data, started["id"], "then review the APM")
    assert result["handled"] == "queued_command"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = store.load_run(workspace_with_data, started["id"])
        if state["pending_commands"]:
            break
        time.sleep(0.02)
    assert [item["type"] for item in state["actions"]] == ["generate_report"]
    assert state["pending_commands"][0]["text"] == "then review the APM"
    runner.cancel_run(workspace_with_data, started["id"])
    wait_run(workspace_with_data, started["id"])
    deadline = time.monotonic() + 5
    follow_up = None
    while time.monotonic() < deadline:
        follow_up = next((item for item in store.list_runs(workspace_with_data) if item["parent_run_id"] == started["id"]), None)
        if follow_up:
            break
        time.sleep(0.02)
    assert follow_up is not None
    assert wait_run(workspace_with_data, follow_up["id"])["status"] == "completed"


def test_queued_planning_command_keeps_goal_and_document_context(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "current command"}
    )
    run["status"] = "paused"
    store.save_run(workspace_with_data, run)

    result = runner.steer(
        workspace_with_data,
        run["id"],
        "update planning",
        goal_template="planning",
        run_context={"document_ids": ["doc-1"]},
    )

    command = result["command"]
    assert command["goal_template"] == "planning"
    assert command["run_context"] == {"document_ids": ["doc-1"]}


def test_completed_intake_message_starts_unified_command(monkeypatch, workspace_with_data):
    intake_run = store.new_run(
        workspace_with_data,
        "auto",
        {"batch_id": "completed-batch", "source_id": "folder-source"},
        kind="intake",
    )
    intake_run["status"] = "completed"
    store.save_run(workspace_with_data, intake_run)
    captured = {}

    def fake_start_command(workspace, mode, command, parent_run_id=None):
        captured.update(
            workspace=workspace,
            mode=mode,
            command=command,
            parent_run_id=parent_run_id,
        )
        return {
            "schema_version": 2,
            "id": "follow-up-audit",
            "kind": "audit",
            "parent_run_id": parent_run_id,
            "command": command,
        }

    monkeypatch.setattr(runner, "start_command_run", fake_start_command)

    result = runner.steer(workspace_with_data, intake_run["id"], "do the full audit")

    assert result["handled"] == "follow_up_run"
    assert result["run"]["schema_version"] == 2
    assert result["run"]["kind"] == "audit"
    assert captured["workspace"] is workspace_with_data
    assert captured["mode"] == "auto"
    assert captured["parent_run_id"] == intake_run["id"]
    assert captured["command"] == {
        "source": "follow_up",
        "text": "do the full audit",
        "parent_command_id": None,
    }


def test_reversible_edit_can_be_undone_while_postcondition_is_current(monkeypatch, workspace_with_data):
    finding = findings.add(workspace_with_data, {"title": "Duplicate invoice risk", "severity": "medium"})
    response = {
        "objective": "Change then undo", "constraints": [], "completion_criteria": [],
        "actions": [
            {"id": "edit", "type": "edit_finding", "target": {"kind": "finding", "resolved_id": finding["id"]}, "args": {"changes": {"severity": "high"}}},
            {"id": "undo", "type": "undo_action", "args": {"action_id": "edit"}, "depends_on": ["edit"]},
        ],
    }
    configured(monkeypatch, response)
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": f"temporarily change and undo {finding['id']}"})
    completed = wait_run(workspace_with_data, started["id"])
    assert completed["status"] == "completed"
    assert workspaces.load_workspace(workspace_with_data.id).findings[0]["severity"] == "medium"


def test_post_success_planner_failure_keeps_committed_work(monkeypatch, workspace_with_data):
    """Regression: an invalid planner proposal after an action succeeded must
    become a warning, not an illegal succeeded → failed transition that fails
    the run and strands the remaining ready actions."""
    interpreter = {
        "objective": "Create two findings", "constraints": [], "completion_criteria": [],
        "actions": [
            {"id": "first", "type": "create_finding", "args": {"title": "First finding"}, "planning_significant": True},
            {"id": "second", "type": "create_finding", "args": {"title": "Second finding"}, "depends_on": ["first"]},
        ],
    }
    planner = {"actions": [{"id": "bad", "type": "run_analytics", "args": {}}]}
    fake = FakeAgentLLM({"agent:command_interpreter": interpreter, "agent:command_planner": planner})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"})
    started = runner.start_command_run(workspace_with_data, "auto", {"source": "chat", "text": "create two findings"})
    completed = wait_run(workspace_with_data, started["id"])
    by_id = {item["id"]: item for item in completed["actions"]}
    assert completed["status"] == "completed"
    assert by_id["first"]["status"] == "succeeded" and by_id["first"]["error"] is None
    assert by_id["second"]["status"] == "succeeded"
    assert any("invalid planning proposal" in warning for warning in completed["warnings"])
    assert len(workspaces.load_workspace(workspace_with_data.id).findings) == 2


def test_post_success_planner_transport_error_keeps_running_graph(
    monkeypatch, workspace_with_data
):
    interpreter = {
        "objective": "Create two findings", "constraints": [], "completion_criteria": [],
        "actions": [
            {
                "id": "first", "type": "create_finding",
                "args": {"title": "First finding"}, "planning_significant": True,
            },
            {
                "id": "second", "type": "create_finding",
                "args": {"title": "Second finding"}, "depends_on": ["first"],
                "planning_significant": True,
            },
        ],
    }

    def disconnected(_user):
        raise llm.LLMError("LLM request failed: Remote end closed connection without response")

    fake = FakeAgentLLM({
        "agent:command_interpreter": interpreter,
        "agent:command_planner": disconnected,
    })
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    started = runner.start_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "create two findings"}
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["status"] == "completed"
    assert [item["status"] for item in completed["actions"]] == ["succeeded", "succeeded"]
    assert completed["planning_expansion_disabled"] is True
    assert any("Further planning expansion skipped" in warning for warning in completed["warnings"])
    assert [call["tag"] for call in fake.calls].count("agent:command_planner") == 1


def test_append_actions_rolls_back_a_rejected_batch(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test"})
    ledger.append_actions(run, [{"id": "a1", "type": "run_report_quality", "args": {}}])
    revision = run["graph_revision"]
    with pytest.raises(workspaces.WorkspaceError, match="unknown action"):
        ledger.append_actions(run, [
            {"id": "b1", "type": "create_finding", "args": {"title": "Valid"}},
            {"id": "b2", "type": "create_finding", "args": {"title": "Broken"}, "depends_on": ["missing"]},
        ])
    assert [item["id"] for item in run["actions"]] == ["a1"]
    assert run["graph_revision"] == revision
    assert [task["id"] for task in run["plan"]["stages"][0]["tasks"]] == ["a1"]


def test_create_reconciler_detects_after_apply_before_receipt(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "create finding"})
    action = ledger.append_actions(run, [{"id": "create", "type": "create_finding", "args": {"title": "Crash-safe finding"}}])[0]
    action["postcondition"] = actions.expected_postcondition(action)
    action["status"] = "running"
    definition = actions.validate_action(action)
    definition.executor(workspace_with_data, action, run)  # crash boundary: domain write happened, receipt did not
    assert definition.reconciler(workspace_with_data, action) == "already_applied"
    assert len(workspaces.load_workspace(workspace_with_data.id).findings) == 1


def test_full_audit_command_uses_documents_and_planning_templates(monkeypatch, workspace_with_data):
    policy = documents.add_document(
        workspace_with_data, "Procurement SOP.txt",
        b"Procurement SOP: requisitions require approval before a purchase order is issued.",
        category="policy",
    )
    risk = "Purchases may bypass required approval"
    semantic_risk = "rcm:procurement:purchases-may-bypass-required-approval"

    def select_documents(user):
        assert policy["id"] in user
        return {"selected": [{"id": policy["id"], "reason": "Governs procurement approval."}]}

    def interpret(user):
        payload = json.loads(user)
        assert payload["prepared_planning"]["document_content_included"] is True
        kinds = {item["kind"] for item in payload["workspace_index"]["artifacts"]}
        assert {"planning", "rcm", "procedure"} <= kinds
        return {
            "objective": "Complete the full audit through reporting",
            "constraints": [], "completion_criteria": [],
            "actions": [
                {
                    "id": "redundant-apm", "type": "generate_apm",
                    "args": {"apm_markdown": "one line"},
                },
                {
                    "id": "test-approvals", "type": "create_document_test",
                    "args": {
                        "kind": "review", "title": "Procurement approval review",
                        "items": [{"label": "Review approval evidence"}],
                    },
                    "depends_on": ["redundant-apm"],
                },
            ],
        }

    complete_apm = (
        "# Audit Planning Memorandum\n\n## Engagement\n\nEntity and scope.\n\n"
        "## Introduction and background\n\nProcurement background.\n\n"
        "## Process flow and understanding\n\nRequisition through payment.\n\n"
        "## Prior audit findings\n\nNo information available.\n\n"
        "## Key risks and planned response\n\nTest approval compliance."
    )

    def draft_apm(user):
        if "failed the engagement quality gate" not in user:
            return {"apm_markdown": "Full audit planning memorandum for the engagement."}
        return {"apm_markdown": complete_apm}

    fake = FakeAgentLLM({
        "agent:document_selection": select_documents,
        "agent:document_context": {
            "context": {"scope": "Procurement approvals", "entity": "Example Bank"},
        },
        "agent:apm": draft_apm,
        "agent:rcm": {"rows": [{
            "process": "Procurement", "risk": risk, "risk_rating": "high",
            "assertion": "Authorization", "control": "Approval before commitment",
            "control_type": "Manual preventive",
            "test_procedure": "Inspect requisitions and approval evidence.",
        }]},
        "agent:work_program": {"procedures": [{
            "stable_slug": "approval-compliance", "rcm_refs": [semantic_risk],
            "objective": "Determine whether purchases were approved",
            "criteria": "Approval is documented before commitment.",
            "steps": ["Select purchases and inspect approval evidence."],
            "method": "Inspection", "expected_evidence": "Approved requisitions",
        }]},
        "agent:command_interpreter": interpret,
    })
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"})

    started = runner.start_command_run(
        workspace_with_data, "auto", {"source": "follow_up", "text": "do the full audit"}
    )
    completed = wait_run(workspace_with_data, started["id"])
    reloaded = workspaces.load_workspace(workspace_with_data.id)

    assert completed["status"] == "completed"
    assert [item["type"] for item in completed["actions"]] == ["create_document_test"]
    assert "## Key risks and planned response" in reloaded.planning["apm_markdown"]
    assert reloaded.rcm[-1]["control"] == "Approval before commitment"
    assert reloaded.work_program[-1]["steps"] == ["Select purchases and inspect approval evidence."]
    assert completed["prepared_planning"]["document_content_included"] is True
    assert [call["tag"] for call in fake.calls].count("agent:apm") == 2
    activity = documents.activities(reloaded, limit=250)["items"]
    assert any(item["stage"] == "agent:apm" and policy["id"] in item["document_ids"] for item in activity)
    assert any(item["stage"] == "agent:apm" and item["template_versions"] for item in activity)


def test_full_audit_failure_closes_embedded_running_planning_task(monkeypatch, workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto",
        {"source": "follow_up", "text": "do the full audit"},
    )
    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    command = command_runner.CommandRunner(workspace_with_data, run, handle)

    def fail_during_planning():
        completed = command.add_task("apm", "planning:apm", "Draft the APM")
        command.task_status(completed, "completed")
        running = command.add_task("work_program", "planning:work_program", "Draft the audit program")
        command.task_status(running, "running")
        raise ConnectionError("remote connection closed")

    monkeypatch.setattr(command, "_prepare_planning", fail_during_planning)
    command.execute()

    saved = store.load_run(workspace_with_data, run["id"])
    tasks = {
        task["id"]: task
        for stage in saved["plan"]["stages"]
        for task in stage["tasks"]
    }
    assert saved["status"] == "failed"
    assert tasks["planning:apm"]["status"] == "completed"
    assert tasks["planning:work_program"]["status"] == "failed"
    assert tasks["planning:work_program"]["error"] == "remote connection closed"


def test_document_test_kind_is_validated_before_execution(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test documents"})
    with pytest.raises(workspaces.WorkspaceError, match="unsupported value"):
        ledger.append_actions(run, [{
            "id": "bad-test", "type": "create_document_test",
            "args": {"kind": "doctest", "title": "Invalid generic test"},
        }])
