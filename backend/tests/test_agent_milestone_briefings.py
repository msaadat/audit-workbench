"""Milestone briefings: what a stage established, not what it did.

A milestone used to describe the machine — planning fields populated, sections
drafted. These pin the projection an auditor actually reads: the two or three
things worth saying out loud when handing the work over, every one of them
derived from durable local state rather than asserted.
"""

from types import SimpleNamespace

from app import document_analysis
from app.agent import audit_execution as audit


class _Workspace:
    """Only the surface the briefing helpers touch."""

    def __init__(self, documents=(), rcm=(), findings=(), planning=None):
        self.documents = list(documents)
        self.rcm = list(rcm)
        self.findings = list(findings)
        self.planning = dict(planning or {})


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


APM = """# Audit Planning Memorandum

## Key risks and planned response

### Authorisation against the approval matrix

Approvals may exceed stated limits.

### Completeness of the recorded population

Transactions may be absent entirely.

## Planning assumptions and matters reported

Where information was not available:

- The approved version of the Financial Approval Matrix was not provided; the
  extract carries no version or effective date.
- No explicit materiality threshold was provided.
"""


def _apm_milestone(workspace, units=()) -> dict:
    """Project the planning milestone for a finished APM stage."""

    execution = audit.AuditWorkflowExecution.__new__(audit.AuditWorkflowExecution)
    return execution.milestone_projection(
        workspace,
        {"id": "run-milestone", "planning_changes": {"apm_updated": 1}},
        # The projection reads only the capability's id; a registered capability
        # would carry a scheduler's worth of unused bindings.
        SimpleNamespace(id="planning.apm_ready"),
        {"units": list(units)},
    )


def _metric(milestone: dict, label: str):
    return next(
        (item["value"] for item in milestone["metrics"] if item["label"] == label),
        None,
    )


def test_the_planning_milestone_describes_the_memorandum_it_filed():
    """The row's artifact is the APM, so the row reads the APM.

    It used to summarise the governing documents' *analysis* instead, which put
    another stage's notes about the client's own minutes under a headline saying
    the memorandum was ready — where they read as defects in the memorandum.
    """
    workspace = _Workspace(_documents(), planning={"apm_markdown": APM})

    milestone = _apm_milestone(workspace)

    assert milestone["summary"] == (
        "Plans the engagement from 2 policy documents and 1 set of minutes, and "
        "sets a planned response against 2 risks. 2 matters are recorded to "
        "confirm before the plan is relied on."
    )
    assert _metric(milestone, "Risks assessed") == 2
    assert _metric(milestone, "Matters to confirm") == 2


def test_the_planning_highlights_are_what_the_memorandum_left_open():
    workspace = _Workspace(_documents(), planning={"apm_markdown": APM})

    highlights = _apm_milestone(workspace)["highlights"]

    assert [item["artifact_ref"] for item in highlights] == [
        "planning:apm", "planning:apm",
    ]
    # A matter written as one sentence splits at its semicolon, so the highlight
    # is a line to scan with the reason under it rather than one long label.
    assert highlights[0]["label"] == (
        "The approved version of the Financial Approval Matrix was not provided."
    )
    assert highlights[0]["detail"] == (
        "The extract carries no version or effective date."
    )
    assert all(item["severity"] == "warning" for item in highlights)


def test_a_memorandum_with_no_matters_section_is_not_reported_as_having_none():
    memo = "# APM\n\n## Key risks and planned response\n\n### Payables\n\nRisk.\n"
    workspace = _Workspace(_documents(), planning={"apm_markdown": memo})

    milestone = _apm_milestone(workspace)

    assert milestone["summary"].endswith(
        "It has no section for matters left open, so nothing was recorded as "
        "outstanding."
    )
    # Reported as 0, that metric would be a claim this memorandum never made.
    assert _metric(milestone, "Matters to confirm") is None


def test_a_risk_assessment_argued_as_prose_says_so_rather_than_counting_zero():
    """Prose is a legitimate way to argue a risk assessment.

    It is also the shape the RCM cannot build rows from, so a reader deciding
    whether to run the matrix next needs to know before they do.
    """
    memo = (
        "# APM\n\n## Key risks and planned response\n\n"
        + "Approvals may exceed the limits the entity states, the recorded "
        "population may be incomplete, and incompatible duties may sit with one "
        "person. Each will be tested substantively against the transactions "
        "received rather than by reliance on controls, because no control has "
        "been walked through and none has been evidenced as operating.\n"
    )
    workspace = _Workspace(_documents(), planning={"apm_markdown": memo})

    milestone = _apm_milestone(workspace)

    assert "argued as prose under \u201cKey risks and planned response\u201d" in (
        milestone["summary"]
    )
    # Prose is not a defect; a memorandum that assessed nothing at all is.
    assert milestone["status"] == "completed"


def test_a_memorandum_that_assesses_no_risk_is_not_reported_as_clean():
    workspace = _Workspace(_documents(), planning={"apm_markdown": "# APM\n"})

    milestone = _apm_milestone(workspace)

    assert milestone["status"] == "completed_with_issues"
    assert milestone["headline"] == (
        "Audit planning memorandum ready — no risk assessed"
    )


def test_the_planning_highlights_stop_at_the_milestone_budget():
    memo = "# APM\n\n## Planning assumptions and matters reported\n\n" + "".join(
        f"- Matter {index} was not provided.\n" for index in range(6)
    )
    workspace = _Workspace(_documents(), planning={"apm_markdown": memo})

    assert len(_apm_milestone(workspace)["highlights"]) == audit.HIGHLIGHT_LIMIT


# --------------------------------------------------------------------------- #
# The matrix briefing
# --------------------------------------------------------------------------- #
def _row(rcm_id, rating, process="Invoice matching", risk=None, control="Finance matches invoices to the order."):
    return {
        "id": rcm_id,
        "process": process,
        "risk": risk if risk is not None else f"A {rating} risk to the cycle. More prose follows.",
        "risk_rating": rating,
        "control": control,
    }


def _rcm_milestone(rows) -> dict:
    execution = audit.AuditWorkflowExecution.__new__(audit.AuditWorkflowExecution)
    return execution.milestone_projection(
        _Workspace(rcm=rows),
        {"id": "run-milestone", "planning_changes": {}},
        SimpleNamespace(id="planning.rcm_ready"),
        {"units": []},
    )


def _tally(milestone: dict) -> dict:
    return {item["label"]: item["value"] for item in milestone["stats"]}


def test_a_matrix_states_its_distribution_before_any_single_row():
    """An auditor reads a matrix as "one critical, eight high" first.

    Three rows quoted at equal weight — all a milestone built only from
    highlights can say — cannot carry that, and made the row look like every
    other artifact's.
    """
    rows = [_row("RCM-1", "critical")] + [
        _row(f"RCM-{index}", "high") for index in range(2, 10)
    ] + [_row("RCM-10", "medium")]

    milestone = _rcm_milestone(rows)

    assert _tally(milestone) == {"critical": 1, "high": 8, "medium": 1}
    # The counts live in the tally now, so the sentence says what it covers.
    assert milestone["summary"].startswith("10 rows covering 1 process,")


def test_the_tally_states_a_severe_tier_at_zero_and_omits_an_absent_mild_one():
    # "No critical risks" is a finding about the matrix. "No low risks" is not.
    milestone = _rcm_milestone([_row("RCM-1", "medium")])

    assert _tally(milestone) == {"critical": 0, "high": 0, "medium": 1}


def test_the_critical_rows_are_read_out_and_the_rest_are_counted():
    rows = [_row("RCM-1", "critical", risk="Management may override the controls. Prose.")] + [
        _row(f"RCM-{index}", "high", process=f"Process {index}") for index in range(2, 10)
    ]

    highlights = _rcm_milestone(rows)["highlights"]

    assert len(highlights) == 2
    assert highlights[0]["severity"] == "error"
    assert highlights[0]["label"] == "Management may override the controls."
    # The row's own control is what it proposes to rely on, which is worth more
    # than repeating a rating the tally already carries.
    assert highlights[0]["detail"] == (
        "Invoice matching — Finance matches invoices to the order."
    )
    assert highlights[1]["label"] == "8 further rows rated high"
    assert highlights[1]["detail"].startswith("Process 2, Process 3")


def test_a_matrix_with_no_critical_row_leads_with_its_high_ones():
    # An empty list of exemplars is worse than a slightly less severe one.
    rows = [_row(f"RCM-{index}", "high", process=f"Process {index}") for index in range(1, 6)]

    highlights = _rcm_milestone(rows)["highlights"]

    assert [item["label"] for item in highlights][:2] == [
        "A high risk to the cycle.", "A high risk to the cycle.",
    ]
    assert highlights[-1]["label"] == "3 further rows rated high"


def test_incomplete_rows_are_one_line_and_never_crowd_out_the_critical_ones():
    """They used to take every slot a milestone had.

    The projection built three incomplete highlights and three severe ones and
    handed over six, and the writer keeps the first three — so a matrix with
    three blank rows filed a milestone that never mentioned its critical risk.
    """
    rows = [
        _row(f"RCM-b{index}", "medium", risk="", control="") for index in range(3)
    ] + [_row("RCM-1", "critical", risk="Management may override the controls. Prose.")]

    milestone = _rcm_milestone(rows)
    highlights = milestone["highlights"]

    assert milestone["status"] == "completed_with_issues"
    assert highlights[0]["label"] == "3 rows have no risk or control description"
    assert any(item["label"] == "Management may override the controls." for item in highlights)
    assert _tally(milestone)["incomplete"] == 3


def test_a_matrix_with_nothing_to_grade_states_no_tally_rather_than_zeroes():
    milestone = _rcm_milestone([])

    assert milestone["stats"] == [{"label": "critical", "value": 0, "severity": "error"},
                                  {"label": "high", "value": 0, "severity": "warning"}]
    assert milestone["highlights"] == []


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


# --------------------------------------------------------------------------- #
# Reading a bold-led observation
# --------------------------------------------------------------------------- #
BOLD_NOTES = """## Review observations
- **Governance metadata is incomplete.** The supplied extract does not identify the issuer or version. This matters because the authority cannot be verified. [C1]
- **Action owner and deadlines are not stated.** The agreed review scope is documented. [C4] [C5]
"""


def test_a_bold_lead_becomes_the_label_without_its_markers(monkeypatch):
    _stub_notes(monkeypatch, {"d_sop": BOLD_NOTES})

    observations = document_analysis.audit_observations(_Workspace(), "d_sop")

    # The bold lead is the analysis' own summary of the observation, which is a
    # better label than the first sentence of the prose after it.
    assert observations[0]["statement"] == "Governance metadata is incomplete."
    assert observations[0]["detail"].startswith("The supplied extract does not identify")
    # Labels render as plain text, so emphasis markers must never reach them.
    assert "**" not in observations[0]["statement"]
    assert "**" not in observations[0]["detail"]
    assert observations[1]["statement"] == "Action owner and deadlines are not stated."


def test_emphasis_inside_a_plain_bullet_is_stripped_too(monkeypatch):
    _stub_notes(monkeypatch, {
        "d_sop": "## Notes\n- The SOP has no *effective date* and no `owner`. Obtain the record. [C1]\n",
    })

    observation = document_analysis.audit_observations(_Workspace(), "d_sop")[0]

    assert observation["statement"] == "The SOP has no effective date and no owner."
