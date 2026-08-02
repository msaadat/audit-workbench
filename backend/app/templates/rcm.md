# Risk and Control Matrix Guidance

## What a row is

One row = one risk + one control. Never bundle several controls into one row: if
a process relies on three controls, that is three rows. Keep rows
non-duplicative — two rows describing the same underlying failure are one row.

## Building the risk set

Work in two passes.

**Pass 1 — the standard risk universe.** Before considering any engagement
observation, enumerate from your own knowledge of this business cycle the risks
a competent auditor would expect to consider for each in-scope process. Every
process in the planning basis carries its standard risks whether or not the
supplied material mentions them. A process nobody has commented on is not a
process without risk.

For each process, work through this checklist of risk themes and ask what could
go wrong under each. **These themes are a prompt for your thinking only. They
are not assertions, not field values, and must never be copied into any field of
a row** — see the `assertion` field below for the closed list that field takes.

1. Approval outside delegated limits, after the fact, or not at all.
2. One person initiating, approving, recording, or executing the same
   transaction; self-approval and self-verification.
3. Transactions split to stay below an approval, competitive-sourcing, or review
   limit.
4. Transactions with no underlying business need, or with unapproved,
   fictitious, or related-party counterparties.
5. Transactions unrecorded, recorded twice, or recorded at the wrong amount,
   quantity, or classification.
6. Unauthorized creation or amendment of standing data (counterparties, bank
   details, prices, limits); duplicates; stale records.
7. A downstream record accepted without agreement to the upstream records that
   support it.
8. Events recorded in the wrong period, or dependent steps performed out of
   order.
9. Performance or exceptions not reviewed; management override; exceptions
   handled outside the defined process.
10. Breach of the policy, contract, or regulation the process exists to satisfy.

Discard the themes that genuinely do not apply to the process. Do not discard a
theme merely because the supplied material is silent about it — silence is the
normal state of planning material, and is itself a reason to carry the risk.

**Pass 2 — tailor to the engagement.** Use the planning basis to adjust wording,
rating, and the control description. Engagement observations refine the risk
set; they never define it. Never emit a row whose only content is a restatement
of a supplied observation or audit note.

## Writing the risk

State what could go wrong, in generic auditor wording, independent of whether it
has happened.

    Write: "Purchase orders may be approved by officials below the required
            delegation level."
    Not:   "LIMIT_NOTES is missing in 75% of approval matrix rows."

    Write: "Invoices may be paid without evidence that the goods were received."
    Not:   "GRN_ID_LINK is missing in 18.64% of invoice records."

Rules:

- No percentages, counts, null rates, column names, table names, file names, or
  document ids in a risk statement.
- No embedded cause ("...because the SOP defines no thresholds"). A risk
  describes the exposure, not its reason.
- No pre-concluded deficiency. Whether the risk has materialized is what
  fieldwork establishes; the matrix is written before that is known.
- Exception rates, quantified conditions, and deficiencies belong to tests and
  findings, not to this matrix.

## Writing the control

Record the control management asserts is in place, as it currently operates.

- Where the planning basis shows no control for a risk, write "No control
  identified" and rate the risk accordingly. Never phrase a recommendation as
  though it were an operating control ("A formal exception procedure defines
  ..."): a control that does not exist cannot be tested, and the design gap is
  a finding, not a row.
- Describe the control's mechanics — who performs it, over what population, how
  often, and what happens when it detects an error. Do not describe how it will
  be tested.

**Never assert a system behaviour the planning basis does not state.** Do not
write that a system enforces, prevents, blocks, validates, restricts, or makes
something impossible unless the planning basis says so in terms. A field
existing in a table is evidence that a value is *recorded*, never evidence that
it is *controlled*, *validated*, or *required* — those are different claims and
a schema cannot support them. Where the basis names a control but not its
mechanism, describe what it is asserted to do and add "mechanism not confirmed
in the planning basis". An unconfirmed mechanism is a testable question; an
invented one leads the engagement to place reliance on a control that may not
exist.

    Write: "The SOP requires the requisitioning department to verify a
            requisition before approval; the requisition record captures a
            verifier and an approver. Whether the system prevents the same
            person from performing both is not confirmed in the planning basis."
    Not:   "ERP workflow requires separate VERIFIED_BY_ID and APPROVED_BY_ID
            fields; system prevents the same user ID from populating both."

**The wording rules for risks apply to the control field too.** No percentages,
null rates, counts, column names, or table names. Do not append a deficiency
clause ("...; but GRN links are missing in 18.64% of invoices", "...; the null
rate suggests gaps"). Whether the control operates is what fieldwork
establishes — a quantified condition in this field pre-concludes it, and a
statistic read from a profile is not a fact about the population. Describe the
control; leave the exceptions to the tests.

## Fields

- **process** — the process step as named in the planning basis. Keep the
  wording stable across runs so revisions reconcile.
- **risk_rating** — `critical` only where a single failure permits material loss
  or fraud with no compensating control; `high` where the exposure is material
  but partly mitigated; `medium` and `low` below that. Two rows describing the
  same underlying failure must not carry different ratings.
- **assertion** — the assertion the risk threatens. **Exactly one of this closed
  list, spelled exactly as shown:** `Existence`, `Completeness`, `Accuracy`,
  `Authorization`, `Valuation`, `Cut-off`, `Compliance`, `Operational`. Use
  `Operational` where the risk is operational and threatens no
  financial-statement assertion, rather than forcing a fit. The risk themes in
  the checklist above are not assertions: never write `Validity`, `Master Data`,
  `Segregation of Duties`, `Matching and Reconciliation`, `Monitoring and
  Override`, `Cut-off and Sequence`, `Completeness and Accuracy`, or any other
  value into this field. Do not combine two assertions with "and".
- **control_type** — `preventive` where the control stops the error before it
  occurs, `detective` where it identifies the error afterwards. Validation,
  mandatory-field, and blocking rules are preventive *where the planning basis
  establishes that they operate*; reconciliations, exception reports, and
  after-the-fact reviews are detective. Judge the control's own mechanics, not
  the severity of the risk. Classifying a control as preventive is not a licence
  to assert a system mechanism the basis does not state.
- **criteria** — the specific clause, policy section, matrix, or standard the
  control is measured against. Cite only a criterion that appears in the
  planning basis. If none does, leave the field empty.
- **control_owner** — the role accountable for operating the control. **Name a
  role only if that role appears verbatim in the planning basis. If the basis
  names no owner for this control, leave the field empty.** An empty owner is a
  question to put to the client; an invented one is a false attribution that
  survives into the working paper. Never infer an owner from the nature of the
  control — a system-enforced control does not imply an IT or systems
  administration owner unless the basis names one.

`criteria` and `control_owner` are both optional. Leaving either empty is the
correct answer whenever the planning basis does not supply it, and is never a
reason to guess.

## Reading supplied table profiles

Table profiles are value-free shape statistics: row counts, distinct counts,
null percentages, minima, and maxima. They do not show what the table contains.

- A null percentage is not an exception rate. Nulls routinely reflect legitimate
  workflow states — a record not yet at that stage, a field that does not apply,
  or a deliberate "no limit" marker.
- A maximum is the largest value present, not a policy ceiling.
- Never state a fact about the population, and never conclude that a control has
  failed, from a profile statistic. Profiles show which processes exist and
  which fields carry them; they are not evidence.

<!-- section: One risk per row, non-duplicative. Use stable process and risk wording so reruns reconcile. Do not claim that a control exists unless the planning basis supports it. -->
