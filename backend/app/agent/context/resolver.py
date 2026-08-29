"""Deterministic, bounded resolution of declared worker context.

This module deliberately resolves local candidate material only. Domain
adapters populate :class:`ContextScope`; the closed selector implementation
set accepts data, never a gateway, provider client, or network service.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .manifest import (
    omission_record,
    source_hash,
    supplied_size,
    total_supplied_size,
    truncation_record,
)
from .model import (
    AutoSelect,
    ContextBudget,
    ContextBundle,
    ContextBundleItem,
    ContextManifest,
    ContextPrivacyDecision,
    ContextRepresentation,
    ROW_LEVEL_TABLE_REPRESENTATIONS,
    ContextSelection,
    ContextSize,
    ContextSource,
    ContextSpec,
)
from .presets import (
    PRESETS,
    SELECTORS,
    PresetRegistry,
    SelectorDefinition,
    SelectorRegistry,
)


_RESOLVER_IDENTITY = (
    "context-resolver:closed-local-selector-strategies:metadata-lexical-embedding:"
    "source-declaration-order:strategy-rank-source-ref:"
    "per-source-before-global:truncate-text-only:deny-undeclared-representations:"
    "reject-row-level-table-candidates"
)
RESOLVER_HASH = f"sha256:{hashlib.sha256(_RESOLVER_IDENTITY.encode('utf-8')).hexdigest()}"
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.IGNORECASE)


class ContextResolutionError(ValueError):
    """A declared context source cannot be resolved safely."""

    def __init__(self, message: str, *, source_id: str | None = None) -> None:
        super().__init__(message)
        self.source_id = source_id


@dataclass(frozen=True)
class ContextCandidate:
    """One local candidate emitted by a domain adapter.

    Selection inputs remain local and are deliberately data-only. The resolver
    owns ranking and reasons so an adapter cannot smuggle in an opaque or
    provider-produced selection decision.
    """

    source_ref: str
    source: object
    representations: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)
    lexical_text: str = ""
    embedding: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        source_ref = str(self.source_ref or "").strip()
        if not source_ref:
            raise ValueError("context_candidate.source_ref must be a non-empty string.")
        if not isinstance(self.representations, Mapping):
            raise ValueError("context_candidate.representations must be an object.")
        normalized: dict[str, object] = {}
        for raw_kind, content in self.representations.items():
            kind = str(raw_kind or "").strip()
            if not kind:
                raise ValueError(
                    "context_candidate representation keys must be non-empty strings."
                )
            if kind in normalized:
                raise ValueError(
                    f"context_candidate representation '{kind}' is duplicated."
                )
            if kind in ROW_LEVEL_TABLE_REPRESENTATIONS:
                raise ValueError(
                    f"Row-level table representation '{kind}' is forbidden in agent "
                    "context."
                )
            normalized[kind] = content
        if not isinstance(self.metadata, Mapping):
            raise ValueError("context_candidate.metadata must be an object.")
        normalized_metadata: dict[str, object] = {}
        for raw_key, value in self.metadata.items():
            key = str(raw_key or "").strip()
            if not key:
                raise ValueError("context_candidate metadata keys must be non-empty strings.")
            normalized_metadata[key] = value
        source_hash(normalized_metadata)
        if not isinstance(self.lexical_text, str):
            raise ValueError("context_candidate.lexical_text must be a string.")
        lexical_text = self.lexical_text
        embedding = None
        if self.embedding is not None:
            embedding = _normalized_vector(self.embedding, "context_candidate.embedding")
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "representations", normalized)
        object.__setattr__(self, "metadata", normalized_metadata)
        object.__setattr__(self, "lexical_text", lexical_text)
        object.__setattr__(self, "embedding", embedding)


@dataclass(frozen=True)
class LocalEmbeddingQuery:
    """Hash-identified local query vector for one selector source."""

    model_hash: str
    index_hash: str
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        for field_name in ("model_hash", "index_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
                raise ValueError(
                    f"local_embedding_query.{field_name} must be a sha256 hash."
                )
        object.__setattr__(
            self,
            "vector",
            _normalized_vector(self.vector, "local_embedding_query.vector"),
        )


@dataclass(frozen=True)
class ContextScope:
    """Local candidate inventory keyed by declared context source ID."""

    candidates: Mapping[str, Iterable[ContextCandidate]]
    selector_context: Mapping[str, object] = field(default_factory=dict)
    local_embedding_queries: Mapping[str, LocalEmbeddingQuery] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, Mapping):
            raise ValueError("context_scope.candidates must be an object.")
        normalized: dict[str, tuple[ContextCandidate, ...]] = {}
        for raw_source_id, values in self.candidates.items():
            source_id = str(raw_source_id or "").strip()
            if not source_id:
                raise ValueError("context_scope source IDs must be non-empty strings.")
            if source_id in normalized:
                raise ValueError(f"context_scope source ID '{source_id}' is duplicated.")
            items = tuple(values)
            if any(not isinstance(item, ContextCandidate) for item in items):
                raise ValueError(
                    "context_scope candidates must contain only ContextCandidate values."
                )
            source_refs = [item.source_ref for item in items]
            if len(source_refs) != len(set(source_refs)):
                raise ValueError(
                    f"context_scope source '{source_id}' contains duplicate source refs."
                )
            normalized[source_id] = items
        if not isinstance(self.selector_context, Mapping):
            raise ValueError("context_scope.selector_context must be an object.")
        normalized_context: dict[str, object] = {}
        for raw_key, value in self.selector_context.items():
            key = str(raw_key or "").strip()
            if not key:
                raise ValueError(
                    "context_scope selector-context keys must be non-empty strings."
                )
            normalized_context[key] = value
        source_hash(normalized_context)
        if not isinstance(self.local_embedding_queries, Mapping):
            raise ValueError("context_scope.local_embedding_queries must be an object.")
        normalized_queries: dict[str, LocalEmbeddingQuery] = {}
        for raw_source_id, query in self.local_embedding_queries.items():
            source_id = str(raw_source_id or "").strip()
            if not source_id:
                raise ValueError(
                    "context_scope local-embedding source IDs must be non-empty strings."
                )
            if not isinstance(query, LocalEmbeddingQuery):
                raise ValueError(
                    "context_scope local-embedding queries must contain only "
                    "LocalEmbeddingQuery values."
                )
            normalized_queries[source_id] = query
        object.__setattr__(self, "candidates", normalized)
        object.__setattr__(self, "selector_context", normalized_context)
        object.__setattr__(self, "local_embedding_queries", normalized_queries)

    def for_source(self, source_id: str) -> tuple[ContextCandidate, ...]:
        return tuple(self.candidates.get(source_id, ()))


@dataclass(frozen=True)
class _SelectorMatch:
    source_ref: str
    reason: str


@dataclass(frozen=True)
class _SelectorInput:
    """Data-only view supplied to a closed local selector strategy."""

    source_ref: str
    metadata: Mapping[str, object]
    lexical_text: str
    embedding: tuple[float, ...] | None


def _member(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required_id(value: object, kind: str) -> str:
    identifier = str(_member(value, "id") or "").strip()
    if not identifier:
        raise ContextResolutionError(f"Context {kind} must expose a non-empty id.")
    return identifier


def _context_spec(
    capability: object,
    presets: PresetRegistry,
    *,
    unit: object | None = None,
    manifest: ContextManifest | None = None,
) -> ContextSpec:
    declaration = _member(capability, "context_spec")
    if declaration is None:
        declaration = _member(capability, "context")
    if isinstance(declaration, Mapping):
        if unit is not None:
            kind = str(_member(unit, "kind") or "").strip()
            if kind not in declaration:
                raise ContextResolutionError(
                    f"Context capability has no binding for unit kind '{kind}'."
                )
            declaration = declaration[kind]
        elif manifest is not None:
            # Keyed by spec, not by kind: two unit kinds may legitimately read
            # the same declared context, and finding that one spec twice is not
            # an ambiguity — it is the same answer arrived at twice. Counting
            # entries rather than distinct specs refused every resume for a
            # capability whose kinds shared a preset.
            matches: dict[str, ContextSpec] = {}
            for value in declaration.values():
                spec = (
                    presets.compile(value)
                    if isinstance(value, str)
                    else value
                )
                if not isinstance(spec, ContextSpec):
                    continue
                spec_hash = _spec_hash(spec)
                if spec_hash == manifest.context_spec_hash:
                    matches[spec_hash] = spec
            if len(matches) != 1:
                raise ContextResolutionError(
                    "Context manifest does not identify exactly one declared "
                    "per-unit context binding."
                )
            return next(iter(matches.values()))
        else:
            raise ContextResolutionError(
                "Per-unit context binding requires a unit or manifest identity."
            )
    if isinstance(declaration, str):
        return presets.compile(declaration)
    if isinstance(declaration, ContextSpec):
        return ContextSpec.from_json(declaration.to_json())
    raise ContextResolutionError(
        "Context capability must expose a ContextSpec or registered context preset."
    )


def _spec_hash(spec: ContextSpec) -> str:
    return f"sha256:{hashlib.sha256(spec.to_json().encode('utf-8')).hexdigest()}"


def _normalized_vector(values: Sequence[float], field_name: str) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a numeric vector.")
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field_name} must contain only finite numbers.")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{field_name} must contain only finite numbers.")
        vector.append(number)
    if not vector:
        raise ValueError(f"{field_name} must not be empty.")
    return tuple(vector)


def _tokens(value: object) -> tuple[str, ...]:
    return tuple(_TOKEN_PATTERN.findall(str(value or "").casefold()))


def _metadata_matches(
    candidate: _SelectorInput,
    configuration: Mapping[str, object],
) -> bool:
    for key, expected in configuration.items():
        if key in {"query_fields", "refs"}:
            continue
        actual = candidate.metadata.get(key)
        if isinstance(actual, str) and isinstance(expected, str):
            if actual.strip().casefold() != expected.strip().casefold():
                return False
        elif actual != expected:
            return False
    return True


def _metadata_matches_for_source(
    source: ContextSource,
    candidates: Iterable[_SelectorInput],
) -> tuple[_SelectorMatch, ...]:
    configuration = source.selector.configuration
    refs = configuration.get("refs")
    if refs is not None:
        if not isinstance(refs, list):
            raise ContextResolutionError(
                f"Selector '{source.selector.selector_id}' refs must be an array.",
                source_id=source.id,
            )
        positions = {str(value): position for position, value in enumerate(refs)}
        ordered = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.source_ref in positions
                and _metadata_matches(candidate, configuration)
            ),
            key=lambda candidate: (positions[candidate.source_ref], candidate.source_ref),
        )
        return tuple(
            _SelectorMatch(
                candidate.source_ref,
                "Matched a declared reference using declared-reference order.",
            )
            for candidate in ordered
        )
    ordered = sorted(
        (
            candidate
            for candidate in candidates
            if _metadata_matches(candidate, configuration)
        ),
        key=lambda candidate: candidate.source_ref,
    )
    return tuple(
        _SelectorMatch(
            candidate.source_ref,
            "Matched the declared local metadata constraints.",
        )
        for candidate in ordered
    )


def _lexical_matches(
    source: ContextSource,
    definition: SelectorDefinition,
    candidates: Iterable[_SelectorInput],
    selector_context: Mapping[str, object],
) -> tuple[_SelectorMatch, ...]:
    raw_fields = source.selector.configuration.get("query_fields", ())
    if not isinstance(raw_fields, list):
        raise ContextResolutionError(
            f"Selector '{source.selector.selector_id}' query_fields must be an array.",
            source_id=source.id,
        )
    query_terms = Counter(
        token
        for field_name in raw_fields
        for token in _tokens(selector_context.get(str(field_name)))
    )
    eligible = [
        candidate
        for candidate in candidates
        if _metadata_matches(candidate, source.selector.configuration)
    ]
    # A ranking selector with nothing to rank by still has to return the
    # candidate set in its stable order; only a filtering one may conclude
    # that an empty query matches nothing.
    if not query_terms:
        if not definition.retain_unmatched:
            return ()
        return tuple(
            _SelectorMatch(
                candidate.source_ref,
                "Ranked below every lexically matched candidate.",
            )
            for candidate in sorted(eligible, key=lambda item: item.source_ref)
        )
    ranked: list[tuple[int, str, _SelectorInput]] = []
    unmatched: list[_SelectorInput] = []
    for candidate in eligible:
        candidate_terms = Counter(_tokens(candidate.lexical_text))
        score = sum(
            min(count, candidate_terms.get(term, 0))
            for term, count in query_terms.items()
        )
        if score > 0:
            ranked.append((-score, candidate.source_ref, candidate))
        elif definition.retain_unmatched:
            unmatched.append(candidate)
    ranked.sort(key=lambda item: (item[0], item[1]))
    unmatched.sort(key=lambda item: item.source_ref)
    return tuple(
        [
            _SelectorMatch(
                candidate.source_ref,
                f"Matched {abs(score)} normalized lexical term occurrence(s).",
            )
            for score, _source_ref, candidate in ranked
        ]
        + [
            _SelectorMatch(
                candidate.source_ref,
                "Ranked below every lexically matched candidate.",
            )
            for candidate in unmatched
        ]
    )


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ContextResolutionError(
            "Local-embedding query and candidate dimensions do not match."
        )
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _local_embedding_matches(
    source: ContextSource,
    definition: SelectorDefinition,
    candidates: Iterable[_SelectorInput],
    query: LocalEmbeddingQuery | None,
) -> tuple[_SelectorMatch, ...]:
    if query is None:
        return ()
    if (
        query.model_hash != definition.local_embedding_model_hash
        or query.index_hash != definition.local_embedding_index_hash
    ):
        raise ContextResolutionError(
            f"Selector '{definition.selector_id}' local embedding identity does not match "
            "its registered model/index hashes.",
            source_id=source.id,
        )
    ranked: list[tuple[float, str, _SelectorInput]] = []
    for candidate in candidates:
        if candidate.embedding is None or not _metadata_matches(
            candidate, source.selector.configuration
        ):
            continue
        score = round(_cosine(query.vector, candidate.embedding), 12)
        ranked.append((-score, candidate.source_ref, candidate))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(
        _SelectorMatch(
            candidate.source_ref,
            f"Local embedding cosine score {-score:.12f} using the registered "
            "model/index identity.",
        )
        for score, _source_ref, candidate in ranked
    )


def _select_candidates(
    source: ContextSource,
    definition: SelectorDefinition,
    candidates: Iterable[ContextCandidate],
    scope: ContextScope,
) -> tuple[_SelectorMatch, ...]:
    selection_inputs = tuple(
        _SelectorInput(
            source_ref=candidate.source_ref,
            metadata=candidate.metadata,
            lexical_text=candidate.lexical_text,
            embedding=candidate.embedding,
        )
        for candidate in candidates
    )
    if definition.strategy == "metadata":
        return _metadata_matches_for_source(source, selection_inputs)
    if definition.strategy == "lexical":
        return _lexical_matches(
            source, definition, selection_inputs, scope.selector_context
        )
    if definition.strategy == "local_embedding":
        return _local_embedding_matches(
            source,
            definition,
            selection_inputs,
            scope.local_embedding_queries.get(source.id),
        )
    raise ContextResolutionError(
        f"Selector '{definition.selector_id}' uses an unsupported strategy.",
        source_id=source.id,
    )


def _remaining(
    budget: ContextBudget, used: ContextSize
) -> tuple[int, int, int | None, int | None, int | None, int | None, int | None]:
    return (
        max(0, budget.max_items - used.items),
        max(0, budget.max_characters - used.characters),
        None
        if budget.max_estimated_tokens is None
        else max(0, budget.max_estimated_tokens - used.estimated_tokens),
        None
        if budget.max_media_items is None
        else max(0, budget.max_media_items - used.media_items),
        None
        if budget.max_media_bytes is None
        else max(0, budget.max_media_bytes - used.media_bytes),
        None
        if budget.max_media_pixels is None
        else max(0, budget.max_media_pixels - used.media_pixels),
        None
        if budget.max_estimated_image_tokens is None
        else max(
            0,
            budget.max_estimated_image_tokens - used.estimated_image_tokens,
        ),
    )


def _sum_size(left: ContextSize, right: ContextSize) -> ContextSize:
    return ContextSize(
        items=left.items + right.items,
        characters=left.characters + right.characters,
        estimated_tokens=left.estimated_tokens + right.estimated_tokens,
        media_items=left.media_items + right.media_items,
        media_bytes=left.media_bytes + right.media_bytes,
        media_pixels=left.media_pixels + right.media_pixels,
        estimated_image_tokens=(
            left.estimated_image_tokens + right.estimated_image_tokens
        ),
    )


def _zero_size() -> ContextSize:
    return ContextSize(
        items=0,
        characters=0,
        estimated_tokens=0,
        media_items=0,
        media_bytes=0,
        media_pixels=0,
        estimated_image_tokens=0,
    )


def _selection_reason(
    source: ContextSource,
    definition_tie_breaker: str,
    selector_reason: str,
    selected_rank: int,
) -> str:
    if isinstance(source.selector, AutoSelect):
        return (
            f"Automatic selector '{source.selector.selector_id}' selected rank "
            f"{selected_rank} using tie-breaker '{definition_tie_breaker}': "
            f"{selector_reason}"
        )
    return (
        f"Deterministic selector '{source.selector.selector_id}' selected the item: "
        f"{selector_reason}"
    )


def _representation(
    source: ContextSource,
    candidate: ContextCandidate,
) -> tuple[ContextRepresentation | None, tuple[ContextPrivacyDecision, ...]]:
    for declared in source.representations:
        if declared.kind in candidate.representations:
            return declared, (
                ContextPrivacyDecision(
                    source_id=source.id,
                    source_ref=candidate.source_ref,
                    representation=declared.kind,
                    allowed=True,
                    reason=(
                        "Representation is declared and permitted by the normalized "
                        "context policy."
                    ),
                ),
            )
    decisions = tuple(
        ContextPrivacyDecision(
            source_id=source.id,
            source_ref=candidate.source_ref,
            representation=kind,
            allowed=False,
            reason="Representation is not declared for this context source.",
        )
        for kind in sorted(candidate.representations)
    )
    return None, decisions


def _fit_content(
    content: object,
    *,
    representation_kind: str,
    source_budget: ContextBudget,
    source_used: ContextSize,
    global_budget: ContextBudget,
    global_used: ContextSize,
) -> tuple[object | None, ContextSize, str | None]:
    original = supplied_size(
        content, representation_kind=representation_kind
    )
    (
        source_items,
        source_chars,
        source_tokens,
        source_media_items,
        source_media_bytes,
        source_media_pixels,
        source_image_tokens,
    ) = _remaining(source_budget, source_used)
    (
        global_items,
        global_chars,
        global_tokens,
        global_media_items,
        global_media_bytes,
        global_media_pixels,
        global_image_tokens,
    ) = _remaining(global_budget, global_used)
    if source_items == 0 or global_items == 0:
        return None, original, "item"
    if representation_kind == "page_image":
        media_limits = (
            (source_media_items, original.media_items),
            (global_media_items, original.media_items),
            (source_media_bytes, original.media_bytes),
            (global_media_bytes, original.media_bytes),
            (source_media_pixels, original.media_pixels),
            (global_media_pixels, original.media_pixels),
            (source_image_tokens, original.estimated_image_tokens),
            (global_image_tokens, original.estimated_image_tokens),
        )
        if any(limit is not None and required > limit for limit, required in media_limits):
            return None, original, "media"
        return content, original, None

    character_limit = min(source_chars, global_chars)
    token_limits = [value for value in (source_tokens, global_tokens) if value is not None]
    if token_limits:
        character_limit = min(character_limit, min(token_limits) * 4)
    if original.characters <= character_limit:
        return content, original, None
    if not isinstance(content, str) or character_limit <= 0:
        return None, original, "size"
    truncated = content[:character_limit]
    return (
        truncated,
        supplied_size(truncated, representation_kind=representation_kind),
        "truncate",
    )


class ContextResolver:
    """Resolve a normalized declaration into a content-free manifest and local bundle."""

    def __init__(
        self,
        *,
        selectors: SelectorRegistry = SELECTORS,
        presets: PresetRegistry = PRESETS,
    ) -> None:
        self._selectors = selectors
        self._presets = presets

    @property
    def resolver_hash(self) -> str:
        return RESOLVER_HASH

    def execution_identity(
        self,
        capability: object,
        manifest: ContextManifest,
    ) -> dict[str, object]:
        """Return the complete content-free context identity for proposal reuse."""
        if not isinstance(manifest, ContextManifest):
            raise ContextResolutionError(
                "Context execution identity requires a ContextManifest."
            )
        capability_id = _required_id(capability, "capability")
        spec = self._selectors.validate_spec(
            _context_spec(
                capability, self._presets, manifest=manifest
            )
        )
        spec_hash = _spec_hash(spec)
        if (
            manifest.capability_id != capability_id
            or manifest.context_spec_hash != spec_hash
            or manifest.resolver_hash != self.resolver_hash
        ):
            raise ContextResolutionError(
                "Context manifest does not match the current capability policy."
            )
        return {
            "context_manifest_hash": manifest.manifest_hash,
            "context_spec_hash": spec_hash,
            "resolver_hash": self.resolver_hash,
            "selector_definition_hashes": [
                {
                    "source_id": source.id,
                    "selector_id": source.selector.selector_id,
                    "definition_hash": self._selectors.validate_source(
                        source
                    ).definition_hash,
                }
                for source in spec.sources
            ],
        }

    def resolve(
        self,
        workspace: object,
        capability: object,
        unit: object,
        scope: ContextScope,
    ) -> tuple[ContextManifest, ContextBundle]:
        """Resolve in declaration order and enforce every hard budget.

        ``workspace`` is intentionally unused until domain adapters are added;
        retaining it in the contract prevents a later scheduler API change.
        """
        del workspace
        if not isinstance(scope, ContextScope):
            raise ContextResolutionError("Context scope must be a ContextScope.")
        capability_id = _required_id(capability, "capability")
        unit_id = _required_id(unit, "unit")
        spec = self._selectors.validate_spec(
            _context_spec(capability, self._presets, unit=unit)
        )
        declared_ids = {source.id for source in spec.sources}
        undeclared = sorted(
            (set(scope.candidates) | set(scope.local_embedding_queries)) - declared_ids
        )
        if undeclared:
            raise ContextResolutionError(
                f"Context scope contains undeclared source '{undeclared[0]}'.",
                source_id=undeclared[0],
            )

        selections: list[ContextSelection] = []
        bundle_items: list[ContextBundleItem] = []
        omissions = []
        truncations = []
        privacy_decisions = []
        global_used = _zero_size()

        for source in spec.sources:
            definition = self._selectors.validate_source(source)
            source_candidates = scope.for_source(source.id)
            if not source_candidates:
                reason = (
                    "Required context source is unavailable."
                    if source.required
                    else "Optional context source is unavailable."
                )
                if source.required:
                    raise ContextResolutionError(
                        f"Required context source '{source.id}' is unavailable.",
                        source_id=source.id,
                    )
                omissions.append(omission_record(source_id=source.id, reason=reason))
                continue
            candidates = _select_candidates(
                source,
                definition,
                source_candidates,
                scope,
            )
            candidates_by_ref = {
                candidate.source_ref: candidate for candidate in source_candidates
            }
            matched_refs = {match.source_ref for match in candidates}
            for excluded in sorted(
                (
                    candidate
                    for candidate in source_candidates
                    if candidate.source_ref not in matched_refs
                ),
                key=lambda candidate: candidate.source_ref,
            ):
                omissions.append(
                    omission_record(
                        source_id=source.id,
                        source_ref=excluded.source_ref,
                        source=excluded.source,
                        reason=(
                            f"Local selector strategy '{definition.strategy}' did not "
                            "match the candidate."
                        ),
                    )
                )
            source_used = _zero_size()
            selected_for_source = 0
            selector_limit = (
                source.selector.item_limit
                if isinstance(source.selector, AutoSelect)
                else source.budget.max_items
            )

            if not candidates:
                reason = (
                    "Required context selector matched no local candidates."
                    if source.required
                    else "Optional context selector matched no local candidates."
                )
                if source.required:
                    raise ContextResolutionError(
                        f"Required context source '{source.id}' matched no candidates.",
                        source_id=source.id,
                    )
                omissions.append(omission_record(source_id=source.id, reason=reason))
                continue

            for match in candidates:
                candidate = candidates_by_ref[match.source_ref]
                if selected_for_source >= selector_limit:
                    omissions.append(
                        omission_record(
                            source_id=source.id,
                            source_ref=candidate.source_ref,
                            source=candidate.source,
                            reason="Selector item limit reached.",
                        )
                    )
                    continue

                representation, decisions = _representation(source, candidate)
                privacy_decisions.extend(decisions)
                if representation is None:
                    omissions.append(
                        omission_record(
                            source_id=source.id,
                            source_ref=candidate.source_ref,
                            source=candidate.source,
                            reason="No declared representation is available.",
                        )
                    )
                    continue

                original_content = candidate.representations[representation.kind]
                content, content_size, disposition = _fit_content(
                    original_content,
                    representation_kind=representation.kind,
                    source_budget=source.budget,
                    source_used=source_used,
                    global_budget=spec.budget,
                    global_used=global_used,
                )
                if content is None:
                    omissions.append(
                        omission_record(
                            source_id=source.id,
                            source_ref=candidate.source_ref,
                            source=candidate.source,
                            reason=(
                                "Global or per-source item limit reached."
                                if disposition == "item"
                                else (
                                    "Global or per-source media limit reached."
                                    if disposition == "media"
                                    else "Global or per-source size limit reached."
                                )
                            ),
                        )
                    )
                    continue
                if disposition == "truncate":
                    truncations.append(
                        truncation_record(
                            source_id=source.id,
                            source_ref=candidate.source_ref,
                            reason="Global or per-source character/token limit reached.",
                            original_content=original_content,
                            supplied_content=content,
                        )
                    )

                source_identity = source_hash(candidate.source)
                reason = _selection_reason(
                    source,
                    definition.tie_breaker,
                    match.reason,
                    selected_for_source + 1,
                )
                selection = ContextSelection(
                    source_id=source.id,
                    source_type=source.source_type,
                    source_ref=candidate.source_ref,
                    source_hash=source_identity,
                    selector_kind=source.selector.kind,
                    selector_id=source.selector.selector_id,
                    selector_definition_hash=definition.definition_hash,
                    reason=reason,
                    representation=representation,
                    supplied_size=content_size,
                    media=(
                        {
                            key: content.get(key)
                            for key in (
                                "source_ref",
                                "source_sha1",
                                "prepared_sha256",
                                "page",
                                "frame",
                                "variant",
                                "tile_order",
                                "mime",
                                "width",
                                "height",
                                "prepared_byte_count",
                                "pixel_count",
                                "policy_hash",
                                "prepared_set_hash",
                            )
                        }
                        if representation.kind == "page_image"
                        and isinstance(content, Mapping)
                        else None
                    ),
                )
                bundle_item = ContextBundleItem(
                    source_id=source.id,
                    source_ref=candidate.source_ref,
                    representation=representation,
                    content=content,
                    supplied_size=content_size,
                )
                selections.append(selection)
                bundle_items.append(bundle_item)
                source_used = _sum_size(source_used, content_size)
                global_used = _sum_size(global_used, content_size)
                selected_for_source += 1

            if source.required and selected_for_source == 0:
                raise ContextResolutionError(
                    f"Required context source '{source.id}' supplied no permitted items.",
                    source_id=source.id,
                )
            if not source.required and selected_for_source == 0 and not any(
                omission.source_id == source.id and omission.source_ref is None
                for omission in omissions
            ):
                omissions.append(
                    omission_record(
                        source_id=source.id,
                        reason="Optional context source supplied no permitted items.",
                    )
                )

        supplied = total_supplied_size(item.supplied_size for item in bundle_items)
        manifest = ContextManifest(
            capability_id=capability_id,
            unit_id=unit_id,
            context_spec_hash=_spec_hash(spec),
            resolver_hash=self.resolver_hash,
            selections=tuple(selections),
            omissions=tuple(omissions),
            truncations=tuple(truncations),
            privacy_decisions=tuple(privacy_decisions),
            supplied_size=supplied,
        )
        bundle = ContextBundle(
            capability_id=capability_id,
            unit_id=unit_id,
            items=tuple(bundle_items),
            supplied_size=supplied,
        )
        return manifest, bundle


__all__ = [
    "ContextCandidate",
    "ContextResolutionError",
    "ContextResolver",
    "ContextScope",
    "LocalEmbeddingQuery",
    "RESOLVER_HASH",
]
