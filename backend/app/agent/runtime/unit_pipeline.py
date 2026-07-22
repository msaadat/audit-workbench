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
    approval_kind: str | None = None


@dataclass(frozen=True)
class UnitPipelineOutcome:
    manifest_reference: Mapping[str, str]
    proposal_reference: Mapping[str, str]
    receipt_reference: Mapping[str, str] | None
    receipt: ExecutorReceipt | None
    readiness: object | None
    status: str


ContextProvider = Callable[[], tuple[ContextManifest, ContextBundle]]
ApprovalProvider = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
ReadinessProvider = Callable[[], object]


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
            "proposal_hash": _sha256(proposal),
            "status": "proposed",
            "proposal": proposal,
        }
        proposal_reference = self.sidecars.persist_proposal(
            request.unit_id, proposal_payload
        )

        if approval_provider is not None:
            accepted = approval_provider(worker_result.proposal)
            if accepted is None:
                return UnitPipelineOutcome(
                    manifest_reference=manifest_reference,
                    proposal_reference=proposal_reference,
                    receipt_reference=None,
                    receipt=None,
                    readiness=None,
                    status="approval_rejected",
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
        )


__all__ = [
    "ApprovalProvider",
    "ContextProvider",
    "ReadinessProvider",
    "UnitPipeline",
    "UnitPipelineError",
    "UnitPipelineOutcome",
    "UnitPipelineRequest",
    "UnitSidecarStore",
]
