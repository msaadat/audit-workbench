from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError

import pytest

from app.agent.executors import (
    ExecutorConcurrency,
    ExecutorContractError,
    ExecutorDefinition,
    ExecutorReceipt,
    ExecutorReconciliation,
    ExecutorRegistry,
    ExecutorRequest,
    ExecutorResult,
)
from app.agent.executors import model as executor_model


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
PARENT_A = "a" * 40
PARENT_B = "b" * 40
POST_A = "c" * 40


def _request(*, parents=None, revision=4, proposal=None, activity=None):
    return ExecutorRequest(
        executor_id="planning.apm",
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        proposal=proposal or {"apm_markdown": "# Audit planning memorandum"},
        expected_revision=revision,
        expected_parents=(
            {"planning:context": PARENT_A} if parents is None else parents
        ),
        activity=activity or {"artifact_refs": ["planning:apm"]},
    )


def _result(
    request=None,
    *,
    before=4,
    after=5,
    parents=None,
    executor_id="planning.apm",
    capability_id="planning.apm_ready",
    unit_id="planning.apm",
):
    request = request or _request()
    return ExecutorResult(
        executor_id=executor_id,
        capability_id=capability_id,
        unit_id=unit_id,
        workspace_revision_before=before,
        workspace_revision_after=after,
        artifact_refs=["planning:apm"],
        applied_parents=(
            dict(request.expected_parents) if parents is None else parents
        ),
        postcondition_hashes={"planning:apm": POST_A},
        output={"preserved_auditor_edits": False},
    )


def _definition(implementation=None, reconciler=None, **overrides):
    values = {
        "executor_id": "planning.apm",
        "concurrency": ExecutorConcurrency("parent_hashes"),
        "implementation": implementation or (lambda request, target: _result(request)),
        "reconciler": reconciler
        or (lambda request, target: ExecutorReconciliation("not_applied")),
    }
    values.update(overrides)
    return ExecutorDefinition(**values)


def test_executor_request_detaches_and_recursively_freezes_inputs():
    proposal = {"sections": ["scope"]}
    parents = {"planning:context": PARENT_A}
    activity = {"artifact_refs": ["planning:apm"]}
    request = _request(proposal=proposal, parents=parents, activity=activity)
    proposal["sections"].append("risk")
    parents["planning:context"] = PARENT_B
    activity["artifact_refs"].append("planning:context")

    assert request.proposal == {"sections": ("scope",)}
    assert request.expected_parents == {"planning:context": PARENT_A}
    assert request.activity == {"artifact_refs": ("planning:apm",)}
    assert request.proposal_hash.startswith("sha256:")
    with pytest.raises(TypeError):
        request.proposal["sections"] = ()
    with pytest.raises(FrozenInstanceError):
        request.unit_id = "other"


def test_executor_request_rejects_invalid_revision_parent_hash_and_json():
    with pytest.raises(ValueError, match="non-negative integer"):
        _request(revision=-1)
    with pytest.raises(ValueError, match="lowercase SHA-1"):
        _request(parents={"planning:context": "not-a-hash"})
    with pytest.raises(ValueError, match="JSON-compatible"):
        _request(proposal={"bad": {"not-json"}})


def test_proposal_hash_is_canonical_and_changes_only_with_proposal_content():
    first = _request(proposal={"b": 2, "a": 1})
    reordered = _request(proposal={"a": 1, "b": 2}, activity={"other": True})
    changed = _request(proposal={"a": 1, "b": 3})

    assert first.proposal_hash == reordered.proposal_hash
    assert first.proposal_hash != changed.proposal_hash


def test_executor_result_is_immutable_and_requires_exact_postconditions():
    result = _result()

    assert result.postcondition_hashes == {"planning:apm": POST_A}
    assert result.output == {"preserved_auditor_edits": False}
    with pytest.raises(TypeError):
        result.output["preserved_auditor_edits"] = True
    with pytest.raises(ValueError, match="must contain exactly"):
        ExecutorResult(
            executor_id="planning.apm",
            capability_id="planning.apm_ready",
            unit_id="planning.apm",
            workspace_revision_before=4,
            workspace_revision_after=5,
            artifact_refs=["planning:apm"],
            postcondition_hashes={"other:artifact": POST_A},
            applied_parents={"planning:context": PARENT_A},
        )
    with pytest.raises(ValueError, match="must be greater"):
        _result(after=4)
    with pytest.raises(ValueError, match="must be a list or tuple"):
        ExecutorResult(
            executor_id="planning.apm",
            capability_id="planning.apm_ready",
            unit_id="planning.apm",
            workspace_revision_before=4,
            workspace_revision_after=5,
            artifact_refs="planning:apm",
            postcondition_hashes={"planning:apm": POST_A},
            applied_parents={"planning:context": PARENT_A},
        )


def test_executor_metadata_is_hash_identified_and_definition_hash_is_stable():
    implementation = lambda request, target: _result(request)
    reconciler = lambda request, target: ExecutorReconciliation("not_applied")
    first = _definition(implementation, reconciler)
    second = _definition(implementation, reconciler)
    changed = _definition(
        implementation,
        reconciler,
        concurrency=ExecutorConcurrency("workspace_revision"),
    )

    assert first.definition_hash == second.definition_hash
    assert first.definition_hash.startswith("sha256:")
    assert first.definition_hash != changed.definition_hash
    assert first.to_dict() == {
        "executor_id": "planning.apm",
        "concurrency": {"mode": "parent_hashes"},
    }


@pytest.mark.parametrize(
    "factory, message",
    [
        (
            lambda: ExecutorConcurrency("last_write_wins"),
            "mode must be one of",
        ),
        (
            lambda: _definition(reconciler=None, implementation=object()),
            "implementation must be callable",
        ),
    ],
)
def test_executor_definitions_reject_unhashable_or_invalid_metadata(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def test_executor_registry_rejects_duplicates_unknown_ids_and_wrong_entries():
    registry = ExecutorRegistry()
    definition = _definition()
    registry.register(definition)

    assert registry.get("planning.apm") is definition
    assert registry.all() == (definition,)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)
    with pytest.raises(ValueError, match="Unknown executor"):
        registry.get("missing")
    with pytest.raises(ValueError, match="ExecutorDefinition"):
        registry.register(object())


def test_parent_hash_executor_requires_parents_and_allows_unrelated_revision_advance():
    request = _request(revision=4)
    seen = []

    def implementation(value, target):
        seen.append((value, target))
        return _result(value, before=7, after=8)

    registry = ExecutorRegistry()
    definition = registry.register(_definition(implementation=implementation))
    target = object()
    receipt = registry.execute(request, target)

    assert seen == [(request, target)]
    assert definition.concurrency.allows_unrelated_writes is True
    assert receipt.expected_revision == 4
    assert receipt.workspace_revision_before == 7
    assert receipt.workspace_revision_after == 8
    assert receipt.expected_parents == {"planning:context": PARENT_A}

    with pytest.raises(ExecutorContractError, match="requires at least one parent"):
        registry.execute(_request(parents={}), target)


def test_revision_cas_executor_rejects_parent_hashes_and_requires_exact_revision():
    registry = ExecutorRegistry()
    registry.register(
        _definition(
            concurrency=ExecutorConcurrency("workspace_revision"),
            implementation=lambda request, target: _result(
                request, before=4, after=5, parents={}
            ),
        )
    )

    receipt = registry.execute(_request(parents={}), object())
    assert receipt.concurrency_mode == "workspace_revision"
    assert receipt.expected_parents == {}

    with pytest.raises(ExecutorContractError, match="cannot receive parent hashes"):
        registry.execute(_request(), object())

    wrong = ExecutorRegistry()
    wrong.register(
        _definition(
            concurrency=ExecutorConcurrency("workspace_revision"),
            implementation=lambda request, target: _result(
                request, before=5, after=6, parents={}
            ),
        )
    )
    with pytest.raises(ExecutorContractError, match="exact requested workspace revision"):
        wrong.execute(_request(parents={}, revision=4), object())


def test_registry_rejects_wrong_result_type_identity_and_guard_claims():
    cases = [
        (
            lambda request, target: {"not": "a result"},
            "must return an ExecutorResult",
        ),
        (
            lambda request, target: _result(request, unit_id="other"),
            "different executor, capability, or unit",
        ),
        (
            lambda request, target: _result(
                request, parents={"planning:context": PARENT_B}
            ),
            "exact requested parent-hash guard",
        ),
        (
            lambda request, target: _result(request, before=3, after=4),
            "older than the captured parent-hash snapshot",
        ),
    ]
    for implementation, message in cases:
        registry = ExecutorRegistry()
        registry.register(_definition(implementation=implementation))
        with pytest.raises(ExecutorContractError, match=message):
            registry.execute(_request(), object())


def test_successful_execution_returns_hash_identified_immutable_receipt():
    request = _request()
    registry = ExecutorRegistry()
    definition = registry.register(_definition())
    receipt = registry.execute(request, object())

    assert isinstance(receipt, ExecutorReceipt)
    assert receipt.executor_definition_hash == definition.definition_hash
    assert receipt.proposal_hash == request.proposal_hash
    assert receipt.artifact_refs == ("planning:apm",)
    assert receipt.postcondition_hashes == {"planning:apm": POST_A}
    assert receipt.output == {"preserved_auditor_edits": False}
    assert receipt.reconciled is False
    assert receipt.receipt_hash.startswith("sha256:")
    assert receipt.receipt_hash == registry.execute(request, object()).receipt_hash
    with pytest.raises(TypeError):
        receipt.postcondition_hashes["planning:apm"] = PARENT_B


def test_receipt_preserves_nested_json_and_validates_direct_construction():
    request = _request()
    definition = _definition()
    result = ExecutorResult(
        executor_id="planning.apm",
        capability_id="planning.apm_ready",
        unit_id="planning.apm",
        workspace_revision_before=4,
        workspace_revision_after=5,
        artifact_refs=["planning:apm"],
        applied_parents={"planning:context": PARENT_A},
        postcondition_hashes={"planning:apm": POST_A},
        output={"preservation": {"fields": ["scope", "owner"]}},
    )
    receipt = ExecutorReceipt(
        request=request,
        definition=definition,
        result=result,
    )

    assert receipt.to_dict()["output"] == {
        "preservation": {"fields": ["scope", "owner"]}
    }
    assert receipt.receipt_hash.startswith("sha256:")
    with pytest.raises(TypeError):
        receipt.output["preservation"]["fields"] = ()
    with pytest.raises(ExecutorContractError, match="different executor"):
        ExecutorReceipt(
            request=request,
            definition=definition,
            result=_result(request, unit_id="other"),
        )


def test_receipt_round_trip_revalidates_request_definition_and_payload():
    request = _request()
    definition = _definition()
    receipt = ExecutorReceipt(
        request=request,
        definition=definition,
        result=_result(request),
    )

    assert ExecutorReceipt.from_dict(
        receipt.to_dict(), request=request, definition=definition
    ) == receipt
    tampered = {**receipt.to_dict(), "proposal_hash": HASH_B}
    with pytest.raises(ValueError, match="identity does not match"):
        ExecutorReceipt.from_dict(
            tampered, request=request, definition=definition
        )


@pytest.mark.parametrize(
    "outcome, message",
    [
        (lambda: ExecutorReconciliation("already_applied"), "requires an ExecutorResult"),
        (
            lambda: ExecutorReconciliation("not_applied", result=_result()),
            "Only already_applied",
        ),
        (lambda: ExecutorReconciliation("conflict"), "requires a reason"),
        (lambda: ExecutorReconciliation("unknown"), "must be one of"),
    ],
)
def test_reconciliation_models_reject_ambiguous_outcomes(outcome, message):
    with pytest.raises(ValueError, match=message):
        outcome()


def test_interrupted_commit_reconciliation_can_create_receipt_without_reexecution():
    calls = {"execute": 0, "reconcile": 0}

    def implementation(request, target):
        calls["execute"] += 1
        return _result(request)

    def reconciler(request, target):
        calls["reconcile"] += 1
        return ExecutorReconciliation(
            "already_applied",
            result=_result(request),
            reason="The APM postcondition already holds.",
        )

    registry = ExecutorRegistry()
    registry.register(_definition(implementation, reconciler))
    request = _request()
    outcome = registry.reconcile(request, object())
    receipt = registry.receipt_for_reconciliation(request, outcome)

    assert calls == {"execute": 0, "reconcile": 1}
    assert outcome.disposition == "already_applied"
    assert receipt.reconciled is True
    assert receipt.receipt_hash != registry.execute(request, object()).receipt_hash


def test_not_applied_and_conflict_reconciliation_do_not_create_receipts():
    for outcome in (
        ExecutorReconciliation("not_applied"),
        ExecutorReconciliation("conflict", reason="The parent changed."),
    ):
        registry = ExecutorRegistry()
        registry.register(_definition(reconciler=lambda request, target, value=outcome: value))
        request = _request()
        returned = registry.reconcile(request, object())
        assert returned is outcome
        with pytest.raises(ExecutorContractError, match="already_applied"):
            registry.receipt_for_reconciliation(request, returned)


def test_registry_rejects_invalid_reconciler_return_and_result_identity():
    invalid_type = ExecutorRegistry()
    invalid_type.register(_definition(reconciler=lambda request, target: "retry"))
    with pytest.raises(ExecutorContractError, match="must return an ExecutorReconciliation"):
        invalid_type.reconcile(_request(), object())

    invalid_identity = ExecutorRegistry()
    invalid_identity.register(
        _definition(
            reconciler=lambda request, target: ExecutorReconciliation(
                "already_applied", result=_result(request, unit_id="other")
            )
        )
    )
    with pytest.raises(ExecutorContractError, match="different executor"):
        invalid_identity.reconcile(_request(), object())


def test_executor_contract_layer_has_no_model_workspace_store_or_scheduler_dependency():
    source = inspect.getsource(executor_model)
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
        module.endswith(
            (
                "workspaces",
                "workspace_transactions",
                "store",
                "workflow",
                "workflow_runner",
                "model_gateway",
            )
        )
        for module in imported_modules
    )
    assert "app.llm" not in source
    assert "ModelGateway" not in source
    assert "ContextResolver" not in source
    assert "Workspace" not in source
