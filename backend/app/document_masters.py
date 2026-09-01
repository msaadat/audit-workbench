"""The accumulating vocabulary one document type is read under.

A master is what a type's documents have collectively said they carry, built up
one document at a time as the type is read. It is deliberately **not** a schema:
it carries no version, nothing is ever stamped against it, and
``document_schemas.is_current`` has nothing to say about it. Keeping the two
apart is what lets the vocabulary move while a type is being read without the
staleness family firing on every document — a master mutating in place under a
fixed ``schema_version`` would either invalidate every prior reading or hold a
version steady while its content moved, which is a stamp that lies.

``DocumentSchemas/<type>.json`` is written exactly once per type per run, by the
stamp, from the finished master. This store is where the vocabulary lives until
then.

Three properties the accumulation holds, each with a failure behind it:

- **The master only grows.** Removing a field would leave every earlier reading
  holding a value under a name the vocabulary no longer explains. Document 18
  may not retract what document 1 read.
- **A field enters only by being filled.** ``fill_count`` starts at 1 by
  construction, because a descriptor arrives with the value and citation that
  introduced it. The zero-fill field an induced schema could carry —
  ``fx_contract.received_date`` at 0 of 11, which an RCM turn then wrote a
  comparison against — cannot exist here.
- **``introduced_at`` is the index into ``documents_read``.** The documents that
  were never asked about a field are exactly those before it, which is what
  makes the late-field sweep computable without hash archaeology.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Iterable, Mapping

from . import document_schemas, document_types
from .workspaces import Workspace, WorkspaceError, write_json_atomic

_DIRNAME = "DocumentMasters"
_INDEX_NAME = ".index.json"

_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Keys a master field carries beyond what a schema field does. Stripped before
#: the fields reach ``document_schemas.validate_fields``, which knows nothing
#: about them and would reject the field for carrying them.
MASTER_ONLY_KEYS = ("fill_count", "introduced_at")

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(workspace: Workspace, document_type: str) -> threading.RLock:
    key = f"{workspace.root}:{document_type}"
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


def _root(workspace: Workspace) -> Path:
    return Path(workspace.root) / _DIRNAME


def _path(workspace: Workspace, document_type: str) -> Path:
    type_id = document_types.validate(
        document_type, local_types=document_schemas.local_type_ids(workspace)
    )
    if type_id == document_types.OTHER:
        raise WorkspaceError(
            "The 'other' bucket carries no master; the read coins a type first."
        )
    return _root(workspace) / f"{type_id.replace('/', '_')}.json"


def empty(document_type: str) -> dict:
    """A master for a type nothing has been read into yet."""

    return {
        "document_type": str(document_type),
        "master_ref": "",
        "documents_read": [],
        "fields": [],
        "renames": [],
        "widened": [],
    }


def master_ref(document_type: str, fields: Iterable[Mapping[str, object]]) -> str:
    """The master's content hash.

    Covers the vocabulary and nothing else — not which documents contributed it,
    not the fill counts, not the rename log. It is what a reading carries until
    its type is stamped, and it has to move when and only when the *names a
    document was read under* move. Fill counts rise on every document and are
    deliberately outside it: a reading is not made stale by a later document
    stating the same field.
    """

    material = [
        {key: value for key, value in dict(field).items() if key not in MASTER_ONLY_KEYS}
        for field in fields
    ]
    return document_schemas.canonical_sha256(
        {"document_type": str(document_type), "fields": material}
    )


def schema_fields(master: Mapping[str, object]) -> list[dict]:
    """The master's fields in the shape ``save_schema`` takes."""

    return [
        {key: value for key, value in dict(field).items() if key not in MASTER_ONLY_KEYS}
        for field in master.get("fields") or []
    ]


def load_master(workspace: Workspace, document_type: str) -> dict | None:
    """The stored master for a type, or None. Never raises on a missing one."""

    try:
        path = _path(workspace, str(document_type))
    except (WorkspaceError, document_types.DocumentTypeError):
        return None
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def master(workspace: Workspace, document_type: str) -> dict:
    """The stored master, or an empty one for a type nothing has been read into."""

    return load_master(workspace, document_type) or empty(document_type)


def field_names(workspace: Workspace, document_type: str) -> list[str]:
    """The names a reading of this type may use, in master order."""

    return [
        str(field.get("name") or "")
        for field in master(workspace, document_type).get("fields") or []
    ]


def has_read(workspace: Workspace, document_type: str, document_id: str) -> bool:
    return str(document_id) in [
        str(value) for value in master(workspace, document_type).get("documents_read") or []
    ]


def _highest_entry(field: Mapping[str, object]) -> int:
    """The largest ``entry`` a declared field was filled at in one document."""

    return max(
        (int(value.get("entry") or 1) for value in field.get("values") or []),
        default=1,
    )


def _validate_name(name: str, label: str) -> str:
    value = str(name or "").strip()
    if not _FIELD_NAME_RE.fullmatch(value):
        raise WorkspaceError(f"{label} has an invalid field name '{value}'.")
    return value


def apply_reading(
    workspace: Workspace,
    document_type: str,
    *,
    document_id: str,
    new_fields: Iterable[Mapping[str, object]] = (),
    renames: Iterable[Mapping[str, object]] = (),
    filled: Iterable[str] | Mapping[str, int] = (),
) -> dict:
    """Fold one document's reading into its type's master and persist it.

    ``filled`` is the master field names this document stated, counted once per
    document rather than per record: breadth is what distinguishes a field the
    type carries from one record repeating it, and a selector written against a
    name that fourteen of eighteen documents state is a different thing from one
    four of them do. Supplied as a mapping it also carries the highest ``entry``
    each name reached, which is what widens cardinality.

    **Cardinality widens, and never narrows.** A field's cardinality is a guess
    made from whichever document introduced it, and a later document stating it
    twice is *evidence* rather than a violation — "one sample seeing two proves
    the type can carry two" is the rule the sample union already held, and it
    has to survive the union's deletion. Without it document one silently fixes
    a constraint document two cannot satisfy, and the failure is charged to
    document two: measured on the treasury corpus, the first dealing ticket
    declared ``rate`` as ``one``, the second states it twice, and the second
    document failed outright and blocked its type's stamp.

    Renames are applied before additions, so a document may rename a field and
    add another in one reading without the two colliding. Both are recorded:
    the rename log is what makes 4c's re-sweep able to say *why* a prior reading
    is being re-opened, and a rename costs exactly what a late-added field does
    because the earlier readings used the old name and their silence under the
    new one would be a lie.
    """

    type_id = document_types.validate(
        document_type, local_types=document_schemas.local_type_ids(workspace)
    )
    with _lock(workspace, type_id):
        record = master(workspace, type_id)
        fields = [dict(field) for field in record.get("fields") or []]
        read = [str(value) for value in record.get("documents_read") or []]
        rename_log = [dict(item) for item in record.get("renames") or []]
        by_name = {str(field.get("name")): field for field in fields}

        index = read.index(str(document_id)) if str(document_id) in read else len(read)

        applied_renames: dict[str, str] = {}
        for position, item in enumerate(renames):
            source = str(dict(item).get("from") or "")
            target = _validate_name(dict(item).get("to"), f"renames[{position}]")
            field = by_name.get(source)
            if field is None:
                raise WorkspaceError(
                    f"renames[{position}] renames '{source}', which this master "
                    "does not carry."
                )
            if target in by_name and target != source:
                raise WorkspaceError(
                    f"renames[{position}] renames '{source}' to '{target}', which "
                    "this master already carries."
                )
            if target == source:
                continue
            del by_name[source]
            field["name"] = target
            by_name[target] = field
            applied_renames[source] = target
            rename_log.append(
                {
                    "from": source,
                    "to": target,
                    "reason": str(dict(item).get("reason") or "").strip(),
                    "document_id": str(document_id),
                    "at_index": index,
                }
            )

        entries = (
            {str(name): int(value) for name, value in filled.items()}
            if isinstance(filled, Mapping)
            else {str(name): 1 for name in filled}
        )
        stated = {
            applied_renames.get(name, name): entry
            for name, entry in entries.items()
            if name
        }
        for position, raw in enumerate(new_fields):
            item = dict(raw)
            name = _validate_name(item.get("name"), f"new_fields[{position}]")
            if name in by_name:
                # Not an error the read can be blamed for: a field the master
                # gained earlier in this same response, or one a rename just
                # freed. Counting it as stated is the truthful outcome.
                stated[name] = max(stated.get(name, 1), _highest_entry(item))
                continue
            field = {
                key: item[key]
                for key in (
                    "name",
                    "role",
                    "value_type",
                    "cardinality",
                    "verbatim",
                    "confidence",
                    "label",
                )
                if key in item
            }
            field["name"] = name
            # Filled by the document that introduces it, by construction — the
            # response contract carries the value and citation alongside the
            # descriptor, so a zero-fill field cannot enter here.
            field["fill_count"] = 0
            field["introduced_at"] = index
            fields.append(field)
            by_name[name] = field
            stated[name] = max(stated.get(name, 1), _highest_entry(item))

        widened: list[dict] = []
        for name, entry in stated.items():
            field = by_name.get(name)
            if field is None:
                continue
            field["fill_count"] = int(field.get("fill_count") or 0) + 1
            if entry > 1 and str(field.get("cardinality") or "one") == "one":
                field["cardinality"] = "many"
                widened.append(
                    {"name": name, "document_id": str(document_id), "at_index": index}
                )

        # ``validate_fields`` is the same gate a schema goes through, so a master
        # can never accumulate into something ``save_schema`` would refuse at the
        # end of the type — which would strand every reading of it.
        #
        # Except emptiness, which it refuses and this must not: a document that
        # states no record is a truthful reading, and the master it folds into is
        # legitimately still empty. Refusing here failed the *reading* over a
        # property of the vocabulary, and took the type's other documents with it.
        # A type that reaches its stamp with nothing is the stamp's refusal to
        # make, and it makes it.
        if fields:
            document_schemas.validate_fields(
                [
                    {
                        key: value
                        for key, value in field.items()
                        if key not in MASTER_ONLY_KEYS
                    }
                    for field in fields
                ]
            )

        if str(document_id) not in read:
            read.append(str(document_id))
        ordered = sorted(fields, key=lambda field: str(field.get("name")))
        updated = {
            "document_type": type_id,
            "master_ref": master_ref(type_id, ordered),
            "documents_read": read,
            "fields": ordered,
            "renames": rename_log,
            # Recorded for the same reason a rename is: a prior reading was made
            # under a narrower claim about this field, and a reviewer trusting an
            # accumulating vocabulary needs to see where it moved.
            "widened": [*(record.get("widened") or []), *widened],
        }
        _write(workspace, type_id, updated)
        return updated


def unread_for_field(master_record: Mapping[str, object], name: str) -> list[str]:
    """The documents of this type that were never asked about one field.

    Exactly those read before it was introduced. This is what 4c sweeps, and it
    is why ``introduced_at`` is stored rather than recovered: absence on a
    document that predates a field means *nobody looked*, which is a different
    answer from *the document does not state this* — and in an audit the
    difference is the finding.
    """

    read = [str(value) for value in master_record.get("documents_read") or []]
    for field in master_record.get("fields") or []:
        if str(field.get("name")) == str(name):
            return read[: int(field.get("introduced_at") or 0)]
    return []


def late_fields(master_record: Mapping[str, object]) -> list[dict]:
    """Fields introduced after the first document, with what they re-open."""

    return [
        {
            "name": str(field.get("name")),
            "introduced_at": int(field.get("introduced_at") or 0),
            "unread": unread_for_field(master_record, str(field.get("name"))),
        }
        for field in master_record.get("fields") or []
        if int(field.get("introduced_at") or 0) > 0
    ]


def vocabulary(workspace: Workspace, document_type: str) -> dict:
    """One type's vocabulary as a reviewer needs to read it.

    Fill counts stop being a refinement here and become the thing that makes an
    accumulating master usable. An authoring turn shown names without
    frequencies made ``fx_contract.received_date`` — 0 of 11 — the anchor of
    three population-wide comparisons, and 1 of 18 would have read exactly the
    same to it. A field stated by fourteen of eighteen documents and one stated
    by one are different evidence, and only one of them can carry a rule.

    ``thin`` is the other half, and it exists because of a measured silence: a
    dealing ticket carrying fourteen labelled fields returned one, the type's
    whole vocabulary became that one field, and the second ticket then agreed
    with it — so the master read *1 field, stated by 2 of 2*, indistinguishable
    from a corroborated vocabulary. Nothing refuses that, and nothing should:
    the reading is truthful about what it was given. What was missing was
    anywhere to see it.

    The test is *functional* rather than a threshold, because any field count
    would be a guess and types genuinely differ in size. A vocabulary earns its
    place by supporting a cycle rule, and a rule needs two things: an
    ``identifier`` to join the document to another one, and something that is
    not an identifier to assert about once joined. The one-field dealing ticket
    could be joined and had nothing to say — which is exactly what an auditor
    needs told, in the terms they would have discovered it in.
    """

    record = master(workspace, document_type)
    read = [str(value) for value in record.get("documents_read") or []]
    fields = [dict(field) for field in record.get("fields") or []]
    corroborated = [
        field for field in fields if int(field.get("fill_count") or 0) >= 2
    ]
    joinable = any(str(field.get("role") or "") == "identifier" for field in fields)
    comparable = any(str(field.get("role") or "") != "identifier" for field in fields)
    return {
        "document_type": str(document_type),
        "documents_read": read,
        "fields": [
            {
                "name": str(field.get("name")),
                "role": str(field.get("role")),
                "value_type": str(field.get("value_type")),
                "cardinality": str(field.get("cardinality") or "one"),
                "label": str(field.get("label") or ""),
                "fill_count": int(field.get("fill_count") or 0),
                "introduced_at": int(field.get("introduced_at") or 0),
                # The documents that were never asked about this field, which is
                # a different answer from the ones that were asked and did not
                # state it — and in an audit the difference is the finding.
                "unread": unread_for_field(record, str(field.get("name"))),
            }
            for field in fields
        ],
        "renames": [dict(item) for item in record.get("renames") or []],
        "widened": [dict(item) for item in record.get("widened") or []],
        # A vocabulary only one document contributed to, or one where no field
        # was stated twice, is a guess rather than a corroborated reading. Said
        # rather than scored: the number that matters is on the screen beside it.
        "corroborated_fields": len(corroborated),
        "joinable": joinable,
        "comparable": comparable,
        "thin": bool(read)
        and (len(read) < 2 or not corroborated or not (joinable and comparable)),
    }


def catalog(workspace: Workspace) -> list[dict]:
    """Every type's vocabulary, for the documents tab."""

    return [vocabulary(workspace, name) for name in types_with_master(workspace)]


def reset(workspace: Workspace, document_type: str) -> None:
    """Discard a type's master so the pass rebuilds it from the start.

    Only ``revise_vocabulary`` does this. Appending to an existing master while
    re-reading the type from document one would leave ``introduced_at`` indices
    that no longer describe what any document was asked, which does not fail —
    it makes the sweep run over the wrong set, silently.
    """

    try:
        path = _path(workspace, str(document_type))
    except (WorkspaceError, document_types.DocumentTypeError):
        return
    if path.exists():
        path.unlink()
    _reindex(workspace)


def list_masters(workspace: Workspace) -> list[dict]:
    root = _root(workspace)
    if not root.exists():
        return []
    records = []
    for path in sorted(root.glob("*.json")):
        if path.name == _INDEX_NAME:
            continue
        record = load_master(workspace, path.stem)
        if record is not None:
            records.append(record)
    return records


def types_with_master(workspace: Workspace) -> list[str]:
    return sorted(
        str(record.get("document_type") or "") for record in list_masters(workspace)
    )


def _write(workspace: Workspace, document_type: str, record: dict) -> None:
    path = _path(workspace, document_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, record)
    _reindex(workspace)


def _reindex(workspace: Workspace) -> None:
    """Mirror the existing directory-index pattern so listing opens one file."""

    root = _root(workspace)
    if not root.exists():
        return
    entries = {}
    for path in sorted(root.glob("*.json")):
        if path.name == _INDEX_NAME:
            continue
        record = load_master(workspace, path.stem)
        if record is None:
            continue
        entries[str(record.get("document_type") or path.stem)] = {
            "master_ref": str(record.get("master_ref") or ""),
            "documents_read": len(list(record.get("documents_read") or [])),
            "fields": len(list(record.get("fields") or [])),
        }
    write_json_atomic(root / _INDEX_NAME, {"schema_version": 1, "masters": entries})


def index(workspace: Workspace) -> dict:
    path = _root(workspace) / _INDEX_NAME
    if not path.exists():
        return {"schema_version": 1, "masters": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {"schema_version": 1, "masters": {}}
    return value if isinstance(value, dict) else {"schema_version": 1, "masters": {}}


__all__ = [
    "MASTER_ONLY_KEYS",
    "apply_reading",
    "catalog",
    "empty",
    "field_names",
    "has_read",
    "index",
    "late_fields",
    "list_masters",
    "load_master",
    "master",
    "master_ref",
    "reset",
    "schema_fields",
    "types_with_master",
    "vocabulary",
    "unread_for_field",
]
