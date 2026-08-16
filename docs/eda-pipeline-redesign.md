# EDA pipeline redesign

**Status:** E1, E2 and E4 landed; E3 landed in part (domains yes, sample rejected
on evidence); E5 and E6 not started. Four changes not in the original design were forced
by measurement and have landed — §7.3. Latest measured run
`20260816-114649-9f35cc`, reaching **18** answer-key items against the baseline's
**9** on §7.2's hand-built basis — **19 of 28** on the scorer now in the
repository, which additionally counts A23 and is the basis §7.5 uses.
**Date:** 2026-08-16 (design), implementation record §7 current to the same day
**Scope:** `analysis_workflow_v1` only. The audit workflow, document analysis and
document tests are untouched.
**Baseline:** `Workspaces/pro`, run `20260816-065650-652b21` at code `90de2a5` —
§6. Every change below is measured against it.
**Companion to:** `procurement-pipeline-review.md` (rev 609) and
`procurement-pipeline-review-pass3.md` (rev 838). Those two describe what the
pipeline produced; this one describes why the exploratory half produced it and
what replaces the exploratory half. Answer-key IDs (`A01`–`A33`) are Appendix A
of the second-pass review. New fix IDs in this document are `E*`.

Every measurement below was taken from two workspaces — `Workspaces/procurement`
(EDA run `20260813-192502-da3357`, the diagnosis in §1) and `Workspaces/pro`
(EDA run `20260816-065650-652b21`, the baseline in §6) — plus the six source
workbooks, verified byte-identical between the two and read directly. Nothing
here is inferred from reading code alone.

---

## 0. Decision summary

The exploratory pipeline is not underperforming because its stages are wrong. It
is underperforming because **it decides what can be found before it decides what
to look for**, and because it decomposes by data object rather than by question.

Five changes, in the order they should be built:

| ID | Change | Recovers | Baseline evidence | Status |
|---|---|---|---|---|
| **E1** | A deterministic probe layer that sweeps intra-frame then cross-frame column pairs and nominates test specs ranked by measured yield | A05, A13, A14, A27 | §6.5 — `compare_columns` and `format_anomaly` exist and are aimed at the wrong columns | **Landed** — `agent/probes.py`, six families |
| **E2** | Stop pruning the relationship map; let orphan counts nominate `referential` directly | A01, A03/A04 route, A30 | §6.5 — both `referential` tests that ran returned `ok`; the two that would have failed were never proposed | **Landed** — the `referential` family |
| **E3** | A 30-row diagnostic sample plus low-cardinality value domains in model context | A03/A04, A05, judgment quality throughout | §6.2 | **Half landed.** Value domains yes. The sample was built, measured, and **reverted** — §7.2 |
| **E4** | One `analysis.reading` turn over the whole map, replacing one proposal turn per frame | A33-class negative space; 19 turns → ~7 | §6.4 — 9 of 20 frames carry no analysis; compound hypotheses half-discharged | **Landed** — `agent/register.py`, `analysis.register_ready`. §7.5 |
| **E5** | Joins materialize *after* definitions, from what the accepted specs need | A11/A12, A13, A15, A21, A22, A28 | §6.4 — *"this test was prepared nowhere"*, twice, on the 2,856M item | Not started — still the largest open item |
| **E6** | `analysis.reconciled` — a coverage artifact over the assertion register | S1b, S5b's column half, S2d's input | §6.4 — nothing detects a hypothesis answered by half | Not started |

Steps E1 and E2 are wholly deterministic, need no prompt work and no model turn.

**What the record since has added to this summary.** The design's central bet —
that measuring before proposing is the lever — held, and by a wider margin than
argued here: across four measured runs a nomination yields roughly **ten times**
the exceptions of a model-authored analysis, and the reach against Appendix A went
**9 → 18** on a single consistent scoring basis (§7.2). What the design got wrong is where the remaining loss sits. It assumed
the binding constraint after E1/E2 would be *coverage*; measurement showed it
becomes **contention** — several frames able to compute the same thing, and the
machinery deciding between them doing so on grounds that had nothing to do with
which answer was better. Four unplanned changes (§7.3) were needed for that, and
three of the four were defects the design's own reasoning had already stated
correctly in prose without implementing.

The baseline sharpens the argument rather than softening it: at §6 the model
**states the right hypotheses aloud** — invoice approval beyond delegated
authority, `APPROVED_BY_ID == REQUESTER_ID` — and the per-frame stage either has
no frame to carry them or has one and writes no test. The gap is no longer
mostly a question of what the model can think of. It is a question of what the
architecture will let it act on.

---

## 1. Diagnosis

### 1.1 Five multiplicative filters run before the model's first substantive turn

| Filter | Where | Rule |
|---|---|---|
| pair pruning | `joins.py:733` | one candidate per table pair, best evidence |
| match-rate classification | `joins.py:369` | `weak` below a 0.90 match rate; `weak` on any row multiplication above 1.001 |
| noise floor | `joins.py:597`, `joins.py:716` | `weak` and match rate < 0.5 → `continue  # noise` |
| utility-gate admission | `adapters.py:1992` | only `strong`/`moderate` reach the model |
| one route per pair | `workers/analysis.py:69`, enforced `:466` | *"Exactly one candidate may be retained for any pair of tables, in either direction"* |
| lookup pruning | `capabilities/analysis.py:281` | frames under 5 rows and 10× smaller than the largest are not analysed |

None of them knows what question is being asked. They compose, and every one of
them narrows. The reference run that produced the answer key applied none.

### 1.2 The gates discard in proportion to audit signal

Diagnosed on `procurement`, from the run record:

| Relationship the pipeline diagnosed | Match rate | Unmatched | Fate | Item | Exposure |
|---|---:|---:|---|---|---:|
| `invoice.PO_NUMBER_LINK → po.PO_NUMBER` | 0.8376 | 19 | `weak` — never offered | A01 | 1,054M |
| `requisitions.REQUISITION_ID ← invoice+po` | 0.8304 | 19 | `weak` | A03/A04 route | 202M |
| `invoice.SUPERVISOR_APPROVAL_ID → matrix+staff` | 0.0179 | 110 | below the noise floor | A11/A12 | 2,856M |
| `po.BUYER_ID → staff.STAFF_ID` | 0.0000 | 93 | below the noise floor | A30 | — |

Verified independently against the workbooks: 20 invoices carry a
`PO_NUMBER_LINK` matching no purchase order, PKR 1,054,406,260, of which
563,602,720 is paid. That is A01 to the rupee. The pipeline computed
`unmatched_keys: 19` and deleted the candidate **for having them**.

The unmatched values are `REQ2024011`, `REQ2024047`, `REQ2024053` … — requisition
identifiers sitting in a purchase-order field.

The principle the gates encode is backwards for audit work: **a low match rate is
a finding about the population, not a verdict on the relationship.** The pipeline
is most certain to discard the relationships carrying the largest exposures, and
its certainty scales with the exposure.

### 1.3 One route per pair forecloses segregation of duties

`requisitions ↔ staff_details` produced four strong role keys, all at 1.0 match
and 1.0 multiplication: `FIN_APPROVED_BY_ID`, `REQUESTER_ID`, `VERIFIED_BY_ID`,
`APPROVED_BY_ID`. Three were rejected under protest — the run record carries the
model's own wording, *"The requester role is useful context, but…"*. The same
happened on `invoice ↔ staff` (`SUPERVISOR_APPROVAL_ID` dropped) and
`vendor ↔ staff` (`UPDATED_BY` dropped).

Segregation of duties is by definition a comparison of two roles on one
transaction. The contract forbids materializing two roles.

Five warnings recording exactly this sit in the run record and reach no reader.

### 1.4 The pipeline never compares two columns of the same table

Its entire model of "relationship" is between tables, so intra-frame column pairs
are never examined. Measured directly:

```
invoice_data:  VERIFIED_BY_ID == SUPERVISOR_APPROVAL_ID   1 row
               INV2024063, 98,061,570, Paid                        = A14
requisitions:  REQUESTER_ID   == VERIFIED_BY_ID           8 rows
               REQUESTER_ID   == FIN_APPROVED_BY_ID       1 row    = A13
```

Segregation of duties on this data needs **no join at all**. A14 has been
downgraded across three revisions and is reported to the client as tooling
unreliability; it is one comparison of two columns of `invoice_data`.

### 1.5 Where the 21 missed items go

| Root cause | Items |
|---|---|
| Relationship pruning (weak / noise discard) | A01, A03, A04, A11, A12, A16, A30 |
| One route per table pair | A14, A15, A28 |
| Missing test shapes | A05, A11, A12, A25, A27, A28 |
| Value blindness | A03, A05, A28, A33 |
| No challenge to a clean result | A07, A08, A09, A24 |
| Not the EDA's work | A29, A31, A32 |

Nineteen of twenty-one trace to four causes, all of them in the pipeline's front
half. The execution half — the sandbox, spec-not-data rerunnability, the evidence
sidecar, `result_sha1` linkage, the memo — is not where the loss is and is not
changed by this design.

### 1.6 Decomposition is not the problem; the axis is

The reference run was decomposed too — by question, inside one context. This
pipeline decomposes by data object: one proposal turn per frame. That choice has
three consequences and they are the design's real subject.

- A unit's context is one frame, so **no unit can hold a cross-frame hypothesis.**
  The only cross-frame turn is the memo, which runs after execution and can
  therefore summarize but never test.
- The unit count is set by the schema, not by the risk. Twenty frames, twenty
  turns, of which eleven produced date-ordering checks and eight of those found
  nothing.
- Narrowing is monotone. No later stage can recover what an earlier one discarded.

---

## 2. Decisions taken

**D1 — No whole-population disclosure.** The engagement is 418 rows / 6,244 cells
(~17k tokens as CSV) and would fit in one prompt, but the privacy boundary in
`AGENTS.md` stands. Model context gains two things only: low-cardinality value
domains, and a bounded **30-row diagnostic sample** under its own permission
(§4.2, E3). `allow_table_rows` remains denied everywhere.

**D2 — No cycle-registry dependency.** `cycle_registry/packs/procure_to_pay.py`
encodes a date lifecycle that would assert A16 outright, and it is deliberately
*not* used. The pipeline stays domain-neutral: structure is discovered
empirically from the data, and all domain meaning comes from the model. This is
not a compromise — see §3.1.

**D3 — `analysis.reading` requires no auditor intervention.** No gate, no
checkpoint. That imposes one rule: the reading turn is **additive by default** and
may only subtract with a recorded reason (§4.2, E4).

**D4 — Joins are an output of the analysis, not an input to it.**

---

## 3. Two findings that shape the design

### 3.1 Empirical invariant discovery replaces the cycle pack, and is better

An invariant does not have to be declared. It can be measured: for every pair of
comparable columns reachable on a route, compute the direction distribution. A
pair holding one way 95%+ of the time is an invariant the population asserts
about itself; the residue is the exception.

Run on the invoice↔PO route — the one the gates discarded — with no domain
knowledge and no model turn:

| Discovered invariant | Holds | Violations | |
|---|---:|---:|---|
| `PO_DATE ≤ INVOICE_DATE` | 94/98 | **4** | **A16** — 4 paid invoices, 35,236,240, exact |
| `INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT` | 93/98 | **5** | **A06** |
| `GRN_DATE ≤ INVOICE_DATE` | 93/98 | 5 | A17 |
| `GRN_DATE ≤ PAYMENT_DATE` | 91/94 | 3 | A18 |
| `PO_DATE ≤ DUE_DATE` | 98/98 | 0 | a confirmed invariant — reportable as *tested, holds* |

Domain-neutral, self-calibrating, and it generalizes to payroll or revenue with
no new code. The pack would have to be written per cycle and would still not know
that `PO_DATE ≤ DUE_DATE` is worth stating as tested.

**The route choice was not merely a risk — it cost coverage on the one item the
pipeline is credited with recovering.** The promoted invoice-over-PO test found
three violations; the discarded route finds five. `INV2024017` and `INV2025007`
were invisible because the GRN route aligns 96 invoices and the PO route aligns
98.

### 3.2 The same sweep finds within-group variance

`ITEM_DESCRIPTION → UNIT_PRICE` on `po_data`: 14 items appear more than once,
**none** at a single price. Top spreads 194% (Cybersecurity Training Platform) and
167% (Cloud Migration Services) — A27, including the two items the answer key
names.

This one carries its own warning. The dependency does not hold *at all*, so a
naive "flag the violations" flags every repeated item — the same vacuity trap
`SATURATION_SENSITIVE_TESTS` patches after the fact. The signal is the **spread
distribution and its tail**, not the violation set. 194% is a finding; 63%
probably is not. The probe layer must carry that discipline from the start.

---

## 4. Target architecture

### 4.1 The graph

```
data.characterized      det    types, low-cardinality domains, format census,
                               blanks, distinctness
data.relationship_map   det    every route diagnosed, unpruned; orphan counts and
                               where the orphans do resolve
data.probes             det    sweep intra-frame pairs, then cross-frame over
                               viable routes; emit ranked nominations as
                               {test, params, expected_breaches, tested}
analysis.reading        LLM×1  the whole map plus the 30-row diagnostic sample.
                               Keep / add / decline-with-reason. Writes the
                               durable assertion register.
analysis.definitions    LLM×n  one turn per assertion cluster; a frame carrying
                               no assertion gets no turn
data.joins_ready        det    materialize only the routes accepted specs need
analysis.executed       det    unchanged
analysis.reconciled     det    register × outcome — the coverage artifact
analysis.summarized     LLM×1  unchanged
```

Approximately seven model turns against today's twenty-two, with the expensive
one spent on judgment rather than enumeration. At `max_llm_concurrency: 1` and
97.4% model wait (second-pass review §8.1) that is also the latency line.

### 4.2 Stage contracts

**`data.characterized` (E3, deterministic half).** Extends the existing profile
with the two things the model currently cannot see: the complete distinct set for
any column under ~30 distinct values, and a format census over identifier columns
using the mask already implemented at `analytics.py:1219`. `REQUISITION_STATUS ∈
{Approved, Pending, Rejected}` is what makes A03/A04 askable at all.

**`data.relationship_map` (E2).** Diagnoses every route and **prunes nothing**.
The `weak` classification survives as a *label*, not as a gate. Orphan counts, and
the table where the orphans do resolve, are read as findings and nominate a
`referential` spec directly. On this engagement that is A01 and A30, from
measurements already present in the run record.

**`data.probes` (E1).** The layer's job is not to be a second test engine — with
`compare_columns` and `referential` in the registry, a discovered invariant and an
executable test are the same object. It sweeps, measures, and emits nominations
**in the library's own vocabulary**, ranked by yield.

Order matters: **intra-frame column pairs first** (cheapest, and on this
engagement they own A13 and A14), then cross-frame over transient alignments.
Bounding rules — type compatibility, minimum support, a stated confidence, and
the vacuity discipline of §3.2 — are part of the contract, not a later patch.

This is what fixes the proposal pathology at its root. Today the model proposes
date comparisons from schema shape: two date columns exist, so compare them. With
measured yield in hand it proposes from evidence, and `PO_DATE ≤ DUE_DATE` costs
one line as a confirmed invariant instead of a proposal slot.

**`analysis.reading` (E4).** One turn, the whole map, plus the sample. Output is
an ordered assertion register: what should be true, which columns and routes state
it, why it matters, and — separately — what this data cannot answer. Every
assertion must name columns and routes from the supplied map, validated
deterministically by the machinery `validate_analysis_proposal` already provides.

Because there is no auditor gate (D3), **the deterministic nominations are the
default set and the reading turn may only subtract with a recorded reason** — the
same decline-with-reason contract `analysis_promotion` already uses, counted in
the coverage artifact. That gives a hard floor: A01, A05, A06, A13, A14, A16 and
A30 are all deterministic nominations and land whether or not the reading turn
mentions them. What the model adds is what a sweep cannot reach: what a pattern
*means*, cross-cutting reads, and negative space — a model holding the complete
column inventory can say *"no field anywhere in this data records competitive
bidding"* (A33), which twenty keyhole turns structurally cannot.

The register is durable and inspectable after the fact. Not a gate, but not
invisible: reconciliation reports against it.

**The 30-row diagnostic sample (E3, disclosure half).** Not a preview —
**stratified over what the deterministic layers already found**:

- rows from each minority format cluster
- rows whose keys are orphans
- rows breaching each candidate invariant
- rows with nulls in otherwise-populated columns
- a handful of ordinary rows for baseline

A random 30 of 118 is a coin flip on seeing a `VINSUSP` row. A stratified 30
spans the anomaly space at the same token cost. It follows the existing precedent
exactly: its own permission (`allow_population_sample`), its own cap, recorded in
the manifest, and **split by source so truncation cannot silently drop the
interesting half** — the lesson `analysis_exceptions` / `analysis_anomalies`
already learned. The memo's existing rule about capped rows extends verbatim and
matters more here: **never count from the sample.**

**`analysis.definitions`.** Units become assertion clusters sharing a target
frame, rather than one unit per frame. Frames carrying no assertion get no turn,
which is where most of the turn saving comes from.

**`data.joins_ready` (E5).** Today "join" conflates a durable workspace artifact
the auditor sees with a transient alignment needed to compute a statistic.
Separating them removes the scarcity that produced one-route-per-pair: **probe 70
routes, materialize 8.** Materialization happens after definitions, from what the
accepted specs actually need, so multiple routes between one pair are ordinary
rather than contested.

**`analysis.reconciled` (E6).** Register × outcome: answered, unanswerable,
declined-with-reason, never-attempted. This replaces promotion-as-afterthought —
promotion adjudicates only analyses that flagged something (eleven zero-exception
analyses at rev 838 sit at `promotion: null`), whereas reconciliation adjudicates
the register, so *asked and unanswerable* becomes distinguishable from *never
asked*. It is the artifact `S1b`, `S5b` and `S2d` have each been reaching for from
a different direction.

No separate `analysis.challenged` turn. The duplicate-key qualifier rule
(`workers/analysis.py:1049`) handles the known case deterministically, and once
every clean result traces to a probe that measured the same thing, *"could this
test see what it cleared"* is a reconciliation check rather than a model call.

---

## 5. Already landed

Committed at `90de2a5` ("improved analytics library inventory"). These implement
most of the library work this design assumed, and one thing better than it
assumed. §6 measures what they actually produced on a live run.

| Addition | Where |
|---|---|
| `referential` — reconcile a column against another table's key, reporting **where the unmatched values do resolve** | `analytics.py:1029`, `_resolves_in` at `:989` |
| `compare_columns` — a relationship between two columns of one row, six operators, auto type mode | `analytics.py:1138` |
| `format_anomaly` — learns the character-class shape a column takes and flags the minority, only where one shape governs | `analytics.py:1232`, `value_mask` at `:1219` |
| `signal` taxonomy — `exception` / `screening` / `descriptive`, typed at the registry | `analytics.py:1325`, accessors `signal_for` `:1737`, `ids_with_signal` `:1747` |
| Base-rate vacuity gate — a weekend scan flagging ~2/7 established nothing | `analysis_results.py:119` |
| `descriptive` tests excluded from autonomous proposal | `workflows/analysis.py`, `EXCLUDED_ANALYTICS_TEST_IDS` |
| Lookup candidates in definition context, with per-table branches so an invalid `(lookup_table, lookup_column)` pair is unrepresentable | `adapters.py:1908`, `presets.py` `lookup_candidates` |
| Duplicate-key qualifier rule — `T7` | `workers/analysis.py:1049` |
| `reference_candidates` — the schema-only half of `candidate_keys`, so a caller holding only metadata can ask which column plausibly references which key before any frame is read | `joins.py:216` |

Three notes.

`_resolves_in` is stronger than what this design originally proposed. Pattern-
classing the orphans would have said "19 values shaped `REQ\d{7}`"; naming the
table says *"19 of them are `requisitions.REQUISITION_ID` values"*, which is A01's
actual finding and hands the model the route to A03/A04 at the same time.

The `signal` taxonomy is the field the second-pass review's §8.3 was circling —
*established nothing* versus *redundant with a cheaper signal* — typed once at the
registry instead of guessed at in three places. It also does `F5`'s work: once the
severity basis reads the signal, a design-gap finding and a paid exception cannot
be rated alike.

`reference_candidates` is the first piece of **E2** in place. It answers "which
column plausibly references which key" from names alone, which is exactly the
question `data.relationship_map` must ask across every frame pair before it
measures anything — and asking it without reading a frame is what makes an
unpruned map affordable.

Each test run by hand against the source data with default parameters — what the
primitive *can* do, given the right column. What the pipeline actually aimed them
at is §6.

| Test | Result | Owns |
|---|---|---|
| `format_anomaly` on `VENDOR_INVOICE_NUMBER` | dominant `A{4}9{3}-9{6}` 95/118 (80.5%); minority `A{7}9{3}` ×6 | **A05**, exactly the six VINSUSP rows |
| `compare_columns` `PO_DATE ≤ INVOICE_DATE` | 4 breaches | **A16** |
| `compare_columns` `INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT` | 5 breaches | **A06** |
| `compare_columns` `VERIFIED_BY_ID ≠ SUPERVISOR_APPROVAL_ID` | 1 breach, INV2024063, 98.06M, Paid | **A14** |
| `referential` `PO_NUMBER_LINK → po.PO_NUMBER` | 19 orphans, resolve in `requisitions.REQUISITION_ID` | **A01** |
| `referential` `BUYER_ID → staff.STAFF_ID` | 93 orphans, resolve nowhere | **A30** |

A tuning observation on the first: there is a third cluster,
`A{4}9{3}-9{6}A` ×17 (14.4%), that the 10% default does not flag. It is not in the
answer key, but seventeen invoice references carrying a trailing letter is worth a
look — the threshold is doing real work and 10% may be tight.

**Remaining library gap:** within-group variance — same key, different value.
That is A27 and nothing currently expresses it.

### 5.1 Landed since this design was written

| Addition | Where | Owns |
|---|---|---|
| **The probe sweep** — six families (`referential`, `comparison`, `equality`, `values`, `duplicates`, `format`), each nomination a runnable spec pre-run with its counts and a one-line reading | `agent/probes.py` | E1, E2 |
| **`value_domains`** — the complete vocabulary of each low-cardinality column, with per-column JSON-Schema enums making an unnameable value unrepresentable | `probes.py`, `adapters.py`, `presets.py`, `workers/analysis.py` | E3's surviving half |
| **`value_filter`** — rows whose value in one column is, or is not, among named values, in both `flag` and `allow` directions | `analytics.py:997` | The shape the library could not express; A03/A04 |
| **The `values` probe family** — a minority state in a column that has a usual one, selected by a dominance floor and a minority ceiling, both calibrated against the engagement's own fifteen vocabularies | `probes.py:850` | A03 |
| **`frame_root` / `frame_route`** — which table a frame's rows are rows *of*, and by which key each joined-in table was reached | `joins.py:515`, `:541` | The population half of analysis identity |
| **Population-aware analysis identity** | `workers/analysis.py:762` | A12, A10, A30 |
| **The sweep outranks the hypothesis router** | `analysis_execution.py:897` | 6 of 18 frames that were being dropped unmeasured |

**Library gap now closed except A27.** Within-group variance is still unexpressed
and is still the last one.

---

## 6. Baseline — the `pro` engagement

**Workspace:** `Workspaces/pro`
**Run:** `AgentRuns/20260816-065650-652b21`, `analysis_workflow_v1`
**Code:** `90de2a5`
**Data:** the same six workbooks as `procurement`, verified byte-identical

This is the reference point every change in §8 is measured against.

**Read the scale carefully.** This run is the EDA alone — no RCM, no fieldwork, no
report. The reviews' tally (12/33 at rev 838) counts items that *reached a
finding and the report*. This one counts items an EDA procedure **computed**,
which is a strictly earlier and more forgiving bar. The two numbers are not
comparable and must never be quoted against each other.

### 6.1 What the run produced

| | |
|---|---:|
| Model turns | 19 — 2 `join_utility`, 15 `analysis_definitions`, 2 `analysis_summary` |
| Prompt tokens | 244,974 |
| Wall clock | 11m 53s |
| Joins materialized | 14 |
| Frames | 20 (6 base + 14 joins) |
| Frames carrying no analysis | **9** |
| Saved analyses | 26 |
| Run status | `completed_with_failures` |

Test mix: `date_lag` ×10 (five with zero exceptions), `compare_columns` ×4,
python ×4, `duplicates` ×2, `referential` ×2, `completeness` ×1,
`format_anomaly` ×1, `outliers` ×1.

**No memo was produced.** `analysis.summary` failed twice — *"Worker
'analysis.summary' returned an invalid response after 2 attempt(s): an embed
names no analysis"* — so `analysis.summarized` never completed. A new defect,
unrelated to this design, and it means the run's one cross-frame reading of the
results does not exist.

### 6.2 EDA reach against the answer key

A31 and A32 are document-side and outside the EDA's scope, leaving 31 addressable
items.

| Outcome | Count | Items |
|---|---:|---|
| **Computed** | 9 | A07, A08, A10, A16, A18, A19, A20, A23, A29 |
| **Partial** | 2 | A02 (inside a 22-row completeness result, not isolated), A06 (3 of 5) |
| **Absent** | 20 | the rest |

### 6.3 The new library tests, working

| Analysis | Spec | Result | |
|---|---|---|---|
| `A-1CFB3868` | `duplicates` on `VENDOR_INVOICE_NUMBER` **alone** | 2 keys, 4 rows | **A07 + A08 recovered.** `T7`'s qualifier rule did exactly its job — the same test keyed `(VENDOR_ID, VENDOR_INVOICE_NUMBER)` returned a false clear for two revisions. |
| `A-152A3303` | `date_lag` `PO_DATE → INVOICE_DATE` | 4 rows, 35,236,240 | **A16 recovered.** A two-revision regression, closed. |
| `A-5E16319E` | `compare_columns` `ESTIMATED_TOTAL_COST ≤ MAX_APPROVAL_AMOUNT` | 1 | A10 |
| `A-1E10FA83` | `compare_columns` `INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT` | 3 | A06, partial — see §6.5 |
| — | base-rate gate | 2 proposals dropped | two weekend analyses, at 17% and 33% against 29% expected by chance |

### 6.4 Three failure modes, all one cause

The run makes the decomposition-axis argument (§1.6) far more sharply than the
earlier one did, because this time the model **states the right hypotheses out
loud at the join-utility turn** and the per-frame stage fails to carry them.

**1. Asked, and refused.** Two retained hypotheses name the largest single missed
item in the register:

> *"Invoice amounts exist that exceed the MAX_APPROVAL_AMOUNT recorded for the
> job title of the staff member who approved them."*
>
> *"Invoices exist where the approving supervisor's JOB_TITLE has a
> MAX_APPROVAL_AMOUNT lower than the INVOICE_AMOUNT, i.e. approval beyond
> delegated authority."*

That is **A11/A12, PKR 2,856M**. `_warn_untestable_hypotheses`
(`analysis_execution.py:329`) then reported, twice:

> *"No materialized frame brings together financial_approval_matrix,
> invoice_data, staff_details, so this test was prepared nowhere."*

The model identified the test. The join architecture refused to build the frame,
said so precisely, and nothing acted on it. This is **E5**'s case in one
artifact.

**2. Compound hypotheses half-discharged, undetected.** The requisitions/staff
hypothesis reads *"Requisitions exist where **APPROVED_BY_ID equals REQUESTER_ID**,
or where the approver's JOB_TITLE limit is below the ESTIMATED_TOTAL_COST."* The
limit clause became `A-5E16319E`. The segregation clause became nothing — that is
**A13**. Identically, the vendor hypothesis carried *"the UPDATED_BY staff member
is not authorized … or a vendor's BANK_ACCOUNT_NUMBER was changed"*: the first
clause became `A-3147AD3F`, the second became nothing — **A21**, which the
earlier `procurement` run did catch.

Nothing anywhere detects a hypothesis that was half answered.

**3. Frame built, hypothesis stated, no test written.** Nine of twenty frames
carry no analysis, and three of them are frames the utility gate materialized
*for* a retained hypothesis:

| Empty frame | Its retained hypothesis | Cost |
|---|---|---|
| `invoice_data_staff_details_joined` | invoice approval beyond delegated authority | A11/A12 |
| `requisitions_staff_details_joined` | `APPROVED_BY_ID == REQUESTER_ID` | A13 |
| `invoice_data_vendor_master_file_joined` | invoices to non-active vendors | A23 invoice side, A02 |

### 6.5 Four defects this design already predicted, now observed

**The route choice is non-deterministic, and it moves what is findable.** On
byte-identical data the one-route-per-pair gate chose differently than the
`procurement` run: `invoice↔staff` VERIFIED_BY_ID → **SUPERVISOR_APPROVAL_ID**,
`requisitions↔staff` FIN_APPROVED_BY_ID → **APPROVED_BY_ID**, `vendor↔staff`
APPROVED_BY → **UPDATED_BY**. The candidates are equally evidenced (1.0 match,
1.0 multiplication), so the tie-break is arbitrary. Consequence: A21 and A22,
both reached by the earlier run, are unreachable in this one. **Which findings
are available is a coin flip.**

**The GRN route still costs coverage.** `A-1E10FA83` tests `INVOICE_AMOUNT ≤
PO_TOTAL_AMOUNT` on the GRN-joined frame: `tested: 96`, 3 breaches. The
`PO_NUMBER_LINK` route aligns 98 and finds 5 — `INV2024017` and `INV2025007` are
invisible. §3.1's prediction, reproduced exactly.

**Segregation of duties is now expressible and still absent.**
`compare_columns` makes `VERIFIED_BY_ID ≠ SUPERVISOR_APPROVAL_ID` a one-line spec
on `invoice_data` needing no join at all. Across all 26 analyses,
`VERIFIED_BY_ID`, `SUPERVISOR_APPROVAL_ID` and `REQUESTER_ID` are referenced by
**zero** tests. The primitive exists; nothing points the model at the column
pair. This is **E1**'s case.

**`format_anomaly` was aimed at the wrong column.** It ran on
`staff_details.EMAIL_ADDRESS` — *"No format governs EMAIL_ADDRESS; 16 shapes
across 52 values"*, 0 exceptions — rather than at `VENDOR_INVOICE_NUMBER`, where
§5 shows it isolates the six VINSUSP rows outright. Same cause as above: a
capability with nothing pointing it.

**Both `referential` tests that ran returned `ok`.** `APPROVED_BY → staff` (39
tested) and `APPROVED_BY_ID → staff` (108 tested), both clean. The two
reconciliations that would have *failed* — `PO_NUMBER_LINK → po.PO_NUMBER` (19
orphans, A01) and `BUYER_ID → staff.STAFF_ID` (93 orphans, A30) — were never
proposed, because the relationship map had already discarded those routes before
any turn saw them. Neither column is referenced by any test in the run. This is
**E2**'s case, stated as plainly as it can be: *the pipeline only reconciles the
relationships it already believes in.*

### 6.6 The baseline in one line

Twenty-six procedures, nineteen model turns and 245k tokens produced nine of
thirty-one answer-key items — and the three biggest absences were each named
aloud by the run itself, in a hypothesis, a warning, or an empty frame.

---

## 7. Implementation record

Seven runs on byte-identical data, same model (`deepseek/deepseek-v4-flash-0731`,
`max_llm_concurrency: 1`), across four workspaces. A workspace keeps only its
latest run, so four of the seven survive as scoreable artifacts; the other three
are recorded from measurements taken at the time.

### 7.1 The series

| # | Change under test | Workspace / run | Turns | Prompt tokens | Analyses | Frames swept | Flagged nominations | Key items |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 0 | **Baseline** (`90de2a5`) | `pro` `…065650-652b21` | 19 | 244,974 | 26 | — | — | 9† |
| 1 | E1 + E2 | `pro` `…074814-408ee8` | 16 | 244,487 | 34 | 12 | 50 | **9** |
| 2 | E3 with the 30-row sample | `pro2` (superseded) | 22 | ~374k | 13 | 12 | — | 4† |
| 3 | E3 sample reverted | `pro2` (superseded) | 8 | ~84k | 21 | 12 | — | 7†‡ |
| 4 | Join-gate repair + `value_filter` | `pro2` `…093126-6480c8` | 15 | 269,041 | 39 | 12 | 53 | **14** |
| 5 | Sweep before the gate; `values` family | `pro3` `…102919-f3ccd6` | 24 | 418,041 | 45 | 18 | 115 | **13** |
| 6 | Ranking, identity, disclosure | `pro4` `…114649-9f35cc` | 23 | 413,495 | 50 | 18 | 116 | **18** |
| 7 | E4 — the register | `pro5` `…161822-fceda8` | 5 | — | 23 | 5 | 23 | **13**§ |
| 8 | E4 + the gate fix | `pro5` `…163049-d059ea` | 14 | 297,607 | 39 | 18 | — | **18**¶ |

**Bold counts are scorer-derived and mutually comparable** (§7.2). The three
marked **†** are not: each workspace keeps only its latest run, so runs 0, 2 and 3
no longer exist as artifacts and their counts were taken by hand at the time, on
bases that differ from the scorer's and from each other. The baseline's 9 and run
1's 9 are *not the same nine* — §6.2's nine are A07, A08, A10, A16, A18, A19, A20,
A23, A29, while run 1's are the scorer's. They must not be read as "no change".

**‡** Run 3 materialized **zero** joins — the gate rejected all 16 candidates — so
its seven are base-table only and not comparable with anything.

**§** Run 7 materialized zero joins for a different reason: the join gate failed
both attempts, and every join unit blocked behind it (§7.6). Its thirteen are
base-table only and are not comparable either — but they are the same shape of
run as run 3's seven, and the difference between 7 and 13 is what the register
commits without a model turn.

**¶** Run 8 is the first clean E4 run and §7.7 reads it. Its eighteen are the
workspace's cumulative state: a ninth run over the same workspace re-stamped the
analyses it re-committed with its own id, so per-run attribution does not
partition after a repeat and the honest figure is the workspace's.

Run 6 is `completed`; every earlier run whose record survives is
`completed_with_failures`, and in each the sole failure was `analysis.summary`,
across four distinct modes: an embed naming no analysis; a hallucinated procedure
id; the memo emitted twice; and an earlier double-rejection. Run 6's success is
**not attributable to anything in this document** — it follows commit `bd3e00d`, a
separate rewrite of the summary worker landed the same minute the run began.

### 7.2 Reach against the answer key

Scored by matching the *computation* — spec signature plus count — never the
title, since titles move between runs and the test a frame ran does not. A hit
flagging its whole population is rejected as saturated rather than counted, which
is why A12 shows `sat.` at run 5 and A30 at run 1.

| item | run 1 | run 4 | run 5 | run 6 |
|---|---:|---:|---:|---:|
| A01 invoice PO link that is not a PO | 19/118 | 19/118 | 19/118 | 19/118 |
| A03 invoices against Rejected requisitions | — | — | — | **4/118** |
| A05 VINSUSP re-billing | — | 6/118 | 6/118 | 6/118 |
| A06 invoice exceeds its PO total | 3/118 | 3/118 | 3/118 | 3/118 |
| A07/A08 duplicate vendor invoice number | — | 4/118 | 4/118 | 4/118 |
| A10 requisition approved above the limit | 1/112 | 1/112 | — | 1/112 |
| A12 approver title absent from the matrix | 48/52 | 110/118 | *sat.* | 110/118 |
| A13 requester also verifies | 8/112 | 8/112 | 8/112 | 8/112 |
| A14 approval outside the verifier's chain | — | — | — | **18/118** |
| A16 invoice dated before its PO | — | 4/118 | 4/118 | 4/118 |
| A17 receipt dated before the GRN | — | 5/118 | 5/118 | 5/118 |
| A18 payment before goods receipt | 3/118 | 3/118 | 3/118 | 3/118 |
| A19 payment before the invoice arrived | — | 1/118 | 1/118 | 1/118 |
| A21 vendors sharing a bank account | 4/39 | 4/39 | 4/39 | 4/39 |
| A22 vendor created and approved by one person | 5/39 | 5/39 | 5/39 | 5/39 |
| A27 unit price vs requisition estimate | — | — | 2/93 | 2/93 |
| A29 staff sharing a bank account | 6/52 | 6/52 | 6/52 | 6/52 |
| A30 `BUYER_ID` resolves to no staff record | *sat.* | — | — | **96/118** |
| **Reached** | **9** | **14** | **13** | **18** |

**The one measurement that governs everything else.**

| | taken nominations | exceptions | model-authored | exceptions | ratio per analysis |
|---|---:|---:|---:|---:|---:|
| Run 4 | 19 | 221 | 18 | 13 | **16×** |
| Run 5 | 25 | 237 | 20 | 147 | 1.3× — or **6×** excluding the single vacuous 118 |
| Run 6 | 25 | 341 | 25 | 31 | **11×** |

Run 5's raw figure is the exception that proves the rule: 118 of its 147
model-authored exceptions are one saturated analysis establishing nothing about
any row. Stated plainly: *what the sweep nominates gets taken, and what it does
not nominate mostly does not get used.* Every subsequent decision follows from
that, including the ones below.

### 7.3 Four changes measurement forced, none of them in the original design

**The sweep was subordinated to a router that guesses.** A joined frame with no
retained hypothesis routed to it was dropped as redundant — before the sweep had
been allowed to look at it. Six of eighteen frames went that way in three
milliseconds each, one of them a 118-row frame holding an invoice's payment status
beside its requisition's approval status. The gate now consults the measurement
and the sweep gets the last word (`analysis_execution.py:897`). Frames swept: 12 →
18; flagged nominations 53 → 115.

**Rarity had to be ranked as strength, for one family only.** Every other family
counts how badly a rule was broken, so more breaches is a stronger nomination. The
`values` family counts how much of the population sits in a state, and its whole
premise is that the *rare* state is the exception. Ranked the common way, `Pending
Payment` (13 of 118) outranked `Rejected` (4 of 118) on every frame carrying both,
was taken as the column's one nomination, and **A03 stayed unreached through a
whole run in which it was nominated eleven times**. One line, and it owns A03.

**Analysis identity knew the columns and not the rows.** Identity was derived from
which columns from which tables — right for a column a join carries through
unchanged, wrong for one the join re-samples. The approval-matrix reconciliation
reads one column of one table whichever frame asks it, and answers 48 of 52 over
the staff master, 110 of 112 over invoices keyed to their approver, and 118 of 118
over invoices keyed to their verifier. Three questions, three answers, one
identity — and whichever frame ran first silently deleted the other two,
alphabetically. §1.3 of this document states the problem correctly in prose and
the code did not implement it; the `lookup` carve-out at `workers/analysis.py`
even says *"the difference is the finding"* in a comment beside the rule that
erased it. Identity now carries the frame's root and join route. Measured over the
engagement: 34 → 43 distinct ids, 30 groups still deduplicating correctly, and
**zero** groups collapsing measurements that differ (was 3). Owns A12, A10, A30.

**Removals were silent.** A proposal removed by the validator appeared in no
artifact: the frame showed what it kept, and nothing recorded that more had been
written. One run removed 38 proposals across 18 frames and said nothing about any
of them. Now carried out of the worker as `declined` and merged into the
executor's `dropped` channel, which commits in every mode. *This one is not yet
confirmed on a live run* — the first attempt put the disclosure in
`approval_provider`, which is only bound when `run["mode"] == "permission"`, so it
was dead on every unattended run and produced nothing at run 6. Fixed and tested
at the executor boundary; unverified in the field.

### 7.4 What the runs disproved

**The 30-row diagnostic sample (E3's other half) is reverted.** Run 2 cost 53%
more tokens for 62% fewer analyses than run 1, produced four saturated results and
leaked sample rows into procedure notes. The value domains alone — the vocabulary
without the rows — carry the whole of the benefit E3 was proposed for. The design's
"sample discipline" risk in §9 was not a risk to be managed; the sample was simply
not worth its cost.

**Turn count did not fall, and should stop being a target.** E4 was argued partly
on 19 turns → ~7. Runs 5 and 6 use 23–24 turns and 413k tokens against the
baseline's 19 and 245k, and are unambiguously the better runs. Cost per frame fell;
frames rose. E4's case has to rest on cross-frame reading, which is real, and not
on turn count.

---

### 7.5 E4 — what landed, and the one number that argues for it

`analysis.register_ready` sits between the joins and the definitions and carries
two units: `analysis_reading`, one model turn over the whole map, and
`analysis_register`, one deterministic commit. The register is run-durable, like
the relationship evidence beside it.

**The floor is the finding.** Before any turn runs, the sweep over every scoped
frame is deduplicated by `analysis_semantic_id` and committed as saved analyses.
On the `pro4` data that is **116 flagged nominations resolving to 43 distinct
computations**, and executed against the answer key it reaches **18 of 28
scoreable items with no model turn at all** — against run 6's 19 from 23 turns
and 413k tokens. Scored by the same scorer, now in the repository (§8.1 item 5,
`backend/tests/eda_answer_key.py`), which reproduces §7.2's run-6 column item for
item.

| | pro4 (run 6) | the floor alone |
|---|---:|---:|
| Model turns | 23 | **0** |
| Saved analyses | 50 | 43 |
| Answer-key items | 19 | **18** |

The two the floor does not reach are A10 and A14, and neither is a loss the sweep
could have prevented: **the sweep never nominated either one**. They were
authored by the model in run 6, which is exactly what the reading turn's `add`
exists for. What the floor *gains* is **A03r** — `REQUISITION_STATUS = Rejected`,
4 of 112, §8.1's second item, nominated on five frames and taken on none in every
run on record.

That table is the whole argument for spending one turn on the entire engagement.
§9 called a single reading turn a single point of failure, and it would be if the
turn were the only author. Because the register is additive by default — silence
keeps a nomination, and only a decline needs an argument — the turn's worst case
is eighteen items rather than zero. A reading that fails after its repair attempt
settles as `skipped` and the commit unit writes the measured floor.

**Three defects found while building it, all of the same family as §7.3's.**

*The register unit re-billed itself when a join landed.* Unit ids were derived
from the frame list, so materializing a join produced a *different* unit id — and
the old one had already spent its turn. A two-table scope billed the reading turn
twice. Both register units are now named for the capability, like the memo.

*A reference that resolves nowhere was four findings.* The sweep names one master
per referencing column, ranked by orphan rate — and when every candidate fails at
100%, the tie breaks on alphabetical order among the tables outside that frame's
lineage. `BUYER_ID` arrived eight times across two populations and four masters,
saying the same thing. The register folds those on `(root, column)` and keeps the
one that ruled out the most masters; the title states what was found rather than
naming a master the data did not choose. This is §9's contention again, in the
one family §9 exempted from the saturation rule.

*The register broke the stage below it by succeeding.* `analysis.definitions_ready`
decided both its readiness and its unit expansion by asking "does this frame
carry an analysis" — which the register now makes true of every frame that
measured anything, so no definition unit expanded at all. Expansion no longer
filters on it, and the binder makes the distinction expansion cannot: which of a
frame's analyses the register wrote, and which a definition turn did. The
zero-model-turn property of `analysis_execution` moved to the register's own
readiness, where it now lives.

**What E4 did not do.** Turn count is unchanged in shape — one reading turn
replaces nothing, it is added, and definitions still fans out per frame carrying
an authored assertion. §7.4 already withdrew that half of the argument. The
`MAX_NOMINATIONS_PER_FRAME = 12` cap is also untouched and is now doing different
work than it was designed for: it bounded what one definition turn could read,
and it now bounds the floor itself. Six of eighteen frames sit at it on this
engagement. Raising it is cheap — the register deduplicates aggressively and the
cost is Polars passes, not tokens — but it is a separate measurable change and
§7.3's lesson is that adding coverage without measuring arbitration subtracts.

### 7.6 Run 7 — the gate failed, and the floor held

The first run on E4 code failed at `data.join_utility_ready` and materialized no
join at all. The cause is not E4's and predates it: the gate asks for one
decision per candidate as an *array* whose `ref` is an enum, which prevents an
invented reference and does nothing about a repeated one. Given sixteen
candidates the model returned **sixteen decisions covering six distinct
references**, four of them answered four times over. Both attempts failed the
duplicate rule and all fifteen join units blocked behind it.

Fixed structurally rather than with a third attempt: `decisions` is now an object
**keyed by the candidate reference**, every reference required and nothing else
permitted, and the decision body no longer carries a `ref` of its own. A repeat
is unrepresentable, a missing decision is unrepresentable, and an invented
reference is unrepresentable — the same move `value_domains` makes for a value a
column does not hold. The array shape is still parsed, because a model that
ignores the schema is still answering the question.

**What the run establishes about E4.** The gate took every join down and the run
still completed: the register committed **23 measured analyses** across six base
tables and the memo wrote. Against run 3 — the earlier zero-join run, whose seven
base-table items §7.1 calls "not comparable with anything" — this one reaches
**13**. That is the floor doing exactly what it was built for, observed under a
failure nobody staged.

**What it does not establish.** The reading turn ran, validated, and returned
`{keep: [], add: [], decline: [], unanswerable: []}` — nothing. It had five base
frames, no joined frame, and no gate hypotheses, because the stage that produces
those had already failed. Its whole purpose is the cross-frame read, and there
were no cross-frame frames; A10 and A14, the two items the floor misses, both
need one. So this is close to no evidence about the turn either way.

The one part of that answer the degenerate map does not excuse is
`unanswerable`. Negative space is a statement about the column inventory, the
turn held the whole inventory, and A33 is a real finding on this engagement. One
empty answer is not a pattern, and the prompt is deliberately **not** being tuned
on it: changing the instructions now would confound the first clean measurement.
Re-run with the gate fixed, then judge.

### 7.7 Run 8 — the first clean reading turn

The gate fix held: one attempt, fourteen joins materialized, every stage
succeeded except the memo. **14 model turns against the baseline's 23**, and
**18 of 28 items against its 19**.

The reading turn finally had the map it was designed for, and used it. Over
eighteen frames it kept 39 nominations (renaming six), **authored eight
assertions**, declined four with arguments, and wrote three statements of what
the data cannot answer. Two of those matter beyond this engagement:

> *"Was the purchase competitively sourced with independent quotes as policy
> requires?"* — **A33**, stated by the pipeline for the first time.
>
> *"Is there a conflict of interest or ownership link between the
> requester/approver and the vendor?"*

Neither is a procedure and neither will ever be a result. They exist because one
stage held the whole column inventory, which is the argument §4.2 made for E4 and
the first evidence for it. The authored assertions carried the approval-authority
shape (A10, reached) and the requester/approver segregation.

**And it lost A30, reproducibly, to a defect this document introduced.** Both
runs on the register declined `BUYER_ID reconciles to no imported master`, with
the same reasoning: *"BUYER_ID identifies a staff member, but the spec tests it
against requisitions.REQUISITION_ID"*. The critique is correct about the spec and
wrong about the finding. §7.5's collapse folds the eight duplicate nominations
onto one and must still name *a* master in the runnable params; every candidate
failed identically, name affinity scores all four at 0.7, and so the choice is
arbitrary — and it reads as a mis-aimed probe. The `reading` line said "checked
against …" and that prose did not survive contact.

The lookup cannot be made better, so the arbitrariness is now disclosed as data:
an unreferenced nomination carries `lookup_is_arbitrary` and a note saying every
master was checked, and the prompt names this as the one decline that is a
mistake. Unverified on a live run.

This is the third time in this record that a collapse rule cost a finding, and
the shape is always the same: the rule is right about what to fold and silent
about what the fold throws away. §9's warning generalizes further than it was
written — **on this pipeline, arbitration that hides its own arbitrariness
subtracts.**

### 7.8 The memo failure, diagnosed — a fifth mode and a sixth

Run 8's memo failed with *"the response is not a valid JSON object: Expecting
value at character 0. The text around that position reads: ..."*. The response
was three tab characters. That error names nothing actionable, and it was
masking two separate defects.

**Attempt 1** returned one tool call carrying 15,017 characters of arguments —
**malformed JSON at character 9,853**. Recoverable in principle, and the repair
turn is exactly for it.

**Attempt 2 returned two tool calls, 11,253 and 4,708 characters, both valid
JSON with every required key.** Every forced-tool extractor in this module
required *exactly* one match and fell through to `content` otherwise — which on
a tool-call response is empty. So the run discarded two complete memos, read
three tabs, and failed the stage. **It lost its memo while holding two good
ones.** The same guard sat in all four workers, including the reading worker
E4 had just added.

That is the "memo emitted twice" mode §7.1 lists — the one `bd3e00d` was
understood to have killed. It did kill it in Markdown; it reappeared as two tool
calls, in a code path that turned a known content fault into an unreadable
transport one.

Fixed by reading the first complete submission instead of discarding all of
them. A model that submits twice has repeated itself, not contradicted itself.

**And a redundancy rule that could only fail.** With the response recovered, the
semantic validator rejected it nine times, every one of the form *"attributes a
sentence to [#13] but does not list 13 in its `procedures`"*. That rule asked the
model to state one fact in two places, consistently, across a long structured
document — the coupling a long emission breaks by construction, and a rule with
nothing to say about whether the memo was right. A marker in the prose is now
read as the declaration it already is, which also *widens* the checks that
matter: numbers reached only through prose now face the range and
informativeness tests they previously escaped.

Replayed through both fixes, run 8's discarded response validates down to **two**
errors, and both are the validator doing its job — one procedure argued in the
findings section and again in the reliance section, with a repair message that
says what to do about it.

`bd3e00d` hardened what the model *writes*. These two are about what the
application *reads*, which is why the hardening did not reach them.

---

## 8. Sequencing

Items 1–3 are done; the table is kept with status so the ordering argument stays
readable against what it produced.

| Order | Item | Why here | Status |
|---|---|---|---|
| 1 | **E1** `data.probes`, intra-frame pairs first | Highest yield, no model risk. Also what makes the three new tests get proposed against the right columns instead of waiting to be guessed. Owns A13, A14, A16, A06. | **Done.** A14 and A16 landed; A13 held throughout |
| 2 | **E2** unprune the relationship map; orphans nominate `referential` | Owns A01, A30, and hands over the A03/A04 route. The measurements already exist in the run record. | **Done.** A01 from run 1; A30 needed the identity fix to survive |
| 3 | **E3** diagnostic sample + value domains in context | Makes A03/A04 askable and lifts judgment quality throughout. | **Domains done; sample rejected on evidence** — §7.4. A03 needed the ranking fix as well |
| 4 | **E4** `analysis.reading` replacing per-frame proposal | The cross-frame win. Depends on 1–3 for its input. The turn-count half of the argument is withdrawn — §7.4. | **Done** — §7.5. The floor reaches 18 with no model turn; A03r landed. Unmeasured on a live run |
| 5 | **E5** joins after definitions | Dissolves one-route-per-pair. Owns A15, A28; completes A14. | **Not started, and now the largest single item.** See below |
| 6 | **E6** `analysis.reconciled` | Closes `S1b`; supplies `S2d` its content. | Not started |
| 7 | Within-group variance primitive | Last library gap. Owns A27. | Not started. A27 is reached at 2/93 by a different test, so this is now about doing it *properly* rather than reaching it at all |
| 8 | Regression fixture over Appendix A (`S6`) | Makes every step above measurable. Three regressions landed unnoticed in the last pass, and §6.5 adds a fourth kind — one that moves between runs on identical data. | **Done.** `backend/tests/eda_answer_key.py` plus `test_eda_answer_key.py`, which pins `pro4` at 19 |

The original claim here was that steps 1 and 2 alone would recover seven items
before any model-facing change. §7.2 does not support it. Run 1 — E1 and E2, no
prompt work — reached nine: A01, A06, A10, A12 (staff-side, 48/52), A13, A18, A21,
A22, A29. A05 and A16 arrived at run 4, A14 and A03 only at run 6, and A30 was
*taken* at run 1 but saturated, then absent for two runs, and became a usable
finding only once identity was fixed. Determinism delivered the floor it promised;
it did not deliver its own list on its own schedule.

### 8.1 What to do next, in order

1. **The `invoice_data ↔ requisitions` relationship is never diagnosed.**
   `PO_NUMBER_LINK` carries two kinds of value: 19 of 118 invoices hold a
   requisition id rather than a PO number, and five of those requisitions are
   `Rejected` — **5 invoices, PKR 202,094,220**, which is A03/A04 in full. The
   value family reaches four of them from the invoice side alone; the join is what
   makes it the finding. This is E5's territory and it is the biggest thing left.
2. ~~**A03r — the requisition side.**~~ **Closed by E4.** It was nominated on
   five frames and taken on none because taking was a model decision; the
   register commits it. 4 of 112, 158.0M.
3. **Single-sidedness is now the dominant loss channel.** At run 6, 33 of 38
   removed proposals were dropped for reading one side of a joined frame, against
   only 5 for repetition. Several read like real tests. Worth measuring before
   assuming the rule is right.
4. **Confirm the disclosure fix on a live run** (§7.3, fourth item).
5. ~~**Land the scorer as a test**~~ **Done** — `backend/tests/eda_answer_key.py`.
   It reproduces §7.2's run-6 column exactly and pins `pro4` at 19, so the next
   run is scored rather than read.

Two items outside the E-series:

- **The memo failure.** Diagnosed at last — §7.8. `bd3e00d` fixed what the model
  writes and could not reach two defects in what the application reads: a
  forced-tool extractor that discarded a duplicate submission rather than taking
  the first, and a marker/`procedures` redundancy rule that a long emission
  breaks by construction. Both fixed, unverified on a live run. Note also that
  the memo is now the *most expensive* turn in the pipeline — 100–160s per call
  at ~113k prompt tokens, 35% of run 8's wall clock and 69% of run 9's — because
  E4 removed the definition turns that used to dwarf it.
- **Route tie-breaking is arbitrary (§6.5).** Partly overtaken: identity now
  distinguishes routes rather than collapsing them, so an arbitrary tie-break no
  longer silently deletes the better answer. The *choice* of which single route to
  materialize per pair is still arbitrary, and that is E5.

---

## 9. Risks and open items

Marked against what the runs showed: two held, one of them costing a finding; one
is withdrawn outright; two remain untested because the work they attach to is not
built. The risk that actually did the damage is not on the original list.

**Small populations manufacture spurious invariants.** *Held, and handled.*
`MIN_COMPARISON_ROWS = 20` and `INVARIANT_HOLD_RATE = 0.85` bound it, and no run
has produced a nomination traceable to coincidence at these sizes. Still live at
smaller frames.

**The vacuity trap moves rather than disappearing.** *Held, and it cost A10.* At
run 5 the model wrote the approval-limit comparison with its operands inverted,
flagging 99% of 112 rows; the saturation gate correctly killed it and the item was
lost for that run. The gate is right and the loss is real — this is now a case for
stating the invariant direction in the nomination rather than for loosening the
gate. Note also that `referential` is deliberately *not* saturation-sensitive: A30
is a legitimate 96-of-118 finding, and a blanket rule would delete it.

**One reading turn is a single point of failure.** *Answered structurally, still
untested in the field.* E4 is built, and the register is additive by default
precisely because of this risk: the turn may add freely and subtract only with a
written reason, so a reading that fails after its repair attempt costs the run
its judgment and none of its evidence. Measured, the floor that survives such a
failure is 18 of 28 items (§7.5). What remains untested is the turn itself on a
live provider — no run has yet exercised it.

**Combinatorics.** Unchanged. At 84 columns the sweep costs seconds; 500 columns
is still untested.

**Sample discipline.** *Withdrawn.* The sample is reverted (§7.4), so there is no
row disclosure to discipline. What replaced the concern is narrower and was real:
value domains published staff **names and email addresses** as "vocabularies" on
seven joined frames, because a join re-profiles a 52-row dimension attribute into
a category. Fixed by judging a column where it lives (`probes.py:711`), plus a
word cap for prose columns. Worth remembering as the shape of the next disclosure
bug: *the join changed what the column looked like, not what it was.*

**The real risk was none of these: contention.** Every one of the risks above is
about a single measurement being wrong. What actually cost the most was several
frames being able to compute the same thing and the machinery choosing between
them on grounds unrelated to which answer was better — alphabetical order, in
practice. It cost the 2,855.6M item for a whole run. Adding frames (§7.3, first
item) made it worse before identity was fixed, which is the general warning: **on
this pipeline, more coverage without better arbitration can subtract.**

**Not addressed here.** `T6` (lookup anchoring in *test* generation) is a
fieldwork defect and is unaffected by this design. `S5b`'s row-wise reachability
count is still not built — this design gives route and column coverage, not "how
many rows of each population no executed test ever touched".

---

## Appendix — measurement provenance

All paths are relative to `Workspaces/procurement` in §1 and §3, and to
`Workspaces/pro` in §6. §7 names its own workspace per row.

| Claim | Source |
|---|---|
| §7.1 turns, tokens, wall clock, run status, frames swept, flagged nominations | each run's `run.json` — `usage`, `status`, and `analysis.probes` |
| §7.2 reach, all four runs | each workspace's `Analyses/A-*.json` filtered on `agent_run_id`, matched on spec signature and `last_result.exception_count`, saturated hits rejected |
| §7.2 nomination-vs-authored yield | the same records, joined to `analysis.probes` on the canonical `(test, params)` of each nomination |
| §7.3 six frames dropped unmeasured, three milliseconds each | `pro3` `events.jsonl`, `unit_update` records with `kind: analysis_definition`, `started_at`/`finished_at` |
| §7.3 identity grouping, 34 → 43 ids | `analysis_semantic_id` recomputed over every flagged nomination on all 18 frames, before and after |
| §7.3 38 proposals removed, 33 single-sided / 5 repeats | `pro4` proposal sidecars, `declined`, against the tool-call arguments in `Debug/LLMCalls` |
| A03/A04 route: 5 invoices, 202,094,220 | `invoice_data.PO_NUMBER_LINK` matched against `requisitions.REQUISITION_ID` where `REQUISITION_STATUS = 'Rejected'` |
| The four workspaces hold identical data | sha256 over all six workbooks, `pro`↔`pro2`↔`pro3`↔`pro4` |
| Relationship candidates, strengths, match rates, retained/rejected decisions, the five one-route warnings | `AgentRuns/20260813-192502-da3357/run.json`, `analysis.relationships` and `analysis.join_utility` |
| Baseline turn counts, tokens, warnings, run status and failure text | `Workspaces/pro/AgentRuns/20260816-065650-652b21/run.json` — `usage`, `warnings`, `error` |
| Baseline hypotheses, retained and rejected | same record, `analysis.join_utility` |
| Baseline analyses, specs, verdicts and denominators | `Workspaces/pro/Analyses/A-*.json`, field `last_result` |
| Baseline frame coverage (9 of 20 empty) | `Workspaces/pro/workspace.json` tables + joins, against the `table` field of each analysis |
| The two workspaces hold identical data | md5 over all six workbooks |
| 20 invoices without a real PO, 1,054,406,260 (563,602,720 paid) | `Data/invoice_data.xlsx` × `Data/po_data.xlsx` |
| A16: 4 paid invoices, 35,236,240 | invoice↔PO inner join on `PO_NUMBER_LINK` |
| A06: 5 breaches on the PO route vs 3 on the GRN route | both routes computed and compared |
| A13 / A14 intra-frame equality counts | `requisitions.xlsx`, `invoice_data.xlsx` |
| A27 spreads (194%, 167%) | `po_data.xlsx` grouped on `ITEM_DESCRIPTION` |
| A05 format census | `analytics.value_mask` over `VENDOR_INVOICE_NUMBER` |
| A12: 110 of 118 invoices approved by a title absent from the matrix | `invoice_data` × `staff_details` × `financial_approval_matrix` |
| 418 rows / 6,244 cells / ~17k tokens | all six workbooks |
| 28 saved analyses, 11 `date_lag`, 8 of those with zero exceptions | `Analyses/A-*.json` |
