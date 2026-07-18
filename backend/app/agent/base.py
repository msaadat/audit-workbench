"""Shared durable-run plumbing for all audit-assistant run kinds."""

from __future__ import annotations

import json
import hashlib
import time
import uuid

from .. import llm
from ..workspaces import Workspace
from . import prompts, store

MAX_LLM_TURNS = 40
MAX_TASKS = 60
MAX_RUNTIME_SECONDS = 1800
LLM_JSON_ATTEMPTS = 2


class Cancelled(Exception):
    pass


class LimitExceeded(Exception):
    pass


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

    def save(self) -> None:
        store.save_run(self.ws, self.run)

    def emit(self, type_: str, data: dict) -> None:
        store.append_event(self.ws, self.run["id"], type_, data)

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

    def llm_json(self, system: str, user: str, activity: dict | None = None) -> dict:
        last_error = ""
        attempt_user = user
        for _ in range(LLM_JSON_ATTEMPTS):
            self.checkpoint()
            usage = self.run.setdefault("usage", {})
            if usage.get("llm_turns", 0) >= MAX_LLM_TURNS:
                raise LimitExceeded("model turn limit reached")
            usage["llm_turns"] = usage.get("llm_turns", 0) + 1
            self.save()
            message = llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": attempt_user}],
                profile="agent",
            )
            # The ledger stores provenance and hashes, never full prompt or
            # document content.
            from ..documents import append_activity
            tag = system.split("]", 1)[0].lstrip("[") if system.startswith("[") else "agent"
            profile = llm.agent_status()
            sources = list(self.run.get("model_sources") or [])
            activity_fields = dict(activity or {})
            template_name = {
                "agent:apm": "apm", "agent:rcm": "rcm",
                "agent:work_program": "workpaper",
            }.get(tag)
            template_versions = []
            if template_name:
                from .. import templates_store
                active = templates_store.get_template(self.ws, template_name)
                template_versions.append({
                    "name": template_name, "source": active["source"],
                    "sha1": hashlib.sha1(active["markdown"].encode("utf-8")).hexdigest(),
                })
            append_activity(
                self.ws, run_id=self.run["id"], stage=tag, task=None, purpose=tag,
                provider=profile.get("provider"), model=profile.get("model"), vision_used=False,
                prompt_version=hashlib.sha1(f"{system}\n{user}".encode("utf-8")).hexdigest(),
                template_versions=template_versions,
                knowledge_packs=[{"source_ref": item["source_ref"], "sha1": item.get("source_sha1")} for item in sources if str(item.get("source_ref", "")).startswith("pack:")],
                document_ids=activity_fields.pop("document_ids", [item["document_id"] for item in sources if not str(item.get("source_ref", "")).startswith("pack:")]),
                page_ranges=activity_fields.pop("page_ranges", sorted({page for item in sources for page in item.get("pages", [])})),
                source_hashes=activity_fields.pop("source_hashes", sorted({item["source_sha1"] for item in sources if item.get("source_sha1")})),
                response_at=store.utcnow(), response_hash=hashlib.sha1(str(message.get("content") or "").encode("utf-8")).hexdigest(),
                artifact_ref=None, disposition="generated",
                **activity_fields,
            )
            try:
                return prompts.parse_json_object(message.get("content") or "")
            except (ValueError, json.JSONDecodeError) as error:
                last_error = str(error)
                attempt_user = f"{user}\n\nYour previous response could not be used: {last_error}. {prompts.JSON_RULES}"
        raise llm.LLMError(f"The model did not return usable JSON: {last_error}")

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
        }
        self._stage(stage_id)["tasks"].append(task)
        self.save()
        self.emit("task_update", {"task": task})
        return task

    def task_status(self, task: dict, status: str, error: str | None = None) -> None:
        task["status"] = status
        task["error"] = error
        self.save()
        self.emit("task_update", {"task": task})

    def task_detail(self, task: dict, detail: str) -> None:
        """Persist and broadcast a user-facing progress message for a task."""
        if task.get("detail") == detail:
            return
        task["detail"] = detail
        self.save()
        self.emit("task_update", {"task": task})

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
        decisions = None
        while decisions is None:
            if self.handle.cancel.is_set():
                raise Cancelled()
            if self.handle.approval_resolved.wait(0.2):
                self.handle.approval_resolved.clear()
                decisions = self.handle.decisions.pop(approval["id"], None)
        self.deadline += time.monotonic() - waited_from
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
