import inspect

import polars as pl

from app import assistant, document_analysis, document_context, documents, methodology, workspaces
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
        "planning:context",
        "template:apm",
        "planning:apm",
        f"document:{policy['id']}",
        "methodology:workspace:procurement-audit-guide:1",
    ]
    assert "SENSITIVE PROCUREMENT SUMMARY" in bundle.to_json()
    assert "SENSITIVE METHODOLOGY" in bundle.to_json()
    assert "SENSITIVE PROCUREMENT SUMMARY" not in manifest.to_json()
    assert "SENSITIVE METHODOLOGY" not in manifest.to_json()
    assert {item.source_id for item in manifest.selections} == {
        "planning_context",
        "apm_template",
        "current_apm",
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
        summary="approval " + "C" * 29_000,
        notes="approval " + "D" * 29_000,
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
    document_items = [item for item in bundle.items if item.source_id == "documents"]
    assert len(document_items) == 1
    assert document_items[0].supplied_size.characters == 40_000
    assert any(
        item.source_id == "documents" and "size limit" in item.reason
        for item in manifest.omissions
    )


def test_apm_table_adapters_supply_metadata_and_profiles_without_row_values(
    monkeypatch,
):
    workspace = workspaces.create_workspace("APM table adapter")
    sentinel = "ROW_SECRET_NEVER_SEND_7F4C"
    workspace.add_table(
        "private-ledger.csv",
        pl.DataFrame(
            {
                "invoice_id": [sentinel, "INV-2"],
                "amount": [100.0, 300.0],
            }
        ).write_csv().encode(),
    )

    schema_calls = []
    original_schema_brief = assistant.schema_brief

    def tracked_schema_brief(*args, **kwargs):
        schema_calls.append(True)
        return original_schema_brief(*args, **kwargs)

    profile_calls = []
    original_table_metadata = assistant.table_metadata

    def tracked_table_metadata(*args, **kwargs):
        profile_calls.append(kwargs.get("include_category_values"))
        return original_table_metadata(*args, **kwargs)

    monkeypatch.setattr(assistant, "schema_brief", tracked_schema_brief)
    monkeypatch.setattr(assistant, "table_metadata", tracked_table_metadata)

    manifest, bundle = ContextResolver().resolve(
        workspace,
        {"id": "planning.apm_ready", "context": "planning.apm"},
        {"id": "planning.apm:workspace"},
        agent_context.apm_document_methodology_scope(workspace),
    )

    assert schema_calls == [True]
    assert profile_calls == [False]
    assert [item.representation.kind for item in bundle.items] == [
        "planning_context",
        "artifact_template",
        "current_artifact",
        "table_metadata",
        "table_profile",
    ]
    serialized = bundle.to_json()
    assert "private_ledger" in serialized
    assert "invoice_id" in serialized
    assert '"mean":"200"' in serialized
    assert sentinel not in serialized
    assert '"values"' not in serialized
    assert sentinel not in manifest.to_json()
    assert [selection.source_id for selection in manifest.selections] == [
        "planning_context",
        "apm_template",
        "current_apm",
        "table_metadata",
        "table_profiles",
    ]


def test_planning_apm_preset_declares_all_current_adapter_sources():
    spec = PRESETS.compile("planning.apm")

    assert [source.id for source in spec.sources] == [
        "planning_context",
        "apm_template",
        "current_apm",
        "table_metadata",
        "table_profiles",
        "documents",
        "methodology",
    ]
    assert [source.selector.selector_id for source in spec.sources] == [
        "planning.current",
        "templates.current",
        "artifacts.current",
        "tables.all",
        "tables.all",
        "documents.lexical",
        "methodology.lexical",
    ]
    assert spec.privacy.allow_planning_context is True
    assert spec.privacy.allow_template_text is True
    assert spec.privacy.allow_document_text is True
    assert spec.privacy.allow_table_metadata is True
    assert spec.privacy.allow_table_profiles is True
    assert spec.privacy.allow_table_rows is False

    adapter_source = inspect.getsource(context_adapters)
    assert "assistant.schema_brief" in adapter_source
    assert "assistant.table_metadata" in adapter_source
    assert "document_context.apm_document_context" in adapter_source
    assert "methodology.context_sections" in adapter_source
    assert ".get_frame(" not in adapter_source
    assert "project_frame" not in adapter_source
    assert "profiler" not in adapter_source
    assert "document_analysis" not in adapter_source
    assert "document_search" not in adapter_source


def test_planning_planned_tests_preset_declares_the_row_scoped_sources():
    spec = PRESETS.compile("planning.planned_tests")

    assert [source.id for source in spec.sources] == [
        "planning_context",
        "rcm_row",
        "other_rcm_rows",
        "table_metadata",
        "documents",
        "methodology",
    ]
    # The one target row is required; the duplicate-avoidance index is not.
    assert [source.required for source in spec.sources] == [
        True, True, False, False, False, False,
    ]
    # Planned-test drafting reads schema metadata, never row values or profiles.
    assert spec.privacy.allow_table_metadata is True
    assert spec.privacy.allow_table_profiles is False
    assert spec.privacy.allow_table_rows is False


def test_planned_test_scope_supplies_one_target_row_and_citable_methodology():
    workspace = workspaces.create_workspace("Planned test scope")
    workspace.update_planning(
        {"context": {"objective": "Assess payments", "scope": "Accounts payable"}}
    )
    workspace.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate payments are processed",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    workspace.add_rcm(
        {"process": "Payroll", "risk": "Ghost employees", "control": "Headcount review"}
    )
    target_id = workspace.rcm[0]["id"]
    other_id = workspace.rcm[1]["id"]
    workspace.add_planned_test(
        target_id,
        {"title": "Existing", "objective": "Existing objective", "method": "inquiry"},
    )
    methodology.save_pack(
        workspace,
        "Firm AP Guide",
        "# Duplicate payments\nProcedures should address duplicate-payment risk.",
    )

    scope = context_adapters.planned_test_scope(workspace, target_id)

    rows = scope.candidates["rcm_row"]
    assert [candidate.source_ref for candidate in rows] == [f"rcm:{target_id}"]
    # The existing planned tests travel with the row so an update can name one.
    assert rows[0].source["planned_tests"][0]["objective"] == "Existing objective"
    # Execution state stays out of the drafting context.
    assert "execution_rollup" not in rows[0].source
    assert [candidate.source_ref for candidate in scope.candidates["other_rcm_rows"]] == [
        f"rcm:{other_id}"
    ]
    assert set(scope.candidates["other_rcm_rows"][0].source) == {
        "id", "semantic_id", "risk",
    }
    section = scope.candidates["methodology"][0].representations["excerpt"]
    assert section["pack_name"] == "Firm AP Guide"
    assert section["citation"].startswith("Firm AP Guide v")
    assert "duplicate-payment risk" in section["text"]
    assert "Duplicate payments are processed" in scope.selector_context["planned_test_query"]


def test_fieldwork_execution_definitions_preset_serves_both_unit_kinds():
    spec = PRESETS.compile("fieldwork.execution_definitions")

    assert [source.id for source in spec.sources] == [
        "rcm_row",
        "planned_test",
        "table_metadata",
        "documents",
        "current_data_tests",
        "current_document_tests",
    ]
    # Only the two shared parents are required; each unit kind supplies its own
    # optional sources and the rest are recorded as absent in that manifest.
    assert [source.id for source in spec.sources if source.required] == [
        "rcm_row",
        "planned_test",
    ]
    assert spec.privacy.allow_table_metadata is True
    assert spec.privacy.allow_table_rows is False
    assert spec.privacy.allow_table_profiles is False


def test_definition_scopes_supply_only_their_own_unit_kind_sources():
    workspace = workspaces.create_workspace("Definition scopes")
    workspace.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1, 2]}).write_csv().encode(),
    )
    documents.add_document(workspace, "Approval.txt", b"Management approved.")
    row = workspace.add_rcm(
        {"process": "AP", "risk": "Duplicate payments", "control": "Duplicate check"}
    )
    planned = workspace.add_planned_test(
        row["id"],
        {
            "title": "Test duplicates",
            "objective": "Identify duplicates",
            "method": "data_analytics",
            "steps": ["Identify repeated invoice identifiers."],
        },
    )

    data_scope = context_adapters.data_test_spec_scope(
        workspace, row["id"], planned["id"]
    )
    document_scope = context_adapters.document_test_spec_scope(
        workspace, row["id"], planned["id"]
    )

    assert set(data_scope.candidates) == {
        "rcm_row",
        "planned_test",
        "table_metadata",
        "current_data_tests",
    }
    assert set(document_scope.candidates) == {
        "rcm_row",
        "planned_test",
        "documents",
        "current_document_tests",
    }
    # A document with no analysis is still selectable evidence for an item.
    supplied = document_scope.candidates["documents"][0].representations["summary"]
    assert supplied["summary"] == ""
    assert supplied["id"]
