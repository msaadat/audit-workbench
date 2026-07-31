"""Agent run process launch, active-handle registry, and control surface.

This module owns run *processes*, not run behavior. It creates a durable
record, enforces one live run per workspace and the process-wide cap, launches
one daemon thread per run, and exposes the control surface
(pause/resume/cancel/approve/respond/steer/retry/continue) through the
in-memory :class:`RunHandle` registry. Every transition is persisted through
:mod:`.store` so the frontend can replay events and a restarted backend can
resume an interrupted run.

Scheduling itself belongs to the engines. :func:`_execute` asks
:mod:`.routing` for the run's explicit engine and dispatches to exactly one of
them: the domain-neutral workflow scheduler, the action-graph scheduler, or
the retained folder-intake protocol runner. No engine is inferred from record
shape, and a record without a supported engine fails closed.
"""

from __future__ import annotations

import os
import re
import threading

from .. import debug_store, llm
from ..workspaces import Workspace, WorkspaceError, load_workspace
from . import store
from .runtime import submit_approval_response, submit_interaction_response


class AgentBusyError(RuntimeError):
    """Another run is already active (per-workspace or global limit)."""


class RunHandle:
    """In-memory control state for one live run."""

    def __init__(self, workspace_id: str, run_id: str):
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.cancel_context: dict = {}
        self.pause_requested = threading.Event()
        self.resume = threading.Event()
        self.approval_resolved = threading.Event()
        self.decisions: dict[str, list] = {}
        self.inbox: list[str] = []
        self.command_queue: list[dict] = []
        self.command_queued = threading.Event()
        self.interaction_resolved = threading.Event()
        self.interaction_responses: dict[str, dict] = {}
        self.lock = threading.Lock()


_HANDLES: dict[str, RunHandle] = {}
_HANDLES_LOCK = threading.Lock()


def _max_concurrent() -> int:
    try:
        return max(1, int(os.environ.get("AGENT_MAX_CONCURRENT") or 1))
    except ValueError:
        return 1


def live_handles() -> list[RunHandle]:
    with _HANDLES_LOCK:
        return [h for h in _HANDLES.values() if h.thread and h.thread.is_alive()]


def get_handle(run_id: str) -> RunHandle | None:
    with _HANDLES_LOCK:
        handle = _HANDLES.get(run_id)
    if handle and handle.thread and handle.thread.is_alive():
        return handle
    return None


def recover_workspace(workspace: Workspace) -> list[str]:
    """Mark runs orphaned by a restart as interrupted (idempotent)."""
    live = {h.run_id for h in live_handles()}
    return store.recover_orphans(workspace, live)


# ------------------------------------------------------------ control surface
def start_run(
    workspace: Workspace,
    mode: str,
    context: dict | None = None,
    parent_run_id: str | None = None,
    kind: str = store.INTAKE_RUN_KIND,
    chat_id: str | None = None,
    source_message_id: str | None = None,
) -> dict:
    """Start the one retained protocol run: a staged folder-intake batch.

    Every other request is a command run. ``start_command_run`` classifies it
    once and routing selects the engine; there is no fixed-stage pipeline
    behind this entry point any more.
    """
    recover_workspace(workspace)
    if kind not in store.PROTOCOL_RUN_KINDS:
        raise WorkspaceError("Unknown agent run kind.")
    live = live_handles()
    if any(h.workspace_id == workspace.id for h in live):
        raise AgentBusyError(
            "An agent run is already active in this workspace. Wait for it, "
            "or pause/cancel it first."
        )
    if len(live) >= _max_concurrent():
        raise AgentBusyError(
            "The agent is busy with another workspace right now; try again "
            "when that run finishes."
        )
    model_profiles = llm.model_profile_snapshot()
    run = store.new_run(
        workspace, mode, context, parent_run_id, kind=kind,
        chat_id=chat_id, source_message_id=source_message_id,
    )
    run["model_profiles"] = model_profiles
    run["model_profiles_snapshotted"] = True
    store.save_run(workspace, run)
    store.append_event(workspace, run["id"], "run_status", {"status": "queued"})
    _launch(workspace.id, run["id"])
    return run


def start_command_run(
    workspace: Workspace,
    mode: str,
    command: dict,
    parent_run_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Start the unified engagement command runner.

    Creates a schema-v2 run and classifies it exactly once, here, before the
    worker thread starts: `routing.resolve_route` persists one normalized route
    and the selected engine, promoting the record to schema-v3 when the route is
    a workflow. A recognized command therefore reaches the UI with its full
    stage list already visible and without spending a model turn; a command the
    deterministic pass cannot classify launches with `route.status == "pending"`
    and the bounded router worker decides on the thread.
    """
    recover_workspace(workspace)
    if not llm.agent_status()["configured"]:
        raise llm.LLMError(
            "The agent's LLM is not configured. Set an API key for the "
            "assistant provider (or AGENT_PROVIDER/AGENT_MODEL) first."
        )
    # One live run per workspace, and a process-wide cap. Callers turn the
    # busy signal into a queued command on the active run rather than an error.
    live = live_handles()
    if any(handle.workspace_id == workspace.id for handle in live):
        raise AgentBusyError(
            "An agent run is already active in this workspace. New chat commands "
            "are queued on that run."
        )
    if len(live) >= _max_concurrent():
        raise AgentBusyError("The agent is busy with another workspace right now; try again when it finishes.")
    command = dict(command)
    context = dict(context or {})
    if not command.get("planning_basis_run_id") and not context.get("planning_basis_run_id"):
        basis_run_id = str((workspace.planning or {}).get("agent_run_id") or "").strip()
        if basis_run_id:
            context["planning_basis_run_id"] = basis_run_id
    model_profiles = llm.model_profile_snapshot()
    run = store.new_command_run(
        workspace, mode, command, parent_run_id=parent_run_id, context=context
    )
    # A resumed run must use the exact provider/model identities selected at
    # creation even if local settings change while it is paused.
    run["model_profiles"] = model_profiles
    run["model_profiles_snapshotted"] = True
    store.save_run(workspace, run)
    from .routing import resolve_route

    resolve_route(workspace, run)
    store.append_event(workspace, run["id"], "run_status", {"status": "queued"})
    _launch(workspace.id, run["id"])
    return run


def resume_run(workspace: Workspace, run_id: str) -> dict:
    run = store.load_run(workspace, run_id)
    handle = get_handle(run_id)
    if handle is not None:
        handle.pause_requested.clear()
        handle.resume.set()
        return run
    if run["status"] in store.TERMINAL_STATUSES:
        raise WorkspaceError("This run has finished; start a follow-up run instead.")
    recover_workspace(workspace)
    run = store.load_run(workspace, run_id)
    _launch(workspace.id, run_id)
    return run


def _carried_target_refs(workflow_state: dict) -> list[str]:
    """Target refs a linked run must inherit, including a resolved table scope.

    A workflow scope resolved during the run — an analysis run's answered scope
    checkpoint, for example — is part of what "the same request" means, and the
    durable command only carries target refs, so scoped tables travel as
    ``table:<name>`` refs.
    """
    refs = [str(value) for value in workflow_state.get("target_refs") or []]
    refs.extend(
        f"table:{value}"
        for value in (workflow_state.get("scope") or {}).get("tables") or []
    )
    return list(dict.fromkeys(refs))


def retry_run(workspace: Workspace, run_id: str) -> dict:
    """Start a fresh linked attempt without rewriting the failed run ledger."""
    previous = store.load_run(workspace, run_id)
    if previous.get("status") != "failed":
        raise WorkspaceError("Only a failed run can be retried.")
    # Command-ness, not engine: a run that failed while its route was still
    # pending has no engine yet and is still retryable as the same command.
    if not store.is_command_run(previous):
        raise WorkspaceError("This run type cannot be retried as a command.")
    original = previous.get("command") or {}
    previous_workflow = previous.get("workflow") or {}
    command = {
        "source": original.get("source") or "chat",
        "text": original.get("text") or "",
        "goal_template": original.get("goal_template"),
        "parent_command_id": original.get("id"),
        "chat_id": previous.get("chat_id") or original.get("chat_id"),
        "source_message_id": previous.get("source_message_id") or original.get("source_message_id"),
        "context_refs": list(original.get("context_refs") or []),
        "planning_basis_run_id": previous.get("planning_basis_run_id"),
        "requested_outcomes": list(previous_workflow.get("requested_outcomes") or []),
        "target_refs": _carried_target_refs(previous_workflow),
        # Structural readiness keeps successful siblings; carrying a prior
        # ``force`` mode into retry would unnecessarily repeat them.
        "generation_mode": "reuse_existing",
    }
    return start_command_run(
        workspace,
        previous["mode"],
        command,
        parent_run_id=previous["id"],
        context=dict(previous.get("context") or {}),
    )


def continue_audit(workspace: Workspace, run_id: str) -> dict:
    """Start a linked workflow from a terminal run's deterministic open items."""
    previous = store.load_run(workspace, run_id)
    if previous.get("engine") != store.WORKFLOW_ENGINE:
        raise WorkspaceError("Only a workflow run can continue audit outcomes.")
    if previous.get("status") != "completed_with_open_items":
        raise WorkspaceError("Only a run completed with open items can be continued.")
    workflow_state = previous.get("workflow") or {}
    outcomes = list(workflow_state.get("next_outcomes") or [])
    if not outcomes:
        raise WorkspaceError("This run has no remaining workflow outcomes to continue.")
    return start_command_run(
        workspace,
        previous["mode"],
        {
            "source": "follow_up",
            "text": "Continue the open audit work from the linked run.",
            "requested_outcomes": outcomes,
            "target_refs": _carried_target_refs(workflow_state)
            or ["workspace:current"],
            "generation_mode": "reuse_existing",
            "parent_command_id": (previous.get("command") or {}).get("id"),
            "chat_id": previous.get("chat_id"),
            "source_message_id": previous.get("source_message_id"),
        },
        parent_run_id=run_id,
        context={"continued_from_run_id": run_id},
    )


def notify_evidence_available(
    workspace: Workspace,
    *,
    document_ids: list[str] | None = None,
    request_ids: list[str] | None = None,
    test_ids: list[str] | None = None,
    reason: str = "evidence_updated",
) -> dict:
    """Publish targeted evidence invalidation to affected workflow runs."""
    document_ids = list(dict.fromkeys(str(value) for value in document_ids or []))
    explicit_requests = {str(value) for value in request_ids or []}
    explicit_tests = {str(value) for value in test_ids or []}
    documents_by_id = {str(item.get("id")): item for item in workspace.documents}

    def normalized(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    document_texts = [
        (
            document_id,
            normalized(
                f"{documents_by_id[document_id].get('source') or ''} "
                f"{documents_by_id[document_id].get('title') or ''} "
                f"{documents_by_id[document_id].get('category') or ''}"
            ),
        )
        for document_id in document_ids
        if document_id in documents_by_id
    ]
    affected = []
    for request in workspace.evidence_requests:
        request_id = str(request.get("id") or "")
        test_id = str(request.get("document_test_id") or "")
        identifier = normalized(request.get("transaction_identifier"))
        kinds = [normalized(value) for value in request.get("missing_document_types") or []]
        exact_matching_document_ids = [
            document_id
            for document_id, text in document_texts
            if identifier and identifier in text
        ]
        type_matching_document_ids = [
            document_id
            for document_id, text in document_texts
            if kinds and any(kind and kind in text for kind in kinds)
        ]
        matching_document_ids = list(
            dict.fromkeys([*exact_matching_document_ids, *type_matching_document_ids])
        )
        matches_document = bool(matching_document_ids)
        if request_id in explicit_requests or test_id in explicit_tests or matches_document:
            request["_matching_document_ids"] = matching_document_ids
            request["_exact_matching_document_ids"] = exact_matching_document_ids
            affected.append(request)
    # Exact transaction-identifier matches are safe to attach automatically;
    # category-only or multiple matches remain an auditor selection.
    from .. import doc_tests
    from .workflow import canonical_sha1

    availability_sha1 = canonical_sha1(
        [
            {
                key: item.get(key)
                for key in ("id", "sha1", "category", "title", "text_state")
            }
            for item in workspace.documents
        ]
    )
    availability_changed = False
    for request in affected:
        if request.get("evidence_availability_sha1") != availability_sha1:
            request["evidence_availability_sha1"] = availability_sha1
            request["updated"] = workspace._updated_now()
            availability_changed = True
    if availability_changed:
        workspace.save()

    for request in affected:
        identifier = normalized(request.get("transaction_identifier"))
        request.pop("_matching_document_ids", None)
        matches = list(request.pop("_exact_matching_document_ids", []))
        if not identifier or len(matches) != 1:
            continue
        test_id = str(request.get("document_test_id") or "")
        item_id = str(request.get("item_id") or "")
        if test_id and item_id and doc_tests.exists(workspace, test_id):
            doc_tests.attach_document(workspace, test_id, item_id, matches[0])
    blocked_units = sorted(
        {
            str(item.get("blocked_unit_id"))
            for item in affected
            if item.get("blocked_unit_id")
        }
    )
    payload = {
        "reason": reason,
        "document_ids": document_ids,
        "request_ids": sorted({str(item.get("id")) for item in affected}),
        "document_test_ids": sorted({str(item.get("document_test_id")) for item in affected if item.get("document_test_id")}),
        "blocked_unit_ids": blocked_units,
        "workspace_revision": workspace.revision,
    }
    live_ids = {handle.run_id for handle in live_handles() if handle.workspace_id == workspace.id}
    for summary in store.list_runs(workspace)[:50]:
        run_id = str(summary.get("id") or "")
        try:
            run = store.load_run(workspace, run_id)
        except WorkspaceError:
            continue
        known_units = {
            unit.get("id")
            for stage in (run.get("workflow") or {}).get("stages") or []
            for unit in stage.get("units") or []
        }
        if run_id in live_ids or known_units.intersection(blocked_units):
            store.append_event(workspace, run_id, "evidence_available", payload)
    return payload


def pause_run(workspace: Workspace, run_id: str) -> dict:
    run = store.load_run(workspace, run_id)
    handle = get_handle(run_id)
    if handle is None:
        raise WorkspaceError("This run is not currently executing.")
    handle.pause_requested.set()
    return run


def cancel_run(
    workspace: Workspace, run_id: str, *, reason: str = "",
    actor: str = "auditor", source: str = "api",
) -> dict:
    run = store.load_run(workspace, run_id)
    handle = get_handle(run_id)
    cancellation = {
        "actor": str(actor or "auditor"), "source": str(source or "api"),
        "reason": str(reason or "").strip() or None,
        "requested_at": store.utcnow(),
    }
    if handle is not None:
        handle.cancel_context = cancellation
        handle.cancel.set()
        handle.resume.set()  # unblock a paused/approval wait so it can die
        return run
    if run["status"] in store.TERMINAL_STATUSES:
        return run
    run["status"] = "cancelled"
    run["finished"] = store.utcnow()
    run["cancellation"] = {**cancellation, "cancelled_at": run["finished"]}
    if isinstance(run.get("command"), dict):
        from . import ledger

        for action in run.get("actions") or []:
            if action.get("status") in {
                "proposed", "ready", "awaiting_input", "awaiting_confirmation",
                "blocked",
            }:
                ledger.transition(action, "cancelled")
        ledger.project_action_plan(run)
        run["command"]["status"] = "cancelled"
    store.save_run(workspace, run)
    store.append_event(
        workspace, run_id, "run_status",
        {"status": "cancelled", "cancellation": run["cancellation"]},
    )
    return run


def resolve_approval(
    workspace: Workspace, run_id: str, approval_id: str, decisions: list
) -> dict:
    return submit_approval_response(
        workspace,
        run_id,
        approval_id,
        decisions,
        handle=get_handle(run_id),
    )


def resolve_interaction(
    workspace: Workspace, run_id: str, interaction_id: str, response: dict
) -> dict:
    return submit_interaction_response(
        workspace,
        run_id,
        interaction_id,
        response,
        handle=get_handle(run_id),
    )


def steer(
    workspace: Workspace,
    run_id: str,
    content: str,
    *,
    chat_id: str | None = None,
    source_message_id: str | None = None,
    context_refs: list[dict] | None = None,
    target_refs: list[str] | None = None,
    run_context: dict | None = None,
    goal_template: str | None = None,
    requested_outcomes: list[str] | None = None,
) -> dict:
    """A message to a run: steering while it's live, a persisted note while
    paused, or a linked follow-up run once it has finished."""
    content = str(content or "").strip()
    if not content:
        raise WorkspaceError("Say what the agent should do.")
    run = store.load_run(workspace, run_id)

    if store.is_command_run(run) and run["status"] in store.TERMINAL_STATUSES:
        follow_up = start_command_run(
            workspace, run["mode"],
            {"source": "follow_up", "text": content, "goal_template": goal_template,
             "parent_command_id": (run.get("command") or {}).get("id"),
             "chat_id": chat_id, "source_message_id": source_message_id,
             "context_refs": list(context_refs or []),
             "target_refs": list(target_refs or []),
             "requested_outcomes": list(requested_outcomes or [])},
            parent_run_id=run_id,
            context=run_context,
        )
        return {"handled": "follow_up_run", "run": follow_up}

    if store.is_command_run(run):
        command = {
            "id": f"cmd_{__import__('uuid').uuid4().hex[:12]}", "source": "follow_up",
            "text": content, "goal_template": goal_template, "submitted_at": store.utcnow(),
            "status": "queued", "parent_command_id": (run.get("command") or {}).get("id"),
            "chat_id": chat_id, "source_message_id": source_message_id,
            "context_refs": list(context_refs or []),
            "target_refs": list(target_refs or []),
            "requested_outcomes": list(requested_outcomes or []),
            "run_context": dict(run_context or {}),
        }
        handle = get_handle(run_id)
        if handle is not None:
            with handle.lock:
                handle.command_queue.append(command)
            handle.command_queued.set()
        else:
            run.setdefault("pending_commands", []).append(command)
            store.save_run(workspace, run)
        store.append_event(workspace, run_id, "command_queued", {"command": command})
        return {"handled": "queued_command", "run": run, "command": command}

    # The only non-command run left is folder intake: an operational import,
    # not an engagement command. Once it has finished, text entered in the
    # shared assistant drawer starts the unified command runner instead of
    # replaying the same completed batch. Any other terminal non-command record
    # is a pre-cutover shape and fails closed rather than being replayed.
    if run["status"] in store.TERMINAL_STATUSES:
        if run.get("engine") != store.INTAKE_ENGINE:
            raise WorkspaceError(
                "This run predates the supported agent run schema; start a new "
                "request instead."
            )
        follow_up = start_command_run(
            workspace,
            run["mode"],
            {"source": "follow_up", "text": content, "parent_command_id": None},
            parent_run_id=run_id,
        )
        return {"handled": "follow_up_run", "run": follow_up}

    handle = get_handle(run_id)
    if handle is not None:
        with handle.lock:
            handle.inbox.append(content)
        # The worker persists and emits it at the next checkpoint; emit a
        # provisional event now so the drawer shows the message immediately.
        store.append_event(
            workspace,
            run_id,
            "message",
            {"message": {"role": "user", "content": content, "at": store.utcnow()}},
        )
        return {"handled": "steering", "run": run}

    # Paused/interrupted with no thread: persist directly (no write contention).
    run["messages"].append(
        {"role": "user", "content": content, "at": store.utcnow(), "handled": False}
    )
    store.save_run(workspace, run)
    store.append_event(
        workspace,
        run_id,
        "message",
        {"message": run["messages"][-1]},
    )
    return {"handled": "queued", "run": run}


def _launch(workspace_id: str, run_id: str) -> None:
    handle = RunHandle(workspace_id, run_id)
    thread = threading.Thread(
        target=_execute, args=(workspace_id, run_id, handle), daemon=True,
        name=f"agent-run-{run_id}",
    )
    handle.thread = thread
    with _HANDLES_LOCK:
        _HANDLES[run_id] = handle
    thread.start()


def _execute(workspace_id: str, run_id: str, handle: RunHandle) -> None:
    try:
        workspace = load_workspace(workspace_id)
        run = store.load_run(workspace, run_id)
        with debug_store.trace_context(
            workspace_id=workspace_id, workspace_root=str(workspace.root), run_id=run_id, chat_id=run.get("chat_id"),
            purpose="agent_run", trigger="agent.run.worker",
        ):
            debug_store.capture_structural_state(workspace, trigger="run_start", run_id=run_id)
            try:
                from .routing import dispatch_engine

                # One classification per run: `dispatch_engine` only finalizes a
                # pending route or finishes a run whose route selects no engine.
                # It never infers an engine from `kind`, `schema_version`, or
                # the presence of a workflow record.
                engine = dispatch_engine(workspace, run, handle)
                if engine is None:
                    return  # the route selected no engine; the run is finished
                if engine == store.INTAKE_ENGINE:
                    from .intake_runner import IntakeRunner

                    IntakeRunner(workspace, run, handle).execute()
                elif engine == store.WORKFLOW_ENGINE:
                    from .workflow_dispatch import build_workflow_runner

                    build_workflow_runner(workspace, run, handle).execute()
                elif engine == store.ACTION_ENGINE:
                    from .action_runner import ActionRunner

                    ActionRunner(workspace, run, handle).execute()
                else:
                    raise WorkspaceError(
                        f"Agent run engine is {engine!r} or unsupported."
                    )
            finally:
                debug_store.capture_structural_state(workspace, trigger="run_completion", run_id=run_id)
    except Exception as error:  # last-resort: never leave a run stuck 'active'
        try:
            workspace = load_workspace(workspace_id)
            run = store.load_run(workspace, run_id)
            if run["status"] not in store.TERMINAL_STATUSES:
                run["status"] = "failed"
                run["error"] = str(error)
                run["finished"] = store.utcnow()
                store.save_run(workspace, run)
                store.append_event(
                    workspace, run_id, "run_status",
                    {"status": "failed", "error": str(error)},
                )
        except Exception:
            pass
    finally:
        with _HANDLES_LOCK:
            current = _HANDLES.get(run_id)
            if current is handle:
                _HANDLES.pop(run_id, None)
        try:
            workspace = load_workspace(workspace_id)
            finished = store.load_run(workspace, run_id)
            if store.is_command_run(finished) and finished["status"] in store.TERMINAL_STATUSES:
                _launch_next_command(workspace, finished)
        except Exception:
            pass


def _launch_next_command(workspace: Workspace, run: dict) -> None:
    pending = run.get("pending_commands") or []
    if not pending:
        return
    command = pending.pop(0)
    store.save_run(workspace, run)
    try:
        start_command_run(
            workspace, run["mode"], command, parent_run_id=run["id"],
            context=command.get("run_context") or {},
        )
    except Exception as error:
        run = store.load_run(workspace, run["id"])
        run.setdefault("pending_commands", []).insert(0, command)
        run.setdefault("warnings", []).append(f"Queued command could not start yet: {error}")
        store.save_run(workspace, run)
