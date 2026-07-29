# Data dictionary and relationships

## Recommended table relationships

| From table | Key | To table | Key | Purpose |
|---|---|---|---|---|
| `02_expense_claims` | `claim_id` | `03_claim_line_items` | `claim_id` | Test receipt and category detail against a claim. |
| `02_expense_claims` | `claim_id` | `04_approval_log` | `claim_id` | Test approval role, limit, and date. |
| `02_expense_claims` | `payment_voucher_id` | `05_payment_vouchers` | `payment_voucher_id` | Test payment date and paid amount. |
| `05_payment_vouchers` | `payment_voucher_id` | `06_gl_postings` | `payment_voucher_id` | Confirm posting to travel and expense GL. |
| `02_expense_claims` | `employee_id` | `01_employees` | `employee_id` | Add employee role and cost-center context. |

## Suggested data analytics

- Claim lines at or above PKR 1,000 where `receipt_available = No`.
- Meal lines over PKR 4,000 per employee and expense date.
- Approval log entries where `total_claimed_pkr > delegated_limit_pkr`.
- Payment vouchers where `payment_date < approval_date`.
- Claims where `submission_date - expense_end_date > 60 days`.
- Duplicate `receipt_id` used across different `claim_id` values.
- Lines with an ineligible category or a business purpose that signals personal expenditure.

## Field conventions

- Amount fields are numeric PKR values without currency symbols.
- Dates use ISO `YYYY-MM-DD` values.
- IDs are stable, artificial keys that may be used for joins and evidence anchors.
- The GL extract records the reimbursement expense debit only, to keep the demo focused on the expense process.
