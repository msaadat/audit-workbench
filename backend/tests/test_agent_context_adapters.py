import inspect

import polars as pl

from app import (
    analysis_memo,
    assistant,
    cycle_vouching,
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
    evidence=None,
    citations=(),
):
    """Persist one analysis. ``evidence`` makes it a registry-backed voucher.

    The voucher profile persists a reduction — ``registry``, ``records``,
    ``record_fragments``, ``unresolved_fragments``, ``conflicts`` — so a fixture
    that supplied the removed ``fields`` map described a shape nothing produces
    and nothing reads.
    """

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
            "analysis_profile": "voucher" if evidence else "generic",
            **(dict(evidence or {})),
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


def test_small_table_row_candidates_supplies_whole_small_reference_tables():
    """A profile alone cannot say which row carries a correlated value.

    A 4-row approval matrix profiled as ``max=10000000, nulls_pct=25`` cannot
    distinguish "one row overrides the cap" from "the cap is 10,000,000" — the
    aggregate is a faithful description of the wrong thing for a table this
    small. Below ``MAX_SMALL_TABLE_ROWS`` the whole table is supplied instead.
    """
    workspace = workspaces.create_workspace("Small reference table")
    workspace.add_table(
        "approval_matrix.csv",
        pl.DataFrame(
            {
                "role": ["Clerk", "Manager", "Director", "CEO"],
                "max_approval_amount": [1_000_000, 5_000_000, 10_000_000, 10_000_000],
                "limit_notes": [None, None, None, "No ceiling; board notified"],
            }
        ).write_csv().encode(),
    )

    candidates = context_adapters.small_table_row_candidates(workspace)

    assert [candidate.source_ref for candidate in candidates] == [
        "table:approval_matrix"
    ]
    content = candidates[0].representations["table_rows_small"]
    assert content["table"] == "approval_matrix"
    rows = content["rows"]
    assert len(rows) == 4
    # The CEO row's actual override reaches the candidate; no aggregate substitutes.
    assert ["Clerk", 1_000_000, None] in rows
    assert ["CEO", 10_000_000, "No ceiling; board notified"] in rows


def test_small_table_row_candidates_withholds_tables_above_the_row_ceiling():
    workspace = workspaces.create_workspace("Large table")
    workspace.add_table(
        "transactions.csv",
        pl.DataFrame(
            {
                "id": list(range(context_adapters.MAX_SMALL_TABLE_ROWS + 1)),
                "amount": [1.0] * (context_adapters.MAX_SMALL_TABLE_ROWS + 1),
            }
        ).write_csv().encode(),
    )

    assert context_adapters.small_table_row_candidates(workspace) == ()


def test_planning_rcm_preset_declares_the_small_table_rows_source():
    spec = PRESETS.compile("planning.rcm")

    source_ids = [source.id for source in spec.sources]
    assert "small_table_rows" in source_ids
    small_table_source = next(
        source for source in spec.sources if source.id == "small_table_rows"
    )
    assert small_table_source.required is False
    assert [rep.kind for rep in small_table_source.representations] == [
        "table_rows_small"
    ]
    assert spec.privacy.allow_small_table_rows is True
    # The generic table-row permission stays denied even though this preset
    # now admits whole small tables under its own, narrower permission.
    assert spec.privacy.allow_table_rows is False


def test_rcm_scope_supplies_small_table_rows_alongside_profiles():
    workspace = workspaces.create_workspace("RCM small table scope")
    workspace.add_table(
        "approval_matrix.csv",
        pl.DataFrame(
            {"role": ["Clerk", "CEO"], "max_approval_amount": [1_000_000, 10_000_000]}
        ).write_csv().encode(),
    )

    rcm = context_adapters.rcm_scope(workspace)

    small_table_candidates = rcm.candidates["small_table_rows"]
    assert [candidate.source_ref for candidate in small_table_candidates] == [
        "table:approval_matrix"
    ]


def _dated_workspace(name, *, joined=False):
    workspace = workspaces.create_workspace(name)
    workspace.add_table(
        "requisitions.csv",
        pl.DataFrame(
            {
                "req_id": ["R1", "R2"],
                "staff_id": ["S1", "S2"],
                "raised_on": ["2023-01-10", "2024-06-01"],
                "amount": [10.0, 30.0],
            }
        ).write_csv().encode(),
    )
    workspace.add_table(
        "staff.csv",
        pl.DataFrame(
            {"staff_id": ["S1", "S2"], "hire_date": ["2010-01-15", "2019-04-04"]}
        ).write_csv().encode(),
    )
    if joined:
        workspace.add_join(
            {
                "name": "requisitions_staff_joined",
                "left": "requisitions",
                "right": "staff",
                "how": "left",
                "left_on": ["staff_id"],
                "right_on": ["staff_id"],
            }
        )
    return workspace


def test_the_population_summary_reports_ranges_per_table_and_totals_per_column():
    workspace = _dated_workspace("APM populations")

    (candidate,) = context_adapters.population_summary_candidates(workspace)
    content = candidate.representations["population_summary"]
    by_table = {item["table"]: item for item in content["tables"]}

    assert content["total_rows"] == 4
    # Each range stays attached to the column it came from. A single span
    # across the workspace would open this engagement in 2010, on a hire date.
    assert by_table["requisitions"]["date_columns"] == [
        {"column": "raised_on", "min": "2023-01-10", "max": "2024-06-01"}
    ]
    assert by_table["staff"]["date_columns"] == [
        {"column": "hire_date", "min": "2010-01-15", "max": "2019-04-04"}
    ]
    assert "observed_period" not in content
    # The number an audit reads a money column by, which no profile carries.
    assert by_table["requisitions"]["numeric_columns"] == [
        {"column": "amount", "total": "40"}
    ]


def test_the_population_summary_describes_imported_tables_not_derived_frames():
    workspace = _dated_workspace("APM derived frames", joined=True)
    joined = [
        name for name in workspace.table_names()
        if name not in {"requisitions", "staff"}
    ]

    (candidate,) = context_adapters.population_summary_candidates(workspace)
    content = candidate.representations["population_summary"]

    assert [item["table"] for item in content["tables"]] == ["requisitions", "staff"]
    # Whatever join inference derived, total_rows counts each record once.
    assert content["total_rows"] == 4
    assert all(name not in str(content) for name in joined)


def test_a_reference_column_is_counted_but_never_totalled():
    workspace = workspaces.create_workspace("APM reference columns")
    workspace.add_table(
        "invoices.csv",
        pl.DataFrame(
            {
                # A foreign key repeated across the population: too few distinct
                # values to infer as an id, so only its name rules it out.
                "approved_by_id": [7, 7, 9, 9],
                "verified_by": [3, 3, 4, 4],
                "invoice_amount": [10.0, 20.0, 30.0, 40.0],
            }
        ).write_csv().encode(),
    )

    (candidate,) = context_adapters.population_summary_candidates(workspace)
    (table,) = candidate.representations["population_summary"]["tables"]

    assert table["numeric_columns"] == [{"column": "invoice_amount", "total": "100"}]


def test_a_workspace_whose_dates_are_text_reports_no_range_to_read():
    workspace = workspaces.create_workspace("APM undated")
    workspace.add_table(
        "ledger.csv",
        pl.DataFrame(
            # A period written as text is not one the range can be read off:
            # min/max would be lexical order, which is only accidentally right.
            {"entry": ["E1", "E2"], "booked": ["Q1 2024", "Q3 2023"]}
        ).write_csv().encode(),
    )

    (candidate,) = context_adapters.population_summary_candidates(workspace)
    content = candidate.representations["population_summary"]

    assert content["tables"][0]["date_columns"] == []


def test_planning_table_sources_are_scoped_to_the_populations_received():
    workspace = _dated_workspace("APM planning scope", joined=True)
    derived = {
        name for name in workspace.table_names()
        if name not in {"requisitions", "staff"}
    }
    assert derived, "the fixture must derive at least one join frame"

    scope = context_adapters.apm_document_methodology_scope(workspace)
    rcm = context_adapters.rcm_scope(workspace)

    # A derived frame carries the same data under another name, and a budgeted
    # source fills in candidate-name order — so admitting them crowds the
    # populations themselves out of the turn.
    for candidates in (
        scope.candidates["table_metadata"],
        scope.candidates["table_profiles"],
        rcm.candidates["table_metadata"],
        rcm.candidates["table_profiles"],
        rcm.candidates["small_table_rows"],
    ):
        assert {candidate.metadata["table"] for candidate in candidates} <= {
            "requisitions",
            "staff",
        }
    # The analysis capability still sees them: a derived frame is what it
    # analyses.
    assert derived & {
        candidate.metadata.get("table")
        for candidate in context_adapters.apm_table_profile_candidates(workspace)
    }


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
    # Category values are requested by the profile adapter, but the table has
    # only 2 rows — well under MIN_CATEGORY_ROWS — so the gate in
    # apm_table_profile_candidates strips them before they reach the bundle
    # (checked below). The population summary asks for none: it reports ranges,
    # never domains.
    assert profile_calls == [False, True]
    assert [item.representation.kind for item in bundle.items] == [
        "planning_context",
        "artifact_template",
        "current_artifact",
        "population_summary",
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
        "population_summary",
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
        "population_summary",
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
    assert "profiler" not in adapter_source
    assert "document_analysis" not in adapter_source
    assert "document_search" not in adapter_source

    # planning.apm's own table sources stay metadata/profile-only: no adapter
    # it declares fetches a real frame or a row preview. ``get_frame`` and
    # ``project_frame`` do appear elsewhere in the module now, for the
    # separate, row-count-gated ``small_table_rows`` source that only
    # ``planning.rcm`` declares (see the small-table tests below).
    for adapter in (
        context_adapters.apm_table_metadata_candidates,
        context_adapters.apm_table_profile_candidates,
        context_adapters.population_summary_candidates,
        context_adapters.apm_document_methodology_scope,
    ):
        source = inspect.getsource(adapter)
        assert ".get_frame(" not in source
        assert "project_frame" not in source


def test_tests_generate_preset_declares_the_row_scoped_sources():
    spec = PRESETS.compile("tests.generate")

    assert [source.id for source in spec.sources] == [
        "planning_context",
        "rcm_row",
        "table_metadata",
        "transaction_evidence",
        "documents",
        "methodology",
    ]
    # The one target row is required; every material source is not, since the
    # model chooses source per test.
    assert [source.required for source in spec.sources] == [
        True, True, False, True, False, False,
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
            "transaction_evidence",
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


def _vendor_workspace(name):
    """A workspace whose join frames outnumber and outsize its base tables."""
    workspace = workspaces.create_workspace(name)
    workspace.add_table(
        "vendor_master_file.csv",
        pl.DataFrame(
            {
                "VENDOR_ID": ["V1", "V2"],
                "VENDOR_NAME": ["A", "B"],
                "BANK_ACCOUNT_NUMBER": ["1", "1"],
                "APPROVED_BY": [7, 7],
            }
        )
        .write_csv()
        .encode(),
    )
    workspace.add_table(
        "invoice_data.csv",
        pl.DataFrame(
            {
                "INVOICE_ID": ["I1", "I2"],
                "VENDOR_ID": ["V1", "V2"],
                "INVOICE_AMOUNT": [10, 20],
                "PAYMENT_STATUS": ["Paid", "Paid"],
                "PAYMENT_DATE": ["2024-01-01", "2024-01-02"],
                "VERIFIED_BY_ID": [7, 8],
            }
        )
        .write_csv()
        .encode(),
    )
    return workspace


def test_a_population_outranks_the_join_frames_built_over_it():
    """The table an RCM row is about must survive a budgeted, ranked source.

    A join frame's name contains every word of the tables it was built from, so
    at equal weight it matches every query its parents match and sorts above
    all of them. Six joins *over* the vendor master once filled a vendor-master
    row's schema list while the vendor master itself never reached the prompt.
    """
    workspace = _vendor_workspace("Vendor ranking")
    workspace.add_join(
        {
            "name": "invoice_data_vendor_master_file_joined",
            "left": "invoice_data",
            "right": "vendor_master_file",
            "left_on": ["VENDOR_ID"],
            "right_on": ["VENDOR_ID"],
            "how": "left",
        }
    )
    row = workspace.add_rcm(
        {
            "process": "Vendor master maintenance",
            "risk": "Two vendor master records may share one bank account",
            "control": "Vendor master bank account review",
        }
    )

    scope = context_adapters.test_generate_scope(workspace, row["id"])
    candidates = {
        candidate.metadata["table"]: candidate
        for candidate in scope.candidates["table_metadata"]
    }

    assert candidates["vendor_master_file"].metadata["grain"] == "vendor_master_file"
    assert (
        candidates["invoice_data_vendor_master_file_joined"].metadata["grain"]
        == "invoice_data"
    )
    manifest, bundle = ContextResolver().resolve(
        workspace,
        {"id": "tests.specified", "context": "tests.generate"},
        {"id": f"test_generation:{row['id']}"},
        scope,
    )
    ordered = [
        item.source_ref for item in bundle.items if item.source_id == "table_metadata"
    ]
    assert ordered.index("table:vendor_master_file") < ordered.index(
        "table:invoice_data_vendor_master_file_joined"
    )
    assert manifest.omissions == () or all(
        omission.source_id != "table_metadata" for omission in manifest.omissions
    )


def test_a_table_no_word_of_the_row_matches_is_ranked_down_not_dropped():
    """Lexical ranking of tables is an ordering, not a filter.

    A document sharing no term with the row is not evidence for it. A *table*
    sharing none is still a population the turn may have to test, and dropping
    it empties the schema list — the one input that decides whether a data test
    can be written for the row at all.
    """
    workspace = _vendor_workspace("Unmatched ranking")
    row = workspace.add_rcm(
        {
            "process": "Vendor master maintenance",
            "risk": "Two vendor master records may share one bank account",
            "control": "Vendor master bank account review",
        }
    )

    scope = context_adapters.test_generate_scope(workspace, row["id"])
    _, bundle = ContextResolver().resolve(
        workspace,
        {"id": "tests.specified", "context": "tests.generate"},
        {"id": f"test_generation:{row['id']}"},
        scope,
    )
    ordered = [
        item.source_ref for item in bundle.items if item.source_id == "table_metadata"
    ]

    assert ordered == ["table:vendor_master_file", "table:invoice_data"]


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
        evidence={
            "registry": cycle_vouching.DEFAULT_REGISTRY.reference(
                "procure_to_pay"
            ).to_dict(),
            "record_fragments": [],
            "records": [],
            "unresolved_fragments": [],
            "conflicts": [],
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
    assert "vouch_anchor_candidates" not in table
    document_context = scope.candidates["documents"][0].source
    assert "vouch_profile" not in document_context
    manifest = scope.candidates["transaction_evidence"][0].source
    assert manifest["groups"] == []
    assert manifest["manifest_sha256"].startswith("sha256:")


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
    "## Data received and its limitations\n"
    "118 invoices were received.\n\n"
    f"{FENCE}embed\nanalysis: A-0DAB063C\nas: exception_table\n"
    f"caption: the backdated invoice\n{FENCE}\n\n"
    "## What the analysis found\nInvoice INV2024008 was received before it was issued.\n"
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


# --------------------------------------------------------------------------- #
# Naming documents for the model
# --------------------------------------------------------------------------- #
def test_document_display_name_prefers_the_file_over_the_intake_slug():
    document = {
        "id": "d1",
        "source": "Minutes of Meeting - CFO.docx",
        "title": "minutes_of_meeting_cfo",
    }

    assert context_adapters._document_display_name(document) == (
        "Minutes of Meeting - CFO.docx"
    )
    # The slug still names a document that arrived without a filename.
    assert context_adapters._document_display_name(
        {"id": "d1", "title": "minutes_of_meeting_cfo"}
    ) == "minutes_of_meeting_cfo"
    assert context_adapters._document_display_name(None, "d1") == "d1"
    assert context_adapters._document_display_name({}, "") == ""


def test_document_candidates_index_the_slug_even_though_they_stop_showing_it(
    workspace_with_data,
):
    """Naming a document differently must not change which are selected."""
    ws = workspace_with_data
    source = documents.add_document(ws, "Procurement SOP Extracts.docx", b"The procedure text.")
    documents.update_document(ws, source["id"], {"category": "policy"})
    ws = workspaces.Workspace(ws.root)

    candidates = context_adapters.apm_document_candidates(ws)
    candidate = next(
        item for item in candidates if item.source_ref == f"document:{source['id']}"
    )

    # What the model is handed.
    assert candidate.metadata["title"] == "Procurement SOP Extracts.docx"
    # What a lexical selector still matches on.
    assert source["title"] in candidate.lexical_text
    assert "Procurement SOP Extracts.docx" in candidate.lexical_text
