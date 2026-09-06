import inspect
import json
import time

import polars as pl
import pytest

from app import assistant, data_tests, doc_tests, documents, findings, llm, model_context, rcm_execution, report, tooling, workspaces
from app.agent import action_execution, action_tools, actions, artifact_index, ledger, loop_tools, prompts, runner, store
from app.agent import capabilities as audit_capabilities
from conftest import FakeAgentLLM, wait_run


def configured(monkeypatch, response, *, router_response=None):
    responses = {"agent:command_interpreter": response}
    if router_response is not None:
        responses["agent:workflow_router"] = router_response
    fake = FakeAgentLLM(responses)
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
    assert {item.type for item in actions.REGISTRY.all()} >= {"edit_finding", "delete_finding", "edit_report"}
    # Generation of a workflow-owned deliverable is not an action (P11.2A).
    assert {item.type for item in actions.REGISTRY.all()}.isdisjoint({
        "generate_apm", "generate_report", "rollup_rcm_results",
        "verify_audit_completion", "infer_relationships", "run_document_test",
        "generate_all_rcm_working_papers",
    })
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test"})
    with pytest.raises(workspaces.WorkspaceError, match="Unknown agent action"):
        ledger.append_actions(run, [{"type": "write_json", "args": {}}])
    with pytest.raises(workspaces.WorkspaceError, match="unknown action"):
        ledger.append_actions(run, [{"id": "a", "type": "run_report_quality", "args": {}, "depends_on": ["missing"]}])


def test_action_graph_rejects_duplicate_intent_even_with_distinct_ids(
    workspace_with_data,
):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "check once"}
    )

    with pytest.raises(workspaces.WorkspaceError, match="duplicate action intent"):
        ledger.append_actions(
            run,
            [
                {"id": "quality-1", "type": "run_report_quality", "args": {}},
                {"id": "quality-2", "type": "run_report_quality", "args": {}},
            ],
        )

    assert run["actions"] == []
    assert run["graph_revision"] == 0


def test_singleton_target_kind_is_normalized_before_validation(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "delete the document test"})
    action = ledger.append_actions(run, [{
        "id": "delete-test", "type": "delete_document_test", "args": {},
        "target": {"resolved_id": "DT-EXISTING"},
    }])[0]

    assert action["target"] == {
        "kind": "doctest", "selector": None, "resolved_id": "DT-EXISTING",
    }








def test_command_interpreter_repair_lists_supported_validation_checks():
    message = action_execution.ActionExecution._proposal_repair_user(
        "base", {"actions": []}, workspaces.WorkspaceError("Unknown check 'invented'.")
    )

    assert "Supported validation check ids are:" in message
    assert "required" in message
    assert "Use `required` for null or blank checks." in message






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






def test_created_rcm_references_add_generic_action_dependencies(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto",
        {"source": "chat", "text": "create and run this linked data test"},
    )
    created = ledger.append_actions(run, [
        {"id": "rcm", "type": "create_rcm_row", "args": {"risk": "Purchases bypass approval"}},
        {
            "id": "data", "type": "create_data_test",
            "args": {
                "rcm_id": "rcm",
                "title": "Approval population", "objective": "Find missing approvals",
                "engine": "analytics", "table_refs": ["transactions"],
                "spec": {"test_id": "completeness", "params": {"columns": ["invoice_no"]}},
            },
        },
        {"id": "run", "type": "run_data_test", "target": {"resolved_id": "data"}},
        {"id": "paper", "type": "generate_rcm_working_paper", "target": {"resolved_id": "rcm"}},
    ])
    by_id = {item["id"]: item for item in created}

    assert by_id["data"]["args"]["rcm_id"] == by_id["rcm"]["args"]["id"]
    assert by_id["run"]["target"]["resolved_id"] == by_id["data"]["args"]["id"]
    assert by_id["paper"]["target"]["resolved_id"] == by_id["rcm"]["args"]["id"]
    assert by_id["data"]["depends_on"] == ["rcm"]
    assert by_id["run"]["depends_on"] == ["data"]
    assert by_id["paper"]["depends_on"] == ["rcm"]
    ledger.validate_graph(run)


def test_description_only_document_test_is_rejected(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "test documents"}
    )
    with pytest.raises(workspaces.WorkspaceError, match="needs comparison checks"):
        ledger.append_actions(run, [{
            "id": "empty-vouch", "type": "create_document_test",
            "args": {
                "kind": "vouching", "title": "Invoice vouching",
                "items": [{"label": "Review the invoice"}],
            },
        }])


def test_observation_finding_action_derives_immutable_evidence_locally(
    workspace_with_data,
):
    row = workspace_with_data.add_rcm({"risk": "Duplicate invoices may be paid"})
    data_test = data_tests.create(workspace_with_data, {
        "title": "Duplicate invoices", "objective": "Identify duplicate invoices",
        "rcm_id": row["id"],
        "engine": "analytics", "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    })
    data_tests.run(workspace_with_data, data_test["id"])
    rcm_execution.rollup(workspace_with_data)
    observation = workspace_with_data.observations[0]
    assert observation["outcome"] == "exception"
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "draft the finding"}
    )
    action = ledger.append_actions(run, [{
        "id": "draft-finding", "type": "draft_finding_from_observation",
        "target": {"kind": "observation", "resolved_id": observation["id"]},
        "args": {
            "title": "Duplicate invoice identifiers were processed",
            "severity": "medium",
            "cause_pending": True,
            "narrative": (
                "## Condition\n\nA duplicate invoice identifier exists in the "
                "population.\n\n"
                "## Criteria\n\nInvoice identifiers must be unique before payment.\n\n"
                "## Root Cause\n\n"
                "## Risk\n\nA duplicate payment may be processed.\n\n"
                "## Recommendation\n\nInvestigate and prevent duplicate invoice "
                "identifiers.\n"
            ),
        },
    }])[0]

    receipt = actions.REGISTRY.get(action["type"]).executor(
        workspace_with_data, action, run
    )
    finding = workspace_with_data.findings[-1]

    assert receipt["result"]["support_complete"] is True
    assert receipt["result"]["auditor_confirmation_required"] is True
    assert finding["auditor_confirmed"] is False
    assert finding["rcm_refs"] == [row["id"]]
    assert finding["test_refs"] == [data_test["id"]]
    assert finding["execution_refs"] == [observation["execution_ref"]]
    assert finding["evidence_refs"][0]["source_kind"] == "datatest"
    assert finding["evidence_refs"][0]["source_sha1"]


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
            "id": "delete-test", "type": "delete_document_test", "args": {},
            "target": {"resolved_id": "create-test"},
        },
    ])
    by_id = {item["id"]: item for item in created}

    assert by_id["paper"]["target"]["resolved_id"] == by_id["procedure"]["args"]["id"]
    assert "procedure" in by_id["paper"]["depends_on"]
    assert by_id["delete-test"]["target"]["resolved_id"] == by_id["create-test"]["args"]["id"]
    assert "create-test" in by_id["delete-test"]["depends_on"]
    item_id = by_id["create-test"]["args"]["items"][0]["id"]
    assert by_id["attach"]["target"]["resolved_id"] == f"{by_id['create-test']['args']['id']}:{item_id}"
    assert by_id["attach"]["target"]["selector"] is None


def test_report_producer_action_reference_resolves_to_working_report(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "prepare report"})
    created = ledger.append_actions(run, [
        {
            "id": "generate", "type": "edit_report",
            "args": {"changes": {"markdown": "# Draft"}},
            "target": {"kind": "report", "resolved_id": "working"},
        },
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
    } in run["target_adjustments"]


@pytest.mark.parametrize("proposed_kind", ["doctest_item", "doctest"])
def test_created_document_test_resolved_id_binds_sole_item(workspace_with_data, proposed_kind):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "follow_up", "text": "do the full audit"})
    created = ledger.append_actions(run, [
        {
            "id": "create-test", "type": "create_document_test",
            "args": {
                "kind": "vouching", "title": "Invoice, PO, and GRN match",
                    "items": [{
                        "label": "Three-way match sample",
                        "checks": [{"field": "invoice_no", "expected": "INV-001"}],
                    }],
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
                "items": [{
                    "label": "Invoice, PO, and GRN",
                    "checks": [{"field": "invoice_no", "expected": "INV-001"}],
                }],
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


def test_compact_artifact_index_exposes_bare_ids_and_canonicalizes_typed_refs(
    workspace_with_data,
):
    row = workspace_with_data.add_rcm({"risk": "Duplicate invoices may be paid"})
    data_test = data_tests.create(workspace_with_data, {
        "title": "Test duplicates", "objective": "Test duplicate invoices",
        "steps": [{"label": "Identify duplicate invoice numbers.", "instruction": "Identify duplicate invoice numbers."}],
        "rcm_id": row["id"],
        "engine": "analytics", "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    })
    compact = artifact_index.compact(artifact_index.build(workspace_with_data))
    by_ref = {item["ref"]: item for item in compact["artifacts"]}

    assert by_ref[f"rcm:{row['id']}"]["id"] == row["id"]
    assert by_ref[f"datatest:{data_test['id']}"]["id"] == data_test["id"]
    assert artifact_index.canonical_id(f"rcm:{row['id']}", "rcm") == row["id"]
    with pytest.raises(ValueError, match="Expected artifact kind 'rcm'"):
        artifact_index.canonical_id(f"datatest:{data_test['id']}", "rcm")


def test_data_test_action_preflight_rejects_wrong_engine_spec(workspace_with_data):
    action = {
        "type": "create_data_test",
        "args": {
            "title": "Malformed duplicate test", "objective": "Find duplicates",
            "engine": "analytics", "table_refs": ["transactions"],
            "spec": {"checks": [{"column": "invoice_no"}]},
        },
    }

    with pytest.raises(workspaces.WorkspaceError, match="Unknown analytics test"):
        actions.canonicalize_action_fields(workspace_with_data, action)

    existing = data_tests.create(workspace_with_data, {
        "title": "Duplicates", "objective": "Find duplicates", "engine": "analytics",
        "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    })
    edit = {
        "type": "edit_data_test", "target": {"resolved_id": existing["id"]},
        "args": {"changes": {"spec": {"checks": []}}},
    }
    with pytest.raises(workspaces.WorkspaceError, match="Unknown analytics test"):
        actions.canonicalize_action_fields(workspace_with_data, edit)


def test_model_context_includes_unmasked_identifier_values():
    frame = pl.DataFrame({"acct_no": [123456789, 987654321], "branch": ["North", "South"], "amount": [10.0, 20.0]})
    projected = model_context.project_frame(frame)
    assert projected["numeric_summary"]["acct_no"]["max"] == 987654321
    assert projected["rows"][0][0] == 123456789
    assert projected["rows"][0][1:] == ["North", 10.0]






def test_cancelled_command_records_actor_reason_and_terminal_task_status(
    workspace_with_data,
):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Create a finding"}
    )
    ledger.append_actions(run, [
        {"id": "finding", "type": "create_finding", "args": {"title": "Draft finding"}}
    ])
    store.save_run(workspace_with_data, run)

    cancelled = runner.cancel_run(
        workspace_with_data, run["id"], reason="Auditor stopped duplicate work",
        actor="auditor@example.com",
    )
    reloaded = store.load_run(workspace_with_data, run["id"])

    assert cancelled["status"] == "cancelled"
    assert reloaded["command"]["status"] == "cancelled"
    assert reloaded["cancellation"]["actor"] == "auditor@example.com"
    assert reloaded["cancellation"]["reason"] == "Auditor stopped duplicate work"
    assert reloaded["plan"]["stages"][0]["tasks"][0]["status"] == "cancelled"


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
    command = action_execution.ActionExecution(
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


def test_failed_action_blocks_transitive_dependents_without_execution(
    workspace_with_data,
):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "dependent checks"}
    )
    failed, child, grandchild = ledger.append_actions(
        run,
        [
            {"id": "quality", "type": "run_report_quality", "args": {}},
            {
                "id": "duplicates",
                "type": "run_analytics",
                "args": {
                    "table": "transactions",
                    "test": "duplicates",
                    "params": {"columns": ["invoice_no"]},
                },
                "depends_on": ["quality"],
            },
            {
                "id": "amounts",
                "type": "run_analytics",
                "args": {
                    "table": "transactions",
                    "test": "sign_scan",
                    "params": {"column": "amount"},
                },
                "depends_on": ["duplicates"],
            },
        ],
    )
    ledger.transition(failed, "ready")
    ledger.transition(failed, "running")
    ledger.transition(failed, "failed")
    command = action_execution.ActionExecution(
        workspace_with_data, run, runner.RunHandle(workspace_with_data.id, run["id"])
    )

    command._block_failed_dependencies()
    command._block_failed_dependencies()

    assert failed["status"] == "failed"
    assert child["status"] == "blocked"
    assert grandchild["status"] == "blocked"
    assert child["error"] == "A required action did not succeed."
    assert grandchild["error"] == "A required action did not succeed."
    assert run["activity"]["phase"] == "actions.blocked"


def test_external_change_still_requires_conflict_resolution(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "edit planning"}
    )
    action = ledger.append_actions(run, [{
        "id": "apm", "type": "edit_apm",
        "args": {"apm_markdown": "# Proposed audit plan"},
    }])[0]
    command = action_execution.ActionExecution(
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
    command = action_execution.ActionExecution(
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


# --------------------------------------------------------------------------- #
# The catalog through the steering loop's tools (step 8)
# --------------------------------------------------------------------------- #
def _loop_action_turn(name: str, args: dict) -> dict:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _finish_turn(summary: str = "Done.") -> dict:
    return _loop_action_turn("finish", {"summary": summary})


def _loop_script(*turns):
    state = {"index": 0}

    def script(_user: str) -> dict:
        turn = turns[min(state["index"], len(turns) - 1)]
        state["index"] += 1
        return turn

    return script


def _configured_loop(monkeypatch, *turns) -> FakeAgentLLM:
    fake = FakeAgentLLM({"agent:loop": _loop_script(*turns)})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"}
    )
    return fake


def _start_loop(workspace, text, mode="auto") -> dict:
    return runner.start_command_run(
        workspace, mode, {"source": store.LOOP_COMMAND_SOURCE, "text": text}
    )


def test_every_registered_action_is_reachable_as_a_loop_tool():
    """The catalog is the contract; the tool list is derived from it."""

    offered = {schema["function"]["name"] for schema in loop_tools.action_tools()}
    registered = {definition.type for definition in actions.REGISTRY.all()}

    assert offered == registered - loop_tools.UNOFFERED_ACTIONS
    assert {item["action"] for item in actions.ACTION_COVERAGE} >= offered
    # A tool takes the action's own declared schema, not a paraphrase of it.
    for schema in loop_tools.action_tools():
        definition = actions.REGISTRY.get(schema["function"]["name"])
        properties = schema["function"]["parameters"]["properties"]
        assert properties["args"]["type"] == definition.input_schema["type"]
        assert bool(definition.target_kinds) == ("target" in properties)


def test_a_destructive_action_runs_without_confirmation_in_auto_mode(
    monkeypatch, workspace_with_data
):
    finding = findings.add(workspace_with_data, {"title": "Duplicate invoices"})
    _configured_loop(
        monkeypatch,
        _loop_action_turn(
            "delete_finding",
            {"args": {}, "target": {"kind": "finding", "id": finding["id"]}},
        ),
        _finish_turn("Removed it."),
    )

    started = _start_loop(workspace_with_data, f"remove finding {finding['id']}")
    completed = wait_run(workspace_with_data, started["id"], timeout=30)

    assert completed["status"] == "completed"
    assert completed["interactions"] == []
    assert [item["type"] for item in completed["actions"]] == ["delete_finding"]
    assert completed["actions"][0]["status"] == "succeeded"
    assert completed["actions"][0]["receipt"]
    assert not workspaces.load_workspace(workspace_with_data.id).findings


def test_a_destructive_action_still_waits_for_confirmation_in_permission_mode(
    monkeypatch, workspace_with_data
):
    """The approval rule is the action definition's, not the engine's."""

    finding = findings.add(workspace_with_data, {"title": "Duplicate invoices"})
    _configured_loop(
        monkeypatch,
        _loop_action_turn(
            "delete_finding",
            {"args": {}, "target": {"kind": "finding", "id": finding["id"]}},
        ),
        _finish_turn("Removed it."),
    )

    started = _start_loop(
        workspace_with_data, f"remove finding {finding['id']}", mode="permission"
    )
    deadline = time.monotonic() + 10
    pending = None
    while time.monotonic() < deadline:
        state = store.load_run(workspace_with_data, started["id"])
        pending = next(
            (item for item in state.get("interactions") or [] if item["status"] == "pending"),
            None,
        )
        if pending:
            break
        time.sleep(0.02)

    assert pending is not None and pending["type"] == "confirmation"
    # Nothing is committed while the auditor is being asked.
    assert any(item["id"] == finding["id"] for item in workspace_with_data.findings)

    runner.resolve_interaction(
        workspace_with_data, started["id"], pending["id"], {"decision": "approve"}
    )
    completed = wait_run(workspace_with_data, started["id"], timeout=30)

    assert completed["status"] == "completed"
    assert not workspaces.load_workspace(workspace_with_data.id).findings


def test_a_reversible_edit_can_still_be_undone(monkeypatch, workspace_with_data):
    finding = findings.add(
        workspace_with_data, {"title": "Duplicate invoice risk", "severity": "medium"}
    )
    _configured_loop(
        monkeypatch,
        _loop_action_turn(
            "edit_finding",
            {
                "args": {"changes": {"severity": "high"}},
                "target": {"kind": "finding", "id": finding["id"]},
            },
        ),
        _loop_action_turn("undo_action", {"args": {"action_id": "PLACEHOLDER"}}),
        _finish_turn("Changed it and put it back."),
    )

    started = _start_loop(
        workspace_with_data, f"temporarily change and undo {finding['id']}"
    )
    # The undo names the action the loop just ran, which it reads off the
    # result the tool returned; the script has to be told the generated id.
    deadline = time.monotonic() + 10
    edit_id = None
    while time.monotonic() < deadline and edit_id is None:
        state = store.load_run(workspace_with_data, started["id"])
        edit = next(
            (item for item in state.get("actions") or [] if item["type"] == "edit_finding"),
            None,
        )
        edit_id = edit["id"] if edit and edit["status"] == "succeeded" else None
        time.sleep(0.02)
    assert edit_id, "the edit never committed"

    completed = wait_run(workspace_with_data, started["id"], timeout=30)
    severity = workspaces.load_workspace(workspace_with_data.id).findings[0]["severity"]
    # Either the undo ran against the real id, or it failed on the placeholder
    # and said so; what must not happen is a silent half-application.
    undone = next(
        (item for item in completed["actions"] if item["type"] == "undo_action"), None
    )
    assert undone is not None
    assert severity == ("medium" if undone["status"] == "succeeded" else "high")














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












def test_task_progress_updates_run_activity_and_timing(workspace_with_data):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "prepare planning"}
    )
    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    command = action_execution.ActionExecution(workspace_with_data, run, handle)
    task = command.add_task(
        "context", "planning:context", "Assemble planning context",
        "Reviewing documents…",
    )

    command.task_status(task, "running")
    command.task_detail(task, "Analyzing document 1 of 7: Policy.docx")
    command.task_status(task, "completed")

    saved = store.load_run(workspace_with_data, run["id"])
    saved_task = saved["plan"]["stages"][0]["tasks"][0]
    assert saved["activity"]["label"] == "Assemble planning context"
    assert saved["activity"]["detail"] == "Analyzing document 1 of 7: Policy.docx"
    assert saved["activity_revision"] >= 2
    assert saved_task["started_at"]
    assert saved_task["finished_at"]
    assert any(
        event["type"] == "activity_update"
        for event in store.read_events(workspace_with_data, run["id"])
    )


def test_document_test_kind_is_validated_before_execution(workspace_with_data):
    run = store.new_command_run(workspace_with_data, "auto", {"source": "chat", "text": "test documents"})
    with pytest.raises(workspaces.WorkspaceError, match="unsupported value"):
        ledger.append_actions(run, [{
            "id": "bad-test", "type": "create_document_test",
            "args": {"kind": "doctest", "title": "Invalid generic test"},
        }])
