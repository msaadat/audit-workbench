"""Durable document tests and explainable, deterministic matching.

Each test is stored independently under ``DocTests/<test-id>.json``.  That
keeps row-level confirmations out of ``workspace.json`` and makes every item
checkpoint an atomic, resumable write.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import unicodedata
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import analytics, cycle_vouching, documents, explore
from .text import counted, verb
from .evidence import document_anchor, normalize_many
from .workspaces import (
    CONTROL_CONCLUSIONS,
    TEST_STATUSES,
    Workspace,
    WorkspaceConflict,
    WorkspaceError,
    slugify,
    sync_workspace,
    workspace_write_lock,
    write_json_atomic,
)

KINDS = {"vouching", "attribute", "review", "qa", "cycle_vouch"}
DIRECTIONS = {"vouching", "tracing"}
# What the runner found and what the auditor decided about it are two separate
# facts, and an audit file needs both on the record. Item-first tests keep
# ``item.state`` as their joint projection so every existing counter, rollup,
# and worklist reader carries on working, but these two vocabularies below are
# what actually gets written. Cycle tests have always been split this way.
STATES = {"pending", "agent_checked", "confirmed", "exception", "manual_review"}
EVALUATION_STATES = {"not_run", "agent_checked", "passed", "failed", "inconclusive"}
DISPOSITION_STATES = {"pending", "confirmed", "exception", "needs_review"}
METHODS = {
    "exact",
    "normalized",
    "fuzzy",
    "numeric_tolerance",
    "date_tolerance",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tests_dir(workspace: Workspace) -> Path:
    path = workspace.root / "DocTests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _test_path(workspace: Workspace, test_id: str) -> Path:
    test_id = str(test_id or "")
    if not _ID_RE.fullmatch(test_id):
        raise WorkspaceError("Invalid document-test ID.")
    return tests_dir(workspace) / f"{test_id}.json"


def _json_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _sha1(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def test_sha1(test: dict) -> str:
    return _sha1({key: value for key, value in test.items() if key != "sha1"})


# What a finding actually rests on when it cites a Document Test: what was
# tested, against which documents, and what the runner found. ``test_sha1``
# above cannot serve this purpose — it covers the whole record, and every save
# stamps ``updated``, so recording a conclusion or dispositioning one item would
# read as "the evidence under this finding changed".
#
# Auditor rulings (``disposition``), runner notes, roll-ups, and save stamps are
# therefore excluded. So are item ``evidence_refs``: ``normalize_anchor`` mints a
# fresh ``EV-`` id for any anchor stored without one, which would make the hash
# differ between two reads of the same unchanged file.
_TEST_EVIDENCE_FIELDS = (
    "id",
    "rcm_id",
    "kind",
    "objective",
    "criteria",
    "steps",
    "spec",
)
_ITEM_ANNOTATION_FIELDS = frozenset(
    {"disposition", "evaluation", "runner_note", "qa_answers", "evidence_refs"}
)


def test_evidence_projection(test: dict) -> dict:
    """Return the evidentiary basis of one Document Test."""
    projection = {field: test.get(field) for field in _TEST_EVIDENCE_FIELDS}
    projection["items"] = [
        {
            key: value
            for key, value in (item or {}).items()
            if key not in _ITEM_ANNOTATION_FIELDS
        }
        for item in test.get("items") or []
    ]
    return projection


def test_evidence_sha1(test: dict) -> str:
    return _sha1(test_evidence_projection(test))


def _normalize_steps(values: object) -> list[dict]:
    """Preserve declared step fields as objects; reject a step that is not an object."""
    steps: list[dict] = []
    for value in values or []:
        if not value:
            continue
        if not isinstance(value, dict):
            raise WorkspaceError("Each test step must be an object.")
        steps.append(dict(value))
    return steps


def kind_from_steps(steps: list[dict]) -> str:
    """Derive a Document Test's durable kind from its homogeneous step mode."""
    modes = {str(step.get("mode") or "") for step in steps if isinstance(step, dict)}
    modes.discard("")
    if modes == {"question"}:
        return "qa"
    if modes == {"vouch"}:
        return "vouching"
    raise WorkspaceError("Document Test steps must share one execution mode (question or vouch).")


def is_cycle_test(test: dict) -> bool:
    return str(test.get("kind") or "") == "cycle_vouch"


def item_execution_pending(test: dict, item: dict) -> bool:
    """Whether deterministic/model execution still owes work for this item."""

    return cycle_vouching.execution_pending(item, cycle=is_cycle_test(test))


def item_execution_current(test: dict, item: dict) -> bool:
    """Whether the runner has produced a current outcome for this item."""

    return cycle_vouching.execution_current(item, cycle=is_cycle_test(test))


def item_disposition_current(test: dict, item: dict) -> bool:
    """Whether the auditor has dispositioned the current item outcome."""

    return cycle_vouching.disposition_current(item, cycle=is_cycle_test(test))


def item_disposition_pending(test: dict, item: dict) -> bool:
    return cycle_vouching.disposition_pending(item, cycle=is_cycle_test(test))


def item_state_projection(test: dict, item: dict) -> str:
    """Project the split cycle/item state into joint list counters."""

    if not is_cycle_test(test):
        return project_item_state(item)
    disposition = item.get("disposition") or {}
    if item_disposition_current(test, item):
        return str(disposition.get("state"))
    if item_execution_current(test, item):
        return "agent_checked"
    return "pending"


# The joint reading each side projects into. An auditor's `needs_review` and a
# runner's `inconclusive` both surface as `manual_review` because that is the
# one bucket the worklist has for "somebody still has to look at this"; which of
# the two it was stays readable on the item itself.
_EVALUATION_PROJECTION = {
    "not_run": "pending",
    "agent_checked": "agent_checked",
    "passed": "confirmed",
    "failed": "exception",
    "inconclusive": "manual_review",
}
_DISPOSITION_PROJECTION = {
    "confirmed": "confirmed",
    "exception": "exception",
    "needs_review": "manual_review",
}
_STATE_EVALUATION = {
    joint: state for state, joint in _EVALUATION_PROJECTION.items()
}


def new_evaluation(
    state: str = "not_run",
    note: str = "",
    *,
    input_sha1: str | None = None,
    ran_at: str | None = None,
) -> dict:
    """What the runner found, and against which evidence it found it."""

    if state not in EVALUATION_STATES:
        raise WorkspaceError("Unknown document-test evaluation state.")
    return {
        "state": state,
        "note": str(note or ""),
        "input_sha1": input_sha1,
        "ran_at": ran_at,
    }


def new_disposition(
    state: str = "pending",
    note: str = "",
    *,
    actor: str | None = None,
    at: str | None = None,
    evaluated_input_sha1: str | None = None,
    stale: bool = False,
) -> dict:
    """What the auditor decided, by whom, when, and against which evidence."""

    if state not in DISPOSITION_STATES:
        raise WorkspaceError("Unknown document-test disposition state.")
    return {
        "state": state,
        "note": str(note or ""),
        "actor": actor,
        "at": at,
        "evaluated_input_sha1": evaluated_input_sha1,
        "stale": bool(stale),
    }


def project_item_state(item: dict) -> str:
    """Read one item jointly: the auditor's call when current, else the runner's.

    A stale disposition falls back to the runner's verdict rather than standing
    in for a decision that was made against evidence which has since changed.
    """

    disposition = item.get("disposition") or {}
    state = str(disposition.get("state") or "pending")
    if state != "pending" and not disposition.get("stale"):
        return _DISPOSITION_PROJECTION[state]
    evaluation = item.get("evaluation") or {}
    return _EVALUATION_PROJECTION[str(evaluation.get("state") or "not_run")]


def _normalize_marking(item: dict) -> None:
    """Validate one item's runner verdict and auditor call, then re-project."""

    evaluation = item.get("evaluation")
    item["evaluation"] = new_evaluation(
        str((evaluation or {}).get("state") or "not_run"),
        str((evaluation or {}).get("note") or ""),
        input_sha1=(evaluation or {}).get("input_sha1"),
        ran_at=(evaluation or {}).get("ran_at"),
    )
    disposition = item.get("disposition")
    item["disposition"] = new_disposition(
        str((disposition or {}).get("state") or "pending"),
        str((disposition or {}).get("note") or ""),
        actor=(disposition or {}).get("actor"),
        at=(disposition or {}).get("at"),
        evaluated_input_sha1=(disposition or {}).get("evaluated_input_sha1"),
        stale=bool((disposition or {}).get("stale")),
    )
    item["state"] = project_item_state(item)
    # ``runner_note`` is the single note older readers (the agent context
    # adapter, the report) still read. Keep it pointing at whichever side the
    # joint state currently reflects.
    current = item["disposition"]
    item["runner_note"] = (
        current["note"]
        if item["state"] == _DISPOSITION_PROJECTION.get(current["state"])
        and not current["stale"]
        and current["note"]
        else item["evaluation"]["note"]
    )


def _document_sha1_index(workspace: Workspace) -> dict[str, str]:
    return {
        str(document.get("id")): str(document.get("sha1") or "")
        for document in workspace.documents
    }


def item_input_sha1(
    test: dict, item: dict, sha1_by_document: Mapping[str, str]
) -> str:
    """Hash everything one run consumes for this item.

    Evidence and procedure both feed it, so attaching a document, swapping a
    file behind an id, or editing a matching rule all change the hash — which is
    what makes a prior sign-off detectably stale instead of silently wrong.
    """

    document_ids = [str(value) for value in item.get("document_ids") or []]
    return _sha1(
        {
            "kind": str(test.get("kind") or ""),
            "documents": [
                [document_id, sha1_by_document.get(document_id, "")]
                for document_id in document_ids
            ],
            "checks": [
                {
                    "field": _json_value(check.get("field")),
                    "expected": _json_value(check.get("expected")),
                    "method": _json_value(check.get("method")),
                    "tolerance": _json_value(check.get("tolerance")),
                }
                for check in item.get("checks") or []
            ],
            "attributes": [
                {
                    "name": _json_value(attribute.get("name")),
                    "expected": _json_value(attribute.get("expected")),
                }
                for attribute in item.get("attributes") or []
            ],
            "question": str(item.get("question") or ""),
            "page": _json_value(item.get("page")),
            "pages": [_json_value(value) for value in item.get("pages") or []],
        }
    )


def _refresh_staleness(test: dict, sha1_by_document: Mapping[str, str]) -> None:
    """Mark a run and a sign-off stale when their inputs no longer match."""

    for item in test.get("items") or []:
        current = item_input_sha1(test, item, sha1_by_document)
        disposition = item["disposition"]
        signed = disposition["evaluated_input_sha1"]
        disposition["stale"] = bool(
            disposition["state"] != "pending" and signed is not None and signed != current
        )
        evaluation = item["evaluation"]
        if (
            evaluation["state"] != "not_run"
            and evaluation["input_sha1"] is not None
            and evaluation["input_sha1"] != current
        ):
            # The run no longer describes the evidence in front of the auditor.
            item["evaluation"] = new_evaluation(
                "not_run",
                "Evidence or procedure changed after this run; re-run the item.",
            )
        _normalize_marking(item)


def record_evaluation(
    workspace: Workspace, test: dict, item: dict, state: str, note: str
) -> dict:
    """Persist what the runner found without touching the auditor's call."""

    item["evaluation"] = new_evaluation(
        state,
        note,
        input_sha1=item_input_sha1(test, item, _document_sha1_index(workspace)),
        ran_at=utcnow(),
    )
    _normalize_marking(item)
    return item


def record_disposition(
    workspace: Workspace,
    test: dict,
    item: dict,
    state: str,
    *,
    note: str = "",
    actor: str = "auditor",
) -> dict:
    """Persist the auditor's call without touching what the runner found."""

    if state == "pending":
        item["disposition"] = new_disposition("pending", note)
    else:
        item["disposition"] = new_disposition(
            state,
            note,
            actor=actor,
            at=utcnow(),
            evaluated_input_sha1=item_input_sha1(
                test, item, _document_sha1_index(workspace)
            ),
        )
    _normalize_marking(item)
    return item


def invalidate_evaluation(test: dict, item: dict, reason: str) -> dict:
    """Retire a run whose evidence or procedure just changed.

    A sign-off made against the old inputs is marked stale rather than deleted:
    the fact that an auditor signed remains part of the record, it just stops
    counting as a current decision.
    """

    item["evaluation"] = new_evaluation("not_run", reason)
    disposition = item.get("disposition") or {}
    if str(disposition.get("state") or "pending") != "pending":
        disposition["stale"] = True
    _normalize_marking(item)
    return item


def refresh_test_status(test: dict) -> str:
    """Roll item markings into the test's status, without flattering the result.

    An item the runner could not settle, or one whose sign-off went stale,
    leaves the test in ``review_required``. Reporting that as ``completed`` was
    what let a test nobody had resolved unlock finding generation.
    """

    items = test.get("items") or []
    if not items:
        return str(test.get("status") or "draft")
    states = [project_item_state(item) for item in items]
    if any(state in {"pending", "agent_checked"} for state in states):
        test["status"] = "in_progress"
    elif any(state == "manual_review" for state in states) or any(
        (item.get("disposition") or {}).get("stale") for item in items
    ):
        test["status"] = "review_required"
    else:
        # RCM execution refines a completed test into with/without exception.
        # Keep that reading when the items still bear it out, so recording a
        # sign-off does not flatten what a completed run already established.
        refined = str(test.get("status") or "")
        has_exception = any(state == "exception" for state in states)
        test["status"] = (
            refined
            if refined == ("completed_with_exception" if has_exception else "completed_no_exception")
            else "completed"
        )
    return test["status"]


def execution_settled(test: dict) -> bool:
    """Whether every item has a runner verdict, signed off or not."""

    items = test.get("items") or []
    return bool(items) and all(
        str((item.get("evaluation") or {}).get("state") or "not_run")
        in {"passed", "failed", "inconclusive"}
        for item in items
    )


def _hydrate(test: dict, workspace: Workspace | None = None) -> dict:
    # A drafted test has no kind until its spec pass runs, so ``kind`` stays
    # None rather than defaulting to a builder the plan never chose.
    kind = str(test.get("kind") or "") or None
    test.setdefault("status", "draft")
    test.setdefault("semantic_id", f"doctest:{slugify(test.get('title') or 'test')}")
    test.setdefault("rcm_refs", [])
    test.setdefault("procedure_refs", [])
    test.setdefault("rcm_id", None)
    # Audit plan — the fields that used to live on the RCM planned test.
    test.setdefault("objective", "")
    test.setdefault("criteria", "")
    test.setdefault("steps", [])
    test.setdefault("methodology_refs", [])
    # Outcome.
    test.setdefault("conclusion", "")
    # Conclusions produced by the bounded worker may be refreshed on a forced
    # rerun. A conclusion saved through the auditor-facing API is never changed
    # by that process.
    test.setdefault(
        "conclusion_source",
        "auditor" if str(test.get("conclusion") or "").strip() else "none",
    )
    test.setdefault("control_conclusion", "no_conclusion")
    test.setdefault(
        "control_conclusion_source",
        "auditor" if test["control_conclusion"] != "no_conclusion" else "none",
    )
    test.setdefault("result_summary", "")
    test.setdefault("scope_limitations", "")
    test.setdefault("next_action", "")
    test.setdefault("exception_count", 0)
    test.setdefault("open_exception_count", 0)
    test.setdefault("evidence_refs", [])
    test.setdefault("finding_refs", [])
    test.setdefault("created_by", "user")
    test.setdefault("agent_run_id", None)
    test.setdefault("workflow_parent_sha1", None)
    if test.get("rcm_id") and test["rcm_id"] not in test["rcm_refs"]:
        test["rcm_refs"].append(test["rcm_id"])
    test.setdefault("spec", {})
    test.setdefault("items", [])
    test.setdefault("created", utcnow())
    test.setdefault("updated", test["created"])
    if kind == "cycle_vouch":
        try:
            cycle_vouching.validate_cycle_test(test)
        except cycle_vouching.CycleSchemaError as error:
            raise WorkspaceError(str(error)) from error
    for index, raw_item in enumerate(test["items"]):
        item = raw_item
        item.setdefault("id", f"ITEM-{uuid.uuid4().hex[:8].upper()}")
        item.setdefault("instruction", "")
        if kind == "cycle_vouch":
            try:
                item = cycle_vouching.normalize_cycle_item(
                    item, registry_ref=test["registry"]
                )
            except cycle_vouching.CycleSchemaError as error:
                raise WorkspaceError(str(error)) from error
            test["items"][index] = item
        else:
            _normalize_marking(item)
        item.setdefault("document_ids", [])
        item.setdefault("evidence_refs", [])
        item["evidence_refs"] = normalize_many(item.get("evidence_refs") or [])
        for check in item.get("checks") or []:
            check.setdefault("verdict", "pending")
            check.setdefault("comparisons", [])
            check["evidence_refs"] = normalize_many(check.get("evidence_refs") or [])
    if kind != "cycle_vouch" and workspace is not None and test["items"]:
        _refresh_staleness(test, _document_sha1_index(workspace))
        # A read may discover that evidence moved under a signed-off test, but
        # it must not promote a test nobody has touched. Only the downgrade is
        # safe to apply here; the write paths own the full roll-up.
        if str(test.get("status") or "") == "completed" and refresh_test_status(
            copy.deepcopy(test)
        ) != "completed":
            test["status"] = "review_required"
    test["kind"] = kind
    test["evidence_refs"] = normalize_many(test.get("evidence_refs") or [])
    test["steps"] = _normalize_steps(test.get("steps"))
    test["sha1"] = test_sha1(test)
    return test


def save_test(workspace: Workspace, test: dict) -> dict:
    from .workspace_transactions import (
        complete_linked_write,
        prepare_linked_write,
        rollback_linked_write,
    )

    with workspace_write_lock(workspace.root):
        current_revision = Workspace(workspace.root).revision
        if current_revision != workspace.revision:
            raise WorkspaceConflict(workspace.revision, current_revision)
        test["updated"] = utcnow()
        test["sha1"] = test_sha1(test)
        path = _test_path(workspace, test["id"])
        linked_write = prepare_linked_write(workspace, path, test)
        try:
            write_json_atomic(path, test)
            _link_test(workspace, test)
            # Document Test files are linked workspace artifacts. Even
            # item-only edits advance the shared revision so API/workflow
            # races cannot be invisible to the transaction coordinator.
            workspace.save(expected_revision=current_revision)
        except Exception:
            rollback_linked_write(linked_write)
            sync_workspace(workspace, Workspace(workspace.root))
            raise
        else:
            complete_linked_write(linked_write)
    return test


def write_test(workspace: Workspace, test: dict) -> dict:
    """Rewrite one test file in place without advancing the workspace revision.

    For callers already inside their own workspace mutation that own the single
    save. :func:`save_test` stays the coordinated path for edits that must be
    visible as a workspace revision of their own.
    """
    test["updated"] = utcnow()
    test["sha1"] = test_sha1(test)
    write_json_atomic(_test_path(workspace, test["id"]), test)
    return test


# Readiness checks and worklist/report projections each resolve their own
# Document Test scope independently, so a single read-only computation (a
# capability workflow-state pass, a report render) can call ``list_tests``
# and ``load_test`` for the same tests many times over. Cycle-vouching tests
# re-validate every evidence record against every assertion on each call, so
# that fan-out is not free. ``request_cache_scope`` lets a caller that is
# certain no test write happens in its span memoize both functions for its
# duration; it is reentrant, so nesting is safe and only the outermost scope
# pays for setup and teardown.
_cache: ContextVar[dict | None] = ContextVar("doc_tests_request_cache", default=None)


@contextmanager
def request_cache_scope():
    if _cache.get() is not None:
        yield
        return
    token = _cache.set({})
    try:
        # Cycle-vouching materialization is the expensive step underneath
        # both functions here, and is called directly by some readers
        # (bypassing load_test/list_tests), so it needs the same scope.
        with cycle_vouching.request_cache_scope():
            yield
    finally:
        _cache.reset(token)


def load_test(workspace: Workspace, test_id: str) -> dict:
    cache = _cache.get()
    if cache is not None:
        tests = cache.setdefault("tests", {})
        cached = tests.get(test_id)
        if cached is not None:
            return copy.deepcopy(cached)
    path = _test_path(workspace, test_id)
    if not path.exists():
        raise WorkspaceError(f"Document test '{test_id}' not found.")
    try:
        result = _project_cycle_items(
            workspace,
            _hydrate(json.loads(path.read_text(encoding="utf-8")), workspace),
        )
    except json.JSONDecodeError as error:
        raise WorkspaceError(f"Document test '{test_id}' is unreadable.") from error
    if cache is not None:
        cache["tests"][test_id] = copy.deepcopy(result)
    return result


def exists(workspace: Workspace, test_id: str) -> bool:
    try:
        return _test_path(workspace, test_id).is_file()
    except WorkspaceError:
        return False


def _project_cycle_items(workspace: Workspace, test: dict) -> dict:
    """Expose the local item-builder projection without turning a GET into a write."""

    if not is_cycle_test(test):
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
    test["items"] = cycle_vouching.materialize_cycle_items(workspace, test)
    return test


def list_tests(workspace: Workspace) -> list[dict]:
    cache = _cache.get()
    if cache is not None:
        cached = cache.get("list_tests")
        if cached is not None:
            return copy.deepcopy(cached)
    items = []
    for path in tests_dir(workspace).glob("*.json"):
        try:
            test = _project_cycle_items(
                workspace,
                _hydrate(json.loads(path.read_text(encoding="utf-8")), workspace),
            )
        except (OSError, json.JSONDecodeError, WorkspaceError):
            continue
        # This projection already paid for cycle-item materialization; a
        # ``load_test`` call for the same test within this scope should reuse
        # it rather than materializing a second time.
        if cache is not None and test.get("id"):
            cache.setdefault("tests", {})[test["id"]] = copy.deepcopy(test)
        states = [item_state_projection(test, item) for item in test["items"]]
        items.append({
            **{key: test.get(key) for key in (
                "id", "kind", "title", "status", "semantic_id", "rcm_refs",
                "procedure_refs", "rcm_id", "spec", "created",
                "updated", "sha1", "created_by", "agent_run_id",
                "workflow_parent_sha1", "objective", "criteria", "steps",
                "schema_version", "registry", "requirement_refs", "procedure_key",
                "definition", "coverage", "context_manifest_sha256",
                "selection_confirmation",
                "conclusion", "conclusion_source", "control_conclusion", "control_conclusion_source", "result_summary", "next_action",
                "exception_count", "open_exception_count", "scope_limitations",
            )},
            "item_count": len(states),
            "state_counts": {state: states.count(state) for state in sorted(STATES)},
        })
    result = sorted(items, key=lambda item: item.get("updated") or "", reverse=True)
    if cache is not None:
        cache["list_tests"] = copy.deepcopy(result)
    return result


def _validate_links(workspace: Workspace, rcm_refs: list[str], procedure_refs: list[str]) -> None:
    known_rcm = {item["id"] for item in workspace.rcm}
    known_procedures = {item["id"] for item in workspace.work_program}
    missing = [ref for ref in rcm_refs if ref not in known_rcm]
    if missing:
        raise WorkspaceError(f"RCM row '{missing[0]}' not found.")
    missing = [ref for ref in procedure_refs if ref not in known_procedures]
    if missing:
        raise WorkspaceError(f"Procedure '{missing[0]}' not found.")


def _validate_rcm_id(workspace: Workspace, rcm_id: object) -> str | None:
    """Resolve the optional RCM row a test covers.

    The link is optional in both directions: an auditor may author a test with
    no row at all, and only the row side requires at least one test.
    """
    value = str(rcm_id or "").strip() or None
    if value and not any(row.get("id") == value for row in workspace.rcm):
        raise WorkspaceError(f"RCM row '{value}' not found.")
    return value


def _link_test(workspace: Workspace, test: dict) -> None:
    ref = f"doctest:{test['id']}"
    for row in workspace.rcm:
        refs = row.setdefault("test_refs", [])
        if row["id"] in test["rcm_refs"] and ref not in refs:
            refs.append(ref)
        elif row["id"] not in test["rcm_refs"]:
            row["test_refs"] = [value for value in refs if value != ref]
    for procedure in workspace.work_program:
        refs = procedure.setdefault("test_refs", [])
        if procedure["id"] in test["procedure_refs"] and ref not in refs:
            refs.append(ref)
        elif procedure["id"] not in test["procedure_refs"]:
            procedure["test_refs"] = [value for value in refs if value != ref]


def unlink_rcm(workspace: Workspace, rcm_id: str) -> list[str]:
    """Clear the RCM link from every Document Test that carried it.

    Used when a row is deleted. The test files are rewritten in place without
    advancing the workspace revision, because the caller is already inside its
    own workspace mutation and owns the single save.
    """
    unlinked = []
    for summary in list_tests(workspace):
        if summary.get("rcm_id") != rcm_id:
            continue
        test = load_test(workspace, summary["id"])
        test["rcm_id"] = None
        test["rcm_refs"] = [ref for ref in test.get("rcm_refs") or [] if ref != rcm_id]
        write_test(workspace, test)
        unlinked.append(test["id"])
    return unlinked


def _base_test(workspace: Workspace, payload: dict, kind: str | None) -> dict:
    if kind is not None and kind not in KINDS:
        raise WorkspaceError(
            "Document-test kind must be vouching, attribute, review, qa, or cycle_vouch."
        )
    title = str(payload.get("title") or f"New {kind or 'document'} test").strip()
    if not title:
        raise WorkspaceError("Document-test title is required.")
    rcm_id = _validate_rcm_id(workspace, payload.get("rcm_id"))
    rcm_refs = [str(ref) for ref in (payload.get("rcm_refs") or [])]
    procedure_refs = [str(ref) for ref in (payload.get("procedure_refs") or [])]
    _validate_links(workspace, rcm_refs, procedure_refs)
    if rcm_id and rcm_id not in rcm_refs:
        rcm_refs.append(rcm_id)
    status = str(payload.get("status") or ("draft" if kind is None else "ready"))
    if status not in TEST_STATUSES:
        raise WorkspaceError("Unknown document-test status.")
    now = utcnow()
    return {
        "id": str(payload.get("id") or f"DT-{uuid.uuid4().hex[:8].upper()}"),
        "kind": kind,
        "title": title,
        "status": status,
        "semantic_id": str(payload.get("semantic_id") or f"doctest:{slugify(title)}"),
        "rcm_refs": rcm_refs,
        "procedure_refs": procedure_refs,
        "rcm_id": rcm_id,
        "objective": str(payload.get("objective") or ""),
        "criteria": str(payload.get("criteria") or ""),
        "steps": _normalize_steps(payload.get("steps")),
        "methodology_refs": list(payload.get("methodology_refs") or []),
        "spec": dict(payload.get("spec") or {}),
        "items": [],
        "created_by": "agent" if payload.get("agent_run_id") else "user",
        "agent_run_id": payload.get("agent_run_id"),
        "workflow_parent_sha1": str(payload.get("workflow_parent_sha1") or "") or None,
        "created": now,
        "updated": now,
    }


def _new_item(payload: dict | None = None, *, cycle: bool = False) -> dict:
    payload = dict(payload or {})
    state = str(payload.get("state") or "pending")
    if state not in STATES:
        raise WorkspaceError("Unknown document-test item state.")
    item = {
        "id": str(payload.get("id") or f"ITEM-{uuid.uuid4().hex[:8].upper()}"),
        "label": str(payload.get("label") or "Test item"),
        "instruction": str(payload.get("instruction") or ""),
        "document_ids": [str(value) for value in (payload.get("document_ids") or [])],
        "evidence_refs": normalize_many(payload.get("evidence_refs") or []),
    }
    if cycle:
        # A cycle item's evaluation and disposition are typed against the
        # registry definition, so its own normalizer owns both fields.
        return item
    # A payload may carry the split fields directly, or the joint ``state`` as
    # shorthand — which reads as a runner verdict, because a caller building an
    # item is describing what a check found, not signing it off.
    item["evaluation"] = payload.get("evaluation") or new_evaluation(
        _STATE_EVALUATION[state], str(payload.get("runner_note") or "")
    )
    item["disposition"] = payload.get("disposition") or new_disposition()
    _normalize_marking(item)
    return item


def _build_items(workspace: Workspace, test: dict, raw_items: object) -> None:
    """Normalize one payload's items onto a test according to its kind."""
    kind = test["kind"]
    known_documents = {str(item.get("id")) for item in workspace.documents}
    for raw in raw_items or []:
        item = _new_item(raw, cycle=kind == "cycle_vouch")
        missing_documents = [
            document_id
            for document_id in item.get("document_ids") or []
            if document_id not in known_documents
        ]
        if missing_documents:
            raise WorkspaceError(
                f"Document '{missing_documents[0]}' not found for Document Test item."
            )
        if kind == "cycle_vouch":
            try:
                item = cycle_vouching.normalize_cycle_item(
                    {**dict(raw), **item}, registry_ref=test["registry"]
                )
            except cycle_vouching.CycleSchemaError as error:
                raise WorkspaceError(str(error)) from error
            role_document_ids = [
                str(binding.get("document_id") or "")
                for binding in item.get("role_bindings") or []
                if str(binding.get("document_id") or "")
            ]
            missing_role_documents = [
                document_id
                for document_id in role_document_ids
                if document_id not in known_documents
            ]
            if missing_role_documents:
                raise WorkspaceError(
                    f"Document '{missing_role_documents[0]}' not found for cycle role binding."
                )
            item["document_ids"] = list(dict.fromkeys(role_document_ids))
        elif kind == "vouching":
            item["frozen"] = {str(key): _json_value(value) for key, value in dict(raw.get("frozen") or {}).items()}
            item["checks"] = [_normalize_check(check) for check in (raw.get("checks") or [])]
        elif kind == "attribute":
            item["attributes"] = [_normalize_attribute(value) for value in (raw.get("attributes") or [])]
        elif kind == "review":
            item.update(page=raw.get("page"), review_kind=str(raw.get("review_kind") or "general"), summary=str(raw.get("summary") or ""), excerpt=str(raw.get("excerpt") or ""))
        elif kind == "qa":
            item.update(question=str(raw.get("question") or ""), response=str(raw.get("response") or ""), citations=normalize_many(raw.get("citations") or []))
        else:
            raise WorkspaceError(f"Unsupported Document Test kind '{kind}'.")
        test["items"].append(item)


def _apply_kind_spec(test: dict, payload: dict) -> None:
    if test["kind"] == "vouching":
        direction = str(
            (payload.get("spec") or {}).get("direction")
            or payload.get("direction")
            or "vouching"
        )
        if direction not in DIRECTIONS:
            raise WorkspaceError("Direction must be vouching or tracing.")
        test["spec"]["direction"] = direction
        test["spec"].setdefault("require_all_documents", True)
    elif test["kind"] == "cycle_vouch":
        test["schema_version"] = payload.get("schema_version")
        test["registry"] = dict(payload.get("registry") or {})
        test["requirement_refs"] = list(payload.get("requirement_refs") or [])
        test["procedure_key"] = str(payload.get("procedure_key") or "")
        test["definition"] = dict(payload.get("definition") or {})
        test["coverage"] = dict(payload.get("coverage") or {})
        test["context_manifest_sha256"] = str(
            payload.get("context_manifest_sha256") or ""
        )
        # Retained so a sampled definition stays distinguishable from an
        # auditor's free choice of sample: this one exists because the
        # evidence-linked reach exceeded the item cap.
        confirmation = payload.get("selection_confirmation")
        test["selection_confirmation"] = (
            dict(confirmation) if isinstance(confirmation, Mapping) else None
        )
        try:
            cycle_vouching.validate_cycle_test(test)
        except cycle_vouching.CycleSchemaError as error:
            raise WorkspaceError(str(error)) from error


def create_test(workspace: Workspace, payload: dict) -> dict:
    kind = str(payload.get("kind") or "vouching").lower()
    test = _base_test(workspace, payload, kind)
    _apply_kind_spec(test, payload)
    _build_items(workspace, test, payload.get("items"))
    save_test(workspace, test)
    return test


def create_draft(workspace: Workspace, payload: dict) -> dict:
    """Create a planned-but-unspecified Document Test.

    This is what the draft pass of test generation commits: the audit plan for
    one test, with no builder chosen and no items yet. :func:`apply_spec` fills
    those in and moves the record out of ``draft``.
    """
    test = _base_test(workspace, payload, None)
    save_test(workspace, test)
    return test


PLAN_FIELDS = (
    "title",
    "objective",
    "criteria",
    "steps",
    "methodology_refs",
)


def update_plan(workspace: Workspace, test_id: str, payload: dict) -> dict:
    """Rewrite one test's audit plan, leaving its spec and results untouched.

    This is what a re-run of the draft pass commits onto a test it has already
    created; the executable spec belongs to :func:`apply_spec`.
    """
    test = load_test(workspace, test_id)
    for key in PLAN_FIELDS:
        if key not in payload:
            continue
        if key == "steps":
            test["steps"] = _normalize_steps(payload["steps"])
        elif key == "methodology_refs":
            test["methodology_refs"] = list(payload["methodology_refs"] or [])
        else:
            test[key] = str(payload[key] or "")
    if "rcm_id" in payload:
        rcm_id = _validate_rcm_id(workspace, payload["rcm_id"])
        test["rcm_refs"] = [ref for ref in test.get("rcm_refs") or [] if ref != test.get("rcm_id")]
        test["rcm_id"] = rcm_id
        if rcm_id and rcm_id not in test["rcm_refs"]:
            test["rcm_refs"].append(rcm_id)
    for key in ("agent_run_id", "workflow_parent_sha1"):
        if payload.get(key):
            test[key] = str(payload[key])
    save_test(workspace, test)
    return test


def apply_spec(workspace: Workspace, test_id: str, payload: dict) -> dict:
    """Write the executable spec onto an existing test, replacing its items.

    Items with final results are never discarded: a test that
    has been executed must be re-drafted rather than silently re-specified.
    """
    test = load_test(workspace, test_id)
    # "Final result" is about the run having produced one, not about anybody
    # having signed it off — an executed test must be re-drafted either way.
    settled = [
        item for item in test.get("items") or []
        if item_execution_current(test, item)
    ]
    if settled:
        raise WorkspaceError(
            f"Document Test '{test_id}' has final-result items and cannot be re-specified."
        )
    kind = str(payload.get("kind") or "").lower()
    if kind not in KINDS:
        raise WorkspaceError(
            "Document-test kind must be vouching, attribute, review, qa, or cycle_vouch."
        )
    test["kind"] = kind
    test["spec"] = dict(payload.get("spec") or {})
    test["items"] = []
    _apply_kind_spec(test, payload)
    _build_items(workspace, test, payload.get("items"))
    if payload.get("title"):
        test["title"] = str(payload["title"]).strip()
    test["status"] = "ready"
    if payload.get("workflow_parent_sha1"):
        test["workflow_parent_sha1"] = str(payload["workflow_parent_sha1"])
    if payload.get("agent_run_id"):
        test["agent_run_id"] = str(payload["agent_run_id"])
    save_test(workspace, test)
    return test


def _normalize_check(check: dict) -> dict:
    """Normalize one literal comparison for auditor-authored simple vouching."""
    if "left" in check or "right" in check:
        raise WorkspaceError(
            "Dotted-path checks are not supported; use a typed cycle_vouch assertion."
        )
    method = str(check.get("method") or "normalized")
    if method not in METHODS:
        raise WorkspaceError(f"Unknown comparison method '{method}'.")
    return {
        "field": str(check.get("field") or "value"),
        "expected": _json_value(check.get("expected")),
        "found": _json_value(check.get("found")),
        "method": method,
        "tolerance": check.get("tolerance"),
        "verdict": str(check.get("verdict") or "pending"),
        "note": str(check.get("note") or ""),
        "comparisons": list(check.get("comparisons") or []),
        "evidence_refs": normalize_many(check.get("evidence_refs") or []),
    }


def _normalize_attribute(value: dict) -> dict:
    return {
        "name": str(value.get("name") or "Attribute"),
        "expected": str(value.get("expected") or ""),
        "verdict": str(value.get("verdict") or "pending"),
        "note": str(value.get("note") or ""),
        "evidence_refs": normalize_many(value.get("evidence_refs") or []),
    }


def build_vouching(workspace: Workspace, payload: dict) -> dict:
    table = str(payload.get("table") or "")
    if table not in workspace.table_names():
        raise WorkspaceError(f"No table named '{table}'.")
    frame = workspace.get_frame(table)
    frozen_fields = [str(field) for field in (payload.get("frozen_fields") or frame.columns[:6])]
    missing = [field for field in frozen_fields if field not in frame.columns]
    if missing:
        raise WorkspaceError(f"Frozen field '{missing[0]}' does not exist in '{table}'.")
    sampling_spec = {
        "method": str(payload.get("method") or "random"),
        "size": int(payload.get("size") or 25),
        "seed": int(payload.get("seed") or 42),
        "stratify_by": payload.get("stratify_by"),
    }
    result = analytics.run_test(frame, "sampling", sampling_spec)
    if result.detail is None:
        raise WorkspaceError("Sampling did not return a worklist.")
    safe = explore.frame_payload(result.detail)
    rows = [dict(zip(safe["columns"], row)) for row in safe["rows"]]
    direction = str(payload.get("direction") or "vouching")
    if direction not in DIRECTIONS:
        raise WorkspaceError("Direction must be vouching or tracing.")
    source_hash = _sha1(workspace._table_signature(table))
    test = _base_test(workspace, payload, "vouching")
    test["spec"].update({
        "direction": direction,
        "table": table,
        "table_sha1": source_hash,
        "sampling": sampling_spec,
        "selection_basis": "sample",
        "assurance_scope": "sampled_population",
        "frozen_fields": frozen_fields,
        "population_rows": frame.height,
        "require_all_documents": bool(payload.get("require_all_documents", True)),
    })
    default_method = str(payload.get("methodology") or "normalized")
    if default_method not in METHODS:
        default_method = "normalized"
    for row in rows:
        frozen = {field: row.get(field) for field in frozen_fields}
        source_row = row.get("source_row")
        item = _new_item({"label": f"{table} row {source_row}", "document_ids": []})
        item.update(
            source={"table": table, "source_row": source_row, "source_sha1": source_hash},
            frozen=frozen,
            checks=[_normalize_check({"field": field, "expected": value, "method": default_method}) for field, value in frozen.items()],
        )
        test["items"].append(item)
    save_test(workspace, test)
    return test


_DOCUMENT_TYPE_ALIASES = {
    "requisition": ("requisition", "purchase request", "req"),
    "purchase_order": ("purchase order", "po"),
    "goods_receipt": ("goods receipt", "receiving report", "grn"),
    "invoice": ("invoice", "inv"),
    "approval": ("approval", "approved", "authorization"),
    "contract": ("contract", "agreement"),
}


def _normalized_identifier(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    return text if len(text) >= 4 and any(char.isdigit() for char in text) else ""


def _document_index(workspace: Workspace) -> list[dict]:
    index = []
    for document in workspace.documents:
        source_text = f"{document.get('source') or ''} {document.get('title') or ''}"
        extracted = ""
        image_only = document.get("text_state") == "image_only"
        try:
            preview = documents.preview(workspace, document["id"])
            pages = preview.get("pages") or []
            extracted = "\n".join(str(page.get("text") or "") for page in pages)
            image_only = image_only or any(page.get("image_only") for page in pages)
        except Exception:
            pass
        raw = f"{source_text}\n{extracted}".casefold()
        normalized = re.sub(r"[^a-z0-9]+", "", raw)
        types = {
            kind
            for kind, aliases in _DOCUMENT_TYPE_ALIASES.items()
            if any(re.search(rf"(?:^|[^a-z0-9]){re.escape(alias)}(?:[^a-z0-9]|$)", raw) for alias in aliases)
        }
        index.append(
            {
                "document": document,
                "raw": raw,
                "normalized": normalized,
                "types": types,
                "image_only": image_only,
            }
        )
    return index


def _required_document_types(workspace: Workspace, payload: dict) -> list[str]:
    explicit = [
        str(value).strip().lower()
        for value in (payload.get("required_document_types") or [])
        if str(value).strip()
    ]
    if explicit:
        return list(dict.fromkeys(explicit))
    # Otherwise derive them from the test's own plan: each document step names
    # its own evidence via ``missing_evidence``/``question``, which is where
    # the expected evidence now lives.
    text_parts = [str(payload.get("criteria") or "")]
    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        text_parts.extend(
            str(step.get(field) or "")
            for field in ("missing_evidence", "instruction", "question", "label")
        )
    source = " ".join(text_parts).casefold()
    if not source.strip():
        return []
    return [
        kind
        for kind, aliases in _DOCUMENT_TYPE_ALIASES.items()
        if any(alias in source for alias in aliases)
    ]


def _evidence_request(
    workspace: Workspace,
    *,
    test: dict,
    item: dict,
    transaction_identifier: str,
    missing_types: list[str],
) -> dict:
    existing = next(
        (
            request
            for request in workspace.evidence_requests
            if request.get("document_test_id") == test["id"]
            and request.get("item_id") == item["id"]
            and request.get("status") == "open"
        ),
        None,
    )
    if existing is None:
        existing = {
            "id": f"ER-{uuid.uuid4().hex[:10].upper()}",
            "rcm_id": test.get("rcm_id"),
            "document_test_id": test["id"],
            "item_id": item["id"],
            "transaction_identifier": transaction_identifier,
            "missing_document_types": missing_types,
            "status": "open",
            "reason": "Required evidence was not available in the imported document set.",
            "next_action": "Request the listed evidence and attach it to this test item.",
            "created": utcnow(),
            "updated": utcnow(),
        }
        workspace.evidence_requests.append(existing)
    else:
        existing["missing_document_types"] = missing_types
        existing["updated"] = utcnow()
    return existing


def prepare_evidence_aware_vouching(workspace: Workspace, payload: dict) -> dict:
    """Build a vouching worklist that prioritizes transactions with evidence."""
    table = str(payload.get("table") or "")
    if table not in workspace.table_names():
        raise WorkspaceError(f"No table named '{table}'.")
    frame = workspace.get_frame(table).with_row_index("source_row", offset=2)
    identifier_fields = [str(value) for value in (payload.get("identifier_fields") or [])]
    if not identifier_fields:
        identifier_fields = [
            name
            for name in frame.columns
            if re.search(r"(?i)(id|no|num|number|ref|req|po|grn|inv)", name)
        ][:8]
    missing = [name for name in identifier_fields if name not in frame.columns]
    if missing:
        raise WorkspaceError(f"Identifier field '{missing[0]}' does not exist in '{table}'.")
    frozen_fields = [
        str(value) for value in (payload.get("frozen_fields") or identifier_fields or frame.columns[:6])
    ]
    missing = [name for name in frozen_fields if name not in frame.columns]
    if missing:
        raise WorkspaceError(f"Frozen field '{missing[0]}' does not exist in '{table}'.")
    size = int(payload.get("size") or 25)
    if size < 1:
        raise WorkspaceError("Evidence-aware sample size must be positive.")
    index = _document_index(workspace)
    candidates = []
    for row in frame.iter_rows(named=True):
        identifier_pairs = [
            (str(row.get(field)).strip(), normalized)
            for field in identifier_fields
            if (normalized := _normalized_identifier(row.get(field)))
        ]
        identifiers = list(dict.fromkeys(display for display, _ in identifier_pairs))
        normalized_identifiers = list(dict.fromkeys(value for _, value in identifier_pairs))
        matched = [
            entry
            for entry in index
            if normalized_identifiers
            and any(identifier in entry["normalized"] for identifier in normalized_identifiers)
        ]
        candidates.append({"row": row, "identifiers": identifiers, "documents": matched})
    covered = sorted(
        (item for item in candidates if item["documents"]),
        key=lambda item: (-len(item["documents"]), int(item["row"]["source_row"])),
    )
    uncovered = [item for item in candidates if not item["documents"]]
    random.Random(int(payload.get("seed") or 42)).shuffle(uncovered)
    selected = [*covered, *uncovered][: min(size, len(candidates))]
    required_types = _required_document_types(workspace, payload)
    source_hash = _sha1(workspace._table_signature(table))
    test = _base_test(workspace, payload, "vouching")
    test["spec"].update(
        {
            "direction": str(payload.get("direction") or "vouching"),
            "table": table,
            "table_sha1": source_hash,
            "sampling": {
                "method": "evidence_covered_first",
                "size": size,
                "seed": int(payload.get("seed") or 42),
            },
            "selection_basis": "evidence_covered_first",
            "assurance_scope": "targeted_evidence_only",
            "identifier_fields": identifier_fields,
            "frozen_fields": frozen_fields,
            "required_document_types": required_types,
            "population_rows": frame.height,
            "require_all_documents": bool(payload.get("require_all_documents", True)),
        }
    )
    requests = []
    image_only_count = 0
    for selected_item in selected:
        row = selected_item["row"]
        document_entries = selected_item["documents"]
        doc_ids = [entry["document"]["id"] for entry in document_entries]
        available_types = {kind for entry in document_entries for kind in entry["types"]}
        missing_types = [kind for kind in required_types if kind not in available_types]
        if not doc_ids and not missing_types:
            missing_types = ["supporting_evidence"]
        image_only = any(entry["image_only"] for entry in document_entries)
        image_only_count += int(image_only)
        identifiers = selected_item["identifiers"]
        label = " / ".join(identifiers) or f"{table} row {row['source_row']}"
        frozen = {field: _json_value(row.get(field)) for field in frozen_fields}
        item = _new_item({"label": label, "document_ids": doc_ids})
        item.update(
            source={"table": table, "source_row": row["source_row"], "source_sha1": source_hash},
            transaction_identifiers=identifiers,
            frozen=frozen,
            checks=[
                _normalize_check({"field": field, "expected": value, "method": "normalized"})
                for field, value in frozen.items()
            ],
            evidence_coverage={
                "document_ids": doc_ids,
                "available_document_types": sorted(available_types),
                "missing_document_types": missing_types,
                "image_only": image_only,
            },
            evidence_request_ids=[],
        )
        test["items"].append(item)
        if missing_types:
            request = _evidence_request(
                workspace,
                test=test,
                item=item,
                transaction_identifier=label,
                missing_types=missing_types,
            )
            item["evidence_request_ids"].append(request["id"])
            requests.append(request)
        if image_only:
            item.update(
                state="manual_review",
                runner_note="Image-only evidence requires OCR or auditor review.",
            )
    covered_count = sum(bool(item.get("document_ids")) for item in test["items"])
    test["spec"]["evidence_coverage"] = {
        "selected": len(test["items"]),
        "evidence_covered": covered_count,
        "evidence_requested": len(requests),
        "image_only": image_only_count,
    }
    test["status"] = (
        "blocked"
        if test["items"] and covered_count == 0
        else "review_required"
        if requests or image_only_count
        else "in_progress"
    )
    test["scope_limitations"] = (
        f"{counted(len(requests), 'selected item')} {verb(len(requests), 'requires', 'require')} additional evidence."
        if requests
        else ""
    )
    save_test(workspace, test)
    return {**test, "evidence_requests": requests}


def update_evidence_request(
    workspace: Workspace, request_id: str, *, status: str, note: str = ""
) -> dict:
    request = next(
        (item for item in workspace.evidence_requests if item.get("id") == request_id),
        None,
    )
    if request is None:
        raise WorkspaceError(f"Evidence request '{request_id}' not found.")
    if status not in {"open", "received", "cancelled"}:
        raise WorkspaceError("Unknown evidence-request status.")
    request["status"] = status
    request["auditor_note"] = str(note or "")
    request["updated"] = utcnow()
    test_id = str(request.get("document_test_id") or "")
    if status != "open" and test_id:
        has_open_request = any(
            item.get("document_test_id") == test_id and item.get("status") == "open"
            for item in workspace.evidence_requests
        )
        if not has_open_request:
            test = load_test(workspace, test_id)
            test["scope_limitations"] = ""
            write_test(workspace, test)
    workspace.save()
    return request


def build_attribute(workspace: Workspace, payload: dict) -> dict:
    built = {**payload, "kind": "attribute"}
    attributes = [
        {"name": str(value.get("name") or "Attribute"), "expected": str(value.get("expected") or "")}
        for value in (payload.get("attributes") or [])
    ]
    items = []
    default_item = {
        "label": "Attribute test item",
        "document_ids": [str(value) for value in (payload.get("document_ids") or [])],
    }
    for raw in payload.get("items") or [default_item]:
        items.append({**raw, "attributes": raw.get("attributes") or attributes})
    built["items"] = items
    return create_test(workspace, built)


def build_review(workspace: Workspace, payload: dict) -> dict:
    document_id = str(payload.get("document_id") or "")
    document = next((value for value in workspace.documents if value.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    selected = documents.preview(workspace, document_id, payload.get("pages"))
    items = []
    for page in selected["pages"]:
        excerpt = str(page.get("text") or "")[:400].strip()
        anchor = document_anchor(document, int(page["page"]), excerpt, generated_by="review-builder")
        items.append({
            "label": f"{document.get('title') or document_id}, page {page['page']}",
            "document_ids": [document_id], "page": page["page"],
            "review_kind": str(payload.get("review_kind") or "general"),
            "summary": "", "excerpt": excerpt, "evidence_refs": [anchor],
        })
    return create_test(workspace, {**payload, "kind": "review", "items": items})


def build_qa(workspace: Workspace, payload: dict) -> dict:
    document_ids = [str(value) for value in (payload.get("document_ids") or [])]
    known = {doc["id"] for doc in workspace.documents}
    missing = [value for value in document_ids if value not in known]
    if missing:
        raise WorkspaceError(f"Document '{missing[0]}' not found.")
    questions = [str(value).strip() for value in (payload.get("questions") or []) if str(value).strip()]
    if not questions:
        raise WorkspaceError("Add at least one question to a Q&A test.")
    return create_test(workspace, {
        **payload, "kind": "qa",
        "items": [{"label": question, "question": question, "document_ids": document_ids} for question in questions],
    })


def update_test(workspace: Workspace, test_id: str, changes: dict) -> dict:
    test = load_test(workspace, test_id)
    allowed = {
        "title", "status", "rcm_refs", "procedure_refs", "rcm_id", "spec",
        "objective", "criteria", "steps", "conclusion",
        "control_conclusion", "scope_limitations", "next_action",
    }
    if set(changes) - allowed:
        raise WorkspaceError("Unknown document-test field.")
    rcm_id = _validate_rcm_id(workspace, changes.get("rcm_id", test.get("rcm_id")))
    rcm_refs = [str(ref) for ref in changes.get("rcm_refs", test["rcm_refs"])]
    procedure_refs = [str(ref) for ref in changes.get("procedure_refs", test["procedure_refs"])]
    _validate_links(workspace, rcm_refs, procedure_refs)
    if rcm_id and rcm_id not in rcm_refs:
        rcm_refs.append(rcm_id)
    if not rcm_id:
        rcm_refs = [ref for ref in rcm_refs if ref != test.get("rcm_id")]
    if "title" in changes:
        test["title"] = str(changes["title"] or "").strip()
        if not test["title"]:
            raise WorkspaceError("Document-test title is required.")
    if "status" in changes:
        status = str(changes["status"])
        if status not in TEST_STATUSES:
            raise WorkspaceError("Unknown document-test status.")
        test["status"] = status
    if "control_conclusion" in changes:
        conclusion = str(changes["control_conclusion"] or "no_conclusion")
        if conclusion not in CONTROL_CONCLUSIONS:
            raise WorkspaceError("Unknown control conclusion.")
        # An incomplete evidence base no longer refuses the write. Concluding
        # over open items is the auditor's call to make; what the file needs is
        # that the call be disclosed, not prevented.
        if conclusion != "no_conclusion" and (blocked := conclusion_block(test)):
            raise WorkspaceError(blocked)
        test["control_conclusion"] = conclusion
        test["control_conclusion_source"] = "auditor"
    if "steps" in changes:
        test["steps"] = _normalize_steps(changes["steps"])
    for key in ("objective", "criteria", "conclusion", "scope_limitations", "next_action"):
        if key in changes:
            test[key] = str(changes[key] or "")
    if "conclusion" in changes:
        test["conclusion_source"] = "auditor"
    if "control_conclusion" in changes:
        # After the auditor's own scope text has been applied, so an edit in the
        # same request is preserved and the disclosure is appended below it.
        record_conclusion_override(test)
    test["rcm_refs"], test["procedure_refs"] = rcm_refs, procedure_refs
    test["rcm_id"] = rcm_id
    if "spec" in changes:
        test["spec"] = {**test["spec"], **dict(changes["spec"] or {})}
    save_test(workspace, test)
    return test


def remove_test(workspace: Workspace, test_id: str) -> None:
    load_test(workspace, test_id)
    _test_path(workspace, test_id).unlink()
    ref = f"doctest:{test_id}"
    for item in [*workspace.rcm, *workspace.work_program]:
        item["test_refs"] = [value for value in item.get("test_refs", []) if value != ref]
    workspace.save()


def _item(test: dict, item_id: str) -> dict:
    item = next((value for value in test["items"] if value.get("id") == item_id), None)
    if item is None:
        raise WorkspaceError(f"Document-test item '{item_id}' not found.")
    return item


def attach_document(workspace: Workspace, test_id: str, item_id: str, document_id: str) -> dict:
    test = load_test(workspace, test_id)
    if is_cycle_test(test):
        raise WorkspaceError(
            "Cycle evidence is attached to a typed role binding, not a flat document list."
        )
    item = _item(test, item_id)
    document = next((value for value in workspace.documents if value.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    if document_id not in item["document_ids"]:
        item["document_ids"].append(document_id)
    invalidate_evaluation(test, item, "New evidence attached; re-run the item.")
    request_ids = {str(value) for value in item.get("evidence_request_ids") or []}
    for request in workspace.evidence_requests:
        if str(request.get("id")) in request_ids and request.get("status") == "open":
            request["status"] = "received"
            request["updated"] = utcnow()
    if all(test_item.get("document_ids") for test_item in test.get("items") or []):
        test["status"] = "in_progress"
        test["scope_limitations"] = ""
    result = save_test(workspace, test)
    return result


def detach_document(workspace: Workspace, test_id: str, item_id: str, document_id: str) -> dict:
    test = load_test(workspace, test_id)
    if is_cycle_test(test):
        raise WorkspaceError(
            "Cycle evidence is detached from a typed role binding, not a flat document list."
        )
    item = _item(test, item_id)
    item["document_ids"] = [value for value in item["document_ids"] if value != document_id]
    invalidate_evaluation(test, item, "Evidence detached; re-run the item.")
    return save_test(workspace, test)


def update_comparisons(workspace: Workspace, test_id: str, item_id: str, checks: list[dict]) -> dict:
    test = load_test(workspace, test_id)
    if test["kind"] != "vouching":
        raise WorkspaceError("Comparison settings apply only to vouching/tracing tests.")
    item = _item(test, item_id)
    item["checks"] = [_normalize_check(check) for check in checks]
    invalidate_evaluation(test, item, "Matching rules changed; re-run the item.")
    return save_test(workspace, test)


def append_cycle_assertions(
    workspace: Workspace,
    test_id: str,
    *,
    expected_test_sha1: str,
    assertions: list[dict],
    placement: object = None,
    actor: str = "auditor",
) -> dict:
    """Atomically upsert canonical Cycle-vouch assertions under two CAS gates."""

    from .workspace_transactions import ParentConflict

    expected = str(expected_test_sha1 or "").strip()
    if not expected:
        raise WorkspaceError("expected_test_sha1 is required.")
    with workspace_write_lock(workspace.root):
        test = load_test(workspace, test_id)
        current_sha1 = str(test.get("sha1") or "")
        if current_sha1 != expected:
            raise ParentConflict(
                f"doctest:{test_id}", expected, current_sha1, workspace.revision
            )
        if not is_cycle_test(test):
            raise WorkspaceError(
                "Assertion columns can be authored only on cycle_vouch tests."
            )
        try:
            mutated, mutation = cycle_vouching.mutate_cycle_assertions(
                workspace,
                test,
                assertions,
                placement=placement,
                actor=actor,
            )
        except cycle_vouching.CycleSchemaError as error:
            raise WorkspaceError(str(error)) from error
        if mutation["changed"]:
            mutated = save_test(workspace, mutated)
        else:
            mutated = test
        mutation["before_test_sha1"] = current_sha1
        mutation["after_test_sha1"] = str(mutated.get("sha1") or current_sha1)
        return {"test": mutated, "mutation": mutation}


# The calls an auditor may make by hand. These write the disposition and never
# the evaluation: disagreeing with the runner records the disagreement, it does
# not rewrite what the runner found. ``needs_review`` is how an auditor parks an
# item for a second pair of eyes — distinct from the runner's ``inconclusive``,
# which says the machine could not settle it.
MANUAL_SIGNOFF_STATES = frozenset({"confirmed", "exception", "needs_review", "pending"})
# Cycle dispositions stay binary; parking is an item-first affordance.
CYCLE_SIGNOFF_STATES = frozenset({"confirmed", "exception", "pending"})


def update_item(
    workspace: Workspace,
    test_id: str,
    item_id: str,
    changes: dict,
    *,
    runner_note: str | None = None,
) -> dict:
    test = load_test(workspace, test_id)
    if is_cycle_test(test):
        if set(changes) != {"state"}:
            raise WorkspaceError(
                "Cycle items accept only the typed auditor disposition mutation."
            )
        state = str(changes.get("state") or "")
        if state not in CYCLE_SIGNOFF_STATES:
            raise WorkspaceError(
                "An auditor may only set a cycle disposition to confirmed, "
                "exception, or pending."
            )
        # Re-materialize in memory before accepting sign-off.  A changed table,
        # extraction, record hash, or role closure makes the prior result stale
        # and must never be confirmed against yesterday's inputs.
        test["items"] = cycle_vouching.materialize_cycle_items(workspace, test)
        item = _item(test, item_id)
        evaluation = item.get("evaluation") or {}
        if state != "pending" and not item_execution_current(test, item):
            raise WorkspaceError(
                "A cycle item must have a current deterministic evaluation before disposition."
            )
        if state == "pending":
            item["disposition"] = {
                "state": "pending",
                "evaluated_definition_sha1": None,
                "stale": False,
            }
            item["runner_note"] = "Auditor disposition cleared; evaluation retained."
        else:
            item["disposition"] = {
                "state": state,
                "evaluated_definition_sha1": evaluation.get("definition_sha1"),
                "stale": False,
            }
            item["runner_note"] = "Auditor sign-off."
        test["status"] = (
            "completed"
            if test.get("items")
            and all(item_disposition_current(test, value) for value in test["items"])
            else "review_required"
        )
        return save_test(workspace, test)
    item = _item(test, item_id)
    allowed = {
        "attributes",
        "summary", "excerpt", "response", "citations", "state", "disposition_note",
    }
    if set(changes) - allowed:
        raise WorkspaceError("Unknown document-test item field.")
    if "disposition_note" in changes and "state" not in changes:
        raise WorkspaceError("A disposition note accompanies an auditor's call.")
    for key, value in changes.items():
        if key == "attributes":
            item[key] = [_normalize_attribute(entry) for entry in (value or [])]
        elif key == "citations":
            item[key] = normalize_many(value or [], require_hash=True)
        elif key in {"state", "disposition_note"}:
            continue
        else:
            item[key] = value
    if "state" in changes:
        state = str(changes["state"] or "")
        if state not in MANUAL_SIGNOFF_STATES:
            raise WorkspaceError(
                "An auditor may only set a document-test item to confirmed, "
                "exception, needs_review, or pending."
            )
        note = str(changes.get("disposition_note") or "").strip()
        record_disposition(workspace, test, item, state, note=note)
        refresh_test_status(test)
    if runner_note is not None:
        item["runner_note"] = runner_note
    return save_test(workspace, test)


def update_dispositions(workspace: Workspace, entries: list[dict]) -> dict:
    """Record one auditor call across many items, grouped by test.

    A 25-item sample where the runner got 23 right is the ordinary case, and
    signing those off one request at a time is the slowest part of the worklist.
    Each test is loaded and saved once so the revision advances per test rather
    than per item.
    """

    by_test: dict[str, list[dict]] = {}
    for entry in entries or []:
        test_id = str(entry.get("test_id") or "")
        item_id = str(entry.get("item_id") or "")
        if not test_id or not item_id:
            raise WorkspaceError("Each disposition needs a test and an item.")
        by_test.setdefault(test_id, []).append(entry)
    updated: list[dict] = []
    for test_id, group in by_test.items():
        test = load_test(workspace, test_id)
        if is_cycle_test(test):
            raise WorkspaceError(
                "Cycle dispositions are recorded through the cycle grid."
            )
        for entry in group:
            state = str(entry.get("state") or "")
            if state not in MANUAL_SIGNOFF_STATES:
                raise WorkspaceError(
                    "An auditor may only set a document-test item to confirmed, "
                    "exception, needs_review, or pending."
                )
            record_disposition(
                workspace,
                test,
                _item(test, str(entry["item_id"])),
                state,
                note=str(entry.get("disposition_note") or "").strip(),
            )
        refresh_test_status(test)
        updated.append(save_test(workspace, test))
    return {"tests": updated, "items": sum(len(group) for group in by_test.values())}


def _refresh_agent_conclusion(test: dict) -> None:
    """Roll worker-authored item conclusions into the completed test result.

    The worker evaluates one item/document pair, while the durable result is a
    Document Test. This deterministic roll-up needs no extra model turn and
    never overwrites an explicit auditor conclusion.
    """
    # Gated on the run having produced every answer, not on the test being
    # signed off: this rolls up what the worker concluded, which is complete the
    # moment the last item is evaluated, whether or not an auditor has looked.
    if not execution_settled(test) or test.get("conclusion_source") == "auditor":
        return
    answer_key = "qa_answers" if test.get("kind") == "qa" else "llm_answers"
    item_conclusions: list[tuple[str, str]] = []
    for item in test.get("items") or []:
        answers = item.get(answer_key) or {}
        conclusions = [
            str((answers.get(document_id) or {}).get("conclusion") or "").strip()
            for document_id in item.get("document_ids") or []
        ]
        conclusions = [conclusion for conclusion in conclusions if conclusion]
        if not conclusions:
            return
        item_conclusions.append(
            (str(item.get("label") or "Test item"), " ".join(conclusions))
        )
    if not item_conclusions:
        return
    test["conclusion"] = (
        item_conclusions[0][1]
        if len(item_conclusions) == 1
        else "\n".join(
            f"{label}: {conclusion}" for label, conclusion in item_conclusions
        )
    )
    test["conclusion_source"] = "agent"


def _refresh_agent_control_conclusion(test: dict) -> None:
    """Conservatively combine every settled worker control conclusion."""
    if (
        not execution_settled(test)
        or test.get("control_conclusion_source") == "auditor"
    ):
        return
    answer_key = "qa_answers" if test.get("kind") == "qa" else "llm_answers"
    conclusions: list[str] = []
    for item in test.get("items") or []:
        answers = item.get(answer_key) or {}
        for document_id in item.get("document_ids") or []:
            conclusion = str(
                (answers.get(document_id) or {}).get("control_conclusion")
                or "no_conclusion"
            )
            if conclusion not in CONTROL_CONCLUSIONS:
                return
            conclusions.append(conclusion)
    if not conclusions:
        return
    if "ineffective" in conclusions:
        result = "ineffective"
    elif "partially_effective" in conclusions:
        result = "partially_effective"
    elif "no_conclusion" in conclusions:
        result = "no_conclusion"
    elif all(value == "not_applicable" for value in conclusions):
        result = "not_applicable"
    elif all(value == "effective" for value in conclusions):
        result = "effective"
    else:
        result = "no_conclusion"
    test["control_conclusion"] = result
    test["control_conclusion_source"] = "agent"


def settle_llm_assessment(
    workspace: Workspace, test_id: str, item_id: str,
) -> dict | None:
    """Apply the worker's outcome to a complete LLM assessment.

    Multiple attached documents settle conservatively: any exception wins,
    otherwise any manual-check outcome wins, otherwise every answer must be
    accepted. Incomplete results remain available for an auditor.
    """
    test = load_test(workspace, test_id)
    item = _item(test, item_id)
    answer_key = "qa_answers" if test.get("kind") == "qa" else "llm_answers"
    answers = item.get(answer_key) or {}
    document_ids = list(item.get("document_ids") or [])
    if (
        str((item.get("evaluation") or {}).get("state") or "not_run") != "agent_checked"
        or not document_ids
        or any(document_id not in answers for document_id in document_ids)
    ):
        return None
    outcomes = {
        str(answers[document_id].get("outcome") or "")
        for document_id in document_ids
    }
    if "exception" in outcomes:
        assessment_outcome = "exception"
    elif "needs_manual_check" in outcomes or outcomes != {"accepted"}:
        assessment_outcome = "needs_manual_check"
    else:
        assessment_outcome = "accepted"
    record_evaluation(
        workspace,
        test,
        item,
        "failed" if assessment_outcome == "exception"
        else "inconclusive" if assessment_outcome == "needs_manual_check"
        else "passed",
        f"Model assessment outcome: {assessment_outcome}.",
    )
    refresh_test_status(test)
    _refresh_agent_conclusion(test)
    _refresh_agent_control_conclusion(test)
    return save_test(workspace, test)


def llm_assessment_outcome(
    workspace: Workspace,
    test_id: str,
    item_id: str,
    document_id: str,
) -> str:
    """Return the persisted worker outcome for one item/document assessment."""
    test = load_test(workspace, test_id)
    item = _item(test, item_id)
    answer_key = "qa_answers" if test.get("kind") == "qa" else "llm_answers"
    answer = (item.get(answer_key) or {}).get(document_id) or {}
    return str(answer.get("outcome") or "needs_manual_check")


def normalize_value(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value if value is not None else "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def _number(value: object) -> float | None:
    try:
        cleaned = re.sub(r"[^0-9.()\-]", "", str(value or ""))
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = f"-{cleaned[1:-1]}"
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _date(value: object) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def compare_values(expected: object, found: object, method: str = "normalized", tolerance=None) -> dict:
    method = str(method or "normalized")
    if method not in METHODS:
        raise WorkspaceError(f"Unknown comparison method '{method}'.")
    result = {
        "expected": _json_value(expected), "found": _json_value(found), "method": method,
        "normalization": None, "tolerance": tolerance, "result": "mismatch", "similarity": None,
    }
    if found in (None, ""):
        result["result"] = "missing"
        return result
    if method == "exact":
        result["result"] = "match" if str(expected) == str(found) else "mismatch"
    elif method == "normalized":
        left, right = normalize_value(expected), normalize_value(found)
        result["normalization"] = {"expected": left, "found": right}
        result["result"] = "match" if left == right else "mismatch"
    elif method == "fuzzy":
        left, right = normalize_value(expected), normalize_value(found)
        score = SequenceMatcher(None, left, right).ratio()
        threshold = float(tolerance if tolerance not in (None, "") else 0.85)
        if threshold > 1:
            threshold /= 100.0
        result.update(normalization={"expected": left, "found": right}, tolerance=threshold, similarity=round(score, 4), result="match" if score >= threshold else "mismatch")
    elif method == "numeric_tolerance":
        left, right = _number(expected), _number(found)
        if left is None or right is None:
            result["result"] = "invalid"
        else:
            config = tolerance if isinstance(tolerance, dict) else {"absolute": float(tolerance or 0)}
            absolute = float(config.get("absolute") or 0)
            percent = float(config.get("percent") or 0)
            allowed = max(absolute, abs(left) * percent / 100.0)
            result.update(normalization={"expected": left, "found": right}, tolerance={"absolute": absolute, "percent": percent, "allowed": allowed}, result="match" if abs(left - right) <= allowed else "mismatch")
    else:
        left, right = _date(expected), _date(found)
        if left is None or right is None:
            result["result"] = "invalid"
        else:
            days = int((tolerance or {}).get("days", 0) if isinstance(tolerance, dict) else tolerance or 0)
            result.update(normalization={"expected": left.isoformat(), "found": right.isoformat()}, tolerance={"days": days}, result="match" if abs((left - right).days) <= days else "mismatch")
    return result


def _candidate(expected: object, method: str, tolerance, text: str) -> tuple[str, str] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    expected_text = str(expected if expected is not None else "")
    if method == "exact":
        line = next((line for line in lines if expected_text in line), None)
        return (expected_text, line) if line is not None else None
    if method == "normalized":
        needle = normalize_value(expected)
        line = next((line for line in lines if needle and needle in normalize_value(line)), None)
        return (expected_text, line) if line is not None else None
    if method == "fuzzy":
        needle = normalize_value(expected)
        scored = [(SequenceMatcher(None, needle, normalize_value(line)).ratio(), line) for line in lines]
        score, line = max(scored, default=(0.0, ""))
        threshold = float(tolerance if tolerance not in (None, "") else 0.85)
        threshold = threshold / 100 if threshold > 1 else threshold
        return (line, line) if score >= threshold else None
    if method == "numeric_tolerance":
        for token in re.findall(r"[-(]?[\d][\d,]*(?:\.\d+)?\)?", text):
            if compare_values(expected, token, method, tolerance)["result"] == "match":
                line = next((line for line in lines if token in line), token)
                return token, line
        return None
    for token in re.findall(r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})\b", text):
        if compare_values(expected, token, method, tolerance)["result"] == "match":
            line = next((line for line in lines if token in line), token)
            return token, line
    return None


def _document_conflicts(workspace: Workspace, document_ids: list[str]) -> dict:
    docs = [next((doc for doc in workspace.documents if doc.get("id") == doc_id), None) for doc_id in document_ids]
    docs = [doc for doc in docs if doc is not None]
    hashes: dict[str, list[str]] = {}
    for doc in docs:
        hashes.setdefault(doc.get("sha1") or "", []).append(doc["id"])
    duplicates = [ids for sha1, ids in hashes.items() if sha1 and len(ids) > 1]
    return {"duplicate_documents": duplicates}


def run_item(
    workspace: Workspace, test_id: str, item_id: str, *,
    run_id: str | None = None, model_adapter=None,
) -> dict:
    test = load_test(workspace, test_id)
    item = _item(test, item_id)
    if test["kind"] == "qa":
        if not item.get("document_ids") or not str(item.get("question") or "").strip():
            record_evaluation(
                workspace, test, item, "inconclusive",
                "Attach evidence and record a question before running this item.",
            )
            refresh_test_status(test)
            save_test(workspace, test)
            return item
        try:
            answers, citations = [], []
            for doc_id in item["document_ids"]:
                answer = documents.document_chat(
                    workspace, doc_id, item["question"], item.get("pages"), run_id=run_id,
                    model_adapter=model_adapter,
                )
                answers.append(str(answer.get("answer") or ""))
                citations.extend(answer.get("citations") or [])
            item.update(
                response="\n\n".join(value for value in answers if value),
                citations=citations, evidence_refs=citations,
            )
            record_evaluation(
                workspace, test, item, "passed",
                "Cited answer generated from the attached pages.",
            )
        except Exception as error:
            record_evaluation(
                workspace, test, item, "inconclusive", f"Manual fallback: {error}"
            )
        refresh_test_status(test)
        save_test(workspace, test)
        return item
    if test["kind"] != "vouching":
        record_evaluation(
            workspace, test, item, "inconclusive",
            "This test kind requires auditor input in the current runner.",
        )
        refresh_test_status(test)
        save_test(workspace, test)
        return item
    if not item.get("document_ids"):
        record_evaluation(
            workspace, test, item, "inconclusive",
            "Attach at least one document before running this item.",
        )
        refresh_test_status(test)
        save_test(workspace, test)
        return item
    conflicts = _document_conflicts(workspace, item["document_ids"])
    item["document_conflicts"] = conflicts
    require_all = bool(test.get("spec", {}).get("require_all_documents", True))
    all_anchors = []
    any_usable = False
    for check in item.get("checks") or []:
        comparisons = []
        anchors = []
        for doc_id in item["document_ids"]:
            doc = next((value for value in workspace.documents if value.get("id") == doc_id), None)
            if doc is None:
                comparisons.append({"document_id": doc_id, "result": "missing_document"})
                continue
            preview = documents.preview(workspace, doc_id)
            best = None
            for page in preview.get("pages") or []:
                candidate = _candidate(check.get("expected"), check.get("method", "normalized"), check.get("tolerance"), page.get("text") or "")
                if candidate is None:
                    continue
                found, excerpt = candidate
                outcome = compare_values(check.get("expected"), found, check.get("method", "normalized"), check.get("tolerance"))
                excerpt = excerpt[:400]
                anchor = document_anchor(doc, int(page["page"]), excerpt, generated_by=run_id or "local-match")
                best = {**outcome, "document_id": doc_id, "source_sha1": doc.get("sha1"), "page": page["page"], "evidence": anchor}
                anchors.append(anchor)
                any_usable = True
                break
            if best is None:
                best = {
                    **compare_values(check.get("expected"), None, check.get("method", "normalized"), check.get("tolerance")),
                    "document_id": doc_id, "source_sha1": doc.get("sha1"), "page": None,
                }
            comparisons.append(best)
        results = [entry.get("result") for entry in comparisons]
        matched = bool(results) and (all(value == "match" for value in results) if require_all else any(value == "match" for value in results))
        check["comparisons"] = comparisons
        check["evidence_refs"] = anchors
        check["found"] = next((entry.get("found") for entry in comparisons if entry.get("result") == "match"), None)
        check["verdict"] = "match" if matched else "mismatch" if any(value not in {"missing", "missing_document"} for value in results) else "missing"
        all_anchors.extend(anchors)
    item["evidence_refs"] = all_anchors
    has_conflict = bool(conflicts["duplicate_documents"])
    if not any_usable or has_conflict:
        record_evaluation(
            workspace, test, item, "inconclusive",
            "Manual check required because evidence could not be matched or "
            "duplicate documents are attached.",
        )
    else:
        record_evaluation(
            workspace, test, item,
            "failed"
            if any(
                check.get("verdict") in {"mismatch", "missing", "invalid"}
                for check in item.get("checks") or []
            )
            else "passed",
            "Deterministic local comparison completed.",
        )
    refresh_test_status(test)
    save_test(workspace, test)
    return item


def _llm_control_conclusion(answer: dict) -> str:
    """Validate the fixed enum before an LLM assessment becomes durable."""
    conclusion = str(answer.get("control_conclusion") or "no_conclusion")
    if conclusion not in CONTROL_CONCLUSIONS:
        raise WorkspaceError("Unknown control conclusion.")
    return conclusion


def commit_qa_answer(
    workspace: Workspace,
    test_id: str,
    item_id: str,
    document_id: str,
    answer: dict,
) -> dict:
    """Merge one immutable item/document Q&A candidate in document order."""
    test = load_test(workspace, test_id)
    if test.get("kind") != "qa":
        raise WorkspaceError("Q&A answers can only be committed to a Q&A Document Test.")
    item = _item(test, item_id)
    if document_id not in item.get("document_ids", []):
        raise WorkspaceError(
            f"Document '{document_id}' is not attached to Document Test item '{item_id}'."
        )
    candidate = {
        "answer": str(answer.get("answer") or ""),
        "conclusion": str(answer.get("conclusion") or answer.get("answer") or ""),
        "control_conclusion": _llm_control_conclusion(answer),
        "outcome": str(answer.get("outcome") or "needs_manual_check"),
        "citations": normalize_many(answer.get("citations") or []),
    }
    answers = item.setdefault("qa_answers", {})
    answers[document_id] = candidate
    ordered = [
        answers[value]
        for value in item.get("document_ids") or []
        if value in answers
    ]
    item.update(
        response="\n\n".join(value["answer"] for value in ordered if value["answer"]),
        citations=[citation for value in ordered for citation in value["citations"]],
        evidence_refs=[citation for value in ordered for citation in value["citations"]],
    )
    record_evaluation(
        workspace, test, item, "agent_checked",
        "Cited answers were generated from the attached pages in stable document order.",
    )
    save_test(workspace, test)
    settle_llm_assessment(workspace, test_id, item_id)
    return _item(load_test(workspace, test_id), item_id)


def commit_llm_assessment(
    workspace: Workspace, test_id: str, item_id: str, document_id: str, answer: dict,
) -> dict:
    """Persist one cited LLM assessment for a Q&A, attribute, or review item."""
    test = load_test(workspace, test_id)
    if test.get("kind") not in {"qa", "attribute", "review"}:
        raise WorkspaceError("This Document Test kind does not support an LLM assessment.")
    if test.get("kind") == "qa":
        return commit_qa_answer(workspace, test_id, item_id, document_id, answer)
    item = _item(test, item_id)
    if document_id not in item.get("document_ids", []):
        raise WorkspaceError(f"Document '{document_id}' is not attached to Document Test item '{item_id}'.")
    answers = item.setdefault("llm_answers", {})
    answers[document_id] = {
        "answer": str(answer.get("answer") or ""),
        "conclusion": str(answer.get("conclusion") or answer.get("answer") or ""),
        "control_conclusion": _llm_control_conclusion(answer),
        "outcome": str(answer.get("outcome") or "needs_manual_check"),
        "citations": normalize_many(answer.get("citations") or []),
    }
    ordered = [answers[value] for value in item.get("document_ids") or [] if value in answers]
    item.update(
        response="\n\n".join(value["answer"] for value in ordered if value["answer"]),
        citations=[citation for value in ordered for citation in value["citations"]],
        evidence_refs=[citation for value in ordered for citation in value["citations"]],
    )
    record_evaluation(
        workspace, test, item, "agent_checked",
        "Cited LLM assessment generated from the attached pages.",
    )
    save_test(workspace, test)
    settle_llm_assessment(workspace, test_id, item_id)
    return _item(load_test(workspace, test_id), item_id)


def execution_issues(test: dict) -> list[str]:
    """Return deterministic blockers to attempting a document test.

    These issues identify definitions
    that cannot perform even that bounded local work (the failure mode that
    previously turned description-only items into nominally successful runs).
    """
    kind = str(test.get("kind") or "")
    items = list(test.get("items") or [])
    if kind == "cycle_vouch":
        try:
            cycle_vouching.validate_cycle_test(test)
            for item in items:
                cycle_vouching.normalize_cycle_item(
                    item, registry_ref=test["registry"]
                )
        except cycle_vouching.CycleSchemaError as error:
            return [str(error)]
        return []
    if not items:
        return ["the test has no items"]
    issues = []
    for index, item in enumerate(items, start=1):
        prefix = f"item {index}"
        if not item.get("document_ids"):
            issues.append(f"{prefix} has no attached document")
        if kind == "vouching" and not item.get("checks"):
            issues.append(f"{prefix} has no comparison checks")
        elif kind == "attribute" and not item.get("attributes"):
            issues.append(f"{prefix} has no attributes")
        elif kind == "review" and not (
            item.get("page") not in (None, "")
            or str(item.get("excerpt") or "").strip()
            or str(item.get("summary") or "").strip()
        ):
            issues.append(f"{prefix} has no page, excerpt, or review summary")
        elif kind == "qa" and not str(item.get("question") or "").strip():
            issues.append(f"{prefix} has no question")
    return issues


def evidence_blocked(test: dict) -> bool:
    """Whether a valid worklist is intentionally waiting on requested evidence."""
    if test.get("status") not in {"blocked", "review_required"}:
        return False
    return bool(
        str(test.get("scope_limitations") or "").strip()
        or any(
            item.get("evidence_request_ids")
            for item in test.get("items") or []
        )
    )


def result_rollup(test: dict) -> dict:
    items = test.get("items") or []
    if is_cycle_test(test):
        return cycle_vouching.result_rollup(test)
    checks = [check for item in items for check in (item.get("checks") or [])]
    failed_items = sum(
        any(check.get("verdict") == "mismatch" for check in item.get("checks") or [])
        for item in items
    )
    incomplete_items = sum(
        any(check.get("verdict") in {"missing", "invalid"} for check in item.get("checks") or [])
        for item in items
    )
    scope = assurance_scope(test)
    # An item-first test concludes on resolved items. The runner's own verdict
    # resolves one unless the auditor overrode it; what blocks a conclusion is
    # an item nobody settled — parked, inconclusive, or signed against evidence
    # that has since moved. (Cycle tests keep the stricter rule that every item
    # carries an explicit, current auditor disposition.)
    dispositions_current = bool(items) and all(
        project_item_state(item) in {"confirmed", "exception"}
        and not (item.get("disposition") or {}).get("stale")
        for item in items
    )
    executions_current = bool(items) and all(
        item_execution_current(test, item) for item in items
    )
    population_scope_eligible = (
        scope == "sampled_population" if test.get("kind") == "vouching" else True
    )
    conclusion_eligible = bool(
        population_scope_eligible
        and executions_current
        and dispositions_current
        and not incomplete_items
    )
    return {
        "items": len(items),
        "tested_items": sum(item_execution_current(test, item) for item in items),
        "failed_items": failed_items,
        "incomplete_items": incomplete_items,
        "assertion_mismatches": sum(
            check.get("verdict") == "mismatch" for check in checks
        ),
        "matched": sum(check.get("verdict") == "match" for check in checks),
        "mismatched": sum(check.get("verdict") in {"mismatch", "missing", "invalid"} for check in checks),
        "confirmed": sum(item.get("state") == "confirmed" for item in items),
        "exceptions": sum(item.get("state") == "exception" for item in items),
        "open_exceptions": sum(item.get("state") == "exception" for item in items),
        "manual_review": sum(item.get("state") == "manual_review" for item in items),
        "pending": sum(item.get("state") in {"pending", "agent_checked"} for item in items),
        # The two sides behind that joint reading, so a caller can tell "the
        # runner failed it" from "the auditor called it an exception".
        "evaluation_counts": {
            state: sum(
                str((item.get("evaluation") or {}).get("state") or "not_run") == state
                for item in items
            )
            for state in sorted(EVALUATION_STATES)
        },
        "disposition_counts": {
            state: sum(
                str((item.get("disposition") or {}).get("state") or "pending") == state
                for item in items
            )
            for state in sorted(DISPOSITION_STATES)
        },
        "stale_dispositions": sum(
            bool((item.get("disposition") or {}).get("stale")) for item in items
        ),
        "pending_dispositions": sum(
            item_disposition_pending(test, item) for item in items
        ),
        "assurance_scope": scope,
        "assurance_label": (
            "Targeted evidence - not a sample"
            if scope == "targeted_evidence_only"
            else "Sampled population"
            if scope == "sampled_population"
            else None
        ),
        "conclusion_eligible": conclusion_eligible,
        # `conclusion_eligible` still means "clean": every item resolved and
        # every check usable. Reporting the conclusion is a weaker test — an
        # auditor may conclude over open items, and that conclusion has to reach
        # the RCM rollup, the working paper, and the report, or overriding would
        # silently achieve nothing. What travels with it is the disclosure.
        "conclusion_disclosed": bool(test.get("conclusion_override")),
        "unresolved_items": unresolved_items(test),
        "control_conclusion": (
            str(test.get("control_conclusion") or "no_conclusion")
            if not conclusion_block(test)
            else "no_conclusion"
        ),
    }


def assurance_scope(test: dict) -> str | None:
    """Return structural population assurance without trusting display metadata."""

    if is_cycle_test(test):
        selection = ((test.get("definition") or {}).get("population") or {}).get(
            "selection"
        ) or {}
        return cycle_vouching.assurance_scope_for(selection)
    if test.get("kind") != "vouching":
        return None
    method = str(
        ((test.get("spec") or {}).get("sampling") or {}).get("method") or ""
    )
    if method == "evidence_covered_first":
        return "targeted_evidence_only"
    if method in {"random", "interval", "stratified"}:
        return "sampled_population"
    return None


# The auto-written half of ``scope_limitations``. Everything above the marker is
# the auditor's own text and is never touched; the block below it is rewritten
# from the current item state each time a conclusion is saved.
_OVERRIDE_MARKER = "[Concluded over unresolved items]"
_UNRESOLVED_READING = {
    "manual_review": "unresolved",
    "pending": "not run",
    "agent_checked": "still running",
}


def unresolved_items(test: dict) -> list[dict]:
    """Items that carry no settled reading, with why each one is open."""

    if is_cycle_test(test):
        return []
    open_items = []
    for item in test.get("items") or []:
        state = project_item_state(item)
        disposition = item.get("disposition") or {}
        evaluation = item.get("evaluation") or {}
        if state in {"confirmed", "exception"} and not disposition.get("stale"):
            continue
        if disposition.get("stale"):
            reason = "signed off against evidence that has since changed"
        elif str(disposition.get("state") or "pending") == "needs_review":
            reason = "parked for review"
        else:
            reason = (
                f"runner: {evaluation.get('state') or 'not_run'}"
                if state == "manual_review"
                else _UNRESOLVED_READING.get(state, state)
            )
        open_items.append({
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or item.get("id") or "Test item"),
            "reason": reason,
        })
    return open_items


def incomplete_checks(test: dict) -> int:
    return sum(
        any(
            check.get("verdict") in {"missing", "invalid"}
            for check in item.get("checks") or []
        )
        for item in test.get("items") or []
    )


def conclusion_block(test: dict) -> str:
    """Why this test structurally cannot carry a conclusion, or an empty string.

    Only two things are genuinely not the auditor's call: projecting a
    population conclusion from evidence that was not sampled for it, and
    concluding on a test that has not run. An incomplete evidence base is a
    different matter — that is a judgment the auditor is entitled to make and
    disclose, which is what :func:`record_conclusion_override` writes down.
    """

    if assurance_scope(test) == "targeted_evidence_only":
        return "Targeted evidence cannot support a population control conclusion."
    if is_cycle_test(test):
        return (
            ""
            if result_rollup(test).get("conclusion_eligible")
            else "Complete deterministic evaluation and current auditor "
            "disposition are required before recording a control conclusion."
        )
    items = test.get("items") or []
    if not items or not all(item_execution_current(test, item) for item in items):
        return "Run every item before recording a control conclusion."
    return ""


def record_conclusion_override(test: dict) -> None:
    """Keep ``scope_limitations`` telling the truth about what was still open.

    Rewritten on every conclusion save, so resolving the items later clears the
    disclosure rather than leaving a stale one on the file.
    """

    existing = str(test.get("scope_limitations") or "")
    auditor_text = existing.split(_OVERRIDE_MARKER)[0].rstrip()
    open_items = unresolved_items(test)
    incomplete = incomplete_checks(test)
    concluded = str(test.get("control_conclusion") or "no_conclusion") != "no_conclusion"
    if not concluded or (not open_items and not incomplete):
        test["scope_limitations"] = auditor_text
        test.pop("conclusion_override", None)
        return
    lines = [_OVERRIDE_MARKER]
    if open_items:
        total = len(test.get("items") or [])
        lines.append(
            f"Concluded with {len(open_items)} of {counted(total, 'item')} unresolved:"
        )
        lines.extend(f"- {item['label']} ({item['reason']})" for item in open_items)
    if incomplete:
        lines.append(
            f"{counted(incomplete, 'item')} {verb(incomplete, 'carries', 'carry')} "
            "a check that returned no usable result."
        )
    test["scope_limitations"] = "\n".join(
        ([auditor_text, ""] if auditor_text else []) + lines
    )
    test["conclusion_override"] = {
        "unresolved_items": open_items,
        "incomplete_check_items": incomplete,
        "recorded_at": utcnow(),
    }


def conclusion_eligible(test: dict) -> bool:
    """Whether this exact current test may support a population conclusion."""

    return bool(result_rollup(test).get("conclusion_eligible"))


def meta_payload() -> dict:
    """The closed vocabularies the create form binds to.

    Exposing them keeps the UI from asking auditors to type comma-separated
    document types and column names it already knows.
    """
    return {
        "kinds": sorted(KINDS),
        "directions": sorted(DIRECTIONS),
        "document_types": sorted(_DOCUMENT_TYPE_ALIASES),
        "comparison_methods": sorted(METHODS),
        "cycle_vouch": cycle_vouching.metadata(),
    }


SUMMARY_CLASSES = ("exception", "needs_review", "awaiting_evidence", "confirmed", "not_run")
_SUMMARY_RANK = {name: index for index, name in enumerate(SUMMARY_CLASSES)}


def _item_classification(test: dict, item: dict) -> str:
    """Bucket one worklist item by what the auditor has to do about it."""
    state = item_state_projection(test, item)
    coverage = item.get("evidence_coverage") or {}
    if state == "exception":
        return "exception"
    if state == "manual_review":
        return "needs_review"
    if state == "agent_checked":
        return "needs_review"
    if state == "confirmed":
        return "confirmed"
    if item.get("evidence_request_ids") or coverage.get("missing_document_types"):
        return "awaiting_evidence"
    return "not_run"


def _conclusion_state(test: dict, rollup: dict) -> str:
    """Where the test's conclusion stands, as one exclusive bucket.

    The worklist filters on this beside the outcome, so "exceptions nobody has
    concluded on yet" is a pair of clicks rather than a read of every row.

    The control conclusion alone decides whether one was recorded. Written
    reasoning without it is not a conclusion — a run that narrates a result and
    still reaches `no_conclusion` has left the sign-off outstanding, and burying
    those in a "concluded" bucket is what the filter exists to prevent. A
    control conclusion can only be saved while the test is eligible for one, so
    one recorded against a test that is no longer eligible is a conclusion the
    evidence has since moved out from under.
    """

    if str(test.get("control_conclusion") or "no_conclusion") == "no_conclusion":
        return "none"
    if str(test.get("control_conclusion_source") or "none") != "auditor":
        return "agent"
    # Only the auditor path is guarded by eligibility, so an auditor conclusion
    # on a test that is no longer eligible is one whose ground moved. An
    # unattended run writes its own conclusion without that guard, and an
    # ineligible test is no reason to hide it from the auditor reviewing them.
    return "auditor" if rollup.get("conclusion_eligible") else "stale"


def _cycle_test_classification(test: dict, rollup: dict) -> str:
    """Bucket one Cycle vouch test without flattening its assertion cells."""

    if rollup["exception_items"]:
        return "exception"
    items = test.get("items") or []
    missing_evidence = any(
        item.get("missing_roles")
        or any(
            result.get("verdict") == "missing_evidence"
            for result in (item.get("result_by_assertion") or {}).values()
        )
        for item in items
    )
    if missing_evidence:
        return "awaiting_evidence"
    requires_review = bool(
        rollup["failed_items"]
        or rollup["incomplete_items"]
        or rollup["needs_review_items"]
        or (rollup["tested_items"] and rollup["pending_dispositions"])
    )
    if requires_review:
        return "needs_review"
    if rollup["items"] and rollup["confirmed_items"] == rollup["items"]:
        return "confirmed"
    return "not_run"


def summary_payload(workspace: Workspace) -> dict:
    """Return discriminated Cycle-test and ordinary-item triage entries."""

    entry_counts = {name: 0 for name in SUMMARY_CLASSES}
    test_counts = {
        "total": 0,
        "item_first": 0,
        **{kind: 0 for kind in sorted(KINDS)},
    }
    tested_item_counts = {
        "total": 0,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "incomplete": 0,
        "needs_review": 0,
        "not_run": 0,
        "stale": 0,
        "confirmed": 0,
        "exceptions": 0,
        "pending_disposition": 0,
    }
    assertion_counts = {
        "total": 0,
        **{verdict: 0 for verdict in sorted(cycle_vouching.ASSERTION_VERDICTS)},
    }
    entries: list[dict] = []
    for path in tests_dir(workspace).glob("*.json"):
        try:
            test = _project_cycle_items(
                workspace,
                _hydrate(json.loads(path.read_text(encoding="utf-8")), workspace),
            )
        except (OSError, json.JSONDecodeError, WorkspaceError):
            continue
        test_counts["total"] += 1
        kind = str(test.get("kind") or "")
        if kind in KINDS:
            test_counts[kind] += 1
        # Both branches need the rollup now: the conclusion state each entry
        # carries turns on whether the test is still eligible for one.
        rollup = result_rollup(test)
        conclusion_state = _conclusion_state(test, rollup)
        if is_cycle_test(test):
            classification = _cycle_test_classification(test, rollup)
            entry_counts[classification] += 1
            for state in cycle_vouching.EVALUATION_STATES:
                tested_item_counts[state] += int(rollup["item_counts"][state])
            tested_item_counts["total"] += int(rollup["items"])
            tested_item_counts["executed"] += int(rollup["tested_items"])
            tested_item_counts["confirmed"] += int(rollup["confirmed_items"])
            tested_item_counts["exceptions"] += int(rollup["exception_items"])
            tested_item_counts["pending_disposition"] += int(
                rollup["pending_dispositions"]
            )
            for verdict in cycle_vouching.ASSERTION_VERDICTS:
                assertion_counts[verdict] += int(
                    rollup["assertion_counts"][verdict]
                )
            assertion_counts["total"] += int(rollup["assertion_counts"]["total"])
            entries.append({
                "entry_type": "cycle_test",
                "test_id": test.get("id"),
                "title": test.get("title") or "Untitled test",
                "test_kind": test.get("kind"),
                "test_status": test.get("status"),
                "rcm_id": test.get("rcm_id"),
                "classification": classification,
                "conclusion_state": conclusion_state,
                "item_count": int(rollup["items"]),
                "tested_item_count": int(rollup["tested_items"]),
                "evaluation_counts": dict(rollup["item_counts"]),
                "disposition_counts": dict(rollup["disposition_counts"]),
                "assertion_columns": int(rollup["assertion_columns"]),
                "assertion_counts": dict(rollup["assertion_counts"]),
                "coverage": dict(rollup["coverage"]),
                "selection_basis": str(rollup["coverage"]["selection_basis"]),
                "assurance_scope": rollup["assurance_scope"],
                "assurance_label": rollup["assurance_label"],
                "requirement_refs": list(test.get("requirement_refs") or []),
                "updated": test.get("updated"),
            })
            continue

        test_counts["item_first"] += 1
        for item in test.get("items") or []:
            classification = _item_classification(test, item)
            entry_counts[classification] += 1
            coverage = item.get("evidence_coverage") or {}
            checks = item.get("checks") or []
            conflicts = item.get("document_conflicts") or {}
            state = item_state_projection(test, item)
            tested_item_counts["total"] += 1
            if item_execution_current(test, item):
                tested_item_counts["executed"] += 1
            if state == "pending":
                tested_item_counts["not_run"] += 1
            elif state in {"agent_checked", "manual_review"}:
                tested_item_counts["needs_review"] += 1
                tested_item_counts["pending_disposition"] += 1
            elif state == "confirmed":
                tested_item_counts["confirmed"] += 1
            elif state == "exception":
                tested_item_counts["exceptions"] += 1
            verdict_map = {
                "match": "match",
                "mismatch": "mismatch",
                "missing": "missing_evidence",
                "invalid": "invalid_extraction",
            }
            for check in checks:
                verdict = verdict_map.get(str(check.get("verdict") or ""), "not_run")
                assertion_counts[verdict] += 1
                assertion_counts["total"] += 1
            entries.append({
                "entry_type": "item",
                "test_id": test.get("id"),
                "test_title": test.get("title") or "Untitled test",
                "test_kind": test.get("kind"),
                "test_status": test.get("status"),
                "rcm_id": test.get("rcm_id"),
                "item_id": item.get("id"),
                "label": item.get("label") or "",
                "instruction": item.get("instruction") or "",
                "state": state,
                "classification": classification,
                # Test-grain, repeated on every item of the test: the auditor
                # concludes once per test, and the worklist filters per row.
                "conclusion_state": conclusion_state,
                # Both readings travel with the worklist row so triage can show
                # "the runner failed it, you confirmed it" without a second load.
                "evaluation": dict(item.get("evaluation") or {}),
                "disposition": dict(item.get("disposition") or {}),
                "question": item.get("question") or "",
                "response": item.get("response") or "",
                "runner_note": item.get("runner_note") or "",
                "document_count": len(item.get("document_ids") or []),
                "citation_count": len(item.get("citations") or []),
                "evidence_count": len(item.get("evidence_refs") or []),
                "checks_total": len(checks),
                "checks_matched": sum(check.get("verdict") == "match" for check in checks),
                "checks_failed": sum(
                    check.get("verdict") in {"mismatch", "missing", "invalid"} for check in checks
                ),
                "missing_document_types": list(coverage.get("missing_document_types") or []),
                "image_only": bool(coverage.get("image_only")),
                "evidence_request_count": len(item.get("evidence_request_ids") or []),
                "has_conflict": bool(conflicts.get("duplicate_documents")),
                "updated": test.get("updated"),
            })
    # Stable sorts: title/label within severity, with the most urgent first.
    entries.sort(
        key=lambda entry: (
            _SUMMARY_RANK[entry["classification"]],
            str(entry.get("title") or entry.get("test_title") or ""),
            str(entry.get("label") or ""),
            str(entry.get("test_id") or ""),
            str(entry.get("item_id") or ""),
        )
    )
    return {
        "entry_counts": entry_counts,
        "test_counts": test_counts,
        "tested_item_counts": tested_item_counts,
        "assertion_counts": assertion_counts,
        "entries": entries,
    }
