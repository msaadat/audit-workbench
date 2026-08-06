"""Domain-neutral contracts for registry-backed cycle evidence tests.

The core owns structural validation only.  Record, identifier and field
vocabulary comes from immutable, hash-identified registry packs; neither the
model nor a workspace payload may introduce new kinds at runtime.
"""

from __future__ import annotations

import re
from typing import Mapping

from .cycle_registry import DEFAULT_REGISTRY, CycleRegistry, RegistryError
from .cycle_registry.models import RegistryReference


class CycleSchemaError(ValueError):
    """A cycle-evidence payload violates the closed schema."""


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

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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
    table_columns: set[str] | None,
) -> tuple[dict, str, bool]:
    operand = _object(value, label)
    source = str(operand.get("source") or "")
    if source == "row":
        column = _text(operand.get("column"), f"{label}.column")
        if table_columns is not None and column not in table_columns:
            raise CycleSchemaError(f"{label}.column '{column}' does not exist.")
        semantic_type = str(operand.get("value_type") or "unknown")
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
    table_columns: set[str] | None = None,
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
    table_columns: set[str] | None = None,
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
    table_columns: set[str] | None = None,
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
        },
    }


# Compatibility aliases for Phase 0 callers.  The public nouns are now generic;
# these names can be removed once no persisted Phase 0 code imports them.
validate_record_fragment = validate_evidence_record_fragment
validate_reduced_record = validate_evidence_record
validate_reduction = validate_evidence_reduction
