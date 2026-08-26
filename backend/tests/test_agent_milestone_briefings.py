"""Milestone briefings: what a stage established, not what it did.

A milestone used to describe the machine — planning fields populated, sections
drafted. These pin the projection an auditor actually reads: the two or three
things worth saying out loud when handing the work over, every one of them
derived from durable local state rather than asserted.
"""

from app import document_analysis
from app.agent import audit_execution as audit


class _Workspace:
    """Only the surface the briefing helpers touch."""

    def __init__(self, documents=(), rcm=(), findings=()):
        self.documents = list(documents)
        self.rcm = list(rcm)
        self.findings = list(findings)


SOP_NOTES = """## Drafting and governance observations
- Governance metadata is not stated in the supplied document: owner, version and approval date. This matters for determining authorization and currency; obtain the controlled-document header. [C1]
- The process refers to an Authority Matrix that is not included in the extract. This matters because approval requirements cannot be determined alone; obtain the governing matrix. [C4]
"""

MATRIX_NOTES = """## Review Observations

- The matrix specifies approval amounts but not the transaction types. This matters for consistent interpretation; obtain the accompanying procedure. [C2]
"""

QUIET_NOTES = """## Audit notes

No specific drafting or control-design observations were identified from the supplied text.
"""


def _documents():
    return [
        {"id": "d_sop", "source": "Procurement SOP Extracts.docx", "title": "procurement_sop_extracts", "category": "policy"},
        {"id": "d_matrix", "source": "Financial Approval Matrix.docx", "title": "financial_approval_matrix", "category": "policy"},
        {"id": "d_min", "source": "Minutes of Meeting - CFO.docx", "title": "minutes_of_meeting_cfo", "category": "minutes"},
        {"id": "d_vou", "source": "PO2024004_Purchase_Order.pdf", "title": "po2024004", "category": "voucher"},
    ]


def _stub_notes(monkeypatch, notes: dict[str, str]):
    monkeypatch.setattr(
        document_analysis,
        "effective_audit_notes",
        lambda workspace, document_id: notes.get(document_id, ""),
    )


# --------------------------------------------------------------------------- #
# Reading the observations back out of an analysis
# --------------------------------------------------------------------------- #
def test_audit_observations_splits_a_bullet_into_statement_and_why(monkeypatch):
    _stub_notes(monkeypatch, {"d_sop": SOP_NOTES})

    observations = document_analysis.audit_observations(_Workspace(), "d_sop")

    assert len(observations) == 2
    first = observations[0]
    assert first["statement"] == (
        "Governance metadata is not stated in the supplied document: owner, "
        "version and approval date."
    )
    assert first["detail"].startswith("This matters for determining authorization")
    # Citation anchors read as noise in a one-line label.
    assert "[C1]" not in first["statement"]


def test_audit_observations_reports_nothing_when_the_notes_found_nothing(monkeypatch):
    _stub_notes(monkeypatch, {"d_vou": QUIET_NOTES})

    assert document_analysis.audit_observations(_Workspace(), "d_vou") == []


# --------------------------------------------------------------------------- #
# The planning briefing
# --------------------------------------------------------------------------- #
def test_planning_documents_exclude_transaction_evidence():
    documents = audit._planning_documents(_Workspace(_documents()))

    assert [item["id"] for item in documents] == ["d_matrix", "d_sop", "d_min"]
    # A voucher is fieldwork evidence, not something planning rests on.
    assert all(item["category"] != "voucher" for item in documents)


def test_category_breakdown_describes_the_material_not_the_count():
    assert audit._category_breakdown(_documents()[:3]) == (
        "2 policy documents and 1 set of minutes"
    )
    assert audit._category_breakdown([]) == ""


def test_observation_highlights_take_one_note_per_document(monkeypatch):
    _stub_notes(monkeypatch, {
        "d_sop": SOP_NOTES, "d_matrix": MATRIX_NOTES, "d_min": QUIET_NOTES,
    })
    workspace = _Workspace(_documents())
    documents = audit._planning_documents(workspace)

    highlights = audit._observation_highlights(
        audit._document_observations(workspace, documents)
    )

    # The SOP has two observations; breadth across sources beats depth in one.
    assert len(highlights) == 2
    assert [item["artifact_ref"] for item in highlights] == [
        "document:d_matrix", "document:d_sop",
    ]
    # The source is named, so two documents raising the same gap read as a
    # pattern rather than as one sentence printed twice.
    assert highlights[0]["detail"].startswith("Financial Approval Matrix.docx — ")
    assert highlights[1]["detail"].startswith("Procurement SOP Extracts.docx — ")
    assert all(item["severity"] == "warning" for item in highlights)


def test_observation_highlights_stop_at_the_milestone_budget(monkeypatch):
    documents = [
        {"id": f"d{index}", "source": f"Policy {index}.docx", "category": "policy"}
        for index in range(6)
    ]
    _stub_notes(monkeypatch, {item["id"]: MATRIX_NOTES for item in documents})
    workspace = _Workspace(documents)

    highlights = audit._observation_highlights(
        audit._document_observations(workspace, audit._planning_documents(workspace))
    )

    assert len(highlights) == audit.HIGHLIGHT_LIMIT


# --------------------------------------------------------------------------- #
# Naming the things a briefing points at
# --------------------------------------------------------------------------- #
def test_rcm_label_leads_with_the_risk_it_states():
    row = {
        "id": "RCM-01",
        "process": "Invoice matching",
        "risk": "Invoices may be paid without agreement to the approved purchase order. Further prose.",
        "control": "Finance matches invoices.",
    }

    assert audit._rcm_label(row) == (
        "Invoices may be paid without agreement to the approved purchase order."
    )


def test_rcm_label_falls_back_through_to_the_row_id():
    assert audit._rcm_label({"id": "RCM-02"}) == "RCM-02"


def test_lead_sentence_reads_one_section_of_a_finding_narrative():
    narrative = (
        "## Condition\n\nOf the 118 invoice records examined, 1 was unmatched. "
        "A second sentence.\n\n## Criteria\n\nThe SOP requires matching.\n"
    )

    assert audit._lead_sentence(narrative, section="Condition") == (
        "Of the 118 invoice records examined, 1 was unmatched."
    )
    assert audit._lead_sentence(narrative, section="Criteria") == (
        "The SOP requires matching."
    )
    assert audit._lead_sentence(narrative, section="Cause") == ""
