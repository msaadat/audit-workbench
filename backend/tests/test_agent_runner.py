import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import app.agent as agent_package
from app import documents, llm, workspaces
from app.agent import (
    action_runner,
    doc_test_runner,
    document_analysis_runner,
    intake_runner,
    ledger,
    runner,
    store,
    workflow_runner,
)
from conftest import wait_run


def _start(ws, mode="auto", context=None):
    return runner.start_run(ws, mode, context or {})


def _artifact_kinds(run):
    return {a["kind"] for a in run["artifacts"]}


def _next_gate(ws, run_id, decided=frozenset(), timeout=15.0):
    """Wait until the run is parked on a pending approval not yet decided by
    this test (returning it) or has reached a terminal state (returning
    None). ``decided`` avoids re-submitting a batch the worker is still in
    the middle of applying."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        run = store.load_run(ws, run_id)
        if run["status"] in store.TERMINAL_STATUSES:
            return run, None
        pending = [
            a
            for a in run["approvals"]
            if a["status"] == "pending" and a["id"] not in decided
        ]
        if run["status"] == "awaiting_approval" and pending:
            return run, pending[0]
        _time.sleep(0.02)
    raise AssertionError(f"Run stuck in {run['status']} without a pending approval")


def _approve_all_gates(ws, run_id, action="approve", collect=None):
    """Drive a permission run to completion, deciding every batch the same
    way; optionally collect the approval kinds seen."""
    decided = set()
    while True:
        _run, approval = _next_gate(ws, run_id, decided)
        if approval is None:
            break
        if collect is not None:
            collect.add(approval["kind"])
        decided.add(approval["id"])
        runner.resolve_approval(ws, run_id, approval["id"], _decisions(approval, action))
    return wait_run(ws, run_id)


# ---------------------------------------------------------------- auto mode
def test_auto_run_populates_workspace(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data)
    done = wait_run(workspace_with_data, run["id"])

    assert done["status"] == "completed"
    assert done["error"] is None
    assert done["discovery"]["domain"] == "sales"
    assert {"join", "ruleset", "analysis", "tile"} <= _artifact_kinds(done)
    assert done["summary_markdown"].startswith("# Analyst Summary")
    assert done["findings"] and done["findings"][0]["severity"] == "medium"

    ws = workspaces.load_workspace(workspace_with_data.id)
    # join created with provenance
    assert ws.joins and ws.joins[0]["created_by"] == "agent"
    assert ws.joins[0]["agent_run_id"] == done["id"]
    # one ruleset per base table, each executed once
    assert {r["table"] for r in ws.rulesets} == {"transactions", "customers"}
    assert all(r.get("runs") for r in ws.rulesets)
    # analyses: library duplicates + custom python
    kinds = {(a["kind"], a["source"]) for a in ws.analyses}
    assert ("analytics", "library") in kinds and ("python", "ai") in kinds
    # tiles pinned (validation tiles + query chart at minimum)
    assert any(t["kind"] == "validation" for t in ws.tiles)
    assert any(t["kind"] == "query" for t in ws.tiles)
    # duplicates test on invoice_no fails → notable → analytics tile pinned
    assert any(t["kind"] == "analytics" for t in ws.tiles)


def test_auto_run_emits_replayable_events(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data)
    wait_run(workspace_with_data, run["id"])
    events = store.read_events(workspace_with_data, run["id"])
    types = [e["type"] for e in events]
    assert "run_status" in types
    assert "plan_update" in types
    assert "task_update" in types
    assert "workspace_changed" in types
    assert "summary_ready" in types
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


def test_all_tasks_terminal_after_completion(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data)
    done = wait_run(workspace_with_data, run["id"])
    tasks = [t for s in done["plan"]["stages"] for t in s["tasks"]]
    assert tasks
    assert all(t["status"] in ("completed", "skipped", "failed") for t in tasks)
    assert all(
        t["status"] != "failed" for t in tasks
    ), [t for t in tasks if t["status"] == "failed"]


def test_run_requires_tables(fake_agent_llm):
    ws = workspaces.create_workspace("Empty")
    with pytest.raises(workspaces.WorkspaceError, match="at least one"):
        _start(ws)


def test_run_requires_configured_llm(workspace_with_data, monkeypatch):
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})
    with pytest.raises(llm.LLMError, match="not configured"):
        _start(workspace_with_data)


def test_model_gateway_charges_budgets_and_persists_hash_only_provenance(
    workspace_with_data, monkeypatch
):
    calls = []

    def fake_chat(messages, **_kwargs):
        calls.append(messages)
        return {
            "content": "SENSITIVE_PROVIDER_RESPONSE",
            "usage": {"prompt_tokens": 17, "completion_tokens": 5},
        }

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "provider": "accounting-test", "model": "model"},
    )
    run = store.new_run(workspace_with_data, "auto")
    run["limits"] = {
        "max_model_turns": 1,
        "max_estimated_prompt_tokens": 1_000,
        "max_completion_tokens": 100,
        "max_llm_concurrency": 1,
    }
    store.save_run(workspace_with_data, run)
    command = runner._Runner(
        workspace_with_data, run, runner.RunHandle(workspace_with_data.id, run["id"])
    )
    system = "[agent:accounting_test]\nSENSITIVE_SYSTEM_PROMPT"
    user = "SENSITIVE_USER_PAYLOAD"

    assert command._llm_content(system, user) == "SENSITIVE_PROVIDER_RESPONSE"

    usage = store.load_run(workspace_with_data, run["id"])["usage"]
    assert usage["llm_turns"] == 1
    assert usage["request_characters"] == len(system) + len(user)
    assert usage["prompt_tokens"] == 17
    assert usage["completion_tokens"] == 5
    assert usage["model_calls_by_worker"] == {"agent:accounting_test": 1}
    assert usage["model_call_metrics"][0]["estimated_input_tokens"] == max(
        1, (len(system) + len(user)) // 4
    )
    activity = documents.activities(workspace_with_data)["items"][-1]
    serialized = json.dumps(activity)
    assert activity["prompt_version"]
    assert activity["response_hash"]
    assert "SENSITIVE_SYSTEM_PROMPT" not in serialized
    assert "SENSITIVE_USER_PAYLOAD" not in serialized
    assert "SENSITIVE_PROVIDER_RESPONSE" not in serialized

    with pytest.raises(runner.LimitExceeded, match="model turn limit"):
        command._llm_content(system, user)
    assert len(calls) == 1


def test_provider_concurrency_is_shared_and_bounded_across_runs(
    workspace_with_data, monkeypatch
):
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_chat(_messages, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return {"content": "ok", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        finally:
            with lock:
                active -= 1

    monkeypatch.setenv("AGENT_PROVIDER_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {
            "configured": True,
            "provider": "shared-concurrency-characterization",
            "model": "one-at-a-time",
        },
    )
    commands = []
    for index in range(2):
        run = store.new_run(
            workspace_with_data, "auto", {"objective": f"concurrency {index}"}
        )
        run["limits"] = {
            "max_model_turns": 2,
            "max_estimated_prompt_tokens": 100,
            "max_completion_tokens": 10,
            "max_llm_concurrency": 2,
        }
        store.save_run(workspace_with_data, run)
        commands.append(
            runner._Runner(
                workspace_with_data,
                run,
                runner.RunHandle(workspace_with_data.id, run["id"]),
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda command: command._llm_content(
                    "[agent:concurrency_test]", "bounded request"
                ),
                commands,
            )
        )

    assert results == ["ok", "ok"]
    assert peak == 1


def test_concurrent_run_rejected(workspace_with_data, fake_agent_llm):
    import time as _time

    def slow_planning(_user):
        _time.sleep(0.8)  # hold the run in planning while we try a second one
        return json.loads(json.dumps(fake_agent_llm.DEFAULTS["agent:planning"]))

    fake_agent_llm.overrides["agent:planning"] = slow_planning
    run = _start(workspace_with_data)
    with pytest.raises(runner.AgentBusyError, match="already active"):
        _start(workspace_with_data)
    wait_run(workspace_with_data, run["id"])


# ---------------------------------------------------------- permission mode
def _decisions(approval, action="approve"):
    return [{"item_id": i["id"], "action": action} for i in approval["items"]]


def test_permission_run_gates_mutations(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data, mode="permission")

    # First gate: the join proposal.
    _state, approval = _next_gate(workspace_with_data, run["id"])
    assert approval is not None and approval["kind"] == "join"
    assert approval["items"][0]["spec"]["left"] in ("transactions", "customers")
    # Nothing mutated yet.
    ws = workspaces.load_workspace(workspace_with_data.id)
    assert not ws.joins and not ws.rulesets and not ws.analyses and not ws.tiles

    seen_kinds = set()
    done = _approve_all_gates(ws, run["id"], collect=seen_kinds)
    assert done["status"] == "completed"
    assert {"join", "rules", "tests"} <= seen_kinds

    ws = workspaces.load_workspace(workspace_with_data.id)
    assert ws.joins and ws.rulesets and ws.analyses and ws.tiles


def test_permission_rejection_blocks_mutation(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data, mode="permission")
    done = _approve_all_gates(workspace_with_data, run["id"], action="reject")
    assert done["status"] == "completed"
    ws = workspaces.load_workspace(workspace_with_data.id)
    assert not ws.joins
    assert not ws.rulesets
    assert not ws.analyses


def test_permission_edit_applies_edited_spec(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data, mode="permission")
    edited_min = 5.0
    saw_rules_edit = False
    decided = set()
    while True:
        _state, approval = _next_gate(workspace_with_data, run["id"], decided)
        if approval is None:
            break
        decided.add(approval["id"])
        decisions = []
        for item in approval["items"]:
            if (
                approval["kind"] == "rules"
                and not saw_rules_edit
                and item["spec"].get("check") == "range"
            ):
                saw_rules_edit = True
                edited = dict(item["spec"], params={"min": edited_min, "max": 100000})
                decisions.append(
                    {"item_id": item["id"], "action": "edit", "spec": edited}
                )
            else:
                decisions.append({"item_id": item["id"], "action": "approve"})
        runner.resolve_approval(
            workspace_with_data, run["id"], approval["id"], decisions
        )
    done = wait_run(workspace_with_data, run["id"])
    assert done["status"] == "completed"
    assert saw_rules_edit
    ws = workspaces.load_workspace(workspace_with_data.id)
    tx_rules = next(r for r in ws.rulesets if r["table"] == "transactions")["rules"]
    range_rule = next(r for r in tx_rules if r["check"] == "range")
    assert float(range_rule["params"]["min"]) == edited_min


# ------------------------------------------------------------------ control
def test_cancel_during_approval(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data, mode="permission")
    _next_gate(workspace_with_data, run["id"])
    runner.cancel_run(workspace_with_data, run["id"])
    done = wait_run(workspace_with_data, run["id"])
    assert done["status"] == "cancelled"
    assert done["finished"]


def test_steering_message_recorded_and_fed_to_prompts(
    workspace_with_data, fake_agent_llm
):
    run = _start(workspace_with_data, mode="permission")
    _next_gate(workspace_with_data, run["id"])
    runner.steer(workspace_with_data, run["id"], "Focus on weekend postings")
    done = _approve_all_gates(workspace_with_data, run["id"])
    assert any(
        m["role"] == "user" and "weekend" in m["content"] for m in done["messages"]
    )
    later_prompts = [
        c for c in fake_agent_llm.calls if c["tag"] in ("agent:analyses", "agent:summary")
    ]
    assert any(
        "Focus on weekend postings" in c["messages"][1]["content"]
        for c in later_prompts
    )


def test_follow_up_run_after_completion(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data)
    wait_run(workspace_with_data, run["id"])
    result = runner.steer(workspace_with_data, run["id"], "Also check round numbers")
    assert result["handled"] == "follow_up_run"
    follow = result["run"]
    assert follow["parent_run_id"] == run["id"]
    assert "round numbers" in follow["context"]["objective"]
    wait_run(workspace_with_data, follow["id"])


# -------------------------------------------------------------- degradation
def test_llm_failure_mid_run_yields_partial_summary(
    workspace_with_data, fake_agent_llm
):
    def boom(_user):
        raise llm.LLMError("backend melted")

    fake_agent_llm.overrides["agent:analyses"] = boom
    fake_agent_llm.overrides["agent:dashboard"] = boom
    fake_agent_llm.overrides["agent:summary"] = boom
    run = _start(workspace_with_data)
    done = wait_run(workspace_with_data, run["id"])
    assert done["status"] == "completed"
    assert done["summary_markdown"]  # deterministic fallback
    assert any("melted" in w or "skipped" in w for w in done["warnings"])
    ws = workspaces.load_workspace(workspace_with_data.id)
    assert ws.rulesets  # earlier stages' work is retained


def test_invalid_llm_json_retried_then_degraded(workspace_with_data, fake_agent_llm):
    attempts = {"n": 0}

    def flaky(_user):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return "not json at all"  # str → json.dumps'd, unparseable object
        return fake_agent_llm.DEFAULTS["agent:planning"]

    fake_agent_llm.overrides["agent:planning"] = flaky
    run = _start(workspace_with_data)
    done = wait_run(workspace_with_data, run["id"])
    assert attempts["n"] == 2
    assert done["discovery"]["domain"] == "sales"


def test_structurally_invalid_rule_params_are_repaired_before_use(
    workspace_with_data, fake_agent_llm
):
    attempts = {"n": 0}

    def malformed_then_valid(_user):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return {
                "rules": [{
                    "column": "amount", "check": "range",
                    "params": "minimum 0 and maximum 100000",
                    "severity": "warn", "rationale": "Plausibility range.",
                }]
            }
        return fake_agent_llm.DEFAULTS["agent:rules"]

    fake_agent_llm.overrides["agent:rules"] = malformed_then_valid
    done = wait_run(workspace_with_data, _start(workspace_with_data)["id"])

    assert done["status"] == "completed"
    assert attempts["n"] >= 3  # first table repaired; second table also receives advice
    assert all(
        isinstance(rule["params"], dict)
        for ruleset in workspaces.load_workspace(workspace_with_data.id).rulesets
        for rule in ruleset["rules"]
    )


@pytest.mark.parametrize(
    ("validator", "payload", "message"),
    [
        (runner._validate_analyses_payload, {"library": [{"table": "transactions", "test": "duplicates", "title": "Duplicates", "rationale": "Audit risk", "params": "all rows"}], "custom": []}, "params must be an object"),
        (runner._validate_dashboard_payload, {"queries": [{"table": "transactions", "title": "Chart", "rationale": "Trend", "spec": "group it", "viz": {}}]}, "spec must be an object"),
        (runner._validate_summary_payload, {"findings": [{"evidence_refs": "analysis:x"}], "summary_markdown": "# Summary"}, "array of strings"),
    ],
)
def test_stage_structured_output_validators_reject_nested_shape_errors(
    validator, payload, message
):
    with pytest.raises(ValueError, match=message):
        validator(payload)


def test_preflight_drops_broken_rules(workspace_with_data, fake_agent_llm):
    fake_agent_llm.overrides["agent:rules"] = {
        "rules": [
            {
                "column": "no_such_column",
                "check": "required",
                "params": {},
                "severity": "fail",
                "rationale": "hallucinated",
            }
        ]
    }
    run = _start(workspace_with_data)
    done = wait_run(workspace_with_data, run["id"])
    assert done["status"] == "completed"
    ws = workspaces.load_workspace(workspace_with_data.id)
    for ruleset in ws.rulesets:
        assert all(r["column"] != "no_such_column" for r in ruleset["rules"])
    assert any("no_such_column" in w for w in done["warnings"])


def test_broken_custom_code_gets_one_repair(workspace_with_data, fake_agent_llm):
    fake_agent_llm.overrides["agent:analyses"] = {
        "library": [],
        "custom": [
            {
                "table": "transactions",
                "title": "Broken then fixed",
                "code": "result = transactions.group_by('nope').agg(pl.len())",
                "rationale": "Exercise the custom-code repair path.",
            }
        ],
    }
    fake_agent_llm.overrides["agent:fix_code"] = {
        "code": "result = transactions.group_by('cust_id').agg(pl.len())"
    }
    run = _start(workspace_with_data)
    done = wait_run(workspace_with_data, run["id"])
    assert done["status"] == "completed"
    ws = workspaces.load_workspace(workspace_with_data.id)
    fixed = next(a for a in ws.analyses if a["title"] == "Broken then fixed")
    assert "cust_id" in fixed["spec"]["code"]


# ----------------------------------------------------------- reconciliation
def test_rerun_reconciles_instead_of_duplicating(workspace_with_data, fake_agent_llm):
    first = _start(workspace_with_data)
    wait_run(workspace_with_data, first["id"])
    ws = workspaces.load_workspace(workspace_with_data.id)
    counts = (len(ws.joins), len(ws.rulesets), len(ws.analyses), len(ws.tiles))

    second = _start(workspaces.load_workspace(workspace_with_data.id))
    done = wait_run(workspace_with_data, second["id"])
    assert done["status"] == "completed"
    ws = workspaces.load_workspace(workspace_with_data.id)
    assert (len(ws.joins), len(ws.rulesets), len(ws.analyses), len(ws.tiles)) == counts


def test_rerun_leaves_user_edited_items_alone(workspace_with_data, fake_agent_llm):
    first = _start(workspace_with_data)
    wait_run(workspace_with_data, first["id"])
    ws = workspaces.load_workspace(workspace_with_data.id)
    ruleset = next(r for r in ws.rulesets if r["table"] == "transactions")
    user_rules = [
        {"column": "amount", "check": "required", "params": {}, "severity": "fail"}
    ]
    ws.update_ruleset(ruleset["id"], {"rules": user_rules})  # user edit → user-owned

    second = _start(workspaces.load_workspace(workspace_with_data.id))
    done = wait_run(workspace_with_data, second["id"])
    assert done["status"] == "completed"
    ws = workspaces.load_workspace(workspace_with_data.id)
    edited = next(r for r in ws.rulesets if r["id"] == ruleset["id"])
    assert edited["created_by"] == "user"
    assert len(edited["rules"]) == 1  # agent did not touch it
    assert any("user-edited" in w for w in done["warnings"])


# ------------------------------------------------ explicit engine dispatch
def test_action_runner_has_no_legacy_module_or_class_alias():
    assert importlib.util.find_spec("app.agent.command_runner") is None
    assert not hasattr(action_runner, "CommandRunner")
    assert not hasattr(agent_package, "CommandRunner")


def test_phase_one_has_no_v2_reader_alias_or_compatibility_module(
    workspace_with_data,
):
    package_dir = Path(action_runner.__file__).parent
    forbidden_paths = {
        path
        for path in package_dir.iterdir()
        if path.name == "command_runner.py" or "compat" in path.name.casefold()
    }
    assert not forbidden_paths

    run = store.new_command_run(
        workspace_with_data,
        "auto",
        {"source": "chat", "text": "run a bounded action"},
    )
    run.pop("engine")
    store.save_run(workspace_with_data, run)

    loaded = store.load_run(workspace_with_data, run["id"])
    assert "engine" not in loaded
    assert store.run_summary(loaded)["engine"] is None


@pytest.mark.parametrize(
    ("engine", "module", "class_name", "expected"),
    [
        (store.WORKFLOW_ENGINE, workflow_runner, "WorkflowRunner", "workflow"),
        (store.ACTION_ENGINE, action_runner, "ActionRunner", "action"),
        (store.INTAKE_ENGINE, intake_runner, "IntakeRunner", "intake"),
        (store.DOC_TEST_ENGINE, doc_test_runner, "DocTestRunner", "doc_test"),
        (
            store.DOCUMENT_ANALYSIS_ENGINE,
            document_analysis_runner,
            "DocumentAnalysisRunner",
            "document_analysis",
        ),
    ],
)
def test_execute_dispatches_by_explicit_engine_only(
    workspace_with_data, monkeypatch, engine, module, class_name, expected
):
    called = []

    class StubRunner:
        def __init__(self, _workspace, _run, _handle):
            pass

        def execute(self):
            called.append(expected)

    run = store.new_run(workspace_with_data, "auto")
    run["engine"] = engine
    run["kind"] = "deliberately-not-a-dispatch-key"
    store.save_run(workspace_with_data, run)
    monkeypatch.setattr(module, class_name, StubRunner)

    runner._execute(
        workspace_with_data.id,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    assert called == [expected]


def test_execute_dispatches_legacy_analysis_by_explicit_engine_only(
    workspace_with_data, monkeypatch
):
    called = []

    class StubRunner:
        def __init__(self, _workspace, _run, _handle):
            pass

        def execute(self):
            called.append("analysis")

    run = store.new_run(workspace_with_data, "auto")
    run["kind"] = "deliberately-not-a-dispatch-key"
    store.save_run(workspace_with_data, run)
    monkeypatch.setattr(runner, "_Runner", StubRunner)

    runner._execute(
        workspace_with_data.id,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    assert called == ["analysis"]


@pytest.mark.parametrize(("engine", "label"), [(None, "missing"), ("v2", "unsupported")])
def test_execute_fails_closed_without_a_supported_explicit_engine(
    workspace_with_data, engine, label
):
    run = store.new_run(workspace_with_data, "auto", kind="intake")
    if engine is None:
        run.pop("engine")
    else:
        run["engine"] = engine
    store.save_run(workspace_with_data, run)

    runner._execute(
        workspace_with_data.id,
        run["id"],
        runner.RunHandle(workspace_with_data.id, run["id"]),
    )

    failed = store.load_run(workspace_with_data, run["id"])
    assert failed["status"] == "failed"
    assert "engine" in failed["error"]
    assert label in failed["error"]


# -------------------------------------------------------- interruption
def test_same_schema_orphan_recovery_preserves_durable_checkpoints(workspace_with_data):
    cases = {}

    queued = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "queued command"}
    )
    cases[queued["id"]] = {"status": "queued", "checkpoint": queued["command"]}

    partial = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "partially committed command"}
    )
    committed, in_flight = ledger.append_actions(
        partial,
        [
            {"id": "committed", "type": "run_report_quality", "args": {}},
            {
                "id": "in-flight",
                "type": "run_analytics",
                "args": {
                    "table": "transactions",
                    "test": "duplicates",
                    "params": {"columns": ["invoice_no"]},
                },
                "depends_on": ["committed"],
            },
        ],
    )
    ledger.transition(committed, "ready")
    ledger.transition(committed, "running")
    ledger.transition(committed, "succeeded")
    ledger.transition(in_flight, "ready")
    ledger.transition(in_flight, "running")
    partial["status"] = "executing"
    store.save_run(workspace_with_data, partial)
    cases[partial["id"]] = {
        "status": "executing",
        "checkpoint": [action["status"] for action in partial["actions"]],
    }

    approval = store.new_command_run(
        workspace_with_data, "permission", {"source": "chat", "text": "approval command"}
    )
    approval["status"] = "awaiting_approval"
    approval["approvals"] = [
        {"id": "approval-1", "kind": "mutations", "status": "pending", "items": []}
    ]
    store.save_run(workspace_with_data, approval)
    cases[approval["id"]] = {
        "status": "awaiting_approval",
        "checkpoint": approval["approvals"],
    }

    interaction = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "interaction command"}
    )
    interaction["status"] = "awaiting_input"
    interaction["interactions"] = [
        {
            "id": "interaction-1",
            "action_id": "missing-target",
            "type": "clarification",
            "status": "pending",
            "prompt": "Which artifact?",
        }
    ]
    store.save_run(workspace_with_data, interaction)
    cases[interaction["id"]] = {
        "status": "awaiting_input",
        "checkpoint": interaction["interactions"],
    }

    provider = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "provider command"}
    )
    provider["status"] = "interpreting"
    provider["usage"].update(llm_turns=1, estimated_prompt_tokens=321)
    store.save_run(workspace_with_data, provider)
    cases[provider["id"]] = {
        "status": "interpreting",
        "checkpoint": provider["usage"],
    }

    completed = store.new_command_run(
        workspace_with_data, "auto", {"source": "chat", "text": "completed command"}
    )
    completed.update(status="completed", finished=store.utcnow(), summary_markdown="Done")
    store.save_run(workspace_with_data, completed)

    recovered = runner.recover_workspace(workspace_with_data)

    assert set(recovered) == set(cases)
    for run_id, expected in cases.items():
        durable = store.load_run(workspace_with_data, run_id)
        assert durable["status"] == "interrupted"
        if expected["status"] == "queued":
            checkpoint = durable["command"]
        elif expected["status"] == "executing":
            checkpoint = [action["status"] for action in durable["actions"]]
        elif expected["status"] == "awaiting_approval":
            checkpoint = durable["approvals"]
        elif expected["status"] == "awaiting_input":
            checkpoint = durable["interactions"]
        else:
            checkpoint = durable["usage"]
        assert checkpoint == expected["checkpoint"]
        assert "prepared_planning" not in durable
        assert store.read_events(workspace_with_data, run_id)[-1] == {
            "seq": 1,
            "at": store.read_events(workspace_with_data, run_id)[-1]["at"],
            "type": "run_status",
            "data": {"status": "interrupted"},
        }

    durable_completed = store.load_run(workspace_with_data, completed["id"])
    assert durable_completed["status"] == "completed"
    assert durable_completed["summary_markdown"] == "Done"
    assert store.read_events(workspace_with_data, completed["id"]) == []


def test_interrupted_run_can_resume_to_completion(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data, mode="permission")
    _next_gate(workspace_with_data, run["id"])
    # Simulate a process crash: kill the worker, then rewrite the active
    # status a real crash would have left behind.
    handle = runner.get_handle(run["id"])
    handle.cancel.set()
    handle.thread.join(timeout=5)
    state = store.load_run(workspace_with_data, run["id"])
    state["status"] = "awaiting_approval"
    state["finished"] = None
    store.save_run(workspace_with_data, state)
    assert runner.recover_workspace(workspace_with_data) == [run["id"]]
    assert store.load_run(workspace_with_data, run["id"])["status"] == "interrupted"

    runner.resume_run(workspace_with_data, run["id"])
    resumed, approval = _next_gate(workspace_with_data, run["id"])
    assert approval is not None
    # The pending approval survived and is re-served, not re-proposed.
    assert len([a for a in resumed["approvals"] if a["status"] == "pending"]) == 1
    done = _approve_all_gates(workspace_with_data, run["id"])
    assert done["status"] == "completed"


# ----------------------------------------------------------- model context
def test_unmasked_profile_values_reach_the_model(workspace_with_data, fake_agent_llm):
    run = _start(workspace_with_data)
    wait_run(workspace_with_data, run["id"])
    outbound = "\n".join(
        message["content"]
        for call in fake_agent_llm.calls
        for message in call["messages"]
    )
    assert "C1" in outbound
    assert "sensitive_identifier" not in outbound
