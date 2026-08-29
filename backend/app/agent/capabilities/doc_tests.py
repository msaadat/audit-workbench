"""Standalone document-test capability group.

Owns every outcome of the authoritative graph in
:mod:`agent.workflows.doc_tests`: ``doc_tests.definitions_ready``,
``doc_tests.executed``, and ``doc_tests.dispositioned``.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry key for its
declared context. The dependency edges come from the authoritative graph; this
module never restates them.

The unit expansion for ``doc_tests.executed`` is deliberately the same
per-Document-Test expansion the audit graph's ``fieldwork.executed`` uses, and it
lives here once: :func:`document_test_units` is imported by
:mod:`agent.capabilities.fieldwork` rather than reimplemented there, so a Q&A
test cannot fan out one way for a standalone run and another way inside an audit.

Everything in this module is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass

from ... import doc_tests as doc_test_service
from ...text import counted, verb
from ... import cycle_vouching
from ...workspaces import Workspace
from ..workflow import Capability, Readiness, UnitSpec, semantic_unit_id
from ..workflows import doc_tests as doc_tests_workflow

CAPABILITY_IDS: tuple[str, ...] = (
    "doc_tests.definitions_ready",
    "doc_tests.executed",
    "doc_tests.dispositioned",
)

# The bounded fallback when a request names no Document Test. Execution is up to
# one model turn per Q&A item/document pair, so the cap is what keeps an unscoped
# request from turning a whole worklist library into an unbounded provider bill.
MAX_SCOPE_TESTS = 12

# Item states that represent a completed outcome. ``manual_review`` remains
# visible but does not require a workflow gate.
DISPOSED_STATES = frozenset({"confirmed", "exception", "manual_review"})
# Item states that mean the agent has already done what it can for the item.
CHECKED_STATES = frozenset({"agent_checked", "manual_review"}) | DISPOSED_STATES


@dataclass(frozen=True)
class DocTestScope:
    """The resolved Document Test scope shared by every capability in the group."""

    test_ids: tuple[str, ...]
    requested: tuple[str, ...]
    unknown: tuple[str, ...]
    ambiguity: str | None = None

    @property
    def explicit(self) -> bool:
        return bool(self.requested)


def _requested_ids(scope: dict) -> tuple[str, ...]:
    """Explicit Document Test targets from the command or the calling endpoint."""

    names: list[str] = []
    for value in scope.get("test_ids") or []:
        text = str(value or "").strip()
        if text:
            names.append(text)
    for value in scope.get("target_refs") or []:
        ref = str(value or "").strip()
        kind, separator, item = ref.partition(":")
        if separator and kind == "doctest" and item.strip():
            names.append(item.strip())
    return tuple(dict.fromkeys(names))


def _execution_projection(workspace: Workspace | None, test: dict) -> dict:
    """Return current cycle inputs for readiness without persisting them."""

    if (
        workspace is None
        or not doc_test_service.is_cycle_test(test)
        or not test.get("items")
    ):
        return test
    table = str(
        (((test.get("definition") or {}).get("population") or {}).get("table"))
        or ""
    )
    known_tables = {
        str(value.get("name") or "")
        for value in [*workspace.tables, *workspace.joins]
    }
    if table not in known_tables:
        return test
    projected = dict(test)
    projected["items"] = doc_test_service.cycle_vouching.materialize_cycle_items(
        workspace, projected
    )
    return projected


def _outstanding(workspace: Workspace, test: dict) -> bool:
    """Whether this test still owes execution or auditor disposition.

    Read through the joint projection rather than the disposition alone. On an
    item-first test the runner's own ``passed``/``failed`` verdict resolves an
    item, so a cleanly executed test is not re-selected forever merely because
    nobody countersigned it; what stays outstanding is an item left unresolved
    — never run, parked by an auditor, inconclusive to the runner, or signed off
    against evidence that has since changed. A cycle item projects to
    ``agent_checked`` until it is dispositioned, so cycle tests keep owing an
    explicit sign-off.
    """

    test = _execution_projection(workspace, test)
    if doc_test_service.is_cycle_test(test) and not test.get("items"):
        return True
    return any(
        doc_test_service.item_execution_pending(test, item)
        or doc_test_service.item_state_projection(test, item)
        not in {"confirmed", "exception"}
        or bool((item.get("disposition") or {}).get("stale"))
        for item in test.get("items") or []
    )


def resolve_doc_test_scope(workspace: Workspace, scope: dict) -> DocTestScope:
    """Resolve explicitly named tests, or a bounded set of outstanding ones."""

    known = {
        str(summary["id"]) for summary in doc_test_service.list_tests(workspace)
    }
    requested = _requested_ids(scope)
    selected = [value for value in requested if value in known]
    unknown = [value for value in requested if value not in known]

    ambiguity: str | None = None
    if requested:
        test_ids = tuple(dict.fromkeys(selected))
    else:
        eligible = tuple(
            test_id
            for test_id in sorted(known)
            if _outstanding(workspace, doc_test_service.load_test(workspace, test_id))
        )
        test_ids = eligible[:MAX_SCOPE_TESTS]
        if len(eligible) > MAX_SCOPE_TESTS:
            ambiguity = (
                f"{len(eligible)} Document Tests have outstanding items and none was "
                f"named; running the first {MAX_SCOPE_TESTS} in identifier order. "
                "Name the Document Tests to run instead."
            )
    return DocTestScope(
        test_ids=test_ids,
        requested=requested,
        unknown=tuple(dict.fromkeys(unknown)),
        ambiguity=ambiguity,
    )


def scoped_tests(workspace: Workspace, scope: dict) -> list[dict]:
    """The durable Document Test records in scope, in resolved order."""

    resolved = resolve_doc_test_scope(workspace, scope)
    return [
        doc_test_service.load_test(workspace, test_id)
        for test_id in resolved.test_ids
    ]


def _forced(scope: dict) -> bool:
    return str(scope.get("generation_mode") or "") == "force"


def _unknown_tests(test_scope: DocTestScope) -> Readiness | None:
    if test_scope.unknown:
        return Readiness(
            "blocked",
            tuple(
                f"'{test_id}' is not a Document Test in this workspace"
                for test_id in test_scope.unknown
            ),
        )
    return None


# --------------------------------------------------------------------------- #
# Shared unit expansion (also used by the audit graph's fieldwork.executed)
# --------------------------------------------------------------------------- #
#: What each model-facing document-test unit kind reads, keyed by kind because
#: they do not read the same thing: a document assessment reads pages, and a
#: cycle judgment reads one linked cycle's pending checks and the values they
#: bind. Declared beside the expansion that produces the kinds, and imported by
#: the audit graph, so the two graphs cannot drift into disagreeing about what
#: a kind is given.
DOCUMENT_TEST_CONTEXT = {
    "document_qa_execution": "fieldwork.document_qa",
    "document_llm_execution": "fieldwork.document_qa",
    "cycle_vouch_execution": "fieldwork.cycle_vouch",
}


def document_test_units(
    workspace: Workspace,
    test_id: str,
    *,
    forced: bool,
    title: str,
    parent_refs: tuple[str, ...] = (),
) -> list[UnitSpec]:
    """Expand one Document Test into the units its kind and state require.

    Four shapes, and which one applies is a property of the test rather than of
    the graph that scheduled it:

    * a worklist waiting on requested evidence stays one execution unit, so the
      run records the block against the evidence request instead of pretending to
      test;
    * Q&A, attribute, and review tests fan out one unit per unanswered
      item/document pair, because each assessment is a separate bounded model
      turn against declared page context;
    * a test the agent has already checked end to end becomes a review unit, which
      only an auditor can settle;
    * anything else is one deterministic local execution unit.

    ``parent_refs`` prefixes the caller's own lineage — the audit graph adds the
    RCM row the worklist covers — ahead of the references this expansion
    owns.
    """
    test = doc_test_service.load_test(workspace, test_id)
    test = _execution_projection(workspace, test)
    prefix = tuple(parent_refs)
    test_refs = prefix + (f"doctest:{test_id}",)
    if test.get("kind") in {"qa", "attribute", "review"}:
        if doc_test_service.evidence_blocked(test):
            return [
                UnitSpec(
                    semantic_unit_id("document_test_execution", test_id),
                    "document_test_execution",
                    f"Run document test — {title}",
                    test_refs,
                    {"test_id": test_id},
                )
            ]
        llm_units = []
        for item in test.get("items") or []:
            answered = set((item.get("llm_answers") or item.get("qa_answers") or {}).keys())
            for document_id in item.get("document_ids") or []:
                if document_id in answered and not forced:
                    continue
                unit_kind = "document_qa_execution" if test.get("kind") == "qa" else "document_llm_execution"
                llm_units.append(
                    UnitSpec(
                        semantic_unit_id(
                            unit_kind,
                            test_id,
                            item["id"],
                            document_id,
                        ),
                        unit_kind,
                        f"Assess {item.get('label') or item['id']} — {document_id}",
                        test_refs
                        + (f"docitem:{item['id']}", f"document:{document_id}"),
                        {
                            "test_sha1": test.get("sha1"),
                            "question": item.get("question"),
                            "document_sha1": next(
                                (
                                    document.get("sha1")
                                    for document in workspace.documents
                                    if document.get("id") == document_id
                                ),
                                None,
                            ),
                        },
                    )
                )
        if llm_units:
            return llm_units
        return [
            UnitSpec(
                semantic_unit_id("document_test_review", test_id),
                "document_test_review",
                f"Review LLM-assisted document test — {title}",
                test_refs,
                {"test_id": test_id},
            )
        ]
    if test.get("kind") == "cycle_vouch" and not doc_test_service.evidence_blocked(test):
        # One unit per linked cycle, because one model pass judges one cycle:
        # the reader needs the whole cycle in front of it to tell a difference
        # in presentation from a difference in fact.
        judgment_units = [
            UnitSpec(
                semantic_unit_id("cycle_vouch_execution", test_id, item["id"]),
                "cycle_vouch_execution",
                f"Vouch cycle {item.get('label') or item['id']}",
                test_refs + (f"cycleitem:{item['id']}",),
                {"test_sha1": test.get("sha1"), "item_id": item["id"]},
            )
            for item in test.get("items") or []
            if forced or cycle_vouching.execution_pending(item, cycle=True)
        ]
        if judgment_units:
            return judgment_units
    already_checked = bool(test.get("items")) and all(
        doc_test_service.item_execution_current(test, item)
        for item in test.get("items") or []
    )
    # Explicit force is the auditor asking for the work again, so a test the
    # agent already checked is re-executed rather than handed straight back for
    # review. Items with a final result are still skipped —
    # that is ``run_document_test``'s own rule, not a scheduling decision.
    kind = (
        "document_test_review"
        if already_checked and not forced
        else "document_test_execution"
    )
    return [
        UnitSpec(
            semantic_unit_id(kind, test_id),
            kind,
            f"{'Review' if already_checked else 'Execute'} doctest — {title}",
            test_refs,
            {"test_id": test_id},
        )
    ]


# --------------------------------------------------------------------------- #
# doc_tests.definitions_ready
# --------------------------------------------------------------------------- #
def _definitions_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Structural executability only — never whether the result is current."""

    test_scope = resolve_doc_test_scope(workspace, scope)
    blocked = _unknown_tests(test_scope)
    if blocked is not None:
        return blocked
    if not test_scope.test_ids:
        # Nothing in scope is a satisfied outcome, not a blocked one.
        return Readiness("satisfied", details={"tests": 0})
    unusable = [
        test_id
        for test_id in test_scope.test_ids
        if doc_test_service.execution_issues(
            doc_test_service.load_test(workspace, test_id)
        )
    ]
    details = {"tests": len(test_scope.test_ids), "unusable": len(unusable)}
    if not unusable:
        return Readiness("satisfied", details=details)
    return Readiness(
        "review_required",
        (
            f"{counted(len(unusable), 'document test definition')} cannot perform even "
            "bounded local work",
        ),
        details=details,
    )


def _definition_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per unusable definition — the agent cannot author the fix.

    A standalone Document Test was created by the auditor (or by the audit
    graph's ``tests.specified``); this workflow never invents one, so
    a definition that cannot run is reported rather than regenerated.
    """
    test_scope = resolve_doc_test_scope(workspace, scope)
    units = []
    for test_id in test_scope.test_ids:
        test = doc_test_service.load_test(workspace, test_id)
        issues = doc_test_service.execution_issues(test)
        if not issues:
            continue
        units.append(
            UnitSpec(
                semantic_unit_id("document_test_definition", test_id),
                "document_test_definition",
                f"Review definition — {test.get('title') or test_id}",
                (f"doctest:{test_id}",),
                {"test_id": test_id, "issues": issues},
            )
        )
    return units


def _doc_tests_definitions_ready() -> Capability:
    return Capability(
        "doc_tests.definitions_ready",
        "doc_test_definitions",
        "Document test definitions",
        "document_test_definition",
        doc_tests_workflow.dependencies("doc_tests.definitions_ready"),
        _definitions_ready,
        _definition_units,
        # Deterministic local inspection of a durable definition: no model ever
        # sees this capability's inputs, so it declares no context.
        context=None,
        invalidate_on=("definition",),
    )


# --------------------------------------------------------------------------- #
# doc_tests.executed
# --------------------------------------------------------------------------- #
def unexecuted_items(test: dict, workspace: Workspace | None = None) -> int:
    """Items the agent has not yet checked.

    Deliberately *not* ``result_rollup(...)['pending']``, which also counts
    ``agent_checked``: that field includes transient model work. An
    ``agent_checked`` item has been executed
    — its comparison ran and its anchors are recorded — so counting it here would
    leave ``doc_tests.executed`` permanently unsatisfied and re-run every item on
    every request.
    """
    test = _execution_projection(workspace, test)
    if doc_test_service.is_cycle_test(test) and not test.get("items"):
        # The item builder itself is the first deterministic execution work.
        return 1
    return sum(
        doc_test_service.item_execution_pending(test, item)
        for item in test.get("items") or []
    )


def _executed_ready(workspace: Workspace, scope: dict) -> Readiness:
    test_scope = resolve_doc_test_scope(workspace, scope)
    blocked = _unknown_tests(test_scope)
    if blocked is not None:
        return blocked
    pending = 0
    evidence_blocked = 0
    for test_id in test_scope.test_ids:
        test = doc_test_service.load_test(workspace, test_id)
        if doc_test_service.evidence_blocked(test):
            evidence_blocked += 1
            continue
        if unexecuted_items(test, workspace):
            pending += 1
    details = {
        "tests": len(test_scope.test_ids),
        "pending": pending,
        "evidence_blocked": evidence_blocked,
    }
    if pending:
        return Readiness(
            "missing",
            (f"{counted(pending, 'document test')} {verb(pending, 'has', 'have')} unchecked items",),
            details=details,
        )
    if evidence_blocked:
        return Readiness(
            "review_required",
            (f"{counted(evidence_blocked, 'document test')} {verb(evidence_blocked, 'is', 'are')} blocked on evidence",),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _execution_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    test_scope = resolve_doc_test_scope(workspace, scope)
    forced = _forced(scope)
    units: list[UnitSpec] = []
    for test_id in test_scope.test_ids:
        test = doc_test_service.load_test(workspace, test_id)
        if doc_test_service.execution_issues(test):
            # An unusable definition is reported by the upstream capability; it
            # is not silently executed here.
            continue
        if not forced and not unexecuted_items(test, workspace):
            continue
        units.extend(
            document_test_units(
                workspace,
                test_id,
                forced=forced,
                title=str(test.get("title") or test_id),
            )
        )
    return units


def _doc_tests_executed() -> Capability:
    return Capability(
        "doc_tests.executed",
        "doc_test_execution",
        "Document test execution",
        "mixed_execution",
        doc_tests_workflow.dependencies("doc_tests.executed"),
        _executed_ready,
        _execution_units,
        context=DOCUMENT_TEST_CONTEXT,
        invalidate_on=("definition", "evidence"),
    )


# --------------------------------------------------------------------------- #
# doc_tests.dispositioned
# --------------------------------------------------------------------------- #
def undispositioned_items(test: dict) -> int:
    """Current execution outcomes that still require an auditor decision."""

    return sum(
        doc_test_service.item_disposition_pending(test, item)
        for item in test.get("items") or []
    )


def _dispositioned_ready(workspace: Workspace, scope: dict) -> Readiness:
    test_scope = resolve_doc_test_scope(workspace, scope)
    blocked = _unknown_tests(test_scope)
    if blocked is not None:
        return blocked
    tests = [
        doc_test_service.load_test(workspace, test_id)
        for test_id in test_scope.test_ids
    ]
    awaiting_execution = sum(unexecuted_items(test, workspace) for test in tests)
    pending = sum(undispositioned_items(test) for test in tests)
    details = {
        "tests": len(test_scope.test_ids),
        "awaiting_execution": awaiting_execution,
        "pending": pending,
    }
    if awaiting_execution:
        return Readiness(
            "missing",
            (f"{counted(awaiting_execution, 'document test item')} must execute before disposition",),
            details=details,
        )
    if pending:
        return Readiness(
            "review_required",
            (f"{counted(pending, 'document test item')} {verb(pending, 'awaits', 'await')} auditor disposition",),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _disposition_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    test_scope = resolve_doc_test_scope(workspace, scope)
    units: list[UnitSpec] = []
    for test_id in test_scope.test_ids:
        test = doc_test_service.load_test(workspace, test_id)
        for item in test.get("items") or []:
            if not doc_test_service.item_disposition_pending(test, item):
                continue
            units.append(
                UnitSpec(
                    semantic_unit_id("document_test_disposition", test_id, item["id"]),
                    "document_test_disposition",
                    f"Disposition {item.get('label') or item['id']} — {test.get('title') or test_id}",
                    (f"doctest:{test_id}", f"docitem:{item['id']}"),
                    {"test_id": test_id, "item_id": item["id"]},
                )
            )
    return units


def _doc_tests_dispositioned() -> Capability:
    return Capability(
        "doc_tests.dispositioned",
        "doc_test_disposition",
        "Auditor disposition",
        "document_test_disposition",
        doc_tests_workflow.dependencies("doc_tests.dispositioned"),
        _dispositioned_ready,
        _disposition_units,
        context=None,
        invalidate_on=("definition", "evidence", "evaluation", "disposition"),
    )


_BUILDERS = {
    "doc_tests.definitions_ready": _doc_tests_definitions_ready,
    "doc_tests.executed": _doc_tests_executed,
    "doc_tests.dispositioned": _doc_tests_dispositioned,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)


__all__ = [
    "CAPABILITY_IDS",
    "CHECKED_STATES",
    "DISPOSED_STATES",
    "MAX_SCOPE_TESTS",
    "DocTestScope",
    "capabilities",
    "document_test_units",
    "unexecuted_items",
    "undispositioned_items",
    "resolve_doc_test_scope",
    "scoped_tests",
]
