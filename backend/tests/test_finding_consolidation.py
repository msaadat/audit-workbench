"""Findings consolidation, phases 3 and 4: the merge, its routes, the redraft,
and the report.

Accepting a suggestion merges members into one lead — references unioned,
severity the highest, absorbed findings marked and kept — and the lead is
redrafted by the finding worker from every member observation. The report
carries leads only and lists what each absorbed as supporting procedures.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import data_tests, finding_consolidation, findings, report, working_papers, workspaces
from app.agent import capabilities as audit_capabilities
from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    finding_draft_scope,
    supplied_size,
    total_supplied_size,
)
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.reporting import FINDING_EXECUTOR, FindingExecutorTarget
from app.agent.workers import WORKERS, WorkerRequest
from app.main import create_app
from app.workspace_transactions import parent_hashes
from test_agent_reporting_consolidation import _commit_finding, _two_drafts
from test_agent_reporting_finding import TEMPLATE, _Gateway, _markdown
from test_finding_coverage import NARRATIVE, _polars_test, _row, _selecting


def _lead_and_member(ws):
    ordered = sorted(ws.findings, key=lambda item: item["title"])
    return ordered[0], ordered[1]


# --------------------------------------------------------------------------- #
# The merge
# --------------------------------------------------------------------------- #
def test_consolidate_unions_references_takes_the_highest_severity_and_marks_the_rest(
    workspace_with_data,
):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.update(ws, member["id"], {"severity": "critical"})
    assert lead["rcm_refs"] != member["rcm_refs"]

    merged = findings.consolidate(
        ws,
        finding_ids=[lead["id"], member["id"]],
        lead_id=lead["id"],
        relation="same_condition",
        title="Invoices paid outside the control",
        root_cause_hypothesis="The match is not enforced.",
    )

    ws = workspaces.load_workspace(ws.id)
    lead = next(item for item in ws.findings if item["id"] == merged["id"])
    member = next(item for item in ws.findings if item["id"] != merged["id"])
    assert set(lead["rcm_refs"]) == {*merged["rcm_refs"]} and len(lead["rcm_refs"]) == 2
    assert len(lead["rcm_semantic_refs"]) == 2
    assert len(lead["test_refs"]) == 2
    assert len(lead["execution_refs"]) == 2
    assert len(lead["evidence_refs"]) == 2
    assert lead["severity"] == "critical"
    assert lead["title"] == "Invoices paid outside the control"
    assert lead["consolidation"]["role"] == "lead"
    assert lead["consolidation"]["members"] == [member["id"]]
    assert lead["consolidation"]["relation"] == "same_condition"
    assert lead["consolidation"]["narrative_pending"] is True
    assert lead["auditor_confirmed"] is False
    # Marked, never deleted: the absorbed finding keeps its own narrative.
    assert member["consolidation"] == {
        **{key: lead["consolidation"][key] for key in ("group_id", "relation", "basis_sha1", "decided_by", "decided_at")},
        "role": "absorbed",
        "into": lead["id"],
    }
    assert member["narrative"] == NARRATIVE.strip()
    assert findings.consolidation_members(ws, lead) == [member]


def test_consolidating_a_confirmed_finding_is_refused_without_the_flag(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.update(ws, member["id"], {"auditor_confirmed": True})

    with pytest.raises(workspaces.WorkspaceError, match="auditor-confirmed"):
        findings.consolidate(ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"], relation="shared_cause")

    merged = findings.consolidate(
        ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"],
        relation="shared_cause", include_confirmed=True,
    )
    assert merged["auditor_confirmed"] is False


def test_an_absorbed_finding_cannot_be_confirmed_and_cannot_be_absorbed_twice(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.consolidate(ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"], relation="shared_cause")

    with pytest.raises(workspaces.WorkspaceError, match="absorbed into"):
        findings.update(ws, member["id"], {"auditor_confirmed": True})
    third = findings.add(ws, {"title": "Third"})
    with pytest.raises(workspaces.WorkspaceError, match="already absorbed"):
        findings.consolidate(ws, finding_ids=[member["id"], third["id"]], lead_id=third["id"], relation="shared_cause")
    # ``consolidation`` changes only through the merge routes.
    with pytest.raises(workspaces.WorkspaceError, match="Unknown finding field"):
        findings.update(ws, lead["id"], {"consolidation": None})


def test_unconsolidate_restores_the_member_and_trims_the_lead(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.consolidate(ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"], relation="shared_cause")

    restored = findings.unconsolidate(ws, member["id"])

    ws = workspaces.load_workspace(ws.id)
    assert "consolidation" not in restored
    lead = next(item for item in ws.findings if item["id"] == lead["id"])
    # The last member gone, the lead is an ordinary finding again; the
    # references it took stay, since they are still true of its condition.
    assert "consolidation" not in lead
    assert len(lead["test_refs"]) == 2
    with pytest.raises(workspaces.WorkspaceError, match="not absorbed"):
        findings.unconsolidate(ws, member["id"])


def test_a_lead_reports_a_support_issue_when_a_members_observation_is_no_longer_an_exception(
    workspace_with_data,
):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.consolidate(ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"], relation="shared_cause")
    ws = workspaces.load_workspace(ws.id)
    lead = next(item for item in ws.findings if item["id"] == lead["id"])
    assert findings.support_issues(ws, lead) == []

    source = next(item for item in ws.observations if item["id"] == member["source_observation_id"])
    source["outcome"] = "resolved"
    ws.save()

    issues = findings.support_issues(ws, lead)
    assert any(f"absorbed finding {member['id']}" in issue for issue in issues)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _suggest(ws, *, relation="same_condition"):
    basis = finding_consolidation.basis_sha1(ws)
    ids = [item["id"] for item in ws.findings]
    finding_consolidation.save(
        ws, basis,
        {
            "groups": [{
                "finding_ids": ids, "lead_finding_id": ids[0], "relation": relation,
                "basis": "entity", "proposed_title": "One issue", "root_cause_hypothesis": "One cause.",
                "rationale": "Same invoices.", "shared_entities": {"invoice_no": ["1001", "1003"]},
            }],
            "singletons": [],
        },
        run_id="run-c",
    )
    return basis, finding_consolidation.load(ws, basis)["groups"][0]


def test_the_consolidation_routes_accept_dismiss_group_and_restore(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    basis, group = _suggest(ws)
    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}/findings"

    listed = client.get(f"{base}/consolidation").json()
    assert listed["current"] is True
    assert listed["undecided"] == 1
    assert [member["id"] for member in listed["suggestion"]["groups"][0]["members"]] == group["finding_ids"]
    assert listed["suggestion"]["groups"][0]["members"][0]["process"] == "Procurement"

    accepted = client.post(f"{base}/consolidation/{group['group_id']}/accept", json={"title": "Chosen title"})
    assert accepted.status_code == 200, accepted.text
    lead = accepted.json()
    assert lead["id"] == group["lead_finding_id"]
    assert lead["title"] == "Chosen title"
    assert lead["consolidation"]["members"] == [group["finding_ids"][1]]
    ws = workspaces.load_workspace(ws.id)
    assert finding_consolidation.load(ws, basis)["groups"][0]["decision"] == "accepted"
    # Accepting does not move the basis, so a second suggestion of the same
    # set is not re-asked and the decision is what the page reads back.
    assert finding_consolidation.basis_sha1(ws) == basis
    assert client.get(f"{base}/consolidation").json()["undecided"] == 0
    # Already decided: a second accept is refused rather than merged twice.
    assert client.post(f"{base}/consolidation/{group['group_id']}/accept", json={}).status_code >= 400

    # The register carries the roles; the absorbed finding is restorable.
    items = client.get(base).json()["items"]
    roles = {item["id"]: (item.get("consolidation") or {}).get("role") for item in items}
    assert roles == {group["finding_ids"][0]: "lead", group["finding_ids"][1]: "absorbed"}
    restored = client.post(f"{base}/{group['finding_ids'][1]}/unconsolidate").json()
    assert restored.get("consolidation") is None

    # A manual group through the same merge, recorded beside the suggestions.
    manual = client.post(
        f"{base.rsplit('/', 1)[0]}/findings/consolidate",
        json={"finding_ids": group["finding_ids"], "lead_finding_id": group["finding_ids"][1], "relation": "shared_cause", "title": "Manual"},
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["consolidation"]["role"] == "lead"
    ws = workspaces.load_workspace(ws.id)
    recorded = finding_consolidation.load(ws, finding_consolidation.basis_sha1(ws))
    manual_group = next(item for item in recorded["groups"] if item["decided_by"] == "auditor" and item["lead_finding_id"] == group["finding_ids"][1])
    assert manual_group["decision"] == "accepted"


def test_dismissing_a_suggestion_is_recorded_against_its_basis(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    basis, group = _suggest(ws)
    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}/findings"

    dismissed = client.post(f"{base}/consolidation/{group['group_id']}/dismiss").json()

    assert dismissed["undecided"] == 0
    assert dismissed["suggestion"]["groups"][0]["decision"] == "dismissed"
    ws = workspaces.load_workspace(ws.id)
    assert all("consolidation" not in item for item in ws.findings)
    # Dismissed stays dismissed until the findings change: the capability is
    # satisfied and expands nothing.
    from app.agent import capabilities as audit_capabilities

    capability = audit_capabilities.REGISTRY.get("findings.consolidated")
    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def test_accepting_a_suggestion_the_findings_have_outgrown_is_refused(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    _basis, group = _suggest(ws)
    findings.add(ws, {"title": "A third finding moves the basis"})
    client = TestClient(create_app())

    response = client.post(f"/api/workspaces/{ws.id}/findings/consolidation/{group['group_id']}/accept", json={})

    assert response.status_code >= 400
    assert "refresh" in response.text


# --------------------------------------------------------------------------- #
# The redraft of a lead from every member observation
# --------------------------------------------------------------------------- #
def test_the_draft_scope_supplies_siblings_and_the_brief_for_a_lead(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.consolidate(
        ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"],
        relation="shared_cause", title="One issue", root_cause_hypothesis="One cause.",
    )
    ws = workspaces.load_workspace(ws.id)
    lead = next(item for item in ws.findings if item["id"] == lead["id"])
    brief = {
        "lead_finding_id": lead["id"], "group_id": lead["consolidation"]["group_id"],
        "relation": "shared_cause", "proposed_title": "One issue", "root_cause_hypothesis": "One cause.",
    }

    scope = finding_draft_scope(
        ws, lead["source_observation_id"],
        sibling_observation_ids=[member["source_observation_id"]],
        consolidation_brief=brief,
    )

    assert [item.metadata["observation_id"] for item in scope.candidates["sibling_observations"]] == [member["source_observation_id"]]
    assert scope.candidates["sibling_observations"][0].source["test"]["title"] == "Right"
    assert len(scope.candidates["sibling_execution_results"]) == 1
    assert len(scope.candidates["sibling_exception_rows"]) == 1
    assert scope.candidates["consolidation_brief"][0].source == brief
    # An ordinary draft supplies none of them.
    plain = finding_draft_scope(ws, lead["source_observation_id"])
    assert plain.candidates["sibling_observations"] == ()
    assert plain.candidates["consolidation_brief"] == ()


def _consolidated_bundle(narrative: str | None = None):
    from test_agent_reporting_finding import EXCEPTION_ROWS

    values = [
        ("observation", "observation:OBS-1", ContextRepresentation("current_artifact"), {"id": "OBS-1", "summary": "Paid before receipt.", "outcome": "exception"}),
        ("rcm_row", "rcm:RCM-1", ContextRepresentation("current_artifact"), {"id": "RCM-1", "risk": "Early payment"}),
        ("test", "datatest:DAT-1", ContextRepresentation("current_artifact"), {"id": "DAT-1", "title": "Payment before goods receipt"}),
        ("execution_result", "datatest:DAT-1", ContextRepresentation("current_artifact"), {"execution_ref": "datatest:DAT-1", "immutable_execution_result": {"exception_count": 1}}),
        ("finding_template", "template:finding", ContextRepresentation("artifact_template"), TEMPLATE),
        ("exception_rows", "datatest:DAT-1:exceptions", ContextRepresentation("datatest_exception_rows"), EXCEPTION_ROWS),
        ("sibling_observations", "observation:OBS-2", ContextRepresentation("current_artifact"), {"id": "OBS-2", "summary": "Vendor inactive at payment.", "outcome": "exception", "test": {"id": "DAT-2", "title": "Vendor status at payment"}}),
        ("sibling_execution_results", "datatest:DAT-2", ContextRepresentation("current_artifact"), {"execution_ref": "datatest:DAT-2", "immutable_execution_result": {"exception_count": 1}}),
        ("consolidation_brief", "finding:F-1:consolidation", ContextRepresentation("current_artifact"), {"lead_finding_id": "F-1", "relation": "shared_cause", "proposed_title": "Payments released outside the control", "root_cause_hypothesis": "Release is not gated."}),
    ]
    items = tuple(
        ContextBundleItem(source_id=source_id, source_ref=source_ref, representation=representation, content=content, supplied_size=supplied_size(content))
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(capability_id="findings.drafted", unit_id="finding:OBS-1", items=items, supplied_size=total_supplied_size(item.supplied_size for item in items))


def _consolidated_request():
    return WorkerRequest(
        worker_id="reporting.finding", capability_id="findings.drafted", unit_id="finding:OBS-1",
        context=_consolidated_bundle(), unit_input={"input_sha1": "x"}, activity={"artifact_refs": ["observation:OBS-1"]},
    )


CONSOLIDATED_NARRATIVE = NARRATIVE.replace(
    "Invoices 1001 and 1003 were paid twice.",
    "**Payment before goods receipt.** Invoice INV2024008 was paid before its goods were received.\n\n"
    "**Vendor status at payment.** The same invoice was released to a vendor whose status was inactive.",
)


def test_a_consolidated_draft_must_name_every_member_stage():
    # The lead's instance alone is not a consolidated finding.
    gateway = _Gateway([_markdown(narrative=NARRATIVE.replace("Invoices 1001 and 1003 were paid twice.", "**Payment before goods receipt.** Paid early.")), _markdown(narrative=CONSOLIDATED_NARRATIVE)])

    result = WORKERS.execute(_consolidated_request(), gateway)

    assert result.repaired is True
    assert "'Vendor status at payment' is not named" in gateway.calls[1]["user"]
    assert "Vendor status at payment" in result.proposal["finding"]["narrative"]
    # The brief and the siblings reach the prompt, named as the system prompt names them.
    sent = json.loads(gateway.calls[0]["user"])
    assert sent["CONSOLIDATION BRIEF"]["proposed_title"] == "Payments released outside the control"
    assert [item["id"] for item in sent["SIBLING OBSERVATIONS"]] == ["OBS-2"]
    assert "SIBLING OBSERVATIONS" in gateway.calls[0]["system"]


def test_an_ordinary_draft_carries_no_sibling_keys():
    from test_agent_reporting_finding import _request

    gateway = _Gateway([_markdown()])
    WORKERS.execute(_request(), gateway)
    sent = json.loads(gateway.calls[0]["user"])
    assert "SIBLING OBSERVATIONS" not in sent
    assert "CONSOLIDATION BRIEF" not in sent


def test_a_lead_awaiting_redraft_is_outstanding_work_for_the_capability(
    workspace_with_data,
):
    """The merge leaves the lead's narrative pending, and the capability has
    to say so.

    Readiness and unit expansion must name the same work: readiness reporting
    ``satisfied`` here made the scheduler reuse ``findings.drafted`` without
    ever asking for units, so the redraft run completed having done nothing
    and the lead kept one member's narrative.
    """
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    capability = audit_capabilities.REGISTRY.get("findings.drafted")
    assert capability.readiness(ws, {}).state == "satisfied"

    findings.consolidate(
        ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"],
        relation="shared_cause",
    )
    ws = workspaces.load_workspace(ws.id)
    lead = next(item for item in ws.findings if item["id"] == lead["id"])
    assert lead["consolidation"]["narrative_pending"] is True

    readiness = capability.readiness(ws, {})
    assert readiness.state == "missing"
    assert readiness.details["pending_redraft"] == 1
    assert any("awaits redrafting" in reason for reason in readiness.reasons)

    # Named or not, the pending lead expands — and only the lead's own
    # observation does, since the member is absorbed.
    for scope in ({}, {"target_refs": [f"finding:{lead['id']}"]}):
        units = capability.expand_units(ws, scope)
        assert [unit.parent_refs[0] for unit in units] == [
            f"observation:{lead['source_observation_id']}"
        ], scope


def test_the_executor_redrafts_a_lead_in_place_keeping_its_unioned_references(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.consolidate(ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"], relation="shared_cause")
    ws = workspaces.load_workspace(ws.id)
    lead = next(item for item in ws.findings if item["id"] == lead["id"])
    observation_id = lead["source_observation_id"]
    request = ExecutorRequest(
        executor_id="reporting.finding", capability_id="findings.drafted", unit_id=f"finding:{observation_id}",
        proposal={"finding": {"title": "Redrafted lead", "severity": "high", "narrative": CONSOLIDATED_NARRATIVE, "cause_pending": False}},
        expected_revision=ws.revision, expected_parents=parent_hashes(ws, [f"observation:{observation_id}"]),
        activity={"artifact_refs": [f"observation:{observation_id}"]},
    )
    target = FindingExecutorTarget(ws, "run-redraft", observation_id, named_by_request=True, lead_finding_id=lead["id"])
    assert FINDING_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    receipt = EXECUTORS.execute(request, target)

    committed = next(item for item in target.workspace.findings if item["id"] == lead["id"])
    assert receipt.output["id"] == lead["id"]
    assert committed["title"] == "Redrafted lead"
    assert committed["narrative"] == CONSOLIDATED_NARRATIVE
    assert committed["semantic_id"] == f"finding:consolidated:{lead['consolidation']['group_id']}"
    assert len(committed["test_refs"]) == 2
    assert committed["consolidation"]["members"] == [member["id"]]
    assert committed["consolidation"]["narrative_pending"] is False
    assert len(target.workspace.findings) == 2
    assert FINDING_EXECUTOR.reconciler(request, target).disposition == "already_applied"


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def test_the_report_carries_the_lead_only_and_lists_its_supporting_procedures(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.consolidate(ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"], relation="shared_cause", title="One issue")
    findings.update(ws, lead["id"], {"auditor_confirmed": True})
    ws = workspaces.load_workspace(ws.id)

    context = report.build_context(ws)
    assert [item["id"] for item in context["findings"]] == [lead["id"]]
    assert context["draft_findings_excluded"] == []
    assert context["absorbed_findings"] == [{"id": member["id"], "into": lead["id"]}]
    assert context["findings"][0]["supporting_procedures"][0]["id"] == member["id"]

    markdown = report.deterministic_markdown(ws, context)
    assert "Supporting procedures" in markdown
    assert member["title"] in markdown
    # The absorbed finding is neither a draft the report lacks nor a row of its own.
    codes = {issue["code"]: issue for issue in report.quality_checks(ws, markdown)["issues"]}
    assert "finding_draft" not in codes
    assert "absorbed_finding_confirmed" not in codes

    # The row's working paper still shows what its own test found.
    paper = working_papers.generate_rcm(ws, member["rcm_refs"][0])
    assert f"consolidated into {lead['id']}" in paper["markdown"]


def test_the_advisory_consolidation_check_fires_and_clears(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    basis, group = _suggest(ws)

    fired = [issue for issue in report.quality_checks(ws)["issues"] if issue["code"] == "unreviewed_consolidation"]
    assert len(fired) == 1
    assert fired[0]["severity"] == "warning"
    assert set(fired[0]["refs"]) == {f"finding:{value}" for value in group["finding_ids"]}

    finding_consolidation.decide(ws, basis, group["group_id"], "dismissed")
    assert not [issue for issue in report.quality_checks(ws)["issues"] if issue["code"] == "unreviewed_consolidation"]


def test_a_confirmed_absorbed_finding_blocks_the_report(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    lead, member = _lead_and_member(ws)
    findings.consolidate(ws, finding_ids=[lead["id"], member["id"]], lead_id=lead["id"], relation="shared_cause")
    # Written past the routes, which refuse it: the report must still not
    # verify over it.
    absorbed = next(item for item in ws.findings if item["id"] == member["id"])
    absorbed["auditor_confirmed"] = True
    ws.save()

    issues = {issue["code"]: issue for issue in report.quality_checks(ws)["issues"]}
    assert issues["absorbed_finding_confirmed"]["severity"] == "error"
    assert report.build_context(ws)["findings"] == []
