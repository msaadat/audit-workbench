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
from types import SimpleNamespace
import json
from pathlib import Path

import pytest
from PIL import Image

from app import (
    cycle_vouching,
    document_analysis,
    document_classification,
    document_context,
    document_schemas,
    documents,
    llm,
    workspaces,
)
from app import document_masters
from app.agent import runner, store, workflow
from app.agent import capabilities as capability_registries
from app.agent.capabilities import documents as document_capabilities
from app.agent.context import PRESETS
from app.agent.documents_execution import (
    DocumentWorkflowExecution,
    build_documents_workflow_runner,
)
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
CLASSIFY_TAG = "agent:document_classification"


def _analysis_calls(fake) -> list[dict]:
    """The document-analysis model calls, with the classification pass removed.

    Every document run now names what the document *is* before mapping it, one
    call per document. These assertions pin the map/reduce sequence, which is a
    separate concern; classification has its own coverage.
    """
    return [call for call in fake.calls if call["tag"] != CLASSIFY_TAG]


def _analysis_tags(fake) -> list[str]:
    return [call["tag"] for call in _analysis_calls(fake)]

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
        "documents.categorized": ("documents.text_ready",),
        "documents.types_classified": ("documents.categorized",),
        "documents.evidence_read": ("documents.types_classified",),
        "documents.schemas_stamped": ("documents.evidence_read",),
        "documents.analysis_chunks_ready": (
            "documents.text_ready",
            "documents.categorized",
        ),
        "documents.analysis_generated": ("documents.analysis_chunks_ready",),
        "documents.analysis_reviewed": ("documents.analysis_generated",),
    }
    assert documents_workflow.FULL_DOCUMENT_OUTCOMES == [
        "documents.categorized",
        "documents.types_classified",
        "documents.schemas_stamped",
        "documents.analysis_generated",
    ]
    assert documents_workflow.outcomes_for_template("document_analysis") == [
        "documents.categorized",
        "documents.types_classified",
        "documents.schemas_stamped",
        "documents.analysis_generated",
    ]

    registry = capability_registries.build_documents_registry()
    # The prose closure no longer drags the evidence read behind it. That is the
    # schema edge coming off ``analysis_chunks_ready``: this pass carries
    # planning prose, which needs no vocabulary, and making a policy summary wait
    # on eighteen payment instructions bought nothing. What it does still wait
    # for is the category, because excluding evidence from this pass requires
    # knowing which documents are evidence.
    assert registry.closure(["documents.analysis_generated"]) == [
        "documents.text_ready",
        "documents.categorized",
        "documents.analysis_chunks_ready",
        "documents.analysis_generated",
    ]
    assert registry.closure(["documents.schemas_stamped"]) == [
        "documents.text_ready",
        "documents.categorized",
        "documents.types_classified",
        "documents.evidence_read",
        "documents.schemas_stamped",
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
        "sources.imported",
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
    assert resolved["requested_outcomes"] == [
        "documents.categorized",
        "documents.types_classified",
        "documents.schemas_stamped",
        "documents.analysis_generated",
    ]

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
    assert _analysis_tags(fake) == [
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
    assert len(_analysis_calls(fake)) == 1
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


def test_text_chunk_worker_returns_freeform_markdown_without_a_tool_call(monkeypatch):
    ws, document = _policy_workspace("Freeform document Markdown")
    documents.extract_document(ws, document["id"])
    fake = _fake_model(monkeypatch)
    run = _document_run(ws, [document["id"]])

    _drive(ws, run, "documents.analysis_chunks_ready")

    call = next(item for item in fake.calls if item["tag"] == MAP_TAG)
    assert call["tool_choice"] is None
    assert call["tools"] is None
    assert "freeform Markdown strings" in call["messages"][0]["content"]


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
    assert _analysis_tags(fake) == [MAP_TAG]
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
        "documents.categorized",
        "documents.types_classified",
        "documents.evidence_read",
        "documents.schemas_stamped",
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

    # One text map worker, one visual map worker, one reduction worker, and one
    # persistence executor are shared by standalone and audit callers. The map
    # carries planning prose; everything downstream of it — reduction,
    # persistence, reconciliation — is the single shared implementation.
    #
    # Two sit *before* it and say what a document is: the category says whether
    # this engagement holds it as transaction evidence, the type says what it is.
    # Both have to answer, and only one of them was once being asked.
    #
    # ``documents.evidence_read`` is the one that replaced a family. It reads a
    # whole evidence document — text and page images in one call — against its
    # type's accumulating master, and there is no sample worker and no
    # reconciliation, because there is no guess left to police.
    assert {
        "documents.analysis_chunk",
        "documents.analysis_visual_page",
        "documents.analysis_reduction",
        "documents.category",
        "documents.classification",
        "documents.evidence_read",
        "documents.analysis_structured",
    } <= workers
    assert len({name for name in workers if name.startswith("documents.")}) == 7
    assert {name for name in executors if name.startswith("documents.")} == {
        "documents.analysis",
        "documents.category",
        "documents.classification",
        # The read commits its reading and its type's master in one mutate;
        # the stamp writes the finished master into a schema and back-stamps
        # the readings it was built from. Splitting them is what makes a failed
        # read leave the type with no vocabulary rather than a partial one.
        "documents.read",
        "documents.stamp",
    }


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
    voucher = documents.add_document(ws, filename, source, category="evidence")
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


def test_structured_narrative_rejects_markers_whose_excerpt_did_not_survive():
    proposal = {
        "summary_markdown": "## Summary\n\nApproval is required. [C1]",
        "audit_notes_markdown": "## Audit notes\n\nNo specific observations.",
        "citations": [{"id": "C1", "page": 1, "excerpt": "not in source"}],
        "_narrative_contract": "structured_blocks_v1",
    }
    validated = {
        "summary_markdown": proposal["summary_markdown"],
        "audit_notes_markdown": proposal["audit_notes_markdown"],
        "citations": [],
    }

    with pytest.raises(
        document_workers.WorkerResponseValidationError,
        match="did not survive exact source validation",
    ):
        document_workers._validate_surviving_narrative_citations(
            proposal, validated
        )


def _analysis_milestone(ws, document_ids: list[str]) -> dict:
    """Project the document-analysis milestone for a finished run."""

    execution = DocumentWorkflowExecution.__new__(DocumentWorkflowExecution)
    execution.ws = ws
    execution.run = {
        "id": "run-milestone",
        "workflow": {
            "requested_outcomes": ["documents.analysis_generated"],
            "scope": {"document_ids": list(document_ids)},
        },
    }
    return execution.milestone_projection(
        ws,
        execution.run,
        # The projection reads only the capability's id; a full registered
        # capability would carry a scheduler's worth of unused bindings.
        SimpleNamespace(id="documents.analysis_generated"),
        {"units": []},
    )


def _metric(milestone: dict, label: str) -> int:
    return next(
        item["value"] for item in milestone["metrics"] if item["label"] == label
    )


def test_fully_covered_analyses_are_not_reported_as_partial():
    """Coverage is read from the analysis record, not from its envelope.

    `load_analysis` returns an envelope whose top level has no `coverage` key,
    so reading it there yielded None for every document and reported a clean run
    as entirely partial — one warning per analyzed document, every run.
    """

    ws = workspaces.create_workspace("Analysis coverage milestone")
    doc = documents.add_document(ws, "requisition.txt", b"Quantity 25 Kits")
    extracted = documents.extract_document(ws, doc["id"])
    document_analysis.persist_analysis(
        ws,
        doc,
        extracted,
        {
            "summary_markdown": "A purchase requisition for onboarding kits.",
            "audit_notes_markdown": "No specific observations.",
            "citations": [
                {
                    "id": "c1",
                    "page": 1,
                    "excerpt": "Quantity 25 Kits",
                    "source_sha1": doc["sha1"],
                }
            ],
        },
        provider="test",
        model="test",
        coverage={"state": "complete", "analyzed_pages": [1], "omitted_pages": []},
    )

    milestone = _analysis_milestone(ws, [doc["id"]])

    assert _metric(milestone, "Analyses generated") == 1
    assert _metric(milestone, "Partial coverage") == 0
    assert milestone["highlights"] == []
    assert milestone["status"] == "completed"


def test_the_analysis_milestone_counts_the_observations_it_recorded():
    """The notes this stage wrote are reported by this stage.

    They were once counted on the planning milestone, where a note about the
    client's own minutes sat under a headline saying the audit planning
    memorandum was ready and read as a defect in the memorandum.
    """

    ws = workspaces.create_workspace("Analysis observation milestone")
    doc = documents.add_document(ws, "sop.txt", b"Procurement SOP extract")
    extracted = documents.extract_document(ws, doc["id"])
    document_analysis.persist_analysis(
        ws,
        doc,
        extracted,
        {
            "summary_markdown": "An extract of the procurement SOP.",
            "audit_notes_markdown": (
                "## Drafting and governance observations\n"
                "- Governance metadata is not stated. Obtain the controlled header.\n"
                "- The Authority Matrix is referenced but not included. Obtain it.\n"
            ),
            "citations": [
                {
                    "id": "c1",
                    "page": 1,
                    "excerpt": "Procurement SOP extract",
                    "source_sha1": doc["sha1"],
                }
            ],
        },
        provider="test",
        model="test",
        coverage={"state": "complete", "analyzed_pages": [1], "omitted_pages": []},
    )

    milestone = _analysis_milestone(ws, [doc["id"]])

    assert _metric(milestone, "Observations recorded") == 2
    assert "They record 2 observations across 1 document." in milestone["summary"]
    # Observations are notes, not coverage gaps: they do not make the run dirty.
    assert milestone["status"] == "completed"


def test_an_analysis_that_found_nothing_says_nothing_about_observations():
    ws = workspaces.create_workspace("Analysis quiet milestone")
    doc = documents.add_document(ws, "note.txt", b"A short note")
    extracted = documents.extract_document(ws, doc["id"])
    document_analysis.persist_analysis(
        ws,
        doc,
        extracted,
        {
            "summary_markdown": "A short note.",
            "audit_notes_markdown": "No specific observations were identified.",
            "citations": [
                {
                    "id": "c1",
                    "page": 1,
                    "excerpt": "A short note",
                    "source_sha1": doc["sha1"],
                }
            ],
        },
        provider="test",
        model="test",
        coverage={"state": "complete", "analyzed_pages": [1], "omitted_pages": []},
    )

    milestone = _analysis_milestone(ws, [doc["id"]])

    assert _metric(milestone, "Observations recorded") == 0
    assert "They record" not in milestone["summary"]


def test_genuinely_partial_coverage_is_still_reported():
    ws = workspaces.create_workspace("Partial coverage milestone")
    doc = documents.add_document(ws, "minutes.txt", b"Procurement planning minutes")
    extracted = documents.extract_document(ws, doc["id"])
    document_analysis.persist_analysis(
        ws,
        doc,
        extracted,
        {
            "summary_markdown": "Minutes of the procurement planning meeting.",
            "audit_notes_markdown": "No specific observations.",
            "citations": [
                {
                    "id": "c1",
                    "page": 1,
                    "excerpt": "Procurement planning minutes",
                    "source_sha1": doc["sha1"],
                }
            ],
        },
        provider="test",
        model="test",
        coverage={
            "state": "partial",
            "analyzed_pages": [1],
            "omitted_pages": [2],
            "reason": "partial_coverage",
        },
    )

    milestone = _analysis_milestone(ws, [doc["id"]])

    assert _metric(milestone, "Partial coverage") == 1
    assert milestone["highlights"][0]["detail"] == "Only part of this document was covered."
    assert milestone["status"] == "completed_with_issues"


def _voucher_note_payload(observation: str, why: str, follow_up: str) -> dict:
    return {
        "audit_notes": [
            {
                "title": "Unit-cost arithmetic",
                "observation": observation,
                "why_it_matters": why,
                "follow_up": follow_up,
            }
        ],
        "citations": [
            {"id": "c7", "page": 1, "excerpt": "Quantity 25 Kits"},
            {"id": "c8", "page": 1, "excerpt": "Estimated unit cost (PKR) 80,000.00"},
            {"id": "c9", "page": 1, "excerpt": "Estimated total cost (PKR) 2,000,000.00"},
        ],
        "registry": {},
        "record_fragments": [],
    }


def test_structured_narrative_folds_marker_case_onto_the_supplied_id():
    payload = {
        "summary_sections": [
            {
                "heading": "Scope",
                "paragraphs": ["The policy covers procurement approvals [C1]."],
                "bullets": [],
            }
        ],
        "audit_notes": [],
        "citations": [{"id": "c1", "page": 1, "excerpt": "procurement approvals"}],
    }

    summary, _notes, contract = document_workers._structured_narrative(payload)

    assert contract == "structured_blocks_v1"
    assert "[c1]" in summary
    assert "[C1]" not in summary


def test_document_analysis_includes_vouchers_but_planning_default_excludes_them():
    """Standalone analysis and audit planning have intentionally different defaults."""
    ws = workspaces.create_workspace("Voucher scope isolation")
    policy = documents.add_document(
        ws, "Policy.txt", b"Purchases require documented approval.", category="policy"
    )
    voucher = documents.add_document(
        ws, "PV-1.txt", b"Voucher PV-1 for PKR 100.", category="evidence"
    )
    ws = workspaces.load_workspace(ws.id)

    default_scope = document_capabilities.resolve_document_scope(ws, {})
    assert policy["id"] in default_scope.document_ids
    assert voucher["id"] in default_scope.document_ids

    planning_scope = document_capabilities.resolve_document_scope(
        ws, {"document_scope_mode": "planning"}
    )
    assert policy["id"] in planning_scope.document_ids
    assert voucher["id"] not in planning_scope.document_ids

    # Naming it explicitly still selects it, under the voucher profile.
    named = document_capabilities.resolve_document_scope(
        ws, {"document_ids": [voucher["id"]]}
    )
    assert named.document_ids == (voucher["id"],)


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
    assert _analysis_tags(fake).count(MAP_TAG) == 1
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
    # Every document capability the *APM closure* reaches is reused. That is a
    # narrower set than it was: an APM needs planning context, which needs prose
    # analyses, which under 4b.1 need no vocabulary — so an APM-only run no
    # longer drags the whole evidence read behind it. What still must hold is
    # that nothing it does reach expands a unit.
    reached = {
        capability_id
        for capability_id in capability_registries.REGISTRY_BY_WORKFLOW[
            audit_workflow.WORKFLOW_ID
        ].closure(["planning.apm_ready"])
        if capability_id.startswith("documents.")
    }
    assert reached
    assert reached <= set(reloaded["workflow"]["reused_capabilities"])


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


def test_malformed_map_json_names_the_position_and_quotes_the_region():
    """The map worker returns hand-written JSON, so escaping is a live failure.

    A real run lost its single repair turn to `only the "Agreed" decision` — an
    unescaped quote inside audit_notes_markdown — because the worker carried a
    private decoder that reported only "the response is not a valid JSON
    object". That locates nothing, so the same quote comes back in the same
    place. The voucher worker submits through a tool call and cannot hit this.
    """

    broken = (
        '{"summary_markdown": "## Summary\\n\\nFine.", '
        '"audit_notes_markdown": "The extract contains only the "Agreed" decision.", '
        '"citations": []}'
    )

    with pytest.raises(document_workers.WorkerResponseValidationError) as caught:
        document_workers._json_object(broken)

    message = caught.value.errors[0]
    assert "is not a valid JSON object:" in message
    assert "at character" in message
    # The region around the break is quoted, so the repair can see the quote.
    assert "Agreed" in message
    assert "escaped" in message


def test_map_json_still_accepts_a_fenced_object_and_rejects_a_non_object():
    fenced = '```json\n{"summary_markdown": "ok"}\n```'
    assert document_workers._json_object(fenced) == {"summary_markdown": "ok"}

    with pytest.raises(
        document_workers.WorkerResponseValidationError,
        match="must be a JSON object",
    ):
        document_workers._json_object('["not", "an", "object"]')


# ------------------------------------------------- partial dependency policy
def test_every_edge_in_the_document_graph_is_partial():
    """The rule the map states, enforced rather than restated.

    ``_PARTIAL_DEPENDENCIES`` opens with "every edge in this graph is partial"
    and once listed three of the seven. The four it omitted were the upstream
    ones, so a single failed sample failed its stage, blocked induction, blocked
    chunking, and blocked every analysis in the run. Prose cannot hold a map to a
    graph; this can.

    4b.1 briefly added an exception here and it was the wrong shape. A master
    built from eight of eighteen documents is not the type's vocabulary — true,
    and worth enforcing — but expressed as a *stage* edge it became a claim
    about the whole corpus. Measured on the treasury engagement: one bank
    statement failed on a dangling citation and blocked every stamp in the run,
    including two types whose documents had all read cleanly and agreed with
    each other.

    The guarantee moved to ``_types_awaiting_stamp``, which asks it per type —
    see ``test_a_failed_reading_stops_only_its_own_type_being_stamped``.
    """
    from app.agent.documents_execution import _PARTIAL_DEPENDENCIES
    from app.agent.workflows import documents as documents_workflow

    edges = {
        (capability, dependency)
        for capability, dependencies in documents_workflow.DEPENDENCIES.items()
        for dependency in dependencies
    }
    declared = {
        (capability, dependency)
        for capability, dependencies in _PARTIAL_DEPENDENCIES.items()
        for dependency in dependencies
    }

    assert edges - declared == set(), "these edges still block the whole run"
    assert declared - edges == set(), "these name a dependency the graph does not have"


def test_one_failed_reading_no_longer_blocks_the_rest_of_the_corpus():
    """The starvation this map exists to prevent, at the edges that had it.

    A failed unit leaves its stage ``failed``; without a partial edge every later
    capability is refused. Each of these re-expands against what the earlier
    stage actually produced, so a document nobody could read costs that document
    and nothing else.
    """
    from app.agent.documents_execution import _PARTIAL_DEPENDENCIES

    def may_proceed(capability_id: str, dependency_id: str) -> bool:
        return dependency_id in _PARTIAL_DEPENDENCIES.get(capability_id, set())

    assert may_proceed("documents.evidence_read", "documents.types_classified")
    assert may_proceed("documents.analysis_chunks_ready", "documents.categorized")
    assert may_proceed("documents.analysis_chunks_ready", "documents.text_ready")
    assert may_proceed("documents.types_classified", "documents.categorized")
    assert may_proceed("documents.categorized", "documents.text_ready")


def test_a_failed_reading_stops_only_its_own_type_being_stamped():
    """The guarantee, asked per type rather than per stage.

    A vocabulary must never claim coverage it does not have: a type with an
    unread document is not offered for stamping, keeps its ``master_ref``
    readings, and is reported. But a type that read cleanly is stamped whatever
    happened to its neighbours — one bank statement failing costs the bank
    statements their schema and costs the payment instructions nothing.
    """
    from app.agent.capabilities.documents import (
        _types_awaiting_stamp, unread_documents_of_type,
    )

    ws = workspaces.create_workspace("Per-type stamp guard")
    read, unread = [
        documents.add_document(
            ws, f"{name}.txt", b"Invoice No. INV-1042", category="evidence"
        )
        for name in ("read", "unread")
    ], None
    other = documents.add_document(
        ws, "other.txt", b"Statement line", category="evidence"
    )
    for document in (*read, other):
        documents.extract_document(ws, document["id"])
    document_classification.assign(
        ws, read[0]["id"], "vendor_invoice", assigned_by="auditor", confidence="high"
    )
    document_classification.assign(
        ws, read[1]["id"], "vendor_invoice", assigned_by="auditor", confidence="high"
    )
    document_classification.assign(
        ws, other["id"], "bank_statement", assigned_by="auditor", confidence="high"
    )
    field = {
        "name": "invoice_number", "role": "identifier", "value_type": "identifier",
        "cardinality": "one", "verbatim": True, "confidence": "high", "label": "",
        "values": [{"record": 1, "entry": 1, "value": "INV", "citation": "1"}],
    }
    # One type fully read; the other missing a document.
    document_masters.apply_reading(
        ws, "bank_statement", document_id=other["id"], new_fields=[field]
    )
    document_masters.apply_reading(
        ws, "vendor_invoice", document_id=read[0]["id"], new_fields=[field]
    )
    ws = workspaces.load_workspace(ws.id)
    scope = {
        "document_ids": [item["id"] for item in (*read, other)],
        "document_scope_mode": "all",
    }

    assert unread_documents_of_type(ws, "vendor_invoice", scope) == [read[1]["id"]]
    assert unread_documents_of_type(ws, "bank_statement", scope) == []
    # The complete type is stamped; the incomplete one is withheld, not the
    # other way round and not both.
    assert _types_awaiting_stamp(ws, scope) == ["bank_statement"]


def test_an_unrelated_dependency_is_not_waved_through():
    """Partial is a property of these edges, not a blanket permission."""
    from app.agent.documents_execution import _PARTIAL_DEPENDENCIES

    assert "documents.text_ready" not in _PARTIAL_DEPENDENCIES.get(
        "documents.analysis_reviewed", set()
    )


def test_evidence_never_reaches_the_prose_pass():
    """What replaced the unstructured-voucher warning, and why it can go.

    That warning existed because completing ``_PARTIAL_DEPENDENCIES`` let a
    voucher whose type had no schema *reach* the chunk pass, where
    ``analysis_profile`` read it under ``standard``: a narrative analysis, not
    cycle evidence, and nothing about it said so. Trading a loud stop for a
    silent downgrade would have been the worse of the two.

    Under 4b.1 there is no shared stage left to fall through. Transaction
    evidence has its own pass and its own readiness, and the prose pass excludes
    it by category — so the downgrade is not reported, it is unreachable. This
    pins that, because a regression here would silently analyse every voucher
    twice under two vocabularies.
    """
    ws, voucher = _voucher_workspace("Evidence stays out of prose")
    document_classification.assign(
        ws, voucher["id"], "goods_receipt", assigned_by="auditor", confidence="high"
    )
    documents.extract_document(ws, voucher["id"])
    scope = {"document_ids": [voucher["id"]], "document_scope_mode": "all"}

    assert document_capabilities._prose_documents(ws, scope) == []
    assert document_capabilities._chunk_units(ws, scope) == []
    assert [
        unit.id.split(":")[-1]
        for unit in document_capabilities._evidence_read_units(ws, scope)
    ] == [voucher["id"]]


def test_planning_material_still_reaches_the_prose_pass():
    """The other side of the same gate. Excluding by category, not by type."""
    ws, policy = _policy_workspace("Policy stays prose")
    documents.extract_document(ws, policy["id"])
    scope = {"document_ids": [policy["id"]], "document_scope_mode": "all"}

    assert document_capabilities._prose_documents(ws, scope) == [policy["id"]]
    assert document_capabilities._evidence_read_units(ws, scope) == []
