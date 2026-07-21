"""Durable auditor-response transitions shared by run schedulers."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from ...workspaces import WorkspaceError
from .. import store


class InteractionTransitions:
    """Own blocking approval, input, and structured-response transitions."""

    def __init__(
        self,
        *,
        workspace: Any,
        run: dict[str, Any],
        handle: Any | None,
        save: Callable[[], None],
        emit: Callable[[str, dict[str, Any]], None],
        set_status: Callable[[str], None],
        utcnow: Callable[[], str],
        monotonic: Callable[[], float] | None = None,
        extend_deadline: Callable[[float], None],
        cancelled_error: type[Exception],
        task_transition: Callable[[dict[str, Any], str], None] | None = None,
        approval_disposition: Callable[[str, dict[str, Any], dict[str, Any]], None]
        | None = None,
    ):
        self.workspace = workspace
        self.run = run
        self.handle = handle
        self._save = save
        self._emit = emit
        self._set_status = set_status
        self._utcnow = utcnow
        self._monotonic = monotonic or time.monotonic
        self._extend_deadline = extend_deadline
        self._cancelled_error = cancelled_error
        self._task_transition = task_transition or self._default_task_transition
        self._approval_disposition = approval_disposition

    def _require_handle(self) -> Any:
        if self.handle is None:
            raise RuntimeError("Auditor interaction requires a live run handle")
        return self.handle

    def _default_task_transition(self, task: dict[str, Any], status: str) -> None:
        task["status"] = status
        self._save()
        self._emit("task_update", {"task": task})

    def _pause_while_blocked(self, waiting_status: str) -> None:
        handle = self._require_handle()
        if not handle.pause_requested.is_set():
            return
        self._set_status("paused")
        while not handle.resume.wait(0.2):
            if handle.cancel.is_set():
                raise self._cancelled_error()
        handle.resume.clear()
        handle.pause_requested.clear()
        self._set_status(waiting_status)

    def wait_for_input(self, question: str) -> str:
        """Persist one free-text question and wait for an in-process answer."""
        for message in self.run.get("messages", []):
            if message.get("role") == "user" and not message.get("handled", True):
                message["handled"] = True
                self.run.setdefault("interview", {})["pending_question"] = None
                self._save()
                return str(message.get("content") or "")

        interview = self.run.setdefault(
            "interview", {"captured": {}, "turns": 0, "pending_question": None}
        )
        if interview.get("pending_question") != question:
            message = {
                "role": "agent",
                "content": question,
                "at": self._utcnow(),
                "handled": True,
            }
            self.run.setdefault("messages", []).append(message)
            interview["pending_question"] = question
            self._save()
            self._emit("message", {"message": message})

        handle = self._require_handle()
        self._set_status("awaiting_input")
        waited_from = self._monotonic()
        while True:
            if handle.cancel.is_set():
                raise self._cancelled_error()
            self._pause_while_blocked("awaiting_input")
            with handle.lock:
                content = handle.inbox.pop(0) if handle.inbox else None
            if content is not None:
                self.run.setdefault("messages", []).append(
                    {
                        "role": "user",
                        "content": content,
                        "at": self._utcnow(),
                        "handled": True,
                    }
                )
                interview["pending_question"] = None
                self._extend_deadline(self._monotonic() - waited_from)
                self._save()
                self._set_status("executing")
                return str(content)
            time.sleep(0.2)

    def request_approval(
        self,
        kind: str,
        task: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Block on an editable, restart-safe approval batch."""
        approval = next(
            (
                candidate
                for candidate in self.run.get("approvals", [])
                if candidate.get("kind") == kind
                and candidate.get("task_id") == task["id"]
                and candidate.get("status") == "pending"
            ),
            None,
        )
        if approval is None:
            if not items:
                return []
            approval = {
                "id": uuid.uuid4().hex[:10],
                "kind": kind,
                "task_id": task["id"],
                "status": "pending",
                "created": self._utcnow(),
                "items": items,
            }
            self.run.setdefault("approvals", []).append(approval)

        self._task_transition(task, "awaiting_approval")
        self._emit("approval_request", {"approval": approval})
        decisions = self._wait_for_approval(approval)

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
                item["decision"] = (
                    "approved" if action == "approve" else "rejected"
                )
        for item in approval["items"]:
            if not item.get("decision"):
                item["decision"] = "rejected"
            if self._approval_disposition is not None:
                self._approval_disposition(kind, task, item)

        approval.update(status="resolved", resolved=self._utcnow())
        approval.pop("response_submitted_at", None)
        self.run["status"] = "executing"
        self._save()
        self._emit("approval_resolved", {"approval": approval})
        self._emit("run_status", {"status": "executing"})
        self._task_transition(task, "running")

        accepted = []
        for item in approval["items"]:
            if item["decision"] == "approved":
                accepted.append({**item, "spec": item["spec"]})
            elif item["decision"] == "edited" and item.get("edited_spec"):
                accepted.append({**item, "spec": item["edited_spec"]})
        return accepted

    def _wait_for_approval(self, approval: dict[str, Any]) -> list[dict[str, Any]]:
        handle = self._require_handle()
        self._set_status("awaiting_approval")
        waited_from = self._monotonic()
        decisions = approval.pop("submitted_decisions", None)
        while decisions is None:
            if handle.cancel.is_set():
                raise self._cancelled_error()
            self._pause_while_blocked("awaiting_approval")
            if handle.approval_resolved.wait(0.2):
                handle.approval_resolved.clear()
                decisions = handle.decisions.pop(approval["id"], None)
            if decisions is None:
                durable = store.load_run(self.workspace, self.run["id"])
                current = next(
                    (
                        item
                        for item in durable.get("approvals") or []
                        if item.get("id") == approval["id"]
                    ),
                    None,
                )
                if current and current.get("submitted_decisions") is not None:
                    decisions = current["submitted_decisions"]
        self._extend_deadline(self._monotonic() - waited_from)
        return list(decisions or [])

    def wait_for_interaction(
        self,
        interaction: dict[str, Any],
        *,
        waiting_status: str = "awaiting_input",
        queue_commands: bool = False,
        poll_interval: float = 0.1,
    ) -> dict[str, Any]:
        """Wait for a structured response, including one persisted offline."""
        handle = self._require_handle()
        self._set_status(waiting_status)
        waited_from = self._monotonic()
        response = interaction.pop("submitted_response", None)
        while response is None:
            if handle.cancel.is_set():
                raise self._cancelled_error()
            self._pause_while_blocked(waiting_status)
            if queue_commands and handle.command_queued.is_set():
                self._drain_queued_commands()
            if handle.interaction_resolved.wait(poll_interval):
                handle.interaction_resolved.clear()
                with handle.lock:
                    response = handle.interaction_responses.pop(
                        interaction["id"], None
                    )
            if response is None:
                durable = store.load_run(self.workspace, self.run["id"])
                current = next(
                    (
                        item
                        for item in durable.get("interactions") or []
                        if item.get("id") == interaction["id"]
                    ),
                    None,
                )
                if current and current.get("submitted_response") is not None:
                    response = current["submitted_response"]
        self._extend_deadline(self._monotonic() - waited_from)
        self._set_status("executing")
        return dict(response or {})

    def _drain_queued_commands(self) -> None:
        handle = self._require_handle()
        with handle.lock:
            pending, handle.command_queue = handle.command_queue[:], []
        handle.command_queued.clear()
        if pending:
            self.run.setdefault("pending_commands", []).extend(pending)
            self._save()

    def resolve_interaction(
        self,
        interaction: dict[str, Any],
        response: dict[str, Any],
        *,
        event_data: dict[str, Any] | None = None,
        persist: Callable[[], None] | None = None,
    ) -> None:
        """Persist a scheduler-interpreted structured response and emit it."""
        interaction.pop("submitted_response", None)
        interaction.update(
            status="resolved",
            response=dict(response or {}),
            actor="auditor",
            resolved_at=self._utcnow(),
        )
        (persist or self._save)()
        payload = {"interaction_id": interaction["id"]}
        payload.update(event_data or {})
        self._emit("interaction_resolved", payload)


def submit_approval_response(
    workspace: Any,
    run_id: str,
    approval_id: str,
    decisions: list[dict[str, Any]],
    *,
    handle: Any | None,
) -> dict[str, Any]:
    """Persist an approval response before notifying an optional live worker."""
    run = store.load_run(workspace, run_id)
    approval = next(
        (item for item in run.get("approvals") or [] if item["id"] == approval_id),
        None,
    )
    if approval is None:
        raise WorkspaceError("Approval not found on this run.")
    if approval["status"] != "pending":
        raise WorkspaceError("This approval batch was already resolved.")
    normalized = list(decisions or [])
    approval["submitted_decisions"] = normalized
    approval["response_submitted_at"] = store.utcnow()
    store.save_run(workspace, run)
    store.append_event(
        workspace,
        run_id,
        "approval_response_stored",
        {"approval_id": approval_id},
    )
    if handle is not None:
        handle.decisions[approval_id] = normalized
        handle.approval_resolved.set()
    elif run["status"] not in store.TERMINAL_STATUSES:
        run["status"] = "interrupted"
        store.save_run(workspace, run)
    return run


def submit_interaction_response(
    workspace: Any,
    run_id: str,
    interaction_id: str,
    response: dict[str, Any],
    *,
    handle: Any | None,
) -> dict[str, Any]:
    """Persist a structured response before notifying an optional live worker."""
    run = store.load_run(workspace, run_id)
    interaction = next(
        (
            item
            for item in run.get("interactions") or []
            if item["id"] == interaction_id
        ),
        None,
    )
    if interaction is None:
        raise WorkspaceError("Interaction not found on this run.")
    if interaction["status"] != "pending":
        raise WorkspaceError("This interaction was already resolved.")
    normalized = dict(response or {})
    interaction["submitted_response"] = normalized
    interaction["response_submitted_at"] = store.utcnow()
    store.save_run(workspace, run)
    store.append_event(
        workspace,
        run_id,
        "interaction_response_stored",
        {"interaction_id": interaction_id},
    )
    if handle is not None:
        with handle.lock:
            handle.interaction_responses[interaction_id] = normalized
        handle.interaction_resolved.set()
    elif run["status"] not in store.TERMINAL_STATUSES:
        run["status"] = "interrupted"
        store.save_run(workspace, run)
    return run
