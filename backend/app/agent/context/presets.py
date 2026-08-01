"""Normalized, application-authored context preset and selector registries.

Selector implementations are a closed set of local strategies implemented by
the resolver. Registry entries carry stable implementation identities without
accepting executable callables or service dependencies. Registrations define
the declaration-only context policy; they are not workspace or auditor
overrides.
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
_SELECTOR_STRATEGIES = {"metadata", "lexical", "local_embedding"}
_STABLE_TIE_BREAKERS = {"source_ref_ascending", "declared_reference_order"}

# Representation permissions are intentionally structural.  A declaration
# cannot make sensitive content permissible merely by naming it differently.
_REPRESENTATION_PRIVACY_FIELD = {
    "planning_context": "allow_planning_context",
    "artifact_template": "allow_template_text",
    "current_artifact": "allow_document_text",
    "excerpt": "allow_document_text",
    "raw_pages": "allow_document_text",
    "summary": "allow_document_text",
    "derived_text": "allow_document_text",
    "vision_transcript": "allow_document_text",
    "page_image": "allow_document_images",
    "file_metadata": "allow_file_metadata",
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
    strategy: str = "metadata"
    configuration_keys: tuple[str, ...] = ()
    required_configuration_keys: tuple[str, ...] = ()
    tie_breaker: str = "source_ref_ascending"
    emits_reasons: bool = True
    local_embedding_model_hash: str | None = None
    local_embedding_index_hash: str | None = None

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
        if self.strategy not in _SELECTOR_STRATEGIES:
            raise ValueError(
                "selector_definition.strategy must be 'metadata', 'lexical', or "
                "'local_embedding'."
            )
        if self.selector_kind == ContextSelector.kind and self.strategy != "metadata":
            raise ValueError(
                "Deterministic selectors support only the metadata strategy."
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
        if self.tie_breaker not in _STABLE_TIE_BREAKERS:
            raise ValueError(
                "selector_definition.tie_breaker must be a registered stable "
                "tie-breaker."
            )
        if self.strategy in {"lexical", "local_embedding"} and (
            self.tie_breaker != "source_ref_ascending"
        ):
            raise ValueError(
                "Lexical and local-embedding selectors must use the stable "
                "source_ref_ascending tie-breaker."
            )
        if self.tie_breaker == "declared_reference_order" and (
            "refs" not in required_keys
        ):
            raise ValueError(
                "declared_reference_order requires the selector's refs configuration."
            )
        if "refs" in configuration_keys and (
            self.tie_breaker != "declared_reference_order"
        ):
            raise ValueError(
                "Selectors configured with refs must use declared_reference_order."
            )
        if not isinstance(self.emits_reasons, bool):
            raise ValueError("selector_definition.emits_reasons must be a boolean.")
        embedding_hashes = (
            self.local_embedding_model_hash,
            self.local_embedding_index_hash,
        )
        if self.strategy == "local_embedding":
            if self.selector_kind != AutoSelect.kind:
                raise ValueError(
                    "Local-embedding selectors must be bounded automatic selectors."
                )
            if any(
                not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value)
                for value in embedding_hashes
            ):
                raise ValueError(
                    "Local-embedding selectors require sha256 model and index hashes."
                )
        elif any(value is not None for value in embedding_hashes):
            raise ValueError(
                "Only local-embedding selectors may declare model or index hashes."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "selector_id": self.selector_id,
            "selector_kind": self.selector_kind,
            "supported_source_types": list(self.supported_source_types),
            "implementation_hash": self.implementation_hash,
            "strategy": self.strategy,
            "configuration_keys": list(self.configuration_keys),
            "required_configuration_keys": list(self.required_configuration_keys),
            "tie_breaker": self.tie_breaker,
            "emits_reasons": self.emits_reasons,
            "local_embedding_model_hash": self.local_embedding_model_hash,
            "local_embedding_index_hash": self.local_embedding_index_hash,
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
            if permission is None:
                raise ValueError(
                    "Invalid context privacy: representation "
                    f"'{representation.kind}' for source '{source.id}' is not registered; "
                    "representations are denied by default."
                )
            if not getattr(privacy, permission):
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
            selector_id="planning.current",
            selector_kind="deterministic",
            supported_source_types=("planning",),
            implementation_hash=_implementation_hash(
                "planning.current:metadata-all:source-ref-ascending"
            ),
            strategy="metadata",
        ),
        SelectorDefinition(
            selector_id="templates.current",
            selector_kind="deterministic",
            supported_source_types=("templates",),
            implementation_hash=_implementation_hash(
                "templates.current:metadata-all:source-ref-ascending"
            ),
            strategy="metadata",
        ),
        SelectorDefinition(
            selector_id="artifacts.current",
            selector_kind="deterministic",
            supported_source_types=("artifacts",),
            implementation_hash=_implementation_hash(
                "artifacts.current:metadata-all:source-ref-ascending"
            ),
            strategy="metadata",
        ),
        SelectorDefinition(
            selector_id="documents.by_category",
            selector_kind="deterministic",
            supported_source_types=("documents",),
            implementation_hash=_implementation_hash(
                "documents.by_category:metadata-category-equality:source-ref-ascending"
            ),
            strategy="metadata",
            configuration_keys=("category",),
            required_configuration_keys=("category",),
        ),
        SelectorDefinition(
            selector_id="documents.planning_relevant",
            selector_kind="deterministic",
            supported_source_types=("documents",),
            implementation_hash=_implementation_hash(
                "documents.planning_relevant:metadata-flag-equality:"
                "source-ref-ascending"
            ),
            strategy="metadata",
            configuration_keys=("planning_relevant",),
            required_configuration_keys=("planning_relevant",),
        ),
        SelectorDefinition(
            selector_id="documents.all",
            selector_kind="deterministic",
            supported_source_types=("documents",),
            implementation_hash=_implementation_hash(
                "documents.all:metadata-all:source-ref-ascending"
            ),
            strategy="metadata",
        ),
        SelectorDefinition(
            selector_id="documents.lexical",
            selector_kind="auto",
            supported_source_types=("documents",),
            implementation_hash=_implementation_hash(
                "documents.lexical:stable-local-lexical-score:source-ref-ascending"
            ),
            strategy="lexical",
            configuration_keys=("category", "planning_relevant", "query_fields"),
        ),
        SelectorDefinition(
            selector_id="methodology.explicit_refs",
            selector_kind="deterministic",
            supported_source_types=("methodology",),
            implementation_hash=_implementation_hash(
                "methodology.explicit_refs:declared-reference-order"
            ),
            strategy="metadata",
            configuration_keys=("refs",),
            required_configuration_keys=("refs",),
            tie_breaker="declared_reference_order",
        ),
        SelectorDefinition(
            selector_id="methodology.lexical",
            selector_kind="auto",
            supported_source_types=("methodology",),
            implementation_hash=_implementation_hash(
                "methodology.lexical:stable-local-lexical-score:source-ref-ascending"
            ),
            strategy="lexical",
            configuration_keys=("query_fields", "scope"),
        ),
        SelectorDefinition(
            selector_id="intake.staged_files",
            selector_kind="deterministic",
            supported_source_types=("staged_files",),
            implementation_hash=_implementation_hash(
                "intake.staged_files:metadata-all:source-ref-ascending"
            ),
            strategy="metadata",
        ),
        SelectorDefinition(
            selector_id="tables.all",
            selector_kind="deterministic",
            supported_source_types=("tables",),
            implementation_hash=_implementation_hash(
                "tables.all:metadata-all:source-ref-ascending"
            ),
            strategy="metadata",
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
PRESETS.register(
    ContextPreset(
        preset_id="planning.context",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="current_planning_context",
                    source_type="planning",
                    required=False,
                    selector=ContextSelector(selector_id="planning.current"),
                    representations=(ContextRepresentation("planning_context"),),
                    budget=ContextBudget(max_items=1, max_characters=10_000),
                ),
                ContextSource(
                    id="planning_documents",
                    source_type="documents",
                    required=True,
                    # Planning relevance is a deterministic category rule, not a
                    # model judgment and not a lexical score: at this point there
                    # is no stated objective or scope to score against, which is
                    # exactly the context this capability produces. Explicit
                    # auditor curation overrides the rule in the adapter.
                    selector=ContextSelector(
                        selector_id="documents.planning_relevant",
                        configuration={"planning_relevant": True},
                    ),
                    # A document with a current analysis contributes its bounded
                    # ``summary``; one without contributes its bounded leading
                    # ``raw_pages``, because this capability is what produces the
                    # objective and scope a retrieval query would need.
                    representations=(
                        ContextRepresentation("summary"),
                        ContextRepresentation("raw_pages"),
                    ),
                    budget=ContextBudget(max_items=8, max_characters=40_000),
                ),
            ),
            budget=ContextBudget(max_items=9, max_characters=50_000),
            privacy=ContextPrivacy(
                allow_planning_context=True,
                allow_document_text=True,
            ),
        ),
    )
)
PRESETS.register(
    ContextPreset(
        preset_id="planning.apm",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="planning_context",
                    source_type="planning",
                    required=True,
                    selector=ContextSelector(selector_id="planning.current"),
                    representations=(ContextRepresentation("planning_context"),),
                    budget=ContextBudget(max_items=1, max_characters=10_000),
                ),
                ContextSource(
                    id="apm_template",
                    source_type="templates",
                    required=True,
                    selector=ContextSelector(selector_id="templates.current"),
                    representations=(ContextRepresentation("artifact_template"),),
                    budget=ContextBudget(max_items=1, max_characters=16_000),
                ),
                ContextSource(
                    id="current_apm",
                    source_type="artifacts",
                    required=False,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=20_000),
                ),
                ContextSource(
                    id="table_metadata",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=12, max_characters=8_000),
                ),
                ContextSource(
                    id="table_profiles",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_profile"),),
                    budget=ContextBudget(max_items=12, max_characters=16_000),
                ),
                ContextSource(
                    id="documents",
                    source_type="documents",
                    required=False,
                    selector=AutoSelect(
                        selector_id="documents.lexical",
                        item_limit=12,
                        # Planning material only: the same declared category rule
                        # the planning-context capability uses keeps transaction
                        # vouchers and raw evidence out of a planning turn.
                        configuration={
                            "query_fields": ["apm_query"],
                            "planning_relevant": True,
                        },
                    ),
                    # A current analysis contributes ``summary``; a document
                    # without one still grounds the turn through locally
                    # retrieved ``excerpt`` passages.
                    representations=(
                        ContextRepresentation("summary"),
                        ContextRepresentation("excerpt"),
                    ),
                    budget=ContextBudget(max_items=12, max_characters=40_000),
                ),
                ContextSource(
                    id="methodology",
                    source_type="methodology",
                    required=False,
                    selector=AutoSelect(
                        selector_id="methodology.lexical",
                        item_limit=5,
                        configuration={"query_fields": ["apm_query"]},
                    ),
                    representations=(ContextRepresentation("excerpt"),),
                    budget=ContextBudget(max_items=5, max_characters=8_000),
                ),
            ),
            budget=ContextBudget(max_items=44, max_characters=70_000),
            privacy=ContextPrivacy(
                allow_planning_context=True,
                allow_template_text=True,
                allow_document_text=True,
                allow_table_metadata=True,
                allow_table_profiles=True,
            ),
        ),
    )
)
PRESETS.register(
    ContextPreset(
        preset_id="planning.rcm",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="planning_context",
                    source_type="planning",
                    required=True,
                    selector=ContextSelector(selector_id="planning.current"),
                    representations=(ContextRepresentation("planning_context"),),
                    budget=ContextBudget(max_items=1, max_characters=10_000),
                ),
                ContextSource(
                    id="rcm_template",
                    source_type="templates",
                    required=True,
                    selector=ContextSelector(selector_id="templates.current"),
                    representations=(ContextRepresentation("artifact_template"),),
                    budget=ContextBudget(max_items=1, max_characters=16_000),
                ),
                ContextSource(
                    id="current_apm",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=24_000),
                ),
                ContextSource(
                    id="current_rcm",
                    source_type="artifacts",
                    required=False,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=200, max_characters=40_000),
                ),
                ContextSource(
                    id="table_metadata",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=12, max_characters=8_000),
                ),
                ContextSource(
                    id="table_profiles",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_profile"),),
                    budget=ContextBudget(max_items=12, max_characters=16_000),
                ),
                ContextSource(
                    id="documents",
                    source_type="documents",
                    required=False,
                    selector=AutoSelect(
                        selector_id="documents.lexical",
                        item_limit=12,
                        # Planning material only: the same declared category rule
                        # the planning-context capability uses keeps transaction
                        # vouchers and raw evidence out of a planning turn.
                        configuration={
                            "query_fields": ["rcm_query"],
                            "planning_relevant": True,
                        },
                    ),
                    # A current analysis contributes ``summary``; a document
                    # without one still grounds the turn through locally
                    # retrieved ``excerpt`` passages.
                    representations=(
                        ContextRepresentation("summary"),
                        ContextRepresentation("excerpt"),
                    ),
                    budget=ContextBudget(max_items=12, max_characters=40_000),
                ),
                ContextSource(
                    id="methodology",
                    source_type="methodology",
                    required=False,
                    selector=AutoSelect(
                        selector_id="methodology.lexical",
                        item_limit=5,
                        configuration={"query_fields": ["rcm_query"]},
                    ),
                    representations=(ContextRepresentation("excerpt"),),
                    budget=ContextBudget(max_items=5, max_characters=8_000),
                ),
            ),
            budget=ContextBudget(max_items=244, max_characters=100_000),
            privacy=ContextPrivacy(
                allow_planning_context=True,
                allow_template_text=True,
                allow_document_text=True,
                allow_table_metadata=True,
                allow_table_profiles=True,
            ),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="tests.generate",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="planning_context",
                    source_type="planning",
                    required=True,
                    selector=ContextSelector(selector_id="planning.current"),
                    representations=(ContextRepresentation("planning_context"),),
                    budget=ContextBudget(max_items=1, max_characters=10_000),
                ),
                ContextSource(
                    id="rcm_row",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=16_000),
                ),
                ContextSource(
                    id="other_rcm_rows",
                    source_type="artifacts",
                    required=False,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=150, max_characters=16_000),
                ),
                ContextSource(
                    id="table_metadata",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=12, max_characters=12_000),
                ),
                ContextSource(
                    id="documents",
                    source_type="documents",
                    required=False,
                    # The single document source for the merged capability: every
                    # candidate document with its citation identity, not the
                    # planning-relevant-filtered set ``tests.draft`` used. A
                    # Document Test step must be able to name transaction-level
                    # evidence a planning-relevant filter would withhold.
                    selector=AutoSelect(
                        selector_id="documents.lexical",
                        item_limit=12,
                        configuration={"query_fields": ["test_generate_query"]},
                    ),
                    representations=(
                        ContextRepresentation("summary"),
                        ContextRepresentation("excerpt"),
                    ),
                    budget=ContextBudget(max_items=12, max_characters=26_000),
                ),
                ContextSource(
                    id="methodology",
                    source_type="methodology",
                    required=False,
                    selector=AutoSelect(
                        selector_id="methodology.lexical",
                        item_limit=5,
                        configuration={"query_fields": ["test_generate_query"]},
                    ),
                    representations=(ContextRepresentation("excerpt"),),
                    budget=ContextBudget(max_items=5, max_characters=8_000),
                ),
            ),
            # Data Test code is validated against schema-only empty frames, so
            # profiles are not needed to generate a valid executable procedure.
            # Keep the overall ceiling aligned with the remaining source limits.
            budget=ContextBudget(max_items=181, max_characters=88_000),
            privacy=ContextPrivacy(
                allow_planning_context=True,
                allow_document_text=True,
                allow_table_metadata=True,
            ),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="fieldwork.document_qa",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="qa_item",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=4_000),
                ),
                ContextSource(
                    id="document_pages",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    # One page per item, so the pages an auditor scoped are
                    # ``raw_pages`` and an unscoped question's retrieved passages
                    # are ``excerpt``. The declaration is the same either way.
                    representations=(
                        ContextRepresentation("raw_pages"),
                        ContextRepresentation("excerpt"),
                    ),
                    budget=ContextBudget(max_items=60, max_characters=26_000),
                ),
            ),
            # The former inline path refused a question whose pages exceeded a
            # 30,000-character workflow budget. The declared resolver instead
            # omits the pages that do not fit and records the omission, and the
            # worker binds every citation to a page it was actually supplied, so
            # a bounded answer stays grounded rather than failing the unit.
            budget=ContextBudget(max_items=61, max_characters=30_000),
            privacy=ContextPrivacy(allow_document_text=True),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="reporting.finding_draft",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="observation",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=6_000),
                ),
                ContextSource(
                    id="rcm_row",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=4_000),
                ),
                ContextSource(
                    id="test",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=6_000),
                ),
                ContextSource(
                    id="execution_result",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=12_000),
                ),
            ),
            budget=ContextBudget(max_items=4, max_characters=16_000),
            # A finding is grounded only in its exception observation and the
            # immutable execution result behind it; no document or table content
            # is declared.
            privacy=ContextPrivacy(allow_document_text=True),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="analysis.definitions",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="target_schema",
                    source_type="tables",
                    required=True,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=1, max_characters=8_000),
                ),
                ContextSource(
                    id="target_profile",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_profile"),),
                    budget=ContextBudget(max_items=1, max_characters=16_000),
                ),
                ContextSource(
                    id="target_aggregates",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # Bounded local aggregates — group counts and numeric totals
                    # over low-cardinality columns. Aggregates are the only
                    # value-derived class this preset permits, and never a row.
                    representations=(ContextRepresentation("table_aggregate"),),
                    budget=ContextBudget(max_items=8, max_characters=12_000),
                ),
                ContextSource(
                    id="related_frames",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=8, max_characters=8_000),
                ),
                ContextSource(
                    id="relationship_evidence",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # The deterministic join diagnostics for frames related to
                    # the target: match rates, key uniqueness, and row-count
                    # effects. Aggregate metrics only — this is the same evidence
                    # the relationship capability recorded, not a re-derivation
                    # the model is asked to make.
                    representations=(ContextRepresentation("table_aggregate"),),
                    budget=ContextBudget(max_items=8, max_characters=8_000),
                ),
                ContextSource(
                    id="analytics_registry",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=20_000),
                ),
                ContextSource(
                    id="current_analyses",
                    source_type="artifacts",
                    required=False,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=20, max_characters=12_000),
                ),
            ),
            budget=ContextBudget(max_items=47, max_characters=60_000),
            # Row-level table data is structurally impossible here: the
            # permission is denied and ``table_rows`` is rejected by the
            # resolver boundary before a candidate can become a bundle item.
            privacy=ContextPrivacy(
                allow_document_text=True,
                allow_table_metadata=True,
                allow_table_profiles=True,
                allow_table_aggregates=True,
            ),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="documents.analysis_chunk",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="document_metadata",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=2_000),
                ),
                ContextSource(
                    id="document_chunk",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    # Exactly one bounded chunk of the document's own text. The
                    # per-source budget is above ``ANALYSIS_CHUNK_CHARACTERS`` on
                    # purpose: a chunk is the unit of evidence a citation binds
                    # to, so supplying a truncated one would let the worker cite
                    # text it never saw in full.
                    representations=(ContextRepresentation("raw_pages"),),
                    budget=ContextBudget(max_items=1, max_characters=32_000),
                ),
            ),
            budget=ContextBudget(max_items=2, max_characters=34_000),
            privacy=ContextPrivacy(allow_document_text=True),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        # Deliberately *narrower* than ``documents.analysis_chunk``. This profile
        # extracts transaction identifiers from the record's own text, and the
        # standard metadata projection carries the original filename — which for
        # a voucher pack contains those same identifiers. Supplying it would let
        # a worker report a value it never read from the document body, so this
        # preset declares bare identity instead: enough to bind a citation to its
        # source hash, and nothing a field could be lifted from.
        preset_id="documents.analysis_voucher",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="document_identity",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=500),
                ),
                ContextSource(
                    id="document_chunk",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    representations=(ContextRepresentation("raw_pages"),),
                    budget=ContextBudget(max_items=1, max_characters=32_000),
                ),
            ),
            budget=ContextBudget(max_items=2, max_characters=34_000),
            privacy=ContextPrivacy(allow_document_text=True),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="documents.analysis_visual_page",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="document_metadata",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=2_000),
                ),
                ContextSource(
                    id="document_page_images",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    representations=(ContextRepresentation("page_image"),),
                    budget=ContextBudget(
                        max_items=4,
                        max_characters=1,
                        max_media_items=4,
                        max_media_bytes=12 * 1024 * 1024,
                        max_media_pixels=12_000_000,
                        max_estimated_image_tokens=4 * 4_096,
                    ),
                ),
            ),
            budget=ContextBudget(
                max_items=5,
                max_characters=2_000,
                max_estimated_tokens=1_000,
                max_media_items=4,
                max_media_bytes=12 * 1024 * 1024,
                max_media_pixels=12_000_000,
                max_estimated_image_tokens=4 * 4_096,
            ),
            privacy=ContextPrivacy(
                allow_document_text=True,
                allow_document_images=True,
            ),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="documents.analysis_reduction",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="document_metadata",
                    source_type="documents",
                    required=True,
                    selector=ContextSelector(selector_id="documents.all"),
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=1, max_characters=2_000),
                ),
                ContextSource(
                    id="chunk_analyses",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    # Generated chunk analyses only. The reduction is declared
                    # with no raw-source representation at all, which is what
                    # makes "you receive no raw source" a policy the resolver
                    # enforces rather than a prompt instruction.
                    representations=(ContextRepresentation("summary"),),
                    budget=ContextBudget(max_items=200, max_characters=60_000),
                ),
            ),
            budget=ContextBudget(max_items=201, max_characters=62_000),
            privacy=ContextPrivacy(allow_document_text=True),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="intake.classification",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="staged_files",
                    source_type="staged_files",
                    required=True,
                    selector=ContextSelector(selector_id="intake.staged_files"),
                    # Technical metadata only. There is no representation here
                    # for spreadsheet cells, row previews, formulas, comments, or
                    # extracted document text, so "the classifier sees no file
                    # content" is a policy the resolver enforces structurally
                    # rather than a promise the prompt makes.
                    representations=(ContextRepresentation("file_metadata"),),
                    budget=ContextBudget(max_items=500, max_characters=120_000),
                ),
            ),
            budget=ContextBudget(max_items=500, max_characters=120_000),
            privacy=ContextPrivacy(allow_file_metadata=True),
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
