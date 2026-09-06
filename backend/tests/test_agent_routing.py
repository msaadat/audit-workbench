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
from app.agent import action_execution, actions, routing, runner, store
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
# The routing matrix, after step 7: four deterministic cases, then the loop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "route", "definition", "outcomes", "decided_by"),
    [
        # 1. The coordinator handed it to the loop.
        (
            {"source": "loop", "text": "Redraft DT-1 and check it"},
            "agent", None, [], "loop_source",
        ),
        # 2. Explicit outcomes — a tab button, a suggestion, a queued follow-up.
        (
            {"source": "tab_button", "text": "Draft the APM",
             "requested_outcomes": ["planning.apm_ready"]},
            "workflow", AUDIT, ["planning.apm_ready"], "explicit_outcomes",
        ),
        (
            {"source": "follow_up", "text": "pin this result to the dashboard",
             "requested_outcomes": ["planning.apm_ready"]},
            "workflow", AUDIT, ["planning.apm_ready"], "explicit_outcomes",
        ),
        # 3. A registered goal template — a slash command or a chat phrase.
        (
            {"source": "goal_template", "text": "Draft the APM",
             "goal_template": "apm_only"},
            "workflow", AUDIT, ["planning.apm_ready"], "goal_template",
        ),
        # 4. The one phrase that can mean nothing else.
        (
            {"source": "chat", "text": "Complete the audit"},
            "workflow", AUDIT, list(audit_capabilities.FULL_AUDIT_OUTCOMES),
            "lifecycle_completion",
        ),
        # 5. Everything else is a sentence, and a sentence is the loop's. Each
        # of these used to be decided by a phrase table that guessed an outcome
        # set from wording without ever reading the workspace.
        ({"source": "chat", "text": "Draft the APM"}, "agent", None, [], "text_request"),
        ({"source": "chat", "text": "Regenerate the RCM"}, "agent", None, [], "text_request"),
        ({"source": "chat", "text": "Run the RCM tests"}, "agent", None, [], "text_request"),
        ({"source": "chat", "text": "Attach this invoice to DT-1"}, "agent", None, [], "text_request"),
        ({"source": "chat", "text": "Rename the transactions ruleset"}, "agent", None, [], "text_request"),
        ({"source": "chat", "text": "Regenerate the APM. Then pin the revenue tile."},
         "agent", None, [], "text_request"),
        ({"source": "chat", "text": "Please handle the outstanding work appropriately"},
         "agent", None, [], "text_request"),
    ],
)
def test_routing_matrix(command, route, definition, outcomes, decided_by):
    resolved = routing.classify_command(command)

    assert resolved["route"] == route
    assert resolved["decided_by"] == decided_by
    assert resolved["workflow_definition"] == definition
    assert resolved["requested_outcomes"] == outcomes
    assert resolved["engine"] == routing.ENGINE_BY_ROUTE[route]


def test_every_command_resolves_to_an_engine():
    """There is no pending route and no router turn to wait for."""

    for text in (
        "Please handle the outstanding work appropriately",
        "I uploaded document XX, revise the APM and RCM as appropriate",
        "",
    ):
        resolved = routing.classify_command({"source": "chat", "text": text})
        assert resolved["engine"] in (store.WORKFLOW_ENGINE, store.AGENT_ENGINE)
    assert not hasattr(routing, "pending_route")
    assert not hasattr(routing, "CommandRouter")
    assert not hasattr(routing, "resolve_pending_route")


def test_a_forced_regeneration_still_travels_with_the_request():
    """The loop reads the auditor's own force phrase off the route it was given."""

    resolved = routing.classify_command(
        {"source": "loop", "text": "Regenerate the APM from scratch"}
    )

    assert resolved["route"] == "agent"
    assert resolved["generation_mode"] == "force"


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
# P11.4 — one normalized route and engine before thread launch
# --------------------------------------------------------------------------- #
def test_a_workflow_route_is_persisted_and_materialized_before_launch(
    workspace_with_data,
):
    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {
            "source": "goal_template",
            "text": "Draft the APM",
            "goal_template": "apm_only",
        },
    )

    assert routing.resolve_route(workspace_with_data, run) == store.WORKFLOW_ENGINE
    persisted = store.load_run(workspace_with_data, run["id"])

    assert persisted["engine"] == store.WORKFLOW_ENGINE
    assert persisted["schema_version"] == 3
    assert persisted["route"]["status"] == "resolved"
    assert persisted["route"]["route"] == "workflow"
    assert persisted["route"]["decided_by"] == "goal_template"
    assert persisted["workflow"]["definition"] == AUDIT
    assert persisted["usage"]["llm_turns"] == 0
    # The projection the API and drawer read carries the same route.
    assert store.run_summary(persisted)["route"] == persisted["route"]


def test_a_text_request_is_persisted_as_an_agent_route_before_launch(
    workspace_with_data,
):
    """A sentence resolves to the loop with no model turn and no pending state."""

    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Regenerate the APM. Then pin the revenue tile."},
    )

    assert routing.resolve_route(workspace_with_data, run) == store.AGENT_ENGINE
    persisted = store.load_run(workspace_with_data, run["id"])

    assert persisted["engine"] == store.AGENT_ENGINE
    assert persisted["route"]["status"] == "resolved"
    assert persisted["route"]["decided_by"] == "text_request"
    assert persisted["usage"]["llm_turns"] == 0
    # A compound request is no longer a clarification: it is one request the
    # loop can carry out as two children.
    assert "workflow" not in persisted


def test_a_persisted_clarification_route_still_finishes_its_run(
    monkeypatch, workspace_with_data
):
    """Nothing produces one any more; a record written before step 7 might."""

    fake = _configured(monkeypatch)
    run = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "Something ambiguous"}
    )
    run["route"] = routing.normalize_route(
        "clarification",
        decided_by="compound_request",
        clarification="Send them as two requests.",
    )
    run["engine"] = None
    store.save_run(workspace_with_data, run)

    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    assert routing.dispatch_engine(
        workspace_with_data, store.load_run(workspace_with_data, run["id"]), handle
    ) is None

    completed = store.load_run(workspace_with_data, run["id"])
    assert completed["status"] == "completed_with_open_items"
    assert completed["command"]["status"] == "completed"
    assert "two requests" in completed["summary_markdown"]
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# P11.5 — dispatch by explicit engine only
# --------------------------------------------------------------------------- #
def test_dispatch_reads_only_the_explicit_engine(workspace_with_data):
    # The switch itself, which a top-level run reaches through ``_execute`` and
    # a child run of the steering loop reaches inline.
    source = inspect.getsource(runner._run_engine)

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
    # Phase 12 retired the legacy ``analysis`` pipeline, and the agent-loop
    # step added the steering loop: three schedulers plus the one justified
    # protocol engine.
    assert store.RUN_ENGINES == frozenset({"workflow", "agent", "intake"})
    assert store.COMMAND_ENGINES == frozenset({"workflow", "agent"})
    assert set(store.PROTOCOL_ENGINE_BY_RUN_KIND) == {"intake"}


# --------------------------------------------------------------------------- #
# P11.6 — no duplicated local resolution, no cross-scheduler fallback
# --------------------------------------------------------------------------- #
def test_no_engine_classifies_or_reaches_into_another():
    from app.agent import agent_loop

    for engine in (WorkflowRunner, agent_loop.AgentLoop, action_execution.ActionExecution):
        source = inspect.getsource(engine)
        assert "classify_command" not in source
        assert "resolve_route" not in source
        assert "dispatch_engine" not in source

    assert "AgentLoop(" not in inspect.getsource(WorkflowRunner)
    # The steering loop composes the other two through their own entry points
    # and imports no capability, worker or executor module of its own.
    loop_source = inspect.getsource(agent_loop)
    assert "WorkflowRunner(" not in loop_source
    assert "from .capabilities" not in loop_source
    assert "from .workers" not in loop_source
    assert "from .executors" not in loop_source
    # Action execution is no longer an engine: it has no ``execute`` of its own
    # and nothing dispatches to it.
    assert not hasattr(action_execution.ActionExecution, "execute")


def test_the_classification_runs_exactly_once_per_run(monkeypatch, workspace_with_data):
    calls: list[str] = []
    original = routing.classify_command

    def counting(command):
        calls.append(inspect.stack()[1].function)
        return original(command)

    monkeypatch.setattr(routing, "classify_command", counting)
    _configured(
        monkeypatch,
        {"agent:loop": {"content": "Nothing needed doing."}},
    )

    started = runner.start_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Please handle the outstanding work appropriately"},
    )
    completed = wait_run(workspace_with_data, started["id"])

    assert completed["engine"] == store.AGENT_ENGINE
    assert completed["route"]["decided_by"] == "text_request"
    # Once, at creation. Nothing downstream re-classifies: there is no router
    # turn to spend and no second opinion to take.
    assert calls == ["resolve_route"]


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
        workspace_with_data,
        "auto",
        {"source": "goal_template", "text": "Draft the APM", "goal_template": "apm_only"},
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
    # A queued follow-up is a sentence, so it drains to the steering loop.
    assert follow_up["route"]["route"] == "agent"


def test_retry_and_continue_link_to_their_parent_run(monkeypatch, workspace_with_data):
    _configured(monkeypatch)
    monkeypatch.setattr(runner, "_launch", lambda *args: None)

    failed = store.new_command_run(
        workspace_with_data,
        "permission",
        {"source": "goal_template", "text": "Draft the APM", "goal_template": "apm_only"},
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


def test_a_text_run_can_still_queue_and_be_retried(workspace_with_data):
    """Command-ness is the record shape, and a text run is one like any other."""
    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "Please handle the outstanding work appropriately"},
    )
    routing.resolve_route(workspace_with_data, run)
    persisted = store.load_run(workspace_with_data, run["id"])

    assert persisted["engine"] == store.AGENT_ENGINE
    assert store.is_command_run(persisted) is True

    # With no live handle the message is persisted for the run to pick up.
    queued = runner.steer(workspace_with_data, run["id"], "and pin the result")
    assert queued["handled"] == "queued_command"
    assert store.load_run(workspace_with_data, run["id"])["pending_commands"]


# --------------------------------------------------------------------------- #
# The instruction travels from the run's context into workflow scope (step 3)
# --------------------------------------------------------------------------- #
def test_install_resolution_copies_the_instruction_into_scope(workspace_with_data):
    ws = workspace_with_data
    run = store.new_command_run(
        ws,
        "auto",
        {"source": "chat", "text": "Redraft the memorandum."},
        context={"instruction": "Every template section must contain text."},
    )

    routing.install_resolution(
        ws,
        run,
        {
            "requested_outcomes": ["planning.apm_ready"],
            "target_refs": ["workspace:current"],
            "generation_mode": "force",
        },
    )

    assert run["workflow"]["scope"]["instruction"] == (
        "Every template section must contain text."
    )


def test_a_run_with_no_instruction_has_no_instruction_key(workspace_with_data):
    """Absent, not empty: an unsteered run declares the optional source absent."""
    ws = workspace_with_data
    run = store.new_command_run(
        ws, "auto", {"source": "chat", "text": "Draft the memorandum."}
    )

    routing.install_resolution(
        ws,
        run,
        {
            "requested_outcomes": ["planning.apm_ready"],
            "target_refs": ["workspace:current"],
        },
    )

    assert "instruction" not in run["workflow"]["scope"]


def _failed_run(monkeypatch, workspace, *, target_refs=None):
    _configured(monkeypatch)
    monkeypatch.setattr(runner, "_launch", lambda *args: None)
    failed = store.new_command_run(
        workspace,
        "auto",
        {
            "source": "chat",
            "text": "Draft the findings",
            "requested_outcomes": ["findings.drafted"],
            **({"target_refs": list(target_refs)} if target_refs else {}),
        },
    )
    routing.resolve_route(workspace, failed)
    failed = store.load_run(workspace, failed["id"])
    failed["status"] = "failed"
    failed["finished"] = store.utcnow()
    store.save_run(workspace, failed)
    return failed


def test_a_retry_with_an_instruction_carries_it_into_scope(
    monkeypatch, workspace_with_data
):
    """A retry with nothing new to say repeats the ask that already failed."""
    failed = _failed_run(monkeypatch, workspace_with_data)

    retried = runner.retry_run(
        workspace_with_data,
        failed["id"],
        instruction="Every template section must contain text.",
    )

    assert retried["context"]["instruction"] == (
        "Every template section must contain text."
    )
    assert retried["workflow"]["scope"]["instruction"] == (
        "Every template section must contain text."
    )


def test_a_plain_retry_does_not_inherit_the_last_attempts_instruction(
    monkeypatch, workspace_with_data
):
    """The instruction belongs to the ask that carried it, not to the run."""
    failed = _failed_run(monkeypatch, workspace_with_data)
    first = runner.retry_run(
        workspace_with_data, failed["id"], instruction="Say more about cause."
    )
    first = store.load_run(workspace_with_data, first["id"])
    first["status"] = "failed"
    first["finished"] = store.utcnow()
    store.save_run(workspace_with_data, first)

    second = runner.retry_run(workspace_with_data, first["id"])

    assert "instruction" not in second["context"]
    assert "instruction" not in second["workflow"]["scope"]


def test_a_retry_may_narrow_to_part_of_what_the_command_covered(
    monkeypatch, workspace_with_data
):
    failed = _failed_run(
        monkeypatch,
        workspace_with_data,
        target_refs=["rcm:RCM-1", "rcm:RCM-2"],
    )

    narrowed = runner.retry_run(
        workspace_with_data, failed["id"], target_refs=["rcm:RCM-2"]
    )

    assert narrowed["command"]["target_refs"] == ["rcm:RCM-2"]
    # Without one it still carries what the linked command carried.
    assert runner.retry_run(workspace_with_data, failed["id"])["command"][
        "target_refs"
    ] == ["rcm:RCM-1", "rcm:RCM-2"]
