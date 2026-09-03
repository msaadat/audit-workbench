"""Cycle evidence built from approved rulesets and schema-guided extractions.

This is the half of cycle vouching the registry packs used to own. What replaced
them is not a smaller registry: it is a ruleset held in the workspace, proposed
by a model from the induced schemas, measured by code against the corpus, and
approved by an auditor. The rules are data, so a treasury engagement gets a
treasury cycle without anyone shipping a treasury pack.

What is unchanged is deliberate, and imported rather than reimplemented. The
graph is still a bounded breadth-first traversal. Roles are still bound with
cardinality conflicts surfaced rather than resolved. The six comparison
operators, the deterministic sampler, the citation catalogue and the result
rollup are the registry engine's, because none of them ever depended on a pack.

The edges are the difference worth stating. The registry decided what could join
by asking whether an identifier kind had been declared ``transaction`` or
``entity`` at authoring time. Here an edge exists because an auditor approved a
join key whose measured fan-out they read first. That is a weaker guarantee on
paper and a much stronger one in practice: the old rule was a claim someone made
about a vocabulary, and this one is a count taken from the documents in hand.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from typing import Iterable, Mapping

from . import (
    cycle_measurement,
    cycle_rulesets,
    document_classification,
    document_schemas,
    document_types,
)
from .cycle_vouching import (
    ASSERTIONS,
    ASSERTION_VERDICTS,
    CURRENT_EVALUATION_STATES,
    DISPOSITION_STATES,
    EVALUATION_STATES,
    ITEMS_INPUTS_KEY,
    MAX_CYCLE_RECORDS,
    MAX_GRAPH_HOPS,
    MAX_ITEMS,
    MAX_ROLES,
    MAX_TRAVERSED_EDGES,
    SCHEMA_VERSION,
    CycleSchemaError,
    SelectionConfirmationRequired,
    apply_cross_item_reuse,
    disposition_current,
    execution_pending,
    normalize_evidence_value,
)

# The vocabulary-agnostic half of the registry engine, reused as-is rather than
# reimplemented: the operators an auditor's stored verdicts were produced by,
# the deterministic sampler their selection was drawn with, the citation
# catalogue their evidence anchors resolve through, and the rollup their status
# is read from. These are private in ``cycle_vouching`` only because nothing
# outside it needed them before; phase 9 deletes the registry half around them
# and this module inherits them outright.
from .cycle_vouching import _aggregate_evaluation as aggregate_evaluation
from .cycle_vouching import _bounded_value as bounded_value
from .cycle_vouching import _cache as _request_cache
from .cycle_vouching import _dedupe_evidence as dedupe_evidence
from .cycle_vouching import _evidence_catalog as evidence_catalog
from .cycle_vouching import _frame_signature as frame_signature
from .cycle_vouching import _plain_json as plain_json
from .cycle_vouching import _sample_row_indices as sample_row_indices
from .cycle_vouching import _sha1_hash as sha1_hash


def record_id(document_id: str, index: int, record: Mapping[str, object]) -> str:
    """Content-addressed identity for one extracted record.

    The content is in the hash on purpose. A re-extraction that changes a value
    produces a different record, and a different record cannot inherit a stored
    verdict by carrying the same id — the result goes stale by construction
    rather than by a staleness check someone has to remember to run.
    """

    material = json.dumps(
        [str(document_id), int(index), record],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"REC-{digest[:24].upper()}"


def structured_evidence(workspace) -> tuple[list[dict], dict[str, str]]:
    """Every current structured record in the workspace, with its identity.

    Returns the records and, separately, each document's extraction hash. The
    second is what tells a stored result that the analysis under it moved, and
    it is kept off the record so that re-analysing a document which produced an
    identical record does not churn that record's id.
    """

    cache = _request_cache.get()
    cache_key = ("structured_evidence", id(workspace))
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            # Shared, not copied: every reader indexes or traverses these
            # records and none writes to them, and a request-scoped cache is
            # a promise that no extraction moves while it is open.
            return cached

    records: list[dict] = []
    extraction_hashes: dict[str, str] = {}
    for row in cycle_measurement.structured_records(workspace):
        document_id = str(row.get("document_id") or "")
        record = row.get("record") or {}
        extraction_hashes[document_id] = str(row.get("extraction_hash") or "")
        records.append({
            "record_id": record_id(document_id, int(row.get("record_index") or 0), record),
            "document_id": document_id,
            "document_type": str(row.get("document_type") or ""),
            "record_index": int(row.get("record_index") or 0),
            "content_hash": sha1_hash(record),
            "fields": list(record.get("fields") or []),
            "additional_fields": list(record.get("additional_fields") or []),
        })
    records.sort(key=lambda item: (item["document_id"], item["record_index"]))
    result = (records, extraction_hashes)
    if cache is not None:
        cache[cache_key] = result
    return result


def stated(record: Mapping[str, object], field: str) -> list[Mapping[str, object]]:
    """Every entry of one schema field on a record, in the order extracted."""

    return [
        item
        for item in record.get("fields") or []
        if str(item.get("name") or "") == field and str(item.get("value") or "").strip()
    ]


class PreparedCycle:
    """One ruleset and one evidence set, indexed once for many traversals.

    :func:`link` is called per population row, and every call would otherwise
    re-index the whole corpus against every join key — work that depends only
    on the ruleset and the records, neither of which move between rows.
    """

    __slots__ = (
        "ruleset",
        "ruleset_hash",
        "roles",
        "records",
        "by_id",
        "extraction_hashes",
        "edges",
        "fields",
        "anchors",
        "linkages",
    )

    def __init__(
        self,
        *,
        ruleset: Mapping[str, object],
        records: list[dict],
        extraction_hashes: Mapping[str, str],
        edges: Mapping[str, Mapping[str, Mapping[str, tuple[str, ...]]]],
        fields: Mapping[str, Mapping[str, dict]],
        anchors: Mapping[str, tuple[str, ...]],
    ):
        self.ruleset = dict(ruleset)
        self.ruleset_hash = str(ruleset.get("ruleset_hash") or "")
        self.roles = {
            str(role.get("name")): dict(role) for role in ruleset.get("roles") or []
        }
        self.records = records
        self.by_id = {str(record["record_id"]): record for record in records}
        self.extraction_hashes = dict(extraction_hashes)
        self.edges = edges
        self.fields = fields
        self.anchors = anchors
        # Traversals already made from this index, by the anchor values they
        # started from. A traversal depends on nothing but the index and its
        # limits, so one made for one test answers every other test that runs
        # the same rules over the same rows — seven evidence-linked tests on
        # one ledger linked every row seven times over.
        self.linkages: dict[tuple, dict] = {}

    def role_type(self, role: object) -> str:
        return str((self.roles.get(str(role)) or {}).get("document_type") or "")

    def field_definition(self, role: object, field: object) -> dict:
        return (self.fields.get(self.role_type(role)) or {}).get(str(field)) or {}


def prepare(
    workspace,
    ruleset: Mapping[str, object],
    *,
    records: Iterable[Mapping[str, object]] | None = None,
    extraction_hashes: Mapping[str, str] | None = None,
) -> PreparedCycle:
    """Index the corpus against one ruleset's join keys.

    Inside a request cache scope, one index per approved ruleset: the index
    depends on the rules and the corpus, and neither moves inside a scope. The
    tests of one cycle all name the same rules, and each was re-indexing the
    same records and re-reading the same schemas as the one before it.
    """

    cache = _request_cache.get() if records is None else None
    cache_key = None
    if cache is not None and str(ruleset.get("ruleset_hash") or ""):
        cache_key = ("prepared", id(workspace), str(ruleset.get("ruleset_hash")))
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    if records is None:
        record_values, hashes = structured_evidence(workspace)
    else:
        record_values = [dict(value) for value in records]
        hashes = dict(extraction_hashes or {})

    roles = {str(role.get("name")): dict(role) for role in ruleset.get("roles") or []}
    if len(roles) > MAX_ROLES:
        raise CycleSchemaError(f"A cycle may declare at most {MAX_ROLES} roles.")

    fields: dict[str, dict[str, dict]] = {}
    for role in roles.values():
        document_type = str(role.get("document_type") or "")
        if document_type in fields:
            continue
        schema = document_schemas.load_schema(workspace, document_type)
        fields[document_type] = {
            str(entry.get("name")): dict(entry)
            for entry in (schema or {}).get("fields") or []
        }

    by_type: dict[str, list[dict]] = {}
    for record in record_values:
        by_type.setdefault(str(record.get("document_type") or ""), []).append(record)

    edges: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {}
    for key in ruleset.get("join_keys") or []:
        key_id = str(key.get("id") or "")
        match = str(key.get("match") or "normalized_equal")
        sides: dict[str, dict[str, tuple[str, ...]]] = {}
        for side in ("left", "right"):
            operand = key.get(side) or {}
            role = roles.get(str(operand.get("role"))) or {}
            document_type = str(role.get("document_type") or "")
            field = str(operand.get("field") or "")
            index: dict[str, list[str]] = {}
            for record in by_type.get(document_type) or []:
                for entry in stated(record, field):
                    value = cycle_measurement.join_value(entry.get("value"), match)
                    if value:
                        index.setdefault(value, []).append(str(record["record_id"]))
            sides[side] = {
                value: tuple(sorted(dict.fromkeys(ids))) for value, ids in index.items()
            }
        edges[key_id] = sides

    # The anchor seed lookup is the one per-row step in ``link``, and it
    # depends only on the ruleset and the records. Indexed here with the same
    # conservative normalizer ``link`` matches with, so a traversal starts from
    # a dict lookup rather than a scan of the whole corpus per population row.
    anchor = ruleset.get("anchor") or {}
    anchor_role = roles.get(str(anchor.get("role"))) or {}
    anchor_field = str(anchor.get("field") or "")
    anchor_seeds: dict[str, list[str]] = {}
    for record in by_type.get(str(anchor_role.get("document_type") or "")) or []:
        for entry in stated(record, anchor_field):
            value = cycle_measurement.normalize(entry.get("value"))
            if value:
                anchor_seeds.setdefault(value, []).append(str(record["record_id"]))
    anchors = {
        value: tuple(sorted(dict.fromkeys(ids))) for value, ids in anchor_seeds.items()
    }

    prepared = PreparedCycle(
        ruleset=ruleset,
        records=record_values,
        extraction_hashes=hashes,
        edges=edges,
        fields=fields,
        anchors=anchors,
    )
    if cache is not None and cache_key is not None:
        cache[cache_key] = prepared
    return prepared


#: Fields a durable Document Test owns independently of its cycle definition.
#: Regeneration replaces the definition and its derived coverage; it must not
#: silently reset the record's workflow status or the auditor's own links.
PRESERVED_ON_REGENERATION = (
    "id",
    "created",
    "created_by",
    "status",
    "procedure_refs",
    "criteria",
    "evidence_refs",
    "finding_refs",
)

_OPPOSITE = {"left": "right", "right": "left"}


def link(
    prepared: PreparedCycle,
    *,
    anchor_values: Iterable[object],
    max_hops: int = MAX_GRAPH_HOPS,
    max_records: int = MAX_CYCLE_RECORDS,
    max_edges: int = MAX_TRAVERSED_EDGES,
) -> dict:
    """Traverse the approved join keys breadth-first from one population row.

    The traversal is over records, not documents. A voucher stating three
    invoice lines is three records, and joining them as one would let a rule the
    auditor approved on a per-invoice fan-out silently operate per-voucher.

    Answered from the index's own memo when the same values were traversed
    from it before — see ``PreparedCycle.linkages``. The result is shared, and
    every reader copies what it keeps: the materializer lists the bindings
    into its item and then round-trips the item through JSON.
    """

    # The anchor is matched with the conservative normalizer whatever a join
    # key's match mode is: it compares a population-table value to a printed
    # one, and those two are written by different systems. ``prepared.anchors``
    # is keyed by that same normalization and holds only the anchor role's own
    # document type, so the seed set is a lookup rather than a corpus scan.
    wanted = tuple(sorted({
        cycle_measurement.normalize(value)
        for value in anchor_values
        if str(value or "").strip()
    }))
    memo_key = (wanted, int(max_hops), int(max_records), int(max_edges))
    linkage = prepared.linkages.get(memo_key)
    if linkage is None:
        linkage = prepared.linkages[memo_key] = _traverse(
            prepared,
            wanted=wanted,
            max_hops=max_hops,
            max_records=max_records,
            max_edges=max_edges,
        )
    return linkage


def _traverse(
    prepared: PreparedCycle,
    *,
    wanted: tuple[str, ...],
    max_hops: int,
    max_records: int,
    max_edges: int,
) -> dict:
    ruleset = prepared.ruleset
    join_keys = {str(key.get("id")): key for key in ruleset.get("join_keys") or []}

    seeds = sorted(
        {
            record_id
            for value in wanted
            for record_id in prepared.anchors.get(value) or ()
        }
    )

    reached: dict[str, dict] = {}
    traversed_edges = 0
    queue: deque[tuple[str, list[dict]]] = deque((seed, []) for seed in seeds)

    def limited(limit: str, trigger: Mapping[str, object], hops: int) -> dict:
        return {
            "state": "needs_review",
            "review_reason": f"graph_{limit}_limit_exceeded",
            "limit": limit,
            "counts": {"hops": hops, "records": len(reached), "edges": traversed_edges},
            "triggering_key": dict(trigger),
            "records": [],
            "role_bindings": [],
            "role_conflicts": [],
            "unassigned_records": [],
            "ruleset_hash": prepared.ruleset_hash,
        }

    while queue:
        current_id, path = queue.popleft()
        if current_id in reached:
            continue
        record = prepared.by_id.get(current_id)
        if record is None:
            continue
        if len(reached) + 1 > max_records:
            return limited("records", {"record_id": current_id}, len(path) + 1)
        reached[current_id] = {"record": record, "matched_by": path}
        if len(path) >= max_hops:
            # Reached at the hop limit: kept, but not expanded from. Refusing
            # the whole traversal here would discard a complete cycle for the
            # sake of a record beyond it that nothing asked for.
            continue
        document_type = str(record.get("document_type") or "")
        for key_id in sorted(join_keys):
            key = join_keys[key_id]
            match = str(key.get("match") or "normalized_equal")
            sides = prepared.edges.get(key_id) or {}
            for side in ("left", "right"):
                operand = key.get(side) or {}
                if prepared.role_type(operand.get("role")) != document_type:
                    continue
                opposite = sides.get(_OPPOSITE[side]) or {}
                for entry in stated(record, str(operand.get("field") or "")):
                    value = cycle_measurement.join_value(entry.get("value"), match)
                    if not value:
                        continue
                    for neighbour in opposite.get(value, ()):
                        if neighbour == current_id or neighbour in reached:
                            continue
                        traversed_edges += 1
                        if traversed_edges > max_edges:
                            return limited(
                                "edges",
                                {"join_key": key_id, "normalized_value": value},
                                len(path) + 1,
                            )
                        queue.append((
                            neighbour,
                            [*path, {
                                "join_key": key_id,
                                "normalized_value": value,
                                "from_record_id": current_id,
                                "to_record_id": neighbour,
                            }],
                        ))

    return bind_roles(prepared, reached, traversed_edges)


def bind_roles(prepared: PreparedCycle, reached: Mapping[str, dict], edges: int) -> dict:
    """Assign the reached records to the ruleset's roles."""

    bindings: list[dict] = []
    conflicts: list[dict] = []
    assigned: set[str] = set()
    for name, role in sorted(prepared.roles.items()):
        document_type = str(role.get("document_type") or "")
        matches = [
            value
            for value in reached.values()
            if str(value["record"].get("document_type") or "") == document_type
        ]
        if str(role.get("cardinality") or "one") == "one" and len(matches) > 1:
            # Two candidates for a position that holds one is not a failure to
            # be resolved by picking: which invoice this payment settles is a
            # question the evidence did not answer, and answering it here would
            # bury the ambiguity under a verdict.
            conflicts.append({
                "kind": "role_cardinality_conflict",
                "role": name,
                "record_ids": sorted(
                    str(value["record"]["record_id"]) for value in matches
                ),
                "bindable": False,
            })
            continue
        for value in matches:
            record = value["record"]
            assigned.add(str(record["record_id"]))
            bindings.append({
                "role": name,
                "document_id": str(record["document_id"]),
                "record_id": str(record["record_id"]),
                "document_type": document_type,
                "record_content_hash": str(record.get("content_hash") or ""),
                "matched_by": value["matched_by"],
                "reuse_across_items": str(role.get("reuse_across_items") or "exclusive"),
                "extraction_hash": prepared.extraction_hashes.get(
                    str(record["document_id"]), ""
                ),
            })
    return {
        "state": "needs_review" if conflicts else "linked",
        "ruleset_hash": prepared.ruleset_hash,
        "records": [
            {**value["record"], "matched_by": value["matched_by"]}
            for _, value in sorted(reached.items())
        ],
        "role_bindings": sorted(
            bindings, key=lambda item: (item["role"], item["record_id"])
        ),
        "role_conflicts": conflicts,
        "unassigned_records": sorted(set(reached) - assigned),
        "counts": {
            "hops": max(
                (len(value["matched_by"]) for value in reached.values()), default=0
            ),
            "records": len(reached),
            "edges": edges,
        },
    }


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def normalize_value(
    raw: object, field: Mapping[str, object], citation: object
) -> dict:
    """Type one printed value according to the schema field that names it.

    Extraction reports what the document says and nothing else — deciding that
    ``29-Apr -2024`` is a date is this side's job, and keeping it here is what
    lets a value that will not type be reported as invalid evidence instead of
    being quietly compared as text.
    """

    value_type = str(field.get("value_type") or "text")
    text = str(raw or "").strip()
    if value_type == "identifier":
        # The conservative normalizer, the same one the join keys use: an
        # identifier that linked two records must compare equal to itself.
        if not text:
            return {
                "raw_value": raw,
                "value": None,
                "normalization_status": "invalid",
                "normalization_error": "empty identifier",
                "citation": citation,
            }
        return {
            "raw_value": text,
            "value": cycle_measurement.normalize(text),
            "normalization_status": "normalized",
            "normalization_error": None,
            "citation": citation,
        }
    try:
        return normalize_evidence_value(
            raw, semantic_type=value_type, citation=citation
        )
    except CycleSchemaError as error:
        return {
            "raw_value": text,
            "value": None,
            "normalization_status": "invalid",
            "normalization_error": str(error),
            "citation": citation,
        }


def operand_entries(
    prepared: PreparedCycle,
    item: Mapping[str, object],
    operand: Mapping[str, object],
    catalog: Mapping[tuple[str, str], dict],
) -> list[dict]:
    """Every value the bound records state for one role and field."""

    role = str(operand.get("role") or "")
    field_name = str(operand.get("field") or "")
    definition = prepared.field_definition(role, field_name)
    entries: list[dict] = []
    for binding in item.get("role_bindings") or []:
        if str(binding.get("role") or "") != role:
            continue
        record = prepared.by_id.get(str(binding.get("record_id") or "")) or {}
        document_id = str(binding.get("document_id") or "")
        for fact in stated(record, field_name):
            citation = str(fact.get("citation") or "")
            envelope = normalize_value(fact.get("value"), definition, citation)
            anchor = catalog.get((document_id, citation))
            entries.append({
                "role": role,
                "document_id": document_id,
                "record_id": str(binding.get("record_id") or ""),
                "entry": int(fact.get("entry") or 1),
                "raw_value": plain_json(envelope.get("raw_value")),
                "value": plain_json(envelope.get("value")),
                "normalization_status": str(
                    envelope.get("normalization_status") or "invalid"
                ),
                "normalization_error": envelope.get("normalization_error"),
                "evidence_refs": [anchor] if anchor else [],
            })
    return entries


def resolve_operand(
    prepared: PreparedCycle,
    item: Mapping[str, object],
    operand: Mapping[str, object],
    catalog: Mapping[tuple[str, str], dict],
) -> dict:
    """Reduce one role/field operand to a single comparable value, or say why not.

    A role bound to several records resolves when they agree and is ambiguous
    when they do not. Picking one would be inventing the answer the documents
    declined to give.
    """

    role = str(operand.get("role") or "")
    conflict = next(
        (
            value
            for value in item.get("role_conflicts") or []
            if str(value.get("role") or "") == role
        ),
        None,
    )
    if conflict is not None:
        return {
            "state": "ambiguous",
            "entries": [],
            "record_ids": list(conflict.get("record_ids") or []),
        }
    entries = operand_entries(prepared, item, operand, catalog)
    if not entries:
        return {"state": "missing_evidence", "entries": []}
    if any(entry["normalization_status"] == "invalid" for entry in entries):
        return {"state": "invalid_extraction", "entries": entries}
    normalized = [
        entry
        for entry in entries
        if entry["normalization_status"] == "normalized"
        and entry.get("value") not in (None, "")
    ]
    if not normalized:
        return {"state": "missing_evidence", "entries": entries}
    distinct = {
        json.dumps(entry.get("value"), sort_keys=True, default=str)
        for entry in normalized
    }
    if len(distinct) != 1:
        return {"state": "ambiguous", "entries": entries}
    return {"state": "resolved", "value": normalized[0]["value"], "entries": entries}


def assertion_inputs(assertion: Mapping[str, object], *, item: Mapping[str, object]) -> dict:
    """Everything a stored verdict depended on, hashed so a change invalidates it."""

    roles = {
        str((operand or {}).get("role") or "")
        for operand in (assertion.get("left"), assertion.get("right"))
        if isinstance(operand, Mapping)
    } - {""}
    bindings = [
        binding
        for binding in item.get("role_bindings") or []
        if str(binding.get("role") or "") in roles
    ]
    material = {
        "population_source_sha1": str(
            (item.get("population_ref") or {}).get("source_sha1") or ""
        ),
        "frozen_row_sha1": sha1_hash(item.get("frozen_row") or {}),
        "bound_record_hashes": [
            list(value)
            for value in sorted({
                str(binding.get("record_id") or ""): str(
                    binding.get("record_content_hash") or ""
                )
                for binding in bindings
            }.items())
        ],
        "extraction_hashes": [
            list(value)
            for value in sorted({
                str(binding.get("document_id") or ""): str(
                    binding.get("extraction_hash") or ""
                )
                for binding in bindings
            }.items())
        ],
        "role_binding_sha1": sha1_hash([
            {
                "role": binding.get("role"),
                "document_id": binding.get("document_id"),
                "record_id": binding.get("record_id"),
                "matched_by": binding.get("matched_by") or [],
            }
            for binding in bindings
        ]),
    }
    material["input_sha1"] = sha1_hash(material)
    return material


def result_reusable(
    result: Mapping[str, object],
    *,
    assertion_sha1: str,
    inputs: Mapping[str, object],
    ruleset_hash: str,
) -> bool:
    return bool(
        result.get("verdict") in ASSERTION_VERDICTS - {"not_run"}
        and not result.get("stale")
        and result.get("assertion_sha1") == assertion_sha1
        and result.get("ruleset_hash") == ruleset_hash
        and result.get("input_hashes") == inputs
    )


# --------------------------------------------------------------------------- #
# the test definition
# --------------------------------------------------------------------------- #
SELECTION_MODES = frozenset({"sample", "evidence_linked"})
SAMPLING_METHODS = frozenset({"random", "interval", "stratified"})


def is_ruleset_backed(test: Mapping[str, object]) -> bool:
    """Whether this cycle test is defined by a ruleset rather than a pack."""

    return bool(str(((test.get("definition") or {}).get("ruleset_id")) or ""))


def resolve_ruleset(workspace, test: Mapping[str, object]) -> dict:
    """The exact rules a test was built on, or a refusal naming what moved.

    Fails closed on a hash mismatch. A ruleset that changed after the test was
    built is a different set of rules, and re-linking under it would silently
    reattribute an auditor's dispositions to comparisons they never saw.
    """

    definition = test.get("definition") or {}
    ruleset_id = str(definition.get("ruleset_id") or "")
    if not ruleset_id:
        raise CycleSchemaError("This cycle test names no ruleset.")
    ruleset = cycle_rulesets.load(workspace, ruleset_id)
    if ruleset is None:
        raise CycleSchemaError(f"Cycle ruleset '{ruleset_id}' no longer exists.")
    expected = str(definition.get("ruleset_hash") or "")
    if expected and str(ruleset.get("ruleset_hash") or "") != expected:
        raise CycleSchemaError(
            "The cycle ruleset changed after this test was built; regenerate the "
            "test against the approved rules."
        )
    return ruleset


def validate_selection(value: object) -> dict:
    selection = value if isinstance(value, Mapping) else {}
    mode = str(selection.get("mode") or "sample")
    if mode not in SELECTION_MODES:
        raise CycleSchemaError(f"Unsupported selection mode '{mode}'.")
    if mode == "evidence_linked":
        return {"mode": mode}
    method = str(selection.get("method") or "")
    if method not in SAMPLING_METHODS:
        raise CycleSchemaError(f"Unsupported sampling method '{method}'.")
    size = int(selection.get("size") or 0)
    if size <= 0:
        raise CycleSchemaError("A sampled selection needs a positive size.")
    if size > MAX_ITEMS:
        raise CycleSchemaError(f"A cycle test may hold at most {MAX_ITEMS} items.")
    normalized = {"mode": mode, "method": method, "size": size, "seed": int(selection.get("seed") or 0)}
    if method == "stratified":
        normalized["stratify_by"] = str(selection.get("stratify_by") or "").strip()
        if not normalized["stratify_by"]:
            raise CycleSchemaError("A stratified selection needs a stratify_by column.")
    return normalized


def validate_cycle_test(workspace, test: Mapping[str, object]) -> dict:
    """Normalize the ruleset-backed half of a cycle test into executable shape.

    ``workspace`` may be ``None``, which validates the definition's shape
    without resolving the rules it names. Persistence normalizes stored tests in
    places that legitimately have no workspace in hand, and refusing there would
    make a test unreadable for the sake of a check the executing path repeats.
    """

    ruleset = resolve_ruleset(workspace, test) if workspace is not None else None
    definition = test.get("definition") or {}
    population = definition.get("population") or {}
    anchor = (ruleset or {}).get("anchor") or {}
    table = str(population.get("table") or anchor.get("table") or "")
    column = str(population.get("column") or anchor.get("column") or "")
    if not table or not column:
        raise CycleSchemaError("A cycle test needs a population table and column.")
    validated = {
        **dict(test),
        "definition": {
            "ruleset_id": str(
                (ruleset or {}).get("ruleset_id") or definition.get("ruleset_id") or ""
            ),
            "ruleset_hash": str(
                (ruleset or {}).get("ruleset_hash")
                or definition.get("ruleset_hash")
                or ""
            ),
            "population": {
                "table": table,
                "column": column,
                "selection": validate_selection(population.get("selection")),
            },
        },
    }
    if ruleset is not None:
        validated["ruleset"] = ruleset
    return validated


def cycle_definition_sha1(test: Mapping[str, object]) -> str:
    """Hash the executable definition and the rules it runs under.

    ``ruleset_hash`` stands in for the rules themselves: it already covers the
    roles, join keys and assertions exactly, and an approved ruleset is
    immutable, so naming it is naming them.
    """

    return sha1_hash({
        "schema_version": test.get("schema_version"),
        "rcm_id": test.get("rcm_id"),
        "requirement_refs": test.get("requirement_refs") or [],
        "procedure_key": test.get("procedure_key"),
        "definition": test.get("definition") or {},
    })


def stable_test_semantic_id(test: Mapping[str, object]) -> str:
    """Identity derived only from the durable procedure/population tuple."""

    population = (test.get("definition") or {}).get("population") or {}
    digest = hashlib.sha1(
        json.dumps(
            {
                "rcm_id": str(test.get("rcm_id") or ""),
                "kind": "cycle_vouch",
                "procedure_key": str(test.get("procedure_key") or ""),
                "table": str(population.get("table") or ""),
                "column": str(population.get("column") or ""),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"cycle_vouch:{digest}"


def stable_cycle_test_id(test: Mapping[str, object]) -> str:
    digest = hashlib.sha1(stable_test_semantic_id(test).encode("utf-8")).hexdigest()
    return f"DT-{digest[:8].upper()}"


def stable_cycle_item_id(test: Mapping[str, object], anchor_value: object) -> str:
    """Semantic item identity, independent of source-row ordering."""

    definition = test.get("definition") or {}
    population = definition.get("population") or {}
    digest = hashlib.sha256(
        json.dumps(
            [
                str(test.get("semantic_id") or stable_test_semantic_id(test)),
                str(definition.get("ruleset_hash") or ""),
                str(population.get("table") or ""),
                str(population.get("column") or ""),
                cycle_measurement.normalize(anchor_value),
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"ITEM-{digest[:24].upper()}"


def normalize_cycle_item(test: Mapping[str, object], value: object) -> dict:
    """Structural validation of one stored cycle item under its ruleset."""

    if not isinstance(value, Mapping):
        raise CycleSchemaError("A cycle item must be an object.")
    item = dict(value)
    if not str(item.get("id") or "").strip():
        raise CycleSchemaError("A cycle item needs an id.")
    ruleset_hash = str((test.get("definition") or {}).get("ruleset_hash") or "")
    evaluation = item.get("evaluation") or {"state": "not_run"}
    if str(evaluation.get("state") or "") not in EVALUATION_STATES:
        raise CycleSchemaError("Cycle item evaluation state is unsupported.")
    disposition = item.get("disposition") or {
        "state": "pending",
        "evaluated_definition_sha1": None,
        "stale": False,
    }
    if str(disposition.get("state") or "") not in DISPOSITION_STATES:
        raise CycleSchemaError("Cycle item disposition state is unsupported.")
    if not isinstance(disposition.get("stale", False), bool):
        raise CycleSchemaError("Cycle item disposition stale must be boolean.")
    results = item.get("result_by_assertion") or {}
    if not isinstance(results, Mapping):
        raise CycleSchemaError("cycle item.result_by_assertion must be an object.")
    for key, result in results.items():
        if not isinstance(result, Mapping):
            raise CycleSchemaError(f"result_by_assertion.{key} must be an object.")
        if result.get("verdict") not in ASSERTION_VERDICTS:
            raise CycleSchemaError(f"Assertion result '{key}' has an unsupported verdict.")
        if ruleset_hash and str(result.get("ruleset_hash") or "") != ruleset_hash:
            raise CycleSchemaError(
                f"Assertion result '{key}' was produced under different rules."
            )
    item.update(
        evaluation=dict(evaluation),
        disposition=dict(disposition),
        result_by_assertion=dict(results),
    )
    item.pop("state", None)
    return item


# --------------------------------------------------------------------------- #
# materialization and evaluation
# --------------------------------------------------------------------------- #
#: Bumped when `materialize_cycle_population` changes what it would produce
#: from unchanged inputs, so a fingerprint written by the old projection stops
#: vouching for items the new one would draw differently.
MATERIALIZATION_VERSION = 1


def _inputs_sha1(
    validated: Mapping[str, object],
    source_sha1: str,
    records: Iterable[Mapping[str, object]],
    extraction_hashes: Mapping[str, str],
) -> str:
    """The identity of everything materialization reads.

    The definition and the rules it names, the population as it stands, and the
    evidence set — each record by its content-addressed id, so a re-extraction
    that changed a value is a different set, and each document's extraction
    hash, so one that moved without changing a record still counts. What the
    projection copies onto an item beside those (the objective, the semantic
    id the item ids are derived from) is in too. Nothing about the stored items
    is: the fingerprint says what the items were drawn *from*, and
    idempotence says the drawing would come out the same.
    """
    definition = validated.get("definition") or {}
    return sha1_hash({
        "version": MATERIALIZATION_VERSION,
        "definition_sha1": cycle_definition_sha1(validated),
        "ruleset_hash": str(definition.get("ruleset_hash") or ""),
        "semantic_id": str(
            validated.get("semantic_id") or stable_test_semantic_id(validated)
        ),
        "objective": str(validated.get("objective") or ""),
        "source_sha1": source_sha1,
        "records": sorted(
            (
                str(record.get("document_id") or ""),
                str(record.get("document_type") or ""),
                str(record.get("record_id") or ""),
            )
            for record in records
        ),
        "extraction_hashes": sorted(
            (str(key), str(value)) for key, value in extraction_hashes.items()
        ),
    })


def materialization_inputs_sha1(workspace, test: Mapping[str, object]) -> str:
    """What `materialize_cycle_population` would read now, without reading the rows.

    A stored test carries the fingerprint of the inputs its items were drawn
    from. A reader that finds this equal to it knows the items are what the
    projection would produce, and is spared producing them: the whole
    population is otherwise re-linked against the whole corpus on every read
    to learn, most of the time, that nothing moved.
    """
    validated = validate_cycle_test(workspace, test)
    frame = workspace.get_frame(validated["definition"]["population"]["table"])
    records, extraction_hashes = structured_evidence(workspace)
    return _inputs_sha1(validated, frame_signature(frame), records, extraction_hashes)


def materialize_cycle_items(workspace, test: Mapping[str, object]) -> list[dict]:
    """Select population rows and bind each one's linked record closure."""

    return materialize_cycle_population(workspace, test)[0]


def materialize_cycle_population(
    workspace, test: Mapping[str, object]
) -> tuple[list[dict], str]:
    """The items and the fingerprint of the inputs they were drawn from.

    A writer that persists the items persists the fingerprint beside them —
    see ``doc_tests.ITEMS_INPUTS_KEY`` — which is what lets a later read tell
    a current population from one that has to be drawn again.
    """

    cache = _request_cache.get()
    cache_key = None
    if cache is not None and str(test.get("id") or ""):
        cache_key = (
            "ruleset_items",
            id(workspace),
            str(test.get("id")),
            sha1_hash(test.get("items") or []),
        )
        cached = cache.get(cache_key)
        if cached is not None:
            # Shared with every reader in the scope rather than copied for
            # each: a projection reads these items and assigns the list into
            # its own view of the test, and a writer runs outside any scope.
            return cached

    validated = validate_cycle_test(workspace, test)
    ruleset = validated["ruleset"]
    definition = validated["definition"]
    population = definition["population"]
    selection = population["selection"]
    assertions = list(ruleset.get("assertions") or [])
    # Once per assertion, not once per assertion per item: a twenty-assertion
    # ruleset over a hundred items was hashing the same twenty dicts two
    # thousand times.
    assertion_sha1s = {str(assertion["id"]): sha1_hash(assertion) for assertion in assertions}
    roles = list(ruleset.get("roles") or [])
    ruleset_hash = str(definition.get("ruleset_hash") or "")

    frame = workspace.get_frame(population["table"])
    source_sha1 = frame_signature(frame)
    column = population["column"]
    # The whole ledger is walked in order, so it is streamed rather than fetched
    # a row at a time by index; a sample is the rows it names.
    selected_rows = (
        enumerate(frame.iter_rows(named=True))
        if selection.get("mode") == "evidence_linked"
        else ((index, frame.row(index, named=True)) for index in sample_row_indices(frame, selection))
    )
    prepared = prepare(workspace, ruleset)
    inputs_sha1 = _inputs_sha1(
        validated, source_sha1, prepared.records, prepared.extraction_hashes
    )
    definition_sha1 = cycle_definition_sha1(validated)
    existing_by_id = {str(item.get("id") or ""): item for item in test.get("items") or []}

    materialized: list[dict] = []
    for source_row, row in selected_rows:
        anchor_value = row.get(column)
        if anchor_value is None or not str(anchor_value).strip():
            continue
        linkage = link(prepared, anchor_values=[anchor_value])
        if selection.get("mode") == "evidence_linked" and not linkage.get("records"):
            continue
        item_id = stable_cycle_item_id(validated, anchor_value)
        bindings = list(linkage.get("role_bindings") or [])
        bound_roles = {str(binding.get("role") or "") for binding in bindings}
        item = {
            "id": item_id,
            "label": str(anchor_value),
            "instruction": str(test.get("objective") or "Vouch the transaction cycle."),
            "population_ref": {
                "table": population["table"],
                "source_row": source_row,
                "source_sha1": source_sha1,
            },
            "frozen_row": plain_json(row),
            "cycle_identifiers": [{
                "role": str((ruleset.get("anchor") or {}).get("role") or ""),
                "field": str((ruleset.get("anchor") or {}).get("field") or ""),
                "value": plain_json(anchor_value),
            }],
            "role_bindings": bindings,
            "document_ids": list(
                dict.fromkeys(str(binding.get("document_id") or "") for binding in bindings)
            ),
            "unassigned_records": list(linkage.get("unassigned_records") or []),
            "missing_roles": [
                str(role["name"])
                for role in roles
                if role.get("required", True) and str(role["name"]) not in bound_roles
            ],
            "role_conflicts": list(linkage.get("role_conflicts") or []),
            "linkage_state": str(linkage.get("state") or "linked"),
            **(
                {
                    "linkage_review": {
                        key: linkage.get(key)
                        for key in ("review_reason", "limit", "counts", "triggering_key")
                        if linkage.get(key) is not None
                    }
                }
                if linkage.get("review_reason")
                else {}
            ),
            "result_by_assertion": {},
            "evaluation": {
                "state": "not_run",
                "definition_sha1": definition_sha1,
                "result_sha1": None,
            },
            "disposition": {
                "state": "pending",
                "evaluated_definition_sha1": None,
                "stale": False,
            },
            "evidence_refs": [],
        }
        old = existing_by_id.get(item_id) or {}
        old_results = old.get("result_by_assertion") or {}
        prior_evaluation_current = str(
            (old.get("evaluation") or {}).get("state") or "not_run"
        ) in CURRENT_EVALUATION_STATES
        for assertion in assertions:
            key = str(assertion["id"])
            assertion_sha1 = assertion_sha1s[key]
            inputs = assertion_inputs(assertion, item=item)
            old_result = old_results.get(key) or {}
            if result_reusable(
                old_result,
                assertion_sha1=assertion_sha1,
                inputs=inputs,
                ruleset_hash=ruleset_hash,
            ):
                item["result_by_assertion"][key] = dict(old_result)
            else:
                item["result_by_assertion"][key] = {
                    "ruleset_hash": ruleset_hash,
                    "assertion_sha1": assertion_sha1,
                    "input_hashes": inputs,
                    "verdict": "not_run",
                    "display": "",
                    "comparisons": [],
                    "evidence_refs": [],
                    # Adding one assertion to a formerly evaluated item makes the
                    # aggregate evaluation stale even though that new key has no
                    # prior result object of its own.
                    "stale": bool(old_result) or prior_evaluation_current,
                    "result_sha1": None,
                }
        item["evaluation"]["state"] = aggregate_evaluation(item)
        if item["evaluation"]["state"] in CURRENT_EVALUATION_STATES:
            item["evaluation"]["result_sha1"] = sha1_hash(item["result_by_assertion"])
        item["evidence_refs"] = dedupe_evidence(
            anchor
            for result in item["result_by_assertion"].values()
            for anchor in result.get("evidence_refs") or []
        )
        if old.get("runner_note"):
            item["runner_note"] = str(old["runner_note"])
        if old.get("disposition_history"):
            item["disposition_history"] = copy.deepcopy(
                list(old.get("disposition_history") or [])
            )
        old_disposition = dict(old.get("disposition") or {})
        if old_disposition:
            item["disposition"] = {
                "state": str(old_disposition.get("state") or "pending"),
                "evaluated_definition_sha1": old_disposition.get(
                    "evaluated_definition_sha1"
                ),
                "stale": bool(old_disposition.get("stale")),
            }
            if item["disposition"]["state"] != "pending" and (
                item["evaluation"]["state"] not in CURRENT_EVALUATION_STATES
                or item["disposition"]["evaluated_definition_sha1"] != definition_sha1
            ):
                item["disposition"]["stale"] = True
        materialized.append(item)

    reuse_roles = [
        {
            "role": str(role.get("name")),
            "reuse_across_items": str(role.get("reuse_across_items") or "exclusive"),
        }
        for role in roles
    ]
    materialized = apply_cross_item_reuse(materialized, reuse_roles)
    for item in materialized:
        projected = aggregate_evaluation(item)
        if item["evaluation"].get("state") != projected:
            item["evaluation"]["state"] = projected
            if (item.get("disposition") or {}).get("state") != "pending":
                item["disposition"]["stale"] = True
    result = sorted(materialized, key=lambda item: item["id"])
    population = (result, inputs_sha1)
    if cache is not None and cache_key is not None:
        cache[cache_key] = population
        # The projection is idempotent: run again over its own output under
        # the same inputs it returns the same items, so the output is also
        # the answer for a test that already carries them. Without this entry
        # every test was materialized twice per scope — once from its stored
        # items by the reader that loaded it, and once more from the current
        # items by the readiness probe handed that loaded test.
        cache[cache_key[:3] + (sha1_hash(result),)] = population
    return population


def evaluate_cycle_item(
    workspace,
    test: Mapping[str, object],
    item: dict,
    *,
    prepared: PreparedCycle | None = None,
    judgments: Mapping[str, Mapping[str, object]] | None = None,
) -> dict:
    """Bind each assertion to its evidence, and record the verdict it carries.

    Resolution is local and stays local: which document fills a role, which
    field an operand reads, and whether that field yielded exactly one usable
    value are questions about the records, and they decide ``missing_evidence``,
    ``ambiguous`` and ``invalid_extraction`` here.

    Whether two resolved values *agree* is not that kind of question. It used to
    be answered by a comparison operator chosen when the matrix was written, and
    on real documents that operator was wrong more often than right: an amount
    printed 'PKR 2,000,000.00' against '2,000,000.00', a vendor carrying its
    code in one record and not the other, a date no ISO parser accepts. Those
    are differences in presentation, and every one of them was reported as an
    exception against documents that agreed. Agreement is now judged against the
    values, by ``fieldwork.cycle_vouch``, and arrives here in ``judgments``.

    An assertion whose operands resolved but which no judgment covers is left
    ``not_run`` — pending, exactly as it reads before any evaluation. Nothing
    infers agreement from the absence of a verdict.
    """

    validated = validate_cycle_test(workspace, test)
    ruleset = validated["ruleset"]
    ruleset_hash = str(validated["definition"].get("ruleset_hash") or "")
    if prepared is None:
        prepared = prepare(workspace, ruleset)
    catalog = evidence_catalog(
        workspace,
        [
            str(binding.get("document_id") or "")
            for binding in item.get("role_bindings") or []
        ],
    )
    results = item.setdefault("result_by_assertion", {})
    for assertion in ruleset.get("assertions") or []:
        key = str(assertion["id"])
        inputs = assertion_inputs(assertion, item=item)
        assertion_sha1 = sha1_hash(assertion)
        if result_reusable(
            results.get(key) or {},
            assertion_sha1=assertion_sha1,
            inputs=inputs,
            ruleset_hash=ruleset_hash,
        ):
            continue
        left = resolve_operand(prepared, item, assertion.get("left") or {}, catalog)
        comparisons = [{"side": "left", **left}]
        evidence = [
            anchor
            for entry in left.get("entries") or []
            for anchor in entry.get("evidence_refs") or []
        ]
        judgment = dict((judgments or {}).get(key) or {})
        if assertion.get("right") is None:
            # ``resolve_operand`` already decided this: it resolves only when
            # exactly one usable value exists, and otherwise names why. That a
            # field was stated is settled by reading it, so nothing is judged.
            verdict = "match" if left["state"] == "resolved" else str(left["state"])
            judgment = {}
        else:
            right = resolve_operand(
                prepared, item, assertion.get("right") or {}, catalog
            )
            comparisons.append({"side": "right", **right})
            evidence.extend(
                anchor
                for entry in right.get("entries") or []
                for anchor in entry.get("evidence_refs") or []
            )
            states = {left["state"], right["state"]}
            verdict = (
                "ambiguous"
                if "ambiguous" in states
                else "invalid_extraction"
                if "invalid_extraction" in states
                else "missing_evidence"
                if "missing_evidence" in states
                else str(judgment.get("verdict") or "not_run")
            )
        result = {
            "ruleset_hash": ruleset_hash,
            "assertion_sha1": assertion_sha1,
            "input_hashes": inputs,
            "verdict": verdict,
            # Why the reader reached this verdict, in its own words. A judged
            # verdict that cannot say what it compared is not evidence of
            # anything, so the reason travels with it into the working paper.
            "reason": str(judgment.get("reason") or ""),
            "display": " vs ".join(
                str(bounded_value(entry.get("value")))
                for entry in comparisons
                if entry.get("state") == "resolved"
            )[:240],
            "comparisons": comparisons,
            "evidence_refs": dedupe_evidence(evidence),
            "stale": False,
        }
        result["result_sha1"] = sha1_hash(result)
        results[key] = result
    item["evaluation"] = {
        "state": aggregate_evaluation(item),
        "definition_sha1": cycle_definition_sha1(validated),
        "result_sha1": sha1_hash(results),
    }
    item["evidence_refs"] = dedupe_evidence(
        anchor
        for result in results.values()
        for anchor in result.get("evidence_refs") or []
    )
    disposition = item.get("disposition") or {}
    if disposition.get("state") in {"confirmed", "exception"}:
        disposition["stale"] = True
    item["disposition"] = {
        "state": str(disposition.get("state") or "pending"),
        "evaluated_definition_sha1": disposition.get("evaluated_definition_sha1"),
        "stale": bool(disposition.get("stale")),
    }
    return item


def evaluate_cycle_test(
    workspace,
    test: Mapping[str, object],
    *,
    judgments: Mapping[str, Mapping[str, Mapping[str, object]]] | None = None,
) -> dict:
    """Materialize current inputs and evaluate only work that is not current.

    ``judgments`` is keyed by item id and then by assertion id. Called without
    it — a projection, a readiness probe, a reader opening the test — every
    assertion still binds its evidence, and the ones awaiting a verdict stay
    ``not_run``. That is what makes this safe to call from a read: it resolves,
    it never asks the model anything, and it never invents agreement.
    """

    output = dict(test)
    output["items"], output[ITEMS_INPUTS_KEY] = materialize_cycle_population(
        workspace, output
    )
    validated = validate_cycle_test(workspace, output)
    prepared = prepare(workspace, validated["ruleset"])
    for item in output["items"]:
        if execution_pending(item, cycle=True):
            evaluate_cycle_item(
                workspace,
                output,
                item,
                prepared=prepared,
                judgments=(judgments or {}).get(str(item.get("id"))),
            )
    dispositions_current = bool(output["items"]) and all(
        disposition_current(item, cycle=True) for item in output["items"]
    )
    # Rules that have since been superseded still evaluate — the hash pins
    # exactly what ran — but the test is not finished work until it has been
    # regenerated against the rules now in force.
    effective = cycle_rulesets.effective(workspace) or {}
    superseded = str(effective.get("ruleset_hash") or "") != str(
        validated["definition"].get("ruleset_hash") or ""
    )
    output["ruleset_superseded"] = superseded
    output["status"] = (
        "completed" if dispositions_current and not superseded else "review_required"
    )
    return output


# --------------------------------------------------------------------------- #
# building a test
# --------------------------------------------------------------------------- #
def coverage_for(workspace, ruleset: Mapping[str, object], table: str, column: str) -> dict:
    """What the approved rules actually reach across the whole population.

    Computed by linking, not estimated. An auditor choosing a selection is
    choosing among rows this engine can and cannot complete, and reporting that
    from anything other than the traversal itself would misdescribe the choice.
    """

    frame = workspace.get_frame(table)
    prepared = prepare(workspace, ruleset)
    required = [
        str(role.get("name"))
        for role in ruleset.get("roles") or []
        if role.get("required", True)
    ]
    linked_rows = 0
    complete = 0
    missing_counts: dict[str, int] = {}
    for index in range(frame.height):
        value = frame.row(index, named=True).get(column)
        if value is None or not str(value).strip():
            continue
        linkage = link(prepared, anchor_values=[value])
        if not linkage.get("records"):
            continue
        linked_rows += 1
        bound = {str(binding.get("role") or "") for binding in linkage.get("role_bindings") or []}
        missing = [name for name in required if name not in bound]
        for name in missing:
            missing_counts[name] = missing_counts.get(name, 0) + 1
        if not missing and not linkage.get("role_conflicts"):
            complete += 1
    return {
        "population_rows": frame.height,
        "linked_rows": linked_rows,
        "complete_cycles": complete,
        "missing_role_counts": dict(sorted(missing_counts.items())),
    }


def selection_confirmation(reach: Mapping[str, object]) -> dict | None:
    """The deterministic sample proposal an oversized evidence-linked reach needs."""

    eligible = int(reach.get("linked_rows") or 0)
    if eligible <= MAX_ITEMS:
        return None
    return {
        "kind": "selection_confirmation",
        "eligible_row_count": eligible,
        "maximum_items": MAX_ITEMS,
        "suggested_selection": {"mode": "sample", "method": "random", "size": 25, "seed": 42},
        "reason": (
            f"{eligible} evidence-linked rows qualify; confirm a deterministic "
            f"sample of at most {MAX_ITEMS} before the test is persisted."
        ),
    }


def build_cycle_vouch_test(workspace, payload: Mapping[str, object]) -> dict:
    """Validate and persist one cycle test against the workspace's approved rules.

    Raises :class:`SelectionConfirmationRequired` when an evidence-linked reach
    exceeds the item cap: no test is persisted and no rows are truncated until
    the caller confirms a deterministic sample.
    """

    rcm_id = str(payload.get("rcm_id") or "").strip()
    rcm_row = next(
        (row for row in workspace.rcm if str(row.get("id") or "") == rcm_id), None
    )
    if rcm_row is None:
        raise CycleSchemaError(f"RCM row '{rcm_id}' not found.")

    requested = str(((payload.get("definition") or {}).get("ruleset_id")) or "").strip()
    ruleset = (
        cycle_rulesets.load(workspace, requested)
        if requested
        else cycle_rulesets.effective(workspace)
    )
    if ruleset is None:
        raise CycleSchemaError(
            "No cycle ruleset has been approved for this workspace. Propose and "
            "approve one before building a cycle test."
        )
    if str(ruleset.get("status") or "") != "approved":
        # Results are produced under approved rules only. A proposal is a draft
        # an auditor has not yet stood behind, and evaluating one would put their
        # name on comparisons they never reviewed.
        raise CycleSchemaError(
            f"Cycle ruleset '{ruleset.get('ruleset_id')}' is "
            f"{ruleset.get('status')}, not approved."
        )

    # Selector-exact coverage. A cited requirement demands specific fields
    # agree; an approved ruleset either holds that comparison or it does not,
    # and a related assertion over neighbouring fields is a different test. Both
    # gaps refuse the build, and each names which repair it needs: a field
    # nothing states is an evidence gap, an uncovered comparison is a rules gap.
    required = required_comparisons_for(rcm_row, payload.get("requirement_refs") or [])
    unanswerable = unanswerable_comparisons(workspace, required)
    if unanswerable:
        raise CycleSchemaError([
            f"Control attribute '{item.get('attribute_key')}' requires "
            f"'{item.get('key')}', which the current schemas cannot express: "
            + "; ".join(item.get("reasons") or [])
            for item in unanswerable
        ])
    uncovered = uncovered_comparisons(ruleset, required)
    if uncovered:
        raise CycleSchemaError([
            f"The approved cycle rules hold no assertion answering required "
            f"comparison '{item.get('key')}' of control attribute "
            f"'{item.get('attribute_key')}' "
            f"({_comparison_text(item)}). Add it in the cycle rules review and "
            "approve, rather than generating a test that leaves it untested."
            for item in uncovered
        ])

    anchor = ruleset.get("anchor") or {}
    population = (payload.get("definition") or {}).get("population") or {}
    table = str(population.get("table") or anchor.get("table") or "")
    column = str(population.get("column") or anchor.get("column") or "")
    selection = validate_selection(population.get("selection"))

    reach = coverage_for(workspace, ruleset, table, column)
    if selection.get("mode") == "evidence_linked":
        confirmation = selection_confirmation(reach)
        if confirmation is not None:
            raise SelectionConfirmationRequired({
                **confirmation,
                "ruleset_id": str(ruleset.get("ruleset_id") or ""),
                "table": table,
                "column": column,
            })

    selected_rows = (
        int(reach["linked_rows"])
        if selection.get("mode") == "evidence_linked"
        else min(int(selection.get("size") or 0), int(reach["population_rows"]))
    )
    assurance_scope = (
        "targeted_evidence_only"
        if selection.get("mode") == "evidence_linked"
        else "sampled_population"
    )
    coverage = {
        "population_rows": reach["population_rows"],
        "selected_rows": selected_rows,
        "rows_with_evidence": (
            reach["linked_rows"] if selection.get("mode") == "evidence_linked" else None
        ),
        "complete_cycles": reach["complete_cycles"],
        "missing_role_counts": reach["missing_role_counts"],
        "selection_basis": selection.get("mode"),
        "assurance_scope": assurance_scope,
    }

    test = {
        **dict(payload),
        "schema_version": SCHEMA_VERSION,
        "kind": "cycle_vouch",
        "steps": [],
        "rcm_refs": [rcm_id],
        "definition": {
            "ruleset_id": str(ruleset.get("ruleset_id") or ""),
            "ruleset_hash": str(ruleset.get("ruleset_hash") or ""),
            "population": {"table": table, "column": column, "selection": selection},
        },
    }
    test.pop("registry", None)
    semantic_id = stable_test_semantic_id(test)
    test_id = str(payload.get("id") or "").strip() or stable_cycle_test_id(test)
    if not str(payload.get("title") or "").strip():
        raise CycleSchemaError("cycle test.title is required.")
    if not str(payload.get("objective") or "").strip():
        raise CycleSchemaError("cycle test.objective is required.")
    test.update(
        id=test_id,
        title=str(payload.get("title")).strip(),
        objective=str(payload.get("objective")).strip(),
        semantic_id=semantic_id,
        methodology_refs=list(payload.get("methodology_refs") or []),
        agent_run_id=payload.get("agent_run_id"),
        workflow_parent_sha1=payload.get("workflow_parent_sha1"),
        selection_confirmation=dict(payload.get("selection_confirmation") or {}) or None,
        coverage=coverage,
        spec={
            "selection_basis": selection.get("mode"),
            "assurance_scope": assurance_scope,
            "coverage": coverage,
        },
        items=[],
    )

    from . import doc_tests

    if doc_tests.exists(workspace, test_id):
        existing = doc_tests.load_test(workspace, test_id)
        preserved = {
            key: existing[key] for key in PRESERVED_ON_REGENERATION if key in existing
        }
        # Regeneration replaces the definition and its derived coverage; it does
        # not discard prior evaluation or the auditor's own history. The
        # materializer reconciles these semantic item ids against current rows
        # and marks only the changed inputs stale.
        prior_items = list(existing.get("items") or [])
        existing.clear()
        existing.update({**test, **preserved, "items": prior_items})
        return doc_tests.save_test(workspace, existing)
    return doc_tests.create_test(workspace, test)


def grid_assertions(ruleset: Mapping[str, object]) -> list[dict]:
    """A ruleset's assertions in the column shape the grid projection reads.

    An adapter, not a second definition. The grid names an assertion by ``key``
    and reads operands through a ``source`` discriminator it shares with the
    pack projection; a ruleset addresses everything by role, so the discriminator
    is constant here. Phase 9 collapses the two shapes into this one.
    """

    columns = []
    for assertion in ruleset.get("assertions") or []:
        left = assertion.get("left") or {}
        right = assertion.get("right")
        columns.append({
            "key": str(assertion.get("id") or ""),
            "label": str(assertion.get("label") or assertion.get("id") or ""),
            "operator": str(assertion.get("operator") or ""),
            "left": {"source": "role", **dict(left)},
            "right": {"source": "role", **dict(right)} if right else None,
            "tolerance": assertion.get("tolerance"),
        })
    return columns


def candidate(workspace) -> dict:
    """What a cycle test can be built on, for the create form to bind to.

    There is one candidate, not a list. The registry generated candidates
    because a pack described several possible cycles and the population column
    that seeded each had to be inferred; an approved ruleset already names its
    anchor table and column, because that is part of what the auditor approved.
    What remains genuinely open is the selection, so this returns the reach they
    need to choose one.
    """

    ruleset = cycle_rulesets.effective(workspace)
    if ruleset is None:
        return {"kind": "ruleset", "ruleset": None, "reason": "no_approved_ruleset"}

    anchor = ruleset.get("anchor") or {}
    table = str(anchor.get("table") or "")
    column = str(anchor.get("column") or "")
    known = {
        str(value.get("name") or "")
        for value in [*workspace.tables, *workspace.joins]
    }
    if table not in known:
        # Stated rather than raised: the rules are sound and the table they name
        # is not loaded, which the auditor fixes by loading it.
        return {
            "kind": "ruleset",
            "ruleset_id": str(ruleset.get("ruleset_id") or ""),
            "ruleset": None,
            "reason": "population_table_missing",
            "anchor": {"table": table, "column": column},
        }
    reach = coverage_for(workspace, ruleset, table, column)
    return {
        "kind": "ruleset",
        "ruleset_id": str(ruleset.get("ruleset_id") or ""),
        "ruleset_hash": str(ruleset.get("ruleset_hash") or ""),
        "cycle_label": str(ruleset.get("cycle_label") or ""),
        "roles": list(ruleset.get("roles") or []),
        "anchor": {
            "table": table,
            "column": column,
            "role": str(anchor.get("role") or ""),
            "field": str(anchor.get("field") or ""),
        },
        "assertions": [
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "operator": str(item.get("operator") or ""),
                "rationale": str(item.get("rationale") or ""),
            }
            for item in ruleset.get("assertions") or []
        ],
        "reach": reach,
        "selection_confirmation": selection_confirmation(reach),
    }


# --------------------------------------------------------------------------- #
# RCM control attributes, addressed by schema field
# --------------------------------------------------------------------------- #
#: A schema-backed transaction-cycle attribute. No registry reference and no
#: recipe citations: the comparison says what must agree, in the vocabulary the
#: documents were extracted under.
CONTROL_ATTRIBUTE_KEYS = frozenset({
    "key",
    "assertion",
    "requirement",
    "evidence_kind",
    "required_comparisons",
})

#: A comparison names the fields that must agree and says why, and deliberately
#: does not name how to compare them. Choosing between exact and normalized
#: equality is not an audit judgment and cannot be made here in any case: the
#: author is writing the matrix, long before anyone has seen that one document
#: prints 'PKR 2,000,000.00' where another prints '2,000,000.00'. Agreement is
#: judged against the values, where the values are.
COMPARISON_KEYS = frozenset({"key", "left", "right", "rationale"})

#: How a control's requirement is answered. Only the first reaches the cycle
#: engine; the rest name work performed and evidenced elsewhere.
EVIDENCE_KINDS = frozenset({
    "transaction_cycle",
    "tabular_population",
    "document_content",
    "manual_inspection",
    "inquiry",
    "mixed",
})

def schema_backed(attribute: Mapping[str, object]) -> bool:
    """Whether one control attribute states comparisons the cycle engine runs."""

    return attribute.get("required_comparisons") is not None


def _comparison_operand(
    workspace, value: object, label: str, errors: list[str]
) -> dict:
    """One side of a required comparison: a document type and a field on it."""

    if not isinstance(value, Mapping):
        errors.append(f"{label} must be an object with document_type and field.")
        return {}
    document_type = str(value.get("document_type") or "").strip()
    field = str(value.get("field") or "").strip()
    if not document_type or not field:
        errors.append(f"{label} needs a document_type and a field.")
        return {}
    if workspace is None:
        # Structure only. The response validator runs where no workspace is in
        # hand; the commit gate runs where one is, and that is where a field no
        # schema states has to fail closed.
        return {"document_type": document_type, "field": field}
    try:
        document_type = document_types.validate(
            document_type, local_types=document_schemas.local_type_ids(workspace)
        )
    except document_types.DocumentTypeError as error:
        errors.append(f"{label}: {error}")
        return {}
    schema = document_schemas.load_schema(workspace, document_type)
    if schema is None:
        errors.append(
            f"{label} names '{document_type}', which this engagement has no "
            "schema for. Classify and induce before requiring it."
        )
        return {}
    if field not in {str(entry.get("name")) for entry in schema.get("fields") or []}:
        # Fails closed rather than matching a similarly-named field: a
        # requirement pointing at a field the schema does not state is a
        # requirement nothing can answer, and substituting a related one would
        # silently change what the control was said to do.
        errors.append(
            f"{label} names field '{field}', which '{document_type}' does not state."
        )
        return {}
    return {"document_type": document_type, "field": field}


def validate_required_comparisons(
    workspace, value: object, *, label: str, errors: list[str]
) -> list[dict]:
    """Validate the comparisons one control attribute requires of a cycle."""

    if not isinstance(value, (list, tuple)) or not value:
        # Named as the missing contract rather than as a shape error: an
        # attribute declaring linked source records and stating nothing that
        # must agree describes no work, and the repair turn acts on that
        # sentence rather than on a type.
        errors.append(
            f"{label} is empty, so this attribute names no evidence contract. "
            "Say what must agree, or classify the requirement another way."
        )
        return []
    comparisons: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        entry_label = f"{label}[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{entry_label} must be an object.")
            continue
        unknown = sorted(str(key) for key in raw if str(key) not in COMPARISON_KEYS)
        if unknown:
            errors.extend(
                f"{entry_label} has unexpected key '{name}'. A comparison has "
                "key, left, right, and rationale. It does not name how to "
                "compare: say which fields must agree and why."
                for name in unknown
            )
            continue
        key = str(raw.get("key") or "").strip()
        if not key:
            errors.append(f"{entry_label}.key is required.")
            continue
        if key in seen:
            errors.append(f"{entry_label}.key '{key}' is used twice.")
            continue
        seen.add(key)
        rationale = str(raw.get("rationale") or "").strip()
        if not rationale:
            # The rationale carries what the operator used to pretend to carry.
            # "The invoice is settled for the amount the order committed" states
            # a requirement a reader can act on; ``equal_exact`` states a string
            # operation nobody asked for.
            errors.append(
                f"{entry_label}.rationale is required: say what these fields "
                "must show, in the terms the control is written in."
            )
            continue
        before = len(errors)
        left = _comparison_operand(workspace, raw.get("left"), f"{entry_label}.left", errors)
        # One operand is the requirement that a field be stated at all; two is
        # the requirement that they agree.
        right = (
            None
            if raw.get("right") is None
            else _comparison_operand(
                workspace, raw.get("right"), f"{entry_label}.right", errors
            )
        )
        if len(errors) != before:
            continue
        comparisons.append({
            "key": key,
            "left": left,
            "right": right,
            "rationale": rationale,
        })
    return comparisons


def validate_control_attribute(
    workspace,
    raw: Mapping[str, object],
    *,
    label: str,
    keys: set[str],
    errors: list[str],
) -> dict:
    """Validate one control attribute of an asserted control.

    Only a ``transaction_cycle`` attribute states comparisons, and it must:
    that is the whole content of declaring linked source records. Every other
    evidence strategy is answered somewhere the cycle engine never reaches, so
    a comparison on one would describe work nothing performs.
    """

    # Every unexpected key, not the first: a row carrying three of them is
    # repaired in one pass or in three, and the model doing the repairing only
    # gets one look at each error.
    for unknown in sorted(
        str(key) for key in raw if str(key) not in CONTROL_ATTRIBUTE_KEYS
    ):
        errors.append(
            f"{label} has unexpected key '{unknown}'. A control attribute has "
            "key, assertion, requirement, evidence_kind, and — for a "
            "transaction cycle — required_comparisons. It names no pack, cites "
            "no recipes, and does not list record kinds."
        )
    key = str(raw.get("key") or "").strip()
    if not key:
        errors.append(f"{label}.key is required.")
    elif key in keys:
        errors.append(f"Duplicate control attribute key '{key}'.")
    else:
        keys.add(key)
    if str(raw.get("assertion") or "") not in ASSERTIONS:
        errors.append(
            f"{label}.assertion '{raw.get('assertion')}' is not supported. It must "
            f"be exactly one of: {', '.join(sorted(ASSERTIONS))}."
        )
    if not str(raw.get("requirement") or "").strip():
        errors.append(f"{label}.requirement is required.")
    evidence_kind = str(raw.get("evidence_kind") or "")
    if evidence_kind not in EVIDENCE_KINDS:
        errors.append(
            f"{label}.evidence_kind '{evidence_kind}' is not supported. It must "
            f"be exactly one of: {', '.join(sorted(EVIDENCE_KINDS))}."
        )
    attribute = {
        "key": key,
        "assertion": str(raw.get("assertion") or ""),
        "requirement": str(raw.get("requirement") or "").strip(),
        "evidence_kind": evidence_kind,
    }
    if evidence_kind != "transaction_cycle":
        if raw.get("required_comparisons") is not None:
            errors.append(
                f"{label} states required_comparisons, which only a "
                f"transaction_cycle attribute may do; this one is "
                f"'{evidence_kind or 'unset'}'."
            )
        return attribute
    attribute["required_comparisons"] = validate_required_comparisons(
        workspace,
        raw.get("required_comparisons"),
        label=f"{label}.required_comparisons",
        errors=errors,
    )
    return attribute


def required_comparisons_for(
    rcm_row: Mapping[str, object], requirement_refs: Iterable[object]
) -> list[dict]:
    """Every comparison the cited requirements of one row demand.

    No expansion and no binding step. A pack cited an audit *shape* and left the
    record kinds to be decided against the evidence; a schema-backed attribute
    names the fields outright, because by the time the RCM is written the
    schemas already exist.
    """

    wanted = {str(reference).split(":", 1)[-1] for reference in requirement_refs or []}
    comparisons: list[dict] = []
    for attribute in rcm_row.get("control_attributes") or []:
        if not isinstance(attribute, Mapping) or not schema_backed(attribute):
            continue
        if attribute.get("evidence_kind") != "transaction_cycle":
            continue
        if str(attribute.get("key") or "") not in wanted:
            continue
        for comparison in attribute.get("required_comparisons") or []:
            if isinstance(comparison, Mapping):
                comparisons.append({**dict(comparison), "attribute_key": str(attribute.get("key") or "")})
    return comparisons


def _assertion_signature(
    assertion: Mapping[str, object], roles: Mapping[str, Mapping[str, object]]
) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    def side(operand: object) -> tuple[str, str] | None:
        if not isinstance(operand, Mapping):
            return None
        role = roles.get(str(operand.get("role") or "")) or {}
        return (
            str(role.get("document_type") or ""),
            str(operand.get("field") or ""),
        )

    return side(assertion.get("left")), side(assertion.get("right"))


def assertion_covers(
    assertion: Mapping[str, object],
    comparison: Mapping[str, object],
    roles: Mapping[str, Mapping[str, object]],
) -> bool:
    """Whether one approved assertion answers one required comparison exactly.

    Selector-exact on the operands, which is now the whole of it. Coverage used
    to require the assertion's comparison operator to equal the requirement's,
    and that is what a matrix could not reliably satisfy: the operator asked the
    author to choose between exact and normalized equality before any value had
    been seen, so a requirement written ``equal_exact`` went unanswered by the
    approved rule that read the very same two fields. Agreement is now judged
    against the values, so what an assertion has to establish is that it reads
    the fields the requirement names.
    """

    actual_left, actual_right = _assertion_signature(assertion, roles)

    def wanted(operand: object) -> tuple[str, str] | None:
        if not isinstance(operand, Mapping):
            return None
        return (
            str(operand.get("document_type") or ""),
            str(operand.get("field") or ""),
        )

    expected_left = wanted(comparison.get("left"))
    expected_right = wanted(comparison.get("right"))
    if actual_left == expected_left and actual_right == expected_right:
        return True
    # Reading two fields is symmetric: which side was written first is not part
    # of what the assertion establishes.
    return bool(
        expected_right is not None
        and actual_left == expected_right
        and actual_right == expected_left
    )


def comparison_text(comparison: Mapping[str, object]) -> str:
    """One comparison as a sentence, for an error a person has to act on."""

    def side(operand: object) -> str:
        if not isinstance(operand, Mapping):
            return "?"
        return f"{operand.get('document_type')}.{operand.get('field')}"

    if comparison.get("right") is None:
        return f"{side(comparison.get('left'))} must be present"
    return f"{side(comparison.get('left'))} agrees with {side(comparison.get('right'))}"


#: Retained for readers that knew the private name.
_comparison_text = comparison_text


def comparison_signature(comparison: Mapping[str, object]) -> tuple:
    """The operands that decide whether a comparison is covered.

    The same selector ``assertion_covers`` compares on, named so callers can
    group by it. Sorted for a pair because coverage reads two fields
    symmetrically: which side the matrix wrote first is not part of the
    requirement, so the two orderings are one piece of work.
    """

    def side(operand: object) -> tuple[str, str] | None:
        if not isinstance(operand, Mapping):
            return None
        return (
            str(operand.get("document_type") or ""),
            str(operand.get("field") or ""),
        )

    left, right = side(comparison.get("left")), side(comparison.get("right"))
    if right is None:
        return (left,)
    return tuple(sorted([left, right]))


def distinct_comparisons(
    comparisons: Iterable[Mapping[str, object]]
) -> list[dict]:
    """One comparison per distinct operand signature, first occurrence kept.

    A matrix routinely requires the same field pair from several control
    attributes — an amount that ``deal_accuracy`` and ``match_terms`` both
    depend on is one test, demanded twice. Counting those apart overstates the
    work by more than half on a real engagement, and asking for an assertion
    each yields duplicate rules that differ only by an id no coverage check
    reads.
    """

    seen: set[tuple] = set()
    distinct: list[dict] = []
    for comparison in comparisons or []:
        signature = comparison_signature(comparison)
        if signature in seen:
            continue
        seen.add(signature)
        distinct.append(dict(comparison))
    return distinct


def join_key_covers(
    join_key: Mapping[str, object],
    comparison: Mapping[str, object],
    roles: Mapping[str, Mapping[str, object]],
) -> bool:
    """Whether one approved join key answers one required comparison.

    A matrix requirement that two documents reference each other is answered by
    the join that binds them, not by an assertion repeating it. The distinction
    matters the other way round from usual: an assertion duplicating a join key
    *cannot fail*, because the pair it would test exists only because the join
    already matched. Demanding one would file a test incapable of finding an
    exception — the same defect the data-test validity gate refuses — and read
    as coverage while proving nothing.

    Selector-exact on the operands, which is now the entire test. It used to ask
    in addition what the join's ``match`` mode *proved* — a normalized join
    establishes nothing about the printed values, so it answered only a
    normalized requirement. That algebra is gone with the operators it compared:
    a check reading exactly the two fields the cycle was linked on cannot fail
    whatever mode bound them, and that is the whole of what this refuses.
    """

    actual_left, actual_right = _assertion_signature(join_key, roles)

    def wanted(operand: object) -> tuple[str, str] | None:
        if not isinstance(operand, Mapping):
            return None
        return (
            str(operand.get("document_type") or ""),
            str(operand.get("field") or ""),
        )

    expected = (wanted(comparison.get("left")), wanted(comparison.get("right")))
    if expected[1] is None:
        return False
    # A join is symmetric by construction: it binds a pair, and which side was
    # written first is not part of what it establishes.
    return (actual_left, actual_right) == expected or (
        actual_right,
        actual_left,
    ) == expected


def uncovered_comparisons(
    ruleset: Mapping[str, object], comparisons: Iterable[Mapping[str, object]]
) -> list[dict]:
    """The required comparisons this ruleset's assertions do not answer.

    Selector-exact, deliberately. A related assertion over neighbouring fields
    is a different test, and accepting it would let a control read as covered by
    work that did not address it.
    """

    roles = {str(role.get("name")): role for role in ruleset.get("roles") or []}
    assertions = list(ruleset.get("assertions") or [])
    join_keys = list(ruleset.get("join_keys") or [])
    return [
        dict(comparison)
        for comparison in comparisons or []
        if not any(
            assertion_covers(assertion, comparison, roles) for assertion in assertions
        )
        # A requirement that two documents reference each other is answered by
        # the join that binds them. See ``join_key_covers``: the assertion it
        # would otherwise demand is one that cannot fail.
        and not any(
            join_key_covers(join_key, comparison, roles) for join_key in join_keys
        )
    ]


def unanswerable_comparisons(workspace, comparisons: Iterable[Mapping[str, object]]) -> list[dict]:
    """Comparisons no current schema can express.

    Separate from uncovered: a rule nobody wrote is a gap in the ruleset, and a
    field nothing states is a gap in the evidence. They are repaired in
    different places, so they are reported apart.
    """

    unanswerable: list[dict] = []
    for comparison in comparisons or []:
        errors: list[str] = []
        for side in ("left", "right"):
            operand = comparison.get(side)
            if operand is None:
                continue
            _comparison_operand(workspace, operand, f"{comparison.get('key')}.{side}", errors)
        if errors:
            unanswerable.append({**dict(comparison), "reasons": errors})
    return unanswerable


def unanswerable_cycle_requirements(workspace, rcm_row: Mapping[str, object]) -> list[str]:
    """Degradation notes for one row's unanswerable schema-backed attributes.

    The stage must say these out loud. A requirement whose fields no schema
    states, or which no approved ruleset answers, is untested — and a run that
    reports the row as generated over it has reported success across a gap.
    """

    attributes = [
        attribute
        for attribute in rcm_row.get("control_attributes") or []
        if isinstance(attribute, Mapping)
        and schema_backed(attribute)
        and attribute.get("evidence_kind") == "transaction_cycle"
    ]
    if not attributes:
        return []
    rcm_id = str(rcm_row.get("id") or "")
    ruleset = cycle_rulesets.effective(workspace)
    if ruleset is None:
        return [
            f"{rcm_id} control attribute '{attribute.get('key')}' declares "
            "transaction_cycle evidence and this engagement has no approved "
            "cycle ruleset, so no cycle test could be generated for it and the "
            "requirement is untested."
            for attribute in attributes
        ]
    notes: list[str] = []
    for attribute in attributes:
        comparisons = [
            {**dict(item), "attribute_key": str(attribute.get("key") or "")}
            for item in attribute.get("required_comparisons") or []
            if isinstance(item, Mapping)
        ]
        for item in unanswerable_comparisons(workspace, comparisons):
            notes.append(
                f"{rcm_id} control attribute '{attribute.get('key')}' requires "
                f"'{item.get('key')}', which the current schemas cannot express "
                f"({'; '.join(item.get('reasons') or [])}), so that comparison "
                "is untested."
            )
        answerable = [
            item
            for item in comparisons
            if not unanswerable_comparisons(workspace, [item])
        ]
        for item in uncovered_comparisons(ruleset, answerable):
            notes.append(
                f"{rcm_id} control attribute '{attribute.get('key')}' requires "
                f"{_comparison_text(item)}, which the approved cycle rules do "
                "not assert, so that comparison is untested."
            )
    return notes


def schema_catalog(workspace) -> list[dict]:
    """The induced schemas, as the RCM authoring turn needs to see them.

    What a comparison can address: the type, what distinguishes it from its
    neighbours, how many documents carry it, and each field's name, role and
    value type. Still nothing about how many documents were *sampled* or how
    confident the induction was — those are questions about the schema rather
    than about the control.

    ``documents`` and ``discriminator`` are not decoration, and both were added
    after a treasury engagement wrote a matrix that validated perfectly and meant
    something else. Every operand named a real field on a real type, because that
    is all selector-exactness can check.

    The count is the population signal. A requirement is written against a
    population, and a type carrying one document cannot answer one. That
    engagement had coined ``local.internal_deal_confirmation`` for a single
    anomalous document — a confirmation the entity had produced for itself — and
    the authoring turn made it the deal-record side of three population-wide
    comparisons on the strength of its name, while ``treasury_deal_ticket`` and
    its eighteen documents went unnamed.

    The discriminator is the same defence one step earlier. It is the sentence
    saying a broker's confirmation is an intermediary's record and *not* the
    counterparty's — the distinction a bare field list cannot carry, and the one
    the same matrix collapsed by testing "the counterparty's confirmation has
    been received" against ``broker_confirmation``.
    """

    coined = {
        str(entry.get("id") or ""): str(entry.get("discriminator") or "")
        for entry in document_schemas.local_types(workspace)
    }

    def discriminator(type_id: str) -> str:
        definition = document_types.BY_ID.get(type_id)
        if definition is not None:
            return definition.discriminator
        return coined.get(type_id, "")

    catalog: list[dict] = []
    for schema in document_schemas.list_schemas(workspace):
        document_type = str(schema.get("document_type") or "")
        catalog.append({
            "document_type": document_type,
            "discriminator": discriminator(document_type),
            "documents": len(
                document_classification.documents_of_type(workspace, document_type)
            ),
            "fields": [
                {
                    "name": str(field.get("name") or ""),
                    "role": str(field.get("role") or ""),
                    "value_type": str(field.get("value_type") or ""),
                    "label": str(field.get("label") or ""),
                }
                for field in schema.get("fields") or []
            ],
        })
    return catalog


# --------------------------------------------------------------------------- #
# what a reader is asked to judge
# --------------------------------------------------------------------------- #
def judgment_request(workspace, test: Mapping[str, object], item_id: str) -> dict:
    """One item's pending checks, with the values each one reads.

    Built from a local evaluation, so the operands are bound exactly as the
    stored result binds them and the reader is asked about nothing else. Only
    assertions left ``not_run`` appear: a verdict already reached is not
    re-asked, and a resolution failure is not a question about agreement.

    ``raw_value`` is what travels, not the normalized form. The whole reason
    this is judged rather than computed is that presentation carries the
    difficulty — a currency prefix, a vendor code, a scanned date — and a reader
    handed the folded value would be answering an easier question than the one
    the documents pose.
    """

    output = dict(test)
    output["items"] = materialize_cycle_items(workspace, output)
    validated = validate_cycle_test(workspace, output)
    item = next(
        (
            value
            for value in output["items"]
            if str(value.get("id")) == str(item_id)
        ),
        None,
    )
    if item is None:
        raise CycleSchemaError(f"This cycle test has no item '{item_id}'.")
    evaluate_cycle_item(
        workspace, output, item, prepared=prepare(workspace, validated["ruleset"])
    )
    by_id = {
        str(assertion["id"]): assertion
        for assertion in validated["ruleset"].get("assertions") or []
    }
    roles = {
        str(role.get("name")): str(role.get("document_type") or "")
        for role in validated["ruleset"].get("roles") or []
    }
    checks = []
    for key, result in (item.get("result_by_assertion") or {}).items():
        if str(result.get("verdict")) != "not_run":
            continue
        assertion = by_id.get(str(key)) or {}
        operands = []
        for comparison in result.get("comparisons") or []:
            side = str(comparison.get("side") or "")
            operand = assertion.get(side) or {}
            role = str(operand.get("role") or "")
            entries = comparison.get("entries") or []
            first = entries[0] if entries else {}
            anchors = first.get("evidence_refs") or []
            operands.append({
                "operand": f"{role}.{operand.get('field')}",
                "document_type": roles.get(role, ""),
                "value": first.get("raw_value"),
                # The source line the value was read from. It is what lets the
                # reader notice a value that does not match its own evidence.
                "excerpt": str((anchors[0] or {}).get("excerpt") or "") if anchors else "",
            })
        checks.append({
            "check_id": str(key),
            "label": str(assertion.get("label") or ""),
            "requirement": str(
                assertion.get("requirement") or assertion.get("rationale") or ""
            ),
            "operands": operands,
        })
    return {
        "item_id": str(item_id),
        "anchor": plain_json(item.get("frozen_row") or {}),
        "documents": sorted(
            {
                str(binding.get("document_id") or "")
                for binding in item.get("role_bindings") or []
                if binding.get("document_id")
            }
        ),
        "checks": checks,
    }
