"""Phase 8 gate: the exploratory data-analysis workflow.

These tests prove the whole declared workflow — deterministic relationship
inference, evidence-gated join materialization, model-proposed but locally
validated analysis definitions, and local execution that persists only a bounded
result contract — runs on the same domain-neutral scheduler as the audit
workflow, with different declared outcome sets.
"""

from __future__ import annotations

import ast
import inspect
import json
import re

import polars as pl
import pytest

from app import analysis_results, analytics, dashboard, llm, workspaces
from app.agent import narration, runner, store, workflow
from app.agent import capabilities as capability_registries
from app.agent import joins as join_diagnostics
from app.agent import probes
from app.agent.analysis_execution import build_analysis_workflow_runner
from app.agent.audit_execution import build_audit_workflow_runner
from app.agent.capabilities import analysis as analysis_capabilities
from app.agent.context import (
    ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS,
    PRESETS,
    ContextResolver,
    analysis_definition_scope,
    analysis_summary_scope,
)
from app.agent.context import adapters
from app.agent.executors import ExecutorRequest
from app.agent.executors import analysis as analysis_executors
from app.agent.routing import classify_command, resolve_route
from app.agent.workers import analysis as analysis_worker
from app.agent.workers.model import WorkerRequest, WorkerResponseValidationError
from app.agent.workflow_dispatch import build_workflow_runner
from app.agent.workflows import analysis as analysis_workflow
from app.agent.workflows import audit as audit_workflow
from app.workspace_transactions import parent_hashes
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import FakeAgentLLM, wait_run


ANALYSIS_TAG = "agent:analysis_definitions"
SUMMARY_TAG = "agent:analysis_summary"


def _scripted_summary(user: str) -> dict:
    """A memo that cites whichever procedures the bundle actually supplied.

    Citing a real supplied id matters: the worker's validator rejects an embed
    naming a procedure it was not shown, so a script with a hardcoded id would
    pass or fail for reasons unrelated to what is being tested.

    Returned as a ``content`` message rather than a bare value because this
    worker answers in Markdown; ``FakeAgentLLM`` JSON-encodes anything else,
    which would deliver the memo as one quoted line.
    """
    payload = json.loads(user.split("\n\nThe previous summary failed")[0])
    supplied = [
        item["content"]["analysis_id"]
        for item in payload["RESOLVED CONTEXT"]["items"]
        if item["source_id"] == "analysis_results"
    ]
    embed = (
        "\n```embed\nanalysis: %s\nas: summary_table\ncaption: what it showed\n```\n"
        % supplied[0]
        if supplied
        else ""
    )
    return {
        "content": (
            "The population is complete and the duplicate keys are the only issue.\n"
            "\n## Data received and its limitations\n"
            f"{len(supplied)} procedure(s) were run over the imported data.\n"
            f"{embed}"
            "\n## What the analysis found\nNone that require escalation.\n"
            "\n## How far these results can be relied on\nKeys are largely complete.\n"
            "\n## Further work required\nNothing outstanding.\n"
        )
    }


def _scripted_definitions(user: str) -> dict:
    """A model script that answers for whichever frame it was actually given."""

    payload = json.loads(user.split("\n\nYour previous response")[0])
    frame = payload["TARGET FRAME"]
    table = frame["table"]
    columns = [column["name"] for column in frame["columns"]]
    key = "invoice_no" if "invoice_no" in columns else columns[0]
    return {
        "analyses": [
            {
                "title": f"Duplicates in {table}",
                "kind": "analytics",
                "spec": {"test": "duplicates", "params": {"columns": [key]}},
                "note": "Reused keys signal double postings.",
            },
            {
                "title": f"Preview {table}",
                "kind": "python",
                "spec": {"code": f"result = tables['{table}'].head(3)"},
                "note": "Expected to hold across these columns.",
            },
        ]
    }


def _fake_model(monkeypatch, script=None) -> FakeAgentLLM:
    fake = FakeAgentLLM(
        {ANALYSIS_TAG: script or _scripted_definitions, SUMMARY_TAG: _scripted_summary}
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "backend": "fake", "model": "fake"},
    )
    return fake


def _analysis_run(workspace, *, mode="auto", text=None, command=None) -> dict:
    run = store.new_command_run(
        workspace,
        mode,
        command
        or {
            "source": "chat",
            "text": text or "See the two tables, perform relevant joins and data analysis",
        },
    )
    assert resolve_route(workspace, run) == "workflow"
    return store.load_run(workspace, run["id"])


def _stage(run: dict, capability_id: str) -> dict:
    return next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == capability_id
    )


def _drive(workspace, run: dict, capability_id: str):
    """Run one stage through the scheduler exactly as ``execute()`` would."""

    scheduler = build_analysis_workflow_runner(
        workspace, run, runner.RunHandle(workspace.id, run["id"])
    )
    scheduler._refresh()
    stage = _stage(run, capability_id)
    if scheduler.before_stage is not None:
        # The execute loop crosses this boundary before every stage, and some
        # of what a stage owes the reader is said there rather than per unit.
        scheduler.before_stage(
            scheduler.subject, scheduler.registry.get(capability_id), stage
        )
        scheduler._refresh()
    scheduler._run_stage(stage)
    return scheduler


def _moderate_pair_workspace() -> workspaces.Workspace:
    """Two tables whose only relationship is good but not strong.

    ``orders`` matches 9 of its 10 keys into ``regions`` (a 90% match rate, so
    good but not strong), and the reverse direction is unusable because the
    orders key repeats, which would multiply rows.
    """

    ws = workspaces.create_workspace("Moderate evidence")
    orders = pl.DataFrame(
        {
            "order_ref": [f"R{index}" for index in range(1, 10)] + ["R1"],
            "amount": [float(index) for index in range(1, 11)],
        }
    )
    dimension = pl.DataFrame(
        {
            "order_ref": [f"R{index}" for index in range(1, 9)] + ["R11"],
            "region": list("abcdefghi"),
        }
    )
    ws.add_table("orders.csv", orders.write_csv().encode())
    ws.add_table("regions.csv", dimension.write_csv().encode())
    return ws


# --------------------------------------------------------------------------- #
# P8.2 — workflow definition and grouped composition
# --------------------------------------------------------------------------- #
def test_analysis_graph_declares_a_linear_closure_with_a_stable_hash():
    assert analysis_workflow.DEPENDENCIES == {
        "data.relationships_inferred": (),
        "data.join_utility_ready": ("data.relationships_inferred",),
        "data.joins_ready": ("data.join_utility_ready",),
        "analysis.definitions_ready": ("data.joins_ready",),
        "analysis.executed": ("analysis.definitions_ready",),
        "analysis.summarized": ("analysis.executed",),
    }
    assert analysis_workflow.FULL_ANALYSIS_OUTCOMES == ["analysis.summarized"]
    assert analysis_workflow.outcomes_for_template("data_analysis") == [
        "analysis.summarized"
    ]
    # "Bring the saved analyses up to date" stays a zero-model-turn request:
    # summarising is a model turn, and this template exists not to spend one.
    assert analysis_workflow.outcomes_for_template("analysis_execution") == [
        "analysis.executed"
    ]

    registry = capability_registries.build_analysis_registry()
    assert registry.closure(["analysis.summarized"]) == [
        "data.relationships_inferred",
        "data.join_utility_ready",
        "data.joins_ready",
        "analysis.definitions_ready",
        "analysis.executed",
        "analysis.summarized",
    ]

    baseline = analysis_workflow.definition_hash()
    assert baseline == analysis_workflow.definition_hash()
    assert baseline != audit_workflow.definition_hash()

    original = dict(analysis_workflow.DEPENDENCIES)
    try:
        analysis_workflow.DEPENDENCIES["analysis.executed"] = (
            "analysis.definitions_ready",
            "data.joins_ready",
        )
        assert analysis_workflow.definition_hash() != baseline
    finally:
        analysis_workflow.DEPENDENCIES.clear()
        analysis_workflow.DEPENDENCIES.update(original)


def test_grouped_analysis_composition_is_validated_at_startup():
    assert capability_registries.grouped_analysis_capability_ids() == tuple(
        analysis_workflow.DEPENDENCIES
    )
    live = {
        capability.id: capability
        for capability in capability_registries.ANALYSIS_REGISTRY.all()
    }
    assert set(live) == set(analysis_workflow.DEPENDENCIES)
    assert live["analysis.definitions_ready"].context == "analysis.definitions"
    # The three deterministic capabilities declare no context because no model
    # ever sees their inputs.
    assert [
        capability.id for capability in live.values() if capability.context is None
    ] == [
        "data.relationships_inferred",
        "data.joins_ready",
        "analysis.executed",
    ]

    rebuilt = capability_registries.build_analysis_registry()
    assert (
        capability_registries.validate_analysis_composition(rebuilt) is rebuilt
    )
    partial = workflow.CapabilityRegistry()
    for capability in rebuilt.all():
        if capability.id != "analysis.executed":
            partial.register(capability)
    with pytest.raises(
        capability_registries.AuditCompositionError, match="analysis.executed"
    ):
        capability_registries.validate_analysis_composition(partial)


def test_the_two_workflows_are_separately_addressable():
    assert (
        capability_registries.workflow_for_outcomes(["analysis.executed"])
        == analysis_workflow.WORKFLOW_ID
    )
    assert (
        capability_registries.workflow_for_outcomes(["planning.apm_ready"])
        == audit_workflow.WORKFLOW_ID
    )
    # The audit composition deliberately owns this combined scope so a full
    # audit can schedule analysis before APM without making APM depend on it.
    assert (
        capability_registries.workflow_for_outcomes(
            ["planning.apm_ready", "analysis.executed"]
        )
        == audit_workflow.WORKFLOW_ID
    )


# --------------------------------------------------------------------------- #
# P8.3 — table scope resolution
# --------------------------------------------------------------------------- #
def test_explicit_targets_and_selected_artifacts_resolve_the_same_scope(
    workspace_with_data,
):
    ws = workspace_with_data
    explicit = analysis_capabilities.resolve_table_scope(
        ws, {"tables": ["transactions"]}
    )
    assert explicit.tables == ("transactions",)
    assert explicit.explicit is True
    assert explicit.ambiguity is None

    ws.add_join(
        {
            "name": "tx_customers",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    from_join = analysis_capabilities.resolve_table_scope(
        ws, {"target_refs": ["join:tx_customers"]}
    )
    assert set(from_join.tables) == {"transactions", "customers"}
    assert from_join.joins == ("tx_customers",)
    assert set(from_join.targets) == {"transactions", "customers", "tx_customers"}

    saved = ws.add_analysis(
        {
            "title": "Spend",
            "kind": "analytics",
            "table": "transactions",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
            "note": "Expected to hold across these columns.",
        }
    )
    from_analysis = analysis_capabilities.resolve_table_scope(
        ws, {"target_refs": [f"analysis:{saved['id']}"]}
    )
    assert from_analysis.tables == ("transactions",)


def test_unscoped_request_is_bounded_and_reports_its_own_ambiguity():
    ws = workspaces.create_workspace("Many tables")
    for index in range(analysis_capabilities.MAX_SCOPE_TABLES + 2):
        frame = pl.DataFrame({"id": [1, 2, 3], "value": [index, index, index]})
        ws.add_table(f"table_{index}.csv", frame.write_csv().encode())

    scope = analysis_capabilities.resolve_table_scope(ws, {})
    assert len(scope.tables) == analysis_capabilities.MAX_SCOPE_TABLES
    assert scope.tables == tuple(sorted(scope.tables))
    assert scope.ambiguity is not None
    assert "Name the tables to analyse instead." in scope.ambiguity
    # Naming the tables removes the ambiguity entirely.
    named = analysis_capabilities.resolve_table_scope(
        ws, {"tables": ["table_0", "table_1"]}
    )
    assert named.ambiguity is None


def test_a_lookup_table_costs_no_definition_turn():
    """One turn is taken per frame, so a reference table must not claim one."""
    ws = workspaces.create_workspace("Lookup and ledger")
    ledger = pl.DataFrame(
        {
            "id": list(range(1, 61)),
            "grade": ["A", "B", "C"] * 20,
            "amount": [float(value) for value in range(1, 61)],
        }
    )
    ws.add_table("ledger.csv", ledger.write_csv().encode())
    # Four rows beside sixty: small in itself and dwarfed by what it sits next
    # to, which is what makes it a lookup rather than a small population.
    grades = pl.DataFrame({"grade": ["A", "B", "C", "D"], "limit": [1, 2, 3, 4]})
    ws.add_table("grades.csv", grades.write_csv().encode())

    scope = analysis_capabilities.resolve_table_scope(ws, {})
    assert set(scope.targets) == {"ledger", "grades"}
    assert analysis_capabilities.definable_targets(ws, scope) == ("ledger",)

    # Naming it is the answer to whether it is worth analysing.
    named = analysis_capabilities.resolve_table_scope(ws, {"tables": ["grades"]})
    assert "grades" in analysis_capabilities.definable_targets(ws, named)


def test_a_small_workspace_is_not_an_empty_one():
    """The rule discriminates between frames; it cannot decide an engagement."""
    ws = workspaces.create_workspace("Small everywhere")
    for name in ("left", "right"):
        frame = pl.DataFrame({"id": [1, 2, 3], "value": [1, 2, 3]})
        ws.add_table(f"{name}.csv", frame.write_csv().encode())

    scope = analysis_capabilities.resolve_table_scope(ws, {})
    # Every frame is under the floor, and none is dwarfed by another. Pruning
    # here would analyse nothing at all, so nothing is pruned.
    assert set(analysis_capabilities.definable_targets(ws, scope)) == {"left", "right"}


def test_unknown_table_blocks_every_analysis_capability(workspace_with_data):
    scope = {"tables": ["not_imported"]}
    state = capability_registries.ANALYSIS_REGISTRY.workflow_state(
        workspace_with_data, scope
    )
    assert state["data.relationships_inferred"]["state"] == "blocked"
    assert "not_imported" in state["data.relationships_inferred"]["reasons"][0]
    assert {payload["state"] for payload in state.values()} == {"blocked"}


def test_ambiguous_scope_asks_the_auditor_in_permission_mode(monkeypatch):
    ws = workspaces.create_workspace("Ambiguous scope")
    for index in range(analysis_capabilities.MAX_SCOPE_TABLES + 2):
        frame = pl.DataFrame({"id": [1, 2, 3], "value": [index, index, index]})
        ws.add_table(f"table_{index}.csv", frame.write_csv().encode())
    _fake_model(monkeypatch)
    run = _analysis_run(ws, mode="permission")

    scheduler = build_analysis_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    )
    adapter = scheduler.execution_adapter
    responses = []

    def answer(interaction, **_kwargs):
        responses.append(interaction)
        return {"tables": ["table_0", "table_3"]}

    monkeypatch.setattr(adapter.runtime, "wait_for_interaction", answer)
    monkeypatch.setattr(adapter.runtime, "resolve_interaction", lambda *_a, **_k: None)
    adapter._scope_checkpoint()

    assert responses and responses[0]["type"] == "analysis_scope"
    assert responses[0]["payload"]["max_tables"] == analysis_capabilities.MAX_SCOPE_TABLES
    assert run["workflow"]["scope"]["tables"] == ["table_0", "table_3"]
    assert adapter.scope().tables == ("table_0", "table_3")

    # Declared on both fan-out boundaries, but settled once per run.
    assert set(analysis_capabilities.STAGE_CHECKPOINTS) == {
        "data.relationships_inferred",
        "analysis.definitions_ready",
    }
    adapter._scope_checkpoint()
    assert len(responses) == 1


def test_ambiguous_scope_is_reported_once_in_auto_mode(monkeypatch):
    ws = workspaces.create_workspace("Ambiguous auto scope")
    for index in range(analysis_capabilities.MAX_SCOPE_TABLES + 2):
        frame = pl.DataFrame({"id": [1, 2, 3], "value": [index, index, index]})
        ws.add_table(f"table_{index}.csv", frame.write_csv().encode())
    _fake_model(monkeypatch)
    run = _analysis_run(ws)

    adapter = build_analysis_workflow_runner(
        ws, run, runner.RunHandle(ws.id, run["id"])
    ).execution_adapter
    adapter._scope_checkpoint()
    adapter._scope_checkpoint()

    assert len(run["warnings"]) == 1
    assert "Name the tables to analyse instead." in run["warnings"][0]
    assert not run.get("interactions")


# --------------------------------------------------------------------------- #
# P8.4 — deterministic relationship inference
# --------------------------------------------------------------------------- #
def test_two_table_request_infers_the_plausible_join_deterministically(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    fake = _fake_model(monkeypatch)
    run = _analysis_run(ws)

    _drive(ws, run, "data.relationships_inferred")

    stage = _stage(run, "data.relationships_inferred")
    unit = stage["units"][0]
    assert stage["status"] == "succeeded"
    assert unit["status"] == "succeeded"
    assert "relationship:transactions:customers:cust_id:id" in unit["result_refs"]

    record = run["analysis"]["relationships"][0]
    best = record["strong"][0]
    assert (best["left"], best["right"]) == ("transactions", "customers")
    assert best["left_on"] == ["cust_id"] and best["right_on"] == ["id"]
    # The evidence is exactly what the deterministic diagnostic produced.
    assert best["diagnostics"] == join_diagnostics.diagnose(
        ws.get_frame("transactions"), ws.get_frame("customers"), "cust_id", "id"
    )
    assert best["diagnostics"]["match_rate"] == 1.0
    assert best["diagnostics"]["right_key_unique"] is True
    # Inference is local: no provider call, and nothing committed.
    assert fake.calls == []
    assert workspaces.load_workspace(ws.id).joins == []


def test_relationship_inference_cannot_reach_a_model():
    source = inspect.getsource(analysis_executors)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(name.endswith(("model_gateway", "workers")) for name in imported)
    assert "ModelGateway" not in source
    assert "app.llm" not in source
    assert ".complete(" not in source


# --------------------------------------------------------------------------- #
# P8.5 — join materialization
# --------------------------------------------------------------------------- #
def test_strong_evidence_materializes_exactly_one_guarded_join(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    fake = _fake_model(monkeypatch)
    run = _analysis_run(ws)
    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")

    stage = _stage(run, "data.joins_ready")
    unit = stage["units"][0]
    assert unit["status"] == "succeeded"
    assert unit["result_refs"] == ["join:transactions_customers_joined"]

    fresh = workspaces.load_workspace(ws.id)
    assert [item["name"] for item in fresh.joins] == ["transactions_customers_joined"]
    created = fresh.joins[0]
    assert created["left"] == "transactions" and created["right"] == "customers"
    assert created["created_by"] == "agent"
    assert created["agent_run_id"] == run["id"]
    # The join is a real derived table.
    assert fresh.get_frame("transactions_customers_joined").height == 6
    # A receipt proves the guarded commit, and no model chose the join: the one
    # turn the run spent went to the utility gate, which admits a relationship
    # without naming keys, and the key itself came from local evidence.
    assert unit["receipt_sidecar"]["unit_id"] == unit["id"]
    assert unit["proposal_sidecar"]["unit_id"] == unit["id"]
    assert [call["tag"] for call in fake.calls] == ["agent:join_utility"]


def test_auto_mode_materializes_the_top_ranked_moderate_join(monkeypatch):
    ws = _moderate_pair_workspace()
    _fake_model(monkeypatch)
    run = _analysis_run(ws)
    _drive(ws, run, "data.relationships_inferred")

    record = run["analysis"]["relationships"][0]
    assert record["strong"] == []
    assert record["moderate"], "the 90% match should be a moderate candidate"

    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")
    unit = _stage(run, "data.joins_ready")["units"][0]
    assert unit["status"] == "succeeded"
    fresh = workspaces.load_workspace(ws.id)
    assert len(fresh.joins) == 1
    assert fresh.joins[0]["left_on"] == ["order_ref"]
    assert fresh.joins[0]["right_on"] == ["order_ref"]
    assert any(
        "Auto-selected the best-evidenced join candidate" in warning
        for warning in run["warnings"]
    )


def test_audit_analysis_dependencies_are_partial_when_join_review_is_open():
    # The audit composition must preserve the standalone analysis workflow's
    # ability to derive analyses from frames and joins that are already usable.
    from app.agent import audit_execution

    assert audit_execution._PARTIAL_DEPENDENCIES["analysis.definitions_ready"] == {
        "data.joins_ready"
    }
    assert audit_execution._PARTIAL_DEPENDENCIES["analysis.executed"] == {
        "analysis.definitions_ready"
    }


def test_unrelatable_tables_are_skipped_rather_than_joined(monkeypatch):
    ws = workspaces.create_workspace("No relationship")
    left = pl.DataFrame({"alpha_code": ["A", "B", "C"], "value": [1, 2, 3]})
    right = pl.DataFrame({"zulu_name": ["x", "y", "z"], "other": [9, 8, 7]})
    ws.add_table("alphas.csv", left.write_csv().encode())
    ws.add_table("zulus.csv", right.write_csv().encode())
    _fake_model(monkeypatch)
    run = _analysis_run(ws)

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")

    relationship = _stage(run, "data.relationships_inferred")["units"][0]
    assert relationship["status"] == "succeeded"
    assert relationship["error"] is None
    unit = _stage(run, "data.joins_ready")["units"][0]
    assert unit["status"] == "skipped"
    assert unit["error"] is None
    assert run["warnings"] == []
    assert narration.skipped(run) == []
    assert workspaces.load_workspace(ws.id).joins == []


def _mixed_strength_workspace() -> workspaces.Workspace:
    """Two tables related both strongly and moderately, by different keys.

    ``region_code`` is unique in ``regions`` and fully matched from ``orders``,
    so it diagnoses strong. ``order_ref`` matches 9 of 10 and repeats on the
    left, so it diagnoses moderate. Which of the two an auditor wants is a
    question about the engagement, not about the evidence.
    """

    ws = workspaces.create_workspace("Mixed evidence")
    orders = pl.DataFrame(
        {
            "order_ref": [f"R{index}" for index in range(1, 10)] + ["R1"],
            "region_code": [f"G{index}" for index in range(1, 10)] + ["G1"],
            "amount": [float(index) for index in range(1, 11)],
        }
    )
    regions = pl.DataFrame(
        {
            "order_ref": [f"R{index}" for index in range(1, 9)] + ["R11"],
            "region_code": [f"G{index}" for index in range(1, 10)],
            "region": list("abcdefghi"),
        }
    )
    ws.add_table("orders.csv", orders.write_csv().encode())
    ws.add_table("regions.csv", regions.write_csv().encode())
    return ws


def _gate_script(retain: str | None):
    """Answer the gate by retaining one named ref and rejecting the rest."""

    def respond(user: str) -> dict:
        catalog = json.loads(user.split("\nRepair the prior response")[0])[
            "JOIN CANDIDATES"
        ]
        return {
            "decisions": [
                {
                    "ref": candidate["ref"],
                    "decision": "retain" if candidate["ref"] == retain else "reject",
                    "rationale": "The keys match but no control spans the two tables.",
                    "hypothesis": "Every key resolves on the other side.",
                    "columns": [
                        f"{candidate['left']}.{candidate['left_on'][0]}",
                        f"{candidate['right']}.{candidate['right_on'][0]}",
                    ],
                    "requires": [candidate["left"], candidate["right"]],
                }
                for candidate in catalog["candidates"]
            ]
        }

    return respond


def test_the_gate_may_keep_a_moderate_key_over_the_strong_one_it_rejected(monkeypatch):
    """Evidence ranks the keys; the gate decides which relationship is a test.

    A pair whose best-evidenced key answers no audit question is not thereby a
    pair with nothing to join — reading a rejected strong candidate as a
    rejected pair would discard the weaker key the gate kept on purpose.
    """

    ws = _mixed_strength_workspace()
    moderate_ref = "relationship:orders:regions:order_ref:order_ref"
    fake = _fake_model(monkeypatch)
    fake.overrides["agent:join_utility"] = _gate_script(moderate_ref)
    run = _analysis_run(ws)

    _drive(ws, run, "data.relationships_inferred")
    record = run["analysis"]["relationships"][0]
    assert [item["left_on"] for item in record["strong"]] == [["region_code"]]
    assert [item["left_on"] for item in record["moderate"]] == [["order_ref"]]

    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")

    assert _stage(run, "data.joins_ready")["units"][0]["status"] == "succeeded"
    fresh = workspaces.load_workspace(ws.id)
    assert [item["left_on"] for item in fresh.joins] == [["order_ref"]]
    # The strong key was diagnosed, judged, and passed over. A reader comparing
    # this frame against the evidence has to be able to see that happen.
    assert any(
        "region_code" in warning and "order_ref" in warning
        for warning in run["warnings"]
    )


def test_a_pair_the_gate_rejects_outright_is_skipped_and_says_why(monkeypatch):
    """An absent frame explains nothing by itself. The gate wrote a reason for
    every rejection, and that reason is the only account of why an analysis a
    reader expected does not exist."""

    ws = _mixed_strength_workspace()
    fake = _fake_model(monkeypatch)
    fake.overrides["agent:join_utility"] = _gate_script(None)
    run = _analysis_run(ws)

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")

    unit = _stage(run, "data.joins_ready")["units"][0]
    assert unit["status"] == "skipped"
    assert workspaces.load_workspace(ws.id).joins == []
    assert any(
        "No join was materialized for 'orders' and 'regions'" in warning
        and "no control spans the two tables" in warning
        for warning in run["warnings"]
    ), "the gate's own words are the explanation"


def test_the_gate_is_not_asked_about_a_pair_local_evidence_already_rejected(
    monkeypatch,
):
    """Two unrelatable tables produce no safe candidate, so there is nothing to
    judge. Spending a provider turn to hear that back is the one cost a purely
    local diagnosis exists to avoid."""

    ws = workspaces.create_workspace("No relationship")
    left = pl.DataFrame({"alpha_code": ["A", "B", "C"], "value": [1, 2, 3]})
    right = pl.DataFrame({"zulu_name": ["x", "y", "z"], "other": [9, 8, 7]})
    ws.add_table("alphas.csv", left.write_csv().encode())
    ws.add_table("zulus.csv", right.write_csv().encode())
    fake = _fake_model(monkeypatch)
    run = _analysis_run(ws)

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")

    assert _stage(run, "data.join_utility_ready")["units"][0]["status"] == "skipped"
    assert [call["tag"] for call in fake.calls] == []


def _requires_script(requires_by_pair):
    """Answer the gate, declaring what each retained test actually reads."""

    def respond(user: str) -> dict:
        catalog = json.loads(user.split("\nRepair the prior response")[0])[
            "JOIN CANDIDATES"
        ]
        decisions, seen = [], set()
        for candidate in catalog["candidates"]:
            pair = frozenset((candidate["left"], candidate["right"]))
            requires = requires_by_pair.get(pair)
            if requires is None or pair in seen:
                decisions.append(
                    {
                        "ref": candidate["ref"],
                        "decision": "reject",
                        "rationale": "No control spans these two tables.",
                    }
                )
                continue
            seen.add(pair)
            decisions.append(
                {
                    "ref": candidate["ref"],
                    "decision": "retain",
                    "rationale": "The relationship carries a testable control.",
                    "hypothesis": f"A test over {', '.join(sorted(requires))} must hold.",
                    "columns": [
                        f"{candidate['left']}.{candidate['left_on'][0]}",
                        f"{candidate['right']}.{candidate['right_on'][0]}",
                    ],
                    "requires": sorted(requires),
                }
            )
        return {"decisions": decisions}

    return respond


def test_a_test_spanning_three_tables_is_prepared_on_the_frame_that_can_run_it(
    monkeypatch,
):
    """The approval-limit shape. The limit is a relationship between the plan
    and the customer, but the test compares an *order* against it, so the pair's
    own frame holds no amount to check. Preparing the test there spends a turn
    on a frame that cannot answer it — which is what ``requires`` exists to
    prevent."""

    ws = _three_hop_workspace()
    fake = _fake_model(monkeypatch)
    fake.overrides["agent:join_utility"] = _requires_script(
        {
            frozenset(("orders", "customers")): {"orders", "customers"},
            frozenset(("customers", "plans")): {"orders", "customers", "plans"},
        }
    )
    run = _analysis_run(ws, text="join these tables and analyse them")

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")
    _drive(ws, run, "analysis.definitions_ready")

    units = {
        unit["id"].split(":", 1)[1]: unit["status"]
        for unit in _stage(run, "analysis.definitions_ready")["units"]
    }
    chained = next(name for name in units if name.count("joined") > 1)
    pair_frame = next(
        name for name in units if "customers" in name and "plans" in name and name != chained
    )

    # The three-table test lands on the only frame that holds all three.
    assert units[chained] == "succeeded"
    # Its pair's own frame carries nothing else, so it is not asked at all.
    assert units[pair_frame] == "skipped"
    # Base tables are never narrowed away: single-table work needs no join.
    assert all(units[name] == "succeeded" for name in ("orders", "customers", "plans"))
    assert "agent:analysis_definitions" not in [
        call["tag"]
        for call in fake.calls
        if json.loads(
            call["messages"][-1]["content"].split("\n\nYour previous")[0]
        ).get("TARGET FRAME", {}).get("table") == pair_frame
    ]


def test_a_frame_is_told_which_test_it_was_materialized_to_support(monkeypatch):
    """The gate stated a falsifiable test before the join existed. A frame left
    to re-derive its purpose from schemas writes a worse question than the one
    already asked of it — a completeness check on a dimension frame rather than
    the control the join was admitted for."""

    ws = _mixed_strength_workspace()
    fake = _fake_model(monkeypatch)
    fake.overrides["agent:join_utility"] = _requires_script(
        {frozenset(("orders", "regions")): {"orders", "regions"}}
    )
    run = _analysis_run(ws)

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")
    _drive(ws, run, "analysis.definitions_ready")

    joined = next(
        call
        for call in fake.calls
        if call["tag"] == "agent:analysis_definitions"
        and json.loads(
            call["messages"][-1]["content"].split("\n\nYour previous")[0]
        )["TARGET FRAME"]["table"].endswith("_joined")
    )
    payload = json.loads(
        joined["messages"][-1]["content"].split("\n\nYour previous")[0]
    )
    carried = payload["TESTS THIS FRAME WAS MATERIALIZED TO SUPPORT"]
    assert [item["hypothesis"] for item in carried] == [
        "A test over orders, regions must hold."
    ]
    # The target schema is named once. It used to be sent again inside the
    # serialized bundle, billing the same block twice on every frame.
    supplied = payload["RESOLVED CONTEXT"]["items"]
    assert not [item for item in supplied if item["source_id"] == "target_schema"]
    # A frame's own sides describe no column it does not already hold.
    assert not [
        item
        for item in supplied
        if item["source_id"] == "related_frames"
        and item["content"]["table"] in {"orders", "regions"}
    ]


def test_a_frame_carrying_a_hypothesis_is_told_the_join_already_happened(monkeypatch):
    """The regression this guards: a hypothesis like "invoice X has a matching
    GRN in po_data" describes the join condition that already built the frame.
    A model told only that text, with no framing, wrote a self-join to
    re-establish a match that was already true of every row — and the bad spec
    then cost its working sibling too (see
    test_one_broken_python_spec_does_not_cost_its_working_siblings). The system
    prompt sent alongside a carried hypothesis has to say the match already
    holds, not leave the model to infer it."""

    ws = _mixed_strength_workspace()
    fake = _fake_model(monkeypatch)
    fake.overrides["agent:join_utility"] = _requires_script(
        {frozenset(("orders", "regions")): {"orders", "regions"}}
    )
    run = _analysis_run(ws)

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")
    _drive(ws, run, "analysis.definitions_ready")

    joined = next(
        call
        for call in fake.calls
        if call["tag"] == "agent:analysis_definitions"
        and json.loads(
            call["messages"][-1]["content"].split("\n\nYour previous")[0]
        )["TARGET FRAME"]["table"].endswith("_joined")
    )
    system = joined["messages"][0]["content"]
    assert "already reflect" in system
    assert "no join, self-join" in system


def test_a_retained_test_no_frame_can_carry_is_reported_rather_than_lost(monkeypatch):
    """A gap in what the run prepared is a finding about the run. The frames it
    skipped are not where a reader would go looking for it, so it is said once,
    plainly, before any frame is bound."""

    ws = _three_hop_workspace()
    fake = _fake_model(monkeypatch)
    # The limit test needs all three tables, but only the first hop is
    # retained, so nothing ever brings ``plans`` alongside ``orders``.
    fake.overrides["agent:join_utility"] = _requires_script(
        {frozenset(("orders", "customers")): {"orders", "customers", "plans"}}
    )
    run = _analysis_run(ws, text="join these tables and analyse them")

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")
    _drive(ws, run, "analysis.definitions_ready")

    assert any(
        "No materialized frame brings together" in warning
        and "plans" in warning
        for warning in run["warnings"]
    ), "a test the run prepared nowhere has to be said out loud"


def test_join_commit_is_parent_guarded_and_reconciles_an_interrupted_attempt(
    workspace_with_data,
):
    ws = workspace_with_data
    spec = {
        "left": "transactions",
        "right": "customers",
        "how": "left",
        "left_on": ["cust_id"],
        "right_on": ["id"],
    }
    expected = parent_hashes(ws, ["table:transactions", "table:customers"])

    def build_request(parents):
        return ExecutorRequest(
            executor_id="analysis.join",
            capability_id="data.joins_ready",
            unit_id="join:customers:transactions",
            proposal={"join": spec},
            expected_revision=ws.revision,
            expected_parents=parents,
        )

    target = analysis_executors.JoinExecutorTarget(
        ws, "run-join", "transactions", "customers"
    )
    first = analysis_executors.execute_join(build_request(expected), target)
    assert first.artifact_refs == ("join:transactions_customers_joined",)

    # Replaying the same commit is reconciled, not repeated.
    replay_target = analysis_executors.JoinExecutorTarget(
        target.workspace, "run-join", "transactions", "customers"
    )
    reconciliation = analysis_executors.reconcile_join(
        build_request(expected), replay_target
    )
    assert reconciliation.disposition == "already_applied"
    assert len(workspaces.load_workspace(ws.id).joins) == 1

    # A replaced source table is a parent conflict, not an overwrite.
    fresh = workspaces.load_workspace(ws.id)
    replacement = pl.DataFrame({"id": ["C1"], "customer": ["Alpha only"]})
    fresh.replace_table("customers", "customers.csv", replacement.write_csv().encode())
    changed_target = analysis_executors.JoinExecutorTarget(
        workspaces.load_workspace(ws.id), "run-join", "transactions", "customers"
    )
    conflicted = analysis_executors.reconcile_join(
        build_request(expected), changed_target
    )
    assert conflicted.disposition == "conflict"
    assert "table:customers" in str(conflicted.reason)


# --------------------------------------------------------------------------- #
# P8.6 — declared context and privacy
# --------------------------------------------------------------------------- #
def test_analysis_context_supplies_metadata_and_aggregates_without_row_values(
    workspace_with_data,
):
    ws = workspace_with_data
    capability = capability_registries.ANALYSIS_REGISTRY.get(
        "analysis.definitions_ready"
    )
    scope = analysis_definition_scope(
        ws,
        "transactions",
        related=["customers"],
        relationships=[
            {
                "left": "transactions",
                "right": "customers",
                "left_on": ["cust_id"],
                "right_on": ["id"],
                "how": "left",
                "strength": "strong",
                "diagnostics": {"match_rate": 1.0},
            }
        ],
    )
    manifest, bundle = ContextResolver().resolve(
        ws, capability, {"id": "analysis_definitions:transactions"}, scope
    )

    supplied = {item.source_id for item in bundle.items}
    assert {"target_schema", "target_aggregates", "analytics_registry"} <= supplied
    kinds = {item.representation.kind for item in bundle.items}
    assert "table_rows" not in kinds
    assert kinds <= {"table_metadata", "table_profile", "table_aggregate", "current_artifact"}

    serialized = bundle.to_json()
    # No row-level data and no category literals: the fixture's customer names
    # and customer codes are exactly the value class the planning presets also
    # withhold, and nothing supplies a row.
    for literal in ("Alpha", "Beta", "Gamma", '"C1"', "top_values"):
        assert literal not in serialized
    assert "1001" not in serialized, "an invoice number is a row value"
    # The manifest stays content-free: it records which references were
    # selected and their hashes, never the values or metrics they carried.
    manifest_json = manifest.to_json()
    for content in ("1001", "Alpha", "duplicate_rows", "distinct_count", "match_rate"):
        assert content not in manifest_json
    assert manifest.selections and all(
        selection.source_ref for selection in manifest.selections
    )

    aggregates = [
        item.content for item in bundle.items if item.source_id == "target_aggregates"
    ]
    assert any(item.get("scope") == "table" for item in aggregates)
    columns = [item for item in aggregates if item.get("scope") == "column"]
    assert columns and all("top_values" not in item for item in columns)
    amount = next(item for item in columns if item["column"] == "amount")
    assert amount["inferred_type"] == "numeric"
    assert amount["distinct_count"] == 4
    # A categorical column contributes shape only — never its values.
    cust = next(item for item in columns if item["column"] == "cust_id")
    assert cust["inferred_type"] == "categorical"
    assert set(cust) == {
        "table",
        "scope",
        "column",
        "dtype",
        "inferred_type",
        "rows",
        "blank_count",
        "blank_pct",
        "distinct_count",
        "distinct_pct",
    }

    relationship = [
        item.content
        for item in bundle.items
        if item.source_id == "relationship_evidence"
    ]
    assert relationship and relationship[0]["diagnostics"]["match_rate"] == 1.0


def test_values_reach_this_preset_only_through_their_own_declared_sources():
    """The two value classes this preset admits, and the one it still does not.

    The aggregates above stay value-free, and that is now a statement about the
    aggregates rather than about the preset: a vocabulary and a bounded sample
    are admitted, each under its own permission, and an arbitrary slice of a
    table is still refused outright.
    """
    ws = workspaces.create_workspace("Values")
    ws.add_table(
        "requisitions.csv",
        pl.DataFrame(
            {
                "REQ_ID": [f"R{n:03d}" for n in range(1, 31)],
                "REQUISITION_STATUS": [
                    ("Rejected" if n <= 4 else "Approved" if n % 2 else "Pending")
                    for n in range(1, 31)
                ],
                "ESTIMATED_TOTAL_COST": [float(n * 100) for n in range(1, 31)],
            }
        )
        .write_csv()
        .encode(),
    )
    capability = capability_registries.ANALYSIS_REGISTRY.get(
        "analysis.definitions_ready"
    )
    findings = probes.probe_frame(ws, "requisitions")
    scope = analysis_definition_scope(
        ws,
        "requisitions",
        probe_findings=findings,
        value_domains=probes.value_domains(ws, "requisitions"),
    )
    _, bundle = ContextResolver().resolve(
        ws, capability, {"id": "analysis_definitions:requisitions"}, scope
    )
    kinds = {item.representation.kind for item in bundle.items}
    assert "value_domain" in kinds
    # The wider disclosures a vocabulary does not reopen.
    assert "table_rows" not in kinds
    assert "population_sample" not in kinds

    spec = PRESETS.get("analysis.definitions").spec
    assert spec.privacy.allow_value_domains is True
    assert spec.privacy.allow_table_rows is False
    assert spec.privacy.allow_analysis_exception_rows is False
    assert not hasattr(spec.privacy, "allow_population_sample")

    domain = next(
        item.content
        for item in bundle.items
        if item.representation.kind == "value_domain"
    )
    # A vocabulary names what values exist and never which row holds one.
    assert domain["column"] == "REQUISITION_STATUS"
    assert domain["values"] == ["Approved", "Pending", "Rejected"]
    assert "R001" not in bundle.to_json(), "a requisition id is a row value"


def test_analysis_context_supplies_the_complete_workflow_library_contract(
    workspace_with_data,
):
    ws = workspace_with_data
    capability = capability_registries.ANALYSIS_REGISTRY.get("analysis.definitions_ready")
    _manifest, bundle = ContextResolver().resolve(
        ws,
        capability,
        {"id": "analysis_definitions:transactions"},
        analysis_definition_scope(ws, "transactions"),
    )
    supplied_registry = next(
        item.content for item in bundle.items if item.source_id == "analytics_registry"
    )
    expected = [
        item
        for item in analytics.registry_payload()
        if item["id"] not in ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS
    ]
    assert supplied_registry == expected
    supplied_ids = {item["id"] for item in supplied_registry}
    assert ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS.isdisjoint(supplied_ids)
    outliers = next(item for item in supplied_registry if item["id"] == "outliers")
    assert next(item for item in outliers["params"] if item["name"] == "method") == {
        "name": "method",
        "kind": "select",
        "label": "Method",
        "options": [
            {"label": "Z-score (mean ± kσ)", "value": "zscore"},
            {"label": "IQR (Tukey fence)", "value": "iqr"},
        ],
        "default": "zscore",
    }


def test_low_impact_digit_tests_are_not_workflow_analyses(workspace_with_data):
    ws = workspace_with_data
    for test_id in sorted(ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS):
        ws.add_analysis(
            {
                "title": f"Excluded {test_id}",
                "kind": "analytics",
                "table": "transactions",
                "spec": {"test": test_id, "params": {}},
                "agent_run_id": "run-exclusions",
            }
        )
    included = ws.add_analysis(
        {
            "title": "Included duplicate test",
            "kind": "analytics",
            "table": "transactions",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
            "agent_run_id": "run-exclusions",
        }
    )
    table_scope = analysis_capabilities.resolve_table_scope(
        ws, {"target_refs": ["table:transactions"]}
    )

    assert [
        item["id"] for item in analysis_capabilities.agent_analyses(ws, table_scope)
    ] == [included["id"]]


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"column": "amount", "method": "tukey"}, "must be one of"),
        (
            {"column": "amount", "method": "iqr", "made_up": True},
            "unsupported parameter",
        ),
        (
            {"column": "amount", "method": "iqr", "threshold": True},
            "must be a number",
        ),
        ({"column": "cust_id", "method": "iqr"}, "requires a numeric column"),
    ],
)
def test_analysis_worker_enforces_library_test_contract(
    workspace_with_data, params, error
):
    ws = workspace_with_data
    capability = capability_registries.ANALYSIS_REGISTRY.get("analysis.definitions_ready")
    _manifest, bundle = ContextResolver().resolve(
        ws,
        capability,
        {"id": "analysis_definitions:transactions"},
        analysis_definition_scope(ws, "transactions"),
    )
    request = WorkerRequest(
        worker_id=analysis_worker.ANALYSIS_DEFINITION_WORKER_ID,
        capability_id=capability.id,
        unit_id="analysis_definitions:transactions",
        context=bundle,
    )
    with pytest.raises(WorkerResponseValidationError, match=error):
        analysis_worker.validate_analysis_proposal(
            {
                "analyses": [
                    {
                        "title": "Outlier test",
                        "kind": "analytics",
                        "spec": {"test": "outliers", "params": params},
                        "note": "Expected to hold across these columns.",
                    }
                ]
            },
            request,
        )


def test_analysis_worker_rejects_repeated_columns_locally(workspace_with_data):
    ws = workspace_with_data
    capability = capability_registries.ANALYSIS_REGISTRY.get(
        "analysis.definitions_ready"
    )
    _manifest, bundle = ContextResolver().resolve(
        ws,
        capability,
        {"id": "analysis_definitions:transactions"},
        analysis_definition_scope(ws, "transactions"),
    )
    request = WorkerRequest(
        worker_id=analysis_worker.ANALYSIS_DEFINITION_WORKER_ID,
        capability_id=capability.id,
        unit_id="analysis_definitions:transactions",
        context=bundle,
    )

    with pytest.raises(WorkerResponseValidationError, match="repeats a column"):
        analysis_worker.validate_analysis_proposal(
            {
                "analyses": [
                    {
                        "title": "Duplicate test",
                        "kind": "analytics",
                        "spec": {
                            "test": "duplicates",
                            "params": {
                                "columns": ["invoice_no", "invoice_no"],
                            },
                        },
                        "note": "Expected to hold across these columns.",
                    }
                ]
            },
            request,
        )


def test_analysis_generation_is_constrained_by_a_forced_target_specific_tool(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    fake = _fake_model(monkeypatch)
    run = _analysis_run(
        ws,
        command={
            "source": "chat",
            "text": "Explore the data in this workspace",
            "target_refs": ["table:transactions"],
        },
    )
    _drive(ws, run, "analysis.definitions_ready")

    call = next(item for item in fake.calls if item["tag"] == ANALYSIS_TAG)
    assert call["tool_choice"] == {
        "type": "function",
        "function": {"name": analysis_worker.ANALYSIS_SUBMISSION_TOOL},
    }
    assert len(call["tools"]) == 1
    tool = call["tools"][0]
    assert tool["function"]["name"] == analysis_worker.ANALYSIS_SUBMISSION_TOOL
    assert "uniqueItems" not in json.dumps(tool)
    item_branches = tool["function"]["parameters"]["properties"]["analyses"]["items"][
        "oneOf"
    ]
    analytics_branch = next(
        item
        for item in item_branches
        if item["properties"]["kind"]["enum"] == ["analytics"]
    )
    spec_branches = analytics_branch["properties"]["spec"]["oneOf"]
    allowed_ids = {
        item["properties"]["test"]["enum"][0] for item in spec_branches
    }
    assert ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS.isdisjoint(allowed_ids)
    # The fixture holds six rows, so tests that answer a question about a
    # population are not offered at all: an IQR fence over six values is
    # arithmetic without a finding in it. Per-row integrity checks remain.
    assert analysis_worker.POPULATION_TEST_IDS.isdisjoint(allowed_ids)
    assert {"completeness", "duplicates", "date_lag", "sign_scan"} <= allowed_ids
    # A frame this small is also asked for fewer analyses than a populous one.
    assert tool["function"]["parameters"]["properties"]["analyses"]["maxItems"] == (
        analysis_worker.SMALL_FRAME_ANALYSES
    )

    sign_scan = next(
        item
        for item in spec_branches
        if item["properties"]["test"]["enum"] == ["sign_scan"]
    )
    amount_columns = sign_scan["properties"]["params"]["properties"]["column"]["enum"]
    assert "amount" in amount_columns
    assert "cust_id" not in amount_columns


@pytest.mark.parametrize(
    ("title", "code", "error"),
    [
        (
            "Date lag",
            "df = tables['transactions']\nresult = (df['tx_date'] - df['tx_date']).dt.days()",
            "DateTimeNameSpace.*days",
        ),
        (
            "Outlier count",
            "values = tables['transactions']['amount']\nresult = pl.DataFrame({'count': [values.height]})",
            "Series.*height",
        ),
        (
            "Ambiguous join",
            "left = tables['transactions'].rename({'cust_id': 'VENDOR_ID', 'amount': 'VENDOR_ID_right'})\nright = tables['transactions'].rename({'cust_id': 'VENDOR_ID'})\nresult = left.join(right, on='invoice_no', how='left')",
            "DuplicateError.*VENDOR_ID_right",
        ),
    ],
)
def test_generated_python_analysis_must_run_before_it_is_saved(
    workspace_with_data, title, code, error
):
    ws = workspace_with_data
    parent_ref = "table:transactions"
    semantic_id = analysis_worker.analysis_semantic_id(
        "python", "transactions", {"code": code}
    )
    request = ExecutorRequest(
        executor_id=analysis_executors.DEFINITIONS_EXECUTOR_ID,
        capability_id="analysis.definitions_ready",
        unit_id="analysis_definitions:transactions",
        proposal={
            "analyses": [
                {
                    "title": title,
                    "kind": "python",
                    "table": "transactions",
                    "spec": {"code": code},
                    "semantic_id": semantic_id,
                }
            ]
        },
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [parent_ref]),
    )
    target = analysis_executors.AnalysisDefinitionExecutorTarget(
        ws, "run-python-validation", "transactions", parent_ref
    )

    with pytest.raises(workspaces.WorkspaceError, match=error):
        analysis_executors.execute_analysis_definitions(request, target)

    assert not ws.analyses


def test_the_declared_preset_denies_row_level_table_data():
    spec = PRESETS.compile("analysis.definitions")
    assert spec.privacy.allow_table_rows is False
    assert spec.privacy.allow_table_aggregates is True
    representations = {
        representation.kind
        for source in spec.sources
        for representation in source.representations
    }
    assert "table_rows" not in representations


# --------------------------------------------------------------------------- #
# P8.7 — the analysis-definition worker
# --------------------------------------------------------------------------- #
def test_invalid_definitions_are_repaired_once_and_never_commit(
    workspace_with_data, monkeypatch
):
    attempts = []

    def script(user: str) -> dict:
        attempts.append(user)
        if len(attempts) == 1:
            return {
                "analyses": [
                    {
                        "title": "Invented test",
                        "kind": "analytics",
                        "spec": {"test": "three_way_match", "params": {}},
                        "note": "Expected to hold across these columns.",
                    },
                    {
                        "title": "Unsafe code",
                        "kind": "python",
                        "spec": {"code": "import os\nresult = 1"},
                        "note": "Expected to hold across these columns.",
                    },
                    {
                        "title": "Wrong column",
                        "kind": "analytics",
                        "spec": {
                            "test": "duplicates",
                            "params": {"columns": ["not_a_column"]},
                        },
                        "note": "Expected to hold across these columns.",
                    },
                ]
            }
        return _scripted_definitions(user)

    ws = workspace_with_data
    fake = _fake_model(monkeypatch, script)
    run = _analysis_run(
        ws,
        command={
            "source": "chat",
            "text": "Analyse the tables",
            "target_refs": ["table:transactions"],
        },
    )
    _drive(ws, run, "analysis.definitions_ready")

    # Every violation was reported in one bounded repair turn.
    guidance = attempts[1]
    assert "unknown analytics test 'three_way_match'" in guidance
    assert "not safe Polars" in guidance
    assert "not in the supplied schema" in guidance
    assert len(attempts) == 2
    assert [call["tag"] for call in fake.calls] == [ANALYSIS_TAG, ANALYSIS_TAG]

    saved = workspaces.load_workspace(ws.id).analyses
    assert {item["title"] for item in saved} == {
        "Duplicates in transactions",
        "Preview transactions",
    }
    assert all(item["kind"] in {"analytics", "python"} for item in saved)


def test_the_worker_uses_only_the_supplied_bundle():
    source = inspect.getsource(analysis_worker)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name.endswith(("workspaces", "store", "resolver", "workflow_runner"))
        for name in imported
    )
    assert "Workspace" not in source
    # The frame a definition targets comes from the resolved context, never the
    # model's own answer.
    assert '"table": target' in source


# --------------------------------------------------------------------------- #
# P8.8 — deterministic definition persistence
# --------------------------------------------------------------------------- #
def test_definitions_deduplicate_and_preserve_auditor_edits(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    _fake_model(monkeypatch)
    command = {
        "source": "chat",
        "text": "Analyse the tables",
        "target_refs": ["table:transactions"],
    }
    first = _analysis_run(ws, command=command)
    _drive(ws, first, "analysis.definitions_ready")

    saved = workspaces.load_workspace(ws.id).analyses
    assert len(saved) == 2
    identities = {item["semantic_id"] for item in saved}
    assert all(identity.startswith("analysis:") for identity in identities)

    # The auditor edits one definition; a later run must not replace it.
    edited = workspaces.load_workspace(ws.id)
    edited.update_analysis(saved[0]["id"], {"title": "Auditor's duplicate scan"})
    assert edited._analysis(saved[0]["id"])["created_by"] == "user"

    second = _analysis_run(
        edited, command={**command, "generation_mode": "force"}
    )
    _drive(edited, second, "analysis.definitions_ready")

    after = workspaces.load_workspace(ws.id).analyses
    assert len(after) == 2, "an identical proposal must not create a second analysis"
    preserved = next(item for item in after if item["id"] == saved[0]["id"])
    assert preserved["title"] == "Auditor's duplicate scan"
    assert preserved["created_by"] == "user"


# --------------------------------------------------------------------------- #
# P8.9 — local execution and the bounded result contract
# --------------------------------------------------------------------------- #
def test_execution_is_local_and_persists_only_the_bounded_result(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    fake = _fake_model(monkeypatch)
    command = {
        "source": "chat",
        "text": "Analyse the tables",
        "target_refs": ["table:transactions"],
    }
    run = _analysis_run(ws, command=command)
    _drive(ws, run, "analysis.definitions_ready")
    model_turns_before = len(fake.calls)
    _drive(ws, run, "analysis.executed")

    stage = _stage(run, "analysis.executed")
    assert stage["status"] == "succeeded"
    assert len(fake.calls) == model_turns_before, "execution must not call a model"

    fresh = workspaces.load_workspace(ws.id)
    duplicates = next(
        item for item in fresh.analyses if item["title"] == "Duplicates in transactions"
    )
    result = duplicates["last_result"]
    assert result["status"] == "ok"
    assert result["run_id"] == run["id"]
    assert result["row_count"] >= 1
    assert result["result_sha1"]
    # Only the bounded contract is durable: no frame, no rows, no stdout.
    # ``exception_count``, ``exception_rows_retained``, and the denominators
    # are counts, not data — the flagged rows themselves live in the evidence
    # sidecar, never here.
    assert set(result) == {
        "run_id",
        "executed_at",
        "status",
        "error",
        "verdict",
        "verdict_text",
        "row_count",
        "column_count",
        "stat_count",
        "stats",
        "exception_count",
        "exception_rows_retained",
        "population",
        "tested",
        "not_tested",
        "exception_rate",
        "exception_rate_of",
        "informative",
        "uninformative_reason",
        "input_sha1",
        "result_sha1",
    }
    # The flagged count is recorded against what it is a count of, so a rate
    # cannot be read off the wrong denominator downstream.
    assert result["population"] == result["tested"]
    assert result["not_tested"] == 0
    assert result["exception_rate"] == round(
        result["exception_count"] / result["tested"], 4
    )
    assert result["exception_rate_of"] == "tested"
    # Two of six rows are duplicated, which separates them from the rest.
    assert result["informative"] is True
    assert result["uninformative_reason"] is None
    assert len(result["stats"]) <= analysis_executors.MAX_RESULT_STATS
    # Content hashes are opaque hex derived from the result; their digits carry
    # no row value, and searching them for one produces a false positive as soon
    # as any hashed input changes. Search everything else.
    searchable = {
        key: value for key, value in result.items()
        if not re.fullmatch(r"[0-9a-f]{32,}", str(value))
    }
    assert "1001" not in json.dumps(searchable)
    # The definition itself is still a spec that recomputes on demand.
    assert dashboard.compute_payload(fresh, duplicates)["error"] is None

    # The workflow writes the rows it flagged to the same evidence sidecar the
    # auditor's Run button writes, under the same result identity — so a
    # procedure is equally reviewable whichever origin executed it.
    evidence = analysis_results.read_exception_evidence(fresh, duplicates)
    assert (evidence is not None) == bool(result["exception_count"])
    if evidence is not None:
        assert evidence["result_sha1"] == result["result_sha1"]
        assert len(evidence["frame"]["rows"]) == result["exception_rows_retained"]


ORDERS_CSV = (
    b"ref,ordered,delivered,memo\n"
    b"R1,2026-01-01,2026-01-05,\n"
    b"R2,2026-02-01,2026-02-06,\n"
    b"R3,2026-03-01,2026-03-04,\n"
)


def test_a_comparison_that_separates_nothing_is_marked_uninformative(
    workspace_with_data,
):
    """Every row flagged means the two sides were never comparable."""
    ws = workspace_with_data
    ws.add_table("orders.csv", ORDERS_CSV)

    def lag(from_date: str, to_date: str) -> dict:
        analysis = ws.add_analysis({
            "kind": "analytics",
            "table": "orders",
            "title": f"{from_date} to {to_date}",
            "spec": {
                "test": "date_lag",
                "params": {"from_date": from_date, "to_date": to_date},
            },
        })
        return analysis_results.execute_analysis(ws, analysis, run_id="t").result

    # Delivery always follows the order, so asking for the reverse flags every
    # row — which establishes nothing about any of them.
    backwards = lag("delivered", "ordered")
    assert backwards["exception_rate"] == 1.0
    assert backwards["informative"] is False
    assert "never runs the other way" in backwards["uninformative_reason"]

    forwards = lag("ordered", "delivered")
    assert forwards["exception_count"] == 0
    assert forwards["informative"] is True


def test_a_wholly_blank_column_stays_a_finding(workspace_with_data):
    """Saturation is only meaningless where flagging everything cannot be the point.

    A completeness test that flags every row found a column with nothing in it.
    That is exactly what an auditor needs told, so the saturation rule must not
    reach it.
    """
    ws = workspace_with_data
    ws.add_table("orders.csv", ORDERS_CSV)
    analysis = ws.add_analysis({
        "kind": "analytics",
        "table": "orders",
        "title": "Completeness of memo",
        "spec": {"test": "completeness", "params": {"columns": ["memo"]}},
    })
    result = analysis_results.execute_analysis(ws, analysis, run_id="t").result
    assert result["exception_rate"] == 1.0
    assert result["informative"] is True
    assert result["uninformative_reason"] is None


def _weekend_result(ws, dates: list[str], table: str = "postings") -> dict:
    frame = pl.DataFrame({"posted": dates})
    ws.add_table(f"{table}.csv", frame.write_csv().encode())
    analysis = ws.add_analysis({
        "kind": "analytics",
        "table": table,
        "title": f"Weekend postings — {table}",
        "spec": {"test": "weekend_activity", "params": {"date_column": "posted"}},
    })
    return analysis_results.execute_analysis(ws, analysis, run_id="t").result


def _dates(weekends: int, weekdays: int) -> list[str]:
    # 2026-01-03 is a Saturday; 2026-01-05 a Monday. Both repeat weekly.
    return [f"2026-01-{3 + 7 * index:02d}" for index in range(weekends)] + [
        f"2026-01-{5 + 7 * index:02d}" for index in range(weekdays)
    ]


def test_a_weekend_share_at_the_base_rate_establishes_nothing(workspace_with_data):
    """Two days in seven are a weekend, so about 29% of any ordinary spread of
    dates lands on one. Reporting that share as a finding describes the calendar,
    which is what a whole engagement's worth of weekend results did.
    """
    result = _weekend_result(workspace_with_data, _dates(weekends=2, weekdays=4))
    assert result["exception_count"] == 2
    assert result["informative"] is False
    assert "expected by chance" in result["uninformative_reason"]


def test_a_genuinely_elevated_weekend_share_stays_a_finding(workspace_with_data):
    """The gate is about the distance from chance, not about the test."""
    result = _weekend_result(workspace_with_data, _dates(weekends=4, weekdays=0))
    assert result["informative"] is True
    assert result["uninformative_reason"] is None


def test_the_base_rate_gate_scales_with_the_population(workspace_with_data):
    """The same share means different things at different sizes, so the margin is
    measured in standard errors rather than in percentage points. Two weekend
    dates in five is what an ordinary calendar produces; the identical 40% across
    eighty rows is not."""
    ws = workspace_with_data
    spread = _dates(weekends=2, weekdays=3)
    small = _weekend_result(ws, spread, table="few_postings")
    large = _weekend_result(ws, spread * 16, table="many_postings")

    assert small["exception_rate"] == pytest.approx(0.4)
    assert large["exception_rate"] == pytest.approx(0.4)
    assert small["informative"] is False
    assert large["informative"] is True


def test_an_uninformative_proposal_is_run_and_dropped_before_it_is_saved(
    workspace_with_data, monkeypatch
):
    """Whether a comparison holds is not knowable from the schema — only by running it."""
    ws = workspace_with_data
    ws.add_table("orders.csv", ORDERS_CSV)

    def script(user: str) -> dict:
        return {
            "analyses": [
                {
                    "title": "Deliveries before their order",
                    "kind": "analytics",
                    "spec": {
                        "test": "date_lag",
                        "params": {"from_date": "delivered", "to_date": "ordered"},
                    },
                    "note": "A delivery cannot precede the order it fulfils.",
                },
                {
                    "title": "Duplicate references",
                    "kind": "analytics",
                    "spec": {"test": "duplicates", "params": {"columns": ["ref"]}},
                    "note": "One reference should identify one order.",
                },
            ]
        }

    _fake_model(monkeypatch, script)
    run = _analysis_run(
        ws,
        command={
            "source": "chat",
            "text": "Analyse the tables",
            "target_refs": ["table:orders"],
        },
    )
    _drive(ws, run, "analysis.definitions_ready")

    saved = {item["title"] for item in workspaces.load_workspace(ws.id).analyses}
    # The good proposal survives; the one that flags its whole population never
    # becomes a procedure with a verdict for the memo to narrate.
    assert saved == {"Duplicate references"}


def test_a_frame_whose_every_proposal_separates_nothing_settles(
    workspace_with_data, monkeypatch
):
    """A frame supporting no discriminating procedure is an answer, not a failure."""
    ws = workspace_with_data
    ws.add_table("orders.csv", ORDERS_CSV)

    def script(user: str) -> dict:
        return {
            "analyses": [
                {
                    "title": "Deliveries before their order",
                    "kind": "analytics",
                    "spec": {
                        "test": "date_lag",
                        "params": {"from_date": "delivered", "to_date": "ordered"},
                    },
                    "note": "A delivery cannot precede the order it fulfils.",
                }
            ]
        }

    _fake_model(monkeypatch, script)
    run = _analysis_run(
        ws,
        command={
            "source": "chat",
            "text": "Analyse the tables",
            "target_refs": ["table:orders"],
        },
    )
    _drive(ws, run, "analysis.definitions_ready")

    unit = _stage(run, "analysis.definitions_ready")["units"][0]
    assert unit["status"] == "skipped"
    assert "separated any of its rows" in unit["error"]
    assert not workspaces.load_workspace(ws.id).analyses


def test_a_proposal_without_its_rationale_is_rejected(workspace_with_data):
    """Writing why the two columns belong together is the check."""
    ws = workspace_with_data
    request = _definition_request(workspaces.load_workspace(ws.id), "transactions")
    proposal = {
        "analyses": [
            {
                "title": "Duplicate invoice numbers",
                "kind": "analytics",
                "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
            }
        ]
    }
    with pytest.raises(WorkerResponseValidationError, match="missing a note"):
        analysis_worker.validate_analysis_proposal(proposal, request)

    proposal["analyses"][0]["note"] = "One invoice number should identify one invoice."
    accepted = analysis_worker.validate_analysis_proposal(proposal, request)
    assert accepted["analyses"][0]["note"].startswith("One invoice number")


def test_the_budget_is_a_ceiling_and_a_frame_may_propose_nothing(workspace_with_data):
    """Padding a frame that supports nothing is what produced the bad procedures."""
    ws = workspace_with_data
    request = _definition_request(workspaces.load_workspace(ws.id), "transactions")
    tool = analysis_worker._analysis_submission_tool(request)
    array = tool["function"]["parameters"]["properties"]["analyses"]
    assert array["minItems"] == 0
    assert array["maxItems"] >= 1

    # An empty answer settles the unit rather than failing the run, by the same
    # route as a frame whose every proposal was already saved.
    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_proposal({"analyses": []}, request)
    assert analysis_worker.NOTHING_NEW_TO_ANALYSE in str(raised.value)

    # The response schema has to let the empty array reach that judgement. It
    # used to reject one as malformed, so the prompt asked for an answer the
    # first validator called invalid: a model that complied spent both attempts
    # being corrected and the run failed over a permitted answer.
    assert analysis_worker._analysis_response_schema('{"analyses": []}') == {
        "analyses": []
    }
    with pytest.raises(WorkerResponseValidationError):
        analysis_worker._analysis_response_schema('{"analyses": "none"}')


def test_a_broken_definition_is_rejected_before_it_is_saved(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data

    def script(user: str) -> dict:
        return {
            "analyses": [
                {
                    "title": "Broken preview",
                    "kind": "python",
                    "spec": {"code": "result = tables['transactions'].group_by('nope').len()"},
                    "note": "Expected to hold across these columns.",
                }
            ]
        }

    _fake_model(monkeypatch, script)
    run = _analysis_run(
        ws,
        command={
            "source": "chat",
            "text": "Analyse the tables",
            "target_refs": ["table:transactions"],
        },
    )
    _drive(ws, run, "analysis.definitions_ready")
    _drive(ws, run, "analysis.executed")

    definition_unit = _stage(run, "analysis.definitions_ready")["units"][0]
    assert definition_unit["status"] == "failed"
    assert "failed local validation" in definition_unit["error"]
    assert _stage(run, "analysis.executed")["units"] == []
    assert not workspaces.load_workspace(ws.id).analyses


def test_one_broken_python_spec_does_not_cost_its_working_siblings(
    workspace_with_data, monkeypatch
):
    """A bug in one generated snippet is not evidence against the rest of the
    same response. This is the regression a self-join bug actually caused: an
    unrelated, valid analytics check on the same frame was lost alongside it,
    because the whole unit failed rather than only the broken proposal."""

    def script(user: str) -> dict:
        return {
            "analyses": [
                {
                    "title": "Duplicate invoice numbers",
                    "kind": "analytics",
                    "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
                    "note": "Reused invoice numbers signal double postings.",
                },
                {
                    "title": "Broken preview",
                    "kind": "python",
                    "spec": {"code": "result = tables['transactions'].group_by('nope').len()"},
                    "note": "Expected to hold across these columns.",
                },
            ]
        }

    _fake_model(monkeypatch, script)
    run = _analysis_run(
        ws := workspace_with_data,
        command={
            "source": "chat",
            "text": "Analyse the tables",
            "target_refs": ["table:transactions"],
        },
    )
    _drive(ws, run, "analysis.definitions_ready")

    definition_unit = _stage(run, "analysis.definitions_ready")["units"][0]
    assert definition_unit["status"] == "succeeded"
    saved = workspaces.load_workspace(ws.id).analyses
    assert [item["title"] for item in saved] == ["Duplicate invoice numbers"]
    assert any(
        "dropped a proposed analysis" in warning and "Broken preview" in warning
        for warning in run["warnings"]
    ), "a proposal that never got saved has to be said out loud, not silently lost"


# --------------------------------------------------------------------------- #
# P8.10 — routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "See the two tables, perform relevant joins and data analysis",
        "Join the tables and show the relationships between tables",
        "Explore the data in this workspace",
        "Analyse the two tables",
    ],
)
def test_data_analysis_requests_route_to_the_analysis_workflow(text):
    resolution = classify_command({"source": "chat", "text": text})
    assert resolution is not None
    assert resolution["route"] == "workflow"
    assert resolution["workflow_definition"] == analysis_workflow.WORKFLOW_ID
    assert resolution["requested_outcomes"] == ["analysis.summarized"]


@pytest.mark.parametrize(
    "text",
    [
        "Pin this result to the dashboard",
        "Rerun the saved analysis for transactions",
        "Delete the duplicate invoices analysis",
    ],
)
def test_isolated_analysis_operations_still_route_to_the_action_runner(text):
    resolution = classify_command({"source": "chat", "text": text})
    assert resolution is not None
    assert resolution["route"] == "action"
    assert resolution["requested_outcomes"] == []


def test_a_materialized_analysis_run_selects_the_analysis_composition(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    _fake_model(monkeypatch)
    run = _analysis_run(ws)

    assert run["engine"] == store.WORKFLOW_ENGINE
    assert run["workflow"]["definition"] == analysis_workflow.WORKFLOW_ID
    assert run["workflow"]["definition_hash"] == analysis_workflow.definition_hash()
    assert run["workflow"]["scope"]["target_refs"] == ["workspace:current"]
    # An analysis run carries no audit planning ledger.
    assert "planning_changes" not in run
    assert run["analysis"] == {"relationships": []}

    scheduler = build_workflow_runner(ws, run, runner.RunHandle(ws.id, run["id"]))
    assert scheduler.registry is capability_registries.ANALYSIS_REGISTRY


# --------------------------------------------------------------------------- #
# P8.11 — integration, reuse, and the phase exit gate
# --------------------------------------------------------------------------- #
def test_full_analysis_run_completes_then_repeats_without_duplicating_work(
    workspace_with_data, monkeypatch
):
    ws = workspace_with_data
    fake = _fake_model(monkeypatch)

    started = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "chat",
            "text": "See the two tables, perform relevant joins and data analysis",
        },
    )
    completed = wait_run(ws, started["id"], timeout=60)

    assert completed["status"] == "completed"
    assert [stage["status"] for stage in completed["workflow"]["stages"]] == [
        "succeeded"
    ] * 6
    fresh = workspaces.load_workspace(ws.id)
    # The run's answer is the memo, written over the results it just recorded
    # and current against them.
    memo = fresh.analysis_summary
    # It opens on what the analysis concluded, not on a heading: a reader who
    # stops after the first paragraph should still have the answer.
    assert not memo["markdown"].startswith("#")
    assert memo["markdown"].split("\n", 1)[0].strip()
    assert f"## {analysis_worker.SUMMARY_SECTIONS[0]}" in memo["markdown"]
    assert memo["run_id"] == started["id"]
    assert memo["basis_sha1"] == analysis_results.summary_basis_digest(fresh)
    assert memo["cited_analysis_ids"]
    assert all(
        any(item["id"] == cited for item in fresh.analyses)
        for cited in memo["cited_analysis_ids"]
    )
    assert [item["name"] for item in fresh.joins] == ["transactions_customers_joined"]
    # One definition unit per scoped frame — both tables and the new join — but
    # five analyses, not six. The script proposes the same duplicate check on
    # every frame it is given, and on the join that check reads only columns
    # originating in ``transactions``: the same computation, so it is dropped
    # rather than saved twice. The join keeps only the analysis that is
    # genuinely its own.
    assert len(fresh.analyses) == 5
    assert [item["table"] for item in fresh.analyses].count(
        "transactions_customers_joined"
    ) == 1
    assert all(item["last_result"]["status"] == "ok" for item in fresh.analyses)
    assert "Analyses executed: 5" in completed["summary_markdown"]
    assert [item["capability"] for item in completed["milestones"]] == [
        "analysis.executed"
    ]
    milestone = completed["milestones"][0]
    assert milestone["headline"] == "Data analysis complete"
    assert next(
        item["value"] for item in milestone["metrics"]
        if item["label"] == "Checks executed"
    ) == 5
    assert "rows" not in milestone and "table_rows" not in milestone
    first_turns = len(fake.calls)
    # One utility-gate turn for the whole scope, one definition turn per scoped
    # frame (both tables and the join), and the single workspace-wide summary
    # turn. The gate is charged once no matter how many pairs it judges, which
    # is why it sits on its own stage rather than inside the join units.
    assert [call["tag"] for call in fake.calls] == [
        "agent:join_utility",
        "agent:analysis_definitions",
        "agent:analysis_definitions",
        "agent:analysis_definitions",
        "agent:analysis_summary",
    ]
    assert first_turns == 5

    repeat = runner.start_command_run(
        # Materialization reads the caller's workspace, which is how a route
        # sees the state a previous run committed.
        workspaces.load_workspace(ws.id),
        "auto",
        {
            "source": "chat",
            "text": "See the two tables, perform relevant joins and data analysis",
        },
    )
    second = wait_run(ws, repeat["id"], timeout=60)

    assert second["status"] == "completed"
    assert second["workflow"]["stages"] == []
    assert set(second["workflow"]["reused_capabilities"]) == set(
        analysis_workflow.DEPENDENCIES
    )
    assert len(fake.calls) == first_turns, "a repeat run must not re-bill the provider"
    unchanged = workspaces.load_workspace(ws.id)
    assert len(unchanged.joins) == 1
    assert len(unchanged.analyses) == 5


def test_analysis_and_audit_requests_use_one_scheduler_with_different_outcomes(
    workspace_with_data, monkeypatch
):
    """Phase 8 exit gate."""

    ws = workspace_with_data
    _fake_model(monkeypatch)

    analysis_run = _analysis_run(ws)
    audit_run = store.new_command_run(
        ws,
        "auto",
        {
            "source": "goal_template",
            "text": "Draft the APM",
            "goal_template": "apm_only",
        },
    )
    assert resolve_route(ws, audit_run) == "workflow"
    audit_run = store.load_run(ws, audit_run["id"])

    analysis_scheduler = build_workflow_runner(
        ws, analysis_run, runner.RunHandle(ws.id, analysis_run["id"])
    )
    audit_scheduler = build_audit_workflow_runner(
        ws, audit_run, runner.RunHandle(ws.id, audit_run["id"])
    )

    assert type(analysis_scheduler) is type(audit_scheduler)
    assert analysis_scheduler.registry is not audit_scheduler.registry
    assert analysis_run["workflow"]["requested_outcomes"] == ["analysis.summarized"]
    assert audit_run["workflow"]["requested_outcomes"] == ["planning.apm_ready"]
    assert analysis_run["workflow"]["definition"] != audit_run["workflow"]["definition"]

    # The scheduler itself stays domain-neutral: it learns either workflow only
    # through an injected registry and validated execution bindings, and knows
    # no capability of either domain by name.
    scheduler_source = inspect.getsource(type(analysis_scheduler)).casefold()
    for capability_id in (
        *analysis_workflow.DEPENDENCIES,
        *audit_workflow.DEPENDENCIES,
    ):
        assert capability_id not in scheduler_source


# --------------------------------------------------------------------------- #
# Analysis identity is the computation, not the frame it was written against
# --------------------------------------------------------------------------- #
def _joined(workspace):
    workspace.add_join(
        {
            "name": "tx_customers",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    return workspaces.load_workspace(workspace.id)


def test_one_computation_has_one_identity_across_a_join_family(workspace_with_data):
    """The same check on a table and on a join built from it is one analysis.

    This is what stops a single invoice date-lag check being saved once per
    frame that can see the invoice columns.
    """
    ws = _joined(workspace_with_data)
    spec = {"test": "duplicates", "params": {"columns": ["invoice_no"]}}

    base = analysis_worker.analysis_semantic_id(
        "analytics",
        "transactions",
        spec,
        join_diagnostics.column_origins(ws, "transactions"),
    )
    joined = analysis_worker.analysis_semantic_id(
        "analytics",
        "tx_customers",
        spec,
        join_diagnostics.column_origins(ws, "tx_customers"),
    )
    assert base == joined

    # A spec reaching across the join is its own computation, not a repeat.
    spanning = analysis_worker.analysis_semantic_id(
        "analytics",
        "tx_customers",
        {"test": "duplicates", "params": {"columns": ["invoice_no", "customer"]}},
        join_diagnostics.column_origins(ws, "tx_customers"),
    )
    assert spanning != base

    # Without provenance the frame name still carries identity, so the two
    # frames disagree — the behaviour every caller had before.
    assert analysis_worker.analysis_semantic_id(
        "analytics", "transactions", spec
    ) != analysis_worker.analysis_semantic_id("analytics", "tx_customers", spec)


def test_definition_context_supplies_the_whole_join_family(workspace_with_data):
    """A proposal can only avoid repeating an analysis it was actually shown."""
    ws = _joined(workspace_with_data)
    ws.add_analysis(
        {
            "title": "Duplicate invoice numbers",
            "kind": "analytics",
            "table": "transactions",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
            "semantic_id": "analysis:already-saved",
        }
    )
    ws = workspaces.load_workspace(ws.id)

    scope = analysis_definition_scope(ws, "tx_customers")
    shown = {
        str(candidate.source["table"])
        for candidate in scope.candidates["current_analyses"]
    }
    assert shown == {"transactions"}, (
        "the join must see the analyses already saved on the tables behind it"
    )
    schema = scope.candidates["target_schema"][0].source
    assert schema["column_origins"]["invoice_no"] == "transactions"
    assert schema["column_origins"]["customer"] == "customers"


def test_a_repeat_from_a_sibling_frame_is_dropped_not_saved(workspace_with_data):
    """The duplicate is dropped and the rest of the response still stands."""
    ws = _joined(workspace_with_data)
    ws.add_analysis(
        {
            "title": "Duplicate invoice numbers",
            "kind": "analytics",
            "table": "transactions",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
            "semantic_id": analysis_worker.analysis_semantic_id(
                "analytics",
                "transactions",
                {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
                join_diagnostics.column_origins(ws, "transactions"),
            ),
        }
    )
    ws = workspaces.load_workspace(ws.id)

    capability = capability_registries.ANALYSIS_REGISTRY.get(
        "analysis.definitions_ready"
    )
    _, bundle = ContextResolver().resolve(
        ws,
        capability,
        {"id": "analysis_definitions:tx_customers"},
        analysis_definition_scope(ws, "tx_customers"),
    )
    request = WorkerRequest(
        worker_id="analysis.definitions",
        capability_id="analysis.definitions_ready",
        unit_id="analysis_definitions:tx_customers",
        context=bundle,
        activity={},
    )

    accepted = analysis_worker.validate_analysis_proposal(
        {
            "analyses": [
                {
                    "title": "Duplicate invoice numbers",
                    "kind": "analytics",
                    "spec": {
                        "test": "duplicates",
                        "params": {"columns": ["invoice_no"]},
                    },
                    "note": "Expected to hold across these columns.",
                },
                {
                    "title": "Duplicates across the join",
                    "kind": "analytics",
                    "spec": {
                        "test": "duplicates",
                        "params": {"columns": ["invoice_no", "customer"]},
                    },
                    "note": "Expected to hold across these columns.",
                },
            ]
        },
        request,
    )
    assert [item["title"] for item in accepted["analyses"]] == [
        "Duplicates across the join"
    ]


def test_a_frame_with_nothing_of_its_own_reports_it_instead_of_erroring(
    workspace_with_data,
):
    """Every proposal repeats a sibling's: a real answer about the data, and
    the binder settles the unit rather than failing the run over it."""
    ws = _joined(workspace_with_data)
    spec = {"test": "duplicates", "params": {"columns": ["invoice_no"]}}
    ws.add_analysis(
        {
            "title": "Duplicate invoice numbers",
            "kind": "analytics",
            "table": "transactions",
            "spec": spec,
            "semantic_id": analysis_worker.analysis_semantic_id(
                "analytics",
                "transactions",
                spec,
                join_diagnostics.column_origins(ws, "transactions"),
            ),
        }
    )
    ws = workspaces.load_workspace(ws.id)

    capability = capability_registries.ANALYSIS_REGISTRY.get(
        "analysis.definitions_ready"
    )
    _, bundle = ContextResolver().resolve(
        ws,
        capability,
        {"id": "analysis_definitions:tx_customers"},
        analysis_definition_scope(ws, "tx_customers"),
    )
    request = WorkerRequest(
        worker_id="analysis.definitions",
        capability_id="analysis.definitions_ready",
        unit_id="analysis_definitions:tx_customers",
        context=bundle,
        activity={},
    )

    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_proposal(
            {
                "analyses": [
                    {
                        "title": "Duplicate invoice numbers",
                        "kind": "analytics",
                        "spec": spec,
                        "note": "One invoice number should appear once.",
                    }
                ]
            },
            request,
        )
    assert analysis_worker.NOTHING_NEW_TO_ANALYSE in str(raised.value)


def _three_hop_workspace() -> workspaces.Workspace:
    """A limit that only meets its transaction two hops away.

    ``orders`` and ``plans`` share no column, exactly as a requisition and an
    approval matrix share none: the limit is held against the customer's plan
    code, so the two can only be tested together through ``customers``.
    """
    ws = workspaces.create_workspace("Three hops")
    orders = pl.DataFrame(
        {
            "order_id": [f"O{index}" for index in range(1, 7)],
            "cust_id": ["C1", "C2", "C3", "C1", "C2", "C3"],
            "amount": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        }
    )
    customers = pl.DataFrame(
        {"id": ["C1", "C2", "C3"], "plan_code": ["P1", "P2", "P1"]}
    )
    plans = pl.DataFrame({"plan_code": ["P1", "P2"], "credit_limit": [25.0, 55.0]})
    ws.add_table("orders.csv", orders.write_csv().encode())
    ws.add_table("customers.csv", customers.write_csv().encode())
    ws.add_table("plans.csv", plans.write_csv().encode())
    return ws


def test_the_join_stage_builds_a_chain_its_first_wave_could_not_name(monkeypatch):
    """The chain unit does not exist until its first hop is committed, so the
    stage has to re-expand rather than settle on the units it started with."""
    ws = _three_hop_workspace()
    _fake_model(monkeypatch)
    run = _analysis_run(ws, text="join these tables and analyse them")

    _drive(ws, run, "data.relationships_inferred")
    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")

    fresh = workspaces.load_workspace(ws.id)
    lineages = {
        join_diagnostics.frame_lineage(fresh, str(item["name"]))
        for item in fresh.joins
    }
    assert frozenset({"orders", "customers", "plans"}) in lineages, (
        "the three-table frame is the only place a limit meets its order"
    )

    chained = next(
        item
        for item in fresh.joins
        if join_diagnostics.frame_lineage(fresh, str(item["name"]))
        == frozenset({"orders", "customers", "plans"})
    )
    frame = fresh.get_frame(str(chained["name"]))
    assert {"amount", "credit_limit"} <= set(frame.columns)
    assert frame.height == 6, "a chain must not multiply the fact rows"
    over = frame.filter(pl.col("amount") > pl.col("credit_limit"))
    assert over.height == 3


def _role_tie_workspace() -> workspaces.Workspace:
    """One table reaching a person dimension through two equally-perfect roles."""
    ws = workspaces.create_workspace("Role tie")
    requisitions = pl.DataFrame(
        {
            "requisition_id": [f"R{index}" for index in range(1, 7)],
            "requested_by_id": ["S1", "S2", "S3", "S1", "S2", "S3"],
            "approved_by_id": ["S3", "S3", "S1", "S2", "S1", "S2"],
            "amount": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        }
    )
    staff = pl.DataFrame(
        {"staff_id": ["S1", "S2", "S3"], "job_title": ["CFO", "Head", "Analyst"]}
    )
    ws.add_table("requisitions.csv", requisitions.write_csv().encode())
    ws.add_table("staff.csv", staff.write_csv().encode())
    return ws


def test_auto_mode_applies_a_tied_role_join_and_names_what_it_passed_over(monkeypatch):
    """Auto mode is unattended: leaving the pair unjoined would delete the frame
    every downstream analysis needs, so it proceeds — and says what it chose
    between, because the two roles do not mean the same thing.

    The choice is the utility gate's to make now, and only one route to a pair
    survives it. What the run owes the reader is unchanged: the role it did not
    join has to be named, or an analysis of approvals reads like an analysis of
    requests.
    """
    ws = _role_tie_workspace()
    _fake_model(monkeypatch)
    run = _analysis_run(ws, text="join these tables and analyse them")

    _drive(ws, run, "data.relationships_inferred")
    record = run["analysis"]["relationships"][0]
    assert len(record["strong"]) == 2, "both roles should diagnose identically"
    assert not join_diagnostics.decisive(record["strong"])

    _drive(ws, run, "data.join_utility_ready")
    _drive(ws, run, "data.joins_ready")
    unit = _stage(run, "data.joins_ready")["units"][0]
    assert unit["status"] == "succeeded"

    fresh = workspaces.load_workspace(ws.id)
    assert len(fresh.joins) == 1
    assert fresh.joins[0]["right_on"] == ["staff_id"]
    assert fresh.joins[0]["left_on"][0] in {"approved_by_id", "requested_by_id"}

    applied = fresh.joins[0]["left_on"][0]
    passed_over = (
        "requested_by_id" if applied == "approved_by_id" else "approved_by_id"
    )
    assert any(
        "related by more than one key" in warning
        and applied in warning
        and passed_over in warning
        for warning in run["warnings"]
    ), "the road not taken has to be visible in the run"


# --------------------------------------------------------------------------- #
# A frame is asked only for work it can actually support
# --------------------------------------------------------------------------- #
def test_proposal_budget_follows_frame_size():
    assert analysis_worker.proposal_budget(4) == analysis_worker.SMALL_FRAME_ANALYSES
    assert analysis_worker.proposal_budget(52) == analysis_worker.MODEST_FRAME_ANALYSES
    assert analysis_worker.proposal_budget(118) == analysis_worker.MAX_PROPOSED_ANALYSES
    # An undeclared row count must not silently shrink the contract.
    assert analysis_worker.proposal_budget(None) == analysis_worker.MAX_PROPOSED_ANALYSES


def _definition_request(workspace, target):
    capability = capability_registries.ANALYSIS_REGISTRY.get(
        "analysis.definitions_ready"
    )
    _, bundle = ContextResolver().resolve(
        workspace,
        capability,
        {"id": f"analysis_definitions:{target}"},
        analysis_definition_scope(workspace, target),
    )
    return WorkerRequest(
        worker_id="analysis.definitions",
        capability_id="analysis.definitions_ready",
        unit_id=f"analysis_definitions:{target}",
        context=bundle,
        activity={},
    )


def test_a_populous_frame_keeps_its_population_tests(workspace_with_data):
    """The gate is about frame size, not about the tests being unwelcome."""
    ws = workspace_with_data
    wide = pl.DataFrame(
        {
            "row_id": [f"R{index}" for index in range(200)],
            "amount": [float(index) for index in range(200)],
        }
    )
    ws.add_table("ledger.csv", wide.write_csv().encode())
    ws = workspaces.load_workspace(ws.id)

    tool = analysis_worker._analysis_submission_tool(_definition_request(ws, "ledger"))
    branches = next(
        item
        for item in tool["function"]["parameters"]["properties"]["analyses"]["items"]["oneOf"]
        if item["properties"]["kind"]["enum"] == ["analytics"]
    )["properties"]["spec"]["oneOf"]
    allowed = {item["properties"]["test"]["enum"][0] for item in branches}
    # Both are population tests the small-frame gate withholds. They are asserted
    # rather than `stratify`, which a populous frame also does not receive — but
    # for the unrelated reason that a stratification is `descriptive` and the
    # workflow proposes no descriptive test at any size.
    assert "outliers" in allowed and "threshold_check" in allowed
    assert tool["function"]["parameters"]["properties"]["analyses"]["maxItems"] == (
        analysis_worker.MAX_PROPOSED_ANALYSES
    )


def _spec_branches(workspace, target: str) -> dict[str, dict]:
    tool = analysis_worker._analysis_submission_tool(
        _definition_request(workspace, target)
    )
    analytics_branch = next(
        item
        for item in tool["function"]["parameters"]["properties"]["analyses"]["items"][
            "oneOf"
        ]
        if item["properties"]["kind"]["enum"] == ["analytics"]
    )
    return {
        item["properties"]["test"]["enum"][0]: item
        for item in analytics_branch["properties"]["spec"]["oneOf"]
    }


def test_the_workflow_proposes_no_descriptive_test(workspace_with_data):
    """A stratification, a period trend and a drawn sample have no exception
    concept, so an autonomous run proposing one spends a definition turn and an
    execution to produce something nothing downstream can conclude from.

    The exclusion list is a literal in the workflow definition — a workflow may
    import only graph primitives — so this is what keeps it honest against the
    registry's own classification.
    """
    descriptive = analytics.ids_with_signal(analytics.SIGNAL_DESCRIPTIVE)
    assert descriptive <= ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS
    assert descriptive.isdisjoint(_spec_branches(workspace_with_data, "transactions"))


def _lookup_pairs(ws, target: str) -> set[tuple[str, tuple[str, ...]]]:
    referential = _spec_branches(ws, target)["referential"]
    branches = referential["properties"]["params"].get("oneOf") or [
        referential["properties"]["params"]
    ]
    return {
        (
            branch["properties"]["lookup_table"]["enum"][0],
            tuple(branch["properties"]["lookup_column"]["enum"]),
        )
        for branch in branches
    }


def test_a_reconciliation_may_only_name_a_supplied_lookup(workspace_with_data):
    """``lookup_column`` means nothing except relative to a chosen table, so the
    two are emitted as one branch per candidate. A single pair of independent
    enums would admit every cross product, most of which name a column the
    chosen table does not have."""
    ws = workspace_with_data
    ws.add_table(
        "invoice_master.csv",
        pl.DataFrame(
            {
                "invoice_no": [1001, 1002, 1003, 1005, 1006],
                "region": ["north", "north", "south", "south", "north"],
            }
        ).write_csv().encode(),
    )
    ws = workspaces.load_workspace(ws.id)

    pairs = _lookup_pairs(ws, "transactions")
    # ``invoice_no`` is distinct on every row and populated throughout; ``region``
    # repeats, so it identifies nothing and is not offered as a key.
    assert ("invoice_master", ("invoice_no",)) in pairs
    # A frame never reconciles against itself, nor against a frame it contains.
    assert all(table != "transactions" for table, _ in pairs)


def test_a_reconciliation_is_only_offered_where_the_target_references_it(
    workspace_with_data,
):
    """Offering every table costs a schema branch each — carrying the target's
    whole column enum — and invites a reconciliation between two populations that
    were never meant to meet. Narrowed by the same schema-only name affinity the
    join diagnostics use before they measure anything.
    """
    ws = workspace_with_data
    ws.add_table(
        "po_master.csv",
        pl.DataFrame({"po_number": ["PO1", "PO2", "PO3"]}).write_csv().encode(),
    )
    ws = workspaces.load_workspace(ws.id)

    offered = {table for table, _ in _lookup_pairs(ws, "transactions")}
    # `transactions.cust_id` is a reference to `customers.id`, so that pair is
    # worth offering. Nothing in the fixture names a purchase order at all.
    assert "customers" in offered
    assert "po_master" not in offered


def test_a_reconciliation_is_not_offered_with_nothing_to_reconcile_against(
    transactions_df,
):
    """A workspace of one table has no lookup, and a test offered with an empty
    enum is a test the model cannot write — so it is withheld rather than
    offered and then rejected after a turn has been spent on it."""
    ws = workspaces.create_workspace("Single table")
    ws.add_table("transactions.csv", transactions_df.write_csv().encode())
    ws = workspaces.load_workspace(ws.id)
    assert "referential" not in _spec_branches(ws, "transactions")


def test_a_duplicate_key_may_not_be_qualified_by_what_it_already_identifies(
    workspace_with_data,
):
    """The false clear this exists for: duplicates keyed on (VENDOR_ID,
    VENDOR_INVOICE_NUMBER) reports "No duplicate keys found" over a population
    holding two invoice numbers reused across different vendors, because the
    vendor id is the very dimension the test was supposed to look across."""
    ws = workspace_with_data
    ws.add_table(
        "invoices.csv",
        pl.DataFrame(
            {
                "vendor_id": ["V1", "V2"],
                "vendor_invoice_number": ["X1", "X1"],
                "invoice_amount": [10.0, 10.0],
            }
        ).write_csv().encode(),
    )
    ws = workspaces.load_workspace(ws.id)
    request = _definition_request(ws, "invoices")

    def propose(columns: list[str]) -> dict:
        return {
            "analyses": [
                {
                    "title": "Duplicate vendor invoice references",
                    "kind": "analytics",
                    "note": "A vendor invoice number should identify one invoice.",
                    "spec": {"test": "duplicates", "params": {"columns": columns}},
                }
            ]
        }

    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_proposal(
            propose(["vendor_id", "vendor_invoice_number"]), request
        )
    assert "only narrows" in str(raised.value)

    # The corrected key, and a legitimate composite that shares no subject, both
    # pass: the rule is about one column restating another, not about breadth.
    for columns in (["vendor_invoice_number"], ["vendor_id", "invoice_amount"]):
        accepted = analysis_worker.validate_analysis_proposal(propose(columns), request)
        assert accepted["analyses"][0]["spec"]["params"]["columns"] == columns


def test_a_joined_frame_may_not_restate_one_side_s_own_work(workspace_with_data):
    """A join exists to relate its sides. An analytics spec there that reads
    only one of them computes what that table alone computes, so it belongs on
    the table — this is what filled joined frames with their sides' analyses."""
    ws = _joined(workspace_with_data)
    request = _definition_request(ws, "tx_customers")

    accepted = analysis_worker.validate_analysis_proposal(
        {
            "analyses": [
                {
                    # Both columns originate in transactions.
                    "title": "Duplicate invoice and customer id",
                    "kind": "analytics",
                    "spec": {
                        "test": "duplicates",
                        "params": {"columns": ["invoice_no", "cust_id"]},
                    },
                    "note": "Expected to hold across these columns.",
                },
                {
                    # Spans the join: an invoice column and a customer column.
                    "title": "Duplicate invoice per customer name",
                    "kind": "analytics",
                    "spec": {
                        "test": "duplicates",
                        "params": {"columns": ["invoice_no", "customer"]},
                    },
                    "note": "Expected to hold across these columns.",
                },
            ]
        },
        request,
    )
    assert [item["title"] for item in accepted["analyses"]] == [
        "Duplicate invoice per customer name"
    ]


def test_a_base_table_may_of_course_use_its_own_columns(workspace_with_data):
    """The cross-side rule applies to joined frames only."""
    request = _definition_request(workspace_with_data, "transactions")
    accepted = analysis_worker.validate_analysis_proposal(
        {
            "analyses": [
                {
                    "title": "Duplicate invoice numbers",
                    "kind": "analytics",
                    "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
                    "note": "Expected to hold across these columns.",
                }
            ]
        },
        request,
    )
    assert len(accepted["analyses"]) == 1


def test_a_joined_frame_with_only_one_sided_proposals_settles(workspace_with_data):
    ws = _joined(workspace_with_data)
    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_proposal(
            {
                "analyses": [
                    {
                        "title": "Duplicate invoice numbers",
                        "kind": "analytics",
                        "spec": {
                            "test": "duplicates",
                            "params": {"columns": ["invoice_no"]},
                        },
                        "note": "Expected to hold across these columns.",
                    }
                ]
            },
            _definition_request(ws, "tx_customers"),
        )
    assert analysis_worker.NOTHING_NEW_TO_ANALYSE in str(raised.value)
    assert "only one of the tables this frame joins" in str(raised.value)


# --------------------------------------------------------------------------- #
# analysis.summarized
# --------------------------------------------------------------------------- #
FENCE = "```"


def _embed(analysis_id: str, kind: str = "chart") -> str:
    return f"{FENCE}embed\nanalysis: {analysis_id}\nas: {kind}\ncaption: x\n{FENCE}\n"


def _summary_request(workspace):
    """A resolved worker request for the summary unit of this workspace."""
    capability = next(
        item
        for item in capability_registries.ANALYSIS_REGISTRY.all()
        if item.id == "analysis.summarized"
    )
    unit = {
        "id": "analysis_summary",
        "kind": "analysis_summary",
        "parent_refs": ["analysis_summary:current"],
    }
    _manifest, bundle = ContextResolver().resolve(
        workspace, capability, unit, analysis_summary_scope(workspace)
    )
    return WorkerRequest(
        worker_id="analysis.summary",
        capability_id="analysis.summarized",
        unit_id="analysis_summary",
        context=bundle,
        activity={},
    )


def _memo(sections=None, embeds: str = "", findings: str = "Text.") -> str:
    # The lead paragraph is part of the contract: a memo that opens straight
    # into a heading has not said what the analysis concluded.
    body = "The population reconciles and one issue is worth following up.\n\n"
    for section in analysis_worker.SUMMARY_SECTIONS if sections is None else sections:
        if section == analysis_worker.FINDINGS_SECTION:
            body += f"## {section}\n{findings}\n{embeds}"
        else:
            body += f"## {section}\nText.\n"
    return body


def _saved(ws, title: str, spec: dict, **extra) -> dict:
    analysis = ws.add_analysis(
        {"kind": "analytics", "table": "transactions", "title": title, "spec": spec, **extra}
    )
    analysis_results.execute_and_record(ws, analysis["id"])
    return analysis


DUPLICATES = {"test": "duplicates", "params": {"columns": ["invoice_no"]}}
COMPLETENESS = {"test": "completeness", "params": {"columns": ["amount"]}}


def test_the_summary_reads_every_saved_analysis_not_only_the_workflows(
    workspace_with_data,
):
    """A procedure the auditor wrote is part of the EDA the memo describes."""
    ws = workspace_with_data
    agent_owned = _saved(ws, "Duplicates", DUPLICATES, agent_run_id="run-1")
    auditor_owned = _saved(ws, "Auditor completeness check", COMPLETENESS)

    scope = analysis_summary_scope(workspaces.load_workspace(ws.id))
    supplied = {
        candidate.metadata["analysis_id"]
        for candidate in scope.candidates["analysis_results"]
    }
    assert supplied == {agent_owned["id"], auditor_owned["id"]}


def test_the_summary_is_shown_the_rows_a_procedure_flagged(workspace_with_data):
    """The memo can name a flagged item because the rows reach the worker."""
    ws = workspace_with_data
    _saved(ws, "Duplicate invoices", DUPLICATES)

    scope = analysis_summary_scope(workspaces.load_workspace(ws.id))
    # 'duplicates' concludes a failure, so its rows are in the failure half.
    flagged = scope.candidates["analysis_exceptions"]
    assert len(flagged) == 1
    payload = flagged[0].source
    assert payload["exception_count"] == 2
    assert 1006 in [
        row[payload["columns"].index("invoice_no")] for row in payload["rows"]
    ]


def test_the_summary_is_shown_what_a_procedure_did_not_only_its_title(
    workspace_with_data,
):
    """A title is authored text; the spec is what ran.

    Without the parameters a duplicates test over two join columns and a
    genuine cross-system mismatch check are indistinguishable, and a date-lag
    test's direction — which decides whether its flagged rows are the exception
    or the population — is invisible.
    """
    ws = workspace_with_data
    lag = _saved(
        ws,
        "Detect backdating",
        {"test": "date_lag", "params": {"from_date": "tx_date", "to_date": "tx_date"}},
    )

    scope = analysis_summary_scope(workspaces.load_workspace(ws.id))
    supplied = {
        candidate.metadata["analysis_id"]: candidate.source
        for candidate in scope.candidates["analysis_results"]
    }
    assert supplied[lag["id"]]["test"] == "date_lag"
    assert supplied[lag["id"]]["parameters"] == {
        "from_date": "tx_date",
        "to_date": "tx_date",
    }


def test_the_summary_is_shown_a_python_procedures_code_and_outcome_policy(
    workspace_with_data,
):
    """Under ``exception_rows`` an unfiltered frame is counted as exceptions.

    The memo can only decline to report that count as exceptions if it can see
    that the code never narrowed anything.
    """
    ws = workspace_with_data
    unfiltered = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions",
            "title": "Check for mismatched amounts",
            "spec": {"code": "result = tables['transactions']"},
            "note": "Expected to hold across these columns.",
            "outcome_policy": {"mode": "exception_rows"},
        }
    )
    analysis_results.execute_and_record(ws, unfiltered["id"])

    scope = analysis_summary_scope(workspaces.load_workspace(ws.id))
    supplied = next(
        candidate.source
        for candidate in scope.candidates["analysis_results"]
        if candidate.metadata["analysis_id"] == unfiltered["id"]
    )
    assert supplied["code"] == "result = tables['transactions']"
    assert supplied["outcome_policy"] == {"mode": "exception_rows"}
    # Every row of the declared frame came back, and the rate says so.
    assert supplied["exception_count"] == supplied["population"]
    assert supplied["exception_rate"] == 1.0
    assert supplied["exception_rate_of"] == "population"


def test_a_long_procedures_code_is_bounded_rather_than_dropped(workspace_with_data):
    ws = workspace_with_data
    code = "# " + "x" * (adapters.MAX_SUMMARY_CODE_CHARACTERS + 500)
    analysis = ws.add_analysis(
        {
            "kind": "python",
            "table": "transactions",
            "title": "Long",
            "spec": {"code": code},
            "note": "Expected to hold across these columns.",
        }
    )
    supplied = adapters._summary_result_projection(
        workspaces.load_workspace(ws.id),
        next(item for item in ws.analyses if item["id"] == analysis["id"]),
    )
    assert len(supplied["code"]) < len(code)
    assert supplied["code"].endswith("truncated")


def test_the_summary_is_shown_the_keys_each_join_was_built_on(workspace_with_data):
    """Join keys are exactly what a memo invents when it is not given them."""
    ws = workspace_with_data
    ws.add_join(
        {
            "name": "transactions_customers_joined",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )

    scope = analysis_summary_scope(workspaces.load_workspace(ws.id))
    joins = {candidate.source["frame"]: candidate.source for candidate in scope.candidates["table_joins"]}
    supplied = joins["transactions_customers_joined"]
    assert supplied["left_on"] == ["cust_id"] and supplied["right_on"] == ["id"]
    assert supplied["how"] == "left"
    # And how well they matched: every transaction found a customer, and the
    # customer side is unique on its key, so the join multiplied nothing.
    assert supplied["match"]["match_rate"] == 1.0
    assert supplied["match"]["unmatched_keys"] == 0
    assert supplied["match"]["row_multiplication"] == 1.0


def test_a_join_that_will_not_resolve_still_supplies_its_definition(
    workspace_with_data,
):
    """An unmeasured join reads as unmeasured, not as a join that never existed."""
    ws = workspace_with_data
    ws.add_join(
        {
            "name": "transactions_customers_joined",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    fresh = workspaces.load_workspace(ws.id)

    def broken(name, *args, **kwargs):
        raise workspaces.WorkspaceError("gone")

    fresh.get_frame = broken
    supplied = adapters.analysis_join_candidates(fresh)[0].source
    assert supplied["left_on"] == ["cust_id"]
    assert "match" not in supplied


def test_a_flagged_count_travels_with_what_it_is_a_count_of(workspace_with_data):
    """``row_count`` sizes a result frame and was never a denominator."""
    ws = workspace_with_data
    weekend = _saved(
        ws,
        "Weekend activity",
        {"test": "weekend_activity", "params": {"date_column": "tx_date"}},
    )

    scope = analysis_summary_scope(workspaces.load_workspace(ws.id))
    supplied = next(
        candidate.source
        for candidate in scope.candidates["analysis_results"]
        if candidate.metadata["analysis_id"] == weekend["id"]
    )
    assert supplied["population"] == 6
    assert supplied["tested"] == 6
    assert supplied["not_tested"] == 0
    assert supplied["exception_rate_of"] == "tested"
    # The summary frame is one row per weekday, which is not six of anything.
    assert supplied["row_count"] != supplied["population"]


def test_rows_a_procedure_could_not_evaluate_are_reported_as_untested(
    workspace_with_data,
):
    """A row dropped for an unparseable date is a row no conclusion covers."""
    ws = workspace_with_data
    ws.add_table(
        "partial.csv",
        b"ref,started,finished\nR1,2026-01-05,2026-01-09\nR2,,\nR3,2026-02-01,2026-01-02\n",
    )
    lag = ws.add_analysis(
        {
            "kind": "analytics",
            "table": "partial",
            "title": "Lag",
            "spec": {
                "test": "date_lag",
                "params": {"from_date": "started", "to_date": "finished"},
            },
            "note": "Expected to hold across these columns.",
        }
    )
    analysis_results.execute_and_record(ws, lag["id"])

    supplied = adapters._summary_result_projection(
        workspaces.load_workspace(ws.id),
        next(
            item
            for item in workspaces.load_workspace(ws.id).analyses
            if item["id"] == lag["id"]
        ),
    )
    assert supplied["population"] == 3
    assert supplied["tested"] == 2
    assert supplied["not_tested"] == 1
    assert supplied["exception_count"] == 1
    assert supplied["exception_rate"] == 0.5


def test_failures_get_their_own_budget_so_warnings_cannot_crowd_them_out():
    """Deterministic selection orders by reference, not by severity.

    A single flagged-rows source would drop whichever procedures sorted late by
    analysis ID, which could cut a backdating failure while keeping a weekend
    activity warning. The two declared sources are what prevent that.
    """
    spec = PRESETS.compile("analysis.summary")
    sources = {source.id: source for source in spec.sources}
    assert "analysis_exceptions" in sources and "analysis_anomalies" in sources
    for source_id in ("analysis_exceptions", "analysis_anomalies"):
        assert [item.kind for item in sources[source_id].representations] == [
            "analysis_exception_rows"
        ]
    assert (
        sources["analysis_exceptions"].budget.max_characters
        > sources["analysis_anomalies"].budget.max_characters
    )


def test_the_summary_context_permits_flagged_rows_and_nothing_wider():
    """The memo's row access is the declared exception rows, not table rows."""
    spec = PRESETS.compile("analysis.summary")
    assert spec.privacy.allow_analysis_exception_rows is True
    assert spec.privacy.allow_analysis_results is True
    # The general row permission stays denied: this capability may see rows a
    # declared test flagged, never an arbitrary slice of a table.
    assert spec.privacy.allow_table_rows is False


def test_a_summary_citing_an_unsupplied_procedure_is_rejected(workspace_with_data):
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    with pytest.raises(WorkerResponseValidationError, match="not a supplied procedure"):
        analysis_worker.validate_analysis_summary(
            {"markdown": _memo(embeds=_embed("A-NOTREAL"))}, request
        )

    # The same memo citing a procedure it was actually shown is accepted, and
    # the accepted proposal reports what it cited.
    accepted = analysis_worker.validate_analysis_summary(
        {"markdown": _memo(embeds=_embed(analysis["id"], "exception_table"))}, request
    )
    assert accepted["cited_analysis_ids"] == [analysis["id"]]


def test_every_embed_error_offers_deleting_the_block(workspace_with_data):
    """A repair told only that a value is wrong reaches for a replacement.

    One live repair, told an embed named no supplied procedure, substituted an
    id that did not exist under a kind that did not exist — three lines above
    the correct embed for the same result. Deleting the block was always a way
    to comply; nothing in the message said so.
    """
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    broken = {
        "unsupplied id": _embed("A-NOTREAL", "exception_table"),
        "coined kind": _embed(analysis["id"], "spreadsheets"),
        "no analysis named": f"{FENCE}embed\nas: exception_table\n{FENCE}\n",
        "embedded twice": (
            _embed(analysis["id"], "exception_table")
            + _embed(analysis["id"], "exception_table")
        ),
    }
    for label, embeds in broken.items():
        with pytest.raises(WorkerResponseValidationError) as raised:
            analysis_worker.validate_analysis_summary(
                {"markdown": _memo(embeds=embeds)}, request
            )
        assert "delete the" in "; ".join(raised.value.errors), label


def test_a_memo_written_twice_says_so_rather_than_reporting_a_jumble(
    workspace_with_data,
):
    """The defect is that it is two documents, not that one is misordered."""
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    drafted_twice = _memo() + _memo()
    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_summary({"markdown": drafted_twice}, request)

    errors = "; ".join(raised.value.errors)
    assert "the memo is written more than once" in errors
    for section in analysis_worker.SUMMARY_SECTIONS:
        assert f"'{section}' appears 2 times" in errors
    # One defect, one error. Four sections duplicated is a memo written twice,
    # and four messages saying so would crowd the repair turn's budget.
    assert len([item for item in raised.value.errors if "appears" in item]) == 1
    # The ordering rule stays quiet: against a doubled skeleton it always fails,
    # and it would send the repair off to rearrange the wrong thing.
    assert "out of order" not in errors


def test_the_embed_example_in_the_prompt_cannot_be_copied_into_a_valid_memo():
    """A placeholder left in a block is the shape of a value, not a value.

    The prompt's example was once ``analysis: <analysis_id>`` inside a real
    embed fence — formally identical to valid output, and duly emitted as
    output. Whatever it shows now must not be mistaken for something to keep.
    """
    prompt = analysis_worker.ANALYSIS_SUMMARY_SYSTEM

    assert "<analysis_id>" not in prompt
    assert "illustration" in prompt
    assert "delete it" in prompt
    # And it still shows the exact grammar, which is why the example is there.
    for field in ("analysis:", "as:", "caption:"):
        assert field in prompt


def test_a_summary_missing_a_section_is_rejected(workspace_with_data):
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    partial = _memo(sections=analysis_worker.SUMMARY_SECTIONS[:-1])
    with pytest.raises(WorkerResponseValidationError, match="Further work required"):
        analysis_worker.validate_analysis_summary({"markdown": partial}, request)


def test_an_embed_fence_survives_the_response_unwrapper():
    """A memo carrying fences must not be mistaken for a fenced document."""
    memo = _memo(embeds=_embed("A-1"))
    unwrapped = analysis_worker._summary_response_schema(memo)["markdown"]
    assert f"{FENCE}embed" in unwrapped
    assert analysis_worker.parse_embeds(unwrapped) == [
        {"analysis": "A-1", "as": "chart", "caption": "x"}
    ]


def test_sections_out_of_order_are_rejected(workspace_with_data):
    """The populations frame the findings; the limits qualify them."""
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    shuffled = list(analysis_worker.SUMMARY_SECTIONS)
    shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
    with pytest.raises(WorkerResponseValidationError, match="out of order"):
        analysis_worker.validate_analysis_summary(
            {"markdown": _memo(sections=shuffled)}, request
        )


def test_a_summary_that_opens_on_a_heading_is_rejected(workspace_with_data):
    """A reader who stops after the first paragraph should have the answer."""
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    headless = _memo().split("\n\n", 1)[1]
    with pytest.raises(WorkerResponseValidationError, match="no paragraph"):
        analysis_worker.validate_analysis_summary({"markdown": headless}, request)

    # A title and a rule are punctuation, not an opening.
    decorated = f"# Exploratory Data Analysis Summary\n\n---\n\n{headless}"
    with pytest.raises(WorkerResponseValidationError, match="no paragraph"):
        analysis_worker.validate_analysis_summary({"markdown": decorated}, request)


def test_a_lead_paragraph_survives_the_response_unwrapper():
    """The unwrapper used to discard everything before the first heading."""
    memo = _memo()
    unwrapped = analysis_worker._summary_response_schema(memo)["markdown"]
    assert unwrapped.startswith("The population reconciles")
    # A conversational hand-off is still dropped: that is what the rule was for.
    preambled = analysis_worker._summary_response_schema(
        "Here is the summary:\n\n" + memo
    )["markdown"]
    assert preambled.startswith("The population reconciles")


def test_identifier_density_is_editorial_not_a_line_based_contract(
    workspace_with_data,
):
    """Physical Markdown lines do not decide whether a memo is valid."""
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    dumped = _memo(
        findings=(
            "Vendor IDs do not match. Flagged rows include INV2024091, "
            "INV2024032, INV2024024, INV2024021, INV2024068, INV2024029."
        )
    )
    assert analysis_worker.validate_analysis_summary({"markdown": dumped}, request)

    # Naming the two or three that carry the finding is exactly what is wanted.
    named = _memo(
        findings=(
            "Two invoices carry the exposure: INV2024091 at 120,000,000 and "
            "INV2024032 at 115,268,880."
        )
    )
    assert analysis_worker.validate_analysis_summary({"markdown": named}, request)


def test_summary_validation_reports_independent_errors_together(workspace_with_data):
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))
    malformed = (
        "## What the analysis found\nText.\n"
        "```embed\nanalysis: A-NOTREAL\nas: mystery\ncaption: x\n```\n"
    )

    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_summary({"markdown": malformed}, request)

    errors = raised.value.errors
    assert any("missing section 'Data received" in item for item in errors)
    assert any("no paragraph" in item for item in errors)
    assert any("not a supplied procedure" in item for item in errors)
    assert any("unknown kind" in item for item in errors)


def test_summary_repair_replays_the_rejected_draft(workspace_with_data):
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))
    rejected = _memo(sections=analysis_worker.SUMMARY_SECTIONS[:-1])
    corrected = _memo()

    class Gateway:
        def __init__(self):
            self.responses = [rejected, corrected]
            self.calls = []

        def complete(
            self,
            system,
            user,
            activity=None,
            *,
            attempt=1,
            conversation=None,
        ):
            self.calls.append(
                {
                    "system": system,
                    "user": user,
                    "activity": activity,
                    "attempt": attempt,
                    "conversation": conversation,
                }
            )
            return self.responses.pop(0)

    gateway = Gateway()
    result = analysis_worker.WORKERS.execute(request, gateway)

    assert result.repaired is True
    assert gateway.calls[0]["conversation"] is None
    repair = gateway.calls[1]
    assert repair["attempt"] == 2
    assert repair["conversation"][1] == {
        "role": "assistant",
        "content": rejected,
    }
    assert "preserving all otherwise-valid wording" in repair["conversation"][2][
        "content"
    ]
    assert "Further work required" in repair["conversation"][2]["content"]


def test_citing_several_procedures_is_not_a_run_of_identifiers(workspace_with_data):
    """An auditor-saved procedure's id is a bare hex string, not an invoice."""
    ws = workspace_with_data
    ids = [
        _saved(ws, f"Check {index}", COMPLETENESS)["id"] for index in range(5)
    ]
    request = _summary_request(workspaces.load_workspace(ws.id))

    cited = _memo(findings="Completeness held across the cycle (" + ", ".join(ids) + ").")
    assert analysis_worker.validate_analysis_summary({"markdown": cited}, request)


def test_a_citation_missing_its_prefix_is_not_a_free_pass(workspace_with_data):
    """The gate used to fall hardest on the references that were right.

    An exception-bearing procedure cited as ``A-1F2E3D4C`` was held to the
    embed rule; the same procedure cited as ``1F2E3D4C`` was held to nothing,
    because the scan matches ids exactly and saw no citation there at all. The
    reader got the unresolvable one.
    """
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES, id="A-1F2E3D4C")
    request = _summary_request(workspaces.load_workspace(ws.id))

    stripped = _memo(findings="Invoices are duplicated (1F2E3D4C).")
    with pytest.raises(WorkerResponseValidationError, match="mangled copy") as raised:
        analysis_worker.validate_analysis_summary({"markdown": stripped}, request)

    assert analysis["id"] in "; ".join(raised.value.errors)
    # The message names the procedure the reference was reaching for, because a
    # repair turn told only that something is unresolvable has to guess.
    assert f"cite '{analysis['id']}' exactly" in "; ".join(raised.value.errors)
    # Reported, but not fatal at the end of the budget: the finding it sits next
    # to is still true, and a memo with a broken pointer beats no memo.
    assert raised.value.partial is not None


def test_a_hand_saved_procedure_is_caught_when_its_bare_id_is_mistyped(
    workspace_with_data,
):
    """No prefix to lose does not mean no way to get the reference wrong."""
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES, id="ab12cd34ef")
    request = _summary_request(workspaces.load_workspace(ws.id))

    assert "-" not in analysis["id"]
    truncated = _memo(findings="Invoices are duplicated (ab12cd34).")
    with pytest.raises(WorkerResponseValidationError, match="mangled copy"):
        analysis_worker.validate_analysis_summary({"markdown": truncated}, request)


def test_a_truncated_citation_is_rejected_rather_than_read_as_absent(
    workspace_with_data,
):
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES, id="A-1F2E3D4C")
    request = _summary_request(workspaces.load_workspace(ws.id))

    for mangled in ("A-DF4??", "A-052…", "A-NOTREAL"):
        memo = _memo(findings=f"Something was found ({mangled}).")
        with pytest.raises(WorkerResponseValidationError, match="names no supplied"):
            analysis_worker.validate_analysis_summary({"markdown": memo}, request)


def test_business_identifiers_are_never_read_as_broken_citations(workspace_with_data):
    """The check is anchored to the supplied register, not to a guessed shape.

    A memo names requisitions, purchase orders, invoices, vendors and buyers by
    their own identifiers, and several of those carry prefixes and hyphens. A
    rule that read those as procedure references would reject every memo that
    did its job.
    """
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES, id="A-1F2E3D4C")
    request = _summary_request(workspaces.load_workspace(ws.id))

    memo = _memo(
        findings=(
            "Requisition REQ2024081 and REQ-2024081 ran against PO20251017 and "
            "PO-20251017 for vendor V1010, invoice INV2024035, buyers B001 to "
            "B006, staff 1002, on 2024-08-19 for 120,000,000. The GRN_ID_LINK "
            "and MAX_APPROVAL_AMOUNT columns are null on 22 rows. This A-list "
            "vendor has a front-end, purchase-to-payment, 3-sigma profile, and "
            "labels A-1 through A-9 come from the client's own manual."
        )
    )

    assert analysis_worker.validate_analysis_summary({"markdown": memo}, request)


def test_an_exception_result_supporting_a_finding_is_embedded_for_the_writer(
    workspace_with_data,
):
    """The requirement comes from result metadata, not digits in the prose.

    Which table belongs under which finding is not a judgment call — the
    citation names it and the kind is fixed — so the validator places it rather
    than spending the one repair turn asking for it back.
    """
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    unlinked = _memo(findings=f"Invoices are duplicated ({analysis['id']}).")
    repaired = analysis_worker.validate_analysis_summary(
        {"markdown": unlinked}, request
    )

    placed = analysis_worker.parse_embeds(repaired["markdown"])
    assert [(item["analysis"], item["as"]) for item in placed] == [
        (analysis["id"], "exception_table")
    ]
    assert repaired["cited_analysis_ids"] == [analysis["id"]]
    # Under the paragraph that argues it, not appended to the document.
    lines = repaired["markdown"].splitlines()
    finding = next(
        index for index, line in enumerate(lines) if "Invoices are duplicated" in line
    )
    assert lines[finding + 2] == f"{FENCE}embed"

    linked = _memo(
        findings=f"Invoices are duplicated ({analysis['id']}).",
        embeds=_embed(analysis["id"], "exception_table"),
    )
    accepted = analysis_worker.validate_analysis_summary({"markdown": linked}, request)
    assert accepted["markdown"] == linked.strip()


def test_an_exception_result_embedded_under_the_wrong_kind_goes_back_to_the_writer(
    workspace_with_data,
):
    """Replacing a placed embed would overrule prose the validator cannot read."""
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    charted = _memo(
        findings=f"Invoices are duplicated ({analysis['id']}).",
        embeds=_embed(analysis["id"], "chart"),
    )
    with pytest.raises(WorkerResponseValidationError, match="not exception_table"):
        analysis_worker.validate_analysis_summary({"markdown": charted}, request)


def test_digits_do_not_turn_a_zero_exception_result_into_an_embed_requirement(
    workspace_with_data,
):
    ws = workspace_with_data
    analysis = _saved(ws, "Completeness", COMPLETENESS)
    assert analysis["last_result"]["exception_count"] == 0
    request = _summary_request(workspaces.load_workspace(ws.id))
    memo = _memo(
        findings=(
            f"The 2026 completeness procedure found no missing amount values "
            f"({analysis['id']})."
        )
    )

    assert analysis_worker.validate_analysis_summary({"markdown": memo}, request)


def test_a_procedure_argued_in_both_findings_and_reliance_is_rejected(
    workspace_with_data,
):
    """The restatement the older three-section skeleton produced by construction."""
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    restated = _memo(
        findings=f"Duplicate keys are the issue ({analysis['id']}).",
        embeds=_embed(analysis["id"], "exception_table"),
    ).replace(
        f"## {analysis_worker.RELIANCE_SECTION}\nText.",
        f"## {analysis_worker.RELIANCE_SECTION}\nAlso worth noting ({analysis['id']}).",
    )
    with pytest.raises(WorkerResponseValidationError, match="argued in both"):
        analysis_worker.validate_analysis_summary({"markdown": restated}, request)


def test_a_result_that_established_nothing_cannot_be_a_finding(workspace_with_data):
    """The backstop for anything that reaches the memo already saturated.

    Definition-time screening stops these being created, but an auditor may
    have saved one by hand, and a frame can change under a saved procedure.
    """
    ws = workspace_with_data
    ws.add_table("orders.csv", ORDERS_CSV)
    saturated = ws.add_analysis({
        "kind": "analytics",
        "table": "orders",
        "title": "Deliveries before their order",
        "spec": {
            "test": "date_lag",
            "params": {"from_date": "delivered", "to_date": "ordered"},
        },
    })
    analysis_results.execute_and_record(ws, saturated["id"])
    request = _summary_request(workspaces.load_workspace(ws.id))

    as_finding = _memo(
        findings=f"Deliveries systematically precede their orders ({saturated['id']}).",
        embeds=_embed(saturated["id"], "exception_table"),
    )
    with pytest.raises(WorkerResponseValidationError, match="cannot be a finding"):
        analysis_worker.validate_analysis_summary({"markdown": as_finding}, request)

    # Reported where it belongs — as a limit on the work — it is accepted.
    as_limit = _memo().replace(
        f"## {analysis_worker.RELIANCE_SECTION}\nText.",
        f"## {analysis_worker.RELIANCE_SECTION}\n"
        f"One procedure ({saturated['id']}) flagged its whole population and is "
        "not treated as a finding.",
    )
    assert analysis_worker.validate_analysis_summary({"markdown": as_limit}, request)


def test_a_memo_wrong_only_in_shape_survives_a_spent_repair_budget(
    workspace_with_data,
):
    """What the run that prompted this lost: a whole memo, over punctuation.

    The partial changes nothing while repair turns remain. It decides only what
    happens at the end of them, and a misshapen memo beats the nothing that got
    committed when the document was discarded outright.
    """
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    restated = _memo(
        findings=f"Duplicate keys are the issue ({analysis['id']}).",
        embeds=_embed(analysis["id"], "exception_table"),
    ).replace(
        f"## {analysis_worker.RELIANCE_SECTION}\nText.",
        f"## {analysis_worker.RELIANCE_SECTION}\nAlso worth noting ({analysis['id']}).",
    )
    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_summary({"markdown": restated}, request)

    assert raised.value.partial is not None
    assert raised.value.partial["markdown"] == restated.strip()
    assert raised.value.partial["cited_analysis_ids"] == [analysis["id"]]


def test_a_memo_that_says_something_untrue_is_never_salvaged(workspace_with_data):
    """A memo in the audit file is read as the work, so a gap beats a falsehood."""
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))

    invented = _memo(
        findings=f"Duplicate keys are the issue ({analysis['id']}).",
        embeds=_embed(analysis["id"], "exception_table") + _embed("A-NOTREAL", "chart"),
    )
    with pytest.raises(WorkerResponseValidationError) as raised:
        analysis_worker.validate_analysis_summary({"markdown": invented}, request)

    assert "not a supplied procedure" in "; ".join(raised.value.errors)
    assert raised.value.partial is None


def test_the_skeleton_is_finding_shaped(workspace_with_data):
    """No section invites an inventory of procedures or a register of joins."""
    assert analysis_worker.SUMMARY_SECTIONS == (
        "Data received and its limitations",
        "What the analysis found",
        "How far these results can be relied on",
        "Further work required",
    )
    prompt = analysis_worker.ANALYSIS_SUMMARY_SYSTEM
    assert "organise \"What the analysis found\" by *issue*" in prompt
    # The record of work performed survives as one coverage table, not as a
    # section that asks for a list of procedures.
    assert "coverage table" in prompt.lower()


def test_the_summary_goes_stale_when_the_result_set_moves(workspace_with_data):
    """Readiness is content-hashed against the results the memo was written from."""
    ws = workspace_with_data
    _saved(ws, "Duplicates", DUPLICATES)
    capability = next(
        item
        for item in capability_registries.ANALYSIS_REGISTRY.all()
        if item.id == "analysis.summarized"
    )

    fresh = workspaces.load_workspace(ws.id)
    assert capability.readiness(fresh, {}).state == "missing"

    fresh.analysis_summary.update(
        {
            "markdown": "## Data received and population characteristics\nText.\n",
            "basis_sha1": analysis_results.summary_basis_digest(fresh),
            "generated_at": "2026-08-07T00:00:00+00:00",
            "run_id": "run-1",
        }
    )
    fresh.save()
    assert capability.readiness(workspaces.load_workspace(ws.id), {}).state == "satisfied"

    later = workspaces.load_workspace(ws.id)
    later.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Completeness",
            "spec": COMPLETENESS,
        }
    )
    readiness = capability.readiness(workspaces.load_workspace(ws.id), {})
    assert readiness.state == "missing"
    assert "predates the current results" in readiness.reasons[0]


def test_the_memo_endpoint_reports_staleness(workspace_with_data):
    ws = workspace_with_data
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    fresh = workspaces.load_workspace(ws.id)
    fresh.analysis_summary.update(
        {
            "markdown": "## Data received and population characteristics\nText.\n",
            "cited_analysis_ids": [analysis["id"]],
            "basis_sha1": analysis_results.summary_basis_digest(fresh),
            "generated_at": "2026-08-07T00:00:00+00:00",
            "run_id": "run-1",
        }
    )
    fresh.save()

    client = TestClient(create_app())
    body = client.get(f"/api/workspaces/{ws.id}/analyses/memo").json()
    assert body["stale"] is False
    assert body["cited_analysis_ids"] == [analysis["id"]]

    # Re-running a procedure that concludes the same thing does *not* stale the
    # memo. The basis hashes what the procedures found, not when they were run,
    # so pressing Run cannot invalidate prose that still describes reality.
    analysis_results.execute_and_record(workspaces.load_workspace(ws.id), analysis["id"])
    assert client.get(f"/api/workspaces/{ws.id}/analyses/memo").json()["stale"] is False

    # A procedure whose conclusion is not in the memo does stale it.
    later = workspaces.load_workspace(ws.id)
    added = later.add_analysis(
        {
            "kind": "analytics",
            "table": "transactions",
            "title": "Completeness",
            "spec": COMPLETENESS,
        }
    )
    analysis_results.execute_and_record(later, added["id"])
    assert client.get(f"/api/workspaces/{ws.id}/analyses/memo").json()["stale"] is True
