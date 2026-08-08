"""Immutable definitions for transaction-evidence registry packs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

SemanticType = Literal["identifier", "date", "number", "text", "boolean"]
EdgePolicy = Literal["transaction", "non_linking"]
RecordKindRequirement = Literal["required", "forbidden"]
Normalizer = Callable[[object], str]


@dataclass(frozen=True)
class NormalizerDefinition:
    id: str
    label: str
    implementation_version: str
    implementation: Normalizer = field(repr=False, compare=False)

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "implementation_version": self.implementation_version,
        }


@dataclass(frozen=True)
class IdentifierKindDefinition:
    id: str
    label: str
    edge_policy: EdgePolicy
    value_type: SemanticType
    normalizer_id: str

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "edge_policy": self.edge_policy,
            "value_type": self.value_type,
            "normalizer_id": self.normalizer_id,
        }


@dataclass(frozen=True)
class FieldAttributeDefinition:
    id: str
    semantic_type: SemanticType
    #: Whether the value is copied from the record or characterizes it. A
    #: verbatim attribute must appear in the excerpt that supports it; an
    #: interpretive one — the *role* a named party plays, the *decision* a
    #: signature block represents — is the worker's reading of the record and
    #: routinely uses a word the record never prints. Demanding a quote for
    #: those is unsatisfiable, and a response cannot be repaired into one.
    verbatim: bool = True

    def identity(self) -> dict:
        return {
            "id": self.id,
            "semantic_type": self.semantic_type,
            "verbatim": self.verbatim,
        }


@dataclass(frozen=True)
class FieldKindDefinition:
    id: str
    label: str
    group: str
    kind: str
    attributes: tuple[FieldAttributeDefinition, ...]

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "attributes": [attribute.identity() for attribute in self.attributes],
        }


@dataclass(frozen=True)
class RecordKindDefinition:
    id: str
    label: str
    primary_identifier_kinds: tuple[str, ...]
    available_field_kinds: tuple[str, ...]
    bindable: bool = True

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "primary_identifier_kinds": list(self.primary_identifier_kinds),
            "available_field_kinds": list(self.available_field_kinds),
            "bindable": self.bindable,
        }


@dataclass(frozen=True)
class EvidenceKindDefinition:
    id: str
    label: str
    record_kind_requirement: RecordKindRequirement

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "record_kind_requirement": self.record_kind_requirement,
        }


@dataclass(frozen=True)
class CyclePackDefinition:
    id: str
    label: str
    version: int
    normalizer_ids: tuple[str, ...]
    identifier_kind_ids: tuple[str, ...]
    field_kind_ids: tuple[str, ...]
    record_kind_ids: tuple[str, ...]

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "version": self.version,
            "normalizer_ids": list(self.normalizer_ids),
            "identifier_kind_ids": list(self.identifier_kind_ids),
            "field_kind_ids": list(self.field_kind_ids),
            "record_kind_ids": list(self.record_kind_ids),
        }


@dataclass(frozen=True)
class RegistryReference:
    pack_id: str
    pack_version: int
    definition_hash: str

    def to_dict(self) -> dict:
        return {
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "definition_hash": self.definition_hash,
        }

