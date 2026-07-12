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
disclosure-gated metadata views — raw data rows never enter a prompt.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from .. import analytics, assistant, dashboard, explore, llm, sandbox, validation
from ..workspaces import Workspace, WorkspaceError, load_workspace, slugify
from . import joins as joins_mod
from . import prompts, store, suggest, summary as summary_mod

# Fixed V1 limits — deliberately not user-configurable yet.
MAX_LLM_TURNS = 40
MAX_TASKS = 60
MAX_CUSTOM_ANALYSES = 6
MAX_RUNTIME_SECONDS = 1800
MAX_QUERY_TILES = 5
LLM_JSON_ATTEMPTS = 2

STAGES = (
    ("joins", "Relationships"),
    ("validation", "Validation rules"),
    ("analyses", "Analytics"),
    ("dashboard", "Dashboard"),
    ("verify", "Verification"),
    ("summary", "Summary"),
)

DISCLOSURE_NOTE = assistant.DISCLOSURE


class AgentBusyError(RuntimeError):
    """Another run is already active (per-workspace or global limit)."""


class _Cancelled(Exception):
    pass


class _LimitExceeded(Exception):
    pass


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
) -> dict:
    recover_workspace(workspace)
    if not llm.agent_status()["configured"]:
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
    if not workspace.tables:
        raise WorkspaceError("Upload at least one data table before running the agent.")

    run = store.new_run(workspace, mode, context, parent_run_id)
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


def steer(workspace: Workspace, run_id: str, content: str) -> dict:
    """A message to a run: steering while it's live, a persisted note while
    paused, or a linked follow-up run once it has finished."""
    content = str(content or "").strip()
    if not content:
        raise WorkspaceError("Say what the agent should do.")
    run = store.load_run(workspace, run_id)

    if run["status"] in store.TERMINAL_STATUSES:
        context = dict(run.get("context") or {})
        objective = (context.get("objective") or "").strip()
        context["objective"] = f"{objective}\n{content}".strip()
        follow_up = start_run(
            workspace, run["mode"], context, parent_run_id=run_id
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
        _Runner(workspace, run, handle).execute()
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


# -------------------------------------------------------------------- runner
class _Runner:
    def __init__(self, workspace: Workspace, run: dict, handle: RunHandle):
        self.ws = workspace
        self.run = run
        self.handle = handle
        self.deadline = time.monotonic() + MAX_RUNTIME_SECONDS
        self.tables_meta: list[dict] = []

    # ------------------------------------------------------------ plumbing
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
        self.run["warnings"].append(text)
        self.save()
        self.emit("warning", {"text": text})

    def checkpoint(self) -> None:
        """Between units of work: honor cancel/pause, apply steering, enforce
        the runtime budget."""
        if self.handle.cancel.is_set():
            raise _Cancelled()
        if self.handle.pause_requested.is_set():
            self.set_status("paused")
            waited_from = time.monotonic()
            while not self.handle.resume.wait(0.2):
                if self.handle.cancel.is_set():
                    raise _Cancelled()
            self.handle.resume.clear()
            self.handle.pause_requested.clear()
            self.deadline += time.monotonic() - waited_from  # user time is free
            self.set_status("executing")
        self._drain_inbox()
        if time.monotonic() > self.deadline:
            raise _LimitExceeded("run time limit reached")

    def _drain_inbox(self) -> None:
        with self.handle.lock:
            pending, self.handle.inbox = self.handle.inbox[:], []
        for content in pending:
            self.run["messages"].append(
                {
                    "role": "user",
                    "content": content,
                    "at": store.utcnow(),
                    "handled": True,
                }
            )
        if pending:
            self.save()

    @property
    def guidance(self) -> list[str]:
        return [m["content"] for m in self.run["messages"] if m["role"] == "user"]

    @property
    def context(self) -> dict:
        return self.run.get("context") or {}

    # ------------------------------------------------------------ LLM turns
    def llm_json(self, system: str, user: str) -> dict:
        """One structured LLM exchange with a parse-retry. Counts turns."""
        last_error = ""
        attempt_user = user
        for _ in range(LLM_JSON_ATTEMPTS):
            self.checkpoint()
            if self.run["usage"]["llm_turns"] >= MAX_LLM_TURNS:
                raise _LimitExceeded("model turn limit reached")
            self.run["usage"]["llm_turns"] += 1
            self.save()
            message = llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": attempt_user},
                ],
                profile="agent",
            )
            try:
                return prompts.parse_json_object(message.get("content") or "")
            except (ValueError, json.JSONDecodeError) as error:
                last_error = str(error)
                attempt_user = (
                    f"{user}\n\nYour previous response could not be used: "
                    f"{last_error}. {prompts.JSON_RULES}"
                )
        raise llm.LLMError(f"The model did not return usable JSON: {last_error}")

    # ---------------------------------------------------------------- tasks
    def _stage(self, stage_id: str) -> dict:
        for stage in self.run["plan"]["stages"]:
            if stage["id"] == stage_id:
                return stage
        stage = {
            "id": stage_id,
            "title": dict(STAGES).get(stage_id, stage_id.title()),
            "tasks": [],
        }
        self.run["plan"]["stages"].append(stage)
        return stage

    def _task(self, task_id: str) -> dict | None:
        for stage in self.run["plan"]["stages"]:
            for task in stage["tasks"]:
                if task["id"] == task_id:
                    return task
        return None

    def add_task(
        self, stage_id: str, task_id: str, title: str, detail: str = ""
    ) -> dict:
        existing = self._task(task_id)
        if existing is not None:
            return existing
        total = sum(len(s["tasks"]) for s in self.run["plan"]["stages"])
        if total >= MAX_TASKS:
            raise _LimitExceeded("task limit reached")
        task = {
            "id": task_id,
            "stage": stage_id,
            "title": title,
            "detail": detail,
            "status": "queued",
            "error": None,
            "result_refs": [],
            "disclosure": [],
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

    def disclose(self, task: dict, note: str) -> None:
        if note not in task["disclosure"]:
            task["disclosure"].append(note)

    def record_artifact(
        self, kind: str, item_id: str, semantic_id: str, action: str, task: dict | None
    ) -> str:
        ref = f"{kind}:{item_id}"
        self.run["artifacts"].append(
            {"kind": kind, "id": item_id, "semantic_id": semantic_id, "action": action}
        )
        if task is not None and ref not in task["result_refs"]:
            task["result_refs"].append(ref)
        self.save()
        self.emit(
            "workspace_changed", {"kind": kind, "id": item_id, "action": action}
        )
        return ref

    # ------------------------------------------------------------ approvals
    def _pending_approval(self, kind: str, task_id: str) -> dict | None:
        for approval in self.run["approvals"]:
            if (
                approval["kind"] == kind
                and approval["task_id"] == task_id
                and approval["status"] == "pending"
            ):
                return approval
        return None

    def request_approval(self, kind: str, task: dict, items: list[dict]) -> list[dict]:
        """Pause for an editable approval batch; returns the accepted items
        with any edits applied (empty when everything was rejected). Reuses a
        persisted pending batch when resuming an interrupted run."""
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
        decisions: list | None = None
        while decisions is None:
            if self.handle.cancel.is_set():
                raise _Cancelled()
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
        approval["status"] = "resolved"
        approval["resolved"] = store.utcnow()
        self.save()
        self.emit("approval_resolved", {"approval": approval})
        self.set_status("executing")
        self.task_status(task, "running")

        accepted = []
        for item in approval["items"]:
            if item["decision"] == "approved":
                accepted.append({**item, "spec": item["spec"]})
            elif item["decision"] == "edited" and item.get("edited_spec"):
                accepted.append({**item, "spec": item["edited_spec"]})
        return accepted

    def proposal_item(
        self, title: str, rationale: str, spec: dict, evidence: dict | None = None
    ) -> dict:
        return {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "rationale": rationale,
            "spec": spec,
            "evidence": evidence or {},
            "disclosure": DISCLOSURE_NOTE,
            "decision": None,
            "edited_spec": None,
        }

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
        except _Cancelled:
            self.run["finished"] = store.utcnow()
            self.set_status("cancelled")
        except _LimitExceeded as error:
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
        self.disclose(task, "aggregate join diagnostics (key match rates)")

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
            except (_Cancelled, _LimitExceeded):
                raise
            except Exception as error:
                self.task_status(task, "failed", str(error))

    def _validate_table(self, table: str, task: dict) -> None:
        baseline = suggest.suggest_rules(self.ws, table)
        self.disclose(task, f"column profile of {table} (aggregates only)")
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
        self.disclose(task, "validation verdict counts (aggregates only)")
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
            except (_Cancelled, _LimitExceeded):
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
        self.disclose(task, "analytics verdict and aggregate summary")
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
        self.disclose(task, "shape/aggregate stats of the custom result")
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
            self.disclose(task, "aggregated query result (preview)")
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
