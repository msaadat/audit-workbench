"""Evidence-linked audit-report generation, reconciliation, and quality checks."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

from . import data_tests, debug_store, doc_tests, llm, rcm_execution, templates_store
from .documents import append_activity
from .findings import artifact, support_issues
from .workspaces import Workspace, WorkspaceError

REQUIRED_FINDING_FIELDS = ("condition", "criteria", "cause", "effect", "recommendation")
MODEL_CONTEXT_LIMIT = 30_000
_PLANNING_FIELDS = ("objective", "entity", "period", "scope", "materiality")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hydrate(workspace: Workspace) -> dict:
    defaults = {
        "markdown": "",
        "generated_markdown": "",
        "generated_at": None,
        "generated_by_run": None,
        "edited": False,
        "updated": None,
        "generation_warnings": [],
        "workflow_parent_sha1": None,
    }
    stored = workspace.report or {}
    return {**defaults, **{key: stored[key] for key in defaults if key in stored}}


def _safe_finding(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "id", "title", "severity", "condition", "criteria", "cause", "effect",
            "recommendation", "management_response", "rcm_refs",
            "planned_test_refs", "execution_refs", "cause_pending",
            "severity_rationale", "auditor_confirmed", "source",
        )
    } | {
        "evidence": [
            {
                "id": ref.get("id"), "source_kind": ref.get("source_kind"),
                "source_id": ref.get("source_id"), "source_sha1": ref.get("source_sha1"),
                "page": ref.get("page"), "field": ref.get("field"),
            }
            for ref in item.get("evidence_refs") or []
        ]
    }


def _apm_context(markdown: str) -> dict:
    """Narrow fallback for older workspaces whose APM predates context storage."""
    labels = {
        "objective": "objective", "entity": "entity", "period": "period",
        "scope": "scope", "materiality": "materiality",
        "objective & scope": "objective_scope",
    }
    recovered = {}
    pattern = re.compile(r"^\s*[-*]?\s*(?:\*\*)?([^:*]+?)(?:\*\*)?\s*:\s*(.+?)\s*$")
    for line in str(markdown or "").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        key = labels.get(re.sub(r"\s+", " ", match.group(1).strip().casefold()))
        value = match.group(2).strip()
        if not key or not value:
            continue
        if key == "objective_scope":
            recovered.setdefault("objective", value)
            recovered.setdefault("scope", value)
        else:
            recovered.setdefault(key, value)
    return recovered


def _planning_context(workspace: Workspace) -> dict:
    structured = dict(workspace.planning.get("context") or {})
    fallback = _apm_context(str(workspace.planning.get("apm_markdown") or ""))
    return {
        key: structured.get(key) or fallback.get(key)
        for key in _PLANNING_FIELDS
    }


def build_context(workspace: Workspace) -> dict:
    """Build report context without structured rows or document excerpts."""
    rolled = rcm_execution.rollup(workspace)
    completion = rcm_execution.completion(workspace)
    tests = doc_tests.list_tests(workspace)
    test_summaries = []
    totals = {"items": 0, "exceptions": 0, "manual_review": 0, "pending": 0}
    for summary in tests:
        test = doc_tests.load_test(workspace, summary["id"])
        rollup = doc_tests.result_rollup(test)
        for key in totals:
            totals[key] += int(rollup.get(key) or 0)
        test_summaries.append(
            {
                "id": test["id"], "title": test.get("title"), "kind": test.get("kind"),
                "status": test.get("status"), "rcm_refs": test.get("rcm_refs") or [],
                "rcm_id": test.get("rcm_id"),
                "planned_test_id": test.get("planned_test_id"), "rollup": rollup,
            }
        )
    context = _planning_context(workspace)
    data_test_summaries = []
    for item in workspace.data_tests:
        if not item.get("rcm_id") or not item.get("planned_test_id"):
            continue
        latest = None
        if item.get("last_run"):
            result = data_tests.load_result(workspace, item["id"], item["last_run"]["id"])
            latest = {
                key: result.get(key)
                for key in (
                    "id", "status", "verdict", "verdict_text", "statistics",
                    "exception_count", "semantic_valid", "semantic_issues", "result_sha1",
                )
            }
        data_test_summaries.append({
            "id": item["id"], "title": item.get("title"), "objective": item.get("objective"),
            "engine": item.get("engine"), "status": item.get("status"),
            "rcm_id": item.get("rcm_id"), "planned_test_id": item.get("planned_test_id"),
            "latest_result": latest,
        })
    supported = [
        item for item in workspace.findings
        if item.get("auditor_confirmed") and not support_issues(workspace, item)
    ]
    risk_distribution = {
        rating: sum(
            str(item.get("risk_rating") or "").casefold() == rating
            for item in workspace.rcm
        )
        for rating in ("critical", "high", "medium", "low")
    }
    return {
        "workspace": {"id": workspace.id, "name": workspace.name, "description": workspace.description},
        "planning": {
            "objective": context.get("objective"), "entity": context.get("entity"),
            "period": context.get("period"), "scope": context.get("scope"),
            "materiality": context.get("materiality"),
        },
        "rcm": [
            {
                "id": item.get("id"), "process": item.get("process"),
                "risk": item.get("risk"), "risk_rating": item.get("risk_rating"),
                "assertion": item.get("assertion"), "control": item.get("control"),
                "control_conclusion": (item.get("execution_rollup") or {}).get("control_conclusion"),
                "review_status": item.get("review_status"),
                "planned_tests": [
                    {
                        key: planned.get(key)
                        for key in (
                            "id", "title", "objective", "criteria", "method", "steps",
                            "expected_evidence", "execution_refs", "status", "result_summary",
                            "conclusion", "control_conclusion", "scope_limitations",
                            "exception_count", "open_exception_count", "finding_refs",
                        )
                    }
                    for planned in item.get("planned_tests") or []
                ],
            }
            for item in workspace.rcm
        ],
        "rcm_rollup": rolled["rows"],
        "data_tests": data_test_summaries,
        "document_tests": test_summaries,
        "findings": [_safe_finding(item) for item in supported],
        "draft_findings_excluded": [item["id"] for item in workspace.findings if item not in supported],
        "scope_limitations": [
            {"rcm_id": row["id"], "planned_test_id": planned["id"], "text": planned.get("scope_limitations")}
            for row in workspace.rcm
            for planned in row.get("planned_tests") or []
            if str(planned.get("scope_limitations") or "").strip()
        ],
        "completion": completion,
        "preliminary": completion["status"] != "completed",
        "statistics": {
            "rcm_rows": len(workspace.rcm),
            "risk_distribution": risk_distribution,
            "planned_tests": sum(len(row.get("planned_tests") or []) for row in workspace.rcm),
            "data_tests": len(data_test_summaries), "findings": len(supported),
            "draft_findings": len(workspace.findings) - len(supported),
            "document_tests": len(tests), **totals,
        },
    }


def _finding_link(item: dict) -> str:
    return f"[Finding {item['id']}](?tab=findings&finding={quote(str(item['id']))})"


def _finding_citations(markdown: str) -> set[str]:
    citations = {
        match.group(1)
        for match in re.finditer(r"\?tab=findings&finding=([A-Za-z0-9_-]+)", markdown)
    }
    citations.update(
        match.group(1)
        for match in re.finditer(r"\[\s*(?:Finding\s+)?(F-[A-Za-z0-9_-]+)\s*\](?!\s*\()", markdown)
    )
    return citations


def _normalize_finding_citations(workspace: Workspace, markdown: str) -> str:
    normalized = str(markdown)
    for finding in workspace.findings:
        finding_id = str(finding["id"])
        pattern = re.compile(rf"\[\s*(?:Finding\s+)?{re.escape(finding_id)}\s*\](?!\s*\()")
        normalized = pattern.sub(_finding_link(finding), normalized)
    return normalized


def _ensure_preliminary_label(markdown: str, preliminary: bool) -> str:
    if not preliminary:
        return markdown
    body = re.sub(
        r"^#\s+.*$", "# Preliminary Internal Audit Working Draft",
        str(markdown or "").strip(), count=1, flags=re.MULTILINE,
    )
    banner = (
        "> **Preliminary working draft:** fieldwork, evidence, review, or auditor "
        "judgment remains open. This document is not a final audit opinion."
    )
    if "preliminary working draft" not in body.casefold():
        first_break = body.find("\n")
        if first_break < 0:
            body = body + "\n\n" + banner
        else:
            body = body[:first_break] + "\n\n" + banner + body[first_break:]
    return body.strip() + "\n"


def _template_order(workspace: Workspace, generated: str, context: dict) -> str:
    """Apply configured report heading order to the deterministic fallback."""
    template = templates_store.get_template(workspace, "report")["markdown"]
    matches = list(re.finditer(r"^##\s+(.+)$", generated, re.MULTILINE))
    generated_sections: dict[str, str] = {}
    generated_order: list[str] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(generated)
        generated_sections[title.casefold()] = generated[match.end():end].strip()
        generated_order.append(title)
    template_headings = [match.group(1).strip() for match in re.finditer(r"^##\s+(.+)$", template, re.MULTILINE)]
    if not template_headings:
        return generated
    planning = context["planning"]
    replacements = {
        "workspace": workspace.name, "entity": planning.get("entity") or "Not stated",
        "period": planning.get("period") or "Not stated", "objective": planning.get("objective") or "Not stated",
        "scope": planning.get("scope") or "Not stated", "materiality": planning.get("materiality") or "Not stated",
    }
    title = next((line.strip() for line in template.splitlines() if line.startswith("# ")), "# Internal Audit Report")
    for key, value in replacements.items():
        title = title.replace(f"{{{{{key}}}}}", str(value))
    lines = [title, ""]
    included: set[str] = set()
    for heading in template_headings:
        key = heading.casefold()
        body = generated_sections.get(key)
        if body is None:
            # Preserve a custom empty section without copying model-instruction comments.
            body = "Content to be completed by the auditor."
        lines.extend([f"## {heading}", "", body, ""])
        included.add(key)
    # Limitations are required even when an older/custom template omitted them.
    for heading in generated_order:
        key = heading.casefold()
        if key == "scope limitations" and key not in included:
            lines.extend([f"## {heading}", "", generated_sections[key], ""])
    return "\n".join(lines).strip() + "\n"


def deterministic_markdown(workspace: Workspace, context: dict | None = None) -> str:
    context = context or build_context(workspace)
    planning = context["planning"]
    stats = context["statistics"]
    findings = context.get("findings") or []
    lines = [
        "# Preliminary Internal Audit Working Draft" if context.get("preliminary") else "# Internal Audit Report",
        "",
    ]
    if context.get("preliminary"):
        lines.extend([
            "> **Preliminary working draft:** fieldwork, evidence, review, or auditor judgment remains open. This document is not a final audit opinion.",
            "",
        ])
    lines.extend([
        "## Executive summary", "",
        f"This draft reports the results of the {workspace.name} engagement. "
        f"Fieldwork recorded {stats['findings']} supported finding(s) across "
        f"{stats['planned_tests']} RCM planned test(s).",
        "", "## Background, objective, and scope", "",
        f"**Entity:** {planning.get('entity') or 'Not stated'}", "",
        f"**Period:** {planning.get('period') or 'Not stated'}", "",
        f"**Objective:** {planning.get('objective') or 'Not stated'}", "",
        f"**Scope:** {planning.get('scope') or 'Not stated'}", "",
        "## Findings and recommendations", "",
    ])
    if not findings:
        lines.append("No auditor-confirmed, evidence-supported findings have been recorded.")
    for item in findings:
        lines.extend(
            [
                f"### {item.get('title') or item['id']}", "",
                f"**Severity:** {item.get('severity', 'medium').title()}", "",
                f"**Reference:** {_finding_link(item)}", "",
                f"**Condition:** {item.get('condition') or 'Not stated'}", "",
                f"**Criteria:** {item.get('criteria') or 'Not stated'}", "",
                f"**Cause:** {item.get('cause') or 'Not stated'}", "",
                f"**Effect:** {item.get('effect') or 'Not stated'}", "",
                f"**Recommendation:** {item.get('recommendation') or 'Not stated'}", "",
            ]
        )
    lines.extend(["## Management responses", ""])
    responses = [item for item in findings if str(item.get("management_response") or "").strip()]
    lines.extend(
        [f"- {_finding_link(item)}: {item['management_response']}" for item in responses]
        or ["No management responses have been recorded."]
    )
    lines.extend(["", "## Conclusion", ""])
    if findings:
        lines.append(
            "The findings above require management consideration and auditor evaluation. "
            "This draft does not constitute an audit opinion."
        )
    else:
        lines.append("No conclusion is drawn because no findings have been recorded.")
    lines.extend(["", "## Scope limitations", ""])
    limitations = context.get("scope_limitations") or []
    lines.extend(
        [f"- RCM {item['rcm_id']} / planned test {item['planned_test_id']}: {item['text']}" for item in limitations]
        or ["No scope limitations were recorded."]
    )
    rendered = _template_order(workspace, "\n".join(lines).strip() + "\n", context)
    return _ensure_preliminary_label(rendered, bool(context.get("preliminary")))


def _template_sections(markdown: str) -> list[str]:
    headings = [match.group(1).strip() for match in re.finditer(r"^##\s+(.+)$", markdown, re.MULTILINE)]
    return headings or ["Complete report"]


def _model_turn(workspace: Workspace, system: str, user: str, *, run_id: str | None = None) -> str:
    stage = system.split("]", 1)[0].lstrip("[") if system.startswith("[") else "agent:report"
    with debug_store.trace_context(
        workspace_id=workspace.id, workspace_root=str(workspace.root), run_id=run_id, stage=stage,
        purpose="report_generation", artifact_refs=["report:draft"],
    ):
        message = llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            profile="agent",
        )
    content = str(message.get("content") or "").strip()
    if not content:
        raise llm.LLMError("The report model returned an empty response.")
    if content.startswith("```"):
        content = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    return content.strip()


def _generate_with_model(workspace: Workspace, template: str, context: dict, *, run_id: str | None = None) -> tuple[str, bool]:
    serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
    base = (
        "[agent:report]\nYou draft an internal-audit report for auditor review. "
        "Use only the supplied structured context, retain clickable finding references, "
        "state limitations, do not invent evidence or issue an audit opinion, and return Markdown only. "
        "Every numeric statement must copy the supplied statistics exactly; statistics.risk_distribution "
        "is the authoritative RCM rating count."
    )
    if len(serialized) <= MODEL_CONTEXT_LIMIT:
        return _model_turn(workspace, base, f"Template:\n{template}\n\nReport context:\n{serialized}", run_id=run_id), False
    sections = []
    for heading in _template_sections(template):
        system = base.replace("[agent:report]", "[agent:report_section]")
        body = _model_turn(
            workspace,
            system,
            f"Draft only the section headed '## {heading}'.\nReport context:\n{serialized}",
            run_id=run_id,
        )
        sections.append(body if body.startswith("## ") else f"## {heading}\n\n{body}")
    return "# Internal Audit Report\n\n" + "\n\n".join(sections).strip() + "\n", True


def _record_activity(workspace: Workspace, markdown: str, *, run_id: str | None, chunked: bool) -> None:
    profile = llm.agent_status()
    template = templates_store.get_template(workspace, "report")
    append_activity(
        workspace, run_id=run_id, stage="agent:report", task=None, purpose="report_generation",
        provider=profile.get("provider"), model=profile.get("model"), vision_used=False,
        prompt_version="report-v1", template_versions=[{
            "name": "report", "source": template["source"],
            "sha1": hashlib.sha1(template["markdown"].encode("utf-8")).hexdigest(),
        }], knowledge_packs=[], document_ids=[], page_ranges=[], source_hashes=[],
        response_at=_now(), response_hash=hashlib.sha1(markdown.encode("utf-8")).hexdigest(),
        artifact_ref="report:draft", disposition="generated_chunked" if chunked else "generated",
    )


def generate(workspace: Workspace, *, use_model: bool = True, run_id: str | None = None) -> dict:
    context = build_context(workspace)
    candidate = deterministic_markdown(workspace, context)
    warnings: list[str] = []
    used_model = False
    chunked = False
    if use_model and llm.agent_status().get("configured"):
        try:
            template = templates_store.get_template(workspace, "report")["markdown"]
            candidate, chunked = _generate_with_model(workspace, template, context, run_id=run_id)
            candidate = _normalize_finding_citations(workspace, candidate)
            used_model = True
            _record_activity(workspace, candidate, run_id=run_id, chunked=chunked)
        except (llm.LLMError, ValueError, TypeError) as error:
            warnings.append(f"Model drafting was unavailable; deterministic draft used: {error}")
    elif use_model:
        warnings.append("The report model is not configured; deterministic draft used.")
    candidate = _ensure_preliminary_label(candidate, bool(context.get("preliminary")))

    current = hydrate(workspace)
    had_edits = bool(current["markdown"] and current["edited"])
    timestamp = _now()
    workspace.report = {
        **current,
        "generated_markdown": candidate,
        "generated_at": timestamp,
        "generated_by_run": run_id,
        "generation_warnings": warnings,
        "workflow_parent_sha1": hashlib.sha1(
            json.dumps(context, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest(),
        "updated": timestamp,
    }
    if not had_edits:
        workspace.report["markdown"] = candidate
        workspace.report["edited"] = False
    workspace.save()
    return {
        **payload(workspace),
        "requires_reconcile": had_edits,
        "current_markdown": current["markdown"] if had_edits else candidate,
        "candidate_markdown": candidate,
        "used_model": used_model,
        "chunked": chunked,
    }


def update(workspace: Workspace, changes: dict) -> dict:
    allowed = {"markdown"}
    unknown = set(changes) - allowed
    if unknown:
        raise WorkspaceError(f"Unknown report field: {sorted(unknown)[0]}.")
    current = hydrate(workspace)
    if "markdown" in changes:
        current["markdown"] = str(changes["markdown"] or "")
        current["edited"] = current["markdown"] != current["generated_markdown"]
    current["updated"] = _now()
    workspace.report = current
    workspace.save()
    return payload(workspace)


def reconcile(workspace: Workspace, action: str) -> dict:
    current = hydrate(workspace)
    if action not in ("keep", "replace"):
        raise WorkspaceError("Report reconciliation action must be 'keep' or 'replace'.")
    if action == "replace":
        current["markdown"] = current["generated_markdown"]
        current["edited"] = False
    else:
        current["edited"] = current["markdown"] != current["generated_markdown"]
    current["updated"] = _now()
    workspace.report = current
    workspace.save()
    return payload(workspace)


def _issue(code: str, severity: str, message: str, refs: list[str] | None = None, *, source: str = "deterministic") -> dict:
    return {"code": code, "severity": severity, "message": message, "refs": refs or [], "source": source}


def quality_checks(workspace: Workspace, markdown: str | None = None) -> dict:
    report = hydrate(workspace)
    text = report["markdown"] if markdown is None else str(markdown)
    issues: list[dict] = []
    known_rcm = {item.get("id") for item in workspace.rcm}
    known_planned = {
        planned.get("id"): row.get("id")
        for row in workspace.rcm
        for planned in row.get("planned_tests") or []
    }
    cited_findings = _finding_citations(text)
    supported_findings = []
    for finding in workspace.findings:
        ref = f"finding:{finding['id']}"
        blockers = support_issues(workspace, finding)
        if not finding.get("auditor_confirmed"):
            issues.append(_issue(
                "finding_draft", "error",
                f"{finding['id']} remains a draft and cannot support report conclusions.", [ref],
            ))
        if blockers:
            issues.append(_issue(
                "unsupported_finding", "error",
                f"{finding['id']} lacks formal support: {'; '.join(blockers)}.", [ref],
            ))
        if finding.get("auditor_confirmed") and not blockers:
            supported_findings.append(finding)
        for rcm_id in finding.get("rcm_refs") or []:
            if rcm_id not in known_rcm:
                issues.append(_issue("broken_rcm_ref", "error", f"{finding['id']} references missing RCM row {rcm_id}.", [ref]))
        for planned_id in finding.get("planned_test_refs") or []:
            if planned_id not in known_planned:
                issues.append(_issue("broken_planned_test_ref", "error", f"{finding['id']} references missing planned test {planned_id}.", [ref]))
        for anchor in finding.get("evidence_refs") or []:
            resolved = artifact(workspace, anchor.get("source_kind"), anchor.get("source_id"))
            if resolved is None:
                issues.append(_issue("broken_evidence", "error", f"{finding['id']} has a broken evidence reference.", [ref, str(anchor.get('id'))]))
            elif anchor.get("source_sha1") != resolved["sha1"]:
                issues.append(_issue("stale_evidence", "error", f"{finding['id']} evidence {anchor.get('id')} no longer matches its source hash.", [ref, str(anchor.get('id'))]))
        if text and finding in supported_findings and finding["id"] not in cited_findings:
            issues.append(_issue("finding_missing_from_report", "warning", f"{finding['id']} is not cited in the report.", [ref]))

    open_observations = [
        item for item in workspace.observations if item.get("status") != "disposed"
    ]
    for observation in open_observations:
        issues.append(_issue(
            "unresolved_exception", "error",
            f"Observation {observation['id']} has no auditor disposition.",
            [f"observation:{observation['id']}", str(observation.get("execution_ref") or "")],
        ))
    observed_doc_tests = {
        str(item.get("execution_ref") or "").split(":", 1)[1]
        for item in workspace.observations
        if str(item.get("execution_ref") or "").startswith("doctest:")
    }
    for summary in doc_tests.list_tests(workspace):
        test = doc_tests.load_test(workspace, summary["id"])
        exceptions = [
            item for item in test.get("items") or []
            if item.get("state") == "exception"
            or item.get("auditor_disposition") == "exception"
        ]
        if exceptions and test["id"] not in observed_doc_tests:
            issues.append(_issue(
                "unresolved_exception", "error",
                f"{len(exceptions)} exception(s) in {test['id']} have no RCM observation disposition.",
                [f"doctest:{test['id']}"],
            ))
    exception_count = sum(int(item.get("exception_count") or 0) for item in workspace.observations)

    if not text.strip():
        issues.append(_issue("report_empty", "warning", "The report has no Markdown content."))
    for match in re.finditer(r"\?tab=findings&finding=([A-Za-z0-9_-]+)", text):
        if not any(item.get("id") == match.group(1) for item in workspace.findings):
            issues.append(_issue("broken_report_citation", "error", f"The report cites missing finding {match.group(1)}.", [f"finding:{match.group(1)}"]))
    finding_claim = re.search(r"\b(\d+)\s+(?:draft\s+)?finding\(s\)|\b(\d+)\s+findings?\b", text, re.IGNORECASE)
    if finding_claim:
        claimed = int(next(value for value in finding_claim.groups() if value is not None))
        if claimed != len(supported_findings):
            issues.append(_issue("report_arithmetic", "error", f"The report states {claimed} findings but {len(supported_findings)} are auditor-confirmed and supported."))
    exception_claim = re.search(r"\b(\d+)\s+exceptions?\b", text, re.IGNORECASE)
    if exception_claim and int(exception_claim.group(1)) != exception_count:
        issues.append(_issue("report_arithmetic", "error", f"The report states {exception_claim.group(1)} exceptions but {exception_count} are stored."))
    number_words = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    risk_distribution = {
        rating: sum(
            str(item.get("risk_rating") or "").casefold() == rating
            for item in workspace.rcm
        )
        for rating in ("critical", "high", "medium", "low")
    }
    risk_count_text = "\n".join(
        line
        for line in text.splitlines()
        if "risk distribution" in line.casefold() or "rcm" in line.casefold()
    )
    for rating, actual in risk_distribution.items():
        claims = []
        for match in re.finditer(
            rf"\b(\d+|{'|'.join(number_words)})\s+{rating}(?:[-\s]+risk)?\b"
            rf"|\b{rating}(?:[-\s]+risk)?\s*[:=\-–—]?\s*(\d+)\b",
            risk_count_text,
            re.IGNORECASE,
        ):
            raw = next(value for value in match.groups() if value is not None).casefold()
            claims.append(int(raw) if raw.isdigit() else number_words[raw])
        if any(claim != actual for claim in claims):
            issues.append(_issue(
                "report_risk_arithmetic", "error",
                f"The report's {rating}-risk count conflicts with the {actual} RCM row(s) stored.",
                [f"rcm:{item['id']}" for item in workspace.rcm if str(item.get("risk_rating") or "").casefold() == rating],
            ))
    limitations = [
        (row, planned)
        for row in workspace.rcm
        for planned in row.get("planned_tests") or []
        if str(planned.get("scope_limitations") or "").strip()
    ]
    if limitations and "limitation" not in text.lower():
        issues.append(_issue(
            "missing_limitations", "warning",
            "Recorded planned-test scope limitations are not disclosed in the report.",
            [f"planned_test:{planned['id']}" for _row, planned in limitations],
        ))
    completion = rcm_execution.completion(workspace)
    if completion["status"] != "completed" and text.strip() and "preliminary" not in text.casefold():
        issues.append(_issue(
            "preliminary_label_missing", "error",
            "Open fieldwork remains, so the report must be clearly labelled as a preliminary working draft.",
        ))

    counts = {level: sum(item["severity"] == level for item in issues) for level in ("error", "warning", "info")}
    return {"checked_at": _now(), "issues": issues, "counts": counts, "ok": counts["error"] == 0}


def editorial_review(workspace: Workspace) -> dict:
    result = quality_checks(workspace)
    if not llm.agent_status().get("configured"):
        result["editorial"] = [_issue("editorial_unavailable", "info", "Optional editorial review is unavailable because the report model is not configured.", source="editorial")]
        return result
    prompt = (
        "[agent:report_editorial]\nReview wording only. Return one JSON object only with an `issues` "
        "array of objects. Each issue object has string `code`, severity (`warning` or `info`), "
        "string `message`, and `refs` as an array of strings. Do not return prose outside the JSON "
        "object or a Markdown fence. Flag unclear wording, duplicate findings, severity "
        "inconsistency, or tone. Do not clear deterministic issues."
    )
    try:
        with debug_store.trace_context(
            workspace_id=workspace.id, workspace_root=str(workspace.root), stage="agent:report_editorial",
            purpose="report_editorial_review", artifact_refs=["report:draft"],
        ):
            message = llm.chat(
                [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps({"report": hydrate(workspace)["markdown"], "findings": [_safe_finding(item) for item in workspace.findings]}, ensure_ascii=False)}],
                profile="agent",
            )
        parsed = json.loads(str(message.get("content") or "{}"))
        editorial = []
        if not isinstance(parsed, dict):
            raise TypeError("editorial response must be an object")
        issues = parsed.get("issues")
        if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
            raise TypeError("issues must be an array of objects")
        for item in issues:
            severity = item.get("severity") if item.get("severity") in ("warning", "info") else "info"
            refs = item.get("refs")
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise TypeError("issue refs must be an array of strings")
            editorial.append(_issue(str(item.get("code") or "editorial_note"), severity, str(item.get("message") or ""), refs, source="editorial"))
        result["editorial"] = editorial
    except (llm.LLMError, json.JSONDecodeError, TypeError) as error:
        result["editorial"] = [_issue("editorial_unavailable", "info", f"Optional editorial review could not be completed: {error}", source="editorial")]
    return result


def _inline_html(value: str) -> str:
    escaped = html.escape(value)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    def link(match: re.Match) -> str:
        href = html.unescape(match.group(2))
        if not (href.startswith("?tab=") or href.startswith("/") or href.startswith("https://") or href.startswith("http://")):
            return match.group(1)
        return f'<a href="{html.escape(href, quote=True)}">{match.group(1)}</a>'
    return pattern.sub(link, escaped)


def markdown_to_html(markdown: str) -> str:
    output: list[str] = ['<article class="audit-report">']
    list_open = False
    for raw in str(markdown or "").splitlines():
        line = raw.strip()
        if line.startswith("- "):
            if not list_open:
                output.append("<ul>"); list_open = True
            output.append(f"<li>{_inline_html(line[2:])}</li>")
            continue
        if list_open:
            output.append("</ul>"); list_open = False
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            output.append(f"<h{level}>{_inline_html(heading.group(2))}</h{level}>")
        elif line:
            output.append(f"<p>{_inline_html(line)}</p>")
    if list_open:
        output.append("</ul>")
    output.append("</article>")
    return "".join(output)


def payload(workspace: Workspace) -> dict:
    current = hydrate(workspace)
    return {**current, "html": markdown_to_html(current["markdown"]), "quality": quality_checks(workspace)}
