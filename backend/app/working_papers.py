"""Procedure result drafting and safe working-paper rendering."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone

from . import data_tests, doc_tests, rcm_execution
from .workspaces import Workspace, WorkspaceError, write_json_atomic


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


def _render_test_step(step: object) -> list[str]:
    """Render one step from its own declared fields, never as a stringified dict."""
    if not isinstance(step, dict):
        return [f"- {step}"]
    label = str(step.get("label") or "Step").strip()
    instruction = str(step.get("instruction") or "").strip()
    lines = [f"- **{label}**" + (f" — {instruction}" if instruction else "")]
    if "document_ids" in step or "mode" in step:
        mode = str(step.get("mode") or "").strip()
        documents = ", ".join(step.get("document_ids") or []) or "none"
        lines.append(f"  - Mode: {mode or 'Not stated'}; documents: {documents}")
        if mode == "question" and step.get("question"):
            lines.append(f"  - Question: {step['question']}")
        elif mode == "vouch" and step.get("checks"):
            checks = "; ".join(
                f"{check.get('field')} = {check.get('expected')}"
                for check in step.get("checks") or []
                if isinstance(check, dict)
            )
            lines.append(f"  - Checks: {checks}")
        if step.get("missing_evidence"):
            lines.append(f"  - Missing evidence: {step['missing_evidence']}")
    else:
        lines.append("  - Tables: all workspace tables and joins")
    return lines


def _rcm_row(workspace: Workspace, rcm_id: str) -> dict:
    row = next((item for item in workspace.rcm if item.get("id") == rcm_id), None)
    if row is None:
        raise WorkspaceError(f"RCM row '{rcm_id}' not found.")
    return row


def render_rcm_markdown(workspace: Workspace, rcm_id: str) -> str:
    """Render one RCM row with every linked Data and Document Test result."""
    rcm_execution.rollup(workspace)
    row = _rcm_row(workspace, rcm_id)
    lines = [
        f"# RCM Working Paper — {row['id']}", "",
        f"**Process:** {row.get('process') or 'Not stated'}", "",
        f"**Risk:** {row.get('risk') or 'Not stated'}", "",
        f"**Risk rating:** {row.get('risk_rating') or 'Not stated'}", "",
        f"**Control:** {row.get('control') or 'Not stated'}", "",
        f"**Control owner:** {row.get('control_owner') or 'Not stated'}", "",
        f"**Criteria:** {row.get('criteria') or 'Not stated'}", "",
        "## Tests and execution", "",
    ]
    linked_refs = list(row.get("test_refs") or [])
    if not linked_refs:
        lines.append("No tests recorded.")
    source_hashes = []
    for test_ref in linked_refs:
        kind, separator, test_id = str(test_ref).partition(":")
        if not separator:
            continue
        test = (
            next(
                (item for item in workspace.data_tests if item.get("id") == test_id),
                None,
            )
            if kind == "datatest"
            else (doc_tests.load_test(workspace, test_id) if doc_tests.exists(workspace, test_id) else None)
        )
        if test is None:
            lines.append(f"Missing test reference: {test_ref}")
            continue
        lines.extend(
            [
                f"### {test.get('title') or test_id} ({test_id})", "",
                f"Objective: {test.get('objective') or 'Not stated'}", "",
                f"Source: {'data' if kind == 'datatest' else 'document'}; "
                f"status: {test.get('status')}; "
                f"control conclusion: {test.get('control_conclusion') or 'no_conclusion'}.", "",
                "Steps:", "",
            ]
        )
        steps = test.get("steps") or []
        if steps:
            for step in steps:
                lines.extend(_render_test_step(step))
        else:
            lines.append("No steps recorded.")
        lines.append("")
        for ref in [test_ref]:
            kind, separator, execution_id = str(ref).partition(":")
            if not separator:
                continue
            if kind == "datatest":
                item = next(
                    (test for test in workspace.data_tests if test.get("id") == execution_id),
                    None,
                )
                if item is None:
                    lines.append(f"- Missing Data Test reference: {ref}")
                    continue
                if not item.get("last_run"):
                    lines.append(f"- Data Test {item['id']} — {item.get('status')}; not executed.")
                    continue
                result = data_tests.load_result(workspace, item["id"], item["last_run"]["id"])
                source_hashes.append(result["result_sha1"])
                lines.append(
                    f"- Data Test {item['id']} — {result['status']}; verdict {result['verdict']}; "
                    f"{result['exception_count']} exception(s); result hash {result['result_sha1']}."
                )
            elif kind == "doctest" and doc_tests.exists(workspace, execution_id):
                test = doc_tests.load_test(workspace, execution_id)
                rollup = doc_tests.result_rollup(test)
                source_hashes.append(test["sha1"])
                lines.append(
                    f"- Document Test {test['id']} — {test.get('status')}; {rollup['items']} item(s); "
                    f"{rollup['exceptions']} confirmed exception(s); {rollup['manual_review']} manual review; "
                    f"source hash {test['sha1']}."
                )
        lines.extend(
            [
                "", "Result summary:", "",
                test.get("result_summary") or "No result summary recorded.", "",
                "Conclusion:", "",
                test.get("conclusion") or "No conclusion recorded.", "",
                "Scope limitations:", "",
                test.get("scope_limitations") or "No scope limitations recorded.", "",
            ]
        )
    lines.extend(["## Observations and findings", ""])
    observations = [
        item for item in workspace.observations if item.get("rcm_id") == rcm_id
    ]
    lines.extend(
        [
            f"- {item['id']}: {item.get('summary')} — disposition: "
            f"{item.get('disposition') or 'pending'}"
            for item in observations
        ]
        or ["- No observations recorded."]
    )
    findings = [item for item in workspace.findings if rcm_id in (item.get("rcm_refs") or [])]
    lines.extend(["", "## Linked findings", ""])
    lines.extend(
        [f"- {item['id']}: {item.get('title')} ({item.get('severity')})" for item in findings]
        or ["- No linked findings."]
    )
    lines.extend(
        [
            "", "## Review", "",
            f"Prepared by: {row.get('prepared_by') or 'Not assigned'}", "",
            f"Reviewed by: {row.get('reviewed_by') or 'Not assigned'}", "",
            f"Review status: {row.get('review_status') or 'draft'}", "",
            "## Immutable execution hashes", "",
        ]
    )
    lines.extend([f"- {value}" for value in source_hashes] or ["- No executed result hashes."])
    return "\n".join(lines).strip() + "\n"


def render_rcm(workspace: Workspace, rcm_id: str) -> dict:
    markdown = render_rcm_markdown(workspace, rcm_id)
    return {
        "rcm_id": rcm_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_sha1": hashlib.sha1(markdown.encode("utf-8")).hexdigest(),
        "markdown": markdown,
        "html": markdown_to_html(markdown),
    }


def generate_rcm(workspace: Workspace, rcm_id: str) -> dict:
    from .workspace_transactions import mutate, parent_hashes

    paper = render_rcm(workspace, rcm_id)
    expected = parent_hashes(workspace, [f"rcm:{rcm_id}"])
    paper["workflow_parent_sha1"] = expected[f"rcm:{rcm_id}"]

    def commit(fresh: Workspace) -> dict:
        path = fresh.root / "WorkingPapers" / f"{rcm_id}.json"
        write_json_atomic(path, paper)
        return paper

    result = mutate(workspace, commit, expected_parents=expected)
    from .workspaces import sync_workspace

    sync_workspace(workspace, result.workspace)
    return result.value
