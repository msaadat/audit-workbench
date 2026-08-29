# Data dictionary - Meridian Bank treasury dealing

Ten populations covering 1 January to 30 June 2025. All entities, people,
counterparties, rates and transactions are fictional.

Load each file as a table using the file stem as the table name. Amounts are
PKR unless a currency column says otherwise; dates are ISO `YYYY-MM-DD`; times
are 24-hour local.

---

## 04_deals.csv - 1,000 rows

The transaction spine. One row per deal struck by the dealing room.

| Column | Meaning |
| --- | --- |
| `DEAL_ID` | Deal reference, allocated at capture. Format `TD-2025-nnnn`. |
| `DEAL_DATE` | Trade date. |
| `DEAL_TIME` | Time of execution, as distinct from time of capture. |
| `DEAL_TYPE` | `FX_SPOT`, `FX_FORWARD`, `MM_PLACEMENT`, `MM_BORROWING`, `TBILL_PURCHASE`. |
| `INSTRUMENT` | `USD/PKR`, `EUR/PKR`, `GBP/PKR`, `AED/PKR`, `PKR_MM`, `PKR_TBILL`. Joins to `07_market_rates`. |
| `TENOR` | `SPOT`, `1W`, `2W`, `1M`, `2M`, `3M`, `6M`, `12M`. Joins to `07_market_rates`. |
| `COUNTERPARTY_ID` | Joins to `03_counterparties`. |
| `DEALER_ID` | The dealer who struck it. Joins to `01_staff` and `02_dealer_limits`. |
| `BROKER_ID` | Joins to `09_brokers`. Empty where the deal was direct. |
| `CURRENCY` | Currency of `NOTIONAL_AMOUNT`. `PKR` for money market and T-bills. |
| `NOTIONAL_AMOUNT` | Principal in `CURRENCY`. |
| `DEAL_RATE` | FX rate for foreign exchange; per cent per annum for money market and T-bills. |
| `NOTIONAL_PKR` | PKR equivalent. For FX this is `NOTIONAL_AMOUNT` x `DEAL_RATE`. |
| `VALUE_DATE` | Contracted settlement date. |
| `MATURITY_DATE` | Equals `VALUE_DATE` for FX; value date plus tenor for money market and T-bills. |
| `DEAL_STATUS` | `Settled`, `Outstanding` (value date after the period end), `Cancelled`. |
| `CAPTURED_BY_ID` | Who entered the deal in the treasury system. |
| `CAPTURE_TIMESTAMP` | When it was entered. |
| `AMENDED_FLAG` | `Y` where the deal was amended or cancelled after capture. |
| `AMENDMENT_DATE` | Date of the amendment. |
| `AMENDMENT_APPROVED_BY_ID` | Operations approver for the amendment. |

Counterparty exposure is the sum of `NOTIONAL_PKR` for deals live on a given
date, that is where `VALUE_DATE <= date <= MATURITY_DATE`. Testing exposure
deal by deal will not find an aggregate breach.

## 05_confirmations.csv - 997 rows

One row per confirmed deal. `DEAL_ID` joins to `04_deals`.

| Column | Meaning |
| --- | --- |
| `CONFIRMATION_ID` | `CNF-2025-nnnn`. |
| `CONFIRMATION_MODE` | `SWIFT MT300` (FX), `MT320` (money market), `MT518` (securities). |
| `SENT_DATE` | Date the bank despatched its confirmation. |
| `COUNTERPARTY_RECEIVED_DATE` | Date the counterparty's confirmation was received and matched. |
| `CONFIRMED_BY_ID` | Who matched it. Joins to `01_staff`; the `DESK` column is what makes a segregation test meaningful. |
| `MATCH_STATUS` | `Matched`, `Discrepancy`, `Cancelled`. |
| `DISCREPANCY_NOTE` | Free text, populated only where the status is `Discrepancy`. |

## 06_settlements.csv - 934 rows

One row per settlement. `DEAL_ID` joins to `04_deals` - test that join in both
directions.

| Column | Meaning |
| --- | --- |
| `SETTLEMENT_ID` | `STL-2025-nnnn`. |
| `SETTLEMENT_DATE` | Date funds moved. |
| `SETTLEMENT_CURRENCY` / `SETTLEMENT_AMOUNT` | The leg paid. |
| `SETTLEMENT_AMOUNT_PKR` | PKR equivalent of the settlement. |
| `SSI_ID` | Standing settlement instruction used. Joins to `08_standing_settlement_instructions`. Empty where none was quoted. |
| `BENEFICIARY_BANK`, `BENEFICIARY_ACCOUNT` | Where the money went. |
| `NOSTRO_ACCOUNT` | Account debited. |
| `RELEASED_BY_ID`, `APPROVED_BY_ID` | Release and approval. |
| `SECOND_APPROVER_ID` | Populated for settlements at or above the dual-signature threshold. |
| `PAYMENT_REFERENCE` | `PMT-2025-nnnnn`, quoted on the payment instruction. |

## 01_staff.csv - 22 rows

`STAFF_ID`, `NAME`, `JOB_TITLE`, `DESK`, `SUPERVISOR_ID`.

`DESK` is `Front Office`, `Middle Office`, `Back Office`, `Compliance` or
`Executive`. Segregation tests that compare identifiers alone will miss cases
where two different people sit on the same desk.

## 02_dealer_limits.csv - 7 rows

`DEALER_ID`, `PER_DEAL_LIMIT_PKR`, `DAILY_LIMIT_PKR`, `AUTHORISED_PRODUCTS`,
`AUTHORISATION_EXPIRY_DATE`.

`AUTHORISED_PRODUCTS` is a semicolon-separated list of `DEAL_TYPE` values. Not
every dealer who appears in `04_deals` has a row here.

## 03_counterparties.csv - 10 rows

`COUNTERPARTY_ID`, `COUNTERPARTY_NAME`, `COUNTERPARTY_TYPE`, `CREDIT_RATING`,
`EXPOSURE_LIMIT_PKR`, `LIMIT_EXPIRY_DATE`, `STATUS`, `STATUS_EFFECTIVE_DATE`.

`EXPOSURE_LIMIT_PKR` is the maximum aggregate principal that may be outstanding
at any one time. `STATUS_EFFECTIVE_DATE` is populated only where the status is
not `Active`, and is the date from which the change applies.

## 07_market_rates.csv - 2,880 rows

`RATE_DATE`, `INSTRUMENT`, `TENOR`, `MID_RATE`, `DAY_HIGH`, `DAY_LOW`.

Published independently of the dealing room. Joins to `04_deals` on
`DEAL_DATE = RATE_DATE`, `INSTRUMENT` and `TENOR`. There is no row for a
non-business day, so a deal that fails this join is itself the finding.

## 08_standing_settlement_instructions.csv - 45 rows

`SSI_ID`, `COUNTERPARTY_ID`, `CURRENCY`, `BENEFICIARY_BANK`,
`BENEFICIARY_ACCOUNT`, `EFFECTIVE_DATE`, `APPROVED_BY_ID`, `STATUS`.

`STATUS` is `Active` or `Superseded`. An amendment appears as a new row with a
later `EFFECTIVE_DATE`, with the previous row marked `Superseded`.

## 09_brokers.csv - 3 rows

`BROKER_ID`, `BROKER_NAME`, `PANEL_SEGMENT`, `STATUS`.

## 10_policy_parameters.csv - 14 rows

`PARAMETER_ID`, `PARAMETER`, `VALUE`, `UNIT`, `POLICY_CLAUSE`.

The thresholds from the Treasury and Investment Policy in structured form, so
a test can cite a number rather than embed one. `POLICY_CLAUSE` points at the
clause in `documents/01_Treasury_and_Investment_Policy.docx`.

---

## Recommended joins

| Left | Right | Key |
| --- | --- | --- |
| `04_deals` | `05_confirmations` | `DEAL_ID` (test both directions) |
| `04_deals` | `06_settlements` | `DEAL_ID` (test both directions) |
| `04_deals` | `03_counterparties` | `COUNTERPARTY_ID` |
| `04_deals` | `02_dealer_limits` | `DEALER_ID` (a left join; some deals have no match) |
| `04_deals` | `01_staff` | `DEALER_ID = STAFF_ID` |
| `04_deals` | `07_market_rates` | `DEAL_DATE = RATE_DATE`, `INSTRUMENT`, `TENOR` |
| `05_confirmations` | `01_staff` | `CONFIRMED_BY_ID = STAFF_ID` |
| `06_settlements` | `01_staff` | `RELEASED_BY_ID` and `APPROVED_BY_ID` to `STAFF_ID` |
| `06_settlements` | `08_standing_settlement_instructions` | `SSI_ID` |

## What the data will not tell you

The populations are complete and internally consistent except where an
exception makes them otherwise. Some of what this engagement has to find is not
in these files at all: it is in the deal packs, and in the disagreement between
the packs and these tables.
