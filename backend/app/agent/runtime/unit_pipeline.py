"""Runner-independent sequencing for one model-backed workflow unit."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ...workspaces import WorkspaceError, write_json_atomic
from .. import store
from ..context import ContextBundle, ContextManifest
from ..executors import ExecutorReceipt, ExecutorRegistry, ExecutorRequest
from ..workers import WorkerRegistry, WorkerRequest
from .model_gateway import ModelGateway
from .run_runtime import RunRuntime


class UnitPipelineError(RuntimeError):
    """A unit could not complete the declared pipeline contract."""


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


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Unit sidecar payload must be JSON-compatible.") from error


def _sha256(value: object) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _unit_filename(unit_id: str) -> str:
    value = str(unit_id or "").strip()
    if not value:
        raise ValueError("Pipeline unit_id must be non-empty.")
    filename = quote(value, safe="._-:")
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

    def load_proposal(self, unit_id: str) -> Mapping[str, Any] | None:
        path = self._folder("proposals") / _unit_filename(unit_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError("Unit proposal sidecar is invalid.") from error
        if not isinstance(value, dict):
            raise WorkspaceError("Unit proposal sidecar must contain an object.")
        return value

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


@dataclass(frozen=True)
class UnitPipelineRequest:
    capability_id: str
    unit_id: str
    worker_id: str
    executor_id: str
    unit_input: Mapping[str, Any]
    activity: Mapping[str, Any]
    expected_revision: int
    expected_parents: Mapping[str, str]
    capability_definition_hash: str
    approval_kind: str | None = None


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


@dataclass(frozen=True)
class ProposalExecutionIdentity:
    """Exact identity of one uncommitted model proposal execution."""

    capability_definition_hash: str
    unit_input_hash: str
    context_manifest_hash: str
    context_spec_hash: str
    resolver_hash: str
    selector_definition_hashes: tuple[str, ...]
    selected_source_hashes: tuple[str, ...]
    worker_definition_hash: str
    worker_implementation_hash: str
    prompt_hash: str
    response_schema_hash: str

    def __post_init__(self) -> None:
        scalar_fields = (
            "capability_definition_hash",
            "unit_input_hash",
            "context_manifest_hash",
            "context_spec_hash",
            "resolver_hash",
            "worker_definition_hash",
            "worker_implementation_hash",
            "prompt_hash",
            "response_schema_hash",
        )
        for name in scalar_fields:
            object.__setattr__(self, name, _require_hash(getattr(self, name), name))
        for name in ("selector_definition_hashes", "selected_source_hashes"):
            values = tuple(_require_hash(value, name) for value in getattr(self, name))
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be unique and sorted.")
            object.__setattr__(self, name, values)

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_definition_hash": self.capability_definition_hash,
            "unit_input_hash": self.unit_input_hash,
            "context_manifest_hash": self.context_manifest_hash,
            "context_spec_hash": self.context_spec_hash,
            "resolver_hash": self.resolver_hash,
            "selector_definition_hashes": list(self.selector_definition_hashes),
            "selected_source_hashes": list(self.selected_source_hashes),
            "worker_definition_hash": self.worker_definition_hash,
            "worker_implementation_hash": self.worker_implementation_hash,
            "prompt_hash": self.prompt_hash,
            "response_schema_hash": self.response_schema_hash,
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
            "context_spec_hash",
            "resolver_hash",
            "selector_definition_hashes",
            "selected_source_hashes",
            "worker_definition_hash",
            "worker_implementation_hash",
            "prompt_hash",
            "response_schema_hash",
        }
        if set(value) != expected:
            raise ValueError("Proposal execution identity fields are invalid.")
        return cls(
            **{
                **dict(value),
                "selector_definition_hashes": tuple(
                    value["selector_definition_hashes"]
                ),
                "selected_source_hashes": tuple(value["selected_source_hashes"]),
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
            ("context_spec_hash", "context_spec_changed"),
            ("resolver_hash", "resolver_changed"),
            ("selector_definition_hashes", "selector_definitions_changed"),
            ("selected_source_hashes", "selected_sources_changed"),
            ("worker_definition_hash", "worker_definition_changed"),
            ("worker_implementation_hash", "worker_implementation_changed"),
            ("prompt_hash", "prompt_changed"),
            ("response_schema_hash", "response_schema_changed"),
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

    def run(
        self,
        request: UnitPipelineRequest,
        *,
        context_provider: ContextProvider,
        target: object,
        context_identity_provider: ContextIdentityProvider,
        approval_provider: ApprovalProvider | None = None,
        readiness_provider: ReadinessProvider | None = None,
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
        worker_definition = self.workers.get(request.worker_id)
        context_identity = context_identity_provider(manifest)
        if not isinstance(context_identity, Mapping):
            raise UnitPipelineError("Context execution identity must be an object.")
        if context_identity.get("manifest_hash") != manifest.manifest_hash:
            raise UnitPipelineError("Context execution identity manifest hash is invalid.")
        if context_identity.get("context_spec_hash") != manifest.context_spec_hash:
            raise UnitPipelineError("Context execution identity spec hash is invalid.")
        if context_identity.get("resolver_hash") != manifest.resolver_hash:
            raise UnitPipelineError("Context execution identity resolver hash is invalid.")
        selector_hashes = tuple(
            sorted(
                {
                    _require_hash(value, "selector_definition_hashes")
                    for value in context_identity.get("selector_definition_hashes") or []
                }
            )
        )
        selected_source_hashes = tuple(
            sorted({selection.source_hash for selection in manifest.selections})
        )
        execution_identity = ProposalExecutionIdentity(
            capability_definition_hash=request.capability_definition_hash,
            unit_input_hash=_sha256(dict(request.unit_input)),
            context_manifest_hash=manifest.manifest_hash,
            context_spec_hash=manifest.context_spec_hash,
            resolver_hash=manifest.resolver_hash,
            selector_definition_hashes=selector_hashes,
            selected_source_hashes=selected_source_hashes,
            worker_definition_hash=worker_definition.definition_hash,
            worker_implementation_hash=worker_definition.implementation_hash,
            prompt_hash=worker_definition.prompt_hash,
            response_schema_hash=worker_definition.response_schema.schema_hash,
        )

        cached = self.sidecars.load_proposal(request.unit_id)
        proposal_reused = False
        rejection_reasons: tuple[str, ...] = ()
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
            )
            proposal = dict(worker_result.proposal)
            proposal_payload = {
                "capability_id": request.capability_id,
                "unit_id": request.unit_id,
                "worker_id": request.worker_id,
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

        if approval_provider is not None and proposal_payload.get("status") != "accepted":
            accepted = approval_provider(worker_result.proposal)
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

        executor_request = ExecutorRequest(
            executor_id=request.executor_id,
            capability_id=request.capability_id,
            unit_id=request.unit_id,
            proposal=proposal,
            expected_revision=request.expected_revision,
            expected_parents=request.expected_parents,
            activity=request.activity,
        )
        receipt = self.executors.execute(executor_request, target)
        receipt_reference = self.sidecars.persist_receipt(request.unit_id, receipt)
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
        )


__all__ = [
    "ApprovalProvider",
    "ContextProvider",
    "ContextIdentityProvider",
    "ProposalExecutionIdentity",
    "ReadinessProvider",
    "UnitPipeline",
    "UnitPipelineError",
    "UnitPipelineOutcome",
    "UnitPipelineRequest",
    "UnitSidecarStore",
]
