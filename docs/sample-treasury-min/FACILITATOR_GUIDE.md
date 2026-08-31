# Facilitator guide - minimised treasury pack

The answer key. None of it is in the data files, the policy document, the deal
pack or any filename.

This pack tests the **workflow mechanism**, not audit output quality. It is
sized so a full intake -> RCM -> analysis -> vouch cycle runs cheaply, and
seeded so that exactly one exception exists per failing control. Judge a run on
whether each stage fired and joined correctly, not on the depth of the
findings.

## Scope

- Population: 41 deals, `TD-2025-0160` to `TD-2025-0200`, struck 31 January to
  10 February 2025, PKR 5,844,246,605.00 aggregate notional, and 40
  confirmations.
- Criteria: seven clauses of the Treasury and Investment Policy - 4.1, 5.2,
  5.4, 5.6, 7.2, 7.3, 7.4.
- Documents: the policy extract, and one deal pack of four PDFs for
  `TD-2025-0166`.

## Expected controls and results

| Clause | Control | Test | Expected result |
| --- | --- | --- | --- |
| 4.1 | Dealing is segregated from confirmation | `CONFIRMED_BY_ID` against `CAPTURED_BY_ID` and `DEALER_ID` across the join | **1 exception** |
| 5.2 | Dealing hours and business days | `DEAL_TIME` within 09:00-17:00; `DEAL_DATE` a business day | **Clean** |
| 5.4 | Capture within 15 minutes of execution | `CAPTURE_TIMESTAMP` less `DEAL_DATE` + `DEAL_TIME` | **Clean** - the longest lag is 9.6 minutes |
| 5.6 | Amendments require operations approval | `AMENDED_FLAG` = `Y` with no `AMENDMENT_APPROVED_BY_ID` | **Clean** - nothing was amended |
| 7.2 | Confirmation despatched within one business day | `SENT_DATE` against `DEAL_DATE` | **Clean** |
| 7.3 | Discrepancies resolved before settlement | `MATCH_STATUS` | **Clean** - all 40 are `Matched` |
| 7.4 | No deal settled unconfirmed | settled deals with no confirmation row; received date after value date | **1 exception** |

Five of the seven controls conclude *effective*. That is deliberate: a run that
only ever raises exceptions has not been shown to conclude either way.

## Exceptions in the tables

| Ref | Exception | Severity | Deal |
| --- | --- | --- | --- |
| M1 | Deal captured and confirmed by the same person | High | `TD-2025-0167` |
| M2 | No confirmation record exists for a settled deal | High | `TD-2025-0180` |

- **M1** - `TD-2025-0167` was dealt and captured by TS-006 and confirmed by
  TS-006. The dealer signed off the counterparty confirmation on their own
  deal. Clause 4.1. Every other deal in the window is confirmed by TS-015,
  TS-016 or TS-020, so the exception stands alone against a clean pattern.
- **M2** - `TD-2025-0180` carries `DEAL_STATUS` = `Settled` with nothing in the
  confirmation file to show the counterparty agreed its terms. The join has to
  be run left to right to find it; a join driven off the confirmation file will
  not. Clause 7.4.

The reverse direction is clean: all 40 confirmations match a deal. So is the
reference sequence - the window is contiguous, `TD-2025-0160` through
`TD-2025-0200` with no gaps - and so is the arithmetic: `NOTIONAL_PKR` agrees
to `NOTIONAL_AMOUNT` x `DEAL_RATE` on every row.

## The vouching exception

`TD-2025-0166` is clean in both tables and each document in its pack is
internally consistent. The exception exists only when the paper is read against
the system.

| Ref | Exception | Severity | Document |
| --- | --- | --- | --- |
| V1 | Rate on the counterparty confirmation differs from the rate booked | High | `CNF-2025-0166_Counterparty_Confirmation.pdf` |

- **V1** - The deal file and the dealing ticket both carry USD/PKR 279.5132 on
  a principal of USD 900,000, PKR 251,561,880.00. Northgate Bank's
  confirmation, on Northgate letterhead under SWIFT `NRGTPKLA`, confirms the
  same deal reference at **282.1686** in field `:37G:`. The difference of
  2.6554 is PKR 2,389,860 on the principal. The confirmation table records the
  deal as `Matched`, so no data test reaches it.

The pack holds four documents, one of each type: the dealing ticket, the
counterparty confirmation, the payment instruction and the nostro account
statement. Only the confirmation carries the exception; the other three agree
with the deal record, and the payment instruction carries all three signatures
its amount requires. That is the point - the vouch has to read all four and
raise one.

## What a good run looks like

1. Both CSVs land as tables with the columns typed sensibly.
2. All four documents classify to distinct types from their filenames alone.
3. The RCM authors roughly seven controls from seven clauses, and none of them
   about limits, market rates or settlement routing - there is no table for
   those and the policy extract does not carry those clauses.
4. Analysis raises M1 and M2 and concludes the other five controls effective.
5. The vouch of `TD-2025-0166` raises V1 and does not raise a false positive on
   the dealing ticket, the payment instruction or the nostro statement.
6. Findings cite the deal reference, the clause and the document.

## How this differs from the full pack

The rows and PDFs are copied from `docs/sample-treasury` unchanged, with three
edits, all applied in `generator/gen_min.py`:

- `TD-2025-0196` - `NOTIONAL_PKR` raised by PKR 0.50 to agree to notional times
  rate. Harmless in a file of 1,000 rows; in a file of 41 it would be the only
  recomputation break and would read as a seeded exception.
- `CNF-2025-0168` - confirmer changed from TS-007 to TS-016. The full pack
  seeds the same segregation failure twice; one is enough.
- `CNF-2025-0181` and `CNF-2025-0183` - added. The full pack leaves three
  settled deals unconfirmed; here only `TD-2025-0180` is.

## Counting

- 41 deals, 40 confirmations, 5 documents.
- 2 exception types in the tables, one deal reference each.
- 1 exception type reachable only by vouching, one deal reference.
- 5 of 7 controls clean.
