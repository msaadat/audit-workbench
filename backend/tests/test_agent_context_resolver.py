from dataclasses import dataclass

import pytest

from app.agent.context import (
    AutoSelect,
    ContextBudget,
    ContextCandidate,
    ContextPrivacy,
    ContextRepresentation,
    ContextResolutionError,
    ContextResolver,
    ContextScope,
    ContextSelector,
    ContextSource,
    ContextSpec,
    LocalEmbeddingQuery,
    PresetRegistry,
    SelectorDefinition,
    SelectorRegistry,
)


@dataclass(frozen=True)
class _Capability:
    id: str
    context_spec: ContextSpec


@dataclass(frozen=True)
class _Unit:
    id: str


def _document_source(
    source_id: str,
    *,
    required: bool = False,
    automatic: bool = False,
    item_limit: int = 3,
    max_items: int = 3,
    max_characters: int = 100,
    representation: str = "excerpt",
) -> ContextSource:
    selector = (
        AutoSelect(
            "documents.lexical",
            item_limit=item_limit,
            configuration={"query_fields": ["scope"]},
        )
        if automatic
        else ContextSelector(
            "documents.by_category",
            configuration={"category": "policy"},
        )
    )
    return ContextSource(
        id=source_id,
        source_type="documents",
        required=required,
        selector=selector,
        representations=(ContextRepresentation(representation),),
        budget=ContextBudget(max_items=max_items, max_characters=max_characters),
    )


def _resolve(spec: ContextSpec, scope: ContextScope):
    return ContextResolver().resolve(
        object(),
        _Capability("planning.apm_ready", spec),
        _Unit("planning.apm:workspace"),
        scope,
    )


def test_resolver_orders_sources_candidates_and_auto_reasons_stably():
    spec = ContextSpec(
        sources=(
            _document_source("automatic", automatic=True, item_limit=2),
            _document_source("deterministic"),
        ),
        budget=ContextBudget(max_items=5, max_characters=500),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    candidates = {
        "deterministic": (
            ContextCandidate(
                "document:D",
                "source-d",
                {"excerpt": "D"},
                metadata={"category": "policy"},
            ),
        ),
        "automatic": (
            ContextCandidate(
                "document:B",
                "source-b",
                {"excerpt": "B"},
                metadata={"category": "policy"},
                lexical_text="scope",
            ),
            ContextCandidate(
                "document:C",
                "source-c",
                {"excerpt": "C"},
                metadata={"category": "policy"},
                lexical_text="scope scope",
            ),
            ContextCandidate(
                "document:A",
                "source-a",
                {"excerpt": "A"},
                metadata={"category": "policy"},
                lexical_text="scope",
            ),
        ),
    }

    first_manifest, first_bundle = _resolve(
        spec,
        ContextScope(candidates, selector_context={"scope": "scope scope"}),
    )
    second_manifest, second_bundle = _resolve(
        spec,
        ContextScope(
            {key: tuple(reversed(value)) for key, value in candidates.items()},
            selector_context={"scope": "scope scope"},
        ),
    )

    assert [item.source_ref for item in first_manifest.selections] == [
        "document:C",
        "document:A",
        "document:D",
    ]
    assert [item.source_ref for item in first_bundle.items] == [
        "document:C",
        "document:A",
        "document:D",
    ]
    assert first_manifest == second_manifest
    assert first_bundle == second_bundle
    assert first_manifest.manifest_hash == second_manifest.manifest_hash
    assert all(
        item.reason.startswith("Automatic selector 'documents.lexical' selected rank")
        and "source_ref_ascending" in item.reason
        for item in first_manifest.selections[:2]
    )
    assert first_manifest.omissions[0].source_ref == "document:B"
    assert first_manifest.omissions[0].reason == "Selector item limit reached."


def test_resolver_enforces_per_source_and_global_limits_with_stable_truncation():
    per_source_spec = ContextSpec(
        sources=(
            _document_source("policies", max_items=2, max_characters=5),
            _document_source("other", max_items=2, max_characters=20),
        ),
        budget=ContextBudget(max_items=2, max_characters=20),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    scope = ContextScope(
        {
            "policies": (
                ContextCandidate(
                    "document:A",
                    "source-a",
                    {"excerpt": "abcd"},
                    metadata={"category": "policy"},
                ),
                ContextCandidate(
                    "document:B",
                    "source-b",
                    {"excerpt": "efgh"},
                    metadata={"category": "policy"},
                ),
            ),
            "other": (
                ContextCandidate(
                    "document:C",
                    "source-c",
                    {"excerpt": "zz"},
                    metadata={"category": "policy"},
                ),
            ),
        }
    )

    manifest, bundle = _resolve(per_source_spec, scope)

    assert [item.content for item in bundle.items] == ["abcd", "e"]
    assert manifest.supplied_size.characters == 5
    assert manifest.supplied_size.items == 2
    assert manifest.truncations[0].source_ref == "document:B"
    assert manifest.truncations[0].original_size.characters == 4
    assert manifest.truncations[0].supplied_size.characters == 1
    assert any(
        item.source_ref == "document:C"
        and item.reason == "Global or per-source item limit reached."
        for item in manifest.omissions
    )

    global_spec = ContextSpec(
        sources=(_document_source("policies", max_items=3, max_characters=100),),
        budget=ContextBudget(
            max_items=3,
            max_characters=6,
            max_estimated_tokens=2,
        ),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    global_manifest, global_bundle = _resolve(
        global_spec,
        ContextScope(
            {
                "policies": (
                    ContextCandidate(
                        "document:A",
                        "source-a",
                        {"excerpt": "abcd"},
                        metadata={"category": "policy"},
                    ),
                    ContextCandidate(
                        "document:B",
                        "source-b",
                        {"excerpt": "efgh"},
                        metadata={"category": "policy"},
                    ),
                )
            }
        ),
    )

    assert [item.content for item in global_bundle.items] == ["abcd", "ef"]
    assert global_manifest.supplied_size.characters == 6
    assert global_manifest.supplied_size.estimated_tokens == 2
    assert global_manifest.truncations[0].supplied_size.characters == 2


def test_required_source_absence_blocks_and_optional_absence_is_manifested():
    required_spec = ContextSpec(
        sources=(_document_source("required", required=True),),
        budget=ContextBudget(max_items=3, max_characters=100),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    with pytest.raises(
        ContextResolutionError,
        match="Required context source 'required'",
    ) as error:
        _resolve(required_spec, ContextScope({}))
    assert error.value.source_id == "required"

    optional_spec = ContextSpec(
        sources=(_document_source("optional"),),
        budget=ContextBudget(max_items=3, max_characters=100),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    manifest, bundle = _resolve(optional_spec, ContextScope({}))

    assert bundle.items == ()
    assert manifest.omissions[0].source_id == "optional"
    assert manifest.omissions[0].source_ref is None
    assert manifest.omissions[0].reason == "Optional context source is unavailable."


def test_resolver_denies_unknown_and_undeclared_representations_by_default():
    unknown_spec = ContextSpec(
        sources=(_document_source("policies", representation="invented_raw_dump"),),
        budget=ContextBudget(max_items=3, max_characters=100),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    with pytest.raises(ValueError, match="denied by default"):
        _resolve(unknown_spec, ContextScope({}))

    declared_spec = ContextSpec(
        sources=(_document_source("policies"),),
        budget=ContextBudget(max_items=3, max_characters=100),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    sentinel = "ROW LEVEL DATA MUST NOT BE SUPPLIED"
    manifest, bundle = _resolve(
        declared_spec,
        ContextScope(
            {
                "policies": (
                    ContextCandidate(
                        "document:A",
                        "source-a",
                        {"table_rows": sentinel},
                        metadata={"category": "policy"},
                    ),
                )
            }
        ),
    )

    assert bundle.items == ()
    assert manifest.privacy_decisions[0].allowed is False
    assert manifest.privacy_decisions[0].representation == "table_rows"
    assert manifest.omissions[0].reason == "No declared representation is available."
    assert sentinel not in manifest.to_json()


def test_resolver_rejects_undeclared_sources_before_materializing_content():
    spec = ContextSpec(
        sources=(_document_source("policies"),),
        budget=ContextBudget(max_items=3, max_characters=100),
        privacy=ContextPrivacy(allow_document_text=True),
    )

    with pytest.raises(ContextResolutionError, match="undeclared source 'other'"):
        _resolve(
            spec,
            ContextScope(
                {
                    "other": (
                        ContextCandidate("document:A", "source-a", {"excerpt": "A"}),
                    )
                }
            ),
        )


def test_local_embedding_selector_is_hash_bound_and_stably_tie_broken():
    model_hash = "sha256:" + "1" * 64
    index_hash = "sha256:" + "2" * 64
    selectors = SelectorRegistry()
    definition = selectors.register(
        SelectorDefinition(
            selector_id="documents.embedding",
            selector_kind="auto",
            supported_source_types=("documents",),
            implementation_hash="sha256:" + "3" * 64,
            strategy="local_embedding",
            local_embedding_model_hash=model_hash,
            local_embedding_index_hash=index_hash,
        )
    )
    spec = ContextSpec(
        sources=(
            ContextSource(
                id="documents",
                source_type="documents",
                required=True,
                selector=AutoSelect("documents.embedding", item_limit=2),
                representations=(ContextRepresentation("excerpt"),),
                budget=ContextBudget(max_items=2, max_characters=100),
            ),
        ),
        budget=ContextBudget(max_items=2, max_characters=100),
        privacy=ContextPrivacy(allow_document_text=True),
    )
    candidates = (
        ContextCandidate(
            "document:B",
            "source-b",
            {"excerpt": "B"},
            embedding=(1.0, 0.0),
        ),
        ContextCandidate(
            "document:C",
            "source-c",
            {"excerpt": "C"},
            embedding=(0.0, 1.0),
        ),
        ContextCandidate(
            "document:A",
            "source-a",
            {"excerpt": "A"},
            embedding=(1.0, 0.0),
        ),
    )
    query = LocalEmbeddingQuery(model_hash, index_hash, (1.0, 0.0))
    resolver = ContextResolver(
        selectors=selectors,
        presets=PresetRegistry(selectors),
    )

    first_manifest, first_bundle = resolver.resolve(
        object(),
        _Capability("planning.apm_ready", spec),
        _Unit("planning.apm:workspace"),
        ContextScope(
            {"documents": candidates},
            local_embedding_queries={"documents": query},
        ),
    )
    second_manifest, second_bundle = resolver.resolve(
        object(),
        _Capability("planning.apm_ready", spec),
        _Unit("planning.apm:workspace"),
        ContextScope(
            {"documents": tuple(reversed(candidates))},
            local_embedding_queries={"documents": query},
        ),
    )

    assert [item.source_ref for item in first_bundle.items] == [
        "document:A",
        "document:B",
    ]
    assert first_manifest == second_manifest
    assert first_bundle == second_bundle
    assert (
        first_manifest.selections[0].selector_definition_hash
        == definition.definition_hash
    )
    assert "source_ref_ascending" in first_manifest.selections[0].reason
    assert "registered model/index identity" in first_manifest.selections[0].reason
    assert first_manifest.omissions[0].source_ref == "document:C"

    mismatched = LocalEmbeddingQuery(
        "sha256:" + "4" * 64,
        index_hash,
        (1.0, 0.0),
    )
    with pytest.raises(ContextResolutionError, match="identity does not match"):
        resolver.resolve(
            object(),
            _Capability("planning.apm_ready", spec),
            _Unit("planning.apm:workspace"),
            ContextScope(
                {"documents": candidates},
                local_embedding_queries={"documents": mismatched},
            ),
        )
