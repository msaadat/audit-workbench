"""Phase 11 gate: one classification, one persisted route, one engine.

The assertions here are the phase's exit gate, in the order the plan states it:
the routing matrix (`P11.2`/`P11.2A`), the bounded router-worker schema
(`P11.3`), route/engine persistence before thread launch (`P11.4`),
engine-only dispatch (`P11.5`), no duplicated local resolution or
cross-scheduler fallback (`P11.6`), and the scheduling invariants routing must
not have disturbed (`P11.7`).
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path

import pytest

from app import llm, workspaces
from app.agent import action_runner, actions, routing, runner, store
from app.agent import capabilities as audit_capabilities
from app.agent.runtime import WorkflowRunner
from app.workspaces import WorkspaceError
from conftest import FakeAgentLLM, wait_run


AUDIT = "audit_workflow_v3"
ANALYSIS = "analysis_workflow_v1"
DOCUMENTS = "documents_workflow_v1"
DOC_TESTS = "doc_tests_workflow_v2"


def _configured(monkeypatch, overrides: dict | None = None) -> FakeAgentLLM:
    fake = FakeAgentLLM(overrides or {})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    return fake


# --------------------------------------------------------------------------- #
# P11.2 / P11.2A — the routing matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "route", "definition", "outcomes", "generation_mode"),
    [
        # Prepare a planning deliverable → workflow with reuse_existing.
        ("Draft the APM", "workflow", AUDIT, ["planning.apm_ready"], "reuse_existing"),
        ("Generate the RCM", "workflow", AUDIT, ["planning.rcm_ready"], "reuse_existing"),
        # Improve / regenerate / refresh → workflow with explicit force.
        ("Regenerate the APM", "workflow", AUDIT, ["planning.apm_ready"], "force"),
        ("Refresh the RCM", "workflow", AUDIT, ["planning.rcm_ready"], "force"),
        ("Improve the APM", "workflow", AUDIT, ["planning.apm_ready"], "force"),
        # Lifecycle-wide completion.
        (
            "Complete the audit",
            "workflow",
            AUDIT,
            audit_capabilities.FULL_AUDIT_OUTCOMES,
            "reuse_existing",
        ),
        # Infer joins and analyze tables.
        ("Perform relevant joins and data analysis", "workflow", ANALYSIS, ["analysis.summarized"], "reuse_existing"),
        # Analyze selected documents.
        ("Analyse the selected documents", "workflow", DOCUMENTS, ["documents.analysis_generated"], "reuse_existing"),
        # Declared RCM fieldwork.
        (
            "Run the RCM tests",
            "workflow",
            AUDIT,
            ["fieldwork.executed", "results.rolled_up"],
            "reuse_existing",
        ),
        # Executing one named Document Test is workflow-owned (P11.2A).
        ("Run document test DT-1", "workflow", DOC_TESTS, ["doc_tests.executed"], "reuse_existing"),
        # Attach / detach / rename / delete / manually edit / pin → action.
        ("Attach this invoice to DT-1", "action", None, [], "reuse_existing"),
        ("Detach the policy from DT-1", "action", None, [], "reuse_existing"),
        ("Delete the duplicate invoices analysis", "action", None, [], "reuse_existing"),
        ("Rename the transactions ruleset", "action", None, [], "reuse_existing"),
        ("Replace this APM paragraph with a shorter one", "action", None, [], "reuse_existing"),
        ("Pin this result to the dashboard", "action", None, [], "reuse_existing"),
        # Rerun one identified existing test → action.
        ("Rerun the saved analysis for transactions", "action", None, [], "reuse_existing"),
    ],
)
def test_routing_matrix(text, route, definition, outcomes, generation_mode):
    resolved = routing.classify_command({"source": "chat", "text": text})

    assert resolved is not None, text
    assert resolved["route"] == route
    assert resolved["workflow_definition"] == definition
    assert resolved["requested_outcomes"] == outcomes
    assert resolved["generation_mode"] == generation_mode
    assert resolved["engine"] == routing.ENGINE_BY_ROUTE[route]


def test_compound_cross_engine_request_is_clarified_not_split():
    resolved = routing.classify_command(
        {"source": "chat", "text": "Regenerate the APM. Then pin the revenue tile."}
    )

    assert resolved["route"] == "clarification"
    assert resolved["decided_by"] == "compound_request"
    assert resolved["engine"] is None
    assert "two requests" in resolved["clarification"]


def test_a_single_scope_wide_request_is_not_treated_as_compound():
    """A bare "and" joins one request; only strong separators split segments."""
    resolved = routing.classify_command(
        {"source": "chat", "text": "Join the tables and analyse them"}
    )

    assert resolved["route"] == "workflow"
    assert resolved["workflow_definition"] == ANALYSIS


def test_an_unrecognized_request_defers_to_the_bounded_router():
    assert routing.classify_command(
        {"source": "chat", "text": "Please handle the outstanding work appropriately"}
    ) is None


def test_explicit_outcomes_outrank_every_phrase_rule():
    resolved = routing.classify_command(
        {
            "source": "follow_up",
            "text": "pin this result to the dashboard",
            "requested_outcomes": ["planning.apm_ready"],
        }
    )

    assert resolved["route"] == "workflow"
    assert resolved["decided_by"] == "explicit_outcomes"
    assert resolved["requested_outcomes"] == ["planning.apm_ready"]


def test_next_step_outcome_bypasses_text_reparsing():
    resolved = routing.classify_command(
        {
            "source": "tab_button",
            "text": "Generate each executable test the RCM rows need.",
            "requested_outcomes": ["tests.specified"],
            "generation_mode": "reuse_existing",
        }
    )

    assert resolved["route"] == "workflow"
    assert resolved["decided_by"] == "explicit_outcomes"
    assert resolved["requested_outcomes"] == ["tests.specified"]
    assert resolved["generation_mode"] == "reuse_existing"


def test_classification_is_pure(monkeypatch, workspace_with_data):
    """Routing cannot execute actions, gather domain context, or mutate."""
    monkeypatch.setattr(
        llm, "chat", lambda *args, **kwargs: pytest.fail("routing called the provider")
    )
    before = workspaces.load_workspace(workspace_with_data.id).revision

    for text in ("Complete the audit", "Pin this result", "Analyse these documents"):
        routing.classify_command({"source": "chat", "text": text})

    assert workspaces.load_workspace(workspace_with_data.id).revision == before
    source = inspect.getsource(routing.classify_command)
    assert "load_workspace" not in source
    assert "save" not in source


# --------------------------------------------------------------------------- #
# P11.2A — the action catalog owns no workflow-owned generator
# --------------------------------------------------------------------------- #
def test_workflow_owned_generators_are_not_registered_actions():
    registered = {definition.type for definition in actions.REGISTRY.all()}

    assert registered.isdisjoint(
        {
            "generate_apm",
            "infer_relationships",
            "run_document_test",
            "rollup_rcm_results",
            "generate_all_rcm_working_papers",
            "generate_report",
            "verify_audit_completion",
        }
    )
    # Target-specific operations on the same artifact families remain actions.
    assert {
        "edit_apm",
        "create_join",
        "run_data_test",
        "create_document_test",
        "attach_document_to_test",
        "generate_rcm_working_paper",
        "edit_report",
        "reconcile_report",
    } <= registered


def test_no_action_can_answer_a_document_qa_worklist():
    """The removed ``run_document_test`` was the last unbudgeted Q&A path."""
    source = Path(actions.__file__).read_text(encoding="utf-8")

    assert "doc_tests.run_item(" not in source
    assert "document_chat(" not in source


def test_every_registered_goal_template_names_a_declared_outcome_set():
    for template in routing.GOAL_TEMPLATES:
        outcomes = routing.template_outcomes(template)
        assert outcomes, template
        assert routing.validate_requested_outcomes(outcomes)


# --------------------------------------------------------------------------- #
# P11.3 — the bounded router-worker result schema
# --------------------------------------------------------------------------- #
def _router_payload(**overrides) -> dict:
    return {
        "route": "workflow",
        "requested_outcomes": ["planning.apm_ready"],
        "objective": "Draft the APM",
        "target_refs": [],
        "generation_mode": "reuse_existing",
        "action_intent": None,
        "constraints": [],
        "clarification": None,
        **overrides,
    }


def test_router_result_schema_accepts_the_four_declared_results():
    supported = routing.supported_outcomes()

    workflow = routing.validate_router_result(_router_payload(), supported)
    action = routing.validate_router_result(
        _router_payload(
            route="action", requested_outcomes=[], action_intent="run_report_quality"
        ),
        supported,
    )
    clarification = routing.validate_router_result(
        _router_payload(
            route="clarification", requested_outcomes=[], clarification="Which test?"
        ),
        supported,
    )
    unsupported = routing.validate_router_result(
        _router_payload(route="unsupported", requested_outcomes=[]), supported
    )

    assert workflow["engine"] == store.WORKFLOW_ENGINE
    assert workflow["workflow_definition"] == AUDIT
    assert action["engine"] == store.ACTION_ENGINE
    assert action["action_intent"] == "run_report_quality"
    assert clarification["engine"] is None
    assert unsupported["engine"] is None
    assert all(
        result["decided_by"] == "router_worker"
        for result in (workflow, action, clarification, unsupported)
    )


@pytest.mark.parametrize(
    "payload",
    [
        _router_payload(route="generic_action"),
        _router_payload(requested_outcomes=["planning.not_a_capability"]),
        _router_payload(requested_outcomes=[]),
        _router_payload(generation_mode="always"),
        _router_payload(route="clarification", requested_outcomes=[], clarification=""),
        _router_payload(
            route="action", requested_outcomes=[], action_intent="write_json"
        ),
    ],
)
def test_router_result_schema_rejects_unsupported_results(payload):
    with pytest.raises(ValueError):
        routing.validate_router_result(payload, routing.supported_outcomes())


def test_router_result_accepts_the_audit_owned_apm_and_analysis_scope():
    resolved = routing.validate_router_result(
        _router_payload(
            requested_outcomes=["planning.apm_ready", "analysis.executed"]
        ),
        routing.supported_outcomes(),
    )

    assert resolved["workflow_definition"] == AUDIT


def test_action_intent_is_validated_against_the_action_registry():
    assert routing.validate_action_intent(None) == "isolated_mutation"
    assert routing.validate_action_intent("edit_finding") == "edit_finding"
    with pytest.raises(WorkspaceError):
        routing.validate_action_intent("regenerate_everything")


# --------------------------------------------------------------------------- #
# P11.4 — one normalized route and engine before thread launch
# --------------------------------------------------------------------------- #
def test_a_workflow_route_is_persisted_and_materialized_before_launch(
    workspace_with_data,
):
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
    )

    assert routing.resolve_route(workspace_with_data, run) == store.WORKFLOW_ENGINE
    persisted = store.load_run(workspace_with_data, run["id"])

    assert persisted["engine"] == store.WORKFLOW_ENGINE
    assert persisted["schema_version"] == 3
    assert persisted["route"]["status"] == "resolved"
    assert persisted["route"]["route"] == "workflow"
    assert persisted["route"]["decided_by"] == "workflow_generation"
    assert persisted["workflow"]["definition"] == AUDIT
    assert persisted["usage"]["llm_turns"] == 0
    # The projection the API and drawer read carries the same route.
    assert store.run_summary(persisted)["route"] == persisted["route"]


def test_a_clarification_route_persists_no_engine_and_finishes_the_run(
    monkeypatch, workspace_with_data
):
    fake = _configured(monkeypatch)

    started = runner.start_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Regenerate the APM. Then pin the revenue tile."},
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["engine"] is None
    assert completed["route"]["route"] == "clarification"
    assert completed["status"] == "completed_with_open_items"
    assert completed["command"]["status"] == "completed"
    assert "two requests" in completed["summary_markdown"]
    assert completed["actions"] == []
    assert fake.calls == []


def test_a_deterministic_route_never_spends_a_router_turn(
    monkeypatch, workspace_with_data
):
    fake = _configured(monkeypatch, {"agent:command_interpreter": {
        "objective": "Attach the invoice", "constraints": [],
        "completion_criteria": [], "needs_planning_wave": False, "actions": [],
    }})

    started = runner.start_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Attach the invoice to DT-1"},
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["engine"] == store.ACTION_ENGINE
    assert completed["route"]["decided_by"] == "isolated_operation"
    assert [call["tag"] for call in fake.calls] == ["agent:command_interpreter"]


# --------------------------------------------------------------------------- #
# P11.5 — dispatch by explicit engine only
# --------------------------------------------------------------------------- #
def test_dispatch_reads_only_the_explicit_engine(workspace_with_data):
    source = inspect.getsource(runner._execute)

    assert 'run.get("kind")' not in source
    assert 'run["kind"]' not in source
    assert 'run.get("schema_version")' not in source
    assert 'run["schema_version"]' not in source
    # Every dispatch branch compares the explicit engine and nothing else.
    branches = [line.strip() for line in source.splitlines() if " engine ==" in line]
    assert len(branches) == len(store.RUN_ENGINES)
    assert all(line.startswith(("if engine ==", "elif engine ==")) for line in branches)


@pytest.mark.parametrize("retired", ["doc_test", "document_analysis", "analysis"])
def test_a_record_without_a_supported_engine_fails_closed(workspace_with_data, retired):
    run = store.new_run(workspace_with_data, "auto", None, kind="intake")
    run["engine"] = retired
    run["route"] = None
    store.save_run(workspace_with_data, run)

    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    with pytest.raises(WorkspaceError, match="unsupported"):
        routing.dispatch_engine(
            workspace_with_data, store.load_run(workspace_with_data, run["id"]), handle
        )

    run["engine"] = None
    store.save_run(workspace_with_data, run)
    with pytest.raises(WorkspaceError, match="missing"):
        routing.dispatch_engine(
            workspace_with_data, store.load_run(workspace_with_data, run["id"]), handle
        )


def test_supported_engine_set_matches_the_phase_10_decision_record():
    # Phase 12 retired the legacy ``analysis`` pipeline, so the decision
    # record's table is now exactly two schedulers plus the one justified
    # protocol engine.
    assert store.RUN_ENGINES == frozenset({"workflow", "action", "intake"})
    assert store.COMMAND_ENGINES == frozenset({"workflow", "action"})
    assert set(store.PROTOCOL_ENGINE_BY_RUN_KIND) == {"intake"}


# --------------------------------------------------------------------------- #
# P11.6 — no duplicated local resolution, no cross-scheduler fallback
# --------------------------------------------------------------------------- #
def test_no_scheduler_classifies_or_calls_the_other_scheduler():
    for scheduler in (WorkflowRunner, action_runner.ActionRunner):
        source = inspect.getsource(scheduler)
        assert "classify_command" not in source
        assert "resolve_route" not in source
        assert "resolve_pending_route" not in source
        assert "dispatch_engine" not in source

    workflow_source = inspect.getsource(WorkflowRunner)
    assert "ActionRunner(" not in workflow_source
    action_source = inspect.getsource(action_runner.ActionRunner)
    assert "WorkflowRunner(" not in action_source
    # The action scheduler's only routing dependency is the shared ownership
    # rule, so its guard and the persisted route can never disagree.
    assert "routing.workflow_owned_request" in action_source


def test_the_deterministic_pass_runs_exactly_once_per_run(
    monkeypatch, workspace_with_data
):
    calls: list[str] = []
    original = routing.classify_command

    def counting(command):
        calls.append(inspect.stack()[1].function)
        return original(command)

    monkeypatch.setattr(routing, "classify_command", counting)
    _configured(
        monkeypatch,
        {
            "agent:workflow_router": {
                "route": "action",
                "requested_outcomes": [],
                "objective": "Check report quality",
                "target_refs": [],
                "generation_mode": "reuse_existing",
                "action_intent": "run_report_quality",
                "constraints": [],
                "clarification": None,
            },
            "agent:command_interpreter": {
                "objective": "Check report quality", "constraints": [],
                "completion_criteria": [], "needs_planning_wave": False,
                "actions": [{"id": "quality", "type": "run_report_quality", "args": {}}],
            },
        },
    )

    started = runner.start_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Please handle the outstanding work appropriately"},
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["engine"] == store.ACTION_ENGINE
    assert completed["route"]["decided_by"] == "router_worker"
    # Routing classifies once, at creation. The bounded router does not repeat
    # the deterministic pass, and the only other caller is the action
    # scheduler's defensive ownership guard.
    assert calls == ["resolve_route", "workflow_owned_request"]
    assert "classify_command" not in inspect.getsource(routing.CommandRouter.resolve)
    assert "classify_command" not in inspect.getsource(routing.resolve_pending_route)


# --------------------------------------------------------------------------- #
# P11.7 — the scheduling invariants routing must preserve
# --------------------------------------------------------------------------- #
def test_one_live_run_per_workspace_and_a_global_concurrency_cap(
    monkeypatch, workspace_with_data
):
    _configured(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(runner, "_execute", lambda *args: release.wait(10))

    first = runner.start_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
    )
    try:
        with pytest.raises(runner.AgentBusyError):
            runner.start_command_run(
                workspace_with_data, "auto", {"source": "chat", "text": "Generate the RCM"}
            )

        other = workspaces.create_workspace("Second engagement")
        monkeypatch.setattr(runner, "_max_concurrent", lambda: 1)
        with pytest.raises(runner.AgentBusyError):
            runner.start_command_run(
                other, "auto", {"source": "chat", "text": "Draft the APM"}
            )
    finally:
        release.set()
        handle = runner.get_handle(first["id"])
        if handle is not None and handle.thread is not None:
            handle.thread.join(timeout=5)


def test_queued_commands_keep_fifo_order_and_survive_a_terminal_crash(
    monkeypatch, workspace_with_data
):
    _configured(monkeypatch)
    started = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Draft the APM"}
    )
    routing.resolve_route(workspace_with_data, started)
    started = store.load_run(workspace_with_data, started["id"])
    started["status"] = "failed"
    started["error"] = "worker crashed"
    started["finished"] = store.utcnow()
    started["pending_commands"] = [
        {"id": "cmd_first", "source": "follow_up", "text": "Attach the invoice to DT-1"},
        {"id": "cmd_second", "source": "follow_up", "text": "Pin this result"},
    ]
    store.save_run(workspace_with_data, started)

    launched: list[dict] = []
    monkeypatch.setattr(runner, "_launch", lambda *args: launched.append(args))

    runner._launch_next_command(workspace_with_data, started)
    remaining = store.load_run(workspace_with_data, started["id"])["pending_commands"]

    assert [item["text"] for item in remaining] == ["Pin this result"]
    assert len(launched) == 1
    follow_up = next(
        item
        for item in store.list_runs(workspace_with_data)
        if item["parent_run_id"] == started["id"]
    )
    assert follow_up["route"]["route"] == "action"


def test_retry_and_continue_link_to_their_parent_run(monkeypatch, workspace_with_data):
    _configured(monkeypatch)
    monkeypatch.setattr(runner, "_launch", lambda *args: None)

    failed = store.new_command_run(
        workspace_with_data, "permission", {"source": "chat", "text": "Draft the APM"}
    )
    routing.resolve_route(workspace_with_data, failed)
    failed = store.load_run(workspace_with_data, failed["id"])
    failed["status"] = "failed"
    failed["finished"] = store.utcnow()
    store.save_run(workspace_with_data, failed)

    retried = runner.retry_run(workspace_with_data, failed["id"])
    assert retried["parent_run_id"] == failed["id"]
    assert retried["engine"] == store.WORKFLOW_ENGINE
    assert retried["route"]["decided_by"] == "explicit_outcomes"

    completed = store.load_run(workspace_with_data, retried["id"])
    completed["status"] = "completed_with_open_items"
    completed["finished"] = store.utcnow()
    completed["workflow"]["next_outcomes"] = ["planning.rcm_ready"]
    store.save_run(workspace_with_data, completed)

    continued = runner.continue_audit(workspace_with_data, completed["id"])
    assert continued["parent_run_id"] == completed["id"]
    assert continued["engine"] == store.WORKFLOW_ENGINE
    assert continued["workflow"]["requested_outcomes"] == ["planning.rcm_ready"]


def test_a_pending_route_run_can_still_queue_and_be_retried(workspace_with_data):
    """Command-ness is the record shape, not the engine (which is not yet set)."""
    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Please handle the outstanding work appropriately"},
    )
    routing.resolve_route(workspace_with_data, run)
    persisted = store.load_run(workspace_with_data, run["id"])

    assert persisted["engine"] is None
    assert store.is_command_run(persisted) is True

    queued = runner.steer(workspace_with_data, run["id"], "and pin the result")
    assert queued["handled"] == "queued_command"
    assert store.load_run(workspace_with_data, run["id"])["pending_commands"]
