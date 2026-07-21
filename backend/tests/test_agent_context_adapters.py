import inspect

from app import document_analysis, document_context, documents, methodology, workspaces
from app.agent import context as agent_context
from app.agent.context import ContextResolver, PRESETS
import app.agent.context.adapters as context_adapters


def _analyzed_document(workspace, filename, text, *, category="policy", summary, notes):
    document = documents.add_document(
        workspace,
        filename,
        text.encode("utf-8"),
        category=category,
    )
    extracted = documents.extract_document(workspace, document["id"])
    document_analysis.persist_analysis(
        workspace,
        document,
        extracted,
        {
            "summary_markdown": summary,
            "audit_notes_markdown": notes,
            "citations": [],
        },
        provider="local",
        model="test",
    )
    return document


def test_apm_adapters_reuse_document_and_methodology_boundaries(monkeypatch):
    workspace = workspaces.create_workspace("APM adapter sources")
    policy = _analyzed_document(
        workspace,
        "procurement-policy.txt",
        "Purchases require approval before commitment.",
        summary="SENSITIVE PROCUREMENT SUMMARY: approval is required.",
        notes="Inspect evidence of procurement approval.",
    )
    unrelated = _analyzed_document(
        workspace,
        "travel-guide.txt",
        "Travel claims require receipts.",
        category="other",
        summary="Travel reimbursement guidance.",
        notes="Inspect travel receipts.",
    )
    methodology.save_pack(
        workspace,
        "Procurement Audit Guide",
        "# Approval testing\nSENSITIVE METHODOLOGY: test procurement approvals before commitment.",
    )
    methodology.save_pack(
        workspace,
        "Travel Audit Guide",
        "# Expense testing\nTest mileage and meal claims.",
    )

    document_calls = []
    original_document_context = document_context.apm_document_context

    def tracked_document_context(*args, **kwargs):
        document_calls.append(args[1])
        return original_document_context(*args, **kwargs)

    section_calls = []
    original_sections = methodology.context_sections

    def tracked_sections(*args, **kwargs):
        section_calls.append(True)
        return original_sections(*args, **kwargs)

    monkeypatch.setattr(document_context, "apm_document_context", tracked_document_context)
    monkeypatch.setattr(methodology, "context_sections", tracked_sections)

    scope = agent_context.apm_document_methodology_scope(
        workspace,
        planning_context={
            "objective": "Assess procurement approval compliance",
            "scope": "Purchases before commitment",
        },
    )
    manifest, bundle = ContextResolver().resolve(
        workspace,
        {"id": "planning.apm_ready", "context": "planning.apm"},
        {"id": "planning.apm:workspace"},
        scope,
    )

    assert document_calls == [policy["id"], unrelated["id"]]
    assert section_calls == [True]
    assert [item.source_ref for item in bundle.items] == [
        f"document:{policy['id']}",
        "methodology:workspace:procurement-audit-guide:1",
    ]
    assert "SENSITIVE PROCUREMENT SUMMARY" in bundle.to_json()
    assert "SENSITIVE METHODOLOGY" in bundle.to_json()
    assert "SENSITIVE PROCUREMENT SUMMARY" not in manifest.to_json()
    assert "SENSITIVE METHODOLOGY" not in manifest.to_json()
    assert {item.source_id for item in manifest.selections} == {
        "documents",
        "methodology",
    }
    assert any(
        item.source_ref == f"document:{unrelated['id']}"
        and "did not match" in item.reason
        for item in manifest.omissions
    )
    assert documents.activities(workspace)["items"] == []


def test_apm_document_adapter_leaves_final_truncation_and_omission_to_resolver():
    workspace = workspaces.create_workspace("APM adapter budgets")
    first = _analyzed_document(
        workspace,
        "approval-a.txt",
        "Approval policy A.",
        summary="approval " + "A" * 29_000,
        notes="approval " + "B" * 29_000,
    )
    second = _analyzed_document(
        workspace,
        "approval-b.txt",
        "Approval policy B.",
        summary="approval " + "C" * 4_000,
        notes="approval " + "D" * 4_000,
    )

    manifest, bundle = ContextResolver().resolve(
        workspace,
        {"id": "planning.apm_ready", "context": "planning.apm"},
        {"id": "planning.apm:workspace"},
        agent_context.apm_document_methodology_scope(
            workspace,
            planning_context={"objective": "approval"},
            document_ids=[first["id"], second["id"]],
        ),
    )

    assert manifest.truncations
    assert manifest.truncations[0].source_id == "documents"
    assert manifest.truncations[0].supplied_size.characters == 40_000
    assert len(bundle.items) == 1
    assert bundle.supplied_size.characters == 40_000
    assert any(
        item.source_id == "documents" and "size limit" in item.reason
        for item in manifest.omissions
    )


def test_planning_apm_preset_declares_only_current_adapter_sources():
    spec = PRESETS.compile("planning.apm")

    assert [source.id for source in spec.sources] == ["documents", "methodology"]
    assert [source.selector.selector_id for source in spec.sources] == [
        "documents.lexical",
        "methodology.lexical",
    ]
    assert spec.privacy.allow_document_text is True
    assert spec.privacy.allow_table_metadata is False

    adapter_source = inspect.getsource(context_adapters)
    assert "document_context.apm_document_context" in adapter_source
    assert "methodology.context_sections" in adapter_source
    assert "document_analysis" not in adapter_source
    assert "document_search" not in adapter_source
