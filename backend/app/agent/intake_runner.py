"""Durable folder-classification and incorporation runner."""

from __future__ import annotations

from .. import intake, llm
from . import prompts
from .base import BaseRunner, Cancelled, LimitExceeded


class IntakeRunner(BaseRunner):
    stage_titles = {
        "validate": "Validate batch",
        "classify": "Classify files",
        "apply": "Apply routing",
        "verify": "Verify imports",
        "summary": "Intake summary",
    }

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
        if not llm.agent_status().get("configured"):
            self.warn("No model is configured; using deterministic local routing.")
            return
        payload = intake.classification_payload_for_model(self.ws, batch)
        try:
            response = self.llm_json(
                prompts.FILE_CLASSIFICATION_SYSTEM,
                prompts.file_classification_user(payload),
                validator=lambda value: prompts.validate_json_shape(
                    value, object_arrays=("items",)
                ),
            )
            intake.merge_model_classifications(batch, response.get("items") or [])
        except llm.LLMError as error:
            self.warn(f"Model classification was unavailable; using local routing: {error}")

    def _decisions(self, batch: dict, task: dict) -> list[dict]:
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
            return decisions
        decisions = []
        for spec in specs:
            local_route = spec.get("deterministic_route")
            agreed = spec.get("route") == local_route
            # Medium confidence means the route is certain but category/role is a
            # default guess; auto mode still imports those.
            safe = spec.get("confidence") in ("high", "medium") and not spec.get("duplicate_ref")
            decisions.append({**spec, "proposed_action": "import" if agreed and safe else "ignore"})
        return decisions
