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

import polars as pl
import pytest

from app import analytics, dashboard, llm, workspaces
from app.agent import narration, runner, store, workflow
from app.agent import capabilities as capability_registries
from app.agent import joins as join_diagnostics
from app.agent.analysis_execution import build_analysis_workflow_runner
from app.agent.audit_execution import build_audit_workflow_runner
from app.agent.capabilities import analysis as analysis_capabilities
from app.agent.context import (
    ANALYSIS_WORKFLOW_EXCLUDED_TEST_IDS,
    PRESETS,
    ContextResolver,
    analysis_definition_scope,
)
from app.agent.executors import ExecutorRequest
from app.agent.executors import analysis as analysis_executors
from app.agent.routing import classify_command, resolve_route
from app.agent.workers import analysis as analysis_worker
from app.agent.workers.model import WorkerRequest, WorkerResponseValidationError
from app.agent.workflow_dispatch import build_workflow_runner
from app.agent.workflows import analysis as analysis_workflow
from app.agent.workflows import audit as audit_workflow
from app.workspace_transactions import parent_hashes
from conftest import FakeAgentLLM, wait_run


ANALYSIS_TAG = "agent:analysis_definitions"


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
            },
        ]
    }


def _fake_model(monkeypatch, script=None) -> FakeAgentLLM:
    fake = FakeAgentLLM({ANALYSIS_TAG: script or _scripted_definitions})
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
    scheduler._run_stage(_stage(run, capability_id))
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
        "data.joins_ready": ("data.relationships_inferred",),
        "analysis.definitions_ready": ("data.joins_ready",),
        "analysis.executed": ("analysis.definitions_ready",),
    }
    assert analysis_workflow.FULL_ANALYSIS_OUTCOMES == ["analysis.executed"]
    assert analysis_workflow.outcomes_for_template("data_analysis") == [
        "analysis.executed"
    ]

    registry = capability_registries.build_analysis_registry()
    assert registry.closure(["analysis.executed"]) == [
        "data.relationships_inferred",
        "data.joins_ready",
        "analysis.definitions_ready",
        "analysis.executed",
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
    # A receipt proves the guarded commit, and no model was involved.
    assert unit["receipt_sidecar"]["unit_id"] == unit["id"]
    assert unit["proposal_sidecar"]["unit_id"] == unit["id"]
    assert fake.calls == []


def test_auto_mode_materializes_the_top_ranked_moderate_join(monkeypatch):
    ws = _moderate_pair_workspace()
    _fake_model(monkeypatch)
    run = _analysis_run(ws)
    _drive(ws, run, "data.relationships_inferred")

    record = run["analysis"]["relationships"][0]
    assert record["strong"] == []
    assert record["moderate"], "the 90% match should be a moderate candidate"

    _drive(ws, run, "data.joins_ready")
    unit = _stage(run, "data.joins_ready")["units"][0]
    assert unit["status"] == "succeeded"
    fresh = workspaces.load_workspace(ws.id)
    assert len(fresh.joins) == 1
    assert fresh.joins[0]["left_on"] == ["order_ref"]
    assert fresh.joins[0]["right_on"] == ["order_ref"]
    assert any(
        "Auto-selected the top-ranked join candidate" in warning
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
    outliers = next(
        item
        for item in spec_branches
        if item["properties"]["test"]["enum"] == ["outliers"]
    )
    amount_columns = outliers["properties"]["params"]["properties"]["column"]["enum"]
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
                    },
                    {
                        "title": "Unsafe code",
                        "kind": "python",
                        "spec": {"code": "import os\nresult = 1"},
                    },
                    {
                        "title": "Wrong column",
                        "kind": "analytics",
                        "spec": {
                            "test": "duplicates",
                            "params": {"columns": ["not_a_column"]},
                        },
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
        "input_sha1",
        "result_sha1",
    }
    assert len(result["stats"]) <= analysis_executors.MAX_RESULT_STATS
    assert "1001" not in json.dumps(result)
    # The definition itself is still a spec that recomputes on demand.
    assert dashboard.compute_payload(fresh, duplicates)["error"] is None


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
    assert resolution["requested_outcomes"] == ["analysis.executed"]


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
    ] * 4
    fresh = workspaces.load_workspace(ws.id)
    assert [item["name"] for item in fresh.joins] == ["transactions_customers_joined"]
    # One definition unit per scoped frame — both tables and the new join.
    assert len(fresh.analyses) == 6
    assert all(item["last_result"]["status"] == "ok" for item in fresh.analyses)
    assert "Analyses executed: 6" in completed["summary_markdown"]
    assert [item["capability"] for item in completed["milestones"]] == [
        "analysis.executed"
    ]
    milestone = completed["milestones"][0]
    assert milestone["headline"] == "Data analysis complete"
    assert next(
        item["value"] for item in milestone["metrics"]
        if item["label"] == "Checks executed"
    ) == 6
    assert "rows" not in milestone and "table_rows" not in milestone
    first_turns = len(fake.calls)
    assert first_turns == 3

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
    assert len(unchanged.analyses) == 6


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
    assert analysis_run["workflow"]["requested_outcomes"] == ["analysis.executed"]
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
