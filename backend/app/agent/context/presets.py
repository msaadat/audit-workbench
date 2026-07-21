"""Normalized context preset and selector registries.

This module validates declarations only.  Selector execution and context
materialization belong to later resolver tasks; registry entries therefore
carry stable implementation identities without invoking their implementations.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable

from .model import (
    AutoSelect,
    ContextBudget,
    ContextPrivacy,
    ContextRepresentation,
    ContextSelector,
    ContextSource,
    ContextSpec,
)


_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SELECTOR_KINDS = {ContextSelector.kind, AutoSelect.kind}

# Representation permissions are intentionally structural.  A declaration
# cannot make sensitive content permissible merely by naming it differently.
_REPRESENTATION_PRIVACY_FIELD = {
    "excerpt": "allow_document_text",
    "raw_pages": "allow_document_text",
    "summary": "allow_document_text",
    "table_metadata": "allow_table_metadata",
    "table_profile": "allow_table_profiles",
    "table_aggregate": "allow_table_aggregates",
    "table_rows": "allow_table_rows",
}


def _normalized_id(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string.")
    return text


def _content_hash(payload: object, field_name: str) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} is unhashable.") from error
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _implementation_hash(identity: str) -> str:
    return f"sha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class SelectorDefinition:
    """Hash-identified metadata for one registered local selector."""

    selector_id: str
    selector_kind: str
    supported_source_types: tuple[str, ...]
    implementation_hash: str
    configuration_keys: tuple[str, ...] = ()
    required_configuration_keys: tuple[str, ...] = ()
    tie_breaker: str = "source_ref_ascending"
    emits_reasons: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selector_id",
            _normalized_id(self.selector_id, "selector_definition.selector_id"),
        )
        if self.selector_kind not in _SELECTOR_KINDS:
            raise ValueError(
                "selector_definition.selector_kind must be 'deterministic' or 'auto'."
            )
        source_types = tuple(
            _normalized_id(value, "selector_definition.supported_source_types")
            for value in self.supported_source_types
        )
        if not source_types:
            raise ValueError(
                "selector_definition.supported_source_types must not be empty."
            )
        if len(source_types) != len(set(source_types)):
            raise ValueError(
                "selector_definition.supported_source_types must be unique."
            )
        object.__setattr__(self, "supported_source_types", source_types)
        if not isinstance(self.implementation_hash, str) or not _HASH_PATTERN.fullmatch(
            self.implementation_hash
        ):
            raise ValueError(
                f"Selector definition '{self.selector_id}' is unhashable: "
                "implementation_hash must be a sha256 hash."
            )
        configuration_keys = tuple(
            _normalized_id(value, "selector_definition.configuration_keys")
            for value in self.configuration_keys
        )
        required_keys = tuple(
            _normalized_id(value, "selector_definition.required_configuration_keys")
            for value in self.required_configuration_keys
        )
        if len(configuration_keys) != len(set(configuration_keys)):
            raise ValueError("selector_definition.configuration_keys must be unique.")
        if len(required_keys) != len(set(required_keys)):
            raise ValueError(
                "selector_definition.required_configuration_keys must be unique."
            )
        if not set(required_keys).issubset(configuration_keys):
            raise ValueError(
                "selector_definition.required_configuration_keys must be declared."
            )
        object.__setattr__(self, "configuration_keys", configuration_keys)
        object.__setattr__(self, "required_configuration_keys", required_keys)
        object.__setattr__(
            self,
            "tie_breaker",
            _normalized_id(self.tie_breaker, "selector_definition.tie_breaker"),
        )
        if not isinstance(self.emits_reasons, bool):
            raise ValueError("selector_definition.emits_reasons must be a boolean.")

    def to_dict(self) -> dict[str, object]:
        return {
            "selector_id": self.selector_id,
            "selector_kind": self.selector_kind,
            "supported_source_types": list(self.supported_source_types),
            "implementation_hash": self.implementation_hash,
            "configuration_keys": list(self.configuration_keys),
            "required_configuration_keys": list(self.required_configuration_keys),
            "tie_breaker": self.tie_breaker,
            "emits_reasons": self.emits_reasons,
        }

    @property
    def definition_hash(self) -> str:
        return _content_hash(self.to_dict(), f"Selector definition '{self.selector_id}'")


class SelectorRegistry:
    """Registry and construction-time policy gate for context selectors."""

    def __init__(self) -> None:
        self._definitions: dict[str, SelectorDefinition] = {}

    def register(self, definition: SelectorDefinition) -> SelectorDefinition:
        if not isinstance(definition, SelectorDefinition):
            raise ValueError("Selector registry entries must be SelectorDefinition values.")
        if definition.selector_id in self._definitions:
            raise ValueError(
                f"Selector '{definition.selector_id}' is already registered."
            )
        # Force construction of the canonical identity before accepting an
        # entry.  Invalid definitions never partially enter the registry.
        definition.definition_hash
        self._definitions[definition.selector_id] = definition
        return definition

    def get(self, selector_id: str) -> SelectorDefinition:
        try:
            return self._definitions[selector_id]
        except KeyError as error:
            raise ValueError(f"Unknown context selector '{selector_id}'.") from error

    def all(self) -> tuple[SelectorDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def validate_spec(self, spec: ContextSpec) -> ContextSpec:
        if not isinstance(spec, ContextSpec):
            raise ValueError("Context declaration must be a ContextSpec.")
        _validate_privacy(spec)
        for source in spec.sources:
            self.validate_source(source)
        return spec

    def validate_source(self, source: ContextSource) -> SelectorDefinition:
        definition = self.get(source.selector.selector_id)
        if source.selector.kind != definition.selector_kind:
            raise ValueError(
                f"Selector '{definition.selector_id}' is registered as "
                f"'{definition.selector_kind}', not '{source.selector.kind}'."
            )
        if source.source_type not in definition.supported_source_types:
            raise ValueError(
                f"Selector '{definition.selector_id}' does not support source type "
                f"'{source.source_type}'."
            )
        keys = set(source.selector.configuration)
        unknown = sorted(keys - set(definition.configuration_keys))
        if unknown:
            raise ValueError(
                f"Unknown configuration key '{unknown[0]}' for selector "
                f"'{definition.selector_id}'."
            )
        missing = sorted(set(definition.required_configuration_keys) - keys)
        if missing:
            raise ValueError(
                f"Selector '{definition.selector_id}' requires configuration key "
                f"'{missing[0]}'."
            )
        if isinstance(source.selector, AutoSelect):
            if source.selector.item_limit > source.budget.max_items:
                raise ValueError(
                    f"Selector '{definition.selector_id}' item limit exceeds source "
                    f"'{source.id}' budget."
                )
            if not definition.emits_reasons:
                raise ValueError(
                    f"Automatic selector '{definition.selector_id}' must emit reasons."
                )
        return definition


@dataclass(frozen=True)
class ContextPreset:
    """One concise preset compiled to a normalized context declaration."""

    preset_id: str
    spec: ContextSpec

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "preset_id",
            _normalized_id(self.preset_id, "context_preset.preset_id"),
        )
        if not isinstance(self.spec, ContextSpec):
            raise ValueError("context_preset.spec must be a ContextSpec.")

    @property
    def definition_hash(self) -> str:
        return _content_hash(
            {"preset_id": self.preset_id, "spec": self.spec.to_dict()},
            f"Context preset '{self.preset_id}'",
        )


class PresetRegistry:
    """Registry that exposes only validated, normalized preset specs."""

    def __init__(self, selectors: SelectorRegistry) -> None:
        self._selectors = selectors
        self._presets: dict[str, ContextPreset] = {}

    def register(self, preset: ContextPreset) -> ContextPreset:
        if not isinstance(preset, ContextPreset):
            raise ValueError("Preset registry entries must be ContextPreset values.")
        if preset.preset_id in self._presets:
            raise ValueError(f"Context preset '{preset.preset_id}' is already registered.")
        self._selectors.validate_spec(preset.spec)
        preset.definition_hash
        self._presets[preset.preset_id] = preset
        return preset

    def get(self, preset_id: str) -> ContextPreset:
        try:
            return self._presets[preset_id]
        except KeyError as error:
            raise ValueError(f"Unknown context preset '{preset_id}'.") from error

    def compile(self, preset_id: str) -> ContextSpec:
        # Round-tripping prevents callers from retaining mutable nested option
        # dictionaries from the registry's canonical definition.
        return ContextSpec.from_json(self.get(preset_id).spec.to_json())

    def all(self) -> tuple[ContextPreset, ...]:
        return tuple(self._presets[key] for key in sorted(self._presets))


def _validate_privacy(spec: ContextSpec) -> None:
    privacy = spec.privacy
    if not privacy.allow_provider and spec.sources:
        raise ValueError(
            "Invalid context privacy: provider delivery is disabled for a non-empty spec."
        )
    if privacy.allow_table_rows:
        raise ValueError(
            "Invalid context privacy: row-level table data cannot be sent to the provider."
        )
    for source in spec.sources:
        for representation in source.representations:
            permission = _REPRESENTATION_PRIVACY_FIELD.get(representation.kind)
            if permission is not None and not getattr(privacy, permission):
                raise ValueError(
                    "Invalid context privacy: representation "
                    f"'{representation.kind}' for source '{source.id}' requires "
                    f"privacy.{permission}."
                )


def _register_selectors(
    registry: SelectorRegistry,
    definitions: Iterable[SelectorDefinition],
) -> None:
    for definition in definitions:
        registry.register(definition)


SELECTORS = SelectorRegistry()
_register_selectors(
    SELECTORS,
    (
        SelectorDefinition(
            selector_id="documents.by_category",
            selector_kind="deterministic",
            supported_source_types=("documents",),
            implementation_hash=_implementation_hash(
                "documents.by_category:metadata-category-equality:source-ref-ascending"
            ),
            configuration_keys=("category",),
            required_configuration_keys=("category",),
        ),
        SelectorDefinition(
            selector_id="documents.lexical",
            selector_kind="auto",
            supported_source_types=("documents",),
            implementation_hash=_implementation_hash(
                "documents.lexical:stable-local-lexical-score:source-ref-ascending"
            ),
            configuration_keys=("category", "query_fields"),
        ),
        SelectorDefinition(
            selector_id="methodology.explicit_refs",
            selector_kind="deterministic",
            supported_source_types=("methodology",),
            implementation_hash=_implementation_hash(
                "methodology.explicit_refs:declared-reference-order"
            ),
            configuration_keys=("refs",),
            required_configuration_keys=("refs",),
            tie_breaker="declared_reference_order",
        ),
    ),
)


PRESETS = PresetRegistry(SELECTORS)
PRESETS.register(
    ContextPreset(
        preset_id="documents.policies",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="policy_documents",
                    source_type="documents",
                    required=False,
                    selector=ContextSelector(
                        selector_id="documents.by_category",
                        configuration={"category": "policy"},
                    ),
                    representations=(ContextRepresentation("excerpt"),),
                    budget=ContextBudget(max_items=12, max_characters=40_000),
                ),
            ),
            budget=ContextBudget(max_items=12, max_characters=40_000),
            privacy=ContextPrivacy(allow_document_text=True),
        ),
    )
)


__all__ = [
    "ContextPreset",
    "PRESETS",
    "PresetRegistry",
    "SELECTORS",
    "SelectorDefinition",
    "SelectorRegistry",
]
