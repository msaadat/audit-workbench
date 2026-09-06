"""Step 4 gate: the agent engine is a durable, budgeted, gated tool loop.

The loop steers; the registry gates. Every test here is about one half of that:
that the loop can decide and carry work out through ordinary child runs, and
that what it may decide is bounded by code rather than by its prompt.
"""

from __future__ import annotations

import json

import pytest

from app import assistant, assistant_chats, llm, workspaces
from app.agent import agent_loop, loop_tools, routing, runner, store
from app.workspaces import WorkspaceError
from conftest import FakeAgentLLM, wait_run


APM_OUTCOME = "planning.apm_ready"


class LoopScript:
    """A scripted sequence of ``agent:loop`` turns, one per call."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = 0
        self.conversations: list[list[dict]] = []

    def __call__(self, _user: str) -> dict:
        index = min(self.calls, len(self.turns) - 1)
        self.calls += 1
        turn = self.turns[index]
        return turn(self) if callable(turn) else turn


def tool_turn(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def finish_turn(summary: str, **extra) -> dict:
    return tool_turn("finish", {"summary": summary, **extra}, call_id="call_finish")


#: A memorandum that passes the template gate, for tests whose subject is the
#: loop rather than the planning worker.
COMPLETE_APM = (
    "# Audit Planning Memorandum\n\n## Engagement\n\nEntity and scope.\n\n"
    "## Introduction and background\n\nPayments background.\n\n"
    "## Process flow and understanding\n\nRequisition through payment.\n\n"
    "## Prior audit findings\n\nNo information available.\n\n"
    "## Data analytics performed\n\nNo data analysis has been performed.\n\n"
    "## Fraud risk and management override\n\nManagement override is presumed.\n\n"
    "## Key risks and planned response\n\nTest approval compliance.\n\n"
    "## Planning assumptions and matters reported\n- Policy currency is assumed."
)
APM_DRAFT = {"apm_markdown": COMPLETE_APM}


def configured(monkeypatch, script, extra: dict | None = None) -> FakeAgentLLM:
    fake = FakeAgentLLM({"agent:loop": script, "agent:apm": APM_DRAFT, **(extra or {})})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    return fake


def start_loop(ws, text="Get the planning memorandum in order.", context=None) -> dict:
    return runner.start_command_run(
        ws,
        "auto",
        {"source": store.LOOP_COMMAND_SOURCE, "text": text},
        context=context,
    )


def start_loop_with_limits(ws, limits: dict, text="Do the planning work.") -> dict:
    """Start a loop whose budgets are set before its thread reads them.

    Editing ``run.json`` after ``start_command_run`` races the worker, which
    holds the record in memory and saves over it on its first transition.
    """

    run = store.new_command_run(
        ws,
        "auto",
        {"source": store.LOOP_COMMAND_SOURCE, "text": text},
        limits=limits,
    )
    routing.resolve_route(ws, run)
    return runner.resume_run(ws, run["id"])


def loop_calls(fake: FakeAgentLLM) -> list[dict]:
    return [call for call in fake.calls if call["tag"] == "agent:loop"]


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def test_a_loop_command_routes_to_the_agent_engine_before_any_phrase_match():
    ws = workspaces.create_workspace("Loop routing")
    # The text is a registered workflow phrase. Source wins: the coordinator
    # already decided this one needs the loop.
    run = store.new_command_run(
        ws, "auto", {"source": "loop", "text": "Draft the APM"}
    )

    assert routing.resolve_route(ws, run) == store.AGENT_ENGINE
    persisted = store.load_run(ws, run["id"])
    assert persisted["engine"] == store.AGENT_ENGINE
    assert persisted["route"]["decided_by"] == "loop_source"
    assert persisted["route"]["requested_outcomes"] == []
    # Same text without the loop source still routes to the workflow engine.
    assert routing.classify_command({"source": "chat", "text": "Draft the APM"})[
        "engine"
    ] == store.WORKFLOW_ENGINE


def test_a_loop_run_carries_its_own_budgets():
    ws = workspaces.create_workspace("Loop budgets")
    run = store.new_command_run(ws, "auto", {"source": "loop", "text": "Do a thing"})

    assert run["limits"]["max_loop_turns"] == 24
    assert run["limits"]["max_child_runs"] == 6
    assert run["limits"]["max_tool_calls"] == 60
    assert run["limits"]["max_auditor_questions"] == 3


# --------------------------------------------------------------------------- #
# Answering, planning, running
# --------------------------------------------------------------------------- #
def test_a_loop_that_answers_without_tools_completes_with_one_message(
    monkeypatch, workspace_with_data
):
    script = LoopScript({"content": "Nothing needed doing: the APM is current."})
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data)
    run = wait_run(workspace_with_data, started["id"])

    assert run["status"] == "completed"
    assert not run["children"]
    agent_messages = [item for item in run["messages"] if item["role"] == "agent"]
    assert len(agent_messages) == 1
    assert "current" in agent_messages[0]["content"]


def test_plan_then_run_produces_one_child_run_and_a_closing_summary(
    monkeypatch, workspace_with_data
):
    script = LoopScript(
        tool_turn("plan_outcomes", {"requested_outcomes": [APM_OUTCOME]}),
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}, "call_2"),
        finish_turn("The planning memorandum is drafted."),
    )
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data)
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert run["status"] == "completed"
    assert len(run["children"]) == 1
    child = store.load_run(workspace_with_data, run["children"][0])
    assert child["engine"] == store.WORKFLOW_ENGINE
    assert child["parent_run_id"] == run["id"]
    assert child["status"] == "completed"
    assert child["workflow"]["requested_outcomes"] == [APM_OUTCOME]
    # The model's prose, then the ledger's own account of what committed.
    closing = run["messages"][-1]["content"]
    assert closing.startswith("The planning memorandum is drafted.")
    assert "What the runs actually did:" in closing
    assert "1 item committed" in closing
    assert APM_OUTCOME in {
        item["capability"] for item in run["committed_account"][0]["committed"]
    }
    assert workspace_with_data.reload().planning.get("apm_markdown")


def test_plan_outcomes_reads_without_starting_anything(monkeypatch, workspace_with_data):
    script = LoopScript(
        tool_turn("plan_outcomes", {"requested_outcomes": [APM_OUTCOME]}),
        finish_turn("Here is what that would involve."),
    )
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data)
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert run["status"] == "completed"
    assert run["children"] == []
    # The plan reached the model as a tool result, with the stages it would run.
    conversation = agent_loop.ConversationStore(
        workspace_with_data, run["id"]
    ).load()["messages"]
    plan = json.loads(
        next(item for item in conversation if item["role"] == "tool")["content"]
    )
    assert plan["definition"] == "audit_workflow_v3"
    assert [stage["capability"] for stage in plan["stages"]]
    assert plan["estimated_model_turns"] >= 1


# --------------------------------------------------------------------------- #
# Guards that are not prompt-only
# --------------------------------------------------------------------------- #
def test_whole_workspace_force_is_refused_without_the_auditors_own_words(
    monkeypatch, workspace_with_data
):
    script = LoopScript(
        tool_turn(
            "run_outcomes",
            {
                "requested_outcomes": [APM_OUTCOME],
                "target_refs": ["workspace:current"],
                "generation_mode": "force",
            },
        ),
        finish_turn("I did not redo everything; say the word if you want that."),
    )
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data)
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert run["children"] == []
    conversation = agent_loop.ConversationStore(workspace_with_data, run["id"]).load()["messages"]
    refusal = json.loads(
        next(item for item in conversation if item["role"] == "tool")["content"]
    )
    assert "whole workspace" in refusal["error"]


def test_confirmed_force_is_allowed_through(monkeypatch, workspace_with_data):
    script = LoopScript(
        tool_turn(
            "run_outcomes",
            {
                "requested_outcomes": [APM_OUTCOME],
                "target_refs": ["workspace:current"],
                "generation_mode": "force",
            },
        ),
        finish_turn("Redrafted from scratch, as you asked."),
    )
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data, context={"force_confirmed": True})
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert len(run["children"]) == 1
    child = store.load_run(workspace_with_data, run["children"][0])
    assert child["workflow"]["generation_mode"] == "force"


def test_the_child_budget_stops_a_loop_that_keeps_starting_runs(
    monkeypatch, workspace_with_data
):
    script = LoopScript(
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}, "call_1"),
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}, "call_2"),
        finish_turn("One run was as far as this request goes."),
    )
    configured(monkeypatch, script)

    started = start_loop_with_limits(
        workspace_with_data, {"max_child_runs": 1}, "Draft and redraft the APM"
    )
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert len(run["children"]) == 1
    conversation = agent_loop.ConversationStore(workspace_with_data, run["id"]).load()["messages"]
    results = [json.loads(item["content"]) for item in conversation if item["role"] == "tool"]
    assert any("limit" in str(item.get("error") or "") for item in results)


def test_a_loop_that_only_reads_is_made_to_decide_and_then_stopped(
    monkeypatch, workspace_with_data
):
    """The second live run's failure: reading forever, committing nothing.

    Reading always looks like progress and never commits, so a request no
    registered outcome can carry out never ends on its own. One nudge, then a
    stop — long before the turn budget spends twenty-four turns learning
    nothing.
    """

    script = LoopScript(tool_turn("get_audit_progress", {}))
    configured(monkeypatch, script)

    started = start_loop_with_limits(workspace_with_data, {"max_read_turns": 2})
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert run["status"] == "completed_with_open_items"
    assert run["children"] == []
    assert "without finding work I could carry out" in run["messages"][-1]["content"]
    # Two reads, a nudge, two more, and it stops — not the 24-turn budget.
    assert run["usage"]["llm_turns"] == 4
    conversation = agent_loop.ConversationStore(workspace_with_data, run["id"]).load()[
        "messages"
    ]
    assert any(
        "turns that only read" in str(item.get("content"))
        for item in conversation
        if item["role"] == "user"
    )


def test_a_turn_that_acts_clears_the_read_counter(monkeypatch, workspace_with_data):
    script = LoopScript(
        tool_turn("get_audit_progress", {}),
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}, "call_2"),
        tool_turn("get_audit_progress", {}, "call_3"),
        finish_turn("Drafted it."),
    )
    configured(monkeypatch, script)

    started = start_loop_with_limits(workspace_with_data, {"max_read_turns": 2})
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert run["status"] == "completed"
    assert len(run["children"]) == 1


def test_the_turn_budget_ends_the_request_with_open_items(
    monkeypatch, workspace_with_data
):
    # A loop that only ever reads never calls finish.
    script = LoopScript(tool_turn("get_audit_progress", {}))
    configured(monkeypatch, script)

    started = start_loop_with_limits(workspace_with_data, {"max_loop_turns": 2})
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert run["status"] == "completed_with_open_items"
    assert "as far as one request goes" in run["messages"][-1]["content"]
    assert run["children"] == []


def test_a_finish_with_an_unfinished_child_is_refused(workspace_with_data, monkeypatch):
    """The guard, unit-tested: ``finish`` reads the children's real statuses."""

    ws = workspace_with_data
    parent = store.new_command_run(ws, "auto", {"source": "loop", "text": "Do work"})
    child = store.new_command_run(ws, "auto", {"source": "follow_up", "text": "Some work"})
    child["status"] = "executing"
    store.save_run(ws, child)
    parent["children"] = [child["id"]]
    store.save_run(ws, parent)

    class _Loop:
        def __init__(self):
            self.ws = ws
            self.run = parent
            self.finished = None

        def save(self):
            store.save_run(ws, parent)

        def request_finish(self, summary, suggestions):
            self.finished = summary

    tools = loop_tools.LoopTools(_Loop())
    with pytest.raises(loop_tools.ToolError):
        tools.finish({"summary": "All done."})


def test_a_unit_is_only_rerun_once_per_request(workspace_with_data):
    ws = workspace_with_data
    parent = store.new_command_run(ws, "auto", {"source": "loop", "text": "Fix it"})
    child = store.new_command_run(ws, "auto", {"source": "follow_up", "text": "Draft"})
    child["workflow"] = {
        "requested_outcomes": [APM_OUTCOME],
        "target_refs": ["workspace:current"],
        "stages": [
            {
                "capability": APM_OUTCOME,
                "title": "Planning memorandum",
                "units": [
                    {
                        "id": "apm:1",
                        "status": "failed",
                        "parent_refs": ["planning:current"],
                        "error": "narrative section 'Condition' is empty",
                    }
                ],
            }
        ],
    }
    child["status"] = "completed_with_failures"
    store.save_run(ws, child)
    parent["children"] = [child["id"]]
    parent["repairs"] = [{"unit_id": "apm:1", "from_run_id": child["id"]}]
    store.save_run(ws, parent)

    class _Loop:
        ws = workspace_with_data
        run = parent

        def save(self):
            store.save_run(workspace_with_data, parent)

    tools = loop_tools.LoopTools(_Loop())
    with pytest.raises(loop_tools.ToolError) as error:
        tools.rerun_units({"run_id": child["id"], "unit_ids": ["apm:1"]})
    assert "already been run again once" in str(error.value)


def test_an_outcome_set_no_workflow_owns_starts_nothing(monkeypatch, workspace_with_data):
    """A refusal is a tool result, and leaves no half-created run behind."""

    script = LoopScript(
        # Both are registered outcomes, but of two different workflows.
        tool_turn(
            "run_outcomes",
            {
                "requested_outcomes": [
                    "analysis.definitions_ready",
                    "doc_tests.dispositioned",
                ]
            },
        ),
        finish_turn("Those two do not belong to one workflow."),
    )
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data)
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    assert run["children"] == []
    conversation = agent_loop.ConversationStore(workspace_with_data, run["id"]).load()[
        "messages"
    ]
    refusal = json.loads(
        next(item for item in conversation if item["role"] == "tool")["content"]
    )
    assert "one registered workflow" in refusal["error"]


def test_reading_a_run_is_open_and_changing_one_is_not(workspace_with_data):
    """Review is a read; rerunning someone else's units needs that read first."""

    ws = workspace_with_data
    parent = store.new_command_run(ws, "auto", {"source": "loop", "text": "Look"})
    other = store.new_command_run(ws, "auto", {"source": "chat", "text": "Elsewhere"})
    other["workflow"] = {
        "requested_outcomes": [APM_OUTCOME],
        "target_refs": ["workspace:current"],
        "stages": [
            {
                "capability": APM_OUTCOME,
                "title": "Planning memorandum",
                "units": [
                    {"id": "apm:1", "status": "failed", "parent_refs": ["planning:current"]}
                ],
            }
        ],
    }
    other["status"] = "completed_with_failures"
    store.save_run(ws, other)

    class _Loop:
        pass

    loop = _Loop()
    loop.ws = ws
    loop.run = parent
    loop.save = lambda: store.save_run(ws, parent)
    tools = loop_tools.LoopTools(loop)

    # A run this request never started cannot be reran...
    with pytest.raises(loop_tools.ToolError):
        tools.rerun_units({"run_id": other["id"], "unit_ids": ["apm:1"]})
    # ...until it has been read, which is what "review run X" asks for.
    report = tools.inspect_run({"run_id": other["id"]})
    assert report["status"] == "completed_with_failures"
    assert store.load_run(ws, other["id"])["reviewed_by_run_id"] == parent["id"]
    # A run that does not exist is a tool error, not a crash.
    with pytest.raises(loop_tools.ToolError):
        tools.inspect_run({"run_id": "20260101-000000-nope00"})


# --------------------------------------------------------------------------- #
# The closing message is the ledger's, not the model's
# --------------------------------------------------------------------------- #
def test_a_stage_that_did_nothing_is_named_as_having_done_nothing():
    """The live-run failure, as a unit: a zero-unit stage reports success.

    `doc_tests.definitions_ready` under force expanded no units — the
    capability only defines tests it considers unusable — so the redraft never
    happened, while every status projection said the stage succeeded. The
    account is the one place that says so.
    """

    child = {
        "id": "run-1",
        "workflow": {
            "stages": [
                {
                    "capability": "doc_tests.definitions_ready",
                    "title": "Document test definitions",
                    "status": "succeeded",
                    "units": [],
                    "readiness_before": {"state": "satisfied"},
                },
                {
                    "capability": "doc_tests.executed",
                    "title": "Document test execution",
                    "status": "succeeded",
                    "units": [
                        {"id": "u1", "status": "succeeded", "result_refs": ["doctest:DT-1"]},
                        {"id": "u2", "status": "succeeded", "result_refs": ["doctest:DT-1"]},
                    ],
                },
            ]
        },
    }

    account = loop_tools.run_account(child)

    assert [item["capability"] for item in account["nothing_to_do"]] == [
        "doc_tests.definitions_ready"
    ]
    assert account["committed"] == [
        {
            "capability": "doc_tests.executed",
            "title": "Document test execution",
            "status": "succeeded",
            "units": 2,
            "of": 2,
            "skipped": 0,
            "refs": ["doctest:DT-1"],
            "more_refs": 0,
        }
    ]
    lines = loop_tools.account_sentences([account])
    assert "Document test execution: 2 items committed (doctest:DT-1)." in lines
    assert (
        "Document test definitions: nothing to do, so nothing changed." in lines
    )


def test_the_closing_message_contradicts_a_summary_that_overclaims(
    monkeypatch, workspace_with_data
):
    """A summary the runs do not support is published beside what they did."""

    script = LoopScript(
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}),
        finish_turn("I rewrote every test in the engagement from scratch."),
    )
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data)
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    closing = run["messages"][-1]["content"]
    assert closing.startswith("I rewrote every test in the engagement from scratch.")
    # The claim stands as the model's, and the record's own account stands
    # beside it naming the one capability that actually committed.
    assert "What the runs actually did:" in closing
    assert "test" not in closing.split("What the runs actually did:")[1].lower()
    assert "Audit planning memorandum" in closing or "planning" in closing.lower()


# --------------------------------------------------------------------------- #
# Failure is an observation
# --------------------------------------------------------------------------- #
def test_a_failed_unit_is_reported_with_its_validator_errors_and_rerun_once(
    monkeypatch, workspace_with_data
):
    """The evidence's own failure class, end to end.

    The APM worker is scripted to answer with something the executor refuses,
    then — once the loop has read the rejection and said what to fix — with
    something usable. The loop's second child is an ordinary linked retry
    carrying the instruction as declared context.
    """

    ws = workspace_with_data
    attempts = {"count": 0}

    def apm(_user: str) -> dict:
        attempts["count"] += 1
        if attempts["count"] <= 2:
            # A memorandum missing its template sections: refused after the
            # model turn, by the validator, exactly as in the evidence.
            return {"apm_markdown": "# Audit Planning Memorandum\n\nScope."}
        return {"apm_markdown": COMPLETE_APM}

    inspected: dict = {}

    def after_run(script: LoopScript) -> dict:
        return tool_turn(
            "inspect_run",
            {"run_id": script.last_child},
            "call_inspect",
        )

    script = LoopScript(
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}),
        after_run,
        lambda script: tool_turn(
            "rerun_units",
            {
                "run_id": script.last_child,
                "unit_ids": script.failed_units,
                "instruction": "Every template section must contain text.",
            },
            "call_rerun",
        ),
        finish_turn("Redrafted the memorandum after the first attempt was rejected."),
    )

    # The script needs to read the loop's own tool results to name the run and
    # the units, the same way the model does.
    def read_back(fake: FakeAgentLLM) -> None:
        for call in loop_calls(fake):
            for message in call["messages"]:
                if message.get("role") != "tool":
                    continue
                payload = json.loads(message["content"])
                if isinstance(payload, dict) and payload.get("run_id"):
                    script.last_child = payload["run_id"]
                    units = payload.get("unsettled_units") or []
                    if units:
                        script.failed_units = [unit["unit_id"] for unit in units]
                        inspected.update(units[0])

    class _Fake(FakeAgentLLM):
        def __call__(self, messages, **kwargs):
            read_back(self)
            return super().__call__(messages, **kwargs)

    fake = _Fake({"agent:loop": script, "agent:apm": apm})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    script.last_child = ""
    script.failed_units = []

    started = start_loop(ws, "The memorandum came back empty; sort it out.")
    run = wait_run(ws, started["id"], timeout=90)

    assert len(run["children"]) == 2
    first, second = (store.load_run(ws, run_id) for run_id in run["children"])
    assert first["status"] in {"failed", "completed_with_failures"}
    # The loop saw the validator's own words, not just "a unit failed".
    assert inspected.get("validation_errors") or inspected.get("error")
    # The rerun is a linked retry carrying the instruction as declared context.
    assert second["context"]["instruction"] == "Every template section must contain text."
    assert second["parent_run_id"] == run["id"]
    assert run["repairs"] and run["repairs"][0]["unit_id"] in script.failed_units


# --------------------------------------------------------------------------- #
# Steering
# --------------------------------------------------------------------------- #
def test_steering_a_live_loop_reaches_its_next_turn(monkeypatch, workspace_with_data):
    ws = workspace_with_data
    delivered: list[str] = []

    def first(script: LoopScript) -> dict:
        # Steer while this turn is being decided; the loop reads it next turn.
        runner.steer(ws, script.run_id, "Skip the second row.")
        return tool_turn("get_audit_progress", {})

    def second(script: LoopScript) -> dict:
        return finish_turn("Noted.")

    script = LoopScript(first, second)

    class _Fake(FakeAgentLLM):
        def __call__(self, messages, **kwargs):
            if str(messages[0]["content"]).startswith("[agent:loop]"):
                delivered.append(
                    " | ".join(
                        str(item.get("content") or "")
                        for item in messages
                        if item.get("role") == "user"
                    )
                )
            return super().__call__(messages, **kwargs)

    fake = _Fake({"agent:loop": script})
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"}
    )

    started = start_loop(ws)
    script.run_id = started["id"]
    run = wait_run(ws, started["id"], timeout=60)

    assert run["status"] == "completed"
    assert "Skip the second row." not in delivered[0]
    assert "Skip the second row." in delivered[-1]


def test_steer_delivers_to_the_loop_rather_than_queueing_a_command(
    monkeypatch, workspace_with_data
):
    """A live loop takes a message as steering; a workflow run queues it."""

    ws = workspace_with_data
    held = {"release": False}

    def waiting(script: LoopScript) -> dict:
        if script.calls == 1:
            while not held["release"]:
                pass
        return finish_turn("Done.")

    script = LoopScript(waiting)
    configured(monkeypatch, script)
    started = start_loop(ws)
    handle = None
    while handle is None:
        handle = runner.get_handle(started["id"])
    response = runner.steer(ws, started["id"], "Also check the treasury row.")
    held["release"] = True
    run = wait_run(ws, started["id"], timeout=60)

    assert response["handled"] == "steering"
    assert store.load_run(ws, started["id"])["pending_commands"] == []
    assert any(
        item["role"] == "user" and "treasury" in item["content"]
        for item in run["messages"]
    )


def test_cancelling_during_a_child_stops_both_and_keeps_committed_work(
    monkeypatch, workspace_with_data
):
    ws = workspace_with_data
    script = LoopScript(
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}),
        finish_turn("Never reached."),
    )
    state: dict = {}

    def apm(_user: str) -> dict:
        # Cancel while the child's own worker turn is in flight: the loop and
        # the child share one cancel event, so both must stop.
        runner.cancel_run(ws, state["run_id"])
        return APM_DRAFT

    configured(monkeypatch, script, {"agent:apm": apm})
    started = start_loop(ws)
    state["run_id"] = started["id"]
    run = wait_run(ws, started["id"], timeout=60)

    assert run["status"] == "cancelled"
    # The loop stopped where it was told to; it did not go on deciding.
    assert not any("Never reached" in str(item.get("content")) for item in run["messages"])
    child = store.load_run(ws, run["children"][0])
    assert child["status"] in store.TERMINAL_STATUSES
    # Work the child had already committed when the cancel arrived is kept.
    if child["status"] == "completed":
        assert ws.reload().planning.get("apm_markdown")


def test_a_child_handle_shares_stopping_and_owns_answering():
    """Cancel and pause are the request's; approvals and steering are the run's."""

    parent = runner.RunHandle("ws", "parent-run")
    child = runner.RunHandle.child_of(parent, "child-run")

    assert child.cancel is parent.cancel
    assert child.pause_requested is parent.pause_requested
    assert child.resume is parent.resume
    parent.cancel.set()
    assert child.cancel.is_set()

    assert child.inbox is not parent.inbox
    assert child.command_queue is not parent.command_queue
    assert child.decisions is not parent.decisions
    assert child.interaction_responses is not parent.interaction_responses
    assert child.interaction_resolved is not parent.interaction_resolved


# --------------------------------------------------------------------------- #
# The conversation sidecar
# --------------------------------------------------------------------------- #
def test_the_conversation_is_persisted_beside_the_run(monkeypatch, workspace_with_data):
    script = LoopScript(
        tool_turn("get_audit_progress", {}),
        finish_turn("Nothing to do."),
    )
    configured(monkeypatch, script)

    started = start_loop(workspace_with_data)
    run = wait_run(workspace_with_data, started["id"], timeout=60)

    path = store.run_dir(workspace_with_data, run["id"]) / "conversation.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    roles = [item["role"] for item in payload["messages"]]
    assert roles[0] == "user"
    assert "tool" in roles
    assert roles[-1] == "tool"


def test_a_long_conversation_drops_old_tool_bodies_and_keeps_decisions():
    messages = [{"role": "user", "content": "Do the work"}]
    for index in range(12):
        messages.append(
            {"role": "assistant", "content": f"decision {index}", "tool_calls": []}
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "content": json.dumps({"rows": "x" * 8_000}),
            }
        )

    bounded = agent_loop.bounded_conversation(messages)

    assert len(json.dumps(bounded)) <= agent_loop.MAX_CONVERSATION_CHARS
    assert [item["content"] for item in bounded if item["role"] == "assistant"] == [
        f"decision {index}" for index in range(12)
    ]
    kept = [item for item in bounded if item["role"] == "tool"]
    assert json.loads(kept[-1]["content"]).get("omitted") is None
    assert json.loads(kept[0]["content"])["omitted"] is True


def test_a_resumed_loop_continues_its_own_conversation(monkeypatch, workspace_with_data):
    ws = workspace_with_data
    script = LoopScript(finish_turn("Carried on where I left off."))
    configured(monkeypatch, script)

    run = store.new_command_run(
        ws, "auto", {"source": store.LOOP_COMMAND_SOURCE, "text": "Do the planning"}
    )
    routing.resolve_route(ws, run)
    run = store.load_run(ws, run["id"])
    run["status"] = "interrupted"
    # Something the auditor said while the loop was down, unread by it.
    run["messages"] = [
        {"role": "user", "content": "Leave the treasury row alone.", "at": store.utcnow()}
    ]
    store.save_run(ws, run)
    agent_loop.ConversationStore(ws, run["id"]).save(
        [
            {"role": "user", "content": "Do the planning"},
            {"role": "assistant", "content": "I read the workspace first."},
        ],
        delivered_messages=0,
    )

    runner.resume_run(ws, run["id"])
    finished = wait_run(ws, run["id"], timeout=60)

    assert finished["status"] == "completed"
    conversation = agent_loop.ConversationStore(ws, run["id"]).load()["messages"]
    assert conversation[1]["content"] == "I read the workspace first."
    assert any("resumed" in str(item.get("content")) for item in conversation)
    # The unread message is delivered rather than lost to the crash.
    assert any(
        "Leave the treasury row alone." in str(item.get("content"))
        for item in conversation
    )


# --------------------------------------------------------------------------- #
# The hand-off from the coordinator
# --------------------------------------------------------------------------- #
def test_the_coordinator_is_lent_take_action_and_seeds_the_loop(
    monkeypatch, workspace_with_data
):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    assistant_chats.send_message(
        ws,
        chat["id"],
        {
            "content": "What state is the RCM in?",
            "intent": "ask",
            "mode": "auto",
            "request_id": "request-loop-context",
            "source": "composer",
        },
    )

    script = LoopScript(finish_turn("Nothing needed doing."))
    coordinator_calls: list[dict] = []

    def coordinator(messages, tools=None, **kwargs):
        coordinator_calls.append({"messages": messages, "tools": tools})
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_take",
                    "type": "function",
                    "function": {
                        "name": "take_action",
                        "arguments": json.dumps(
                            {"brief": "Redraft test DT-1 and check the result."}
                        ),
                    },
                }
            ],
        }

    fake = FakeAgentLLM({"agent:loop": script})

    def chat_llm(messages, **kwargs):
        system = str(messages[0].get("content") or "")
        if system.startswith("[agent:"):
            return fake(messages, **kwargs)
        return coordinator(messages, **kwargs)

    monkeypatch.setattr(llm, "chat", chat_llm)
    monkeypatch.setattr(
        llm, "status", lambda: {"configured": True, "backend": "fake", "model": "fake"}
    )
    monkeypatch.setattr(
        llm, "agent_status", lambda: {"configured": True, "backend": "fake", "model": "fake"}
    )

    result = assistant_chats.send_message(
        ws,
        chat["id"],
        {
            "content": "Test DT-1 doesn't look right, redraft it.",
            "intent": "auto",
            "mode": "auto",
            "request_id": "request-loop-handoff",
            "source": "composer",
        },
    )

    outcome = result.get("outcome") or {}
    assert outcome["kind"] == "run_started"
    run = wait_run(ws, outcome["run_id"], timeout=60)
    assert run["engine"] == store.AGENT_ENGINE
    assert run["command"]["source"] == store.LOOP_COMMAND_SOURCE
    assert run["command"]["text"] == "Redraft test DT-1 and check the result."
    # The loop opens with the conversation that produced the brief.
    seed = run["context"]["conversation_seed"]
    assert any("What state is the RCM in?" in item["content"] for item in seed)
    conversation = agent_loop.ConversationStore(ws, run["id"]).load()["messages"]
    user_turns = [item for item in conversation if item["role"] == "user"]
    assert user_turns[-1]["content"] == "Redraft test DT-1 and check the result."
    assert user_turns[0]["content"] == "What state is the RCM in?"
    # The tool is only offered when a launcher is bound.
    names = [
        schema["function"]["name"]
        for schema in coordinator_calls[0]["tools"] or []
    ]
    assert "take_action" in names
    assert assistant._command_schemas(
        assistant.Commander(catalog=(), launch_command=lambda _: {}, launch_action=lambda _: {})
    )[-1]["function"]["name"] == "start_action"


def test_the_finished_loops_offers_come_before_readiness_suggestions(
    monkeypatch, workspace_with_data
):
    ws = workspace_with_data
    script = LoopScript(
        finish_turn(
            "Drafted the memorandum.",
            suggestions=[
                {"label": "Generate the matrix next", "requested_outcomes": ["planning.rcm_ready"]},
                {"label": "Tell me what changed", "message": "What changed in the APM?"},
                {"label": "Nonsense", "requested_outcomes": ["not.a.capability"]},
            ],
        )
    )
    configured(monkeypatch, script)
    chat = assistant_chats.create_chat(ws)
    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": store.LOOP_COMMAND_SOURCE,
            "text": "Draft the planning memorandum",
            "chat_id": chat["id"],
        },
    )
    run = wait_run(ws, started["id"], timeout=60)

    # An offer naming an unregistered outcome is dropped, not persisted.
    assert [item["label"] for item in run["suggestions"]] == [
        "Generate the matrix next",
        "Tell me what changed",
    ]
    record = assistant_chats.get_chat(ws, chat["id"])
    labels = [item["label"] for item in record["suggestions"]]
    assert labels[:2] == ["Generate the matrix next", "Tell me what changed"]
    assert len(labels) > 2  # readiness still supplies the rest
    offered = record["suggestions"][1]
    assert offered["message"] == "What changed in the APM?"
    assert record["suggestions"][0]["requested_outcomes"] == ["planning.rcm_ready"]


def test_a_run_that_ended_badly_is_offered_for_review_until_it_is_read(
    monkeypatch, workspace_with_data
):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    failed = store.new_command_run(
        ws, "auto", {"source": "chat", "text": "Draft the APM", "chat_id": chat["id"]}
    )
    failed["status"] = "completed_with_failures"
    failed["engine"] = store.WORKFLOW_ENGINE
    store.save_run(ws, failed)

    labels = [
        item["label"] for item in assistant_chats.get_chat(ws, chat["id"])["suggestions"]
    ]
    assert "Review this run with the agent" in labels

    # Once a loop has inspected it, the offer retires.
    failed["reviewed_by_run_id"] = "20260101-000000-abc123"
    store.save_run(ws, failed)
    labels = [
        item["label"] for item in assistant_chats.get_chat(ws, chat["id"])["suggestions"]
    ]
    assert "Review this run with the agent" not in labels


def test_a_loop_run_projects_its_children_into_the_chat(monkeypatch, workspace_with_data):
    ws = workspace_with_data
    script = LoopScript(
        tool_turn("run_outcomes", {"requested_outcomes": [APM_OUTCOME]}),
        finish_turn("Drafted."),
    )
    configured(monkeypatch, script)
    chat = assistant_chats.create_chat(ws)
    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": store.LOOP_COMMAND_SOURCE,
            "text": "Draft the planning memorandum",
            "chat_id": chat["id"],
        },
    )
    run = wait_run(ws, started["id"], timeout=60)

    record = assistant_chats.get_chat(ws, chat["id"])
    projections = {
        item["run_id"]: item
        for item in record["transcript"]
        if item.get("type") == "run"
    }
    assert projections[run["id"]]["children"] == run["children"]
    assert projections[run["id"]]["engine"] == store.AGENT_ENGINE
    assert run["children"][0] in projections
