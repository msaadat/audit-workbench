# Facilitator guide - Global Bank procure-to-pay audit

Everything below is the answer key. Nothing in the data files or the documents
marks an exception: there are no flag columns and no hints in filenames.

Every exception was established independently from the six Excel files and the
thirteen documents, against the rules in the Procurement SOP extract and the
Financial Approval Matrix. Each one was then recomputed in Polars, so every
record, count and value below can be reproduced from `data/`.

## Suggested story

Global Bank is expanding into new regions and into digital banking. New
branches, technology upgrades and recruitment have pushed procurement volume up
across every department. At the planning meeting on 22 July 2025, the Head of
Procurement made two admissions to Internal Audit: approvals and supporting
records have at times been completed after the event, and nobody has
systematically reviewed exceptions. The planned review covers the whole
procure-to-pay cycle.

## Compact audit scope

- Period: requisitions raised 8 January to 20 December 2024. The invoices and
  payments that settle them run to 10 February 2025.
- Population: 55 requisitions (PKR 645,372,000), 52 purchase orders
  (PKR 609,982,000) and 52 invoices (PKR 622,535,800), all paid.
- Masters: 20 staff, 12 vendors, and a 4-row financial approval matrix.
- Documents: the SOP extract, the approval matrix, the planning minutes, and two
  sample packs, each holding the requisition, PO, goods receipt note, vendor
  invoice and payment voucher for one transaction as five PDFs: pack 01,
  REQ2024009 / PO2024004 / INV2024004, and pack 02, REQ2024105 / PO2024105 /
  INV2024105. Every document is a text PDF, signatures and stamps included,
  so everything a reader sees on the page is in its extracted text.
- Currency: PKR throughout. The source documents say so, and no figure in this
  pack is in any other currency.

## Recommended RCM controls

| Risk | Control (SOP) | Test approach |
| --- | --- | --- |
| A requisition is approved by someone who raised or checked it | Procurement approver is a person other than the requester and the verifier (§2) | Compare requester, verifier and approver IDs on every requisition. |
| A commitment is approved beyond authority | Financial approval within the approver's matrix limit (§2, matrix) | Resolve the financial approver's job title through the staff file to the matrix; compare the limit with the estimated cost. |
| Approvals are not evidenced | Verification, procurement approval and financial approval are each recorded with identity and date (§2) | Test the six approval fields for blanks. |
| Approved demand is never closed | Every approved requisition is converted to a PO or withdrawn (§3) | Requisitions in Approved status with no PO number. |
| Orders and payments go to unapproved vendors | Only Active vendors are ordered from and paid (§3, §5) | Join requisitions, POs and invoices to the vendor master on status. |
| Orders depart from what was approved | The PO is dated on or after financial approval and preserves the approved vendor, quantity and value (§3) | Compare PO date, vendor and value with the requisition; compare unit prices for the same item across POs. |
| Goods are received improperly | The GRN is completed and signed by the receiving department, never the vendor, and dated on or after the PO (§4) | Inspect the GRN document; compare GRN date with PO date. |
| Invoices are paid without a three-way match | The invoice is matched to the PO and the GRN; no invoice without a GRN reference is settled; the amount does not exceed the PO total (§5) | Blank GRN links; invoice amount against PO total. |
| Invoices are paid without authority | Verified by Finance and separately approved by a designated Financial Authority within limit (§5) | Blank approvals on paid invoices; approver limit against invoice amount; verifier against approver. |
| Payment precedes delivery or the invoice | Received date on or after the invoice date; payment only after the goods receipt (§5) | Compare invoice, received, GRN and payment dates. |

## Expected exception ground truth

Three classes. A ▸ marks a record that also has documents in the sample pack.

### Class 1 - visible in the tables

| Ref | Exception | SOP | Severity | Records | Value (PKR) |
| --- | --- | --- | --- | --- | ---: |
| P-01 | Requisition verified by the person who raised it | §2 | High | REQ2024108, REQ2024120, REQ2024134, REQ2024149 | 91,545,000 |
| P-02 | Requisition financially approved above the approver's limit | §2, matrix | High | REQ2024112, REQ2024127, REQ2024145 | 71,200,000 |
| P-03 | Procurement approval not recorded, yet the requisition was completed and ordered | §2 | Moderate | REQ2024116, REQ2024130, REQ2024152 | 22,770,000 |
| P-04 | Approved requisition neither converted to a PO nor withdrawn | §3 | Moderate | REQ2024124, REQ2024138, REQ2024153 | 35,390,000 |
| P-05 | Vendor not Active, yet ordered from and paid | §3, §5 | High | V1010 *Under Review*: REQ/PO/INV2024126; V1017 *Inactive*: REQ/PO/INV2024150 | 6,200,000 |
| P-06 | Invoice settled with no goods receipt reference | §5 | High | INV2024110, INV2024132, INV2024147 | 95,150,000 |
| P-07 | Invoice amount above the PO total | §5 | High | INV2024106, INV2024140, INV2024122 | 12,553,800 excess |
| P-08 | Invoice paid with no approval recorded | §5 | High | INV2024104, INV2024131, INV2024154 | 50,086,000 |
| P-09 | Invoice approved above the approver's limit | §5, matrix | High | INV2024127, INV2024140 | 5,256,000 |
| P-10 | Payment released before the goods receipt | §5 | High | INV2024114, INV2024129, INV2024143 | 76,440,000 |
| P-11 | Unit price far above every other order for the same item | §3 | High | PO2024127 | 3,000,000 |
| P-12 | Invoice dated before its purchase order | §3 | Moderate | INV2024004 ▸ | 2,000,000 |
| P-13 | Invoice recorded as received before its own invoice date | §5 | Low | INV2024118, INV2024136 | 11,640,000 |
| P-14 | Invoice dated before the goods were received | §5 (indicator) | Moderate | INV2024004 ▸, INV2024114, INV2024129, INV2024143 | 78,440,000 |

- **P-01:** Requesters 1042 (twice), 1053 and 1051 verified their own requisitions. REQ2024134 is a PKR 74,000,000 branch fit-out; see J-02.
- **P-02:** The Head of Treasury (limit 1,000,000) approved REQ2024112 at 4,200,000 and REQ2024127 at 3,000,000. The CFO (limit 10,000,000) approved REQ2024145 at 64,000,000. Finding these requires resolving the approver ID to a job title through the staff file before the matrix applies.
- **P-03:** The approver ID and the approval date are both blank. All three requisitions are nonetheless Completed, financially approved and ordered.
- **P-04:** REQ2024138 alone is PKR 34,000,000. All three are still in Approved status, with no PO and no recorded withdrawal.
- **P-05:** The exception runs through the whole chain: the requisition proposed the vendor, the PO was placed with it, and the invoice was paid. The value shown is the payments.
- **P-06:** Each PO does record a GRN, so the goods may well have arrived. The failure is that the invoice was paid without being matched to it. INV2024110, at PKR 85,000,000, is the largest single exception in the pack.
- **P-07:** INV2024106 is 43,200,000 against a 32,000,000 PO, an excess of 11,200,000 (35%). INV2024140 is 2,256,000 against 940,000, an excess of 1,316,000 (140%). INV2024122 is 247,800 against 210,000, an excess of 37,800 (18%). The SOP requires any excess to be referred back to Procurement, and nothing records a referral.
- **P-08:** The supervisor approver and the approval date are both blank on all three, and all three were paid. INV2024154 is PKR 40,800,000.
- **P-09:** The Head of Treasury (limit 1,000,000) approved both. INV2024140 is also a P-07 excess.
- **P-10:** INV2024114 was paid 3 March against a GRN dated 11 March. INV2024129, PKR 74,000,000, was paid 29 October against a GRN dated 11 November. INV2024143 was paid 22 April against a GRN dated 1 May.
- **P-11:** "New Hire Onboarding Kits" cost PKR 80,000 a unit on three POs: PO2024004, PO2024109 and PO2024137. PO2024127 ordered one kit for 3,000,000, 37.5 times that price. Every other item in the PO file has a single unit price. This one chain, REQ2024127 → PO2024127 → INV2024127, also breaches the approval limit twice (P-02 and P-09). See *The headline finding*.
- **P-12:** The vendor invoice is dated 9 April 2024. The PO it bills was issued on 19 April. The sample pack shows the same dates on the documents.
- **P-13:** INV2024118 is recorded as received 11 August against an invoice dated 13 August. INV2024136 is recorded as received 27 January against an invoice dated 30 January. Either the received date is wrong or the invoice was re-dated.
- **P-14:** Billing before delivery. It is not a separate SOP breach, but three of the four go on to P-10. Scored on the received date instead of the invoice date, INV2024136 also qualifies, by one day, which makes 5 records.

### Class 2 - visible only in the documents

Pack 01 is REQ2024009 → PO2024004 → GRN2024004 → INV2024004. Pack 02 is
REQ2024105 → PO2024105 → GRN2024105 → INV2024105.

| Ref | Exception | SOP | Severity | Document |
| --- | --- | --- | --- | --- |
| D-01 | Goods received and inspected by **Ethan Smith (1041)**, the Requisition & PO Associate who raised the requisition | §3, §4 | Moderate | GRN2024004_Signed_Receipt.pdf |
| D-02 | Payment authorised by **Olivia Smith (1010)**, the invoice verifier, and **Max Baker (1023)**, a Sourcing Manager with no financial authority; the recorded approver did not sign, and neither signature is dated | §5 | High | INV2024004_Signed_Payment_Voucher.pdf |
| D-03 | Requisition names the requesting department as Finance; the requester, Ethan Smith (1041), is a Procurement associate whose other nine requisitions all say Procurement | §2 | Low | REQ2024009_Purchase_Requisition.pdf |
| D-04 | POs record buyer "B001", which matches no staff record; the buyer cannot be identified | §3 | Low | PO2024004_Purchase_Order.pdf, PO2024105_Purchase_Order.pdf |
| D-05 | Goods receipt records **17** laptops received against **20** ordered; the invoice billed and the voucher paid for all 20, so PKR 435,000 was paid for 3 laptops never received | §4, §5 | High | GRN2024105_Signed_Receipt.pdf, against the PO, invoice and voucher |

- **The signatures are part of the text.** Each signature line carries the signer's name above its caption, and the RECEIVED and PAID stamps are text too, so D-01 and D-02 can be reached by reading. Earlier editions of pack 01's GRN and voucher were scans whose OCR text layer dropped "Ethan Smith (1041)" entirely, garbled "Ahmed Khan" and the PO number, and left the voucher reading "Signed Signed"; runs read from that layer reported the vendor's representative as the receiver. A run scored on those editions could not reach D-01 or D-02, and should be re-run before it is scored on them.
- **D-01:** Ahmed Khan, the vendor's contact on the PO, signs only to acknowledge delivery, which is normal. The receipt itself was certified by the person who raised the requisition, a Requisition & PO Associate on the ordering side of Procurement. SOP §4 gives inspection and receipt to the requisitioning department and §3 gives ordering to Procurement, so ordering and receiving were not segregated. The requisition also names Finance as the requesting department (D-03), so who should have received the goods is itself unclear.
- **D-02:** The invoice table records INV2024004 as verified by 1010 and approved by 1003, Daniel Reed, the Financial Controller. The voucher that released PKR 2,000,000 carries Olivia Smith (the verifier) and Max Baker, the Sourcing Manager who gave procurement approval on the requisition. Max Baker has no limit in the approval matrix, and the recorded approver's signature is absent. When either signature was given cannot be established.
- **D-03** is the one requisition in the file whose department contradicts the requester's other records. The requisition table carries the same "Finance" value.
- **D-04:** The tables have no buyer field at all, so this can only be seen on the documents. Both POs name the same code, so it is one matter.
- **D-05** is the one exception in pack 02. The PO table records the GRN's number, date and status ("Received") but no received quantity, so nothing in `data/` shows the shortfall. Only the GRN states 17. The PO and the requisition say 20; the invoice bills, and the voucher pays, PKR 2,900,000, which is 20 × 145,000. SOP §5 requires the invoice to be matched to the GRN before approval, and a match would have found 3 laptops (PKR 435,000) billed but not delivered. The inspection comment, "Laptops received and inspected.", does not mention the short delivery.

### Class 3 - judgement items

These need inquiry rather than a conclusion. A run that raises them as matters for follow-up has done well. A run that states them as confirmed exceptions has overreached.

| Ref | Matter | Records |
| --- | --- | --- |
| J-01 | Requisition verification is done by Finance staff (Senior Accountants and Invoice Processing Specialists) on 51 of 55 requisitions. SOP §2 assigns verification to the Procurement Team. The other 4 were verified by their own requesters (P-01). | Population-wide |
| J-02 | "Branch fit-out — Gulberg" was ordered three times, for PKR 157,250,000 in total. PO2024134 (January, V1025 Horizon Learning Partners, 74,000,000, self-verified by its requester) and PO2024129 (October, V1013 BlueRiver Consulting, 74,000,000, paid before its GRN) are the same quantity at the same value from two vendors, neither of which is a fit-out contractor by name. PO2024104 (August, V1028, 9,250,000) is the third. | REQ/PO/INV2024134, …129, …104 |
| J-03 | The approval matrix lists 4 job titles. The two Sourcing Managers who give procurement approval on every requisition have no documented authority. The matrix governs financial approval only, so this is a gap in the delegation framework rather than a breach. | Staff 1023, 1024 |

## Tested and clean

These were tested and are not exceptions. A run that reports them as findings has a false positive.

- No procurement approver is ever the requester or the verifier. The approvers are 1023 and 1024, who never raise or verify a requisition.
- No invoice verifier is ever the invoice approver.
- No PO predates its financial approval, every PO preserves the approved vendor and value, every PO rests on an approved requisition, and no GRN predates its PO.
- No payment predates its invoice date, received date, verification or approval. No invoice was paid after its due date.
- No duplicate vendor invoice numbers, and no vendor billed the same amount twice. Some different vendors bill identical amounts, but only because every item has a fixed unit price.
- No requisition splitting. The two same-requester, same-vendor pairs within 30 days differ widely in value and straddle no limit.
- 46 of 52 invoices were approved by the same person who financially approved the requisition. SOP §5 asks for a designated Financial Authority and does not prohibit this.
- Invoice approvers mostly sit outside the verifier's reporting line. The SOP does not require approval within the verifier's line.
- Vendor invoice number VINV001-202404 (INV2024004) has a different format from the other 51 (VINV1nn-2024). It is the sample-pack invoice, and the format is not an exception.
- 16 of 20 staff job titles are not in the approval matrix. Only financial approvers need to be, and all four that approve are (see J-03).
- Pack 02 agrees with the tables in every other field: dates, people, vendor, item, ordered quantity, unit price and amounts. Its GRN was received and inspected by Hina Raza (1056), the requester in the requisitioning department, as SOP §4 requires. Its voucher is signed by Omar Farooq (1013) and Daniel Reed (1003), the invoice's recorded verifier and approver, and Daniel Reed's PKR 5,000,000 limit covers PKR 2,900,000.
- In both packs the vendor, references, item, unit price and amounts agree across the requisition, PO, invoice and voucher, apart from D-05's quantity.

## The headline finding

**One transaction chain defeats three controls.** REQ2024127 asked for one New
Hire Onboarding Kit at PKR 3,000,000, against PKR 80,000 a kit on every other
order. The Head of Treasury, whose limit is PKR 1,000,000, approved the
requisition (P-02). PO2024127 was raised at the same price (P-11). INV2024127
was then approved for payment by the same Head of Treasury, again above that
limit (P-09).

Each control failed separately, and no exception review joined them up. That is
exactly what the Head of Procurement admitted at the planning meeting.

The largest single value at risk is INV2024110: PKR 85,000,000 paid with no
goods receipt match (P-06).

## Scoring a run

Score each Class 1 and Class 2 item on the run's audit output (findings, then
the report), not on its analyses or planning memorandum alone.

| Outcome | Meaning |
| --- | --- |
| Captured | A finding names the records (or the pattern, for J-items) and the report carries it |
| Partial | Raised, but with the population or value wrong, or only some records |
| Surfaced only | Found by an analysis or mentioned in the memorandum, but never reached a finding |
| Missed | Absent from every artifact |
| False positive | A "tested and clean" item reported as a finding |

There are 14 table items and 5 document items, 19 in all, plus 3 judgement items
that are scored as raised or not raised. Values overlap across items (the
127 chain appears three times), so do not sum the value column.
