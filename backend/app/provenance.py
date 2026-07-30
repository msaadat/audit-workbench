"""What the agent read, what it proposed, and what it committed — for one unit.

Every workflow unit already persists three sidecars: a content-free context
manifest, the worker's proposal, and the executor's receipt. Together they
answer the question a reviewer asks first about anything an agent wrote —
*what did it actually see?* — and until now nothing could read them back.

Two rules govern this module.

**It fails closed.** Each sidecar reports its own state. A reference that
points at a missing file is ``unavailable``; a file whose hash or unit identity
does not match its reference is ``invalid``. Neither is quietly downgraded into
a partial record, because a provenance trail that fills its own gaps is worse
than none.

**It returns no worker content.** The manifest is content-free by construction,
so it passes through whole. The proposal is the generated artifact itself — the
drafted APM, the RCM rows — so only its hash is exposed. The receipt is
commit metadata and carries no source text.
"""

from __future__ import annotations

from typing import Any

from .agent import store
from .agent.capabilities import REGISTRY_BY_WORKFLOW
from .agent.context import manifest as context_manifest
from .agent.runtime.unit_pipeline import UnitSidecarStore, UnitSidecarValidationError
from .workspaces import Workspace, WorkspaceError

# A sidecar is one of these and never a blend of them.
STATES = ("available", "absent", "unavailable", "invalid")

_ABSENT = {"state": "absent", "reason": "The unit recorded no reference to this sidecar."}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason}


def _invalid(reason: str) -> dict[str, Any]:
    return {"state": "invalid", "reason": reason}


def _find_unit(run: dict, unit_id: str) -> tuple[dict, dict] | tuple[None, None]:
    for stage in (run.get("workflow") or {}).get("stages") or []:
        for unit in stage.get("units") or []:
            if str(unit.get("id")) == unit_id:
                return stage, unit
    return None, None


def _worker_kind(run: dict, capability_id: str) -> str:
    """Resolve the worker a capability runs, for model-usage attribution."""
    definition = str((run.get("workflow") or {}).get("definition") or "")
    registry = REGISTRY_BY_WORKFLOW.get(definition)
    if registry is None or not capability_id:
        return ""
    try:
        return str(registry.get(capability_id).worker_kind or "")
    except (KeyError, AttributeError, WorkspaceError):
        return ""


# --------------------------------------------------------------------------- #
# Sidecars
# --------------------------------------------------------------------------- #
def _context(workspace: Workspace, run_id: str, unit: dict) -> dict[str, Any]:
    reference = unit.get("context_manifest")
    if not reference:
        return dict(_ABSENT)
    try:
        manifest = context_manifest.load_manifest(workspace, run_id, reference)
    except WorkspaceError as error:
        # `load_manifest` raises the same type for "not found" and "identity
        # mismatch"; the message is the only thing that separates them, and the
        # distinction matters to a reviewer.
        message = str(error)
        return _invalid(message) if "identity" in message or "invalid" in message else _unavailable(message)

    payload = manifest.to_dict()
    return {
        "state": "available",
        "manifest_hash": manifest.manifest_hash,
        "context_spec_hash": payload["context_spec_hash"],
        "resolver_hash": payload["resolver_hash"],
        "supplied_size": payload["supplied_size"],
        # Content-free records, passed through exactly as persisted.
        "selections": payload["selections"],
        "omissions": payload["omissions"],
        "truncations": payload["truncations"],
        "privacy_decisions": payload["privacy_decisions"],
    }


def _proposal(sidecars: UnitSidecarStore, unit: dict) -> dict[str, Any]:
    reference = unit.get("proposal_sidecar")
    if not reference:
        return dict(_ABSENT)
    try:
        payload = sidecars.load_proposal(str(unit.get("id")), reference)
    except UnitSidecarValidationError as error:
        return _invalid(str(error))
    except WorkspaceError as error:
        return _unavailable(str(error))
    if payload is None:
        return _unavailable("The proposal sidecar file is missing.")
    # The proposal *is* the generated artifact. Its identity is provenance; its
    # body is the work product, which the auditor reads on its own surface.
    return {
        "state": "available",
        "payload_hash": str(reference.get("payload_hash") or ""),
        "content_withheld": True,
    }


def _receipt(sidecars: UnitSidecarStore, unit: dict) -> dict[str, Any]:
    reference = unit.get("receipt_sidecar")
    if not reference:
        return dict(_ABSENT)
    try:
        payload = sidecars.read_receipt_record(str(unit.get("id")), reference)
    except UnitSidecarValidationError as error:
        return _invalid(str(error))
    except WorkspaceError as error:
        return _unavailable(str(error))
    if payload is None:
        return _unavailable("The receipt sidecar file is missing.")
    # A receipt is commit metadata end to end — refs, hashes, and revisions.
    return {
        "state": "available",
        "receipt_hash": payload.get("receipt_hash"),
        "proposal_hash": payload.get("proposal_hash"),
        "executor_id": payload.get("executor_id"),
        "executor_definition_hash": payload.get("executor_definition_hash"),
        "artifact_refs": list(payload.get("artifact_refs") or []),
        "postcondition_hashes": dict(payload.get("postcondition_hashes") or {}),
        "workspace_revision_before": payload.get("workspace_revision_before"),
        "workspace_revision_after": payload.get("workspace_revision_after"),
        "reconciled": bool(payload.get("reconciled")),
        "output": payload.get("output") or {},
    }


def _profile_used(run: dict, worker_key: str) -> dict[str, Any]:
    """The model profile this worker's calls actually ran against.

    Matching on the profile hash recorded per call, rather than assuming the
    text profile, keeps vision workers attributed to the vision model.
    """
    profiles = run.get("model_profiles") or {}
    hashes = {
        str(call.get("model_profile_hash") or "")
        for call in (run.get("usage") or {}).get("model_call_metrics") or []
        if str(call.get("worker") or "") == worker_key
    }
    for profile in profiles.values():
        if isinstance(profile, dict) and str(profile.get("profile_hash") or "") in hashes:
            return profile
    # No call recorded for this worker — a reused or deterministic unit.
    return {}


def _model(run: dict, capability_id: str) -> dict[str, Any]:
    worker_kind = _worker_kind(run, capability_id)
    worker_key = f"agent:{worker_kind}" if worker_kind else ""
    usage = (run.get("usage") or {}).get("model_usage_by_worker") or {}
    record = usage.get(worker_key) if worker_key else None
    profile = _profile_used(run, worker_key) if worker_key else {}
    return {
        "worker_kind": worker_kind,
        "provider": profile.get("provider") or "",
        "model": profile.get("model") or "",
        "profile_hash": profile.get("profile_hash") or "",
        "configuration_source": profile.get("configuration_source") or "",
        # Usage is accounted per worker, not per unit: a stage that fans out
        # over seventeen RCM rows shares one worker. Saying so is better than
        # implying these numbers belong to this unit alone.
        "scope": "worker_across_run",
        "usage": record or {},
    }


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #
def unit_provenance(workspace: Workspace, run_id: str, unit_id: str) -> dict[str, Any]:
    """Everything recorded about how one unit reached its result."""
    run = store.load_run(workspace, run_id)
    stage, unit = _find_unit(run, str(unit_id))
    if unit is None:
        raise WorkspaceError(f"Unit '{unit_id}' is not part of run '{run_id}'.")

    capability_id = str(unit.get("capability") or stage.get("capability") or "")
    sidecars = UnitSidecarStore(workspace, run_id)
    return {
        "run_id": run.get("id"),
        "run_status": run.get("status"),
        "workflow_definition": (run.get("workflow") or {}).get("definition"),
        "unit": {
            "id": unit.get("id"),
            "title": unit.get("title"),
            "kind": unit.get("kind"),
            "capability": capability_id,
            "stage_id": stage.get("id"),
            "stage_title": stage.get("title"),
            "status": unit.get("status"),
            "attempts": unit.get("attempts"),
            "started_at": unit.get("started_at"),
            "finished_at": unit.get("finished_at"),
            "error": unit.get("error"),
        },
        "context": _context(workspace, run_id, unit),
        "proposal": _proposal(sidecars, unit),
        "receipt": _receipt(sidecars, unit),
        "model": _model(run, capability_id),
    }


def resolve_artifact(workspace: Workspace, artifact_ref: str) -> dict[str, str] | None:
    """Find the unit that most recently committed an artifact.

    Receipts record exactly what each unit wrote, so the artifact-to-unit index
    is the receipts themselves rather than anything inferred from ordering.
    Runs are newest-first, so the first match is the current author.
    """
    wanted = str(artifact_ref or "").strip()
    if not wanted:
        return None
    for summary in store.list_runs(workspace):
        run_id = str(summary.get("id") or "")
        try:
            run = store.load_run(workspace, run_id)
        except WorkspaceError:
            continue
        sidecars = UnitSidecarStore(workspace, run_id)
        for stage in (run.get("workflow") or {}).get("stages") or []:
            for unit in stage.get("units") or []:
                reference = unit.get("receipt_sidecar")
                if not reference:
                    continue
                try:
                    payload = sidecars.read_receipt_record(str(unit.get("id")), reference)
                except (UnitSidecarValidationError, WorkspaceError):
                    continue
                if payload and wanted in (payload.get("artifact_refs") or []):
                    return {"run_id": run_id, "unit_id": str(unit.get("id"))}
    return None


def artifact_provenance(workspace: Workspace, artifact_ref: str) -> dict[str, Any]:
    """Provenance for whatever unit last committed this artifact."""
    located = resolve_artifact(workspace, artifact_ref)
    if located is None:
        return {
            "state": "unattributed",
            "artifact_ref": artifact_ref,
            "reason": "No agent run recorded a receipt for this artifact. "
                      "It was created or last edited outside the agent.",
        }
    payload = unit_provenance(workspace, located["run_id"], located["unit_id"])
    return {"state": "attributed", "artifact_ref": artifact_ref, **payload}
