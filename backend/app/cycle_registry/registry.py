"""Validated, hash-identified registry for transaction-evidence packs."""

from __future__ import annotations

import hashlib
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from .models import (
    CyclePackDefinition,
    EvidenceKindDefinition,
    FieldKindDefinition,
    IdentifierKindDefinition,
    NormalizerDefinition,
    RecordKindDefinition,
    RegistryReference,
)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_SEMANTIC_TYPES = {"identifier", "date", "number", "text", "boolean"}


class RegistryError(ValueError):
    """A registry definition or reference is unsafe or inconsistent."""


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _definitions(values: Iterable, label: str) -> Mapping[str, object]:
    output = {}
    for value in values:
        identifier = str(getattr(value, "id", "") or "")
        if not _ID_RE.fullmatch(identifier):
            raise RegistryError(f"{label} id '{identifier}' is invalid.")
        if identifier in output:
            raise RegistryError(f"Duplicate {label} id '{identifier}'.")
        output[identifier] = value
    return MappingProxyType(output)


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise RegistryError(f"{label} contains duplicate ids.")


class CycleRegistry:
    """Immutable catalog of closed domain packs and their shared definitions."""

    def __init__(
        self,
        *,
        normalizers: Iterable[NormalizerDefinition],
        identifier_kinds: Iterable[IdentifierKindDefinition],
        field_kinds: Iterable[FieldKindDefinition],
        record_kinds: Iterable[RecordKindDefinition],
        evidence_kinds: Iterable[EvidenceKindDefinition],
        packs: Iterable[CyclePackDefinition],
    ):
        self.normalizers = _definitions(normalizers, "normalizer")
        self.identifier_kinds = _definitions(identifier_kinds, "identifier kind")
        self.field_kinds = _definitions(field_kinds, "field kind")
        self.record_kinds = _definitions(record_kinds, "record kind")
        self.evidence_kinds = _definitions(evidence_kinds, "evidence kind")
        self.packs = _definitions(packs, "cycle pack")
        self._field_selectors: Mapping[tuple[str, str], FieldKindDefinition]
        self._pack_hashes: Mapping[str, str]
        self._validate()

    def _validate(self) -> None:
        selectors: dict[tuple[str, str], FieldKindDefinition] = {}
        for definition in self.identifier_kinds.values():
            if definition.edge_policy not in {"transaction", "non_linking"}:
                raise RegistryError(
                    f"Identifier kind '{definition.id}' has an invalid edge policy."
                )
            if definition.value_type not in _SEMANTIC_TYPES:
                raise RegistryError(
                    f"Identifier kind '{definition.id}' has an invalid value type."
                )
            if definition.normalizer_id not in self.normalizers:
                raise RegistryError(
                    f"Identifier kind '{definition.id}' names unknown normalizer "
                    f"'{definition.normalizer_id}'."
                )
        for definition in self.field_kinds.values():
            selector = (definition.group, definition.kind)
            if selector in selectors:
                raise RegistryError(
                    f"Field selector {definition.group}.{definition.kind} is duplicated."
                )
            selectors[selector] = definition
            attributes = [attribute.id for attribute in definition.attributes]
            if not attributes or len(attributes) != len(set(attributes)):
                raise RegistryError(
                    f"Field kind '{definition.id}' needs unique attributes."
                )
            if any(
                attribute.semantic_type not in _SEMANTIC_TYPES
                for attribute in definition.attributes
            ):
                raise RegistryError(
                    f"Field kind '{definition.id}' has an invalid semantic type."
                )
        for definition in self.record_kinds.values():
            _unique(definition.primary_identifier_kinds, definition.id)
            _unique(definition.available_field_kinds, definition.id)
            if definition.bindable and not definition.primary_identifier_kinds:
                raise RegistryError(
                    f"Bindable record kind '{definition.id}' needs a primary identifier."
                )
            for identifier_id in definition.primary_identifier_kinds:
                if identifier_id not in self.identifier_kinds:
                    raise RegistryError(
                        f"Record kind '{definition.id}' names unknown primary identifier "
                        f"'{identifier_id}'."
                    )
            for field_id in definition.available_field_kinds:
                if field_id not in self.field_kinds:
                    raise RegistryError(
                        f"Record kind '{definition.id}' names unknown field '{field_id}'."
                    )
        hashes = {}
        for pack in self.packs.values():
            if (
                not isinstance(pack.version, int)
                or isinstance(pack.version, bool)
                or pack.version < 1
            ):
                raise RegistryError(f"Cycle pack '{pack.id}' has an invalid version.")
            for values, known, label in (
                (pack.normalizer_ids, self.normalizers, "normalizer"),
                (pack.identifier_kind_ids, self.identifier_kinds, "identifier kind"),
                (pack.field_kind_ids, self.field_kinds, "field kind"),
                (pack.record_kind_ids, self.record_kinds, "record kind"),
            ):
                _unique(values, f"Cycle pack '{pack.id}' {label}s")
                unknown = [value for value in values if value not in known]
                if unknown:
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' names unknown {label} '{unknown[0]}'."
                    )
            if "common.other" not in pack.record_kind_ids:
                raise RegistryError(
                    f"Cycle pack '{pack.id}' must include the non-bindable common.other kind."
                )
            for identifier_id in pack.identifier_kind_ids:
                normalizer_id = self.identifier_kinds[identifier_id].normalizer_id
                if normalizer_id not in pack.normalizer_ids:
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' omits normalizer '{normalizer_id}'."
                    )
            staged: set[tuple[str, str, str]] = set()
            for stage in pack.date_lifecycle:
                selector = (stage.record_kind_id, stage.group, stage.kind)
                if selector in staged:
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' declares {stage.group}.{stage.kind} "
                        f"twice for '{stage.record_kind_id}'."
                    )
                staged.add(selector)
                if stage.record_kind_id not in pack.record_kind_ids:
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' stages unknown record kind "
                        f"'{stage.record_kind_id}'."
                    )
                field = selectors.get((stage.group, stage.kind))
                if field is None or field.id not in self.record_kinds[
                    stage.record_kind_id
                ].available_field_kinds:
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' stages {stage.group}.{stage.kind}, "
                        f"which '{stage.record_kind_id}' does not offer."
                    )
                if not any(
                    attribute.semantic_type == "date"
                    for attribute in field.attributes
                ):
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' stages {stage.group}.{stage.kind}, "
                        "which has no date attribute."
                    )
                if isinstance(stage.order, bool) or not isinstance(stage.order, int):
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' stages {stage.group}.{stage.kind} "
                        "with a non-integer order."
                    )
            for record_id in pack.record_kind_ids:
                record = self.record_kinds[record_id]
                if any(
                    identifier_id not in pack.identifier_kind_ids
                    for identifier_id in record.primary_identifier_kinds
                ):
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' omits a primary identifier for '{record_id}'."
                    )
                if any(
                    field_id not in pack.field_kind_ids
                    for field_id in record.available_field_kinds
                ):
                    raise RegistryError(
                        f"Cycle pack '{pack.id}' omits a field for '{record_id}'."
                    )
            hashes[pack.id] = canonical_sha256(self._expanded_identity(pack.id))
        self._field_selectors = MappingProxyType(selectors)
        self._pack_hashes = MappingProxyType(hashes)

    def _expanded_identity(self, pack_id: str) -> dict:
        pack = self.packs[pack_id]
        return {
            "pack": pack.identity(),
            "normalizers": [
                self.normalizers[value].identity() for value in pack.normalizer_ids
            ],
            "identifier_kinds": [
                self.identifier_kinds[value].identity()
                for value in pack.identifier_kind_ids
            ],
            "field_kinds": [
                self.field_kinds[value].identity() for value in pack.field_kind_ids
            ],
            "record_kinds": [
                self.record_kinds[value].identity() for value in pack.record_kind_ids
            ],
        }

    def pack(self, pack_id: str) -> CyclePackDefinition:
        try:
            return self.packs[str(pack_id)]
        except KeyError as error:
            raise RegistryError(f"Unknown cycle pack '{pack_id}'.") from error

    def reference(self, pack_id: str) -> RegistryReference:
        pack = self.pack(pack_id)
        return RegistryReference(pack.id, pack.version, self._pack_hashes[pack.id])

    def validate_reference(self, value: object) -> RegistryReference:
        if not isinstance(value, dict):
            raise RegistryError("registry must be an object.")
        pack_id = str(value.get("pack_id") or "")
        expected = self.reference(pack_id)
        pack_version = value.get("pack_version")
        if isinstance(pack_version, bool) or not isinstance(pack_version, int):
            raise RegistryError(
                f"Registry reference for '{pack_id}' has an invalid pack version."
            )
        supplied = RegistryReference(
            pack_id=pack_id,
            pack_version=pack_version,
            definition_hash=str(value.get("definition_hash") or ""),
        )
        if supplied != expected:
            raise RegistryError(
                f"Registry reference for '{pack_id}' is stale or inconsistent."
            )
        return expected

    def _available(self, pack_id: str, value: str, field: str) -> bool:
        return value in getattr(self.pack(pack_id), field)

    def identifier_kind(self, pack_id: str, identifier_id: str) -> IdentifierKindDefinition:
        if not self._available(pack_id, identifier_id, "identifier_kind_ids"):
            raise RegistryError(
                f"Identifier kind '{identifier_id}' is not registered for '{pack_id}'."
            )
        return self.identifier_kinds[identifier_id]

    def record_kind(self, pack_id: str, record_id: str) -> RecordKindDefinition:
        if not self._available(pack_id, record_id, "record_kind_ids"):
            raise RegistryError(
                f"Record kind '{record_id}' is not registered for '{pack_id}'."
            )
        return self.record_kinds[record_id]

    def field_kind(self, pack_id: str, group: str, kind: str) -> FieldKindDefinition:
        definition = self._field_selectors.get((group, kind))
        if definition is None or not self._available(pack_id, definition.id, "field_kind_ids"):
            raise RegistryError(
                f"Field kind '{group}.{kind}' is not registered for '{pack_id}'."
            )
        return definition

    def date_lifecycle_order(
        self, pack_id: str, record_kind_id: str, group: str, kind: str
    ) -> int | None:
        """Return the cycle rank of one record kind's date field, if declared.

        A record kind from another pack raises rather than reporting no stage:
        an unordered field is a deliberate abstention, and a cross-pack
        reference silently reading as one would let a stale definition escape
        the direction rule instead of failing closed.
        """

        self.record_kind(pack_id, record_kind_id)
        for stage in self.pack(pack_id).date_lifecycle:
            if (stage.record_kind_id, stage.group, stage.kind) == (
                record_kind_id,
                group,
                kind,
            ):
                return stage.order
        return None

    def control_evidence_attribute(
        self, pack_id: str, group: str, kind: str, attribute: str
    ) -> bool:
        """Whether finding this attribute is itself evidence a control operated."""

        definition = self.field_kind(pack_id, group, kind)
        return any(
            item.id == attribute and item.control_evidence
            for item in definition.attributes
        )

    def normalize_identifier(self, pack_id: str, identifier_id: str, value: object) -> str:
        definition = self.identifier_kind(pack_id, identifier_id)
        normalized = self.normalizers[definition.normalizer_id].implementation(value)
        if not normalized:
            raise RegistryError("An identifier value must not be empty.")
        return normalized

    def metadata(self) -> dict:
        return {
            "packs": [
                {
                    **pack.identity(),
                    "definition_hash": self._pack_hashes[pack.id],
                    "identifier_kinds": [
                        self.identifier_kinds[value].identity()
                        for value in pack.identifier_kind_ids
                    ],
                    "field_kinds": [
                        self.field_kinds[value].identity()
                        for value in pack.field_kind_ids
                    ],
                    "record_kinds": [
                        self.record_kinds[value].identity()
                        for value in pack.record_kind_ids
                    ],
                }
                for pack in self.packs.values()
            ],
            "evidence_kinds": [
                definition.identity() for definition in self.evidence_kinds.values()
            ],
        }
