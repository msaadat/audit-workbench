# End-to-end pipeline review — `procurement` workspace

Assessment only. Ground truth was derived independently from the six Excel files and seven
documents before any pipeline artifact was opened, then compared stage by stage. The answer key
is Appendix A.

**Workspace:** `Workspaces/procurement`, revision 609 (regenerated 2026-08-14 at `c720572`)
**Population:** 112 requisitions · 93 POs · 118 invoices · 39 vendors · 52 staff
**Value:** invoices PKR 3,103,467,230 (paid 2,510,413,690) · POs 1,934,810,970

> **Second pass.** The first revision of this document reviewed revision 471. Everything it
> raised that has since been fixed has been **removed** from the body — this file now describes
> only what is still open, plus the defects the fixes themselves introduced. §0 records what was
> closed so nobody re-does it. Fix IDs (`P*`, `R*`, `T*`, `F*`, `S*`) are carried over unchanged
> where the item survives, so cross-references from `vouching-grid-plan.md` and
> `rcm-generation-quality.md` still resolve.

---

## Verdict

Recall to the report went from **3/33 (9%) to 11/33 (33%)**, with 5 more items surfaced in
planning but not tested. The three structural breaks diagnosed in the first pass — `evidence_kind`
blocking data tests, the missing goods-receipt row, and requisition-anchored test construction —
are closed at their point of origin, and the largest item in the population (a PKR 100,000,000
invoice paid with no vendor, no PO and no GRN) is now a critical finding in the report.

What is left divides into three kinds:

1. **New defects introduced by the fixes.** A 4-row reference table is now the anchor for 8 of 34
   test steps, which produced a *"No control identified"* row concluding **`effective`** on the
   splitting risk — the same false-negative class as the first pass, on a higher-risk subject.
2. **Subsystems correctly wired but still producing nothing.** The RCM now classifies 10
   attributes as `transaction_cycle`; zero cycle tests are generated, for a mechanical reason
   traced in §4.
3. **Items never remediated.** The QA-channel split (`F1`/`S4`), finding deduplication (`F2`),
   root cause (`F6`) and the regression harness (`S6`) were not implemented.

One confirmed regression: 4 paid invoices dated before their own PO (35.2M) were caught in the
previous run and are not caught now.

| | Rev 471 | Rev 609 |
|---|---:|---:|
| RCM rows | 19 | 28 |
| `evidence_kind: inquiry` attributes | 9 | **0** |
| `transaction_cycle` attributes | 0 | **10** |
| Data tests | 16 | 27 |
| Cycle vouch tests | 0 | **0** |
| Findings → report | 13 → 12 | 23 → 23 |
| Answer-key items intact to report | 3 | **11** |

---

## 0. Closed — do not re-open

Verified against revision 609. Listed so the remediation history stays legible.

| ID | Fix | Evidence it landed |
|---|---|---|
| **P1** | EDA runs before the APM | `AgentRuns`: documents 19:20 → analysis 19:25 → APM 05:19. The memo carries a populated `## Data analytics performed` section. |
| **P2** | Audit period derived from the populations | `Planning/APM.md:7` proposes 10 Jan 2023 → 16 Aug 2025 from the date columns, qualified as an observation. |
| **P3** | Fraud risk section | `## Fraud risk and management override` with six named risks incl. splitting and concealed payment destinations. |
| **P4** | Value for money | "Economy of amounts committed" is a lens in all three process sections. |
| **R1** | `evidence_kind` decoupled from control existence | `agent/workers/planning.py:400` states the rule explicitly. **0 `inquiry` attributes** across 28 rows. |
| **R3** | Goods-receipt / three-way-match rows | RCM-3B36FB, RCM-24B2C0, RCM-1843A6, RCM-7A0E02. |
| **R5** | Fraud-lens coverage rows | Splitting ×2, duplicate payment, vendor/bank integrity ×4, override. |
| **T1 / S3** | Cycle bypass is no longer silent | The test-generation run record carries 10 warnings, one per unvouched `transaction_cycle` attribute, each naming row and requirement. |
| **T2** (RCM half) | `transaction_cycle` classification carried through | 10 attributes across 7 rows, up from 0 of 52. The *test* half is still open — see §4. |
| **T3 / S5** (anchoring half) | Steps declare a population and invoice tests start from `invoice_data` | `DAT-EBEDB382B8` filters paid invoices with a null `PO_NUMBER_LINK` → **INV2024144 recovered as a critical finding**. The reconciliation half is still open — see §3. |
| **F3** | Findings → report reconciliation | 23 findings, 23 reported. `generation_warnings` is populated rather than `[]`. |

Partially closed, remainder tracked below: **R4/S2** (`_evidence_ceiling` exists and fired once —
§2), **F4** (values in the Key Findings table, no total — §5), **F5** (an `info` tier exists — §5).

Recovered answer-key items: A02, A13, A17, A18, A20, A21, A22, A23, A26.

---

## 1. Recall scorecard

Only the rows that are still not intact. Items now fully captured (A02, A10, A13, A17, A18, A19,
A20, A21, A22, A23, A26) are dropped from this table and marked in Appendix A.

| # | Issue present in the source | Exposure | Pipeline outcome at rev 609 |
|---|---|---|---|
| G1 | 20 invoices paid/pending against a **requisition id or nothing** in `PO_NUMBER_LINK` — no PO exists | 1,054M (563.6M paid) | **Partial, unchanged** — F-DD71D6 reports 8 *requisitions* with no downstream PO and F-59CFF3 reports the 1 invoice with a null link. The 20-invoice, 1,054M invoice-side population is still never stated. |
| G3 | 4 invoices raised against **Rejected** requisitions; INV2025009 re-raises rejected REQ2024047 for the identical 44,102,320 | 202M | **Missed.** The only `Rejected` predicates in the test set are payment-status valid-value lists. |
| G4 | Six invoices numbered **`VINSUSP001`–`VINSUSP006`**, each re-billing a closed prior-year PO | 157M | **Missed.** The string occurs in one analysis and nowhere else. |
| G5 | Invoice exceeds PO total — incl. **80M billed against an 8M PO** | 79.75M variance | **Planning-only.** `A-379F8C80` found 3, and the APM names `INV2025008` as its *most significant* analytics result. **No data test references `PO_TOTAL_AMOUNT`.** Nothing carries it to a test or a finding. |
| G6/G7 | Duplicate vendor invoice numbers across **different** vendors (`VINV011-202404`, both paid at 16,193,000); same vendor + identical amount (V1002 30M twice) | 46M | **Missed.** `DAT-EBEDB382B8` keys on `(VENDOR_ID, VENDOR_INVOICE_NUMBER)` — the same key `A-272B7E11` uses to return a false clear. `VINV011-202404` occurs nowhere in the workspace. |
| G8b | Invoice-side authority never tested: INV2024079 27.6M approved by Financial Controller (limit 5M); 110 of 118 invoices approved by roles absent from the matrix | 2,855M | **Missed.** No test joins `invoice_data` to `financial_approval_matrix` on approver role. |
| G10a | Invoice dated before its PO (4, all paid) | 35.2M | **Regression.** Caught at rev 471 by `DAT-85368A37FA` → F-13D9B6. No test now compares `INVOICE_DATE` to `PO_DATE`; `DAT-CA8CF3ABF5` checks `GRN_DATE` against both and never the pair. |
| G12b | 5 vendors created and approved by the same person is **caught**, but V1016 approved the same day it was added, and no vendor approved outside Procurement, are not | — | **Missed.** `DAT-788C5224E4` flagged 38 of 39 vendors on approval timing, was marked semantically invalid, and ended `review_required` — burying A24 inside an over-broad predicate. |
| G14 | Same item, wildly different unit price: Cybersecurity Training Platform +194%, Cloud Migration +167% | — | **Missed.** RCM-BE8381 (value for money, medium) carries a document test only; no population price comparison runs. |
| G15 | 94 of 112 requisitions state a department contradicting the HR master; 3 staff pairs share a bank account; `BUYER_ID` B001-B006 reconcile to no staff record | — | **Missed** (staff bank accounts reach the APM narrative only). No test references `BUYER_ID`. |
| G16 | GRN2024004 signed "Received and inspected by" by **Ethan Smith (1041) — the requisitioner**; invoice dated 10 days before the PO | 2M | **Missed.** Extraction now captures the approval block as *"Vendor representative / OfficeSupply Co."*; the handwritten name is still lost. |
| G17 | SOP §3.2 mandates RFQ/comparative bid evaluation — **no bid or quotation field exists in any table** | 2,868M | **Partial.** A scope-limitation bullet and F-35A263 raise it at document level; the population-wide absence of evidence for the whole 2.87bn is still never stated. |
| **N1** | *New* — RCM-DA9CE3, a row whose control is *"No control identified"*, concluded **`effective`** on the splitting risk | — | See §2. |
| **N2** | *New* — 8 of 34 test steps are null/validity checks on the 4-row `financial_approval_matrix` | — | See §3. |

---

## 2. RCM

28 rows. `inquiry` is gone, the goods-receipt rows exist, and rows asserting no control now carry
`tabular_population` attributes and get real data tests. That change alone recovered A02, A20,
A21, A22 and A26.

### 2.1 A "No control identified" row concluded `effective`

This is the RCM-4301AC false negative from the first pass, reproduced on a higher-risk subject.

```
RCM-DA9CE3  risk:       Management may circumvent an approval threshold by dividing a
                        commitment among transactions, suppliers, departments, or stages.
            control:    No control identified
            attributes: 1 × tabular_population
            tests:      DAT-FFE8BB2518 (data) — 0 exceptions
            conclusion: effective
```

`DAT-FFE8BB2518` aggregates invoices per vendor and compares the total against
`MAX_APPROVAL_AMOUNT.max()`. The matrix encodes the CEO's "above 10,000,000" as
**999,999,999,999**, so the comparison is unsatisfiable by construction and returns zero
exceptions, which the rollup reads as a clean pass.

`_evidence_ceiling` (`rcm_execution.py:570`) exists and is correct in what it checks — it caught
RCM-D45365 and downgraded it. RCM-DA9CE3 escapes through the deliberate single-attribute
exemption at `rcm_execution.py:612`:

```python
if len(attributes) < 2:
    return ""
```

The exemption's reasoning is sound for its stated purpose (a one-attribute row cannot be
*selectively* tested, and matching one requirement's wording against one test title measures
nothing). It just does not cover this case, where the problem is not selectivity but that the row
asserts no control exists at all.

**Fix S2b.** A row whose `control` field says *"No control identified"* may never reach
`effective`, independent of attribute count or test outcome. Absence of a control is not
something a zero-exception population test can establish; the strongest available conclusion is
`partially_effective` with the scope limitation stated. Add the check ahead of the
single-attribute return in `_evidence_ceiling` so it applies before the selectivity logic.

**Fix S2c.** Sentinel magnitudes must not be consumed as thresholds. `999,999,999,999` is an
"unlimited" marker, not a limit. Either the profiler flags implausible threshold magnitudes so
the test worker will not compare against them, or the comparison is inverted to test *against the
approver's own row* rather than the population maximum. This defect also reaches the client — see
§5.4.

### 2.2 Ten rows at `no_conclusion`, narrated nowhere

| Conclusion | Rows |
|---|---:|
| `ineffective` | 14 |
| `no_conclusion` | **10** |
| `partially_effective` | 3 |
| `effective` | 1 |

**S2's third leg is still open.** A row that was never tested and a row that was tested and found
clean are both an absence in the report, which has no coverage section at all. The one
`evidence_ceiling` that did fire ("No executed test names the subject of 1 control requirement:
approval_before_commitment") sits in `generation_warnings` and reaches no reader.

**Fix S2d.** The report needs a coverage table: every RCM row, its conclusion, and for
`no_conclusion` rows the reason — *not tested*, *tested inconclusively*, or *evidence withheld* —
sourced from `evidence_ceiling` and the stage warnings that already exist.

### 2.3 Row proliferation without deduplication

28 rows include two splitting rows (**RCM-18B639 `ineffective` and RCM-DA9CE3 `effective`** — the
same risk, opposite conclusions), three PO/receipt-matching rows (RCM-24B2C0, RCM-1843A6,
RCM-F12F10) and four vendor-master rows (RCM-E7C293, RCM-98D457, RCM-642BAF, RCM-ECEA3F).

Coverage is genuinely broader than at rev 471 and this is not a reason to shrink it. But the same
risk settling at contradictory conclusions is a reporting hazard, and it is detectable: two rows
whose risk tokens overlap above a threshold and whose conclusions disagree should raise a
proposal-level warning.

**Fix R6.** Add a same-subject conclusion-consistency check to the execution rollup, on the same
token signal `_evidence_ceiling` already uses.

---

## 3. Data tests

27 tests, 34 steps. The substantive cross-table testing that was missing at rev 471 now exists:
vendor bank duplicates, vendor maker-checker, GRN completeness and chronology, payment
prerequisites, requisition aggregation. Anchoring works where it matters — the invoice-side tests
start from `invoice_data`, which is how INV2024144 became visible.

### 3.1 A 4-row lookup table is the anchor for a quarter of the test set

Eight of 34 steps are null/validity checks on `financial_approval_matrix`, spread across seven
different RCM rows:

| Step's owning row | Row's actual subject | Step |
|---|---|---|
| RCM-D45365 | Approval within delegated limits | `DAT-5E33BFC751` "Check financial approval matrix limits" |
| RCM-24EAD3 | Segregation of duties | `DAT-69E2177EFE` ×2 |
| RCM-1843A6 | Payment before receiving | `DAT-7BF1D457CD` "Approval matrix records with invalid limits" |
| RCM-98D457 | Vendor due diligence | `DAT-88B87820AB` |
| RCM-18B639 | Approval aggregation & splitting | `DAT-ADB608D955` |
| RCM-86A9CE | Requisition SoD | `DAT-B4BCC27AAE` |
| RCM-76FB81 | Exception management | `DAT-6AFC399015` step 2 |

Every one returns 0 exceptions on 4 rows and contributes an `effective` test conclusion to a row
about transactions. `DAT-5E33BFC751`, titled *"Financial approval authority is within delegated
limits"*, tests the matrix rather than the requisitions — the genuine limit breach (REQ2024081) is
caught elsewhere, by `DAT-4F31FC9D8B` under the PO-amendment row.

§8.2's lookup-frame pruning (`capabilities/analysis.py`, `definable_targets()`,
`MIN_ANALYSABLE_FRAME_ROWS`, `LOOKUP_SCALE_RATIO`) was applied to the **analysis** stage only. The
test stage has no equivalent guard, so the behaviour the pruning removed from EDA reappeared one
stage later and at four times the volume.

**Fix T6.** Port the lookup-frame predicate into test generation. A step whose base frame is a
lookup — small in itself and dwarfed by what it sits beside — may not be the anchor for a row
whose risk concerns transactions. The reference table remains available as a *join target*; what
is barred is anchoring on it. Reuse `definable_targets()` rather than re-deriving the rule, so the
two stages cannot disagree about what a lookup is.

This one change also removes the mechanism behind N1 (§2.1).

### 3.2 Three missing predicates, each owning a confirmed answer-key item

| Predicate | Owns | Status |
|---|---|---|
| `INVOICE_DATE < PO_DATE` | A16, 4 paid invoices, 35.2M | **Regression** — present at rev 471 |
| `INVOICE_AMOUNT > PO_TOTAL_AMOUNT` | A06, incl. 80M against an 8M PO | Never tested; named in the APM as the headline analytics result |
| Duplicate `VENDOR_INVOICE_NUMBER` keyed **without** `VENDOR_ID` | A07, A08, 25.2M | The current key reproduces the known false clear |

The third deserves emphasis. `A-272B7E11` at rev 471 returned *"No duplicate keys found"*,
`verdict: ok`, on `(VENDOR_ID, VENDOR_INVOICE_NUMBER)`. At rev 609 it returns exactly the same
clean pass, and `DAT-EBEDB382B8` was generated against the same key. The first pass called an
analysis that affirmatively clears a risk it cannot see worse than one that never ran; that is
now true of a data test as well.

**Fix T7.** Where a requirement names duplication across a population, the generated key must not
include a column whose whole purpose is to distinguish the parties being compared. This is
narrower than a general rule and can be stated as a step-contract line in the test-generation
prompt: *a duplicate-detection key over a supplier reference may not be qualified by the supplier
identifier.*

### 3.3 `review_required` is still terminal — T4 unimplemented

| Test | Exceptions | `semantic_valid` | `next_action` | Owning row |
|---|---:|---|---|---|
| `DAT-788C5224E4` Vendor master approval authorization | 38 | false | `""` | RCM-E7C293 |
| `DAT-CF2A90215C` Segregation of verification and approval | 2 | false | `""` | RCM-24EAD3 |

40 exceptions sit in `review_required` with nothing driving them forward. `rcm_execution.py:541`
sets the status; nothing escalates it. **T4 stands as written in the first pass.** Its cost here
is concrete: A24 (V1016 approved the same day it was added) is inside `DAT-788C5224E4`'s 38 rows,
and the over-broad predicate that made the result invalid is also what buried it.

### 3.4 Populations never tested

Invoice-side approval authority (A11/A12, 2,855M), invoices against Rejected requisitions
(A03/A04, 202M), unit-price variance (A27), `BUYER_ID` reconciliation (A30), requisition
department vs HR master (A28). These are coverage gaps rather than mechanism failures, and they
are what **S1** and **S6** exist to surface.

---

## 4. Document tests and cycle vouching

30 doc tests, all `kind: qa`. **Zero cycle tests, from 10 `transaction_cycle` attributes.**

The RCM half of T2 is fixed and the bypass is no longer silent — the run record names all ten
unvouched attributes. What remains has a precise, mechanical cause, traced below. It is not a
design gap.

### 4.1 Root cause: the extracted vouchers are pinned to a superseded pack

```
installed registry pack   procure_to_pay v7
extracted voucher records procure_to_pay v6   (Documents/.analysis/*/generated/DA-*.json)
```

`validate_evidence_reduction` raises `CycleSchemaError: Registry reference for 'procure_to_pay' is
stale or inconsistent`. `load_analysis` catches it and sets `fragment_override_state: "stale"`
(`document_analysis.py:766`). `registry_evidence_records` then excludes **all five voucher
documents** as `stale_fragment_overrides` (`document_analysis.py:403`). The manifest group holds
0 records, so every comparison recipe is unanswerable and no cycle test can be generated for any
row.

The extraction itself is sound. A complete registry-backed `procure_to_pay` package is on disk:

| Document | Record kind |
|---|---|
| `req2024009_purchase_requisition` | `procure_to_pay.purchase_requisition` |
| `po2024004_purchase_order` | `procure_to_pay.purchase_order` |
| `grn2024004_signed_receipt` | `procure_to_pay.goods_receipt` |
| `vinv001_202404_invoice` | `procure_to_pay.vendor_invoice` |
| `inv2024004_signed_payment_voucher` | `procure_to_pay.payment_voucher` |

**Fix T8.** Re-run document analysis so the records are re-extracted against v7. This is one
operator action and it unblocks the whole subsystem — the registry packs, the evaluator and the
grid have never executed on this engagement.

### 4.2 The warning that fired was the wrong one

`unanswerable_cycle_requirements` (`cycle_vouching.py:2337`) has a *starved* branch for exactly
this case, whose text is the actionable one:

> no transaction evidence is available for pack 'procure_to_pay' — all 5 analysed voucher
> documents are excluded … **Re-run document analysis to bring the extracted records back into
> scope.**

The generic per-attribute text fired instead, ten times, saying the records answer none of the
recipes — which describes the symptom and points at the RCM rather than at the document stage.

**Fix T9.** Make the starved branch reachable at generation time, and rename the exclusion reason.
`stale_fragment_overrides` misdescribes what happened: there are no auditor overrides on any of
the nine documents (`fragment_overrides: []` throughout). The cause is a pack version bump, and
the label should say so — an auditor reading the current label has no reason to suspect the
registry.

### 4.3 T5 — signature and OCR defects, unchanged

Two independent failures, both still present:

- **Key corruption.** The GRN's PO number extracts as `P02024004` (letter O for zero) and
  normalizes to `p02024004`. Every other record in the package normalizes to `po2024004`. Even
  once T8 lands, the goods-receipt leg of the three-way match will not join.
- **Signatures still lost.** The GRN's approval block captures `role: Vendor representative`,
  `name: OfficeSupply Co.` The handwritten *"Received and inspected by Ethan Smith (1041)"* — the
  requisitioner signing for his own receipt, A31 — is absent, as is the payment voucher's undated
  signature block (A32).

**T5 stands as written**, with the OCR normalisation added: a normalizer for identifier kinds
should reconcile visually confusable characters against the registered identifier pattern
(`PO` followed by digits), rather than emitting a key that can never match.

---

## 5. Findings and report

23 findings, 23 in the report. Reconciliation is fixed and the report is materially better —
INV2024144 leads the Key Findings table at critical, and the SoD exceptions that were reported as
tooling noise at rev 471 are now a real High finding (F-FFCB58, 12 exceptions).

### 5.1 F1 / S4 — the pipeline still reports on itself

Both `semantic_valid: false` results produced client-facing findings.

| Finding | Severity | In report | Source |
|---|---|---|---|
| F-5D1CF5 *"Invoice verification and approval independence was not reliably established"* | **high** | §B.10 | `DAT-CF2A90215C` |
| F-383722 *"Vendor master approval timing exception result requires validation"* | info | §B.23 | `DAT-788C5224E4` |

F-5D1CF5's narrative reads: *"the two checks identified the same underlying record and shared the
same deciding condition, so the result double-counted one outcome … The result was therefore not
semantically valid."* The underlying exception is real and reproducible — **INV2024063, 98.06M,
verifier 1020 = supervisor approver 1020, Paid**. The pipeline found a genuine control failure and
told the auditee its own test was unsound.

The demotion of F-383722 to `info` is progress on presentation, not on routing. **F1 and S4 stand
as written.** The `semantic_valid` / `semantic_issues` signal is present on every result and is
still the natural router.

### 5.2 F2 — deduplication not implemented, and duplication is worse

INV2024144 now generates three findings:

| Finding | Severity | Condition |
|---|---|---|
| F-59CFF3 | critical | Paid invoice without a linked purchase obligation |
| F-68B575 | **high** | Incomplete vendor identification in the invoice population |
| F-ED4918 | **medium** | Invoice record missing vendor identification |

The last two are the same condition on the same record at two severities. F2's comparison —
`evidence_refs[].source_id` plus the exception row key — would catch this; the first pass's
example (two Highs on INV2024008) is now correctly a single Medium, so the machinery may be
partly there but is not catching same-record different-test duplicates.

### 5.3 F6 — root cause is not running

**23 of 23 findings carry `cause_pending: true`.** Unchanged from 13 of 13. The report's
substitute text is honest and correctly implemented, but a step that never produces an output for
any finding in two consecutive regenerations is not a step.

### 5.4 Presentation defects that reach the client

- **Finding §B.4 prints `999,999,999,999` as the CEO's approval limit, four times**, in a table
  headed "Maximum approval amount". The sentinel from §2.1 is rendered to the auditee as a fact.
- `100000000` (§B.11) and `100000000.0` (§B.22) — unformatted, one a raw float — in tables where
  every other amount is formatted.
- *"A further 1 finding is recorded at informational severity and are not counted above."*
- The Key Findings table is grouped by process but numbered by finding order, so the `#` column
  reads 1, 5, 11, 3, 14, 2, 8, 12, 15, 10, 7, 13, 16, 6, 4, 17, 9.

### 5.5 F4 — exposure quantified per finding, never totalled

The Key Findings table now carries values ("1 case totalling 100 million", "5 cases totalling
257.3 million", "1 commitment totalling 99.3 million"). The Summary of Findings remains severity
counts only. **The second half of F4 stands** — a report covering PKR 2.5bn of payments should
total what is at risk.

### 5.6 F5 — the severity basis is still undifferentiated

An `info` tier exists and is excluded from the counts, which is the mechanism F5 needs. It is used
for one finding. Design-gap findings — *"Vendor duplicate detection and bank-detail validation
mechanism not documented"*, *"Procurement policy applicability … not evidenced"* — are still rated
**High** alongside findings evidencing paid exceptions. **F5 stands**: the tier needs to be driven
by whether the finding evidences a transaction or a documentation absence.

---

## 6. Structural fixes still open

### S1. Give exploratory analysis a path into the RCM — **not implemented**

The EDA now reaches the **APM** (`presets.py`, `analysis_summary` source, `allow_analysis_summary`
on the `planning.apm` preset). The `planning.rcm` preset carries `current_apm` but **no analysis
source and no coverage gate**. Option 1 as specified in the first pass was not built.

The narrative path is doing real work — shared bank accounts, V1010 and the invoice-over-PO
variance all reach the APM, and the first two reached findings. But the standing proof that the
gate is still needed is **A06**: the APM names `INV2025008` (80M against an 8M PO) as its most
significant analytics result, and no test, finding or report line ever picks it up. A memo is not
a coverage assertion.

**S1 stands as written**, with its priority raised: option 1 needs no rows, no new analytics, and
the evidence is already computed.

Related and still true: **all 28 analyses are marked `informative: true`**, 11 of them with zero
exceptions. The informativeness gate still never fires, so nothing downstream can distinguish
`A-A86687CB` (shared bank accounts) from a sign scan on four rows. See §8.3.

### S2. A row that cannot be tested must not look like a row that passed — **partially implemented**

`_evidence_ceiling` is built and correct. Three gaps remain, specified above as **S2b** (no
`effective` for a row asserting no control), **S2c** (sentinel thresholds), and **S2d** (a
coverage section distinguishing *not tested* from *tested, no exceptions*).

### S4. The pipeline reports on itself to the client — **not implemented**

See §5.1. Unchanged from the first pass.

### S5. Anchor and completeness invariants — **half implemented**

Anchoring works: steps declare a population and the invoice tests use it. That recovered
INV2024144.

The **reconciliation half was not built.** No count of unreached rows per population is produced,
which is what would have surfaced the VINSUSP invoices (A05, 157M) and the invoices against
rejected requisitions (A03/A04, 202M) without anyone having to anticipate them. Both remain in the
**No** band.

**S5b.** Every row of each imported population must be reachable by at least one executed test,
and the count of unreached rows reported per population.

### S6. Regression harness — **not implemented**

Appendix A now has two measured points (9% at rev 471, 33% at rev 609) and one confirmed
regression (A16, caught then lost). That regression is the argument: it went unnoticed through a
full regeneration because nothing asserts recall. Wire the register as an acceptance fixture and
fail the build on a drop.

---

## 7. Sequencing

| Order | Fix | Why here |
|---|---|---|
| 1 | **T8** — re-run document analysis against pack v7 | One operator action; unblocks cycle vouching entirely, which has never executed |
| 2 | **T6** — bar lookup-table anchoring in test generation | Removes 8 filler steps and the mechanism behind the RCM-DA9CE3 `effective` conclusion |
| 3 | **S2b + S2c** — no `effective` on a "no control" row; sentinel thresholds | Stops the highest-consequence output in the workspace reaching a client |
| 4 | **F1 / S4** — QA channel split | Recovers a real 98.06M SoD finding currently reported as tooling noise |
| 5 | **T7 + the two missing predicates** (`INVOICE_DATE < PO_DATE`, `INVOICE_AMOUNT > PO_TOTAL_AMOUNT`) | Owns A06, A07, A08, A16; A16 is a regression and A06 is already named in the APM |
| 6 | **S1** option 1 — analysis coverage reconciliation into the RCM | Largest remaining recall win, no new analytics, no row disclosure |
| 7 | **T4** — `review_required` non-terminal | 40 exceptions currently parked, including A24 |
| 8 | **S5b** — per-population reachability count | Surfaces A03, A04, A05 without anticipating them |
| 9 | **T9 + T5** — correct bypass diagnosis; OCR and signature handling | Prevents the next silent regression; owns A31, A32 |
| 10 | **F2, F5, F6, S2d, R6** and §5.4 presentation | Report quality; none block recall |
| 11 | **S6** — regression harness over Appendix A | Makes every fix above measurable and would have caught A16 |

---

## 8. EDA performance — resolved, retained for the record

The first pass measured the EDA at 157.5s wall clock, 97.4% model wait, 18 serialized calls, and
priced the P1 decision (EDA before APM) at roughly two minutes per engagement. **P1 was
implemented and the ordering is enforced** — the EDA now runs before the APM. The two-minute cost
was accepted and §1 vindicates it: the analytics section is doing real work in the memo.

Measurements are retained below because they still bound what any future optimisation can achieve.

### 8.1 The binding constraint

Every run is installed with `max_llm_concurrency: 1` (`agent/routing.py:1028`), a deliberate
choice to stay inside the provider's rate limits. The parallel barrier
(`agent/workflow.py:30`) and the runner's fan-out (`workflow_runner.py:425`) are built and
correct, but raising concurrency is a provider-capacity decision, not a code one. Observed
LLM-seconds ÷ wall-seconds was **1.00 on every stage of the engagement**.

### 8.2 What was implemented, measured on recorded prompts

- **A. Reference tables no longer claim a definition turn.** `capabilities/analysis.py`,
  `definable_targets()`, used by both the readiness check and unit expansion. A frame is a lookup
  when it is under `MIN_ANALYSABLE_FRAME_ROWS` (5) *and* at least `LOOKUP_SCALE_RATIO` (10×)
  smaller than the largest frame in scope. Measured: 2 of 20 frames pruned. The three-way
  `requisitions_financial_approval_matrix_staff_details_joined_joined` (112 rows) survives, which
  matters — it carries the approval-limit breach. **This is the predicate T6 needs to port into
  test generation.**
- **B. The transport envelope no longer bills as audit material.** `workers/analysis.py`,
  `_model_facing_context()`. Measured: 97,344 characters removed, 14.1% of context, ~24,300
  prompt tokens.
- **C. The analytics catalog moved ahead of the frame description.** Shared prefix across
  definition calls 1,160 → 3,021 tokens (2.6×); total prompt characters −14.1%.

Net: roughly 10–17% off a ~157s stage. Only removing round trips changes that materially, and
one turn per frame is the design.

### 8.3 Not implemented — the informativeness gate (E6)

Attempted and reverted at rev 471; **still unimplemented at rev 609, where all 28 analyses remain
`informative: true` with 11 returning zero exceptions.**

The gate fires only on *saturation*. Extending it to *vacuity* — a duplicates test on a key the
profile already shows is unique — conflicts with
`test_an_uninformative_proposal_is_run_and_dropped_before_it_is_saved`, which asserts that exactly
such a check is the *good* proposal that must survive; `informative: false` **drops** the
procedure.

Making this work needs a distinction the contract does not carry: *established nothing* (drop)
versus *redundant with a cheaper signal* (keep, deprioritise). That is the same field **S1** needs
for coverage reconciliation, which is now the reason to do it — the two should be designed
together rather than E6 being smuggled in as an optimisation.

---

## Appendix A — independent issue register

Every issue found by working the six Excel files and seven documents directly, before any pipeline
artifact was opened. This is the answer key for S6.

`Captured` is against the **audit output** — an issue reported only by a saved analysis or only in
the APM is *not* captured, because nothing carried it into a test, a finding or the report.

| ID | Issue | Value (PKR) | Rev 471 | Rev 609 | Captured where at rev 609 |
|---|---|---:|---|---|---|
| A01 | 20 invoices reference a requisition id or nothing in `PO_NUMBER_LINK` — no PO exists | 1,054.4M (563.6M paid) | Partial | Partial | F-DD71D6 (8 requisitions, requisition-side) + F-59CFF3 (1 invoice); the 20-invoice population unstated |
| A02 | **INV2024144 paid with no vendor, no PO, no GRN** | 100.0M | No | **Yes** | `DAT-EBEDB382B8` → F-59CFF3 critical → report §B.1 |
| A03 | 4 invoices raised against **Rejected** requisitions | 158.0M | No | No | — |
| A04 | INV2025009 re-raises rejected REQ2024047 for the identical amount | 44.1M | No | No | — |
| A05 | Six invoices numbered `VINSUSP001`–`VINSUSP006`, each re-billing a closed prior-year PO | 157.1M | No | No | — |
| A06 | Invoice amount exceeds PO total (incl. 80M against an 8M PO) | 79.8M variance | EDA only | **Planning only** | `A-379F8C80` → APM "Data analytics performed"; no test references `PO_TOTAL_AMOUNT` |
| A07 | Duplicate vendor invoice number across **different** vendors, both paid (`VINV011-202404`) | 16.2M | No | No | `A-272B7E11` and `DAT-EBEDB382B8` both key on `(VENDOR_ID, VENDOR_INVOICE_NUMBER)` — false clear |
| A08 | Second duplicate number pair `VINV006-202412` | 9.0M | No | No | as A07 |
| A09 | Same vendor + identical amount, both paid (V1002 × 30.0M) | 30.0M | No | No | dup key includes `INVOICE_DATE` and `PO_NUMBER_LINK` |
| A10 | REQ2024081 approved by CFO against a 10.0M limit | 99.3M | Yes | **Yes** | `DAT-4F31FC9D8B` → F-0587D2 → report §B.4 |
| A11 | INV2024079 approved by Financial Controller against a 5.0M limit | 27.6M | No | No | invoice-side authority still never tested |
| A12 | 110 of 118 invoices approved by roles absent from the approval matrix | 2,855.6M | No | No | — |
| A13 | Segregation of duties — requisitions (requester = verifier / = fin approver) | 504.4M | Downgraded | **Yes** | `DAT-B74C47AB5B` → F-FFCB58 high, 12 exceptions → report §B.17 |
| A14 | INV2024063 verifier = supervisor approver, paid | 98.1M | Downgraded | Downgraded | `DAT-CF2A90215C` → F-5D1CF5, record named but framed as *"not reliably established"* |
| A15 | Cross-document SoD — invoice approver is also the requisition approver | 166.0M | No | No | — |
| A16 | Invoice dated before its PO (4 invoices, all paid) | 35.2M | **Yes** | **No — regression** | no test compares `INVOICE_DATE` to `PO_DATE` |
| A17 | Invoice dated before GRN (5 invoices) | 60.2M | Partial | **Yes** | `DAT-CA8CF3ABF5` → F-8EC023 → report §B.13 |
| A18 | Payment made before goods receipt (3 invoices) | 50.2M | Partial | **Yes** | `DAT-90BF73A10A` → F-082E47, 9 cases → report §B.5 |
| A19 | INV2024008 paid before its own invoice date and before approval | 24.9M | Yes (duplicated) | **Yes** | `DAT-39196F79F4` → F-237D46, single finding |
| A20 | 22 invoices with no GRN link, incl. INV2024017 (12.0M paid) | 12.0M+ | No | **Yes** | `DAT-F1EFE8117C` → F-338785 high → report §B.7 |
| A21 | Two vendor pairs share a bank account (V1008+V1009, V1007+V1018) | — | EDA only | **Yes** | `DAT-2F7D27EA94` → F-3726EA high → report §B.8 |
| A22 | 5 vendors created and approved by the same person | — | No | **Yes** | `DAT-DF3ED5F0C3` → F-5B9992 critical → report §B.2 |
| A23 | V1010 "Under Review" paid | 36.4M | EDA only | **Yes** | `DAT-6AFC399015` → F-90E674 exception table → report §B.14 |
| A24 | V1016 approved the same day it was added | — | No | No | inside `DAT-788C5224E4`'s 38 rows; test ended `review_required`, result invalid |
| A25 | No vendor approved by anyone outside Procurement | — | No | No | — |
| A26 | 35 same-vendor requisition pairs within 30 days | — | No | **Yes** | `DAT-58E84092FA` → F-386A6F high, 43 groups → report §B.9 |
| A27 | Same item at wildly different unit prices | — | No | No | RCM-BE8381 exists (medium) but carries a document test only |
| A28 | 94 of 112 requisitions state a department contradicting the HR master | — | No | No | — |
| A29 | 3 staff pairs share a bank account | — | No | Planning only | APM analytics section; no test |
| A30 | `BUYER_ID` B001–B006 reconcile to no staff record | — | No | No | no test references `BUYER_ID` |
| A31 | GRN2024004 signed "Received and inspected by" by the requisitioner | 2.0M | No | No | approval block extracts as "Vendor representative"; name still lost |
| A32 | Payment voucher signatures carry no date | 2.0M | No | No | — |
| A33 | SOP §3.2 mandates RFQ/comparative bids; no bid or quotation field exists in any table | 2,868M | Partial | Partial | scope-limitation bullet + F-35A263 at document level; population-wide absence unstated |

### Tally

| Outcome | Rev 471 | Rev 609 |
|---|---:|---:|
| **Yes** — reached a finding and the report | 3 (9%) | **11 (33%)** |
| **Partial** — surfaced but mis-framed or unvalued | 4 | 3 |
| **Downgraded** — real exceptions reported as tool unreliability | 2 | 1 |
| **Planning / EDA only** — computed, never carried forward | 3 | 2 |
| **No** — absent from every artifact | 21 | 16 |
| | **33** | **33** |

Of the four highest-value single items, A02 (100.0M) is now captured; A03 (158.0M), A05 (157.1M)
and A12 (2,855.6M) remain in the **No** band.

### Caveats on the register

- **A12 is a judgement call, not a certainty.** The approval matrix governs requisition financial
  approval; whether it also governs invoice supervisor approval is not stated in the SOP. The
  point stands regardless: the pipeline never raised the question.
- **A26 flags candidates, not confirmed splits.** The 43 groups F-386A6F reports are a screening
  result requiring auditor judgement on divisibility. That a screen now runs at all is the change.
- **A28–A30 are data-integrity observations** rather than control failures on their own; they
  undermine reliance on the requester and buyer fields used by other tests.
- Values are the gross transaction amounts touched by each issue, not quantified loss, and they
  overlap across rows — do not sum the column.
