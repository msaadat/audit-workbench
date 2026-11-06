# Risk and Control Matrix Guidance

<!-- This file governs the risk half of a row: which risks the matrix carries,
how they are worded, which process each belongs to and how each is rated. The
control is governed by `rcm_controls.md` and the control attributes by
`rcm_attributes.md`, each written by its own pass shown its own file. A
workspace override of this file that still carries the control or attribute
rules is harmless: this pass is not asked for those fields and ignores guidance
about fields it does not write. -->

## What a row is

One row = one risk = one failure of one duty, at one step of the cycle. A row
that lists several failures ("approved below the limit, after the fact, by the
requester, or split to avoid the limit") is several rows, or one row and a
judgement about which failure matters. Two rows describing the same failure are
one row.

Do not split the other way either. A duty that names several actions of one
kind is one duty and one row: "shall not confirm, verify or release settlement"
is one segregation duty, "amended or cancelled without approval" is one
approval duty, "records the dealer's identity and the time of execution" is one
record-keeping duty. Split only where the failures would need different
evidence to find, or carry clearly different exposure.

## Building the risk set

Work from the duties first, then look for what they miss.

**1. Duties.** For each in-scope step, the memorandum describes what the entity
must do: the policy requirements, approvals, records, limits and timeframes its
process flow and key risks name. Each duty is answered by at least one row. A
requirement with two distinct duties (deal within hours *and* prior approval
for one outside them) gets one row per duty. The risk is that the duty is not
performed: name the event at the step and what goes wrong.

**2. Gaps.** Then ask what could go wrong in this cycle that no stated duty
addresses: a population that could be incomplete, an exception with no
escalation path, a duty with no one checking it. Carry a gap as a row only when
it is a concrete failure at a step in scope that this engagement could find
evidence of, or one the memorandum names as a key risk. These rows are the
ones most likely to have no control, and they are worth carrying.

**3. Considered, not carried.** Everything else you weighed goes in
`considered`, with the reason it is not a row:

- `out_of_scope` — the planning context excludes it;
- `not_applicable` — it cannot arise in this cycle as the entity runs it;
- `covered_by` — an existing row already answers it (name the row by its risk);
- `responded_by_substantive_procedure` — a planned response, not a control
  (management override, incentive and pressure).

Account for every theme in MEMORANDUM THEMES in `considered`, one entry per
theme, with `theme` copied exactly as listed: `covered_by` naming the rows that
answer it, or the disposition and reason it is not carried. This is the
matrix's record of how each planned theme was answered.

The themes below are prompts for that thinking, not a list every step must
answer. **They are not assertions, not field values, and must never be copied
into any field of a row.**

1. Approval outside delegated limits, after the fact, or not at all.
2. One person initiating, approving, recording, or executing the same
   transaction; self-approval and self-verification.
3. Transactions split to stay below an approval or review limit.
4. Transactions with no underlying business need, or with unapproved,
   fictitious, or related-party counterparties.
5. Transactions unrecorded, recorded twice, or recorded at the wrong amount,
   quantity, or classification.
6. Unauthorized creation or amendment of standing data.
7. A downstream record accepted without agreement to the upstream records that
   support it.
8. Events recorded in the wrong period, or dependent steps performed out of
   order.
9. Exceptions not reviewed or escalated; management override.
10. Breach of the policy, contract, or regulation the process exists to satisfy.

Size follows the duties, not the checklist. A matrix normally carries one to
three rows per duty and a few gap rows; one much larger than that has usually
restated the checklist.

## Writing the risk

State the failure concretely, independent of whether it has happened: the
event, the step where it happens, and what goes wrong, in the cycle's own
nouns.

    Write: "A deal may be captured in the treasury system more than 15 minutes
            after it was executed."
    Not:   "Deal lifecycle events may be recorded in the wrong period or out of
            sequence."

    Write: "A purchase order may be raised for a vendor that is not Active in
            the vendor master."
    Not:   "Transactions may be recorded with unapproved, fictitious, or
            related-party counterparties."

    Write: "Purchase orders may be approved by officials below the required
            delegation level."
    Not:   "LIMIT_NOTES is missing in 75% of approval matrix rows."

Rules:

- No percentages, counts, null rates, column names, table names, file names, or
  document ids in a risk statement.
- No embedded cause ("...because the SOP defines no thresholds"). A risk
  describes the exposure, not its reason.
- No pre-concluded deficiency. Whether the risk has materialized is what
  fieldwork establishes; the matrix is written before that is known.
- One failure per risk, at most two closely joined ("approved without
  verification or without the approver recorded"). A list is a bundle.

## Fields

- **process** — the step of the cycle this row belongs to, chosen from the
  supplied PROCESS NAMES and spelled exactly as given. It groups the rows; the
  same name recurs across every row of its step. A duty that spans steps
  (segregation, for instance) sits at the step where it would be breached.
- **risk_rating** — `critical` only where a single failure permits material loss
  or fraud with no compensating control; `high` where the exposure is material
  but partly mitigated; `medium` and `low` below that. As anchors: money or
  settlement released without the required approval, match or confirmation, a
  segregation breach that lets one person both execute and approve or confirm,
  and approval beyond delegated authority are usually `high` or `critical`; a
  record-keeping lapse (a field not recorded, a record not retained) or a
  timeliness slip with no loss attached is usually `medium` or `low`. Rate against the band, not
  against the row beside it: a set of risks with no `medium` in it has not been
  rated, and neither has one where most rows share a rating. The rating is a
  property of the exposure alone.
- **business_cycle** — the cycle this row belongs to, in the engagement's own
  words. One engagement is normally one cycle, and every row names it
  identically.
<!-- section: One risk per row, non-duplicative. Use stable process and risk wording so reruns reconcile. Do not claim that a control exists unless the planning basis supports it. -->
