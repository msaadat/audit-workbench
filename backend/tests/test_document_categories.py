"""What an engagement holds a document as, read from its opening page.

The category was guessed at intake from the filename. It is now read from page
one by ``documents.categorized``, one stage ahead of the type. Three things these
tests pin, each of which failed silently before:

- the four values partition the corpus, so nothing lands outside both sets;
- an audit run reaches its evidence, rather than scoping itself to the planning
  subset and then reporting satisfied having classified nothing;
- an auditor's category is not overwritten by a rerun.
"""

from __future__ import annotations

import pytest

from app import document_classification as dc
from app import documents, intake, llm, workspaces
from app.agent import runner
from app.agent.capabilities import documents as document_capabilities
from app.agent.workflows import audit as audit_workflow
from app.agent.workflows import documents as documents_workflow
from conftest import FakeAgentLLM, wait_run

pytestmark = pytest.mark.integration

CATEGORY_TAG = "agent:document_category"
CLASSIFY_TAG = "agent:document_classification"

POLICY_TEXT = (
    b"Treasury Policy Manual\n"
    b"All payments above USD 100,000 require two authorised signatories."
)
EVIDENCE_TEXT = (
    b"Payment Instruction\nRef PMT-2025-00074\n"
    b"Beneficiary Crescent Investment Bank\nApproved by E-1042"
)


# --------------------------------------------------------------------------- #
# the partition
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_categories_partition_into_planning_and_evidence():
    """No value may sit outside both sets.

    ``evidence`` and ``other`` once did, and ``other`` was the default: a
    document whose filename intake could not read was neither planning material
    nor transaction evidence, so an audit run gave it no text, no type, no
    schema and no analysis. Nothing said so.
    """

    assert (
        intake.PLANNING_DOCUMENT_CATEGORIES | intake.EVIDENCE_DOCUMENT_CATEGORIES
        == intake.DOCUMENT_CATEGORIES
    )
    assert not (
        intake.PLANNING_DOCUMENT_CATEGORIES & intake.EVIDENCE_DOCUMENT_CATEGORIES
    )


@pytest.mark.unit
def test_document_module_mirrors_the_intake_domain():
    """``documents.CATEGORIES`` is a second copy and must not drift from the first."""

    assert documents.CATEGORIES == set(intake.DOCUMENT_CATEGORIES)


@pytest.mark.unit
def test_intake_proposes_no_category():
    """A filename is not evidence of content, so intake stops answering from one."""

    item = {
        "relative_path": "treasury/payment-instruction-0041.pdf",
        "local_metadata": {"route": "document", "parse_ok": True},
    }
    assert intake.deterministic_classification(item)["document_category"] is None


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def _workspace():
    ws = workspaces.create_workspace("Document categories")
    policy = documents.add_document(ws, "treasury-manual.txt", POLICY_TEXT)
    voucher = documents.add_document(ws, "scan-0041.txt", EVIDENCE_TEXT)
    return workspaces.load_workspace(ws.id), policy, voucher


def test_a_document_arrives_uncategorized():
    ws, policy, voucher = _workspace()
    assert [item.get("category") for item in ws.documents] == ["", ""]
    assert sorted(dc.uncategorized_ids(ws)) == sorted(
        [policy["id"], voucher["id"]]
    )
    assert dc.transaction_evidence(ws) == []


def test_assignment_writes_the_sidecar_and_mirrors_the_entry():
    """Both, deliberately: readiness reads the sidecar, a dozen readers read the entry."""

    ws, _policy, voucher = _workspace()
    dc.assign_category(ws, voucher["id"], "evidence", assigned_by="model")
    ws.save()
    ws = workspaces.load_workspace(ws.id)

    assert dc.category(ws, voucher["id"]) == "evidence"
    entry = next(item for item in ws.documents if item["id"] == voucher["id"])
    assert entry["category"] == "evidence"
    assert [item["id"] for item in dc.transaction_evidence(ws)] == [voucher["id"]]


def test_a_rerun_never_overwrites_an_auditor():
    ws, policy, _voucher = _workspace()
    dc.assign_category(ws, policy["id"], "policy", assigned_by="auditor")
    dc.assign_category(ws, policy["id"], "evidence", assigned_by="model")
    assert dc.category(ws, policy["id"]) == "policy"


def test_assigning_a_type_does_not_drop_the_category():
    """One sidecar holds both answers, so the second write must merge."""

    ws, _policy, voucher = _workspace()
    dc.assign_category(ws, voucher["id"], "evidence", assigned_by="model")
    dc.assign(ws, voucher["id"], "payment_instruction", assigned_by="model")
    assert dc.category(ws, voucher["id"]) == "evidence"
    assert dc.document_type(ws, voucher["id"]) == "payment_instruction"


def test_an_unknown_category_is_refused():
    ws, _policy, voucher = _workspace()
    with pytest.raises(workspaces.WorkspaceError):
        dc.assign_category(ws, voucher["id"], "voucher", assigned_by="model")


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #
def test_an_audit_scope_reaches_evidence():
    """The defect this repairs, stated as its own test.

    ``document_scope_mode: planning`` selects ``_planning_relevant`` documents,
    which is disjoint from the evidence category by construction. Text
    extraction and classification were bounded by it, so an audit run extracted
    no evidence text, classified nothing, and ``schemas_induced`` reported
    satisfied having induced nothing — with both edges into ``rcm_ready``
    satisfied by an empty vocabulary.
    """

    ws, policy, voucher = _workspace()
    dc.assign_category(ws, policy["id"], "policy", assigned_by="model")
    dc.assign_category(ws, voucher["id"], "evidence", assigned_by="model")
    ws.save()
    ws = workspaces.load_workspace(ws.id)

    scope = {"document_scope_mode": "planning", "document_ids": []}
    corpus = document_capabilities.corpus_scope(ws, scope)
    planning = document_capabilities.resolve_document_scope(ws, scope)

    assert voucher["id"] in corpus.document_ids
    assert voucher["id"] not in planning.document_ids
    assert [
        unit.input_payload["document_id"]
        for unit in document_capabilities._classified_units(ws, scope)
    ] == [voucher["id"]]


def test_named_documents_still_win_over_the_corpus():
    """Naming files is the auditor saying what the run is about."""

    ws, policy, _voucher = _workspace()
    scope = {"document_scope_mode": "planning", "document_ids": [policy["id"]]}
    assert document_capabilities.corpus_scope(ws, scope).document_ids == (
        policy["id"],
    )


@pytest.mark.unit
def test_the_audit_graph_waits_for_the_category():
    assert "documents.categorized" in audit_workflow.DEPENDENCIES
    assert audit_workflow.dependencies("documents.types_classified") == (
        "documents.categorized",
    )
    assert "documents.categorized" in audit_workflow.dependencies(
        "planning.rcm_ready"
    )


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #
def _fake(monkeypatch):
    """A model that reads the page it is shown and nothing else."""

    def category(user_view):
        value = "evidence" if "Payment Instruction" in user_view else "policy"
        return {
            "category": value,
            "confidence": "high",
            "rationale": "Read from the opening page.",
        }

    fake = FakeAgentLLM({
        CATEGORY_TAG: category,
        CLASSIFY_TAG: {
            "document_type": "payment_instruction",
            "document_type_other": "",
            "confidence": "high",
            "rationale": "The header reads Payment Instruction.",
        },
    })
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    return fake


@pytest.mark.e2e
def test_a_run_categorizes_then_classifies_only_the_evidence(monkeypatch):
    """The partition holds end to end, and the type is asked of evidence only."""

    ws, policy, voucher = _workspace()
    fake = _fake(monkeypatch)
    created = [policy, voucher]

    run = runner.start_command_run(
        ws, "auto",
        {
            "source": "tab_button",
            "text": "Analyse the documents.",
            "goal_template": "document_analysis",
            "requested_outcomes": ["documents.types_classified"],
            "target_refs": [f"document:{item['id']}" for item in created],
        },
        context={"document_ids": [item["id"] for item in created]},
    )
    record = wait_run(ws, run["id"], timeout=90)
    assert record["status"] == "completed", record.get("error")

    ws = workspaces.load_workspace(ws.id)
    assert dc.category(ws, policy["id"]) == "policy"
    assert dc.category(ws, voucher["id"]) == "evidence"

    # Both documents were categorized; only the evidence was typed. A policy
    # correctly named ``delegation_of_authority`` is still policy, and reading it
    # under voucher fields replaces the narrative planning consumes.
    tags = [call["tag"] for call in fake.calls]
    assert tags.count(CATEGORY_TAG) == 2
    assert tags.count(CLASSIFY_TAG) == 1
    assert dc.document_type(ws, policy["id"]) == ""
    assert dc.document_type(ws, voucher["id"]) == "payment_instruction"
    assert dc.types_for_induction(ws) == ["payment_instruction"]
