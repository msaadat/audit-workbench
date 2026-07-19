"""Workspace model and storage.

A workspace is the unit of an engagement: a folder holding the auditor's data
files plus a JSON definition of the tables and joins built on them::

    Workspaces/<id>/
        workspace.json   ← meta + table entries + join definitions
        Data/            ← the uploaded data files

Base tables map 1:1 to files; joins are named, derived tables that can
reference base tables or other joins. Frames are resolved lazily through
:mod:`.loader`, so nothing is parsed until a tab actually needs data.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import keyword
import os
import re
import shutil
import time
import tokenize
import uuid
import threading
from contextvars import ContextVar, Token
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from . import config  # noqa: F401  # load .env before reading WORKBENCH_DATA
from . import loader, profiler
from .field_names import resolve_columns

SCHEMA_VERSION = 2
JOIN_TYPES = ("inner", "left", "full", "semi", "anti", "cross")

PLANNED_TEST_METHODS = {
    "data_analytics",
    "validation",
    "document_inspection",
    "inquiry",
    "hybrid",
    "evidence_unavailable",
}
PLANNED_TEST_STATUSES = {
    "not_ready",
    "ready",
    "in_progress",
    "review_required",
    "blocked",
    "completed_no_exception",
    "completed_with_exception",
    "not_applicable",
}
CONTROL_CONCLUSIONS = {
    "effective",
    "partially_effective",
    "ineffective",
    "no_conclusion",
    "not_applicable",
}
REVIEW_STATUSES = {"draft", "prepared", "review_required", "reviewed"}
OBSERVATION_DISPOSITIONS = {
    "confirmed_control_exception",
    "data_quality_issue",
    "expected_or_benign",
    "screening_follow_up",
    "invalid_test_or_result",
    "duplicate",
    "draft_finding_candidate",
}

WORKSPACES_DIR = Path(
    os.environ.get("WORKBENCH_DATA", "")
    or Path(__file__).resolve().parents[2] / "Workspaces"
)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "item"


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON via a temp file + rename so a crash mid-write can never
    leave a truncated definition behind."""
    before_meta = None
    if path.exists():
        try:
            raw_before = path.read_bytes()
            parsed_before = json.loads(raw_before)
            before_meta = _debug_artifact_meta(path, parsed_before, raw_before)
        except (OSError, json.JSONDecodeError):
            before_meta = None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Windows can briefly deny a replace while antivirus/indexing software or
    # a concurrent API read has the destination open. Retrying preserves the
    # atomic-write guarantee without surfacing a spurious engagement error.
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            _record_atomic_artifact_transition(path, before_meta, payload)
            return
        except PermissionError:
            if attempt == 5:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.02 * (attempt + 1))


def _debug_artifact_meta(path: Path, payload: object, raw: bytes | None = None) -> dict:
    """Metadata-only projection: never duplicate document text or binaries."""
    encoded = raw if raw is not None else json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    selected = {}
    if isinstance(payload, dict):
        for key in (
            "schema_version", "id", "document_id", "test_id", "run_id", "revision",
            "status", "state", "run_state", "analysis_state", "review_state",
            "updated", "updated_at", "created_at", "finished_at",
        ):
            if key in payload: selected[key] = payload[key]
    return {
        "artifact_path": str(path), "size_bytes": len(encoded),
        "sha1": hashlib.sha1(encoded).hexdigest(), "structural_fields": selected,
    }


def _record_atomic_artifact_transition(path: Path, before: dict | None, payload: dict) -> None:
    # Primary workspace/run writes have richer hooks. Debug writes are the
    # telemetry itself and must never recursively trace themselves.
    if "Debug" in path.parts or ".Transactions" in path.parts or path.name == "workspace.json" or (
        path.name == "run.json" and "AgentRuns" in path.parts
    ):
        return
    root = path.parent
    while root != root.parent and not (root / "workspace.json").exists():
        root = root.parent
    if not (root / "workspace.json").exists():
        return
    try:
        workspace_id = str(json.loads((root / "workspace.json").read_text(encoding="utf-8")).get("id") or root.name)
        from . import debug_store
        after = _debug_artifact_meta(path, payload)
        relative = str(path.relative_to(root))
        with debug_store.trace_context(workspace_id=workspace_id, workspace_root=str(root)):
            debug_store.record_transition(
                workspace_id, before, after,
                trigger=str(debug_store.current_context().get("trigger") or f"artifact.write:{relative}"),
                kind="artifact_state",
            )
    except Exception:
        pass


# Provenance keys accepted on saved items (tiles/analyses/rulesets/joins).
# An item carrying agent_run_id was created by an agent run; semantic_id is a
# stable slug the agent uses to reconcile reruns instead of duplicating work.
def _apply_provenance(item: dict, payload: dict) -> dict:
    if payload.get("agent_run_id"):
        item["agent_run_id"] = str(payload["agent_run_id"])
        item["created_by"] = "agent"
    if payload.get("semantic_id"):
        item["semantic_id"] = str(payload["semantic_id"])
    return item


def _user_touch(item: dict) -> None:
    """A manual edit of an agent-created item makes it user-owned: reruns of
    the agent must no longer update or replace it."""
    if item.get("created_by") == "agent":
        item["created_by"] = "user"


class WorkspaceError(ValueError):
    """A user-facing workspace problem (bad name, missing table, bad join)."""


class WorkspaceConflict(WorkspaceError):
    """A compare-and-swap write failed because the workspace changed."""

    def __init__(self, expected_revision: int, current_revision: int):
        self.expected_revision = int(expected_revision)
        self.current_revision = int(current_revision)
        super().__init__(
            "The workspace changed while this operation was in progress "
            f"(expected revision {self.expected_revision}, current revision "
            f"{self.current_revision}). Reload and retry."
        )


_workspace_write_locks: dict[str, threading.RLock] = {}
_workspace_write_locks_guard = threading.Lock()
_request_revision: ContextVar[dict[str, int] | None] = ContextVar(
    "workspace_request_revision", default=None
)


def workspace_write_lock(root: Path) -> threading.RLock:
    """Return the process-wide lock shared by every writer for one workspace."""
    key = str(Path(root).resolve())
    with _workspace_write_locks_guard:
        return _workspace_write_locks.setdefault(key, threading.RLock())


def sync_workspace(target: "Workspace", source: "Workspace") -> "Workspace":
    """Refresh an instance while preserving object references held by callers."""

    def merge_list(current: list[dict], incoming: list[dict], key: str) -> list[dict]:
        existing = {str(item.get(key)): item for item in current if item.get(key) is not None}
        merged = []
        for value in incoming:
            old = existing.get(str(value.get(key)))
            if old is None:
                merged.append(value)
                continue
            if "planned_tests" in value and isinstance(value.get("planned_tests"), list):
                planned = merge_list(
                    list(old.get("planned_tests") or []),
                    list(value.get("planned_tests") or []),
                    "id",
                )
                old.clear()
                old.update(value)
                old["planned_tests"] = planned
            else:
                old.clear()
                old.update(value)
            merged.append(old)
        current[:] = merged
        return current

    list_keys = {
        "tables": "name", "joins": "name", "tiles": "id", "analyses": "id",
        "rulesets": "id", "documents": "id", "rcm": "id", "work_program": "id",
        "data_tests": "id", "observations": "id", "evidence_requests": "id",
        "findings": "id",
    }
    for attribute, key in list_keys.items():
        merge_list(getattr(target, attribute), getattr(source, attribute), key)
    for attribute in ("planning", "report", "dashboard_advice", "rcm_migration"):
        current = getattr(target, attribute)
        current.clear()
        current.update(getattr(source, attribute))
    for attribute, value in source.__dict__.items():
        if attribute not in list_keys and attribute not in {
            "planning", "report", "dashboard_advice", "rcm_migration",
        }:
            setattr(target, attribute, value)
    return target


def set_request_revision(revision: int) -> Token:
    return _request_revision.set({"revision": int(revision)})


def reset_request_revision(token: Token) -> None:
    _request_revision.reset(token)


def _legacy_method(value: object, test_refs: object = None) -> str:
    """Map the former free-text procedure method to the RCM vocabulary.

    The mapping is deliberately conservative. Ambiguous text that combines
    data work and inspection becomes ``hybrid``; unknown methods remain
    document inspection because legacy procedures were manual by default.
    """
    text = str(value or "").strip().lower()
    has_data = any(word in text for word in ("data", "analytic", "polars", "validation"))
    has_document = any(
        word in text
        for word in ("inspect", "document", "vouch", "trace", "inquiry", "walkthrough")
    )
    if has_data and has_document:
        return "hybrid"
    if "validation" in text:
        return "validation"
    if has_data:
        return "data_analytics"
    if "inquiry" in text or "walkthrough" in text:
        return "inquiry"
    if "unavailable" in text or "missing evidence" in text:
        return "evidence_unavailable"
    # Existing procedure test_refs only contain document tests.
    if test_refs:
        return "document_inspection"
    return "document_inspection"


def _planned_test_id(rcm_id: str, procedure_id: str) -> str:
    digest = hashlib.sha1(f"{rcm_id}\0{procedure_id}".encode("utf-8")).hexdigest()[:10]
    return f"PT-{digest.upper()}"


def _normalize_sampling(value: object) -> dict:
    if value not in (None, "") and not isinstance(value, dict):
        raise WorkspaceError("Planned-test sampling must be an object.")
    raw = dict(value or {})
    size = raw.get("size")
    if size not in (None, ""):
        try:
            size = int(size)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Planned-test sample size must be a positive integer.") from error
        if size < 1:
            raise WorkspaceError("Planned-test sample size must be a positive integer.")
    else:
        size = None
    try:
        seed = int(raw.get("seed", 42))
    except (TypeError, ValueError) as error:
        raise WorkspaceError("Planned-test sampling seed must be an integer.") from error
    return {
        "strategy": str(raw.get("strategy") or "judgmental"),
        "size": size,
        "seed": seed,
        "stratify_by": str(raw.get("stratify_by") or "").strip() or None,
    }


def _normalize_thresholds(value: object) -> dict:
    if value not in (None, "") and not isinstance(value, dict):
        raise WorkspaceError("Planned-test thresholds must be an object.")
    return dict(value or {})


def _normalize_planned_test(
    payload: dict,
    *,
    rcm_id: str,
    now: str,
    legacy_procedure_id: str | None = None,
) -> dict:
    title = str(payload.get("title") or payload.get("objective") or "").strip()
    objective = str(payload.get("objective") or title).strip()
    if not objective:
        raise WorkspaceError("Planned-test objective is required.")
    method = str(payload.get("method") or "document_inspection").strip().lower()
    if method not in PLANNED_TEST_METHODS:
        method = _legacy_method(method, payload.get("execution_refs") or payload.get("test_refs"))
    status = str(payload.get("status") or "not_ready").strip().lower()
    if status not in PLANNED_TEST_STATUSES:
        raise WorkspaceError("Unknown planned-test status.")
    conclusion = str(payload.get("control_conclusion") or "no_conclusion").strip().lower()
    if conclusion not in CONTROL_CONCLUSIONS:
        raise WorkspaceError("Unknown planned-test control conclusion.")
    procedure_id = legacy_procedure_id or str(payload.get("legacy_procedure_id") or "") or None
    item_id = str(
        payload.get("id")
        or (_planned_test_id(rcm_id, procedure_id) if procedure_id else f"PT-{uuid.uuid4().hex[:10].upper()}")
    )
    semantic_default = f"planned-test:{slugify(title or objective)}"
    evidence = payload.get("evidence_refs") or []
    from .evidence import normalize_many

    return {
        "id": item_id,
        "semantic_id": str(payload.get("semantic_id") or semantic_default),
        "title": title or objective,
        "objective": objective,
        "criteria": str(payload.get("criteria") or ""),
        "method": method,
        "steps": [str(step).strip() for step in (payload.get("steps") or []) if str(step).strip()],
        "expected_evidence": str(payload.get("expected_evidence") or ""),
        "sampling": _normalize_sampling(payload.get("sampling")),
        "thresholds": _normalize_thresholds(payload.get("thresholds")),
        "execution_refs": list(
            dict.fromkeys(
                str(ref).strip()
                for ref in (payload.get("execution_refs") or payload.get("test_refs") or [])
                if str(ref).strip()
            )
        ),
        "status": status,
        "result_summary": str(payload.get("result_summary") or ""),
        "conclusion": str(payload.get("conclusion") or ""),
        "control_conclusion": conclusion,
        "scope_limitations": str(payload.get("scope_limitations") or ""),
        "next_action": str(payload.get("next_action") or ""),
        "exception_count": max(0, int(payload.get("exception_count") or 0)),
        "open_exception_count": max(0, int(payload.get("open_exception_count") or 0)),
        "evidence_refs": normalize_many(evidence),
        "methodology_refs": list(payload.get("methodology_refs") or []),
        "finding_refs": [str(ref) for ref in (payload.get("finding_refs") or [])],
        "workflow_parent_sha1": str(payload.get("workflow_parent_sha1") or "") or None,
        "created_by": str(payload.get("created_by") or ("agent" if payload.get("agent_run_id") else "user")),
        "agent_run_id": payload.get("agent_run_id"),
        "updated": str(payload.get("updated") or now),
        **({"legacy_procedure_id": procedure_id} if procedure_id else {}),
    }


def _normalize_rcm_row(row: dict, *, now: str) -> dict:
    item = dict(row)
    item.setdefault("criteria", "")
    item.setdefault("criteria_refs", [])
    item.setdefault("control_owner", "")
    item.setdefault("planned_tests", [])
    item.setdefault("execution_rollup", {})
    item.setdefault("finding_refs", [])
    item.setdefault("evidence_refs", [])
    item.setdefault("prepared_by", None)
    item.setdefault("reviewed_by", None)
    item.setdefault("review_status", "draft")
    item.setdefault("updated", now)
    review = str(item.get("review_status") or "draft").lower()
    item["review_status"] = review if review in REVIEW_STATUSES else "draft"
    from .evidence import normalize_many

    item["evidence_refs"] = normalize_many(item.get("evidence_refs") or [])
    normalized = []
    seen: set[str] = set()
    for planned in item.get("planned_tests") or []:
        value = _normalize_planned_test(dict(planned), rcm_id=str(item.get("id") or ""), now=now)
        if value["id"] not in seen:
            normalized.append(value)
            seen.add(value["id"])
    item["planned_tests"] = normalized
    return item


def _migrate_legacy_planning(
    rcm_rows: list[dict], procedures: list[dict], *, from_version: int, now: str
) -> dict:
    """Project unambiguous legacy procedures into RCM planned tests.

    The old collection is retained verbatim for rollback. This projection is
    idempotent because migrated tests use deterministic IDs and record the
    source procedure ID.
    """
    known_rcm = {str(row.get("id")): row for row in rcm_rows}
    migrated: dict[str, dict] = {}
    review: list[dict] = []
    unassigned: list[dict] = []
    for procedure in procedures:
        procedure_id = str(procedure.get("id") or "")
        refs = list(dict.fromkeys(str(ref) for ref in (procedure.get("rcm_refs") or []) if str(ref)))
        valid = [ref for ref in refs if ref in known_rcm]
        if len(refs) == 1 and len(valid) == 1:
            row = known_rcm[valid[0]]
            planned_id = _planned_test_id(valid[0], procedure_id)
            existing = next(
                (
                    item
                    for item in row["planned_tests"]
                    if item.get("legacy_procedure_id") == procedure_id or item.get("id") == planned_id
                ),
                None,
            )
            if existing is None:
                planned = _normalize_planned_test(
                    {
                        **procedure,
                        "title": procedure.get("title") or procedure.get("objective"),
                        "method": _legacy_method(procedure.get("method"), procedure.get("test_refs")),
                        "execution_refs": procedure.get("test_refs") or [],
                        "control_conclusion": procedure.get("control_conclusion") or "no_conclusion",
                    },
                    rcm_id=valid[0],
                    now=now,
                    legacy_procedure_id=procedure_id,
                )
                row["planned_tests"].append(planned)
                existing = planned
            migrated[procedure_id] = {"rcm_id": valid[0], "planned_test_id": existing["id"]}
        elif not refs:
            unassigned.append({"procedure_id": procedure_id, "reason": "No RCM reference."})
        else:
            reason = "Procedure has multiple RCM references and requires auditor assignment."
            if not valid:
                reason = "Procedure references an RCM row that does not exist."
            review.append({"procedure_id": procedure_id, "rcm_refs": refs, "reason": reason})
    return {
        "from_schema_version": from_version,
        "migrated_procedures": migrated,
        "review_queue": review,
        "unassigned_procedures": unassigned,
        "legacy_work_program_retained": True,
    }


def _legacy_execution_parent(workspace: "Workspace", item: dict) -> tuple[str, str] | None:
    rcm_id = str(item.get("rcm_id") or "")
    planned_id = str(item.get("planned_test_id") or "")
    if rcm_id and planned_id:
        try:
            row, _planned = workspace.planned_test(planned_id)
        except WorkspaceError:
            return None
        return (rcm_id, planned_id) if row["id"] == rcm_id else None
    procedure_refs = [str(value) for value in item.get("procedure_refs") or []]
    targets = [
        workspace.rcm_migration.get("migrated_procedures", {}).get(value)
        for value in procedure_refs
    ]
    targets = [value for value in targets if value]
    if len(targets) == 1 and len(procedure_refs) == 1:
        return targets[0]["rcm_id"], targets[0]["planned_test_id"]
    rcm_refs = [str(value) for value in item.get("rcm_refs") or []]
    if len(rcm_refs) == 1:
        row = next((value for value in workspace.rcm if value.get("id") == rcm_refs[0]), None)
        if row is not None and len(row.get("planned_tests") or []) == 1:
            return row["id"], row["planned_tests"][0]["id"]
    return None


def _migrate_legacy_execution(workspace: "Workspace") -> None:
    """Project only unambiguously linked saved analyses/rules into Data Tests."""
    from . import data_tests

    migrated = dict(workspace.rcm_migration.get("migrated_execution") or {})
    unassigned = []
    sources = [
        *(('analysis', item) for item in workspace.analyses),
        *(('ruleset', item) for item in workspace.rulesets),
    ]
    existing_sources = {
        (item.get("legacy_source_kind"), item.get("legacy_source_id"))
        for item in workspace.data_tests
    }
    for kind, source in sources:
        source_id = str(source.get("id") or "")
        if not source_id or (kind, source_id) in existing_sources:
            continue
        parent = _legacy_execution_parent(workspace, source)
        if parent is None:
            unassigned.append({
                "kind": kind, "id": source_id,
                "reason": "No unambiguous RCM planned-test parent.",
            })
            continue
        engine = "validation" if kind == "ruleset" else (
            "polars" if source.get("kind") == "python" else "analytics"
        )
        table_refs = [str(source.get("table") or "")]
        spec = dict(source.get("spec") or {})
        if kind == "ruleset":
            spec = {"rules": source.get("rules") or []}
        elif engine == "analytics":
            spec = {"test_id": spec.get("test") or spec.get("test_id"), "params": spec.get("params") or {}}
        try:
            refs = data_tests._table_refs(workspace, table_refs)
            normalized_spec, warnings = data_tests._validate_spec(workspace, engine, refs, spec)
        except WorkspaceError as error:
            unassigned.append({"kind": kind, "id": source_id, "reason": str(error)})
            continue
        digest = hashlib.sha1(f"{kind}\0{source_id}".encode("utf-8")).hexdigest()[:10]
        now = str(source.get("updated") or source.get("created") or workspace._updated_now())
        item = {
            "id": f"DAT-{digest.upper()}",
            "semantic_id": f"datatest:migrated-{kind}:{slugify(source_id)}",
            "rcm_id": parent[0], "planned_test_id": parent[1],
            "title": str(source.get("title") or f"Migrated {kind} {source_id}"),
            "objective": str(source.get("objective") or source.get("title") or f"Reperform {kind} {source_id}"),
            "engine": engine, "table_refs": refs, "spec": normalized_spec,
            "status": "ready", "semantic_warnings": warnings,
            "last_run": None, "runs": [], "auditor_disposition": "pending",
            "evidence_refs": [], "created_by": source.get("created_by") or "user",
            "agent_run_id": source.get("agent_run_id"), "created": now, "updated": now,
            "legacy_source_kind": kind, "legacy_source_id": source_id,
        }
        workspace.data_tests.append(item)
        data_tests._link(workspace, item)
        migrated[f"{kind}:{source_id}"] = item["id"]
    workspace.rcm_migration["migrated_execution"] = migrated
    workspace.rcm_migration["unassigned_tests"] = unassigned


def _validate_test_refs(workspace: "Workspace", refs: object) -> list[str]:
    if not refs:
        return []
    # A bare string is malformed input (the model occasionally returns a free-text
    # citation here). Iterating it would split it into single characters, so wrap
    # it as one reference rather than exploding it.
    if isinstance(refs, str):
        refs = [refs]
    elif not isinstance(refs, (list, tuple)):
        raise WorkspaceError("test_refs must be a list of document-test references.")
    from . import doc_tests
    values: list[str] = []
    for ref in refs:
        value = str(ref).strip()
        if not value:
            continue
        kind, separator, item_id = value.partition(":")
        # test_refs holds document-test links only; they are added automatically
        # when a doc test is linked to a row. Drop anything that is not a doctest
        # reference (e.g. a hallucinated citation string) instead of persisting it.
        if kind != "doctest":
            continue
        if not separator or not item_id or not doc_tests.exists(workspace, item_id):
            raise WorkspaceError(f"Document test reference '{value}' does not exist.")
        values.append(value)
    return values


def _normalized_table_name(text: str) -> str:
    if not str(text or "").strip():
        return ""
    return slugify(text).replace("-", "_")


def _is_bare_table_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _python_reference_token_positions(
    code: str, old: str, new: str
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Find exact table-name references in saved Python without reparsing text.

    The string positions cover explicit ``tables["old"]`` and
    ``tables.get("old")`` lookups. Bare-variable positions are only returned
    when the old name is used as an injected table variable, not assigned inside
    the snippet; that avoids changing a user's local alias.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return set(), set()

    string_positions: set[tuple[int, int]] = set()
    bare_positions: set[tuple[int, int]] = set()
    assigned_old = False

    def is_tables_name(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "tables"

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id == old and isinstance(node.ctx, (ast.Store, ast.Del)):
                assigned_old = True
            continue
        if isinstance(node, ast.arg) and node.arg == old:
            assigned_old = True
            continue
        if isinstance(node, ast.ExceptHandler) and node.name == old:
            assigned_old = True
            continue
        if isinstance(node, ast.Subscript) and is_tables_name(node.value):
            if isinstance(node.slice, ast.Constant) and node.slice.value == old:
                string_positions.add((node.slice.lineno, node.slice.col_offset))
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and is_tables_name(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == old
        ):
            string_positions.add((node.args[0].lineno, node.args[0].col_offset))

    if (
        _is_bare_table_identifier(old)
        and _is_bare_table_identifier(new)
        and not assigned_old
    ):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and node.id == old
                and isinstance(node.ctx, ast.Load)
            ):
                bare_positions.add((node.lineno, node.col_offset))

    return string_positions, bare_positions


def _rewrite_python_table_references(code: str, old: str, new: str) -> tuple[str, bool]:
    if not code or old == new:
        return code, False

    string_positions, bare_positions = _python_reference_token_positions(code, old, new)
    if not string_positions and not bare_positions:
        return code, False

    changed = False
    rewritten = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        for token in tokens:
            value = token.string
            if token.type == tokenize.STRING and token.start in string_positions:
                value = repr(new)
                changed = True
            elif token.type == tokenize.NAME and token.start in bare_positions:
                value = new
                changed = True
            rewritten.append(
                tokenize.TokenInfo(token.type, value, token.start, token.end, token.line)
            )
    except tokenize.TokenError:
        return code, False

    return tokenize.untokenize(rewritten), changed


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.definition_path = self.root / "workspace.json"
        definition = json.loads(self.definition_path.read_text(encoding="utf-8"))
        self.schema_version = int(definition.get("schema_version") or 1)
        self.revision = int(definition.get("revision") or 0)
        self.id: str = definition.get("id") or self.root.name
        self.name: str = definition.get("name") or self.id
        self.description: str = definition.get("description") or ""
        self.created: str = definition.get("created") or ""
        self.tables: list[dict] = list(definition.get("tables") or [])
        self.joins: list[dict] = list(definition.get("joins") or [])
        self.tiles: list[dict] = list(definition.get("tiles") or [])
        self.analyses: list[dict] = list(definition.get("analyses") or [])
        self.rulesets: list[dict] = list(definition.get("rulesets") or [])
        # Full-audit-cycle records hydrate defensively so every pre-extension
        # workspace remains readable without a migration step.
        stored_documents = list(definition.get("documents") or [])
        # Document versions were removed in favor of explicit in-place
        # replacement. Keep only the current member of each legacy chain and
        # hydrate it into the simpler document shape. A later save persists the
        # migration while old evidence IDs remain visibly unavailable.
        superseded_ids = {
            str(item.get("supersedes"))
            for item in stored_documents
            if item.get("supersedes")
        }
        self.documents: list[dict] = []
        for stored in stored_documents:
            if str(stored.get("id")) in superseded_ids:
                continue
            document = dict(stored)
            document.pop("version", None)
            document.pop("supersedes", None)
            document.setdefault("updated", None)
            self.documents.append(document)
        self.planning: dict = {
            "context": {
                "objective": "",
                "entity": "",
                "period": "",
                "scope": "",
                "materiality": "",
                "key_contacts": "",
                "background_notes": "",
                "interview_answers": {},
            },
            "apm_markdown": "",
            "created_by": "user",
            "agent_run_id": None,
            "updated": None,
            **dict(definition.get("planning") or {}),
        }
        self.planning["context"] = {
            "objective": "",
            "entity": "",
            "period": "",
            "scope": "",
            "materiality": "",
            "key_contacts": "",
            "background_notes": "",
            "interview_answers": {},
            **dict(self.planning.get("context") or {}),
        }
        hydrated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.rcm: list[dict] = [
            _normalize_rcm_row(dict(row), now=hydrated_at)
            for row in (definition.get("rcm") or [])
        ]
        self.work_program: list[dict] = list(definition.get("work_program") or [])
        derived_migration = _migrate_legacy_planning(
            self.rcm,
            self.work_program,
            from_version=self.schema_version,
            now=hydrated_at,
        )
        stored_migration = dict(definition.get("rcm_migration") or {})
        self.rcm_migration: dict = {**stored_migration, **derived_migration}
        self.data_tests: list[dict] = list(definition.get("data_tests") or [])
        self.observations: list[dict] = list(definition.get("observations") or [])
        self.evidence_requests: list[dict] = list(definition.get("evidence_requests") or [])
        self.findings: list[dict] = list(definition.get("findings") or [])
        self.dashboard_advice: dict = dict(definition.get("dashboard_advice") or {})
        # Legacy evidence strings remain represented through a typed wrapper;
        # all subsequent writes validate the durable anchor shape.
        from .evidence import normalize_many
        for item in self.work_program:
            item["evidence_refs"] = normalize_many(item.get("evidence_refs") or [])
            item.setdefault("methodology_refs", [])
        for item in self.findings:
            item["evidence_refs"] = normalize_many(item.get("evidence_refs") or [])
            item.setdefault("rcm_refs", [])
            item.setdefault("procedure_refs", [])
            item.setdefault("planned_test_refs", [])
            item.setdefault("execution_refs", [])
            item.setdefault("cause_pending", False)
            item.setdefault("severity_rationale", "")
            # Legacy/manual origin is not equivalent to a formal auditor
            # confirmation. Unsupported records stay visible as drafts.
            item.setdefault("auditor_confirmed", False)
            for procedure_id in item.get("procedure_refs") or []:
                target = self.rcm_migration["migrated_procedures"].get(str(procedure_id))
                if target and target["planned_test_id"] not in item["planned_test_refs"]:
                    item["planned_test_refs"].append(target["planned_test_id"])
            item.pop("status", None)
            item.setdefault("source", "manual")
        self.report: dict = dict(definition.get("report") or {})
        _migrate_legacy_execution(self)
        self.planning.pop("status", None)
        self.report.pop("status", None)

    # ------------------------------------------------------------- persistence
    @property
    def data_dir(self) -> Path:
        return self.root / "Data"

    def save(self, *, expected_revision: int | None = None) -> None:
        """Persist with compare-and-swap revision protection.

        Existing callers do not need to pass ``expected_revision``: the
        revision loaded with this instance is used.  This turns stale whole-file
        writes into explicit conflicts instead of silently losing another API
        or workflow mutation.
        """
        with workspace_write_lock(self.root):
            self._save_locked(expected_revision=expected_revision)

    def _save_locked(self, *, expected_revision: int | None = None) -> None:
        request_state = _request_revision.get()
        if request_state is not None:
            expected = int(request_state["revision"])
        elif expected_revision is not None:
            expected = int(expected_revision)
        else:
            expected = self.revision
        current = 0
        if self.definition_path.exists():
            try:
                current = int(
                    json.loads(self.definition_path.read_text(encoding="utf-8")).get("revision")
                    or 0
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                current = 0
        if current != expected:
            raise WorkspaceConflict(expected, current)
        self.revision = current + 1
        if request_state is not None:
            request_state["revision"] = self.revision
        definition = {
            "schema_version": SCHEMA_VERSION,
            "revision": self.revision,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created": self.created,
            "tables": self.tables,
            "joins": self.joins,
            "tiles": self.tiles,
            "analyses": self.analyses,
            "rulesets": self.rulesets,
            "documents": self.documents,
            "planning": self.planning,
            "rcm": self.rcm,
            "data_tests": self.data_tests,
            "observations": self.observations,
            "evidence_requests": self.evidence_requests,
            "work_program": self.work_program,
            "rcm_migration": self.rcm_migration,
            "findings": self.findings,
            "report": self.report,
            "dashboard_advice": self.dashboard_advice,
        }
        before = None
        if self.definition_path.exists():
            try:
                before = json.loads(self.definition_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                before = None
        write_json_atomic(self.definition_path, definition)
        # Debug telemetry is best-effort and must never make the auditor's
        # primary workspace write fail. Import lazily to avoid a storage cycle.
        try:
            from . import debug_store
            with debug_store.trace_context(workspace_root=str(self.root)):
                debug_store.record_workspace_save(self.id, before, definition)
        except Exception:
            pass

    # ------------------------------------------------------------------ tables
    def table_names(self) -> list[str]:
        return [t["name"] for t in self.tables] + [j["name"] for j in self.joins]

    def _table_entry(self, name: str) -> dict | None:
        return next((t for t in self.tables if t["name"] == name), None)

    def _join_entry(self, name: str) -> dict | None:
        return next((j for j in self.joins if j["name"] == name), None)

    def add_table(self, filename: str, content: bytes) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in loader.SUPPORTED_SUFFIXES:
            raise WorkspaceError(
                f"Unsupported file type '{suffix}'. "
                f"Supported: {', '.join(loader.SUPPORTED_SUFFIXES)}"
            )

        base = slugify(Path(filename).stem).replace("-", "_")
        name = base
        counter = 1
        while name in self.table_names():
            counter += 1
            name = f"{base}_{counter}"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / f"{name}{suffix}"
        target.write_bytes(content)

        # Fail fast on unreadable files: parse once now, before registering.
        try:
            frame = loader.read_table(target)
        except Exception as error:
            target.unlink(missing_ok=True)
            raise WorkspaceError(f"Could not read '{filename}': {error}") from error
        if frame.width == 0:
            target.unlink(missing_ok=True)
            raise WorkspaceError(f"'{filename}' appears to be empty.")

        entry = {"name": name, "file": target.name, "source": filename}
        self.tables.append(entry)
        self.save()
        return entry

    def replace_table(self, name: str, filename: str, content: bytes) -> dict:
        """Swap the data behind an existing base table, keeping its ``name``.

        Saved queries/tiles/analyses/joins link to a table by name and recompute
        live, so replacing the file content updates every one of them at once.
        The new file is validated in a temp file first; only a successful parse
        commits the swap, so a bad upload never destroys the existing data.
        """
        entry = self._table_entry(name)
        if entry is None:
            join = self._join_entry(name)
            if join is not None:
                raise WorkspaceError(f"'{name}' is a join, not a data table.")
            raise WorkspaceError(f"No table named '{name}'.")

        suffix = Path(filename).suffix.lower()
        if suffix not in loader.SUPPORTED_SUFFIXES:
            raise WorkspaceError(
                f"Unsupported file type '{suffix}'. "
                f"Supported: {', '.join(loader.SUPPORTED_SUFFIXES)}"
            )

        # Snapshot the current columns for the schema diff (best-effort: the old
        # file may itself be unreadable, in which case there's nothing to diff).
        try:
            old_columns = self.get_frame(name).columns
        except Exception:
            old_columns = []

        # Validate the new content in a temp file before touching the live one.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.data_dir / f".{name}.upload{suffix}"
        tmp.write_bytes(content)
        try:
            frame = loader.read_table(tmp)
        except Exception as error:
            loader.clear_cache(tmp)
            tmp.unlink(missing_ok=True)
            raise WorkspaceError(f"Could not read '{filename}': {error}") from error
        if frame.width == 0:
            loader.clear_cache(tmp)
            tmp.unlink(missing_ok=True)
            raise WorkspaceError(f"'{filename}' appears to be empty.")
        loader.clear_cache(tmp)  # drop the temp path's entry before committing

        old_path = self.data_dir / entry["file"]
        new_path = self.data_dir / f"{name}{suffix}"
        loader.clear_cache(old_path)
        os.replace(tmp, new_path)
        if new_path != old_path:
            # Format changed (e.g. csv → xlsx): retire the old file.
            old_path.unlink(missing_ok=True)
            entry["file"] = new_path.name
        loader.clear_cache(new_path)
        self._clear_profile_cache(name)

        entry["source"] = filename
        self.save()

        new_columns = self.get_frame(name).columns
        return {
            "name": name,
            "file": entry["file"],
            "source": filename,
            "added_columns": [c for c in new_columns if c not in old_columns],
            "removed_columns": [c for c in old_columns if c not in new_columns],
        }

    def rename_table(self, name: str, new_name: str) -> dict:
        """Rename a base table or join and migrate saved references.

        Stored work links by table name, so this updates the workspace metadata
        in one commit: joins that depend on the table, saved dashboard tiles,
        saved analyses, and validation rulesets. Python snippets are edited
        conservatively for exact table lookups and unshadowed bare table names.
        """
        entry = self._table_entry(name)
        join = self._join_entry(name)
        if entry is None and join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        target = _normalized_table_name(new_name)
        if not target:
            raise WorkspaceError("Table name is required.")
        if target == name:
            return {
                "old_name": name,
                "name": target,
                "updated": {
                    "joins": 0,
                    "tiles": 0,
                    "analyses": 0,
                    "rulesets": 0,
                    "python_snippets": 0,
                },
            }
        if target in self.table_names():
            raise WorkspaceError(f"A table named '{target}' already exists.")

        updated = {
            "joins": 0,
            "tiles": 0,
            "analyses": 0,
            "rulesets": 0,
            "data_tests": 0,
            "python_snippets": 0,
        }

        if entry is not None:
            entry["name"] = target
        else:
            join["name"] = target

        for existing_join in self.joins:
            touched = False
            if existing_join.get("left") == name:
                existing_join["left"] = target
                touched = True
            if existing_join.get("right") == name:
                existing_join["right"] = target
                touched = True
            if touched:
                updated["joins"] += 1

        for collection_name, collection in (
            ("tiles", self.tiles),
            ("analyses", self.analyses),
        ):
            for item in collection:
                touched = False
                if item.get("table") == name:
                    item["table"] = target
                    touched = True
                if item.get("kind") == "python" and isinstance(item.get("spec"), dict):
                    code = str(item["spec"].get("code") or "")
                    rewritten, changed = _rewrite_python_table_references(code, name, target)
                    if changed:
                        item["spec"]["code"] = rewritten
                        updated["python_snippets"] += 1
                        touched = True
                if touched:
                    updated[collection_name] += 1

        for ruleset in self.rulesets:
            if ruleset.get("table") == name:
                ruleset["table"] = target
                updated["rulesets"] += 1

        for data_test in self.data_tests:
            original_refs = list(data_test.get("table_refs") or [])
            touched = name in original_refs
            data_test["table_refs"] = [
                target if value == name else value
                for value in original_refs
            ]
            if data_test.get("engine") == "polars":
                code = str((data_test.get("spec") or {}).get("code") or "")
                rewritten, changed = _rewrite_python_table_references(code, name, target)
                if changed:
                    data_test["spec"]["code"] = rewritten
                    updated["python_snippets"] += 1
                    touched = True
            if touched:
                data_test["status"] = "ready"
                data_test["updated"] = self._updated_now()
                updated["data_tests"] += 1

        self._clear_profile_cache(name)
        self._clear_profile_cache(target)
        self.save()
        if not updated["data_tests"]:
            # Preserve the pre-schema-v2 response shape for callers that compare
            # the legacy counters exactly; expose the new counter when relevant.
            updated.pop("data_tests")
        return {"old_name": name, "name": target, "updated": updated}

    def remove_table(self, name: str) -> None:
        entry = self._table_entry(name)
        join = self._join_entry(name)
        if entry is None and join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        dependents = [
            j["name"]
            for j in self.joins
            if j["name"] != name and name in (j.get("left"), j.get("right"))
        ]
        if dependents:
            raise WorkspaceError(
                f"'{name}' is used by join(s): {', '.join(dependents)}. Remove those first."
            )
        data_test_refs = [
            item.get("id")
            for item in self.data_tests
            if name in (item.get("table_refs") or [])
        ]
        if data_test_refs:
            raise WorkspaceError(
                f"'{name}' is used by Data Test(s): {', '.join(data_test_refs)}. Reassign those first."
            )

        if entry is not None:
            path = self.data_dir / entry["file"]
            if path.exists():
                loader.clear_cache(path)
            path.unlink(missing_ok=True)
            self.tables.remove(entry)
        if join is not None:
            self.joins.remove(join)
        self._clear_profile_cache(name)
        self.save()

    # ------------------------------------------------------------------- joins
    def add_join(self, spec: dict) -> dict:
        name = slugify(spec.get("name") or "").replace("-", "_")
        if not name:
            raise WorkspaceError("Join name is required.")
        if name in self.table_names():
            raise WorkspaceError(f"A table named '{name}' already exists.")

        how = spec.get("how") or "left"
        if how not in JOIN_TYPES:
            raise WorkspaceError(f"Unknown join type '{how}'.")

        left, right = spec.get("left"), spec.get("right")
        for side in (left, right):
            if side not in self.table_names():
                raise WorkspaceError(f"Unknown table '{side}'.")
        if name in (left, right):
            raise WorkspaceError("A join cannot reference itself.")

        left_columns = self.get_frame(left).columns
        right_columns = self.get_frame(right).columns
        left_on = resolve_columns(
            spec.get("left_on"), left_columns, table=left, error_type=WorkspaceError
        )
        right_on = resolve_columns(
            spec.get("right_on"), right_columns, table=right, error_type=WorkspaceError
        )
        if how != "cross":
            if not left_on or len(left_on) != len(right_on):
                raise WorkspaceError("Join keys are required and must pair up.")

        entry = _apply_provenance(
            {
                "name": name,
                "left": left,
                "right": right,
                "how": how,
                "left_on": left_on,
                "right_on": right_on,
            },
            spec,
        )
        # Validate by executing once before persisting.
        self.joins.append(entry)
        try:
            self.get_frame(name)
        except Exception:
            self.joins.remove(entry)
            raise
        self.save()
        return entry

    def remove_join(self, name: str) -> None:
        entry = self._join_entry(name)
        if entry is None:
            raise WorkspaceError(f"No join named '{name}'.")
        self.remove_table(name)

    # ------------------------------------------------------------------- tiles
    # A tile pins a *spec* (a query or an analytics run), never data: the
    # dashboard recomputes tiles on load, so it stays live when files change
    # and every tile is reproducible.
    def add_tile(self, payload: dict) -> dict:
        kind = payload.get("kind")
        if kind not in ("query", "analytics", "python", "pivot", "validation"):
            raise WorkspaceError(
                "Tile kind must be 'query', 'pivot', 'analytics', 'python' or 'validation'."
            )
        table = payload.get("table")
        # Python tiles carry their own code and may reference any table(s), so
        # a bound table is optional (and only used as a label) for them.
        if kind == "python":
            table = table if table in self.table_names() else None
        elif table not in self.table_names():
            raise WorkspaceError(f"Unknown table '{table}'.")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise WorkspaceError("Tile title is required.")
        if kind == "python" and not str((payload.get("spec") or {}).get("code") or "").strip():
            raise WorkspaceError("A Python tile needs code.")

        tile = _apply_provenance(
            {
                "id": str(payload.get("id") or uuid.uuid4().hex[:10]),
                "title": title,
                "kind": kind,
                "table": table,
                "spec": dict(payload.get("spec") or {}),
                "viz": dict(payload.get("viz") or {"type": "table"}),
                "note": str(payload.get("note") or "").strip(),
                "created": date.today().isoformat(),
                **{
                    key: payload[key]
                    for key in (
                        "data_test_id", "rcm_id", "planned_test_id", "result_ref"
                    )
                    if payload.get(key)
                },
            },
            payload,
        )
        self.tiles.append(tile)
        self.save()
        return tile

    def _tile(self, tile_id: str) -> dict:
        tile = next((t for t in self.tiles if t["id"] == tile_id), None)
        if tile is None:
            raise WorkspaceError("Tile not found.")
        return tile

    def update_tile(self, tile_id: str, changes: dict) -> dict:
        tile = self._tile(tile_id)
        _user_touch(tile)
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise WorkspaceError("Tile title is required.")
            tile["title"] = title
        if "note" in changes:
            tile["note"] = str(changes["note"] or "").strip()
        if "viz" in changes and isinstance(changes["viz"], dict):
            tile["viz"] = dict(changes["viz"])
        if "move" in changes:
            step = int(changes["move"])
            index = self.tiles.index(tile)
            target = max(0, min(len(self.tiles) - 1, index + step))
            self.tiles.insert(target, self.tiles.pop(index))
        self.save()
        return tile

    def remove_tile(self, tile_id: str) -> None:
        self.tiles.remove(self._tile(tile_id))
        self.save()

    # --------------------------------------------------------------- analyses
    # A saved analysis is the working-set sibling of a tile: same spec-not-data
    # model (recomputed live), but it lives in the Analysis tab's rail rather
    # than the dashboard. It comes from either the predefined library
    # (kind 'analytics') or AI-assisted code (kind 'python'). Pinning promotes
    # a copy to a dashboard tile; the two collections stay independent.
    def add_analysis(self, payload: dict) -> dict:
        kind = payload.get("kind")
        if kind not in ("analytics", "python"):
            raise WorkspaceError("Analysis kind must be 'analytics' or 'python'.")
        table = payload.get("table")
        # Python analyses carry their own code and may reference any table(s),
        # so a bound table is optional (and only a label) for them.
        if kind == "python":
            table = table if table in self.table_names() else None
        elif table not in self.table_names():
            raise WorkspaceError(f"Unknown table '{table}'.")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise WorkspaceError("Analysis title is required.")
        if kind == "python" and not str((payload.get("spec") or {}).get("code") or "").strip():
            raise WorkspaceError("A Python analysis needs code.")

        analysis = _apply_provenance(
            {
                "id": str(payload.get("id") or uuid.uuid4().hex[:10]),
                "title": title,
                "kind": kind,
                "table": table,
                "spec": dict(payload.get("spec") or {}),
                "viz": dict(payload.get("viz") or {"type": "table"}),
                "note": str(payload.get("note") or "").strip(),
                "source": payload.get("source") or ("ai" if kind == "python" else "library"),
                "created": date.today().isoformat(),
            },
            payload,
        )
        self.analyses.append(analysis)
        self.save()
        return analysis

    def _analysis(self, analysis_id: str) -> dict:
        analysis = next((a for a in self.analyses if a["id"] == analysis_id), None)
        if analysis is None:
            raise WorkspaceError("Analysis not found.")
        return analysis

    def update_analysis(self, analysis_id: str, changes: dict) -> dict:
        analysis = self._analysis(analysis_id)
        _user_touch(analysis)
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise WorkspaceError("Analysis title is required.")
            analysis["title"] = title
        if "note" in changes:
            analysis["note"] = str(changes["note"] or "").strip()
        if "viz" in changes and isinstance(changes["viz"], dict):
            analysis["viz"] = dict(changes["viz"])
        # Unlike a tile, an analysis is an editing surface: params (library) and
        # code (AI) are re-saved by rewriting its spec.
        if "spec" in changes and isinstance(changes["spec"], dict):
            if analysis["kind"] == "python" and not str(changes["spec"].get("code") or "").strip():
                raise WorkspaceError("A Python analysis needs code.")
            analysis["spec"] = dict(changes["spec"])
        self.save()
        return analysis

    def remove_analysis(self, analysis_id: str) -> None:
        self.analyses.remove(self._analysis(analysis_id))
        self.save()

    # ---------------------------------------------------------------- rulesets
    # A rule set is the validation sibling of an analysis: field-wise checks
    # bound to a table by name, stored as a spec and recomputed live — so a
    # replaced or refreshed table re-validates with the same saved rules.
    def _normalize_rules(self, rules: list, table: str | None = None) -> list[dict]:
        from . import validation

        normalized = []
        for rule in rules or []:
            check = validation.canonical_check_id((rule or {}).get("check"))
            meta = validation.CHECKS.get(check)
            if meta is None:
                raise WorkspaceError(f"Unknown check '{check}'.")
            column = str(rule.get("column") or "").strip() or None
            if meta["scope"] == "column" and not column:
                raise WorkspaceError(f"Check '{check}' needs a column.")
            severity = rule.get("severity")
            normalized.append(
                {
                    "id": rule.get("id") or uuid.uuid4().hex[:10],
                    "column": column if meta["scope"] == "column" else None,
                    "check": check,
                    "params": dict(rule.get("params") or {}),
                    "severity": severity if severity in validation.SEVERITIES else "fail",
                    "enabled": rule.get("enabled") is not False,
                }
            )
        if table in self.table_names():
            normalized = validation.canonicalize_rules(
                self.get_frame(table), normalized, resolve=self.get_frame, strict=False
            )
        return normalized

    def add_ruleset(self, payload: dict) -> dict:
        table = payload.get("table")
        if table not in self.table_names():
            raise WorkspaceError(f"Unknown table '{table}'.")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise WorkspaceError("Rule set title is required.")
        ruleset = _apply_provenance(
            {
                "id": str(payload.get("id") or uuid.uuid4().hex[:10]),
                "title": title,
                "table": table,
                "rules": self._normalize_rules(payload.get("rules") or [], table),
                "note": str(payload.get("note") or "").strip(),
                "created": date.today().isoformat(),
            },
            payload,
        )
        self.rulesets.append(ruleset)
        self.save()
        return ruleset

    def _ruleset(self, ruleset_id: str) -> dict:
        ruleset = next((r for r in self.rulesets if r["id"] == ruleset_id), None)
        if ruleset is None:
            raise WorkspaceError("Rule set not found.")
        return ruleset

    def update_ruleset(self, ruleset_id: str, changes: dict) -> dict:
        ruleset = self._ruleset(ruleset_id)
        _user_touch(ruleset)
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise WorkspaceError("Rule set title is required.")
            ruleset["title"] = title
        if "note" in changes:
            ruleset["note"] = str(changes["note"] or "").strip()
        # Rebinding to another table is allowed even when some rule columns
        # don't exist there — missing columns degrade to per-rule errors at
        # run time, which is the point of re-running on evolving data.
        if "table" in changes:
            if changes["table"] not in self.table_names():
                raise WorkspaceError(f"Unknown table '{changes['table']}'.")
            ruleset["table"] = changes["table"]
        if "rules" in changes:
            ruleset["rules"] = self._normalize_rules(changes["rules"], ruleset["table"])
        elif "table" in changes:
            ruleset["rules"] = self._normalize_rules(
                ruleset.get("rules") or [], ruleset["table"]
            )
        self.save()
        return ruleset

    def remove_ruleset(self, ruleset_id: str) -> None:
        self.rulesets.remove(self._ruleset(ruleset_id))
        self.save()

    RUN_HISTORY_MAX = 20

    def record_run(self, ruleset_id: str, run: dict) -> list[dict]:
        """Append a summary-only entry (never row data) to the rule set's run
        history — enough for a 2025-vs-2026 trend without a second store of
        results. Only runs of the *saved* spec are recorded; draft runs are not
        evidence."""
        ruleset = self._ruleset(ruleset_id)
        runs = ruleset.setdefault("runs", [])
        runs.append(
            {
                "run_at": run["run_at"],
                "table": run["table"],
                "rows": run["rows"],
                "verdict": run["verdict"],
                "counts": dict(run["counts"]),
            }
        )
        del runs[: -self.RUN_HISTORY_MAX]
        self.save()
        return runs

    # -------------------------------------------------------------- provenance
    def find_semantic(self, collection: str, semantic_id: str) -> dict | None:
        """Find a saved item by its agent semantic id ('tiles', 'analyses',
        'rulesets', 'joins', 'rcm', or 'procedures'). Used by agent reruns to reconcile instead of
        duplicating outputs."""
        items = {
            "tiles": self.tiles,
            "analyses": self.analyses,
            "rulesets": self.rulesets,
            "joins": self.joins,
            "rcm": self.rcm,
            "planned_tests": [
                planned for row in self.rcm for planned in row.get("planned_tests") or []
            ],
            "data_tests": self.data_tests,
            "procedures": self.work_program,
            "work_program": self.work_program,
            "findings": self.findings,
        }.get(collection, [])
        return next((i for i in items if i.get("semantic_id") == semantic_id), None)

    # --------------------------------------------------------------- planning
    @staticmethod
    def _updated_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def update_planning(self, changes: dict, *, agent: bool = False) -> dict:
        allowed = {
            "context", "apm_markdown", "agent_run_id", "created_by",
            "workflow_basis_sha1",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise WorkspaceError(f"Unknown planning field: {sorted(unknown)[0]}.")
        if not agent and ({"agent_run_id", "created_by", "workflow_basis_sha1"} & set(changes)):
            raise WorkspaceError("Planning provenance is managed by the workbench.")
        apm_changed = (
            "apm_markdown" in changes
            and changes["apm_markdown"] != self.planning.get("apm_markdown")
        )
        if "context" in changes:
            context = changes["context"]
            if not isinstance(context, dict):
                raise WorkspaceError("Planning context must be an object.")
            self.planning["context"].update(context)
        for key in ("apm_markdown", "agent_run_id", "created_by", "workflow_basis_sha1"):
            if key in changes:
                self.planning[key] = changes[key]
        if not agent and apm_changed and self.planning.get("created_by") == "agent":
            self.planning["created_by"] = "user"
        self.planning["updated"] = self._updated_now()
        self.save()
        return self.planning

    def _planning_record(self, collection: list[dict], item_id: str, label: str) -> dict:
        item = next((row for row in collection if row.get("id") == item_id), None)
        if item is None:
            raise WorkspaceError(f"{label} '{item_id}' not found.")
        return item

    def add_rcm(self, payload: dict) -> dict:
        process = str(payload.get("process") or "").strip()
        risk = str(payload.get("risk") or "").strip()
        if not risk:
            raise WorkspaceError("RCM risk is required.")
        now = self._updated_now()
        item = {
            "id": str(payload.get("id") or f"RCM-{uuid.uuid4().hex[:6].upper()}"),
            "semantic_id": str(payload.get("semantic_id") or f"rcm:{slugify(process)}:{slugify(risk)}"),
            "created_by": "agent" if payload.get("agent_run_id") else "user",
            "agent_run_id": payload.get("agent_run_id"),
            "process": process,
            "risk": risk,
            "risk_rating": str(payload.get("risk_rating") or "medium").lower(),
            "assertion": str(payload.get("assertion") or ""),
            "control": str(payload.get("control") or ""),
            "control_type": str(payload.get("control_type") or ""),
            "control_owner": str(payload.get("control_owner") or ""),
            "criteria": str(payload.get("criteria") or ""),
            "criteria_refs": list(payload.get("criteria_refs") or []),
            "test_procedure": str(payload.get("test_procedure") or ""),
            "test_refs": _validate_test_refs(self, payload.get("test_refs") or []),
            "planned_tests": [],
            "execution_rollup": dict(payload.get("execution_rollup") or {}),
            "finding_refs": [str(ref) for ref in (payload.get("finding_refs") or [])],
            "evidence_refs": list(payload.get("evidence_refs") or []),
            "prepared_by": payload.get("prepared_by"),
            "reviewed_by": payload.get("reviewed_by"),
            "review_status": str(payload.get("review_status") or "draft"),
            "workflow_parent_sha1": str(payload.get("workflow_parent_sha1") or "") or None,
            "updated": now,
        }
        if item["risk_rating"] not in ("low", "medium", "high", "critical"):
            raise WorkspaceError("Risk rating must be low, medium, high, or critical.")
        item = _normalize_rcm_row(item, now=now)
        item["planned_tests"] = [
            _normalize_planned_test(dict(test), rcm_id=item["id"], now=now)
            for test in (payload.get("planned_tests") or [])
        ]
        self.rcm.append(item)
        self.save()
        return item

    def update_rcm(self, item_id: str, changes: dict, *, agent: bool = False) -> dict:
        item = self._planning_record(self.rcm, item_id, "RCM row")
        allowed = {
            "process", "risk", "risk_rating", "assertion", "control", "control_type",
            "control_owner", "criteria", "criteria_refs", "test_procedure", "test_refs",
            "evidence_refs", "prepared_by", "reviewed_by", "review_status",
            "workflow_parent_sha1",
        }
        if set(changes) - allowed or ("workflow_parent_sha1" in changes and not agent):
            raise WorkspaceError("Unknown RCM field.")
        if "risk_rating" in changes and changes["risk_rating"] not in ("low", "medium", "high", "critical"):
            raise WorkspaceError("Risk rating must be low, medium, high, or critical.")
        if "test_refs" in changes:
            changes = {**changes, "test_refs": _validate_test_refs(self, changes["test_refs"])}
        if "review_status" in changes and changes["review_status"] not in REVIEW_STATUSES:
            raise WorkspaceError("Unknown RCM review status.")
        from .evidence import normalize_many
        for key, value in changes.items():
            if key in ("test_refs", "criteria_refs"):
                item[key] = [str(ref) for ref in (value or [])]
            elif key == "evidence_refs":
                item[key] = normalize_many(value or [], require_hash=True)
            elif key in ("prepared_by", "reviewed_by"):
                item[key] = str(value).strip() if value not in (None, "") else None
            else:
                item[key] = str(value or "")
        if not agent:
            _user_touch(item)
        item["updated"] = self._updated_now()
        self.save()
        return item

    def remove_rcm(self, item_id: str) -> None:
        item = self._planning_record(self.rcm, item_id, "RCM row")
        self.rcm.remove(item)
        for procedure in self.work_program:
            procedure["rcm_refs"] = [ref for ref in procedure.get("rcm_refs", []) if ref != item_id]
        for test in self.data_tests:
            if test.get("rcm_id") == item_id:
                test["rcm_id"] = None
                test["planned_test_id"] = None
        for finding in self.findings:
            finding["rcm_refs"] = [ref for ref in finding.get("rcm_refs", []) if ref != item_id]
            planned_ids = {test.get("id") for test in item.get("planned_tests") or []}
            finding["planned_test_refs"] = [
                ref for ref in finding.get("planned_test_refs", []) if ref not in planned_ids
            ]
        self.save()

    def _planned_test_record(self, rcm_id: str, planned_test_id: str) -> tuple[dict, dict]:
        row = self._planning_record(self.rcm, rcm_id, "RCM row")
        planned = next(
            (item for item in row.get("planned_tests") or [] if item.get("id") == planned_test_id),
            None,
        )
        if planned is None:
            raise WorkspaceError(f"Planned test '{planned_test_id}' not found under RCM row '{rcm_id}'.")
        return row, planned

    def planned_test(self, planned_test_id: str) -> tuple[dict, dict]:
        for row in self.rcm:
            for planned in row.get("planned_tests") or []:
                if planned.get("id") == planned_test_id:
                    return row, planned
        raise WorkspaceError(f"Planned test '{planned_test_id}' not found.")

    def add_planned_test(self, rcm_id: str, payload: dict) -> dict:
        row = self._planning_record(self.rcm, rcm_id, "RCM row")
        item = _normalize_planned_test(payload, rcm_id=rcm_id, now=self._updated_now())
        if any(
            test.get("id") == item["id"] or test.get("semantic_id") == item["semantic_id"]
            for existing_row in self.rcm
            for test in existing_row.get("planned_tests") or []
        ):
            raise WorkspaceError("A planned test with that ID or semantic ID already exists.")
        row.setdefault("planned_tests", []).append(item)
        row["updated"] = self._updated_now()
        self.save()
        return item

    def update_planned_test(
        self, rcm_id: str, planned_test_id: str, changes: dict, *, agent: bool = False
    ) -> dict:
        row, current = self._planned_test_record(rcm_id, planned_test_id)
        immutable = {"id", "legacy_procedure_id"}
        allowed = set(current) - immutable
        unknown = set(changes) - allowed
        if unknown:
            raise WorkspaceError(f"Unknown planned-test field: {sorted(unknown)[0]}.")
        normalized = _normalize_planned_test(
            {**current, **changes}, rcm_id=rcm_id, now=self._updated_now()
        )
        normalized["id"] = current["id"]
        if current.get("legacy_procedure_id"):
            normalized["legacy_procedure_id"] = current["legacy_procedure_id"]
        if not agent and normalized.get("created_by") == "agent":
            normalized["created_by"] = "user"
        current.clear()
        current.update(normalized)
        row["updated"] = current["updated"]
        self.save()
        return current

    def remove_planned_test(self, rcm_id: str, planned_test_id: str) -> None:
        row, item = self._planned_test_record(rcm_id, planned_test_id)
        if item.get("execution_refs"):
            raise WorkspaceError("A planned test with linked execution artifacts cannot be removed.")
        row["planned_tests"].remove(item)
        for finding in self.findings:
            finding["planned_test_refs"] = [
                ref for ref in finding.get("planned_test_refs", []) if ref != planned_test_id
            ]
        row["updated"] = self._updated_now()
        self.save()

    def add_procedure(self, payload: dict) -> dict:
        from .evidence import normalize_many
        objective = str(payload.get("objective") or "").strip()
        if not objective:
            raise WorkspaceError("Procedure objective is required.")
        item = {
            "id": str(payload.get("id") or f"PROC-{uuid.uuid4().hex[:6].upper()}"),
            "semantic_id": str(payload.get("semantic_id") or f"procedure:{slugify(objective)}"),
            "created_by": "agent" if payload.get("agent_run_id") else "user",
            "agent_run_id": payload.get("agent_run_id"),
            "rcm_refs": [str(ref) for ref in (payload.get("rcm_refs") or [])],
            "objective": objective,
            "criteria": str(payload.get("criteria") or ""),
            "steps": [str(step) for step in (payload.get("steps") or []) if str(step).strip()],
            "method": str(payload.get("method") or ""),
            "expected_evidence": str(payload.get("expected_evidence") or ""),
            "test_refs": _validate_test_refs(self, payload.get("test_refs") or []),
            "evidence_refs": normalize_many(payload.get("evidence_refs") or [], require_hash=True),
            "methodology_refs": list(payload.get("methodology_refs") or []),
            "result_summary": str(payload.get("result_summary") or ""),
            "conclusion": str(payload.get("conclusion") or ""),
            "scope_limitations": str(payload.get("scope_limitations") or ""),
            "updated": self._updated_now(),
        }
        self.work_program.append(item)
        self._sync_legacy_procedure(item)
        self.save()
        return item

    def update_procedure(self, item_id: str, changes: dict, *, agent: bool = False) -> dict:
        from .evidence import normalize_many
        item = self._planning_record(self.work_program, item_id, "Procedure")
        allowed = {"rcm_refs", "objective", "criteria", "steps", "method", "expected_evidence", "test_refs", "evidence_refs", "methodology_refs", "result_summary", "conclusion", "scope_limitations"}
        if set(changes) - allowed:
            raise WorkspaceError("Unknown procedure field.")
        if "test_refs" in changes:
            changes = {**changes, "test_refs": _validate_test_refs(self, changes["test_refs"])}
        for key, value in changes.items():
            if key in ("rcm_refs", "steps", "test_refs"):
                item[key] = [str(entry) for entry in (value or [])]
            elif key == "evidence_refs":
                item[key] = normalize_many(value or [], require_hash=True)
            elif key == "methodology_refs":
                item[key] = list(value or [])
            else:
                item[key] = str(value or "")
        if not agent:
            _user_touch(item)
        item["updated"] = self._updated_now()
        self._sync_legacy_procedure(item)
        self.save()
        return item

    def remove_procedure(self, item_id: str) -> None:
        item = self._planning_record(self.work_program, item_id, "Procedure")
        self.work_program.remove(item)
        for row in self.rcm:
            row["planned_tests"] = [
                planned
                for planned in row.get("planned_tests") or []
                if planned.get("legacy_procedure_id") != item_id
                or planned.get("execution_refs")
            ]
        for finding in self.findings:
            finding["procedure_refs"] = [ref for ref in finding.get("procedure_refs", []) if ref != item_id]
        self.rcm_migration = _migrate_legacy_planning(
            self.rcm,
            self.work_program,
            from_version=SCHEMA_VERSION,
            now=self._updated_now(),
        )
        self.save()

    def _sync_legacy_procedure(self, procedure: dict) -> None:
        """Dual-write an unambiguous legacy procedure during the migration window."""
        procedure_id = str(procedure.get("id") or "")
        refs = list(dict.fromkeys(str(ref) for ref in (procedure.get("rcm_refs") or []) if str(ref)))
        # Remove an unexecuted projection if the legacy procedure was reassigned.
        for row in self.rcm:
            row["planned_tests"] = [
                planned
                for planned in row.get("planned_tests") or []
                if planned.get("legacy_procedure_id") != procedure_id
                or planned.get("execution_refs")
                or (len(refs) == 1 and refs[0] == row.get("id"))
            ]
        if len(refs) == 1:
            row = next((value for value in self.rcm if value.get("id") == refs[0]), None)
            if row is not None:
                planned_id = _planned_test_id(row["id"], procedure_id)
                existing = next(
                    (
                        planned
                        for planned in row.get("planned_tests") or []
                        if planned.get("legacy_procedure_id") == procedure_id
                        or planned.get("id") == planned_id
                    ),
                    None,
                )
                payload = {
                    **(existing or {}),
                    **procedure,
                    "id": planned_id,
                    "title": procedure.get("title") or procedure.get("objective"),
                    "method": _legacy_method(procedure.get("method"), procedure.get("test_refs")),
                    "execution_refs": [
                        *[
                            ref
                            for ref in ((existing or {}).get("execution_refs") or [])
                            if not str(ref).startswith("doctest:")
                        ],
                        *(procedure.get("test_refs") or []),
                    ],
                }
                normalized = _normalize_planned_test(
                    payload,
                    rcm_id=row["id"],
                    now=self._updated_now(),
                    legacy_procedure_id=procedure_id,
                )
                if existing is None:
                    row.setdefault("planned_tests", []).append(normalized)
                else:
                    existing.clear()
                    existing.update(normalized)
        self.rcm_migration = _migrate_legacy_planning(
            self.rcm,
            self.work_program,
            from_version=self.schema_version,
            now=self._updated_now(),
        )

    # ------------------------------------------------------------------ frames
    def get_frame(self, name: str, _seen: frozenset = frozenset()) -> pl.DataFrame:
        if name in _seen:
            raise WorkspaceError(f"Join '{name}' references itself in a cycle.")

        entry = self._table_entry(name)
        if entry is not None:
            return loader.read_table(self.data_dir / entry["file"])

        join = self._join_entry(name)
        if join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        seen = _seen | {name}
        left = self.get_frame(join["left"], seen)
        right = self.get_frame(join["right"], seen)
        if join["how"] == "cross":
            return left.join(right, how="cross")
        return left.join(
            right,
            how=join["how"],
            left_on=join["left_on"],
            right_on=join["right_on"],
            coalesce=True,
        )

    def _table_signature(self, name: str, _seen: frozenset = frozenset()) -> tuple:
        """A hashable fingerprint of a table's content: the source file's
        (size, mtime) for base tables, or the join spec plus both sides'
        signatures, recursively. Used to key the on-disk profile cache."""
        if name in _seen:
            raise WorkspaceError(f"Join '{name}' references itself in a cycle.")

        entry = self._table_entry(name)
        if entry is not None:
            return ("file", loader.file_signature(self.data_dir / entry["file"]))

        join = self._join_entry(name)
        if join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        seen = _seen | {name}
        return (
            "join",
            join["how"],
            tuple(join["left_on"]),
            tuple(join["right_on"]),
            self._table_signature(join["left"], seen),
            self._table_signature(join["right"], seen),
        )

    # ---------------------------------------------------------------- profile
    def _cache_dir(self) -> Path:
        return self.data_dir / loader.CACHE_DIRNAME

    def _profile_cache_path(self, name: str, sig: tuple) -> Path:
        digest = hashlib.sha1(repr(sig).encode()).hexdigest()[:16]
        return self._cache_dir() / f"{name}.{digest}.profile.json"

    def _clear_profile_cache(self, name: str) -> None:
        cache_dir = self._cache_dir()
        if not cache_dir.exists():
            return
        for stale in cache_dir.glob(f"{name}.*.profile.json"):
            stale.unlink(missing_ok=True)

    def get_profile(self, name: str) -> dict:
        """Column/dataset profile for a table, cached on disk by content
        signature — profiling a large frame is expensive and the result
        never changes until the underlying file (or join input) does."""
        sig = self._table_signature(name)
        cache_file = self._profile_cache_path(name, sig)
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        profile = profiler.profile_table(self.get_frame(name))
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(profile), encoding="utf-8")
            for stale in cache_file.parent.glob(f"{name}.*.profile.json"):
                if stale != cache_file:
                    stale.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort: an unwritable cache dir shouldn't break profiling
        return profile

    # ----------------------------------------------------------------- summary
    def summary(self) -> dict:
        tables = []
        for entry in self.tables:
            info = {"name": entry["name"], "kind": "file", "source": entry.get("source", entry["file"])}
            try:
                frame = self.get_frame(entry["name"])
                info.update(rows=frame.height, columns=frame.width, error=None)
            except Exception as error:
                info.update(rows=None, columns=None, error=str(error))
            tables.append(info)
        for join in self.joins:
            info = {
                "name": join["name"],
                "kind": "join",
                "source": f"{join['left']} {join['how']} join {join['right']}",
                "join": join,
            }
            try:
                frame = self.get_frame(join["name"])
                info.update(rows=frame.height, columns=frame.width, error=None)
            except Exception as error:
                info.update(rows=None, columns=None, error=str(error))
            tables.append(info)
        return {
            "id": self.id,
            "revision": self.revision,
            "name": self.name,
            "description": self.description,
            "created": self.created,
            "tables": tables,
            "tile_count": len(self.tiles),
            "document_count": len(self.documents),
            "finding_count": len(self.findings),
        }


# -------------------------------------------------------------------- registry
def list_workspaces() -> list[dict]:
    if not WORKSPACES_DIR.exists():
        return []
    items = []
    for folder in sorted(WORKSPACES_DIR.iterdir()):
        if not (folder / "workspace.json").exists():
            continue
        try:
            ws = Workspace(folder)
        except Exception:
            continue
        items.append(
            {
                "id": ws.id,
                "name": ws.name,
                "description": ws.description,
                "created": ws.created,
                "revision": ws.revision,
                "table_count": len(ws.tables) + len(ws.joins),
            }
        )
    return items


def load_workspace(workspace_id: str) -> Workspace:
    root = WORKSPACES_DIR / workspace_id
    if not (root / "workspace.json").exists():
        raise WorkspaceError(f"Workspace '{workspace_id}' not found.")
    from .workspace_transactions import recover_linked_writes

    recover_linked_writes(root)
    return Workspace(root)


def create_workspace(name: str, description: str = "") -> Workspace:
    name = str(name).strip()
    if not name:
        raise WorkspaceError("Workspace name is required.")
    workspace_id = slugify(name)
    root = WORKSPACES_DIR / workspace_id
    if root.exists():
        raise WorkspaceError(f"A workspace named '{workspace_id}' already exists.")
    (root / "Data").mkdir(parents=True)
    (root / "workspace.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "revision": 0,
                "id": workspace_id,
                "name": name,
                "description": str(description).strip(),
                "created": date.today().isoformat(),
                "tables": [],
                "joins": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return Workspace(root)


def delete_workspace(workspace_id: str) -> None:
    ws = load_workspace(workspace_id)
    for entry in ws.tables:
        path = ws.data_dir / entry["file"]
        if path.exists():
            loader.clear_cache(path)
    shutil.rmtree(ws.root, ignore_errors=True)
