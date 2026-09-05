"""Focused tests for the registered ``planning.rcm`` model worker (P7C.2).

The worker owns only the RCM prompt, bundle-to-message transformation, response
schema, and the engagement quality gate. It is exercised with constructed
bundles and a gateway stub and must not touch a workspace, store, resolver, or
scheduler.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import (
    WORKERS,
    WorkerRepairSeed,
    WorkerRequest,
    WorkerResponseValidationError,
    WorkerRunError,
)
from app.agent.workers import planning


class _Gateway:
    """Scripted rows calls, with the attributes call answered by agreement.

    ``responses`` is the risks call's script, in order — what every test in this
    file was written against. The controls and attributes calls are real calls
    in every run, and answering them by hand in each test would say nothing
    about the test, so by default each returns exactly what the scripted rows
    already carry. A test about one of them passes ``controls`` or
    ``attributes``, in which case ``None`` at a position means "answer by
    agreement here".
    """

    def __init__(self, responses, controls=None, attributes=None):
        self.responses = list(responses)
        self.controls = None if controls is None else list(controls)
        self.attributes = None if attributes is None else list(attributes)
        self.rows_seen: list[dict] = []
        self.calls = []

    def complete(
        self, system, user, activity=None, *, attempt=1, conversation=None
    ):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "activity": activity,
                "attempt": attempt,
                "conversation": conversation,
            }
        )
        if system == planning.RCM_CONTROLS_SYSTEM:
            scripted = self.controls.pop(0) if self.controls else None
            if scripted is not None:
                return scripted
            return json.dumps({"controls": self._agreed_controls(user)})
        if system == planning.RCM_ATTRIBUTES_SYSTEM:
            scripted = self.attributes.pop(0) if self.attributes else None
            # ``None`` in the script means "answer by agreement here", so a
            # test about the second attributes call need not spell out the
            # first.
            if scripted is not None:
                return scripted
            return json.dumps({"attributes": self._agreed(user)})
        response = self.responses.pop(0)
        try:
            parsed = json.loads(response)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
            self.rows_seen = [
                row for row in parsed["rows"] if isinstance(row, dict)
            ]
        return response

    def _agreed_controls(self, user: str) -> list[dict]:
        """Echo whatever the scripted rows said their control fields are."""

        payload = json.loads(user)
        rows = payload.get("RISKS") or payload.get("CONTROLS TO CORRECT") or []
        answers = []
        for row in rows:
            index = int(row["row_index"])
            scripted = (
                self.rows_seen[index - 1] if index <= len(self.rows_seen) else {}
            )
            entry = {"row_index": index}
            for key in planning._RCM_CONTROL_FIELDS:
                if key in scripted:
                    entry[key] = scripted[key]
            answers.append(entry)
        return answers

    def _agreed(self, user: str) -> list[dict]:
        """Echo whatever the scripted rows said their attributes are."""

        payload = json.loads(user)
        if "ATTRIBUTES TO CORRECT" in payload:
            return [
                {
                    "row_index": item["row_index"],
                    "control_attributes": item.get("current_attributes") or [],
                }
                for item in payload["ATTRIBUTES TO CORRECT"]
            ]
        answers = []
        for row in payload.get("ROWS") or []:
            index = int(row["row_index"])
            scripted = (
                self.rows_seen[index - 1] if index <= len(self.rows_seen) else {}
            )
            if "control_attributes" in scripted:
                answers.append(
                    {
                        "row_index": index,
                        "control_attributes": scripted["control_attributes"],
                    }
                )
        return answers


def _bundle(*, current_rows=None, profiles=(), metadata=(), apm=None):
    values = [
        (
            "rcm_template",
            "template:rcm",
            ContextRepresentation("artifact_template"),
            "# Risk and control matrix\n",
        ),
        (
            "rcm_controls_template",
            "template:rcm_controls",
            ContextRepresentation("artifact_template"),
            "# Control guidance\n",
        ),
        (
            "rcm_attributes_template",
            "template:rcm_attributes",
            ContextRepresentation("artifact_template"),
            "# Control attribute guidance\n",
        ),
        (
            "current_apm",
            "planning:apm",
            ContextRepresentation("current_artifact"),
            apm or "# APM\n\nAssess procurement approvals.",
        ),
        (
            "planning_context",
            "planning:context",
            ContextRepresentation("planning_context"),
            {"context": {"objective": "Assess procurement approvals"}},
        ),
    ]
    for row in current_rows or []:
        values.append(
            (
                "current_rcm",
                f"rcm:{row['id']}",
                ContextRepresentation("current_artifact"),
                row,
            )
        )
    for profile in profiles:
        values.append(
            (
                "table_profiles",
                f"table:{profile['table']}",
                ContextRepresentation("table_profile"),
                profile,
            )
        )
    for table in metadata:
        values.append(
            (
                "table_metadata",
                f"table:{table['table']}",
                ContextRepresentation("table_metadata"),
                table,
            )
        )
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=representation,
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="planning.rcm",
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        context=bundle or _bundle(),
        unit_input={"input_sha1": "rcm-input"},
        activity={"artifact_refs": ["planning:apm"]},
    )


def _row(**overrides):
    row = {
        "operation": "create",
        "process": "Accounts payable",
        "risk": "Duplicate payments are processed",
        "risk_rating": "high",
        "business_cycle": "",
        "control_attributes": [
            {
                "key": "duplicate_payment_prevention",
                "assertion": "Operational",
                "requirement": "Duplicate invoice validation operates before payment.",
                "evidence_kind": "manual_inspection",
            }
        ],
        "control": "Duplicate invoice validation",
        "control_type": "preventive",
        "test_procedure": "Test invoice and amount duplicates.",
        "new_risk_reason": "No existing RCM row covers duplicate payments.",
    }
    row.update(overrides)
    return row


def _cycle_attribute(**overrides):
    """A transaction-cycle attribute as the judgment pass returns it.

    The judgment pass states the evidence *strategy* only; registry, record
    kinds, and comparisons come from the second pass.
    """

    attribute = {
        "key": "three_way_match",
        "assertion": "Accuracy",
        "requirement": "Invoices agree to purchase and receipt records.",
        "evidence_kind": "transaction_cycle",
    }
    attribute.update(overrides)
    return attribute


def test_rcm_worker_uses_only_bundle_and_returns_validated_rows():
    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway)

    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Duplicate payments are processed"
    ]
    assert gateway.calls[0]["system"] == planning.RCM_RISKS_SYSTEM
    assert gateway.calls[0]["attempt"] == 1
    assert (
        gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == "rcm_risks"
    )
    # Then the control, then the attributes, each its own call.
    assert [call["activity"]["context_metrics"]["worker_kind"] for call in gateway.calls] == [
        "rcm_risks", "rcm_controls", "rcm_attributes",
    ]
    # The APM narrative and template reach the model.
    assert "Assess procurement approvals" in gateway.calls[0]["user"]
    assert "Risk and control matrix" in gateway.calls[0]["user"]


_INVOICES = {
    "table": "invoice_data",
    "rows": 118,
    "columns": [
        {"name": "INVOICE_ID"},
        {"name": "VENDOR_INVOICE_NUMBER"},
        {"name": "INVOICE_AMOUNT"},
    ],
}
_UNRELATED = {
    "table": "office_locations",
    "rows": 12,
    "columns": [{"name": "SITE_NAME"}, {"name": "FLOOR_AREA"}],
}


def test_a_row_claiming_no_population_answers_it_is_reported_not_refused():
    """The defect is untestability; refusing the matrix over it was worse.

    A row with no ``tabular_population`` attribute makes the test worker
    withhold every table schema, so ``data`` never enters its allowed variants
    and no data test can be generated for it. Worth an auditor's attention —
    and not something to fail a matrix over, because the same lexical signal
    calls competitive bidding and ERP configuration testable when no column in
    the engagement holds a quotation or a role assignment.
    """
    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(_bundle(profiles=[_INVOICES])), gateway)

    assert len(result.proposal["rows"]) == 1
    (flagged,) = planning.untested_population_rows([_INVOICES], result.proposal["rows"])
    assert "invoice_data" in flagged


def test_a_row_the_populations_do_not_bear_on_is_not_reported():
    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(_bundle(profiles=[_UNRELATED])), gateway)

    assert result.proposal["rows"][0]["control_attributes"][0]["evidence_kind"] == (
        "manual_inspection"
    )
    assert planning.untested_population_rows([_UNRELATED], result.proposal["rows"]) == []


def test_a_reference_table_is_not_a_population_a_row_can_be_tested_against():
    # Four rows of approval limits are read *through*, not tested. Without this
    # the matrix answered every row whose risk mentioned approval, because the
    # table's own name carries the word.
    matrix = {
        "table": "financial_approval_matrix",
        "rows": 4,
        "columns": [{"name": "ROLE"}, {"name": "MAX_APPROVAL_AMOUNT"}],
    }
    row = _row(risk="Approval limits may be exceeded", control="Approval matrix")

    assert planning.untested_population_rows([matrix], [row]) == []


def test_a_row_that_already_claims_the_population_passes_the_gate():
    row = _row(
        control_attributes=[
            {
                "key": "duplicate_invoice_number",
                "assertion": "Completeness",
                "requirement": "Vendor invoice numbers are unique across the population.",
                "evidence_kind": "tabular_population",
            }
        ]
    )
    gateway = _Gateway([json.dumps({"rows": [row]})])

    result = WORKERS.execute(_request(_bundle(profiles=[_INVOICES])), gateway)

    assert len(result.proposal["rows"]) == 1


_PLANNED = """# APM

## Key risks and planned response

### Accounts payable

Duplicate payment risk.

### Goods receipt and three-way match

Payment before receipt risk.
"""


def test_a_risk_theme_no_row_owns_is_reported_without_refusing_the_matrix():
    """The matrix is the memo's risk assessment made testable.

    A theme planned for and never converted into a control is how goods receipt
    came to have no owning row while the memo raised it. That is worth telling
    the auditor and — since the check reads the memo's markdown, and twice threw
    away a matrix that did answer the theme when the markdown defeated it — not
    worth refusing a matrix over. The matrix commits; the theme is reported.
    """
    gateway = _Gateway([json.dumps({"rows": [_row()]})] * 2)

    result = WORKERS.execute(_request(_bundle(apm=_PLANNED)), gateway)

    assert len(result.proposal["rows"]) == 1
    assert planning.unowned_themes(_PLANNED, [_row()]) == [
        "Goods receipt and three-way match"
    ]


_BULLETED = """# APM

## Fraud risk and management override

Management override is presumed present.

* **Circumvention through transaction splitting:** A party avoiding thresholds
  could divide purchases across requisitions.

* **Unauthorised payment destinations:** Duplicate bank-account assignments
  create an opportunity for payments to be directed incorrectly.

## Key risks and planned response

### Accounts payable

Duplicate payment risk.
"""


def test_a_theme_is_a_theme_whether_it_is_a_heading_or_a_bold_bullet():
    """A memo's formatting is not a statement about its risk assessment.

    A regenerated APM moved its fraud risks from sub-headings to bold bullets
    and this check went from enforcing six themes to enforcing none, silently.
    """
    themes = planning.planned_risk_themes(_BULLETED)

    assert themes == [
        "Circumvention through transaction splitting",
        "Unauthorised payment destinations",
        "Accounts payable",
    ]


def test_bullets_under_a_headed_section_are_detail_not_themes():
    # The shipped template puts a seven-lens checklist under each process, each
    # entry bold-led. Reading both levels turns one theme into eight.
    memo = (
        "# APM\n\n## Key risks and planned response\n\n### Accounts payable\n\n"
        "* **Authorisation against limits:** considered and applicable.\n"
        "* **Timing and sequence:** considered and applicable.\n"
    )

    assert planning.planned_risk_themes(memo) == ["Accounts payable"]


def test_a_risk_section_that_enumerates_nothing_is_reported_not_ignored():
    prose = (
        "# APM\n\n## Fraud risk and management override\n\n"
        + "Management override is presumed present for this engagement and the "
        "planned response will include testing entries and approval records for "
        "unusual or high-value activity, reviewing changes to vendor and payment "
        "data, and corroborating approval evidence with independent records. "
        "Override indicators will be evaluated without assuming intent.\n"
    )

    assert planning.unstructured_risk_sections(prose) == [
        "fraud risk and management override"
    ]


# --------------------------------------------------------------------------- #
# What the memorandum says it could not settle
# --------------------------------------------------------------------------- #
_MATTERS = """# APM

## Key risks and planned response

### Accounts payable

Duplicate payment risk.

## Planning assumptions and matters reported

The table above is based on observed data. Where information was not available:

- The approved version of the Financial Approval Matrix was not provided; the
  extract lacks version metadata.
- No explicit materiality threshold was provided.
  - Materiality will be set during detailed planning.
"""


def test_planning_matters_reads_the_bullets_a_memorandum_left_open():
    # A matter is a sentence, and a sentence wraps. Matching per line truncated
    # the first matter at its line break, mid-clause.
    assert planning.planning_matters(_MATTERS) == [
        "The approved version of the Financial Approval Matrix was not provided; "
        "the extract lacks version metadata.",
        "No explicit materiality threshold was provided. Materiality will be set "
        "during detailed planning.",
    ]


def test_a_nested_bullet_qualifies_a_matter_rather_than_adding_one():
    # An indented item is the "and here is what that means" half of the matter
    # above it. Counted separately it becomes a matter with no subject.
    matters = planning.planning_matters(_MATTERS) or []

    assert len(matters) == 2
    assert matters[1].endswith("Materiality will be set during detailed planning.")


def test_a_matter_is_carried_whole_where_a_risk_theme_is_carried_by_name():
    """The two projections want different halves of a bold-led bullet.

    A risk section enumerates themes and the bold lead is the theme's name; a
    matters section enumerates matters and the text after the lead is the reason
    the matter has to be resolved, which is exactly what a reader needs.
    """
    memo = (
        "# APM\n\n## Planning assumptions and matters reported\n\n"
        "- **Period unconfirmed.** It is proposed from observed ranges.\n"
    )

    assert planning.planning_matters(memo) == [
        "**Period unconfirmed.** It is proposed from observed ranges."
    ]


def test_a_memorandum_with_no_matters_section_is_not_a_memorandum_with_none():
    """``None`` and ``[]`` are different answers about a plan.

    Every APM drafted before the template carried the section has no section to
    read. Reporting that as "nothing outstanding" states a clean plan on a
    memorandum that was never asked the question.
    """
    memo = "# APM\n\n## Key risks and planned response\n\n### Payables\n\nRisk.\n"

    assert planning.planning_matters(memo) is None
    assert planning.planning_matters(
        "# APM\n\n## Planning assumptions and matters reported\n\n"
        "Nothing remains outstanding at the date of this memorandum.\n"
    ) == []


def test_the_matters_section_is_found_by_what_its_heading_means():
    # A firm renaming the section keeps the projection, the same way
    # `planned_risk_themes` scopes by the word "risk".
    for heading in (
        "Limitations",
        "Outstanding items",
        "Matters for the auditee",
        "Assumptions",
    ):
        memo = f"# APM\n\n## {heading}\n\n- The period is unconfirmed.\n"
        assert planning.planning_matters(memo) == ["The period is unconfirmed."], heading


def test_theme_ownership_survives_ordinary_variation_in_wording():
    """Inflection and spelling are not coverage gaps.

    Exact token equality reported three owned themes as unowned on the live
    pair: the memo wrote "circumvention" and "payments" where the matrix wrote
    "circumvent" and "payment", and "unauthorised" against "unauthorized".
    Matching is still lexical, so a theme and a row that share no word at all
    remain unreconciled — that costs a repair round, and forcing the matrix to
    answer in the memo's terms is the point of the check.
    """
    rows = [
        {
            "process": "Purchasing",
            "risk": (
                "Commitments may be split across transactions to circumvent "
                "approval thresholds"
            ),
        },
        {
            "process": "Payments",
            "risk": "Payment may be directed to unauthorized destinations",
        },
    ]

    assert planning._unowned_themes(planning.planned_risk_themes(_BULLETED)[:2], rows) == []


_VALUED_PROFILES = [
    {
        "table": "invoice_data",
        "rows": 118,
        "columns": [{"name": "INVOICE_AMOUNT", "sum": "3,103,467,230"}],
    },
    {
        "table": "po_data",
        "rows": 93,
        "columns": [{"name": "PO_TOTAL_AMOUNT", "sum": "1,934,810,970"}],
    },
]


def _tabular_row(**overrides):
    """A row that already satisfies the inquiry gate, so the next one is tested."""
    return _row(
        control_attributes=[
            {
                "key": "duplicate_invoice_number",
                "assertion": "Completeness",
                "requirement": "Vendor invoice numbers are unique across the population.",
                "evidence_kind": "tabular_population",
            }
        ],
        **overrides,
    )


def test_two_valued_populations_require_a_requirement_that_they_agree():
    """The third leg of the three-way match, and the one a matrix omits.

    Sequence and authorization get reached for unprompted; agreement of the
    amounts is where an invoice of 80,000,000 against a purchase order of
    8,000,000 had no requirement it could fail.
    """
    gateway = _Gateway([json.dumps({"rows": [_tabular_row()]})] * 4)
    request = _request(_bundle(profiles=_VALUED_PROFILES))

    with pytest.raises(WorkerRunError, match="recorded values agree"):
        WORKERS.execute(request, gateway)


def test_a_requirement_asserting_agreement_satisfies_it():
    row = _tabular_row()
    row["control_attributes"].append(
        {
            "key": "invoice_to_order_agreement",
            "assertion": "Accuracy",
            "requirement": (
                "The invoice amount agrees to the approved purchase order total."
            ),
            "evidence_kind": "tabular_population",
        }
    )
    gateway = _Gateway([json.dumps({"rows": [row]})])

    result = WORKERS.execute(_request(_bundle(profiles=_VALUED_PROFILES)), gateway)

    assert len(result.proposal["rows"]) == 1


def test_one_valued_population_asserts_nothing_about_agreement():
    # Nothing to reconcile against: a single population records what a
    # transaction is worth in exactly one place.
    gateway = _Gateway([json.dumps({"rows": [_tabular_row()]})])

    result = WORKERS.execute(
        _request(_bundle(profiles=_VALUED_PROFILES[:1])), gateway
    )

    assert len(result.proposal["rows"]) == 1


def test_a_theme_owned_on_one_shared_word_passes_but_is_reported():
    """A memo names the technique; a matrix names the risk condition.

    "Circumvention through transaction splitting" is answered by "a commitment
    may be divided across related requisitions to circumvent approval", and
    those share exactly one word. Requiring two rejected a 27-row matrix that
    covered every theme it was accused of missing, and failed the run.
    """
    rows = [
        {
            "process": "Requisition approval",
            "risk": (
                "A commitment may be divided across related requisitions to "
                "circumvent approval thresholds"
            ),
        },
        {"process": "Payments", "risk": "Payment may be directed incorrectly"},
        {"process": "Accounts payable", "risk": "Duplicate payment"},
    ]
    themes = planning.planned_risk_themes(_BULLETED)

    assert planning._unowned_themes(themes, rows) == []
    assert "Circumvention through transaction splitting" in planning.weakly_owned_themes(
        _BULLETED, rows
    )


def test_a_document_level_failure_re_asks_the_attributes_of_every_row():
    """A scoped repair cannot add what the document as a whole is missing.

    Every row validating means there is nothing to scope to, and the earlier
    behaviour returned the identical rows without asking the model anything —
    which made a document-level error unrepairable by construction.

    Which call answers it is settled by what the gate reads. ``_asserts_agreement``
    reads attribute requirement text, so the correction is to add an attribute,
    and the rows call — the expensive one — is not re-asked at all.
    """
    corrected = list(_tabular_row()["control_attributes"])
    corrected.append(
        {
            "key": "invoice_to_order_agreement",
            "assertion": "Accuracy",
            "requirement": "The invoice amount agrees to the purchase order total.",
            "evidence_kind": "tabular_population",
        }
    )
    gateway = _Gateway(
        [json.dumps({"rows": [_tabular_row()]})],
        attributes=[
            None,  # the initial attributes call answers by agreement
            json.dumps(
                {"attributes": [{"row_index": 1, "control_attributes": corrected}]}
            ),
        ],
    )

    result = WORKERS.execute(
        _request(_bundle(profiles=_VALUED_PROFILES)), gateway
    )

    assert result.repaired is True
    # Risks and controls once each; the attributes call twice. Neither of the
    # calls before it is re-asked to add one requirement.
    assert [call["system"] for call in gateway.calls] == [
        planning.RCM_RISKS_SYSTEM,
        planning.RCM_CONTROLS_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
    ]
    repair = gateway.calls[3]
    assert "ROWS TO CORRECT" not in repair["user"]
    assert "recorded values agree" in repair["user"]
    # Every row, because the fix is to add something and the scoped envelope
    # forbids returning a row it was not given.
    assert len(json.loads(repair["user"])["ATTRIBUTES TO CORRECT"]) == 1
    assert len(result.proposal["rows"]) == 1


def test_a_repair_that_echoes_the_request_envelope_still_splices():
    """The request presents each failing row as {row_index, row}.

    A real engagement had the model answer in that same shape, and every
    corrected row spliced in as ``{"row": {…}}`` — no process, no risk, no
    attributes. Nineteen good rows were replaced by empty ones and the matrix
    was then rejected for covering nothing, which read as the model's failure
    and was the parser's.
    """
    good = _row(process="Payments", risk="Payments may be misdirected")
    failing = _row()
    corrected = _row(
        control_attributes=[
            {
                "key": "duplicate_invoice_number",
                "assertion": "Completeness",
                "requirement": "Vendor invoice numbers are unique across the population.",
                "evidence_kind": "tabular_population",
            }
        ]
    )

    merged = planning._repair_scoped_rows(
        [good, failing],
        [{"index": 2}],
        json.dumps({"rows": [{"row_index": 2, "row": corrected}]}),
    )

    assert [row["process"] for row in merged] == ["Payments", "Accounts payable"]
    assert merged[1]["control_attributes"][0]["evidence_kind"] == "tabular_population"
    # The row that validated is the object that was parsed, untouched.
    assert merged[0] is good


def test_row_and_document_failures_are_reported_in_one_pass():
    """One repair attempt has to answer everything that is wrong.

    A gate that only fires once the previous one is satisfied costs an attempt
    each. With one attempt available, a matrix that tripped a row rule first
    had its rows corrected and then failed on a document rule it had never been
    told about — which is how a regeneration failed with 19 good rows in hand.
    """
    # An unsupported rating trips the row-level gate; nothing in the row
    # asserts that recorded values agree, which trips the document-level one.
    gateway = _Gateway([json.dumps({"rows": [_row(risk_rating="severe")]})] * 2)
    request = _request(_bundle(profiles=_VALUED_PROFILES))

    with pytest.raises(WorkerRunError) as error:
        WORKERS.execute(request, gateway)

    message = str(error.value)
    assert "unsupported risk rating" in message
    assert "recorded values agree" in message
    # Both were on the table from the first repair, not discovered one per
    # turn — and each went to the call whose job it is. The rating is the rows
    # call's; the missing agreement requirement is an attribute, so it is the
    # attributes call's.
    risks_repair = gateway.calls[3]
    attributes_repair = gateway.calls[-1]
    assert risks_repair["system"] == planning.RCM_RISKS_SYSTEM
    assert "unsupported risk rating" in risks_repair["user"]
    assert attributes_repair["system"] == planning.RCM_ATTRIBUTES_SYSTEM
    assert "recorded values agree" in attributes_repair["user"]


def test_a_theme_owned_by_any_proposed_row_reconciles():
    receipt = _row(
        process="Goods receipt",
        risk="Payment is made before the three-way match is complete",
        control="No control identified",
        new_risk_reason="The memorandum plans a response for goods receipt.",
    )
    gateway = _Gateway([json.dumps({"rows": [_row(), receipt]})])

    result = WORKERS.execute(_request(_bundle(apm=_PLANNED)), gateway)

    assert len(result.proposal["rows"]) == 2


def test_rcm_worker_accepts_json_fenced_response():
    gateway = _Gateway(["```json\n" + json.dumps({"rows": [_row()]}) + "\n```"])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["operation"] == "create"


def test_rcm_worker_repairs_only_the_failing_row_and_preserves_the_rest():
    """A repair is scoped to the rows that failed.

    The whole document used to be regenerated for any single row error, which
    cost every correct row a second, differently-worded draft — and burned the
    one bounded correction turn on rows that had nothing wrong with them.
    """

    good = _row(process="Vendor onboarding", risk="Unapproved vendors are created")
    bad = _row(risk_rating="urgent")
    gateway = _Gateway(
        [
            json.dumps({"rows": [good, bad]}),
            json.dumps({"rows": [{**bad, "risk_rating": "high", "row_index": 2}]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    # The three calls on attempt 1; on attempt 2 the row re-enters at the risks
    # call and flows forward through the two after it.
    assert [call["attempt"] for call in gateway.calls] == [1, 1, 1, 2, 2, 2]
    assert [call["system"] for call in gateway.calls] == [
        planning.RCM_RISKS_SYSTEM,
        planning.RCM_CONTROLS_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
        planning.RCM_RISKS_SYSTEM,
        planning.RCM_CONTROLS_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
    ]
    repair_request = json.loads(gateway.calls[3]["user"])
    # Only the failing row travels, carrying its index and its own reasons.
    assert [entry["row_index"] for entry in repair_request["RISKS TO CORRECT"]] == [2]
    assert repair_request["RISKS TO CORRECT"][0]["errors"] == [
        "RCM row 2 has an unsupported risk rating; it must be exactly one of "
        "critical, high, low, medium"
    ]
    # Its control and its attributes are restated against the new wording; the
    # row that never failed is not re-asked at either.
    assert [
        entry["row_index"]
        for entry in json.loads(gateway.calls[4]["user"])["CONTROLS TO CORRECT"]
    ] == [2]
    assert [
        entry["row_index"]
        for entry in json.loads(gateway.calls[5]["user"])["ATTRIBUTES TO CORRECT"]
    ] == [2]
    rows = result.proposal["rows"]
    assert [row["risk"] for row in rows] == [
        "Unapproved vendors are created",
        "Duplicate payments are processed",
    ]
    assert rows[1]["risk_rating"] == "high"


def test_rcm_worker_preserves_untouched_rows_byte_for_byte_through_a_repair():
    """The untouched row is the object that was parsed, not a re-serialization."""

    good = _row(process="Vendor onboarding", risk="Unapproved vendors are created")
    gateway = _Gateway(
        [
            json.dumps({"rows": [good, _row(risk_rating="urgent")]}),
            json.dumps(
                {
                    "rows": [
                        {
                            **_row(risk_rating="high"),
                            # A repair that tries to reword an accepted row cannot:
                            # only the indices that failed are spliced.
                            "row_index": 1,
                            "risk": "Rewritten by the repair turn",
                        },
                        {**_row(risk_rating="high"), "row_index": 2},
                    ]
                }
            ),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    accepted = planning._plain_json(result.proposal["rows"][0])
    expected = planning._normalized_rcm_row(good, 1, set())
    assert json.dumps(accepted, sort_keys=True) == json.dumps(
        expected, sort_keys=True
    )
    assert accepted["risk"] == "Unapproved vendors are created"


def test_rcm_worker_drops_non_durable_keys_from_a_row():
    row = _row(control_attributes=[_cycle_attribute()])
    gateway = _Gateway([json.dumps({"rows": [row]})])

    result = WORKERS.execute(_request(), gateway)

    normalized = result.proposal["rows"][0]
    # Narrative keys the model likes to add are dropped rather than failing the
    # row; the workspace discards them anyway.
    assert "new_risk_reason" not in normalized
    assert "test_procedure" not in normalized


def test_a_cycle_attribute_naming_no_comparison_is_accepted():
    """The state a matrix now commits its cycle attributes in.

    Deciding *that* a requirement needs several source records read together is
    the matrix's judgment. Which fields must then agree is the cycle design's,
    downstream, and this turn has read none of the documents it would need to
    say. Refusing the row for not saying it would refuse every matrix.
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row(control_attributes=[_cycle_attribute()])]})]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is False
    (attribute,) = result.proposal["rows"][0]["control_attributes"]
    assert attribute["evidence_kind"] == "transaction_cycle"
    assert "required_comparisons" not in attribute
    # Two calls, and neither of them is an evidence pass: the rows, then their
    # attributes. What a cycle attribute must show is settled downstream.
    assert [call["system"] for call in gateway.calls] == [
        planning.RCM_RISKS_SYSTEM,
        planning.RCM_CONTROLS_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
    ]


def test_a_cycle_attribute_stating_an_empty_contract_is_still_refused():
    """Absent and empty are different statements.

    Absent means the cycle design has not run yet. Empty means a contract was
    authored and says nothing must agree, which describes no work at all — and
    it is what the matrix produced when it tried to write the contract itself.
    """

    gateway = _Gateway(
        [
            json.dumps(
                {
                    "rows": [
                        _row(
                            control_attributes=[
                                {**_cycle_attribute(), "required_comparisons": []}
                            ]
                        )
                    ]
                }
            ),
            json.dumps({"rows": []}),
        ]
    )

    with pytest.raises(WorkerRunError, match="names no evidence contract"):
        WORKERS.execute(_request(), gateway)


def test_rcm_worker_aggregates_quality_errors_across_rows():
    with pytest.raises(planning.WorkerResponseValidationError) as raised:
        planning.validate_rcm_proposal(
            {
                "rows": [
                    _row(risk_rating="urgent"),
                    _row(operation="replace"),
                ]
            },
            _request(),
        )

    assert raised.value.errors == (
        "RCM row 1 has an unsupported risk rating; it must be exactly one of "
        "critical, high, low, medium",
        "RCM row 2 has an unsupported operation; it must be exactly 'update' "
        "or 'create'",
    )


def test_rcm_worker_tolerates_stray_characters_after_a_complete_object():
    """Run 20260809-140906-19a740: a valid 24-row draft lost to a trailing `]}`.

    ``json.loads`` requires the entire string to be one value, so a complete
    object followed by two brackets the model had already closed was discarded
    whole — and, because the rows never parsed, the evidence pass never ran.
    """

    gateway = _Gateway([json.dumps({"rows": [_row()]}) + "]}"])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is False
    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Duplicate payments are processed"
    ]


def test_rcm_worker_tolerates_prose_before_the_object():
    gateway = _Gateway(
        ["Here is the revised matrix:\n" + json.dumps({"rows": [_row()]})]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["operation"] == "create"


def test_rcm_worker_still_rejects_a_truncated_object():
    """Surplus is tolerated; shortfall is not.

    A truncated draft must never be read as a complete one — that would commit a
    matrix that silently stops halfway.
    """

    truncated = json.dumps({"rows": [_row(), _row()]})[:-40]
    gateway = _Gateway([truncated, truncated])

    with pytest.raises(WorkerRunError, match="not a valid JSON object"):
        WORKERS.execute(_request(), gateway)


def test_a_whole_document_re_ask_is_the_second_and_last_call():
    """When the prior draft never parsed there are no rows to scope a repair to.

    The whole document is re-asked, and that is the end of it. There used to be
    a third call behind this one authoring the evidence contracts, and a path
    that skipped it left thirteen transaction-cycle attributes carrying nothing
    — the class of bug that stops existing once there is only one call.
    """

    gateway = _Gateway(
        [
            "{ truncated",
            json.dumps({"rows": [_row(control_attributes=[_cycle_attribute()])]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    # The rows call twice — the draft, then the re-ask — with the attributes
    # call following each of them over whatever parsed.
    assert [call["system"] for call in gateway.calls] == [
        planning.RCM_RISKS_SYSTEM,
        planning.RCM_RISKS_SYSTEM,
        planning.RCM_CONTROLS_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
    ]
    (attribute,) = result.proposal["rows"][0]["control_attributes"]
    assert attribute["evidence_kind"] == "transaction_cycle"
    assert "required_comparisons" not in attribute


def test_rcm_worker_hands_unparseable_output_to_the_registry_to_repair():
    """The worker returns text; the registry owns rejection and repair.

    The two-pass implementation parses the model's output in order to run the
    second pass over it. Raising from there would escape the bounded repair loop
    altogether — `WorkerRegistry.execute` only guards the schema and semantic
    validators — and surface as an unhandled error instead of a repaired draft.
    """

    gateway = _Gateway(["not json at all", json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Duplicate payments are processed"
    ]


def test_rcm_worker_re_asks_the_document_when_the_prior_draft_never_parsed():
    """A linked retry can start from a draft the schema rejected.

    There are no rows to scope a repair to, so the whole document is re-asked
    rather than the worker failing on a partition it cannot compute.
    """

    seed = WorkerRepairSeed(
        previous_response="{ truncated",
        validation_errors=("the response is not a valid JSON object",),
    )
    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    result = WORKERS.execute(_request(), gateway, repair_seed=seed)

    assert result.proposal["rows"][0]["risk"] == "Duplicate payments are processed"
    assert "could not be parsed" in gateway.calls[0]["user"]


def test_rcm_worker_quarantines_an_unrepairable_row_and_keeps_the_rest():
    """One row that will not validate no longer costs the whole matrix.

    This is the shape of the live failure: twelve good rows discarded because one
    comparison named an operator that does not exist.
    """

    good = _row(process="Vendor onboarding", risk="Unapproved vendors are created")
    stubborn = _row(risk_rating="urgent")
    gateway = _Gateway(
        [
            json.dumps({"rows": [good, stubborn]}),
            # The repair turn does not fix it either.
            json.dumps({"rows": [{**stubborn, "row_index": 2}]}),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert [row["risk"] for row in result.proposal["rows"]] == [
        "Unapproved vendors are created"
    ]
    quarantined = result.proposal["quarantined"]
    assert [item["risk"] for item in quarantined] == [
        "Duplicate payments are processed"
    ]
    assert "unsupported risk rating" in quarantined[0]["errors"][0]


def test_rcm_worker_still_fails_when_no_row_survives():
    """Quarantine salvages a partial matrix; it never manufactures an empty one."""

    stubborn = _row(risk_rating="urgent")
    gateway = _Gateway(
        [
            json.dumps({"rows": [stubborn]}),
            json.dumps({"rows": [{**stubborn, "row_index": 1}]}),
        ]
    )

    with pytest.raises(WorkerRunError, match="unsupported risk rating"):
        WORKERS.execute(_request(), gateway)


def test_a_repair_carries_only_the_prompt_of_the_call_it_is_correcting():
    """Each repair is sent the rules of the job it is repairing and no others.

    The scoped repair used to be sent two system prompts concatenated, because
    a row it was correcting might carry an evidence contract. A rating is the
    rows call's rule; nothing about the attributes belongs in that turn.
    """

    attribute = _cycle_attribute()
    broken = _row(risk_rating="severe", control_attributes=[attribute])
    gateway = _Gateway(
        [
            json.dumps({"rows": [broken]}),
            json.dumps(
                {"rows": [{**_row(control_attributes=[attribute]), "row_index": 1}]}
            ),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    risks_repair = gateway.calls[3]
    assert risks_repair["system"] == planning.RCM_RISKS_SYSTEM
    # The rules of the other calls are not in it.
    assert "evidence_kind" not in risks_repair["system"]
    assert "control_owner" not in risks_repair["system"]
    errors = json.loads(risks_repair["user"])["RISKS TO CORRECT"][0]["errors"]
    assert any("unsupported risk rating" in error for error in errors)
    # And the row travels as the risk half alone: the fields the other calls
    # own are not shown to the call that does not write them.
    sent = json.loads(risks_repair["user"])["RISKS TO CORRECT"][0]["row"]
    assert set(sent) <= set(planning._RCM_RISK_FIELDS)
    (repaired,) = result.proposal["rows"]
    assert repaired["risk"] == "Duplicate payments are processed"
    assert "required_comparisons" not in repaired["control_attributes"][0]


def test_a_control_type_outside_the_two_kinds_is_refused():
    """A procurement regeneration wrote the literal "None" on seven rows.

    It passed, because the only check was that the field was not empty, and it
    reached the matrix as a control type nothing can read.
    """

    gateway = _Gateway([json.dumps({"rows": [_row(control_type="Automated")]})] * 2)

    with pytest.raises(WorkerRunError, match="unsupported control_type 'Automated'"):
        WORKERS.execute(_request(), gateway)


def test_a_row_identifying_no_control_leaves_the_kind_empty():
    """There is nothing there to be preventive or detective about.

    Naming a kind for a control the row says does not exist states mechanics
    the planning basis never described — which is what the runs before this
    did, classifying every uncontrolled row anyway.
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row(control="No control identified", control_type="")]})]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["control_type"] == ""


def test_a_placeholder_for_the_absence_is_read_as_the_absence():
    """"None", "N/A" and an empty field are one answer, and the row is right.

    Refusing it would spend the bounded repair turn on a row whose substance is
    correct, so the field is cleared instead.
    """

    for stated in ("None", "n/a", "Not applicable"):
        gateway = _Gateway(
            [
                json.dumps(
                    {
                        "rows": [
                            _row(control="No control identified", control_type=stated)
                        ]
                    }
                )
            ]
        )

        result = WORKERS.execute(_request(), gateway)

        assert result.proposal["rows"][0]["control_type"] == "", stated


def test_a_row_asserting_a_control_must_still_say_which_kind():
    gateway = _Gateway([json.dumps({"rows": [_row(control_type="")]})] * 2)

    with pytest.raises(WorkerRunError, match="missing control_type"):
        WORKERS.execute(_request(), gateway)


def test_the_kind_is_normalized_rather_than_refused_for_its_case():
    gateway = _Gateway([json.dumps({"rows": [_row(control_type="Detective")]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["control_type"] == "detective"


# --------------------------------------------- the wording rules, as checks
def test_a_percentage_in_a_risk_is_refused_rather_than_asked_against():
    """Recommendation 7 of the quality doc, with a check behind it at last.

    A statistic read from a profile is not a fact about the population, and a
    quantified condition in a risk pre-concludes what fieldwork establishes.
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row(risk="GRN links are missing in 18.64% of invoices")]})] * 2
    )

    with pytest.raises(WorkerRunError, match="quotes a percentage"):
        WORKERS.execute(_request(), gateway)


def test_a_column_name_in_a_control_is_refused():
    """A risk written against one corpus's schema is not an audit risk."""

    gateway = _Gateway(
        [json.dumps({"rows": [_row()]})],
        controls=[
            json.dumps({"controls": [{
                "row_index": 1,
                "control": "The ERP requires GRN_ID_LINK before payment.",
                "control_type": "preventive",
            }]}),
            json.dumps({"controls": [{
                "row_index": 1,
                "control": "The ERP requires a goods receipt reference before payment.",
                "control_type": "preventive",
            }]}),
        ],
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    errors = json.loads(gateway.calls[3]["user"])["CONTROLS TO CORRECT"][0]["errors"]
    assert any("names the column 'GRN_ID_LINK'" in error for error in errors)


def test_a_recommendation_wearing_the_grammar_of_a_control_is_refused():
    """A control that does not exist cannot be tested.

    The design gap is a finding; the honest row says "No control identified".
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row()]})] ,
        controls=[
            json.dumps({"controls": [{
                "row_index": 1,
                "control": "A formal exception procedure should define thresholds.",
                "control_type": "preventive",
            }]}),
            json.dumps({"controls": [{
                "row_index": 1, "control": "No control identified", "control_type": "",
            }]}),
        ],
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert result.proposal["rows"][0]["control"] == "No control identified"


def test_no_control_identified_is_not_read_as_a_recommendation():
    gateway = _Gateway([json.dumps({"rows": [_row(control="No control identified",
                                                  control_type="")]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["rows"][0]["control"] == "No control identified"


def test_an_owner_the_basis_never_names_is_refused():
    """The D4 defect class: a role inferred from the nature of the control.

    An empty owner is a question to put to the client; an invented one is a
    false attribution that survives into the working paper.
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row()]})],
        controls=[
            json.dumps({"controls": [{
                "row_index": 1, "control": "Duplicate invoice validation",
                "control_type": "preventive",
                "control_owner": "Chief Information Security Officer",
            }]}),
            json.dumps({"controls": [{
                "row_index": 1, "control": "Duplicate invoice validation",
                "control_type": "preventive", "control_owner": "",
            }]}),
        ],
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    errors = json.loads(gateway.calls[3]["user"])["CONTROLS TO CORRECT"][0]["errors"]
    assert any("does not appear in the planning basis" in error for error in errors)
    assert result.proposal["rows"][0]["control_owner"] == ""


def test_an_owner_the_basis_does_name_is_kept_verbatim():
    apm = "# APM\n\nThe Head of Finance approves every claim above the limit."
    gateway = _Gateway(
        [json.dumps({"rows": [_row()]})],
        controls=[
            json.dumps({"controls": [{
                "row_index": 1, "control": "Approval against the delegation matrix",
                "control_type": "preventive", "control_owner": "Head of Finance",
            }]})
        ],
    )

    result = WORKERS.execute(_request(_bundle(apm=apm)), gateway)

    assert result.proposal["rows"][0]["control_owner"] == "Head of Finance"


# ------------------------------------------------------- flags, not refusals
def test_asserted_system_enforcement_is_flagged_and_the_row_still_commits():
    """Right about as often as wrong, so the auditor is told and decides.

    Enforced, it rejected correct rows: the basis sometimes does say the portal
    blocks it.
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row()]})],
        controls=[
            json.dumps({"controls": [{
                "row_index": 1,
                "control": "The ERP workflow prevents payment before approval.",
                "control_type": "preventive",
            }]})
        ],
    )

    result = WORKERS.execute(_request(), gateway)

    (flag,) = [f for f in result.proposal["flags"]
               if f["kind"] == "asserted_system_enforcement"]
    assert flag["row_index"] == 1
    assert "confirm the planning basis says so" in flag["message"]
    # And the row is committed, not refused.
    assert result.proposal["rows"][0]["control"].startswith("The ERP workflow")


def test_two_rows_stating_one_risk_are_flagged_with_both_indices():
    risk = "Invoices may be paid without the required verification and approval"
    gateway = _Gateway(
        [json.dumps({"rows": [
            _row(risk=risk),
            _row(process="Payments", risk=risk + " recorded"),
        ]})]
    )

    result = WORKERS.execute(_request(), gateway)

    (flag,) = [f for f in result.proposal["flags"] if f["kind"] == "near_duplicate_risk"]
    assert flag["row_index"] == 1
    assert "row 2" in flag["message"]


def test_distinct_risks_of_one_cycle_are_not_flagged_for_sharing_vocabulary():
    """The threshold is high on purpose: one cycle's risks share a great deal."""

    gateway = _Gateway(
        [json.dumps({"rows": [
            _row(risk="Invoices may be paid without the required approval"),
            _row(process="Payments", risk="Payments may be released before goods are received"),
        ]})]
    )

    result = WORKERS.execute(_request(), gateway)

    assert not [f for f in result.proposal.get("flags") or []
                if f["kind"] == "near_duplicate_risk"]


def test_a_criterion_that_resolves_to_no_sentence_is_flagged_not_refused():
    gateway = _Gateway(
        [json.dumps({"rows": [_row()]})],
        controls=[
            json.dumps({"controls": [{
                "row_index": 1, "control": "Duplicate invoice validation",
                "control_type": "preventive",
                "criteria": "A clause no supplied document states.",
            }]})
        ],
    )

    result = WORKERS.execute(_request(), gateway)

    (flag,) = [f for f in result.proposal["flags"] if f["kind"] == "criteria_unresolved"]
    assert flag["row_index"] == 1
    # The quote is kept; only the pointer is missing.
    assert result.proposal["rows"][0]["criteria"] == "A clause no supplied document states."
    assert not result.proposal["rows"][0]["criteria_refs"]


# ------------------------------------------------ the two calls of one matrix
def test_the_rows_call_is_not_asked_for_attributes_and_the_second_supplies_them():
    """The seam. Each call is asked for the fields it is competent to write.

    A row's risk is domain recall from a memorandum; its assertion is a choice
    from a list of eight. Asked together, a wrong assertion name discarded the
    risk with it — and the risk was the expensive half.
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row(control_attributes=None)]})],
        attributes=[
            json.dumps(
                {
                    "attributes": [
                        {
                            "row_index": 1,
                            "control_attributes": [
                                {
                                    "key": "validation_operates",
                                    "assertion": "Operational",
                                    "requirement": "Duplicate validation runs.",
                                    "evidence_kind": "tabular_population",
                                }
                            ],
                        }
                    ]
                }
            )
        ],
    )

    result = WORKERS.execute(_request(_bundle(profiles=[_INVOICES])), gateway)

    risks_call, controls_call, attributes_call = gateway.calls
    # The risks prompt asks for neither the control nor the attributes, and
    # carries no vocabulary for either.
    assert "control_attributes" not in risks_call["system"]
    assert "evidence_kind" not in risks_call["system"]
    assert "control_owner" not in risks_call["system"]
    assert "control_owner" in controls_call["system"]
    assert "evidence_kind" in attributes_call["system"]
    # The risks call is shown the memorandum and no engagement material.
    assert "Assess procurement approvals" in risks_call["user"]
    # The controls call is shown the settled risks and the engagement's own
    # documents to read them against — and not the memorandum, which it would
    # quote its criteria out of: the memo carries no citation anchors, so a
    # criterion quoted from it can never be traced to the policy it rests on.
    assert "Assess procurement approvals" not in controls_call["user"]
    # The attributes call sees the rows and where an answer could live, and is
    # not shown the memorandum it could second-guess a risk from.
    assert json.loads(controls_call["user"])["RISKS"] == [
        {
            "row_index": 1,
            "process": "Accounts payable",
            "risk": "Duplicate payments are processed",
            "risk_rating": "high",
        }
    ]
    sent = json.loads(attributes_call["user"])
    assert sent["ROWS"] == [
        {
            "row_index": 1,
            "process": "Accounts payable",
            "risk": "Duplicate payments are processed",
            "control": "Duplicate invoice validation",
            "control_type": "preventive",
        }
    ]
    assert "Assess procurement approvals" not in attributes_call["user"]

    (attribute,) = result.proposal["rows"][0]["control_attributes"]
    assert attribute["key"] == "validation_operates"


def test_the_attributes_call_is_shown_tables_by_name_and_never_by_statistic():
    """Where an answer lives, and nothing it could misread as an answer.

    A null percentage is not an exception rate and a maximum is not a policy
    limit — the two mistakes the template spends a section warning against. The
    call that classifies is simply never shown one.
    """

    gateway = _Gateway([json.dumps({"rows": [_row()]})])

    WORKERS.execute(
        _request(_bundle(profiles=[_INVOICES], metadata=[_INVOICES])), gateway
    )

    sent = json.loads(gateway.calls[2]["user"])
    assert sent["TABLES"] == [
        {
            "table": "invoice_data",
            "columns": ["INVOICE_ID", "VENDOR_INVOICE_NUMBER", "INVOICE_AMOUNT"],
        }
    ]
    assert "null_percent" not in gateway.calls[2]["user"]


def test_the_attributes_call_is_told_which_record_kinds_the_engagement_holds():
    """By name and count, and nothing about what their fields are.

    The count is the population signal a strategy turns on: a type carrying one
    document cannot answer a requirement written against a population. What
    those records *state* belongs to the cycle design, and supplying it here is
    what had a matrix naming fields it had never seen a document of.
    """

    gateway = _Gateway([json.dumps({"rows": [_row()]})])
    request = _request()
    request = WorkerRequest(
        worker_id=request.worker_id,
        capability_id=request.capability_id,
        unit_id=request.unit_id,
        context=request.context,
        unit_input={
            "input_sha1": "rcm-input",
            "document_types": [
                {"document_type": "vendor_invoice", "documents": 18},
                {"document_type": "purchase_order", "documents": 12},
            ],
        },
        activity={"artifact_refs": ["planning:apm"]},
    )

    WORKERS.execute(request, gateway)

    sent = json.loads(gateway.calls[2]["user"])
    assert sent["DOCUMENT TYPES HELD"] == [
        {"document_type": "vendor_invoice", "documents": 18},
        {"document_type": "purchase_order", "documents": 12},
    ]
    assert "fields" not in gateway.calls[2]["user"]


def test_a_row_the_attributes_call_omits_fails_on_its_own():
    """Not invented locally, and not fatal to the rows beside it.

    Writing a plausible attribute here would answer for a control this call
    never classified. The row reaches the gate as one with no attributes, which
    is what the scoped attributes repair exists to correct.
    """

    gateway = _Gateway(
        [json.dumps({"rows": [_row(control_attributes=None)]})] * 2,
        attributes=[json.dumps({"attributes": []}), json.dumps({"attributes": []})],
    )

    with pytest.raises(WorkerRunError, match="missing control_attributes"):
        WORKERS.execute(_request(), gateway)


def test_an_attribute_only_failure_repairs_at_the_attributes_call_alone():
    """The rows are right. Re-asking for them would pay for the expensive half."""

    gateway = _Gateway(
        [json.dumps({"rows": [_row(control_attributes=None)]})],
        attributes=[
            json.dumps(
                {
                    "attributes": [
                        {
                            "row_index": 1,
                            "control_attributes": [
                                {
                                    "key": "validation_operates",
                                    "assertion": "Sufficiency",
                                    "requirement": "Duplicate validation runs.",
                                    "evidence_kind": "manual_inspection",
                                }
                            ],
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "attributes": [
                        {
                            "row_index": 1,
                            "control_attributes": [
                                {
                                    "key": "validation_operates",
                                    "assertion": "Operational",
                                    "requirement": "Duplicate validation runs.",
                                    "evidence_kind": "manual_inspection",
                                }
                            ],
                        }
                    ]
                }
            ),
        ],
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    # The three calls once; then the attributes call again, alone. Neither the
    # risks nor the controls call is re-asked for something neither wrote.
    assert [call["system"] for call in gateway.calls] == [
        planning.RCM_RISKS_SYSTEM,
        planning.RCM_CONTROLS_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
        planning.RCM_ATTRIBUTES_SYSTEM,
    ]
    correcting = json.loads(gateway.calls[3]["user"])["ATTRIBUTES TO CORRECT"][0]
    assert correcting["row_index"] == 1
    # The repair is given the attributes it wrote, not only the violation.
    assert correcting["current_attributes"][0]["assertion"] == "Sufficiency"
    assert any("assertion" in error for error in correcting["errors"])
    (attribute,) = result.proposal["rows"][0]["control_attributes"]
    assert attribute["assertion"] == "Operational"


def test_a_row_that_never_failed_is_not_re_asked_at_either_call():
    """A scoped repair is scoped at both calls or at neither."""

    good = _row(process="Vendor onboarding", risk="Unapproved vendors are created")
    bad = _row(risk_rating="urgent")
    gateway = _Gateway(
        [
            json.dumps({"rows": [good, bad]}),
            json.dumps({"rows": [{**bad, "risk_rating": "high", "row_index": 2}]}),
        ]
    )

    WORKERS.execute(_request(), gateway)

    assert [
        entry["row_index"]
        for entry in json.loads(gateway.calls[-1]["user"])["ATTRIBUTES TO CORRECT"]
    ] == [2]


def test_rcm_worker_rejects_update_to_unknown_existing_row():
    current = _row(id="RCM-EXISTING", operation="update")
    gateway = _Gateway(
        [
            json.dumps({"rows": [_row(operation="update", rcm_id="RCM-MISSING")]}),
            json.dumps({"rows": [_row(operation="update", rcm_id="RCM-MISSING")]}),
        ]
    )

    with pytest.raises(WorkerRunError, match="does not identify an existing RCM row"):
        WORKERS.execute(_request(_bundle(current_rows=[current])), gateway)


def test_rcm_worker_accepts_update_matching_a_supplied_current_row():
    current = _row(id="RCM-1")
    gateway = _Gateway(
        [json.dumps({"rows": [_row(operation="update", rcm_id="rcm:RCM-1")]})]
    )

    result = WORKERS.execute(_request(_bundle(current_rows=[current])), gateway)

    assert result.proposal["rows"][0]["rcm_id"] == "rcm:RCM-1"


def test_rcm_worker_has_no_workspace_store_resolver_or_scheduler_dependency():
    source = inspect.getsource(planning)
    tree = ast.parse(source)
    imported = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name.endswith(
            (
                "workspaces",
                "workspace_transactions",
                "store",
                "resolver",
                "workflow_runner",
                "action_runner",
            )
        )
        for name in imported
    )
    assert ".ws" not in source
    assert "load_workspace" not in source


# --------------------------------------------------------------------------- #
# Citing a criterion: the register, and what a row may point at
# --------------------------------------------------------------------------- #
SOP_SUMMARY = (
    "DOCUMENT SUMMARY — Procurement SOP Extracts.docx\n"
    "## Process\n"
    "- Financial Authorities review the requisition. [C4]\n"
    "- Procurement matches the invoice with the PO and GRN. [C7]\n"
)
MATRIX_SUMMARY = (
    "DOCUMENT SUMMARY — Financial Approval Matrix.docx\n"
    "## Limits\n- CFO approves to PKR 10,000,000. [C2]\n"
)


def _document_bundle(*summaries):
    """A bundle carrying supplied document summaries, in order."""
    base = _bundle()
    items = list(base.items) + [
        ContextBundleItem(
            source_id="documents",
            source_ref=f"document:{document_id}",
            representation=ContextRepresentation("summary"),
            content=content,
            supplied_size=supplied_size(content),
        )
        for document_id, content in summaries
    ]
    return ContextBundle(
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        items=tuple(items),
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _sheet():
    return planning.rcm_citation_sheet(
        _request(_document_bundle(("d_sop", SOP_SUMMARY), ("d_matrix", MATRIX_SUMMARY)))
    )


def test_citation_sheet_numbers_documents_and_lists_only_their_own_anchors():
    sheet = _sheet()

    assert sheet == [
        {
            "ref": 1,
            "document": "Procurement SOP Extracts.docx",
            "document_id": "d_sop",
            "citations": ["C4", "C7"],
        },
        {
            "ref": 2,
            "document": "Financial Approval Matrix.docx",
            "document_id": "d_matrix",
            "citations": ["C2"],
        },
    ]


LOWER_CASE_SUMMARY = (
    "DOCUMENT SUMMARY — Delegation of Authority.docx\n"
    "## Limits\n- The CFO approves commitments to PKR 10,000,000. [c3]\n"
    "- Approval below the committed value breaches the delegation. [c9]\n"
)


def test_the_citation_sheet_recognises_either_marker_case():
    """The register is built from what the documents worker actually writes.

    That worker's prompt asks for `[c1]`, and its own normalizer folds every
    case variant onto the supplied id rather than rejecting the difference.
    Matching only `[C1]` here did not reject anything — it recognized nothing:
    a live engagement whose markers were all lower case produced an empty
    register, so no row could cite a criterion and none did.
    """

    sheet = planning.rcm_citation_sheet(
        _request(_document_bundle(("d_doa", LOWER_CASE_SUMMARY)))
    )

    assert sheet == [
        {
            "ref": 1,
            "document": "Delegation of Authority.docx",
            "document_id": "d_doa",
            "citations": ["c3", "c9"],
        }
    ]


def test_citation_sheet_skips_a_document_with_nothing_to_cite():
    sheet = planning.rcm_citation_sheet(
        _request(_document_bundle(("d_plain", "DOCUMENT SUMMARY — Notes.docx\nNo anchors.")))
    )

    assert sheet == []


# --------------------------------------------------------------------------- #
# Resolving a quoted criterion back to the sentence it came from
# --------------------------------------------------------------------------- #
def _index():
    return planning.rcm_sentence_index(
        _request(_document_bundle(("d_sop", SOP_SUMMARY), ("d_matrix", MATRIX_SUMMARY)))
    )


def test_the_sentence_index_carries_every_citable_sentence_with_its_marker():
    index = _index()

    assert [(entry["ref"], entry["citation"]) for entry in index] == [
        (1, "C4"), (1, "C7"), (2, "C2"),
    ]
    assert "financial authorities review the requisition" in index[0]["sentence"]


def test_a_quoted_clause_resolves_to_the_document_it_came_from():
    """The governing rule: the model names things, local code finds things.

    A row used to choose a `ref` number and a citation id out of a register and
    copy both correctly. Now it quotes the sentence, and this looks up where
    the sentence is.
    """

    refs, flag = planning.resolve_criteria(
        "Financial Authorities review the requisition.", "", _index()
    )

    assert refs == [{"document_id": "d_sop", "document": "Procurement SOP Extracts.docx", "citation_id": "C4"}]
    assert flag == ""


def test_a_quote_is_matched_through_case_punctuation_and_surrounding_quotes():
    refs, flag = planning.resolve_criteria(
        '  "CFO approves to PKR 10,000,000."  ', "", _index()
    )

    assert refs == [{"document_id": "d_matrix", "document": "Financial Approval Matrix.docx", "citation_id": "C2"}]
    assert flag == ""


def test_a_fragment_of_a_supplied_sentence_still_resolves():
    refs, _flag = planning.resolve_criteria(
        "matches the invoice with the PO and GRN", "", _index()
    )

    assert refs == [{"document_id": "d_sop", "document": "Procurement SOP Extracts.docx", "citation_id": "C7"}]


def test_a_lightly_paraphrased_quote_resolves_on_overlap_and_is_not_refused():
    """A quote is a copy the model makes, and copies drift.

    Below the floor it stops resolving rather than attaching a citation to the
    wrong sentence, which is worse than attaching none.
    """

    refs, flag = planning.resolve_criteria(
        "The Financial Authorities review the requisition", "", _index()
    )
    assert refs == [{"document_id": "d_sop", "document": "Procurement SOP Extracts.docx", "citation_id": "C4"}]

    refs, flag = planning.resolve_criteria(
        "Vendors are paid within thirty days of invoice receipt.", "", _index()
    )
    assert refs == []
    assert flag == "criteria_unresolved"


def test_the_same_sentence_in_two_documents_is_broken_by_the_hint():
    duplicated = (
        "DOCUMENT SUMMARY — Second Copy.docx\n"
        "- Financial Authorities review the requisition. [C9]\n"
    )
    index = planning.rcm_sentence_index(
        _request(_document_bundle(("d_sop", SOP_SUMMARY), ("d_copy", duplicated)))
    )

    hinted, flag = planning.resolve_criteria(
        "Financial Authorities review the requisition.", "c9", index
    )
    assert hinted == [{"document_id": "d_copy", "document": "Second Copy.docx", "citation_id": "C9"}]
    assert flag == ""

    # No usable hint: the first in bundle order, and the auditor is told.
    ambiguous, flag = planning.resolve_criteria(
        "Financial Authorities review the requisition.", "", index
    )
    assert ambiguous == [{"document_id": "d_sop", "document": "Procurement SOP Extracts.docx", "citation_id": "C4"}]
    assert flag == "criteria_ambiguous"


def test_a_quote_that_matches_nothing_falls_back_to_its_marker_and_says_so():
    refs, flag = planning.resolve_criteria(
        "Payment terms are net thirty days from receipt of a valid invoice.",
        "C7",
        _index(),
    )

    assert refs == [{"document_id": "d_sop", "document": "Procurement SOP Extracts.docx", "citation_id": "C7"}]
    assert flag == "criteria_unverified"


def test_a_criterion_that_resolves_to_nothing_keeps_its_quote_and_carries_no_ref():
    """A criterion an auditor can read is worth having without a pointer.

    What is never done is inventing a pointer to make one look sourced.
    """

    refs, flag = planning.resolve_criteria("Some clause nobody supplied.", "", _index())

    assert refs == []
    assert flag == "criteria_unresolved"


def test_an_empty_criterion_resolves_to_nothing_and_is_not_flagged():
    assert planning.resolve_criteria("", "", _index()) == ([], "")
    assert planning.resolve_criteria(None, "c4", _index()) == ([], "")


# The shipped template's risk section names each lens in full under the first
# process it applies to and abbreviates it under the rest. Both forms reached
# scoring as separate themes, and they did not behave alike.
_ABBREVIATED = """# APM

## Fraud risk and management override

* **Opportunity.** After-the-event approvals and the absence of any systematic
  exception review leave settlement possible without supporting evidence. The
  planned response is to vouch payments to approval and receipt evidence.

## Key risks and planned response

**Requisition and approval**

- **Segregation of incompatible duties.** The approvers differed from the
  requester on every comparable row.
- **Compliance with the entity's own stated policy.** The SOP requires an
  outcome to be recorded for every approved requisition.

**Purchase order**

- **Segregation.** Considered; no incompatible combination evidenced.
- **Compliance.** The SOP requires orders only with active vendors.
"""


def test_an_abbreviated_lens_is_the_same_theme_as_the_one_it_shortens():
    """A memo that shortens its own lens has not added a theme.

    "Segregation." carries one abstract noun a matrix never writes, so it was
    owned by nothing while "Segregation of incompatible duties." — the same
    lens, the same sentence away — was owned by the row that answers it. The
    matrix was failed for its vocabulary rather than its coverage.
    """
    themes = planning.planned_risk_themes(_ABBREVIATED)

    assert "Segregation." not in themes
    assert "Compliance." not in themes
    assert "Segregation of incompatible duties." in themes
    assert "Compliance with the entity's own stated policy." in themes


def test_a_theme_is_owned_by_answering_it_not_by_repeating_its_name():
    """Coverage is what the memo committed to, not the label over it.

    A fraud-frame label is a word no control matrix writes: no row states a
    risk of "opportunity". Scoring the name alone demanded the matrix repeat a
    heading, and the bullet's own sentence — the commitment — went unread.
    """
    rows = [
        {
            "process": "Invoice processing and payment",
            "risk": "An invoice may be settled without evidence that goods arrived.",
            "control": (
                "Settlement requires approval and a matching receipt, and "
                "exceptions are reviewed."
            ),
        }
    ]
    themes = planning.planned_risk_themes(_ABBREVIATED)
    texts = planning.risk_theme_texts(_ABBREVIATED)

    assert "Opportunity." in themes
    assert planning._unowned_themes(["Opportunity."], rows) == ["Opportunity."]
    assert planning._unowned_themes(["Opportunity."], rows, texts) == []


def test_a_row_answering_in_its_control_owns_the_theme():
    """The failure tells the model to fix the risk *and control*; both are read."""
    rows = [
        {
            "process": "Purchase order",
            "risk": "An order may be placed outside the delegation levels.",
            "control": "Segregation of duties is enforced between raising and approving.",
        }
    ]

    assert planning._unowned_themes(["Segregation of incompatible duties."], rows) == []


def test_shared_function_words_are_not_coverage():
    """A theme and a row share "the" by writing English, not by covering a risk."""
    rows = [{"process": "Payments", "risk": "The vendor may be paid in the wrong currency."}]

    assert planning._unowned_themes(["Compliance with the entity's own stated policy."], rows) == [
        "Compliance with the entity's own stated policy."
    ]


# --------------------------------------------------------------------------- #
# The cycle's vocabulary (step 5d of docs/rcm-generation-redesign.md)
# --------------------------------------------------------------------------- #
def _cycle_request(bundle=None, *, process_names=(), business_cycle=""):
    return WorkerRequest(
        worker_id="planning.rcm",
        capability_id="planning.rcm_ready",
        unit_id="rcm",
        context=bundle or _bundle(),
        unit_input={
            "input_sha1": "rcm-input",
            "process_names": list(process_names),
            "business_cycle": business_cycle,
        },
        activity={"artifact_refs": ["planning:apm"]},
    )


def test_the_risks_call_is_given_the_cycle_s_step_names_to_choose_from():
    gateway = _Gateway([json.dumps({"rows": [_row(process="Purchase order")]})])
    WORKERS.execute(
        _cycle_request(
            process_names=["Purchase order", "Invoice processing"],
            business_cycle="Procure-to-pay",
        ),
        gateway,
    )

    payload = json.loads(gateway.calls[0]["user"])
    assert payload["PROCESS NAMES"] == ["Purchase order", "Invoice processing"]
    assert payload["BUSINESS CYCLE"] == "Procure-to-pay"


def test_every_row_takes_the_cycle_s_name_as_its_business_cycle():
    """The cycle names itself; a row does not get to disagree with it."""

    gateway = _Gateway([
        json.dumps({"rows": [_row(process="Purchase order", business_cycle="Something else")]})
    ])
    result = WORKERS.execute(
        _cycle_request(process_names=["Purchase order"], business_cycle="Procure-to-pay"),
        gateway,
    )

    assert result.proposal["rows"][0]["business_cycle"] == "Procure-to-pay"


def test_a_row_keeps_its_own_business_cycle_when_no_cycle_has_been_designed():
    gateway = _Gateway([
        json.dumps({"rows": [_row(business_cycle="Accounts payable")]})
    ])
    result = WORKERS.execute(_cycle_request(), gateway)

    assert result.proposal["rows"][0]["business_cycle"] == "Accounts payable"


def test_a_process_outside_the_cycle_is_flagged_and_the_row_still_commits():
    """A flag, not an error: the shape may have missed a step, or the matrix may
    have invented a name, and the string cannot say which."""

    gateway = _Gateway([
        json.dumps({"rows": [_row(process="Petty cash disbursement")]})
    ])
    result = WORKERS.execute(
        _cycle_request(process_names=["Purchase order", "Invoice processing"]),
        gateway,
    )

    assert len(result.proposal["rows"]) == 1
    flags = [
        flag for flag in result.proposal["flags"]
        if flag["kind"] == "process_outside_cycle"
    ]
    assert len(flags) == 1
    assert "Petty cash disbursement" in flags[0]["message"]
    assert "Purchase order, Invoice processing" in flags[0]["message"]


def test_a_process_inside_the_cycle_is_not_flagged_whatever_its_case():
    gateway = _Gateway([json.dumps({"rows": [_row(process="purchase ORDER")]})])
    result = WORKERS.execute(
        _cycle_request(process_names=["Purchase order"]), gateway
    )

    assert not [
        flag for flag in result.proposal.get("flags") or ()
        if flag["kind"] == "process_outside_cycle"
    ]


def test_no_cycle_means_no_process_flag_at_all():
    """Before the shape exists there is no vocabulary to be outside of."""

    gateway = _Gateway([json.dumps({"rows": [_row(process="Anything at all")]})])
    result = WORKERS.execute(_cycle_request(), gateway)

    assert not [
        flag for flag in result.proposal.get("flags") or ()
        if flag["kind"] == "process_outside_cycle"
    ]
