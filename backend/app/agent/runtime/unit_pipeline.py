"""Runner-independent sequencing for one model-backed workflow unit."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ...workspaces import WorkspaceError, write_json_atomic
from .. import store
from ..context import ContextBundle, ContextManifest
from ..executors import (
    ExecutorDefinition,
    ExecutorReceipt,
    ExecutorRegistry,
    ExecutorRequest,
)
from ..workers import (
    WorkerRegistry,
    WorkerRepairSeed,
    WorkerRequest,
    WorkerRunError,
)
from .model_gateway import ModelGateway
from .run_runtime import RunRuntime


class UnitPipelineError(RuntimeError):
    """A unit could not complete the declared pipeline contract."""


class UnitPipelineConflict(UnitPipelineError):
    """Recovery found a changed parent or committed target."""

    def __init__(
        self,
        message: str,
        *,
        manifest_reference: Mapping[str, str] | None = None,
        proposal_reference: Mapping[str, str] | None = None,
        receipt_reference: Mapping[str, str] | None = None,
        proposal_reuse_rejection_reasons: tuple[str, ...] = (),
    ) -> None:
        self.manifest_reference = manifest_reference
        self.proposal_reference = proposal_reference
        self.receipt_reference = receipt_reference
        self.proposal_reuse_rejection_reasons = proposal_reuse_rejection_reasons
        super().__init__(message)


class UnitSidecarValidationError(WorkspaceError):
    """A semantic-unit sidecar failed shape or integrity validation."""


_SHA256_PREFIX = "sha256:"


def _require_hash(value: object, field_name: str) -> str:
    text = str(value or "")
    if (
        not text.startswith(_SHA256_PREFIX)
        or len(text) != len(_SHA256_PREFIX) + 64
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise ValueError(f"{field_name} must be a sha256 hash.")
    return text


def _mapping_default(value: object) -> object:
    # Frozen proposals wrap nested objects in MappingProxyType; unwrap them for
    # canonical serialization while still rejecting non-JSON values.
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _canonical_json(value: object) -> str:
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
        raise ValueError("Unit sidecar payload must be JSON-compatible.") from error


def _sha256(value: object) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _unit_filename(unit_id: str) -> str:
    value = str(unit_id or "").strip()
    if not value:
        raise ValueError("Pipeline unit_id must be non-empty.")
    filename = quote(value, safe="._-")
    if filename in {".", ".."}:
        filename = "".join(f"%{byte:02X}" for byte in value.encode("utf-8"))
    return f"{filename}.json"


class UnitSidecarStore:
    """Atomic proposal and receipt persistence using semantic unit paths."""

    def __init__(self, workspace: object, run_id: str):
        self.workspace = workspace
        self.run_id = str(run_id or "").strip()
        if not self.run_id:
            raise ValueError("Unit sidecar store requires a run_id.")

    def _folder(self, kind: str) -> Path:
        run_folder = store.run_dir(self.workspace, self.run_id)  # type: ignore[arg-type]
        if (
            run_folder.parent != store.runs_dir(self.workspace)  # type: ignore[arg-type]
            or run_folder.name != self.run_id
            or not (run_folder / "run.json").is_file()
        ):
            raise WorkspaceError(f"Agent run '{self.run_id}' not found.")
        return run_folder / kind

    def _persist(
        self,
        kind: str,
        unit_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, str]:
        normalized = json.loads(_canonical_json(dict(payload)))
        path = self._folder(kind) / _unit_filename(unit_id)
        write_json_atomic(path, normalized)
        return {
            "path": f"{kind}/{path.name}",
            "unit_id": unit_id,
            "payload_hash": _sha256(normalized),
        }

    def persist_proposal(
        self,
        unit_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, str]:
        return self._persist("proposals", unit_id, payload)

    def persist_rejection(
        self,
        unit_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, str]:
        """Persist the final invalid worker response for an exact linked retry."""

        return self._persist("rejections", unit_id, payload)

    def _load(
        self,
        kind: str,
        unit_id: str,
        reference: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        path = self._folder(kind) / _unit_filename(unit_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UnitSidecarValidationError(
                f"Unit {kind[:-1]} sidecar is invalid."
            ) from error
        if not isinstance(value, dict):
            raise UnitSidecarValidationError(
                f"Unit {kind[:-1]} sidecar must contain an object."
            )
        if reference is not None:
            expected_path = f"{kind}/{path.name}"
            if (
                reference.get("path") != expected_path
                or reference.get("unit_id") != unit_id
                or reference.get("payload_hash") != _sha256(value)
            ):
                raise UnitSidecarValidationError(
                    f"Unit {kind[:-1]} sidecar reference is incompatible."
                )
        return value

    def load_proposal(
        self,
        unit_id: str,
        reference: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any] | None:
        return self._load("proposals", unit_id, reference)

    def load_rejection(
        self,
        unit_id: str,
        reference: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any] | None:
        return self._load("rejections", unit_id, reference)

    def read_receipt_record(
        self,
        unit_id: str,
        reference: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Return a persisted receipt as stored, without rebuilding it.

        Reconstructing an :class:`ExecutorReceipt` needs the request and
        definition that produced it, which a read-only caller such as the
        provenance API does not have. This still validates the reference, so a
        mismatched or unreadable sidecar raises rather than returning a partial
        record.
        """
        return self._load("receipts", unit_id, reference)

    def persist_receipt(
        self,
        unit_id: str,
        receipt: ExecutorReceipt,
    ) -> dict[str, str]:
        if not isinstance(receipt, ExecutorReceipt):
            raise ValueError("Receipt persistence requires an ExecutorReceipt.")
        payload = {**receipt.to_dict(), "receipt_hash": receipt.receipt_hash}
        reference = self._persist("receipts", unit_id, payload)
        reference["receipt_hash"] = receipt.receipt_hash
        return reference

    def load_receipt(
        self,
        unit_id: str,
        *,
        request: ExecutorRequest,
        definition: ExecutorDefinition,
        reference: Mapping[str, str] | None = None,
    ) -> ExecutorReceipt | None:
        payload = self._load("receipts", unit_id, reference)
        if payload is None:
            return None
        receipt_hash = payload.pop("receipt_hash", None)
        try:
            receipt = ExecutorReceipt.from_dict(
                payload,
                request=request,
                definition=definition,
            )
        except (TypeError, ValueError) as error:
            raise UnitSidecarValidationError(
                "Unit receipt sidecar contract is invalid."
            ) from error
        if receipt_hash != receipt.receipt_hash:
            raise UnitSidecarValidationError(
                "Unit receipt sidecar hash does not match its payload."
            )
        if reference is not None and reference.get("receipt_hash") != receipt.receipt_hash:
            raise UnitSidecarValidationError(
                "Unit receipt reference hash does not match its payload."
            )
        return receipt


@dataclass(frozen=True)
class UnitPipelineRequest:
    capability_id: str
    unit_id: str
    worker_id: str
    # ``None`` declares a proposal-only unit: one whose durable outcome is the
    # persisted proposal itself, consumed by a later unit rather than committed.
    # The manifest-before-model-call and exact-identity proposal reuse guarantees
    # are unchanged; there is simply no mutation, and therefore no receipt.
    executor_id: str | None
    unit_input: Mapping[str, Any]
    activity: Mapping[str, Any]
    expected_revision: int
    expected_parents: Mapping[str, str]
    capability_definition_hash: str
    approval_kind: str | None = None
    proposal_reference: Mapping[str, str] | None = None
    receipt_reference: Mapping[str, str] | None = None
    model_profile_hash: str | None = None
    input_modalities: tuple[str, ...] = ()
    prepared_media_hashes: tuple[str, ...] = ()
    media_policy_hash: str | None = None


@dataclass(frozen=True)
class UnitPipelineOutcome:
    manifest_reference: Mapping[str, str]
    proposal_reference: Mapping[str, str]
    receipt_reference: Mapping[str, str] | None
    receipt: ExecutorReceipt | None
    readiness: object | None
    status: str
    proposal_reused: bool = False
    proposal_reuse_rejection_reasons: tuple[str, ...] = ()
    receipt_reused: bool = False
    executor_reconciled: bool = False


@dataclass(frozen=True)
class ProposalExecutionIdentity:
    """Exact identity of one uncommitted model proposal execution.

    Every field here is an independent chance to reject a reusable proposal and
    re-bill the provider, so the set is kept to what carries information the
    others do not. Four fields were dropped as provably contained in
    ``context_manifest_hash``, which is the hash of the manifest JSON:
    ``context_spec_hash`` and ``resolver_hash`` are manifest members (and are
    still checked against the manifest in :meth:`UnitPipeline.run`), and
    ``selector_definition_hashes`` and ``selected_source_hashes`` are carried
    per selection inside it. Three more were dropped as contained in
    ``worker_definition_hash``, which hashes a dict holding the prompt hash,
    the response-schema hash, and — before it was removed entirely — the
    implementation hash.
    """

    capability_definition_hash: str
    unit_input_hash: str
    context_manifest_hash: str
    worker_definition_hash: str
    model_profile_hash: str = field(
        default_factory=lambda: _sha256({"profile": "unspecified"})
    )
    input_modalities: tuple[str, ...] = ("text",)
    prepared_media_hashes: tuple[str, ...] = ()
    media_policy_hash: str = field(
        default_factory=lambda: _sha256({"media_policy_hashes": []})
    )

    def __post_init__(self) -> None:
        scalar_fields = (
            "capability_definition_hash",
            "unit_input_hash",
            "context_manifest_hash",
            "worker_definition_hash",
            "model_profile_hash",
            "media_policy_hash",
        )
        for name in scalar_fields:
            object.__setattr__(self, name, _require_hash(getattr(self, name), name))
        for name in ("prepared_media_hashes",):
            values = tuple(_require_hash(value, name) for value in getattr(self, name))
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be unique and sorted.")
            object.__setattr__(self, name, values)
        modalities = tuple(sorted(set(str(value) for value in self.input_modalities)))
        if not modalities or any(value not in {"text", "image"} for value in modalities):
            raise ValueError("input_modalities must contain text and/or image.")
        object.__setattr__(self, "input_modalities", modalities)

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_definition_hash": self.capability_definition_hash,
            "unit_input_hash": self.unit_input_hash,
            "context_manifest_hash": self.context_manifest_hash,
            "worker_definition_hash": self.worker_definition_hash,
            "model_profile_hash": self.model_profile_hash,
            "input_modalities": list(self.input_modalities),
            "prepared_media_hashes": list(self.prepared_media_hashes),
            "media_policy_hash": self.media_policy_hash,
        }

    @property
    def identity_hash(self) -> str:
        return _sha256(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProposalExecutionIdentity":
        if not isinstance(value, Mapping):
            raise ValueError("Proposal execution identity must be an object.")
        expected = {
            "capability_definition_hash",
            "unit_input_hash",
            "context_manifest_hash",
            "worker_definition_hash",
            "model_profile_hash",
            "input_modalities",
            "prepared_media_hashes",
            "media_policy_hash",
        }
        if set(value) != expected:
            raise ValueError("Proposal execution identity fields are invalid.")
        return cls(
            **{
                **dict(value),
                "input_modalities": tuple(value["input_modalities"]),
                "prepared_media_hashes": tuple(value["prepared_media_hashes"]),
            }
        )

    def rejection_reasons(
        self,
        persisted: "ProposalExecutionIdentity",
    ) -> tuple[str, ...]:
        fields = (
            ("capability_definition_hash", "capability_definition_changed"),
            ("unit_input_hash", "unit_input_changed"),
            ("context_manifest_hash", "exact_context_changed"),
            ("worker_definition_hash", "worker_definition_changed"),
            ("model_profile_hash", "model_profile_changed"),
            ("input_modalities", "input_modalities_changed"),
            ("prepared_media_hashes", "prepared_media_changed"),
            ("media_policy_hash", "media_policy_changed"),
        )
        return tuple(
            reason
            for field_name, reason in fields
            if getattr(self, field_name) != getattr(persisted, field_name)
        )


ContextProvider = Callable[[], tuple[ContextManifest, ContextBundle]]
ApprovalProvider = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
ReadinessProvider = Callable[[], object]
ContextIdentityProvider = Callable[[ContextManifest], Mapping[str, Any]]
PersistenceObserver = Callable[[Mapping[str, str]], None]


class UnitPipeline:
    """Sequence one unit without depending on either scheduler implementation."""

    def __init__(
        self,
        *,
        runtime: RunRuntime,
        gateway: ModelGateway,
        workers: WorkerRegistry,
        executors: ExecutorRegistry,
        sidecars: UnitSidecarStore,
    ) -> None:
        if not isinstance(runtime, RunRuntime):
            raise ValueError("Unit pipeline requires a RunRuntime.")
        if not isinstance(gateway, ModelGateway):
            raise ValueError("Unit pipeline requires a ModelGateway.")
        if not isinstance(workers, WorkerRegistry):
            raise ValueError("Unit pipeline requires a WorkerRegistry.")
        if not isinstance(executors, ExecutorRegistry):
            raise ValueError("Unit pipeline requires an ExecutorRegistry.")
        if not isinstance(sidecars, UnitSidecarStore):
            raise ValueError("Unit pipeline requires a UnitSidecarStore.")
        self.runtime = runtime
        self.gateway = gateway
        self.workers = workers
        self.executors = executors
        self.sidecars = sidecars

    def _linked_repair_seed(
        self,
        *,
        request: UnitPipelineRequest,
        execution_identity: ProposalExecutionIdentity,
    ) -> WorkerRepairSeed | None:
        """Load an exact-identity rejected response from the direct parent run."""

        run = getattr(self.runtime, "run", None)
        parent_run_id = (
            str(run.get("parent_run_id") or "").strip()
            if isinstance(run, Mapping)
            else ""
        )
        if not parent_run_id:
            return None
        try:
            payload = UnitSidecarStore(
                self.sidecars.workspace, parent_run_id
            ).load_rejection(request.unit_id)
        except (WorkspaceError, UnitSidecarValidationError):
            return None
        if not isinstance(payload, Mapping):
            return None
        try:
            persisted_identity = ProposalExecutionIdentity.from_dict(
                payload.get("execution_identity")
            )
        except (TypeError, ValueError):
            return None
        if execution_identity.rejection_reasons(persisted_identity):
            return None
        if payload.get("execution_identity_hash") != persisted_identity.identity_hash:
            return None
        if payload.get("status") != "rejected":
            return None
        if payload.get("capability_id") != request.capability_id:
            return None
        if payload.get("unit_id") != request.unit_id:
            return None
        if payload.get("worker_id") != request.worker_id:
            return None
        response = payload.get("response")
        raw_errors = payload.get("validation_errors")
        if not isinstance(response, str) or not isinstance(raw_errors, list):
            return None
        if payload.get("response_hash") != _sha256_text(response):
            return None
        definition = self.workers.get(request.worker_id)
        if not definition.repair_policy.max_repair_attempts:
            return None
        if payload.get("response_schema_hash") != definition.response_schema.schema_hash:
            return None
        try:
            return WorkerRepairSeed(
                previous_response=response,
                validation_errors=definition.repair_policy.bounded_errors(raw_errors),
            )
        except ValueError:
            return None

    def commit_local(
        self,
        request: UnitPipelineRequest,
        *,
        proposal: Mapping[str, Any],
        target: object,
        origin: str = "deterministic",
        on_proposal_persisted: PersistenceObserver | None = None,
        on_receipt_persisted: PersistenceObserver | None = None,
    ) -> UnitPipelineOutcome:
        """Commit a locally derived proposal with the model path's durability.

        A unit whose proposal needs no provider turn still gets the same
        guarantees: the accepted proposal is persisted before the mutation, an
        interrupted commit is reconciled rather than repeated, and the receipt is
        written to the same ``receipts/<unit_id>.json`` sidecar. There is no
        context manifest because no context was resolved and no model was called.
        """
        if not isinstance(request, UnitPipelineRequest):
            raise UnitPipelineError("Unit pipeline requires a UnitPipelineRequest.")
        if request.executor_id is None:
            raise UnitPipelineError("A local commit requires an executor.")
        payload = dict(proposal)
        executor_request = ExecutorRequest(
            executor_id=request.executor_id,
            capability_id=request.capability_id,
            unit_id=request.unit_id,
            proposal=payload,
            expected_revision=request.expected_revision,
            expected_parents=request.expected_parents,
            activity=request.activity,
        )
        proposal_reference = self.sidecars.persist_proposal(
            request.unit_id,
            {
                "capability_id": request.capability_id,
                "unit_id": request.unit_id,
                "status": "accepted",
                "origin": origin,
                "proposal_hash": executor_request.proposal_hash,
                "proposal": payload,
            },
        )
        if on_proposal_persisted is not None:
            on_proposal_persisted(proposal_reference)
        reconciliation = self.executors.reconcile(executor_request, target)
        if reconciliation.disposition == "conflict":
            raise UnitPipelineConflict(
                reconciliation.reason or "Executor reconciliation found a conflict.",
                proposal_reference=proposal_reference,
            )
        executor_reconciled = reconciliation.disposition == "already_applied"
        if executor_reconciled:
            receipt = self.executors.receipt_for_reconciliation(
                executor_request, reconciliation
            )
        else:
            receipt = self.executors.execute(executor_request, target)
        receipt_reference = self.sidecars.persist_receipt(request.unit_id, receipt)
        if on_receipt_persisted is not None:
            on_receipt_persisted(receipt_reference)
        return UnitPipelineOutcome(
            manifest_reference={},
            proposal_reference=proposal_reference,
            receipt_reference=receipt_reference,
            receipt=receipt,
            readiness=None,
            status="succeeded",
            executor_reconciled=executor_reconciled,
        )

    def run(
        self,
        request: UnitPipelineRequest,
        *,
        context_provider: ContextProvider,
        target: object,
        context_identity_provider: ContextIdentityProvider,
        approval_provider: ApprovalProvider | None = None,
        readiness_provider: ReadinessProvider | None = None,
        on_manifest_persisted: PersistenceObserver | None = None,
        on_manifest_resolved: Callable[[ContextManifest], None] | None = None,
        on_proposal_persisted: PersistenceObserver | None = None,
        on_receipt_persisted: PersistenceObserver | None = None,
        on_worker_repaired: Callable[[int, tuple[str, ...]], None] | None = None,
        on_worker_completed: Callable[[Mapping[str, Any]], None] | None = None,
        on_rejection_persisted: PersistenceObserver | None = None,
    ) -> UnitPipelineOutcome:
        if not isinstance(request, UnitPipelineRequest):
            raise UnitPipelineError("Unit pipeline requires a UnitPipelineRequest.")
        manifest, bundle = context_provider()
        if not isinstance(manifest, ContextManifest) or not isinstance(
            bundle, ContextBundle
        ):
            raise UnitPipelineError(
                "Context provider must return ContextManifest and ContextBundle."
            )
        if (
            manifest.capability_id != request.capability_id
            or bundle.capability_id != request.capability_id
            or manifest.unit_id != request.unit_id
            or bundle.unit_id != request.unit_id
        ):
            raise UnitPipelineError(
                "Resolved context identity does not match the pipeline unit."
            )
        manifest_reference = self.runtime.persist_context_manifest(manifest)
        if on_manifest_persisted is not None:
            on_manifest_persisted(manifest_reference)
        worker_definition = self.workers.get(request.worker_id)
        context_identity = context_identity_provider(manifest)
        if not isinstance(context_identity, Mapping):
            raise UnitPipelineError("Context execution identity must be an object.")
        context_manifest_hash = (
            context_identity.get("context_manifest_hash")
            or context_identity.get("manifest_hash")
        )
        if context_manifest_hash != manifest.manifest_hash:
            raise UnitPipelineError("Context execution identity manifest hash is invalid.")
        if context_identity.get("context_spec_hash") != manifest.context_spec_hash:
            raise UnitPipelineError("Context execution identity spec hash is invalid.")
        if context_identity.get("resolver_hash") != manifest.resolver_hash:
            raise UnitPipelineError("Context execution identity resolver hash is invalid.")
        # Selector definitions and selected sources are still validated as
        # well-formed hashes, but they are not carried on the identity: both are
        # recorded per selection inside the manifest, so ``context_manifest_hash``
        # already commits to them.
        for value in context_identity.get("selector_definition_hashes") or []:
            _require_hash(
                value.get("definition_hash") if isinstance(value, Mapping) else value,
                "selector_definition_hashes",
            )
        image_handles = [
            item.content
            for item in bundle.items
            if item.representation.kind == "page_image"
            and isinstance(item.content, Mapping)
        ]
        prepared_media_hashes = tuple(
            sorted(
                {
                    _require_hash(
                        value,
                        "prepared_media_hashes",
                    )
                    for value in (
                        request.prepared_media_hashes
                        or tuple(
                            str(item.get("prepared_sha256") or "")
                            for item in image_handles
                        )
                    )
                }
            )
        )
        policy_hashes = tuple(
            sorted(
                {
                    _require_hash(
                        str(item.get("policy_hash") or ""),
                        "media_policy_hash",
                    )
                    for item in image_handles
                }
            )
        )
        media_policy_hash = (
            _require_hash(request.media_policy_hash, "media_policy_hash")
            if request.media_policy_hash is not None
            else (
                policy_hashes[0]
                if len(policy_hashes) == 1
                else _sha256({"media_policy_hashes": list(policy_hashes)})
            )
        )
        input_modalities = tuple(request.input_modalities) or (
            ("text", "image") if image_handles else ("text",)
        )
        model_profile_hash = request.model_profile_hash
        if model_profile_hash is None:
            run = getattr(self.gateway, "run", {})
            profiles = run.get("model_profiles") if isinstance(run, Mapping) else {}
            profile_key = (
                "vision"
                if "vision" in worker_definition.required_model_capabilities
                else "text"
            )
            profile = (
                profiles.get(profile_key)
                if isinstance(profiles, Mapping)
                else None
            )
            model_profile_hash = (
                profile.get("profile_hash")
                if isinstance(profile, Mapping)
                else None
            )
        if model_profile_hash is None:
            model_profile_hash = _sha256(
                {
                    "profile": "unspecified",
                    "required_capabilities": list(
                        worker_definition.required_model_capabilities
                    ),
                }
            )
        execution_identity = ProposalExecutionIdentity(
            capability_definition_hash=request.capability_definition_hash,
            unit_input_hash=_sha256(dict(request.unit_input)),
            context_manifest_hash=manifest.manifest_hash,
            worker_definition_hash=worker_definition.definition_hash,
            model_profile_hash=model_profile_hash,
            input_modalities=input_modalities,
            prepared_media_hashes=prepared_media_hashes,
            media_policy_hash=media_policy_hash,
        )

        rejection_reasons: tuple[str, ...] = ()
        try:
            cached = self.sidecars.load_proposal(
                request.unit_id, request.proposal_reference
            )
        except UnitSidecarValidationError:
            cached = None
            rejection_reasons = ("proposal_sidecar_invalid",)
        proposal_reused = False
        proposal_payload: dict[str, Any]
        if cached is not None:
            try:
                persisted_identity = ProposalExecutionIdentity.from_dict(
                    cached.get("execution_identity")
                )
                rejection_reasons = execution_identity.rejection_reasons(
                    persisted_identity
                )
                cached_proposal = cached.get("proposal")
                if not isinstance(cached_proposal, dict):
                    rejection_reasons += ("proposal_payload_invalid",)
                elif cached.get("proposal_hash") != _sha256(cached_proposal):
                    rejection_reasons += ("proposal_hash_mismatch",)
                elif cached.get("execution_identity_hash") != persisted_identity.identity_hash:
                    rejection_reasons += ("execution_identity_hash_mismatch",)
            except (TypeError, ValueError):
                rejection_reasons = ("execution_identity_invalid",)
            if not rejection_reasons:
                proposal_reused = True
                proposal = dict(cached["proposal"])
                proposal_payload = dict(cached)
            else:
                cached = None

        if cached is None:
            # Fired only when a model call is actually about to happen — a
            # reused proposal below never reaches here, so a reader is never
            # told the agent is "reading" sources it already drew from.
            if on_manifest_resolved is not None:
                on_manifest_resolved(manifest)
            repair_seed = self._linked_repair_seed(
                request=request,
                execution_identity=execution_identity,
            )
            try:
                worker_result = self.workers.execute(
                    WorkerRequest(
                        worker_id=request.worker_id,
                        capability_id=request.capability_id,
                        unit_id=request.unit_id,
                        context=bundle,
                        unit_input=request.unit_input,
                        activity=request.activity,
                    ),
                    self.gateway,
                    on_repair=on_worker_repaired,
                    repair_seed=repair_seed,
                )
            except WorkerRunError as error:
                rejection_payload = {
                    "capability_id": request.capability_id,
                    "unit_id": request.unit_id,
                    "worker_id": request.worker_id,
                    "response_schema_hash": worker_definition.response_schema.schema_hash,
                    "execution_identity": execution_identity.to_dict(),
                    "execution_identity_hash": execution_identity.identity_hash,
                    "response_hash": _sha256_text(error.last_response),
                    "status": "rejected",
                    "validation_errors": list(error.errors),
                    "response": error.last_response,
                }
                rejection_reference = self.sidecars.persist_rejection(
                    request.unit_id, rejection_payload
                )
                if on_rejection_persisted is not None:
                    on_rejection_persisted(rejection_reference)
                raise
            proposal = dict(worker_result.proposal)
            # Fired with the freshly generated proposal, before approval can
            # revise it: this reflects what the model actually produced, the
            # same thing the live progress checklist was reading as it came in.
            if on_worker_completed is not None:
                on_worker_completed(proposal)
            proposal_payload = {
                "capability_id": request.capability_id,
                "unit_id": request.unit_id,
                "worker_id": request.worker_id,
                # A repair happens inside one worker call, so the unit's own
                # attempt counter stays at 1 however many turns the response
                # took. Recording the worker's count keeps the repairs on a
                # succeeded unit visible after the run, where until now only the
                # aggregate `usage.retries` and the raw call log showed them.
                "worker_attempts": worker_result.attempts,
                "worker_response_hash": worker_result.response_hash,
                "response_schema_hash": worker_result.response_schema_hash,
                "execution_identity": execution_identity.to_dict(),
                "execution_identity_hash": execution_identity.identity_hash,
                "proposal_hash": _sha256(proposal),
                "status": "proposed",
                "proposal": proposal,
            }
            proposal_reference = self.sidecars.persist_proposal(
                request.unit_id, proposal_payload
            )
        else:
            proposal_reference = {
                "path": f"proposals/{_unit_filename(request.unit_id)}",
                "unit_id": request.unit_id,
                "payload_hash": _sha256(proposal_payload),
            }
        if on_proposal_persisted is not None:
            on_proposal_persisted(proposal_reference)

        if approval_provider is not None and proposal_payload.get("status") != "accepted":
            accepted = approval_provider(proposal)
            if accepted is None:
                return UnitPipelineOutcome(
                    manifest_reference=manifest_reference,
                    proposal_reference=proposal_reference,
                    receipt_reference=None,
                    receipt=None,
                    readiness=None,
                    status="approval_rejected",
                    proposal_reused=proposal_reused,
                    proposal_reuse_rejection_reasons=rejection_reasons,
                )
            proposal = dict(accepted)
            proposal_payload.update(
                {
                    "proposal_hash": _sha256(proposal),
                    "status": "accepted",
                    "proposal": proposal,
                }
            )
            proposal_reference = self.sidecars.persist_proposal(
                request.unit_id, proposal_payload
            )
            if on_proposal_persisted is not None:
                on_proposal_persisted(proposal_reference)

        if request.executor_id is None:
            # A proposal-only unit. Its persisted proposal is the durable
            # outcome, so there is nothing to reconcile, commit, or receipt.
            return UnitPipelineOutcome(
                manifest_reference=manifest_reference,
                proposal_reference=proposal_reference,
                receipt_reference=None,
                receipt=None,
                readiness=None,
                status="proposed",
                proposal_reused=proposal_reused,
                proposal_reuse_rejection_reasons=rejection_reasons,
            )

        executor_request = ExecutorRequest(
            executor_id=request.executor_id,
            capability_id=request.capability_id,
            unit_id=request.unit_id,
            proposal=proposal,
            expected_revision=request.expected_revision,
            expected_parents=request.expected_parents,
            activity=request.activity,
        )
        executor_definition = self.executors.get(request.executor_id)
        try:
            receipt = self.sidecars.load_receipt(
                request.unit_id,
                request=executor_request,
                definition=executor_definition,
                reference=request.receipt_reference,
            )
        except UnitSidecarValidationError:
            receipt = None
        receipt_reused = receipt is not None
        executor_reconciled = False
        reconciliation = self.executors.reconcile(executor_request, target)
        if receipt is not None:
            if reconciliation.disposition != "already_applied":
                raise UnitPipelineConflict(
                    reconciliation.reason
                    or "Persisted receipt postcondition no longer holds.",
                    manifest_reference=manifest_reference,
                    proposal_reference=proposal_reference,
                    receipt_reference=request.receipt_reference,
                    proposal_reuse_rejection_reasons=rejection_reasons,
                )
            receipt_reference = request.receipt_reference or {
                "path": f"receipts/{_unit_filename(request.unit_id)}",
                "unit_id": request.unit_id,
                "payload_hash": _sha256(
                    {**receipt.to_dict(), "receipt_hash": receipt.receipt_hash}
                ),
                "receipt_hash": receipt.receipt_hash,
            }
        elif reconciliation.disposition == "already_applied":
            receipt = self.executors.receipt_for_reconciliation(
                executor_request, reconciliation
            )
            receipt_reference = self.sidecars.persist_receipt(
                request.unit_id, receipt
            )
            executor_reconciled = True
        elif reconciliation.disposition == "conflict":
            raise UnitPipelineConflict(
                reconciliation.reason or "Executor reconciliation found a conflict.",
                manifest_reference=manifest_reference,
                proposal_reference=proposal_reference,
                proposal_reuse_rejection_reasons=rejection_reasons,
            )
        else:
            receipt = self.executors.execute(executor_request, target)
            receipt_reference = self.sidecars.persist_receipt(request.unit_id, receipt)
        if on_receipt_persisted is not None:
            on_receipt_persisted(receipt_reference)
        readiness = readiness_provider() if readiness_provider is not None else None
        if readiness is not None:
            satisfied = (
                bool(getattr(readiness, "satisfied"))
                if hasattr(readiness, "satisfied")
                else isinstance(readiness, Mapping)
                and readiness.get("state") == "satisfied"
            )
            if not satisfied:
                raise UnitPipelineError(
                    "Executor committed but capability readiness is not satisfied."
                )
        return UnitPipelineOutcome(
            manifest_reference=manifest_reference,
            proposal_reference=proposal_reference,
            receipt_reference=receipt_reference,
            receipt=receipt,
            readiness=readiness,
            status="succeeded",
            proposal_reused=proposal_reused,
            proposal_reuse_rejection_reasons=rejection_reasons,
            receipt_reused=receipt_reused,
            executor_reconciled=executor_reconciled,
        )


__all__ = [
    "ApprovalProvider",
    "ContextProvider",
    "ContextIdentityProvider",
    "ProposalExecutionIdentity",
    "ReadinessProvider",
    "UnitPipeline",
    "UnitPipelineConflict",
    "UnitPipelineError",
    "UnitPipelineOutcome",
    "UnitPipelineRequest",
    "UnitSidecarStore",
    "UnitSidecarValidationError",
]
