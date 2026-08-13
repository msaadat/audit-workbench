"""Evidence-linked audit-report generation, reconciliation, and quality checks."""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

from . import data_tests, debug_store, doc_tests, llm, rcm_execution, templates_store
from .text import counted, verb
from .documents import append_activity
from .findings import CAUSE_SECTION_KEYS, artifact, support_issues
from .workspaces import Workspace, WorkspaceError


def _linked_tests(
    workspace: Workspace,
    rcm_id: str,
    document_tests: rcm_execution.DocumentTestIndex | None = None,
) -> list[dict]:
    """Every durable test linked to one RCM row, in a stable order."""
    tests = [
        item for item in workspace.data_tests if item.get("rcm_id") == rcm_id
    ]
    tests.extend(
        (document_tests or rcm_execution.document_test_index(workspace)).by_rcm_id.get(
            rcm_id, ()
        )
    )
    return sorted(tests, key=lambda item: str(item.get("id") or ""))

_PLANNING_FIELDS = ("objective", "entity", "period", "scope", "materiality")

# The engagement ratings, most to least favourable. The draft assigns one; the
# recorded evidence decides how favourable it is allowed to be.
RATINGS = ("satisfactory", "fair", "marginal", "unsatisfactory")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
# The severities the executive Key Findings table carries. Senior management is
# deciding where to direct attention, not reading the whole population.
KEY_FINDING_SEVERITIES = ("critical", "high")

# The report has two levels but the template declares one flat list of sections.
# The detail section is the boundary: every section before it is rendered as a
# sub-part of an inserted executive-summary heading. A firm therefore reorders
# or renames the sub-parts without having to restate the two-level structure.
EXECUTIVE_SUMMARY_HEADING = "Executive Summary"
_DETAIL_SECTION_KEY = "detailed findings"
_CONCLUSION_SECTION_KEY = "audit conclusion"
_KEY_FINDINGS_SECTION_KEY = "key findings"
_SUMMARY_SECTION_KEY = "summary of findings"
_INTRODUCTION_SECTION_KEY = "introduction"
_SCOPE_SECTION_KEY = "objective and scope"
_DEFAULT_HEADINGS = (
    "Introduction", "Objective and Scope", "Audit Conclusion",
    "Key Findings", "Summary of Findings", "Detailed Findings",
)
# The severities the summary table counts, in column order.
_SUMMARY_SEVERITIES = ("critical", "high", "medium", "low")
_UNWRITTEN_SECTION = "Content to be completed by the auditor."
# How many recorded limitations the deterministic draft lists before it counts
# the rest. Limitations are recorded per test, so a thinly evidenced engagement
# restates the same few gaps many times over.
_SCOPE_LIMITATION_LIMIT = 6
# How alike two finding titles must be before the pair is worth an auditor's
# attention. Set to catch a restatement, not a shared subject: across the
# procurement engagement the one true duplicate pair scores 0.88 and the next
# closest unrelated pair 0.58, so the gap is wide and this sits inside it.
_DUPLICATE_TITLE_RATIO = 0.75
_PRELIMINARY_BANNER = (
    "> **Preliminary working draft:** fieldwork, evidence, review, or auditor "
    "judgment remains open. This document is not a final audit opinion."
)
# A rating is claimed either in bold or after the word "rating". Both forms are
# required to carry the word itself, because "fair" is ordinary English and a
# looser pattern would raise an error on prose that assigns nothing.
_RATING_CLAIM = re.compile(
    r"\*\*\s*(satisfactory|fair|marginal|unsatisfactory)\s*\*\*"
    r"|\brating\b[^A-Za-z0-9]{0,12}(satisfactory|fair|marginal|unsatisfactory)\b",
    re.IGNORECASE,
)


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
            "id", "title", "severity", "narrative", "management_response",
            "rcm_refs", "test_refs", "execution_refs", "cause_pending",
            "auditor_confirmed", "source",
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


def _apm_section(workspace: Workspace, *needles: str) -> str:
    """The first APM section whose heading matches one of ``needles``.

    The APM already holds the engagement's background in the auditor's own
    prose, so the report takes it from there rather than re-deriving it from the
    control matrix. A firm that renames the heading loses the passage rather
    than getting the wrong one; objective and scope still come from the
    structured planning context either way.
    """
    bodies = templates_store.section_bodies(
        str(workspace.planning.get("apm_markdown") or "")
    )
    for needle in needles:
        for key, body in bodies.items():
            if needle in key and body.strip():
                return body.strip()
    return ""


def _narrative_section(item: dict, *needles: str) -> str:
    """One section of a finding's narrative, matched loosely by heading."""
    bodies = templates_store.section_bodies(str(item.get("narrative") or ""))
    for needle in needles:
        for key, body in bodies.items():
            if needle in key and body.strip():
                return body.strip()
    return ""


def _comparable_title(item: dict) -> str:
    """One finding's title reduced to what a duplicate check should compare."""
    return " ".join(str(item.get("title") or "").casefold().split())


def _ordered_findings(items: list[dict]) -> list[dict]:
    """Confirmed findings, most severe first, ties broken by id."""
    return sorted(
        items,
        key=lambda item: (
            _SEVERITY_RANK.get(str(item.get("severity") or "").casefold(), 9),
            str(item.get("id") or ""),
        ),
    )


def rating_band(context: dict) -> dict:
    """The engagement ratings the recorded evidence permits.

    The draft chooses the rating; it never chooses the band. Confirmed finding
    severities, ineffective controls, and rows the fieldwork could not conclude
    on each set a ceiling, and open fieldwork withholds a rating entirely. An
    overstated rating is the one claim in this report a reader cannot check
    against the evidence themselves, so the ceiling is computed here and
    enforced by :func:`quality_checks` against whatever the draft finally says.

    Only optimism is constrained. An auditor who judges the environment worse
    than the ceiling is exercising judgment the evidence does not contradict.
    """
    rows = context.get("rcm") or []
    findings = context.get("findings") or []
    if context.get("preliminary"):
        return {
            "assignable": False,
            "ceiling": None,
            "allowed": [],
            # A reason completes "no rating because …" and "no rating while …",
            # so it names the cause and stops there.
            "reasons": ["fieldwork, evidence, or auditor judgment remains open"],
        }
    severities = {str(item.get("severity") or "").casefold() for item in findings}
    conclusions = [str(row.get("control_conclusion") or "").casefold() for row in rows]
    ineffective = sum(value == "ineffective" for value in conclusions)
    unconcluded = sum(value in ("", "no_conclusion") for value in conclusions)
    constraints: list[tuple[int, str]] = []
    if "critical" in severities:
        constraints.append((2, "a confirmed critical-severity finding was recorded"))
    if "high" in severities:
        constraints.append((1, "a confirmed high-severity finding was recorded"))
    if ineffective:
        constraints.append((1, f"{counted(ineffective, 'control')} {verb(ineffective, 'was', 'were')} concluded ineffective"))
    if rows and ineffective >= max(1, len(rows) // 3):
        constraints.append((
            2, f"{ineffective} of {len(rows)} controls were concluded ineffective"
        ))
    if rows and unconcluded >= max(1, len(rows) // 3):
        constraints.append((
            1, f"{unconcluded} of {len(rows)} controls reached no conclusion"
        ))
    if rows and unconcluded >= max(1, len(rows) // 2):
        constraints.append((
            2, f"{unconcluded} of {len(rows)} controls reached no conclusion"
        ))
    ceiling = max((index for index, _reason in constraints), default=0)
    return {
        "assignable": True,
        "ceiling": RATINGS[ceiling],
        "allowed": list(RATINGS[ceiling:]),
        "reasons": list(dict.fromkeys(reason for _index, reason in constraints)),
    }


def _incomplete_coverage(
    workspace: Workspace,
    workflow: dict | None = None,
    *,
    document_tests: rcm_execution.DocumentTestIndex | None = None,
) -> dict:
    """Summarize coverage lost before deterministic report generation.

    Workspace gaps remain authoritative after a partial workflow commits its
    successful branches.  Workflow unit failures add the otherwise-transient
    reason that those gaps remain, without turning report generation itself
    into a completion gate.
    """
    stages = (workflow or {}).get("stages") or []
    failed_statuses = {"failed", "conflict"}
    failed_planning = sum(
        unit.get("status") in failed_statuses
        for stage in stages
        if str(stage.get("capability") or "").startswith("planning.")
        for unit in stage.get("units") or []
    )
    failed_definitions = sum(
        unit.get("status") in failed_statuses
        for stage in stages
        if stage.get("capability") == "tests.specified"
        for unit in stage.get("units") or []
    )
    completion = rcm_execution.completion(workspace, document_tests=document_tests)
    coverage = completion.get("coverage") or {}
    missing_planning = (
        int(not str(workspace.planning.get("apm_markdown") or "").strip())
        + int(not workspace.rcm)
        + len(coverage.get("rows_without_tests") or [])
    )
    missing_definitions = len(coverage.get("unspecified_tests") or [])
    return {
        "failed_planning_units": failed_planning,
        "missing_planning_items": missing_planning,
        "failed_execution_definition_units": failed_definitions,
        "missing_execution_definitions": missing_definitions,
    }


def _coverage_warnings(coverage: dict) -> list[str]:
    warnings = []
    failed_planning = int(coverage.get("failed_planning_units") or 0)
    missing_planning = int(coverage.get("missing_planning_items") or 0)
    if failed_planning or missing_planning:
        warnings.append(
            "Incomplete planning coverage: "
            f"{counted(failed_planning, 'planning step')} failed and "
            f"{counted(missing_planning, 'required planning item')} {verb(missing_planning, 'is', 'are')} missing."
        )
    failed_definitions = int(coverage.get("failed_execution_definition_units") or 0)
    missing_definitions = int(coverage.get("missing_execution_definitions") or 0)
    if failed_definitions or missing_definitions:
        warnings.append(
            "Incomplete execution-definition coverage: "
            f"{counted(failed_definitions, 'execution-definition step')} failed and "
            f"{counted(missing_definitions, 'required execution definition')} {verb(missing_definitions, 'is', 'are')} missing."
        )
    return warnings


def _report_test_projection(workspace: Workspace, test: dict) -> dict:
    """Project what one test *found*, without how it was performed.

    A report states what was tested and what came of it. The procedure itself —
    step instructions, questions, document ids — is working-paper material, and
    reproducing it here was the single largest part of the drafting payload
    while contributing nothing a reader of the report can use. Hashes go for the
    same reason: provenance travels on the finding's evidence anchors and in the
    activity ledger, not through a drafting prompt. Recorded limitations are
    collected once at the top level rather than repeated under every test.
    """

    projection = {
        key: test.get(key)
        for key in (
            "id",
            "title",
            "objective",
            "criteria",
            "status",
            "result_summary",
            "conclusion",
            "exception_count",
            "open_exception_count",
            "finding_refs",
        )
    }
    if doc_tests.is_cycle_test(test):
        # A cycle test's item counts live only in its roll-up, and they are what
        # its control conclusion rests on.
        rollup = doc_tests.result_rollup(test)
        projection["rollup"] = rollup
        projection["control_conclusion"] = rollup["control_conclusion"]
    else:
        projection["control_conclusion"] = test.get("control_conclusion")
    if "engine" in test and test.get("last_run"):
        # A Data Test's durable result carries the one thing its definition does
        # not: whether the comparison it ran was semantically capable of finding
        # anything. A clean result from an impossible comparison is not evidence,
        # and a conclusion drawn over it would be unsupported.
        result = data_tests.load_result(workspace, test["id"], test["last_run"]["id"])
        projection["latest_result"] = {
            key: result.get(key)
            for key in (
                "status", "verdict", "verdict_text", "exception_count",
                "semantic_valid", "semantic_issues",
            )
        }
    return projection


def build_context(workspace: Workspace, *, workflow: dict | None = None) -> dict:
    """Build report context without structured rows or document excerpts.

    This is the engagement's reporting state, and it is deliberately one view of
    each fact. The same test previously appeared under its RCM row, again in the
    roll-up, and again in the document-test list; the report needs it once. What
    remains is what a reader of the report could be told: the planning basis,
    the matrix and what each control concluded, what each test found, the
    confirmed findings, and the limitations bounding all of it.
    """
    document_tests = rcm_execution.document_test_index(workspace)
    rolled = rcm_execution.rollup(workspace, document_tests=document_tests)
    completion = rcm_execution.completion(workspace, document_tests=document_tests)
    totals = {
        "items": 0,
        "tested_items": 0,
        "failed_items": 0,
        "incomplete_items": 0,
        "assertion_mismatches": 0,
        "exceptions": 0,
        "manual_review": 0,
        "pending": 0,
    }
    for test in document_tests.tests:
        rollup = doc_tests.result_rollup(test)
        for key in totals:
            totals[key] += int(rollup.get(key) or 0)
    context = _planning_context(workspace)
    linked_data_tests = [item for item in workspace.data_tests if item.get("rcm_id")]
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
    incomplete_coverage = _incomplete_coverage(
        workspace, workflow, document_tests=document_tests,
    )
    rolled_by_rcm = {item["rcm_id"]: item for item in rolled["rows"]}
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
                "business_cycle": item.get("business_cycle"),
                "control": item.get("control"),
                "control_conclusion": (rolled_by_rcm.get(item["id"]) or {}).get("control_conclusion"),
                "assurance_scopes": (rolled_by_rcm.get(item["id"]) or {}).get("assurance_scopes") or [],
                "review_status": item.get("review_status"),
                "tests": [
                    _report_test_projection(workspace, test)
                    for test in _linked_tests(workspace, item["id"], document_tests)
                ],
            }
            for item in workspace.rcm
        ],
        "findings": [_safe_finding(item) for item in supported],
        "draft_findings_excluded": [item["id"] for item in workspace.findings if item not in supported],
        "scope_limitations": [
            {"rcm_id": row["id"], "test_id": test["id"], "text": test.get("scope_limitations")}
            for row in workspace.rcm
            for test in _linked_tests(workspace, row["id"], document_tests)
            if str(test.get("scope_limitations") or "").strip()
        ] + [
            {
                "rcm_id": row["id"],
                "test_id": test["id"],
                "text": (
                    "Targeted evidence - not a sample; this test cannot support a "
                    "population control conclusion or projected exception rate."
                ),
            }
            for row in workspace.rcm
            for test in _linked_tests(workspace, row["id"], document_tests)
            if doc_tests.assurance_scope(test) == "targeted_evidence_only"
        ],
        "completion": completion,
        "preliminary": completion["status"] != "completed",
        "incomplete_coverage": incomplete_coverage,
        "statistics": {
            "rcm_rows": len(workspace.rcm),
            "risk_distribution": risk_distribution,
            "tests": sum(
                len(_linked_tests(workspace, row["id"], document_tests))
                for row in workspace.rcm
            ),
            "data_tests": len(linked_data_tests), "findings": len(supported),
            "draft_findings": len(workspace.findings) - len(supported),
            "document_tests": len(document_tests.tests), **totals,
        },
    }


def _answer_empty_sections(markdown: str, *, cause_pending: bool) -> str:
    """Give an unanswered narrative heading a statement, not a blank space.

    An auditor may formally defer the cause when the evidence does not establish
    it, which is the honest answer — but the report has to say so. Rendering the
    heading with nothing beneath it reads as an omission, and repeated once per
    finding it reads as a broken document.
    """
    # Horizontal whitespace only: a ``\s*$`` tail would consume the blank line
    # after the heading, and the replacement text would land in the wrong place.
    matches = list(re.finditer(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", markdown, re.MULTILINE))
    parts: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end():end]
        parts.append(markdown[cursor:match.end()])
        if not body.strip():
            deferred = (
                cause_pending
                and templates_store.section_key(match.group(2)) in CAUSE_SECTION_KEYS
            )
            parts.append(
                "\n\n"
                + (
                    "Not established by the evidence obtained; pending auditor "
                    "follow-up."
                    if deferred
                    else "Not stated."
                )
            )
        parts.append(body)
        cursor = end
    parts.append(markdown[cursor:])
    return "".join(parts)


def _finding_narrative(item: dict) -> str:
    """One finding's narrative, placed under its report heading unchanged.

    The narrative is authored as final report prose, so it is copied rather than
    reformatted. Only its heading depth moves: a finding sits at ``###`` in the
    report, so the narrative's own ``##`` sections are demoted to ``####`` to
    keep the document's outline intact. Authoring comments never travel.
    """
    body = templates_store.strip_guidance(item.get("narrative") or "").strip()
    if not body:
        return "The finding narrative has not been completed."
    return _answer_empty_sections(
        re.sub(
            r"^(#{1,4})(?=\s)",
            lambda match: "#" * min(len(match.group(1)) + 2, 6),
            body,
            flags=re.MULTILINE,
        ),
        cause_pending=bool(item.get("cause_pending")),
    )


def _finding_link(item: dict) -> str:
    return f"[Finding {item['id']}](?tab=findings&finding={quote(str(item['id']))})"


def _finding_citations(markdown: str) -> set[str]:
    # Report text may have been edited by a Markdown editor, which can escape
    # the ampersand in the canonical in-app URL.  Also accept any Markdown
    # link whose visible label is a finding ID: the destination may be an
    # in-report anchor (``#f-...``), an app route (``finding:F-...``), or the
    # canonical query route.  Citation status is based on the visible finding
    # reference, not on the link target.
    # Treat the Markdown escape as equivalent to a literal ampersand for
    # citation matching; it is presentation syntax, not a different route.
    searchable = str(markdown).replace(r"\&", "&")
    citations = {
        match.group(1)
        for match in re.finditer(r"\?tab=findings&finding=([A-Za-z0-9_-]+)", searchable)
    }
    citations.update(
        match.group(1)
        for match in re.finditer(
            r"\[\s*(?:Finding\s+)?(F-[A-Za-z0-9_-]+)\s*\](?:\s*\([^)]*\))?",
            searchable,
        )
    )
    # Older report templates prepended ``F-`` to an ID that already carried
    # that prefix. Retain those links as citations while normalizing only the
    # repeated prefix; an ordinary finding ID remains unchanged.
    normalized = set(citations)
    for citation in citations:
        current = citation
        while current.startswith("F-F-"):
            current = current[2:]
            normalized.add(current)
    return normalized


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


def _report_title(workspace: Workspace, context: dict, template: str) -> str:
    """The template's title line with its planning placeholders filled."""
    planning = context["planning"]
    replacements = {
        "workspace": workspace.name,
        "entity": planning.get("entity") or "Not stated",
        "period": planning.get("period") or "Not stated",
        "objective": planning.get("objective") or "Not stated",
        "scope": planning.get("scope") or "Not stated",
        "materiality": planning.get("materiality") or "Not stated",
    }
    title = next(
        (line.strip() for line in template.splitlines() if line.startswith("# ")),
        "# Internal Audit Report",
    )
    for key, value in replacements.items():
        title = title.replace(f"{{{{{key}}}}}", str(value))
    return title


def _part_label(index: int) -> str:
    """The letter one top-level part is numbered with: A, B, ... Z, AA."""
    label = ""
    while True:
        index, remainder = divmod(index, 26)
        label = chr(ord("A") + remainder) + label
        if index == 0:
            return label
        index -= 1


def _finding_numbers(context: dict) -> dict[str, int]:
    """Each confirmed finding's number in the report, from its detail order.

    One numbering serves both places a finding appears, so an executive table
    row names the finding a reader then turns to rather than leaving them to
    match on title.
    """
    return {
        str(item.get("id")): index + 1
        for index, item in enumerate(_ordered_findings(context.get("findings") or []))
    }


def _assemble(workspace: Workspace, context: dict, sections: dict[str, str]) -> str:
    """Render the configured sections into the report's two-level structure.

    The template declares one flat list of sections; the document has two
    levels. Everything before the detail section is a sub-part of the executive
    summary and renders one level down under an inserted grouping heading, so a
    firm reorders or renames those sub-parts without restating the nesting.
    """
    template = templates_store.get_template(workspace, "report")["markdown"]
    headings = templates_store.sections(template) or list(_DEFAULT_HEADINGS)
    detail_index = next(
        (
            index
            for index, heading in enumerate(headings)
            if templates_store.section_key(heading) == _DETAIL_SECTION_KEY
        ),
        len(headings),
    )
    lines = [_report_title(workspace, context, template), ""]
    part = 0
    for index, heading in enumerate(headings):
        if index == 0 and detail_index:
            lines.extend([f"## {_part_label(part)}. {EXECUTIVE_SUMMARY_HEADING}", ""])
            part += 1
        body = str(sections.get(templates_store.section_key(heading)) or "").strip()
        if index < detail_index:
            label = f"### {index + 1}. {heading}"
        else:
            label = f"## {_part_label(part)}. {heading}"
            part += 1
        lines.extend(
            [
                label,
                "",
                body or _UNWRITTEN_SECTION,
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _table_cell(value: object) -> str:
    """One table cell: single-line, with the column separator neutralized."""
    return " ".join(str(value or "").split()).replace("|", "\\|") or "Not stated"


def _first_sentence(value: str) -> str:
    text = " ".join(str(value or "").split())
    match = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (match.group(1) if match else text).strip()


def _limitation_texts(context: dict) -> list[str]:
    """Every recorded limitation once, in a stable order."""
    texts = list(
        dict.fromkeys(
            str(item.get("text") or "").strip()
            for item in context.get("scope_limitations") or []
            if str(item.get("text") or "").strip()
        )
    )
    if context.get("preliminary"):
        texts.extend(_coverage_warnings(context.get("incomplete_coverage") or {}))
    return texts


def _finding_processes(context: dict, item: dict) -> str:
    """The RCM process name(s) one finding sits under."""
    refs = {str(value) for value in item.get("rcm_refs") or []}
    names = {
        str(row.get("process") or "").strip()
        for row in context.get("rcm") or []
        if str(row.get("id")) in refs
    }
    return "; ".join(sorted(value for value in names if value))


def _key_findings(context: dict) -> list[dict]:
    """The findings the executive table carries, most severe first."""
    return [
        item
        for item in _ordered_findings(context.get("findings") or [])
        if str(item.get("severity") or "").casefold() in KEY_FINDING_SEVERITIES
    ]


def _grouped_by_process(context: dict, items: list[dict]) -> list[dict]:
    """Findings ordered so that those sharing a process sit together.

    Groups lead with their most severe finding, and the severest group leads the
    table, so grouping never buries a critical finding behind a quieter process.
    """
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(_finding_processes(context, item), []).append(item)
    return [
        item
        for _rank, _process, group in sorted(
            (
                (
                    min(
                        _SEVERITY_RANK.get(
                            str(entry.get("severity") or "").casefold(), 9
                        )
                        for entry in group
                    ),
                    process,
                    group,
                )
                for process, group in groups.items()
            ),
            key=lambda entry: entry[:2],
        )
        for item in group
    ]


def _key_findings_table(context: dict, drafted: list[dict] | None = None) -> str:
    """The executive table: drafted prose fills two cells, records fill the rest.

    Process and risk level are already recorded facts, so they are never taken
    from a response — only the two cells that genuinely require compression are.
    """
    items = _key_findings(context)
    if not items:
        return "No high-risk findings were identified."
    cells = {
        str(row.get("finding_id")): row
        for row in drafted or []
        if isinstance(row, dict)
    }
    numbers = _finding_numbers(context)
    lines = [
        "| # | Process | Key Finding | Risk Level | Recommendation |",
        "| --- | --- | --- | --- | --- |",
    ]
    seen_processes: set[str] = set()
    for item in _grouped_by_process(context, items):
        row = cells.get(str(item.get("id"))) or {}
        process = _finding_processes(context, item)
        # Markdown has no row spanning, so a process is named once and its
        # remaining findings continue under a blank cell — which is what a
        # merged cell looks like in a rendered table.
        label = "" if process in seen_processes else process
        seen_processes.add(process)
        lines.append(
            "| "
            + " | ".join(
                (
                    _table_cell(numbers.get(str(item.get("id")), "")),
                    _table_cell(label) if label else " ",
                    _table_cell(
                        row.get("key_finding")
                        or _first_sentence(_narrative_section(item, "condition"))
                        or item.get("title")
                    ),
                    _table_cell(str(item.get("severity") or "").title()),
                    _table_cell(
                        row.get("recommendation")
                        or _first_sentence(
                            _narrative_section(item, "recommendation", "remedy")
                        )
                    ),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def _summary_of_findings(workspace: Workspace, context: dict) -> str:
    """The confirmed findings counted by severity for the audited unit.

    Assembled, never drafted: this is arithmetic over the same findings the
    detail section carries, and a count a model produced would be one more
    number the report's own checks would have to police.
    """
    items = context.get("findings") or []
    if not items:
        return "No auditor-confirmed, evidence-supported findings have been recorded."
    counts = {
        severity: sum(
            str(item.get("severity") or "").casefold() == severity for item in items
        )
        for severity in _SUMMARY_SEVERITIES
    }
    uncounted = len(items) - sum(counts.values())
    lines = [
        "| Unit | " + " | ".join(name.title() for name in _SUMMARY_SEVERITIES) + " |",
        "| --- | " + " | ".join("---" for _ in _SUMMARY_SEVERITIES) + " |",
        f"| {_table_cell(workspace.name)} | "
        + " | ".join(str(counts[name]) for name in _SUMMARY_SEVERITIES)
        + " |",
    ]
    if uncounted:
        # The table's columns are the severities a finding is reported at; an
        # informational finding is still recorded, so it is stated rather than
        # quietly dropped from a count the reader will treat as complete.
        lines.extend([
            "",
            f"A further {counted(uncounted, 'finding')} {verb(uncounted, 'is', 'are')} recorded at informational "
            "severity and are not counted above.",
        ])
    return "\n".join(lines)


def _detailed_findings(context: dict) -> str:
    """Every confirmed finding in full, most severe first.

    Assembled, never drafted: the narrative is auditor-approved report prose and
    the management response belongs with the finding it answers, not in a
    separate list a reader has to cross-reference.
    """
    items = _ordered_findings(context.get("findings") or [])
    if not items:
        return "No auditor-confirmed, evidence-supported findings have been recorded."
    numbers = _finding_numbers(context)
    # Where no response has been received at all, that is one fact about the
    # engagement rather than a line to repeat under every finding.
    any_response = any(
        str(item.get("management_response") or "").strip() for item in items
    )
    blocks = []
    for item in items:
        response = str(item.get("management_response") or "").strip()
        parts = [
            f"### {numbers[str(item['id'])]}. {item.get('title') or item['id']}",
            f"**Severity:** {str(item.get('severity') or 'medium').title()} · "
            f"**Reference:** {_finding_link(item)}",
            _finding_narrative(item),
        ]
        if response:
            parts.append(f"**Management response:** {response}")
        blocks.append("\n\n".join(parts))
    if not any_response:
        blocks.insert(
            0, "No management responses have been received for the findings below."
        )
    return "\n\n".join(blocks)


def _introduction_body(workspace: Workspace, context: dict) -> str:
    planning = context["planning"]
    opening = (
        f"This report presents the results of the internal audit of "
        f"{workspace.name} at {planning.get('entity') or 'the entity'} for "
        f"{planning.get('period') or 'the period under review'}."
    )
    # The planning memorandum's citation markers are workbench navigation, not
    # report prose; the report's traceability travels on its evidence anchors.
    background = re.sub(
        r"\s*\[document:[^\]]*\]", "", _apm_section(workspace, "introduction", "background")
    )
    return "\n\n".join(part for part in (opening, background.strip()) if part)


def _scope_body(context: dict) -> str:
    planning = context["planning"]
    lines = [
        f"**Objective:** {planning.get('objective') or 'Not stated'}",
        "",
        f"**Scope:** {planning.get('scope') or 'Not stated'}",
    ]
    if str(planning.get("materiality") or "").strip():
        lines.extend(["", f"**Materiality:** {planning['materiality']}"])
    lines.extend(["", "**Scope limitations**", ""])
    texts = _limitation_texts(context)
    if not texts:
        lines.append("No scope limitations were recorded.")
        return "\n".join(lines)
    # Limitations are recorded per test, so an engagement with thin evidence
    # produces the same few gaps restated twenty times. Grouping them is the
    # drafting call's job; without it, the count is more use than the list.
    lines.extend(f"- {text}" for text in texts[:_SCOPE_LIMITATION_LIMIT])
    remaining = len(texts) - _SCOPE_LIMITATION_LIMIT
    if remaining > 0:
        lines.append(
            f"- A further {counted(remaining, 'limitation')} {verb(remaining, 'is', 'are')} recorded against "
            "individual tests and are set out in the working papers."
        )
    return "\n".join(lines)


def _conclusion_body(context: dict, band: dict) -> str:
    stats = context["statistics"]
    recorded = (
        f"Fieldwork recorded {counted(stats['findings'], 'confirmed finding')} across "
        f"{counted(stats['tests'], 'test')} over {counted(stats['rcm_rows'], 'control')}."
    )
    if not band["assignable"]:
        return (
            "No overall rating is assigned because "
            + "; ".join(band["reasons"])
            + f". {recorded}"
        )
    lines = [
        f"**Rating: {str(band['ceiling']).title()}**",
        "",
        "This is the most favourable rating the recorded evidence permits and "
        f"requires auditor judgment to confirm or lower. {recorded}",
    ]
    if band["reasons"]:
        lines.extend(["", "It is bounded by: " + "; ".join(band["reasons"]) + "."])
    return "\n".join(lines)


def _deterministic_sections(
    workspace: Workspace, context: dict, band: dict
) -> dict[str, str]:
    """A body for every section, written from records alone."""
    return {
        _INTRODUCTION_SECTION_KEY: _introduction_body(workspace, context),
        _SCOPE_SECTION_KEY: _scope_body(context),
        _CONCLUSION_SECTION_KEY: _conclusion_body(context, band),
        _KEY_FINDINGS_SECTION_KEY: _key_findings_table(context),
        _SUMMARY_SECTION_KEY: _summary_of_findings(workspace, context),
        _DETAIL_SECTION_KEY: _detailed_findings(context),
    }


def deterministic_markdown(workspace: Workspace, context: dict | None = None) -> str:
    context = context or build_context(workspace)
    band = rating_band(context)
    rendered = _assemble(
        workspace, context, _deterministic_sections(workspace, context, band)
    )
    return _ensure_preliminary_label(rendered, bool(context.get("preliminary")))


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
        content = re.sub(r"^```(?:json|markdown)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    return content.strip()


# This call is the one that returns multi-paragraph prose with a bulleted list.
# Asking for that inside a JSON string invites an unescaped newline and loses
# the whole section to a parse error, so it returns Markdown under two known
# headings and the sections are split back out deterministically.
OVERVIEW_SYSTEM = (
    "[agent:report_overview]\n"
    "You draft the opening of an internal-audit report for auditor review. "
    "Return Markdown only, under exactly these two headings and no others:\n"
    "## Introduction\n"
    "## Objective and Scope\n"
    "Under `Introduction`, one short paragraph of three or four lines: who was "
    "audited, over what period, and what the process covers.\n"
    "Under `Objective and Scope`, two short paragraphs of three or four lines "
    "each — the objective, then the scope — stating the boundary of the work: "
    "what the audit covered and what it did not. Do not enumerate process areas, "
    "controls, or sub-processes; a list of everything in scope is an inventory, "
    "not a scope.\n"
    "Then a bolded `**Scope limitations**` line followed by at most five short "
    "bullets. The recorded limitations repeat the same few gaps in different "
    "words, so group them by what was missing — operating evidence, approval "
    "records, sourcing documentation, and so on — and say what each gap prevents "
    "the report from concluding. Reproducing the list is not a disclosure; a "
    "reader will not read twenty bullets.\n"
    "Use only the supplied context. Do not preview the conclusion, do not assign "
    "a rating, do not name tests or other workbench identifiers, and do not "
    "invent an authority, a period, or a criterion the context lacks."
)

CONCLUSION_SYSTEM = (
    "[agent:report_conclusion]\n"
    "You draft the audit conclusion of an internal-audit report, written for "
    "senior management. Return one JSON object only, with a string `rating` and "
    "a string `conclusion` holding Markdown body text with no heading of its "
    "own. `rating` must be one of `rating_band.allowed` — the set the recorded "
    "evidence permits — and a more favourable rating will be rejected; where "
    "`rating_band.assignable` is false, return an empty `rating` and assign "
    "none. Open the conclusion with the rating in bold. Evaluate the overall "
    "control environment: what the pattern across control conclusions and "
    "findings shows about how well the process is controlled, and where the "
    "weight of the risk sits. Do not recount the findings one by one. Disclose "
    "incomplete coverage.\n"
    "Any figure you state must match `statistics` exactly — but state only the "
    "few that carry the conclusion. Reciting the set is not a conclusion, and a "
    "senior reader will not read past it.\n"
    "Write in audit language, never in the language of the tool: no test counts, "
    "no test identifiers, and no mention of data tests, document tests, items, "
    "roll-ups, manual reviews, assertion mismatches, or exception records. "
    "Three to five sentences, no hedging."
)

KEY_FINDINGS_SYSTEM = (
    "[agent:report_key_findings]\n"
    "You compress audit findings into an executive table. Return one JSON "
    "object only, with a `rows` array holding one object per supplied finding: "
    "string `finding_id`, string `key_finding`, and string `recommendation`.\n"
    "`key_finding` is one short sentence stating the scale of the issue and the "
    "control that failed — how many cases, and their total value where the "
    "narrative gives amounts. Aggregate: this reader is deciding where to direct "
    "attention, not reviewing transactions.\n"
    "    Write: \"1 case totalling 99.3 million where the approver's delegated "
    "financial authority was exceeded.\"\n"
    "    Not:   \"REQ2024081 for 99,348,150 was approved by approver 1002, whose "
    "delegated limit was 10,000,000.\"\n"
    "Never name an individual record, person, staff number, document, file, or "
    "system field, and never mention tests, exceptions recorded or withheld, "
    "result validity, or any other mechanic of how the work was performed.\n"
    "`recommendation` is one short imperative sentence: the action management "
    "should take, with no record identifiers.\n"
    "Never introduce a fact the narrative does not carry, never omit a supplied "
    "finding, and keep every cell on one line.\n"
    "Use British spelling — analyse, summarise, recognise, organisation."
)


def _model_json(
    workspace: Workspace, system: str, payload: dict, *, run_id: str | None = None
) -> dict:
    """One contracted model turn whose response must be a JSON object."""
    parsed = json.loads(
        _model_turn(
            workspace,
            system,
            json.dumps(payload, ensure_ascii=False, default=str),
            run_id=run_id,
        )
    )
    if not isinstance(parsed, dict):
        raise TypeError("the report model returned a non-object response")
    return parsed


def _overview_call_context(workspace: Workspace, context: dict) -> dict:
    """What the introduction and scope call may use.

    The objective and scope are the auditor's own, taken from the planning
    basis; the control matrix is not offered, because the reader needs the
    boundary of the work rather than its inventory.
    """
    return {
        "engagement": workspace.name,
        "planning": context["planning"],
        "background_from_planning_memorandum": _apm_section(
            workspace, "introduction", "background"
        ),
        # Counts only. Handing over the process names produced a scope paragraph
        # that listed all nineteen of them: an inventory, not a boundary.
        "coverage": {
            "controls": context["statistics"]["rcm_rows"],
            "tests": context["statistics"]["tests"],
        },
        "scope_limitations": _limitation_texts(context),
        "preliminary": context["preliminary"],
    }


def _conclusion_call_context(context: dict, band: dict) -> dict:
    """What the audit-conclusion call may use.

    A conclusion about the control environment is drawn from what the controls
    concluded and what the findings expose, so each finding contributes its risk
    section rather than its whole narrative.
    """
    return {
        "planning": context["planning"],
        "statistics": context["statistics"],
        "completion_status": (context.get("completion") or {}).get("status"),
        "preliminary": context["preliminary"],
        "rating_band": band,
        "controls": [
            {
                key: row.get(key)
                for key in ("process", "risk", "risk_rating", "control_conclusion")
            }
            for row in context["rcm"]
        ],
        "findings": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "severity": item.get("severity"),
                "risk": _narrative_section(item, "risk", "effect", "impact"),
            }
            for item in _ordered_findings(context.get("findings") or [])
        ],
        "scope_limitations": _limitation_texts(context),
    }


def _key_findings_call_context(context: dict) -> dict:
    """What the executive-table call may use: only the findings it compresses."""
    return {
        "findings": [
            {
                "finding_id": item.get("id"),
                "title": item.get("title"),
                "severity": item.get("severity"),
                "process": _finding_processes(context, item),
                "narrative": templates_store.strip_guidance(
                    str(item.get("narrative") or "")
                ).strip(),
            }
            for item in _key_findings(context)
        ]
    }


def _drafted_sections(
    workspace: Workspace, context: dict, band: dict, *, run_id: str | None = None
) -> tuple[dict[str, str], list[str]]:
    """Draft the model-written sections, one bounded call each.

    The calls are independent, so a failed or rejected response costs one
    section its drafted prose and nothing else — the report still assembles from
    records. The findings themselves are never offered to a model here: they are
    auditor-approved text and are copied, not rewritten.
    """
    drafted: dict[str, str] = {}
    warnings: list[str] = []

    def overview() -> None:
        bodies = templates_store.section_bodies(
            _model_turn(
                workspace,
                OVERVIEW_SYSTEM,
                json.dumps(
                    _overview_call_context(workspace, context),
                    ensure_ascii=False,
                    default=str,
                ),
                run_id=run_id,
            )
        )
        for key in (_INTRODUCTION_SECTION_KEY, _SCOPE_SECTION_KEY):
            if body := str(bodies.get(key) or "").strip():
                drafted[key] = body
        if not drafted.keys() & {_INTRODUCTION_SECTION_KEY, _SCOPE_SECTION_KEY}:
            raise ValueError("the response carried neither expected heading")

    def conclusion() -> None:
        parsed = _model_json(
            workspace, CONCLUSION_SYSTEM,
            _conclusion_call_context(context, band), run_id=run_id,
        )
        rating = str(parsed.get("rating") or "").strip().casefold()
        if band["assignable"] and rating not in band["allowed"]:
            raise ValueError(
                f"the proposed rating '{rating or 'none'}' is not one the recorded "
                f"evidence permits ({', '.join(band['allowed'])})"
            )
        if not band["assignable"] and rating:
            raise ValueError(
                "no overall rating may be assigned while fieldwork remains open"
            )
        if body := str(parsed.get("conclusion") or "").strip():
            drafted[_CONCLUSION_SECTION_KEY] = body

    def key_findings() -> None:
        parsed = _model_json(
            workspace, KEY_FINDINGS_SYSTEM,
            _key_findings_call_context(context), run_id=run_id,
        )
        rows = parsed.get("rows")
        if not isinstance(rows, list):
            raise TypeError("the key-findings response carries no `rows` array")
        drafted[_KEY_FINDINGS_SECTION_KEY] = _key_findings_table(context, rows)

    calls = [
        ("introduction and scope", overview),
        ("audit conclusion", conclusion),
    ]
    if _key_findings(context):
        calls.append(("key findings", key_findings))
    for label, call in calls:
        try:
            call()
        except (
            llm.LLMError, json.JSONDecodeError, ValueError, TypeError, KeyError
        ) as error:
            warnings.append(
                f"The {label} section kept its deterministic draft: {error}"
            )
    return drafted, warnings


def _record_activity(
    workspace: Workspace, markdown: str, *, run_id: str | None, sections: list[str]
) -> None:
    profile = llm.agent_status()
    template = templates_store.get_template(workspace, "report")
    append_activity(
        workspace, run_id=run_id, stage="agent:report", task=None, purpose="report_generation",
        provider=profile.get("provider"), model=profile.get("model"), vision_used=False,
        prompt_version="report-v2", template_versions=[{
            "name": "report", "source": template["source"],
            "sha1": hashlib.sha1(template["markdown"].encode("utf-8")).hexdigest(),
        }], knowledge_packs=[], document_ids=[], page_ranges=[], source_hashes=[],
        response_at=_now(), response_hash=hashlib.sha1(markdown.encode("utf-8")).hexdigest(),
        artifact_ref="report:draft",
        disposition="drafted:" + ",".join(sorted(sections)),
    )


def generate(
    workspace: Workspace, *, use_model: bool = True, run_id: str | None = None,
    workflow: dict | None = None,
) -> dict:
    context = build_context(workspace, workflow=workflow)
    band = rating_band(context)
    sections = _deterministic_sections(workspace, context, band)
    warnings = _coverage_warnings(context["incomplete_coverage"])
    drafted_sections: list[str] = []
    if use_model and llm.agent_status().get("configured"):
        drafted, drafting_warnings = _drafted_sections(
            workspace, context, band, run_id=run_id
        )
        sections.update(drafted)
        warnings.extend(drafting_warnings)
        drafted_sections = sorted(drafted)
    elif use_model:
        warnings.append("The report model is not configured; deterministic draft used.")
    candidate = _normalize_finding_citations(
        workspace, _assemble(workspace, context, sections)
    )
    candidate = _ensure_preliminary_label(candidate, bool(context.get("preliminary")))
    if drafted_sections:
        _record_activity(
            workspace, candidate, run_id=run_id, sections=drafted_sections
        )

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
        "used_model": bool(drafted_sections),
        "drafted_sections": drafted_sections,
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


def _report_claims(workspace: Workspace, text: str) -> str:
    """The report's own prose, with the copied finding narratives removed.

    A finding's narrative is auditor-authored text the report reproduces
    unchanged, and it legitimately counts the exceptions in its own population —
    "1 exception" in a finding is not a claim that the engagement found one. The
    arithmetic checks govern what the report itself asserts about the
    engagement, so the copied lines come out before they run. Headings survive
    the removal, which is harmless: they carry no figures.

    Table rows come out for the same reason: a Key Findings row compresses one
    finding and counts that finding's cases. Only the report's own prose speaks
    for the engagement as a whole.
    """
    narrative_lines = {
        line.strip()
        for finding in workspace.findings
        for line in templates_store.strip_guidance(
            finding.get("narrative") or ""
        ).splitlines()
        if line.strip()
    }
    return "\n".join(
        line
        for line in str(text).splitlines()
        if line.strip() not in narrative_lines and not line.strip().startswith("|")
    )


def _issue(code: str, severity: str, message: str, refs: list[str] | None = None, *, source: str = "deterministic") -> dict:
    return {"code": code, "severity": severity, "message": message, "refs": refs or [], "source": source}


def quality_checks(
    workspace: Workspace,
    markdown: str | None = None,
    *,
    document_tests: rcm_execution.DocumentTestIndex | None = None,
) -> dict:
    document_tests = document_tests or rcm_execution.document_test_index(workspace)
    report = hydrate(workspace)
    text = report["markdown"] if markdown is None else str(markdown)
    issues: list[dict] = []
    known_rcm = {item.get("id") for item in workspace.rcm}
    known_tests = {
        str(test.get("id")): row.get("id")
        for row in workspace.rcm
        for test in _linked_tests(workspace, row["id"], document_tests)
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
        for test_id in finding.get("test_refs") or []:
            if test_id not in known_tests:
                issues.append(_issue("broken_test_ref", "error", f"{finding['id']} references missing test {test_id}.", [ref]))
        for anchor in finding.get("evidence_refs") or []:
            resolved = artifact(workspace, anchor.get("source_kind"), anchor.get("source_id"))
            if resolved is None:
                issues.append(_issue("broken_evidence", "error", f"{finding['id']} has a broken evidence reference.", [ref, str(anchor.get('id'))]))
            elif anchor.get("source_sha1") != resolved["sha1"]:
                issues.append(_issue("stale_evidence", "error", f"{finding['id']} evidence {anchor.get('id')} no longer matches its source hash.", [ref, str(anchor.get('id'))]))
        if text and finding in supported_findings and finding["id"] not in cited_findings:
            issues.append(_issue("finding_missing_from_report", "warning", f"{finding['id']} is not cited in the report.", [ref]))

    observed_doc_tests = {
        str(item.get("test_id") or "")
        for item in workspace.observations
        if str(item.get("execution_ref") or "").startswith("doctest:")
        and item.get("outcome") == "exception"
    }
    observed_cycle_items = {
        (str(item.get("test_id") or ""), str(item.get("cycle_item_id") or ""))
        for item in workspace.observations
        if item.get("outcome") == "exception" and item.get("cycle_item_id")
    }
    for test in document_tests.tests:
        if doc_tests.is_cycle_test(test):
            exceptions = [
                item
                for item in test.get("items") or []
                if doc_tests.item_execution_current(test, item)
                and doc_tests.item_disposition_current(test, item)
                and (item.get("disposition") or {}).get("state") == "exception"
            ]
            missing = [
                item
                for item in exceptions
                if (str(test["id"]), str(item["id"])) not in observed_cycle_items
            ]
        else:
            exceptions = [
                item for item in test.get("items") or []
                if item.get("state") == "exception"
            ]
            missing = exceptions if exceptions and test["id"] not in observed_doc_tests else []
        if missing:
            issues.append(_issue(
                "unresolved_exception", "error",
                f"{counted(len(missing), 'exception item')} in {test['id']} "
                f"{verb(len(missing), 'has', 'have')} no current RCM observation.",
                [f"doctest:{test['id']}"],
            ))
    # Two findings that restate each other become two rows of the executive
    # table, which reads to management as two problems. The pair need not share
    # an RCM row — the same underlying weakness is often observed through more
    # than one control — so titles are compared across every confirmed finding.
    # Whether they are genuinely distinct is the auditor's call, so this is
    # advisory: it points at the pair rather than merging or hiding either.
    for first, second in itertools.combinations(supported_findings, 2):
        similarity = difflib.SequenceMatcher(
            None, _comparable_title(first), _comparable_title(second)
        ).ratio()
        if similarity >= _DUPLICATE_TITLE_RATIO:
            issues.append(_issue(
                "duplicate_finding", "warning",
                f"{first['id']} and {second['id']} report near-identical findings; "
                "consider merging them or distinguishing them.",
                [f"finding:{first['id']}", f"finding:{second['id']}"],
            ))
    exception_count = sum(int(item.get("exception_count") or 0) for item in workspace.observations)

    if not text.strip():
        issues.append(_issue("report_empty", "warning", "The report has not been drafted yet."))
    for match in re.finditer(r"\?tab=findings&finding=([A-Za-z0-9_-]+)", text):
        if not any(item.get("id") == match.group(1) for item in workspace.findings):
            issues.append(_issue("broken_report_citation", "error", f"The report cites missing finding {match.group(1)}.", [f"finding:{match.group(1)}"]))
    claims = _report_claims(workspace, text)
    finding_claim = re.search(r"\b(\d+)\s+(?:draft\s+)?finding\(s\)|\b(\d+)\s+findings?\b", claims, re.IGNORECASE)
    if finding_claim:
        claimed = int(next(value for value in finding_claim.groups() if value is not None))
        if claimed != len(supported_findings):
            issues.append(_issue("report_arithmetic", "error", f"The report states {claimed} findings but {len(supported_findings)} are auditor-confirmed and supported."))
    exception_claim = re.search(r"\b(\d+)\s+exceptions?\b", claims, re.IGNORECASE)
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
        for line in claims.splitlines()
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
                f"The report's {rating}-risk count conflicts with the "
                f"{counted(actual, 'RCM row')} stored.",
                [f"rcm:{item['id']}" for item in workspace.rcm if str(item.get("risk_rating") or "").casefold() == rating],
            ))
    limitations = [
        (row, test)
        for row in workspace.rcm
        for test in _linked_tests(workspace, row["id"], document_tests)
        if str(test.get("scope_limitations") or "").strip()
    ]
    if limitations and "limitation" not in text.lower():
        issues.append(_issue(
            "missing_limitations", "warning",
            "Recorded test scope limitations are not disclosed in the report.",
            [f"test:{test['id']}" for _row, test in limitations],
        ))
    completion = rcm_execution.completion(workspace, document_tests=document_tests)
    if completion["status"] != "completed" and text.strip() and "preliminary" not in text.casefold():
        issues.append(_issue(
            "preliminary_label_missing", "error",
            "Open fieldwork remains, so the report must be clearly labelled as a preliminary working draft.",
        ))
    # The rating is the one claim a reader cannot check against the evidence
    # themselves, so an assigned rating is held to the band the records permit.
    # Only optimism is an error: a rating at or below the ceiling is auditor
    # judgment the evidence does not contradict. The band is computed from the
    # three inputs it needs rather than from a full report context, because this
    # runs on every report read and a context build is not free.
    band = rating_band({
        "preliminary": completion["status"] != "completed",
        "findings": supported_findings,
        "rcm": rcm_execution.rollup(workspace, document_tests=document_tests)["rows"],
    })
    claim = _RATING_CLAIM.search(text)
    claimed = (
        next(value for value in claim.groups() if value is not None).casefold()
        if claim
        else ""
    )
    if claimed and not band["assignable"]:
        issues.append(_issue(
            "report_rating_unsupported", "error",
            f"The report assigns a '{claimed.title()}' rating, but no overall rating "
            f"can be assigned while {'; '.join(band['reasons'])}.",
        ))
    elif claimed and claimed not in band["allowed"]:
        issues.append(_issue(
            "report_rating_unsupported", "error",
            f"The report assigns a '{claimed.title()}' rating, but the recorded "
            f"evidence supports no better than '{str(band['ceiling']).title()}': "
            f"{'; '.join(band['reasons'])}.",
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


def payload(workspace: Workspace) -> dict:
    current = hydrate(workspace)
    return {**current, "quality": quality_checks(workspace)}
