"""Workspace model and storage.

A workspace is the unit of an engagement: a folder holding the auditor's data
files plus a JSON definition of the tables and joins built on them::

    Workspaces/<id>/
        workspace.json   ← engagement manifest + table/join definitions
        Data/            ← the uploaded data files
        Planning/        ← APM, planning context, and RCM row sidecars
        DataTests/       ← independently persisted Data Test definitions

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
from .text import counted, plural_word

SCHEMA_VERSION = 4
JOIN_TYPES = ("inner", "left", "full", "semi", "anti", "cross")

# Polars renames a right-hand column that collides with a left-hand one by
# appending this. It is only safe while the left side has no column already
# carrying it — which stops being true the moment a join is built on a join.
DEFAULT_JOIN_SUFFIX = "_right"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "item"


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON via a temp file + rename so a crash mid-write can never
    leave a truncated definition behind."""
    before_meta = None
    # The prior contents are read only to describe them for state telemetry, and
    # the recorder below ignores exactly the paths written most often (run.json,
    # workspace.json, and the Debug tree itself). Deciding that up front keeps a
    # hot save from parsing a record whose "before" is then discarded.
    if _traces_artifact_transitions(path) and path.exists():
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


def _traces_artifact_transitions(path: Path) -> bool:
    """False for writes that artifact-state telemetry deliberately ignores.

    Primary workspace/run writes have richer hooks of their own, and Debug
    writes are the telemetry itself and must never recursively trace themselves.
    """
    if "Debug" in path.parts or ".Transactions" in path.parts:
        return False
    if path.name == "workspace.json":
        return False
    if path.name == "run.json" and "AgentRuns" in path.parts:
        return False
    from . import debug_store

    return debug_store.state_enabled()


def _record_atomic_artifact_transition(path: Path, before: dict | None, payload: dict) -> None:
    if not _traces_artifact_transitions(path):
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


def join_suffix(
    left_columns: list[str],
    right_columns: list[str],
    right_on: list[str] | tuple[str, ...] = (),
    right_name: str = "",
) -> str:
    """A suffix that renames colliding right-hand columns without colliding again.

    ``invoice_data`` and ``po_data`` both carry ``VENDOR_ID``, so their join
    holds ``VENDOR_ID`` and ``VENDOR_ID_right``. Joining *that* frame to
    ``requisitions`` — which carries ``VENDOR_ID`` too — asks Polars to produce
    a second ``VENDOR_ID_right`` and the join fails outright. The default
    suffix is kept whenever it is free, so frames already materialized keep the
    column names they have; only a genuine second collision escalates, first to
    the right frame's own name and then to a counter.

    Join keys are excluded: a coalesced key contributes no separate right-hand
    column, so it can never be the collision.
    """
    left = set(left_columns)
    reserved = left | set(right_columns)
    keys = {str(value) for value in right_on or ()}
    colliding = [
        column for column in right_columns if column in left and column not in keys
    ]
    if not colliding:
        return DEFAULT_JOIN_SUFFIX
    slug = slugify(str(right_name)).replace("-", "_")
    options = [DEFAULT_JOIN_SUFFIX]
    if slug:
        options.append(f"_{slug}")
    options.extend(f"{DEFAULT_JOIN_SUFFIX}_{index}" for index in range(2, 20))
    for suffix in options:
        renamed = [column + suffix for column in colliding]
        if len(set(renamed)) == len(renamed) and not set(renamed) & reserved:
            return suffix
    raise WorkspaceError(
        f"Cannot join '{right_name or 'the right frame'}' without renaming "
        f"{counted(len(colliding), 'colliding column')}; rename them first."
    )

# The one status vocabulary shared by Document Tests and Data Tests. ``draft`` is
# a test whose plan exists but whose executable spec has not been written yet;
# ``completed`` is what a runner writes when every item is settled, which roll-up
# then refines into the exception-bearing pair from the durable results.
TEST_STATUSES = {
    "draft",
    "ready",
    "in_progress",
    "review_required",
    "blocked",
    "completed",
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
# The plain-content RCM columns a spreadsheet export/reimport round-trips.
# Identity (id/semantic_id), provenance, and reference fields (test_refs,
# execution_rollup, finding_refs, evidence_refs) are deliberately excluded —
# those are maintained by linking, not by editing free text in a cell.
RCM_IMPORT_FIELDS = (
    "process", "risk", "risk_rating", "business_cycle", "control", "control_type",
    "control_attributes",
    "control_owner", "criteria", "prepared_by", "reviewed_by", "review_status",
)
WORKSPACES_DIR = Path(
    os.environ.get("WORKBENCH_DATA", "")
    or Path(__file__).resolve().parents[2] / "Workspaces"
)


def workspace_write_lock(root: Path) -> threading.RLock:
    """Return the process-wide lock shared by every writer for one workspace."""
    key = str(Path(root).resolve())
    with _workspace_write_locks_guard:
        return _workspace_write_locks.setdefault(key, threading.RLock())


# Audit artifacts are deliberately stored as independently replaceable files.
# ``workspace.json`` is now the engagement manifest: it owns identity, the
# optimistic-concurrency revision, and data-workbench configuration.  The
# collections below are hydrated into the same in-memory shape the rest of the
# application already consumes, which keeps read-side call sites simple while
# avoiding a monolithic audit-record write for every edit.
_ARTIFACT_COLLECTIONS: dict[str, tuple[str, str]] = {
    "tiles": ("Dashboard/Tiles", "id"),
    "analyses": ("Analyses", "id"),
    "rulesets": ("Validation/Rulesets", "id"),
    "documents": ("Documents/.inventory", "id"),
    "rcm": ("Planning/RCM", "id"),
    "data_tests": ("DataTests", "id"),
    "work_program": ("Planning/Procedures", "id"),
    "observations": ("Observations", "id"),
    "evidence_requests": ("EvidenceRequests", "id"),
    "findings": ("Findings", "id"),
}
_ARTIFACT_OBJECTS: dict[str, str] = {
    "report": "Reports/current.json",
    "dashboard_advice": "Dashboard/advice.json",
    # The exploratory-analysis memo. A single derived artifact, read-only to
    # the auditor and regenerated from the results rather than edited, so
    # unlike the APM it needs no separate ``.md`` file to edit and no
    # ownership flip — the markdown rides inside the object.
    "analysis_summary": "Analyses/.summary.json",
}
# Row-level exception evidence for a saved analysis lives beside the
# definitions, never inside them.  ``Analyses/*.json`` is an artifact
# collection whose every file is parsed on each workspace load, so flagged
# rows stored inline would make an ordinary load carry the engagement's whole
# evidence base.  The dot prefix also keeps the directory outside the
# collection's own ``*.json`` glob.
_ANALYSIS_EVIDENCE_DIRNAME = "Analyses/.results"
_MANIFEST_FIELDS = (
    "schema_version", "revision", "id", "name", "description", "created",
    "tables", "joins",
)
_PLANNING_DEFAULT = {
    "context": {
        "objective": "", "entity": "", "period": "", "scope": "",
        "materiality": "", "key_contacts": "", "background_notes": "",
        "interview_answers": {},
    },
    "created_by": "user", "agent_run_id": None, "updated": None,
}
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _artifact_path(root: Path, collection: str, item_id: str) -> Path:
    directory, _ = _ARTIFACT_COLLECTIONS[collection]
    item_id = str(item_id or "")
    if not _ARTIFACT_ID_RE.fullmatch(item_id):
        raise WorkspaceError(f"Invalid {collection} artifact ID.")
    return root / directory / f"{item_id}.json"


def _artifact_index_path(root: Path, collection: str) -> Path:
    directory, _ = _ARTIFACT_COLLECTIONS[collection]
    return root / directory / ".index.json"


def _artifact_object_path(root: Path, name: str) -> Path:
    return root / _ARTIFACT_OBJECTS[name]


def analysis_evidence_path(root: Path, analysis_id: str) -> Path:
    """Where one saved analysis' bounded exception rows are stored."""
    analysis_id = str(analysis_id or "")
    if not _ARTIFACT_ID_RE.fullmatch(analysis_id):
        raise WorkspaceError("Invalid analyses artifact ID.")
    return root / _ANALYSIS_EVIDENCE_DIRNAME / f"{analysis_id}.json"


def _load_artifact_collection(root: Path, collection: str) -> list[dict]:
    directory, identity_key = _ARTIFACT_COLLECTIONS[collection]
    path = root / directory
    if not path.exists():
        return []
    if not path.is_dir():
        raise WorkspaceError(f"Artifact directory '{directory}' is not readable.")
    by_id: dict[str, dict] = {}
    for item_path in sorted(path.glob("*.json")):
        if item_path.name.startswith("."):
            continue
        try:
            item = json.loads(item_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError(f"Artifact '{item_path.relative_to(root)}' is unreadable.") from error
        if not isinstance(item, dict) or str(item.get(identity_key) or "") != item_path.stem:
            raise WorkspaceError(f"Artifact '{item_path.relative_to(root)}' has an invalid identity.")
        by_id[item_path.stem] = item
    index_path = _artifact_index_path(root, collection)
    if not index_path.exists():
        return list(by_id.values())
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        ordered_ids = list(index.get("ids") or [])
    except (OSError, json.JSONDecodeError, AttributeError) as error:
        raise WorkspaceError(f"Artifact index '{index_path.relative_to(root)}' is unreadable.") from error
    if len(ordered_ids) != len(set(ordered_ids)) or any(
        not isinstance(item_id, str) or item_id not in by_id for item_id in ordered_ids
    ):
        raise WorkspaceError(f"Artifact index '{index_path.relative_to(root)}' is invalid.")
    return [by_id.pop(item_id) for item_id in ordered_ids] + list(by_id.values())


def _load_artifact_object(root: Path, name: str) -> dict:
    path = _artifact_object_path(root, name)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Artifact '{path.relative_to(root)}' is unreadable.") from error
    if not isinstance(value, dict):
        raise WorkspaceError(f"Artifact '{path.relative_to(root)}' must be an object.")
    return value


def _load_planning_artifact(root: Path) -> dict:
    context_path = root / "Planning" / "context.json"
    apm_path = root / "Planning" / "APM.md"
    value: dict = {}
    if context_path.exists():
        try:
            value = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError("Planning context is unreadable.") from error
        if not isinstance(value, dict):
            raise WorkspaceError("Planning context must be an object.")
    return {**value, "apm_markdown": apm_path.read_text(encoding="utf-8") if apm_path.exists() else ""}


def _write_planning_artifact(root: Path, planning: dict) -> None:
    stored = {key: value for key, value in planning.items() if key != "apm_markdown"}
    write_json_atomic(root / "Planning" / "context.json", stored)
    apm_path = root / "Planning" / "APM.md"
    apm_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = apm_path.with_name(f".{apm_path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(str(planning.get("apm_markdown") or ""), encoding="utf-8")
    os.replace(tmp, apm_path)


def _migrate_artifacts(root: Path, definition: dict) -> dict:
    """Move v1-v3 embedded audit records to v4 sidecars before loading.

    The manifest is replaced last. A crash before that point leaves the legacy
    source intact, so the next open safely repeats the idempotent migration.
    """
    if int(definition.get("schema_version") or 1) >= SCHEMA_VERSION:
        return definition
    with workspace_write_lock(root):
        # Another opener may have completed the migration while we waited.
        definition = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
        if int(definition.get("schema_version") or 1) >= SCHEMA_VERSION:
            return definition
        planning = {**_PLANNING_DEFAULT, **dict(definition.get("planning") or {})}
        planning["context"] = {
            **_PLANNING_DEFAULT["context"], **dict(planning.get("context") or {}),
        }
        _write_planning_artifact(root, planning)
        for collection, (_, identity_key) in _ARTIFACT_COLLECTIONS.items():
            seen: set[str] = set()
            ordered_ids: list[str] = []
            superseded = (
                {str(item.get("supersedes")) for item in definition.get("documents") or []
                 if isinstance(item, dict) and item.get("supersedes")}
                if collection == "documents" else set()
            )
            for item in definition.get(collection) or []:
                if not isinstance(item, dict):
                    raise WorkspaceError(f"Legacy {collection} artifact must be an object.")
                item_id = str(item.get(identity_key) or "")
                if not item_id or item_id in seen:
                    raise WorkspaceError(f"Legacy {collection} artifacts have invalid identities.")
                seen.add(item_id)
                if item_id in superseded:
                    continue
                write_json_atomic(_artifact_path(root, collection, item_id), item)
                ordered_ids.append(item_id)
            write_json_atomic(_artifact_index_path(root, collection), {"ids": ordered_ids})
        for name in _ARTIFACT_OBJECTS:
            write_json_atomic(_artifact_object_path(root, name), dict(definition.get(name) or {}))
        manifest = {key: definition.get(key) for key in _MANIFEST_FIELDS if key in definition}
        manifest["schema_version"] = SCHEMA_VERSION
        write_json_atomic(root / "workspace.json", manifest)
        return manifest


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
    for attribute in ("planning", "report", "dashboard_advice", "analysis_summary"):
        current = getattr(target, attribute)
        current.clear()
        current.update(getattr(source, attribute))
    for attribute, value in source.__dict__.items():
        if attribute not in list_keys and attribute not in {
            "planning", "report", "dashboard_advice", "analysis_summary",
        }:
            setattr(target, attribute, value)
    return target


def set_request_revision(revision: int) -> Token:
    return _request_revision.set({"revision": int(revision)})


def reset_request_revision(token: Token) -> None:
    _request_revision.reset(token)


def _normalize_rcm_row(row: dict, *, now: str, strict: bool = True) -> dict:
    """Normalize one RCM row's control attributes and derived business cycle.

    ``strict`` is the write contract: an invalid attribute set is refused.
    Loading uses ``strict=False`` so one row whose registry pack has since
    changed is flagged for review instead of making the whole engagement —
    its documents, findings and the editor needed to repair the row —
    impossible to open.
    """
    item = dict(row)
    from . import cycle_vouching

    attributes = item.get("control_attributes")
    if not attributes:
        # A manually-created row starts with one explicit, editable attribute.
        # This is a new-schema authoring default, not an interpretation of the
        # removed top-level assertion field.
        attributes = [
            {
                "key": "manual_inspection",
                "assertion": "Operational",
                "requirement": str(item.get("control") or item.get("risk") or "Manual control assessment."),
                "evidence_kind": "manual_inspection",
            }
        ]
    try:
        item["control_attributes"] = cycle_vouching.validate_control_attributes(
            attributes
        )
        transaction_packs = {
            str(attribute["registry"]["pack_id"])
            for attribute in item["control_attributes"]
            if attribute.get("evidence_kind") == "transaction_cycle"
        }
        if len(transaction_packs) > 1:
            raise cycle_vouching.CycleSchemaError(
                "One RCM row cannot mix transaction-cycle packs."
            )
    except cycle_vouching.CycleSchemaError as error:
        if strict:
            raise WorkspaceError(str(error)) from error
        item["control_attributes"] = [dict(value) for value in attributes]
        item["attributes_status"] = "invalid"
        item["attributes_error"] = str(error)
        item["business_cycle"] = str(item.get("business_cycle") or "")
        item.pop("assertion", None)
        return _rcm_row_defaults(item, now=now)
    # The row's business cycle is a projection of its validated attributes, not
    # a separate editable field: a caller that omits it, or sends a stale one
    # alongside changed attributes, gets the derived value rather than an error.
    item["business_cycle"] = next(iter(transaction_packs), "")
    item["attributes_status"] = "valid"
    item["attributes_error"] = ""
    item.pop("assertion", None)
    return _rcm_row_defaults(item, now=now)


def _rcm_row_defaults(item: dict, *, now: str) -> dict:
    item.setdefault("criteria", "")
    item.setdefault("criteria_refs", [])
    item.setdefault("control_owner", "")
    item.setdefault("test_refs", [])
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
    item["test_refs"] = list(
        dict.fromkeys(
            str(ref).strip() for ref in item.get("test_refs") or [] if str(ref).strip()
        )
    )
    return item


def _validate_test_refs(workspace: "Workspace", refs: object) -> list[str]:
    """Validate an RCM row's ``test_refs`` — its links to durable tests.

    Both test kinds live here, so a row lists every test that covers it whatever
    its source. The refs are maintained automatically when a test is linked to a
    row; anything that is not a ``doctest:``/``datatest:`` reference (a model's
    free-text citation, say) is dropped rather than persisted.
    """
    if not refs:
        return []
    # A bare string would iterate into single characters, so wrap it as one ref.
    if isinstance(refs, str):
        refs = [refs]
    elif not isinstance(refs, (list, tuple)):
        raise WorkspaceError("test_refs must be a list of test references.")
    from . import doc_tests

    values: list[str] = []
    for ref in refs:
        value = str(ref).strip()
        if not value:
            continue
        kind, separator, item_id = value.partition(":")
        if kind not in {"doctest", "datatest"}:
            continue
        if not separator or not item_id:
            raise WorkspaceError(f"Test reference '{value}' is malformed.")
        known = (
            doc_tests.exists(workspace, item_id)
            if kind == "doctest"
            else any(item.get("id") == item_id for item in workspace.data_tests)
        )
        if not known:
            raise WorkspaceError(f"Test reference '{value}' does not exist.")
        values.append(value)
    return list(dict.fromkeys(values))


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


# The fixed narrative fields a finding carried before the ``finding`` template
# owned its shape, paired with the heading each becomes. The first five are the
# shipped template's sections; "Severity rationale" is not, so migrated prose
# lands in a section the completeness gate does not require — the text an
# auditor wrote survives without becoming a new obligation.
_LEGACY_FINDING_SECTIONS = (
    ("condition", "Condition"),
    ("criteria", "Criteria"),
    ("cause", "Root Cause"),
    ("effect", "Risk"),
    ("recommendation", "Recommendation"),
    ("severity_rationale", "Severity rationale"),
)


def _migrate_finding_narrative(item: dict) -> None:
    """Fold a pre-narrative finding's fields into one Markdown narrative.

    One-way and idempotent: a record that already carries a narrative is left
    alone, and the legacy keys are dropped so a later write cannot resurrect a
    stale copy of prose the auditor has since edited.
    """
    if "narrative" not in item:
        item["narrative"] = "\n\n".join(
            f"## {heading}\n\n{str(item.get(field) or '').strip()}"
            for field, heading in _LEGACY_FINDING_SECTIONS
            if str(item.get(field) or "").strip()
        )
    for field, _heading in _LEGACY_FINDING_SECTIONS:
        item.pop(field, None)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.definition_path = self.root / "workspace.json"
        definition = _migrate_artifacts(
            self.root, json.loads(self.definition_path.read_text(encoding="utf-8"))
        )
        self._table_signature_cache: dict[str, tuple] = {}
        self.schema_version = int(definition.get("schema_version") or 1)
        self.revision = int(definition.get("revision") or 0)
        self.id: str = definition.get("id") or self.root.name
        self.name: str = definition.get("name") or self.id
        self.description: str = definition.get("description") or ""
        self.created: str = definition.get("created") or ""
        self.tables: list[dict] = list(definition.get("tables") or [])
        self.joins: list[dict] = list(definition.get("joins") or [])
        self.tiles: list[dict] = _load_artifact_collection(self.root, "tiles")
        self.analyses: list[dict] = _load_artifact_collection(self.root, "analyses")
        self.rulesets: list[dict] = _load_artifact_collection(self.root, "rulesets")
        # Full-audit-cycle records hydrate defensively so every pre-extension
        # workspace remains readable without a migration step.
        stored_documents = _load_artifact_collection(self.root, "documents")
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
            **_PLANNING_DEFAULT,
            "apm_markdown": "",
            **_load_planning_artifact(self.root),
        }
        self.planning["context"] = {
            **_PLANNING_DEFAULT["context"],
            **dict(self.planning.get("context") or {}),
        }
        hydrated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.rcm: list[dict] = [
            _normalize_rcm_row(dict(row), now=hydrated_at, strict=False)
            for row in _load_artifact_collection(self.root, "rcm")
        ]
        self.work_program: list[dict] = _load_artifact_collection(self.root, "work_program")
        self.data_tests: list[dict] = _load_artifact_collection(self.root, "data_tests")
        self.observations: list[dict] = _load_artifact_collection(self.root, "observations")
        self.evidence_requests: list[dict] = _load_artifact_collection(self.root, "evidence_requests")
        self.findings: list[dict] = _load_artifact_collection(self.root, "findings")
        self.dashboard_advice: dict = _load_artifact_object(self.root, "dashboard_advice")
        self.analysis_summary: dict = _load_artifact_object(self.root, "analysis_summary")
        legacy_finding_statuses = {
            str(item.get("id")): item.get("status")
            for item in self.findings
            if item.get("status") is not None
        }
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
            item.setdefault("test_refs", [])
            item.setdefault("execution_refs", [])
            item.setdefault("cause_pending", False)
            _migrate_finding_narrative(item)
            # Legacy/manual origin is not equivalent to a formal auditor
            # confirmation. Unsupported records stay visible as drafts.
            item.setdefault("auditor_confirmed", False)
            item.pop("planned_test_refs", None)
            item.pop("status", None)
            item.setdefault("source", "manual")
        self.report: dict = _load_artifact_object(self.root, "report")
        legacy_artifact_statuses = {
            "planning": self.planning.get("status"),
            "report": self.report.get("status"),
            "findings": legacy_finding_statuses,
        }
        self.planning.pop("status", None)
        self.report.pop("status", None)
        self._artifact_snapshot = self._artifact_state()
        # Keep removed legacy statuses in the prior snapshot so the next
        # ordinary save rewrites the sidecars without those retired fields.
        if legacy_artifact_statuses["planning"] is not None:
            self._artifact_snapshot["planning"]["status"] = legacy_artifact_statuses["planning"]
        if legacy_artifact_statuses["report"] is not None:
            self._artifact_snapshot["report"]["status"] = legacy_artifact_statuses["report"]
        for item in self._artifact_snapshot["findings"]:
            status = legacy_artifact_statuses["findings"].get(str(item.get("id")))
            if status is not None:
                item["status"] = status

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
        definition = {
            "schema_version": SCHEMA_VERSION,
            "revision": current + 1,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created": self.created,
            "tables": self.tables,
            "joins": self.joins,
        }
        before = None
        if self.definition_path.exists():
            try:
                before = json.loads(self.definition_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                before = None
        before_artifacts = self._artifact_snapshot
        after_artifacts = self._artifact_state()
        writes = self._changed_artifact_writes(before_artifacts, after_artifacts)
        transaction = None
        try:
            if writes:
                from .workspace_transactions import apply_artifact_writes, prepare_artifact_writes

                transaction = prepare_artifact_writes(self, writes)
                apply_artifact_writes(writes)
            write_json_atomic(self.definition_path, definition)
        except Exception:
            if transaction is not None:
                from .workspace_transactions import rollback_artifact_writes

                rollback_artifact_writes(self.root.resolve(), transaction)
            raise
        if transaction is not None:
            from .workspace_transactions import complete_artifact_writes

            complete_artifact_writes(transaction)
        self.revision = current + 1
        self.schema_version = SCHEMA_VERSION
        self._artifact_snapshot = after_artifacts
        if request_state is not None:
            request_state["revision"] = self.revision
        # Debug telemetry is best-effort and must never make the auditor's
        # primary workspace write fail. Import lazily to avoid a storage cycle.
        try:
            from . import debug_store
            with debug_store.trace_context(workspace_root=str(self.root)):
                debug_store.record_workspace_save(self.id, before, definition)
        except Exception:
            pass

    def _artifact_state(self) -> dict:
        """Canonical in-memory projection of sidecar-backed audit artifacts."""
        return json.loads(json.dumps({
            "planning": self.planning,
            **{name: getattr(self, name) for name in _ARTIFACT_COLLECTIONS},
            **{name: getattr(self, name) for name in _ARTIFACT_OBJECTS},
        }, sort_keys=True, default=str))

    def _changed_artifact_writes(self, before: dict, after: dict) -> list[tuple[Path, str, object]]:
        writes: list[tuple[Path, str, object]] = []
        if before.get("planning") != after.get("planning"):
            writes.extend([
                (self.root / "Planning" / "context.json", "json", {
                    key: value for key, value in self.planning.items() if key != "apm_markdown"
                }),
                (self.root / "Planning" / "APM.md", "text", self.planning.get("apm_markdown") or ""),
            ])
        for collection, (_, identity_key) in _ARTIFACT_COLLECTIONS.items():
            prior = {str(item[identity_key]): item for item in before.get(collection, [])}
            current = {str(item[identity_key]): item for item in after.get(collection, [])}
            removed = prior.keys() - current.keys()
            for item_id in removed:
                writes.append((_artifact_path(self.root, collection, item_id), "delete", None))
            for item_id, item in current.items():
                if prior.get(item_id) != item:
                    writes.append((_artifact_path(self.root, collection, item_id), "json", item))
            if before.get(collection) != after.get(collection):
                writes.append((_artifact_index_path(self.root, collection), "json", {
                    "ids": [str(item[identity_key]) for item in after.get(collection, [])],
                }))
        for name in _ARTIFACT_OBJECTS:
            if before.get(name) != after.get(name):
                writes.append((_artifact_object_path(self.root, name), "json", getattr(self, name)))
        return writes

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
                f"'{name}' is used by {plural_word(len(dependents), 'join')}: "
                f"{', '.join(dependents)}. Remove those first."
            )
        data_test_refs = [
            item.get("id")
            for item in self.data_tests
            if name in (item.get("table_refs") or [])
        ]
        if data_test_refs:
            raise WorkspaceError(
                f"'{name}' is used by {plural_word(len(data_test_refs), 'data test')}: "
                f"{', '.join(data_test_refs)}. Reassign those first."
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
                    for key in ("data_test_id", "analysis_id", "rcm_id", "result_ref")
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
    @staticmethod
    def _outcome_policy(value: object) -> dict:
        """Validate the audit meaning declared for a procedure's returned rows.

        ``exception_rows`` says every returned row is a potential exception, so
        an empty result is the clean outcome. ``informational`` says the rows are
        context and carry no verdict of their own. The distinction decides what
        an execution concludes, so an unrecognized mode is rejected rather than
        silently read as informational.
        """
        mode = str((value or {}).get("mode") or "").strip()
        if mode not in {"exception_rows", "informational"}:
            raise WorkspaceError(
                "Analysis outcome policy mode must be 'exception_rows' or "
                "'informational'."
            )
        return {"mode": mode}

    def _analytics_default_viz(self, table: str | None, spec: dict) -> dict | None:
        """The chart the chosen analytics test naturally suggests, if any."""
        test_id = str(spec.get("test") or "")
        if not table or not test_id:
            return None
        from . import analytics

        try:
            frame = self.get_frame(table)
        except Exception:
            return None
        return analytics.suggested_viz(frame, test_id, dict(spec.get("params") or {}))

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

        spec = dict(payload.get("spec") or {})
        viz = payload.get("viz")
        if not isinstance(viz, dict) and kind == "analytics":
            # The test itself knows its natural chart — computed once here so
            # a saved definition's own viz is meaningful without recomputing,
            # instead of always defaulting to a table.
            viz = self._analytics_default_viz(table, spec)

        analysis = _apply_provenance(
            {
                "id": str(payload.get("id") or uuid.uuid4().hex[:10]),
                "title": title,
                "kind": kind,
                "table": table,
                "spec": spec,
                "viz": dict(viz or {"type": "table"}),
                "note": str(payload.get("note") or "").strip(),
                "source": payload.get("source") or ("ai" if kind == "python" else "library"),
                "created": date.today().isoformat(),
                **(
                    {"outcome_policy": self._outcome_policy(payload["outcome_policy"])}
                    if isinstance(payload.get("outcome_policy"), dict)
                    else {}
                ),
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
        # Unlike a tile, an analysis is an editing surface: params (library) and
        # code (AI) are re-saved by rewriting its spec.
        spec_changed = False
        if "spec" in changes and isinstance(changes["spec"], dict):
            if analysis["kind"] == "python" and not str(changes["spec"].get("code") or "").strip():
                raise WorkspaceError("A Python analysis needs code.")
            spec = dict(changes["spec"])
            if spec != (analysis.get("spec") or {}):
                analysis["spec"] = spec
                spec_changed = True
                # A result belongs to an exact procedure definition. Do not
                # carry an old conclusion forward after changing its code or
                # parameters — but re-saving an unchanged definition (a title
                # edit, a second press of Save) is not a change and must not
                # discard a current result.
                analysis.pop("last_result", None)
                self._drop_analysis_evidence(analysis_id)
        if "viz" in changes and isinstance(changes["viz"], dict):
            analysis["viz"] = dict(changes["viz"])
        elif spec_changed and analysis["kind"] == "analytics":
            # An edited spec may now name a different test, so its chart
            # preference is recomputed the same way a new definition's is —
            # never left holding what the previous spec suggested.
            viz = self._analytics_default_viz(analysis.get("table"), analysis["spec"])
            analysis["viz"] = dict(viz or {"type": "table"})
        if "outcome_policy" in changes and isinstance(changes["outcome_policy"], dict):
            policy = self._outcome_policy(changes["outcome_policy"])
            if policy != (analysis.get("outcome_policy") or {}):
                # The policy is part of what the result means, so a changed
                # policy invalidates the conclusion drawn under the old one.
                analysis["outcome_policy"] = policy
                analysis.pop("last_result", None)
                self._drop_analysis_evidence(analysis_id)
        self.save()
        return analysis

    def _drop_analysis_evidence(self, analysis_id: str) -> None:
        """Discard the flagged rows a superseded result concluded about.

        Exception evidence belongs to one exact execution of one exact
        definition. Whenever the conclusion it supported is dropped — an edited
        spec, a changed outcome policy, a deleted procedure — the rows go with
        it, so nothing can later read evidence that no recorded result claims.
        """
        analysis_evidence_path(self.root, analysis_id).unlink(missing_ok=True)

    def remove_analysis(self, analysis_id: str) -> None:
        self.analyses.remove(self._analysis(analysis_id))
        self._drop_analysis_evidence(analysis_id)
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
            "business_cycle": str(payload.get("business_cycle") or ""),
            "control_attributes": list(payload.get("control_attributes") or []),
            "control": str(payload.get("control") or ""),
            "control_type": str(payload.get("control_type") or ""),
            "control_owner": str(payload.get("control_owner") or ""),
            "criteria": str(payload.get("criteria") or ""),
            "criteria_refs": list(payload.get("criteria_refs") or []),
            "test_refs": _validate_test_refs(self, payload.get("test_refs") or []),
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
        self.rcm.append(item)
        self.save()
        return item

    def update_rcm(self, item_id: str, changes: dict, *, agent: bool = False) -> dict:
        item = self._planning_record(self.rcm, item_id, "RCM row")
        allowed = {
            "process", "risk", "risk_rating", "business_cycle", "control_attributes",
            "attributes_status", "attributes_error",
            "control", "control_type",
            "control_owner", "criteria", "criteria_refs", "test_refs",
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
        if "control_attributes" in changes or "business_cycle" in changes:
            candidate = _normalize_rcm_row(
                {**item, **changes}, now=self._updated_now()
            )
            changes = {
                **changes,
                "control_attributes": candidate["control_attributes"],
                "business_cycle": candidate["business_cycle"],
                "attributes_status": candidate["attributes_status"],
                "attributes_error": candidate["attributes_error"],
            }
        from .evidence import normalize_many
        for key, value in changes.items():
            if key in ("test_refs", "criteria_refs"):
                item[key] = [str(ref) for ref in (value or [])]
            elif key == "control_attributes":
                item[key] = [dict(attribute) for attribute in value]
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

    def export_rcm_rows(self) -> list[dict]:
        """Flatten RCM rows to the plain content columns a reimport accepts."""
        return [
            {
                "id": row["id"],
                **{
                    field: (
                        json.dumps(row.get(field) or [], sort_keys=True)
                        if field == "control_attributes"
                        else row.get(field, "")
                    )
                    for field in RCM_IMPORT_FIELDS
                },
            }
            for row in self.rcm
        ]

    def import_rcm(self, rows: list[dict]) -> dict:
        """Update RCM row content from a reuploaded export.

        Rows are matched by ``id``; only the plain content fields are
        accepted. No row is added or removed — an id absent from the
        workspace is reported as unmatched rather than inserted, and any
        existing row missing from the file is left untouched.
        """
        by_id = {row["id"]: row for row in self.rcm}
        planned: list[tuple[dict, dict]] = []
        unmatched: list[str] = []
        for raw in rows:
            row_id = str(raw.get("id") or "").strip()
            if not row_id:
                continue
            item = by_id.get(row_id)
            if item is None:
                unmatched.append(row_id)
                continue
            changes: dict = {}
            for field in RCM_IMPORT_FIELDS:
                if field not in raw:
                    continue
                value = raw[field]
                if field == "risk_rating":
                    value = str(value or "medium").lower()
                    if value not in ("low", "medium", "high", "critical"):
                        raise WorkspaceError(
                            f"Row {row_id}: risk rating must be low, medium, high, or critical."
                        )
                elif field == "review_status":
                    value = str(value or "draft").lower()
                    if value not in REVIEW_STATUSES:
                        raise WorkspaceError(f"Row {row_id}: unknown review status '{value}'.")
                elif field in ("prepared_by", "reviewed_by"):
                    value = str(value).strip() if value not in (None, "") else None
                elif field == "control_attributes":
                    try:
                        parsed = json.loads(str(value or "[]"))
                    except json.JSONDecodeError as error:
                        raise WorkspaceError(
                            f"Row {row_id}: control_attributes must be valid JSON."
                        ) from error
                    from . import cycle_vouching
                    try:
                        value = cycle_vouching.validate_control_attributes(parsed)
                    except cycle_vouching.CycleSchemaError as error:
                        raise WorkspaceError(f"Row {row_id}: {error}") from error
                else:
                    value = str(value) if value is not None else ""
                changes[field] = value
            if "control_attributes" in changes or "business_cycle" in changes:
                try:
                    normalized = _normalize_rcm_row(
                        {**item, **changes}, now=self._updated_now()
                    )
                except WorkspaceError as error:
                    raise WorkspaceError(f"Row {row_id}: {error}") from error
                changes["control_attributes"] = normalized["control_attributes"]
                changes["business_cycle"] = normalized["business_cycle"]
                changes["attributes_status"] = normalized["attributes_status"]
                changes["attributes_error"] = normalized["attributes_error"]
            planned.append((item, changes))

        now = self._updated_now()
        changed = 0
        for item, changes in planned:
            if any(item.get(field) != value for field, value in changes.items()):
                item.update(changes)
                _user_touch(item)
                item["updated"] = now
                changed += 1
        if changed:
            self.save()
        return {"updated": changed, "matched": len(planned), "unmatched": unmatched}

    def remove_rcm(self, item_id: str) -> None:
        """Delete one RCM row, unlinking — never deleting — the tests it linked.

        A test is a durable artifact in its own right, so losing its row leaves
        it as an unlinked test rather than destroying the work.
        """
        from . import doc_tests

        item = self._planning_record(self.rcm, item_id, "RCM row")
        linked = set(item.get("test_refs") or [])
        self.rcm.remove(item)
        for procedure in self.work_program:
            procedure["rcm_refs"] = [ref for ref in procedure.get("rcm_refs", []) if ref != item_id]
        for test in self.data_tests:
            if test.get("rcm_id") == item_id:
                test["rcm_id"] = None
        doc_tests.unlink_rcm(self, item_id)
        for finding in self.findings:
            finding["rcm_refs"] = [ref for ref in finding.get("rcm_refs", []) if ref != item_id]
            finding["test_refs"] = [
                ref for ref in finding.get("test_refs", []) if ref not in linked
            ]
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
        self.save()
        return item

    def remove_procedure(self, item_id: str) -> None:
        item = self._planning_record(self.work_program, item_id, "Procedure")
        self.work_program.remove(item)
        for finding in self.findings:
            finding["procedure_refs"] = [
                ref for ref in finding.get("procedure_refs", []) if ref != item_id
            ]
        self.save()

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
            return left.join(
                right,
                how="cross",
                suffix=join_suffix(
                    left.columns, right.columns, (), str(join["right"])
                ),
            )
        return left.join(
            right,
            how=join["how"],
            left_on=join["left_on"],
            right_on=join["right_on"],
            coalesce=True,
            # A join built on a join already carries suffixed columns, so the
            # default suffix can collide a second time. Derived from the two
            # frames, so a join that never collides keeps the names it has.
            suffix=join_suffix(
                left.columns,
                right.columns,
                join["right_on"],
                str(join["right"]),
            ),
        )

    def _table_signature(self, name: str, _seen: frozenset = frozenset()) -> tuple:
        """A hashable fingerprint of a table's content: the source file's
        (size, mtime) for base tables, or the join spec plus both sides'
        signatures, recursively. Used to key the on-disk profile cache.

        Callers across data_tests/doc_tests/findings/analysis_results each
        re-derive this for overlapping sets of tables, and joins-of-joins
        re-derive shared base tables again on every recursive branch. Cached
        per name for the life of this instance, since a Workspace is rebuilt
        fresh on every load and the underlying files can't change out from
        under it mid-request.
        """
        cached = self._table_signature_cache.get(name)
        if cached is not None:
            return cached
        if name in _seen:
            raise WorkspaceError(f"Join '{name}' references itself in a cycle.")

        entry = self._table_entry(name)
        if entry is not None:
            signature = ("file", loader.file_signature(self.data_dir / entry["file"]))
            self._table_signature_cache[name] = signature
            return signature

        join = self._join_entry(name)
        if join is None:
            raise WorkspaceError(f"No table named '{name}'.")

        seen = _seen | {name}
        signature = (
            "join",
            join["how"],
            tuple(join["left_on"]),
            tuple(join["right_on"]),
            self._table_signature(join["left"], seen),
            self._table_signature(join["right"], seen),
        )
        self._table_signature_cache[name] = signature
        return signature

    # ---------------------------------------------------------------- profile
    def _cache_dir(self) -> Path:
        return self.data_dir / loader.CACHE_DIRNAME

    def _profile_cache_path(self, name: str, sig: tuple) -> Path:
        # The profiler's schema version is part of the key: the signature covers
        # the table's *content*, so on its own it would keep serving a profile
        # written in an older payload shape long after the shape changed.
        digest = hashlib.sha1(
            repr((profiler.SCHEMA_VERSION, sig)).encode()
        ).hexdigest()[:16]
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
            # Written through the temp-file + rename path because capabilities
            # that fan their units out resolve context concurrently, and two
            # threads profiling the same table would otherwise be free to
            # interleave writes to this one path.
            write_json_atomic(cache_file, profile)
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
