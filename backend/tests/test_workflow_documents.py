"""Phase 9 gate: the unified document-analysis workflow.

These tests prove that document extraction, chunk mapping, reduction, and
persistence have exactly one implementation, that it runs on the same
domain-neutral scheduler as the audit and analysis workflows, and that standalone
analysis and the audit-planning dependency reach it through the same capability
declarations.

The invariants that matter most here are billing and durability: a successful
chunk proposal survives a sibling's failure and a restart, a resumed run reuses
it without paying the provider again, and an interrupted persistence commit is
reconciled rather than repeated into a second artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import document_analysis, documents, llm, workspaces
from app.agent import runner, store, workflow
from app.agent import capabilities as capability_registries
from app.agent.capabilities import documents as document_capabilities
from app.agent.context import PRESETS
from app.agent.documents_execution import build_documents_workflow_runner
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors import documents as document_executors
from app.agent.routing import initialize_known_workflow, local_resolution
from app.agent.workers import WORKERS
from app.agent.workers import documents as document_workers
from app.agent.workflow_dispatch import build_workflow_runner
from app.agent.workflows import audit as audit_workflow
from app.agent.workflows import documents as documents_workflow
from app.workspace_transactions import parent_hashes
from conftest import FakeAgentLLM, wait_run


MAP_TAG = "agent:document_analysis_map"
REDUCE_TAG = "agent:document_analysis_reduce"

# Long enough that ``ANALYSIS_CHUNK_CHARACTERS`` splits it into several chunks.
LONG_PAGE = "\n\n".join(
    f"Clause {index}: purchases above the threshold require documented approval "
    "from the finance director before a commitment is made."
    for index in range(1, 900)
)


def _chunk_response(user: str) -> dict:
    source = user.split("RAW SOURCE CHUNK:\n", 1)[-1].strip()
    page = int(user.split("\nPAGE: ", 1)[1].splitlines()[0])
    return {
        "summary_markdown": f"## Summary\n\nApproval is required. [C1]",
        "audit_notes_markdown": "## Notes\n\nObtain evidence of operation. [C1]",
        "citations": [{"id": "C1", "page": page, "excerpt": source[:120].strip()}],
    }


def _reduce_response(_user: str) -> dict:
    return {
        "summary_markdown": "## Consolidated summary\n\nApproval is required. [C1]",
        "audit_notes_markdown": "## Consolidated notes\n\nObtain evidence. [C1]",
    }


def _fake_model(monkeypatch, overrides: dict | None = None) -> FakeAgentLLM:
    fake = FakeAgentLLM(
        {MAP_TAG: _chunk_response, REDUCE_TAG: _reduce_response, **(overrides or {})}
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    return fake


def _document_run(workspace, document_ids, *, action="analyze", mode="auto") -> dict:
    run = store.new_command_run(
        workspace,
        mode,
        {
            "source": "tab_button",
            "text": f"Analyze {len(document_ids)} selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{value}" for value in document_ids],
            "generation_mode": "force" if action == "refresh" else "reuse_existing",
        },
        context={"document_ids": list(document_ids), "action": action},
    )
    assert initialize_known_workflow(workspace, run) is True
    return store.load_run(workspace, run["id"])


def _stage(run: dict, capability_id: str) -> dict:
    return next(
        item
        for item in run["workflow"]["stages"]
        if item["capability"] == capability_id
    )


def _drive(workspace, run: dict, capability_id: str):
    """Run one stage through the scheduler exactly as ``execute()`` would."""

    scheduler = build_documents_workflow_runner(
        workspace, run, runner.RunHandle(workspace.id, run["id"])
    )
    scheduler._refresh()
    scheduler._run_stage(_stage(run, capability_id))
    return scheduler


def _policy_workspace(name: str = "Document workflow", *, text: str | None = None):
    ws = workspaces.create_workspace(name)
    document = documents.add_document(
        ws,
        "Procurement Policy.txt",
        (text or "Purchases require documented approval before commitment.").encode(),
        category="policy",
    )
    return ws, document


# --------------------------------------------------------------------------- #
# P9.2 — workflow definition, grouped composition, and routing scope
# --------------------------------------------------------------------------- #
def test_document_graph_declares_a_linear_closure_with_a_stable_hash():
    assert documents_workflow.DEPENDENCIES == {
        "documents.text_ready": (),
        "documents.analysis_chunks_ready": ("documents.text_ready",),
        "documents.analysis_generated": ("documents.analysis_chunks_ready",),
        "documents.analysis_reviewed": ("documents.analysis_generated",),
    }
    assert documents_workflow.FULL_DOCUMENT_OUTCOMES == [
        "documents.analysis_generated"
    ]
    assert documents_workflow.outcomes_for_template("document_analysis") == [
        "documents.analysis_generated"
    ]

    registry = capability_registries.build_documents_registry()
    assert registry.closure(["documents.analysis_generated"]) == [
        "documents.text_ready",
        "documents.analysis_chunks_ready",
        "documents.analysis_generated",
    ]

    baseline = documents_workflow.definition_hash()
    assert baseline == documents_workflow.definition_hash()
    assert baseline != audit_workflow.definition_hash()


def test_audit_graph_reuses_the_same_document_declarations():
    # The document capabilities are declared once. The audit graph composes the
    # three generation outcomes through a view of the same module, so their
    # edges, readiness, unit expansion, and context cannot drift apart.
    audit_declared = {
        capability.id: capability
        for capability in capability_registries.AUDIT_REGISTRY.all()
    }
    document_declared = {
        capability.id: capability
        for capability in capability_registries.DOCUMENTS_REGISTRY.all()
    }

    for capability_id in documents_workflow.AUDIT_CAPABILITY_IDS:
        audit_capability = audit_declared[capability_id]
        document_capability = document_declared[capability_id]
        assert audit_capability.depends_on == document_capability.depends_on
        assert audit_capability.context == document_capability.context
        assert audit_capability.readiness is document_capability.readiness
        assert audit_capability.expand_units is document_capability.expand_units

    # Auditor review is deliberately absent from the audit graph.
    assert "documents.analysis_reviewed" not in audit_declared
    assert audit_declared["planning.context_ready"].depends_on == (
        "documents.analysis_generated",
    )


def test_document_requests_route_to_the_narrowest_declaring_workflow():
    assert (
        capability_registries.workflow_for_outcomes(["documents.analysis_generated"])
        == documents_workflow.WORKFLOW_ID
    )
    assert (
        capability_registries.workflow_for_outcomes(["planning.apm_ready"])
        == audit_workflow.WORKFLOW_ID
    )

    resolved = local_resolution({"source": "chat", "text": "analyse these documents"})
    assert resolved["route"] == "workflow"
    assert resolved["workflow_definition"] == documents_workflow.WORKFLOW_ID
    assert resolved["requested_outcomes"] == ["documents.analysis_generated"]

    # Isolated document operations stay ActionRunner requests.
    attached = local_resolution({"source": "chat", "text": "attach this file to DT-1"})
    assert attached["route"] == "generic_action"


def test_declared_context_presets_are_registered_and_document_scoped():
    registered = {preset.preset_id for preset in PRESETS.all()}
    assert {"documents.analysis_chunk", "documents.analysis_reduction"} <= registered

    reduction = PRESETS.get("documents.analysis_reduction").spec
    representations = {
        representation.kind
        for source in reduction.sources
        for representation in source.representations
    }
    # The reduction is declared with no raw-source representation at all, so
    # "you receive no raw source" is policy the resolver enforces.
    assert "raw_pages" not in representations
    assert not reduction.privacy.allow_table_rows


# --------------------------------------------------------------------------- #
# P9.3 — deterministic text readiness and extraction
# --------------------------------------------------------------------------- #
def test_unit_expansion_never_extracts_and_readiness_reports_missing_text():
    ws, document = _policy_workspace("Deterministic extraction")
    scope = {"target_refs": [f"document:{document['id']}"]}
    cache = documents.cache_path(ws, document["id"])
    cache.unlink(missing_ok=True)

    registry = capability_registries.build_documents_registry()
    readiness = registry.get("documents.text_ready").readiness(ws, scope)

    assert readiness.state == "missing"
    # Asking what work remains must not perform the work.
    assert not cache.exists()
    assert document_capabilities.chunk_specs(ws, document["id"], scope) == []

    document_executors.extract_text(ws, document["id"])
    refreshed = workspaces.load_workspace(ws.id)

    assert registry.get("documents.text_ready").readiness(refreshed, scope).satisfied
    assert document_capabilities.chunk_specs(refreshed, document["id"], scope)


def test_image_only_documents_settle_for_review_without_failing_the_run(monkeypatch):
    ws = workspaces.create_workspace("Image only document")
    document = documents.add_document(ws, "scan.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    _fake_model(monkeypatch)

    finished = wait_run(ws, runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyze 1 selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{document['id']}"],
        },
        context={"document_ids": [document["id"]], "action": "analyze"},
    )["id"])

    assert finished["status"] == "completed_with_open_items"
    units = [
        unit
        for stage in finished["workflow"]["stages"]
        for unit in stage.get("units") or []
    ]
    assert any(
        unit["status"] == "awaiting_confirmation"
        and unit["error"] == document_executors.DOCUMENT_TEXT_UNAVAILABLE
        for unit in units
    )
    assert document_analysis.generated_record(
        workspaces.load_workspace(ws.id), document["id"]
    ) is None


# --------------------------------------------------------------------------- #
# P9.4 / P9.5 — bounded, stably ordered chunk fan-out with per-chunk proposals
# --------------------------------------------------------------------------- #
def test_multi_chunk_fan_out_is_bounded_and_stably_ordered(monkeypatch):
    ws, document = _policy_workspace("Chunk fan-out", text=LONG_PAGE)
    documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])

    scheduler = _drive(ws, run, "documents.analysis_chunks_ready")
    stage = _stage(run, "documents.analysis_chunks_ready")

    assert len(stage["units"]) > 1
    assert all(unit["status"] == "succeeded" for unit in stage["units"])
    # Concurrency is bounded by the run's declared model concurrency, and the
    # durable unit order is semantic-unit order regardless of completion timing.
    assert [unit["id"] for unit in stage["units"]] == sorted(
        unit["id"] for unit in stage["units"]
    )
    assert len([call for call in fake.calls if call["tag"] == MAP_TAG]) == len(
        stage["units"]
    )
    # Every chunk persisted its own run-local proposal; none committed.
    for unit in stage["units"]:
        payload = scheduler.execution_adapter.sidecars.load_proposal(unit["id"])
        assert payload["proposal"]["chunk_id"]
        assert payload["proposal"]["citations"]
        assert unit["receipt_sidecar"] is None


def test_one_failed_chunk_keeps_its_siblings_proposals(monkeypatch):
    ws, document = _policy_workspace("Chunk failure isolation", text=LONG_PAGE)
    documents.extract_document(ws, document["id"])
    calls = {"count": 0}

    def flaky(user: str) -> dict:
        calls["count"] += 1
        if calls["count"] == 2:
            raise llm.LLMError("the provider dropped this chunk")
        return _chunk_response(user)

    _fake_model(monkeypatch, {MAP_TAG: flaky})
    run = _document_run(ws, [document["id"]])
    scheduler = _drive(ws, run, "documents.analysis_chunks_ready")
    stage = _stage(run, "documents.analysis_chunks_ready")

    statuses = [unit["status"] for unit in stage["units"]]
    assert statuses.count("failed") == 1
    assert statuses.count("succeeded") == len(statuses) - 1
    # All-settled: the successful siblings kept the proposals they paid for.
    for unit in stage["units"]:
        payload = scheduler.execution_adapter.sidecars.load_proposal(unit["id"])
        if unit["status"] == "succeeded":
            assert payload["proposal"]["summary_markdown"]
        else:
            assert payload is None


def test_resume_reuses_chunk_proposals_without_rebilling(monkeypatch):
    ws, document = _policy_workspace("Chunk proposal reuse", text=LONG_PAGE)
    documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])

    _drive(ws, run, "documents.analysis_chunks_ready")
    first = len([call for call in fake.calls if call["tag"] == MAP_TAG])
    assert first > 1

    # Re-open the interrupted stage and drive it again: the persisted proposals
    # still carry a compatible execution identity, so no provider call repeats.
    stage = _stage(run, "documents.analysis_chunks_ready")
    for unit in stage["units"]:
        unit["status"] = "queued"
        unit["attempts"] = 0
        unit["finished_at"] = None
    stage["status"] = "queued"
    store.save_run(ws, run)

    reloaded = store.load_run(ws, run["id"])
    _drive(ws, reloaded, "documents.analysis_chunks_ready")

    assert len([call for call in fake.calls if call["tag"] == MAP_TAG]) == first
    assert all(
        unit["status"] == "succeeded"
        for unit in _stage(reloaded, "documents.analysis_chunks_ready")["units"]
    )


# --------------------------------------------------------------------------- #
# P9.6 — one reduction worker over persisted chunk proposals
# --------------------------------------------------------------------------- #
def test_reduction_consumes_only_chunk_proposals_and_carries_their_citations(
    monkeypatch,
):
    ws, document = _policy_workspace("Reduction inputs", text=LONG_PAGE)
    documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])

    _drive(ws, run, "documents.analysis_chunks_ready")
    _drive(ws, run, "documents.analysis_generated")

    reduce_calls = [call for call in fake.calls if call["tag"] == REDUCE_TAG]
    assert len(reduce_calls) == 1
    supplied = reduce_calls[0]["messages"][-1]["content"]
    assert "GENERATED CHUNK ANALYSES" in supplied
    # The reduction never sees raw source.
    assert "RAW SOURCE CHUNK" not in supplied
    chunk_ids = [
        item["chunk_id"]
        for item in json.loads(supplied.split("GENERATED CHUNK ANALYSES:\n", 1)[1])
    ]
    assert chunk_ids == sorted(chunk_ids)

    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), document["id"]
    )
    assert analysis["generated"]["summary_markdown"].startswith("## Consolidated")
    # Citations came from the map worker's supplied chunks, not from the
    # reduction, which saw no text to cite.
    assert analysis["generated"]["citations"]
    assert all(item["excerpt_hash"] for item in analysis["generated"]["citations"])


def test_a_single_chunk_document_commits_without_a_reduction_turn(monkeypatch):
    ws, document = _policy_workspace("Single chunk document")
    documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])

    _drive(ws, run, "documents.analysis_chunks_ready")
    _drive(ws, run, "documents.analysis_generated")

    # Consolidating one chunk analysis is the identity, so no provider turn is
    # spent on it — but the commit still produced a durable receipt.
    assert [call["tag"] for call in fake.calls] == [MAP_TAG]
    unit = _stage(run, "documents.analysis_generated")["units"][0]
    assert unit["status"] == "succeeded"
    assert unit["receipt_sidecar"]["receipt_hash"]
    assert document_analysis.generated_record(
        workspaces.load_workspace(ws.id), document["id"]
    )


# --------------------------------------------------------------------------- #
# P9.7 — one persistence executor with CAS, receipts, and reconciliation
# --------------------------------------------------------------------------- #
def test_persistence_reconciles_an_interrupted_commit_instead_of_repeating_it(
    monkeypatch,
):
    ws, document = _policy_workspace("Interrupted persistence")
    extracted = documents.extract_document(ws, document["id"])
    _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])
    _drive(ws, run, "documents.analysis_chunks_ready")
    _drive(ws, run, "documents.analysis_generated")

    fresh = workspaces.load_workspace(ws.id)
    committed = document_analysis.generated_record(fresh, document["id"])
    unit = _stage(run, "documents.analysis_generated")["units"][0]
    proposal = {
        "summary_markdown": committed["summary_markdown"],
        "audit_notes_markdown": committed["audit_notes_markdown"],
        "citations": committed["citations"],
        "coverage": committed["coverage"],
    }
    parent_ref = document_executors.document_ref(document["id"])
    request = ExecutorRequest(
        executor_id=document_executors.ANALYSIS_EXECUTOR_ID,
        capability_id="documents.analysis_generated",
        unit_id=unit["id"],
        proposal=proposal,
        expected_revision=fresh.revision - 1,
        expected_parents=parent_hashes(fresh, [parent_ref]),
        activity={},
    )
    target = document_executors.DocumentAnalysisExecutorTarget(
        fresh, run["id"], document["id"], extracted=extracted
    )

    reconciliation = EXECUTORS.reconcile(request, target)

    assert reconciliation.disposition == "already_applied"
    assert reconciliation.result.artifact_refs == (
        document_executors.analysis_ref(document["id"]),
    )
    # A second commit for the same unit never runs, so no duplicate artifact.
    generated = Path(fresh.root) / "Documents" / ".analysis" / document["id"] / "generated"
    assert len(list(generated.glob("*.json"))) == 1


def test_a_replaced_source_document_is_a_conflict_not_an_overwrite(monkeypatch):
    ws, document = _policy_workspace("Replaced source")
    extracted = documents.extract_document(ws, document["id"])
    _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])

    stale_parents = parent_hashes(
        ws, [document_executors.document_ref(document["id"])]
    )
    documents.replace_document(
        ws, document["id"], "Procurement Policy.txt", b"A completely different policy."
    )
    fresh = workspaces.load_workspace(ws.id)

    request = ExecutorRequest(
        executor_id=document_executors.ANALYSIS_EXECUTOR_ID,
        capability_id="documents.analysis_generated",
        unit_id="document_analysis:x",
        proposal={
            "summary_markdown": "Summary.",
            "audit_notes_markdown": "Notes.",
            "citations": [],
            "coverage": {"state": "complete", "analyzed_pages": [1], "omitted_pages": []},
        },
        expected_revision=fresh.revision,
        expected_parents=stale_parents,
        activity={},
    )
    target = document_executors.DocumentAnalysisExecutorTarget(
        fresh, run["id"], document["id"], extracted=extracted
    )

    assert EXECUTORS.reconcile(request, target).disposition == "conflict"


def test_explicit_force_resolves_current_content_as_a_review_candidate(monkeypatch):
    ws, document = _policy_workspace("Explicit regeneration")
    documents.extract_document(ws, document["id"])
    _fake_model(monkeypatch)

    first = wait_run(ws, runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyze 1 selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{document['id']}"],
        },
        context={"document_ids": [document["id"]], "action": "analyze"},
    )["id"])
    assert first["status"] == "completed"
    active = document_analysis.load_index(
        workspaces.load_workspace(ws.id), document["id"]
    )["active_analysis_id"]

    second = wait_run(ws, runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyze 1 selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{document['id']}"],
            "generation_mode": "force",
        },
        context={"document_ids": [document["id"]], "action": "refresh"},
    )["id"])
    assert second["status"] == "completed"

    reloaded = workspaces.load_workspace(ws.id)
    index = document_analysis.load_index(reloaded, document["id"])
    # An explicit regeneration lands as a candidate the auditor accepts; it never
    # silently replaces the active artifact.
    assert index["active_analysis_id"] == active
    assert index["candidate_analysis_id"] not in (None, active)


def test_reuse_does_not_assess_currency(monkeypatch):
    ws, document = _policy_workspace("Reuse without currency")
    documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)

    for _ in range(2):
        finished = wait_run(ws, runner.start_command_run(
            ws,
            "auto",
            {
                "source": "tab_button",
                "text": "Analyze 1 selected document(s).",
                "goal_template": "document_analysis",
                "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
                "target_refs": [f"document:{document['id']}"],
            },
            context={"document_ids": [document["id"]], "action": "analyze"},
        )["id"])
        assert finished["status"] == "completed"

    # The second run reused the existing outcome without a provider call and
    # without asking whether it is still current.
    assert len([call for call in fake.calls if call["tag"] == MAP_TAG]) == 1
    assert finished["workflow"]["reused_capabilities"] == [
        "documents.text_ready",
        "documents.analysis_chunks_ready",
        "documents.analysis_generated",
    ]
    assert all(
        detail["currency_status"] == "not_assessed"
        for detail in finished["workflow"]["reused_capability_details"]
    )


# --------------------------------------------------------------------------- #
# P9.8 — generated and reviewed are separate outcomes
# --------------------------------------------------------------------------- #
def test_generated_is_never_treated_as_reviewed(monkeypatch):
    ws, document = _policy_workspace("Review separation")
    documents.extract_document(ws, document["id"])
    _fake_model(monkeypatch)

    finished = wait_run(ws, runner.start_command_run(
        ws,
        "auto",
        {
            "source": "chat",
            "text": "Review these documents.",
            "requested_outcomes": ["documents.analysis_reviewed"],
            "target_refs": [f"document:{document['id']}"],
        },
    )["id"])
    reloaded = workspaces.load_workspace(ws.id)
    analysis = document_analysis.load_analysis(reloaded, document["id"])

    # The analysis exists, the run is open, and nothing the agent did marked it
    # reviewed.
    assert analysis["generated"]
    assert analysis["review"]["review_state"] == "needs_review"
    assert finished["status"] == "completed_with_open_items"
    review_unit = _stage(finished, "documents.analysis_reviewed")["units"][0]
    assert review_unit["status"] == "awaiting_confirmation"
    assert review_unit["error"] == document_executors.DOCUMENT_REVIEW_REQUIRED

    # Only the auditor's own decision satisfies the outcome.
    document_analysis.patch_review(
        reloaded,
        document["id"],
        {"review_revision": analysis["review_revision"], "review_state": "reviewed"},
    )
    registry = capability_registries.build_documents_registry()
    assert registry.get("documents.analysis_reviewed").readiness(
        workspaces.load_workspace(ws.id),
        {"target_refs": [f"document:{document['id']}"]},
    ).satisfied


def test_status_projection_survives_the_workflow(monkeypatch):
    ws, document = _policy_workspace("Status projection")
    documents.extract_document(ws, document["id"])
    _fake_model(monkeypatch)

    wait_run(ws, runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyze 1 selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{document['id']}"],
        },
        context={"document_ids": [document["id"]], "action": "analyze"},
    )["id"])

    reloaded = workspaces.load_workspace(ws.id)
    entry = next(
        item
        for item in document_analysis.inventory(reloaded)
        if item["id"] == document["id"]
    )

    assert entry["analysis_run_state"] == "idle"
    assert entry["analysis_coverage_state"] == "complete"
    assert entry["analysis_validity_state"] == "current"
    assert entry["analysis_review_state"] == "needs_review"
    # The workflow does not leave a document claiming a resumable leaf run.
    assert entry["analysis_resumable_run_id"] is None


# --------------------------------------------------------------------------- #
# P9.9 / P9.10 — one implementation for both callers
# --------------------------------------------------------------------------- #
def test_standalone_and_planning_use_the_same_workers_and_executor():
    workers = {definition.worker_id for definition in WORKERS.all()}
    executors = {definition.executor_id for definition in EXECUTORS.all()}

    # Exactly one map worker, one reduction worker, and one persistence executor.
    assert {"documents.analysis_chunk", "documents.analysis_reduction"} <= workers
    assert len({name for name in workers if name.startswith("documents.")}) == 2
    assert {name for name in executors if name.startswith("documents.")} == {
        "documents.analysis"
    }


def test_no_document_analysis_runner_or_engine_remains():
    package = Path(store.__file__).parent
    assert not (package / "document_analysis_runner.py").exists()
    assert not hasattr(store, "DOCUMENT_ANALYSIS_ENGINE")
    assert "document_analysis" not in store.ENGINE_BY_RUN_KIND
    with pytest.raises(workspaces.WorkspaceError):
        store.new_run(
            workspaces.create_workspace("Rejected kind"),
            "auto",
            {"document_ids": ["DOC-1"]},
            kind="document_analysis",
        )

    # The former runner's prompts moved into the registered workers.
    from app.agent import prompts

    assert not hasattr(prompts, "DOCUMENT_ANALYSIS_MAP_SYSTEM")
    assert not hasattr(prompts, "DOCUMENT_ANALYSIS_REDUCE_SYSTEM")
    assert document_workers.CHUNK_SYSTEM.startswith(f"[{MAP_TAG}]")
    assert document_workers.REDUCTION_SYSTEM.startswith(f"[{REDUCE_TAG}]")


def test_the_api_endpoints_dispatch_the_document_workflow(monkeypatch):
    ws, document = _policy_workspace("API dispatch")
    documents.extract_document(ws, document["id"])
    _fake_model(monkeypatch)

    from app.routes.document_routes import _analysis_command

    run = _analysis_command(ws, [document["id"]], "analyze", "auto")
    finished = wait_run(ws, run["id"])

    assert run["engine"] == store.WORKFLOW_ENGINE
    assert run["workflow"]["definition"] == documents_workflow.WORKFLOW_ID
    assert finished["status"] == "completed"
    scheduler = build_workflow_runner(
        ws, finished, runner.RunHandle(ws.id, finished["id"])
    )
    assert scheduler.registry is capability_registries.DOCUMENTS_REGISTRY


def test_planning_requests_the_same_declared_outcome(monkeypatch):
    ws, document = _policy_workspace("Planning dependency")
    documents.extract_document(ws, document["id"])
    fake = _fake_model(
        monkeypatch,
        {
            "agent:document_context": {
                "context": {
                    "objective": "Review procurement approvals",
                    "scope": "Procurement approvals",
                }
            }
        },
    )

    finished = wait_run(ws, runner.start_command_run(
        ws, "auto", {"source": "goal_template", "goal_template": "apm_only"}
    )["id"], timeout=30)

    stages = [stage["capability"] for stage in finished["workflow"]["stages"]]
    assert "documents.analysis_generated" in stages
    assert stages.index("documents.analysis_generated") < stages.index(
        "planning.context_ready"
    )
    assert [call["tag"] for call in fake.calls].count(MAP_TAG) == 1
    assert document_analysis.generated_record(
        workspaces.load_workspace(ws.id), document["id"]
    )


def test_an_audit_without_documents_expands_no_document_unit(fake_agent_llm):
    ws = workspaces.create_workspace("Audit without documents")
    run = store.new_command_run(
        ws, "auto", {"source": "goal_template", "goal_template": "apm_only"}
    )
    assert initialize_known_workflow(ws, run) is True
    reloaded = store.load_run(ws, run["id"])

    scheduled = {stage["capability"] for stage in reloaded["workflow"]["stages"]}
    assert not any(
        capability_id.startswith("documents.") for capability_id in scheduled
    )
    assert set(documents_workflow.AUDIT_CAPABILITY_IDS) <= set(
        reloaded["workflow"]["reused_capabilities"]
    )


# --------------------------------------------------------------------------- #
# P9.11 — bounds and privacy
# --------------------------------------------------------------------------- #
def test_the_page_limit_bounds_coverage_and_records_the_omission(monkeypatch):
    ws, document = _policy_workspace("Page bound", text=LONG_PAGE)
    documents.extract_document(ws, document["id"])
    _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])
    # A page bound the run persisted at routing time, so a resume cannot change
    # the coverage the artifact claims because the environment changed.
    run["workflow"]["scope"]["page_limit"] = 1
    store.save_run(ws, run)
    reloaded = store.load_run(ws, run["id"])

    _drive(ws, reloaded, "documents.analysis_chunks_ready")
    _drive(ws, reloaded, "documents.analysis_generated")

    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), document["id"]
    )
    assert analysis["generated"]["coverage"]["state"] == "complete"
    assert analysis["generated"]["coverage"]["analyzed_pages"] == [1]


def test_the_chunk_worker_only_ever_sees_the_chunk_it_was_supplied(monkeypatch):
    ws, document = _policy_workspace("Chunk isolation", text=LONG_PAGE)
    extracted = documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])
    _drive(ws, run, "documents.analysis_chunks_ready")

    chunks = document_analysis.analysis_chunks(extracted)
    map_calls = [call for call in fake.calls if call["tag"] == MAP_TAG]
    assert len(map_calls) == len(chunks)
    for call in map_calls:
        supplied = call["messages"][-1]["content"]
        body = supplied.split("RAW SOURCE CHUNK:\n", 1)[1]
        assert sum(chunk["text"] == body for chunk in chunks) == 1
        # No accumulating generated preamble: chunk units are independent, which
        # is what makes them resumable and safe to run concurrently.
        assert "GENERATED ORIENTATION" not in supplied
        # Internal storage filenames never reach the provider.
        assert document["file"] not in supplied
