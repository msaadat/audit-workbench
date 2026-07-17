"""Durable unified command/action-graph runner."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import assistant, documents, llm, methodology, templates_store
from ..workspaces import Workspace, WorkspaceError, slugify
from . import actions, artifact_index, ledger, prompts, store
from .base import BaseRunner, Cancelled, LimitExceeded

GOAL_TEMPLATES = {
    "full_audit_working_draft": {
        "objective": "Prepare complete audit working content through an evidence-linked report draft.",
        "constraints": ["Do not assert a formal audit opinion.", "Preserve auditor edits."],
    },
    "planning": {"objective": "Prepare or improve engagement planning, RCM, and audit procedures."},
    "apm_only": {"objective": "Prepare or revise only the audit planning memorandum."},
    "data_analysis": {"objective": "Analyze available structured data and preserve useful validated work."},
    "document_testing": {"objective": "Prepare and execute relevant document tests and working papers."},
    "report": {"objective": "Prepare evidence-linked audit report working content and run quality checks."},
}

MAX_SOURCE_DOCUMENTS = 8
MAX_PAGES_PER_SOURCE_DOCUMENT = 50
ELIGIBLE_TEXT_STATES = ("extracted", "partial")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def fill_unavailable_placeholders(markdown: str) -> str:
    def replace(match: re.Match) -> str:
        label = match.group(1).replace("_", " ").strip()
        return f"_[{label} - context not available]_"

    return _PLACEHOLDER.sub(replace, markdown)

SEMANTIC_PROPOSAL_ATTEMPTS = 2


def _text_list(value: object) -> list[str]:
    values = [value] if isinstance(value, str) else list(value or [])
    return [str(item) for item in values]


class CommandRunner(BaseRunner):
    stage_titles = {
        "context": "Planning context",
        "apm": "Audit planning memorandum",
        "rcm": "Risk and control matrix",
        "work_program": "Audit program",
        "verify": "Traceability",
        "summary": "Planning summary",
    }
    PLANNING_ACTION_TYPES = {
        "update_planning_context", "generate_apm", "edit_apm",
        "create_rcm_row", "edit_rcm_row", "create_procedure", "edit_procedure",
    }

    def _drain_inbox(self) -> None:
        """General chat is durable follow-up work, never active-graph steering."""
        with self.handle.lock:
            queued, self.handle.command_queue = self.handle.command_queue[:], []
        self.handle.command_queued.clear()
        if queued:
            self.run.setdefault("pending_commands", []).extend(queued)
            self.save()

    def execute(self) -> None:
        if not self.run.get("started"):
            self.run["started"] = store.utcnow()
            self.save()
        try:
            self._recover_running_actions()
            if not self.run.get("actions"):
                if self._requests_enriched_planning():
                    self._prepare_planning()
                    if self._is_planning_command():
                        self._finish_planning()
                        return
                self._interpret()
            self._drive_graph()
            self._finish()
        except Cancelled:
            for action in self.run.get("actions") or []:
                if action["status"] in {"proposed", "ready", "awaiting_input", "awaiting_confirmation"}:
                    ledger.transition(action, "cancelled")
            ledger.project_legacy_plan(self.run)
            self.run["finished"] = store.utcnow()
            self.set_status("cancelled")
        except (LimitExceeded, llm.LLMError) as error:
            if self._is_planning_command() or (
                self._requests_full_audit() and not self.run.get("prepared_planning")
            ):
                self._fail_run(str(error))
            else:
                self._fail_running_plan_tasks(str(error))
                self.warn(str(error))
                self._finish(force_issue=True)
        except Exception as error:
            self._fail_run(str(error))

    def _fail_run(self, error: str) -> None:
        self._fail_running_plan_tasks(error)
        self.run["error"] = error
        self.run["finished"] = store.utcnow()
        self.run["command"]["status"] = "failed"
        self.set_status("failed")

    def _fail_running_plan_tasks(self, error: str) -> None:
        """Close embedded planning tasks when a command run ends abruptly.

        Full-audit commands prepare their APM, RCM, and audit program before
        the action graph is interpreted.  Those tasks live in the legacy plan
        projection, so a transport error would otherwise leave the current
        planning task permanently displayed as running after the run failed.
        """
        for stage in self.run.get("plan", {}).get("stages", []):
            for task in stage.get("tasks", []):
                if task.get("status") == "running":
                    self.task_status(task, "failed", error)

    def _catalog(self) -> list[dict]:
        return [
            {
                "type": value.type, "version": value.version,
                "description": value.description, "input_schema": value.input_schema,
                "target_kinds": list(value.target_kinds), "risk": value.risk,
                "model_usage": value.model_usage,
            }
            for value in actions.REGISTRY.all()
        ]

    def _interpret(self) -> None:
        self.set_status("interpreting")
        index = artifact_index.build(self.ws)
        command = self.run["command"]
        template = GOAL_TEMPLATES.get(command.get("goal_template"))
        if command.get("goal_template") and template is None:
            raise WorkspaceError("Unknown goal template.")
        base_user = prompts.command_interpreter_user(
            command, template, artifact_index.compact(index), self._catalog(), self.run["limits"],
            assistant.schema_brief(self.ws),
            prepared_planning=self.run.get("prepared_planning"),
        )
        attempt_user = base_user
        created = []
        payload = {}
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            payload = self.llm_json(prompts.COMMAND_INTERPRETER_SYSTEM, attempt_user)
            objective = str(payload.get("objective") or (template or {}).get("objective") or command.get("text") or "").strip()
            goal = {
                "objective": objective,
                "constraints": _text_list(
                    payload.get("constraints") or (template or {}).get("constraints") or []
                ),
                "completion_criteria": _text_list(payload.get("completion_criteria")),
            }
            self.run["goal"] = goal
            proposals = payload.get("actions") or []
            if self.run.get("prepared_planning") and isinstance(proposals, list):
                proposals = self._remove_redundant_planning_actions(proposals)
            try:
                if not isinstance(proposals, list):
                    raise WorkspaceError("Command interpreter actions must be a list.")
                for proposal in proposals:
                    if isinstance(proposal, dict):
                        actions.canonicalize_action_fields(self.ws, proposal)
                created = ledger.append_actions(
                    self.run, proposals, audit_lifecycle=self._is_full_audit_goal(goal)
                )
                break
            except WorkspaceError as error:
                self._record_rejected_proposals("command_interpreter", proposals, error)
                if attempt + 1 >= SEMANTIC_PROPOSAL_ATTEMPTS:
                    raise
                attempt_user = self._proposal_repair_user(base_user, payload, error)
        if payload.get("needs_planning_wave") and not any(item.get("planning_significant") for item in created):
            for item in reversed(created):
                definition = actions.REGISTRY.get(item["type"], item["definition_version"])
                if definition.risk in {"read", "compute"}:
                    item["planning_significant"] = True
                    break
        self.run["command"]["status"] = "planned"
        self.save()
        self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})

    def _requests_full_audit(self) -> bool:
        command = self.run.get("command") or {}
        if command.get("goal_template") == "full_audit_working_draft":
            return True
        text = str(command.get("text") or "").casefold()
        return any(phrase in text for phrase in (
            "full audit", "complete the audit", "complete audit", "entire audit",
            "end-to-end audit", "end to end audit",
        ))

    def _is_planning_command(self) -> bool:
        return (self.run.get("command") or {}).get("goal_template") == "planning"

    def _requests_enriched_planning(self) -> bool:
        return self._is_planning_command() or self._requests_full_audit()

    def _prepare_planning(self) -> None:
        """Prepare grounded planning before asking for downstream audit work."""
        if self.run.get("prepared_planning"):
            return
        self.set_status("executing")
        self.run.setdefault("context", {})["require_planning_quality"] = True
        basis = self.stage_context()
        apm = self.stage_apm(basis)
        rcm = self.stage_rcm(basis, apm)
        self.stage_work_program(basis, rcm)
        self.run["prepared_planning"] = {
            "apm": bool(str(self.ws.planning.get("apm_markdown") or "").strip()),
            "rcm_refs": [item["id"] for item in self.ws.rcm],
            "procedure_refs": [item["id"] for item in self.ws.work_program],
            "document_content_disclosed": bool(basis.get("document_content_disclosed")),
        }
        self.save()

    def stage_context(self) -> dict:
        task = self.add_task("context", "planning:context", "Assemble disclosed planning context")
        if task["status"] == "completed" and self.run.get("planning_basis"):
            return self.run["planning_basis"]
        if task["status"] != "completed":
            self.task_status(task, "running")
        tables = []
        for name in self.ws.table_names():
            try:
                tables.append(assistant.table_metadata(self.ws, name))
            except Exception as error:
                self.warn(f"Could not profile '{name}' for planning: {error}")
        requested_document_ids = [
            str(value) for value in (self.context.get("document_ids") or []) if str(value).strip()
        ][:MAX_SOURCE_DOCUMENTS]
        requested = set(requested_document_ids)
        missing = requested - {str(doc.get("id")) for doc in self.ws.documents}
        if missing:
            raise WorkspaceError(f"Planning source document not found: {sorted(missing)[0]}.")
        context = self.ws.planning.get("context") or {}
        if not requested_document_ids and self.ws.settings.get("doc_llm_optin"):
            requested_document_ids = self._select_planning_documents(task, context)
            requested = set(requested_document_ids)
        document_metadata = [
            {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "category": doc.get("category"),
                "pages": doc.get("pages"),
                "text_state": doc.get("text_state"),
                "selected_for_update": doc.get("id") in requested,
            }
            for doc in self.ws.documents
        ]
        methodology_query = " ".join(
            str(context.get(key) or "") for key in ("objective", "scope", "background_notes")
        ).strip() or "internal audit risk controls procedures"
        pack_results = methodology.search(self.ws, methodology_query, limit=5)
        methodology_context = []
        disclosed_documents = []
        disclosed_names: list[str] = []
        disclosed_packs: dict[str, dict] = {}
        if self.ws.settings.get("doc_llm_optin"):
            for document_id in requested_document_ids:
                doc = next(item for item in self.ws.documents if item.get("id") == document_id)
                disclosed_names.append(
                    Path(str(doc.get("source") or doc.get("title") or document_id)).name
                )
                page_count = max(1, int(doc.get("pages") or 1))
                disclosed = documents.disclosable_content(
                    self.ws,
                    document_id,
                    "planning_update",
                    self.run["id"],
                    list(range(1, min(page_count, MAX_PAGES_PER_SOURCE_DOCUMENT) + 1)),
                    mask_pii=bool(self.ws.settings.get("doc_pii_masking")),
                )
                disclosed_documents.append(
                    {
                        "id": document_id,
                        "title": doc.get("title"),
                        "category": doc.get("category"),
                        "source_sha1": disclosed.get("source_sha1"),
                        "pages": disclosed.get("pages") or [],
                    }
                )
            for result in pack_results:
                source_ref = f"pack:{result['scope']}:{result['pack_id']}"
                if source_ref not in disclosed_packs:
                    disclosed_packs[source_ref] = documents.disclosable_content(
                        self.ws, source_ref, "planning_methodology", self.run["id"], [1]
                    )
                content = disclosed_packs[source_ref]["pages"][0]["text"]
                excerpt = result["excerpt"] if result["excerpt"] in content else ""
                methodology_context.append({**result, "excerpt": excerpt})
        elif requested_document_ids:
            self.warn(
                "Document AI is off; planning can use imported document metadata but not document content."
            )
        basis = {
            "planning": self.ws.planning,
            "documents": document_metadata,
            "tables": tables,
            "document_content_disclosed": bool(disclosed_documents),
            "document_content": disclosed_documents,
            "methodology_available": [
                {key: pack.get(key) for key in ("id", "name", "scope", "version", "sha1")}
                for pack in methodology.list_packs(self.ws)
            ],
            "methodology": methodology_context,
        }
        if disclosed_documents:
            self.disclose(
                task,
                f"{', '.join(disclosed_names)} "
                f"({len(disclosed_names)} file{'s' if len(disclosed_names) != 1 else ''})",
            )
            payload = self.llm_json(
                prompts.DOCUMENT_CONTEXT_SYSTEM,
                prompts.document_context_user(context, disclosed_documents),
            )
            proposed_context = {
                key: value
                for key, value in dict(payload.get("context") or {}).items()
                if key
                in {
                    "objective", "entity", "period", "scope", "materiality",
                    "key_contacts", "background_notes",
                }
                and str(value or "").strip()
            }
            proposals = []
            if proposed_context:
                proposals.append(
                    self.proposal_item(
                        "Planning context from imported documents",
                        "Grounded facts extracted from the selected policy and procedure material.",
                        {"context": proposed_context},
                        {"document_ids": requested_document_ids},
                    )
                )
            if proposals:
                for accepted in self._accepted_specs("context", task, proposals):
                    accepted_context = accepted.get("context")
                    if not isinstance(accepted_context, dict):
                        raise WorkspaceError("The approved planning context must be an object.")
                    accepted_context = {
                        key: value
                        for key, value in accepted_context.items()
                        if key
                        in {
                            "objective", "entity", "period", "scope", "materiality",
                            "key_contacts", "background_notes",
                        }
                        and str(value or "").strip()
                    }
                    if accepted_context:
                        self.ws.update_planning({"context": accepted_context}, agent=True)
                        self.record_artifact("planning", "context", "planning:context", "updated", task)
        self.run["planning_basis"] = basis
        self.save()
        if task["status"] != "completed":
            self.task_status(task, "completed")
        return basis

    def _select_planning_documents(self, task: dict, context: dict) -> list[str]:
        eligible = [
            doc for doc in self.ws.documents
            if str(doc.get("text_state") or "") in ELIGIBLE_TEXT_STATES
        ]
        if not eligible:
            return []
        eligible_meta = [
            {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "category": doc.get("category"),
                "pages": doc.get("pages"),
                "text_state": doc.get("text_state"),
            }
            for doc in eligible
        ]
        eligible_ids = {str(doc.get("id")) for doc in eligible}
        payload = self.llm_json(
            prompts.DOCUMENT_SELECTION_SYSTEM,
            prompts.document_selection_user(context, eligible_meta),
        )
        proposals = []
        seen: set[str] = set()
        for item in payload.get("selected") or []:
            if not isinstance(item, dict):
                continue
            document_id = str(item.get("id") or "")
            if document_id not in eligible_ids or document_id in seen:
                continue
            seen.add(document_id)
            doc = next(entry for entry in eligible if str(entry.get("id")) == document_id)
            proposals.append(
                self.proposal_item(
                    str(doc.get("title") or document_id),
                    str(item.get("reason") or "Relevant to the planning basis."),
                    {"document_id": document_id},
                )
            )
            if len(proposals) >= MAX_SOURCE_DOCUMENTS:
                break
        if not proposals:
            return []
        confirmed = [
            str(spec.get("document_id"))
            for spec in self._accepted_specs("documents", task, proposals)
            if str(spec.get("document_id") or "") in eligible_ids
        ]
        return confirmed[:MAX_SOURCE_DOCUMENTS]

    def _accepted_specs(self, kind: str, task: dict, proposals: list[dict]) -> list[dict]:
        if self.run["mode"] == "permission":
            return [item["spec"] for item in self.request_approval(kind, task, proposals)]
        return [item["spec"] for item in proposals]

    def _quality_draft(self, system: str, user: str, validator, label: str) -> dict:
        payload = self.llm_json(system, user)
        if not self.context.get("require_planning_quality"):
            return payload
        error = validator(payload)
        if not error:
            return payload
        repair = (
            f"{user}\n\nThe previous {label} draft failed the engagement quality gate: {error}. "
            "Return a complete corrected JSON object that satisfies every supplied template and field requirement."
        )
        payload = self.llm_json(system, repair)
        error = validator(payload)
        if error:
            raise WorkspaceError(f"The {label} draft failed the engagement quality gate: {error}")
        return payload

    @staticmethod
    def _apm_quality(payload: dict, template: str) -> str | None:
        markdown = str(payload.get("apm_markdown") or "").strip()
        if not markdown:
            return "the memorandum is empty"
        headings = {
            match.group(1).strip().casefold()
            for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", markdown, re.MULTILINE)
        }
        required = [
            match.group(1).strip().casefold()
            for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", template, re.MULTILINE)
        ]
        missing = [heading for heading in required if heading not in headings]
        if missing:
            return f"missing template section '{missing[0]}'"
        return None

    @staticmethod
    def _rcm_quality(payload: dict) -> str | None:
        rows = payload.get("rows") or []
        if not isinstance(rows, list) or not rows:
            return "no RCM rows were proposed"
        required = (
            "process", "risk", "risk_rating", "assertion", "control",
            "control_type", "test_procedure",
        )
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                return f"RCM row {index} is not an object"
            missing = [key for key in required if not str(row.get(key) or "").strip()]
            if missing:
                return f"RCM row {index} is missing {missing[0]}"
            if str(row.get("risk_rating")).casefold() not in {"low", "medium", "high", "critical"}:
                return f"RCM row {index} has an unsupported risk rating"
        return None

    @staticmethod
    def _program_quality(payload: dict) -> str | None:
        procedures = payload.get("procedures") or []
        if not isinstance(procedures, list) or not procedures:
            return "no audit procedures were proposed"
        required = ("objective", "criteria", "method", "expected_evidence")
        for index, procedure in enumerate(procedures, start=1):
            if not isinstance(procedure, dict):
                return f"procedure {index} is not an object"
            missing = [key for key in required if not str(procedure.get(key) or "").strip()]
            if missing:
                return f"procedure {index} is missing {missing[0]}"
            if not isinstance(procedure.get("steps"), list) or not any(
                str(value or "").strip() for value in procedure["steps"]
            ):
                return f"procedure {index} has no executable steps"
            if not isinstance(procedure.get("rcm_refs"), list) or not procedure["rcm_refs"]:
                return f"procedure {index} is not linked to an RCM row"
        return None

    def stage_apm(self, basis: dict) -> str:
        task = self.add_task("apm", "planning:apm", "Draft the audit planning memorandum")
        if task["status"] == "completed":
            return str(self.ws.planning.get("apm_markdown") or "")
        self.task_status(task, "running")
        template = templates_store.get_template(self.ws, "apm")["markdown"]
        payload = self._quality_draft(
            prompts.APM_SYSTEM,
            prompts.apm_user(template, basis),
            lambda value: self._apm_quality(value, template),
            "APM",
        )
        markdown = str(payload.get("apm_markdown") or "").strip()
        if not markdown:
            raise WorkspaceError("The model returned an empty APM draft.")
        markdown = fill_unavailable_placeholders(markdown)
        proposals = [
            self.proposal_item(
                "Audit planning memorandum",
                "Drafted from the current planning basis.",
                {"apm_markdown": markdown},
            )
        ]
        accepted = self._accepted_specs("apm", task, proposals)
        if accepted:
            accepted_markdown = str(accepted[0].get("apm_markdown") or "").strip()
            if not accepted_markdown:
                raise WorkspaceError("The approved APM draft is empty.")
            existing = str(self.ws.planning.get("apm_markdown") or "")
            auditor_owned = (
                existing
                and self.ws.planning.get("created_by") == "user"
                and self.ws.planning.get("agent_run_id") != self.run["id"]
            )
            if auditor_owned:
                self.warn("Preserved the auditor-edited APM; the rerun draft was not applied.")
            else:
                self.ws.update_planning(
                    {
                        "apm_markdown": accepted_markdown,
                        "created_by": "agent",
                        "agent_run_id": self.run["id"],
                    },
                    agent=True,
                )
                self.record_artifact("planning", "apm", "planning:apm", "updated", task)
        self.task_status(task, "completed")
        return str(self.ws.planning.get("apm_markdown") or markdown)

    def stage_rcm(self, basis: dict, apm: str) -> list[dict]:
        task = self.add_task("rcm", "planning:rcm", "Draft the risk and control matrix")
        if task["status"] == "completed":
            return self.ws.rcm
        self.task_status(task, "running")
        template = templates_store.get_template(self.ws, "rcm")["markdown"]
        payload = self._quality_draft(
            prompts.RCM_SYSTEM,
            prompts.rcm_user(template, basis, apm),
            self._rcm_quality,
            "RCM",
        )
        proposals = []
        for row in payload.get("rows") or []:
            if not isinstance(row, dict) or not str(row.get("risk") or "").strip():
                continue
            spec = dict(row)
            spec["semantic_id"] = f"rcm:{slugify(spec.get('process', ''))}:{slugify(spec['risk'])}"
            proposals.append(
                self.proposal_item(
                    str(spec["risk"]), "Proposed planning risk and response.", spec
                )
            )
        for spec in self._accepted_specs("rcm", task, proposals):
            if not str(spec.get("risk") or "").strip():
                raise WorkspaceError("An approved RCM row is missing its risk.")
            semantic = str(
                spec.get("semantic_id")
                or f"rcm:{slugify(spec.get('process', ''))}:{slugify(spec['risk'])}"
            )
            spec["semantic_id"] = semantic
            existing = self.ws.find_semantic("rcm", semantic)
            if existing and existing.get("created_by") != "agent":
                self.warn(f"Preserved auditor-edited RCM row '{existing['id']}'.")
                continue
            if existing:
                changes = {
                    key: spec.get(key)
                    for key in (
                        "process", "risk", "risk_rating", "assertion", "control",
                        "control_type", "test_procedure",
                    )
                }
                item = self.ws.update_rcm(existing["id"], changes, agent=True)
                action = "updated"
            else:
                item = self.ws.add_rcm({**spec, "agent_run_id": self.run["id"]})
                action = "created"
            self.record_artifact("rcm", item["id"], semantic, action, task)
        self.task_status(task, "completed")
        return self.ws.rcm

    def stage_work_program(self, basis: dict, rcm_rows: list[dict]) -> None:
        task = self.add_task("work_program", "planning:work_program", "Draft the audit program")
        if task["status"] == "completed":
            return
        self.task_status(task, "running")
        template = templates_store.get_template(self.ws, "workpaper")["markdown"]
        payload = self._quality_draft(
            prompts.WORK_PROGRAM_SYSTEM,
            prompts.work_program_user(template, basis, rcm_rows),
            self._program_quality,
            "audit program",
        )
        proposals = []
        for procedure in payload.get("procedures") or []:
            if not isinstance(procedure, dict) or not str(procedure.get("objective") or "").strip():
                continue
            spec = dict(procedure)
            spec["semantic_id"] = f"procedure:{slugify(spec.get('stable_slug') or spec['objective'])}"
            spec["rcm_refs"] = self._resolve_rcm_refs(spec.get("rcm_refs") or [])
            spec["methodology_refs"] = [
                {
                    key: item[key]
                    for key in ("pack_id", "pack_name", "version", "sha1", "section", "citation")
                }
                for item in basis.get("methodology") or []
            ]
            proposals.append(
                self.proposal_item(
                    str(spec["objective"]), "Procedure linked to the draft RCM.", spec
                )
            )
        for spec in self._accepted_specs("work_program", task, proposals):
            if not str(spec.get("objective") or "").strip():
                raise WorkspaceError("An approved procedure is missing its objective.")
            semantic = str(
                spec.get("semantic_id")
                or f"procedure:{slugify(spec.get('stable_slug') or spec['objective'])}"
            )
            spec["semantic_id"] = semantic
            spec["rcm_refs"] = self._resolve_rcm_refs(spec.get("rcm_refs") or [])
            existing = self.ws.find_semantic("procedures", semantic)
            if existing and existing.get("created_by") != "agent":
                self.warn(f"Preserved auditor-edited procedure '{existing['id']}'.")
                continue
            fields = (
                "rcm_refs", "objective", "criteria", "steps", "method", "expected_evidence",
                "evidence_refs", "methodology_refs", "result_summary", "conclusion",
                "scope_limitations",
            )
            if existing:
                item = self.ws.update_procedure(
                    existing["id"],
                    {key: spec.get(key) for key in fields if key in spec},
                    agent=True,
                )
                action = "updated"
            else:
                item = self.ws.add_procedure({**spec, "agent_run_id": self.run["id"]})
                action = "created"
            self.record_artifact("procedure", item["id"], semantic, action, task)
        self.task_status(task, "completed")

    def _resolve_rcm_refs(self, refs: list) -> list[str]:
        resolved = []
        for ref in refs:
            value = str(ref)
            row = next(
                (
                    item for item in self.ws.rcm
                    if item.get("id") == value or item.get("semantic_id") == value
                ),
                None,
            )
            if row and row["id"] not in resolved:
                resolved.append(row["id"])
        return resolved

    def stage_verify(self) -> None:
        task = self.add_task("verify", "planning:verify", "Verify planning traceability")
        if task["status"] == "completed":
            return
        self.set_status("verifying")
        self.task_status(task, "running")
        rcm_ids = {row["id"] for row in self.ws.rcm}
        issues = []
        for procedure in self.ws.work_program:
            missing = [ref for ref in procedure.get("rcm_refs", []) if ref not in rcm_ids]
            if missing:
                issues.append(f"{procedure['id']} has missing RCM links: {', '.join(missing)}")
        self.run["verification"] = {"ok": not issues, "issues": issues}
        for issue in issues:
            self.warn(issue)
        self.save()
        self.task_status(task, "completed")

    def stage_summary(self) -> None:
        task = self.add_task("summary", "planning:summary", "Summarize planning drafts")
        if task["status"] == "completed":
            return
        self.set_status("summarizing")
        self.task_status(task, "running")
        self.run["summary_markdown"] = (
            "# Planning draft summary\n\n"
            f"Prepared an audit planning memorandum, **{len(self.ws.rcm)}** RCM row(s), "
            f"and **{len(self.ws.work_program)}** audit procedure(s). "
        )
        self.save()
        self.task_status(task, "completed")

    def _finish_planning(self) -> None:
        self.stage_verify()
        self.stage_summary()
        self.run["finished"] = store.utcnow()
        self.run["command"]["status"] = "completed"
        self.set_status("completed")
        self.emit("summary_ready", {"run_id": self.run["id"]})

    def _remove_redundant_planning_actions(self, proposals: list[dict]) -> list[dict]:
        removed_ids = {
            str(item.get("id") or "") for item in proposals
            if isinstance(item, dict) and item.get("type") in self.PLANNING_ACTION_TYPES
        }
        cleaned = []
        for proposal in proposals:
            if not isinstance(proposal, dict) or proposal.get("type") in self.PLANNING_ACTION_TYPES:
                continue
            item = dict(proposal)
            dependencies = item.get("depends_on") or []
            if isinstance(dependencies, list):
                item["depends_on"] = [str(value) for value in dependencies if str(value) not in removed_ids]
            cleaned.append(item)
        return cleaned

    def _is_full_audit_goal(self, goal: dict | None = None) -> bool:
        command = self.run.get("command") or {}
        if self._requests_full_audit():
            return True
        goal = goal or self.run.get("goal") or {}
        combined = " ".join([
            str(command.get("text") or ""),
            str(goal.get("objective") or ""),
            *[str(value) for value in goal.get("completion_criteria") or []],
        ]).casefold()
        wants_report = "report" in combined and any(word in combined for word in ("draft", "generate", "complete"))
        wants_full_cycle = any(phrase in combined for phrase in (
            "complete the audit", "full audit", "all steps", "all procedures", "all_procedures",
        ))
        return wants_report and wants_full_cycle

    @staticmethod
    def _proposal_repair_user(base_user: str, payload: dict, error: Exception) -> str:
        return (
            f"{base_user}\n\nYour previous JSON parsed, but its action graph violated the registered "
            f"contract: {error}. Return a corrected complete JSON object. Preserve the intended goal and "
            f"valid dependencies, use only catalog target kinds and required args. Previous JSON: "
            f"{json.dumps(payload, default=str)}"
        )

    def _record_rejected_proposals(self, stage: str, proposals: object, error: Exception) -> None:
        safe_actions = []
        if isinstance(proposals, list):
            for proposal in proposals[: int(self.run["limits"].get("max_actions", 60))]:
                if not isinstance(proposal, dict):
                    safe_actions.append({"value_type": type(proposal).__name__})
                    continue
                target = proposal.get("target") if isinstance(proposal.get("target"), dict) else {}
                args = proposal.get("args") if isinstance(proposal.get("args"), dict) else {}
                dependencies = proposal.get("depends_on") if isinstance(proposal.get("depends_on"), list) else []
                safe_actions.append({
                    "id": str(proposal.get("id") or ""),
                    "type": str(proposal.get("type") or ""),
                    "target": {
                        "kind": target.get("kind"),
                        "selector": str(target.get("selector"))[:200] if target.get("selector") else None,
                        "resolved_id": str(target.get("resolved_id"))[:200] if target.get("resolved_id") else None,
                    },
                    "depends_on": [str(value) for value in dependencies],
                    "arg_keys": sorted(str(key) for key in args),
                })
        diagnostic = {
            "at": store.utcnow(), "stage": stage, "error": str(error), "actions": safe_actions,
        }
        self.run.setdefault("rejected_proposals", []).append(diagnostic)
        self.save()
        self.emit("proposal_rejected", diagnostic)

    def _recover_running_actions(self) -> None:
        changed = False
        for action in self.run.get("actions") or []:
            if action["status"] != "running":
                continue
            definition = actions.validate_action(action)
            outcome = definition.reconciler(self.ws, action) if definition.reconciler else "retry"
            if outcome == "already_applied":
                ledger.transition(action, "succeeded")
                action["finished_at"] = store.utcnow()
            elif outcome == "retry":
                ledger.transition(action, "failed")
                ledger.transition(action, "ready")
            else:
                ledger.transition(action, "awaiting_input")
                interaction = ledger.interaction(
                    self.run, action, "conflict_resolution",
                    "This artifact changed while the action was interrupted. Choose how to proceed.",
                    options=[{"value": "skip", "label": "Keep current and skip"}, {"value": "retry", "label": "Revalidate and retry"}],
                    policy_reason="The current artifact matches neither the prepared before nor after state.",
                )
                self.emit("action_conflict", {"action_id": action["id"], "interaction_id": interaction["id"]})
            changed = True
        if changed:
            ledger.project_legacy_plan(self.run); self.save()

    def _drive_graph(self) -> None:
        self.set_status("executing")
        while True:
            self.checkpoint()
            self._block_failed_dependencies()
            pending_interaction = next((item for item in self.run.get("interactions") or [] if item["status"] == "pending"), None)
            if pending_interaction:
                action = self._action(pending_interaction["action_id"])
                if self._dismiss_obsolete_interaction(action, pending_interaction):
                    continue
                self._wait_interaction(action, pending_interaction)
                continue
            proposed = next((action for action in self.run["actions"] if action["status"] == "proposed"), None)
            if proposed:
                self._resolve_and_gate(proposed)
                continue
            ready = next((action for action in self.run["actions"] if action["status"] == "ready" and self._dependencies_succeeded(action)), None)
            if ready:
                self._execute_action(ready)
                continue
            if any(action["status"] in {"running", "awaiting_input", "awaiting_confirmation"} for action in self.run["actions"]):
                time.sleep(0.05)
                continue
            break

    def _pending_target_producer(self, action: dict) -> dict | None:
        """Return a dependency that will create this action's target locally."""
        target = action.get("target") or {}
        target_kind = str(target.get("kind") or "")
        target_id = str(target.get("resolved_id") or "")
        if not target_kind or not target_id:
            return None
        by_id = {item["id"]: item for item in self.run.get("actions") or []}
        pending = list(action.get("depends_on") or [])
        seen = set()
        while pending:
            dependency_id = pending.pop()
            if dependency_id in seen or dependency_id not in by_id:
                continue
            seen.add(dependency_id)
            dependency = by_id[dependency_id]
            if dependency["status"] != "succeeded":
                expected = actions.expected_postcondition(dependency)
                produced_kind = str(expected.get("kind") or "")
                produced_id = str(expected.get("id") or "")
                exact_match = produced_kind == target_kind and produced_id == target_id
                child_match = (
                    target_kind == "doctest_item"
                    and produced_kind == "doctest"
                    and bool(produced_id)
                    and target_id.startswith(f"{produced_id}:")
                )
                generated_report = (
                    target_kind == "report" and target_id == "working"
                    and dependency["type"] in {"generate_report", "edit_report"}
                )
                if exact_match or child_match or generated_report:
                    return dependency
            pending.extend(dependency.get("depends_on") or [])
        return None

    def _dismiss_obsolete_interaction(self, action: dict, interaction: dict) -> bool:
        """Resume runs paused by interactions made obsolete by local dependencies."""
        if interaction.get("type") == "conflict_resolution":
            target = action.get("target") or {}
            if not target.get("kind") or not target.get("resolved_id"):
                return False
            resolution = artifact_index.resolve(
                artifact_index.build(self.ws), target["kind"], None, target["resolved_id"]
            )
            dependency = self._dependency_post_state_source(action, resolution)
            if dependency is None:
                return False
            snapshot = actions.artifact_snapshot(self.ws, target["kind"], target["resolved_id"])
            interaction.update(
                status="resolved",
                response={
                    "decision": "rebased_to_dependency",
                    "dependency_action_id": dependency["id"],
                },
                actor="orchestrator",
                resolved_at=store.utcnow(),
            )
            action["resolution"] = resolution
            action["precondition"] = {
                "artifact_sha1": artifact_index.canonical_sha1(snapshot) if snapshot is not None else None,
                "snapshot": store.write_sidecar(self.ws, self.run["id"], snapshot) if snapshot is not None else None,
            }
            if action["status"] == "awaiting_input":
                ledger.transition(action, "ready")
            self._save_action(action)
            self.emit("interaction_resolved", {
                "interaction_id": interaction["id"], "action_id": action["id"],
                "decision": "rebased_to_dependency",
            })
            return True
        if interaction.get("type") not in {"clarification", "target_choice"}:
            return False
        adjustments = ledger.normalize_created_targets(self.run, [action])
        if adjustments:
            self.run.setdefault("lifecycle_adjustments", []).extend(adjustments)
        producer = self._pending_target_producer(action)
        if producer is None:
            return False
        interaction.update(
            status="resolved",
            response={"decision": "deferred_to_dependency", "producer_action_id": producer["id"]},
            actor="orchestrator",
            resolved_at=store.utcnow(),
        )
        action["target"]["selector"] = None
        action["resolution"] = None
        action["precondition"] = None
        if action["status"] == "awaiting_input":
            ledger.transition(action, "proposed")
        self._save_action(action)
        self.emit("interaction_resolved", {
            "interaction_id": interaction["id"], "action_id": action["id"],
            "decision": "deferred_to_dependency",
        })
        return True

    def _resolve_and_gate(self, action: dict) -> None:
        target = action["target"]
        actions.canonicalize_action_fields(self.ws, action)
        definition = actions.REGISTRY.get(action["type"], action["definition_version"])
        # Also normalize pre-fix persisted proposals when an interrupted run
        # is resumed. New proposals are normalized by the ledger.
        adjustments = ledger.normalize_created_targets(self.run, [action])
        if adjustments:
            self.run.setdefault("lifecycle_adjustments", []).extend(adjustments)
        if definition.target_kinds and not target.get("kind") and len(definition.target_kinds) == 1:
            target["kind"] = definition.target_kinds[0]
        definition = actions.validate_action(action)
        if definition.target_kinds:
            defaults = {"planning": "apm", "report": "working"}
            if not target.get("resolved_id") and target.get("kind") in defaults and not target.get("selector"):
                target["resolved_id"] = defaults[target["kind"]]
            index = artifact_index.build(self.ws)
            producer = self._pending_target_producer(action)
            if producer is not None:
                resolution = {
                    "index_revision": index["revision"],
                    "resolved_id": target["resolved_id"],
                    "resolved_ref": f"{target['kind']}:{target['resolved_id']}",
                    "confidence": 1.0,
                    "reason": f"target will be created by dependency '{producer['id']}'",
                    "candidates": [], "sha1": None,
                    "title": target["resolved_id"], "created_by": "agent",
                    "producer_action_id": producer["id"],
                }
            else:
                resolution = artifact_index.resolve(
                    index, target["kind"], target.get("selector"), target.get("resolved_id")
                )
            action["resolution"] = resolution
            if not resolution.get("resolved_id"):
                if resolution.get("candidates"):
                    ledger.transition(action, "awaiting_input")
                    interaction = ledger.interaction(
                        self.run, action, "target_choice", "Choose the exact artifact this command should affect.",
                        options=resolution["candidates"], policy_reason=resolution["reason"],
                    )
                else:
                    ledger.transition(action, "awaiting_input")
                    interaction = ledger.interaction(
                        self.run, action, "clarification", "Which artifact should this action affect?",
                        policy_reason="No local artifact matched the selector.",
                    )
                self._save_action(action)
                self.emit("interaction_request", {"interaction": interaction})
                return
            target["resolved_id"] = resolution["resolved_id"]
            if producer is None and definition.risk not in {"read", "compute"}:
                snapshot = actions.artifact_snapshot(self.ws, target["kind"], target["resolved_id"])
                action["precondition"] = {
                    "artifact_sha1": artifact_index.canonical_sha1(snapshot) if snapshot is not None else None,
                    "snapshot": store.write_sidecar(self.ws, self.run["id"], snapshot) if snapshot is not None else None,
                }
        if actions.approval_required(definition, self.run, action):
            ledger.transition(action, "awaiting_confirmation")
            type_ = "confirmation" if definition.risk == "destructive" else "proposal_approval"
            resolution = action.get("resolution") or {}
            prompt = (
                f"Confirm {definition.description.lower()} for {resolution.get('title') or action['type']}."
                if type_ == "confirmation" else f"Review and approve: {definition.description}."
            )
            payload = {"action_type": action["type"], "args": action["args"], "target": action["target"], "resolution": resolution, "before": action.get("precondition")}
            ref = store.write_sidecar(self.ws, self.run["id"], payload)
            interaction = ledger.interaction(
                self.run, action, type_, prompt,
                options=[{"value": "approve", "label": "Approve"}, {"value": "reject", "label": "Reject"}],
                payload={"sidecar": ref, "preview": {"action_type": action["type"], "target": resolution.get("resolved_ref"), "title": resolution.get("title"), "args": action["args"] if len(json.dumps(action["args"], default=str)) <= 4000 else "Stored in sidecar"}},
                policy_reason=f"{definition.risk} actions require review under the selected mode.",
            )
            self._save_action(action); self.emit("interaction_request", {"interaction": interaction})
            return
        ledger.transition(action, "ready")
        self._save_action(action)

    def _execute_action(self, action: dict) -> None:
        definition = actions.validate_action(action)
        self.checkpoint()
        if action["type"] == "classify_import_batch":
            from .. import intake
            from .intake_runner import IntakeRunner

            batch = intake.load_batch(self.ws, action["args"]["batch_id"])
            IntakeRunner(self.ws, self.run, self.handle)._classify(batch)
            intake.save_batch(self.ws, batch)
        # Re-resolve immediately before mutations and capture a hashed before snapshot.
        if definition.target_kinds:
            index = artifact_index.build(self.ws)
            resolution = artifact_index.resolve(index, action["target"]["kind"], None, action["target"].get("resolved_id"))
            previous = action.get("resolution") or {}
            resolution_changed = bool(
                previous.get("sha1") and previous.get("sha1") != resolution.get("sha1")
            )
            dependency_source = (
                self._dependency_post_state_source(action, resolution)
                if resolution_changed else None
            )
            if not resolution.get("resolved_id") or (resolution_changed and dependency_source is None):
                ledger.transition(action, "awaiting_input")
                current_snapshot = actions.artifact_snapshot(self.ws, action["target"]["kind"], action["target"]["resolved_id"])
                comparison = store.write_sidecar(self.ws, self.run["id"], {
                    "before": store.read_sidecar(self.ws, self.run["id"], (action.get("precondition") or {}).get("snapshot")) if (action.get("precondition") or {}).get("snapshot") else None,
                    "current": current_snapshot, "proposed_args": action["args"],
                })
                interaction = ledger.interaction(
                    self.run, action, "conflict_resolution", "The target changed after planning. Review before continuing.",
                    options=[{"value": "retry", "label": "Use current version"}, {"value": "skip", "label": "Skip action"}],
                    payload={"target": resolution, "comparison_sidecar": comparison}, policy_reason="Optimistic precondition failed.",
                )
                self._save_action(action); self.emit("action_conflict", {"action_id": action["id"], "interaction_id": interaction["id"]})
                return
            action["resolution"] = resolution
            snapshot = actions.artifact_snapshot(self.ws, action["target"]["kind"], action["target"]["resolved_id"])
            if dependency_source is not None or not action.get("precondition"):
                action["precondition"] = {
                    "artifact_sha1": artifact_index.canonical_sha1(snapshot) if snapshot is not None else None,
                    "snapshot": store.write_sidecar(self.ws, self.run["id"], snapshot) if snapshot is not None else None,
                }
        if definition.risk not in {"read", "compute"}:
            action["postcondition"] = actions.expected_postcondition(action)
        ledger.transition(action, "running")
        action["attempts"] += 1; action["prepared_at"] = store.utcnow(); action["started_at"] = store.utcnow()
        self.run["usage"]["actions_started"] += 1
        self._save_action(action)
        # Only the executor call may route to the failure path. Once the
        # action commits as succeeded, post-success bookkeeping and planning
        # waves must never flip its outcome — succeeded is terminal.
        try:
            receipt = definition.executor(self.ws, action, self.run)
        except Exception as error:
            action["error"] = str(error); action["finished_at"] = store.utcnow()
            ledger.transition(action, "failed")
            attempts = int(self.run["limits"].get("max_execution_attempts", 2))
            if action["attempts"] < attempts and definition.failure_policy != "stop_run":
                ledger.transition(action, "ready")
            self._save_action(action)
            if action.get("planning_significant") and action["status"] == "failed":
                self._expand_after(action, {"error": str(error)})
            if definition.failure_policy == "stop_run":
                raise
            return
        action["receipt"] = receipt
        action["result_refs"] = list(receipt.get("result_refs") or [])
        action["finished_at"] = store.utcnow()
        ledger.transition(action, "succeeded")
        self.run["artifacts"].extend(
            {"kind": ref.partition(":")[0], "id": ref.partition(":")[2], "semantic_id": "", "action": "updated"}
            for ref in action["result_refs"]
            if not any(item.get("kind") == ref.partition(":")[0] and item.get("id") == ref.partition(":")[2] for item in self.run["artifacts"])
        )
        self._save_action(action)
        warning = (receipt.get("result") or {}).get("warning")
        if warning:
            self.warn(str(warning))
        for ref in action["result_refs"]:
            kind, _, item_id = ref.partition(":")
            self.emit("workspace_changed", {"kind": kind, "id": item_id, "action": "removed" if definition.risk == "destructive" else "updated"})
        if action.get("planning_significant"):
            self._expand_after(action)

    def _expand_after(self, action: dict, safe_result: dict | None = None) -> None:
        if self.run.get("planning_expansion_disabled"):
            return
        usage = self.run["usage"]
        if usage["planner_waves"] >= int(self.run["limits"].get("max_waves", 8)):
            self.warn("Planning-wave limit reached; the current graph will finish without further expansion.")
            return
        safe_result = safe_result if safe_result is not None else ((action.get("receipt") or {}).get("result") or {})
        usage["planner_waves"] += 1; self.save()
        index = artifact_index.build(self.ws)
        base_user = prompts.command_planner_user(
            self.run["goal"],
            [{"id": item["id"], "type": item["type"], "status": item["status"], "result_refs": item["result_refs"]} for item in self.run["actions"]],
            [{"action_id": action["id"], "result": safe_result}],
            artifact_index.compact(index), self._catalog(), self.run["limits"],
            assistant.schema_brief(self.ws),
        )
        attempt_user = base_user
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            try:
                payload = self.llm_json(prompts.COMMAND_PLANNER_SYSTEM, attempt_user)
            except (LimitExceeded, llm.LLMError) as error:
                # The initial graph is already validated and may contain
                # independent local work. Adaptive expansion is optional, so
                # a provider outage must not strand that work in ready state.
                self.run["planning_expansion_disabled"] = True
                self.warn(f"Further planning expansion skipped: {error}")
                return
            proposals = payload.get("actions") or []
            if not proposals:
                return
            # Expansion is opportunistic follow-up planning; even repeated
            # invalid proposals must not fail work that already committed.
            try:
                for proposal in proposals:
                    if isinstance(proposal, dict):
                        actions.canonicalize_action_fields(self.ws, proposal)
                created = ledger.append_actions(
                    self.run, proposals, depth=action["depth"] + 1,
                    audit_lifecycle=self._is_full_audit_goal(),
                )
            except WorkspaceError as error:
                self._record_rejected_proposals("command_planner", proposals, error)
                if attempt + 1 >= SEMANTIC_PROPOSAL_ATTEMPTS:
                    self.warn(f"Discarded an invalid planning proposal: {error}")
                    return
                attempt_user = self._proposal_repair_user(base_user, payload, error)
                continue
            self.save()
            self.emit("graph_update", {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]})
            return

    def _wait_interaction(self, action: dict, interaction: dict) -> None:
        self.set_status("awaiting_input" if interaction["type"] in {"clarification", "target_choice", "conflict_resolution"} else "awaiting_approval")
        waited_from = time.monotonic()
        response = None
        while response is None:
            if self.handle.cancel.is_set():
                raise Cancelled()
            if self.handle.command_queued.is_set():
                self._drain_inbox()
            if self.handle.interaction_resolved.wait(0.05):
                self.handle.interaction_resolved.clear()
                response = self.handle.interaction_responses.pop(interaction["id"], None)
        self.deadline += time.monotonic() - waited_from
        decision = str(response.get("decision") or response.get("choice") or "").strip()
        interaction.update(status="resolved", response=response, actor="auditor", resolved_at=store.utcnow())
        if interaction["type"] == "clarification":
            text = str(response.get("text") or "").strip()
            if not text:
                raise WorkspaceError("A clarification response is required.")
            action["target"]["selector"] = text; ledger.transition(action, "proposed")
        elif interaction["type"] == "target_choice":
            choice = str(response.get("choice") or "")
            option = next((item for item in interaction["options"] if item.get("ref") == choice or item.get("id") == choice), None)
            if option is None:
                raise WorkspaceError("Choose one of the offered targets.")
            action["target"]["resolved_id"] = option["id"]; action["target"]["selector"] = None
            ledger.transition(action, "proposed")
        elif interaction["type"] in {"confirmation", "proposal_approval"}:
            if decision == "approve":
                if isinstance(response.get("args"), dict):
                    action["args"] = response["args"]; actions.validate_action(action)
                ledger.transition(action, "ready")
            else:
                ledger.transition(action, "skipped")
        else:
            if decision == "retry":
                action["resolution"] = None; action["precondition"] = None
                ledger.transition(action, "proposed")
            else:
                ledger.transition(action, "skipped")
        self.set_status("executing")
        self._save_action(action)
        self.emit("interaction_resolved", {"interaction_id": interaction["id"], "action_id": action["id"], "decision": decision})

    def _block_failed_dependencies(self) -> None:
        by_id = {action["id"]: action for action in self.run["actions"]}
        changed = False
        for action in self.run["actions"]:
            if action["status"] not in {"proposed", "ready"}:
                continue
            if any(by_id[dep]["status"] in {"failed", "blocked", "cancelled", "skipped"} for dep in action["depends_on"]):
                ledger.transition(action, "blocked"); action["error"] = "A required action did not succeed."; changed = True
        if changed:
            ledger.project_legacy_plan(self.run); self.save()

    def _dependencies_succeeded(self, action: dict) -> bool:
        by_id = {item["id"]: item for item in self.run["actions"]}
        return all(by_id[value]["status"] == "succeeded" for value in action["depends_on"])

    def _dependency_post_state_source(self, action: dict, resolution: dict) -> dict | None:
        """Identify a dependency that produced the target's current version.

        Proposed actions are resolved before ready actions execute, so two
        dependent mutations of the same artifact initially retain the same
        optimistic snapshot.  The second action may safely rebase only when
        the current artifact exactly matches a succeeded dependency's durable
        receipt; any later auditor or external edit still becomes a conflict.
        """
        current_sha1 = resolution.get("sha1")
        target_ref = resolution.get("resolved_ref")
        if not current_sha1 or not target_ref:
            return None
        by_id = {item["id"]: item for item in self.run.get("actions") or []}
        pending = list(action.get("depends_on") or [])
        seen = set()
        while pending:
            dependency_id = pending.pop()
            if dependency_id in seen or dependency_id not in by_id:
                continue
            seen.add(dependency_id)
            dependency = by_id[dependency_id]
            receipt = dependency.get("receipt") or {}
            if (
                dependency.get("status") == "succeeded"
                and receipt.get("post_sha1") == current_sha1
                and target_ref in (dependency.get("result_refs") or [])
            ):
                return dependency
            pending.extend(dependency.get("depends_on") or [])
        return None

    def _action(self, action_id: str) -> dict:
        return next(item for item in self.run["actions"] if item["id"] == action_id)

    def _save_action(self, action: dict) -> None:
        ledger.project_legacy_plan(self.run); self.save()
        self.emit("action_update", {"action": {k: action.get(k) for k in ("id", "type", "status", "error", "result_refs", "attempts")}})

    def _finish(self, force_issue: bool = False) -> None:
        if self.run["status"] in store.TERMINAL_STATUSES:
            return
        self._drain_inbox()
        self.set_status("verifying")
        failed = [item for item in self.run.get("actions") or [] if item["status"] in {"failed", "blocked", "cancelled"}]
        succeeded = [item for item in self.run.get("actions") or [] if item["status"] == "succeeded"]
        skipped = [item for item in self.run.get("actions") or [] if item["status"] == "skipped"]
        lines = [f"## Command result", "", self.run["goal"].get("objective") or self.run["command"].get("text") or "Audit command", ""]
        lines.append(f"Completed {len(succeeded)} action(s); {len(failed)} failed or blocked; {len(skipped)} skipped.")
        if succeeded:
            lines.extend(["", "### Committed work", *[f"- {actions.REGISTRY.get(item['type'], item['definition_version']).description}" for item in succeeded]])
        if failed:
            lines.extend(["", "### Issues", *[f"- {item['type']}: {item.get('error') or item['status']}" for item in failed]])
        lines.extend(["", "This is assistant working content, not a formal audit-stage conclusion or audit opinion."])
        self.run["summary_markdown"] = "\n".join(lines)
        self.run["finished"] = store.utcnow(); self.run["command"]["status"] = "completed"
        status = "completed_with_issues" if force_issue or failed else "completed"
        self.set_status(status); self.emit("summary_ready", {"run_id": self.run["id"]})
