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
            ContextCandidate("document:D", "source-d", {"excerpt": "D"}),
        ),
        "automatic": (
            ContextCandidate(
                "document:B",
                "source-b",
                {"excerpt": "B"},
                rank=1,
                reason="Lexical score 0.8 for the declared scope fields.",
            ),
            ContextCandidate(
                "document:C",
                "source-c",
                {"excerpt": "C"},
                rank=0,
                reason="Lexical score 0.9 for the declared scope fields.",
            ),
            ContextCandidate(
                "document:A",
                "source-a",
                {"excerpt": "A"},
                rank=1,
                reason="Lexical score 0.8 for the declared scope fields.",
            ),
        ),
    }

    first_manifest, first_bundle = _resolve(spec, ContextScope(candidates))
    second_manifest, second_bundle = _resolve(
        spec,
        ContextScope({key: tuple(reversed(value)) for key, value in candidates.items()}),
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
                ContextCandidate("document:A", "source-a", {"excerpt": "abcd"}),
                ContextCandidate("document:B", "source-b", {"excerpt": "efgh"}),
            ),
            "other": (
                ContextCandidate("document:C", "source-c", {"excerpt": "zz"}),
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
                    ContextCandidate("document:A", "source-a", {"excerpt": "abcd"}),
                    ContextCandidate("document:B", "source-b", {"excerpt": "efgh"}),
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
