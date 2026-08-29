import ast
import inspect
import json
import threading

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
    load_manifest,
    manifest_identity,
    omission_record,
    persist_manifest,
    source_hash,
    supplied_size,
    total_supplied_size,
    truncation_record,
)
from app.agent import store
from app.agent.runtime import DefaultRunRuntime
from app.workspaces import WorkspaceError
import app.agent.context.presets as context_presets
import app.agent.context.resolver as context_resolver


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


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.update(
                {"auditor_overrides": {"max_characters": 99_999}}
            ),
            "Unknown context_spec field 'auditor_overrides'",
        ),
        (
            lambda payload: payload["sources"][0].update(
                {"selected_refs": ["document:auditor-choice"]}
            ),
            "Unknown source field 'selected_refs'",
        ),
    ],
)
def test_context_spec_is_declaration_only_and_rejects_runtime_override_fields(
    mutate,
    message,
):
    payload = PRESETS.compile("documents.policies").to_dict()
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        ContextSpec.from_dict(payload)


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


def test_bundle_item_structurally_rejects_row_level_table_representation():
    with pytest.raises(
        ValueError,
        match="bundle_item.representation cannot use row-level table representation",
    ):
        ContextBundleItem(
            source_id="ledger",
            source_ref="table:ledger",
            representation=ContextRepresentation("table_rows"),
            content=[["ROW SECRET"]],
            supplied_size=_size(characters=16, estimated_tokens=4),
        )

    with pytest.raises(
        ValueError,
        match="selection.representation cannot use row-level table representation",
    ):
        ContextSelection(
            source_id="ledger",
            source_type="tables",
            source_ref="table:ledger",
            source_hash="sha256:" + "1" * 64,
            selector_kind="deterministic",
            selector_id="tables.all",
            selector_definition_hash="sha256:" + "2" * 64,
            reason="Selected for testing.",
            representation=ContextRepresentation("table_rows"),
            supplied_size=_size(characters=16, estimated_tokens=4),
        )


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
    assert definition.strategy == "lexical"


def test_selector_definitions_allow_only_closed_local_strategy_identities():
    with pytest.raises(ValueError, match="strategy must be"):
        _selector_definition(strategy="provider_model")
    with pytest.raises(ValueError, match="Deterministic selectors support only"):
        _selector_definition(strategy="lexical")
    with pytest.raises(ValueError, match="require sha256 model and index hashes"):
        _selector_definition(selector_kind="auto", strategy="local_embedding")
    with pytest.raises(ValueError, match="Only local-embedding selectors"):
        _selector_definition(local_embedding_model_hash="sha256:" + "2" * 64)
    with pytest.raises(ValueError, match="must use the stable"):
        _selector_definition(
            selector_kind="auto",
            strategy="lexical",
            tie_breaker="declared_reference_order",
            configuration_keys=("refs",),
            required_configuration_keys=("refs",),
        )


def test_local_embedding_model_and_index_hashes_participate_in_selector_identity():
    common = {
        "selector_id": "documents.embedding",
        "selector_kind": "auto",
        "supported_source_types": ("documents",),
        "implementation_hash": "sha256:" + "1" * 64,
        "strategy": "local_embedding",
        "local_embedding_model_hash": "sha256:" + "2" * 64,
    }
    first = SelectorDefinition(
        **common,
        local_embedding_index_hash="sha256:" + "3" * 64,
    )
    second = SelectorDefinition(
        **common,
        local_embedding_index_hash="sha256:" + "4" * 64,
    )

    assert first.definition_hash != second.definition_hash
    assert first.to_dict()["local_embedding_model_hash"] == "sha256:" + "2" * 64
    assert first.to_dict()["local_embedding_index_hash"] == "sha256:" + "3" * 64


def test_selector_implementation_boundary_has_no_gateway_or_network_dependency():
    forbidden_imports = {
        "aiohttp",
        "anthropic",
        "httpx",
        "openai",
        "requests",
        "socket",
        "urllib",
        "urllib3",
    }
    for module in (context_presets, context_resolver):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            str(node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not (imported_roots & forbidden_imports)
        assert "ModelGateway" not in source
        assert "app.llm" not in source

    assert set(inspect.signature(SelectorRegistry.register).parameters) == {
        "self",
        "definition",
    }
    assert set(inspect.signature(context_resolver.ContextResolver).parameters) == {
        "selectors",
        "presets",
    }


def test_selector_inputs_reject_service_objects_before_strategy_execution():
    class _ServiceObject:
        pass

    with pytest.raises(ValueError, match="deterministically hashable"):
        context_resolver.ContextCandidate(
            "document:A",
            "source-a",
            {"excerpt": "A"},
            metadata={"provider_client": _ServiceObject()},
        )
    with pytest.raises(ValueError, match="deterministically hashable"):
        context_resolver.ContextScope(
            {},
            selector_context={"provider_client": _ServiceObject()},
        )


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
        (
            ContextPrivacy(),
            "table_rows_small",
            "representation 'table_rows_small'.*requires privacy.allow_small_table_rows",
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


def _content_free_manifest(*, omission_reason="Per-source item limit reached."):
    selection_size = supplied_size("selected policy excerpt")
    return ContextManifest(
        capability_id="planning.apm_ready",
        unit_id="planning.apm:workspace",
        context_spec_hash="sha256:" + "1" * 64,
        resolver_hash="sha256:" + "2" * 64,
        selections=(
            ContextSelection(
                source_id="policy_documents",
                source_type="documents",
                source_ref="document:DOC-1",
                source_hash=source_hash("complete policy source"),
                selector_kind="deterministic",
                selector_id="documents.by_category",
                selector_definition_hash="sha256:" + "3" * 64,
                reason="Policy category matched.",
                representation=ContextRepresentation("excerpt"),
                supplied_size=selection_size,
            ),
        ),
        omissions=(
            omission_record(
                source_id="policy_documents",
                source_ref="document:DOC-2",
                source="second policy source",
                reason=omission_reason,
            ),
        ),
        truncations=(
            truncation_record(
                source_id="policy_documents",
                source_ref="document:DOC-1",
                reason="Per-source character limit reached.",
                original_content="selected policy excerpt plus excluded suffix",
                supplied_content="selected policy excerpt",
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
        ),
        supplied_size=total_supplied_size((selection_size,)),
    )


def test_manifest_identity_source_hashes_records_and_sizes_are_deterministic():
    manifest = _content_free_manifest()
    reordered_source = {"b": [2, 3], "a": 1}

    assert manifest_identity(manifest) == manifest.manifest_hash
    assert (
        manifest_identity(ContextManifest.from_json(manifest.to_json()))
        == manifest.manifest_hash
    )
    assert source_hash(reordered_source) == source_hash({"a": 1, "b": [2, 3]})
    assert source_hash({"a": 2, "b": [2, 3]}) != source_hash(reordered_source)
    assert manifest.omissions[0].source_hash == source_hash("second policy source")
    assert manifest.truncations[0].original_size.characters == 44
    assert manifest.truncations[0].supplied_size.characters == 23
    assert manifest.supplied_size == ContextSize(items=1, characters=23, estimated_tokens=6)


def test_manifest_identity_changes_for_a_record_change_and_rejects_unhashable_sources():
    first = _content_free_manifest()
    second = _content_free_manifest(omission_reason="Global item limit reached.")

    assert manifest_identity(first) != manifest_identity(second)
    with pytest.raises(ValueError, match="deterministically hashable"):
        source_hash({"unsupported": {"set"}})
    with pytest.raises(ValueError, match="non-negative integer"):
        supplied_size("text", items=-1)


def test_manifest_persistence_is_atomic_hash_checked_and_excludes_bundle_content(
    workspace_with_data,
):
    sentinel = "SENSITIVE POLICY BODY THAT MUST REMAIN LOCAL"
    run = store.new_run(workspace_with_data, "auto")
    manifest = _content_free_manifest()
    bundle = ContextBundle(
        capability_id=manifest.capability_id,
        unit_id=manifest.unit_id,
        items=(
            ContextBundleItem(
                source_id="policy_documents",
                source_ref="document:DOC-1",
                representation=ContextRepresentation("excerpt"),
                content=sentinel,
                supplied_size=supplied_size(sentinel),
            ),
        ),
        supplied_size=supplied_size(sentinel),
    )

    reference = persist_manifest(workspace_with_data, run["id"], manifest)
    path = store.run_dir(workspace_with_data, run["id"]) / reference["path"]

    assert load_manifest(workspace_with_data, run["id"], reference) == manifest
    assert reference["manifest_hash"] == manifest.manifest_hash
    assert reference["unit_id"] == manifest.unit_id
    assert sentinel not in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(ValueError, match="bundle content is local-only"):
        persist_manifest(workspace_with_data, run["id"], bundle)

    replacement = _content_free_manifest(
        omission_reason="Global item limit reached."
    )
    replacement_reference = persist_manifest(
        workspace_with_data,
        run["id"],
        replacement,
    )
    assert replacement_reference["path"] == reference["path"]
    assert replacement_reference["manifest_hash"] != reference["manifest_hash"]
    assert load_manifest(workspace_with_data, run["id"], replacement_reference) == replacement
    assert not list(path.parent.glob("*.tmp"))

    bad_reference = {**reference, "manifest_hash": "sha256:" + "0" * 64}
    with pytest.raises(WorkspaceError, match="identity does not match"):
        load_manifest(workspace_with_data, run["id"], bad_reference)


def test_run_runtime_owns_manifest_persistence_boundary(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto")
    runtime = DefaultRunRuntime(
        workspace=workspace_with_data,
        run=run,
        state_lock=threading.RLock(),
    )
    manifest = _content_free_manifest()

    reference = runtime.persist_context_manifest(manifest)

    assert runtime.load_context_manifest(reference) == manifest


def test_every_privacy_permission_survives_serialization():
    """A permission missing from ``_FIELDS`` is granted in memory and lost on disk.

    ``to_dict`` enumerates an explicit tuple rather than the dataclass fields,
    so adding a permission without adding it there yields an object that reads
    ``True`` in the preset and ``False`` everywhere the spec is persisted and
    reloaded — which is where resolution actually checks it. The failure
    surfaces as the preset being refused for a permission it visibly grants.
    """

    from dataclasses import fields as dataclass_fields

    declared = {
        item.name
        for item in dataclass_fields(ContextPrivacy)
        if item.name.startswith("allow_")
    }
    assert declared == set(ContextPrivacy._FIELDS), (
        "ContextPrivacy._FIELDS must list every allow_* permission; missing: "
        + ", ".join(sorted(declared - set(ContextPrivacy._FIELDS)))
    )

    # And the round trip actually carries them.
    granted = ContextPrivacy(
        **{name: True for name in declared}
    )
    restored = ContextPrivacy.from_dict(granted.to_dict())
    for name in declared:
        assert getattr(restored, name) is True, f"{name} was lost in the round trip"
