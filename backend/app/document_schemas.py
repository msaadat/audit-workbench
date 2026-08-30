"""Per-workspace induced document schemas and the local type vocabulary.

A schema describes the fields *this engagement's* documents of one type actually
carry. It is induced from a small sample, frozen, and then handed to extraction
as guidance. Schemas are workspace-scoped on purpose: one describing another
engagement's invoices would drift exactly the way an authored pack does.

The stored hash covers the schema's *meaning* — its document type and its fields
— and nothing else. Bookkeeping such as which documents it was derived from, or
when it was written, moves without invalidating anything, because that hash is
stamped into every extraction and an extraction is only stale when the words it
extracted against have changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from . import document_types
from .workspaces import Workspace, WorkspaceError, write_json_atomic

_DIRNAME = "DocumentSchemas"
_INDEX_NAME = ".index.json"
_LOCAL_TYPES_NAME = ".local_types.json"

_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: What a field is *for*. The only part of a schema with downstream meaning.
FIELD_ROLES = frozenset({"identifier", "party", "attribute", "control"})
#: How a value is normalized and which operators may read it.
VALUE_TYPES = frozenset({"identifier", "date", "number", "text", "boolean"})
CARDINALITIES = frozenset({"one", "many"})
CONFIDENCES = frozenset({"high", "medium", "low"})

class SchemaConflict(WorkspaceError):
    """Two samples named one field as two different things."""

    def __init__(self, document_type: str, conflicts: list[dict]):
        self.document_type = str(document_type)
        self.conflicts = list(conflicts)
        described = "; ".join(
            f"'{item['name']}' is {' and '.join(item['values'])} ({item['attribute']})"
            for item in self.conflicts
        )
        super().__init__(
            f"Samples of '{document_type}' disagree on what a field is: {described}."
        )


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(workspace: Workspace, document_type: str) -> threading.RLock:
    key = f"{workspace.root.resolve()}:{document_type}"
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _root(workspace: Workspace) -> Path:
    return workspace.root / _DIRNAME


def _schema_path(workspace: Workspace, document_type: str) -> Path:
    root = _root(workspace).resolve()
    path = (root / f"{document_type}.json").resolve()
    if not path.is_relative_to(root) or path.name.startswith("."):
        raise WorkspaceError("Unsafe document-schema reference.")
    return path


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


# --------------------------------------------------------------------------- #
# the workspace's coined vocabulary
# --------------------------------------------------------------------------- #
def local_types(workspace: Workspace) -> list[dict]:
    """Types an auditor coined for this engagement, in creation order."""

    stored = _read_json(_root(workspace) / _LOCAL_TYPES_NAME, {"types": []})
    return [item for item in (stored.get("types") or []) if isinstance(item, dict)]


def local_type_ids(workspace: Workspace) -> tuple[str, ...]:
    return tuple(str(item.get("id") or "") for item in local_types(workspace))


def effective_type_ids(workspace: Workspace) -> tuple[str, ...]:
    """Everything a classifier may return here: the global list plus coined types."""

    return (*document_types.SELECTABLE_IDS, *local_type_ids(workspace))


def coin_local_type(
    workspace: Workspace,
    name: str,
    *,
    discriminator: str = "",
    created_by: str = "auditor",
) -> dict:
    """Register an auditor-coined type, or return the existing one unchanged.

    Coining is idempotent by id. Re-coining a name that already exists returns
    what is stored rather than raising: an auditor retyping a second document to
    the same name is doing the ordinary thing, not making a mistake.
    """

    type_id = document_types.local_id(name)
    root = _root(workspace)
    path = root / _LOCAL_TYPES_NAME
    stored = _read_json(path, {"types": []})
    entries = [item for item in (stored.get("types") or []) if isinstance(item, dict)]
    for entry in entries:
        if str(entry.get("id") or "") == type_id:
            return entry
    entry = {
        "id": type_id,
        "label": str(name).strip(),
        "discriminator": str(discriminator or "").strip(),
        "created": utcnow(),
        "created_by": str(created_by or "auditor"),
    }
    root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"schema_version": 1, "types": [*entries, entry]})
    return entry


# --------------------------------------------------------------------------- #
# schema validation
# --------------------------------------------------------------------------- #
def _validate_field(raw: object, label: str) -> dict:
    if not isinstance(raw, Mapping):
        raise WorkspaceError(f"{label} must be an object.")
    name = str(raw.get("name") or "")
    if not _FIELD_NAME_RE.fullmatch(name):
        raise WorkspaceError(f"{label} has an invalid field name '{name}'.")
    role = str(raw.get("role") or "")
    if role not in FIELD_ROLES:
        raise WorkspaceError(f"{label} has an unsupported role '{role}'.")
    value_type = str(raw.get("value_type") or "")
    if value_type not in VALUE_TYPES:
        raise WorkspaceError(f"{label} has an unsupported value type '{value_type}'.")
    # A join candidate must normalize as an identifier. Letting one be typed as a
    # number would compare "0042" against "42" through numeric rules and silently
    # split or merge a cycle.
    if role == "identifier" and value_type != "identifier":
        raise WorkspaceError(
            f"{label} is an identifier field and must have value_type 'identifier'."
        )
    cardinality = str(raw.get("cardinality") or "one")
    if cardinality not in CARDINALITIES:
        raise WorkspaceError(f"{label} has an unsupported cardinality '{cardinality}'.")
    confidence = str(raw.get("confidence") or "medium")
    if confidence not in CONFIDENCES:
        raise WorkspaceError(f"{label} has an unsupported confidence '{confidence}'.")
    verbatim = raw.get("verbatim", True)
    if not isinstance(verbatim, bool):
        raise WorkspaceError(f"{label} needs a boolean 'verbatim'.")
    return {
        "name": name,
        "role": role,
        "value_type": value_type,
        "cardinality": cardinality,
        "verbatim": verbatim,
        "confidence": confidence,
        "label": str(raw.get("label") or "").strip(),
    }


def validate_fields(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)) or not value:
        raise WorkspaceError("A document schema needs at least one field.")
    fields = [_validate_field(raw, f"fields[{index}]") for index, raw in enumerate(value)]
    names = [field["name"] for field in fields]
    if len(names) != len(set(names)):
        raise WorkspaceError("A document schema cannot repeat a field name.")
    return sorted(fields, key=lambda field: field["name"])


#: Ordered weakest to strongest, so a union can keep the strongest thing any
#: sample said about a field it saw.
_CONFIDENCE_ORDER = ("low", "medium", "high")

#: What two samples must agree on for a field to merge. Everything else about a
#: field can be reconciled arithmetically; these two change what the field *is*.
CONFLICTING_ATTRIBUTES = ("value_type", "role")


def union_fields(
    samples: Iterable[Iterable[Mapping[str, object]]],
) -> tuple[list[dict], list[dict]]:
    """Merge per-sample field lists into one schema, reporting real conflicts.

    Union, never intersect. A field one sample saw and another did not is
    optional, not a disagreement — intersecting would permanently discard a field
    the corpus really contains, while carrying it costs nothing, since an absent
    field is simply absent at extraction time.

    A conflict is only ever the same name meaning two different things: a
    differing ``value_type`` or ``role``. Those change what the field *is* and
    cannot be merged arithmetically, so they are returned for a reconciliation
    turn to settle rather than resolved by picking a winner here.
    """

    seen: dict[str, dict] = {}
    sources: dict[str, list[dict]] = {}
    for fields in samples:
        for raw in fields:
            field = dict(raw)
            name = str(field.get("name") or "")
            if not name:
                continue
            sources.setdefault(name, []).append(field)
            prior = seen.get(name)
            if prior is None:
                seen[name] = field
                continue
            seen[name] = {
                **prior,
                # A sample that saw two of something proves the type can carry
                # two; one that saw one proves nothing about the maximum.
                "cardinality": (
                    "many"
                    if "many" in {prior.get("cardinality"), field.get("cardinality")}
                    else "one"
                ),
                # Interpretive wins. Demanding a quote for a value the document
                # never prints is unsatisfiable, and a response cannot be
                # repaired into one.
                "verbatim": bool(prior.get("verbatim", True))
                and bool(field.get("verbatim", True)),
                "confidence": max(
                    (
                        str(prior.get("confidence") or "medium"),
                        str(field.get("confidence") or "medium"),
                    ),
                    key=lambda value: (
                        _CONFIDENCE_ORDER.index(value)
                        if value in _CONFIDENCE_ORDER
                        else 0
                    ),
                ),
                "label": str(prior.get("label") or "") or str(field.get("label") or ""),
            }

    conflicts: list[dict] = []
    for name, entries in sorted(sources.items()):
        for attribute in CONFLICTING_ATTRIBUTES:
            values = sorted({str(entry.get(attribute) or "") for entry in entries})
            if len(values) > 1:
                conflicts.append({"name": name, "attribute": attribute, "values": values})
    return [seen[name] for name in sorted(seen)], conflicts


def induce(
    workspace: Workspace,
    document_type: str,
    samples: Iterable[Iterable[Mapping[str, object]]],
    *,
    derived_from: Iterable[str] = (),
    reconciled: bool = False,
) -> dict:
    """Union sample schemas and freeze the result. Raises on unsettled conflict.

    ``low_confidence`` marks a schema induced from a single sample: the agreement
    check cannot run on one document, so nothing here has been corroborated. It
    is deliberately not a refusal — a one-off document with a real schema is
    still better evidence than an unclassified one — and the escape-rate metric
    is what catches it once more documents of the type arrive.
    """

    supplied = [list(sample) for sample in samples]
    fields, conflicts = union_fields(supplied)
    if conflicts:
        raise SchemaConflict(document_type, conflicts)
    return save_schema(
        workspace,
        document_type,
        fields,
        derived_from=derived_from,
        reconciled=reconciled,
        low_confidence=len(supplied) < 2,
    )


def meaning(document_type: str, fields: Iterable[Mapping[str, object]]) -> dict:
    """The hashed part of a schema: what it says, not when it was written."""

    return {"document_type": document_type, "fields": [dict(field) for field in fields]}


# --------------------------------------------------------------------------- #
# read and write
# --------------------------------------------------------------------------- #
def save_schema(
    workspace: Workspace,
    document_type: str,
    fields: object,
    *,
    derived_from: Iterable[str] = (),
    reconciled: bool = False,
    low_confidence: bool = False,
) -> dict:
    """Freeze one type's schema, bumping its version only if the meaning moved.

    Re-inducing a type that yields the same fields is a no-op by design: the
    version and hash are what extractions are stamped with, so bumping them on an
    identical result would invalidate a corpus for nothing.
    """

    type_id = document_types.validate(
        document_type, local_types=local_type_ids(workspace)
    )
    if type_id == document_types.OTHER:
        raise WorkspaceError("The 'other' bucket cannot carry a schema; retype first.")
    validated = validate_fields(fields)
    schema_hash = canonical_sha256(meaning(type_id, validated))
    with _lock(workspace, type_id):
        path = _schema_path(workspace, type_id)
        prior = _read_json(path, {}) if path.exists() else None
        if prior and str(prior.get("schema_hash") or "") == schema_hash:
            return prior
        version = int(prior.get("schema_version") or 0) + 1 if prior else 1
        record = {
            "document_type": type_id,
            "schema_version": version,
            "schema_hash": schema_hash,
            "fields": validated,
            "derived_from": [str(value) for value in derived_from],
            "reconciled": bool(reconciled),
            "low_confidence": bool(low_confidence),
            "created": prior.get("created") if prior else utcnow(),
            "updated": utcnow(),
            "versions": [
                *(list(prior.get("versions") or []) if prior else []),
                {"schema_version": version, "schema_hash": schema_hash, "created": utcnow()},
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, record)
        _reindex(workspace)
        return record


def load_schema(workspace: Workspace, document_type: str) -> dict | None:
    """The current schema for a type, or None. Never raises on a missing one."""

    try:
        path = _schema_path(workspace, str(document_type))
    except WorkspaceError:
        return None
    if not path.exists():
        return None
    stored = _read_json(path, {})
    return stored or None


def get_schema(workspace: Workspace, document_type: str) -> dict:
    schema = load_schema(workspace, document_type)
    if schema is None:
        raise WorkspaceError(f"No schema has been induced for '{document_type}'.")
    return schema


def list_schemas(workspace: Workspace) -> list[dict]:
    root = _root(workspace)
    if not root.exists():
        return []
    records = []
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("."):
            continue
        stored = _read_json(path, {})
        if stored:
            records.append(stored)
    return sorted(records, key=lambda item: str(item.get("document_type") or ""))


def remove_schema(workspace: Workspace, document_type: str) -> None:
    path = _schema_path(workspace, str(document_type))
    if not path.exists():
        raise WorkspaceError(f"No schema has been induced for '{document_type}'.")
    path.unlink()
    _reindex(workspace)


def current_hash(workspace: Workspace, document_type: str) -> str | None:
    schema = load_schema(workspace, document_type)
    return str(schema.get("schema_hash")) if schema else None


def schema_ref(workspace: Workspace, document_type: str) -> dict:
    """The stamp an extraction carries, identifying what it extracted against."""

    schema = get_schema(workspace, document_type)
    return {
        "document_type": schema["document_type"],
        "schema_version": schema["schema_version"],
        "schema_hash": schema["schema_hash"],
    }


def is_current(workspace: Workspace, ref: object) -> bool:
    """Whether a stored extraction's schema stamp still matches the live schema.

    Fails closed: an unreadable or partial reference is not current. A caller
    excluding the extraction is right to; reinterpreting it under today's fields
    is what the stamp exists to prevent.
    """

    if not isinstance(ref, Mapping):
        return False
    schema = load_schema(workspace, str(ref.get("document_type") or ""))
    if schema is None:
        return False
    return (
        str(ref.get("schema_hash") or "") == str(schema.get("schema_hash") or "")
        and ref.get("schema_version") == schema.get("schema_version")
    )


def is_current_for(workspace: Workspace, ref: object, document_type: str) -> bool:
    """Whether a stamp is current *and* belongs to this document's own type.

    :func:`is_current` answers only whether the schema the stamp names has moved
    since. That is the right question when the catalog changes underneath a
    corpus, and the wrong one when a single document changes what it *is*: an
    extraction stamped ``investment_confirmation`` stays perfectly current under
    that type's schema after an auditor retypes the document to something else,
    and reading it as evidence of the new type — or reusing it instead of
    re-extracting — attributes values to fields they were never read against.

    The type is passed in rather than read here, because ``document_types`` and
    ``document_classification`` both already read this module.
    """

    if not isinstance(ref, Mapping):
        return False
    if str(ref.get("document_type") or "") != str(document_type or ""):
        return False
    return is_current(workspace, ref)


def joinable(schema: Mapping[str, object]) -> bool:
    """Whether anything in this schema could serve as a join key."""

    return any(
        str(field.get("role") or "") == "identifier"
        for field in (schema.get("fields") or [])
    )


#: The share of a type's documents that may state a field the schema has no room
#: for before the schema is treated as unrepresentative. Not zero: an occasional
#: one-off is a document being unusual, not a schema being wrong.
ESCAPE_RATE_THRESHOLD = 0.25


def escape_rate(
    workspace: Workspace,
    document_type: str,
    extractions: Iterable[Mapping[str, object]],
) -> dict:
    """Measure how often extraction had to step outside this type's schema.

    This is the safety net for small-n induction. Two samples agreeing tells you
    little when a corpus is heterogeneous, and agreement is itself biased toward
    sparse schemas — two documents agree most easily when both state little. What
    catches an unrepresentative sample is not a better agreement check but this:
    the rate at which real extraction finds facts the frozen schema cannot hold.

    Deterministic and model-free, so it can run after every extraction pass. A
    field escaping on a material share of the type is evidence the samples were
    unrepresentative and marks the schema for re-derivation; a field escaping
    once is a document being unusual.
    """

    supplied = list(extractions)
    documents_seen = len(supplied)
    escaped_documents = 0
    by_field: dict[str, int] = {}
    for extraction in supplied:
        names = {
            str(field.get("name") or "")
            for record in extraction.get("records") or []
            for field in record.get("additional_fields") or []
            if str(field.get("name") or "")
        }
        if names:
            escaped_documents += 1
        for name in names:
            by_field[name] = by_field.get(name, 0) + 1
    rate = (escaped_documents / documents_seen) if documents_seen else 0.0
    schema = load_schema(workspace, document_type)
    return {
        "document_type": str(document_type),
        "schema_version": (schema or {}).get("schema_version"),
        "documents": documents_seen,
        "documents_with_escapes": escaped_documents,
        "rate": rate,
        # Ordered by how widely a field escapes, because that is what says
        # whether the schema is missing something the *type* carries rather than
        # something one document happened to state.
        "fields": [
            {"name": name, "documents": count, "rate": count / documents_seen}
            for name, count in sorted(
                by_field.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "unrepresentative": documents_seen > 0 and rate >= ESCAPE_RATE_THRESHOLD,
    }


def _reindex(workspace: Workspace) -> None:
    root = _root(workspace)
    entries = [
        {
            "document_type": record["document_type"],
            "schema_version": record["schema_version"],
            "schema_hash": record["schema_hash"],
            "low_confidence": bool(record.get("low_confidence")),
            "updated": record.get("updated"),
        }
        for record in list_schemas(workspace)
    ]
    root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(root / _INDEX_NAME, {"schema_version": 1, "schemas": entries})


def index(workspace: Workspace) -> dict:
    return _read_json(_root(workspace) / _INDEX_NAME, {"schema_version": 1, "schemas": []})
