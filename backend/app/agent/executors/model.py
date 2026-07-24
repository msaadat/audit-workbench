"""Immutable contracts and registry for deterministic workflow executors.

Executors own conflict-aware workspace mutation and interrupted-commit
reconciliation.  They receive an accepted, detached proposal plus an explicit
concurrency guard; they do not construct context, call a model, or schedule
additional work.  The registry validates those boundaries and produces a
hash-identified receipt for every successful or reconciled commit.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
CONCURRENCY_MODES = frozenset({"workspace_revision", "parent_hashes"})
RECONCILIATION_DISPOSITIONS = frozenset(
    {"not_applied", "already_applied", "conflict"}
)


def _normalized_id(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string.")
    return text


def _require_sha1(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA1_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-1 hash.")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a sha256 hash.")
    return value


def _revision(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _mapping_default(value: object) -> object:
    # Frozen proposals wrap nested objects in MappingProxyType; unwrap them for
    # canonical serialization while still rejecting non-JSON values.
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _canonical_json(value: object, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_mapping_default,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{field_name} must contain only JSON-compatible values."
        ) from error


def _canonical_object(value: object, field_name: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object.")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} object keys must be strings.")
    return _canonical_json(dict(value), field_name)


def _sha256_json(value: object, field_name: str) -> str:
    encoded = _canonical_json(value, field_name)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _frozen_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _frozen_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_frozen_json(item) for item in value)
    return value


def _normalized_sha1_mapping(value: Mapping[str, str], field_name: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object.")
    normalized: dict[str, str] = {}
    for raw_ref, raw_hash in value.items():
        if not isinstance(raw_ref, str):
            raise ValueError(f"{field_name} object keys must be strings.")
        ref = _normalized_id(raw_ref, f"{field_name} reference")
        if ref in normalized:
            raise ValueError(f"{field_name} contains duplicate reference '{ref}'.")
        normalized[ref] = _require_sha1(raw_hash, f"{field_name}.{ref}")
    return _canonical_object(normalized, field_name)


class ExecutorContractError(ValueError):
    """A registered executor or reconciler violated its public contract."""


@dataclass(frozen=True)
class ExecutorConcurrency:
    """Concurrency policy declared by one executor definition.

    ``workspace_revision`` requires strict compare-and-swap against the
    captured revision. ``parent_hashes`` requires one or more material parent
    hashes and permits unrelated workspace revisions to advance.
    """

    mode: str

    def __post_init__(self) -> None:
        mode = str(self.mode or "").strip()
        if mode not in CONCURRENCY_MODES:
            choices = ", ".join(sorted(CONCURRENCY_MODES))
            raise ValueError(f"executor_concurrency.mode must be one of: {choices}.")
        object.__setattr__(self, "mode", mode)

    @property
    def allows_unrelated_writes(self) -> bool:
        return self.mode == "parent_hashes"

    def to_dict(self) -> dict[str, str]:
        return {"mode": self.mode}


@dataclass(frozen=True, init=False)
class ExecutorRequest:
    """Detached proposal and concurrency snapshot for one executor call."""

    executor_id: str
    capability_id: str
    unit_id: str
    expected_revision: int
    proposal_hash: str
    _proposal_json: str = field(repr=False)
    _expected_parents_json: str = field(repr=False)
    _activity_json: str = field(repr=False)

    def __init__(
        self,
        *,
        executor_id: str,
        capability_id: str,
        unit_id: str,
        proposal: Mapping[str, Any],
        expected_revision: int,
        expected_parents: Mapping[str, str] | None = None,
        activity: Mapping[str, Any] | None = None,
    ) -> None:
        proposal_json = _canonical_object(proposal, "executor_request.proposal")
        parents_json = _normalized_sha1_mapping(
            expected_parents or {}, "executor_request.expected_parents"
        )
        activity_json = _canonical_object(
            activity or {}, "executor_request.activity"
        )
        object.__setattr__(
            self, "executor_id", _normalized_id(executor_id, "executor_request.executor_id")
        )
        object.__setattr__(
            self,
            "capability_id",
            _normalized_id(capability_id, "executor_request.capability_id"),
        )
        object.__setattr__(
            self, "unit_id", _normalized_id(unit_id, "executor_request.unit_id")
        )
        object.__setattr__(
            self,
            "expected_revision",
            _revision(expected_revision, "executor_request.expected_revision"),
        )
        object.__setattr__(self, "_proposal_json", proposal_json)
        object.__setattr__(self, "_expected_parents_json", parents_json)
        object.__setattr__(self, "_activity_json", activity_json)
        object.__setattr__(
            self,
            "proposal_hash",
            _sha256_json(json.loads(proposal_json), "executor_request.proposal"),
        )

    @property
    def proposal(self) -> Mapping[str, Any]:
        return _frozen_json(json.loads(self._proposal_json))  # type: ignore[return-value]

    @property
    def expected_parents(self) -> Mapping[str, str]:
        return _frozen_json(  # type: ignore[return-value]
            json.loads(self._expected_parents_json)
        )

    @property
    def activity(self) -> Mapping[str, Any]:
        return _frozen_json(json.loads(self._activity_json))  # type: ignore[return-value]


@dataclass(frozen=True, init=False)
class ExecutorResult:
    """Implementation result describing the committed workspace postcondition."""

    executor_id: str
    capability_id: str
    unit_id: str
    workspace_revision_before: int
    workspace_revision_after: int
    artifact_refs: tuple[str, ...]
    _applied_parents_json: str = field(repr=False)
    _postcondition_hashes_json: str = field(repr=False)
    _output_json: str = field(repr=False)

    def __init__(
        self,
        *,
        executor_id: str,
        capability_id: str,
        unit_id: str,
        workspace_revision_before: int,
        workspace_revision_after: int,
        artifact_refs: tuple[str, ...] | list[str],
        postcondition_hashes: Mapping[str, str],
        applied_parents: Mapping[str, str] | None = None,
        output: Mapping[str, Any] | None = None,
    ) -> None:
        before = _revision(
            workspace_revision_before, "executor_result.workspace_revision_before"
        )
        after = _revision(
            workspace_revision_after, "executor_result.workspace_revision_after"
        )
        if after <= before:
            raise ValueError(
                "executor_result.workspace_revision_after must be greater than "
                "workspace_revision_before."
            )
        if not isinstance(artifact_refs, (tuple, list)):
            raise ValueError("executor_result.artifact_refs must be a list or tuple.")
        refs = tuple(
            _normalized_id(ref, "executor_result.artifact_refs item")
            for ref in artifact_refs
        )
        if not refs:
            raise ValueError("executor_result.artifact_refs must not be empty.")
        if len(set(refs)) != len(refs):
            raise ValueError("executor_result.artifact_refs must be unique.")
        postconditions_json = _normalized_sha1_mapping(
            postcondition_hashes, "executor_result.postcondition_hashes"
        )
        postcondition_refs = set(json.loads(postconditions_json))
        if postcondition_refs != set(refs):
            raise ValueError(
                "executor_result.postcondition_hashes must contain exactly the "
                "artifact_refs."
            )
        object.__setattr__(
            self, "executor_id", _normalized_id(executor_id, "executor_result.executor_id")
        )
        object.__setattr__(
            self,
            "capability_id",
            _normalized_id(capability_id, "executor_result.capability_id"),
        )
        object.__setattr__(
            self, "unit_id", _normalized_id(unit_id, "executor_result.unit_id")
        )
        object.__setattr__(self, "workspace_revision_before", before)
        object.__setattr__(self, "workspace_revision_after", after)
        object.__setattr__(self, "artifact_refs", refs)
        object.__setattr__(
            self,
            "_applied_parents_json",
            _normalized_sha1_mapping(
                applied_parents or {}, "executor_result.applied_parents"
            ),
        )
        object.__setattr__(
            self, "_postcondition_hashes_json", postconditions_json
        )
        object.__setattr__(
            self,
            "_output_json",
            _canonical_object(output or {}, "executor_result.output"),
        )

    @property
    def applied_parents(self) -> Mapping[str, str]:
        return _frozen_json(  # type: ignore[return-value]
            json.loads(self._applied_parents_json)
        )

    @property
    def postcondition_hashes(self) -> Mapping[str, str]:
        return _frozen_json(  # type: ignore[return-value]
            json.loads(self._postcondition_hashes_json)
        )

    @property
    def output(self) -> Mapping[str, Any]:
        return _frozen_json(json.loads(self._output_json))  # type: ignore[return-value]


@dataclass(frozen=True)
class ExecutorReconciliation:
    """An interrupted-commit determination returned by a reconciler."""

    disposition: str
    result: ExecutorResult | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        disposition = str(self.disposition or "").strip()
        if disposition not in RECONCILIATION_DISPOSITIONS:
            choices = ", ".join(sorted(RECONCILIATION_DISPOSITIONS))
            raise ValueError(
                f"executor_reconciliation.disposition must be one of: {choices}."
            )
        reason = None if self.reason is None else str(self.reason).strip()
        if self.reason is not None and not reason:
            raise ValueError(
                "executor_reconciliation.reason must be non-empty when supplied."
            )
        if disposition == "already_applied" and not isinstance(
            self.result, ExecutorResult
        ):
            raise ValueError(
                "already_applied reconciliation requires an ExecutorResult."
            )
        if disposition != "already_applied" and self.result is not None:
            raise ValueError(
                "Only already_applied reconciliation may include an ExecutorResult."
            )
        if disposition == "conflict" and reason is None:
            raise ValueError("conflict reconciliation requires a reason.")
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "reason", reason)


ExecutorImplementation = Callable[[ExecutorRequest, object], ExecutorResult]
ExecutorReconciler = Callable[[ExecutorRequest, object], ExecutorReconciliation]


@dataclass(frozen=True)
class ExecutorDefinition:
    """Hash-identified metadata and callables for one deterministic executor."""

    executor_id: str
    implementation_hash: str
    reconciliation_hash: str
    concurrency: ExecutorConcurrency
    implementation: ExecutorImplementation = field(repr=False, compare=False)
    reconciler: ExecutorReconciler = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "executor_id",
            _normalized_id(self.executor_id, "executor_definition.executor_id"),
        )
        _require_sha256(
            self.implementation_hash, "executor_definition.implementation_hash"
        )
        _require_sha256(
            self.reconciliation_hash, "executor_definition.reconciliation_hash"
        )
        if not isinstance(self.concurrency, ExecutorConcurrency):
            raise ValueError(
                "executor_definition.concurrency must be an ExecutorConcurrency."
            )
        if not callable(self.implementation):
            raise ValueError("executor_definition.implementation must be callable.")
        if not callable(self.reconciler):
            raise ValueError("executor_definition.reconciler must be callable.")
        self.definition_hash

    def to_dict(self) -> dict[str, object]:
        return {
            "executor_id": self.executor_id,
            "implementation_hash": self.implementation_hash,
            "reconciliation_hash": self.reconciliation_hash,
            "concurrency": self.concurrency.to_dict(),
        }

    @property
    def definition_hash(self) -> str:
        return _sha256_json(self.to_dict(), "executor_definition")


def _validate_request_guard(
    request: ExecutorRequest, definition: ExecutorDefinition
) -> None:
    if request.executor_id != definition.executor_id:
        raise ExecutorContractError(
            "Executor request identity does not match the executor definition."
        )
    if definition.concurrency.mode == "workspace_revision":
        if request.expected_parents:
            raise ExecutorContractError(
                f"Executor '{definition.executor_id}' uses workspace_revision "
                "CAS and cannot receive parent hashes."
            )
    elif not request.expected_parents:
        raise ExecutorContractError(
            f"Executor '{definition.executor_id}' requires at least one parent hash."
        )


def _validate_execution_contract(
    request: ExecutorRequest,
    definition: ExecutorDefinition,
    result: object,
) -> ExecutorResult:
    _validate_request_guard(request, definition)
    if not isinstance(result, ExecutorResult):
        raise ExecutorContractError(
            f"Executor '{definition.executor_id}' must return an ExecutorResult."
        )
    expected_identity = (
        request.executor_id,
        request.capability_id,
        request.unit_id,
    )
    result_identity = (
        result.executor_id,
        result.capability_id,
        result.unit_id,
    )
    if result_identity != expected_identity:
        raise ExecutorContractError(
            f"Executor '{definition.executor_id}' returned a result for a "
            "different executor, capability, or unit."
        )
    if dict(result.applied_parents) != dict(request.expected_parents):
        raise ExecutorContractError(
            f"Executor '{definition.executor_id}' did not apply the exact "
            "requested parent-hash guard."
        )
    if definition.concurrency.mode == "workspace_revision":
        if result.workspace_revision_before != request.expected_revision:
            raise ExecutorContractError(
                f"Executor '{definition.executor_id}' did not apply the exact "
                "requested workspace revision CAS guard."
            )
    elif result.workspace_revision_before < request.expected_revision:
        raise ExecutorContractError(
            f"Executor '{definition.executor_id}' reported a workspace revision "
            "older than the captured parent-hash snapshot."
        )
    return result


@dataclass(frozen=True, init=False)
class ExecutorReceipt:
    """Immutable, hash-identified proof of a validated executor commit."""

    executor_id: str
    executor_definition_hash: str
    capability_id: str
    unit_id: str
    proposal_hash: str
    concurrency_mode: str
    expected_revision: int
    workspace_revision_before: int
    workspace_revision_after: int
    artifact_refs: tuple[str, ...]
    reconciled: bool
    _expected_parents_json: str = field(repr=False)
    _postcondition_hashes_json: str = field(repr=False)
    _output_json: str = field(repr=False)

    def __init__(
        self,
        *,
        request: ExecutorRequest,
        definition: ExecutorDefinition,
        result: ExecutorResult,
        reconciled: bool = False,
    ) -> None:
        if not isinstance(request, ExecutorRequest):
            raise ValueError("executor_receipt.request must be an ExecutorRequest.")
        if not isinstance(definition, ExecutorDefinition):
            raise ValueError(
                "executor_receipt.definition must be an ExecutorDefinition."
            )
        if not isinstance(result, ExecutorResult):
            raise ValueError("executor_receipt.result must be an ExecutorResult.")
        if not isinstance(reconciled, bool):
            raise ValueError("executor_receipt.reconciled must be a boolean.")
        _validate_execution_contract(request, definition, result)
        object.__setattr__(self, "executor_id", request.executor_id)
        object.__setattr__(
            self, "executor_definition_hash", definition.definition_hash
        )
        object.__setattr__(self, "capability_id", request.capability_id)
        object.__setattr__(self, "unit_id", request.unit_id)
        object.__setattr__(self, "proposal_hash", request.proposal_hash)
        object.__setattr__(self, "concurrency_mode", definition.concurrency.mode)
        object.__setattr__(self, "expected_revision", request.expected_revision)
        object.__setattr__(
            self, "workspace_revision_before", result.workspace_revision_before
        )
        object.__setattr__(
            self, "workspace_revision_after", result.workspace_revision_after
        )
        object.__setattr__(self, "artifact_refs", result.artifact_refs)
        object.__setattr__(self, "reconciled", reconciled)
        object.__setattr__(
            self,
            "_expected_parents_json",
            _canonical_object(
                json.loads(request._expected_parents_json),
                "executor_receipt.expected_parents",
            ),
        )
        object.__setattr__(
            self,
            "_postcondition_hashes_json",
            _canonical_object(
                json.loads(result._postcondition_hashes_json),
                "executor_receipt.postcondition_hashes",
            ),
        )
        object.__setattr__(
            self,
            "_output_json",
            _canonical_object(
                json.loads(result._output_json), "executor_receipt.output"
            ),
        )

    @property
    def expected_parents(self) -> Mapping[str, str]:
        return _frozen_json(  # type: ignore[return-value]
            json.loads(self._expected_parents_json)
        )

    @property
    def postcondition_hashes(self) -> Mapping[str, str]:
        return _frozen_json(  # type: ignore[return-value]
            json.loads(self._postcondition_hashes_json)
        )

    @property
    def output(self) -> Mapping[str, Any]:
        return _frozen_json(json.loads(self._output_json))  # type: ignore[return-value]

    def to_dict(self) -> dict[str, object]:
        return {
            "executor_id": self.executor_id,
            "executor_definition_hash": self.executor_definition_hash,
            "capability_id": self.capability_id,
            "unit_id": self.unit_id,
            "proposal_hash": self.proposal_hash,
            "concurrency_mode": self.concurrency_mode,
            "expected_revision": self.expected_revision,
            "expected_parents": json.loads(self._expected_parents_json),
            "workspace_revision_before": self.workspace_revision_before,
            "workspace_revision_after": self.workspace_revision_after,
            "artifact_refs": list(self.artifact_refs),
            "postcondition_hashes": json.loads(self._postcondition_hashes_json),
            "output": json.loads(self._output_json),
            "reconciled": self.reconciled,
        }

    @property
    def receipt_hash(self) -> str:
        return _sha256_json(self.to_dict(), "executor_receipt")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        request: ExecutorRequest,
        definition: ExecutorDefinition,
    ) -> "ExecutorReceipt":
        if not isinstance(value, Mapping):
            raise ValueError("executor_receipt must be an object.")
        expected_fields = {
            "executor_id",
            "executor_definition_hash",
            "capability_id",
            "unit_id",
            "proposal_hash",
            "concurrency_mode",
            "expected_revision",
            "expected_parents",
            "workspace_revision_before",
            "workspace_revision_after",
            "artifact_refs",
            "postcondition_hashes",
            "output",
            "reconciled",
        }
        if set(value) != expected_fields:
            raise ValueError("executor_receipt fields are invalid.")
        result = ExecutorResult(
            executor_id=value.get("executor_id"),
            capability_id=value.get("capability_id"),
            unit_id=value.get("unit_id"),
            workspace_revision_before=value.get("workspace_revision_before"),
            workspace_revision_after=value.get("workspace_revision_after"),
            artifact_refs=value.get("artifact_refs"),
            applied_parents=value.get("expected_parents"),
            postcondition_hashes=value.get("postcondition_hashes"),
            output=value.get("output"),
        )
        receipt = cls(
            request=request,
            definition=definition,
            result=result,
            reconciled=value.get("reconciled"),
        )
        if receipt.to_dict() != dict(value):
            raise ValueError("executor_receipt identity does not match its request.")
        return receipt


class ExecutorRegistry:
    """Validated registry and common execution/reconciliation boundary."""

    def __init__(self) -> None:
        self._definitions: dict[str, ExecutorDefinition] = {}

    def register(self, definition: ExecutorDefinition) -> ExecutorDefinition:
        if not isinstance(definition, ExecutorDefinition):
            raise ValueError(
                "Executor registry entries must be ExecutorDefinition values."
            )
        if definition.executor_id in self._definitions:
            raise ValueError(
                f"Executor '{definition.executor_id}' is already registered."
            )
        definition.definition_hash
        self._definitions[definition.executor_id] = definition
        return definition

    def get(self, executor_id: str) -> ExecutorDefinition:
        try:
            return self._definitions[executor_id]
        except KeyError as error:
            raise ValueError(f"Unknown executor '{executor_id}'.") from error

    def all(self) -> tuple[ExecutorDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def _definition_for(self, request: ExecutorRequest) -> ExecutorDefinition:
        if not isinstance(request, ExecutorRequest):
            raise ExecutorContractError(
                "Executor execution requires an ExecutorRequest."
            )
        definition = self.get(request.executor_id)
        _validate_request_guard(request, definition)
        return definition

    def execute(self, request: ExecutorRequest, target: object) -> ExecutorReceipt:
        definition = self._definition_for(request)
        result = definition.implementation(request, target)
        validated = _validate_execution_contract(request, definition, result)
        return ExecutorReceipt(
            request=request,
            definition=definition,
            result=validated,
        )

    def reconcile(
        self, request: ExecutorRequest, target: object
    ) -> ExecutorReconciliation:
        definition = self._definition_for(request)
        outcome = definition.reconciler(request, target)
        if not isinstance(outcome, ExecutorReconciliation):
            raise ExecutorContractError(
                f"Executor '{definition.executor_id}' reconciler must return an "
                "ExecutorReconciliation."
        )
        if outcome.disposition == "already_applied":
            _validate_execution_contract(request, definition, outcome.result)
        return outcome

    def receipt_for_reconciliation(
        self,
        request: ExecutorRequest,
        reconciliation: ExecutorReconciliation,
    ) -> ExecutorReceipt:
        definition = self._definition_for(request)
        if not isinstance(reconciliation, ExecutorReconciliation):
            raise ExecutorContractError(
                "Reconciled receipt requires an ExecutorReconciliation."
            )
        if reconciliation.disposition != "already_applied":
            raise ExecutorContractError(
                "Only an already_applied reconciliation can produce a receipt."
            )
        result = _validate_execution_contract(
            request, definition, reconciliation.result
        )
        return ExecutorReceipt(
            request=request,
            definition=definition,
            result=result,
            reconciled=True,
        )


EXECUTORS = ExecutorRegistry()
