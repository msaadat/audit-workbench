"""Shared durable-run plumbing for all audit-assistant run kinds."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable

from .. import llm
from ..workspaces import Workspace
from . import prompts, store
from .runtime import DefaultModelGateway

MAX_TASKS = 60
MAX_RUNTIME_SECONDS = 1800
LLM_JSON_ATTEMPTS = 2


class Cancelled(Exception):
    pass


class LimitExceeded(Exception):
    pass


MODEL_WAIT_LABELS = {
    "agent:command_interpreter": "Preparing the action plan",
    "agent:command_planner": "Planning the next fieldwork steps",
    "agent:document_selection": "Selecting planning documents",
    "agent:document_context": "Analyzing planning documents",
    "agent:apm": "Drafting the audit planning memorandum",
    "agent:rcm": "Drafting the risk and control matrix",
    "agent:work_program": "Drafting RCM planned tests",
    "agent:data_test_spec": "Defining a Data Test",
    "agent:document_test_spec": "Defining a Document Test",
    "agent:document_qa": "Answering from document evidence",
    "agent:finding": "Drafting an evidence-linked finding",
    "agent:file_classification": "Classifying staged files",
    "agent:document_analysis_map": "Analyzing document content",
    "agent:document_analysis_reduce": "Consolidating document analysis",
}


class BaseRunner:
    """Generic state, checkpoint, LLM, task, approval, and event behavior.

    Concrete runners implement ``execute`` and may provide ``stage_titles``.
    The handle is deliberately duck-typed so the thread-control object stays
    in the orchestration module without introducing an import cycle.
    """

    stage_titles: dict[str, str] = {}

    def __init__(self, workspace: Workspace, run: dict, handle):
        self.ws = workspace
        self.run = run
        self.handle = handle
        self.deadline = time.monotonic() + MAX_RUNTIME_SECONDS
        # Independent planning-document requests may run concurrently. Keep
        # provider waits parallel while serializing the shared durable ledger.
        self._state_lock = threading.RLock()
        self.model_gateway = DefaultModelGateway(
            workspace_id=self.ws.id,
            workspace_root=str(self.ws.root),
            run=self.run,
            state_lock=self._state_lock,
            checkpoint=self.checkpoint,
            save=self.save,
            emit=self.emit,
            utcnow=store.utcnow,
            append_provenance=self._append_model_provenance,
            template_context=self._model_template_context,
            stage_labels=MODEL_WAIT_LABELS,
            limit_error=LimitExceeded,
        )
        # Temporary compatibility for existing workflow correlation call sites.
        # The attribution state itself is owned by ModelGateway.
        self._model_context = self.model_gateway.context

    def save(self) -> None:
        with self._state_lock:
            store.save_run(self.ws, self.run)

    def emit(self, type_: str, data: dict) -> None:
        with self._state_lock:
            store.append_event(self.ws, self.run["id"], type_, data)

    def set_activity(
        self,
        phase: str,
        label: str,
        *,
        detail: str | None = None,
        current: int | None = None,
        total: int | None = None,
        attempt: int | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
    ) -> None:
        """Persist one safe, user-facing description of the work in flight."""
        now = store.utcnow()
        with self._state_lock:
            previous = self.run.get("activity") or {}
            same_phase = previous.get("phase") == phase
            activity = {
                "phase": phase,
                "label": label,
                "detail": detail,
                "current": current,
                "total": total,
                "attempt": attempt,
                "task_id": task_id,
                "action_id": action_id,
                "started_at": previous.get("started_at") if same_phase else now,
                "updated_at": now,
                "waiting_on": previous.get("waiting_on") if same_phase else None,
                "model_calls_active": int(previous.get("model_calls_active") or 0) if same_phase else 0,
                "model_started_at": previous.get("model_started_at") if same_phase else None,
            }
            if activity["model_calls_active"] <= 0:
                activity["waiting_on"] = None
                activity["model_started_at"] = None
            self.run["activity"] = activity
            self.run["activity_revision"] = int(self.run.get("activity_revision") or 0) + 1
            self.save()
            self.emit("activity_update", {
                "activity": activity,
                "revision": self.run["activity_revision"],
            })

    def set_status(self, status: str) -> None:
        if self.run["status"] == status:
            return
        self.run["status"] = status
        self.save()
        self.emit("run_status", {"status": status})

    def warn(self, text: str) -> None:
        self.run.setdefault("warnings", []).append(text)
        self.save()
        self.emit("warning", {"text": text})

    def checkpoint(self) -> None:
        if self.handle.cancel.is_set():
            raise Cancelled()
        if self.handle.pause_requested.is_set():
            self.set_status("paused")
            waited_from = time.monotonic()
            while not self.handle.resume.wait(0.2):
                if self.handle.cancel.is_set():
                    raise Cancelled()
            self.handle.resume.clear()
            self.handle.pause_requested.clear()
            self.deadline += time.monotonic() - waited_from
            self.set_status("executing")
        self._drain_inbox()
        if time.monotonic() > self.deadline:
            raise LimitExceeded("run time limit reached")

    def _drain_inbox(self) -> None:
        with self.handle.lock:
            pending, self.handle.inbox = self.handle.inbox[:], []
        for content in pending:
            self.run.setdefault("messages", []).append(
                {"role": "user", "content": content, "at": store.utcnow(), "handled": True}
            )
        if pending:
            self.save()

    def wait_for_input(self, question: str) -> str:
        """Persist one agent question and wait durably for an inbox answer."""
        for message in self.run.get("messages", []):
            if message.get("role") == "user" and not message.get("handled", True):
                message["handled"] = True
                self.run.setdefault("interview", {})["pending_question"] = None
                self.save()
                return str(message.get("content") or "")

        interview = self.run.setdefault(
            "interview", {"captured": {}, "turns": 0, "pending_question": None}
        )
        if interview.get("pending_question") != question:
            message = {
                "role": "agent",
                "content": question,
                "at": store.utcnow(),
                "handled": True,
            }
            self.run.setdefault("messages", []).append(message)
            interview["pending_question"] = question
            self.save()
            self.emit("message", {"message": message})
        self.set_status("awaiting_input")
        waited_from = time.monotonic()
        while True:
            if self.handle.cancel.is_set():
                raise Cancelled()
            if self.handle.pause_requested.is_set():
                self.set_status("paused")
                while not self.handle.resume.wait(0.2):
                    if self.handle.cancel.is_set():
                        raise Cancelled()
                self.handle.resume.clear()
                self.handle.pause_requested.clear()
                self.set_status("awaiting_input")
            with self.handle.lock:
                content = self.handle.inbox.pop(0) if self.handle.inbox else None
            if content is not None:
                self.run.setdefault("messages", []).append(
                    {"role": "user", "content": content, "at": store.utcnow(), "handled": True}
                )
                interview["pending_question"] = None
                self.deadline += time.monotonic() - waited_from
                self.save()
                self.set_status("executing")
                return str(content)
            time.sleep(0.2)

    @property
    def guidance(self) -> list[str]:
        return [m["content"] for m in self.run.get("messages", []) if m.get("role") == "user"]

    @property
    def context(self) -> dict:
        return self.run.get("context") or {}

    def _llm_content(
        self, system: str, user: str, activity: dict | None = None, *, attempt: int = 1,
    ) -> str:
        """Delegate one model turn to the shared gateway."""
        return self.model_gateway.complete(
            system,
            user,
            activity,
            attempt=attempt,
        )

    def _append_model_provenance(self, fields: dict) -> None:
        from ..documents import append_activity

        append_activity(self.ws, **fields)

    def _model_template_context(self, tag: str) -> dict | None:
        template_name = {
            "agent:apm": "apm",
            "agent:rcm": "rcm",
            "agent:work_program": "workpaper",
        }.get(tag)
        if not template_name:
            return None
        from .. import templates_store

        active = templates_store.get_template(self.ws, template_name)
        return {
            "name": template_name,
            "source": active["source"],
            "markdown": active["markdown"],
        }

    def llm_json(
        self,
        system: str,
        user: str,
        activity: dict | None = None,
        *,
        validator: Callable[[dict], dict] | None = None,
        attempts: int = LLM_JSON_ATTEMPTS,
    ) -> dict:
        last_error = ""
        attempt_user = user
        maximum_attempts = max(1, min(int(attempts), 4))
        for attempt in range(1, maximum_attempts + 1):
            content = self._llm_content(system, attempt_user, activity, attempt=attempt)
            try:
                payload = prompts.parse_json_object(content)
                return validator(payload) if validator is not None else payload
            except (ValueError, json.JSONDecodeError) as error:
                last_error = str(error)
                attempt_user = f"{user}\n\nYour previous response could not be used: {last_error}. {prompts.JSON_RULES}"
        raise llm.LLMError(f"The model did not return usable JSON: {last_error}")

    def llm_markdown(
        self, system: str, user: str, *, legacy_field: str | None = None,
        activity: dict | None = None,
    ) -> str:
        """Return Markdown without requiring a large JSON string envelope."""
        content = self._llm_content(system, user, activity)
        return prompts.parse_markdown_response(content, legacy_field=legacy_field)

    def model_adapter(self, messages: list[dict], activity: dict | None = None) -> dict:
        """Route a nested model-assisted service call through run budgets."""
        activity = dict(activity or {})
        attempt = int(activity.pop("retry_number", 1) or 1)
        content = self._llm_content(
            str(messages[0].get("content") or ""),
            str(messages[1].get("content") or ""),
            activity,
            attempt=attempt,
        )
        return {"content": content}

    def _stage(self, stage_id: str) -> dict:
        for stage in self.run["plan"]["stages"]:
            if stage["id"] == stage_id:
                return stage
        stage = {"id": stage_id, "title": self.stage_titles.get(stage_id, stage_id.title()), "tasks": []}
        self.run["plan"]["stages"].append(stage)
        return stage

    def _task(self, task_id: str) -> dict | None:
        for stage in self.run["plan"]["stages"]:
            for task in stage.get("tasks", []):
                if task["id"] == task_id:
                    return task
        return None

    def add_task(self, stage_id: str, task_id: str, title: str, detail: str = "") -> dict:
        existing = self._task(task_id)
        if existing is not None:
            return existing
        total = sum(len(stage.get("tasks", [])) for stage in self.run["plan"]["stages"])
        if total >= MAX_TASKS:
            raise LimitExceeded("task limit reached")
        task = {
            "id": task_id,
            "stage": stage_id,
            "title": title,
            "detail": detail,
            "status": "queued",
            "error": None,
            "result_refs": [],
            "context_notes": [],
            "started_at": None,
            "finished_at": None,
        }
        self._stage(stage_id)["tasks"].append(task)
        self.save()
        self.emit("task_update", {"task": task})
        return task

    def task_status(self, task: dict, status: str, error: str | None = None) -> None:
        task["status"] = status
        task["error"] = error
        if status == "running" and not task.get("started_at"):
            task["started_at"] = store.utcnow()
        if status in {"completed", "failed", "skipped", "cancelled"}:
            task["finished_at"] = store.utcnow()
        self.save()
        self.emit("task_update", {"task": task})
        if status in {"running", "awaiting_approval"}:
            self.set_activity(
                f"task.{task['stage']}", task["title"], detail=task.get("detail") or None,
                task_id=task["id"],
            )
        elif status == "failed":
            self.set_activity(
                f"task.{task['stage']}.failed", f"{task['title']} failed",
                detail=error, task_id=task["id"],
            )

    def task_detail(self, task: dict, detail: str) -> None:
        """Persist and broadcast a user-facing progress message for a task."""
        if task.get("detail") == detail:
            return
        task["detail"] = detail
        self.save()
        self.emit("task_update", {"task": task})
        if task.get("status") in {"running", "awaiting_approval"}:
            self.set_activity(
                f"task.{task['stage']}", task["title"], detail=detail,
                task_id=task["id"],
            )

    def note_context(self, task: dict, note: str) -> None:
        notes = task.setdefault("context_notes", [])
        if note not in notes:
            notes.append(note)

    def record_model_source(self, source: dict) -> None:
        """Record document/methodology provenance used by later model turns."""
        entry = {
            "source_ref": source.get("source_ref"),
            "document_id": source.get("document_id"),
            "source_sha1": source.get("source_sha1"),
            "pages": [int(page["page"]) for page in source.get("pages") or []],
        }
        with self._state_lock:
            sources = self.run.setdefault("model_sources", [])
            if entry not in sources:
                sources.append(entry)
                self.save()

    def record_artifact(self, kind: str, item_id: str, semantic_id: str, action: str, task: dict | None) -> str:
        ref = f"{kind}:{item_id}"
        self.run.setdefault("artifacts", []).append(
            {"kind": kind, "id": item_id, "semantic_id": semantic_id, "action": action}
        )
        if task is not None and ref not in task["result_refs"]:
            task["result_refs"].append(ref)
        self.save()
        from ..documents import append_activity
        profile = llm.agent_status()
        append_activity(
            self.ws, run_id=self.run["id"], stage=(task or {}).get("id"),
            task=(task or {}).get("id"), purpose="artifact_disposition",
            provider=profile.get("provider"), model=profile.get("model"), vision_used=False,
            prompt_version=None, template_versions=[], knowledge_packs=[], document_ids=[],
            page_ranges=[], source_hashes=[], response_at=store.utcnow(), response_hash=None,
            artifact_ref=ref, disposition="accepted" if self.run.get("mode") == "permission" else "applied",
        )
        self.emit("workspace_changed", {"kind": kind, "id": item_id, "action": action})
        return ref

    def _pending_approval(self, kind: str, task_id: str) -> dict | None:
        return next(
            (
                approval
                for approval in self.run.get("approvals", [])
                if approval.get("kind") == kind
                and approval.get("task_id") == task_id
                and approval.get("status") == "pending"
            ),
            None,
        )

    def request_approval(self, kind: str, task: dict, items: list[dict]) -> list[dict]:
        """Block on an editable approval batch and return the accepted specs.

        Reuses any pending batch for this task so a resumed run re-presents the
        same items instead of proposing them twice.
        """
        approval = self._pending_approval(kind, task["id"])
        if approval is None:
            if not items:
                return []
            approval = {
                "id": uuid.uuid4().hex[:10],
                "kind": kind,
                "task_id": task["id"],
                "status": "pending",
                "created": store.utcnow(),
                "items": items,
            }
            self.run["approvals"].append(approval)
        self.task_status(task, "awaiting_approval")
        self.emit("approval_request", {"approval": approval})
        self.set_status("awaiting_approval")
        waited_from = time.monotonic()
        # Wait durably: the handle delivers decisions in-process, but a
        # decision submitted while no handle was attached only exists on disk,
        # so the run document is re-read each pass. Time spent blocked on the
        # auditor is added back to the deadline rather than counted against it.
        decisions = approval.pop("submitted_decisions", None)
        while decisions is None:
            if self.handle.cancel.is_set():
                raise Cancelled()
            if self.handle.pause_requested.is_set():
                self.set_status("paused")
                while not self.handle.resume.wait(0.2):
                    if self.handle.cancel.is_set():
                        raise Cancelled()
                self.handle.resume.clear()
                self.handle.pause_requested.clear()
                self.set_status("awaiting_approval")
            if self.handle.approval_resolved.wait(0.2):
                self.handle.approval_resolved.clear()
                decisions = self.handle.decisions.pop(approval["id"], None)
            if decisions is None:
                durable = store.load_run(self.ws, self.run["id"])
                current = next(
                    (
                        item for item in durable.get("approvals") or []
                        if item.get("id") == approval["id"]
                    ),
                    None,
                )
                if current and current.get("submitted_decisions") is not None:
                    decisions = current["submitted_decisions"]
        self.deadline += time.monotonic() - waited_from
        # Apply the decisions, then default anything the auditor did not answer
        # to rejected — silence must never be read as consent for a mutation.
        by_id = {item["id"]: item for item in approval["items"]}
        for decision in decisions:
            item = by_id.get(str(decision.get("item_id")))
            action = decision.get("action")
            if item is None or action not in ("approve", "reject", "edit"):
                continue
            if action == "edit":
                item["decision"] = "edited"
                item["edited_spec"] = decision.get("spec")
            else:
                item["decision"] = "approved" if action == "approve" else "rejected"
        for item in approval["items"]:
            if not item.get("decision"):
                item["decision"] = "rejected"
        from ..documents import append_activity
        profile = llm.agent_status()
        for item in approval["items"]:
            append_activity(
                self.ws, run_id=self.run["id"], stage=kind, task=task["id"],
                purpose="auditor_disposition", provider=profile.get("provider"), model=profile.get("model"),
                vision_used=False, prompt_version=None, template_versions=[], knowledge_packs=[],
                document_ids=[], page_ranges=[], source_hashes=[], response_at=store.utcnow(),
                response_hash=None, artifact_ref=f"proposal:{kind}:{item['id']}", disposition=item["decision"],
            )
        approval.update(status="resolved", resolved=store.utcnow())
        approval.pop("response_submitted_at", None)
        # Persist the resolved approval and resumed run status atomically. A
        # save in between exposed an impossible state to pollers: the run said
        # awaiting_approval while no pending approval existed.
        self.run["status"] = "executing"
        self.save()
        self.emit("approval_resolved", {"approval": approval})
        self.emit("run_status", {"status": "executing"})
        self.task_status(task, "running")
        accepted = []
        for item in approval["items"]:
            if item["decision"] == "approved":
                accepted.append({**item, "spec": item["spec"]})
            elif item["decision"] == "edited" and item.get("edited_spec"):
                accepted.append({**item, "spec": item["edited_spec"]})
        return accepted

    def proposal_item(self, title: str, rationale: str, spec: dict, evidence: dict | None = None) -> dict:
        return {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "rationale": rationale,
            "spec": spec,
            "evidence": evidence or {},
            "decision": None,
            "edited_spec": None,
        }
