import json

import pytest

from app.agent.context import (
    AutoSelect,
    ContextBudget,
    ContextBundle,
    ContextBundleItem,
    ContextManifest,
    ContextOmission,
    ContextPrivacy,
    ContextPrivacyDecision,
    ContextPreset,
    ContextRepresentation,
    ContextSelection,
    ContextSelector,
    ContextSize,
    ContextSource,
    ContextSpec,
    ContextTruncation,
    PRESETS,
    PresetRegistry,
    SELECTORS,
    SelectorDefinition,
    SelectorRegistry,
)


def _size(items=1, characters=120, estimated_tokens=30):
    return ContextSize(
        items=items,
        characters=characters,
        estimated_tokens=estimated_tokens,
    )


def test_context_spec_normalizes_and_round_trips_deterministically():
    spec = ContextSpec(
        sources=(
            ContextSource(
                id=" policy_documents ",
                source_type=" documents ",
                required=True,
                selector=AutoSelect(
                    selector_id="documents.lexical",
                    item_limit=4,
                    configuration={
                        "query_fields": ("objective", "scope"),
                        "category": "policy",
                    },
                ),
                representations=(
                    ContextRepresentation(
                        "excerpt",
                        {"page_window": 2, "include_citations": True},
                    ),
                ),
                budget=ContextBudget(max_items=4, max_characters=20_000),
            ),
            ContextSource(
                id="methodology",
                source_type="methodology",
                required=False,
                selector=ContextSelector(
                    selector_id="methodology.explicit_refs",
                    configuration={"refs": ["methodology:internal-audit"]},
                ),
                representations=(ContextRepresentation("summary"),),
                budget=ContextBudget(
                    max_items=2,
                    max_characters=8_000,
                    max_estimated_tokens=2_000,
                ),
            ),
        ),
        budget=ContextBudget(
            max_items=6,
            max_characters=28_000,
            max_estimated_tokens=7_000,
        ),
        privacy=ContextPrivacy(
            allow_document_text=True,
            allow_table_metadata=True,
            allow_table_profiles=True,
            allow_table_rows=False,
        ),
    )

    payload = spec.to_dict()
    encoded = spec.to_json()

    assert spec.sources[0].id == "policy_documents"
    assert spec.sources[0].source_type == "documents"
    assert payload["sources"][0]["selector"] == {
        "kind": "auto",
        "selector_id": "documents.lexical",
        "item_limit": 4,
        "configuration": {
            "category": "policy",
            "query_fields": ["objective", "scope"],
        },
    }
    assert payload["sources"][1]["selector"]["kind"] == "deterministic"
    assert encoded == json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert ContextSpec.from_dict(payload) == spec
    assert ContextSpec.from_json(encoded) == spec
    assert ContextSpec.from_json(encoded).to_json() == encoded


def test_manifest_round_trip_is_content_free_but_records_all_decisions():
    representation = ContextRepresentation("excerpt", {"page_window": 2})
    selection = ContextSelection(
        source_id="policy_documents",
        source_type="documents",
        source_ref="document:DOC-1",
        source_hash="sha1:document-source",
        selector_kind="auto",
        selector_id="documents.lexical",
        selector_definition_hash="sha1:selector-definition",
        reason="Highest stable lexical score; tie broken by document id.",
        representation=representation,
        supplied_size=_size(characters=120, estimated_tokens=30),
    )
    manifest = ContextManifest(
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        context_spec_hash="sha1:context-spec",
        resolver_hash="sha1:resolver",
        selections=(selection,),
        omissions=(
            ContextOmission(
                source_id="policy_documents",
                source_ref="document:DOC-2",
                source_hash="sha1:omitted-source",
                reason="Per-source item limit reached.",
            ),
        ),
        truncations=(
            ContextTruncation(
                source_id="policy_documents",
                source_ref="document:DOC-1",
                reason="Per-source character limit reached.",
                original_size=_size(characters=240, estimated_tokens=60),
                supplied_size=_size(characters=120, estimated_tokens=30),
            ),
        ),
        privacy_decisions=(
            ContextPrivacyDecision(
                source_id="policy_documents",
                source_ref="document:DOC-1",
                representation="excerpt",
                allowed=True,
                reason="Document text is permitted by the normalized spec.",
            ),
            ContextPrivacyDecision(
                source_id="ledger_rows",
                representation="table_rows",
                allowed=False,
                reason="Row-level table data is prohibited.",
            ),
        ),
        supplied_size=_size(characters=120, estimated_tokens=30),
    )

    encoded = manifest.to_json()

    assert ContextManifest.from_json(encoded) == manifest
    assert ContextManifest.from_dict(manifest.to_dict()).to_json() == encoded
    assert "selector_definition_hash" in encoded
    assert "Row-level table data is prohibited." in encoded
    assert "SENSITIVE POLICY BODY" not in encoded
    assert set(manifest.to_dict()) == {
        "capability_id",
        "unit_id",
        "context_spec_hash",
        "resolver_hash",
        "selections",
        "omissions",
        "truncations",
        "privacy_decisions",
        "supplied_size",
    }


def test_bundle_round_trip_keeps_local_content_separate_from_manifest_shape():
    sentinel = "SENSITIVE POLICY BODY"
    bundle = ContextBundle(
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        items=(
            ContextBundleItem(
                source_id="policy_documents",
                source_ref="document:DOC-1",
                representation=ContextRepresentation("excerpt"),
                content={"citation": "DOC-1:p1", "text": sentinel},
                supplied_size=_size(characters=len(sentinel), estimated_tokens=5),
            ),
        ),
        supplied_size=_size(characters=len(sentinel), estimated_tokens=5),
    )

    encoded = bundle.to_json()

    assert ContextBundle.from_json(encoded) == bundle
    assert sentinel in encoded
    assert "content" in bundle.to_dict()["items"][0]
    assert "content" not in ContextSelection.__dataclass_fields__
    assert "content" not in ContextManifest.__dataclass_fields__


@pytest.mark.parametrize(
    "factory, message",
    [
        (
            lambda: ContextBudget(max_items=0, max_characters=100),
            "budget.max_items must be a positive integer",
        ),
        (
            lambda: AutoSelect("documents.lexical", item_limit=0),
            "selector.item_limit must be a positive integer",
        ),
        (
            lambda: ContextRepresentation("excerpt", {"bad": {"not-json"}}),
            "representation.options.bad must contain only JSON-compatible values",
        ),
    ],
)
def test_context_models_reject_non_normalizable_values(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def test_context_spec_deserialization_rejects_unknown_selector_discriminator():
    payload = {
        "sources": [
            {
                "id": "documents",
                "source_type": "documents",
                "required": False,
                "selector": {
                    "kind": "model",
                    "selector_id": "provider-selection",
                    "configuration": {},
                },
                "representations": [{"kind": "excerpt", "options": {}}],
                "budget": {"max_items": 1, "max_characters": 1000, "max_estimated_tokens": None},
            }
        ],
        "budget": {"max_items": 1, "max_characters": 1000, "max_estimated_tokens": None},
        "privacy": ContextPrivacy().to_dict(),
    }

    with pytest.raises(
        ValueError,
        match="source.selector.kind must be 'deterministic' or 'auto'",
    ):
        ContextSpec.from_dict(payload)


def _selector_definition(**overrides):
    values = {
        "selector_id": "documents.test",
        "selector_kind": "deterministic",
        "supported_source_types": ("documents",),
        "implementation_hash": "sha256:" + "1" * 64,
        "configuration_keys": ("category",),
    }
    values.update(overrides)
    return SelectorDefinition(**values)


def _registered_spec(
    *,
    source_type="documents",
    selector_id="documents.test",
    representation="excerpt",
    privacy=None,
    configuration=None,
):
    return ContextSpec(
        sources=(
            ContextSource(
                id="source",
                source_type=source_type,
                required=False,
                selector=ContextSelector(
                    selector_id=selector_id,
                    configuration=configuration or {},
                ),
                representations=(ContextRepresentation(representation),),
                budget=ContextBudget(max_items=2, max_characters=1_000),
            ),
        ),
        budget=ContextBudget(max_items=2, max_characters=1_000),
        privacy=privacy or ContextPrivacy(allow_document_text=True),
    )


def test_documents_policies_preset_compiles_to_validated_normalized_spec():
    preset = PRESETS.get("documents.policies")
    compiled = PRESETS.compile("documents.policies")
    source = compiled.sources[0]
    selector_definition = SELECTORS.validate_source(source)

    assert compiled == preset.spec
    assert compiled is not preset.spec
    assert source.source_type == "documents"
    assert source.selector.selector_id == "documents.by_category"
    assert source.selector.configuration == {"category": "policy"}
    assert source.representations == (ContextRepresentation("excerpt"),)
    assert compiled.privacy.allow_document_text is True
    assert preset.definition_hash.startswith("sha256:")
    assert selector_definition.definition_hash.startswith("sha256:")
    assert selector_definition.tie_breaker == "source_ref_ascending"


def test_registered_auto_selector_is_bounded_hash_identified_and_reasoned():
    definition = SELECTORS.get("documents.lexical")
    source = ContextSource(
        id="policy_documents",
        source_type="documents",
        required=False,
        selector=AutoSelect(
            selector_id="documents.lexical",
            item_limit=3,
            configuration={"category": "policy", "query_fields": ["scope"]},
        ),
        representations=(ContextRepresentation("excerpt"),),
        budget=ContextBudget(max_items=3, max_characters=5_000),
    )

    assert SELECTORS.validate_source(source) is definition
    assert definition.selector_kind == "auto"
    assert definition.supported_source_types == ("documents",)
    assert definition.implementation_hash.startswith("sha256:")
    assert definition.definition_hash.startswith("sha256:")
    assert definition.tie_breaker == "source_ref_ascending"
    assert definition.emits_reasons is True


def test_context_registries_reject_duplicate_and_unknown_keys():
    selectors = SelectorRegistry()
    definition = _selector_definition()
    selectors.register(definition)
    with pytest.raises(ValueError, match="Selector 'documents.test' is already registered"):
        selectors.register(definition)
    with pytest.raises(ValueError, match="Unknown context selector 'missing'"):
        selectors.validate_spec(_registered_spec(selector_id="missing"))
    with pytest.raises(ValueError, match="Unknown configuration key 'other'"):
        selectors.validate_spec(
            _registered_spec(configuration={"other": "unsupported"})
        )

    presets = PresetRegistry(selectors)
    preset = ContextPreset("documents.test", _registered_spec())
    presets.register(preset)
    with pytest.raises(ValueError, match="already registered"):
        presets.register(preset)
    with pytest.raises(ValueError, match="Unknown context preset 'missing'"):
        presets.compile("missing")


def test_selector_registry_rejects_unhashable_and_unsupported_definitions():
    with pytest.raises(ValueError, match="is unhashable"):
        _selector_definition(implementation_hash="manual-version-1")

    selectors = SelectorRegistry()
    selectors.register(_selector_definition())
    with pytest.raises(
        ValueError,
        match="does not support source type 'methodology'",
    ):
        selectors.validate_spec(
            _registered_spec(source_type="methodology", representation="summary")
        )


@pytest.mark.parametrize(
    "privacy, representation, message",
    [
        (
            ContextPrivacy(),
            "excerpt",
            "representation 'excerpt'.*requires privacy.allow_document_text",
        ),
        (
            ContextPrivacy(allow_document_text=True, allow_table_rows=True),
            "excerpt",
            "row-level table data cannot be sent",
        ),
        (
            ContextPrivacy(allow_provider=False, allow_document_text=True),
            "excerpt",
            "provider delivery is disabled",
        ),
    ],
)
def test_selector_registry_rejects_invalid_privacy_combinations(
    privacy,
    representation,
    message,
):
    selectors = SelectorRegistry()
    selectors.register(_selector_definition())

    with pytest.raises(ValueError, match=message):
        selectors.validate_spec(
            _registered_spec(privacy=privacy, representation=representation)
        )
