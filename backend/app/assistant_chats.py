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

from . import assistant, llm
from .agent import command_runner, runner, store
from .workspaces import Workspace, WorkspaceError, write_json_atomic

CHATS_DIRNAME = "AssistantChats"
CHAT_SCHEMA_VERSION = 1
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            pending = [item for item in record["messages"] if item.get("state") == "pending"]
            if pending:
                for item in pending:
                    item["state"] = "failed"
                    item["error"] = "Interrupted before completion. Retry this request to continue."
                record["updated_at"] = utcnow()
                write_json_atomic(path, record)
        return record


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
    shutil.rmtree(target)


def capabilities() -> dict:
    assistant_status = llm.status()
    agent_status = llm.agent_status()
    return {
        "ask": bool(assistant_status.get("configured")),
        "act": bool(agent_status.get("configured")),
        "assistant": assistant_status,
        "agent": agent_status,
    }


def _document_snapshot(workspace: Workspace, document_ids: list[str], *, manifest=None) -> dict:
    by_id = {str(item.get("id")): item for item in workspace.documents}
    return {
        "document_ids": list(document_ids),
        "sources": [
            {"document_id": value, "sha1": by_id[value].get("sha1"), "version": by_id[value].get("version")}
            for value in document_ids if value in by_id
        ],
        "document_ai_enabled": bool(workspace.settings.get("doc_llm_optin")),
        "mask_pii": bool(workspace.settings.get("doc_pii_masking")),
        "manifest": list(manifest or []),
    }


def _context_matches(current: dict, prior: dict | None) -> bool:
    if not prior:
        return not current["document_ids"]
    if current["document_ids"] != list(prior.get("document_ids") or []):
        return False
    if current["mask_pii"] != bool(prior.get("mask_pii")):
        return False
    if current["document_ids"] and (
        not current["document_ai_enabled"] or not prior.get("document_ai_enabled")
    ):
        return False
    return current["sources"] == list(prior.get("sources") or [])


def _eligible_history(record: dict, current: dict) -> list[dict]:
    messages = [item for item in record.get("messages") or [] if item.get("state") == "complete"]
    selected: list[tuple[dict, dict]] = []
    index = len(messages) - 1
    while index >= 1 and len(selected) < 4:
        answer, user = messages[index], messages[index - 1]
        if not (
            user.get("role") == "user" and answer.get("role") == "assistant"
            and user.get("resolved_intent") == answer.get("resolved_intent") == "ask"
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


def _active_run(workspace: Workspace) -> dict | None:
    active = set(store.ACTIVE_STATUSES) | set(store.RESUMABLE_STATUSES)
    summaries = [item for item in store.list_runs(workspace) if item.get("status") in active]
    if not summaries:
        return None
    return store.load_run(workspace, summaries[0]["id"])


ASK_WORDS = {"explain", "summarize", "compare", "inspect", "count", "calculate", "show", "what", "why", "how", "which", "who", "when"}
ACT_WORDS = {"create", "edit", "update", "delete", "remove", "save", "pin", "rerun", "execute", "import", "generate", "prepare", "preserve", "reconcile", "approve", "run"}


def _deterministic_intent(content: str) -> str | None:
    words = re.findall(r"[a-z]+", content.casefold())
    if not words:
        return None
    # Interrogative shape wins over action words used as nouns (for example,
    # "What did the last run do?").  Only obvious imperatives take the local
    # action fast path; everything else is classified or clarified.
    if words[0] in ASK_WORDS or content.rstrip().endswith("?"):
        return "ask"
    if words[0] in ACT_WORDS or (words[0] == "please" and len(words) > 1 and words[1] in ACT_WORDS):
        return "act"
    return None


def _classifier(record: dict, content: str, active: bool) -> dict:
    if not llm.status().get("configured"):
        return {"intent": "clarify", "confidence": "low", "clarification": "Choose Ask or Act so I can route this safely."}
    prior = []
    for item in reversed(record.get("messages") or []):
        if item.get("state") == "complete" and item.get("kind") == "text" and item.get("role") in {"user", "assistant"}:
            prior.append({"role": item["role"], "content": str(item.get("content") or "")[:2000]})
            if len(prior) == 2:
                break
    prompt = {
        "text": content, "prior_text_turns": list(reversed(prior)),
        "active_schema_v2_run": active,
    }
    message = llm.chat([
        {"role": "system", "content": "Classify the request. Return JSON only: {\"intent\":\"ask|act|clarify\",\"confidence\":\"high|medium|low\",\"clarification\":null}. Actions change or persist workspace state. Questions are read-only. Ambiguous pronouns require clarify."},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], profile="assistant")
    try:
        value = json.loads(str(message.get("content") or ""))
    except (json.JSONDecodeError, TypeError):
        value = {}
    if value.get("intent") not in {"ask", "act", "clarify"} or value.get("confidence") not in {"high", "medium", "low"}:
        return {"intent": "clarify", "confidence": "low", "clarification": "I could not safely tell whether you want an answer or a workspace change. Choose Ask or Act."}
    if value["intent"] == "act" and value["confidence"] != "high":
        value["intent"] = "clarify"
        value["clarification"] = value.get("clarification") or "Should I answer this as a question, or carry it out as an action?"
    return value


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
    if goal_template and (requested != "act" or goal_template not in command_runner.GOAL_TEMPLATES):
        raise WorkspaceError("goal_template requires act intent and a registered template.")

    path = _chat_path(workspace, chat_id)
    process_lock = _processing_lock(path)
    with process_lock:
        key = str(path.resolve())
        _processing_paths.add(key)
        try:
            with _lock(path):
                record = _read(workspace, chat_id, recover=False)
                existing = next((item for item in record["messages"] if item.get("request_id") == request_id), None)
                if existing:
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
            if run_context and not run_kind and (requested != "act" or goal_template != "planning"):
                raise WorkspaceError("Command run context is only accepted for planning actions.")
            outcome = _process_message(workspace, chat_id, user, record, mode, goal_template, run_kind, run_context)
            return {"outcome": outcome, "chat": get_chat(workspace, chat_id)}
        except Exception as error:
            try:
                def fail(record, user):
                    user["state"] = "failed"
                    user["error"] = str(error)
                    user["outcome"] = {"kind": "error"}
                    _append_assistant(record, kind="error", content=str(error), resolved=user.get("resolved_intent") or "clarify", reply_to=user["id"], error=str(error), outcome={"kind": "error"})
                if 'user_id' in locals():
                    _finalize(workspace, chat_id, user_id, fail)
                    return {"outcome": {"kind": "error"}, "chat": get_chat(workspace, chat_id)}
            except Exception:
                pass
            raise
        finally:
            _processing_paths.discard(key)


def _process_message(
    workspace: Workspace, chat_id: str, user: dict, record: dict, mode: str,
    goal_template: str | None, run_kind: str | None = None, run_context: dict | None = None,
) -> dict:
    active = _active_run(workspace)
    # Free-text interactions have precedence over all routing.
    if active and active.get("interview", {}).get("pending_question"):
        result = runner.steer(workspace, active["id"], user["content"])
        outcome = {"kind": "interaction_answered", "run_id": active["id"]}
        def done(rec, stored):
            stored.update(state="complete", resolved_intent="interaction_response", outcome=outcome)
        _finalize(workspace, chat_id, user["id"], done)
        return outcome
    if active and active.get("schema_version", 1) >= 2:
        interaction = next((item for item in active.get("interactions") or [] if item.get("status") == "pending" and item.get("type") == "clarification"), None)
        if interaction:
            runner.resolve_interaction(workspace, active["id"], interaction["id"], {"text": user["content"]})
            outcome = {"kind": "interaction_answered", "run_id": active["id"], "interaction_id": interaction["id"]}
            def done(rec, stored): stored.update(state="complete", resolved_intent="interaction_response", outcome=outcome)
            _finalize(workspace, chat_id, user["id"], done)
            return outcome

    resolved = requested = user["requested_intent"]
    if requested == "auto":
        resolved = _deterministic_intent(user["content"])
        if resolved is None:
            classified = _classifier(record, user["content"], bool(active and active.get("schema_version", 1) >= 2))
            resolved = classified["intent"]
            clarification = classified.get("clarification")
        else:
            clarification = None
    else:
        clarification = None

    if resolved == "clarify":
        text = str(clarification or "Should I answer this as a question, or carry it out as an action?")
        outcome = {"kind": "clarification_requested"}
        def done(rec, stored):
            stored.update(state="complete", resolved_intent="clarify", outcome=outcome)
            _append_assistant(rec, kind="clarification", content=text, resolved="clarify", reply_to=stored["id"], outcome=outcome)
        _finalize(workspace, chat_id, user["id"], done)
        return outcome

    if resolved == "ask":
        if not llm.status().get("configured"):
            raise llm.LLMError("The read-only assistant model is not configured.")
        doc_ids = list(record.get("composer_context", {}).get("document_ids") or [])
        current = _document_snapshot(workspace, doc_ids)
        history = _eligible_history(record, current)
        result = assistant.ask(
            workspace, user["content"], doc_ids,
            mask_pii=current["mask_pii"], prior_turns=history,
        )
        final_snapshot = _document_snapshot(
            workspace, doc_ids, manifest=(result.get("document_context") or {}).get("manifest") or []
        )
        answer_id = _new_id("msg")
        artifact_ids = _write_artifacts(workspace, chat_id, answer_id, result.get("artifacts") or [])
        outcome = {"kind": "answer", "message_id": answer_id}
        def done(rec, stored):
            stored.update(state="complete", resolved_intent="ask", outcome=outcome, document_context=final_snapshot)
            answer = _append_assistant(
                rec, kind="text", content=str(result.get("answer") or ""), resolved="ask",
                reply_to=stored["id"], document_context=final_snapshot, artifact_ids=artifact_ids,
                outcome=outcome, citation_ids=[str(item.get("id") or _new_id("citation")) for item in result.get("citations") or []],
                citations=result.get("citations") or [], tool_trace=_safe_trace(result.get("steps") or []),
                disclosure=result.get("disclosure"), document_manifest=result.get("document_context"),
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

    # Action routing never bypasses the command runner.
    if not llm.agent_status().get("configured"):
        raise llm.LLMError("The action agent model is not configured.")
    if active and active.get("schema_version", 1) < 2:
        text = "A legacy run is active. Wait for it to finish, or pause or cancel it before starting another action."
        outcome = {"kind": "clarification_requested", "run_id": active["id"]}
        def done(rec, stored):
            stored.update(state="complete", resolved_intent="clarify", outcome=outcome)
            _append_assistant(rec, kind="clarification", content=text, resolved="clarify", reply_to=stored["id"], outcome=outcome)
        _finalize(workspace, chat_id, user["id"], done)
        return outcome

    refs = []
    if re.search(r"\b(that|it|this)\b", user["content"], re.I):
        preceding = next((item for item in reversed(record.get("messages") or []) if item.get("role") == "assistant"), None)
        ids = list((preceding or {}).get("artifact_ids") or [])
        if len(ids) != 1:
            text = "Which result do you mean? Name the artifact or use its action button."
            outcome = {"kind": "clarification_requested"}
            def done(rec, stored):
                stored.update(state="complete", resolved_intent="clarify", outcome=outcome)
                _append_assistant(rec, kind="clarification", content=text, resolved="clarify", reply_to=stored["id"], outcome=outcome)
            _finalize(workspace, chat_id, user["id"], done)
            return outcome
        artifact = _artifact(workspace, chat_id, ids[0])
        refs = [{"kind": "assistant_artifact", "id": artifact["id"], "title": artifact.get("title")}]

    command = {
        "source": "goal_template" if goal_template else ("tab_button" if user.get("source") != "composer" else "chat"),
        "text": user["content"], "goal_template": goal_template,
        "chat_id": chat_id, "source_message_id": user["id"], "context_refs": refs,
    }
    planning_context = dict(run_context or {})
    if goal_template == "planning" and not planning_context.get("document_ids"):
        planning_context["document_ids"] = list(
            (user.get("document_context") or {}).get("document_ids") or []
        )
    if active:
        response = runner.steer(
            workspace, active["id"], user["content"], chat_id=chat_id,
            source_message_id=user["id"], context_refs=refs,
            run_context=planning_context, goal_template=goal_template,
        )
        queued = response.get("command") or next((item for item in reversed((store.load_run(workspace, active["id"]).get("pending_commands") or [])) if item.get("source_message_id") == user["id"]), None)
        pending_count = len(store.load_run(workspace, active["id"]).get("pending_commands") or [])
        outcome = {"kind": "command_queued", "run_id": active["id"], "command_id": (queued or {}).get("id"), "position": max(1, pending_count)}
    else:
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
            outcome = {"kind": "run_started", "run_id": run["id"], "command_id": run["command"]["id"]}
        except runner.AgentBusyError:
            raced = _active_run(workspace)
            if not raced or raced.get("schema_version", 1) < 2:
                raise
            response = runner.steer(
                workspace, raced["id"], user["content"], chat_id=chat_id,
                source_message_id=user["id"], context_refs=refs,
                run_context=planning_context, goal_template=goal_template,
            )
            queued = response.get("command") or {}
            outcome = {"kind": "command_queued", "run_id": raced["id"], "command_id": queued.get("id"), "position": 1}
    def done(rec, stored): stored.update(state="complete", resolved_intent="act", outcome=outcome)
    _finalize(workspace, chat_id, user["id"], done)
    return outcome


def _run_projection(run: dict) -> dict:
    summary = store.run_summary(run)
    summary.update({
        "type": "run", "derived": True, "run_id": run["id"],
        "id": f"run:{run['id']}", "created_at": run.get("created"),
        "title": (run.get("command") or {}).get("text") or (run.get("goal") or {}).get("objective") or run.get("kind", "Agent run"),
        "current_activity": str(run.get("status") or "").replace("_", " "),
        "pending_attention": bool(
            any(item.get("status") == "pending" for item in run.get("approvals") or [])
            or any(item.get("status") == "pending" for item in run.get("interactions") or [])
        ),
        "summary_line": (str(run.get("summary_markdown") or "").strip().splitlines() or [run.get("error") or ""])[0],
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

    linked = []
    for summary in store.list_runs(workspace):
        if summary.get("chat_id") == chat_id:
            try:
                linked.append(_run_projection(store.load_run(workspace, summary["id"])))
            except WorkspaceError:
                continue
    by_run = {item["run_id"]: item for item in linked}
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
        run_id = (item.get("outcome") or {}).get("run_id")
        if run_id and not RUN_ID_RE.fullmatch(str(run_id)):
            continue
        if run_id and run_id in by_run:
            card = dict(by_run[run_id], created_at=item.get("created_at"), ordinal=float(item.get("ordinal") or 0) + 0.5, source_message_id=item["id"])
            transcript.append(card)
        elif run_id and (item.get("outcome") or {}).get("kind") == "command_queued":
            try:
                parent = _run_projection(store.load_run(workspace, run_id))
                parent.update({
                    "id": f"run:{run_id}:command:{(item.get('outcome') or {}).get('command_id')}",
                    "title": item.get("content") or parent["title"], "status": "queued",
                    "current_activity": f"queued position {(item.get('outcome') or {}).get('position') or 1}",
                    "created_at": item.get("created_at"),
                    "ordinal": float(item.get("ordinal") or 0) + 0.5,
                    "source_message_id": item["id"],
                })
                transcript.append(parent)
            except WorkspaceError:
                pass
    # Child runs can appear after a queued parent ends; include unplaced links.
    placed = {item.get("run_id") for item in transcript if item.get("type") == "run"}
    transcript.extend(item for item in linked if item["run_id"] not in placed)
    # Run-owned conversational messages and typed attention items are response
    # projections.  They retain stable derived IDs and are never written back
    # through chat APIs.
    for projection in linked:
        try:
            run = store.load_run(workspace, projection["run_id"])
        except WorkspaceError:
            continue
        stored_user_text = {
            str(item.get("content") or "") for item in record.get("messages") or []
            if item.get("role") == "user"
        }
        for index, message in enumerate(run.get("messages") or []):
            if message.get("role") == "user" and str(message.get("content") or "") in stored_user_text:
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
    transcript.sort(key=lambda item: (str(item.get("created_at") or ""), int(item.get("ordinal") or 0), item.get("id") or ""))

    result = dict(record)
    result.update({
        "transcript": transcript, "artifacts": artifacts, "artifact_errors": artifact_errors,
        "runs": linked, "missing_document_ids": missing, "capabilities": capabilities(),
    })
    active = _active_run(workspace)
    result["active_workspace_run"] = _run_projection(active) if active else None
    return result
