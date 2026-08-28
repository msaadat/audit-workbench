"""Procedure result drafting and safe working-paper rendering."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone

from . import cycle_vouching, data_tests, doc_tests, rcm_execution
from .workspaces import Workspace, WorkspaceError, write_json_atomic
from .text import counted, verb


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
            f"Executed {counted(len(tests), 'linked document test')} covering {counted(total_items, 'item')}. "
            f"Deterministic checks recorded {counted(matched, 'match', 'matches')} and {counted(mismatched, 'mismatch or missing result', 'mismatch or missing results')}; "
            f"results recorded {counted(exceptions, 'exception')}."
        )
        if exceptions or mismatched:
            conclusion = "The stored results include exceptions or unmatched evidence that require auditor evaluation against the procedure criteria."
        elif pending or manual:
            conclusion = "Manual-check results remain a documented limitation."
        else:
            conclusion = "The linked results support the procedure objective, subject to the stated scope limitations."
        limitations = (
            f"{counted(manual, 'item')} {verb(manual, 'requires', 'require')} manual review and {counted(pending, 'item')} {verb(pending, 'has', 'have')} not run."
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
            lines.append(f"- {item.get('label') or item['id']} — {item.get('state')}")
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


def _markdown_cell(value: object) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _cycle_test_lines(test: dict) -> tuple[list[str], list[str]]:
    """Render the canonical Cycle vouch definition and item results once."""

    rollup = doc_tests.result_rollup(test)
    definition = test["definition"]
    population = definition["population"]
    selection = population["selection"]
    coverage = rollup["coverage"]
    assertions = list(definition["assertions"])
    tested = [
        item
        for item in test.get("items") or []
        if doc_tests.item_execution_current(test, item)
    ]
    lines = [
        "Canonical Cycle vouch procedure:", "",
        f"- Population: {population['table']} keyed by {population['row_key']['column']}",
        f"- Selection basis: {selection['mode']}",
        f"- Assurance scope: **{rollup['assurance_label']}**",
        f"- Conclusion eligible: {'yes' if rollup['conclusion_eligible'] else 'no'}",
        f"- Coverage: {coverage.get('selected_rows', len(test.get('items') or []))} selected; "
        f"{coverage.get('rows_with_evidence', '—')} with evidence; "
        f"{coverage.get('complete_cycles', '—')} complete cycles",
        "- Missing required roles: "
        + (
            ", ".join(
                f"{role}={count}"
                for role, count in sorted(
                    (coverage.get("missing_role_counts") or {}).items()
                )
            )
            or "none"
        ),
        f"- Tested items: {rollup['tested_items']}; failed: {rollup['failed_items']}; "
        f"incomplete: {rollup['incomplete_items']}; need review: {rollup['needs_review_items']}",
        f"- Auditor dispositions: {rollup['confirmed_items']} confirmed; "
        f"{counted(rollup['open_exceptions'], 'open exception')}; "
        f"{rollup['pending_dispositions']} pending",
        f"- Diagnostic assertion mismatches: {rollup['assertion_mismatches']}",
        "",
        "Assertion columns:", "",
    ]
    for assertion in assertions:
        key = str(assertion["key"])
        counts = {
            verdict: sum(
                str(((item.get("result_by_assertion") or {}).get(key) or {}).get("verdict") or "not_run")
                == verdict
                for item in test.get("items") or []
            )
            for verdict in cycle_vouching.ASSERTION_VERDICTS
        }
        lines.append(
            f"- {assertion.get('label') or key} (`{key}`): "
            + ", ".join(f"{name}={count}" for name, count in counts.items())
        )
    lines.extend(["", "Tested cycle grid:", ""])
    headers = ["Item", "Evaluation", "Auditor disposition", *[
        str(assertion.get("label") or assertion["key"]) for assertion in assertions
    ]]
    lines.extend([
        "| " + " | ".join(_markdown_cell(value) for value in headers) + " |",
        "| " + " | ".join("---" for _value in headers) + " |",
    ])
    for item in tested:
        results = item.get("result_by_assertion") or {}
        values = [
            item.get("label") or item["id"],
            (item.get("evaluation") or {}).get("state"),
            (item.get("disposition") or {}).get("state"),
        ]
        for assertion in assertions:
            result = results.get(str(assertion["key"])) or {}
            values.append(
                f"{result.get('verdict') or 'not_run'}"
                + (f" — {result['display']}" if result.get("display") else "")
            )
        lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    if not tested:
        lines.append("| No tested cycles | — | — |" + " — |" * len(assertions))
    lines.extend(["", "Cycle evidence details:", ""])
    hashes = [
        str(test.get("sha1") or ""),
        cycle_vouching.cycle_definition_sha1(test),
        str(test.get("registry", {}).get("definition_hash") or ""),
    ]
    for item in tested:
        evaluation = item.get("evaluation") or {}
        hashes.append(str(evaluation.get("result_sha1") or ""))
        lines.append(
            f"- **{item.get('label') or item['id']}** (`{item['id']}`) — "
            f"evaluation {evaluation.get('state')}; disposition "
            f"{(item.get('disposition') or {}).get('state')}"
        )
        for binding in item.get("role_bindings") or []:
            chain = " → ".join(
                f"{edge.get('identifier_kind')}={edge.get('normalized_value')}"
                for edge in binding.get("matched_by") or []
            ) or "manual/current binding"
            lines.append(
                f"  - Role {binding.get('role')}: document {binding.get('document_id')}, "
                f"record {binding.get('record_id')}; matched by {chain}"
            )
            hashes.extend(
                str(value or "")
                for value in (
                    binding.get("record_content_hash"),
                    binding.get("extraction_hash"),
                )
            )
        for assertion in assertions:
            key = str(assertion["key"])
            result = (item.get("result_by_assertion") or {}).get(key) or {}
            hashes.extend(
                str(value or "")
                for value in (result.get("assertion_sha1"), result.get("result_sha1"))
            )
            for anchor in result.get("evidence_refs") or []:
                lines.append(
                    f"  - {assertion.get('label') or key}: {_citation(anchor)}"
                )
                hashes.append(str(anchor.get("source_sha1") or ""))
    return lines, list(dict.fromkeys(value for value in hashes if value))


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
        execution_rollup = next(
            (
                value
                for value in (row.get("execution_rollup") or {}).get("test_rollups") or []
                if value.get("test_id") == test_id
            ),
            {},
        )
        lines.extend(
            [
                f"### {test.get('title') or test_id} ({test_id})", "",
                f"Objective: {test.get('objective') or 'Not stated'}", "",
                f"Source: {'data' if kind == 'datatest' else 'document'}; "
                f"status: {test.get('status')}; "
                f"control conclusion: {execution_rollup.get('control_conclusion') or 'no_conclusion'}.", "",
            ]
        )
        if kind == "doctest" and doc_tests.is_cycle_test(test):
            cycle_lines, cycle_hashes = _cycle_test_lines(test)
            lines.extend(cycle_lines)
            source_hashes.extend(cycle_hashes)
        else:
            lines.extend(["Steps:", ""])
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
                    f"{counted(result['exception_count'], 'exception')}; result hash {result['result_sha1']}."
                )
            elif kind == "doctest" and doc_tests.exists(workspace, execution_id):
                test = doc_tests.load_test(workspace, execution_id)
                rollup = doc_tests.result_rollup(test)
                source_hashes.append(test["sha1"])
                if doc_tests.is_cycle_test(test):
                    lines.append(
                        f"- Document Test {test['id']} — {test.get('status')}; "
                        f"{counted(rollup['tested_items'], 'tested item')}; "
                        f"{counted(rollup['open_exceptions'], 'open item exception')}; "
                        f"{rollup['assertion_mismatches']} diagnostic assertion mismatch(es); "
                        f"source hash {test['sha1']}."
                    )
                else:
                    lines.append(
                        f"- Document Test {test['id']} — {test.get('status')}; {counted(rollup['items'], 'item')}; "
                        f"{counted(rollup['exceptions'], 'confirmed exception')}; {rollup['manual_review']} manual review; "
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
            f"- {item['id']}: {item.get('summary')} — outcome: "
            f"{item.get('outcome') or 'needs_manual_check'}"
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
            # No reviewer line: nothing ever captured a name for one, so the
            # paper printed "Not assigned" beside rows marked reviewed. Sign-off
            # states itself; a signer the product cannot name is not claimed.
            "", "## Review", "",
            f"Prepared by: {row.get('prepared_by') or 'Not assigned'}", "",
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
