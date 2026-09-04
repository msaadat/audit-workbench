# Risk and Control Matrix Guidance

<!-- This file governs the risk half of a row: which risks the matrix carries,
how they are worded, which process each belongs to and how each is rated. The
control is governed by `rcm_controls.md` and the control attributes by
`rcm_attributes.md`, each written by its own pass shown its own file. A
workspace override of this file that still carries the control or attribute
rules is harmless: this pass is not asked for those fields and ignores guidance
about fields it does not write. -->

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

## Fields

- **process** — the process step this row belongs to, as named in the planning
  basis. It groups the rows; it is not a label for one of them. A cycle has a
  handful of steps and each carries several risks, so the same process name
  recurs across every row belonging to it, spelled identically — and a matrix
  naming a different process on every row has grouped nothing. Where the basis
  describes the flow without naming its steps, name a few covering the whole
  flow and reuse those. Keep the wording stable across runs so revisions
  reconcile.
- **risk_rating** — `critical` only where a single failure permits material loss
  or fraud with no compensating control; `high` where the exposure is material
  but partly mitigated; `medium` and `low` below that. Two rows describing the
  same underlying failure must not carry different ratings.

  Rate against the band, not against the row beside it. A matrix that rates
  almost everything `high` has stopped distinguishing, and the rating is the
  first thing a reviewer uses to direct effort — so a set of risks with no
  `medium` in it is a set that has not been rated. The rating is a property of
  the exposure alone: you have not yet been told what control the entity
  operates, and a risk is not lower because one exists.
- **business_cycle** — the cycle this row belongs to, in the engagement's own
  words: "Treasury dealing and settlement", "Procure to pay". It is the label
  the matrix chooses; nothing derives it, so a row that omits it carries none.
  One engagement is normally one cycle, and every row names it identically.
<!-- section: One risk per row, non-duplicative. Use stable process and risk wording so reruns reconcile. Do not claim that a control exists unless the planning basis supports it. -->
