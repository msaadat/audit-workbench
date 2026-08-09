"""Domain-neutral contracts for registry-backed cycle evidence tests.

The core owns structural validation only.  Record, identifier and field
vocabulary comes from immutable, hash-identified registry packs; neither the
model nor a workspace payload may introduce new kinds at runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

import polars as pl

from .cycle_registry import DEFAULT_REGISTRY, CycleRegistry, RegistryError
from .cycle_registry import operators as _operators
from .cycle_registry import recipes as _recipes
from .cycle_registry.models import RegistryReference


class CycleSchemaError(ValueError):
    """A cycle-evidence payload violates the closed schema."""

    def __init__(self, errors: str | Iterable[str]):
        values = (errors,) if isinstance(errors, str) else tuple(errors)
        normalized = tuple(str(item).strip() for item in values if str(item).strip())
        if not normalized:
            normalized = ("The payload violates the cycle schema.",)
        #: Every independent violation found, not just the first. A caller that
        #: feeds violations back to a model needs all of them: repairing one of
        #: five and being told nothing about the other four cannot converge.
        self.errors = normalized
        super().__init__("; ".join(normalized))


class SelectionConfirmationRequired(CycleSchemaError):
    """An evidence-linked reach exceeds the item cap and needs a sample decision.

    Carried as an exception rather than an alternate return value so no caller
    can mistake the deterministic sample proposal for a persisted test.
    """

    def __init__(self, proposal: dict) -> None:
        super().__init__(str(proposal.get("reason") or "Confirm a deterministic sample."))
        self.proposal = proposal


class GridStaleDefinitionError(CycleSchemaError):
    """Stored evaluated results cannot be projected against this definition."""


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
# Derived from the one operator table every prompt also renders from, so the
# gate and the instructions cannot drift apart.
OPERATORS = _operators.OPERATORS
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
MAX_GRID_PAGE_SIZE = 200
MAX_GRID_RELATED_ITEMS = 25
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


# The closed key sets of the evidence contract an RCM row authors. Placement
# carries meaning here — ``required_record_kinds`` nested inside ``registry``
# instead of beside it is a different statement, and silently ignoring the
# misplaced key produced either a confusing reference error or a row that passed
# while meaning something the author did not write. So these objects reject
# unknown keys rather than dropping them.
REGISTRY_REFERENCE_KEYS = frozenset({"pack_id", "pack_version", "definition_hash"})
FIELD_SELECTOR_KEYS = frozenset({"group", "kind", "attribute"})
OPERAND_KEYS = frozenset({"record_kind", "field"})
COMPARISON_KEYS = frozenset(
    {"key", "label", "operator", "left", "right", "tolerance"}
)
RECIPE_REFERENCE_KEYS = frozenset({"recipe_id", "bindings"})
# Validation records which recipes it expanded under this key and clears
# ``comparison_recipes``, because validation runs more than once over the life of
# a row: the worker normalizes the proposal, the executor re-validates it before
# committing, and the workspace re-validates it on load. Expanding into
# ``required_comparisons`` while leaving the recipe list in place made the second
# pass expand it again and collide with its own first expansion. The applied form
# is provenance only and is never expanded.
APPLIED_RECIPES_KEY = "comparison_recipes_applied"
CONTROL_ATTRIBUTE_KEYS = frozenset(
    {
        "key",
        "assertion",
        "requirement",
        "evidence_kind",
        "registry",
        "required_record_kinds",
        "required_comparisons",
        "comparison_recipes",
        APPLIED_RECIPES_KEY,
    }
)


def _unknown_key_error(
    value: Mapping[str, object],
    allowed: frozenset[str],
    *,
    label: str,
) -> str | None:
    """Return the message for a key outside a closed set, or ``None``."""

    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if not unknown:
        return None
    return (
        f"{label} has unexpected key '{unknown[0]}'. It accepts exactly: "
        f"{', '.join(sorted(allowed))}."
    )


def _reject_unknown_keys(
    value: Mapping[str, object],
    allowed: frozenset[str],
    *,
    label: str,
) -> None:
    """Reject keys outside a closed set, naming what the object does accept."""

    message = _unknown_key_error(value, allowed, label=label)
    if message is not None:
        raise CycleSchemaError(message)


def _known_keys_only(
    value: Mapping[str, object],
    allowed: frozenset[str],
    *,
    label: str,
    errors: list[str],
) -> dict:
    """Record an unknown key and carry on with the keys that are known.

    Stopping here would hide whatever else is wrong with the same object. The
    live failure did exactly that: a comparison carried an invented
    ``operator_tolerance`` key *and* an invented operator, and reporting only the
    key would have sent the next attempt back with the operator still wrong.
    """

    message = _unknown_key_error(value, allowed, label=label)
    if message is not None:
        errors.append(message)
    return {key: item for key, item in value.items() if str(key) in allowed}


def _collect(errors: list[str], operation) -> object:
    """Run one independent validation, recording rather than raising its errors.

    Independent here means what it says: a sibling comparison's operator does
    not depend on this one's operand, so a caller that stops at the first
    failure reports one violation out of five and cannot be repaired in one
    turn. Callers use this where the units genuinely are independent, and plain
    raising where a later check depends on an earlier one's result.
    """

    try:
        return operation()
    except CycleSchemaError as error:
        errors.extend(error.errors)
        return None


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
        raise CycleSchemaError(f"{label}: {error}") from error
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


def _separate_entry_ordinals(facts: list[dict]) -> list[dict]:
    """Give each occurrence of a repeated field kind its own ``entry`` ordinal.

    The contract asks the map worker to number occurrences so that an approver
    stays paired with the date and role printed beside it, but a worker that
    reports three party names as three facts on ordinal 0 has stated three
    occurrences under one. The manifest then advertises the selector as
    single-valued while evaluation finds three values and reports ambiguity, so
    the authoring turn cannot avoid an assertion that can only ever be
    ambiguous.

    Repair only the unambiguous case: within one ordinal of one field kind,
    exactly one attribute holds several values. Those values are separate
    occurrences and are renumbered in canonical order. Where two attributes are
    both overloaded the pairing between them is genuinely unrecoverable, so the
    facts are left alone and evaluation continues to report ambiguity rather
    than inventing an approver/date pair the record never printed.
    """

    by_selector: dict[tuple[str, str, int], dict[str, list[dict]]] = {}
    for fact in facts:
        selector = (
            str(fact.get("group") or ""),
            str(fact.get("kind") or ""),
            int(fact.get("entry") or 0),
        )
        by_selector.setdefault(selector, {}).setdefault(
            str(fact.get("attribute") or ""), []
        ).append(fact)
    for (group, kind, _ordinal), by_attribute in by_selector.items():
        overloaded = [values for values in by_attribute.values() if len(values) > 1]
        if len(overloaded) != 1:
            continue
        taken = {
            int(fact.get("entry") or 0)
            for fact in facts
            if (str(fact.get("group") or ""), str(fact.get("kind") or "")) == (group, kind)
        }
        for offset, fact in enumerate(overloaded[0]):
            if offset == 0:
                continue
            ordinal = max(taken) + 1
            taken.add(ordinal)
            fact["entry"] = ordinal
    return facts


def _merge_fact_entries(
    fragments: Iterable[Mapping[str, object]],
    collection: str,
    *,
    separate_entries: bool = False,
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
    merged = [grouped[key] for key in sorted(grouped)]
    return _separate_entry_ordinals(merged) if separate_entries else merged


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
            "fields": _merge_fact_entries(
                component["fragments"], "fields", separate_entries=True
            ),
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
                for key in [{"column": row_column, "identifier_kind": row_kind}, *cycle_keys]
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
    # The ranking exists to make the choice deterministic, so authoring has to be
    # able to see it. Without a visible rank every candidate over one evidenced
    # cycle looks identical — same linked rows, same complete cycles, same role
    # coverage — and the selection becomes a guess between a table's own key and
    # whichever foreign key it also carries.
    for position, candidate in enumerate(candidates, 1):
        candidate["rank"] = position
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
    # Distinct normalized values per exact selector. ``entry_count`` reports what
    # extraction *claimed* about multiplicity; this reports what the record
    # actually holds, and it is the count evaluation will see. Where a worker
    # numbered three party names as one occurrence the two disagree, and only
    # this one lets authoring avoid a scalar assertion that must be ambiguous.
    values: dict[tuple[str, str, str], set[str]] = {}
    for fact in record.get("fields") or []:
        selector = (str(fact.get("group") or ""), str(fact.get("kind") or ""))
        attribute = str(fact.get("attribute") or "")
        envelope = fact.get("value") or {}
        attributes.setdefault(selector, set()).add(attribute)
        statuses.setdefault(selector, set()).add(
            str(envelope.get("normalization_status") or "")
        )
        entries.setdefault(selector, set()).add(int(fact.get("entry") or 0))
        if envelope.get("normalization_status") == "normalized":
            values.setdefault((*selector, attribute), set()).add(
                json.dumps(envelope.get("value"), sort_keys=True, default=str)
            )
    available_fields = []
    for (group, kind), selected in attributes.items():
        definition = registry.field_kind(reference.pack_id, group, kind)
        available_fields.append(
            {
                "group": group,
                "kind": kind,
                "label": definition.label,
                "attributes": sorted(selected),
                "attribute_types": {
                    attribute.id: attribute.semantic_type
                    for attribute in definition.attributes
                    if attribute.id in selected
                },
                "control_evidence_attributes": sorted(
                    attribute.id
                    for attribute in definition.attributes
                    if attribute.id in selected and attribute.control_evidence
                ),
                "normalization_status": (
                    "invalid" if "invalid" in statuses[(group, kind)] else "normalized"
                ),
                "entry_count": len(entries[(group, kind)]),
                "distinct_value_counts": {
                    attribute: len(values.get((group, kind, attribute), ()))
                    for attribute in sorted(selected)
                },
            }
        )
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
                "requirements": [
                    {
                        "key": str(attribute["key"]),
                        "requirement": str(attribute["requirement"]),
                        "required_comparisons": copy.deepcopy(
                            attribute["required_comparisons"]
                        ),
                    }
                    for attribute in grouped_attributes
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


def _required_comparison_assertion(
    value: object,
    *,
    label: str,
    required_record_kinds: set[str],
    errors: list[str],
) -> dict:
    """Translate an RCM evidence contract into the canonical assertion shape.

    RCM rows do not know procedure-local role aliases or a population yet. A
    transaction-cycle attribute therefore names exact registry record kinds.
    Test generation later maps those kinds to its hydrated roles and must cover
    this exact comparison; a prose ``requirement_ref`` alone is never coverage.
    """

    comparison = _known_keys_only(
        _object(value, label), COMPARISON_KEYS, label=label, errors=errors
    )
    assertion = {
        "key": _key(comparison.get("key"), f"{label}.key"),
        "label": _text(comparison.get("label"), f"{label}.label"),
        "operator": comparison.get("operator"),
    }
    for side in ("left", "right"):
        raw_operand = comparison.get(side)
        if side == "right" and raw_operand is None:
            continue
        operand = _known_keys_only(
            _object(raw_operand, f"{label}.{side}"),
            OPERAND_KEYS,
            label=f"{label}.{side}",
            errors=errors,
        )
        record_kind = _text(
            operand.get("record_kind"), f"{label}.{side}.record_kind"
        )
        if record_kind not in required_record_kinds:
            raise CycleSchemaError(
                f"{label}.{side}.record_kind '{record_kind}' is not one of the "
                "attribute's required record kinds "
                f"({', '.join(sorted(required_record_kinds))})."
            )
        field = _known_keys_only(
            _object(operand.get("field"), f"{label}.{side}.field"),
            FIELD_SELECTOR_KEYS,
            label=f"{label}.{side}.field",
            errors=errors,
        )
        assertion[side] = {
            "source": "role",
            "role": record_kind,
            "field": field,
        }
    if comparison.get("tolerance") is not None:
        assertion["tolerance"] = comparison.get("tolerance")
    return assertion


def _expand_comparison_recipes(
    value: object,
    *,
    label: str,
    reference: RegistryReference,
    required_record_kinds: list[str],
    registry: CycleRegistry,
) -> list[dict]:
    """Expand named recipes into canonical comparisons.

    The expansion is ordinary comparison payload: it goes on to the same
    operand, type, tolerance, direction, and meaning checks a hand-authored
    comparison does. A recipe removes the *authoring* of four nested objects
    from a judgment-heavy turn; it does not remove the gate.
    """

    entries = _list(value, label)
    required = set(required_record_kinds)
    # One recipe legitimately applies twice to the same attribute — an invoice's
    # amount agreeing to its order *and* to its goods receipt is two uses of one
    # shape. The comparison keys it carries would then collide with themselves, so
    # a repeated recipe qualifies its keys with the record kinds it was bound to.
    # A single use keeps the plain key, which is what reads well downstream.
    repeated = {
        recipe_id
        for recipe_id in (
            str(entry.get("recipe_id") or "")
            for entry in entries
            if isinstance(entry, Mapping)
        )
        if sum(
            1
            for entry in entries
            if isinstance(entry, Mapping)
            and str(entry.get("recipe_id") or "") == recipe_id
        )
        > 1
    }
    errors: list[str] = []
    expanded: list[dict] = []
    for index, raw in enumerate(entries):
        entry_label = f"{label}[{index}]"

        def expand(raw=raw, entry_label=entry_label) -> list[dict]:
            entry = _object(raw, entry_label)
            _reject_unknown_keys(entry, RECIPE_REFERENCE_KEYS, label=entry_label)
            recipe_id = _text(entry.get("recipe_id"), f"{entry_label}.recipe_id")
            definition = _recipes.recipe(recipe_id)
            offered = _recipes.recipes_for_pack(reference.pack_id)
            if definition is None or definition not in offered:
                raise CycleSchemaError(
                    f"{entry_label}.recipe_id '{recipe_id}' is not a comparison "
                    f"recipe offered for pack '{reference.pack_id}'. Available "
                    f"recipes are: {', '.join(item.id for item in offered)}."
                )
            bindings = _object(entry.get("bindings"), f"{entry_label}.bindings")
            supplied = {str(key) for key in bindings}
            expected = set(definition.roles)
            if supplied != expected:
                raise CycleSchemaError(
                    f"{entry_label}.bindings must bind exactly "
                    f"{', '.join(definition.roles)} for recipe '{recipe_id}'."
                )
            bound: dict[str, str] = {}
            for role in definition.roles:
                record_kind = _text(
                    bindings.get(role), f"{entry_label}.bindings.{role}"
                )
                if record_kind not in required:
                    raise CycleSchemaError(
                        f"{entry_label}.bindings.{role} names record kind "
                        f"'{record_kind}', which is not one of the attribute's "
                        f"required record kinds "
                        f"({', '.join(sorted(required))})."
                    )
                bound[role] = record_kind
            labels = {
                role: registry.record_kind(reference.pack_id, record_kind).label
                for role, record_kind in bound.items()
            }
            suffix = (
                "_" + "_".join(
                    bound[role].rpartition(".")[2] for role in definition.roles
                )
                if recipe_id in repeated
                else ""
            )
            comparisons: list[dict] = []
            for comparison in definition.comparisons:
                payload: dict[str, object] = {
                    "key": f"{comparison.key}{suffix}",
                    "label": comparison.label.format(**labels),
                    "operator": comparison.operator,
                    "left": {
                        "record_kind": bound[comparison.left.role],
                        "field": {
                            "group": comparison.left.group,
                            "kind": comparison.left.kind,
                            "attribute": comparison.left.attribute,
                        },
                    },
                }
                if comparison.right is not None:
                    payload["right"] = {
                        "record_kind": bound[comparison.right.role],
                        "field": {
                            "group": comparison.right.group,
                            "kind": comparison.right.kind,
                            "attribute": comparison.right.attribute,
                        },
                    }
                if comparison.tolerance is not None:
                    payload["tolerance"] = copy.deepcopy(comparison.tolerance)
                comparisons.append(payload)
            return comparisons

        result = _collect(errors, expand)
        if result is not None:
            expanded.extend(result)
    if errors:
        raise CycleSchemaError(errors)
    return expanded


def _validate_required_comparisons(
    value: object,
    *,
    label: str,
    reference: RegistryReference,
    required_record_kinds: list[str],
    registry: CycleRegistry,
    recipe_count: int = 0,
) -> list[dict]:
    """Validate an attribute's comparisons, reporting every independent failure.

    Each comparison is validated on its own so that a row with five malformed
    comparisons yields five errors. Reporting only the first meant a bounded
    repair turn was told about one violation, corrected it, and failed again on
    the four it had never been shown.
    """
    comparisons = _list(value, label, nonempty=True)
    if len(comparisons) > MAX_ASSERTIONS:
        raise CycleSchemaError(
            f"A transaction-cycle attribute may require at most {MAX_ASSERTIONS} comparisons."
        )
    required = set(required_record_kinds)
    roles = {record_kind: record_kind for record_kind in required_record_kinds}
    errors: list[str] = []
    normalized: list[dict] = []
    keys: set[str] = set()
    used_record_kinds: set[str] = set()
    for index, raw in enumerate(comparisons):
        # A recipe expansion is prepended to whatever the author wrote, so the
        # reported index has to point back at the payload they can actually see.
        comparison_label = (
            f"{label}[{index}]"
            if index >= recipe_count
            else f"comparison_recipes expansion[{index}]"
        )

        # An invented key and an invented operator on the same comparison are two
        # independent defects; both are reported, so one repair turn can fix both.
        local: list[str] = []
        try:
            assertion = _required_comparison_assertion(
                raw,
                label=comparison_label,
                required_record_kinds=required,
                errors=local,
            )
            if assertion["key"] in keys:
                raise CycleSchemaError(
                    f"{comparison_label}: duplicate required comparison key "
                    f"'{assertion['key']}'."
                )
            keys.add(assertion["key"])
            validated = validate_assertions(
                [assertion],
                roles=roles,
                pack_id=reference.pack_id,
                registry=registry,
                label_prefix=comparison_label,
                index_labels=False,
            )
            for item in validated:
                _validate_assertion_meaning(
                    item,
                    role_kinds=roles,
                    required_roles=set(roles),
                    multiplicity_by_kind={},
                    pack_id=reference.pack_id,
                    registry=registry,
                )
            for side in ("left", "right"):
                operand = assertion.get(side)
                if isinstance(operand, Mapping):
                    used_record_kinds.add(str(operand.get("role")))
        except CycleSchemaError as error:
            local.extend(error.errors)
        errors.extend(local)
        if not local:
            normalized.append(_object(raw, comparison_label))
    # A declared record kind no comparison reads is a real defect, not a
    # harmless surplus: it becomes a bound role in the generated cycle test that
    # no assertion consumes, which test generation then rejects. Catching it
    # here keeps the failure in the turn that can still fix it.
    unused = sorted(required - used_record_kinds)
    if unused and not errors:
        raise CycleSchemaError(
            f"{label}: required record kind '{unused[0]}' is never read by a "
            "comparison. Either compare it against another record, or drop it "
            "from required_record_kinds."
        )
    if errors:
        raise CycleSchemaError(errors)
    return normalized


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
    errors: list[str] = []
    for index, raw in enumerate(attributes):
        # Attributes are independent requirements of one control: a malformed
        # third attribute says nothing about the first two, so all of them are
        # reported together.
        local: list[str] = []

        def validate(raw=raw, index=index, local=local) -> dict:
            return _validate_control_attribute(
                raw,
                label=f"control_attributes[{index}]",
                reference=reference,
                keys=keys,
                registry=registry,
                errors=local,
            )

        attribute = _collect(local, validate)
        errors.extend(local)
        if attribute is not None and not local:
            normalized.append(attribute)
    if errors:
        raise CycleSchemaError(errors)
    return normalized


def _validate_control_attribute(
    raw: object,
    *,
    label: str,
    reference: RegistryReference | None,
    keys: set[str],
    registry: CycleRegistry,
    errors: list[str],
) -> dict:
    """Validate one control attribute of an asserted control."""

    # An unknown key here is recorded and the rest of the attribute is still
    # validated: a misspelled ``required_comparisons`` should report both the
    # unknown key and the contract it therefore fails to supply.
    attribute = _known_keys_only(
        _object(raw, label), CONTROL_ATTRIBUTE_KEYS, label=label, errors=errors
    )
    attribute_reference = reference
    if attribute.get("registry") is not None:
        registry_label = f"{label}.registry"
        supplied = _object(attribute.get("registry"), registry_label)
        # Checked before the reference itself, because the misplacement is the
        # cause: a ``required_record_kinds`` nested here is also missing from
        # where it belongs, and reporting a stale-reference error instead sent
        # the author looking at the pack version.
        unknown = sorted(
            str(key) for key in supplied if str(key) not in REGISTRY_REFERENCE_KEYS
        )
        if unknown:
            raise CycleSchemaError(
                f"{registry_label} has unexpected key '{unknown[0]}'. A registry "
                "reference has exactly pack_id, pack_version, and definition_hash, "
                "copied from one installed pack; required_record_kinds, "
                "required_comparisons, and comparison_recipes are siblings of "
                "registry on the control attribute, never keys inside it."
            )
        supplied_reference = _registry_reference(
            supplied,
            registry,
            label=registry_label,
        )
        if reference is not None and supplied_reference != reference:
            raise CycleSchemaError(
                f"{registry_label} does not match the supplied registry."
            )
        attribute_reference = supplied_reference
    key = _key(attribute.get("key"), f"{label}.key")
    if key in keys:
        raise CycleSchemaError(f"Duplicate control attribute key '{key}'.")
    keys.add(key)
    assertion = str(attribute.get("assertion") or "")
    if assertion not in ASSERTIONS:
        raise CycleSchemaError(
            f"{label}.assertion '{assertion}' is not supported. It must be "
            f"exactly one of: {', '.join(sorted(ASSERTIONS))}."
        )
    _text(attribute.get("requirement"), f"{label}.requirement")
    evidence_kind_id = str(attribute.get("evidence_kind") or "")
    evidence_kind = registry.evidence_kinds.get(evidence_kind_id)
    if evidence_kind is None:
        raise CycleSchemaError(
            f"{label}.evidence_kind '{evidence_kind_id}' is not supported. It "
            f"must be exactly one of: "
            f"{', '.join(sorted(registry.evidence_kinds))}."
        )
    kinds_value = attribute.get("required_record_kinds")
    comparisons_value = attribute.get("required_comparisons")
    recipes_value = attribute.get("comparison_recipes")
    if evidence_kind.record_kind_requirement == "required":
        # Nothing at all, rather than something malformed. Reporting the missing
        # registry first sent the author to fix one field of a contract that had
        # not been written, so the absence is named as the absence it is.
        if (
            attribute_reference is None
            and not kinds_value
            and not comparisons_value
            and not recipes_value
        ):
            raise CycleSchemaError(
                f"{label} declares evidence kind '{evidence_kind_id}' but names "
                "no evidence contract. Supply registry, required_record_kinds, "
                "and comparison_recipes or required_comparisons."
            )
        if attribute_reference is None:
            raise CycleSchemaError(
                f"Evidence kind '{evidence_kind_id}' requires a registry reference."
            )
        kinds = _list(
            kinds_value,
            f"{label}.required_record_kinds",
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
                f"{label} uses evidence kind "
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
        record_kinds = [str(kind) for kind in kinds]
        authored_recipes = _list(
            recipes_value if recipes_value is not None else [],
            f"{label}.comparison_recipes",
        )
        expanded = _expand_comparison_recipes(
            authored_recipes,
            label=f"{label}.comparison_recipes",
            reference=attribute_reference,
            required_record_kinds=record_kinds,
            registry=registry,
        )
        supplied_comparisons = (
            _list(comparisons_value, f"{label}.required_comparisons")
            if comparisons_value is not None
            else []
        )
        if not expanded and not supplied_comparisons:
            raise CycleSchemaError(
                f"{label} declares evidence kind '{evidence_kind_id}' but names "
                "no evidence contract. Supply comparison_recipes, or "
                "required_comparisons, or both."
            )
        attribute["required_comparisons"] = _validate_required_comparisons(
            [*expanded, *supplied_comparisons],
            label=f"{label}.required_comparisons",
            reference=attribute_reference,
            required_record_kinds=record_kinds,
            registry=registry,
            recipe_count=len(expanded),
        )
        # The expansion now lives in required_comparisons, so the recipe list is
        # retired to its applied form. Validating this attribute again is then a
        # no-op rather than a second expansion.
        if authored_recipes:
            attribute[APPLIED_RECIPES_KEY] = [
                *_list(
                    attribute.get(APPLIED_RECIPES_KEY) or [],
                    f"{label}.{APPLIED_RECIPES_KEY}",
                ),
                *authored_recipes,
            ]
        attribute.pop("comparison_recipes", None)
    elif kinds_value not in (None, []):
        raise CycleSchemaError(
            f"Evidence kind '{evidence_kind_id}' does not accept record kinds."
        )
    elif comparisons_value not in (None, []):
        raise CycleSchemaError(
            f"Evidence kind '{evidence_kind_id}' does not accept required comparisons."
        )
    elif recipes_value not in (None, []) or attribute.get(APPLIED_RECIPES_KEY) not in (
        None,
        [],
    ):
        raise CycleSchemaError(
            f"Evidence kind '{evidence_kind_id}' does not accept comparison recipes."
        )
    return attribute


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
    label_prefix: str = "definition.assertions",
    index_labels: bool = True,
) -> list[dict]:
    """Validate assertion shape against the shared operator table.

    ``label_prefix`` lets a caller keep its own path in the error text. An RCM
    control attribute's comparisons are validated through here, and reporting
    them as ``definition.assertions[0]`` pointed the reader at a structure that
    does not exist in the payload they wrote. A caller validating one assertion
    whose path it already knows passes ``index_labels=False`` so the path is not
    given a second, meaningless index.
    """
    assertions = _list(value, label_prefix)
    if len(assertions) > MAX_ASSERTIONS:
        raise CycleSchemaError(
            f"A cycle test may have at most {MAX_ASSERTIONS} assertions."
        )
    keys: set[str] = set()
    normalized: list[dict] = []
    for index, raw in enumerate(assertions):
        label = f"{label_prefix}[{index}]" if index_labels else label_prefix
        assertion = _object(raw, label)
        key = _key(assertion.get("key"), f"{label}.key")
        if key in keys:
            raise CycleSchemaError(f"Duplicate assertion key '{key}'.")
        keys.add(key)
        _text(assertion.get("label"), f"{label}.label")
        operator = str(assertion.get("operator") or "")
        definition = _operators.operator(operator)
        if definition is None:
            raise CycleSchemaError(
                _operators.unsupported_operator_message(operator, label=label)
            )
        left, left_type, left_set = _operand(
            assertion.get("left"),
            f"{label}.left",
            roles=roles,
            pack_id=pack_id,
            registry=registry,
            table_columns=table_columns,
        )
        right_raw = assertion.get("right")
        if definition.arity == "unary":
            if right_raw is not None or left_set:
                raise CycleSchemaError(
                    f"{label}: {operator} is unary and requires one scalar operand."
                )
            normalized.append(assertion)
            continue
        if right_raw is None:
            raise CycleSchemaError(
                f"Assertion '{key}' requires a right operand."
            )
        right, right_type, right_set = _operand(
            right_raw,
            f"{label}.right",
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
        expected_type = definition.operand_type
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
        _validate_tolerance(
            assertion.get("tolerance"),
            definition=definition,
            key=key,
            label=label,
        )
        assert left is not None and right is not None
        normalized.append(assertion)
    return normalized


def _validate_tolerance(
    tolerance: object,
    *,
    definition: _operators.OperatorDefinition,
    key: str,
    label: str,
) -> None:
    """Apply the tolerance rule the operator table declares for this operator."""

    if definition.tolerance == "numeric_object":
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
                f"{label}: {definition.id} tolerance values must be non-negative "
                'numbers, as {"absolute": <number>, "percent": <number>}.'
            )
        return
    if definition.tolerance == "integer_days":
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, int)
            or tolerance < 0
        ):
            raise CycleSchemaError(
                f"{label}: {definition.id} tolerance must be a non-negative "
                "integer day count, not an object."
            )
        return
    if tolerance is not None:
        raise CycleSchemaError(
            f"{label}: operator '{definition.id}' does not accept a tolerance."
        )


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


_SYMMETRIC_OPERATORS = frozenset(
    {"equal_exact", "equal_normalized", "numeric_within", "date_within"}
)


def _comparison_operand_signature(
    operand: object,
    *,
    role_kinds: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str]:
    value = _object(operand, "comparison operand")
    if role_kinds is None:
        record_kind = str(value.get("record_kind") or "")
    else:
        if value.get("source") != "role":
            return ("", "", "", "")
        record_kind = str(role_kinds.get(str(value.get("role") or "")) or "")
    field = value.get("field") or {}
    return (
        record_kind,
        str(field.get("group") or ""),
        str(field.get("kind") or ""),
        str(field.get("attribute") or ""),
    )


def _assertion_covers_required_comparison(
    assertion: Mapping[str, object],
    comparison: Mapping[str, object],
    *,
    role_kinds: Mapping[str, str],
) -> bool:
    operator = str(comparison.get("operator") or "")
    if str(assertion.get("operator") or "") != operator:
        return False
    if _plain_json(assertion.get("tolerance")) != _plain_json(
        comparison.get("tolerance")
    ):
        return False
    expected_left = _comparison_operand_signature(comparison.get("left"))
    actual_left = _comparison_operand_signature(
        assertion.get("left"), role_kinds=role_kinds
    )
    expected_right = (
        _comparison_operand_signature(comparison.get("right"))
        if comparison.get("right") is not None
        else None
    )
    actual_right = (
        _comparison_operand_signature(assertion.get("right"), role_kinds=role_kinds)
        if assertion.get("right") is not None
        else None
    )
    if actual_left == expected_left and actual_right == expected_right:
        return True
    return bool(
        operator in _SYMMETRIC_OPERATORS
        and expected_right is not None
        and actual_left == expected_right
        and actual_right == expected_left
    )


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
    # Validate the closed canonical shape before looking up its candidate.  A
    # missing or misplaced candidate_id must produce the exact structural path,
    # not the misleading claim that an absent value was a non-prevalidated ID.
    structurally_validated = validate_cycle_test(
        test,
        registry=registry,
        table_columns=None,
    )
    group = manifest_group_for_test(structurally_validated, manifest)
    definition = _object(
        structurally_validated.get("definition"), "definition"
    )
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
        structurally_validated,
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
    # Worst observed multiplicity per exact selector, across every record of one
    # kind. A scalar operand resolves only when one value answers it, so a
    # selector that already holds two can produce nothing but ``ambiguous``.
    multiplicity_by_kind: dict[str, dict[tuple[str, str, str], int]] = {}
    for record in group.get("records") or []:
        if not isinstance(record, dict):
            continue
        record_kind = str(record.get("record_kind") or "")
        fields_by_kind.setdefault(record_kind, set()).update(
            (
                str(field.get("group") or ""),
                str(field.get("kind") or ""),
                str(attribute),
            )
            for field in record.get("available_fields") or []
            if isinstance(field, dict)
            for attribute in field.get("attributes") or []
        )
        observed = multiplicity_by_kind.setdefault(record_kind, {})
        for field in record.get("available_fields") or []:
            if not isinstance(field, dict):
                continue
            counts = field.get("distinct_value_counts")
            for attribute, count in (counts or {}).items():
                selector = (
                    str(field.get("group") or ""),
                    str(field.get("kind") or ""),
                    str(attribute),
                )
                observed[selector] = max(observed.get(selector, 0), int(count or 0))
    role_kinds = {str(role["role"]): str(role["record_kind"]) for role in roles}
    required_roles = {
        str(role["role"]) for role in roles if role.get("required") is True
    }
    relationship_by_role = {
        str(role["role"]): facts_by_kind.get(str(role["record_kind"])) or {}
        for role in roles
    }
    asserted_roles: set[str] = set()
    for assertion in validated["definition"]["assertions"]:
        for operand in (assertion.get("left"), assertion.get("right")):
            if isinstance(operand, Mapping) and operand.get("source") == "role":
                asserted_roles.add(str(operand.get("role") or ""))
            elif isinstance(operand, Mapping) and operand.get("source") == "roles":
                asserted_roles.update(str(role) for role in operand.get("roles") or [])
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
        # Meaning is judged only after the assertion is known to name a field the
        # evidence actually supplies: "that selector is absent" is the more
        # specific and more actionable repair than "that selector proves nothing".
        _validate_assertion_meaning(
            assertion,
            role_kinds=role_kinds,
            required_roles=required_roles,
            multiplicity_by_kind=multiplicity_by_kind,
            pack_id=_registry_reference(validated["registry"], registry).pack_id,
            registry=registry,
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

    assertions = list(validated["definition"]["assertions"])
    for attribute in referenced_attributes:
        for comparison in attribute.get("required_comparisons") or []:
            if any(
                _assertion_covers_required_comparison(
                    assertion,
                    comparison,
                    role_kinds=role_kinds,
                )
                for assertion in assertions
            ):
                continue
            raise CycleSchemaError(
                f"RCM control attribute '{attribute['key']}' requires comparison "
                f"'{comparison['key']}', but no generated assertion covers its "
                "exact record kinds, field selectors, operator, and tolerance."
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

    unasserted = sorted(set(role_kinds) - asserted_roles)
    if unasserted:
        raise CycleSchemaError(
            f"Role '{unasserted[0]}' is declared but no assertion reads it; a role "
            "the procedure never tests is coverage, not evidence."
        )

    # Two candidates over the same table are the same rows keyed differently, so
    # there is no lifecycle argument for the lower-ranked one — a purchase-order
    # export keyed on its goods-receipt column is a grain error, not a choice.
    # Candidates over *different* tables are genuinely different populations and
    # the ranking stays advisory there.
    selected_rank = int(candidate.get("rank") or 0)
    better_same_table = next(
        (
            item
            for item in group.get("candidates") or []
            if selected_rank > 0
            and item.get("table") == candidate.get("table")
            and 0 < int(item.get("rank") or 0) < selected_rank
            and int(item.get("required_role_coverage") or 0)
            >= int(candidate.get("required_role_coverage") or 0)
        ),
        None,
    )
    if better_same_table is not None:
        raise CycleSchemaError(
            f"Candidate '{better_same_table['candidate_id']}' keys the same "
            f"'{candidate.get('table')}' population on "
            f"'{better_same_table.get('row_key', {}).get('column')}' and ranks "
            f"above the selected '{candidate.get('row_key', {}).get('column')}'; "
            "select the higher-ranked candidate or a different table."
        )
    return validated


def _assigned_assertion_key(
    assertion: Mapping[str, object],
    *,
    existing_keys: set[str],
) -> str:
    """Return a structural immutable key for a newly authored assertion."""

    supplied = assertion.get("key")
    if supplied not in (None, ""):
        return _key(supplied, "assertion.key")
    base = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(assertion.get("label") or assertion.get("operator") or "assertion")
        .strip()
        .lower(),
    ).strip("_-") or "assertion"
    if not base[0].isalnum():
        base = f"assertion_{base}"
    candidate = base
    suffix = 2
    while candidate in existing_keys:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _validate_assertion_mutation_shape(
    value: Mapping[str, object], *, label: str
) -> None:
    """Keep the incremental write contract narrow before semantic validation."""

    unknown = set(value) - {
        "key",
        "label",
        "left",
        "right",
        "operator",
        "tolerance",
        "role_quantifier",
    }
    if unknown:
        raise CycleSchemaError(
            f"{label} contains unsupported field '{sorted(unknown)[0]}'."
        )
    for side in ("left", "right"):
        if value.get(side) is None:
            continue
        operand = _object(value.get(side), f"{label}.{side}")
        source = str(operand.get("source") or "")
        allowed = {
            "row": {"source", "column", "value_type"},
            "role": {"source", "role", "field"},
            "roles": {"source", "roles", "field", "entry_quantifier"},
        }.get(source, {"source"})
        operand_unknown = set(operand) - allowed
        if operand_unknown:
            raise CycleSchemaError(
                f"{label}.{side} contains unsupported field "
                f"'{sorted(operand_unknown)[0]}'."
            )
        if source in {"role", "roles"} and operand.get("field") is not None:
            field = _object(operand.get("field"), f"{label}.{side}.field")
            field_unknown = set(field) - {"group", "kind", "attribute"}
            if field_unknown:
                raise CycleSchemaError(
                    f"{label}.{side}.field contains unsupported field "
                    f"'{sorted(field_unknown)[0]}'."
                )
    tolerance = value.get("tolerance")
    if isinstance(tolerance, Mapping):
        tolerance_unknown = set(tolerance) - {"absolute", "percent"}
        if tolerance_unknown:
            raise CycleSchemaError(
                f"{label}.tolerance contains unsupported field "
                f"'{sorted(tolerance_unknown)[0]}'."
            )


def _assertion_placement_index(
    assertions: list[dict], placement: object,
) -> int:
    if placement is None:
        return len(assertions)
    value = _object(placement, "placement")
    unknown = set(value) - {"before_key", "after_key"}
    if unknown:
        raise CycleSchemaError(
            f"placement contains unsupported field '{sorted(unknown)[0]}'."
        )
    before = value.get("before_key")
    after = value.get("after_key")
    if (before in (None, "")) == (after in (None, "")):
        raise CycleSchemaError(
            "placement must name exactly one before_key or after_key."
        )
    target = _key(before or after, "placement assertion key")
    keys = [str(assertion.get("key") or "") for assertion in assertions]
    if target not in keys:
        raise CycleSchemaError(f"Placement assertion '{target}' was not found.")
    index = keys.index(target)
    return index if before not in (None, "") else index + 1


def mutate_cycle_assertions(
    workspace,
    test: Mapping[str, object],
    assertions: Iterable[Mapping[str, object]],
    *,
    placement: object = None,
    actor: str = "auditor",
    manifest: Mapping[str, object] | None = None,
) -> tuple[dict, dict]:
    """Upsert typed assertion columns and selectively stale their item results.

    The definition remains the only executable source.  Existing results are
    projected through :func:`materialize_cycle_items`, whose assertion and input
    hashes retain complete comparison/evidence payloads only when they are
    still exact.  This function never evaluates a result.
    """

    current = validate_cycle_test(copy.deepcopy(dict(test)))
    incoming = [dict(assertion) for assertion in assertions]
    if not incoming:
        raise CycleSchemaError("Add at least one assertion.")

    existing = [dict(value) for value in current["definition"]["assertions"]]
    existing_by_key = {str(value["key"]): value for value in existing}
    assigned: list[dict] = []
    seen = set(existing_by_key)
    incoming_keys: set[str] = set()
    for index, assertion in enumerate(incoming):
        _validate_assertion_mutation_shape(
            assertion, label=f"assertions[{index}]"
        )
        key = _assigned_assertion_key(assertion, existing_keys=seen)
        if key in incoming_keys:
            raise CycleSchemaError(f"Duplicate assertion key '{key}' in mutation.")
        assertion["key"] = key
        assigned.append(assertion)
        incoming_keys.add(key)
        seen.add(key)

    changed_keys: list[str] = []
    new_assertions: list[dict] = []
    proposed = []
    replacements = {str(value["key"]): value for value in assigned}
    for existing_assertion in existing:
        key = str(existing_assertion["key"])
        replacement = replacements.pop(key, None)
        if replacement is None:
            proposed.append(existing_assertion)
            continue
        proposed.append(replacement)
        if replacement != existing_assertion:
            changed_keys.append(key)
    for assertion in assigned:
        if str(assertion["key"]) in existing_by_key:
            continue
        new_assertions.append(assertion)

    if placement is not None and not new_assertions:
        raise CycleSchemaError("placement applies only when adding a new assertion.")
    if new_assertions:
        index = _assertion_placement_index(proposed, placement)
        proposed[index:index] = new_assertions

    output = copy.deepcopy(dict(test))
    output.setdefault("definition", {})["assertions"] = proposed
    rcm_id = str(output.get("rcm_id") or "")
    rcm_row = next(
        (row for row in workspace.rcm if str(row.get("id") or "") == rcm_id),
        None,
    )
    if rcm_row is None:
        raise CycleSchemaError(f"RCM row '{rcm_id}' not found.")
    semantic_manifest = manifest or transaction_evidence_manifest(
        workspace, rcm_row.get("control_attributes") or []
    )
    output = validate_cycle_test_semantics(
        output,
        rcm_row=rcm_row,
        manifest=semantic_manifest,
    )

    before_definition_sha1 = cycle_definition_sha1(current)
    after_definition_sha1 = cycle_definition_sha1(output)
    definition_changed = before_definition_sha1 != after_definition_sha1
    before_items = {
        str(item.get("id") or ""): copy.deepcopy(item)
        for item in test.get("items") or []
    }
    if definition_changed:
        output["items"] = materialize_cycle_items(workspace, output)
    else:
        output["items"] = copy.deepcopy(list(test.get("items") or []))

    stale_dispositions = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in output.get("items") or []:
        prior = before_items.get(str(item.get("id") or "")) or {}
        history = copy.deepcopy(list(prior.get("disposition_history") or []))
        prior_disposition = dict(prior.get("disposition") or {})
        if (
            definition_changed
            and prior_disposition.get("state") in {"confirmed", "exception"}
            and not prior_disposition.get("stale")
        ):
            history.append(
                {
                    **prior_disposition,
                    "superseded_at": now,
                    "superseded_by": actor,
                    "reason": "cycle_assertion_definition_changed",
                    "definition_sha1": before_definition_sha1,
                }
            )
            stale_dispositions += 1
        if history:
            item["disposition_history"] = history

    if definition_changed and output.get("items"):
        output["status"] = "review_required"

    retained_results = 0
    pending_results = 0
    for item in output.get("items") or []:
        prior_results = (
            before_items.get(str(item.get("id") or ""), {}).get(
                "result_by_assertion"
            )
            or {}
        )
        for key, result in (item.get("result_by_assertion") or {}).items():
            if key in prior_results and result == prior_results[key]:
                retained_results += 1
            if result.get("verdict") == "not_run":
                pending_results += 1

    return output, {
        "changed": definition_changed,
        "new_assertion_keys": [str(value["key"]) for value in new_assertions],
        "changed_assertion_keys": changed_keys,
        "before_definition_sha1": before_definition_sha1,
        "after_definition_sha1": after_definition_sha1,
        "retained_result_count": retained_results,
        "pending_result_count": pending_results,
        "stale_disposition_count": stale_dispositions,
    }


def _validate_assertion_meaning(
    assertion: Mapping[str, object],
    *,
    role_kinds: Mapping[str, str],
    required_roles: set[str],
    multiplicity_by_kind: Mapping[str, Mapping[tuple[str, str, str], int]],
    pack_id: str,
    registry: CycleRegistry,
) -> None:
    """Reject assertions that are typed correctly and prove nothing.

    Structural validation establishes that an assertion *can* run. These rules
    establish that running it can produce audit evidence: a presence check on a
    field the form prints regardless, a scalar comparison against a selector the
    evidence already holds twice, and a date ordering stated against the
    cycle's own chronology all execute cleanly and answer nothing.
    """

    key = str(assertion.get("key") or "")
    operator = str(assertion.get("operator") or "")
    left = assertion.get("left")
    right = assertion.get("right")

    if operator == "present":
        if isinstance(left, Mapping) and left.get("source") == "row":
            raise CycleSchemaError(
                f"Assertion '{key}' checks that population column "
                f"'{left.get('column')}' is populated. That is a data test over the "
                "table, not evidence from a voucher; assert a role field, or compare "
                "the column to one."
            )
        if isinstance(left, Mapping) and left.get("source") == "role":
            role_name = str(left.get("role") or "")
            field = left.get("field") or {}
            selector = (
                str(field.get("group") or ""),
                str(field.get("kind") or ""),
                str(field.get("attribute") or ""),
            )
            if role_name in required_roles and not registry.control_evidence_attribute(
                pack_id, selector[0], selector[1], selector[2]
            ):
                raise CycleSchemaError(
                    f"Assertion '{key}' checks that required role '{role_name}' "
                    f"states {selector[0]}.{selector[1]}.{selector[2]}. The role is "
                    "already bound before any assertion runs, and the record prints "
                    "that field whether or not the control operated, so the check "
                    "can only fail on an extraction gap. Assert an approval or "
                    "attachment attribute, or compare the field to another record."
                )

    for operand in (left, right):
        if not isinstance(operand, Mapping) or operand.get("source") != "role":
            continue
        field = operand.get("field") or {}
        selector = (
            str(field.get("group") or ""),
            str(field.get("kind") or ""),
            str(field.get("attribute") or ""),
        )
        observed = int(
            multiplicity_by_kind.get(role_kinds[str(operand.get("role") or "")], {}).get(
                selector, 0
            )
        )
        if observed > 1:
            raise CycleSchemaError(
                f"Assertion '{key}' reads {selector[0]}.{selector[1]}.{selector[2]} "
                f"from role '{operand.get('role')}' as one value, but the evidence "
                f"holds {observed}. A scalar operand can only report that as "
                "ambiguous; use a roles operand with an explicit entry_quantifier, "
                "or select a selector the record states once."
            )

    if operator != "date_on_or_before":
        return
    orders = []
    for operand in (left, right):
        if not isinstance(operand, Mapping) or operand.get("source") != "role":
            return
        field = operand.get("field") or {}
        orders.append(
            registry.date_lifecycle_order(
                pack_id,
                role_kinds[str(operand.get("role") or "")],
                str(field.get("group") or ""),
                str(field.get("kind") or ""),
            )
        )
    if orders[0] is None or orders[1] is None or orders[0] <= orders[1]:
        return
    raise CycleSchemaError(
        f"Assertion '{key}' requires role '{left.get('role')}' "
        f"{left.get('field', {}).get('group')}."
        f"{left.get('field', {}).get('kind')} to fall on or before role "
        f"'{right.get('role')}' {right.get('field', {}).get('group')}."
        f"{right.get('field', {}).get('kind')}, but the registered cycle order "
        "puts it later. Swap the operands, or use date_within if the test is "
        "proximity rather than sequence."
    )


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
        # Definition regeneration does not discard prior evaluation or auditor
        # history.  The Phase 3 materializer reconciles these semantic item IDs
        # against current rows/evidence and marks only changed inputs stale.
        prior_items = list(existing.get("items") or [])
        existing.clear()
        existing.update({**validated, **preserved, "items": prior_items})
        return doc_tests.save_test(workspace, existing)
    return doc_tests.create_test(workspace, validated)


def _sha1_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha1:{hashlib.sha1(encoded.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    return json.loads(json.dumps(value, default=str))


def cycle_definition_sha1(test: Mapping[str, object]) -> str:
    """Hash the exact executable definition and registry identity."""

    return _sha1_hash(
        {
            "schema_version": test.get("schema_version"),
            "registry": test.get("registry"),
            "rcm_id": test.get("rcm_id"),
            "requirement_refs": test.get("requirement_refs") or [],
            "procedure_key": test.get("procedure_key"),
            "definition": test.get("definition") or {},
        }
    )


def stable_cycle_item_id(test: Mapping[str, object], row_key_value: object) -> str:
    """Semantic item identity, independent of source-row ordering."""

    definition = _object(test.get("definition"), "definition")
    population = _object(definition.get("population"), "definition.population")
    row_key = _object(population.get("row_key"), "definition.population.row_key")
    reference = _registry_reference(test.get("registry"), DEFAULT_REGISTRY)
    normalized = DEFAULT_REGISTRY.normalize_identifier(
        reference.pack_id,
        str(row_key.get("identifier_kind") or ""),
        row_key_value,
    )
    digest = hashlib.sha256(
        json.dumps(
            [
                str(test.get("semantic_id") or stable_test_semantic_id(test)),
                reference.definition_hash,
                str(population.get("table") or ""),
                str(row_key.get("identifier_kind") or ""),
                normalized,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"ITEM-{digest[:24].upper()}"


def _sample_row_indices(frame: pl.DataFrame, selection: Mapping[str, object]) -> list[int]:
    """Return a deterministic sample using Polars-backed row ranking."""

    size = min(int(selection.get("size") or 0), frame.height)
    if size <= 0:
        return []
    method = str(selection.get("method") or "")
    seed = int(selection.get("seed") or 0)
    indexed = frame.with_row_index("__cycle_source_row")
    if method == "interval":
        # Evenly spaced, stable positions; no hidden random starting point.
        return sorted({min((index * frame.height) // size, frame.height - 1) for index in range(size)})
    rank = pl.struct(frame.columns).hash(seed=seed).alias("__cycle_rank")
    ranked = indexed.with_columns(rank)
    if method == "random":
        return sorted(
            int(value)
            for value in ranked.sort(["__cycle_rank", "__cycle_source_row"])
            .head(size)["__cycle_source_row"]
            .to_list()
        )
    if method != "stratified":
        raise CycleSchemaError(f"Unsupported sampling method '{method}'.")
    stratum = str(selection.get("stratify_by") or "")
    ordered = ranked.sort([stratum, "__cycle_rank", "__cycle_source_row"])
    queues: dict[str, list[int]] = {}
    for row in ordered.select([stratum, "__cycle_source_row"]).iter_rows(named=True):
        queues.setdefault(json.dumps(row[stratum], default=str), []).append(
            int(row["__cycle_source_row"])
        )
    chosen: list[int] = []
    keys = sorted(queues)
    while len(chosen) < size and any(queues.values()):
        for key in keys:
            if queues[key] and len(chosen) < size:
                chosen.append(queues[key].pop(0))
    return sorted(chosen)


def _current_records(workspace, registry_ref: object) -> tuple[list[dict], dict[str, str]]:
    from . import document_analysis

    records = document_analysis.registry_evidence_records(workspace, registry_ref)
    extraction_hashes: dict[str, str] = {}
    for document in workspace.documents:
        document_id = str(document.get("id") or "")
        detail = document_analysis.load_analysis(
            workspace, document_id, document=document
        )
        artifact = detail.get("effective") or {}
        if artifact.get("analysis_profile") != "voucher":
            continue
        extraction_hashes[document_id] = str(
            artifact.get("evidence_content_sha256")
            or artifact.get("content_sha1")
            or artifact.get("source_sha1")
            or ""
        )
    return records, extraction_hashes


def _assertion_roles(assertion: Mapping[str, object]) -> set[str]:
    roles: set[str] = set()
    for operand in (assertion.get("left"), assertion.get("right")):
        if not isinstance(operand, Mapping):
            continue
        if operand.get("source") == "role":
            roles.add(str(operand.get("role") or ""))
        elif operand.get("source") == "roles":
            roles.update(str(value) for value in operand.get("roles") or [])
    return {value for value in roles if value}


def _assertion_inputs(
    assertion: Mapping[str, object],
    *,
    item: Mapping[str, object],
) -> dict:
    roles = _assertion_roles(assertion)
    bindings = [
        binding
        for binding in item.get("role_bindings") or []
        if str(binding.get("role") or "") in roles
    ]
    material = {
        "population_source_sha1": str(
            (item.get("population_ref") or {}).get("source_sha1") or ""
        ),
        "frozen_row_sha1": _sha1_hash(item.get("frozen_row") or {}),
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
        "role_binding_sha1": _sha1_hash(
            [
                {
                    "role": binding.get("role"),
                    "document_id": binding.get("document_id"),
                    "record_id": binding.get("record_id"),
                    "matched_by": binding.get("matched_by") or [],
                }
                for binding in bindings
            ]
        ),
    }
    material["input_sha1"] = _sha1_hash(material)
    return material


def _result_reusable(
    result: Mapping[str, object],
    *,
    assertion_sha1: str,
    inputs: Mapping[str, object],
    registry_definition_hash: str,
) -> bool:
    return bool(
        result.get("verdict") in ASSERTION_VERDICTS - {"not_run"}
        and not result.get("stale")
        and result.get("assertion_sha1") == assertion_sha1
        and result.get("registry_definition_hash") == registry_definition_hash
        and result.get("input_hashes") == inputs
    )


def _aggregate_evaluation(item: Mapping[str, object]) -> str:
    results = list((item.get("result_by_assertion") or {}).values())
    if not results or any(result.get("verdict") == "not_run" for result in results):
        return "stale" if any(result.get("stale") for result in results) else "not_run"
    verdicts = {str(result.get("verdict") or "not_run") for result in results}
    if "mismatch" in verdicts:
        return "failed"
    if (
        "ambiguous" in verdicts
        or item.get("role_conflicts")
        or item.get("collisions")
        or item.get("linkage_state") == "needs_review"
    ):
        return "needs_review"
    if verdicts & {"missing_evidence", "invalid_extraction"}:
        return "incomplete"
    return "passed"


# Every evidence record is re-validated against every assertion on each call,
# so this is the expensive step in a Document Test read. Several independent
# read-only projections (capability readiness, worklist summaries, report
# rendering) each resolve their own test scope and call this for the same
# test within a single request. ``request_cache_scope`` lets a caller certain
# no write happens in its span memoize by (workspace instance, test id, and
# the exact prior items the test carries in), so a materialization that
# would reproduce an identical result is skipped rather than redone.
# Reentrant: nesting is safe and only the outermost scope pays for teardown.
_cache: ContextVar[dict | None] = ContextVar("cycle_vouching_request_cache", default=None)


@contextmanager
def request_cache_scope():
    if _cache.get() is not None:
        yield
        return
    token = _cache.set({})
    try:
        yield
    finally:
        _cache.reset(token)


def materialize_cycle_items(workspace, test: Mapping[str, object]) -> list[dict]:
    """Select population rows and bind their complete exact record closures."""

    cache = _cache.get()
    cache_key = None
    if cache is not None:
        test_id = str(test.get("id") or "")
        if test_id:
            cache_key = (id(workspace), test_id, _sha1_hash(test.get("items") or []))
            cached = cache.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached)

    validated = validate_cycle_test(test)
    definition = validated["definition"]
    population = definition["population"]
    roles = definition["roles"]
    assertions = definition["assertions"]
    frame = workspace.get_frame(population["table"])
    source_sha1 = _frame_signature(frame)
    records, extraction_hashes = _current_records(workspace, validated["registry"])
    selection = population["selection"]
    selected_indices = (
        list(range(frame.height))
        if selection.get("mode") == "evidence_linked"
        else _sample_row_indices(frame, selection)
    )
    existing_by_id = {
        str(item.get("id") or ""): item for item in test.get("items") or []
    }
    definition_sha1 = cycle_definition_sha1(validated)
    reference = _registry_reference(validated["registry"], DEFAULT_REGISTRY)
    row_key = population["row_key"]
    key_specs = [row_key, *population["cycle_keys"]]
    materialized: list[dict] = []
    for source_row in selected_indices:
        row = frame.row(source_row, named=True)
        seeds = [
            {"kind": spec["identifier_kind"], "value": row.get(spec["column"])}
            for spec in key_specs
            if row.get(spec["column"]) is not None
            and str(row.get(spec["column"])).strip()
        ]
        linkage = link_cycle_records(
            registry_ref=validated["registry"],
            seeds=seeds,
            records=records,
            roles=roles,
        )
        if selection.get("mode") == "evidence_linked" and not linkage.get("records"):
            continue
        item_id = stable_cycle_item_id(validated, row[row_key["column"]])
        bindings = [
            {
                **binding,
                "extraction_hash": extraction_hashes.get(
                    str(binding.get("document_id") or ""), ""
                ),
            }
            for binding in linkage.get("role_bindings") or []
        ]
        bound_roles = {str(binding.get("role") or "") for binding in bindings}
        item = {
            "id": item_id,
            "label": str(row[row_key["column"]]),
            "instruction": str(test.get("objective") or "Vouch the transaction cycle."),
            "population_ref": {
                "table": population["table"],
                "source_row": source_row,
                "source_sha1": source_sha1,
            },
            "frozen_row": _plain_json(row),
            "cycle_identifiers": [
                {
                    "kind": spec["identifier_kind"],
                    "value": _plain_json(row.get(spec["column"])),
                }
                for spec in key_specs
                if row.get(spec["column"]) is not None
                and str(row.get(spec["column"])).strip()
            ],
            "role_bindings": bindings,
            "document_ids": list(
                dict.fromkeys(
                    str(binding.get("document_id") or "") for binding in bindings
                )
            ),
            "unassigned_records": list(linkage.get("unassigned_records") or []),
            "missing_roles": [
                str(role["role"])
                for role in roles
                if role.get("required") and str(role["role"]) not in bound_roles
            ],
            "role_conflicts": list(linkage.get("role_conflicts") or []),
            "linkage_state": str(linkage.get("state") or "linked"),
            **(
                {
                    "linkage_review": {
                        key: linkage.get(key)
                        for key in (
                            "review_reason",
                            "limit",
                            "counts",
                            "triggering_identifier",
                        )
                        if linkage.get(key) is not None
                    }
                }
                if linkage.get("state") == "needs_review"
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
            key = str(assertion["key"])
            assertion_sha1 = _sha1_hash(assertion)
            inputs = _assertion_inputs(assertion, item=item)
            old_result = old_results.get(key) or {}
            if _result_reusable(
                old_result,
                assertion_sha1=assertion_sha1,
                inputs=inputs,
                registry_definition_hash=reference.definition_hash,
            ):
                item["result_by_assertion"][key] = dict(old_result)
            else:
                item["result_by_assertion"][key] = {
                    "registry_definition_hash": reference.definition_hash,
                    "assertion_sha1": assertion_sha1,
                    "input_hashes": inputs,
                    "verdict": "not_run",
                    "display": "",
                    "comparisons": [],
                    "evidence_refs": [],
                    # Adding one assertion to a formerly evaluated item makes
                    # the aggregate evaluation stale even though that new key
                    # has no prior result object of its own.
                    "stale": bool(old_result) or prior_evaluation_current,
                    "result_sha1": None,
                }
        item["evaluation"]["state"] = _aggregate_evaluation(item)
        if item["evaluation"]["state"] in CURRENT_EVALUATION_STATES:
            item["evaluation"]["result_sha1"] = _sha1_hash(
                item["result_by_assertion"]
            )
        item["evidence_refs"] = _dedupe_evidence(
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
            if (
                item["disposition"]["state"] != "pending"
                and (
                    item["evaluation"]["state"] not in CURRENT_EVALUATION_STATES
                    or item["disposition"]["evaluated_definition_sha1"]
                    != definition_sha1
                )
            ):
                item["disposition"]["stale"] = True
        materialized.append(item)
    materialized = apply_cross_item_reuse(materialized, roles)
    for item in materialized:
        projected_state = _aggregate_evaluation(item)
        if item["evaluation"].get("state") != projected_state:
            item["evaluation"]["state"] = projected_state
            if (item.get("disposition") or {}).get("state") != "pending":
                item["disposition"]["stale"] = True
    result = sorted(materialized, key=lambda item: item["id"])
    if cache is not None and cache_key is not None:
        cache[cache_key] = copy.deepcopy(result)
    return result


def project_cycle_staleness(workspace, test: dict) -> dict:
    """Project current inputs without persisting or evaluating them."""

    if test.get("kind") != "cycle_vouch" or not test.get("items"):
        return test
    test["items"] = materialize_cycle_items(workspace, test)
    return test


def _evidence_catalog(workspace, document_ids: Iterable[str]) -> dict[tuple[str, str], dict]:
    """Resolve analysis citation IDs to stable typed document anchors."""

    from . import document_analysis
    from .evidence import normalize_anchor

    documents_by_id = {
        str(document.get("id") or ""): document for document in workspace.documents
    }
    catalog: dict[tuple[str, str], dict] = {}
    for document_id in sorted(set(document_ids)):
        document = documents_by_id.get(document_id)
        if document is None:
            continue
        artifact = (
            document_analysis.load_analysis(
                workspace, document_id, document=document
            ).get("effective")
            or {}
        )
        for citation in artifact.get("citations") or []:
            citation_id = str(citation.get("id") or "")
            if not citation_id:
                continue
            anchor_id = "EV-CYCLE-" + hashlib.sha1(
                f"{document_id}:{citation_id}".encode("utf-8")
            ).hexdigest()[:12].upper()
            catalog[(document_id, citation_id)] = normalize_anchor(
                {
                    "id": anchor_id,
                    "source_kind": "document",
                    "source_id": document_id,
                    "source_sha1": citation.get("source_sha1")
                    or document.get("sha1"),
                    "page": citation.get("page"),
                    "excerpt": str(citation.get("excerpt") or "")[:400],
                    "excerpt_hash": citation.get("excerpt_hash"),
                    "generated_by": "cycle-vouching",
                },
                require_hash=True,
            )
    return catalog


def _fact_entries(
    records_by_id: Mapping[str, Mapping[str, object]],
    bindings: Iterable[Mapping[str, object]],
    operand: Mapping[str, object],
    catalog: Mapping[tuple[str, str], dict],
) -> list[dict]:
    field = operand.get("field") or {}
    roles = (
        {str(operand.get("role") or "")}
        if operand.get("source") == "role"
        else {str(value) for value in operand.get("roles") or []}
    )
    entries: list[dict] = []
    for binding in bindings:
        if str(binding.get("role") or "") not in roles:
            continue
        record = records_by_id.get(str(binding.get("record_id") or "")) or {}
        for fact in record.get("fields") or []:
            if (
                str(fact.get("group") or "") != str(field.get("group") or "")
                or str(fact.get("kind") or "") != str(field.get("kind") or "")
                or str(fact.get("attribute") or "")
                != str(field.get("attribute") or "")
            ):
                continue
            envelope = fact.get("value") or {}
            citations = envelope.get("citation")
            citation_ids = citations if isinstance(citations, list) else [citations]
            evidence = [
                catalog[(str(binding.get("document_id") or ""), str(citation_id))]
                for citation_id in citation_ids
                if (
                    str(binding.get("document_id") or ""), str(citation_id)
                )
                in catalog
            ]
            entries.append(
                {
                    "role": str(binding.get("role") or ""),
                    "document_id": str(binding.get("document_id") or ""),
                    "record_id": str(binding.get("record_id") or ""),
                    "entry": int(fact.get("entry") or 0),
                    "raw_value": _plain_json(envelope.get("raw_value")),
                    "value": _plain_json(envelope.get("value")),
                    "normalization_status": str(
                        envelope.get("normalization_status") or "invalid"
                    ),
                    "normalization_error": envelope.get("normalization_error"),
                    "evidence_refs": evidence,
                }
            )
    return entries


def _bounded_value(value: object) -> object:
    plain = _plain_json(value)
    if isinstance(plain, str) and len(plain) > 200:
        return plain[:197] + "..."
    return plain


def _comparison(operator: str, left: object, right: object, tolerance: object) -> str:
    if operator == "present":
        if left in (None, ""):
            return "missing_evidence"
        return "match"
    if operator == "equal_exact":
        # Two extractions of one quantity may normalize to 25 and 25.0. Those
        # are the same number and comparing their text is a false mismatch, so
        # already-typed numbers compare numerically. Strings stay textual: an
        # identifier is exact by definition and must not acquire numeric
        # equality on the way past.
        if all(
            isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
            for value in (left, right)
        ):
            return "match" if Decimal(str(left)) == Decimal(str(right)) else "mismatch"
        return "match" if str(left) == str(right) else "mismatch"
    if operator == "equal_normalized":
        normalize = lambda value: " ".join(
            re.sub(
                r"\s+",
                " ",
                unicodedata.normalize("NFKC", str(value or "")).strip(),
            )
            .casefold()
            .split()
        )
        return "match" if normalize(left) == normalize(right) else "mismatch"
    if operator == "numeric_within":
        try:
            left_number = Decimal(str(left).replace(",", ""))
            right_number = Decimal(str(right).replace(",", ""))
        except (InvalidOperation, ValueError):
            return "invalid_extraction"
        config = tolerance if isinstance(tolerance, Mapping) else {}
        absolute = Decimal(str(config.get("absolute") or 0))
        percent = Decimal(str(config.get("percent") or 0))
        allowed = max(absolute, abs(left_number) * percent / Decimal("100"))
        return "match" if abs(left_number - right_number) <= allowed else "mismatch"

    def parsed(value: object) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(str(value)[:10]).date()
        except ValueError:
            return None

    left_date, right_date = parsed(left), parsed(right)
    if left_date is None or right_date is None:
        return "invalid_extraction"
    if operator == "date_on_or_before":
        return "match" if left_date <= right_date else "mismatch"
    if operator == "date_within":
        return (
            "match"
            if abs((left_date - right_date).days) <= int(tolerance or 0)
            else "mismatch"
        )
    raise CycleSchemaError(f"Unsupported assertion operator '{operator}'.")


def _scalar_operand(
    item: Mapping[str, object],
    operand: Mapping[str, object],
    records_by_id: Mapping[str, Mapping[str, object]],
    catalog: Mapping[tuple[str, str], dict],
) -> dict:
    if operand.get("source") == "row":
        column = str(operand.get("column") or "")
        value = (item.get("frozen_row") or {}).get(column)
        if value in (None, ""):
            return {"state": "missing_evidence", "entries": []}
        return {
            "state": "resolved",
            "value": value,
            "entries": [{"value": _bounded_value(value), "column": column}],
        }
    conflicted_role = str(operand.get("role") or "")
    conflict = next(
        (
            value
            for value in item.get("role_conflicts") or []
            if str(value.get("role") or "") == conflicted_role
        ),
        None,
    )
    if conflict is not None:
        return {
            "state": "ambiguous",
            "entries": [],
            "record_ids": list(conflict.get("record_ids") or []),
        }
    entries = _fact_entries(
        records_by_id, item.get("role_bindings") or [], operand, catalog
    )
    normalized = [
        entry
        for entry in entries
        if entry["normalization_status"] == "normalized"
        and entry.get("value") not in (None, "")
    ]
    invalid = [entry for entry in entries if entry["normalization_status"] == "invalid"]
    if not entries:
        return {"state": "missing_evidence", "entries": []}
    if invalid:
        return {"state": "invalid_extraction", "entries": entries}
    distinct = {
        json.dumps(entry.get("value"), sort_keys=True, default=str)
        for entry in normalized
    }
    if not normalized:
        return {"state": "missing_evidence", "entries": entries}
    if len(distinct) != 1:
        return {"state": "ambiguous", "entries": entries}
    return {"state": "resolved", "value": normalized[0]["value"], "entries": entries}


def _aggregate_verdict(verdicts: list[str], quantifier: str) -> str:
    if not verdicts:
        return "missing_evidence"
    if quantifier == "any" and "match" in verdicts:
        return "match"
    if quantifier == "all" and all(verdict == "match" for verdict in verdicts):
        return "match"
    if "mismatch" in verdicts and (
        quantifier == "all" or all(verdict == "mismatch" for verdict in verdicts)
    ):
        return "mismatch"
    if "ambiguous" in verdicts:
        return "ambiguous"
    if "invalid_extraction" in verdicts:
        return "invalid_extraction"
    if "missing_evidence" in verdicts:
        return "missing_evidence"
    return "mismatch"


def _evaluate_role_set(
    item: Mapping[str, object],
    assertion: Mapping[str, object],
    scalar: Mapping[str, object],
    set_operand: Mapping[str, object],
    *,
    set_is_left: bool,
    records_by_id: Mapping[str, Mapping[str, object]],
    catalog: Mapping[tuple[str, str], dict],
) -> tuple[str, list[dict], list[dict]]:
    if scalar["state"] != "resolved":
        return str(scalar["state"]), [{"side": "scalar", **dict(scalar)}], []
    all_entries = _fact_entries(
        records_by_id, item.get("role_bindings") or [], set_operand, catalog
    )
    comparisons: list[dict] = []
    evidence: list[dict] = []
    entry_quantifier = str(set_operand.get("entry_quantifier") or "one")
    for role in set_operand.get("roles") or []:
        role_bindings = [
            binding
            for binding in item.get("role_bindings") or []
            if str(binding.get("role") or "") == str(role)
        ]
        if not role_bindings:
            conflict = next(
                (
                    value
                    for value in item.get("role_conflicts") or []
                    if str(value.get("role") or "") == str(role)
                ),
                None,
            )
            comparisons.append(
                {
                    "role": role,
                    "document_id": None,
                    "record_ids": list((conflict or {}).get("record_ids") or []),
                    "verdict": "ambiguous" if conflict else "missing_evidence",
                    "entries": [],
                }
            )
            continue
        by_document: dict[str, list[dict]] = {}
        for entry in all_entries:
            if entry["role"] == role:
                by_document.setdefault(entry["document_id"], []).append(entry)
        for document_id in sorted(
            {str(binding.get("document_id") or "") for binding in role_bindings}
        ):
            entries = by_document.get(document_id, [])
            valid = [
                entry
                for entry in entries
                if entry["normalization_status"] == "normalized"
                and entry.get("value") not in (None, "")
            ]
            invalid = [
                entry for entry in entries if entry["normalization_status"] == "invalid"
            ]
            entry_results: list[dict] = []
            for entry in valid:
                verdict = _comparison(
                    str(assertion["operator"]),
                    entry["value"] if set_is_left else scalar["value"],
                    scalar["value"] if set_is_left else entry["value"],
                    assertion.get("tolerance"),
                )
                entry_results.append(
                    {"value": _bounded_value(entry["value"]), "verdict": verdict}
                )
                evidence.extend(entry.get("evidence_refs") or [])
            if not entries:
                verdict = "missing_evidence"
            elif invalid:
                verdict = "invalid_extraction"
            elif entry_quantifier == "one":
                distinct = {
                    json.dumps(entry["value"], sort_keys=True, default=str)
                    for entry in valid
                }
                verdict = (
                    "missing_evidence"
                    if not valid
                    else "ambiguous"
                    if len(distinct) != 1
                    else entry_results[0]["verdict"]
                )
            else:
                verdict = _aggregate_verdict(
                    [entry["verdict"] for entry in entry_results], entry_quantifier
                )
            comparisons.append(
                {
                    "role": role,
                    "document_id": document_id,
                    "record_ids": sorted(
                        str(binding.get("record_id") or "")
                        for binding in role_bindings
                        if str(binding.get("document_id") or "") == document_id
                    ),
                    "verdict": verdict,
                    "entries": [
                        {
                            "entry": entry.get("entry"),
                            "raw_value": _bounded_value(entry.get("raw_value")),
                            "value": _bounded_value(entry.get("value")),
                            "normalization_status": entry.get("normalization_status"),
                            "normalization_error": entry.get("normalization_error"),
                            "evidence_refs": entry.get("evidence_refs") or [],
                        }
                        for entry in entries
                    ],
                    "entry_results": entry_results,
                }
            )
    verdict = _aggregate_verdict(
        [str(comparison["verdict"]) for comparison in comparisons],
        str(assertion.get("role_quantifier") or "all"),
    )
    return verdict, comparisons, _dedupe_evidence(evidence)


def evaluate_cycle_item(
    workspace,
    test: Mapping[str, object],
    item: dict,
    *,
    records: Iterable[Mapping[str, object]] | None = None,
) -> dict:
    """Evaluate pending/stale assertions locally, retaining every sub-result."""

    validated = validate_cycle_test(test)
    reference = _registry_reference(validated["registry"], DEFAULT_REGISTRY)
    record_values = list(records or _current_records(workspace, validated["registry"])[0])
    records_by_id = {str(record["record_id"]): record for record in record_values}
    catalog = _evidence_catalog(
        workspace,
        [str(binding.get("document_id") or "") for binding in item.get("role_bindings") or []],
    )
    results = item.setdefault("result_by_assertion", {})
    for assertion in validated["definition"]["assertions"]:
        key = str(assertion["key"])
        current = results.get(key) or {}
        if _result_reusable(
            current,
            assertion_sha1=_sha1_hash(assertion),
            inputs=_assertion_inputs(assertion, item=item),
            registry_definition_hash=reference.definition_hash,
        ):
            continue
        left = assertion["left"]
        right = assertion.get("right")
        set_operand = next(
            (
                operand
                for operand in (left, right)
                if isinstance(operand, Mapping) and operand.get("source") == "roles"
            ),
            None,
        )
        evidence: list[dict] = []
        if set_operand is not None:
            scalar_operand = right if set_operand is left else left
            scalar = _scalar_operand(item, scalar_operand, records_by_id, catalog)
            for entry in scalar.get("entries") or []:
                evidence.extend(entry.get("evidence_refs") or [])
            verdict, comparisons, set_evidence = _evaluate_role_set(
                item,
                assertion,
                scalar,
                set_operand,
                set_is_left=set_operand is left,
                records_by_id=records_by_id,
                catalog=catalog,
            )
            evidence.extend(set_evidence)
        elif assertion["operator"] == "present":
            resolved = _scalar_operand(item, left, records_by_id, catalog)
            evidence = [
                anchor
                for entry in resolved.get("entries") or []
                for anchor in entry.get("evidence_refs") or []
            ]
            # ``_scalar_operand`` already decides this: it resolves only when
            # exactly one usable value exists, and otherwise names why. Re-deriving
            # the verdict by scanning entries for a normalization status reported a
            # resolved population-row value as missing evidence, because a row
            # entry carries a column and a value and never had a normalization
            # envelope to scan.
            verdict = (
                "match" if resolved["state"] == "resolved" else str(resolved["state"])
            )
            comparisons = [{"side": "left", **resolved}]
        else:
            left_value = _scalar_operand(item, left, records_by_id, catalog)
            right_value = _scalar_operand(item, right, records_by_id, catalog)
            states = {left_value["state"], right_value["state"]}
            verdict = (
                "ambiguous"
                if "ambiguous" in states
                else "invalid_extraction"
                if "invalid_extraction" in states
                else "missing_evidence"
                if "missing_evidence" in states
                else _comparison(
                    str(assertion["operator"]),
                    left_value["value"],
                    right_value["value"],
                    assertion.get("tolerance"),
                )
            )
            comparisons = [
                {"side": "left", **left_value},
                {"side": "right", **right_value},
            ]
            evidence = [
                anchor
                for resolved in (left_value, right_value)
                for entry in resolved.get("entries") or []
                for anchor in entry.get("evidence_refs") or []
            ]
        result = {
            "registry_definition_hash": reference.definition_hash,
            "assertion_sha1": _sha1_hash(assertion),
            "input_hashes": _assertion_inputs(assertion, item=item),
            "verdict": verdict,
            "display": " vs ".join(
                str(_bounded_value(value))
                for value in (
                    (
                        comparisons[0].get("value")
                        if comparisons and comparisons[0].get("state") == "resolved"
                        else None
                    ),
                    (
                        comparisons[1].get("value")
                        if len(comparisons) > 1
                        and comparisons[1].get("state") == "resolved"
                        else None
                    ),
                )
                if value is not None
            )[:240],
            "comparisons": comparisons,
            "evidence_refs": _dedupe_evidence(evidence),
            "stale": False,
        }
        result["result_sha1"] = _sha1_hash(result)
        results[key] = result
    item["evaluation"] = {
        "state": _aggregate_evaluation(item),
        "definition_sha1": cycle_definition_sha1(validated),
        "result_sha1": _sha1_hash(results),
    }
    item["evidence_refs"] = _dedupe_evidence(
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


def evaluate_cycle_test(workspace, test: Mapping[str, object]) -> dict:
    """Materialize current inputs and evaluate only work that is not current."""

    output = dict(test)
    output["items"] = materialize_cycle_items(workspace, output)
    records, _extraction_hashes = _current_records(workspace, output["registry"])
    for item in output["items"]:
        if execution_pending(item, cycle=True):
            evaluate_cycle_item(workspace, output, item, records=records)
    dispositions_current = bool(output["items"]) and all(
        disposition_current(item, cycle=True) for item in output["items"]
    )
    output["status"] = "completed" if dispositions_current else "review_required"
    return output


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
    disposition_history = _list(
        item.get("disposition_history") or [],
        "cycle item.disposition_history",
    )
    for index, raw_history in enumerate(disposition_history):
        history = _object(
            raw_history, f"cycle item.disposition_history[{index}]"
        )
        if history.get("state") not in {"confirmed", "exception"}:
            raise CycleSchemaError(
                "Cycle item disposition history must contain a signed disposition."
            )
        if not isinstance(history.get("stale", False), bool):
            raise CycleSchemaError(
                "Cycle item disposition history stale must be boolean."
            )
        _text(
            history.get("superseded_at"),
            f"cycle item.disposition_history[{index}].superseded_at",
        )
        _text(
            history.get("reason"),
            f"cycle item.disposition_history[{index}].reason",
        )
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
    if disposition_history or "disposition_history" in item:
        item["disposition_history"] = disposition_history
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


def _assurance_label(scope: str) -> str:
    return (
        "Targeted evidence - not a sample"
        if scope == "targeted_evidence_only"
        else "Sampled population"
    )


def result_rollup(test: Mapping[str, object]) -> dict:
    """Count cycle items and assertion cells as separate, non-additive units."""

    validated = validate_cycle_test(test)
    items = list(test.get("items") or [])
    item_counts = {state: 0 for state in sorted(EVALUATION_STATES)}
    disposition_counts = {state: 0 for state in sorted(DISPOSITION_STATES)}
    assertion_counts = {verdict: 0 for verdict in sorted(ASSERTION_VERDICTS)}
    assertion_keys = [
        str(assertion["key"])
        for assertion in validated["definition"]["assertions"]
    ]
    for item in items:
        evaluation_state = str(
            (item.get("evaluation") or {}).get("state") or "not_run"
        )
        item_counts[evaluation_state] += 1
        disposition_state = str(
            (item.get("disposition") or {}).get("state") or "pending"
        )
        if disposition_current(item, cycle=True):
            disposition_counts[disposition_state] += 1
        else:
            disposition_counts["pending"] += 1
        results = item.get("result_by_assertion") or {}
        for key in assertion_keys:
            verdict = str((results.get(key) or {}).get("verdict") or "not_run")
            assertion_counts[verdict] += 1

    scope = assurance_scope_for(
        validated["definition"]["population"]["selection"]
    )
    selection_basis = str(
        validated["definition"]["population"]["selection"].get("mode") or ""
    )
    coverage = {
        **dict(test.get("coverage") or {}),
        "selection_basis": selection_basis,
        "assurance_scope": scope,
    }
    tested_items = sum(
        item_counts[state] for state in sorted(CURRENT_EVALUATION_STATES)
    )
    current_items = [item for item in items if execution_current(item, cycle=True)]
    failed_items = sum(
        any(
            result.get("verdict") == "mismatch"
            for result in (item.get("result_by_assertion") or {}).values()
        )
        for item in current_items
    )
    incomplete_items = sum(
        any(
            result.get("verdict") in {"missing_evidence", "invalid_extraction"}
            for result in (item.get("result_by_assertion") or {}).values()
        )
        for item in current_items
    )
    pending_dispositions = sum(
        disposition_pending(item, cycle=True) for item in items
    )
    evaluations_current = bool(items) and tested_items == len(items)
    dispositions_current = bool(items) and (
        disposition_counts["confirmed"] + disposition_counts["exception"]
        == len(items)
    )
    conclusion_eligible = bool(
        scope == "sampled_population"
        and evaluations_current
        and dispositions_current
        and not item_counts["incomplete"]
        and not item_counts["needs_review"]
    )
    control_conclusion = str(test.get("control_conclusion") or "no_conclusion")
    if not conclusion_eligible:
        control_conclusion = "no_conclusion"
    return {
        "items": len(items),
        "tested_items": tested_items,
        "item_counts": item_counts,
        "disposition_counts": disposition_counts,
        "assertion_columns": len(assertion_keys),
        "assertion_counts": {
            "total": len(items) * len(assertion_keys),
            **assertion_counts,
        },
        "failed_items": failed_items,
        "incomplete_items": incomplete_items,
        "needs_review_items": item_counts["needs_review"],
        "confirmed_items": disposition_counts["confirmed"],
        "exception_items": disposition_counts["exception"],
        "open_exceptions": disposition_counts["exception"],
        "pending_dispositions": pending_dispositions,
        "coverage": coverage,
        "assurance_scope": scope,
        "assurance_label": _assurance_label(scope),
        "conclusion_eligible": conclusion_eligible,
        "control_conclusion": control_conclusion,
        "assertion_mismatches": assertion_counts["mismatch"],
        # Common Document Test rollup fields remain canonical for consumers
        # that aggregate all test kinds. They are not added to the item counts.
        "matched": assertion_counts["match"],
        "mismatched": sum(
            assertion_counts[verdict]
            for verdict in (
                "mismatch",
                "missing_evidence",
                "invalid_extraction",
                "ambiguous",
            )
        ),
        "confirmed": disposition_counts["confirmed"],
        "exceptions": disposition_counts["exception"],
        "manual_review": sum(
            1
            for item in items
            if execution_current(item, cycle=True)
            and not disposition_current(item, cycle=True)
        ),
        "pending": sum(
            1
            for item in items
            if execution_pending(item, cycle=True)
            or disposition_pending(item, cycle=True)
        ),
    }


def _grid_comparison(value: object) -> dict:
    """Project one comparison without extraction envelopes or evidence text."""

    comparison = _object(value, "assertion comparison")
    entries = [
        _object(entry, "assertion comparison entry")
        for entry in comparison.get("entries") or []
    ]
    evidence_count = sum(
        len(entry.get("evidence_refs") or []) for entry in entries
    )
    display_values = []
    for entry in comparison.get("entry_results") or []:
        entry_object = _object(entry, "assertion comparison result")
        if "value" in entry_object:
            display_values.append(_bounded_value(entry_object.get("value")))
    if not display_values and comparison.get("state") == "resolved":
        display_values.append(_bounded_value(comparison.get("value")))
    return {
        key: comparison.get(key)
        for key in ("side", "role", "document_id", "state", "verdict")
        if comparison.get(key) is not None
    } | {
        "record_ids": [
            str(record_id) for record_id in comparison.get("record_ids") or []
        ],
        "display_values": display_values,
        "entry_count": len(entries),
        "evidence_count": evidence_count,
    }


def _grid_cell(result: Mapping[str, object]) -> dict:
    comparisons = [
        _grid_comparison(value) for value in result.get("comparisons") or []
    ]
    return {
        "verdict": str(result.get("verdict") or "not_run"),
        "display": str(result.get("display") or "")[:240],
        "comparison_count": len(comparisons),
        "evidence_count": len(result.get("evidence_refs") or []),
        "comparisons": comparisons,
    }


def _assert_grid_definition_current(
    test: Mapping[str, object],
    *,
    definition_sha1: str,
    assertions: Mapping[str, Mapping[str, object]],
) -> None:
    """Fail when evaluated cells cannot be attributed to current columns."""

    for item in test.get("items") or []:
        results = item.get("result_by_assertion") or {}
        evaluated = any(
            str(result.get("verdict") or "not_run") != "not_run"
            for result in results.values()
        )
        item_definition_sha1 = str(
            (item.get("evaluation") or {}).get("definition_sha1") or ""
        )
        if evaluated and item_definition_sha1 != definition_sha1:
            raise GridStaleDefinitionError(
                "Cycle results were produced for a different test definition."
            )
        for key, result in results.items():
            if str(result.get("verdict") or "not_run") == "not_run":
                continue
            assertion = assertions.get(str(key))
            if assertion is None or result.get("assertion_sha1") != _sha1_hash(assertion):
                raise GridStaleDefinitionError(
                    "Cycle results cannot be attributed to the current assertion columns."
                )


def grid_projection(
    test: Mapping[str, object],
    *,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """Return a bounded, read-only grid over canonical cycle item results."""

    if str(test.get("kind") or "") != "cycle_vouch":
        raise CycleSchemaError("The grid is available only for cycle_vouch tests.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CycleSchemaError("Grid offset must be a non-negative integer.")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > MAX_GRID_PAGE_SIZE
    ):
        raise CycleSchemaError(
            f"Grid limit must be between 1 and {MAX_GRID_PAGE_SIZE}."
        )
    validated = validate_cycle_test(test)
    assertion_values = validated["definition"]["assertions"]
    if len(assertion_values) > MAX_ASSERTIONS:
        # validate_cycle_test enforces this too; retaining the projection guard
        # makes the no-silent-column-truncation property explicit here.
        raise CycleSchemaError(
            f"A cycle grid may project at most {MAX_ASSERTIONS} assertions."
        )
    assertions = {
        str(assertion["key"]): assertion for assertion in assertion_values
    }
    definition_sha1 = cycle_definition_sha1(validated)
    _assert_grid_definition_current(
        test,
        definition_sha1=definition_sha1,
        assertions=assertions,
    )
    items = sorted(
        [dict(item) for item in test.get("items") or []],
        key=lambda item: (str(item.get("label") or ""), str(item.get("id") or "")),
    )
    rollup = result_rollup({**dict(validated), **dict(test), "items": items})
    columns = []
    for assertion in assertion_values:
        key = str(assertion["key"])
        applicable_roles: list[str] = []
        for operand in (assertion.get("left"), assertion.get("right")):
            if not isinstance(operand, Mapping):
                continue
            if operand.get("source") == "role":
                applicable_roles.append(str(operand.get("role") or ""))
            elif operand.get("source") == "roles":
                applicable_roles.extend(str(role) for role in operand.get("roles") or [])
        counts = {verdict: 0 for verdict in sorted(ASSERTION_VERDICTS)}
        for item in items:
            result = (item.get("result_by_assertion") or {}).get(key) or {}
            counts[str(result.get("verdict") or "not_run")] += 1
        columns.append(
            {
                "key": key,
                "label": str(assertion.get("label") or key),
                "operator": str(assertion.get("operator") or ""),
                "applicable_roles": list(dict.fromkeys(applicable_roles)),
                "counts": counts,
            }
        )
    page_items = items[offset : offset + limit]
    rows = []
    for item in page_items:
        results = item.get("result_by_assertion") or {}
        rows.append(
            {
                "item_id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "evaluation_state": str(
                    (item.get("evaluation") or {}).get("state") or "not_run"
                ),
                "disposition_state": str(
                    (item.get("disposition") or {}).get("state") or "pending"
                ),
                "disposition_stale": bool(
                    (item.get("disposition") or {}).get("stale")
                ),
                "roles_present": sorted({
                    str(binding.get("role") or "")
                    for binding in item.get("role_bindings") or []
                }),
                "missing_roles": list(item.get("missing_roles") or []),
                "shared_record_facts": [
                    {
                        "role": str(fact.get("role") or ""),
                        "record_id": str(fact.get("record_id") or ""),
                        "related_item_ids": list(
                            fact.get("related_item_ids") or []
                        )[:MAX_GRID_RELATED_ITEMS],
                        "related_item_count": len(
                            fact.get("related_item_ids") or []
                        ),
                        "related_items_truncated": len(
                            fact.get("related_item_ids") or []
                        ) > MAX_GRID_RELATED_ITEMS,
                        "reuse_across_items": str(
                            fact.get("reuse_across_items") or ""
                        ),
                        "identifier_edge": dict(fact.get("identifier_edge") or {}),
                    }
                    for fact in item.get("shared_record_facts") or []
                ],
                "cells": {
                    key: _grid_cell(results.get(key) or {"verdict": "not_run"})
                    for key in assertions
                },
            }
        )
    selection = validated["definition"]["population"]["selection"]
    scope = assurance_scope_for(selection)
    total = len(items)
    return {
        "test_id": str(test.get("id") or ""),
        "test_sha1": str(test.get("sha1") or ""),
        "definition_sha1": definition_sha1,
        "title": str(test.get("title") or ""),
        "population": dict(validated["definition"]["population"]),
        "coverage": dict(rollup["coverage"]),
        "selection_basis": str(selection.get("mode") or ""),
        "assurance_scope": scope,
        "assurance_label": _assurance_label(scope),
        "tested_item_counts": dict(rollup["item_counts"]),
        "assertion_counts": dict(rollup["assertion_counts"]),
        "columns": columns,
        "rows": rows,
        "page": {"offset": offset, "limit": limit, "total": total},
        "truncated": offset + len(rows) < total,
    }


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
            "max_grid_page_size": MAX_GRID_PAGE_SIZE,
            "max_grid_related_items": MAX_GRID_RELATED_ITEMS,
            "min_cycle_record_kinds": MIN_CYCLE_RECORD_KINDS,
        },
    }


# Compatibility aliases for Phase 0 callers.  The public nouns are now generic;
# these names can be removed once no persisted Phase 0 code imports them.
validate_record_fragment = validate_evidence_record_fragment
validate_reduced_record = validate_evidence_record
validate_reduction = validate_evidence_reduction
