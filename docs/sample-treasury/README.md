# Treasury Dealing Audit Demo Pack

All entities, people, counterparties, rates, transactions and documents in this
folder are fictional. The pack is built for an internal audit of treasury
dealing, confirmation and settlement at a bank, for the half year ended
30 June 2025.

It is deliberately larger than `docs/sample-expense`: 1,000 deals across five
instrument types, with the supporting confirmation, settlement, limit, market
rate and standing-instruction populations, so the workspace exercises context
handling as well as audit logic.

## Fast demo setup

1. Create a workspace named **Treasury Dealing Audit - H1 2025**.
2. Import each CSV from `data/` as a separate table. Use the file stem as the
   table name.
3. Add the three DOCX files from `documents/` and a selection of the deal packs
   from `documents/deal-packs/` to the document inventory.
4. In auto-mode, use this command:

   `Perform an internal audit of treasury dealing, confirmation and settlement
   for the half year ended 30 June 2025. Assess compliance with the Treasury
   and Investment Policy; test dealer and counterparty limits, rate
   reasonableness against the independent market rate file, segregation of
   duties, confirmation timeliness and completeness, and settlement routing and
   timing; identify exceptions; prepare evidence-backed findings and a draft
   report.`

## Contents

- `data/04_deals.csv` - 1,000 deals, PKR 158bn notional, 1 January to 30 June 2025.
- `data/05_confirmations.csv` - counterparty confirmations.
- `data/06_settlements.csv` - the settlement population.
- `data/01_staff.csv` - treasury staff and their desks.
- `data/02_dealer_limits.csv` - per-deal and per-day dealer limits and product authorisations.
- `data/03_counterparties.csv` - counterparty exposure limits, ratings and status.
- `data/07_market_rates.csv` - the independent daily market rate file.
- `data/08_standing_settlement_instructions.csv` - approved settlement accounts.
- `data/09_brokers.csv` - the broker panel.
- `data/10_policy_parameters.csv` - policy thresholds in structured form.
- `data/README_Data_Dictionary.md` - fields, joins and the definitions the tests depend on.
- `documents/01_Treasury_and_Investment_Policy.docx` - the compliance baseline, sections 4 to 8.
- `documents/02_Counterparty_and_Dealer_Limit_Matrix.docx` - approved limits.
- `documents/03_Minutes_Internal_Audit_Planning.docx` - planning meeting minutes.
- `documents/deal-packs/` - 18 sampled deals, each a single PDF holding the deal
  ticket, broker note where applicable, counterparty confirmation, settlement
  payment instruction and nostro statement extract.
- `FACILITATOR_GUIDE.md` - the answer key and a suggested demo path.

## What the sample is built to demonstrate

Exceptions fall into three classes, and the split is deliberate:

1. **Visible in the tables.** Limit breaches, off-market rates, segregation
   failures, missing confirmations, misrouted settlements. The analytics find
   these without opening a document.
2. **Visible only in the documents.** An unsigned deal ticket, a rate altered
   in ink without initials, a confirmation the bank produced itself, a payment
   instruction released under one signature where policy requires two. Every
   one of these deals is clean in the tables.
3. **The paper contradicts the system.** A deal booked to one counterparty and
   confirmed by another; a confirmed rate that differs from the booked rate; a
   complete pack, with funds gone from the nostro, for a deal reference the
   system never recorded. Clean in the tables, internally consistent on each
   document, and wrong only when the two are read together.

There is also one control that cannot be concluded on at all: a dealer who
transacted through the period has no row in the dealer limit file, so 61 deals
are untestable for authority. The correct result there is *inconclusive*, not a
pass.

The sample deliberately includes exceptions, but no exception flag is embedded
in any table, document or filename.
