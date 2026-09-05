"""Shared durable-run plumbing for all audit-assistant run kinds."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .. import llm
from ..workspaces import Workspace
from . import narration, prompts, store
from .context import ContextManifest
from .runtime import (
    Cancelled,
    DefaultModelGateway,
    DefaultRunRuntime,
    LimitExceeded,
    RunRuntime,
)

MAX_TASKS = 60
LLM_JSON_ATTEMPTS = 2


def _plain_json(value: object) -> object:
    """Project frozen worker-proposal data back to plain JSON containers."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


MODEL_WAIT_LABELS = {
    # Every worker that can hold a run needs a line here; an unmapped tag falls
    # back to "Waiting for the model", which tells the auditor nothing about
    # what is actually being worked out.
    "agent:workflow_router": "Working out which workflow this needs",
    "agent:report": "Drafting the audit report",
    "agent:report_editorial": "Editing the audit report",
    "agent:command_interpreter": "Preparing the action plan",
    "agent:command_planner": "Planning the next fieldwork steps",
    "agent:document_context": "Analyzing planning documents",
    "agent:apm": "Drafting the audit planning memorandum",
    "agent:planning_cycle": "Working out the steps of the cycle",
    "agent:rcm": "Drafting the risk and control matrix",
    "agent:rcm_attributes": "Stating what each control has to demonstrate",
    "agent:test_generate": "Generating RCM tests",
    "agent:document_qa": "Answering from document evidence",
    "agent:finding": "Drafting an evidence-linked finding",
    "agent:file_classification": "Classifying staged files",
    "agent:document_analysis_map": "Analyzing document content",
    "agent:document_analysis_reduce": "Consolidating document analysis",
}


_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")


def _markdown_heading_progress(accumulated: str) -> list[dict[str, str]]:
    """Sections seen so far in a markdown draft, last one still in progress.

    A pure read of the response text streamed so far — no foreknowledge of the
    active template's section list is needed, since a heading only needs to be
    recognized once the model has actually written it.
    """
    headings = [match.group(1).strip() for match in _HEADING_RE.finditer(accumulated)]
    if not headings:
        return []
    items = [{"label": heading, "state": "done"} for heading in headings[:-1]]
    items.append({"label": headings[-1], "state": "current"})
    return items[-12:]


_RCM_ROW_FIELD_RE = re.compile(r'"risk_rating"\s*:')


def _rcm_row_progress(accumulated: str) -> list[dict[str, str]]:
    """How many RCM rows the draft has written so far.

    ``risk_rating`` is a required field on every row (agent/workers/planning.py
    ``RCM_ROWS_SYSTEM``), so counting its occurrences in the raw JSON stream is
    a cheap, reliable proxy for rows completed — without waiting for the
    response to parse, which only happens once the whole call has returned.

    Keyed on the rows call alone. The attributes call that follows it streams
    an ``attributes`` array carrying no rating, so this reads nothing there and
    the progress line stops moving rather than counting the same rows twice.
    """
    count = len(_RCM_ROW_FIELD_RE.findall(accumulated))
    if not count:
        return []
    return [{"label": f"{count} row{'s' if count != 1 else ''} drafted", "state": "current"}]


# Keyed the same way as MODEL_WAIT_LABELS. A worker whose response is a large
# markdown document or a long JSON array benefits from a structured read of
# what has streamed so far; a short response does not need one.
PROGRESS_READERS: dict[str, Callable[[str], list[dict[str, str]]]] = {
    "agent:apm": _markdown_heading_progress,
    "agent:rcm": _rcm_row_progress,
}


class BaseRunner:
    """Generic state, checkpoint, LLM, task, approval, and event behavior.

    Concrete runners implement ``execute`` and may provide ``stage_titles``.
    The handle is deliberately duck-typed so the thread-control object stays
    in the orchestration module without introducing an import cycle.
    """

    stage_titles: dict[str, str] = {}

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        state_lock: threading.RLock | None = None,
    ):
        self.ws = workspace
        self.run = run
        self.handle = handle
        # Production launchers persist this before the worker thread starts.
        # Directly constructed runners in local tests/tools still receive a
        # stable in-memory snapshot rather than consulting settings per call.
        if "model_profiles" not in self.run:
            self.run["model_profiles"] = llm.model_profile_snapshot()
        # Independent planning-document requests may run concurrently. Keep
        # provider waits parallel while serializing the shared durable ledger.
        # Two compositions of the *same* durable run — the audit graph and the
        # document capabilities it depends on — must share one lock, or the two
        # would serialize their own writes to the one run record independently.
        self._state_lock = state_lock if state_lock is not None else threading.RLock()
        self.runtime = runtime if runtime is not None else DefaultRunRuntime(
            workspace=self.ws,
            run=self.run,
            state_lock=self._state_lock,
            handle=self.handle,
            limit_error=LimitExceeded,
            cancelled_error=Cancelled,
            task_transition=self.task_status,
            approval_disposition=self._record_approval_disposition,
        )
        self.model_gateway = DefaultModelGateway(
            workspace_id=self.ws.id,
            workspace_root=str(self.ws.root),
            run=self.run,
            state_lock=self._state_lock,
            checkpoint=self.checkpoint,
            reserve_model_turn=self.runtime.reserve_model_turn,
            record_model_usage=self.runtime.record_model_usage,
            model_wait=self.runtime.set_model_wait,
            utcnow=self.runtime.utcnow,
            append_provenance=self._append_model_provenance,
            template_context=self._model_template_context,
            stage_labels=MODEL_WAIT_LABELS,
            limit_error=LimitExceeded,
            stream_sink=self._emit_model_stream,
            progress_readers=PROGRESS_READERS,
            progress_sink=self._emit_model_progress,
            heartbeat_sink=self._emit_model_wait_heartbeat,
        )

    def _emit_model_progress(
        self, tag: str, items: list[dict[str, Any]], call_id: str
    ) -> None:
        """Publish a structured read of the response so far, and only as an event.

        Same non-durable treatment as ``_emit_model_stream``: a reader who is
        not watching live loses nothing, since the eventual milestone states
        the finished result.
        """
        self.runtime.emit(
            "model_progress",
            {"call_id": call_id, "stage": tag, "items": items},
        )

    def _emit_model_wait_heartbeat(self, tag: str) -> None:
        """Say one thing when a call is still running well past normal.

        A real conversational turn, not a log entry: a reader who opens the
        transcript after the call finally settles should still see that it
        ran long, not just its eventual result.
        """
        label = MODEL_WAIT_LABELS.get(tag, "Working")
        narration.say(
            self.run,
            self.emit,
            f"Still {label[0].lower()}{label[1:]} — this is taking longer than usual.",
        )

    def _emit_model_stream(self, tag: str, text: str, call_id: str) -> None:
        """Publish live model text as an event, and only as an event.

        A generation turn against a local model can run for a minute, and a
        static "Waiting for the model" label tells the auditor nothing about
        whether it is progressing. This is deliberately *not* written to
        ``run.json``: the durable record stays content-free apart from the
        artifacts a capability actually commits, and a run that saved on every
        fragment would rewrite the whole record thousands of times per call.
        """
        self.runtime.emit(
            "model_stream",
            {
                # The call id marks where one call's text ends and the next
                # begins. Two calls of the same stage — a retry, or two units of
                # one capability — would otherwise read as one garbled block.
                "call_id": call_id,
                "stage": tag,
                "label": MODEL_WAIT_LABELS.get(tag, "Working"),
                "text": text,
            },
        )
    def save(self) -> None:
        self.runtime.save()

    def emit(self, type_: str, data: dict) -> None:
        self.runtime.emit(type_, data)

    def utcnow(self) -> str:
        return self.runtime.utcnow()

    def mark_started(self) -> str:
        return self.runtime.mark_started()

    def mark_finished(self) -> str:
        return self.runtime.mark_finished()

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
        self.runtime.set_activity(
            phase,
            label,
            detail=detail,
            current=current,
            total=total,
            attempt=attempt,
            task_id=task_id,
            action_id=action_id,
        )

    def set_status(self, status: str) -> None:
        self.runtime.set_status(status)

    def set_model_wait(
        self,
        phase: str,
        label: str,
        *,
        started: bool,
        attempt: int = 1,
    ) -> None:
        self.runtime.set_model_wait(
            phase,
            label,
            started=started,
            attempt=attempt,
        )

    def warn(self, text: str) -> None:
        self.runtime.warn(text)

    def persist_context_manifest(
        self,
        manifest: ContextManifest,
    ) -> dict[str, str]:
        return self.runtime.persist_context_manifest(manifest)

    def load_context_manifest(
        self,
        reference: dict[str, str],
    ) -> ContextManifest:
        return self.runtime.load_context_manifest(reference)

    @property
    def deadline(self) -> float:
        return self.runtime.deadline

    @deadline.setter
    def deadline(self, value: float) -> None:
        self.runtime.deadline = value

    def update_limits(
        self,
        updates: dict[str, int],
        *,
        grow_only: bool = False,
    ) -> dict:
        return self.runtime.update_limits(updates, grow_only=grow_only)

    def reserve_model_turn(self, **fields) -> dict[str, int]:
        return self.runtime.reserve_model_turn(**fields)

    def record_model_usage(self, **fields) -> bool:
        return self.runtime.record_model_usage(**fields)

    def checkpoint(self) -> None:
        self.runtime.checkpoint(drain_inbox=self._drain_inbox)

    def drain_inbox(self, *, queue_commands: bool = False) -> None:
        self.runtime.drain_inbox(queue_commands=queue_commands)

    def _drain_inbox(self) -> None:
        self.runtime.drain_inbox()

    def wait_for_input(self, question: str) -> str:
        return self.runtime.wait_for_input(question)

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

    def _llm_message(
        self,
        system: str,
        conversation: list[dict],
        tools: list[dict],
        activity: dict | None = None,
        *,
        attempt: int = 1,
    ) -> dict:
        """Run one budgeted tool-capable turn and preserve its wire message."""
        message = self.model_gateway.complete(
            system,
            "",
            activity,
            attempt=attempt,
            tools=tools,
            conversation=conversation,
            return_message=True,
        )
        if not isinstance(message, dict):
            raise llm.LLMError("The model gateway did not return a tool-capable message.")
        return message

    def _append_model_provenance(self, fields: dict) -> None:
        from ..documents import append_activity

        append_activity(self.ws, **fields)

    def _model_template_context(self, tag: str) -> dict | None:
        template_name = {
            "agent:apm": "apm",
            "agent:rcm": "rcm",
            # Its own entry, so the activity record says which of the two
            # matrix templates a given turn was written against — and an
            # override of one is visible on the call it actually reached.
            "agent:rcm_attributes": "rcm_attributes",
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
            task["started_at"] = self.utcnow()
        if status in {"completed", "failed", "skipped", "cancelled"}:
            task["finished_at"] = self.utcnow()
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
            page_ranges=[], source_hashes=[], response_at=self.utcnow(), response_hash=None,
            artifact_ref=ref, disposition="accepted" if self.run.get("mode") == "permission" else "applied",
        )
        self.emit("workspace_changed", {"kind": kind, "id": item_id, "action": action})
        return ref

    def request_approval(self, kind: str, task: dict, items: list[dict]) -> list[dict]:
        return self.runtime.request_approval(kind, task, items)

    def wait_for_interaction(
        self,
        interaction: dict,
        *,
        waiting_status: str = "awaiting_input",
        queue_commands: bool = False,
        poll_interval: float = 0.1,
    ) -> dict:
        return self.runtime.wait_for_interaction(
            interaction,
            waiting_status=waiting_status,
            queue_commands=queue_commands,
            poll_interval=poll_interval,
        )

    def resolve_interaction(
        self,
        interaction: dict,
        response: dict,
        *,
        event_data: dict | None = None,
        persist: Callable[[], None] | None = None,
    ) -> None:
        self.runtime.resolve_interaction(
            interaction,
            response,
            event_data=event_data,
            persist=persist,
        )

    def _record_approval_disposition(
        self,
        kind: str,
        task: dict,
        item: dict,
    ) -> None:
        from ..documents import append_activity
        profile = llm.agent_status()
        append_activity(
            self.ws,
            run_id=self.run["id"],
            stage=kind,
            task=task["id"],
            purpose="approval_recorded",
            provider=profile.get("provider"),
            model=profile.get("model"),
            vision_used=False,
            prompt_version=None,
            template_versions=[],
            knowledge_packs=[],
            document_ids=[],
            page_ranges=[],
            source_hashes=[],
            response_at=self.utcnow(),
            response_hash=None,
            artifact_ref=f"proposal:{kind}:{item['id']}",
            disposition=item["decision"],
        )

    def proposal_item(self, title: str, rationale: str, spec: dict, evidence: dict | None = None) -> dict:
        return {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "rationale": rationale,
            # An approval item is persisted verbatim into ``run.json``, and a
            # worker proposal arrives recursively frozen (``MappingProxyType``
            # objects and tuples). Project it back to plain JSON here, at the
            # one boundary every engine's approval batch passes through.
            "spec": _plain_json(spec),
            "evidence": _plain_json(evidence or {}),
            "decision": None,
            "edited_spec": None,
        }
