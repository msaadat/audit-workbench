"""Agent run orchestration: state machine, worker thread, control surface.

One background thread drives one run through a fixed stage skeleton —
discovery → planning → joins → validation → analyses → dashboard → verify →
summary — creating dataset-specific tasks inside the stages as it goes. The
orchestrator (never the model) decides whether a mutation executes: in auto
mode validated mutations apply immediately; in permission mode they pause the
run as an editable approval batch.

Control (pause/resume/cancel/approve/steer) flows through in-memory
:class:`RunHandle` flags checked between units of work; every transition is
persisted through :mod:`.store` so the frontend can replay events and a
restarted backend can resume an interrupted run from its persisted plan.

Everything the model sees is assembled from :mod:`..assistant`'s
bounded workspace views supplied by the orchestrator.
"""

from __future__ import annotations

import os
import threading

from .. import analytics, assistant, dashboard, explore, llm, sandbox, validation
from ..workspaces import Workspace, WorkspaceError, load_workspace, slugify
from . import joins as joins_mod
from . import prompts, store, suggest, summary as summary_mod
from .base import BaseRunner, Cancelled, LimitExceeded

# Fixed V1 limits — deliberately not user-configurable yet.
MAX_CUSTOM_ANALYSES = 6
MAX_QUERY_TILES = 5

STAGES = (
    ("joins", "Relationships"),
    ("validation", "Validation rules"),
    ("analyses", "Analytics"),
    ("dashboard", "Dashboard"),
    ("verify", "Verification"),
    ("summary", "Summary"),
)

class AgentBusyError(RuntimeError):
    """Another run is already active (per-workspace or global limit)."""


class RunHandle:
    """In-memory control state for one live run."""

    def __init__(self, workspace_id: str, run_id: str):
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.thread: threading.Thread | None = None
        self.cancel = threading.Event()
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
    kind: str = "analysis",
    chat_id: str | None = None,
    source_message_id: str | None = None,
) -> dict:
    recover_workspace(workspace)
    if kind not in ("analysis", "intake", "doc_test", "document_analysis"):
        raise WorkspaceError("Unknown agent run kind.")
    if kind in {"analysis", "document_analysis"} and not llm.agent_status()["configured"]:
        raise llm.LLMError(
            "The agent's LLM is not configured. Set an API key for the "
            "assistant provider (or AGENT_PROVIDER/AGENT_MODEL) first."
        )
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
    if kind == "analysis" and not workspace.tables:
        raise WorkspaceError("Upload at least one data table before running the agent.")
    if kind == "doc_test":
        from .. import doc_tests

        test_id = str((context or {}).get("test_id") or "")
        if not test_id:
            raise WorkspaceError("A document-test run requires context.test_id.")
        doc_tests.load_test(workspace, test_id)
    if kind == "document_analysis":
        document_ids = list(dict.fromkeys(str(value) for value in ((context or {}).get("document_ids") or [])))
        if not document_ids:
            raise WorkspaceError("Select at least one document to analyze.")
        known = {str(document.get("id")) for document in workspace.documents}
        if any(document_id not in known for document_id in document_ids):
            raise WorkspaceError("A selected analysis document was not found.")
        if str((context or {}).get("action") or "analyze") not in {"analyze", "refresh"}:
            raise WorkspaceError("Document analysis action must be analyze or refresh.")

    run = store.new_run(
        workspace, mode, context, parent_run_id, kind=kind,
        chat_id=chat_id, source_message_id=source_message_id,
    )
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
    """Start the schema-v2 unified engagement command runner."""
    recover_workspace(workspace)
    if not llm.agent_status()["configured"]:
        raise llm.LLMError(
            "The agent's LLM is not configured. Set an API key for the "
            "assistant provider (or AGENT_PROVIDER/AGENT_MODEL) first."
        )
    live = live_handles()
    if any(handle.workspace_id == workspace.id for handle in live):
        raise AgentBusyError(
            "An agent run is already active in this workspace. New chat commands "
            "are queued on that run."
        )
    if len(live) >= _max_concurrent():
        raise AgentBusyError("The agent is busy with another workspace right now; try again when it finishes.")
    run = store.new_command_run(
        workspace, mode, command, parent_run_id=parent_run_id, context=context
    )
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


def retry_run(workspace: Workspace, run_id: str) -> dict:
    """Start a fresh linked attempt without rewriting the failed run ledger."""
    previous = store.load_run(workspace, run_id)
    if previous.get("status") != "failed":
        raise WorkspaceError("Only a failed run can be retried.")
    if previous.get("schema_version", 1) < 2 or previous.get("kind") != "audit":
        raise WorkspaceError("This run type cannot be retried as a command.")
    original = previous.get("command") or {}
    command = {
        "source": original.get("source") or "chat",
        "text": original.get("text") or "",
        "goal_template": original.get("goal_template"),
        "parent_command_id": original.get("id"),
        "chat_id": previous.get("chat_id") or original.get("chat_id"),
        "source_message_id": previous.get("source_message_id") or original.get("source_message_id"),
        "context_refs": list(original.get("context_refs") or []),
    }
    return start_command_run(
        workspace,
        previous["mode"],
        command,
        parent_run_id=previous["id"],
        context=dict(previous.get("context") or {}),
    )


def pause_run(workspace: Workspace, run_id: str) -> dict:
    run = store.load_run(workspace, run_id)
    handle = get_handle(run_id)
    if handle is None:
        raise WorkspaceError("This run is not currently executing.")
    handle.pause_requested.set()
    return run


def cancel_run(workspace: Workspace, run_id: str) -> dict:
    run = store.load_run(workspace, run_id)
    handle = get_handle(run_id)
    if handle is not None:
        handle.cancel.set()
        handle.resume.set()  # unblock a paused/approval wait so it can die
        return run
    if run["status"] in store.TERMINAL_STATUSES:
        return run
    run["status"] = "cancelled"
    run["finished"] = store.utcnow()
    store.save_run(workspace, run)
    store.append_event(workspace, run_id, "run_status", {"status": "cancelled"})
    return run


def resolve_approval(
    workspace: Workspace, run_id: str, approval_id: str, decisions: list
) -> dict:
    run = store.load_run(workspace, run_id)
    approval = next(
        (a for a in run["approvals"] if a["id"] == approval_id), None
    )
    if approval is None:
        raise WorkspaceError("Approval not found on this run.")
    if approval["status"] != "pending":
        raise WorkspaceError("This approval batch was already resolved.")
    handle = get_handle(run_id)
    if handle is None:
        raise WorkspaceError(
            "The run is not executing — resume it first, then decide."
        )
    handle.decisions[approval_id] = list(decisions or [])
    handle.approval_resolved.set()
    return run


def resolve_interaction(
    workspace: Workspace, run_id: str, interaction_id: str, response: dict
) -> dict:
    run = store.load_run(workspace, run_id)
    interaction = next((item for item in run.get("interactions") or [] if item["id"] == interaction_id), None)
    if interaction is None:
        raise WorkspaceError("Interaction not found on this run.")
    if interaction["status"] != "pending":
        raise WorkspaceError("This interaction was already resolved.")
    handle = get_handle(run_id)
    if handle is None:
        raise WorkspaceError("The run is not executing — resume it before responding.")
    with handle.lock:
        handle.interaction_responses[interaction_id] = dict(response or {})
    handle.interaction_resolved.set()
    return run


def steer(
    workspace: Workspace,
    run_id: str,
    content: str,
    *,
    chat_id: str | None = None,
    source_message_id: str | None = None,
    context_refs: list[dict] | None = None,
    run_context: dict | None = None,
    goal_template: str | None = None,
) -> dict:
    """A message to a run: steering while it's live, a persisted note while
    paused, or a linked follow-up run once it has finished."""
    content = str(content or "").strip()
    if not content:
        raise WorkspaceError("Say what the agent should do.")
    run = store.load_run(workspace, run_id)

    if run.get("schema_version", 1) >= 2 and run["status"] in store.TERMINAL_STATUSES:
        follow_up = start_command_run(
            workspace, run["mode"],
            {"source": "follow_up", "text": content, "goal_template": goal_template,
             "parent_command_id": (run.get("command") or {}).get("id"),
             "chat_id": chat_id, "source_message_id": source_message_id,
             "context_refs": list(context_refs or [])},
            parent_run_id=run_id,
            context=run_context,
        )
        return {"handled": "follow_up_run", "run": follow_up}

    if run.get("schema_version", 1) >= 2:
        command = {
            "id": f"cmd_{__import__('uuid').uuid4().hex[:12]}", "source": "follow_up",
            "text": content, "goal_template": goal_template, "submitted_at": store.utcnow(),
            "status": "queued", "parent_command_id": (run.get("command") or {}).get("id"),
            "chat_id": chat_id, "source_message_id": source_message_id,
            "context_refs": list(context_refs or []),
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

    # Intake is an operational import run, not an engagement command. Once it
    # has finished, text entered in the shared assistant drawer must start the
    # unified command runner instead of replaying the same completed batch.
    if run["status"] in store.TERMINAL_STATUSES and run.get("kind") == "intake":
        follow_up = start_command_run(
            workspace,
            run["mode"],
            {"source": "follow_up", "text": content, "parent_command_id": None},
            parent_run_id=run_id,
        )
        return {"handled": "follow_up_run", "run": follow_up}

    if run["status"] in store.TERMINAL_STATUSES:
        context = dict(run.get("context") or {})
        objective = (context.get("objective") or "").strip()
        context["objective"] = f"{objective}\n{content}".strip()
        follow_up = start_run(
            workspace,
            run["mode"],
            context,
            parent_run_id=run_id,
            kind=run.get("kind", "analysis"),
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
        if run.get("kind", "analysis") == "intake":
            from .intake_runner import IntakeRunner

            IntakeRunner(workspace, run, handle).execute()
        elif run.get("kind") == "doc_test":
            from .doc_test_runner import DocTestRunner

            DocTestRunner(workspace, run, handle).execute()
        elif run.get("kind") == "document_analysis":
            from .document_analysis_runner import DocumentAnalysisRunner

            DocumentAnalysisRunner(workspace, run, handle).execute()
        elif run.get("kind") == "audit":
            from .command_runner import CommandRunner

            CommandRunner(workspace, run, handle).execute()
        elif run.get("kind", "analysis") == "analysis":
            _Runner(workspace, run, handle).execute()
        else:
            raise WorkspaceError(
                f"Run kind '{run.get('kind')}' is not implemented yet."
            )
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
            if finished.get("schema_version", 1) >= 2 and finished["status"] in store.TERMINAL_STATUSES:
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


# -------------------------------------------------------------------- runner
class _Runner(BaseRunner):
    """The original data-analysis runner on the shared durable-run base."""

    stage_titles = dict(STAGES)

    def __init__(self, workspace: Workspace, run: dict, handle: RunHandle):
        super().__init__(workspace, run, handle)
        self.tables_meta: list[dict] = []
    # ------------------------------------------------------------- execute
    def execute(self) -> None:
        if not self.run.get("started"):
            self.run["started"] = store.utcnow()
        try:
            self.stage_discovery()
            self.stage_planning()
            self.set_status("executing")
            self.stage_joins()
            self.stage_validation()
            self.stage_analyses()
            self.stage_dashboard()
            self.stage_verify()
            self.stage_summary()
            self.run["finished"] = store.utcnow()
            self.set_status("completed")
            self.emit("summary_ready", {"run_id": self.run["id"]})
        except Cancelled:
            self.run["finished"] = store.utcnow()
            self.set_status("cancelled")
        except LimitExceeded as error:
            self.warn(f"Execution limit reached ({error}); results are partial.")
            self._finish_partial()
        except llm.LLMError as error:
            self.warn(f"The model became unavailable: {error}. Results are partial.")
            self._finish_partial()
        except Exception as error:
            self.run["error"] = str(error)
            self.run["finished"] = store.utcnow()
            self.set_status("failed")

    def _finish_partial(self) -> None:
        """Close out with whatever evidence exists — a partial run still ends
        in a usable, resumable summary."""
        try:
            self.stage_summary(allow_llm=False)
        except Exception:
            pass
        self.run["finished"] = store.utcnow()
        self.set_status("completed")
        self.emit("summary_ready", {"run_id": self.run["id"]})

    # ------------------------------------------------------------ discovery
    def stage_discovery(self) -> None:
        self.set_status("discovering")
        self.checkpoint()
        metas = []
        for name in self.ws.table_names():
            try:
                metas.append(assistant.table_metadata(self.ws, name))
            except Exception as error:
                self.warn(f"Could not profile '{name}': {error}")
        if not metas:
            raise WorkspaceError("No readable tables to analyze.")
        self.tables_meta = metas
        if not self.run["discovery"].get("tables"):
            self.run["discovery"]["tables"] = [
                {"table": m["table"], "rows": m["rows"], "columns": len(m["columns"])}
                for m in metas
            ]
            self.save()
            self.emit("discovery", {"tables": self.run["discovery"]["tables"]})

    # ------------------------------------------------------------- planning
    def stage_planning(self) -> None:
        if self.run["plan"]["stages"]:
            return  # resuming: the plan is persisted
        self.set_status("planning")
        focus: list[dict] = []
        try:
            payload = self.llm_json(
                prompts.PLANNING_SYSTEM,
                prompts.planning_user(self.tables_meta, self.context, self.guidance),
            )
            discovery = self.run["discovery"]
            discovery["domain"] = str(payload.get("domain") or "unknown")
            confidence = str(payload.get("confidence") or "low").lower()
            discovery["confidence"] = (
                confidence if confidence in ("high", "medium", "low") else "low"
            )
            roles = payload.get("table_roles")
            discovery["table_roles"] = roles if isinstance(roles, dict) else {}
            discovery["assumptions"] = [
                str(a) for a in (payload.get("assumptions") or [])
            ]
            discovery["warnings"] = [str(w) for w in (payload.get("warnings") or [])]
            known = set(self.ws.table_names())
            focus = [
                t
                for t in (payload.get("analysis_tasks") or [])
                if isinstance(t, dict) and t.get("table") in known
            ]
        except llm.LLMError as error:
            self.run["discovery"].setdefault("domain", "unknown")
            self.run["discovery"].setdefault("confidence", "low")
            self.warn(f"Planning without the model ({error}); using defaults.")
        self.run["discovery"]["focus"] = focus

        base_tables = [t["name"] for t in self.ws.tables]
        if len(base_tables) >= 2:
            self.add_task(
                "joins",
                "joins:assess",
                "Assess relationships between tables",
                "Find candidate keys, measure match rates, create strong joins.",
            )
        for table in base_tables:
            self.add_task(
                "validation",
                f"validation:{table}",
                f"Propose validation rules for {table}",
                "Deterministic suggestions plus model-advised rules, preflighted.",
            )
        self.add_task(
            "analyses",
            "analyses:propose",
            "Select analytics and custom tests",
            "Pick library tests and propose custom Polars where needed.",
        )
        self.add_task(
            "dashboard",
            "dashboard:curate",
            "Curate dashboard tiles",
            "Pin notable results and informative charts.",
        )
        self.add_task("verify", "verify:artifacts", "Verify saved artifacts")
        self.add_task("summary", "summary:write", "Write the analyst summary")
        self.emit("plan_update", {"stages": self.run["plan"]["stages"]})
        self.save()

    # ---------------------------------------------------------------- joins
    def stage_joins(self) -> None:
        task = self._task("joins:assess")
        if task is None or task["status"] in ("completed", "skipped"):
            return
        self.checkpoint()
        self.task_status(task, "running")
        try:
            candidates = joins_mod.find_candidates(self.ws)
        except Exception as error:
            self.task_status(task, "failed", str(error))
            return
        self.note_context(task, "aggregate join diagnostics (key match rates)")

        creatable = [c for c in candidates if c["strength"] == "strong"]
        proposed = [c for c in candidates if c["strength"] == "moderate"]
        for candidate in candidates:
            if candidate["strength"] == "weak":
                d = candidate["diagnostics"]
                self.warn(
                    f"Possible relation {candidate['left']}.{d['left_on']} → "
                    f"{candidate['right']}.{d['right_on']} was not joined "
                    f"(match rate {d['match_rate']:.0%}, "
                    f"row multiplication {d['row_multiplication']:.2f})."
                )

        if self.run["mode"] == "permission" and (creatable or proposed):
            items = [
                self.proposal_item(
                    title=(
                        f"Join {c['left']} → {c['right']} on "
                        f"{c['left_on'][0]} = {c['right_on'][0]} ({c['how']})"
                    ),
                    rationale=(
                        f"{c['diagnostics']['relationship']} relationship, "
                        f"match rate {c['diagnostics']['match_rate']:.0%}, "
                        f"expected rows {c['diagnostics']['expected_rows']}"
                    ),
                    spec={k: c[k] for k in ("left", "right", "left_on", "right_on", "how")},
                    evidence=c["diagnostics"],
                )
                for c in creatable + proposed
            ]
            accepted = self.request_approval("join", task, items)
            to_create = [item["spec"] for item in accepted]
        else:
            to_create = [
                {k: c[k] for k in ("left", "right", "left_on", "right_on", "how")}
                for c in creatable
            ]
            for c in proposed:
                d = c["diagnostics"]
                self.warn(
                    f"Moderate-confidence relation {c['left']} → {c['right']} "
                    f"(match rate {d['match_rate']:.0%}) left for manual review."
                )

        for spec in to_create:
            self.checkpoint()
            try:
                self._create_join(spec, task)
            except Exception as error:
                self.warn(f"Could not create join {spec.get('left')} → "
                          f"{spec.get('right')}: {error}")
        self.task_status(task, "completed")

    def _create_join(self, spec: dict, task: dict) -> None:
        semantic = (
            f"join:{spec['left']}:{spec['right']}:{'+'.join(spec['left_on'])}"
        )
        existing = self.ws.find_semantic("joins", semantic)
        if existing is not None:
            ref = f"join:{existing['name']}"
            if ref not in task["result_refs"]:
                task["result_refs"].append(ref)
            return
        base = f"{spec['left']}_{spec['right']}"
        name = base
        counter = 1
        while name in self.ws.table_names():
            counter += 1
            name = f"{base}_{counter}"
        entry = self.ws.add_join(
            {
                **spec,
                "name": name,
                "agent_run_id": self.run["id"],
                "semantic_id": semantic,
            }
        )
        self.record_artifact("join", entry["name"], semantic, "created", task)

    # ----------------------------------------------------------- validation
    def stage_validation(self) -> None:
        for table in [t["name"] for t in self.ws.tables]:
            task = self._task(f"validation:{table}")
            if task is None or task["status"] in ("completed", "skipped"):
                continue
            self.checkpoint()
            self.task_status(task, "running")
            try:
                self._validate_table(table, task)
            except (Cancelled, LimitExceeded):
                raise
            except Exception as error:
                self.task_status(task, "failed", str(error))

    def _validate_table(self, table: str, task: dict) -> None:
        baseline = suggest.suggest_rules(self.ws, table)
        self.note_context(task, f"column profile and bounded values for {table}")
        proposals = list(baseline)
        try:
            payload = self.llm_json(
                prompts.RULES_SYSTEM,
                prompts.rules_user(
                    assistant.table_metadata(self.ws, table),
                    prompts.checks_meta_for_model(),
                    baseline,
                    self.context,
                    self.guidance,
                ),
            )
            seen = {(r["column"], r["check"]) for r in baseline}
            for rule in payload.get("rules") or []:
                if not isinstance(rule, dict) or rule.get("check") not in validation.CHECKS:
                    continue
                key = (rule.get("column"), rule["check"])
                if key in seen:
                    continue
                seen.add(key)
                proposals.append(
                    {
                        "column": rule.get("column"),
                        "check": rule["check"],
                        "params": dict(rule.get("params") or {}),
                        "severity": rule.get("severity")
                        if rule.get("severity") in validation.SEVERITIES
                        else "warn",
                        "rationale": str(rule.get("rationale") or "Model-advised rule."),
                    }
                )
        except llm.LLMError as error:
            self.warn(f"Rule advice for {table} skipped ({error}); "
                      "using deterministic suggestions only.")

        proposals = self._preflight_rules(table, proposals)
        if not proposals:
            self.task_status(task, "skipped")
            return

        if self.run["mode"] == "permission":
            items = [
                self.proposal_item(
                    title=f"{rule.get('column') or '(table)'}: "
                    f"{validation.rule_label(rule)}",
                    rationale=rule.get("rationale") or "",
                    spec={k: rule[k] for k in ("column", "check", "params", "severity")},
                )
                for rule in proposals
            ]
            accepted = self.request_approval("rules", task, items)
            rules = [item["spec"] for item in accepted]
            rules = self._preflight_rules(table, rules)  # edits re-checked
        else:
            rules = proposals
        if not rules:
            self.task_status(task, "skipped")
            return

        semantic = f"ruleset:{table}"
        existing = self.ws.find_semantic("rulesets", semantic)
        if existing is not None and existing.get("created_by") != "agent":
            self.warn(
                f"A user-edited rule set already covers {table}; the agent "
                "left it untouched."
            )
            self.task_status(task, "completed")
            return
        if existing is not None:
            existing["rules"] = self.ws._normalize_rules(rules)
            self.ws.save()
            ruleset = existing
            action = "updated"
        else:
            ruleset = self.ws.add_ruleset(
                {
                    "title": f"{table} data quality",
                    "table": table,
                    "rules": rules,
                    "note": "Created by the analyst agent.",
                    "agent_run_id": self.run["id"],
                    "semantic_id": semantic,
                }
            )
            action = "created"
        self.record_artifact("ruleset", ruleset["id"], semantic, action, task)

        run_result = validation.run_rules(
            self.ws.get_frame(table), ruleset["rules"], table, resolve=self.ws.get_frame
        )
        self.ws.record_run(ruleset["id"], run_result)
        self.note_context(task, "validation verdict counts")
        self.task_status(task, "completed")

    def _preflight_rules(self, table: str, rules: list[dict]) -> list[dict]:
        """Keep only rules that execute cleanly against the current frame."""
        if not rules:
            return []
        frame = self.ws.get_frame(table)
        checked = validation.run_rules(frame, rules, table, resolve=self.ws.get_frame)
        kept = []
        for rule, result in zip(rules, checked["results"]):
            if result["verdict"] == "error":
                self.warn(
                    f"Dropped proposed rule '{result['label']}' on {table}: "
                    f"{result['error']}"
                )
            else:
                kept.append(rule)
        return kept

    # ------------------------------------------------------------- analyses
    def stage_analyses(self) -> None:
        task = self._task("analyses:propose")
        if task is None or task["status"] in ("completed", "skipped"):
            return
        self.checkpoint()
        self.task_status(task, "running")

        library: list[dict] = []
        custom: list[dict] = []
        try:
            payload = self.llm_json(
                prompts.ANALYSES_SYSTEM,
                prompts.analyses_user(
                    self.tables_meta,
                    self.run["discovery"].get("focus") or [],
                    self.context,
                    self.guidance,
                ),
            )
            known = set(self.ws.table_names())
            for item in payload.get("library") or []:
                if (
                    isinstance(item, dict)
                    and item.get("table") in known
                    and item.get("test") in analytics.ANALYTICS
                ):
                    library.append(item)
            for item in payload.get("custom") or []:
                if isinstance(item, dict) and str(item.get("code") or "").strip():
                    custom.append(item)
            custom = custom[:MAX_CUSTOM_ANALYSES]
        except llm.LLMError as error:
            self.warn(f"Analytics selection skipped ({error}).")
            self.task_status(task, "skipped")
            return

        if self.run["mode"] == "permission" and (library or custom):
            items = [
                self.proposal_item(
                    title=item.get("title")
                    or f"{analytics.ANALYTICS[item['test']]['label']} on {item['table']}",
                    rationale=item.get("rationale") or "",
                    spec={
                        "type": "library",
                        "table": item["table"],
                        "test": item["test"],
                        "params": dict(item.get("params") or {}),
                        "title": item.get("title") or "",
                    },
                )
                for item in library
            ] + [
                self.proposal_item(
                    title=item.get("title") or "Custom Polars test",
                    rationale=item.get("rationale") or "",
                    spec={
                        "type": "custom",
                        "table": item.get("table"),
                        "title": item.get("title") or "Custom Polars test",
                        "code": item["code"],
                    },
                )
                for item in custom
            ]
            accepted = self.request_approval("tests", task, items)
            specs = [item["spec"] for item in accepted]
        else:
            specs = [
                {
                    "type": "library",
                    "table": i["table"],
                    "test": i["test"],
                    "params": dict(i.get("params") or {}),
                    "title": i.get("title") or "",
                }
                for i in library
            ] + [
                {
                    "type": "custom",
                    "table": i.get("table"),
                    "title": i.get("title") or "Custom Polars test",
                    "code": i["code"],
                }
                for i in custom
            ]

        for index, spec in enumerate(specs):
            self.checkpoint()
            if spec.get("type") == "custom":
                title = spec.get("title") or f"Custom test {index + 1}"
                sub = self.add_task(
                    "analyses", f"analyses:custom:{slugify(title)}", title
                )
            else:
                label = analytics.ANALYTICS[spec["test"]]["label"]
                title = spec.get("title") or f"{label} on {spec['table']}"
                sub = self.add_task(
                    "analyses",
                    f"analyses:{spec['table']}:{spec['test']}",
                    title,
                )
            if sub["status"] in ("completed", "skipped"):
                continue
            self.task_status(sub, "running")
            try:
                if spec.get("type") == "custom":
                    self._run_custom(spec, sub)
                else:
                    self._run_library(spec, sub)
                self.task_status(sub, "completed")
            except (Cancelled, LimitExceeded):
                raise
            except Exception as error:
                self.task_status(sub, "failed", str(error))
        self.task_status(task, "completed")

    def _save_analysis(
        self, *, kind: str, table: str | None, title: str, spec: dict, viz: dict,
        source: str, semantic: str, task: dict,
    ) -> dict:
        existing = self.ws.find_semantic("analyses", semantic)
        if existing is not None and existing.get("created_by") != "agent":
            self.warn(f"'{title}' exists as user-edited work; left untouched.")
            return existing
        if existing is not None:
            existing["spec"] = dict(spec)
            existing["viz"] = dict(viz)
            existing["title"] = title
            self.ws.save()
            self.record_artifact("analysis", existing["id"], semantic, "updated", task)
            return existing
        analysis = self.ws.add_analysis(
            {
                "kind": kind,
                "table": table,
                "title": title,
                "spec": spec,
                "viz": viz,
                "source": source,
                "note": "Created by the analyst agent.",
                "agent_run_id": self.run["id"],
                "semantic_id": semantic,
            }
        )
        self.record_artifact("analysis", analysis["id"], semantic, "created", task)
        return analysis

    def _run_library(self, spec: dict, task: dict) -> None:
        frame = self.ws.get_frame(spec["table"])
        result = analytics.run_test(frame, spec["test"], spec.get("params") or {})
        self.note_context(task, "analytics verdict and bounded result preview")
        self._save_analysis(
            kind="analytics",
            table=spec["table"],
            title=spec.get("title") or result.title,
            spec={"test": spec["test"], "params": dict(spec.get("params") or {})},
            viz=result.viz or {"type": "table"},
            source="library",
            semantic=f"analysis:{spec['table']}:{spec['test']}",
            task=task,
        )

    def _run_custom(self, spec: dict, task: dict) -> None:
        code = str(spec.get("code") or "")
        frames = {}
        for name in self.ws.table_names():
            try:
                frames[name] = self.ws.get_frame(name)
            except Exception:
                continue
        try:
            sandbox.run(code, frames)
        except Exception as error:
            # One model-assisted repair, then give up on this snippet.
            table_meta = None
            if spec.get("table") in self.ws.table_names():
                table_meta = assistant.table_metadata(self.ws, spec["table"])
            payload = self.llm_json(
                prompts.FIX_CODE_SYSTEM,
                prompts.fix_code_user(code, str(error), table_meta),
            )
            code = str(payload.get("code") or "").strip()
            if not code:
                raise
            sandbox.run(code, frames)  # raises if still broken
        self.run["usage"]["custom_analyses"] += 1
        self.note_context(task, "bounded custom-result preview")
        title = spec.get("title") or "Custom Polars test"
        self._save_analysis(
            kind="python",
            table=spec.get("table"),
            title=title,
            spec={"code": code},
            viz={"type": "table"},
            source="ai",
            semantic=f"analysis:custom:{slugify(title)}",
            task=task,
        )

    # ------------------------------------------------------------ dashboard
    def stage_dashboard(self) -> None:
        task = self._task("dashboard:curate")
        if task is None or task["status"] in ("completed", "skipped"):
            return
        self.checkpoint()
        self.task_status(task, "running")

        # Notable analytics results → analytics tiles; every agent rule set →
        # a validation tile. Tiles are reversible, so this applies in both
        # modes without approval.
        for analysis in list(self.ws.analyses):
            if analysis.get("agent_run_id") != self.run["id"]:
                continue
            if analysis["kind"] != "analytics":
                continue
            payload = dashboard.analysis_payload(self.ws, analysis)
            if payload.get("error") or payload.get("verdict") not in ("warn", "fail"):
                continue
            self._pin_tile(
                task,
                kind="analytics",
                table=analysis["table"],
                title=analysis["title"],
                spec=analysis["spec"],
                viz=analysis.get("viz") or {"type": "table"},
                semantic=f"tile:{analysis.get('semantic_id')}",
                note="Pinned by the analyst agent (notable verdict).",
            )
        for ruleset in list(self.ws.rulesets):
            if ruleset.get("agent_run_id") != self.run["id"]:
                continue
            self._pin_tile(
                task,
                kind="validation",
                table=ruleset["table"],
                title=f"Validation — {ruleset['table']}",
                spec={"rules": ruleset["rules"]},
                viz={"type": "table"},
                semantic=f"tile:{ruleset.get('semantic_id')}",
                note="Pinned by the analyst agent.",
            )

        try:
            payload = self.llm_json(
                prompts.DASHBOARD_SYSTEM,
                prompts.dashboard_user(self.tables_meta, self.context, self.guidance),
            )
            proposals = [
                p
                for p in (payload.get("queries") or [])
                if isinstance(p, dict) and p.get("table") in self.ws.table_names()
            ][:MAX_QUERY_TILES]
        except llm.LLMError as error:
            self.warn(f"Chart proposals skipped ({error}).")
            proposals = []

        for proposal in proposals:
            self.checkpoint()
            spec = dict(proposal.get("spec") or {})
            spec.pop("page", None)
            spec.pop("page_size", None)
            title = str(proposal.get("title") or f"View on {proposal['table']}")
            try:
                frame = self.ws.get_frame(proposal["table"])
                result, _ = explore.run_query_full(frame, spec)
                if not result.height:
                    continue
                viz = self._safe_viz(proposal.get("viz"), result.columns)
            except Exception as error:
                self.warn(f"Dropped proposed chart '{title}': {error}")
                continue
            self.note_context(task, "bounded query-result preview")
            self._pin_tile(
                task,
                kind="query",
                table=proposal["table"],
                title=title,
                spec=spec,
                viz=viz,
                semantic=f"tile:query:{slugify(title)}",
                note=str(proposal.get("rationale") or ""),
            )
        self.task_status(task, "completed")

    def _safe_viz(self, viz: dict | None, columns: list[str]) -> dict:
        viz = dict(viz or {})
        kind = viz.get("type")
        if kind not in ("bar", "line", "pie", "table"):
            return {"type": "table"}
        if kind == "table":
            return {"type": "table"}
        x = viz.get("x")
        ys = [y for y in (viz.get("y") or []) if y in columns]
        if x not in columns or not ys:
            return {"type": "table"}
        return {"type": kind, "x": x, "y": ys}

    def _pin_tile(
        self, task: dict, *, kind: str, table: str | None, title: str, spec: dict,
        viz: dict, semantic: str, note: str,
    ) -> None:
        existing = self.ws.find_semantic("tiles", semantic)
        if existing is not None:
            if existing.get("created_by") == "agent":
                existing["spec"] = dict(spec)
                existing["viz"] = dict(viz)
                existing["title"] = title
                self.ws.save()
                self.record_artifact("tile", existing["id"], semantic, "updated", task)
            return
        try:
            tile = self.ws.add_tile(
                {
                    "kind": kind,
                    "table": table,
                    "title": title,
                    "spec": spec,
                    "viz": viz,
                    "note": note,
                    "agent_run_id": self.run["id"],
                    "semantic_id": semantic,
                }
            )
        except WorkspaceError as error:
            self.warn(f"Could not pin '{title}': {error}")
            return
        self.record_artifact("tile", tile["id"], semantic, "created", task)

    # --------------------------------------------------------------- verify
    def stage_verify(self) -> None:
        task = self._task("verify:artifacts")
        if task is None or task["status"] in ("completed", "skipped"):
            return
        self.set_status("verifying")
        self.task_status(task, "running")
        checks = []
        for artifact in self.run["artifacts"]:
            self.checkpoint()
            ref = f"{artifact['kind']}:{artifact['id']}"
            ok, error = True, None
            try:
                if artifact["kind"] == "tile":
                    item = next(
                        (t for t in self.ws.tiles if t["id"] == artifact["id"]), None
                    )
                    payload = dashboard.compute_payload(self.ws, item) if item else None
                    ok = bool(payload) and not payload.get("error")
                    error = (payload or {}).get("error") or (None if ok else "missing")
                elif artifact["kind"] == "analysis":
                    item = next(
                        (a for a in self.ws.analyses if a["id"] == artifact["id"]), None
                    )
                    payload = dashboard.analysis_payload(self.ws, item) if item else None
                    ok = bool(payload) and not payload.get("error")
                    error = (payload or {}).get("error") or (None if ok else "missing")
                elif artifact["kind"] == "join":
                    self.ws.get_frame(artifact["id"])
                # rulesets were executed at save time; nothing extra to verify
            except Exception as exc:
                ok, error = False, str(exc)
            checks.append({"ref": ref, "ok": ok, "error": error})
            if not ok:
                self.warn(f"Verification failed for {ref}: {error}")
        self.run["verification"] = checks
        self.save()
        self.task_status(task, "completed")

    # -------------------------------------------------------------- summary
    def stage_summary(self, allow_llm: bool = True) -> None:
        task = self._task("summary:write")
        if task is not None and task["status"] == "completed":
            return
        self.set_status("summarizing")
        if task is not None:
            self.task_status(task, "running")
        evidence = self._build_evidence()
        refs = self._valid_refs(evidence)
        findings: list[dict] = []
        markdown = ""
        if allow_llm:
            try:
                payload = self.llm_json(
                    prompts.SUMMARY_SYSTEM,
                    prompts.summary_user(evidence, self.context, self.guidance),
                )
                findings = summary_mod.clean_findings(payload.get("findings"), refs)
                markdown = str(payload.get("summary_markdown") or "").strip()
            except llm.LLMError as error:
                self.warn(f"Summary narrative skipped ({error}).")
        if not markdown:
            markdown = summary_mod.fallback_markdown(self.run, evidence)
        self.run["findings"] = findings
        self.run["summary_markdown"] = markdown
        self.save()
        if task is not None:
            self.task_status(task, "completed")

    def _valid_refs(self, evidence: dict) -> set[str]:
        refs = set()
        for group in ("joins", "rulesets", "analyses", "tiles"):
            for item in evidence.get(group, []):
                refs.add(item["ref"])
        return refs

    def _build_evidence(self) -> dict:
        """Aggregate-only evidence for the summary prompt, recomputed from
        saved specs so it also works when resuming an interrupted run."""
        evidence: dict = {
            "tables": self.run["discovery"].get("tables") or [],
            "domain": self.run["discovery"].get("domain"),
            "assumptions": self.run["discovery"].get("assumptions") or [],
            "warnings": self.run.get("warnings") or [],
            "joins": [],
            "rulesets": [],
            "analyses": [],
            "tiles": [],
        }
        run_id = self.run["id"]
        for join in self.ws.joins:
            if join.get("agent_run_id") != run_id:
                continue
            entry = {
                "ref": f"join:{join['name']}",
                "name": join["name"],
                "left": join["left"],
                "right": join["right"],
            }
            try:
                left = self.ws.get_frame(join["left"])
                right = self.ws.get_frame(join["right"])
                entry["diagnostics"] = joins_mod.diagnose(
                    left, right, join["left_on"][0], join["right_on"][0]
                )
            except Exception:
                pass
            evidence["joins"].append(entry)
        for ruleset in self.ws.rulesets:
            if ruleset.get("agent_run_id") != run_id:
                continue
            runs = ruleset.get("runs") or []
            evidence["rulesets"].append(
                {
                    "ref": f"ruleset:{ruleset['id']}",
                    "title": ruleset["title"],
                    "table": ruleset["table"],
                    "rules": len(ruleset["rules"]),
                    "last_run": runs[-1] if runs else None,
                }
            )
        for analysis in self.ws.analyses:
            if analysis.get("agent_run_id") != run_id:
                continue
            payload = dashboard.analysis_payload(self.ws, analysis)
            evidence["analyses"].append(
                {
                    "ref": f"analysis:{analysis['id']}",
                    "title": analysis["title"],
                    "table": analysis.get("table"),
                    "kind": analysis["kind"],
                    "verdict": payload.get("verdict"),
                    "verdict_text": payload.get("verdict_text"),
                    "stats": payload.get("stats"),
                    "error": payload.get("error"),
                }
            )
        for tile in self.ws.tiles:
            if tile.get("agent_run_id") != run_id:
                continue
            evidence["tiles"].append(
                {
                    "ref": f"tile:{tile['id']}",
                    "title": tile["title"],
                    "kind": tile["kind"],
                }
            )
        return evidence
