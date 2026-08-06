"""Durable document tests and explainable, deterministic matching.

Each test is stored independently under ``DocTests/<test-id>.json``.  That
keeps row-level confirmations out of ``workspace.json`` and makes every item
checkpoint an atomic, resumable write.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import unicodedata
import uuid
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import analytics, document_analysis, documents, explore
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

KINDS = {"vouching", "attribute", "review", "qa"}
DIRECTIONS = {"vouching", "tracing"}
STATES = {"pending", "agent_checked", "confirmed", "exception", "manual_review"}
METHODS = {
    "exact",
    "normalized",
    "fuzzy",
    "numeric_tolerance",
    "date_tolerance",
    # Sequencing: the left date must not fall after the right one. This is the
    # shape of "approval precedes payment", which no equality method can express.
    "date_order",
    # Unary: the addressed value must exist and be affirmatively true. This is
    # the shape of "a receipt is attached", where the voucher profile records
    # ``present: false`` and an equality comparison would have nothing to compare.
    "present",
}
# Methods that read only the left side of a check.
UNARY_METHODS = {"present"}
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
    for item in test["items"]:
        item.setdefault("id", f"ITEM-{uuid.uuid4().hex[:8].upper()}")
        item.setdefault("instruction", "")
        item.setdefault("state", "pending")
        item.setdefault("document_ids", [])
        item.setdefault("evidence_refs", [])
        item["evidence_refs"] = normalize_many(item.get("evidence_refs") or [])
        for check in item.get("checks") or []:
            check.setdefault("verdict", "pending")
            check.setdefault("comparisons", [])
            check["evidence_refs"] = normalize_many(check.get("evidence_refs") or [])
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


def load_test(workspace: Workspace, test_id: str) -> dict:
    path = _test_path(workspace, test_id)
    if not path.exists():
        raise WorkspaceError(f"Document test '{test_id}' not found.")
    try:
        return _hydrate(json.loads(path.read_text(encoding="utf-8")), workspace)
    except json.JSONDecodeError as error:
        raise WorkspaceError(f"Document test '{test_id}' is unreadable.") from error


def exists(workspace: Workspace, test_id: str) -> bool:
    try:
        return _test_path(workspace, test_id).is_file()
    except WorkspaceError:
        return False


def list_tests(workspace: Workspace) -> list[dict]:
    items = []
    for path in tests_dir(workspace).glob("*.json"):
        try:
            test = _hydrate(json.loads(path.read_text(encoding="utf-8")), workspace)
        except (OSError, json.JSONDecodeError, WorkspaceError):
            continue
        states = [item.get("state", "pending") for item in test["items"]]
        items.append({
            **{key: test.get(key) for key in (
                "id", "kind", "title", "status", "semantic_id", "rcm_refs",
                "procedure_refs", "rcm_id", "spec", "created",
                "updated", "sha1", "created_by", "agent_run_id",
                "workflow_parent_sha1", "objective", "criteria", "steps",
                "conclusion", "conclusion_source", "control_conclusion", "control_conclusion_source", "result_summary", "next_action",
                "exception_count", "open_exception_count", "scope_limitations",
            )},
            "item_count": len(states),
            "state_counts": {state: states.count(state) for state in sorted(STATES)},
        })
    return sorted(items, key=lambda item: item.get("updated") or "", reverse=True)


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
        raise WorkspaceError("Document-test kind must be vouching, attribute, review, or qa.")
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


def _new_item(payload: dict | None = None) -> dict:
    payload = dict(payload or {})
    item = {
        "id": str(payload.get("id") or f"ITEM-{uuid.uuid4().hex[:8].upper()}"),
        "label": str(payload.get("label") or "Test item"),
        "instruction": str(payload.get("instruction") or ""),
        "state": str(payload.get("state") or "pending"),
        "document_ids": [str(value) for value in (payload.get("document_ids") or [])],
        "evidence_refs": normalize_many(payload.get("evidence_refs") or []),
    }
    if item["state"] not in STATES:
        raise WorkspaceError("Unknown document-test item state.")
    return item


def _build_items(workspace: Workspace, test: dict, raw_items: object) -> None:
    """Normalize one payload's items onto a test according to its kind."""
    kind = test["kind"]
    known_documents = {str(item.get("id")) for item in workspace.documents}
    for raw in raw_items or []:
        item = _new_item(raw)
        missing_documents = [
            document_id
            for document_id in item.get("document_ids") or []
            if document_id not in known_documents
        ]
        if missing_documents:
            raise WorkspaceError(
                f"Document '{missing_documents[0]}' not found for Document Test item."
            )
        if kind == "vouching":
            item["frozen"] = {str(key): _json_value(value) for key, value in dict(raw.get("frozen") or {}).items()}
            item["checks"] = [_normalize_check(check) for check in (raw.get("checks") or [])]
            # Role-tagged attachments are what make a check's ``<role>.…`` path
            # resolvable. Absent, the item is a legacy single-shape worklist and
            # its checks read document page text instead.
            item["documents"] = [
                {
                    "document_id": str(value.get("document_id") or ""),
                    "role": str(value.get("role") or "other"),
                    "document_type": str(value.get("document_type") or ""),
                    "matched_by": str(value.get("matched_by") or "auditor"),
                }
                for value in (raw.get("documents") or [])
                if str(value.get("document_id") or "")
            ]
            item["missing_roles"] = [
                str(value) for value in (raw.get("missing_roles") or [])
            ]
            if item["documents"] and not item["document_ids"]:
                item["document_ids"] = [
                    entry["document_id"] for entry in item["documents"]
                ]
        elif kind == "attribute":
            item["attributes"] = [_normalize_attribute(value) for value in (raw.get("attributes") or [])]
        elif kind == "review":
            item.update(page=raw.get("page"), review_kind=str(raw.get("review_kind") or "general"), summary=str(raw.get("summary") or ""), excerpt=str(raw.get("excerpt") or ""))
        else:
            item.update(question=str(raw.get("question") or ""), response=str(raw.get("response") or ""), citations=normalize_many(raw.get("citations") or []))
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
    settled = [
        item for item in test.get("items") or []
        if item.get("state") in {"confirmed", "exception"}
    ]
    if settled:
        raise WorkspaceError(
            f"Document Test '{test_id}' has final-result items and cannot be re-specified."
        )
    kind = str(payload.get("kind") or "").lower()
    if kind not in KINDS:
        raise WorkspaceError("Document-test kind must be vouching, attribute, review, or qa.")
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
    """Normalize one comparison, in either of the two supported shapes.

    A check with ``left``/``right`` is a cycle comparison: both sides are paths
    resolved at run time, against the frozen population row and the structured
    fields of the documents attached in each role. A check with ``expected`` is
    the original shape — a literal value searched for in document page text —
    and is preserved unchanged for auditor-authored and historical tests.
    """
    method = str(check.get("method") or "normalized")
    if method not in METHODS:
        raise WorkspaceError(f"Unknown comparison method '{method}'.")
    left = str(check.get("left") or "").strip()
    right = str(check.get("right") or "").strip()
    if left:
        validate_path(left)
        if right:
            validate_path(right)
        elif method not in UNARY_METHODS:
            raise WorkspaceError(
                f"Comparison method '{method}' needs both a left and a right path."
            )
    elif method in UNARY_METHODS or right:
        raise WorkspaceError(
            f"Comparison method '{method}' requires a left path."
        )
    return {
        "field": str(check.get("field") or "value"),
        "left": left,
        "right": right,
        "expected": _json_value(check.get("expected")),
        "found": _json_value(check.get("found")),
        "method": method,
        "tolerance": check.get("tolerance"),
        "verdict": str(check.get("verdict") or "pending"),
        "note": str(check.get("note") or ""),
        "comparisons": list(check.get("comparisons") or []),
        "evidence_refs": normalize_many(check.get("evidence_refs") or []),
    }


# --------------------------------------------------------------------------- #
# Cycle vouching: resolving one side of a comparison
#
# A check names two sides by path. ``row.<column>`` reads the frozen population
# row; ``<role>.<group>.<key>[.<attr>]`` reads the structured field a voucher
# profile extracted from the document attached in that role. That second form is
# the whole chaining primitive: for a procurement cycle, ``purchase_order.amount
# .total`` against ``invoice.amount.total`` compares two documents to each other,
# and neither against the population, using exactly the same machinery as a
# row-to-document comparison.
# --------------------------------------------------------------------------- #

# group name -> (fields group, discriminator key, default attribute)
FIELD_GROUPS = {
    "identifier": ("identifiers", "kind", "value"),
    "date": ("dates", "kind", "value"),
    "amount": ("amounts", "kind", "value"),
    "party": ("parties", "role", "name"),
    "attachment": ("attachments", "kind", "present"),
    "approval": ("approvals", "role", "date"),
    "line_item": ("line_items", "category", "amount"),
}

# The attributes each group's entries actually carry, per the voucher profile's
# declared schema. Validated rather than resolved leniently: an unknown
# attribute would otherwise resolve to nothing and surface as a missing
# comparison at run time, which reads like absent evidence rather than the
# authoring mistake it is.
FIELD_GROUP_ATTRIBUTES = {
    "identifier": frozenset({"kind", "value"}),
    "date": frozenset({"kind", "value"}),
    "amount": frozenset({"kind", "value", "currency"}),
    "party": frozenset({"role", "name"}),
    "attachment": frozenset({"kind", "reference", "present"}),
    "approval": frozenset({"approver", "role", "decision", "date"}),
    "line_item": frozenset(
        {"description", "amount", "quantity", "date", "category", "receipt_reference"}
    ),
}
ROW_PREFIX = "row"
WILDCARD = "*"


class PathError(WorkspaceError):
    """A check names a path this test's shape cannot resolve."""


def _parse_path(path: str) -> tuple[str, str, str, str]:
    """Split one check path into (role, group, key, attribute)."""

    parts = [part for part in str(path or "").split(".") if part != ""]
    if not parts:
        raise PathError("A check path cannot be empty.")
    if parts[0] == ROW_PREFIX:
        if len(parts) != 2:
            raise PathError(f"'{path}' must name exactly one population column.")
        return ROW_PREFIX, "", parts[1], ""
    if len(parts) < 3:
        raise PathError(
            f"'{path}' must be 'row.<column>' or '<role>.<group>.<key>[.<attribute>]'."
        )
    role, group, key = parts[0], parts[1], parts[2]
    if group not in FIELD_GROUPS:
        raise PathError(
            f"'{path}' names unknown field group '{group}'; expected one of "
            + ", ".join(sorted(FIELD_GROUPS))
            + "."
        )
    attribute = parts[3] if len(parts) > 3 else FIELD_GROUPS[group][2]
    if len(parts) > 4:
        raise PathError(f"'{path}' has too many segments.")
    known = FIELD_GROUP_ATTRIBUTES[group]
    # ``raw_<attr>`` is the verbatim form normalization preserves beside each
    # typed value, so it is addressable wherever its typed counterpart is.
    base = attribute[4:] if attribute.startswith("raw_") else attribute
    if base not in known:
        raise PathError(
            f"'{path}' names unknown attribute '{attribute}' of group '{group}'; "
            "expected one of " + ", ".join(sorted(known)) + "."
        )
    return role, group, key, attribute


def validate_path(path: str) -> None:
    """Raise if a path is not resolvable in principle. Shape only, never data."""

    _parse_path(path)


def _resolved(value, citation, document_id, page, excerpt):
    return {
        "value": _json_value(value),
        "citation": citation,
        "document_id": document_id,
        "page": page,
        "excerpt": excerpt,
    }


def resolve_check_path(
    path: str,
    *,
    frozen: dict,
    role_fields: dict,
) -> list[dict]:
    """Every value one path addresses, with the citation that anchors each.

    ``role_fields`` maps a role to ``(document_id, fields, citations_by_id)`` for
    each document attached in that role. Returning a list rather than a scalar is
    deliberate: zero matches is a missing comparison, and two conflicting matches
    is an ambiguity an auditor must see rather than a value the runner silently
    picks between.
    """
    role, group, key, attribute = _parse_path(path)
    if role == ROW_PREFIX:
        if key not in frozen:
            return []
        return [_resolved(frozen[key], None, None, None, None)]
    matches: list[dict] = []
    for document_id, fields, citations in role_fields.get(role, ()):
        fields_group, discriminator, _default = FIELD_GROUPS[group]
        for entry in fields.get(fields_group) or []:
            if not isinstance(entry, dict):
                continue
            if key != WILDCARD and normalize_value(
                entry.get(discriminator)
            ) != normalize_value(key):
                continue
            if attribute not in entry:
                continue
            citation = str(entry.get("citation") or "")
            anchor = citations.get(citation) or {}
            matches.append(
                _resolved(
                    entry.get(attribute),
                    citation,
                    document_id,
                    anchor.get("page"),
                    anchor.get("excerpt"),
                )
            )
    return matches


def _single_value(matches: list[dict]) -> tuple[object, str]:
    """Collapse resolved matches to one comparable value, or say why not.

    Repeated matches that normalize identically are one fact the record stated
    more than once; genuinely different values are an ambiguity, because picking
    one would decide the comparison's outcome by list order.
    """
    if not matches:
        return None, "missing"
    distinct = {normalize_value(match["value"]) for match in matches}
    if len(distinct) > 1:
        return None, "ambiguous"
    return matches[0]["value"], "resolved"


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


def voucher_field_index(workspace: Workspace) -> list[dict]:
    """Every analyzed document that carries a structured voucher record.

    Reads the generated analysis rather than the document text: after the voucher
    profile runs, a document's identifiers are discrete extracted values with
    citations behind them, so linking is exact-value matching rather than a
    substring scan that would confuse ``EXP-2025-001`` with ``EXP-2025-0010``.
    """
    index = []
    for document in workspace.documents:
        record = document_analysis.generated_record(workspace, document["id"]) or {}
        fields = record.get("fields") or {}
        if not fields:
            continue
        citations = {
            str(citation.get("id") or ""): citation
            for citation in record.get("citations") or []
        }
        index.append(
            {
                "document": document,
                "document_id": str(document["id"]),
                "role": str(fields.get("document_type") or "") or "other",
                "fields": fields,
                "citations": citations,
                "identifiers": {
                    normalize_value(entry.get("value"))
                    for entry in fields.get("identifiers") or []
                    if isinstance(entry, dict) and normalize_value(entry.get("value"))
                },
            }
        )
    return index


def voucher_anchor_candidates(
    workspace: Workspace,
    table: str,
    columns: list[dict],
    *,
    document_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Return aggregate table columns that overlap extracted identifiers.

    Identifier values are compared only on the user's machine.  The projection
    contains names, counts, and document types but no table row or extracted
    identifier value, so context adapters can ground a generated cycle plan
    without crossing the row-data privacy boundary.
    """

    index = [
        item
        for item in voucher_field_index(workspace)
        if document_ids is None or str(item.get("document_id") or "") in document_ids
    ]
    if not index:
        return []
    frame = workspace.get_frame(table)
    candidates = []
    for column in columns:
        name = str(column.get("name") or "").strip()
        if not name or name not in frame.columns:
            continue
        matched_rows = 0
        matched_documents: set[str] = set()
        document_types: set[str] = set()
        for value in frame.get_column(name).to_list():
            normalized = normalize_value(value)
            if not normalized:
                continue
            matches = [
                item
                for item in index
                if normalized in item.get("identifiers", set())
            ]
            if not matches:
                continue
            matched_rows += 1
            matched_documents.update(str(item["document_id"]) for item in matches)
            document_types.update(str(item["role"]) for item in matches)
        if matched_rows:
            candidates.append(
                {
                    "table": table,
                    "anchor_key": name,
                    "matched_rows": matched_rows,
                    "matched_document_count": len(matched_documents),
                    "document_types": sorted(document_types),
                }
            )
    return candidates


def voucher_document_profile(
    workspace: Workspace,
    document_id: str,
) -> dict[str, object] | None:
    """Project one voucher's usable comparison paths without field values."""

    record = document_analysis.generated_record(workspace, document_id) or {}
    fields = record.get("fields") or {}
    document_type = str(fields.get("document_type") or "").strip()
    if not document_type:
        return None
    suffixes: set[str] = set()
    for group, (collection, discriminator, default_attribute) in FIELD_GROUPS.items():
        for entry in fields.get(collection) or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get(discriminator) or "").strip()
            if not key:
                continue
            base = f"{group}.{key}"
            suffixes.add(base)
            for attribute in sorted(FIELD_GROUP_ATTRIBUTES[group]):
                if attribute in {default_attribute, discriminator}:
                    continue
                if attribute in entry:
                    suffixes.add(f"{base}.{attribute}")
    return {
        "document_id": document_id,
        "document_type": document_type,
        "available_path_suffixes": sorted(suffixes),
    }


def _role_fields(workspace: Workspace, item: dict) -> dict[str, list[tuple]]:
    """Group one item's attached documents by role, with their extracted fields."""

    grouped: dict[str, list[tuple]] = {}
    for attachment in item.get("documents") or []:
        document_id = str(attachment.get("document_id") or "")
        record = document_analysis.generated_record(workspace, document_id) or {}
        fields = record.get("fields") or {}
        citations = {
            str(citation.get("id") or ""): citation
            for citation in record.get("citations") or []
        }
        grouped.setdefault(str(attachment.get("role") or "other"), []).append(
            (document_id, fields, citations)
        )
    return grouped


def build_cycle_vouching(workspace: Workspace, payload: dict) -> dict:
    """Build a vouching worklist from documents already linked to a population.

    The sample is not selected here — it is *recognized*. Every row whose anchor
    identifier appears in an analyzed document's extracted identifiers becomes an
    item covering that row's whole cycle: one anchor row, every document that
    names it, and the declared comparisons across them. Rows with no document are
    counted and reported as uncovered rather than silently dropped, so the
    coverage the test actually achieved stays visible against the population.
    """
    table = str(payload.get("table") or "")
    if table not in workspace.table_names():
        raise WorkspaceError(f"No table named '{table}'.")
    frame = workspace.get_frame(table).with_row_index("source_row", offset=2)
    anchor_key = str(payload.get("anchor_key") or "")
    if anchor_key not in frame.columns:
        raise WorkspaceError(f"Anchor key '{anchor_key}' does not exist in '{table}'.")
    frozen_fields = [
        str(value) for value in (payload.get("frozen_fields") or frame.columns)
        if str(value) != "source_row"
    ]
    missing = [name for name in frozen_fields if name not in frame.columns]
    if missing:
        raise WorkspaceError(f"Frozen field '{missing[0]}' does not exist in '{table}'.")
    # Which extracted document types fill a role is the test's declaration, not
    # one model field's verdict. A profile classifies each record independently,
    # so an identically formatted pack can come back ``expense_claim`` rather
    # than ``payment_voucher``; without a declared mapping that single word would
    # silently drop a whole cycle out of scope. Defaults to the role's own name.
    declared_roles = [
        {
            "role": str(value.get("role") or "").strip(),
            "required": bool(value.get("required", True)),
            "document_types": [
                str(entry).strip()
                for entry in (
                    value.get("document_types")
                    or [str(value.get("role") or "").strip()]
                )
                if str(entry).strip()
            ],
        }
        for value in (payload.get("document_roles") or [])
        if str(value.get("role") or "").strip()
    ]
    role_by_type = {
        document_type: entry["role"]
        for entry in declared_roles
        for document_type in entry["document_types"]
    }
    check_templates = [_normalize_check(check) for check in (payload.get("checks") or [])]
    if not check_templates:
        raise WorkspaceError("A cycle vouching test needs at least one comparison.")

    index = voucher_field_index(workspace)
    source_hash = _sha1(workspace._table_signature(table))
    test = _base_test(workspace, payload, "vouching")
    required_roles = [entry["role"] for entry in declared_roles if entry["required"]]

    linked = 0
    incomplete = 0
    for row in frame.iter_rows(named=True):
        anchor = normalize_value(row.get(anchor_key))
        if not anchor:
            continue
        matched = [entry for entry in index if anchor in entry["identifiers"]]
        if not matched:
            continue
        linked += 1
        # A document whose extracted type fills no declared role still attaches
        # under its own type, so a path naming that type resolves and an
        # unexpected document in the pack stays visible rather than vanishing.
        roles = {
            entry["document_id"]: role_by_type.get(entry["role"], entry["role"])
            for entry in matched
        }
        available_roles = set(roles.values())
        missing_roles = [role for role in required_roles if role not in available_roles]
        incomplete += bool(missing_roles)
        item = _new_item(
            {
                "label": str(row.get(anchor_key)),
                "instruction": str(payload.get("objective") or ""),
                "document_ids": [entry["document_id"] for entry in matched],
            }
        )
        item.update(
            source={
                "table": table,
                "source_row": row["source_row"],
                "source_sha1": source_hash,
            },
            transaction_identifiers=[str(row.get(anchor_key))],
            frozen={field: _json_value(row.get(field)) for field in frozen_fields},
            documents=[
                {
                    "document_id": entry["document_id"],
                    "role": roles[entry["document_id"]],
                    "document_type": entry["role"],
                    "matched_by": "extracted_identifier",
                }
                for entry in matched
            ],
            missing_roles=missing_roles,
            checks=[dict(check) for check in check_templates],
        )
        test["items"].append(item)

    population = frame.height
    test["spec"].update(
        {
            "direction": "vouching",
            "shape": "cycle",
            "table": table,
            "table_sha1": source_hash,
            "anchor_key": anchor_key,
            "frozen_fields": frozen_fields,
            "document_roles": declared_roles,
            "population_rows": population,
            "require_all_documents": bool(payload.get("require_all_documents", True)),
            "coverage": {
                "population": population,
                "linked": linked,
                "unlinked": population - linked,
                "incomplete_roles": incomplete,
            },
        }
    )
    uncovered = population - linked
    limitations = []
    if uncovered:
        limitations.append(
            f"{uncovered} of {population} population row(s) have no linked document "
            "and were not tested."
        )
    if incomplete:
        limitations.append(
            f"{incomplete} linked row(s) are missing a required document role."
        )
    test["scope_limitations"] = " ".join(limitations)
    test["status"] = "blocked" if not test["items"] else "in_progress"
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
        f"{len(requests)} selected item(s) require additional evidence."
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
        test["control_conclusion"] = conclusion
        test["control_conclusion_source"] = "auditor"
    if "steps" in changes:
        test["steps"] = _normalize_steps(changes["steps"])
    for key in ("objective", "criteria", "conclusion", "scope_limitations", "next_action"):
        if key in changes:
            test[key] = str(changes[key] or "")
    if "conclusion" in changes:
        test["conclusion_source"] = "auditor"
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
    item = _item(test, item_id)
    document = next((value for value in workspace.documents if value.get("id") == document_id), None)
    if document is None:
        raise WorkspaceError(f"Document '{document_id}' not found.")
    if document_id not in item["document_ids"]:
        item["document_ids"].append(document_id)
    item["state"] = "pending"
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
    item = _item(test, item_id)
    item["document_ids"] = [value for value in item["document_ids"] if value != document_id]
    item["state"] = "pending"
    return save_test(workspace, test)


def update_comparisons(workspace: Workspace, test_id: str, item_id: str, checks: list[dict]) -> dict:
    test = load_test(workspace, test_id)
    if test["kind"] != "vouching":
        raise WorkspaceError("Comparison settings apply only to vouching/tracing tests.")
    item = _item(test, item_id)
    item["checks"] = [_normalize_check(check) for check in checks]
    item["state"] = "pending"
    return save_test(workspace, test)


# The only transitions an auditor may make by hand: sign off a result either
# way, or send it back for re-run. ``agent_checked``/``manual_review`` are
# outcomes the runner produces, never a state a person picks.
MANUAL_SIGNOFF_STATES = frozenset({"confirmed", "exception", "pending"})


def update_item(
    workspace: Workspace,
    test_id: str,
    item_id: str,
    changes: dict,
    *,
    runner_note: str | None = None,
) -> dict:
    test = load_test(workspace, test_id)
    item = _item(test, item_id)
    allowed = {
        "attributes",
        "summary", "excerpt", "response", "citations", "state",
    }
    if set(changes) - allowed:
        raise WorkspaceError("Unknown document-test item field.")
    for key, value in changes.items():
        if key == "attributes":
            item[key] = [_normalize_attribute(entry) for entry in (value or [])]
        elif key == "citations":
            item[key] = normalize_many(value or [], require_hash=True)
        elif key == "state":
            state = str(value or "")
            if state not in MANUAL_SIGNOFF_STATES:
                raise WorkspaceError(
                    "An auditor may only set a document-test item to confirmed, "
                    "exception, or pending."
                )
            item["state"] = state
            item["runner_note"] = "Auditor sign-off." if state != "pending" else "Reset for re-run by the auditor."
        else:
            item[key] = value
    if runner_note is not None:
        item["runner_note"] = runner_note
    if "state" in changes:
        states = [value.get("state") for value in test["items"]]
        test["status"] = "completed" if states and all(
            state in {"confirmed", "exception", "manual_review"} for state in states
        ) else "in_progress"
    return save_test(workspace, test)


def _refresh_agent_conclusion(test: dict) -> None:
    """Roll worker-authored item conclusions into the completed test result.

    The worker evaluates one item/document pair, while the durable result is a
    Document Test. This deterministic roll-up needs no extra model turn and
    never overwrites an explicit auditor conclusion.
    """
    if test.get("status") != "completed" or test.get("conclusion_source") == "auditor":
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
        test.get("status") != "completed"
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
        item.get("state") != "agent_checked"
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
    item["state"] = (
        "exception" if assessment_outcome == "exception"
        else "manual_review" if assessment_outcome == "needs_manual_check"
        else "confirmed"
    )
    item["runner_note"] = f"Model assessment outcome: {assessment_outcome}."
    states = [value.get("state") for value in test["items"]]
    test["status"] = "completed" if states and all(
        state in {"confirmed", "exception", "manual_review"} for state in states
    ) else "in_progress"
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
    elif method == "present":
        # ``found`` carries the single addressed value; a false boolean is an
        # affirmative negative — the record says the item is not there — and is
        # a mismatch rather than a missing value.
        value = found if not isinstance(found, str) else found.strip()
        truth = (
            value
            if isinstance(value, bool)
            else str(value).strip().casefold() not in {"false", "no", "none", "0"}
        )
        result.update(
            normalization={"expected": True, "found": truth},
            result="match" if truth else "mismatch",
        )
    elif method == "date_order":
        left, right = _date(expected), _date(found)
        if left is None or right is None:
            result["result"] = "invalid"
        else:
            days = int((tolerance or {}).get("days", 0) if isinstance(tolerance, dict) else tolerance or 0)
            result.update(
                normalization={"expected": left.isoformat(), "found": right.isoformat()},
                tolerance={"days": days},
                result="match" if (left - right).days <= days else "mismatch",
            )
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


def _cycle_evidence(
    workspace: Workspace, matches: list[dict], *, run_id: str | None
) -> list[dict]:
    """Anchor each resolved document value to the citation that supports it."""

    anchors = []
    for match in matches:
        document_id = match.get("document_id")
        if not document_id or not match.get("excerpt"):
            continue
        document = next(
            (value for value in workspace.documents if value.get("id") == document_id),
            None,
        )
        if document is None:
            continue
        anchors.append(
            document_anchor(
                document,
                int(match.get("page") or 1),
                str(match["excerpt"])[:400],
                generated_by=run_id or "cycle-vouching",
            )
        )
    return anchors


def _run_cycle_item(
    workspace: Workspace, test: dict, item: dict, *, run_id: str | None
) -> dict:
    """Compare one transaction cycle deterministically.

    Every value on both sides was extracted by the voucher profile and is
    anchored to a verbatim citation, so this function performs no matching
    against raw text and calls no model: it resolves two paths, compares them,
    and records which citation supports each side.
    """
    role_fields = _role_fields(workspace, item)
    frozen = dict(item.get("frozen") or {})
    anchors: list[dict] = []
    unresolved = 0
    for check in item.get("checks") or []:
        if not check.get("left"):
            # A legacy literal check on a cycle item: nothing addresses it, so it
            # is surfaced for the auditor rather than silently passed.
            check.update(verdict="missing", note="This check names no path to resolve.")
            unresolved += 1
            continue
        try:
            left_matches = resolve_check_path(
                check["left"], frozen=frozen, role_fields=role_fields
            )
            right_matches = (
                resolve_check_path(
                    check["right"], frozen=frozen, role_fields=role_fields
                )
                if check.get("right")
                else []
            )
        except PathError as error:
            check.update(verdict="invalid", note=str(error))
            unresolved += 1
            continue
        left_value, left_state = _single_value(left_matches)
        if check["method"] in UNARY_METHODS:
            right_value, right_state = left_value, left_state
        else:
            right_value, right_state = _single_value(right_matches)
        states = {left_state, right_state}
        if "ambiguous" in states:
            check.update(
                verdict="ambiguous",
                note="More than one differing value matched a path on this record.",
                comparisons=[
                    {"side": "left", "matches": left_matches},
                    {"side": "right", "matches": right_matches},
                ],
            )
            unresolved += 1
            continue
        outcome = compare_values(
            left_value if check["method"] not in UNARY_METHODS else True,
            right_value,
            check["method"],
            check.get("tolerance"),
        )
        matched_anchors = _cycle_evidence(
            workspace, left_matches + right_matches, run_id=run_id
        )
        anchors.extend(matched_anchors)
        check.update(
            expected=_json_value(left_value),
            found=_json_value(right_value),
            verdict=outcome["result"],
            evidence_refs=matched_anchors,
            comparisons=[
                {
                    "side": "left",
                    "path": check["left"],
                    "state": left_state,
                    "matches": left_matches,
                },
                {
                    "side": "right",
                    "path": check.get("right") or "",
                    "state": right_state,
                    "matches": right_matches,
                },
                outcome,
            ],
        )
    item["evidence_refs"] = anchors
    verdicts = [check.get("verdict") for check in item.get("checks") or []]
    if item.get("missing_roles") or unresolved or "ambiguous" in verdicts:
        item["state"] = "manual_review"
        item["runner_note"] = (
            "Manual check required: "
            + (
                f"missing document role(s) {', '.join(item['missing_roles'])}. "
                if item.get("missing_roles")
                else ""
            )
            + (f"{unresolved} check(s) could not be resolved." if unresolved else "")
        ).strip()
    elif any(value in {"mismatch", "missing", "invalid"} for value in verdicts):
        item["state"] = "exception"
        item["runner_note"] = "Deterministic cycle comparison found an exception."
    else:
        item["state"] = "confirmed"
        item["runner_note"] = "Deterministic cycle comparison completed."
    save_test(workspace, test)
    return item


def run_item(
    workspace: Workspace, test_id: str, item_id: str, *,
    run_id: str | None = None, model_adapter=None,
) -> dict:
    test = load_test(workspace, test_id)
    item = _item(test, item_id)
    if test["kind"] == "qa":
        if not item.get("document_ids") or not str(item.get("question") or "").strip():
            item.update(state="manual_review", runner_note="Attach evidence and record a question before running this item.")
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
                response="\n\n".join(value for value in answers if value), citations=citations,
                evidence_refs=citations, state="confirmed",
                runner_note="Cited answer generated from the attached pages.",
            )
        except Exception as error:
            item.update(
                state="manual_review",
                runner_note=f"Manual fallback: {error}",
            )
        save_test(workspace, test)
        return item
    if test["kind"] != "vouching":
        item["state"] = "manual_review"
        item["runner_note"] = "This test kind requires auditor input in the current runner."
        save_test(workspace, test)
        return item
    if not item.get("document_ids"):
        item.update(state="manual_review", runner_note="Attach at least one document before running this item.")
        save_test(workspace, test)
        return item
    if item.get("documents"):
        return _run_cycle_item(workspace, test, item, run_id=run_id)

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
        item["state"] = "manual_review"
        item["runner_note"] = "Manual check required because evidence could not be matched or duplicate documents are attached."
    else:
        item["state"] = (
            "exception"
            if any(check.get("verdict") in {"mismatch", "missing", "invalid"} for check in item.get("checks") or [])
            else "confirmed"
        )
        item["runner_note"] = "Deterministic local comparison completed."
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
        state="agent_checked",
        runner_note="Cited answers were generated from the attached pages in stable document order.",
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
        state="agent_checked",
        runner_note="Cited LLM assessment generated from the attached pages.",
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
    checks = [check for item in items for check in (item.get("checks") or [])]
    return {
        "items": len(items),
        "matched": sum(check.get("verdict") == "match" for check in checks),
        "mismatched": sum(check.get("verdict") in {"mismatch", "missing", "invalid"} for check in checks),
        "confirmed": sum(item.get("state") == "confirmed" for item in items),
        "exceptions": sum(item.get("state") == "exception" for item in items),
        "manual_review": sum(item.get("state") == "manual_review" for item in items),
        "pending": sum(item.get("state") in {"pending", "agent_checked"} for item in items),
    }


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
    }


SUMMARY_CLASSES = ("exception", "needs_review", "awaiting_evidence", "confirmed", "not_run")
_SUMMARY_RANK = {name: index for index, name in enumerate(SUMMARY_CLASSES)}


def _item_classification(item: dict) -> str:
    """Bucket one worklist item by what the auditor has to do about it."""
    state = str(item.get("state") or "pending")
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


def summary_payload(workspace: Workspace) -> dict:
    """Return every worklist item across the engagement, flattened and bucketed.

    Document tests carry very few items each (commonly one or two), so the
    engagement-level unit of review is the item, not the test.  This keeps the
    tab to one request and lets it show a single triage list instead of nesting
    a rail inside a rail.
    """
    counts = {name: 0 for name in SUMMARY_CLASSES}
    items: list[dict] = []
    tests: list[dict] = []
    for path in tests_dir(workspace).glob("*.json"):
        try:
            test = _hydrate(json.loads(path.read_text(encoding="utf-8")), workspace)
        except (OSError, json.JSONDecodeError, WorkspaceError):
            continue
        test_counts = {name: 0 for name in SUMMARY_CLASSES}
        for item in test.get("items") or []:
            classification = _item_classification(item)
            counts[classification] += 1
            test_counts[classification] += 1
            coverage = item.get("evidence_coverage") or {}
            checks = item.get("checks") or []
            conflicts = item.get("document_conflicts") or {}
            items.append({
                "test_id": test.get("id"),
                "test_title": test.get("title") or "Untitled test",
                "test_kind": test.get("kind"),
                "test_status": test.get("status"),
                "rcm_id": test.get("rcm_id"),
                "item_id": item.get("id"),
                "label": item.get("label") or "",
                "instruction": item.get("instruction") or "",
                "state": item.get("state"),
                "classification": classification,
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
        tests.append({
            "test_id": test.get("id"),
            "title": test.get("title") or "Untitled test",
            "kind": test.get("kind"),
            "status": test.get("status"),
            "rcm_id": test.get("rcm_id"),
            "item_count": len(test.get("items") or []),
            "counts": test_counts,
            "objective": test.get("objective") or "",
            "result_summary": test.get("result_summary") or "",
            "conclusion": test.get("conclusion") or "",
            "control_conclusion": test.get("control_conclusion"),
            "scope_limitations": test.get("scope_limitations") or "",
            "next_action": test.get("next_action") or "",
            "exception_count": test.get("exception_count") or 0,
            "open_exception_count": test.get("open_exception_count") or 0,
            "updated": test.get("updated"),
        })
    # Stable sorts: newest engagement work first, then severity on top.
    items.sort(key=lambda item: (str(item.get("test_title") or ""), str(item.get("label") or "")))
    items.sort(key=lambda item: _SUMMARY_RANK[item["classification"]])
    tests.sort(key=lambda test: str(test.get("updated") or ""), reverse=True)
    return {"counts": counts, "items": items, "tests": tests}
