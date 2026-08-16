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
    # Derived strictly by aggregating table profiles, so it is the same content
    # class under a different shape and travels under the same permission. A
    # context permitted to see per-table ranges is permitted to see their union.
    "population_summary": "allow_table_profiles",
    "table_aggregate": "allow_table_aggregates",
    "table_rows": "allow_table_rows",
    "table_rows_small": "allow_small_table_rows",
    "analysis_result": "allow_analysis_results",
    "analysis_exception_rows": "allow_analysis_exception_rows",
    "datatest_exception_rows": "allow_datatest_exception_rows",
    "analysis_summary": "allow_analysis_summary",
    "value_domain": "allow_value_domains",
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
    #: Whether a scoring strategy ranks the candidate set or filters it. A
    #: document that shares no term with the query is not evidence and is
    #: rightly dropped. A *table* that shares none is still a population the
    #: turn may have to test, and dropping it empties the schema list, which
    #: is the one input that decides whether a data test can be written at
    #: all. Where this is set, unmatched candidates keep their place at the
    #: tail in source-ref order rather than leaving the result.
    retain_unmatched: bool = False
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
        if self.retain_unmatched and self.strategy == "metadata":
            raise ValueError(
                "selector_definition.retain_unmatched applies only to a scoring "
                "strategy; a metadata selector matches or does not."
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
        SelectorDefinition(
            # ``tables.all`` fills a budgeted source in source-ref order, which
            # is alphabetical. On a workspace whose join frames sort early and
            # are several times wider than a base table, the character budget
            # is spent before the populations an RCM row is actually about are
            # reached: a vendor-master row was offered seven frames, none of
            # them the vendor master. Ranking the same candidates against the
            # row spends the budget on what the row names instead.
            selector_id="tables.lexical",
            selector_kind="auto",
            supported_source_types=("tables",),
            implementation_hash=_implementation_hash(
                "tables.lexical:stable-local-lexical-score:retain-unmatched"
                ":source-ref-ascending"
            ),
            strategy="lexical",
            configuration_keys=("query_fields",),
            required_configuration_keys=("query_fields",),
            retain_unmatched=True,
        ),
        SelectorDefinition(
            selector_id="analyses.all",
            selector_kind="deterministic",
            supported_source_types=("analyses",),
            implementation_hash=_implementation_hash(
                "analyses.all:metadata-all:source-ref-ascending"
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
                    # A revision turn that cannot see the end of what it is
                    # revising rewrites it. Text truncates rather than drops,
                    # so the loss is the memo's tail and nothing says so; a
                    # 28.6k-character memo lost 8.6k of itself at 20_000.
                    budget=ContextBudget(max_items=1, max_characters=32_000),
                ),
                ContextSource(
                    id="analysis_summary",
                    source_type="analyses",
                    required=False,
                    selector=ContextSelector(selector_id="analyses.all"),
                    # The EDA memo, when one has been written. This is what
                    # lets planning state a risk assessment grounded in what
                    # the data actually showed rather than in schema shape
                    # alone. Absent before any analysis has run, which is why
                    # the source is optional and the section degrades.
                    representations=(ContextRepresentation("analysis_summary"),),
                    budget=ContextBudget(max_items=1, max_characters=24_000),
                ),
                ContextSource(
                    id="population_summary",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # The aggregate the per-table profiles cannot state: total
                    # rows received and the observed date range across every
                    # date-typed column. Planning cites scale and period from
                    # here rather than deriving them across twelve profiles.
                    representations=(ContextRepresentation("population_summary"),),
                    budget=ContextBudget(max_items=1, max_characters=6_000),
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
            # 46 = the declared per-source ceilings summed; the population
            # summary is the one added item and must not cost the last
            # methodology excerpt its slot.
            budget=ContextBudget(max_items=46, max_characters=94_000),
            privacy=ContextPrivacy(
                allow_planning_context=True,
                allow_template_text=True,
                allow_document_text=True,
                allow_table_metadata=True,
                allow_table_profiles=True,
                # The memo only. Planning never sees the flagged rows
                # themselves — it sees what the auditor's summary said about
                # them, with embed directives already flattened to citations.
                allow_analysis_summary=True,
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
                    # The RCM is built from the whole memo. At 24_000 the last
                    # process sections of a 28.6k-character APM never reached
                    # the turn, so no row could be proposed for them. Paid for
                    # by scoping the table sources to imported populations.
                    budget=ContextBudget(max_items=1, max_characters=32_000),
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
                    id="small_table_rows",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # A profile's aggregates cannot describe a small reference or
                    # dimension table faithfully — a 4-row approval matrix's
                    # max/null statistics said nothing about which row carried
                    # the override. Below the adapter's row-count ceiling the
                    # whole table is supplied instead; above it, only the
                    # profile and aggregate sources above apply.
                    representations=(ContextRepresentation("table_rows_small"),),
                    budget=ContextBudget(max_items=8, max_characters=16_000),
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
            budget=ContextBudget(max_items=252, max_characters=116_000),
            privacy=ContextPrivacy(
                allow_planning_context=True,
                allow_template_text=True,
                allow_document_text=True,
                allow_table_metadata=True,
                allow_table_profiles=True,
                allow_small_table_rows=True,
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
                # Every other RCM row was supplied here as duplicate avoidance and
                # did not achieve it: the projection carried the other rows' risks
                # rather than the tests already written for them, and a unit cannot
                # see what its siblings produce in any case. It cost a third of the
                # prompt — more than the target row by a factor of twenty — and was
                # truncated by its own budget besides. Deduplication needs a pass
                # that can see every generated test at once, not a per-unit prompt.
                # Unlike planning, this source carries the derived join frames
                # as well as the base tables, because a data test is written
                # against whichever frame already holds the columns it needs.
                # That makes the candidate set wide enough for fill order to
                # decide coverage, so it is ranked against the row rather than
                # taken alphabetically, and budgeted to hold more than the six
                # the worker finally selects.
                ContextSource(
                    id="table_metadata",
                    source_type="tables",
                    required=False,
                    selector=AutoSelect(
                        selector_id="tables.lexical",
                        item_limit=12,
                        configuration={"query_fields": ["test_generate_query"]},
                    ),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=12, max_characters=24_000),
                ),
                ContextSource(
                    id="transaction_evidence",
                    source_type="tables",
                    required=True,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=1, max_characters=40_000),
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
            budget=ContextBudget(max_items=182, max_characters=128_000),
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
                # The narrative's sections are the firm's, so the template is
                # required context rather than a constant in the worker.
                ContextSource(
                    id="finding_template",
                    source_type="templates",
                    required=True,
                    selector=ContextSelector(selector_id="templates.current"),
                    representations=(ContextRepresentation("artifact_template"),),
                    budget=ContextBudget(max_items=1, max_characters=16_000),
                ),
                # The rows the Data Test flagged. Optional: a Document Test has
                # no tabular exception population, and the finding is drafted
                # from its item disposition instead.
                ContextSource(
                    id="exception_rows",
                    source_type="artifacts",
                    required=False,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(
                        ContextRepresentation("datatest_exception_rows"),
                    ),
                    budget=ContextBudget(max_items=1, max_characters=10_000),
                ),
            ),
            budget=ContextBudget(max_items=6, max_characters=42_000),
            # A finding is grounded in its exception observation, the immutable
            # execution result behind it, and — for a Data Test — the rows that
            # result flagged. The row admission is the deliberate widening: it
            # is what lets a finding name the invoice that failed instead of
            # only counting it, and it is capped by row count and characters in
            # the adapter before this budget applies. No document text, no table
            # slice, and no population beyond the flagged rows is declared.
            privacy=ContextPrivacy(
                allow_document_text=True,
                allow_template_text=True,
                allow_datatest_exception_rows=True,
            ),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="analysis.join_utility",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="join_candidates", source_type="tables", required=True,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_aggregate"),),
                    budget=ContextBudget(max_items=1, max_characters=20_000),
                ),
                ContextSource(
                    id="join_tables", source_type="tables", required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=12, max_characters=24_000),
                ),
            ),
            budget=ContextBudget(max_items=13, max_characters=44_000),
            privacy=ContextPrivacy(
                allow_document_text=True, allow_table_metadata=True,
                allow_table_aggregates=True,
            ),
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
                    id="join_hypotheses",
                    source_type="artifacts",
                    required=False,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    # The utility gate's retained decisions whose test this
                    # frame can carry: a stated hypothesis and the columns
                    # naming it. Text the model wrote about local metadata —
                    # no value, no row.
                    representations=(ContextRepresentation("current_artifact"),),
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
                    id="lookup_candidates",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # The frames a reconciliation may read, and the columns in
                    # each that are actually keys. Metadata and cardinality
                    # counts, drawn from the cached profile — a lookup candidate
                    # is described by the shape of its key column, never by the
                    # values in it. Supplied so the submission schema can make
                    # naming a non-existent lookup unrepresentable rather than
                    # merely wrong.
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=12, max_characters=12_000),
                ),
                ContextSource(
                    id="probe_findings",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # Measured nominations: a runnable spec plus what it produced
                    # when the sweep ran it. Aggregate counts and column names,
                    # which is why this rides the same value-free representation
                    # as every other aggregate here — the flagged rows behind a
                    # nomination are never part of it.
                    representations=(ContextRepresentation("table_aggregate"),),
                    budget=ContextBudget(max_items=12, max_characters=12_000),
                ),
                ContextSource(
                    id="value_domains",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # The complete value vocabulary of the columns that have a
                    # small one. Category literals, never a record: a domain
                    # says a status column holds Approved, Pending and Rejected
                    # and cannot say which requisition holds which.
                    representations=(ContextRepresentation("value_domain"),),
                    budget=ContextBudget(max_items=20, max_characters=8_000),
                ),
                ContextSource(
                    id="current_analyses",
                    source_type="artifacts",
                    required=False,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    representations=(ContextRepresentation("current_artifact"),),
                    # Sized for a frame's whole join family, not just the frame
                    # itself: a proposal can only avoid repeating an analysis it
                    # was actually shown, and the same computation is reachable
                    # from every frame sharing a base table with this one.
                    budget=ContextBudget(max_items=40, max_characters=16_000),
                ),
            ),
            budget=ContextBudget(max_items=95, max_characters=86_000),
            # This preset admits exactly one class of engagement value, and no
            # row. ``allow_value_domains`` admits the categories a column ranges
            # over — the disclosure that lets a procedure be written against
            # ``Rejected`` instead of against "a status value" — and says what
            # values exist without saying which record holds which.
            #
            # A bounded sample of real rows was declared here and withdrawn: it
            # cost half the context of a definition turn and, measured against
            # the run before it, bought fewer analyses and worse ones. It belongs
            # to a stage that reads the whole engagement once, not to a turn that
            # reads one frame nineteen times over.
            #
            # ``allow_table_rows`` remains denied and ``table_rows`` remains
            # structurally rejected at the resolver boundary.
            privacy=ContextPrivacy(
                allow_document_text=True,
                allow_table_metadata=True,
                allow_table_profiles=True,
                allow_table_aggregates=True,
                allow_value_domains=True,
            ),
        ),
    )
)
PRESETS.register(
    ContextPreset(
        preset_id="analysis.summary",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="analysis_results",
                    source_type="analyses",
                    required=True,
                    selector=ContextSelector(selector_id="analyses.all"),
                    # Every saved procedure and what it concluded: title, the
                    # note explaining why it was proposed, the frame it ran
                    # against, verdict, and bounded statistics. This is the
                    # inventory the memo narrates.
                    representations=(ContextRepresentation("analysis_result"),),
                    budget=ContextBudget(max_items=120, max_characters=60_000),
                ),
                ContextSource(
                    id="analysis_exceptions",
                    source_type="analyses",
                    required=False,
                    selector=ContextSelector(selector_id="analyses.all"),
                    # The rows identified by procedures that concluded a
                    # failure. The one row-level source class in the contract,
                    # and the reason the memo can name a backdated invoice
                    # rather than merely count it.
                    representations=(ContextRepresentation("analysis_exception_rows"),),
                    budget=ContextBudget(max_items=40, max_characters=45_000),
                ),
                ContextSource(
                    id="analysis_anomalies",
                    source_type="analyses",
                    required=False,
                    selector=ContextSelector(selector_id="analyses.all"),
                    # The same rows for procedures that flagged something
                    # unusual without concluding a failure. Declared separately
                    # so a truncated budget can never drop a failure's evidence
                    # in favour of a warning's: deterministic selection orders
                    # candidates by reference, not by severity.
                    representations=(ContextRepresentation("analysis_exception_rows"),),
                    budget=ContextBudget(max_items=40, max_characters=30_000),
                ),
                ContextSource(
                    id="coverage_gaps",
                    source_type="analyses",
                    required=False,
                    selector=ContextSelector(selector_id="analyses.all"),
                    # Deterministic, locally computed: procedures never run or
                    # stale, ones that errored, tables carrying no procedure at
                    # all, and table pairs never joined. Supplied as fact so the
                    # "further work" section reports the gaps that exist rather
                    # than the ones a model happens to think of.
                    representations=(ContextRepresentation("analysis_result"),),
                    budget=ContextBudget(max_items=1, max_characters=12_000),
                ),
                ContextSource(
                    id="table_joins",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # What each joined frame is: the columns matched, the
                    # direction, and how well they matched. A procedure's frame
                    # name does not carry its keys, so the section describing
                    # the relationships tested has no other source for them —
                    # and a per-row count over a frame that multiplied rows
                    # means something different from one that did not.
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=40, max_characters=20_000),
                ),
                ContextSource(
                    id="table_metadata",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=24, max_characters=12_000),
                ),
                ContextSource(
                    id="table_profiles",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # The population characteristics the memo's opening section
                    # describes: row counts, ranges, blank rates, cardinality.
                    representations=(ContextRepresentation("table_profile"),),
                    budget=ContextBudget(max_items=24, max_characters=24_000),
                ),
                ContextSource(
                    id="planning_context",
                    source_type="planning",
                    required=False,
                    selector=ContextSelector(selector_id="planning.current"),
                    # Present only once planning has run. The memo frames itself
                    # against the stated objective and period when they exist,
                    # and reads perfectly well when they do not — exploratory
                    # analysis frequently precedes planning.
                    representations=(ContextRepresentation("planning_context"),),
                    budget=ContextBudget(max_items=1, max_characters=8_000),
                ),
            ),
            budget=ContextBudget(max_items=250, max_characters=170_000),
            privacy=ContextPrivacy(
                allow_planning_context=True,
                allow_table_metadata=True,
                allow_table_profiles=True,
                allow_analysis_results=True,
                # The deliberate widening. Bounded by the per-source character
                # budget above and by the row cap the adapter applies.
                allow_analysis_exception_rows=True,
            ),
        ),
    )
)


PRESETS.register(
    ContextPreset(
        preset_id="analysis.promotion",
        spec=ContextSpec(
            sources=(
                ContextSource(
                    id="promotion_subject",
                    source_type="analyses",
                    required=True,
                    # The adapter scopes this source to the single candidate
                    # the unit is about, so "all" is one procedure here.
                    selector=ContextSelector(selector_id="analyses.all"),
                    # One procedure per turn: its definition, its verdict and
                    # its counts. Never the rows it flagged — deciding which
                    # control a procedure is evidence about is a judgement
                    # about the procedure, not about what it caught, so this
                    # preset has no reason to request the row permission and
                    # deliberately does not.
                    representations=(ContextRepresentation("analysis_result"),),
                    budget=ContextBudget(max_items=1, max_characters=8_000),
                ),
                ContextSource(
                    id="rcm_rows",
                    source_type="artifacts",
                    required=True,
                    selector=ContextSelector(selector_id="artifacts.current"),
                    # Every row, not a lexically ranked subset. The fit is a
                    # choice among the whole matrix, and a ranked shortlist
                    # would decide it by the same prose overlap that was
                    # measured not to separate — every analysis in one
                    # engagement matched some row at 4-11 shared tokens.
                    representations=(ContextRepresentation("current_artifact"),),
                    budget=ContextBudget(max_items=80, max_characters=48_000),
                ),
                ContextSource(
                    id="table_metadata",
                    source_type="tables",
                    required=False,
                    selector=ContextSelector(selector_id="tables.all"),
                    # Column spelling. A carried procedure needs none of this,
                    # but a catalog procedure's step has to be written, and a
                    # step naming a column that does not exist fails at commit.
                    representations=(ContextRepresentation("table_metadata"),),
                    budget=ContextBudget(max_items=24, max_characters=24_000),
                ),
            ),
            budget=ContextBudget(max_items=105, max_characters=80_000),
            privacy=ContextPrivacy(
                # ``current_artifact`` is authored audit text and travels under
                # the document-text permission, the same way the RCM rows reach
                # ``planning.rcm``. No engagement row of any kind is admitted:
                # neither table-row permission is declared, and the flagged
                # rows behind the procedure stay in their sidecar.
                allow_document_text=True,
                allow_table_metadata=True,
                allow_analysis_results=True,
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
                    # Identity plus the engagement's committed pack ids. Still far
                    # too small to hold a descriptive projection, which is the
                    # point of the narrowing.
                    budget=ContextBudget(max_items=1, max_characters=1_500),
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
