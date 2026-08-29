# Facilitator guide - Meridian Bank treasury dealing audit

Everything below is the answer key. It is not in the data files and not in the documents: no exception flag, no marker column, no filename hint.

## Suggested story

Meridian Bank Limited runs a treasury dealing room covering interbank foreign exchange, money market placements and borrowings, and treasury bills. Dealing volume grew sharply in the first half of 2025 while the desk ran one dealer short. The Board Audit Committee asked Internal Audit to assess whether deals were struck within authority and at market rates, confirmed independently, and settled to the right account on the right day.

## Compact audit scope

- Period: 1 January to 30 June 2025.
- Population: 1,000 deals, PKR 158,041,244,380 in aggregate notional.
- Objective: assess operating effectiveness of dealer and counterparty limit controls, rate reasonableness, segregation of duties across the front, middle and back office, confirmation timeliness and completeness, and settlement routing and timing.
- Documents: the treasury policy, the limit matrix, the planning minutes, and 18 deal packs. A pack is a folder under `documents/deal-packs/`, holding the dealing ticket, the broker note where the deal was brokered, the counterparty confirmation, the settlement payment instruction and the nostro statement extract as separate files - 81 documents in all.

## Recommended RCM controls

| Risk | Control | Test approach |
| --- | --- | --- |
| Deals are struck beyond the authority of the dealer | Per-deal and per-day dealer limits, and product authorisations, are enforced at capture (policy 5.3) | Join deals to the dealer limit file; test each deal and each dealer-day aggregate. |
| Exposure to a counterparty exceeds its approved limit | Middle office monitors aggregate outstanding exposure daily (policy 4.2) | Build a daily exposure profile per counterparty from value date to maturity; compare the peak to the limit. |
| The bank deals with a blocked or lapsed counterparty | Dealing is restricted to counterparties that are Active with a current limit (policy 5.1) | Compare deal date to counterparty status effective date and limit expiry date. |
| Deals are struck away from the market to a counterparty's benefit | Dealt rates are evidenced against the independent market rate file (policy 5.5, 6.3) | Compute basis-point deviation from the published mid for the deal's instrument and tenor; test against the 25bp and 15bp tolerances, then aggregate the breaches by dealer and counterparty. |
| One person controls a deal end to end | Dealing, confirmation and settlement are segregated (policy 4.1, 4.3) | Compare captured-by to confirmed-by; released-by to approved-by; and resolve every one of them to a desk on the staff file. |
| Deals are struck outside controlled conditions | Dealing hours and a 15-minute capture deadline (policy 5.2, 5.4) | Test deal time against the permitted window, deal date against the business calendar, and capture timestamp against deal time. |
| Terms are settled that the counterparty never agreed | Independent confirmation despatched within one business day and matched before settlement (policy 7.2, 7.3, 7.4) | Test despatch lag, received date against value date, match status against settlement, and deals with no confirmation at all. |
| Funds are released to the wrong account or at the wrong time | Settlement only to an approved standing instruction, on the value date, at the contracted amount (policy 8.1 to 8.5) | Test settlement date against value date, SSI reference against the SSI file, amendment cooling-off, and settlement amount recomputed from notional and rate. |
| Deals are altered or cancelled after the fact | Amendment and cancellation require operations approval (policy 5.6) | Test amended and cancelled deals for an approver, and amendment date against the confirmation date. |

## Expected exception ground truth

Three classes, and the distinction is the point of the sample.

**Class 1 - visible in the tables.** The analytics find these without any document. A ▸ marks a deal reference that also has a document pack, so a tables-detected exception can be vouched.

### Limits and dealing authority

| Ref | Exception | Severity | Deals |
| --- | --- | --- | --- |
| X01 | Deal notional exceeds the dealer's per-deal limit | High | `TD-2025-0171` ▸, `TD-2025-0532` |
| X02 | Dealer's aggregate dealing on one day exceeds the daily limit | High | `TD-2025-0395`, `TD-2025-0396`, `TD-2025-0398`, `TD-2025-0402`, `TD-2025-0404` |
| X03 | Counterparty exposure limit breached in aggregate | High | `TD-2025-0267`, `TD-2025-0270`, `TD-2025-0271`, `TD-2025-0272` |
| X04a | Deal executed with a suspended counterparty | High | `TD-2025-0475` ▸, `TD-2025-0467` |
| X04b | Deal executed after the counterparty's limit had expired | Moderate | `TD-2025-0677`, `TD-2025-0679` |
| X05a | Dealer transacted a product outside their authorisation | High | `TD-2025-0207`, `TD-2025-0222` |
| X05b | Dealer transacted after their dealing authorisation had lapsed | High | `TD-2025-0890`, `TD-2025-0889`, `TD-2025-0897` |
| X06 | Deals struck by a dealer with no row in the limit file | Moderate | population-wide |

- **X01** - TS-005 dealt PKR 142m against a PKR 100m per-deal limit; TS-008 dealt PKR 231m against a PKR 150m limit.
- **X02** - TS-005 dealt five tickets of ~PKR 95m on 12 March 2025 - PKR 475m against a PKR 400m daily limit. Every ticket passes the per-deal test on its own.
- **X03** - CP-005's book already stood at PKR 1,223,269,550 against a PKR 1,500,000,000 limit, or 82%. Four further placements struck on 19 February 2025, together PKR 381,730,450, carry it past the limit for the life of the tenor. Each of the four is around 6% of the limit and none is close to any per-deal threshold, so the breach exists only in the aggregate and only on a daily exposure profile. Thirteen deals in total are outstanding on the breached days.
- **X04a** - CP-008 Northgate Bank was suspended with effect from 14 March 2025; both deals were struck after that date.
- **X04b** - CP-007's approved limit expired on 30 April 2025 and was not renewed.
- **X05a** - TS-005 Bilal Ahmed is authorised for FX_SPOT only; both tickets are FX forwards.
- **X05b** - TS-010 Sana Tariq's authorisation expired on 31 May 2025; three June tickets follow it.
- **X06** - TS-009 Kamran Yousuf executed 61 T-bill deals in the period. The dealer limit file has no row for TS-009, so no deal of his can be tested for authority at all. This is an inconclusive result, not a pass and not a failure.

### Rate reasonableness

| Ref | Exception | Severity | Deals |
| --- | --- | --- | --- |
| X07 | Dealt rate outside the permitted deviation from the market mid | High | `TD-2025-0096` ▸, `TD-2025-0095`, `TD-2025-0525`, `TD-2025-0671` |

- **X07** - Two of the four are the same dealer (TS-005) with the same counterparty (CP-003), which is the pattern rather than the individual deal.

### Segregation of duties

| Ref | Exception | Severity | Deals |
| --- | --- | --- | --- |
| X08a | Deal captured and confirmed by the same person | High | `TD-2025-0167`, `TD-2025-0168` |
| X08b | Settlement released and approved by the same person | High | `TD-2025-0205`, `TD-2025-0210` |
| X08c | Settlement approved by the dealer who struck the deal | High | `TD-2025-0520` |
| X08d | Confirmation signed off by a front-office member of staff | High | `TD-2025-0339`, `TD-2025-0332` |

- **X08a** - The dealer who struck and captured the deal also signed off the counterparty confirmation.
- **X08b** - No second pair of eyes on release.
- **X08c** - Front office authorising the movement of its own funds.
- **X08d** - The confirmer is not the dealer, so an identity test passes; the staff file places them on the dealing desk.

### Dealing hours and capture discipline

| Ref | Exception | Severity | Deals |
| --- | --- | --- | --- |
| X09a | Deal struck outside authorised dealing hours | Moderate | `TD-2025-0001`, `TD-2025-0006`, `TD-2025-0007` |
| X09b | Deal struck on a non-business day | Moderate | `TD-2025-0571` |
| X09c | Deal captured well after execution | Low | `TD-2025-0004`, `TD-2025-0002`, `TD-2025-0005` |

- **X09a** - Policy clause 5.2 permits dealing between 09:00 and 17:00 only.
- **X09b** - 12 April 2025 is a Saturday; the deal carries a market rate the file does not publish for that date.
- **X09c** - Capture lags execution by 47, 96 and 214 minutes against a 15-minute policy deadline.

### Confirmation

| Ref | Exception | Severity | Deals |
| --- | --- | --- | --- |
| X10a | Confirmation despatched outside the one-business-day deadline | Moderate | `TD-2025-0099`, `TD-2025-0104`, `TD-2025-0110`, `TD-2025-0118` |
| X10b | Deal settled before the counterparty confirmation was received | High | `TD-2025-0284` ▸, `TD-2025-0281`, `TD-2025-0283` |
| X10c | Deal settled while its confirmation was flagged as discrepant | High | `TD-2025-0384`, `TD-2025-0386`, `TD-2025-0391` |
| X13 | No confirmation record exists for a settled deal | High | `TD-2025-0183`, `TD-2025-0181`, `TD-2025-0180` |

- **X10a** - Despatched three to six business days after the deal.
- **X10b** - Funds moved on an unconfirmed deal; the confirmation arrived two business days after value date.
- **X10c** - The discrepancy was never cleared and the settlement went ahead anyway.
- **X13** - The deal settled with nothing on file to show the counterparty agreed its terms.

### Settlement

| Ref | Exception | Severity | Deals |
| --- | --- | --- | --- |
| X11 | Settlement did not fall on the contracted value date | Moderate | `TD-2025-0131`, `TD-2025-0130`, `TD-2025-0135` |
| X14a | Deal marked settled with no settlement record | Moderate | `TD-2025-0474`, `TD-2025-0463` |
| X14b | Settlement recorded against a deal that does not exist | High | `TD-2025-0884` |
| X17a | Settlement routed to an account with no standing instruction | High | `TD-2025-0541`, `TD-2025-0534` |
| X17b | Settlement made inside the standing-instruction cooling-off period | High | `TD-2025-0707` |
| X18 | Settlement amount does not agree to notional multiplied by the dealt rate | High | `TD-2025-0331`, `TD-2025-0338` |

- **X11** - Two settled late by two and five business days; one settled a business day early.
- **X14a** - Nothing in the settlement file evidences that the funds moved.
- **X14b** - PKR 185,000,000 released on 27 May 2025 against deal reference TD-2025-0884, which the deal file does not contain.
- **X17a** - Both payments went to accounts absent from the SSI file, and both beneficiary banks differ from the counterparty's.
- **X17b** - CP-006's PKR instruction was amended with effect from 2025-05-09 and paid on 2025-05-12, one business day later, against a two-business-day cooling-off requirement.
- **X18** - One overpaid by 1.8% and one underpaid by 0.9% against the contracted terms.

### Amendment, cancellation and reference integrity

| Ref | Exception | Severity | Deals |
| --- | --- | --- | --- |
| X12 | Deal amended after the counterparty confirmation was received, without approval | High | `TD-2025-0362`, `TD-2025-0359` |
| X19 | Deal cancelled without the required operations approval | Moderate | `TD-2025-0252`, `TD-2025-0250` |
| X15 | Near-duplicate deals booked on the same day | Moderate | `TD-2025-0334`, `TD-2025-0340` |
| X16a | Gaps in the deal reference sequence | Moderate | `TD-2025-0447` ▸, `TD-2025-0612`, `TD-2025-0661`, `TD-2025-0884` |
| X16b | Deal reference does not follow the standard format | Low | `TD-25-733` |

- **X12** - The amendment post-dates the matched confirmation and carries no operations approver.
- **X19** - Eight deals were cancelled in the period; six carry an operations approver and these two carry none.
- **X15** - Same counterparty, notional, rate and value date, booked 90 minutes apart. Both settle.
- **X16a** - Four references are absent. TD-2025-0884 is the reference a settlement was released against; TD-2025-0447 is the deal a complete document pack exists for; TD-2025-0661 is empty because that deal was booked as TD-25-733; TD-2025-0612 is unexplained.
- **X16b** - Booked as TD-25-733 where every other reference in the file is TD-2025-nnnn. It sits in the sequence where TD-2025-0661 would fall.

**Class 2 - visible only in the documents.** Every one of these deals is clean in the tables. No data test will ever raise them; only reading the pack does.

| Ref | Exception | Severity | Deal pack | Document to open |
| --- | --- | --- | --- | --- |
| D1 | Deal ticket carries no supervisory authorisation | Moderate | `01_TD-2025-0094` | dealing ticket |
| D2 | Rate on the deal ticket was altered and the alteration is not initialled | High | `07_TD-2025-0165` | dealing ticket |
| D3 | The counterparty confirmation is an internally produced document | High | `18_TD-2025-0518` | counterparty confirmation |
| D4 | Broker note absent from the pack for a brokered deal | Low | `15_TD-2025-0420` | the broker note is absent |
| D5 | Payment instruction released under a single signature above the dual-signature threshold | High | `09_TD-2025-0169` | payment instruction |
| D6 | Supervisory authorisation on the deal ticket post-dates execution | Moderate | `11_TD-2025-0263` | dealing ticket |

- **D1** - The dealer signed; the authorisation block is blank. The system record shows nothing wrong.
- **D2** - The ticket shows a struck-through rate over-written by hand. The booked rate matches the over-written figure, so no data test sees it.
- **D3** - The confirmation in the pack is a Meridian Bank internal print with no counterparty letterhead, no SWIFT header and no counterparty reference. The confirmation table records it as Matched.
- **D4** - The deal records broker BR-001; the pack contains no broker note and brokerage was paid.
- **D5** - PKR 267,000,000 against a PKR 250m threshold. The settlement record names a second approver who did not sign the instruction.
- **D6** - The ticket was authorised two business days after the deal was struck and after the confirmation had been despatched.

**Class 3 - the paper contradicts the system.** Each of these is clean in the tables and internally consistent on the face of each document. The exception exists only in the disagreement between the two, which is the case for vouching at all.

| Ref | Exception | Severity | Deal pack | Documents to read together |
| --- | --- | --- | --- | --- |
| C1 | Deal booked against a different counterparty from the one it was struck with | High | `14_TD-2025-0341` | confirmation against the deal record and the exposure profile |
| C2 | Rate on the counterparty confirmation differs from the rate booked | High | `08_TD-2025-0166` | confirmation against the deal record and the market rate file |
| C3 | Notional on the counterparty confirmation differs from the notional booked | High | `13_TD-2025-0337` | confirmation against the deal record and the settlement |
| C4 | A complete document pack exists for a deal the system never recorded | High | `16_TD-2025-0447` | the whole folder against the deal, confirmation and settlement files |

- **C1** - Booked to CP-004 Horizon Bank. The counterparty confirmation in the pack is on Cedar Commercial Bank (CP-009) paper and carries Cedar's reference. Recorded CP-009 exposure peaks at PKR 922,000,000 against a limit of PKR 1,000,000,000; adding this PKR 120,000,000 deal takes it to PKR 1,042,000,000, or 104% of the limit. Invisible to every data test and to either document read alone.
- **C2** - Booked at 279.5132, which is 1bp from the published mid of 279.4966 and so inside the 25bp policy band. The counterparty confirms 282.1686, which is 96bp from the same mid. The rate reasonableness test passes because it reads the recorded rate, not the confirmed one.
- **C3** - Booked at PKR 205,000,000; the counterparty confirms PKR 230,000,000. The PKR 25m difference is neither settled nor recorded.
- **C4** - Deal ticket, broker note, counterparty confirmation, payment instruction and a nostro extract showing the funds leaving all exist for TD-2025-0447. The deal file skips that reference, no confirmation and no settlement row carries it, and the confirmation, instruction and settlement references on the paper are manual-series (CNF-2025-M041, PMT-2025-M0114, STL-2025-M014) that appear nowhere in the populations.

## The headline finding

`TD-2025-0341` is the finding worth building the walkthrough around. The deal file books a PKR 120,000,000 money market placement to Horizon Bank Limited (CP-004), which has ample headroom. The counterparty confirmation in the pack is on Cedar Commercial Bank paper, under Cedar's SWIFT address and Cedar's own reference.

Cedar (CP-009) carries a PKR 1,000,000,000 limit. On 2025-03-06 the recorded book against Cedar peaks at PKR 922,000,000, or 92% of the limit - inside it, so the exposure test passes. Add the deal the confirmation says was actually struck with Cedar and the position becomes PKR 1,042,000,000, or 104%.

Nothing in the populations shows it. Nothing in any single document shows it. It appears only when the confirmation is read against the deal record, and it matters only when the reclassified exposure is recomputed against the limit. That is a three-step finding, and it is the one to promote.

## Demo pacing

1. Import the ten CSVs and let the exploratory analysis run. The limit, rate and segregation exceptions should surface without prompting.
2. Note what the analysis cannot settle: TS-009 has no row in the dealer limit file, so 61 T-bill deals are untestable for authority. That is an inconclusive result and should be reported as one, not as a pass.
3. Open two or three class-1 packs (`02_TD-2025-0096`, `10_TD-2025-0171`, `17_TD-2025-0475`) to show the paper corroborating what the data already found.
4. Open the class-2 packs (`01_TD-2025-0094`, `18_TD-2025-0518`, `09_TD-2025-0169`) against clean table rows, to show what the analytics structurally cannot reach.
5. Walk `14_TD-2025-0341` end to end, then `16_TD-2025-0447` - a complete pack, funds out of the nostro, and no deal record anywhere in the file.
6. Promote the counterparty substitution, the settled-unconfirmed deals and the off-market dealing into findings; generate the working paper and the report draft.

## Counting

- 41 seeded exception types in all.
- 31 are visible in the tables, touching 72 deal references out of 1,000.
- 6 are visible only in the documents.
- 4 are contradictions between the two.
- 18 deal packs holding 81 documents, of which 4 packs are clean: `03_TD-2025-0098`, `04_TD-2025-0102`, `06_TD-2025-0115`, `05_TD-2025-0112`.
