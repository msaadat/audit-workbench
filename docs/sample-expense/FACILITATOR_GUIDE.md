# Facilitator guide - Employee Expense Reimbursement Audit

## Suggested story

Northstar Trading reimburses employee travel and operating expenses through a portal. Finance asked Internal Audit to assess whether claims are supported, approved within delegated limits, processed after approval, and reimbursed in line with the SOP.

## Compact audit scope

- Period: 1 April to 30 June 2025.
- Population: 40 paid employee expense claims.
- Objective: assess operating effectiveness of receipt, policy, delegated-approval, submission-timing, and payment-sequencing controls.
- Documents: SOP, delegation matrix, and 12 selected payment-voucher packs.

## Recommended RCM controls

| Risk | Control | Test approach |
|---|---|---|
| Unsupported claims are paid | Receipts are required for items >= PKR 1,000 | Test claim lines and voucher support. |
| Unauthorized reimbursements are approved | Portal validates delegated approval limit | Compare claim amount to approver limit. |
| Policy limits are exceeded | Manager checks category, daily meal cap, and business purpose | Test categories and daily meal claims. |
| Stale claims are paid | Claims are submitted within 60 days | Calculate days from final expense date to submission. |
| Payment is released without approval | Finance verifies approval before payment | Compare approval date to payment voucher date. |

## Expected exception ground truth (for the facilitator only)

| Claim | Expected exception | Suggested severity |
|---|---|---|
| EXP-2025-003 | Missing receipt for PKR 4,800 accommodation claim | Moderate |
| EXP-2025-007 | PKR 6,200 domestic meal exceeds PKR 4,000 cap | Moderate |
| EXP-2025-012 | PKR 47,500 claim approved by manager with PKR 30,000 limit | High |
| EXP-2025-004 and EXP-2025-018 | Duplicate receipt ID RCP-77820 is used on two different claims | High |
| EXP-2025-023 | Payment date is before approval date | High |
| EXP-2025-029 | Claim submitted 66 days after expense date | Low |
| EXP-2025-035 | Ineligible alcohol expense reimbursed | Moderate |
| EXP-2025-040 | Personal residence ride lacks valid business purpose | Low |

## Demo pacing

Use the automatically generated RCM and tests, then open two or three selected vouchers to show document evidence. Good choices are EXP-2025-003, EXP-2025-012, EXP-2025-018, and EXP-2025-023. Promote the approval and payment-sequencing observations into findings, then generate the working paper and report draft.
