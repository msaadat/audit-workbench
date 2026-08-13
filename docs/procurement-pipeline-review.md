# End-to-end pipeline review — `procurement` workspace

Assessment only. Ground truth was derived independently from the six Excel files and seven
documents before any pipeline artifact was opened, then compared stage by stage.

**Workspace:** `Workspaces/procurement`, revision 471
**Population:** 112 requisitions · 93 POs · 118 invoices · 39 vendors · 52 staff
**Value:** invoices PKR 3,103,467,230 (paid 2,510,413,690) · POs 1,934,810,970

---

## Verdict

The pipeline produces artifacts that *look* like a competent audit and are structurally sound —
the APM reads like a real planning memo, the RCM is coherent, the report is well-formed prose.
But on the thing that actually matters, **it misses most of what is in the data**.

Working the source material independently produced **33 issues** (Appendix A). Of those the
pipeline reported **3 intact, 4 partially, downgraded 2 to "unreliable result", left 3 in the
EDA layer without carrying them forward, and missed 21 entirely.** Recall to the report is 9%.

The single largest item in the population — a PKR 100,000,000 invoice that was **paid with no
vendor, no PO and no GRN** — appears in no test, no observation, no finding, and no report.
The string `INV2024144` does not occur anywhere in `Planning/`, `DataTests/`, `DocTests/`,
`Findings/`, `Observations/` or `Reports/`.

The root cause is not model quality. It is a small number of **structural breaks** — analysed in
§6 and with fix plans in §7 — the most consequential being that one field on an RCM row
(`evidence_kind: inquiry`) makes a data test *unreachable* for the very rows where no control
exists, and that the EDA layer *did* find several of the misses with nothing to carry them forward.

---

## 1. Recall scorecard

| # | Issue present in the source | Exposure | Pipeline outcome |
|---|---|---|---|
| G1 | 20 invoices paid/pending against a **requisition id or nothing** in `PO_NUMBER_LINK` — no PO exists | 1,054M (563.6M already paid) | **Partial** — surfaced inside F-F1C01E as "19 records without ... linkage", from the requisition side, unvalued, framed as a data-linkage defect |
| G2 | **INV2024144: 100M PAID, `VENDOR_ID` null, PO null, GRN null**, vendor invoice no. `VINV011-202313` (month 13), paid 8 days past due | 100M | **Missed entirely** |
| G3 | 4 invoices raised against **Rejected** requisitions; INV2025009 re-raises rejected REQ2024047 for the identical 44,102,320 | 202M | **Missed** |
| G4 | Six invoices numbered **`VINSUSP001`–`VINSUSP006`** (INV2025004-09), each re-billing a closed prior-year PO | 157M | **Missed** |
| G5 | Invoice exceeds PO total — incl. **80M billed against an 8M PO** (10×) | 79.75M variance | **Missed** by the test layer; *found by saved analysis A-D6F9512D and never promoted* |
| G6 | Duplicate vendor invoice numbers across different vendors — `VINV011-202404` used by V1032 and V1018, **both paid at 16,193,000** | 16.2M | **Missed** as a data finding; only F-680331 "controls not documented" |
| G7 | Same vendor + identical amount (V1002 30M twice, both paid) | 30M+ | **Missed** |
| G8 | REQ2024081 99,348,150 approved by CFO (limit 10M) | 99.3M | **Caught** — F-13ACA8 |
| G8b | Invoice-side authority never tested: INV2024079 27.6M approved by Financial Controller (limit 5M); 110 of 118 invoices approved by roles absent from the matrix | — | **Missed** |
| G9 | Segregation of duties — 10 requisitions + INV2024063 (98.06M, verifier = approver) | 98M+ | **Found, then downgraded** to two "exception result is not reliable" findings |
| G10 | Chronology: invoice before PO (4, all paid), payment before GRN (3), INV2024008 paid before its own invoice date *and* before approval | 50M+ | **Caught** — F-13D9B6, F-F1C01E, F-3FCA6C, F-D92220 |
| G11 | 22 invoices with **no GRN link**, incl. INV2024017 (12M paid) | 12M+ | **Missed** |
| G12 | Vendor master: V1008+V1009 and V1007+V1018 **share bank accounts**; 5 vendors created and approved by the same person; **V1010 "Under Review" paid 36.4M**; no vendor approved by anyone outside Procurement | 36.4M+ | **Missed** — and RCM-4301AC concluded **"effective"**. *Found by analyses A-A86687CB, A-D2310860, A-9A87B89F* |
| G13 | Split purchases — 35 same-vendor requisition pairs inside 30 days; REQ2024060 + REQ2024065 one day apart, 128.8M combined | — | **Missed** — RCM-99C6E0 exists but carries only an inquiry doc test |
| G14 | Same item, wildly different unit price: Cybersecurity Training Platform +194%, Cloud Migration +167% | — | **Missed** |
| G15 | 94 of 112 requisitions state a department contradicting the HR master; 3 staff pairs share a bank account; `BUYER_ID` B001-B006 reconcile to no staff record | — | **Missed** |
| G16 | Sample voucher package: GRN2024004 signed "Received and inspected by" by **Ethan Smith (1041) — the requisitioner**; invoice dated 10 days before the PO | 2M | **Missed** |
| G17 | SOP §3.2 mandates RFQ/comparative bid evaluation — **no bid or quotation field exists in any table**, so competitive sourcing is unevidenced for the entire 2.87bn | 2,868M | **Partial** — F-6E104E raises the *policy* gap, never the population-wide absence of evidence |

---

## 2. APM — good, with two real gaps

**Works well.** Genuinely well-written planning prose. Risk themes map onto the real risk
surface: approval authority, requisition-to-PO compliance, sourcing/due diligence, GRN and
invoice processing, vendor master and bank-account integrity, exception management. It is
correctly disciplined about not asserting exceptions from profiling alone, and the
`_[not available]_` placeholders are honest rather than hallucinated.

**Gaps:**

1. **No fraud risk assessment.** For a procurement audit this is a standard section and its
   absence propagates: no fraud lens in the RCM means no split-purchase testing, no duplicate-payment
   testing, no vendor-employee collusion testing.
2. **No split-purchase / threshold-circumvention risk** despite the approval matrix being the
   central control document.
3. **No value-for-money / price-reasonableness risk.**
4. **"No exploratory data analysis summary was supplied."** The 29 saved analyses ran ~40 minutes
   *after* the APM and were never fed back. The APM also declares the audit period "not available"
   while the profiles it cites make the range plain (2023-01-10 → 2025-07-30).

### Fix plan — planning / APM

**P1. Order the stages, or make the APM re-runnable.** The APM is generated before the EDA and
never revisited, so the planning basis is permanently poorer than the evidence on hand. Either
gate APM generation on the analysis workflow having completed, or add an explicit
`refresh_apm` step after analysis that re-runs the memo with `analysis_memo` flattened into the
planning context. The adapter already exists (`backend/app/analysis_memo.py`,
`flatten_embeds()`); it is the sequencing that is missing.

Enforcing the ordering means paying the EDA's latency on the critical path of every engagement,
which is what has made it unattractive. **§8 measures that cost, implements what can be recovered
without raising provider concurrency, and prices the decision honestly at roughly two minutes per
engagement.**

**P2. Derive the audit period from the populations.** When the planning context supplies no
period, state the observed min/max of the date columns as a *proposed* scope with a clear
"to be confirmed" qualifier, rather than emitting `_[audit period — not available]_`. The memo
already cites row counts from the same profiles; it should cite ranges from them too.

**P3. Add a fraud risk section to the APM template.** This is a template change, not a code
change — `templates_store.py` derives sections from the `##` headings, so adding a
**Fraud risk and management override** heading to the workspace `apm` template is sufficient
to make the worker populate it. The absence of this section is the upstream cause of the
missing splitting, duplicate-payment and conflict-of-interest coverage in the RCM.

**P4. Add a value-for-money / price-reasonableness risk** to the same template, which owns G14.

## 3. RCM — best artifact in the chain; one structural hole

19 rows with sensible risk statements and honest control descriptions ("No control identified
for..." is the right phrasing where nothing exists). Coverage of *themes* is good — splitting,
duplicate invoices, vendor/bank integrity and SoD all have rows.

**The hole: there is no goods-receipt / three-way-match row.** The APM flagged it as a risk;
the RCM never converts it into a control. Consequently the 22 invoices with no GRN link, the
payments made before receipt, and the invoice-vs-PO amount variances have no owning control.

**Second issue — rows exist but are unarmed.** The highest-risk rows carry only inquiry-style
document tests and no data test at all:

| RCM row | Rating | Tests | Consequence |
|---|---|---|---|
| RCM-6E29FF Vendor & bank-account payment integrity | **critical** | 2 doc tests, 0 data tests | shared bank accounts, V1010 payment never tested → `no_conclusion` |
| RCM-459752 Duplicate invoice prevention | high | 1 doc test, 0 data tests | no duplicate-payment testing ran at all |
| RCM-99C6E0 Approval aggregation & splitting | high | 1 doc test, 0 data tests | 35 candidate split pairs never examined |
| RCM-6F8420 PO completeness & accuracy | high | 1 doc test, 0 data tests | invoice-over-PO variance never tested |

**Third — a false negative.** RCM-4301AC "Vendor master maintenance" concluded **effective**
on the strength of two tests that check only that Vendor IDs are unique and status values are
spelled correctly. Duplicate bank accounts and self-approved vendor records sit untested
underneath an "effective" conclusion. That is the most dangerous single output in the workspace.

### The mechanism behind the unarmed rows

This is not a model judgement call — it is a deterministic consequence of one field.

When the RCM worker finds no asserted control it writes `"No control identified for …"`
(`backend/app/agent/workers/planning.py:259`) and then sets **`evidence_kind: inquiry`** on
every attribute of that row. Every one of the four unarmed rows above has *only* `inquiry`
attributes:

| Row | Attributes | evidence_kind |
|---|---|---|
| RCM-459752 Duplicate invoice prevention | 1 | `inquiry` |
| RCM-6E29FF Vendor & bank-account integrity (critical) | 2 | `inquiry`, `inquiry` |
| RCM-6F8420 PO completeness & accuracy | 1 | `inquiry` |
| RCM-99C6E0 Approval aggregation & splitting | 1 | `inquiry` |

`_relevant_table_schemas()` (`backend/app/agent/workers/tests.py:175`) then does this:

```python
evidence_kinds = {str(item.get("evidence_kind") or "") for item in attributes}
if attributes and "tabular_population" not in evidence_kinds:
    return []
```

With no table schemas in the prompt, `allowed_variants` never gains `"data"`
(`tests.py:281`), so **a data test cannot be generated for that row at all**. The only
reachable variant is `document_question`, which asks whether the documentation describes the
missing control. It does not — so the row yields a "policy not documented" finding and no
substantive testing ever happens.

The logical error is the equation of *"management asserts no control"* with *"the population
cannot be tested"*. Absence of a control is precisely when substantive testing matters most.
This one inference is the direct cause of four missed issue classes — G5, G6/G7, G12 and G13 —
covering the duplicate payments, the shared vendor bank accounts, the 80M-against-8M invoice,
and the 35 candidate split pairs.

RCM-4301AC is the same defect one step subtler: its `vendor_master_amendment` attribute — the
one whose requirement names *"changes to vendor and bank-account information"* — was classified
`manual_inspection`, so the duplicate-bank-account test was never generated, while its two
`tabular_population` attributes produced the two weak tests that carried the row to "effective".

### Fix plan — RCM

**R1. Decouple `evidence_kind` from control existence.** (`agent/workers/planning.py`, prompt
~line 277)
`evidence_kind` must describe where evidence for *the requirement* lives, not whether a control
exists. Add to the prompt, adjacent to the existing `evidence_kind` menu:

> A row whose control field says "No control identified" still requires evidence_kind to be
> chosen from the supplied material. Where the imported tables carry the fields the requirement
> names, that is `tabular_population` regardless of whether a control is asserted — testing the
> population is how the absence of the control is evidenced. Use `inquiry` only where no
> supplied table and no supplied document can answer the requirement.

**R2. Gate it in `validate_rcm_proposal`** (`planning.py:593`). Reject a proposal where a row
has no `tabular_population` attribute while the supplied table profiles carry columns matching
the requirement's tokens. Reuse the existing `_schema_relevance_tokens()` scorer so the check is
the same relevance signal the test worker already applies — if the test worker *would have*
ranked a table into the prompt, the RCM row may not claim `inquiry`. This is the same class of
gate as the phrasing/coverage checks in `docs/rcm-generation-quality.md` §7, and belongs beside
them.

**R3. Add the missing goods-receipt / three-way-match row to the required-coverage set.**
The APM raises it, the RCM never emits it, and it owns G5, G10 and G11. Coverage assertion:
for a procure-to-pay engagement the RCM must contain a row whose control concerns receipt of
goods evidenced before payment. Same enforcement point as R2.

**R4. Block `effective` on thin evidence.** `rcm_execution.py` must refuse a
`control_conclusion` of `effective` where every contributing test is an inquiry doc test, or
where the row's own risk text names a concept (`bank account`, `duplicate`, `split`) that no
executed test predicate references. Downgrade to `partially_effective` with an explicit
`scope_limitations` string instead. This alone would have stopped the RCM-4301AC false negative
reaching the report.

**R5. Fraud-lens coverage rows.** Splitting, duplicate payment and vendor-employee conflict
rows should be seeded from the methodology pack rather than left to the model to invent — the
model *did* invent the splitting row here and then failed to arm it, which is the worse of the
two failure modes because the RCM looks complete.

## 4. Tests

**Data tests (16).** The ones that exist are competently written — `DAT-2DF158A860` correctly
joins requisitions → staff → matrix and finds the one genuine limit breach. But the set is
skewed toward completeness/validity checks (`is_null`, `is_duplicated`, valid-value lists) and
away from cross-table substantive testing. Nothing compares `INVOICE_AMOUNT` to
`PO_TOTAL_AMOUNT`. Nothing looks for duplicate payments. Nothing tests the vendor master's bank
accounts. Nothing tests invoice-side approval authority.

Structural blind spot: the two lifecycle tests (`DAT-85368A37FA`, `DAT-BBEE43384F`) both start
from `requisitions`. Any invoice with no requisition is invisible to them by construction —
which is exactly how INV2024144 escapes.

Two tests finished in `review_required` with 11 and 13 exceptions and were never resolved,
leaving RCM-960A8F at `no_conclusion` and RCM-24309E without a usable conclusion.

**Document tests (27).** All 27 objectives are variations of *"Determine whether the supplied
documentation defines/establishes X"* — policy-design inquiries against the SOP. Not one is a
vouching test.

Five of the seven uploaded documents are a **complete voucher package**
(REQ2024009 → PO2024004 → GRN2024004 → vendor invoice → payment voucher) that exists precisely
to be tie-matched. `GRN2024004` is referenced by exactly **one** doc-test item out of 32. No test
asks whether the invoice date precedes the PO date, whether quantities agree across documents,
or who signed the GRN — so the requisitioner-signed goods receipt is missed.

`DT-8B7DAFE6` ("Identify missing exception approval evidence") ran over the requisition, PO,
invoice and voucher together and returned **no exception**, even though that package contains
an invoice dated 10 days before its PO.

**Extraction defect worth fixing.** Both scanned PDFs lose every handwritten name in extraction:
the GRN renders as `A~ K./tVl.lll t`, the voucher as `Signed Signed`. Any doc test reading
extracted text is structurally blind to signature evidence. The names are legible in the page
image (`Ethan Smith (1041)`, `Ahmed Khan`; `Olivia Smith`, `Max Baker`).

**Why no vouching test exists — this is the known Phase 2/3 gap.** All 27 doc tests carry
`kind: qa`; there are **zero** `cycle` tests. Across all 19 RCM rows, **not one control attribute
carries `evidence_kind: transaction_cycle`** (the distribution is `tabular_population` 19,
`manual_inspection` 14, `document_content` 9, `inquiry` 9, `mixed` 1). Since
`allowed_variants` only gains `"cycle_vouch"` when the manifest yields candidates for such an
attribute (`tests.py:281`), the entire cycle-vouching subsystem — `cycle_vouching.py`, the
registry packs, the evaluator, the grid — was **silently bypassed for this engagement**.

That is precisely the failure recorded in the status line of `docs/vouching-grid-plan.md`:
*"the regenerated RCM classified PO/GRN agreement as tabular-only so the planned documentary
Cycle vouch test was absent."* This review is independent confirmation of that gap, on the full
workspace rather than one row, and it should be treated as evidence for **Checkpoint C repeat**
rather than as a new workstream.

### Fix plan — tests

**T1. Fail loudly when a cycle-capable engagement generates zero cycle tests.**
The bypass is silent today: nothing in the run reports that a five-document voucher package was
analysed, matched to a registered `procure_to_pay` pack, and then never vouched. Add a
post-generation assertion in `agent/capabilities/tests.py` — where a transaction-evidence
manifest yields candidates for a pack, an engagement with zero `kind: cycle` tests raises a
`review_required` stage warning naming the pack and the unvouched record kinds. Silent
degradation to `qa` doc tests is the specific behaviour to eliminate.

**T2. Carry `transaction_cycle` classification through — tracked in the vouching-grid plan.**
The RCM-side cause (nothing classified as `transaction_cycle`) is Phase 2/3 robustness
remediation already specified in `docs/vouching-grid-plan.md` §3.1/§4.2, including typed
registry-backed `required_comparisons` and exact generated-assertion coverage. No new design is
needed here. What this review adds is that the failure is **total, not partial** — 0 of 52
attributes — so Checkpoint C repeat should assert a non-zero `transaction_cycle` count as an
acceptance criterion, not just inspect the tests that happen to be produced.

**T3. Anchor tests on the population being asserted about.** Both lifecycle tests
(`DAT-85368A37FA`, `DAT-BBEE43384F`) start from `requisitions`, so an invoice with no
requisition cannot appear in any result — the mechanism by which INV2024144 (100M, paid) is
invisible. Add a step-contract rule to the test-generation prompt
(`agent/workers/tests.py`): where a requirement concerns invoices or payments, the step's base
frame must be the invoice population, with joins outward; a step asserting about population A
may not be anchored on population B. This is the same concern as *"separate population from
exceptions in the step contract"* — `docs/test-generation-quality.md` §1 open recommendation —
and should be folded into it.

**T4. Make `review_required` non-terminal.** Two tests ended there holding 24 real exceptions
and nothing escalated. `rcm_execution.py:525` sets the status; nothing drives it forward. A
test in `review_required` carrying a non-zero exception count must either raise an auditor
action item or roll up as an unresolved exception — it must not let the owning row settle at
`no_conclusion` while genuine exceptions sit inside it.

**T5. OCR the signature blocks, or route signature attributes to image review.** An
`Authorization` attribute whose evidence is a handwritten signature cannot be tested from
extracted text. Either extend extraction to OCR signature regions, or have the validator refuse
a text-based assertion on a signature attribute and require the `visual` evidence path that
`agent/workers/documents.py:2125` already implements.

## 5. Findings and report

13 findings, 12 in the report. Composition:

- **5 real transaction findings** — F-13ACA8, F-13D9B6, F-3FCA6C, F-D92220, F-F1C01E
- **5 "the SOP does not say X" policy-design findings** — F-1329B4, F-680331, F-6E104E, F-86EEDB, F-C17AE7
- **3 findings about the pipeline's own broken tests** — F-91FBE5, F-AE7223, F-E8D783

That third category is a defect. F-AE7223 and F-E8D783 are titled *"Segregation-of-duties
exception result is not reliable"* and *"...was not reliable"*, and both are in the client-facing
report. The underlying exceptions are real (I reproduced all 11 independently) — the pipeline
found genuine SoD failures, decided its own test was unsound, and reported the tooling problem
to the auditee instead of the control failure. F-91FBE5 cites `DAT-EEAC2146AF`, which has a
result directory but **no test definition** — an orphan.

**Duplication:** F-3FCA6C and F-D92220 are both about INV2024008 and are reported as two separate
High findings.

**Reconciliation gap:** F-91FBE5 exists in `Findings/` but is absent from the report, with no
recorded exclusion rationale. 13 findings → 12 reported.

**Report quality is otherwise good.** Clean structure, honest scope-limitation section, correct
Condition/Criteria/Root Cause/Risk/Recommendation discipline, working finding links, sensible
handling of empty root causes ("Not established by the evidence obtained; pending auditor
follow-up" — all 13 findings carry `cause_pending: true`). Recommendations are specific and
actionable.

Two report-level weaknesses:

- **No quantified exposure.** Individual findings carry amounts but nothing is totalled. A report
  covering 2.5bn of payments should tell the reader what is at risk.
- The conclusion says *"exposure at the critical vendor and bank-account payment integrity risk"*
  — but that risk produced no finding because it was never data-tested. The report describes the
  gap as a coverage limitation rather than testing it.

### Fix plan — findings and report

**F1. Stop emitting test-quality defects as client-facing findings.** F-91FBE5, F-AE7223 and
F-E8D783 report the tool's own unreliability to the auditee. Where the finding-draft worker
concludes a result is not semantically reliable, the correct output is a **QA item against the
test**, not a finding against the client. Route on the existing signal — the result already
carries `semantic_valid` and `semantic_issues` (`DataTestResults/*/DTR-CURRENT.json`) — and have
the reporting worker refuse to draft a finding from a result whose `semantic_valid` is false,
raising the test back to `review_required` with the issue attached instead.

This matters beyond presentation: F-AE7223 and F-E8D783 concern SoD exceptions I reproduced
independently and confirmed as **real**. The pipeline found genuine control failures and
converted them into a statement about its own tooling. Fixing the routing recovers a real
high-severity finding rather than merely suppressing noise.

**F2. Deduplicate findings on shared subject.** F-3FCA6C and F-D92220 are both INV2024008 and
both High. Where two findings cite the same primary record and the same RCM row, merge them
into one finding with two conditions, or suppress the weaker. Compare on
`evidence_refs[].source_id` plus the exception row key.

**F3. Assert findings→report reconciliation.** 13 findings exist, 12 reach the report, with no
recorded rationale. `report.py:_ordered_findings()` is the choke point: any finding excluded from
the assembled report must be recorded in `generation_warnings` (currently `[]`) with a reason,
and the QA pass should fail on a silent drop.

**F4. Quantify exposure in the executive summary.** Individual findings carry amounts;
`_key_findings()` never totals them. Add a value column and a total to the Summary of Findings,
sourced from the exception frames the findings already reference. A report covering PKR 2.5bn of
payments should state what is at risk.

**F5. Distinguish "control absent" from "control tested and failed" in severity.**
Five of thirteen findings are "the SOP does not define X" — legitimate, but currently rated High
alongside findings evidencing actual paid exceptions. Give the design-gap findings a distinct
severity basis so a reader can tell a documentation gap from a payment that should not have been
made.

**F6. Fill root cause, or say why not.** All 13 findings carry `cause_pending: true` and an empty
Root Cause. The report substitutes *"Not established by the evidence obtained; pending auditor
follow-up"*, which is honest and correctly implemented — but 13 of 13 means the cause step is
effectively not running. Either drive it from the exception data (an approver ID recurring across
exceptions is a cause signal) or surface it as an explicit auditor to-do count rather than
per-finding boilerplate.

## 6. Root cause analysis

Diagnosis only — the remedies are in the per-stage fix plans above and in §7. Each cause is
tagged with the fix that owns it.

**A. The EDA layer and the RCM layer never talk.** → owned by **S1**
This is the big one. 15 saved analyses produced exceptions that never reached a finding:

| Analysis | Found | Fate |
|---|---|---|
| A-D6F9512D Invoice amount above PO total | INV2025004/05/08 incl. 80M vs 8M | dropped |
| A-A86687CB Shared bank accounts across vendors | V1007+V1018, V1008+V1009 | dropped |
| A-D2310860 Reused bank account numbers | 6 rows | dropped |
| A-9A87B89F Payments to vendors under review | INV2024042, V1010, 36.4M paid | dropped |
| A-8964691B Linked GRN status and receipt chronology | 5 rows | dropped |
| A-94DC6898 Weekend requisition approvals | 29 rows | dropped |
| A-1F753CC9 Rare vendor identifiers | 7 rows | dropped |

The analyses ran at 16:11Z; findings were drafted at 16:56Z. The information was present and
45 minutes old. Findings are drafted only from RCM test executions, so exploratory exceptions
have no path into the RCM, into a test, or into a finding.

One correction to the framing above: A-272B7E11 "Duplicate vendor invoice references" was not
merely dropped — it **ran and returned a clean pass**. It keyed on
`(VENDOR_ID, VENDOR_INVOICE_NUMBER)` over all 118 rows and reported *"No duplicate keys found"*,
`verdict: ok`. The actual duplicates are the same vendor invoice number under **different**
vendor IDs (A07, A08), which that key excludes by construction. An analysis that affirmatively
clears a risk it cannot see is worse than one that never ran, and S1 must not treat a green
analysis as evidence of coverage.

**B. A row with no asserted control cannot be data-tested.** → owned by **R1**, **R2**, **S2**
Not a workflow omission — a hard block. `evidence_kind: inquiry` empties the table schemas in
the generation prompt (`tests.py:175`), so `"data"` never enters `allowed_variants` and no data
test is reachable. The RCM records intent to test, the execution layer records `no_conclusion`,
and the report renders that as a scope limitation rather than an untested control. Four
high/critical rows and four issue classes (A06, A07/A08, A21, A26) trace to this single line.

**C. Document tests only interrogate policy, never vouch transactions.** → owned by **T1**, **T2**
Now measured rather than inferred: 0 of 52 control attributes carry `evidence_kind:
transaction_cycle`, and all 27 doc tests are `kind: qa`. `cycle_vouching.py`, the registry packs
and the evaluator were not merely unused — they were **unreachable**, and the run reported
success. This is the Phase 2/3 gap already recorded in `docs/vouching-grid-plan.md`.

**Secondary:**
- **D.** → **T3**, **S5**. Requisition-anchored test construction makes invoice-only anomalies
  structurally invisible. This alone hides A02, the largest item in the population.
- **E.** → **T4**. `review_required` is terminal. Two tests carrying 24 real exceptions ended
  there and nothing escalated them.
- **F.** → **F1**, **S4**. Low-confidence test results are converted into findings *about the
  test*, losing the real control failures underneath.
- **G.** → **T5**. Extraction does not capture handwriting, so signature attributes cannot be
  tested from text.

## 7. Structural fixes

The per-stage plans above address defects that live inside one stage. These are the
cross-cutting ones — each spans stages, and none can be fixed by improving a single prompt.

### S1. Give exploratory analysis a path into the RCM

**The defect.** Findings are drafted only from RCM test executions. Saved analyses are a parallel
universe with no edge into the audit graph, so 15 analyses holding real exceptions — including
three of the highest-value misses in this engagement — expired where they were computed.

**The shape of the fix.** An analysis exception is a *candidate observation*, not a finding. It
should not auto-promote (that would flood the RCM with weekend-approval noise), but it must
become visible to the stage that decides coverage. Two seams, in preference order:

1. **Coverage reconciliation at RCM generation.** Feed the analysis result index — titles,
   exception counts, and the tables/columns each touched, *not* rows — into the RCM worker's
   context. Add a validation gate: an analysis with a non-zero exception count whose subject
   matches no RCM row's attributes is a coverage gap the proposal must answer, either by adding
   a row or by recording why the exceptions are not control-relevant.
2. **Candidate observations at fieldwork.** Where an analysis exception maps to an existing RCM
   row, raise it as an unconfirmed observation against that row so it appears in the rollup and
   an auditor dispositions it, rather than being silently absent.

**Privacy constraint — this is already solved and must be reused.** Per `AGENTS.md`, analysis
exception rows reach the model only under `allow_analysis_exception_rows`, capped per procedure,
and the two exception-row permissions are deliberately separate. Option 1 above needs no rows at
all — counts and column names are enough for coverage reconciliation, which keeps the highest-value
fix entirely outside the row-disclosure path. Option 2 must go through the existing capped
projection and must not widen it.

**Why this is first.** It is the largest recall win available, it needs no new analytics, and the
evidence is already computed and sitting in `Analyses/.results/`.

### S2. A row that cannot be tested must not look like a row that passed

**The defect.** Three separate mechanisms let untested controls present as satisfactory:

- `evidence_kind: inquiry` makes a data test *unreachable* (§3, R1/R2) — the row is not
  under-tested, it is untestable by construction;
- a row whose only tests are inquiry doc tests can still reach `control_conclusion: effective`
  (RCM-4301AC);
- `no_conclusion` rows are narrated in the report as scope limitations, which reads to a client
  as "we could not look" rather than "we did not test".

**The shape of the fix.** One invariant, enforced at three points: *a conclusion may never be
stronger than the evidence class behind it.* Inquiry-only evidence caps a row at
`partially_effective` with a mandatory `scope_limitations` string; a row whose risk names a
data-observable concept that no executed predicate references cannot settle at all; and the
report must distinguish **not tested** from **tested, no exceptions** in the coverage narrative.
Today both render as an absence.

### S3. Silent subsystem bypass

**The defect.** The cycle-vouching subsystem — registry packs, evaluator, grid — was fully
bypassed for this engagement and *nothing said so*. Zero of 52 control attributes carried
`transaction_cycle`, so zero cycle tests were generated, and the run reported success. The same
class of silence hides the empty-table-schema path in §3 and the `review_required` dead end in §4.

**The shape of the fix.** Any point where the pipeline degrades from a stronger evidence path to
a weaker one must emit a warning that survives into the run record and the report's coverage
section. Specifically: a registered pack matched against analysed documents with no cycle test
generated; an RCM row for which table schemas were withheld; a test that ends `review_required`
holding exceptions. Degradation is acceptable; **silent** degradation is not.

### S4. The pipeline reports on itself to the client

**The defect.** Three of thirteen findings are about the tool's own test quality, two reached the
client-facing report, and the real control failures underneath them were lost in the process.

**The shape of the fix.** A hard boundary between two channels: findings describe the auditee;
QA items describe the engagement's own execution. The `semantic_valid` / `semantic_issues` signal
already exists on every result and is the natural router (§5, F1). Nothing whose subject is a test
should be able to reach the report assembler.

### S5. Anchor and completeness invariants

**The defect.** Tests are anchored on whichever population the model started from, so an invoice
with no requisition cannot appear in any result. INV2024144 — the single largest anomaly in the
population — is invisible for this reason alone, not because of any judgement error.

**The shape of the fix.** A step asserting about population A must be anchored on population A.
Pair it with a reconciliation assertion per population: every row of `invoice_data` must be
reachable by at least one executed test, and the count of unreached rows is reported. That check
alone surfaces INV2024144 without anyone having to anticipate it.

### S6. Regression harness

None of the above is verifiable without a fixture that states what *should* be found. The
procurement workspace now has an independently derived answer key (Appendix A) with 25 issues,
IDs, and values. Wire it as an acceptance fixture: after a clean regeneration, assert recall
against the register and fail the build on regression. `docs/vouching-grid-plan.md` §9 already
defines the manual regeneration checkpoints this would slot into — Checkpoint B and C are the
natural gates.

Without this, "the findings got better" stays a matter of opinion.

### Sequencing

| Order | Fix | Why here |
|---|---|---|
| 1 | S1 option 1 — analysis coverage reconciliation | Largest recall win, no new analytics, no row disclosure |
| 2 | R1 + R2 — decouple `evidence_kind` from control existence | Unblocks four unarmed rows; cheapest high-value change |
| 3 | S2 — conclusion may not exceed evidence class | Stops the RCM-4301AC false negative reaching a client |
| 4 | S4 / F1 — QA channel split | Recovers a real SoD finding currently reported as tooling noise |
| 5 | R3 + T3 / S5 — GRN row and anchor invariants | Owns G5, G10, G11, and surfaces INV2024144 |
| 6 | T1 / S3 — bypass warnings | Prevents the next silent regression |
| 7 | T2 — Checkpoint C repeat with a non-zero cycle-test assertion | Tracked in `vouching-grid-plan.md`; no new design |
| 8 | S6 — regression harness over Appendix A | Makes every fix above measurable |

---

## 8. Making the EDA fast enough to run before the APM

Measured on run `20260809-160933-ea3678`, the analysis workflow that produced all 29 saved
analyses and the memo.

| | |
|---|---|
| Wall clock | **157.5s** (16:09:33.119 → 16:12:10.621) |
| Model time | **153.4s** across 18 calls |
| **Model wait as a share of wall clock** | **97.4%** |
| Prompt tokens | 292,488 (largest of any run in the engagement) |
| Completion tokens | 15,488 |
| Cost | $0.046 |

Local Polars execution of all 29 analyses, join inference and profiling together account for
**about 4 seconds**. The EDA is not slow because it computes a lot. It is slow because it makes
18 model calls one after another.

### 8.1 Nothing in this pipeline runs concurrently

LLM-seconds divided by wall-seconds, every run in the workspace:

| Run | Stage | Calls | Model s | Wall s | Ratio |
|---|---|---:|---:|---:|---:|
| `…152235-5545ab` | document analysis | 8 | 110.1 | 110.3 | **1.00** |
| `…153300-d20f7e` | test generation | 25 | 267.7 | 268.7 | **1.00** |
| `…154605-2d9448` | document QA | 30 | 116.1 | 117.5 | **0.99** |
| `…160933-ea3678` | **analysis / EDA** | 18 | 153.4 | 157.1 | **0.98** |
| `…165554-d16221` | findings | 24 | 190.4 | 191.0 | **1.00** |

A ratio of 1.00 means strictly serial — one call in flight at a time, everywhere, for the whole
engagement. Total serialized model wait across all runs is **~21.9 minutes**.

This is not a missing feature. The scheduler already implements a parallel barrier
(`agent/workflow.py:30`, `PARALLEL_BARRIER = "all_settled_parallel"`) and
`workflow_runner.py:425` fans units out under `max_llm_concurrency`, defaulting to 4. Two
capabilities already declare the barrier — document chunks (`capabilities/documents.py:543`)
and test generation (`capabilities/tests.py:155`).

**Both are nonetheless serial, by design:**

```python
# backend/app/agent/routing.py:1029
max_llm_concurrency=int(run.get("limits", {}).get("max_llm_concurrency") or 1),
```

Every run is installed with `max_llm_concurrency: 1`, which is a **deliberate choice to stay
inside the LLM provider's rate limits**, not an oversight. The `or 4` default in the runner is
consequently unreachable, since routing always sets the key.

This is the binding constraint on everything below. The parallel barrier is built and correct,
but raising it is a provider-capacity decision rather than a code one, so the optimisations in
§8.4 all had to work without it — and the ceiling on what they can achieve follows directly.

### 8.2 Where the 153 seconds go

| Stage | Calls | Model s | Share |
|---|---:|---:|---:|
| `analysis_definitions` | 16 | **104.2** | 68% |
| `analysis_summary` | 1 | **34.9** | 23% |
| `join_utility` | 1 | 14.2 | 9% |

Per-call prompt sizes for the 16 definition calls run 10.7k → 18.6k tokens, with **near-zero
cached tokens** (one call reported 1,331; the rest reported 0). Two things drive the growth:
frame width, and the `current_analyses` source — capped at 16,000 characters — which shows each
call what earlier frames already produced so it does not repeat them.

That duplicate-avoidance channel also couples the units to each other, which is the same
coupling `docs/test-generation-quality.md` §5 already recommends removing from the test worker:
*deduplicate after generation, not during it*. With concurrency fixed at 1 that coupling costs
nothing today, but it is what any future batching or fan-out would have to undo first.

### 8.3 The work itself is larger than it needs to be

One model call is made **per frame**, and frames grow combinatorially: 6 tables and 14 joins
produced 20 targets and 16 definition calls. What those calls bought:

- **3 analyses over `financial_approval_matrix` — a 4-row reference table.** Two frames, two
  full model calls, zero exceptions, and nothing a 4-row table could have yielded.
  (`A-8E91E011`, `A-B6F3ACA1` completeness; `A-CE59BB14` "Invalid approval limit sign scan" —
  verdict *"No negatives (0 zero value(s))"*.)
- **`A-ACDFA399` duplicate-check on `REQUISITION_ID`** — a primary key whose profile already
  records `distinct_pct` 100. Zero by construction.
- **14 of 29 analyses returned zero exceptions** (48%).
- **`A-94DC6898` weekend requisition approvals** — 29 of 108 rows flagged, which is a calendar
  fact, not a control exception.
- Four of the five three-way derived join frames produced no analysis at all, yet each still
  cost a full model turn. The fifth is the counter-example that rules out a depth cap:
  `requisitions_financial_approval_matrix_staff_details_joined_joined` is where the
  approval-limit breach was found. Depth does not predict value here; frame size does.

Meanwhile **all 29 analyses are marked `informative: true`** — the informativeness gate exists
in the result contract and never fires, so nothing downstream can tell the shared bank accounts
(`A-A86687CB`) from the sign scan on four rows.

### 8.4 What was implemented

Concurrency is fixed at 1 deliberately, to stay inside the provider's rate limits. E1 and E2 are
therefore **struck** — not deferred. Everything below is independent of concurrency, and each
change is measured against the recorded prompts of run `20260809-160933-ea3678` rather than
estimated.

**Implemented — A. Reference tables no longer claim a definition turn.**
`capabilities/analysis.py`: `definable_targets()`, used by both the readiness check and the unit
expansion so the stage cannot disagree with itself about its own scope.

A frame is a lookup when it is **small in itself and dwarfed by what it sits beside** — under
`MIN_ANALYSABLE_FRAME_ROWS` (5) *and* at least `LOOKUP_SCALE_RATIO` (10×) smaller than the
largest frame in scope. Neither condition works alone: an absolute floor calls every frame in a
small workspace a lookup, and a purely relative rule calls a 200-row dimension a lookup beside a
million-row ledger. Two guards keep it from deciding an engagement — an explicitly named frame is
never pruned, and a scope where everything is small is analysed in full.

Measured on the procurement workspace: **2 of 20 frames pruned** — `financial_approval_matrix`
and `financial_approval_matrix_staff_details_joined`, both 4 rows. The three-way
`requisitions_financial_approval_matrix_staff_details_joined_joined` (112 rows) survives, which
matters: it carried the approval-limit breach, the single most valuable analysis in the run.

*This is materially less than the "5–6 of 16 calls" estimated in the first draft of this
section.* Only two frames in the whole workspace are lookups, and the join-depth cap that
estimate assumed would have removed the best result in the engagement. Frame pruning is correct
and worth keeping, but it is a ~12% saving on the definition stage, not a third.

**Implemented — B. The transport envelope no longer bills as audit material.**
`workers/analysis.py`: `_model_facing_context()`, used by both analysis workers.

Every resolved item carried a `supplied_size` block (item, character, token and media counts) and
a `representation` envelope. Both exist so the durable manifest can account for what was supplied;
neither says anything about the engagement. `workers/tests.py` already performs this projection —
its docstring is explicit that the model needs the audit material and not the transport envelope —
but the analysis workers serialized the bundle whole.

Measured across the 16 re-parseable analysis-stage calls: **97,344 characters removed, 14.1% of
context, ~24,300 prompt tokens.** No content the model reasons over is affected.

**Implemented — C. The analytics catalog moved ahead of the frame description.**
`workers/analysis.py`, definition worker.

The catalog is byte-identical on all 16 calls — verified, one content hash across the run — at
5,983 characters (~1,495 tokens) re-sent per frame. A provider can only reuse a prefix it has
already seen, and the prefix ends at the first differing byte, so a stable block sitting behind
the per-frame `TARGET FRAME` can never be part of one. Promoting it ahead of everything
frame-specific costs nothing and lengthens the identical head.

Measured shared prefix across the definition calls: **1,160 → 3,021 tokens (2.6×)**, total prompt
characters **756,528 → 650,089 (−14.1%)**. This also lifts the prefix clear of the ~1,024-token
minimum most providers require, which fits the observed behaviour: 15 of 16 calls reported zero
cached tokens against a 1,149-token system prompt sitting right at that boundary.

### 8.5 Not implemented, and why

**E6 — the informativeness gate.** Attempted, then reverted. The gate only fires on *saturation*
(a procedure flagging so much of its frame that the count is the population). Extending it to
*vacuity* — a duplicates test on a key the profile already shows is unique — conflicts with a
deliberate existing contract: `test_an_uninformative_proposal_is_run_and_dropped_before_it_is_saved`
asserts that exactly such a check is the *good* proposal that must survive, and `informative` is
load-bearing, so a false there **drops the procedure**. That is defensible — a uniqueness control
that passes is audit evidence, not noise.

Making this work needs a distinction the contract does not currently carry: *established
nothing* (drop) versus *redundant with a cheaper signal* (keep, deprioritise). That is a contract
change, not a tuning change, and it is the same field S1 would need for coverage reconciliation.
Worth doing deliberately; not worth smuggling in as an optimization.

**E4 as originally scoped — dropping zero-exception results from the memo.** Measuring the
summary prompt showed the premise was wrong. Composition of its 126,930 characters:

| Source | Items | Chars | Share |
|---|---:|---:|---:|
| `analysis_results` | 29 | 38,162 | 40.4% |
| `analysis_anomalies` | 11 | 16,332 | 17.3% |
| `table_profiles` | 6 | 11,920 | 12.6% |
| `table_joins` | 14 | 11,844 | 12.5% |
| `analysis_exceptions` | 4 | 7,017 | 7.4% |
| `table_metadata` | 6 | 6,663 | 7.1% |

The zero-exception results are not padding — they are the coverage record, and a memo that
narrates only what failed cannot say what was tested and found clean. The genuinely wasteful 9%
was the `supplied_size` envelope, which B removes. The rest is evidence the memo uses.

### 8.6 Projected

| | Wall clock | Basis |
|---|---:|---|
| Today | **157s** | measured |
| Frame pruning (A) | ~144s | 2 of 16 calls removed, measured |
| Envelope + catalog (B, C) | **~130–140s** | −14% prompt; latency gain is prompt-processing only, and the cache effect is unverified against the live provider |

**Roughly 10–17%, not the 3× the concurrency-based plan projected.** With concurrency fixed at 1,
the EDA stays a ~2.2-minute step, because 16 serialized round trips dominate and only removing
round trips changes that materially.

That reframes the P1 decision honestly: enforcing EDA-before-APM costs about two minutes of wall
clock per engagement. The question is whether two minutes is acceptable on the critical path —
and given §1, where the EDA layer independently found three of the highest-value missed issues,
it very likely is. But it should be decided as a two-minute cost, not on a promise of 50 seconds.

If that cost does need to come down further without raising concurrency, the only remaining lever
of size is **making fewer calls**: one turn per frame is the design, and 20 frames from 6 tables
is the multiplier. Batching several small frames into one turn, or proposing across the join
family at once, would cut round trips in a way none of the above can.

### 8.7 Verification

```
backend/tests/test_workflow_analysis.py::test_a_lookup_table_costs_no_definition_turn
backend/tests/test_workflow_analysis.py::test_a_small_workspace_is_not_an_empty_one
```

Full suite: **1544 passed, 2 failed** — `test_completion_uses_execution_and_outcome_gates` and
`test_synthetic_procurement_acceptance_from_population_to_preliminary_report`, both failing
identically before these changes. The first is the separately tracked Phase 3 item recorded in
`docs/vouching-grid-plan.md`.

Measurements B and C were taken by replaying the recorded prompts in
`Workspaces/procurement/Debug/LLMCalls/` through the new projection, so they are observed on real
payloads rather than modelled. No agent run was executed and no workspace artifact was modified.

---

## Appendix A — independent issue register

Every issue found by working the six Excel files and seven documents directly, before any
pipeline artifact was opened. This is the answer key for S6.

`Captured` is against the **audit output** — an issue reported only by a saved analysis is *not*
captured, because nothing carried it into the RCM, a finding, or the report. Those rows are
marked **EDA only** and are the direct evidence for S1.

| ID | Issue | Value (PKR) | Captured | Captured where |
|---|---|---:|---|---|
| A01 | 20 invoices reference a requisition id or nothing in `PO_NUMBER_LINK` — no PO exists | 1,054.4M (563.6M paid) | Partial | `DAT-BBEE43384F` → F-F1C01E, framed as requisition-side linkage; unvalued |
| A02 | **INV2024144 paid with no vendor, no PO, no GRN**; vendor invoice no. `VINV011-202313` | 100.0M | **No** | — (string absent from every artifact) |
| A03 | 4 invoices raised against **Rejected** requisitions | 158.0M | **No** | — |
| A04 | INV2025009 re-raises rejected REQ2024047 for the identical amount | 44.1M | **No** | — |
| A05 | Six invoices numbered `VINSUSP001`–`VINSUSP006`, each re-billing a closed prior-year PO | 157.1M | **No** | — |
| A06 | Invoice amount exceeds PO total (5 invoices; incl. 80M against an 8M PO) | 79.8M variance | EDA only | `A-D6F9512D` found 3 of 5; never promoted |
| A07 | Duplicate vendor invoice number across **different** vendors, both paid (`VINV011-202404`) | 16.2M | **No** | `A-272B7E11` keyed on `(VENDOR_ID, VENDOR_INVOICE_NUMBER)` and returned *"No duplicate keys found"* — a false clear |
| A08 | Second duplicate number pair `VINV006-202412` (V1027 paid, V1003 pending) | 9.0M | **No** | as A07 |
| A09 | Same vendor + identical amount, both paid (V1002 × 30.0M) | 30.0M | **No** | — |
| A10 | REQ2024081 approved by CFO against a 10.0M limit | 99.3M | **Yes** | `DAT-2DF158A860` → F-13ACA8 → report §2 |
| A11 | INV2024079 approved by Financial Controller against a 5.0M limit | 27.6M | **No** | invoice-side authority never tested |
| A12 | 110 of 118 invoices approved by roles absent from the approval matrix | 2,855.6M | **No** | — |
| A13 | Segregation of duties — 10 requisitions (requester = verifier / = fin approver) | 504.4M | Downgraded | `DAT-50957C1BF2`, `DAT-864DB4A949` → F-AE7223 / F-E8D783, reported as *"result is not reliable"* |
| A14 | INV2024063 verifier = supervisor approver, paid | 98.1M | Downgraded | as A13 |
| A15 | Cross-document SoD — invoice approver is also the requisition approver (10 invoices) | 166.0M | **No** | — |
| A16 | Invoice dated before its PO (4 invoices, all paid) | 35.2M | **Yes** | `DAT-85368A37FA` → F-13D9B6 → report §3 |
| A17 | Invoice dated before GRN (5 invoices) | 60.2M | Partial | `A-8964691B` (EDA); lifecycle test covers 4 via F-13D9B6 |
| A18 | Payment made before goods receipt (3 invoices) | 50.2M | Partial | inside F-F1C01E's 43 rows; not stated as a payment-before-receipt condition |
| A19 | INV2024008 paid before its own invoice date **and** before supervisor approval | 24.9M | **Yes** | `DAT-EBA9DB1CB1` → F-3FCA6C; `DAT-ADC849F3F2` → F-D92220 (duplicated, see F2) |
| A20 | 22 invoices with no GRN link, incl. INV2024017 (12.0M paid) | 12.0M+ | **No** | noted only as an 18.64% null rate in the APM profile |
| A21 | Two vendor pairs share a bank account (V1008+V1009, V1007+V1018) | — | EDA only | `A-A86687CB`, `A-D2310860`; RCM-4301AC concluded **effective** |
| A22 | 5 vendors created and approved by the same person | — | **No** | — |
| A23 | V1010 "Under Review" paid | 36.4M | EDA only | `A-9A87B89F`; never promoted |
| A24 | V1016 approved the same day it was added — no due-diligence interval | — | **No** | — |
| A25 | No vendor approved by anyone outside Procurement | — | **No** | — |
| A26 | 35 same-vendor requisition pairs within 30 days (REQ2024060+65: 128.8M, 1 day apart) | — | **No** | RCM-99C6E0 exists but carries only an inquiry doc test |
| A27 | Same item at wildly different unit prices (Cybersecurity Training +194%, Cloud Migration +167%) | — | **No** | — |
| A28 | 94 of 112 requisitions state a department contradicting the HR master | — | **No** | — |
| A29 | 3 staff pairs share a bank account (1002/1023, 1003/1034, 1004/1045) | — | **No** | — |
| A30 | `BUYER_ID` B001–B006 reconcile to no staff record | — | **No** | — |
| A31 | GRN2024004 signed "Received and inspected by" by the requisitioner (Ethan Smith, 1041) | 2.0M | **No** | no doc test vouches the package; name lost in extraction |
| A32 | Payment voucher signatures carry no date | 2.0M | **No** | — |
| A33 | SOP §3.2 mandates RFQ/comparative bids; no bid or quotation field exists in any table | 2,868M | Partial | F-6E104E raises the policy gap, never the population-wide absence of evidence |

### Tally

| Outcome | Count | Share |
|---|---:|---:|
| **Yes** — reached a finding and the report | 3 | 9% |
| **Partial** — surfaced but mis-framed, unvalued, or incidental | 4 | 12% |
| **Downgraded** — real exceptions reported as tool unreliability | 2 | 6% |
| **EDA only** — found by a saved analysis, never carried forward | 3 | 9% |
| **No** — absent from every artifact | 21 | 64% |
| | **33** | |

Only **3 of 33 issues (9%)** made it intact from the source data to the report.

Three of the four highest-value single items in the population — A02 (100.0M), A05 (157.1M) and
A03 (158.0M) — are in the **No** band.

### Caveats on the register

- **A12 is a judgement call, not a certainty.** The approval matrix governs requisition financial
  approval; whether it also governs invoice supervisor approval is not stated in the SOP. The
  point stands regardless: the pipeline never raised the question, so the ambiguity was never
  put to the auditee.
- **A26 flags candidates, not confirmed splits.** 35 same-vendor pairs inside 30 days is a
  screening result requiring auditor judgement on whether the purchases are genuinely divisible.
  No screen was run at all, which is the finding.
- **A28–A30 are data-integrity observations** rather than control failures on their own; they
  undermine reliance on the requester and buyer fields used by other tests.
- Values are the gross transaction amounts touched by each issue, not quantified loss, and they
  overlap across rows — do not sum the column.
