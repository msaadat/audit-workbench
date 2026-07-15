"""Shared privacy projection for all model-visible structured data.

Column names and types remain useful schema metadata, but identifier-like
values (including aggregates and category labels) are never model context.
Classification is deliberately conservative and happens locally.
"""

from __future__ import annotations

import re
from typing import Iterable

import polars as pl


SENSITIVE_NAME_PARTS = {
    "account", "acct", "iban", "swift", "bic", "routing", "sortcode",
    "banknumber", "card", "pan", "cvv", "cvc", "passport", "nationalid",
    "governmentid", "govtid", "taxid", "tin", "ssn", "socialsecurity",
    "phone", "mobile", "telephone", "email", "emailaddress", "contactno",
    "cnic", "nic", "aadhaar", "license", "licence", "ipaddress",
}
IDENTIFIER_SUFFIXES = ("id", "number", "no", "num", "code")

_EMAIL = re.compile(r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$")
_PHONE = re.compile(r"^\+?\d[\d ()-]{7,}\d$")
_CARD = re.compile(r"^(?:\d[ -]?){13,19}$")
_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$", re.I)
_LONG_ID = re.compile(r"^[A-Z0-9-]{8,40}$", re.I)


def normalize_name(name: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").casefold())


def sensitive_name(name: object) -> bool:
    normalized = normalize_name(name)
    if not normalized:
        return False
    if any(part in normalized for part in SENSITIVE_NAME_PARTS):
        return True
    # Generic identifiers are high risk unless clearly an audit/workbench ref.
    safe_ids = {"rowid", "itemid", "findingid", "procedureid", "testid"}
    return normalized not in safe_ids and normalized.endswith(IDENTIFIER_SUFFIXES)


def sensitive_value(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = re.sub(r"[ -]", "", text)
    if _EMAIL.fullmatch(text) or _PHONE.fullmatch(text) or _IBAN.fullmatch(compact):
        return True
    if _CARD.fullmatch(text):
        digits = re.sub(r"\D", "", text)
        return 13 <= len(digits) <= 19
    # Long, mostly unique identifier-shaped strings are treated as sensitive
    # when sampled from a column. This intentionally does not classify prose.
    return bool(_LONG_ID.fullmatch(text) and any(c.isdigit() for c in text))


def classify_column(
    name: object,
    values: Iterable[object] = (),
    *,
    distinct: int | None = None,
    rows: int | None = None,
) -> str:
    if sensitive_name(name):
        return "sensitive_identifier"
    sampled = [value for value in values if value not in (None, "")][:20]
    pattern_hits = sum(1 for value in sampled if sensitive_value(value))
    if sampled and pattern_hits >= max(2, (len(sampled) + 1) // 2):
        return "sensitive_identifier"
    # Near-unique numeric/string fields with an identifier suffix were already
    # caught above. Do not guess that ordinary measures are identifiers.
    return "ordinary"


def frame_column_classes(df: pl.DataFrame) -> dict[str, str]:
    classes: dict[str, str] = {}
    for name in df.columns:
        try:
            values = df[name].drop_nulls().head(20).to_list()
            distinct = int(df[name].n_unique())
        except Exception:
            values, distinct = [], None
        classes[name] = classify_column(name, values, distinct=distinct, rows=df.height)
    return classes


def project_frame(df: pl.DataFrame, *, allow_rows: bool, row_limit: int = 40) -> dict:
    """Return one compact, privacy-safe model view of a result frame."""
    classes = frame_column_classes(df)
    summary: dict[str, dict] = {}
    for name, dtype in df.schema.items():
        if dtype.is_numeric() and classes[name] == "ordinary":
            col = df[name]
            summary[name] = {
                "min": _round(col.min()), "max": _round(col.max()),
                "mean": _round(col.mean()), "nulls": int(col.null_count()),
            }
    view: dict = {
        "shape": [df.height, df.width],
        "columns": df.columns,
        "dtypes": [str(value) for value in df.dtypes],
        "classifications": classes,
        "numeric_summary": summary,
    }
    if allow_rows and df.height <= row_limit:
        rows = []
        for row in df.iter_rows():
            rows.append([
                "[sensitive identifier withheld]" if classes[name] != "ordinary" else _json(value)
                for name, value in zip(df.columns, row)
            ])
        view["rows"] = rows
    else:
        view["note"] = (
            "Row-level values are withheld (raw, large, or identifier-sensitive result). "
            "The auditor sees the complete local result."
        )
    return view


def project_column_profile(profile: dict) -> dict:
    """Privacy-safe projection of one profiler column payload."""
    top_values = [item.get("value") for item in profile.get("top_values") or []]
    classification = classify_column(
        profile.get("name"), top_values,
        distinct=profile.get("distinct_count"), rows=profile.get("rows"),
    )
    meta = {
        "name": profile["name"], "dtype": profile["dtype"],
        "type": profile["inferred_type"], "nulls_pct": profile["blank_pct"],
        "distinct": profile["distinct_count"], "classification": classification,
    }
    if classification == "ordinary" and profile["inferred_type"] in ("numeric", "date"):
        meta.update(min=profile.get("min"), max=profile.get("max"))
        if profile.get("mean") is not None:
            meta["mean"] = profile["mean"]
    if (
        classification == "ordinary"
        and profile["distinct_count"] <= 30
        and profile.get("top_values")
    ):
        meta["values"] = top_values
    return meta


def scrub_text(text: object) -> str:
    """Mask embedded high-risk identifiers in model-visible narrative."""
    value = str(text or "")
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email withheld]", value)
    value = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[number withheld]", value)
    value = re.sub(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", "[account withheld]", value, flags=re.I)
    return value


def _round(value):
    return round(value, 4) if isinstance(value, float) else value


def _json(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
