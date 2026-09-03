"""Per-workspace cycle rulesets: roles, join keys, and assertions.

A ruleset is what replaces an authored pack. A model proposes it from the
induced schemas, code measures it against the corpus, and an auditor approves or
edits it. Only an approved ruleset may produce results, and the approval is a
human act: nothing in this module lets an agent approve its own rules.

Join keys and assertions are kept structurally apart because they are different
operations. A join key builds the evidence graph — it decides *which* purchase
order a given invoice belongs to. An assertion tests a graph already built — it
decides whether their amounts agree. Collapsing them reads natural and is wrong:
without the first, the second has nothing to compare against but the whole
corpus.

Roles, not document types, are the addressing scheme. A role names a position in
the cycle and constrains what may fill it, which is what allows two roles of the
same type — an original and a revised invoice — in one cycle.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from . import document_schemas, document_types
from .workspaces import Workspace, WorkspaceError, write_json_atomic

_DIRNAME = "CycleRulesets"
_INDEX_NAME = ".index.json"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: How a role, join key or assertion id must read, in the words a proposer is
#: given. Stated once so the worker that writes rules and the store that keeps
#: them cannot drift apart.
RULE_ID_RULE = (
    "lower_snake_case: letters, digits and underscores only, starting with a "
    "letter"
)


def valid_rule_id(value: object) -> bool:
    """Whether a role, join key or assertion id is well formed.

    Public because the worker proposing rules enforces this one turn earlier,
    where a repair can still fix it. Left to the store alone it costs a whole
    run: by commit the model turn has already succeeded, and nothing retries it.
    """

    return bool(_RULE_ID_RE.fullmatch(str(value or "")))

#: Statuses a ruleset moves through. ``superseded`` is terminal and retained:
#: results keep their own ruleset_hash and stay readable against the rules that
#: produced them.
STATUSES = frozenset({"proposed", "approved", "rejected", "superseded"})

#: How a join key compares two identifier values.
MATCH_MODES = frozenset({"normalized_equal", "exact_equal"})

CARDINALITIES = frozenset({"one", "many"})

#: Who made a ruleset effective. ``auditor`` is a person acting through the
#: review screen; ``agent`` is an ``mode: auto`` run approving rules it wrote,
#: which the auditor delegated by selecting that mode. Stored rather than
#: inferred, because the two carry different weight in a file and a reader must
#: be able to tell them apart years later. Ordered auditor-first: it is the
#: default for a record written before the distinction existed.
APPROVER_KINDS = ("auditor", "agent")


class RulesetError(WorkspaceError):
    """A ruleset definition or reference is unusable."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _root(workspace: Workspace) -> Path:
    return workspace.root / _DIRNAME


def _path(workspace: Workspace, ruleset_id: str) -> Path:
    if not _ID_RE.fullmatch(str(ruleset_id or "")):
        raise RulesetError(f"Invalid ruleset id '{ruleset_id}'.")
    root = _root(workspace).resolve()
    path = (root / f"{ruleset_id}.json").resolve()
    if not path.is_relative_to(root):
        raise RulesetError("Unsafe ruleset reference.")
    return path


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def _object(value: object, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise RulesetError(f"{label} must be an object.")
    return value


def _text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RulesetError(f"{label} is required.")
    return text


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _validate_roles(value: object, workspace: Workspace) -> list[dict]:
    if not isinstance(value, (list, tuple)) or not value:
        raise RulesetError("A ruleset needs at least one role.")
    local = document_schemas.local_type_ids(workspace)
    roles: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _object(raw, f"roles[{index}]")
        name = _text(item.get("name"), f"roles[{index}].name")
        if not _RULE_ID_RE.fullmatch(name):
            raise RulesetError(f"Role name '{name}' is invalid.")
        if name in seen:
            raise RulesetError(f"Role '{name}' is declared twice.")
        seen.add(name)
        document_type = document_types.validate(
            item.get("document_type"), local_types=local
        )
        if document_type == document_types.OTHER:
            raise RulesetError(
                f"Role '{name}' cannot be filled by the 'other' bucket; retype first."
            )
        cardinality = str(item.get("cardinality") or "one")
        if cardinality not in CARDINALITIES:
            raise RulesetError(f"Role '{name}' has an unsupported cardinality.")
        roles.append({
            "name": name,
            "document_type": document_type,
            "cardinality": cardinality,
            "required": bool(item.get("required", True)),
        })
    return roles


def _role_field(
    value: object, label: str, roles: Mapping[str, dict], workspace: Workspace
) -> dict:
    item = _object(value, label)
    role = _text(item.get("role"), f"{label}.role")
    if role not in roles:
        raise RulesetError(f"{label} names unknown role '{role}'.")
    field = _text(item.get("field"), f"{label}.field")
    schema = document_schemas.load_schema(workspace, roles[role]["document_type"])
    if schema is None:
        raise RulesetError(
            f"{label} refers to '{roles[role]['document_type']}', which has no schema."
        )
    names = {str(entry.get("name")) for entry in (schema.get("fields") or [])}
    if field not in names:
        # Fails closed rather than matching a similarly-named field: an approved
        # rule pointing at a field the schema no longer states is unusable, and
        # guessing a replacement would silently change what was approved.
        raise RulesetError(
            f"{label} names field '{field}', which '{roles[role]['document_type']}' "
            "does not state."
        )
    return {"role": role, "field": field}


def _field_role(
    workspace: Workspace, roles: Mapping[str, dict], operand: Mapping[str, object]
) -> str:
    schema = document_schemas.get_schema(workspace, roles[operand["role"]]["document_type"])
    for entry in schema.get("fields") or []:
        if str(entry.get("name")) == operand["field"]:
            return str(entry.get("role") or "")
    return ""


def _validate_join_keys(
    value: object, roles: Mapping[str, dict], workspace: Workspace
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise RulesetError("join_keys must be an array.")
    keys: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _object(raw, f"join_keys[{index}]")
        key_id = _text(item.get("id"), f"join_keys[{index}].id")
        if not _RULE_ID_RE.fullmatch(key_id):
            raise RulesetError(f"Join key id '{key_id}' is invalid.")
        if key_id in seen:
            raise RulesetError(f"Join key '{key_id}' is declared twice.")
        seen.add(key_id)
        left = _role_field(item.get("left"), f"join_keys[{index}].left", roles, workspace)
        right = _role_field(item.get("right"), f"join_keys[{index}].right", roles, workspace)
        if left == right:
            raise RulesetError(f"Join key '{key_id}' joins a field to itself.")
        # Only identifier-role fields build edges. An amount or a date that
        # happens to match on two records is a coincidence, not a link, and
        # joining on one would fuse unrelated transactions.
        for side, operand in (("left", left), ("right", right)):
            field_role = _field_role(workspace, roles, operand)
            if field_role != "identifier":
                raise RulesetError(
                    f"Join key '{key_id}' {side} field '{operand['field']}' has role "
                    f"'{field_role or 'unknown'}'; only identifier fields can join."
                )
        match = str(item.get("match") or "normalized_equal")
        if match not in MATCH_MODES:
            raise RulesetError(f"Join key '{key_id}' has an unsupported match mode.")
        keys.append({
            "id": key_id,
            "left": left,
            "right": right,
            "match": match,
            "rationale": str(item.get("rationale") or "").strip(),
        })
    return keys


def _validate_assertions(
    value: object, roles: Mapping[str, dict], workspace: Workspace
) -> list[dict]:
    if not isinstance(value, (list, tuple)) or not value:
        raise RulesetError("A ruleset needs at least one assertion.")
    assertions: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _object(raw, f"assertions[{index}]")
        assertion_id = _text(item.get("id"), f"assertions[{index}].id")
        if not _RULE_ID_RE.fullmatch(assertion_id):
            raise RulesetError(f"Assertion id '{assertion_id}' is invalid.")
        if assertion_id in seen:
            raise RulesetError(f"Assertion '{assertion_id}' is declared twice.")
        seen.add(assertion_id)
        # An assertion names the fields and says what they must show. It does
        # not name how to compare them: the auditor approving these rules is
        # approving what the cycle must demonstrate, and exact-against-
        # normalized equality is neither their judgment to make nor answerable
        # before a value has been read.
        requirement = str(item.get("requirement") or item.get("rationale") or "").strip()
        if not requirement:
            raise RulesetError(
                f"Assertion '{assertion_id}' states no requirement. Say what these "
                "fields must show for the control to hold."
            )
        left = _role_field(item.get("left"), f"assertions[{index}].left", roles, workspace)
        right_raw = item.get("right")
        if right_raw is None:
            # One operand is the requirement that the field be stated at all.
            right = None
        else:
            right = _role_field(right_raw, f"assertions[{index}].right", roles, workspace)
            if right == left:
                raise RulesetError(
                    f"Assertion '{assertion_id}' compares a field to itself."
                )
        assertions.append({
            "id": assertion_id,
            "label": str(item.get("label") or "").strip(),
            "left": left,
            "right": right,
            "requirement": requirement,
            "rationale": str(item.get("rationale") or "").strip(),
        })
    return assertions


def _validate_anchor(
    value: object, roles: Mapping[str, dict], workspace: Workspace
) -> dict:
    item = _object(value, "anchor")
    operand = _role_field(item, "anchor", roles, workspace)
    if _field_role(workspace, roles, operand) != "identifier":
        raise RulesetError("The anchor must name an identifier field; it seeds the graph.")
    return {
        "table": _text(item.get("table"), "anchor.table"),
        "column": _text(item.get("column"), "anchor.column"),
        "role": operand["role"],
        "field": operand["field"],
    }


def definition_of(record: Mapping[str, object]) -> dict:
    """The hashed part of a ruleset: the rules, not their measurement.

    ``measured`` is recomputed whenever the corpus moves and is deliberately
    outside the hash — a fan-out that shifted because documents were added has
    not changed what an auditor approved, and invalidating results for it would
    make approval meaningless.
    """

    return {
        "roles": record.get("roles") or [],
        "anchor": record.get("anchor") or {},
        "join_keys": [
            {key: value for key, value in item.items() if key != "measured"}
            for item in (record.get("join_keys") or [])
        ],
        "assertions": [
            {key: value for key, value in item.items() if key != "measured"}
            for item in (record.get("assertions") or [])
        ],
    }


def validate(workspace: Workspace, payload: object) -> dict:
    """Validate a proposal or an edit into its stored shape."""

    item = _object(payload, "ruleset")
    roles = _validate_roles(item.get("roles"), workspace)
    by_name = {role["name"]: role for role in roles}
    anchor = _validate_anchor(item.get("anchor"), by_name, workspace)
    join_keys = _validate_join_keys(item.get("join_keys"), by_name, workspace)
    assertions = _validate_assertions(item.get("assertions"), by_name, workspace)

    # Every role beyond the anchor's own must be reachable, or the cycle silently
    # never binds it. Checked here rather than at run time because an unreachable
    # role is a defect in the rules, not in the evidence.
    reachable = {anchor["role"]}
    changed = True
    while changed:
        changed = False
        for key in join_keys:
            left, right = key["left"]["role"], key["right"]["role"]
            for source, target in ((left, right), (right, left)):
                if source in reachable and target not in reachable:
                    reachable.add(target)
                    changed = True
    unreachable = sorted(role["name"] for role in roles if role["name"] not in reachable)
    if unreachable:
        raise RulesetError(
            "No join key reaches "
            + ", ".join(f"'{name}'" for name in unreachable)
            + f" from the anchor role '{anchor['role']}'."
        )

    record = {
        "cycle_label": str(item.get("cycle_label") or "").strip(),
        "roles": roles,
        "anchor": anchor,
        "join_keys": join_keys,
        "assertions": assertions,
        "schema_refs": sorted(
            (
                document_schemas.schema_ref(workspace, document_type)
                for document_type in {role["document_type"] for role in roles}
            ),
            key=lambda ref: ref["document_type"],
        ),
    }
    record["ruleset_hash"] = canonical_sha256(definition_of(record))
    return record


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def save(
    workspace: Workspace,
    payload: object,
    *,
    ruleset_id: str | None = None,
    proposed_by: str = "agent",
) -> dict:
    """Store a proposal, or replace an existing one that is not yet approved.

    An approved ruleset is immutable. Editing one means proposing a successor and
    approving that, so the rules a stored result was produced under can always be
    read back exactly.
    """

    validated = validate(workspace, payload)
    identifier = str(ruleset_id or f"lnk-{uuid.uuid4().hex[:12]}")
    path = _path(workspace, identifier)
    prior = _read_json(path, {}) if path.exists() else None
    if prior and str(prior.get("status")) == "approved":
        raise RulesetError(
            "An approved ruleset cannot be edited; propose a successor instead."
        )
    record = {
        "ruleset_id": identifier,
        "status": "proposed",
        **validated,
        "proposed_by": str(proposed_by or "agent"),
        "created": prior.get("created") if prior else utcnow(),
        "updated": utcnow(),
        "approved_by": None,
        # Stamped at approval. Absent on a record written before auto-mode
        # approval existed, which `approver_kind` reads as an auditor's.
        "approved_by_kind": None,
        "approved_at": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, record)
    _reindex(workspace)
    return record


def load(workspace: Workspace, ruleset_id: str) -> dict | None:
    path = _path(workspace, ruleset_id)
    if not path.exists():
        return None
    return _read_json(path, {}) or None


def get(workspace: Workspace, ruleset_id: str) -> dict:
    record = load(workspace, ruleset_id)
    if record is None:
        raise RulesetError(f"Ruleset '{ruleset_id}' not found.")
    return record


def list_rulesets(workspace: Workspace) -> list[dict]:
    root = _root(workspace)
    if not root.exists():
        return []
    records = [
        _read_json(path, {})
        for path in sorted(root.glob("*.json"))
        if not path.name.startswith(".")
    ]
    return sorted(
        (record for record in records if record),
        key=lambda item: str(item.get("created") or ""),
    )


def effective(workspace: Workspace) -> dict | None:
    """The one approved ruleset, if there is one.

    A workspace holds at most one. An engagement covering a second cycle leaves
    its documents extracted but unbound, which readiness reports as a degradation
    rather than passing silently.
    """

    approved = [
        record for record in list_rulesets(workspace)
        if str(record.get("status")) == "approved"
    ]
    return approved[-1] if approved else None


def approve(
    workspace: Workspace,
    ruleset_id: str,
    *,
    approved_by: str,
    approved_by_kind: str = "auditor",
) -> dict:
    """Approve a proposal, superseding whatever was approved before.

    ``approved_by`` is required. ``approved_by_kind`` says what kind of approver
    it names, and it is the only thing that distinguishes the two ways a ruleset
    becomes effective.

    An earlier version of this docstring read "never an agent identity by
    construction". That is no longer true, and the change is deliberate rather
    than an erosion: selecting ``mode: auto`` on a run is the auditor delegating
    the approvals of that run, and withholding this one made auto mode silently
    unable to vouch a cycle at all — the agent wrote the rules, every stage
    reported success, and the engagement fell back to prose tests nobody asked
    for. What auto mode does not buy is the *appearance* of a signature, which
    is why the kind is stored and the working paper prints it: a reader of the
    file can always tell which of the two happened.
    """

    who = _text(approved_by, "approved_by")
    kind = str(approved_by_kind or "").strip()
    if kind not in APPROVER_KINDS:
        raise RulesetError(
            f"approved_by_kind must be one of: {', '.join(APPROVER_KINDS)}."
        )
    record = get(workspace, ruleset_id)
    status = str(record.get("status"))
    if status == "approved":
        return record
    if status != "proposed":
        raise RulesetError(f"A '{status}' ruleset cannot be approved.")
    # Revalidate at the moment of approval: schemas may have moved since the
    # proposal was written, and approving rules that no longer resolve would
    # store an approval nothing can execute. A vanished field raises here, naming
    # itself, which is more use to an auditor than a hash mismatch would be.
    revalidated = validate(workspace, record)
    if revalidated["ruleset_hash"] != record.get("ruleset_hash"):
        raise RulesetError(
            "The rules this ruleset was proposed with no longer validate to the "
            "same definition; re-propose it before approving."
        )
    # A schema that merely gained a field leaves every rule resolving and the
    # definition hash untouched, but the stored refs would still claim the
    # version the proposal saw. Refreshing them keeps the approval's provenance
    # honest about what it was actually checked against.
    record["schema_refs"] = revalidated["schema_refs"]
    for other in list_rulesets(workspace):
        if str(other.get("status")) == "approved":
            other["status"] = "superseded"
            other["updated"] = utcnow()
            write_json_atomic(_path(workspace, str(other["ruleset_id"])), other)
    record["status"] = "approved"
    record["approved_by"] = who
    record["approved_by_kind"] = kind
    record["approved_at"] = utcnow()
    record["updated"] = utcnow()
    write_json_atomic(_path(workspace, ruleset_id), record)
    _reindex(workspace)
    return record


def approver_kind(record: Mapping[str, object]) -> str:
    """What kind of approver made this ruleset effective.

    Defaults to ``auditor`` for a record approved before auto-mode approval
    existed: every one of those went through the review screen, so reading them
    that way states a fact rather than guessing one.
    """

    kind = str(record.get("approved_by_kind") or "").strip()
    return kind if kind in APPROVER_KINDS else "auditor"


def reject(workspace: Workspace, ruleset_id: str) -> dict:
    record = get(workspace, ruleset_id)
    if str(record.get("status")) == "approved":
        raise RulesetError("An approved ruleset cannot be rejected; supersede it instead.")
    record["status"] = "rejected"
    record["updated"] = utcnow()
    write_json_atomic(_path(workspace, ruleset_id), record)
    _reindex(workspace)
    return record


def set_measured(
    workspace: Workspace,
    ruleset_id: str,
    *,
    join_keys: Mapping[str, Mapping[str, object]] = {},
    assertions: Mapping[str, Mapping[str, object]] = {},
) -> dict:
    """Attach code-computed corpus statistics without moving the ruleset hash.

    Measurement is never model-supplied. Fan-out is what makes the most dangerous
    approval legible: a key whose values fan out to hundreds of records is an
    entity identifier, and approving it would fuse every unrelated transaction
    into one cluster.
    """

    record = get(workspace, ruleset_id)
    before = record.get("ruleset_hash")
    for item in record.get("join_keys") or []:
        measured = join_keys.get(str(item.get("id")))
        if measured is not None:
            item["measured"] = dict(measured)
    for item in record.get("assertions") or []:
        measured = assertions.get(str(item.get("id")))
        if measured is not None:
            item["measured"] = dict(measured)
    record["measured_at"] = utcnow()
    if canonical_sha256(definition_of(record)) != before:
        raise RulesetError("Measurement must not change the ruleset definition.")
    write_json_atomic(_path(workspace, ruleset_id), record)
    return record


def is_current(workspace: Workspace, ruleset_hash: object) -> bool:
    """Whether a stored result's stamp matches the effective ruleset."""

    approved = effective(workspace)
    if approved is None:
        return False
    return str(ruleset_hash or "") == str(approved.get("ruleset_hash") or "")


def _reindex(workspace: Workspace) -> None:
    root = _root(workspace)
    entries = [
        {
            "ruleset_id": record["ruleset_id"],
            "status": record.get("status"),
            "cycle_label": record.get("cycle_label"),
            "ruleset_hash": record.get("ruleset_hash"),
            "updated": record.get("updated"),
        }
        for record in list_rulesets(workspace)
    ]
    root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(root / _INDEX_NAME, {"schema_version": 1, "rulesets": entries})


def index(workspace: Workspace) -> dict:
    return _read_json(_root(workspace) / _INDEX_NAME, {"schema_version": 1, "rulesets": []})
