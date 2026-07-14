"""Procedure result drafting and safe working-paper rendering."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone

from . import doc_tests
from .workspaces import Workspace, WorkspaceError


def _procedure(workspace: Workspace, procedure_id: str) -> dict:
    item = next((value for value in workspace.work_program if value.get("id") == procedure_id), None)
    if item is None:
        raise WorkspaceError(f"Procedure '{procedure_id}' not found.")
    return item


def _linked_tests(workspace: Workspace, procedure: dict) -> list[dict]:
    tests = []
    for ref in procedure.get("test_refs") or []:
        kind, separator, test_id = str(ref).partition(":")
        if separator and kind == "doctest" and doc_tests.exists(workspace, test_id):
            tests.append(doc_tests.load_test(workspace, test_id))
    return tests


def draft_results(workspace: Workspace, procedure_id: str) -> dict:
    procedure = _procedure(workspace, procedure_id)
    tests = _linked_tests(workspace, procedure)
    rollups = [doc_tests.result_rollup(test) for test in tests]
    total_items = sum(item["items"] for item in rollups)
    matched = sum(item["matched"] for item in rollups)
    mismatched = sum(item["mismatched"] for item in rollups)
    exceptions = sum(item["exceptions"] for item in rollups)
    manual = sum(item["manual_review"] for item in rollups)
    pending = sum(item["pending"] for item in rollups)
    if not tests:
        result = "No linked document tests have been executed."
        conclusion = "No conclusion has been drafted because the procedure has no linked test results."
        limitations = "Link execution artifacts or document tests before drawing a conclusion."
    else:
        result = (
            f"Executed {len(tests)} linked document test(s) covering {total_items} item(s). "
            f"Deterministic checks recorded {matched} match(es) and {mismatched} mismatch/missing result(s); "
            f"auditors marked {exceptions} exception(s)."
        )
        if exceptions or mismatched:
            conclusion = "The stored results include exceptions or unmatched evidence that require auditor evaluation against the procedure criteria."
        elif pending or manual:
            conclusion = "No final conclusion is supported while auditor dispositions or manual checks remain outstanding."
        else:
            conclusion = "The linked results support the procedure objective, subject to the stated scope limitations."
        limitations = (
            f"{manual} item(s) require manual review and {pending} item(s) remain pending auditor disposition."
            if manual or pending else "No unresolved document-test limitations were recorded."
        )
    return workspace.update_procedure(
        procedure_id,
        {"result_summary": result, "conclusion": conclusion, "scope_limitations": limitations},
        agent=True,
    )


def _citation(anchor: dict) -> str:
    page = f", page {anchor['page']}" if anchor.get("page") else ""
    field = f", field {anchor['field']}" if anchor.get("field") else ""
    return f"{anchor.get('source_kind')}:{anchor.get('source_id')}{page}{field} [{anchor.get('source_sha1') or 'legacy'}]"


def render_markdown(workspace: Workspace, procedure_id: str) -> str:
    procedure = _procedure(workspace, procedure_id)
    tests = _linked_tests(workspace, procedure)
    rcm = [row for row in workspace.rcm if row.get("id") in procedure.get("rcm_refs", [])]
    lines = [
        f"# Working Paper — {procedure['id']}", "",
        f"**Objective:** {procedure.get('objective') or 'Not stated'}", "",
        f"**Criteria:** {procedure.get('criteria') or 'Not stated'}", "",
        f"**Method/tool:** {procedure.get('method') or 'Not stated'}", "",
        "## Procedure steps", "",
    ]
    lines.extend([f"{index}. {step}" for index, step in enumerate(procedure.get("steps") or ["No steps recorded."], 1)])
    lines.extend(["", "## Planning links", ""])
    lines.extend([f"- {row['id']}: {row.get('risk') or row.get('process') or 'RCM row'}" for row in rcm] or ["- No linked RCM rows."])
    lines.extend(["", "## Execution artifacts", ""])
    if not tests:
        lines.append("No linked document tests.")
    for test in tests:
        rollup = doc_tests.result_rollup(test)
        lines.extend([
            f"### {test['title']} ({test['id']})", "",
            f"Kind: {test['kind']}; items: {rollup['items']}; matches: {rollup['matched']}; "
            f"mismatch/missing: {rollup['mismatched']}; exceptions: {rollup['exceptions']}; "
            f"manual review: {rollup['manual_review']}.", "",
        ])
        for item in test.get("items") or []:
            lines.append(f"- {item.get('label') or item['id']} — {item.get('state')}; auditor disposition: {item.get('auditor_disposition', 'pending')}")
    anchors = list(procedure.get("evidence_refs") or [])
    for test in tests:
        anchors.extend(anchor for item in test.get("items") or [] for anchor in item.get("evidence_refs") or [])
    unique = {anchor.get("id") or _citation(anchor): anchor for anchor in anchors}
    lines.extend(["", "## Evidence", ""])
    lines.extend([f"- {_citation(anchor)} — {anchor.get('excerpt') or 'No excerpt retained.'}" for anchor in unique.values()] or ["- No evidence anchors recorded."])
    lines.extend([
        "", "## Result summary", "", procedure.get("result_summary") or "No result summary recorded.",
        "", "## Conclusion", "", procedure.get("conclusion") or "No conclusion recorded.",
        "", "## Scope limitations", "", procedure.get("scope_limitations") or "No scope limitations recorded.",
    ])
    return "\n".join(lines).strip() + "\n"


def markdown_to_html(markdown: str) -> str:
    """Render the small generated subset of Markdown with all content escaped."""
    output, list_mode = [], None
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            if list_mode:
                output.append(f"</{list_mode}>")
                list_mode = None
            continue
        if line.startswith("### "):
            if list_mode: output.append(f"</{list_mode}>"); list_mode = None
            output.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            if list_mode: output.append(f"</{list_mode}>"); list_mode = None
            output.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            if list_mode: output.append(f"</{list_mode}>"); list_mode = None
            output.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif re_match := re.match(r"^(\d+)\.\s+(.*)$", line):
            if list_mode != "ol":
                if list_mode: output.append(f"</{list_mode}>")
                output.append("<ol>"); list_mode = "ol"
            output.append(f"<li>{html.escape(re_match.group(2))}</li>")
        elif line.startswith("- "):
            if list_mode != "ul":
                if list_mode: output.append(f"</{list_mode}>")
                output.append("<ul>"); list_mode = "ul"
            output.append(f"<li>{html.escape(line[2:])}</li>")
        else:
            if list_mode: output.append(f"</{list_mode}>"); list_mode = None
            escaped = html.escape(line)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            output.append(f"<p>{escaped}</p>")
    if list_mode:
        output.append(f"</{list_mode}>")
    return '<article class="working-paper">' + "".join(output) + "</article>"


def render(workspace: Workspace, procedure_id: str) -> dict:
    markdown = render_markdown(workspace, procedure_id)
    return {
        "procedure_id": procedure_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_sha1": hashlib.sha1(markdown.encode("utf-8")).hexdigest(),
        "markdown": markdown,
        "html": markdown_to_html(markdown),
    }
