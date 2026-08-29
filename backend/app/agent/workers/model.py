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
import unicodedata
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

# Appended to bounded guidance whenever the list handed to a repair turn is
# shorter than the list the validator raised. Phrased as an instruction rather
# than a count alone: the useful response to "there are more" is to re-check the
# whole response, not to guess which items were withheld.
_TRUNCATION_NOTICE = (
    "{dropped} further validation error(s) were raised but not listed here. "
    "Re-check the whole response against every rule, not only the errors named "
    "above."
)


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
    """A model response failed the registered structured-response contract.

    A validator may attach ``partial``: the normalized proposal built from the
    parts of the response that *did* satisfy the contract. It changes nothing
    while repair turns remain — the loop still asks the model to correct the
    whole response — but when the allowance is exhausted the registry commits
    that subset instead of discarding the response wholesale. A validator is
    responsible for offering a partial only when the missing parts are not
    themselves a durable gap; omitting it keeps the strict all-or-nothing
    behaviour.
    """

    def __init__(
        self,
        errors: str | Iterable[str],
        *,
        partial: Mapping[str, Any] | None = None,
    ):
        values = (errors,) if isinstance(errors, str) else tuple(errors)
        normalized = tuple(str(item).strip() for item in values if str(item).strip())
        if not normalized:
            normalized = ("The response did not satisfy the registered schema.",)
        if partial is not None and not isinstance(partial, Mapping):
            raise ValueError("worker_response_validation.partial must be an object.")
        self.errors = normalized
        self.partial = partial
        super().__init__("; ".join(normalized))


_FENCED_JSON = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)
#: How much of the response to quote either side of a parse failure. Enough to
#: contain the malformed token and the structure around it; short enough that
#: the repair message stays readable next to the other errors.
_JSON_ERROR_WINDOW = 120


#: The four characters JSON accepts as whitespace. Named because ASCII space
#: is itself a ``Zs``, so the category test below would otherwise strip it.
_JSON_WHITESPACE = " \t\n\r"
#: Unicode categories a tokenizer emits that JSON has no place for: zero-width
#: and bidi format marks (``Cf``), the non-ASCII spaces and separators
#: (``Zs``/``Zl``/``Zp``) that look like whitespace without being any of the
#: four characters above, and the combining marks (``Mn``/``Mc``/``Me``) that
#: render as nothing at all with no base character to attach to. All three
#: kinds were observed in one run: 40 zero-width spaces and 5 combining dots
#: below, every one of them between a colon and its value.
_JSON_INVISIBLE_CATEGORIES = frozenset(
    {"Cf", "Zs", "Zl", "Zp", "Mn", "Mc", "Me"}
)


def _without_invisible_noise(value: str) -> str:
    r"""Drop invisible non-JSON characters sitting outside string literals.

    A model emitting ``"page":  \u200b1`` has produced something no parser
    accepts and no reader can see, so quoting the region back for repair asks
    it to correct a character it cannot observe — which it duly re-emits. Four
    of eight document analyses failed that way in one run, every call finishing
    cleanly on ``stop``.

    Only outside a string literal, and only these categories: inside a string
    the same character is content this has no business rewriting, and a
    document that genuinely prints one should extract with it intact. Valid
    JSON cannot carry them outside a string, so this is a no-op on anything
    that would have parsed.
    """

    out: list[str] = []
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif (
            char not in _JSON_WHITESPACE
            and unicodedata.category(char) in _JSON_INVISIBLE_CATEGORIES
        ):
            continue
        out.append(char)
    return "".join(out)


def decode_json_response(response: object) -> object:
    """Parse a fenced-or-bare JSON response, saying where it broke if it did.

    "The response is not a valid JSON object" is true and useless: it locates
    nothing, so a response with one misplaced brace in two thousand characters
    of escaped Polars code gets re-emitted with the same brace in the same
    place until the repair allowance runs out. That is what happened to a live
    splitting-risk row — three attempts, one stray ``}`` after the first step,
    every time. Quoting the decoder's own position and the text around it turns
    an unactionable retry into a correction the model can actually make.
    """
    value = str(response or "").strip()
    fenced = _FENCED_JSON.fullmatch(value)
    if fenced:
        value = fenced.group(1).strip()
    value = _without_invisible_noise(value)
    try:
        # ``strict=False`` accepts a raw control character inside a string
        # literal, which is the one malformation here that is unambiguous: a
        # newline the model failed to escape is a newline, and the alternative
        # is discarding a whole document analysis over how it was transported.
        # It relaxes nothing else — every other defect still raises below.
        return json.loads(value, strict=False)
    except json.JSONDecodeError as error:
        window = value[
            max(0, error.pos - _JSON_ERROR_WINDOW) : error.pos + _JSON_ERROR_WINDOW
        ]
        raise WorkerResponseValidationError(
            "the response is not a valid JSON object: "
            f"{error.msg} at character {error.pos}. The text around that "
            f"position reads: ...{window}... Re-emit the whole object with "
            "that region corrected; check the braces and brackets there close "
            "exactly what they opened, and that every quote and backslash "
            "inside a code string is escaped."
        ) from error


def submission_response(message: object, expected_tool: str) -> str:
    """Extract the one required submission from a forced tool call.

    Shared rather than copied per worker family: the sentinel below is what
    routes a missing or duplicated call into the ordinary repair loop, and two
    implementations of that would drift apart exactly where a stored proposal
    could not tell them apart.
    """

    if not isinstance(message, Mapping):
        return ""
    matches = [
        item
        for item in message.get("tool_calls") or []
        if isinstance(item, Mapping)
        and isinstance(item.get("function"), Mapping)
        and item["function"].get("name") == expected_tool
    ]
    if len(matches) == 1:
        arguments = matches[0]["function"].get("arguments")
        return arguments if isinstance(arguments, str) else json.dumps(arguments)
    # Do not silently accept JSON prose when this worker explicitly required a
    # tool call. Returning this sentinel routes the issue through the normal
    # bounded schema-repair loop rather than committing an unchecked response.
    return json.dumps({"_submission_error": f"Call {expected_tool} exactly once."})


class WorkerRunError(RuntimeError):
    """A worker exhausted its bounded response-repair allowance."""

    def __init__(
        self,
        worker_id: str,
        attempts: int,
        errors: tuple[str, ...],
        *,
        last_response: str,
    ):
        self.worker_id = worker_id
        self.attempts = attempts
        self.errors = errors
        # The rejected response is local run state, not part of the exception
        # message.  The unit pipeline persists it to an identity-bound sidecar
        # so a linked retry can correct this exact draft instead of regenerating
        # the artifact from scratch.
        self.last_response = str(last_response)
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
    previous_response: str | None = field(default=None, repr=False)

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
        previous = (
            str(self.previous_response)
            if self.previous_response is not None
            else None
        )
        if self.number == 1 and previous is not None:
            raise ValueError("The initial worker attempt cannot carry a previous response.")
        if self.number > 1 and not errors:
            raise ValueError("A worker repair attempt requires validation guidance.")
        object.__setattr__(self, "validation_errors", errors)
        object.__setattr__(self, "previous_response", previous)

    @property
    def is_repair(self) -> bool:
        return self.number > 1


@dataclass(frozen=True)
class WorkerRepairSeed:
    """A rejected response that an exact-identity retry may continue editing."""

    previous_response: str = field(repr=False)
    validation_errors: tuple[str, ...]

    def __post_init__(self) -> None:
        previous = str(self.previous_response or "")
        errors = tuple(str(item).strip() for item in self.validation_errors)
        if not previous:
            raise ValueError("worker_repair_seed.previous_response must be non-empty.")
        if not errors or any(not item for item in errors):
            raise ValueError(
                "worker_repair_seed.validation_errors must contain non-empty strings."
            )
        object.__setattr__(self, "previous_response", previous)
        object.__setattr__(self, "validation_errors", errors)


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
        """The guidance a repair turn carries, and how much of it was left out.

        The bound is the point: guidance has to fit a prompt. What does not
        fit still has to be *announced*, because a list handed over silently
        truncated reads as the complete set of what is wrong, and a model that
        corrects every item on it has then done everything it was asked and
        still fails the same gate. The workspace already applies this rule to
        the flagged rows a memo is shown — supplied of total, never as the set
        — and guidance is no different.
        """
        messages = [text for text in (str(raw).strip() for raw in errors) if text]
        if not messages:
            return (
                "The response did not satisfy the registered schema."[
                    : self.max_guidance_characters
                ],
            )
        # Reserved before filling rather than trimmed to fit afterwards: the
        # notice is the one line that must survive, and the full count bounds
        # the number that can finally be dropped, so the reservation is never
        # too small. Unspent reservation costs a few characters of guidance,
        # which is the cheaper failure by a wide margin. A budget too small to
        # hold the notice whole carries none of it — half a sentence saying
        # something was withheld is not something a reader can act on.
        reserve = len(_TRUNCATION_NOTICE.format(dropped=len(messages)))
        if len(messages) < 2 or reserve >= self.max_guidance_characters:
            reserve = 0
        bounded: list[str] = []
        remaining = self.max_guidance_characters - reserve
        for message in messages:
            if len(bounded) >= self.max_validation_errors or remaining <= 0:
                break
            message = message[:remaining]
            bounded.append(message)
            remaining -= len(message)
        if reserve and len(bounded) < len(messages):
            bounded.append(
                _TRUNCATION_NOTICE.format(dropped=len(messages) - len(bounded))
            )
        return tuple(bounded)

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
    """Hash-identified metadata and implementation for one model worker.

    The identity is *authored*, not derived from source text. An earlier design
    hashed ``inspect.getsource`` of the implementation and the semantic
    validator; that made comments and whitespace part of the worker's identity,
    so reformatting a docstring invalidated persisted proposals and forced a
    re-billed model call. What actually changes a worker's behaviour is its
    prompt, its response schema, and its repair policy — all of which are
    hashed below.
    """

    worker_id: str
    prompt_hash: str
    response_schema: WorkerResponseSchema
    repair_policy: WorkerRepairPolicy
    implementation: WorkerImplementation = field(repr=False, compare=False)
    required_model_capabilities: tuple[str, ...] = ()
    #: Whether this worker's response is a JSON document. True for every worker
    #: but the APM, whose contract is Markdown — its prompt asks for the
    #: memorandum "as Markdown only, without a JSON wrapper", and its schema
    #: reads a JSON envelope only as a legacy shape. Constraining that one to
    #: JSON produced a complete 16,000-character memorandum filed under a key
    #: the model chose for itself, which the template check then failed. Not
    #: derived from the response schema: every schema here can parse JSON, so
    #: parsing it is no evidence that JSON is what was asked for.
    json_response: bool = True
    semantic_validator: SemanticValidator | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "worker_id", _normalized_id(self.worker_id, "worker_definition.worker_id")
        )
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
        if self.semantic_validator is not None and not callable(
            self.semantic_validator
        ):
            raise ValueError("worker_definition.semantic_validator must be callable.")
        self.definition_hash

    def to_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "prompt_hash": self.prompt_hash,
            "response_schema_id": self.response_schema.schema_id,
            "response_schema_hash": self.response_schema.schema_hash,
            "semantic_validation": self.semantic_validator is not None,
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
    partial: bool
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
        partial: bool = False,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("worker_result.attempts must be a positive integer.")
        if not isinstance(partial, bool):
            raise ValueError("worker_result.partial must be a boolean.")
        object.__setattr__(self, "partial", partial)
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
        *,
        on_repair: Callable[[int, tuple[str, ...]], None] | None = None,
        repair_seed: WorkerRepairSeed | None = None,
    ) -> WorkerResult:
        if not isinstance(request, WorkerRequest):
            raise WorkerContractError("Worker execution requires a WorkerRequest.")
        if not isinstance(gateway, ModelGateway):
            raise WorkerContractError("Worker execution requires a ModelGateway.")
        definition = self.get(request.worker_id)
        if repair_seed is not None and not isinstance(repair_seed, WorkerRepairSeed):
            raise WorkerContractError("Worker repair seed is invalid.")
        if repair_seed is not None and not definition.repair_policy.max_repair_attempts:
            raise WorkerContractError(
                f"Worker '{definition.worker_id}' does not allow response repair."
            )
        errors = repair_seed.validation_errors if repair_seed is not None else ()
        previous_response = (
            repair_seed.previous_response if repair_seed is not None else None
        )
        # A linked retry starts at attempt two: the persisted draft is attempt
        # one, even though it was authored by the parent run.  It gets the same
        # bounded number of correction turns as an in-run repair, never a new
        # unconditioned generation.
        first_attempt = 2 if repair_seed is not None else 1
        call_count = (
            definition.repair_policy.max_repair_attempts
            if repair_seed is not None
            else 1 + definition.repair_policy.max_repair_attempts
        )
        attempt_numbers = range(first_attempt, first_attempt + call_count)
        final_attempt = first_attempt + call_count - 1
        for attempt_number in attempt_numbers:
            attempt = WorkerAttempt(attempt_number, errors, previous_response)
            gateway_context = getattr(gateway, "context", None)
            previous_capabilities = (
                getattr(gateway_context, "required_model_capabilities", None)
                if gateway_context is not None
                else None
            )
            previous_json_response = (
                getattr(gateway_context, "json_response", None)
                if gateway_context is not None
                else None
            )
            if gateway_context is not None:
                gateway_context.required_model_capabilities = (
                    definition.required_model_capabilities
                )
                # Scoped to the call rather than set on the gateway, for the
                # same reason capabilities are: the gateway is shared, and
                # neither a prose caller nor a Markdown worker must inherit a
                # JSON worker's constraint.
                gateway_context.json_response = definition.json_response
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
                    if previous_json_response is None:
                        try:
                            del gateway_context.json_response
                        except AttributeError:
                            pass
                    else:
                        gateway_context.json_response = previous_json_response
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
                # A repair is a correction of this exact response, not a fresh
                # generation that happens to know one thing the last response
                # got wrong. The worker decides how to present the prior text;
                # keeping it on the attempt leaves prompt structure local to
                # the registered worker.
                previous_response = response
                errors = definition.repair_policy.bounded_errors(error.errors)
                if attempt_number == final_attempt:
                    # The allowance is spent. Where the validator could salvage
                    # part of the response, committing it beats discarding work
                    # that satisfied the contract because a sibling did not.
                    if error.partial:
                        return WorkerResult(
                            worker_id=definition.worker_id,
                            capability_id=request.capability_id,
                            unit_id=request.unit_id,
                            proposal=_frozen_json(  # type: ignore[arg-type]
                                json.loads(
                                    _canonical_object(
                                        error.partial, "worker_result.partial"
                                    )
                                )
                            ),
                            attempts=attempt_number,
                            response_hash=_sha256_text(response),
                            response_schema_hash=definition.response_schema.schema_hash,
                            partial=True,
                        )
                    raise WorkerRunError(
                        definition.worker_id,
                        attempt_number,
                        errors,
                        last_response=response,
                    ) from error
                if on_repair is not None:
                    try:
                        on_repair(attempt_number, errors)
                    except Exception:
                        # A progress note must never break the repair it is
                        # describing.
                        pass
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
