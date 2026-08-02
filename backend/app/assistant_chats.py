"""Durable workspace-scoped assistant conversations.

Chats own conversational state and local Q&A artifacts.  AgentRuns remains the
authoritative execution ledger; chat reads only project linked run records.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import assistant, dashboard, debug_store, llm
from .agent import commands, narration, routing, runner, store
from .workspaces import Workspace, WorkspaceError, write_json_atomic

CHATS_DIRNAME = "AssistantChats"
CHAT_SCHEMA_VERSION = 1
# A pending message younger than this is assumed to still be processing (the
# in-process marker cannot be seen from other workers); older ones are
# recovered as failed.
PENDING_RECOVERY_GRACE_SECONDS = 15 * 60
# Derived transcript items carry no chat ordinal; they sort after every chat
# message that shares their timestamp.
DERIVED_ORDINAL = 1e12
# A milestone and the handoff line the agent says about it are written in the
# same breath, so they routinely share a timestamp to the millisecond. The
# result belongs above the sentence that points at what comes next, and leaving
# that to the id tiebreak decided it alphabetically — "message" before
# "milestone" — which is backwards, and only stable by accident.
MILESTONE_ORDINAL = DERIVED_ORDINAL - 1
# The run card is a receipt, not a bulletin posted the moment the run starts:
# it belongs after everything the run has said or asked about so far, not
# pinned to the message that launched it. Larger than the default derived
# ordinal so it sorts after a plain message sharing its chased timestamp.
RUN_CARD_ORDINAL = DERIVED_ORDINAL + 1
CHAT_ID_RE = re.compile(r"chat_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}\Z")
MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{12}\Z")
ARTIFACT_ID_RE = re.compile(r"art_[0-9a-f]{12}\Z")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{8,160}\Z")
RUN_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\Z")
INTENTS = {"auto", "ask", "act"}
SOURCES = {"composer", "shortcut", "tab_button", "folder_intake"}

_file_locks: dict[str, threading.RLock] = {}
_file_locks_guard = threading.Lock()
_processing_locks: dict[str, threading.Lock] = {}
_processing_locks_guard = threading.Lock()
_processing_paths: set[str] = set()


class RevisionConflict(WorkspaceError):
    """An optimistic artifact edit used an old revision."""


def utcnow() -> str:
    # Millisecond precision keeps transcript ordering stable: chat messages,
    # run projections, and run events are interleaved by timestamp string.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _file_locks_guard:
        return _file_locks.setdefault(key, threading.RLock())


def _processing_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _processing_locks_guard:
        return _processing_locks.setdefault(key, threading.Lock())


def _validate(value: str, pattern: re.Pattern[str], label: str) -> str:
    value = str(value or "")
    if not pattern.fullmatch(value):
        raise WorkspaceError(f"Invalid {label}.")
    return value


def chats_dir(workspace: Workspace) -> Path:
    return workspace.root / CHATS_DIRNAME


def chat_dir(workspace: Workspace, chat_id: str) -> Path:
    _validate(chat_id, CHAT_ID_RE, "chat id")
    return chats_dir(workspace) / chat_id


def _chat_path(workspace: Workspace, chat_id: str) -> Path:
    return chat_dir(workspace, chat_id) / "chat.json"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def create_chat(workspace: Workspace) -> dict:
    now = datetime.now(timezone.utc)
    chat_id = f"chat_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    record = {
        "schema_version": CHAT_SCHEMA_VERSION,
        "id": chat_id,
        "workspace_id": workspace.id,
        "title": "New chat",
        "title_source": "auto",
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "next_ordinal": 1,
        "composer_context": {"document_ids": []},
        "messages": [],
    }
    write_json_atomic(_chat_path(workspace, chat_id), record)
    return get_chat(workspace, chat_id)


def _read(workspace: Workspace, chat_id: str, *, recover: bool = True) -> dict:
    path = _chat_path(workspace, chat_id)
    if not path.is_file():
        raise WorkspaceError(f"Assistant chat '{chat_id}' not found.")
    with _lock(path):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError(f"Assistant chat '{chat_id}' is unreadable.") from error
        if record.get("id") != chat_id or record.get("workspace_id") != workspace.id:
            raise WorkspaceError("Assistant chat identity does not match its workspace.")
        record.setdefault("composer_context", {"document_ids": []})
        record.setdefault("messages", [])
        record.setdefault("next_ordinal", len(record["messages"]) + 1)
        key = str(path.resolve())
        if recover and key not in _processing_paths:
            pending = [
                item for item in record["messages"]
                if item.get("state") == "pending" and _pending_expired(item)
            ]
            if pending:
                for item in pending:
                    item["state"] = "failed"
                    item["error"] = "Interrupted before completion. Retry this request to continue."
                record["updated_at"] = utcnow()
                write_json_atomic(path, record)
        return record


def _pending_expired(message: dict) -> bool:
    """True when a pending message is old enough to be a genuine orphan.

    The in-process ``_processing_paths`` marker is invisible to other worker
    processes, so a fresh pending message read elsewhere must not be failed
    while its LLM round-trip may still be running. Unparseable or missing
    timestamps are treated as expired so real orphans are always recovered.
    """
    raw = str(message.get("created_at") or "")
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds()
    return age > PENDING_RECOVERY_GRACE_SECONDS


def _summary(record: dict) -> dict:
    return {key: record.get(key) for key in (
        "id", "workspace_id", "title", "title_source", "created_at", "updated_at"
    )} | {"message_count": len(record.get("messages") or [])}


def list_chats(workspace: Workspace, *, limit: int = 50, cursor: int = 0) -> dict:
    limit = min(max(int(limit), 1), 100)
    cursor = max(int(cursor), 0)
    root = chats_dir(workspace)
    records: list[dict] = []
    if root.exists():
        for folder in root.iterdir():
            if not CHAT_ID_RE.fullmatch(folder.name):
                continue
            try:
                records.append(_read(workspace, folder.name))
            except WorkspaceError:
                continue
    records.sort(key=lambda item: (str(item.get("updated_at") or ""), item["id"]), reverse=True)
    page = records[cursor:cursor + limit]
    next_cursor = cursor + limit if cursor + limit < len(records) else None
    return {"chats": [_summary(item) for item in page], "next_cursor": next_cursor}


def _clean_title(content: str) -> str:
    value = " ".join("".join(ch for ch in str(content) if ch >= " " and ch != "\x7f").split())
    if len(value) <= 60:
        return value or "New chat"
    return value[:59].rstrip() + "…"


def update_chat(workspace: Workspace, chat_id: str, changes: dict) -> dict:
    allowed = {"title", "document_ids"}
    if set(changes) - allowed:
        raise WorkspaceError("Unknown assistant chat field.")
    path = _chat_path(workspace, chat_id)
    with _lock(path):
        record = _read(workspace, chat_id, recover=False)
        if "title" in changes:
            title = _clean_title(changes["title"])
            if not str(changes["title"] or "").strip():
                raise WorkspaceError("Chat title is required.")
            record["title"] = title
            record["title_source"] = "user"
        if "document_ids" in changes:
            values = changes["document_ids"]
            if not isinstance(values, list):
                raise WorkspaceError("document_ids must be an array.")
            known = {str(item.get("id")) for item in workspace.documents}
            normalized = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
            missing = [value for value in normalized if value not in known]
            if missing:
                raise WorkspaceError(f"Document '{missing[0]}' not found.")
            record["composer_context"] = {"document_ids": normalized}
        record["updated_at"] = utcnow()
        write_json_atomic(path, record)
    return get_chat(workspace, chat_id)


def delete_chat(workspace: Workspace, chat_id: str) -> None:
    target = chat_dir(workspace, chat_id).resolve()
    root = chats_dir(workspace).resolve()
    if not target.is_relative_to(root) or target.parent != root:
        raise WorkspaceError("Unsafe assistant chat path.")
    if not (target / "chat.json").is_file():
        raise WorkspaceError(f"Assistant chat '{chat_id}' not found.")
    # Runs outlive the chat directory that launched them. A non-terminal one
    # left behind becomes the workspace's "active run" indefinitely and
    # reappears at the bottom of every new chat with no conversation to return
    # to, so deleting the conversation stops the work it started.
    for summary in store.list_runs(workspace):
        if summary.get("chat_id") != chat_id:
            continue
        if summary.get("status") in store.TERMINAL_STATUSES:
            continue
        try:
            runner.cancel_run(
                workspace, str(summary["id"]),
                reason="The chat that started this run was deleted.",
                source="chat_delete",
            )
        except WorkspaceError:
            continue
    shutil.rmtree(target)
    # Drop the per-path locks so long-lived processes don't accumulate one
    # entry per deleted chat.
    key = str((target / "chat.json"))
    with _file_locks_guard:
        _file_locks.pop(key, None)
    with _processing_locks_guard:
        _processing_locks.pop(key, None)


def capabilities() -> dict:
    assistant_status = llm.status()
    agent_status = llm.agent_status()
    return {
        "ask": bool(assistant_status.get("configured")),
        "act": bool(agent_status.get("configured")),
        "assistant": assistant_status,
        "agent": agent_status,
        "commands": commands.list_for_ui(),
    }


def _document_snapshot(workspace: Workspace, document_ids: list[str], *, manifest=None) -> dict:
    by_id = {str(item.get("id")): item for item in workspace.documents}
    return {
        "document_ids": list(document_ids),
        "sources": [
            {"document_id": value, "sha1": by_id[value].get("sha1")}
            for value in document_ids if value in by_id
        ],
        "manifest": list(manifest or []),
    }


def _context_matches(current: dict, prior: dict | None) -> bool:
    if not prior:
        return not current["document_ids"]
    if current["document_ids"] != list(prior.get("document_ids") or []):
        return False
    return current["sources"] == list(prior.get("sources") or [])


def _eligible_history(record: dict, current: dict) -> list[dict]:
    messages = [item for item in record.get("messages") or [] if item.get("state") == "complete"]
    selected: list[tuple[dict, dict]] = []
    index = len(messages) - 1
    while index >= 1 and len(selected) < 4:
        answer, user = messages[index], messages[index - 1]
        # A coordinated turn that started a run resolves to "act" but still
        # produced an ordinary text reply, and it is exactly the context a
        # follow-up needs ("did that finish?"). Only turns with no text reply —
        # clarifications, deterministic command launches — end the chain.
        if not (
            user.get("role") == "user" and answer.get("role") == "assistant"
            and user.get("resolved_intent") in {"ask", "act"}
            and answer.get("resolved_intent") in {"ask", "act"}
            and user.get("kind") == answer.get("kind") == "text"
        ):
            break
        if not _context_matches(current, user.get("document_context")):
            break
        if not _context_matches(current, answer.get("document_context")):
            break
        selected.append((user, answer))
        index -= 2
    turns = []
    for user, answer in reversed(selected):
        turns.extend([
            {"role": "user", "content": user.get("content") or ""},
            {"role": "assistant", "content": answer.get("content") or ""},
        ])
    return turns


def _safe_trace(steps: list[dict]) -> list[dict]:
    trace = []
    for step in steps[:20]:
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        summary = {}
        for key, value in args.items():
            if key == "code":
                summary[key] = f"{len(str(value))} characters"
            elif isinstance(value, (str, int, float, bool)) or value is None:
                summary[key] = str(value)[:120]
            elif isinstance(value, list):
                summary[key] = f"{len(value)} items"
            elif isinstance(value, dict):
                summary[key] = f"{len(value)} fields"
        trace.append({"tool": str(step.get("tool") or ""), "ok": bool(step.get("ok")), "args": summary})
    return trace


def _write_artifacts(workspace: Workspace, chat_id: str, message_id: str, values: list[dict]) -> list[str]:
    ids = []
    for value in values:
        artifact_id = _new_id("art")
        now = utcnow()
        payload = {
            **value, "id": artifact_id, "chat_id": chat_id, "message_id": message_id,
            "created_at": now, "updated_at": now, "revision": 1,
            "last_run_at": now, "last_error": value.get("error"),
        }
        path = chat_dir(workspace, chat_id) / "artifacts" / f"{artifact_id}.json"
        write_json_atomic(path, payload)
        ids.append(artifact_id)
    return ids


def _artifact(workspace: Workspace, chat_id: str, artifact_id: str) -> dict:
    _validate(artifact_id, ARTIFACT_ID_RE, "artifact id")
    path = chat_dir(workspace, chat_id) / "artifacts" / f"{artifact_id}.json"
    if not path.is_file():
        raise WorkspaceError(f"Assistant artifact '{artifact_id}' not found.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Assistant artifact '{artifact_id}' is unreadable.") from error
    if value.get("chat_id") != chat_id:
        raise WorkspaceError("Assistant artifact does not belong to this chat.")
    return value


def update_artifact(workspace: Workspace, chat_id: str, artifact_id: str, changes: dict, revision: int) -> dict:
    allowed = {"code", "title", "viz"}
    if set(changes) - allowed:
        raise WorkspaceError("Only code, title, and visualization settings are editable.")
    path = chat_dir(workspace, chat_id) / "artifacts" / f"{_validate(artifact_id, ARTIFACT_ID_RE, 'artifact id')}.json"
    with _lock(path):
        value = _artifact(workspace, chat_id, artifact_id)
        try:
            expected_revision = int(revision)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Artifact revision is required.") from error
        if expected_revision != int(value.get("revision") or 0):
            raise RevisionConflict("This artifact changed after it was opened. Reload and try again.")
        for key, item in changes.items():
            value[key] = item
        if "code" in changes:
            value["spec"] = {**dict(value.get("spec") or {}), "code": str(changes["code"] or "")}
        value["revision"] = int(value.get("revision") or 0) + 1
        value["updated_at"] = utcnow()
        write_json_atomic(path, value)
    _touch(workspace, chat_id)
    return value


def rerun_artifact(workspace: Workspace, chat_id: str, artifact_id: str, revision: int) -> dict:
    path = chat_dir(workspace, chat_id) / "artifacts" / f"{_validate(artifact_id, ARTIFACT_ID_RE, 'artifact id')}.json"
    with _lock(path):
        value = _artifact(workspace, chat_id, artifact_id)
        try:
            expected_revision = int(revision)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Artifact revision is required.") from error
        if expected_revision != int(value.get("revision") or 0):
            raise RevisionConflict("This artifact changed after it was opened. Reload and try again.")
        if value.get("kind") != "python" or "code" not in value:
            raise WorkspaceError("Only editable Python artifacts can be rerun.")
        try:
            result = assistant.run_python_snippet(workspace, str(value.get("code") or ""))
            value.update(result)
            value["last_error"] = None
            value["error"] = None
        except Exception as error:
            value["last_error"] = str(error)
            value["error"] = str(error)
            value["revision"] = int(value.get("revision") or 0) + 1
            value["updated_at"] = value["last_run_at"] = utcnow()
            write_json_atomic(path, value)
            _touch(workspace, chat_id)
            raise
        value["revision"] = int(value.get("revision") or 0) + 1
        value["updated_at"] = value["last_run_at"] = utcnow()
        value["spec"] = {**dict(value.get("spec") or {}), "code": value["code"]}
        write_json_atomic(path, value)
    _touch(workspace, chat_id)
    return value


def _touch(workspace: Workspace, chat_id: str) -> None:
    path = _chat_path(workspace, chat_id)
    with _lock(path):
        record = _read(workspace, chat_id, recover=False)
        record["updated_at"] = utcnow()
        write_json_atomic(path, record)


def _orphaned_run(workspace: Workspace, summary: dict) -> bool:
    """True when the chat that started this run no longer exists.

    An orphan has nowhere to be answered and nothing to resume it, so it must
    not count as the workspace's active run: steering would queue commands onto
    a record that will never execute them, and the messages would vanish.

    A run with no ``chat_id`` at all (started from a tab button) is not
    orphaned — it never had a conversation to lose.
    """
    owner = str(summary.get("chat_id") or "")
    if not owner:
        return False
    try:
        return not (chat_dir(workspace, owner) / "chat.json").is_file()
    except WorkspaceError:
        # A malformed chat id can never name a chat that exists.
        return True


def _active_run(workspace: Workspace, summaries: list[dict] | None = None) -> dict | None:
    active = set(store.ACTIVE_STATUSES) | set(store.RESUMABLE_STATUSES)
    if summaries is None:
        summaries = store.list_runs(workspace)
    matches = [
        item for item in summaries
        if item.get("status") in active and not _orphaned_run(workspace, item)
    ]
    if not matches:
        return None
    return store.load_run(workspace, matches[0]["id"])


# Intent is no longer guessed ahead of the work. A message resolves to a
# command deterministically (a slash command, a caller-supplied command id, or
# an exact registered phrase), or it goes to the assistant tool loop, which
# decides for itself — by ordinary tool-calling — whether to answer the message
# or hand it to the command runner. See :func:`_commander`.


def _append_assistant(record: dict, *, kind: str, content: str, resolved: str, reply_to: str, error=None, **extra) -> dict:
    message = {
        "id": _new_id("msg"), "ordinal": record["next_ordinal"],
        "role": "assistant", "kind": kind, "content": content,
        "created_at": utcnow(), "request_id": None, "state": "complete" if not error else "failed",
        "requested_intent": None, "resolved_intent": resolved, "reply_to_id": reply_to,
        "document_context": extra.pop("document_context", None), "artifact_ids": extra.pop("artifact_ids", []),
        "outcome": extra.pop("outcome", None), "error": error, **extra,
    }
    record["next_ordinal"] += 1
    record["messages"].append(message)
    return message


def _finalize(workspace: Workspace, chat_id: str, user_id: str, updater) -> None:
    path = _chat_path(workspace, chat_id)
    with _lock(path):
        record = _read(workspace, chat_id, recover=False)
        user = next(item for item in record["messages"] if item["id"] == user_id)
        updater(record, user)
        record["updated_at"] = utcnow()
        write_json_atomic(path, record)


def send_message(workspace: Workspace, chat_id: str, payload: dict) -> dict:
    content = str(payload.get("content") or "").strip()
    if not content:
        raise WorkspaceError("Write a message first.")
    requested = str(payload.get("intent") or "auto")
    if requested not in INTENTS:
        raise WorkspaceError("Message intent must be auto, ask, or act.")
    source = str(payload.get("source") or "composer")
    if source not in SOURCES:
        raise WorkspaceError("Unknown assistant message source.")
    mode = str(payload.get("mode") or "auto")
    if mode not in store.MODES:
        raise WorkspaceError("Message mode must be auto or permission.")
    request_id = _validate(str(payload.get("request_id") or ""), REQUEST_ID_RE, "request id")
    goal_template = str(payload.get("goal_template") or "").strip() or None
    command_id = str(payload.get("command") or "").strip() or None
    local_handler = None
    if command_id:
        matched_command = commands.COMMANDS.get(command_id)
        if matched_command is None:
            raise WorkspaceError(f"Unknown command '{command_id}'.")
        if goal_template:
            raise WorkspaceError("Use either command or goal_template, not both.")
        local_handler = matched_command.local_handler
        if local_handler:
            goal_template = None
        else:
            if requested not in {"auto", "act"}:
                raise WorkspaceError("command requires act or auto intent.")
            goal_template = matched_command.goal_template
            requested = "act"
    if goal_template and (requested != "act" or goal_template not in routing.GOAL_TEMPLATES):
        raise WorkspaceError("goal_template requires act intent and a registered template.")
    raw_outcomes = payload.get("requested_outcomes") or []
    if not isinstance(raw_outcomes, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_outcomes
    ):
        raise WorkspaceError("requested_outcomes must be a list of outcome ids.")
    requested_outcomes = [value.strip() for value in raw_outcomes]
    if requested_outcomes:
        if requested != "act":
            raise WorkspaceError("requested_outcomes requires act intent.")
        if goal_template:
            raise WorkspaceError("Use either requested_outcomes or a goal_template, not both.")
        # A structured suggestion must name registered outcomes from one
        # workflow before a durable command is created.
        routing.validate_requested_outcomes(requested_outcomes)

    path = _chat_path(workspace, chat_id)
    process_lock = _processing_lock(path)
    with process_lock:
        key = str(path.resolve())
        _processing_paths.add(key)
        try:
            with _lock(path):
                record = _read(workspace, chat_id, recover=False)
                # Idempotency: replay the newest attempt for this request id,
                # but let an explicitly failed attempt be retried.
                existing = next(
                    (item for item in reversed(record["messages"]) if item.get("request_id") == request_id),
                    None,
                )
                if existing and existing.get("state") != "failed":
                    return {"outcome": existing.get("outcome"), "chat": get_chat(workspace, chat_id)}
                doc_ids = list(record.get("composer_context", {}).get("document_ids") or [])
                snapshot = _document_snapshot(workspace, doc_ids)
                user_id = _new_id("msg")
                user = {
                    "id": user_id, "ordinal": record["next_ordinal"], "role": "user", "kind": "text",
                    "content": content, "created_at": utcnow(), "request_id": request_id, "state": "pending",
                    "requested_intent": requested, "resolved_intent": None, "reply_to_id": None,
                    "document_context": snapshot, "artifact_ids": [], "outcome": None, "error": None,
                    "source": source,
                }
                record["next_ordinal"] += 1
                record["messages"].append(user)
                if record.get("title_source") != "user" and not any(item.get("role") == "user" for item in record["messages"][:-1]):
                    record["title"] = _clean_title(content)
                record["updated_at"] = utcnow()
                write_json_atomic(path, record)

            run_kind = str(payload.get("run_kind") or "").strip() or None
            run_context = payload.get("run_context") or {}
            if not isinstance(run_context, dict):
                raise WorkspaceError("Run context must be an object.")
            if run_kind and (source != "folder_intake" or requested != "act" or run_kind != "intake" or not isinstance(run_context, dict)):
                raise WorkspaceError("Specialized run context is only accepted for folder intake actions.")
            if run_context and not run_kind:
                # Run context is scope, never a routing override: a template
                # accepts only the scope keys routing declares for it.
                allowed = routing.TEMPLATE_RUN_CONTEXT_KEYS.get(goal_template or "")
                if requested != "act" or allowed is None or not set(run_context) <= allowed:
                    raise WorkspaceError(
                        "Command run context is only accepted for a registered "
                        "goal template's declared scope keys."
                    )
            outcome = _process_message(
                workspace, chat_id, user, record, mode, goal_template, run_kind,
                run_context, requested_outcomes, local_handler,
            )
            return {"outcome": outcome, "chat": get_chat(workspace, chat_id)}
        except Exception as error:
            try:
                outcome = {"kind": "error", "message": str(error)}
                def fail(record, user):
                    user["state"] = "failed"
                    user["error"] = str(error)
                    user["outcome"] = outcome
                    _append_assistant(record, kind="error", content=str(error), resolved=user.get("resolved_intent") or "clarify", reply_to=user["id"], error=str(error), outcome=outcome)
                if 'user_id' in locals():
                    _finalize(workspace, chat_id, user_id, fail)
                    return {"outcome": outcome, "chat": get_chat(workspace, chat_id)}
            except Exception:
                pass
            raise
        finally:
            _processing_paths.discard(key)


class CommandLaunchError(WorkspaceError):
    """A command could not be started. The message is safe to show a user."""

    def __init__(self, message: str, *, run_id: str | None = None):
        super().__init__(message)
        self.run_id = run_id


def _launch_command(
    workspace: Workspace, chat_id: str, user: dict, record: dict, mode: str,
    *, goal_template: str | None = None, run_context: dict | None = None,
    requested_outcomes: list[str] | None = None, text: str | None = None,
) -> dict:
    """Start or queue one command run, and report where it landed.

    Shared by the deterministic entry points (slash commands, tab buttons,
    exact phrases) and by the assistant tool loop's ``start_command`` /
    ``start_action`` tools, so run policy — the agent-model requirement, the
    single-live-run rule, queueing behind an active run — is written once and
    cannot drift between them. Refusals are raised as
    :class:`CommandLaunchError`: a deterministic caller turns one into a
    clarification message, while the tool loop hands it back to the model as a
    tool error so it can explain or choose differently.
    """

    request = str(text if text is not None else user["content"]).strip()
    # Action routing never bypasses the command runner.
    if not llm.agent_status().get("configured"):
        raise llm.LLMError("The action agent model is not configured.")
    active = _active_run(workspace)
    # The only non-command run is folder intake, which owns the workspace
    # until its staged batch is applied.
    if active and not store.is_command_run(active):
        raise CommandLaunchError(
            "A folder import is running. Wait for it to finish, or pause or "
            "cancel it before starting another action.",
            run_id=active["id"],
        )

    refs = []
    if not goal_template and re.search(r"\b(that|it|this)\b", request, re.I):
        preceding = next((item for item in reversed(record.get("messages") or []) if item.get("role") == "assistant"), None)
        ids = list((preceding or {}).get("artifact_ids") or [])
        if len(ids) != 1:
            raise CommandLaunchError(
                "Which result do you mean? Name the artifact or use its action button."
            )
        artifact = _artifact(workspace, chat_id, ids[0])
        refs = [{"kind": "assistant_artifact", "id": artifact["id"], "title": artifact.get("title")}]

    planning_context = dict(run_context or {})
    target_refs: list[str] = []
    if goal_template == "finding_draft":
        observation_id = str(planning_context.get("observation_id") or "").strip()
        rcm_id = str(planning_context.get("rcm_id") or "").strip()
        if observation_id:
            target_refs = [f"observation:{observation_id}"]
        elif rcm_id:
            target_refs = [f"rcm:{rcm_id}"]
    command = {
        "source": "goal_template" if goal_template else ("tab_button" if user.get("source") != "composer" else "chat"),
        "text": request, "goal_template": goal_template,
        "chat_id": chat_id, "source_message_id": user["id"], "context_refs": refs,
        "target_refs": target_refs,
        "requested_outcomes": list(requested_outcomes or []),
    }
    if goal_template == "planning" and not planning_context.get("document_ids"):
        planning_context["document_ids"] = list(
            (user.get("document_context") or {}).get("document_ids") or []
        )
    if active:
        response = runner.steer(
            workspace, active["id"], request, chat_id=chat_id,
            source_message_id=user["id"], context_refs=refs, target_refs=target_refs,
            run_context=planning_context, goal_template=goal_template,
            requested_outcomes=requested_outcomes,
        )
        queued = response.get("command") or next((item for item in reversed((store.load_run(workspace, active["id"]).get("pending_commands") or [])) if item.get("source_message_id") == user["id"]), None)
        pending_count = len(store.load_run(workspace, active["id"]).get("pending_commands") or [])
        return {"kind": "command_queued", "run_id": active["id"], "command_id": (queued or {}).get("id"), "position": max(1, pending_count)}

    parent_id = None
    for item in reversed(record.get("messages") or []):
        candidate = (item.get("outcome") or {}).get("run_id")
        if candidate:
            if not RUN_ID_RE.fullmatch(str(candidate)):
                break
            try:
                linked = store.load_run(workspace, candidate)
                if linked.get("chat_id") == chat_id and linked.get("status") in store.TERMINAL_STATUSES:
                    parent_id = candidate
            except WorkspaceError:
                pass
            break
    try:
        if planning_context:
            run = runner.start_command_run(
                workspace, mode, command, parent_run_id=parent_id,
                context=planning_context,
            )
        else:
            run = runner.start_command_run(
                workspace, mode, command, parent_run_id=parent_id,
            )
        return {"kind": "run_started", "run_id": run["id"], "command_id": run["command"]["id"]}
    except runner.AgentBusyError:
        raced = _active_run(workspace)
        if not raced or not store.is_command_run(raced):
            raise
        response = runner.steer(
            workspace, raced["id"], request, chat_id=chat_id,
            source_message_id=user["id"], context_refs=refs, target_refs=target_refs,
            run_context=planning_context, goal_template=goal_template,
            requested_outcomes=requested_outcomes,
        )
        queued = response.get("command") or {}
        return {"kind": "command_queued", "run_id": raced["id"], "command_id": queued.get("id"), "position": 1}


def _commander(
    workspace: Workspace, chat_id: str, user: dict, record: dict, mode: str,
) -> assistant.Commander:
    """Bind this message's launch policy for the assistant tool loop.

    The assistant module never imports the agent runner; the chat layer owns
    run policy and hands the loop a pre-bound launcher, so ``assistant.ask``
    stays read-only for every caller that does not pass one.
    """

    def launch_command(command_id: str) -> dict:
        return _launch_command(
            workspace, chat_id, user, record, mode,
            goal_template=commands.COMMANDS[command_id].goal_template,
        )

    def launch_action(request: str) -> dict:
        return _launch_command(
            workspace, chat_id, user, record, mode, text=request,
        )

    return assistant.Commander(
        catalog=tuple(
            {"id": item.id, "label": item.label, "description": item.description}
            for item in commands.COMMANDS.values()
            if item.goal_template
        ),
        launch_command=launch_command,
        launch_action=launch_action,
    )


def _process_message(
    workspace: Workspace, chat_id: str, user: dict, record: dict, mode: str,
    goal_template: str | None, run_kind: str | None = None, run_context: dict | None = None,
    requested_outcomes: list[str] | None = None, local_handler: str | None = None,
) -> dict:
    slash_command = None
    if commands.is_slash(user["content"]):
        slash_command = commands.match_slash(user["content"])
        if slash_command and slash_command.local_handler:
            local_handler = slash_command.local_handler

    # Local slash commands have precedence over active-run interactions: a
    # status check should report the engagement rather than answer a pending
    # agent question or spend a model turn.
    if local_handler == "status":
        return _process_status_message(workspace, chat_id, user)

    active = _active_run(workspace)
    # Free-text interactions have precedence over all routing.
    if active and active.get("interview", {}).get("pending_question"):
        result = runner.steer(workspace, active["id"], user["content"])
        outcome = {"kind": "interaction_answered", "run_id": active["id"]}
        def done(rec, stored):
            stored.update(state="complete", resolved_intent="interaction_response", outcome=outcome)
        _finalize(workspace, chat_id, user["id"], done)
        return outcome
    if active and store.is_command_run(active):
        interaction = next((item for item in active.get("interactions") or [] if item.get("status") == "pending" and item.get("type") == "clarification"), None)
        if interaction:
            runner.resolve_interaction(workspace, active["id"], interaction["id"], {"text": user["content"]})
            outcome = {"kind": "interaction_answered", "run_id": active["id"], "interaction_id": interaction["id"]}
            def done(rec, stored): stored.update(state="complete", resolved_intent="interaction_response", outcome=outcome)
            _finalize(workspace, chat_id, user["id"], done)
            return outcome

    resolved = requested = user["requested_intent"]
    if requested == "auto" and commands.is_slash(user["content"]):
        # An explicit slash command is resolved outright, the same as a caller
        # passing `command` directly — it never reaches the model.
        matched_command = slash_command or commands.match_slash(user["content"])
        if matched_command is None:
            text = f"Unknown command. Available: {commands.help_text()}"
            outcome = {"kind": "clarification_requested"}
            def done(rec, stored):
                stored.update(state="complete", resolved_intent="clarify", outcome=outcome)
                _append_assistant(rec, kind="clarification", content=text, resolved="clarify", reply_to=stored["id"], outcome=outcome)
            _finalize(workspace, chat_id, user["id"], done)
            return outcome
        resolved = "act"
        goal_template = matched_command.goal_template
    elif requested == "auto":
        # A message that *is* a registered command phrase resolves without a
        # model turn. Anything else is left to the coordinator, which decides
        # by tool-calling whether to answer or to start work.
        phrase_match = commands.match_phrase(user["content"])
        resolved = "act" if phrase_match else "coordinate"
        if phrase_match:
            goal_template = phrase_match.goal_template

    if resolved in {"ask", "coordinate"}:
        if not llm.status().get("configured"):
            raise llm.LLMError("The read-only assistant model is not configured.")
        doc_ids = list(record.get("composer_context", {}).get("document_ids") or [])
        current = _document_snapshot(workspace, doc_ids)
        history = _eligible_history(record, current)
        # An explicit Ask is a promise the workspace will not change, so the
        # coordinator is lent no mutating capability for it.
        commander = (
            _commander(workspace, chat_id, user, record, mode)
            if resolved == "coordinate" else None
        )
        with debug_store.trace_context(
            workspace_id=workspace.id, workspace_root=str(workspace.root), chat_id=chat_id,
            stage="assistant.answer", purpose="assistant_answer",
        ):
            result = assistant.ask(
                workspace, user["content"], doc_ids,
                prior_turns=history,
                chat_id=chat_id,
                commander=commander,
            )
        final_snapshot = _document_snapshot(
            workspace, doc_ids, manifest=(result.get("document_context") or {}).get("manifest") or []
        )
        answer_id = _new_id("msg")
        artifact_ids = _write_artifacts(workspace, chat_id, answer_id, result.get("artifacts") or [])
        started = result.get("started_run") or None
        answer_outcome = {"kind": "answer", "message_id": answer_id}
        # The run card hangs off whichever message carries a run id. Only the
        # auditor's message may carry it: repeating it on the reply would append
        # a second, identical card for the same run.
        outcome = started or answer_outcome
        final_intent = "act" if started else "ask"
        def done(rec, stored):
            stored.update(state="complete", resolved_intent=final_intent, outcome=outcome, document_context=final_snapshot)
            answer = _append_assistant(
                rec, kind="text", content=str(result.get("answer") or ""), resolved=final_intent,
                reply_to=stored["id"], document_context=final_snapshot, artifact_ids=artifact_ids,
                outcome=answer_outcome, citation_ids=[str(item.get("id") or _new_id("citation")) for item in result.get("citations") or []],
                citations=result.get("citations") or [], tool_trace=_safe_trace(result.get("steps") or []),
                document_manifest=result.get("document_context"),
            )
            answer["id"] = answer_id
        _finalize(workspace, chat_id, user["id"], done)
        return outcome

    if run_kind == "intake":
        if active:
            text = "Another workspace run is active. Finish or cancel it before starting folder intake."
            outcome = {"kind": "clarification_requested", "run_id": active["id"]}
            def done(rec, stored):
                stored.update(state="complete", resolved_intent="clarify", outcome=outcome)
                _append_assistant(rec, kind="clarification", content=text, resolved="clarify", reply_to=stored["id"], outcome=outcome)
            _finalize(workspace, chat_id, user["id"], done)
            return outcome
        run = runner.start_run(
            workspace, mode, run_context or {}, kind="intake",
            chat_id=chat_id, source_message_id=user["id"],
        )
        outcome = {"kind": "run_started", "run_id": run["id"], "command_id": None}
        def done(rec, stored): stored.update(state="complete", resolved_intent="act", outcome=outcome)
        _finalize(workspace, chat_id, user["id"], done)
        return outcome

    try:
        outcome = _launch_command(
            workspace, chat_id, user, record, mode, goal_template=goal_template,
            run_context=run_context, requested_outcomes=requested_outcomes,
        )
    except CommandLaunchError as error:
        text = str(error)
        outcome = {"kind": "clarification_requested"}
        if error.run_id:
            outcome["run_id"] = error.run_id
        def done(rec, stored):
            stored.update(state="complete", resolved_intent="clarify", outcome=outcome)
            _append_assistant(rec, kind="clarification", content=text, resolved="clarify", reply_to=stored["id"], outcome=outcome)
        _finalize(workspace, chat_id, user["id"], done)
        return outcome
    def done(rec, stored): stored.update(state="complete", resolved_intent="act", outcome=outcome)
    _finalize(workspace, chat_id, user["id"], done)
    return outcome


def _status_label(value: str) -> str:
    return str(value or "unknown").replace("_", " ").capitalize()


def _audit_status_text(workspace: Workspace) -> str:
    """Render the deterministic engagement projection as a concise chat reply."""
    phases = dashboard.engagement_status_payload(workspace).get("phases") or []
    states = {str(phase.get("state") or "") for phase in phases}
    if "attention" in states:
        overall = "Needs attention"
    elif phases and all(bool(phase.get("complete")) for phase in phases):
        overall = "Complete"
    elif "in_progress" in states or "complete" in states:
        overall = "In progress"
    else:
        overall = "Not started"

    lines = ["## Audit status", "", f"**Overall:** {overall}", ""]
    for phase in phases:
        line = (
            f"- **{phase.get('label') or phase.get('id')}:** "
            f"{_status_label(phase.get('state'))}"
        )
        summary = str(phase.get("summary") or "").strip()
        if summary:
            line += f" — {summary}"
        lines.append(line)
        for issue in (phase.get("issues") or [])[:2]:
            lines.append(f"  - Attention: {issue}")

    active = _active_run(workspace)
    if active:
        lines.extend(["", f"**Current run:** {_current_activity(active)}."])
    return "\n".join(lines)


def _process_status_message(workspace: Workspace, chat_id: str, user: dict) -> dict:
    content = _audit_status_text(workspace)
    outcome = {"kind": "status"}

    def done(rec, stored):
        answer = _append_assistant(
            rec, kind="text", content=content, resolved="ask",
            reply_to=stored["id"], outcome=outcome,
        )
        outcome["message_id"] = answer["id"]
        stored.update(state="complete", resolved_intent="ask", outcome=outcome)

    _finalize(workspace, chat_id, user["id"], done)
    return outcome


def _current_activity(run: dict) -> str:
    status = str(run.get("status") or "")
    if status in {*store.TERMINAL_STATUSES, *store.RESUMABLE_STATUSES}:
        return status.replace("_", " ")
    activity = run.get("activity") or {}
    label = str(activity.get("label") or "").strip()
    if label:
        current, total = activity.get("current"), activity.get("total")
        if isinstance(current, int) and isinstance(total, int) and total > 0:
            label = f"{label} ({current}/{total})"
        detail = str(activity.get("detail") or "").strip()
        return f"{label} — {detail}" if detail else label
    for action in run.get("actions") or []:
        if action.get("status") == "running":
            return str(action.get("type") or "executing action").replace("_", " ")
    for stage in (run.get("plan") or {}).get("stages") or []:
        for task in stage.get("tasks") or []:
            if task.get("status") in {"running", "awaiting_approval"}:
                return str(task.get("detail") or task.get("title") or "executing")
    return str(run.get("status") or "").replace("_", " ")


def _summary_line(run: dict) -> str:
    """One line of plain language about where the run got to.

    The domain summary markdown is an audit work product whose first line is a
    heading ("# Document analysis"), which says nothing on a card. The agent's
    own closing turn is the sentence a reader wants, so it is preferred and the
    markdown heading is never used as a summary.
    """
    for message in reversed(run.get("messages") or []):
        if message.get("role") == "agent":
            first = str(message.get("content") or "").strip().splitlines()
            if first and first[0].strip():
                return first[0].strip()
    for entry in reversed(run.get("narration") or []):
        if str(entry.get("text") or "").strip():
            return str(entry["text"]).strip()
    if run.get("error"):
        return str(run["error"])
    if run.get("warnings"):
        return str(run["warnings"][-1])
    # Runs recorded before narration existed still deserve a sentence; the
    # closing text is a pure projection, so it can be derived on read.
    if str(run.get("status") or "") in store.TERMINAL_STATUSES:
        derived = narration.closing_text(run).splitlines()
        if derived:
            return derived[0]
    return ""


# Terminal statuses are the run's verdict; a raw identifier ("completed with
# open items") makes the reader translate it. These do not replace `status`,
# which stays the machine value every client already keys on.
_STATUS_LABELS = {
    "queued": "Queued",
    "interpreting": "Working out the plan",
    "executing": "Working",
    "verifying": "Checking the result",
    "awaiting_approval": "Waiting for your approval",
    "awaiting_input": "Waiting for your answer",
    "paused": "Paused",
    "interrupted": "Interrupted",
    "completed": "Done",
    "completed_with_open_items": "Done, with things for you",
    "completed_with_issues": "Done, with issues",
    "completed_with_failures": "Done, with parts unfinished",
    "failed": "Couldn't finish",
    "cancelled": "Stopped",
}


def _run_card_anchor(run: dict, fallback_created_at: object) -> str:
    """Where a run's card sorts: after everything it has said or asked, so far.

    The card is a receipt of where the run currently stands, not a bulletin
    posted at launch. A run that keeps talking — a context note, a repair, its
    eventual close — must not leave that receipt stranded next to the message
    that started it while the conversation visibly continues below. Paired
    with ``RUN_CARD_ORDINAL``, which is what actually wins the tie once the
    anchor lands on the same timestamp as the turn it chased.
    """
    latest = str(fallback_created_at or "")
    for message in run.get("messages") or []:
        candidate = str(message.get("at") or "")
        if candidate > latest:
            latest = candidate
    for milestone in run.get("milestones") or []:
        candidate = str(milestone.get("created_at") or "")
        if candidate > latest:
            latest = candidate
    for interaction in run.get("interactions") or []:
        candidate = str(interaction.get("created_at") or "")
        if candidate > latest:
            latest = candidate
    for approval in run.get("approvals") or []:
        candidate = str(approval.get("created") or "")
        if candidate > latest:
            latest = candidate
    return latest


def _run_start_anchor(run: dict, fallback_created_at: object) -> str:
    """Where a run's opening line sorts: when the run began, not where its
    receipt lands. ``plan_line`` reads as the agent speaking before it did
    anything, so it must not share ``_run_card_anchor``'s late timestamp —
    doing so once dragged the opening line down past every milestone and
    narration line the run went on to produce.
    """
    return str(run.get("created") or fallback_created_at or "")


def _plan_line_entry(run_id: str, plan_line: str, run: dict, fallback_created_at: object) -> dict:
    return {
        "id": f"run:{run_id}:plan_line", "type": "message", "derived": True,
        "role": "assistant", "kind": "text", "content": plan_line,
        "created_at": _run_start_anchor(run, fallback_created_at), "state": "complete",
        "requested_intent": None, "resolved_intent": None,
        "reply_to_id": None, "artifact_ids": [], "outcome": None, "error": None,
        "run_id": run_id,
    }


def _run_projection(run: dict) -> dict:
    summary = store.run_summary(run)
    open_items = narration.blockers(run)
    status = str(run.get("status") or "")
    summary.update({
        "type": "run", "derived": True, "run_id": run["id"],
        "id": f"run:{run['id']}", "created_at": run.get("created"),
        "title": (run.get("command") or {}).get("text") or (run.get("goal") or {}).get("objective") or run.get("kind", "Agent run"),
        "current_activity": _current_activity(run),
        # A unit that stopped needing a person is attention even when it never
        # became a typed interaction — that gap is why blocked work used to end
        # a run silently.
        "pending_attention": bool(
            any(item.get("status") == "pending" for item in run.get("approvals") or [])
            or any(item.get("status") == "pending" for item in run.get("interactions") or [])
            or any(item["severity"] != "review" for item in open_items)
        ),
        "status_label": _STATUS_LABELS.get(status, status.replace("_", " ")),
        "plan_line": (run.get("workflow") or {}).get("workflow_explanation") or "",
        # The tail is what a live card shows; the whole log stays on the record.
        "narration": list(run.get("narration") or [])[-12:],
        "blockers": open_items,
        "summary_line": _summary_line(run),
    })
    return summary


def get_chat(workspace: Workspace, chat_id: str) -> dict:
    record = _read(workspace, chat_id)
    # Missing documents remain in immutable message snapshots but are removed
    # from the active composer context and explicitly reported.
    known = {str(item.get("id")) for item in workspace.documents}
    attached = list(record.get("composer_context", {}).get("document_ids") or [])
    active_missing = [value for value in attached if value not in known]
    referenced_ids = {
        value for item in record.get("messages") or []
        for value in (item.get("document_context") or {}).get("document_ids") or []
    }
    missing = sorted({*active_missing, *(value for value in referenced_ids if value not in known)})
    if active_missing:
        record["composer_context"]["document_ids"] = [value for value in attached if value in known]
        record["updated_at"] = utcnow()
        write_json_atomic(_chat_path(workspace, chat_id), record)

    artifact_ids = {value for item in record.get("messages") or [] for value in item.get("artifact_ids") or []}
    artifacts = {}
    artifact_errors = []
    for artifact_id in artifact_ids:
        try:
            artifacts[artifact_id] = _artifact(workspace, chat_id, artifact_id)
        except WorkspaceError as error:
            artifact_errors.append({"id": artifact_id, "error": str(error)})

    run_summaries = store.list_runs(workspace)
    linked = []
    linked_runs: dict[str, dict] = {}
    for summary in run_summaries:
        if summary.get("chat_id") == chat_id:
            try:
                run = store.load_run(workspace, summary["id"])
            except WorkspaceError:
                continue
            linked_runs[run["id"]] = run
            linked.append(_run_projection(run))
    by_run = {item["run_id"]: item for item in linked}
    # A command queued behind a busy run is relaunched as its own child run when
    # the parent finishes, and that child carries the queueing message's id. It
    # is how a queued command is told apart from one that was never drained.
    by_source_message = {}
    for projection in linked:
        source = str(projection.get("source_message_id") or "")
        if source:
            by_source_message.setdefault(source, projection)
    # Tracks which runs already got their opening line placed, so a run
    # touched by more than one placement path below doesn't repeat it.
    plan_line_seen: set[str] = set()
    transcript = [dict(item, type="message", derived=False) for item in record.get("messages") or []]
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}
    for item in transcript:
        for citation in item.get("citations") or []:
            document = documents_by_id.get(str(citation.get("source_id") or ""))
            citation["available"] = bool(
                document and (
                    not citation.get("source_sha1")
                    or citation.get("source_sha1") == document.get("sha1")
                )
            )
    for item in record.get("messages") or []:
        outcome = item.get("outcome") or {}
        run_id = outcome.get("run_id")
        if run_id and not RUN_ID_RE.fullmatch(str(run_id)):
            continue
        # A queued command is projected as the command, always — never as the
        # run it is waiting on. Testing `run_id in by_run` first appended a
        # second, identical copy of that run's card (sharing its id) whenever
        # the run belonged to this same chat, which is the ordinary case for a
        # follow-up message steering a live run.
        launched = by_source_message.get(str(item.get("id") or ""))
        if run_id and outcome.get("kind") == "command_queued" and launched is not None:
            # The queue drained: this command became its own run. Show that run
            # in the message's place rather than a card still saying "queued".
            transcript.append(dict(
                launched,
                created_at=_run_card_anchor(linked_runs.get(run_id) or {}, item.get("created_at")),
                ordinal=RUN_CARD_ORDINAL,
                source_message_id=item["id"],
            ))
            if launched.get("plan_line") and run_id not in plan_line_seen:
                plan_line_seen.add(run_id)
                transcript.append(_plan_line_entry(
                    run_id, launched["plan_line"], linked_runs.get(run_id) or {}, item.get("created_at"),
                ))
        elif run_id and outcome.get("kind") == "command_queued":
            try:
                parent_run = linked_runs.get(run_id) or store.load_run(workspace, run_id)
                parent = _run_projection(parent_run)
                position = (item.get("outcome") or {}).get("position") or 1
                # A command only leaves the queue when its parent finishes
                # cleanly. A parent that died, or whose chat was deleted, will
                # never drain it, and reporting that as "queued" waits forever.
                stranded = (
                    parent_run.get("status") in store.TERMINAL_STATUSES
                    or _orphaned_run(workspace, parent)
                )
                # The card sits directly beneath the message that produced it,
                # so titling it with that same text printed the request twice.
                # It identifies the run being waited on instead.
                parent.update({
                    "id": f"run:{run_id}:command:{(item.get('outcome') or {}).get('command_id')}",
                    "status": "cancelled" if stranded else "queued",
                    "status_label": "Didn't run" if stranded else "Queued",
                    "current_activity": (
                        "the run it was waiting for ended first — send it again"
                        if stranded
                        else f"waiting for “{parent['title']}” to finish"
                        if position <= 1
                        else f"number {position} in the queue behind “{parent['title']}”"
                    ),
                    "title": "Queued for the agent",
                    "plan_line": "", "narration": [], "blockers": [],
                    "summary_line": "", "pending_attention": False,
                    # This card is the command, not the run it is waiting on:
                    # inheriting the parent's counters and clock would report
                    # the parent's progress as though it were the command's.
                    "task_counts": {"total": 0, "completed": 0, "failed": 0, "blocked": 0},
                    "started": None, "finished": None, "duration_ms": None,
                    "activity": None, "next_outcomes": [], "error": None,
                    "created_at": item.get("created_at"),
                    "ordinal": float(item.get("ordinal") or 0) + 0.5,
                    "source_message_id": item["id"],
                })
                transcript.append(parent)
            except WorkspaceError:
                pass
        elif run_id and run_id in by_run:
            anchor = _run_card_anchor(linked_runs.get(run_id) or {}, item.get("created_at"))
            card = dict(by_run[run_id], created_at=anchor, ordinal=RUN_CARD_ORDINAL, source_message_id=item["id"])
            transcript.append(card)
            if card.get("plan_line") and run_id not in plan_line_seen:
                plan_line_seen.add(run_id)
                transcript.append(_plan_line_entry(
                    run_id, card["plan_line"], linked_runs.get(run_id) or {}, item.get("created_at"),
                ))
    # Child runs can appear after a queued parent ends; include unplaced links.
    # A queued-command card carries its parent's ``run_id`` but is not that
    # run's card, so it must not satisfy the placement check.
    placed = {
        item.get("run_id") for item in transcript
        if item.get("type") == "run" and ":command:" not in str(item.get("id") or "")
    }
    for item in linked:
        run_id = item["run_id"]
        if run_id in placed:
            continue
        transcript.append(dict(
            item,
            created_at=_run_card_anchor(linked_runs.get(run_id) or {}, item.get("created_at")),
            ordinal=RUN_CARD_ORDINAL,
        ))
        if item.get("plan_line") and run_id not in plan_line_seen:
            plan_line_seen.add(run_id)
            transcript.append(_plan_line_entry(
                run_id, item["plan_line"], linked_runs.get(run_id) or {}, item.get("created_at"),
            ))
    # Run-owned conversational messages and typed attention items are response
    # projections.  They retain stable derived IDs and are never written back
    # through chat APIs.
    # Steered chat messages are echoed into run message logs without a source
    # id, so they are hidden by content — but only chat messages that actually
    # fed a run are compared, so unrelated identical text stays visible.
    steered_user_text = {
        str(item.get("content") or "") for item in record.get("messages") or []
        if item.get("role") == "user" and (
            (item.get("outcome") or {}).get("run_id")
            or item.get("resolved_intent") == "interaction_response"
        )
    }
    for projection in linked:
        run = linked_runs.get(projection["run_id"])
        if run is None:
            continue
        for index, message in enumerate(run.get("messages") or []):
            if message.get("role") == "user" and str(message.get("content") or "") in steered_user_text:
                continue
            transcript.append({
                "id": f"run:{run['id']}:message:{index}", "type": "message", "derived": True,
                "role": "assistant" if message.get("role") == "agent" else "user",
                "kind": "text", "content": str(message.get("content") or ""),
                "created_at": message.get("at") or run.get("created"), "state": "complete",
                "requested_intent": None, "resolved_intent": "interaction_response",
                "reply_to_id": None, "artifact_ids": [], "outcome": None, "error": None,
                "run_id": run["id"],
            })
        for item in run.get("milestones") or []:
            transcript.append({
                "id": f"run:{run['id']}:milestone:{item.get('id')}",
                "type": "milestone",
                "derived": True,
                "run_id": run["id"],
                "created_at": item.get("created_at") or run.get("created"),
                "ordinal": MILESTONE_ORDINAL,
                "milestone": dict(item),
            })
        for interaction in run.get("interactions") or []:
            transcript.append({
                "id": f"run:{run['id']}:interaction:{interaction['id']}", "type": "interaction",
                "derived": True, "run_id": run["id"], "created_at": interaction.get("created_at") or run.get("created"),
                "interaction": interaction,
            })
        for approval in run.get("approvals") or []:
            transcript.append({
                "id": f"run:{run['id']}:approval:{approval['id']}", "type": "approval",
                "derived": True, "run_id": run["id"], "created_at": approval.get("created") or run.get("created"),
                "approval": approval,
            })
    # A run's card sorts by RUN_CARD_ORDINAL at its chased timestamp (see
    # _run_card_anchor); a still-queued placeholder keeps the fractional
    # anchor (source ordinal + 0.5) it needs to sit right under the message
    # that queued it; derived items without an ordinal sort after every chat
    # message sharing their timestamp — all of this floating-point precision
    # would be destroyed by an int() truncation.
    transcript.sort(key=lambda item: (
        str(item.get("created_at") or ""),
        float(item.get("ordinal") if item.get("ordinal") is not None else DERIVED_ORDINAL),
        str(item.get("id") or ""),
    ))

    workflow_state = None
    try:
        from .agent import capabilities as audit_capabilities

        workflow_state = audit_capabilities.workflow_state(workspace)
    except Exception:
        # Recommendation UI must not make an otherwise durable chat unreadable.
        workflow_state = None
    if workflow_state is not None:
        try:
            workflow_state = {
                **workflow_state,
                **audit_capabilities.doc_tests_workflow_state(workspace),
            }
        except Exception:
            # Keep audit recommendations available if a standalone worklist
            # cannot be inspected.
            pass

    result = dict(record)
    result.update({
        "transcript": transcript, "artifacts": artifacts, "artifact_errors": artifact_errors,
        "runs": linked, "missing_document_ids": missing, "capabilities": capabilities(),
        "suggestions": narration.next_steps(workspace, workflow_state),
        "guided_workflows": narration.guided_workflows(workflow_state),
    })
    active_statuses = set(store.ACTIVE_STATUSES) | set(store.RESUMABLE_STATUSES)
    active_summary = next(
        (
            item for item in run_summaries
            if item.get("status") in active_statuses and not _orphaned_run(workspace, item)
        ),
        None,
    )
    active = None
    if active_summary is not None:
        active = linked_runs.get(active_summary["id"])
        if active is None:
            try:
                active = store.load_run(workspace, active_summary["id"])
            except WorkspaceError:
                active = None
    result["active_workspace_run"] = _run_projection(active) if active else None
    return result
