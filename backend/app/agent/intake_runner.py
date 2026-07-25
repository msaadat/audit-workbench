"""Durable folder-classification and incorporation runner.

Intake is deliberately *not* a capability workflow. Its authoritative state is
the staged batch under ``Imports/<batch_id>/``, advanced by a separate HTTP
upload protocol; there is exactly one unit at every step; and application creates
the workspace artifacts rather than committing to existing ones. The reasoning,
measured against the workflow scheduler's own contract, is recorded in
``docs/agent-protocol-runner-decisions.md``.

Retention is not an exemption from the target contracts. This runner composes
``RunRuntime``, reaches the provider only through the registered
``intake.classification`` worker and the shared ``ModelGateway``, supplies that
worker only what the declared ``intake.classification`` context preset resolves,
persists the content-free manifest before the call, and persists both the model
proposal and the accepted decision set before any file is touched.
"""

from __future__ import annotations

import hashlib
import json
import threading

from .. import intake, llm
from ..workspaces import Workspace
from .base import BaseRunner, Cancelled, LimitExceeded
from .context import (
    ContextResolutionError,
    ContextResolver,
    intake_classification_scope,
)
from .executors import EXECUTORS
from .runtime import RunRuntime, UnitPipeline, UnitPipelineRequest, UnitSidecarStore
from .workers import WORKERS
from .workers.intake import CLASSIFICATION_WORKER_ID
from .workers.model import (
    WorkerContractError,
    WorkerResponseValidationError,
    WorkerRunError,
)

# The runner is the classification "capability" for context and proposal
# identity. It is a stable declaration key, not a registered capability: nothing
# schedules it, and no dependency graph names it.
CLASSIFICATION_DECLARATION = {
    "id": "intake.classification",
    "context": "intake.classification",
}
# Errors that mean "the model could not be used", for which intake has always
# fallen back to deterministic local routing rather than failing the batch.
CLASSIFICATION_FALLBACK_ERRORS = (
    llm.LLMError,
    WorkerRunError,
    WorkerResponseValidationError,
    WorkerContractError,
    ContextResolutionError,
)


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def classification_unit_id(batch_id: str) -> str:
    """Stable semantic unit for one batch's model classification."""

    return f"intake_classification:{batch_id}"


def apply_unit_id(batch_id: str) -> str:
    """Stable semantic unit for one batch's accepted routing decisions."""

    return f"intake_apply:{batch_id}"


class IntakeRunner(BaseRunner):
    stage_titles = {
        "validate": "Validate batch",
        "classify": "Classify files",
        "apply": "Apply routing",
        "verify": "Verify imports",
        "summary": "Intake summary",
    }

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        state_lock: threading.RLock | None = None,
        context_resolver: ContextResolver | None = None,
    ):
        super().__init__(workspace, run, handle, runtime=runtime, state_lock=state_lock)
        self.context_resolver = context_resolver or ContextResolver()
        self.sidecars = UnitSidecarStore(workspace, run["id"])
        self.unit_pipeline = UnitPipeline(
            runtime=self.runtime,
            gateway=self.model_gateway,
            workers=WORKERS,
            executors=EXECUTORS,
            sidecars=self.sidecars,
        )

    def execute(self) -> None:
        if not self.run.get("started"):
            self.mark_started()
        try:
            self.set_status("executing")
            batch_id = str(self.context.get("batch_id") or "")
            if not batch_id:
                raise ValueError("An intake run requires context.batch_id.")
            batch = intake.load_batch(self.ws, batch_id)
            file_count = len(batch.get("items") or [])
            if batch.get("source_id") != self.context.get("source_id", batch.get("source_id")):
                raise ValueError("The intake source does not match the batch.")
            validate = self.add_task(
                "validate", "intake:validate", "Validate staged files",
                f"Validating {file_count} staged file{'s' if file_count != 1 else ''}…",
            )
            self.task_status(validate, "running")
            if batch["status"] == "uploading":
                batch = intake.complete_upload(self.ws, batch_id)
            if batch["status"] not in ("classifying", "awaiting_approval", "applying", "completed"):
                raise ValueError("The folder-import batch is not ready for classification.")
            self.task_status(validate, "completed")

            classify = self.add_task(
                "classify", "intake:classify", "Classify folder candidates",
                f"Classifying {file_count} staged file{'s' if file_count != 1 else ''}…",
            )
            if classify["status"] != "completed" and batch["status"] != "completed":
                self.task_status(classify, "running")
                if llm.agent_status().get("configured"):
                    self.task_detail(
                        classify,
                        f"Classifying {file_count} staged file{'s' if file_count != 1 else ''} with the model…",
                    )
                else:
                    self.task_detail(classify, "Applying deterministic local file routing…")
                self._classify(batch)
                intake.save_batch(self.ws, batch)
                self.task_status(classify, "completed")

            apply_task = self.add_task(
                "apply", "intake:apply", "Confirm and apply file routing",
                f"Applying routing decisions to {file_count} file{'s' if file_count != 1 else ''}…",
            )
            if batch["status"] != "completed":
                self.task_status(apply_task, "running")
                decisions = self._decisions(batch, apply_task)
                batch = intake.apply_batch(self.ws, batch_id, decisions)
                for item in batch["items"]:
                    if item.get("target_ref"):
                        kind, item_id = item["target_ref"].split(":", 1)
                        self.record_artifact(kind, item_id, f"intake:{batch_id}:{item['id']}", item.get("action") or "created", apply_task)
                self.task_status(apply_task, "completed")

            imported_count = sum(
                item.get("action") == "imported" for item in batch.get("items") or []
            )
            verify = self.add_task(
                "verify", "intake:verify", "Verify imported targets",
                f"Verifying {imported_count} imported artifact{'s' if imported_count != 1 else ''}…",
            )
            self.task_status(verify, "running")
            refreshed = intake.load_batch(self.ws, batch_id)
            missing = [item["relative_path"] for item in refreshed["items"] if item.get("action") == "imported" and not item.get("target_ref")]
            if missing:
                raise ValueError(f"{len(missing)} imported target(s) could not be verified.")
            self.task_status(verify, "completed")
            self.run["intake"] = {
                "batch_id": batch_id,
                "source_id": refreshed["source_id"],
                "classified": sum(bool(item.get("classification")) for item in refreshed["items"]),
                **dict(refreshed.get("summary") or {}),
            }
            summary = self.add_task("summary", "intake:summary", "Summarize folder intake")
            self.task_status(summary, "running")
            counts = self.run["intake"]
            self.run["summary_markdown"] = (
                "# Folder intake summary\n\n"
                f"Imported **{counts.get('imported', 0)}** file(s), ignored "
                f"**{counts.get('ignored', 0)}**, and left **{counts.get('ambiguous', 0)}** "
                "for manual classification."
            )
            self.task_status(summary, "completed")
            self.mark_finished()
            self.set_status("completed")
            self.emit("summary_ready", {"run_id": self.run["id"]})
        except Cancelled:
            self.mark_finished()
            self.set_status("cancelled")
        except LimitExceeded as error:
            self.run["error"] = str(error)
            self.mark_finished()
            self.set_status("failed")
        except Exception as error:
            self.run["error"] = str(error)
            self.mark_finished()
            self.set_status("failed")

    def _classify(self, batch: dict) -> None:
        """Merge one bounded model classification into the staged batch.

        Classification is a *proposal-only* pipeline unit: it resolves declared
        context, persists the content-free manifest before the provider call,
        invokes the registered worker, and persists the proposal. Nothing is
        committed here — the batch merge below is staging state, and the files
        themselves are only touched by ``apply``.

        A batch that reaches the provider once is not re-billed on a resumed run:
        the proposal sidecar is reused whenever its exact execution identity
        still holds. A model that cannot be reached, or that cannot produce a
        usable response within its bounded repair allowance, falls back to the
        deterministic local routing every item already carries.
        """
        if not llm.agent_status().get("configured"):
            self.warn("No model is configured; using deterministic local routing.")
            return
        batch_id = str(batch.get("id") or "")
        unit_id = classification_unit_id(batch_id)
        try:
            self.unit_pipeline.run(
                UnitPipelineRequest(
                    capability_id=CLASSIFICATION_DECLARATION["id"],
                    unit_id=unit_id,
                    worker_id=CLASSIFICATION_WORKER_ID,
                    # Classification proposes; it never commits. ``apply`` owns
                    # the file operations, which are not an artifact commit.
                    executor_id=None,
                    unit_input={"batch_id": batch_id},
                    activity={"task_id": "intake:classify"},
                    expected_revision=self.ws.revision,
                    expected_parents={},
                    capability_definition_hash=_sha256_json(CLASSIFICATION_DECLARATION),
                ),
                context_provider=lambda: self.context_resolver.resolve(
                    self.ws,
                    CLASSIFICATION_DECLARATION,
                    {"id": unit_id},
                    intake_classification_scope(self.ws, batch),
                ),
                context_identity_provider=lambda manifest: (
                    self.context_resolver.execution_identity(
                        CLASSIFICATION_DECLARATION, manifest
                    )
                ),
                target=None,
            )
        except CLASSIFICATION_FALLBACK_ERRORS as error:
            self.warn(f"Model classification was unavailable; using local routing: {error}")
            return
        proposal = (self.sidecars.load_proposal(unit_id) or {}).get("proposal") or {}
        intake.merge_model_classifications(batch, list(proposal.get("items") or []))

    def _decisions(self, batch: dict, task: dict) -> list[dict]:
        """Resolve the routing decisions and make them durable before applying.

        In permission mode the auditor edits and approves per-file proposals; in
        auto mode only routes that agree with the deterministic local route and
        are confident and non-duplicate are imported. Either way the resulting
        decision set is persisted to its own semantic-unit sidecar before
        ``apply_batch`` touches a file, so what was applied is recoverable
        independently of the batch record the application itself rewrites.
        """
        specs = intake.approval_specs(batch)
        if batch["mode"] == "permission":
            batch["status"] = "awaiting_approval"
            intake.save_batch(self.ws, batch)
            proposals = [
                self.proposal_item(
                    spec["relative_path"],
                    str(spec.get("rationale") or "Review the proposed route."),
                    spec,
                    {"local_metadata": next(item for item in batch["items"] if item["id"] == spec["item_id"])["local_metadata"]},
                )
                for spec in specs
            ]
            accepted = self.request_approval("file_classification", task, proposals)
            accepted_ids = {entry["spec"]["item_id"] for entry in accepted}
            decisions = [entry["spec"] for entry in accepted]
            for spec in specs:
                if spec["item_id"] not in accepted_ids:
                    decisions.append({**spec, "proposed_action": "ignore"})
            return self._persist_decisions(batch, decisions, origin="auditor_approved")
        decisions = []
        for spec in specs:
            local_route = spec.get("deterministic_route")
            agreed = spec.get("route") == local_route
            # Medium confidence means the route is certain but category/role is a
            # default guess; auto mode still imports those.
            safe = spec.get("confidence") in ("high", "medium") and not spec.get("duplicate_ref")
            decisions.append({**spec, "proposed_action": "import" if agreed and safe else "ignore"})
        return self._persist_decisions(batch, decisions, origin="deterministic")

    def _persist_decisions(
        self,
        batch: dict,
        decisions: list[dict],
        *,
        origin: str,
    ) -> list[dict]:
        batch_id = str(batch.get("id") or "")
        payload = {"batch_id": batch_id, "decisions": decisions}
        self.sidecars.persist_proposal(
            apply_unit_id(batch_id),
            {
                "capability_id": "intake.apply",
                "unit_id": apply_unit_id(batch_id),
                "status": "accepted",
                "origin": origin,
                "proposal_hash": _sha256_json(payload),
                "proposal": payload,
            },
        )
        return decisions
