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

from io import BytesIO
import json
from pathlib import Path

import pytest
from PIL import Image

from app import cycle_vouching, document_analysis, document_context, documents, llm, workspaces
from app.agent import runner, store, workflow
from app.agent import capabilities as capability_registries
from app.agent.capabilities import documents as document_capabilities
from app.agent.context import PRESETS
from app.agent.context.adapters import (
    document_identity_candidate as document_context_identity,
)
from app.agent.documents_execution import build_documents_workflow_runner
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors import documents as document_executors
from app.agent.routing import classify_command, resolve_route
from app.agent.workers import WORKERS
from app.agent.workers import documents as document_workers
from app.agent.workflow_dispatch import build_workflow_runner
from app.agent.workflows import audit as audit_workflow
from app.agent.workflows import documents as documents_workflow
from app.workspace_transactions import parent_hashes
from conftest import FakeAgentLLM, wait_run


MAP_TAG = "agent:document_analysis_map"
REDUCE_TAG = "agent:document_analysis_reduce"
VOUCHER_TAG = "agent:document_analysis_voucher"

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

    resolved = classify_command({"source": "chat", "text": "analyse these documents"})
    assert resolved["route"] == "workflow"
    assert resolved["workflow_definition"] == documents_workflow.WORKFLOW_ID
    assert resolved["requested_outcomes"] == ["documents.analysis_generated"]

    # Isolated document operations stay ActionRunner requests.
    attached = classify_command({"source": "chat", "text": "attach this file to DT-1"})
    assert attached["route"] == "action"


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


def _image_only_run(mode: str, monkeypatch):
    ws = workspaces.create_workspace(f"Image only document {mode}")
    document = documents.add_document(ws, "scan.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    fake = _fake_model(monkeypatch)
    monkeypatch.setattr(
        llm,
        "model_profile_snapshot",
        lambda: {
            "text": {
                "name": "text",
                "provider": "local",
                "model": "test",
                "capabilities": [],
                "configuration_source": "test",
                "configured": True,
                "base_url": "http://local.test/v1",
                "profile_hash": f"sha256:{'1' * 64}",
                "unavailability_reason": None,
            },
            "vision": {
                "name": "vision",
                "provider": "local",
                "model": "test-vision",
                "capabilities": [],
                "configuration_source": "test",
                "configured": False,
                "base_url": "http://local.test/v1",
                "profile_hash": f"sha256:{'2' * 64}",
                "unavailability_reason": "No vision profile is configured.",
            },
        },
    )

    finished = wait_run(ws, runner.start_command_run(
        ws,
        mode,
        {
            "source": "tab_button",
            "text": "Analyze 1 selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{document['id']}"],
        },
        context={"document_ids": [document["id"]], "action": "analyze"},
    )["id"])
    units = [
        unit
        for stage in finished["workflow"]["stages"]
        for unit in stage.get("units") or []
    ]
    return ws, document, finished, units, fake


def test_permission_mode_settles_an_image_only_document_for_review(monkeypatch):
    ws, document, finished, units, fake = _image_only_run("permission", monkeypatch)

    assert finished["status"] == "completed_with_open_items"
    assert any(
        unit["status"] == "awaiting_confirmation"
        and unit["error"] == document_executors.DOCUMENT_REQUIRES_VISION
        for unit in units
    )
    assert document_analysis.generated_record(
        workspaces.load_workspace(ws.id), document["id"]
    ) is None
    assert fake.calls == []


def test_auto_mode_leaves_an_image_only_document_open_without_vision(monkeypatch):
    """Known lack of vision is an explicit, provider-free open item."""
    ws, document, finished, units, fake = _image_only_run("auto", monkeypatch)

    assert finished["status"] == "completed_with_open_items"
    assert any(
        unit["status"] == "awaiting_confirmation"
        and unit["error"] == document_executors.DOCUMENT_REQUIRES_VISION
        for unit in units
    )
    assert document_analysis.generated_record(
        workspaces.load_workspace(ws.id), document["id"]
    ) is None
    assert fake.calls == []


def test_standalone_png_uses_one_visual_call_and_commits_without_reduction(
    monkeypatch,
):
    output = BytesIO()
    Image.new("RGB", (900, 600), "white").save(output, format="PNG")
    ws = workspaces.create_workspace("Visual document success")
    document = documents.add_document(ws, "org-chart.png", output.getvalue())
    fake = _fake_model(
        monkeypatch,
        {
            "agent:document_analysis_visual_map": {
                "transcription_markdown": (
                    "## Visual transcription\n\nThe chart places Finance under "
                    "the Chief Financial Officer."
                ),
                "summary_markdown": "## Summary\n\nFinance reports to the CFO. [V1]",
                "audit_notes_markdown": (
                    "## Notes\n\nConfirm the reporting relationship with the "
                    "approved organization register. [V1]"
                ),
                "citations": [
                    {
                        "id": "V1",
                        "kind": "visual",
                        "page": 1,
                        "tile_order": 0,
                        "description": "Finance box connected below the CFO box.",
                        "region": {
                            "x": 0.1,
                            "y": 0.1,
                            "width": 0.8,
                            "height": 0.8,
                        },
                    }
                ],
            }
        },
    )
    monkeypatch.setattr(
        llm,
        "model_profile_snapshot",
        lambda: {
            "text": {
                "name": "text",
                "provider": "local",
                "model": "test",
                "capabilities": [],
                "configuration_source": "test",
                "configured": True,
                "base_url": "http://local.test/v1",
                "profile_hash": f"sha256:{'1' * 64}",
                "unavailability_reason": None,
            },
            "vision": {
                "name": "vision",
                "provider": "local",
                "model": "test-vision",
                "capabilities": ["vision"],
                "configuration_source": "test",
                "configured": True,
                "base_url": "http://local.test/v1",
                "profile_hash": f"sha256:{'3' * 64}",
                "unavailability_reason": None,
            },
        },
    )

    finished = wait_run(
        ws,
        runner.start_command_run(
            ws,
            "auto",
            {
                "source": "tab_button",
                "text": "Analyze one visual document.",
                "goal_template": "document_analysis",
                "requested_outcomes": list(
                    documents_workflow.FULL_DOCUMENT_OUTCOMES
                ),
                "target_refs": [f"document:{document['id']}"],
            },
            context={"document_ids": [document["id"]], "action": "analyze"},
        )["id"],
    )

    generated = document_analysis.generated_record(
        workspaces.load_workspace(ws.id), document["id"]
    )
    assert finished["status"] == "completed", (
        finished.get("error"),
        [
            (stage["capability"], unit["status"], unit.get("error"))
            for stage in finished["workflow"]["stages"]
            for unit in stage.get("units") or []
        ],
    )
    assert [call["tag"] for call in fake.calls] == [
        "agent:document_analysis_visual_map"
    ]
    assert generated is not None
    assert generated["vision_used"] is True
    assert generated["derived_text_markdown"].startswith(
        "## Visual transcription"
    )
    assert generated["citations"][0]["evidence_kind"] == "visual"
    assert generated["generation_profiles"][0]["name"] == "vision"

    reused = wait_run(
        ws,
        runner.start_command_run(
            ws,
            "auto",
            {
                "source": "tab_button",
                "text": "Analyze the same visual document.",
                "goal_template": "document_analysis",
                "requested_outcomes": list(
                    documents_workflow.FULL_DOCUMENT_OUTCOMES
                ),
                "target_refs": [f"document:{document['id']}"],
            },
            context={"document_ids": [document["id"]], "action": "analyze"},
        )["id"],
    )
    context = document_context.get_document_context(
        workspaces.load_workspace(ws.id),
        document["id"],
        "pages",
        record_activity=False,
    )
    assert reused["status"] == "completed"
    assert len(fake.calls) == 1
    assert "AI-derived visual transcription" in context["content"]
    assert "Finance under" in context["content"]


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


def test_text_chunk_worker_submits_a_schema_bound_citation_tool_call(monkeypatch):
    ws, document = _policy_workspace("Schema-bound chunk citations")
    documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])

    _drive(ws, run, "documents.analysis_chunks_ready")

    call = next(item for item in fake.calls if item["tag"] == MAP_TAG)
    assert call["tool_choice"]["function"]["name"] == (
        document_workers.CHUNK_SUBMISSION_TOOL
    )
    citation = call["tools"][0]["function"]["parameters"]["properties"]["citations"]
    assert citation["items"]["required"] == ["id", "page", "excerpt"]
    assert citation["items"]["additionalProperties"] is False


def test_text_chunk_accepts_the_legacy_exact_excerpt_alias_after_exact_validation(monkeypatch):
    ws, document = _policy_workspace("Exact excerpt compatibility")
    documents.extract_document(ws, document["id"])

    def legacy_response(user: str) -> dict:
        response = _chunk_response(user)
        response["citations"][0]["exact_excerpt"] = response["citations"][0].pop("excerpt")
        return response

    _fake_model(monkeypatch, {MAP_TAG: legacy_response})
    run = _document_run(ws, [document["id"]])

    _drive(ws, run, "documents.analysis_chunks_ready")

    unit = _stage(run, "documents.analysis_chunks_ready")["units"][0]
    assert unit["status"] == "succeeded"
    proposal = json.loads(
        (
            Path(ws.root)
            / "AgentRuns"
            / run["id"]
            / unit["proposal_sidecar"]["path"]
        ).read_text(encoding="utf-8")
    )
    assert proposal["proposal"]["citations"][0]["excerpt"]


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

    # Two text map profiles (standard and voucher), one visual map worker, one
    # reduction worker, and one persistence executor are shared by standalone and
    # audit callers. The profiles differ only in prompt and response contract;
    # everything downstream of the map — reduction, persistence, reconciliation —
    # is the single shared implementation.
    assert {
        "documents.analysis_chunk",
        "documents.analysis_voucher",
        "documents.analysis_visual_page",
        "documents.analysis_reduction",
    } <= workers
    assert len({name for name in workers if name.startswith("documents.")}) == 4
    assert {name for name in executors if name.startswith("documents.")} == {
        "documents.analysis"
    }


# --------------------------------------------------------------------------- #
# Voucher analysis profile
# --------------------------------------------------------------------------- #
def test_voucher_category_routes_to_the_voucher_profile():
    """A declared voucher is mapped by the structured profile; nothing else is."""

    ws = workspaces.create_workspace("Voucher profile routing")
    policy = documents.add_document(
        ws, "Policy.txt", b"Purchases require documented approval.", category="policy"
    )
    voucher = documents.add_document(
        ws,
        "PV-2025-001.txt",
        b"Payment voucher PV-2025-001 paid PKR 2,390 to Ayesha Khan on 2025-04-13.",
        category="voucher",
    )
    unclassified = documents.add_document(
        ws, "Notes.txt", b"Some background notes.", category="other"
    )
    for entry in (policy, voucher, unclassified):
        document_executors.extract_text(ws, entry["id"])
    ws = workspaces.load_workspace(ws.id)

    assert document_capabilities.analysis_profile(ws, voucher["id"]) == "voucher"
    assert document_capabilities.analysis_profile(ws, policy["id"]) == "standard"
    # Explicit only: an unclassified document is never assumed to be evidence.
    assert document_capabilities.analysis_profile(ws, unclassified["id"]) == "standard"

    kinds = {
        spec["kind"]
        for spec in document_capabilities.analysis_unit_specs(ws, voucher["id"], {})
    }
    assert kinds == {"document_voucher_analysis"}
    assert {
        spec["kind"]
        for spec in document_capabilities.analysis_unit_specs(ws, policy["id"], {})
    } == {"document_chunk_analysis"}


def test_voucher_workflow_persists_registry_backed_reduced_records(monkeypatch):
    reference = cycle_vouching.DEFAULT_REGISTRY.reference("procure_to_pay").to_dict()

    def voucher_response(user: str) -> dict:
        source = user.split("RAW SOURCE CHUNK:\n", 1)[-1].strip()
        page = int(user.split("\nPAGE: ", 1)[1].splitlines()[0])
        chunk_id = user.split("\nCHUNK ID: ", 1)[1].splitlines()[0]
        return {
            "summary_markdown": "Purchase order evidence. [C1]",
            "audit_notes_markdown": "No observations were identified on the face of this record.",
            "citations": [{"id": "C1", "page": page, "excerpt": source}],
            "registry": reference,
            "record_fragments": [
                {
                    # Deterministic envelope fields are deliberately malformed:
                    # semantic validation must replace, not trust, model output.
                    "registry": {"pack_id": "invented"},
                    "chunk_id": "invented-chunk",
                    "page_span": str(page),
                    "record_kind": "procure_to_pay.purchase_order",
                    "classification_evidence": ["C1"],
                    "identifiers": [
                        {
                            "kind": "procure_to_pay.purchase_order_number",
                            "value": {
                                "raw_value": "PO-2025-001",
                                "value": "PO-2025-001",
                                "normalization_status": "normalized",
                                "normalization_error": None,
                                "citation": "C1",
                            },
                        }
                    ],
                    "fields": [
                        {
                            "group": "amounts",
                            # Model-facing aliases are canonicalized locally.
                            "kind": "common.amount.total",
                            "attribute": "raw_value",
                            "value": {
                                "raw_value": "4,800",
                                "value": 4800,
                                "normalization_status": "normalized",
                                "normalization_error": None,
                                "citation": "C1",
                            },
                        }
                    ],
                }
            ],
        }

    ws = workspaces.create_workspace("Registry voucher workflow")
    voucher = documents.add_document(
        ws,
        "PO-2025-001.txt",
        b"Purchase order PO-2025-001 has total PKR 4,800.",
        category="voucher",
    )
    documents.extract_document(ws, voucher["id"])
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})

    finished = wait_run(
        ws,
        runner.start_command_run(
            ws,
            "auto",
            {
                "source": "tab_button",
                "text": "Analyze the selected voucher.",
                "goal_template": "document_analysis",
                "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
                "target_refs": [f"document:{voucher['id']}"],
            },
            context={"document_ids": [voucher["id"]], "action": "analyze"},
        )["id"],
    )
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]
    assert analysis["registry"] == reference
    assert len(analysis["record_fragments"]) == 1
    assert analysis["record_fragments"][0]["registry"] == reference
    assert analysis["record_fragments"][0]["chunk_id"] == "AC-0001"
    assert analysis["record_fragments"][0]["page_span"] == [1, 1]
    assert len(analysis["record_fragments"][0]["fields"]) == 1
    assert analysis["record_fragments"][0]["fields"][0]["kind"] == "total"
    assert analysis["record_fragments"][0]["fields"][0]["attribute"] == "value"
    assert len(analysis["records"]) == 1
    assert analysis["records"][0]["record_kind"] == "procure_to_pay.purchase_order"
    assert analysis["records"][0]["identifiers"][0]["value"]["value"] == "po-2025-001"
    assert analysis["records"][0]["fields"][0]["value"]["value"] == 4800
    assert document_analysis.registry_evidence_records(
        workspaces.load_workspace(ws.id), reference
    )[0]["record_id"] == analysis["records"][0]["record_id"]


GRN_SOURCE = (
    b"GOODS RECEIPT NOTE\nGRN2024004\nPurchase order P02024004\n"
    b"Vendor OfficeSupply Co. (V1022)\nReceipt date 29-Apr -2024\n"
    b"Quantity received 25\nReceipt status Received"
)


def _voucher_run(ws, voucher):
    return wait_run(
        ws,
        runner.start_command_run(
            ws,
            "auto",
            {
                "source": "tab_button",
                "text": "Analyze the selected goods receipt.",
                "goal_template": "document_analysis",
                "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
                "target_refs": [f"document:{voucher['id']}"],
            },
            context={"document_ids": [voucher["id"]], "action": "analyze"},
        )["id"],
    )


def _voucher_workspace(name: str, source: bytes = GRN_SOURCE, filename="GRN2024004.txt"):
    ws = workspaces.create_workspace(name)
    voucher = documents.add_document(ws, filename, source, category="voucher")
    documents.extract_document(ws, voucher["id"])
    return ws, voucher


def _excerpt_citations(source: str) -> list[dict]:
    """One citation per source line, so a fact can cite the line it appears on."""

    return [
        {"id": f"C{index}", "page": 1, "excerpt": line}
        for index, line in enumerate(source.splitlines(), start=1)
        if line.strip()
    ]


def _cited(source: str, needle: str) -> str:
    return next(
        citation["id"]
        for citation in _excerpt_citations(source)
        if needle in citation["excerpt"]
    )


def _value(raw: str, citation: str, status: str = "normalized") -> dict:
    return {"raw_value": raw, "normalization_status": status, "citation": citation}


def _grn_fragment(source: str, fields: list[dict]) -> dict:
    return {
        "record_kind": "procure_to_pay.goods_receipt",
        "classification_evidence": [_cited(source, "GOODS RECEIPT NOTE")],
        "identifiers": [
            {
                "kind": "procure_to_pay.goods_receipt_number",
                "value": _value("GRN2024004", _cited(source, "GRN2024004")),
            }
        ],
        "fields": fields,
    }


def _grn_response(source: str, fields: list[dict], **extra) -> dict:
    return {
        "summary_markdown": "The GRN records receipt of 25 kits. [C1]",
        "audit_notes_markdown": (
            "No observations were identified on the face of this record. [C1]"
        ),
        "citations": _excerpt_citations(source),
        "registry": cycle_vouching.DEFAULT_REGISTRY.reference("procure_to_pay").to_dict(),
        "record_fragments": [_grn_fragment(source, fields)],
        **extra,
    }


def _source_of(user: str) -> str:
    return user.split("RAW SOURCE CHUNK:\n", 1)[-1].split(
        "\n\nYour previous response", 1
    )[0].strip()


def test_voucher_repairs_every_bad_selector_in_one_turn(monkeypatch):
    """One repair turn must be told about every field it has to change.

    Reporting only the first offending field made a response with two bad
    selectors unrepairable inside the single permitted repair attempt: the model
    fixed what it was told about and failed again on the next one.
    """

    seen: list[str] = []

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        if "Your previous response could not be used" in user:
            seen.append(user)
            fields = [
                {
                    "group": "dates",
                    "kind": "receipt_date",
                    "attribute": "value",
                    "value": _value("29-Apr -2024", _cited(source, "Receipt date")),
                },
                {
                    "group": "quantities",
                    "kind": "total",
                    "attribute": "value",
                    "value": _value("25", _cited(source, "Quantity received")),
                },
            ]
        else:
            # Two unregistered selectors at once, plus one that is registered but
            # unavailable on this record kind.
            fields = [
                {
                    "group": "quantities",
                    "kind": "quantity_received",
                    "attribute": "value",
                    "value": _value("25", _cited(source, "Quantity received")),
                },
                {
                    "group": "dates",
                    "kind": "inspection_date",
                    "attribute": "value",
                    "value": _value("29-Apr -2024", _cited(source, "Receipt date")),
                },
                {
                    "group": "amounts",
                    "kind": "net_pay",
                    "attribute": "value",
                    "value": _value("25", _cited(source, "Quantity received")),
                },
            ]
        return _grn_response(source, fields)

    ws, voucher = _voucher_workspace("Multi-error voucher repair")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]

    # Every bad selector is named once, not just the first.
    repair = seen[0]
    assert "quantities.quantity_received" in repair
    assert "dates.inspection_date" in repair
    assert "amounts.net_pay" in repair
    assert "quantities.total.value|raw_value" in repair
    assert "do not substitute an unrelated field" not in repair
    assert "substituting an unrelated field" in repair
    # The previous response goes back so a repair does not have to reconstruct
    # the evidence it already had right.
    assert "YOUR PREVIOUS RESPONSE:" in repair
    assert "Keep every identifier, field, and citation" in repair


def test_voucher_keeps_a_party_name_that_the_record_states(monkeypatch):
    """The pack gap that cost the most in practice: a party name had no home.

    ``common.party.name`` was declared by both packs and offered by no record
    kind, so every voucher naming a vendor spent a repair turn and then dropped
    the name entirely.
    """

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        vendor = _cited(source, "Vendor OfficeSupply Co.")
        response = _grn_response(
            source,
            [
                {
                    "group": "parties",
                    "kind": "name",
                    "attribute": "name",
                    "entry": 0,
                    "value": _value("OfficeSupply Co.", vendor),
                },
                {
                    # Interpretive: the record prints "Vendor" as a label here,
                    # but the role is a reading, not a quote.
                    "group": "parties",
                    "kind": "name",
                    "attribute": "role",
                    "entry": 0,
                    "value": _value("supplier", vendor),
                },
            ],
        )
        # The name and the code beside it are two facts, not a choice.
        response["record_fragments"][0]["identifiers"].append(
            {"kind": "common.vendor_id", "value": _value("V1022", vendor)}
        )
        return response

    ws, voucher = _voucher_workspace("Voucher party name")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    # No repair: the fact the record states is representable on the first turn.
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]
    fields = {
        (field["group"], field["kind"], field["attribute"]): field["value"]
        for field in analysis["records"][0]["fields"]
    }
    assert fields[("parties", "name", "name")]["value"] == "OfficeSupply Co."
    # An interpretive attribute may use a word the record never prints.
    assert fields[("parties", "name", "role")]["value"] == "supplier"
    assert {
        identifier["kind"] for identifier in analysis["records"][0]["identifiers"]
    } == {"procure_to_pay.goods_receipt_number", "common.vendor_id"}


def test_voucher_keeps_repeated_approvals_paired_by_entry(monkeypatch):
    """Reduction groups facts by content, so occurrences need an ordinal.

    Without one, three approvals reduce to three unrelated approvers beside
    three unrelated dates and the pairing the record printed is unrecoverable.
    """

    source_text = (
        b"PURCHASE REQUISITION\nREQ2024009\n"
        b"Verified by Olivia Smith on 02-Apr-2024\n"
        b"Financial approval John Doe on 06-Apr-2024"
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        first = _cited(source, "Verified by")
        second = _cited(source, "Financial approval")
        approvals = [
            ("approver", "Olivia Smith", first, 0),
            ("role", "Verified by", first, 0),
            ("date", "02-Apr-2024", first, 0),
            ("approver", "John Doe", second, 1),
            ("role", "Financial approval", second, 1),
            ("date", "06-Apr-2024", second, 1),
        ]
        return {
            "summary_markdown": "Requisition REQ2024009 carries two approvals. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [
                {
                    "record_kind": "procure_to_pay.purchase_requisition",
                    "classification_evidence": [_cited(source, "PURCHASE REQUISITION")],
                    "identifiers": [
                        {
                            "kind": "procure_to_pay.requisition_number",
                            "value": _value("REQ2024009", _cited(source, "REQ2024009")),
                        }
                    ],
                    "fields": [
                        {
                            "group": "approvals",
                            "kind": "approval",
                            "attribute": attribute,
                            "entry": entry,
                            "value": _value(raw, citation),
                        }
                        for attribute, raw, citation, entry in approvals
                    ],
                }
            ],
        }

    ws, voucher = _voucher_workspace(
        "Voucher approval pairing", source_text, "REQ2024009.txt"
    )
    _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    approvals: dict[int, dict] = {}
    for field in analysis["records"][0]["fields"]:
        if (field["group"], field["kind"]) != ("approvals", "approval"):
            continue
        approvals.setdefault(field["entry"], {})[field["attribute"]] = field["value"][
            "value"
        ]
    assert approvals == {
        0: {
            "approver": "Olivia Smith",
            "role": "Verified by",
            "date": "2024-04-02",
        },
        1: {"approver": "John Doe", "role": "Financial approval", "date": "2024-04-06"},
    }


def test_voucher_keeps_the_currency_of_an_amount(monkeypatch):
    """``currency`` is a registered attribute, so it is its own fact.

    Supplied inside the value envelope it was silently discarded when local code
    rebuilt the envelope from ``raw_value``, leaving a bare number.
    """

    source_text = b"TAX INVOICE\nVINV001\nInvoice amount (PKR) PKR 2,000,000.00"

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        amount = _cited(source, "Invoice amount")
        return {
            "summary_markdown": "Invoice VINV001 for PKR 2,000,000.00. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [
                {
                    "record_kind": "procure_to_pay.vendor_invoice",
                    "classification_evidence": [_cited(source, "TAX INVOICE")],
                    "identifiers": [
                        {
                            "kind": "procure_to_pay.vendor_invoice_number",
                            "value": _value("VINV001", _cited(source, "VINV001")),
                        }
                    ],
                    "fields": [
                        {
                            "group": "amounts",
                            "kind": "total",
                            "attribute": "value",
                            "value": _value("PKR 2,000,000.00", amount),
                        },
                        {
                            "group": "amounts",
                            "kind": "total",
                            "attribute": "currency",
                            "value": _value("PKR", amount),
                        },
                    ],
                }
            ],
        }

    ws, voucher = _voucher_workspace(
        "Voucher amount currency", source_text, "VINV001.txt"
    )
    _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    amounts = {
        field["attribute"]: field["value"]["value"]
        for field in analysis["records"][0]["fields"]
        if (field["group"], field["kind"]) == ("amounts", "total")
    }
    assert amounts == {"value": 2000000, "currency": "PKR"}


def test_voucher_repairs_claimed_normalized_value_that_has_wrong_type(monkeypatch):
    """A ``normalized`` claim the field's type cannot read is a selector mistake.

    A value the record itself prints malformed still commits as ``invalid``
    evidence; what is refused is claiming a well-formed value that is not one.
    """

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        status = _cited(source, "Receipt status")
        if "Your previous response could not be used" in user:
            assert "cannot be normalized for it" in user
            fields = [
                {
                    "group": "statuses",
                    "kind": "status",
                    "attribute": "value",
                    "value": _value("Received", status),
                }
            ]
        else:
            fields = [
                {
                    "group": "dates",
                    "kind": "receipt_date",
                    "attribute": "value",
                    "value": _value("Received", status),
                }
            ]
        return _grn_response(source, fields)

    ws, voucher = _voucher_workspace("Typed voucher repair")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]
    assert [field["kind"] for field in analysis["records"][0]["fields"]] == ["status"]
    assert analysis["records"][0]["fields"][0]["value"]["value"] == "Received"


def test_voucher_commits_a_malformed_source_value_as_invalid_evidence(monkeypatch):
    """A defect the record itself carries is evidence, not a repair trigger."""

    source_text = b"GOODS RECEIPT NOTE\nGRN2024004\nReceipt date 31-Feb-2024"

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        return _grn_response(
            source,
            [
                {
                    "group": "dates",
                    "kind": "receipt_date",
                    "attribute": "value",
                    "value": _value(
                        "31-Feb-2024", _cited(source, "Receipt date"), status="invalid"
                    ),
                }
            ],
        )

    ws, voucher = _voucher_workspace("Invalid voucher date", source_text)
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]
    field = analysis["records"][0]["fields"][0]
    assert field["value"]["normalization_status"] == "invalid"
    assert field["value"]["raw_value"] == "31-Feb-2024"
    assert field["value"]["value"] is None
    assert field["value"]["normalization_error"]


def test_voucher_rejects_a_value_that_is_not_in_the_excerpt_it_cites(monkeypatch):
    """Citing a surviving excerpt is not the same as being grounded in one."""

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        if "Your previous response could not be used" in user:
            assert "cite the line the value sits on" in user
            citation = _cited(source, "Receipt status")
        else:
            # The value is real, but anchored to an unrelated line.
            citation = _cited(source, "GOODS RECEIPT NOTE")
        return _grn_response(
            source,
            [
                {
                    "group": "statuses",
                    "kind": "status",
                    "attribute": "value",
                    "value": _value("Received", citation),
                }
            ],
        )

    ws, voucher = _voucher_workspace("Voucher excerpt grounding")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]


def test_voucher_rejects_a_citation_excerpt_absent_from_the_chunk(monkeypatch):
    """A dropped citation is reported, not silently removed.

    ``validate_analysis_map`` discards an excerpt it cannot find. For the
    narrative that is tolerable; for a structured fact it removes the anchor the
    fact depends on and the fragment is then rejected for ungrounded evidence
    without ever saying which excerpt was wrong.
    """

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        citations = _excerpt_citations(source)
        if "Your previous response could not be used" in user:
            assert "does not appear\nverbatim" in user or "does not appear" in user
        else:
            # Two source lines joined into one excerpt: the exact defect seen in
            # the procurement run.
            citations = citations + [
                {"id": "CX", "page": 1, "excerpt": "GOODS RECEIPT NOTE GRN2024004"}
            ]
        return _grn_response(
            source,
            [
                {
                    "group": "statuses",
                    "kind": "status",
                    "attribute": "value",
                    "value": _value("Received", _cited(source, "Receipt status")),
                }
            ],
            citations=citations,
        )

    ws, voucher = _voucher_workspace("Voucher citation exactness")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]


def test_voucher_document_with_no_transaction_record_commits_empty_evidence(monkeypatch):
    """An empty ``record_fragments`` array is a truthful answer.

    Demanding at least one fragment demanded a fabricated one from any document
    the voucher category routed here by mistake.
    """

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        return {
            "summary_markdown": "This page carries no transaction record. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [],
        }

    ws, voucher = _voucher_workspace(
        "Voucher without a record", b"COVER SHEET\nAttachments follow.", "cover.txt"
    )
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]
    assert analysis["records"] == []
    assert analysis["record_fragments"] == []
    assert analysis["unresolved_fragments"] == []
    assert analysis["conflicts"] == []
    assert analysis["registry"]["pack_id"] == "procure_to_pay"


def test_an_interpretive_role_never_has_to_be_quoted_from_the_record(monkeypatch):
    """A goods receipt naming its buyer never prints the word "buyer".

    Requiring every value to appear in its excerpt made this unsatisfiable: the
    live run's best extraction — precise per-line citations, both cycle
    identifiers, the OCR typo preserved — was rejected three times over two facts
    no response could have supplied, and one repair attempt could only move the
    citation to another line that also does not contain the word.
    """

    source_text = (
        b"GLOBAL BANK\nGOODS RECEIPT NOTE\nGRN2024004\nPurchase order P02024004\n"
        b"OfficeSupply Co. (V1022)\nReceipt status Received"
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        bank = _cited(source, "GLOBAL BANK")
        supplier = _cited(source, "OfficeSupply Co.")
        return {
            "summary_markdown": "Goods receipt GRN2024004. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [
                {
                    "record_kind": "procure_to_pay.goods_receipt",
                    "classification_evidence": [_cited(source, "GOODS RECEIPT NOTE")],
                    "identifiers": [
                        {
                            "kind": "procure_to_pay.goods_receipt_number",
                            "value": _value("GRN2024004", _cited(source, "GRN2024004")),
                        },
                        {
                            "kind": "procure_to_pay.purchase_order_number",
                            "value": _value(
                                "P02024004", _cited(source, "Purchase order")
                            ),
                        },
                        {
                            "kind": "common.vendor_id",
                            "value": _value("V1022", supplier),
                        },
                    ],
                    "fields": [
                        {
                            "group": "parties",
                            "kind": "name",
                            "attribute": "name",
                            "entry": 0,
                            "value": _value("GLOBAL BANK", bank),
                        },
                        {
                            "group": "parties",
                            "kind": "name",
                            "attribute": "role",
                            "entry": 0,
                            "value": _value("buyer", bank),
                        },
                        {
                            "group": "parties",
                            "kind": "name",
                            "attribute": "name",
                            "entry": 1,
                            "value": _value("OfficeSupply Co.", supplier),
                        },
                        {
                            "group": "parties",
                            "kind": "name",
                            "attribute": "role",
                            "entry": 1,
                            "value": _value("vendor", supplier),
                        },
                        {
                            "group": "statuses",
                            "kind": "status",
                            "attribute": "value",
                            "value": _value(
                                "Received", _cited(source, "Receipt status")
                            ),
                        },
                    ],
                }
            ],
        }

    ws, voucher = _voucher_workspace("Interpretive role", source_text)
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]
    roles = {
        field["entry"]: field["value"]["value"]
        for field in analysis["records"][0]["fields"]
        if field["attribute"] == "role"
    }
    assert roles == {0: "buyer", 1: "vendor"}
    # The typo stays visible and both cycle references survive.
    assert {
        identifier["kind"]: identifier["value"]["raw_value"]
        for identifier in analysis["records"][0]["identifiers"]
    } == {
        "procure_to_pay.goods_receipt_number": "GRN2024004",
        "procure_to_pay.purchase_order_number": "P02024004",
        "common.vendor_id": "V1022",
    }


def test_a_verbatim_attribute_still_has_to_appear_in_its_excerpt(monkeypatch):
    """Exempting interpretation does not exempt quotation."""

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        if "Your previous response could not be used" in user:
            assert "cite the line the value sits on" in user
            citation = _cited(source, "Receipt status")
        else:
            citation = _cited(source, "GOODS RECEIPT NOTE")
        return _grn_response(
            source,
            [
                {
                    "group": "statuses",
                    "kind": "status",
                    "attribute": "value",
                    "value": _value("Received", citation),
                }
            ],
        )

    ws, voucher = _voucher_workspace("Verbatim still enforced")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})

    assert _voucher_run(ws, voucher)["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]


def test_one_whole_chunk_excerpt_cited_everywhere_is_rejected(monkeypatch):
    """A citation is an anchor, and quoting the chunk anchors nothing.

    Every committed document in the live run collapsed to a single citation whose
    excerpt was the entire chunk, because that satisfies "the value appears in
    its excerpt" for free. The rule rewarded useless citations and rejected the
    one document that cited precisely.
    """

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        if "Your previous response could not be used" in user:
            assert "an excerpt must be at most" in user
            assert "points at the part" in user
            citations = _excerpt_citations(source)
            citation = _cited(source, "Receipt status")
        else:
            citations = [{"id": "C1", "page": 1, "excerpt": source}]
            citation = "C1"
        return _grn_response(
            source,
            [
                {
                    "group": "statuses",
                    "kind": "status",
                    "attribute": "value",
                    "value": _value("Received", citation),
                }
            ],
            citations=citations,
        )

    ws, voucher = _voucher_workspace("Whole chunk citation")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]
    assert len(analysis["citations"]) > 1
    assert all(
        len(citation["excerpt"]) <= document_workers.CITATION_EXCERPT_CHARACTERS
        for citation in analysis["citations"]
    )


def test_a_wrapped_source_line_can_still_be_quoted(monkeypatch):
    """The bound is two lines, so a phrase the source wrapped stays citable."""

    source_text = (
        b"GOODS RECEIPT NOTE\nGRN2024004\n"
        b"Business requirement Procurement of New Hire Onboarding Kits to support\n"
        b"approved operational requirements."
    )
    wrapped = (
        "Business requirement Procurement of New Hire Onboarding Kits to support\n"
        "approved operational requirements."
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        citations = [
            {"id": "C1", "page": 1, "excerpt": "GOODS RECEIPT NOTE"},
            {"id": "C2", "page": 1, "excerpt": "GRN2024004"},
            {"id": "C3", "page": 1, "excerpt": wrapped},
        ]
        return _grn_response(
            source,
            [
                {
                    "group": "descriptions",
                    "kind": "description",
                    "attribute": "value",
                    "value": _value(
                        "Procurement of New Hire Onboarding Kits to support approved "
                        "operational requirements.",
                        "C3",
                    ),
                }
            ],
            citations=citations,
        )

    ws, voucher = _voucher_workspace("Wrapped line citation", source_text)
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})

    assert _voucher_run(ws, voucher)["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]


def test_a_cycle_reference_parked_in_a_prose_field_is_rejected(monkeypatch):
    """References are what link records; a description does not link anything.

    The live run filed an invoice's purchase-order and goods-receipt references
    as attachment references and a voucher's purchase reference as a description.
    Every affected document still looked complete, and the cycle graph split into
    two disconnected components.
    """

    source_text = (
        b"PAYMENT VOUCHER\nVoucher reference: INV2024004\n"
        b"Purchase reference P02024004\nGRN reference GRN2024004"
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        purchase = _cited(source, "Purchase reference")
        receipt = _cited(source, "GRN reference")
        identifiers = [
            {
                "kind": "procure_to_pay.payment_voucher_number",
                "value": _value("INV2024004", _cited(source, "Voucher reference")),
            }
        ]
        repairing = "Your previous response could not be used" in user
        if repairing:
            assert "leaves the reference" in user
            assert '"P02024004"' in user and '"GRN2024004"' in user
            assert "belongs in identifiers" in user
            identifiers += [
                {
                    "kind": "procure_to_pay.purchase_order_number",
                    "value": _value("P02024004", purchase),
                },
                {
                    "kind": "procure_to_pay.goods_receipt_number",
                    "value": _value("GRN2024004", receipt),
                },
            ]
            fields = []
        else:
            fields = [
                {
                    "group": "descriptions",
                    "kind": "description",
                    "attribute": "value",
                    "value": _value("Purchase reference P02024004", purchase),
                },
                {
                    "group": "attachments",
                    "kind": "attachment",
                    "attribute": "reference",
                    "value": _value("GRN2024004", receipt),
                },
            ]
        return {
            "summary_markdown": "Payment voucher INV2024004. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [
                {
                    "record_kind": "procure_to_pay.payment_voucher",
                    "classification_evidence": [_cited(source, "PAYMENT VOUCHER")],
                    "identifiers": identifiers,
                    "fields": fields,
                }
            ],
        }

    ws, voucher = _voucher_workspace(
        "Reference in prose", source_text, "INV2024004.txt"
    )
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]
    assert {
        identifier["kind"] for identifier in analysis["records"][0]["identifiers"]
    } == {
        "procure_to_pay.payment_voucher_number",
        "procure_to_pay.purchase_order_number",
        "procure_to_pay.goods_receipt_number",
    }


def test_a_party_code_printed_beside_its_name_is_not_dropped(monkeypatch):
    """``Ethan Smith (1041)`` is a name and a code, not a choice between them.

    Told that a display name is never an identifier value, the live run reported
    the requisition's department, requester, and vendor as party names and
    dropped all three codes, leaving the record with one identifier.
    """

    source_text = (
        b"PURCHASE REQUISITION\nREQ2024009\n"
        b"Requested by Ethan Smith (1041)\nProposed vendor OfficeSupply Co. (V1022)"
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        requester = _cited(source, "Requested by")
        vendor = _cited(source, "Proposed vendor")
        identifiers = [
            {
                "kind": "procure_to_pay.requisition_number",
                "value": _value("REQ2024009", _cited(source, "REQ2024009")),
            }
        ]
        if "Your previous response could not be used" in user:
            assert "leaves the reference" in user
            assert '"1041"' in user and '"V1022"' in user
            identifiers += [
                {"kind": "common.employee_id", "value": _value("1041", requester)},
                {"kind": "common.vendor_id", "value": _value("V1022", vendor)},
            ]
        return {
            "summary_markdown": "Requisition REQ2024009. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [
                {
                    "record_kind": "procure_to_pay.purchase_requisition",
                    "classification_evidence": [
                        _cited(source, "PURCHASE REQUISITION")
                    ],
                    "identifiers": identifiers,
                    "fields": [
                        {
                            "group": "parties",
                            "kind": "name",
                            "attribute": "name",
                            "entry": 0,
                            "value": _value("Ethan Smith", requester),
                        },
                        {
                            "group": "parties",
                            "kind": "name",
                            "attribute": "name",
                            "entry": 1,
                            "value": _value("OfficeSupply Co.", vendor),
                        },
                    ],
                }
            ],
        }

    ws, voucher = _voucher_workspace(
        "Party code beside name", source_text, "REQ2024009.txt"
    )
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]
    assert {
        identifier["kind"] for identifier in analysis["records"][0]["identifiers"]
    } == {
        "procure_to_pay.requisition_number",
        "common.employee_id",
        "common.vendor_id",
    }


def test_prose_that_carries_no_reference_code_is_left_alone(monkeypatch):
    """A date or a plain phrase in a description is not a misplaced reference."""

    source_text = (
        b"GOODS RECEIPT NOTE\nGRN2024004\n"
        b"Description New Hire Onboarding Kits\nInspected 29-Apr-2024. All delivered."
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        return _grn_response(
            source,
            [
                {
                    "group": "descriptions",
                    "kind": "description",
                    "attribute": "value",
                    "value": _value(
                        "New Hire Onboarding Kits", _cited(source, "Description")
                    ),
                },
                {
                    "group": "notes",
                    "kind": "note",
                    "attribute": "value",
                    "value": _value(
                        "Inspected 29-Apr-2024. All delivered.",
                        _cited(source, "Inspected"),
                    ),
                },
            ],
        )

    ws, voucher = _voucher_workspace("Prose without references", source_text)
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})

    assert _voucher_run(ws, voucher)["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]


def test_a_value_wrapped_across_two_source_lines_may_cite_either(monkeypatch):
    """Grounding is about the same text, not about which side is longer.

    A requisition's business requirement wraps mid-phrase. The worker reported
    the whole value and quoted the line it read it from, and requiring the value
    to sit *inside* its excerpt rejected a correct extraction over the choice of
    granularity.
    """

    source_text = (
        b"GOODS RECEIPT NOTE\nGRN2024004\n"
        b"Business requirement Procurement of New Hire Onboarding Kits to support\n"
        b"approved operational requirements."
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        return _grn_response(
            source,
            [
                {
                    "group": "descriptions",
                    "kind": "description",
                    "attribute": "value",
                    "value": _value(
                        "Procurement of New Hire Onboarding Kits to support approved "
                        "operational requirements.",
                        _cited(source, "Business requirement"),
                    ),
                }
            ],
        )

    ws, voucher = _voucher_workspace("Wrapped value one line cited", source_text)
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})

    assert _voucher_run(ws, voucher)["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]


def test_a_value_cited_to_an_unrelated_line_is_still_rejected(monkeypatch):
    """Accepting either direction must not accept a heading above the value."""

    source_text = (
        b"TAX INVOICE\nVINV001\nSupplier\n"
        b"Address Plot 18, Block A, Gulshan-e-Iqbal, Karachi, Pakistan"
    )

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        repairing = "Your previous response could not be used" in user
        citation = _cited(source, "Address" if repairing else "Supplier")
        return {
            "summary_markdown": "Invoice VINV001. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [
                {
                    "record_kind": "procure_to_pay.vendor_invoice",
                    "classification_evidence": [_cited(source, "TAX INVOICE")],
                    "identifiers": [
                        {
                            "kind": "procure_to_pay.vendor_invoice_number",
                            "value": _value("VINV001", _cited(source, "VINV001")),
                        }
                    ],
                    "fields": [
                        {
                            "group": "parties",
                            "kind": "address",
                            "attribute": "value",
                            "value": _value(
                                "Plot 18, Block A, Gulshan-e-Iqbal, Karachi, Pakistan",
                                citation,
                            ),
                        }
                    ],
                }
            ],
        }

    ws, voucher = _voucher_workspace(
        "Address cited to its heading", source_text, "VINV001.txt"
    )
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})

    assert _voucher_run(ws, voucher)["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]


def test_a_repeated_citation_excerpt_is_remapped_not_reported_as_wrong(monkeypatch):
    """Two ids quoting the same line is a duplicate, not a bad quote.

    ``validate_citations`` keeps one citation per (page, excerpt), so the second
    id vanished and the fact citing it was reported as text the page does not
    contain — guidance no response could act on, because the excerpt was right.
    """

    source_text = b"PAYMENT VOUCHER\nINV2024004\nAuthorisations\nSigned Signed\nPAID"

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        citations = _excerpt_citations(source) + [
            # A second id for a line already cited above.
            {"id": "CDUP", "page": 1, "excerpt": "Signed Signed"}
        ]
        return {
            "summary_markdown": "Payment voucher INV2024004. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": citations,
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [
                {
                    "record_kind": "procure_to_pay.payment_voucher",
                    "classification_evidence": [_cited(source, "PAYMENT VOUCHER")],
                    "identifiers": [
                        {
                            "kind": "procure_to_pay.payment_voucher_number",
                            "value": _value("INV2024004", _cited(source, "INV2024004")),
                        }
                    ],
                    "fields": [
                        {
                            "group": "approvals",
                            "kind": "approval",
                            "attribute": "decision",
                            "value": _value("Signed", "CDUP"),
                        }
                    ],
                }
            ],
        }

    ws, voucher = _voucher_workspace(
        "Duplicate citation excerpt", source_text, "INV2024004.txt"
    )
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG]
    # The fact survives, anchored to the citation that was kept.
    surviving = [
        citation["id"]
        for citation in analysis["citations"]
        if citation["excerpt"] == "Signed Signed"
    ]
    assert len(surviving) == 1
    decision = next(
        field
        for field in analysis["records"][0]["fields"]
        if field["attribute"] == "decision"
    )
    assert decision["value"]["citation"] == surviving


def test_a_second_repair_is_available_when_faults_are_independent(monkeypatch):
    """One repair turned recoverable responses into failed documents.

    The profile checks several independent things, and a response that gets one
    wrong commonly gets another wrong elsewhere — or introduces one while fixing
    the first.
    """

    attempts = {"n": 0}

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        attempt = attempts["n"]
        attempts["n"] += 1
        status = _cited(source, "Receipt status")
        if attempt == 0:
            # Fault one: an unregistered selector.
            fields = [
                {
                    "group": "quantities",
                    "kind": "quantity_received",
                    "attribute": "value",
                    "value": _value("25", _cited(source, "Quantity received")),
                }
            ]
        elif attempt == 1:
            # Fixed, but fault two appears: the value is cited to the wrong line.
            fields = [
                {
                    "group": "quantities",
                    "kind": "total",
                    "attribute": "value",
                    "value": _value("25", _cited(source, "Quantity received")),
                },
                {
                    "group": "statuses",
                    "kind": "status",
                    "attribute": "value",
                    "value": _value("Received", _cited(source, "GRN2024004")),
                },
            ]
        else:
            fields = [
                {
                    "group": "quantities",
                    "kind": "total",
                    "attribute": "value",
                    "value": _value("25", _cited(source, "Quantity received")),
                },
                {
                    "group": "statuses",
                    "kind": "status",
                    "attribute": "value",
                    "value": _value("Received", status),
                },
            ]
        return _grn_response(source, fields)

    ws, voucher = _voucher_workspace("Two independent faults")
    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG] * 3
    assert {field["kind"] for field in analysis["records"][0]["fields"]} == {
        "total",
        "status",
    }


def test_voucher_pack_is_constrained_to_the_packs_the_engagement_uses(monkeypatch):
    """Which cycle an engagement audits is not a per-chunk judgement."""

    def voucher_response(user: str) -> dict:
        source = _source_of(user)
        if "Your previous response could not be used" in user:
            assert "is not one this engagement uses" in user
            reference = cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict()
            fragment = _grn_fragment(source, [])
        else:
            reference = cycle_vouching.DEFAULT_REGISTRY.reference("payroll").to_dict()
            fragment = {
                "record_kind": "payroll.payslip",
                "classification_evidence": [_cited(source, "GOODS RECEIPT NOTE")],
                "identifiers": [
                    {
                        "kind": "payroll.payslip_number",
                        "value": _value("GRN2024004", _cited(source, "GRN2024004")),
                    }
                ],
                "fields": [],
            }
        return {
            "summary_markdown": "A goods receipt note. [C1]",
            "audit_notes_markdown": (
                "No observations were identified on the face of this record. [C1]"
            ),
            "citations": _excerpt_citations(source),
            "registry": reference,
            "record_fragments": [fragment],
        }

    ws, voucher = _voucher_workspace("Voucher pack constraint")
    ws.add_rcm(
        {
            "process": "Purchasing",
            "risk": "Receipts may be unsupported",
            "control": "Goods are receipted against an order",
            "control_attributes": [
                {
                    "key": "three_way_match",
                    "assertion": "Existence",
                    "requirement": "A receipt is supported by an order.",
                    "evidence_kind": "transaction_cycle",
                    "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                        "procure_to_pay"
                    ).to_dict(),
                    "required_record_kinds": [
                        "procure_to_pay.purchase_order",
                        "procure_to_pay.goods_receipt",
                    ],
                }
            ],
        }
    )
    ws.save()
    ws = workspaces.load_workspace(ws.id)

    assert cycle_vouching.committed_pack_ids(ws) == ["procure_to_pay"]
    assert (
        document_context_identity(ws, voucher["id"])
        .representations["current_artifact"]["cycle_pack_ids"]
        == ["procure_to_pay"]
    )

    fake = _fake_model(monkeypatch, {VOUCHER_TAG: voucher_response})
    finished = _voucher_run(ws, voucher)
    analysis = document_analysis.load_analysis(
        workspaces.load_workspace(ws.id), voucher["id"]
    )["effective"]

    assert finished["status"] == "completed"
    assert [call["tag"] for call in fake.calls] == [VOUCHER_TAG, VOUCHER_TAG]
    assert analysis["registry"]["pack_id"] == "procure_to_pay"


def test_voucher_prompt_exposes_exact_registry_reference_objects():
    descriptors = document_workers._VOUCHER_REGISTRY_DESCRIPTORS

    for pack_id in ("procure_to_pay", "payroll"):
        reference = cycle_vouching.DEFAULT_REGISTRY.reference(pack_id).to_dict()
        assert set(reference) == {"pack_id", "pack_version", "definition_hash"}
        # Rendered as the exact object the response must copy back, so a stale
        # hash cannot survive a pack change unnoticed.
        assert json.dumps(reference, sort_keys=True, separators=(",", ":")) in descriptors

    assert "copy its\n`registry` object exactly" in document_workers.VOUCHER_SYSTEM
    assert "`pack_id`, `pack_version`, and" in document_workers.VOUCHER_SYSTEM
    assert "pack_id, version, and definition_hash" not in document_workers.VOUCHER_SYSTEM


def test_voucher_prompt_states_the_allowed_selectors_for_each_record_kind():
    """The vocabulary that fixes a bad selector must be in the prompt, not only
    in the repair message.

    Every first-attempt failure observed in the procurement run was a field the
    record genuinely stated, named through a selector the record kind does not
    offer — because the prompt supplied namespaced field ids under record kinds
    and group/short-kind under field kinds, as two lists to join by hand.
    """

    descriptors = document_workers._VOUCHER_REGISTRY_DESCRIPTORS
    registry = cycle_vouching.DEFAULT_REGISTRY

    # The selector form a response actually needs, copyable verbatim. Marking
    # interpretive attributes inline as `role~` put a syntax character inside the
    # string a response has to copy, and responses copied it — so the exemption
    # is stated on its own line instead.
    assert "parties.name.name|role" in descriptors
    assert "approvals.approval.approver|role|decision|date" in descriptors
    assert "dates.receipt_date.value|raw_value" in descriptors
    assert "~" not in descriptors
    assert "interpretive attributes, which may use your own wording" in descriptors
    for selector in ("parties.name.role", "approvals.approval.decision"):
        assert selector in descriptors

    for record_id in registry.pack("procure_to_pay").record_kind_ids:
        record = registry.record_kinds[record_id]
        assert record_id in descriptors
        for identifier_id in record.primary_identifier_kinds:
            assert identifier_id in descriptors
        for selector in document_workers._field_selectors(record.available_field_kinds):
            assert selector in descriptors

    # Every allowed selector, and nothing that is merely declared by the pack.
    assert "common.party.name" not in descriptors
    assert "available_field_kinds" not in descriptors


def test_vouchers_stay_out_of_the_unscoped_planning_default():
    """Analyzing evidence must never become a cost every planning run pays.

    A voucher is analyzed when something names it. It is not swept into the
    bounded default set, so an APM-only run over a workspace holding a voucher
    library analyses the policies and nothing else.
    """
    ws = workspaces.create_workspace("Voucher scope isolation")
    policy = documents.add_document(
        ws, "Policy.txt", b"Purchases require documented approval.", category="policy"
    )
    voucher = documents.add_document(
        ws, "PV-1.txt", b"Voucher PV-1 for PKR 100.", category="voucher"
    )
    ws = workspaces.load_workspace(ws.id)

    default_scope = document_capabilities.resolve_document_scope(ws, {})
    assert policy["id"] in default_scope.document_ids
    assert voucher["id"] not in default_scope.document_ids

    # Naming it explicitly still selects it, under the voucher profile.
    named = document_capabilities.resolve_document_scope(
        ws, {"document_ids": [voucher["id"]]}
    )
    assert named.document_ids == (voucher["id"],)


def test_voucher_profile_withholds_identifier_bearing_metadata():
    """The voucher context supplies bare identity, never the source filename.

    A voucher pack's filename routinely contains the transaction identifiers the
    profile is asked to extract from the record body, so supplying the standard
    metadata projection would let a worker report a value it never read.
    """
    ws = workspaces.create_workspace("Voucher metadata isolation")
    voucher = documents.add_document(
        ws,
        "EXP-2025-003_PV-2025-003.txt",
        b"Payment voucher for PKR 4,800.",
        category="voucher",
    )
    ws = workspaces.load_workspace(ws.id)

    candidate = document_context_identity(ws, voucher["id"])
    payload = candidate.representations["current_artifact"]
    assert set(payload) == {
        "document_id",
        "source_sha1",
        "category",
        # A closed vocabulary of pack ids, not descriptive metadata: which cycle
        # the engagement audits cannot be lifted into a field value.
        "cycle_pack_ids",
    }
    assert payload["cycle_pack_ids"] == []
    serialized = json.dumps(payload)
    assert "EXP-2025-003" not in serialized
    assert "PV-2025-003" not in serialized

    # The two text profiles must stay distinguishable by declared policy: the
    # scheduler resolves a unit's binding by its context spec hash.
    chunk_spec = PRESETS.compile("documents.analysis_chunk")
    voucher_spec = PRESETS.compile("documents.analysis_voucher")
    assert chunk_spec.to_json() != voucher_spec.to_json()


def test_no_document_analysis_runner_or_engine_remains():
    package = Path(store.__file__).parent
    assert not (package / "document_analysis_runner.py").exists()
    assert not hasattr(store, "DOCUMENT_ANALYSIS_ENGINE")
    assert "document_analysis" not in store.PROTOCOL_ENGINE_BY_RUN_KIND
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
    assert resolve_route(ws, run) == "workflow"
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
