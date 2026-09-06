"""The steering loop: one durable, budgeted tool loop per request.

The third engine. Where the workflow scheduler executes a plan that routing
fixed before the run started, this one *decides* — reads the workspace, plans an
outcome set, runs it as a child run, reads what happened, repairs what it can,
asks when it cannot, and says what it did. What it may decide is bounded by the
tools in :mod:`.loop_tools`, which gate every effect; what it may spend is
bounded by four durable budgets and the run deadline.

The loop is a *scheduler*, not a second way to change the workspace. Every
artifact it produces is produced by a child command run of one of the other
engines, through the same materialization, the same unit pipeline, the same
approvals and the same receipts. A child of this loop and the same run started
from a tab button are the same record, byte for byte.

Durability. The conversation the loop is having with the model lives beside the
run as ``conversation.json`` and is rewritten after every turn, so a crash
resumes the request at its next turn rather than restarting it. The durable
record itself stays content-free apart from the loop's own prose.
"""

from __future__ import annotations

import json
import time

from .. import llm
from ..workspaces import Workspace, WorkspaceError, write_json_atomic
from . import loop_tools, narration, prompts, store
from ..assistant_tools import ReadToolSession, loop_tool_schemas
from .base import BaseRunner
from .runtime import Cancelled, LimitExceeded

#: One tool result the model is handed, at most this long. A read that returns
#: more than this is one the loop should have narrowed.
MAX_TOOL_RESULT_CHARS = 6_000
#: The whole conversation, at most this long. Past it, older tool results are
#: replaced by their first line: what the loop *did* stays, what it read is
#: summarized. The assistant's own turns are never trimmed — they are the
#: reasoning that has to survive for the next turn to make sense.
MAX_CONVERSATION_CHARS = 60_000
#: How many recent turns keep their tool bodies whole while trimming.
KEPT_TOOL_TURNS = 6
#: Trimmed bodies keep this much of what they said.
OMITTED_SUMMARY_CHARS = 200


class ConversationStore:
    """The loop's conversation, persisted beside its run.

    ``delivered_messages`` travels with it: how many of the run's own user
    messages the loop has already read. Without it a resume cannot tell a
    steering message it has answered from one that arrived while it was down,
    and would either repeat the first or lose the second.
    """

    SCHEMA = 1
    FILENAME = "conversation.json"

    def __init__(self, workspace: Workspace, run_id: str):
        self.workspace = workspace
        self.run_id = str(run_id)

    @property
    def path(self):
        return store.run_dir(self.workspace, self.run_id) / self.FILENAME

    def load(self) -> dict | None:
        path = self.path
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or int(payload.get("schema") or 0) != self.SCHEMA:
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        return {
            "messages": [item for item in messages if isinstance(item, dict)],
            "delivered_messages": int(payload.get("delivered_messages") or 0),
        }

    def save(self, messages: list[dict], delivered_messages: int = 0) -> None:
        write_json_atomic(
            self.path,
            {
                "schema": self.SCHEMA,
                "messages": messages,
                "delivered_messages": int(delivered_messages),
            },
        )


def bounded_conversation(messages: list[dict]) -> list[dict]:
    """Trim old tool bodies until the conversation fits its budget.

    Oldest first, and only tool results: an assistant turn is a decision the
    later turns refer back to, and dropping one makes the transcript lie about
    why something ran. A dropped result keeps its first line, which is enough
    for the model to know it already looked and roughly what it saw.
    """

    if len(json.dumps(messages, default=str)) <= MAX_CONVERSATION_CHARS:
        return messages
    trimmed = [dict(message) for message in messages]
    tool_positions = [
        index for index, message in enumerate(trimmed) if message.get("role") == "tool"
    ]
    for index in tool_positions[:-KEPT_TOOL_TURNS] if len(tool_positions) > KEPT_TOOL_TURNS else []:
        content = str(trimmed[index].get("content") or "")
        if content.startswith('{"omitted"'):
            continue
        trimmed[index]["content"] = json.dumps(
            {"omitted": True, "summary": content[:OMITTED_SUMMARY_CHARS]}
        )
        if len(json.dumps(trimmed, default=str)) <= MAX_CONVERSATION_CHARS:
            break
    return trimmed


class AgentLoop(BaseRunner):
    """One request, carried out by a model steering gated capabilities."""

    def __init__(self, workspace: Workspace, run: dict, handle, **kwargs):
        super().__init__(workspace, run, handle, **kwargs)
        self.conversation_store = ConversationStore(workspace, run["id"])
        self.conversation: list[dict] = []
        self.reads = ReadToolSession(workspace, chat_id=run.get("chat_id"))
        self.tools = loop_tools.LoopTools(self)
        self._tool_schemas = [
            *loop_tool_schemas(),
            *loop_tools.tool_schemas(),
            *loop_tools.action_tools(),
        ]
        self._read_names = {
            str(schema["function"]["name"]) for schema in loop_tool_schemas()
        }
        self._finish: dict | None = None
        # Consecutive turns whose every tool call was a read. Reading is the
        # cheapest thing a model can always do next, and a request no outcome
        # can carry out gives it no reason to stop; see ``_note_read_only_turn``.
        self._read_only_turns = 0
        # Built on first use: an action tool call is the only thing that needs
        # it, and most requests never make one.
        self._action_execution = None
        # The count of user messages already delivered into the conversation.
        # Steering arrives as run messages through the runtime's inbox drain,
        # and the loop reads forward from here at the top of every turn.
        self._delivered_messages = 0

    # -- lifecycle ---------------------------------------------------------- #
    def execute(self) -> None:
        try:
            self.mark_started()
            self.set_status("executing")
            self._resume_children()
            self._load_conversation()
            self._loop()
        except Cancelled:
            self.run["cancellation"] = {
                "requested_at": self.utcnow(),
                "reason": (getattr(self.handle, "cancel_context", {}) or {}).get("reason"),
            }
            self._close("cancelled")
        except LimitExceeded as error:
            self.warn(str(error))
            self._close("completed_with_open_items", note=str(error))
        except llm.LLMError as error:
            self.warn(str(error))
            self._close("completed_with_open_items", note=str(error))
        except Exception as error:  # a fault, not an outcome
            self.run["error"] = str(error)
            self._close("failed")

    def _loop(self) -> None:
        turns = 0
        maximum = int((self.run.get("limits") or {}).get("max_loop_turns") or 24)
        while True:
            self.checkpoint()
            self._deliver_steering()
            if turns >= maximum:
                self._close(
                    "completed_with_open_items",
                    note=(
                        "I stopped after "
                        f"{turns} steps, which is as far as one request goes. "
                        "Tell me what to do next and I'll carry on."
                    ),
                )
                return
            turns += 1
            self.set_activity(
                "loop.turn",
                "Working out what to do next",
                current=turns,
                total=maximum,
                task_id="loop",
            )
            message = self._llm_message(
                prompts.LOOP_SYSTEM,
                bounded_conversation(self.conversation),
                self._tool_schemas,
                # Every loop turn is a first attempt at a *different* turn. The
                # gateway reads ``attempt`` as a retry counter — it charges
                # ``usage["retries"]`` and labels the activity strip with it —
                # so passing the turn number here reported a ten-turn request
                # as nine retries of one call.
                attempt=1,
            )
            calls = [
                call
                for call in (message.get("tool_calls") or [])
                if isinstance(call, dict)
            ]
            self._append(
                {
                    "role": "assistant",
                    "content": str(message.get("content") or ""),
                    **({"tool_calls": calls} if calls else {}),
                }
            )
            if not calls:
                # A turn with nothing to call is an answer. It ends the request
                # the same way ``finish`` does, so a model that forgets the tool
                # still leaves a run that said something and stopped.
                self._close("completed", note=str(message.get("content") or ""))
                return
            for call in calls:
                self._dispatch(call)
                if self._finish is not None:
                    self._close_with_finish()
                    return
            if self._note_read_only_turn(calls):
                return

    def _note_read_only_turn(self, calls: list[dict]) -> bool:
        """Make a loop that only reads decide. True when the request is over.

        Observed live: asked for something no registered outcome can do, the
        loop read for eleven turns and thirty-four tool calls without starting
        anything. Reading always looks like progress and never commits, so
        nothing in the request itself ever ends it — the turn budget does,
        expensively, having learned nothing since turn four.

        One nudge at the limit, and one stop at twice it. Not prompt-only,
        because "you have read enough" is a fact about the run rather than
        advice about the work.
        """

        names = {
            str((call.get("function") or {}).get("name") or "")
            for call in calls
            if isinstance(call.get("function"), dict)
        }
        if not names or not names <= self._read_names:
            self._read_only_turns = 0
            return False
        self._read_only_turns += 1
        limit = int((self.run.get("limits") or {}).get("max_read_turns") or 4)
        if self._read_only_turns == limit:
            self._append(
                {
                    "role": "user",
                    "content": (
                        f"You have taken {self._read_only_turns} turns that only "
                        "read. Decide now: plan and run an outcome, ask the "
                        "auditor a question, or finish and say plainly what you "
                        "cannot do and why. Do not read again first."
                    ),
                }
            )
            return False
        if self._read_only_turns >= limit * 2:
            self._close(
                "completed_with_open_items",
                note=(
                    "I read the workspace at length without finding work I could "
                    f"carry out — {self._read_only_turns} turns of reading and "
                    "nothing to run. Nothing was changed. Tell me which artifact "
                    "you want changed, or ask me what I found."
                ),
            )
            return True
        return False

    # -- conversation ------------------------------------------------------- #
    def _load_conversation(self) -> None:
        existing = self.conversation_store.load()
        if existing and existing["messages"]:
            self.conversation = existing["messages"]
            # Anything the auditor said while the loop was down is unread, and
            # is delivered by the next turn's ordinary steering drain.
            self._delivered_messages = existing["delivered_messages"]
            self._deliver_resume_note()
            return
        seed = (self.run.get("context") or {}).get("conversation_seed")
        turns = [item for item in seed if isinstance(item, dict)] if isinstance(seed, list) else []
        self.conversation = [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
            for item in turns
            if str(item.get("content") or "").strip()
            and str(item.get("role") or "") in ("user", "assistant")
        ]
        request = str((self.run.get("command") or {}).get("text") or "").strip()
        if request:
            self.conversation.append({"role": "user", "content": request})
        if not self.conversation:
            raise WorkspaceError("A steering loop needs a request to carry out.")
        self._delivered_messages = sum(
            1 for item in self.run.get("messages") or [] if item.get("role") == "user"
        )
        self._persist()

    def _append(self, message: dict) -> None:
        self.conversation.append(message)
        self._persist()

    def _persist(self) -> None:
        self.conversation_store.save(self.conversation, self._delivered_messages)

    def _deliver_steering(self) -> None:
        """Hand the loop whatever the auditor said since its last turn."""

        user_messages = [
            item for item in self.run.get("messages") or [] if item.get("role") == "user"
        ]
        fresh = user_messages[self._delivered_messages :]
        if not fresh:
            return
        self._delivered_messages = len(user_messages)
        for message in fresh:
            content = str(message.get("content") or "").strip()
            if content:
                self._append(
                    {
                        "role": "user",
                        "content": f"The auditor added, while you were working: {content}",
                    }
                )
        self._persist()

    def _mark_messages_delivered(self) -> None:
        self._delivered_messages = sum(
            1 for item in self.run.get("messages") or [] if item.get("role") == "user"
        )
        self._persist()

    def _deliver_resume_note(self) -> None:
        self._append(
            {
                "role": "user",
                "content": (
                    "This request was interrupted and has resumed. Check what "
                    "the runs you already started ended as before starting more."
                ),
            }
        )

    # -- tool dispatch ------------------------------------------------------ #
    def _dispatch(self, call: dict) -> None:
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or "")
        args = _tool_arguments(function.get("arguments"))
        spent = int((self.run.get("usage") or {}).get("tool_calls") or 0)
        allowed = int((self.run.get("limits") or {}).get("max_tool_calls") or 0)
        if allowed and spent >= allowed:
            self._append_tool_result(
                call,
                {
                    "error": (
                        "The tool-call limit for this request is spent. Call "
                        "finish and say what is left."
                    )
                },
            )
            return
        self.run.setdefault("usage", {})["tool_calls"] = spent + 1
        self.save()
        self.set_activity(
            "loop.tool",
            loop_tools.describe_tool_call(name, args),
            task_id="loop",
        )
        try:
            result = self._run_tool(name, args)
        except (Cancelled, LimitExceeded):
            raise
        except Exception as error:
            # Every other failure is an observation the loop is meant to read.
            # A tool that cannot answer does not end the request; a loop that
            # cannot make progress will say so through ``finish``.
            result = {"error": str(error)}
        self._append_tool_result(call, result)

    def _run_tool(self, name: str, args: dict) -> dict:
        if name in self._read_names:
            return self.reads.dispatch(name, args)
        return loop_tools.dispatch(self.tools, name, args)

    def _append_tool_result(self, call: dict, result: object) -> None:
        self._append(
            {
                "role": "tool",
                "tool_call_id": str(call.get("id") or f"loop-tool-{len(self.conversation)}"),
                "content": loop_tools.json_result(result, MAX_TOOL_RESULT_CHARS),
            }
        )

    # -- what the tools call back into -------------------------------------- #
    def child_run(self, command: dict, context: dict | None = None) -> dict:
        """Run one child command run in process and return its finished record."""

        from . import runner

        self.checkpoint()
        started = time.monotonic()
        try:
            child = runner.run_child_run(self.ws, self.run, command, context)
        finally:
            # The loop's deadline is about the loop. A child carries its own,
            # and waiting for it is not the loop deliberating — charging that
            # time twice would end a request for taking as long as the work it
            # asked for took.
            self.runtime.deadline = self.runtime.deadline + (time.monotonic() - started)
        # ``run_child_run`` appended the child id to the in-memory record; reload
        # the parent's own view of what it now owns before the next turn reads
        # it.
        self.run["children"] = list(
            dict.fromkeys([*(self.run.get("children") or []), child["id"]])
        )
        self.save()
        self.emit("child_run", {"run_id": child["id"], "status": child.get("status")})
        return child

    def ask_auditor(self, question: str, options: list[str]) -> str:
        """Put one question to the auditor and wait for the answer."""

        if not options:
            answer = self.wait_for_input(question)
            # The answer is on the record as a user message, and the tool result
            # already carries it. Delivering it again at the next turn as
            # unsolicited steering would ask the model to read it twice.
            self._mark_messages_delivered()
            return answer
        interaction = {
            "id": loop_tools.new_interaction_id(),
            "action_id": "agent:loop",
            "type": "clarification",
            "prompt": question,
            "options": [{"id": str(index), "label": label} for index, label in enumerate(options)],
            "payload": {"original_command": (self.run.get("command") or {}).get("text")},
            "policy_reason": "The answer changes what the agent does next.",
            "status": "pending",
            "response": None,
            "actor": None,
            "created_at": self.utcnow(),
            "resolved_at": None,
        }
        self.run.setdefault("interactions", []).append(interaction)
        self.save()
        self.emit("checkpoint_request", {"interaction": interaction})
        response = self.runtime.wait_for_interaction(interaction)
        answer = str(response.get("text") or response.get("option_id") or "").strip()
        if not answer:
            raise WorkspaceError("A clarification response is required.")
        self._mark_messages_delivered()
        return answer

    def action_execution(self):
        """The action executor bound to this run, created once.

        It shares the loop's runtime and state lock, so an approval it raises
        is an interaction on the loop's own record and both write the durable
        run under one lock.
        """

        from .action_execution import ActionExecution

        if self._action_execution is None:
            self._action_execution = ActionExecution(
                self.ws, self.run, self.handle, runtime=self.runtime
            )
        return self._action_execution

    def request_finish(self, summary: str, suggestions: list[dict]) -> None:
        self._finish = {"summary": summary, "suggestions": list(suggestions)}

    # -- resume and close --------------------------------------------------- #
    def _resume_children(self) -> None:
        """Finish a child left non-terminal by a crash, before deciding more."""

        from . import runner

        for run_id in list(self.run.get("children") or []):
            try:
                child = store.load_run(self.ws, run_id)
            except WorkspaceError:
                continue
            if child.get("status") in store.TERMINAL_STATUSES:
                continue
            self.checkpoint()
            runner.run_child_run_resume(self.ws, self.run, child)

    def _close_with_finish(self) -> None:
        finish = self._finish or {}
        suggestions = list(finish.get("suggestions") or [])
        if suggestions:
            self.run["suggestions"] = suggestions
        summary = str(finish.get("summary") or "").strip()
        account = self._committed_account()
        if account:
            # The model's prose, then the ledger's own account of what the runs
            # committed. The first live run's summary claimed a redraft that a
            # zero-unit stage had not performed; a reader could not tell,
            # because nothing beside the prose said otherwise. Now something
            # does, and it is not written by the thing being checked.
            self.run["committed_account"] = account["items"]
            summary = f"{summary}\n\n{account['text']}" if summary else account["text"]
        self._close(self._terminal_status(), note=summary)

    def _committed_account(self) -> dict | None:
        """What this request's runs actually committed, read from their records."""

        accounts = []
        for run_id in self.run.get("children") or []:
            try:
                accounts.append(loop_tools.run_account(store.load_run(self.ws, run_id)))
            except WorkspaceError:
                continue
        lines = loop_tools.account_sentences(accounts)
        if not lines:
            return None
        return {
            "items": accounts,
            "text": "What the runs actually did:\n" + "\n".join(f"- {line}" for line in lines),
        }

    def _terminal_status(self) -> str:
        """What this request amounted to, read from the runs it started."""

        statuses = []
        for run_id in self.run.get("children") or []:
            try:
                statuses.append(str(store.load_run(self.ws, run_id).get("status") or ""))
            except WorkspaceError:
                continue
        if any(status in {"failed", "completed_with_failures"} for status in statuses):
            return "completed_with_failures"
        if any(status in store.PARTIAL_STATUSES for status in statuses):
            return "completed_with_open_items"
        if any(status == "cancelled" for status in statuses):
            return "cancelled"
        return "completed"

    def _close(self, status: str, *, note: str = "") -> None:
        """Say the closing turn, then publish the terminal status."""

        if self.run.get("status") in store.TERMINAL_STATUSES:
            return
        text = str(note or "").strip() or narration.closing_text(self.run, status)
        narration.say(self.run, self.emit, text)
        self.run["summary_markdown"] = self.run.get("summary_markdown") or text
        command = self.run.get("command")
        if isinstance(command, dict):
            command["status"] = status
        self.mark_finished()
        self.set_status(status)
        self._persist()


def _tool_arguments(raw: object) -> dict:
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(args) if isinstance(args, dict) else {}


__all__ = ["AgentLoop", "ConversationStore", "bounded_conversation"]
