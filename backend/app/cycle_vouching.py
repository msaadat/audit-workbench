"""Domain-neutral contracts for registry-backed cycle evidence tests.

The core owns structural validation only.  Record, identifier and field
vocabulary comes from immutable, hash-identified registry packs; neither the
model nor a workspace payload may introduce new kinds at runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

import polars as pl

from .cycle_registry import DEFAULT_REGISTRY, CycleRegistry, RegistryError
from .cycle_registry.models import RegistryReference


class CycleSchemaError(ValueError):
    """A cycle-evidence payload violates the closed schema."""


class SelectionConfirmationRequired(CycleSchemaError):
    """An evidence-linked reach exceeds the item cap and needs a sample decision.

    Carried as an exception rather than an alternate return value so no caller
    can mistake the deterministic sample proposal for a persisted test.
    """

    def __init__(self, proposal: dict) -> None:
        super().__init__(str(proposal.get("reason") or "Confirm a deterministic sample."))
        self.proposal = proposal


SCHEMA_VERSION = 2
CARDINALITIES = frozenset({"one", "many"})
REUSE_RULES = frozenset({"exclusive", "allowed"})
ASSURANCE_SCOPES = frozenset({"targeted_evidence_only", "sampled_population"})
SELECTION_MODES = frozenset({"evidence_linked", "sample"})
SAMPLING_METHODS = frozenset({"random", "interval", "stratified"})
ASSERTIONS = frozenset(
    {
        "Existence",
        "Completeness",
        "Accuracy",
        "Authorization",
        "Valuation",
        "Cut-off",
        "Compliance",
        "Operational",
    }
)
OPERATORS = frozenset(
    {
        "equal_exact",
        "equal_normalized",
        "numeric_within",
        "date_on_or_before",
        "date_within",
        "present",
    }
)
ENTRY_QUANTIFIERS = frozenset({"one", "any", "all"})
ROLE_QUANTIFIERS = frozenset({"all", "any"})
NORMALIZATION_STATUSES = frozenset({"normalized", "invalid"})
EVALUATION_STATES = frozenset(
    {"not_run", "passed", "failed", "incomplete", "needs_review", "stale"}
)
CURRENT_EVALUATION_STATES = frozenset(
    {"passed", "failed", "incomplete", "needs_review"}
)
DISPOSITION_STATES = frozenset({"pending", "confirmed", "exception"})
ASSERTION_VERDICTS = frozenset(
    {
        "match",
        "mismatch",
        "missing_evidence",
        "invalid_extraction",
        "ambiguous",
        "not_run",
    }
)

MAX_GRAPH_HOPS = 6
MAX_CYCLE_RECORDS = 25
MAX_TRAVERSED_EDGES = 100
MAX_ROLES = 20
MAX_ASSERTIONS = 50
MAX_ITEMS = 500
MIN_CYCLE_RECORD_KINDS = 2

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)
# Day/month order the accepted formats above cannot see. Never used to *accept* a
# value — only to detect that a purely numeric date has two readings, so it is
# reported invalid rather than silently resolved to one of them.
_AMBIGUOUS_DATE_FORMATS = ("%m-%d-%Y", "%m/%d/%Y")
# Whitespace is deliberately *not* in the digit class: including it let a raw
# value that spans two numbers ("25 25", common when OCR emits a label column
# and a value column separately) concatenate into 2525.
_NUMBER_RE = re.compile(r"[-+]?\d(?:[\d,]*\d)?(?:\.\d+)?")


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise CycleSchemaError(f"{label} must be an object.")
    return dict(value)


def _list(value: object, label: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list):
        raise CycleSchemaError(f"{label} must be an array.")
    if nonempty and not value:
        raise CycleSchemaError(f"{label} must not be empty.")
    return list(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CycleSchemaError(f"{label} must be a non-empty string.")
    return value.strip()


def _key(value: object, label: str) -> str:
    text = _text(value, label)
    if not _KEY_RE.fullmatch(text):
        raise CycleSchemaError(f"{label} contains unsupported characters.")
    return text


def _registry_reference(
    value: object,
    registry: CycleRegistry,
    *,
    label: str = "registry",
) -> RegistryReference:
    try:
        return registry.validate_reference(value)
    except RegistryError as error:
        raise CycleSchemaError(f"{label}: {error}") from error


def _same_registry(
    value: object,
    expected: RegistryReference,
    registry: CycleRegistry,
    *,
    label: str,
) -> RegistryReference:
    actual = _registry_reference(value, registry, label=label)
    if actual != expected:
        raise CycleSchemaError(f"{label} does not match its parent registry.")
    return actual


def normalized_identifier(
    kind: str,
    value: object,
    *,
    registry_ref: object,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> str:
    reference = _registry_reference(registry_ref, registry)
    try:
        return registry.normalize_identifier(reference.pack_id, kind, value)
    except RegistryError as error:
        raise CycleSchemaError(str(error)) from error


def validate_normalized_value(value: object, *, label: str = "value") -> dict:
    envelope = _object(value, label)
    raw_value = envelope.get("raw_value")
    if raw_value is None or str(raw_value).strip() == "":
        raise CycleSchemaError(f"{label}.raw_value is required.")
    status = str(envelope.get("normalization_status") or "")
    if status not in NORMALIZATION_STATUSES:
        raise CycleSchemaError(
            f"{label}.normalization_status must be normalized or invalid."
        )
    normalized = envelope.get("value")
    error = envelope.get("normalization_error")
    if status == "normalized" and normalized is None:
        raise CycleSchemaError(f"{label}.value is required when normalized.")
    if status == "normalized" and error not in (None, ""):
        raise CycleSchemaError(
            f"{label}.normalization_error must be null when normalized."
        )
    if status == "invalid" and normalized is not None:
        raise CycleSchemaError(f"{label}.value must be null when invalid.")
    if status == "invalid" and not str(error or "").strip():
        raise CycleSchemaError(
            f"{label}.normalization_error is required when invalid."
        )
    if "citation" not in envelope or envelope.get("citation") in (None, ""):
        raise CycleSchemaError(f"{label}.citation is required.")
    return envelope


def _date_candidate(raw: str) -> str:
    """Strip only presentation whitespace around date separators."""

    return re.sub(r"\s*-\s*", "-", raw)


def _parsed_dates(candidate: str, formats: Iterable[str]) -> set[str]:
    parsed: set[str] = set()
    for date_format in formats:
        try:
            parsed.add(datetime.strptime(candidate, date_format).date().isoformat())
        except ValueError:
            continue
    return parsed


def normalize_evidence_value(
    raw_value: object,
    *,
    semantic_type: str,
    citation: object,
    identifier_kind: str | None = None,
    registry_ref: object | None = None,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Normalize one extracted value locally while retaining failed evidence.

    Workers report the verbatim value and its citation.  They do not get to
    choose the normalized transaction key used by the exact graph.
    """

    raw = str(raw_value or "").strip()
    if not raw:
        raise CycleSchemaError("An extracted raw value must not be empty.")
    normalized: object | None = None
    error: str | None = None
    try:
        if semantic_type == "identifier":
            if not identifier_kind or registry_ref is None:
                raise CycleSchemaError(
                    "Identifier normalization requires a registered kind and pack."
                )
            normalized = normalized_identifier(
                identifier_kind,
                raw,
                registry_ref=registry_ref,
                registry=registry,
            )
        elif semantic_type == "date":
            # Human-authored vouchers commonly contain whitespace around date
            # separators (for example ``29-Apr -2024``).  Removing only that
            # presentation whitespace is deterministic and does not guess a
            # missing digit or swap day/month order.
            candidate = _date_candidate(raw)
            accepted = _parsed_dates(candidate, _DATE_FORMATS)
            if not accepted:
                error = "unrecognized date format"
            elif len(accepted | _parsed_dates(candidate, _AMBIGUOUS_DATE_FORMATS)) > 1:
                # ``04-01-2024`` is 4 January or 1 April depending on the
                # record's convention, which this value does not state. Choosing
                # by format order would decide a cut-off comparison silently.
                error = "ambiguous day and month order"
            else:
                normalized = next(iter(accepted))
        elif semantic_type == "number":
            negative = raw.startswith("(") and raw.endswith(")")
            match = _NUMBER_RE.search(raw)
            if match is None:
                error = "unrecognized numeric format"
            elif _parsed_dates(_date_candidate(raw), _DATE_FORMATS):
                # ``19 Apr 2024`` yields 19 from a bare numeric scan. Reporting
                # it invalid is what lets the map validator send a date supplied
                # for an amount back for repair instead of committing a wrong
                # number that normalized cleanly.
                error = "value is a date, not a number"
            else:
                value = Decimal(match.group(0).replace(",", ""))
                if negative:
                    value = -value
                normalized = int(value) if value == value.to_integral() else float(value)
        elif semantic_type == "boolean":
            token = raw.casefold()
            if token in {"true", "yes", "y", "present", "1"}:
                normalized = True
            elif token in {"false", "no", "n", "absent", "missing", "0"}:
                normalized = False
            else:
                error = "unrecognized boolean format"
        elif semantic_type == "text":
            normalized = raw
        else:
            raise CycleSchemaError(f"Unsupported evidence semantic type '{semantic_type}'.")
    except (InvalidOperation, RegistryError, ValueError) as exc:
        error = str(exc) or f"invalid {semantic_type} value"
        normalized = None
    return {
        "raw_value": raw,
        "value": normalized,
        "normalization_status": "normalized" if normalized is not None else "invalid",
        "normalization_error": None if normalized is not None else error,
        "citation": citation,
    }


def normalize_record_fragment(
    value: object,
    *,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Replace worker-supplied normalized values with deterministic envelopes."""

    fragment = _object(value, "record fragment")
    reference = _registry_reference(fragment.get("registry"), registry)
    record_kind = _text(fragment.get("record_kind"), "record fragment.record_kind")
    registry.record_kind(reference.pack_id, record_kind)
    identifiers = []
    for index, raw in enumerate(_list(fragment.get("identifiers") or [], "record fragment.identifiers")):
        identifier = _object(raw, f"record fragment.identifiers[{index}]")
        kind = _text(identifier.get("kind"), f"record fragment.identifiers[{index}].kind")
        registry.identifier_kind(reference.pack_id, kind)
        supplied = _object(identifier.get("value"), f"record fragment.identifiers[{index}].value")
        identifiers.append(
            {
                "kind": kind,
                "value": normalize_evidence_value(
                    supplied.get("raw_value", supplied.get("value")),
                    semantic_type="identifier",
                    citation=supplied.get("citation"),
                    identifier_kind=kind,
                    registry_ref=reference.to_dict(),
                    registry=registry,
                ),
            }
        )
    fields = []
    for index, raw in enumerate(_list(fragment.get("fields") or [], "record fragment.fields")):
        fact = _object(raw, f"record fragment.fields[{index}]")
        definition, semantic_type = _field_definition(
            fact,
            f"record fragment.fields[{index}]",
            pack_id=reference.pack_id,
            registry=registry,
        )
        if definition.id not in registry.record_kind(reference.pack_id, record_kind).available_field_kinds:
            raise CycleSchemaError(
                f"record fragment.fields[{index}] is not available on record kind '{record_kind}'."
            )
        supplied = _object(fact.get("value"), f"record fragment.fields[{index}].value")
        fields.append(
            {
                "group": definition.group,
                "kind": definition.kind,
                "attribute": _text(fact.get("attribute"), f"record fragment.fields[{index}].attribute"),
                "entry": _entry_ordinal(fact, f"record fragment.fields[{index}]"),
                "value": normalize_evidence_value(
                    supplied.get("raw_value", supplied.get("value")),
                    semantic_type=semantic_type,
                    citation=supplied.get("citation"),
                    registry=registry,
                ),
            }
        )
    normalized = {**fragment, "registry": reference.to_dict(), "identifiers": identifiers, "fields": fields}
    return validate_evidence_record_fragment(normalized, registry=registry)


def _entry_ordinal(value: Mapping[str, object], label: str) -> int:
    """Which occurrence of a repeated field kind one attribute belongs to.

    A record that carries three approvals states nine facts, and the reduction
    groups facts by content — so without an ordinal, ``approver`` and ``date``
    lose the pairing the record printed and the merged record reads as three
    unrelated approvers beside three unrelated dates. The ordinal is the
    fragment-local occurrence number; it is part of a fact's merge identity, so
    attributes of one occurrence stay together and repeat facts still collapse.
    """

    supplied = value.get("entry", 0)
    if supplied is None:
        return 0
    if isinstance(supplied, bool) or not isinstance(supplied, int) or supplied < 0:
        raise CycleSchemaError(f"{label}.entry must be a non-negative integer.")
    return supplied


def _field_definition(
    value: object,
    label: str,
    *,
    pack_id: str,
    registry: CycleRegistry,
):
    selector = _object(value, label)
    group = _text(selector.get("group"), f"{label}.group")
    kind = _text(selector.get("kind"), f"{label}.kind")
    attribute = _text(selector.get("attribute"), f"{label}.attribute")
    try:
        definition = registry.field_kind(pack_id, group, kind)
    except RegistryError as error:
        raise CycleSchemaError(str(error)) from error
    attributes = {item.id: item.semantic_type for item in definition.attributes}
    if attribute not in attributes:
        raise CycleSchemaError(
            f"{label} selects unavailable field {group}.{kind}.{attribute}."
        )
    return definition, attributes[attribute]


def _validate_typed_fact(
    value: object,
    label: str,
    *,
    pack_id: str,
    record_kind: str,
    registry: CycleRegistry,
) -> dict:
    fact = _object(value, label)
    definition, _semantic_type = _field_definition(
        fact, label, pack_id=pack_id, registry=registry
    )
    try:
        record = registry.record_kind(pack_id, record_kind)
    except RegistryError as error:
        raise CycleSchemaError(str(error)) from error
    if definition.id not in record.available_field_kinds:
        raise CycleSchemaError(
            f"{label} is not available on record kind '{record_kind}'."
        )
    _entry_ordinal(fact, label)
    validate_normalized_value(fact.get("value"), label=f"{label}.value")
    return fact


def _validate_record_content(
    value: dict,
    *,
    label: str,
    reference: RegistryReference,
    registry: CycleRegistry,
) -> dict:
    record_kind = _text(value.get("record_kind"), f"{label}.record_kind")
    try:
        record_definition = registry.record_kind(reference.pack_id, record_kind)
    except RegistryError as error:
        raise CycleSchemaError(str(error)) from error

    identifiers = _list(value.get("identifiers") or [], f"{label}.identifiers")
    primary_seen: dict[str, set[str]] = {}
    for index, raw in enumerate(identifiers):
        item_label = f"{label}.identifiers[{index}]"
        identifier = _object(raw, item_label)
        kind = _text(identifier.get("kind"), f"{item_label}.kind")
        try:
            registry.identifier_kind(reference.pack_id, kind)
        except RegistryError as error:
            raise CycleSchemaError(str(error)) from error
        envelope = validate_normalized_value(
            identifier.get("value"), label=f"{item_label}.value"
        )
        if (
            kind in record_definition.primary_identifier_kinds
            and envelope["normalization_status"] == "normalized"
        ):
            primary_seen.setdefault(kind, set()).add(
                normalized_identifier(
                    kind,
                    envelope["value"],
                    registry_ref=reference.to_dict(),
                    registry=registry,
                )
            )
    if any(len(values) > 1 for values in primary_seen.values()):
        raise CycleSchemaError(
            "A record fragment cannot blend two values of the same primary "
            "identifier kind."
        )
    for index, fact in enumerate(_list(value.get("fields") or [], f"{label}.fields")):
        _validate_typed_fact(
            fact,
            f"{label}.fields[{index}]",
            pack_id=reference.pack_id,
            record_kind=record_kind,
            registry=registry,
        )
    return value


def validate_evidence_record_fragment(
    value: object,
    *,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    fragment = _object(value, "record fragment")
    reference = _registry_reference(fragment.get("registry"), registry)
    _text(fragment.get("chunk_id"), "record fragment.chunk_id")
    page_span = _list(fragment.get("page_span"), "record fragment.page_span")
    if len(page_span) != 2 or any(
        not isinstance(page, int) or isinstance(page, bool) or page < 1
        for page in page_span
    ):
        raise CycleSchemaError(
            "record fragment.page_span must contain two positive integers."
        )
    record_kind = _text(fragment.get("record_kind"), "record fragment.record_kind")
    try:
        record_definition = registry.record_kind(reference.pack_id, record_kind)
    except RegistryError as error:
        raise CycleSchemaError(str(error)) from error
    _list(
        fragment.get("classification_evidence"),
        "record fragment.classification_evidence",
        nonempty=True,
    )
    if not record_definition.bindable:
        candidates = _list(
            fragment.get("candidate_record_kinds") or [],
            "record fragment.candidate_record_kinds",
        )
        for candidate in candidates:
            try:
                candidate_definition = registry.record_kind(
                    reference.pack_id, str(candidate)
                )
            except RegistryError as error:
                raise CycleSchemaError(str(error)) from error
            if not candidate_definition.bindable:
                raise CycleSchemaError(
                    "A candidate record kind must be bindable in the selected pack."
                )
        _text(fragment.get("review_reason"), "record fragment.review_reason")
    return _validate_record_content(
        fragment,
        label="record fragment",
        reference=reference,
        registry=registry,
    )


def validate_evidence_record(
    value: object,
    *,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    record = _object(value, "record")
    reference = _registry_reference(record.get("registry"), registry)
    _key(record.get("record_id"), "record.record_id")
    _text(record.get("document_id"), "record.document_id")
    _list(
        record.get("classification_evidence"),
        "record.classification_evidence",
        nonempty=True,
    )
    return _validate_record_content(
        record, label="record", reference=reference, registry=registry
    )


def validate_evidence_reduction(
    value: object,
    *,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    reduction = _object(value, "evidence reduction")
    reference = _registry_reference(reduction.get("registry"), registry)
    for record in _list(reduction.get("records"), "evidence reduction.records"):
        entry = _object(record, "record")
        _same_registry(
            entry.get("registry"), reference, registry, label="record.registry"
        )
        validate_evidence_record(entry, registry=registry)
    for fragment in _list(
        reduction.get("unresolved_fragments"),
        "evidence reduction.unresolved_fragments",
    ):
        entry = _object(fragment, "unresolved fragment")
        if entry.get("reason") not in {"missing_identity", "ambiguous_identity"}:
            raise CycleSchemaError("An unresolved fragment has an unknown reason.")
        unresolved = _object(entry.get("fragment"), "unresolved fragment.fragment")
        _same_registry(
            unresolved.get("registry"),
            reference,
            registry,
            label="unresolved fragment.registry",
        )
        validate_evidence_record_fragment(unresolved, registry=registry)
    _list(reduction.get("conflicts"), "evidence reduction.conflicts")
    return reduction


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def fragment_identity(fragment: Mapping[str, object]) -> str:
    """Content identity used by reviewed fragment assignments."""

    return _canonical_hash(dict(fragment))


def _normalized_identifier_facts(
    value: Mapping[str, object],
    reference: RegistryReference,
    registry: CycleRegistry,
    *,
    transaction_only: bool = False,
) -> dict[str, set[str]]:
    facts: dict[str, set[str]] = {}
    for raw in value.get("identifiers") or []:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "")
        definition = registry.identifier_kind(reference.pack_id, kind)
        if transaction_only and definition.edge_policy != "transaction":
            continue
        envelope = raw.get("value") or {}
        if not isinstance(envelope, Mapping) or envelope.get("normalization_status") != "normalized":
            continue
        normalized = registry.normalize_identifier(
            reference.pack_id, kind, envelope.get("value")
        )
        facts.setdefault(kind, set()).add(normalized)
    return facts


def _primary_facts(
    fragment: Mapping[str, object],
    reference: RegistryReference,
    registry: CycleRegistry,
) -> dict[str, set[str]]:
    record = registry.record_kind(reference.pack_id, str(fragment.get("record_kind") or ""))
    facts = _normalized_identifier_facts(fragment, reference, registry)
    return {
        kind: set(facts.get(kind) or ())
        for kind in record.primary_identifier_kinds
        if facts.get(kind)
    }


def _identifier_conflict(left: Mapping[str, set[str]], right: Mapping[str, set[str]]) -> bool:
    return any(
        kind in right and values and right[kind] and values.isdisjoint(right[kind])
        for kind, values in left.items()
    )


def _specific_kinds(fragments: Iterable[Mapping[str, object]]) -> set[str]:
    return {
        str(fragment.get("record_kind") or "")
        for fragment in fragments
        if str(fragment.get("record_kind") or "") != "common.other"
    }


def _compatible_kind(fragment: Mapping[str, object], component: Mapping[str, object]) -> bool:
    candidate = str(fragment.get("record_kind") or "")
    existing = _specific_kinds(component["fragments"])
    return candidate == "common.other" or not existing or existing == {candidate}


def _dedupe_evidence(values: Iterable[object]) -> list[object]:
    output: list[object] = []
    seen: set[str] = set()
    for value in values:
        identity = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        if identity not in seen:
            seen.add(identity)
            output.append(value)
    return output


def _merge_fact_entries(
    fragments: Iterable[Mapping[str, object]], collection: str
) -> list[dict]:
    grouped: dict[str, dict] = {}
    for fragment in fragments:
        for raw in fragment.get(collection) or []:
            if not isinstance(raw, Mapping):
                continue
            entry = json.loads(json.dumps(dict(raw), default=str))
            envelope = dict(entry.get("value") or {})
            citation = envelope.pop("citation", None)
            identity = json.dumps(
                {**entry, "value": envelope},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if identity not in grouped:
                grouped[identity] = {**entry, "value": {**envelope, "citation": []}}
            citations = grouped[identity]["value"]["citation"]
            supplied = citation if isinstance(citation, list) else [citation]
            for item in supplied:
                if item not in citations:
                    citations.append(item)
    for entry in grouped.values():
        entry["value"]["citation"].sort(
            key=lambda value: json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
    return [grouped[key] for key in sorted(grouped)]


def stable_record_id(
    document_id: str,
    reference: RegistryReference,
    record_kind: str,
    primary_kind: str,
    normalized_primary: str,
) -> str:
    """Hash the completed component's exact pack and selected primary identity."""

    material = [
        str(document_id),
        reference.definition_hash,
        str(record_kind),
        str(primary_kind),
        str(normalized_primary),
    ]
    digest = hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"REC-{digest[:24].upper()}"


def reduce_record_fragments(
    document_id: str,
    fragments: Iterable[object],
    *,
    registry_ref: object | None = None,
    overrides: Iterable[object] = (),
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Reduce settled chunk fragments into document-local evidence records.

    Grouping is exact and independent of chunk order.  Secondary identifiers
    may attach a primary-less fragment only when they identify one compatible
    component; they can never bridge conflicting primary identities.

    ``registry_ref`` names the pack when there are no fragments at all: a
    document routed to this profile that turns out to carry no transaction record
    reduces to an empty result under its selected pack, which is a truthful
    answer. Requiring a fragment instead is what made the worker's response
    schema demand one, and a demanded record is a fabricated record.
    """

    normalized_fragments = [
        normalize_record_fragment(value, registry=registry) for value in fragments
    ]
    prepared = list(
        {
            fragment_identity(value): value
            for value in normalized_fragments
        }.values()
    )
    if not prepared:
        if registry_ref is None:
            raise CycleSchemaError(
                "Evidence reduction without fragments requires a registry reference."
            )
        empty = {
            "registry": _registry_reference(registry_ref, registry).to_dict(),
            "records": [],
            "unresolved_fragments": [],
            "conflicts": [],
        }
        return validate_evidence_reduction(empty, registry=registry)
    references = {
        tuple(_registry_reference(value.get("registry"), registry).to_dict().items())
        for value in prepared
    }
    if len(references) != 1:
        raise CycleSchemaError("Evidence fragments from different registry packs cannot be reduced together.")
    reference = _registry_reference(prepared[0]["registry"], registry)
    by_hash = {fragment_identity(value): value for value in prepared}
    original_hash_by_object = {id(value): identity for identity, value in by_hash.items()}
    forced_targets: dict[str, str] = {}
    applied_overrides: list[dict] = []
    for index, raw in enumerate(overrides):
        override = _object(raw, f"fragment overrides[{index}]")
        source_hash = _text(override.get("fragment_hash"), f"fragment overrides[{index}].fragment_hash")
        fragment = by_hash.get(source_hash)
        if fragment is None:
            raise CycleSchemaError("A reviewed fragment override is stale for this extraction.")
        if override.get("record_kind") is not None:
            record_kind = _text(override.get("record_kind"), f"fragment overrides[{index}].record_kind")
            registry.record_kind(reference.pack_id, record_kind)
            fragment["record_kind"] = record_kind
            validate_evidence_record_fragment(fragment, registry=registry)
        target_hash = override.get("assign_to_fragment_hash")
        if target_hash is not None:
            target_hash = _text(target_hash, f"fragment overrides[{index}].assign_to_fragment_hash")
            if target_hash not in by_hash or target_hash == source_hash:
                raise CycleSchemaError("A reviewed fragment assignment names no current target fragment.")
            forced_targets[source_hash] = target_hash
        applied_overrides.append(dict(override))

    primary: list[tuple[str, dict]] = []
    partial: list[tuple[str, dict]] = []
    for value in prepared:
        identity = original_hash_by_object[id(value)]
        (primary if _primary_facts(value, reference, registry) else partial).append((identity, value))
    primary.sort(key=lambda item: item[0])
    partial.sort(key=lambda item: item[0])
    components: list[dict] = []
    for identity, fragment in primary:
        facts = _primary_facts(fragment, reference, registry)
        matches = [
            component
            for component in components
            if not _identifier_conflict(facts, component["primary"])
            and any(
                value in component["primary"].get(kind, set())
                for kind, values in facts.items()
                for value in values
            )
        ]
        combined: dict[str, set[str]] = {kind: set(values) for kind, values in facts.items()}
        for component in matches:
            for kind, values in component["primary"].items():
                combined.setdefault(kind, set()).update(values)
        if matches and any(len(values) > 1 for values in combined.values()):
            matches = []
        if not matches:
            components.append({"fragment_hashes": {identity}, "fragments": [fragment], "primary": combined})
            continue
        target = matches[0]
        target["fragment_hashes"].add(identity)
        target["fragments"].append(fragment)
        target["primary"] = combined
        for extra in matches[1:]:
            target["fragment_hashes"].update(extra["fragment_hashes"])
            target["fragments"].extend(extra["fragments"])
            components.remove(extra)

    unresolved: list[dict] = []
    for identity, fragment in partial:
        fragment_facts = _normalized_identifier_facts(
            fragment, reference, registry, transaction_only=True
        )
        candidates = []
        forced = forced_targets.get(identity)
        for component in components:
            if forced and forced not in component["fragment_hashes"]:
                continue
            component_facts: dict[str, set[str]] = {}
            for member in component["fragments"]:
                for kind, values in _normalized_identifier_facts(
                    member, reference, registry, transaction_only=True
                ).items():
                    component_facts.setdefault(kind, set()).update(values)
            shared = any(
                values & component_facts.get(kind, set())
                for kind, values in fragment_facts.items()
            )
            if (
                (forced or shared)
                and _compatible_kind(fragment, component)
                and not _identifier_conflict(fragment_facts, component_facts)
            ):
                candidates.append(component)
        if len(candidates) == 1:
            candidates[0]["fragment_hashes"].add(identity)
            candidates[0]["fragments"].append(fragment)
        else:
            unresolved.append(
                {
                    "reason": "missing_identity" if not candidates else "ambiguous_identity",
                    "fragment": fragment,
                    "candidate_component_count": len(candidates),
                }
            )

    records: list[dict] = []
    conflicts: list[dict] = []
    for component in sorted(components, key=lambda item: sorted(item["fragment_hashes"])):
        kinds = sorted(_specific_kinds(component["fragments"]))
        if len(kinds) != 1:
            conflicts.append(
                {
                    "kind": "record_kind_conflict",
                    "record_kinds": kinds,
                    "fragment_hashes": sorted(component["fragment_hashes"]),
                    "bindable": False,
                }
            )
            continue
        record_kind = kinds[0]
        definition = registry.record_kind(reference.pack_id, record_kind)
        primary_kind = next(
            (
                kind
                for kind in definition.primary_identifier_kinds
                if len(component["primary"].get(kind, set())) == 1
            ),
            None,
        )
        if primary_kind is None:
            # This can only arise after an invalid reviewed classification.
            unresolved.extend(
                {"reason": "missing_identity", "fragment": fragment}
                for fragment in component["fragments"]
            )
            continue
        normalized_primary = next(iter(component["primary"][primary_kind]))
        record = {
            "registry": reference.to_dict(),
            "record_id": stable_record_id(
                document_id,
                reference,
                record_kind,
                primary_kind,
                normalized_primary,
            ),
            "document_id": str(document_id),
            "record_kind": record_kind,
            "classification_evidence": _dedupe_evidence(
                evidence
                for fragment in component["fragments"]
                for evidence in fragment.get("classification_evidence") or []
            ),
            "identifiers": _merge_fact_entries(component["fragments"], "identifiers"),
            "fields": _merge_fact_entries(component["fragments"], "fields"),
            "primary_identifier": {
                "kind": primary_kind,
                "normalized_value": normalized_primary,
            },
            "fragment_hashes": sorted(component["fragment_hashes"]),
            "content_hash": "",
        }
        record["content_hash"] = _canonical_hash(
            {key: value for key, value in record.items() if key != "content_hash"}
        )
        validate_evidence_record(record, registry=registry)
        records.append(record)
    result = {
        "registry": reference.to_dict(),
        "records": sorted(records, key=lambda item: item["record_id"]),
        "unresolved_fragments": sorted(
            unresolved,
            key=lambda item: fragment_identity(item["fragment"]),
        ),
        "conflicts": sorted(conflicts, key=lambda item: json.dumps(item, sort_keys=True)),
    }
    if applied_overrides:
        result["reviewed_fragment_overrides"] = applied_overrides
    validate_evidence_reduction(result, registry=registry)
    return result


def build_identifier_index(
    records: Iterable[object],
    *,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict[tuple[str, str, str], tuple[dict, ...]]:
    """Index only exact transaction identifiers under their pack hash."""

    edges: dict[tuple[str, str, str], list[dict]] = {}
    for raw in records:
        record = validate_evidence_record(raw, registry=registry)
        reference = _registry_reference(record["registry"], registry)
        if not registry.record_kind(reference.pack_id, record["record_kind"]).bindable:
            continue
        for kind, values in _normalized_identifier_facts(
            record, reference, registry, transaction_only=True
        ).items():
            for normalized in values:
                key = (reference.definition_hash, kind, normalized)
                edges.setdefault(key, []).append(record)
    return {
        key: tuple(sorted(values, key=lambda item: (item["document_id"], item["record_id"])))
        for key, values in sorted(edges.items())
    }


def _seed_keys(
    reference: RegistryReference,
    seeds: Iterable[object],
    registry: CycleRegistry,
) -> list[tuple[str, str, str]]:
    keys = []
    for index, raw in enumerate(seeds):
        seed = _object(raw, f"cycle seed[{index}]")
        kind = _text(seed.get("kind"), f"cycle seed[{index}].kind")
        definition = registry.identifier_kind(reference.pack_id, kind)
        if definition.edge_policy != "transaction":
            raise CycleSchemaError("Entity identifiers cannot seed a transaction cycle.")
        value = seed.get("normalized_value", seed.get("value"))
        normalized = registry.normalize_identifier(reference.pack_id, kind, value)
        keys.append((reference.definition_hash, kind, normalized))
    return sorted(set(keys))


def link_cycle_records(
    *,
    registry_ref: object,
    seeds: Iterable[object],
    records: Iterable[object],
    roles: Iterable[object] = (),
    registry: CycleRegistry = DEFAULT_REGISTRY,
    max_hops: int = MAX_GRAPH_HOPS,
    max_records: int = MAX_CYCLE_RECORDS,
    max_edges: int = MAX_TRAVERSED_EDGES,
) -> dict:
    """Traverse the bounded exact identifier/record graph breadth-first."""

    reference = _registry_reference(registry_ref, registry)
    record_values = [validate_evidence_record(value, registry=registry) for value in records]
    if any(_registry_reference(value["registry"], registry) != reference for value in record_values):
        raise CycleSchemaError("Cycle linkage cannot cross registry definitions.")
    index = build_identifier_index(record_values, registry=registry)
    queue = deque((key, []) for key in _seed_keys(reference, seeds, registry))
    visited_keys: set[tuple[str, str, str]] = set()
    reached: dict[str, dict] = {}
    traversed_edges = 0

    def limited(limit: str, trigger: tuple[str, str, str], hops: int) -> dict:
        return {
            "state": "needs_review",
            "review_reason": f"graph_{limit}_limit_exceeded",
            "limit": limit,
            "counts": {
                "hops": hops,
                "records": len(reached),
                "edges": traversed_edges,
            },
            "triggering_identifier": {
                "registry_definition_hash": trigger[0],
                "identifier_kind": trigger[1],
                "normalized_value": trigger[2],
            },
            "records": [],
            "role_bindings": [],
            "role_conflicts": [],
        }

    while queue:
        key, chain = queue.popleft()
        if key in visited_keys:
            continue
        visited_keys.add(key)
        next_hops = len(chain) + 1
        matches = index.get(key, ())
        for record in matches:
            traversed_edges += 1
            if traversed_edges > max_edges:
                return limited("edges", key, next_hops)
            record_id = str(record["record_id"])
            edge = {
                "registry_definition_hash": key[0],
                "identifier_kind": key[1],
                "normalized_value": key[2],
                "from_record_id": chain[-1]["to_record_id"] if chain else None,
                "to_record_id": record_id,
            }
            path = [*chain, edge]
            if len(path) > max_hops:
                return limited("hops", key, len(path))
            if record_id not in reached:
                if len(reached) + 1 > max_records:
                    return limited("records", key, len(path))
                reached[record_id] = {"record": record, "matched_by": path}
                facts = _normalized_identifier_facts(
                    record, reference, registry, transaction_only=True
                )
                for kind, values in sorted(facts.items()):
                    for normalized in sorted(values):
                        next_key = (reference.definition_hash, kind, normalized)
                        if next_key not in visited_keys:
                            queue.append((next_key, path))

    role_values = [_object(value, f"roles[{index}]") for index, value in enumerate(roles)]
    if len(role_values) > MAX_ROLES:
        raise CycleSchemaError(f"A cycle may declare at most {MAX_ROLES} roles.")
    bindings: list[dict] = []
    role_conflicts: list[dict] = []
    assigned: set[str] = set()
    for role in role_values:
        name = _key(role.get("role"), "role.role")
        record_kind = _text(role.get("record_kind"), f"role '{name}'.record_kind")
        definition = registry.record_kind(reference.pack_id, record_kind)
        if not definition.bindable:
            raise CycleSchemaError(f"Role '{name}' requires a bindable record kind.")
        cardinality = role.get("cardinality") or "one"
        if cardinality not in CARDINALITIES:
            raise CycleSchemaError(f"Role '{name}' has unsupported cardinality.")
        reuse_rule = role.get("reuse_across_items") or "exclusive"
        if reuse_rule not in REUSE_RULES:
            raise CycleSchemaError(f"Role '{name}' has unsupported reuse rule.")
        matches = [
            value
            for value in reached.values()
            if value["record"]["record_kind"] == record_kind
        ]
        if cardinality == "one" and len(matches) > 1:
            role_conflicts.append(
                {
                    "kind": "role_cardinality_conflict",
                    "role": name,
                    "record_ids": sorted(value["record"]["record_id"] for value in matches),
                    "bindable": False,
                }
            )
            continue
        for value in matches:
            record = value["record"]
            assigned.add(record["record_id"])
            bindings.append(
                {
                    "role": name,
                    "document_id": record["document_id"],
                    "record_id": record["record_id"],
                    "record_kind": record["record_kind"],
                    "record_content_hash": record.get("content_hash") or _canonical_hash(record),
                    "matched_by": value["matched_by"],
                    "reuse_across_items": reuse_rule,
                }
            )
    return {
        "state": "needs_review" if role_conflicts else "linked",
        "registry": reference.to_dict(),
        "records": [
            {**value["record"], "matched_by": value["matched_by"]}
            for _, value in sorted(reached.items())
        ],
        "role_bindings": sorted(bindings, key=lambda item: (item["role"], item["record_id"])),
        "role_conflicts": role_conflicts,
        "unassigned_records": sorted(set(reached) - assigned),
        "counts": {
            "hops": max((len(value["matched_by"]) for value in reached.values()), default=0),
            "records": len(reached),
            "edges": traversed_edges,
        },
    }


def apply_cross_item_reuse(items: Iterable[object], roles: Iterable[object]) -> list[dict]:
    """Annotate shared records according to each role's cross-item reuse rule."""

    role_rules = {}
    for role in (_object(value, "role") for value in roles):
        name = _key(role.get("role"), "role.role")
        rule = str(role.get("reuse_across_items") or "exclusive")
        if rule not in REUSE_RULES:
            raise CycleSchemaError(f"Role '{name}' has an unsupported reuse rule.")
        role_rules[name] = rule
    output = [json.loads(json.dumps(_object(value, "cycle item"), default=str)) for value in items]
    uses: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for item in output:
        for binding in item.get("role_bindings") or []:
            key = (str(binding.get("role") or ""), str(binding.get("record_id") or ""))
            uses.setdefault(key, []).append((item, binding))
    for (role, record_id), values in sorted(uses.items()):
        if len(values) < 2:
            continue
        rule = role_rules.get(role, "exclusive")
        related = sorted(str(item.get("id") or "") for item, _binding in values)
        for item, binding in values:
            fact = {
                "role": role,
                "record_id": record_id,
                "related_item_ids": [value for value in related if value != str(item.get("id") or "")],
                "reuse_across_items": rule,
                "identifier_edge": (binding.get("matched_by") or [None])[-1],
            }
            item.setdefault("shared_record_facts", []).append(fact)
            if rule == "exclusive":
                item.setdefault("collisions", []).append({**fact, "kind": "cross_item_collision"})
                item["linkage_state"] = "needs_review"
    return output


def _frame_signature(frame: pl.DataFrame) -> str:
    hashes = frame.hash_rows(seed=0).to_list() if frame.height else []
    return _canonical_hash(
        {
            "schema": [(name, str(dtype)) for name, dtype in frame.schema.items()],
            "height": frame.height,
            "row_hashes": hashes,
        }
    )


def generate_cycle_candidates(
    workspace,
    *,
    registry_ref: object,
    mappings: Iterable[object],
    records: Iterable[object],
    required_roles: Iterable[object],
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Build and rank content-free population candidates using local Polars frames."""

    reference = _registry_reference(registry_ref, registry)
    record_values = [validate_evidence_record(value, registry=registry) for value in records]
    if any(_registry_reference(value["registry"], registry) != reference for value in record_values):
        raise CycleSchemaError("Candidate generation cannot use cross-pack or stale records.")
    roles = []
    for index, raw in enumerate(required_roles):
        if isinstance(raw, str):
            record_kind = raw
            role = record_kind.rsplit(".", 1)[-1]
            value = {
                "role": role,
                "record_kind": record_kind,
                "cardinality": "many",
                "reuse_across_items": "allowed",
            }
        else:
            value = _object(raw, f"required_roles[{index}]")
        definition = registry.record_kind(
            reference.pack_id, str(value.get("record_kind") or "")
        )
        if not definition.bindable:
            raise CycleSchemaError("Candidate roles require bindable record kinds.")
        roles.append(value)
    candidates: list[dict] = []
    rejected: list[dict] = []
    record_kind_by_id = {
        str(record.get("record_id") or ""): str(record.get("record_kind") or "")
        for record in record_values
    }
    base_names = {str(item.get("name") or "") for item in workspace.tables}
    join_names = {str(item.get("name") or "") for item in workspace.joins}
    for index, raw in enumerate(mappings):
        mapping = _object(raw, f"mappings[{index}]")
        table = _text(mapping.get("table"), f"mappings[{index}].table")
        if table not in base_names | join_names:
            raise CycleSchemaError(f"Candidate table '{table}' does not exist.")
        if table in join_names and not str(mapping.get("join_justification") or "").strip():
            rejected.append({"table": table, "reason": "derived_join_requires_justification"})
            continue
        frame = workspace.get_frame(table)
        row_key = _object(mapping.get("row_key"), f"mappings[{index}].row_key")
        row_column = _text(row_key.get("column"), f"mappings[{index}].row_key.column")
        row_kind = _text(row_key.get("identifier_kind"), f"mappings[{index}].row_key.identifier_kind")
        if row_column not in frame.columns:
            rejected.append({"table": table, "reason": "row_key_missing", "row_key": row_column})
            continue
        if registry.identifier_kind(reference.pack_id, row_kind).edge_policy != "transaction":
            rejected.append({"table": table, "reason": "row_key_not_transaction", "row_key": row_column})
            continue
        null_count = int(frame.select(pl.col(row_column).null_count()).item())
        unique_count = int(frame.select(pl.col(row_column).n_unique()).item())
        if null_count or unique_count != frame.height:
            rejected.append(
                {
                    "table": table,
                    "reason": "row_key_must_be_non_null_and_unique",
                    "row_key": row_column,
                    "null_count": null_count,
                    "unique_count": unique_count,
                    "population_rows": frame.height,
                }
            )
            continue
        cycle_keys = []
        invalid_mapping = None
        seen_columns = {row_column}
        for key_index, key_raw in enumerate(_list(mapping.get("cycle_keys"), f"mappings[{index}].cycle_keys", nonempty=True)):
            key = _object(key_raw, f"mappings[{index}].cycle_keys[{key_index}]")
            column = _text(key.get("column"), f"mappings[{index}].cycle_keys[{key_index}].column")
            kind = _text(key.get("identifier_kind"), f"mappings[{index}].cycle_keys[{key_index}].identifier_kind")
            if column not in frame.columns:
                invalid_mapping = "cycle_key_missing"
                break
            if column in seen_columns:
                invalid_mapping = "duplicate_population_key_column"
                break
            seen_columns.add(column)
            if registry.identifier_kind(reference.pack_id, kind).edge_policy != "transaction":
                invalid_mapping = "cycle_key_not_transaction"
                break
            cycle_keys.append({"column": column, "identifier_kind": kind})
        if invalid_mapping:
            rejected.append({"table": table, "reason": invalid_mapping})
            continue
        row_key_position = mapping.get("row_key_position")
        row_key_position = (
            int(row_key_position)
            if isinstance(row_key_position, int) and not isinstance(row_key_position, bool)
            else frame.columns.index(row_column)
        )
        linked_rows = 0
        complete_cycles = 0
        reachable_records: set[str] = set()
        reachable_documents: set[str] = set()
        reachable_kinds: set[str] = set()
        record_rows: dict[str, set[int]] = {}
        records_per_role_per_row: dict[str, list[int]] = {
            str(role.get("role") or ""): [] for role in roles
        }
        missing_role_counts: dict[str, int] = {
            str(role.get("role") or ""): 0 for role in roles
        }
        limit_reviews = 0
        required_kinds = {str(role.get("record_kind") or "") for role in roles}
        for row_index, row in enumerate(frame.iter_rows(named=True)):
            seeds = [
                {"kind": key["identifier_kind"], "value": row.get(key["column"])}
                for key in cycle_keys
                if row.get(key["column"]) is not None and str(row.get(key["column"])).strip()
            ]
            linkage = link_cycle_records(
                registry_ref=reference.to_dict(),
                seeds=seeds,
                records=record_values,
                roles=roles,
                registry=registry,
            )
            if linkage["state"] == "needs_review" and linkage.get("limit"):
                limit_reviews += 1
                continue
            reached = linkage.get("records") or []
            if reached:
                linked_rows += 1
            row_kinds = {str(value.get("record_kind") or "") for value in reached}
            if required_kinds <= row_kinds and not linkage.get("role_conflicts"):
                complete_cycles += 1
            for value in reached:
                record_id = str(value.get("record_id") or "")
                reachable_records.add(record_id)
                reachable_documents.add(str(value.get("document_id") or ""))
                reachable_kinds.add(str(value.get("record_kind") or ""))
                record_rows.setdefault(record_id, set()).add(row_index)
            # Role coverage describes the rows a test would actually select. A
            # row that linked no evidence at all is reported once as an
            # unlinked row; counting it again under every role would read as a
            # per-role coverage failure it is not.
            if not reached:
                continue
            for role in roles:
                role_name = str(role.get("role") or "")
                record_kind = str(role.get("record_kind") or "")
                count = sum(
                    str(value.get("record_kind") or "") == record_kind
                    for value in reached
                )
                records_per_role_per_row[role_name].append(count)
                if bool(role.get("required", True)) and not count:
                    missing_role_counts[role_name] += 1
        collisions = sum(len(rows) - 1 for rows in record_rows.values() if len(rows) > 1)
        table_signature = _frame_signature(frame)
        identity_material = {
            "registry": reference.to_dict(),
            "table_signature": table_signature,
            "table": table,
            "row_key": {"column": row_column, "identifier_kind": row_kind},
            "cycle_keys": cycle_keys,
        }
        candidate_id = f"CYCLE-CAND-{_canonical_hash(identity_material).split(':', 1)[1][:20].upper()}"
        required_coverage = len(required_kinds & reachable_kinds)
        reachable_role_names = sorted(
            str(role.get("role") or "")
            for role in roles
            if str(role.get("record_kind") or "") in reachable_kinds
        )
        source_priority = 0 if table in base_names else 1
        candidate = {
            "candidate_id": candidate_id,
            "registry": reference.to_dict(),
            "table": table,
            "table_signature": table_signature,
            "source_kind": "authoritative" if source_priority == 0 else "derived_join",
            "join_justification": str(mapping.get("join_justification") or "") or None,
            "row_key": {"column": row_column, "identifier_kind": row_kind},
            "cycle_keys": cycle_keys,
            "population_rows": frame.height,
            "linked_rows": linked_rows,
            "local_coverage": {
                "linked_rows": linked_rows,
                "population_rows": frame.height,
                "ratio": linked_rows / frame.height if frame.height else 0.0,
            },
            "collision_count": collisions,
            "reachable_record_count": len(reachable_records),
            "reachable_document_count": len(reachable_documents),
            "reachable_role_count": len(reachable_role_names),
            "reachable_roles": reachable_role_names,
            "reachable_record_kinds": sorted(reachable_kinds),
            "required_role_coverage": required_coverage,
            "required_role_count": len(required_kinds),
            "complete_cycle_count": complete_cycles,
            "missing_role_counts": missing_role_counts,
            "relationship_facts": {
                str(role.get("role") or ""): {
                    "max_records_per_item": max(
                        records_per_role_per_row[str(role.get("role") or "")],
                        default=0,
                    ),
                    "max_items_per_record": max(
                        (
                            len(rows)
                            for record_id, rows in record_rows.items()
                            if record_kind_by_id.get(record_id)
                            == str(role.get("record_kind") or "")
                        ),
                        default=0,
                    ),
                }
                for role in roles
            },
            "graph_limit_review_rows": limit_reviews,
            # Section 4.1's ranking terms in order, with the arbitrary lexical
            # fallback preceded by column position: an exported ledger leads
            # with the key of its own grain, so a PO table ranks PO_NUMBER above
            # the GRN and requisition identifiers it also carries.
            "rank_tuple": [
                -required_coverage,
                source_priority,
                -complete_cycles,
                -linked_rows,
                collisions,
                row_key_position,
                table,
                row_column,
            ],
        }
        candidates.append(candidate)
    candidates.sort(key=lambda item: tuple(item["rank_tuple"]))
    # One rejection reason per (table, reason, row key): several mappings over
    # the same table otherwise repeat an identical rejection, which reads as
    # several distinct problems.
    deduplicated = {
        json.dumps(item, sort_keys=True): item for item in rejected
    }
    return {
        "registry": reference.to_dict(),
        "candidates": candidates,
        "rejected_candidates": [
            deduplicated[key] for key in sorted(deduplicated)
        ],
    }


def _column_semantic_type(dtype: object) -> str:
    normalized = str(dtype).casefold().replace(" ", "")
    if normalized.startswith(("int", "uint", "float", "decimal")):
        return "number"
    if normalized in {"date", "datetime", "time"} or normalized.startswith("datetime"):
        return "date"
    if normalized in {"bool", "boolean"}:
        return "boolean"
    return "text"


def _name_tokens(value: str) -> frozenset[str]:
    """Lower-cased alphanumeric word tokens of a column or registry name."""

    return frozenset(part for part in re.split(r"[^A-Za-z0-9]+", value.casefold()) if part)


def infer_cycle_mappings(
    workspace,
    *,
    registry_ref: object,
    records: Iterable[object],
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> list[dict]:
    """Infer exact column-to-identifier mappings without exposing row values.

    A column is mapped to the registered transaction identifier kind its locally
    normalized values overlap most strongly, breaking ties on how much evidence
    the kind reaches and then on the registry's own naming. Entity identifiers
    are never considered, and a column that stays tied on every signal is
    omitted rather than guessed.
    """

    reference = _registry_reference(registry_ref, registry)
    record_values = [validate_evidence_record(value, registry=registry) for value in records]
    identifiers: dict[str, set[str]] = {}
    # How many distinct evidence records carry each kind. Two kinds can hold the
    # same literal value — a payment voucher whose voucher number *is* the
    # invoice's internal id — so a row-count tie says nothing about which kind a
    # column really is. Record reach does: the kind that reaches more evidence
    # is the one that will actually connect the cycle.
    record_reach: dict[str, int] = {}
    for record in record_values:
        seen_kinds: set[str] = set()
        for fact in record.get("identifiers") or []:
            kind = str(fact.get("kind") or "")
            definition = registry.identifier_kind(reference.pack_id, kind)
            if definition.edge_policy != "transaction":
                continue
            envelope = fact.get("value") or {}
            if envelope.get("normalization_status") != "normalized":
                continue
            try:
                normalized = registry.normalize_identifier(
                    reference.pack_id, kind, envelope.get("value")
                )
            except RegistryError:
                continue
            identifiers.setdefault(kind, set()).add(normalized)
            seen_kinds.add(kind)
        for kind in seen_kinds:
            record_reach[kind] = record_reach.get(kind, 0) + 1

    # Identifier kinds sharing a normalizer normalize a value identically, so a
    # column's distinct values are normalized once per normalizer rather than
    # once per value per kind.
    normalizers = {
        kind: registry.identifier_kind(reference.pack_id, kind).normalizer_id
        for kind in identifiers
    }
    # Where reach also ties, the registry's own words for the kind are the last
    # non-arbitrary signal. `INVOICE_ID` shares two tokens with
    # `internal_invoice_id` and none with `payment_voucher_number`, and the same
    # comparison holds for payroll or any future pack: it reads the registered
    # id and label, never a hard-coded domain name.
    kind_tokens = {
        kind: _name_tokens(
            kind.rsplit(".", 1)[-1]
            + " "
            + registry.identifier_kind(reference.pack_id, kind).label
        )
        for kind in identifiers
    }
    # A derived join restates rows that a source table already owns, and §4.1
    # requires an authoritative population wherever one exists. Joins are
    # therefore never inferred; `generate_cycle_candidates` still accepts an
    # explicitly justified join mapping from any other producer.
    join_names = {str(item.get("name") or "") for item in workspace.joins}
    mappings: list[dict] = []
    for table in sorted(set(workspace.table_names()) - join_names):
        frame = workspace.get_frame(table)
        mapped_columns: list[dict] = []
        for position, column in enumerate(frame.columns):
            counts: dict[object, int] = {}
            for value in frame.get_column(column).drop_nulls().to_list():
                counts[value] = counts.get(value, 0) + 1
            normalized_values: dict[str, dict[object, str]] = {}
            for normalizer_id in set(normalizers.values()):
                cache = normalized_values.setdefault(normalizer_id, {})
                implementation = registry.normalizers[normalizer_id].implementation
                for value in counts:
                    try:
                        cache[value] = implementation(value)
                    except Exception:  # noqa: BLE001 - a normalizer rejects a value
                        continue
            column_tokens = _name_tokens(column)
            scores: list[tuple[int, int, int, str]] = []
            for kind, known in identifiers.items():
                cache = normalized_values.get(normalizers[kind], {})
                matched = sum(
                    counts[value]
                    for value, normalized in cache.items()
                    if normalized in known
                )
                if matched:
                    scores.append(
                        (
                            matched,
                            record_reach.get(kind, 0),
                            len(column_tokens & kind_tokens[kind]),
                            kind,
                        )
                    )
            scores.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            if not scores:
                continue
            # A kind matching the same rows, reaching the same evidence, and
            # named no more like this column than its rival is genuinely
            # ambiguous; that column is omitted rather than guessed.
            if len(scores) > 1 and scores[0][:3] == scores[1][:3]:
                continue
            mapped_columns.append(
                {
                    "column": column,
                    "position": position,
                    "identifier_kind": scores[0][3],
                    "matched_rows": scores[0][0],
                    "record_reach": scores[0][1],
                    "name_affinity": scores[0][2],
                    "semantic_type": "identifier",
                }
            )
        column_types = {
            name: _column_semantic_type(dtype) for name, dtype in frame.schema.items()
        }
        for row_key in mapped_columns:
            null_count = int(frame.select(pl.col(row_key["column"]).null_count()).item())
            unique_count = int(frame.select(pl.col(row_key["column"]).n_unique()).item())
            if null_count or unique_count != frame.height:
                continue
            cycle_keys = [
                {"column": item["column"], "identifier_kind": item["identifier_kind"]}
                for item in mapped_columns
                if item["column"] != row_key["column"]
            ]
            if not cycle_keys:
                continue
            mappings.append(
                {
                    "table": table,
                    "row_key": {
                        "column": row_key["column"],
                        "identifier_kind": row_key["identifier_kind"],
                    },
                    "row_key_position": row_key["position"],
                    "cycle_keys": cycle_keys,
                    "column_types": column_types,
                }
            )
    return mappings


def committed_pack_ids(workspace) -> list[str]:
    """Registered packs this engagement already uses, or none when undecided.

    Two things commit a workspace to a pack: an RCM row whose control attributes
    declare transaction-cycle evidence, and a voucher analysis already reduced
    under a registered pack. Which business cycle an engagement audits is a
    property of the engagement, so the extraction worker is told it rather than
    left to infer it from one chunk of one voucher. An empty list means nothing
    has committed yet, and every registered pack stays available.
    """

    from . import document_analysis

    packs = {
        str((attribute.get("registry") or {}).get("pack_id") or "")
        for row in workspace.rcm
        for attribute in row.get("control_attributes") or []
        if attribute.get("evidence_kind") == "transaction_cycle"
    }
    for document in workspace.documents:
        artifact = (
            document_analysis.load_analysis(
                workspace, str(document.get("id") or ""), document=document
            ).get("effective")
            or {}
        )
        if artifact.get("analysis_profile") == "voucher":
            packs.add(str((artifact.get("registry") or {}).get("pack_id") or ""))
    return sorted(value for value in packs if value)


def _record_manifest(record: Mapping[str, object], registry: CycleRegistry) -> dict:
    """One record's authoring surface: which registered selectors it can answer.

    ``attributes`` is each fact's own registered ``attribute`` selector, not the
    keys of its normalization envelope. Every envelope carries ``raw_value`` and
    ``value`` whatever the field kind declares, so deriving the list from the
    envelope advertised ``approvals.approval`` as answering ``value`` — which is
    not one of its attributes — while hiding ``approver``, ``decision``, and
    ``date``, which are. Both the assertion validator and the authoring dialog
    read this list, so an approval could neither be asserted nor offered.
    """

    reference = _registry_reference(record.get("registry"), registry)
    attributes: dict[tuple[str, str], set[str]] = {}
    statuses: dict[tuple[str, str], set[str]] = {}
    entries: dict[tuple[str, str], set[int]] = {}
    for fact in record.get("fields") or []:
        selector = (str(fact.get("group") or ""), str(fact.get("kind") or ""))
        envelope = fact.get("value") or {}
        attributes.setdefault(selector, set()).add(str(fact.get("attribute") or ""))
        statuses.setdefault(selector, set()).add(
            str(envelope.get("normalization_status") or "")
        )
        entries.setdefault(selector, set()).add(int(fact.get("entry") or 0))
    available_fields = [
        {
            "group": group,
            "kind": kind,
            "attributes": sorted(selected),
            "normalization_status": (
                "invalid" if "invalid" in statuses[(group, kind)] else "normalized"
            ),
            "entry_count": len(entries[(group, kind)]),
        }
        for (group, kind), selected in attributes.items()
    ]
    return {
        "document_id": str(record.get("document_id") or ""),
        "record_id": str(record.get("record_id") or ""),
        "registry": reference.to_dict(),
        "record_kind": str(record.get("record_kind") or ""),
        "allowed_transaction_identifier_kinds": sorted(
            {
                str(fact.get("kind") or "")
                for fact in record.get("identifiers") or []
                if registry.identifier_kind(
                    reference.pack_id, str(fact.get("kind") or "")
                ).edge_policy
                == "transaction"
            }
        ),
        "available_fields": sorted(
            available_fields,
            key=lambda item: (item["group"], item["kind"]),
        ),
    }


def default_roles(required_record_kinds: Iterable[object]) -> list[dict]:
    """Derive procedure-local role aliases without domain switches."""

    roles = []
    seen: set[str] = set()
    for raw_kind in required_record_kinds:
        record_kind = str(raw_kind or "")
        base = record_kind.rsplit(".", 1)[-1]
        role = base
        suffix = 2
        while role in seen:
            role = f"{base}_{suffix}"
            suffix += 1
        seen.add(role)
        roles.append(
            {
                "role": role,
                "record_kind": record_kind,
                "required": True,
                "cardinality": "one",
                "reuse_across_items": "allowed",
            }
        )
    return roles


def transaction_evidence_manifest(
    workspace,
    control_attributes: Iterable[object],
    *,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Build the complete bounded, value-free manifest used by authoring."""

    attributes = validate_control_attributes(list(control_attributes), registry=registry)
    cycle_attributes = [
        attribute
        for attribute in attributes
        if attribute.get("evidence_kind") == "transaction_cycle"
    ]
    groups: list[dict] = []
    from . import document_analysis

    by_reference: dict[str, list[dict]] = {}
    for attribute in cycle_attributes:
        key = json.dumps(attribute["registry"], sort_keys=True, separators=(",", ":"))
        by_reference.setdefault(key, []).append(attribute)
    for key in sorted(by_reference):
        grouped_attributes = by_reference[key]
        reference = _registry_reference(grouped_attributes[0]["registry"], registry)
        excluded: list[dict] = []
        records = document_analysis.registry_evidence_records(
            workspace, reference.to_dict(), excluded=excluded
        )
        required_kinds = list(
            dict.fromkeys(
                str(kind)
                for attribute in grouped_attributes
                for kind in attribute.get("required_record_kinds") or []
            )
        )
        roles = default_roles(required_kinds)
        mappings = infer_cycle_mappings(
            workspace,
            registry_ref=reference.to_dict(),
            records=records,
            registry=registry,
        )
        candidate_manifest = generate_cycle_candidates(
            workspace,
            registry_ref=reference.to_dict(),
            mappings=mappings,
            records=records,
            required_roles=roles,
            registry=registry,
        )
        candidates = candidate_manifest["candidates"]
        for candidate in candidates:
            mapping = next(
                value
                for value in mappings
                if value["table"] == candidate["table"]
                and value["row_key"] == candidate["row_key"]
                and value["cycle_keys"] == candidate["cycle_keys"]
            )
            candidate["column_types"] = mapping["column_types"]
        groups.append(
            {
                "registry": reference.to_dict(),
                "requirement_refs": [
                    str(attribute["key"]) for attribute in grouped_attributes
                ],
                "required_record_kinds": required_kinds,
                "roles": roles,
                "records": [_record_manifest(record, registry) for record in records],
                # Named, not merely absent: an analysis excluded for a stale pack
                # or stale reviewed assignments is evidence that exists and is not
                # being counted, which reads very differently from a document that
                # was never voucher-analyzed.
                "excluded_documents": sorted(
                    excluded, key=lambda item: item["document_id"]
                ),
                **candidate_manifest,
            }
        )
    manifest = {"groups": groups}
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    return manifest


def select_prevalidated_candidate(
    manifest: object,
    candidate_id: object,
    selection_reason: object,
) -> dict:
    """Accept only an exact ID from the deterministic candidate manifest."""

    value = _object(manifest, "candidate manifest")
    selected = next(
        (
            candidate
            for candidate in value.get("candidates") or []
            if isinstance(candidate, dict) and candidate.get("candidate_id") == candidate_id
        ),
        None,
    )
    if selected is None:
        raise CycleSchemaError("The selected candidate ID is not prevalidated.")
    return {**selected, "selection_reason": _text(selection_reason, "selection_reason")}


def validate_control_attributes(
    value: object,
    *,
    registry_ref: object | None = None,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> list[dict]:
    attributes = _list(value, "control_attributes", nonempty=True)
    reference = (
        _registry_reference(registry_ref, registry)
        if registry_ref is not None
        else None
    )
    keys: set[str] = set()
    normalized: list[dict] = []
    for index, raw in enumerate(attributes):
        attribute = _object(raw, f"control_attributes[{index}]")
        attribute_reference = reference
        if attribute.get("registry") is not None:
            supplied_reference = _registry_reference(
                attribute.get("registry"),
                registry,
                label=f"control_attributes[{index}].registry",
            )
            if reference is not None and supplied_reference != reference:
                raise CycleSchemaError(
                    f"control_attributes[{index}].registry does not match "
                    "the supplied registry."
                )
            attribute_reference = supplied_reference
        key = _key(attribute.get("key"), f"control_attributes[{index}].key")
        if key in keys:
            raise CycleSchemaError(f"Duplicate control attribute key '{key}'.")
        keys.add(key)
        assertion = str(attribute.get("assertion") or "")
        if assertion not in ASSERTIONS:
            raise CycleSchemaError(f"Unsupported assertion '{assertion}'.")
        _text(attribute.get("requirement"), f"control_attributes[{index}].requirement")
        evidence_kind_id = str(attribute.get("evidence_kind") or "")
        evidence_kind = registry.evidence_kinds.get(evidence_kind_id)
        if evidence_kind is None:
            raise CycleSchemaError(
                f"Unsupported evidence kind '{evidence_kind_id}'."
            )
        kinds_value = attribute.get("required_record_kinds")
        if evidence_kind.record_kind_requirement == "required":
            if attribute_reference is None:
                raise CycleSchemaError(
                    f"Evidence kind '{evidence_kind_id}' requires a registry reference."
                )
            kinds = _list(
                kinds_value,
                f"control_attributes[{index}].required_record_kinds",
                nonempty=True,
            )
            if len(set(kinds)) != len(kinds):
                raise CycleSchemaError(
                    "Control attributes require unique, bindable record kinds."
                )
            # A cycle is a relationship between records. One record kind has no
            # transaction linkage to test, so the requirement belongs to a
            # document-content or tabular strategy instead of producing a cycle
            # test whose graph can only ever reach its own seed.
            if len(kinds) < MIN_CYCLE_RECORD_KINDS:
                raise CycleSchemaError(
                    f"control_attributes[{index}] uses evidence kind "
                    f"'{evidence_kind_id}' with one record kind; a transaction "
                    f"cycle links at least {MIN_CYCLE_RECORD_KINDS} record kinds. "
                    "Use document_content or tabular_population for a "
                    "single-record requirement."
                )
            for kind in kinds:
                try:
                    definition = registry.record_kind(
                        attribute_reference.pack_id, str(kind)
                    )
                except RegistryError as error:
                    raise CycleSchemaError(str(error)) from error
                if not definition.bindable:
                    raise CycleSchemaError(
                        "Control attributes require unique, bindable record kinds."
                    )
        elif kinds_value not in (None, []):
            raise CycleSchemaError(
                f"Evidence kind '{evidence_kind_id}' does not accept record kinds."
            )
        normalized.append(attribute)
    return normalized


def assurance_scope_for(selection: Mapping[str, object]) -> str:
    mode = str(selection.get("mode") or "")
    if mode == "evidence_linked":
        return "targeted_evidence_only"
    if mode == "sample":
        return "sampled_population"
    raise CycleSchemaError(f"Unsupported selection mode '{mode}'.")


def _operand(
    value: object,
    label: str,
    *,
    roles: Mapping[str, str],
    pack_id: str,
    registry: CycleRegistry,
    table_columns: set[str] | Mapping[str, str] | None,
) -> tuple[dict, str, bool]:
    operand = _object(value, label)
    source = str(operand.get("source") or "")
    if source == "row":
        column = _text(operand.get("column"), f"{label}.column")
        if table_columns is not None and column not in table_columns:
            raise CycleSchemaError(f"{label}.column '{column}' does not exist.")
        derived_type = (
            str(table_columns.get(column) or "unknown")
            if isinstance(table_columns, Mapping)
            else "unknown"
        )
        supplied_type = str(operand.get("value_type") or "")
        if supplied_type and derived_type != "unknown" and supplied_type != derived_type:
            raise CycleSchemaError(
                f"{label}.value_type does not match the local table schema."
            )
        semantic_type = derived_type if derived_type != "unknown" else supplied_type or "unknown"
        return operand, semantic_type, False
    if source == "role":
        role = _text(operand.get("role"), f"{label}.role")
        if role not in roles:
            raise CycleSchemaError(f"{label} names unknown role '{role}'.")
        definition, semantic_type = _field_definition(
            operand.get("field"), f"{label}.field", pack_id=pack_id, registry=registry
        )
        record = registry.record_kind(pack_id, roles[role])
        if definition.id not in record.available_field_kinds:
            raise CycleSchemaError(
                f"{label}.field is unavailable on role '{role}'."
            )
        return operand, semantic_type, False
    if source == "roles":
        named_roles = _list(operand.get("roles"), f"{label}.roles", nonempty=True)
        if len(set(named_roles)) != len(named_roles) or any(
            role not in roles for role in named_roles
        ):
            raise CycleSchemaError(f"{label}.roles must name unique declared roles.")
        if operand.get("entry_quantifier") not in ENTRY_QUANTIFIERS:
            raise CycleSchemaError(f"{label}.entry_quantifier is unsupported.")
        definition, semantic_type = _field_definition(
            operand.get("field"), f"{label}.field", pack_id=pack_id, registry=registry
        )
        unavailable = [
            role
            for role in named_roles
            if definition.id
            not in registry.record_kind(pack_id, roles[role]).available_field_kinds
        ]
        if unavailable:
            raise CycleSchemaError(
                f"{label}.field is unavailable on role '{unavailable[0]}'."
            )
        return operand, semantic_type, True
    raise CycleSchemaError(f"{label}.source must be row, role, or roles.")


def validate_assertions(
    value: object,
    *,
    roles: Mapping[str, str],
    pack_id: str,
    registry: CycleRegistry = DEFAULT_REGISTRY,
    table_columns: set[str] | Mapping[str, str] | None = None,
) -> list[dict]:
    assertions = _list(value, "definition.assertions")
    if len(assertions) > MAX_ASSERTIONS:
        raise CycleSchemaError(
            f"A cycle test may have at most {MAX_ASSERTIONS} assertions."
        )
    keys: set[str] = set()
    normalized: list[dict] = []
    for index, raw in enumerate(assertions):
        assertion = _object(raw, f"definition.assertions[{index}]")
        key = _key(assertion.get("key"), f"definition.assertions[{index}].key")
        if key in keys:
            raise CycleSchemaError(f"Duplicate assertion key '{key}'.")
        keys.add(key)
        _text(assertion.get("label"), f"definition.assertions[{index}].label")
        operator = str(assertion.get("operator") or "")
        if operator not in OPERATORS:
            raise CycleSchemaError(f"Unsupported assertion operator '{operator}'.")
        left, left_type, left_set = _operand(
            assertion.get("left"),
            f"definition.assertions[{index}].left",
            roles=roles,
            pack_id=pack_id,
            registry=registry,
            table_columns=table_columns,
        )
        right_raw = assertion.get("right")
        if operator == "present":
            if right_raw is not None or left_set:
                raise CycleSchemaError(
                    "present is unary and requires one scalar operand."
                )
            normalized.append(assertion)
            continue
        if right_raw is None:
            raise CycleSchemaError(f"Assertion '{key}' requires a right operand.")
        right, right_type, right_set = _operand(
            right_raw,
            f"definition.assertions[{index}].right",
            roles=roles,
            pack_id=pack_id,
            registry=registry,
            table_columns=table_columns,
        )
        if left_set and right_set:
            raise CycleSchemaError("Set-to-set assertions are not supported.")
        if left_set or right_set:
            if assertion.get("role_quantifier") not in ROLE_QUANTIFIERS:
                raise CycleSchemaError(
                    "A generalized assertion requires role_quantifier."
                )
        elif assertion.get("role_quantifier") is not None:
            raise CycleSchemaError(
                "role_quantifier applies only to a roles operand."
            )
        expected_type = {
            "numeric_within": "number",
            "date_on_or_before": "date",
            "date_within": "date",
        }.get(operator)
        known_types = {
            item for item in (left_type, right_type) if item != "unknown"
        }
        if expected_type and known_types - {expected_type}:
            raise CycleSchemaError(
                f"Operator '{operator}' requires {expected_type} operands."
            )
        if not expected_type and len(known_types) > 1:
            raise CycleSchemaError(
                f"Assertion '{key}' compares unlike semantic types."
            )
        tolerance = assertion.get("tolerance")
        if operator == "numeric_within":
            tolerance_object = _object(tolerance, f"assertion '{key}' tolerance")
            absolute = tolerance_object.get("absolute", 0)
            percent = tolerance_object.get("percent", 0)
            if any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or item < 0
                for item in (absolute, percent)
            ):
                raise CycleSchemaError(
                    "numeric_within tolerance values must be non-negative numbers."
                )
        elif operator == "date_within":
            if (
                isinstance(tolerance, bool)
                or not isinstance(tolerance, int)
                or tolerance < 0
            ):
                raise CycleSchemaError(
                    "date_within tolerance must be a non-negative integer day count."
                )
        elif tolerance is not None:
            raise CycleSchemaError(
                f"Operator '{operator}' does not accept a tolerance."
            )
        assert left is not None and right is not None
        normalized.append(assertion)
    return normalized


def validate_cycle_definition(
    value: object,
    *,
    registry_ref: object,
    registry: CycleRegistry = DEFAULT_REGISTRY,
    table_columns: set[str] | Mapping[str, str] | None = None,
) -> dict:
    reference = _registry_reference(registry_ref, registry)
    definition = _object(value, "definition")
    population = _object(definition.get("population"), "definition.population")
    _key(population.get("candidate_id"), "definition.population.candidate_id")
    _text(population.get("selection_reason"), "definition.population.selection_reason")
    _text(population.get("table"), "definition.population.table")
    row_key = _object(population.get("row_key"), "definition.population.row_key")
    row_column = _text(row_key.get("column"), "definition.population.row_key.column")
    if table_columns is not None and row_column not in table_columns:
        raise CycleSchemaError(f"Row-key column '{row_column}' does not exist.")
    row_kind = _text(
        row_key.get("identifier_kind"),
        "definition.population.row_key.identifier_kind",
    )
    try:
        row_identifier = registry.identifier_kind(reference.pack_id, row_kind)
    except RegistryError as error:
        raise CycleSchemaError(str(error)) from error
    if row_identifier.edge_policy != "transaction":
        raise CycleSchemaError(
            "The population row key must use a transaction identifier kind."
        )
    cycle_keys = _list(
        population.get("cycle_keys"),
        "definition.population.cycle_keys",
        nonempty=True,
    )
    seen_columns = {row_column}
    for index, raw in enumerate(cycle_keys):
        key = _object(raw, f"definition.population.cycle_keys[{index}]")
        column = _text(
            key.get("column"), f"definition.population.cycle_keys[{index}].column"
        )
        if column in seen_columns:
            raise CycleSchemaError(f"Duplicate population key column '{column}'.")
        seen_columns.add(column)
        if table_columns is not None and column not in table_columns:
            raise CycleSchemaError(f"Cycle-key column '{column}' does not exist.")
        identifier_kind = _text(
            key.get("identifier_kind"),
            f"definition.population.cycle_keys[{index}].identifier_kind",
        )
        try:
            identifier_definition = registry.identifier_kind(
                reference.pack_id, identifier_kind
            )
        except RegistryError as error:
            raise CycleSchemaError(str(error)) from error
        if identifier_definition.edge_policy != "transaction":
            raise CycleSchemaError("Entity identifiers cannot be cycle keys.")
    selection = _object(
        population.get("selection"), "definition.population.selection"
    )
    derived_scope = assurance_scope_for(selection)
    supplied_scope = selection.get("assurance_scope")
    if supplied_scope is not None and supplied_scope != derived_scope:
        raise CycleSchemaError(
            "assurance_scope does not match the structural selection mode."
        )
    if selection.get("mode") == "sample":
        method = str(selection.get("method") or "")
        if method not in SAMPLING_METHODS:
            raise CycleSchemaError(f"Unsupported sampling method '{method}'.")
        size = selection.get("size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= MAX_ITEMS
        ):
            raise CycleSchemaError(
                f"Sample size must be between 1 and {MAX_ITEMS}."
            )
        if isinstance(selection.get("seed"), bool) or not isinstance(
            selection.get("seed"), int
        ):
            raise CycleSchemaError("Sample seed must be an integer.")
        stratify_by = selection.get("stratify_by")
        if method == "stratified":
            column = _text(
                stratify_by, "definition.population.selection.stratify_by"
            )
            if table_columns is not None and column not in table_columns:
                raise CycleSchemaError(
                    f"Stratification column '{column}' does not exist."
                )
        elif stratify_by not in (None, ""):
            raise CycleSchemaError(
                "stratify_by is valid only for stratified sampling."
            )
    roles = _list(definition.get("roles"), "definition.roles", nonempty=True)
    if len(roles) > MAX_ROLES:
        raise CycleSchemaError(f"A cycle test may have at most {MAX_ROLES} roles.")
    role_kinds: dict[str, str] = {}
    for index, raw in enumerate(roles):
        role = _object(raw, f"definition.roles[{index}]")
        name = _key(role.get("role"), f"definition.roles[{index}].role")
        if name in role_kinds:
            raise CycleSchemaError(f"Duplicate role '{name}'.")
        record_kind = _text(
            role.get("record_kind"), f"definition.roles[{index}].record_kind"
        )
        try:
            record_definition = registry.record_kind(
                reference.pack_id, record_kind
            )
        except RegistryError as error:
            raise CycleSchemaError(str(error)) from error
        if not record_definition.bindable:
            raise CycleSchemaError(f"Role '{name}' requires a bindable record kind.")
        role_kinds[name] = record_kind
        if not isinstance(role.get("required"), bool):
            raise CycleSchemaError(f"Role '{name}' required must be boolean.")
        if role.get("cardinality") not in CARDINALITIES:
            raise CycleSchemaError(f"Role '{name}' has unsupported cardinality.")
        if role.get("reuse_across_items") not in REUSE_RULES:
            raise CycleSchemaError(f"Role '{name}' has unsupported reuse rule.")
    validate_assertions(
        definition.get("assertions"),
        roles=role_kinds,
        pack_id=reference.pack_id,
        registry=registry,
        table_columns=table_columns,
    )
    definition["population"] = {
        **population,
        "selection": {**selection, "assurance_scope": derived_scope},
    }
    return definition


def validate_cycle_test(
    value: object,
    *,
    registry: CycleRegistry = DEFAULT_REGISTRY,
    table_columns: set[str] | Mapping[str, str] | None = None,
) -> dict:
    test = _object(value, "cycle test")
    reference = _registry_reference(test.get("registry"), registry)
    if test.get("schema_version") != SCHEMA_VERSION:
        raise CycleSchemaError(
            f"cycle_vouch schema_version must be {SCHEMA_VERSION}."
        )
    if test.get("kind") != "cycle_vouch":
        raise CycleSchemaError("The cycle test kind must be cycle_vouch.")
    rcm_id = _key(test.get("rcm_id"), "cycle test.rcm_id")
    requirement_refs = _list(
        test.get("requirement_refs"),
        "cycle test.requirement_refs",
        nonempty=True,
    )
    if len(set(requirement_refs)) != len(requirement_refs):
        raise CycleSchemaError("requirement_refs must be unique.")
    for requirement_ref in requirement_refs:
        if not isinstance(requirement_ref, str) or not requirement_ref.startswith(
            f"{rcm_id}:"
        ):
            raise CycleSchemaError(
                "Each requirement_ref must name this RCM row and an attribute key."
            )
        _key(requirement_ref.split(":", 1)[1], "requirement attribute key")
    _key(test.get("procedure_key"), "cycle test.procedure_key")
    test["registry"] = reference.to_dict()
    test["definition"] = validate_cycle_definition(
        test.get("definition"),
        registry_ref=reference.to_dict(),
        registry=registry,
        table_columns=table_columns,
    )
    if test.get("steps"):
        raise CycleSchemaError(
            "cycle_vouch tests cannot store executable checks in steps."
        )
    return test


def selection_confirmation(candidate: Mapping[str, object]) -> dict | None:
    """Return the required deterministic sample proposal for an oversized reach."""

    eligible = int(candidate.get("linked_rows") or 0)
    if eligible <= MAX_ITEMS:
        return None
    return {
        "kind": "selection_confirmation",
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "eligible_row_count": eligible,
        "maximum_items": MAX_ITEMS,
        "suggested_selection": {
            "mode": "sample",
            "method": "random",
            "size": 25,
            "seed": 42,
        },
        "reason": (
            f"{eligible} evidence-linked rows qualify; confirm a deterministic "
            f"sample of at most {MAX_ITEMS} before the test is persisted."
        ),
    }


def stable_test_semantic_id(test: Mapping[str, object]) -> str:
    """Identity derived only from the durable procedure/population tuple."""

    definition = _object(test.get("definition"), "definition")
    population = _object(definition.get("population"), "definition.population")
    row_key = _object(population.get("row_key"), "definition.population.row_key")
    material = {
        "rcm_id": str(test.get("rcm_id") or ""),
        "kind": "cycle_vouch",
        "procedure_key": str(test.get("procedure_key") or ""),
        "table": str(population.get("table") or ""),
        "row_key": {
            "column": str(row_key.get("column") or ""),
            "identifier_kind": str(row_key.get("identifier_kind") or ""),
        },
    }
    digest = hashlib.sha1(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"cycle_vouch:{digest}"


def stable_cycle_test_id(test: Mapping[str, object]) -> str:
    """Return the shared durable Document Test ID for one semantic cycle test."""

    semantic = stable_test_semantic_id(test)
    digest = hashlib.sha1(semantic.encode("utf-8")).hexdigest()
    return f"DT-{digest[:8].upper()}"


def manifest_group_for_test(test: Mapping[str, object], manifest: Mapping[str, object]) -> dict:
    reference = _registry_reference(test.get("registry"), DEFAULT_REGISTRY)
    matches = [
        group
        for group in manifest.get("groups") or []
        if isinstance(group, dict)
        and _registry_reference(group.get("registry"), DEFAULT_REGISTRY) == reference
    ]
    if len(matches) != 1:
        raise CycleSchemaError("The cycle test registry has no exact manifest group.")
    return matches[0]


def validate_cycle_test_semantics(
    value: object,
    *,
    rcm_row: Mapping[str, object],
    manifest: Mapping[str, object],
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    """Apply the local semantic quality gate after structural validation."""

    test = _object(value, "cycle test")
    if str(test.get("rcm_id") or "") != str(rcm_row.get("id") or ""):
        raise CycleSchemaError("The cycle test does not name its target RCM row.")
    group = manifest_group_for_test(test, manifest)
    definition = _object(test.get("definition"), "definition")
    population = _object(definition.get("population"), "definition.population")
    candidate = next(
        (
            item
            for item in group.get("candidates") or []
            if isinstance(item, dict)
            and item.get("candidate_id") == population.get("candidate_id")
        ),
        None,
    )
    if candidate is None:
        raise CycleSchemaError("The selected candidate ID is not prevalidated.")
    table_columns = dict(candidate.get("column_types") or {})
    validated = validate_cycle_test(
        test,
        registry=registry,
        table_columns=table_columns,
    )
    normalized_population = validated["definition"]["population"]
    for key in ("table", "row_key", "cycle_keys"):
        if normalized_population.get(key) != candidate.get(key):
            raise CycleSchemaError(
                f"definition.population.{key} does not match the selected candidate."
            )

    attributes = validate_control_attributes(
        list(rcm_row.get("control_attributes") or []), registry=registry
    )
    attributes_by_key = {str(attribute["key"]): attribute for attribute in attributes}
    referenced_attributes = []
    for requirement_ref in validated["requirement_refs"]:
        key = str(requirement_ref).split(":", 1)[1]
        attribute = attributes_by_key.get(key)
        if attribute is None:
            raise CycleSchemaError(f"Unknown RCM control attribute '{key}'.")
        if attribute.get("evidence_kind") != "transaction_cycle":
            raise CycleSchemaError(
                f"RCM control attribute '{key}' is not transaction-cycle evidence."
            )
        if _registry_reference(attribute.get("registry"), registry) != _registry_reference(
            validated.get("registry"), registry
        ):
            raise CycleSchemaError(
                f"RCM control attribute '{key}' uses a different registry pack."
            )
        referenced_attributes.append(attribute)

    required_kinds = {
        str(kind)
        for attribute in referenced_attributes
        for kind in attribute.get("required_record_kinds") or []
    }
    roles = validated["definition"]["roles"]
    if len({str(role["record_kind"]) for role in roles}) != len(roles):
        raise CycleSchemaError("A cycle procedure cannot declare duplicate record-kind roles.")
    declared_required = {
        str(role["record_kind"]) for role in roles if role.get("required") is True
    }
    missing_required = sorted(required_kinds - declared_required)
    if missing_required:
        raise CycleSchemaError(
            f"Required record kind '{missing_required[0]}' has no required role."
        )
    reachable = set(candidate.get("reachable_record_kinds") or [])
    unreachable = sorted(str(role["record_kind"]) for role in roles if role["record_kind"] not in reachable)
    if unreachable:
        raise CycleSchemaError(f"Role record kind '{unreachable[0]}' is unreachable.")

    facts_by_kind = {}
    default_by_role = {
        role["role"]: role["record_kind"] for role in group.get("roles") or []
    }
    for role_name, facts in (candidate.get("relationship_facts") or {}).items():
        record_kind = default_by_role.get(role_name)
        if record_kind:
            facts_by_kind[record_kind] = facts
    for role in roles:
        facts = facts_by_kind.get(role["record_kind"]) or {}
        if role["cardinality"] == "one" and int(facts.get("max_records_per_item") or 0) > 1:
            raise CycleSchemaError(
                f"Role '{role['role']}' is observed many times within one item."
            )
        if role["reuse_across_items"] == "exclusive" and int(facts.get("max_items_per_record") or 0) > 1:
            raise CycleSchemaError(
                f"Role '{role['role']}' is observed across several population items."
            )

    fields_by_kind: dict[str, set[tuple[str, str, str]]] = {}
    for record in group.get("records") or []:
        if not isinstance(record, dict):
            continue
        fields_by_kind.setdefault(str(record.get("record_kind") or ""), set()).update(
            (
                str(field.get("group") or ""),
                str(field.get("kind") or ""),
                str(attribute),
            )
            for field in record.get("available_fields") or []
            if isinstance(field, dict)
            for attribute in field.get("attributes") or []
        )
    role_kinds = {str(role["role"]): str(role["record_kind"]) for role in roles}
    relationship_by_role = {
        str(role["role"]): facts_by_kind.get(str(role["record_kind"])) or {}
        for role in roles
    }
    for assertion in validated["definition"]["assertions"]:
        for operand in (assertion.get("left"), assertion.get("right")):
            if not isinstance(operand, dict) or operand.get("source") not in {"role", "roles"}:
                continue
            selected_roles = (
                [str(operand.get("role") or "")]
                if operand.get("source") == "role"
                else [str(role) for role in operand.get("roles") or []]
            )
            field = operand.get("field") or {}
            selector = (
                str(field.get("group") or ""),
                str(field.get("kind") or ""),
                str(field.get("attribute") or ""),
            )
            for role_name in selected_roles:
                if selector not in fields_by_kind.get(role_kinds[role_name], set()):
                    raise CycleSchemaError(
                        f"Assertion field {selector[0]}.{selector[1]}.{selector[2]} is not present "
                        f"in supplied evidence for role '{role_name}'."
                    )
        operands = (assertion.get("left"), assertion.get("right"))
        has_row_operand = any(
            isinstance(operand, dict) and operand.get("source") == "row"
            for operand in operands
        )
        if has_row_operand:
            for operand in operands:
                if not isinstance(operand, dict) or operand.get("source") != "role":
                    continue
                role_name = str(operand.get("role") or "")
                field = operand.get("field") or {}
                if (
                    str(field.get("group") or "") == "amounts"
                    and int(
                        relationship_by_role.get(role_name, {}).get(
                            "max_items_per_record"
                        )
                        or 0
                    )
                    > 1
                ):
                    raise CycleSchemaError(
                        f"Assertion '{assertion['key']}' compares a population item "
                        f"to shared aggregate role '{role_name}' without an "
                        "allocation or aggregation rule; grouped population reducers "
                        "are not supported by this definition version."
                    )

    selected_is_join = candidate.get("source_kind") == "derived_join"
    if selected_is_join:
        selected_cycle_kinds = {
            item["identifier_kind"] for item in candidate.get("cycle_keys") or []
        }
        equivalent_source = next(
            (
                item
                for item in group.get("candidates") or []
                if item.get("source_kind") == "authoritative"
                and item.get("row_key", {}).get("identifier_kind")
                == candidate.get("row_key", {}).get("identifier_kind")
                and {entry["identifier_kind"] for entry in item.get("cycle_keys") or []}
                == selected_cycle_kinds
                and int(item.get("required_role_coverage") or 0)
                >= int(candidate.get("required_role_coverage") or 0)
            ),
            None,
        )
        if equivalent_source is not None:
            raise CycleSchemaError(
                "An equivalent authoritative source population must be used instead of a derived join."
            )
    return validated


# Fields a durable Document Test owns independently of its cycle definition.
# Regeneration replaces the definition and its derived coverage; it must not
# silently reset the record's workflow status or the auditor's own links.
_PRESERVED_ON_REGENERATION = (
    "id",
    "created",
    "created_by",
    "status",
    "procedure_refs",
    "criteria",
    "evidence_refs",
    "finding_refs",
)


def build_cycle_vouch_test(workspace, payload: Mapping[str, object]) -> dict:
    """Validate and persist one canonical cycle definition.

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
    manifest = transaction_evidence_manifest(
        workspace, rcm_row.get("control_attributes") or [], registry=DEFAULT_REGISTRY
    )
    # A definition generated against one manifest is only grounded in the
    # evidence that manifest described. If documents or tables moved between
    # proposal and commit, the selection was made against facts that no longer
    # hold, so the caller regenerates rather than committing a stale choice.
    expected_manifest = str(payload.get("context_manifest_sha256") or "").strip()
    if expected_manifest and expected_manifest != manifest["manifest_sha256"]:
        raise CycleSchemaError(
            "The transaction-evidence manifest changed after this cycle "
            "definition was generated; regenerate the test against current "
            "evidence."
        )
    test = {
        **dict(payload),
        "schema_version": SCHEMA_VERSION,
        "kind": "cycle_vouch",
        "steps": [],
        "rcm_refs": [rcm_id],
    }
    validated = validate_cycle_test_semantics(
        test, rcm_row=rcm_row, manifest=manifest
    )
    group = manifest_group_for_test(validated, manifest)
    candidate = next(
        item
        for item in group["candidates"]
        if item["candidate_id"]
        == validated["definition"]["population"]["candidate_id"]
    )
    selection = validated["definition"]["population"]["selection"]
    if selection.get("mode") == "evidence_linked":
        confirmation = selection_confirmation(candidate)
        if confirmation is not None:
            raise SelectionConfirmationRequired(
                {
                    **confirmation,
                    "definition": validated["definition"],
                    "manifest_sha256": manifest["manifest_sha256"],
                }
            )

    assurance_scope = assurance_scope_for(selection)
    selected_rows = (
        int(candidate.get("linked_rows") or 0)
        if selection.get("mode") == "evidence_linked"
        else min(int(selection.get("size") or 0), int(candidate.get("population_rows") or 0))
    )
    coverage = {
        "population_rows": int(candidate.get("population_rows") or 0),
        "selected_rows": selected_rows,
        "rows_with_evidence": (
            int(candidate.get("linked_rows") or 0)
            if selection.get("mode") == "evidence_linked"
            else None
        ),
        "complete_cycles": int(candidate.get("complete_cycle_count") or 0),
        "missing_role_counts": dict(candidate.get("missing_role_counts") or {}),
        "selection_basis": selection.get("mode"),
        "assurance_scope": assurance_scope,
    }
    semantic_id = stable_test_semantic_id(validated)
    test_id = str(payload.get("id") or "").strip() or stable_cycle_test_id(validated)
    validated.update(
        id=test_id,
        title=_text(payload.get("title"), "cycle test.title"),
        objective=_text(payload.get("objective"), "cycle test.objective"),
        semantic_id=semantic_id,
        methodology_refs=list(payload.get("methodology_refs") or []),
        agent_run_id=payload.get("agent_run_id"),
        workflow_parent_sha1=payload.get("workflow_parent_sha1"),
        context_manifest_sha256=manifest["manifest_sha256"],
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

    existing_id = test_id
    if existing_id and doc_tests.exists(workspace, existing_id):
        existing = doc_tests.load_test(workspace, existing_id)
        preserved = {
            key: existing[key]
            for key in _PRESERVED_ON_REGENERATION
            if key in existing
        }
        existing.clear()
        existing.update({**validated, **preserved})
        return doc_tests.save_test(workspace, existing)
    return doc_tests.create_test(workspace, validated)


def normalize_cycle_item(
    value: object,
    *,
    registry_ref: object,
    registry: CycleRegistry = DEFAULT_REGISTRY,
) -> dict:
    item = _object(value, "cycle item")
    _key(item.get("id"), "cycle item.id")
    reference = _registry_reference(registry_ref, registry)
    for index, raw in enumerate(
        _list(item.get("cycle_identifiers") or [], "cycle item.cycle_identifiers")
    ):
        identifier = _object(raw, f"cycle item.cycle_identifiers[{index}]")
        kind = _text(
            identifier.get("kind"),
            f"cycle item.cycle_identifiers[{index}].kind",
        )
        try:
            definition = registry.identifier_kind(reference.pack_id, kind)
        except RegistryError as error:
            raise CycleSchemaError(str(error)) from error
        if definition.edge_policy != "transaction":
            raise CycleSchemaError(
                "Cycle item identifiers must use transaction identifier kinds."
            )
        normalized_identifier(
            kind,
            identifier.get("value"),
            registry_ref=reference.to_dict(),
            registry=registry,
        )
    for binding_index, raw in enumerate(
        _list(item.get("role_bindings") or [], "cycle item.role_bindings")
    ):
        binding = _object(raw, f"cycle item.role_bindings[{binding_index}]")
        for edge_index, raw_edge in enumerate(
            _list(
                binding.get("matched_by") or [],
                f"cycle item.role_bindings[{binding_index}].matched_by",
            )
        ):
            edge = _object(
                raw_edge,
                f"cycle item.role_bindings[{binding_index}].matched_by[{edge_index}]",
            )
            kind = _text(
                edge.get("identifier_kind"),
                f"cycle item.role_bindings[{binding_index}].matched_by"
                f"[{edge_index}].identifier_kind",
            )
            try:
                definition = registry.identifier_kind(reference.pack_id, kind)
            except RegistryError as error:
                raise CycleSchemaError(str(error)) from error
            if definition.edge_policy != "transaction":
                raise CycleSchemaError(
                    "Cycle role bindings must match through transaction identifiers."
                )
            _text(
                edge.get("normalized_value"),
                f"cycle item.role_bindings[{binding_index}].matched_by"
                f"[{edge_index}].normalized_value",
            )
    evaluation = _object(
        item.get("evaluation") or {"state": "not_run"},
        "cycle item.evaluation",
    )
    if evaluation.get("state") not in EVALUATION_STATES:
        raise CycleSchemaError("Cycle item evaluation state is unsupported.")
    disposition = _object(
        item.get("disposition")
        or {
            "state": "pending",
            "evaluated_definition_sha1": None,
            "stale": False,
        },
        "cycle item.disposition",
    )
    if disposition.get("state") not in DISPOSITION_STATES:
        raise CycleSchemaError("Cycle item disposition state is unsupported.")
    if not isinstance(disposition.get("stale", False), bool):
        raise CycleSchemaError("Cycle item disposition stale must be boolean.")
    results = item.get("result_by_assertion") or {}
    if not isinstance(results, dict):
        raise CycleSchemaError("cycle item.result_by_assertion must be an object.")
    for key, result in results.items():
        _key(key, "assertion result key")
        result_object = _object(result, f"result_by_assertion.{key}")
        if result_object.get("verdict") not in ASSERTION_VERDICTS:
            raise CycleSchemaError(
                f"Assertion result '{key}' has an unsupported verdict."
            )
        if result_object.get("registry_definition_hash") != reference.definition_hash:
            raise CycleSchemaError(
                f"Assertion result '{key}' has a stale registry definition hash."
            )
    item.update(
        evaluation=evaluation,
        disposition=disposition,
        result_by_assertion=results,
    )
    item.pop("state", None)
    return item


def execution_pending(item: Mapping[str, object], *, cycle: bool) -> bool:
    if cycle:
        return str((item.get("evaluation") or {}).get("state") or "not_run") in {
            "not_run",
            "stale",
        }
    return str(item.get("state") or "pending") == "pending"


def execution_current(item: Mapping[str, object], *, cycle: bool) -> bool:
    if cycle:
        return (
            str((item.get("evaluation") or {}).get("state") or "not_run")
            in CURRENT_EVALUATION_STATES
        )
    return str(item.get("state") or "pending") in {
        "agent_checked",
        "confirmed",
        "exception",
        "manual_review",
    }


def disposition_current(item: Mapping[str, object], *, cycle: bool) -> bool:
    if cycle:
        disposition = item.get("disposition") or {}
        return (
            str(disposition.get("state") or "pending")
            in {"confirmed", "exception"}
            and not bool(disposition.get("stale"))
        )
    return str(item.get("state") or "pending") in {
        "confirmed",
        "exception",
        "manual_review",
    }


def disposition_pending(item: Mapping[str, object], *, cycle: bool) -> bool:
    return execution_current(item, cycle=cycle) and not disposition_current(
        item, cycle=cycle
    )


def metadata() -> dict:
    """Structural vocabulary plus immutable descriptors for installed packs."""

    return {
        "schema_version": SCHEMA_VERSION,
        "registry": DEFAULT_REGISTRY.metadata(),
        "cardinalities": sorted(CARDINALITIES),
        "reuse_rules": sorted(REUSE_RULES),
        "selection_modes": sorted(SELECTION_MODES),
        "sampling_methods": sorted(SAMPLING_METHODS),
        "assurance_scopes": sorted(ASSURANCE_SCOPES),
        "operators": sorted(OPERATORS),
        "entry_quantifiers": sorted(ENTRY_QUANTIFIERS),
        "role_quantifiers": sorted(ROLE_QUANTIFIERS),
        "limits": {
            "max_graph_hops": MAX_GRAPH_HOPS,
            "max_cycle_records": MAX_CYCLE_RECORDS,
            "max_traversed_edges": MAX_TRAVERSED_EDGES,
            "max_roles": MAX_ROLES,
            "max_assertions": MAX_ASSERTIONS,
            "max_items": MAX_ITEMS,
            "min_cycle_record_kinds": MIN_CYCLE_RECORD_KINDS,
        },
    }


# Compatibility aliases for Phase 0 callers.  The public nouns are now generic;
# these names can be removed once no persisted Phase 0 code imports them.
validate_record_fragment = validate_evidence_record_fragment
validate_reduced_record = validate_evidence_record
validate_reduction = validate_evidence_reduction
