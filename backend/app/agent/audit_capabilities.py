"""Audit capability registry and deterministic semantic readiness checks."""

from __future__ import annotations

import json

from .. import doc_tests, findings, methodology, rcm_execution, report
from ..workspaces import Workspace
from .workflow import (
    Capability,
    CapabilityRegistry,
    Readiness,
    UnitSpec,
    canonical_sha1,
    semantic_unit_id,
)
from .workflows import audit as audit_workflow

# The audit dependency graph and outcome sets are authoritative in
# ``workflows.audit``. These re-exports keep existing call sites stable while
# ``build_registry`` below derives every capability's ``depends_on`` from the
# authoritative graph rather than restating the edges.
FULL_AUDIT_OUTCOMES = audit_workflow.FULL_AUDIT_OUTCOMES
TEMPLATE_OUTCOMES = audit_workflow.TEMPLATE_OUTCOMES
outcomes_for_template = audit_workflow.outcomes_for_template


def planning_basis_sha1(workspace: Workspace) -> str:
    table_signatures = {}
    for name in workspace.table_names():
        try:
            table_signatures[name] = workspace._table_signature(name)
        except Exception as error:
            # Broken/missing sources still participate deterministically in
            # invalidation; readiness checks must not crash the scheduler.
            table_signatures[name] = {"unavailable": type(error).__name__, "message": str(error)}
    return canonical_sha1(
        {
            "context": workspace.planning.get("context") or {},
            "tables": table_signatures,
            "documents": [
                {
                    key: item.get(key)
                    for key in ("id", "sha1", "title", "category", "text_state")
                }
                for item in workspace.documents
            ],
            "methodology": [
                {key: item.get(key) for key in ("id", "scope", "version", "sha1")}
                for item in methodology.list_packs(workspace)
            ],
        }
    )


def apm_sha1(workspace: Workspace) -> str:
    return canonical_sha1(
        {
            "markdown": workspace.planning.get("apm_markdown") or "",
            "basis": workspace.planning.get("workflow_basis_sha1"),
        }
    )


def rcm_row_sha1(row: dict) -> str:
    return canonical_sha1(
        {
            key: row.get(key)
            for key in (
                "id", "process", "risk", "risk_rating", "assertion", "control",
                "control_type", "control_owner", "criteria", "criteria_refs",
                "test_procedure", "evidence_refs", "review_status",
            )
        }
    )


def planned_test_sha1(planned: dict) -> str:
    return canonical_sha1(
        {
            key: planned.get(key)
            for key in (
                "id", "title", "objective", "criteria", "method", "steps",
                "expected_evidence", "sampling", "thresholds", "methodology_refs",
            )
        }
    )


def _target_rcm_ids(workspace: Workspace, scope: dict) -> list[str]:
    refs = [str(value) for value in scope.get("target_refs") or []]
    selected = {
        value.split(":", 1)[1]
        for value in refs
        if value.startswith("rcm:") and ":" in value
    }
    return [row["id"] for row in workspace.rcm if not selected or row["id"] in selected]


def _rows(workspace: Workspace, scope: dict) -> list[dict]:
    selected = set(_target_rcm_ids(workspace, scope))
    return [row for row in workspace.rcm if row["id"] in selected]


def _context_ready(workspace: Workspace, _scope: dict) -> Readiness:
    context = workspace.planning.get("context") or {}
    missing = [field for field in ("objective", "scope") if not str(context.get(field) or "").strip()]
    if not missing:
        return Readiness("satisfied")
    if not workspace.tables and not workspace.documents:
        return Readiness("blocked", ("no imported data or documents are available",))
    return Readiness("missing", tuple(f"engagement {field} is missing" for field in missing))


def _apm_ready(workspace: Workspace, _scope: dict) -> Readiness:
    markdown = str(workspace.planning.get("apm_markdown") or "").strip()
    if not markdown:
        return Readiness("missing", ("APM content is empty",))
    if not any(line.lstrip().startswith("#") for line in markdown.splitlines()):
        return Readiness("review_required", ("APM has no structured headings",))
    # Existence and structural usability only. Whether the APM is substantively
    # current with respect to changed planning sources is not assessed by the
    # framework; the auditor decides when to force a regeneration.
    return Readiness("satisfied", details={"artifact_count": 1})


def _rcm_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    if not rows:
        return Readiness("missing", ("no RCM rows exist for the requested scope",), details={"artifact_count": 0})
    invalid = [row["id"] for row in rows if not str(row.get("risk") or "").strip() or not str(row.get("control") or "").strip()]
    if invalid:
        return Readiness("review_required", (f"{len(invalid)} RCM row(s) lack a risk or control",), details={"artifact_count": len(rows)})
    # Structurally usable RCM rows exist; currency relative to the APM is not
    # assessed. Parent hashes remain on the rows for executor CAS/provenance.
    return Readiness("satisfied", details={"artifact_count": len(rows)})


def _planned_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    total = len(rows)
    ready = sum(bool(row.get("planned_tests")) for row in rows)
    invalid = [
        planned.get("id")
        for row in rows
        for planned in row.get("planned_tests") or []
        if not planned.get("steps") or not rcm_execution.required_execution_kinds(str(planned.get("method") or ""))
    ]
    if invalid:
        return Readiness("review_required", (f"{len(invalid)} planned test(s) are not executable",), details={"ready": ready, "total": total})
    # Every RCM row has executable planned tests; currency relative to the RCM
    # row is not assessed. Parent hashes remain for executor CAS/provenance.
    if total and ready == total:
        return Readiness("satisfied", details={"ready": ready, "total": total})
    return Readiness("missing", (f"{total - ready} RCM row(s) need planned tests",), details={"ready": ready, "total": total})


def _definitions_ready(workspace: Workspace, scope: dict) -> Readiness:
    selected = set(_target_rcm_ids(workspace, scope))
    manifest = [item for item in rcm_execution.execution_manifest(workspace) if item["rcm_id"] in selected]
    missing = sum(len(item.get("missing_execution") or []) for item in manifest)
    if not manifest:
        return Readiness("missing", ("no planned tests are available to translate",))
    if missing:
        return Readiness("missing", (f"{missing} required execution definition(s) are missing",), details={"missing": missing, "total": len(manifest)})
    # Required execution definitions all exist; currency relative to their
    # planned test is not assessed. Parent hashes remain for executor CAS.
    return Readiness("satisfied", details={"total": len(manifest)})


def _execution_ready(workspace: Workspace, scope: dict) -> Readiness:
    selected = set(_target_rcm_ids(workspace, scope))
    manifest = [item for item in rcm_execution.execution_manifest(workspace) if item["rcm_id"] in selected]
    pending = []
    review = []
    blocked = []
    for requirement in manifest:
        for artifact in requirement.get("existing_execution") or []:
            # An artifact with a durable result counts as executed. Whether that
            # result predates changed inputs (``result_stale``) is currency, not
            # structural usability, and is not assessed by the framework.
            if not artifact.get("executable"):
                review.append(artifact["id"])
            elif artifact.get("has_durable_result"):
                continue
            elif artifact.get("status") == "blocked":
                blocked.append(artifact["id"])
            elif artifact.get("status") == "review_required" and artifact["kind"] == "doctest":
                review.append(artifact["id"])
            else:
                pending.append(artifact["id"])
    details = {"pending": len(pending), "review_required": len(review), "blocked": len(blocked)}
    if pending:
        return Readiness("missing", (f"{len(pending)} execution artifact(s) have not run",), details=details)
    if review:
        return Readiness("review_required", (f"{len(review)} execution artifact(s) require auditor review",), details=details)
    if blocked:
        return Readiness("review_required", (f"{len(blocked)} execution artifact(s) are blocked on evidence",), details=details)
    return Readiness("satisfied", details=details)


def _rollup_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    missing = [row["id"] for row in rows if not row.get("execution_rollup")]
    if missing:
        return Readiness("missing", (f"{len(missing)} RCM row(s) have not been rolled up",))
    return Readiness("satisfied", details={"artifact_count": len(rows)})


def _eligible_observations(workspace: Workspace) -> list[dict]:
    allowed = {"confirmed_control_exception", "draft_finding_candidate"}
    return [
        item for item in workspace.observations
        if item.get("status") == "disposed" and item.get("disposition") in allowed
    ]


def _findings_ready(workspace: Workspace, scope: dict) -> Readiness:
    open_observations = [
        item for item in workspace.observations if item.get("status") != "disposed"
    ]
    if open_observations and scope.get("permission_mode"):
        return Readiness(
            "review_required",
            (f"{len(open_observations)} observation(s) require auditor disposition",),
            details={"open_observations": len(open_observations)},
        )
    eligible = _eligible_observations(workspace)
    linked = {
        str(item.get("source_observation_id") or ""): item
        for item in workspace.findings
        if item.get("source_observation_id")
    }
    invalid = [
        item["id"]
        for observation in eligible
        if (item := linked.get(observation["id"])) is not None
        and findings.support_issues(workspace, item)
    ]
    if invalid:
        return Readiness(
            "review_required",
            (f"{len(invalid)} existing finding draft(s) fail support validation",),
            details={"eligible": len(eligible), "invalid": len(invalid)},
        )
    covered = set(linked)
    missing = [item["id"] for item in eligible if item["id"] not in covered]
    if missing:
        return Readiness("missing", (f"{len(missing)} eligible observation(s) need finding drafts",), details={"eligible": len(eligible)})
    return Readiness("satisfied", details={"eligible": len(eligible), "drafted": len(eligible)})


def _working_papers_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    missing = []
    for row in rows:
        path = workspace.root / "WorkingPapers" / f"{row['id']}.json"
        if not path.is_file():
            missing.append(row["id"])
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append(row["id"])
    # Existence and structural readability only; whether a paper predates changed
    # source results is currency and is not assessed by the framework.
    return (
        Readiness("missing", (f"{len(missing)} RCM working paper(s) are missing",), details={"missing": len(missing)})
        if missing else Readiness("satisfied", details={"artifact_count": len(rows)})
    )


def _dashboard_ready(workspace: Workspace, _scope: dict) -> Readiness:
    curation = workspace.planning.get("dashboard_curation") or {}
    if not curation.get("completed_at"):
        return Readiness("missing", ("the RCM dashboard has not been curated",))
    # A completed curation exists; whether it predates current roll-ups is
    # currency and is not assessed by the framework.
    return Readiness("satisfied", details={"tile_count": int(curation.get("created_count") or 0)})


def _report_ready(workspace: Workspace, _scope: dict) -> Readiness:
    current = report.hydrate(workspace)
    if not str(current.get("generated_markdown") or current.get("markdown") or "").strip():
        return Readiness("missing", ("the report working draft is empty",))
    if current.get("edited") and current.get("generated_markdown") != current.get("markdown"):
        return Readiness("review_required", ("an auditor-edited report has a generated candidate awaiting reconciliation",))
    # A non-empty, reconciled report working draft exists; whether it predates
    # current planning/results/findings is currency and is not assessed here.
    preliminary = any(
        planned.get("status") in {"not_ready", "ready", "in_progress", "blocked", "review_required"}
        for row in workspace.rcm for planned in row.get("planned_tests") or []
    ) or any(item.get("status") != "disposed" for item in workspace.observations)
    return Readiness("satisfied", details={"preliminary": preliminary})


def _verified(workspace: Workspace, _scope: dict) -> Readiness:
    coverage = rcm_execution.coverage(workspace)
    quality = report.quality_checks(workspace)
    errors = [item for item in quality.get("issues") or [] if item.get("severity") == "error"]
    open_items = bool(
        coverage.get("issue_count")
        or any(item.get("status") != "disposed" for item in workspace.observations)
        or any(
            planned.get("status") not in {"completed_no_exception", "completed_with_exception", "not_applicable"}
            for row in workspace.rcm for planned in row.get("planned_tests") or []
        )
    )
    status = "completed" if not open_items else "completed_with_open_items"
    if status == "completed" and not errors:
        return Readiness("satisfied", details={"completion_status": "completed", "report_quality_ok": True})
    reasons = [f"audit completion status is {status}"]
    if errors:
        reasons.append(f"report quality has {len(errors)} error(s)")
    return Readiness("review_required", tuple(reasons), details={"completion_status": status, "report_quality_ok": not errors})


def _single(kind: str, title: str, *parents: str):
    return lambda _workspace, _scope: [UnitSpec(kind, kind, title, tuple(parents), parents)]


def _planned_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    return [
        UnitSpec(
            semantic_unit_id("planned_test", row["id"]), "planned_test_generation",
            f"Draft planned tests — {row.get('risk') or row['id']}", (f"rcm:{row['id']}",), row,
        )
        for row in _rows(workspace, scope)
        if not row.get("planned_tests") or scope.get("generation_mode") == "force"
    ]


def _definition_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    selected = set(_target_rcm_ids(workspace, scope))
    units = []
    for item in rcm_execution.execution_manifest(workspace):
        if item["rcm_id"] not in selected:
            continue
        missing = set(item.get("missing_execution") or [])
        for kind in item.get("required_execution") or []:
            # Generate only missing definitions, or everything on explicit force.
            # A definition that predates its planned test (currency) is not
            # regenerated automatically; the auditor forces regeneration instead.
            needs = kind in missing or (kind == "datatest" and "validation_datatest" in missing)
            if not needs and scope.get("generation_mode") != "force":
                continue
            worker_kind = "data_test_spec" if kind == "datatest" else "document_test_spec"
            units.append(UnitSpec(
                semantic_unit_id(worker_kind, item["planned_test_id"]), worker_kind,
                f"Create {kind} definition — {item['title']}",
                (f"rcm:{item['rcm_id']}", f"planned_test:{item['planned_test_id']}"), item,
            ))
    return units


def _execution_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    selected = set(_target_rcm_ids(workspace, scope))
    units = []
    for item in rcm_execution.execution_manifest(workspace):
        if item["rcm_id"] not in selected:
            continue
        for artifact in item.get("existing_execution") or []:
            if artifact.get("has_durable_result") and scope.get("generation_mode") != "force":
                continue
            if artifact["kind"] == "datatest":
                units.append(UnitSpec(
                    semantic_unit_id("data_test_execution", artifact["id"]),
                    "data_test_execution", f"Execute datatest — {item['title']}",
                    (f"planned_test:{item['planned_test_id']}", f"datatest:{artifact['id']}"),
                    artifact,
                ))
                continue
            test = doc_tests.load_test(workspace, artifact["id"])
            if test.get("kind") == "qa":
                if doc_tests.evidence_blocked(test):
                    units.append(UnitSpec(
                        semantic_unit_id("document_test_execution", artifact["id"]),
                        "document_test_execution",
                        f"Execute doctest — {item['title']}",
                        (
                            f"planned_test:{item['planned_test_id']}",
                            f"doctest:{artifact['id']}",
                        ),
                        artifact,
                    ))
                    continue
                qa_units = []
                for test_item in test.get("items") or []:
                    answered = set((test_item.get("qa_answers") or {}).keys())
                    for document_id in test_item.get("document_ids") or []:
                        if document_id in answered and scope.get("generation_mode") != "force":
                            continue
                        qa_units.append(UnitSpec(
                            semantic_unit_id(
                                "document_qa_execution", artifact["id"],
                                test_item["id"], document_id,
                            ),
                            "document_qa_execution",
                            f"Answer {test_item.get('label') or test_item['id']} — {document_id}",
                            (
                                f"planned_test:{item['planned_test_id']}",
                                f"doctest:{artifact['id']}",
                                f"docitem:{test_item['id']}",
                                f"document:{document_id}",
                            ),
                            {
                                "test_sha1": test.get("sha1"),
                                "question": test_item.get("question"),
                                "document_sha1": next(
                                    (
                                        document.get("sha1")
                                        for document in workspace.documents
                                        if document.get("id") == document_id
                                    ),
                                    None,
                                ),
                            },
                        ))
                if qa_units:
                    units.extend(qa_units)
                    continue
                units.append(UnitSpec(
                    semantic_unit_id("document_test_review", artifact["id"]),
                    "document_test_review", f"Review document Q&A — {item['title']}",
                    (f"planned_test:{item['planned_test_id']}", f"doctest:{artifact['id']}"),
                    artifact,
                ))
                continue
            already_checked = bool(test.get("items")) and all(
                test_item.get("state") in {"agent_checked", "manual_review", "confirmed", "exception"}
                for test_item in test.get("items") or []
            )
            kind = "document_test_review" if already_checked else "document_test_execution"
            units.append(UnitSpec(
                semantic_unit_id(kind, artifact["id"]), kind,
                f"{'Review' if already_checked else 'Execute'} doctest — {item['title']}",
                (f"planned_test:{item['planned_test_id']}", f"doctest:{artifact['id']}"), artifact,
            ))
    return units


def _finding_units(workspace: Workspace, _scope: dict) -> list[UnitSpec]:
    existing = {str(item.get("source_observation_id") or "") for item in workspace.findings}
    return [
        UnitSpec(
            semantic_unit_id("finding", item["id"]), "finding_draft",
            f"Draft finding — {item.get('summary') or item['id']}",
            (f"observation:{item['id']}", f"rcm:{item['rcm_id']}", f"planned_test:{item['planned_test_id']}"), item,
        )
        for item in _eligible_observations(workspace) if item["id"] not in existing
    ]


def _paper_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    return [
        UnitSpec(semantic_unit_id("working_paper", row["id"]), "working_paper", f"Generate working paper — {row.get('risk') or row['id']}", (f"rcm:{row['id']}",), row)
        for row in _rows(workspace, scope)
    ]


def _depends_on(capability_id: str) -> tuple[str, ...]:
    """Direct dependencies from the authoritative ``workflows.audit`` graph."""

    return audit_workflow.dependencies(capability_id)


def build_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    register = registry.register
    register(Capability("planning.context_ready", "planning_context", "Planning context", "planning_context", _depends_on("planning.context_ready"), _context_ready, _single("planning_context", "Assemble planning context"), invalidate_on=("sources",)))
    register(Capability("planning.apm_ready", "apm", "Audit planning memorandum", "apm", _depends_on("planning.apm_ready"), _apm_ready, _single("apm", "Draft audit planning memorandum", "planning:context"), context="planning.apm", invalidate_on=("planning:context",)))
    register(Capability("planning.rcm_ready", "rcm", "Risk and control matrix", "rcm", _depends_on("planning.rcm_ready"), _rcm_ready, _single("rcm", "Draft risk and control matrix", "planning:apm"), context="planning.rcm", invalidate_on=("planning:apm",)))
    register(Capability("planning.planned_tests_ready", "planned_tests", "RCM planned tests", "planned_test_generation", _depends_on("planning.planned_tests_ready"), _planned_ready, _planned_units, invalidate_on=("rcm",)))
    register(Capability("fieldwork.definitions_ready", "execution_definitions", "Execution definitions", "execution_spec", _depends_on("fieldwork.definitions_ready"), _definitions_ready, _definition_units, invalidate_on=("planned_test",)))
    register(Capability("fieldwork.executed", "execution", "Fieldwork execution", "mixed_execution", _depends_on("fieldwork.executed"), _execution_ready, _execution_units, invalidate_on=("definition", "evidence")))
    register(Capability("results.rolled_up", "rollup", "Results and observations", "rollup", _depends_on("results.rolled_up"), _rollup_ready, _single("rollup", "Roll up RCM results"), invalidate_on=("execution",)))
    register(Capability("findings.drafted", "findings", "Eligible finding drafts", "finding_draft", _depends_on("findings.drafted"), _findings_ready, _finding_units, invalidate_on=("observation",)))
    register(Capability("working_papers.generated", "working_papers", "RCM working papers", "working_paper", _depends_on("working_papers.generated"), _working_papers_ready, _paper_units, invalidate_on=("rollup",)))
    register(Capability("dashboard.curated", "dashboard", "Dashboard curation", "dashboard", _depends_on("dashboard.curated"), _dashboard_ready, _single("dashboard", "Curate RCM dashboard"), invalidate_on=("rollup",)))
    register(Capability("report.working_draft", "report", "Report working draft", "report", _depends_on("report.working_draft"), _report_ready, _single("report", "Assemble report working draft"), invalidate_on=("planning:apm", "rollup", "findings")))
    register(Capability("audit.verified", "verify", "Audit verification", "verify", _depends_on("audit.verified"), _verified, _single("verify", "Verify completion and report quality"), invalidate_on=("outputs",)))
    return registry


REGISTRY = build_registry()


def workflow_state(workspace: Workspace, scope: dict | None = None) -> dict[str, dict]:
    return REGISTRY.workflow_state(workspace, scope)
