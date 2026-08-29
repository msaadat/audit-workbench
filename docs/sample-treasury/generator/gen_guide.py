"""Emit the facilitator guide from ground_truth.json so the expected results
and the files can never drift apart."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GT = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text())

TRUTH = GT["ground_truth"]
PACKS = GT["packs"]
EXTRA = GT["pack_extras"]
PACK_BY_DEAL = {v: k for k, v in PACKS.items()}

ORDER = ["X01", "X02", "X03", "X04a", "X04b", "X05a", "X05b", "X06", "X07",
         "X08a", "X08b", "X08c", "X08d", "X09a", "X09b", "X09c", "X10a",
         "X10b", "X10c", "X11", "X12", "X13", "X14a", "X14b", "X15", "X16a",
         "X16b", "X17a", "X17b", "X18", "X19"]

GROUPS = [
    ("Limits and dealing authority", ["X01", "X02", "X03", "X04a", "X04b",
                                      "X05a", "X05b", "X06"]),
    ("Rate reasonableness", ["X07"]),
    ("Segregation of duties", ["X08a", "X08b", "X08c", "X08d"]),
    ("Dealing hours and capture discipline", ["X09a", "X09b", "X09c"]),
    ("Confirmation", ["X10a", "X10b", "X10c", "X13"]),
    ("Settlement", ["X11", "X14a", "X14b", "X17a", "X17b", "X18"]),
    ("Amendment, cancellation and reference integrity",
     ["X12", "X19", "X15", "X16a", "X16b"]),
]


def refs(code: str) -> str:
    ids = TRUTH[code]["deal_ids"]
    if not ids:
        return "population-wide"
    marked = [f"`{i}`" + (" ▸" if i in PACK_BY_DEAL else "") for i in ids]
    return ", ".join(marked)


lines: list[str] = []
w = lines.append

w("# Facilitator guide - Meridian Bank treasury dealing audit")
w("")
w("Everything below is the answer key. It is not in the data files and not in "
  "the documents: no exception flag, no marker column, no filename hint.")
w("")
w("## Suggested story")
w("")
w("Meridian Bank Limited runs a treasury dealing room covering interbank "
  "foreign exchange, money market placements and borrowings, and treasury "
  "bills. Dealing volume grew sharply in the first half of 2025 while the desk "
  "ran one dealer short. The Board Audit Committee asked Internal Audit to "
  "assess whether deals were struck within authority and at market rates, "
  "confirmed independently, and settled to the right account on the right day.")
w("")
w("## Compact audit scope")
w("")
w("- Period: 1 January to 30 June 2025.")
w(f"- Population: {GT['deal_count']:,} deals, PKR "
  f"{sum(float(d['NOTIONAL_PKR']) for d in GT['deals'].values()):,.0f} in "
  f"aggregate notional.")
w("- Objective: assess operating effectiveness of dealer and counterparty "
  "limit controls, rate reasonableness, segregation of duties across the "
  "front, middle and back office, confirmation timeliness and completeness, "
  "and settlement routing and timing.")
w(f"- Documents: the treasury policy, the limit matrix, the planning minutes, "
  f"and {len(PACKS)} deal packs.")
w("")
w("## Recommended RCM controls")
w("")
w("| Risk | Control | Test approach |")
w("| --- | --- | --- |")
for risk, control, approach in [
    ("Deals are struck beyond the authority of the dealer",
     "Per-deal and per-day dealer limits, and product authorisations, are "
     "enforced at capture (policy 5.3)",
     "Join deals to the dealer limit file; test each deal and each "
     "dealer-day aggregate."),
    ("Exposure to a counterparty exceeds its approved limit",
     "Middle office monitors aggregate outstanding exposure daily (policy 4.2)",
     "Build a daily exposure profile per counterparty from value date to "
     "maturity; compare the peak to the limit."),
    ("The bank deals with a blocked or lapsed counterparty",
     "Dealing is restricted to counterparties that are Active with a current "
     "limit (policy 5.1)",
     "Compare deal date to counterparty status effective date and limit "
     "expiry date."),
    ("Deals are struck away from the market to a counterparty's benefit",
     "Dealt rates are evidenced against the independent market rate file "
     "(policy 5.5, 6.3)",
     "Compute basis-point deviation from the published mid for the deal's "
     "instrument and tenor; test against the 25bp and 15bp tolerances, then "
     "aggregate the breaches by dealer and counterparty."),
    ("One person controls a deal end to end",
     "Dealing, confirmation and settlement are segregated (policy 4.1, 4.3)",
     "Compare captured-by to confirmed-by; released-by to approved-by; and "
     "resolve every one of them to a desk on the staff file."),
    ("Deals are struck outside controlled conditions",
     "Dealing hours and a 15-minute capture deadline (policy 5.2, 5.4)",
     "Test deal time against the permitted window, deal date against the "
     "business calendar, and capture timestamp against deal time."),
    ("Terms are settled that the counterparty never agreed",
     "Independent confirmation despatched within one business day and matched "
     "before settlement (policy 7.2, 7.3, 7.4)",
     "Test despatch lag, received date against value date, match status "
     "against settlement, and deals with no confirmation at all."),
    ("Funds are released to the wrong account or at the wrong time",
     "Settlement only to an approved standing instruction, on the value date, "
     "at the contracted amount (policy 8.1 to 8.5)",
     "Test settlement date against value date, SSI reference against the SSI "
     "file, amendment cooling-off, and settlement amount recomputed from "
     "notional and rate."),
    ("Deals are altered or cancelled after the fact",
     "Amendment and cancellation require operations approval (policy 5.6)",
     "Test amended and cancelled deals for an approver, and amendment date "
     "against the confirmation date."),
]:
    w(f"| {risk} | {control} | {approach} |")
w("")
w("## Expected exception ground truth")
w("")
w("Three classes, and the distinction is the point of the sample.")
w("")
w("**Class 1 - visible in the tables.** The analytics find these without any "
  "document. A ▸ marks a deal reference that also has a document pack, so "
  "a tables-detected exception can be vouched.")
w("")
for group, codes in GROUPS:
    w(f"### {group}")
    w("")
    w("| Ref | Exception | Severity | Deals |")
    w("| --- | --- | --- | --- |")
    for code in codes:
        entry = TRUTH[code]
        w(f"| {code} | {entry['title']} | {entry['severity']} | {refs(code)} |")
    w("")
    for code in codes:
        w(f"- **{code}** - {TRUTH[code]['detail']}")
    w("")

w("**Class 2 - visible only in the documents.** Every one of these deals is "
  "clean in the tables. No data test will ever raise them; only reading the "
  "pack does.")
w("")
w("| Ref | Exception | Severity | Deal pack |")
w("| --- | --- | --- | --- |")
for code in ["D1", "D2", "D3", "D4", "D5", "D6"]:
    entry = TRUTH[code]
    w(f"| {code} | {entry['title']} | {entry['severity']} | "
      f"`{entry['deal_ids'][0]}` |")
w("")
for code in ["D1", "D2", "D3", "D4", "D5", "D6"]:
    w(f"- **{code}** - {TRUTH[code]['detail']}")
w("")

w("**Class 3 - the paper contradicts the system.** Each of these is clean in "
  "the tables and internally consistent on the face of each document. The "
  "exception exists only in the disagreement between the two, which is the "
  "case for vouching at all.")
w("")
w("| Ref | Exception | Severity | Deal pack |")
w("| --- | --- | --- | --- |")
for code in ["C1", "C2", "C3", "C4"]:
    entry = TRUTH[code]
    w(f"| {code} | {entry['title']} | {entry['severity']} | "
      f"`{entry['deal_ids'][0]}` |")
w("")
for code in ["C1", "C2", "C3", "C4"]:
    w(f"- **{code}** - {TRUTH[code]['detail']}")
w("")

w("## The headline finding")
w("")
c1_deal = TRUTH["C1"]["deal_ids"][0]
w(f"`{c1_deal}` is the finding worth building the walkthrough around. The deal "
  f"file books a PKR "
  f"{float(GT['deals'][c1_deal]['NOTIONAL_PKR']):,.0f} money market placement "
  f"to Horizon Bank Limited (CP-004), which has ample headroom. The "
  f"counterparty confirmation in the pack is on Cedar Commercial Bank paper, "
  f"under Cedar's SWIFT address and Cedar's own reference.")
w("")
w(f"Cedar (CP-009) carries a PKR {EXTRA['cp009_limit']:,} limit. On "
  f"{EXTRA['cp009_peak_day']} the recorded book against Cedar peaks at PKR "
  f"{EXTRA['cp009_peak']:,.0f}, or "
  f"{EXTRA['cp009_peak'] / EXTRA['cp009_limit']:.0%} of the limit - inside it, "
  f"so the exposure test passes. Add the deal the confirmation says was "
  f"actually struck with Cedar and the position becomes PKR "
  f"{EXTRA['cp009_peak'] + float(GT['deals'][c1_deal]['NOTIONAL_PKR']):,.0f}, "
  f"or "
  f"{(EXTRA['cp009_peak'] + float(GT['deals'][c1_deal]['NOTIONAL_PKR'])) / EXTRA['cp009_limit']:.0%}.")
w("")
w("Nothing in the populations shows it. Nothing in any single document shows "
  "it. It appears only when the confirmation is read against the deal record, "
  "and it matters only when the reclassified exposure is recomputed against "
  "the limit. That is a three-step finding, and it is the one to promote.")
w("")
w("## Demo pacing")
w("")
w("1. Import the ten CSVs and let the exploratory analysis run. The limit, "
  "rate and segregation exceptions should surface without prompting.")
w("2. Note what the analysis cannot settle: TS-009 has no row in the dealer "
  "limit file, so 61 T-bill deals are untestable for authority. That is an "
  "inconclusive result and should be reported as one, not as a pass.")
w("3. Open two or three class-1 packs (`" + PACKS["X07"] + "`, `"
  + PACKS["X01"] + "`, `" + PACKS["X04a"] + "`) to show the paper "
  "corroborating what the data already found.")
w("4. Open the class-2 packs (`" + PACKS["D1"] + "`, `" + PACKS["D3"]
  + "`, `" + PACKS["D5"] + "`) against clean table rows, to show what the "
  "analytics structurally cannot reach.")
w("5. Walk `" + PACKS["C1"] + "` end to end, then `" + PACKS["C4"]
  + "` - a complete pack, funds out of the nostro, and no deal record "
    "anywhere in the file.")
w("6. Promote the counterparty substitution, the settled-unconfirmed deals and "
  "the off-market dealing into findings; generate the working paper and the "
  "report draft.")
w("")
w("## Counting")
w("")
data_codes = [c for c in ORDER if TRUTH[c]["class"] == "data"]
flagged = {r for c in data_codes for r in TRUTH[c]["deal_ids"]}
w(f"- {len(TRUTH) - 1} seeded exception types in all.")
w(f"- {len(data_codes)} are visible in the tables, touching {len(flagged)} "
  f"deal references out of {GT['deal_count']:,}.")
w("- 6 are visible only in the documents.")
w("- 4 are contradictions between the two.")
w(f"- {len(PACKS)} deal packs, of which 4 are clean: "
  + ", ".join(f"`{PACKS[k]}`" for k in ("OK1", "OK2", "OK3", "OK4")) + ".")

(ROOT / "FACILITATOR_GUIDE.md").write_text("\n".join(lines) + "\n")
print(f"wrote FACILITATOR_GUIDE.md ({len(lines)} lines)")
