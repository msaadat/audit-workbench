"""Deterministic RCM coverage, result roll-up, observations, and completion gates.

A test is one durable record with one source, so this module folds test results
straight into the RCM row that links them. There is no intermediate planned-test
layer and nothing to reconcile between a plan and its execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import cycle_vouching, data_tests, doc_tests
from .evidence import normalize_anchor
from .workspace_transactions import canonical_sha1, material_projection
from .workspaces import Workspace
from .text import counted, relevance_tokens

# A document test of this kind asks whether the documentation *describes* a
# control. That is design inquiry: it can establish that a policy exists and
# never that the population complies with it.
_DESIGN_INQUIRY_VARIANTS = frozenset({"qa"})
# Distinct words of a requirement that an executed test must name before the
# requirement counts as covered. One shared word is coincidence, two is a
# subject.
MIN_COVERAGE_TOKEN_MATCH = 2
# Carried by the tokenizer because they are long enough to look like content.
# Harmless when ranking, where noise cancels; not harmless against a threshold,
# where "are" alone can make a requirement look covered.
_EMPTY_WORDS = frozenset(
    {"and", "are", "for", "from", "not", "that", "the", "their", "them", "this", "with"}
)


_DURABLE_DOC_TEST_STATUSES = frozenset({
    "completed",
    "completed_no_exception",
    "completed_with_exception",
    "not_applicable",
})
# The auditor's control-level disposition is the authoritative conclusion for
# completion gates. The explanatory free-text note remains useful workpaper
# context, but it is not required to close a test.
CONCLUDED_CONTROL_CONCLUSIONS = frozenset({
    "effective",
    "partially_effective",
    "ineffective",
    "not_applicable",
})


@dataclass(frozen=True)
class DocumentTestIndex:
    """One request-local, fully hydrated view of the document-test worklist."""

    tests: tuple[dict, ...]
    by_rcm_id: dict[str, tuple[dict, ...]]
    summaries: tuple[dict, ...]


def document_test_index(workspace: Workspace) -> DocumentTestIndex:
    """Load document tests once and group them by their linked RCM row.

    The index deliberately lives only for the current calculation. Durable
    document-test mutations write their own files, so rebuilding it for each
    request keeps status surfaces current without cache invalidation state.
    """
    summaries = tuple(doc_tests.list_tests(workspace))
    tests = tuple(doc_tests.load_test(workspace, item["id"]) for item in summaries)
    grouped: dict[str, list[dict]] = {}
    for test in tests:
        rcm_id = str(test.get("rcm_id") or "")
        if rcm_id:
            grouped.setdefault(rcm_id, []).append(test)
    return DocumentTestIndex(
        tests=tests,
        by_rcm_id={rcm_id: tuple(items) for rcm_id, items in grouped.items()},
        summaries=summaries,
    )


def _tests(
    workspace: Workspace,
    rcm_id: str,
    document_tests: DocumentTestIndex | None = None,
) -> list[dict]:
    """Every durable test linked to one RCM row, in a stable order."""
    tests = [
        {"kind": "datatest", "id": item["id"], "item": item}
        for item in workspace.data_tests
        if item.get("rcm_id") == rcm_id
    ]
    tests.extend(
        {"kind": "doctest", "id": item["id"], "item": item}
        for item in (
            document_tests or document_test_index(workspace)
        ).by_rcm_id.get(rcm_id, ())
    )
    return sorted(tests, key=lambda test: (test["kind"], test["id"]))


def _specified(test: dict) -> bool:
    """Whether a test has an executable specification yet."""
    item = test["item"]
    if item.get("status") == "draft":
        return False
    return bool(item.get("engine")) if test["kind"] == "datatest" else bool(item.get("kind"))


def _executable(test: dict) -> bool:
    if test["kind"] != "doctest":
        return _specified(test)
    item = test["item"]
    # Preserve auditor-completed historical work even when its older definition
    # predates the stronger runner preflight metadata.
    return (
        item.get("status") in _DURABLE_DOC_TEST_STATUSES
        or doc_tests.evidence_blocked(item)
        or not doc_tests.execution_issues(item)
    )


def test_manifest(workspace: Workspace) -> list[dict]:
    """Build a bounded, model-safe view of every RCM row's linked tests.

    The manifest carries plan specifications and artifact metadata only; it never
    includes table rows or document text. It records whether a test has a durable
    result rather than merely whether a definition exists.
    """
    manifest = []
    document_tests = document_test_index(workspace)
    for row in workspace.rcm:
        for test in _tests(workspace, row["id"], document_tests):
            item = test["item"]
            if test["kind"] == "datatest":
                has_result = bool(item.get("last_run"))
                execution_refs = data_tests._execution_table_refs(workspace, item)
                current_source_sha1 = data_tests._sha1(
                    {
                        "engine": item.get("engine"),
                        "table_refs": execution_refs,
                        "spec": item.get("spec") or {},
                    }
                )
                current_fingerprints = data_tests._dataset_fingerprints(
                    workspace, execution_refs
                )
                result_stale = bool(
                    item.get("last_run")
                    and (
                        item["last_run"].get("source_sha1") != current_source_sha1
                        or item["last_run"].get("dataset_fingerprints") != current_fingerprints
                    )
                )
            else:
                current_items = bool(item.get("items")) and all(
                    doc_tests.item_execution_current(item, test_item)
                    for test_item in item.get("items") or []
                )
                has_result = (
                    current_items
                    if doc_tests.is_cycle_test(item)
                    else item.get("status") in _DURABLE_DOC_TEST_STATUSES
                    or current_items
                )
                result_stale = False
            manifest.append({
                "rcm_id": row["id"],
                "rcm_risk": str(row.get("risk") or ""),
                "risk_rating": str(row.get("risk_rating") or ""),
                "kind": test["kind"],
                "test_id": test["id"],
                "title": str(item.get("title") or ""),
                "objective": str(item.get("objective") or ""),
                "criteria": str(item.get("criteria") or ""),
                "steps": list(item.get("steps") or []),
                "test_kind": str(item.get("kind") or ""),
                "requirement_refs": list(item.get("requirement_refs") or []),
                "procedure_key": str(item.get("procedure_key") or ""),
                "definition": dict(item.get("definition") or {}),
                "coverage": dict(item.get("coverage") or {}),
                "assurance_scope": doc_tests.assurance_scope(item) or "",
                "status": str(item.get("status") or ""),
                "created_by": str(item.get("created_by") or ""),
                "specified": _specified(test),
                "has_durable_result": has_result,
                "executable": _executable(test),
                "workflow_parent_sha1": item.get("workflow_parent_sha1"),
                "result_stale": result_stale,
                **(
                    {"execution_issues": doc_tests.execution_issues(item)}
                    if test["kind"] == "doctest" else {}
                ),
                **(
                    {"engine": str(item.get("engine") or "")}
                    if test["kind"] == "datatest" else {}
                ),
            })
    return manifest


def coverage(
    workspace: Workspace,
    *,
    document_tests: DocumentTestIndex | None = None,
) -> dict:
    test_index = document_tests or document_test_index(workspace)
    rows_without_tests = []
    unspecified_tests = []
    invalid_test_parents = []
    high_risks_without_executable_work = []
    completed_without_durable_result = []
    inconsistent_conclusions = []
    known_rows = {row["id"] for row in workspace.rcm}

    for row in workspace.rcm:
        tests = _tests(workspace, row["id"], test_index)
        if not tests:
            rows_without_tests.append(row["id"])
        usable = False
        for test in tests:
            item = test["item"]
            if not _specified(test):
                unspecified_tests.append({"rcm_id": row["id"], "test_id": test["id"]})
            if item.get("status") not in {"blocked", "draft"}:
                usable = True
            if str(item.get("status") or "").startswith("completed"):
                missing_result = (
                    not item.get("last_run")
                    if test["kind"] == "datatest"
                    else item.get("status") != "completed"
                    and not str(item.get("status") or "").startswith("completed")
                )
                if missing_result:
                    completed_without_durable_result.append(
                        {"rcm_id": row["id"], "test_id": test["id"]}
                    )
            if (
                item.get("control_conclusion") == "effective"
                and (
                    int(item.get("open_exception_count") or 0) > 0
                    or str(item.get("scope_limitations") or "").strip()
                )
            ):
                inconsistent_conclusions.append(
                    {
                        "rcm_id": row["id"],
                        "test_id": test["id"],
                        "reason": "Effective conclusion conflicts with open exceptions or limitations.",
                    }
                )
            if (
                doc_tests.assurance_scope(item) == "targeted_evidence_only"
                and item.get("control_conclusion") != "no_conclusion"
            ):
                inconsistent_conclusions.append(
                    {
                        "rcm_id": row["id"],
                        "test_id": test["id"],
                        "reason": (
                            "Targeted evidence cannot support a population control conclusion."
                        ),
                    }
                )
        if row.get("risk_rating") in {"high", "critical"} and tests and not usable:
            high_risks_without_executable_work.append(row["id"])

    for item in [*workspace.data_tests, *test_index.tests]:
        rcm_id = item.get("rcm_id")
        # An unlinked test is exploration or standalone document work, not a
        # coverage defect. Only a link that does not resolve is.
        if rcm_id and rcm_id not in known_rows:
            invalid_test_parents.append(
                {"id": item.get("id"), "reason": "Linked RCM row does not exist."}
            )

    issues = (
        len(rows_without_tests)
        + len(unspecified_tests)
        + len(invalid_test_parents)
        + len(high_risks_without_executable_work)
        + len(completed_without_durable_result)
        + len(inconsistent_conclusions)
    )
    return {
        "ok": issues == 0,
        "issue_count": issues,
        "rows_without_tests": rows_without_tests,
        "unspecified_tests": unspecified_tests,
        "invalid_test_parents": invalid_test_parents,
        "high_risks_without_executable_work": high_risks_without_executable_work,
        "completed_without_durable_result": completed_without_durable_result,
        "inconsistent_conclusions": inconsistent_conclusions,
    }


def _observation(
    workspace: Workspace,
    *,
    rcm_id: str,
    test_id: str,
    execution_ref: str,
    exception_count: int,
    classification: str,
    summary: str,
    observation_key: str | None = None,
    details: dict | None = None,
    evidence_refs: list[dict] | None = None,
) -> dict:
    key = observation_key or execution_ref
    existing = next(
        (
            item
            for item in workspace.observations
            if str(item.get("observation_key") or item.get("execution_ref") or "")
            == key
        ),
        None,
    )
    # Data Tests retain only their current durable result. Older workspaces may
    # still point an observation at the pre-current result ID, which otherwise
    # makes a read-only rollup project the same test result as a second
    # observation. Reuse that test's observation and refresh its reference.
    if execution_ref.startswith(f"datatest:{test_id}:"):
        matches = [
            item
            for item in workspace.observations
            if item.get("rcm_id") == rcm_id
            and item.get("test_id") == test_id
            and str(item.get("execution_ref") or "").startswith(
                f"datatest:{test_id}:"
            )
        ]
        if matches:
            existing = existing or matches[0]
            if len(matches) > 1:
                duplicate_ids = {id(item) for item in matches if item is not existing}
                workspace.observations[:] = [
                    item for item in workspace.observations if id(item) not in duplicate_ids
                ]
    if existing is None:
        existing = {
            "id": f"OBS-{uuid.uuid4().hex[:10].upper()}",
            "rcm_id": rcm_id,
            "test_id": test_id,
            "execution_ref": execution_ref,
            "observation_key": key,
            "exception_count": exception_count,
            "summary": summary,
            "classification": classification,
            "outcome": "exception" if exception_count else "needs_manual_check",
            "created": workspace._updated_now(),
            "updated": workspace._updated_now(),
        }
        workspace.observations.append(existing)
    else:
        existing.update(
            execution_ref=execution_ref,
            exception_count=exception_count,
            summary=summary,
            classification=classification,
            outcome="exception" if exception_count else "needs_manual_check",
            updated=workspace._updated_now(),
        )
    existing["observation_key"] = key
    if details:
        existing.update(details)
    if evidence_refs is not None:
        existing["evidence_refs"] = list(evidence_refs)
    return existing


def _rollup_datatest(workspace: Workspace, row: dict, item: dict) -> tuple[str, int, int, int]:
    """Fold one Data Test's latest run into its own record."""
    last_run = item.get("last_run")
    exceptions = int((last_run or {}).get("exception_count") or 0)
    open_exceptions = 0
    executed = 0
    if last_run:
        executed = 1
        run = data_tests.load_result(workspace, item["id"], last_run["id"])
        if exceptions or not run.get("semantic_valid"):
            suggestion = (
                "invalid_test_or_result"
                if not run.get("semantic_valid")
                else "screening_follow_up"
                if any("Screening result" in issue for issue in run.get("semantic_issues") or [])
                else "draft_finding_candidate"
            )
            observation = _observation(
                workspace,
                rcm_id=row["id"],
                test_id=item["id"],
                execution_ref=f"datatest:{item['id']}:{last_run['id']}",
                exception_count=exceptions,
                classification=suggestion,
                summary=run.get("verdict_text") or item["title"],
            )
            if observation.get("outcome") == "exception":
                open_exceptions = exceptions
    status = str(item.get("status") or "draft")
    return status, exceptions, open_exceptions, executed


def _cycle_observation_evidence(item: dict) -> list[dict]:
    """Citation-complete evidence for one tested item, without copying results."""

    anchors: dict[tuple[str, str, int | None, str | None], dict] = {}
    results = item.get("result_by_assertion") or {}
    exceptional = {
        key: result
        for key, result in results.items()
        if result.get("verdict") != "match"
    } or results
    for assertion_key, result in exceptional.items():
        for raw in result.get("evidence_refs") or []:
            anchor = normalize_anchor(
                {
                    **raw,
                    "item_id": item.get("id"),
                    "field": assertion_key,
                },
                require_hash=True,
            )
            identity = (
                str(anchor["source_kind"]),
                str(anchor["source_id"]),
                anchor.get("page"),
                anchor.get("field"),
            )
            anchors[identity] = anchor
    return list(anchors.values())


def _sync_cycle_observations(
    workspace: Workspace,
    row: dict,
    test: dict,
    rollup: dict,
) -> int:
    """Maintain one current observation per auditor-dispositioned cycle item."""

    definition_sha1 = cycle_vouching.cycle_definition_sha1(test)
    assertion_labels = {
        str(assertion["key"]): str(assertion.get("label") or assertion["key"])
        for assertion in (test.get("definition") or {}).get("assertions") or []
    }
    active_keys: set[str] = set()
    for cycle_item in test.get("items") or []:
        if not (
            doc_tests.item_execution_current(test, cycle_item)
            and doc_tests.item_disposition_current(test, cycle_item)
            and (cycle_item.get("disposition") or {}).get("state") == "exception"
        ):
            continue
        item_id = str(cycle_item["id"])
        observation_key = f"doctest:{test['id']}:item:{item_id}"
        active_keys.add(observation_key)
        results = cycle_item.get("result_by_assertion") or {}
        diagnostic_keys = [
            key for key, result in results.items() if result.get("verdict") != "match"
        ]
        labels = [assertion_labels.get(str(key), str(key)) for key in diagnostic_keys]
        summary = (
            f"{rollup['assurance_label']} identified an auditor-dispositioned "
            f"exception in one tested item. Diagnostic assertions: "
            + (", ".join(labels) if labels else "none; see the item-specific evidence")
            + ". This observation does not project beyond that tested item."
        )
        _observation(
            workspace,
            rcm_id=row["id"],
            test_id=test["id"],
            execution_ref=f"doctest:{test['id']}",
            observation_key=observation_key,
            exception_count=1,
            classification="draft_finding_candidate",
            summary=summary,
            evidence_refs=_cycle_observation_evidence(cycle_item),
            details={
                "cycle_item_id": item_id,
                "assurance_scope": rollup["assurance_scope"],
                "definition_sha1": definition_sha1,
                "evaluation_result_sha1": (cycle_item.get("evaluation") or {}).get(
                    "result_sha1"
                ),
                "evaluation_state": (cycle_item.get("evaluation") or {}).get("state"),
                "disposition_state": "exception",
                "assertion_keys": list(diagnostic_keys),
                "assertion_mismatch_count": sum(
                    result.get("verdict") == "mismatch" for result in results.values()
                ),
            },
        )
    for observation in workspace.observations:
        if (
            observation.get("test_id") == test.get("id")
            and observation.get("cycle_item_id")
            and str(observation.get("observation_key") or "") not in active_keys
        ):
            observation.update(
                exception_count=0,
                outcome="needs_manual_check",
                classification="stale_cycle_disposition",
                summary=(
                    "The prior Cycle vouch exception is no longer current; rerun or "
                    "re-disposition the item before using it downstream."
                ),
                updated=workspace._updated_now(),
            )
    return len(active_keys)


def _rollup_doctest(workspace: Workspace, row: dict, item: dict) -> tuple[str, int, int, int, list]:
    rollup = doc_tests.result_rollup(item)
    status = str(item.get("status") or "draft")
    # A deterministic mismatch is an evaluation result, not an auditor-owned
    # exception disposition.  Cycle tests keep those concepts separate all the
    # way through the rollup; downstream Phase 7 work can enrich the assurance
    # presentation without reviving the old double count.
    cycle = doc_tests.is_cycle_test(item)
    exceptions = int(
        rollup["exception_items"]
        if cycle
        else rollup["exceptions"] + rollup["mismatched"]
    )
    open_exceptions = 0
    current_items = bool(item.get("items")) and all(
        doc_tests.item_execution_current(item, test_item)
        for test_item in item.get("items") or []
    )
    executed = int(
        current_items
        if cycle
        else status in _DURABLE_DOC_TEST_STATUSES or current_items
    )
    if cycle and current_items and not all(
        doc_tests.item_disposition_current(item, test_item)
        for test_item in item.get("items") or []
    ):
        status = "review_required"
    evidence_refs = [
        anchor
        for test_item in item.get("items") or []
        for anchor in test_item.get("evidence_refs") or []
    ]
    if cycle:
        open_exceptions = _sync_cycle_observations(
            workspace, row, item, rollup
        )
    elif exceptions:
        observation = _observation(
            workspace,
            rcm_id=row["id"],
            test_id=item["id"],
            execution_ref=f"doctest:{item['id']}",
            exception_count=exceptions,
            classification="draft_finding_candidate",
            summary=f"{counted(exceptions, 'document-test exception or mismatch result')}.",
        )
        if observation.get("outcome") == "exception":
            open_exceptions = exceptions
    if status == "completed":
        status = (
            "completed_with_exception" if exceptions else "completed_no_exception"
        )
    return status, exceptions, open_exceptions, executed, evidence_refs


def _evidence_ceiling(row: dict, test_rollups: list[dict]) -> str:
    """Why this row's evidence cannot support ``effective``, or an empty string.

    One invariant: a conclusion may never be stronger than the evidence class
    behind it. Two ways a row breaks it, both observed in the same engagement —
    a critical vendor-integrity row concluded from documentation inquiry alone,
    and a vendor-master row that reached "effective" on two tests checking that
    IDs were unique and status values spelled correctly, while the requirement
    naming bank-account changes went untested underneath.

    The remedy is a downgrade with a stated scope limitation, never a hard
    failure: the tests that did run are real evidence for what they covered.
    """
    contributing = [item for item in test_rollups if item["conclusion_eligible"]]
    if not contributing:
        return ""
    if all(item["variant"] in _DESIGN_INQUIRY_VARIANTS for item in contributing):
        return (
            "Every test contributing to this conclusion asks whether the "
            "documentation describes the control. Design inquiry cannot "
            "establish that the population complies with it."
        )
    executed = set().union(
        *(set(item["subject_tokens"]) for item in contributing), set()
    ) - _EMPTY_WORDS
    # A transaction_cycle attribute declares its evidence through the registry
    # contract, and the cycle evaluator checks the comparisons that contract
    # names. Its coverage is established structurally, so matching its wording
    # against a test title decides nothing and only caps rows that were in fact
    # vouched.
    vouched = any(item["variant"] == "cycle_vouch" for item in contributing)
    attributes = [
        attribute
        for attribute in row.get("control_attributes") or []
        if isinstance(attribute, dict)
        and not (vouched and attribute.get("evidence_kind") == "transaction_cycle")
    ]
    # What this detects is *selective* testing: a row that decomposed its
    # control into several requirements and then evidenced only some of them.
    # A single-attribute row cannot be selectively tested, and comparing one
    # requirement's wording against one test's title is not a measurement — it
    # caps rows whose evidence is real and whose phrasing merely differs.
    if len(attributes) < 2:
        return ""
    wording = [
        relevance_tokens(attribute.get("requirement")) - _EMPTY_WORDS
        for attribute in attributes
    ]
    # What makes each requirement different from its siblings. A row about
    # vendor master data has "vendor" in every attribute, so matching on it
    # says only that the tests were about vendors — the question is whether
    # anything executed named *bank account changes*, and the shared words are
    # exactly what hides that.
    shared = set.intersection(*wording)
    uncovered = [
        str(attribute.get("key") or attribute.get("requirement") or "")
        for attribute, tokens in zip(attributes, wording)
        if len((tokens - shared) & executed) < MIN_COVERAGE_TOKEN_MATCH
    ]
    if uncovered:
        return (
            "No executed test names the subject of "
            f"{counted(len(uncovered), 'control requirement')}: "
            f"{', '.join(sorted(uncovered))}."
        )
    return ""


def _rollup_test(workspace: Workspace, row: dict, test: dict) -> dict:
    """Recompute one test's outcome from its own durable results."""
    item = test["item"]
    evidence_refs = list(item.get("evidence_refs") or [])
    if test["kind"] == "datatest":
        status, exceptions, open_exceptions, executed = _rollup_datatest(
            workspace, row, item
        )
        detailed_rollup: dict = {}
    else:
        detailed_rollup = doc_tests.result_rollup(item)
        status, exceptions, open_exceptions, executed, anchors = _rollup_doctest(
            workspace, row, item
        )
        evidence_refs.extend(anchors)
    if not _specified(test):
        status = "draft"

    assurance_scope = detailed_rollup.get("assurance_scope")
    conclusion_eligible = bool(
        detailed_rollup.get("conclusion_eligible", True)
    )
    control_conclusion = (
        str(detailed_rollup.get("control_conclusion") or "no_conclusion")
        if test["kind"] == "doctest"
        else str(item.get("control_conclusion") or "no_conclusion")
    )
    if assurance_scope == "targeted_evidence_only":
        item["control_conclusion"] = "no_conclusion"
        item["control_conclusion_source"] = "none"
    if test["kind"] == "doctest" and doc_tests.is_cycle_test(item):
        result_summary = (
            f"{detailed_rollup['tested_items']} of {counted(detailed_rollup['items'], 'item')} tested; "
            f"{detailed_rollup['failed_items']} failed, "
            f"{detailed_rollup['incomplete_items']} incomplete, "
            f"{detailed_rollup['needs_review_items']} need review; "
            f"{detailed_rollup['assertion_mismatches']} assertion mismatch(es); "
            f"{counted(open_exceptions, 'open item exception')}."
        )
    else:
        result_summary = (
            f"{counted(executed, 'run')}; {counted(exceptions, 'exception result')}, "
            f"{open_exceptions} open."
        )
    item.update(
        status=status,
        exception_count=exceptions,
        open_exception_count=open_exceptions,
        evidence_refs=list(
            {anchor.get("id") or str(anchor): anchor for anchor in evidence_refs}.values()
        ),
        result_summary=result_summary,
        updated=workspace._updated_now(),
    )
    return {
        "test_id": test["id"],
        "kind": test["kind"],
        # The evidence class, not the storage class: `kind` says which store a
        # test lives in, `variant` says what sort of evidence it produced.
        "variant": (
            str(item.get("kind") or "") if test["kind"] == "doctest" else "data"
        ),
        "subject_tokens": sorted(
            relevance_tokens(item.get("title")) | relevance_tokens(item.get("objective"))
        ),
        "title": str(item.get("title") or ""),
        "executed_count": executed,
        "exception_count": exceptions,
        "open_exception_count": open_exceptions,
        "evidence_count": len(item["evidence_refs"]),
        "status": status,
        "result_summary": item["result_summary"],
        "conclusion": item.get("conclusion") or "",
        "control_conclusion": control_conclusion,
        "conclusion_eligible": conclusion_eligible,
        "assurance_scope": assurance_scope,
        "assurance_label": detailed_rollup.get("assurance_label"),
        "selection_basis": str(
            (detailed_rollup.get("coverage") or {}).get("selection_basis") or ""
        ),
        "coverage": dict(detailed_rollup.get("coverage") or {}),
        "tested_items": int(detailed_rollup.get("tested_items") or 0),
        "failed_items": int(detailed_rollup.get("failed_items") or 0),
        "incomplete_items": int(detailed_rollup.get("incomplete_items") or 0),
        "needs_review_items": int(detailed_rollup.get("needs_review_items") or 0),
        "confirmed_items": int(detailed_rollup.get("confirmed_items") or detailed_rollup.get("confirmed") or 0),
        "assertion_mismatches": int(detailed_rollup.get("assertion_mismatches") or 0),
        "scope_limitations": item.get("scope_limitations") or "",
        "finding_refs": item.get("finding_refs") or [],
    }


def rollup(
    workspace: Workspace,
    *,
    persist: bool = True,
    rcm_ids: set[str] | None = None,
    document_tests: DocumentTestIndex | None = None,
) -> dict:
    """Recompute RCM outcomes, persisting only material changes.

    Status surfaces call this with ``persist=False`` so GET requests remain
    read-only.  Explicit roll-up commands still persist, but repeated calls do
    not advance the workspace revision when only volatile timestamps changed.
    """
    test_index = document_tests or document_test_index(workspace)
    before_sha1 = canonical_sha1(
        material_projection(
            {"rcm": workspace.rcm, "observations": workspace.observations}
        )
    )
    documents_to_write: dict[str, dict] = {}
    rows = []
    selected_rcm_ids = None if rcm_ids is None else set(rcm_ids)
    for row in workspace.rcm:
        if selected_rcm_ids is not None and row["id"] not in selected_rcm_ids:
            continue
        tests = _tests(workspace, row["id"], test_index)
        test_rollups = [_rollup_test(workspace, row, test) for test in tests]
        for test in tests:
            if test["kind"] == "doctest":
                documents_to_write[test["id"]] = test["item"]
        conclusions = [
            item["control_conclusion"]
            for item in test_rollups
            if item["conclusion_eligible"]
        ]
        if "ineffective" in conclusions:
            control_conclusion = "ineffective"
        elif "partially_effective" in conclusions:
            control_conclusion = "partially_effective"
        elif conclusions and all(value == "effective" for value in conclusions):
            control_conclusion = "effective"
        elif conclusions and all(value == "not_applicable" for value in conclusions):
            control_conclusion = "not_applicable"
        else:
            control_conclusion = "no_conclusion"
        evidence_ceiling = ""
        if control_conclusion == "effective":
            evidence_ceiling = _evidence_ceiling(row, test_rollups)
            if evidence_ceiling:
                control_conclusion = "partially_effective"
        # The row-level conclusion is this tally: how much of the linked work
        # completed, how much of it passed, and what it produced.
        row_rollup = {
            "tests": len(test_rollups),
            "completed": sum(item["status"].startswith("completed") for item in test_rollups),
            "passed": sum(item["status"] == "completed_no_exception" for item in test_rollups),
            "failed": sum(item["status"] == "completed_with_exception" for item in test_rollups),
            "blocked": sum(item["status"] == "blocked" for item in test_rollups),
            "review_required": sum(item["status"] == "review_required" for item in test_rollups),
            "draft": sum(item["status"] == "draft" for item in test_rollups),
            "exceptions": sum(item["exception_count"] for item in test_rollups),
            "open_exceptions": sum(item["open_exception_count"] for item in test_rollups),
            "tested_items": sum(item["tested_items"] for item in test_rollups),
            "failed_items": sum(item["failed_items"] for item in test_rollups),
            "incomplete_items": sum(item["incomplete_items"] for item in test_rollups),
            "needs_review_items": sum(item["needs_review_items"] for item in test_rollups),
            "confirmed_items": sum(item["confirmed_items"] for item in test_rollups),
            "assertion_mismatches": sum(
                item["assertion_mismatches"] for item in test_rollups
            ),
            "conclusion_eligible_tests": sum(
                item["conclusion_eligible"] for item in test_rollups
            ),
            "supplemental_tests": sum(
                item.get("assurance_scope") == "targeted_evidence_only"
                for item in test_rollups
            ),
            "assurance_scopes": sorted(
                {
                    str(item["assurance_scope"])
                    for item in test_rollups
                    if item.get("assurance_scope")
                }
            ),
            "control_conclusion": control_conclusion,
            "evidence_ceiling": evidence_ceiling,
            "findings": len(row.get("finding_refs") or []),
            "review_status": row.get("review_status") or "draft",
            "test_rollups": test_rollups,
        }
        row["execution_rollup"] = row_rollup
        row["updated"] = workspace._updated_now()
        rows.append({"rcm_id": row["id"], **row_rollup})
    after_sha1 = canonical_sha1(
        material_projection(
            {"rcm": workspace.rcm, "observations": workspace.observations}
        )
    )
    if persist and after_sha1 != before_sha1:
        # Document Tests are separate files, so their recomputed outcome is
        # written back explicitly rather than riding the workspace save. The
        # single coordinated revision bump is the workspace save below.
        for test in documents_to_write.values():
            doc_tests.write_test(workspace, test)
        workspace.save()
    return {"rows": rows, "coverage": coverage(workspace, document_tests=test_index)}


def completion(
    workspace: Workspace,
    *,
    document_tests: DocumentTestIndex | None = None,
) -> dict:
    # Completion is used by dashboard/report GET paths. It must derive current
    # outcomes without turning a read into an optimistic-concurrency write.
    document_tests = document_tests or document_test_index(workspace)
    rolled = rollup(workspace, persist=False, document_tests=document_tests)
    cov = rolled["coverage"]
    linked = [
        (row, test)
        for row in workspace.rcm
        for test in _tests(workspace, row["id"], document_tests)
    ]
    incomplete_outcomes = [
        {"rcm_id": row["id"], "test_id": test["id"], "status": test["item"].get("status")}
        for row, test in linked
        if test["item"].get("status")
        not in {
            "blocked", "review_required", "completed", "completed_no_exception",
            "completed_with_exception", "not_applicable",
        }
    ]
    blank_conclusions = [
        {"rcm_id": row["id"], "test_id": test["id"]}
        for row, test in linked
        if str(test["item"].get("status") or "").startswith("completed")
        and (
            test["kind"] != "doctest"
            or doc_tests.conclusion_eligible(test["item"])
        )
        and test["item"].get("control_conclusion") not in CONCLUDED_CONTROL_CONCLUSIONS
    ]
    blocked_without_plan = [
        {
            "rcm_id": row["id"], "test_id": test["id"],
            "missing": [
                label
                for label, present in (
                    ("scope limitation", bool(str(test["item"].get("scope_limitations") or "").strip())),
                    ("next action", bool(str(test["item"].get("next_action") or "").strip())),
                    ("evidence request", any(
                        request.get("document_test_id") == test["id"]
                        and request.get("status") in {"open", "requested"}
                        for request in workspace.evidence_requests
                    )),
                )
                if not present
            ],
        }
        for row, test in linked
        if test["item"].get("status") == "blocked"
    ]
    blocked_without_plan = [item for item in blocked_without_plan if item["missing"]]
    rcm_without_conclusion = [
        row["id"] for row in workspace.rcm
        if (row.get("execution_rollup") or {}).get("control_conclusion") == "no_conclusion"
        and not all(
            str(test["item"].get("scope_limitations") or "").strip()
            for test in _tests(workspace, row["id"], document_tests)
        )
    ]
    assurance_gaps = [
        {
            "rcm_id": row["id"],
            "reason": (
                "Targeted evidence is supplemental and cannot support a population "
                "control conclusion."
            ),
        }
        for row in workspace.rcm
        if (row.get("execution_rollup") or {}).get("tests")
        and not (row.get("execution_rollup") or {}).get("conclusion_eligible_tests")
        and (row.get("execution_rollup") or {}).get("supplemental_tests")
    ]
    # A conclusion that was capped rather than earned. Reported separately from
    # the rows that reached no conclusion at all, because "we tested less than
    # this claims" and "we could not look" are different disclosures.
    evidence_ceilings = [
        {
            "rcm_id": row["id"],
            "reason": str((row.get("execution_rollup") or {}).get("evidence_ceiling")),
        }
        for row in workspace.rcm
        if (row.get("execution_rollup") or {}).get("evidence_ceiling")
    ]
    pending_cycle_dispositions = [
        {
            "rcm_id": row["id"],
            "test_id": test["id"],
            "item_id": str(item.get("id") or "") or None,
        }
        for row, test in linked
        if test["kind"] == "doctest"
        and doc_tests.is_cycle_test(test["item"])
        for item in (test["item"].get("items") or [{}])
        if not item
        or not doc_tests.item_disposition_current(test["item"], item)
    ]
    technical = bool(cov["invalid_test_parents"] or cov["completed_without_durable_result"])
    open_items = bool(
        cov["issue_count"]
        or incomplete_outcomes
        or blank_conclusions
        or blocked_without_plan
        or rcm_without_conclusion
        or assurance_gaps
        or pending_cycle_dispositions
        or any(
            test["item"].get("status") in {"blocked", "review_required"}
            for _row, test in linked
        )
    )
    status = "completed_with_issues" if technical else "completed_with_open_items" if open_items else "completed"
    return {
        "status": status,
        "coverage": cov,
        "incomplete_outcomes": incomplete_outcomes,
        "blank_conclusions": blank_conclusions,
        "blocked_without_plan": blocked_without_plan,
        "rcm_without_conclusion": rcm_without_conclusion,
        "assurance_gaps": assurance_gaps,
        "evidence_ceilings": evidence_ceilings,
        "pending_cycle_dispositions": pending_cycle_dispositions,
    }
