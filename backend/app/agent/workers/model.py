"""Immutable contracts and registry for model-backed workflow workers.

Workers own prompt construction and response validation, but they do not own
scheduling, persistence, context retrieval, or workspace mutation.  The
registry invokes a worker through the shared :class:`ModelGateway` and applies
one common, bounded validation-and-repair loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..context import ContextBundle
from ..runtime.model_gateway import ModelGateway


_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_REPAIR_ATTEMPTS = 2
MAX_REPAIR_ERRORS = 20
MAX_REPAIR_GUIDANCE_CHARACTERS = 4_000


def _normalized_id(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string.")
    return text


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a sha256 hash.")
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


def _frozen_json(value: object) -> object:
    """Return a recursively immutable projection of normalized JSON data."""
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_json(item) for item in value)
    return value


class WorkerContractError(ValueError):
    """The registered worker implementation violated the worker contract."""


class WorkerResponseValidationError(ValueError):
    """A model response failed the registered structured-response contract."""

    def __init__(self, errors: str | Iterable[str]):
        values = (errors,) if isinstance(errors, str) else tuple(errors)
        normalized = tuple(str(item).strip() for item in values if str(item).strip())
        if not normalized:
            normalized = ("The response did not satisfy the registered schema.",)
        self.errors = normalized
        super().__init__("; ".join(normalized))


class WorkerRunError(RuntimeError):
    """A worker exhausted its bounded response-repair allowance."""

    def __init__(self, worker_id: str, attempts: int, errors: tuple[str, ...]):
        self.worker_id = worker_id
        self.attempts = attempts
        self.errors = errors
        super().__init__(
            f"Worker '{worker_id}' returned an invalid response after "
            f"{attempts} attempt(s): {'; '.join(errors)}"
        )


@dataclass(frozen=True, init=False)
class WorkerRequest:
    """Local-only, immutable input supplied to exactly one registered worker.

    Context and unit input are serialized on construction.  Accessors return a
    detached bundle and a recursively immutable input mapping, so later caller
    mutation cannot change the request seen by a worker.
    """

    worker_id: str
    capability_id: str
    unit_id: str
    _context_json: str = field(repr=False)
    _unit_input_json: str = field(repr=False)
    _activity_json: str = field(repr=False)

    def __init__(
        self,
        *,
        worker_id: str,
        capability_id: str,
        unit_id: str,
        context: ContextBundle,
        unit_input: Mapping[str, Any] | None = None,
        activity: Mapping[str, Any] | None = None,
    ) -> None:
        normalized_worker = _normalized_id(worker_id, "worker_request.worker_id")
        normalized_capability = _normalized_id(
            capability_id, "worker_request.capability_id"
        )
        normalized_unit = _normalized_id(unit_id, "worker_request.unit_id")
        if not isinstance(context, ContextBundle):
            raise ValueError("worker_request.context must be a ContextBundle.")
        if context.capability_id != normalized_capability:
            raise ValueError(
                "worker_request.context capability does not match capability_id."
            )
        if context.unit_id != normalized_unit:
            raise ValueError("worker_request.context unit does not match unit_id.")
        normalized_input = {} if unit_input is None else unit_input
        normalized_activity = {} if activity is None else activity
        object.__setattr__(self, "worker_id", normalized_worker)
        object.__setattr__(self, "capability_id", normalized_capability)
        object.__setattr__(self, "unit_id", normalized_unit)
        object.__setattr__(self, "_context_json", context.to_json())
        object.__setattr__(
            self,
            "_unit_input_json",
            _canonical_object(normalized_input, "worker_request.unit_input"),
        )
        object.__setattr__(
            self,
            "_activity_json",
            _canonical_object(normalized_activity, "worker_request.activity"),
        )

    @property
    def context(self) -> ContextBundle:
        return ContextBundle.from_json(self._context_json)

    @property
    def unit_input(self) -> Mapping[str, Any]:
        return _frozen_json(json.loads(self._unit_input_json))  # type: ignore[return-value]

    @property
    def activity(self) -> Mapping[str, Any]:
        return _frozen_json(json.loads(self._activity_json))  # type: ignore[return-value]


@dataclass(frozen=True)
class WorkerAttempt:
    """One initial or repair invocation supplied to a worker implementation."""

    number: int
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise ValueError("worker_attempt.number must be a positive integer.")
        errors = tuple(str(item).strip() for item in self.validation_errors)
        if any(not item for item in errors):
            raise ValueError(
                "worker_attempt.validation_errors must contain non-empty strings."
            )
        if self.number == 1 and errors:
            raise ValueError("The initial worker attempt cannot contain repair guidance.")
        if self.number > 1 and not errors:
            raise ValueError("A worker repair attempt requires validation guidance.")
        object.__setattr__(self, "validation_errors", errors)

    @property
    def is_repair(self) -> bool:
        return self.number > 1


@dataclass(frozen=True)
class WorkerRepairPolicy:
    """Hash-identified hard bounds for response-repair turns and guidance."""

    max_repair_attempts: int
    guidance_hash: str | None = None
    max_validation_errors: int = 8
    max_guidance_characters: int = 2_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_repair_attempts, bool)
            or not isinstance(self.max_repair_attempts, int)
            or not 0 <= self.max_repair_attempts <= MAX_REPAIR_ATTEMPTS
        ):
            raise ValueError(
                f"repair_policy.max_repair_attempts must be between 0 and "
                f"{MAX_REPAIR_ATTEMPTS}."
            )
        if self.max_repair_attempts:
            _require_hash(self.guidance_hash, "repair_policy.guidance_hash")
        elif self.guidance_hash is not None:
            raise ValueError(
                "repair_policy.guidance_hash requires at least one repair attempt."
            )
        if (
            isinstance(self.max_validation_errors, bool)
            or not isinstance(self.max_validation_errors, int)
            or not 1 <= self.max_validation_errors <= MAX_REPAIR_ERRORS
        ):
            raise ValueError(
                f"repair_policy.max_validation_errors must be between 1 and "
                f"{MAX_REPAIR_ERRORS}."
            )
        if (
            isinstance(self.max_guidance_characters, bool)
            or not isinstance(self.max_guidance_characters, int)
            or not 1
            <= self.max_guidance_characters
            <= MAX_REPAIR_GUIDANCE_CHARACTERS
        ):
            raise ValueError(
                "repair_policy.max_guidance_characters must be between 1 and "
                f"{MAX_REPAIR_GUIDANCE_CHARACTERS}."
            )

    def bounded_errors(self, errors: Iterable[str]) -> tuple[str, ...]:
        bounded: list[str] = []
        remaining = self.max_guidance_characters
        for raw in errors:
            if len(bounded) >= self.max_validation_errors or remaining <= 0:
                break
            message = str(raw).strip()
            if not message:
                continue
            message = message[:remaining]
            bounded.append(message)
            remaining -= len(message)
        return tuple(bounded) or (
            "The response did not satisfy the registered schema."[
                : self.max_guidance_characters
            ],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "max_repair_attempts": self.max_repair_attempts,
            "guidance_hash": self.guidance_hash,
            "max_validation_errors": self.max_validation_errors,
            "max_guidance_characters": self.max_guidance_characters,
        }


ResponseValidator = Callable[[str], Mapping[str, Any]]
SemanticValidator = Callable[[Mapping[str, Any], WorkerRequest], Mapping[str, Any]]


@dataclass(frozen=True)
class WorkerResponseSchema:
    """Registered structured-response validator and its authored identity."""

    schema_id: str
    schema_hash: str
    validator: ResponseValidator = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schema_id", _normalized_id(self.schema_id, "response_schema.schema_id")
        )
        _require_hash(self.schema_hash, "response_schema.schema_hash")
        if not callable(self.validator):
            raise ValueError("response_schema.validator must be callable.")

    def validate(self, response: str) -> Mapping[str, Any]:
        try:
            proposal = self.validator(response)
        except WorkerContractError:
            raise
        except WorkerResponseValidationError:
            raise
        except ValueError as error:
            raise WorkerResponseValidationError(str(error)) from error
        if not isinstance(proposal, Mapping):
            raise WorkerContractError(
                f"Response schema '{self.schema_id}' must return an object proposal."
            )
        try:
            encoded = _canonical_object(proposal, "worker_result.proposal")
        except ValueError as error:
            raise WorkerContractError(
                f"Response schema '{self.schema_id}' returned an invalid proposal: "
                f"{error}"
            ) from error
        return _frozen_json(json.loads(encoded))  # type: ignore[return-value]


WorkerImplementation = Callable[[WorkerRequest, ModelGateway, WorkerAttempt], str]


@dataclass(frozen=True)
class WorkerDefinition:
    """Hash-identified metadata and implementation for one model worker."""

    worker_id: str
    implementation_hash: str
    prompt_hash: str
    response_schema: WorkerResponseSchema
    repair_policy: WorkerRepairPolicy
    implementation: WorkerImplementation = field(repr=False, compare=False)
    required_model_capabilities: tuple[str, ...] = ()
    semantic_validation_hash: str | None = None
    semantic_validator: SemanticValidator | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "worker_id", _normalized_id(self.worker_id, "worker_definition.worker_id")
        )
        _require_hash(self.implementation_hash, "worker_definition.implementation_hash")
        _require_hash(self.prompt_hash, "worker_definition.prompt_hash")
        if not isinstance(self.response_schema, WorkerResponseSchema):
            raise ValueError(
                "worker_definition.response_schema must be a WorkerResponseSchema."
            )
        if not isinstance(self.repair_policy, WorkerRepairPolicy):
            raise ValueError(
                "worker_definition.repair_policy must be a WorkerRepairPolicy."
            )
        if not callable(self.implementation):
            raise ValueError("worker_definition.implementation must be callable.")
        capabilities = tuple(
            sorted(
                {
                    _normalized_id(
                        value,
                        "worker_definition.required_model_capabilities",
                    )
                    for value in self.required_model_capabilities
                }
            )
        )
        unknown = [value for value in capabilities if value not in {"vision"}]
        if unknown:
            raise ValueError(
                f"Unknown required model capability '{unknown[0]}'."
            )
        object.__setattr__(self, "required_model_capabilities", capabilities)
        if self.semantic_validator is None:
            if self.semantic_validation_hash is not None:
                raise ValueError(
                    "worker_definition.semantic_validation_hash requires a validator."
                )
        else:
            if not callable(self.semantic_validator):
                raise ValueError(
                    "worker_definition.semantic_validator must be callable."
                )
            _require_hash(
                self.semantic_validation_hash,
                "worker_definition.semantic_validation_hash",
            )
        self.definition_hash

    def to_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "implementation_hash": self.implementation_hash,
            "prompt_hash": self.prompt_hash,
            "response_schema_id": self.response_schema.schema_id,
            "response_schema_hash": self.response_schema.schema_hash,
            "semantic_validation_hash": self.semantic_validation_hash,
            "required_model_capabilities": list(
                self.required_model_capabilities
            ),
            "repair_policy": self.repair_policy.to_dict(),
        }

    @property
    def definition_hash(self) -> str:
        return _sha256_text(_canonical_json(self.to_dict(), "worker_definition"))


@dataclass(frozen=True, init=False)
class WorkerResult:
    """Validated immutable proposal returned by a registered worker."""

    worker_id: str
    capability_id: str
    unit_id: str
    attempts: int
    response_hash: str
    response_schema_hash: str
    _proposal_json: str = field(repr=False)

    def __init__(
        self,
        *,
        worker_id: str,
        capability_id: str,
        unit_id: str,
        proposal: Mapping[str, Any],
        attempts: int,
        response_hash: str,
        response_schema_hash: str,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("worker_result.attempts must be a positive integer.")
        object.__setattr__(self, "worker_id", _normalized_id(worker_id, "worker_result.worker_id"))
        object.__setattr__(
            self,
            "capability_id",
            _normalized_id(capability_id, "worker_result.capability_id"),
        )
        object.__setattr__(self, "unit_id", _normalized_id(unit_id, "worker_result.unit_id"))
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "response_hash", _require_hash(response_hash, "worker_result.response_hash"))
        object.__setattr__(
            self,
            "response_schema_hash",
            _require_hash(response_schema_hash, "worker_result.response_schema_hash"),
        )
        object.__setattr__(
            self,
            "_proposal_json",
            _canonical_object(proposal, "worker_result.proposal"),
        )

    @property
    def proposal(self) -> Mapping[str, Any]:
        return _frozen_json(json.loads(self._proposal_json))  # type: ignore[return-value]

    @property
    def repaired(self) -> bool:
        return self.attempts > 1


class WorkerRegistry:
    """Validated registry and common bounded execution boundary for workers."""

    def __init__(self) -> None:
        self._definitions: dict[str, WorkerDefinition] = {}

    def register(self, definition: WorkerDefinition) -> WorkerDefinition:
        if not isinstance(definition, WorkerDefinition):
            raise ValueError("Worker registry entries must be WorkerDefinition values.")
        if definition.worker_id in self._definitions:
            raise ValueError(f"Worker '{definition.worker_id}' is already registered.")
        definition.definition_hash
        self._definitions[definition.worker_id] = definition
        return definition

    def get(self, worker_id: str) -> WorkerDefinition:
        try:
            return self._definitions[worker_id]
        except KeyError as error:
            raise ValueError(f"Unknown worker '{worker_id}'.") from error

    def all(self) -> tuple[WorkerDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def execute(
        self,
        request: WorkerRequest,
        gateway: ModelGateway,
    ) -> WorkerResult:
        if not isinstance(request, WorkerRequest):
            raise WorkerContractError("Worker execution requires a WorkerRequest.")
        if not isinstance(gateway, ModelGateway):
            raise WorkerContractError("Worker execution requires a ModelGateway.")
        definition = self.get(request.worker_id)
        errors: tuple[str, ...] = ()
        total_attempts = 1 + definition.repair_policy.max_repair_attempts
        for attempt_number in range(1, total_attempts + 1):
            attempt = WorkerAttempt(attempt_number, errors)
            gateway_context = getattr(gateway, "context", None)
            previous_capabilities = (
                getattr(gateway_context, "required_model_capabilities", None)
                if gateway_context is not None
                else None
            )
            if gateway_context is not None:
                gateway_context.required_model_capabilities = (
                    definition.required_model_capabilities
                )
            try:
                response = definition.implementation(request, gateway, attempt)
            finally:
                if gateway_context is not None:
                    if previous_capabilities is None:
                        try:
                            del gateway_context.required_model_capabilities
                        except AttributeError:
                            pass
                    else:
                        gateway_context.required_model_capabilities = (
                            previous_capabilities
                        )
            if not isinstance(response, str):
                raise WorkerContractError(
                    f"Worker '{definition.worker_id}' must return response text."
                )
            try:
                proposal = definition.response_schema.validate(response)
                if definition.semantic_validator is not None:
                    proposal = definition.semantic_validator(proposal, request)
                    encoded = _canonical_object(
                        proposal, "worker_result.semantic_proposal"
                    )
                    proposal = _frozen_json(json.loads(encoded))  # type: ignore[assignment]
            except WorkerResponseValidationError as error:
                errors = definition.repair_policy.bounded_errors(error.errors)
                if attempt_number == total_attempts:
                    raise WorkerRunError(
                        definition.worker_id,
                        attempt_number,
                        errors,
                    ) from error
                continue
            return WorkerResult(
                worker_id=definition.worker_id,
                capability_id=request.capability_id,
                unit_id=request.unit_id,
                proposal=proposal,
                attempts=attempt_number,
                response_hash=_sha256_text(response),
                response_schema_hash=definition.response_schema.schema_hash,
            )
        raise AssertionError("The bounded worker loop did not terminate.")


WORKERS = WorkerRegistry()
