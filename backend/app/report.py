"""Evidence-linked audit-report generation, reconciliation, and quality checks."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

from . import doc_tests, llm, templates_store
from .documents import append_activity
from .findings import artifact
from .workspaces import Workspace, WorkspaceError

REPORT_STATUSES = ("draft", "final")
REQUIRED_FINDING_FIELDS = ("condition", "criteria", "cause", "effect", "recommendation")
MODEL_CONTEXT_LIMIT = 30_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hydrate(workspace: Workspace) -> dict:
    return {
        "status": "draft",
        "markdown": "",
        "generated_markdown": "",
        "generated_at": None,
        "generated_by_run": None,
        "edited": False,
        "updated": None,
        "generation_warnings": [],
        **dict(workspace.report or {}),
    }


def _safe_finding(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "id", "title", "severity", "condition", "criteria", "cause", "effect",
            "recommendation", "management_response", "status", "rcm_refs",
            "procedure_refs", "source",
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


def build_context(workspace: Workspace) -> dict:
    """Build report context without structured rows or document excerpts."""
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
                "procedure_refs": test.get("procedure_refs") or [], "rollup": rollup,
            }
        )
    context = workspace.planning.get("context") or {}
    procedures = [
        {
            key: item.get(key)
            for key in (
                "id", "rcm_refs", "objective", "criteria", "steps", "method",
                "test_refs", "result_summary", "conclusion", "scope_limitations",
            )
        }
        for item in workspace.work_program
    ]
    return {
        "workspace": {"id": workspace.id, "name": workspace.name, "description": workspace.description},
        "planning": {
            "status": workspace.planning.get("status"),
            "objective": context.get("objective"), "entity": context.get("entity"),
            "period": context.get("period"), "scope": context.get("scope"),
            "materiality": context.get("materiality"),
        },
        "rcm": [
            {
                key: item.get(key)
                for key in ("id", "process", "risk", "risk_rating", "assertion", "control", "test_refs")
            }
            for item in workspace.rcm
        ],
        "procedures": procedures,
        "document_tests": test_summaries,
        "findings": [_safe_finding(item) for item in workspace.findings],
        "scope_limitations": [
            {"procedure_id": item.get("id"), "text": item.get("scope_limitations")}
            for item in workspace.work_program if str(item.get("scope_limitations") or "").strip()
        ],
        "statistics": {
            "rcm_rows": len(workspace.rcm), "procedures": len(workspace.work_program),
            "findings": len(workspace.findings), "final_findings": sum(
                item.get("status") == "final" for item in workspace.findings
            ),
            "document_tests": len(tests), **totals,
        },
    }


def _finding_link(item: dict) -> str:
    return f"[Finding {item['id']}](?tab=findings&finding={quote(str(item['id']))})"


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
    lines = [
        "# Internal Audit Report", "", "## Executive summary", "",
        f"This draft reports the results of the {workspace.name} engagement. "
        f"Fieldwork recorded {stats['findings']} finding(s) across {stats['procedures']} audit procedure(s).",
        "", "## Background, objective, and scope", "",
        f"**Entity:** {planning.get('entity') or 'Not stated'}", "",
        f"**Period:** {planning.get('period') or 'Not stated'}", "",
        f"**Objective:** {planning.get('objective') or 'Not stated'}", "",
        f"**Scope:** {planning.get('scope') or 'Not stated'}", "",
        "## Findings and recommendations", "",
    ]
    if not workspace.findings:
        lines.append("No findings have been recorded.")
    for item in workspace.findings:
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
    responses = [item for item in workspace.findings if str(item.get("management_response") or "").strip()]
    lines.extend(
        [f"- {_finding_link(item)}: {item['management_response']}" for item in responses]
        or ["No management responses have been recorded."]
    )
    lines.extend(["", "## Conclusion", ""])
    if workspace.findings:
        lines.append(
            "The findings above require management consideration and auditor evaluation. "
            "This draft does not constitute an audit opinion."
        )
    else:
        lines.append("No conclusion is drawn because no findings have been recorded.")
    lines.extend(["", "## Scope limitations", ""])
    limitations = context.get("scope_limitations") or []
    lines.extend(
        [f"- Procedure {item['procedure_id']}: {item['text']}" for item in limitations]
        or ["No scope limitations were recorded."]
    )
    return _template_order(workspace, "\n".join(lines).strip() + "\n", context)


def _template_sections(markdown: str) -> list[str]:
    headings = [match.group(1).strip() for match in re.finditer(r"^##\s+(.+)$", markdown, re.MULTILINE)]
    return headings or ["Complete report"]


def _model_turn(system: str, user: str) -> str:
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


def _generate_with_model(template: str, context: dict) -> tuple[str, bool]:
    serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
    base = (
        "[agent:report]\nYou draft an internal-audit report for auditor review. "
        "Use only the supplied structured context, retain clickable finding references, "
        "state limitations, do not invent evidence or issue an audit opinion, and return Markdown only."
    )
    if len(serialized) <= MODEL_CONTEXT_LIMIT:
        return _model_turn(base, f"Template:\n{template}\n\nReport context:\n{serialized}"), False
    sections = []
    for heading in _template_sections(template):
        system = base.replace("[agent:report]", "[agent:report_section]")
        body = _model_turn(
            system,
            f"Draft only the section headed '## {heading}'.\nReport context:\n{serialized}",
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
            candidate, chunked = _generate_with_model(template, context)
            used_model = True
            _record_activity(workspace, candidate, run_id=run_id, chunked=chunked)
        except (llm.LLMError, ValueError, TypeError) as error:
            warnings.append(f"Model drafting was unavailable; deterministic draft used: {error}")
    elif use_model:
        warnings.append("The report model is not configured; deterministic draft used.")

    current = hydrate(workspace)
    had_edits = bool(current["markdown"] and current["edited"])
    timestamp = _now()
    workspace.report = {
        **current,
        "generated_markdown": candidate,
        "generated_at": timestamp,
        "generated_by_run": run_id,
        "generation_warnings": warnings,
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
    allowed = {"status", "markdown"}
    unknown = set(changes) - allowed
    if unknown:
        raise WorkspaceError(f"Unknown report field: {sorted(unknown)[0]}.")
    current = hydrate(workspace)
    if "status" in changes:
        status = str(changes["status"] or "").lower()
        if status not in REPORT_STATUSES:
            raise WorkspaceError("Report status must be 'draft' or 'final'.")
        current["status"] = status
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
    known_procedures = {item.get("id"): item for item in workspace.work_program}
    for finding in workspace.findings:
        ref = f"finding:{finding['id']}"
        missing = [field for field in REQUIRED_FINDING_FIELDS if not str(finding.get(field) or "").strip()]
        if missing:
            issues.append(_issue("finding_incomplete", "warning", f"{finding['id']} is missing: {', '.join(missing)}.", [ref]))
        if not finding.get("procedure_refs"):
            issues.append(_issue("finding_no_procedure", "error", f"{finding['id']} is not linked to an audit procedure.", [ref]))
        for procedure_id in finding.get("procedure_refs") or []:
            if procedure_id not in known_procedures:
                issues.append(_issue("broken_procedure_ref", "error", f"{finding['id']} references missing procedure {procedure_id}.", [ref]))
        for rcm_id in finding.get("rcm_refs") or []:
            if rcm_id not in known_rcm:
                issues.append(_issue("broken_rcm_ref", "error", f"{finding['id']} references missing RCM row {rcm_id}.", [ref]))
        if not finding.get("evidence_refs"):
            issues.append(_issue("unsupported_finding", "error", f"{finding['id']} has no evidence anchor.", [ref]))
        for anchor in finding.get("evidence_refs") or []:
            resolved = artifact(workspace, anchor.get("source_kind"), anchor.get("source_id"))
            if resolved is None:
                issues.append(_issue("broken_evidence", "error", f"{finding['id']} has a broken evidence reference.", [ref, str(anchor.get('id'))]))
            elif anchor.get("source_sha1") != resolved["sha1"]:
                issues.append(_issue("stale_evidence", "error", f"{finding['id']} evidence {anchor.get('id')} no longer matches its source hash.", [ref, str(anchor.get('id'))]))
        if text and f"finding={finding['id']}" not in text:
            issues.append(_issue("finding_missing_from_report", "warning", f"{finding['id']} is not cited in the report.", [ref]))

    linked_tests: set[str] = set()
    for finding in workspace.findings:
        for procedure_id in finding.get("procedure_refs") or []:
            procedure = known_procedures.get(procedure_id) or {}
            linked_tests.update(
                value.split(":", 1)[1] for value in procedure.get("test_refs") or []
                if str(value).startswith("doctest:")
            )
    exception_count = 0
    for summary in doc_tests.list_tests(workspace):
        test = doc_tests.load_test(workspace, summary["id"])
        exceptions = [
            item for item in test.get("items") or []
            if item.get("state") == "exception" or item.get("auditor_disposition") == "exception"
        ]
        exception_count += len(exceptions)
        if exceptions and test["id"] not in linked_tests:
            issues.append(_issue("unresolved_exception", "warning", f"{len(exceptions)} exception(s) in {test['id']} are not linked through a finding procedure.", [f"doctest:{test['id']}"]))

    if not text.strip():
        issues.append(_issue("report_empty", "warning", "The report has no Markdown content."))
    for match in re.finditer(r"\?tab=findings&finding=([A-Za-z0-9_-]+)", text):
        if not any(item.get("id") == match.group(1) for item in workspace.findings):
            issues.append(_issue("broken_report_citation", "error", f"The report cites missing finding {match.group(1)}.", [f"finding:{match.group(1)}"]))
    finding_claim = re.search(r"\b(\d+)\s+(?:draft\s+)?finding\(s\)|\b(\d+)\s+findings?\b", text, re.IGNORECASE)
    if finding_claim:
        claimed = int(next(value for value in finding_claim.groups() if value is not None))
        if claimed != len(workspace.findings):
            issues.append(_issue("report_arithmetic", "error", f"The report states {claimed} findings but {len(workspace.findings)} are stored."))
    exception_claim = re.search(r"\b(\d+)\s+exceptions?\b", text, re.IGNORECASE)
    if exception_claim and int(exception_claim.group(1)) != exception_count:
        issues.append(_issue("report_arithmetic", "error", f"The report states {exception_claim.group(1)} exceptions but {exception_count} are stored."))
    limitations = [item for item in workspace.work_program if str(item.get("scope_limitations") or "").strip()]
    if limitations and "limitation" not in text.lower():
        issues.append(_issue("missing_limitations", "warning", "Recorded procedure scope limitations are not disclosed in the report.", [f"procedure:{item['id']}" for item in limitations]))

    counts = {level: sum(item["severity"] == level for item in issues) for level in ("error", "warning", "info")}
    return {"checked_at": _now(), "issues": issues, "counts": counts, "ok": counts["error"] == 0}


def editorial_review(workspace: Workspace) -> dict:
    result = quality_checks(workspace)
    if not llm.agent_status().get("configured"):
        result["editorial"] = [_issue("editorial_unavailable", "info", "Optional editorial review is unavailable because the report model is not configured.", source="editorial")]
        return result
    prompt = (
        "[agent:report_editorial]\nReview wording only. Return JSON with an issues array. Each issue has "
        "code, severity (warning|info), message, and refs. Flag unclear wording, duplicate findings, "
        "severity inconsistency, or tone. Do not clear deterministic issues."
    )
    try:
        message = llm.chat(
            [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps({"report": hydrate(workspace)["markdown"], "findings": [_safe_finding(item) for item in workspace.findings]}, ensure_ascii=False)}],
            profile="agent",
        )
        parsed = json.loads(str(message.get("content") or "{}"))
        editorial = []
        for item in parsed.get("issues") or []:
            severity = item.get("severity") if item.get("severity") in ("warning", "info") else "info"
            editorial.append(_issue(str(item.get("code") or "editorial_note"), severity, str(item.get("message") or ""), [str(ref) for ref in item.get("refs") or []], source="editorial"))
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
