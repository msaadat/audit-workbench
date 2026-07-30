from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError

import pytest

from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
)
from app.agent.workers import (
    MAX_REPAIR_ATTEMPTS,
    WorkerAttempt,
    WorkerContractError,
    WorkerDefinition,
    WorkerRegistry,
    WorkerRepairPolicy,
    WorkerRequest,
    WorkerResponseSchema,
    WorkerResponseValidationError,
    WorkerRunError,
)
from app.agent.workers import model as worker_model


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


class _Gateway:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1):
        self.calls.append((system, user, activity, attempt))
        return self.responses.pop(0)


def _bundle(content=None):
    value = content if content is not None else {"scope": "purchases"}
    return ContextBundle(
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        items=(
            ContextBundleItem(
                source_id="planning_context",
                source_ref="planning:context",
                representation=ContextRepresentation("planning_context"),
                content=value,
                supplied_size=supplied_size(value),
            ),
        ),
        supplied_size=supplied_size(value),
    )


def _request(*, unit_input=None, activity=None, context=None):
    return WorkerRequest(
        worker_id="planning.apm",
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        context=context or _bundle(),
        unit_input=unit_input or {"target": {"kind": "workspace"}},
        activity=activity or {"artifact_refs": ["planning:apm"]},
    )


def _json_validator(response):
    if response == "invalid":
        raise WorkerResponseValidationError(("missing apm_markdown", "empty draft"))
    return {"apm_markdown": response}


def _definition(implementation, **overrides):
    values = {
        "worker_id": "planning.apm",
        "implementation_hash": HASH_A,
        "prompt_hash": HASH_B,
        "response_schema": WorkerResponseSchema(
            "planning.apm.response", HASH_C, _json_validator
        ),
        "repair_policy": WorkerRepairPolicy(1, HASH_A),
        "implementation": implementation,
    }
    values.update(overrides)
    return WorkerDefinition(**values)


def test_worker_request_detaches_context_and_recursively_freezes_json_inputs():
    content = {"scope": ["purchases"]}
    unit_input = {"target": {"ids": ["A"]}}
    request = _request(context=_bundle(content), unit_input=unit_input)
    content["scope"].append("payments")
    unit_input["target"]["ids"].append("B")

    assert request.context.items[0].content == {"scope": ["purchases"]}
    assert request.unit_input["target"]["ids"] == ("A",)
    with pytest.raises(TypeError):
        request.unit_input["target"] = {}
    with pytest.raises(FrozenInstanceError):
        request.unit_id = "other"


def test_worker_request_rejects_context_identity_mismatches_and_non_json_input():
    with pytest.raises(ValueError, match="capability does not match"):
        WorkerRequest(
            worker_id="planning.apm",
            capability_id="planning.other",
            unit_id="planning.apm",
            context=_bundle(),
        )
    with pytest.raises(ValueError, match="unit does not match"):
        WorkerRequest(
            worker_id="planning.apm",
            capability_id="planning.apm_ready",
            unit_id="planning.other",
            context=_bundle(),
        )
    with pytest.raises(ValueError, match="JSON-compatible"):
        _request(unit_input={"bad": {"not-json"}})


def test_worker_metadata_is_hash_identified_and_definition_hash_is_stable():
    implementation = lambda request, gateway, attempt: "draft"
    first = _definition(implementation)
    second = _definition(implementation)
    changed = _definition(
        implementation,
        repair_policy=WorkerRepairPolicy(1, HASH_B),
    )

    assert first.definition_hash == second.definition_hash
    assert first.definition_hash.startswith("sha256:")
    assert first.definition_hash != changed.definition_hash
    assert first.to_dict() == {
        "worker_id": "planning.apm",
        "implementation_hash": HASH_A,
        "prompt_hash": HASH_B,
        "response_schema_id": "planning.apm.response",
        "response_schema_hash": HASH_C,
        "semantic_validation_hash": None,
        "required_model_capabilities": [],
        "repair_policy": WorkerRepairPolicy(1, HASH_A).to_dict(),
    }


@pytest.mark.parametrize(
    "factory, message",
    [
        (
            lambda: _definition(lambda request, gateway, attempt: "draft", prompt_hash="v1"),
            "prompt_hash must be a sha256 hash",
        ),
        (
            lambda: WorkerResponseSchema("schema", "v1", _json_validator),
            "schema_hash must be a sha256 hash",
        ),
        (
            lambda: WorkerRepairPolicy(MAX_REPAIR_ATTEMPTS + 1, HASH_A),
            "max_repair_attempts must be between",
        ),
        (
            lambda: WorkerRepairPolicy(0, HASH_A),
            "guidance_hash requires at least one repair attempt",
        ),
    ],
)
def test_worker_definitions_reject_unhashable_or_unbounded_metadata(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def test_worker_registry_rejects_duplicates_unknown_ids_and_wrong_entry_types():
    registry = WorkerRegistry()
    definition = _definition(lambda request, gateway, attempt: "draft")
    registry.register(definition)

    assert registry.get("planning.apm") is definition
    assert registry.all() == (definition,)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)
    with pytest.raises(ValueError, match="Unknown worker"):
        registry.get("missing")
    with pytest.raises(ValueError, match="WorkerDefinition"):
        registry.register(object())


def test_registry_validates_response_and_returns_immutable_hash_only_result():
    gateway = _Gateway(["# Audit planning memorandum"])

    def implementation(request, model_gateway, attempt):
        assert request.context.items[0].source_id == "planning_context"
        return model_gateway.complete(
            "[agent:apm] system",
            "draft",
            dict(request.activity),
            attempt=attempt.number,
        )

    registry = WorkerRegistry()
    registry.register(_definition(implementation))
    result = registry.execute(_request(), gateway)

    assert result.proposal == {"apm_markdown": "# Audit planning memorandum"}
    assert result.attempts == 1
    assert result.repaired is False
    assert result.response_hash.startswith("sha256:")
    assert result.response_schema_hash == HASH_C
    assert "Audit planning memorandum" not in repr(result)
    with pytest.raises(TypeError):
        result.proposal["apm_markdown"] = "changed"


def test_invalid_response_receives_one_bounded_repair_then_succeeds():
    attempts = []

    def implementation(request, gateway, attempt):
        attempts.append(attempt)
        return "invalid" if attempt.number == 1 else "repaired draft"

    registry = WorkerRegistry()
    registry.register(_definition(implementation))
    result = registry.execute(_request(), _Gateway())

    assert result.proposal == {"apm_markdown": "repaired draft"}
    assert result.attempts == 2
    assert result.repaired is True
    assert attempts == [
        WorkerAttempt(1),
        WorkerAttempt(2, ("missing apm_markdown", "empty draft")),
    ]


def test_repair_guidance_is_bounded_by_error_count_and_characters():
    seen = []

    def validator(response):
        if response == "bad":
            raise WorkerResponseValidationError(("123456789", "second", "third"))
        return {"value": response}

    def implementation(request, gateway, attempt):
        seen.append(attempt)
        return "bad" if attempt.number == 1 else "good"

    registry = WorkerRegistry()
    registry.register(
        _definition(
            implementation,
            response_schema=WorkerResponseSchema("bounded", HASH_C, validator),
            repair_policy=WorkerRepairPolicy(
                1,
                HASH_A,
                max_validation_errors=2,
                max_guidance_characters=12,
            ),
        )
    )
    registry.execute(_request(), _Gateway())

    assert seen[1].validation_errors == ("123456789", "sec")
    assert sum(map(len, seen[1].validation_errors)) == 12


def test_invalid_response_stops_exactly_at_the_registered_repair_bound():
    calls = []

    def implementation(request, gateway, attempt):
        calls.append(attempt.number)
        return "invalid"

    registry = WorkerRegistry()
    registry.register(_definition(implementation))

    with pytest.raises(WorkerRunError, match="after 2 attempt") as captured:
        registry.execute(_request(), _Gateway())
    assert calls == [1, 2]
    assert captured.value.attempts == 2
    assert captured.value.errors == ("missing apm_markdown", "empty draft")


def test_zero_repair_policy_makes_one_attempt_only():
    calls = []
    registry = WorkerRegistry()
    registry.register(
        _definition(
            lambda request, gateway, attempt: calls.append(attempt.number) or "invalid",
            repair_policy=WorkerRepairPolicy(0),
        )
    )

    with pytest.raises(WorkerRunError, match="after 1 attempt"):
        registry.execute(_request(), _Gateway())
    assert calls == [1]


def test_worker_and_schema_implementation_contract_failures_are_not_repaired():
    registry = WorkerRegistry()
    registry.register(_definition(lambda request, gateway, attempt: {"not": "text"}))
    with pytest.raises(WorkerContractError, match="must return response text"):
        registry.execute(_request(), _Gateway())

    bad_schema = WorkerResponseSchema("bad", HASH_C, lambda response: [response])
    second = WorkerRegistry()
    second.register(
        _definition(
            lambda request, gateway, attempt: "text",
            response_schema=bad_schema,
        )
    )
    with pytest.raises(WorkerContractError, match="must return an object proposal"):
        second.execute(_request(), _Gateway())

    invalid_json_schema = WorkerResponseSchema(
        "invalid-json",
        HASH_C,
        lambda response: {"unsupported": {response}},
    )
    third = WorkerRegistry()
    third.register(
        _definition(
            lambda request, gateway, attempt: "text",
            response_schema=invalid_json_schema,
        )
    )
    with pytest.raises(WorkerContractError, match="returned an invalid proposal"):
        third.execute(_request(), _Gateway())


def test_registry_requires_the_shared_model_gateway_contract():
    registry = WorkerRegistry()
    registry.register(_definition(lambda request, gateway, attempt: "draft"))

    with pytest.raises(WorkerContractError, match="requires a ModelGateway"):
        registry.execute(_request(), object())


def test_worker_contract_layer_has_no_workspace_provider_or_scheduler_dependency():
    source = inspect.getsource(worker_model)
    tree = ast.parse(source)
    imported_modules = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        module.endswith(("workspaces", "store", "workflow", "workflow_runner"))
        for module in imported_modules
    )
    assert "app.llm" not in source
    assert "ContextResolver" not in source
    assert "Workspace" not in source
