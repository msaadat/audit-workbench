"""The steering loop's own tools: plan, run, inspect, rerun, ask, finish.

Every tool here is a *gate*, not a capability. The loop decides what it wants;
this module decides whether that is a thing the framework will do, and then
hands the doing to the machinery that already owns it — the capability
registry's dependency closure, the workflow scheduler, the unit pipeline, the
approval interactions. Nothing here writes an artifact, and nothing here
reaches a provider.

The three rules the guards exist to keep, none of which is left to the prompt:

* A prerequisite cannot be skipped. ``run_outcomes`` materializes through the
  registry, so a request for an outcome whose dependencies are unsatisfied
  either schedules them or reports them blocked.
* Whole-workspace regeneration stays an explicit human ask. ``force`` over the
  whole workspace is refused unless the auditor's own words asked for it.
* A unit gets one second chance. ``rerun_units`` refuses a unit this loop has
  already rerun, so a failure the model cannot read its way out of stops rather
  than spending the child budget on the same rejection.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .. import planning_delta
from ..workspaces import Workspace, WorkspaceError
from . import actions as action_catalog
from . import capabilities as audit_capabilities
from . import ledger, narration, routing, store, workflow
from .runtime.unit_pipeline import UnitSidecarStore, UnitSidecarValidationError

#: How much of a rejected response the loop is shown. Enough to see the shape
#: of what went wrong, not enough to re-read the whole draft.
MAX_RESPONSE_EXCERPT_CHARS = 600
MAX_VALIDATION_ERRORS = 8
MAX_VALIDATION_ERROR_CHARS = 2_000
#: A whole-workspace target. Named because it is what the force guard is about.
WORKSPACE_TARGET = "workspace:current"

#: Registered actions the loop is not offered. The two import actions belong to
#: the intake protocol runner, which owns the staged batch they read; the
#: procedure trio is the legacy shape the workflow's test capabilities replaced.
UNOFFERED_ACTIONS = frozenset(
    {
        "classify_import_batch",
        "apply_import_batch",
        "create_procedure",
        "edit_procedure",
        "delete_procedure",
        "generate_working_paper",
    }
)

_UNSETTLED_UNIT_STATUSES = frozenset(
    {"failed", "conflict", "blocked", "awaiting_input", "awaiting_confirmation"}
)
_SETTLED_UNIT_STATUSES = frozenset({"succeeded", "skipped"})
#: How many committed refs one stage contributes to an account before the rest
#: are counted rather than named.
MAX_ACCOUNT_REFS = 8


def run_account(child: dict) -> dict:
    """What a finished run actually did, read from its own record.

    The loop's own account of a run is a model's account, and a model that
    asked for three things will describe three things. This reads the ledger
    instead: which capabilities committed units and what those units produced,
    which ran with nothing to do, and what never settled. It is what the
    auditor is told at the end, and — supplied back through ``inspect_run`` and
    ``run_outcomes`` — what the loop has to reconcile its story against.

    A stage with no units is the interesting case and the reason this exists.
    It is not a failure and reads as a success in every status projection: the
    capability had nothing it considered doing, which is exactly the shape of
    "the redraft you asked for did not happen".
    """

    committed: list[dict] = []
    nothing_to_do: list[dict] = []
    for stage in (child.get("workflow") or {}).get("stages") or []:
        units = list(stage.get("units") or [])
        entry = {
            "capability": stage.get("capability"),
            "title": stage.get("title"),
            "status": stage.get("status"),
        }
        if not units:
            nothing_to_do.append(
                {**entry, "reasons": list((stage.get("readiness_before") or {}).get("reasons") or [])}
            )
            continue
        # Skipped is settled but committed nothing, and counting it as work
        # done is the same lie this account exists to prevent.
        done = [unit for unit in units if unit.get("status") == "succeeded"]
        skipped = [unit for unit in units if unit.get("status") == "skipped"]
        if not done:
            if skipped:
                nothing_to_do.append({**entry, "reasons": [f"{len(skipped)} skipped"]})
            continue
        refs = list(
            dict.fromkeys(
                str(ref)
                for unit in done
                for ref in unit.get("result_refs") or []
            )
        )
        committed.append(
            {
                **entry,
                "units": len(done),
                "of": len(units),
                "skipped": len(skipped),
                "refs": refs[:MAX_ACCOUNT_REFS],
                "more_refs": max(0, len(refs) - MAX_ACCOUNT_REFS),
            }
        )
    return {
        "run_id": child.get("id"),
        "committed": committed,
        "nothing_to_do": nothing_to_do,
    }


def account_sentences(accounts: list[dict]) -> list[str]:
    """The deterministic half of a closing message, one line per fact."""

    lines: list[str] = []
    for account in accounts:
        for item in account["committed"]:
            title = str(item.get("title") or narration.humanize(item.get("capability")))
            count = int(item.get("units") or 0)
            lines.append(
                f"{title}: {count} item{'' if count == 1 else 's'} committed"
                + (f" ({', '.join(item['refs'][:3])})" if item.get("refs") else "")
                + "."
            )
        for item in account["nothing_to_do"]:
            title = str(item.get("title") or narration.humanize(item.get("capability")))
            reasons = "; ".join(item.get("reasons") or [])
            lines.append(
                f"{title}: nothing to do, so nothing changed"
                + (f" ({reasons})" if reasons else "")
                + "."
            )
    return lines


class ToolError(WorkspaceError):
    """A tool refusal the loop is meant to read and act on, not die of."""


def tool_schemas() -> list[dict]:
    """Wire schemas for the loop's own tools, in a stable order."""

    outcomes = sorted(routing.supported_outcomes())
    outcome_array = {
        "type": "array",
        "items": {"type": "string", "enum": outcomes},
        "description": "Registered workflow outcome ids, all from one workflow.",
    }
    target_array = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "What to work on, as typed refs: rcm:<id>, doctest:<id>, "
            "datatest:<id>, observation:<id>, finding:<id>, document:<id>, "
            "table:<name>. Omit for the whole workspace."
        ),
    }
    generation_mode = {
        "type": "string",
        "enum": list(store.GENERATION_MODES),
        "description": (
            "reuse_existing keeps work that is already committed; force redoes "
            "it. Name targets whenever you force."
        ),
    }
    return [
        _function(
            "plan_outcomes",
            "Show what running these outcomes would schedule: the stages, how "
            "many units each would expand into, what is already done and would "
            "be reused, and what is blocked and why. Reads only; starts "
            "nothing. Call this before the first run_outcomes.",
            {
                "type": "object",
                "properties": {
                    "requested_outcomes": outcome_array,
                    "target_refs": target_array,
                    "generation_mode": generation_mode,
                },
                "required": ["requested_outcomes"],
            },
        ),
        _function(
            "run_outcomes",
            "Run these outcomes as a durable child run and wait for it. The "
            "registry decides what actually executes: prerequisites are "
            "scheduled, satisfied work is reused. Returns the finished run's "
            "status, stages, and any failed or blocked units.",
            {
                "type": "object",
                "properties": {
                    "requested_outcomes": outcome_array,
                    "target_refs": target_array,
                    "generation_mode": generation_mode,
                    "instruction": {
                        "type": "string",
                        "description": (
                            "What the auditor wants different this time, in "
                            "one or two plain sentences. The workers read it "
                            "as declared context."
                        ),
                    },
                    "review_each_stage": {
                        "type": "boolean",
                        "description": (
                            "Ask the auditor before each stage runs. Only has "
                            "an effect in permission mode."
                        ),
                    },
                },
                "required": ["requested_outcomes"],
            },
        ),
        _function(
            "inspect_run",
            "Read a finished child run: its status, its stages, and for every "
            "unit that failed or is blocked, the validator's own errors and an "
            "excerpt of the rejected response.",
            {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "unit_id": {
                        "type": "string",
                        "description": "Limit the detail to one unit.",
                    },
                },
                "required": ["run_id"],
            },
        ),
        _function(
            "rerun_units",
            "Run named units of a finished child run again, once, with an "
            "instruction that says what to do differently. Use after "
            "inspect_run, and restate the validator's errors in plain terms.",
            {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "unit_ids": {"type": "array", "items": {"type": "string"}},
                    "instruction": {"type": "string"},
                },
                "required": ["run_id", "unit_ids"],
            },
        ),
        _function(
            "assess_change",
            "Decide what newly supplied documents change for the audit plan. "
            "Reads the documents' analyses against the current memorandum and "
            "matrix and returns an impact ('none', 'apm', 'rcm' or 'both'), a "
            "summary, and where each change belongs. Changes nothing: revising "
            "is a separate run you start after reading this.",
            {
                "type": "object",
                "properties": {
                    "document_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The documents to assess, by id.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "What the auditor asked you to look for, if anything.",
                    },
                },
                "required": ["document_ids"],
            },
        ),
        _function(
            "ask_auditor",
            "Ask the auditor one question and wait for the answer. Use only "
            "when the answer changes what you would do next.",
            {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Offer choices when the answer is one of a few.",
                    },
                },
                "required": ["question"],
            },
        ),
        _function(
            "finish",
            "End the request. The summary is what the auditor reads: name what "
            "was produced, what was left, and why.",
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "suggestions": {
                        "type": "array",
                        "description": "Up to four next steps, as clickable offers.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "requested_outcomes": {
                                    "type": "array",
                                    "items": {"type": "string", "enum": outcomes},
                                },
                                "target_refs": {"type": "array", "items": {"type": "string"}},
                                "message": {"type": "string"},
                            },
                            "required": ["label"],
                        },
                    },
                },
                "required": ["summary"],
            },
        ),
    ]


def _function(name: str, description: str, parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def action_tools() -> list[dict]:
    """One tool per registered action, named and shaped by its definition.

    The catalog is the contract. A tool here takes the action's own declared
    argument schema plus the artifact it applies to, and nothing about it is
    written twice: adding an action to the registry offers it to the loop, and
    the risk, approval rule, reconciler and receipt it declared all still apply
    when the loop calls it.

    This is what replaced the action engine. The interpreter used to write a
    whole DAG of these from the command text before anything ran; the loop
    calls them one at a time, having read the workspace, and reads each result
    before deciding the next.
    """

    schemas = []
    for definition in action_catalog.REGISTRY.all():
        if definition.type in UNOFFERED_ACTIONS:
            continue
        properties = {
            "args": {
                **definition.input_schema,
                "description": f"Arguments for {definition.type}.",
            }
        }
        required = ["args"]
        if definition.target_kinds:
            properties["target"] = {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(definition.target_kinds)},
                    "id": {
                        "type": "string",
                        "description": "The artifact's bare id, for example RCM-123.",
                    },
                },
                "required": ["kind", "id"],
                "description": "Which existing artifact this applies to.",
            }
            required.append("target")
        schemas.append(
            _function(
                definition.type,
                f"{definition.description} ({definition.risk} action)",
                {"type": "object", "properties": properties, "required": required},
            )
        )
    return schemas


def action_tool_names() -> set[str]:
    return {
        str(schema["function"]["name"]) for schema in action_tools()
    }


TOOL_LABELS = {
    "plan_outcomes": "Working out what this would run",
    "run_outcomes": "Running audit work",
    "inspect_run": "Reading what happened",
    "rerun_units": "Trying the failed work again",
    "assess_change": "Working out what the new evidence changes",
    "ask_auditor": "Asking you a question",
    "finish": "Wrapping up",
}

LOOP_TOOL_NAMES = tuple(TOOL_LABELS)


class LoopTools:
    """Dispatch for the loop's own tools against one live loop run."""

    def __init__(self, loop: Any):
        self.loop = loop

    # -- helpers ----------------------------------------------------------- #
    @property
    def ws(self) -> Workspace:
        return self.loop.ws

    @property
    def run(self) -> dict:
        return self.loop.run

    def _limit(self, key: str) -> int:
        return int((self.run.get("limits") or {}).get(key) or 0)

    def _used(self, key: str) -> int:
        return int((self.run.get("usage") or {}).get(key) or 0)

    def _charge(self, key: str) -> None:
        usage = self.run.setdefault("usage", {})
        usage[key] = int(usage.get(key) or 0) + 1
        self.loop.save()

    def _readable_run(self, run_id: str) -> dict:
        """Load any command run in this workspace, for reading.

        Reading a run is a read like any other, and a request to review one the
        auditor names — "review run X" — is exactly the case the loop exists
        for. Changing one is narrower: see :meth:`_writable_run`.
        """

        wanted = str(run_id or "").strip()
        if not wanted:
            raise ToolError("Name the run to inspect.")
        try:
            return store.load_run(self.ws, wanted)
        except WorkspaceError as error:
            raise ToolError(str(error)) from error

    def _writable_run(self, run_id: str) -> dict:
        """Load a run this loop may run part of again.

        Its own children, or a run it was asked to review and has inspected.
        Anything else is somebody else's work, reachable by reading only.
        """

        child = self._readable_run(run_id)
        own = child["id"] in (self.run.get("children") or []) or child["id"] == self.run["id"]
        reviewed = str(child.get("reviewed_by_run_id") or "") == self.run["id"]
        if not own and not reviewed:
            raise ToolError(
                f"Run '{child['id']}' is not one of this request's runs. Inspect "
                "it first if the auditor asked you to review it."
            )
        return child

    # -- tools ------------------------------------------------------------- #
    def plan_outcomes(self, args: dict) -> dict:
        outcomes = _string_list(args.get("requested_outcomes"))
        if not outcomes:
            raise ToolError("Name at least one registered outcome to plan.")
        target_refs = _string_list(args.get("target_refs")) or [WORKSPACE_TARGET]
        mode = _generation_mode(args.get("generation_mode"))
        definition_id = routing.validate_requested_outcomes(outcomes)
        _, scope = routing.resolution_scope(
            self.ws,
            self.run,
            {
                "workflow_definition": definition_id,
                "requested_outcomes": outcomes,
                "target_refs": target_refs,
                "generation_mode": mode,
            },
        )
        registry = audit_capabilities.REGISTRY_BY_WORKFLOW[definition_id]
        resolved, stages, reused = workflow.materialize(
            registry, self.ws, outcomes, scope, generation_mode=mode
        )
        blocked = []
        for stage in stages:
            readiness = stage.get("readiness_before") or {}
            if str(readiness.get("state") or "") != "blocked":
                continue
            blocked.append(
                {
                    "capability": stage.get("capability"),
                    "reasons": list(readiness.get("reasons") or []),
                    "blocking_on": list(readiness.get("blocking_on") or []),
                }
            )
        return {
            "definition": definition_id,
            "stages": [
                {
                    "capability": stage.get("capability"),
                    "title": stage.get("title"),
                    "units": len(stage.get("units") or []),
                    "readiness_before": stage.get("readiness_before") or {},
                }
                for stage in stages
            ],
            "resolved": list(resolved),
            "reused": list(reused),
            "blocked": blocked,
            # A stage that expands no units will run and change nothing. It is
            # not an error and it will report success, so it is named here
            # rather than left to be inferred from a zero.
            "will_do_nothing": [
                stage.get("capability")
                for stage in stages
                if not (stage.get("units") or [])
            ],
            # One model turn per unit is the shape of every generation stage;
            # a stage that spends more says so through its own budget, and the
            # loop only needs the order of magnitude before it commits.
            "estimated_model_turns": sum(len(stage.get("units") or []) for stage in stages),
        }

    def run_outcomes(self, args: dict) -> dict:
        outcomes = _string_list(args.get("requested_outcomes"))
        if not outcomes:
            raise ToolError("Name at least one registered outcome to run.")
        target_refs = _string_list(args.get("target_refs")) or [WORKSPACE_TARGET]
        mode = _generation_mode(args.get("generation_mode"))
        instruction = str(args.get("instruction") or "").strip()
        definition_id = routing.validate_requested_outcomes(outcomes)
        self._guard_force(mode, target_refs)
        self._guard_child_budget()
        command = {
            "source": "follow_up",
            "text": _child_objective(outcomes, target_refs, instruction),
            "requested_outcomes": outcomes,
            "target_refs": target_refs,
            "generation_mode": mode,
        }
        context = {"instruction": instruction} if instruction else {}
        if bool(args.get("review_each_stage")):
            context["review_each_stage"] = True
        child = self.loop.child_run(command, context)
        return {
            "definition": definition_id,
            **self._run_report(child),
        }

    def inspect_run(self, args: dict) -> dict:
        child = self._readable_run(str(args.get("run_id") or ""))
        only = str(args.get("unit_id") or "").strip() or None
        report = self._run_report(child, unit_id=only)
        self._stamp_reviewed(child)
        return report

    def _stamp_reviewed(self, child: dict) -> None:
        """Record that this request has read that run.

        It is what retires the chat's "review this run" offer, and what lets
        this loop rerun units of a run it did not start.
        """

        if child["id"] == self.run["id"]:
            return
        if str(child.get("reviewed_by_run_id") or "") == self.run["id"]:
            return
        child["reviewed_by_run_id"] = self.run["id"]
        store.save_run(self.ws, child)

    def rerun_units(self, args: dict) -> dict:
        from . import runner

        child = self._writable_run(str(args.get("run_id") or ""))
        wanted = _string_list(args.get("unit_ids"))
        if not wanted:
            raise ToolError("Name the units to run again.")
        instruction = str(args.get("instruction") or "").strip()
        units = {
            unit["id"]: unit
            for stage in (child.get("workflow") or {}).get("stages") or []
            for unit in stage.get("units") or []
        }
        unknown = [unit_id for unit_id in wanted if unit_id not in units]
        if unknown:
            raise ToolError(
                f"Run '{child['id']}' has no unit '{unknown[0]}'. Use the unit "
                "ids inspect_run returned."
            )
        already = [
            unit_id
            for unit_id in wanted
            if any(
                item.get("unit_id") == unit_id
                for item in self.run.get("repairs") or []
            )
        ]
        if already:
            raise ToolError(
                f"Unit '{already[0]}' has already been run again once in this "
                "request. Report what is still wrong instead of trying a third "
                "time."
            )
        self._guard_child_budget()
        refs = list(
            dict.fromkeys(
                ref
                for unit_id in wanted
                for ref in units[unit_id].get("parent_refs") or []
            )
        )
        if not refs:
            raise ToolError(
                "Those units name nothing to narrow the rerun to; run the "
                "outcome again with explicit targets instead."
            )
        command, context = runner.linked_retry_command(
            child, target_refs=refs, instruction=instruction
        )
        repairs = self.run.setdefault("repairs", [])
        for unit_id in wanted:
            repairs.append(
                {"unit_id": unit_id, "from_run_id": child["id"], "at": store.utcnow()}
            )
        self.loop.save()
        rerun = self.loop.child_run(command, context)
        return {"rerun_of": child["id"], "units": wanted, **self._run_report(rerun)}

    def run_registered_action(self, name: str, args: dict) -> dict:
        """Append one registered action to this run's ledger and drive it.

        Everything an action was subject to under the action engine still
        applies — target resolution, the optimistic precondition, the approval
        interaction in permission mode, the executor's own receipt, the
        reconciler, undo — because this drives the same code. What is gone is
        the model-written DAG in front of it.
        """

        target = args.get("target") if isinstance(args.get("target"), dict) else {}
        proposal = {
            "type": name,
            "args": dict(args.get("args") or {}),
            "target": {
                "kind": str(target.get("kind") or "") or None,
                "resolved_id": str(target.get("id") or target.get("resolved_id") or "") or None,
                "selector": target.get("selector"),
            },
        }
        execution = self.loop.action_execution()
        try:
            created = ledger.append_actions(self.run, [proposal])
        except WorkspaceError as error:
            raise ToolError(str(error)) from error
        action = created[0]
        action_catalog.canonicalize_action_fields(self.ws, action)
        self.loop.save()
        execution.drive_actions()
        settled = next(
            (item for item in self.run.get("actions") or [] if item["id"] == action["id"]),
            action,
        )
        return {
            "action_id": settled["id"],
            "type": settled["type"],
            "status": settled["status"],
            "error": settled.get("error"),
            "result_refs": list(settled.get("result_refs") or []),
            "receipt": settled.get("receipt"),
        }

    def assess_change(self, args: dict) -> dict:
        """Ask what new evidence changes, without changing anything.

        The request this whole plan was written around — "I uploaded document
        XX, revise the APM and RCM as appropriate" — split into the two
        decisions it actually contains. This is the first: does anything need to
        change, and where. The second, revising, is a run the loop starts after
        reading the answer, so the judgment and the rewrite are separately
        reviewable and the rewrite can be declined.
        """

        document_ids = _string_list(args.get("document_ids"))
        if not document_ids:
            raise ToolError("Name the documents to assess.")
        instruction = str(args.get("instruction") or "").strip()
        self._guard_child_budget()
        command = {
            "source": "follow_up",
            "text": f"Assess what {len(document_ids)} new document(s) change",
            "requested_outcomes": ["planning.change_assessed"],
            "target_refs": [f"document:{value}" for value in document_ids],
            "generation_mode": "reuse_existing",
        }
        context = {"instruction": instruction} if instruction else {}
        child = self.loop.child_run(command, context)
        report = self._run_report(child)
        assessment = planning_delta.load(
            self.ws.reload(),
            planning_delta.basis_sha1(self.ws.reload(), document_ids),
        )
        if assessment is None:
            return {**report, "assessment": None}
        return {
            **report,
            "assessment": {
                key: assessment.get(key)
                for key in ("impact", "summary", "apm_changes", "rcm_changes")
            },
            # Naming the next move rather than leaving it to be inferred: the
            # matrix reads the memorandum, so a plan that revises both revises
            # them in that order.
            "revise_next": {
                "none": [],
                "apm": ["planning.apm_ready"],
                "rcm": ["planning.rcm_ready"],
                "both": ["planning.apm_ready", "planning.rcm_ready"],
            }[str(assessment.get("impact") or "none")],
        }

    def ask_auditor(self, args: dict) -> dict:
        question = str(args.get("question") or "").strip()
        if not question:
            raise ToolError("Ask something specific.")
        options = [value for value in _string_list(args.get("options")) if value]
        asked = self._used("auditor_questions")
        allowed = self._limit("max_auditor_questions")
        if allowed and asked >= allowed:
            raise ToolError(
                f"You have already asked {asked} questions for this request. "
                "Decide with what you have, or finish and say what you need."
            )
        self._charge("auditor_questions")
        answer = self.loop.ask_auditor(question, options)
        return {"question": question, "answer": answer}

    def finish(self, args: dict) -> dict:
        summary = str(args.get("summary") or "").strip()
        if not summary:
            raise ToolError("Say what was produced and what was left.")
        open_children = [
            run_id
            for run_id in self.run.get("children") or []
            if store.load_run(self.ws, run_id).get("status")
            not in store.TERMINAL_STATUSES
        ]
        if open_children:
            raise ToolError(
                f"Child run '{open_children[0]}' has not finished. Wait for it "
                "before finishing."
            )
        suggestions = _suggestions(args.get("suggestions"))
        self.loop.request_finish(summary, suggestions)
        return {"finished": True, "summary": summary, "suggestions": suggestions}

    # -- guards ------------------------------------------------------------ #
    def _guard_force(self, mode: str, target_refs: list[str]) -> None:
        """Whole-workspace regeneration stays an explicit human ask."""

        if mode != "force":
            return
        whole = not target_refs or WORKSPACE_TARGET in target_refs
        if not whole:
            return
        if (self.run.get("context") or {}).get("force_confirmed"):
            return
        raise ToolError(
            "Regenerating the whole workspace is not yours to decide. Name the "
            "rows, tests or findings to redo, or ask the auditor to confirm "
            "they want everything redone."
        )

    def _guard_child_budget(self) -> None:
        allowed = self._limit("max_child_runs")
        started = len(self.run.get("children") or [])
        if allowed and started >= allowed:
            raise ToolError(
                f"This request has already started {started} runs, which is its "
                "limit. Finish and report what is left."
            )

    # -- reporting --------------------------------------------------------- #
    def _run_report(self, child: dict, *, unit_id: str | None = None) -> dict:
        state = child.get("workflow") or {}
        stages = []
        details = []
        for stage in state.get("stages") or []:
            counts = workflow.stage_counts(stage)
            stages.append(
                {
                    "capability": stage.get("capability"),
                    "title": stage.get("title"),
                    "status": stage.get("status"),
                    "counts": counts,
                }
            )
            for unit in stage.get("units") or []:
                if unit.get("status") not in _UNSETTLED_UNIT_STATUSES:
                    continue
                if unit_id and unit.get("id") != unit_id:
                    continue
                details.append(self._unit_detail(child, stage, unit))
        account = run_account(child)
        return {
            "run_id": child["id"],
            "engine": child.get("engine"),
            "status": child.get("status"),
            # What the run actually committed, and which stages ran with
            # nothing to do. Say nothing in a summary that this contradicts.
            "committed": account["committed"],
            "nothing_to_do": account["nothing_to_do"],
            "error": child.get("error"),
            "requested_outcomes": list(state.get("requested_outcomes") or []),
            "target_refs": list(state.get("target_refs") or []),
            "next_outcomes": list(state.get("next_outcomes") or []),
            "stages": stages,
            "unsettled_units": details,
            "blockers": [
                {
                    "message": item.get("message"),
                    "severity": item.get("severity"),
                    "unit_ids": item.get("unit_ids") or [item.get("unit_id")],
                }
                for item in narration.blockers(child)
            ],
        }

    def _unit_detail(self, child: dict, stage: dict, unit: dict) -> dict:
        detail = {
            "unit_id": unit.get("id"),
            "capability": stage.get("capability"),
            "title": unit.get("title"),
            "status": unit.get("status"),
            "error": unit.get("error"),
            "parent_refs": list(unit.get("parent_refs") or []),
        }
        rejection = self._rejection(child["id"], str(unit.get("id") or ""))
        if rejection is None:
            return detail
        errors = [str(value) for value in rejection.get("validation_errors") or []]
        detail["validation_errors"] = _clip_list(
            errors, MAX_VALIDATION_ERRORS, MAX_VALIDATION_ERROR_CHARS
        )
        response = str(rejection.get("response") or "")
        if response:
            detail["response_excerpt"] = response[:MAX_RESPONSE_EXCERPT_CHARS]
        return detail

    def _rejection(self, run_id: str, unit_id: str) -> dict | None:
        if not unit_id:
            return None
        try:
            payload = UnitSidecarStore(self.ws, run_id).load_rejection(unit_id)
        except (WorkspaceError, UnitSidecarValidationError):
            return None
        return payload if isinstance(payload, dict) else None


def dispatch(tools: LoopTools, name: str, args: dict) -> dict:
    """Run one loop tool by name, or raise :class:`ToolError` for an unknown."""

    if name in LOOP_TOOL_NAMES:
        return getattr(tools, name)(args)
    if name in action_tool_names():
        return tools.run_registered_action(name, args)
    raise ToolError(f"Unknown tool '{name}'.")


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _generation_mode(value: object) -> str:
    mode = str(value or "reuse_existing").strip() or "reuse_existing"
    if mode not in store.GENERATION_MODES:
        raise ToolError(
            "generation_mode must be 'reuse_existing' or 'force'."
        )
    return mode


def _child_objective(
    outcomes: list[str], target_refs: list[str], instruction: str
) -> str:
    """A one-line command text for the child run's own card."""

    titles = ", ".join(narration.humanize(item) for item in outcomes)
    scope = (
        ""
        if target_refs == [WORKSPACE_TARGET]
        else f" for {', '.join(target_refs[:4])}"
    )
    text = f"{titles}{scope}"
    return f"{text}: {instruction}" if instruction else text


def _clip_list(values: list[str], count: int, characters: int) -> list[str]:
    kept: list[str] = []
    spent = 0
    for value in values[:count]:
        if spent >= characters:
            break
        remaining = characters - spent
        kept.append(value[:remaining])
        spent += min(len(value), remaining)
    return kept


def _suggestions(value: object) -> list[dict]:
    """Validate the closing turn's offers; a bad one is dropped, not fatal."""

    if not isinstance(value, (list, tuple)):
        return []
    supported = routing.supported_outcomes()
    kept: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        outcomes = [
            outcome
            for outcome in _string_list(item.get("requested_outcomes"))
            if outcome in supported
        ]
        message = str(item.get("message") or "").strip()
        if outcomes:
            kept.append(
                {
                    "label": label,
                    "requested_outcomes": outcomes,
                    "target_refs": _string_list(item.get("target_refs")),
                }
            )
        elif message:
            kept.append({"label": label, "message": message})
        if len(kept) == 4:
            break
    return kept


def describe_tool_call(name: str, args: dict) -> str:
    """A short, safe activity label for one tool call."""

    label = TOOL_LABELS.get(name)
    if label is None:
        from ..assistant_tools import TOOL_LABELS as READ_LABELS

        label = READ_LABELS.get(name)
    if label is None:
        try:
            label = action_catalog.REGISTRY.get(name).description
        except WorkspaceError:
            label = "Working"
    outcomes = _string_list(args.get("requested_outcomes"))
    if outcomes:
        return f"{label}: {', '.join(narration.humanize(item) for item in outcomes[:3])}"
    return label


def new_interaction_id() -> str:
    return f"int_{uuid.uuid4().hex[:12]}"


def json_result(payload: object, limit: int) -> str:
    """Serialize one tool result for the conversation, bounded."""

    text = json.dumps(payload, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + '… [truncated]"'


__all__ = [
    "LOOP_TOOL_NAMES",
    "action_tools",
    "LoopTools",
    "ToolError",
    "describe_tool_call",
    "dispatch",
    "json_result",
    "new_interaction_id",
    "tool_schemas",
]
