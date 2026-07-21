"""Deterministic, bounded resolution of declared worker context.

This module deliberately resolves local candidate material only.  Domain
adapters populate :class:`ContextScope`; selector implementations and active
workflow wiring are introduced by later Phase 4 slices.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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
    ContextSelection,
    ContextSize,
    ContextSource,
    ContextSpec,
)
from .presets import PRESETS, SELECTORS, PresetRegistry, SelectorRegistry


_RESOLVER_IDENTITY = (
    "context-resolver:source-declaration-order:candidate-rank-source-ref:"
    "per-source-before-global:truncate-text-only:deny-undeclared-representations"
)
RESOLVER_HASH = f"sha256:{hashlib.sha256(_RESOLVER_IDENTITY.encode('utf-8')).hexdigest()}"


class ContextResolutionError(ValueError):
    """A declared context source cannot be resolved safely."""

    def __init__(self, message: str, *, source_id: str | None = None) -> None:
        super().__init__(message)
        self.source_id = source_id


@dataclass(frozen=True)
class ContextCandidate:
    """One local candidate emitted by a domain adapter or selector.

    ``rank`` is selector-owned.  The resolver always breaks equal ranks by
    ``source_ref`` so caller iteration order cannot affect the manifest.
    ``reason`` is required because every automatic selection must remain
    explainable without persisting candidate content.
    """

    source_ref: str
    source: object
    representations: Mapping[str, object]
    rank: int = 0
    reason: str = "Matched the declared deterministic selector."

    def __post_init__(self) -> None:
        source_ref = str(self.source_ref or "").strip()
        if not source_ref:
            raise ValueError("context_candidate.source_ref must be a non-empty string.")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 0:
            raise ValueError("context_candidate.rank must be a non-negative integer.")
        reason = str(self.reason or "").strip()
        if not reason:
            raise ValueError("context_candidate.reason must be a non-empty string.")
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
            normalized[kind] = content
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "representations", normalized)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True)
class ContextScope:
    """Local candidate inventory keyed by declared context source ID."""

    candidates: Mapping[str, Iterable[ContextCandidate]]

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
        object.__setattr__(self, "candidates", normalized)

    def for_source(self, source_id: str) -> tuple[ContextCandidate, ...]:
        return tuple(self.candidates.get(source_id, ()))


def _member(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required_id(value: object, kind: str) -> str:
    identifier = str(_member(value, "id") or "").strip()
    if not identifier:
        raise ContextResolutionError(f"Context {kind} must expose a non-empty id.")
    return identifier


def _context_spec(capability: object, presets: PresetRegistry) -> ContextSpec:
    declaration = _member(capability, "context_spec")
    if declaration is None:
        declaration = _member(capability, "context")
    if isinstance(declaration, str):
        return presets.compile(declaration)
    if isinstance(declaration, ContextSpec):
        return ContextSpec.from_json(declaration.to_json())
    raise ContextResolutionError(
        "Context capability must expose a ContextSpec or registered context preset."
    )


def _spec_hash(spec: ContextSpec) -> str:
    return f"sha256:{hashlib.sha256(spec.to_json().encode('utf-8')).hexdigest()}"


def _ordered_candidates(candidates: Iterable[ContextCandidate]) -> tuple[ContextCandidate, ...]:
    return tuple(sorted(candidates, key=lambda item: (item.rank, item.source_ref)))


def _remaining(budget: ContextBudget, used: ContextSize) -> tuple[int, int, int | None]:
    return (
        max(0, budget.max_items - used.items),
        max(0, budget.max_characters - used.characters),
        None
        if budget.max_estimated_tokens is None
        else max(0, budget.max_estimated_tokens - used.estimated_tokens),
    )


def _sum_size(left: ContextSize, right: ContextSize) -> ContextSize:
    return ContextSize(
        items=left.items + right.items,
        characters=left.characters + right.characters,
        estimated_tokens=left.estimated_tokens + right.estimated_tokens,
    )


def _zero_size() -> ContextSize:
    return ContextSize(items=0, characters=0, estimated_tokens=0)


def _selection_reason(
    source: ContextSource,
    definition_tie_breaker: str,
    candidate: ContextCandidate,
    selected_rank: int,
) -> str:
    if isinstance(source.selector, AutoSelect):
        return (
            f"Automatic selector '{source.selector.selector_id}' selected rank "
            f"{selected_rank} using tie-breaker '{definition_tie_breaker}': "
            f"{candidate.reason}"
        )
    return (
        f"Deterministic selector '{source.selector.selector_id}' selected the item: "
        f"{candidate.reason}"
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
    source_budget: ContextBudget,
    source_used: ContextSize,
    global_budget: ContextBudget,
    global_used: ContextSize,
) -> tuple[object | None, ContextSize, str | None]:
    original = supplied_size(content)
    source_items, source_chars, source_tokens = _remaining(source_budget, source_used)
    global_items, global_chars, global_tokens = _remaining(global_budget, global_used)
    if source_items == 0 or global_items == 0:
        return None, original, "item"

    character_limit = min(source_chars, global_chars)
    token_limits = [value for value in (source_tokens, global_tokens) if value is not None]
    if token_limits:
        character_limit = min(character_limit, min(token_limits) * 4)
    if original.characters <= character_limit:
        return content, original, None
    if not isinstance(content, str) or character_limit <= 0:
        return None, original, "size"
    truncated = content[:character_limit]
    return truncated, supplied_size(truncated), "truncate"


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
        spec = self._selectors.validate_spec(_context_spec(capability, self._presets))
        declared_ids = {source.id for source in spec.sources}
        undeclared = sorted(set(scope.candidates) - declared_ids)
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
            candidates = _ordered_candidates(scope.for_source(source.id))
            source_used = _zero_size()
            selected_for_source = 0
            selector_limit = (
                source.selector.item_limit
                if isinstance(source.selector, AutoSelect)
                else source.budget.max_items
            )

            if not candidates:
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

            for candidate in candidates:
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
                                else "Global or per-source size limit reached."
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
                    candidate,
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
    "RESOLVER_HASH",
]
