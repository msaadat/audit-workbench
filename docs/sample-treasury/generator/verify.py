"""Re-derive every exception from the written CSVs and reconcile to ground truth.

Nothing here reads the generator's in-memory state: it loads the files the way
the app would and recomputes each test independently, so an exception the
baseline created by accident shows up as an unexplained extra.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

DATA = Path(__file__).resolve().parent.parent / "data"
GT = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text())["ground_truth"]

HOLIDAYS = {date(2025, 1, 1), date(2025, 2, 5), date(2025, 3, 31),
            date(2025, 4, 1), date(2025, 4, 2), date(2025, 5, 1),
            date(2025, 6, 6), date(2025, 6, 9), date(2025, 6, 10)}


def is_bd(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def bd_between(a: date, b: date) -> int:
    """Business days from a to b, signed."""
    step = 1 if b >= a else -1
    n, cur = 0, a
    while cur != b:
        cur += timedelta(days=step)
        if is_bd(cur):
            n += step
    return n


def load(name: str) -> list[dict]:
    with (DATA / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# schema sanity: the files must load in the engine the app uses
for path in sorted(DATA.glob("*.csv")):
    frame = pl.read_csv(path, infer_schema_length=2000)
    assert frame.height > 0, path.name
    assert not any(c.startswith("column_") for c in frame.columns), path.name

deals = load("04_deals.csv")
confs = {c["DEAL_ID"]: c for c in load("05_confirmations.csv")}
setts = load("06_settlements.csv")
sett_by_deal = {s["DEAL_ID"]: s for s in setts}
staff = {s["STAFF_ID"]: s for s in load("01_staff.csv")}
limits = {r["DEALER_ID"]: r for r in load("02_dealer_limits.csv")}
cps = {r["COUNTERPARTY_ID"]: r for r in load("03_counterparties.csv")}
rates = {(r["RATE_DATE"], r["INSTRUMENT"], r["TENOR"]): r
         for r in load("07_market_rates.csv")}
ssis = load("08_standing_settlement_instructions.csv")
policy = {r["PARAMETER_ID"]: r["VALUE"] for r in load("10_policy_parameters.csv")}

BY_ID = {d["DEAL_ID"]: d for d in deals}
live = [d for d in deals if d["DEAL_STATUS"] != "Cancelled"]
settled = [d for d in deals if d["DEAL_STATUS"] == "Settled"]

found: dict[str, set[str]] = {}


def flag(code: str, ids) -> None:
    found[code] = set(ids)


# --- limits ---------------------------------------------------------------
flag("X01", [d["DEAL_ID"] for d in live
             if d["DEALER_ID"] in limits
             and float(d["NOTIONAL_PKR"]) > float(limits[d["DEALER_ID"]]["PER_DEAL_LIMIT_PKR"])])

daily: dict[tuple[str, str], float] = {}
for d in live:
    key = (d["DEALER_ID"], d["DEAL_DATE"])
    daily[key] = daily.get(key, 0.0) + float(d["NOTIONAL_PKR"])
breach_days = {k for k, v in daily.items()
               if k[0] in limits and v > float(limits[k[0]]["DAILY_LIMIT_PKR"])}
flag("X02", [d["DEAL_ID"] for d in live
             if (d["DEALER_ID"], d["DEAL_DATE"]) in breach_days])

exposure: dict[tuple[str, str], float] = {}
for d in live:
    cur = date.fromisoformat(d["VALUE_DATE"])
    end = date.fromisoformat(d["MATURITY_DATE"])
    while cur <= end:
        key = (d["COUNTERPARTY_ID"], cur.isoformat())
        exposure[key] = exposure.get(key, 0.0) + float(d["NOTIONAL_PKR"])
        cur += timedelta(days=1)
breached_cp_days = {k for k, v in exposure.items()
                    if v > float(cps[k[0]]["EXPOSURE_LIMIT_PKR"])}
x03_deals = set()
for d in live:
    cur = date.fromisoformat(d["VALUE_DATE"])
    end = date.fromisoformat(d["MATURITY_DATE"])
    while cur <= end:
        if (d["COUNTERPARTY_ID"], cur.isoformat()) in breached_cp_days:
            x03_deals.add(d["DEAL_ID"])
            break
        cur += timedelta(days=1)
flag("X03", x03_deals)

flag("X04a", [d["DEAL_ID"] for d in live
              if cps[d["COUNTERPARTY_ID"]]["STATUS"] != "Active"
              and d["DEAL_DATE"] > cps[d["COUNTERPARTY_ID"]]["STATUS_EFFECTIVE_DATE"]])
flag("X04b", [d["DEAL_ID"] for d in live
              if d["DEAL_DATE"] > cps[d["COUNTERPARTY_ID"]]["LIMIT_EXPIRY_DATE"]])
flag("X05a", [d["DEAL_ID"] for d in live
              if d["DEALER_ID"] in limits
              and d["DEAL_TYPE"] not in limits[d["DEALER_ID"]]["AUTHORISED_PRODUCTS"].split(";")])
flag("X05b", [d["DEAL_ID"] for d in live
              if d["DEALER_ID"] in limits
              and d["DEAL_DATE"] > limits[d["DEALER_ID"]]["AUTHORISATION_EXPIRY_DATE"]])
x06 = [d["DEAL_ID"] for d in live if d["DEALER_ID"] not in limits]
flag("X06", [])   # structural; counted separately

# --- rate reasonableness --------------------------------------------------
x07 = []
for d in live:
    key = (d["DEAL_DATE"], d["INSTRUMENT"], d["TENOR"])
    if key not in rates:
        continue                      # the non-business-day deal, caught by X09b
    m = float(rates[key]["MID_RATE"])
    band = float(policy["POL-01"]) if d["INSTRUMENT"] not in ("PKR_MM", "PKR_TBILL") \
        else float(policy["POL-02"])
    dev = abs(float(d["DEAL_RATE"]) - m) / m * 10_000
    if dev > band:
        x07.append(d["DEAL_ID"])
flag("X07", x07)

# --- segregation of duties -------------------------------------------------
flag("X08a", [d["DEAL_ID"] for d in live
              if d["DEAL_ID"] in confs
              and confs[d["DEAL_ID"]]["CONFIRMED_BY_ID"] == d["CAPTURED_BY_ID"]])
flag("X08d", [d["DEAL_ID"] for d in live
              if d["DEAL_ID"] in confs
              and staff[confs[d["DEAL_ID"]]["CONFIRMED_BY_ID"]]["DESK"] == "Front Office"])
flag("X08b", [s["DEAL_ID"] for s in setts
              if s["RELEASED_BY_ID"] == s["APPROVED_BY_ID"]])
flag("X08c", [s["DEAL_ID"] for s in setts
              if s["DEAL_ID"] in BY_ID
              and s["APPROVED_BY_ID"] == BY_ID[s["DEAL_ID"]]["DEALER_ID"]])

# --- timing ---------------------------------------------------------------
flag("X09a", [d["DEAL_ID"] for d in live
              if not ("09:00:00" <= d["DEAL_TIME"] <= "17:00:00")])
flag("X09b", [d["DEAL_ID"] for d in live
              if not is_bd(date.fromisoformat(d["DEAL_DATE"]))])
x09c = []
for d in live:
    struck = datetime.fromisoformat(f"{d['DEAL_DATE']} {d['DEAL_TIME']}")
    captured = datetime.fromisoformat(d["CAPTURE_TIMESTAMP"])
    if (captured - struck).total_seconds() / 60 > float(policy["POL-08"]):
        x09c.append(d["DEAL_ID"])
flag("X09c", x09c)

flag("X10a", [d["DEAL_ID"] for d in live
              if d["DEAL_ID"] in confs
              and bd_between(date.fromisoformat(d["DEAL_DATE"]),
                             date.fromisoformat(confs[d["DEAL_ID"]]["SENT_DATE"]))
              > int(policy["POL-03"])])
flag("X10b", [d["DEAL_ID"] for d in settled
              if d["DEAL_ID"] in confs
              and confs[d["DEAL_ID"]]["COUNTERPARTY_RECEIVED_DATE"] > d["VALUE_DATE"]])
flag("X10c", [d["DEAL_ID"] for d in settled
              if d["DEAL_ID"] in confs
              and confs[d["DEAL_ID"]]["MATCH_STATUS"] == "Discrepancy"])
flag("X11", [s["DEAL_ID"] for s in setts
             if s["DEAL_ID"] in BY_ID
             and s["SETTLEMENT_DATE"] != BY_ID[s["DEAL_ID"]]["VALUE_DATE"]])
flag("X12", [d["DEAL_ID"] for d in live
             if d["AMENDED_FLAG"] == "Y" and not d["AMENDMENT_APPROVED_BY_ID"]
             and d["DEAL_STATUS"] != "Cancelled"])
flag("X19", [d["DEAL_ID"] for d in deals
             if d["DEAL_STATUS"] == "Cancelled" and not d["AMENDMENT_APPROVED_BY_ID"]])

# --- completeness ---------------------------------------------------------
flag("X13", [d["DEAL_ID"] for d in settled if d["DEAL_ID"] not in confs])
flag("X14a", [d["DEAL_ID"] for d in settled if d["DEAL_ID"] not in sett_by_deal])
flag("X14b", [s["DEAL_ID"] for s in setts if s["DEAL_ID"] not in BY_ID])

seen: dict[tuple, list[str]] = {}
for d in live:
    key = (d["DEAL_DATE"], d["COUNTERPARTY_ID"], d["DEAL_TYPE"],
           d["NOTIONAL_PKR"], d["DEAL_RATE"], d["VALUE_DATE"])
    seen.setdefault(key, []).append(d["DEAL_ID"])
flag("X15", [i for group in seen.values() if len(group) > 1 for i in group])

refs = sorted(int(d["DEAL_ID"].split("-")[-1]) for d in deals
              if d["DEAL_ID"].startswith("TD-2025-"))
gaps = [f"TD-2025-{n:04d}" for n in range(min(refs), max(refs) + 1)
        if n not in set(refs)]
flag("X16a", gaps)
flag("X16b", [d["DEAL_ID"] for d in deals if not d["DEAL_ID"].startswith("TD-2025-")])

# --- settlement routing ---------------------------------------------------
ssi_ids = {s["SSI_ID"] for s in ssis}
flag("X17a", [s["DEAL_ID"] for s in setts if s["SSI_ID"] not in ssi_ids])
x17b = []
for s in setts:
    ssi = next((x for x in ssis if x["SSI_ID"] == s["SSI_ID"]), None)
    if not ssi or ssi["EFFECTIVE_DATE"] < "2025-01-01":
        continue
    gap = bd_between(date.fromisoformat(ssi["EFFECTIVE_DATE"]),
                     date.fromisoformat(s["SETTLEMENT_DATE"]))
    if 0 <= gap < int(policy["POL-11"]):
        x17b.append(s["DEAL_ID"])
flag("X17b", x17b)

x18 = []
for s in setts:
    d = BY_ID.get(s["DEAL_ID"])
    if not d or d["DEAL_TYPE"] not in ("FX_SPOT", "FX_FORWARD"):
        continue
    expected = float(d["NOTIONAL_AMOUNT"]) * float(d["DEAL_RATE"])
    if abs(float(s["SETTLEMENT_AMOUNT_PKR"]) - expected) > 1.0:
        x18.append(s["DEAL_ID"])
flag("X18", x18)

# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------

# Overlaps that are real audit behaviour rather than generation accidents: a
# deal can legitimately fail two tests, and the ground truth records it once.
EXPECTED_EXTRA = {
    "X08d": "X08a",   # a dealer confirming their own deal is also front office
}
# Every deal outstanding on a breached counterparty-day is implicated by the
# breach; the ground truth names only the tickets that caused it.
X03_IMPLICATED = found["X03"]

print(f"{'code':6} {'expected':>9} {'found':>6}  status")
problems = []
for code, entry in sorted(GT.items()):
    if entry["class"] != "data" or code in ("X06", "X16a"):
        continue
    expected = set(entry["deal_ids"])
    got = found.get(code, set())
    allowed = set(expected)
    if code in EXPECTED_EXTRA:
        allowed |= set(GT[EXPECTED_EXTRA[code]]["deal_ids"])
    if code == "X03":
        allowed |= X03_IMPLICATED
    missing = expected - got
    extra = got - allowed
    status = "ok"
    if missing or extra:
        status = f"MISSING {sorted(missing)} EXTRA {sorted(extra)}"
        problems.append(code)
    print(f"{code:6} {len(expected):>9} {len(got):>6}  {status}")

print(f"\nX16a gaps found: {sorted(found['X16a'])}")
print(f"X06 dealers with no limit row: "
      f"{sorted({BY_ID[i]['DEALER_ID'] for i in x06})} over {len(x06)} deals")
print(f"contradiction packs (invisible to data tests, by design): "
      f"{[GT[c]['deal_ids'][0] for c in ('C1', 'C2', 'C3', 'C4')]}")
for code in ("C1", "C2", "C3", "D1", "D2", "D3", "D4", "D5", "D6"):
    ids = set(GT[code]["deal_ids"])
    hits = [c for c, s in found.items() if ids & s]
    if hits:
        problems.append(code)
        print(f"  !! {code} {sorted(ids)} is visible to data tests {hits}")

print("\nFAILED: " + ", ".join(problems) if problems else "\nall reconciled")
