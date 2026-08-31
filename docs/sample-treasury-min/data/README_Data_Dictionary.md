# Data dictionary - Meridian Bank treasury dealing (minimised)

Two populations covering deals struck between 31 January and 10 February 2025.
All entities, people, counterparties and transactions are fictional.

Load each file as a table using the file stem as the table name. Amounts are
PKR unless a currency column says otherwise; dates are ISO `YYYY-MM-DD`; times
are 24-hour local.

---

## 04_deals.csv - 41 rows

The transaction spine. One row per deal struck by the dealing room. References
run `TD-2025-0160` to `TD-2025-0200` with no gaps.

| Column | Meaning |
| --- | --- |
| `DEAL_ID` | Deal reference, allocated at capture. Format `TD-2025-nnnn`. |
| `DEAL_DATE` | Trade date. |
| `DEAL_TIME` | Time of execution, as distinct from time of capture. |
| `DEAL_TYPE` | `FX_SPOT`, `FX_FORWARD`, `MM_PLACEMENT`, `MM_BORROWING`, `TBILL_PURCHASE`. |
| `INSTRUMENT` | `USD/PKR`, `EUR/PKR`, `GBP/PKR`, `AED/PKR`, `PKR_MM`, `PKR_TBILL`. |
| `TENOR` | `SPOT`, `1W`, `2W`, `1M`, `2M`, `3M`, `6M`, `12M`. |
| `COUNTERPARTY_ID` | The counterparty. No counterparty master accompanies this cut. |
| `DEALER_ID` | The dealer who struck it. |
| `BROKER_ID` | Empty where the deal was direct. |
| `CURRENCY` | Currency of `NOTIONAL_AMOUNT`. `PKR` for money market and T-bills. |
| `NOTIONAL_AMOUNT` | Principal in `CURRENCY`. |
| `DEAL_RATE` | FX rate for foreign exchange; per cent per annum for money market and T-bills. |
| `NOTIONAL_PKR` | PKR equivalent. For FX this is `NOTIONAL_AMOUNT` x `DEAL_RATE`. |
| `VALUE_DATE` | Contracted settlement date. |
| `MATURITY_DATE` | Equals `VALUE_DATE` for FX; value date plus tenor for money market and T-bills. |
| `DEAL_STATUS` | `Settled` throughout this cut. |
| `CAPTURED_BY_ID` | Who entered the deal in the treasury system. |
| `CAPTURE_TIMESTAMP` | When it was entered. |
| `AMENDED_FLAG` | `Y` where the deal was amended or cancelled after capture. `N` throughout this cut. |
| `AMENDMENT_DATE` | Date of the amendment. Empty throughout this cut. |
| `AMENDMENT_APPROVED_BY_ID` | Operations approver for the amendment. Empty throughout this cut. |

## 05_confirmations.csv - 40 rows

One row per confirmed deal. `DEAL_ID` joins to `04_deals`. One settled deal has
no row here, so test the join in both directions.

| Column | Meaning |
| --- | --- |
| `CONFIRMATION_ID` | `CNF-2025-nnnn`. |
| `DEAL_ID` | Joins to `04_deals`. |
| `CONFIRMATION_MODE` | `SWIFT MT300` (FX), `MT320` (money market), `MT518` (securities). |
| `SENT_DATE` | Date the bank despatched its confirmation. |
| `COUNTERPARTY_RECEIVED_DATE` | Date the counterparty's confirmation was received and matched. |
| `CONFIRMED_BY_ID` | Who matched it. Compare to `CAPTURED_BY_ID` and `DEALER_ID` on the deal. |
| `MATCH_STATUS` | `Matched`, `Discrepancy`, `Cancelled`. |
| `DISCREPANCY_NOTE` | Free text, populated only where the status is `Discrepancy`. |

---

## Recommended joins

| Left | Right | Key |
| --- | --- | --- |
| `04_deals` | `05_confirmations` | `DEAL_ID` (test both directions) |

Segregation is tested inside that join: `CONFIRMED_BY_ID` against
`CAPTURED_BY_ID`, and `CONFIRMED_BY_ID` against `DEALER_ID`. The full pack's
staff file, which would place each identifier on a desk, is not part of this
cut, so the test here is on identity alone.

## Business calendar

Saturdays and Sundays are non-business days. 5 February 2025 is a public
holiday and falls inside the window; every other weekday in it is a business
day.

## What the data will not tell you

The two populations are complete and internally consistent except where an
exception makes them otherwise. The sampled deal `TD-2025-0166` is clean in
both files. What is wrong with it is in the deal pack, and only in the
disagreement between the pack and these tables.
