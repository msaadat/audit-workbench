"""Deterministic, profile-based validation-rule suggestions.

Conservative by design: only checks the current data already satisfies are
suggested, so accepting a suggestion never turns the grid red on day one —
the rules exist to catch *future* regressions in refreshed data. This is the
backend twin of the Validation tab's ghost suggestions; both the UI and the
agent call it so there is a single source of truth.

Everything here derives from the cached table profile (aggregate statistics
only) — no LLM involvement, no raw rows.
"""

from __future__ import annotations

from datetime import date

from ..workspaces import Workspace

# Suggest allowed_values only for tightly bounded categorical columns.
ALLOWED_VALUES_MAX = 12
# A column whose name matches nothing date-like still gets date suggestions
# when the profiler typed it as a date.
NOT_IN_FUTURE_SLACK_DAYS = 0


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def suggest_rules(workspace: Workspace, table: str) -> list[dict]:
    """Suggested rules for one table, each with a short rationale. Shapes match
    the validation grid's rule dicts (sans id) plus ``rationale``."""
    profile = workspace.get_profile(table)
    suggestions: list[dict] = []

    for column in profile["column_profiles"]:
        name = column["name"]
        kind = column["inferred_type"]
        blank_pct = column.get("blank_pct") or 0.0
        distinct = column.get("distinct_count") or 0
        total = column.get("total") or 0
        non_blank = total - (column.get("blank_count") or 0)

        if kind == "empty":
            continue

        if blank_pct == 0.0 and total:
            suggestions.append(
                {
                    "column": name,
                    "check": "required",
                    "params": {},
                    "severity": "fail",
                    "rationale": "No blanks today — protect against future gaps.",
                }
            )

        if kind in ("numeric", "id") and column.get("min") is not None:
            low = _num(column["min"])
            if low is not None and low >= 0 and kind == "numeric":
                mode = "positive" if low > 0 else "non_negative"
                suggestions.append(
                    {
                        "column": name,
                        "check": "numeric_sign",
                        "params": {"mode": mode},
                        "severity": "fail",
                        "rationale": f"All current values are {'> 0' if low > 0 else '≥ 0'}.",
                    }
                )

        if kind == "date" and column.get("max") is not None:
            try:
                latest = date.fromisoformat(str(column["max"])[:10])
            except ValueError:
                latest = None
            if latest is not None and latest <= date.today():
                suggestions.append(
                    {
                        "column": name,
                        "check": "date_range",
                        "params": {"not_in_future": True},
                        "severity": "warn",
                        "rationale": "No future dates today — flag any that appear.",
                    }
                )

        if (
            kind in ("id", "numeric", "text")
            and non_blank > 1
            and distinct == non_blank
        ):
            suggestions.append(
                {
                    "column": name,
                    "check": "unique",
                    "params": {},
                    "severity": "fail",
                    "rationale": "Every current value is distinct — looks like a key.",
                }
            )

        if (
            kind == "categorical"
            and 0 < distinct <= ALLOWED_VALUES_MAX
            and len(column.get("top_values") or []) == distinct
        ):
            values = [v["value"] for v in column["top_values"]]
            suggestions.append(
                {
                    "column": name,
                    "check": "allowed_values",
                    "params": {"values": values},
                    "severity": "warn",
                    "rationale": f"Only {distinct} distinct values — lock the code list.",
                }
            )

    return suggestions
