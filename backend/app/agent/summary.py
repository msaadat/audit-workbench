"""Findings assembly and summary rendering for agent runs.

The runner collects evidence (aggregate results of everything it created)
into a compact structure; the LLM turns it into findings + markdown. When the
model fails or is unavailable, :func:`fallback_markdown` renders a plain
deterministic report from the same evidence so a run always ends with a
usable summary.
"""

from __future__ import annotations

SEVERITIES = ("high", "medium", "low", "info")


def clean_findings(raw, valid_refs: set[str]) -> list[dict]:
    """Validate model-produced findings: keep only known evidence refs, coerce
    severities, and drop entries without a statement."""
    findings = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement") or "").strip()
        if not statement:
            continue
        severity = str(item.get("severity") or "info").lower()
        refs = [
            str(r) for r in (item.get("evidence_refs") or []) if str(r) in valid_refs
        ]
        findings.append(
            {
                "id": f"f{index + 1}",
                "severity": severity if severity in SEVERITIES else "info",
                "statement": statement,
                "basis": "observed" if item.get("basis") == "observed" else "interpretation",
                "evidence_refs": refs,
            }
        )
    return findings


def fallback_markdown(run: dict, evidence: dict) -> str:
    """A deterministic summary used when the model can't produce one."""
    discovery = run.get("discovery") or {}
    lines = [
        "# Analyst Summary (generated without model narrative)",
        "",
        "## Scope & Domain",
        f"- Inferred domain: {discovery.get('domain') or 'unknown'} "
        f"(confidence: {discovery.get('confidence') or 'low'})",
        f"- Mode: {run['mode']}",
        "",
        "## Data & Relationships",
    ]
    for table in evidence.get("tables", []):
        lines.append(f"- {table['table']}: {table['rows']} rows")
    for join in evidence.get("joins", []):
        d = join.get("diagnostics") or {}
        lines.append(
            f"- Join {join.get('name')}: {join.get('left')} → {join.get('right')} "
            f"(match rate {d.get('match_rate')}, ref: {join.get('ref')})"
        )
    assumptions = discovery.get("assumptions") or []
    warnings = (run.get("warnings") or []) + (discovery.get("warnings") or [])
    lines += ["", "## Assumptions & Limitations"]
    lines += [f"- {a}" for a in assumptions] or ["- None recorded."]
    lines += [f"- {w}" for w in warnings]
    lines += ["", "## Validation Results"]
    for ruleset in evidence.get("rulesets", []):
        run_info = ruleset.get("last_run") or {}
        lines.append(
            f"- {ruleset['title']}: verdict {run_info.get('verdict', 'not run')} "
            f"({run_info.get('counts', {})}, ref: {ruleset['ref']})"
        )
    lines += ["", "## Analytical Findings"]
    notable = [
        a for a in evidence.get("analyses", []) if a.get("verdict") in ("warn", "fail")
    ]
    for item in notable or evidence.get("analyses", []):
        verdict = item.get("verdict") or "info"
        lines.append(
            f"- {item['title']}: {verdict} — {item.get('verdict_text') or ''} "
            f"(ref: {item['ref']})"
        )
    if not evidence.get("analyses"):
        lines.append("- No analyses were completed.")
    lines += [
        "",
        "## Recommended Follow-ups",
        "- Review warned/failed items above and drill into their saved artifacts.",
    ]
    return "\n".join(lines)
