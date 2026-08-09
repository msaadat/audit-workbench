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

Of roughly 17 distinct issue classes present in the source material, the pipeline reported
**2 cleanly, 2 partially, downgraded 1 to "unreliable result", and missed 12 entirely.**

The single largest item in the population — a PKR 100,000,000 invoice that was **paid with no
vendor, no PO and no GRN** — appears in no test, no observation, no finding, and no report.
The string `INV2024144` does not occur anywhere in `Planning/`, `DataTests/`, `DocTests/`,
`Findings/`, `Observations/` or `Reports/`.

The root cause is not model quality. It is **three structural breaks** described in
§6, the most serious being that the EDA layer *did* find several of the missed issues and
nothing carries them forward.

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

## 6. Root causes (what to fix in the pipeline)

**A. The EDA layer and the RCM layer never talk.**
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

**B. RCM rows are created without a matching data test.**
Four high/critical rows carry only inquiry doc tests. Nothing in the workflow requires a row
whose risk is data-observable to acquire a data test, so the RCM records intent to test and the
execution layer records `no_conclusion` — and the report reads that as a scope limitation rather
than an untested control.

**C. Document tests only interrogate policy, never vouch transactions.**
The doc-test generator produces "does the documentation define X" for every RCM row. With a
voucher package on hand, it should be generating tie-out attributes (amount agreement across
five documents, date sequence, quantity agreement, signature/authority checks). `cycle_vouching.py`
and the evidence-recipe registry exist in the codebase; this workspace shows no sign of them
being exercised.

**Secondary:**
- **D.** Requisition-anchored test construction makes invoice-only anomalies structurally
  invisible. Tests should be anchored on the population being asserted about.
- **E.** `review_required` is terminal. Two tests carrying 24 real exceptions ended there and
  nothing escalated them.
- **F.** Low-confidence test results are converted into findings *about the test*. These should
  be routed to a QA queue, not to the client-facing report.
- **G.** OCR/extraction does not capture handwriting, so signature attributes cannot be tested
  from text. Either OCR the signature blocks or route signature attributes to image review.

## 7. Suggested priority

1. Wire exploratory analysis exceptions into RCM coverage — biggest recall win, and the
   evidence is already sitting in `Analyses/.results/`.
2. Require a data test for any RCM row whose risk is data-observable; block `effective`
   conclusions on rows whose only tests are inquiry doc tests (fixes the RCM-4301AC false negative).
3. Add a goods-receipt / three-way-match RCM row and the invoice-anchored tests behind it.
4. Generate real vouching attributes for uploaded document packages.
5. Stop emitting "test result is unreliable" as a client-facing finding.
6. Add a findings→report reconciliation assertion (13 in, 12 out, silently).
