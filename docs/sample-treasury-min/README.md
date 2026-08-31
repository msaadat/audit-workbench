# Treasury Dealing Audit Demo Pack - minimised

A cut-down copy of `docs/sample-treasury`, built to exercise the **workflow
mechanism** rather than audit output quality: intake and classification,
schema induction, RCM authoring, analysis, table joins and cycle vouching,
at a size that keeps processing time and LLM calls low.

One policy document, two tables, one deal pack. Every row and every PDF is
copied unaltered from the full pack, so the two agree wherever they overlap.

All entities, people, counterparties, rates and transactions are fictional.

## Contents

| | |
| --- | --- |
| `data/04_deals.csv` | 41 deals, `TD-2025-0160` to `TD-2025-0200`, PKR 5.84bn notional |
| `data/05_confirmations.csv` | 40 confirmations |
| `data/README_Data_Dictionary.md` | fields and joins |
| `documents/01_Treasury_and_Investment_Policy.docx` | the compliance baseline, cut to eight clauses |
| `documents/deal-packs/01_TD-2025-0166/` | one sampled deal, four single-page PDFs |
| `FACILITATOR_GUIDE.md` | the answer key |
| `generator/gen_min.py` | regenerates this pack from `docs/sample-treasury` |

The pack is seeded for exactly one exception per failing control: two in the
tables, one reachable only by vouching, and five controls that conclude clean.
The deal window is contiguous and the notional arithmetic agrees on every row,
so sequence and recomputation tests raise nothing spurious. `FACILITATOR_GUIDE.md`
carries the answer key and the three edits made to the copied rows.

## Setup

1. Create a workspace named **Treasury Dealing Audit - minimised**.
2. Import both CSVs from `data/` as separate tables, using the file stem as the
   table name.
3. Add `documents/01_Treasury_and_Investment_Policy.docx` to the document
   inventory, then stage `documents/deal-packs/01_TD-2025-0166/`. Each of the
   four PDFs is one document; intake classifies from the filename.
4. In auto-mode:

   `Perform an internal audit of treasury deal capture and counterparty
   confirmation for deals struck between 31 January and 10 February 2025.
   Assess compliance with the Treasury and Investment Policy; test segregation
   of dealing from confirmation, dealing hours, capture timeliness,
   confirmation despatch and completeness; vouch the sampled deal to its
   document pack; identify exceptions and prepare evidence-backed findings.`

## What it is scoped to exercise

- **Analysis on one table.** Dealing hours, capture lag and amendment approval
  run on `04_deals` alone. All three are clean, so the run has to be able to
  conclude *effective*, not only to raise exceptions.
- **A join in both directions.** `04_deals` to `05_confirmations` on `DEAL_ID`.
  Left to right finds one settled deal with no confirmation. Right to left
  finds nothing, which is also a result.
- **A join that resolves people.** Comparing `CAPTURED_BY_ID` to
  `CONFIRMED_BY_ID` raises one deal. The full pack's staff file is not here,
  so the test works on identity alone.
- **Cycle vouching.** `TD-2025-0166` is clean in both tables. Its counterparty
  confirmation states a different rate from the one booked. The exception
  exists only in the disagreement between the paper and the system, which is
  the case for vouching at all.

## What it deliberately drops

No staff, dealer limit, counterparty, market rate, SSI, broker or policy
parameter file, and no settlement file. Limit breaches, rate reasonableness,
settlement routing and desk-level segregation are all out of scope here - use
the full pack for those. The policy extract is cut to match, so no control is
authored that no table can test.
