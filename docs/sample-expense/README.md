# Employee Expense Reimbursement Audit Demo Pack

All entities, people, transactions, policies, and documents in this folder are fictional. The pack is designed for a compact Q2 2025 internal-audit demonstration of employee expense reimbursements.

## Fast demo setup

1. Create a workspace named **Employee Expense Reimbursement Audit - Q2 2025**.
2. Import each CSV from `data/` as a separate table. Use the file stem as the table name.
3. Add the two control-reference DOCX files and the selected PDFs from `documents/` to the document inventory.
4. In auto-mode, use this command:

   `Perform an internal audit of employee expense reimbursements for Q2 2025. Assess compliance with the Expense Reimbursement SOP, test approval, receipt, timing, and payment-sequencing controls; identify exceptions; prepare evidence-backed findings and a draft report.`

## Contents

- `data/01_employees.csv` - employee master and cost-center context.
- `data/02_expense_claims.csv` - 40 paid Q2 claims.
- `data/03_claim_line_items.csv` - claim-level expense evidence and receipt identifiers.
- `data/04_approval_log.csv` - delegated approvals.
- `data/05_payment_vouchers.csv` - payment population.
- `data/06_gl_postings.csv` - GL posting evidence.
- `data/07_policy_limits.csv` - policy thresholds in structured form.
- `data/README_Data_Dictionary.md` - fields and recommended joins.
- `documents/01_Expense_Reimbursement_SOP.docx` - policy and procedure.
- `documents/02_Expense_Delegation_of_Authority.docx` - approval limits.
- `documents/voucher-packs/` - 12 selected supporting vouchers, including clean and exception examples.
- `FACILITATOR_GUIDE.md` - suggested demo path and expected audit results.

The sample deliberately includes exceptions, but no exception flag is embedded in the imported transactions.
