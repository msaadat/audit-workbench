"""Audit-side composition of the domain-neutral capability scheduler.

Every audit capability is bound to a native scheduler path here: a per-unit
pipeline binding for the model-backed ones and a per-unit deterministic
computation for the rest. This module owns only the audit-shaped glue the
scheduler must not know about — which worker and executor a unit uses, the
declared context scope it resolves, the approval items an auditor sees, the
post-commit bookkeeping, the declared checkpoint handlers, and the audit
completion projection.

The class still inherits ``ActionRunner`` for the shared task, artifact, and
approval helpers that Phase 12 consolidates; it no longer implements any stage
handler.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .. import cycle_linking, cycle_vouching, doc_tests, document_analysis, rcm_execution
from ..text import counted, verb
from ..workspace_transactions import parent_hashes
from ..workspaces import (
    Workspace,
    WorkspaceConflict,
    WorkspaceError,
    slugify,
)
from . import capabilities as audit_capabilities
from . import narration, workflow
from .action_runner import ActionRunner
from .analysis_execution import AnalysisWorkflowExecution
from .capabilities.analysis import (
    ANALYSIS_SCOPE_CHECKPOINT,
    STAGE_CHECKPOINTS as ANALYSIS_STAGE_CHECKPOINTS,
)
from .capabilities.documents import (
    DOCUMENT_SCOPE_CHECKPOINT,
    STAGE_CHECKPOINTS as DOCUMENT_STAGE_CHECKPOINTS,
    analysis_unit_specs,
    resolve_document_scope,
)
from .capabilities._shared import target_rcm_ids
from .doc_tests_execution import bind_document_test_unit
from .documents_execution import (
    DocumentWorkflowExecution,
    build_document_capability_executions,
)
from .context import (
    ContextResolver,
    apm_document_methodology_scope,
    apm_table_profile_candidates,
    finding_draft_scope,
    planning_context_scope,
    rcm_scope,
    test_generate_scope,
)
from .executors import EXECUTORS
from .executors.fieldwork import (
    roll_up_results,
    run_data_test,
    untested_populations,
)
from .executors.planning import (
    AUDITOR_EDIT_PRESERVED,
    ApmExecutorTarget,
    PlanningContextExecutorTarget,
    RcmExecutorTarget,
)
from .executors.tests import TestGenerateExecutorTarget
from .executors.reporting import (
    VERIFICATION_REF,
    FindingExecutorTarget,
    generate_report_draft,
    generate_working_paper,
    output_issues,
    verify_audit,
)
from .execution_support import refresh_workspace, resolve_context, workflow_scope
from .runtime import (
    BoundUnitPipeline,
    CapabilityExecution,
    DeterministicUnitResult,
    FinishProjection,
    RunRuntime,
    UnitPipeline,
    UnitPipelineRequest,
    UnitSidecarStore,
    WorkflowRunner,
    first_unit_error,
    fold_terminal_status,
    unsettled_capabilities,
)
from .workers import WORKERS
from .workers import planning as planning_workers

# --------------------------------------------------------------------------- #
# Milestone highlights
# --------------------------------------------------------------------------- #
# A milestone carries at most three highlights, which is a constraint worth
# keeping: the point is the two or three things an auditor would say out loud
# when handing the work over, not a second copy of the artifact. Everything
# below is derived from durable local state, so a briefing never asserts
# anything the workspace cannot already show.
# Stages that deliberately file no milestone: machine steps whose completion is
# not an audit deliverable. `engagement_record` reads this too — a stage that
# never narrates must not be drawn as work still owed.
UNNARRATED_CAPABILITIES = frozenset({
    # Nobody ran it: the auditor imported, and a stage that reports what they
    # did has nothing of its own to narrate.
    "sources.imported",
    "documents.text_ready",
    "documents.analysis_chunks_ready",
    "documents.analysis_generated",
    "data.relationships_inferred",
    "data.joins_ready",
    "analysis.register_ready",
    "analysis.definitions_ready",
    "analysis.executed",
    "planning.context_ready",
})

HIGHLIGHT_LIMIT = 3

# Planning reads the governing material. Ordering by how directly a category
# establishes control criteria puts the policy that sets the rule above the
# meeting that discussed it.
_PLANNING_CATEGORY_RANK = {
    "policy": 0,
    "regulation": 1,
    "contract": 2,
    "minutes": 3,
    "prior_report": 4,
    "background": 5,
}


def _planning_documents(workspace: Workspace) -> list[dict]:
    """The governing documents planning rests on, most authoritative first."""
    return sorted(
        (
            item for item in workspace.documents
            if str(item.get("category") or "") in _PLANNING_CATEGORY_RANK
        ),
        key=lambda item: (
            _PLANNING_CATEGORY_RANK[str(item.get("category"))],
            str(item.get("source") or item.get("title") or "").casefold(),
        ),
    )


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _lead_sentence(text: str, *, section: str = "") -> str:
    """The first sentence of a block, optionally of one Markdown section.

    Findings carry their condition, criteria and cause under headings in one
    narrative; a briefing wants the condition's opening line and nothing else.
    """
    body = str(text or "")
    if section:
        lines: list[str] = []
        capturing = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                if capturing:
                    break
                capturing = section.casefold() in stripped.casefold()
                continue
            if capturing and stripped:
                lines.append(stripped)
        body = " ".join(lines)
    body = " ".join(body.split())
    if not body:
        return ""
    return re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0].strip()


def _rcm_label(row: Mapping) -> str:
    """What to call an RCM row in one line."""
    for field in ("risk", "risk_description", "control", "process"):
        value = _lead_sentence(str(row.get(field) or ""))
        if value:
            return value
    return str(row.get("id") or "RCM row")


def _rating(row: Mapping) -> str:
    return str(row.get("risk_rating") or "").casefold()


def _incomplete(row: Mapping) -> bool:
    """A row with no risk or no control cannot support coverage either way."""
    return not str(row.get("risk") or row.get("risk_description") or "").strip() or not str(
        row.get("control") or row.get("control_description") or ""
    ).strip()


def _processes(rows: list[dict]) -> list[str]:
    return list(dict.fromkeys(
        str(row.get("process") or "").strip() for row in rows if str(row.get("process") or "").strip()
    ))


def _rating_tally(by_rating: dict[str, list[dict]], incomplete: list[dict]) -> list[dict]:
    """The severity distribution, as a strip a reader takes in at a glance.

    Critical and high are stated even at zero — "no critical risks" is a
    finding about the matrix, not an absence worth hiding — while medium and
    low appear only where the matrix has them, so a two-tier engagement is not
    padded out to four.
    """
    tally = [
        {"label": rating, "value": len(by_rating.get(rating, [])), "severity": severity}
        for rating, severity in (
            ("critical", "error"), ("high", "warning"),
            ("medium", "info"), ("low", "info"),
        )
        if by_rating.get(rating) or rating in {"critical", "high"}
    ]
    if incomplete:
        tally.append(
            {"label": "incomplete", "value": len(incomplete), "severity": "error"}
        )
    return tally


def _rcm_row_highlight(row: Mapping) -> dict:
    """One row read out in full: the risk, then what is relied on against it."""
    control = _lead_sentence(str(row.get("control") or row.get("control_description") or ""))
    process = str(row.get("process") or "").strip()
    return {
        "severity": "error" if _rating(row) == "critical" else "warning",
        "label": _rcm_label(row),
        "detail": " — ".join(part for part in (process, control) if part),
        "artifact_ref": f"rcm:{row.get('id')}",
    }


def _rcm_highlights(
    critical: list[dict], high: list[dict], incomplete: list[dict]
) -> list[dict]:
    """Critical rows read out, everything else severe counted underneath them.

    Three rows quoted at equal weight said nothing about which mattered, and
    where a matrix had incomplete rows they took every slot and the critical
    risks vanished from the milestone entirely. The incomplete count is rolled
    into one line so it can never do that again.
    """
    highlights: list[dict] = []
    if incomplete:
        highlights.append({
            "severity": "error",
            "label": (
                f"{counted(len(incomplete), 'row')} "
                f"{verb(len(incomplete), 'has', 'have')} no risk or control description"
            ),
            "detail": "Neither side of the pairing is stated, so the row cannot be tested.",
            "artifact_ref": f"rcm:{incomplete[0].get('id')}",
        })
    # Critical rows are the ones worth reading. Where a matrix has none, the
    # high rows lead instead — an empty list of exemplars is worse than a
    # slightly less severe one.
    severe = critical + high
    slots = max(0, HIGHLIGHT_LIMIT - len(highlights) - 1)
    detailed = (critical or high)[:slots]
    named = {str(row.get("id") or "") for row in detailed}
    highlights.extend(_rcm_row_highlight(row) for row in detailed)
    remaining = [row for row in severe if str(row.get("id") or "") not in named]
    if remaining:
        tiers = sorted(
            {_rating(row) for row in remaining},
            key=lambda rating: _SEVERITY_RANK.get(rating, 9),
        )
        processes = _processes(remaining)
        highlights.append({
            "severity": "warning",
            "label": (
                f"{counted(len(remaining), 'further row')} rated "
                f"{' or '.join(tiers)}"
                if detailed else
                f"{counted(len(remaining), 'row')} rated {' or '.join(tiers)}"
            ),
            "detail": ", ".join(processes[:4]) + ("…" if len(processes) > 4 else ""),
            "artifact_ref": "rcm",
        })
    return highlights[:HIGHLIGHT_LIMIT]


def _quoted_headings(headings: list[str]) -> str:
    """"Key risks and planned response", from the casefolded heading keys."""
    named = [f"\u201c{heading[:1].upper()}{heading[1:]}\u201d" for heading in headings if heading]
    if len(named) <= 1:
        return named[0] if named else "its risk section"
    return f"{', '.join(named[:-1])} and {named[-1]}"


def _matters_sentence(matters: list[str] | None) -> str:
    """What the memorandum says it still owes.

    ``None`` and ``[]`` are different answers and are stated differently: a
    memorandum drafted before the template carried a section for matters was
    never asked, and reporting it as a plan with nothing outstanding would be a
    claim it never made.
    """
    if matters is None:
        return (
            "It has no section for matters left open, so nothing was recorded "
            "as outstanding."
        )
    if not matters:
        return "It records nothing outstanding."
    return (
        f"{counted(len(matters), 'matter')} {verb(len(matters), 'is', 'are')} "
        "recorded to confirm before the plan is relied on."
    )


# A matter is routinely written as one sentence whose clause after the
# semicolon is exactly the "why this has to be resolved" half — "the extracts
# lack version metadata, so the current versions are assumptions to be
# confirmed". Splitting there is what keeps the highlight a scannable line with
# a reason under it rather than one 190-character label.
_CLAUSE_END = re.compile(r";\s+")


def _split_note(text: str) -> tuple[str, str]:
    """A planning matter as a highlight: the statement, then the reason."""
    statement, detail = document_analysis.split_note(text)
    if detail or not statement:
        return statement, detail
    parts = _CLAUSE_END.split(statement, maxsplit=1)
    if len(parts) == 1:
        return statement, ""
    return f"{parts[0]}.", parts[1][:1].upper() + parts[1][1:]


def _category_breakdown(documents: list[dict]) -> str:
    """"2 policy documents and 2 sets of minutes", from the categories present."""
    labels = {
        "policy": ("policy document", "policy documents"),
        "regulation": ("regulation", "regulations"),
        "contract": ("contract", "contracts"),
        "minutes": ("set of minutes", "sets of minutes"),
        "prior_report": ("prior report", "prior reports"),
        "background": ("background document", "background documents"),
    }
    counts: dict[str, int] = {}
    for item in documents:
        category = str(item.get("category") or "")
        counts[category] = counts.get(category, 0) + 1
    parts = [
        f"{total} {labels[category][0 if total == 1 else 1]}"
        for category, total in sorted(
            counts.items(), key=lambda pair: _PLANNING_CATEGORY_RANK.get(pair[0], 9)
        )
        if category in labels
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


class AuditWorkflowExecution(ActionRunner):
    """Per-unit audit execution bindings and projections for the scheduler."""

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        context_resolver: ContextResolver | None = None,
    ):
        """Create the audit execution adapter with an injectable per-run runtime.

        The optional dependencies preserve the existing three-argument
        construction API while letting a caller supply its own runtime or context
        resolver.
        """
        super().__init__(workspace, run, handle, runtime=runtime)
        self.context_resolver = context_resolver or ContextResolver()
        self.scheduler: WorkflowRunner | None = None

    # ------------------------------------------------------------ ledger
    def _refresh_dynamic_limits(self) -> None:
        test_count = len(self.ws.data_tests) + len(doc_tests.list_tests(self.ws))
        qa_pairs = sum(
            len(item.get("document_ids") or [])
            for summary in doc_tests.list_tests(self.ws)
            if summary.get("kind") == "qa"
            for item in doc_tests.load_test(self.ws, summary["id"]).get("items") or []
        )
        eligible_findings = sum(
            item.get("outcome") == "exception"
            for item in self.ws.observations
        )
        calculated = (
            20 + 4 * len(self.ws.rcm) + 4 * test_count
            + 2 * qa_pairs + 2 * eligible_findings
        )
        document_scope = dict(
            (self.run.get("workflow") or {}).get("scope") or {}
        )
        resolved_documents = resolve_document_scope(
            self.ws, document_scope
        )
        document_specs = [
            spec
            for document_id in resolved_documents.document_ids
            for spec in analysis_unit_specs(
                self.ws, document_id, document_scope
            )
        ]
        text_units = sum(
            spec["kind"] == "document_chunk_analysis"
            for spec in document_specs
        )
        visual_units = sum(
            spec["kind"] == "document_visual_page_analysis"
            and not spec.get("unsupported_reason")
            for spec in document_specs
        )
        document_turns = len(document_specs) + max(
            1, len(resolved_documents.document_ids)
        )
        calculated += document_turns
        prompt_allowance = (
            calculated * 10_000
            + text_units * 2_000
            + visual_units * 10_480
        )
        self.update_limits(
            {
                "max_model_turns": calculated,
                "max_estimated_prompt_tokens": prompt_allowance,
                "max_completion_tokens": calculated * 4_000,
                "max_image_parts": max(4, visual_units * 4),
                "max_prepared_image_bytes": max(
                    12 * 1024 * 1024,
                    visual_units * 12 * 1024 * 1024,
                ),
                "max_prepared_image_pixels": max(
                    12_000_000, visual_units * 12_000_000
                ),
            },
            grow_only=True,
        )

    def milestone_projection(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
    ) -> dict | None:
        """Build compact audit-deliverable summaries from durable local state."""
        self.ws = subject
        capability_id = capability.id
        if capability_id in UNNARRATED_CAPABILITIES:
            return None
        units = list(stage.get("units") or [])
        done = sum(unit.get("status") in {"succeeded", "skipped"} for unit in units)
        attention = sum(
            unit.get("status")
            in {
                "failed",
                "conflict",
                "blocked",
                "awaiting_input",
                "awaiting_confirmation",
            }
            for unit in units
        )
        refs = list(dict.fromkeys(
            ref for unit in units for ref in unit.get("result_refs") or []
        ))
        state = "completed_with_issues" if attention else "completed"
        changes = run.get("planning_changes") or {}

        if capability_id == "planning.apm_ready":
            updated = int(changes.get("apm_updated") or 0)
            proposed = int(changes.get("apm_proposed") or 0)
            # Read the memorandum, not only the material it was drafted from.
            # This row's artifact is the APM; summarising the governing
            # documents' analysis put another stage's notes about the client's
            # own minutes under a headline saying the memorandum is ready, where
            # they read as a defect in the memorandum. Those notes belong to the
            # document-analysis milestone, which now counts them.
            markdown = str((subject.planning or {}).get("apm_markdown") or "")
            governing = _planning_documents(subject)
            risks = planning_workers.planned_risk_themes(markdown)
            argued = planning_workers.unstructured_risk_sections(markdown)
            matters = planning_workers.planning_matters(markdown)
            basis = _category_breakdown(governing)
            summary = (
                f"Plans the engagement from {basis}"
                if basis
                else "Plans the engagement with no governing documents to work from"
            )
            if risks:
                summary += (
                    f", and sets a planned response against "
                    f"{counted(len(risks), 'risk')}."
                )
            elif argued:
                # Prose is a legitimate way to argue a risk assessment. It is
                # also the shape the RCM cannot build rows from, so a reader
                # deciding whether to run the matrix needs to know now.
                summary += (
                    f". Its risk assessment is argued as prose under "
                    f"{_quoted_headings(argued)}, so it enumerates no risk for "
                    "the matrix to build from."
                )
            else:
                summary += " and enumerates no risk."
            summary += " " + _matters_sentence(matters)
            # A memorandum that assesses no risk at all is not a plan anyone can
            # work from, whatever else it covers.
            thin = not risks and not argued
            metrics = [
                {"label": "Risks assessed", "value": len(risks)},
                {"label": "Governing documents", "value": len(governing)},
                {"label": "Updated", "value": updated},
                {"label": "Proposed for approval", "value": proposed},
            ]
            # Omitted rather than reported as zero on a memorandum that has no
            # section for matters: "0" is an answer this one never gave.
            if matters is not None:
                metrics.insert(1, {"label": "Matters to confirm", "value": len(matters)})
            return {
                "status": "completed_with_issues" if thin else state,
                "headline": (
                    "Audit planning memorandum ready — no risk assessed"
                    if thin else "Audit planning memorandum ready"
                ),
                "summary": summary,
                "metrics": metrics,
                # What the memorandum itself says it could not settle. Same
                # shape as any other highlight — a statement to scan and the
                # reason under it — but sourced from the artifact on this row.
                "highlights": [
                    {
                        "severity": "warning",
                        "label": statement,
                        "detail": detail,
                        "artifact_ref": "planning:apm",
                    }
                    for statement, detail in (
                        _split_note(item) for item in (matters or [])
                    )
                    if statement
                ][:HIGHLIGHT_LIMIT],
                "artifact_refs": refs or ["planning:apm"],
            }
        if capability_id == "planning.rcm_ready":
            # A matrix is a distribution before it is a list. Reading it as
            # "one critical, eight high" is what an auditor does first, and
            # three rows quoted at equal weight — which is what a milestone
            # built only from highlights can say — buried that.
            rows = list(subject.rcm)
            incomplete = [row for row in rows if _incomplete(row)]
            by_rating: dict[str, list[dict]] = {}
            for row in rows:
                by_rating.setdefault(_rating(row), []).append(row)
            critical = by_rating.get("critical", [])
            high = by_rating.get("high", [])
            processes = _processes(rows)
            return {
                "status": "completed_with_issues" if incomplete or attention else state,
                "headline": (
                    f"Risk and control matrix drafted — "
                    f"{counted(len(incomplete), 'row')} incomplete"
                    if incomplete else "Risk and control matrix ready"
                ),
                # The counts moved to the tally, so the sentence says what the
                # matrix covers instead of repeating them.
                "summary": (
                    f"{counted(len(rows), 'row')} covering "
                    f"{counted(len(processes), 'process', 'processes')}, each "
                    "pairing a risk with the control the entity relies on."
                    + (
                        f" {counted(len(incomplete), 'row')} "
                        f"{verb(len(incomplete), 'is', 'are')} missing a risk or "
                        "control description and cannot support coverage."
                        if incomplete else ""
                    )
                ),
                "metrics": [
                    {"label": "RCM rows", "value": len(rows)},
                    {"label": "Created", "value": int(changes.get("rcm_created") or 0)},
                    {"label": "Updated", "value": int(changes.get("rcm_updated") or 0)},
                    {"label": "Preserved", "value": int(changes.get("rcm_preserved") or 0)},
                    {"label": "High or critical", "value": len(critical) + len(high)},
                ],
                "stats": _rating_tally(by_rating, incomplete),
                "highlights": _rcm_highlights(critical, high, incomplete),
                "artifact_refs": refs,
            }
        if capability_id == "tests.specified":
            doc_count = len(doc_tests.list_tests(subject))
            total = len(subject.data_tests) + doc_count
            uncovered = sum(not (row.get("test_refs") or []) for row in subject.rcm)
            return {
                "status": "completed_with_issues" if uncovered or attention else state,
                "headline": (
                    f"Tests specified — {counted(uncovered, 'risk')} still uncovered"
                    if uncovered else "Tests specified"
                ),
                "summary": (
                    f"Prepared {counted(total, 'test')}: "
                    f"{counted(len(subject.data_tests), 'data test')} and "
                    f"{counted(doc_count, 'document test')}."
                    + (
                        f" {counted(uncovered, 'RCM row')} still "
                        f"{verb(uncovered, 'has', 'have')} no linked test."
                        if uncovered else ""
                    )
                ),
                "metrics": [
                    {"label": "Tests", "value": total},
                    {"label": "Created", "value": int(changes.get("test_created") or 0)},
                    {"label": "Updated", "value": int(changes.get("test_updated") or 0)},
                    {"label": "Preserved", "value": int(changes.get("test_preserved") or 0)},
                    {"label": "RCM rows without tests", "value": uncovered},
                ],
                # A risk nobody can test is the fact worth carrying out of this
                # stage; the tests that were built speak for themselves.
                "highlights": [
                    {
                        "severity": "warning",
                        "label": _rcm_label(row),
                        "detail": "No test covers this row, so it cannot pass coverage.",
                        "artifact_ref": f"rcm:{row.get('id')}",
                    }
                    for row in sorted(
                        (item for item in subject.rcm if not (item.get("test_refs") or [])),
                        key=lambda item: (
                            _SEVERITY_RANK.get(
                                str(item.get("risk_rating") or "").casefold(), 9
                            ),
                            str(item.get("id") or ""),
                        ),
                    )
                ][:HIGHLIGHT_LIMIT],
                "artifact_refs": refs,
            }
        if capability_id == "fieldwork.executed":
            return {
                "status": state,
                # Titled by outcome, not by stage lifecycle. "Complete" here only
                # ever meant "the stage stopped running", so a card reading
                # "Fieldwork execution complete" sat directly above a body saying
                # 0 of 2 completed and 2 failed.
                "headline": (
                    f"Fieldwork ran — {counted(attention, 'test')} "
                    f"{verb(attention)} you"
                    if attention else "Fieldwork complete"
                ),
                "summary": (
                    f"Completed {done} of {counted(len(units), 'scheduled test')}. "
                    f"{counted(attention, 'test')} failed or "
                    f"{verb(attention)} your attention."
                    if attention else
                    f"Completed all {counted(len(units), 'scheduled test')}."
                ),
                "metrics": [
                    {"label": "Scheduled", "value": len(units)},
                    {"label": "Completed", "value": done},
                    {"label": "Needs attention", "value": attention},
                    {
                        "label": "Open evidence requests",
                        "value": sum(
                            item.get("status") in {"open", "requested"}
                            for item in subject.evidence_requests
                        ),
                    },
                ],
                # Which tests stopped, and why. A count of failures tells an
                # auditor to go looking; the titles tell them where.
                "highlights": [
                    {
                        "severity": (
                            "error" if unit.get("status") in {"failed", "conflict"}
                            else "warning"
                        ),
                        "label": narration.subject_of(unit)
                        or str(unit.get("title") or "Scheduled test"),
                        "detail": narration.humanize(unit.get("error_code"))
                        or str(unit.get("error") or "").strip()
                        or f"Stopped as {narration.humanize(unit.get('status'))}.",
                        "artifact_ref": next(
                            iter(unit.get("result_refs") or []), ""
                        ),
                    }
                    for unit in units
                    if unit.get("status") in {
                        "failed", "conflict", "blocked",
                        "awaiting_input", "awaiting_confirmation",
                    }
                ][:HIGHLIGHT_LIMIT],
                "artifact_refs": refs,
            }
        if capability_id == "results.rolled_up":
            rows = [
                row.get("execution_rollup") or {}
                for row in subject.rcm
            ]
            exceptions = sum(int(row.get("exceptions") or 0) for row in rows)
            exception_observations = sum(
                item.get("outcome") == "exception" for item in subject.observations
            )
            return {
                "status": (
                    "completed_with_issues"
                    if exceptions or attention
                    else state
                ),
                "headline": "Results and observations updated",
                "summary": (
                    f"Rolled results into {counted(len(rows), 'RCM row')}. "
                    f"Recorded {counted(exceptions, 'exception')} across "
                    f"{counted(exception_observations, 'exception observation')}."
                ),
                "metrics": [
                    {"label": "RCM rows", "value": len(rows)},
                    {"label": "Exceptions", "value": exceptions},
                    {"label": "Exception observations", "value": exception_observations},
                ],
                # Where the exceptions landed, worst first — the question an
                # auditor asks the moment fieldwork stops.
                "highlights": [
                    {
                        "severity": (
                            "error"
                            if str((row.get("execution_rollup") or {}).get("control_conclusion") or "")
                            == "ineffective"
                            else "warning"
                        ),
                        "label": _rcm_label(row),
                        "detail": (
                            f"{counted(int((row.get('execution_rollup') or {}).get('exceptions') or 0), 'exception')}"
                            f" across "
                            f"{counted(int((row.get('execution_rollup') or {}).get('completed') or 0), 'completed test')}"
                            + (
                                " — control concluded ineffective."
                                if str((row.get("execution_rollup") or {}).get("control_conclusion") or "")
                                == "ineffective"
                                else "."
                            )
                        ),
                        "artifact_ref": f"rcm:{row.get('id')}",
                    }
                    for row in sorted(
                        (
                            item for item in subject.rcm
                            if int((item.get("execution_rollup") or {}).get("exceptions") or 0)
                        ),
                        key=lambda item: (
                            -int((item.get("execution_rollup") or {}).get("exceptions") or 0),
                            str(item.get("id") or ""),
                        ),
                    )
                ][:HIGHLIGHT_LIMIT],
                "artifact_refs": refs,
            }
        if capability_id == "findings.drafted":
            findings = [
                item for item in subject.findings
                if item.get("agent_run_id") == run.get("id")
            ]
            by_severity: dict[str, int] = {}
            for item in findings:
                severity = str(item.get("severity") or "unspecified")
                by_severity[severity] = by_severity.get(severity, 0) + 1
            distribution = ", ".join(
                f"{count} {severity}" for severity, count in sorted(by_severity.items())
            ) or "no new drafts"
            return {
                "status": state,
                "headline": "Finding drafts prepared",
                "summary": (
                    f"Prepared {counted(len(findings), 'evidence-linked finding draft')} "
                    f"({distribution})."
                    + (
                        f" {counted(attention, 'item')} {verb(attention)} attention."
                        if attention else ""
                    )
                ),
                "metrics": [
                    {"label": "Drafts prepared", "value": len(findings)},
                    {"label": "Needs attention", "value": attention},
                ],
                # The most material drafts, named. A severity distribution says
                # how many; only the titles say what.
                "highlights": [
                    {
                        "severity": (
                            "error"
                            if str(item.get("severity") or "").casefold()
                            in {"critical", "high"}
                            else "warning"
                        ),
                        "label": str(item.get("title") or "Untitled finding"),
                        "detail": _lead_sentence(
                            str(item.get("narrative") or ""), section="Condition"
                        ) or f"{str(item.get('severity') or 'unspecified').capitalize()} severity.",
                        "artifact_ref": f"finding:{item.get('id')}",
                    }
                    for item in sorted(
                        findings,
                        key=lambda item: (
                            _SEVERITY_RANK.get(
                                str(item.get("severity") or "").casefold(), 9
                            ),
                            str(item.get("id") or ""),
                        ),
                    )
                ][:HIGHLIGHT_LIMIT],
                "artifact_refs": refs,
            }
        if capability_id == "working_papers.generated":
            return {
                "status": state,
                "headline": "RCM working papers generated",
                "summary": (
                    f"Generated {done} of "
                    f"{counted(len(units), 'scheduled working paper')}."
                    + (
                        f" {counted(attention, 'paper')} could not be completed."
                        if attention else ""
                    )
                ),
                "metrics": [
                    {"label": "Generated", "value": done},
                    {"label": "Needs attention", "value": attention},
                ],
                "artifact_refs": refs,
            }
        if capability_id == "report.working_draft":
            reconciliation = any(
                unit.get("status") == "awaiting_confirmation" for unit in units
            )
            supported = sum(
                bool(item.get("auditor_confirmed"))
                for item in subject.findings
            )
            return {
                "status": "needs_review" if reconciliation else state,
                "headline": "Report working draft ready",
                "summary": (
                    "Prepared the report working draft using "
                    + counted(supported, "auditor-confirmed finding")
                    + ". "
                    + (
                        "An auditor-edited draft was preserved and needs reconciliation."
                        if reconciliation
                        else "No draft reconciliation is pending."
                    )
                ),
                "metrics": [
                    {"label": "Confirmed findings represented", "value": supported},
                    {"label": "Reconciliation required", "value": reconciliation},
                ],
                "artifact_refs": refs or ["report:draft"],
            }
        if capability_id == "audit.verified":
            outcome = run.get("audit_outcome") or {}
            output_gate_count = len(output_issues(outcome)) if outcome else 0
            return {
                "status": "completed" if outcome.get("audit_complete") else "needs_review",
                "headline": (
                    "Audit verification complete"
                    if outcome.get("audit_complete")
                    else "Audit verification found open items"
                ),
                "summary": (
                    f"Completion status: {outcome.get('completion_status') or 'unknown'}. "
                    f"Report quality errors: "
                    f"{outcome.get('report_quality_errors') or 0}; "
                    f"open output gates: {output_gate_count}."
                ),
                "metrics": [
                    {"label": "Audit complete", "value": bool(outcome.get("audit_complete"))},
                    {
                        "label": "Report quality errors",
                        "value": int(outcome.get("report_quality_errors") or 0),
                    },
                    {"label": "Open output gates", "value": output_gate_count},
                ],
                "artifact_refs": refs or [VERIFICATION_REF],
            }
        return None

    def _curated_document_ids(self) -> list[str] | None:
        """The auditor's explicit planning-document curation, if any.

        Curation lives on the run, not in a derived snapshot: when the auditor
        named documents, every model-backed planning capability is restricted to
        exactly those. Otherwise each capability's declared bounded selector
        decides which documents are relevant.
        """
        curated = [
            str(value).strip()
            for value in (self.context.get("document_ids") or [])
            if str(value).strip()
        ]
        return curated or None

    def _bind_planning_context(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind planning-context synthesis to the shared ``UnitPipeline``.

        Synthesis reads only the declared context — the current planning context
        and bounded material from the selected documents — and the registered
        ``planning.context`` executor merges the accepted fields under the
        planning-context parent guard, preserving auditor-entered fields.

        When no document supplies usable material there is nothing to synthesize
        from, so the unit settles without a model call and the existing context
        stands. Producing durable document analyses is the documents subsystem's
        own capability, not a side effect of planning.
        """
        self.ws = subject
        document_ids = self._curated_document_ids()
        scope = planning_context_scope(self.ws, document_ids=document_ids)
        supplied = scope.candidates.get("planning_documents") or ()
        task = self.add_task(
            "context",
            "planning:context",
            "Assemble planning context",
            "Reviewing workspace data and available documents…",
        )
        if not supplied:
            self.warn(
                "No document material is available to synthesize planning context; "
                "the current context stands."
            )
            self.task_status(task, "completed")
            return DeterministicUnitResult("succeeded", ("planning:context",))
        expected_context = parent_hashes(self.ws, ["planning:context"])
        target = PlanningContextExecutorTarget(self.ws, self.run["id"])
        self.task_detail(
            task,
            f"Synthesizing planning context from {len(supplied)} "
            f"document{'s' if len(supplied) != 1 else ''}…",
        )

        def context_provider():
            return resolve_context(self, self.context_resolver, capability, unit, scope)

        def approval_provider(proposal):
            accepted = self.request_approval(
                "context",
                task,
                [
                    self.proposal_item(
                        "Planning context from imported documents",
                        "Grounded facts extracted from the selected engagement "
                        "material.",
                        {"context": dict(proposal.get("context") or {})},
                        {"document_ids": list(document_ids or [])},
                    )
                ],
            )
            return {"context": dict(accepted[0]["spec"]["context"])} if accepted else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            if outcome.receipt is not None and dict(outcome.receipt.output).get(
                "recovered_from_labelled_facts"
            ):
                self.warn(
                    "Planning-context synthesis returned no usable fields; "
                    "recovered labelled facts from the supplied documents."
                )
            self.record_artifact(
                "planning", "context", "planning:context", "updated", task
            )
            self.task_status(task, "completed")

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="planning.context",
                executor_id="planning.context",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": ["planning:context"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_context,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "context" if self.run["mode"] == "permission" else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            # Deliberately not a post-commit postcondition: a committed
            # synthesis is a real outcome even when the available sources do
            # not establish every planning detail.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _bind_apm(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind the APM capability to the shared ``UnitPipeline``.

        This is the native pipeline binding for ``planning.apm_ready``: the
        domain-neutral :class:`WorkflowRunner` owns manifest/proposal/receipt
        persistence, proposal reuse, approval, and readiness reevaluation. The
        binding supplies only the APM-specific context, executor target, and two
        domain callbacks — post-commit bookkeeping and the auditor-edit-preserved
        conflict translation — that the scheduler invokes.
        """
        self.ws = subject
        expected_context = parent_hashes(self.ws, ["planning:context"])
        target = ApmExecutorTarget(
            self.ws,
            self.run["id"],
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task("apm", "workflow:apm", "Audit planning memorandum")

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                apm_document_methodology_scope(
                    self.ws,
                    document_ids=self._curated_document_ids(),
                ),
            )

        def approval_provider(proposal):
            proposals = [
                self.proposal_item(
                    "Audit planning memorandum",
                    "Drafted from the current planning basis.",
                    dict(proposal),
                )
            ]
            accepted = self.request_approval("apm", task, proposals)
            return dict(accepted[0]["spec"]) if accepted else None

        def on_committed(_stage, _unit, _outcome) -> None:
            self.ws = target.workspace
            self.run["planning_changes"]["apm_updated"] += 1
            self.record_artifact("planning", "apm", "planning:apm", "updated", task)

        def conflict_handler(_stage, _unit, error) -> tuple[str, str] | None:
            if str(error) != AUDITOR_EDIT_PRESERVED:
                return None
            self.run.setdefault("planning_revisions", []).append(
                {
                    "kind": "apm",
                    "status": "proposed",
                    "sidecar": dict(error.proposal_reference or {}),
                }
            )
            self.run["planning_changes"]["apm_proposed"] += 1
            return ("awaiting_confirmation", "Auditor-owned APM was preserved.")

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="planning.apm",
                executor_id="planning.apm",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": ["planning:apm"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_context,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=("apm" if self.run["mode"] == "permission" else None),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            readiness_provider=lambda: capability.readiness(target.workspace, {}),
            on_committed=on_committed,
            conflict_handler=conflict_handler,
        )

    def _bind_rcm(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind the RCM capability to the shared ``UnitPipeline``.

        The domain-neutral scheduler owns manifest/proposal/receipt persistence,
        proposal reuse, approval, and readiness reevaluation. This binding
        supplies only the RCM-specific declared context, the row-commit executor
        target, per-row approval items, and the post-commit bookkeeping callback
        that translates receipt row actions into planning-change accounting.
        """
        self.ws = subject
        expected_apm = parent_hashes(self.ws, ["planning:apm"])
        target = RcmExecutorTarget(
            self.ws,
            self.run["id"],
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task("rcm", "workflow:rcm", "Risk and control matrix")

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                rcm_scope(
                    self.ws,
                    document_ids=self._curated_document_ids(),
                ),
            )

        def approval_provider(proposal):
            proposed = []
            for raw in proposal.get("rows") or []:
                spec = dict(raw)
                spec["semantic_id"] = str(
                    spec.get("semantic_id")
                    or f"rcm:{slugify(str(spec.get('process') or ''))}:{slugify(str(spec.get('risk') or ''))}"
                )
                proposed.append(
                    self.proposal_item(
                        str(spec.get("risk")), "Risk/control matrix revision.", spec
                    )
                )
            accepted = self.request_approval("rcm", task, proposed)
            rows = [dict(item["spec"]) for item in accepted]
            return {"rows": rows} if rows else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            for row in (outcome.receipt.output.get("rows") or []) if outcome.receipt else []:
                action = str(row.get("action"))
                if action == "preserved":
                    self.warn(f"Preserved auditor-owned RCM row '{row['id']}'.")
                    self.run["planning_changes"]["rcm_preserved"] += 1
                    continue
                self.run["planning_changes"][f"rcm_{action}"] += 1
                self.record_artifact(
                    "rcm", str(row["id"]), str(row["semantic_id"]), action, task
                )
            # Reconciliation the matrix passed on a single shared word. Too
            # weak to refuse a matrix over — it flagged three of ten themes on
            # one that covered all of them — and too pointed to discard, since
            # a theme nothing in the matrix discusses is exactly how a planned
            # response becomes no procedure. The auditor decides.
            for theme in planning_workers.weakly_owned_themes(
                str(self.ws.planning.get("apm_markdown") or ""), self.ws.rcm
            ):
                self.warn(
                    f"Check that the matrix covers the planned risk theme "
                    f"'{theme}': no row states it in the memorandum's terms."
                )
            # A row asserting that no imported table answers it makes the test
            # worker withhold every schema, so no data test can be generated
            # for it at all. Often right — a bid file is not in a table — and
            # too often wrong to enforce, so the auditor is told and decides.
            profiles = [
                candidate.representations["table_profile"]
                for candidate in apm_table_profile_candidates(
                    self.ws, imported_only=True
                )
            ]
            for row in planning_workers.untested_population_rows(
                profiles, self.ws.rcm
            ):
                self.warn(
                    f"Check that no population test applies to '{row}': the row "
                    "asserts none does, which makes a data test unreachable for it."
                )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="planning.rcm",
                executor_id="planning.rcm",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                    # The vocabulary a transaction-cycle attribute may address.
                    # On the unit input rather than in the prompt because it is
                    # per-workspace, and covered by the unit's own input hash so
                    # a re-derived schema re-runs the matrix rather than leaving
                    # a requirement pointing at a field that moved.
                    #
                    # It belongs to *this* unit: ``_with_evidence_contracts``
                    # reads it, and it is the RCM worker that runs. Sent with
                    # the APM instead, the evidence turn was handed an empty
                    # vocabulary under a prompt promising it the engagement's
                    # fields, and every transaction-cycle attribute came back
                    # `unsupported` — correctly, and for a reason no reader of
                    # the matrix could have guessed.
                    "schema_catalog": cycle_linking.schema_catalog(self.ws),
                },
                activity={
                    "artifact_refs": ["planning:apm"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_apm,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=("rcm" if self.run["mode"] == "permission" else None),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            readiness_provider=lambda: capability.readiness(target.workspace, {}),
            on_committed=on_committed,
        )

    def _bind_test_generate(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind one RCM row's test-generation unit to the shared ``UnitPipeline``.

        The domain-neutral scheduler owns manifest/proposal/receipt persistence,
        proposal reuse, approval, and readiness reevaluation. This binding
        supplies only the row-scoped declared context, the generation commit
        target, per-test approval items, and the post-commit bookkeeping that
        translates receipt actions into planning-change accounting.

        Replaces ``_bind_test_draft``/``_bind_test_spec``: the RCM row is the
        sole guarded parent, since one model turn now decides every test's
        source and writes its complete executable definition in one commit
        (docs/test-capability-merge-plan.md, section 6).
        """
        self.ws = subject
        rcm_id = unit["parent_refs"][0].split(":", 1)[1]
        expected_row = parent_hashes(self.ws, [f"rcm:{rcm_id}"])
        target = TestGenerateExecutorTarget(
            self.ws,
            self.run["id"],
            rcm_id,
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task(
            "test_specs", "workflow:test_specs", "Executable test specifications"
        )

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                test_generate_scope(
                    self.ws,
                    rcm_id,
                    document_ids=self._curated_document_ids(),
                ),
            )

        def approval_provider(proposal):
            proposed = [
                self.proposal_item(
                    str(spec.get("title") or spec.get("objective")),
                    f"{spec.get('source')} test for {rcm_id}.",
                    dict(spec),
                )
                for spec in proposal.get("tests") or []
            ]
            accepted = self.request_approval("test_specs", task, proposed)
            tests = [dict(item["spec"]) for item in accepted]
            return {"tests": tests} if tests else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            for item in (
                (outcome.receipt.output.get("tests") or []) if outcome.receipt else []
            ):
                action = str(item.get("action"))
                self.run["planning_changes"][f"test_{action}"] += 1
                self.record_artifact(
                    str(item["kind"]), str(item["id"]), "", action, None
                )
            # A requirement the evidence cannot answer produces no cycle test
            # and no validation error — the generation turn has no move that
            # would change that. Without this the row commits looking complete.
            row = next(
                (item for item in self.ws.rcm if str(item.get("id")) == rcm_id), None
            )
            for warning in cycle_vouching.unanswerable_cycle_requirements(
                self.ws, row or {}
            ):
                self.warn(warning)

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="tests.generate",
                executor_id="tests.generate",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": [f"rcm:{rcm_id}"],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_row,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "test_specs" if self.run["mode"] == "permission" else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            # Generation fans out one unit per RCM row, so this capability's
            # readiness is only satisfied once every row's unit has committed.
            # Post-commit readiness is therefore evaluated by the stage fold,
            # not per unit.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _bind_execution(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one fieldwork-execution unit to the execution it actually needs.

        This capability fans out into four unit kinds and only one of them —
        document Q&A — asks the model anything. The binding therefore returns a
        ``BoundUnitPipeline`` for that kind and a ``DeterministicUnitResult`` for
        the local ones, which is how a capability with mixed units keeps exactly
        one execution binding.

        Only the datatest branch is audit-specific. Every Document Test unit kind
        is bound by :func:`doc_tests_execution.bind_document_test_unit`, the same
        function the standalone ``doc_tests_workflow_v2`` composition uses, so a
        worklist behaves identically whichever graph scheduled it.
        """
        self.ws = subject
        # Existing execution services combine compute and mutation and check the
        # revision they were handed, so each unit runs against a freshly loaded
        # workspace rather than the subject the stage started with.
        refresh_workspace(self)
        artifact_ref = unit["parent_refs"][-1]
        kind, item_id = artifact_ref.split(":", 1)
        if kind != "datatest":
            task = self.add_task(
                "execution", "workflow:execution", "Fieldwork execution"
            )
            return bind_document_test_unit(self, capability, unit, task=task)
        try:
            outcome = run_data_test(self.ws, item_id)
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        if outcome.executed:
            self.emit(
                "workspace_changed",
                {"kind": kind, "id": item_id, "action": "executed"},
            )
        return DeterministicUnitResult(
            outcome.status, (outcome.artifact_ref,), outcome.error
        )

    def _bind_rollup(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``results.rolled_up``.

        Roll-up recomputes each RCM row's derived result and its observations from
        the current execution artifacts and persists only material changes.
        Observation identities are keyed on ``execution_ref``, so a repeated
        roll-up reuses the same observation rows rather than creating duplicates.
        On success the binder emits the ``workspace_changed`` roll-up signal the
        generic deterministic path does not. No model call or approval is involved.
        """
        self.ws = subject
        try:
            refs = roll_up_results(
                self.ws,
                rcm_ids=set(target_rcm_ids(self.ws, workflow_scope(self.run))),
            )
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        # Roll-up is the first point that can see the fieldwork as a whole, and
        # so the only one that can report the populations it never reached. A
        # per-row conclusion cannot: every row concluded on the tests it had.
        for population in untested_populations(self.ws):
            self.warn(
                f"No executed data test makes a statement about the '{population}' "
                "population; its rows are outside the tested scope."
            )
        self.emit(
            "workspace_changed", {"kind": "rcm", "id": "rollup", "action": "updated"}
        )
        return DeterministicUnitResult("succeeded", tuple(refs))

    def _bind_finding(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind one eligible observation's finding-draft unit to the pipeline.

        The binding supplies only the observation-scoped declared context, the
        finding commit target, the approval item, and the post-commit workspace
        signal. Evidence linkage and support validation live in the executor,
        where they run against the committed workspace state.
        """
        self.ws = subject
        observation_id = unit["parent_refs"][0].split(":", 1)[1]
        expected_observation = parent_hashes(self.ws, [f"observation:{observation_id}"])
        target = FindingExecutorTarget(self.ws, self.run["id"], observation_id)
        task = self.add_task("findings", "workflow:findings", "Eligible finding drafts")

        def context_provider():
            return resolve_context(
                self,
                self.context_resolver,
                capability,
                unit,
                finding_draft_scope(self.ws, observation_id),
            )

        def approval_provider(proposal):
            accepted = self.request_approval(
                "finding_drafts",
                task,
                [
                    self.proposal_item(
                        unit["title"],
                        "Draft from an exception observation.",
                        dict(proposal.get("finding") or {}),
                    )
                ],
            )
            return {"finding": dict(accepted[0]["spec"])} if accepted else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            if outcome.receipt is None:
                return
            self.emit(
                "workspace_changed",
                {
                    "kind": "finding",
                    "id": str(outcome.receipt.output["id"]),
                    "action": "created",
                },
            )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="reporting.finding",
                executor_id="reporting.finding",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": list(unit.get("parent_refs") or []),
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected_observation,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "finding_drafts" if self.run["mode"] == "permission" else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            # Findings fan out one unit per eligible observation, so this
            # capability's readiness only holds once every unit has committed.
            readiness_provider=None,
            on_committed=on_committed,
        )

    def _bind_working_papers(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``working_papers.generated``.

        Each unit renders and commits one RCM working paper. Generation is a pure
        projection of current RCM/execution state, and the commit is parent-hash
        guarded, so a changed RCM parent surfaces as a conflict rather than an
        overwrite. No model call or approval is involved.
        """
        self.ws = subject
        rcm_id = unit["parent_refs"][0].split(":", 1)[1]
        try:
            ref = generate_working_paper(self.ws, rcm_id)
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        return DeterministicUnitResult("succeeded", (ref,))

    def _bind_report(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``report.working_draft``.

        The workflow assembles the draft from current planning, results, and
        findings without a model call, so this capability has no worker. An
        auditor-edited draft is preserved and its regenerated candidate is left
        for reconciliation, which is what ``awaiting_confirmation`` records. On
        success the binder emits the ``workspace_changed`` report signal the
        generic deterministic path does not.
        """
        self.ws = subject
        ref, requires_reconcile = generate_report_draft(
            self.ws,
            run_id=self.run["id"],
            workflow=self.run.get("workflow"),
        )
        self.emit(
            "workspace_changed", {"kind": "report", "id": "draft", "action": "updated"}
        )
        if requires_reconcile:
            return DeterministicUnitResult(
                "awaiting_confirmation",
                (ref,),
                "Auditor-edited report preserved; reconcile the generated candidate.",
            )
        return DeterministicUnitResult("succeeded", (ref,))

    def _bind_unreachable(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """The executor for a capability that never expands a unit.

        Registered so the execution registry covers every declared capability,
        and never called: `_run_stage` settles a stage with no units from its
        readiness alone. If this ever raises, the capability grew units it was
        not supposed to have.
        """
        raise WorkspaceError(
            f"Capability '{capability.id}' expands no units and cannot be executed."
        )

    def _bind_verify(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``audit.verified``.

        Verification is read-only: it computes the completion/quality/output
        outcome, records it on the run for the completion projection, and derives
        the terminal unit status from it. No workspace mutation, model call, or
        approval is involved.
        """
        self.ws = subject
        outcome = verify_audit(self.ws)
        self.run["audit_outcome"] = outcome
        if outcome["audit_complete"]:
            return DeterministicUnitResult("succeeded", (VERIFICATION_REF,))
        error = (
            f"Completion status: {outcome['completion_status']}; "
            f"report errors: {outcome['report_quality_errors']}; "
            f"output gates: {len(output_issues(outcome))}."
        )
        return DeterministicUnitResult("blocked", (VERIFICATION_REF,), error)

    # --------------------------------------------------------- interactions
    def _wait_interaction_response(self, interaction: dict) -> dict:
        return self.runtime.wait_for_interaction(interaction)

    def _resolve_interaction_record(self, interaction: dict, response: dict) -> None:
        self.runtime.resolve_interaction(interaction, response)

    # ------------------------------------------------------------ finish
    def _finish_projection(
        self,
        subject: Workspace,
        _workflow_state: dict,
        stages: tuple[dict, ...],
    ) -> FinishProjection:
        """Close the run on real audit outcomes, not on unit bookkeeping.

        A run that executed every unit cleanly can still be incomplete — open
        evidence requests or an unreconciled
        report. Those become `next_outcomes`, the exact input "Continue audit"
        replays, and they are why `completed_with_open_items` is a distinct
        terminal status from `completed`.
        """
        self.ws = subject
        completion = rcm_execution.completion(subject)
        failed = sum(unit.get("status") in {"failed", "conflict"} for stage in stages for unit in stage.get("units") or [])
        open_units = sum(unit.get("status") in {"blocked", "awaiting_input", "awaiting_confirmation"} for stage in stages for unit in stage.get("units") or [])
        open_evidence = [item for item in subject.evidence_requests if item.get("status") == "open"]
        next_outcomes = []
        execution_open = bool(open_evidence) or any(
            stage.get("capability") == "fieldwork.executed"
            and any(
                unit.get("status") in {"blocked", "awaiting_input", "awaiting_confirmation"}
                for unit in stage.get("units") or []
            )
            for stage in stages
        )
        if execution_open:
            next_outcomes.extend(["fieldwork.executed", "results.rolled_up"])
        reconciliation_open = any(
            stage.get("capability") == "report.working_draft"
            and any(unit.get("status") == "awaiting_confirmation" for unit in stage.get("units") or [])
            for stage in stages
        )
        if execution_open or reconciliation_open:
            next_outcomes.extend(["report.working_draft", "audit.verified"])
        self.run["workflow"]["workspace_revision"] = subject.revision
        requires_full_completion = "audit.verified" in self.run["workflow"].get("requested_outcomes", [])
        terminal = fold_terminal_status(
            stages,
            complete=completion["status"] == "completed" or not requires_full_completion,
        )
        if failed:
            self.run["error"] = first_unit_error(
                stages, "One or more workflow units failed."
            )
            # A capability that did not settle is what a follow-up run should
            # reattempt. Without this a partly failed audit closes with nothing
            # to continue, and the only affordance left is a full retry.
            next_outcomes.extend(unsettled_capabilities(stages))
        summary = narration.summary_markdown(
            "Audit workflow",
            [
                ("Requested", [
                    narration.humanize(item)
                    for item in self.run["workflow"]["requested_outcomes"]
                ]),
                ("Completion", None if completion["status"] == "completed" else completion["status"]),
                ("Failed or conflicting units", failed),
                ("Open workflow units", open_units),
                ("Exception observations", sum(
                    item.get("outcome") == "exception"
                    for item in subject.observations
                )),
                ("Open evidence requests", len(open_evidence)),
            ],
        )
        return FinishProjection(
            next_outcomes=tuple(dict.fromkeys(next_outcomes)),
            summary_markdown=summary,
            terminal_status=terminal,
        )


_PARTIAL_DEPENDENCIES = {
    # Relationship diagnosis is local and independent by pair, so one failed
    # diagnostic need not prevent the utility stage from evaluating the rest.
    "data.join_utility_ready": {"data.relationships_inferred"},
    # A safe auto-selected join, a skipped unrelatable pair, or an
    # auditor-held join choice must not withhold analysis of the frames that
    # remain usable. Later analysis capabilities re-expand from the joins that
    # actually committed, matching the standalone analysis workflow. What is
    # deliberately *not* partial is ``data.joins_ready`` in
    # ``data.join_utility_ready``: a pair whose gate never answered has nothing
    # admitting it, and materializing it anyway would bypass the gate outright.
    "analysis.register_ready": {"data.joins_ready"},
    "analysis.definitions_ready": {"analysis.register_ready"},
    "analysis.executed": {"analysis.definitions_ready"},
    # One procedure that would not execute must not withhold the memo. A
    # summary written over the results that did land is the useful artifact,
    # and the procedure that failed is itself reported in "further work".
    "analysis.summarized": {"analysis.executed"},
    # A document the run could not extract or analyze must never stop the audit:
    # planning consumes the document material that exists, which is exactly what
    # the scoped document dependency is for. The document chain's own edges are
    # partial for the same reason — one unanalyzable document does not withhold
    # the analyses of the others.
    "documents.analysis_chunks_ready": {"documents.text_ready"},
    "documents.analysis_generated": {"documents.analysis_chunks_ready"},
    # Sources are partial for a related but distinct reason. An auditor can
    # create an engagement, record a brief, and ask for a planning memorandum
    # before importing anything — `test_planning_run_without_tables_and_user_safe_rerun`
    # covers exactly that, and the RCM it produces correctly drafts no tests
    # because there is nothing to test against. The edge is there to *order*
    # sources before planning and to give the record a stage to draw, not to
    # forbid planning from a brief alone.
    "planning.context_ready": {"sources.imported", "documents.analysis_generated"},
    # Promotion is additive: it carries an exploratory procedure into a test
    # the engagement would not otherwise have. A fit the model could not make
    # is one test the audit does not gain, and must never withhold the tests it
    # already has — without this, one unsatisfiable placement blocked every
    # data and document test in the engagement.
    "fieldwork.executed": {"tests.specified", "tests.promoted_from_analysis"},
    "results.rolled_up": {"fieldwork.executed"},
    "report.working_draft": {"findings.drafted"},
    "audit.verified": {
        "working_papers.generated",
        "report.working_draft",
    },
}


def build_audit_workflow_runner(
    workspace: Workspace,
    run: dict,
    handle,
    *,
    runtime: RunRuntime | None = None,
    context_resolver: ContextResolver | None = None,
) -> WorkflowRunner:
    """Compose the domain-neutral scheduler with temporary audit adapters."""

    adapter = AuditWorkflowExecution(
        workspace,
        run,
        handle,
        runtime=runtime,
        context_resolver=context_resolver,
    )
    unit_pipeline = UnitPipeline(
        runtime=adapter.runtime,
        gateway=adapter.model_gateway,
        workers=WORKERS,
        executors=EXECUTORS,
        sidecars=UnitSidecarStore(workspace, run["id"]),
    )
    # Every audit capability is bound to a native scheduler path. A
    # pipeline-backed capability supplies a per-unit binding the domain-neutral
    # scheduler drives through ``UnitPipeline``; a deterministic one supplies a
    # per-unit computation with no model call. There is no transitional batch
    # handler left.
    _PIPELINE_BINDERS = {
        "planning.context_ready": (
            adapter._bind_planning_context,
            {"worker": "planning.context", "executor": "planning.context"},
        ),
        "planning.apm_ready": (
            adapter._bind_apm,
            {"worker": "planning.apm", "executor": "planning.apm"},
        ),
        "planning.rcm_ready": (
            adapter._bind_rcm,
            {"worker": "planning.rcm", "executor": "planning.rcm"},
        ),
        "tests.specified": (
            adapter._bind_test_generate,
            {"worker": "tests.generate", "executor": "tests.generate"},
        ),
        "fieldwork.executed": (
            adapter._bind_execution,
            {
                "worker": "fieldwork.document_qa",
                "executor": "fieldwork.document_qa",
                "deterministic": (
                    "fieldwork.data_test_run|fieldwork.document_test_run|"
                    "fieldwork.document_test_review"
                ),
            },
        ),
        "findings.drafted": (
            adapter._bind_finding,
            {"worker": "reporting.finding", "executor": "reporting.finding"},
        ),
    }
    # Capabilities whose every unit is deterministic, bound through the
    # scheduler's deterministic execution path.
    _DETERMINISTIC_BINDERS = {
        # `sources.imported` expands no units, so the runner returns before it
        # ever resolves an execution — see `_run_stage`, which short-circuits on
        # an empty unit list. The binding exists because every declared
        # capability must have one, and it raises rather than pretending to
        # work: reaching it would mean the agent had been asked to import,
        # which is the auditor's act and not a thing this system can do.
        "sources.imported": (
            adapter._bind_unreachable,
            {"deterministic": "sources.imported"},
        ),
        "results.rolled_up": (
            adapter._bind_rollup,
            {"deterministic": "fieldwork.rollup"},
        ),
        "working_papers.generated": (
            adapter._bind_working_papers,
            {"deterministic": "reporting.working_paper"},
        ),
        "report.working_draft": (
            adapter._bind_report,
            {"deterministic": "reporting.report_draft"},
        ),
        "audit.verified": (
            adapter._bind_verify,
            {"deterministic": "reporting.verify"},
        ),
    }
    # Full-audit runs include the same exploratory analysis capability branch as
    # the standalone analysis workflow. The audit graph schedules this branch
    # before planning, but does not make APM semantically depend on it.
    analysis_adapter = AnalysisWorkflowExecution(
        workspace,
        run,
        handle,
        runtime=adapter.runtime,
        context_resolver=adapter.context_resolver,
    )
    analysis_adapter.unit_pipeline = unit_pipeline
    # Both graphs bind the analysis capabilities through the same analysis
    # adapter methods, so a capability behaves identically whether an audit run
    # or a standalone analysis request scheduled it.
    _ANALYSIS_PIPELINE_BINDERS = {
        "data.join_utility_ready": (
            analysis_adapter._bind_join_utility,
            {"worker": "analysis.join_utility", "executor": None},
        ),
        "analysis.register_ready": (
            analysis_adapter._bind_register,
            {"worker": "analysis.reading", "executor": "analysis.register"},
        ),
        "analysis.definitions_ready": (
            analysis_adapter._bind_definitions,
            {"worker": "analysis.definitions", "executor": "analysis.definitions"},
        ),
        "analysis.summarized": (
            analysis_adapter._bind_summary,
            {"worker": "analysis.summary", "executor": "analysis.summary"},
        ),
        "tests.promoted_from_analysis": (
            analysis_adapter._bind_promotion,
            {"worker": "analysis.promotion", "executor": "analysis.promotion"},
        ),
    }
    _ANALYSIS_DETERMINISTIC_BINDERS = {
        "data.relationships_inferred": (
            analysis_adapter._bind_relationships,
            {"deterministic": "analysis.relationships"},
        ),
        "data.joins_ready": (
            analysis_adapter._bind_join,
            {"deterministic": "analysis.join"},
        ),
        "analysis.executed": (
            analysis_adapter._bind_execution,
            {"deterministic": "analysis.execution"},
        ),
    }
    # The three document generation capabilities ``planning.context_ready``
    # depends on are declared and implemented once, in the document group. The
    # audit composition binds them through the same execution adapter rather than
    # through a second implementation, sharing this run's runtime and ledger lock
    # so both write one durable record under one lock.
    document_adapter = DocumentWorkflowExecution(
        workspace,
        run,
        handle,
        runtime=adapter.runtime,
        state_lock=adapter._state_lock,
        context_resolver=adapter.context_resolver,
    )
    document_adapter.unit_pipeline = unit_pipeline
    executions = build_document_capability_executions(
        document_adapter, audit_capabilities.AUDIT_DOCUMENT_GROUP.capabilities()
    )
    for capability in audit_capabilities.REGISTRY.all():
        if capability.id in audit_capabilities.AUDIT_DOCUMENT_GROUP.CAPABILITY_IDS:
            continue
        analysis_pipeline_binding = _ANALYSIS_PIPELINE_BINDERS.get(capability.id)
        if analysis_pipeline_binding is not None:
            binder, identity = analysis_pipeline_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=workflow.canonical_sha256(
                        {"capability": capability.id, **identity}
                    ),
                    pipeline_binder=binder,
                )
            )
            continue
        analysis_binder = _ANALYSIS_DETERMINISTIC_BINDERS.get(capability.id)
        if analysis_binder is not None:
            binder, identity = analysis_binder
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=workflow.canonical_sha256(
                        {"capability": capability.id, **identity}
                    ),
                    deterministic_executor=binder,
                )
            )
            continue
        pipeline_binding = _PIPELINE_BINDERS.get(capability.id)
        if pipeline_binding is not None:
            binder, identity = pipeline_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=workflow.canonical_sha256(
                        {"capability": capability.id, **identity}
                    ),
                    pipeline_binder=binder,
                )
            )
            continue
        binder, identity = _DETERMINISTIC_BINDERS[capability.id]
        executions.register(
            CapabilityExecution(
                capability_id=capability.id,
                implementation_hash=workflow.canonical_sha256(
                    {"capability": capability.id, **identity}
                ),
                deterministic_executor=binder,
            )
        )

    def dependency_policy(
        capability_id: str,
        dependency_id: str,
        _dependency_status: str,
    ) -> bool:
        return dependency_id in _PARTIAL_DEPENDENCIES.get(capability_id, set())

    checkpoint_handlers = {
        DOCUMENT_SCOPE_CHECKPOINT: document_adapter._scope_checkpoint,
        ANALYSIS_SCOPE_CHECKPOINT: analysis_adapter._scope_checkpoint,
    }
    stage_checkpoints = {
        **DOCUMENT_STAGE_CHECKPOINTS,
        **ANALYSIS_STAGE_CHECKPOINTS,
    }

    def before_stage(
        subject: Workspace,
        capability: workflow.Capability,
        _stage: dict,
    ) -> None:
        adapter.ws = subject
        document_adapter.ws = subject
        analysis_adapter.ws = subject
        checkpoint = stage_checkpoints.get(capability.id)
        if checkpoint is not None and run.get("mode") == "permission":
            checkpoint_handlers[checkpoint]()

    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=adapter.runtime,
        registry=audit_capabilities.REGISTRY,
        executions=executions,
        unit_pipeline=unit_pipeline,
        refresh_subject=lambda: refresh_workspace(adapter),
        refresh_limits=lambda _subject: adapter._refresh_dynamic_limits(),
        dependency_policy=dependency_policy,
        before_stage=before_stage,
        milestone_projector=lambda subject, current_run, capability, stage: (
            document_adapter.milestone_projection(
                subject, current_run, capability, stage
            )
            if capability.id in audit_capabilities.AUDIT_DOCUMENT_GROUP.CAPABILITY_IDS
            else analysis_adapter.milestone_projection(
                subject, current_run, capability, stage
            )
            if capability.id in {
                "data.relationships_inferred",
                "data.joins_ready",
                "analysis.register_ready",
                "analysis.definitions_ready",
                "analysis.executed",
            }
            else adapter.milestone_projection(
                subject, current_run, capability, stage
            )
        ),
        finish_evaluator=adapter._finish_projection,
    )
    adapter.scheduler = scheduler
    scheduler.execution_adapter = adapter
    return scheduler
