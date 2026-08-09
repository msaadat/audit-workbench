import json

import pytest
from fastapi.testclient import TestClient

from app import doc_tests, findings, llm, report, templates_store, workspaces
from app.agent import store
from app.main import create_app


def linked_workspace(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Test duplicate-payment controls.", "scope": "Supplied transactions."}})
    rcm = ws.add_rcm({"process": "Payables", "risk": "Duplicate payments", "risk_rating": "high"})
    procedure = ws.add_procedure(
        {
            "objective": "Determine whether duplicate payments occurred",
            "criteria": "Invoices are paid once.",
            "rcm_refs": [rcm["id"]],
            "scope_limitations": "Only the supplied period was tested.",
        }
    )
    analysis = ws.add_analysis(
        {
            "kind": "analytics", "table": "transactions", "title": "Duplicate invoices",
            "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
        }
    )
    anchor = findings.anchor_from_ref(ws, f"analysis:{analysis['id']}")
    execution = doc_tests.create_test(ws, {
        "kind": "review", "title": "Duplicate-payment result review",
        "objective": "Determine whether duplicate payments occurred",
        "rcm_id": rcm["id"],
        "items": [{"label": "Review result", "state": "confirmed", "auditor_disposition": "accepted"}],
    })
    execution = doc_tests.update_test(ws, execution["id"], {
        "status": "completed",
        "scope_limitations": "Only the supplied period was tested.",
    })
    return ws, rcm, procedure, execution, analysis, anchor


COMPLETE_NARRATIVE = """## Condition

Invoice 1006 appears twice.

## Criteria

Each invoice should be paid once.

## Root Cause

No duplicate check at invoice entry.

## Risk

Financial loss through duplicate payment.

## Recommendation

Configure and monitor a duplicate-payment control.
"""


def complete_finding_payload(rcm, procedure, execution, anchor):
    return {
        "title": "Duplicate invoices were processed",
        "severity": "high",
        "narrative": COMPLETE_NARRATIVE,
        "management_response": "Management will update the control.",
        "rcm_refs": [rcm["id"]],
        "procedure_refs": [procedure["id"]],
        "test_refs": [execution["id"]],
        "execution_refs": [f"doctest:{execution['id']}"],
        "evidence_refs": [anchor],
        "auditor_confirmed": True,
    }


def test_finding_crud_validates_typed_sources_and_rolls_up(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    assert item["source"] == "manual"
    assert item["evidence_refs"][0]["source_sha1"]
    assert findings.rollups(ws)["by_rcm"][rcm["id"]][0]["id"] == item["id"]

    updated = findings.update(ws, item["id"], {"severity": "critical"})
    assert updated["severity"] == "critical"
    assert "status" not in updated
    assert workspaces.load_workspace(ws.id).find_semantic("findings", item["semantic_id"])["id"] == item["id"]

    broken = {**anchor, "source_id": "missing"}
    with pytest.raises(workspaces.WorkspaceError, match="does not exist"):
        findings.update(ws, item["id"], {"evidence_refs": [broken]})

    stale = {**anchor, "source_sha1": "0" * 40}
    updated = findings.update(ws, item["id"], {"evidence_refs": [stale]})
    assert updated["evidence_refs"][0]["source_sha1"] == "0" * 40
    assert findings.evidence_warnings(ws, updated) == [
        f"Evidence source '{anchor['source_kind']}:{anchor['source_id']}' has changed since this finding was drafted."
    ]

    findings.remove(ws, item["id"])
    assert ws.findings == []


def test_agent_finding_promotion_is_explicit_typed_and_idempotent(workspace_with_data):
    ws, _rcm, _procedure, _execution, analysis, _anchor = linked_workspace(workspace_with_data)
    run = store.new_run(ws, "auto")
    run["findings"] = [
        {
            "id": "finding-1", "severity": "medium", "statement": "A duplicate invoice was observed.",
            "basis": "observed", "evidence_refs": [f"analysis:{analysis['id']}"],
        }
    ]
    store.save_run(ws, run)

    promoted = findings.promote(ws, run["id"], "finding-1")
    again = findings.promote(ws, run["id"], "finding-1")
    assert promoted["id"] == again["id"]
    assert promoted["source"] == "promoted"
    assert "status" not in promoted
    # The run's statement seeds the first template section; the rest stay open
    # for the auditor, so the promotion is a draft rather than a formal finding.
    assert promoted["narrative"].startswith("## Condition\n\nA duplicate invoice was observed.")
    assert "## Recommendation" in promoted["narrative"]
    assert "narrative section not completed: Recommendation" in findings.support_issues(ws, promoted)
    assert promoted["evidence_refs"][0]["source_kind"] == "analysis"


def test_finding_derives_typed_evidence_from_execution_reference(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, _anchor = linked_workspace(
        workspace_with_data
    )
    payload = complete_finding_payload(
        rcm, procedure, execution,
        {"source_kind": "doctest", "source_id": execution["id"], "source_sha1": execution["sha1"]},
    )
    payload.pop("evidence_refs")

    item = findings.add(ws, payload)

    assert item["evidence_refs"][0]["source_kind"] == "doctest"
    assert item["evidence_refs"][0]["source_id"] == execution["id"]
    assert item["evidence_refs"][0]["source_sha1"] == findings.artifact(
        ws, "doctest", execution["id"]
    )["sha1"]
    assert findings.support_issues(ws, item) == []


def test_a_new_finding_starts_from_the_template_scaffold(workspace_with_data):
    ws = workspace_with_data

    item = findings.add(ws, {"title": "Blank finding"})

    # The auditor is handed the firm's own sections rather than an empty box,
    # and an empty scaffold never passes the confirmation gate.
    assert findings.template_sections(ws) == [
        "Condition", "Criteria", "Root Cause", "Risk", "Recommendation",
    ]
    assert item["narrative"] == (
        "## Condition\n\n## Criteria\n\n## Root Cause\n\n## Risk\n\n## Recommendation\n"
    )
    # Template guidance is authoring instruction, never finding content.
    assert "<!--" not in item["narrative"]
    assert "narrative section not completed: Condition" in findings.support_issues(ws, item)


def test_the_finding_gate_follows_the_workspace_template(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    templates_store.put_template(
        ws, "finding", "# Finding\n\n## Observation\n\n## Remedy\n"
    )
    payload = complete_finding_payload(rcm, procedure, execution, anchor)
    payload["auditor_confirmed"] = False

    item = findings.add(ws, payload)

    # The shipped headings no longer exist for this firm, so a narrative written
    # against them answers nothing the template asks for.
    assert findings.support_issues(ws, item) == [
        "narrative section not completed: Observation",
        "narrative section not completed: Remedy",
    ]
    answered = findings.update(
        ws, item["id"],
        {"narrative": "## Observation\n\nOne duplicate.\n\n## Remedy\n\nBlock it.\n"},
    )
    assert findings.support_issues(ws, answered) == []


def test_root_cause_is_the_only_section_an_auditor_may_defer(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    payload = complete_finding_payload(rcm, procedure, execution, anchor)
    payload["auditor_confirmed"] = False
    payload["narrative"] = COMPLETE_NARRATIVE.replace(
        "## Root Cause\n\nNo duplicate check at invoice entry.", "## Root Cause"
    ).replace("## Risk\n\nFinancial loss through duplicate payment.", "## Risk")

    item = findings.add(ws, payload)

    assert findings.support_issues(ws, item) == [
        "narrative section not completed: Root Cause",
        "narrative section not completed: Risk",
    ]
    deferred = findings.update(ws, item["id"], {"cause_pending": True})
    assert findings.support_issues(ws, deferred) == [
        "narrative section not completed: Risk"
    ]


def test_legacy_field_findings_migrate_into_one_narrative(workspace_with_data):
    ws = workspace_with_data
    ws.findings.append({
        "id": "F-LEGACY",
        "title": "Approvals were missing",
        "severity": "high",
        "condition": "Twelve payments had no approval.",
        "criteria": "Payments require approval.",
        "cause": "No system check.",
        "effect": "Unauthorized disbursement.",
        "recommendation": "Enforce approval before release.",
        "severity_rationale": "Material to the payment population.",
    })
    ws.save()

    migrated = workspaces.load_workspace(ws.id).findings[0]

    assert migrated["narrative"] == (
        "## Condition\n\nTwelve payments had no approval.\n\n"
        "## Criteria\n\nPayments require approval.\n\n"
        "## Root Cause\n\nNo system check.\n\n"
        "## Risk\n\nUnauthorized disbursement.\n\n"
        "## Recommendation\n\nEnforce approval before release.\n\n"
        # Severity rationale is no longer a required section, but the prose an
        # auditor already wrote is carried rather than dropped.
        "## Severity rationale\n\nMaterial to the payment population."
    )
    assert not any(field in migrated for field in findings.LEGACY_NARRATIVE_FIELDS)


def test_a_stale_caller_cannot_write_a_removed_narrative_field(workspace_with_data):
    ws = workspace_with_data
    with pytest.raises(workspaces.WorkspaceError, match="section of the finding narrative"):
        findings.add(ws, {"title": "Legacy", "condition": "Something happened."})
    item = findings.add(ws, {"title": "Legacy"})
    with pytest.raises(workspaces.WorkspaceError, match="section of the finding narrative"):
        findings.update(ws, item["id"], {"recommendation": "Do better."})


def test_the_report_copies_a_finding_narrative_verbatim(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))

    markdown = report.deterministic_markdown(ws)

    # The narrative is auditor-approved report text, so only its heading depth
    # moves: a finding sits at `###`, its sections one level below.
    assert "#### Condition\n\nInvoice 1006 appears twice." in markdown
    assert "#### Root Cause\n\nNo duplicate check at invoice entry." in markdown
    assert "#### Risk\n\nFinancial loss through duplicate payment." in markdown
    assert "**Condition:**" not in markdown
    assert "\n## Condition" not in markdown


def test_report_context_excludes_rows_and_document_excerpts(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    context = report.build_context(ws)
    serialized = json.dumps(context)
    assert context["statistics"]["findings"] == 1
    assert "invoice_no" not in serialized
    assert "excerpt" not in serialized
    assert "Duplicate invoices were processed" in serialized


def test_report_context_falls_back_to_labelled_apm_fields(workspace_with_data):
    workspace_with_data.update_planning({
        "apm_markdown": (
            "# APM\n\n## Engagement\n\n"
            "- Entity: Global Bank\n"
            "- Period: January–December 2025\n"
            "- Objective & Scope: Review procurement approvals.\n"
        )
    })

    planning = report.build_context(workspace_with_data)["planning"]

    assert planning == {
        "objective": "Review procurement approvals.",
        "entity": "Global Bank",
        "period": "January–December 2025",
        "scope": "Review procurement approvals.",
        "materiality": None,
    }


def test_deterministic_report_edit_aware_regeneration_and_reconcile(monkeypatch, workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})

    first = report.generate(ws)
    assert first["requires_reconcile"] is False
    assert f"finding={item['id']}" in first["markdown"]
    report.update(ws, {"markdown": first["markdown"] + "\nAuditor edit.\n"})
    second = report.generate(ws)
    assert second["requires_reconcile"] is True
    assert second["markdown"].endswith("Auditor edit.\n")
    assert second["candidate_markdown"] != second["markdown"]

    kept = report.reconcile(ws, "keep")
    assert kept["edited"] is True
    replaced = report.reconcile(ws, "replace")
    assert replaced["edited"] is False
    assert replaced["markdown"] == replaced["generated_markdown"]


def test_deterministic_preliminary_report_discloses_incomplete_workflow_coverage():
    ws = workspaces.create_workspace("Incomplete report coverage")
    ws.update_planning({
        "context": {"objective": "Assess procurement", "scope": "Procure to pay"},
        "apm_markdown": "# Audit Planning Memorandum\n\nProcurement scope.",
    })
    ws.add_rcm({
        "process": "Purchasing", "risk": "Purchases bypass approval",
        "control": "Approval workflow",
    })
    row = ws.add_rcm({
        "process": "Payments", "risk": "Duplicate invoices are paid",
        "control": "Duplicate invoice check",
    })
    doc_tests.create_draft(ws, {
        "title": "Test duplicate invoices",
        "objective": "Identify duplicate invoices",
        "criteria": "Each invoice is paid once.",
        "steps": [{"label": "Inspect duplicate invoice identifiers.", "instruction": "Inspect duplicate invoice identifiers."}],
        "rcm_id": row["id"],
    })
    workflow_state = {
        "stages": [
            {"capability": "planning.rcm_ready", "units": [{"status": "failed"}]},
            {"capability": "tests.specified", "units": [{"status": "failed"}]},
        ]
    }

    generated = report.generate(ws, use_model=False, workflow=workflow_state)

    assert generated["generation_warnings"] == [
        "Incomplete planning coverage: 1 planning workflow unit(s) failed and "
        "1 required planning item(s) are missing.",
        "Incomplete execution-definition coverage: 1 execution-definition workflow "
        "unit(s) failed and 1 required execution definition(s) are missing.",
    ]
    assert "# Preliminary Internal Audit Working Draft" in generated["markdown"]
    # Limitations bound the scope, so they are disclosed under it rather than in
    # a section of their own a reader has to go looking for.
    assert "### 2. Objective and Scope" in generated["markdown"]
    assert "**Scope limitations**" in generated["markdown"]
    assert "Incomplete planning coverage: 1 planning workflow unit(s) failed" in generated["markdown"]
    assert "Incomplete execution-definition coverage: 1 execution-definition workflow unit(s) failed" in generated["markdown"]


def test_report_nests_the_executive_summary_under_one_heading(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))

    markdown = report.deterministic_markdown(ws)

    # The template declares one flat list of sections; the detail section is the
    # boundary, and everything before it renders one level down under an
    # inserted grouping heading. Parts are lettered, sub-parts numbered.
    assert "## A. Executive Summary" in markdown
    for number, heading in enumerate(
        ("Introduction", "Objective and Scope", "Audit Conclusion", "Key Findings"), 1
    ):
        assert f"### {number}. {heading}" in markdown
    assert "## B. Detailed Findings" in markdown
    assert markdown.index("## A. Executive Summary") < markdown.index("## B. Detailed Findings")
    # Findings are numbered in the order they are presented.
    assert "### 1. Duplicate invoices were processed" in markdown
    # The management response sits with the finding it answers.
    assert "**Management response:** Management will update the control." in markdown
    assert "## Management responses" not in markdown


def test_a_deferred_root_cause_is_stated_rather_than_left_blank(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    payload = complete_finding_payload(rcm, procedure, execution, anchor)
    payload["narrative"] = COMPLETE_NARRATIVE.replace(
        "## Root Cause\n\nNo duplicate check at invoice entry.", "## Root Cause"
    )
    payload["cause_pending"] = True
    findings.add(ws, payload)

    markdown = report.deterministic_markdown(ws)

    # An auditor may formally defer the cause; the report has to say so rather
    # than print the heading over empty space.
    assert (
        "#### Root Cause\n\nNot established by the evidence obtained; "
        "pending auditor follow-up."
    ) in markdown
    assert "#### Root Cause\n\n#### Risk" not in markdown


def test_the_management_response_line_is_stated_once_when_none_exist(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    for title in ("First gap", "Second gap"):
        findings.add(ws, {
            **complete_finding_payload(rcm, procedure, execution, anchor),
            "title": title, "management_response": "",
        })

    markdown = report.deterministic_markdown(ws)

    # One fact about the engagement, not a line repeated under every finding.
    assert markdown.count("No management responses have been received") == 1
    assert "**Management response:**" not in markdown


def test_key_findings_table_carries_high_risk_findings_only(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    high = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    low = findings.add(
        ws,
        {
            **complete_finding_payload(rcm, procedure, execution, anchor),
            "title": "A minor gap",
            "severity": "low",
        },
    )

    markdown = report.deterministic_markdown(ws)
    table = markdown.split("### 4. Key Findings", 1)[1].split("##", 1)[0]

    assert "| # | Process | Key Finding | Risk Level | Recommendation |" in table
    assert "Payables" in table and "High" in table
    # The row names the numbered finding a reader then turns to, so the
    # executive table and the detail section share one numbering.
    assert "| 1 | Payables |" in table
    assert f"### 1. {high['title']}" in markdown
    # Senior management is deciding where to look, so the table carries the
    # high-risk findings only; every finding still appears in full below.
    assert low["title"] not in table
    assert f"### 2. {low['title']}" in markdown


def test_key_findings_group_the_findings_of_one_process(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    other = ws.add_rcm({"process": "Vendors", "risk": "Unvetted vendors", "risk_rating": "high"})
    for title, row in (
        ("Duplicate one", rcm), ("Vendor gap", other), ("Duplicate two", rcm),
    ):
        payload = {**complete_finding_payload(rcm, procedure, execution, anchor), "title": title}
        if row is other:
            payload["rcm_refs"] = [rcm["id"], other["id"]]
        findings.add(ws, payload)

    table = report.deterministic_markdown(ws).split("### 4. Key Findings", 1)[1].split("\n##", 1)[0]
    rows = [line for line in table.splitlines() if line.startswith("| ") and "---" not in line][1:]
    processes = [line.split("|")[2].strip() for line in rows]

    # Markdown cannot span rows, so a process is named once and its remaining
    # findings continue under a blank cell.
    assert processes[0] == "Payables"
    assert processes[1] == ""
    assert processes.count("") == len(processes) - len({p for p in processes if p})


def test_summary_of_findings_counts_every_confirmed_finding(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    for title, severity in (("A", "high"), ("B", "high"), ("C", "medium"), ("D", "info")):
        findings.add(ws, {
            **complete_finding_payload(rcm, procedure, execution, anchor),
            "title": title, "severity": severity,
        })

    summary = report.deterministic_markdown(ws).split("### 5. Summary of Findings", 1)[1]

    assert "| Unit | Critical | High | Medium | Low |" in summary
    assert f"| {ws.name} | 0 | 2 | 1 | 0 |" in summary
    # An informational finding has no column, so it is stated rather than
    # dropped from a count a reader will take as complete.
    assert "A further 1 finding(s) are recorded at informational severity" in summary


def test_recorded_limitations_are_capped_and_the_remainder_counted(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    context = report.build_context(ws)
    context["scope_limitations"] = [
        {"rcm_id": rcm["id"], "test_id": f"T-{index}", "text": f"Limitation {index}."}
        for index in range(report._SCOPE_LIMITATION_LIMIT + 4)
    ]
    context["preliminary"] = False

    body = report._scope_body(context)

    # Limitations are recorded per test, so a thinly evidenced engagement
    # restates the same few gaps many times; the count is more use than the list.
    assert body.count("\n- ") == report._SCOPE_LIMITATION_LIMIT + 1
    assert "A further 4 limitation(s) are recorded" in body


def test_a_findings_own_counts_are_not_read_as_the_reports_arithmetic(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    payload = complete_finding_payload(rcm, procedure, execution, anchor)
    payload["narrative"] = COMPLETE_NARRATIVE.replace(
        "Invoice 1006 appears twice.",
        "The review identified 1 exception across the invoices examined.",
    )
    findings.add(ws, payload)

    codes = {
        issue["code"]
        for issue in report.quality_checks(ws, report.deterministic_markdown(ws))["issues"]
    }

    # The finding counts exceptions in its own population; only the report's own
    # prose claims a total for the engagement.
    assert "report_arithmetic" not in codes
    # A total the report itself states is still held to the records.
    assert "report_arithmetic" in {
        issue["code"]
        for issue in report.quality_checks(ws, "# Report\n\nFieldwork found 7 exceptions.")["issues"]
    }


def test_near_identical_findings_are_flagged_for_the_auditor(workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    first = findings.add(ws, {
        **complete_finding_payload(rcm, procedure, execution, anchor),
        "title": "Segregation-of-duties exception result is not reliable",
    })
    second = findings.add(ws, {
        **complete_finding_payload(rcm, procedure, execution, anchor),
        "title": "Procurement segregation-of-duties exception result was not reliable",
    })
    findings.add(ws, {
        **complete_finding_payload(rcm, procedure, execution, anchor),
        "title": "Payment recorded before invoice date",
    })

    duplicates = [
        issue
        for issue in report.quality_checks(ws)["issues"]
        if issue["code"] == "duplicate_finding"
    ]

    # Advisory, and only for the restatement — a finding that merely shares a
    # subject is a different finding.
    assert len(duplicates) == 1
    assert duplicates[0]["severity"] == "warning"
    assert set(duplicates[0]["refs"]) == {f"finding:{first['id']}", f"finding:{second['id']}"}


def test_the_model_drafts_three_sections_and_never_the_findings(monkeypatch, workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    calls = []

    def fake_chat(messages, tools=None, temperature=0.0, profile="assistant"):
        stage = messages[0]["content"].splitlines()[0]
        calls.append((stage, messages[1]["content"]))
        if "key_findings" in stage:
            return {"content": json.dumps({"rows": [
                {"finding_id": ws.findings[0]["id"], "key_finding": "Two payments duplicated.",
                 "recommendation": "Block duplicate invoice numbers."}
            ]})}
        if "conclusion" in stage:
            return {"content": json.dumps({
                "rating": "marginal", "conclusion": "**Marginal.** Controls need work."
            })}
        # The overview call returns Markdown, not JSON: its body is multi-
        # paragraph prose with a bulleted list, which a JSON string escapes
        # badly and loses the whole section to a parse error.
        return {"content": (
            "## Introduction\n\nDrafted introduction.\n\n"
            "## Objective and Scope\n\nDrafted scope.\n\n"
            "**Scope limitations**\n\n- Operating evidence was not supplied.\n"
        )}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "fake", "model": "fake"})

    result = report.generate(ws)

    assert result["used_model"] is True
    # One bounded call per drafted section, and no call carries the whole context.
    assert [stage for stage, _user in calls] == [
        "[agent:report_overview]", "[agent:report_conclusion]", "[agent:report_key_findings]",
    ]
    # The old whole-context prompt overflowed a 30,000-character budget and fell
    # back to re-sending everything once per section; each call now fits easily.
    assert all(len(user) < 30_000 for _stage, user in calls)
    # Fieldwork is still open here, so the proposed rating is refused and the
    # conclusion keeps the deterministic body that assigns none.
    assert result["drafted_sections"] == [
        "introduction", "key findings", "objective and scope",
    ]
    assert "Controls need work" not in result["markdown"]
    assert "No overall rating is assigned" in result["markdown"]
    assert "Drafted introduction." in result["markdown"]
    assert "Drafted scope." in result["markdown"]
    assert "Two payments duplicated." in result["markdown"]
    # The narrative is auditor-approved text: it is assembled, never redrafted,
    # so no call is ever given the chance to rewrite it.
    assert "#### Condition\n\nInvoice 1006 appears twice." in result["markdown"]
    assert not any("Invoice 1006 appears twice" in user for _stage, user in calls[:2])


def test_the_rating_band_is_bounded_by_the_recorded_evidence():
    closed = {"preliminary": False, "rcm": [], "findings": []}

    assert report.rating_band(closed)["ceiling"] == "satisfactory"

    # A confirmed finding caps the rating however the conclusion is worded.
    high = report.rating_band({**closed, "findings": [{"severity": "high"}]})
    assert high["ceiling"] == "fair"
    assert "satisfactory" not in high["allowed"]
    assert report.rating_band(
        {**closed, "findings": [{"severity": "critical"}]}
    )["ceiling"] == "marginal"

    # Controls the fieldwork could not conclude on are a coverage failure, and
    # they bound the rating as surely as an adverse result does.
    unconcluded = report.rating_band({**closed, "rcm": [
        {"control_conclusion": "no_conclusion"}, {"control_conclusion": "no_conclusion"},
        {"control_conclusion": "effective"}, {"control_conclusion": "effective"},
    ]})
    assert unconcluded["ceiling"] == "marginal"
    assert unconcluded["allowed"] == ["marginal", "unsatisfactory"]

    # Open fieldwork withholds a rating rather than assigning a cautious one.
    open_fieldwork = report.rating_band({**closed, "preliminary": True})
    assert open_fieldwork["assignable"] is False
    assert open_fieldwork["allowed"] == []


def test_an_overstated_rating_is_rejected_and_flagged(monkeypatch, workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))

    def fake_chat(messages, tools=None, temperature=0.0, profile="assistant"):
        if "conclusion" in messages[0]["content"].splitlines()[0]:
            return {"content": json.dumps({
                "rating": "satisfactory", "conclusion": "**Satisfactory.** All is well.",
            })}
        return {"content": json.dumps({"rows": [], "introduction": "", "objective_and_scope": ""})}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "fake", "model": "fake"})

    result = report.generate(ws)

    assert "audit conclusion" not in result["drafted_sections"]
    assert "All is well" not in result["markdown"]
    assert any("no overall rating may be assigned" in warning
               for warning in result["generation_warnings"])
    # An auditor who types the overstated rating back in is told the same thing.
    codes = {
        issue["code"]
        for issue in report.quality_checks(ws, "# Report\n\n**Rating: Satisfactory**")["issues"]
    }
    assert "report_rating_unsupported" in codes


def test_quality_checks_are_advisory_and_detect_traceability_arithmetic_and_exceptions(workspace_with_data):
    ws, rcm, procedure, _execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, {"title": "Incomplete finding", "rcm_refs": [rcm["id"]]})
    test = doc_tests.create_test(
        ws,
        {
            "kind": "review", "title": "Minutes review",
            "items": [{"id": "ITEM-1", "label": "Minutes", "state": "exception", "auditor_disposition": "exception"}],
        },
    )
    checked = report.quality_checks(
        ws,
        f"# Report\n\nThere are 9 findings and 3 exceptions. [Broken](?tab=findings&finding=F-MISSING)",
    )
    codes = {issue["code"] for issue in checked["issues"]}
    assert {"finding_draft", "unsupported_finding", "unresolved_exception", "report_arithmetic", "broken_report_citation", "missing_limitations", "preliminary_label_missing"} <= codes
    assert checked["ok"] is False
    assert ws.findings[0]["id"] == item["id"]
    assert doc_tests.exists(ws, test["id"])


def test_quality_checks_detect_rcm_risk_distribution_drift(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({"context": {"objective": "Audit procurement", "scope": "Procurement"}})
    ws.add_rcm({"risk": "Approval bypass", "risk_rating": "high"})
    ws.add_rcm({"risk": "Duplicate payment", "risk_rating": "medium"})
    ws.add_rcm({"risk": "Vendor concentration", "risk_rating": "medium"})

    checked = report.quality_checks(
        ws,
        "# Preliminary report\n\nRisk distribution: high 2, medium 1, low 0.",
    )

    risk_issues = [
        issue for issue in checked["issues"]
        if issue["code"] == "report_risk_arithmetic"
    ]
    assert len(risk_issues) == 2
    assert any("high-risk count" in issue["message"] for issue in risk_issues)
    assert any("medium-risk count" in issue["message"] for issue in risk_issues)


def test_bare_markdown_finding_reference_is_a_citation_and_model_output_is_normalized(
    monkeypatch, workspace_with_data
):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    checked = report.quality_checks(ws, f"# Report\n\n### Finding [{item['id']}]: Duplicate invoices")
    assert "finding_missing_from_report" not in {issue["code"] for issue in checked["issues"]}

    monkeypatch.setattr(
        llm, "chat",
        lambda *args, **kwargs: {"content": f"# Report\n\n### Finding [{item['id']}]: Duplicate invoices"},
    )
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "fake", "model": "fake"})
    generated = report.generate(ws)
    assert f"[Finding {item['id']}](?tab=findings&finding={item['id']})" in generated["markdown"]


@pytest.mark.parametrize(
    "reference",
    [
        "[F-{id}](#f-{id_lower})",
        "[F-{id}](finding:F-{id})",
        "[Finding F-{id}](?tab=findings\\&finding=F-{id})",
    ],
)
def test_linked_markdown_finding_references_satisfy_report_quality(
    workspace_with_data, reference
):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    item = findings.add(ws, complete_finding_payload(rcm, procedure, execution, anchor))
    markdown = reference.format(id=item["id"], id_lower=item["id"].lower())

    checked = report.quality_checks(ws, f"# Report\n\n{markdown}")

    assert "finding_missing_from_report" not in {issue["code"] for issue in checked["issues"]}


def test_editorial_review_degrades_safely_on_wrong_issue_shape(
    monkeypatch, workspace_with_data
):
    ws = workspace_with_data
    ws.report = {"markdown": "# Preliminary internal audit report"}
    ws.save()
    monkeypatch.setattr(
        llm, "chat", lambda *args, **kwargs: {
            "content": json.dumps({"issues": ["unclear wording"]})
        },
    )
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "fake", "model": "fake"},
    )

    reviewed = report.editorial_review(ws)

    assert reviewed["editorial"][0]["code"] == "editorial_unavailable"


def test_finding_and_report_routes(monkeypatch, workspace_with_data):
    ws, rcm, procedure, execution, _analysis, anchor = linked_workspace(workspace_with_data)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": False})
    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}"
    created = client.post(f"{base}/findings", json=complete_finding_payload(rcm, procedure, execution, anchor))
    assert created.status_code == 200
    finding_id = created.json()["id"]
    assert client.get(f"{base}/findings").json()["items"][0]["id"] == finding_id
    generated = client.post(f"{base}/report/generate", json={"use_model": False})
    assert generated.status_code == 200
    assert client.post(f"{base}/report/quality", json={}).status_code == 200
    assert client.post(f"{base}/findings", json={"title": "Old payload", "status": "draft"}).status_code == 400
    assert client.patch(f"{base}/report", json={"status": "final"}).status_code == 400
    assert client.patch(f"{base}/findings/{finding_id}", json={"status": "final"}).status_code == 400
    assert client.delete(f"{base}/findings/{finding_id}").json() == {"ok": True}


def test_removed_artifact_statuses_are_discarded_when_loading(workspace_with_data):
    ws = workspace_with_data
    ws.planning["status"] = "final"
    ws.findings.append({"id": "F-OLD", "title": "Legacy finding", "status": "draft"})
    ws.report = {"status": "final", "markdown": "# Existing report"}
    ws.save()

    loaded = workspaces.load_workspace(ws.id)
    assert "status" not in loaded.planning
    assert "status" not in loaded.findings[0]
    assert "status" not in loaded.report
    loaded.save()

    planning = json.loads((loaded.root / "Planning" / "context.json").read_text(encoding="utf-8"))
    finding = json.loads(next((loaded.root / "Findings").glob("*.json")).read_text(encoding="utf-8"))
    report_artifact = json.loads((loaded.root / "Reports" / "current.json").read_text(encoding="utf-8"))
    assert "status" not in planning
    assert "status" not in finding
    assert "status" not in report_artifact
