import inspect

import polars as pl

from app import (
    analysis_memo,
    assistant,
    data_tests,
    doc_tests,
    document_analysis,
    document_context,
    documents,
    methodology,
    templates_store,
    workspaces,
)
from app.agent import context as agent_context
from app.agent.context import ContextResolver, PRESETS
import app.agent.context.adapters as context_adapters


def _analyzed_document(
    workspace,
    filename,
    text,
    *,
    category="policy",
    summary,
    notes,
    fields=None,
    citations=(),
):
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
            "citations": list(citations),
            "fields": dict(fields or {}),
            "analysis_profile": "voucher" if fields else "generic",
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


def test_rcm_scope_supplies_the_process_description_without_the_audit_notes():
    """The RCM turn reasons from the process, not from conclusions about it.

    Its parent APM already carries every audit note forward, so repeating the
    numbered deficiency list here is what makes the turn transcribe observations
    into rows. The APM turn itself is unaffected and still receives both blocks.
    """
    workspace = workspaces.create_workspace("RCM document scope")
    document = _analyzed_document(
        workspace,
        "procurement-sop.txt",
        "Purchase orders require approval before issue.",
        summary="The SOP describes requisition, approval, and purchase order issue.",
        notes="1. **Missing thresholds** - the SOP defines no monetary bands.",
    )
    workspace.update_planning(
        {
            "context": {"objective": "Assess procurement approvals"},
            "apm_markdown": "# APM\n\nProcurement approvals.",
        }
    )

    rcm = context_adapters.rcm_scope(workspace, document_ids=[document["id"]])
    apm = context_adapters.apm_document_methodology_scope(
        workspace, document_ids=[document["id"]]
    )

    rcm_summary = rcm.candidates["documents"][0].representations["summary"]
    apm_summary = apm.candidates["documents"][0].representations["summary"]
    assert "requisition, approval, and purchase order issue" in rcm_summary
    assert "AUDIT NOTES" not in rcm_summary
    assert "Missing thresholds" not in rcm_summary
    # The APM keeps both blocks: observations belong to the memorandum.
    assert "AUDIT NOTES" in apm_summary
    assert "Missing thresholds" in apm_summary


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
        "analysis_summary",
        "table_metadata",
        "table_profiles",
        "documents",
        "methodology",
    ]
    assert [source.selector.selector_id for source in spec.sources] == [
        "planning.current",
        "templates.current",
        "artifacts.current",
        "analyses.all",
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
    # Planning sees the written memo, never the flagged rows behind it.
    assert spec.privacy.allow_analysis_summary is True
    assert spec.privacy.allow_analysis_exception_rows is False
    assert spec.privacy.allow_table_rows is False
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


def test_tests_generate_preset_declares_the_row_scoped_sources():
    spec = PRESETS.compile("tests.generate")

    assert [source.id for source in spec.sources] == [
        "planning_context",
        "rcm_row",
        "table_metadata",
        "documents",
        "methodology",
    ]
    # The one target row is required; every material source is not, since the
    # model chooses source per test.
    assert [source.required for source in spec.sources] == [
        True, True, False, False, False,
    ]
    # Generation reads schema metadata and document text — never a table row —
    # since it decides both Data and Document Test sources itself.
    assert spec.privacy.allow_table_metadata is True
    assert spec.privacy.allow_table_profiles is False
    assert spec.privacy.allow_document_text is True
    assert spec.privacy.allow_table_rows is False


def test_test_generate_scope_supplies_one_target_row_and_citable_methodology():
    workspace = workspaces.create_workspace("Test generate scope")
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
    doc_tests.create_draft(
        workspace,
        {"title": "Existing", "objective": "Existing objective", "rcm_id": target_id},
    )
    methodology.save_pack(
        workspace,
        "Firm AP Guide",
        "# Duplicate payments\nProcedures should address duplicate-payment risk.",
    )

    scope = context_adapters.test_generate_scope(workspace, target_id)

    rows = scope.candidates["rcm_row"]
    assert [candidate.source_ref for candidate in rows] == [f"rcm:{target_id}"]
    # The row's existing tests travel with it so a re-run revises rather than
    # duplicates them.
    assert rows[0].source["existing_tests"][0]["objective"] == "Existing objective"
    assert rows[0].source["existing_tests"][0]["source"] == "document"
    # Execution state stays out of the generation context.
    assert "execution_rollup" not in rows[0].source
    # The turn is scoped to its own row: no other row travels with it, since a
    # unit cannot see what its siblings generate and the projection carried
    # their risks rather than their tests.
    assert "other_rcm_rows" not in scope.candidates
    section = scope.candidates["methodology"][0].representations["excerpt"]
    assert section["pack_name"] == "Firm AP Guide"
    assert section["citation"].startswith("Firm AP Guide v")
    assert "duplicate-payment risk" in section["text"]
    assert "Duplicate payments are processed" in scope.selector_context["test_generate_query"]


def test_test_generate_scope_supplies_table_and_document_sources_together():
    # One RCM row's unit needs both table and document material in the same
    # turn, since the model — not the unit — decides each test's source.
    workspace = workspaces.create_workspace("Test generate mixed scope")
    workspace.add_table(
        "transactions.csv",
        pl.DataFrame({"invoice": [1, 1, 2]}).write_csv().encode(),
    )
    # A document with no ``planning_relevant`` category flag would have been
    # withheld by the old ``tests.draft`` preset's planning-relevant filter;
    # the merged document source must still offer it.
    documents.add_document(
        workspace, "Approval.txt", b"Management approved.", category="evidence"
    )
    row = workspace.add_rcm(
        {"process": "AP", "risk": "Duplicate payments", "control": "Duplicate check"}
    )

    scope = context_adapters.test_generate_scope(workspace, row["id"])

    assert {key for key, value in scope.candidates.items() if value} == {
        "planning_context",
        "rcm_row",
        "table_metadata",
        "documents",
    }
    table_names = {
        candidate.metadata.get("table") for candidate in scope.candidates["table_metadata"]
    }
    assert "transactions" in table_names
    # A document with no analysis and no planning-relevant flag is still
    # selectable evidence for a step.
    supplied = scope.candidates["documents"][0].representations["summary"]
    assert supplied["summary"] == ""
    assert supplied["id"]


def test_test_generate_metadata_supplies_category_values_but_never_row_values():
    """A generated Polars step is a predicate, so it needs the column's domain.

    Given names and dtypes alone the turn has to guess what a status column
    holds, and a wrong guess does not fail loudly: it matches every row or none,
    and both are reported as a control conclusion. A value is only supplied where
    it is a category rather than a datum — the table has a population and each
    value recurs across it — and only where the list is provably complete.
    """
    workspace = workspaces.create_workspace("Test generation metadata")
    orders = pl.DataFrame(
        {
            "order_id": [f"O{index:03d}" for index in range(40)],
            "status": ["Closed"] * 38 + ["Open"] * 2,
            "note": [f"free text {index}" for index in range(40)],
        }
    )
    workspace.add_table("orders.csv", orders.write_csv().encode())

    candidates = context_adapters.test_generate_table_metadata_candidates(workspace)
    columns = {
        column["name"]: column
        for column in candidates[0].representations["table_metadata"]["columns"]
    }

    # A recurring, provably complete domain: the step can be written against it.
    assert columns["status"]["values"] == ["Closed", "Open"]
    # One row per value is the row itself, so the count is supplied without them.
    assert "values" not in columns["note"]
    assert columns["note"]["distinct"] == 40
    # The identifier is neither categorical nor bounded.
    assert "values" not in columns["order_id"]


def test_test_generate_metadata_withholds_a_truncated_category_vocabulary():
    """An incomplete list is worse than none — it licenses excluding a real value."""
    workspace = workspaces.create_workspace("Truncated vocabulary")
    frame = pl.DataFrame(
        {
            "row_id": list(range(120)),
            # More distinct values than the profile retains, each well repeated.
            "bank": [f"Bank {index % 20:02d}" for index in range(120)],
        }
    )
    workspace.add_table("payments.csv", frame.write_csv().encode())

    candidates = context_adapters.test_generate_table_metadata_candidates(workspace)
    columns = {
        column["name"]: column
        for column in candidates[0].representations["table_metadata"]["columns"]
    }

    assert columns["bank"]["distinct"] == 20
    assert "values" not in columns["bank"]


def test_test_generate_scope_supplies_grounded_vouch_metadata_without_values():
    workspace = workspaces.create_workspace("Grounded test generation")
    workspace.add_table(
        "po_header.csv",
        pl.DataFrame(
            {
                "po_number": ["PO-1001", "PO-1002"],
                "amount": [50000, 7500],
            }
        ).write_csv().encode(),
    )
    document = _analyzed_document(
        workspace,
        "PO-1001.txt",
        "Purchase order PO-1001 total PKR 50,000.",
        category="voucher",
        summary="Purchase order.",
        notes="None.",
        citations=(
            {
                "id": "C1",
                "page": 1,
                "excerpt": "Purchase order PO-1001 total PKR 50,000.",
            },
        ),
        fields={
            "document_type": "purchase_order",
            "identifiers": [
                {"kind": "po_number", "value": "PO-1001", "citation": "C1"}
            ],
            "amounts": [
                {"kind": "total", "value": 50000.0, "citation": "C1"}
            ],
        },
    )
    row = workspace.add_rcm(
        {
            "process": "Purchasing",
            "risk": "Purchase orders may be unsupported",
            "control": "Orders require support",
        }
    )

    scope = context_adapters.test_generate_scope(
        workspaces.load_workspace(workspace.id),
        row["id"],
        document_ids=[document["id"]],
    )

    table = scope.candidates["table_metadata"][0].source
    assert table["vouch_anchor_candidates"] == [
        {
            "table": "po_header",
            "anchor_key": "po_number",
            "matched_rows": 1,
            "matched_document_count": 1,
            "document_types": ["purchase_order"],
        }
    ]
    profile = scope.candidates["documents"][0].source["vouch_profile"]
    assert profile["document_type"] == "purchase_order"
    assert profile["available_path_suffixes"] == [
        "amount.total",
        "identifier.po_number",
    ]
    assert "PO-1001" not in str(profile)
    assert "50000" not in str(profile)


def test_test_generate_scope_supplies_the_process_description_without_the_audit_notes():
    """A test obtains evidence about a control; audit notes are conclusions.

    The notes block is a numbered deficiency list whose entries each end in a
    follow-up, which makes it the most test-shaped content in the turn. Supplied
    here it comes back as objectives that re-confirm a known deficiency rather
    than establishing whether a control operated. The deficiency already reaches
    the turn through the RCM row that drives the unit.
    """
    workspace = workspaces.create_workspace("Test generate document scope")
    document = _analyzed_document(
        workspace,
        "procurement-sop.txt",
        "Purchase orders require approval before issue.",
        summary="The SOP describes requisition, approval, and purchase order issue.",
        notes="1. **Missing thresholds** - the SOP defines no monetary bands.",
    )
    workspace.update_planning({"context": {"objective": "Assess procurement approvals"}})
    row = workspace.add_rcm(
        {
            "process": "Procurement",
            "risk": "Purchase orders may be issued without approval",
            "risk_rating": "high",
            "control": "No control identified",
        }
    )

    scope = context_adapters.test_generate_scope(
        workspace, row["id"], document_ids=[document["id"]]
    )

    summary = scope.candidates["documents"][0].source["summary"]
    assert "requisition, approval, and purchase order issue" in summary
    assert "AUDIT NOTES" not in summary
    assert "Missing thresholds" not in summary


# --------------------------------------------------------------------------- #
# The EDA memo reaches planning
# --------------------------------------------------------------------------- #
FENCE = "```"

MEMO = (
    "## Data received and population characteristics\n"
    "118 invoices were received.\n\n"
    f"{FENCE}embed\nanalysis: A-0DAB063C\nas: exception_table\n"
    f"caption: the backdated invoice\n{FENCE}\n\n"
    "## Exceptions noted\nInvoice INV2024008 was received before it was issued.\n"
)


def _memo_workspace(name: str = "APM analysis basis"):
    ws = workspaces.create_workspace(name)
    ws.analysis_summary.update(
        {
            "markdown": MEMO,
            "cited_analysis_ids": ["A-0DAB063C"],
            "basis_sha1": "digest",
            "generated_at": "2026-08-07T00:00:00+00:00",
            "run_id": "run-1",
        }
    )
    ws.save()
    return workspaces.load_workspace(ws.id)


def test_the_analysis_memo_reaches_the_apm_bundle():
    ws = _memo_workspace()
    scope = context_adapters.apm_document_methodology_scope(ws)

    candidates = scope.candidates[context_adapters.APM_SUMMARY_SOURCE_ID]
    assert len(candidates) == 1
    content = candidates[0].source
    assert "INV2024008" in content["markdown"]
    assert content["cited_analysis_ids"] == ["A-0DAB063C"]


def test_embed_directives_are_flattened_before_planning_sees_them():
    """Planning has no renderer for an embed fence.

    A raw directive copied into an APM prints as stray text, so the reference
    survives as a readable citation instead of as markup nobody can resolve.
    """
    ws = _memo_workspace("APM flattening")

    markdown = context_adapters.apm_document_methodology_scope(ws).candidates[context_adapters.APM_SUMMARY_SOURCE_ID][
        0
    ].source["markdown"]

    assert f"{FENCE}embed" not in markdown
    assert "as: exception_table" not in markdown
    assert "See analysis A-0DAB063C" in markdown
    assert "the backdated invoice" in markdown


def test_a_flattened_citation_names_the_procedure_when_the_title_is_known():
    titles = {"A-0DAB063C": "Invoice-to-Receipt Date Lag"}
    flattened = analysis_memo.flatten_embeds(MEMO, titles)

    assert "See analysis Invoice-to-Receipt Date Lag (A-0DAB063C)" in flattened
    assert f"{FENCE}" not in flattened


def test_a_workspace_with_no_memo_supplies_no_analysis_source():
    """Exploratory analysis frequently has not run when the APM is drafted."""
    ws = workspaces.create_workspace("APM without analysis")

    scope = context_adapters.apm_document_methodology_scope(workspaces.load_workspace(ws.id))

    assert scope.candidates[context_adapters.APM_SUMMARY_SOURCE_ID] == ()


def test_the_apm_template_requires_the_analytics_section():
    """The worker's validator enforces every template heading, so adding the
    section to the template is what makes the APM answer for the analysis."""
    ws = workspaces.create_workspace("APM template sections")
    template = templates_store.get_template(ws, "apm")["markdown"]

    assert "## Data analytics performed" in template
    headings = [
        line.strip() for line in template.splitlines() if line.startswith("## ")
    ]
    # It belongs before the risk response: the risks are argued from it.
    assert headings.index("## Data analytics performed") < headings.index(
        "## Key risks and planned response"
    )
