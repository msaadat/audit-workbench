"""Generate the Meridian Bank treasury demo pack: masters, 1000 deals, and
downstream confirmation/settlement populations.

The baseline is generated clean on purpose: dealer, counterparty and rate
constraints are enforced while deals are built, so every exception in the file
is one this script injected deliberately and recorded in ground_truth.json.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data"
GT_PATH = Path(__file__).resolve().parent / "ground_truth.json"

RNG = random.Random(20250630)

PERIOD_START = date(2025, 1, 1)
PERIOD_END = date(2025, 6, 30)

HOLIDAYS = {
    date(2025, 1, 1),
    date(2025, 2, 5),
    date(2025, 3, 31),
    date(2025, 4, 1),
    date(2025, 4, 2),
    date(2025, 5, 1),
    date(2025, 6, 6),
    date(2025, 6, 9),
    date(2025, 6, 10),
}


def is_bd(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def bd_range(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if is_bd(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def add_bd(d: date, n: int) -> date:
    cur = d
    while n > 0:
        cur += timedelta(days=1)
        if is_bd(cur):
            n -= 1
    return cur


def next_bd(d: date) -> date:
    cur = d
    while not is_bd(cur):
        cur += timedelta(days=1)
    return cur


BUSINESS_DAYS = bd_range(PERIOD_START, PERIOD_END)

# --------------------------------------------------------------------------
# masters
# --------------------------------------------------------------------------

STAFF = [
    ("TS-001", "Nadia Rahman", "Chief Financial Officer", "Executive", ""),
    ("TS-002", "Imran Qadir", "Head of Treasury", "Front Office", "TS-001"),
    ("TS-003", "Faisal Karim", "Chief Dealer - FX", "Front Office", "TS-002"),
    ("TS-004", "Ayesha Malik", "Senior Dealer - FX", "Front Office", "TS-003"),
    ("TS-005", "Bilal Ahmed", "Dealer - FX", "Front Office", "TS-003"),
    ("TS-006", "Hina Shah", "Chief Dealer - Money Market", "Front Office", "TS-002"),
    ("TS-007", "Omar Siddiqui", "Senior Dealer - Money Market", "Front Office", "TS-006"),
    ("TS-008", "Zara Iqbal", "Dealer - Money Market", "Front Office", "TS-006"),
    ("TS-009", "Kamran Yousuf", "Dealer - Fixed Income", "Front Office", "TS-002"),
    ("TS-010", "Sana Tariq", "Junior Dealer - FX", "Front Office", "TS-003"),
    ("TS-011", "Adnan Raza", "Head of Treasury Middle Office", "Middle Office", "TS-019"),
    ("TS-012", "Mehreen Aslam", "Treasury Risk Analyst", "Middle Office", "TS-011"),
    ("TS-013", "Usman Sheikh", "Market Risk Analyst", "Middle Office", "TS-011"),
    ("TS-014", "Rehan Baig", "Head of Treasury Operations", "Back Office", "TS-001"),
    ("TS-015", "Farah Nadeem", "Confirmations Officer", "Back Office", "TS-014"),
    ("TS-016", "Junaid Alvi", "Senior Confirmations Officer", "Back Office", "TS-014"),
    ("TS-017", "Saima Butt", "Settlements Officer", "Back Office", "TS-014"),
    ("TS-018", "Tariq Mehmood", "Senior Settlements Officer", "Back Office", "TS-014"),
    ("TS-019", "Nida Hasan", "Head of Risk", "Executive", "TS-001"),
    ("TS-020", "Shahid Anwar", "Treasury Operations Officer", "Back Office", "TS-014"),
    ("TS-021", "Yasir Kamal", "Nostro Reconciliation Officer", "Back Office", "TS-014"),
    ("TS-022", "Lubna Farooq", "Compliance Officer", "Compliance", "TS-001"),
]
STAFF_NAME = {row[0]: row[1] for row in STAFF}

# TS-009 and TS-010 are deliberately handled below: TS-009 has no limit row at
# all (authority untestable), TS-010's authorisation lapses inside the period.
DEALER_LIMITS = [
    ("TS-003", 500_000_000, 2_000_000_000, "FX_SPOT;FX_FORWARD", "2025-12-31"),
    ("TS-004", 250_000_000, 1_000_000_000, "FX_SPOT;FX_FORWARD", "2025-12-31"),
    ("TS-005", 100_000_000, 400_000_000, "FX_SPOT", "2025-12-31"),
    ("TS-006", 750_000_000, 3_000_000_000,
     "MM_PLACEMENT;MM_BORROWING;TBILL_PURCHASE", "2025-12-31"),
    ("TS-007", 400_000_000, 1_500_000_000, "MM_PLACEMENT;MM_BORROWING", "2025-12-31"),
    ("TS-008", 150_000_000, 600_000_000, "MM_PLACEMENT;MM_BORROWING", "2025-12-31"),
    ("TS-010", 60_000_000, 200_000_000, "FX_SPOT;FX_FORWARD", "2025-05-31"),
]
LIMIT_BY_DEALER = {row[0]: row for row in DEALER_LIMITS}

COUNTERPARTIES = [
    ("CP-001", "Sterling Union Bank Limited", "Bank", "AA-", 3_000_000_000,
     "2025-12-31", "Active", ""),
    ("CP-002", "Harbour National Bank", "Bank", "A+", 2_500_000_000,
     "2025-12-31", "Active", ""),
    ("CP-003", "Pinnacle Commercial Bank", "Bank", "A", 2_000_000_000,
     "2025-12-31", "Active", ""),
    ("CP-004", "Horizon Bank Limited", "Bank", "AA", 3_500_000_000,
     "2025-12-31", "Active", ""),
    ("CP-005", "Crescent Investment Bank", "Bank", "A-", 1_500_000_000,
     "2025-12-31", "Active", ""),
    ("CP-006", "Apex Islamic Bank", "Bank", "A", 1_800_000_000,
     "2025-12-31", "Active", ""),
    ("CP-007", "Frontier Development Bank", "Bank", "BBB+", 800_000_000,
     "2025-04-30", "Active", ""),
    ("CP-008", "Northgate Bank Limited", "Bank", "BBB", 600_000_000,
     "2025-12-31", "Suspended", "2025-03-14"),
    ("CP-009", "Cedar Commercial Bank", "Bank", "A-", 1_200_000_000,
     "2025-12-31", "Active", ""),
    ("CP-010", "State Bank Treasury Operations", "Central Bank", "SOV",
     40_000_000_000, "2025-12-31", "Active", ""),
]
CP_NAME = {row[0]: row[1] for row in COUNTERPARTIES}
CP_LIMIT = {row[0]: row[4] for row in COUNTERPARTIES}

BROKERS = [
    ("BR-001", "Vanguard Money Brokers (Pvt) Limited", "Money Market", "Active"),
    ("BR-002", "Silk Route Brokerage Services", "FX and Money Market", "Active"),
    ("BR-003", "Anchor FX Brokers Limited", "Foreign Exchange", "Active"),
]

POLICY = [
    ("POL-01", "Permitted deviation from market mid rate - FX", "25", "basis points", "6.3"),
    ("POL-02", "Permitted deviation from market mid rate - money market and T-bills",
     "15", "basis points", "6.3"),
    ("POL-03", "Confirmation despatch deadline after deal date", "1", "business days", "7.2"),
    ("POL-04", "Counterparty confirmation must be received before value date",
     "1", "boolean (1 = required)", "7.4"),
    ("POL-05", "Authorised dealing hours - start", "09:00", "time", "5.2"),
    ("POL-06", "Authorised dealing hours - end", "17:00", "time", "5.2"),
    ("POL-07", "Dealing permitted on non-business days", "0", "boolean (1 = permitted)", "5.2"),
    ("POL-08", "Deal capture deadline after execution", "15", "minutes", "5.4"),
    ("POL-09", "Dual signature threshold for payment release", "250000000", "PKR", "8.5"),
    ("POL-10", "Settlement permitted only to an approved standing instruction",
     "1", "boolean (1 = required)", "8.3"),
    ("POL-11", "Standing settlement instruction amendment cooling-off period",
     "2", "business days", "8.4"),
    ("POL-12", "Front office staff may confirm or settle deals", "0",
     "boolean (1 = permitted)", "4.1"),
    ("POL-13", "Deal amendment or cancellation requires operations approval",
     "1", "boolean (1 = required)", "5.6"),
    ("POL-14", "Settlement must fall on the contracted value date", "1",
     "boolean (1 = required)", "8.1"),
]

# --------------------------------------------------------------------------
# market rates
# --------------------------------------------------------------------------

FX_PAIRS = {
    "USD/PKR": (278.50, 283.20, 0.35),
    "EUR/PKR": (289.40, 305.10, 0.90),
    "GBP/PKR": (348.20, 358.60, 1.10),
    "AED/PKR": (75.82, 77.14, 0.09),
}
FX_TENORS = {"SPOT": 0, "1M": 30, "2M": 60, "3M": 90}
FX_FWD_TENORS = ["1M", "2M", "3M"]
MM_TENORS = {"1W": 7, "2W": 14, "1M": 30, "2M": 60, "3M": 90}
TBILL_TENORS = {"3M": 91, "6M": 182, "12M": 364}
FOREIGN_RATE = {"USD/PKR": 4.50, "EUR/PKR": 2.90, "GBP/PKR": 4.20, "AED/PKR": 4.60}

market: dict[tuple[str, str, str], dict] = {}
mm_curve: dict[str, dict[str, float]] = {}

n_days = len(BUSINESS_DAYS)
for i, d in enumerate(BUSINESS_DAYS):
    frac = i / max(n_days - 1, 1)
    key = d.isoformat()
    pkr_1m = round(12.60 - 1.70 * frac + RNG.uniform(-0.04, 0.04), 4)
    mm_curve[key] = {
        "1W": round(pkr_1m - 0.22 + RNG.uniform(-0.03, 0.03), 4),
        "2W": round(pkr_1m - 0.14 + RNG.uniform(-0.03, 0.03), 4),
        "1M": pkr_1m,
        "2M": round(pkr_1m + 0.09 + RNG.uniform(-0.03, 0.03), 4),
        "3M": round(pkr_1m + 0.17 + RNG.uniform(-0.03, 0.03), 4),
    }
    for pair, (lo, hi, vol) in FX_PAIRS.items():
        spot = lo + (hi - lo) * frac + RNG.uniform(-vol, vol)
        for tenor, days in FX_TENORS.items():
            carry = (pkr_1m - FOREIGN_RATE[pair]) / 100.0 * days / 365.0
            mid = round(spot * (1 + carry), 4)
            market[(key, pair, tenor)] = {
                "mid": mid,
                "high": round(mid * 1.0030, 4),
                "low": round(mid * 0.9970, 4),
            }
    for tenor, rate in mm_curve[key].items():
        market[(key, "PKR_MM", tenor)] = {
            "mid": rate,
            "high": round(rate + 0.04, 4),
            "low": round(rate - 0.04, 4),
        }
    for tenor, base in (("3M", 0.10), ("6M", 0.24), ("12M", 0.38)):
        rate = round(pkr_1m + base + RNG.uniform(-0.03, 0.03), 4)
        market[(key, "PKR_TBILL", tenor)] = {
            "mid": rate,
            "high": round(rate + 0.03, 4),
            "low": round(rate - 0.03, 4),
        }


def mid(d: date, instrument: str, tenor: str) -> float:
    return market[(d.isoformat(), instrument, tenor)]["mid"]


# --------------------------------------------------------------------------
# deal generation
# --------------------------------------------------------------------------

DEAL_MIX = (
    ["FX_SPOT"] * 380
    + ["FX_FORWARD"] * 180
    + ["MM_PLACEMENT"] * 190
    + ["MM_BORROWING"] * 150
    + ["TBILL_PURCHASE"] * 100
)

DEALERS_BY_TYPE = {
    "FX_SPOT": (["TS-003", "TS-004", "TS-005", "TS-010"], [30, 30, 25, 15]),
    "FX_FORWARD": (["TS-003", "TS-004", "TS-010"], [45, 45, 10]),
    "MM_PLACEMENT": (["TS-006", "TS-007", "TS-008"], [35, 40, 25]),
    "MM_BORROWING": (["TS-006", "TS-007", "TS-008"], [40, 35, 25]),
    "TBILL_PURCHASE": (["TS-006", "TS-009"], [45, 55]),
}

CP_POOL = ["CP-001", "CP-002", "CP-003", "CP-004", "CP-005",
           "CP-006", "CP-007", "CP-008", "CP-009"]
CP_WEIGHTS = [18, 16, 14, 16, 11, 10, 5, 4, 6]

SIZE_BAND = {
    "FX_SPOT": (30_000_000, 260_000_000),
    "FX_FORWARD": (50_000_000, 320_000_000),
    "MM_PLACEMENT": (60_000_000, 620_000_000),
    "MM_BORROWING": (60_000_000, 560_000_000),
    "TBILL_PURCHASE": (200_000_000, 1_200_000_000),
}

FX_LOT = {"USD/PKR": 50_000, "EUR/PKR": 25_000, "GBP/PKR": 25_000, "AED/PKR": 100_000}
PAIR_WEIGHTS = ([("USD/PKR", 62), ("EUR/PKR", 16), ("GBP/PKR", 13), ("AED/PKR", 9)])

# running control state, so the clean baseline stays clean
dealer_day_total: dict[tuple[str, str], int] = {}
cp_exposure: dict[tuple[str, str], int] = {}   # (cp, iso date) -> notional live


def exposure_days(value_date: date, maturity: date) -> list[str]:
    out, cur = [], value_date
    while cur <= maturity:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def cp_peak(cp: str, days: list[str], extra: int) -> int:
    return max(cp_exposure.get((cp, day), 0) + extra for day in days)


def deal_time(rng: random.Random) -> str:
    hour = rng.choices([9, 10, 11, 12, 13, 14, 15, 16], [8, 16, 18, 12, 8, 14, 16, 8])[0]
    return f"{hour:02d}:{rng.randrange(0, 60):02d}:{rng.randrange(0, 60):02d}"


deals: list[dict] = []
seq = 0
# three references are skipped so the file carries a reference-sequence gap;
# 0447 is the deal a document pack exists for but the system never recorded.
GAP_REFS = {447, 612, 884}

day_pool = [d for d in BUSINESS_DAYS for _ in range(9)]
RNG.shuffle(DEAL_MIX)

deal_dates = []
while len(deal_dates) < len(DEAL_MIX):
    deal_dates.append(RNG.choice(BUSINESS_DAYS))
deal_dates.sort()

for idx, deal_date in enumerate(deal_dates):
    deal_type = DEAL_MIX[idx]
    seq += 1
    while seq in GAP_REFS:
        seq += 1
    deal_id = f"TD-2025-{seq:04d}"

    dealers, weights = DEALERS_BY_TYPE[deal_type]
    dealer = RNG.choices(dealers, weights)[0]
    if dealer == "TS-010" and deal_date > date(2025, 5, 30):
        dealer = "TS-005" if deal_type == "FX_SPOT" else "TS-004"
    if deal_type == "FX_FORWARD" and dealer == "TS-005":
        dealer = "TS-004"

    limit_row = LIMIT_BY_DEALER.get(dealer)
    per_deal_cap = limit_row[1] if limit_row else 600_000_000
    daily_cap = limit_row[2] if limit_row else 2_400_000_000

    lo, hi = SIZE_BAND[deal_type]
    hi = min(hi, int(per_deal_cap * 0.90))
    lo = min(lo, int(hi * 0.6))
    notional_pkr = RNG.randrange(lo, hi, 1_000_000)

    day_key = (dealer, deal_date.isoformat())
    used = dealer_day_total.get(day_key, 0)
    if used + notional_pkr > daily_cap * 0.80:
        notional_pkr = max(20_000_000, int((daily_cap * 0.80 - used) // 1_000_000) * 1_000_000)
        if notional_pkr < 20_000_000:
            notional_pkr = 20_000_000

    if deal_type in ("FX_SPOT", "FX_FORWARD"):
        instrument = RNG.choices([p for p, _ in PAIR_WEIGHTS],
                                 [w for _, w in PAIR_WEIGHTS])[0]
        if deal_type == "FX_SPOT":
            tenor, value_date = "SPOT", add_bd(deal_date, 2)
        else:
            tenor = RNG.choices(FX_FWD_TENORS, [50, 30, 20])[0]
            value_date = next_bd(deal_date + timedelta(days=FX_TENORS[tenor]))
        maturity = value_date
        rate_mid = mid(deal_date, instrument, tenor)
        drift = RNG.uniform(-0.00013, 0.00013)  # inside the 25bp policy band
        deal_rate = round(rate_mid * (1 + drift), 4)
        lot = FX_LOT[instrument]
        units = max(1, round(notional_pkr / deal_rate / lot))
        notional_amount = units * lot
        notional_pkr = int(round(notional_amount * deal_rate))
        currency = instrument.split("/")[0]
    elif deal_type in ("MM_PLACEMENT", "MM_BORROWING"):
        instrument = "PKR_MM"
        tenor = RNG.choices(list(MM_TENORS), [22, 18, 30, 16, 14])[0]
        value_date = next_bd(deal_date + timedelta(days=RNG.choice([0, 1])))
        maturity = next_bd(value_date + timedelta(days=MM_TENORS[tenor]))
        rate_mid = mid(deal_date, instrument, tenor)
        deal_rate = round(rate_mid + RNG.uniform(-0.008, 0.008), 4)
        notional_amount = notional_pkr
        currency = "PKR"
    else:
        instrument = "PKR_TBILL"
        tenor = RNG.choices(list(TBILL_TENORS), [55, 30, 15])[0]
        value_date = add_bd(deal_date, 1)
        maturity = next_bd(value_date + timedelta(days=TBILL_TENORS[tenor]))
        rate_mid = mid(deal_date, instrument, tenor)
        deal_rate = round(rate_mid + RNG.uniform(-0.008, 0.008), 4)
        notional_amount = notional_pkr
        currency = "PKR"

    if deal_type == "TBILL_PURCHASE":
        candidates = ["CP-010", "CP-010", "CP-010", "CP-001", "CP-002"]
    else:
        candidates = RNG.choices(CP_POOL, CP_WEIGHTS, k=8)
    window = exposure_days(value_date, maturity)
    counterparty = None
    for cand in candidates:
        if cand == "CP-007" and deal_date > date(2025, 4, 29):
            continue
        if cand == "CP-008" and deal_date > date(2025, 3, 13):
            continue
        if cp_peak(cand, window, notional_pkr) <= CP_LIMIT[cand] * 0.82:
            counterparty = cand
            break
    if counterparty is None:
        counterparty = "CP-001" if deal_type != "TBILL_PURCHASE" else "CP-010"
        notional_pkr = min(notional_pkr, 40_000_000)
        if deal_type in ("FX_SPOT", "FX_FORWARD"):
            lot = FX_LOT[instrument]
            units = max(1, round(notional_pkr / deal_rate / lot))
            notional_amount = units * lot
            notional_pkr = int(round(notional_amount * deal_rate))
        else:
            notional_amount = notional_pkr

    for day in window:
        cp_exposure[(counterparty, day)] = cp_exposure.get((counterparty, day), 0) + notional_pkr
    dealer_day_total[day_key] = dealer_day_total.get(day_key, 0) + notional_pkr

    if deal_type in ("MM_PLACEMENT", "MM_BORROWING") and RNG.random() < 0.42:
        broker = RNG.choices(["BR-001", "BR-002"], [55, 45])[0]
    elif deal_type in ("FX_SPOT", "FX_FORWARD") and RNG.random() < 0.28:
        broker = RNG.choices(["BR-003", "BR-002"], [60, 40])[0]
    else:
        broker = ""

    dtime = deal_time(RNG)
    captured_at = datetime.combine(deal_date, datetime.strptime(dtime, "%H:%M:%S").time())
    captured_at += timedelta(minutes=RNG.randrange(1, 10), seconds=RNG.randrange(0, 60))

    status = "Settled" if value_date <= PERIOD_END else "Outstanding"

    deals.append({
        "DEAL_ID": deal_id,
        "DEAL_DATE": deal_date.isoformat(),
        "DEAL_TIME": dtime,
        "DEAL_TYPE": deal_type,
        "INSTRUMENT": instrument,
        "TENOR": tenor,
        "COUNTERPARTY_ID": counterparty,
        "DEALER_ID": dealer,
        "BROKER_ID": broker,
        "CURRENCY": currency,
        "NOTIONAL_AMOUNT": f"{notional_amount:.2f}",
        "DEAL_RATE": f"{deal_rate:.4f}",
        "NOTIONAL_PKR": f"{notional_pkr:.2f}",
        "VALUE_DATE": value_date.isoformat(),
        "MATURITY_DATE": maturity.isoformat(),
        "DEAL_STATUS": status,
        "CAPTURED_BY_ID": dealer,
        "CAPTURE_TIMESTAMP": captured_at.strftime("%Y-%m-%d %H:%M:%S"),
        "AMENDED_FLAG": "N",
        "AMENDMENT_DATE": "",
        "AMENDMENT_APPROVED_BY_ID": "",
    })


BY_ID = {d["DEAL_ID"]: d for d in deals}
GT: dict[str, dict] = {}
USED: set[str] = set()


def note(code: str, title: str, cls: str, severity: str, deal_ids, detail: str) -> None:
    GT[code] = {
        "code": code,
        "title": title,
        "class": cls,
        "severity": severity,
        "deal_ids": list(deal_ids),
        "detail": detail,
    }


def pick(n: int, **criteria) -> list[dict]:
    """Deterministically choose n unused deals matching the criteria."""
    pred = criteria.pop("pred", None)
    out = []
    for d in deals:
        if d["DEAL_ID"] in USED:
            continue
        if any(d[k] != v for k, v in criteria.items()):
            continue
        if pred and not pred(d):
            continue
        out.append(d)
        if len(out) == n:
            break
    if len(out) < n:
        raise SystemExit(f"only {len(out)} of {n} deals matched {criteria}")
    for d in out:
        USED.add(d["DEAL_ID"])
    return out


def set_notional(d: dict, notional_pkr: int) -> None:
    """Resize a deal, keeping the FX arithmetic self-consistent."""
    if d["DEAL_TYPE"] in ("FX_SPOT", "FX_FORWARD"):
        rate = float(d["DEAL_RATE"])
        lot = FX_LOT[d["INSTRUMENT"]]
        units = max(lot, round(notional_pkr / rate / lot) * lot)
        d["NOTIONAL_AMOUNT"] = f"{units:.2f}"
        d["NOTIONAL_PKR"] = f"{units * rate:.2f}"
    else:
        d["NOTIONAL_AMOUNT"] = f"{notional_pkr:.2f}"
        d["NOTIONAL_PKR"] = f"{notional_pkr:.2f}"


def reprice(d: dict, deviation: float = 0.0) -> None:
    """Re-read the deal's rate off the market curve for its (possibly new) date."""
    base = mid(date.fromisoformat(d["DEAL_DATE"]), d["INSTRUMENT"], d["TENOR"])
    if d["INSTRUMENT"] in ("PKR_MM", "PKR_TBILL"):
        rate = round(base + deviation * 100, 4)  # deviation as a fraction
    else:
        rate = round(base * (1 + deviation), 4)
    d["DEAL_RATE"] = f"{rate:.4f}"
    if d["DEAL_TYPE"] in ("FX_SPOT", "FX_FORWARD"):
        d["NOTIONAL_PKR"] = f"{float(d['NOTIONAL_AMOUNT']) * rate:.2f}"


def retime(d: dict, new_date: date, new_time: str, capture_gap: int = 4) -> None:
    """Move a deal to another dealing date, rebuilding everything date-derived."""
    d["DEAL_DATE"] = new_date.isoformat()
    d["DEAL_TIME"] = new_time
    captured = datetime.combine(new_date, datetime.strptime(new_time, "%H:%M:%S").time())
    captured += timedelta(minutes=capture_gap)
    d["CAPTURE_TIMESTAMP"] = captured.strftime("%Y-%m-%d %H:%M:%S")
    if d["DEAL_TYPE"] == "FX_SPOT":
        value = add_bd(new_date, 2)
        maturity = value
    elif d["DEAL_TYPE"] == "FX_FORWARD":
        value = next_bd(new_date + timedelta(days=FX_TENORS[d["TENOR"]]))
        maturity = value
    elif d["DEAL_TYPE"] in ("MM_PLACEMENT", "MM_BORROWING"):
        value = next_bd(new_date)
        maturity = next_bd(value + timedelta(days=MM_TENORS[d["TENOR"]]))
    else:
        value = add_bd(new_date, 1)
        maturity = next_bd(value + timedelta(days=TBILL_TENORS[d["TENOR"]]))
    d["VALUE_DATE"] = value.isoformat()
    d["MATURITY_DATE"] = maturity.isoformat()
    d["DEAL_STATUS"] = "Settled" if value <= PERIOD_END else "Outstanding"
    if is_bd(new_date):
        reprice(d)


def cp_profile() -> dict[str, dict[str, float]]:
    prof: dict[str, dict[str, float]] = {}
    for d in deals:
        if d["DEAL_STATUS"] == "Cancelled":
            continue
        cp = d["COUNTERPARTY_ID"]
        amount = float(d["NOTIONAL_PKR"])
        for day in exposure_days(date.fromisoformat(d["VALUE_DATE"]),
                                 date.fromisoformat(d["MATURITY_DATE"])):
            prof.setdefault(cp, {})
            prof[cp][day] = prof[cp].get(day, 0.0) + amount
    return prof


# --- X01  notional above the dealer's per-deal limit -----------------------
x01 = pick(1, DEALER_ID="TS-005", DEAL_TYPE="FX_SPOT", DEAL_STATUS="Settled",
           pred=lambda d: d["DEAL_DATE"] > "2025-02-01")
x01 += pick(1, DEALER_ID="TS-008", DEAL_STATUS="Settled",
            pred=lambda d: d["DEAL_DATE"] > "2025-04-01")
set_notional(x01[0], 142_000_000)   # cap 100,000,000
set_notional(x01[1], 231_000_000)   # cap 150,000,000
note("X01", "Deal notional exceeds the dealer's per-deal limit", "data", "High",
     [d["DEAL_ID"] for d in x01],
     "TS-005 dealt PKR 142m against a PKR 100m per-deal limit; TS-008 dealt "
     "PKR 231m against a PKR 150m limit.")

# --- X02  dealer daily aggregate above the daily limit ---------------------
x02_date = date(2025, 3, 12)
x02 = pick(5, DEALER_ID="TS-005", DEAL_TYPE="FX_SPOT",
           pred=lambda d: d["DEAL_DATE"] > "2025-03-14")
for i, d in enumerate(x02):
    retime(d, x02_date, f"{10 + i:02d}:{17 + i * 6:02d}:0{i}")
    set_notional(d, 95_000_000)
    d["COUNTERPARTY_ID"] = ["CP-001", "CP-002", "CP-004", "CP-006", "CP-003"][i]
note("X02", "Dealer's aggregate dealing on one day exceeds the daily limit",
     "data", "High", [d["DEAL_ID"] for d in x02],
     "TS-005 dealt five tickets of ~PKR 95m on 12 March 2025 - PKR 475m against "
     "a PKR 400m daily limit. Every ticket passes the per-deal test on its own.")

# --- X03  counterparty exposure breached by splitting ----------------------
# Sized against CP-005's book as it already stands on the day, so the four
# tickets together carry the limit just past 100% and no further.
x03_date = date(2025, 2, 19)
_x03_window = exposure_days(next_bd(x03_date),
                            next_bd(next_bd(x03_date) + timedelta(days=30)))
_x03_base = cp_profile().get("CP-005", {})
_x03_peak = max((_x03_base.get(day, 0.0) for day in _x03_window), default=0.0)
_x03_total = int(CP_LIMIT["CP-005"] * 1.07 - _x03_peak)
_x03_split = [0.29, 0.27, 0.23, 0.21]
x03 = pick(4, DEAL_TYPE="MM_PLACEMENT",
           pred=lambda d: d["DEALER_ID"] in ("TS-006", "TS-007")
           and d["DEAL_DATE"] > "2025-02-20")
for i, d in enumerate(x03):
    d["TENOR"] = "1M"
    retime(d, x03_date, f"{11 + i:02d}:{5 + i * 9:02d}:1{i}")
    set_notional(d, int(_x03_total * _x03_split[i] // 1_000_000) * 1_000_000)
    d["COUNTERPARTY_ID"] = "CP-005"
GT_X03_CAUSE = [d["DEAL_ID"] for d in x03]
note("X03", "Counterparty exposure limit breached in aggregate", "data",
     "High", GT_X03_CAUSE,
     f"CP-005's book already stood at PKR {_x03_peak:,.0f} against a PKR "
     f"{CP_LIMIT['CP-005']:,} limit, or "
     f"{_x03_peak / CP_LIMIT['CP-005']:.0%}. Four further placements struck on "
     f"19 February 2025, together PKR {_x03_total:,}, carry it past the limit "
     f"for the life of the tenor. Each of the four is around 6% of the limit "
     f"and none is close to any per-deal threshold, so the breach exists only "
     f"in the aggregate and only on a daily exposure profile. Thirteen deals "
     f"in total are outstanding on the breached days.")

# --- X04  dealing with a blocked or lapsed counterparty --------------------
x04a = pick(2, DEAL_TYPE="FX_SPOT",
            pred=lambda d: d["DEAL_DATE"] > "2025-03-20"
            and float(d["NOTIONAL_PKR"]) < 200_000_000)
for d in x04a:
    d["COUNTERPARTY_ID"] = "CP-008"
note("X04a", "Deal executed with a suspended counterparty", "data", "High",
     [d["DEAL_ID"] for d in x04a],
     "CP-008 Northgate Bank was suspended with effect from 14 March 2025; both "
     "deals were struck after that date.")

x04b = pick(2, DEAL_TYPE="MM_PLACEMENT",
            pred=lambda d: d["DEAL_DATE"] > "2025-05-05"
            and float(d["NOTIONAL_PKR"]) < 300_000_000)
for d in x04b:
    d["COUNTERPARTY_ID"] = "CP-007"
note("X04b", "Deal executed after the counterparty's limit had expired",
     "data", "Moderate", [d["DEAL_ID"] for d in x04b],
     "CP-007's approved limit expired on 30 April 2025 and was not renewed.")

# --- X05  dealer authority ------------------------------------------------
x05a = pick(2, DEAL_TYPE="FX_FORWARD", pred=lambda d: d["DEAL_DATE"] > "2025-02-10")
for d in x05a:
    d["DEALER_ID"] = "TS-005"
    d["CAPTURED_BY_ID"] = "TS-005"
    set_notional(d, 78_000_000)
note("X05a", "Dealer transacted a product outside their authorisation", "data",
     "High", [d["DEAL_ID"] for d in x05a],
     "TS-005 Bilal Ahmed is authorised for FX_SPOT only; both tickets are FX "
     "forwards.")

x05b = pick(3, DEAL_TYPE="FX_SPOT", pred=lambda d: d["DEAL_DATE"] > "2025-06-11")
for d in x05b:
    d["DEALER_ID"] = "TS-010"
    d["CAPTURED_BY_ID"] = "TS-010"
    set_notional(d, 48_000_000)
note("X05b", "Dealer transacted after their dealing authorisation had lapsed",
     "data", "High", [d["DEAL_ID"] for d in x05b],
     "TS-010 Sana Tariq's authorisation expired on 31 May 2025; three June "
     "tickets follow it.")

# --- X07  off-market rates -------------------------------------------------
x07 = pick(2, DEALER_ID="TS-005", DEAL_TYPE="FX_SPOT", COUNTERPARTY_ID="CP-003",
           pred=lambda d: d["DEAL_DATE"] > "2025-01-20")
x07 += pick(1, DEAL_TYPE="FX_SPOT", pred=lambda d: d["DEALER_ID"] == "TS-004"
            and d["DEAL_DATE"] > "2025-04-01")
x07 += pick(1, DEAL_TYPE="MM_BORROWING", pred=lambda d: d["DEAL_DATE"] > "2025-05-01")
reprice(x07[0], 0.0092)
reprice(x07[1], 0.0104)
reprice(x07[2], -0.0071)
reprice(x07[3], 0.0038)   # money market: +38bp against a 15bp band
note("X07", "Dealt rate outside the permitted deviation from the market mid",
     "data", "High", [d["DEAL_ID"] for d in x07],
     "Two of the four are the same dealer (TS-005) with the same counterparty "
     "(CP-003), which is the pattern rather than the individual deal.")

# --- X09  dealing hours and capture discipline -----------------------------
x09a = pick(3, pred=lambda d: d["DEAL_TYPE"] in ("FX_SPOT", "MM_PLACEMENT"))
for d, t in zip(x09a, ("07:42:11", "18:15:37", "19:03:52")):
    d["DEAL_TIME"] = t
    captured = datetime.combine(date.fromisoformat(d["DEAL_DATE"]),
                                datetime.strptime(t, "%H:%M:%S").time())
    captured += timedelta(minutes=6)
    d["CAPTURE_TIMESTAMP"] = captured.strftime("%Y-%m-%d %H:%M:%S")
note("X09a", "Deal struck outside authorised dealing hours", "data", "Moderate",
     [d["DEAL_ID"] for d in x09a],
     "Policy clause 5.2 permits dealing between 09:00 and 17:00 only.")

x09b = pick(1, DEAL_TYPE="FX_SPOT", pred=lambda d: d["DEAL_DATE"] > "2025-04-10")
retime(x09b[0], date(2025, 4, 12), "12:26:04")   # a Saturday
note("X09b", "Deal struck on a non-business day", "data", "Moderate",
     [d["DEAL_ID"] for d in x09b],
     "12 April 2025 is a Saturday; the deal carries a market rate the file does "
     "not publish for that date.")

x09c = pick(3, pred=lambda d: d["DEAL_TYPE"] in ("FX_SPOT", "FX_FORWARD",
                                                 "MM_PLACEMENT"))
for d, gap in zip(x09c, (47, 96, 214)):
    captured = datetime.combine(date.fromisoformat(d["DEAL_DATE"]),
                                datetime.strptime(d["DEAL_TIME"], "%H:%M:%S").time())
    captured += timedelta(minutes=gap)
    d["CAPTURE_TIMESTAMP"] = captured.strftime("%Y-%m-%d %H:%M:%S")
note("X09c", "Deal captured well after execution", "data", "Low",
     [d["DEAL_ID"] for d in x09c],
     "Capture lags execution by 47, 96 and 214 minutes against a 15-minute "
     "policy deadline.")

# --- X15  near-duplicate deals --------------------------------------------
x15_seed = pick(1, DEAL_TYPE="MM_PLACEMENT",
                pred=lambda d: d["DEAL_DATE"] > "2025-03-01"
                and d["DEAL_STATUS"] == "Settled")[0]
x15_twin = pick(1, DEAL_TYPE="MM_PLACEMENT",
                pred=lambda d: d["DEAL_DATE"] > x15_seed["DEAL_DATE"])[0]
for field in ("DEAL_DATE", "DEAL_TYPE", "INSTRUMENT", "TENOR", "COUNTERPARTY_ID",
              "DEALER_ID", "CURRENCY", "NOTIONAL_AMOUNT", "DEAL_RATE",
              "NOTIONAL_PKR", "VALUE_DATE", "MATURITY_DATE", "DEAL_STATUS",
              "CAPTURED_BY_ID"):
    x15_twin[field] = x15_seed[field]
x15_twin["DEAL_TIME"] = "15:41:09"
x15_twin["CAPTURE_TIMESTAMP"] = f"{x15_seed['DEAL_DATE']} 15:48:22"
note("X15", "Near-duplicate deals booked on the same day", "data", "Moderate",
     [x15_seed["DEAL_ID"], x15_twin["DEAL_ID"]],
     "Same counterparty, notional, rate and value date, booked 90 minutes "
     "apart. Both settle.")

# --- X19  cancellations, two of them unapproved ----------------------------
x19_ok = pick(6, pred=lambda d: d["DEAL_STATUS"] == "Settled"
              and d["DEAL_DATE"] > "2025-01-15")
for i, d in enumerate(x19_ok):
    d["DEAL_STATUS"] = "Cancelled"
    d["AMENDED_FLAG"] = "Y"
    d["AMENDMENT_DATE"] = add_bd(date.fromisoformat(d["DEAL_DATE"]), 1).isoformat()
    d["AMENDMENT_APPROVED_BY_ID"] = "TS-014" if i % 2 == 0 else "TS-018"
x19_bad = pick(2, pred=lambda d: d["DEAL_STATUS"] == "Settled"
               and d["DEAL_DATE"] > "2025-02-15")
for d in x19_bad:
    d["DEAL_STATUS"] = "Cancelled"
    d["AMENDED_FLAG"] = "Y"
    d["AMENDMENT_DATE"] = add_bd(date.fromisoformat(d["DEAL_DATE"]), 1).isoformat()
    d["AMENDMENT_APPROVED_BY_ID"] = ""
note("X19", "Deal cancelled without the required operations approval", "data",
     "Moderate", [d["DEAL_ID"] for d in x19_bad],
     "Eight deals were cancelled in the period; six carry an operations "
     "approver and these two carry none.")

# --- X12  amended after the counterparty confirmation, unapproved ----------
x12 = pick(2, pred=lambda d: d["DEAL_STATUS"] == "Settled"
           and d["DEAL_DATE"] > "2025-03-05"
           and d["DEAL_TYPE"] in ("MM_PLACEMENT", "FX_FORWARD"))
for d in x12:
    d["AMENDED_FLAG"] = "Y"
    d["AMENDMENT_DATE"] = add_bd(date.fromisoformat(d["DEAL_DATE"]), 3).isoformat()
    d["AMENDMENT_APPROVED_BY_ID"] = ""
note("X12", "Deal amended after the counterparty confirmation was received, "
     "without approval", "data", "High", [d["DEAL_ID"] for d in x12],
     "The amendment post-dates the matched confirmation and carries no "
     "operations approver.")

# The reference-sequence exceptions are applied in the renumbering step below,
# once every deal sits on its final dealing date.

# --------------------------------------------------------------------------
# counterparty limit calibration and baseline verification
# --------------------------------------------------------------------------

profile = cp_profile()

# CP-009 is the counterparty the headline mis-booking conceals. Its approved
# limit is set so the *recorded* book peaks just inside it: the concealed deal
# is what tips it over, and nothing in the tables shows that.
cp009_peak = max(profile.get("CP-009", {0: 0}).values())
cp009_peak_day = max(profile["CP-009"], key=lambda k: profile["CP-009"][k])
cp009_limit = int(round(cp009_peak / 0.93 / 50_000_000) * 50_000_000)
COUNTERPARTIES = [
    (row[0], row[1], row[2], row[3], cp009_limit if row[0] == "CP-009" else row[4],
     row[5], row[6], row[7])
    for row in COUNTERPARTIES
]
CP_LIMIT = {row[0]: row[4] for row in COUNTERPARTIES}
print(f"CP-009 peak {cp009_peak:,.0f} on {cp009_peak_day} -> limit {cp009_limit:,}")

INTENTIONAL_CP_BREACH = {"CP-005"}
for cp, days in profile.items():
    peak = max(days.values())
    if peak > CP_LIMIT[cp] and cp not in INTENTIONAL_CP_BREACH:
        raise SystemExit(f"unintended exposure breach: {cp} peak {peak:,.0f} "
                         f"vs limit {CP_LIMIT[cp]:,}")
    if cp in INTENTIONAL_CP_BREACH:
        print(f"  {cp} intentional peak {peak:,.0f} vs limit {CP_LIMIT[cp]:,} "
              f"({peak / CP_LIMIT[cp]:.1%})")

# --------------------------------------------------------------------------
# document-only and contradiction deals: their table rows stay clean
# --------------------------------------------------------------------------

CLEAN = dict(pred=lambda d: d["DEAL_STATUS"] == "Settled"
             and d["DEAL_DATE"] > "2025-01-20" and d["BROKER_ID"] != "")

# Dated so its exposure window straddles CP-009's peak day: that is the whole
# point of the concealment.
_peak_day = date.fromisoformat(cp009_peak_day)
_c1_deal_date = bd_range(_peak_day - timedelta(days=8), _peak_day)[-3]
c1 = pick(1, COUNTERPARTY_ID="CP-004", DEAL_TYPE="MM_PLACEMENT",
          pred=lambda d: d["DEAL_STATUS"] == "Settled")[0]
c1["TENOR"] = "1M"
retime(c1, _c1_deal_date, "11:34:18")
c1["VALUE_DATE"] = next_bd(_c1_deal_date).isoformat()
c1["MATURITY_DATE"] = next_bd(next_bd(_c1_deal_date) + timedelta(days=30)).isoformat()
c1["DEAL_STATUS"] = "Settled"
set_notional(c1, int(cp009_limit * 0.12 // 1_000_000) * 1_000_000)
assert c1["VALUE_DATE"] <= cp009_peak_day <= c1["MATURITY_DATE"], "C1 misses the peak"
concealed = float(c1["NOTIONAL_PKR"])
note("C1", "Deal booked against a different counterparty from the one it was "
     "struck with", "contradiction", "High", [c1["DEAL_ID"]],
     f"Booked to CP-004 Horizon Bank. The counterparty confirmation in the pack "
     f"is on Cedar Commercial Bank (CP-009) paper and carries Cedar's reference. "
     f"Recorded CP-009 exposure peaks at PKR {cp009_peak:,.0f} against a limit "
     f"of PKR {cp009_limit:,}; adding this PKR {concealed:,.0f} deal takes it to "
     f"PKR {cp009_peak + concealed:,.0f}, or "
     f"{(cp009_peak + concealed) / cp009_limit:.0%} of the limit. Invisible to "
     f"every data test and to either document read alone.")

c2 = pick(1, DEAL_TYPE="FX_SPOT", pred=lambda d: d["DEAL_STATUS"] == "Settled"
          and d["DEAL_DATE"] > "2025-02-01" and d["INSTRUMENT"] == "USD/PKR")[0]
c2_confirmed_rate = round(float(c2["DEAL_RATE"]) * 1.0095, 4)
_c2_mid = mid(date.fromisoformat(c2["DEAL_DATE"]), c2["INSTRUMENT"], c2["TENOR"])
_c2_booked_bp = abs(float(c2["DEAL_RATE"]) - _c2_mid) / _c2_mid * 10_000
_c2_conf_bp = abs(c2_confirmed_rate - _c2_mid) / _c2_mid * 10_000
note("C2", "Rate on the counterparty confirmation differs from the rate booked",
     "contradiction", "High", [c2["DEAL_ID"]],
     f"Booked at {c2['DEAL_RATE']}, which is {_c2_booked_bp:.0f}bp from the "
     f"published mid of {_c2_mid:.4f} and so inside the 25bp policy band. The "
     f"counterparty confirms {c2_confirmed_rate:.4f}, which is "
     f"{_c2_conf_bp:.0f}bp from the same mid. The rate reasonableness test "
     f"passes because it reads the recorded rate, not the confirmed one.")

c3 = pick(1, DEAL_TYPE="MM_BORROWING", pred=lambda d: d["DEAL_STATUS"] == "Settled"
          and d["DEAL_DATE"] > "2025-03-01")[0]
c3_confirmed_notional = float(c3["NOTIONAL_PKR"]) + 25_000_000
note("C3", "Notional on the counterparty confirmation differs from the notional "
     "booked", "contradiction", "High", [c3["DEAL_ID"]],
     f"Booked at PKR {float(c3['NOTIONAL_PKR']):,.0f}; the counterparty confirms "
     f"PKR {c3_confirmed_notional:,.0f}. The PKR 25m difference is neither "
     f"settled nor recorded.")

note("C4", "A complete document pack exists for a deal the system never "
     "recorded", "contradiction", "High", ["TD-2025-0447"],
     "Deal ticket, broker note, counterparty confirmation and payment "
     "instruction all exist for TD-2025-0447. The deal file skips that "
     "reference and no settlement row carries it.")

d1 = pick(1, **CLEAN)[0]
note("D1", "Deal ticket carries no supervisory authorisation", "document",
     "Moderate", [d1["DEAL_ID"]],
     "The dealer signed; the authorisation block is blank. The system record "
     "shows nothing wrong.")

d2 = pick(1, DEAL_TYPE="FX_SPOT", pred=lambda d: d["DEAL_STATUS"] == "Settled"
          and d["DEAL_DATE"] > "2025-02-01")[0]
note("D2", "Rate on the deal ticket was altered and the alteration is not "
     "initialled", "document", "High", [d2["DEAL_ID"]],
     "The ticket shows a struck-through rate over-written by hand. The booked "
     "rate matches the over-written figure, so no data test sees it.")

d3 = pick(1, DEAL_TYPE="MM_PLACEMENT",
          pred=lambda d: d["DEAL_STATUS"] == "Settled"
          and d["DEAL_DATE"] > "2025-04-01")[0]
note("D3", "The counterparty confirmation is an internally produced document",
     "document", "High", [d3["DEAL_ID"]],
     "The confirmation in the pack is a Meridian Bank internal print with no "
     "counterparty letterhead, no SWIFT header and no counterparty reference. "
     "The confirmation table records it as Matched.")

d4 = pick(1, pred=lambda d: d["BROKER_ID"] != "" and d["DEAL_STATUS"] == "Settled"
          and d["DEAL_DATE"] > "2025-03-15")[0]
note("D4", "Broker note absent from the pack for a brokered deal", "document",
     "Low", [d4["DEAL_ID"]],
     f"The deal records broker {d4['BROKER_ID']}; the pack contains no broker "
     f"note and brokerage was paid.")

d5 = pick(1, pred=lambda d: d["DEAL_STATUS"] == "Settled"
          and float(d["NOTIONAL_PKR"]) > 250_000_000
          and d["DEAL_DATE"] > "2025-02-01")[0]
note("D5", "Payment instruction released under a single signature above the "
     "dual-signature threshold", "document", "High", [d5["DEAL_ID"]],
     f"PKR {float(d5['NOTIONAL_PKR']):,.0f} against a PKR 250m threshold. The "
     f"settlement record names a second approver who did not sign the "
     f"instruction.")

d6 = pick(1, DEAL_TYPE="FX_FORWARD",
          pred=lambda d: d["DEAL_STATUS"] == "Settled"
          and d["DEAL_DATE"] > "2025-02-15")[0]
note("D6", "Supervisory authorisation on the deal ticket post-dates execution",
     "document", "Moderate", [d6["DEAL_ID"]],
     "The ticket was authorised two business days after the deal was struck "
     "and after the confirmation had been despatched.")

clean_packs = pick(4, **CLEAN)
note("OK", "Packs with no exception", "clean", "-",
     [d["DEAL_ID"] for d in clean_packs],
     "Complete, consistent packs. Included so the sample is not uniformly "
     "exceptional.")

# --------------------------------------------------------------------------
# renumber chronologically
# --------------------------------------------------------------------------
# Several deals were moved onto other dealing dates while exceptions were
# injected. Deal references are allocated at capture, so they have to run with
# the dates: leaving a March deal holding a January reference would plant a
# pattern nobody seeded and send an auditor down a false trail.

MALFORMED_SLOT = 661
ordered = sorted(deals, key=lambda d: (d["DEAL_DATE"], d["DEAL_TIME"], d["DEAL_ID"]))
remap: dict[str, str] = {}
counter = 0
for d in ordered:
    counter += 1
    while counter in GAP_REFS:
        counter += 1
    new_ref = ("TD-25-733" if counter == MALFORMED_SLOT
               else f"TD-2025-{counter:04d}")
    remap[d["DEAL_ID"]] = new_ref
for d in deals:
    d["DEAL_ID"] = remap[d["DEAL_ID"]]
BY_ID = {d["DEAL_ID"]: d for d in deals}
USED = {remap.get(ref, ref) for ref in USED}
for entry in GT.values():
    entry["deal_ids"] = [remap.get(ref, ref) for ref in entry["deal_ids"]]

note("X16b", "Deal reference does not follow the standard format", "data", "Low",
     ["TD-25-733"],
     f"Booked as TD-25-733 where every other reference in the file is "
     f"TD-2025-nnnn. It sits in the sequence where TD-2025-{MALFORMED_SLOT:04d} "
     f"would fall.")
note("X16a", "Gaps in the deal reference sequence", "data", "Moderate",
     sorted(f"TD-2025-{n:04d}" for n in GAP_REFS | {MALFORMED_SLOT}),
     f"Four references are absent. TD-2025-0884 is the reference a settlement "
     f"was released against; TD-2025-0447 is the deal a complete document pack "
     f"exists for; TD-2025-{MALFORMED_SLOT:04d} is empty because that deal was "
     f"booked as TD-25-733; TD-2025-0612 is unexplained.")

# the unrecorded deal's pack has to be dated where its reference falls
C4_DATE = BY_ID["TD-2025-0446"]["DEAL_DATE"]
C4_NEXT = BY_ID["TD-2025-0448"]["DEAL_DATE"]
assert C4_DATE <= C4_NEXT

# --------------------------------------------------------------------------
# standing settlement instructions
# --------------------------------------------------------------------------

BANK_OF = {
    "CP-001": "Sterling Union Bank Limited, Karachi",
    "CP-002": "Harbour National Bank, Karachi",
    "CP-003": "Pinnacle Commercial Bank, Lahore",
    "CP-004": "Horizon Bank Limited, Karachi",
    "CP-005": "Crescent Investment Bank, Karachi",
    "CP-006": "Apex Islamic Bank, Karachi",
    "CP-007": "Frontier Development Bank, Islamabad",
    "CP-008": "Northgate Bank Limited, Lahore",
    "CP-009": "Cedar Commercial Bank, Karachi",
    "CP-010": "State Bank of Pakistan, Karachi",
}
CORRESPONDENT = {
    "USD": "Citibank N.A., New York",
    "EUR": "Deutsche Bank AG, Frankfurt",
    "GBP": "Barclays Bank PLC, London",
    "AED": "Emirates NBD, Dubai",
}
NOSTRO = {
    "PKR": "SBP-CA-0041-MBL",
    "USD": "NOSTRO-USD-CITI-3610044821",
    "EUR": "NOSTRO-EUR-DB-70044120088",
    "GBP": "NOSTRO-GBP-BARC-20551188",
    "AED": "NOSTRO-AED-ENBD-1019277440",
}

settlement_currency = {}
for d in deals:
    cur = d["CURRENCY"]
    settlement_currency.setdefault(d["COUNTERPARTY_ID"], set()).add(cur)

ssis: list[dict] = []
ssi_lookup: dict[tuple[str, str], str] = {}
ssi_seq = 0
for cp, _n, _t, _r, _l, _e, _s, _sd in COUNTERPARTIES:
    for cur in sorted(settlement_currency.get(cp, {"PKR"}) | {"PKR"}):
        ssi_seq += 1
        ssi_id = f"SSI-{ssi_seq:03d}"
        ssi_lookup[(cp, cur)] = ssi_id
        acct = (f"{RNG.randrange(10, 99)}-{RNG.randrange(1000, 9999)}-"
                f"{RNG.randrange(100000, 999999)}")
        ssis.append({
            "SSI_ID": ssi_id,
            "COUNTERPARTY_ID": cp,
            "CURRENCY": cur,
            "BENEFICIARY_BANK": BANK_OF[cp] if cur == "PKR" else CORRESPONDENT[cur],
            "BENEFICIARY_ACCOUNT": acct,
            "EFFECTIVE_DATE": "2024-11-01",
            "APPROVED_BY_ID": RNG.choice(["TS-014", "TS-019"]),
            "STATUS": "Active",
        })

# --------------------------------------------------------------------------
# confirmations
# --------------------------------------------------------------------------

CONF_MODE = {
    "FX_SPOT": "SWIFT MT300",
    "FX_FORWARD": "SWIFT MT300",
    "MM_PLACEMENT": "SWIFT MT320",
    "MM_BORROWING": "SWIFT MT320",
    "TBILL_PURCHASE": "SWIFT MT518",
}
BACK_OFFICE = ["TS-015", "TS-016", "TS-020"]

confirmations: list[dict] = []
conf_by_deal: dict[str, dict] = {}
for i, d in enumerate(sorted(deals, key=lambda x: (x["DEAL_DATE"], x["DEAL_ID"])), 1):
    deal_date = date.fromisoformat(d["DEAL_DATE"])
    value_date = date.fromisoformat(d["VALUE_DATE"])
    sent = deal_date if RNG.random() < 0.84 else add_bd(deal_date, 1)
    received = sent if RNG.random() < 0.55 else add_bd(sent, 1)
    if received > value_date:
        received = value_date
    row = {
        "CONFIRMATION_ID": f"CNF-2025-{i:04d}",
        "DEAL_ID": d["DEAL_ID"],
        "CONFIRMATION_MODE": CONF_MODE[d["DEAL_TYPE"]],
        "SENT_DATE": sent.isoformat(),
        "COUNTERPARTY_RECEIVED_DATE": received.isoformat(),
        "CONFIRMED_BY_ID": RNG.choice(BACK_OFFICE),
        "MATCH_STATUS": "Cancelled" if d["DEAL_STATUS"] == "Cancelled" else "Matched",
        "DISCREPANCY_NOTE": "",
    }
    confirmations.append(row)
    conf_by_deal[d["DEAL_ID"]] = row

# --------------------------------------------------------------------------
# settlements
# --------------------------------------------------------------------------

RELEASERS = ["TS-017", "TS-018", "TS-020"]
APPROVERS = ["TS-014", "TS-018"]

settlements: list[dict] = []
stl_by_deal: dict[str, dict] = {}
for i, d in enumerate(sorted((x for x in deals if x["DEAL_STATUS"] == "Settled"),
                            key=lambda x: (x["VALUE_DATE"], x["DEAL_ID"])), 1):
    cur = d["CURRENCY"]
    ssi_id = ssi_lookup[(d["COUNTERPARTY_ID"], cur)]
    ssi = next(s for s in ssis if s["SSI_ID"] == ssi_id)
    released = RNG.choice(RELEASERS)
    approver = RNG.choice([a for a in APPROVERS if a != released])
    amount_pkr = float(d["NOTIONAL_PKR"])
    row = {
        "SETTLEMENT_ID": f"STL-2025-{i:04d}",
        "DEAL_ID": d["DEAL_ID"],
        "SETTLEMENT_DATE": d["VALUE_DATE"],
        "SETTLEMENT_CURRENCY": cur,
        "SETTLEMENT_AMOUNT": d["NOTIONAL_AMOUNT"],
        "SETTLEMENT_AMOUNT_PKR": f"{amount_pkr:.2f}",
        "SSI_ID": ssi_id,
        "BENEFICIARY_BANK": ssi["BENEFICIARY_BANK"],
        "BENEFICIARY_ACCOUNT": ssi["BENEFICIARY_ACCOUNT"],
        "NOSTRO_ACCOUNT": NOSTRO[cur],
        "RELEASED_BY_ID": released,
        "APPROVED_BY_ID": approver,
        "SECOND_APPROVER_ID": "TS-014" if amount_pkr >= 250_000_000
        and approver != "TS-014" else ("TS-019" if amount_pkr >= 250_000_000 else ""),
        "PAYMENT_REFERENCE": f"PMT-2025-{i:05d}",
    }
    settlements.append(row)
    stl_by_deal[d["DEAL_ID"]] = row

# --------------------------------------------------------------------------
# confirmation and settlement injections
# --------------------------------------------------------------------------


def settled_pool(n: int, pred=None) -> list[dict]:
    out = []
    for d in deals:
        if d["DEAL_ID"] in USED or d["DEAL_STATUS"] != "Settled":
            continue
        if d["DEAL_ID"] not in conf_by_deal or d["DEAL_ID"] not in stl_by_deal:
            continue
        if pred and not pred(d):
            continue
        out.append(d)
        if len(out) == n:
            break
    if len(out) < n:
        raise SystemExit(f"settled_pool short: {len(out)} of {n}")
    for d in out:
        USED.add(d["DEAL_ID"])
    return out


x08a = settled_pool(2, lambda d: d["DEAL_DATE"] > "2025-02-01")
for d in x08a:
    conf_by_deal[d["DEAL_ID"]]["CONFIRMED_BY_ID"] = d["DEALER_ID"]
note("X08a", "Deal captured and confirmed by the same person", "data", "High",
     [d["DEAL_ID"] for d in x08a],
     "The dealer who struck and captured the deal also signed off the "
     "counterparty confirmation.")

x08d = settled_pool(2, lambda d: d["DEAL_DATE"] > "2025-03-01")
for d in x08d:
    conf_by_deal[d["DEAL_ID"]]["CONFIRMED_BY_ID"] = (
        "TS-004" if d["DEALER_ID"] != "TS-004" else "TS-003")
note("X08d", "Confirmation signed off by a front-office member of staff", "data",
     "High", [d["DEAL_ID"] for d in x08d],
     "The confirmer is not the dealer, so an identity test passes; the staff "
     "file places them on the dealing desk.")

x08b = settled_pool(2, lambda d: d["DEAL_DATE"] > "2025-02-10")
for d in x08b:
    stl = stl_by_deal[d["DEAL_ID"]]
    stl["APPROVED_BY_ID"] = stl["RELEASED_BY_ID"]
note("X08b", "Settlement released and approved by the same person", "data",
     "High", [d["DEAL_ID"] for d in x08b], "No second pair of eyes on release.")

x08c = settled_pool(1, lambda d: d["DEAL_DATE"] > "2025-04-01")
stl_by_deal[x08c[0]["DEAL_ID"]]["APPROVED_BY_ID"] = x08c[0]["DEALER_ID"]
note("X08c", "Settlement approved by the dealer who struck the deal", "data",
     "High", [x08c[0]["DEAL_ID"]],
     "Front office authorising the movement of its own funds.")

# On a long-dated deal a late confirmation is still received before value date,
# which keeps the despatch-deadline test separable from the settled-unconfirmed
# one. A money-market tenor does not help here: those value at T+1.
x10a = settled_pool(4, lambda d: d["DEAL_DATE"] > "2025-01-20"
                    and (date.fromisoformat(d["VALUE_DATE"])
                         - date.fromisoformat(d["DEAL_DATE"])).days >= 25)
for d, lag in zip(x10a, (3, 4, 6, 5)):
    conf = conf_by_deal[d["DEAL_ID"]]
    sent = add_bd(date.fromisoformat(d["DEAL_DATE"]), lag)
    conf["SENT_DATE"] = sent.isoformat()
    conf["COUNTERPARTY_RECEIVED_DATE"] = add_bd(sent, 1).isoformat()
note("X10a", "Confirmation despatched outside the one-business-day deadline",
     "data", "Moderate", [d["DEAL_ID"] for d in x10a],
     "Despatched three to six business days after the deal.")

x10b = settled_pool(3, lambda d: d["DEAL_DATE"] > "2025-02-20")
for d in x10b:
    conf = conf_by_deal[d["DEAL_ID"]]
    conf["COUNTERPARTY_RECEIVED_DATE"] = add_bd(
        date.fromisoformat(d["VALUE_DATE"]), 2).isoformat()
note("X10b", "Deal settled before the counterparty confirmation was received",
     "data", "High", [d["DEAL_ID"] for d in x10b],
     "Funds moved on an unconfirmed deal; the confirmation arrived two "
     "business days after value date.")

x10c = settled_pool(3, lambda d: d["DEAL_DATE"] > "2025-03-10")
for d, txt in zip(x10c, (
        "Counterparty confirms value date one business day later than booked.",
        "Counterparty confirms a different tenor; unresolved at settlement.",
        "Counterparty reference not quoted; amount agreed verbally.")):
    conf = conf_by_deal[d["DEAL_ID"]]
    conf["MATCH_STATUS"] = "Discrepancy"
    conf["DISCREPANCY_NOTE"] = txt
note("X10c", "Deal settled while its confirmation was flagged as discrepant",
     "data", "High", [d["DEAL_ID"] for d in x10c],
     "The discrepancy was never cleared and the settlement went ahead anyway.")

x11 = settled_pool(3, lambda d: d["DEAL_DATE"] > "2025-01-25")
for d, shift in zip(x11, (2, 5, -1)):
    stl = stl_by_deal[d["DEAL_ID"]]
    value = date.fromisoformat(d["VALUE_DATE"])
    stl["SETTLEMENT_DATE"] = (add_bd(value, shift) if shift > 0
                              else bd_range(value - timedelta(days=6), value)[-2]).isoformat()
note("X11", "Settlement did not fall on the contracted value date", "data",
     "Moderate", [d["DEAL_ID"] for d in x11],
     "Two settled late by two and five business days; one settled a business "
     "day early.")

x13 = settled_pool(3, lambda d: d["DEAL_DATE"] > "2025-02-05")
for d in x13:
    confirmations = [c for c in confirmations if c["DEAL_ID"] != d["DEAL_ID"]]
    conf_by_deal.pop(d["DEAL_ID"], None)
note("X13", "No confirmation record exists for a settled deal", "data", "High",
     [d["DEAL_ID"] for d in x13],
     "The deal settled with nothing on file to show the counterparty agreed "
     "its terms.")

x14a = settled_pool(2, lambda d: d["DEAL_DATE"] > "2025-03-20")
for d in x14a:
    settlements = [s for s in settlements if s["DEAL_ID"] != d["DEAL_ID"]]
    stl_by_deal.pop(d["DEAL_ID"], None)
note("X14a", "Deal marked settled with no settlement record", "data", "Moderate",
     [d["DEAL_ID"] for d in x14a],
     "Nothing in the settlement file evidences that the funds moved.")

orphan_ref = "TD-2025-0884"
settlements.append({
    "SETTLEMENT_ID": "STL-2025-0949",
    "DEAL_ID": orphan_ref,
    "SETTLEMENT_DATE": "2025-05-27",
    "SETTLEMENT_CURRENCY": "PKR",
    "SETTLEMENT_AMOUNT": "185000000.00",
    "SETTLEMENT_AMOUNT_PKR": "185000000.00",
    "SSI_ID": ssi_lookup[("CP-006", "PKR")],
    "BENEFICIARY_BANK": BANK_OF["CP-006"],
    "BENEFICIARY_ACCOUNT": next(s["BENEFICIARY_ACCOUNT"] for s in ssis
                                if s["SSI_ID"] == ssi_lookup[("CP-006", "PKR")]),
    "NOSTRO_ACCOUNT": NOSTRO["PKR"],
    "RELEASED_BY_ID": "TS-017",
    "APPROVED_BY_ID": "TS-014",
    "SECOND_APPROVER_ID": "",
    "PAYMENT_REFERENCE": "PMT-2025-00949",
})
note("X14b", "Settlement recorded against a deal that does not exist", "data",
     "High", [orphan_ref],
     "PKR 185,000,000 released on 27 May 2025 against deal reference "
     "TD-2025-0884, which the deal file does not contain.")

x17a = settled_pool(2, lambda d: d["DEAL_DATE"] > "2025-04-05"
                    and d["CURRENCY"] == "PKR")
for d, acct in zip(x17a, ("31-8842-770519", "44-1207-336284")):
    stl = stl_by_deal[d["DEAL_ID"]]
    stl["SSI_ID"] = ""
    stl["BENEFICIARY_ACCOUNT"] = acct
    stl["BENEFICIARY_BANK"] = "Meridian Bank Limited, Clifton Branch"
note("X17a", "Settlement routed to an account with no standing instruction",
     "data", "High", [d["DEAL_ID"] for d in x17a],
     "Both payments went to accounts absent from the SSI file, and both "
     "beneficiary banks differ from the counterparty's.")

x17b = settled_pool(1, lambda d: d["COUNTERPARTY_ID"] == "CP-006"
                    and d["CURRENCY"] == "PKR" and d["DEAL_DATE"] > "2025-05-01")
x17b_stl = stl_by_deal[x17b[0]["DEAL_ID"]]
amend_effective = bd_range(date.fromisoformat(x17b_stl["SETTLEMENT_DATE"])
                           - timedelta(days=6),
                           date.fromisoformat(x17b_stl["SETTLEMENT_DATE"]))[-2]
old_ssi = next(s for s in ssis if s["SSI_ID"] == ssi_lookup[("CP-006", "PKR")])
old_ssi["STATUS"] = "Superseded"
ssi_seq += 1
new_ssi_id = f"SSI-{ssi_seq:03d}"
ssis.append({
    "SSI_ID": new_ssi_id,
    "COUNTERPARTY_ID": "CP-006",
    "CURRENCY": "PKR",
    "BENEFICIARY_BANK": BANK_OF["CP-006"],
    "BENEFICIARY_ACCOUNT": "58-3390-114772",
    "EFFECTIVE_DATE": amend_effective.isoformat(),
    "APPROVED_BY_ID": "TS-014",
    "STATUS": "Active",
})
for stl in settlements:
    if (stl["SSI_ID"] == old_ssi["SSI_ID"]
            and stl["SETTLEMENT_DATE"] >= amend_effective.isoformat()):
        stl["SSI_ID"] = new_ssi_id
        stl["BENEFICIARY_ACCOUNT"] = "58-3390-114772"
note("X17b", "Settlement made inside the standing-instruction cooling-off "
     "period", "data", "High", [x17b[0]["DEAL_ID"]],
     f"CP-006's PKR instruction was amended with effect from "
     f"{amend_effective.isoformat()} and paid on "
     f"{x17b_stl['SETTLEMENT_DATE']}, one business day later, against a "
     f"two-business-day cooling-off requirement.")

x18 = settled_pool(2, lambda d: d["DEAL_TYPE"] == "FX_SPOT"
                   and d["DEAL_DATE"] > "2025-03-01")
for d, factor in zip(x18, (1.018, 0.991)):
    stl = stl_by_deal[d["DEAL_ID"]]
    stl["SETTLEMENT_AMOUNT_PKR"] = f"{float(d['NOTIONAL_PKR']) * factor:.2f}"
note("X18", "Settlement amount does not agree to notional multiplied by the "
     "dealt rate", "data", "High", [d["DEAL_ID"] for d in x18],
     "One overpaid by 1.8% and one underpaid by 0.9% against the contracted "
     "terms.")

note("X06", "Deals struck by a dealer with no row in the limit file", "data",
     "Moderate", [],
     f"TS-009 Kamran Yousuf executed "
     f"{sum(1 for d in deals if d['DEALER_ID'] == 'TS-009')} T-bill deals in "
     f"the period. The dealer limit file has no row for TS-009, so no deal "
     f"of his can be tested for authority at all. This is an inconclusive "
     f"result, not a pass and not a failure.")

# --------------------------------------------------------------------------
# final verification: nothing unintended crept into the populations
# --------------------------------------------------------------------------

final_profile = cp_profile()
for cp, days in final_profile.items():
    peak = max(days.values())
    ratio = peak / CP_LIMIT[cp]
    if cp == "CP-005":
        assert 1.02 <= ratio <= 1.15, f"CP-005 breach out of range: {ratio:.1%}"
    else:
        assert ratio <= 1.0, f"unintended breach {cp} at {ratio:.1%}"
print(f"CP-005 final utilisation {max(final_profile['CP-005'].values()) / CP_LIMIT['CP-005']:.1%}")
print(f"CP-009 recorded utilisation "
      f"{max(final_profile['CP-009'].values()) / CP_LIMIT['CP-009']:.1%}; "
      f"with the concealed deal "
      f"{(max(final_profile['CP-009'].values()) + float(c1['NOTIONAL_PKR'])) / CP_LIMIT['CP-009']:.1%}")

# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

OUT.mkdir(parents=True, exist_ok=True)


def write_csv(name: str, header: list[str], rows: list) -> None:
    path = OUT / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row if isinstance(row, (list, tuple))
                            else [row[c] for c in header])
    print(f"  {name}: {len(rows)} rows")


write_csv("01_staff.csv",
          ["STAFF_ID", "NAME", "JOB_TITLE", "DESK", "SUPERVISOR_ID"], STAFF)
write_csv("02_dealer_limits.csv",
          ["DEALER_ID", "PER_DEAL_LIMIT_PKR", "DAILY_LIMIT_PKR",
           "AUTHORISED_PRODUCTS", "AUTHORISATION_EXPIRY_DATE"], DEALER_LIMITS)
write_csv("03_counterparties.csv",
          ["COUNTERPARTY_ID", "COUNTERPARTY_NAME", "COUNTERPARTY_TYPE",
           "CREDIT_RATING", "EXPOSURE_LIMIT_PKR", "LIMIT_EXPIRY_DATE", "STATUS",
           "STATUS_EFFECTIVE_DATE"], COUNTERPARTIES)

deal_header = ["DEAL_ID", "DEAL_DATE", "DEAL_TIME", "DEAL_TYPE", "INSTRUMENT",
               "TENOR", "COUNTERPARTY_ID", "DEALER_ID", "BROKER_ID", "CURRENCY",
               "NOTIONAL_AMOUNT", "DEAL_RATE", "NOTIONAL_PKR", "VALUE_DATE",
               "MATURITY_DATE", "DEAL_STATUS", "CAPTURED_BY_ID",
               "CAPTURE_TIMESTAMP", "AMENDED_FLAG", "AMENDMENT_DATE",
               "AMENDMENT_APPROVED_BY_ID"]
deals_sorted = sorted(deals, key=lambda d: (d["DEAL_DATE"], d["DEAL_TIME"]))
write_csv("04_deals.csv", deal_header, deals_sorted)

write_csv("05_confirmations.csv",
          ["CONFIRMATION_ID", "DEAL_ID", "CONFIRMATION_MODE", "SENT_DATE",
           "COUNTERPARTY_RECEIVED_DATE", "CONFIRMED_BY_ID", "MATCH_STATUS",
           "DISCREPANCY_NOTE"],
          sorted(confirmations, key=lambda c: c["CONFIRMATION_ID"]))

write_csv("06_settlements.csv",
          ["SETTLEMENT_ID", "DEAL_ID", "SETTLEMENT_DATE", "SETTLEMENT_CURRENCY",
           "SETTLEMENT_AMOUNT", "SETTLEMENT_AMOUNT_PKR", "SSI_ID",
           "BENEFICIARY_BANK", "BENEFICIARY_ACCOUNT", "NOSTRO_ACCOUNT",
           "RELEASED_BY_ID", "APPROVED_BY_ID", "SECOND_APPROVER_ID",
           "PAYMENT_REFERENCE"],
          sorted(settlements, key=lambda s: s["SETTLEMENT_ID"]))

rate_rows = []
for (day, instrument, tenor), values in sorted(market.items()):
    rate_rows.append([day, instrument, tenor, f"{values['mid']:.4f}",
                      f"{values['high']:.4f}", f"{values['low']:.4f}"])
write_csv("07_market_rates.csv",
          ["RATE_DATE", "INSTRUMENT", "TENOR", "MID_RATE", "DAY_HIGH", "DAY_LOW"],
          rate_rows)

write_csv("08_standing_settlement_instructions.csv",
          ["SSI_ID", "COUNTERPARTY_ID", "CURRENCY", "BENEFICIARY_BANK",
           "BENEFICIARY_ACCOUNT", "EFFECTIVE_DATE", "APPROVED_BY_ID", "STATUS"],
          sorted(ssis, key=lambda s: s["SSI_ID"]))

write_csv("09_brokers.csv",
          ["BROKER_ID", "BROKER_NAME", "PANEL_SEGMENT", "STATUS"], BROKERS)

write_csv("10_policy_parameters.csv",
          ["PARAMETER_ID", "PARAMETER", "VALUE", "UNIT", "POLICY_CLAUSE"], POLICY)

# --------------------------------------------------------------------------
# ground truth for the facilitator guide and the document generator
# --------------------------------------------------------------------------

PACKS = {
    "C1": c1["DEAL_ID"], "C2": c2["DEAL_ID"], "C3": c3["DEAL_ID"],
    "C4": "TD-2025-0447",
    "D1": d1["DEAL_ID"], "D2": d2["DEAL_ID"], "D3": d3["DEAL_ID"],
    "D4": d4["DEAL_ID"], "D5": d5["DEAL_ID"], "D6": d6["DEAL_ID"],
    "X01": GT["X01"]["deal_ids"][0],
    "X04a": GT["X04a"]["deal_ids"][0],
    "X07": GT["X07"]["deal_ids"][0],
    "X10b": GT["X10b"]["deal_ids"][0],
    "OK1": clean_packs[0]["DEAL_ID"], "OK2": clean_packs[1]["DEAL_ID"],
    "OK3": clean_packs[2]["DEAL_ID"], "OK4": clean_packs[3]["DEAL_ID"],
}

payload = {
    "entity": "Meridian Bank Limited",
    "period": [PERIOD_START.isoformat(), PERIOD_END.isoformat()],
    "deal_count": len(deals),
    "ground_truth": GT,
    "packs": PACKS,
    "pack_extras": {
        "C2_confirmed_rate": f"{c2_confirmed_rate:.4f}",
        "C3_confirmed_notional": f"{c3_confirmed_notional:.2f}",
        "cp009_limit": cp009_limit,
        "cp009_peak": cp009_peak,
        "cp009_peak_day": cp009_peak_day,
        "orphan_settlement_ref": orphan_ref,
        "ssi_amendment_effective": amend_effective.isoformat(),
    },
    "deals": {d["DEAL_ID"]: d for d in deals},
    "confirmations": {c["DEAL_ID"]: c for c in confirmations},
    "settlements": {s["DEAL_ID"]: s for s in settlements},
    "staff": {row[0]: {"name": row[1], "title": row[2], "desk": row[3]}
              for row in STAFF},
    "counterparties": {row[0]: {"name": row[1], "type": row[2], "rating": row[3],
                                "limit": row[4], "status": row[6]}
                       for row in COUNTERPARTIES},
    "brokers": {row[0]: row[1] for row in BROKERS},
    "ssis": ssis,
    "market": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in market.items()},
}
GT_PATH.write_text(json.dumps(payload, indent=1))

flagged = sorted({ref for entry in GT.values() if entry["class"] == "data"
                  for ref in entry["deal_ids"]})
print(f"\nseeded exception types: {len(GT)}")
print(f"deal references carrying a data-visible exception: {len(flagged)}")
print(f"document-only packs: {sum(1 for e in GT.values() if e['class'] == 'document')}")
print(f"contradiction packs: {sum(1 for e in GT.values() if e['class'] == 'contradiction')}")
